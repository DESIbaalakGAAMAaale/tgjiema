"""R59 P1: SQLite 版本化迁移框架。

替换 services/data_lifecycle.py 中 ``_ensure_command_approvals_table()`` 的惰性 DDL 模式,
将运行时 CREATE TABLE / ALTER TABLE 迁移到版本化 SQL 文件。

设计原则:
  - 版本化: 每个 migration 文件按 ``001_xxx.sql``, ``002_xxx.sql`` 编号,按文件名排序执行
  - 可回滚: 当前实现 up 方向(应用迁移);down 方向可通过新增降级 SQL 文件扩展
  - 可重复 dry-run: 重复执行不会产生副作用(IF NOT EXISTS + 严格白名单错误)
  - 幂等性: 已应用的 migration 通过 ``_migrations_applied`` 表记录,不会重复执行
  - 无第三方依赖: 纯 Python + aiosqlite,不引入 alembic/yoyo-migrations 等

``_migrations_applied`` 表结构(R60 P0-05 增强):
    version     TEXT PRIMARY KEY  — migration 文件名(如 '001_initial_schema.sql')
    sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改,fail-closed)
    applied_at  TEXT NOT NULL     — 应用时间(ISO 8601 格式)
    duration_ms INTEGER           — 应用耗时(毫秒)

调用方式:
    # 在 _ensure_command_approvals_table() 中调用(兼容入口)
    from database.migrate import apply_migrations
    result = await apply_migrations(db=store._db)

    # 也可独立调用(如启动时一次性应用所有 migration)
    from database.migrate import apply_migrations
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.init()
    result = await apply_migrations(db=store._db)

返回值:
    {
        "applied": [str],   — 本次新应用的 migration 文件名列表
        "skipped": [str],   — 已应用跳过的 migration 文件名列表
        "failed":  [str],   — 执行失败的 migration 文件名列表(非幂等错误)
    }
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json as _json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t

# migration 文件目录(database/migrations/)
_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"

# R61 P0-05: signed manifest listing all migrations + their SHA-256.
# Used as trust anchor for backfilling old _migrations_applied rows with empty sha256
# (trust-on-first-use: 篡改的 disk file 不能成为 trusted baseline)。
_MANIFEST_PATH: Path = _MIGRATIONS_DIR / "migration-manifest.json"

# R63 P0-04: cosign verify-blob 验证所需的常量。
# CI 在 sign-image job 中通过 cosign sign-blob --keyless 生成 detached signature,
# 签名材料与 release commit/tree 绑定。部署/迁移启动前必须 cosign verify-blob。
# 本地无 cosign 二进制或签名密钥时,通过 MIGRATION_MANIFEST_VERIFY=0 禁用验签
# (会输出 warning,但不阻断 — 仅用于本地开发/测试)。
_DEFAULT_CERT_ISSUER = "https://token.actions.githubusercontent.com"
# 仓库根目录(用于 git rev-parse 获取当前 HEAD/Tree SHA)
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _is_manifest_verify_enabled() -> bool:
    """R63 P0-04 / R64 P0-02: 检查是否启用 migration manifest 验签。

    通过环境变量 ``MIGRATION_MANIFEST_VERIFY`` 控制:
      - ``1`` / ``true`` / ``yes`` (大小写不敏感): 启用验签(CI 模式,fail-closed)
      - 未设置 / ``0`` / ``false`` / ``no``: 禁用验签(本地模式,warning 不阻断)

    CI 中应在 workflow 中设置 ``MIGRATION_MANIFEST_VERIFY=1`` 强制验签。
    本地开发/测试可不设置或显式设为 ``0``,会输出 warning 但不阻断迁移。

    R64 P0-02 新增 fail-closed 联动:
      - 若 ``APP_ENV`` 为 ``staging`` / ``production`` 且未启用验签 → raise AppError
        (拒绝启动,staging/production 必须启用验证)
      - ``APP_ENV=local`` / ``test`` / 未设置时允许禁用验签

    Returns:
        True 表示启用验签(必须通过 cosign verify-blob + 签名文件存在性检查)

    Raises:
        AppError(MIGRATION_MANIFEST_VERIFY_REQUIRED): staging/production 未启用验签
    """
    val = os.environ.get("MIGRATION_MANIFEST_VERIFY", "").strip().lower()
    enabled = val in ("1", "true", "yes")
    # R64 P0-02: staging/production 必须 fail-closed
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if not enabled and app_env in ("staging", "production"):
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_VERIFY_REQUIRED,
            params={
                "app_env": app_env,
                "reason": (
                    "staging/production 必须启用 MIGRATION_MANIFEST_VERIFY=1 "
                    "(R64 P0-02: 禁用验签拒绝启动)"
                ),
            },
        )
    return enabled


def _git_rev_parse(rev: str) -> str | None:
    """R63 P0-04: 执行 ``git rev-parse <rev>`` 获取 SHA。

    在仓库根目录执行 git 命令。若 git 不可用或不在 git 仓库中,返回 None
    (调用方应据此决定是 warn 还是 fail)。

    Args:
        rev: git revision spec,如 ``HEAD`` 或 ``HEAD^{tree}``

    Returns:
        40 字符 SHA 字符串,或 None(git 不可用/不在仓库中)
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", rev],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    # 校验为 40 字符十六进制
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
        return None
    return sha


def _verify_catalog_only_model(data: dict[str, Any]) -> None:
    """R66 P0-01: 验证 catalog 不包含自引用字段 release_commit / tree_sha。

    R66 P0-01 整改:catalog(migration-manifest.json)不再绑定 commit/tree。
    旧版 catalog 包含 release_commit / tree_sha 字段,但 catalog 自身被提交到
    Git,任何 commit 都使其失效(自引用循环且无稳态)。R66 P0-01 后:HEAD 绑定
    由 release-artifacts/release-manifest.json(CI 产物,不提交到 Git)承担。

    Args:
        data: 已解析的 catalog JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_FIELD_MISSING): catalog 包含禁止字段
            release_commit 或 tree_sha(catalog-only 模型违规)
    """
    for forbidden_field in ("release_commit", "tree_sha"):
        if forbidden_field in data:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
                params={
                    "field": forbidden_field,
                    "reason": (
                        "R66 P0-01: catalog-only 模型违规 — "
                        "catalog 禁止包含自引用字段 release_commit/tree_sha; "
                        "HEAD 绑定由 release-artifacts/release-manifest.json 承担"
                    ),
                },
            )
    logger.info(
        "[migrate] R66 P0-01: catalog-only 模型验证通过 "
        "(无 release_commit/tree_sha 自引用字段)"
    )


def _verify_manifest_migration_set(data: dict[str, Any]) -> None:
    """R63 P0-04: 验证磁盘上的 migration 文件集合与 manifest 声明集合完全一致。

    要求 "磁盘集合 == manifest 集合",不允许:
      - 磁盘有但 manifest 没列出的 migration(漏项,可能跳过验签)
      - manifest 列出但磁盘不存在的 migration(多项,可能引用旧 manifest)

    Args:
        data: 已解析的 manifest JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_SET_MISMATCH): 磁盘集合与 manifest 集合不一致
    """
    manifest_versions = {
        str(entry["version"]) for entry in data.get("migrations", [])
        if "version" in entry
    }
    disk_versions = {
        f.name for f in _MIGRATIONS_DIR.glob("*.sql")
    } if _MIGRATIONS_DIR.exists() else set()
    missing_in_manifest = disk_versions - manifest_versions
    missing_on_disk = manifest_versions - disk_versions
    if missing_in_manifest:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SET_MISMATCH,
            params={
                "missing_in_manifest": sorted(missing_in_manifest),
                "missing_on_disk": [],
            },
        )
    if missing_on_disk:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SET_MISMATCH,
            params={
                "missing_in_manifest": [],
                "missing_on_disk": sorted(missing_on_disk),
            },
        )
    logger.info(
        f"[migrate] R63 P0-04: migration 集合一致性验证通过 "
        f"({len(manifest_versions)} 个 migration)"
    )


# R64 P0-02: release artifact manifest 路径(由 generate_release_manifest.py 生成,
# CI 构建镜像时复制到 /app/release-artifacts/release-manifest.json)。
# 默认指向镜像内路径;本地开发时若不存在则跳过 release manifest 一致性验证。
_RELEASE_MANIFEST_PATH: Path = Path(
    os.environ.get(
        "RELEASE_MANIFEST_PATH",
        str(_REPO_ROOT / "release-artifacts" / "release-manifest.json"),
    )
)


def _verify_release_manifest_consistency(data: dict[str, Any]) -> None:
    """R64 P0-02 / R66 P0-01: 验证 release-manifest.json 与 migration-manifest.json 一致性 + HEAD 绑定。

    release-manifest.json 是独立的 release artifact(不提交到 git,由 CI 在
    docker build 之前生成,随镜像一起 COPY),绑定 source_commit + source_tree +
    migration digest 集合。image_digest 通过 OCI attestation 单独绑定(R66 P0-02)。
    本函数在运行时验证:

      1. release-manifest.json 存在(若 RELEASE_MANIFEST_PATH 指向的文件不存在,
         输出 warning 并跳过 — 兼容本地开发 / 旧镜像;但 staging/production
         应通过 _is_manifest_verify_enabled() 的 fail-closed 保证镜像内必有
         release-manifest.json)
      2. R66 P0-01 HEAD/Tree 绑定验证:
         - release-manifest.json.source_commit / source_tree 必须与当前 git HEAD/Tree
           一致;非 git 部署环境通过 RELEASE_SOURCE_COMMIT / RELEASE_SOURCE_TREE
           环境变量注入(从签名 attestation 或镜像 label 提取)
         - 任意一项缺失或不匹配 → raise AppError(fail-closed)
      3. release-manifest.json.migrations 集合 == migration-manifest.json.migrations 集合
      4. 每个 migration 的 sha256 一致(release manifest 与 migration manifest)
      5. release-manifest.json.migration_manifest_digest == 当前 migration-manifest.json
         实际 sha256(防止 release manifest 引用旧 migration-manifest.json)

    Args:
        data: 已解析的 migration-manifest.json dict(用于集合/digest 比对)

    Raises:
        AppError(MIGRATION_MANIFEST_RELEASE_CONSISTENCY): 集合/sha256/digest 不一致
        AppError(MIGRATION_MANIFEST_BINDING_MISMATCH): HEAD/Tree SHA 与 release manifest 不一致
        AppError(MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED): 非 git 部署环境
            未通过 RELEASE_SOURCE_COMMIT/TREE 注入 source commit/tree
    """
    if not _RELEASE_MANIFEST_PATH.exists():
        # 兼容本地开发 / 旧镜像:无 release-manifest.json 时跳过
        # (staging/production 应通过 _is_manifest_verify_enabled() 的 fail-closed
        # 保证镜像内 ENV MIGRATION_MANIFEST_VERIFY=1 + 必有 release-manifest.json;
        # 此处仅 warning,不阻断 — 真正的强制由 _is_manifest_verify_enabled() + cosign
        # 验签 + RELEASE_SOURCE_COMMIT/TREE 注入共同保证)
        logger.warning(
            f"[migrate] R64 P0-02: release-manifest.json 不存在 "
            f"({_RELEASE_MANIFEST_PATH}),跳过 release manifest 一致性验证 — "
            f"staging/production 镜像必须包含 release-manifest.json"
        )
        return
    import json as _json
    try:
        release_data = _json.loads(
            _RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
        )
    except (_json.JSONDecodeError, OSError) as e:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY,
            params={
                "reason": f"release-manifest.json 解析失败: {e}",
                "field": "release-manifest.json",
            },
        ) from e

    # 0. R66 P0-01: HEAD/Tree 绑定验证(从 release-manifest.json 提取,
    #    因 catalog 不再包含 release_commit/tree_sha)
    rm_commit = str(release_data.get("source_commit", "")).strip()
    rm_tree = str(release_data.get("source_tree", "")).strip()
    if not rm_commit or not rm_tree:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={
                "field": "source_commit/source_tree",
                "reason": "release-manifest.json 缺少 source_commit/source_tree 字段",
            },
        )
    head_sha = _git_rev_parse("HEAD")
    tree_sha = _git_rev_parse("HEAD^{tree}")
    if head_sha is None or tree_sha is None:
        # R66 P0-01: 非 git 部署环境 fail-closed
        # 从环境变量读取 RELEASE_SOURCE_COMMIT / RELEASE_SOURCE_TREE
        # (部署环境从签名 attestation 或镜像 label 注入)
        env_commit = os.environ.get("RELEASE_SOURCE_COMMIT", "").strip()
        env_tree = os.environ.get("RELEASE_SOURCE_TREE", "").strip()
        if not env_commit or not env_tree:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED,
                params={
                    "reason": (
                        "非 git 部署环境必须通过 RELEASE_SOURCE_COMMIT / "
                        "RELEASE_SOURCE_TREE 环境变量提供 source commit/tree "
                        "(从签名 attestation 或镜像 label 注入)"
                    ),
                },
            )
        if env_commit != rm_commit:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
                params={
                    "field": "source_commit",
                    "expected": rm_commit[:12],
                    "actual": env_commit[:12],
                },
            )
        if env_tree != rm_tree:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
                params={
                    "field": "source_tree",
                    "expected": rm_tree[:12],
                    "actual": env_tree[:12],
                },
            )
        logger.info(
            f"[migrate] R66 P0-01: release-manifest HEAD/Tree 绑定验证通过(非 git 环境) "
            f"(commit={env_commit[:12]}..., tree={env_tree[:12]}..., "
            f"source=RELEASE_SOURCE_COMMIT/TREE 环境变量)"
        )
    else:
        if head_sha != rm_commit:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
                params={
                    "field": "source_commit",
                    "expected": rm_commit[:12],
                    "actual": head_sha[:12],
                },
            )
        if tree_sha != rm_tree:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
                params={
                    "field": "source_tree",
                    "expected": rm_tree[:12],
                    "actual": tree_sha[:12],
                },
            )
        logger.info(
            f"[migrate] R66 P0-01: release-manifest HEAD/Tree 绑定验证通过 "
            f"(commit={head_sha[:12]}..., tree={tree_sha[:12]}...)"
        )

    # 1. 集合一致性
    migration_versions = {
        str(entry["version"]): str(entry.get("sha256", ""))
        for entry in data.get("migrations", [])
        if "version" in entry
    }
    release_versions = {
        str(entry["version"]): str(entry.get("sha256", ""))
        for entry in release_data.get("migrations", [])
        if "version" in entry
    }
    if set(migration_versions.keys()) != set(release_versions.keys()):
        missing_in_release = set(migration_versions.keys()) - set(release_versions.keys())
        missing_in_migration = set(release_versions.keys()) - set(migration_versions.keys())
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY,
            params={
                "reason": "migration 集合不一致",
                "field": "migrations",
                "expected": sorted(missing_in_release),
                "actual": sorted(missing_in_migration),
            },
        )

    # 2. 每个 migration 的 sha256 一致
    for version, expected_sha in migration_versions.items():
        actual_sha = release_versions.get(version, "")
        if actual_sha != expected_sha:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY,
                params={
                    "reason": f"{version} sha256 不一致",
                    "field": version,
                    "expected": expected_sha[:12],
                    "actual": actual_sha[:12],
                },
            )

    # 3. release-manifest.json.migration_manifest_digest == 当前 migration-manifest.json
    #    实际 sha256(防止 release manifest 引用旧 migration-manifest.json)
    expected_mm_digest = str(release_data.get("migration_manifest_digest", "")).strip()
    if expected_mm_digest:
        # RC58: 规范化 CRLF→LF(migration-manifest.json 在 CI/Linux 生成 LF,
        # Windows 检出为 CRLF,raw bytes digest 不匹配)
        actual_mm_digest = hashlib.sha256(
            _MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        if expected_mm_digest != actual_mm_digest:
            raise AppError(
                ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY,
                params={
                    "reason": "migration_manifest_digest 不一致 — release manifest 引用旧 migration-manifest.json",
                    "field": "migration_manifest_digest",
                    "expected": expected_mm_digest[:12],
                    "actual": actual_mm_digest[:12],
                },
            )

    logger.info(
        f"[migrate] R64 P0-02: release-manifest.json 与 migration-manifest.json "
        f"一致性验证通过 ({len(migration_versions)} 个 migration)"
    )


def _verify_manifest_cosign_signature(data: dict[str, Any]) -> None:
    """R63 P0-04: 通过 cosign verify-blob 验证 manifest 的 detached signature。

    要求:
      1. manifest JSON 中的 ``verification.signature_file`` / ``certificate_file``
         指向的文件必须存在(detached signature + certificate)
      2. ``cosign verify-blob`` 必须成功(签名有效 + 证书 identity/issuer 钉扎匹配)

    签名失败、签名文件缺失、证书 identity/issuer 不匹配 → raise AppError(fail-closed)。

    本地无 cosign 二进制或签名密钥时,应通过 ``MIGRATION_MANIFEST_VERIFY=0`` 禁用验签
    (会输出 warning)。CI 中必须启用。

    Args:
        data: 已解析的 manifest JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_SIGNATURE_INVALID): 签名文件缺失 / cosign 不可用 / 验签失败
    """
    verification = data.get("verification", {})
    if not isinstance(verification, dict):
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={"field": "verification", "reason": "missing_or_not_dict"},
        )
    sig_rel = str(verification.get("signature_file", "")).strip()
    cert_rel = str(verification.get("certificate_file", "")).strip()
    issuer = str(
        verification.get("certificate_oidc_issuer", _DEFAULT_CERT_ISSUER)
    ).strip()
    if not sig_rel or not cert_rel:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={
                "field": "signature_file/certificate_file",
                "reason": "empty",
            },
        )
    sig_path = _MIGRATIONS_DIR / sig_rel
    cert_path = _MIGRATIONS_DIR / cert_rel
    if not sig_path.exists():
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "signature_file_not_found",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    if not cert_path.exists():
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "certificate_file_not_found",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    cosign_bin = shutil.which("cosign")
    if cosign_bin is None:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "cosign_binary_not_in_path",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    # 从 manifest 提取 certificate_identity_prefix,构造精确 identity
    # (CI 签名时使用的 workflow identity)
    identity_prefix = str(
        verification.get("certificate_identity_prefix", "")
    ).strip()
    if not identity_prefix:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={
                "field": "certificate_identity_prefix",
                "reason": "empty",
            },
        )
    # 通过 git 获取当前 ref,构造完整 identity
    # (与 CI 签名时使用的 identity 完全一致)
    head_sha = _git_rev_parse("HEAD")
    if head_sha is None:
        # 退化:只用 prefix 做前缀匹配(regexp 模式)
        identity_regexp = _escape_regexp(identity_prefix) + r".+"
        cmd = [
            cosign_bin, "verify-blob",
            "--certificate-identity-regexp", identity_regexp,
            "--certificate-oidc-issuer", issuer,
            "--certificate", str(cert_path),
            "--signature", str(sig_path),
            str(_MANIFEST_PATH),
        ]
    else:
        # 精确模式:prefix + 当前 ref(从 git symbolic-ref 获取)
        git_bin = shutil.which("git")
        ref_result = subprocess.run(
            [git_bin, "symbolic-ref", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=10, check=False,
        )
        if ref_result.returncode == 0:
            current_ref = ref_result.stdout.strip()
        else:
            # detached HEAD,退化到 regexp 模式
            current_ref = None
        if current_ref:
            identity = f"{identity_prefix}{current_ref}"
            cmd = [
                cosign_bin, "verify-blob",
                "--certificate-identity", identity,
                "--certificate-oidc-issuer", issuer,
                "--certificate", str(cert_path),
                "--signature", str(sig_path),
                str(_MANIFEST_PATH),
            ]
        else:
            identity_regexp = _escape_regexp(identity_prefix) + r".+"
            cmd = [
                cosign_bin, "verify-blob",
                "--certificate-identity-regexp", identity_regexp,
                "--certificate-oidc-issuer", issuer,
                "--certificate", str(cert_path),
                "--signature", str(sig_path),
                str(_MANIFEST_PATH),
            ]
    logger.info(
        f"[migrate] R63 P0-04: cosign verify-blob 验证 manifest 签名 "
        f"(manifest={_MANIFEST_PATH.name})"
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": f"cosign_verify_blob_execution_failed: {e}",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        ) from e
    if result.returncode != 0:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": f"cosign_verify_blob_failed exit={result.returncode}",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    logger.info("[migrate] R63 P0-04: migration manifest cosign 验签通过")


def _escape_regexp(s: str) -> str:
    """转义字符串中的正则元字符,使其可作为字面量用于 regexp。

    Args:
        s: 待转义的字符串

    Returns:
        转义后的字符串(正则元字符已转义)
    """
    import re
    return re.escape(s)


def _strip_sql_comments(sql_content: str) -> str:
    """Remove SQL comments (``--`` line and ``/* */`` block) before splitting.

    R61 P1-02: 旧版 splitter 用字符串分割 + 行内 ``--`` 截断会破坏字符串字面量
    (例如 CHECK 约束中的 ``'a-b'`` 不会被破坏,但 ``--`` 出现在字符串内时被误截断)。
    新版只移除整行 ``--`` 注释 + 块注释,保留字符串字面量内的 ``--``。
    """
    import re
    # Remove /* */ block comments (DOTALL: . matches newline)
    result = re.sub(r'/\*.*?\*/', '', sql_content, flags=re.DOTALL)
    # Remove -- line comments (only when line starts with -- after lstrip;
    # 不处理行内 -- 以避免破坏字符串字面量中的 --,如 'a-b' 或 CHECK 约束)
    lines = []
    for line in result.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('--'):
            continue
        lines.append(line)
    return '\n'.join(lines)


def _split_sql_statements(sql_content: str) -> list[str]:
    """Split SQL using :func:`sqlite3.complete_statement` for proper parsing.

    R61 P1-02: 旧版用 naive 分号分割,无法处理字符串内的分号 / 触发器体 / CASE 表达式
    内的分号。新版改用 SQLite 原生 :func:`sqlite3.complete_statement` 判断语句边界
    (该函数考虑引号配对 + 分号结尾,与 sqlite3 CLI 行为一致)。

    处理规则:
      1. 移除块注释 ``/* */`` 和整行 ``--`` 注释(保留字符串字面量)
      2. 逐行累积到 buffer,直到 :func:`sqlite3.complete_statement` 返回 True
      3. 过滤空白语句

    Args:
        sql_content: SQL 文件原始内容

    Returns:
        SQL 语句列表(每条语句已 strip)
    """
    import sqlite3
    cleaned = _strip_sql_comments(sql_content)
    statements: list[str] = []
    buffer = ""
    for line in cleaned.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    # Handle any remaining buffer (无尾分号的最后一条语句)
    remaining = buffer.strip()
    if remaining:
        statements.append(remaining)
    return statements


async def _should_skip_statement(db: Any, stmt: str) -> bool:
    """R61 P1-02: 判断单条 SQL 语句是否应跳过(幂等预检)。

    替换 R59 的 ``_is_ignorable_error`` 子串匹配机制(该机制会吞掉所有
    "duplicate column" / "already exists" 错误,无法区分"列已存在"与
    "约束已存在"等不同语义)。

    新版改为执行前 PRAGMA 预检:仅对 ``ALTER TABLE ... ADD COLUMN`` 语句,
    若目标列已存在则跳过(等价于 ``ADD COLUMN IF NOT EXISTS``,SQLite 原生不支持)。
    其他 DDL 错误(语法/约束/连接等)一律让事务 ROLLBACK(fail-closed)。

    Args:
        db: aiosqlite.Connection(处于 BEGIN IMMEDIATE 事务中)
        stmt: 待执行的 SQL 语句(已 strip)

    Returns:
        True 表示该语句应跳过(列已存在);False 表示应执行
    """
    import re
    # 仅匹配:ALTER TABLE <table> ADD COLUMN <column> ...
    # (不匹配 ALTER TABLE ... RENAME / DROP COLUMN 等)
    m = re.match(
        r'ALTER\s+TABLE\s+["\'`]?(\w+)["\'`]?\s+ADD\s+COLUMN\s+["\'`]?(\w+)["\'`]?',
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return False
    table_name = m.group(1)
    column_name = m.group(2)
    # PRAGMA table_info 不支持参数绑定,table_name 来自受信任的 migration SQL(非用户输入)
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    existing_cols: set[str] = {str(row[1]) for row in await cursor.fetchall()}
    return column_name in existing_cols


async def _assert_migration_fingerprint(db: Any, version: str) -> None:
    """R61 P1-02: Post-execution schema fingerprint assertion.

    在 migration 所有语句执行完毕、COMMIT 之前,验证 schema 是否符合预期
    (defense in depth:即使 SQL 执行成功但 schema 漂移也应阻断)。
    任一验证失败 raise RuntimeError,触发事务 ROLLBACK,migration 不被记录为已应用。

    Args:
        db: aiosqlite.Connection(处于 BEGIN IMMEDIATE 事务中,DDL 已执行但未提交)
        version: migration 文件名(如 '003_rebuild_command_approvals.sql')

    Raises:
        RuntimeError: schema 指纹不匹配(缺失列 / 表不存在)
    """
    if version == "003_rebuild_command_approvals.sql":
        # Verify the rebuilt command_approvals table exists with all required columns
        cursor = await db.execute("PRAGMA table_info(command_approvals)")
        cols = {str(row[1]) for row in await cursor.fetchall()}
        required = {
            "id", "action_id", "approver_id", "approval_type", "decision",
            "request_hash", "mfa_receipt", "permission", "approved_at",
            "expires_at", "consumed_at", "revoked_at", "metadata_json",
        }
        if not required.issubset(cols):
            missing = required - cols
            raise RuntimeError(
                f"Migration {version} fingerprint mismatch: "
                f"missing columns {missing} (actual cols: {sorted(cols)})"
            )

    if version == "004_effect_receipts_request_hash_unique.sql":
        # R63 P1-03: Post-migration conservation + evidence-completeness assertion.
        # 防御纵深:004 migration SQL 内已有 CASE WHEN 守恒/证据断言(违反 CHECK →
        # ROLLBACK),此处再在 Python 层做一次跨表 COUNT 比对(SQLite SQL 内难以
        # 直接 RAISE 跨表断言,Python 层是最可靠的 fail-closed 位置)。
        # 守恒等式(rename 后):
        #   count(effect_receipts)               — strict winner 行(rename 后的新表)
        #   + count(effect_receipts_r62_quarantine)            — 非法隔离行
        #   + count(effect_receipts_r62_duplicates WHERE classification='duplicate') — 去重 loser 行
        #   == count(effect_receipts_invalid_r62)              — 旧表(rename 后保留取证)
        # 证据完整性:count(effect_receipts_r62_duplicates) == count(effect_receipts_invalid_r62)
        #   (每条原始 row 都在取证表有一行,不论分类)
        cursor = await db.execute("SELECT COUNT(*) FROM effect_receipts")
        strict_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        )
        quarantine_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        )
        duplicates_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
        )
        original_count = (await cursor.fetchone())[0]
        if strict_count + quarantine_count + duplicates_count != original_count:
            raise RuntimeError(
                f"Migration {version} conservation assertion failed: "
                f"strict({strict_count}) + quarantine({quarantine_count}) "
                f"+ duplicates({duplicates_count}) "
                f"!= original({original_count}) — rows silently lost"
            )
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
        )
        evidence_count = (await cursor.fetchone())[0]
        if evidence_count != original_count:
            raise RuntimeError(
                f"Migration {version} evidence completeness failed: "
                f"duplicates evidence table has {evidence_count} rows, "
                f"original has {original_count} — not every original row has evidence"
            )


def _load_migration_manifest() -> dict[str, dict[str, Any]]:
    """R61 P0-05 / R63 P0-04: 加载并验证 migration-manifest.json,返回 {version: entry} 映射。

    manifest 是签名的 trust anchor,列出每个 migration 文件的预期 SHA-256。
    用于 backfill 旧 ``_migrations_applied`` 行(stored_sha256 为空时),
    替代 R60 的"信任当前 disk file"TOFU(篡改的 disk file 不能成为 baseline)。

    R63 P0-04 整改要点:
      - 不再把未验签 JSON 称作 signed trust anchor — 加载时强制执行:
        ① R66 P0-01 catalog-only 模型验证(catalog 不得含 release_commit/tree_sha)
        ② 磁盘 migration 集合 == manifest 集合(不允许漏项/多项)
        ③ R66 P0-01 release-manifest.json 的 source_commit/source_tree 必须匹配当前 git
        ④ 若 ``MIGRATION_MANIFEST_VERIFY=1``: cosign verify-blob 验证 detached signature
      - 验签失败、签名文件缺失、HEAD/Tree 不匹配、集合不一致 → raise(fail-closed)
      - 本地无 cosign 时可设 ``MIGRATION_MANIFEST_VERIFY=0`` 跳过 cosign 验签(warning)

    Returns:
        {version: {"sha256": str, "predecessor": str|None, ...}} 映射

    Raises:
        RuntimeError: manifest 文件不存在 / 解析失败 / HEAD/Tree 不匹配 /
                      集合不一致 / cosign 验签失败(fail-closed)
    """
    import json
    if not _MANIFEST_PATH.exists():
        raise RuntimeError(
            f"R61 P0-05: migration manifest not found at {_MANIFEST_PATH} "
            f"(required for trust-on-first-use backfill of old _migrations_applied rows)"
        )
    try:
        data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"R61 P0-05: migration manifest parse failed: {e}"
        ) from e
    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        raise RuntimeError(
            f"R61 P0-05: migration manifest 'migrations' field is not a list"
        )
    # R63 P0-04: 加载 manifest 作为 trust anchor 前必须验证完整性
    # R66 P0-01: catalog-only 模型 — catalog 不再绑定 HEAD/Tree,
    #            HEAD 绑定由 release-manifest.json(CI 产物)承担
    # (catalog-only 模型验证 + 磁盘集合一致性 + release-manifest 一致性 +
    #  可选 cosign 验签)
    _verify_catalog_only_model(data)
    _verify_manifest_migration_set(data)
    _verify_release_manifest_consistency(data)
    if _is_manifest_verify_enabled():
        _verify_manifest_cosign_signature(data)
    else:
        logger.warning(
            "[migrate] R63 P0-04: MIGRATION_MANIFEST_VERIFY 未启用, "
            "跳过 cosign verify-blob 验签 — 本地开发/测试模式,不验证 manifest 签名。"
            "CI 部署/迁移启动前必须设置 MIGRATION_MANIFEST_VERIFY=1 强制验签。"
        )
    return {str(entry["version"]): entry for entry in migrations if "version" in entry}


def _list_migration_files() -> list[Path]:
    """列出 migrations 目录下所有 .sql 文件,按文件名排序。

    Returns:
        排序后的 Path 列表(如 001_initial_schema.sql, 002_xxx.sql, ...)
    """
    if not _MIGRATIONS_DIR.exists():
        logger.warning(f"[migrate] migration 目录不存在: {_MIGRATIONS_DIR}")
        return []
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _compute_sha256(file_path: Path) -> str:
    """计算文件内容的 SHA-256 校验和(十六进制小写)。

    用于在应用 migration 时记录其 SQL 内容指纹,启动时比对以检测文件被篡改
    (R60 P0-05: fail-closed,篡改/删除的 migration 文件阻断服务启动)。

    RC58 fix: 规范化 CRLF→LF 后再计算 sha256,确保跨平台一致。
    migration-manifest.json 在 CI(Linux,LF)中生成,Windows 检出文件
    因 core.autocrlf=true 会变成 CRLF,导致 raw bytes sha256 不匹配。
    规范化行尾后 Windows/Linux 计算结果一致。
    """
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            # RC58: CRLF→LF 规范化,跨平台 sha256 一致
            h.update(chunk.replace(b"\r\n", b"\n"))
    return h.hexdigest()


async def _get_applied_versions(
    db: Any,
    *,
    check_mode: bool = False,
) -> dict[str, str]:
    """查询已应用的 migration 版本及其 SHA-256 校验和。

    若 ``_migrations_applied`` 表不存在则自动创建(首次运行,R60 新 schema)。
    若旧 schema(无 sha256 / duration_ms 列)已存在,通过 ALTER TABLE ADD COLUMN
    补列(向后兼容 R59 已部署实例);旧记录的 sha256 留空,由 ``apply_migrations``
    用当前文件内容回填。

    R72 P0-08 / RC58 fix: ``check_mode=True`` 时跳过所有 DDL(CREATE TABLE /
    ALTER TABLE / COMMIT),仅执行 SELECT。若 ``_migrations_applied`` 表不存在,
    返回空 dict(视为"所有 migration 未应用" = 全部 pending)。这避免了
    check_mode 下的写锁争抢(虽然 _migrations_applied 表很小,DDL 很快,
    但与 db_writer 进程的写操作仍可能冲突导致短暂等待)。

    R60 P0-05 schema:
        version     TEXT PRIMARY KEY  — migration 文件名
        sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改)
        applied_at  TEXT NOT NULL     — 应用时间(ISO 8601)
        duration_ms INTEGER           — 应用耗时(毫秒)

    Args:
        db: aiosqlite.Connection
        check_mode: True 时跳过 DDL,仅 SELECT(只读模式)

    Returns:
        {version: sha256} 映射(已应用 migration 的文件名 → 校验和,空串表示旧记录未回填)
    """
    if check_mode:
        # RC58 fix: check_mode 下跳过 DDL,仅 SELECT
        # 若表不存在(极少数情况:migration_runner 未运行或失败),SELECT 会抛
        # sqlite3.OperationalError "no such table",捕获后返回空 dict(全部 pending)
        try:
            cursor = await db.execute("SELECT version, sha256 FROM _migrations_applied")
            rows = await cursor.fetchall()
            return {str(row[0]): (row[1] or "") for row in rows}
        except Exception as e:
            if "no such table" in str(e).lower():
                logger.warning(
                    "[migrate] RC58: check_mode 下 _migrations_applied 表不存在 "
                    "(migration_runner 可能未运行) → 视为全部 pending"
                )
                return {}
            raise
    # 创建版本记录表(首次运行,R60 新 schema;对已存在表是 no-op)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS _migrations_applied (
            version     TEXT PRIMARY KEY,
            sha256      TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            duration_ms INTEGER
        )"""
    )
    # 向后兼容: 旧 R59 schema 只有 (version, applied_at),补 sha256 / duration_ms 列。
    # CREATE TABLE IF NOT EXISTS 不会修改已存在表,需通过 PRAGMA 检测列是否缺失。
    cursor = await db.execute("PRAGMA table_info(_migrations_applied)")
    existing_cols: set[str] = {str(row[1]) for row in await cursor.fetchall()}
    if "sha256" not in existing_cols:
        # 旧表已有行无 sha256,先以可空列补上(不能对非空表加 NOT NULL),
        # 后续由 apply_migrations 回填当前文件校验和
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN sha256 TEXT"
        )
    if "duration_ms" not in existing_cols:
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN duration_ms INTEGER"
        )
    await db.commit()
    # 查询已应用的版本及其 sha256
    cursor = await db.execute("SELECT version, sha256 FROM _migrations_applied")
    rows = await cursor.fetchall()
    return {str(row[0]): (row[1] or "") for row in rows}


async def _apply_single_migration(db: Any, migration_file: Path) -> bool:
    """应用单个 migration 文件(R60 P0-05 / R61 P1-02: 显式事务 + 预检 + 指纹断言)。

    整个 migration(所有 DDL + 版本记录 INSERT)在单个 ``BEGIN IMMEDIATE`` 事务中
    执行,确保部分 DDL 或被篡改的 migration 文件不会被记录为已应用。

    R61 P1-02 改动(替换旧的 _is_ignorable_error 子串匹配):
      - 执行前用 ``_should_skip_statement`` PRAGMA 预检(仅 ALTER TABLE ADD COLUMN):
        目标列已存在则跳过(等价 ADD COLUMN IF NOT EXISTS)
      - 其他任何 DDL 错误(语法/约束/连接等)一律让事务 ROLLBACK(fail-closed)
      - 所有语句执行后、COMMIT 前调用 ``_assert_migration_fingerprint`` 验证 schema
        (defense in depth: SQL 执行成功但 schema 漂移也阻断)

    R61 P0-05 改动(防御纵深):
      - 版本记录 INSERT 改用 plain ``INSERT INTO``(原 ``INSERT OR REPLACE``)
        PRIMARY KEY 冲突 raise RuntimeError(不应发生 — apply_migrations 已跳过
        已应用版本;若发生说明存在并发写入或状态错乱)

    成功后将版本记录(含 SQL 内容 SHA-256 与耗时)写入 ``_migrations_applied`` 表
    (与 DDL 在同一事务内提交)。

    Args:
        db: aiosqlite.Connection
        migration_file: migration SQL 文件路径

    Returns:
        True 应用成功;False 应用失败(可恢复的执行/提交失败)

    Raises:
        RuntimeError: schema 指纹不匹配(P1-02)或 INSERT PRIMARY KEY 冲突(P0-05)
    """
    version = migration_file.name
    sql_content = migration_file.read_text(encoding="utf-8")
    sha256 = _compute_sha256(migration_file)
    statements = _split_sql_statements(sql_content)
    if not statements:
        logger.warning(f"[migrate] {version} 无可执行 SQL 语句,跳过")
        return True
    logger.info(
        f"[migrate] 应用 {version}({len(statements)} 条语句, sha256={sha256[:12]}...)"
    )
    start_ts = time.perf_counter()
    # R60 P0-05: 显式事务 — 单个 migration 的所有 DDL + 版本记录 INSERT 必须原子提交
    # R60 §ci-fix: except 中不直接 return False(AST 错误协议规则3),
    # 改用标志位在 except 外返回,保持 bool 契约同时满足 fail-closed
    begin_failed = False
    try:
        await db.execute("BEGIN IMMEDIATE")
    except Exception as e:
        logger.error(f"[migrate] {version} BEGIN IMMEDIATE 失败: {e}")
        begin_failed = True
    if begin_failed:
        return False
    commit_failed = False
    try:
        for stmt in statements:
            # R61 P1-02: 执行前 PRAGMA 预检(替换旧的 _is_ignorable_error 子串匹配)
            # 仅对 ALTER TABLE ADD COLUMN: 若列已存在则跳过(幂等,等价 IF NOT EXISTS)
            if await _should_skip_statement(db, stmt):
                logger.debug(
                    f"[migrate] {version} 语句跳过(目标列已存在,幂等预检): "
                    f"{stmt[:80]}..."
                )
                continue
            await db.execute(stmt)
        # R61 P1-02: schema 指纹断言 — 所有 DDL 执行成功后、COMMIT 前验证 schema
        # (defense in depth: SQL 执行成功但 schema 漂移也阻断,raise RuntimeError)
        await _assert_migration_fingerprint(db, version)
        # 记录为已应用(与 DDL 在同一事务内,确保原子)
        now_iso = _dt.datetime.now().isoformat()
        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        # R61 P0-05: plain INSERT(非 INSERT OR REPLACE) — PRIMARY KEY 冲突应
        # 失败而非静默覆盖(apply_migrations 已跳过已应用版本,冲突=状态错乱)
        try:
            await db.execute(
                "INSERT INTO _migrations_applied "
                "(version, sha256, applied_at, duration_ms) VALUES (?, ?, ?, ?)",
                (version, sha256, now_iso, duration_ms),
            )
        except Exception as insert_err:
            raise RuntimeError(
                f"[migrate] {version} INSERT INTO _migrations_applied 失败 "
                f"(可能 PRIMARY KEY 冲突 — 版本已应用?并发写入?): {insert_err}"
            ) from insert_err
        await db.execute("COMMIT")
    except RuntimeError:
        # R61 P0-05 / P1-02: RuntimeError = 防御纵深失败(指纹不匹配 / PRIMARY KEY 冲突)
        # ROLLBACK 后重新抛出(不转换为 return False,确保调用方见到显式失败)
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_err:
            logger.error(f"[migrate] {version} ROLLBACK 失败: {rollback_err}")
        raise
    except Exception as e:
        # 其他执行/提交失败: 回滚,不记录为已应用
        logger.error(
            f"[migrate] {version} 事务提交/版本记录失败,执行 ROLLBACK: {e}"
        )
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_err:
            logger.error(f"[migrate] {version} ROLLBACK 失败: {rollback_err}")
        commit_failed = True
    if commit_failed:
        return False
    logger.info(
        f"[migrate] {version} 应用完成(耗时 {duration_ms}ms)"
    )
    return True


async def apply_migrations(
    db: Any = None,
    *,
    check_mode: bool = False,
) -> dict[str, list[str]]:
    """R59 P1: 应用所有未执行的 SQLite migration。

    R72 P0-08 / RC58 fix: ``check_mode=True`` 时使用直连 SQLite 只读连接,
    不调用 ``cache_store.init()``(会执行大量 DDL CREATE TABLE/ALTER TABLE,
    与运行中的 db_writer 进程争抢 SQLite 写锁,导致 600s 超时)。
    check_mode 仅需读取 ``_migrations_applied`` 表 + 计算 migration 文件 sha256,
    无需初始化 cache_store 的全部业务表 DDL。

    本函数是迁移框架的主入口,执行流程:
      1. 获取数据库连接(参数传入或从 CacheStore 获取)
      2. 创建 ``_migrations_applied`` 版本记录表(首次运行,R60 新 schema)
      3. 列出 migrations 目录下所有 .sql 文件,按文件名排序
      4. R60 P0-05: 校验已应用 migration 文件 SHA-256(篡改/删除 → raise 阻断启动)
      5. 对每个未应用的 migration:
         a. 读取 SQL 内容并按分号分割为独立语句
         b. 在单个 BEGIN IMMEDIATE 事务中逐条执行,可忽略
            "duplicate column" / "already exists" 错误
         c. 非白名单错误立即 ROLLBACK 并终止该 migration,不记录为已应用
         d. 成功后(同一事务)写入 _migrations_applied 表(含 sha256 / duration_ms)
      6. 返回应用结果汇总;若 failed 非空则 raise(fail-closed,禁止继续服务)

    R72 P0-08 / RC57 fix: ``check_mode=True`` 时本函数为纯只读检查:
      - 仍执行步骤 1-4(连接数据库、创建版本表、读取已应用版本、SHA-256 校验)
        注:步骤 2 创建 ``_migrations_applied`` 表是幂等的(CREATE TABLE IF NOT
        EXISTS),不修改已存在表结构,仅确保版本表存在以便 SELECT。若库为全新且
        版本表不存在,CREATE TABLE 是必要的最小写操作,否则无法检查。
      - 跳过步骤 5(不调用 ``_apply_single_migration``,不执行 DDL,不写版本记录)
      - 未应用的 migration 记录到 ``pending`` 字段(由 _build_migration_evidence
        从 applied/skipped 集合差集计算,无需在此显式填充)
      - SHA-256 校验仍执行(篡改/删除已应用 migration 必须阻断,即使 check 模式)
      - 用于 ``python -m database.migrate --check --json`` 干运行验证

    幂等性保证:
      - 已应用的 migration 不会重复执行(_migrations_applied 主键去重)
      - SQL 语句使用 IF NOT EXISTS / 白名单错误处理,重复执行无副作用
      - 支持多次 dry-run(重复调用 apply_migrations 不会产生副作用)
      - R60 P0-05: 启动时校验已应用 migration 文件 SHA-256,篡改/删除则 raise
      - R60 P0-05: 失败的 migration 必须 raise,禁止带失败结果继续服务

    Args:
        db: 可选的 aiosqlite.Connection。若为 None,从 CacheStore 获取连接。
            测试中可传入自定义连接以隔离测试。
        check_mode: R72 P0-08: True 时只读检查(不应用 pending migration),
            用于 ``--check`` CLI 子命令。默认 False(应用 pending migration)。

    Returns:
        {
            "applied": [str],  — 本次新应用的 migration 文件名列表
            "skipped": [str],  — 已应用跳过的 migration 文件名列表
            "failed":  [str],  — 执行失败的 migration 文件名列表(非幂等错误)
        }
        check_mode=True 时 applied 永远为 [](未应用任何 migration)。
    """
    # 获取数据库连接
    # R72 P0-08 / RC58 fix: check_mode=True 时直接用 aiosqlite 打开 SQLite 文件,
    # 跳过 cache_store.init()。cache_store.init() 执行 ~30 条 DDL(CREATE TABLE
    # IF NOT EXISTS / ALTER TABLE / CREATE INDEX),每条 DDL 都需要 SQLite 写锁。
    # 当 migration_check 阶段通过 `docker compose exec db_writer python -m
    # database.migrate --check` 在运行中的 db_writer 容器内启动新进程时,
    # 新进程的 cache_store.init() DDL 与 db_writer 主进程的写操作争抢 WAL 写锁,
    # 导致 600s 超时(prstat busy_timeout=15s × 30+ DDL = 450s+,接近超时边界)。
    # check_mode 只需读取 _migrations_applied 表 + 计算 sha256,无需业务表 DDL。
    own_connection = False
    should_close = False  # RC58: 仅 check_mode 自建连接需关闭(cache_store 连接不关)
    if db is None:
        if check_mode:
            # RC58 fix: check_mode 直连 SQLite,不初始化 cache_store
            # 运行时读取 DB_PATH(非 import 时绑定),支持 monkeypatch 测试
            import aiosqlite as _aiosqlite
            import database.cache_store as _cache_store_mod
            _DB_PATH = _cache_store_mod.DB_PATH
            try:
                db = await _aiosqlite.connect(str(_DB_PATH), timeout=10)
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA busy_timeout=5000")
                own_connection = True
                should_close = True
                logger.info(
                    f"[migrate] R72 P0-08/RC58: check_mode 直连 SQLite "
                    f"(path={_DB_PATH}, 跳过 cache_store.init 的 30+ DDL)"
                )
            except Exception as conn_err:
                logger.error(
                    f"[migrate] RC58: check_mode 直连 SQLite 失败: {conn_err}"
                )
                return {"applied": [], "skipped": [], "failed": []}
        else:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                # CacheStore 未初始化,尝试 init
                await store.init()
            db = store._db
            own_connection = True
    if db is None:
        logger.error("[migrate] 无法获取 SQLite 连接,迁移中止")
        return {"applied": [], "skipped": [], "failed": []}

    result: dict[str, list[str]] = {
        "applied": [],
        "skipped": [],
        "failed": [],
    }

    # 查询已应用版本
    try:
        applied_versions = await _get_applied_versions(db, check_mode=check_mode)
    except Exception as e:
        logger.error(f"[migrate] 查询已应用版本失败: {e}")
        if should_close:
            await db.close()
        return result

    # 列出所有 migration 文件
    migration_files = _list_migration_files()
    if not migration_files:
        logger.warning("[migrate] 无 migration 文件可执行")
        if should_close:
            await db.close()
        return result

    # R60 P0-05: 启动时校验已应用 migration 文件的 SHA-256(fail-closed)
    # 任何已应用 migration 的文件被修改或删除 → 阻断启动,禁止带篡改文件继续服务
    file_map: dict[str, Path] = {mf.name: mf for mf in migration_files}
    # R61 P0-05: 旧记录(stored_sha256 为空)的 backfill 改用签名 manifest 作为
    # trust anchor,而非"信任当前 disk file"(原 TOFU 使篡改的 disk file 成为 baseline)。
    # 仅在存在空 sha256 旧记录时才加载 manifest(避免全新库无 manifest 也能启动)。
    manifest = None
    needs_manifest = any(
        not stored_sha256 for stored_sha256 in applied_versions.values()
    )
    if needs_manifest:
        manifest = _load_migration_manifest()
    for version, stored_sha256 in applied_versions.items():
        if version not in file_map:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256 or '<empty>'}, "
                f"actual_sha256=None)"
            )
        actual_sha256 = _compute_sha256(file_map[version])
        if not stored_sha256:
            # R61 P0-05: 旧记录无 sha256 — 不再信任 disk file,改用 manifest 验证。
            # trust anchor 应是签名 manifest,而非可被篡改的 disk file。
            if manifest is None:
                # needs_manifest 为 False 但 stored_sha256 为空 — 逻辑不应发生,
                # 仍 fail-closed(强制要求 manifest)
                manifest = _load_migration_manifest()
            if version not in manifest:
                raise RuntimeError(
                    f"Migration {version} not listed in migration-manifest.json "
                    f"(cannot backfill empty stored_sha256 — unknown trust baseline)"
                )
            manifest_sha256 = manifest[version].get("sha256", "")
            if not manifest_sha256:
                raise RuntimeError(
                    f"Migration {version} manifest entry missing 'sha256' field"
                )
            if actual_sha256 != manifest_sha256:
                # disk file 的 sha256 与 manifest 不符 → 篡改 detected
                # (拒绝将篡改的 disk 作为 trusted baseline 回填)
                raise RuntimeError(
                    f"Migration file {version} has been tampered "
                    f"(disk sha256={actual_sha256}, "
                    f"manifest sha256={manifest_sha256}) — "
                    f"refusing to backfill empty stored_sha256 from untrusted disk"
                )
            # disk 与 manifest 一致 → 用 manifest 的 sha256 回填(trust anchor)
            # R72 P0-08 / RC58 fix: check_mode 下跳过 UPDATE 回填(避免写锁争抢),
            # 仅验证 disk == manifest 一致即可(check_mode 是只读验证,不需要持久化回填)
            if check_mode:
                logger.info(
                    f"[migrate] R61 P0-05/RC58: check_mode 下跳过 sha256 回填 "
                    f"(disk==manifest 一致验证通过, {version})"
                )
            else:
                await db.execute(
                    "UPDATE _migrations_applied SET sha256 = ? WHERE version = ?",
                    (manifest_sha256, version),
                )
                await db.commit()
                logger.info(
                    f"[migrate] R61 P0-05: 补齐历史 migration {version} 的 sha256 "
                    f"(from signed manifest, disk verified match)"
                )
        elif actual_sha256 != stored_sha256:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256}, "
                f"actual_sha256={actual_sha256})"
            )

    # 逐个应用未执行的 migration
    # R72 P0-08 / RC57 fix: check_mode=True 时不应用 pending migration,
    # 仅记录为 skipped(applied_versions 中已有)或留空(由 _build_migration_evidence
    # 计算 pending 列表)。这是 --check 子命令的只读语义。
    #
    # 根因(RC56 compose-runtime-e2e migration_check 600s 超时):
    #   旧实现 --check 参数被 main() 解析但从未传给 apply_migrations()。--check
    #   模式仍执行 _apply_single_migration → BEGIN IMMEDIATE 写锁,与运行中的
    #   db_writer 进程争抢 SQLite WAL 写锁,导致 600s 超时。
    #   修复:check_mode=True 时跳过 _apply_single_migration 调用,纯只读。
    if check_mode:
        logger.info(
            f"[migrate] R72 P0-08: check_mode=True,跳过 pending migration 应用 "
            f"(只读检查 — 已应用 {len(applied_versions)} 个, "
            f"待应用 {len(migration_files) - len(applied_versions)} 个)"
        )
        for mf in migration_files:
            version = mf.name
            if version in applied_versions:
                result["skipped"].append(version)
            # 未应用的 migration 不加入 applied/skipped/failed,
            # 由 _build_migration_evidence 从 expected_versions 差集计算 pending 列表
        # RC58: 关闭 check_mode 自建连接(避免文件锁泄漏)
        if should_close:
            await db.close()
        return result

    for mf in migration_files:
        version = mf.name
        if version in applied_versions:
            result["skipped"].append(version)
            continue
        success = await _apply_single_migration(db, mf)
        if success:
            result["applied"].append(version)
        else:
            result["failed"].append(version)
            # 遇到严重错误终止后续 migration(避免版本错位)
            logger.error(
                f"[migrate] {version} 应用失败,终止后续 migration(避免版本错位)"
            )
            break

    logger.info(
        f"[migrate] 迁移完成: 应用 {len(result['applied'])} 个, "
        f"跳过 {len(result['skipped'])} 个, 失败 {len(result['failed'])} 个"
    )
    # R60 P0-05: 失败必须 raise,禁止带失败结果继续提供服务(fail-closed)
    if result["failed"]:
        raise RuntimeError(
            f"[migrate] migration 应用失败,阻断启动: failed={result['failed']}"
        )
    return result


# ════════════════════════════════════════════════════════════════
# R72 P0-08: 结构化 migration CLI 入口
# 支持 --check (dry-run 验证) 和 --json (结构化 JSON 输出)
# ════════════════════════════════════════════════════════════════

def _get_backup_schema_version() -> str:
    """R72 P0-08: 安全获取 backup schema version(避免循环 import)。

    从 services.db_backup._BACKUP_SCHEMA_VERSION 获取,
    import 失败时返回 "unknown"(不阻断 migration evidence)。
    """
    try:
        from services.db_backup import _BACKUP_SCHEMA_VERSION
        return _BACKUP_SCHEMA_VERSION
    except Exception:
        return "unknown"


def _build_migration_evidence(
    result: dict[str, list[str]],
    *,
    check_mode: bool = False,
) -> dict[str, Any]:
    """R72 P0-08: 构建结构化 migration evidence。

    包含:
      - target_backend: "sqlite" (当前实现)
      - current_schema_version: 已应用 migration 数量
      - expected_schema_version: migration 文件总数
      - applied: 本次应用的 migration IDs
      - skipped: 已应用跳过的 migration IDs
      - failed: 失败的 migration IDs
      - pending: 未应用的 migration IDs
      - migration_checksums: 每个 migration 文件的 SHA-256
      - manifest_digest: release-manifest.json 的 digest (若存在)
      - final_status: "ok" / "pending" / "failed"
    """
    import hashlib as _hashlib

    migration_dir = _MIGRATIONS_DIR
    all_files = sorted(migration_dir.glob("*.sql")) if migration_dir.is_dir() else []
    expected_versions = [f.name for f in all_files]

    # 计算 checksums
    checksums: dict[str, str] = {}
    for mf in all_files:
        try:
            content = mf.read_bytes()
            checksums[mf.name] = "sha256:" + _hashlib.sha256(content).hexdigest()
        except OSError:
            checksums[mf.name] = "error:unreadable"

    # manifest digest
    manifest_digest = ""
    if _RELEASE_MANIFEST_PATH.is_file():
        try:
            manifest_content = _RELEASE_MANIFEST_PATH.read_bytes()
            manifest_digest = "sha256:" + _hashlib.sha256(manifest_content).hexdigest()
        except OSError:
            manifest_digest = "error:unreadable"

    applied_set = set(result.get("applied", []))
    skipped_set = set(result.get("skipped", []))
    failed_set = set(result.get("failed", []))
    applied_and_skipped = applied_set | skipped_set
    pending = [v for v in expected_versions if v not in applied_and_skipped]

    has_failures = bool(failed_set)
    has_pending = bool(pending) and not check_mode

    if has_failures:
        final_status = "failed"
    elif has_pending:
        final_status = "pending"
    else:
        final_status = "ok"

    return {
        "target_backend": "sqlite",
        "current_schema_version": len(applied_and_skipped),
        "expected_schema_version": len(expected_versions),
        "applied": sorted(applied_set),
        "skipped": sorted(skipped_set),
        "failed": sorted(failed_set),
        "pending": sorted(pending),
        "migration_checksums": checksums,
        "manifest_digest": manifest_digest,
        "ddl_version": _get_backup_schema_version(),
        "check_mode": check_mode,
        "final_status": final_status,
    }


def main() -> int:
    """R72 P0-08: migration CLI 入口 — 支持 --check 和 --json。

    用法:
      python -m database.migrate            # 应用所有 pending migration
      python -m database.migrate --check     # 检查是否有 pending migration(不应用)
      python -m database.migrate --json      # 输出结构化 JSON evidence
      python -m database.migrate --check --json  # 检查模式 + JSON 输出
    """
    parser = argparse.ArgumentParser(
        description=_i18n_t('services.migrate.s1')
    )
    parser.add_argument(
        "--check", action="store_true",
        help=_i18n_t('services.migrate.s2'),
    )
    parser.add_argument(
        "--json", action="store_true",
        help=_i18n_t('services.migrate.s3'),
    )
    args = parser.parse_args()

    try:
        # R72 P0-08 / RC57 fix: 将 --check 传递给 apply_migrations()。
        # 旧实现此处未传 check_mode,导致 --check 模式仍执行 DDL 应用,
        # 与运行中的 db_writer 进程争抢 SQLite WAL 写锁,导致 600s 超时。
        result = asyncio.run(apply_migrations(check_mode=args.check))
    except Exception as e:
        if args.json:
            evidence = {
                "target_backend": "sqlite",
                "final_status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "applied": [],
                "skipped": [],
                "failed": [],
                "pending": [],
                "check_mode": args.check,
            }
            print(_json.dumps(evidence, ensure_ascii=False, indent=2))
        else:
            print(f"[migrate] 迁移失败: {e}", file=sys.stderr)
        return 1

    evidence = _build_migration_evidence(result, check_mode=args.check)

    if args.json:
        # R72 P0-08: 纯 JSON 到 stdout,人类可读文本到 stderr
        print(_json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        print(
            f"[migrate] 迁移完成: applied={len(evidence['applied'])}, "
            f"skipped={len(evidence['skipped'])}, "
            f"failed={len(evidence['failed'])}, "
            f"pending={len(evidence['pending'])}, "
            f"status={evidence['final_status']}"
        )

    # exit code: 有失败或 pending (在非 check 模式下) 则失败
    if evidence["failed"]:
        return 1
    if evidence["pending"] and not args.check:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
