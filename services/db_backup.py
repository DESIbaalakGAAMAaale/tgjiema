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
    "kv_config",
}

_LARGE_TABLES = {
    "file_records", "codes", "decode_logs", "jobs", "message_backups",
    "pending_uploads", "rotate_log",
}

BACKUP_TABLES = SMALL_TABLES

MAX_ROWS_PER_TABLE = 5000

# N-M9: 备份中需要脱敏的敏感字段（不改原库，仅脱敏备份 JSON）
_SENSITIVE_FIELDS = {"r2_secret_key", "r2_access_key", "api_hash"}
_REDACTED_VALUE = "***REDACTED***"


def _redact_secrets(data: dict) -> dict:
    """脱敏备份数据中的敏感字段，不影响原始数据库。"""
    tables = data.get("tables", {})
    for table_name, rows in tables.items():
        if table_name in ("backup_config", "relay_accounts"):
            for row in rows:
                for key in list(row.keys()):
                    if key.lower() in _SENSITIVE_FIELDS:
                        row[key] = _REDACTED_VALUE
    return data


async def backup_all_tables() -> dict:
    """仅备份小元数据表（单表 <= 几百行），避免全表扫描大表消耗大量 RU。

    大表（file_records/codes/decode_logs/jobs 等）跳过：
    - file_records/codes 数据可从 Telegram 频道重新索引
    - decode_logs/jobs 是短期流水数据，无需长期备份
    """
    results = {}
    async with db_client._pool.acquire() as conn:
        for table in sorted(BACKUP_TABLES):
            try:
                safe_name = table.replace('"', '""')
                records = await conn.fetch(
                    f'SELECT * FROM "{safe_name}" LIMIT {MAX_ROWS_PER_TABLE}'
                )
                results[table] = [dict(r) for r in records]
                logger.debug(f"[Backup] {table}: {len(records)} 行")
            except Exception as e:
                logger.debug(f"[Backup] 跳过表 {table}: {e}")

    return {"backup_time": datetime.now(timezone.utc).isoformat(), "tables": results}


async def run_db_backup():
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

    if db_client._pool is None:
        try:
            from database import init_db
            await init_db()
        except Exception as e:
            logger.warning(f"数据库连接初始化失败,跳过备份: {e}")
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
        except BaseException as e:
            logger.error(f"数据库备份失败: {e}")

        interval_cfg = await get_config("db_backup_interval")
        if interval_cfg is None:
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
        else:
            interval = int(interval_cfg)
        await asyncio.sleep(interval * 60)
