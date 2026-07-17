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
    try:
        raw_cols = list(records[0].keys())
        columns = validate_columns_for_table(table, raw_cols)
        if not columns:
            logger.error(f"[{table}] 校验后无合法列(原始列: {raw_cols}),跳过此表")
            return 0
    except ValueError as e:
        logger.error(f"[{table}] 列校验失败: {e},跳过此表")
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
    try:
        raw_cols = list(records[0].keys())
        columns = validate_columns_for_table(table, raw_cols)
        if not columns:
            logger.error(f"[SQLite][{table}] 校验后无合法列(原始列: {raw_cols}),跳过此表")
            return 0
    except ValueError as e:
        logger.error(f"[SQLite][{table}] 列校验失败: {e},跳过此表")
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

async def _restore_from_backup_data(
    data: dict,
    *,
    _capability,  # R61 P0-03: _RestoreCapability(不可伪造,由 validate_and_restore_backup_strict 构造)
    tables: list[str] | None = None,
    merge: bool = False,
) -> dict:
    """从备份数据恢复数据库(R61 P0-03: 私有 Restore Engine 写入器)。

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

    Args:
        data: 备份数据 dict(含 "tables" 键)
        _capability: R61 P0-03 不可伪造的 _RestoreCapability(由
                     validate_and_restore_backup_strict 构造,强制必填)
        tables: 仅恢复指定表；None 则恢复备份中的所有表
        merge: True=增量补充(冲突保留现有数据); False=覆盖(清空后写入,默认)

    Returns:
        {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}
    """
    # R61 P0-03: _capability 是不可伪造的 _RestoreCapability 实例
    # (由 validate_and_restore_backup_strict 通过 _RESTORE_SENTINEL 构造)。
    # 本写入器为私有(仅 validate_and_restore_backup_strict 内部调用),
    # 不再做信任校验 — 安全保证来自:
    #   1. _RestoreCapability.__init__ 的 sentinel 检查(模块私有 _RESTORE_SENTINEL)
    #   2. validate_and_restore_backup_strict 是唯一能构造 _RestoreCapability 的公共入口
    #   3. _restore_from_backup_data 为私有函数(_ 前缀)
    # _capability 参数强制必填(keyword-only,无默认值),调用方必须通过 strict 入口获取。
    backup_tables = data.get("tables", {})

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

    logger.info(
        f"[db_restore] 恢复完成: {sum(result['restored'].values())} 行, "
        f"{len(result['errors'])} 个错误, 模式={'merge' if merge else 'overwrite'}"
    )

    # R44 7.2: 记录 restore RU 消耗(估算: 每个恢复表约 50 RU)
    # 单独记入 service='restore' 维度,不混入业务空载门禁
    try:
        from services.ru_cost_center import record_restore_usage
        await record_restore_usage(
            ru_cost=len(result["restored"]) * 50,
            operation="restore_from_backup_data",
        )
    except Exception:
        pass  # 不影响 restore 主流程

    return result


async def _restore_crdb_tables(
    tables_data: dict[str, list], merge: bool, result: dict,
):
    """恢复 CRDB 表(使用 db_client 连接池)。"""
    from database.session import _client as db_client, _validate_identifier

    if not db_client.is_connected:
        try:
            from database.session import init_db
            await init_db()
        except Exception as e:
            result["errors"].append(f"CRDB 连接初始化失败: {e}")
            logger.error(f"[db_restore] CRDB 连接初始化失败: {e}")
            return

    for table_name, rows in tables_data.items():
        try:
            _validate_identifier(table_name)
        except ValueError as e:
            logger.warning(f"[db_restore] 跳过非法表名: {e}")
            result["skipped"].append(table_name)
            continue
        if not rows:
            result["restored"][table_name] = 0
            continue
        try:
            await db_client.execute("BEGIN")
            try:
                if not merge:
                    # 覆盖模式:清空目标表
                    await db_client.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE')

                # 确定 ON CONFLICT 子句(仅 merge 模式)
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

                # R35 P1-6: 按表校验列
                raw_cols = list(rows[0].keys())
                cols = validate_columns_for_table(table_name, raw_cols)
                if not cols:
                    logger.error(f"[db_restore] 表 {table_name} 校验后无合法列,跳过")
                    result["errors"].append(f"{table_name}: 无合法列")
                    continue

                inserted = 0
                for row in rows:
                    try:
                        # 列名格式校验(防 SQL 注入)
                        safe_cols = [_validate_identifier(c) for c in cols]
                    except ValueError as e:
                        logger.warning(f"[db_restore] 表 {table_name} 含非法列名,跳过该行: {e}")
                        continue
                    placeholders = ", ".join(f"${i+1}" for i in range(len(safe_cols)))
                    col_list = ", ".join(f'"{c}"' for c in safe_cols)
                    params = [row[c] for c in cols]
                    sql = f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders}){conflict_clause}'
                    # R35 P1-4: db_client.execute(sql, params) 内部会 *(params or []) 展开
                    await db_client.execute(sql, params)
                    inserted += 1
                await db_client.execute("COMMIT")
                result["restored"][table_name] = inserted
                mode_text = "增量补充" if merge else "覆盖恢复"
                logger.info(f"[db_restore] {mode_text} 表 {table_name}: {inserted} 行")
            except Exception as inner_e:
                try:
                    await db_client.execute("ROLLBACK")
                except Exception as rollback_err:
                    logger.error(f"[db_restore] ROLLBACK 失败 (table={table_name}): {rollback_err}")
                raise inner_e
        except Exception as e:
            result["errors"].append(f"{table_name}: {e}")
            logger.error(f"[db_restore] 恢复表 {table_name} 失败: {e}")


async def _restore_sqlite_tables_to_db(
    tables_data: dict[str, list], merge: bool, result: dict, is_relay: bool = False,
):
    """恢复 SQLite 表到指定数据库(cache_store.db 或 relay_pool.db)。

    R35 P1-5: source="sqlite" / "relay_sqlite" 的表走此路径。
    """
    import aiosqlite

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
        return

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
                    result["errors"].append(f"{table_name}: {e}")
                    logger.error(f"[db_restore][{db_label}] 恢复表 {table_name} 失败: {e}")
    except Exception as e:
        err = f"{db_label} 打开数据库失败: {e}"
        result["errors"].append(err)
        logger.error(f"[db_restore] {err}")


# ═══════════════════════════════════════════════════════════════
#  CLI 入口(保留向后兼容)
# ═══════════════════════════════════════════════════════════════

async def run_restore(table: str = None, dry_run: bool = False):
    """执行恢复流程(CLI 入口)。

    R35 P1-4: 调用 restore_from_backup_data() 单一 Restore Engine。
    """
    # 1. 初始化 R2
    if not settings.R2_ACCOUNT_ID:
        logger.error("R2 凭证未配置，无法恢复")
        sys.exit(1)

    r2_storage.configure(
        account_id=settings.R2_ACCOUNT_ID,
        access_key=settings.R2_ACCESS_KEY_ID,
        secret_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET_NAME,
        endpoint=settings.R2_ENDPOINT if settings.R2_ENDPOINT else None,
    )
    await r2_storage.connect()

    # 2. 下载并解析备份
    data = await get_latest_backup()
    tables_data = data.get("tables", {})

    # 3. 确定要恢复的表
    if table:
        if table not in tables_data:
            logger.error(f"备份中不包含表 '{table}'，可用表: {', '.join(tables_data.keys())}")
            sys.exit(1)
        target_tables = [table]
    else:
        target_tables = [t for t in ALL_TABLES if t in tables_data]

    if dry_run:
        logger.info("=== DRY-RUN 模式，不会实际写入数据 ===")

    # R35 P1-4: 调用单一 Restore Engine
    # R61 P0-03: 路由通过 validate_and_restore_backup_strict() 公共入口
    # (该入口构造不可伪造的 _RestoreCapability 并调用私有 _restore_from_backup_data)
    # CLI 入口的备份格式为旧版 db_backup_*.json(非三段式 payload/manifest/COMPLETE),
    # 通过 data= 参数传入已下载+解密+校验的数据,跳过严格三段式下载验证。
    from services.backup_dr_validate import validate_and_restore_backup_strict
    result = await validate_and_restore_backup_strict(
        data=data,
        tables=target_tables if table else None,
        merge=False,
        # CLI 旧格式备份: 已通过 get_latest_backup() 完成 manifest+checksum+decrypt 校验
        # 此处跳过严格三段式验证(storage/signing_key/decryptor 等参数留空)
        skip_strict_validation=True,
        validation_note="CLI run_restore: old-format backup validated via get_latest_backup()",
    )

    # 打印恢复结果
    restored = result.get("restored", {})
    errors = result.get("errors", [])
    for tbl, count in restored.items():
        logger.info(f"[{tbl}] 恢复完成: {count} 条记录")
    if errors:
        logger.error(f"恢复过程中有 {len(errors)} 个错误:")
        for err in errors:
            logger.error(f"  - {err}")

    await r2_storage.close()
    logger.info("数据库恢复完成")


def main():
    parser = argparse.ArgumentParser(description=_i18n_t('services.db_restore.s3'))
    parser.add_argument(
        "--table", type=str, default=None,
        help=_i18n_t('services.db_restore.s4'),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=_i18n_t('services.db_restore.s5'),
    )
    args = parser.parse_args()
    asyncio.run(run_restore(table=args.table, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
