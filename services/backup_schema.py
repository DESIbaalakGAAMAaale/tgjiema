"""Backup/Restore 共享 Schema 定义(单一事实源)

db_backup.py 和 db_restore.py 共同使用此模块,
消除两套独立白名单的漂移风险。

变更检查清单:
1. 新增表时,在此模块的 BACKUP_SCHEMA 字典中添加条目
2. db_backup.py 和 db_restore.py 无需修改(自动从 BACKUP_SCHEMA 生成)
3. 运行 tests/ 确保无回归
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableSchema:
    """单张表的备份/恢复 schema 定义"""
    name: str                          # 表名
    pk_columns: tuple[str, ...]        # 主键列(复合主键用元组)
    columns: tuple[str, ...] = ()      # 表的所有列(用于列白名单校验)
    conflict_col: str = ""             # INSERT OR IGNORE 冲突列(空表示用 PK 或无冲突)
    is_large: bool = False             # 是否大表(跳过备份,如 decode_logs/jobs)
    where_clause: str = ""             # 备份时的 WHERE 过滤(空表示全量)
    sync_column: str = "crdb_synced"   # 同步状态列名(空表示无同步列)
    note: str = ""                     # 备注


# ─── 唯一事实源: 所有可备份/恢复的表 ───
# 合并自 db_backup.py 的 SMALL_TABLES/_LARGE_TABLES/_TABLE_WHERE/_CONFLICT_COLS
#       和 db_restore.py 的 ALL_TABLES/TABLE_PK/_ALLOWED_TABLES/_ALLOWED_COLUMNS
BACKUP_SCHEMA: dict[str, TableSchema] = {
    # ─── 核心用户/取件码/文件 ───
    "users": TableSchema(
        name="users",
        pk_columns=("user_id",),
        columns=(
            "user_id", "username", "first_name", "membership_level",
            "daily_decode_quota", "quota_used_today", "quota_date",
            "can_upload", "external_decode_quota", "external_used_today",
            "external_quota_date", "is_banned",
            "created_at", "updated_at",
        ),
        conflict_col="user_id",
        note="用户表(主键 user_id)",
    ),
    "file_records": TableSchema(
        name="file_records",
        pk_columns=("file_code",),
        columns=(
            "file_code", "uploader_id", "primary_channel_id",
            "primary_channel_msg_id", "file_types", "backup_channel_msg_ids",
            "batch_msg_ids", "batch_file_meta", "file_ids", "request_count",
            "create_time", "expire_time", "blocked_users",
            "protect_content", "file_ttl_days", "status",
        ),
        conflict_col="file_code",
        where_clause="status = 'active'",  # 仅备份活跃文件,跳过已过期/删除
        note="取件码→频道/消息映射(核心数据,仅备份 active)",
    ),
    "codes": TableSchema(
        name="codes",
        pk_columns=("code",),
        columns=("code", "file_record_code", "created_at"),
        conflict_col="code",
        note="取件码表(主键 code)",
    ),

    # ─── 频道/槽位/轮转 ───
    "cells": TableSchema(
        name="cells",
        pk_columns=("slot_id",),
        columns=(
            "slot_id", "channel_id", "next_active_chat_id", "prev_slot_id",
            "demoted_to_channel_id", "account_name", "is_r100",
            "last_heartbeat", "last_synced_msg_id", "degrade_count",
            "file_count", "rotation_started_at",
            "created_at", "updated_at",
        ),
        conflict_col="slot_id",
        note="频道槽位表(主键 slot_id)",
    ),
    "spare_pool": TableSchema(
        name="spare_pool",
        pk_columns=("channel_id",),
        columns=("channel_id", "is_used", "created_at"),
        conflict_col="channel_id",
        note="备用频道池(主键 channel_id)",
    ),
    "backup_config": TableSchema(
        name="backup_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "created_at", "updated_at"),
        conflict_col="",  # 主键即 config_key,但 merge 模式下原逻辑未在 _CONFLICT_COLS 中显式配置
        note="备份配置表(主键 config_key)",
    ),
    "rotation_config": TableSchema(
        name="rotation_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "created_at", "updated_at"),
        conflict_col="",
        note="轮转配置表(主键 config_key)",
    ),
    "code_bot_mapping": TableSchema(
        name="code_bot_mapping",
        pk_columns=("code_prefix",),
        columns=("code_prefix", "created_at"),
        conflict_col="",
        note="取件码前缀→Bot 映射(主键 code_prefix)",
    ),
    "external_code_mapping": TableSchema(
        name="external_code_mapping",
        pk_columns=("external_code",),
        columns=("external_code", "system_code", "bot_username", "created_at"),
        conflict_col="external_code",
        note="外部取件码映射(主键 external_code)",
    ),
    "kv_config": TableSchema(
        name="kv_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "created_at", "updated_at"),
        conflict_col="config_key",
        note="KV 配置表(主键 config_key,部分部署中可能不存在,自动跳过)",
    ),
    "message_backups": TableSchema(
        name="message_backups",
        pk_columns=("main_msg_id", "backup_channel_id"),  # 复合主键
        columns=(
            "main_msg_id", "backup_channel_id",
            "backed_msg_id", "backed_at",
        ),
        conflict_col="",  # 复合主键,由 restore 逻辑用 ON CONFLICT (col1, col2) 处理
        note="消息备份表(复合主键 main_msg_id + backup_channel_id)",
    ),

    # ─── 中继账号 ───
    "relay_accounts": TableSchema(
        name="relay_accounts",
        pk_columns=("id",),
        columns=(
            "id", "api_id", "api_hash", "phone",
            "is_active", "last_login_at",
            "created_at", "updated_at",
        ),
        conflict_col="phone",  # 主键 id 是 SERIAL,用 phone UNIQUE 做冲突
        note="中继账号表(主键 id SERIAL,冲突列 phone UNIQUE)",
    ),

    # ─── M0 收尾: 副本恢复元数据 + 幂等去重 ───
    "manifest": TableSchema(
        name="manifest",
        pk_columns=("group_id", "file_unique_id", "channel_id"),  # 复合主键
        columns=(
            "group_id", "file_unique_id", "channel_id",
            "media_type", "media_group_id", "first_seen_at",
        ),
        conflict_col="",  # 复合主键,由 restore 逻辑用 ON CONFLICT (col1, col2, col3) 处理
        note="频道冗余环副本元数据(复合主键,驱动副本重建)",
    ),
    "writer_inbox": TableSchema(
        name="writer_inbox",
        pk_columns=("message_id",),
        columns=(
            "message_id", "method_name", "stream_id",
            "created_at", "processed_at",
        ),
        conflict_col="message_id",
        note="幂等去重表(主键 message_id,恢复后用于幂等性校验)",
    ),

    # ─── M1 业务闭环: 5 张新表 ───
    "upload_sessions": TableSchema(
        name="upload_sessions",
        pk_columns=("upload_id",),
        columns=(
            "upload_id", "source_msg_ids", "primary_msg_ids",
            "options_json", "trace_id", "prev_status",
            "transitioned_at", "transition_reason",
            "lease_owner", "lease_until", "last_error",
            "created_at", "updated_at", "status",
        ),
        conflict_col="upload_id",
        note="上传会话状态机(主键 upload_id TEXT)",
    ),
    "upload_outbox": TableSchema(
        name="upload_outbox",
        pk_columns=("outbox_id",),
        columns=(
            "outbox_id", "job_id", "event_type",
            "attempts", "next_retry_at",
            "created_at", "status",
        ),
        conflict_col="outbox_id",
        note="事务发件箱(主键 outbox_id TEXT)",
    ),
    "quota_ledger": TableSchema(
        name="quota_ledger",
        pk_columns=("ledger_id",),
        columns=(
            "ledger_id", "is_external", "quota_before", "quota_after",
            "request_id", "created_at",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT(追加式日志)
        note="配额变更流水(主键 ledger_id INTEGER 自增,追加式日志)",
    ),
    "delivery_receipts": TableSchema(
        name="delivery_receipts",
        pk_columns=("receipt_id",),
        columns=(
            "receipt_id", "source_msg_id", "sent_msg_id",
            "group_receipt_id", "error_reason", "confirmed_at",
            "created_at",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT
        note="投递回执(主键 receipt_id INTEGER 自增,追加式日志)",
    ),
    "replication_tasks": TableSchema(
        name="replication_tasks",
        pk_columns=("task_id",),
        columns=(
            "task_id", "src_channel_id", "dst_channel_id",
            "src_msg_id", "dst_msg_id", "priority",
            "max_attempts", "committed_at",
            "created_at", "status",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT
        note="副本复制任务(主键 task_id INTEGER 自增,追加式日志)",
    ),

    # ─── 大表(跳过备份,仅在 db_restore ALL_TABLES 中保留以支持旧备份恢复) ───
    "decode_logs": TableSchema(
        name="decode_logs",
        pk_columns=("id",),
        columns=(
            "id", "created_at", "requester_id", "request_time",
            "source_channel_id", "status", "note",
        ),
        conflict_col="",
        is_large=True,  # 短期流水数据,无需长期备份
        note="解码日志(大表,跳过备份;短期流水数据)",
    ),
    "jobs": TableSchema(
        name="jobs",
        pk_columns=("id",),
        columns=(
            "id", "created_at", "target_user_id", "storage_channel_id",
            "storage_msg_ids", "task_type", "dispatched_at", "retry_count",
            "status", "dead", "dead_reason", "dead_retry",
            "dead_retry_at", "dead_retry_count",
        ),
        conflict_col="",
        is_large=True,  # 短期流水数据,无需长期备份
        note="异步任务表(大表,跳过备份;短期流水数据)",
    ),
    "rotate_log": TableSchema(
        name="rotate_log",
        pk_columns=("id",),
        columns=(
            "id", "created_at", "timestamp", "from_slot_id", "to_slot_id",
            "from_status", "to_status", "reason", "triggered_by",
        ),
        conflict_col="",
        is_large=True,  # 审计日志,数据量大但非核心
        note="轮转审计日志(大表,跳过备份;审计数据)",
    ),
    "pending_uploads": TableSchema(
        name="pending_uploads",
        pk_columns=("id",),
        columns=(
            "id", "created_at", "status", "status_msg_id", "processed",
        ),
        conflict_col="",
        is_large=True,  # 瞬时状态,重启后从频道重放
        note="待上传队列(大表,跳过备份;瞬时状态)",
    ),
}


# ─── 向后兼容: 旧备份可能包含的列(不属于任何特定表) ───
_LEGACY_COLUMNS: tuple[str, ...] = (
    "key", "prefix", "api_hash_encrypted",
    "group_key", "account_index", "description", "interval_minutes",
    "enabled", "usage", "total_requests", "avg_wait_ms", "last_used",
    "bot_type", "session_data", "message_id", "chat_id", "from_chat_id",
    "decoded_at", "decode_result", "backup_time",
)


# ═══════════════════════════════════════════════════════════
#  派生属性 / 查询函数
# ═══════════════════════════════════════════════════════════

def get_backup_tables() -> list[str]:
    """返回需要备份的表名列表(非大表)。

    等价于原 db_backup.SMALL_TABLES。
    """
    return [t.name for t in BACKUP_SCHEMA.values() if not t.is_large]


def get_restore_tables() -> list[str]:
    """返回可恢复的表名列表(全部,含大表)。

    等价于原 db_restore.ALL_TABLES。
    """
    return list(BACKUP_SCHEMA.keys())


def get_table_pk(table: str) -> tuple[str, ...]:
    """返回表的主键列元组。

    复合主键返回多元素元组(如 ("main_msg_id", "backup_channel_id"))。
    """
    return BACKUP_SCHEMA[table].pk_columns


def get_conflict_col(table: str) -> str:
    """返回冲突列(用于 INSERT OR IGNORE)。

    空字符串表示无单列冲突(复合主键或自增主键)。
    """
    return BACKUP_SCHEMA[table].conflict_col


def get_where_clause(table: str) -> str:
    """返回备份 WHERE 条件。

    空字符串表示全量备份。
    """
    return BACKUP_SCHEMA[table].where_clause


def get_allowed_columns(table: str) -> list[str]:
    """返回表的允许列列表(用于恢复验证)。

    从 TableSchema 的 columns 属性返回。
    """
    return list(BACKUP_SCHEMA[table].columns)


def is_table_allowed(table: str) -> bool:
    """检查表是否在允许列表中。"""
    return table in BACKUP_SCHEMA


def _build_allowed_columns() -> set[str]:
    """聚合所有表的列 + 向后兼容列,生成全局列白名单。

    等价于原 db_restore._ALLOWED_COLUMNS。
    恢复时 _sanitize_column 仅检查列名是否在此集合中(全局,非按表)。
    """
    cols: set[str] = set(_LEGACY_COLUMNS)
    for table in BACKUP_SCHEMA.values():
        cols.update(table.columns)
    return cols


# 全局列白名单(聚合所有表 columns + 向后兼容列)
# db_restore.py 通过 ALLOWED_COLUMNS 别名引用
ALLOWED_COLUMNS: set[str] = _build_allowed_columns()
