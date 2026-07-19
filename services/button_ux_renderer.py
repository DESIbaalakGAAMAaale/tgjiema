"""R64 P1-08: 按钮式流程 UX 渲染器(Telegram 消息文本 + 恢复按钮)。

审计 P1-08 要求:
    - Telegram 按钮标签需中英一致、避免仅 emoji 表意
    - 错误后提供可聚焦/可操作的重试或返回按钮
    - token 失效、资源版本冲突、审批过期、MFA 过期均应回到可恢复流程

本模块只负责"渲染"(将 ButtonUXSpec + i18n_manager 转为可展示文本/按钮),
不持有任何业务策略 — 策略来源:
    - ButtonUXPolicy(services/button_ux_policy.py)
    - HIGH_RISK_POLICY(services/high_risk_policy.py)
    - i18n_manager(services/i18n.py)

设计原则:
    - 所有按钮标签必须通过 i18n key 查询,禁止硬编码中英文
    - 禁止仅 emoji 表意(emoji 必须配合文字标签)
    - 渲染输出纯文本(不含 HTML/Markdown 转义,由调用方按 sink 选择转义)
    - 与 services/sink_adapters/telegram_adapter.py 解耦 — renderer 只产
      (text, [buttons]),由调用方决定如何发送
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.button_ux_policy import (
    ButtonUXPolicy,
    ButtonUXSpec,
    ERROR_RECOVERY_CATEGORY_MAP,
    ERROR_RECOVERY_KEY_MAP,
    KEY_APPROVAL_STATUS,
    KEY_CANCEL,
    KEY_CONFIRM,
    KEY_IMPACT_SCOPE,
    KEY_IRREVERSIBILITY_NOTICE,
    KEY_RECOVERY_CANCEL,
    KEY_RECOVERY_REAPPLY_APPROVAL,
    KEY_RECOVERY_RELOAD,
    KEY_RECOVERY_REPLAY_MFA,
    KEY_RECOVERY_RESUBMIT,
    KEY_RECOVERY_RETRY,
    KEY_TARGET_DISPLAY,
)


# ════════════════════════════════════════════════════════════════
# 1. InlineKeyboardButton 描述符(sink-agnostic)
# ════════════════════════════════════════════════════════════════
# 不直接依赖 telegram.InlineKeyboardButton(避免 renderer 耦合 telegram 库),
# 而是用 dataclass 描述 (label, callback_data),由调用方转换为
# telegram.InlineKeyboardButton 或 Web <button> 元素。


@dataclass(frozen=True)
class InlineKeyboardButton:
    """R64 P1-08: sink-agnostic 按钮描述符。

    Attributes:
        label: 按钮显示文案(已 i18n 渲染,含中英文;禁止仅 emoji)
        callback_data: 按钮 callback payload(用于签名 token 绑定)
        category: 按钮语义分类(confirm/cancel/retry/resubmit/reload/
                  reapply_approval/replay_mfa)
    """
    label: str
    callback_data: str
    category: str


# ════════════════════════════════════════════════════════════════
# 2. 渲染 destructive confirmation 文本
# ════════════════════════════════════════════════════════════════


def _translate(i18n_manager: Any, key: str, locale: str, **kwargs: Any) -> str:
    """安全调用 i18n_manager.translate,缺失时回退到 key 本身。

    Args:
        i18n_manager: I18nManager 实例(或具备 translate 方法的 duck-typed 对象)
        key: 翻译 key
        locale: 目标 locale
        **kwargs: 插值参数

    Returns:
        翻译后的字符串;失败时返回 key(便于排查)
    """
    try:
        return i18n_manager.translate(key, locale=locale, **kwargs)
    except Exception:
        # 翻译缺失不应导致渲染崩溃,回退到 key(调用方应通过 i18n 门禁发现)
        return key


def render_destructive_confirmation(
    spec: ButtonUXSpec,
    i18n_manager: Any,
) -> str:
    """渲染 destructive action 确认面板为 Telegram 消息文本。

    审计 P1-08 要求渲染结果包含:
        1. 目标(target_display)
        2. 影响范围(impact_scope)
        3. 不可逆性提示(irreversibility_notice)
        4. 审批状态(approval_status_display)
        5. 取消按钮标签(cancel_button_label)

    Args:
        spec: ButtonUXSpec(由 ButtonUXPolicy.destructive_confirmation 产出)
        i18n_manager: I18nManager 实例

    Returns:
        Telegram 消息文本(纯文本,含上述 5 个必填字段)
    """
    locale = spec.locale or "zh-CN"
    # 解析 target_display — 格式 "{KEY_TARGET_DISPLAY}|{target_value}"
    if "|" in spec.target_display:
        target_key, target_value = spec.target_display.split("|", 1)
    else:
        target_key, target_value = spec.target_display, ""
    target_label = _translate(i18n_manager, target_key, locale)
    target_text = f"{target_label}: {target_value}" if target_value else target_label
    # 渲染影响范围 / 不可逆性 / 审批状态
    impact_text = _translate(i18n_manager, spec.impact_scope, locale)
    irreversibility_text = _translate(i18n_manager, spec.irreversibility_notice, locale)
    approval_text = _translate(i18n_manager, spec.approval_status_display, locale)
    # 渲染取消 / 确认按钮标签(显示在文本末尾,供用户预览)
    cancel_label = _translate(i18n_manager, spec.cancel_button_label, locale)
    confirm_label = _translate(i18n_manager, spec.confirm_button_label, locale)
    # 拼装消息文本(每段一行,便于阅读)
    lines: list[str] = [
        f"⚠️ {target_text}",
        f"  • {impact_text}",
        f"  • {irreversibility_text}",
        f"  • {approval_text}",
    ]
    if spec.requires_mfa_badge:
        mfa_badge_key = "button.ux.destructive.mfa_required_badge"
        mfa_badge = _translate(i18n_manager, mfa_badge_key, locale)
        lines.append(f"  • {mfa_badge}")
    # 取消 / 确认按钮标签(展示在文本末尾,实际按钮由 render_confirmation_buttons 产出)
    lines.append("")
    lines.append(f"[{confirm_label}] / [{cancel_label}]")
    return "\n".join(lines)


def render_confirmation_buttons(
    spec: ButtonUXSpec,
    i18n_manager: Any,
    *,
    confirm_callback: str = "",
    cancel_callback: str = "",
) -> list[InlineKeyboardButton]:
    """渲染 destructive action 确认面板的按钮列表(confirm + cancel)。

    Args:
        spec: ButtonUXSpec
        i18n_manager: I18nManager 实例
        confirm_callback: 确认按钮的 callback_data(由调用方提供,通常为签名 token)
        cancel_callback: 取消按钮的 callback_data(由调用方提供)

    Returns:
        [InlineKeyboardButton(confirm), InlineKeyboardButton(cancel)]
        按钮标签通过 i18n key 渲染,中英一致,无仅 emoji 标签。
    """
    locale = spec.locale or "zh-CN"
    confirm_label = _translate(i18n_manager, spec.confirm_button_label, locale)
    cancel_label = _translate(i18n_manager, spec.cancel_button_label, locale)
    return [
        InlineKeyboardButton(
            label=confirm_label,
            callback_data=confirm_callback or f"confirm|{spec.action}",
            category="confirm",
        ),
        InlineKeyboardButton(
            label=cancel_label,
            callback_data=cancel_callback or f"cancel|{spec.action}",
            category="cancel",
        ),
    ]


# ════════════════════════════════════════════════════════════════
# 3. 渲染错误恢复按钮
# ════════════════════════════════════════════════════════════════


def render_recovery_options(
    error_code: str,
    locale: str,
    i18n_manager: Any,
    *,
    policy: Optional[ButtonUXPolicy] = None,
    action: str = "",
    target: str = "",
) -> list[InlineKeyboardButton]:
    """根据错误码返回恢复按钮(重试/返回/重新发起等)。

    审计 P1-08 要求 4 种典型错误必须有可恢复按钮:
        - token 失效 → "重新发起"(resubmit)
        - 资源版本冲突 → "重新加载"(reload)
        - 审批过期 → "重新申请审批"(reapply_approval)
        - MFA 过期 → "重新 MFA"(replay_mfa)

    未知错误码默认返回 "重试"(retry) + "取消"(cancel) 两个按钮。

    Args:
        error_code: ErrorCodes 常量(如 "BUTTON_POLICY_NONCE_CONSUMED")
        locale: 目标 locale(zh-CN / en-US)
        i18n_manager: I18nManager 实例
        policy: 可选的 ButtonUXPolicy(默认使用模块单例)
        action: 关联的 action(用于 callback_data 绑定)
        target: 关联的 target(用于 callback_data 绑定)

    Returns:
        恢复按钮列表(至少 1 个,最多 2 个:主恢复按钮 + 取消按钮)
    """
    if policy is None:
        from services.button_ux_policy import get_button_ux_policy
        policy = get_button_ux_policy()
    recovery_key = policy.recovery_button_key(error_code)
    recovery_category = policy.recovery_button_category(error_code)
    recovery_label = _translate(i18n_manager, recovery_key, locale)
    cancel_label = _translate(i18n_manager, KEY_RECOVERY_CANCEL, locale)
    # callback_data 携带 action / target(由调用方进一步签名)
    callback_prefix = f"recover|{recovery_category}"
    if action:
        callback_prefix += f"|{action}"
    if target:
        callback_prefix += f"|{target}"
    return [
        InlineKeyboardButton(
            label=recovery_label,
            callback_data=callback_prefix,
            category=recovery_category,
        ),
        InlineKeyboardButton(
            label=cancel_label,
            callback_data=f"cancel|{action}" if action else "cancel",
            category="cancel",
        ),
    ]


# ════════════════════════════════════════════════════════════════
# 4. 验证辅助函数(供门禁脚本使用)
# ════════════════════════════════════════════════════════════════


def is_emoji_only_label(label: str) -> bool:
    """检查按钮标签是否仅由 emoji / 空白组成(无文字)。

    审计 P1-08 禁止仅 emoji 表意(emoji 必须配合文字标签)。

    简化判定:标签去除 emoji 后剩余字符均为空白 → 视为仅 emoji。
    判定 emoji 的范围:Unicode U+1F000-U+1FAFF + 部分符号区段。
    """
    if not label:
        return False
    # 去除 emoji + 修饰符 + 变体选择符
    cleaned: list[str] = []
    for ch in label:
        cp = ord(ch)
        # Emoji 主区段
        if 0x1F300 <= cp <= 0x1FAFF:
            continue
        # Emoji 修饰符 / ZWJ / 变体选择符
        if cp in (0x200D, 0xFE0F, 0xFE0E):
            continue
        # 区域符号(如 ⚠️ U+26A0 / ✅ U+2705 等符号字符也可作 emoji)
        if 0x2600 <= cp <= 0x27BF:
            continue
        # Keycap 组合(0-9 # *)
        if ch in "0123456789#*":
            continue
        cleaned.append(ch)
    # 去除空白后若为空,则标签仅由 emoji / 符号 / 空白组成
    return "".join(cleaned).strip() == ""


def validate_button_label(label: str, locale: str = "zh-CN") -> tuple[bool, str]:
    """验证按钮标签是否符合 P1-08 要求(非空 + 非仅 emoji)。

    Args:
        label: 按钮标签文本
        locale: 目标 locale(用于错误消息)

    Returns:
        (ok, reason) — ok=True 表示合规;ok=False 时 reason 为违规原因
    """
    if not label or not label.strip():
        return False, "button_label_empty"
    if is_emoji_only_label(label):
        return False, "button_label_emoji_only"
    return True, ""


# ════════════════════════════════════════════════════════════════
# 5. 模块导出
# ════════════════════════════════════════════════════════════════

__all__ = [
    # 数据类
    "InlineKeyboardButton",
    # 渲染函数
    "render_destructive_confirmation",
    "render_confirmation_buttons",
    "render_recovery_options",
    # 验证函数
    "is_emoji_only_label",
    "validate_button_label",
]
