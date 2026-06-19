import asyncio
import json
from datetime import datetime, timezone

from loguru import logger

from config import settings
from database.session import _client as db_client
from storage.r2 import _r2 as r2_storage


async def backup_all_tables() -> dict:
    """动态发现所有用户表并备份,排除 CockroachDB 系统表。"""
    results = {}
    async with db_client._pool.acquire() as conn:
        # 动态查询所有用户表(排除系统 schema)
        tables = await conn.fetch("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        table_names = [r["table_name"] for r in tables]

        if not table_names:
            # 兜底:手动列出所有表
            table_names = [
                "users", "file_records", "decode_logs", "cells", "codes",
                "jobs", "rotate_log", "pending_uploads",
                "spare_pool", "backup_config", "code_bot_mapping",
                "message_backups",
            ]

        for table in table_names:
            records = await conn.fetch(f"SELECT * FROM {table}")
            results[table] = [dict(r) for r in records]

    data = {"backup_time": datetime.now(timezone.utc).isoformat(), "tables": results}
    return data


async def run_db_backup():
    if not settings.DB_BACKUP_ENABLED:
        logger.info("数据库备份未启用(DB_BACKUP_ENABLED=false),跳过启动")
        return

    if not settings.R2_ACCOUNT_ID or not settings.R2_ACCESS_KEY_ID or not settings.R2_SECRET_ACCESS_KEY:
        logger.warning("R2 凭证未配置,数据库备份跳过")
        return

    from database import init_db
    if db_client._pool is None:
        await init_db()

    r2_storage.configure(
        account_id=settings.R2_ACCOUNT_ID,
        access_key=settings.R2_ACCESS_KEY_ID,
        secret_key=settings.R2_SECRET_ACCESS_KEY,
        bucket=settings.R2_BUCKET_NAME,
        endpoint=settings.R2_ENDPOINT if settings.R2_ENDPOINT else None,
    )
    await r2_storage.connect()

    logger.info("CockroachDB 数据库备份服务启动,间隔 {} 分钟", settings.DB_BACKUP_INTERVAL_MINUTES)

    while True:
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            key = f"db_backup/db_backup_{timestamp}.json"
            data = await backup_all_tables()
            content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
            await r2_storage.upload(key, content, "application/json")
            logger.info(f"数据库已备份到 R2: {key} ({len(content)} 字节)")

            for table in data["tables"]:
                t_content = json.dumps(
                    data["tables"][table], default=str, ensure_ascii=False
                ).encode("utf-8")
                await r2_storage.upload(
                    f"db_backup/latest_{table}.json",
                    t_content,
                    "application/json",
                )

        except Exception as e:
            logger.error(f"数据库备份失败: {e}")

        await asyncio.sleep(settings.DB_BACKUP_INTERVAL_MINUTES * 60)
