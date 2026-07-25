#!/usr/bin/env python3
"""R66 P0-01: migration catalog sha256 重算工具(catalog-only 模型)。

R66 P0-01 根治逻辑:
    旧版 generate_migration_manifest.py 重生 ``release_commit`` / ``tree_sha``
    字段以绑定当前 HEAD/Tree —— 但 catalog 本身被提交到 Git,任何 commit 都使
    catalog 自身的 tree_sha 失效,形成"自引用循环"且不存在稳态。

    R66 P0-01 整改后:
      - Git 中 ``migration-manifest.json`` 只是 **catalog**(migration 集合 +
        顺序 + sha256 + DDL version + rollback strategy),不再绑定 commit/tree。
      - HEAD/Tree 绑定由 CI artifact ``release-artifacts/release-manifest.json``
        承担(不提交到 Git),绑定 source_commit + source_tree + catalog digest +
        image_digest(via OCI attestation)。
      - 本脚本只重算每个 migration 文件的 sha256(因 SQL 文件内容可能变化),
        不再写 release_commit / tree_sha 字段。

设计要点:
  - catalog 只描述"migration 集合 + 每个文件的 sha256",不描述"当前 release"
  - 任何 commit 都不会使 catalog 失效(只要 SQL 文件内容未变)
  - 保留字段:version, tool_version, description, verification
  - 保留每个 migration 的:order, migration_id, version, filename, predecessor,
    predecessor_filename, ddl_version, rollback_strategy,
    schema_fingerprint_before, schema_fingerprint_after, *_note
  - 重生成字段:migrations[*].sha256 → 重新计算磁盘文件 sha256

退出码:
  0 — 成功
  1 — 失败(manifest 不存在 / 解析失败 / 写入失败)
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "database" / "migrations" / "migration-manifest.json"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"


def _file_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。

    RC58 fix: 规范化 CRLF→LF,确保跨平台 digest 一致(CI/Linux LF,Windows CRLF)。
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def regenerate_manifest(verbose: bool = True) -> None:
    """重算每个 migration 的 sha256(catalog-only,不写 release_commit/tree_sha)。

    R66 P0-01: 移除 release_commit / tree_sha 字段生成逻辑。
    若 manifest 仍包含旧字段,本函数会将其删除(向后兼容迁移)。

    Args:
        verbose: 是否打印详细日志
    """
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"migration catalog 不存在: {MANIFEST_PATH}")

    # 1. 加载现有 catalog(保留结构)
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("catalog 顶层不是 JSON object")
    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        raise RuntimeError("catalog 'migrations' 字段不是 list")

    # 2. R66 P0-01: 移除 release_commit / tree_sha 字段(若残留)
    removed_fields = []
    for field in ("release_commit", "tree_sha"):
        if field in data:
            old_val = data.pop(field)
            removed_fields.append((field, old_val))
            if verbose:
                print(f"[generate_manifest] R66 P0-01: 移除自引用字段 {field} (旧值: {str(old_val)[:12]}...)")

    # 3. 重算每个 migration 的 sha256(按 catalog 中已有顺序)
    #    同时校验 catalog 集合 == 磁盘集合(不允许漏项/多项)
    manifest_versions = [str(e.get("version", "")) for e in migrations]
    disk_files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
    disk_versions = {f.name for f in disk_files}
    manifest_set = set(manifest_versions)

    missing_in_manifest = disk_versions - manifest_set
    missing_on_disk = manifest_set - disk_versions
    if missing_in_manifest:
        raise RuntimeError(
            f"磁盘存在但 catalog 未列出的 migration: {sorted(missing_in_manifest)}"
        )
    if missing_on_disk:
        raise RuntimeError(
            f"catalog 列出但磁盘不存在的 migration: {sorted(missing_on_disk)}"
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

    # 4. 写回 catalog(保持 2-space 缩进,ensure_ascii=False 以保留中文)
    MANIFEST_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if verbose:
        print(f"[generate_manifest] catalog 已重写: {MANIFEST_PATH}")
        print(f"[generate_manifest] migrations: {len(migrations)} 个")
        if removed_fields:
            print(f"[generate_manifest] R66 P0-01: 已移除自引用字段 {[f for f, _ in removed_fields]}")
        else:
            print("[generate_manifest] R66 P0-01: catalog 已是 catalog-only 模型(无 release_commit/tree_sha)")
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
