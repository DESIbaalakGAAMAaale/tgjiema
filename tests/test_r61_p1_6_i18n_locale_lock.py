"""R61 P1-06: i18n locale 锁定 + release 构建严格模式 + 高优先级告警测试。

审计 P1-06 整改要求:
1. 锁定支持的 locale 为 zh-CN/en-US(显式拒绝其他 locale)
2. 使用 Babel/ICU 标准库,不自维护 CLDR
3. CI 双向对称检查(key/参数/plural/select/selectordinal AST/HTML 安全上下文)
4. 生产可回退安全文案,但必须触发高优先级告警
5. release 构建:缺 key/缺参/malformed ICU 直接 fail-fast

测试覆盖:
    A. SUPPORTED_LOCALES 锁定
        - 常量值为 {"zh-CN", "en-US"}
        - load_locale 拒绝 ru-RU / fr-FR 等非锁定 locale
        - set_default_locale 拒绝非锁定 locale 且不修改默认值
        - set_user_locale 对非锁定 locale 抛 AppError(优先于目录扫描)
    B. release 构建严格模式(RELEASE_BUILD=1)
        - 缺失 key 直接抛 AppError(不返回安全文案)
        - ICU 缺参直接抛 AppError
    C. 高优先级告警回调
        - 生产环境(非 release)缺失 key 时回调被调用
        - 回调注销后不再被调用(降级为 ERROR 日志)
    D. Babel CLDR ordinal 委托
        - en-US: 1→one / 2→two / 3→few / 11→other
        - zh-CN: 始终 other
        (Babel 缺失时回退到自维护规则,结果一致,测试在两种环境下均通过)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── i18n 单例 + 告警回调重置(避免跨用例污染) ─────────────────────


@pytest.fixture(autouse=True)
def reset_i18n_state():
    """每个用例前重置 i18n 模块级单例 + 告警回调,避免跨用例状态污染。"""
    import services.i18n as i18n_mod
    old_manager = i18n_mod._i18n_manager
    old_cb = i18n_mod._i18n_alert_callback
    i18n_mod._i18n_manager = None
    i18n_mod._i18n_alert_callback = None
    # 清理可能残留的 RELEASE_BUILD 环境变量(防御,monkeypatch 已自动还原)
    old_env = os.environ.get("RELEASE_BUILD")
    os.environ.pop("RELEASE_BUILD", None)
    yield
    i18n_mod._i18n_manager = old_manager
    i18n_mod._i18n_alert_callback = old_cb
    if old_env is not None:
        os.environ["RELEASE_BUILD"] = old_env
    else:
        os.environ.pop("RELEASE_BUILD", None)


# ════════════════════════════════════════════════════════════════
# A. SUPPORTED_LOCALES 锁定
# ════════════════════════════════════════════════════════════════


class TestSupportedLocalesLock:
    """A 节: SUPPORTED_LOCALES 锁定 — 拒绝加载/设置/写入非 zh-CN/en-US locale。"""

    def test_supported_locales_constant_value(self):
        """A1: SUPPORTED_LOCALES 恰好为 {"zh-CN", "en-US"}。"""
        import services.i18n as i18n_mod
        assert i18n_mod.SUPPORTED_LOCALES == frozenset({"zh-CN", "en-US"})
        # 应为 frozenset(不可变,防止运行时被篡改)
        assert isinstance(i18n_mod.SUPPORTED_LOCALES, frozenset)

    def test_load_locale_rejects_unsupported(self):
        """A2: load_locale 对非锁定 locale(如 ru-RU)返回 False。"""
        import services.i18n as i18n_mod
        manager = i18n_mod.I18nManager()
        # ru-RU 不在 SUPPORTED_LOCALES 中 → 拒绝
        assert manager.load_locale("ru-RU") is False
        # fr-FR 同样拒绝
        assert manager.load_locale("fr-FR") is False
        # ru-RU 不应被加载到翻译缓存中
        assert "ru-RU" not in manager._translations

    def test_load_locale_accepts_supported(self):
        """A3: load_locale 对锁定 locale(zh-CN/en-US)正常加载。"""
        import services.i18n as i18n_mod
        manager = i18n_mod.I18nManager()
        assert manager.load_locale("zh-CN") is True
        assert manager.load_locale("en-US") is True
        assert "zh-CN" in manager._translations
        assert "en-US" in manager._translations

    def test_set_default_locale_rejects_unsupported(self):
        """A4: set_default_locale 对非锁定 locale 返回 False 且不改默认值。"""
        import services.i18n as i18n_mod
        manager = i18n_mod.I18nManager()
        original_default = manager.default_locale
        assert manager.set_default_locale("ru-RU") is False
        # 默认 locale 不应被修改
        assert manager.default_locale == original_default

    def test_set_default_locale_accepts_supported(self):
        """A5: set_default_locale 对锁定 locale 正常切换。"""
        import services.i18n as i18n_mod
        manager = i18n_mod.I18nManager()
        # 先加载默认 zh-CN,再切换到 en-US
        assert manager.set_default_locale("en-US") is True
        assert manager.default_locale == "en-US"

    def test_set_user_locale_rejects_unsupported_raises_apperror(self):
        """A6: set_user_locale 对非锁定 locale 抛 AppError(优先于 DB 写入)。

        注:VALIDATION_FAILED 的 ErrorDefinition.safe_params 仅暴露 ``field``
        (隐私/安全设计,见 services/error_codes.py),``locale``/``supported``
        等参数会被 ErrorRegistry.create_envelope 过滤,不通过 params 暴露给用户。
        因此本用例仅校验错误码 + ``field`` 安全参数。
        """
        import services.i18n as i18n_mod
        from services.error_codes import AppError, ErrorCodes
        # ru-RU 不在 SUPPORTED_LOCALES → 抛 AppError(不应触达 DB)
        with pytest.raises(AppError) as exc_info:
            i18n_mod.set_user_locale(123456, "ru-RU")
        # 错误码应为 VALIDATION_FAILED
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        # safe_params 白名单仅暴露 field(VALIDATION_FAILED 的设计)
        assert exc_info.value.params.get("field") == "locale"

    def test_set_user_locale_unsupported_before_db(self):
        """A7: set_user_locale 拒绝非锁定 locale 时不依赖 DB(无 DB 也能抛错)。"""
        import services.i18n as i18n_mod
        from services.error_codes import AppError
        # fr-FR 拒绝应在任何 DB 访问之前发生
        with pytest.raises(AppError):
            i18n_mod.set_user_locale(999999, "fr-FR")


# ════════════════════════════════════════════════════════════════
# B. release 构建严格模式(RELEASE_BUILD=1 → fail-fast)
# ════════════════════════════════════════════════════════════════


class TestReleaseModeFailFast:
    """B 节: release 构建下任何 i18n 缺陷直接抛 AppError(不返回安全文案)。

    验证 R61 P1-06 要求 5:release 构建 fail-fast。
    """

    def test_release_mode_missing_key_raises_apperror(self, monkeypatch):
        """B1: RELEASE_BUILD=1 时缺失 key 直接抛 AppError。

        注:VALIDATION_FAILED 的 safe_params 仅暴露 ``field``,
        ``reason``/``key``/``locale`` 会被过滤,故仅校验错误码 + ``field``。
        """
        import services.i18n as i18n_mod
        from services.error_codes import AppError, ErrorCodes
        monkeypatch.setenv("RELEASE_BUILD", "1")
        manager = i18n_mod.get_i18n_manager()
        with pytest.raises(AppError) as exc_info:
            manager.translate(
                "nonexistent.key.p1_6.release_test", locale="zh-CN"
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        assert exc_info.value.params.get("field") == "i18n_key"

    def test_release_mode_missing_param_raises_apperror(self, monkeypatch):
        """B2: RELEASE_BUILD=1 时 ICU 缺参直接抛 AppError。

        common.files.count_icu 引用 {count},故意不传 count 触发缺参检测。
        """
        import services.i18n as i18n_mod
        from services.error_codes import AppError
        monkeypatch.setenv("RELEASE_BUILD", "1")
        manager = i18n_mod.get_i18n_manager()
        # 传一个无关参数,使 kwargs 非空(否则 format_message_icu 会提前 return)
        with pytest.raises(AppError):
            manager.format_message_icu(
                "common.files.count_icu", locale="zh-CN", foo="bar"
            )

    def test_non_release_missing_key_returns_safe_fallback(self, monkeypatch):
        """B3: 非 release 模式缺失 key 返回安全文案(不抛错)。

        对照测试:确认 release 严格模式仅在 RELEASE_BUILD=1 时生效。
        """
        import services.i18n as i18n_mod
        # 确保 RELEASE_BUILD 未设置
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        manager = i18n_mod.get_i18n_manager()
        # 非缺失 key 不应抛错,应返回安全通用文案
        result = manager.translate(
            "nonexistent.key.p1_6.no_release", locale="zh-CN"
        )
        assert isinstance(result, str)
        assert len(result) > 0
        # 安全文案不应暴露内部 key
        assert "nonexistent.key.p1_6.no_release" not in result

    def test_release_mode_truthy_variants(self, monkeypatch):
        """B4: RELEASE_BUILD 的多种真值写法(1/true/yes/on)均触发 release 模式。"""
        import services.i18n as i18n_mod
        from services.error_codes import AppError
        manager = i18n_mod.get_i18n_manager()
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("RELEASE_BUILD", val)
            with pytest.raises(AppError):
                manager.translate(
                    f"nonexistent.key.{val}", locale="zh-CN"
                )
        # false / 空 / 未设置 → 不抛错
        for val in ("", "0", "false", "no"):
            monkeypatch.setenv("RELEASE_BUILD", val)
            result = manager.translate(
                f"nonexistent.key.{val}", locale="zh-CN"
            )
            assert isinstance(result, str)


# ════════════════════════════════════════════════════════════════
# C. 高优先级告警回调
# ════════════════════════════════════════════════════════════════


class TestAlertCallback:
    """C 节: 生产环境(非 release)缺失 key 时触发高优先级告警回调。

    验证 R61 P1-06 要求 4:生产可回退安全文案,但必须触发高优先级告警。
    """

    def test_alert_callback_invoked_on_missing_key(self, monkeypatch):
        """C1: 注册回调后,缺失 key 触发回调(event_type='missing_key')。"""
        import services.i18n as i18n_mod
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        received: list[tuple[str, dict]] = []

        def cb(event_type: str, details: dict) -> None:
            received.append((event_type, dict(details)))

        i18n_mod.set_i18n_alert_callback(cb)
        try:
            manager = i18n_mod.get_i18n_manager()
            manager.translate("nonexistent.key.p1_6.alert", locale="zh-CN")
        finally:
            i18n_mod.set_i18n_alert_callback(None)

        # 回调应被调用恰好一次(缺失 key)
        assert len(received) == 1, (
            f"告警回调应被调用 1 次,实际: {len(received)};received={received}"
        )
        event_type, details = received[0]
        assert event_type == "missing_key"
        assert details.get("key") == "nonexistent.key.p1_6.alert"
        assert details.get("locale") == "zh-CN"

    def test_alert_callback_not_invoked_when_key_exists(self, monkeypatch):
        """C2: key 存在时不触发告警回调。"""
        import services.i18n as i18n_mod
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        received: list[tuple[str, dict]] = []
        i18n_mod.set_i18n_alert_callback(
            lambda et, d: received.append((et, dict(d)))
        )
        try:
            manager = i18n_mod.get_i18n_manager()
            # common.ok 是真实存在的 key
            result = manager.translate("common.ok", locale="zh-CN")
        finally:
            i18n_mod.set_i18n_alert_callback(None)
        assert result  # 应返回非空翻译
        assert received == [], (
            f"key 存在时不应触发告警回调,实际: {received}"
        )

    def test_alert_callback_unregister(self, monkeypatch):
        """C3: 注销回调后不再调用(降级为 ERROR 日志,不抛错)。"""
        import services.i18n as i18n_mod
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        received: list[tuple[str, dict]] = []
        i18n_mod.set_i18n_alert_callback(
            lambda et, d: received.append((et, dict(d)))
        )
        i18n_mod.set_i18n_alert_callback(None)  # 注销
        manager = i18n_mod.get_i18n_manager()
        # 注销后缺失 key 不应调用回调(降级为 ERROR 日志)
        result = manager.translate("nonexistent.key.p1_6.unregistered", locale="zh-CN")
        assert isinstance(result, str)
        assert received == [], "注销回调后不应再调用回调"

    def test_alert_callback_exception_does_not_propagate(self, monkeypatch):
        """C4: 回调自身抛异常时不影响主流程(吞掉异常,返回安全文案)。"""
        import services.i18n as i18n_mod
        monkeypatch.delenv("RELEASE_BUILD", raising=False)

        def bad_cb(event_type: str, details: dict) -> None:
            raise RuntimeError("callback boom")

        i18n_mod.set_i18n_alert_callback(bad_cb)
        try:
            manager = i18n_mod.get_i18n_manager()
            # 回调抛异常不应传播到调用方
            result = manager.translate(
                "nonexistent.key.p1_6.bad_cb", locale="zh-CN"
            )
            assert isinstance(result, str)
        finally:
            i18n_mod.set_i18n_alert_callback(None)


# ════════════════════════════════════════════════════════════════
# D. Babel CLDR ordinal 委托
# ════════════════════════════════════════════════════════════════


class TestBabelOrdinalDelegation:
    """D 节: _cldr_ordinal_category 优先委托 Babel 标准 CLDR ordinal 规则。

    验证 R61 P1-06 要求 2:使用 Babel/ICU 标准库,不自维护 CLDR。
    Babel 缺失时回退到自维护的 en/ru/zh 规则,结果一致 — 测试在两种
    环境下(Babel 已安装 / 未安装)均应通过。
    """

    def test_ordinal_english_categories(self):
        """D1: en-US ordinal 类别符合 CLDR 规则(1st/2nd/3rd/4th)。"""
        import services.i18n as i18n_mod
        # 1 → one (1st), 2 → two (2nd), 3 → few (3rd), 4 → other (4th)
        assert i18n_mod._cldr_ordinal_category(1, "en-US") == "one"
        assert i18n_mod._cldr_ordinal_category(2, "en-US") == "two"
        assert i18n_mod._cldr_ordinal_category(3, "en-US") == "few"
        assert i18n_mod._cldr_ordinal_category(4, "en-US") == "other"
        # 11 → other (11th), 21 → one (21st), 101 → one (101st)
        assert i18n_mod._cldr_ordinal_category(11, "en-US") == "other"
        assert i18n_mod._cldr_ordinal_category(21, "en-US") == "one"
        assert i18n_mod._cldr_ordinal_category(101, "en-US") == "one"

    def test_ordinal_chinese_always_other(self):
        """D2: zh-CN ordinal 始终 other(中文不区分序数形式)。"""
        import services.i18n as i18n_mod
        for n in (0, 1, 2, 3, 4, 11, 21, 100, 101):
            assert i18n_mod._cldr_ordinal_category(n, "zh-CN") == "other", (
                f"zh-CN ordinal({n}) 应为 other"
            )

    def test_ordinal_negative_takes_absolute_value(self):
        """D3: 负数取绝对值后套用规则(防御性)。"""
        import services.i18n as i18n_mod
        # -1 → abs=1 → one(en-US)
        assert i18n_mod._cldr_ordinal_category(-1, "en-US") == "one"
        assert i18n_mod._cldr_ordinal_category(-11, "en-US") == "other"

    def test_plural_uses_babel_for_english(self):
        """D4: plural 分支通过 Babel 选择 en-US count=1 → one。"""
        import services.i18n as i18n_mod
        # branches 含 one/other;count=1 → one,count=2 → other
        branches = {"one": "1 item", "other": "many items"}
        assert i18n_mod._icu_select_branch_by_count(
            1, "plural", "en-US", branches
        ) == "1 item"
        assert i18n_mod._icu_select_branch_by_count(
            2, "plural", "en-US", branches
        ) == "many items"
        assert i18n_mod._icu_select_branch_by_count(
            0, "plural", "en-US", branches
        ) == "many items"  # en: 0 → other

    def test_plural_chinese_always_other(self):
        """D5: zh-CN plural 始终 other(中文不区分单复数)。"""
        import services.i18n as i18n_mod
        branches = {"one": "1 item", "other": "many items"}
        for n in (0, 1, 2, 5, 100):
            assert i18n_mod._icu_select_branch_by_count(
                n, "plural", "zh-CN", branches
            ) == "many items", f"zh-CN plural({n}) 应走 other"
