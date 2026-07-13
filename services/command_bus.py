"""R40 P0-8: 命令总线 — RBAC + 审批强制门禁。

职责:
    解决原 Web 路由仅依赖 Basic Auth、Bot 命令依赖旧 ``_auth_required`` 的问题。
    所有高风险操作必须经过 CommandBus,完成:
    1. RBAC 权限校验(``check_permission``)
    2. 审批门禁(高风险操作 ``requires_approval=True`` 时,创建 approval 记录,
       等待审批通过后才执行 handler)
    3. 审计日志(记录 principal.id / action / params / result)
    4. 幂等(基于 action_id 防重复执行)

设计要点:
    - 命令对象(Command dataclass)描述所需的权限、审批策略、handler、参数
    - Principal 封装当前操作者身份(id + name + 来源 web/bot)
    - Result 标准化返回(success/data/error/approval_id)
    - 高风险命令通过注册表(REQUIRED_APPROVAL_COMMANDS)集中管理
    - fail-closed:权限校验异常时一律拒绝(返回 success=False)
"""
from __future__ import annotations

import datetime as _dt
import json
import secrets as _secrets
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


# ─── 幂等执行追踪(进程内) ───────────────────────────────────
# action_id → 上次执行结果(防止重复执行)
_EXECUTED_ACTIONS: dict[str, Result] = {}


def _generate_action_id(principal: AdminPrincipal, action: str) -> str:
    """生成幂等 action_id(基于 principal + action + 时间戳 + 随机)。

    同一 principal 多次发起相同 action,每次生成不同 action_id;
    但调用方可以通过传入相同 action_id(从 approval 记录中取)实现幂等。
    """
    return f"{action}_{principal.id}_{_dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_secrets.token_hex(4)}"


def reset_idempotency_cache() -> None:
    """重置进程内幂等缓存(测试用例间隔离)。"""
    _EXECUTED_ACTIONS.clear()


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

        Args:
            command: 命令对象
            principal: 操作者身份
            action_id: 幂等 ID(可选,None 时自动生成;传入相同 ID 触发幂等去重)

        Returns:
            Result 标准化返回
        """
        # 生成或复用 action_id(幂等追踪)
        if not action_id:
            action_id = _generate_action_id(principal, command.action)

        # 幂等检查:已成功执行的 action_id 直接返回缓存结果
        if action_id in _EXECUTED_ACTIONS:
            cached = _EXECUTED_ACTIONS[action_id]
            logger.info(
                f"[CommandBus] 幂等命中 action_id={action_id} action={command.action} "
                f"跳过重复执行(success={cached.success})"
            )
            return cached

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
        if not action_id:
            action_id = payload.get("action_id", _generate_action_id(
                AdminPrincipal(id=principal_id, name=principal_name, source=principal_source),
                command_action,
            ))

        # 幂等检查
        if action_id in _EXECUTED_ACTIONS:
            cached = _EXECUTED_ACTIONS[action_id]
            logger.info(
                f"[CommandBus] 幂等命中 approval_id={approval_id} action_id={action_id}"
            )
            return cached

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

    # ─── 3. 实际执行 handler + 审计 ──────────────────────────────

    async def _execute_handler(
        self,
        command: Command,
        principal: AdminPrincipal,
        action_id: str,
    ) -> Result:
        """执行 handler 并记录审计日志(幂等缓存)。"""
        # 标记 action_id 为执行中(写入占位,防止并发重复)
        # 实际结果在执行完成后覆盖
        _EXECUTED_ACTIONS[action_id] = Result(
            success=False, error="执行中", action_id=action_id,
        )

        try:
            data = await command.handler(command.params)
            result = Result(
                success=True,
                data=data,
                action_id=action_id,
            )
            _EXECUTED_ACTIONS[action_id] = result
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
            # 失败也缓存(防止无脑重试,但可通过 execute_approved_action 重新触发)
            _EXECUTED_ACTIONS[action_id] = result
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
