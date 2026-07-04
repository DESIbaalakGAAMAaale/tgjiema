import asyncio
import json
from datetime import datetime, timezone

from loguru import logger

from config import settings
from database.session import _client as db_client, get_config
from storage.r2 import _r2 as r2_storage


SMALL_TABLES = {
    "cells", "users", "spare_pool", "backup_config", "rotation_config",
    "relay_accounts", "code_bot_mapping", "external_code_mapping",
    "kv_config", "message_backups",
    # R-1: codes/file_records 纳入备份（取件码→频道/消息的映射是核心数据，
    # 无外部备份则为单点故障。每表上限 5000 行，file_records 仅备份 active 状态）
    "codes", "file_records",
}

_LARGE_TABLES = {
    "decode_logs", "jobs", "pending_uploads", "rotate_log",
}

BACKUP_TABLES = SMALL_TABLES

# 每个表可选的 WHERE 条件，用于过滤备份范围
_TABLE_WHERE = {
    "file_records": "status = 'active'",  # 仅备份活跃文件，跳过已过期/删除
}

MAX_ROWS_PER_TABLE = 5000

# N-M9: 备份中需要脱敏的敏感字段（不改原库，仅脱敏备份 JSON）
_SENSITIVE_FIELDS = {"r2_secret_key", "r2_access_key", "api_hash"}
_REDACTED_VALUE = "***REDACTED***"


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


async def backup_all_tables() -> dict:
    """备份核心元数据表（含 codes/file_records 的取件映射）。

    大表（decode_logs/jobs/pending_uploads/rotate_log）跳过：
    - decode_logs/jobs 是短期流水数据，无需长期备份
    - pending_uploads 是瞬时状态，重启后从频道重放
    - rotate_log 是审计日志，数据量大但非核心
    """
    results = {}
    async with db_client._pool.acquire() as conn:
        for table in sorted(BACKUP_TABLES):
            try:
                safe_name = table.replace('"', '""')
                where = _TABLE_WHERE.get(table)
                # message_backups 是核心映射表，行数可能远超 5000，取消上限
                limit = "" if table == "message_backups" else f" LIMIT {MAX_ROWS_PER_TABLE}"
                if where:
                    sql = f'SELECT * FROM "{safe_name}" WHERE {where}{limit}'
                else:
                    sql = f'SELECT * FROM "{safe_name}"{limit}'
                records = await conn.fetch(sql)
                results[table] = [dict(r) for r in records]
                logger.debug(f"[Backup] {table}: {len(records)} 行")
            except Exception as e:
                logger.debug(f"[Backup] 跳过表 {table}: {e}")

    return {"backup_time": datetime.now(timezone.utc).isoformat(), "tables": results}


async def run_db_backup():
    # 确保数据库连接池已初始化（某些场景下 _auto_seed 可能未成功初始化）
    if db_client._pool is None:
        try:
            from database.session import init_db
            await init_db()
        except Exception as e:
            logger.warning(f"数据库连接初始化失败,跳过备份: {e}")
            return

    enabled_cfg = await get_config("db_backup_enabled")
    if enabled_cfg is None:
        enabled = settings.DB_BACKUP_ENABLED
    else:
        enabled = enabled_cfg.lower() == "true"
    if not enabled:
        logger.info("数据库备份未启用(DB_BACKUP_ENABLED=false),跳过启动")
        return

    if not settings.R2_ACCOUNT_ID or not settings.R2_ACCESS_KEY_ID or not settings.R2_SECRET_ACCESS_KEY:
        logger.warning("R2 凭证未配置,数据库备份跳过")
        return

    r2_storage.configure(
        account_id=settings.R2_ACCOUNT_ID,
        access_key=settings.R2_ACCESS_KEY_ID,
        secret_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET_NAME,
        endpoint=settings.R2_ENDPOINT if settings.R2_ENDPOINT else None,
    )
    await r2_storage.connect()

    interval_cfg = await get_config("db_backup_interval")
    if interval_cfg is None:
        interval = settings.DB_BACKUP_INTERVAL_MINUTES
    else:
        interval = int(interval_cfg)
    logger.info("CockroachDB 数据库备份服务启动,间隔 {} 分钟", interval)

    while True:
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            key = f"db_backup/db_backup_{timestamp}.json"
            data = await backup_all_tables()
            # N-M9: 脱敏备份数据中的敏感字段
            data = _redact_secrets(data)
            content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
            await r2_storage.upload(key, content, "application/json")
            total_rows = sum(len(v) for v in data["tables"].values())
            logger.info(
                f"数据库已备份到 R2: {key} ({len(content)} 字节, "
                f"{len(data['tables'])} 表, {total_rows} 行)"
            )

            for table in data["tables"]:
                t_content = json.dumps(
                    data["tables"][table], default=str, ensure_ascii=False
                ).encode("utf-8")
                await r2_storage.upload(
                    f"db_backup/latest_{table}.json",
                    t_content,
                    "application/json",
                )

        except (SystemExit, KeyboardInterrupt):
            raise
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            logger.error(f"数据库备份失败: {e}")

        interval_cfg = await get_config("db_backup_interval")
        if interval_cfg is None:
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
        else:
            interval = int(interval_cfg)
        await asyncio.sleep(interval * 60)
