"""R41 P0-6: 表级复制策略集中声明模块。

将"哪些表同步到 CRDB / 哪些表仅本地 / 哪些表走归档"的决策从 crdb_sync_service
与 cache_store 两处分散硬编码,收口到本模块统一维护,避免双处声明漂移。

策略分类:
  - CRDB:        需要同步到 CockroachDB 的权威业务表(users / file_records / codes / cells / jobs ...)
  - LOCAL_ONLY:  仅存在于 SQLite 本地的表(tasks / approvals / RBAC / sessions / kv_store ...)
                 这些表的 dirty_outbox 记录直接预标记 processed=1 + local_only=1,
                 crdb_sync dispatcher 不会拉取,避免无意义堆积。
  - ARCHIVE_ONLY: 冷归档表(audit_log_archive),不进 CRDB,
                 由独立 backup job 归档到 R2 / 外部存储,与 crdb_sync 解耦。

默认策略(fail-closed):
  未知表名(未在 TABLE_REPLICATION_POLICY 中声明)默认 LOCAL_ONLY,
  避免误将本地临时表/拼写错误的表名同步到 CRDB 污染权威数据。

调用方:
  - database.cache_store.add_dirty_outbox(): 检查 is_local_only() 决定是否预标记
  - services.crdb_sync_service._dispatch_dirty_outbox_to_crdb(): 检查 is_archive_only()
    / is_crdb() 决定分发路径;all_local_only_tables() / all_crdb_tables() 派生
    _LOCAL_ONLY_TABLES / _CRDB_TABLES 集合(模块异常时降级到硬编码 fallback)。
"""
from __future__ import annotations

from enum import Enum


class ReplicationPolicy(str, Enum):
    """R41 P0-6: 表级复制策略枚举。

    继承 str + Enum 使其可直接作为字符串比较(如 policy == "CRDB"),
    便于在 SQL / 日志 / 序列化场景使用。
    """

    CRDB = "crdb"
    """需要同步到 CockroachDB 的权威业务表。"""

    LOCAL_ONLY = "local_only"
    """仅存在于 SQLite 本地,不同步到 CRDB。"""

    ARCHIVE_ONLY = "archive_only"
    """冷归档表,不进 CRDB,由独立 backup job 归档到 R2 / 外部存储。"""


# R41 P0-6: 表级复制策略声明表
# 每个键为逻辑表名(与 dirty_outbox.table_name 对齐),
# 值为 ReplicationPolicy 枚举。
# 新增表时必须在此声明,未声明的表默认 LOCAL_ONLY(fail-closed)。
TABLE_REPLICATION_POLICY: dict[str, ReplicationPolicy] = {
    # ── CRDB 同步表(10 张,权威业务数据,跨节点一致性)──
    "users": ReplicationPolicy.CRDB,
    "file_records": ReplicationPolicy.CRDB,
    "codes": ReplicationPolicy.CRDB,
    "decode_logs": ReplicationPolicy.CRDB,
    "relay_whitelist": ReplicationPolicy.CRDB,
    "collector_whitelist": ReplicationPolicy.CRDB,
    "spare_pool": ReplicationPolicy.CRDB,
    "channels": ReplicationPolicy.CRDB,
    "cells": ReplicationPolicy.CRDB,
    "jobs": ReplicationPolicy.CRDB,

    # ── LOCAL_ONLY 表(仅 SQLite 本地,不进 CRDB)──
    # 统一任务中心 / 文件集合 / 通知
    "tasks": ReplicationPolicy.LOCAL_ONLY,
    "collections": ReplicationPolicy.LOCAL_ONLY,
    "collection_items": ReplicationPolicy.LOCAL_ONLY,
    "notifications": ReplicationPolicy.LOCAL_ONLY,
    "content_reports": ReplicationPolicy.LOCAL_ONLY,
    # 审计 / 配额(本地快查,不阻塞 CRDB)
    "audit_log": ReplicationPolicy.LOCAL_ONLY,
    "quota_reservations": ReplicationPolicy.LOCAL_ONLY,
    "quota_ledger": ReplicationPolicy.LOCAL_ONLY,
    # RBAC / 审批 / 维护状态(管理面本地决策)
    "rbac_roles": ReplicationPolicy.LOCAL_ONLY,
    "rbac_user_roles": ReplicationPolicy.LOCAL_ONLY,
    "approvals": ReplicationPolicy.LOCAL_ONLY,
    "maintenance_state": ReplicationPolicy.LOCAL_ONLY,
    "admin_access_log": ReplicationPolicy.LOCAL_ONLY,
    "command_outbox": ReplicationPolicy.LOCAL_ONLY,
    "command_executions": ReplicationPolicy.LOCAL_ONLY,
    # 安全 / 会话(敏感数据仅本地)
    "mfa_secrets": ReplicationPolicy.LOCAL_ONLY,
    "sessions": ReplicationPolicy.LOCAL_ONLY,
    "ban_state": ReplicationPolicy.LOCAL_ONLY,
    # 缓存 / 临时(本地 KV 与 TTL,无需跨节点)
    "kv_store": ReplicationPolicy.LOCAL_ONLY,
    "ttl_cache": ReplicationPolicy.LOCAL_ONLY,
    # 文件上传队列(SQLite 本地权威,Idx Bot 仅从 SQLite 读取)
    "pending_uploads": ReplicationPolicy.LOCAL_ONLY,
    # dirty_outbox / DLQ 自身(发件箱表,不进 CRDB)
    "dirty_outbox": ReplicationPolicy.LOCAL_ONLY,
    "dlq": ReplicationPolicy.LOCAL_ONLY,
    "dlq_records": ReplicationPolicy.LOCAL_ONLY,

    # ── ARCHIVE_ONLY 表(冷归档,不进 CRDB,走 R2)──
    "audit_log_archive": ReplicationPolicy.ARCHIVE_ONLY,
}


# R41 P0-6: 默认策略 — 未在 TABLE_REPLICATION_POLICY 中声明的表
# 默认 LOCAL_ONLY(fail-closed,避免误同步本地临时表/拼写错误)
_DEFAULT_POLICY: ReplicationPolicy = ReplicationPolicy.LOCAL_ONLY


def get_policy(table: str) -> ReplicationPolicy:
    """R41 P0-6: 查询表的复制策略。

    未声明的表返回 LOCAL_ONLY(fail-closed,避免误将本地临时表同步到 CRDB)。

    Args:
        table: 逻辑表名(与 dirty_outbox.table_name 对齐)

    Returns:
        ReplicationPolicy 枚举(CRDB / LOCAL_ONLY / ARCHIVE_ONLY)
    """
    return TABLE_REPLICATION_POLICY.get(table, _DEFAULT_POLICY)


def is_local_only(table: str) -> bool:
    """R41 P0-6: 判断表是否为 LOCAL_ONLY 策略。

    用于 cache_store.add_dirty_outbox() 预标记 processed=1 + local_only=1,
    避免 crdb_sync dispatcher 重复拉取永远不会同步到 CRDB 的记录。

    Args:
        table: 逻辑表名

    Returns:
        True 若该表仅本地存在(不同步到 CRDB)
    """
    return get_policy(table) is ReplicationPolicy.LOCAL_ONLY


def is_crdb(table: str) -> bool:
    """R41 P0-6: 判断表是否为 CRDB 同步策略。

    用于 crdb_sync._dispatch_dirty_outbox_to_crdb() 校验 CRDB 表必须同时
    提供 upsert + tombstone handler,否则进入 DLQ。

    Args:
        table: 逻辑表名

    Returns:
        True 若该表需要同步到 CockroachDB
    """
    return get_policy(table) is ReplicationPolicy.CRDB


def is_archive_only(table: str) -> bool:
    """R41 P0-6: 判断表是否为 ARCHIVE_ONLY 策略。

    ARCHIVE_ONLY 表(audit_log_archive)不进 CRDB,
    由独立 backup job 归档到 R2 / 外部存储,与 crdb_sync 解耦。

    Args:
        table: 逻辑表名

    Returns:
        True 若该表走冷归档(不同步到 CRDB)
    """
    return get_policy(table) is ReplicationPolicy.ARCHIVE_ONLY


def all_local_only_tables() -> set[str]:
    """R41 P0-6: 返回所有 LOCAL_ONLY 策略的表名集合。

    用于 crdb_sync_service 派生 _LOCAL_ONLY_TABLES 集合(模块加载时一次性派生,
    避免每次 dispatch 都遍历 TABLE_REPLICATION_POLICY)。

    Returns:
        LOCAL_ONLY 表名集合(str)
    """
    return {
        table for table, policy in TABLE_REPLICATION_POLICY.items()
        if policy is ReplicationPolicy.LOCAL_ONLY
    }


def all_crdb_tables() -> set[str]:
    """R41 P0-6: 返回所有 CRDB 同步策略的表名集合。

    用于 crdb_sync_service 派生 _CRDB_TABLES 集合,
    并校验 CRDB 表的 handler 覆盖完整性(upsert + tombstone)。

    Returns:
        CRDB 同步表名集合(str)
    """
    return {
        table for table, policy in TABLE_REPLICATION_POLICY.items()
        if policy is ReplicationPolicy.CRDB
    }


def all_archive_only_tables() -> set[str]:
    """R41 P0-6: 返回所有 ARCHIVE_ONLY 策略的表名集合。

    用于诊断 / 监控 / 归档 job 枚举需要归档的表。

    Returns:
        ARCHIVE_ONLY 表名集合(str)
    """
    return {
        table for table, policy in TABLE_REPLICATION_POLICY.items()
        if policy is ReplicationPolicy.ARCHIVE_ONLY
    }


def all_declared_tables() -> set[str]:
    """R41 P0-6: 返回所有已声明策略的表名集合(用于诊断 / 测试)。

    Returns:
        所有已声明表名集合(str)
    """
    return set(TABLE_REPLICATION_POLICY.keys())


# ════════════════════════════════════════════════════════════════
#  R42 P1-5: Tombstone soft-delete 策略
# ════════════════════════════════════════════════════════════════


# R42 P1-5: Tombstone 策略枚举字符串常量
# - soft_delete: CRDB 表保留 tombstone(UPDATE deleted_at + is_tombstone=1),
#   物理清理由 retention_worker 在备份保留后执行(默认 30 天)。
# - hard_delete: 表不支持 soft_delete(无 deleted_at 字段),fallback 到 DELETE,
#   并写 audit_log 标记 fallback 路径(便于审计追溯)。
# - no_tombstone: LOCAL_ONLY 表不需要 tombstone(仅本地,不同步到 CRDB)。
TOMBSTONE_POLICY_SOFT_DELETE = "soft_delete"
TOMBSTONE_POLICY_HARD_DELETE = "hard_delete"
TOMBSTONE_POLICY_NO_TOMBSTONE = "no_tombstone"


def get_tombstone_policy(table_name: str) -> str:
    """R42 P1-5: 查询表的 tombstone 处理策略。

    决策规则:
        - CRDB 同步表 → soft_delete(默认保留 tombstone,供备份恢复使用)
        - LOCAL_ONLY 表 → no_tombstone(仅本地存在,不需要 CRDB tombstone)
        - ARCHIVE_ONLY 表 → no_tombstone(冷归档,不进 CRDB)
        - 未知表(fail-closed) → no_tombstone(默认 LOCAL_ONLY,不产生 tombstone)

    返回值:
        - "soft_delete": UPDATE deleted_at + is_tombstone=1
        - "hard_delete": DELETE FROM <table> WHERE pk=? + audit_log(此值不会
          由 get_tombstone_policy 直接返回,仅作为运行时 fallback 标记)
        - "no_tombstone": 跳过 tombstone 处理(LOCAL_ONLY / 未知表)

    与 _is_crdb_table_supports_soft_delete 的区别:
        - get_tombstone_policy 是基于 replication_policy 的逻辑策略
          (该表"应该"走什么 tombstone 路径)
        - _is_crdb_table_supports_soft_delete 是 CRDB schema 探测
          (该表实际是否支持 deleted_at + is_tombstone 字段)
        - 运行时优先用 _is_crdb_table_supports_soft_delete 探测实际能力;
          get_tombstone_policy 用于诊断 / 监控 / 默认策略推断

    Args:
        table_name: 逻辑表名(与 dirty_outbox.table_name 对齐)

    Returns:
        tombstone 策略字符串(soft_delete / no_tombstone)
    """
    policy = get_policy(table_name)
    if policy is ReplicationPolicy.CRDB:
        # CRDB 同步表默认 soft_delete
        # (实际是否支持取决于 CRDB 表 schema,_is_crdb_table_supports_soft_delete 探测)
        return TOMBSTONE_POLICY_SOFT_DELETE
    # LOCAL_ONLY / ARCHIVE_ONLY / 未知表 → no_tombstone
    return TOMBSTONE_POLICY_NO_TOMBSTONE


# ════════════════════════════════════════════════════════════════
#  R42 P1-7: backup_policy 与 replication_policy 一致性校验
# ════════════════════════════════════════════════════════════════


def _validate_backup_replication_consistency() -> dict:
    """R42 P1-7: 校验 backup_policy 与 replication_policy 不冲突。

    一致性规则:
        1. NO_EXPORT_PLAINTEXT 的表(敏感数据)不能是 CRDB 同步策略
           (避免明文跨节点复制到 CRDB)
        2. LOCAL_ONLY backup_policy 的表不应是 CRDB 同步策略
           (本地不备份,但跨节点同步 — 数据可能在其他节点备份;允许但不推荐)
        3. MUST_RESTORE 的 CRDB 同步表 — 一致(无冲突)
        4. REBUILDABLE 的 CRDB 同步表 — 一致(无冲突;CRDB 是权威,可重建本地缓存)

    本函数供 CI / 启动时调用,发现冲突时记录警告或抛 ValueError。

    Returns:
        {
            "is_valid": bool,
            "no_export_crdb_conflicts": [表名],   # NO_EXPORT_PLAINTEXT + CRDB(严重)
            "local_only_crdb_conflicts": [表名],  # LOCAL_ONLY backup + CRDB(警告)
            "details": str,
        }

    Raises:
        ValueError: 当存在 NO_EXPORT_PLAINTEXT + CRDB 冲突时(is_valid=False)
    """
    # 懒加载 backup_schema(避免循环 import)
    from services.backup_schema import (
        BackupPolicy,
        BACKUP_POLICY,
        get_backup_policy,
        is_no_export,
        is_local_only_backup,
    )

    no_export_conflicts: list[str] = []
    local_only_crdb_conflicts: list[str] = []

    # 检查所有声明的 backup_policy 表
    for table_name, backup_p in BACKUP_POLICY.items():
        repl_p = get_policy(table_name)
        # 规则 1: NO_EXPORT_PLAINTEXT 不能是 CRDB 同步
        if backup_p is BackupPolicy.NO_EXPORT_PLAINTEXT and repl_p is ReplicationPolicy.CRDB:
            no_export_conflicts.append(table_name)
        # 规则 2: LOCAL_ONLY backup 不应是 CRDB 同步(允许但警告)
        elif backup_p is BackupPolicy.LOCAL_ONLY and repl_p is ReplicationPolicy.CRDB:
            local_only_crdb_conflicts.append(table_name)

    # 也检查所有 CRDB 同步表的 backup_policy
    for table_name in all_crdb_tables():
        bkp = get_backup_policy(table_name)
        if bkp is BackupPolicy.NO_EXPORT_PLAINTEXT:
            if table_name not in no_export_conflicts:
                no_export_conflicts.append(table_name)
        elif bkp is BackupPolicy.LOCAL_ONLY:
            if table_name not in local_only_crdb_conflicts:
                local_only_crdb_conflicts.append(table_name)

    is_valid = not no_export_conflicts

    details_parts: list[str] = []
    if no_export_conflicts:
        details_parts.append(
            f"严重冲突 NO_EXPORT_PLAINTEXT + CRDB: {sorted(no_export_conflicts)}"
        )
    if local_only_crdb_conflicts:
        details_parts.append(
            f"警告 LOCAL_ONLY backup + CRDB: {sorted(local_only_crdb_conflicts)}"
        )
    if not details_parts:
        details_parts.append("backup_policy 与 replication_policy 无冲突")

    result = {
        "is_valid": is_valid,
        "no_export_crdb_conflicts": sorted(no_export_conflicts),
        "local_only_crdb_conflicts": sorted(local_only_crdb_conflicts),
        "details": "; ".join(details_parts),
    }

    # 严重冲突抛 ValueError(阻止启动 / CI 失败)
    if not is_valid:
        raise ValueError(
            f"R42 P1-7: backup_policy 与 replication_policy 冲突 — "
            f"NO_EXPORT_PLAINTEXT 表不能是 CRDB 同步表: "
            f"{sorted(no_export_conflicts)}"
        )

    return result
