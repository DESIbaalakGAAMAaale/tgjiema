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
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.error_codes import AppError, ErrorCodes
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
        return False
    # R59 P1: fail-closed 检查 — 旧 break_glass 数据 expires_at 为空时拒绝
    # 防止无过期时间的旧审批被无限复用(安全风险)
    # 仅检查 break_glass 类型(quarantine_delete 由 redis_queue.quarantine_repair 独立校验)
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
) -> None:
    """R58 P0-2: 校验 break-glass 双人审批已持久化且满足强绑定约束。

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
    - 验证通过后在**同一事务**中原子设置 consumed_at(受影响行数必须恰好为 2,否则回滚)
    - permission 必须非空(R58 已存储,这里强制校验;执行时由 claim_execution_approved 重鉴权)

    Args:
        action_id: 审批动作 ID
        expected_principal_id: 发起人 principal_id(用于确认非自审批)
        expected_request_hash: 期望的 request_hash(64 hex),用于验证两人批准同一请求

    Raises:
        AppError(DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED): 双人审批不足/MFA 缺失/自审批/Hash 不匹配/已过期/已撤销/已消费/原子消费失败
    """
    import re as _re_mod
    # R60 P0-01: 不再导入 consume_mfa_receipt(其内部自行 commit,无法纳入单事务)
    # jti 一次性消费的 CAS 逻辑直接内联到下方 BEGIN IMMEDIATE 事务中
    from admin.mfa import verify_mfa_receipt

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
    jti_list: list[str] = []
    for r in rows:
        approver_id = int(r[0] or 0)
        mfa_receipt = str(r[1] or "")
        try:
            receipt_payload = verify_mfa_receipt(
                token=mfa_receipt,
                expected_principal_id=approver_id,
                expected_purpose=expected_purpose,
                expected_action_hash=canonical_request_hash,
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
    # R60 P0-01: 原子消费 — mfa_receipts(jti 一次性) + command_approvals(consumed_at)
    # 必须在同一 SQLite 连接、同一显式事务中完成。
    # 禁止旧的 "commit → rowcount → rollback" 错误顺序;必须 "rowcount → 条件检查 → commit"。
    # BEGIN IMMEDIATE 立即获取 RESERVED 锁,串行化并发写者(避免两个写者同时进入临界区)。
    consumed_at_now = now_utc.isoformat()
    now_unix = int(now_utc.timestamp())
    # R61 P0-02: 修复 BEGIN IMMEDIATE catch-all 误"复用现有事务"问题。
    # 旧实现把所有 BEGIN IMMEDIATE 异常(锁竞争/连接损坏/I/O 错误)当作"已处于事务中,复用",
    # 随后无条件 COMMIT,会擅自提交调用方事务。
    # 现改为通过 `store._db.in_transaction` 精确判断:
    # - 不在事务中 → BEGIN IMMEDIATE(串行化并发写者);失败抛 AppError(不再静默复用)
    # - 已在外层事务中 → SAVEPOINT approval_consume 隔离(退出时 RELEASE/ROLLBACK TO,不提交调用方事务)
    owns_transaction = not bool(getattr(store._db, "in_transaction", False))
    _APPROVAL_SAVEPOINT = "approval_consume"
    if owns_transaction:
        try:
            await store._db.execute("BEGIN IMMEDIATE")
        except Exception as begin_err:
            # BEGIN IMMEDIATE 失败(锁竞争/连接损坏/I/O 错误)→ 显式失败,
            # 不再像旧实现那样静默"复用现有事务"并随后擅自 COMMIT 调用方事务。
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": f"begin_immediate_failed: {type(begin_err).__name__}: {begin_err}",
                    "action_id": action_id,
                },
            ) from begin_err
    else:
        # 已在外层事务中: 用 SAVEPOINT 隔离本消费操作,不擅自 COMMIT 调用方事务。
        await store._db.execute(f"SAVEPOINT {_APPROVAL_SAVEPOINT}")
    try:
        # 1. 确保 mfa_receipts 表存在(幂等 DDL,与 admin.mfa.consume_mfa_receipt 一致)
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
        # 2. 对两个 jti 执行条件 INSERT(INSERT OR IGNORE + rowcount)
        # rowcount=1 → 首次消费成功;rowcount=0 → jti 已存在(重放/已消费)
        # 两个 jti 的 rowcount 之和必须恰好为 2(均首次消费,任一已消费即重放)
        receipt_affected = 0
        for jti in jti_list:
            cursor = await store._db.execute(
                "INSERT OR IGNORE INTO mfa_receipts (jti, used_at, consumed_at) "
                "VALUES (?, ?, ?)",
                (jti, now_unix, now_unix),
            )
            receipt_affected += cursor.rowcount if cursor is not None else 0
        # 3. 对两条审批行执行条件 UPDATE(CAS: consumed_at IS NULL)
        # 受影响行数必须恰好为 2(两条审批都被原子消费)
        # 若 < 2: 验证过程中有记录被并发消费/撤销/过期
        # 若 > 2: 数据库约束异常(不该发生)
        cursor = await store._db.execute(
            "UPDATE command_approvals "
            "SET consumed_at = ? "
            "WHERE action_id = ? "
            "  AND approval_type = 'break_glass' "
            "  AND consumed_at IS NULL "
            "  AND revoked_at IS NULL "
            "  AND expires_at > ?",
            (consumed_at_now, action_id, consumed_at_now),
        )
        approval_affected = cursor.rowcount if cursor is not None else 0
        # 4. 检查两侧 rowcount 均恰好为 2,任何不一致 → 回滚到事务/savepoint 起点
        # receipt_affected != 2: 某 jti 已被消费(重放)或消费失败
        # approval_affected != 2: 审批被并发消费/撤销/过期
        if receipt_affected != 2 or approval_affected != 2:
            # R61 P0-02: owns_transaction → ROLLBACK 整个事务;
            #   否则 ROLLBACK TO SAVEPOINT(保留调用方事务由其自行回滚/提交)
            try:
                if owns_transaction:
                    await store._db.rollback()
                else:
                    await store._db.execute(
                        f"ROLLBACK TO SAVEPOINT {_APPROVAL_SAVEPOINT}"
                    )
            except Exception as rollback_err:
                logger.warning(
                    f"[DataLifecycle] R60 P0-01 rollback 失败 "
                    f"action_id={action_id}: {rollback_err}"
                )
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "atomic_consume_failed",
                    "expected_receipt_affected": 2,
                    "actual_receipt_affected": receipt_affected,
                    "expected_approval_affected": 2,
                    "actual_approval_affected": approval_affected,
                    "action_id": action_id,
                },
            )
        # 5. 两侧 CAS 均成功 → 提交(唯一提交点,在所有 rowcount 检查之后)
        # R61 P0-02: owns_transaction=True → COMMIT 整个事务;
        #   owns_transaction=False → RELEASE SAVEPOINT(不提交调用方事务,仅提交本 savepoint 内的更改)
        if owns_transaction:
            await store._db.commit()
        else:
            await store._db.execute(f"RELEASE SAVEPOINT {_APPROVAL_SAVEPOINT}")
        logger.info(
            f"[DataLifecycle] R60 P0-01: 双人审批原子消费成功 "
            f"action_id={action_id} receipt_affected={receipt_affected} "
            f"approval_affected={approval_affected}"
        )
    except AppError:
        raise
    except Exception as e:
        # R61 P0-02: owns_transaction → ROLLBACK 整个事务;
        #   否则 ROLLBACK TO SAVEPOINT(保留调用方事务)
        try:
            if owns_transaction:
                await store._db.rollback()
            else:
                await store._db.execute(
                    f"ROLLBACK TO SAVEPOINT {_APPROVAL_SAVEPOINT}"
                )
        except Exception as rollback_err:
            # rollback 失败不掩盖原异常,仅记录(原异常已在上层抛出)
            logger.warning(
                f"[DataLifecycle] R60 P0-01 rollback 失败 "
                f"action_id={action_id}: {rollback_err}"
            )
        raise AppError(
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
            params={
                "reason": f"atomic_consume_exception: {type(e).__name__}: {e}",
                "action_id": action_id,
            },
        ) from e


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
    """R51 P1-1 + R55 P0-3/P0-6: 清理过期数据。

    R55 P0-3: break-glass 必须绑定 principal_id、request_hash,
    验证审批 action_type=data_lifecycle_break_glass,审批不可跨请求复用。

    R55 P0-6: 审计日志写入纳入删除事务,审批状态回写失败不能返回成功。
    """
    if not await _ensure_retention_table():
        return 0
    store = get_cache_store()
    if not store._db:
        return 0
    # R55 P0-3: break-glass 必须绑定 principal_id、request_hash
    # 验证审批 action_type=data_lifecycle_break_glass,审批不可跨请求复用
    _break_glass_claimed = False
    if skip_backup_check:
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
        #           同时校验 command_executions.principal_id 与传入的 principal_id 一致,
        #           并校验双人审批已持久化到 command_approvals 表
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
        # R56 P0-2: 校验双人审批已持久化(≥2 approver + MFA receipt + 非自审批)
        await _verify_break_glass_two_person_approval(
            action_id=approval_action_id,
            expected_principal_id=principal_id,
        )
        import socket as _socket
        _owner = f"{_socket.gethostname()}:{os.getpid()}"
        try:
            claimed = await claim_execution_approved(
                action_id=approval_action_id,
                owner=_owner,
                request_hash=request_hash,
            )
        except AppError:
            raise
        if not claimed:
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={"reason": "break_glass_approval_cas_failed_or_not_approved"},
            )
        _break_glass_claimed = True
    # R53 P1-3: 物理删除前强制严格 backup marker 校验
    # require_user_scope=True: 校验用户覆盖范围(全库备份用 user_coverage)
    # require_checksum=True: 校验 manifest checksum(完整性绑定)
    backup_info: dict | None = None
    if not skip_backup_check:
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
    try:
        cursor = await store._db.execute(
            f"SELECT user_id, retention_days FROM {_RETENTION_TABLE} "
            f"WHERE retention_days > 0 LIMIT ?",
            (batch_size,),
        )
        rows = await cursor.fetchall()
    except Exception as e:
        logger.warning(f"[DataLifecycle] cleanup 拉取保留期配置失败: {e}")
        return 0
    for r in rows:
        user_id, retention_days = r[0], int(r[1])
        cutoff_dt = now_dt - _dt.timedelta(days=retention_days)
        cutoff_iso = cutoff_dt.isoformat()
        # R53 P1-3: 校验该用户在 backup marker 的 user_coverage 中
        # 批量全库备份使用 manifest 中的 user_coverage,而非单一 user_id 字段
        if not skip_backup_check and backup_info is not None:
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
        if not skip_backup_check and backup_info is not None:
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
    # R54 P0-3: break-glass 审批成功后标记 executed
    # R56 P0-3: committed_delta 模式 — 删除计数作为 result 传给 mark_approved_executed,
    #           命令状态回写失败必须 raise(不再只 log 后返回 total_cleaned,
    #           否则会出现"数据已删但命令状态仍 executing"的不一致)
    if _break_glass_claimed and approval_action_id:
        try:
            _executed_ok = await mark_approved_executed(
                action_id=approval_action_id,
                result={"total_cleaned": total_cleaned},
            )
        except Exception as mark_err:
            # R56 P0-3: mark_approved_executed 抛异常时,删除已提交无法回滚,
            # 必须向上传播让调用方进入 reconciliation(不可静默返回成功)
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "mark_approved_executed_failed_after_delete",
                    "action_id": approval_action_id,
                    "total_cleaned": total_cleaned,
                    "error": f"{type(mark_err).__name__}: {mark_err}",
                },
            ) from mark_err
        if not _executed_ok:
            # R56 P0-3: CAS 未命中(状态非 executing)也视为不一致,raise
            raise AppError(
                ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
                params={
                    "reason": "mark_approved_executed_cas_missed",
                    "action_id": approval_action_id,
                    "total_cleaned": total_cleaned,
                },
            )
    logger.info(
        f"[DataLifecycle] cleanup_expired_data 清理 {total_cleaned} 行 "
        f"(users_checked={len(rows)})"
    )
    return total_cleaned


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
