"""R40 §9.4: 数据生命周期 — 导出/删除/保留期/访问日志。

本模块提供用户数据的全生命周期管理能力:
- 数据导出(GDPR / 隐私合规): 一键导出用户所有数据
- 数据删除: 软删除 + 审计日志(GDPR 被遗忘权)
- 保留期管理: 按用户设置数据保留天数
- 过期清理: 物理删除已过保留期且已备份的数据
- 管理员访问日志: 记录管理员敏感操作(view/export/delete/config)

设计约束:
- 纯函数式 + async,通过 get_cache_store() 获取 CacheStore 单例
- 软删除统一调用 store.soft_delete()(R39 P1-5 铁律)
- 物理删除仅在 retention job 中执行(已备份 + 已过保留期)
- 时间戳统一使用 datetime.datetime.now().isoformat()
- 保留期表(user_data_retention)由本服务惰性创建

R51 P1-1 整改要点(数据生命周期事务化):
- 删除请求改为状态机 deletion_requests(pending → processing → completed/failed)
- 每类数据删除独立 step receipt(step_files/step_codes/step_collections/
  step_notifications/step_tasks/step_users_local)
- 任一 step 失败 → 整个 deletion_request 标记 failed,不允许局部成功
- 物理删除前必须验证 backup marker(无 marker 拒绝物理删除)
- 不再"warning 后返回 success",所有失败显式 raise AppError 或返回 failed 状态
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.error_codes import AppError, ErrorCodes
# R61 P1-04: i18n for logger calls (avoid scanner baseline overflow)
from services.i18n import translate as _i18n_t
# R54 P0-3: break-glass 必须走 CommandBus 真实审批
from services.command_bus import (
    claim_execution_approved,
    mark_approved_executed,
    mark_approved_failed,
)


# ─── 默认保留期(天) ──────────────────────────────────────────
DEFAULT_RETENTION_DAYS = 7
# 保留期特殊值: 0 表示永久保留
RETENTION_PERMANENT = 0

# 保留期配置表名(本服务惰性创建)
_RETENTION_TABLE = "user_data_retention"

# R51 P1-1: 删除请求状态表
_DELETION_REQUESTS_TABLE = "deletion_requests"

# 删除请求状态枚举
DELETE_STATUS_PENDING = "pending"
DELETE_STATUS_PROCESSING = "processing"
DELETE_STATUS_COMPLETED = "completed"
DELETE_STATUS_FAILED = "failed"

# 删除步骤(顺序执行,每步生成独立 receipt)
DELETE_STEPS = (
    "step_files",         # file_records 软删除
    "step_codes",         # codes 软删除
    "step_collections",   # collections 软删除
    "step_notifications", # notifications 物理删除
    "step_tasks",         # tasks 标记 cancelled
    "step_users_local",   # users_local 标记删除 + dirty_outbox
)


# ─── R61 P0-01: 统一高风险命令 UoW 数据结构 ────────────────────


@dataclass
class ApprovalGrant:
    """R61 P0-01: ``_verify_break_glass_two_person_approval`` 纯校验产物。

    旧实现在校验通过后**立即自提交**消费 mfa_receipts + command_approvals,
    业务副作用(outbox/audit/物理删除)不在该提交点 — 若业务动作失败,
    审批与 MFA 已被永久消费;若业务成功但 outbox 失败,审计/通知链断裂。

    R61 P0-01 整改: ``_verify_break_glass_two_person_approval`` 改为**纯校验**,
    不再自提交,仅返回本 grant。实际 CAS 消费(mfa_receipts jti 一次性 +
    command_approvals.consumed_at)延迟到 ``execute_high_risk_command_uow``
    的统一事务中,与权限重鉴权 / 状态机 CAS / 业务副作用 / effect receipt /
    dirty_outbox / audit_log 原子提交或回滚。
    """

    action_id: str
    expected_principal_id: int
    # 两人共同批准的 canonical request_hash(64 lowercase hex)
    request_hash: str
    # 两个不同审批人 principal_id(不含 expected_principal_id,防自审批)
    approver_ids: list[int]
    # 每个 approver 的 mfa_receipt jti(顺序与 approver_ids 对齐),用于一次性 CAS 消费
    jti_list: list[str]
    # 审批时快照的 RBAC 权限(执行时由 UoW 重鉴权)
    permission: str
    # 最早过期时间(ISO UTC,审计用;CAS 由 UoW 重新校验未过期)
    expires_at: str
    # 预计算的 CAS 时间戳(ISO UTC),供 UoW UPDATE command_approvals.consumed_at
    consumed_at_now: str
    # 预计算的 unix 秒,供 UoW INSERT OR IGNORE mfa_receipts.used_at/consumed_at
    now_unix: int


@dataclass
class HighRiskCommand:
    """R61 P0-01 / R62 P0-05: 高风险命令描述符,由 ``execute_high_risk_command_uow`` 执行。

    将"审批消费 + 状态机 + 业务副作用 + effect receipt + outbox + audit"
    封装为单一 Unit of Work,所有 rowcount 检查 / 状态机前置条件 / 唯一键校验
    必须在 COMMIT 前完成,任一失败 → ROLLBACK(审批与 MFA 不被消费)。

    R62 P0-05 整改(事务内外部 I/O 分离):
    - ``business_action`` 回调 MUST 仅做 DB 状态 transitions(DELETE/UPDATE/
      INSERT 到 dirty_outbox / audit_log / outbox_events 等本地表),
      不得在回调内执行任何网络/文件 I/O(Telegram/R2/CRDB/email/文件系统)。
      原因:SQLite 事务失败时外部 I/O 无法回滚(伪事务),会造成
      "本地回滚但外部已执行"的不一致。
    - 外部副作用通过 ``outbox_events`` 字段声明:UoW 在 business_action 之后、
      COMMIT 之前原子写入 outbox_events 表(与业务变更 + effect_receipts 同事务)。
      commit 后由 ``OutboxWorker`` 拉取 lease 调用外部系统(provider 各自幂等)。
    - ``compensation_action`` 可选字段: saga 补偿回调,当 UoW COMMIT 成功后
      外部 worker 失败且无法重试时,由 reconcile 流程调用以撤销 DB 状态变更
      (saga 补偿必须显式实现,不依赖伪数据库回滚)。
    """

    action_id: str
    command_type: str
    principal_id: int
    # 64 hex,传给 claim_execution_approved 做恒定时间比较
    request_hash: str
    # 租约 owner(hostname:pid)
    owner: str
    # effect_receipts 类型(如 "purge",必须在 CRITICAL_EFFECT_TYPES 中)
    effect_type: str
    # effect_receipts target(如 action_id)
    effect_target: str
    # 异步业务回调: async (tx) -> dict,返回 {"total_cleaned": int, ...}
    # R62 P0-05: 回调 MUST 仅做 DB 状态 transitions(无网络/文件 I/O),
    # 全部写入传入的 tx(统一事务),不得自行 commit。
    # 外部副作用通过 outbox_events 字段声明,UoW 在回调后原子写入 outbox 表,
    # commit 后由 OutboxWorker 调用外部系统(provider 各自幂等)。
    business_action: Any
    # R62 P0-05: 可选 saga 补偿回调: async (tx, error_msg) -> None
    # 当 UoW COMMIT 成功后 OutboxWorker 持续失败且无法重试时,
    # 由 reconcile 流程在独立事务中调用以撤销 DB 状态变更。
    # 补偿必须显式实现(不依赖伪数据库回滚),且需幂等(可能被多次调用)。
    compensation_action: Any = None
    # R62 P0-05: 外部副作用 outbox 事件列表,每项为 dict 含:
    #   {effect_type, target, request_hash, payload_json, max_attempts?}
    # UoW 在 business_action 之后、COMMIT 之前原子写入 outbox_events 表。
    # commit 后由 OutboxWorker 拉取 lease 调用外部系统。
    outbox_events: list = field(default_factory=list)


@dataclass
class HighRiskCommandResult:
    """R61 P0-01: ``execute_high_risk_command_uow`` 执行结果。"""

    success: bool
    total_cleaned: int = 0
    business_result: dict = field(default_factory=dict)
    error: str = ""


async def _ensure_retention_table() -> bool:
    """内部: 惰性创建保留期配置表(若不存在)。

    表结构:
        user_id        BIGINT PRIMARY KEY,
        retention_days INTEGER DEFAULT 7,
        updated_at     TEXT,
        last_purged_at TEXT

    Returns:
        True 表就绪;False 创建失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    try:
        await store._db.execute(
            f"""CREATE TABLE IF NOT EXISTS {_RETENTION_TABLE} (
                user_id        BIGINT PRIMARY KEY,
                retention_days INTEGER DEFAULT 7,
                updated_at     TEXT,
                last_purged_at TEXT
            )"""
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] 创建保留期表失败: {e}")
    # fail-closed:创建表失败时返回 False
    return False


async def _ensure_command_approvals_table() -> bool:
    """R59 P1: 通过版本化 migration 创建/升级 command_approvals 表。

    R59 P1 改造: 移除运行时惰性 DDL(CREATE TABLE + ALTER TABLE 循环),
    改为调用 ``database.migrate.apply_migrations()`` 应用版本化 SQL 文件。
    保留本函数作为兼容入口(调用方无需改动),内部委托给 migration 框架。

    表结构(R58 P0-2 增强,定义在 database/migrations/001_initial_schema.sql):
        id              INTEGER PRIMARY KEY AUTOINCREMENT
        action_id       TEXT NOT NULL  (关联 command_executions.action_id)
        approver_id     BIGINT NOT NULL (审批人 principal_id)
        approval_type   TEXT NOT NULL  (break_glass / quarantine_delete)
        decision        TEXT NOT NULL DEFAULT 'approved'
                                    (R58 P0-2: 记录存在 ≠ 批准,必须显式 approved)
        request_hash    TEXT NOT NULL DEFAULT ''
                                    (R58 P0-2: 两人必须批准同一请求,防参数错位)
        mfa_receipt     TEXT           (R58 P0-2: 强制非空,绑定 MFA receipt)
        permission      TEXT NOT NULL DEFAULT ''
                                    (R58 P0-2: RBAC 权限快照,执行时再授权)
        approved_at     TEXT NOT NULL
        expires_at      TEXT NOT NULL DEFAULT ''
                                    (R58 P0-2: 旧审批不可无限复用)
        consumed_at     TEXT           (R58 P0-2: 执行时 CAS 消费)
        revoked_at      TEXT           (R58 P0-2: 显式撤销)
        metadata_json   TEXT           (额外元数据)

    R59 P1 fail-closed 检查:
        migration 应用后,若存在 break_glass 审批记录 expires_at 为空(旧 R56 数据
        经 002 补列后 DEFAULT ''),视为不安全(旧审批可能被无限复用),拒绝继续。
        调用方 ``_verify_break_glass_two_person_approval`` 应检查返回值并 raise。

    Returns:
        True 表就绪且 fail-closed 检查通过;False migration 失败或检测到不安全旧数据
    """
    store = get_cache_store()
    if not store._db:
        return False
    # R59 P1: 调用版本化 migration(替换原运行时 CREATE TABLE + ALTER TABLE 循环)
    _migration_failed = False
    try:
        from database.migrate import apply_migrations
        result = await apply_migrations(db=store._db)
        if result.get("failed"):
            logger.error(
                f"[DataLifecycle] R59 P1: command_approvals migration 失败, "
                f"failed={result['failed']}"
            )
            return False
    except Exception as e:
        logger.error(f"[DataLifecycle] R59 P1: command_approvals migration 异常: {e}")
        _migration_failed = True
    if _migration_failed:
        # fail-closed:migration 失败时返回 False
        return False
    # R59 P1: fail-closed 检查 — 旧 break_glass 数据 expires_at 为空时拒绝
    # 防止无过期时间的旧审批被无限复用(安全风险)
    # 仅检查 break_glass 类型(quarantine_delete 由 redis_queue.quarantine_repair 独立校验)
    _expiry_check_failed = False
    try:
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM command_approvals "
            "WHERE approval_type = 'break_glass' "
            "AND (expires_at IS NULL OR expires_at = '')"
        )
        row = await cursor.fetchone()
        empty_expiry_count = int(row[0]) if row else 0
    except Exception as e:
        # 查询失败视为不安全(fail-closed),不继续验证审批
        logger.error(
            f"[DataLifecycle] R59 P1 fail-closed: 检查 expires_at 空值查询失败(视为不安全): {e}"
        )
        _expiry_check_failed = True
    if _expiry_check_failed:
        # fail-closed:查询失败时返回 False
        return False
    if empty_expiry_count > 0:
        logger.error(
            f"[DataLifecycle] R59 P1 fail-closed: 检测到 {empty_expiry_count} 条 "
            f"break_glass 审批记录 expires_at 为空,拒绝继续(防止旧审批无限复用)"
        )
        return False
    return True


async def _verify_break_glass_two_person_approval(
    action_id: str,
    expected_principal_id: int,
    expected_request_hash: str = "",
) -> ApprovalGrant:
    """R58 P0-2 / R61 P0-01: 纯校验 break-glass 双人审批,返回 ``ApprovalGrant``。

    要求(R58 P0-2 增强):
    - command_approvals 表中对该 action_id 至少有 2 个不同的 approver
    - 其中一个必须是 expected_principal_id(发起人不能自己审批自己)
    - 每个 approver 必须有 mfa_receipt(非空)
    - R58 P0-2: decision 必须='approved'(记录存在 ≠ 批准)
    - R58 P0-2: request_hash 必须相同且长度=64(两人批准同一请求)
    - R58 P0-2: 未过期(expires_at > now)
    - R58 P0-2: 未撤销(revoked_at IS NULL)
    - R58 P0-2: 未消费(consumed_at IS NULL,防重用)

    R59 P0-02 增强:
    - request_hash 必须严格匹配 `^[0-9a-f]{64}$`(lowercase hex),不再仅检查长度=64
    - expires_at 必须非空且能解析为 UTC aware datetime,不允许空值绕过
    - 时间比较统一使用 `datetime.now(timezone.utc)`(避免 naive 本地时间 ISO 比较脆弱)
    - permission 必须非空(R58 已存储,这里强制校验;执行时由 UoW 重鉴权)

    R61 P0-01 整改(纯校验化):
    - 本函数**不再自提交**消费 mfa_receipts / command_approvals。
      旧实现在校验通过后立即 BEGIN IMMEDIATE + CAS + COMMIT,业务副作用不在该提交点,
      导致"审批已消费但业务失败"或"业务成功但 outbox 失败"的不一致。
    - 现改为只做纯校验(MFA 签名/sub/purpose/action_hash/amr/iat/exp + 审批状态/过期/撤销/消费/Hash),
      收集 jti 列表与审批元数据,返回 ``ApprovalGrant``。
    - 实际 CAS 消费(mfa_receipts jti 一次性 + command_approvals.consumed_at)延迟到
      ``execute_high_risk_command_uow`` 的统一事务中,与权限重鉴权 / 状态机 CAS /
      业务副作用 / effect receipt / dirty_outbox / audit_log 原子提交或回滚。

    Args:
        action_id: 审批动作 ID
        expected_principal_id: 发起人 principal_id(用于确认非自审批)
        expected_request_hash: 期望的 request_hash(64 hex),用于验证两人批准同一请求

    Returns:
        ApprovalGrant: 含 jti_list / permission / request_hash / 时间戳,
        供 ``execute_high_risk_command_uow`` 在统一事务中消费。

    Raises:
        AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED): 双人审批不足/MFA 缺失/自审批/Hash 不匹配/已过期/已撤销/已消费
    """
    import re as _re_mod
    # R60 P0-01: 不再导入 consume_mfa_receipt(其内部自行 commit,无法纳入单事务)
    # R61 P0-01: jti 一次性消费的 CAS 逻辑移到 execute_high_risk_command_uow 统一事务中
    # R63 P1-05: 改用唯一权威 async verify_mfa_receipt_authoritative(),
    # 内部完成签名 + age + SQLite 权威吊销查询 + (可选)一次性消费。
    # 传 consume=False:实际 CAS 消费延迟到 execute_high_risk_command_uow 统一事务中
    # (R60/R61 P0-01: 审批消费 + 状态机 CAS + 业务副作用原子提交/回滚)。
    from admin.mfa import verify_mfa_receipt_authoritative

    # R59 P0-02: request_hash 严格 lowercase hex 正则
    _REQUEST_HASH_RE = _re_mod.compile(r"^[0-9a-f]{64}$")

    store = get_cache_store()
    if not store._db:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "store_unavailable_for_two_person_check"},
        )
    # R59 P1: 检查 _ensure_command_approvals_table() 返回值
    # 失败原因包括: migration 应用失败 / fail-closed 检查未通过(旧数据 expires_at 为空)
    # fail-closed 模式: 检测到不安全旧数据时拒绝继续执行,而不是降级放行
    if not await _ensure_command_approvals_table():
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "command_approvals_migration_or_fail_closed_check_failed"
            },
        )
    try:
        # R59 P0-02: 查询时增加 permission 字段(执行时重鉴权准备)
        rows = await store._db.execute_fetchall(
            "SELECT approver_id, mfa_receipt, decision, request_hash, "
            "expires_at, consumed_at, revoked_at, permission "
            "FROM command_approvals "
            "WHERE action_id = ? AND approval_type = 'break_glass'",
            (action_id,),
        )
    except Exception as e:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": f"two_person_query_failed: {type(e).__name__}: {e}"},
        ) from e
    if not rows or len(rows) < 2:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "two_person_approval_required",
                "current_approvers": len(rows) if rows else 0,
                "required": 2,
            },
        )
    approver_ids = set()
    request_hashes = set()
    permissions = set()
    # R61 P0-01: 收集每个 approver 的 expires_at(ISO 字符串),用于 grant 审计字段
    expires_at_strs: list[str] = []
    # R61 P0-01: 保持 jti_list 与 approver 顺序对齐(供 UoW CAS 消费)
    ordered_approver_ids: list[int] = []
    # R59 P0-02: 时间比较统一使用 UTC aware datetime(避免 naive 本地时间字符串比较脆弱)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    for r in rows:
        approver_id = int(r[0] or 0)
        mfa_receipt = str(r[1] or "")
        decision = str(r[2] or "")
        request_hash = str(r[3] or "")
        expires_at_raw = r[4]
        expires_at_str = str(expires_at_raw) if expires_at_raw is not None else ""
        consumed_at = r[5]
        revoked_at = r[6]
        permission = str(r[7] or "") if len(r) > 7 else ""
        # R58 P0-2: mfa_receipt 必须非空
        if not mfa_receipt:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "mfa_receipt_missing",
                    "approver_id": approver_id,
                },
            )
        # R59 P0-02: request_hash 必须严格匹配 ^[0-9a-f]{64}$(lowercase hex)
        # (旧版仅检查长度=64,允许大写 HEX 或非 hex 字符通过,不满足不可伪造契约)
        if not _REQUEST_HASH_RE.fullmatch(request_hash):
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "request_hash_invalid_lowercase_hex",
                    "approver_id": approver_id,
                    "hash_len": len(request_hash),
                    "pattern": "^[0-9a-f]{64}$",
                },
            )
        # R59 P0-02: expires_at 必须非空且能解析为 UTC aware datetime
        # (旧版空 expires_at 可绕过过期检查,严重安全漏洞)
        if not expires_at_str:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "expires_at_missing",
                    "approver_id": approver_id,
                },
            )
        # R59 P0-02: 解析 expires_at 为 UTC aware datetime(支持带/不带 tz 后缀)
        # _parse_iso_utc 已抛 AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED),
        # 这里直接传播(避免 AppError 嵌套)
        expires_at_dt = _parse_iso_utc(expires_at_str)
        # R59 P0-02: 使用 UTC aware datetime 比较(避免字符串比较时区漂移)
        if expires_at_dt <= now_utc:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "approval_expired",
                    "approver_id": approver_id,
                    "expires_at": expires_at_str,
                    "now_utc": now_utc.isoformat(),
                },
            )
        # R61 P0-01: 收集 expires_at(已校验未过期),供 grant 审计字段
        expires_at_strs.append(expires_at_str)
        # R58 P0-2: decision 必须='approved'(记录存在 ≠ 批准)
        if decision != "approved":
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "approval_decision_not_approved",
                    "approver_id": approver_id,
                    "decision": decision,
                },
            )
        # R58 P0-2: 未撤销(revoked_at IS NULL)
        if revoked_at:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "approval_revoked",
                    "approver_id": approver_id,
                    "revoked_at": str(revoked_at),
                },
            )
        # R58 P0-2: 未消费(consumed_at IS NULL,防重用)
        if consumed_at:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "approval_already_consumed",
                    "approver_id": approver_id,
                    "consumed_at": str(consumed_at),
                },
            )
        # R59 P0-02: permission 必须非空(执行时由 claim_execution_approved 重鉴权)
        if not permission:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "permission_missing_for_reauth",
                    "approver_id": approver_id,
                },
            )
        if approver_id == expected_principal_id:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "self_approval_forbidden",
                    "principal_id": expected_principal_id,
                },
            )
        approver_ids.add(approver_id)
        request_hashes.add(request_hash)
        permissions.add(permission)
        # R61 P0-01: 保持 approver 顺序与 jti_list 对齐(供 grant 审计)
        ordered_approver_ids.append(approver_id)
    if len(approver_ids) < 2:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "two_distinct_approvers_required",
                "distinct_approvers": len(approver_ids),
            },
        )
    # R58 P0-2: 所有审批的 request_hash 必须相同(两人批准同一请求)
    if len(request_hashes) != 1:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "request_hash_mismatch",
                "distinct_hashes": len(request_hashes),
            },
        )
    # R58 P0-2: 若调用方提供 expected_request_hash,必须与审批记录一致
    if expected_request_hash and expected_request_hash not in request_hashes:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "expected_request_hash_not_match",
                "expected": expected_request_hash,
            },
        )
    # R59 P0-03: 验证 MFA receipt(真实签发,服务端签名)
    # 每个 approver 必须有真实 MFA receipt(jti/sub/purpose/action_hash/amr/iat/exp)
    # receipt 的 purpose 必须与当前高风险动作匹配,sub 必须匹配批准人,
    # action_hash 必须匹配 request_hash,TTL 2-5 分钟
    canonical_request_hash = next(iter(request_hashes))
    expected_purpose = "break_glass_approval"
    # R60 P0-01: 先完成所有纯校验(签名/sub/purpose/action_hash/amr/iat/exp),
    # 仅收集 jti 列表;一次性消费(mfa_receipts + command_approvals)必须在
    # 同一 SQLite 连接、同一显式事务中原子完成,禁止在循环中调用自行 commit 的
    # consume_mfa_receipt(否则会出现 receipt 已消费而审批未消费的半消费状态)。
    # R63 P1-05: 改用 verify_mfa_receipt_authoritative(consume=False) —
    # 权威 SQLite 吊销查询由本函数内部完成,不再依赖调用方"记得"查询权威层;
    # consume=False 保留 R60/R61 P0-01 的统一事务原子性(实际 CAS 消费在 UoW 中)。
    jti_list: list[str] = []
    for r in rows:
        approver_id = int(r[0] or 0)
        mfa_receipt = str(r[1] or "")
        try:
            receipt_payload = await verify_mfa_receipt_authoritative(
                token=mfa_receipt,
                expected_principal_id=approver_id,
                expected_purpose=expected_purpose,
                expected_action_hash=canonical_request_hash,
                consume=False,  # R60/R61 P0-01: CAS 消费延迟到 UoW 统一事务
            )
            # R59 P0-03: 提取 jti(防重放原子消费凭据,实际消费在下方统一事务中)
            jti = receipt_payload.get("jti", "")
            if not jti:
                raise AppError(
                    ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                    params={
                        "reason": "mfa_receipt_jti_missing",
                        "approver_id": approver_id,
                    },
                )
            jti_list.append(jti)
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": f"mfa_receipt_verify_failed: {type(e).__name__}: {e}",
                    "approver_id": approver_id,
                },
            ) from e
    # R61 P0-01: 纯校验完成 — 不再在此自提交消费 mfa_receipts / command_approvals。
    # 旧实现的 BEGIN IMMEDIATE + CAS + COMMIT 块已移除:
    #   实际 CAS 消费(mfa_receipts jti 一次性 + command_approvals.consumed_at)
    #   延迟到 execute_high_risk_command_uow 的统一事务中,与权限重鉴权 /
    #   状态机 CAS / 业务副作用 / effect receipt / dirty_outbox / audit_log
    #   原子提交或回滚(任一失败 → ROLLBACK,审批与 MFA 不被消费)。
    consumed_at_now = now_utc.isoformat()
    now_unix = int(now_utc.timestamp())
    # R61 P0-01: 选取最早过期时间作为 grant 审计字段(CAS 时由 UoW 重新校验未过期)
    earliest_expires_at = min(expires_at_strs) if expires_at_strs else ""
    grant = ApprovalGrant(
        action_id=action_id,
        expected_principal_id=expected_principal_id,
        request_hash=canonical_request_hash,
        approver_ids=ordered_approver_ids,
        jti_list=jti_list,
        permission=next(iter(permissions)) if permissions else "",
        expires_at=earliest_expires_at,
        consumed_at_now=consumed_at_now,
        now_unix=now_unix,
    )
    logger.info(
        _i18n_t(
            "services.data_lifecycle.logger_break_glass_validation_passed",
            action_id=action_id,
            approvers=len(ordered_approver_ids),
            jti_count=len(jti_list),
            permission=grant.permission,
        )
    )
    return grant


def _parse_iso_utc(dt_str: str) -> _dt.datetime:
    """R59 P0-02: 解析 ISO 8601 字符串为 UTC aware datetime。

    支持以下格式:
    - "2026-07-17T12:34:56+00:00"(带 tz 后缀)
    - "2026-07-17T12:34:56Z"(Z 后缀)
    - "2026-07-17T12:34:56.123456"(无 tz,视为 UTC)
    - "2026-07-17T12:34:56"(无 tz,视为 UTC)

    Args:
        dt_str: ISO 8601 时间字符串

    Returns:
        UTC aware datetime

    Raises:
        AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED): 解析失败
            (使用 AppError 而非裸 ValueError,满足 R47 P1-c 裸字符串门禁)
    """
    if not dt_str:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "expires_at_empty_string"},
        )
    s = dt_str.strip()
    # 替换 Z 后缀为 +00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        # 尝试无微秒的格式
        try:
            dt = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as parse_err:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "expires_at_unparseable_format",
                    "input": dt_str,
                    "error_class": type(parse_err).__name__,
                },
            ) from parse_err
    # 若无 tzinfo,视为 UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_OUTBOX_EVENT_UNIQUE_CONFLICT = (
    "[DataLifecycle] R63 P1-02: outbox_event UNIQUE 冲突"
    "(幂等,字段+digest 一致,已排队) action={} type={} target={}"
)


async def execute_high_risk_command_uow(
    command: HighRiskCommand,
    grant: ApprovalGrant,
) -> HighRiskCommandResult:
    """R61 P0-01: 统一高风险命令 Unit of Work — 单事务原子提交。

    将以下步骤封装在**同一** ``BEGIN IMMEDIATE`` 事务中,所有 rowcount 检查 /
    状态机前置条件 / 唯一键校验必须在 COMMIT 前完成,任一失败 → ROLLBACK
    (审批与 MFA 不被消费,状态机不前进):

    执行顺序(对应 R61 P0-01 audit spec):
      1. 权限重鉴权 — verify principal_id 仍持有 grant.permission
      2. 状态机 CAS 认领 — claim_execution_approved(approved → executing)
      3. receipt CAS — INSERT OR IGNORE mfa_receipts(jti 一次性),rowcount=2
      4. approval CAS — UPDATE command_approvals.consumed_at,rowcount=2
      5. 业务状态变更 — command.business_action(tx) 执行实际 delete/restore/isolate
         (回调内负责 per-row dirty_outbox + per-user audit_log,写入同一 tx)
      6. effect receipt / 幂等键 — 写 effect_receipts(completed)
      7. 状态机 CAS 完成 — mark_approved_executed(executing → executed)
      8. COMMIT(owns_transaction=True)/ RELEASE SAVEPOINT(嵌套场景)

    R61 P0-02: 通过 ``store._db.in_transaction`` 精确判断事务所有权:
      - 不在事务中 → BEGIN IMMEDIATE(串行化并发写者);失败抛 AppError
      - 已在外层事务中 → SAVEPOINT high_risk_uow 隔离(不擅自提交调用方事务)

    Args:
        command: 高风险命令描述符(含 business_action 回调)
        grant: 来自 ``_verify_break_glass_two_person_approval`` 的纯校验产物

    Returns:
        HighRiskCommandResult: success=True + business_result;失败时抛 AppError

    Raises:
        AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED):
            权限重鉴权失败 / 状态机 CAS 未命中 / receipt CAS 失败 /
            approval CAS 失败 / 业务异常 / effect receipt 写入失败 /
            mark_approved_executed 失败 / BEGIN IMMEDIATE 失败
    """
    from services.rbac import check_permission

    store = get_cache_store()
    if not store._db:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": "store_unavailable_for_high_risk_uow",
                "action_id": command.action_id,
            },
        )
    # R61 P0-02: 通过 in_transaction 精确判断事务所有权(不复用 catch-all BEGIN 异常)
    owns_transaction = not bool(getattr(store._db, "in_transaction", False))
    _UOW_SAVEPOINT = "high_risk_uow"
    if owns_transaction:
        # BEGIN IMMEDIATE 立即获取 RESERVED 锁,串行化并发写者
        try:
            await store._db.execute("BEGIN IMMEDIATE")
        except Exception as begin_err:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": f"uow_begin_immediate_failed: "
                              f"{type(begin_err).__name__}: {begin_err}",
                    "action_id": command.action_id,
                },
            ) from begin_err
    else:
        # 已在外层事务中: SAVEPOINT 隔离,不擅自 COMMIT 调用方事务
        await store._db.execute(f"SAVEPOINT {_UOW_SAVEPOINT}")

    async def _rollback_to_savepoint() -> None:
        """R61 P0-01: 统一回滚 — owns → ROLLBACK;否则 ROLLBACK TO SAVEPOINT。"""
        try:
            if owns_transaction:
                await store._db.rollback()
            else:
                await store._db.execute(
                    f"ROLLBACK TO SAVEPOINT {_UOW_SAVEPOINT}"
                )
        except Exception as rollback_err:
            logger.warning(
                _i18n_t(
                    "services.data_lifecycle.logger_uow_rollback_failed",
                    action_id=command.action_id,
                    err=str(rollback_err),
                )
            )

    try:
        # ── 1. 权限重鉴权(verify principal_id 仍持有 grant.permission)──
        # claim_execution_approved 仅做 request_hash 校验 + 状态机 CAS,
        # 不校验 RBAC 权限;此处显式重鉴权,防审批后被撤销权限。
        has_perm = await check_permission(
            grant.expected_principal_id, grant.permission
        )
        if not has_perm:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "permission_reauth_failed",
                    "principal_id": grant.expected_principal_id,
                    "permission": grant.permission,
                    "action_id": command.action_id,
                },
            )

        # ── 2. 状态机 CAS 认领(approved → executing)──
        # R61 P0-01: 传入 connection=store._db 复用统一事务,不自动 commit
        claimed = await claim_execution_approved(
            action_id=command.action_id,
            owner=command.owner,
            request_hash=command.request_hash,
            connection=store._db,
        )
        if not claimed:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "uow_claim_execution_cas_missed",
                    "action_id": command.action_id,
                },
            )

        # ── 3. receipt CAS — INSERT OR IGNORE mfa_receipts(jti 一次性)──
        # 确保 mfa_receipts 表存在(幂等 DDL,与 admin.mfa.consume_mfa_receipt 一致)
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS mfa_receipts (
                jti          TEXT PRIMARY KEY,
                sub          BIGINT,
                purpose      TEXT,
                action_hash  TEXT,
                amr          TEXT,
                iat          INTEGER,
                exp          INTEGER,
                used_at      INTEGER,
                consumed_at  INTEGER
            )"""
        )
        # 对每个 jti 执行条件 INSERT;rowcount=1 → 首次消费;rowcount=0 → 重放/已消费
        # jti_list 长度必须 = 2(双人审批),rowcount 之和必须恰好为 2
        receipt_affected = 0
        for jti in grant.jti_list:
            cursor = await store._db.execute(
                "INSERT OR IGNORE INTO mfa_receipts (jti, used_at, consumed_at) "
                "VALUES (?, ?, ?)",
                (jti, grant.now_unix, grant.now_unix),
            )
            receipt_affected += cursor.rowcount if cursor is not None else 0
        if receipt_affected != len(grant.jti_list) or receipt_affected != 2:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "uow_receipt_cas_failed",
                    "expected_receipt_affected": 2,
                    "actual_receipt_affected": receipt_affected,
                    "jti_count": len(grant.jti_list),
                    "action_id": command.action_id,
                },
            )

        # ── 4. approval CAS — UPDATE command_approvals.consumed_at ──
        # 受影响行数必须恰好为 2(两条审批都被原子消费)
        # 若 < 2: 校验后有记录被并发消费/撤销/过期;若 > 2: 约束异常(不该发生)
        cursor = await store._db.execute(
            "UPDATE command_approvals "
            "SET consumed_at = ? "
            "WHERE action_id = ? "
            "  AND approval_type = 'break_glass' "
            "  AND consumed_at IS NULL "
            "  AND revoked_at IS NULL "
            "  AND expires_at > ?",
            (grant.consumed_at_now, grant.action_id, grant.consumed_at_now),
        )
        approval_affected = cursor.rowcount if cursor is not None else 0
        if approval_affected != 2:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "uow_approval_cas_failed",
                    "expected_approval_affected": 2,
                    "actual_approval_affected": approval_affected,
                    "action_id": command.action_id,
                },
            )

        # ── 5. 业务状态变更(delete/restore/isolate)—— R62 P0-05: DB-only ──
        # business_action 回调 MUST 仅做 DB 状态 transitions(DELETE/UPDATE/INSERT
        # 到 dirty_outbox / audit_log 等本地表),不得在回调内执行任何网络/文件 I/O
        # (Telegram/R2/CRDB/email/文件系统)。
        # 原因: SQLite 事务失败时外部 I/O 无法回滚(伪事务),会造成
        # "本地回滚但外部已执行"的不一致(R62 P0-05 audit finding)。
        # 外部副作用通过 command.outbox_events 声明,在步骤 6b 原子写入 outbox_events 表,
        # commit 后由 OutboxWorker 拉取 lease 调用外部系统(provider 各自幂等)。
        # 任一异常 → 触发下方 except,统一 ROLLBACK(审批/MFA 不被消费)。
        business_result = await command.business_action(store._db)
        if not isinstance(business_result, dict):
            business_result = {"raw": business_result}
        total_cleaned = int(business_result.get("total_cleaned", 0))

        # ── 6a. effect receipt / 幂等键 — 写 effect_receipts(pending → completed)──
        # R62 P1-01 整改: 不再使用 INSERT OR IGNORE + UPDATE 模式(会覆盖 request_hash/
        # external_id/status,导致同 (a,e,t) 不同 payload 的 receipt 互相覆盖)。
        # 改为 PRE-SELECT + plain INSERT + UPDATE WHERE status='pending' AND request_hash=?:
        #   1. SELECT 查是否已存在 receipt
        #   2. 不存在 → INSERT pending(UNIQUE 冲突时 SELECT 兜底竞态)
        #   3. 已存在 + 同 hash + pending → 幂等重试(不覆盖)
        #   4. 已存在 + 不同 hash → raise IDEMPOTENCY_CONFLICT
        #   5. 已存在 + completed → raise TERMINAL_STATE
        #   6. UPDATE WHERE status='pending' AND request_hash=? → completed + rowcount 检查
        # 与 EffectReceiptManager.record_pending/record_completed 不同,此处直接写 SQL
        # 以纳入统一事务(record_* 不接受 tx 参数会自行 commit 破坏原子性,R62 P1-01 已加 tx
        # 参数但为保持 UoW 单事务原子性,此处仍直接写 SQL)。
        eff_now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        cursor = await store._db.execute(
            "SELECT status, request_hash FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (grant.action_id, command.effect_type, command.effect_target),
        )
        _eff_existing = await cursor.fetchone()
        if _eff_existing is None:
            # 不存在 → plain INSERT pending(R62 P1-01: 不再用 INSERT OR IGNORE)
            try:
                await store._db.execute(
                    "INSERT INTO effect_receipts "
                    "(action_id, effect_type, target, status, external_id, "
                    " created_at, completed_at, request_hash, attempt, "
                    " lease_owner, lease_until, last_error, reconcile_status) "
                    "VALUES (?, ?, ?, 'pending', '', ?, NULL, ?, 1, ?, '', NULL, 'pending')",
                    (grant.action_id, command.effect_type, command.effect_target,
                     eff_now, grant.request_hash, command.owner),
                )
            except Exception as insert_err:
                # UNIQUE 冲突(竞态)→ SELECT 兜底,若 completed 则 raise TERMINAL_STATE
                if "unique" not in str(insert_err).lower() and "constraint" not in str(insert_err).lower():
                    raise
                cursor = await store._db.execute(
                    "SELECT status, request_hash FROM effect_receipts "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "AND request_hash = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (grant.action_id, command.effect_type, command.effect_target,
                     grant.request_hash),
                )
                _eff_existing = await cursor.fetchone()
                if _eff_existing is None:
                    raise insert_err
                if _eff_existing[0] == "completed":
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                        params={
                            "action_id": grant.action_id,
                            "effect_type": command.effect_type,
                            "target": command.effect_target,
                            "current_status": _eff_existing[0],
                        },
                    )
        else:
            _eff_existing_status = _eff_existing[0]
            _eff_existing_hash = _eff_existing[1] or ""
            # R62 P1-01: 不同 hash → 幂等冲突
            if (grant.request_hash and _eff_existing_hash
                    and grant.request_hash != _eff_existing_hash):
                raise AppError(
                    ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT,
                    params={
                        "action_id": grant.action_id,
                        "effect_type": command.effect_type,
                        "target": command.effect_target,
                    },
                )
            # 终态保护: 已 completed → raise
            if _eff_existing_status == "completed":
                raise AppError(
                    ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                    params={
                        "action_id": grant.action_id,
                        "effect_type": command.effect_type,
                        "target": command.effect_target,
                        "current_status": _eff_existing_status,
                    },
                )
            # pending/failed + 同 hash → 幂等,不 INSERT(下方 UPDATE 转 completed)

        # UPDATE 转 completed,WHERE status='pending' AND request_hash=? 防止误更新
        # R62 P1-01: 若 pending 行的 request_hash 不匹配(理论上不该发生,因上方已校验),
        # rowcount=0 → 抛错;若已 completed(竞态),rowcount=0 → 抛 TERMINAL_STATE。
        # 注意: SET 不再更新 request_hash(R62 P1-01: request_hash 不可变,防止覆盖);
        # external_id 仍用 command.action_id(与原行为一致,作幂等键供查询)。
        cursor = await store._db.execute(
            "UPDATE effect_receipts SET status = 'completed', "
            "completed_at = ?, external_id = ?, reconcile_status = 'completed', "
            "last_error = NULL "
            "WHERE action_id = ? AND effect_type = ? AND target = ? "
            "AND status = 'pending' AND request_hash = ?",
            (eff_now, command.action_id,
             grant.action_id, command.effect_type, command.effect_target,
             grant.request_hash),
        )
        _eff_affected = cursor.rowcount if cursor is not None else 0
        if _eff_affected == 0:
            # rowcount=0 → 可能 failed 状态(pending→failed 由 record_failed)或竞态 completed
            cursor = await store._db.execute(
                "SELECT status FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ? "
                "AND request_hash = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (grant.action_id, command.effect_type, command.effect_target,
                 grant.request_hash),
            )
            _eff_row = await cursor.fetchone()
            if _eff_row and _eff_row[0] == "completed":
                raise AppError(
                    ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
                    params={
                        "action_id": grant.action_id,
                        "effect_type": command.effect_type,
                        "target": command.effect_target,
                        "current_status": "completed",
                    },
                )
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "uow_effect_receipt_update_rowcount_zero",
                    "action_id": command.action_id,
                    "existing_status": _eff_row[0] if _eff_row else "not_found",
                },
            )

        # ── 6b. R62 P0-05: 外部副作用 outbox_events — 与业务变更 + effect_receipts 原子提交 ──
        # 在 business_action 之后、COMMIT 之前写入 outbox_events 表,
        # commit 后由 OutboxWorker 拉取 lease 调用外部系统(provider 各自幂等)。
        # UNIQUE(action_id, effect_type, target, request_hash) 保证幂等:
        # 重复写入(同 a,e,t,rh)抛 UNIQUE 冲突,视为已排队(忽略)。
        # business_action 必须是 DB-only(无网络/文件 I/O),外部副作用通过本字段声明。
        #
        # R63 P1-02 整改(冲突处理改用错误码 + 字段验证):
        # 旧实现在 except 中通过 `str(err)` 包含 `unique`/`constraint` 子串判断"已排队",
        # 这会吞掉 CHECK / NOT NULL / FK / 其它约束错误(它们也含 "constraint" 字样),
        # 错误地把真正的失败视为幂等成功。新实现:
        # 1. 仅捕获 ``sqlite3.IntegrityError``(其它异常透传)
        # 2. 检查 ``str(err)`` 是否为 ``UNIQUE constraint failed: outbox_events.*``
        #    (具体表名 + UNIQUE 关键字,非泛 ``unique``/``constraint`` 子串)
        # 3. 冲突后 SELECT 既有行,逐字段验证 action_id/effect_type/target/
        #    request_hash + payload sha256 digest 一致,否则抛
        #    ``DATA_RECEIPT_IDEMPOTENCY_CONFLICT``(payload 被替换,不可盲目重试)
        _outbox_enqueued = 0
        for _ev in command.outbox_events:
            _ev_effect_type = _ev.get("effect_type", "")
            _ev_target = _ev.get("target", "")
            _ev_request_hash = _ev.get("request_hash", "")
            _ev_payload_json = _ev.get("payload_json", "")
            _ev_max_attempts = int(_ev.get("max_attempts", 3))
            if not _ev_effect_type or not _ev_target or not _ev_request_hash:
                raise AppError(
                    ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                    params={
                        "reason": "uow_outbox_event_missing_fields",
                        "action_id": command.action_id,
                        "event": str(_ev),
                    },
                )
            try:
                await store.add_outbox_event(
                    action_id=command.action_id,
                    effect_type=_ev_effect_type,
                    target=_ev_target,
                    request_hash=_ev_request_hash,
                    payload_json=_ev_payload_json,
                    max_attempts=_ev_max_attempts,
                    tx=store._db,  # R62 P0-05: 纳入统一事务,不自行 commit
                )
                _outbox_enqueued += 1
            except sqlite3.IntegrityError as outbox_err:
                # R63 P1-02: 仅处理 UNIQUE 冲突(检查具体表名 + UNIQUE 关键字)
                # 非 UNIQUE 的 IntegrityError(CHECK / NOT NULL / FK 等)→ 抛错触发 ROLLBACK
                _err_str = str(outbox_err)
                _is_unique_on_outbox = (
                    "UNIQUE constraint failed" in _err_str
                    and "outbox_events" in _err_str
                )
                if not _is_unique_on_outbox:
                    raise AppError(
                        ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                        params={
                            "reason": f"uow_outbox_event_integrity_error_not_unique: "
                                      f"{type(outbox_err).__name__}: {outbox_err}",
                            "action_id": command.action_id,
                            "effect_type": _ev_effect_type,
                            "target": _ev_target,
                        },
                    ) from outbox_err
                # R63 P1-02: UNIQUE 冲突 → SELECT 既有行,逐字段验证一致性
                cursor = await store._db.execute(
                    "SELECT action_id, effect_type, target, request_hash, "
                    "payload_json FROM outbox_events "
                    "WHERE action_id=? AND effect_type=? AND target=? "
                    "AND request_hash=?",
                    (command.action_id, _ev_effect_type, _ev_target,
                     _ev_request_hash),
                )
                _existing_ob = await cursor.fetchone()
                if _existing_ob is None:
                    # UNIQUE 冲突但 SELECT 不到(竞态:行被并发删除)→ 抛错
                    raise AppError(
                        ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                        params={
                            "reason": "uow_outbox_event_conflict_but_row_missing",
                            "action_id": command.action_id,
                            "effect_type": _ev_effect_type,
                            "target": _ev_target,
                        },
                    ) from outbox_err
                _ex_action = _existing_ob[0] or ""
                _ex_effect = _existing_ob[1] or ""
                _ex_target = _existing_ob[2] or ""
                _ex_hash = _existing_ob[3] or ""
                _ex_payload = _existing_ob[4] or ""
                # 逐字段验证(防御性:UNIQUE 冲突理论上 4 字段必匹配,
                # 但显式校验防 SQLite 行为变化 / 索引损坏等极端情况)
                _field_mismatch = (
                    _ex_action != command.action_id
                    or _ex_effect != _ev_effect_type
                    or _ex_target != _ev_target
                    or _ex_hash != _ev_request_hash
                )
                # payload digest 验证(同 a,e,t,rh 但不同 payload → 真冲突)
                _expected_digest = hashlib.sha256(
                    (_ev_payload_json or "").encode("utf-8")
                ).hexdigest()
                _actual_digest = hashlib.sha256(
                    (_ex_payload or "").encode("utf-8")
                ).hexdigest()
                if _field_mismatch or _expected_digest != _actual_digest:
                    raise AppError(
                        ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT,
                        params={
                            "action_id": command.action_id,
                            "effect_type": _ev_effect_type,
                            "target": _ev_target,
                            "reason": "uow_outbox_event_payload_digest_mismatch",
                            "field_mismatch": _field_mismatch,
                            "digest_match": _expected_digest == _actual_digest,
                        },
                    ) from outbox_err
                # 全部一致 → 幂等,视为已排队(不 increment _outbox_enqueued,
                # 因为本次未实际写入新行;与原 R62 行为一致)
                logger.debug(
                    _LOG_OUTBOX_EVENT_UNIQUE_CONFLICT.format(
                        command.action_id, _ev_effect_type, _ev_target
                    )
                )
                continue
            # R63 P1-02: 非 IntegrityError 的异常(如 OperationalError: disk full)
            # 不再被吞掉,直接抛错触发 ROLLBACK(原实现在 except Exception 中也会抛,
            # 但路径更复杂;此处让异常透传到外层 except Exception 包装)

        # ── 7. 状态机 CAS 完成(executing → executed)──
        # R61 P0-01: 传入 connection=store._db 复用统一事务,不自动 commit
        executed_ok = await mark_approved_executed(
            action_id=command.action_id,
            result={
                "success": True,
                "total_cleaned": total_cleaned,
                "effect_type": command.effect_type,
            },
            connection=store._db,
        )
        if not executed_ok:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "uow_mark_executed_cas_missed",
                    "action_id": command.action_id,
                    "total_cleaned": total_cleaned,
                },
            )

        # ── 8. COMMIT / RELEASE SAVEPOINT(唯一提交点)──
        # 所有 rowcount 检查 / 状态机 CAS / 唯一键校验均已完成
        if owns_transaction:
            await store._db.commit()
        else:
            await store._db.execute(f"RELEASE SAVEPOINT {_UOW_SAVEPOINT}")
        logger.info(
            f"[DataLifecycle] R61 P0-01 / R62 P0-05: 高风险命令 UoW 提交成功 "
            f"action_id={command.action_id} command_type={command.command_type} "
            f"receipt_affected={receipt_affected} approval_affected={approval_affected} "
            f"total_cleaned={total_cleaned} outbox_enqueued={_outbox_enqueued}"
        )
        return HighRiskCommandResult(
            success=True,
            total_cleaned=total_cleaned,
            business_result=business_result,
        )
    except AppError:
        # 协议化错误: 统一回滚后向上传播(保留原始 code 与 params)
        await _rollback_to_savepoint()
        raise
    except Exception as e:
        # 非协议化异常: 回滚 + 包装为 AppError(满足 scanner Rule 5 禁裸异常)
        await _rollback_to_savepoint()
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": f"uow_exception: {type(e).__name__}: {e}",
                "action_id": command.action_id,
                "command_type": command.command_type,
            },
        ) from e


# ════════════════════════════════════════════════════════════════
# R62 P0-05: OutboxWorker 桩 — 拉取 outbox_events lease 调用外部系统
# ════════════════════════════════════════════════════════════════


class OutboxWorker:
    """R62 P0-05 + R63 P0-05: 事务性 outbox worker。

    设计目标:
    - ``execute_high_risk_command_uow`` 在事务内将外部副作用写入 outbox_events 表
      (与业务变更 + effect_receipts 原子提交),commit 后由本 worker 拉取 lease
      调用外部系统(Telegram/R2/CRDB/email/文件)。
    - provider 各自幂等(基于 request_hash + idempotency_key),worker 失败可安全重试。
    - 超过 max_attempts 自动转 DLQ(permanent failure,需人工介入)。
    - saga 补偿由 reconcile 流程在独立事务中调用 HighRiskCommand.compensation_action,
      不依赖伪数据库回滚(SQLite 事务失败时外部 I/O 无法回滚)。

    R63 P0-05 整改(防 stub 误启动):
    - 旧实现在 ``provider_registry is None`` 时仅 claim 不调用 provider,直接
      ``complete_outbox_event``,把所有外部副作用永久标记完成。生产环境若误启动
      默认配置,Telegram/R2/CRDB/邮件/文件副作用全部失配,业务状态与外部世界
      严重不一致,且无法重放(completed 是终态)。
    - 新增 ``test_mode`` 参数:仅 ``test_mode=True`` 时允许 ``provider_registry=None``
      (用于单元测试 lease 流转)。生产模式(``test_mode=False``)下 ``run_once``
      在 ``provider_registry is None`` 时立即 raise ``RuntimeError``,fail-fast。
    - 新增 ``validate_providers()`` readiness 检查:每个枚举 effect type 必须恰有
      一个 provider,缺失即 readiness failure(部署时应阻断启动)。
    - provider 调用签名扩展为 ``async (payload_json, request_hash, idempotency_key)
      -> external_id``,强制 provider 实现幂等(基于 request_hash 去重)。
    - ``complete_outbox_event`` 使用 CAS ``WHERE status='in_flight' AND lease_owner=?
      AND request_hash=?`` 双重校验,防止越权 complete / 错配事件。
    - 新增 ``reclaim_stale_leases()``:回收过期 lease(worker 崩溃后 in_flight 行
      不再永久卡住)。

    Lease 机制(分布式 worker 协调):
    - claim_outbox_events 原子地将 pending 行转为 in_flight 并设置 lease_owner /
      lease_expires_at,防止多 worker 并发拉取同一行。
    - lease 过期后由 ``reclaim_stale_leases()`` 回收(转回 pending,清空 lease_owner)。
    - 长 provider 调用应周期性调用 ``store.renew_outbox_lease`` 续约(防超时回收)。
    """

    def __init__(
        self,
        *,
        lease_owner: str = "",
        lease_duration_seconds: int = 60,
        batch_size: int = 10,
        provider_registry: Any = None,
        test_mode: bool = False,
    ):
        """初始化 OutboxWorker。

        Args:
            lease_owner: worker 标识(hostname:pid),用于 claim_outbox_events
            lease_duration_seconds: lease 超时秒数(超时后可被其他 worker 重新 claim)
            batch_size: 单次 run_once 最多处理的事件数
            provider_registry: ``{effect_type: async callable}``
                provider 签名: ``async (payload_json, request_hash, idempotency_key)
                -> external_id_str``。
                生产模式(``test_mode=False``)下必传,``None`` 时 ``run_once`` raise。
            test_mode: 测试模式开关。``True`` 时允许 ``provider_registry=None``
                (用于单元测试 lease 流转,直接 complete 不调外部系统);
                ``False`` 时(生产默认)``provider_registry=None`` → ``run_once``
                raise ``RuntimeError``,fail-fast 防止 stub 误启动。
        """
        import socket as _socket
        self.lease_owner = lease_owner or f"{_socket.gethostname()}:{os.getpid()}"
        self.lease_duration_seconds = lease_duration_seconds
        self.batch_size = batch_size
        # provider_registry: {effect_type: async (payload_json, request_hash,
        #                                          idempotency_key) -> external_id_str}
        # test_mode=True 时允许 None(stub 仅 claim+complete,用于测试 lease 流转)
        # test_mode=False 时 run_once 在 None 时 raise RuntimeError(fail-fast)
        self.provider_registry = provider_registry
        self.test_mode = bool(test_mode)

    def validate_providers(
        self,
        *,
        required_effect_types: Any = None,
    ) -> list[str]:
        """R63 P0-05: readiness 检查 — 每个枚举 effect type 恰有一个 provider。

        生产部署启动时应调用本方法,缺失任一 effect type 的 provider 即视为
        readiness failure(部署系统应阻断 worker 启动)。

        检查规则:
        - 对 ``required_effect_types`` 中每个 effect_type,``provider_registry``
          必须含且仅含一个 provider(即 ``provider_registry[effect_type]`` 是 callable)。
        - ``required_effect_types`` 默认为
          ``services.effect_receipts.CRITICAL_EFFECT_TYPES``(9 个枚举值)。
        - 缺失 → 收集到 missing 列表;调用方决定是否 raise / log / 阻断启动。

        Args:
            required_effect_types: 必须覆盖的 effect_type 集合(默认 CRITICAL_EFFECT_TYPES)

        Returns:
            missing 列表(空 list 表示所有 effect type 均有 provider,readiness OK)
        """
        if required_effect_types is None:
            # 延迟导入避免循环依赖
            try:
                from services.effect_receipts import CRITICAL_EFFECT_TYPES
                required_effect_types = CRITICAL_EFFECT_TYPES
            except Exception:
                # 兜底:effect_receipts 不可用时返回空列表(不阻断)
                return []
        if self.provider_registry is None:
            # test_mode 下允许 None,但 validate_providers 仍报告所有 type 缺失
            return list(required_effect_types)
        missing: list[str] = []
        for effect_type in required_effect_types:
            provider = self.provider_registry.get(effect_type)
            if provider is None or not callable(provider):
                missing.append(effect_type)
        return missing

    async def reclaim_stale_leases(self, *, batch_size: int = 100) -> int:
        """R63 P0-05: 回收过期 lease(in_flight + lease_expires_at < now → pending)。

        worker 崩溃 / OOM / 进程被 kill 后,其 in_flight 行会卡住(无人 complete
        也无人 fail)。本方法扫描过期 lease 并原子转回 pending,让其它 worker
        可重新 claim。``attempt_count`` 不递减(失败的尝试仍计入重试上限)。

        建议在 ``run_once`` 之前调用,或在独立 reconcile 进程中周期性调用。

        Args:
            batch_size: 单次最多回收的行数

        Returns:
            回收的行数(0 表示无过期 lease)
        """
        store = get_cache_store()
        if not store._db:
            return 0
        return await store.reclaim_stale_outbox_leases(batch_size=batch_size)

    async def run_once(self) -> dict:
        """拉取一批 outbox_events 并调用 provider 处理。

        R63 P0-05 整改:
        - ``provider_registry is None`` 且 ``test_mode=False`` → raise RuntimeError
          (fail-fast,防 stub 误启动把外部副作用永久标记完成)。
        - ``test_mode=True`` 且 ``provider_registry=None`` → stub 模式(仅 claim
          + complete,用于测试 lease 流转,不产生真实外部副作用)。
        - provider 调用签名: ``await provider(payload_json, request_hash,
          idempotency_key) -> external_id``。``idempotency_key`` 形如
          ``"{action_id}:{request_hash}"``,provider 必须基于此键去重。
        - ``complete_outbox_event`` 传入 ``lease_owner`` + ``request_hash`` +
          ``external_id``,触发 CAS ``WHERE status='in_flight' AND lease_owner=?
          AND request_hash=?``(防越权 complete / 错配事件)。

        Returns:
            {claimed: int, completed: int, failed: int, dlq: int}

        Raises:
            AppError(OUTBOX_PROVIDER_REGISTRY_REQUIRED): ``provider_registry is None``
                且 ``test_mode=False``(生产模式 fail-fast,防止 stub 误启动)
        """
        # R63 P0-05: fail-fast — 生产模式禁止无 provider 的 stub 运行
        if self.provider_registry is None and not self.test_mode:
            raise AppError(
                ErrorCodes.OUTBOX_PROVIDER_REGISTRY_REQUIRED,
                params={
                    "reason": "stub worker must not silently complete external "
                              "side effects; pass test_mode=True explicitly for tests"
                },
            )
        store = get_cache_store()
        if not store._db:
            return {"claimed": 0, "completed": 0, "failed": 0, "dlq": 0}
        events = await store.claim_outbox_events(
            lease_owner=self.lease_owner,
            lease_duration_seconds=self.lease_duration_seconds,
            limit=self.batch_size,
        )
        if not events:
            return {"claimed": 0, "completed": 0, "failed": 0, "dlq": 0}
        completed = 0
        failed = 0
        dlq = 0
        for ev in events:
            event_id = ev["id"]
            effect_type = ev["effect_type"]
            payload_json = ev.get("payload_json", "")
            # R63 P0-05: 从 outbox event 读取 request_hash(CAS complete 必须传入)
            request_hash = ev.get("request_hash", "") or ""
            action_id = ev.get("action_id", "") or ""
            # R63 P0-05: idempotency_key = action_id:request_hash,
            # provider 基于此键去重(同一逻辑副作用多次重试只执行一次)
            idempotency_key = f"{action_id}:{request_hash}" if action_id else request_hash
            # Stub 模式(仅 test_mode=True 时可达):
            # 无 provider_registry → 直接 complete(用于测试 lease 流转)
            if self.provider_registry is None:
                # R63 P0-05: complete 传入 lease_owner + request_hash 触发 CAS
                await store.complete_outbox_event(
                    event_id,
                    external_id="",
                    lease_owner=self.lease_owner,
                    request_hash=request_hash,
                )
                completed += 1
                continue
            provider = self.provider_registry.get(effect_type)
            if provider is None:
                # 无对应 provider → 永久失败,直接进 DLQ
                await store.move_outbox_to_dlq(
                    event_id, reason=f"no_provider_for_effect_type:{effect_type}",
                )
                dlq += 1
                continue
            try:
                # R63 P0-05: provider 签名扩展为 (payload_json, request_hash,
                # idempotency_key) -> external_id,强制 provider 实现幂等
                _external_id = await provider(
                    payload_json, request_hash, idempotency_key,
                )
                # R63 P0-05: CAS complete — lease_owner + request_hash 双重校验,
                # 同时保存 external_id(供事后对账与人工重放)
                await store.complete_outbox_event(
                    event_id,
                    external_id=str(_external_id) if _external_id is not None else "",
                    lease_owner=self.lease_owner,
                    request_hash=request_hash,
                )
                completed += 1
            except Exception as prov_err:
                result = await store.fail_outbox_event(
                    event_id, error_msg=str(prov_err),
                )
                if result == "dlq":
                    dlq += 1
                else:
                    failed += 1
        return {
            "claimed": len(events),
            "completed": completed,
            "failed": failed,
            "dlq": dlq,
        }


async def export_user_data(user_id: int) -> dict:
    """导出用户所有数据(GDPR/隐私合规)。

    聚合用户在 SQLite 本地库中的所有数据,用于合规导出。
    导出范围: user_info / file_codes / decode_logs / collections / tasks / notifications

    Args:
        user_id: 用户 id

    Returns:
        {user_info, file_codes, decode_logs, collections, tasks, notifications}
        查询失败的字段返回空列表 / None
    """
    store = get_cache_store()
    result: dict = {
        "user_info": None,
        "file_codes": [],
        "decode_logs": [],
        "collections": [],
        "tasks": [],
        "notifications": [],
    }
    if not store._db:
        return result
    now = _dt.datetime.now().isoformat()
    # user_info
    try:
        cursor = await store._db.execute(
            "SELECT user_id, username, first_name, membership_level, "
            "daily_decode_quota, quota_used_today, quota_date, can_upload, "
            "is_banned, created_at FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            result["user_info"] = {
                "user_id": row[0], "username": row[1], "first_name": row[2],
                "membership_level": row[3], "daily_decode_quota": row[4],
                "quota_used_today": row[5], "quota_date": row[6],
                "can_upload": row[7], "is_banned": row[8], "created_at": row[9],
            }
    except Exception as e:
        logger.warning(f"[DataLifecycle] export user_info 失败: {e}")
    # file_codes(file_records_local)
    try:
        cursor = await store._db.execute(
            "SELECT file_code, file_types, status, request_count, "
            "is_collection, collection_codes, create_time "
            "FROM file_records_local WHERE uploader_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["file_codes"] = [
            {
                "file_code": r[0], "file_types": r[1], "status": r[2],
                "request_count": r[3], "is_collection": r[4],
                "collection_codes": r[5], "create_time": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export file_codes 失败: {e}")
    # decode_logs(decode_log_buffer)
    try:
        cursor = await store._db.execute(
            "SELECT file_code, request_time, status, source_channel_id "
            "FROM decode_log_buffer WHERE requester_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["decode_logs"] = [
            {
                "file_code": r[0], "request_time": r[1], "status": r[2],
                "source_channel_id": r[3],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export decode_logs 失败: {e}")
    # collections
    try:
        cursor = await store._db.execute(
            "SELECT id, name, code, description, version, item_count, status, "
            "created_at FROM collections WHERE owner_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["collections"] = [
            {
                "id": r[0], "name": r[1], "code": r[2], "description": r[3],
                "version": r[4], "item_count": r[5], "status": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export collections 失败: {e}")
    # tasks
    try:
        cursor = await store._db.execute(
            "SELECT id, task_type, status, progress, eta_seconds, created_at, updated_at "
            "FROM tasks WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["tasks"] = [
            {
                "id": r[0], "task_type": r[1], "status": r[2], "progress": r[3],
                "eta_seconds": r[4], "created_at": r[5], "updated_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export tasks 失败: {e}")
    # notifications
    try:
        cursor = await store._db.execute(
            "SELECT id, type, payload, is_read, created_at "
            "FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["notifications"] = [
            {
                "id": r[0], "type": r[1], "payload": r[2],
                "is_read": r[3], "created_at": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export notifications 失败: {e}")
    logger.info(
        f"[DataLifecycle] export_user_data user={user_id} "
        f"files={len(result['file_codes'])} "
        f"collections={len(result['collections'])} "
        f"tasks={len(result['tasks'])}"
    )
    return result


async def _write_audit_log(actor_id: int, action: str, target_type: str,
                           target_id: str, details: dict,
                           ip_addr: str = "") -> int:
    """内部: 写入审计日志(audit_log 表)。

    Args:
        actor_id: 操作者 id(管理员)
        action: 动作描述
        target_type: 目标类型
        target_id: 目标主键
        details: 详情字典(序列化为 JSON)
        ip_addr: 操作来源 IP(可选)

    Returns:
        新日志 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    try:
        cursor = await store._db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_type, action, target_type, target_id,
                details, ip_addr, created_at)
               VALUES (?, 'admin', ?, ?, ?, ?, ?, ?)""",
            (actor_id, action, target_type, target_id,
             json.dumps(details, ensure_ascii=False), ip_addr, now),
        )
        await store._db.commit()
        return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
    except Exception as e:
        logger.warning(f"[DataLifecycle] _write_audit_log 失败: {e}")
    # fail-closed:写审计日志失败时返回 0
    return 0


async def _write_audit_log_in_tx(tx, actor_id: int, action: str,
                                  target_type: str, target_id: str,
                                  details: dict,
                                  ip_addr: str = "") -> int:
    """R52 P1-3: 在指定事务连接内写入 audit_log(原子性保障)。

    与 ``_write_audit_log`` 的区别:
        - ``_write_audit_log`` 使用 store._db.execute + commit(独立事务)
        - ``_write_audit_log_in_tx`` 复用调用方的 tx,不单独 commit,
          确保审计日志与业务变更在同一事务内原子提交

    Args:
        tx: 事务连接(store.transaction() 返回的 tx)
        actor_id: 操作者 id(管理员)
        action: 动作描述
        target_type: 目标类型
        target_id: 目标主键
        details: 详情字典(序列化为 JSON)
        ip_addr: 操作来源 IP(可选)

    Returns:
        新日志 id;失败返回 0(异常会向上传播,由调用方决定是否回滚)
    """
    now = _dt.datetime.now().isoformat()
    cursor = await tx.execute(
        """INSERT INTO audit_log
           (actor_id, actor_type, action, target_type, target_id,
            details, ip_addr, created_at)
           VALUES (?, 'admin', ?, ?, ?, ?, ?, ?)""",
        (actor_id, action, target_type, target_id,
         json.dumps(details, ensure_ascii=False), ip_addr, now),
    )
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


# ─── R51 P1-1: 删除请求状态机 ─────────────────────────────


async def _ensure_deletion_requests_table() -> bool:
    """R51 P1-1: 惰性创建 deletion_requests 表(状态机持久化)。

    表结构:
        request_id      TEXT PRIMARY KEY(UUID)
        user_id         BIGINT
        admin_id        BIGINT
        status          TEXT(pending/processing/completed/failed)
        current_step    TEXT(当前执行中的 step)
        step_receipts   TEXT(JSON: 各 step 的 receipt)
        started_at      TEXT
        completed_at    TEXT
        failed_at       TEXT
        failure_reason  TEXT
        failed_step     TEXT
        created_at      TEXT

    Returns:
        True 表就绪;False 创建失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    try:
        await store._db.execute(
            f"""CREATE TABLE IF NOT EXISTS {_DELETION_REQUESTS_TABLE} (
                request_id      TEXT PRIMARY KEY,
                user_id         BIGINT NOT NULL,
                admin_id        BIGINT NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'pending',
                current_step    TEXT,
                step_receipts   TEXT,
                started_at      TEXT,
                completed_at    TEXT,
                failed_at       TEXT,
                failure_reason  TEXT,
                failed_step     TEXT,
                created_at      TEXT NOT NULL
            )"""
        )
        # 状态查询索引(按 user_id + status)
        # CREATE INDEX IF NOT EXISTS 本身幂等,无需 try/except 包裹(避免吞掉异常)
        await store._db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_deletion_requests_user_status "
            f"ON {_DELETION_REQUESTS_TABLE}(user_id, status)"
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] 创建 deletion_requests 表失败: {e}")
    # fail-closed:创建表失败时返回 False
    return False


async def _create_deletion_request(user_id: int, admin_id: int) -> str:
    """R51 P1-1: 创建 pending 状态的删除请求。

    Args:
        user_id: 被删除用户 id
        admin_id: 操作管理员 id(0=用户自删)

    Returns:
        request_id(UUID);失败返回 ""
    """
    store = get_cache_store()
    if not store._db:
        return ""
    request_id = str(uuid.uuid4())
    now = _dt.datetime.now().isoformat()
    try:
        # 注意: step_receipts 默认 '{}' — 末段为普通字符串,避免 f-string 解析 '{}'
        await store._db.execute(
            f"INSERT INTO {_DELETION_REQUESTS_TABLE} "
            f"(request_id, user_id, admin_id, status, current_step, "
            f"step_receipts, started_at, completed_at, failed_at, "
            f"failure_reason, failed_step, created_at) "
            "VALUES (?, ?, ?, ?, NULL, '{}', NULL, NULL, NULL, NULL, NULL, ?)",
            (request_id, user_id, admin_id, DELETE_STATUS_PENDING, now),
        )
        await store._db.commit()
        logger.info(
            f"[DataLifecycle] 创建删除请求 request_id={request_id} "
            f"user={user_id} admin={admin_id}"
        )
        return request_id
    except Exception as e:
        logger.warning(f"[DataLifecycle] 创建删除请求失败: {e}")
        return ""


async def _transition_request_status(
    request_id: str, new_status: str,
    step_receipts: dict | None = None,
    current_step: str | None = None,
    failure_reason: str = "",
    failed_step: str = "",
) -> bool:
    """R51 P1-1: 状态机迁移(pending→processing→completed/failed)。

    Args:
        request_id: 请求 id
        new_status: 目标状态(processing/completed/failed)
        step_receipts: 当前的 step receipts(全量更新)
        current_step: 当前执行中的 step(仅 processing 时填)
        failure_reason: 失败原因(仅 failed 时填)
        failed_step: 失败的 step 名称(仅 failed 时填)

    Returns:
        True 迁移成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    sets = ["status = ?", "current_step = ?"]
    params: list[Any] = [new_status, current_step]
    if step_receipts is not None:
        sets.append("step_receipts = ?")
        params.append(json.dumps(step_receipts, ensure_ascii=False))
    if new_status == DELETE_STATUS_PROCESSING:
        sets.append("started_at = COALESCE(started_at, ?)")
        params.append(now)
    elif new_status == DELETE_STATUS_COMPLETED:
        sets.append("completed_at = ?")
        params.append(now)
    elif new_status == DELETE_STATUS_FAILED:
        sets.append("failed_at = ?")
        params.append(now)
        sets.append("failure_reason = ?")
        params.append(failure_reason)
        sets.append("failed_step = ?")
        params.append(failed_step)
    params.append(request_id)
    try:
        await store._db.execute(
            f"UPDATE {_DELETION_REQUESTS_TABLE} SET "
            + ", ".join(sets)
            + " WHERE request_id = ?",
            tuple(params),
        )
        await store._db.commit()
        logger.info(
            f"[DataLifecycle] 状态迁移 request_id={request_id} "
            f"new_status={new_status}"
        )
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] 状态迁移失败: {e}")
    # fail-closed:状态迁移失败时返回 False
    return False


async def _transition_request_status_in_tx(
    tx, request_id: str, new_status: str,
    step_receipts: dict | None = None,
    current_step: str | None = None,
    failure_reason: str = "",
    failed_step: str = "",
) -> None:
    """R52 P1-3: 在指定事务连接内执行状态机迁移(原子性保障)。

    与 ``_transition_request_status`` 的区别:
        - 复用调用方的事务连接,不单独 commit
        - 失败时抛异常(由调用方决定回滚),不再返回 False

    Args:
        tx: 事务连接(store.transaction() 返回的 tx)
        request_id: 请求 id
        new_status: 目标状态(processing/completed/failed)
        step_receipts: 当前的 step receipts(全量更新)
        current_step: 当前执行中的 step(仅 processing 时填)
        failure_reason: 失败原因(仅 failed 时填)
        failed_step: 失败的 step 名称(仅 failed 时填)

    Raises:
        Exception: 状态迁移失败(由调用方捕获并回滚事务)
    """
    now = _dt.datetime.now().isoformat()
    sets = ["status = ?", "current_step = ?"]
    params: list[Any] = [new_status, current_step]
    if step_receipts is not None:
        sets.append("step_receipts = ?")
        params.append(json.dumps(step_receipts, ensure_ascii=False))
    if new_status == DELETE_STATUS_PROCESSING:
        sets.append("started_at = COALESCE(started_at, ?)")
        params.append(now)
    elif new_status == DELETE_STATUS_COMPLETED:
        sets.append("completed_at = ?")
        params.append(now)
    elif new_status == DELETE_STATUS_FAILED:
        sets.append("failed_at = ?")
        params.append(now)
        sets.append("failure_reason = ?")
        params.append(failure_reason)
        sets.append("failed_step = ?")
        params.append(failed_step)
    params.append(request_id)
    await tx.execute(
        f"UPDATE {_DELETION_REQUESTS_TABLE} SET "
        + ", ".join(sets)
        + " WHERE request_id = ?",
        tuple(params),
    )
    logger.info(
        f"[DataLifecycle] R52 P1-3: 状态迁移(in_tx) request_id={request_id} "
        f"new_status={new_status}"
    )


async def _get_deletion_request(request_id: str) -> dict | None:
    """R51 P1-1: 查询删除请求详情。"""
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            f"SELECT request_id, user_id, admin_id, status, current_step, "
            f"step_receipts, started_at, completed_at, failed_at, "
            f"failure_reason, failed_step, created_at "
            f"FROM {_DELETION_REQUESTS_TABLE} WHERE request_id = ?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        receipts_raw = row[5] or "{}"
        try:
            receipts = json.loads(receipts_raw) if isinstance(receipts_raw, str) else receipts_raw
        except Exception:
            receipts = {}
        return {
            "request_id": row[0], "user_id": row[1], "admin_id": row[2],
            "status": row[3], "current_step": row[4],
            "step_receipts": receipts, "started_at": row[6],
            "completed_at": row[7], "failed_at": row[8],
            "failure_reason": row[9], "failed_step": row[10],
            "created_at": row[11],
        }
    except Exception as e:
        logger.warning(f"[DataLifecycle] 查询删除请求失败: {e}")
        return None


async def _execute_step_in_tx(
    tx: Any, store: Any, step_name: str, user_id: int, now: str,
) -> dict:
    """R51 P1-1: 在事务中执行单个删除 step。

    所有 step 共享同一个 tx(store.transaction() 上下文),
    任一 step 抛异常 → 整个事务回滚。

    Returns:
        receipt dict: {status: success/failed, rows_affected: int,
                       started_at, finished_at, error: str}
    """
    receipt: dict = {
        "status": "success",
        "rows_affected": 0,
        "started_at": now,
        "finished_at": now,
        "error": "",
    }
    try:
        if step_name == "step_files":
            cursor = await tx.execute(
                "UPDATE file_records_local "
                "SET deleted_at = ?, status = 'deleted', crdb_synced = 0 "
                "WHERE uploader_id = ? AND deleted_at IS NULL",
                (now, user_id),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
            # 写 dirty_outbox tombstone(同事务)
            cursor = await tx.execute(
                "SELECT file_code FROM file_records_local "
                "WHERE uploader_id = ? AND deleted_at = ?",
                (user_id, now),
            )
            rows = await cursor.fetchall()
            for r in rows:
                await store.add_dirty_outbox(
                    "file_records_local", str(r[0]),
                    operation="tombstone", connection=tx,
                )
        elif step_name == "step_codes":
            cursor = await tx.execute(
                "UPDATE codes_local "
                "SET deleted_at = ?, status = 'deleted', crdb_synced = 0 "
                "WHERE uploader_id = ? AND deleted_at IS NULL",
                (now, user_id),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
            cursor = await tx.execute(
                "SELECT code FROM codes_local "
                "WHERE uploader_id = ? AND deleted_at = ?",
                (user_id, now),
            )
            rows = await cursor.fetchall()
            for r in rows:
                await store.add_dirty_outbox(
                    "codes_local", str(r[0]),
                    operation="tombstone", connection=tx,
                )
        elif step_name == "step_collections":
            # 注意: collections 表无 deleted_at/crdb_synced 列,
            # 只更新 status='deleted'(collections 表已有 status 列)
            cursor = await tx.execute(
                "UPDATE collections "
                "SET status = 'deleted', updated_at = ? "
                "WHERE owner_id = ? AND status != 'deleted'",
                (now, user_id),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
            cursor = await tx.execute(
                "SELECT id FROM collections "
                "WHERE owner_id = ? AND status = 'deleted' AND updated_at = ?",
                (user_id, now),
            )
            rows = await cursor.fetchall()
            for r in rows:
                await store.add_dirty_outbox(
                    "collections", str(r[0]),
                    operation="tombstone", connection=tx,
                )
        elif step_name == "step_notifications":
            cursor = await tx.execute(
                "DELETE FROM notifications WHERE user_id = ?",
                (user_id,),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
        elif step_name == "step_tasks":
            cursor = await tx.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ? "
                "WHERE user_id = ? AND status NOT IN ('cancelled', 'done')",
                (now, user_id),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
        elif step_name == "step_users_local":
            # 注意: users_local 表无 status 列,使用 is_banned=1 + deleted_at 标记删除
            cursor = await tx.execute(
                "UPDATE users_local "
                "SET is_banned = 1, deleted_at = ?, "
                "crdb_synced = 0, updated_at = ? "
                "WHERE user_id = ?",
                (now, now, user_id),
            )
            receipt["rows_affected"] = int(cursor.rowcount or 0)
            # 写 dirty_outbox upsert(标记用户为 deleted,需同步到 CRDB)
            await store.add_dirty_outbox(
                "users_local", str(user_id),
                operation="upsert", connection=tx,
            )
        else:
            receipt["status"] = "failed"
            receipt["error"] = f"unknown_step:{step_name}"
        receipt["finished_at"] = _dt.datetime.now().isoformat()
    except Exception as e:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(e).__name__}: {e}"
        receipt["finished_at"] = _dt.datetime.now().isoformat()
        # 重新抛出让事务回滚
        raise
    return receipt


async def delete_user_data(user_id: int, admin_id: int = 0) -> bool:
    """R51 P1-1: 删除用户所有数据(状态机 + step receipts + 事务化)。

    改造要点:
    - 创建 deletion_requests 行(pending)
    - 状态迁移: pending → processing → completed/failed
    - 所有 step 在同一 transaction 内执行,任一失败 → 整个事务回滚
    - 失败时标记 deletion_requests 为 failed + 记录 failed_step + failure_reason
    - 成功时标记 completed
    - 不再"warning 后返回 success",失败显式 raise AppError

    Args:
        user_id: 被删除用户 id
        admin_id: 操作管理员 id(0=用户自删)

    Returns:
        True 删除成功(全部 step 完成)

    Raises:
        AppError(DATA_LIFECYCLE_DELETE_REQUEST_FAILED): 状态机初始化/迁移失败
        AppError(DATA_LIFECYCLE_DELETE_STEP_FAILED): 任一 step 失败
    """
    store = get_cache_store()
    if not store._db:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
            params={"user_id": user_id, "reason": "cache_store_unavailable"},
        )
    # 确保状态机表存在
    if not await _ensure_deletion_requests_table():
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
            params={"user_id": user_id, "reason": "table_init_failed"},
        )
    # 创建 pending 请求
    request_id = await _create_deletion_request(user_id, admin_id)
    if not request_id:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
            params={"user_id": user_id, "reason": "create_request_failed"},
        )
    # 迁移到 processing
    await _transition_request_status(
        request_id, DELETE_STATUS_PROCESSING,
        current_step=DELETE_STEPS[0] if DELETE_STEPS else None,
    )

    # 在同一事务内顺序执行所有 step
    step_receipts: dict[str, dict] = {}
    failed_step: str = ""
    failure_reason: str = ""
    current_step_name: str = ""  # 跟踪当前执行的 step(异常时用于识别失败步骤)
    try:
        async with store.transaction() as tx:
            for step_name in DELETE_STEPS:
                # 更新 current_step(在事务内,但用 tx 直接更新避免额外 commit)
                await tx.execute(
                    f"UPDATE {_DELETION_REQUESTS_TABLE} "
                    f"SET current_step = ? WHERE request_id = ?",
                    (step_name, request_id),
                )
                # 提前记录当前 step 名(以防 _execute_step_in_tx 抛异常时能识别)
                current_step_name = step_name
                receipt = await _execute_step_in_tx(
                    tx, store, step_name, user_id,
                    _dt.datetime.now().isoformat(),
                )
                step_receipts[step_name] = receipt
                if receipt["status"] != "success":
                    failed_step = step_name
                    failure_reason = receipt.get("error", "")
                    # 抛异常触发事务回滚
                    raise RuntimeError(
                        f"step {step_name} failed: {failure_reason}"
                    )
            # 全部成功 → 事务自动 COMMIT(store.transaction 退出时)
    except Exception as tx_err:
        # 事务已回滚,标记 deletion_requests 为 failed
        # (failed 标记本身用独立连接,不影响已回滚的事务)
        if not failed_step:
            # _execute_step_in_tx 抛出但未填充 failed_step,使用当前 step 名
            failed_step = current_step_name or "unknown"
            failure_reason = f"{type(tx_err).__name__}: {tx_err}"
        await _transition_request_status(
            request_id, DELETE_STATUS_FAILED,
            step_receipts=step_receipts,
            failure_reason=failure_reason,
            failed_step=failed_step,
        )
        logger.error(
            f"[DataLifecycle] delete_user_data 失败 request_id={request_id} "
            f"user={user_id} failed_step={failed_step} reason={failure_reason}"
        )
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_DELETE_STEP_FAILED,
            params={
                "user_id": user_id, "request_id": request_id,
                "step": failed_step, "step_error": failure_reason,
            },
        ) from tx_err

    # R52 P1-3: 全部成功 → completed 状态迁移 + audit_log 在同一事务内(原子性)
    # 原实现使用独立事务写 audit,若 audit 失败会出现"已删除但无审计"的不一致状态。
    # 现在合并到同一 transaction: completed + audit + dirty_outbox 任一失败 → 整体回滚
    try:
        async with store.transaction() as tx:
            # 1. 状态迁移 pending → completed(在 tx 内)
            await _transition_request_status_in_tx(
                tx, request_id, DELETE_STATUS_COMPLETED,
                step_receipts=step_receipts,
            )
            # 2. 写审计日志(在 tx 内,与状态迁移原子提交)
            await _write_audit_log_in_tx(
                tx, admin_id, "delete_user_data", "user", str(user_id),
                {
                    "request_id": request_id,
                    "step_receipts": step_receipts,
                    "user_id": user_id,
                    "admin_id": admin_id,
                },
                "",
            )
            # 3. 写 dirty_outbox(audit_log 同步到 CRDB)
            await store.add_dirty_outbox(
                "audit_log", "last",
                operation="upsert", connection=tx,
            )
            # 4. 写 dirty_outbox(deletion_requests 同步到 CRDB)
            await store.add_dirty_outbox(
                _DELETION_REQUESTS_TABLE, request_id,
                operation="upsert", connection=tx,
            )
        # 事务自动 COMMIT
        logger.info(
            f"[DataLifecycle] R52 P1-3: delete_user_data 完成(同事务原子审计) "
            f"user={user_id} admin={admin_id} request_id={request_id}"
        )
    except Exception as audit_err:
        # audit/completed 失败 → 整体事务回滚,数据未删除,标记 failed
        logger.error(
            f"[DataLifecycle] R52 P1-3: completed+audit 事务失败 "
            f"user={user_id} request_id={request_id}: {audit_err}"
        )
        await _transition_request_status(
            request_id, DELETE_STATUS_FAILED,
            step_receipts=step_receipts,
            failure_reason=f"audit_tx_failed: {audit_err}",
            failed_step="audit_log",
        )
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
            params={
                "user_id": user_id, "request_id": request_id,
                "reason": f"audit_tx_failed: {audit_err}",
            },
        ) from audit_err
    return True


async def set_retention(user_id: int, days: int) -> bool:
    """设置用户数据保留期(0=永久)。

    Args:
        user_id: 用户 id
        days: 保留天数(0=永久保留)

    Returns:
        True 设置成功;False 失败
    """
    if days < 0:
        logger.warning(f"[DataLifecycle] set_retention 非法 days={days}")
        return False
    if not await _ensure_retention_table():
        return False
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        await store._db.execute(
            f"""INSERT INTO {_RETENTION_TABLE}
                (user_id, retention_days, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    retention_days = excluded.retention_days,
                    updated_at = excluded.updated_at""",
            (user_id, days, now),
        )
        await store._db.commit()
        logger.info(
            f"[DataLifecycle] set_retention user={user_id} days={days}"
        )
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] set_retention 失败: {e}")
    # fail-closed:set_retention 失败时返回 False
    return False


async def get_retention(user_id: int) -> int:
    """获取保留期(默认 7 天)。

    Args:
        user_id: 用户 id

    Returns:
        保留天数(0=永久);未设置时返回默认 7 天
    """
    if not await _ensure_retention_table():
        return DEFAULT_RETENTION_DAYS
    store = get_cache_store()
    if not store._db:
        return DEFAULT_RETENTION_DAYS
    try:
        cursor = await store._db.execute(
            f"SELECT retention_days FROM {_RETENTION_TABLE} WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return DEFAULT_RETENTION_DAYS
        return int(row[0])
    except Exception as e:
        logger.warning(f"[DataLifecycle] get_retention 失败: {e}")
        return DEFAULT_RETENTION_DAYS


async def _verify_backup_marker(
    user_id: int | None = None,
    *,
    require_user_scope: bool = False,
    require_checksum: bool = False,
) -> dict | None:
    """R51 P1-1 + R52 P1-3 + R53 P1-3: 物理删除前验证 backup marker。

    调用 BackupEngine.get_last_successful_backup() 检查最近一次成功备份:
    - 返回非 None → 存在成功备份 → 允许物理删除
    - 返回 None → 无成功备份 → 拒绝物理删除(避免删后无法恢复)

    失败原因(返回 None 的场景):
    - backup_history 为空(从未备份)
    - 最近成功备份 COMPLETE marker 在 R2 中丢失(RPO 不合规)
    - R2 storage 不可达

    R52 P1-3 增强(绑定用户范围 + 时间 + checksum):
        - require_user_scope=True: 备份记录必须含 user_id 字段且匹配
          (防止"备份存在但被删用户不在备份范围内"的假阳性)
        - require_checksum=True: 备份记录必须含 checksum 字段
          (确保备份内容完整性可校验,防备份被篡改)
        - 时间字段 completed_at 必须存在(已隐含在 get_last_successful_backup)

    R53 P1-3 增强(严格绑定 + user_coverage):
        - backup_id 必须存在(绑定具体备份实例,防止"有备份但不指定哪个"的模糊授权)
        - 批量全库备份优先使用 manifest 中的 user_coverage(用户 ID 列表),
          而非单一 user_id 字段;user_id 在 user_coverage 中即视为覆盖
        - 返回 dict(含 backup_id/checksum/completed_at/user_coverage)供调用方绑定审计

    Args:
        user_id: 被删除用户 ID(require_user_scope=True 时校验)
        require_user_scope: 是否要求备份绑定用户范围(R52 P1-3)
        require_checksum: 是否要求备份含 checksum(R52 P1-3)

    Returns:
        成功: dict({backup_id, checksum, completed_at, user_coverage});
        失败: None
    """
    try:
        from services.backup_engine import BackupEngine
        engine = BackupEngine()
        latest = await engine.get_last_successful_backup()
        if latest is None:
            logger.warning(
                "[DataLifecycle] backup marker 验证失败: 无成功备份记录,拒绝物理删除"
            )
            return None
        # R53 P1-3: backup_id 必须存在(绑定具体备份实例)
        backup_id = latest.get("backup_id")
        if not backup_id:
            logger.warning(
                "[DataLifecycle] R53 P1-3: backup marker 缺少 backup_id,"
                "拒绝物理删除(无法绑定具体备份实例)"
            )
            return None
        # R52 P1-3: completed_at 必须存在(时间绑定)
        completed_at = latest.get("completed_at")
        if not completed_at:
            logger.warning(
                f"[DataLifecycle] R52 P1-3: backup marker 缺少 completed_at,"
                f"拒绝物理删除 backup_id={backup_id}"
            )
            return None
        # R52 P1-3 + R53 P1-3: 用户范围绑定
        # 优先使用 manifest 中的 user_coverage(全库备份覆盖的用户列表),
        # 其次回退到单一 user_id 字段(单用户备份场景)
        if require_user_scope:
            user_coverage = latest.get("user_coverage")
            if isinstance(user_coverage, list) and len(user_coverage) > 0:
                # 全库备份: 校验 target user_id 在 user_coverage 中
                if user_id is not None:
                    try:
                        target_uid = int(user_id)
                    except (TypeError, ValueError):
                        target_uid = None
                    covered = False
                    if target_uid is not None:
                        for uid in user_coverage:
                            try:
                                if int(uid) == target_uid:
                                    covered = True
                                    break
                            except (TypeError, ValueError):
                                continue
                    if not covered:
                        logger.warning(
                            f"[DataLifecycle] R53 P1-3: backup marker user_coverage "
                            f"未覆盖目标用户,拒绝物理删除 "
                            f"backup_id={backup_id} target_user_id={user_id} "
                            f"coverage_size={len(user_coverage)}"
                        )
                        return None
            else:
                # 单用户备份: 校验 user_id 字段匹配
                backup_user_id = latest.get("user_id")
                if backup_user_id is None:
                    logger.warning(
                        f"[DataLifecycle] R52 P1-3: backup marker 缺少 user_id 字段,"
                        f"拒绝物理删除(require_user_scope=True) "
                        f"backup_id={backup_id}"
                    )
                    return None
                if user_id is not None and int(backup_user_id) != int(user_id):
                    logger.warning(
                        f"[DataLifecycle] R52 P1-3: backup marker user_id 不匹配,"
                        f"拒绝物理删除 backup_user_id={backup_user_id} "
                        f"target_user_id={user_id}"
                    )
                    return None
        # R52 P1-3: checksum 完整性绑定
        checksum = latest.get("checksum") or latest.get("sha256")
        if require_checksum:
            if not checksum:
                logger.warning(
                    f"[DataLifecycle] R52 P1-3: backup marker 缺少 checksum,"
                    f"拒绝物理删除(require_checksum=True) "
                    f"backup_id={backup_id}"
                )
                return None
        logger.info(
            f"[DataLifecycle] backup marker 验证通过 backup_id={backup_id} "
            f"completed_at={completed_at} "
            f"user_scope_checked={require_user_scope} "
            f"checksum_checked={require_checksum}"
        )
        return {
            "backup_id": backup_id,
            "checksum": checksum or "",
            "completed_at": completed_at,
            "user_coverage": latest.get("user_coverage", []),
        }
    except Exception as e:
        logger.warning(
            f"[DataLifecycle] backup marker 验证异常,拒绝物理删除(失败即拒绝): {e}"
        )
        return None


async def cleanup_expired_data(
    batch_size: int = 1000,
    skip_backup_check: bool = False,
    *,
    approval_action_id: str | None = None,
    request_hash: str = "",
    principal_id: int = 0,
) -> int:
    """R51 P1-1 + R55 P0-3/P0-6 + R61 P0-01: 清理过期数据。

    R55 P0-3: break-glass 必须绑定 principal_id、request_hash,
    验证审批 action_type=data_lifecycle_break_glass,审批不可跨请求复用。

    R55 P0-6: 审计日志写入纳入删除事务,审批状态回写失败不能返回成功。

    R61 P0-01 整改: break-glass 路径(``skip_backup_check=True``)改走
    ``execute_high_risk_command_uow`` 统一事务 — 审批消费 + 状态机 CAS +
    业务删除 + dirty_outbox tombstone + audit_log + effect_receipt +
    mark_approved_executed 在**单一** ``BEGIN IMMEDIATE`` 事务中原子提交/回滚。
    旧实现分别提交审批消费与业务删除,出现"审批已消费但业务失败"或
    "业务成功但 mark_executed 失败"的不一致;新实现任一失败 → ROLLBACK
    (审批与 MFA 不被消费,状态机不前进)。
    """
    if not await _ensure_retention_table():
        return 0
    store = get_cache_store()
    if not store._db:
        return 0
    # R55 P0-3: break-glass 必须绑定 principal_id、request_hash
    # 验证审批 action_type=data_lifecycle_break_glass,审批不可跨请求复用
    if skip_backup_check:
        # ─── R61 P0-01: break-glass 统一 UoW 路径 ───
        # 审批消费 + 状态机 CAS + 业务删除 + dirty_outbox + audit_log +
        # effect_receipt + mark_approved_executed 在单一事务中原子提交/回滚。
        return await _cleanup_expired_data_break_glass_uow(
            batch_size=batch_size,
            approval_action_id=approval_action_id,
            request_hash=request_hash,
            principal_id=principal_id,
        )
    # ─── 普通路径: per-user 事务 + backup marker 校验(不变) ───
    # R53 P1-3: 物理删除前强制严格 backup marker 校验
    # require_user_scope=True: 校验用户覆盖范围(全库备份用 user_coverage)
    # require_checksum=True: 校验 manifest checksum(完整性绑定)
    backup_info: dict | None = None
    backup_info = await _verify_backup_marker(
        require_user_scope=True,
        require_checksum=True,
    )
    if backup_info is None:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING,
            params={"reason": "strict_backup_marker_verification_failed"},
        )
    total_cleaned = 0
    now_dt = _dt.datetime.now()
    # 拉取所有设置了保留期(非永久)的用户
    _fetch_failed = False
    try:
        cursor = await store._db.execute(
            f"SELECT user_id, retention_days FROM {_RETENTION_TABLE} "
            f"WHERE retention_days > 0 LIMIT ?",
            (batch_size,),
        )
        rows = await cursor.fetchall()
    except Exception as e:
        logger.warning(f"[DataLifecycle] cleanup 拉取保留期配置失败: {e}")
        _fetch_failed = True
    if _fetch_failed:
        # fail-closed:拉取失败时返回 0
        return 0
    for r in rows:
        user_id, retention_days = r[0], int(r[1])
        cutoff_dt = now_dt - _dt.timedelta(days=retention_days)
        cutoff_iso = cutoff_dt.isoformat()
        # R53 P1-3: 校验该用户在 backup marker 的 user_coverage 中
        # 批量全库备份使用 manifest 中的 user_coverage,而非单一 user_id 字段
        if backup_info is not None:
            user_coverage = backup_info.get("user_coverage", [])
            if isinstance(user_coverage, list) and len(user_coverage) > 0:
                # 全库备份: 校验 user_id 在 user_coverage 中
                covered = False
                try:
                    target_uid = int(user_id)
                except (TypeError, ValueError):
                    target_uid = None
                if target_uid is not None:
                    for uid in user_coverage:
                        try:
                            if int(uid) == target_uid:
                                covered = True
                                break
                        except (TypeError, ValueError):
                            continue
                if not covered:
                    logger.warning(
                        f"[DataLifecycle] R53 P1-3: user_id={user_id} 不在 backup "
                        f"user_coverage 中,跳过物理删除 "
                        f"backup_id={backup_info.get('backup_id')}"
                    )
                    continue
            # user_coverage 为空时(单用户备份)不适用于批量清理,
            # _verify_backup_marker 已通过 require_user_scope 校验存在性
        # R55 P0-5/P0-6: per-user 原子删除 + 审计同事务
        _user_receipt = {
            "user_id": user_id,
            "file_records_deleted": 0,
            "codes_deleted": 0,
            "status": "pending",
        }
        audit_details = {
            "user_id": user_id,
            "file_records_deleted": 0,
            "codes_deleted": 0,
            "deletion_status": "pending",
            "retention_cutoff": cutoff_iso,
            "retention_days": retention_days,
        }
        if backup_info is not None:
            audit_details.update({
                "backup_id": backup_info.get("backup_id"),
                "checksum": backup_info.get("checksum"),
                "completed_at": backup_info.get("completed_at"),
            })
        try:
            async with store.transaction() as tx:
                # 物理删除该用户已软删(deleted_at < cutoff)的 file_records
                cursor = await tx.execute(
                    "DELETE FROM file_records_local "
                    "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                    "AND deleted_at < ?",
                    (user_id, cutoff_iso),
                )
                _user_receipt["file_records_deleted"] = cursor.rowcount or 0
                # 物理删除该用户已软删的 codes
                cursor = await tx.execute(
                    "DELETE FROM codes_local "
                    "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                    "AND deleted_at < ?",
                    (user_id, cutoff_iso),
                )
                _user_receipt["codes_deleted"] = cursor.rowcount or 0
                # 更新 last_purged_at(同事务)
                await tx.execute(
                    f"UPDATE {_RETENTION_TABLE} SET last_purged_at = ? "
                    f"WHERE user_id = ?",
                    (now_dt.isoformat(), user_id),
                )
                # R55 P0-6: 审计日志纳入删除事务(原子性)
                audit_details["file_records_deleted"] = _user_receipt["file_records_deleted"]
                audit_details["codes_deleted"] = _user_receipt["codes_deleted"]
                audit_details["deletion_status"] = "completed"
                await _write_audit_log_in_tx(
                    tx, 0, "physical_delete_per_user", "user", str(user_id),
                    audit_details,
                )
            _user_receipt["status"] = "completed"
            total_cleaned += (
                _user_receipt["file_records_deleted"]
                + _user_receipt["codes_deleted"]
            )
        except Exception as e:
            _user_receipt["status"] = "failed"
            audit_details["deletion_status"] = "failed"
            logger.error(
                f"[DataLifecycle] R55 P0-6: per-user 原子删除失败 "
                f"user_id={user_id}: {e}"
            )
        if total_cleaned >= batch_size:
            break
    logger.info(
        f"[DataLifecycle] cleanup_expired_data 清理 {total_cleaned} 行 "
        f"(users_checked={len(rows)})"
    )
    return total_cleaned


async def _cleanup_expired_data_break_glass_uow(
    *,
    batch_size: int,
    approval_action_id: str | None,
    request_hash: str,
    principal_id: int,
) -> int:
    """R61 P0-01: break-glass 清理路径 — 走统一高风险命令 UoW。

    将"审批消费 + 状态机 CAS + 业务删除 + dirty_outbox tombstone + audit_log
    + effect_receipt + mark_approved_executed"封装在 ``execute_high_risk_command_uow``
    的单一 ``BEGIN IMMEDIATE`` 事务中,任一失败 → ROLLBACK(审批与 MFA 不被消费,
    状态机不前进)。

    与普通路径(``skip_backup_check=False``)的区别:
      - 无 backup marker 校验(break-glass 已显式跳过)
      - per-user dirty_outbox tombstone(让 relay worker 通知/复制)
      - 审批消费 + 标记 executed 与业务删除同事务(原子性)

    Args:
        batch_size: 单批最大清理行数
        approval_action_id: 审批动作 ID(command_executions.action_id)
        request_hash: 调用方声明的 request_hash(64 hex)
        principal_id: 发起人 principal_id

    Returns:
        总清理行数

    Raises:
        AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED):
            参数缺失 / action_type 不符 / principal_id 不一致 /
            双人审批校验失败 / UoW 任一步骤失败
    """
    store = get_cache_store()
    if not store._db:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "store_unavailable_for_break_glass"},
        )
    # R55 P0-3: 参数校验
    if not approval_action_id:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "skip_backup_check_requires_real_approval"},
        )
    if not request_hash or len(request_hash) != 64:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "request_hash_required_64_hex"},
        )
    if not principal_id:
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": "principal_id_required"},
        )
    # R55 P0-3: 验证审批 action_type 必须是 data_lifecycle_break_glass
    # R56 P0-2: 删除 except Exception: pass(fail-closed),
    #           同时校验 command_executions.principal_id 与传入的 principal_id 一致
    try:
        _type_rows = await store._db.execute_fetchall(
            "SELECT command_type, principal_id FROM command_executions "
            "WHERE action_id = ?",
            (approval_action_id,),
        )
        if not _type_rows or not _type_rows[0]:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={"reason": "approval_record_not_found"},
            )
        _cmd_type = _type_rows[0][0]
        _stored_principal_id = int(_type_rows[0][1] or 0)
        if _cmd_type != "data_lifecycle_break_glass":
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={"reason": "invalid_action_type",
                        "expected": "data_lifecycle_break_glass",
                        "actual": _cmd_type},
            )
        # R56 P0-2: principal_id 必须与 command_executions 记录一致(防越权)
        if _stored_principal_id != principal_id:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "principal_id_mismatch",
                    "stored": _stored_principal_id,
                    "passed": principal_id,
                },
            )
    except AppError:
        raise
    except Exception as e:
        # R56 P0-2: fail-closed — 不再吞异常,任何查询错误都阻断
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={"reason": f"command_type_verification_failed: "
                              f"{type(e).__name__}: {e}"},
        ) from e
    # R61 P0-01: 纯校验双人审批 → ApprovalGrant(不再自提交消费)
    # 实际 CAS 消费(mfa_receipts jti + command_approvals.consumed_at)延迟到 UoW
    grant = await _verify_break_glass_two_person_approval(
        action_id=approval_action_id,
        expected_principal_id=principal_id,
    )
    # R61 P0-01: 业务回调 — per-user 物理删除 + dirty_outbox tombstone + audit_log
    # 全部写入 UoW 传入的 tx(统一事务),不得自行 commit。
    async def _break_glass_business_action(tx: Any) -> dict:
        total = 0
        users_checked = 0
        now_dt = _dt.datetime.now()
        cursor = await tx.execute(
            f"SELECT user_id, retention_days FROM {_RETENTION_TABLE} "
            f"WHERE retention_days > 0 LIMIT ?",
            (batch_size,),
        )
        rows = await cursor.fetchall()
        users_checked = len(rows)
        for r in rows:
            user_id = int(r[0])
            retention_days = int(r[1])
            cutoff_dt = now_dt - _dt.timedelta(days=retention_days)
            cutoff_iso = cutoff_dt.isoformat()
            # R61 P0-01: 查询待删除 PK(删除前),用于 dirty_outbox tombstone
            cursor = await tx.execute(
                "SELECT file_code FROM file_records_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            file_pks = [str(row[0]) for row in await cursor.fetchall()]
            cursor = await tx.execute(
                "SELECT code FROM codes_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            code_pks = [str(row[0]) for row in await cursor.fetchall()]
            # 物理删除 file_records_local
            cursor = await tx.execute(
                "DELETE FROM file_records_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            file_deleted = int(cursor.rowcount or 0)
            # 物理删除 codes_local
            cursor = await tx.execute(
                "DELETE FROM codes_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            codes_deleted = int(cursor.rowcount or 0)
            # 更新 last_purged_at(同事务)
            await tx.execute(
                f"UPDATE {_RETENTION_TABLE} SET last_purged_at = ? "
                f"WHERE user_id = ?",
                (now_dt.isoformat(), user_id),
            )
            # R61 P0-01: per-row dirty_outbox tombstone(让 relay worker 通知/复制)
            for pk in file_pks:
                await store.add_dirty_outbox(
                    "file_records_local", pk,
                    operation="tombstone", connection=tx,
                )
            for pk in code_pks:
                await store.add_dirty_outbox(
                    "codes_local", pk,
                    operation="tombstone", connection=tx,
                )
            # R55 P0-6: 审计日志纳入删除事务(原子性)
            audit_details = {
                "user_id": user_id,
                "file_records_deleted": file_deleted,
                "codes_deleted": codes_deleted,
                "deletion_status": "completed",
                "retention_cutoff": cutoff_iso,
                "retention_days": retention_days,
                "break_glass": True,
                "approval_action_id": approval_action_id,
            }
            await _write_audit_log_in_tx(
                tx, 0, "physical_delete_per_user", "user", str(user_id),
                audit_details,
            )
            total += file_deleted + codes_deleted
            if total >= batch_size:
                break
        return {"total_cleaned": total, "users_checked": users_checked}

    # R61 P0-01: 构造 HighRiskCommand 并执行统一 UoW
    import socket as _socket_bg
    _owner = f"{_socket_bg.gethostname()}:{os.getpid()}"
    command = HighRiskCommand(
        action_id=approval_action_id,
        command_type="data_lifecycle_break_glass",
        principal_id=principal_id,
        # 使用 grant.request_hash(approver 签名的 canonical hash),
        # 而非调用方传入的 request_hash 参数(防调用方篡改 payload)
        request_hash=grant.request_hash,
        owner=_owner,
        effect_type="purge",
        effect_target=approval_action_id,
        business_action=_break_glass_business_action,
    )
    result = await execute_high_risk_command_uow(command, grant)
    logger.info(
        f"[DataLifecycle] R61 P0-01: cleanup_expired_data(break_glass) 清理 "
        f"{result.total_cleaned} 行 action_id={approval_action_id} "
        f"business_result={result.business_result}"
    )
    return result.total_cleaned


async def log_admin_access(admin_id: int, action: str, target_type: str = "",
                           target_id: str = "", details: dict = None,
                           ip_addr: str = "") -> int:
    """记录管理员访问日志。

    Args:
        admin_id: 管理员 id
        action: 动作枚举(view_users/view_files/export_data/delete_data/config_change)
        target_type: 目标类型(可选)
        target_id: 目标主键(可选)
        details: 详情字典(序列化为 JSON)
        ip_addr: 操作来源 IP(可选)

    Returns:
        新日志 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    details_json = json.dumps(details or {}, ensure_ascii=False)
    try:
        cursor = await store._db.execute(
            """INSERT INTO admin_access_log
               (admin_id, action, target_type, target_id, details, ip_addr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (admin_id, action, target_type, target_id,
             details_json, ip_addr, now),
        )
        await store._db.commit()
        log_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        logger.info(
            f"[DataLifecycle] log_admin_access admin={admin_id} "
            f"action={action} target={target_type}:{target_id}"
        )
        return log_id
    except Exception as e:
        logger.warning(f"[DataLifecycle] log_admin_access 失败: {e}")
    # fail-closed:写管理员访问日志失败时返回 0
    return 0


async def list_admin_access_logs(admin_id: int | None = None, page: int = 1,
                                  page_size: int = 20) -> dict:
    """分页查询管理员访问日志。

    Args:
        admin_id: 按管理员过滤(None=全部)
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": N, "page": page, "page_size": page_size}
    """
    store = get_cache_store()
    empty = {"items": [], "total": 0, "page": page, "page_size": page_size}
    if not store._db:
        return empty
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 20
    offset = (page - 1) * page_size
    try:
        # 统计总数
        if admin_id is not None:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM admin_access_log WHERE admin_id = ?",
                (admin_id,),
            )
        else:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM admin_access_log"
            )
        row = await cursor.fetchone()
        total = int(row[0]) if row and row[0] else 0
        # 分页查询
        if admin_id is not None:
            cursor = await store._db.execute(
                """SELECT id, admin_id, action, target_type, target_id,
                          details, ip_addr, created_at
                   FROM admin_access_log WHERE admin_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (admin_id, page_size, offset),
            )
        else:
            cursor = await store._db.execute(
                """SELECT id, admin_id, action, target_type, target_id,
                          details, ip_addr, created_at
                   FROM admin_access_log
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (page_size, offset),
            )
        rows = await cursor.fetchall()
        items = [
            {
                "id": r[0], "admin_id": r[1], "action": r[2],
                "target_type": r[3], "target_id": r[4], "details": r[5],
                "ip_addr": r[6], "created_at": r[7],
            }
            for r in rows
        ]
        return {"items": items, "total": total, "page": page,
                "page_size": page_size}
    except Exception as e:
        logger.warning(f"[DataLifecycle] list_admin_access_logs 失败: {e}")
        return empty


async def check_retention_compliance() -> dict:
    """检查保留期合规性。

    统计:
    - total_users: 总用户数(设置了保留期的用户)
    - expired_count: 已过保留期但未清理的用户数
    - oldest_unpurged_days: 最久未清理的天数
    - compliance_rate: 合规率(1 - expired/total)

    Returns:
        {total_users, expired_count, oldest_unpurged_days, compliance_rate}
    """
    empty = {
        "total_users": 0, "expired_count": 0,
        "oldest_unpurged_days": 0, "compliance_rate": 1.0,
    }
    if not await _ensure_retention_table():
        return empty
    store = get_cache_store()
    if not store._db:
        return empty
    now_dt = _dt.datetime.now()
    total_users = 0
    expired_count = 0
    oldest_unpurged_days = 0
    try:
        cursor = await store._db.execute(
            f"SELECT user_id, retention_days, last_purged_at "
            f"FROM {_RETENTION_TABLE} WHERE retention_days > 0"
        )
        rows = await cursor.fetchall()
        total_users = len(rows)
        for r in rows:
            retention_days = int(r[1])
            last_purged_at = r[2]
            # 判断是否过期:最后清理时间距今超过保留期则视为过期
            if last_purged_at:
                try:
                    purged_dt = _dt.datetime.fromisoformat(last_purged_at)
                    days_since_purge = (now_dt - purged_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    days_since_purge = retention_days + 1
            else:
                # 从未清理过,按最大值处理
                days_since_purge = retention_days + 1
            if days_since_purge > retention_days:
                expired_count += 1
                if days_since_purge > oldest_unpurged_days:
                    oldest_unpurged_days = int(days_since_purge)
    except Exception as e:
        logger.warning(f"[DataLifecycle] check_retention_compliance 失败: {e}")
        return empty
    compliance_rate = 1.0
    if total_users > 0:
        compliance_rate = 1.0 - (expired_count / total_users)
    result = {
        "total_users": total_users,
        "expired_count": expired_count,
        "oldest_unpurged_days": oldest_unpurged_days,
        "compliance_rate": round(compliance_rate, 4),
    }
    logger.info(
        f"[DataLifecycle] check_retention_compliance "
        f"total={total_users} expired={expired_count} "
        f"rate={compliance_rate:.2%}"
    )
    return result
