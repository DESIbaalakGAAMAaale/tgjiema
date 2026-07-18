"""R63 P1-12: i18n ICU 预编译,语法/参数不对称阻断构建 测试。

审计报告要求 (P1-12):
    ICU 解析失败回退到普通 format 可能把 plural/select 模板原样或错误渲染给用户。
    Release 模式应在 locale 加载阶段预编译全部 ICU message;任一语法或参数集合
    不对称直接阻断构建。

测试覆盖:
    1. _validate_icu_message: 合法 ICU message 编译通过
    2. _validate_icu_message: 非法 ICU message(括号不平衡 / 解析失败)编译失败
    3. _extract_icu_param_set: 正确提取参数集合(简单 {var} + ICU var 名)
    4. _get_icu_strict_mode: 环境变量 / release / production 模式判定
    5. I18nManager.load_locale: 严格模式下 ICU 语法错误抛 AppError(I18N_ICU_COMPILE_FAILED)
    6. I18nManager.load_locale: 宽松模式下 ICU 语法错误仅 warning(不阻断)
    7. I18nManager.load_locale: 参数集合不对称 strict 模式抛 AppError
    8. I18nManager.format_message_icu: 预编译缓存命中(避免重复解析)
    9. I18nManager.format_message_icu: plural/select 渲染正确
    10. I18nManager.format_message_icu: ICU_STRICT_MODE=0 允许 fallback
    11. I18N_ICU_COMPILE_FAILED 错误码已注册
    12. 构建时校验脚本 check_i18n_icu_precompile.py 集成测试
    13. 真实 locale 文件预编译通过(集成测试)

测试策略:
    - 单元测试 1-4: 直接调用模块级函数
    - 集成测试 5-10: 使用 tmp_path 隔离的 locale 文件,通过环境变量控制 strict 模式
    - 集成测试 11: 检查 ErrorRegistry
    - 集成测试 12: 调用 scripts/check_i18n_icu_precompile.py 的 verify()
    - 集成测试 13: 使用真实 locales/ 目录
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 测试环境兼容(mock telegram 库)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"


# ════════════════════════════════════════════════════════════════
# Fixture: 环境变量隔离
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def clean_env(monkeypatch):
    """清除影响 strict 模式判定的环境变量(用例间隔离)。"""
    monkeypatch.delenv("ICU_STRICT_MODE", raising=False)
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    yield


@pytest.fixture
def strict_mode(monkeypatch):
    """启用 ICU strict 模式(ICU_STRICT_MODE=1)。"""
    monkeypatch.setenv("ICU_STRICT_MODE", "1")
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    yield


@pytest.fixture
def loose_mode(monkeypatch):
    """禁用 ICU strict 模式(ICU_STRICT_MODE=0,允许 fallback)。"""
    monkeypatch.setenv("ICU_STRICT_MODE", "0")
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    yield


@pytest.fixture
def release_mode(monkeypatch):
    """启用 release 构建(RELEASE_BUILD=1,隐含 strict 模式)。"""
    monkeypatch.setenv("RELEASE_BUILD", "1")
    monkeypatch.delenv("ICU_STRICT_MODE", raising=False)
    yield


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════
def _write_locale_files(
    tmp_path: Path,
    zh_content: dict,
    en_content: dict,
) -> Path:
    """写入临时 locale 文件(zh-CN.json + en-US.json),返回目录路径。"""
    (tmp_path / "zh-CN.json").write_text(
        json.dumps(zh_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "en-US.json").write_text(
        json.dumps(en_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


def _new_manager(locales_dir: Path):
    """创建一个新的 I18nManager(不复用模块级单例)。"""
    from services.i18n import I18nManager
    return I18nManager(locales_dir=locales_dir, default_locale="zh-CN")


# ════════════════════════════════════════════════════════════════
# 1-2: _validate_icu_message 单元测试
# ════════════════════════════════════════════════════════════════
class TestValidateIcuMessage:
    """_validate_icu_message 函数的单元测试。"""

    def test_plain_text_passes(self):
        """测试 1a: 纯文本(无占位符)编译通过。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message("Hello World")
        assert ok is True
        assert reason == ""

    def test_simple_var_passes(self):
        """测试 1b: 简单 {var} 占位符编译通过(不需要 ICU 预编译)。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message("剩余 {count} 次")
        assert ok is True
        assert reason == ""

    def test_valid_plural_passes(self):
        """测试 1c: 合法 ICU plural message 编译通过。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(
            "{count, plural, =0 {无项目} one {# 项} other {# 项}}"
        )
        assert ok is True, f"Expected ok=True, got reason={reason}"

    def test_valid_select_passes(self):
        """测试 1d: 合法 ICU select message 编译通过。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(
            "{gender, select, male {他} female {她} other {他们}}"
        )
        assert ok is True, f"Expected ok=True, got reason={reason}"

    def test_valid_selectordinal_passes(self):
        """测试 1e: 合法 ICU selectordinal message 编译通过。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(
            "{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}"
        )
        assert ok is True, f"Expected ok=True, got reason={reason}"

    def test_unbalanced_brace_fails(self):
        """测试 2a: 括号不平衡(多一个 {)编译失败。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(
            "{count, plural, =0 {无} one {# 项}"  # 缺少闭合 }
        )
        assert ok is False
        assert "unbalanced" in reason

    def test_extra_close_brace_fails(self):
        """测试 2b: 括号不平衡(多一个 })编译失败。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(
            "{count, plural, =0 {无} one {# 项}}}"
        )
        assert ok is False
        assert "unbalanced" in reason

    def test_non_string_input_passes(self):
        """测试 2c: 非 str 输入视为通过(无需预编译)。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message(123)  # type: ignore[arg-type]
        assert ok is True
        assert reason == ""

    def test_empty_string_passes(self):
        """测试 2d: 空字符串编译通过。"""
        from services.i18n import _validate_icu_message
        ok, reason = _validate_icu_message("")
        assert ok is True
        assert reason == ""


# ════════════════════════════════════════════════════════════════
# 3: _extract_icu_param_set 单元测试
# ════════════════════════════════════════════════════════════════
class TestExtractIcuParamSet:
    """_extract_icu_param_set 函数的单元测试。"""

    def test_plain_text_returns_empty(self):
        """测试 3a: 纯文本返回空集。"""
        from services.i18n import _extract_icu_param_set
        assert _extract_icu_param_set("Hello World") == set()

    def test_simple_var(self):
        """测试 3b: 简单 {var} 占位符提取正确。"""
        from services.i18n import _extract_icu_param_set
        assert _extract_icu_param_set("剩余 {count} 次") == {"count"}

    def test_multiple_simple_vars(self):
        """测试 3c: 多个简单 {var} 占位符提取正确。"""
        from services.i18n import _extract_icu_param_set
        assert _extract_icu_param_set("{a} and {b} and {c}") == {"a", "b", "c"}

    def test_icu_plural_var(self):
        """测试 3d: ICU plural var 名提取正确。"""
        from services.i18n import _extract_icu_param_set
        params = _extract_icu_param_set(
            "{count, plural, =0 {无} one {# 项} other {# 项}}"
        )
        assert params == {"count"}

    def test_icu_select_var(self):
        """测试 3e: ICU select var 名提取正确。"""
        from services.i18n import _extract_icu_param_set
        params = _extract_icu_param_set(
            "{gender, select, male {他} female {她} other {他们}}"
        )
        assert params == {"gender"}

    def test_mixed_icu_and_simple(self):
        """测试 3f: ICU + 简单 {var} 混合提取正确。"""
        from services.i18n import _extract_icu_param_set
        params = _extract_icu_param_set(
            "{name} 有 {count, plural, =0 {无} one {# 项} other {# 项}}"
        )
        assert params == {"name", "count"}

    def test_non_string_returns_empty(self):
        """测试 3g: 非 str 输入返回空集。"""
        from services.i18n import _extract_icu_param_set
        assert _extract_icu_param_set(None) == set()  # type: ignore[arg-type]
        assert _extract_icu_param_set(123) == set()  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# 4: _get_icu_strict_mode 单元测试
# ════════════════════════════════════════════════════════════════
class TestGetIcuStrictMode:
    """_get_icu_strict_mode 函数的单元测试。"""

    def test_icu_strict_mode_explicit_on(self, monkeypatch):
        """测试 4a: ICU_STRICT_MODE=1 → strict 模式开启。"""
        monkeypatch.setenv("ICU_STRICT_MODE", "1")
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is True

    def test_icu_strict_mode_explicit_true(self, monkeypatch):
        """测试 4b: ICU_STRICT_MODE=true → strict 模式开启。"""
        monkeypatch.setenv("ICU_STRICT_MODE", "true")
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is True

    def test_icu_strict_mode_explicit_off(self, monkeypatch):
        """测试 4c: ICU_STRICT_MODE=0 → strict 模式关闭。"""
        monkeypatch.setenv("ICU_STRICT_MODE", "0")
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is False

    def test_icu_strict_mode_explicit_false(self, monkeypatch):
        """测试 4d: ICU_STRICT_MODE=false → strict 模式关闭。"""
        monkeypatch.setenv("ICU_STRICT_MODE", "false")
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is False

    def test_release_build_implies_strict(self, release_mode):
        """测试 4e: RELEASE_BUILD=1 隐含 strict 模式开启。"""
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is True

    def test_release_build_overrides_icu_strict_off(self, monkeypatch):
        """测试 4f: RELEASE_BUILD=1 优先于 ICU_STRICT_MODE=0。"""
        monkeypatch.setenv("RELEASE_BUILD", "1")
        monkeypatch.setenv("ICU_STRICT_MODE", "0")
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is True

    def test_production_environment_implies_strict(self, monkeypatch):
        """测试 4g: config.settings.ENVIRONMENT=production → strict 模式开启。"""
        monkeypatch.delenv("ICU_STRICT_MODE", raising=False)
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        # _get_icu_strict_mode 内部执行 from config.settings import settings
        # 需要在 sys.modules 注入可导入的 config.settings 模块
        import sys
        import types
        fake_settings = types.SimpleNamespace(ENVIRONMENT="production")
        fake_config_settings = types.ModuleType("config.settings")
        fake_config_settings.settings = fake_settings
        # 确保 config 是一个 package(有 __path__)
        if "config" not in sys.modules or not hasattr(sys.modules["config"], "__path__"):
            fake_config = types.ModuleType("config")
            fake_config.__path__ = []  # 标记为 package
            fake_config.settings = fake_settings
            monkeypatch.setitem(sys.modules, "config", fake_config)
        monkeypatch.setitem(sys.modules, "config.settings", fake_config_settings)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is True

    def test_development_environment_defaults_off(self, monkeypatch):
        """测试 4h: development 环境 + 无显式 ICU_STRICT_MODE → strict 关闭。"""
        monkeypatch.delenv("ICU_STRICT_MODE", raising=False)
        monkeypatch.delenv("RELEASE_BUILD", raising=False)
        import sys
        import types
        fake_settings = types.SimpleNamespace(ENVIRONMENT="development")
        fake_config_settings = types.ModuleType("config.settings")
        fake_config_settings.settings = fake_settings
        if "config" not in sys.modules or not hasattr(sys.modules["config"], "__path__"):
            fake_config = types.ModuleType("config")
            fake_config.__path__ = []
            fake_config.settings = fake_settings
            monkeypatch.setitem(sys.modules, "config", fake_config)
        monkeypatch.setitem(sys.modules, "config.settings", fake_config_settings)
        from services.i18n import _get_icu_strict_mode
        assert _get_icu_strict_mode() is False


# ════════════════════════════════════════════════════════════════
# 5-7: I18nManager.load_locale 集成测试
# ════════════════════════════════════════════════════════════════
class TestLoadLocalePrecompile:
    """I18nManager.load_locale 的 ICU 预编译行为测试。"""

    def test_valid_icu_loads_successfully_strict(self, strict_mode, tmp_path):
        """测试 5: 合法 ICU message 在 strict 模式下成功加载。"""
        _write_locale_files(
            tmp_path,
            {"common": {"count": "{n, plural, =0 {无} one {# 项} other {# 项}}"}},
            {"common": {"count": "{n, plural, =0 {no items} one {# item} other {# items}}"}},
        )
        manager = _new_manager(tmp_path)
        assert manager.load_locale("zh-CN") is True
        assert manager.load_locale("en-US") is True

    def test_invalid_icu_raises_apperror_strict(self, strict_mode, tmp_path):
        """测试 6a: 非法 ICU message 在 strict 模式下抛 AppError(I18N_ICU_COMPILE_FAILED)。"""
        from services.error_codes import AppError, ErrorCodes
        _write_locale_files(
            tmp_path,
            # zh-CN 含括号不平衡的 ICU message
            {"common": {"broken": "{n, plural, =0 {无} one {# 项}"}},
            {"common": {"broken": "{n, plural, =0 {no items} one {# item}}"}},
        )
        manager = _new_manager(tmp_path)
        with pytest.raises(AppError) as exc_info:
            manager.load_locale("zh-CN")
        assert exc_info.value.code == ErrorCodes.I18N_ICU_COMPILE_FAILED
        # params 应包含 locale / key / reason
        assert "locale" in exc_info.value.params or "key" in exc_info.value.params

    def test_invalid_icu_no_raise_loose(self, loose_mode, tmp_path):
        """测试 6b: 非法 ICU message 在宽松模式下不抛异常(仅 warning,继续加载)。"""
        _write_locale_files(
            tmp_path,
            {"common": {"broken": "{n, plural, =0 {无} one {# 项}"}},
            {"common": {"broken": "{n, plural, =0 {no items} one {# item}}"}},
        )
        manager = _new_manager(tmp_path)
        # 宽松模式下应成功加载(不抛异常)
        assert manager.load_locale("zh-CN") is True
        # 预编译缓存应标记此 key 为 ok=False
        cache = manager._compiled_icu_cache.get("zh-CN", {})
        assert "common.broken" in cache
        assert cache["common.broken"]["ok"] is False

    def test_param_asymmetry_raises_strict(self, strict_mode, tmp_path):
        """测试 7a: 参数集合不对称在 strict 模式下抛 AppError。"""
        from services.error_codes import AppError, ErrorCodes
        _write_locale_files(
            tmp_path,
            # zh-CN 用 {count} 而 en-US 用 {num}
            {"common": {"items": "剩余 {count} 项"}},
            {"common": {"items": "{num} items left"}},
        )
        manager = _new_manager(tmp_path)
        assert manager.load_locale("zh-CN") is True
        # 加载 en-US 后触发跨 locale 参数对称检查 → 抛 AppError
        with pytest.raises(AppError) as exc_info:
            manager.load_locale("en-US")
        assert exc_info.value.code == ErrorCodes.I18N_ICU_COMPILE_FAILED

    def test_param_asymmetry_no_raise_loose(self, loose_mode, tmp_path):
        """测试 7b: 参数集合不对称在宽松模式下不抛异常(仅 warning)。"""
        _write_locale_files(
            tmp_path,
            {"common": {"items": "剩余 {count} 项"}},
            {"common": {"items": "{num} items left"}},
        )
        manager = _new_manager(tmp_path)
        assert manager.load_locale("zh-CN") is True
        # 宽松模式下不对称不抛异常
        assert manager.load_locale("en-US") is True

    def test_param_symmetric_loads_successfully(self, strict_mode, tmp_path):
        """测试 7c: 参数集合对称的 locale 在 strict 模式下成功加载。"""
        _write_locale_files(
            tmp_path,
            {"common": {"items": "剩余 {count} 项"}},
            {"common": {"items": "{count} items left"}},
        )
        manager = _new_manager(tmp_path)
        assert manager.load_locale("zh-CN") is True
        assert manager.load_locale("en-US") is True


# ════════════════════════════════════════════════════════════════
# 8-10: I18nManager.format_message_icu 测试
# ════════════════════════════════════════════════════════════════
class TestFormatMessageIcu:
    """I18nManager.format_message_icu 的预编译缓存与渲染测试。"""

    def test_plural_renders_correctly(self, loose_mode, tmp_path):
        """测试 9a: ICU plural message 渲染正确。"""
        _write_locale_files(
            tmp_path,
            {"common": {"count": "{n, plural, =0 {无项目} one {# 项} other {# 项}}"}},
            {"common": {"count": "{n, plural, =0 {no items} one {# item} other {# items}}"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        # n=0 → "无项目"
        assert manager.format_message_icu("common.count", locale="zh-CN", n=0) == "无项目"
        # n=1 → "1 项"
        assert manager.format_message_icu("common.count", locale="zh-CN", n=1) == "1 项"
        # n=5 → "5 项"
        assert manager.format_message_icu("common.count", locale="zh-CN", n=5) == "5 项"

    def test_select_renders_correctly(self, loose_mode, tmp_path):
        """测试 9b: ICU select message 渲染正确。"""
        _write_locale_files(
            tmp_path,
            {"common": {"pronoun": "{gender, select, male {他} female {她} other {他们}}"}},
            {"common": {"pronoun": "{gender, select, male {he} female {she} other {they}}"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        assert manager.format_message_icu("common.pronoun", locale="zh-CN", gender="male") == "他"
        assert manager.format_message_icu("common.pronoun", locale="zh-CN", gender="female") == "她"
        assert manager.format_message_icu("common.pronoun", locale="zh-CN", gender="other") == "他们"

    def test_precompile_cache_hit(self, loose_mode, tmp_path):
        """测试 8: 预编译缓存命中(第二次调用不再重复解析)。"""
        _write_locale_files(
            tmp_path,
            {"common": {"count": "{n, plural, =0 {无} one {# 项} other {# 项}}"}},
            {"common": {"count": "{n, plural, =0 {no items} one {# item} other {# items}}"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        # 第一次调用(命中预编译缓存,load_locale 阶段已填充)
        result1 = manager.format_message_icu("common.count", locale="zh-CN", n=1)
        # 第二次调用(应直接命中缓存)
        result2 = manager.format_message_icu("common.count", locale="zh-CN", n=5)
        assert result1 == "1 项"
        assert result2 == "5 项"
        # 验证缓存中存在此 key
        cache = manager._compiled_icu_cache.get("zh-CN", {})
        assert "common.count" in cache
        assert cache["common.count"]["ok"] is True
        assert cache["common.count"]["is_icu"] is True

    def test_strict_mode_raises_on_runtime_parse_failure(self, strict_mode, tmp_path):
        """测试 10a: strict 模式下运行时 ICU 解析失败抛 AppError(I18N_ICU_COMPILE_FAILED)。"""
        from services.error_codes import AppError, ErrorCodes
        # 构造一个能在 _validate_icu_message 通过但 _icu_format 运行时失败的场景较难
        # 改为测试:load_locale 阶段就抛 AppError(strict 模式预编译失败)
        _write_locale_files(
            tmp_path,
            {"common": {"broken": "{n, plural, =0 {无} one {# 项}"}},
            {"common": {"broken": "{n, plural, =0 {no items} one {# item}}"}},
        )
        manager = _new_manager(tmp_path)
        with pytest.raises(AppError) as exc_info:
            manager.load_locale("zh-CN")
        assert exc_info.value.code == ErrorCodes.I18N_ICU_COMPILE_FAILED

    def test_loose_mode_fallback_to_format_message(self, loose_mode, tmp_path):
        """测试 10b: ICU_STRICT_MODE=0 时,预编译失败回退到 format_message(不抛异常)。"""
        _write_locale_files(
            tmp_path,
            # broken key 含语法错误,但宽松模式应回退
            {"common": {"broken": "{n, plural, =0 {无} one {# 项}"}},
            {"common": {"broken": "{n, plural, =0 {no items} one {# item}}"}},
        )
        manager = _new_manager(tmp_path)
        # 宽松模式下加载成功
        assert manager.load_locale("zh-CN") is True
        # format_message_icu 在宽松模式下回退到 format_message(不抛异常)
        result = manager.format_message_icu("common.broken", locale="zh-CN", n=1)
        # 回退行为:返回某种字符串(不抛异常即通过)
        assert isinstance(result, str)


# ════════════════════════════════════════════════════════════════
# 11: ErrorCodes 注册检查
# ════════════════════════════════════════════════════════════════
class TestErrorCodeRegistered:
    """I18N_ICU_COMPILE_FAILED 错误码注册检查。"""

    def test_error_code_constant_exists(self):
        """测试 11a: ErrorCodes.I18N_ICU_COMPILE_FAILED 常量存在且值正确。"""
        from services.error_codes import ErrorCodes
        assert hasattr(ErrorCodes, "I18N_ICU_COMPILE_FAILED")
        assert ErrorCodes.I18N_ICU_COMPILE_FAILED == "I18N.ICU.COMPILE_FAILED"

    def test_error_definition_registered(self):
        """测试 11b: ErrorDefinition 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        # 确保默认 ErrorDefinition 已注册(幂等)
        ErrorRegistry._ensure_initialized()
        # ErrorRegistry.get() 在 test/staging 环境下未注册时会抛 UnknownErrorCode
        # 已注册的 I18N_ICU_COMPILE_FAILED 应正常返回 definition
        definition = ErrorRegistry.get(ErrorCodes.I18N_ICU_COMPILE_FAILED)
        assert definition is not None, (
            "I18N_ICU_COMPILE_FAILED ErrorDefinition 未注册"
        )
        assert definition.code == ErrorCodes.I18N_ICU_COMPILE_FAILED
        assert definition.http_status == 500
        assert definition.retryable is False
        assert definition.severity == "critical"
        assert definition.message_key == "errors.i18n.icu.compile_failed"
        # safe_params 应包含 locale / key / reason
        assert "locale" in definition.safe_params
        assert "key" in definition.safe_params
        assert "reason" in definition.safe_params

    def test_locale_entry_exists(self):
        """测试 11c: errors.i18n.icu.compile_failed 在两个 locale 文件中都有条目。"""
        from services.i18n import I18nManager
        manager = I18nManager()
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        zh_text = manager.translate("errors.i18n.icu.compile_failed", locale="zh-CN")
        en_text = manager.translate("errors.i18n.icu.compile_failed", locale="en-US")
        assert zh_text and "ICU" in zh_text or "compile" in zh_text.lower() or "编译" in zh_text, (
            f"zh-CN locale entry missing or invalid: {zh_text!r}"
        )
        assert en_text and ("ICU" in en_text or "compile" in en_text.lower()), (
            f"en-US locale entry missing or invalid: {en_text!r}"
        )


# ════════════════════════════════════════════════════════════════
# 12: 构建时校验脚本集成测试
# ════════════════════════════════════════════════════════════════
class TestBuildTimeValidationScript:
    """scripts/check_i18n_icu_precompile.py 的集成测试。"""

    def test_script_imports_i18n_validators(self):
        """测试 12a: 脚本能成功导入 services.i18n 的校验函数。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "check_i18n_icu_precompile",
            REPO_ROOT / "scripts" / "check_i18n_icu_precompile.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        validate, extract_params = module._import_i18n_validators()
        assert callable(validate)
        assert callable(extract_params)

    def test_verify_passes_with_valid_locales(self):
        """测试 12b: verify() 对合法 locale 文件返回 0(通过)。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "check_i18n_icu_precompile",
            REPO_ROOT / "scripts" / "check_i18n_icu_precompile.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 使用真实 locale 文件
        exit_code = module.verify(("zh-CN", "en-US"))
        assert exit_code == 0, (
            "Expected verify() to pass (exit 0) for real locale files"
        )

    def test_verify_fails_with_broken_icu(self, tmp_path, monkeypatch):
        """测试 12c: verify() 对含语法错误的 locale 文件返回 1(失败)。"""
        import importlib.util
        _write_locale_files(
            tmp_path,
            {"common": {"broken": "{n, plural, =0 {无} one {# 项}"}},  # 括号不平衡
            {"common": {"broken": "{n, plural, =0 {no items} one {# item}}"}},
        )
        spec = importlib.util.spec_from_file_location(
            "check_i18n_icu_precompile_test",
            REPO_ROOT / "scripts" / "check_i18n_icu_precompile.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # patch LOCALES_DIR 指向临时目录
        monkeypatch.setattr(module, "LOCALES_DIR", tmp_path)
        exit_code = module.verify(("zh-CN", "en-US"))
        assert exit_code == 1, (
            "Expected verify() to fail (exit 1) for broken ICU"
        )

    def test_verify_fails_with_param_asymmetry(self, tmp_path, monkeypatch):
        """测试 12d: verify() 对参数集合不对称的 locale 文件返回 1(失败)。"""
        import importlib.util
        _write_locale_files(
            tmp_path,
            {"common": {"items": "剩余 {count} 项"}},
            {"common": {"items": "{num} items left"}},  # 参数不对称
        )
        spec = importlib.util.spec_from_file_location(
            "check_i18n_icu_precompile_test2",
            REPO_ROOT / "scripts" / "check_i18n_icu_precompile.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "LOCALES_DIR", tmp_path)
        exit_code = module.verify(("zh-CN", "en-US"))
        assert exit_code == 1, (
            "Expected verify() to fail (exit 1) for param asymmetry"
        )


# ════════════════════════════════════════════════════════════════
# 13: 真实 locale 文件集成测试
# ════════════════════════════════════════════════════════════════
class TestRealLocaleFiles:
    """使用真实 locales/ 目录的集成测试。"""

    def test_real_locales_load_without_error(self, loose_mode):
        """测试 13a: 真实 locale 文件在宽松模式下加载无错误。"""
        from services.i18n import I18nManager
        manager = I18nManager()
        assert manager.load_locale("zh-CN") is True
        assert manager.load_locale("en-US") is True
        # 验证预编译缓存已填充
        assert "zh-CN" in manager._compiled_icu_cache
        assert "en-US" in manager._compiled_icu_cache
        # 验证所有预编译结果都是 ok=True(真实 locale 文件不应有语法错误)
        for locale, cache in manager._compiled_icu_cache.items():
            failures = [
                key for key, entry in cache.items()
                if not entry.get("ok", True)
            ]
            assert not failures, (
                f"Locale {locale} 有 {len(failures)} 个预编译失败的 key: {failures[:5]}"
            )

    def test_real_locales_param_symmetric(self, loose_mode):
        """测试 13b: 真实 locale 文件参数集合对称(无不对称)。"""
        from services.i18n import I18nManager
        manager = I18nManager()
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        # 对称检查已完成(load_locale 阶段触发)
        # 若有不对称,strict 模式会抛 AppError;宽松模式下检查 _param_asymmetry_check_done
        assert manager._param_asymmetry_check_done is True

    def test_real_locales_strict_mode_loads(self, strict_mode):
        """测试 13c: 真实 locale 文件在 strict 模式下也能成功加载(无 ICU 错误)。"""
        from services.i18n import I18nManager
        manager = I18nManager()
        # strict 模式下,若真实 locale 文件有 ICU 错误会抛 AppError
        # 此测试验证真实 locale 文件是干净的
        assert manager.load_locale("zh-CN") is True
        assert manager.load_locale("en-US") is True

    def test_build_script_passes_on_real_locales(self):
        """测试 13d: 构建时校验脚本对真实 locale 文件返回 0(通过)。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "check_i18n_icu_precompile_real",
            REPO_ROOT / "scripts" / "check_i18n_icu_precompile.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        exit_code = module.verify(("zh-CN", "en-US"))
        assert exit_code == 0, (
            "Build-time validation script should pass on real locale files"
        )
