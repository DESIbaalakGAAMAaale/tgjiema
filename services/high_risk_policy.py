"""R64 P0-05: 统一高风险操作策略。

所有 destructive action 必须通过 HighRiskPolicy 查询所需的安全控制:
required_role / MFA / two_person / reason / resource_version / cooldown / reversible / outbox_effects。

handler 不再自行决定风险级别,只能构造命令并交给 policy/CommandBus。

设计要点:
    - 单一策略表 HIGH_RISK_POLICY:action → HighRiskRule
    - 所有 destructive action 默认 requires_mfa=True + requires_two_person=True
      (R64 P0-05 整改:不再有 requires_approval=False 的 destructive action)
    - get_policy(action) 返回 HighRiskRule 或 None(非高风险)
    - is_high_risk(action) 用于 CommandBus 工厂函数决定 requires_approval
    - 与 button_security.HIGH_RISK_ACTIONS / button_approval_policy 协同:
        * HIGH_RISK_ACTIONS 是底层 effect_type 集合(用于 callback 签名格式门禁)
        * HIGH_RISK_POLICY 是 command 级别的策略表(用于 CommandBus 审批门禁)

R65 P0-05 整改:
    - 对 destructive namespace 使用 fail-closed:未知 destructive action 抛
      HIGH_RISK.ACTION.UNREGISTERED,不再返回 None/False
    - destructive namespace = 显式关键词集合(delete/purge/ban/block/takedown/
      detach/restore/reset/rotate/assign/revoke/grant/enable/disable/wipe/clear/
      shutdown/restart/factory_reset/break_glass/force_logout/approve_appeal/
      reject_appeal/update_config/reload_config) + button_security.HIGH_RISK_ACTIONS
    - 非 destructive action(view/cancel/refresh/list/get/query/read/search/ping 等)
      仍返回 None/False(只读/查询类操作)
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

# 复用 command_bus 中的权限常量,避免重复定义
from services.command_bus import (
    PERM_CONFIG_CHANGE,
    PERM_CONTENT_TAKEDOWN,
    PERM_DATA_PURGE,
    PERM_DISASTER_RESTORE,
    PERM_MAINTENANCE_DISABLE,
    PERM_MAINTENANCE_ENABLE,
    PERM_RBAC_ASSIGN,
    PERM_USERS_BAN,
    PERM_USERS_UNBAN,
)

from services.i18n import translate as _i18n_t


@dataclass(frozen=True)
class HighRiskRule:
    """单个高风险操作的策略规则。

    Attributes:
        action: 命令标识(如 "delete_file" / "ban_user"),与 CommandBus 工厂函数对应
        required_role: RBAC 权限标识(如 PERM_CONTENT_TAKEDOWN)
        requires_mfa: 是否强制 MFA(R64 P0-05:所有 destructive action 默认 True)
        requires_two_person: 是否强制双人审批(requester != approver + approver MFA)
        requires_reason: 是否强制填写操作原因
        requires_resource_version: 是否强制 resource version CAS(防旧按钮操作已更新资源)
        cooldown_seconds: 冷却时间(秒),0=无冷却(防止短时间内重复执行)
        reversible: 是否可逆(有 compensation 动作,如 unban 是 ban 的逆操作)
        outbox_effects: 关联的 outbox effect types(用于 effect receipt 跟踪)
        approval_action: 对应 approval_workflow 的 action 名(空串表示复用默认)
    """
    action: str
    required_role: str
    requires_mfa: bool
    requires_two_person: bool  # requester != approver + 两个独立 approver
    requires_reason: bool
    requires_resource_version: bool  # resource version CAS
    cooldown_seconds: int  # 0 = 无冷却
    reversible: bool  # 是否可逆(有 compensation)
    outbox_effects: tuple[str, ...]  # 关联的 outbox effect types
    approval_action: str = ""


# ════════════════════════════════════════════════════════════════
# 统一策略表 — R64 P0-05 整改
# ════════════════════════════════════════════════════════════════
# 关键整改点:
#   1. 所有 destructive action 的 requires_mfa=True + requires_two_person=True
#      (不再有 requires_approval=False 的 destructive action)
#   2. delete / ban / detach / block / restore / purge / 密钥轮换 / 权限变更
#      默认 requires_resource_version=True(resource version CAS)
#   3. handler 不再自行决定风险级别,只能通过本表查询
# ════════════════════════════════════════════════════════════════

HIGH_RISK_POLICY: dict[str, HighRiskRule] = {
    # ── 文件删除/脱钩/限制(reuse content:takedown 权限)──────────
    "delete_file": HighRiskRule(
        action="delete_file",
        required_role=PERM_CONTENT_TAKEDOWN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=False,  # 软删除有 tombstone,但物理删除不可逆
        outbox_effects=("r2_delete", "soft_delete"),
        approval_action="takedown",  # 复用 APPROVAL_ACTION_TAKEDOWN
    ),
    "detach_file": HighRiskRule(
        action="detach_file",
        required_role=PERM_CONTENT_TAKEDOWN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # 可重新关联
        outbox_effects=("file_detach",),
        approval_action="takedown",
    ),
    "block_user_for_file": HighRiskRule(
        action="block_user_for_file",
        required_role=PERM_CONTENT_TAKEDOWN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # 可解除 block
        outbox_effects=("file_block",),
        approval_action="takedown",
    ),
    # ── 账号封禁/解封 ────────────────────────────────────────────
    "ban_user": HighRiskRule(
        action="ban_user",
        required_role=PERM_USERS_BAN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # unban_user 是逆操作
        outbox_effects=("user_ban",),
        approval_action="ban",  # APPROVAL_ACTION_BAN
    ),
    "unban_user": HighRiskRule(
        action="unban_user",
        required_role=PERM_USERS_UNBAN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # ban_user 是逆操作
        outbox_effects=("user_unban",),
        approval_action="ban",  # 复用 ban 审批(逆操作)
    ),
    # ── 内容下架/恢复 ────────────────────────────────────────────
    "takedown_report": HighRiskRule(
        action="takedown_report",
        required_role=PERM_CONTENT_TAKEDOWN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # restore_content 是逆操作
        outbox_effects=("content_takedown",),
        approval_action="takedown",  # APPROVAL_ACTION_TAKEDOWN
    ),
    "restore_content": HighRiskRule(
        action="restore_content",
        required_role=PERM_DISASTER_RESTORE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # takedown 是逆操作
        outbox_effects=("content_restore",),
        approval_action="restore",  # APPROVAL_ACTION_RESTORE
    ),
    # ── 灾备恢复 ─────────────────────────────────────────────────
    "restore_backup": HighRiskRule(
        action="restore_backup",
        required_role=PERM_DISASTER_RESTORE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=300,  # 5 分钟冷却(防止短时间重复恢复)
        reversible=False,  # 备份恢复不可逆(覆盖生产数据)
        outbox_effects=("db_restore", "r2_download"),
        approval_action="restore",  # APPROVAL_ACTION_RESTORE
    ),
    # ── 数据清除 ─────────────────────────────────────────────────
    "purge_data": HighRiskRule(
        action="purge_data",
        required_role=PERM_DATA_PURGE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=600,  # 10 分钟冷却(物理清除高危)
        reversible=False,  # 物理清除不可逆
        outbox_effects=("crdb_delete", "r2_delete"),
        approval_action="delete_data",  # APPROVAL_ACTION_DELETE_DATA
    ),
    # ── 权限变更 ─────────────────────────────────────────────────
    "assign_role": HighRiskRule(
        action="assign_role",
        required_role=PERM_RBAC_ASSIGN,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=0,
        reversible=True,  # 可重新分配角色
        outbox_effects=("rbac_assign",),
        approval_action="rbac_assign",  # APPROVAL_ACTION_RBAC_ASSIGN
    ),
    # ── 密钥轮换 ─────────────────────────────────────────────────
    "rotate_keys": HighRiskRule(
        action="rotate_keys",
        required_role=PERM_CONFIG_CHANGE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=True,
        cooldown_seconds=60,  # 1 分钟冷却
        reversible=False,  # 密钥轮换不可逆(旧密钥作废)
        outbox_effects=("key_rotate",),
        approval_action="config_change",  # APPROVAL_ACTION_CONFIG_CHANGE
    ),
    # ── 维护模式 ─────────────────────────────────────────────────
    "enable_maintenance": HighRiskRule(
        action="enable_maintenance",
        required_role=PERM_MAINTENANCE_ENABLE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=False,  # 系统级状态,无 resource version
        cooldown_seconds=0,
        reversible=True,  # disable_maintenance 是逆操作
        outbox_effects=("maintenance_enable",),
        approval_action="maintenance_enable",  # APPROVAL_ACTION_MAINTENANCE_ENABLE
    ),
    "disable_maintenance": HighRiskRule(
        action="disable_maintenance",
        required_role=PERM_MAINTENANCE_DISABLE,
        requires_mfa=True,
        requires_two_person=True,
        requires_reason=True,
        requires_resource_version=False,  # 系统级状态,无 resource version
        cooldown_seconds=0,
        reversible=True,  # enable_maintenance 是逆操作
        outbox_effects=("maintenance_disable",),
        approval_action="maintenance_disable",  # APPROVAL_ACTION_MAINTENANCE_DISABLE
    ),
}


# ════════════════════════════════════════════════════════════════
# R65 P0-05: destructive namespace 定义 — fail-closed 关键词集合
# ════════════════════════════════════════════════════════════════
# 任何 action 名匹配以下关键词之一,即属于 destructive namespace,
# 必须在 HIGH_RISK_POLICY 中显式注册(否则 fail-closed 抛
# HIGH_RISK.ACTION.UNREGISTERED,防止新 destructive action 被误判为低风险)。
# 关键词来源:
#   1. HIGH_RISK_POLICY 已注册的 13 个 action 的核心语义
#   2. button_security.HIGH_RISK_ACTIONS 集合(effect_type 维度)
#   3. 行业通用 destructive 关键词(delete/purge/drop/wipe 等)
# 非 destructive action(view/cancel/refresh/list/get/query/read/search/ping/
# readiness/export_status/health_check 等)不会匹配以下关键词,返回 None/False。
DESTRUCTIVE_ACTION_KEYWORDS: tuple[str, ...] = (
    # 删除/清除/销毁
    "delete", "purge", "drop", "truncate", "destroy", "wipe", "clear", "scrub",
    # 封禁/屏蔽/踢出
    "ban", "unban", "block", "kick",
    # 下架/分离/移除
    "takedown", "detach", "remove",
    # 恢复/还原(覆盖生产数据,高风险)
    "restore", "recover",
    # 重置/轮换(密钥/配额等敏感操作)
    "reset", "rotate",
    # 权限授予/撤销
    "assign", "revoke", "grant",
    # 状态变更(维护/启用/禁用,影响可用性)
    "enable", "disable",
    # 工厂重置/紧急访问/强制登出
    "factory_reset", "break_glass", "force_logout",
    # 申诉审批(影响其他用户权益)
    "approve_appeal", "reject_appeal",
    # 配置变更
    "update_config", "reload_config",
    # 关闭/重启
    "shutdown", "restart",
)


def _is_destructive_namespace(action: str) -> bool:
    """R65 P0-05: 检查 action 是否属于 destructive namespace。

    判定规则:
        1. action 名(小写)包含 DESTRUCTIVE_ACTION_KEYWORDS 中任一关键词 → True
        2. action 名精确匹配 button_security.HIGH_RISK_ACTIONS 集合 → True
        3. 其他 → False(只读/查询类操作)

    Args:
        action: 命令标识

    Returns:
        True 表示该 action 属于 destructive namespace,必须在 HIGH_RISK_POLICY 注册
    """
    if not action:
        return False
    action_lower = action.lower().strip()
    # 1. 关键词匹配(action 包含 destructive 关键词)
    for keyword in DESTRUCTIVE_ACTION_KEYWORDS:
        if keyword in action_lower:
            return True
    # 2. 精确匹配 button_security.HIGH_RISK_ACTIONS(effect_type 集合)
    # 延迟导入避免循环依赖
    try:
        from services.button_security import HIGH_RISK_ACTIONS
        if action_lower in HIGH_RISK_ACTIONS:
            return True
    except ImportError as e:
        # button_security 不可用时,仅依赖关键词匹配(不影响主流程)
        logger.warning(_i18n_t('diagnostics.r65.p1_04.high_risk_button_security_unavailable', error=e))
    return False


def _raise_unregistered(action: str) -> None:
    """R65 P0-05: 抛 HIGH_RISK.ACTION.UNREGISTERED 异常(fail-closed)。

    Args:
        action: 未注册的 destructive action 名

    Raises:
        AppError: 始终抛出 HIGH_RISK_ACTION_UNREGISTERED
    """
    from services.error_codes import AppError, ErrorCodes
    raise AppError(
        ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED,
        params={
            "action": action,
            "reason": (
                "action matches destructive namespace but is not registered in "
                "HIGH_RISK_POLICY; register it explicitly or rename to avoid "
                "destructive keywords (delete/purge/ban/block/takedown/detach/"
                "restore/reset/rotate/assign/revoke/grant/enable/disable/...)"
            ),
        },
    )


# ════════════════════════════════════════════════════════════════
# 查询接口
# ════════════════════════════════════════════════════════════════


def get_policy(action: str) -> HighRiskRule | None:
    """查询 action 的高风险策略。

    R65 P0-05: 对 destructive namespace 使用 fail-closed。
    若 action 属于 destructive namespace 但未在 HIGH_RISK_POLICY 中注册,
    抛 HIGH_RISK.ACTION.UNREGISTERED 异常(不返回 None)。

    Args:
        action: 命令标识(如 "delete_file" / "ban_user")

    Returns:
        HighRiskRule 对象;返回 None 表示非高风险操作(只读/查询类)

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    rule = HIGH_RISK_POLICY.get(action)
    if rule is not None:
        return rule
    # action 不在 HIGH_RISK_POLICY 中
    if _is_destructive_namespace(action):
        _raise_unregistered(action)
    # 非 destructive action,返回 None(只读/查询类)
    return None


def is_high_risk(action: str) -> bool:
    """判断 action 是否为高风险操作。

    CommandBus 工厂函数用此函数决定 requires_approval:
        requires_approval = HighRiskPolicy.is_high_risk(action)

    R65 P0-05: 对 destructive namespace fail-closed。
    若 action 属于 destructive namespace 但未在 HIGH_RISK_POLICY 中注册,
    抛 HIGH_RISK.ACTION.UNREGISTERED 异常(不返回 False)。

    Args:
        action: 命令标识

    Returns:
        True 表示该 action 在 HIGH_RISK_POLICY 表中(必须走审批门禁)

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    if action in HIGH_RISK_POLICY:
        return True
    if _is_destructive_namespace(action):
        _raise_unregistered(action)
    return False


def get_required_role(action: str) -> str | None:
    """查询 action 所需的 RBAC 角色。

    R65 P0-05: 对 destructive namespace fail-closed。

    Args:
        action: 命令标识

    Returns:
        RBAC 权限标识;非高风险 action 返回 None

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    rule = get_policy(action)  # 复用 get_policy 的 fail-closed 逻辑
    return rule.required_role if rule else None


def requires_mfa(action: str) -> bool:
    """查询 action 是否强制 MFA。

    R65 P0-05: 对 destructive namespace fail-closed。

    Args:
        action: 命令标识

    Returns:
        True 表示需要 MFA;非高风险 action 返回 False

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    rule = get_policy(action)  # 复用 get_policy 的 fail-closed 逻辑
    return rule.requires_mfa if rule else False


def requires_two_person(action: str) -> bool:
    """查询 action 是否强制双人审批。

    R65 P0-05: 对 destructive namespace fail-closed。

    Args:
        action: 命令标识

    Returns:
        True 表示需要双人审批(requester != approver + approver MFA);
        非高风险 action 返回 False

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    rule = get_policy(action)  # 复用 get_policy 的 fail-closed 逻辑
    return rule.requires_two_person if rule else False


def requires_resource_version(action: str) -> bool:
    """查询 action 是否强制 resource version CAS。

    R65 P0-05: 对 destructive namespace fail-closed。

    Args:
        action: 命令标识

    Returns:
        True 表示需要 resource version 绑定;非高风险 action 返回 False

    Raises:
        AppError(HIGH_RISK_ACTION_UNREGISTERED): 当 action 属于 destructive
            namespace 但未在 HIGH_RISK_POLICY 中注册时
    """
    rule = get_policy(action)  # 复用 get_policy 的 fail-closed 逻辑
    return rule.requires_resource_version if rule else False


# ════════════════════════════════════════════════════════════════
# 模块导出
# ════════════════════════════════════════════════════════════════

__all__ = [
    "HighRiskRule",
    "HIGH_RISK_POLICY",
    "DESTRUCTIVE_ACTION_KEYWORDS",
    "get_policy",
    "is_high_risk",
    "get_required_role",
    "requires_mfa",
    "requires_two_person",
    "requires_resource_version",
]
