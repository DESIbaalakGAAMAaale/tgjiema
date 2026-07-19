"""R64 P1-08: 按钮式流程 UX 完整性策略。

审计 P1-08 要求:
    每个 destructive action 必须先显示目标、影响范围、不可逆性、审批状态和取消按钮;
    Telegram 按钮标签需中英一致、避免仅 emoji 表意;
    错误后提供可聚焦/可操作的重试或返回按钮;
    token 失效、资源版本冲突、审批过期、MFA 过期均应回到可恢复流程,
    而不是死端文字。

本模块作为 destructive action UX 规范的单一事实源:
    1. ``ButtonUXSpec`` dataclass(frozen) — 描述确认面板必须显示的字段
    2. ``ButtonUXPolicy`` — 根据 action + locale 返回对应 spec
    3. 错误恢复映射 — 4 种典型错误码对应的恢复按钮 key

与 high_risk_policy / button_approval_policy 协同:
    - destructive action 集合来源于 HIGH_RISK_POLICY(R64 P0-05 创建)
    - i18n key 通过 button.ux.* 命名空间查询
    - 渲染交由 button_ux_renderer 完成,本模块只负责策略声明
"""
from __future__ import annotations

from dataclasses import dataclass

from services.error_codes import AppError, ErrorCodes
from services.high_risk_policy import HIGH_RISK_POLICY, HighRiskRule, get_policy


# ════════════════════════════════════════════════════════════════
# 1. ButtonUXSpec — 确认面板必须显示的字段集
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ButtonUXSpec:
    """R64 P1-08: destructive action 确认面板 UX 规范。

    所有字段都是 i18n key 或语义标识,渲染时由 button_ux_renderer 解析。
    frozen=True 保证策略对象不可变(防止 handler 篡改 spec 绕过 UX 要求)。

    Attributes:
        action: 命令标识(如 "delete_file" / "ban_user")
        target_display: 目标展示 i18n key(渲染目标资源,如 file_code / user_id)
        impact_scope: 影响范围 i18n key(描述波及范围,如 "影响所有解码记录")
        irreversibility_notice: 不可逆性提示 i18n key(描述是否可恢复)
        approval_status_display: 审批状态展示 i18n key(双人审批 / 单人执行)
        cancel_button_label: 取消按钮 i18n key(必须存在,不允许死路)
        confirm_button_label: 确认按钮 i18n key
        locale: 目标 locale(zh-CN / en-US)
        requires_mfa_badge: 是否需要 MFA 标识(渲染时显示 "需要 MFA")
    """
    action: str
    target_display: str
    impact_scope: str
    irreversibility_notice: str
    approval_status_display: str
    cancel_button_label: str
    confirm_button_label: str
    locale: str
    requires_mfa_badge: bool


# ════════════════════════════════════════════════════════════════
# 2. i18n key 常量(中英一致的 key,值由 locales/{locale}.json 提供)
# ════════════════════════════════════════════════════════════════

# destructive confirmation 通用 key(所有 destructive action 共享)
KEY_TARGET_DISPLAY = "button.ux.destructive.target_display"
KEY_IMPACT_SCOPE = "button.ux.destructive.impact_scope"
KEY_IRREVERSIBILITY_NOTICE = "button.ux.destructive.irreversibility_notice"
KEY_APPROVAL_STATUS = "button.ux.destructive.approval_status"
KEY_CANCEL = "button.ux.destructive.cancel"
KEY_CONFIRM = "button.ux.destructive.confirm"

# 错误恢复按钮 key
KEY_RECOVERY_RETRY = "button.ux.recovery.retry"
KEY_RECOVERY_CANCEL = "button.ux.recovery.cancel"
KEY_RECOVERY_RESUBMIT = "button.ux.recovery.resubmit"  # token 失效
KEY_RECOVERY_RELOAD = "button.ux.recovery.reload"  # 资源版本冲突
KEY_RECOVERY_REAPPLY_APPROVAL = "button.ux.recovery.reapply_approval"  # 审批过期
KEY_RECOVERY_REPLAY_MFA = "button.ux.recovery.replay_mfa"  # MFA 过期


# ════════════════════════════════════════════════════════════════
# 3. 影响范围 / 不可逆性 per-action i18n key 后缀
# ════════════════════════════════════════════════════════════════
# 每个 destructive action 有专属的影响范围与不可逆性描述。
# 命名规则: button.ux.destructive.{action}.{impact_scope|irreversibility|approval_status}
# 通过此映射在 locales 文件中查找 per-action 文案。

_ACTION_IMPACT_SCOPE_KEY: dict[str, str] = {
    "delete_file": "button.ux.destructive.delete_file.impact_scope",
    "detach_file": "button.ux.destructive.detach_file.impact_scope",
    "block_user_for_file": "button.ux.destructive.block_user_for_file.impact_scope",
    "ban_user": "button.ux.destructive.ban_user.impact_scope",
    "unban_user": "button.ux.destructive.unban_user.impact_scope",
    "restore_content": "button.ux.destructive.restore_content.impact_scope",
    "purge": "button.ux.destructive.purge.impact_scope",
    "purge_data": "button.ux.destructive.purge_data.impact_scope",
    "rotate_keys": "button.ux.destructive.rotate_keys.impact_scope",
    "change_permissions": "button.ux.destructive.change_permissions.impact_scope",
    "assign_role": "button.ux.destructive.assign_role.impact_scope",
}

_ACTION_IRREVERSIBILITY_KEY: dict[str, str] = {
    "delete_file": "button.ux.destructive.delete_file.irreversibility",
    "detach_file": "button.ux.destructive.detach_file.irreversibility",
    "block_user_for_file": "button.ux.destructive.block_user_for_file.irreversibility",
    "ban_user": "button.ux.destructive.ban_user.irreversibility",
    "unban_user": "button.ux.destructive.unban_user.irreversibility",
    "restore_content": "button.ux.destructive.restore_content.irreversibility",
    "purge": "button.ux.destructive.purge.irreversibility",
    "purge_data": "button.ux.destructive.purge_data.irreversibility",
    "rotate_keys": "button.ux.destructive.rotate_keys.irreversibility",
    "change_permissions": "button.ux.destructive.change_permissions.irreversibility",
    "assign_role": "button.ux.destructive.assign_role.irreversibility",
}

_ACTION_APPROVAL_STATUS_KEY: dict[str, str] = {
    "delete_file": "button.ux.destructive.delete_file.approval_status",
    "detach_file": "button.ux.destructive.detach_file.approval_status",
    "block_user_for_file": "button.ux.destructive.block_user_for_file.approval_status",
    "ban_user": "button.ux.destructive.ban_user.approval_status",
    "unban_user": "button.ux.destructive.unban_user.approval_status",
    "restore_content": "button.ux.destructive.restore_content.approval_status",
    "purge": "button.ux.destructive.purge.approval_status",
    "purge_data": "button.ux.destructive.purge_data.approval_status",
    "rotate_keys": "button.ux.destructive.rotate_keys.approval_status",
    "change_permissions": "button.ux.destructive.change_permissions.approval_status",
    "assign_role": "button.ux.destructive.assign_role.approval_status",
}


# ════════════════════════════════════════════════════════════════
# 4. 错误恢复映射 — 4 种典型错误码对应的恢复按钮 key
# ════════════════════════════════════════════════════════════════
# token 失效(已使用 / 过期 / principal 不匹配) → 重新发起
# 资源版本冲突(resource_version 不匹配) → 重新加载
# 审批过期(approval 过期) → 重新申请审批
# MFA 过期(receipt age 超限) → 重新 MFA

# 错误码 → 恢复按钮 i18n key
ERROR_RECOVERY_KEY_MAP: dict[str, str] = {
    # token 失效类(BUTTON_POLICY_NONCE_CONSUMED / BUTTON_POLICY_EXPIRED /
    #             BUTTON_POLICY_PRINCIPAL_MISMATCH)
    "BUTTON_POLICY_NONCE_CONSUMED": KEY_RECOVERY_RESUBMIT,
    "BUTTON_POLICY_EXPIRED": KEY_RECOVERY_RESUBMIT,
    "BUTTON_POLICY_PRINCIPAL_MISMATCH": KEY_RECOVERY_RESUBMIT,
    # 资源版本冲突
    "BUTTON_POLICY_VERSION_MISMATCH": KEY_RECOVERY_RELOAD,
    # 审批过期 / 审批相关错误
    "BUTTON_POLICY_DUAL_APPROVAL_REQUIRED": KEY_RECOVERY_REAPPLY_APPROVAL,
    "APPROVAL_EXPIRED": KEY_RECOVERY_REAPPLY_APPROVAL,
    "APPROVAL_INVALID": KEY_RECOVERY_REAPPLY_APPROVAL,
    # MFA 过期 / receipt age 超限
    "BUTTON_POLICY_MFA_REQUIRED": KEY_RECOVERY_REPLAY_MFA,
    "AUTH_MFA_RECEIPT_EXPIRED": KEY_RECOVERY_REPLAY_MFA,
    "AUTH_MFA_RECEIPT_INVALID": KEY_RECOVERY_REPLAY_MFA,
}

# 错误码 → 语义分类(供 renderer 决定按钮 callback action)
ERROR_RECOVERY_CATEGORY_MAP: dict[str, str] = {
    "BUTTON_POLICY_NONCE_CONSUMED": "resubmit",
    "BUTTON_POLICY_EXPIRED": "resubmit",
    "BUTTON_POLICY_PRINCIPAL_MISMATCH": "resubmit",
    "BUTTON_POLICY_VERSION_MISMATCH": "reload",
    "BUTTON_POLICY_DUAL_APPROVAL_REQUIRED": "reapply_approval",
    "APPROVAL_EXPIRED": "reapply_approval",
    "APPROVAL_INVALID": "reapply_approval",
    "BUTTON_POLICY_MFA_REQUIRED": "replay_mfa",
    "AUTH_MFA_RECEIPT_EXPIRED": "replay_mfa",
    "AUTH_MFA_RECEIPT_INVALID": "replay_mfa",
}


# ════════════════════════════════════════════════════════════════
# 5. ButtonUXPolicy — 策略查询入口
# ════════════════════════════════════════════════════════════════


# 审计 P1-08 + 任务说明中点名的 9 个 destructive action 子集
# (delete_file / ban_user / unban_user / detach_file / block_user_for_file /
#  restore_content / purge_data / rotate_keys / assign_role)
# 这 9 个 action 必须有专属 spec;HIGH_RISK_POLICY 中的其他 destructive
# action(restore_backup / takedown_report /
# enable_maintenance / disable_maintenance)也应有 spec,通过自动回退机制覆盖。
# 注: 审计原文用概念名 "purge"/"change_permissions",此处使用 HIGH_RISK_POLICY
# 中的规范 action 名(purge_data / assign_role),保证 has_ux_spec/destructive_confirmation
# 可直接查询,无需别名层。
REQUIRED_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "delete_file",
    "ban_user",
    "unban_user",
    "detach_file",
    "block_user_for_file",
    "restore_content",
    "purge_data",
    "rotate_keys",
    "assign_role",
})


class ButtonUXPolicy:
    """R64 P1-08: destructive action UX 策略查询入口。

    所有 destructive action 在渲染确认面板前必须调用本类的
    ``destructive_confirmation``,获取包含 target/impact/irreversibility/
    approval_status/cancel 的 ButtonUXSpec。

    用法:
        policy = ButtonUXPolicy()
        spec = policy.destructive_confirmation("delete_file", "file_abc", "zh-CN")
        # spec.cancel_button_label / spec.confirm_button_label / ...
    """

    def destructive_confirmation(
        self,
        action: str,
        target: str,
        locale: str = "zh-CN",
    ) -> ButtonUXSpec:
        """返回 destructive action 的确认面板 UX spec。

        Args:
            action: 命令标识(必须在 HIGH_RISK_POLICY 中)
            target: 目标资源标识(如 file_code / user_id),用于 target_display 渲染
            locale: 目标 locale(zh-CN / en-US)

        Returns:
            ButtonUXSpec — 包含所有必须显示的字段(目标/影响/不可逆性/
            审批状态/取消按钮/确认按钮)

        Raises:
            AppError: action 为空(BUTTON.UX.ACTION_REQUIRED)或
                action 不在 HIGH_RISK_POLICY 中(BUTTON.UX.ACTION_NOT_HIGH_RISK)
        """
        if not action:
            raise AppError(
                ErrorCodes.BUTTON_UX_ACTION_REQUIRED,
                params={"reason": "action 不能为空"},
            )
        rule: HighRiskRule | None = get_policy(action)
        if rule is None:
            raise AppError(
                ErrorCodes.BUTTON_UX_ACTION_NOT_HIGH_RISK,
                params={
                    "action": action,
                    "reason": "action 不在 HIGH_RISK_POLICY 中 — 非高风险 action 不需要 destructive confirmation 面板",
                },
            )
        # per-action i18n key(回退到通用 key 保证 always-defined)
        impact_key = _ACTION_IMPACT_SCOPE_KEY.get(action, KEY_IMPACT_SCOPE)
        irreversibility_key = _ACTION_IRREVERSIBILITY_KEY.get(action, KEY_IRREVERSIBILITY_NOTICE)
        approval_status_key = _ACTION_APPROVAL_STATUS_KEY.get(action, KEY_APPROVAL_STATUS)
        return ButtonUXSpec(
            action=action,
            target_display=f"{KEY_TARGET_DISPLAY}|{target}",
            impact_scope=impact_key,
            irreversibility_notice=irreversibility_key,
            approval_status_display=approval_status_key,
            cancel_button_label=KEY_CANCEL,
            confirm_button_label=KEY_CONFIRM,
            locale=locale,
            requires_mfa_badge=rule.requires_mfa,
        )

    def recovery_button_key(self, error_code: str) -> str:
        """返回错误码对应的恢复按钮 i18n key。

        Args:
            error_code: ErrorCodes 常量(如 "BUTTON_POLICY_NONCE_CONSUMED")

        Returns:
            恢复按钮 i18n key(如 button.ux.recovery.resubmit);
            未知错误码回退到 KEY_RECOVERY_RETRY(默认重试按钮)
        """
        return ERROR_RECOVERY_KEY_MAP.get(error_code, KEY_RECOVERY_RETRY)

    def recovery_button_category(self, error_code: str) -> str:
        """返回错误码对应的恢复按钮语义分类。

        用于 renderer 决定按钮的 callback action(如 "resubmit" / "reload" / ...)。

        Args:
            error_code: ErrorCodes 常量

        Returns:
            恢复分类字符串(如 "resubmit" / "reload" / "reapply_approval" /
            "replay_mfa" / "retry");未知错误码回退到 "retry"
        """
        return ERROR_RECOVERY_CATEGORY_MAP.get(error_code, "retry")


# ════════════════════════════════════════════════════════════════
# 6. 模块级便捷函数 + 单例
# ════════════════════════════════════════════════════════════════

_default_policy: ButtonUXPolicy | None = None


def get_button_ux_policy() -> ButtonUXPolicy:
    """获取默认 ButtonUXPolicy 单例(无状态,惰性创建)。"""
    global _default_policy
    if _default_policy is None:
        _default_policy = ButtonUXPolicy()
    return _default_policy


def has_ux_spec(action: str) -> bool:
    """检查 action 是否有对应的 ButtonUXSpec(用于门禁脚本)。

    Args:
        action: 命令标识

    Returns:
        True 表示 action 在 HIGH_RISK_POLICY 中(可生成 spec);
        False 表示非高风险 action(不需要 spec)
    """
    return action in HIGH_RISK_POLICY


def required_destructive_actions() -> frozenset[str]:
    """返回审计 P1-08 强制要求有 spec 的 9 个 destructive action 集合。

    门禁脚本(check_button_handler_gate.py / check_a11y_matrix_enforcement.py)
    用此函数验证完整性。
    """
    return REQUIRED_DESTRUCTIVE_ACTIONS


def get_all_specable_actions() -> frozenset[str]:
    """返回 HIGH_RISK_POLICY 中所有可生成 spec 的 action 集合。

    用于完整性检查 — 所有 destructive action 都应有 spec。
    """
    return frozenset(HIGH_RISK_POLICY.keys())


# ════════════════════════════════════════════════════════════════
# 模块导出
# ════════════════════════════════════════════════════════════════

__all__ = [
    # 数据类
    "ButtonUXSpec",
    # 策略类
    "ButtonUXPolicy",
    # i18n key 常量
    "KEY_TARGET_DISPLAY",
    "KEY_IMPACT_SCOPE",
    "KEY_IRREVERSIBILITY_NOTICE",
    "KEY_APPROVAL_STATUS",
    "KEY_CANCEL",
    "KEY_CONFIRM",
    "KEY_RECOVERY_RETRY",
    "KEY_RECOVERY_CANCEL",
    "KEY_RECOVERY_RESUBMIT",
    "KEY_RECOVERY_RELOAD",
    "KEY_RECOVERY_REAPPLY_APPROVAL",
    "KEY_RECOVERY_REPLAY_MFA",
    # 错误恢复映射
    "ERROR_RECOVERY_KEY_MAP",
    "ERROR_RECOVERY_CATEGORY_MAP",
    # 必填 action 集合
    "REQUIRED_DESTRUCTIVE_ACTIONS",
    # 便捷函数
    "get_button_ux_policy",
    "has_ux_spec",
    "required_destructive_actions",
    "get_all_specable_actions",
]
