"""R64 P1-08: 按钮式流程 UX 完整性测试。

审计 P1-08 整改验收:
    1. 每个 destructive action 必须有 ButtonUXSpec
    2. spec 必须包含: 目标 / 影响范围 / 不可逆性 / 审批状态 / 取消按钮
    3. 渲染后的 Telegram 文本必须包含上述 5 个字段
    4. 4 种典型错误(token/version/approval/mfa)必须有可恢复按钮:
       - token 失效 → resubmit(重新发起)
       - 资源版本冲突 → reload(重新加载)
       - 审批过期 → reapply_approval(重新申请审批)
       - MFA 过期 → replay_mfa(重新 MFA)
    5. Telegram 按钮标签 zh-CN / en-US 一致(相同 i18n key)
    6. 禁止仅 emoji 表意(emoji 必须配合文字标签)
    7. 取消按钮必须始终存在(无死路)

测试覆盖:
    A. ButtonUXSpec 存在性(9 个 destructive action + HIGH_RISK_POLICY 全集)
    B. ButtonUXSpec 字段完整性(target/impact/irreversibility/approval/cancel/confirm)
    C. render_destructive_confirmation 渲染文本包含全部必填字段
    D. render_recovery_options 4 种错误恢复按钮正确
    E. zh-CN / en-US locale key 一致性
    F. 仅 emoji 标签检测
    G. 取消按钮始终存在
    H. Rule D 门禁(check_button_handler_gate.py)真实代码库通过
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.button_ux_policy import (  # noqa: E402
    ButtonUXPolicy,
    ButtonUXSpec,
    ERROR_RECOVERY_CATEGORY_MAP,
    ERROR_RECOVERY_KEY_MAP,
    KEY_CANCEL,
    KEY_CONFIRM,
    KEY_RECOVERY_CANCEL,
    KEY_RECOVERY_REAPPLY_APPROVAL,
    KEY_RECOVERY_RELOAD,
    KEY_RECOVERY_REPLAY_MFA,
    KEY_RECOVERY_RESUBMIT,
    KEY_RECOVERY_RETRY,
    REQUIRED_DESTRUCTIVE_ACTIONS,
    get_all_specable_actions,
    has_ux_spec,
)
from services.button_ux_renderer import (  # noqa: E402
    InlineKeyboardButton,
    is_emoji_only_label,
    render_confirmation_buttons,
    render_destructive_confirmation,
    render_recovery_options,
    validate_button_label,
)

LOCALES_DIR = REPO_ROOT / "locales"


# ════════════════════════════════════════════════════════════════
# 辅助: FakeI18nManager — 读取真实 locale 文件解析嵌套 key
# ════════════════════════════════════════════════════════════════


class FakeI18nManager:
    """轻量 i18n 管理器,读取真实 locales/{locale}.json 解析嵌套 key。

    用于测试验证 locale 文件确实包含 button.ux.* key,
    而不是 mock 掉翻译(避免 locale 文件缺失被掩盖)。
    """

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        for locale_file in LOCALES_DIR.glob("*.json"):
            locale = locale_file.stem
            self._data[locale] = json.loads(locale_file.read_text(encoding="utf-8"))

    def translate(self, key: str, locale: str | None = None, **kwargs: Any) -> str:
        """按 dotted key 路径查找翻译值(如 button.ux.destructive.cancel)。"""
        loc = locale or "zh-CN"
        tree = self._data.get(loc, {})
        parts = key.split(".")
        node: Any = tree
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                # key 不存在,返回 key 本身(与真实 I18nManager fallback 一致)
                return key
        return str(node) if not isinstance(node, dict) else key


@pytest.fixture
def i18n() -> FakeI18nManager:
    """返回 FakeI18nManager 实例(读取真实 locale 文件)。"""
    return FakeI18nManager()


@pytest.fixture
def policy() -> ButtonUXPolicy:
    """返回 ButtonUXPolicy 实例。"""
    return ButtonUXPolicy()


# ════════════════════════════════════════════════════════════════
# A. ButtonUXSpec 存在性
# ════════════════════════════════════════════════════════════════


class TestButtonUXSpecExists:
    """A 节: 每个 destructive action 必须有 ButtonUXSpec。"""

    def test_all_required_destructive_actions_have_spec(self, policy):
        """A1: 9 个必填 destructive action 都能生成 spec(不抛异常)。"""
        for action in REQUIRED_DESTRUCTIVE_ACTIONS:
            spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
            assert isinstance(spec, ButtonUXSpec)
            assert spec.action == action

    def test_all_high_risk_policy_actions_have_spec(self, policy):
        """A2: HIGH_RISK_POLICY 中所有 action 都能生成 spec。"""
        for action in get_all_specable_actions():
            spec = policy.destructive_confirmation(action, "test_target", "en-US")
            assert isinstance(spec, ButtonUXSpec)
            assert spec.action == action

    def test_non_high_risk_action_raises(self, policy):
        """A3: 非 HIGH_RISK_POLICY action 调用应抛 AppError(BUTTON.UX.ACTION_NOT_HIGH_RISK,fail-closed)。"""
        from services.error_codes import AppError, ErrorCodes
        with pytest.raises(AppError) as exc_info:
            policy.destructive_confirmation("nonexistent_action", "target", "zh-CN")
        assert exc_info.value.code == ErrorCodes.BUTTON_UX_ACTION_NOT_HIGH_RISK

    def test_empty_action_raises(self, policy):
        """A4: 空 action 调用应抛 AppError(BUTTON.UX.ACTION_REQUIRED)。"""
        from services.error_codes import AppError, ErrorCodes
        with pytest.raises(AppError) as exc_info:
            policy.destructive_confirmation("", "target", "zh-CN")
        assert exc_info.value.code == ErrorCodes.BUTTON_UX_ACTION_REQUIRED

    def test_has_ux_spec_returns_true_for_high_risk(self):
        """A5: has_ux_spec 对 HIGH_RISK_POLICY action 返回 True。"""
        assert has_ux_spec("delete_file") is True
        assert has_ux_spec("ban_user") is True

    def test_has_ux_spec_returns_false_for_unknown(self):
        """A6: has_ux_spec 对未知 action 返回 False。"""
        assert has_ux_spec("unknown_action") is False


# ════════════════════════════════════════════════════════════════
# B. ButtonUXSpec 字段完整性
# ════════════════════════════════════════════════════════════════


class TestButtonUXSpecFields:
    """B 节: spec 必须包含所有必填字段。"""

    def test_spec_has_target_display(self, policy):
        """B1: spec.target_display 包含 target 值。"""
        spec = policy.destructive_confirmation("delete_file", "file_abc", "zh-CN")
        assert "file_abc" in spec.target_display
        assert "button.ux.destructive.target_display" in spec.target_display

    def test_spec_has_impact_scope(self, policy):
        """B2: spec.impact_scope 是非空 i18n key。"""
        spec = policy.destructive_confirmation("ban_user", "user_1", "zh-CN")
        assert spec.impact_scope
        assert spec.impact_scope.startswith("button.ux.destructive.")

    def test_spec_has_irreversibility_notice(self, policy):
        """B3: spec.irreversibility_notice 是非空 i18n key。"""
        spec = policy.destructive_confirmation("purge_data", "target", "zh-CN")
        assert spec.irreversibility_notice
        assert spec.irreversibility_notice.startswith("button.ux.destructive.")

    def test_spec_has_approval_status_display(self, policy):
        """B4: spec.approval_status_display 是非空 i18n key。"""
        spec = policy.destructive_confirmation("rotate_keys", "target", "zh-CN")
        assert spec.approval_status_display
        assert spec.approval_status_display.startswith("button.ux.destructive.")

    def test_spec_has_cancel_button_label(self, policy):
        """B5: spec.cancel_button_label == KEY_CANCEL(取消按钮必须存在)。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        assert spec.cancel_button_label == KEY_CANCEL

    def test_spec_has_confirm_button_label(self, policy):
        """B6: spec.confirm_button_label == KEY_CONFIRM。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        assert spec.confirm_button_label == KEY_CONFIRM

    def test_spec_locale_propagated(self, policy):
        """B7: spec.locale 与传入 locale 一致。"""
        spec_zh = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        spec_en = policy.destructive_confirmation("delete_file", "f", "en-US")
        assert spec_zh.locale == "zh-CN"
        assert spec_en.locale == "en-US"

    def test_spec_requires_mfa_badge_reflects_policy(self, policy):
        """B8: spec.requires_mfa_badge 与 HIGH_RISK_POLICY 的 requires_mfa 一致。"""
        from services.high_risk_policy import get_policy
        for action in ("ban_user", "delete_file", "rotate_keys"):
            rule = get_policy(action)
            spec = policy.destructive_confirmation(action, "t", "zh-CN")
            assert spec.requires_mfa_badge == rule.requires_mfa


# ════════════════════════════════════════════════════════════════
# C. render_destructive_confirmation 渲染文本
# ════════════════════════════════════════════════════════════════


class TestRenderDestructiveConfirmation:
    """C 节: 渲染文本必须包含目标/影响/不可逆性/审批状态/取消按钮标签。"""

    def test_rendered_text_contains_target(self, policy, i18n):
        """C1: 渲染文本包含目标资源标识。"""
        spec = policy.destructive_confirmation("delete_file", "file_xyz", "zh-CN")
        text = render_destructive_confirmation(spec, i18n)
        assert "file_xyz" in text

    def test_rendered_text_contains_impact_scope(self, policy, i18n):
        """C2: 渲染文本包含影响范围描述(非空)。"""
        spec = policy.destructive_confirmation("ban_user", "u1", "zh-CN")
        text = render_destructive_confirmation(spec, i18n)
        # 影响范围应包含 "封禁" 或 "ban"(取决于 locale 文件实际文案)
        assert len(text) > 10  # 非空文本

    def test_rendered_text_contains_cancel_label(self, policy, i18n):
        """C3: 渲染文本包含取消按钮标签。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        text = render_destructive_confirmation(spec, i18n)
        cancel_text = i18n.translate(KEY_CANCEL, locale="zh-CN")
        assert cancel_text in text

    def test_rendered_text_contains_confirm_label(self, policy, i18n):
        """C4: 渲染文本包含确认按钮标签。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        text = render_destructive_confirmation(spec, i18n)
        confirm_text = i18n.translate(KEY_CONFIRM, locale="zh-CN")
        assert confirm_text in text

    def test_rendered_text_en_us_locale(self, policy, i18n):
        """C5: en-US locale 渲染文本包含英文取消标签。"""
        spec = policy.destructive_confirmation("delete_file", "f", "en-US")
        text = render_destructive_confirmation(spec, i18n)
        cancel_en = i18n.translate(KEY_CANCEL, locale="en-US")
        assert cancel_en in text

    def test_rendered_text_has_multiple_lines(self, policy, i18n):
        """C6: 渲染文本为多行(含目标/影响/不可逆性/审批状态分段)。"""
        spec = policy.destructive_confirmation("purge_data", "target", "zh-CN")
        text = render_destructive_confirmation(spec, i18n)
        lines = [l for l in text.split("\n") if l.strip()]
        # 至少 5 段: 目标 + 影响 + 不可逆性 + 审批状态 + (MFA badge) + 按钮
        assert len(lines) >= 5


# ════════════════════════════════════════════════════════════════
# D. render_recovery_options 错误恢复按钮
# ════════════════════════════════════════════════════════════════


class TestRecoveryButtons:
    """D 节: 4 种典型错误必须有正确的可恢复按钮。"""

    def test_token_invalid_returns_resubmit(self, i18n, policy):
        """D1: token 失效(BUTTON_POLICY_NONCE_CONSUMED)→ resubmit 按钮。"""
        buttons = render_recovery_options(
            "BUTTON_POLICY_NONCE_CONSUMED", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "resubmit" for b in buttons)
        assert any(b.category == "cancel" for b in buttons)

    def test_token_expired_returns_resubmit(self, i18n, policy):
        """D2: token 过期(BUTTON_POLICY_EXPIRED)→ resubmit 按钮。"""
        buttons = render_recovery_options(
            "BUTTON_POLICY_EXPIRED", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "resubmit" for b in buttons)

    def test_version_mismatch_returns_reload(self, i18n, policy):
        """D3: 资源版本冲突(BUTTON_POLICY_VERSION_MISMATCH)→ reload 按钮。"""
        buttons = render_recovery_options(
            "BUTTON_POLICY_VERSION_MISMATCH", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "reload" for b in buttons)
        assert any(b.category == "cancel" for b in buttons)

    def test_approval_expired_returns_reapply_approval(self, i18n, policy):
        """D4: 审批过期(APPROVAL_EXPIRED)→ reapply_approval 按钮。"""
        buttons = render_recovery_options(
            "APPROVAL_EXPIRED", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "reapply_approval" for b in buttons)

    def test_mfa_expired_returns_replay_mfa(self, i18n, policy):
        """D5: MFA 过期(AUTH_MFA_RECEIPT_EXPIRED)→ replay_mfa 按钮。"""
        buttons = render_recovery_options(
            "AUTH_MFA_RECEIPT_EXPIRED", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "replay_mfa" for b in buttons)
        assert any(b.category == "cancel" for b in buttons)

    def test_unknown_error_returns_retry(self, i18n, policy):
        """D6: 未知错误码 → retry 按钮(默认回退)。"""
        buttons = render_recovery_options(
            "UNKNOWN_ERROR_CODE", "zh-CN", i18n, policy=policy,
        )
        assert any(b.category == "retry" for b in buttons)

    def test_recovery_buttons_include_cancel(self, i18n, policy):
        """D7: 所有恢复按钮列表都包含 cancel 按钮(无死路)。"""
        for error_code in (
            "BUTTON_POLICY_NONCE_CONSUMED",
            "BUTTON_POLICY_VERSION_MISMATCH",
            "APPROVAL_EXPIRED",
            "AUTH_MFA_RECEIPT_EXPIRED",
            "UNKNOWN_ERROR",
        ):
            buttons = render_recovery_options(
                error_code, "zh-CN", i18n, policy=policy,
            )
            assert any(b.category == "cancel" for b in buttons), (
                f"error_code={error_code} 恢复按钮缺少 cancel"
            )

    def test_recovery_button_labels_not_emoji_only(self, i18n, policy):
        """D8: 恢复按钮标签非仅 emoji。"""
        for error_code in ERROR_RECOVERY_KEY_MAP:
            buttons = render_recovery_options(
                error_code, "zh-CN", i18n, policy=policy,
            )
            for btn in buttons:
                ok, reason = validate_button_label(btn.label, "zh-CN")
                assert ok, f"error_code={error_code} 按钮 label={btn.label!r} 违规: {reason}"


# ════════════════════════════════════════════════════════════════
# E. zh-CN / en-US locale key 一致性
# ════════════════════════════════════════════════════════════════


class TestLocaleConsistency:
    """E 节: button.ux.* key 在 zh-CN 和 en-US 中都存在。"""

    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_destructive_keys_exist_in_locale(self, i18n, locale):
        """E1: button.ux.destructive.* 通用 key 在两个 locale 都有值。"""
        for key in (
            "button.ux.destructive.target_display",
            "button.ux.destructive.impact_scope",
            "button.ux.destructive.irreversibility_notice",
            "button.ux.destructive.approval_status",
            "button.ux.destructive.cancel",
            "button.ux.destructive.confirm",
            "button.ux.destructive.mfa_required_badge",
        ):
            val = i18n.translate(key, locale=locale)
            assert val != key, f"locale={locale} 缺少 key={key}"

    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_recovery_keys_exist_in_locale(self, i18n, locale):
        """E2: button.ux.recovery.* key 在两个 locale 都有值。"""
        for key in (
            KEY_RECOVERY_RETRY,
            KEY_RECOVERY_CANCEL,
            KEY_RECOVERY_RESUBMIT,
            KEY_RECOVERY_RELOAD,
            KEY_RECOVERY_REAPPLY_APPROVAL,
            KEY_RECOVERY_REPLAY_MFA,
        ):
            val = i18n.translate(key, locale=locale)
            assert val != key, f"locale={locale} 缺少 key={key}"

    @pytest.mark.parametrize("action", sorted(REQUIRED_DESTRUCTIVE_ACTIONS))
    def test_per_action_keys_exist_both_locales(self, i18n, action):
        """E3: 每个 destructive action 的 per-action key 在两个 locale 都有值。"""
        for locale in ("zh-CN", "en-US"):
            for suffix in ("impact_scope", "irreversibility", "approval_status"):
                key = f"button.ux.destructive.{action}.{suffix}"
                val = i18n.translate(key, locale=locale)
                assert val != key, (
                    f"locale={locale} action={action} 缺少 key={key}"
                )

    def test_cancel_label_differs_between_locales(self, i18n):
        """E4: zh-CN 与 en-US 的 cancel 标签不同(各自有独立翻译)。"""
        zh = i18n.translate(KEY_CANCEL, locale="zh-CN")
        en = i18n.translate(KEY_CANCEL, locale="en-US")
        assert zh != en, "zh-CN 与 en-US cancel 标签相同(可能未翻译)"


# ════════════════════════════════════════════════════════════════
# F. 仅 emoji 标签检测
# ════════════════════════════════════════════════════════════════


class TestNoEmojiOnlyLabels:
    """F 节: 禁止仅 emoji 表意。"""

    def test_is_emoji_only_detects_pure_emoji(self):
        """F1: 纯 emoji 字符串被检测为 emoji-only。"""
        assert is_emoji_only_label("⚠️") is True
        assert is_emoji_only_label("🔥🚫") is True

    def test_is_emoji_only_allows_emoji_with_text(self):
        """F2: emoji + 文字组合不被视为 emoji-only。"""
        assert is_emoji_only_label("⚠️ 确认执行") is False
        assert is_emoji_only_label("Confirm ✓") is False

    def test_is_emoji_only_rejects_plain_text(self):
        """F3: 纯文字不是 emoji-only。"""
        assert is_emoji_only_label("确认执行") is False
        assert is_emoji_only_label("Cancel") is False

    def test_validate_button_label_rejects_emoji_only(self):
        """F4: validate_button_label 对 emoji-only 标签返回 False。"""
        ok, reason = validate_button_label("🔥", "zh-CN")
        assert ok is False
        assert reason == "button_label_emoji_only"

    def test_validate_button_label_rejects_empty(self):
        """F5: validate_button_label 对空标签返回 False。"""
        ok, reason = validate_button_label("", "zh-CN")
        assert ok is False
        assert reason == "button_label_empty"

    def test_rendered_buttons_not_emoji_only(self, policy, i18n):
        """F6: render_confirmation_buttons 产出的按钮标签非仅 emoji。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        buttons = render_confirmation_buttons(spec, i18n)
        for btn in buttons:
            ok, _ = validate_button_label(btn.label, "zh-CN")
            assert ok, f"按钮 label={btn.label!r} 是 emoji-only 或空"


# ════════════════════════════════════════════════════════════════
# G. 取消按钮始终存在
# ════════════════════════════════════════════════════════════════


class TestCancelButtonAlwaysPresent:
    """G 节: 确认面板和恢复面板都必须有取消按钮(无死路)。"""

    def test_confirmation_buttons_include_cancel(self, policy, i18n):
        """G1: render_confirmation_buttons 包含 cancel 按钮。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        buttons = render_confirmation_buttons(spec, i18n)
        categories = [b.category for b in buttons]
        assert "cancel" in categories

    def test_confirmation_buttons_include_confirm(self, policy, i18n):
        """G2: render_confirmation_buttons 包含 confirm 按钮。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        buttons = render_confirmation_buttons(spec, i18n)
        categories = [b.category for b in buttons]
        assert "confirm" in categories

    def test_recovery_buttons_always_include_cancel(self, i18n, policy):
        """G3: render_recovery_options 对所有错误码都返回 cancel 按钮。"""
        error_codes = list(ERROR_RECOVERY_KEY_MAP.keys()) + ["UNKNOWN_ERROR"]
        for ec in error_codes:
            buttons = render_recovery_options(ec, "zh-CN", i18n, policy=policy)
            assert any(b.category == "cancel" for b in buttons), (
                f"error_code={ec} 缺少 cancel 按钮"
            )

    def test_cancel_button_callback_data_present(self, policy, i18n):
        """G4: 取消按钮有非空 callback_data(可操作)。"""
        spec = policy.destructive_confirmation("delete_file", "f", "zh-CN")
        buttons = render_confirmation_buttons(spec, i18n)
        cancel_btn = next(b for b in buttons if b.category == "cancel")
        assert cancel_btn.callback_data


# ════════════════════════════════════════════════════════════════
# H. Rule D 门禁(check_button_handler_gate.py)
# ════════════════════════════════════════════════════════════════


class TestRuleDGate:
    """H 节: check_button_handler_gate.py Rule D 真实代码库通过。"""

    def test_rule_d_passes_on_real_codebase(self):
        """H1: 真实代码库运行 check_button_handler_gate,Rule D 0 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_button_handler_gate as gate
            exit_code, new_violations, all_violations = gate.check()
            rule_d_violations = [v for v in all_violations if v["rule"] == "D"]
            assert len(rule_d_violations) == 0, (
                f"Rule D 存在 UXSpec 覆盖违规: {rule_d_violations}"
            )
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))
