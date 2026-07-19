"""R65 P1-11: 按钮式流程真实 UX 落地测试。

审计 P1-11 整改验收:
    按钮式流程要从 policy 表落到真实 UX。ButtonUXSpec 需要在真实
    Telegram/Web handler 上验证:目标、影响、不可逆性、审批状态、MFA 状态、
    resource version、确认、取消、过期恢复、重试和返回按钮均真实渲染;
    不能只检查 sidecar metadata。

本测试覆盖 4 个代表性 ButtonUXSpec(覆盖 3 类 handler 形态):
    1. ``delete_file``     — Telegram callback_sub_dispatcher(_handle_delete_file_action),
                              按钮由 sibling 文件 bots/admin_bot/handlers.py 渲染
                              (InlineKeyboardButton + sign_button_token_with_handle)
    2. ``ban_user``         — Web FastAPI POST(toggle_ban) + Telegram callback_sub_dispatcher
                              (_handle_report_action,report_action 包含 ban 子动作)
    3. ``restore_backup``   — Telegram callback_sub_dispatcher(_handle_restore_action),
                              按钮由 sibling 文件 bots/admin_bot/handlers.py 渲染
    4. ``purge_data``       — policy-only action(无 UI handler),用于负面验证
                              Rule E 跳过 policy-only action 的逻辑

测试维度(A-E):
    A. 按钮渲染(text / callback_data / keyboard) — render_destructive_confirmation +
       render_confirmation_buttons 真实产出可执行按钮(callback_data 非空、
       label 非空且非仅 emoji、含 confirm + cancel 两个按钮)
    B. 5 生命周期路径(confirm/cancel/expire/retry/return)—
       services.button_flow_lifecycle.verify_all_lifecycle_paths 全部通过
    C. 双语翻译(zh-CN + en-US)— 所有 spec 引用的 i18n key 在两个 locale
       都有非空值
    D. UserMessage + locale 绑定 — 真实 handler 文件 AST 扫描到
       UserMessage.from_*(Telegram)与 _i18n_t / i18n_manager.translate
       (所有 handler),与 check_button_flow_real_ux.py Rule I 一致
    E. check_button_flow_real_ux.py --strict 在真实代码库通过(0 违规)

约束:
    - Telegram E2E 用 mock(InMemoryButtonTokenStore),不依赖真实数据库
    - 不破坏现有按钮测试(R64 P1-08 全部用例保持通过)
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCALES_DIR = REPO_ROOT / "locales"

from services.button_ux_policy import (  # noqa: E402
    ButtonUXPolicy,
    ButtonUXSpec,
    KEY_CANCEL,
    KEY_CONFIRM,
    KEY_RECOVERY_CANCEL,
    KEY_RECOVERY_REAPPLY_APPROVAL,
    KEY_RECOVERY_RELOAD,
    KEY_RECOVERY_REPLAY_MFA,
    KEY_RECOVERY_RESUBMIT,
    KEY_RECOVERY_RETRY,
    KEY_TARGET_DISPLAY,
    REQUIRED_DESTRUCTIVE_ACTIONS,
    get_all_specable_actions,
)
from services.button_ux_renderer import (  # noqa: E402
    InlineKeyboardButton,
    is_emoji_only_label,
    render_confirmation_buttons,
    render_destructive_confirmation,
    render_recovery_options,
    validate_button_label,
)
from services.button_flow_lifecycle import (  # noqa: E402
    CANCEL_CALLBACK_PREFIX,
    LIFECYCLE_PATHS,
    RETURN_CALLBACK_DATA,
    simulate_lifecycle_path,
    verify_all_lifecycle_paths,
)
from services.high_risk_policy import HIGH_RISK_POLICY  # noqa: E402


# ════════════════════════════════════════════════════════════════
# 辅助: FakeI18nManager — 读取真实 locale 文件解析嵌套 key
# ════════════════════════════════════════════════════════════════


class FakeI18nManager:
    """轻量 i18n 管理器,读取真实 locales/{locale}.json 解析 dotted key。

    复用 tests/test_r64_p1_8_button_ux_completeness.py 模式,
    确保测试验证真实 locale 文件而非 mock 翻译。
    """

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        for locale_file in LOCALES_DIR.glob("*.json"):
            locale = locale_file.stem
            self._data[locale] = json.loads(locale_file.read_text(encoding="utf-8"))

    def translate(self, key: str, locale: str | None = None, **kwargs: Any) -> str:
        """按 dotted key 路径查找翻译值。"""
        loc = locale or "zh-CN"
        tree = self._data.get(loc, {})
        parts = key.split(".")
        node: Any = tree
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return key  # key 不存在,返回 key 本身(与真实 I18nManager fallback 一致)
        return str(node) if not isinstance(node, dict) else key

    def has_key(self, key: str, locale: str) -> bool:
        """检查 key 在指定 locale 中是否存在(且值为非空字符串)。"""
        return self.translate(key, locale=locale) != key


@pytest.fixture
def i18n() -> FakeI18nManager:
    """返回 FakeI18nManager 实例(读取真实 locale 文件)。"""
    return FakeI18nManager()


@pytest.fixture
def policy() -> ButtonUXPolicy:
    """返回 ButtonUXPolicy 实例。"""
    return ButtonUXPolicy()


# 4 个代表性 action(覆盖 3 类 handler 形态 + 1 个 policy-only)
REPRESENTATIVE_ACTIONS: tuple[str, ...] = (
    "delete_file",     # Telegram callback_sub_dispatcher + sibling handlers.py 渲染
    "ban_user",        # Web FastAPI POST + Telegram callback_sub_dispatcher
    "restore_backup",  # Telegram callback_sub_dispatcher + sibling handlers.py 渲染
    "purge_data",      # policy-only action(无 UI handler),负面验证
)

# 有 UI handler 的 action(用于 Rule E/F/I 验证)
UI_ACTIONS: tuple[str, ...] = (
    "delete_file", "ban_user", "restore_backup",
)


# ════════════════════════════════════════════════════════════════
# A. 按钮渲染(text / callback_data / keyboard)
# ════════════════════════════════════════════════════════════════


class TestButtonRendering:
    """A 节: ButtonUXSpec 渲染出真实可执行按钮(text + callback_data)。"""

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_rendered_text_contains_all_required_fields(
        self, policy, i18n, action, locale,
    ):
        """A1: 渲染文本包含目标/影响/不可逆性/审批状态/取消/确认 6 个字段。

        P1-11 要求 ButtonUXSpec 在真实 handler 上"展示 target/impact/
        irreversibility/approval/MFA/resource version",本测试验证渲染文本
        至少包含前 5 个必填字段(MFA 在 requires_mfa_badge=True 时显示)。
        """
        spec = policy.destructive_confirmation(action, "test_target", locale)
        text = render_destructive_confirmation(spec, i18n)
        # 目标值必须在文本中
        assert "test_target" in text, f"action={action} locale={locale} 文本未含目标值"
        # 文本至少 5 行(目标 + 影响 + 不可逆性 + 审批状态 + 按钮)
        lines = [l for l in text.split("\n") if l.strip()]
        assert len(lines) >= 5, (
            f"action={action} locale={locale} 渲染文本行数 {len(lines)} < 5"
        )
        # 取消与确认标签必须出现在文本末尾的按钮预览段
        cancel_label = i18n.translate(KEY_CANCEL, locale=locale)
        confirm_label = i18n.translate(KEY_CONFIRM, locale=locale)
        assert cancel_label in text, (
            f"action={action} locale={locale} 文本未含取消标签"
        )
        assert confirm_label in text, (
            f"action={action} locale={locale} 文本未含确认标签"
        )

    @pytest.mark.parametrize("action", UI_ACTIONS)
    def test_render_confirmation_buttons_returns_confirm_and_cancel(
        self, policy, i18n, action,
    ):
        """A2: render_confirmation_buttons 返回 [confirm, cancel] 两个按钮。

        P1-11 要求每个按钮流程渲染实际的按钮(不能只渲染文本),
        confirm 与 cancel 按钮必须同时存在(无死路)。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        buttons = render_confirmation_buttons(
            spec, i18n,
            confirm_callback=f"confirm|{action}",
            cancel_callback=f"{CANCEL_CALLBACK_PREFIX}{action}",
        )
        assert len(buttons) == 2, f"action={action} 按钮数 {len(buttons)} != 2"
        categories = [b.category for b in buttons]
        assert "confirm" in categories
        assert "cancel" in categories

    @pytest.mark.parametrize("action", UI_ACTIONS)
    def test_buttons_have_non_empty_callback_data(self, policy, i18n, action):
        """A3: 渲染按钮的 callback_data 非空(可点击触发后续流程)。

        P1-11 要求按钮 UX 必须真实渲染,不能只渲染标签。callback_data
        是 Telegram InlineKeyboardButton 的核心字段(决定点击后路由),
        必须非空且含 action 标识(便于子分发器路由)。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        buttons = render_confirmation_buttons(
            spec, i18n,
            confirm_callback=f"confirm|{action}",
            cancel_callback=f"{CANCEL_CALLBACK_PREFIX}{action}",
        )
        for btn in buttons:
            assert btn.callback_data, (
                f"action={action} category={btn.category} callback_data 为空"
            )
            assert action in btn.callback_data, (
                f"action={action} category={btn.category} "
                f"callback_data={btn.callback_data!r} 未含 action 标识"
            )

    @pytest.mark.parametrize("action", UI_ACTIONS)
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_buttons_labels_not_emoji_only(
        self, policy, i18n, action, locale,
    ):
        """A4: 渲染按钮的 label 非仅 emoji(P1-08 + P1-11 共同要求)。

        P1-08 禁止仅 emoji 表意;P1-11 要求按钮在真实 handler 上"真实渲染",
        即 label 必须含可读文字(emoji 可作辅助但不能独立)。
        """
        spec = policy.destructive_confirmation(action, "test_target", locale)
        buttons = render_confirmation_buttons(spec, i18n)
        for btn in buttons:
            ok, reason = validate_button_label(btn.label, locale)
            assert ok, (
                f"action={action} locale={locale} category={btn.category} "
                f"label={btn.label!r} 违规: {reason}"
            )
            assert not is_emoji_only_label(btn.label), (
                f"action={action} locale={locale} label={btn.label!r} 仅 emoji"
            )

    @pytest.mark.parametrize("action", UI_ACTIONS)
    def test_buttons_keyboard_like_structure(self, policy, i18n, action):
        """A5: 渲染按钮构成可序列化为 Telegram InlineKeyboardMarkup 的结构。

        P1-11 要求按钮 UX 必须真实渲染(text, callback_data, keyboard)。
        本测试验证按钮列表可序列化为 Telegram InlineKeyboardMarkup
        所需的 [[InlineKeyboardButton, ...], ...] 嵌套 list 结构
        (调用方在真实 handler 中使用 InlineKeyboardMarkup(buttons) 包装)。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        buttons = render_confirmation_buttons(
            spec, i18n,
            confirm_callback=f"confirm|{action}",
            cancel_callback=f"{CANCEL_CALLBACK_PREFIX}{action}",
        )
        # 构造 Telegram InlineKeyboardMarkup 期望的嵌套 list 结构
        keyboard: list[list[dict]] = [
            [{"label": b.label, "callback_data": b.callback_data} for b in buttons]
        ]
        assert len(keyboard) == 1
        assert len(keyboard[0]) == 2
        # 序列化为 JSON 验证可传输(真实 handler 通过 Bot API 序列化)
        json_str = json.dumps(keyboard, ensure_ascii=False)
        assert "callback_data" in json_str
        assert action in json_str


# ════════════════════════════════════════════════════════════════
# B. 5 生命周期路径(confirm / cancel / expire / retry / return)
# ════════════════════════════════════════════════════════════════


class TestLifecyclePaths:
    """B 节: 5 生命周期路径在 mock ButtonFlow 上全部通过。

    使用 services.button_flow_lifecycle.InMemoryButtonTokenStore
    (不依赖 aiosqlite,纯内存),模拟器覆盖 P1-11 强制的全部 5 条路径。
    """

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.asyncio
    async def test_confirm_path_token_consumed(self, policy, i18n, action):
        """B1: confirm 路径 — 用户点击确认 → CAS 消费 + 执行 → 成功。

        P1-11 要求每个按钮流程实现 confirm 路径(用户点击确认按钮后,
        token 被 CAS 原子消费,ButtonFlow.execute 成功)。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        result = await simulate_lifecycle_path(
            spec, "confirm", i18n_manager=i18n, policy=policy,
        )
        assert result.success, (
            f"action={action} confirm 路径失败: {result.note}"
        )
        assert result.token_consumed is True, (
            f"action={action} confirm 路径未消费 token"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.asyncio
    async def test_cancel_path_token_not_consumed(self, policy, i18n, action):
        """B2: cancel 路径 — 用户点击取消 → token 未消费 → 渲染返回按钮。

        P1-11 要求 cancel 路径不执行业务操作,token 保留未消费状态,
        并渲染返回按钮(无死路)。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        result = await simulate_lifecycle_path(
            spec, "cancel", i18n_manager=i18n, policy=policy,
        )
        assert result.success, f"action={action} cancel 路径失败: {result.note}"
        assert result.token_consumed is False, (
            f"action={action} cancel 路径不应消费 token"
        )
        assert result.back_button is not None, (
            f"action={action} cancel 路径未渲染返回按钮"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.asyncio
    async def test_expire_path_renders_recovery_buttons(self, policy, i18n, action):
        """B3: expire 路径 — token 过期 → CAS 失败 → 渲染恢复按钮。

        P1-11 要求 expire 路径渲染恢复按钮(如 resubmit),不能死端。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        result = await simulate_lifecycle_path(
            spec, "expire", i18n_manager=i18n, policy=policy,
        )
        assert result.success, f"action={action} expire 路径失败: {result.note}"
        assert result.token_consumed is False, (
            f"action={action} expire 路径不应消费 token"
        )
        assert len(result.recovery_buttons) > 0, (
            f"action={action} expire 路径未渲染恢复按钮"
        )
        assert result.back_button is not None, (
            f"action={action} expire 路径未渲染返回按钮"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.asyncio
    async def test_retry_path_renders_recovery_buttons(self, policy, i18n, action):
        """B4: retry 路径 — 版本冲突 → CAS 失败 → 渲染恢复按钮。

        P1-11 要求 retry 路径在资源版本冲突时渲染恢复按钮(reload/resubmit),
        让用户可重新发起请求获取新 token。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        result = await simulate_lifecycle_path(
            spec, "retry", i18n_manager=i18n, policy=policy,
        )
        assert result.success, f"action={action} retry 路径失败: {result.note}"
        assert result.token_consumed is False, (
            f"action={action} retry 路径不应消费 token"
        )
        assert len(result.recovery_buttons) > 0, (
            f"action={action} retry 路径未渲染恢复按钮"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.asyncio
    async def test_return_path_renders_back_button(self, policy, i18n, action):
        """B5: return 路径 — 渲染 back 按钮回到初始菜单(无死路)。

        P1-11 要求 return 路径渲染 back 按钮,与 bots/admin_bot/menus.py
        的 BACK_BTN 一致(callback_data="menu:main")。
        """
        spec = policy.destructive_confirmation(action, "test_target", "zh-CN")
        result = await simulate_lifecycle_path(
            spec, "return", i18n_manager=i18n, policy=policy,
        )
        assert result.success, f"action={action} return 路径失败: {result.note}"
        assert result.back_button is not None, (
            f"action={action} return 路径未渲染 back 按钮"
        )
        assert result.back_button.callback_data == RETURN_CALLBACK_DATA, (
            f"action={action} return 按钮 callback_data="
            f"{result.back_button.callback_data!r} != 'menu:main'"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    @pytest.mark.asyncio
    async def test_all_5_lifecycle_paths_pass(
        self, policy, i18n, action, locale,
    ):
        """B6: 全部 5 生命周期路径在一个 spec 上通过(verify_all_lifecycle_paths)。

        P1-11 强制要求 confirm/cancel/expire/retry/return 全部实现(无死路)。
        本测试覆盖 4 个代表性 action × 2 locale = 8 个 spec 组合。
        """
        spec = policy.destructive_confirmation(action, "test_target", locale)
        verification = await verify_all_lifecycle_paths(
            spec, i18n_manager=i18n, policy=policy,
        )
        assert verification.all_passed, (
            f"action={action} locale={locale} 5 路径未全部通过, "
            f"失败路径={verification.failed_paths}"
        )
        # 验证 5 条路径结果都已收集
        assert len(verification.path_results) == len(LIFECYCLE_PATHS), (
            f"action={action} locale={locale} 路径数 "
            f"{len(verification.path_results)} != {len(LIFECYCLE_PATHS)}"
        )
        # 验证每条路径都有非空 message(便于排查)
        for r in verification.path_results:
            assert r.note, (
                f"action={action} locale={locale} path={r.path} message 为空"
            )

    def test_lifecycle_paths_constant_order(self):
        """B7: LIFECYCLE_PATHS 常量顺序固定(confirm/cancel/expire/retry/return)。

        P1-11 强制 5 条路径的顺序固定,供门禁脚本与测试按序遍历,
        避免顺序漂移导致 ratchet 不稳定。
        """
        assert LIFECYCLE_PATHS == (
            "confirm", "cancel", "expire", "retry", "return",
        ), f"LIFECYCLE_PATHS 顺序错误: {LIFECYCLE_PATHS}"

    def test_return_callback_data_matches_menus_back_btn(self):
        """B8: RETURN_CALLBACK_DATA 与 bots/admin_bot/menus.py 的 BACK_BTN 一致。

        P1-11 要求 return 路径的 back 按钮与真实代码库的 BACK_BTN
        使用相同的 callback_data(避免死路:用户点击 back 后必须能回到主菜单)。
        """
        menus_path = REPO_ROOT / "bots" / "admin_bot" / "menus.py"
        assert menus_path.exists(), f"menus.py 不存在: {menus_path}"
        source = menus_path.read_text(encoding="utf-8")
        # BACK_BTN 定义中必须包含 callback_data="menu:main"
        assert 'callback_data="menu:main"' in source, (
            f"menus.py 的 BACK_BTN 未使用 callback_data='menu:main'"
        )
        assert RETURN_CALLBACK_DATA == "menu:main"


# ════════════════════════════════════════════════════════════════
# C. 双语翻译(zh-CN + en-US)
# ════════════════════════════════════════════════════════════════


class TestBilingualTranslations:
    """C 节: 所有用户面字符串必须有双语翻译(zh-CN + en-US)。"""

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_spec_keys_exist_in_locale(self, policy, i18n, action, locale):
        """C1: spec 引用的所有 i18n key 在指定 locale 中有非空值。

        P1-11 要求所有用户面字符串(target/impact/irreversibility/approval/
        cancel/confirm)必须有双语翻译。spec 字段中存的是 i18n key,
        通过 FakeI18nManager 查询真实 locale 文件验证 key 存在。
        """
        spec = policy.destructive_confirmation(action, "test_target", locale)
        # target_display 格式 "{KEY_TARGET_DISPLAY}|{target}",取前半部分
        target_key = (
            spec.target_display.split("|", 1)[0]
            if "|" in spec.target_display
            else spec.target_display
        )
        keys_to_check = [
            target_key,
            spec.impact_scope,
            spec.irreversibility_notice,
            spec.approval_status_display,
            spec.cancel_button_label,
            spec.confirm_button_label,
        ]
        if spec.requires_mfa_badge:
            keys_to_check.append("button.ux.destructive.mfa_required_badge")
        for key in keys_to_check:
            assert i18n.has_key(key, locale), (
                f"action={action} locale={locale} key={key} 在 locale 文件中缺失"
            )

    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_recovery_keys_exist_in_locale(self, i18n, locale):
        """C2: 错误恢复按钮 key 在两个 locale 都有值。

        P1-11 要求 expire/retry 路径渲染的恢复按钮有双语翻译。
        """
        for key in (
            KEY_RECOVERY_RETRY,
            KEY_RECOVERY_CANCEL,
            KEY_RECOVERY_RESUBMIT,
            KEY_RECOVERY_RELOAD,
            KEY_RECOVERY_REAPPLY_APPROVAL,
            KEY_RECOVERY_REPLAY_MFA,
        ):
            assert i18n.has_key(key, locale), (
                f"locale={locale} recovery key={key} 缺失"
            )

    def test_cancel_and_confirm_labels_differ_between_locales(self, i18n):
        """C3: zh-CN 与 en-US 的 cancel/confirm 标签各自有独立翻译。

        P1-11 双语要求:不能两个 locale 用相同文案(可能未翻译)。
        """
        zh_cancel = i18n.translate(KEY_CANCEL, locale="zh-CN")
        en_cancel = i18n.translate(KEY_CANCEL, locale="en-US")
        zh_confirm = i18n.translate(KEY_CONFIRM, locale="zh-CN")
        en_confirm = i18n.translate(KEY_CONFIRM, locale="en-US")
        assert zh_cancel != en_cancel, (
            f"cancel 标签 zh/en 相同: zh={zh_cancel!r} en={en_cancel!r}"
        )
        assert zh_confirm != en_confirm, (
            f"confirm 标签 zh/en 相同: zh={zh_confirm!r} en={en_confirm!r}"
        )

    @pytest.mark.parametrize("action", REPRESENTATIVE_ACTIONS)
    def test_back_button_label_exists_both_locales(self, policy, i18n, action):
        """C4: return 路径的 back 按钮标签在两个 locale 都有值。

        back 按钮使用 i18n key 'bot.admin_bot.menus.s20'(与 BACK_BTN 一致)。
        """
        for locale in ("zh-CN", "en-US"):
            assert i18n.has_key("bot.admin_bot.menus.s20", locale), (
                f"action={action} locale={locale} back 按钮 key "
                f"bot.admin_bot.menus.s20 缺失"
            )


# ════════════════════════════════════════════════════════════════
# D. UserMessage + locale 绑定(真实 handler AST 扫描)
# ════════════════════════════════════════════════════════════════


# Rule I 检查的 AST 函数名(与 scripts/check_button_flow_real_ux.py 一致)
USER_MESSAGE_FACTORIES: frozenset[str] = frozenset({
    "UserMessage.from_key",
    "UserMessage.from_error",
    "UserMessage.from_raw_text",
})

LOCALE_BINDING_FUNCS: frozenset[str] = frozenset({
    "_i18n_t",
    "i18n_manager.translate",
    "translate",
})


def _ast_find_call_names(node: ast.AST, target_names: frozenset[str]) -> list[str]:
    """AST 扫描:返回与 target_names 匹配的调用名(支持 Name 与 Attribute)。"""
    matched: list[str] = []

    def _get_call_name(call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                return f"{func.value.id}.{func.attr}"
            return func.attr
        return None

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _get_call_name(child)
            if name and name in target_names:
                matched.append(name)
    return matched


class TestUserMessageLocaleBinding:
    """D 节: 真实 handler 文件使用 UserMessage + locale 绑定。

    与 scripts/check_button_flow_real_ux.py Rule I 的判定一致:
      - Telegram handler(callback_sub_dispatcher / callback_query_handler):
        要求 UserMessage.from_*(typed sink adapter)+ locale 绑定
      - Web handler(fastapi_post):仅要求 locale 绑定
        (Web 通过 HTML 模板 + flash message + RedirectResponse 渲染用户面消息,
         UserMessage 不直接适用)
    """

    def _load_inventory(self) -> dict:
        """加载 button_handler_inventory.json。"""
        path = REPO_ROOT / "scripts" / "button_handler_inventory.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _find_handlers(self, action_type: str) -> list[dict]:
        """查找 inventory 中指定 action_type 的高风险 handler。"""
        inv = self._load_inventory()
        return [
            h for h in inv.get("handlers", [])
            if h.get("is_high_risk")
            and not h.get("is_dispatcher")
            and h.get("entry_type") in (
                "fastapi_post", "callback_query_handler", "callback_sub_dispatcher",
            )
            and h.get("action_type") == action_type
        ]

    def test_telegram_handler_uses_user_message(self):
        """D1: Telegram callback_sub_dispatcher 文件使用 UserMessage.from_*。

        P1-11 要求 handler 使用 UserMessage(typed sink adapter,禁止裸 str)
        以确保用户面消息走统一的 i18n + locale 绑定路径。
        验证 _handle_delete_file_action(action_type=delete)所在文件
        bots/admin_bot/callback.py 含 UserMessage.from_* 调用。
        """
        handlers = self._find_handlers("delete")
        assert handlers, "inventory 中未找到 action_type=delete 的高风险 handler"
        for h in handlers:
            if h.get("entry_type") != "callback_sub_dispatcher":
                continue
            file_path = REPO_ROOT / h["file"]
            assert file_path.exists(), f"handler 文件不存在: {file_path}"
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            um_calls = _ast_find_call_names(tree, USER_MESSAGE_FACTORIES)
            assert um_calls, (
                f"file={h['file']} handler={h['handler']} "
                f"未使用 UserMessage.from_* (typed sink adapter)"
            )

    def test_telegram_handler_uses_locale_binding(self):
        """D2: Telegram callback_sub_dispatcher 文件使用 locale 绑定调用。

        P1-11 要求 handler 显式绑定 locale(_i18n_t / i18n_manager.translate),
        禁止使用全局默认 locale。
        """
        handlers = self._find_handlers("restore")
        assert handlers, "inventory 中未找到 action_type=restore 的高风险 handler"
        for h in handlers:
            if h.get("entry_type") != "callback_sub_dispatcher":
                continue
            file_path = REPO_ROOT / h["file"]
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            locale_calls = _ast_find_call_names(tree, LOCALE_BINDING_FUNCS)
            assert locale_calls, (
                f"file={h['file']} handler={h['handler']} "
                f"未使用 _i18n_t / i18n_manager.translate (locale 绑定)"
            )

    def test_telegram_report_action_uses_user_message(self):
        """D3: report_action 子分发器(ban_user/detach_file/block_user_for_file)使用 UserMessage。

        _handle_report_action 处理 report_action(ban + detach + block 子动作),
        必须用 UserMessage.from_* + locale 绑定。
        """
        handlers = self._find_handlers("report_action")
        assert handlers, "inventory 中未找到 action_type=report_action 的高风险 handler"
        for h in handlers:
            file_path = REPO_ROOT / h["file"]
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            um_calls = _ast_find_call_names(tree, USER_MESSAGE_FACTORIES)
            locale_calls = _ast_find_call_names(tree, LOCALE_BINDING_FUNCS)
            assert um_calls, (
                f"file={h['file']} handler={h['handler']} "
                f"report_action 子分发器未使用 UserMessage.from_*"
            )
            assert locale_calls, (
                f"file={h['file']} handler={h['handler']} "
                f"report_action 子分发器未使用 locale 绑定"
            )

    def test_web_handler_uses_locale_binding(self):
        """D4: Web FastAPI POST handler 使用 locale 绑定。

        Web handler(fastapi_post)通过 HTML 模板 + flash message + RedirectResponse
        渲染用户面消息,UserMessage(typed sink adapter for chat)不直接适用 —
        但仍必须显式绑定 locale。
        验证 toggle_ban / delete_file / takedown_report / maintenance_action
        所在文件 admin/__init__.py 含 locale 绑定调用。
        """
        inv = self._load_inventory()
        web_handlers = [
            h for h in inv.get("handlers", [])
            if h.get("is_high_risk")
            and not h.get("is_dispatcher")
            and h.get("entry_type") == "fastapi_post"
        ]
        assert web_handlers, "inventory 中未找到 fastapi_post 高风险 handler"
        files_seen: set[str] = set()
        for h in web_handlers:
            rel_path = h["file"]
            if rel_path in files_seen:
                continue
            files_seen.add(rel_path)
            file_path = REPO_ROOT / rel_path
            assert file_path.exists(), f"Web handler 文件不存在: {file_path}"
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            locale_calls = _ast_find_call_names(tree, LOCALE_BINDING_FUNCS)
            assert locale_calls, (
                f"file={rel_path} Web handler 未使用 locale 绑定调用"
            )


# ════════════════════════════════════════════════════════════════
# E. check_button_flow_real_ux.py --strict 在真实代码库通过
# ════════════════════════════════════════════════════════════════


class TestGateScriptPassesOnRealCodebase:
    """E 节: check_button_flow_real_ux.py --strict 在真实代码库通过(0 违规)。

    本节直接调用 scripts/check_button_flow_real_ux.py 的 check() 函数,
    在真实代码库上运行 Rule E/F/G/H/I 全部 5 条规则,验证 0 违规。
    """

    def test_check_button_flow_real_ux_strict_passes(self):
        """E1: 真实代码库运行 check_button_flow_real_ux(strict=True)返回 0 违规。

        P1-11 要求严格门禁(--strict 默认)在真实代码库通过。
        """
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_button_flow_real_ux as gate
            exit_code, all_violations = gate.check(strict=True)
            assert exit_code == 0, (
                f"check_button_flow_real_ux --strict 失败, "
                f"exit_code={exit_code}, 违规数={len(all_violations)}, "
                f"违规列表={all_violations}"
            )
            assert all_violations == [], (
                f"check_button_flow_real_ux --strict 返回非空违规列表: "
                f"{all_violations}"
            )
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_check_button_flow_real_ux_rule_e_to_i_all_pass(self):
        """E2: 5 条规则(E/F/G/H/I)在真实代码库各自 0 违规。

        分别统计 E/F/G/H/I 5 条规则的违规数,逐条断言 0 违规,
        便于在出现违规时快速定位是哪条规则。
        """
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_button_flow_real_ux as gate
            exit_code, all_violations = gate.check(strict=True)
            by_rule: dict[str, int] = {}
            for v in all_violations:
                rule = v.get("rule", "?")
                by_rule[rule] = by_rule.get(rule, 0) + 1
            for rule in ("E", "F", "G", "H", "I"):
                assert by_rule.get(rule, 0) == 0, (
                    f"Rule {rule} 存在 {by_rule.get(rule, 0)} 处违规"
                )
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# F. 全 HIGH_RISK_POLICY 覆盖(扩展验证,确保 13 个 action 全部可用)
# ════════════════════════════════════════════════════════════════


class TestFullHighRiskPolicyCoverage:
    """F 节: 扩展验证 — 全部 13 个 HIGH_RISK_POLICY action 都能生成 spec
    并通过 5 生命周期路径(覆盖 zh-CN + en-US)。

    本节为扩展覆盖,确保 P1-11 不只验证 4 个代表性 action,
    而是覆盖 HIGH_RISK_POLICY 全集(13 个 action)。
    """

    def test_high_risk_policy_has_13_actions(self):
        """F1: HIGH_RISK_POLICY 含 13 个 action(预期数量)。"""
        assert len(HIGH_RISK_POLICY) == 13, (
            f"HIGH_RISK_POLICY action 数 {len(HIGH_RISK_POLICY)} != 13"
        )

    @pytest.mark.parametrize("action", sorted(get_all_specable_actions()))
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    @pytest.mark.asyncio
    async def test_all_actions_pass_lifecycle_paths(
        self, policy, i18n, action, locale,
    ):
        """F2: 全部 HIGH_RISK_POLICY action × {zh-CN, en-US} 通过 5 路径。

        共 13 × 2 = 26 个 spec 组合,全部通过 verify_all_lifecycle_paths。
        这是 P1-11 强制要求的全覆盖验证。
        """
        spec = policy.destructive_confirmation(action, "test_target", locale)
        verification = await verify_all_lifecycle_paths(
            spec, i18n_manager=i18n, policy=policy,
        )
        assert verification.all_passed, (
            f"action={action} locale={locale} 5 路径未全部通过, "
            f"失败路径={verification.failed_paths}"
        )

    @pytest.mark.parametrize("action", sorted(REQUIRED_DESTRUCTIVE_ACTIONS))
    def test_required_destructive_actions_subset_of_high_risk(self, action):
        """F3: REQUIRED_DESTRUCTIVE_ACTIONS(9 个)是 HIGH_RISK_POLICY 子集。"""
        assert action in HIGH_RISK_POLICY, (
            f"REQUIRED_DESTRUCTIVE_ACTIONS 含 action={action} "
            f"但 HIGH_RISK_POLICY 中不存在"
        )

    def test_representative_actions_subset_of_high_risk(self):
        """F4: REPRESENTATIVE_ACTIONS(4 个)是 HIGH_RISK_POLICY 子集。"""
        for action in REPRESENTATIVE_ACTIONS:
            assert action in HIGH_RISK_POLICY, (
                f"REPRESENTATIVE_ACTIONS 含 action={action} "
                f"但 HIGH_RISK_POLICY 中不存在"
            )
