from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets as _secrets
import subprocess
from datetime import datetime, timezone

from loguru import logger

from config import settings
from database.session import _client as db_client
from database.session import _validate_identifier, get_config
from services.backup_crypto import (
    encrypt_payload,
    get_key_id,
    is_encryption_available,
)

# R73 §5.3 P0-05: 三段式备份 COMPLETE marker 构建(不可伪造 HMAC 签名)
from services.backup_dr_validate import build_complete_marker
from services.backup_schema import BACKUP_SCHEMA, get_backup_tables, get_tables_by_source

# R48 P1: 统一错误码协议化(替代裸字符串 RuntimeError)
from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t
from storage.r2 import _r2 as r2_storage
from storage.r2 import configure_storage_from_settings

# ─── 表清单(单一事实源: services/backup_schema.py) ───
# 保留向后兼容的别名,等价于原 SMALL_TABLES / _LARGE_TABLES / _TABLE_WHERE
# 新增表时只需在 backup_schema.BACKUP_SCHEMA 中添加条目,无需修改本文件
SMALL_TABLES = set(get_backup_tables())
_LARGE_TABLES = {t.name for t in BACKUP_SCHEMA.values() if t.is_large}
BACKUP_TABLES = SMALL_TABLES

# 每个表可选的 WHERE 条件,用于过滤备份范围(从 BACKUP_SCHEMA 派生)
_TABLE_WHERE = {t.name: t.where_clause for t in BACKUP_SCHEMA.values() if t.where_clause}

# 备份保留份数（超出则自动清理最旧的）
MAX_BACKUP_RETENTION = 168  # 7天 × 24小时 / 1小时间隔 ≈ 168 份

# R35 P1-7: Schema 版本(从 backup_schema 的表数量派生,用于 bundle manifest)
_BACKUP_SCHEMA_VERSION = f"r36_{len(BACKUP_SCHEMA)}tables"

# R36 H7: 增量备份 watermark 配置
_WATERMARK_KEY = "db_backup/watermark.json"  # R2 中的 watermark 存储路径
_FULL_BACKUP_INTERVAL = 24  # 每 24 次增量后做一次全量(如每小时备份 = 每天一次全量)

# P0-3: 备份中不再脱敏敏感字段 (api_hash / r2_secret_key / r2_access_key)。
# 备份仅存储在运维自有 R2 桶内(需可信环境),若替换为 ***REDACTED*** 占位符,
# 会导致 db_restore 恢复后中继账号与 R2 凭证变为占位符、不可用(废库)。
# 因此保留真实值,确保恢复后凭证可用。_SENSITIVE_FIELDS 置空即关闭脱敏。
_SENSITIVE_FIELDS: set[str] = set()
_REDACTED_VALUE = "***REDACTED***"  # 保留常量以便未来按需启用,当前脱敏集为空故不会被写入


def _redact_secrets(data: dict) -> dict:
    """脱敏备份数据中的敏感字段，不影响原始数据库。"""
    tables = data.get("tables", {})
    for table_name, rows in tables.items():
        if table_name in ("backup_config", "kv_config"):
            for row in rows:
                # N-15-1: 按 config_key 匹配行级密钥（如 config_key="r2_secret_key" → config_value 脱敏）
                config_key = (row.get("config_key") or "").lower()
                if config_key in _SENSITIVE_FIELDS:
                    row["config_value"] = _REDACTED_VALUE
        if table_name == "relay_accounts":
            for row in rows:
                for key in list(row.keys()):
                    if key.lower() in _SENSITIVE_FIELDS:
                        row[key] = _REDACTED_VALUE
    return data


# ═══════════════════════════════════════════════════════════════
#  R35 P1-7: Bundle Manifest 辅助函数
# ═══════════════════════════════════════════════════════════════

def _get_commit_sha() -> str:
    """获取当前 git commit SHA(R35 P1-7: bundle manifest 必含字段)。

    优先级:
    1. 环境变量 GIT_COMMIT_SHA(部署时注入)
    2. git rev-parse HEAD(开发环境)
    3. "unknown"(git 不可用时)
    """
    sha = os.environ.get("GIT_COMMIT_SHA", "").strip()
    if sha:
        return sha[:12]  # 短 SHA
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:12]
    except Exception as e:
        # R64 P1-07: destructive 域禁止 except pass;git 不可用时回退到 "unknown"
        logger.debug(f"[db_backup] git rev-parse 失败,回退 unknown: {e}")
    return "unknown"


def _compute_sha256(content: bytes) -> str:
    """计算内容的 SHA-256 校验和(R35 P1-7: bundle manifest 必含字段)。"""
    return hashlib.sha256(content).hexdigest()


def _build_bundle_manifest(
    backup_data: dict,
    content: bytes,
    start_time: datetime,
    end_time: datetime,
    backup_type: str = "full",
    watermark: str | None = None,
    prev_watermark: str | None = None,
    encryption_info: dict | None = None,
    ciphertext_sha256: str | None = None,
    backup_id: str = "",
    *,
    # R73 §5.3 P0-05: 新增强制信任链字段(可选,向后兼容 daemon 路径)
    source_sha: str = "",
    source_database_identity: str = "",
    schema_fingerprint: str = "",
    payload_key: str = "",
    manifest_key: str = "",
    key_id: str = "",
    created_at: str = "",
) -> dict:
    """构建 bundle manifest(R35 P1-7, R36 H7 增强, R40 P0-6 双 checksum, R73 §5.3 P0-05 强信任链)。

    Bundle 包含:
    - commit SHA(git rev-parse HEAD 或环境变量)
    - schema version(backup_schema 版本)
    - 每表行数
    - SHA-256 checksum(整个 backup JSON 的校验和,向后兼容)
    - R40 P0-6: plaintext_sha256(明文校验和) + ciphertext_sha256(密文校验和)
    - R40 P0-6: backup_id(用于 AAD 绑定)
    - 开始/结束时间
    - source 标记(crdb/sqlite/relay_sqlite)
    - R36 H7: backup_type (full/incremental)
    - R36 H7: watermark (本次备份的 updated_at 上界)
    - R36 H7: encryption (加密元数据)
    - R73 §5.3 P0-05: source_sha / source_database_identity / schema_fingerprint /
      payload_key / manifest_key / key_id / created_at(强信任链绑定字段,
      使 manifest 自身可被独立校验,无需依赖外部 latest 指针)
    """
    tables = backup_data.get("tables", {})
    table_stats = {}
    for table_name, rows in tables.items():
        source = "unknown"
        if table_name in BACKUP_SCHEMA:
            source = BACKUP_SCHEMA[table_name].source
        table_stats[table_name] = {
            "row_count": len(rows),
            "source": source,
        }

    # R40 P0-6: plaintext_sha256 = 对明文 content 的 SHA-256
    plaintext_sha = _compute_sha256(content)

    manifest = {
        "version": "3.0",  # R36: manifest v3(含 backup_type/watermark/encryption)
        "commit_sha": _get_commit_sha(),
        "schema_version": _BACKUP_SCHEMA_VERSION,
        # R40 P0-6: 双 checksum 分离
        "plaintext_sha256": plaintext_sha,  # 明文校验和(解密后校验)
        "ciphertext_sha256": ciphertext_sha256 or plaintext_sha,  # 密文校验和(下载后校验)
        "checksum_sha256": plaintext_sha,  # 向后兼容(等价于 plaintext_sha256)
        "backup_id": backup_id,  # R40 P0-6: AAD 绑定标识
        "content_size_bytes": len(content),
        "backup_started_at": start_time.isoformat(),
        "backup_finished_at": end_time.isoformat(),
        "table_stats": table_stats,
        "total_tables": len(tables),
        "total_rows": sum(len(v) for v in tables.values()),
        # R36 H7: 增量备份元数据
        "backup_type": backup_type,  # "full" 或 "incremental"
        "watermark": watermark,       # 本次备份的 updated_at 上界(ISO 格式)
        "prev_watermark": prev_watermark,  # 上次备份的 watermark(增量时使用)
        # R36 H7: 加密元数据
        "encryption": encryption_info or {"encrypted": False, "algorithm": "none"},
    }
    # R73 §5.3 P0-05: 追加强信任链字段(仅当调用方提供时写入,
    # 保持向后兼容 — daemon 路径 / 旧测试不传这些参数时 manifest 结构不变)
    if source_sha:
        manifest["source_sha"] = source_sha
    if source_database_identity:
        manifest["source_database_identity"] = source_database_identity
    if schema_fingerprint:
        manifest["schema_fingerprint"] = schema_fingerprint
    if payload_key:
        manifest["payload_key"] = payload_key
    if manifest_key:
        manifest["manifest_key"] = manifest_key
    if key_id:
        manifest["key_id"] = key_id
    if created_at:
        manifest["created_at"] = created_at
    return manifest


# ═══════════════════════════════════════════════════════════════
#  R35 P1-5: 按 source 分组的备份快照器
# ═══════════════════════════════════════════════════════════════

async def _backup_crdb_tables(tables: list[str], watermark: str | None = None) -> dict:
    """按 ``BACKUP_SCHEMA`` 显式列清单备份 CRDB 表。

    R35 P1-5: 仅备份 source="crdb" 的表,避免对 SQLite-only 表执行 CRDB 查询。
    R36 H7: 支持 watermark 增量备份(只查 updated_at > watermark 的行)。
    R37 P1-5: 增量备份同时捕捉 deleted_at > watermark 的软删除行,
              解决仅靠 updated_at 无法捕捉删除事件的问题。
    R83: 禁止 ``SELECT *`` 将未审计的新列静默带入 payload。备份与恢复均以
         ``BACKUP_SCHEMA.columns`` 为单一事实源；列漂移由测试和真实查询 fail-closed。
    """
    results = {}
    for table in sorted(tables):
        try:
            safe_name = _validate_identifier(table)
            schema = BACKUP_SCHEMA[table]
            if not schema.columns:
                raise AppError(
                    ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID,
                    params={"reason": "backup_schema_columns_empty", "field": table},
                )
            safe_columns = [_validate_identifier(column) for column in schema.columns]
            column_sql = ", ".join(f'"{column}"' for column in safe_columns)
            conditions = []
            # 表级 WHERE 条件(如 status = 'active')
            table_where = _TABLE_WHERE.get(table)
            if table_where:
                conditions.append(table_where)
            # R36 H7 + R37 P1-5: 增量 watermark 条件
            # 同时检查 updated_at(常规变更)和 deleted_at(软删除)
            # 任一列 > watermark 都纳入本次增量备份
            if watermark:
                ts_cols = []
                if schema and "updated_at" in schema.columns:
                    ts_cols.append('"updated_at"')
                # R37 P1-5: 增量 watermark 同时检查 deleted_at 列
                if schema and "deleted_at" in schema.columns:
                    ts_cols.append('"deleted_at"')
                if ts_cols:
                    # (updated_at > $1 OR deleted_at > $1)
                    or_cond = " OR ".join(f"{c} > $1" for c in ts_cols)
                    conditions.append(f"({or_cond})")

            if conditions:
                where_clause = " AND ".join(conditions)
                if watermark and "$1" in where_clause:
                    sql = (
                        f'SELECT {column_sql} FROM "{safe_name}" '
                        f'WHERE {where_clause}'
                    )
                    records = await db_client.fetch(sql, watermark)
                else:
                    sql = (
                        f'SELECT {column_sql} FROM "{safe_name}" '
                        f'WHERE {where_clause}'
                    )
                    records = await db_client.fetch(sql)
            else:
                sql = f'SELECT {column_sql} FROM "{safe_name}"'
                records = await db_client.fetch(sql)
            results[table] = [dict(r) for r in records]
            logger.debug(f"[Backup][CRDB] {table}: {len(records)} 行(watermark={watermark or 'none'})")
        except Exception as e:
            # 表不存在时降级为 DEBUG(如 kv_config 在部分部署中不存在)
            if "does not exist" in str(e):
                logger.debug(f"[Backup][CRDB] 表 {table} 不存在,跳过: {e}")
            else:
                logger.warning(f"[Backup][CRDB] 跳过表 {table}: {e}")
    return results


async def _backup_sqlite_tables(tables: list[str], db_path) -> dict:
    """备份 SQLite 表(从 cache_store.db 读取)。

    R35 P1-5: source="sqlite" 的表走此路径,而非 CRDB。
    使用独立的只读连接,不干扰 CacheStore 的运行时连接。

    Args:
        tables: 要备份的表名列表
        db_path: SQLite 数据库文件路径(cache_store.db)
    """
    import aiosqlite

    results = {}
    if not os.path.exists(str(db_path)):
        logger.warning(f"[Backup][SQLite] 数据库文件不存在,跳过: {db_path}")
        return results

    try:
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
            conn.row_factory = aiosqlite.Row
            for table in sorted(tables):
                try:
                    safe_name = _validate_identifier(table)
                    where = _TABLE_WHERE.get(table)
                    if where:
                        sql = f'SELECT * FROM "{safe_name}" WHERE {where}'
                    else:
                        sql = f'SELECT * FROM "{safe_name}"'
                    cursor = await conn.execute(sql)
                    rows = await cursor.fetchall()
                    results[table] = [dict(r) for r in rows]
                    logger.debug(f"[Backup][SQLite] {table}: {len(rows)} 行")
                except Exception as e:
                    if "no such table" in str(e).lower():
                        logger.debug(f"[Backup][SQLite] 表 {table} 不存在,跳过: {e}")
                    else:
                        logger.warning(f"[Backup][SQLite] 跳过表 {table}: {e}")
    except Exception as e:
        logger.error(f"[Backup][SQLite] 打开数据库失败: {e}")
    return results


async def _backup_relay_sqlite_tables(tables: list[str]) -> dict:
    """备份 relay SQLite 表(从 relay_pool.db 读取)。

    R35 P1-5: source="relay_sqlite" 的表走此路径。
    """
    from database.relay_db import DB_PATH as RELAY_DB_PATH
    return await _backup_sqlite_tables(tables, RELAY_DB_PATH)


# ═══════════════════════════════════════════════════════════════
#  R36 H7: 增量备份 watermark 管理
# ═══════════════════════════════════════════════════════════════

async def _get_last_watermark() -> dict | None:
    """从 R2 读取上一次备份的 watermark。

    Returns:
        watermark dict(含 updated_at, backup_type, count)或 None(首次备份)
    """
    try:
        content = await r2_storage.download(_WATERMARK_KEY)
        return json.loads(content.decode("utf-8"))
    except Exception:
        return None


async def _save_watermark(watermark: str, backup_type: str, incremental_count: int) -> None:
    """保存当前备份的 watermark 到 R2。

    Args:
        watermark: 本次备份的 updated_at 上界(ISO 格式)
        backup_type: "full" 或 "incremental"
        incremental_count: 自上次全量以来的增量次数
    """
    data = {
        "updated_at": watermark,
        "backup_type": backup_type,
        "incremental_count": incremental_count,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
    try:
        await r2_storage.upload(_WATERMARK_KEY, content, "application/json")
        logger.debug(f"[Backup] watermark 已保存: {watermark} (type={backup_type}, count={incremental_count})")
    except Exception as e:
        logger.warning(f"[Backup] 保存 watermark 失败(下次备份将做全量): {e}")


async def _compute_watermark(all_tables: dict) -> str:
    """计算本次备份的 watermark(所有表中最大的 updated_at)。

    遍历所有表的行,找最大的 updated_at 或 created_at 值。
    """
    # R40: 初始用空字符串,如果无数据则返回当前时间
    max_ts = ""
    for table_name, rows in all_tables.items():
        ts_col = None
        if table_name in BACKUP_SCHEMA:
            cols = BACKUP_SCHEMA[table_name].columns
            if "updated_at" in cols:
                ts_col = "updated_at"
            elif "created_at" in cols:
                ts_col = "created_at"
        if not ts_col:
            continue
        for row in rows:
            val = row.get(ts_col)
            if val and isinstance(val, str) and val > max_ts:
                max_ts = val
            elif val and hasattr(val, "isoformat"):
                iso = val.isoformat()
                if iso > max_ts:
                    max_ts = iso
    # R40: 如果没有找到任何时间戳,返回当前时间
    if not max_ts:
        max_ts = datetime.now(timezone.utc).isoformat()
    return max_ts


async def backup_all_tables(watermark: str | None = None, backup_type: str = "full") -> dict:
    """备份所有核心表(按 source 分组,R35 P1-5/P1-7, R36 H7 增量, R38 P1-5 顺序修正)。

    R35 P1-5: 按 source 分组执行备份
    R35 P1-7: 生成 bundle manifest
    R36 H7: 支持 watermark 增量备份(仅备份 updated_at > watermark 的行)
    R38 P1-5: 修正 manifest/checksum 顺序 —
      采集 → 脱敏 → 序列化 plaintext → 生成最终 manifest(含 checksum)→
      返回 backup_data(由调用方加密 + 上传 manifest)
      原 R35 实现 manifest 在脱敏前构建,导致 checksum 不匹配脱敏后数据。

    Args:
        watermark: 上次备份的 watermark(增量模式),None 表示全量
        backup_type: "full" 或 "incremental"(仅影响 manifest 标记)

    大表(decode_logs/jobs/pending_uploads/rotate_log)跳过：
    - decode_logs/jobs 是短期流水数据，无需长期备份
    - pending_uploads 是瞬时状态，重启后从频道重放
    - rotate_log 是审计日志，数据量大但非核心
    """
    start_time = datetime.now(timezone.utc)

    # R35 P1-5: 按 source 分组备份
    crdb_tables = get_tables_by_source("crdb")
    sqlite_tables = get_tables_by_source("sqlite")
    relay_sqlite_tables = get_tables_by_source("relay_sqlite")
    redis_tables = get_tables_by_source("redis")

    logger.info(
        f"[Backup] 按 source 分组: CRDB={len(crdb_tables)}表, "
        f"SQLite={len(sqlite_tables)}表, relay_sqlite={len(relay_sqlite_tables)}表, "
        f"redis={len(redis_tables)}表(跳过), type={backup_type}, watermark={watermark or 'none'}"
    )

    # 1. CRDB 表备份(R36 H7: 传入 watermark 做增量查询)
    crdb_data = await _backup_crdb_tables(crdb_tables, watermark=watermark)

    # 2. SQLite 表备份(从 cache_store.db) — SQLite 表不做增量,每次全量(数据量小)
    from database.cache_store import DB_PATH as CACHE_DB_PATH
    sqlite_data = await _backup_sqlite_tables(sqlite_tables, CACHE_DB_PATH)

    # 3. relay SQLite 表备份(从 relay_pool.db)
    relay_data = await _backup_relay_sqlite_tables(relay_sqlite_tables)

    # 4. Redis 表跳过(暂不支持快照)
    if redis_tables:
        logger.debug(f"[Backup][Redis] 跳过 {len(redis_tables)} 表(暂不支持快照): {redis_tables}")

    # 合并所有数据
    all_tables = {}
    all_tables.update(crdb_data)
    all_tables.update(sqlite_data)
    all_tables.update(relay_data)

    end_time = datetime.now(timezone.utc)

    # R36 H7: 计算本次备份的 watermark(用于下次增量)
    current_watermark = await _compute_watermark(all_tables)

    backup_data = {
        "backup_time": start_time.isoformat(),
        "tables": all_tables,
    }

    # R38 P1-5: 顺序修正 — 脱敏在 manifest 构建之前
    # 原 R35 实现在此处直接构建 manifest(checksum 基于未脱敏的 tables_content),
    # 然后 _run_backup_loop() 再调 _redact_secrets(),导致 checksum 与实际 plaintext 不匹配。
    # 新顺序:此处返回未带 manifest 的 backup_data,
    # 由 _run_backup_loop() 完成:脱敏 → 序列化 → checksum → manifest → 加密 → 上传
    backup_data["_r38_p1_5_metadata"] = {
        "start_time": start_time,
        "end_time": end_time,
        "backup_type": backup_type,
        "watermark": current_watermark,
        "prev_watermark": watermark,
    }
    return backup_data


async def run_db_backup():
    # 确保数据库连接池已初始化（某些场景下 _auto_seed 可能未成功初始化）
    if not db_client.is_connected:
        try:
            from database.session import init_db
            await init_db()
        except Exception as e:
            logger.warning(f"数据库连接初始化失败,跳过备份: {e}")
            return

    try:
        await _run_backup_loop()
    finally:
        # 确保退出时关闭数据库连接和 SQLite 缓存,避免进程挂起被 SIGKILL
        try:
            from database.session import close_db
            await close_db()
        except Exception as close_err:
            # R64 P1-07: destructive 域禁止 except pass;资源清理失败需记录
            logger.warning(f"[db_backup] close_db 失败(资源泄漏风险): {close_err}")
        try:
            await r2_storage.close()
        except Exception as close_err:
            # R64 P1-07: destructive 域禁止 except pass;资源清理失败需记录
            logger.warning(f"[db_backup] r2_storage.close 失败(资源泄漏风险): {close_err}")
        logger.info("[db_backup] 资源已清理,进程退出")


async def _run_backup_loop():
    """备份主循环(由 run_db_backup 调用,确保 finally 中清理资源)。

    R36 H7:
    - 增量 watermark 备份(首次全量,后续增量,每 _FULL_BACKUP_INTERVAL 次全量一次)
    - AES-256-GCM 信封加密(如配置了 BACKUP_KEK)
    """
    enabled_cfg = await get_config("db_backup_enabled")
    if enabled_cfg is None:
        enabled = settings.DB_BACKUP_ENABLED
    else:
        enabled = enabled_cfg.lower() == "true"
    if not enabled:
        logger.info("数据库备份未启用(DB_BACKUP_ENABLED=false),跳过启动")
        # R35 P1-7: 商用建议使用 CRDB Basic 托管备份
        logger.warning(
            "[R35-P1-7] 商用环境建议使用 CRDB Basic 每日托管备份(24h 自动快照,30 天保留),"
            "而非自建每 6 小时全量 SELECT *。自建备份仅作补充手段。"
        )
        return

    # R36 H7 + R37 P1-4: 检查加密可用性
    encryption_enabled = is_encryption_available()
    if encryption_enabled:
        logger.info("[R36-H7] 备份加密已启用(AES-256-GCM 信封加密)")
    else:
        # R37 P1-4: 商用强制加密 — KEK 不可用时停止备份服务,绝不上传明文
        encryption_required = getattr(settings, "BACKUP_ENCRYPTION_REQUIRED", False)
        if encryption_required:
            logger.error(
                "[R37-P1-4] BACKUP_ENCRYPTION_REQUIRED=true 但加密不可用"
                "(BACKUP_KEK 未配置/格式错误/cryptography 未安装),"
                "停止备份服务以避免明文数据泄露。"
                "请配置 BACKUP_KEK(32 字节 base64)或显式设置 BACKUP_ENCRYPTION_REQUIRED=false(仅本地开发)。"
            )
            return
        logger.warning(
            "[R36-H7] 备份加密未启用(BACKUP_KEK 未配置或 cryptography 不可用),"
            "备份将以明文存储。商用环境必须配置 BACKUP_KEK + BACKUP_ENCRYPTION_REQUIRED=true。"
        )

    # R35 P1-7 + R36 H7: 启用自建备份时,日志说明增量模式
    logger.warning(
        "[R36-H7] 自建备份已启用(增量 watermark 模式)。"
        "商用环境建议优先使用 CRDB Basic 托管备份。"
        f"增量配置: 每 {_FULL_BACKUP_INTERVAL} 次增量后做一次全量。"
    )

    # R76 O7 / 10.G: 统一对象存储配置(R2 生产 / MinIO CI),根据 OBJECT_STORAGE_BACKEND 选择
    await configure_storage_from_settings()
    if not r2_storage._access_key or not r2_storage._secret_key:
        logger.warning("对象存储凭证未配置(.env 和 config 表均无),数据库备份跳过")
        return

    interval_cfg = await get_config("db_backup_interval")
    if interval_cfg is None:
        interval = settings.DB_BACKUP_INTERVAL_MINUTES
    else:
        try:
            interval = max(int(interval_cfg), 1)
        except (ValueError, TypeError):
            logger.warning(f"[db_backup] db_backup_interval 配置值 '{interval_cfg}' 无效,使用默认值 {settings.DB_BACKUP_INTERVAL_MINUTES}")
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
    logger.info("CockroachDB 数据库备份服务启动,间隔 {} 分钟(增量 watermark 模式)", interval)

    while True:
        try:
            # R36 H7: 读取上次 watermark,决定全量还是增量
            last_wm = await _get_last_watermark()
            if last_wm is None:
                # 首次备份:全量
                backup_type = "full"
                watermark = None
                incremental_count = 0
                logger.info("[Backup] 首次备份(无 watermark),执行全量备份")
            elif last_wm.get("incremental_count", 0) >= _FULL_BACKUP_INTERVAL:
                # 达到全量间隔:做一次全量
                backup_type = "full"
                watermark = None
                incremental_count = 0
                logger.info(
                    f"[Backup] 增量次数 {last_wm.get('incremental_count')} "
                    f">= {_FULL_BACKUP_INTERVAL},执行全量备份"
                )
            else:
                # 增量备份
                backup_type = "incremental"
                watermark = last_wm.get("updated_at")
                incremental_count = last_wm.get("incremental_count", 0) + 1
                logger.info(f"[Backup] 增量备份 #{incremental_count}, watermark={watermark}")

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            key = f"db_backup/db_backup_{timestamp}_{backup_type}.json"
            data = await backup_all_tables(watermark=watermark, backup_type=backup_type)

            # R38 P1-5: 顺序修正 — 采集 → 脱敏 → 序列化 plaintext → checksum → manifest → 加密 → 上传
            # 原 R35 实现的 manifest 在 backup_all_tables 内构建(checksum 基于未脱敏数据),
            # 然后 _run_backup_loop 才调 _redact_secrets,导致 checksum 与实际 plaintext 不匹配。
            # 新顺序:1) 提取 metadata(不写入 payload) 2) 脱敏 3) 序列化 4) manifest(含 checksum)
            #         5) 加密 6) manifest envelope 补充加密元信息 7) 原子上传
            r38_metadata = data.pop("_r38_p1_5_metadata", {})

            # N-M9 + R38 P1-5: 脱敏备份数据中的敏感字段(在序列化和 checksum 之前)
            data = _redact_secrets(data)

            # R38 P1-5: 序列化 plaintext(checksum 基于此脱敏后的数据)
            plaintext = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")

            # R40 P0-6: 先加密 plaintext,获取 ciphertext_sha256,再构建 manifest(双 checksum)
            # R36 H7 + R38 P1-5: 加密 plaintext
            # R40 P0-6: 传 backup_id(=timestamp) + schema_version 绑定 AAD,防密文重放
            enc_result = encrypt_payload(
                plaintext,
                backup_id=timestamp,
                schema_version=_BACKUP_SCHEMA_VERSION,
            )
            upload_content = enc_result["ciphertext"]

            # R38 P1-5 + R40 P0-6: 生成最终 manifest
            # - plaintext_sha256: 对明文 plaintext 的 SHA-256(解密后校验)
            # - ciphertext_sha256: 对密文 ciphertext 的 SHA-256(下载后校验,来自 enc_result)
            # - backup_id: 用于 AAD 绑定(=timestamp,解密时需回传)
            manifest = _build_bundle_manifest(
                backup_data=data,
                content=plaintext,
                start_time=r38_metadata.get("start_time", datetime.now(timezone.utc)),
                end_time=r38_metadata.get("end_time", datetime.now(timezone.utc)),
                backup_type=r38_metadata.get("backup_type", backup_type),
                watermark=r38_metadata.get("watermark"),
                prev_watermark=r38_metadata.get("prev_watermark"),
                ciphertext_sha256=enc_result.get("ciphertext_sha256"),
                backup_id=timestamp,
            )

            # R38 P1-5: 将加密元信息写入 manifest envelope(含 wrapped_dek/nonce/key_id)
            if enc_result["encrypted"]:
                # R37 P1-6: manifest 记录 key_id(KEK 标识符,不可逆)
                from services.backup_crypto import get_key_id
                manifest["encryption"] = {
                    "encrypted": True,
                    "algorithm": enc_result["algorithm"],
                    "wrapped_dek": enc_result["wrapped_dek"],
                    "nonce": enc_result["nonce"],
                    "key_id": enc_result.get("key_id") or get_key_id(),
                }
            else:
                manifest["encryption"] = {"encrypted": False, "algorithm": "none"}

            # R39 P1-6: 原子上传 — 先上传临时 payload,校验 HEAD/checksum,再上传 manifest,
            # 最后原子更新 latest pointer(条件更新,避免 manifest 指向不存在 payload)
            # 原 R38 实现:先上传 manifest,再上传 ciphertext;
            #   第二步失败会留下指向不存在 payload 的 manifest(恢复时读到坏 manifest)。
            # 新顺序:1) 临时 payload 2) 校验 checksum 3) 正式 payload 4) manifest 5) latest(条件)
            payload_ct = (
                "application/octet-stream" if enc_result["encrypted"] else "application/json"
            )
            # 1. 先上传到临时 key(带随机后缀,避免与并发备份冲突)
            import secrets as _secrets
            _tmp_suffix = _secrets.token_hex(4)
            _tmp_key = f"db_backup/.tmp_{timestamp}_{_tmp_suffix}_{backup_type}.bin"
            await r2_storage.upload(_tmp_key, upload_content, payload_ct)
            # 2. 校验:重新下载并校验 checksum(R39 P1-6: 防止静默上传失败/截断)
            # R40 P0-6: 校验对象是密文,应使用 ciphertext_sha256(而非 plaintext_sha256)
            try:
                _verify_bytes = await r2_storage.download(_tmp_key)
                _verify_sha = _compute_sha256(_verify_bytes)
                _expected_cipher_sha = manifest.get("ciphertext_sha256")
                if _verify_sha != _expected_cipher_sha:
                    raise RuntimeError(
                        _i18n_t('services.db_backup.s1', str_expected_cipher_sha_16=str(_expected_cipher_sha)[:16], verify_sha_16=_verify_sha[:16])
                    )
                if len(_verify_bytes) != len(upload_content):
                    raise RuntimeError(
                        _i18n_t('services.db_backup.s2', len_upload_content=len(upload_content), len_verify_bytes=len(_verify_bytes))
                    )
                logger.debug(
                    f"[Backup] R39 P1-6 + R40 P0-6: 临时 payload 密文校验通过"
                    f"(key={_tmp_key}, cipher_sha256={_verify_sha[:16]}...)"
                )
            except Exception as verify_err:
                # 校验失败:清理临时 payload,本次备份失败(不写 manifest,不留坏指针)
                logger.error(
                    f"[Backup] R39 P1-6: 临时 payload 校验失败,清理并中止本次备份: {verify_err}"
                )
                try:
                    await r2_storage.delete(_tmp_key)
                except Exception as cleanup_err:
                    # R64 P1-07: destructive 域禁止 except pass;清理失败需记录(不掩盖原 verify 错误,原错误在下方 raise 传播)
                    logger.warning(f"[Backup] 清理临时 payload 失败 {_tmp_key}: {cleanup_err}")
                raise

            # 3. 上传到正式 key(覆盖临时 key 内容,或保留临时 key 作为额外副本)
            await r2_storage.upload(key, upload_content, payload_ct)
            # 清理临时 key(正式 key 已上传成功)
            try:
                await r2_storage.delete(_tmp_key)
            except Exception as cleanup_err:
                # R64 P1-07: destructive 域禁止 except pass;清理临时 key 失败非致命(正式 key 已上传),仅记录
                logger.warning(f"[Backup] 清理临时 key 失败 {_tmp_key}(非致命): {cleanup_err}")

            # 4. 上传 manifest(此时 payload 已确认存在且校验通过)
            manifest_content = json.dumps(manifest, default=str, ensure_ascii=False).encode("utf-8")
            await r2_storage.upload(
                f"db_backup/manifest_{timestamp}_{backup_type}.json",
                manifest_content,
                "application/json",
            )

            total_rows = sum(len(v) for v in data["tables"].values())
            # R35 P1-7 + R36 H7 + R38 P1-5 + R39 P1-6 + R40 P0-6: 日志中包含 bundle manifest 摘要
            logger.info(
                f"数据库已备份到 R2: {key} ({len(upload_content)} 字节, "
                f"{len(data['tables'])} 表, {total_rows} 行, "
                f"type={backup_type}, "
                f"commit={manifest.get('commit_sha', 'unknown')}, "
                f"plain_sha={manifest.get('plaintext_sha256', 'unknown')[:16]}..., "
                f"cipher_sha={manifest.get('ciphertext_sha256', 'unknown')[:16]}..., "
                f"backup_id={manifest.get('backup_id', '')}, "
                f"encrypted={enc_result['encrypted']})"
            )

            # R36 H7: 保存 watermark(用于下次增量)
            current_wm = manifest.get("watermark")
            if current_wm:
                await _save_watermark(current_wm, backup_type, incremental_count)

            # R39 P0-6: 删除明文 latest_<table>.json 上传逻辑
            # 原实现: 全量备份时逐表上传明文 latest_{table}.json 到 R2,
            #   绕过了 R36 H7 的 AES-256-GCM 信封加密(BACKUP_KEK),
            #   导致即使配置了 BACKUP_ENCRYPTION_REQUIRED=true,
            #   明文表数据仍可通过 latest_*.json 直接下载,违反强制加密原则。
            #
            # 修复方案:
            #   - 不再上传明文 latest_<table>.json
            #   - 加密 bundle(已上传到 timestamped key)+ manifest(含 checksum)
            #     已提供完整的最新备份状态,恢复时通过 manifest 定位加密 bundle 即可
            #   - 如需 latest 指针,可上传 latest_manifest.json(仅含 manifest key + checksum,
            #     不含表数据),此处暂不实现以保持最小改动
            #
            # 注意: 历史 R2 中已存在的 latest_<table>.json 文件需手动清理,
            #   可用 r2_storage.delete(f"db_backup/latest_{table}.json") 批量删除

            # 清理旧备份，仅保留最近 MAX_BACKUP_RETENTION 份
            try:
                await _cleanup_old_backups(r2_storage)
            except Exception as cleanup_err:
                logger.warning(f"[db_backup] 清理旧备份失败(不影响本次备份): {cleanup_err}")

        except (SystemExit, KeyboardInterrupt):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")

        # 配置重读也需异常保护:CRDB 临时不可用时 get_config 会抛异常
        # 若不保护,整个 run_db_backup 协程会崩溃,备份服务永久停止(需手动重启)
        try:
            interval_cfg = await get_config("db_backup_interval")
            if interval_cfg is None:
                interval = settings.DB_BACKUP_INTERVAL_MINUTES
            else:
                try:
                    interval = max(int(interval_cfg), 1)
                except (ValueError, TypeError):
                    interval = settings.DB_BACKUP_INTERVAL_MINUTES
        except Exception as cfg_err:
            logger.warning(f"[db_backup] 读取备份间隔失败,使用默认值: {cfg_err}")
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
        await asyncio.sleep(interval * 60)


async def _cleanup_old_backups(storage, prefix: str = "db_backup/db_backup_"):
    """清理旧备份文件，仅保留最近 MAX_BACKUP_RETENTION 份。

    按 key 名字典序（即时间序）排序，删除超出保留数量的最旧备份。
    不影响 latest_{table}.json 文件（前缀不同）。
    """
    objects = await storage.list_objects(prefix=prefix, max_keys=1000)
    if len(objects) <= MAX_BACKUP_RETENTION:
        return
    # 按 key 排序（key 含时间戳，字典序等价于时间序）
    objects.sort(key=lambda obj: obj.get("key", ""))
    to_delete = objects[:len(objects) - MAX_BACKUP_RETENTION]
    for obj in to_delete:
        key = obj.get("key", "")
        if not key:
            continue
        try:
            await storage.delete(key)
            logger.debug(f"[db_backup] 已清理旧备份: {key}")
        except Exception as e:
            logger.warning(f"[db_backup] 删除旧备份失败 {key}: {e}")
    if to_delete:
        logger.info(f"[db_backup] 清理了 {len(to_delete)} 份旧备份，保留 {MAX_BACKUP_RETENTION} 份")


async def list_backups() -> list[dict]:
    """列出 R2 中的所有备份文件，按时间倒序返回。

    供管理后台/admin_bot 调用，展示可恢复的备份列表。
    """
    # R76 O7 / 10.G: 统一对象存储配置(R2 生产 / MinIO CI)
    await configure_storage_from_settings()
    if not r2_storage._access_key:
        return []
    objects = await r2_storage.list_objects(prefix="db_backup/db_backup_", max_keys=1000)
    # 按时间倒序（key 含时间戳，倒序 = 最新在前）
    objects.sort(key=lambda obj: obj.get("key", ""), reverse=True)
    return objects


# ═══════════════════════════════════════════════════════════════
#  R35 P1-4: restore_from_backup 委托给 db_restore.py(单一 Restore Engine)
# ═══════════════════════════════════════════════════════════════

async def restore_from_backup(key: str, tables: list[str] | None = None, merge: bool = False) -> dict:
    """从 R2 备份恢复数据库。

    R35 P1-4: 委托给 services/db_restore.py 的 restore_from_backup_data(),
    消除两套恢复执行器。本函数保留向后兼容(admin_bot/callback.py 调用此入口)。

    R62 P0-01: 旧格式备份(db_backup_*.json 单文件)不支持恢复 —
               必须使用离线导入/迁移工具转换为三段式格式后再走严格验证路径。
               旧格式 key 命名 db_backup/db_backup_*.json 视为旧格式,直接 FAIL。

    R65 P0-07 / P1-07: capability-seal — 本生产入口被 capability-seal。
               生产恢复必须改走 ``RestoreOrchestrator`` 蓝绿切换路径(staging →
               active,禁止原地覆盖生产数据)。逃生舱:仅当环境变量
               ``ALLOW_LEGACY_RESTORE=1`` 时跳过 seal(供 tests/ 与 scripts/ 中
               需要直接调用旧 writer 的兼容场景使用)。生产部署绝不应配置此环境变量。

    R67 P0-06 整改(生产镜像物理移除 legacy restore 公共入口):
               在 capability-seal 之前增加硬守卫:生产环境(APP_ENV=production|
               staging)无条件拒绝调用 ``restore_from_backup()``,**不允许**
               ``ALLOW_LEGACY_RESTORE`` 解封。守卫直接读取 ``APP_ENV``,不依赖
               Settings 实例化。

    Args:
        key: R2 对象 key（如 db_backup/db_backup_20240101_120000.json）
        tables: 仅恢复指定表；None 则恢复备份中的所有表
        merge: True=增量补充(冲突保留现有数据); False=覆盖(清空后写入,默认)

    Returns:
        {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}

    Raises:
        AppError(RESTORE_LEGACY_WRITER_SEALED): 生产环境直接调用本入口(capability-seal)
        AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): 旧格式 key 不支持恢复
    """
    # R67 P0-06: 生产环境硬守卫 — 在 capability-seal 之前执行。
    # 即使设置 ALLOW_LEGACY_RESTORE=1,生产环境(APP_ENV=production|staging)
    # 也无条件拒绝调用本 legacy 入口。守卫直接读取 APP_ENV,不依赖
    # Settings 实例化,避免"未加载 Settings 即可绕过"的漏洞。
    from services._production_guard import assert_no_legacy_restore_in_production
    assert_no_legacy_restore_in_production(
        entry_point="db_backup.restore_from_backup()",
        caller="db_backup.restore_from_backup",
    )

    # R65 P0-07 / P1-07: capability-seal — 旧直接 restore writer 已被封存。
    # 生产环境调用 restore_from_backup() 必须 fail-closed,改走 RestoreOrchestrator
    # 蓝绿切换路径(staging → active,禁止原地覆盖)。
    # 逃生舱:ALLOW_LEGACY_RESTORE=1 仅限 tests/ 与 scripts/ 兼容场景使用,
    # 生产部署绝不应配置(应在系统层强制 unset)。
    if os.environ.get("ALLOW_LEGACY_RESTORE", "").lower() not in ("1", "true", "yes"):
        logger.error(
            _i18n_t(
                "diagnostics.r65.p0_07.capability_sealed",
                entry_point="db_backup.restore_from_backup()",
                caller="db_backup.restore_from_backup",
            )
        )
        raise AppError(
            ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
            params={
                "caller": "db_backup.restore_from_backup",
                "reason": "legacy_writer_sealed",
            },
        )

    # R76 O7 / 10.G: 统一对象存储配置(R2 生产 / MinIO CI)
    await configure_storage_from_settings()
    if not r2_storage._access_key:
        # R48 P1: 协议化错误码替代裸字符串 RuntimeError
        raise AppError(ErrorCodes.BACKUP_RESTORE_R2_CREDENTIAL_MISSING)

    # R62 P0-01: 检测旧格式 key 并拒绝 — 必须使用离线导入/迁移工具
    # 旧格式特征:db_backup/db_backup_*.json(单文件 JSON,无三段式 payload/manifest/COMPLETE)
    # 三段式备份:backups/{backup_id}.enc + {backup_id}.manifest.json + {backup_id}.complete
    if key.startswith("db_backup/db_backup_") and key.endswith(".json"):
        logger.error(
            f"R62 P0-01: 旧格式备份 key={key} 不支持恢复。"
            f"请使用离线导入/迁移工具将其转换为三段式格式"
            f"(payload.enc + manifest.json + COMPLETE marker)后再恢复。"
        )
        raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    # 下载备份
    content = await r2_storage.download(key)
    data = json.loads(content)

    # R35 P1-4: 委托给单一 Restore Engine
    # R61 P0-03 / R62 P0-01: 路由通过 validate_and_restore_backup_strict() 公共入口
    # (该入口构造不可伪造的 _RestoreCapability 并调用私有 _restore_from_backup_data)
    # R62 P0-01: 不再支持 skip_strict_validation=True 绕过(已移除该参数)。
    # 三段式备份需提供完整验证参数(signing_key/decryptor 等)。
    # R72 RC69: BACKUP_SIGNING_KEY 在 Settings 中定义为 str,
    # hmac.new 需要 bytes,所以做 encode 转换。
    _signing_key_raw = getattr(settings, "BACKUP_SIGNING_KEY", "") or ""
    _signing_key_bytes = (
        _signing_key_raw.encode("utf-8")
        if isinstance(_signing_key_raw, str)
        else _signing_key_raw
    )
    from services.backup_dr_validate import validate_and_restore_backup_strict
    return await validate_and_restore_backup_strict(
        data=data,
        tables=tables,
        merge=merge,
        # R62 P0-01: 严格三段式验证参数(由调用方注入)
        timestamp=str(data.get("backup_time", data.get("backup_id", ""))),
        backup_type="full",
        r2_storage=r2_storage,
        signing_key=_signing_key_bytes,
        decryptor=_build_db_backup_decryptor(),
        expected_manifest_key=str(data.get("manifest_key", "")),
        expected_backup_id=str(data.get("backup_id", "")),
        current_schema_version=str(data.get("schema_version", "")),
    )


def _build_db_backup_decryptor():
    """R62 P0-01: 构建 db_backup.restore_from_backup 用的解密器。

    生产环境应配置 BACKUP_KEK;未配置时无法走严格三段式解密路径,
    调用方应在调用前检测并提示用户使用离线迁移工具。

    R73 §5.24: 异常路径 fail-closed — 解密器构建失败(非"未配置 KEK"的预期
    情况)必须抛出 AppError 而非静默返回 None,防止调用方在不知情的情况下
    以 None 解密器执行恢复导致数据损坏。
    """
    try:
        from services.backup_crypto import is_encryption_available
        if not is_encryption_available():
            return None
        from services.backup_crypto import BackupDecryptor  # type: ignore
        return BackupDecryptor()
    except AppError:
        raise
    except Exception as e:
        logger.bind(
            component="db_backup",
            event="decryptor_build_failed_unexpected",
            error=str(e),
        ).error("")
        raise AppError(
            ErrorCodes.BACKUP_DECRYPTOR_BUILD_FAILED,
            message=f"解密器构建失败: {e}",
        ) from e


# ═══════════════════════════════════════════════════════════════
# R73 §5.3 P0-05: 不可变 backup_id + 三段式上传 readback + 结构化 evidence
# ═══════════════════════════════════════════════════════════════


def _generate_backup_id(source_sha: str = "") -> str:
    """R73 §5.3 P0-05: 生成不可变 backup_id。

    格式: ``YYYYMMDD_HHMMSS_<sha8>_<nonce8>``

    - UTC 时间戳(秒精度,与 manifest backup_started_at 一致)
    - source_sha 前 8 字符(调用方传入 git commit SHA 或业务版本标识)
    - 8 字符随机 nonce(4 字节 hex,提供同秒内唯一性)

    backup_id 一旦生成就不可变 — 三段式 payload/manifest/COMPLETE 的 R2 key
    均绑定此 backup_id,任何篡改都会导致 readback 校验或 COMPLETE marker
    签名验证失败。

    Args:
        source_sha: 调用方提供的 source SHA(优先使用);
                    为空时回退到当前 git commit SHA(_get_commit_sha())

    Returns:
        不可变 backup_id 字符串(长度固定: 17 + 1 + 8 + 1 + 8 = 35)
    """
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M%S")
    # source_sha 优先级: 调用方传入 > git commit SHA > "unknown0"
    sha_src = (source_sha or _get_commit_sha() or "unknown0")
    # 取前 8 字符并补足(若 source_sha 短于 8 字符)
    sha8 = (sha_src.lower() + "00000000")[:8]
    # 8 字符随机 nonce(4 字节 hex;token_hex 默认小写)
    nonce8 = _secrets.token_hex(4)
    return f"{ts}_{sha8}_{nonce8}"


def _compute_schema_fingerprint() -> str:
    """R73 §5.3 P0-05: 计算 BACKUP_SCHEMA 的稳定指纹(SHA-256 前 16 hex)。

    将 BACKUP_SCHEMA 中每张表的(name, source, pk_columns, columns, is_large,
    where_clause)按表名排序后序列化为 canonical JSON,计算 SHA-256 前 16 字符。

    用途:
        - 写入 manifest.schema_fingerprint(与 manifest.schema_version 互补:
          schema_version 是粗略版本号,指纹能精确检测 schema 变更)
        - 调用方可在备份前后比对指纹,确认 schema 未发生漂移

    Returns:
        16 字符 hex 指纹
    """
    fingerprint_parts = []
    for table_name in sorted(BACKUP_SCHEMA.keys()):
        schema = BACKUP_SCHEMA[table_name]
        fingerprint_parts.append({
            "name": schema.name,
            "source": schema.source,
            "pk_columns": list(schema.pk_columns),
            "columns": list(schema.columns),
            "is_large": schema.is_large,
            "where_clause": schema.where_clause,
        })
    canonical = json.dumps(
        fingerprint_parts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _get_source_database_identity() -> str:
    """R73 §5.3 P0-05: 获取源数据库身份标识(SHA-256 前 16 hex,不暴露连接串)。

    优先级:
        1. settings.COCKROACHDB_URL(生产 CRDB)— 取 SHA-256 前 16 hex
        2. cache_store DB_PATH(本地 SQLite)— 取 SHA-256 前 16 hex
        3. "unknown"(无法确定源时)

    使用 SHA-256 摘要而非原始连接串,避免在 manifest 中暴露密码/主机名等
    敏感信息,但仍能在备份前后比对以确认源数据库未发生切换。

    Returns:
        16 字符 hex 身份标识,或 "unknown"
    """
    try:
        crdb_url = getattr(settings, "COCKROACHDB_URL", "") or ""
        if crdb_url:
            return hashlib.sha256(crdb_url.encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        logger.debug(
            _i18n_t('services.db_backup.s15', error=e)
        )
    # 回退到 SQLite cache_store 路径
    try:
        from database.cache_store import DB_PATH as CACHE_DB_PATH
        return hashlib.sha256(str(CACHE_DB_PATH).encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        logger.debug(
            _i18n_t('services.db_backup.s15', error=e)
        )
    return "unknown"


def _build_signed_complete_marker(
    backup_id: str,
    manifest_key: str,
    manifest_sha256: str,
    payload_key: str,
    payload_sha256: str,
    signing_key: bytes,
    schema_version: str = "",
) -> bytes:
    """R73 §5.3 P0-05: 构建带 HMAC-SHA256 签名的 COMPLETE marker。

    委托给 services.backup_dr_validate.build_complete_marker,
    使用 versioned canonical JSON 签名载荷(R60 P0-04),含:
        backup_id + manifest_key + manifest_sha256 + payload_key + payload_sha256 + schema_version

    防止 COMPLETE marker 被伪造或字段被替换(payload_key 进入签名内容,
    避免攻击者将 COMPLETE 指向任意 payload)。

    Args:
        backup_id: 不可变 backup_id
        manifest_key: manifest.json 的 R2 key
        manifest_sha256: manifest 原始 bytes 的 SHA-256(64 hex)
        payload_key: payload.enc 的 R2 key
        payload_sha256: 密文 SHA-256(64 hex)
        signing_key: HMAC 签名密钥(bytes,来自 BACKUP_SIGNING_KEY)
        schema_version: schema 版本字符串(进入签名内容,默认用 _BACKUP_SCHEMA_VERSION)

    Returns:
        COMPLETE marker JSON bytes(含 signature + signature_version=1)
    """
    schema_ver = schema_version or _BACKUP_SCHEMA_VERSION
    return build_complete_marker(
        backup_id=backup_id,
        manifest_key=manifest_key,
        manifest_sha256=manifest_sha256,
        payload_key=payload_key,
        payload_sha256=payload_sha256,
        signing_key=signing_key,
        schema_version=schema_ver,
    )


async def _verify_object_readback(
    r2_storage,
    key: str,
    expected_sha256: str,
    expected_size: int,
) -> None:
    """R73 §5.3 P0-05: 三段式上传 readback 校验。

    上传后重新下载对象,校验:
        1. SHA-256 与 expected_sha256 一致(对象内容完整性)
        2. 字节长度与 expected_size 一致(防止截断上传)

    任何一项不匹配即抛 AppError(fail-closed,不写 status=success evidence)。

    Args:
        r2_storage: R2 存储客户端
        key: R2 对象 key
        expected_sha256: 期望的 SHA-256(64 hex)
        expected_size: 期望的字节长度

    Raises:
        AppError: readback 校验失败(SHA 不匹配 / 大小不匹配 / 下载失败)
    """
    try:
        downloaded = await r2_storage.download(key)
    except Exception as e:
        # R73 §5.3 P0-05: readback 下载失败 = 上传未持久化,fail-closed
        logger.error(
            _i18n_t('services.db_backup.s16', key=key, error=e)
        )
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": (
                    f"R73 §5.3 P0-05: readback download failed for key={key}: {e}"
                ),
            },
        )
    if downloaded is None:
        logger.error(
            _i18n_t('services.db_backup.s17', key=key)
        )
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": (
                    f"R73 §5.3 P0-05: readback returned None for key={key} "
                    "(object may not have been persisted)"
                ),
            },
        )
    actual_sha = _compute_sha256(downloaded)
    if actual_sha != expected_sha256:
        logger.error(
            _i18n_t(
                'services.db_backup.s18',
                key=key,
                expected_16=expected_sha256[:16],
                actual_16=actual_sha[:16],
            )
        )
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": (
                    f"R73 §5.3 P0-05: readback SHA mismatch for key={key} "
                    f"(expected={expected_sha256[:16]}..., actual={actual_sha[:16]}...)"
                ),
            },
        )
    if len(downloaded) != expected_size:
        logger.error(
            _i18n_t(
                'services.db_backup.s19',
                key=key,
                expected_size=expected_size,
                actual_size=len(downloaded),
            )
        )
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": (
                    f"R73 §5.3 P0-05: readback size mismatch for key={key} "
                    f"(expected={expected_size}, actual={len(downloaded)})"
                ),
            },
        )


def _record_secret_evidence(signing_key: bytes | str | None) -> dict:
    """R73 §5.3 P0-05: 记录 RC 备份 evidence 中的密钥存在性(不暴露密钥值)。

    要求(R73 §5.3):
        RC backup evidence 不写 secret 值,但记录:
            - secret_present: bool —— BACKUP_SIGNING_KEY 是否非空
            - secret_length_range: str —— 长度区间(分桶,避免暴露精确长度)
            - key_id: str —— KEK 标识符(已是 SHA-256 摘要,非密钥本身)

    长度区间分桶策略:
        - "empty"               —— 0 字节
        - "short (<16)"         —— 1-15 字节(过短,不安全)
        - "normal (16-32)"      —— 16-32 字节(常见 HMAC key 长度)
        - "long (33-64)"        —— 33-64 字节
        - "extra-long (>64)"    —— >64 字节

    Args:
        signing_key: BACKUP_SIGNING_KEY 的原始值(str 或 bytes),
                     None 视为未配置

    Returns:
        {"secret_present": bool, "secret_length_range": str, "key_id": str}
    """
    if signing_key is None:
        return {
            "secret_present": False,
            "secret_length_range": "empty",
            "key_id": "",
        }
    # 计算 byte 长度(str 用 utf-8 编码,bytes 直接取长度)
    if isinstance(signing_key, str):
        byte_len = len(signing_key.encode("utf-8"))
    elif isinstance(signing_key, (bytes, bytearray)):
        byte_len = len(signing_key)
    else:
        # 未知类型,按未配置处理
        return {
            "secret_present": False,
            "secret_length_range": "empty",
            "key_id": "",
        }
    if byte_len == 0:
        return {
            "secret_present": False,
            "secret_length_range": "empty",
            "key_id": "",
        }
    # 分桶(避免精确长度暴露,但仍能用于安全审计)
    if byte_len < 16:
        length_range = "short (<16)"
    elif byte_len <= 32:
        length_range = "normal (16-32)"
    elif byte_len <= 64:
        length_range = "long (33-64)"
    else:
        length_range = "extra-long (>64)"
    # key_id 来自 KEK(若配置),非 BACKUP_SIGNING_KEY 本身
    try:
        kek_key_id = get_key_id()
    except Exception:
        kek_key_id = ""
    return {
        "secret_present": True,
        "secret_length_range": length_range,
        "key_id": kek_key_id,
    }


# ═══════════════════════════════════════════════════════════════
# R72 P0-10: 一次性 backup CLI (--once --output-json)
# ═══════════════════════════════════════════════════════════════


async def backup_once(
    output_json_path: str | None = None,
    backup_type: str = "full",
    reason: str = "",
    source_sha: str = "",
    timeout: int = 240,
) -> dict:
    """R72 P0-10 / RC60 / R73 §5.3 P0-05: 执行一次性备份(不进入 daemon 循环),带整体超时。

    用于 RC 门禁中的真实 backup→restore 演练:
      1. 初始化 DB + R2(不计入 timeout)
      2. 生成不可变 backup_id(UTC 时间戳 + source_sha + 随机 nonce)
      3. 执行单次全量备份(backup_all_tables → encrypt → 3-对象上传)
      4. 上传 payload.enc + manifest.json + COMPLETE marker(后者最后)
      5. 对每个对象做 readback 校验(SHA-256 + 字节大小)
      6. 保存 watermark
      7. 输出结构化 JSON evidence(含 secret_present / secret_length_range / key_id)

    R72 RC60: 为防止 asyncpg 连接无超时导致 compose-runtime-e2e
    backup_restore 阶段挂起 600s,在此处添加 asyncio.wait_for 整体超时。
    timeout 秒未完成则返回 status="timeout" 的 evidence 并由 main() 退出码 1 标记失败。
    240s 默认值远小于编排器 540s 超时(60s 余量供 init_db / cleanup)。

    R73 §5.3 P0-05 整改要点:
      - 强制 full once: backup_type 必须为 "full"(传 "incremental" 即 AppError)
      - 不可变 backup_id: UTC 秒精度时间戳 + 8 字符 source_sha + 8 字符随机 nonce
      - 三段式上传: payload.enc + manifest.json + COMPLETE marker(后者最后)
      - Readback 校验: 对每个对象重新下载,校验 SHA-256 + 字节大小,任一不匹配即 AppError
      - 结构化 evidence: 含 secret_present / secret_length_range / key_id(不暴露密钥值)
      - Fail-closed: 任何 R2/CRDB/crypto/signing/timeout 错误均抛 AppError,
        不生成 status="success" 部分证据

    Args:
        output_json_path: 可选,将 evidence JSON 写入文件
        backup_type: 备份类型(R73 §5.3 P0-05 强制 "full";"incremental" 会被拒绝)
        reason: 备份原因(写入 evidence.reason,用于审计)
        source_sha: 调用方提供的 source SHA(优先用,空则回退到 git commit SHA)
        timeout: 整体超时秒数(默认 240),覆盖 backup_all_tables → encrypt → upload → readback

    Returns:
        包含 backup_id / manifest / evidence 的字典。
        status="success" 表示成功;status="timeout" 表示整体超时(调用方应视为失败)。
        失败时抛 AppError,不返回 status="success" 部分证据。

    Raises:
        AppError: R2 凭证缺失 / 加密不可用 / signing_key 缺失 /
                  backup_type != "full"(full once 强制)/
                  上传失败 / readback 校验失败 / COMPLETE marker 构建失败
    """
    # R72 RC62: init_db 移入 _do_backup_inner() 内部,受 asyncio.wait_for 保护
    # 原实现将 init_db 放在 wait_for 之外,导致 init_db 卡住时 --timeout 完全失效,
    # 编排器只能等到 600s 超时强杀,无结构化 evidence 输出。
    async def _do_backup_inner() -> dict:
        # R73 §5.3 P0-05: 强制 full once — backup_type 必须为 "full"
        if backup_type != "full":
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={
                    "reason": (
                        f"R73 §5.3 P0-05: backup_once enforces full once — "
                        f"backup_type must be 'full', got '{backup_type}'. "
                        f"Incremental backups are not supported in one-shot mode."
                    ),
                },
            )

        # 确保数据库连接池已初始化(init_db 计入 timeout,防止 connect() 卡住)
        if not db_client.is_connected:
            from database.session import init_db
            await init_db()

        # R76 O7 / 10.G: 统一对象存储配置(R2 生产 / MinIO CI)
        await configure_storage_from_settings()
        if not r2_storage._access_key:
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_R2_CREDENTIAL_MISSING,
                params={
                    "reason": (
                        "R72 P0-10: 对象存储凭证缺失 — backup_once 需要凭证,"
                        "不得在缺少凭证时返回成功(fail-closed)"
                    ),
                },
            )

        # 加密可用性检查(R72 P0-10: 备份必须加密,绝不上传明文)
        if not is_encryption_available():
            raise AppError(
                ErrorCodes.BACKUP_DECRYPT_KEK_MISSING,
                params={
                    "reason": "R72 P0-10: 加密不可用 — backup_once 需要 BACKUP_KEK 配置",
                },
            )

        # R73 §5.3 P0-05: BACKUP_SIGNING_KEY 必须配置(用于 COMPLETE marker HMAC 签名)
        # Settings 中定义为 str(环境变量总是 str),hmac.new 要求 bytes,故做 encode 转换
        _signing_key_raw = getattr(settings, "BACKUP_SIGNING_KEY", "") or ""
        if not _signing_key_raw:
            raise AppError(
                ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                params={
                    "reason": (
                        "R73 §5.3 P0-05: BACKUP_SIGNING_KEY 未配置 — "
                        "backup_once 需要此密钥为 COMPLETE marker 做 HMAC-SHA256 签名,"
                        "restore 端会用同一密钥验签(fail-closed)"
                    ),
                },
            )
        _signing_key_bytes = (
            _signing_key_raw.encode("utf-8")
            if isinstance(_signing_key_raw, str)
            else _signing_key_raw
        )

        # R73 §5.3 P0-05: 生成不可变 backup_id(UTC 时间戳 + source_sha + 随机 nonce)
        # backup_once 强制 full,不读取 watermark(避免被篡改影响 backup_id)
        backup_id = _generate_backup_id(source_sha=source_sha)
        backup_type_inner = "full"  # 强制 full,不再读 last_wm
        incremental_count = 0

        # 备份数据采集(全量)
        data = await backup_all_tables(watermark=None, backup_type=backup_type_inner)

        r38_metadata = data.pop("_r38_p1_5_metadata", {})
        data = _redact_secrets(data)
        plaintext = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")

        # 加密 plaintext(用 backup_id 作 AAD 绑定)
        enc_result = encrypt_payload(
            plaintext,
            backup_id=backup_id,
            schema_version=_BACKUP_SCHEMA_VERSION,
        )
        upload_content = enc_result["ciphertext"]

        # 计算 R2 key(三段式,绑定 backup_id)
        payload_key = f"db_backup/payload_{backup_id}_{backup_type_inner}.enc"
        manifest_key = f"db_backup/manifest_{backup_id}_{backup_type_inner}.json"
        complete_key = f"db_backup/COMPLETE_{backup_id}_{backup_type_inner}.COMPLETE"

        # R73 §5.3 P0-05: 计算 schema_fingerprint + source_database_identity(强信任链字段)
        schema_fingerprint = _compute_schema_fingerprint()
        source_database_identity = _get_source_database_identity()

        # R73 §5.3 P0-05: 计算 manifest bytes(用于 SHA-256 + COMPLETE marker 绑定)
        # 必须在 _build_bundle_manifest 之后,基于最终 manifest dict 序列化
        # manifest 字段在加密元信息补充后才完整,故分两步构建
        manifest = _build_bundle_manifest(
            backup_data=data,
            content=plaintext,
            start_time=r38_metadata.get("start_time", datetime.now(timezone.utc)),
            end_time=r38_metadata.get("end_time", datetime.now(timezone.utc)),
            backup_type=r38_metadata.get("backup_type", backup_type_inner),
            watermark=r38_metadata.get("watermark"),
            prev_watermark=r38_metadata.get("prev_watermark"),
            ciphertext_sha256=enc_result.get("ciphertext_sha256"),
            backup_id=backup_id,
            # R73 §5.3 P0-05 强信任链字段
            source_sha=source_sha or _get_commit_sha(),
            source_database_identity=source_database_identity,
            schema_fingerprint=schema_fingerprint,
            payload_key=payload_key,
            manifest_key=manifest_key,
            key_id=enc_result.get("key_id") or get_key_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        if enc_result["encrypted"]:
            manifest["encryption"] = {
                "encrypted": True,
                "algorithm": enc_result["algorithm"],
                "wrapped_dek": enc_result["wrapped_dek"],
                "nonce": enc_result["nonce"],
                "key_id": enc_result.get("key_id") or get_key_id(),
            }
        else:
            manifest["encryption"] = {"encrypted": False, "algorithm": "none"}

        # R73 §5.3 P0-05: 三段式上传(payload → manifest → COMPLETE,后者最后)
        # 顺序保证:COMPLETE marker 最后上传,仅在 payload + manifest 已确认存在后才写入;
        # 若任一前置步骤失败,COMPLETE 不会被写入,restore 端会因缺少 COMPLETE 而 fail-closed。
        payload_ct = (
            "application/octet-stream" if enc_result["encrypted"] else "application/json"
        )
        # 1. 上传 payload.enc
        await r2_storage.upload(payload_key, upload_content, payload_ct)
        # 1a. readback 校验 payload
        await _verify_object_readback(
            r2_storage, payload_key,
            expected_sha256=manifest["ciphertext_sha256"],
            expected_size=len(upload_content),
        )

        # 2. 上传 manifest.json(此时 payload 已确认存在且校验通过)
        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        await r2_storage.upload(
            manifest_key, manifest_bytes, "application/json",
        )
        # 2a. readback 校验 manifest(SHA = sha256(manifest_bytes),size = len(manifest_bytes))
        await _verify_object_readback(
            r2_storage, manifest_key,
            expected_sha256=_compute_sha256(manifest_bytes),
            expected_size=len(manifest_bytes),
        )

        # 3. 上传 COMPLETE marker(最后,绑定 backup_id + manifest_key + payload_key + 各 SHA)
        complete_marker_bytes = _build_signed_complete_marker(
            backup_id=backup_id,
            manifest_key=manifest_key,
            manifest_sha256=_compute_sha256(manifest_bytes),
            payload_key=payload_key,
            payload_sha256=manifest["ciphertext_sha256"],
            signing_key=_signing_key_bytes,
        )
        await r2_storage.upload(
            complete_key, complete_marker_bytes, "application/json",
        )
        # 3a. readback 校验 COMPLETE marker
        await _verify_object_readback(
            r2_storage, complete_key,
            expected_sha256=_compute_sha256(complete_marker_bytes),
            expected_size=len(complete_marker_bytes),
        )

        # 保存 watermark(用于后续增量备份调度;即使 backup_once 也会保存,
        # 让 daemon 模式下次启动时能从正确位置开始增量)
        current_wm = manifest.get("watermark")
        await _save_watermark(current_wm, backup_type_inner, incremental_count)

        # R73 §5.3 P0-05: 构建结构化 evidence(不暴露密钥值,仅记录存在性 + 长度区间 + key_id)
        secret_evidence = _record_secret_evidence(_signing_key_raw)
        # R73 P0-05: 三对象结构化 evidence — 同时提供独立字段(向后兼容)
        # 与 objects dict(供 compose_runtime_e2e.phase_full_backup_to_r2 严格校验)
        evidence = {
            "backup_id": backup_id,
            "backup_type": backup_type_inner,
            "reason": reason,
            "manifest": manifest,
            "payload_key": payload_key,
            "manifest_key": manifest_key,
            "complete_key": complete_key,
            # R73 P0-05: 三对象 readback 校验结构化字段
            # compose_runtime_e2e.phase_full_backup_to_r2 读取此 dict 验证三对象存在
            "objects": {
                "payload": payload_key,
                "manifest": manifest_key,
                "COMPLETE": complete_key,
            },
            "status": "success",
            # R73 §5.3 P0-05: secret evidence(不暴露密钥值)
            "secret_present": secret_evidence["secret_present"],
            "secret_length_range": secret_evidence["secret_length_range"],
            "key_id": secret_evidence["key_id"],
            # R73 §5.3 P0-05: readback 已通过标记(便于审计)
            "readback_verified": True,
        }

        if output_json_path:
            from pathlib import Path
            Path(output_json_path).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

        logger.info(
            _i18n_t(
                'services.db_backup.s8', backup_id=backup_id, backup_type=backup_type_inner,
            )
        )
        logger.info(
            _i18n_t(
                'services.db_backup.s20',
                backup_id=backup_id,
                payload_key=payload_key,
                manifest_key=manifest_key,
                complete_key=complete_key,
            )
        )
        return evidence

    try:
        # R72 RC60: 整体超时包裹 — 防止 asyncpg 连接无超时挂起导致
        # compose-runtime-e2e backup_restore 阶段被编排器 600s 超时强杀。
        # 超时后返回结构化 evidence(status="timeout"),由 main() 退出码 1 标记失败。
        return await asyncio.wait_for(_do_backup_inner(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时仍写入 evidence 文件,供编排器解析定位失败原因
        timeout_evidence = {
            "backup_id": "",
            "backup_type": "timeout",
            "manifest": {},
            "payload_key": "",
            "manifest_key": "",
            "status": "timeout",
            "error": _i18n_t('services.db_backup.s11', timeout=timeout),
        }
        if output_json_path:
            try:
                from pathlib import Path
                Path(output_json_path).write_text(
                    json.dumps(timeout_evidence, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except OSError as write_err:
                logger.warning(
                    _i18n_t('services.db_backup.s10', error=write_err)
                )
        logger.error(
            _i18n_t('services.db_backup.s12', timeout=timeout)
        )
        return timeout_evidence
    finally:
        # R72 RC62: cleanup 操作添加独立超时保护,防止 close_db/close 卡住
        # 导致整个 backup_once 在 timeout 后仍无法返回。
        # 即使 timeout 已触发,_do_backup_inner 内部已申请的资源仍需释放,
        # 但释放本身也不能无限等待(否则编排器仍会 600s 超时强杀)。
        try:
            from database.session import close_db
            await asyncio.wait_for(close_db(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning(
                _i18n_t('services.db_backup.s10', error="close_db timeout(15s)")
            )
        except Exception as close_err:
            logger.warning(_i18n_t('services.db_backup.s9', error=close_err))
        try:
            await asyncio.wait_for(r2_storage.close(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning(
                _i18n_t('services.db_backup.s10', error="r2_storage.close timeout(15s)")
            )
        except Exception as close_err:
            logger.warning(_i18n_t('services.db_backup.s10', error=close_err))


def main():
    """R72 P0-10 / RC60 / R73 §5.3 P0-05: db_backup CLI 入口。

    用法:
      # 一次性备份(不进入 daemon 循环)
      python -m services.db_backup backup --once --output-json /tmp/backup_evidence.json
      # 带自定义超时(秒),用于 compose-runtime-e2e 等 CI 场景
      python -m services.db_backup backup --once --timeout 240 --output-json /tmp/backup_evidence.json
      # R73 §5.3 P0-05: 带备份类型/原因/source SHA(用于审计与不可变 backup_id 绑定)
      python -m services.db_backup backup --once --type full \\
          --reason "rc_release" --source-sha "$GIT_SHA" \\
          --output-json /tmp/backup_evidence.json

      # daemon 模式(原有行为,通过 run_all.py 调用)
      python -m services.db_backup daemon

    R73 §5.3 P0-05:
      - --type incremental 会被 backup_once 拒绝(full once 强制),
        CLI 允许传入但函数 fail-closed,便于审计尝试。
      - --source-sha 用于绑定不可变 backup_id(空则回退 git commit SHA)。
      - --reason 写入 evidence.reason,供审计追溯备份触发原因。
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=_i18n_t('services.db_backup.s3')
    )
    subparsers = parser.add_subparsers(dest="command")

    # backup 子命令
    backup_parser = subparsers.add_parser("backup", help=_i18n_t('services.db_backup.s4'))
    backup_parser.add_argument(
        "--once", action="store_true",
        help=_i18n_t('services.db_backup.s5'),
    )
    backup_parser.add_argument(
        "--output-json", type=str, default=None,
        help=_i18n_t('services.db_backup.s6'),
    )
    backup_parser.add_argument(
        # R72 RC60: backup --once 整体超时(秒),防止 asyncpg 连接卡住导致编排器 600s 超时。
        # 默认 240s,远小于编排器 540s 超时,留 60s 余量给 init_db/cleanup。
        "--timeout", type=int, default=240,
        help=_i18n_t('services.db_backup.s13'),
    )
    backup_parser.add_argument(
        # R73 §5.3 P0-05: 备份类型(full/incremental)。
        # backup_once 强制 full once,传 incremental 会被 AppError 拒绝,
        # CLI 允许传入以便审计尝试(fail-closed 而非隐藏选项)。
        "--type", type=str, default="full", choices=["full", "incremental"],
        help=_i18n_t('services.db_backup.s21'),
    )
    backup_parser.add_argument(
        # R73 §5.3 P0-05: 备份原因(写入 evidence.reason,用于审计追溯)。
        "--reason", type=str, default="",
        help=_i18n_t('services.db_backup.s22'),
    )
    backup_parser.add_argument(
        # R73 §5.3 P0-05: source SHA(优先于 git commit SHA 绑定不可变 backup_id)。
        "--source-sha", type=str, default="",
        help=_i18n_t('services.db_backup.s23'),
    )

    # daemon 子命令
    daemon_parser = subparsers.add_parser("daemon", help=_i18n_t('services.db_backup.s7'))

    args = parser.parse_args()

    if args.command == "backup" and args.once:
        # R72 RC60: 校验 timeout 合法性(>=30s 才有意义)
        if args.timeout < 30:
            print(
                _i18n_t('services.db_backup.s14', timeout=args.timeout),
                file=__import__("sys").stderr,
            )
            return 1
        result = asyncio.run(
            backup_once(
                output_json_path=args.output_json,
                backup_type=args.type,
                reason=args.reason,
                source_sha=args.source_sha,
                timeout=args.timeout,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        # R72 RC60: status != "success" 视为失败(exit 1),
        # 让编排器 fail-closed 而非误判成功。
        if result.get("status") != "success":
            return 1
    elif args.command == "daemon":
        asyncio.run(run_db_backup())
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
