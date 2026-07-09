"""数据库恢复脚本
从 R2 下载最新的 JSON 备份文件，解析 JSON 并逐表恢复到 CRDB。
支持命令行参数：--table 指定恢复特定表，--dry-run 预览不执行。
使用 asyncpg 直连 CRDB。
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

import asyncpg
from loguru import logger

from config import settings
from storage.r2 import _r2 as r2_storage

# 备份中会包含的表（按依赖顺序排列，先恢复无依赖的表）
ALL_TABLES = ["users", "file_records", "decode_logs", "cells", "codes", "jobs",
              "rotate_log", "pending_uploads", "spare_pool", "backup_config",
              "code_bot_mapping", "message_backups", "relay_accounts",
              "rotation_config", "external_code_mapping"]

# 各表的主键列
TABLE_PK = {
    "users": "user_id",
    "file_records": "file_code",
    "decode_logs": "id",
    "cells": "slot_id",
    "codes": "code",
    "jobs": "id",
    "rotate_log": "id",
    "pending_uploads": "id",
    "spare_pool": "channel_id",
    "backup_config": "config_key",
    "code_bot_mapping": "code_prefix",
    "message_backups": "main_msg_id, backup_channel_id",
    "relay_accounts": "id",
    "rotation_config": "config_key",
    "external_code_mapping": "external_code",
}

# 白名单:只允许这些列名出现在 INSERT/UPDATE 语句中
_ALLOWED_COLUMNS = set()
for _tbl in ALL_TABLES:
    # 预定义常见列名白名单(防止注入)
    _ALLOWED_COLUMNS.update([
        # 通用
        "id", "created_at", "updated_at", "status", "note",
        # users 表
        "user_id", "username", "first_name", "membership_level",
        "daily_decode_quota", "quota_used_today", "quota_date",
        "can_upload", "external_decode_quota", "external_used_today",
        "external_quota_date", "is_banned",
        # file_records 表
        "file_code", "uploader_id", "primary_channel_id", "primary_channel_msg_id",
        "file_types", "backup_channel_msg_ids", "batch_msg_ids", "batch_file_meta",
        "file_ids", "request_count", "create_time", "expire_time",
        "blocked_users", "protect_content", "file_ttl_days",
        # decode_logs 表
        "requester_id", "request_time", "source_channel_id",
        # pending_uploads 表
        "status_msg_id", "processed",
        # backup_config / rotation_config 表
        "config_key", "config_value",
        # message_backups 表
        "main_msg_id", "backup_channel_id", "backed_msg_id", "backed_at",
        # cells 表
        "slot_id", "channel_id", "next_active_chat_id", "prev_slot_id",
        "demoted_to_channel_id", "account_name", "is_r100",
        "last_heartbeat", "last_synced_msg_id", "degrade_count",
        "file_count", "rotation_started_at",
        # codes 表
        "code", "file_record_code",
        # jobs 表
        "target_user_id", "storage_channel_id", "storage_msg_ids",
        "task_type", "dispatched_at", "retry_count",
        "dead", "dead_reason", "dead_retry", "dead_retry_at", "dead_retry_count",
        # rotate_log 表
        "timestamp", "from_slot_id", "to_slot_id", "from_status",
        "to_status", "reason", "triggered_by",
        # spare_pool 表
        "is_used",
        # relay_accounts 表
        "api_id", "api_hash", "phone", "is_active", "last_login_at",
        # external_code_mapping 表
        "external_code", "system_code", "bot_username",
        # code_bot_mapping 表
        "code_prefix",
        # 向后兼容(旧备份可能包含的列)
        "key", "prefix", "api_hash_encrypted",
        "group_key", "account_index", "description", "interval_minutes",
        "enabled", "usage", "total_requests", "avg_wait_ms", "last_used",
        "bot_type", "session_data", "message_id", "chat_id", "from_chat_id",
        "decoded_at", "decode_result", "backup_time",
    ])


_ALLOWED_TABLES = frozenset([
    "users", "file_records", "decode_logs", "pending_uploads",
    "cells", "codes", "jobs", "rotate_log", "relay_accounts",
    "spare_pool", "backup_config", "code_bot_mapping",
    "message_backups", "rotation_config", "external_code_mapping",
])

def _sanitize_table(name: str) -> str:
    """白名单校验表名,防止 SQL 注入。"""
    clean = name.strip().lower()
    if clean not in _ALLOWED_TABLES:
        raise ValueError(f"非法表名: {name}")
    return clean


def _sanitize_column(name: str) -> str:
    """白名单校验列名,防止 SQL 注入。"""
    clean = name.strip().lower()
    if clean not in _ALLOWED_COLUMNS:
        raise ValueError(f"非法列名: {name}")
    return clean


async def get_latest_backup() -> dict:
    """从 R2 下载最新的全量备份 JSON 文件并解析。"""
    # 列出所有备份文件
    objects = await r2_storage.list_objects(prefix="db_backup/db_backup_")
    if not objects:
        logger.error("R2 上未找到任何备份文件 (prefix: db_backup/db_backup_)")
        sys.exit(1)

    # 按 key 排序（文件名含时间戳），取最新的
    objects.sort(key=lambda o: o["key"], reverse=True)
    latest_key = objects[0]["key"]
    logger.info(f"找到最新备份: {latest_key} ({objects[0]['size']} 字节)")

    content = await r2_storage.download(latest_key)
    data = json.loads(content.decode("utf-8"))
    logger.info(
        f"备份时间: {data.get('backup_time', '未知')}, "
        f"表: {', '.join(data.get('tables', {}).keys())}"
    )
    return data


async def restore_table(conn: asyncpg.Connection, table: str, records: list[dict], dry_run: bool = False):
    """将记录逐表恢复到 CRDB（逐行 UPSERT）。"""
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

    # 白名单校验所有列名,防止 SQL 注入
    try:
        columns = [_sanitize_column(c) for c in records[0].keys()]
    except ValueError as e:
        logger.error(f"[{table}] 列名校验失败: {e}, 跳过此表")
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
                    await conn.execute(sql, *vals)
                    restored += 1
                except Exception as e:
                    logger.error(f"[{table}] 恢复记录失败 (pk={record.get(pk)}): {e}")
        logger.debug(f"[{table}] 已恢复 {restored}/{len(records)}")
    return restored


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


async def run_restore(table: str = None, dry_run: bool = False):
    """执行恢复流程。"""
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

    # 4. 连接 CRDB 并逐表恢复
    if not settings.COCKROACHDB_URL:
        logger.error("COCKROACHDB_URL 未配置")
        sys.exit(1)

    # B9: 初始化 conn = None 防止 connect 抛异常时 finally 引用未绑定变量
    conn = None
    try:
        conn = await asyncpg.connect(settings.COCKROACHDB_URL)
        for tbl in target_tables:
            records = tables_data[tbl]
            logger.info(f"[{tbl}] 开始恢复 {len(records)} 条记录...")
            count = await restore_table(conn, tbl, records, dry_run=dry_run)
            logger.info(f"[{tbl}] 恢复完成: {count} 条记录")
    finally:
        if conn is not None:
            await conn.close()
        await r2_storage.close()
    logger.info("数据库恢复完成")


def main():
    parser = argparse.ArgumentParser(description="从 R2 备份恢复 CRDB 数据库")
    parser.add_argument(
        "--table", type=str, default=None,
        help="指定要恢复的表名（默认恢复所有表）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式，不实际写入数据",
    )
    args = parser.parse_args()
    asyncio.run(run_restore(table=args.table, dry_run=args.dry_run))


if __name__ == "__main__":
    main()