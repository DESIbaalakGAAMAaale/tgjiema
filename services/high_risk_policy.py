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
"""
from __future__ import annotations

from dataclasses import dataclass

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
# 查询接口
# ════════════════════════════════════════════════════════════════


def get_policy(action: str) -> HighRiskRule | None:
    """查询 action 的高风险策略。

    Args:
        action: 命令标识(如 "delete_file" / "ban_user")

    Returns:
        HighRiskRule 对象;返回 None 表示非高风险操作
    """
    return HIGH_RISK_POLICY.get(action)


def is_high_risk(action: str) -> bool:
    """判断 action 是否为高风险操作。

    CommandBus 工厂函数用此函数决定 requires_approval:
        requires_approval = HighRiskPolicy.is_high_risk(action)

    Args:
        action: 命令标识

    Returns:
        True 表示该 action 在 HIGH_RISK_POLICY 表中(必须走审批门禁)
    """
    return action in HIGH_RISK_POLICY


def get_required_role(action: str) -> str | None:
    """查询 action 所需的 RBAC 角色。

    Args:
        action: 命令标识

    Returns:
        RBAC 权限标识;非高风险 action 返回 None
    """
    rule = HIGH_RISK_POLICY.get(action)
    return rule.required_role if rule else None


def requires_mfa(action: str) -> bool:
    """查询 action 是否强制 MFA。

    Args:
        action: 命令标识

    Returns:
        True 表示需要 MFA;非高风险 action 返回 False
    """
    rule = HIGH_RISK_POLICY.get(action)
    return rule.requires_mfa if rule else False


def requires_two_person(action: str) -> bool:
    """查询 action 是否强制双人审批。

    Args:
        action: 命令标识

    Returns:
        True 表示需要双人审批(requester != approver + approver MFA);
        非高风险 action 返回 False
    """
    rule = HIGH_RISK_POLICY.get(action)
    return rule.requires_two_person if rule else False


def requires_resource_version(action: str) -> bool:
    """查询 action 是否强制 resource version CAS。

    Args:
        action: 命令标识

    Returns:
        True 表示需要 resource version 绑定;非高风险 action 返回 False
    """
    rule = HIGH_RISK_POLICY.get(action)
    return rule.requires_resource_version if rule else False


# ════════════════════════════════════════════════════════════════
# 模块导出
# ════════════════════════════════════════════════════════════════

__all__ = [
    "HighRiskRule",
    "HIGH_RISK_POLICY",
    "get_policy",
    "is_high_risk",
    "get_required_role",
    "requires_mfa",
    "requires_two_person",
    "requires_resource_version",
]
