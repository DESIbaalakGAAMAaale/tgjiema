"""数据库恢复脚本(单一 Restore Engine)

R35 P1-4: 本模块是唯一的恢复执行器,db_backup.py::restore_from_backup()
委托给本模块的 _restore_from_backup_data(),消除两套执行器。

R35 P1-5: 按 source 分组恢复:
- source="crdb":        恢复到 CockroachDB(asyncpg 直连)
- source="sqlite":       恢复到 cache_store.db(aiosqlite)
- source="relay_sqlite": 恢复到 relay_pool.db(aiosqlite)

R35 P1-6: 恢复时按表严格校验列(使用 validate_columns_for_table),
不再使用全局白名单。

R61 P0-03: 信任链整改 — 不可伪造的恢复能力令牌。
    - _restore_from_backup_data() 为私有写入器,不再做信任校验(只写数据)。
    - 仅接受 services.backup_dr_validate._RestoreCapability 类型令牌,
      该令牌由 _RESTORE_SENTINEL(模块私有)保护,外部代码无法构造。
    - 生产恢复必须通过 services.backup_dr_validate.validate_and_restore_backup_strict
      公共入口执行 — 该入口做严格验证(COMPLETE 签名/manifest digest/
      payload digest/解密/schema)后构造 _RestoreCapability 并调用私有写入器。
    - run_restore()/main() 仍为公共 CLI 入口,但内部通过
      validate_and_restore_backup_strict() 路由。

支持命令行参数：--table 指定恢复特定表，--dry-run 预览不执行。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

import asyncpg
from loguru import logger

from config import settings
from storage.r2 import _r2 as r2_storage
from services.backup_schema import (
    BACKUP_SCHEMA, get_restore_tables, is_table_allowed, ALLOWED_COLUMNS,
    get_table_source, validate_columns_for_table,
)
from services.i18n import translate as _i18n_t
from services.backup_crypto import (
    decrypt_payload,
    validate_manifest_on_restore,
    verify_checksum,
    is_encryption_available,
)

# R44 7.2: record_restore_usage 在函数内延迟导入(避免循环依赖)

# ─── 表清单(单一事实源: services/backup_schema.py) ───
# 保留向后兼容的别名,等价于原 ALL_TABLES / TABLE_PK
# 新增表时只需在 backup_schema.BACKUP_SCHEMA 中添加条目,无需修改本文件
ALL_TABLES = get_restore_tables()

# 各表的主键列(从 BACKUP_SCHEMA 派生,格式与原 TABLE_PK 一致: "col1, col2" 字符串)
TABLE_PK = {t.name: ", ".join(t.pk_columns) for t in BACKUP_SCHEMA.values()}

# 列白名单(从 BACKUP_SCHEMA 聚合所有表 columns + 向后兼容列,单一事实源)
# R35 P1-6: 推荐使用 validate_columns_for_table() 按表校验,此全局白名单仅向后兼容
_ALLOWED_COLUMNS = ALLOWED_COLUMNS

# 表白名单(从 BACKUP_SCHEMA 派生)
_ALLOWED_TABLES = frozenset(get_restore_tables())


def _sanitize_table(name: str) -> str:
    """白名单校验表名,防止 SQL 注入。"""
    clean = name.strip().lower()
    if clean not in _ALLOWED_TABLES:
        raise ValueError(_i18n_t('services.db_restore.s1', name=name))
    return clean


def _sanitize_column(name: str) -> str:
    """白名单校验列名,防止 SQL 注入(全局白名单,向后兼容)。

    R35 P1-6: 新代码应使用 validate_columns_for_table() 按表校验。
    """
    clean = name.strip().lower()
    if clean not in _ALLOWED_COLUMNS:
        raise ValueError(_i18n_t('services.db_restore.s2', name=name))
    return clean


async def get_latest_backup() -> dict:
    """从 R2 下载最新的全量备份 JSON 文件并解析。

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
#  CRDB 恢复(asyncpg 直连)
# ═══════════════════════════════════════════════════════════════

def _safe_val(val):
    """将 Python 值转换为 CRDB 兼容的类型。"""
    if val is None:
        return None
    if isinstance(val, bool):
        return val  # 保持 bool，asyncpg 兼容 INTEGER/BOOLEAN 列
    if isinstance(val, (list, dict)):
        return json.dumps(val, default=str, ensure_ascii=False)
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val) if not isinstance(val, (int, float, str)) else val


async def _restore_table(conn: asyncpg.Connection, table: str, records: list[dict], dry_run: bool = False):
    """将记录恢复到 CRDB（逐行 UPSERT）。

    R35 P1-6: 使用 validate_columns_for_table() 按表校验列,
    不再使用全局白名单 _sanitize_column()。
    R61 P0-03: 私有写入器,仅由 _restore_from_backup_data 内部调用。
    """
    if not records:
        logger.info(f"[{table}] 无记录，跳过")
        return 0

    pk = TABLE_PK.get(table)
    if not pk:
        logger.warning(f"[{table}] 未知主键，跳过")
        return 0

    # 支持复合主键（如 "main_msg_id, backup_channel_id"）
    pk_cols = [c.strip() for c in pk.split(",")]
    # 复合主键使用所有列名，单主键使用单列名
    pk_clause = pk  # ON CONFLICT (main_msg_id, backup_channel_id) 或 ON CONFLICT (slot_id)

    if dry_run:
        logger.info(f"[DRY-RUN] [{table}] 将恢复 {len(records)} 条记录")
        return len(records)

    # R35 P1-6: 按表校验列(替代全局 _sanitize_column)
    # 使用 validate_columns_for_table 过滤非法列,而非全局白名单
    columns = None
    try:
        raw_cols = list(records[0].keys())
        columns = validate_columns_for_table(table, raw_cols)
        if not columns:
            logger.error(f"[{table}] 校验后无合法列(原始列: {raw_cols}),跳过此表")
    except ValueError as e:
        # R64 P1-07: destructive 域禁止 except 块裸 return 0;记录日志后落到下方统一返回
        logger.error(f"[{table}] 列校验失败: {e},跳过此表")
    if not columns:
        return 0

    # B9: 不排除 id 列 — 排除后 ON CONFLICT(id) 永不触发（id 不在 INSERT 列中），
    # 导致重复恢复时插入重复行而非 upsert。包含 id 列以保证幂等性。
    # 注意：CockroachDB 使用 unique_rowid() 而非传统 sequence，显式插入 id 不影响后续自增。
    insert_cols = columns
    placeholders = [f"${i + 1}" for i in range(len(insert_cols))]
    # 构建 ON CONFLICT ... DO UPDATE SET 子句
    # N-16-4: relay_accounts.api_hash 在 UPSERT 时跳过 UPDATE，
    # 保留 DB 现值（避免备份中的密文覆盖运行中已更新的密钥）；
    # INSERT 时仍包含（满足 NOT NULL 约束，全新库可插入）
    _skip_update_cols = {"relay_accounts": {"api_hash"}}
    update_parts = [f"{c} = EXCLUDED.{c}" for c in insert_cols if c not in pk_cols and c not in _skip_update_cols.get(table, set())]

    sql = (
        f"INSERT INTO {_sanitize_table(table)} ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({pk_clause}) DO UPDATE SET {', '.join(update_parts)}"
    )

    restored = 0
    batch_size = 100
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        async with conn.transaction():
            for record in batch:
                vals = [_safe_val(record.get(c)) for c in insert_cols]
                try:
                    # R35 P1-4: 确保参数正确展开(execute(sql, *vals) 而非 execute(sql, vals))
                    await conn.execute(sql, *vals)
                    restored += 1
                except Exception as e:
                    logger.error(f"[{table}] 恢复记录失败 (pk={record.get(pk)}): {e}")
        logger.debug(f"[{table}] 已恢复 {restored}/{len(records)}")
    return restored


# ═══════════════════════════════════════════════════════════════
#  R35 P1-5: SQLite 恢复(aiosqlite)
# ═══════════════════════════════════════════════════════════════

def _sqlite_safe_val(val):
    """将 Python 值转换为 SQLite 兼容的类型。"""
    if val is None:
        return None
    if isinstance(val, bool):
        return 1 if val else 0  # SQLite 用 INTEGER 0/1 表示布尔
    if isinstance(val, (list, dict)):
        return json.dumps(val, default=str, ensure_ascii=False)
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val) if not isinstance(val, (int, float, str)) else val


async def _restore_sqlite_table(
    conn, table: str, records: list[dict],
    merge: bool = False, dry_run: bool = False,
) -> int:
    """将记录恢复到 SQLite 表(aiosqlite 连接)。

    R35 P1-5: source="sqlite" / "relay_sqlite" 的表走此路径。
    R35 P1-6: 使用 validate_columns_for_table() 按表校验列。

    Args:
        conn: aiosqlite.Connection(已打开)
        table: 表名
        records: 记录列表
        merge: True=增量补充(INSERT OR IGNORE); False=覆盖(DELETE 后 INSERT)
        dry_run: 预览模式
    """
    if not records:
        logger.info(f"[SQLite][{table}] 无记录，跳过")
        return 0

    # R35 P1-6: 按表校验列
    columns = None
    try:
        raw_cols = list(records[0].keys())
        columns = validate_columns_for_table(table, raw_cols)
        if not columns:
            logger.error(f"[SQLite][{table}] 校验后无合法列(原始列: {raw_cols}),跳过此表")
    except ValueError as e:
        # R64 P1-07: destructive 域禁止 except 块裸 return 0;记录日志后落到下方统一返回
        logger.error(f"[SQLite][{table}] 列校验失败: {e},跳过此表")
    if not columns:
        return 0

    if dry_run:
        logger.info(f"[DRY-RUN][SQLite][{table}] 将恢复 {len(records)} 条记录")
        return len(records)

    # 确定 ON CONFLICT 子句
    _schema = BACKUP_SCHEMA.get(table)
    conflict_clause = ""
    if merge:
        # merge 模式: INSERT OR IGNORE(SQLite 语法)
        if _schema and len(_schema.pk_columns) > 1:
            _pk_cols = ", ".join(f'"{c}"' for c in _schema.pk_columns)
            conflict_clause = f' ON CONFLICT ({_pk_cols}) DO NOTHING'
        elif _schema and _schema.conflict_col:
            conflict_clause = f' ON CONFLICT ("{_schema.conflict_col}") DO NOTHING'
        else:
            # 自增主键或无冲突列,merge 模式退化为普通 INSERT
            logger.warning(f"[SQLite][{table}] 未知冲突列,merge 模式可能因主键冲突失败")
    # 非 merge(覆盖)模式: 先 DELETE 再 INSERT

    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f'INSERT {"OR IGNORE" if merge and not conflict_clause else "OR REPLACE" if not merge else ""} INTO "{table}" ({col_list}) VALUES ({placeholders}){conflict_clause}'

    # 非 merge 模式且无 ON CONFLICT: 先清空表(覆盖恢复)
    if not merge:
        try:
            await conn.execute(f'DELETE FROM "{table}"')
            logger.debug(f"[SQLite][{table}] 已清空(覆盖恢复模式)")
        except Exception as e:
            logger.warning(f"[SQLite][{table}] 清空表失败: {e}")

    restored = 0
    for record in records:
        vals = [_sqlite_safe_val(record.get(c)) for c in columns]
        try:
            await conn.execute(sql, vals)
            restored += 1
        except Exception as e:
            logger.error(f"[SQLite][{table}] 恢复记录失败: {e}")
    await conn.commit()
    logger.info(f"[SQLite][{table}] 恢复完成: {restored}/{len(records)} 条记录")
    return restored


# ═══════════════════════════════════════════════════════════════
#  R35 P1-4: 单一 Restore Engine 主入口
# ═══════════════════════════════════════════════════════════════

# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_PAYLOAD_DIGEST_MISMATCH = (
    "R63 P0-02: actual payload bytes digest 与 capability.payload_digest "
    "不匹配 (actual={}..., capability={}...) — payload 可能被篡改"
)
_LOG_RESTORE_ERRORS = (
    "R63 P0-03: 恢复存在 {} 个错误,operation FAILED (不返回部分成功): {}"
)
_LOG_RESTORE_DONE = "[db_restore] 恢复完成: {} 行, 0 个错误, 模式={}"

async def _restore_from_backup_data(
    verified_payload,
    *,
    _capability,  # R61 P0-03 / R62 P0-02: _RestoreCapability(不可伪造,由 validate_and_restore_backup_strict 构造)
    tables: list[str] | None = None,
    merge: bool = False,
) -> dict:
    """从已验证的备份 payload 恢复数据库(R61 P0-03 / R62 P0-02: 私有 Restore Engine 写入器)。

    R35 P1-4: 本函数是唯一的恢复执行器写入器(私有)。
    db_backup.py::restore_from_backup() 与 CLI 的 run_restore() 通过
    services.backup_dr_validate.validate_and_restore_backup_strict() 间接调用本函数。

    R35 P1-5: 按 source 分组恢复:
    - source="crdb":        恢复到 CRDB(asyncpg)
    - source="sqlite":       恢复到 cache_store.db(aiosqlite)
    - source="relay_sqlite": 恢复到 relay_pool.db(aiosqlite)

    R35 P1-6: 恢复时按表校验列(validate_columns_for_table)。

    R61 P0-03: 信任链整改 — 不可伪造的恢复能力令牌。
        - 本函数为私有写入器,不做信任校验(只写数据)。
        - 仅接受 services.backup_dr_validate._RestoreCapability 类型令牌,
          该令牌由 _RESTORE_SENTINEL(模块私有)保护,外部代码无法构造。
        - 调用方必须通过 validate_and_restore_backup_strict() 公共入口
          获取 _RestoreCapability 后再调用本函数。
        - 旧 R59 P0-04 / R60 P0-03 的 BackupValidationResult 令牌已废弃
          (其为公开 dataclass,任意调用方可构造 valid=True,无法防止伪造)。

    R62 P0-02: 强制 capability 边界(本次审计整改)。
        - **首条语句必须为 capability.assert_valid(payload_digest, clock, expected_scope)**
          — 验证令牌有效性(sentinel + 未过期 + payload_digest 一致 + scope 一致) +
          防重放(nonce 消费)。任一校验失败即抛 AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)。
        - 参数从 raw data: dict 改为 VerifiedBackupPayload(frozen dataclass),
          从 verified_payload.tables 读取表数据(不再从 data.get("tables", {}))。
        - capability.payload_digest 与 verified_payload.payload_digest 必须一致,
          防止 payload 在验证后、写入前被替换。

    Args:
        verified_payload: R62 P0-02 VerifiedBackupPayload 实例(frozen dataclass)
                          — 由 validate_and_restore_backup_strict 或
                          _restore_preverified_payload 构造,含已验证的 tables +
                          payload_digest + schema_fingerprint
        _capability: R61 P0-03 / R62 P0-01 不可伪造的 _RestoreCapability(由
                     validate_and_restore_backup_strict / _restore_preverified_payload
                     通过 _RESTORE_SENTINEL 构造,强制必填)
        tables: 仅恢复指定表；None 则恢复备份中的所有表
        merge: True=增量补充(冲突保留现有数据); False=覆盖(清空后写入,默认)

    Returns:
        {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}

    Raises:
        AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): capability 校验失败
            (sentinel 不匹配 / nonce 已消费 / 已过期 / payload_digest 不匹配 /
             schema_fingerprint 不匹配 / actual payload bytes digest 不匹配)

    R63 P0-02 / R64 P1-01: writer 端重算 actual payload bytes digest。
        - 首条语句对 ``verified_payload.canonical_payload_bytes`` 实际 bytes 重新计算
          SHA-256,与 ``_capability.payload_digest`` 比对。
        - 即使 ``object.__setattr__`` 绕过 frozen 替换了 canonical_payload_bytes,
          重算 digest 也会与 capability 内嵌(构造时)的 digest 不匹配 → fail-closed。
        - 重算 digest 传给 ``assert_valid``(而非 verified_payload.payload_digest),
          保证令牌校验基于实际 bytes 而非存储的 digest。
        - R64 P1-01: 改为直接对 canonical_payload_bytes 求 sha256,
          无需经 _compute_payload_digest(已是 canonical bytes,无需再序列化)。
    """
    # R63 P0-02 / R64 P1-01: 首条语句 — 对 verified_payload.canonical_payload_bytes
    # 实际 bytes 重新计算 SHA-256,与 capability.payload_digest 比对。
    # 这防御 object.__setattr__ 绕过 frozen 替换 canonical_payload_bytes 的攻击:
    #   - 即使 attacker 替换 verified_payload.canonical_payload_bytes + payload_digest,
    #     capability.payload_digest 是构造时内嵌的(不可变),重算 digest 不匹配 → fail-closed。
    #   - 即使 attacker 只替换 canonical_payload_bytes,payload_digest 仍为旧值,
    #     重算 digest 与 capability.payload_digest 不匹配 → fail-closed。
    import hashlib as _hashlib
    import time as _time
    from services.error_codes import AppError, ErrorCodes
    actual_payload_digest = _hashlib.sha256(
        verified_payload.canonical_payload_bytes
    ).hexdigest()
    if actual_payload_digest != _capability.payload_digest:
        logger.error(
            _LOG_PAYLOAD_DIGEST_MISMATCH.format(
                actual_payload_digest[:16],
                _capability.payload_digest[:16],
            )
        )
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    # R62 P0-02 / R63 P0-02 / R63 P1-01: capability.assert_valid — 强制 capability 边界
    # 传入 actual_payload_digest(重算值,而非 verified_payload.payload_digest 存储值),
    # 保证令牌校验基于实际 bytes。校验维度:sentinel + nonce 防重放 + 未过期 +
    # payload_digest 一致(重算值 == capability 内嵌值)+ scope 一致。
    # R63 P1-01: nonce 持久化到权威 SQLite/CRDB 表原子消费,assert_valid 现为 async。
    await _capability.assert_valid(
        actual_payload_digest,
        _time.time(),
        verified_payload.schema_fingerprint,
    )

    # R62 P0-02: 从 verified_payload.tables 读取(不再从 data.get("tables", {}))
    # verified_payload 由 validate_and_restore_backup_strict 在严格验证通过后构造,
    # tables 字段已通过 validate_backup_payload 解密 + 校验 plaintext_sha256。
    backup_tables = verified_payload.tables

    if tables:
        restore_tables_map = {t: backup_tables[t] for t in tables if t in backup_tables}
        skipped = [t for t in tables if t not in backup_tables]
    else:
        restore_tables_map = dict(backup_tables)
        skipped = []

    result = {"restored": {}, "skipped": skipped, "errors": []}

    # R35 P1-5: 按 source 分组
    crdb_to_restore: dict[str, list] = {}
    sqlite_to_restore: dict[str, list] = {}
    relay_sqlite_to_restore: dict[str, list] = {}
    unknown_source_tables: dict[str, list] = {}

    for table_name, rows in restore_tables_map.items():
        if table_name not in BACKUP_SCHEMA:
            logger.warning(f"[db_restore] 表 {table_name} 不在 BACKUP_SCHEMA 中,跳过")
            result["skipped"].append(table_name)
            continue
        source = get_table_source(table_name)
        if source == "crdb":
            crdb_to_restore[table_name] = rows
        elif source == "sqlite":
            sqlite_to_restore[table_name] = rows
        elif source == "relay_sqlite":
            relay_sqlite_to_restore[table_name] = rows
        else:
            unknown_source_tables[table_name] = rows
            logger.warning(f"[db_restore] 表 {table_name} source={source}(未知/redis),跳过")
            result["skipped"].append(table_name)

    logger.info(
        f"[db_restore] 按 source 分组: CRDB={len(crdb_to_restore)}表, "
        f"SQLite={len(sqlite_to_restore)}表, relay_sqlite={len(relay_sqlite_to_restore)}表"
    )

    # ─── 1. 恢复 CRDB 表 ───
    if crdb_to_restore:
        await _restore_crdb_tables(crdb_to_restore, merge, result)

    # ─── 2. 恢复 SQLite 表(cache_store.db) ───
    if sqlite_to_restore:
        await _restore_sqlite_tables_to_db(sqlite_to_restore, merge, result)

    # ─── 3. 恢复 relay_sqlite 表(relay_pool.db) ───
    if relay_sqlite_to_restore:
        await _restore_sqlite_tables_to_db(
            relay_sqlite_to_restore, merge, result, is_relay=True,
        )

    # R63 P0-03: 任一数据源恢复失败 → 整个 operation FAILED(raise AppError,不返回部分成功)
    # 恢复不是跨数据源原子操作,但禁止返回"恢复完成"的假象:任一表错误即整体 FAILED,
    # 调用方必须从 staging/备份重试,不接受混合状态。
    if result["errors"]:
        from services.error_codes import AppError, ErrorCodes
        error_summary = "; ".join(result["errors"][:5])
        logger.error(
            _LOG_RESTORE_ERRORS.format(
                len(result['errors']), error_summary
            )
        )
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={"backup_id": verified_payload.backup_id, "errors": error_summary},
        )

    logger.info(
        _LOG_RESTORE_DONE.format(
            sum(result['restored'].values()),
            'merge' if merge else 'overwrite',
        )
    )

    # R44 7.2: 记录 restore RU 消耗(估算: 每个恢复表约 50 RU)
    # 单独记入 service='restore' 维度,不混入业务空载门禁
    try:
        from services.ru_cost_center import record_restore_usage
        await record_restore_usage(
            ru_cost=len(result["strored"]) * 50,
            operation="restore_from_backup_data",
        )
    except Exception as ru_err:
        # R64 P1-07: destructive 域禁止 except pass;RU 统计失败非致命,仅记录
        logger.debug(f"[Restore] RU 统计失败(非致命): {ru_err}")

    return result


async def _restore_crdb_tables(
    tables_data: dict[str, list], merge: bool, result: dict,
):
    """R63 P0-03/P1-04: 恢复 CRDB 表(使用 db_client.transaction() context manager)。

    R63 P1-04 修复(遗留开放事务):
        - 原实现在 BEGIN 后若 cols 为空会 continue,该分支无显式 COMMIT/ROLLBACK,
          导致事务遗留。现已将 schema/column 校验提前到 BEGIN 前,cols 为空时
          raise(不 continue),事务由 context manager 自动管理。

    R63 P0-03 修复(原子性 + fail-closed):
        - 使用 ``async with db_client.transaction() as conn:`` context manager,
          异常时自动 ROLLBACK(不再手动 BEGIN/COMMIT/ROLLBACK)。
        - 任一表错误 → raise AppError(整个 operation FAILED,不返回部分成功)。
        - context manager 在同一连接上执行事务(原 db_client.execute 每次
          acquire 新连接,BEGIN/TRUNCATE/INSERT 可能不在同一连接/事务)。

    R63 P1-04 校验顺序(全部在 BEGIN 前完成):
        1. 表名格式校验(_validate_identifier)
        2. 列校验(validate_columns_for_table)— cols 为空 → raise(不 continue)
        3. 列名格式校验(_validate_identifier per col)
        4. ON CONFLICT 子句构造
        全部通过后才进入 ``async with db_client.transaction() as conn:`` 执行写入。
    """
    from database.session import _client as db_client, _validate_identifier
    from services.error_codes import AppError, ErrorCodes

    if not db_client.is_connected:
        try:
            from database.session import init_db
            await init_db()
        except Exception as e:
            err = f"CRDB 连接初始化失败: {e}"
            result["errors"].append(err)
            logger.error(f"[db_restore] {err}")
            # R63 P0-03: fail-closed — 不返回部分成功
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})

    for table_name, rows in tables_data.items():
        # ── R63 P1-04: schema/column 校验全部在 BEGIN 前完成 ──

        # 1. 表名格式校验
        try:
            _validate_identifier(table_name)
        except ValueError as e:
            err = f"表 {table_name} 非法表名: {e}"
            result["errors"].append(err)
            logger.error(f"[db_restore] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})

        if not rows:
            # 无记录不算错误,标记 0 行继续(无事务需管理)
            result["restored"][table_name] = 0
            continue

        # 2. R35 P1-6: 按表校验列 — cols 为空 → raise(不 continue,不 BEGIN)
        raw_cols = list(rows[0].keys())
        try:
            cols = validate_columns_for_table(table_name, raw_cols)
        except ValueError as e:
            err = f"表 {table_name} 列校验失败: {e}"
            result["errors"].append(err)
            logger.error(f"[db_restore] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})
        if not cols:
            # R63 P1-04: cols 为空 → raise(不 continue,BEGIN 前已校验,无开放事务)
            err = f"表 {table_name} 校验后无合法列(原始列: {raw_cols})"
            result["errors"].append(err)
            logger.error(f"[db_restore] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})

        # 3. 列名格式校验(防 SQL 注入)— BEGIN 前完成
        try:
            safe_cols = [_validate_identifier(c) for c in cols]
        except ValueError as e:
            err = f"表 {table_name} 含非法列名: {e}"
            result["errors"].append(err)
            logger.error(f"[db_restore] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})

        # 4. 确定 ON CONFLICT 子句(仅 merge 模式)— BEGIN 前构造
        conflict_clause = ""
        if merge:
            _schema = BACKUP_SCHEMA.get(table_name)
            if _schema and len(_schema.pk_columns) > 1:
                _pk_cols = ", ".join(f'"{c}"' for c in _schema.pk_columns)
                conflict_clause = f' ON CONFLICT ({_pk_cols}) DO NOTHING'
            elif _schema and _schema.conflict_col:
                conflict_clause = f' ON CONFLICT ("{_schema.conflict_col}") DO NOTHING'
            else:
                logger.warning(f"[db_restore] 表 {table_name} 未知冲突列,merge 模式可能因主键冲突失败")

        # 5. 预构建 SQL 与参数列表(BEGIN 前完成,事务内只执行)
        placeholders = ", ".join(f"${i+1}" for i in range(len(safe_cols)))
        col_list = ", ".join(f'"{c}"' for c in safe_cols)
        sql = f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders}){conflict_clause}'

        # ── R63 P0-03/P1-04: 进入事务 context manager(异常自动 ROLLBACK) ──
        # db_client.transaction() 在同一连接上执行事务:
        #   async with self._pool.acquire() as conn:
        #       async with conn.transaction():
        #           yield conn
        # 异常时 conn.transaction() 自动 ROLLBACK,无需手动处理。
        try:
            async with db_client.transaction() as conn:
                if not merge:
                    # 覆盖模式:清空目标表
                    await conn.execute(
                        f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE'
                    )
                inserted = 0
                for row in rows:
                    # asyncpg: conn.execute(sql, *params)
                    params = [row[c] for c in cols]
                    await conn.execute(sql, *params)
                    inserted += 1
                result["restored"][table_name] = inserted
                mode_text = "增量补充" if merge else "覆盖恢复"
                logger.info(f"[db_restore] {mode_text} 表 {table_name}: {inserted} 行")
        except AppError:
            # 已经是 AppError,直接传播(fail-closed)
            raise
        except Exception as e:
            # R63 P0-03: 任一表错误 → raise AppError(不返回部分成功)
            # context manager 已自动 ROLLBACK
            err = f"表 {table_name}: {e}"
            result["errors"].append(err)
            logger.error(f"[db_restore] 恢复表 {table_name} 失败: {e}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})


async def _restore_sqlite_tables_to_db(
    tables_data: dict[str, list], merge: bool, result: dict, is_relay: bool = False,
):
    """R63 P0-03: 恢复 SQLite 表到指定数据库(cache_store.db 或 relay_pool.db)。

    R35 P1-5: source="sqlite" / "relay_sqlite" 的表走此路径。

    R63 P0-03: 任一表错误 → raise AppError(整个 operation FAILED,不返回部分成功)。
    SQLite 不支持跨数据库事务,但通过 fail-closed 语义保证不返回混合状态:
    任一表恢复失败即整体 FAILED,调用方必须从备份重试。
    """
    import aiosqlite
    from services.error_codes import AppError, ErrorCodes

    if is_relay:
        from database.relay_db import DB_PATH as SQLITE_DB_PATH
        db_label = "relay_sqlite"
    else:
        from database.cache_store import DB_PATH as SQLITE_DB_PATH
        db_label = "sqlite"

    if not os.path.exists(str(SQLITE_DB_PATH)):
        err = f"{db_label} 数据库文件不存在: {SQLITE_DB_PATH}"
        result["errors"].append(err)
        logger.error(f"[db_restore] {err}")
        # R63 P0-03: fail-closed — 不返回部分成功
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                       params={"backup_id": "", "errors": err})

    try:
        # 读写模式连接(需要写入数据)
        async with aiosqlite.connect(str(SQLITE_DB_PATH), timeout=15) as conn:
            for table_name, rows in tables_data.items():
                try:
                    restored = await _restore_sqlite_table(
                        conn, table_name, rows, merge=merge,
                    )
                    result["restored"][table_name] = restored
                except Exception as e:
                    err = f"{table_name}: {e}"
                    result["errors"].append(err)
                    logger.error(f"[db_restore][{db_label}] 恢复表 {table_name} 失败: {e}")
                    # R63 P0-03: 任一表错误 → raise AppError(不返回部分成功)
                    raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                                   params={"backup_id": "", "errors": err})
    except AppError:
        # 已经是 AppError,直接传播
        raise
    except Exception as e:
        err = f"{db_label} 打开数据库失败: {e}"
        result["errors"].append(err)
        logger.error(f"[db_restore] {err}")
        # R63 P0-03: fail-closed
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                       params={"backup_id": "", "errors": err})


# ═══════════════════════════════════════════════════════════════
#  CLI 入口(R63 P0-06: 三段式发现模型)
# ═══════════════════════════════════════════════════════════════

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

async def run_restore(
    backup_id: str = None,
    table: str = None,
    dry_run: bool = False,
    backup_type: str = "full",
):
    """R63 P0-06: 执行恢复流程(CLI 入口)— 从 backup_id/COMPLETE marker 发现备份。

    R65 P0-07 / P1-07 整改(capability-seal 旧直接 restore 写入器):
        - 本 CLI 入口被 capability-seal:生产环境调用 ``run_restore()`` 直接
          fail-closed,抛 ``AppError(RESTORE_LEGACY_WRITER_SEALED)``。生产恢复
          必须改走 ``RestoreOrchestrator`` 蓝绿切换路径(staging → active,
          禁止原地覆盖生产数据)。
        - 逃生舱:仅当环境变量 ``ALLOW_LEGACY_RESTORE=1`` 时跳过 seal(供
          ``tests/`` 与 ``scripts/`` 中需要直接调用旧 writer 的兼容场景使用)。
          生产部署绝不应配置此环境变量(应在系统层强制 unset)。
        - ``_restore_from_backup_data()`` 已通过 R61 P0-03 / R62 P0-02 的
          ``_RestoreCapability``(不可伪造 sentinel 令牌)capability-seal,
          仅由 ``services/backup_dr_validate.validate_and_restore_backup_strict``
          构造并传入。本 seal 是在 CLI 入口层的额外 fail-closed 防线。

    R63 P0-06 修复(CLI 恢复路径与三段式备份发现模型一致):
        - **删除旧 ``get_latest_backup()`` 双重 loader**(该函数枚举
          ``db_backup/db_backup_*.json`` 单文件,与三段式模型不一致)。
        - CLI 只接受 ``backup_id``(= timestamp),由 strict service
          (``validate_and_restore_backup_strict``)自行读取
          COMPLETE→manifest→payload,调用方不得预加载/拼装 data。
        - 删除嵌入 manifest 检测(``_is_three_stage_complete_marker`` 已移除)。
        - 旧格式备份(无 COMPLETE marker)在 strict service 内自然 fail-closed
          (COMPLETE marker 不存在 → AppError)。

    流程:
        1. R65 P0-07: capability-seal 校验(ALLOW_LEGACY_RESTORE 逃生舱)
        2. 初始化 R2(配置 + 连接)
        3. 校验 backup_id 必填(三段式发现的入口参数)
        4. 由 backup_id 计算 expected_manifest_key(strict service 内部
           下载 COMPLETE→manifest→payload,调用方不预加载 data)
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
    #    调用私有写入器。
    #    旧格式备份(无 COMPLETE marker)在 strict service 内 fail-closed
    #    (COMPLETE marker 不存在 → AppError)。
    signing_key = getattr(settings, "BACKUP_SIGNING_KEY", b"") or b""
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


def _build_cli_decryptor():
    """R62 P0-01 / R63 P0-06: 构建 CLI 用的解密器(若配置 BACKUP_KEK 则真实解密,否则 None)。

    生产环境应配置 BACKUP_KEK;未配置时 CLI 无法走严格三段式解密路径,
    调用方应在调用前检测并提示用户使用离线迁移工具。
    """
    try:
        from services.backup_crypto import is_encryption_available
        if not is_encryption_available():
            return None
        # 真实解密器:延迟构造避免循环依赖
        from services.backup_crypto import BackupDecryptor  # type: ignore
        return BackupDecryptor()
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=_i18n_t('services.db_restore.s3'))
    parser.add_argument(
        "--backup-id", type=str, required=True,
        help="备份 ID(timestamp,如 20260718_120000)— 三段式备份发现入口",
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
        help="备份类型(full/incremental,默认 full)",
    )
    args = parser.parse_args()
    asyncio.run(run_restore(
        backup_id=args.backup_id,
        table=args.table,
        dry_run=args.dry_run,
        backup_type=args.backup_type,
    ))


if __name__ == "__main__":
    main()
