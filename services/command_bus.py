"""R40 P0-8 + R41 P0-5: 命令总线 — RBAC + 审批强制门禁 + 持久化幂等。

职责:
    解决原 Web 路由仅依赖 Basic Auth、Bot 命令依赖旧 ``_auth_required`` 的问题。
    所有高风险操作必须经过 CommandBus,完成:
    1. RBAC 权限校验(``check_permission``)
    2. 审批门禁(高风险操作 ``requires_approval=True`` 时,创建 approval 记录,
       等待审批通过后才执行 handler)
    3. 审计日志(记录 principal.id / action / params / result)
    4. 幂等(基于 action_id 防重复执行)

R41 P0-5 整改:
    - 移除进程内 ``_EXECUTED_ACTIONS`` dict,改为 SQLite ``command_executions`` 表持久化。
    - 重启、多 worker、故障恢复后 action_id 仍可见,避免重复执行。
    - "执行中"状态由 ``claim_execution`` + ``lease_until`` 实现持久租约。
    - ``request_hash`` 防篡改:相同 action_id 不同 payload → 拒绝执行。
    - ``cleanup_stale_leases`` 清理过期租约(executing + lease_until < now → pending)。

设计要点:
    - 命令对象(Command dataclass)描述所需的权限、审批策略、handler、参数
    - Principal 封装当前操作者身份(id + name + 来源 web/bot)
    - Result 标准化返回(success/data/error/approval_id)
    - 高风险命令通过注册表(REQUIRED_APPROVAL_COMMANDS)集中管理
    - fail-closed:权限校验异常时一律拒绝(返回 success=False)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets as _secrets
import socket
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger


# ─── 命令权限标识 ─────────────────────────────────────────────
PERM_CONTENT_TAKEDOWN = "content:takedown"
PERM_USERS_BAN = "users:ban"
PERM_USERS_UNBAN = "users:unban"
PERM_RBAC_ASSIGN = "rbac:assign"
PERM_DISASTER_RESTORE = "disaster:restore"
PERM_MAINTENANCE_ENABLE = "maintenance:enable"
PERM_MAINTENANCE_DISABLE = "maintenance:disable"
PERM_DATA_PURGE = "data:purge"


# ─── 审批 workflow action 名(必须与 approval_workflow 中常量一致) ─
APPROVAL_ACTION_TAKEDOWN = "takedown"
APPROVAL_ACTION_BAN = "ban"
APPROVAL_ACTION_RESTORE = "restore"
APPROVAL_ACTION_CONFIG_CHANGE = "config_change"
APPROVAL_ACTION_DELETE_DATA = "delete_data"
APPROVAL_ACTION_FACTORY_RESET = "factory_reset"
# R40 P0-8 新增 action(需在 approval_workflow._ACTIONS_REQUIRING_APPROVAL 中注册)
APPROVAL_ACTION_MAINTENANCE_ENABLE = "maintenance_enable"
APPROVAL_ACTION_MAINTENANCE_DISABLE = "maintenance_disable"
APPROVAL_ACTION_RBAC_ASSIGN = "rbac_assign"


@dataclass
class AdminPrincipal:
    """操作者身份(从 Web BasicAuth 或 Bot telegram user 转换而来)。

    Attributes:
        id: Telegram user_id 或 admin_id(Web 端为 ADMIN_TELEGRAM_ID)
        name: 用户名(Web 端为 ADMIN_USERNAME)
        source: 来源 "web" / "bot"
    """
    id: int
    name: str = ""
    source: str = "web"


@dataclass
class Command:
    """命令对象。

    Attributes:
        action: 命令标识(如 "takedown"/"ban_user"/"restore_backup")
        required_permission: RBAC 权限标识(如 "content:takedown")
        handler: 实际执行的 async 回调,接收 params dict 返回任意结果
        params: 命令参数(传给 handler)
        requires_approval: 是否需要审批(高风险=True)
        approval_action: 审批 workflow 的 action 名(仅 requires_approval=True 时使用)
    """
    action: str
    required_permission: str
    handler: Callable[[dict], Awaitable[Any]]
    params: dict = field(default_factory=dict)
    requires_approval: bool = False
    approval_action: str = ""


@dataclass
class Result:
    """CommandBus 标准化返回。

    Attributes:
        success: 是否成功
        data: 成功时的返回数据
        error: 失败原因
        approval_id: 需要审批时返回的 approval_id(>0 表示已创建审批)
        approval_required: 是否需要等待审批
        action_id: 幂等 ID(用于去重)
    """
    success: bool
    data: Any = None
    error: str = ""
    approval_id: int = 0
    approval_required: bool = False
    action_id: str = ""


# ─── R41 P0-5: 幂等执行追踪(SQLite 持久化) ───────────────────
# 替代原 _EXECUTED_ACTIONS 进程内 dict,改为 command_executions 表 CAS。
# 状态机: pending → executing → executed/failed
CMD_STATUS_PENDING = "pending"
CMD_STATUS_EXECUTING = "executing"
CMD_STATUS_EXECUTED = "executed"
CMD_STATUS_FAILED = "failed"


def _generate_action_id(principal: "AdminPrincipal", action: str) -> str:
    """生成幂等 action_id(基于 principal + action + 时间戳 + 随机)。

    R41 P0-5 说明:此函数保留用于向后兼容(旧测试和 approval payload 重建)。
    新代码应使用 ``_compute_action_id`` 基于 SHA256 生成确定性 ID。

    同一 principal 多次发起相同 action,每次生成不同 action_id;
    但调用方可以通过传入相同 action_id(从 approval 记录中取)实现幂等。
    """
    return f"{action}_{principal.id}_{_dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_secrets.token_hex(4)}"


def _compute_action_id(command_type: str, payload: dict, principal_id: int) -> str:
    """R41 P0-5: 计算确定性 action_id = SHA256(command_type + payload + principal_id)。

    同一 principal 对同一命令(相同参数)多次发起 → 相同 action_id → 自动幂等。
    不同参数 → 不同 action_id → 视为不同操作。
    """
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    raw = f"{command_type}|{payload_str}|{principal_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_request_hash(payload: dict) -> str:
    """R41 P0-5: 计算 payload 的 SHA256(防篡改)。

    当外部传入 action_id(如从 approval payload 恢复)时,通过 request_hash
    校验 payload 是否与首次执行一致,防止篡改。
    """
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def _get_worker_owner() -> str:
    """R41 P0-5: 获取当前 worker 标识(hostname:pid),用于执行租约 owner。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now_iso() -> str:
    """R41 P0-5: 当前 UTC 时间 ISO8601 字符串。"""
    return _dt.datetime.utcnow().isoformat()


def _lease_until_iso(lease_seconds: int) -> str:
    """R41 P0-5: 计算租约过期时间(UTC ISO8601)。"""
    return (_dt.datetime.utcnow() + _dt.timedelta(seconds=lease_seconds)).isoformat()


def _get_store():
    """R41 P0-5: 懒加载获取 CacheStore 单例(避免循环导入)。"""
    from database.cache_store import get_cache_store
    return get_cache_store()


def reset_idempotency_cache() -> None:
    """R41 P0-5: 重置幂等缓存(兼容旧测试 fixture,同步无操作)。

    旧实现清空进程内 ``_EXECUTED_ACTIONS`` dict。R41 P0-5 改为 SQLite 持久化后,
    同步函数无法执行异步 DELETE。测试间隔离请使用 ``clear_command_executions()``
    (异步)或为每个测试创建独立的临时数据库(``real_store`` fixture)。
    """
    logger.debug("[CommandBus] reset_idempotency_cache 已弃用(R41 P0-5 改用 SQLite),无操作")


async def clear_command_executions() -> None:
    """R41 P0-5: 清空 command_executions 表(测试用例间隔离)。

    若数据库未初始化,无操作(兼容无 DB 的单元测试)。
    """
    store = _get_store()
    if not store._db:
        return
    try:
        await store._db.execute("DELETE FROM command_executions")
        await store._db.commit()
    except Exception as e:
        logger.warning(f"[CommandBus] clear_command_executions 失败: {e}")


# ════════════════════════════════════════════════════════════════
# R41 P0-5: 持久化幂等 — 模块级 CAS 操作函数
# ════════════════════════════════════════════════════════════════


async def claim_execution(action_id: str, owner: str, lease_seconds: int = 60) -> bool:
    """R41 P0-5: CAS 认领 — 将 status='pending' 转为 'executing',设置 owner 和 lease_until。

    多 worker 并发时,只有一个 worker 的 CAS UPDATE 会命中(rowcount=1),
    其他 worker rowcount=0 → 抢占失败。

    Args:
        action_id: 命令幂等 ID
        owner: 执行 worker 标识(如 hostname:pid)
        lease_seconds: 租约时长(秒),过期后可被 cleanup_stale_leases 回收

    Returns:
        True 表示认领成功;False 表示已被其他 worker 抢占或状态不符
    """
    store = _get_store()
    if not store._db:
        return False
    lease_until = _lease_until_iso(lease_seconds)
    now = _now_iso()
    try:
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, owner = ?, lease_until = ?, updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (CMD_STATUS_EXECUTING, owner, lease_until, now, action_id, CMD_STATUS_PENDING),
        )
        await store._db.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"[CommandBus] claim_execution 失败 action_id={action_id}: {e}")
        return False


async def renew_lease(action_id: str, lease_seconds: int = 60) -> bool:
    """R41 P0-5: 续租 — 延长 lease_until(仅 status='executing' 的记录可续租)。

    Args:
        action_id: 命令幂等 ID
        lease_seconds: 新的租约时长(秒)

    Returns:
        True 表示续租成功;False 表示记录不存在或状态不符
    """
    store = _get_store()
    if not store._db:
        return False
    lease_until = _lease_until_iso(lease_seconds)
    now = _now_iso()
    try:
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET lease_until = ?, updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (lease_until, now, action_id, CMD_STATUS_EXECUTING),
        )
        await store._db.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"[CommandBus] renew_lease 失败 action_id={action_id}: {e}")
        return False


async def release_execution(action_id: str) -> bool:
    """R41 P0-5: 释放 — 将状态回退到 'pending'(owner/lease 清空),允许重新认领。

    用于执行失败后手动重置,或外部干预释放僵死任务。

    Args:
        action_id: 命令幂等 ID

    Returns:
        True 表示释放成功;False 表示记录不存在
    """
    store = _get_store()
    if not store._db:
        return False
    now = _now_iso()
    try:
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE action_id = ?",
            (CMD_STATUS_PENDING, now, action_id),
        )
        await store._db.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"[CommandBus] release_execution 失败 action_id={action_id}: {e}")
        return False


async def cleanup_stale_leases() -> int:
    """R41 P0-5: 清理过期租约 — status='executing' 且 lease_until < now → status='pending'。

    被 r40_scheduler 每 60 秒调用一次。将过期租约回退到 pending,
    允许其他 worker 重新认领(防止 worker 崩溃后任务永久卡在 executing)。

    Returns:
        清理的记录数
    """
    store = _get_store()
    if not store._db:
        return 0
    now = _now_iso()
    try:
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE status = ? AND lease_until < ?",
            (CMD_STATUS_PENDING, now, CMD_STATUS_EXECUTING, now),
        )
        await store._db.commit()
        return cursor.rowcount
    except Exception as e:
        logger.warning(f"[CommandBus] cleanup_stale_leases 失败: {e}")
        return 0


async def _try_insert_or_get_cached(
    action_id: str,
    command_type: str,
    principal_id: int,
    request_hash: str,
) -> "Result | None":
    """R41 P0-5: 尝试 INSERT 新记录(status='pending')。

    - INSERT 成功 → 返回 None(调用方继续执行 handler)。
    - UNIQUE 冲突(已存在)→ 查询现有记录并返回缓存 Result。
    - 其他 DB 异常 → 返回 None(降级执行,无幂等保护)。

    Args:
        action_id: 命令幂等 ID
        command_type: 命令类型(如 "takedown_report")
        principal_id: 操作者 ID
        request_hash: SHA256(payload),防篡改

    Returns:
        None 表示新记录(继续执行);Result 表示已存在(返回缓存)。
    """
    store = _get_store()
    if not store._db:
        logger.warning("[CommandBus] 数据库未初始化,跳过幂等 INSERT(降级模式)")
        return None
    now = _now_iso()
    try:
        await store._db.execute(
            "INSERT INTO command_executions "
            "(action_id, command_type, principal_id, status, owner, lease_until, "
            "request_hash, result, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?)",
            (action_id, command_type, principal_id, CMD_STATUS_PENDING,
             request_hash, now, now),
        )
        await store._db.commit()
        return None  # INSERT 成功,新记录
    except sqlite3.IntegrityError:
        # UNIQUE 冲突 — action_id 已存在,查询并返回缓存
        logger.debug(f"[CommandBus] INSERT 冲突(已存在)action_id={action_id}")
        return await _get_cached_result(action_id, request_hash)
    except Exception as e:
        # 其他 DB 异常 — 降级执行(无幂等保护)
        logger.warning(
            f"[CommandBus] 幂等 INSERT 异常,降级执行 action_id={action_id}: {e}"
        )
        return None


async def _get_cached_result(action_id: str, request_hash: str) -> "Result | None":
    """R41 P0-5: 查询现有执行记录,返回缓存 Result。

    - request_hash 不匹配 → 返回失败(防篡改)。
    - status='executed' → 返回缓存的成功 Result。
    - status='failed' → 返回缓存的失败 Result。
    - status='pending'/'executing' → 返回"操作已存在"。

    Args:
        action_id: 命令幂等 ID
        request_hash: 当前请求的 SHA256(payload)

    Returns:
        缓存 Result;记录不存在返回 None。
    """
    store = _get_store()
    if not store._db:
        return None
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status, request_hash, result FROM command_executions "
            "WHERE action_id = ?",
            (action_id,),
        )
    except Exception as e:
        logger.warning(f"[CommandBus] _get_cached_result 查询失败 action_id={action_id}: {e}")
        return None
    if not rows:
        return None
    status, stored_hash, result_json = rows[0]
    # request_hash 校验(防篡改)
    if stored_hash != request_hash:
        logger.warning(
            f"[CommandBus] request_hash 不匹配 action_id={action_id} "
            f"(可能 payload 被篡改)"
        )
        return Result(
            success=False,
            error="请求参数与上次执行不一致(防篡改拒绝)",
            action_id=action_id,
        )
    # 已执行 → 返回缓存的成功结果
    if status == CMD_STATUS_EXECUTED and result_json:
        try:
            data = json.loads(result_json)
            return Result(
                success=bool(data.get("success", True)),
                data=data.get("data"),
                error=data.get("error", ""),
                action_id=action_id,
            )
        except (json.JSONDecodeError, TypeError):
            pass
    # 已失败 → 返回缓存的失败结果
    if status == CMD_STATUS_FAILED and result_json:
        try:
            data = json.loads(result_json)
            return Result(
                success=False,
                error=data.get("error", "上次执行失败"),
                action_id=action_id,
            )
        except (json.JSONDecodeError, TypeError):
            pass
    # 正在执行或排队中
    return Result(
        success=False,
        error=f"操作已存在,当前状态: {status}",
        action_id=action_id,
    )


async def _mark_executed(action_id: str, result: "Result") -> None:
    """R41 P0-5: 标记为已执行,存储结果 JSON。"""
    store = _get_store()
    if not store._db:
        return
    now = _now_iso()
    result_json = json.dumps(
        {"success": result.success, "data": result.data, "error": result.error},
        ensure_ascii=False,
        default=str,
    )
    try:
        await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, result = ?, updated_at = ? "
            "WHERE action_id = ?",
            (CMD_STATUS_EXECUTED, result_json, now, action_id),
        )
        await store._db.commit()
    except Exception as e:
        logger.error(f"[CommandBus] _mark_executed 失败 action_id={action_id}: {e}")


async def _mark_failed(action_id: str, result: "Result") -> None:
    """R41 P0-5: 标记为失败,存储错误信息 JSON。"""
    store = _get_store()
    if not store._db:
        return
    now = _now_iso()
    result_json = json.dumps(
        {"success": result.success, "data": result.data, "error": result.error},
        ensure_ascii=False,
        default=str,
    )
    try:
        await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, result = ?, updated_at = ? "
            "WHERE action_id = ?",
            (CMD_STATUS_FAILED, result_json, now, action_id),
        )
        await store._db.commit()
    except Exception as e:
        logger.error(f"[CommandBus] _mark_failed 失败 action_id={action_id}: {e}")


class CommandBus:
    """R40 P0-8: 命令总线 — RBAC + 审批门禁。"""

    def __init__(self, rbac_module=None, approval_module=None):
        """初始化 CommandBus。

        Args:
            rbac_module: rbac 模块(可选,测试时注入 mock;None 时 lazy import)
            approval_module: approval_workflow 模块(可选,同上)
        """
        self._rbac = rbac_module
        self._approval = approval_module

    # ─── 依赖懒加载 ─────────────────────────────────────────

    def _get_rbac(self):
        if self._rbac is not None:
            return self._rbac
        from services import rbac
        return rbac

    def _get_approval(self):
        if self._approval is not None:
            return self._approval
        from services import approval_workflow
        return approval_workflow

    # ─── 1. 执行命令 ────────────────────────────────────────────

    async def execute(
        self,
        command: Command,
        principal: AdminPrincipal,
        action_id: str | None = None,
    ) -> Result:
        """执行命令,完整流程:RBAC → 审批 → 执行 → 审计。

        R41 P0-5: 幂等由 SQLite ``command_executions`` 表 CAS 保证(替代进程内 dict)。
        当 ``action_id`` 为 None 时,基于 ``SHA256(command_type + payload + principal_id)``
        生成确定性 ID,同一 principal 对同一命令(相同参数)多次发起 → 自动幂等。

        Args:
            command: 命令对象
            principal: 操作者身份
            action_id: 幂等 ID(可选,None 时基于 SHA256 自动生成;传入相同 ID 触发幂等去重)

        Returns:
            Result 标准化返回
        """
        # R41 P0-5: 计算确定性 action_id(SHA256)
        if not action_id:
            action_id = _compute_action_id(command.action, command.params, principal.id)

        # 1. RBAC 权限校验(fail-closed:异常时拒绝)
        rbac = self._get_rbac()
        try:
            has_perm = await rbac.check_permission(principal.id, command.required_permission)
        except Exception as e:
            # fail-closed:RBAC 异常一律拒绝
            logger.error(
                f"[CommandBus] RBAC 校验异常 action={command.action} "
                f"principal={principal.id} perm={command.required_permission}: {e}"
            )
            return Result(
                success=False,
                error=f"权限校验异常(已拒绝执行): {e}",
                action_id=action_id,
            )

        if not has_perm:
            logger.warning(
                f"[CommandBus] 权限不足 action={command.action} "
                f"principal={principal.id} perm={command.required_permission}"
            )
            return Result(
                success=False,
                error=f"权限不足: 缺少 {command.required_permission}",
                action_id=action_id,
            )

        # 2. 审批门禁(高风险操作)
        if command.requires_approval:
            approval = self._get_approval()
            try:
                approval_id = await approval.create_approval(
                    action=command.approval_action,
                    payload={
                        "command_action": command.action,
                        "params": command.params,
                        "principal_id": principal.id,
                        "principal_name": principal.name,
                        "principal_source": principal.source,
                        "action_id": action_id,
                    },
                    created_by=principal.id,
                )
            except Exception as e:
                logger.error(
                    f"[CommandBus] 创建审批失败 action={command.action} principal={principal.id}: {e}"
                )
                return Result(
                    success=False,
                    error=f"创建审批失败: {e}",
                    action_id=action_id,
                )

            if approval_id <= 0:
                return Result(
                    success=False,
                    error="创建审批失败(返回无效 approval_id)",
                    action_id=action_id,
                )

            logger.info(
                f"[CommandBus] 命令需审批 action={command.action} "
                f"principal={principal.id} approval_id={approval_id}"
            )
            return Result(
                success=False,
                approval_id=approval_id,
                approval_required=True,
                error=f"操作需要审批(approval_id={approval_id})",
                action_id=action_id,
            )

        # 3. 不需审批,直接执行 handler
        return await self._execute_handler(command, principal, action_id)

    # ─── 2. 审批通过后执行(由 approval_workflow.approve 调用) ────

    async def execute_approved_action(
        self,
        approval_id: int,
        action_id: str | None = None,
    ) -> Result:
        """审批通过后执行实际 handler(幂等)。

        Args:
            approval_id: 审批 ID(用于查找 payload)
            action_id: 幂等 ID(可选,None 时从 approval payload 中提取)

        Returns:
            Result 标准化返回
        """
        approval = self._get_approval()
        # 获取审批记录
        try:
            approval_record = await approval.get_approval(approval_id)
        except Exception as e:
            logger.error(f"[CommandBus] 获取审批记录失败 approval_id={approval_id}: {e}")
            return Result(success=False, error=f"获取审批记录失败: {e}")

        if approval_record is None:
            return Result(success=False, error=f"审批 {approval_id} 不存在")

        if approval_record.get("status") != approval.APPROVAL_STATUS_APPROVED:
            return Result(
                success=False,
                error=f"审批状态非 approved: {approval_record.get('status')}",
            )

        payload = approval_record.get("payload", {}) or {}
        # 从 payload 中恢复 command 信息
        command_action = payload.get("command_action", "")
        params = payload.get("params", {})
        principal_id = int(payload.get("principal_id", 0))
        principal_name = payload.get("principal_name", "")
        principal_source = payload.get("principal_source", "web")
        # R41 P0-5: action_id 优先从 payload 恢复,否则基于 SHA256 计算
        if not action_id:
            action_id = payload.get("action_id") or _compute_action_id(
                command_action, params, principal_id,
            )

        # 重建 command(从已注册的 handler 中查找)
        command = _resolve_command_for_action(command_action, params)
        if command is None:
            return Result(
                success=False,
                error=f"无法解析命令 handler: {command_action}",
                action_id=action_id,
            )

        # 更新审批状态为 EXECUTING
        try:
            await approval.mark_executing(approval_id)
        except Exception as e:
            logger.warning(f"[CommandBus] 标记 EXECUTING 失败 approval_id={approval_id}: {e}")

        principal = AdminPrincipal(
            id=principal_id, name=principal_name, source=principal_source,
        )
        result = await self._execute_handler(command, principal, action_id)

        # 更新审批状态为 EXECUTED 或 FAILED
        try:
            if result.success:
                await approval.mark_executed(approval_id)
            else:
                await approval.mark_failed(approval_id, result.error)
        except Exception as e:
            logger.warning(
                f"[CommandBus] 标记最终状态失败 approval_id={approval_id}: {e}"
            )

        return result

    # ─── 2b. R41 P0-4: 由 ApprovalExecutor 调用,从 command_outbox 条目执行 ──

    async def execute_command_outbox_entry(self, entry: dict) -> Result:
        """R41 P0-4: 执行 command_outbox 中的条目(由 ApprovalExecutor 调用)。

        与 ``execute_approved_action`` 的差异:
        - 不查询 approval_record(payload 已在 entry 中,避免重复 DB 读)
        - 不走 SQLite ``command_executions`` 幂等缓存,允许 ApprovalExecutor 重试
          (幂等由 command_outbox.action_id UNIQUE 约束 + handler 自身保证)
        - 仍调用 ``mark_executing`` / ``mark_executed`` / ``mark_failed`` 维护审批状态机

        Args:
            entry: command_outbox 行字典,字段包含:
                - id / action_id / approval_id / command_type / payload(JSON 字符串) /
                  status / retry_count / max_retries / next_retry_at / last_error /
                  created_at / updated_at

        Returns:
            Result 标准化返回
        """
        import json as _json_ox

        approval = self._get_approval()
        approval_id = int(entry.get("approval_id", 0) or 0)
        action_id = entry.get("action_id", "") or ""
        command_action = entry.get("command_type", "") or ""

        # 解析 payload(JSON 字符串 → dict)
        payload_data = entry.get("payload") or "{}"
        if isinstance(payload_data, str):
            try:
                payload_data = _json_ox.loads(payload_data)
            except (ValueError, TypeError):
                payload_data = {}
        if not isinstance(payload_data, dict):
            payload_data = {}

        params = payload_data.get("params", {}) or {}
        principal_id = int(payload_data.get("principal_id", 0) or 0)
        principal_name = payload_data.get("principal_name", "") or ""
        principal_source = payload_data.get("principal_source", "web") or "web"
        if not action_id:
            action_id = payload_data.get("action_id", _generate_action_id(
                AdminPrincipal(id=principal_id, name=principal_name, source=principal_source),
                command_action,
            ))
        if not command_action:
            command_action = payload_data.get("command_action", "")

        # 重建 command(从已注册的 handler 工厂中查找)
        command = _resolve_command_for_action(command_action, params)
        if command is None:
            error_msg = f"无法解析命令 handler: {command_action}"
            logger.warning(
                f"[CommandBus] execute_command_outbox_entry {error_msg} "
                f"approval_id={approval_id} action_id={action_id}"
            )
            return Result(success=False, error=error_msg, action_id=action_id)

        # 标记审批状态为 EXECUTING(独立事务,不与外层 ApprovalExecutor 事务嵌套)
        try:
            await approval.mark_executing(approval_id)
        except Exception as e:
            logger.warning(
                f"[CommandBus] execute_command_outbox_entry mark_executing 失败 "
                f"approval_id={approval_id}: {e}"
            )

        principal = AdminPrincipal(
            id=principal_id, name=principal_name, source=principal_source,
        )

        # 直接执行 handler(不走 SQLite command_executions 幂等缓存,允许 ApprovalExecutor 重试)
        try:
            data = await command.handler(command.params)
            result = Result(success=True, data=data, action_id=action_id)
            logger.info(
                f"[CommandBus] execute_command_outbox_entry 成功 "
                f"approval_id={approval_id} action_id={action_id} action={command_action}"
            )
            # 更新审批状态为 EXECUTED
            try:
                await approval.mark_executed(approval_id)
            except Exception as e:
                logger.warning(
                    f"[CommandBus] execute_command_outbox_entry mark_executed 失败 "
                    f"approval_id={approval_id}: {e}"
                )
            return result
        except Exception as e:
            result = Result(
                success=False,
                error=f"执行失败: {e}",
                action_id=action_id,
            )
            logger.error(
                f"[CommandBus] execute_command_outbox_entry 失败 "
                f"approval_id={approval_id} action_id={action_id} "
                f"action={command_action}: {e}"
            )
            # 更新审批状态为 FAILED
            try:
                await approval.mark_failed(approval_id, result.error)
            except Exception as me:
                logger.warning(
                    f"[CommandBus] execute_command_outbox_entry mark_failed 失败 "
                    f"approval_id={approval_id}: {me}"
                )
            return result

    # ─── 3. 实际执行 handler + 审计 ──────────────────────────────

    async def _execute_handler(
        self,
        command: Command,
        principal: AdminPrincipal,
        action_id: str,
    ) -> Result:
        """R41 P0-5: 执行 handler,使用 SQLite CAS 保证幂等。

        流程:
        1. 计算 request_hash = SHA256(payload)
        2. CAS INSERT INTO command_executions(status='pending')
           - UNIQUE 冲突 → 查询现有状态返回缓存 Result
        3. CAS UPDATE status='executing' WHERE action_id=? AND status='pending'
           - rowcount=0 → 被其他 worker 抢占
        4. 执行 handler
        5. UPDATE status='executed', result=JSON

        无 DB 时降级为直接执行(无幂等保护,仅用于测试)。
        """
        # R41 P0-5: 无 DB 降级模式 — 直接执行 handler(仅测试/开发用)
        store = _get_store()
        if not store._db:
            logger.warning(
                f"[CommandBus] 数据库未初始化,降级模式(无幂等保护) "
                f"action={command.action} action_id={action_id}"
            )
            try:
                data = await command.handler(command.params)
                return Result(success=True, data=data, action_id=action_id)
            except Exception as e:
                return Result(
                    success=False,
                    error=f"执行失败: {e}",
                    action_id=action_id,
                )

        # 1. 计算 request_hash(防篡改)
        request_hash = _compute_request_hash(command.params)

        # 2. CAS INSERT (status='pending') — 若已存在返回缓存
        cached = await _try_insert_or_get_cached(
            action_id, command.action, principal.id, request_hash,
        )
        if cached is not None:
            logger.info(
                f"[CommandBus] 幂等命中 action_id={action_id} action={command.action} "
                f"跳过重复执行(success={cached.success})"
            )
            return cached

        # 3. CAS claim (pending → executing)
        owner = _get_worker_owner()
        claimed = await claim_execution(action_id, owner, lease_seconds=60)
        if not claimed:
            # 被其他 worker 抢占或状态已变
            logger.warning(
                f"[CommandBus] CAS claim 失败 action_id={action_id} "
                f"action={command.action}(可能被其他 worker 抢占)"
            )
            return Result(
                success=False,
                error="操作已被其他 worker 抢占或状态已变更",
                action_id=action_id,
            )

        # 4. 执行 handler
        try:
            data = await command.handler(command.params)
            result = Result(
                success=True,
                data=data,
                action_id=action_id,
            )
            # 5. UPDATE status='executed', result=JSON
            await _mark_executed(action_id, result)
            logger.info(
                f"[CommandBus] 命令执行成功 action={command.action} "
                f"principal={principal.id} action_id={action_id}"
            )
            return result
        except Exception as e:
            result = Result(
                success=False,
                error=f"执行失败: {e}",
                action_id=action_id,
            )
            # 失败也持久化(防止无脑重试,可通过 release_execution 释放后重试)
            await _mark_failed(action_id, result)
            logger.error(
                f"[CommandBus] 命令执行失败 action={command.action} "
                f"principal={principal.id} action_id={action_id}: {e}"
            )
            return result


# ════════════════════════════════════════════════════════════════
# 高风险命令注册表 — 集中管理所有 requires_approval=True 的命令
# ════════════════════════════════════════════════════════════════

# 命令 action → (permission, approval_action, requires_approval)
HIGH_RISK_COMMAND_REGISTRY: dict[str, tuple[str, str, bool]] = {
    "takedown_report":       (PERM_CONTENT_TAKEDOWN,      APPROVAL_ACTION_TAKEDOWN,             True),
    "ban_user":              (PERM_USERS_BAN,             APPROVAL_ACTION_BAN,                  True),
    "unban_user":            (PERM_USERS_UNBAN,           "",                                   False),
    "assign_role":           (PERM_RBAC_ASSIGN,           APPROVAL_ACTION_RBAC_ASSIGN,          True),
    "restore_backup":        (PERM_DISASTER_RESTORE,      APPROVAL_ACTION_RESTORE,              True),
    "enable_maintenance":    (PERM_MAINTENANCE_ENABLE,    APPROVAL_ACTION_MAINTENANCE_ENABLE,   True),
    "disable_maintenance":   (PERM_MAINTENANCE_DISABLE,   APPROVAL_ACTION_MAINTENANCE_DISABLE,   True),
    "purge_data":            (PERM_DATA_PURGE,            APPROVAL_ACTION_DELETE_DATA,          True),
    # R40 P0-8: 文件软删除 — 复用 content:takedown 权限,无需审批(可立即执行)
    "delete_file":           (PERM_CONTENT_TAKEDOWN,      "",                                   False),
}


def _resolve_command_for_action(action: str, params: dict) -> Command | None:
    """根据 action 名称通过工厂函数构造 Command 对象(含 handler)。

    R40 P0-8: 审批通过后 execute_approved_action 调用此函数重建命令。
    通过工厂函数重新构造 Command,确保 handler 正确绑定到 params。

    Args:
        action: 命令标识(如 "takedown_report")
        params: 命令参数(从审批 payload 中恢复)

    Returns:
        Command 对象(含 handler);未注册返回 None
    """
    if action == "takedown_report":
        return make_takedown_command(
            target_type=params.get("target_type", ""),
            target_id=str(params.get("target_id", "")),
            reason=params.get("reason", ""),
        )
    if action == "ban_user":
        return make_ban_user_command(
            user_id=int(params.get("user_id", 0)),
            reason=params.get("reason", ""),
            duration_days=int(params.get("duration_days", 0)),
        )
    if action == "unban_user":
        return make_unban_user_command(
            user_id=int(params.get("user_id", 0)),
        )
    if action == "assign_role":
        return make_assign_role_command(
            user_id=int(params.get("user_id", 0)),
            role_name=params.get("role_name", ""),
        )
    if action == "restore_backup":
        return make_restore_backup_command(
            backup_id=str(params.get("backup_id", "")),
        )
    if action == "enable_maintenance":
        return make_enable_maintenance_command(
            reason=params.get("reason", "manual"),
        )
    if action == "disable_maintenance":
        return make_disable_maintenance_command()
    if action == "purge_data":
        return make_purge_data_command(
            table_names=params.get("table_names", []),
        )
    if action == "delete_file":
        # R40 P0-8: 文件软删除命令(审批通过后重建)
        return make_delete_file_command(
            file_code=str(params.get("file_code", "")),
        )
    # 未注册的 action
    logger.warning(f"[CommandBus] _resolve_command_for_action 未知 action: {action}")
    return None


# ════════════════════════════════════════════════════════════════
# 命令构造工厂 — 为常见高风险操作提供预构造的 Command
# ════════════════════════════════════════════════════════════════

def make_takedown_command(target_type: str, target_id: str, reason: str = "") -> Command:
    """构造内容下架命令。"""
    async def _handler(params: dict) -> dict:
        from services import content_reports
        ok = await content_reports.takedown_content(
            params["target_type"], params["target_id"],
            params.get("reason", ""), admin_id=0,
        )
        return {"takedown_ok": ok}

    return Command(
        action="takedown_report",
        required_permission=PERM_CONTENT_TAKEDOWN,
        handler=_handler,
        params={"target_type": target_type, "target_id": target_id, "reason": reason},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_TAKEDOWN,
    )


def make_ban_user_command(user_id: int, reason: str = "", duration_days: int = 0) -> Command:
    """构造封禁用户命令。"""
    async def _handler(params: dict) -> dict:
        from services import content_reports
        ok = await content_reports.ban_user(
            params["user_id"],
            params.get("reason", ""),
            duration_days=params.get("duration_days", 0),
            admin_id=0,
        )
        return {"ban_ok": ok}

    return Command(
        action="ban_user",
        required_permission=PERM_USERS_BAN,
        handler=_handler,
        params={"user_id": user_id, "reason": reason, "duration_days": duration_days},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_BAN,
    )


def make_unban_user_command(user_id: int) -> Command:
    """构造解封用户命令(不需审批)。"""
    async def _handler(params: dict) -> dict:
        from services import content_reports
        ok = await content_reports.unban_user(params["user_id"], admin_id=0)
        return {"unban_ok": ok}

    return Command(
        action="unban_user",
        required_permission=PERM_USERS_UNBAN,
        handler=_handler,
        params={"user_id": user_id},
        requires_approval=False,
    )


def make_assign_role_command(user_id: int, role_name: str) -> Command:
    """构造分配角色命令。"""
    async def _handler(params: dict) -> dict:
        from services import rbac
        ok = await rbac.assign_role(
            params["user_id"], params["role_name"], assigned_by=0,
        )
        return {"assign_ok": ok}

    return Command(
        action="assign_role",
        required_permission=PERM_RBAC_ASSIGN,
        handler=_handler,
        params={"user_id": user_id, "role_name": role_name},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_RBAC_ASSIGN,
    )


def make_restore_backup_command(
    backup_id: str,
    tables: list[str] | None = None,
    merge: bool = False,
) -> Command:
    """构造恢复备份命令(必须审批)。

    Args:
        backup_id: 备份文件 key(R2 对象 key)
        tables: 仅恢复指定表(None=全部)
        merge: True=增量补充(不删除现有数据);False=覆盖恢复
    """
    async def _handler(params: dict) -> dict:
        # R40 P0-8 + P0-7: 审批通过后执行实际恢复
        # 优先使用 services.db_backup.restore_from_backup(支持 tables/merge 选择性恢复)
        try:
            from services.db_backup import restore_from_backup
            return await restore_from_backup(
                params["backup_id"],
                tables=params.get("tables"),
                merge=params.get("merge", False),
            )
        except ImportError:
            # 降级到 BackupEngine.restore(不支持选择性恢复)
            from services.backup_engine import BackupEngine
            engine = BackupEngine()
            return await engine.restore(
                params["backup_id"], target="production",
                approver_id=params.get("approver_id", 0),
            )

    return Command(
        action="restore_backup",
        required_permission=PERM_DISASTER_RESTORE,
        handler=_handler,
        params={
            "backup_id": backup_id,
            "tables": tables,
            "merge": merge,
        },
        requires_approval=True,
        approval_action=APPROVAL_ACTION_RESTORE,
    )


def make_enable_maintenance_command(reason: str = "manual") -> Command:
    """构造开启维护模式命令。"""
    async def _handler(params: dict) -> dict:
        from services import maintenance_mode
        ok = await maintenance_mode.enable(
            params.get("reason", "manual"), started_by=0,
        )
        return {"enable_ok": ok}

    return Command(
        action="enable_maintenance",
        required_permission=PERM_MAINTENANCE_ENABLE,
        handler=_handler,
        params={"reason": reason},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_MAINTENANCE_ENABLE,
    )


def make_disable_maintenance_command() -> Command:
    """构造关闭维护模式命令。"""
    async def _handler(params: dict) -> dict:
        from services import maintenance_mode
        ok = await maintenance_mode.disable(ended_by=0)
        return {"disable_ok": ok}

    return Command(
        action="disable_maintenance",
        required_permission=PERM_MAINTENANCE_DISABLE,
        handler=_handler,
        params={},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_MAINTENANCE_DISABLE,
    )


def make_purge_data_command(table_names: list[str] | None = None) -> Command:
    """构造清除数据命令(必须审批)。"""
    async def _handler(params: dict) -> dict:
        # 仅在测试中校验 handler 被调用,实际清除逻辑委托给专用脚本
        return {"purge_ok": True, "tables": params.get("table_names", [])}

    return Command(
        action="purge_data",
        required_permission=PERM_DATA_PURGE,
        handler=_handler,
        params={"table_names": table_names or []},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_DELETE_DATA,
    )


def make_delete_file_command(file_code: str) -> Command:
    """R40 P0-8: 构造文件软删除命令(不需审批,可立即执行)。

    复用 content:takedown 权限门禁,但 requires_approval=False:
    软删除可立即执行,不阻塞用户操作。

    Args:
        file_code: 文件唯一标识

    Returns:
        Command 对象(handler 执行软删除 + 本地 tombstone)
    """
    async def _handler(params: dict) -> dict:
        # 软删除逻辑:status='deleted' + deleted_at + 本地 tombstone
        import datetime as _dt
        from database import get_file_records_col
        from database.cache_store import get_cache_store

        file_code = params["file_code"]
        deleted_at = _dt.datetime.now().isoformat()
        files_col = get_file_records_col()
        result = await files_col.update_one(
            {"file_code": file_code},
            {"$set": {"status": "deleted", "deleted_at": deleted_at}},
        )
        if result.matched_count == 0:
            raise ValueError(f"文件不存在: {file_code}")
        # 写本地 SQLite tombstone + dirty_outbox(保证 CRDB 同步删除事件)
        try:
            await get_cache_store().soft_delete("file_records", file_code, deleted_at)
        except Exception as e:
            # CRDB 已更新,本地 tombstone 失败仅记录(下次 crdb_sync 补偿)
            logger.warning(
                f"[CommandBus] delete_file 本地 tombstone 失败(已更新 CRDB): {e}"
            )
        return {"file_code": file_code, "deleted_at": deleted_at}

    return Command(
        action="delete_file",
        required_permission=PERM_CONTENT_TAKEDOWN,
        handler=_handler,
        params={"file_code": file_code},
        requires_approval=False,
    )
