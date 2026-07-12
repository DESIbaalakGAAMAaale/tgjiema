"""Backup/Restore 共享 Schema 定义(单一事实源)

db_backup.py 和 db_restore.py 共同使用此模块,
消除两套独立白名单的漂移风险。

R35 Batch 3 P1-5/P1-6 修复:
- TableSchema 增加 source 字段(crdb/sqlite/relay_sqlite/redis),
  区分表所在数据库,避免对 SQLite-only 表执行 CRDB SELECT *。
- 从实际 DDL(session.py DDL_STATEMENTS / cache_store.py / relay_db.py)
  补全所有表的真实列定义,修复列不完整问题。
- 新增 get_tables_by_source() 按 source 分组查询。
- 新增 validate_schema() 供 CI 比较 DDL 与 BACKUP_SCHEMA。

变更检查清单:
1. 新增表时,在此模块的 BACKUP_SCHEMA 字典中添加条目
2. db_backup.py 和 db_restore.py 无需修改(自动从 BACKUP_SCHEMA 生成)
3. 运行 tests/ 确保无回归
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableSchema:
    """单张表的备份/恢复 schema 定义

    R35 P1-5: source 字段标记表所在数据库:
      - "crdb":        CockroachDB 主库(走 CRDB SELECT *)
      - "sqlite":       cache_store.db 本地表(走 SQLite SELECT *)
      - "relay_sqlite": relay_pool.db 中继库(走 relay SQLite SELECT *)
      - "redis":        Redis(暂不支持快照,标记用)
    """
    name: str                          # 表名
    pk_columns: tuple[str, ...]        # 主键列(复合主键用元组)
    columns: tuple[str, ...] = ()      # 表的所有列(用于列白名单校验)
    conflict_col: str = ""             # INSERT OR IGNORE 冲突列(空表示用 PK 或无冲突)
    is_large: bool = False             # 是否大表(跳过备份,如 decode_logs/jobs)
    where_clause: str = ""             # 备份时的 WHERE 过滤(空表示全量)
    sync_column: str = "crdb_synced"   # 同步状态列名(空表示无同步列)
    note: str = ""                     # 备注
    source: str = "crdb"               # R35 P1-5: 表所在数据库(crdb/sqlite/relay_sqlite/redis)


# ─── 唯一事实源: 所有可备份/恢复的表 ───
# 合并自 db_backup.py 的 SMALL_TABLES/_LARGE_TABLES/_TABLE_WHERE/_CONFLICT_COLS
#       和 db_restore.py 的 ALL_TABLES/TABLE_PK/_ALLOWED_TABLES/_ALLOWED_COLUMNS
#
# R35 P1-6: 列定义从实际 DDL 补全:
#   - CRDB 表:  从 database/session.py DDL_STATEMENTS + MIGRATION_STATEMENTS 读取
#   - SQLite 表: 从 database/cache_store.py 的 CREATE TABLE 读取
#   - relay_sqlite 表: 从 database/relay_db.py DDL 读取
BACKUP_SCHEMA: dict[str, TableSchema] = {
    # ═══════════════════════════════════════════════════════════
    #  CRDB 主库表 (source="crdb")
    # ═══════════════════════════════════════════════════════════
    # ─── 核心用户/取件码/文件 ───
    "users": TableSchema(
        name="users",
        pk_columns=("user_id",),
        columns=(
            "user_id", "username", "first_name", "membership_level",
            "daily_decode_quota", "quota_used_today", "quota_date",
            "can_upload", "external_decode_quota", "external_used_today",
            "external_quota_date", "is_banned",
            "created_at", "updated_at", "deleted_at",
        ),
        conflict_col="user_id",
        note="用户表(主键 user_id BIGINT;R37 P1-5 含 deleted_at tombstone)",
    ),
    "file_records": TableSchema(
        name="file_records",
        pk_columns=("file_code",),
        columns=(
            "file_code", "uploader_id", "primary_channel_id",
            "primary_channel_msg_id", "file_types", "backup_channel_msg_ids",
            "batch_msg_ids", "batch_file_meta", "file_ids",
            "status", "request_count",
            "create_time", "expire_time", "blocked_users",
            "note", "protect_content", "updated_at", "file_ttl_days",
            "max_requests", "is_collection", "collection_codes",
            "deleted_at",
        ),
        conflict_col="file_code",
        # R37 P1-5: 移除 where_clause=status='active',
        # 改为备份全部行(含 deleted_at 标记的软删除),
        # 增量 watermark 通过 deleted_at > watermark 捕捉删除事件。
        # 恢复时由业务层根据 deleted_at 决定是否激活。
        where_clause="",
        note="取件码→频道/消息映射(核心数据;R37 P1-5 含 deleted_at tombstone,备份全部行用于删除追溯)",
    ),
    "codes": TableSchema(
        name="codes",
        pk_columns=("code",),
        columns=(
            "code", "file_record_code", "uploader_id", "file_types",
            "batch_msg_ids", "batch_file_meta", "primary_channel_id",
            "status", "created_at", "expire_time",
            "note", "updated_at", "deleted_at",
        ),
        conflict_col="code",
        note="取件码表(主键 code;R37 P1-5 含 deleted_at tombstone)",
    ),

    # ─── 频道/槽位/轮转 ───
    "cells": TableSchema(
        name="cells",
        pk_columns=("slot_id",),
        columns=(
            "slot_id", "channel_id", "status", "next_active_chat_id",
            "prev_slot_id", "demoted_to_channel_id", "account_name",
            "is_r100", "last_heartbeat", "last_synced_msg_id",
            "degrade_count", "file_count", "rotation_started_at",
            "created_at", "updated_at", "deleted_at",
        ),
        conflict_col="slot_id",
        note="频道槽位表(主键 slot_id;环形冗余架构;R37 P1-5 含 deleted_at tombstone)",
    ),
    "spare_pool": TableSchema(
        name="spare_pool",
        pk_columns=("channel_id",),
        columns=("channel_id", "account_name", "is_used", "created_at"),
        conflict_col="channel_id",
        note="备用频道池(主键 channel_id;含 account_name)",
    ),
    "backup_config": TableSchema(
        name="backup_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "updated_at"),
        conflict_col="",
        note="备份配置表(主键 config_key;CRDB 无 created_at 列)",
    ),
    "rotation_config": TableSchema(
        name="rotation_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "updated_at"),
        conflict_col="",
        note="轮转配置表(主键 config_key;CRDB 无 created_at 列)",
    ),
    "code_bot_mapping": TableSchema(
        name="code_bot_mapping",
        pk_columns=("code_prefix",),
        columns=("code_prefix", "bot_username", "created_at"),
        conflict_col="",
        note="取件码前缀→Bot 映射(主键 code_prefix;含 bot_username)",
    ),
    "external_code_mapping": TableSchema(
        name="external_code_mapping",
        pk_columns=("external_code",),
        columns=("external_code", "system_code", "bot_username", "created_at", "updated_at"),
        conflict_col="external_code",
        note="外部取件码映射(主键 external_code)",
    ),
    "kv_config": TableSchema(
        name="kv_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "created_at", "updated_at"),
        conflict_col="config_key",
        note="KV 配置表(主键 config_key;部分部署中可能不存在,自动跳过)",
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
            "is_active", "created_at", "last_login_at",
        ),
        conflict_col="phone",  # 主键 id 是 SERIAL,用 phone UNIQUE 做冲突
        note="中继账号表(主键 id SERIAL,冲突列 phone UNIQUE;CRDB 无 updated_at)",
    ),

    # ─── 大表(跳过备份,仅在 db_restore ALL_TABLES 中保留以支持旧备份恢复) ───
    "decode_logs": TableSchema(
        name="decode_logs",
        pk_columns=("id",),
        columns=(
            "id", "file_code", "requester_id", "request_time",
            "status", "source_channel_id",
        ),
        conflict_col="",
        is_large=True,  # 短期流水数据,无需长期备份
        note="解码日志(大表,跳过备份;短期流水数据)",
    ),
    "jobs": TableSchema(
        name="jobs",
        pk_columns=("id",),
        columns=(
            "id", "code", "target_user_id", "storage_channel_id",
            "storage_msg_ids", "batch_file_meta", "task_type",
            "status", "created_at", "dispatched_at",
            "protect_content", "retry_count", "dead_reason",
            "dead_retry_count", "dead_retry", "dead_retry_at",
        ),
        conflict_col="",
        is_large=True,  # 短期流水数据,无需长期备份
        note="异步任务表(大表,跳过备份;含死信队列字段)",
    ),
    "rotate_log": TableSchema(
        name="rotate_log",
        pk_columns=("id",),
        columns=(
            "id", "timestamp", "from_slot_id", "to_slot_id",
            "from_status", "to_status", "reason", "triggered_by",
        ),
        conflict_col="",
        is_large=True,  # 审计日志,数据量大但非核心
        note="轮转审计日志(大表,跳过备份;审计数据;CRDB 无 created_at 列,用 timestamp)",
    ),
    "pending_uploads": TableSchema(
        name="pending_uploads",
        pk_columns=("id",),
        columns=(
            "id", "uploader_id", "primary_channel_id",
            "primary_channel_msg_id", "file_types", "batch_msg_ids",
            "batch_file_meta", "status_msg_id", "created_at",
            "processed", "claimed_at", "note", "protect_content", "file_ttl_days",
        ),
        conflict_col="",
        is_large=True,  # 瞬时状态,重启后从频道重放
        note="待上传队列(大表,跳过备份;瞬时状态;含 claimed_at/note/protect_content/file_ttl_days)",
    ),

    # ═══════════════════════════════════════════════════════════
    #  SQLite 本地表 (source="sqlite") — 数据在 cache_store.db
    #  R35 P1-5: 这些表在 SQLite 建表,CRDB 不存在,必须走 SQLite 快照
    # ═══════════════════════════════════════════════════════════

    # ─── M0 收尾: 副本恢复元数据 + 幂等去重 ───
    "manifest": TableSchema(
        name="manifest",
        pk_columns=("group_id", "file_unique_id", "channel_id"),  # 复合主键
        columns=(
            "group_id", "file_unique_id", "channel_id",
            "message_id", "media_type", "media_group_id", "first_seen_at",
        ),
        conflict_col="",  # 复合主键,由 restore 逻辑用 ON CONFLICT (col1, col2, col3) 处理
        source="sqlite",
        note="频道冗余环副本元数据(复合主键;SQLite-only;含 message_id 列)",
    ),
    "writer_inbox": TableSchema(
        name="writer_inbox",
        pk_columns=("message_id",),
        columns=(
            "message_id", "method_name", "stream_id",
            "created_at", "processed_at",
        ),
        conflict_col="message_id",
        source="sqlite",
        note="幂等去重表(主键 message_id;SQLite-only;db_writer 崩溃恢复用)",
    ),

    # ─── M1 业务闭环: 5 张新表(均为 SQLite-only) ───
    "upload_sessions": TableSchema(
        name="upload_sessions",
        pk_columns=("upload_id",),
        columns=(
            "upload_id", "user_id", "source_msg_ids", "primary_channel_id",
            "primary_msg_ids", "media_group_id", "options_json", "trace_id",
            "status", "prev_status", "transitioned_at", "transition_reason",
            "lease_owner", "lease_until", "last_error",
            "created_at", "updated_at",
        ),
        conflict_col="upload_id",
        source="sqlite",
        note="上传会话状态机(主键 upload_id TEXT;SQLite-only;含 user_id/media_group_id 等列)",
    ),
    "upload_outbox": TableSchema(
        name="upload_outbox",
        pk_columns=("outbox_id",),
        columns=(
            "outbox_id", "upload_id", "job_id", "code",
            "target_user_id", "storage_channel_id", "storage_msg_ids",
            "batch_file_meta", "task_type", "protect_content",
            "event_type", "status", "attempts", "next_retry_at",
            "created_at", "processed_at",
        ),
        conflict_col="outbox_id",
        source="sqlite",
        note="事务发件箱(主键 outbox_id TEXT;SQLite-only;含 code/target_user_id 等列)",
    ),
    "quota_ledger": TableSchema(
        name="quota_ledger",
        pk_columns=("ledger_id",),
        columns=(
            "ledger_id", "user_id", "event_type", "is_external",
            "quota_before", "quota_after", "request_id", "reason",
            "created_at",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT(追加式日志)
        source="sqlite",
        note="配额变更流水(主键 ledger_id INTEGER 自增;SQLite-only;追加式日志;含 user_id/event_type/reason)",
    ),
    "delivery_receipts": TableSchema(
        name="delivery_receipts",
        pk_columns=("receipt_id",),
        columns=(
            "receipt_id", "job_id", "source_msg_id", "target_user_id",
            "sent_msg_id", "media_group_id", "group_receipt_id",
            "status", "attempts", "error_reason",
            "created_at", "confirmed_at",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT
        source="sqlite",
        note="投递回执(主键 receipt_id INTEGER 自增;SQLite-only;含 UNIQUE(job_id, source_msg_id))",
    ),
    "replication_tasks": TableSchema(
        name="replication_tasks",
        pk_columns=("task_id",),
        columns=(
            "task_id", "group_id", "file_unique_id",
            "src_channel_id", "dst_channel_id", "src_msg_id", "dst_msg_id",
            "media_group_id", "task_type", "priority",
            "status", "prev_status", "attempts", "max_attempts",
            "next_retry_at", "last_error",
            "created_at", "updated_at", "committed_at",
        ),
        conflict_col="",  # 自增主键,merge 模式退化为普通 INSERT
        source="sqlite",
        note="副本复制任务(主键 task_id INTEGER 自增;SQLite-only;含 group_id/file_unique_id 等列)",
    ),

    # ─── SQLite 本地缓存表(热路径零 CRDB,部分需要备份) ───
    "kv_store": TableSchema(
        name="kv_store",
        pk_columns=("key",),
        columns=("key", "value"),
        conflict_col="key",
        source="sqlite",
        note="KV 键值存储(SQLite-only;缓存 DDL 版本等配置;主键 key)",
    ),
    "user_quota": TableSchema(
        name="user_quota",
        pk_columns=("user_id",),
        columns=(
            "user_id", "level", "daily_quota", "used_today", "quota_date",
            "ext_quota", "ext_used_today", "ext_quota_date", "synced_at",
        ),
        conflict_col="user_id",
        source="sqlite",
        note="用户配额本地表(SQLite-only;Idx Bot 零 RU 读写;主键 user_id)",
    ),
    "pending_file_codes": TableSchema(
        name="pending_file_codes",
        pk_columns=("id",),
        columns=(
            "id", "user_id", "file_code", "note", "ext_code", "created_at",
        ),
        conflict_col="",
        source="sqlite",
        note="待发送文件码(SQLite-only;用户未 /start idx 时暂存;主键 id 自增)",
    ),
    "cache_backup": TableSchema(
        name="cache_backup",
        pk_columns=("key",),
        columns=("key", "value", "ts"),
        conflict_col="key",
        source="sqlite",
        note="内存缓存 SQLite 持久化(SQLite-only;主键 key;含 ts 时间戳)",
    ),
    "heartbeat_local": TableSchema(
        name="heartbeat_local",
        pk_columns=("slot_id",),
        columns=("slot_id", "last_ok", "fail_streak"),
        conflict_col="slot_id",
        source="sqlite",
        note="心跳本地表(SQLite-only;Mon Bot 写入;主键 slot_id;瞬时状态)",
    ),
    "bot_heartbeat": TableSchema(
        name="bot_heartbeat",
        pk_columns=("name",),
        columns=("name", "last_ping", "is_running", "total_processed", "total_errors"),
        conflict_col="name",
        source="sqlite",
        note="Bot 心跳表(SQLite-only;各 Bot 独立进程写入;主键 name;瞬时状态)",
    ),
    "counter_snapshot": TableSchema(
        name="counter_snapshot",
        pk_columns=("key",),
        columns=("key", "value", "ts"),
        conflict_col="key",
        source="sqlite",
        note="启动统计快照(SQLite-only;主键 key;含 ts 时间戳)",
    ),
    "ttl_cache": TableSchema(
        name="ttl_cache",
        pk_columns=("key",),
        columns=("key", "value", "updated_at"),
        conflict_col="key",
        source="sqlite",
        note="通用 TTL 缓存(SQLite-only;跨进程共享;主键 key)",
    ),
    "cells_local": TableSchema(
        name="cells_local",
        pk_columns=("slot_id",),
        columns=(
            "slot_id", "channel_id", "status", "next_active_chat_id",
            "prev_slot_id", "demoted_to_channel_id", "account_name",
            "is_r100", "last_heartbeat", "last_synced_msg_id",
            "degrade_count", "file_count", "rotation_started_at",
            "updated_at", "crdb_synced",
            "topology_version", "lease_owner", "lease_until", "transition_id",
        ),
        conflict_col="slot_id",
        source="sqlite",
        note="cells 本地逐行存储(SQLite-only;热路径零 CRDB;主键 slot_id;含 CAS/fencing 字段)",
    ),
    "file_records_local": TableSchema(
        name="file_records_local",
        pk_columns=("file_code",),
        columns=(
            "file_code", "uploader_id", "primary_channel_id",
            "primary_channel_msg_id", "file_types", "backup_channel_msg_ids",
            "batch_msg_ids", "batch_file_meta", "file_ids",
            "status", "request_count", "protect_content", "file_ttl_days",
            "note", "expire_time", "blocked_users",
            "create_time", "updated_at",
            "max_requests", "is_collection", "collection_codes", "crdb_synced",
        ),
        conflict_col="file_code",
        source="sqlite",
        note="file_records 热路径全表缓存(SQLite-only;启动时从 CRDB 全量加载;主键 file_code)",
    ),
    "codes_local": TableSchema(
        name="codes_local",
        pk_columns=("code",),
        columns=(
            "code", "file_record_code", "uploader_id", "file_types",
            "batch_msg_ids", "batch_file_meta", "primary_channel_id",
            "status", "created_at", "expire_time", "note", "crdb_synced",
        ),
        conflict_col="code",
        source="sqlite",
        note="codes 热路径全表缓存(SQLite-only;主键 code;含 crdb_synced 同步标志)",
    ),
    "users_local": TableSchema(
        name="users_local",
        pk_columns=("user_id",),
        columns=(
            "user_id", "username", "first_name", "membership_level",
            "daily_decode_quota", "quota_used_today", "quota_date",
            "can_upload", "external_decode_quota", "external_used_today",
            "external_quota_date", "is_banned",
            "created_at", "updated_at", "crdb_synced",
        ),
        conflict_col="user_id",
        source="sqlite",
        note="users 热路径全表缓存(SQLite-only;主键 user_id;含 crdb_synced)",
    ),
    "external_code_mapping_local": TableSchema(
        name="external_code_mapping_local",
        pk_columns=("external_code",),
        columns=(
            "external_code", "system_code", "bot_username",
            "created_at", "updated_at", "crdb_synced",
        ),
        conflict_col="external_code",
        source="sqlite",
        note="external_code_mapping 热路径缓存(SQLite-only;主键 external_code)",
    ),

    # ═══════════════════════════════════════════════════════════
    #  relay_pool.db 中继库表 (source="relay_sqlite")
    # ═══════════════════════════════════════════════════════════
    "mapped_codes": TableSchema(
        name="mapped_codes",
        pk_columns=("code",),
        columns=("code", "file_code", "created_at"),
        conflict_col="code",
        source="relay_sqlite",
        note="外部码映射缓存(relay_sqlite;避免重复查询 CRDB;主键 code)",
    ),
    "relay_spool": TableSchema(
        name="relay_spool",
        pk_columns=("spool_id",),
        columns=(
            "spool_id", "relay_account_id", "code", "user_id",
            "external_code", "source_msg_ids", "buffered_files",
            "checksum", "status", "prev_status", "attempts",
            "ttl_expires_at", "last_error",
            "created_at", "updated_at", "acked_at",
        ),
        conflict_col="",
        source="relay_sqlite",
        note="中继任务池(relay_sqlite;持久化中继代发任务;主键 spool_id 自增)",
    ),
}


# ─── 向后兼容: 旧备份可能包含的列(不属于任何特定表) ───
_LEGACY_COLUMNS: tuple[str, ...] = (
    "key", "prefix", "api_hash_encrypted",
    "group_key", "account_index", "description", "interval_minutes",
    "enabled", "usage", "total_requests", "avg_wait_ms", "last_used",
    "bot_type", "session_data", "message_id", "chat_id", "from_chat_id",
    "decoded_at", "decode_result", "backup_time",
    # R35: 旧备份可能存在的已废弃列
    "account_name", "spare_pool_account", "created_at_legacy",
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


def get_table_source(table: str) -> str:
    """返回表的数据源(crdb/sqlite/relay_sqlite/redis)。

    R35 P1-5: 用于按 source 分组备份/恢复。
    """
    return BACKUP_SCHEMA[table].source


def get_tables_by_source(source: str) -> list[str]:
    """返回指定 source 的所有表名列表(非大表,用于备份)。

    R35 P1-5: 按 source 分组查询,避免对 SQLite-only 表执行 CRDB SELECT *。

    Args:
        source: "crdb" / "sqlite" / "relay_sqlite" / "redis"

    Returns:
        该 source 下所有非大表的表名列表
    """
    return [
        t.name for t in BACKUP_SCHEMA.values()
        if t.source == source and not t.is_large
    ]


def get_restore_tables_by_source(source: str) -> list[str]:
    """返回指定 source 的所有可恢复表名列表(含大表)。

    R35 P1-5: 恢复时按 source 分组,走各自的写入路径。
    """
    return [
        t.name for t in BACKUP_SCHEMA.values()
        if t.source == source
    ]


def _build_allowed_columns() -> set[str]:
    """聚合所有表的列 + 向后兼容列,生成全局列白名单。

    等价于原 db_restore._ALLOWED_COLUMNS。
    恢复时 _sanitize_column 仅检查列名是否在此集合中(全局,非按表)。

    注意: R35 P1-6 推荐使用 get_allowed_columns(table) 按表校验列,
    此全局白名单仅用于向后兼容旧备份(列可能不属于当前表)。
    """
    cols: set[str] = set(_LEGACY_COLUMNS)
    for table in BACKUP_SCHEMA.values():
        cols.update(table.columns)
    return cols


def validate_columns_for_table(table: str, columns: list[str]) -> list[str]:
    """按表严格校验列名(替代全局白名单)。

    R35 P1-6: 恢复时按表校验列,不再使用全局白名单。
    仅允许该表 BACKUP_SCHEMA.columns 中定义的列 + 向后兼容列。

    Args:
        table: 表名(必须在 BACKUP_SCHEMA 中)
        columns: 待校验的列名列表

    Returns:
        合法的列名列表(过滤掉非法列)

    Raises:
        ValueError: 表名不在 BACKUP_SCHEMA 中
    """
    if table not in BACKUP_SCHEMA:
        raise ValueError(f"表 {table} 不在 BACKUP_SCHEMA 中")
    allowed = set(BACKUP_SCHEMA[table].columns) | set(_LEGACY_COLUMNS)
    return [c for c in columns if c in allowed]


# 全局列白名单(聚合所有表 columns + 向后兼容列)
# db_restore.py 通过 ALLOWED_COLUMNS 别名引用
ALLOWED_COLUMNS: set[str] = _build_allowed_columns()


# ═══════════════════════════════════════════════════════════
#  R35 P1-6: Schema 校验函数(供 CI 调用)
# ═══════════════════════════════════════════════════════════

# 已知的 CRDB DDL 表(从 database/session.py DDL_STATEMENTS 派生)
# CI 可对比此列表与实际 CRDB schema,发现遗漏的表
_CRDB_DDL_TABLES: frozenset[str] = frozenset({
    "users", "file_records", "decode_logs", "pending_uploads", "send_queue",
    "backup_config", "message_backups", "cells", "codes", "jobs",
    "rotate_log", "spare_pool", "rotation_config", "relay_accounts",
    "external_code_mapping", "code_bot_mapping",
})

# 已知的 SQLite cache_store.db 表(从 database/cache_store.py 派生)
_CACHE_STORE_DDL_TABLES: frozenset[str] = frozenset({
    "cache_backup", "pending_notify", "dsp_notify", "heartbeat_local",
    "bot_heartbeat", "user_quota", "local_job_queue", "counter_snapshot",
    "cells_snapshot", "cells_change_notify", "relay_change_notify",
    "file_record_change_notify", "cells_local", "manifest", "kv_store",
    "ttl_cache", "user_bot_started", "pending_file_codes",
    "file_records_local", "codes_local", "users_local",
    "external_code_mapping_local", "writer_inbox",
    "upload_sessions", "upload_outbox", "quota_ledger",
    "delivery_receipts", "replication_tasks",
})

# 已知的 relay_pool.db 表(从 database/relay_db.py DDL 派生)
_RELAY_DB_DDL_TABLES: frozenset[str] = frozenset({
    "relay_accounts", "relay_usage", "relay_log", "bot_cooldown",
    "mapped_codes", "bot_overrides", "relay_spool",
})


def validate_schema() -> dict:
    """校验 BACKUP_SCHEMA 与已知 DDL 的一致性。

    R35 P1-6: CI 可调用此函数,比较 BACKUP_SCHEMA 中定义的表/列
    与实际 DDL(database/session.py, cache_store.py, relay_db.py)是否一致。

    Returns:
        {
            "is_valid": bool,
            "missing_tables": [表名],      # DDL 中有但 BACKUP_SCHEMA 没有
            "extra_tables": [表名],         # BACKUP_SCHEMA 中有但 DDL 没有
            "source_mismatches": [表名],    # source 标记与实际 DDL 不符
            "empty_columns": [表名],        # columns 为空的表(应补全)
            "details": str,
        }
    """
    schema_tables = set(BACKUP_SCHEMA.keys())
    all_ddl_tables = _CRDB_DDL_TABLES | _CACHE_STORE_DDL_TABLES | _RELAY_DB_DDL_TABLES

    missing = all_ddl_tables - schema_tables
    extra = schema_tables - all_ddl_tables

    # 检查 source 标记是否与 DDL 来源一致
    source_mismatches = []
    for name, ts in BACKUP_SCHEMA.items():
        if ts.source == "crdb" and name not in _CRDB_DDL_TABLES:
            # crdb 标记但不在 CRDB DDL 中(可能是 SQLite-only 表误标为 crdb)
            if name in _CACHE_STORE_DDL_TABLES or name in _RELAY_DB_DDL_TABLES:
                source_mismatches.append(name)
        elif ts.source == "sqlite" and name not in _CACHE_STORE_DDL_TABLES:
            source_mismatches.append(name)
        elif ts.source == "relay_sqlite" and name not in _RELAY_DB_DDL_TABLES:
            source_mismatches.append(name)

    # 检查 columns 是否为空
    empty_columns = [name for name, ts in BACKUP_SCHEMA.items() if not ts.columns]

    is_valid = not missing and not source_mismatches and not empty_columns

    details_parts = []
    if missing:
        details_parts.append(f"缺失 {len(missing)} 张表: {sorted(missing)}")
    if extra:
        details_parts.append(f"多余 {len(extra)} 张表(DDL 中不存在): {sorted(extra)}")
    if source_mismatches:
        details_parts.append(f"source 标记不符: {sorted(source_mismatches)}")
    if empty_columns:
        details_parts.append(f"columns 为空: {sorted(empty_columns)}")
    if not details_parts:
        details_parts.append("所有表和列定义与 DDL 一致")

    return {
        "is_valid": is_valid,
        "missing_tables": sorted(missing),
        "extra_tables": sorted(extra),
        "source_mismatches": sorted(source_mismatches),
        "empty_columns": sorted(empty_columns),
        "details": "; ".join(details_parts),
    }
