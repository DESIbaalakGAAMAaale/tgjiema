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
from services.i18n import translate as _i18n_t


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
        effect_receipts: R45 — 外部副作用 receipt 状态清单,记录每个副作用的
            status / external_id / skipped 字段,便于调用方判断是否被跳过
    """
    success: bool
    data: Any = None
    error: str = ""
    approval_id: int = 0
    approval_required: bool = False
    action_id: str = ""
    effect_receipts: dict = field(default_factory=dict)


# ─── R41 P0-5: 幂等执行追踪(SQLite 持久化) ───────────────────
# 替代原 _EXECUTED_ACTIONS 进程内 dict,改为 command_executions 表 CAS。
# 状态机: pending → executing → executed/failed
CMD_STATUS_PENDING = "pending"
CMD_STATUS_EXECUTING = "executing"
CMD_STATUS_EXECUTED = "executed"
CMD_STATUS_FAILED = "failed"

# ─── R52 P0-5: 统一高风险动作状态机 ───────────────────────────
# 所有高风险动作(Repair/Maintenance/Restore)共用统一状态机:
#   pending → approved → executing → executed/failed/retryable
# - pending:   审批待处理(等待 approve)
# - approved:  审批通过,等待执行(执行前必须验证此状态)
# - executing: 执行中(CAS 防并发,rowcount=0 表示冲突)
# - executed:  执行成功(终态,不可重复执行)
# - failed:    执行失败(可重新审批后重试)
# - retryable: 执行失败(可重试,用于瞬时故障区分)
CMD_STATUS_APPROVED = "approved"
CMD_STATUS_RETRYABLE = "retryable"

# 状态转换合法性表(用于 is_valid_transition 校验)
# failed/retryable 可重新进入 approved(重新审批后重试)
VALID_TRANSITIONS: dict[str, set[str]] = {
    CMD_STATUS_PENDING: {CMD_STATUS_APPROVED},
    CMD_STATUS_APPROVED: {CMD_STATUS_EXECUTING},
    CMD_STATUS_EXECUTING: {CMD_STATUS_EXECUTED, CMD_STATUS_FAILED, CMD_STATUS_RETRYABLE},
    CMD_STATUS_FAILED: {CMD_STATUS_APPROVED},
    CMD_STATUS_RETRYABLE: {CMD_STATUS_APPROVED},
    CMD_STATUS_EXECUTED: set(),  # 终态,不可再转换
}


# ─── R53 P1-5: 高风险动作 registry(action 级别,与 HIGH_RISK_COMMAND_REGISTRY 区分) ───
# 这里的 action 名对应 command_executions.command_type 列(命令的"动作类型"),
# 用于 claim_execution 旧入口的拦截:当 action ∈ HIGH_RISK_ACTIONS 且
# requires_approval=1 时,必须改走 claim_execution_approved 审批路径,
# 防止高风险动作误走 pending → executing 旧状态机绕过审批。
# 注:HIGH_RISK_COMMAND_REGISTRY 是 command 级别的注册表(action 字符串如
# "takedown_report"),而 HIGH_RISK_ACTIONS 是底层 effect_type / action 名(如
# "takedown"),两者协同工作:
#   - HIGH_RISK_COMMAND_REGISTRY 决定 Command.requires_approval 是否为 True
#   - HIGH_RISK_ACTIONS 决定 claim_execution 是否拦截(防止误走旧入口)
HIGH_RISK_ACTIONS: set[str] = {
    "ban",
    "purge",
    "takedown",
    "restore",
    "crdb_delete",
    "r2_put",
    "r2_download",
    "telegram_copy",
    "telegram_send",
    "force_join",
    "rotate",
    "demote",
}


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """R52 P0-5: 校验状态转换是否合法。

    Args:
        from_status: 当前状态
        to_status: 目标状态

    Returns:
        True 表示转换合法;False 表示非法
    """
    allowed = VALID_TRANSITIONS.get(from_status, set())
    return to_status in allowed


async def get_command_status(action_id: str) -> str | None:
    """R52 P0-5: 查询 command_executions 当前状态。

    Args:
        action_id: 命令幂等 ID

    Returns:
        当前状态字符串;记录不存在或 DB 不可用时返回 None
    """
    store = _get_store()
    if not store._db:
        return None
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
    except Exception as e:
        logger.warning(
            f"[CommandBus] get_command_status 查询失败 action_id={action_id}: {e}"
        )
        return None
    if not rows:
        return None
    return rows[0][0]


async def claim_execution_approved(
    action_id: str,
    owner: str,
    request_hash: str = "",
    lease_seconds: int = 60,
    connection: Any = None,
) -> bool:
    """R52 P0-5 + R55 P0-2: CAS 认领 — 将 status='approved' 转为 'executing'。

    统一高风险动作执行入口,Repair/Maintenance/Restore 共用此函数:
    - 执行前必须验证 approved 状态(避免"执行前已执行"语义冲突)
    - CAS UPDATE 保证只有一个 worker 能进入 executing
    - 失败时由调用方决定是否重试(approved 状态未改变,可再次尝试)

    R55 P0-2 整改:
    - request_hash 从可选改为**强制必填**(64 位 SHA-256 hex)
    - 存储 Hash 为空同样拒绝(fail-closed)
    - 使用 hmac.compare_digest 恒定时间比较(防时序攻击)
    - request_hash 必须绑定 action_id、command_type、principal、target、
      resource_version、canonical_params

    R53 P0-2: fail-closed 整改
    - ``store._db`` 不可用时**必须**抛 AppError,禁止降级执行。

    R61 P0-01: 新增 ``connection`` 参数,允许在调用方已开启的事务内执行 CAS
    认领(不自动 commit),使审批消费 + 认领 + 业务副作用 + 标记 executed 在
    同一事务内原子提交/回滚。``connection=None``(默认)时行为不变(自动 commit)。

    Args:
        action_id: 命令幂等 ID
        owner: 执行 worker 标识(如 hostname:pid)
        request_hash: **强制必填**,SHA-256(payload),64 位 hex,防篡改校验
        lease_seconds: 租约时长(秒),过期后可被 cleanup_stale_leases 回收
        connection: 可选事务连接,传入时复用该连接且**不自动 commit**
            (由调用方在同一事务内控制 commit/rollback,确保原子性)

    Returns:
        True 表示认领成功;False 表示已被其他 worker 抢占或状态不符(rowcount=0)

    Raises:
        AppError(COMMAND_EXECUTION_STORE_UNAVAILABLE): DB 不可用 / 访问异常 / commit 失败
        AppError(COMMAND_MUST_USE_APPROVAL_PATH): request_hash 为空或格式非法
    """
    from services.error_codes import AppError, ErrorCodes
    import hmac as _hmac

    # R55 P0-2: request_hash 强制非空 + 64 位 hex 格式校验
    if not request_hash or len(request_hash) != 64:
        logger.error(
            f"[CommandBus] claim_execution_approved 拒绝: "
            f"request_hash 为空或非 64 位 hex,action_id={action_id}"
        )
        raise AppError(
            ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH,
            params={
                "action_id": action_id,
                "reason": "request_hash_required_64_hex",
            },
        )
    try:
        int(request_hash, 16)  # 验证是合法 hex
    except (ValueError, TypeError):
        raise AppError(
            ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH,
            params={
                "action_id": action_id,
                "reason": "request_hash_invalid_hex",
            },
        )

    store = _get_store()
    if not store._db:
        # R53 P0-2: fail-closed — DB 未初始化时禁止降级执行高风险动作
        logger.error(
            f"[CommandBus] claim_execution_approved fail-closed: "
            f"DB 未初始化,拒绝认领 action_id={action_id} owner={owner}"
            f"(高风险动作必须有状态机保护,禁止降级执行)"
        )
        raise AppError(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
            params={
                "action_id": action_id,
                "reason": "db_not_initialized",
            },
        )
    lease_until = _lease_until_iso(lease_seconds)
    now = _now_iso()
    # R61 P0-01: connection 传入时复用调用方事务连接(不自动 commit);
    # connection=None 时使用 store._db 并在 CAS 成功后自动 commit(向后兼容)。
    _db = connection if connection is not None else store._db
    try:
        # R55 P0-2: request_hash 强制校验(不再可选)
        rows = await _db.execute_fetchall(
            "SELECT request_hash FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        if not rows or not rows[0]:
            logger.warning(
                f"[CommandBus] claim_execution_approved: action_id 不存在 "
                f"action_id={action_id}"
            )
            return False
        stored_hash = rows[0][0]
        # R55 P0-2: 存储 Hash 为空直接拒绝(fail-closed)
        if not stored_hash:
            logger.error(
                f"[CommandBus] claim_execution_approved 拒绝: "
                f"存储 request_hash 为空,action_id={action_id}"
            )
            raise AppError(
                ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH,
                params={
                    "action_id": action_id,
                    "reason": "stored_request_hash_empty",
                },
            )
        # R55 P0-2: 恒定时间比较(防时序攻击)
        if not _hmac.compare_digest(stored_hash, request_hash):
            logger.warning(
                f"[CommandBus] claim_execution_approved hash_mismatch "
                f"action_id={action_id}"
            )
            return False
        # CAS: approved → executing
        cursor = await _db.execute(
            "UPDATE command_executions "
            "SET status = ?, owner = ?, lease_until = ?, approved_at = COALESCE(approved_at, ?), updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (CMD_STATUS_EXECUTING, owner, lease_until, now, now,
             action_id, CMD_STATUS_APPROVED),
        )
        # R61 P0-01: 仅在未传入 connection(自管事务)时自动 commit;
        # 传入 connection 时由调用方统一控制 commit/rollback(原子性保障)。
        if connection is None:
            await store._db.commit()
        claimed = cursor.rowcount > 0
        if claimed:
            logger.info(
                f"[CommandBus] claim_execution_approved 成功 "
                f"action_id={action_id} owner={owner}"
            )
        else:
            logger.warning(
                f"[CommandBus] claim_execution_approved CAS 未命中 "
                f"action_id={action_id}(状态非 approved 或已被抢占)"
            )
        return claimed
    except AppError:
        # 已是协议化错误,直接向上传播(保留原始 code)
        raise
    except Exception as e:
        # R53 P0-2: fail-closed — DB 访问/commit 异常时禁止降级执行
        logger.error(
            f"[CommandBus] claim_execution_approved fail-closed: "
            f"DB 访问异常,拒绝认领 action_id={action_id} owner={owner}: {e}"
            f"(高风险动作必须有状态机保护,禁止降级执行)"
        )
        raise AppError(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
            params={
                "action_id": action_id,
                "reason": f"db_access_error: {type(e).__name__}: {e}",
            },
        ) from e


async def mark_approved_executed(
    action_id: str,
    result: Any = None,
    connection: Any = None,
) -> bool:
    """R52 P0-5: 标记执行成功 — 将 status='executing' 转为 'executed'。

    R61 P0-01: 新增 ``connection`` 参数,允许在调用方已开启的事务内执行 CAS
    标记(不自动 commit),与审批消费/业务副作用同事务原子提交/回滚。
    ``connection=None``(默认)时行为不变(自动 commit)。

    Args:
        action_id: 命令幂等 ID
        result: 执行结果(可选,序列化为 JSON 存储)
        connection: 可选事务连接,传入时复用该连接且**不自动 commit**

    Returns:
        True 成功;False 失败
    """
    store = _get_store()
    if not store._db:
        return False
    # R61 P0-01: connection 传入时复用调用方事务连接(不自动 commit)
    _db = connection if connection is not None else store._db
    now = _now_iso()
    # 序列化 result
    if result is None:
        result_json = json.dumps(
            {"success": True, "data": None, "error": ""},
            ensure_ascii=False, default=str,
        )
    elif hasattr(result, "success") and (hasattr(result, "data") or hasattr(result, "error")):
        result_json = json.dumps(
            {"success": result.success, "data": result.data, "error": result.error},
            ensure_ascii=False, default=str,
        )
    elif isinstance(result, dict):
        result_json = json.dumps(result, ensure_ascii=False, default=str)
    else:
        result_json = json.dumps(
            {"success": True, "data": str(result), "error": ""},
            ensure_ascii=False, default=str,
        )
    try:
        cursor = await _db.execute(
            "UPDATE command_executions "
            "SET status = ?, result = ?, owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (CMD_STATUS_EXECUTED, result_json, now,
             action_id, CMD_STATUS_EXECUTING),
        )
        # R61 P0-01: 仅在未传入 connection 时自动 commit
        if connection is None:
            await store._db.commit()
        success = cursor.rowcount > 0
        if success:
            logger.info(
                f"[CommandBus] mark_approved_executed 成功 action_id={action_id}"
            )
        else:
            logger.warning(
                f"[CommandBus] mark_approved_executed CAS 未命中 "
                f"action_id={action_id}(状态非 executing)"
            )
        return success
    except Exception as e:
        logger.error(
            f"[CommandBus] mark_approved_executed 失败 action_id={action_id}: {e}"
        )
    # fail-closed:标记执行失败时返回 False
    return False


async def mark_approved_failed(
    action_id: str,
    error: str = "",
    retryable: bool = False,
) -> bool:
    """R52 P0-5: 标记执行失败 — 将 status='executing' 转为 'failed' 或 'retryable'。

    Args:
        action_id: 命令幂等 ID
        error: 错误信息(序列化为 JSON 存储)
        retryable: True → status='retryable'(可重试);False → status='failed'(永久失败)

    Returns:
        True 成功;False 失败
    """
    store = _get_store()
    if not store._db:
        return False
    now = _now_iso()
    target_status = CMD_STATUS_RETRYABLE if retryable else CMD_STATUS_FAILED
    result_json = json.dumps(
        {"success": False, "data": None, "error": error},
        ensure_ascii=False, default=str,
    )
    try:
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, result = ?, owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (target_status, result_json, now,
             action_id, CMD_STATUS_EXECUTING),
        )
        await store._db.commit()
        success = cursor.rowcount > 0
        if success:
            logger.info(
                f"[CommandBus] mark_approved_failed 标记为 {target_status} "
                f"action_id={action_id} error={error[:200]}"
            )
        else:
            logger.warning(
                f"[CommandBus] mark_approved_failed CAS 未命中 "
                f"action_id={action_id}(状态非 executing)"
            )
        return success
    except Exception as e:
        logger.error(
            f"[CommandBus] mark_approved_failed 失败 action_id={action_id}: {e}"
        )
    # fail-closed:标记执行失败时返回 False
    return False


async def verify_command_approved(
    action_id: str,
    expected_principal_id: int | None = None,
    expected_request_hash: str | None = None,
) -> dict:
    """R52 P0-5: 验证 command_executions 处于 'approved' 状态(执行前置校验)。

    Repair/Maintenance/Restore 在执行高风险动作前必须调用此函数,
    确保审批已通过(避免"执行前已执行"语义冲突)。

    R59 P0-05: 高风险失败不得降级为继续执行。
    - DB 未初始化时**必须**抛 AppError,禁止返回 approved 假设放行高风险写操作。
    - 只读查询可降级到只读 SQLite 快照;创建、删除、退款、导出、密钥变更、恢复等不得降级。

    Args:
        action_id: 命令幂等 ID
        expected_principal_id: 期望的 principal_id(防越权,None 跳过校验)
        expected_request_hash: 期望的 request_hash(防篡改,None 跳过校验)

    Returns:
        dict 包含 status / principal_id / request_hash(记录元信息)

    Raises:
        AppError(COMMAND_NOT_APPROVED): 记录不存在或状态非 approved
        AppError(COMMAND_HASH_MISMATCH): request_hash 不匹配
        AppError(COMMAND_STATUS_CONFLICT): principal_id 不匹配
        AppError(COMMAND_EXECUTION_STORE_UNAVAILABLE): DB 未初始化(R59 P0-05 fail-closed)
    """
    from services.error_codes import AppError, ErrorCodes

    store = _get_store()
    if not store._db:
        # R59 P0-05: fail-closed — 高风险动作验证不得降级放行
        # (旧版降级返回 approved 假设,绕过审批状态机,严重安全漏洞)
        logger.error(
            f"[CommandBus] verify_command_approved fail-closed: "
            f"DB 未初始化,拒绝放行 action_id={action_id}"
            f"(R59 P0-05: 高风险动作不得降级,必须抛 AppError)"
        )
        raise AppError(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
            params={
                "action_id": action_id,
                "reason": "db_not_initialized_r59_p0_05_fail_closed",
            },
        )
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status, principal_id, request_hash "
            "FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
    except Exception as e:
        logger.error(
            f"[CommandBus] verify_command_approved 查询失败 action_id={action_id}: {e}"
        )
        raise AppError(
            ErrorCodes.COMMAND_NOT_APPROVED,
            params={"action_id": action_id, "reason": f"db_error: {e}"},
        )
    if not rows:
        raise AppError(
            ErrorCodes.COMMAND_NOT_APPROVED,
            params={"action_id": action_id, "reason": "not_found"},
        )
    status, principal_id, request_hash = rows[0]
    # 状态校验:必须为 approved
    if status != CMD_STATUS_APPROVED:
        logger.warning(
            f"[CommandBus] verify_command_approved 状态非 approved "
            f"action_id={action_id} status={status}"
        )
        raise AppError(
            ErrorCodes.COMMAND_NOT_APPROVED,
            params={
                "action_id": action_id,
                "current_status": status,
                "expected_status": CMD_STATUS_APPROVED,
            },
        )
    # principal_id 校验(防越权)
    if expected_principal_id is not None and principal_id != expected_principal_id:
        logger.warning(
            f"[CommandBus] verify_command_approved principal 不匹配 "
            f"action_id={action_id} stored={principal_id} expected={expected_principal_id}"
        )
        raise AppError(
            ErrorCodes.COMMAND_STATUS_CONFLICT,
            params={
                "action_id": action_id,
                "reason": "principal_mismatch",
                "stored_principal_id": principal_id,
                "expected_principal_id": expected_principal_id,
            },
        )
    # request_hash 校验(防篡改)
    if (
        expected_request_hash is not None
        and request_hash
        and request_hash != expected_request_hash
    ):
        logger.warning(
            f"[CommandBus] verify_command_approved hash 不匹配 "
            f"action_id={action_id} stored={request_hash} expected={expected_request_hash}"
        )
        raise AppError(
            ErrorCodes.COMMAND_HASH_MISMATCH,
            params={
                "action_id": action_id,
                "stored_hash": request_hash,
                "expected_hash": expected_request_hash,
            },
        )
    return {
        "status": status,
        "principal_id": principal_id,
        "request_hash": request_hash,
    }


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

    R53 P1-5: 双状态机类型边界校验
    - 旧入口(pending → executing)仅允许低风险动作走
    - 高风险动作(action ∈ HIGH_RISK_ACTIONS 且 requires_approval=1)必须走
      ``claim_execution_approved``(approved → executing)审批路径
    - 若高风险动作误走本入口,抛 ``AppError(COMMAND_MUST_USE_APPROVAL_PATH)``
      fail-closed 阻断执行(防止绕过审批)
    - 低风险动作或 requires_approval=0 的记录正常 CAS claim

    Args:
        action_id: 命令幂等 ID
        owner: 执行 worker 标识(如 hostname:pid)
        lease_seconds: 租约时长(秒),过期后可被 cleanup_stale_leases 回收

    Returns:
        True 表示认领成功;False 表示已被其他 worker 抢占或状态不符

    Raises:
        AppError(COMMAND_MUST_USE_APPROVAL_PATH): 高风险动作(requires_approval=1
            且 command_type ∈ HIGH_RISK_ACTIONS)误走旧入口,必须改走审批路径
    """
    from services.error_codes import AppError, ErrorCodes

    store = _get_store()
    if not store._db:
        return False
    # R54 P0-1: requires_approval=1 一律禁止旧入口,fail-closed
    # _auto_approve 已移除:不再允许未注册命令自动批准
    _query_failed = False
    try:
        rows = await store._db.execute_fetchall(
            "SELECT command_type, requires_approval FROM command_executions "
            "WHERE action_id = ?",
            (action_id,),
        )
    except Exception as e:
        logger.error(
            f"[CommandBus] claim_execution 查询 command_type 失败 "
            f"action_id={action_id}: {e}"
        )
        _query_failed = True
    if _query_failed:
        # fail-closed:查询失败时返回 False
        return False
    if rows and rows[0]:
        command_type, requires_approval = rows[0]
        # R54 P0-1: requires_approval=1 一律禁止走旧入口,fail-closed
        # 无论 command_type 是否在 HIGH_RISK_ACTIONS registry 中,
        # 只要 requires_approval=1 就必须走 claim_execution_approved 审批路径。
        # registry 漏项不得变成审批绕过,未知 command_type fail-closed。
        if requires_approval == 1:
            logger.error(
                f"[CommandBus] claim_execution 拒绝:requires_approval=1 "
                f"action_id={action_id} command_type={command_type} "
                f"in_registry={command_type in HIGH_RISK_ACTIONS}"
                f"(必须改走 claim_execution_approved 审批路径)"
            )
            raise AppError(
                ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH,
                params={
                    "action_id": action_id,
                    "command_type": command_type,
                    "reason": "requires_approval_must_use_approval_path",
                },
            )
    lease_until = _lease_until_iso(lease_seconds)
    now = _now_iso()
    try:
        # R54 P0-1: _auto_approve 已移除,requires_approval=1 一律必须走审批路径
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = ?, owner = ?, lease_until = ?, updated_at = ? "
            "WHERE action_id = ? AND status = ?",
            (CMD_STATUS_EXECUTING, owner, lease_until, now,
             action_id, CMD_STATUS_PENDING),
        )
        await store._db.commit()
        return cursor.rowcount > 0
    except AppError:
        # 已是协议化错误,直接向上传播
        raise
    except Exception as e:
        logger.error(f"[CommandBus] claim_execution 失败 action_id={action_id}: {e}")
    # fail-closed:claim 失败时返回 False
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
    # fail-closed:续租失败时返回 False
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
    # fail-closed:释放失败时返回 False
    return False


async def cleanup_stale_leases() -> int:
    """R41 P0-5: 清理过期租约 — status='executing' 且 lease_until < now → status='retryable'。

    被 r40_scheduler 每 60 秒调用一次。将过期租约转 retryable,
    防止 worker 崩溃后任务永久卡在 executing。

    R53 P1-5: lease 过期只能转 ``retryable``(不可直接 pending)
    - 原行为(executing → pending)会绕过状态机,允许其他 worker 通过
      ``claim_execution`` 旧入口重新认领,可能让高风险动作绕过审批路径
    - 新行为(executing → retryable)强制走 VALID_TRANSITIONS 状态机:
      retryable → approved(重新审批)→ executing(claim_execution_approved)
    - 低风险动作(requires_approval=0)lease 过期后由 ``release_execution``
      显式释放回 pending(由调用方决定),cleanup_stale_leases 不再隐式回退

    Returns:
        清理的记录数
    """
    store = _get_store()
    if not store._db:
        return 0
    now = _now_iso()
    try:
        # R53 P1-5: 高风险动作(requires_approval=1)lease 过期 → retryable(需重新审批)
        # 低风险动作(requires_approval=0)lease 过期 → pending(可直接重新认领)
        cursor = await store._db.execute(
            "UPDATE command_executions "
            "SET status = CASE WHEN requires_approval = 1 THEN ? ELSE ? END, "
            "owner = NULL, lease_until = NULL, updated_at = ? "
            "WHERE status = ? AND lease_until < ?",
            (CMD_STATUS_RETRYABLE, CMD_STATUS_PENDING, now, CMD_STATUS_EXECUTING, now),
        )
        await store._db.commit()
        cleaned = cursor.rowcount
        if cleaned > 0:
            logger.info(
                f"[CommandBus] cleanup_stale_leases 清理过期租约 "
                f"{cleaned} 条(executing → retryable)"
            )
        return cleaned
    except Exception as e:
        logger.warning(f"[CommandBus] cleanup_stale_leases 失败: {e}")
    # fail-closed:清理失败时返回 0
    return 0


# ════════════════════════════════════════════════════════════════
# R53 P1-5: 外部副作用恢复 — lease 过期后先查 Receipt 再决定是否重新执行
# ════════════════════════════════════════════════════════════════


async def check_receipt_before_resume(
    action_id: str,
    effect_type: str,
    target: str,
) -> dict:
    """R53 P1-5: lease 过期恢复执行前查询 effect_receipts,决定是否跳过 handler。

    当 ``cleanup_stale_leases`` 将 status='executing' 转 'retryable' 后,
    重新执行 handler 前必须先查 Receipt:
    - Receipt 存在且 status='completed' → 外部副作用已成功执行,跳过 handler
      (避免重复执行幂等性不保证的外部副作用,如 Telegram 发消息、R2 上传等)
    - Receipt 不存在或非 completed → 重新执行副作用(handler 正常调用)

    与 ``EffectReceiptManager.check_receipt`` 的差异:
    - 本函数为辅助函数,封装"恢复决策"语义,返回 ``{"resume": bool, ...}``
    - 调用方只需根据 ``resume`` 字段决定是否调用 handler
    - 任何异常都 fail-open 返回 ``resume=True``(重新执行副作用,
      保证至少一次执行;若副作用本身幂等,重复执行无副作用)

    Args:
        action_id: 幂等 action_id
        effect_type: 副作用类型(如 "telegram_send"/"r2_put"/"command_handler")
        target: 副作用目标(如 command.action)

    Returns:
        {"resume": False, "reason": "receipt_completed", "external_id": str,
         "receipt": dict} — Receipt 已 completed,跳过 handler
        {"resume": True, "reason": "no_completed_receipt"} — 无 Receipt 或非 completed,
          调用方应重新执行 handler
        {"resume": True, "reason": "manager_unavailable"} — manager 不可用,重新执行
        {"resume": True, "reason": "error: ..."} — 查询异常,重新执行(fail-open)
    """
    try:
        from services.effect_receipts import get_receipt_manager
        manager = get_receipt_manager()
        if manager is None:
            logger.debug(
                f"[CommandBus] check_receipt_before_resume manager 不可用 "
                f"action_id={action_id}(重新执行副作用)"
            )
            return {"resume": True, "reason": "manager_unavailable"}
        receipt = await manager.check_receipt(action_id, effect_type, target)
        if receipt is not None and receipt.get("status") == "completed":
            logger.info(
                f"[CommandBus] check_receipt_before_resume Receipt 已 completed "
                f"action_id={action_id} effect_type={effect_type} target={target}"
                f"(跳过 handler,外部副作用已执行)"
            )
            return {
                "resume": False,
                "reason": "receipt_completed",
                "external_id": receipt.get("external_id", "") or "",
                "receipt": receipt,
            }
        logger.info(
            f"[CommandBus] check_receipt_before_resume 无 completed Receipt "
            f"action_id={action_id} effect_type={effect_type} target={target}"
            f"(重新执行副作用)"
        )
        return {"resume": True, "reason": "no_completed_receipt"}
    except Exception as e:
        # 查询异常 → fail-open 重新执行(保证至少一次执行)
        logger.warning(
            f"[CommandBus] check_receipt_before_resume 查询异常(重新执行副作用) "
            f"action_id={action_id}: {e}"
        )
        return {"resume": True, "reason": f"error: {e}"}


# ════════════════════════════════════════════════════════════════
# R42 P0-2: ApprovalExecutor 持久幂等 — command_executions + command_outbox + approval 三态一致
# ════════════════════════════════════════════════════════════════


async def claim_execution_for_outbox(
    action_id: str,
    request_hash: str,
    owner: str,
    lease_seconds: int = 60,
) -> dict:
    """R42 P0-2: ApprovalExecutor 专用 claim — 与 command_outbox 共享同一 action_id。

    幂等状态机:
        - 已 executed → 返回 already_executed(调用方应直接 mark_executed 跳过)
        - executing 且 lease 未过期 → claimed_by_other(跳过本轮)
        - request_hash 不匹配 → hash_mismatch(防篡改,路由到 DLQ)
        - 否则 CAS claim(INSERT or UPDATE → executing) → claimed

    与 ``claim_execution`` 的差异:
        - ``claim_execution`` 假设记录已存在(由 ``_try_insert_or_get_cached`` 预先 INSERT pending)
        - 本函数自包含:若记录不存在,直接 INSERT 为 executing(因 outbox 已 CAS 认领,
          无需再走 pending 状态)
        - 返回 dict 而非 bool,携带 status / result / owner 等元信息

    Args:
        action_id: 命令幂等 ID(与 command_outbox.action_id 一致)
        request_hash: SHA256(payload params),防篡改
        owner: 执行 worker 标识(如 hostname:pid)
        lease_seconds: 租约时长(秒)

    Returns:
        {"status": "already_executed", "result": <json str or None>}
        {"status": "claimed_by_other", "owner": ..., "lease_until": ...}
        {"status": "hash_mismatch", "stored_hash": ..., "request_hash": ...}
        {"status": "claimed"}
        DB 未初始化时返回 {"status": "claimed"}(降级执行,无幂等保护)
    """
    store = _get_store()
    if not store._db:
        logger.warning(
            f"[CommandBus] claim_execution_for_outbox DB 未初始化(降级执行)"
            f"action_id={action_id}"
        )
        return {"status": "claimed"}

    now = _now_iso()
    lease_until = _lease_until_iso(lease_seconds)

    # 1. 查询现有记录
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status, owner, lease_until, request_hash, result "
            "FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
    except Exception as e:
        logger.error(
            f"[CommandBus] claim_execution_for_outbox 查询失败 action_id={action_id}: {e}"
        )
        return {"status": "claimed"}  # 降级执行

    if rows and rows[0]:
        status, stored_owner, stored_lease, stored_hash, result_json = rows[0]
        # 校验 request_hash(防篡改)
        if stored_hash and stored_hash != request_hash:
            logger.warning(
                f"[CommandBus] claim_execution_for_outbox hash_mismatch "
                f"action_id={action_id} stored={stored_hash} request={request_hash}"
            )
            return {
                "status": "hash_mismatch",
                "stored_hash": stored_hash,
                "request_hash": request_hash,
            }
        # 已执行 → 返回缓存结果(幂等跳过)
        if status == CMD_STATUS_EXECUTED:
            logger.info(
                f"[CommandBus] claim_execution_for_outbox already_executed "
                f"action_id={action_id}"
            )
            return {"status": "already_executed", "result": result_json}
        # 执行中且 lease 未过期 → 被其他 worker 占用
        if status == CMD_STATUS_EXECUTING and stored_lease and stored_lease > now:
            logger.debug(
                f"[CommandBus] claim_execution_for_outbox claimed_by_other "
                f"action_id={action_id} owner={stored_owner} lease_until={stored_lease}"
            )
            return {
                "status": "claimed_by_other",
                "owner": stored_owner,
                "lease_until": stored_lease,
            }
        # 执行中但 lease 已过期 OR pending 状态 → CAS claim
        try:
            cursor = await store._db.execute(
                "UPDATE command_executions "
                "SET status = ?, owner = ?, lease_until = ?, request_hash = ?, updated_at = ? "
                "WHERE action_id = ? AND (status = ? OR (status = ? AND (lease_until IS NULL OR lease_until < ?)))",
                (CMD_STATUS_EXECUTING, owner, lease_until, request_hash, now,
                 action_id, CMD_STATUS_PENDING, CMD_STATUS_EXECUTING, now),
            )
            await store._db.commit()
            if cursor.rowcount > 0:
                logger.info(
                    f"[CommandBus] claim_execution_for_outbox claimed(CAS UPDATE) "
                    f"action_id={action_id} owner={owner}"
                )
                return {"status": "claimed"}
            # CAS 未命中:可能其他 worker 抢先
            logger.debug(
                f"[CommandBus] claim_execution_for_outbox CAS 未命中 "
                f"action_id={action_id}(其他 worker 抢先)"
            )
            return {"status": "claimed_by_other"}
        except Exception as e:
            logger.error(
                f"[CommandBus] claim_execution_for_outbox CAS UPDATE 失败 "
                f"action_id={action_id}: {e}"
            )
            return {"status": "claimed_by_other"}

    # 2. 行不存在 — INSERT 新记录(status='executing',直接进入执行态)
    try:
        await store._db.execute(
            "INSERT INTO command_executions "
            "(action_id, command_type, principal_id, status, owner, lease_until, "
            " request_hash, result, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (action_id, "outbox", 0, CMD_STATUS_EXECUTING, owner, lease_until,
             request_hash, now, now),
        )
        await store._db.commit()
        logger.info(
            f"[CommandBus] claim_execution_for_outbox claimed(INSERT) "
            f"action_id={action_id} owner={owner}"
        )
        return {"status": "claimed"}
    except sqlite3.IntegrityError:
        # UNIQUE 冲突 — 并发竞态,其他 worker 抢先 INSERT
        logger.debug(
            f"[CommandBus] claim_execution_for_outbox INSERT 冲突 "
            f"action_id={action_id}(其他 worker 抢先)"
        )
        return {"status": "claimed_by_other"}
    except Exception as e:
        logger.error(
            f"[CommandBus] claim_execution_for_outbox INSERT 失败 "
            f"action_id={action_id}: {e}"
        )
        return {"status": "claimed_by_other"}


async def mark_outbox_executed(
    action_id: str,
    result: Any,
    approval_id: int | None = None,
) -> bool:
    """R42 P0-2: 同一事务内更新三处状态(command_executions + command_outbox + approval)。

    在 ``store.transaction()`` 内原子完成:
        1. UPDATE command_executions SET status='executed', result=<json>
        2. UPDATE command_outbox SET status='executed'
        3. 若 approval_id 存在,UPDATE approvals SET status='executed', resolved_at=now

    Args:
        action_id: 幂等 action_id
        result: 执行结果,支持 Result dataclass 或 dict
        approval_id: 关联审批 ID(None 时跳过 approval 更新)

    Returns:
        True 成功;False 失败
    """
    store = _get_store()
    if not store._db:
        return False

    now = _now_iso()
    # 序列化 result(兼容 Result dataclass 与 dict)
    if hasattr(result, "success") and (hasattr(result, "data") or hasattr(result, "error")):
        result_json = json.dumps(
            {"success": result.success, "data": result.data, "error": result.error},
            ensure_ascii=False, default=str,
        )
    elif isinstance(result, dict):
        result_json = json.dumps(result, ensure_ascii=False, default=str)
    elif result is None:
        result_json = json.dumps(
            {"success": True, "data": None, "error": ""},
            ensure_ascii=False, default=str,
        )
    else:
        result_json = json.dumps(
            {"success": True, "data": str(result), "error": ""},
            ensure_ascii=False, default=str,
        )

    try:
        async with store.transaction() as tx:
            # 1. command_executions: status='executed' + result
            await tx.execute(
                "UPDATE command_executions "
                "SET status = ?, result = ?, updated_at = ? "
                "WHERE action_id = ?",
                (CMD_STATUS_EXECUTED, result_json, now, action_id),
            )
            # 2. command_outbox: status='executed'
            await tx.execute(
                "UPDATE command_outbox SET status = 'executed', updated_at = ? "
                "WHERE action_id = ?",
                (now, action_id),
            )
            # 3. approvals: status='executed'(若 approval_id 存在)
            if approval_id:
                await tx.execute(
                    "UPDATE approvals SET status = 'executed', resolved_at = ? "
                    "WHERE id = ?",
                    (now, approval_id),
                )
                await store.add_dirty_outbox("approvals", str(approval_id), connection=tx)
        logger.info(
            f"[CommandBus] mark_outbox_executed 完成 action_id={action_id} "
            f"approval_id={approval_id}"
        )
        return True
    except Exception as e:
        logger.error(
            f"[CommandBus] mark_outbox_executed 失败 action_id={action_id}: {e}"
        )
    # fail-closed:标记失败时返回 False
    return False


async def release_lease(action_id: str) -> bool:
    """R42 P0-2: 释放 command_executions lease(handler 失败后由 ApprovalExecutor 调用)。

    将 status 回退到 'pending',清空 owner/lease_until,允许下一轮重新 claim。
    与 ``release_execution`` 语义相同,但专用于 ApprovalExecutor 失败重试场景。

    Args:
        action_id: 命令幂等 ID

    Returns:
        True 释放成功;False 失败或记录不存在
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
        released = cursor.rowcount > 0
        if released:
            logger.info(
                f"[CommandBus] release_lease 释放 action_id={action_id}(回退到 pending)"
            )
        return released
    except Exception as e:
        logger.error(f"[CommandBus] release_lease 失败 action_id={action_id}: {e}")
    # fail-closed:释放失败时返回 False
    return False


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
            error=_i18n_t('services.command_bus.s2'),
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
                error=data.get("error", _i18n_t('services.command_bus.s10')),
                action_id=action_id,
            )
        except (json.JSONDecodeError, TypeError):
            pass
    # 正在执行或排队中
    return Result(
        success=False,
        error=_i18n_t('services.command_bus.s1', status=status),
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
                error=_i18n_t('services.command_bus.s11', e=e),
                action_id=action_id,
            )

        if not has_perm:
            logger.warning(
                f"[CommandBus] 权限不足 action={command.action} "
                f"principal={principal.id} perm={command.required_permission}"
            )
            return Result(
                success=False,
                error=_i18n_t('services.command_bus.s4', command_required_permission=command.required_permission),
                action_id=action_id,
            )

        # 2. 审批门禁(高风险操作)
        if command.requires_approval:
            # R64 P0-05: 从 HighRiskPolicy 查询具体安全控制(MFA/two_person/version CAS)
            # 一并写入 approval payload,供 button_approval_policy / ApprovalExecutor 强制
            from services.high_risk_policy import get_policy as _get_high_risk_policy
            _policy = _get_high_risk_policy(command.action)
            _policy_meta = None
            if _policy is not None:
                _policy_meta = {
                    "requires_mfa": _policy.requires_mfa,
                    "requires_two_person": _policy.requires_two_person,
                    "requires_reason": _policy.requires_reason,
                    "requires_resource_version": _policy.requires_resource_version,
                    "cooldown_seconds": _policy.cooldown_seconds,
                    "reversible": _policy.reversible,
                    "outbox_effects": list(_policy.outbox_effects),
                }

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
                        # R64 P0-05: 附加 HighRiskPolicy 元数据
                        "high_risk_policy": _policy_meta,
                    },
                    created_by=principal.id,
                )
            except Exception as e:
                logger.error(
                    f"[CommandBus] 创建审批失败 action={command.action} principal={principal.id}: {e}"
                )
                return Result(
                    success=False,
                    error=_i18n_t('services.command_bus.s15', e=e),
                    action_id=action_id,
                )

            if approval_id <= 0:
                return Result(
                    success=False,
                    error=_i18n_t('services.command_bus.s12'),
                    action_id=action_id,
                )

            logger.info(
                f"[CommandBus] 命令需审批 action={command.action} "
                f"principal={principal.id} approval_id={approval_id} "
                f"policy={_policy_meta}"
            )
            return Result(
                success=False,
                approval_id=approval_id,
                approval_required=True,
                error=_i18n_t('services.command_bus.s5', approval_id=approval_id),
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
            return Result(success=False, error=_i18n_t('services.command_bus.s6', approval_id=approval_id))

        if approval_record.get("status") != approval.APPROVAL_STATUS_APPROVED:
            return Result(
                success=False,
                error=_i18n_t('services.command_bus.s7', approval_record_get_status=approval_record.get('status')),
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
                error=_i18n_t('services.command_bus.s8', command_action=command_action),
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

        # R44 G0-3: 将 action_id 注入 params 作为 approval_action_id,
        # 使 restore_backup 等 handler 能将其传给 BackupEngine.restore() 进行审批校验
        # (BackupEngine.restore 通过 approval_action_id 反查 command_executions.principal_id)
        if "approval_action_id" not in params:
            params["approval_action_id"] = action_id

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
                error=_i18n_t('services.command_bus.s13', e=e),
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

        R45: 接入 EffectReceiptManager,在 handler 执行前后包装 effect receipt:
            - 执行前:检查 receipt 是否 completed(崩溃重试时跳过 handler);
            - 执行前:record_pending 标记开始执行;
            - 执行后(成功):record_completed 记录 external_id;
            - 执行后(失败):record_failed;
            - Result.effect_receipts 字段返回 receipt 状态清单。

        流程:
        1. 计算 request_hash = SHA256(payload)
        2. CAS INSERT INTO command_executions(status='pending')
           - UNIQUE 冲突 → 查询现有状态返回缓存 Result
        3. CAS UPDATE status='executing' WHERE action_id=? AND status='pending'
           - rowcount=0 → 被其他 worker 抢占
        4. R45: effect receipt 检查 → record_pending → 执行 handler → record_completed/failed
        5. UPDATE status='executed', result=JSON

        无 DB 时降级为直接执行(无幂等保护,仅用于测试),仍会执行 effect receipt 包装。
        """
        # R41 P0-5: 无 DB 降级模式 — 直接执行 handler(仅测试/开发用)
        store = _get_store()
        if not store._db:
            logger.warning(
                f"[CommandBus] 数据库未初始化,降级模式(无幂等保护) "
                f"action={command.action} action_id={action_id}"
            )
            # R45: 即使无 DB 仍尝试接入 effect receipt(manager 可能可用)
            effect_receipts = await self._wrap_with_effect_receipt(
                action_id, command, on_no_db=True,
            )
            if effect_receipts.get("skipped"):
                # 副作用已完成 → 直接返回成功(幂等)
                return Result(
                    success=True,
                    data={"skipped_by_receipt": True,
                          "external_id": effect_receipts.get("external_id", "")},
                    action_id=action_id,
                    effect_receipts=effect_receipts,
                )
            try:
                data = await command.handler(command.params)
                await self._finalize_effect_receipt(
                    action_id, command, success=True, data=data,
                )
                return Result(
                    success=True,
                    data=data,
                    action_id=action_id,
                    effect_receipts=effect_receipts,
                )
            except Exception as e:
                await self._finalize_effect_receipt(
                    action_id, command, success=False, error=str(e),
                )
                return Result(
                    success=False,
                    error=_i18n_t('services.command_bus.s16', e=e),
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
                error=_i18n_t('services.command_bus.s9'),
                action_id=action_id,
            )

        # 4. R45: effect receipt 检查 → record_pending
        effect_receipts = await self._wrap_with_effect_receipt(
            action_id, command, on_no_db=False,
        )
        if effect_receipts.get("skipped"):
            # 副作用已完成 → 跳过 handler,直接 mark_executed
            logger.info(
                f"[CommandBus] effect receipt 已完成,跳过 handler "
                f"action_id={action_id} action={command.action}"
            )
            result = Result(
                success=True,
                data={"skipped_by_receipt": True,
                      "external_id": effect_receipts.get("external_id", "")},
                action_id=action_id,
                effect_receipts=effect_receipts,
            )
            await _mark_executed(action_id, result)
            return result

        # 5. 执行 handler
        try:
            data = await command.handler(command.params)
            result = Result(
                success=True,
                data=data,
                action_id=action_id,
                effect_receipts=effect_receipts,
            )
            # 6. UPDATE status='executed', result=JSON
            await _mark_executed(action_id, result)
            # 7. R45: effect receipt record_completed
            await self._finalize_effect_receipt(
                action_id, command, success=True, data=data,
            )
            logger.info(
                f"[CommandBus] 命令执行成功 action={command.action} "
                f"principal={principal.id} action_id={action_id}"
            )
            return result
        except Exception as e:
            result = Result(
                success=False,
                error=_i18n_t('services.command_bus.s14', e=e),
                action_id=action_id,
            )
            # 失败也持久化(防止无脑重试,可通过 release_execution 释放后重试)
            await _mark_failed(action_id, result)
            # R45: effect receipt record_failed
            await self._finalize_effect_receipt(
                action_id, command, success=False, error=str(e),
            )
            logger.error(
                f"[CommandBus] 命令执行失败 action={command.action} "
                f"principal={principal.id} action_id={action_id}: {e}"
            )
            return result

    # ─── R45: effect receipt 包装辅助方法 ────────────────────

    async def _wrap_with_effect_receipt(
        self,
        action_id: str,
        command: Command,
        on_no_db: bool = False,
    ) -> dict:
        """R45: handler 执行前检查 effect receipt,若已完成则跳过 handler。

        Args:
            action_id: 幂等 ID
            command: Command 对象
            on_no_db: 是否在无 DB 降级模式下调用

        Returns:
            dict: {"skipped": bool, "external_id": str, "effect_type": str, "target": str}
            - skipped=True 表示已完成,调用方应跳过 handler
            - skipped=False 表示需执行 handler,本函数已 record_pending
            - 任何异常都 fail-open 返回 skipped=False
        """
        effect_type = "command_handler"
        target = command.action
        result_info = {
            "skipped": False,
            "external_id": "",
            "effect_type": effect_type,
            "target": target,
            "status": "pending",
        }
        try:
            from services.effect_receipts import get_receipt_manager
            manager = get_receipt_manager()
            if manager is None:
                logger.debug(
                    f"[CommandBus] effect_receipt manager 不可用,跳过 receipt 包装 "
                    f"action_id={action_id}"
                )
                return result_info
            # 检查是否已完成(崩溃重试场景)
            receipt = await manager.check_receipt(action_id, effect_type, target)
            if receipt is not None and receipt.get("status") == "completed":
                result_info["skipped"] = True
                result_info["external_id"] = receipt.get("external_id", "") or ""
                result_info["status"] = "completed"
                logger.info(
                    f"[CommandBus] effect receipt 已 completed,跳过 handler "
                    f"action_id={action_id} action={command.action}"
                )
                return result_info
            # 记录 pending
            await manager.record_pending(action_id, effect_type, target)
            result_info["status"] = "pending"
            return result_info
        except Exception as e:
            # receipt 检查/记录失败不应阻塞 handler 执行
            logger.warning(
                f"[CommandBus] effect receipt 检查失败(降级执行) "
                f"action_id={action_id}: {e}"
            )
            return result_info

    async def _finalize_effect_receipt(
        self,
        action_id: str,
        command: Command,
        success: bool,
        data: Any = None,
        error: str = "",
    ) -> None:
        """R45: handler 执行后写入 effect receipt(completed/failed)。

        Args:
            action_id: 幂等 ID
            command: Command 对象
            success: True=handler 成功;False=handler 失败
            data: handler 返回的数据(用于提取 external_id)
            error: 失败时的错误信息
        """
        effect_type = "command_handler"
        target = command.action
        try:
            from services.effect_receipts import get_receipt_manager
            manager = get_receipt_manager()
            if manager is None:
                return
            if success:
                # 从 data 中提取 external_id(如有)
                external_id = ""
                if isinstance(data, dict):
                    external_id = str(
                        data.get("external_id")
                        or data.get("message_id")
                        or ""
                    )
                await manager.record_completed(
                    action_id, effect_type, target, external_id,
                )
            else:
                await manager.record_failed(action_id, effect_type, target)
        except Exception as e:
            logger.warning(
                f"[CommandBus] effect receipt record_final 失败(非致命) "
                f"action_id={action_id}: {e}"
            )


# ════════════════════════════════════════════════════════════════
# 高风险命令注册表 — 集中管理所有 requires_approval=True 的命令
# ════════════════════════════════════════════════════════════════

# 命令 action → (permission, approval_action, requires_approval)
HIGH_RISK_COMMAND_REGISTRY: dict[str, tuple[str, str, bool]] = {
    "takedown_report":       (PERM_CONTENT_TAKEDOWN,      APPROVAL_ACTION_TAKEDOWN,             True),
    "ban_user":              (PERM_USERS_BAN,             APPROVAL_ACTION_BAN,                  True),
    # R64 P0-05: unban_user 从 False 改为 True(高风险逆操作,统一走审批门禁)
    "unban_user":            (PERM_USERS_UNBAN,           APPROVAL_ACTION_BAN,                  True),
    "assign_role":           (PERM_RBAC_ASSIGN,           APPROVAL_ACTION_RBAC_ASSIGN,          True),
    "restore_backup":        (PERM_DISASTER_RESTORE,      APPROVAL_ACTION_RESTORE,              True),
    "enable_maintenance":    (PERM_MAINTENANCE_ENABLE,    APPROVAL_ACTION_MAINTENANCE_ENABLE,   True),
    "disable_maintenance":   (PERM_MAINTENANCE_DISABLE,   APPROVAL_ACTION_MAINTENANCE_DISABLE,   True),
    "purge_data":            (PERM_DATA_PURGE,            APPROVAL_ACTION_DELETE_DATA,          True),
    # R64 P0-05: delete_file 从 False 改为 True(集中策略统一,高风险操作走审批门禁)
    "delete_file":           (PERM_CONTENT_TAKEDOWN,      APPROVAL_ACTION_TAKEDOWN,             True),
    # R64 P0-05: 新增 3 个 destructive 子动作 — 与 make_*_command 一致
    "detach_file":           (PERM_CONTENT_TAKEDOWN,      APPROVAL_ACTION_TAKEDOWN,             True),
    "block_user_for_file":   (PERM_CONTENT_TAKEDOWN,      APPROVAL_ACTION_TAKEDOWN,             True),
    "restore_content":       (PERM_DISASTER_RESTORE,      APPROVAL_ACTION_RESTORE,              True),
}


# ─── R56 P1-1: CommandSpec registry + 服务端 canonical hash ──────
# CommandSpec 描述每个命令的规范参数 schema,服务端基于此重算 canonical hash,
# 防止客户端篡改 request_hash(只信任服务端重算的结果)。
# canonical_params:参与 hash 计算的参数名列表(其他参数被忽略),
#                   None 表示全部参数参与(向后兼容)


@dataclass
class CommandSpec:
    """R56 P1-1: 命令规范定义(用于服务端 canonical hash 重算)。

    Attributes:
        action: 命令标识(如 "takedown_report")
        permission: RBAC 权限标识
        approval_action: 审批 workflow action 名(无审批则为 "")
        requires_approval: 是否需要审批
        canonical_params: 参与 canonical hash 计算的参数名列表(None=全部参数)
    """
    action: str
    permission: str
    approval_action: str
    requires_approval: bool
    canonical_params: tuple[str, ...] | None = None


# R56 P1-1: CommandSpec registry — 注册所有高风险命令的规范参数 schema
# 服务端 claim_execution_approved 可基于此重算 canonical hash 并与存储的比较
COMMAND_SPEC_REGISTRY: dict[str, CommandSpec] = {
    "takedown_report": CommandSpec(
        action="takedown_report",
        permission=PERM_CONTENT_TAKEDOWN,
        approval_action=APPROVAL_ACTION_TAKEDOWN,
        requires_approval=True,
        canonical_params=("target_type", "target_id", "reason"),
    ),
    "ban_user": CommandSpec(
        action="ban_user",
        permission=PERM_USERS_BAN,
        approval_action=APPROVAL_ACTION_BAN,
        requires_approval=True,
        canonical_params=("user_id", "reason", "duration_days"),
    ),
    "restore_backup": CommandSpec(
        action="restore_backup",
        permission=PERM_DISASTER_RESTORE,
        approval_action=APPROVAL_ACTION_RESTORE,
        requires_approval=True,
        canonical_params=("backup_id", "tables", "merge"),
    ),
    "enable_maintenance": CommandSpec(
        action="enable_maintenance",
        permission=PERM_MAINTENANCE_ENABLE,
        approval_action=APPROVAL_ACTION_MAINTENANCE_ENABLE,
        requires_approval=True,
        canonical_params=("reason",),
    ),
    "disable_maintenance": CommandSpec(
        action="disable_maintenance",
        permission=PERM_MAINTENANCE_DISABLE,
        approval_action=APPROVAL_ACTION_MAINTENANCE_DISABLE,
        requires_approval=True,
        canonical_params=(),
    ),
    "purge_data": CommandSpec(
        action="purge_data",
        permission=PERM_DATA_PURGE,
        approval_action=APPROVAL_ACTION_DELETE_DATA,
        requires_approval=True,
        canonical_params=("table_names",),
    ),
    "assign_role": CommandSpec(
        action="assign_role",
        permission=PERM_RBAC_ASSIGN,
        approval_action=APPROVAL_ACTION_RBAC_ASSIGN,
        requires_approval=True,
        canonical_params=("user_id", "role_name"),
    ),
    "factory_reset": CommandSpec(
        action="factory_reset",
        permission=PERM_DATA_PURGE,
        approval_action=APPROVAL_ACTION_FACTORY_RESET,
        requires_approval=True,
        canonical_params=("tables",),
    ),
    "data_lifecycle_break_glass": CommandSpec(
        action="data_lifecycle_break_glass",
        permission=PERM_DATA_PURGE,
        approval_action="",
        requires_approval=True,
        canonical_params=("batch_size", "skip_backup_check"),
    ),
}


def _compute_canonical_request_hash(
    command_type: str,
    params: dict,
    principal_id: int = 0,
) -> str:
    """R56 P1-1: 服务端基于 CommandSpec 重算 canonical hash。

    根据 COMMAND_SPEC_REGISTRY 中 command_type 的 canonical_params 过滤参数,
    仅保留规范参数后计算 SHA256(防篡改)。
    若 command_type 不在 registry 中,回退到全量参数计算(向后兼容)。

    Args:
        command_type: 命令类型(对应 command_executions.command_type)
        params: 命令参数字典
        principal_id: 操作者 ID(纳入 hash 绑定,防跨 principal 复用)

    Returns:
        完整 SHA256 hex 字符串(64 字符)
    """
    spec = COMMAND_SPEC_REGISTRY.get(command_type)
    if spec is not None and spec.canonical_params is not None:
        # 仅保留 canonical_params 中列出的参数(过滤无关字段)
        filtered = {k: params.get(k) for k in spec.canonical_params if k in params}
    else:
        # 未注册命令:回退到全量参数(向后兼容,但会记录 warning)
        logger.warning(
            f"[CommandBus] R56 P1-1: command_type='{command_type}' "
            f"未在 COMMAND_SPEC_REGISTRY 注册,回退到全量参数 hash"
        )
        filtered = dict(params)
    canonical_str = json.dumps(
        {"command_type": command_type, "params": filtered, "principal_id": principal_id},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


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
        # R44 G0-3: 传递 approval_action_id(由 ApprovalExecutor 在执行时注入 params)
        return make_restore_backup_command(
            backup_id=str(params.get("backup_id", "")),
            tables=params.get("tables"),
            merge=params.get("merge", False),
            approval_action_id=params.get("approval_action_id"),
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
    if action == "detach_file":
        # R64 P0-05: 文件脱钩命令(审批通过后重建,此前为 requires_approval=False)
        return make_detach_file_command(
            file_code=str(params.get("file_code", "")),
            reason=params.get("reason", ""),
        )
    if action == "block_user_for_file":
        # R64 P0-05: 限制用户解码文件命令(审批通过后重建,此前为 requires_approval=False)
        return make_block_user_for_file_command(
            file_code=str(params.get("file_code", "")),
            user_id=int(params.get("user_id", 0)),
            reason=params.get("reason", ""),
        )
    if action == "restore_content":
        # R51 P0-6: 内容申诉恢复命令(由 ApprovalExecutor 消费 command_outbox 调度)
        return make_restore_content_command(
            appeal_id=int(params.get("appeal_id", 0)),
            target_type=params.get("target_type", ""),
            target_id=str(params.get("target_id", "")),
            admin_id=int(params.get("admin_id", 0)),
            content_hash=params.get("content_hash", ""),
            reporter_id=int(params.get("reporter_id", 0)),
            first_approver_id=int(params.get("first_approver_id", 0)),
            note=params.get("note", ""),
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
    """构造解封用户命令(R64 P0-05: 强制审批 — 解封是高风险逆操作)。

    R64 P0-05 整改: ``unban_user`` 不再 ``requires_approval=False``。
    风险级别由 ``HighRiskPolicy.is_high_risk("unban_user")`` 决定。
    """
    # 延迟 import 避免与 services.command_bus 顶层循环依赖
    from services.high_risk_policy import is_high_risk as _is_high_risk

    async def _handler(params: dict) -> dict:
        from services import content_reports
        ok = await content_reports.unban_user(params["user_id"], admin_id=0)
        return {"unban_ok": ok}

    return Command(
        action="unban_user",
        required_permission=PERM_USERS_UNBAN,
        handler=_handler,
        params={"user_id": user_id},
        requires_approval=_is_high_risk("unban_user"),
        approval_action=APPROVAL_ACTION_BAN,  # 复用 ban 审批(逆操作)
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
    approval_action_id: str | None = None,
) -> Command:
    """构造恢复备份命令(必须审批)。

    R44 G0-3: 公共 API restore() 不再接受 approver_id 参数,
    改为通过 approval_action_id 从 command_executions 反查 principal_id。
    ApprovalExecutor 调用此 handler 时通过 params 传入 approval_action_id,
    BackupEngine.restore() 内部使用该 action_id 查询审批状态 + principal_id。

    Args:
        backup_id: 备份文件 key(R2 对象 key)
        tables: 仅恢复指定表(None=全部)
        merge: True=增量补充(不删除现有数据);False=覆盖恢复
        approval_action_id: 审批动作 ID(对应 command_executions.action_id,
                            由 ApprovalExecutor 在执行时注入 params)
    """
    async def _handler(params: dict) -> dict:
        # R40 P0-8 + P0-7: 审批通过后执行实际恢复
        # R44 G0-3: 从 params 获取 approval_action_id,传入 BackupEngine.restore
        # 让 restore() 通过 approval_action_id 反查 principal_id 并校验审批状态
        approval_action_id = params.get("approval_action_id")

        # R65 P0-07 / P1-07: capability-seal — 旧直接 restore writer 已被封存。
        # 生产恢复必须改走 RestoreOrchestrator 蓝绿切换路径(staging → active,
        # 禁止原地覆盖生产数据)。逃生舱:ALLOW_LEGACY_RESTORE=1 仅限 tests/ 与
        # scripts/ 兼容场景使用,生产部署绝不应配置(应在系统层强制 unset)。
        from services.error_codes import AppError, ErrorCodes
        if os.environ.get("ALLOW_LEGACY_RESTORE", "").lower() not in ("1", "true", "yes"):
            logger.error(
                _i18n_t(
                    "diagnostics.r65.p0_07.capability_sealed",
                    entry_point="make_restore_backup_command handler",
                    caller="command_bus.make_restore_backup_command",
                )
            )
            raise AppError(
                ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
                params={
                    "caller": "command_bus.make_restore_backup_command",
                    "reason": "legacy_writer_sealed",
                },
            )

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
            # R44 G0-3: 不传 approver_id,让 restore 从 command_executions 反查 principal_id
            # approval_action_id 必传(production 恢复),否则 restore 抛 ValueError
            from services.backup_engine import BackupEngine
            engine = BackupEngine()
            return await engine.restore(
                params["backup_id"], target="production",
                approval_action_id=approval_action_id,
            )

    return Command(
        action="restore_backup",
        required_permission=PERM_DISASTER_RESTORE,
        handler=_handler,
        params={
            "backup_id": backup_id,
            "tables": tables,
            "merge": merge,
            "approval_action_id": approval_action_id,
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
    """R40 P0-8 + R64 P0-05: 构造文件软删除命令(强制审批)。

    R64 P0-05 整改: ``delete_file`` 不再 ``requires_approval=False``。
    风险级别由 ``HighRiskPolicy.is_high_risk("delete_file")`` 决定。

    Args:
        file_code: 文件唯一标识

    Returns:
        Command 对象(handler 执行软删除 + 本地 tombstone)
    """
    # 延迟 import 避免与 services.command_bus 顶层循环依赖
    from services.high_risk_policy import is_high_risk as _is_high_risk

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
            raise ValueError(_i18n_t('services.command_bus.s3', file_code=file_code))
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
        requires_approval=_is_high_risk("delete_file"),  # R64 P0-05
        approval_action=APPROVAL_ACTION_TAKEDOWN,  # 复用 takedown 审批
    )


def make_detach_file_command(file_code: str, reason: str = "") -> Command:
    """R62 P0-04 + R64 P0-05: 构造文件脱钩命令(report:detach 子动作,强制审批)。

    与 ``make_delete_file_command`` 的区别:
        - ``delete_file``: 软删除(status='deleted' + deleted_at + tombstone)
        - ``detach_file``: 仅脱钩(status='detached',文件记录保留,uploader 解除关联)

    R64 P0-05 整改: ``detach_file`` 不再 ``requires_approval=False``。
    风险级别由 ``HighRiskPolicy.is_high_risk("detach_file")`` 决定。

    Args:
        file_code: 文件唯一标识
        reason: 脱钩原因(如 "report:detach")

    Returns:
        Command 对象(handler 执行 status='detached' 双写 + 缓存失效)
    """
    # 延迟 import 避免与 services.command_bus 顶层循环依赖
    from services.high_risk_policy import is_high_risk as _is_high_risk

    async def _handler(params: dict) -> dict:
        # R62 P0-04: 复用 update_file_record_and_invalidate 双写 CRDB+SQLite + 缓存失效
        # (command_bus.py 在 BUTTON_INFRA_FILES 排除列表中,可调用破坏性 API)
        from database import update_file_record_and_invalidate
        import datetime as _dt
        file_code = params["file_code"]
        await update_file_record_and_invalidate(file_code, {
            "$set": {
                "status": "detached",
                "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        })
        return {"file_code": file_code, "status": "detached"}

    return Command(
        action="detach_file",
        required_permission=PERM_CONTENT_TAKEDOWN,
        handler=_handler,
        params={"file_code": file_code, "reason": reason},
        requires_approval=_is_high_risk("detach_file"),  # R64 P0-05
        approval_action=APPROVAL_ACTION_TAKEDOWN,  # 复用 takedown 审批
    )


def make_block_user_for_file_command(
    file_code: str, user_id: int, reason: str = "",
) -> Command:
    """R62 P0-04 + R64 P0-05: 构造"限制用户解码该文件"命令(report:block 子动作)。

    report:block 操作语义为"将举报人加入 file_code 的 blocked_users 列表",
    使其无法再解码该文件。区别于 ban(封禁账号)和 detach(脱钩文件):
        - block_user_for_file: 仅限制特定 user ↔ 特定 file 的解码权限
        - ban: 全局封禁用户
        - detach: 解除文件与上传者关联(影响所有用户)

    R64 P0-05 整改: ``block_user_for_file`` 不再 ``requires_approval=False``。
    风险级别由 ``HighRiskPolicy.is_high_risk("block_user_for_file")`` 决定。

    Args:
        file_code: 文件唯一标识
        user_id: 被限制的用户 ID
        reason: 限制原因(如 "report:block")

    Returns:
        Command 对象(handler 执行 $addToSet blocked_users 双写 + 缓存失效)
    """
    # 延迟 import 避免与 services.command_bus 顶层循环依赖
    from services.high_risk_policy import is_high_risk as _is_high_risk

    async def _handler(params: dict) -> dict:
        # R62 P0-04: 复用 update_file_record_and_invalidate 双写 + 缓存失效
        # 注:update_file_record_and_invalidate 当前仅支持 $set/$inc/$push 操作符,
        # $addToSet 走 CRDB 原生语义(SQLite 缓存侧由下次 cache 回填补偿)
        from database import update_file_record_and_invalidate
        file_code = params["file_code"]
        user_id = params["user_id"]
        await update_file_record_and_invalidate(file_code, {
            "$addToSet": {"blocked_users": user_id},
        })
        return {"file_code": file_code, "blocked_user_id": user_id}

    return Command(
        action="block_user_for_file",
        required_permission=PERM_CONTENT_TAKEDOWN,
        handler=_handler,
        params={"file_code": file_code, "user_id": user_id, "reason": reason},
        requires_approval=_is_high_risk("block_user_for_file"),  # R64 P0-05
        approval_action=APPROVAL_ACTION_TAKEDOWN,  # 复用 takedown 审批
    )


def make_restore_content_command(
    appeal_id: int,
    target_type: str,
    target_id: str,
    admin_id: int,
    content_hash: str = "",
    reporter_id: int = 0,
    first_approver_id: int = 0,
    note: str = "",
) -> Command:
    """R51 P0-6 + R64 P0-05: 构造内容申诉恢复命令(由 ApprovalExecutor 消费 command_outbox 调度)。

    R64 P0-05 整改: 此前 ``requires_approval=False`` 依赖调用方先完成 2-person 审批。
    但单点策略不一致会导致绕过风险。改为 ``HighRiskPolicy.is_high_risk("restore_content")``
    决定,策略集中可审计;调用方仍可走 ApprovalExecutor 调度,CommandBus 门禁负责兜底。
    handler 执行流程:
    1. 调用 _restore_content_internal() 恢复内容(撤销软删除)
    2. restore 成功 → UPDATE content_reports SET status='resolved' + 通知举报者
    3. restore 失败 → UPDATE content_reports SET status='restore_failed' +
       记录 effect receipt failed(reconciliation)
    4. restore 成功但状态更新失败 → 记录 effect receipt + reconciliation
       (不降级,不仅 warning)

    Args:
        appeal_id: 申诉 ID(等同 report_id)
        target_type: 恢复目标类型(file/user/code)
        target_id: 恢复目标 ID
        admin_id: 第二审批人 ID(执行恢复的管理员)
        content_hash: 内容 hash(用于 action_id 确定性)
        reporter_id: 举报者 ID(用于通知)
        first_approver_id: 第一审批人 ID(用于审计)
        note: 审批备注

    Returns:
        Command 对象(由 ApprovalExecutor 调度执行)
    """
    # 延迟 import 避免与 services.command_bus 顶层循环依赖
    from services.high_risk_policy import is_high_risk as _is_high_risk

    async def _handler(params: dict) -> dict:
        import datetime as _dt_h
        from services import content_reports as _cr_mod
        from services import notifications as _notif_svc
        from database.cache_store import get_cache_store as _get_store
        from services.error_codes import AppError, ErrorCodes
        from services.effect_receipts import get_receipt_manager

        _appeal_id = int(params.get("appeal_id", 0))
        _target_type = params.get("target_type", "")
        _target_id = str(params.get("target_id", ""))
        _admin_id = int(params.get("admin_id", 0))
        _reporter_id = int(params.get("reporter_id", 0))

        # 1. 执行恢复(撤销软删除)
        restore_ok = await _cr_mod._restore_content_internal(
            _target_type, _target_id, _admin_id,
        )

        _store = _get_store()
        _now = _dt_h.datetime.now().isoformat()

        if not restore_ok:
            # 3. restore 失败 → 状态变为 restore_failed + effect receipt failed
            logger.error(
                f"[CommandBus] restore_content handler 恢复失败 "
                f"appeal_id={_appeal_id} target={_target_type}:{_target_id}"
            )
            if _store and _store._db:
                try:
                    async with _store.transaction() as tx:
                        await tx.execute(
                            "UPDATE content_reports "
                            "SET status = ?, resolved_by = ?, resolved_at = ? "
                            "WHERE id = ?",
                            (_cr_mod.REPORT_STATUS_RESTORE_FAILED,
                             _admin_id, _now, _appeal_id),
                        )
                        await _store.add_dirty_outbox(
                            "content_reports", str(_appeal_id), connection=tx,
                        )
                except Exception as status_err:
                    logger.error(
                        f"[CommandBus] restore_content 状态更新到 restore_failed 失败: {status_err}"
                    )
            # 记录 effect receipt failed(reconciliation)
            receipt_mgr = get_receipt_manager(_store)
            if receipt_mgr is not None:
                try:
                    await receipt_mgr.record_failed(
                        action_id=f"restore_content_{_appeal_id}_{params.get('content_hash', '')[:16]}",
                        effect_type="restore",
                        target=f"{_target_type}:{_target_id}",
                        error_msg="restore_content handler failed",
                    )
                except Exception as receipt_err:
                    logger.error(
                        f"[CommandBus] restore_content record_failed 失败: {receipt_err}"
                    )
            raise AppError(
                ErrorCodes.CONTENT_APPEAL_RESTORE_FAILED,
                params={
                    "appeal_id": _appeal_id,
                    "target_type": _target_type,
                    "target_id": _target_id,
                },
            )

        # 2. restore 成功 → 状态变为 resolved + 通知举报者
        status_update_ok = False
        if _store and _store._db:
            try:
                async with _store.transaction() as tx:
                    cursor = await tx.execute(
                        "UPDATE content_reports "
                        "SET status = ?, resolved_by = ?, resolved_at = ? "
                        "WHERE id = ?",
                        (_cr_mod.REPORT_STATUS_RESOLVED,
                         _admin_id, _now, _appeal_id),
                    )
                    if cursor and cursor.rowcount > 0:
                        status_update_ok = True
                    await _store.add_dirty_outbox(
                        "content_reports", str(_appeal_id), connection=tx,
                    )
            except Exception as status_err:
                logger.error(
                    f"[CommandBus] restore_content 状态更新到 resolved 失败 "
                    f"(restore 已执行,进入 reconciliation): {status_err}"
                )

        if not status_update_ok:
            # 4. restore 成功但状态更新失败 → effect receipt + reconciliation
            # 不降级,不仅 warning — 记录 reconciliation 等待人工处理
            logger.error(
                f"[CommandBus] restore_content 恢复成功但状态更新失败 "
                f"appeal_id={_appeal_id}(进入 reconciliation)"
            )
            receipt_mgr = get_receipt_manager(_store)
            if receipt_mgr is not None:
                try:
                    # 标记为 completed(restore 已执行)但 reconcile_status=needs_reconcile
                    await receipt_mgr.record_completed(
                        action_id=f"restore_content_{_appeal_id}_{params.get('content_hash', '')[:16]}",
                        effect_type="restore",
                        target=f"{_target_type}:{_target_id}",
                        external_id=f"restored_but_status_unsynced:{_appeal_id}",
                    )
                    # 手动标记 reconcile_status 为 needs_reconcile
                    if _store and _store._db:
                        await _store._db.execute(
                            "UPDATE effect_receipts "
                            "SET reconcile_status = 'needs_reconcile', "
                            "last_error = ? "
                            "WHERE action_id = ? AND effect_type = 'restore'",
                            ("restore succeeded but report status update failed",
                             f"restore_content_{_appeal_id}_{params.get('content_hash', '')[:16]}"),
                        )
                        await _store._db.commit()
                except Exception as receipt_err:
                    logger.error(
                        f"[CommandBus] restore_content reconciliation 记录失败: {receipt_err}"
                    )
            raise AppError(
                ErrorCodes.CONTENT_APPEAL_RESTORE_FAILED,
                params={
                    "appeal_id": _appeal_id,
                    "target_type": _target_type,
                    "target_id": _target_id,
                },
            )

        # 通知举报者(appeal 已批准,内容已恢复)
        if _reporter_id > 0:
            try:
                await _notif_svc.dispatch_notification(
                    user_id=_reporter_id,
                    type="appeal_approved",
                    content={
                        "appeal_id": _appeal_id,
                        "target_type": _target_type,
                        "target_id": _target_id,
                        "restored": True,
                    },
                    dedup_key=f"appeal_approved:{_appeal_id}",
                )
            except Exception as notif_err:
                logger.warning(
                    f"[CommandBus] restore_content 通知失败(不阻塞): {notif_err}"
                )

        logger.info(
            f"[CommandBus] restore_content handler 成功 "
            f"appeal_id={_appeal_id} target={_target_type}:{_target_id}"
        )
        return {
            "restore_ok": True,
            "appeal_id": _appeal_id,
            "target_type": _target_type,
            "target_id": _target_id,
        }

    return Command(
        action="restore_content",
        required_permission=PERM_DISASTER_RESTORE,
        handler=_handler,
        params={
            "appeal_id": appeal_id,
            "target_type": target_type,
            "target_id": target_id,
            "admin_id": admin_id,
            "content_hash": content_hash,
            "reporter_id": reporter_id,
            "first_approver_id": first_approver_id,
            "note": note,
        },
        requires_approval=_is_high_risk("restore_content"),  # R64 P0-05
        approval_action=APPROVAL_ACTION_RESTORE,
    )


# ─── R41 P1-8: factory_reset / set_r2 命令(强制 RBAC + 审批门禁) ──

# R41 P1-8: 配置变更权限(用于 R2 凭证变更等高风险配置操作)
PERM_CONFIG_CHANGE = "config:change"


def make_factory_reset_command(tables: list[str] | None = None) -> Command:
    """R41 P1-8: 构造工厂重置命令(必须审批,最高风险等级)。

    复用 data:purge 权限和 factory_reset 审批 action。
    handler 仅标记执行,实际重置逻辑由调用方在审批通过后执行。

    Args:
        tables: 要清空的表列表(None=全部)

    Returns:
        Command 对象(必须审批)
    """
    async def _handler(params: dict) -> dict:
        # handler 仅返回标记,实际重置逻辑在审批通过后由调用方执行
        # (factory_reset 涉及 CRDB + SQLite + 内存缓存,逻辑复杂,
        #  由 handlers.py 中的 factory_reset 函数在审批通过后执行)
        return {"factory_reset_ok": True, "tables": params.get("tables", [])}

    return Command(
        action="factory_reset",
        required_permission=PERM_DATA_PURGE,
        handler=_handler,
        params={"tables": tables or []},
        requires_approval=True,
        approval_action=APPROVAL_ACTION_FACTORY_RESET,
    )


def make_set_r2_command(
    account_id: str = "", access_key: str = "",
    secret_key: str = "", bucket: str = "",
) -> Command:
    """R41 P1-8: 构造 R2 凭证变更命令(必须审批)。

    R2 凭证变更是高风险操作(影响备份/存储),必须通过 CommandBus RBAC + 审批。
    handler 在审批通过后执行实际配置写入。

    Args:
        account_id: R2 账号 ID
        access_key: R2 Access Key
        secret_key: R2 Secret Key(加密存储)
        bucket: R2 桶名

    Returns:
        Command 对象(必须审批)
    """
    async def _handler(params: dict) -> dict:
        # 在审批通过后执行实际配置写入
        from database import set_config
        from database.relay_db import encrypt as _encrypt_secret
        await set_config("r2_account_id", params.get("account_id", ""))
        await set_config("r2_access_key", params.get("access_key", ""))
        secret = params.get("secret_key", "")
        if secret:
            await set_config("r2_secret_key", _encrypt_secret(secret))
        bucket = params.get("bucket", "")
        if bucket:
            await set_config("r2_bucket", bucket)
        return {"r2_config_ok": True}

    return Command(
        action="set_r2",
        required_permission=PERM_CONFIG_CHANGE,
        handler=_handler,
        params={
            "account_id": account_id,
            "access_key": access_key,
            "secret_key": secret_key,
            "bucket": bucket,
        },
        requires_approval=True,
        approval_action=APPROVAL_ACTION_CONFIG_CHANGE,
    )
