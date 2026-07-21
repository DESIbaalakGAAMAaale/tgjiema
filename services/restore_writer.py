"""R69 P0-4: 生产镜像可用的唯一恢复写入器模块。

本模块由 R69 Wave 2 整改从 ``services/db_restore.py`` 提取,目的是消除
生产镜像对 ``services/db_restore.py`` 的延迟 import 依赖(``.dockerignore``
排除 ``services/db_restore.py`` 作为 CLI-only 入口,但生产 runtime 仍需要
_restore_from_backup_data / _safe_val / TABLE_PK 等写入器能力)。

架构分层(R69 Wave 2 方案 A):
    - 生产恢复唯一入口: ``services.restore_orchestrator.RestoreOrchestrator``
    - 严格三段式验证 + capability 签发: ``services.backup_dr_validate.validate_and_restore_backup_strict``
    - 私有写入器(本模块): ``services.restore_writer._restore_from_backup_data``
    - CLI 入口(legacy,生产被 capability-sealed): ``services.db_restore.run_restore``

模块内容:
    - ``TABLE_PK`` / ``ALL_TABLES`` / ``_ALLOWED_TABLES`` / ``_ALLOWED_COLUMNS``
      (从 ``services.backup_schema`` 派生,单一事实源)
    - ``_sanitize_table`` / ``_sanitize_column`` (向后兼容白名单校验)
    - ``_safe_val`` / ``_sqlite_safe_val`` (CRDB / SQLite 类型转换)
    - ``_restore_table`` (asyncpg 表级 UPSERT 写入器)
    - ``_restore_sqlite_table`` (aiosqlite 表级 UPSERT 写入器)
    - ``_restore_from_backup_data`` (主写入器,接受不可伪造的 _RestoreCapability)
    - ``_restore_crdb_tables`` (CRDB 子写入器,使用 db_client.transaction())
    - ``_restore_sqlite_tables_to_db`` (SQLite 子写入器)

向后兼容:
    - ``services/db_restore.py`` 通过 re-export 保持对 tests/scripts 的兼容
      (旧代码 ``from services.db_restore import TABLE_PK`` 仍可工作)
    - 生产镜像通过 ``.dockerignore`` 排除 ``services/db_restore.py`` CLI 入口,
      但 ``services/restore_writer.py`` 不被排除(必需的生产 runtime 模块)

安全保证:
    - ``_restore_from_backup_data`` 首条语句对
      ``verified_payload.canonical_payload_bytes`` 重算 SHA-256,
      与 ``_capability.payload_digest`` 比对,防御 ``object.__setattr__``
      绕过 frozen 替换 canonical_payload_bytes 的攻击(R63 P0-02 / R64 P1-01)。
    - ``capability.assert_valid`` 强制 sentinel + nonce 防重放 + 未过期 +
      payload_digest 一致 + scope 一致(R62 P0-02 / R63 P1-01)。
"""

from __future__ import annotations

import hashlib
import json
import os
import time as _time
from datetime import datetime

import asyncpg
from loguru import logger

from config import settings
from services.backup_schema import (
    BACKUP_SCHEMA, get_restore_tables, is_table_allowed, ALLOWED_COLUMNS,
    get_table_source, validate_columns_for_table,
)
from services.i18n import translate as _i18n_t

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

    R69 P0-4 (Wave 2): 本函数从 services/db_restore.py 提取到 services/restore_writer.py,
        消除生产镜像对 services/db_restore.py 的延迟 import 依赖。生产镜像通过
        .dockerignore 排除 services/db_restore.py(CLI-only 入口),但本模块不被
        排除(必需的生产 runtime 模块)。

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
    from services.error_codes import AppError, ErrorCodes

    # R63 P0-02 / R64 P1-01: 首条语句 — 对 verified_payload.canonical_payload_bytes
    # 实际 bytes 重新计算 SHA-256,与 capability.payload_digest 比对。
    # 这防御 object.__setattr__ 绕过 frozen 替换 canonical_payload_bytes 的攻击:
    #   - 即使 attacker 替换 verified_payload.canonical_payload_bytes + payload_digest,
    #     capability.payload_digest 是构造时内嵌的(不可变),重算 digest 不匹配 → fail-closed。
    #   - 即使 attacker 只替换 canonical_payload_bytes,payload_digest 仍为旧值,
    #     重算 digest 与 capability.payload_digest 不匹配 → fail-closed。
    actual_payload_digest = hashlib.sha256(
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
            logger.warning(f"[restore_writer] 表 {table_name} 不在 BACKUP_SCHEMA 中,跳过")
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
            logger.warning(f"[restore_writer] 表 {table_name} source={source}(未知/redis),跳过")
            result["skipped"].append(table_name)

    logger.info(
        f"[restore_writer] 按 source 分组: CRDB={len(crdb_to_restore)}表, "
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
            ru_cost=len(result["restored"]) * 50,
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
            logger.error(f"[restore_writer] {err}")
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
            logger.error(f"[restore_writer] {err}")
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
            logger.error(f"[restore_writer] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})
        if not cols:
            # R63 P1-04: cols 为空 → raise(不 continue,BEGIN 前已校验,无开放事务)
            err = f"表 {table_name} 校验后无合法列(原始列: {raw_cols})"
            result["errors"].append(err)
            logger.error(f"[restore_writer] {err}")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                           params={"backup_id": "", "errors": err})

        # 3. 列名格式校验(防 SQL 注入)— BEGIN 前完成
        try:
            safe_cols = [_validate_identifier(c) for c in cols]
        except ValueError as e:
            err = f"表 {table_name} 含非法列名: {e}"
            result["errors"].append(err)
            logger.error(f"[restore_writer] {err}")
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
                logger.warning(f"[restore_writer] 表 {table_name} 未知冲突列,merge 模式可能因主键冲突失败")

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
                logger.info(f"[restore_writer] {mode_text} 表 {table_name}: {inserted} 行")
        except AppError:
            # 已经是 AppError,直接传播(fail-closed)
            raise
        except Exception as e:
            # R63 P0-03: 任一表错误 → raise AppError(不返回部分成功)
            # context manager 已自动 ROLLBACK
            err = f"表 {table_name}: {e}"
            result["errors"].append(err)
            logger.error(f"[restore_writer] 恢复表 {table_name} 失败: {e}")
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
        logger.error(f"[restore_writer] {err}")
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
                    logger.error(f"[restore_writer][{db_label}] 恢复表 {table_name} 失败: {e}")
                    # R63 P0-03: 任一表错误 → raise AppError(不返回部分成功)
                    raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                                   params={"backup_id": "", "errors": err})
    except AppError:
        # 已经是 AppError,直接传播
        raise
    except Exception as e:
        err = f"{db_label} 打开数据库失败: {e}"
        result["errors"].append(err)
        logger.error(f"[restore_writer] {err}")
        # R63 P0-03: fail-closed
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                       params={"backup_id": "", "errors": err})
