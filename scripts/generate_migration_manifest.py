#!/usr/bin/env python3
"""R63 P0-04: 构建阶段自动生成 canonical migration manifest。

终审报告 P0-04 根治逻辑:
    "构建阶段根据当前 HEAD/Tree 自动生成 canonical manifest,禁止手工旧 SHA。"

本脚本在 CI 构建阶段(运行测试之前)执行,根据当前 git HEAD/Tree 重新生成
``database/migrations/migration-manifest.json`` 的 ``release_commit`` /
``tree_sha`` 字段,并重算每个 migration 文件的 sha256。

设计要点:
  - manifest 是 release artifact,必须绑定到具体的 commit + tree
  - 禁止手工维护旧 SHA — 任何 commit 都会使旧 manifest 失效
  - CI 在运行测试前调用本脚本,确保 manifest 与当前 HEAD 严格绑定
  - 本地开发时也可手动运行 ``python scripts/generate_migration_manifest.py``
    以更新 manifest(运行测试前)

保留字段(不重生成):
  - version, tool_version, description, verification
  - 每个 migration 的 order, version, predecessor,
    schema_fingerprint_before, schema_fingerprint_after, *_note

重生成字段:
  - release_commit  → git rev-parse HEAD
  - tree_sha        → git rev-parse HEAD^{tree}
  - migrations[*].sha256 → 重新计算磁盘文件 sha256

退出码:
  0 — 成功
  1 — 失败(git 不可用 / manifest 不存在 / 解析失败 / 写入失败)
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "database" / "migrations" / "migration-manifest.json"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"


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


def _file_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regenerate_manifest(verbose: bool = True) -> None:
    """重新生成 manifest 的 release_commit / tree_sha / 各 migration sha256。

    Args:
        verbose: 是否打印详细日志
    """
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"migration manifest 不存在: {MANIFEST_PATH}")

    # 1. 获取当前 HEAD / Tree SHA
    head_sha = _git_rev_parse("HEAD")
    tree_sha = _git_rev_parse("HEAD^{tree}")
    if verbose:
        print(f"[generate_manifest] HEAD   = {head_sha}")
        print(f"[generate_manifest] Tree   = {tree_sha}")

    # 2. 加载现有 manifest(保留结构)
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("manifest 顶层不是 JSON object")
    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        raise RuntimeError("manifest 'migrations' 字段不是 list")

    # 3. 更新 release_commit / tree_sha
    old_commit = data.get("release_commit", "")
    old_tree = data.get("tree_sha", "")
    data["release_commit"] = head_sha
    data["tree_sha"] = tree_sha
    if verbose and old_commit != head_sha:
        print(f"[generate_manifest] release_commit: {old_commit[:12]}... → {head_sha[:12]}...")
    if verbose and old_tree != tree_sha:
        print(f"[generate_manifest] tree_sha:        {old_tree[:12]}... → {tree_sha[:12]}...")

    # 4. 重算每个 migration 的 sha256(按 manifest 中已有顺序)
    #    同时校验 manifest 集合 == 磁盘集合(R63 P0-04: 不允许漏项/多项)
    manifest_versions = [str(e.get("version", "")) for e in migrations]
    disk_files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
    disk_versions = {f.name for f in disk_files}
    manifest_set = set(manifest_versions)

    missing_in_manifest = disk_versions - manifest_set
    missing_on_disk = manifest_set - disk_versions
    if missing_in_manifest:
        raise RuntimeError(
            f"磁盘存在但 manifest 未列出的 migration: {sorted(missing_in_manifest)}"
        )
    if missing_on_disk:
        raise RuntimeError(
            f"manifest 列出但磁盘不存在的 migration: {sorted(missing_on_disk)}"
        )

    # 构建 {version: Path} 映射
    disk_map = {f.name: f for f in disk_files}
    for entry in migrations:
        version = str(entry.get("version", ""))
        if not version:
            continue
        disk_file = disk_map.get(version)
        if disk_file is None:
            raise RuntimeError(f"migration 文件不存在: {version}")
        old_sha = entry.get("sha256", "")
        new_sha = _file_sha256(disk_file)
        entry["sha256"] = new_sha
        if verbose and old_sha != new_sha:
            print(f"[generate_manifest] {version} sha256: {old_sha[:12]}... → {new_sha[:12]}...")

    # 5. 写回 manifest(保持 2-space 缩进,ensure_ascii=False 以保留中文)
    MANIFEST_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if verbose:
        print(f"[generate_manifest] manifest 已重写: {MANIFEST_PATH}")
        print(f"[generate_manifest] migrations: {len(migrations)} 个")
        print("[generate_manifest] DONE")


def main() -> int:
    try:
        regenerate_manifest(verbose=True)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — 顶层兜底,打印任意异常以利调试
        print(f"ERROR (unexpected): {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
