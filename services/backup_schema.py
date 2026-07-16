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

R42 P1-7 新增:
- BackupPolicy 枚举:逐表定义 backup/restore 策略
  * MUST_RESTORE         — 必须完整备份与恢复(RBAC/Approval/Audit/Maintenance/Ban/Collections)
  * REBUILDABLE          — 可重建,仅备份 schema 不备份数据(Task/Notification/KV)
  * NO_EXPORT_PLAINTEXT  — 仅备份 schema,数据用 <<REDACTED>> 占位(MFA/Session secret)
  * LOCAL_ONLY           — 不备份,纯本地状态(瞬时缓存)
- BACKUP_POLICY 字典:每张表的 backup_policy 声明
- get_backup_policy / is_must_restore / is_rebuildable / is_no_export 函数

变更检查清单:
1. 新增表时,在此模块的 BACKUP_SCHEMA 字典中添加条目
2. db_backup.py 和 db_restore.py 无需修改(自动从 BACKUP_SCHEMA 生成)
3. 运行 tests/ 确保无回归
"""

from dataclasses import dataclass
from enum import Enum
from services.i18n import translate as _i18n_t


class BackupPolicy(str, Enum):
    """R42 P1-7: 表级 backup/restore 策略枚举。

    继承 str + Enum 使其可直接作为字符串比较(如 policy == "MUST_RESTORE"),
    便于在 SQL / 日志 / 序列化场景使用。

    取值:
        MUST_RESTORE         — 必须完整备份与恢复(RBAC/Approval/Audit 等核心业务表)
        REBUILDABLE          — 可重建,仅备份 schema 不备份数据(Task projection/Notification cache)
        NO_EXPORT_PLAINTEXT  — 仅备份 schema,数据用 <<REDACTED>> 占位(MFA/session secret)
        LOCAL_ONLY           — 不备份,纯本地状态(瞬时缓存/心跳)
    """

    MUST_RESTORE = "must_restore"
    _i18n_t('services.backup_schema.s1')

    REBUILDABLE = "rebuildable"
    _i18n_t('services.backup_schema.s2')

    NO_EXPORT_PLAINTEXT = "no_export_plaintext"
    _i18n_t('services.backup_schema.s3')

    LOCAL_ONLY = "local_only"
    _i18n_t('services.backup_schema.s4')


@dataclass(frozen=True)
class TableSchema:
    """单张表的备份/恢复 schema 定义

    R35 P1-5: source 字段标记表所在数据库:
      - "crdb":        CockroachDB 主库(走 CRDB SELECT *)
      - "sqlite":       cache_store.db 本地表(走 SQLite SELECT *)
      - "relay_sqlite": relay_pool.db 中继库(走 relay SQLite SELECT *)
      - "redis":        Redis(暂不支持快照,标记用)

    R40 P1-9: local_only 字段标记纯本地状态表(不参与 CRDB 同步),
      例如 maintenance_state/admin_access_log 仅本地有效,跨机同步无意义。

    R41 P1-6: backup_order / restore_policy 字段明确每张表的备份/恢复策略:
      - backup_order: 恢复顺序(按外键依赖排序,数字小的先恢复,默认 100)
      - restore_policy: 恢复时的写入策略
        * "skip"               — 不恢复(纯本地状态,如 maintenance_state)
        * "truncate_and_insert" — 清空后写入(默认,保证幂等)
        * "insert_if_not_exists" — 仅在不存在时插入(避免覆盖现有多行)
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
    local_only: bool = False           # R40 P1-9: 纯本地表(不参与 CRDB 同步,如 maintenance_state)
    backup_order: int = 100            # R41 P1-6: 恢复顺序(小数字先恢复,按外键依赖排序)
    restore_policy: str = "truncate_and_insert"  # R41 P1-6: 恢复写入策略


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
        note=_i18n_t('services.backup_schema.s5'),
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
        note=_i18n_t('services.backup_schema.s6'),
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
        note=_i18n_t('services.backup_schema.s7'),
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
        note=_i18n_t('services.backup_schema.s8'),
    ),
    "spare_pool": TableSchema(
        name="spare_pool",
        pk_columns=("channel_id",),
        columns=("channel_id", "account_name", "is_used", "created_at"),
        conflict_col="channel_id",
        note=_i18n_t('services.backup_schema.s9'),
    ),
    "backup_config": TableSchema(
        name="backup_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "updated_at"),
        conflict_col="",
        note=_i18n_t('services.backup_schema.s10'),
    ),
    "rotation_config": TableSchema(
        name="rotation_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "updated_at"),
        conflict_col="",
        note=_i18n_t('services.backup_schema.s11'),
    ),
    "code_bot_mapping": TableSchema(
        name="code_bot_mapping",
        pk_columns=("code_prefix",),
        columns=("code_prefix", "bot_username", "created_at"),
        conflict_col="",
        note=_i18n_t('services.backup_schema.s12'),
    ),
    "external_code_mapping": TableSchema(
        name="external_code_mapping",
        pk_columns=("external_code",),
        columns=("external_code", "system_code", "bot_username", "created_at", "updated_at"),
        conflict_col="external_code",
        note=_i18n_t('services.backup_schema.s13'),
    ),
    "kv_config": TableSchema(
        name="kv_config",
        pk_columns=("config_key",),
        columns=("config_key", "config_value", "created_at", "updated_at"),
        conflict_col="config_key",
        note=_i18n_t('services.backup_schema.s14'),
    ),
    "message_backups": TableSchema(
        name="message_backups",
        pk_columns=("main_msg_id", "backup_channel_id"),  # 复合主键
        columns=(
            "main_msg_id", "backup_channel_id",
            "backed_msg_id", "backed_at",
        ),
        conflict_col="",  # 复合主键,由 restore 逻辑用 ON CONFLICT (col1, col2) 处理
        note=_i18n_t('services.backup_schema.s15'),
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
        note=_i18n_t('services.backup_schema.s16'),
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
        note=_i18n_t('services.backup_schema.s17'),
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
        note=_i18n_t('services.backup_schema.s18'),
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
        note=_i18n_t('services.backup_schema.s19'),
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
        note=_i18n_t('services.backup_schema.s20'),
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
        note=_i18n_t('services.backup_schema.s21'),
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
        note=_i18n_t('services.backup_schema.s22'),
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
        note=_i18n_t('services.backup_schema.s23'),
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
        note=_i18n_t('services.backup_schema.s24'),
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
        note=_i18n_t('services.backup_schema.s25'),
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
        note=_i18n_t('services.backup_schema.s26'),
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
        note=_i18n_t('services.backup_schema.s27'),
    ),

    # ─── R40 P1-9: 12 张新业务表(均为 SQLite-only) ───
    # 注意:依赖顺序需保证父表在子表之前(rbac_roles → rbac_user_roles,
    #       collections → collection_items),便于 db_restore 按列表顺序恢复。
    "tasks": TableSchema(
        name="tasks",
        pk_columns=("id",),
        columns=(
            "id", "task_type", "user_id", "status", "progress",
            "eta_seconds", "payload", "result", "error", "trace_id",
            "created_at", "updated_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s28'),
    ),
    "collections": TableSchema(
        name="collections",
        pk_columns=("id",),
        columns=(
            "id", "name", "code", "owner_id", "description",
            "version", "item_count", "status", "created_at", "updated_at",
        ),
        conflict_col="",
        source="sqlite",
        backup_order=10,  # R41 P1-6: 父表先恢复(在 collection_items 之前)
        note=_i18n_t('services.backup_schema.s29'),
    ),
    "collection_items": TableSchema(
        name="collection_items",
        pk_columns=("id",),
        columns=("id", "collection_id", "file_code", "added_at"),
        conflict_col="",
        source="sqlite",
        backup_order=11,  # R41 P1-6: 子表后恢复(FK collection_id→collections.id)
        note=_i18n_t('services.backup_schema.s30'),
    ),
    "notifications": TableSchema(
        name="notifications",
        pk_columns=("id",),
        columns=(
            "id", "user_id", "type", "payload", "is_read",
            "created_at", "read_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s31'),
    ),
    "content_reports": TableSchema(
        name="content_reports",
        pk_columns=("id",),
        columns=(
            "id", "reporter_id", "target_type", "target_id", "reason",
            "description", "status", "appeal_text", "appealed_at",
            "resolved_by", "resolved_at", "created_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s32'),
    ),
    "audit_log": TableSchema(
        name="audit_log",
        pk_columns=("id",),
        columns=(
            "id", "actor_id", "actor_type", "action", "target_type",
            "target_id", "details", "ip_addr", "created_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s33'),
    ),
    "quota_reservations": TableSchema(
        name="quota_reservations",
        pk_columns=("id",),
        columns=(
            "id", "user_id", "amount", "reason", "status",
            "actual_amount", "created_at", "settled_at", "expired_at",
        ),
        conflict_col="id",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s34'),
    ),
    "rbac_roles": TableSchema(
        name="rbac_roles",
        pk_columns=("id",),
        columns=("id", "name", "description", "permissions", "created_at"),
        conflict_col="name",
        source="sqlite",
        backup_order=10,  # R41 P1-6: 父表先恢复(在 rbac_user_roles 之前)
        note=_i18n_t('services.backup_schema.s35'),
    ),
    "rbac_user_roles": TableSchema(
        name="rbac_user_roles",
        pk_columns=("user_id",),
        columns=("user_id", "role_id", "assigned_at", "assigned_by"),
        conflict_col="user_id",
        source="sqlite",
        backup_order=11,  # R41 P1-6: 子表后恢复(FK role_id→rbac_roles.id)
        note=_i18n_t('services.backup_schema.s36'),
    ),
    "approvals": TableSchema(
        name="approvals",
        pk_columns=("id",),
        columns=(
            "id", "action", "payload", "status", "approver_id",
            "approver_note", "created_by", "created_at", "resolved_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s37'),
    ),
    "maintenance_state": TableSchema(
        name="maintenance_state",
        pk_columns=("id",),
        columns=(
            "id", "enabled", "reason", "started_by",
            "started_at", "ended_at",
        ),
        conflict_col="",
        source="sqlite",
        local_only=True,  # R40 P1-9: 纯本地状态,不参与 CRDB 同步
        backup_order=200,  # R41 P1-6: 本地状态最后恢复
        restore_policy="skip",  # R41 P1-6: 不恢复(避免覆盖本机维护状态)
        note=_i18n_t('services.backup_schema.s38'),
    ),
    "admin_access_log": TableSchema(
        name="admin_access_log",
        pk_columns=("id",),
        columns=(
            "id", "admin_id", "action", "target_type", "target_id",
            "details", "ip_addr", "created_at",
        ),
        conflict_col="",
        source="sqlite",
        local_only=True,  # R40 P1-9: 本地访问日志,不参与 CRDB 同步
        backup_order=201,  # R41 P1-6: 本地审计日志最后恢复
        restore_policy="insert_if_not_exists",  # R41 P1-6: 追加式恢复(不覆盖现有日志)
        note=_i18n_t('services.backup_schema.s39'),
    ),

    # ─── R41 P1-6: 6 张新业务表(均为 SQLite-only) ───
    # mfa_secrets: 用户 TOTP 密钥,跨实例需要(用户登录凭证)
    # sessions: 服务端 session 令牌,纯本地状态(恢复会创建过期 session)
    # command_outbox: 事务发件箱,需备份(命令可能未执行完)
    # command_executions: 命令执行记录,需备份(关联 outbox)
    # dlq_records: 死信队列记录,需备份(用于审计)
    # ban_state: 用户封禁状态,需备份(跨实例一致)
    "mfa_secrets": TableSchema(
        name="mfa_secrets",
        pk_columns=("user_id",),
        columns=(
            "user_id", "secret", "enabled", "backup_codes",
            "created_at", "updated_at",
        ),
        conflict_col="user_id",
        source="sqlite",
        backup_order=80,  # R41 P1-6: 在用户表后恢复
        restore_policy="truncate_and_insert",  # R41 P1-6: 覆盖恢复(凭证以备份为准)
        note=_i18n_t('services.backup_schema.s40'),
    ),
    "sessions": TableSchema(
        name="sessions",
        pk_columns=("session_id",),
        columns=(
            "session_id", "user_id", "principal_id", "username",
            "created_at", "expires_at", "last_activity_at",
            "ip_addr", "user_agent",
        ),
        conflict_col="session_id",
        source="sqlite",
        local_only=True,  # R41 P1-6: 纯本地状态(session 不可跨实例共享)
        backup_order=210,  # R41 P1-6: 本地状态最后恢复
        restore_policy="skip",  # R41 P1-6: 不恢复(避免过期 session 重新激活)
        note=_i18n_t('services.backup_schema.s41'),
    ),
    "command_outbox": TableSchema(
        name="command_outbox",
        pk_columns=("id",),
        columns=(
            "id", "command_type", "target_type", "target_id",
            "payload", "status", "priority", "created_by",
            "created_at", "processed_at", "attempts", "last_error",
        ),
        conflict_col="",
        source="sqlite",
        backup_order=50,  # R41 P1-6: 命令发件箱先恢复(在 command_executions 之前)
        restore_policy="insert_if_not_exists",  # R41 P1-6: 不覆盖进行中的命令
        note=_i18n_t('services.backup_schema.s42'),
    ),
    "command_executions": TableSchema(
        name="command_executions",
        pk_columns=("id",),
        columns=(
            "id", "outbox_id", "executor", "status",
            "started_at", "finished_at", "result", "error",
            "attempts",
        ),
        conflict_col="",
        source="sqlite",
        backup_order=51,  # R41 P1-6: 命令执行记录后恢复(FK outbox_id→command_outbox.id)
        restore_policy="insert_if_not_exists",  # R41 P1-6: 不覆盖执行记录
        note=_i18n_t('services.backup_schema.s43'),
    ),
    "dlq_records": TableSchema(
        name="dlq_records",
        pk_columns=("id",),
        columns=(
            "id", "source", "original_payload", "error",
            "failed_at", "attempts", "last_attempt_at",
            "resolved", "resolved_at", "resolved_by",
        ),
        conflict_col="",
        source="sqlite",
        backup_order=60,  # R41 P1-6: 死信队列后恢复
        restore_policy="insert_if_not_exists",  # R41 P1-6: 追加式恢复(不覆盖)
        note=_i18n_t('services.backup_schema.s44'),
    ),
    "ban_state": TableSchema(
        name="ban_state",
        pk_columns=("user_id",),
        columns=(
            "user_id", "is_banned", "banned_at", "banned_by",
            "reason", "expires_at", "unbanned_at", "unbanned_by",
        ),
        conflict_col="user_id",
        source="sqlite",
        backup_order=70,  # R41 P1-6: 用户封禁状态后恢复
        restore_policy="truncate_and_insert",  # R41 P1-6: 覆盖恢复(最新封禁状态以备份为准)
        note=_i18n_t('services.backup_schema.s45'),
    ),

    # ─── SQLite 本地缓存表(热路径零 CRDB,部分需要备份) ───
    "kv_store": TableSchema(
        name="kv_store",
        pk_columns=("key",),
        columns=("key", "value"),
        conflict_col="key",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s46'),
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
        note=_i18n_t('services.backup_schema.s47'),
    ),
    "pending_file_codes": TableSchema(
        name="pending_file_codes",
        pk_columns=("id",),
        columns=(
            "id", "user_id", "file_code", "note", "ext_code", "created_at",
        ),
        conflict_col="",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s48'),
    ),
    "cache_backup": TableSchema(
        name="cache_backup",
        pk_columns=("key",),
        columns=("key", "value", "ts"),
        conflict_col="key",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s49'),
    ),
    "heartbeat_local": TableSchema(
        name="heartbeat_local",
        pk_columns=("slot_id",),
        columns=("slot_id", "last_ok", "fail_streak"),
        conflict_col="slot_id",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s50'),
    ),
    "bot_heartbeat": TableSchema(
        name="bot_heartbeat",
        pk_columns=("name",),
        columns=("name", "last_ping", "is_running", "total_processed", "total_errors"),
        conflict_col="name",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s51'),
    ),
    "counter_snapshot": TableSchema(
        name="counter_snapshot",
        pk_columns=("key",),
        columns=("key", "value", "ts"),
        conflict_col="key",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s52'),
    ),
    "ttl_cache": TableSchema(
        name="ttl_cache",
        pk_columns=("key",),
        columns=("key", "value", "updated_at"),
        conflict_col="key",
        source="sqlite",
        note=_i18n_t('services.backup_schema.s53'),
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
        note=_i18n_t('services.backup_schema.s54'),
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
        note=_i18n_t('services.backup_schema.s55'),
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
        note=_i18n_t('services.backup_schema.s56'),
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
        note=_i18n_t('services.backup_schema.s57'),
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
        note=_i18n_t('services.backup_schema.s58'),
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
        note=_i18n_t('services.backup_schema.s59'),
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
        note=_i18n_t('services.backup_schema.s60'),
    ),
}


# ════════════════════════════════════════════════════════════════
#  R42 P1-7: 逐表 Backup/Restore Policy 声明
# ════════════════════════════════════════════════════════════════
#
# 设计原则:
#   - MUST_RESTORE         核心业务数据(RBAC/Approval/Audit/Maintenance/Ban/Collections):
#                          丢失会破坏系统完整性,必须完整备份与恢复
#   - REBUILDABLE          可重建数据(Task projection/Notification cache):
#                          schema 备份,数据由系统运行时重建(避免备份大量瞬时数据)
#   - NO_EXPORT_PLAINTEXT  敏感数据(MFA secret/Session secret/Password hash):
#                          schema 备份,数据用 <<REDACTED>> 占位
#                          恢复时保持初始值,强制用户重新设置(防止明文泄漏)
#   - LOCAL_ONLY           瞬时状态(心跳/缓存/计数器):不备份
#
# 与 TABLE_REPLICATION_POLICY(replication_policy.py)协调:
#   - NO_EXPORT_PLAINTEXT 的表不能是 CRDB 同步表(避免明文跨节点复制)
#   - LOCAL_ONLY 的表可以与 ReplicationPolicy.LOCAL_ONLY 重合
BACKUP_POLICY: dict[str, "BackupPolicy"] = {
    # ── MUST_RESTORE:核心业务表(完整备份与恢复)──
    # 用户/取件码/文件(权威业务数据)
    "users": BackupPolicy.MUST_RESTORE,
    "users_local": BackupPolicy.MUST_RESTORE,
    "file_records": BackupPolicy.MUST_RESTORE,
    "file_records_local": BackupPolicy.MUST_RESTORE,
    "codes": BackupPolicy.MUST_RESTORE,
    "codes_local": BackupPolicy.MUST_RESTORE,
    # 频道/槽位/轮转(拓扑核心)
    "cells": BackupPolicy.MUST_RESTORE,
    "cells_local": BackupPolicy.MUST_RESTORE,
    "spare_pool": BackupPolicy.MUST_RESTORE,
    "backup_config": BackupPolicy.MUST_RESTORE,
    "rotation_config": BackupPolicy.MUST_RESTORE,
    "code_bot_mapping": BackupPolicy.MUST_RESTORE,
    "external_code_mapping": BackupPolicy.MUST_RESTORE,
    "external_code_mapping_local": BackupPolicy.MUST_RESTORE,
    "kv_config": BackupPolicy.MUST_RESTORE,
    "message_backups": BackupPolicy.MUST_RESTORE,
    "relay_accounts": BackupPolicy.MUST_RESTORE,
    "mapped_codes": BackupPolicy.MUST_RESTORE,
    "relay_spool": BackupPolicy.MUST_RESTORE,
    # RBAC(权限核心)
    "rbac_roles": BackupPolicy.MUST_RESTORE,
    "rbac_user_roles": BackupPolicy.MUST_RESTORE,
    # 审批/命令(管理面核心)
    "approvals": BackupPolicy.MUST_RESTORE,
    "command_outbox": BackupPolicy.MUST_RESTORE,
    "command_executions": BackupPolicy.MUST_RESTORE,
    # 审计/封禁/维护(合规核心)
    "audit_log": BackupPolicy.MUST_RESTORE,
    "maintenance_state": BackupPolicy.MUST_RESTORE,
    "content_reports": BackupPolicy.MUST_RESTORE,
    "ban_state": BackupPolicy.MUST_RESTORE,
    # 文件集合(用户数据)
    "collections": BackupPolicy.MUST_RESTORE,
    "collection_items": BackupPolicy.MUST_RESTORE,
    # 配额预留(业务事务)
    "quota_reservations": BackupPolicy.MUST_RESTORE,
    "quota_ledger": BackupPolicy.MUST_RESTORE,
    "delivery_receipts": BackupPolicy.MUST_RESTORE,
    "replication_tasks": BackupPolicy.MUST_RESTORE,
    # 副本元数据
    "manifest": BackupPolicy.MUST_RESTORE,
    "writer_inbox": BackupPolicy.MUST_RESTORE,
    "upload_sessions": BackupPolicy.MUST_RESTORE,
    "upload_outbox": BackupPolicy.MUST_RESTORE,
    # 死信队列(审计需要)
    "dlq_records": BackupPolicy.MUST_RESTORE,
    "admin_access_log": BackupPolicy.MUST_RESTORE,

    # ── REBUILDABLE:可重建数据(仅备份 schema)──
    "tasks": BackupPolicy.REBUILDABLE,                # 任务投影,可由 outbox 重建
    "notifications": BackupPolicy.REBUILDABLE,        # 通知缓存,可由 outbox 重建
    "task_progress": BackupPolicy.REBUILDABLE,         # 任务进度(预留)
    "kv_store": BackupPolicy.REBUILDABLE,              # KV 缓存,可由业务重建
    "user_quota": BackupPolicy.REBUILDABLE,            # 用户配额,可由 CRDB 重建
    "pending_file_codes": BackupPolicy.REBUILDABLE,    # 待发送文件码,瞬时状态
    "cache_backup": BackupPolicy.REBUILDABLE,          # 内存缓存持久化
    "counter_snapshot": BackupPolicy.REBUILDABLE,       # 计数器快照
    "ttl_cache": BackupPolicy.REBUILDABLE,              # TTL 缓存

    # ── NO_EXPORT_PLAINTEXT:敏感数据(schema 备份,数据 <<REDACTED>>)──
    "mfa_secrets": BackupPolicy.NO_EXPORT_PLAINTEXT,         # MFA TOTP 密钥
    "sessions": BackupPolicy.NO_EXPORT_PLAINTEXT,            # 服务端 session 令牌
    "admin_password_hashes": BackupPolicy.NO_EXPORT_PLAINTEXT,  # 管理员密码哈希

    # ── LOCAL_ONLY:瞬时状态,不备份 ──
    "heartbeat_local": BackupPolicy.LOCAL_ONLY,        # 心跳本地表(瞬时)
    "bot_heartbeat": BackupPolicy.LOCAL_ONLY,           # Bot 心跳(瞬时)
    "decode_logs": BackupPolicy.LOCAL_ONLY,             # 解码日志(短期流水)
    "jobs": BackupPolicy.LOCAL_ONLY,                    # 异步任务(短期流水)
    "rotate_log": BackupPolicy.LOCAL_ONLY,              # 轮转审计日志(流水)
    "pending_uploads": BackupPolicy.LOCAL_ONLY,         # 待上传队列(瞬时)
}


# R42 P1-7: 默认 backup_policy — 未在 BACKUP_POLICY 中声明的表
# 默认 LOCAL_ONLY(fail-closed,避免误备份未知/拼写错误的表)
_DEFAULT_BACKUP_POLICY: "BackupPolicy" = BackupPolicy.LOCAL_ONLY


def get_backup_policy(table_name: str) -> "BackupPolicy":
    """R42 P1-7: 查询表的 backup/restore policy。

    未声明的表返回 LOCAL_ONLY(fail-closed,避免误备份未知表)。

    Args:
        table_name: 逻辑表名(与 BACKUP_SCHEMA 对齐)

    Returns:
        BackupPolicy 枚举(MUST_RESTORE / REBUILDABLE / NO_EXPORT_PLAINTEXT / LOCAL_ONLY)
    """
    return BACKUP_POLICY.get(table_name, _DEFAULT_BACKUP_POLICY)


def is_must_restore(table_name: str) -> bool:
    """R42 P1-7: 判断表是否为 MUST_RESTORE 策略。

    MUST_RESTORE 表必须完整备份与恢复,丢失会导致数据损坏。

    Args:
        table_name: 逻辑表名

    Returns:
        True 若该表必须完整备份与恢复
    """
    return get_backup_policy(table_name) is BackupPolicy.MUST_RESTORE


def is_rebuildable(table_name: str) -> bool:
    """R42 P1-7: 判断表是否为 REBUILDABLE 策略。

    REBUILDABLE 表仅备份 schema,数据由系统运行时重建。

    Args:
        table_name: 逻辑表名

    Returns:
        True 若该表可重建(仅备份 schema)
    """
    return get_backup_policy(table_name) is BackupPolicy.REBUILDABLE


def is_no_export(table_name: str) -> bool:
    """R42 P1-7: 判断表是否为 NO_EXPORT_PLAINTEXT 策略。

    NO_EXPORT_PLAINTEXT 表仅备份 schema,数据用 <<REDACTED>> 占位。
    恢复时保持初始值,强制用户重新设置(防止明文泄漏)。

    Args:
        table_name: 逻辑表名

    Returns:
        True 若该表不得导出明文
    """
    return get_backup_policy(table_name) is BackupPolicy.NO_EXPORT_PLAINTEXT


def is_local_only_backup(table_name: str) -> bool:
    """R42 P1-7: 判断表是否为 LOCAL_ONLY backup 策略。

    LOCAL_ONLY 表不参与备份,纯本地瞬时状态。

    Args:
        table_name: 逻辑表名

    Returns:
        True 若该表不备份
    """
    return get_backup_policy(table_name) is BackupPolicy.LOCAL_ONLY


def get_tables_by_backup_policy(policy: "BackupPolicy") -> list[str]:
    """R42 P1-7: 返回指定 policy 的所有表名列表(用于按 policy 分组备份/恢复)。

    Args:
        policy: BackupPolicy 枚举值

    Returns:
        该 policy 下所有显式声明的表名列表(不含默认 LOCAL_ONLY 的表)
    """
    return [
        table for table, p in BACKUP_POLICY.items()
        if p is policy
    ]


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


# ─── R41 P1-6: 按依赖排序的恢复顺序 ───────────────────────────

# restore_policy 允许值(用于校验输入)
RESTORE_POLICY_SKIP = "skip"
RESTORE_POLICY_TRUNCATE_AND_INSERT = "truncate_and_insert"
RESTORE_POLICY_INSERT_IF_NOT_EXISTS = "insert_if_not_exists"
_VALID_RESTORE_POLICIES = frozenset({
    RESTORE_POLICY_SKIP,
    RESTORE_POLICY_TRUNCATE_AND_INSERT,
    RESTORE_POLICY_INSERT_IF_NOT_EXISTS,
})


def get_restore_order(include_local_only: bool = True) -> list[str]:
    """R41 P1-6: 返回按 backup_order 排序的恢复顺序表列表。

    按外键依赖排序:backup_order 数字小的先恢复(父表 → 子表)。
    同一 backup_order 内按 BACKUP_SCHEMA 字典序保持稳定。

    Args:
        include_local_only: 是否包含 local_only=True 的表
            - True(默认):包含所有表(用于完整恢复计划)
            - False:排除 local_only 表(用于跨机同步场景)

    Returns:
        按恢复顺序排序的表名列表
    """
    candidates = [
        t for t in BACKUP_SCHEMA.values()
        if include_local_only or not t.local_only
    ]
    # 按 (backup_order, name) 排序,保证稳定顺序
    candidates.sort(key=lambda t: (t.backup_order, t.name))
    return [t.name for t in candidates]


def get_restore_policy(table: str) -> str:
    """R41 P1-6: 返回表的恢复写入策略。

    Returns:
        "skip" / "truncate_and_insert" / "insert_if_not_exists"

    Raises:
        ValueError: 表名不在 BACKUP_SCHEMA 中
    """
    if table not in BACKUP_SCHEMA:
        raise ValueError(_i18n_t('services.backup_schema.s61', table=table))
    return BACKUP_SCHEMA[table].restore_policy


def get_skip_restore_tables() -> list[str]:
    """R41 P1-6: 返回 restore_policy=skip 的表列表(恢复时跳过)。

    这些表通常是纯本地状态(maintenance_state/sessions),
    恢复会覆盖本机运行时状态,因此跳过。
    """
    return [
        t.name for t in BACKUP_SCHEMA.values()
        if t.restore_policy == RESTORE_POLICY_SKIP
    ]


def get_backup_order(table: str) -> int:
    """R41 P1-6: 返回表的恢复顺序数字(小数字先恢复)。

    Raises:
        ValueError: 表名不在 BACKUP_SCHEMA 中
    """
    if table not in BACKUP_SCHEMA:
        raise ValueError(_i18n_t('services.backup_schema.s62', table=table))
    return BACKUP_SCHEMA[table].backup_order


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
        raise ValueError(_i18n_t('services.backup_schema.s63', table=table))
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
# R40 P1-9: 补齐 12 张新业务表(tasks/collections/collection_items/notifications/
#           content_reports/audit_log/quota_reservations/rbac_roles/rbac_user_roles/
#           approvals/maintenance_state/admin_access_log)
# R41 P1-6: 补齐 6 张新业务表(mfa_secrets/sessions/command_outbox/
#           command_executions/dlq_records/ban_state)
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
    # R40 P1-9: 12 张新业务表
    "tasks", "collections", "collection_items", "notifications",
    "content_reports", "audit_log", "quota_reservations",
    "rbac_roles", "rbac_user_roles", "approvals",
    "maintenance_state", "admin_access_log",
    # R41 P1-6: 6 张新业务表
    "mfa_secrets", "sessions", "command_outbox",
    "command_executions", "dlq_records", "ban_state",
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
        details_parts.append(_i18n_t('services.backup_schema.s64', len_missing=len(missing), sorted_missing=sorted(missing)))
    if extra:
        details_parts.append(_i18n_t('services.backup_schema.s65', len_extra=len(extra), sorted_extra=sorted(extra)))
    if source_mismatches:
        details_parts.append(_i18n_t('services.backup_schema.s66', sorted_source_mismatches=sorted(source_mismatches)))
    if empty_columns:
        details_parts.append(_i18n_t('services.backup_schema.s67', sorted_empty_columns=sorted(empty_columns)))
    if not details_parts:
        details_parts.append(_i18n_t('services.backup_schema.s68'))

    return {
        "is_valid": is_valid,
        "missing_tables": sorted(missing),
        "extra_tables": sorted(extra),
        "source_mismatches": sorted(source_mismatches),
        "empty_columns": sorted(empty_columns),
        "details": "; ".join(details_parts),
    }
