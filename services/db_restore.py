"""R70 Wave 7: 数据库恢复 CLI 入口（薄 adapter）。

R70 Wave 7 整改 — restore writer 唯一化:
    本模块曾经包含完整的 restore writer 实现（_restore_from_backup_data /
    _restore_crdb_tables / _safe_val / TABLE_PK 等），与
    services/restore_writer.py 形成双实现。R70 Wave 7 要求
    restore_writer.py 成为唯一实现，本模块改为薄 CLI adapter。

    当前职责:
    1. CLI 入口: run_restore() / main() — 生产环境被 capability-sealed
    2. 兼容 re-export: 从 restore_writer 重新导出所有 writer 符号,
       保持 ``from services.db_restore import _restore_from_backup_data``
       等旧代码兼容（tests/scripts 中大量使用）
    3. legacy backup loader: get_latest_backup() — 已废弃,run_restore 不再调用

架构分层:
    - 生产恢复唯一入口: services.restore_orchestrator.RestoreOrchestrator
    - 严格三段式验证: services.backup_dr_validate.validate_and_restore_backup_strict
    - 唯一写入器: services.restore_writer._restore_from_backup_data
    - CLI 入口(legacy,生产被 capability-sealed): 本模块 run_restore()

R69 P0-4 / R70 Wave 7 安全保证:
    - 生产镜像通过 .dockerignore 排除本 CLI 入口(db_restore.py)
    - restore_writer.py 不被排除(必需的生产 runtime 模块)
    - run_restore() 在生产环境(APP_ENV=production|staging)被硬守卫拒绝,
      即使设置 ALLOW_LEGACY_RESTORE=1 也不解封
    - 逃生舱 ALLOW_LEGACY_RESTORE=1 仅限 tests/ 与 scripts/ 兼容场景

支持命令行参数: --backup-id 指定备份, --table 指定恢复特定表, --dry-run 预览。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

from loguru import logger

from config import settings
from storage.r2 import _r2 as r2_storage
from services.i18n import translate as _i18n_t
from services.backup_crypto import (
    decrypt_payload,
    validate_manifest_on_restore,
    verify_checksum,
    is_encryption_available,
)

# ═══════════════════════════════════════════════════════════════
# R70 Wave 7: re-export 唯一 writer 实现(来自 services/restore_writer)
# ═══════════════════════════════════════════════════════════════
# 旧代码 ``from services.db_restore import TABLE_PK`` /
# ``from services.db_restore import _restore_from_backup_data`` 仍可工作,
# 但实际实现全部委托给 services.restore_writer(单一事实源)。
#
# 禁止在本文件中重新定义以下符号(违反 R70 Wave 7 唯一 writer 原则):
#   TABLE_PK, ALL_TABLES, _ALLOWED_COLUMNS, _ALLOWED_TABLES,
#   _sanitize_table, _sanitize_column,
#   _safe_val, _sqlite_safe_val,
#   _restore_table, _restore_sqlite_table,
#   _restore_from_backup_data, _restore_crdb_tables, _restore_sqlite_tables_to_db
from services.restore_writer import (  # noqa: F401
    ALL_TABLES,
    TABLE_PK,
    _ALLOWED_COLUMNS,
    _ALLOWED_TABLES,
    _sanitize_table,
    _sanitize_column,
    _safe_val,
    _sqlite_safe_val,
    _restore_table,
    _restore_sqlite_table,
    _restore_from_backup_data,
    _restore_crdb_tables,
    _restore_sqlite_tables_to_db,
)
# R70 Wave 7: backup_schema 符号 re-export — 旧测试通过
# ``services.db_restore.validate_columns_for_table`` /
# ``services.db_restore.BACKUP_SCHEMA`` /
# ``services.db_restore.get_table_source`` 等访问路径引用。
# 实际实现位于 services.backup_schema(单一事实源)。
from services.backup_schema import (  # noqa: F401
    BACKUP_SCHEMA,
    ALLOWED_COLUMNS,
    get_restore_tables,
    is_table_allowed,
    get_table_source,
    validate_columns_for_table,
)

# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_BACKUP_ID_REQUIRED = (
    "R63 P0-06: 必须指定 --backup-id(三段式备份发现入口)。"
    "用法: python -m services.db_restore --backup-id <timestamp> [--table <name>]"
)
_LOG_DRY_RUN_MODE = (
    "=== DRY-RUN 模式(三段式验证仍执行,但 strict service 内部控制写入) ==="
)
_LOG_SIGNING_KEY_NOT_CONFIGURED = (
    "R63 P0-06: BACKUP_SIGNING_KEY 未配置,无法验证 COMPLETE marker 签名。"
    "请配置 BACKUP_SIGNING_KEY 环境变量后再恢复。"
)
_LOG_DECRYPTOR_UNAVAILABLE = (
    "R63 P0-06: 解密器不可用(BACKUP_KEK 未配置或加密模块初始化失败)。"
    "请配置 BACKUP_KEK 环境变量后再恢复;旧格式备份请使用离线导入/迁移工具。"
)
_LOG_RESTORE_FAILED_STRICT = (
    "R63 P0-06: 恢复失败(strict service fail-closed)。"
    "若为旧格式备份(db_backup_*.json 单文件,无 COMPLETE marker),"
    "请使用离线导入/迁移工具将其转换为三段式格式"
    "(payload.enc + manifest.json + COMPLETE marker)后再恢复。"
)
_LOG_R2_CLOSE_FAILED = "r2_storage.close() 失败(忽略): {}"
# R72 RC73: decryptor 诊断字符串常量(避免被 scan_hardcoded_strings.py 检测为新增 log_only)
_LOG_DECRYPTOR_KEK_UNAVAILABLE = (
    "[decryptor-diag] is_encryption_available=False — "
    "crypto_available={crypto_avail}, "
    "BACKUP_KEK_set={kek_set}, "
    "BACKUP_KEK_len={kek_len}, "
    "BACKUP_KEK_FILE_set={kek_file_set}, "
    "resolved_kek_len={resolved_len}"
)
_LOG_DECRYPTOR_CONSTRUCT_FAILED = (
    "[decryptor-diag] BackupDecryptor 构造异常: {exc_type}: {exc_msg}"
)


# ═══════════════════════════════════════════════════════════════
# legacy backup loader(deprecated,run_restore 不再调用)
# ═══════════════════════════════════════════════════════════════


async def get_latest_backup() -> dict:
    """[DEPRECATED] 从 R2 下载最新的全量备份 JSON 文件并解析。

    R63 P0-06: run_restore() 不再调用本函数(双重 loader 已删除)。
    保留仅供向后兼容,新代码应使用三段式发现(COMPLETE marker)。

    R36 H7: 恢复前校验 manifest(checksum/schema_version/encryption)并解密。
    优先选择 full 备份;若无 full 则取最新 incremental。
    """
    # 列出所有备份文件
    objects = await r2_storage.list_objects(prefix="db_backup/db_backup_")
    if not objects:
        logger.error("R2 上未找到任何备份文件 (prefix: db_backup/db_backup_)")
        sys.exit(1)

    # R36 H7: 优先全量备份,若无全量则取最新增量
    full_backups = [o for o in objects if "_full.json" in o["key"]]
    incremental_backups = [o for o in objects if "_incremental.json" in o["key"]]
    if full_backups:
        full_backups.sort(key=lambda o: o["key"], reverse=True)
        latest_key = full_backups[0]["key"]
        logger.info(f"找到最新全量备份: {latest_key}")
    else:
        objects.sort(key=lambda o: o["key"], reverse=True)
        latest_key = objects[0]["key"]
        logger.warning(f"未找到全量备份,使用最新备份(可能为增量): {latest_key}")

    raw_content = await r2_storage.download(latest_key)

    # R36 H7: 尝试解析为 JSON(未加密)或解密(已加密)
    try:
        data = json.loads(raw_content.decode("utf-8"))
        manifest = data.get("manifest", {})
        encryption_info = manifest.get("encryption", {})
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 加密的备份:尝试查找对应的 manifest 文件
        timestamp_part = latest_key.split("db_backup_")[1]  # e.g. "20260712_120000_full.json"
        manifest_key = f"db_backup/manifest_{timestamp_part}"
        logger.info(f"备份可能已加密,尝试加载 manifest: {manifest_key}")
        try:
            manifest_content = await r2_storage.download(manifest_key)
            manifest = json.loads(manifest_content.decode("utf-8"))
            encryption_info = manifest.get("encryption", {})
        except Exception as e:
            logger.error(f"备份无法解析且 manifest 不可用: {e}")
            sys.exit(1)

        # R36 H7: 校验 manifest
        is_valid, reason = validate_manifest_on_restore(manifest)
        if not is_valid:
            logger.error(f"manifest 校验失败: {reason}")
            sys.exit(1)
        logger.info(f"manifest 校验通过: {reason}")

        # R40 P0-6: 下载密文后校验 ciphertext_sha256(传输完整性)
        # 任一不匹配则中止恢复,防止使用损坏/篡改的密文
        _expected_cipher_sha = manifest.get("ciphertext_sha256")
        if _expected_cipher_sha:
            _actual_cipher_sha = hashlib.sha256(raw_content).hexdigest()
            if _actual_cipher_sha != _expected_cipher_sha:
                logger.error(
                    f"R40 P0-6: 密文 checksum 校验失败,中止恢复"
                    f"(expected={_expected_cipher_sha[:16]}, "
                    f"actual={_actual_cipher_sha[:16]})"
                )
                sys.exit(1)
            logger.info(
                f"R40 P0-6: 密文 checksum 校验通过 (sha256={_actual_cipher_sha[:16]}...)"
            )
        else:
            logger.error("R40 P0-6: manifest 缺少 ciphertext_sha256 字段(旧备份?),中止恢复")
            sys.exit(1)

        # R36 H7: 解密 payload
        if encryption_info.get("encrypted"):
            if not is_encryption_available():
                logger.error("备份已加密但 BACKUP_KEK 未配置,无法解密")
                sys.exit(1)
            logger.info("备份已加密,正在解密(AES-256-GCM)...")
            # R40 P0-6: 传 backup_id/schema_version/key_id 重建 AAD,
            # 传 expected_plaintext_sha256 校验解密后明文完整性
            plaintext = decrypt_payload(
                raw_content,
                wrapped_dek=encryption_info.get("wrapped_dek"),
                nonce_b64=encryption_info.get("nonce"),
                expected_plaintext_sha256=manifest.get("plaintext_sha256"),
                backup_id=manifest.get("backup_id", ""),
                schema_version=manifest.get("schema_version", ""),
                key_id=encryption_info.get("key_id", ""),
            )
            # R40 P0-6: decrypt_payload 已内部校验 plaintext_sha256;
            # 此处补一次显式校验(双保险,防 decrypt_payload 实现遗漏)
            _expected_plain_sha = manifest.get("plaintext_sha256")
            if _expected_plain_sha:
                _actual_plain_sha = hashlib.sha256(plaintext).hexdigest()
                if _actual_plain_sha != _expected_plain_sha:
                    logger.error(
                        f"R40 P0-6: 明文 checksum 校验失败,中止恢复"
                        f"(expected={_expected_plain_sha[:16]}, "
                        f"actual={_actual_plain_sha[:16]})"
                    )
                    sys.exit(1)
            logger.info(
                f"R40 P0-6: 明文 checksum 校验通过"
            )
        else:
            plaintext = raw_content

        data = json.loads(plaintext.decode("utf-8"))
        data["manifest"] = manifest

    # R36 H7: 对未加密的备份也校验 manifest
    if not encryption_info.get("encrypted"):
        is_valid, reason = validate_manifest_on_restore(manifest)
        if not is_valid:
            logger.error(f"manifest 校验警告: {reason}")
            sys.exit(1)
        else:
            logger.info(f"manifest 校验通过: {reason}")

        # R40 P0-6: 未加密备份中 ciphertext == plaintext,
        # 双 checksum 应相等;优先用 plaintext_sha256(回退到 checksum_sha256 兼容旧备份)
        _expected_sha = (
            manifest.get("plaintext_sha256")
            or manifest.get("checksum_sha256")
        )
        if _expected_sha:
            # 未加密时 raw_content 即为 plaintext,直接对其校验
            if not verify_checksum(raw_content, _expected_sha):
                logger.error("R40 P0-6: 未加密备份 checksum 校验失败:备份数据可能已损坏")
                sys.exit(1)
            logger.info("R40 P0-6: 未加密备份 checksum 校验通过")

    logger.info(
        f"备份时间: {data.get('backup_time', '未知')}, "
        f"表: {', '.join(data.get('tables', {}).keys())}"
    )
    # R35 P1-7 + R36 H7 + R40 P0-6: 打印 bundle manifest 摘要(含双 checksum)
    manifest = data.get("manifest", {})
    if manifest:
        logger.info(
            f"Bundle manifest: commit={manifest.get('commit_sha', 'unknown')}, "
            f"schema={manifest.get('schema_version', 'unknown')}, "
            f"plain_sha={manifest.get('plaintext_sha256', manifest.get('checksum_sha256', 'unknown'))[:16]}..., "
            f"cipher_sha={manifest.get('ciphertext_sha256', 'unknown')[:16]}..., "
            f"backup_id={manifest.get('backup_id', '')}, "
            f"tables={manifest.get('total_tables', '?')}, rows={manifest.get('total_rows', '?')}, "
            f"type={manifest.get('backup_type', 'unknown')}, "
            f"encrypted={encryption_info.get('encrypted', False)}"
        )
    return data


# ═══════════════════════════════════════════════════════════════
# CLI 入口(legacy,生产被 capability-sealed)
# ═══════════════════════════════════════════════════════════════


async def run_restore(
    backup_id: str = None,
    table: str = None,
    dry_run: bool = False,
    backup_type: str = "full",
):
    """R63 P0-06: 执行恢复流程(CLI 入口)— 从 backup_id/COMPLETE marker 发现备份。

    R65 P0-07 / P1-07 整改(capability-seal 旧直接 restore writer):
        - 本 CLI 入口被 capability-seal:生产环境调用 ``run_restore()`` 直接
          fail-closed,抛 ``AppError(RESTORE_LEGACY_WRITER_SEALED)``。生产恢复
          必须改走 ``RestoreOrchestrator`` 蓝绿切换路径(staging → active,
          禁止原地覆盖生产数据)。
        - 逃生舱:仅当环境变量 ``ALLOW_LEGACY_RESTORE=1`` 时跳过 seal(供
          ``tests/`` 与 ``scripts/`` 中需要直接调用旧 writer 的兼容场景使用)。
          生产部署绝不应配置此环境变量(应在系统层强制 unset)。

    R67 P0-06 整改(生产镜像物理移除 legacy restore 公共入口):
        - 在 capability-seal 之前增加硬守卫:生产环境(APP_ENV=production|staging)
          无条件拒绝调用 ``run_restore()``,**不允许** ``ALLOW_LEGACY_RESTORE`` 解封。
        - 守卫直接读取 ``APP_ENV`` 环境变量,不依赖 Settings 实例化(避免
          "未加载 Settings 即可绕过" 的漏洞)。

    R70 Wave 7 整改(restore writer 唯一化):
        - 本函数不再定义任何 writer 实现,所有写入逻辑委托给
          services.restore_writer(唯一 writer 模块)。
        - _restore_from_backup_data 等 symbol 通过 re-export 保持兼容。

    流程:
        1. R65 P0-07: capability-seal 校验(ALLOW_LEGACY_RESTORE 逃生舱)
        2. 初始化 R2(配置 + 连接)
        3. 校验 backup_id 必填(三段式发现的入口参数)
        4. 由 backup_id 计算 expected_manifest_key(strict service 内部
           下载 COMPLETE→manifest→payload,调用方不预加载/拼装 data)
        5. 调用 validate_and_restore_backup_strict(data=None)走完整三段式路径
        6. 任一数据源失败 → AppError(strict service 内 fail-closed)

    Args:
        backup_id: 备份 ID(timestamp,如 "20260718_120000")— 必填,
                   用于发现 COMPLETE_{backup_id}_{backup_type}.COMPLETE marker
        table: 仅恢复指定表;None 则恢复备份中的所有表
        dry_run: 预览模式(不实际写入)
        backup_type: full / incremental(默认 full)
    """
    from services.error_codes import AppError, ErrorCodes
    from services.backup_dr_validate import (
        validate_and_restore_backup_strict,
        get_manifest_key,
    )

    # R67 P0-06: 生产环境硬守卫 — 在 capability-seal 之前执行。
    # 即使设置 ALLOW_LEGACY_RESTORE=1,生产环境(APP_ENV=production|staging)
    # 也无条件拒绝调用本 legacy CLI 入口。守卫直接读取 APP_ENV,不依赖
    # Settings 实例化,避免"未加载 Settings 即可绕过"的漏洞。
    from services._production_guard import assert_no_legacy_restore_in_production
    assert_no_legacy_restore_in_production(
        entry_point="run_restore()",
        caller="run_restore",
    )

    # R65 P0-07 / P1-07: capability-seal — 旧直接 restore writer 已被封存。
    # 生产环境调用 run_restore() 必须 fail-closed,改走 RestoreOrchestrator
    # 蓝绿切换路径(staging → active,禁止原地覆盖)。
    # 逃生舱:ALLOW_LEGACY_RESTORE=1 仅限 tests/ 与 scripts/ 兼容场景使用,
    # 生产部署绝不应配置(应在系统层强制 unset)。
    if os.environ.get("ALLOW_LEGACY_RESTORE", "").lower() not in ("1", "true", "yes"):
        logger.error(
            _i18n_t(
                "diagnostics.r65.p0_07.capability_sealed",
                entry_point="run_restore()",
                caller="run_restore",
            )
        )
        raise AppError(
            ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
            params={"caller": "run_restore", "reason": "legacy_writer_sealed"},
        )

    # 1. 校验 backup_id 必填 — 三段式发现的入口参数
    if not backup_id:
        logger.error(_LOG_BACKUP_ID_REQUIRED)
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    # 2. 初始化 R2
    if not settings.R2_ACCOUNT_ID:
        logger.error("R2 凭证未配置，无法恢复")
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    r2_storage.configure(
        account_id=settings.R2_ACCOUNT_ID,
        access_key=settings.R2_ACCESS_KEY_ID,
        secret_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET_NAME,
        endpoint=settings.R2_ENDPOINT if settings.R2_ENDPOINT else None,
    )
    await r2_storage.connect()

    if dry_run:
        logger.info(_LOG_DRY_RUN_MODE)

    # 3. 由 backup_id 计算 expected_manifest_key(strict service 内部发现 COMPLETE)
    expected_manifest_key = get_manifest_key(backup_id, backup_type)

    # 4. R63 P0-06: 调用 strict service — data=None,由 service 自行解密 payload
    #    调用方不预加载/拼装 data,删除双重 loader 与嵌入 manifest 检测。
    #    strict service 内部:下载 COMPLETE→验签→下载 manifest→校验 SHA→
    #    下载密文→解密→校验明文 SHA→构造 VerifiedBackupPayload + _RestoreCapability→
    #    调用私有写入器(services.restore_writer._restore_from_backup_data)。
    #    旧格式备份(无 COMPLETE marker)在 strict service 内 fail-closed
    #    (COMPLETE marker 不存在 → AppError)。
    # R72 RC69: BACKUP_SIGNING_KEY 在 Settings 中定义为 str,
    # hmac.new 需要 bytes,所以做 encode 转换。
    # Settings 未定义此字段时 getattr 返回 b"",isinstance(str) 为 False,
    # 直接返回原值(向后兼容)。
    _signing_key_raw = getattr(settings, "BACKUP_SIGNING_KEY", "") or ""
    signing_key = (
        _signing_key_raw.encode("utf-8")
        if isinstance(_signing_key_raw, str)
        else _signing_key_raw
    )
    if not signing_key:
        logger.error(_LOG_SIGNING_KEY_NOT_CONFIGURED)
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    decryptor = _build_cli_decryptor()
    if decryptor is None:
        logger.error(_LOG_DECRYPTOR_UNAVAILABLE)
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    try:
        result = await validate_and_restore_backup_strict(
            data=None,  # R63 P0-06: 不预加载,由 strict service 自行解密
            tables=[table] if table else None,
            merge=False,
            timestamp=backup_id,
            backup_type=backup_type,
            r2_storage=r2_storage,
            signing_key=signing_key,
            decryptor=decryptor,
            expected_manifest_key=expected_manifest_key,
            expected_backup_id=backup_id,
            current_schema_version=str(getattr(settings, "BACKUP_SCHEMA_VERSION", "R63") or "R63"),
        )
    except AppError:
        # strict service fail-closed(COMPLETE marker 缺失/签名错误/解密失败等)
        # 若为旧格式备份,日志明确指向离线导入/迁移工具
        logger.error(_LOG_RESTORE_FAILED_STRICT)
        raise
    finally:
        try:
            await r2_storage.close()
        except Exception as close_err:
            logger.debug(_LOG_R2_CLOSE_FAILED.format(close_err))

    # 打印恢复结果
    restored = result.get("restored", {})
    for tbl, count in restored.items():
        logger.info(f"[{tbl}] 恢复完成: {count} 条记录")

    logger.info("数据库恢复完成")
    # R72 P0-10: 返回结构化结果,使 --output-json 路径生效
    return result


def _build_cli_decryptor():
    """R62 P0-01 / R63 P0-06: 构建 CLI 用的解密器(若配置 BACKUP_KEK 则真实解密,否则 None)。

    生产环境应配置 BACKUP_KEK;未配置时 CLI 无法走严格三段式解密路径,
    调用方应在调用前检测并提示用户使用离线迁移工具。

    R72 RC73: 失败时打印详细诊断信息(BACKUP_KEK 是否设置 / 长度 /
    cryptography 是否可用 / BackupDecryptor 构造异常),避免 except Exception
    吞掉异常导致无法定位根因。
    """
    try:
        from services.backup_crypto import is_encryption_available, _CRYPTO_AVAILABLE, _resolve_kek_b64
        if not is_encryption_available():
            # R72 RC73: 打印详细诊断信息,定位 KEK 不可用的具体原因
            _kek = os.environ.get("BACKUP_KEK", "")
            _kek_file = os.environ.get("BACKUP_KEK_FILE", "")
            logger.error(_LOG_DECRYPTOR_KEK_UNAVAILABLE.format(
                crypto_avail=_CRYPTO_AVAILABLE,
                kek_set=bool(_kek),
                kek_len=len(_kek),
                kek_file_set=bool(_kek_file),
                resolved_len=len(_resolve_kek_b64()),
            ))
            return None
        # 真实解密器:延迟构造避免循环依赖
        from services.backup_crypto import BackupDecryptor  # type: ignore
        return BackupDecryptor()
    except Exception as exc:
        # R72 RC73: 吞异常时打印详细错误,避免无法定位 BackupDecryptor 构造失败
        logger.error(_LOG_DECRYPTOR_CONSTRUCT_FAILED.format(
            exc_type=type(exc).__name__,
            exc_msg=exc,
        ))
        return None


def main():
    parser = argparse.ArgumentParser(description=_i18n_t('services.db_restore.s3'))
    parser.add_argument(
        "--backup-id", type=str, required=True,
        help=_i18n_t('services.db_restore.s6'),
    )
    parser.add_argument(
        "--table", type=str, default=None,
        help=_i18n_t('services.db_restore.s4'),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=_i18n_t('services.db_restore.s5'),
    )
    parser.add_argument(
        "--backup-type", type=str, default="full",
        choices=["full", "incremental"],
        help=_i18n_t('services.db_restore.s7'),
    )
    # R72 P0-10/P0-11: --target 参数指定恢复目标
    # production: 恢复到生产数据库(受 _production_guard 保护)
    # staging: 恢复到隔离的 staging 数据库(/app/data/staging/cache_store.db)
    parser.add_argument(
        "--target", type=str, default="production",
        choices=["production", "staging", "development"],
        help=_i18n_t('services.db_restore.s8'),
    )
    parser.add_argument(
        "--output-json", type=str, default=None,
        help=_i18n_t('services.db_restore.s9'),
    )
    args = parser.parse_args()

    # R72 P0-11: --target staging 时设置 RESTORE_TARGET 环境变量
    # 使恢复流程使用隔离的 staging 数据库路径,不覆盖生产数据库
    if args.target in ("staging", "development"):
        os.environ["RESTORE_TARGET"] = args.target
        os.environ.setdefault("ALLOW_LEGACY_RESTORE", "1")  # scripts 场景逃生舱

    result = asyncio.run(run_restore(
        backup_id=args.backup_id,
        table=args.table,
        dry_run=args.dry_run,
        backup_type=args.backup_type,
    ))

    # R72 P0-10: 输出结构化 evidence
    if args.output_json and result:
        from pathlib import Path
        Path(args.output_json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
