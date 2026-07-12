from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

from loguru import logger

from config import settings
from database.session import _client as db_client, get_config, _validate_identifier
from storage.r2 import _r2 as r2_storage, configure_r2_dynamic
from services.backup_schema import (
    BACKUP_SCHEMA, get_backup_tables, get_conflict_col,
    get_tables_by_source,
)
from services.backup_crypto import (
    encrypt_payload,
    is_encryption_available,
)

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
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:12]
    except Exception:
        pass
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
) -> dict:
    """构建 bundle manifest(R35 P1-7, R36 H7 增强)。

    Bundle 包含:
    - commit SHA(git rev-parse HEAD 或环境变量)
    - schema version(backup_schema 版本)
    - 每表行数
    - SHA-256 checksum(整个 backup JSON 的校验和)
    - 开始/结束时间
    - source 标记(crdb/sqlite/relay_sqlite)
    - R36 H7: backup_type (full/incremental)
    - R36 H7: watermark (本次备份的 updated_at 上界)
    - R36 H7: encryption (加密元数据)
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

    manifest = {
        "version": "3.0",  # R36: manifest v3(含 backup_type/watermark/encryption)
        "commit_sha": _get_commit_sha(),
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "checksum_sha256": _compute_sha256(content),
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
    return manifest


# ═══════════════════════════════════════════════════════════════
#  R35 P1-5: 按 source 分组的备份快照器
# ═══════════════════════════════════════════════════════════════

async def _backup_crdb_tables(tables: list[str], watermark: str | None = None) -> dict:
    """备份 CRDB 表(走 CockroachDB SELECT *)。

    R35 P1-5: 仅备份 source="crdb" 的表,避免对 SQLite-only 表执行 CRDB 查询。
    R36 H7: 支持 watermark 增量备份(只查 updated_at > watermark 的行)。
    R37 P1-5: 增量备份同时捕捉 deleted_at > watermark 的软删除行,
              解决仅靠 updated_at 无法捕捉删除事件的问题。
    """
    results = {}
    for table in sorted(tables):
        try:
            safe_name = _validate_identifier(table)
            conditions = []
            # 表级 WHERE 条件(如 status = 'active')
            table_where = _TABLE_WHERE.get(table)
            if table_where:
                conditions.append(table_where)
            # R36 H7 + R37 P1-5: 增量 watermark 条件
            # 同时检查 updated_at(常规变更)和 deleted_at(软删除)
            # 任一列 > watermark 都纳入本次增量备份
            if watermark:
                schema = BACKUP_SCHEMA.get(table)
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
                    sql = f'SELECT * FROM "{safe_name}" WHERE {where_clause}'
                    records = await db_client.fetch(sql, watermark)
                else:
                    sql = f'SELECT * FROM "{safe_name}" WHERE {where_clause}'
                    records = await db_client.fetch(sql)
            else:
                sql = f'SELECT * FROM "{safe_name}"'
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
    max_ts = datetime.now(timezone.utc).isoformat()
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
        except Exception:
            pass
        try:
            await r2_storage.close()
        except Exception:
            pass
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

    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
    if not r2_storage._access_key or not r2_storage._secret_key:
        logger.warning("R2 凭证未配置(.env 和 config 表均无),数据库备份跳过")
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

            # R38 P1-5: 生成最终 manifest(checksum 基于已脱敏的 plaintext)
            # manifest 含 key_id 标识(不含 wrapped_dek/nonce 明文,这些放 envelope 字段)
            manifest = _build_bundle_manifest(
                backup_data=data,
                content=plaintext,
                start_time=r38_metadata.get("start_time", datetime.now(timezone.utc)),
                end_time=r38_metadata.get("end_time", datetime.now(timezone.utc)),
                backup_type=r38_metadata.get("backup_type", backup_type),
                watermark=r38_metadata.get("watermark"),
                prev_watermark=r38_metadata.get("prev_watermark"),
            )

            # R36 H7 + R38 P1-5: 加密 plaintext
            enc_result = encrypt_payload(plaintext)
            upload_content = enc_result["ciphertext"]

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

            # R38 P1-5: 原子上传 — manifest 单独存储,ciphertext 单独存储
            manifest_content = json.dumps(manifest, default=str, ensure_ascii=False).encode("utf-8")
            await r2_storage.upload(
                f"db_backup/manifest_{timestamp}_{backup_type}.json",
                manifest_content,
                "application/json",
            )
            await r2_storage.upload(
                key, upload_content,
                "application/octet-stream" if enc_result["encrypted"] else "application/json",
            )

            total_rows = sum(len(v) for v in data["tables"].values())
            # R35 P1-7 + R36 H7 + R38 P1-5: 日志中包含 bundle manifest 摘要
            logger.info(
                f"数据库已备份到 R2: {key} ({len(upload_content)} 字节, "
                f"{len(data['tables'])} 表, {total_rows} 行, "
                f"type={backup_type}, "
                f"commit={manifest.get('commit_sha', 'unknown')}, "
                f"checksum={manifest.get('checksum_sha256', 'unknown')[:16]}..., "
                f"encrypted={enc_result['encrypted']})"
            )

            # R36 H7: 保存 watermark(用于下次增量)
            current_wm = manifest.get("watermark")
            if current_wm:
                await _save_watermark(current_wm, backup_type, incremental_count)

            # 逐表上传 latest(仅全量备份时更新 latest,避免增量覆盖)
            if backup_type == "full":
                for table in data["tables"]:
                    t_content = json.dumps(
                        data["tables"][table], default=str, ensure_ascii=False
                    ).encode("utf-8")
                    await r2_storage.upload(
                        f"db_backup/latest_{table}.json",
                        t_content,
                        "application/json",
                    )

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
    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
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

    Args:
        key: R2 对象 key（如 db_backup/db_backup_20240101_120000.json）
        tables: 仅恢复指定表；None 则恢复备份中的所有表
        merge: True=增量补充(冲突保留现有数据); False=覆盖(清空后写入,默认)

    Returns:
        {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}
    """
    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
    if not r2_storage._access_key:
        raise RuntimeError("R2 凭证未配置(.env 和 config 表均无)，无法恢复")

    # 下载备份
    content = await r2_storage.download(key)
    data = json.loads(content)

    # R35 P1-4: 委托给单一 Restore Engine
    from services.db_restore import restore_from_backup_data
    return await restore_from_backup_data(
        data, tables=tables, merge=merge,
    )
