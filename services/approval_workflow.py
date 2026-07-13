"""R40 §9.2: 审批工作流 — 高风险操作二次确认。

职责:
- create_approval(): 创建审批请求(高风险操作发起后,需等待审批人确认)
- approve(): 批准审批(检查审批人权限 + 不能审批自己创建的请求)
- reject(): 驳回审批
- cancel(): 取消审批(仅创建者可取消)
- 查询: get_approval / list_pending / list_by_action

高风险操作:
- takedown: 内容下架
- ban: 封禁用户
- restore: 恢复删除
- config_change: 配置变更
- delete_data: 删除数据
- factory_reset: 恢复出厂设置

数据表:
- approvals: id/action/payload(JSON)/status/approver_id/approver_note/created_by/created_at/resolved_at
- audit_log: 审批操作审计记录

设计要点:
- 审批人必须拥有 PERMISSION_APPROVE_TAKEDOWN 权限(通过 rbac.check_permission 检查)
- 审批人不能审批自己创建的请求(created_by != approver_id)
- 所有审批操作写入 audit_log
- 纯函数式 + async,所有写入后调用 add_dirty_outbox()
"""
from __future__ import annotations

import datetime
import json
from loguru import logger

from database.cache_store import get_cache_store
from services.rbac import check_permission, PERMISSION_APPROVE_TAKEDOWN


# ─── 需要审批的操作 ────────────────────────────────────────────
APPROVAL_ACTION_TAKEDOWN = "takedown"              # 内容下架
APPROVAL_ACTION_BAN = "ban"                         # 封禁用户
APPROVAL_ACTION_RESTORE = "restore"                 # 恢复删除
APPROVAL_ACTION_CONFIG_CHANGE = "config_change"     # 配置变更
APPROVAL_ACTION_DELETE_DATA = "delete_data"          # 删除数据
APPROVAL_ACTION_FACTORY_RESET = "factory_reset"     # 恢复出厂设置
# R40 P0-8: 新增高风险 action(与 command_bus 中常量一致)
APPROVAL_ACTION_MAINTENANCE_ENABLE = "maintenance_enable"     # 开启维护模式
APPROVAL_ACTION_MAINTENANCE_DISABLE = "maintenance_disable"   # 关闭维护模式
APPROVAL_ACTION_RBAC_ASSIGN = "rbac_assign"                  # 分配角色

# ─── 审批状态 ──────────────────────────────────────────────────
# R40 P0-8: 状态机扩展为 PENDING → APPROVED → EXECUTING → EXECUTED/FAILED
APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
APPROVAL_STATUS_CANCELLED = "cancelled"
APPROVAL_STATUS_EXECUTING = "executing"   # R40 P0-8: 命令正在执行
APPROVAL_STATUS_EXECUTED = "executed"      # R40 P0-8: 命令执行成功
APPROVAL_STATUS_FAILED = "failed"           # R40 P0-8: 命令执行失败

# ─── 需要审批的操作集合 ────────────────────────────────────────
_ACTIONS_REQUIRING_APPROVAL = {
    APPROVAL_ACTION_TAKEDOWN,
    APPROVAL_ACTION_BAN,
    APPROVAL_ACTION_RESTORE,
    APPROVAL_ACTION_CONFIG_CHANGE,
    APPROVAL_ACTION_DELETE_DATA,
    APPROVAL_ACTION_FACTORY_RESET,
    APPROVAL_ACTION_MAINTENANCE_ENABLE,
    APPROVAL_ACTION_MAINTENANCE_DISABLE,
    APPROVAL_ACTION_RBAC_ASSIGN,
}

# ─── 操作中文描述(用于格式化展示) ─────────────────────────────
_ACTION_LABELS = {
    APPROVAL_ACTION_TAKEDOWN: "内容下架",
    APPROVAL_ACTION_BAN: "封禁用户",
    APPROVAL_ACTION_RESTORE: "恢复删除",
    APPROVAL_ACTION_CONFIG_CHANGE: "配置变更",
    APPROVAL_ACTION_DELETE_DATA: "删除数据",
    APPROVAL_ACTION_FACTORY_RESET: "恢复出厂设置",
    APPROVAL_ACTION_MAINTENANCE_ENABLE: "开启维护模式",
    APPROVAL_ACTION_MAINTENANCE_DISABLE: "关闭维护模式",
    APPROVAL_ACTION_RBAC_ASSIGN: "分配角色",
}


async def create_approval(action: str, payload: dict, created_by: int) -> int:
    """创建审批请求,返回 approval_id。

    Args:
        action: 操作类型(必须是 _ACTIONS_REQUIRING_APPROVAL 中的值)
        payload: 操作参数(如 {"target_user_id": 123, "reason": "违规"})
        created_by: 创建者 ID(发起审批的管理员)

    Returns:
        approval_id(>0 表示成功); -1 表示失败
    """
    if action not in _ACTIONS_REQUIRING_APPROVAL:
        logger.warning(f"[Approval] create_approval 未知操作类型: {action}")
        return -1

    store = get_cache_store()
    if not store._db:
        logger.warning("[Approval] create_approval 数据库未初始化")
        return -1

    now = datetime.datetime.now().isoformat()

    try:
        payload_str = json.dumps(payload, ensure_ascii=False, default=str)
        # R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "INSERT INTO approvals (action, payload, status, approver_id, approver_note, created_by, created_at, resolved_at) "
                "VALUES (?, ?, 'pending', NULL, '', ?, ?, NULL)",
                (action, payload_str, created_by, now),
            )
            approval_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0

            if approval_id > 0:
                await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

                # 写审计日志(同事务)
                await tx.execute(
                    "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                    "VALUES (?, 'admin', 'create_approval', 'approval', ?, ?, ?)",
                    (
                        created_by,
                        str(approval_id),
                        json.dumps({"action": action, "payload": payload}, ensure_ascii=False, default=str),
                        now,
                    ),
                )
                await store.add_dirty_outbox("audit_log", "last", connection=tx)

        logger.info(f"[Approval] create_approval 创建审批 id={approval_id} action={action} by={created_by}")
        return approval_id
    except Exception as e:
        logger.error(f"[Approval] create_approval 失败 action={action}: {e}")
        return -1


async def approve(approval_id: int, approver_id: int, note: str = "") -> bool:
    """批准审批。

    检查:
    - 审批人是否有 PERMISSION_APPROVE_TAKEDOWN 权限
    - 不能审批自己创建的请求(created_by != approver_id)
    - 审批状态必须为 pending

    Args:
        approval_id: 审批 ID
        approver_id: 审批人 ID
        note: 审批备注

    Returns:
        True 表示成功
    """
    approval = await get_approval(approval_id)
    if approval is None:
        logger.warning(f"[Approval] approve 审批不存在: {approval_id}")
        return False

    if approval["status"] != APPROVAL_STATUS_PENDING:
        logger.warning(f"[Approval] approve 审批状态非 pending: {approval_id} status={approval['status']}")
        return False

    # 检查审批人权限
    has_perm = await check_permission(approver_id, PERMISSION_APPROVE_TAKEDOWN)
    if not has_perm:
        logger.warning(f"[Approval] approve 审批人无权限: approver={approver_id} approval={approval_id}")
        return False

    # 不能审批自己创建的请求
    if int(approval["created_by"]) == approver_id:
        logger.warning(f"[Approval] approve 不能审批自己创建的请求: approver={approver_id} created_by={approval['created_by']}")
        return False

    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()

    try:
        # R40 P1-4: CAS UPDATE — 单条 UPDATE 同时完成状态判定与状态变更,
        # 通过 rowcount 检测并发竞争(0=已被处理或不存在,1=本调用成功)。
        # R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = 'approved', approver_id = ?, approver_note = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (approver_id, note, now, approval_id),
            )
            affected = cursor.rowcount if cursor else 0
            if affected == 0:
                # CAS 失败:审批已被其他审批人处理或不存在
                logger.warning(
                    f"[Approval] approve CAS UPDATE 未命中(id={approval_id}): "
                    f"已被处理或不存在"
                )
                return False
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

            # 写审计日志(同事务)
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'admin', 'approve', 'approval', ?, ?, ?)",
                (
                    approver_id,
                    str(approval_id),
                    json.dumps({"note": note, "action_type": approval["action"]}, ensure_ascii=False),
                    now,
                ),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        logger.info(f"[Approval] approve 审批已批准 id={approval_id} approver={approver_id}")

        # R41 P0-4: 审批通过后写入 command_outbox(独立事务),由 ApprovalExecutor 异步消费。
        # 不再在 approve() 中直接调用 cb.execute_approved_action() —— 后者会调用
        # mark_executing() 开启 store.transaction(),与 approve 的事务上下文可能嵌套
        # (SQLite 不支持 BEGIN 嵌套,抛 "cannot start a transaction within a transaction")。
        # 通过 command_outbox 解耦:approve 只做 CAS PENDING → APPROVED + 审计 + 写入 outbox,
        # 真正的 handler 执行由 ApprovalExecutor 在独立事务中完成,支持重试与失败隔离。
        payload_data = approval.get("payload", {}) or {}
        if isinstance(payload_data, str):
            try:
                payload_data = json.loads(payload_data)
            except (json.JSONDecodeError, TypeError):
                payload_data = {}
        if payload_data.get("command_action"):
            await _enqueue_command_outbox(
                approval_id=approval_id,
                action_id=payload_data.get("action_id", ""),
                command_type=payload_data.get("command_action", ""),
                payload=payload_data,
            )
        return True
    except Exception as e:
        logger.error(f"[Approval] approve 失败 id={approval_id}: {e}")
        return False


async def _enqueue_command_outbox(
    approval_id: int,
    action_id: str,
    command_type: str,
    payload: dict,
) -> bool:
    """R41 P0-4: 审批通过后写入 command_outbox(独立事务)。

    ApprovalExecutor 会异步消费此表,调用 CommandBus.execute_command_outbox_entry()
    执行实际 handler,完全独立的事务,避免嵌套 BEGIN。

    Args:
        approval_id: 关联审批 ID
        action_id: 幂等 action_id(从 approval payload 中提取)
        command_type: 命令类型(如 "takedown_report" / "ban_user")
        payload: 完整 payload(含 command_action / params / principal_* 等)

    Returns:
        True 表示写入成功;False 表示失败(审批状态已 APPROVED,可通过手动重试补救)
    """
    store = get_cache_store()
    if not store._db:
        logger.warning(
            f"[Approval] _enqueue_command_outbox 数据库未初始化 approval_id={approval_id}"
        )
        return False

    now = datetime.datetime.now().isoformat()
    if not action_id:
        # 兜底:action_id 缺失时使用 approval_id 作为唯一标识
        action_id = f"approval_{approval_id}_{now}"
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)

    try:
        async with store.transaction() as tx:
            await tx.execute(
                "INSERT INTO command_outbox "
                "(action_id, approval_id, command_type, payload, status, "
                " retry_count, max_retries, next_retry_at, last_error, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, 3, NULL, NULL, ?, ?)",
                (action_id, approval_id, command_type, payload_str, now, now),
            )
        logger.info(
            f"[Approval] command_outbox 已写入 approval_id={approval_id} "
            f"action_id={action_id} command_type={command_type}"
        )
        return True
    except Exception as e:
        # 写入失败不影响审批已批准的状态(APPROVED 已落库),
        # 可通过手动调用 CommandBus.execute_approved_action(approval_id) 兜底执行
        logger.error(
            f"[Approval] command_outbox 写入失败 approval_id={approval_id}: {e}"
        )
        return False


async def reject(approval_id: int, approver_id: int, reason: str = "") -> bool:
    """驳回审批。

    检查:
    - 审批人是否有 PERMISSION_APPROVE_TAKEDOWN 权限
    - 审批状态必须为 pending

    Args:
        approval_id: 审批 ID
        approver_id: 审批人 ID
        reason: 驳回原因

    Returns:
        True 表示成功
    """
    approval = await get_approval(approval_id)
    if approval is None:
        logger.warning(f"[Approval] reject 审批不存在: {approval_id}")
        return False

    if approval["status"] != APPROVAL_STATUS_PENDING:
        logger.warning(f"[Approval] reject 审批状态非 pending: {approval_id} status={approval['status']}")
        return False

    # 检查审批人权限
    has_perm = await check_permission(approver_id, PERMISSION_APPROVE_TAKEDOWN)
    if not has_perm:
        logger.warning(f"[Approval] reject 审批人无权限: approver={approver_id} approval={approval_id}")
        return False

    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()

    try:
        # R40 P1-4: CAS UPDATE — 通过 rowcount 检测并发竞争
        # R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = 'rejected', approver_id = ?, approver_note = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (approver_id, reason, now, approval_id),
            )
            affected = cursor.rowcount if cursor else 0
            if affected == 0:
                # CAS 失败:审批已被其他审批人处理或不存在
                logger.warning(
                    f"[Approval] reject CAS UPDATE 未命中(id={approval_id}): "
                    f"已被处理或不存在"
                )
                return False
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

            # 写审计日志(同事务)
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'admin', 'reject', 'approval', ?, ?, ?)",
                (
                    approver_id,
                    str(approval_id),
                    json.dumps({"reason": reason, "action_type": approval["action"]}, ensure_ascii=False),
                    now,
                ),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        logger.info(f"[Approval] reject 审批已驳回 id={approval_id} approver={approver_id}")
        return True
    except Exception as e:
        logger.error(f"[Approval] reject 失败 id={approval_id}: {e}")
        return False


async def cancel(approval_id: int, user_id: int) -> bool:
    """取消审批(仅创建者可取消)。

    Args:
        approval_id: 审批 ID
        user_id: 取消人 ID(必须为创建者)

    Returns:
        True 表示成功
    """
    approval = await get_approval(approval_id)
    if approval is None:
        logger.warning(f"[Approval] cancel 审批不存在: {approval_id}")
        return False

    if approval["status"] != APPROVAL_STATUS_PENDING:
        logger.warning(f"[Approval] cancel 审批状态非 pending: {approval_id} status={approval['status']}")
        return False

    # 仅创建者可取消
    if int(approval["created_by"]) != user_id:
        logger.warning(f"[Approval] cancel 非创建者无法取消: user={user_id} created_by={approval['created_by']}")
        return False

    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()

    try:
        # R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "UPDATE approvals SET status = 'cancelled', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now, approval_id),
            )
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

            # 写审计日志(同事务)
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'admin', 'cancel_approval', 'approval', ?, ?, ?)",
                (
                    user_id,
                    str(approval_id),
                    json.dumps({"action_type": approval["action"]}, ensure_ascii=False),
                    now,
                ),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        logger.info(f"[Approval] cancel 审批已取消 id={approval_id} user={user_id}")
        return True
    except Exception as e:
        logger.error(f"[Approval] cancel 失败 id={approval_id}: {e}")
        return False


# ─── R40 P0-8: 命令执行状态管理 ─────────────────────────────────


async def mark_executing(approval_id: int) -> bool:
    """R40 P0-8: 将审批状态从 APPROVED 标记为 EXECUTING(命令开始执行)。

    仅当当前状态为 approved 时才允许转换,防止重复执行。

    Args:
        approval_id: 审批 ID

    Returns:
        True 表示标记成功;False 表示状态不符或失败
    """
    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status = ?",
                (APPROVAL_STATUS_EXECUTING, now, approval_id, APPROVAL_STATUS_APPROVED),
            )
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)
            # EXECUTING 状态不写 audit_log(执行结果在 mark_executed/mark_failed 中记录)
        affected = cursor.rowcount if cursor else 0
        if affected == 0:
            logger.warning(
                f"[Approval] mark_executing 未更新(状态非 approved 或不存在): id={approval_id}"
            )
            return False
        logger.info(f"[Approval] mark_executing id={approval_id} 状态 → EXECUTING")
        return True
    except Exception as e:
        logger.error(f"[Approval] mark_executing 失败 id={approval_id}: {e}")
        return False


async def mark_executed(approval_id: int) -> bool:
    """R40 P0-8: 将审批状态从 EXECUTING 标记为 EXECUTED(命令执行成功)。

    Args:
        approval_id: 审批 ID

    Returns:
        True 表示标记成功;False 表示状态不符或失败
    """
    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status = ?",
                (APPROVAL_STATUS_EXECUTED, now, approval_id, APPROVAL_STATUS_EXECUTING),
            )
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

            # 写审计日志
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'system', 'mark_executed', 'approval', ?, ?, ?)",
                (0, str(approval_id), json.dumps({"status": "executed"}, ensure_ascii=False), now),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        affected = cursor.rowcount if cursor else 0
        if affected == 0:
            logger.warning(
                f"[Approval] mark_executed 未更新(状态非 executing 或不存在): id={approval_id}"
            )
            return False
        logger.info(f"[Approval] mark_executed id={approval_id} 状态 → EXECUTED")
        return True
    except Exception as e:
        logger.error(f"[Approval] mark_executed 失败 id={approval_id}: {e}")
        return False


async def mark_failed(approval_id: int, error: str = "") -> bool:
    """R40 P0-8: 将审批状态从 EXECUTING 标记为 FAILED(命令执行失败)。

    错误信息写入 approver_note 字段(前缀 "ERROR: ")。

    Args:
        approval_id: 审批 ID
        error: 错误信息

    Returns:
        True 表示标记成功;False 表示状态不符或失败
    """
    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()
    error_note = f"ERROR: {error}"[:500] if error else "ERROR: unknown"
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = ?, approver_note = ?, resolved_at = ? "
                "WHERE id = ? AND status = ?",
                (APPROVAL_STATUS_FAILED, error_note, now, approval_id, APPROVAL_STATUS_EXECUTING),
            )
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)

            # 写审计日志
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'system', 'mark_failed', 'approval', ?, ?, ?)",
                (0, str(approval_id),
                 json.dumps({"status": "failed", "error": error}, ensure_ascii=False), now),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        affected = cursor.rowcount if cursor else 0
        if affected == 0:
            logger.warning(
                f"[Approval] mark_failed 未更新(状态非 executing 或不存在): id={approval_id}"
            )
            return False
        logger.warning(
            f"[Approval] mark_failed id={approval_id} 状态 → FAILED error={error[:200]}"
        )
        return True
    except Exception as e:
        logger.error(f"[Approval] mark_failed 失败 id={approval_id}: {e}")
        return False


async def mark_approval_executed(approval_id: int, action_id: str = "") -> bool:
    """R42 P0-2: 标记审批为已执行(基于 approval_id,可选 action_id 用于审计)。

    与 ``mark_executed`` 的差异:
        - ``mark_executed`` 要求当前状态为 EXECUTING(状态机严格转换)
        - 本函数直接 UPDATE 到 EXECUTED(用于 ApprovalExecutor 同事务内幂等标记,
          已通过 ``mark_outbox_executed`` 完成主流程,此处仅作冗余更新兜底)

    Args:
        approval_id: 审批 ID
        action_id: 关联的命令幂等 ID(仅用于日志审计,不参与 WHERE 过滤)

    Returns:
        True 表示更新成功;False 表示未更新(记录不存在或已为 executed)
    """
    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status != ?",
                (APPROVAL_STATUS_EXECUTED, now, approval_id, APPROVAL_STATUS_EXECUTED),
            )
            await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)
        affected = cursor.rowcount if cursor else 0
        if affected == 0:
            logger.debug(
                f"[Approval] mark_approval_executed 未更新(已 executed 或不存在): "
                f"id={approval_id} action_id={action_id}"
            )
            return False
        logger.info(
            f"[Approval] mark_approval_executed id={approval_id} "
            f"action_id={action_id} → EXECUTED"
        )
        return True
    except Exception as e:
        logger.error(
            f"[Approval] mark_approval_executed 失败 id={approval_id} "
            f"action_id={action_id}: {e}"
        )
        return False


async def get_approval(approval_id: int) -> dict | None:
    """获取审批详情。

    Args:
        approval_id: 审批 ID

    Returns:
        审批详情字典;不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None

    try:
        rows = await store._db.execute_fetchall(
            "SELECT id, action, payload, status, approver_id, approver_note, "
            "created_by, created_at, resolved_at "
            "FROM approvals WHERE id = ?",
            (approval_id,),
        )
        if not rows:
            return None
        r = rows[0]
        # 解析 payload JSON
        payload_str = r[2] or "{}"
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
        except (json.JSONDecodeError, TypeError):
            payload = {}

        return {
            "id": r[0],
            "action": r[1],
            "payload": payload,
            "status": r[3],
            "approver_id": r[4],
            "approver_note": r[5] or "",
            "created_by": r[6],
            "created_at": r[7],
            "resolved_at": r[8],
        }
    except Exception as e:
        logger.warning(f"[Approval] get_approval 失败 id={approval_id}: {e}")
        return None


async def list_pending(page: int = 1, page_size: int = 20) -> dict:
    """分页列出待审批请求。

    Args:
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": int, "page": int, "page_size": int}
    """
    return await _list_by_status(APPROVAL_STATUS_PENDING, page, page_size)


async def list_by_action(action: str, page: int = 1, page_size: int = 20) -> dict:
    """按操作类型查询审批历史。

    Args:
        action: 操作类型
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": int, "page": int, "page_size": int}
    """
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    offset = max(0, (page - 1) * page_size)

    try:
        rows = await store._db.execute_fetchall(
            "SELECT id, action, payload, status, approver_id, approver_note, "
            "created_by, created_at, resolved_at "
            "FROM approvals WHERE action = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (action, page_size, offset),
        )
        items = [_row_to_dict(r) for r in rows]

        count_rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM approvals WHERE action = ?",
            (action,),
        )
        total = int(count_rows[0][0]) if count_rows else 0

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.warning(f"[Approval] list_by_action 失败 action={action}: {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def requires_approval(action: str) -> bool:
    """检查操作是否需要审批。

    Args:
        action: 操作类型

    Returns:
        True 表示需要审批
    """
    return action in _ACTIONS_REQUIRING_APPROVAL


async def format_approval(approval: dict) -> str:
    """格式化审批为管理员可读文本。

    Args:
        approval: 审批详情字典(来自 get_approval / list_pending)

    Returns:
        格式化的审批信息字符串
    """
    approval_id = approval.get("id", "?")
    action = approval.get("action", "未知")
    action_label = _ACTION_LABELS.get(action, action)
    status = approval.get("status", "未知")
    created_by = approval.get("created_by", "?")
    created_at = approval.get("created_at", "?")
    approver_id = approval.get("approver_id")
    approver_note = approval.get("approver_note", "")
    resolved_at = approval.get("resolved_at", "")
    payload = approval.get("payload", {})

    # 状态中文映射
    status_labels = {
        APPROVAL_STATUS_PENDING: "⏳ 待审批",
        APPROVAL_STATUS_APPROVED: "✅ 已批准",
        APPROVAL_STATUS_REJECTED: "❌ 已驳回",
        APPROVAL_STATUS_CANCELLED: "🚫 已取消",
        APPROVAL_STATUS_EXECUTING: "🔄 执行中",
        APPROVAL_STATUS_EXECUTED: "✅ 已执行",
        APPROVAL_STATUS_FAILED: "⚠️ 执行失败",
    }
    status_text = status_labels.get(status, status)

    lines = [
        f"审批 ID: {approval_id}",
        f"操作类型: {action_label} ({action})",
        f"状态: {status_text}",
        f"发起人: {created_by}",
        f"创建时间: {created_at}",
    ]

    if payload:
        lines.append(f"参数: {json.dumps(payload, ensure_ascii=False, default=str)}")

    if approver_id is not None:
        lines.append(f"审批人: {approver_id}")
    if approver_note:
        lines.append(f"审批备注: {approver_note}")
    if resolved_at:
        lines.append(f"处理时间: {resolved_at}")

    return "\n".join(lines)


# ─── 内部辅助函数 ──────────────────────────────────────────────

def _row_to_dict(r) -> dict:
    """将数据库行转换为字典(与 get_approval 返回格式一致)。"""
    payload_str = r[2] or "{}"
    try:
        payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "id": r[0],
        "action": r[1],
        "payload": payload,
        "status": r[3],
        "approver_id": r[4],
        "approver_note": r[5] or "",
        "created_by": r[6],
        "created_at": r[7],
        "resolved_at": r[8],
    }


async def _list_by_status(status: str, page: int, page_size: int) -> dict:
    """按状态分页查询审批列表(内部辅助函数)。"""
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    offset = max(0, (page - 1) * page_size)

    try:
        rows = await store._db.execute_fetchall(
            "SELECT id, action, payload, status, approver_id, approver_note, "
            "created_by, created_at, resolved_at "
            "FROM approvals WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, page_size, offset),
        )
        items = [_row_to_dict(r) for r in rows]

        count_rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM approvals WHERE status = ?",
            (status,),
        )
        total = int(count_rows[0][0]) if count_rows else 0

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.warning(f"[Approval] _list_by_status 失败 status={status}: {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
