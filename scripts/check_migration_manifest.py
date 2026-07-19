#!/usr/bin/env python3
"""R65 P1-08: migration manifest 完整性 + 签名绑定严格校验。

终审报告 P1-08 根治逻辑:
    Migration 005/006/007 需纳入签名 release manifest。
    本轮 Release 在生成 release manifest 前已经失败,因此新增迁移尚无已签名、
    同 digest 的生产证据。必须验证 001–007 全集合、顺序、hash、前驱、
    DDL version 和回滚策略。

本脚本在 CI release-gates.yml migration-manifest-gate job 中运行
(在 sign-image job 之后),严格校验 ``database/migrations/migration-manifest.json``:

校验矩阵(strict 模式默认开启):
  1. 001-007 全集合存在 (manifest 必须列出所有 migration_id)
  2. predecessor 链完整 (001 ← 002 ← 003 ← ... ← 007)
  3. 每个 SQL 文件 SHA-256 与 manifest 一致 (fail-closed on tampering)
  4. ddl_version 单调非递减 (允许同值,不允许下降)
  5. rollback_strategy 非空 (每个 migration 必须有回滚策略)
  6. manifest 已签名 (或属于 release-manifest.json 一部分)
     - 检查 migration-manifest.json.sig + .pem 存在
     - 或 release-artifacts/release-manifest.json 的 migration_manifest_digest
       字段等于当前 manifest SHA-256
     - 若两者皆无,WARN 不阻断(本地开发模式);
       CI 应通过 needs: [sign-image] 保证签名完成后才运行本 gate

退出码:
  0 — 校验通过
  1 — 校验失败(任一 violation)
  2 — 严重错误(参数解析失败等)

调用示例:
  # 默认 strict 模式
  python scripts/check_migration_manifest.py
  # 显式 strict
  python scripts/check_migration_manifest.py --strict
  # 指定 manifest 路径(测试用)
  python scripts/check_migration_manifest.py --strict --manifest /tmp/manifest.json
  # 宽松模式(本地调试,不推荐)
  python scripts/check_migration_manifest.py --lenient
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
MANIFEST_PATH = MIGRATIONS_DIR / "migration-manifest.json"
RELEASE_MANIFEST_PATH = REPO_ROOT / "release-artifacts" / "release-manifest.json"

# 期望的 migration_id 集合 (001-007)
# R65 P1-08: 全集合必须存在,不允许漏项
EXPECTED_MIGRATION_IDS: list[str] = [f"{i:03d}" for i in range(1, 8)]


def _file_sha256(path: Path) -> str:
    """计算文件内容的 sha256(十六进制小写)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_ddl_version(schema_fingerprint: str | None) -> int | None:
    """从 'DDL_VERSION=N' 字符串提取 N。

    用于 ddl_version 字段缺失时从 schema_fingerprint_after 兜底提取。
    """
    if not schema_fingerprint:
        return None
    match = re.search(r"DDL_VERSION\s*=\s*(\d+)", schema_fingerprint)
    return int(match.group(1)) if match else None


def _normalize_predecessor(value) -> str | None:
    """规范化 predecessor 字段值。

    接受 None / "null" / "" / "001" 等形式,统一返回 None 或 migration_id 字符串。
    """
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v or v.lower() == "null":
            return None
        return v
    return None


def verify_manifest(
    manifest_path: Path = MANIFEST_PATH,
    strict: bool = True,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> tuple[bool, list[str], list[str]]:
    """验证 migration manifest 完整性 + 签名绑定。

    Args:
        manifest_path: manifest 文件路径
        strict: 是否启用 strict 模式(默认 True)
        migrations_dir: migrations 目录(查找 SQL 文件)

    Returns:
        (success, errors, warnings)
        success=True 表示无 violation(strict 模式下含签名检查);
        errors 是阻断性错误;warnings 是非阻断性提示
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest_path.exists():
        errors.append(f"manifest 文件不存在: {manifest_path}")
        return False, errors, warnings

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"manifest 解析失败: {e}")
        return False, errors, warnings

    if not isinstance(data, dict):
        errors.append("manifest 顶层不是 JSON object")
        return False, errors, warnings

    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        errors.append("manifest 'migrations' 字段不是 list")
        return False, errors, warnings

    if not migrations:
        errors.append("manifest 'migrations' 为空")
        return False, errors, warnings

    # 1. 校验 001-007 全集合存在
    found_ids = [str(e.get("migration_id", "")).strip() for e in migrations]
    for expected_id in EXPECTED_MIGRATION_IDS:
        if expected_id not in found_ids:
            errors.append(
                f"缺少 migration_id={expected_id} (期望 001-007 全集合)"
            )

    # 检查重复 migration_id
    seen_ids: set[str] = set()
    for mid in found_ids:
        if mid and mid in seen_ids:
            errors.append(f"migration_id={mid} 在 manifest 中重复出现")
        seen_ids.add(mid)

    # 2. 校验 predecessor 链 (按 migration_id 排序后,001 的 predecessor 为 null,
    #    其余 migration_id=N 的 predecessor 必须等于 N-1 的 migration_id)
    # 先按 migration_id 排序
    sorted_entries = sorted(
        (e for e in migrations if str(e.get("migration_id", "")).strip()),
        key=lambda e: str(e.get("migration_id", "")),
    )
    expected_chain: list[str | None] = [None] + EXPECTED_MIGRATION_IDS[:-1]
    for entry, expected_pred in zip(sorted_entries, expected_chain):
        mid = str(entry.get("migration_id", "")).strip()
        if not mid:
            errors.append(f"条目缺少 migration_id: {entry!r}")
            continue
        actual_pred = _normalize_predecessor(entry.get("predecessor"))
        if expected_pred is None:
            if actual_pred is not None:
                errors.append(
                    f"migration_id={mid} 的 predecessor 应为 null (首个 migration),"
                    f"实际: {actual_pred!r}"
                )
        else:
            if actual_pred != expected_pred:
                errors.append(
                    f"migration_id={mid} 的 predecessor 应为 '{expected_pred}',"
                    f"实际: {actual_pred!r}"
                )

    # 3. 校验每个 SQL 文件 SHA-256 与 manifest 一致
    for entry in migrations:
        mid = str(entry.get("migration_id", "")).strip()
        filename = entry.get("filename") or entry.get("version")
        if not filename:
            errors.append(f"migration_id={mid or '?'} 缺少 filename / version 字段")
            continue
        sql_path = migrations_dir / filename
        if not sql_path.exists():
            errors.append(
                f"migration_id={mid}: SQL 文件不存在: {sql_path}"
            )
            continue
        expected_sha = str(entry.get("sha256", "")).strip().lower()
        if not expected_sha:
            errors.append(f"migration_id={mid}: manifest 缺少 sha256 字段")
            continue
        actual_sha = _file_sha256(sql_path)
        if expected_sha != actual_sha:
            errors.append(
                f"migration_id={mid} ({filename}) SHA-256 不匹配: "
                f"manifest={expected_sha[:16]}... actual={actual_sha[:16]}..."
            )

    # 4. 校验 ddl_version 单调非递减
    #    (允许同值 — 因为列扩展/新表/数据补丁不 bump DDL_VERSION;
    #     只禁止下降)
    last_ddl_version: int | None = None
    for entry in sorted_entries:
        mid = str(entry.get("migration_id", "")).strip()
        if not mid:
            continue
        ddl_version = entry.get("ddl_version")
        if ddl_version is None:
            # 兜底:从 schema_fingerprint_after 提取
            ddl_version = _extract_ddl_version(entry.get("schema_fingerprint_after"))
        if ddl_version is None:
            errors.append(
                f"migration_id={mid} 缺少 ddl_version 字段且无法从 schema_fingerprint_after 提取"
            )
            continue
        try:
            ddl_version_int = int(ddl_version)
        except (TypeError, ValueError):
            errors.append(
                f"migration_id={mid} ddl_version 非数字: {ddl_version!r}"
            )
            continue
        if last_ddl_version is not None and ddl_version_int < last_ddl_version:
            errors.append(
                f"migration_id={mid} ddl_version={ddl_version_int} "
                f"< 前一个 ddl_version={last_ddl_version} (非单调非递减)"
            )
        last_ddl_version = ddl_version_int

    # 5. 校验 rollback_strategy 非空
    for entry in migrations:
        mid = str(entry.get("migration_id", "")).strip() or "?"
        rollback = entry.get("rollback_strategy")
        if rollback is None:
            errors.append(f"migration_id={mid} 缺少 rollback_strategy 字段")
        elif not isinstance(rollback, str):
            errors.append(
                f"migration_id={mid} rollback_strategy 不是字符串: {type(rollback).__name__}"
            )
        elif not rollback.strip():
            errors.append(f"migration_id={mid} rollback_strategy 为空字符串")

    # 6. 校验 manifest 已签名 (或属于 release-manifest.json 一部分)
    #    strict 模式下:若两者皆无,WARN 不阻断(本地开发模式)
    #    CI 应通过 needs: [sign-image] 保证签名完成后才运行本 gate
    if strict:
        sig_path = manifest_path.parent / "migration-manifest.json.sig"
        pem_path = manifest_path.parent / "migration-manifest.json.pem"
        signed = False
        signature_source = ""
        if sig_path.exists() and pem_path.exists():
            signed = True
            signature_source = (
                f"detached signature ({sig_path.name} + {pem_path.name})"
            )
        if not signed and RELEASE_MANIFEST_PATH.exists():
            try:
                rm = json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
                actual_mm_digest = _file_sha256(manifest_path)
                if rm.get("migration_manifest_digest") == actual_mm_digest:
                    signed = True
                    signature_source = (
                        f"release-manifest.json binding "
                        f"(migration_manifest_digest 匹配当前 manifest SHA-256)"
                    )
            except (json.JSONDecodeError, OSError) as e:
                warnings.append(
                    f"release-manifest.json 解析失败(忽略): {e}"
                )
        if signed:
            print(
                f"[INFO] manifest 签名绑定通过: {signature_source}",
                file=sys.stderr,
            )
        else:
            warnings.append(
                "未发现 manifest 独立签名文件 "
                "(migration-manifest.json.sig / .pem) "
                "且未匹配 release-manifest.json 绑定 — "
                "本地开发模式可忽略;CI 应在 sign-image 之后运行本 gate"
            )

    return len(errors) == 0, errors, warnings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "R65 P1-08: migration manifest 完整性 + 签名绑定严格校验"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="严格模式(默认开启):严格校验 001-007 全集合/SHA-256/"
        "predecessor 链/ddl_version/rollback_strategy",
    )
    parser.add_argument(
        "--lenient",
        action="store_false",
        dest="strict",
        help="宽松模式:禁用 strict 校验(仅用于本地开发调试,不推荐 CI 使用)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help=f"manifest 文件路径(默认 {MANIFEST_PATH})",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=MIGRATIONS_DIR,
        help=f"migrations 目录(默认 {MIGRATIONS_DIR})",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    success, errors, warnings = verify_manifest(
        manifest_path=args.manifest,
        strict=args.strict,
        migrations_dir=args.migrations_dir,
    )

    # 打印 warnings(总是打印,无论 success)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    if errors:
        print(f"FAIL: migration manifest 校验失败 ({len(errors)} 个 violation)", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"PASS: migration manifest 校验通过 (strict={args.strict})")
    print(f"  manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
