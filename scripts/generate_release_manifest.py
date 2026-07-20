#!/usr/bin/env python3
"""R64 P0-02: 独立 release artifact manifest 生成器(避免自引用循环)。

终审报告 P0-02 根治逻辑:
    "不要把会改变 tree 的 manifest 直接绑定到包含自身的 tree,避免自引用循环。
     生成独立 release artifact manifest,绑定 source commit、source tree、
     migration file digest 集合和 image digest。"

旧实现问题(R64 P0-02):
  - ``database/migrations/migration-manifest.json`` 包含在提交树中,
    其 ``release_commit`` / ``tree_sha`` 字段指向包含自身的 tree — 自引用循环。
  - 任何 commit 都使旧 manifest 的 release_commit/tree_sha 失效,迫使每次
    commit 都必须重生 manifest,而重生动作本身又改变 tree,造成 "无稳态" 困境。

本脚本设计要点(R64 P0-02):
  1. **不提交到 git** — 本脚本是 CI 产物,输出到 ``release-artifacts/`` 目录,
     在 release-gates.yml sign-image job 中于 docker build 之后、sign 之前运行。
  2. **绑定 source commit + source tree + migration digest 集合 + image digest** —
     每个 release artifact manifest 都是不可变 release 的 canonical 描述。
  3. **不可变** — 同一 commit + image_digest 总是生成完全相同的 JSON(字段顺序固定、
     时间戳取 commit 时间而非 wall clock,以便可复现)。
  4. **由 CI 签名** — release-manifest.json 通过 cosign sign-blob --keyless 生成
     detached signature,部署环境从签名 attestation 或镜像 label 注入
     ``RELEASE_SOURCE_COMMIT`` / ``RELEASE_SOURCE_TREE`` 环境变量,
     运行时验证 manifest 与部署环境一致。

输出格式(canonical release manifest v3):
    {
      "version": 3,
      "type": "release_artifact",
      "source_commit": "<git rev-parse HEAD>",
      "source_tree": "<git rev-parse HEAD^{tree}>",
      "source_tree_sha": "<git rev-parse HEAD^{tree}> (R66 P1-10 别名,供 verify_attestation_semantics.py 使用)",
      "source_repository": "<owner/repo,如 maxiuquan/tgjiema>",
      "image_digest": "sha256:...",
      "image_name": "ghcr.io/maxiuquan/tgjiema",
      "image_ref": "ghcr.io/maxiuquan/tgjiema (R66 P1-10 别名,供 verify_attestation_semantics.py 使用)",
      "migrations": [{"version": "...", "sha256": "..."}, ...],
      "migration_manifest_digest": "<sha256 of database/migrations/migration-manifest.json>",
      "generated_at": "<ISO8601 UTC>",
      "tool_version": "R64-P0-02"
    }

退出码:
  0 — 成功
  1 — 失败(git 不可用 / migration-manifest.json 不存在 / image_digest 缺失 / 写入失败)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_MANIFEST_PATH = REPO_ROOT / "database" / "migrations" / "migration-manifest.json"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

# R64 P0-02: 默认 image_name 与 release-gates.yml 中 docker-build 输出一致
DEFAULT_IMAGE_NAME = "ghcr.io/maxiuquan/tgjiema"

# R64 P0-02: tool_version 标识 release manifest 生成器版本
TOOL_VERSION = "R64-P0-02"

# R64 P0-02: release manifest schema 版本(独立于 migration-manifest.json 的 version 字段)
RELEASE_MANIFEST_VERSION = 3


def _git_rev_parse(rev: str) -> str:
    """执行 git rev-parse,失败则 raise。"""
    result = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {rev} 失败 (exit={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    sha = result.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
        raise RuntimeError(f"git rev-parse {rev} 返回非法 SHA: {sha!r}")
    return sha


def _git_commit_iso8601(commit_sha: str) -> str:
    """获取指定 commit 的提交时间(ISO 8601 UTC,可复现)。

    使用 commit 时间而非 wall clock,保证同一 commit 总是生成相同的 generated_at,
    满足 R64 P0-02 "不可变 + 可复现" 要求。
    """
    result = subprocess.run(
        ["git", "show", "-s", "--format=%cI", commit_sha],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        # 退化到当前 UTC 时间(非可复现,但保证不阻断 CI)
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso = result.stdout.strip()
    if not iso:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # %cI 已是 ISO 8601 严格格式(如 2026-07-18T12:34:56+08:00),
    # 转换为 UTC "Z" 后缀以保持规范
    try:
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso


def _file_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_image_digest(image_digest: str) -> str:
    """规范化 image_digest,确保以 ``sha256:`` 前缀开始。"""
    digest = image_digest.strip()
    if not digest:
        raise RuntimeError("image_digest 为空 — 必须提供 docker build 输出的 digest")
    if not digest.startswith("sha256:"):
        # 容错:CI 输出可能是纯 hex,补前缀
        if len(digest) == 64 and all(
            c in "0123456789abcdef" for c in digest.lower()
        ):
            digest = f"sha256:{digest}"
        else:
            raise RuntimeError(
                f"image_digest 格式非法(应为 sha256:<hex> 或 64 字符 hex): {digest!r}"
            )
    return digest


def _load_migration_manifest_entries(manifest_path: Path | None = None) -> list[dict]:
    """加载 migration-manifest.json,返回 [{version, sha256}, ...] 列表。

    R64 P0-02: release manifest 的 migrations 字段只保留 version + sha256
    (不复制 schema_fingerprint_* / predecessor / *_note 等运行时不需要的字段,
    保持 release manifest 精简且稳定)。

    Args:
        manifest_path: migration-manifest.json 路径。None 时使用模块级
            ``MIGRATION_MANIFEST_PATH`` 默认值。
    """
    path = manifest_path if manifest_path is not None else MIGRATION_MANIFEST_PATH
    if not path.exists():
        raise RuntimeError(
            f"migration-manifest.json 不存在: {path} "
            f"(必须先运行 scripts/generate_migration_manifest.py 重生)"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        raise RuntimeError("migration-manifest.json 'migrations' 字段不是 list")
    entries: list[dict] = []
    for entry in migrations:
        version = str(entry.get("version", ""))
        sha256 = str(entry.get("sha256", ""))
        if not version or not sha256:
            raise RuntimeError(
                f"migration-manifest.json 条目缺少 version/sha256: {entry!r}"
            )
        entries.append({"version": version, "sha256": sha256})
    if not entries:
        raise RuntimeError("migration-manifest.json 'migrations' 为空")
    return entries


def generate_release_manifest(
    image_digest: str,
    image_name: str = DEFAULT_IMAGE_NAME,
    output_path: Path | None = None,
    verbose: bool = True,
    source_repository: str | None = None,
    source_commit: str | None = None,
    source_tree_sha: str | None = None,
    migration_manifest_path: Path | None = None,
) -> dict:
    """生成 canonical release artifact manifest。

    Args:
        image_digest: docker build 输出的镜像 digest(如 ``sha256:abc...``)
        image_name: 镜像仓库地址(默认 ``ghcr.io/maxiuquan/tgjiema``)
        output_path: 输出文件路径(None 表示不写文件,仅返回 dict)
        verbose: 是否打印详细日志
        source_repository: GitHub 仓库 ``owner/repo``(如 ``maxiuquan/tgjiema``)。
            None 时取 ``GITHUB_REPOSITORY`` 环境变量,仍未设置则为空串。
            供 verify_attestation_semantics.py 校验 configSource.uri 使用。
        source_commit: git commit SHA(HEAD)。None 时优先取 ``GITHUB_SHA``
            环境变量,仍未设置则 ``git rev-parse HEAD``。
        source_tree_sha: git tree SHA(HEAD^{tree})。None 时取
            ``git rev-parse HEAD^{tree}``。同时写入 ``source_tree`` 与
            ``source_tree_sha`` 两个字段(后者供 verify_attestation_semantics.py 使用)。
        migration_manifest_path: migration-manifest.json 路径。None 时使用
            默认 ``database/migrations/migration-manifest.json``(相对 REPO_ROOT)。

    Returns:
        canonical release manifest dict

    Raises:
        RuntimeError: git 不可用 / migration-manifest.json 缺失 / image_digest 非法
    """
    # 1. 获取当前 source commit + source tree(R66 P1-10: 兼容 GITHUB_SHA 环境变量)
    if source_commit:
        pass  # 显式 CLI 参数优先
    else:
        github_sha = os.environ.get("GITHUB_SHA", "").strip()
        if github_sha:
            source_commit = github_sha
        else:
            source_commit = _git_rev_parse("HEAD")
    if source_tree_sha:
        pass  # 显式 CLI 参数优先
    else:
        source_tree_sha = _git_rev_parse("HEAD^{tree}")
    # source_tree 字段保持与 source_tree_sha 一致(向后兼容)
    source_tree = source_tree_sha
    if verbose:
        print(f"[release_manifest] source_commit = {source_commit}")
        print(f"[release_manifest] source_tree   = {source_tree}")

    # 1b. 解析 source_repository(R66 P1-10: GITHUB_REPOSITORY 环境变量回退)
    if source_repository is None:
        source_repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if verbose:
        print(f"[release_manifest] source_repository = {source_repository or '(unset)'}")

    # 1c. 解析 migration_manifest_path(允许 CLI 覆盖默认路径)
    if migration_manifest_path is None:
        migration_manifest_path_resolved = MIGRATION_MANIFEST_PATH
    else:
        migration_manifest_path_resolved = Path(migration_manifest_path)
        if not migration_manifest_path_resolved.is_absolute():
            migration_manifest_path_resolved = REPO_ROOT / migration_manifest_path_resolved

    # 2. 规范化 image_digest
    image_digest_norm = _normalize_image_digest(image_digest)
    if verbose:
        print(f"[release_manifest] image_digest  = {image_digest_norm}")
        print(f"[release_manifest] image_name    = {image_name}")

    # 3. 加载 migration-manifest.json 的 migration 集合
    migrations = _load_migration_manifest_entries(migration_manifest_path_resolved)
    if verbose:
        print(f"[release_manifest] migrations    = {len(migrations)} 个")

    # 4. 计算 migration-manifest.json 的 digest(用于绑定原始 trust anchor)
    migration_manifest_digest = _file_sha256(migration_manifest_path_resolved)
    if verbose:
        print(
            f"[release_manifest] migration_manifest_digest = "
            f"{migration_manifest_digest[:12]}..."
        )

    # 5. 生成时间(commit 时间,保证可复现)
    generated_at = _git_commit_iso8601(source_commit)
    if verbose:
        print(f"[release_manifest] generated_at  = {generated_at}")

    # 6. 构造 canonical manifest(字段顺序固定,便于 cosign 签名可复现)
    #    R66 P1-10: 新增 image_ref / source_repository / source_tree_sha 字段,
    #    供 verify_attestation_semantics.py 语义验证使用(不删除任何既有字段)。
    manifest = {
        "version": RELEASE_MANIFEST_VERSION,
        "type": "release_artifact",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_tree_sha": source_tree_sha,
        "source_repository": source_repository,
        "image_digest": image_digest_norm,
        "image_name": image_name,
        "image_ref": image_name,
        "migrations": migrations,
        "migration_manifest_digest": migration_manifest_digest,
        "generated_at": generated_at,
        "tool_version": TOOL_VERSION,
    }

    # 7. 写入文件(2-space 缩进,ensure_ascii=False,key 顺序保持插入顺序)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if verbose:
            print(f"[release_manifest] release manifest 已写入: {output_path}")
            print("[release_manifest] DONE")
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R64 P0-02: 生成 canonical release artifact manifest"
    )
    parser.add_argument(
        "--image-digest",
        required=True,
        help="docker build 输出的镜像 digest(如 sha256:abc... 或 64 字符 hex)",
    )
    parser.add_argument(
        "--image-name",
        default=DEFAULT_IMAGE_NAME,
        help=f"镜像仓库名称(默认 {DEFAULT_IMAGE_NAME})",
    )
    parser.add_argument(
        "--output",
        default="release-artifacts/release-manifest.json",
        help="输出路径(默认 release-artifacts/release-manifest.json)",
    )
    parser.add_argument(
        "--source-repository",
        default=None,
        help=(
            "GitHub 仓库 owner/repo(如 maxiuquan/tgjiema)。"
            "未指定时取 GITHUB_REPOSITORY 环境变量。"
            "R66 P1-10: 写入 release-manifest.json.source_repository 字段,"
            "供 verify_attestation_semantics.py 校验 configSource.uri 使用。"
        ),
    )
    parser.add_argument(
        "--source-commit",
        default=None,
        help=(
            "源代码 commit SHA。未指定时优先取 GITHUB_SHA 环境变量,"
            "仍未设置则取 git rev-parse HEAD。"
        ),
    )
    parser.add_argument(
        "--source-tree-sha",
        default=None,
        help=(
            "源代码 git tree SHA(HEAD^{tree})。未指定时取 "
            "git rev-parse HEAD^{tree}。"
        ),
    )
    parser.add_argument(
        "--migration-manifest",
        default=None,
        help=(
            "migration-manifest.json 路径(默认 database/migrations/"
            "migration-manifest.json,相对仓库根)。"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="减少日志输出",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        generate_release_manifest(
            image_digest=args.image_digest,
            image_name=args.image_name,
            output_path=Path(args.output),
            verbose=not args.quiet,
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            source_tree_sha=args.source_tree_sha,
            migration_manifest_path=Path(args.migration_manifest) if args.migration_manifest else None,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — 顶层兜底
        print(f"ERROR (unexpected): {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
