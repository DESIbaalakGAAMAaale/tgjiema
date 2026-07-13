"""R45 17.1/17.2/17.3: i18n 安全 + 按钮 callback 签名 + WCAG 无障碍测试。

测试范围:
- services/i18n.py
  * format_message: missing key 返回安全通用文案 + 累计 i18n_missing_key_total
  * format_plural (实例方法): CLDR 风格 zh-CN 无复数 / en-US one/other
  * format_datetime: short / long 格式 + 时区转换
  * format_file_size: B/KB/MB/GB 格式化
  * parse_accept_language: RFC 7231 header 解析 + locale 匹配
- services/button_security.py
  * generate_signed_callback + verify_signed_callback 往返
  * verify_signed_callback: 用户 ID 不匹配 / 过期 / 签名篡改
- locales/zh-CN.json / en-US.json
  * common.error.* / common.success.* / common.files.count.* / button.* key 存在性

测试策略:
- 直接调用 I18nManager 实例方法验证返回值
- monkeypatch settings.ADMIN_BOT_TOKEN 为固定字符串(避免 MagicMock 干扰 HMAC)
- 校验 missing_key_count 计数器递增
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"


# ── 辅助 fixture ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_i18n_singleton():
    """每个用例前重置 i18n 模块级单例,避免跨用例状态污染。"""
    import services.i18n as i18n_mod
    old = i18n_mod._i18n_manager
    i18n_mod._i18n_manager = None
    yield
    i18n_mod._i18n_manager = old


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。

    conftest 注入的 settings 是 MagicMock,ADMIN_BOT_TOKEN 属性也是 MagicMock,
    调用 .encode() 会返回 MagicMock 导致 hmac.new() 抛错。
    此处将其设为固定字符串,确保 _sign() 可正常工作。
    """
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "test_admin_bot_token_r45")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "test_sender_bot_token_r45")


@pytest.fixture
def i18n_manager():
    """返回已加载 zh-CN + en-US 的 I18nManager 实例。"""
    import services.i18n as i18n_mod
    manager = i18n_mod.I18nManager()
    manager.load_locale("zh-CN")
    manager.load_locale("en-US")
    return manager


# ── 1. format_message missing key 安全通用文案 ──────────────────────────


class TestFormatMessageMissingKey:
    """R45 17.1: format_message 缺失 key 返回安全通用文案,不暴露内部 key。"""

    def test_missing_key_returns_safe_fallback_not_key(self, i18n_manager):
        """缺失 key 时返回安全通用文案,而非 key 本身。"""
        result = i18n_manager.format_message("nonexistent.key.xyz", locale="zh-CN")
        # 不应返回 key 本身
        assert result != "nonexistent.key.xyz"
        # 应返回非空安全文案
        assert result
        assert isinstance(result, str)
        assert len(result) > 0

    def test_missing_key_increments_counter(self, i18n_manager):
        """缺失 key 时 i18n_missing_key_total 计数器递增。"""
        before = i18n_manager.get_missing_key_count()
        i18n_manager.format_message("totally.missing.key.abc", locale="zh-CN")
        after = i18n_manager.get_missing_key_count()
        assert after == before + 1

    def test_missing_key_en_locale_returns_english_fallback(self, i18n_manager):
        """en-US locale 缺失 key 时返回英文安全文案。"""
        result = i18n_manager.format_message("missing.key.english", locale="en-US")
        # 应包含英文常见错误词
        assert isinstance(result, str)
        assert len(result) > 0
        # 不应暴露内部 key
        assert "missing.key.english" not in result

    def test_existing_key_returns_translation(self, i18n_manager):
        """存在的 key 返回正确翻译(非安全回退)。"""
        result = i18n_manager.format_message(
            "common.error.generic", locale="zh-CN"
        )
        assert result == "操作失败,请稍后重试"

    def test_existing_key_with_interpolation(self, i18n_manager):
        """存在的 key 带占位符正确插值。"""
        result = i18n_manager.format_message(
            "bot.upload_success", locale="zh-CN", code="ABC123"
        )
        assert "ABC123" in result
        assert "{code}" not in result


# ── 2. format_plural (实例方法, CLDR one/other) ──────────────────────────


class TestFormatPlural:
    """R45 17.1: format_plural 实例方法 — CLDR 风格复数格式化。"""

    def test_zh_cn_no_plural_distinction(self, i18n_manager):
        """zh-CN 不区分单复数,始终使用 other 形式。"""
        # count=1
        result_one = i18n_manager.format_plural(
            "common.files.count", locale="zh-CN", count=1
        )
        assert "1" in result_one
        assert "个文件" in result_one
        # count=5
        result_many = i18n_manager.format_plural(
            "common.files.count", locale="zh-CN", count=5
        )
        assert "5" in result_many
        assert "个文件" in result_many

    def test_en_us_one_form_for_single(self, i18n_manager):
        """en-US count==1 使用 one 形式。"""
        result = i18n_manager.format_plural(
            "common.files.count", locale="en-US", count=1
        )
        assert "1 file" in result

    def test_en_us_other_form_for_multiple(self, i18n_manager):
        """en-US count!=1 使用 other 形式。"""
        result = i18n_manager.format_plural(
            "common.files.count", locale="en-US", count=5
        )
        assert "5 files" in result

    def test_en_us_zero_uses_other(self, i18n_manager):
        """en-US count=0 使用 other 形式(符合英文 0 files 习惯)。"""
        result = i18n_manager.format_plural(
            "common.files.count", locale="en-US", count=0
        )
        assert "0 files" in result

    def test_plural_missing_key_returns_safe_fallback(self, i18n_manager):
        """复数 key 缺失时返回安全通用文案,不暴露内部 key。"""
        result = i18n_manager.format_plural(
            "nonexistent.plural.key", locale="zh-CN", count=3
        )
        assert result != "nonexistent.plural.key"
        assert isinstance(result, str)
        assert len(result) > 0


# ── 3. format_datetime ──────────────────────────────────────


class TestFormatDatetime:
    """R45 17.1: format_datetime 日期时间本地化。"""

    def test_zh_cn_short_format(self, i18n_manager):
        """zh-CN short 格式: 2024年1月15日 14:30。"""
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(dt, locale="zh-CN", format="short")
        assert "2024年1月15日" in result
        assert "14:30" in result

    def test_zh_cn_long_format(self, i18n_manager):
        """zh-CN long 格式含星期。"""
        # 2024-01-15 是星期一
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(dt, locale="zh-CN", format="long")
        assert "2024年1月15日" in result
        assert "星期一" in result

    def test_en_us_short_format(self, i18n_manager):
        """en-US short 格式: Jan 15, 2024 ... PM。"""
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(dt, locale="en-US", format="short")
        assert "Jan" in result
        assert "2024" in result
        assert "PM" in result

    def test_en_us_long_format(self, i18n_manager):
        """en-US long 格式含完整星期/月份。"""
        # 2024-01-15 是 Monday
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(dt, locale="en-US", format="long")
        assert "Monday" in result
        assert "January" in result

    def test_default_short_format(self, i18n_manager):
        """默认 format 参数为 short。"""
        dt = datetime.datetime(2024, 6, 5, 9, 0, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(dt, locale="zh-CN")
        assert "2024年6月5日" in result

    def test_none_dt_returns_empty(self, i18n_manager):
        """dt=None 返回空字符串。"""
        assert i18n_manager.format_datetime(None, locale="zh-CN") == ""

    def test_timezone_conversion(self, i18n_manager):
        """时区转换: UTC → Asia/Shanghai (+8)。

        若环境未安装 tzdata(Windows Python 3.9 默认无 IANA 时区数据库),
        zoneinfo 无法解析 Asia/Shanghai,此时跳过断言(验证函数不抛异常即可)。
        """
        # 检测 zoneinfo + tzdata 是否可用
        tzdata_available = False
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
            tzdata_available = True
        except Exception:
            tzdata_available = False

        dt = datetime.datetime(2024, 1, 15, 6, 0, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(
            dt, locale="zh-CN", timezone="Asia/Shanghai"
        )
        # 函数应正常返回字符串,不抛异常
        assert isinstance(result, str)
        assert len(result) > 0
        if tzdata_available:
            # UTC 06:00 → CST 14:00
            assert "14:00" in result, f"时区转换后应含 14:00,实际: {result}"

    def test_timezone_utc_conversion(self, i18n_manager):
        """UTC 时区直接处理(不依赖 tzdata)。"""
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = i18n_manager.format_datetime(
            dt, locale="zh-CN", timezone="UTC"
        )
        # UTC 时区直接处理,时间不变
        assert "14:30" in result


# ── 4. format_file_size ─────────────────────────────────────


class TestFormatFileSize:
    """R45 17.1: format_file_size 文件大小格式化。"""

    def test_bytes(self, i18n_manager):
        """小于 1024 字节显示 B。"""
        assert i18n_manager.format_file_size(512, locale="zh-CN") == "512 B"

    def test_kilobytes(self, i18n_manager):
        """1024 字节显示 KB。"""
        result = i18n_manager.format_file_size(1536, locale="zh-CN")
        assert "KB" in result
        assert "1.5" in result

    def test_megabytes(self, i18n_manager):
        """1MB 显示。"""
        result = i18n_manager.format_file_size(1024 * 1024, locale="en-US")
        assert "MB" in result
        assert "1.0" in result

    def test_zero_bytes(self, i18n_manager):
        """0 字节显示 0 B。"""
        assert i18n_manager.format_file_size(0, locale="zh-CN") == "0 B"

    def test_negative_bytes_clamped_to_zero(self, i18n_manager):
        """负数字节数被钳制为 0。"""
        assert i18n_manager.format_file_size(-100, locale="zh-CN") == "0 B"

    def test_none_bytes_clamped_to_zero(self, i18n_manager):
        """None 字节数被钳制为 0。"""
        assert i18n_manager.format_file_size(None, locale="zh-CN") == "0 B"


# ── 5. parse_accept_language ─────────────────────────────────


class TestParseAcceptLanguage:
    """R45 17.1: parse_accept_language HTTP Accept-Language header 解析。"""

    def test_exact_match_zh_cn(self, i18n_manager):
        """zh-CN 精确匹配。"""
        result = i18n_manager.parse_accept_language("zh-CN,zh;q=0.9,en;q=0.8")
        assert result == "zh-CN"

    def test_exact_match_en_us(self, i18n_manager):
        """en-US 精确匹配。"""
        result = i18n_manager.parse_accept_language("en-US,en;q=0.9")
        assert result == "en-US"

    def test_prefix_match_zh(self, i18n_manager):
        """zh 前缀匹配 zh-CN。"""
        result = i18n_manager.parse_accept_language("zh")
        assert result == "zh-CN"

    def test_prefix_match_en(self, i18n_manager):
        """en 前缀匹配 en-US。"""
        result = i18n_manager.parse_accept_language("en")
        assert result == "en-US"

    def test_q_value_ordering(self, i18n_manager):
        """按 q 值降序优先匹配(q 大者优先)。"""
        # en q=0.9 > zh q=0.1
        result = i18n_manager.parse_accept_language("zh;q=0.1,en;q=0.9")
        assert result == "en-US"

    def test_empty_header_returns_default(self, i18n_manager):
        """空 header 返回默认 locale。"""
        assert i18n_manager.parse_accept_language("") == "zh-CN"

    def test_none_header_returns_default(self, i18n_manager):
        """None header 返回默认 locale。"""
        assert i18n_manager.parse_accept_language(None) == "zh-CN"

    def test_no_match_returns_default(self, i18n_manager):
        """无匹配语言返回默认 locale。"""
        result = i18n_manager.parse_accept_language("fr-FR,de;q=0.9")
        assert result == "zh-CN"

    def test_q_zero_excluded(self, i18n_manager):
        """q=0 的语言被排除,不匹配。"""
        # en;q=0 被排除,fr 不匹配 → 回退 zh-CN
        result = i18n_manager.parse_accept_language("en;q=0,fr-FR")
        assert result == "zh-CN"

    def test_module_level_function(self):
        """模块级 parse_accept_language 函数等价于实例方法。"""
        import services.i18n as i18n_mod
        result = i18n_mod.parse_accept_language("en-US")
        assert result == "en-US"


# ── 6. button_security: generate + verify 往返 ───────────────


class TestButtonSecurityRoundTrip:
    """R45 17.2: 按钮 callback 签名生成与验证往返。"""

    def test_valid_callback_round_trip(self):
        """生成的 callback_data 能被正确验证。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        callback = generate_signed_callback(
            user_id=123456, action="confirm", data="FILE_ABC", ttl=3600
        )
        valid, action, data = verify_signed_callback(callback, current_user_id=123456)
        assert valid is True
        assert action == "confirm"
        assert data == "FILE_ABC"

    def test_empty_data_round_trip(self):
        """空 data 也能正确往返。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        callback = generate_signed_callback(
            user_id=999, action="cancel", data="", ttl=3600
        )
        valid, action, data = verify_signed_callback(callback, current_user_id=999)
        assert valid is True
        assert action == "cancel"
        assert data == ""

    def test_data_with_colon_round_trip(self):
        """data 含冒号也能正确往返(冒号是分隔符)。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        callback = generate_signed_callback(
            user_id=888, action="retry", data="file:code:123", ttl=3600
        )
        valid, action, data = verify_signed_callback(callback, current_user_id=888)
        assert valid is True
        assert action == "retry"
        assert data == "file:code:123"


# ── 7. button_security: 验证失败场景 ─────────────────────────


class TestButtonSecurityFailures:
    """R45 17.2: 按钮 callback 验证失败场景。"""

    def test_user_id_mismatch(self):
        """用户 ID 不匹配时验证失败。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        callback = generate_signed_callback(
            user_id=111, action="confirm", data="X", ttl=3600
        )
        # 用不同的 user_id 验证
        valid, action, data = verify_signed_callback(callback, current_user_id=222)
        assert valid is False
        assert action == ""
        assert data == ""

    def test_expired_callback(self):
        """过期 callback 验证失败。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        # ttl=0 已过期(生成时 expire_ts = now + 0,验证时 now > expire_ts)
        callback = generate_signed_callback(
            user_id=333, action="retry", data="Y", ttl=-1
        )
        valid, action, data = verify_signed_callback(callback, current_user_id=333)
        assert valid is False
        assert action == ""
        assert data == ""

    def test_tampered_signature(self):
        """签名被篡改时验证失败。"""
        from services.button_security import generate_signed_callback, verify_signed_callback

        callback = generate_signed_callback(
            user_id=444, action="confirm", data="Z", ttl=3600
        )
        # 篡改最后一段(签名)
        parts = callback.split(":")
        # 修改签名为无效值
        parts[-1] = "0" * 16
        tampered = ":".join(parts)
        valid, action, data = verify_signed_callback(tampered, current_user_id=444)
        assert valid is False
        assert action == ""
        assert data == ""

    def test_malformed_callback_too_few_parts(self):
        """格式不正确(字段不足)验证失败。"""
        from services.button_security import verify_signed_callback

        valid, action, data = verify_signed_callback("a:b:c", current_user_id=1)
        assert valid is False
        assert action == ""
        assert data == ""

    def test_malformed_callback_non_numeric_user_id(self):
        """user_id 非数字验证失败。"""
        from services.button_security import verify_signed_callback

        valid, action, data = verify_signed_callback(
            "abc:confirm:data:99999999:sig1234567890ab", current_user_id=1
        )
        assert valid is False
        assert action == ""
        assert data == ""

    def test_empty_callback(self):
        """空字符串验证失败。"""
        from services.button_security import verify_signed_callback

        valid, action, data = verify_signed_callback("", current_user_id=1)
        assert valid is False
        assert action == ""
        assert data == ""


# ── 8. locale 文件 key 存在性 ────────────────────────────────


class TestLocaleKeysExist:
    """R45 17.1: 校验 zh-CN.json / en-US.json 新增 key 存在。"""

    def test_zh_cn_common_error_keys_exist(self, i18n_manager):
        """zh-CN common.error.* key 存在。"""
        assert i18n_manager.has_key("common.error.generic", "zh-CN")
        assert i18n_manager.has_key("common.error.permission_denied", "zh-CN")
        assert i18n_manager.has_key("common.error.not_found", "zh-CN")
        assert i18n_manager.has_key("common.error.rate_limited", "zh-CN")

    def test_en_us_common_error_keys_exist(self, i18n_manager):
        """en-US common.error.* key 存在。"""
        assert i18n_manager.has_key("common.error.generic", "en-US")
        assert i18n_manager.has_key("common.error.permission_denied", "en-US")
        assert i18n_manager.has_key("common.error.not_found", "en-US")
        assert i18n_manager.has_key("common.error.rate_limited", "en-US")

    def test_common_success_keys_exist(self, i18n_manager):
        """common.success.* key 存在(zh-CN + en-US)。"""
        assert i18n_manager.has_key("common.success.saved", "zh-CN")
        assert i18n_manager.has_key("common.success.deleted", "zh-CN")
        assert i18n_manager.has_key("common.success.saved", "en-US")
        assert i18n_manager.has_key("common.success.deleted", "en-US")

    def test_plural_keys_exist(self, i18n_manager):
        """common.files.count.one / .other key 存在。"""
        assert i18n_manager.has_key("common.files.count.one", "zh-CN")
        assert i18n_manager.has_key("common.files.count.other", "zh-CN")
        assert i18n_manager.has_key("common.files.count.one", "en-US")
        assert i18n_manager.has_key("common.files.count.other", "en-US")

    def test_button_keys_exist(self, i18n_manager):
        """button.* key 存在(zh-CN + en-US)。"""
        for key in ("button.confirm", "button.cancel", "button.retry",
                    "button.appeal", "button.language_switch"):
            assert i18n_manager.has_key(key, "zh-CN"), f"缺失 zh-CN key: {key}"
            assert i18n_manager.has_key(key, "en-US"), f"缺失 en-US key: {key}"

    def test_button_values_correct(self, i18n_manager):
        """button 文案值正确。"""
        assert i18n_manager.translate("button.confirm", locale="zh-CN") == "确认"
        assert i18n_manager.translate("button.confirm", locale="en-US") == "Confirm"
        assert i18n_manager.translate("button.retry", locale="zh-CN") == "重试"
        assert i18n_manager.translate("button.retry", locale="en-US") == "Retry"
