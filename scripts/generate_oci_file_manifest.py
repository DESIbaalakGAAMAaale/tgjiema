#!/usr/bin/env python3
"""R70 Wave 8 P0-09: 精确 OCI 文件清单 manifest 生成器。

R70 P0-09 整改背景:
    生产镜像构建时没有精确文件清单,未知文件可进入镜像,违反不可变部署原则。
    本脚本通过解析 Dockerfile + .dockerignore,静态推导出预期的 /app 文件清单
    作为 SBOM(Software Bill of Materials),用于:

      1. 构建前:验证 Dockerfile COPY 指令只复制白名单目录/文件
      2. 构建后:与实际镜像 filesystem 对比(通过 --validate <tar>),
         发现任何未声明文件即 fail-closed
      3. 发布时:作为 release artifact 一部分,绑定到 image digest

设计要点:
    - 不依赖 docker 库(本地无 Docker daemon),纯静态解析
    - 使用结构化 tokenizer 解析 Dockerfile(类似 AST 的方式)
    - 应用 .dockerignore 规则(gitignore 语义)过滤文件
    - 应用 Dockerfile RUN rm 指令(第二道防线)过滤文件
    - --strict 模式:文件不在 ALLOWED_ROOTS 白名单时 raise(fail-closed)
    - --validate 模式:与 docker image save 后的 tar 内容对比

输出 JSON 格式:
    {
      "schema_version": "1.0",
      "generated_at": "<ISO8601>",
      "tool_version": "R70-WAVE8-P0-09",
      "project": "tgjiema",
      "dockerfile": "Dockerfile",
      "dockerignore": ".dockerignore",
      "file_count": N,
      "files": [
        {
          "path": "services/restore_writer.py",
          "size": 12345,
          "source_instruction": "COPY services/ ./services/",
          "copy_line_number": 55
        },
        ...
      ],
      "external_copies": [
        {
          "instruction": "COPY --from=builder /app/venv /app/venv",
          "line_number": 44,
          "dest": "/app/venv",
          "source_stage": "builder"
        }
      ],
      "run_rm_paths": [...],
      "dockerignore_rules": [...]
    }

退出码:
    0 — 成功
    1 — 失败(文件缺失/解析失败/strict 违规/validate 不匹配)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"

TOOL_VERSION = "R70-WAVE8-P0-09"
MANIFEST_SCHEMA_VERSION = "1.0"

# R70 Wave 8: 允许进入 /app 的顶层路径白名单(用于 --strict 模式)。
# 派生自 Dockerfile 的 COPY 指令目标 + Dockerfile RUN mkdir 创建的目录。
# 任何不在这些根下的文件视为"未知文件",--strict 模式下 fail-closed。
ALLOWED_ROOTS: tuple[str, ...] = (
    "run_all.py",
    "requirements.txt",
    "services/",
    "bots/",
    "admin/",
    "config/",
    "database/",
    "locales/",
    "utils/",
    "storage/",
    "docker/",
    # 以下由 Dockerfile 非 COPY 指令创建(RUN mkdir / --from=builder),
    # manifest 不展开其内容,但 --strict 允许其作为顶层路径存在。
    "venv/",
    "data/",
    "logs/",
)


class StrictModeViolation(Exception):
    """--strict 模式下发现不在白名单中的文件时抛出。"""


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class CopyInstruction:
    """Dockerfile COPY/ADD 指令的解析结果。"""
    raw: str                           # 原始指令文本(合并续行后)
    instruction: str                   # "COPY" or "ADD"
    flags: tuple[str, ...]             # --from=, --chown=, --chmod= 等
    sources: tuple[str, ...]           # 源路径(可多个,工作区内)
    dest: str                          # 目标路径(镜像内,相对 WORKDIR /app)
    line_number: int                   # Dockerfile 起始行号
    is_from_external_stage: bool = False  # 是否从其他 stage 复制(--from=builder)

    def dest_in_app(self) -> str:
        """返回镜像内 /app 下的相对 POSIX 路径(如 "services/" 或 "run_all.py")。

        标准化:
          "./xxx" → "xxx"
          "." / "./" → ""
          "/app/xxx" → "xxx"
          "/xxx" → "xxx"
          "xxx" → "xxx"
        """
        d = self.dest
        if d.startswith("./"):
            d = d[2:]
        elif d in (".", "./"):
            d = ""
        if d.startswith("/app/"):
            d = d[len("/app/"):]
        elif d.startswith("/"):
            d = d.lstrip("/")
        return d

    def source_stage(self) -> str:
        """返回 --from= 指定的源 stage 名(无则空字符串)。"""
        for f in self.flags:
            if f.startswith("--from="):
                return f[len("--from="):]
        return ""


@dataclass
class RunRmInstruction:
    """Dockerfile RUN 指令中 rm 调用的解析结果。"""
    raw: str                            # 原始 RUN 指令文本
    line_number: int                    # Dockerfile 行号
    removed_paths: tuple[str, ...]      # rm 的目标路径(原始 token)


@dataclass
class FileEntry:
    """manifest 中的单个文件条目。"""
    path: str                   # 镜像内相对 /app 的 POSIX 路径
    size: int                   # 文件大小(字节)
    source_instruction: str     # 来源 COPY 指令原始文本
    copy_line_number: int       # COPY 指令所在 Dockerfile 行号
    sha256: str = ""            # 文件内容 sha256(--with-sha256 时填充)

    def to_dict(self) -> dict:
        d: dict = {
            "path": self.path,
            "size": self.size,
            "source_instruction": self.source_instruction,
            "copy_line_number": self.copy_line_number,
        }
        if self.sha256:
            d["sha256"] = self.sha256
        return d


# ════════════════════════════════════════════════════════════════
# Dockerfile 解析(结构化 tokenizer,不使用 docker 库)
# ════════════════════════════════════════════════════════════════


def parse_dockerfile(
    path: Path,
) -> tuple[list[CopyInstruction], list[RunRmInstruction]]:
    """解析 Dockerfile,提取 COPY/ADD 与 RUN rm 指令。

    处理多行命令(以 ``\\`` 续行),合并为单条逻辑行后再解析。
    跳过注释行与空行。失败时 raise(fail-closed,不吞异常)。

    Args:
        path: Dockerfile 路径

    Returns:
        (copies, rm_instructions) — COPY/ADD 指令列表与 RUN rm 指令列表

    Raises:
        FileNotFoundError: Dockerfile 不存在
        OSError: 读取失败
        ValueError: 指令格式错误
    """
    if not path.exists():
        raise FileNotFoundError(f"Dockerfile 不存在: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise OSError(f"读取 Dockerfile 失败: {e}") from e

    logical_lines = _merge_continuation_lines(content)

    copies: list[CopyInstruction] = []
    rm_instructions: list[RunRmInstruction] = []

    for line_no, logical in logical_lines:
        stripped = logical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = _tokenize_instruction(stripped)
        if not tokens:
            continue
        instr_name = tokens[0].upper()
        if instr_name in ("COPY", "ADD"):
            copies.append(_parse_copy_instruction(stripped, line_no, tokens))
        elif instr_name == "RUN":
            rm = _parse_run_rm(stripped, line_no)
            if rm is not None:
                rm_instructions.append(rm)

    return copies, rm_instructions


def _merge_continuation_lines(content: str) -> list[tuple[int, str]]:
    """合并以 ``\\`` 结尾的续行为单条逻辑行。

    Returns:
        [(起始行号, 合并后的逻辑行), ...]
    """
    result: list[tuple[int, str]] = []
    buffer = ""
    buffer_start = 0
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.rstrip()
        if not buffer:
            buffer_start = line_no
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
        else:
            buffer += stripped
            result.append((buffer_start, buffer.strip()))
            buffer = ""
    if buffer:
        result.append((buffer_start, buffer.strip()))
    return result


def _tokenize_instruction(line: str) -> list[str]:
    """把单条 Dockerfile 指令拆分为 token(支持双引号字符串)。

    例如: ``COPY --from=builder /app/venv /app/venv`` →
          ["COPY", "--from=builder", "/app/venv", "/app/venv"]
    """
    tokens: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 1
            tokens.append(line[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            tokens.append(line[i:j])
            i = j
    return tokens


def _parse_copy_instruction(
    raw: str, line_no: int, tokens: list[str]
) -> CopyInstruction:
    """解析 COPY/ADD 指令为 CopyInstruction 对象。"""
    instruction = tokens[0].upper()
    args = tokens[1:]
    flags: list[str] = []
    paths: list[str] = []
    for a in args:
        if a.startswith("--"):
            flags.append(a)
        else:
            paths.append(a)
    if len(paths) < 2:
        raise ValueError(
            f"Dockerfile 第 {line_no} 行: {instruction} 指令参数不足"
            f"(至少需要 <src> <dest>): {raw}"
        )
    *sources, dest = paths
    is_external = any(f.startswith("--from=") for f in flags)
    return CopyInstruction(
        raw=raw,
        instruction=instruction,
        flags=tuple(flags),
        sources=tuple(sources),
        dest=dest,
        line_number=line_no,
        is_from_external_stage=is_external,
    )


def _parse_run_rm(raw: str, line_no: int) -> RunRmInstruction | None:
    """解析 RUN 指令,提取 rm 调用的目标路径。

    策略:
      1. 去除前导 ``RUN `` 关键字
      2. 按 ``&&`` / ``||`` / ``;`` 拆分为子命令
      3. 跳过非 rm 子命令(如 find / true / apt-get)
      4. 对 rm 子命令:跳过 flag(-f / -rf / -fr / -r),收集路径 token
      5. 跳过 shell 重定向(2> / > / <)与特殊符号

    若 RUN 不含 rm 调用,返回 None。
    """
    cmd = raw.strip()
    if cmd.upper().startswith("RUN "):
        cmd = cmd[4:].strip()
    elif cmd.upper() == "RUN":
        return None

    if "rm " not in cmd and "rm\t" not in cmd:
        return None

    # 按 && / || / ; 拆分(\|\| 必须在 | 之前匹配,但此处无单 | 场景)
    sub_commands = re.split(r"\s*(?:&&|\|\||;)\s*", cmd)

    removed: list[str] = []
    for sub in sub_commands:
        sub = sub.strip()
        if not sub:
            continue
        # 跳过 find 命令(find ... -exec rm ... {} + 的目标由 find 动态决定,
        # 本脚本不展开 find 匹配结果,由 .dockerignore 兜底)
        if sub.startswith("find "):
            continue
        # 仅处理 rm 子命令
        if not (sub.startswith("rm ") or sub.startswith("rm\t")):
            continue
        tokens = sub.split()
        # tokens[0] == "rm",跳过 flags
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                i += 1
                break
            if tok.startswith("-") and len(tok) > 1 and not tok.startswith("-/") \
               and not tok.startswith("-*"):
                # rm flag: -f / -rf / -fr / -r / -v 等(但不包含 -/*. 这类路径)
                i += 1
                continue
            break
        # 收集路径 token
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("&&", "||", ";"):
                break
            # 跳过 shell 重定向(2>/dev/null 等)
            if ">" in tok or "<" in tok:
                i += 1
                continue
            removed.append(tok)
            i += 1

    if not removed:
        return None

    return RunRmInstruction(
        raw=raw,
        line_number=line_no,
        removed_paths=tuple(removed),
    )


# ════════════════════════════════════════════════════════════════
# .dockerignore 解析与匹配(gitignore 语义)
# ════════════════════════════════════════════════════════════════


def parse_dockerignore(path: Path) -> list[str]:
    """解析 .dockerignore,返回规则列表(按出现顺序,含 ! 取反规则)。

    去除注释与空行,保留原行文本(包括 ! 前缀)。

    Raises:
        OSError: 读取失败(path 不存在时返回空列表,不 raise)
    """
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise OSError(f"读取 .dockerignore 失败: {e}") from e

    rules: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(line)
    return rules


def is_ignored_by_dockerignore(rel_path: str, rules: list[str]) -> bool:
    """判断相对路径是否被 .dockerignore 排除。

    遵循 gitignore/dockerignore 语义:
      - 后规则覆盖前规则(! 取反)
      - 不含 ``/`` 的 pattern 匹配任意层级的路径段(文件名或目录名)
      - 含 ``/`` 的 pattern 锚定到构建上下文根
      - 尾部 ``/`` 表示仅匹配目录(及其所有内容)
      - ``*`` 匹配非 ``/`` 字符序列,``**`` 匹配任意字符(含 ``/``)

    Args:
        rel_path: 相对构建上下文根的 POSIX 路径(如 "services/db_restore.py")
        rules: .dockerignore 规则列表(按出现顺序)

    Returns:
        True 表示该路径被排除
    """
    rel_path = rel_path.lstrip("./")
    ignored = False
    for rule in rules:
        pattern = rule
        is_negation = pattern.startswith("!")
        if is_negation:
            pattern = pattern[1:]
        if _dockerignore_match(rel_path, pattern):
            ignored = not is_negation
    return ignored


def _dockerignore_match(rel_path: str, pattern: str) -> bool:
    """单条 .dockerignore 规则匹配(不含 ! 前缀)。"""
    rel_path = rel_path.lstrip("./")
    pattern = pattern.lstrip("./")
    if not pattern:
        return False

    # 目录规则(尾部 /)
    if pattern.endswith("/"):
        dir_pattern = pattern.rstrip("/")
        if "/" not in dir_pattern:
            # 不含 / 的目录名:匹配任意层级的同名目录
            parts = rel_path.split("/")
            for i, part in enumerate(parts[:-1]):
                if _glob_match(part, dir_pattern):
                    return True
            # 也匹配 rel_path 本身(若 rel_path 是目录路径,无扩展名)
            if _glob_match(rel_path, dir_pattern):
                return True
            return False
        # 含 / 的目录规则:锚定到根
        if _glob_match(rel_path, dir_pattern):
            return True
        if _glob_match(rel_path, dir_pattern + "/*"):
            return True
        # ** 通配的目录规则
        if "**" in dir_pattern:
            regex = _pattern_to_regex(dir_pattern + "/**")
            if re.fullmatch(regex, rel_path):
                return True
        return False

    # 文件规则
    if "/" not in pattern:
        # 不含 / 的 pattern:匹配任意路径段或 basename
        parts = rel_path.split("/")
        for part in parts:
            if _glob_match(part, pattern):
                return True
        return False

    # 含 / 的 pattern:锚定到根
    if "**" in pattern:
        regex = _pattern_to_regex(pattern)
        return re.fullmatch(regex, rel_path) is not None
    return _glob_match(rel_path, pattern)


def _glob_match(text: str, pattern: str) -> bool:
    """fnmatch 包装(不跨 / 匹配,但 fnmatch 默认不跨 /)。"""
    return fnmatch.fnmatchcase(text, pattern)


def _pattern_to_regex(pattern: str) -> str:
    """把含 ** 的 .dockerignore pattern 转换为正则表达式。

    ``*`` → ``[^/]*`` (不跨 /)
    ``**`` → ``.*`` (跨 /)
    ``?`` → ``[^/]``
    其他字符转义
    """
    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                parts.append(".*")
                i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return "".join(parts)


# ════════════════════════════════════════════════════════════════
# 文件清单展开
# ════════════════════════════════════════════════════════════════


def expand_copy_to_files(
    copy_instr: CopyInstruction, repo_root: Path
) -> list[FileEntry]:
    """展开单条 COPY 指令为镜像内文件清单。

    对每个源路径(工作区内):
      - 文件 → 单条目(目标为 dest/<basename>)
      - 目录 → 递归展开所有文件(目标为 dest/<rel>)

    外部 stage 复制(--from=builder)返回空列表(由调用方单独记录)。

    Raises:
        FileNotFoundError: 源路径不存在
        OSError: 读取文件大小失败
        ValueError: 源路径不是文件也不是目录
    """
    if copy_instr.is_from_external_stage:
        return []

    entries: list[FileEntry] = []
    dest_rel = copy_instr.dest_in_app()

    for src in copy_instr.sources:
        src_path = repo_root / src
        if not src_path.exists():
            raise FileNotFoundError(
                f"COPY 源路径不存在: {src_path}"
                f"(Dockerfile 第 {copy_instr.line_number} 行: {copy_instr.raw})"
            )
        if src_path.is_file():
            rel_in_dest = _join_posix(dest_rel, src_path.name)
            try:
                size = src_path.stat().st_size
            except OSError as e:
                raise OSError(f"读取文件大小失败 {src_path}: {e}") from e
            entries.append(FileEntry(
                path=rel_in_dest,
                size=size,
                source_instruction=copy_instr.raw,
                copy_line_number=copy_instr.line_number,
            ))
        elif src_path.is_dir():
            for sub in sorted(src_path.rglob("*")):
                if sub.is_file():
                    rel = sub.relative_to(src_path).as_posix()
                    rel_in_dest = _join_posix(dest_rel, rel)
                    try:
                        size = sub.stat().st_size
                    except OSError as e:
                        raise OSError(f"读取文件大小失败 {sub}: {e}") from e
                    entries.append(FileEntry(
                        path=rel_in_dest,
                        size=size,
                        source_instruction=copy_instr.raw,
                        copy_line_number=copy_instr.line_number,
                    ))
        else:
            raise ValueError(
                f"COPY 源路径不是文件也不是目录: {src_path}"
                f"(Dockerfile 第 {copy_instr.line_number} 行)"
            )
    return entries


def _join_posix(base: str, rel: str) -> str:
    """连接 POSIX 路径(base 为空时返回 rel)。"""
    if not base:
        return rel
    if base.endswith("/"):
        return base + rel
    return base + "/" + rel


def apply_dockerignore(
    entries: list[FileEntry], rules: list[str]
) -> list[FileEntry]:
    """应用 .dockerignore 规则过滤文件清单。"""
    return [e for e in entries if not is_ignored_by_dockerignore(e.path, rules)]


def apply_run_rm(
    entries: list[FileEntry], rm_instructions: list[RunRmInstruction]
) -> list[FileEntry]:
    """应用 Dockerfile RUN rm 指令,移除被物理删除的文件。

    支持路径模式:
      - 精确路径(如 "services/db_restore.py")
      - 目录前缀(如 "tests/" → 移除 "tests/" 下所有文件)
      - glob 通配(如 "*.db" / "*.env.secrets.*")
    """
    if not rm_instructions:
        return entries

    rm_patterns: list[str] = []
    for rm in rm_instructions:
        for p in rm.removed_paths:
            norm = p
            if norm.startswith("/app/"):
                norm = norm[len("/app/"):]
            elif norm.startswith("/"):
                norm = norm.lstrip("/")
            rm_patterns.append(norm)

    def is_removed(entry_path: str) -> bool:
        for pat in rm_patterns:
            if pat.endswith("/"):
                if entry_path.startswith(pat) or entry_path == pat.rstrip("/"):
                    return True
            elif "*" in pat or "?" in pat or "[" in pat:
                if fnmatch.fnmatchcase(entry_path, pat):
                    return True
            else:
                if entry_path == pat:
                    return True
        return False

    return [e for e in entries if not is_removed(e.path)]


def _compute_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ════════════════════════════════════════════════════════════════
# manifest 生成
# ════════════════════════════════════════════════════════════════


def generate_manifest(
    repo_root: Path = REPO_ROOT,
    dockerfile: Path = DOCKERFILE_PATH,
    dockerignore: Path = DOCKERIGNORE_PATH,
    with_sha256: bool = False,
) -> dict:
    """生成 OCI 文件 manifest 字典。

    Args:
        repo_root: 仓库根目录
        dockerfile: Dockerfile 路径
        dockerignore: .dockerignore 路径
        with_sha256: 是否为每个文件计算 sha256

    Returns:
        manifest 字典(可序列化为 JSON)

    Raises:
        FileNotFoundError: Dockerfile / .dockerignore / COPY 源路径不存在
        OSError: 读取失败
        ValueError: Dockerfile 解析错误
    """
    copies, rm_instructions = parse_dockerfile(dockerfile)
    rules = parse_dockerignore(dockerignore)

    entries: list[FileEntry] = []
    external_copies: list[dict] = []

    for c in copies:
        if c.is_from_external_stage:
            external_copies.append({
                "instruction": c.raw,
                "line_number": c.line_number,
                "dest": c.dest,
                "source_stage": c.source_stage(),
            })
            continue
        entries.extend(expand_copy_to_files(c, repo_root))

    # 应用 .dockerignore(第一道防线)
    entries = apply_dockerignore(entries, rules)
    # 应用 RUN rm(第二道防线)
    entries = apply_run_rm(entries, rm_instructions)

    # 去重:同一路径可能被多次 COPY(如 requirements.txt 被复制两次),
    # 后者覆盖前者,保留最后一个
    seen: dict[str, FileEntry] = {}
    for e in entries:
        seen[e.path] = e
    entries = sorted(seen.values(), key=lambda x: x.path)

    # 可选 sha256
    if with_sha256:
        for e in entries:
            file_path = repo_root / e.path
            if file_path.is_file():
                e.sha256 = _compute_sha256(file_path)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": _dt.datetime.now().isoformat(),
        "tool_version": TOOL_VERSION,
        "project": "tgjiema",
        "dockerfile": _relative_to(repo_root, dockerfile),
        "dockerignore": _relative_to(repo_root, dockerignore),
        "file_count": len(entries),
        "files": [e.to_dict() for e in entries],
        "external_copies": external_copies,
        "run_rm_paths": [
            {
                "line_number": rm.line_number,
                "removed": list(rm.removed_paths),
            }
            for rm in rm_instructions
        ],
        "dockerignore_rules": rules,
        "allowed_roots": list(ALLOWED_ROOTS),
    }


def _relative_to(base: Path, path: Path) -> str:
    """返回 path 相对 base 的 POSIX 路径,若不在 base 下则返回绝对路径。"""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


# ════════════════════════════════════════════════════════════════
# --strict 模式验证
# ════════════════════════════════════════════════════════════════


def validate_strict(
    manifest: dict,
    allowed_roots: Iterable[str] = ALLOWED_ROOTS,
) -> None:
    """严格模式验证:每个文件必须在白名单根目录下,否则 raise。

    Args:
        manifest: generate_manifest() 的输出
        allowed_roots: 允许的顶层路径前缀(如 "services/" / "run_all.py")

    Raises:
        StrictModeViolation: 存在不在白名单中的文件
    """
    roots = tuple(allowed_roots)
    violations: list[str] = []
    for entry in manifest.get("files", []):
        path = entry["path"]
        allowed = False
        for root in roots:
            if root.endswith("/"):
                if path.startswith(root) or path == root.rstrip("/"):
                    allowed = True
                    break
            else:
                if path == root:
                    allowed = True
                    break
        if not allowed:
            violations.append(path)
    if violations:
        raise StrictModeViolation(
            "R70 Wave 8 strict 模式:发现不在白名单中的文件:\n  - "
            + "\n  - ".join(violations)
            + "\n允许的顶层路径: " + ", ".join(roots)
        )


# ════════════════════════════════════════════════════════════════
# --validate 模式:与 docker image save 后的 tar 内容对比
# ════════════════════════════════════════════════════════════════


def validate_against_image_tar(tar_path: Path, manifest: dict) -> int:
    """与 docker image save 后的 tar 内容对比。

    CI 流程:
      1. ``docker image save <image> -o image.tar``
      2. ``python scripts/generate_oci_file_manifest.py --validate image.tar``

    本函数解析 tar 中的 /app/ 文件清单,与 manifest 比对:
      - manifest 中有但 tar 中无 → 缺失文件(missing)
      - tar 中有但 manifest 中无 → 未声明文件(extra)
    任一不匹配返回 1(fail-closed)。

    本地无 Docker daemon 时,tar_path 可由 CI artifacts 提供。

    Raises:
        FileNotFoundError: tar_path 不存在(fail-closed,不吞异常)
        RuntimeError: tar 解析失败
    """
    import tarfile

    if not tar_path.exists():
        raise FileNotFoundError(f"镜像 tar 不存在: {tar_path}")

    try:
        with tarfile.open(tar_path, "r") as tf:
            members = tf.getmembers()
    except tarfile.TarError as e:
        raise RuntimeError(f"解析 tar 失败: {e}") from e

    actual_files: set[str] = set()
    for m in members:
        if not m.isfile():
            continue
        name = m.name
        # 标准化:去除前导 ./
        if name.startswith("./"):
            name = name[2:]
        # docker image save 的 tar 可能包含多层 layer(每个 layer 是一个 tar)
        # 此处仅处理"扁平化"后的 tar(已 tar -xf 各层并合并)
        # 提取 /app/ 下的文件
        if name.startswith("app/"):
            actual_files.add(name[len("app/"):])
        elif name.startswith("/app/"):
            actual_files.add(name[len("/app/"):])

    expected = {e["path"] for e in manifest.get("files", [])}

    missing = expected - actual_files
    extra = actual_files - expected

    if missing:
        print(
            f"FAIL: 镜像 tar 中缺失 {len(missing)} 个预期文件:",
            file=sys.stderr,
        )
        for p in sorted(missing):
            print(f"  - {p}", file=sys.stderr)
    if extra:
        print(
            f"FAIL: 镜像 tar 中发现 {len(extra)} 个未声明文件:",
            file=sys.stderr,
        )
        for p in sorted(extra):
            print(f"  - {p}", file=sys.stderr)

    if missing or extra:
        return 1
    print(f"PASS: 镜像 tar 与 manifest 完全一致({len(expected)} 个文件)")
    return 0


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def main(argv: Iterable[str] | None = None) -> int:
    """命令行入口。

    Returns:
        0=成功;1=失败
    """
    parser = argparse.ArgumentParser(
        description="R70 Wave 8: 精确 OCI 文件清单 manifest 生成器",
    )
    parser.add_argument(
        "--dockerfile",
        type=Path,
        default=DOCKERFILE_PATH,
        help=f"Dockerfile 路径(默认: {DOCKERFILE_PATH})",
    )
    parser.add_argument(
        "--dockerignore",
        type=Path,
        default=DOCKERIGNORE_PATH,
        help=f".dockerignore 路径(默认: {DOCKERIGNORE_PATH})",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="输出 manifest JSON 路径(不指定则输出到 stdout)",
    )
    parser.add_argument(
        "--with-sha256",
        action="store_true",
        help="为每个文件计算 sha256(适用于 release artifact)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式:文件不在 ALLOWED_ROOTS 白名单时 raise(fail-closed)",
    )
    parser.add_argument(
        "--validate",
        type=Path,
        default=None,
        help="与 docker image save 后的 tar 内容比对(tar 路径)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(f"[generate_oci_file_manifest] 解析 Dockerfile: {args.dockerfile}")
    print(f"[generate_oci_file_manifest] 解析 .dockerignore: {args.dockerignore}")

    try:
        manifest = generate_manifest(
            repo_root=REPO_ROOT,
            dockerfile=args.dockerfile,
            dockerignore=args.dockerignore,
            with_sha256=args.with_sha256,
        )
    except (FileNotFoundError, OSError, ValueError) as e:
        print(
            f"[generate_oci_file_manifest] FAIL: 生成 manifest 失败: {e}",
            file=sys.stderr,
        )
        return 1

    print(
        f"[generate_oci_file_manifest] manifest 包含 "
        f"{manifest['file_count']} 个文件"
    )

    if args.strict:
        try:
            validate_strict(manifest, ALLOWED_ROOTS)
        except StrictModeViolation as e:
            print(
                f"[generate_oci_file_manifest] FAIL(strict): {e}",
                file=sys.stderr,
            )
            return 1
        print("[generate_oci_file_manifest] strict 验证通过")

    if args.validate is not None:
        try:
            rc = validate_against_image_tar(args.validate, manifest)
        except (FileNotFoundError, RuntimeError) as e:
            print(
                f"[generate_oci_file_manifest] FAIL(validate): {e}",
                file=sys.stderr,
            )
            return 1
        if rc != 0:
            return rc

    output = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.output is not None:
        try:
            args.output.write_text(output, encoding="utf-8")
        except OSError as e:
            print(
                f"[generate_oci_file_manifest] FAIL: 写入 {args.output} 失败: {e}",
                file=sys.stderr,
            )
            return 1
        print(f"[generate_oci_file_manifest] manifest 已写入: {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
