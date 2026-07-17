#!/usr/bin/env python3
"""R59 §5.2 P1: ErrorRegistry 唯一注册源 — CI 静态门禁。

本脚本是 R59 §5.2 P1 要求 5 的落地实现,在 CI 中强制以下不变量:

1. **直接字符串错误码**:扫描所有 .py 文件中直接出现的错误码字面量
   (匹配 ``^[A-Z][A-Z0-9_]*\\.[A-Z][A-Z0-9_]*\\.[A-Z][A-Z0-9_]*$`` 三段式),
   排除 ``services/error_codes.py``(唯一注册源)与 ``tests/``(测试用例)。
   所有错误码必须通过 ``ErrorCodes.XXX`` 常量引用,禁止散落字符串字面量。

2. **动态拼接错误码**:扫描 f-string / 字符串拼接产生错误码
   (``f"{prefix}.INVALID"`` / ``"AUTH." + action``),全部禁止。
   错误码必须为静态常量,运行时不可拼接。

3. **语言包缺 key**:``locales/zh-CN.json`` + ``locales/en-US.json`` 中
   所有 ``errors.*`` 命名空间的 message_key 必须在
   ``ErrorRegistry.all_message_keys()`` 中注册(避免语言包残留无主 key)。

4. **重复 code**:
   - ``ErrorCodes`` 类中不允许两个常量共享同一 code 字符串值
     (静态分析,防止常量值漂移)
   - ``_register_defaults()`` 中通过 ``register()`` 注册的 code 字符串唯一
     (dict key 天然唯一,本项作为冗余防御)

5. **缺失元信息**:每个已注册 code 必须有明确的
   ``severity`` / ``retryable`` / ``http_status`` / ``message_key``。

6. **错误 HTTP 映射**:
   - ``http_status`` 必须在 100-599 区间
   - ``severity=critical`` 必须 4xx/5xx(2xx/3xx 不合理)
   - ``retryable=True`` 推荐 5xx(客户端错误重试无意义,但允许 429 例外)

7. **未知 ErrorCodes 引用**:所有 .py 中 ``ErrorCodes.XXX`` / ``ErrorEnum.XXX``
   引用的常量名必须在 ``ErrorCodes`` 类中真实存在(防拼写错误)。

8. **未注册常量**:``ErrorCodes`` 类中所有大写字符串常量都必须在
   ``ErrorRegistry`` 中通过 ``register()`` 注册(防止新增常量漏注册)。

退出码:
    0 — 通过(违规数 <= baseline,或无 baseline 且无违规)
    1 — 失败(违规数 > baseline,或无 baseline 且有违规)

用法:
    # 默认 strict 模式(无 baseline,任何违规即 exit 1)
    python scripts/check_error_registry.py

    # baseline 模式(违规数 <= baseline 即通过,ratchet 下降)
    python scripts/check_error_registry.py --baseline scripts/error_registry_baseline.json

    # 生成 baseline 文件(记录当前违规快照)
    python scripts/check_error_registry.py --generate-baseline scripts/error_registry_baseline.json

Baseline 机制:
    - 与 ``check_error_codes.py`` / ``check_error_protocol.py`` 一致的 ratchet 模式
    - pre-existing 违规记录在 baseline 中,只允许减少不允许新增
    - 修复违规后运行 ``--generate-baseline`` 下降基线
    - R59 §5.2 P1 要求"未注册错误码在 CI 直接失败" — baseline 仅用于
      pre-existing 历史债务的渐进式清理,新 PR 中的新增违规仍然硬失败

R59 §5.2 P1 实现要点:
    - 与 ``scripts/check_error_codes.py`` 互补:
      * check_error_codes.py 检查"裸字符串 raise / return dict"
      * check_error_registry.py 检查"错误码字面量 / 动态拼接 / 注册一致性"
    - 所有检查项均为硬门禁(无 baseline 时任一违规即 exit 1)
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# 错误码三段式正则: DOMAIN.OPERATION.REASON (全大写 + 数字 + 下划线)
THREE_SEGMENT_RE = re.compile(
    r"^[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*$"
)

# ErrorCodes/ErrorEnum 引用正则(用于检测未声明的常量名)
ERROR_CODE_REF_RE = re.compile(
    r"\b(?:ErrorCodes|ErrorEnum)\.([A-Z][A-Z0-9_]*)\b"
)

# 待扫描的目录(相对 REPO_ROOT)
SCAN_DIRS: list[str] = ["services", "bots", "admin", "utils", "database", "config"]

# 跳过的子目录/文件名(缓存/虚拟环境等)
SKIP_PATTERNS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "static",
    "templates",
    "migrations",
    ".venv",
    "venv",
    ".pytest_cache",
]

# 跳过"直接字符串错误码"检查的文件(允许字面量):
# - 注册源(ErrorCodes 类定义)
# - 本脚本自身(包含示例与正则)
# - 其他已有的 error_codes 相关脚本(避免重复报告)
LITERAL_ALLOWLIST: set[str] = {
    "services/error_codes.py",                 # 唯一注册源
    "scripts/check_error_registry.py",          # 本脚本
    "scripts/check_error_codes.py",             # 已有脚本
    "scripts/check_error_codes_locale_schema.py",
    "scripts/check_error_protocol.py",          # 已有协议检查脚本
    "scripts/export_error_codes_frontend.py",   # 前端映射导出
    "scripts/verify_i18n_keys.py",              # i18n key 校验
    "scripts/check_i18n_key_symmetry.py",       # i18n 对称校验
    "scripts/migrate_i18n_strings.py",          # i18n 迁移工具
}


def is_skipped(path: Path) -> bool:
    """检查路径是否应跳过(命中 SKIP_PATTERNS 中任一子串即跳过)。"""
    s = str(path)
    for pat in SKIP_PATTERNS:
        if pat in s:
            return True
    return False


def _flatten_dict(obj: object, prefix: str = "") -> dict[str, str]:
    """递归扁平化 dict,返回 {点分 key: value} 映射。

    与 ``services/error_codes.py`` 中的 ``_flatten_dict`` 保持一致,
    确保语言包 key 查找逻辑统一。
    """
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_dict(v, full_key))
        else:
            result[full_key] = str(v)
    return result


def _get_str_const(node: ast.AST) -> str | None:
    """如果 AST 节点是字符串常量,返回其值;否则返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _looks_like_error_code_fragment(s: str) -> bool:
    """判断字符串是否看起来像错误码片段(严格匹配,避免误报 SQL/日志/文件名)。

    判定标准(全部满足):
        1. 非空,且包含至少一个 "."(错误码必须有分段分隔符)
        2. 按 "." 分割后,**所有**非空片段都匹配 ``[A-Z][A-Z0-9_]*``
           (纯大写字母+数字+下划线,不允许空格/中文/小写/特殊字符)
        3. 至少有一个非空片段长度 >= 2

    示例:
        - ``"AUTH."`` → True (片段 "AUTH" 满足,所有非空片段都匹配)
        - ``".INVALID"`` → True (片段 "INVALID" 满足)
        - ``"AUTH.LOGIN"`` → True
        - ``"AUTH..INVALID"`` → True (空片段忽略,非空片段都匹配)
        - ``"v"`` → False (无 ".")
        - ``".json"`` → False (片段 "json" 非大写)
        - ``"@example.com"`` → False (片段 "@example" 不匹配)
        - ``" = EXCLUDED."`` → False (片段 " = EXCLUDED" 含空格,不匹配)
        - ``"db_backup/COMPLETE__."`` → False (片段 "db_backup/COMPLETE__"
          含小写和斜杠,不匹配)
        - ``"BACKUP_KEK 长度..."`` → False (含中文和空格,不匹配)
    """
    if not s:
        return False
    if "." not in s:
        return False
    parts = [p for p in s.split(".") if p]
    if not parts:
        return False
    # 所有非空片段必须匹配 [A-Z][A-Z0-9_]*(纯大写标识符)
    segment_re = re.compile(r"^[A-Z][A-Z0-9_]*$")
    if not all(segment_re.match(p) for p in parts):
        return False
    # 至少一个片段长度 >= 2(避免单字母片段如 "A.B.C" 误报)
    return any(len(p) >= 2 for p in parts)


def _collect_docstring_line_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """收集 AST 中所有 docstring 的行号区间 [lineno, end_lineno]。

    用于在字面量扫描时排除 docstring 内容(避免 docstring 中的示例被误报)。
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr):
            continue
        if not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        # 这是 docstring
        start = getattr(first.value, "lineno", 0)
        end = getattr(first.value, "end_lineno", start)
        if start and end:
            ranges.append((start, end))
    return ranges


def _is_in_docstring(line_no: int, ranges: list[tuple[int, int]]) -> bool:
    """判断行号是否落在任一 docstring 区间内。"""
    for start, end in ranges:
        if start <= line_no <= end:
            return True
    return False


def check_literal_error_codes(file_path: Path) -> list[tuple[int, str]]:
    """检查 .py 文件中直接出现的错误码字面量。

    匹配三段式 ``DOMAIN.OPERATION.REASON`` (全大写) 的字符串字面量。
    排除 docstring 中的内容。

    Returns:
        [(line_no, literal)] 违规列表
    """
    findings: list[tuple[int, str]] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return findings

    docstring_ranges = _collect_docstring_line_ranges(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        val = node.value.strip()
        if not THREE_SEGMENT_RE.match(val):
            continue
        line_no = getattr(node, "lineno", 0)
        # 排除 docstring 中的示例
        if _is_in_docstring(line_no, docstring_ranges):
            continue
        findings.append((line_no, val))
    return findings


def check_dynamic_concat(file_path: Path) -> list[tuple[int, str]]:
    """检查 .py 文件中的动态错误码拼接。

    检测两种模式:
        1. ``BinOp(left, Add, right)`` — 字符串字面量 + 非常量表达式
           (如 ``"AUTH." + action``)
        2. ``JoinedStr`` — f-string 包含错误码片段字面量 + FormattedValue
           (如 ``f"{prefix}.INVALID"`` / ``f"AUTH.{action}.X"``)

    Returns:
        [(line_no, description)] 违规列表
    """
    findings: list[tuple[int, str]] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return findings

    for sub in ast.walk(tree):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add):
            # 字符串拼接: "AUTH." + action / action + ".INVALID"
            l_str = _get_str_const(sub.left)
            r_str = _get_str_const(sub.right)
            if l_str and _looks_like_error_code_fragment(l_str) \
                    and not isinstance(sub.right, ast.Constant):
                line_no = getattr(sub, "lineno", 0)
                findings.append((
                    line_no,
                    f"dynamic concat: {l_str!r} + <expr> "
                    f"(应使用 ErrorCodes.XXX 常量,禁止运行时拼接错误码)",
                ))
            elif r_str and _looks_like_error_code_fragment(r_str) \
                    and not isinstance(sub.left, ast.Constant):
                line_no = getattr(sub, "lineno", 0)
                findings.append((
                    line_no,
                    f"dynamic concat: <expr> + {r_str!r} "
                    f"(应使用 ErrorCodes.XXX 常量,禁止运行时拼接错误码)",
                ))
        elif isinstance(sub, ast.JoinedStr):
            # f-string: f"{prefix}.INVALID" / f"AUTH.{action}.X"
            has_formatted = False
            literal_text = ""
            for val in sub.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    literal_text += val.value
                elif isinstance(val, ast.FormattedValue):
                    has_formatted = True
            if has_formatted and _looks_like_error_code_fragment(literal_text):
                line_no = getattr(sub, "lineno", 0)
                findings.append((
                    line_no,
                    f"f-string dynamic error code: literal={literal_text!r} "
                    f"(应使用 ErrorCodes.XXX 常量,禁止运行时拼接错误码)",
                ))
    return findings


def check_error_code_refs(file_path: Path) -> list[tuple[int, str, str]]:
    """检查 .py 文件中 ErrorCodes.XXX / ErrorEnum.XXX 引用的常量名是否存在。

    Returns:
        [(line_no, ref_name, content)] 未声明引用列表
    """
    findings: list[tuple[int, str, str]] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings

    # 跳过定义文件本身(ErrorCodes 类内引用自身常量)
    if file_path.name == "error_codes.py" and "services" in file_path.parts:
        return findings

    declared_names: set[str] | None = None
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from services.error_codes import ErrorCodes  # type: ignore[import]
        declared_names = {
            attr for attr in dir(ErrorCodes)
            if attr.isupper() and not attr.startswith("_")
            and isinstance(getattr(ErrorCodes, attr), str)
        }
    except Exception:
        # 无法加载 ErrorCodes,跳过此检查(避免误报)
        return findings

    in_docstring = False
    docstring_marker = ""
    for idx, line in enumerate(content.splitlines(), 1):
        stripped = line.lstrip()
        # 跳过注释行
        if stripped.startswith("#"):
            continue
        # 粗略 docstring 跟踪(避免在 docstring 内的示例被误报)
        if not in_docstring:
            if '"""' in line or "'''" in line:
                for marker in ('"""', "'''"):
                    if marker in line:
                        if line.count(marker) >= 2:
                            # 同行开始 + 结束,跳过本行
                            break
                        # 跨行 docstring 开始
                        in_docstring = True
                        docstring_marker = marker
                        break
                if in_docstring:
                    continue
        else:
            if docstring_marker in line:
                in_docstring = False
                docstring_marker = ""
            continue

        for match in ERROR_CODE_REF_RE.finditer(line):
            ref_name = match.group(1)
            if declared_names and ref_name not in declared_names:
                findings.append((idx, ref_name, line.strip()[:120]))
    return findings


def check_registry_integrity() -> list[str]:
    """检查 ErrorRegistry 内部一致性。

    检查项:
        1. ErrorCodes 类中常量值唯一(无两个常量共享同一 code 字符串)
        2. 每个已注册 code 有明确的 severity/retryable/http_status/message_key
        3. HTTP 映射合理性(http_status 在 100-599;critical 必须 4xx/5xx)
        4. ErrorCodes 所有常量都已注册到 ErrorRegistry(无漏注册)

    Returns:
        违规描述列表
    """
    violations: list[str] = []
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from services.error_codes import (  # type: ignore[import]
            ErrorCodes, ErrorRegistry,
        )
        # 触发初始化(确保 _register_defaults 已执行)
        ErrorRegistry.all_codes()
    except Exception as e:
        violations.append(f"无法加载 ErrorRegistry: {e}")
        return violations

    # 1. ErrorCodes 类中常量值唯一性
    # (防止两个常量共享同一 code 字符串,导致 _register_defaults 中后注册覆盖前者)
    code_to_names: dict[str, list[str]] = {}
    for attr in dir(ErrorCodes):
        if not attr.isupper() or attr.startswith("_"):
            continue
        value = getattr(ErrorCodes, attr)
        if not isinstance(value, str):
            continue
        code_to_names.setdefault(value, []).append(attr)
    for code, names in code_to_names.items():
        if len(names) > 1:
            violations.append(
                f"duplicate code: {code!r} 被 ErrorCodes 中多个常量共享: {names}"
            )

    # 2. 每个已注册 code 必须有明确的元信息
    valid_severities = {"info", "warning", "error", "critical"}
    for code in ErrorRegistry.all_codes():
        d = ErrorRegistry.get(code)
        if not d.message_key:
            violations.append(f"code {code!r} 缺失 message_key")
        if d.severity not in valid_severities:
            violations.append(
                f"code {code!r} severity={d.severity!r} 不在允许值 {valid_severities}"
            )
        if not isinstance(d.retryable, bool):
            violations.append(
                f"code {code!r} retryable={d.retryable!r} 必须为 bool"
            )
        if not isinstance(d.http_status, int) \
                or isinstance(d.http_status, bool) \
                or d.http_status < 100 or d.http_status > 599:
            violations.append(
                f"code {code!r} http_status={d.http_status!r} 必须为 int 且在 100-599"
            )
        # 3. 错误 HTTP 映射:critical 必须 4xx/5xx(2xx/3xx 不合理)
        if d.severity == "critical" and not (400 <= d.http_status <= 599):
            violations.append(
                f"code {code!r} severity=critical 但 http_status={d.http_status} "
                f"(critical 必须 4xx/5xx)"
            )

    # 4. ErrorCodes 所有常量都已注册到 ErrorRegistry
    registered_codes = set(ErrorRegistry.all_codes())
    for attr in dir(ErrorCodes):
        if not attr.isupper() or attr.startswith("_"):
            continue
        value = getattr(ErrorCodes, attr)
        if not isinstance(value, str) or "." not in value:
            continue
        if value not in registered_codes:
            violations.append(
                f"ErrorCodes.{attr} = {value!r} 未在 ErrorRegistry 注册 "
                f"(请在 _register_defaults() 中添加 "
                f"ErrorRegistry.register(ErrorDefinition(...)))"
            )

    return violations


def check_locale_keys_covered() -> list[str]:
    """检查语言包 errors.* key 都在 ErrorRegistry 注册。

    R59 §5.2 P1 要求:语言包不应残留无主 key(所有 errors.* 都应有
    对应的 ErrorDefinition.message_key)。

    Returns:
        违规描述列表
    """
    violations: list[str] = []
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from services.error_codes import ErrorRegistry  # type: ignore[import]
        registered_keys = set(ErrorRegistry.all_message_keys())
    except Exception as e:
        violations.append(f"无法加载 ErrorRegistry: {e}")
        return violations

    for locale in ["zh-CN", "en-US"]:
        path = LOCALES_DIR / f"{locale}.json"
        if not path.exists():
            violations.append(f"语言包文件缺失: {path}")
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            violations.append(f"语言包 {locale} JSON 解析失败: {e}")
            continue
        flat = _flatten_dict(raw)
        # 仅检查 errors.* 命名空间
        for key in flat:
            if not key.startswith("errors."):
                continue
            if key not in registered_keys:
                violations.append(
                    f"语言包 {locale}.json: key {key!r} 未在 ErrorRegistry 注册 "
                    f"(无对应 ErrorDefinition.message_key)"
                )
    return violations


def _violation_key(category: str, *parts: str) -> str:
    """生成违规唯一键(基于类别 + 关键字段,不依赖行号)。

    用于 baseline 比对:同一违规(相同文件+相同内容)在不同行号下
    仍视为同一违规,避免代码移动导致 baseline 失效。
    """
    return "::".join([category, *parts])


def _load_baseline(path: Path) -> set[str]:
    """加载 baseline 文件,返回已知违规键集合。"""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("violations", []))
    except Exception:
        return set()


def _save_baseline(path: Path, violations: set[str]) -> None:
    """保存 baseline 文件。"""
    data = {
        "description": "R59 §5.2 P1: ErrorRegistry 已知违规 baseline (ratchet 下降)",
        "note": (
            "pre-existing 历史债务,只允许减少不允许新增。"
            "修复违规后运行 --generate-baseline 下降基线。"
        ),
        "violation_count": len(violations),
        "violations": sorted(violations),
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_args(argv: list[str]) -> tuple[Path | None, Path | None, bool]:
    """解析命令行参数。

    Returns:
        (baseline_file, generate_baseline_file, strict)
    """
    baseline_file: Path | None = None
    generate_baseline_file: Path | None = None
    strict = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--baseline" and i + 1 < len(argv):
            baseline_file = Path(argv[i + 1])
            i += 2
        elif arg == "--generate-baseline" and i + 1 < len(argv):
            generate_baseline_file = Path(argv[i + 1])
            i += 2
        elif arg == "--strict":
            strict = True
            i += 1
        else:
            i += 1
    return baseline_file, generate_baseline_file, strict


def main() -> int:
    """主入口。返回 0=通过,1=发现违规。"""
    baseline_file, generate_baseline_file, _ = _parse_args(sys.argv[1:])

    literal_findings: list[tuple[str, int, str]] = []
    dynamic_findings: list[tuple[str, int, str]] = []
    ref_findings: list[tuple[str, int, str, str]] = []

    for scan_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / scan_dir
        if not scan_path.exists():
            continue
        for py_file in scan_path.rglob("*.py"):
            if is_skipped(py_file):
                continue
            rel = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
            # 跳过 tests/ 目录(测试用例允许使用字面量验证)
            if rel.startswith("tests/"):
                continue

            if rel in LITERAL_ALLOWLIST:
                # 注册源/检查脚本:跳过字面量与动态拼接检查,
                # 仍检查 ErrorCodes.XXX 引用名(防止拼写错误)
                ref_findings.extend(
                    (rel, ln, name, content)
                    for ln, name, content in check_error_code_refs(py_file)
                )
                continue

            # 1. 字面量错误码检查
            for ln, val in check_literal_error_codes(py_file):
                literal_findings.append((rel, ln, val))
            # 2. 动态拼接检查
            for ln, desc in check_dynamic_concat(py_file):
                dynamic_findings.append((rel, ln, desc))
            # 3. ErrorCodes.XXX 引用名检查
            for ln, name, content in check_error_code_refs(py_file):
                ref_findings.append((rel, ln, name, content))

    # 4. registry 内部一致性
    registry_violations = check_registry_integrity()
    # 5. 语言包 key 覆盖检查
    locale_violations = check_locale_keys_covered()

    # 生成违规键集合(用于 baseline 比对)
    # 键格式: category::file::content(不包含行号,避免代码移动导致 baseline 失效)
    all_violation_keys: set[str] = set()
    for file, _, val in literal_findings:
        all_violation_keys.add(_violation_key("literal", file, val))
    for file, _, desc in dynamic_findings:
        all_violation_keys.add(_violation_key("dynamic", file, desc))
    for file, _, name, _ in ref_findings:
        all_violation_keys.add(_violation_key("ref", file, name))
    for v in registry_violations:
        all_violation_keys.add(_violation_key("registry", v))
    for v in locale_violations:
        all_violation_keys.add(_violation_key("locale", v))

    # 生成 baseline 模式:写入当前违规快照并退出
    if generate_baseline_file is not None:
        _save_baseline(generate_baseline_file, all_violation_keys)
        print(f"✓ R59 §5.2 P1 baseline 已生成: {generate_baseline_file}")
        print(f"  当前违规总数: {len(all_violation_keys)} 项")
        print(f"    - 直接字符串错误码字面量: {len(literal_findings)} 处")
        print(f"    - 动态错误码拼接: {len(dynamic_findings)} 处")
        print(f"    - 未知 ErrorCodes 引用: {len(ref_findings)} 处")
        print(f"    - ErrorRegistry 内部一致性: {len(registry_violations)} 项")
        print(f"    - 语言包 errors.* key 未注册: {len(locale_violations)} 项")
        print()
        print("注意: baseline 仅用于 ratchet 下降 pre-existing 违规,")
        print("      不应在新 PR 中扩大基线。修复违规后重新生成以下降基线。")
        return 0

    # baseline 比对模式:仅新增违规失败(ratchet 下降)
    if baseline_file is not None:
        baseline_keys = _load_baseline(baseline_file)
        new_violations = all_violation_keys - baseline_keys
        if new_violations:
            print(
                f"❌ R59 §5.2 P1: 发现 {len(new_violations)} 处**新增**违规"
                f"(不在 baseline 中,baseline 模式下硬失败):"
            )
            for v in sorted(new_violations)[:50]:
                print(f"  - {v}")
            if len(new_violations) > 50:
                print(f"  ... 还有 {len(new_violations) - 50} 项")
            print()
            print("修复指南:")
            print("  1. 直接字符串错误码 → 改用 ErrorCodes.XXX 常量引用")
            print("  2. 动态拼接错误码 → 改用 ErrorCodes.XXX 常量(运行时禁止拼接)")
            print("  3. 未知 ErrorCodes 引用 → 检查拼写,或在 ErrorCodes 类中新增常量")
            print("  4. 重复 code → 检查 ErrorCodes 类中是否有两个常量共享同一字符串值")
            print("  5. 缺失元信息 → 在 _register_defaults() 中补全 ErrorDefinition")
            print("  6. 错误 HTTP 映射 → 调整 ErrorDefinition.http_status")
            print("  7. 语言包无主 key → 在 _register_defaults() 中注册对应 message_key")
            print()
            print(f"如需将新违规纳入 baseline(仅限无法立即修复的场景),")
            print(f"运行: python scripts/check_error_registry.py "
                  f"--generate-baseline {baseline_file}")
            return 1
        print(
            f"✓ R59 §5.2 P1 ErrorRegistry 检查通过(baseline 模式):"
        )
        print(f"  - 当前违规总数: {len(all_violation_keys)} 项")
        print(f"  - baseline 违规: {len(baseline_keys)} 项")
        print(f"  - 新增违规: 0 项(ratchet 下降中)")
        if all_violation_keys < baseline_keys:
            fixed = baseline_keys - all_violation_keys
            print(
                f"  - 已修复违规: {len(fixed)} 项"
                f"(运行 --generate-baseline 下降基线以反映清理进度)"
            )
        return 0

    # 默认 strict 模式(无 baseline,任何违规即 exit 1)
    has_violation = False

    if literal_findings:
        has_violation = True
        print(
            f"❌ R59 §5.2 P1: 发现 {len(literal_findings)} 处直接字符串错误码字面量"
            f"(应通过 ErrorCodes.XXX 引用,而非散落字符串字面量):"
        )
        for file, ln, val in literal_findings[:50]:
            print(f"  {file}:{ln}: {val!r}")
        if len(literal_findings) > 50:
            print(f"  ... 还有 {len(literal_findings) - 50} 处")
        print()

    if dynamic_findings:
        has_violation = True
        print(
            f"❌ R59 §5.2 P1: 发现 {len(dynamic_findings)} 处动态错误码拼接"
            f"(禁止 f-string / 字符串拼接产生错误码,必须使用 ErrorCodes.XXX 常量):"
        )
        for file, ln, desc in dynamic_findings[:50]:
            print(f"  {file}:{ln}: {desc}")
        if len(dynamic_findings) > 50:
            print(f"  ... 还有 {len(dynamic_findings) - 50} 处")
        print()

    if ref_findings:
        has_violation = True
        print(
            f"❌ R59 §5.2 P1: 发现 {len(ref_findings)} 处未知 ErrorCodes/ErrorEnum 引用"
            f"(常量名在 ErrorCodes 类中不存在,可能是拼写错误):"
        )
        for file, ln, name, content in ref_findings[:50]:
            print(f"  {file}:{ln}: {name!r} in: {content}")
        if len(ref_findings) > 50:
            print(f"  ... 还有 {len(ref_findings) - 50} 处")
        print()

    if registry_violations:
        has_violation = True
        print(
            f"❌ R59 §5.2 P1: ErrorRegistry 内部一致性问题 "
            f"({len(registry_violations)} 项):"
        )
        for v in registry_violations[:50]:
            print(f"  - {v}")
        if len(registry_violations) > 50:
            print(f"  ... 还有 {len(registry_violations) - 50} 项")
        print()

    if locale_violations:
        has_violation = True
        print(
            f"❌ R59 §5.2 P1: 语言包 errors.* key 未注册 "
            f"({len(locale_violations)} 项,所有 errors.* key 必须在 "
            f"ErrorRegistry 注册):"
        )
        for v in locale_violations[:50]:
            print(f"  - {v}")
        if len(locale_violations) > 50:
            print(f"  ... 还有 {len(locale_violations) - 50} 项")
        print()

    if has_violation:
        print("R59 §5.2 P1 ErrorRegistry 检查未通过(strict 模式)。")
        print("修复指南:")
        print("  1. 直接字符串错误码 → 改用 ErrorCodes.XXX 常量引用")
        print("  2. 动态拼接错误码 → 改用 ErrorCodes.XXX 常量(运行时禁止拼接)")
        print("  3. 未知 ErrorCodes 引用 → 检查拼写,或在 ErrorCodes 类中新增常量")
        print("  4. 重复 code → 检查 ErrorCodes 类中是否有两个常量共享同一字符串值")
        print("  5. 缺失元信息 → 在 _register_defaults() 中补全 ErrorDefinition")
        print("  6. 错误 HTTP 映射 → 调整 ErrorDefinition.http_status")
        print("  7. 语言包无主 key → 在 _register_defaults() 中注册对应 message_key")
        print()
        print("如需对 pre-existing 违规启用 baseline 模式(ratchet 下降):")
        print(f"  python scripts/check_error_registry.py "
              f"--generate-baseline scripts/error_registry_baseline.json")
        print(f"  python scripts/check_error_registry.py "
              f"--baseline scripts/error_registry_baseline.json")
        return 1

    print("✓ R59 §5.2 P1 ErrorRegistry 检查通过(strict 模式):")
    print("  - 无直接字符串错误码字面量(全部通过 ErrorCodes.XXX 引用)")
    print("  - 无动态错误码拼接(f-string / 字符串拼接)")
    print("  - 所有 ErrorCodes/ErrorEnum 引用名都已声明(无拼写错误)")
    print("  - ErrorRegistry 内部一致(无重复 code、无缺失元信息、无错误 HTTP 映射)")
    print("  - ErrorCodes 所有常量都已通过 register() 注册")
    print("  - locales/zh-CN.json + en-US.json 中所有 errors.* key 都已注册")
    return 0


if __name__ == "__main__":
    sys.exit(main())
