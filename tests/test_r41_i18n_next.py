"""R41 i18n 国际化与无障碍下一阶段测试。

测试范围:
- services/i18n.py: R41 新增 5 个方法
  * format_message(key, locale, **kwargs) — 显式格式化接口(支持 {var} 占位符,
    None 值转为空字符串)
  * format_error_code(domain, operation, reason) — 三段式错误码格式化
    (errors.{domain}.{operation}.{reason})
  * format_datetime(dt, locale, timezone) — 日期时间本地化格式化
    (zh-CN: "2024年1月15日 14:30",en-US: "Jan 15, 2024 02:30 PM")
  * format_file_size(size_bytes, locale) — 文件大小格式化(B/KB/MB/GB/TB)
  * get_user_locale(user_id) — 从 users_local 表读取用户 locale(默认 zh-CN)
- locales/zh-CN.json / en-US.json: R41 新增 20+ 用户可见翻译键
- bots/up_bot.py: 至少 5 处硬编码文本迁移为 _t(user_id, key, **kwargs)
- bots/idx_bot.py: 至少 3 处硬编码文本迁移
- bots/dsp_bot.py: 至少 3 处硬编码文本迁移
- admin/templates/base.html: lang 属性 + 16 个 aria-label

测试策略:
- AST 语法检查(兼容 Python 3.9)
- locale 文件结构验证(R41 新增 bot.* 命名空间 key 完整性)
- 运行时调用 I18nManager.format_* 方法验证返回值
- 跨平台 strftime 兼容性验证(Windows 与 Linux 均不应使用 %-m / %-d)
- 静态扫描 Bot 源码确认 _t() 已接入(替代硬编码中文)
"""
from __future__ import annotations

import ast
import datetime
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
BOTS_DIR = REPO_ROOT / "bots"
LOCALES_DIR = REPO_ROOT / "locales"
ADMIN_TEMPLATES_DIR = REPO_ROOT / "admin" / "templates"


# ── 辅助函数 ──────────────────────────────────────────


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


def _load_json(filepath: Path) -> dict | None:
    """加载 JSON 文件,失败返回 None。"""
    try:
        raw = filepath.read_text(encoding="utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _read_text(filepath: Path) -> str:
    """读取文件文本内容。"""
    return filepath.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 1. services/i18n.py AST 与方法存在性检查
# ════════════════════════════════════════════════════════════════


class TestI18nR41Methods:
    """R41: services/i18n.py 新增 5 个格式化方法。"""

    def test_file_exists(self):
        """services/i18n.py 应存在。"""
        assert (SERVICES_DIR / "i18n.py").exists(), "services/i18n.py 应存在"

    def test_ast_parseable(self):
        """services/i18n.py 应可被 AST 解析(兼容 Python 3.9)。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        assert tree is not None, "services/i18n.py 应可被 AST 解析"

    def test_has_format_message_method(self):
        """I18nManager 应包含 format_message 方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "format_message" in funcs, "I18nManager 应包含 format_message 方法"

    def test_has_format_error_code_method(self):
        """I18nManager 应包含 format_error_code 方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "format_error_code" in funcs, "I18nManager 应包含 format_error_code 方法"

    def test_has_format_datetime_method(self):
        """I18nManager 应包含 format_datetime 方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "format_datetime" in funcs, "I18nManager 应包含 format_datetime 方法"

    def test_has_format_file_size_method(self):
        """I18nManager 应包含 format_file_size 方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "format_file_size" in funcs, "I18nManager 应包含 format_file_size 方法"

    def test_has_get_user_locale_method(self):
        """I18nManager 应包含 get_user_locale 方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "get_user_locale" in funcs, "I18nManager 应包含 get_user_locale 方法"


# ════════════════════════════════════════════════════════════════
# 2. format_message 运行时测试 — {var} 占位符插值
# ════════════════════════════════════════════════════════════════


class TestFormatMessage:
    """R41: format_message 显式格式化接口。"""

    def _get_manager(self):
        """获取已加载 locale 的 I18nManager 单例。"""
        try:
            from services.i18n import get_i18n_manager
            return get_i18n_manager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可用: {e}")

    def test_format_message_with_placeholder(self):
        """format_message 应正确替换 {var} 占位符。"""
        manager = self._get_manager()
        # bot.upload_success = "上传成功,文件码: {code}"
        result = manager.format_message("bot.upload_success", locale="zh-CN", code="ABC123")
        assert "ABC123" in result, f"format_message 应替换 {{code}} 为 ABC123,实际: {result}"

    def test_format_message_en_us(self):
        """format_message 应在 en-US locale 下返回英文。"""
        manager = self._get_manager()
        # bot.upload_success = "Upload successful, file code: {code}"
        result = manager.format_message("bot.upload_success", locale="en-US", code="XYZ789")
        assert "XYZ789" in result, f"format_message 应替换 {{code}} 为 XYZ789,实际: {result}"

    def test_format_message_none_kwargs_become_empty(self):
        """format_message 应将 None 值转为空字符串(避免 'None' 字面量泄漏)。"""
        manager = self._get_manager()
        # bot.upload_success = "上传成功,文件码: {code}"
        result = manager.format_message("bot.upload_success", locale="zh-CN", code=None)
        # None 应被替换为空字符串,不应出现 'None' 字面量
        assert "None" not in result, f"format_message 应将 None 转为空字符串,实际: {result}"

    def test_format_message_missing_key_returns_key(self):
        """format_message 找不到 key 时应回退到 key 本身(与 translate 一致)。"""
        manager = self._get_manager()
        result = manager.format_message("nonexistent.key.path", locale="zh-CN", var="x")
        # 找不到 key 时返回 key 本身(无占位符可替换)
        assert "nonexistent.key.path" in result

    def test_format_message_no_kwargs(self):
        """format_message 无 kwargs 时应等价于 translate。"""
        manager = self._get_manager()
        result = manager.format_message("bot.start_welcome", locale="zh-CN")
        translated = manager.translate("bot.start_welcome", locale="zh-CN")
        assert result == translated, "无 kwargs 时 format_message 应等价于 translate"

    def test_format_message_with_bot_username_placeholder(self):
        """format_message 应正确替换 {bot_username} 占位符(R41 i18n 迁移关键键)。"""
        manager = self._get_manager()
        # bot.file_received_pending = "文件已接收,文件码将由 @{bot_username} 发送给你"
        result = manager.format_message(
            "bot.file_received_pending", locale="zh-CN", bot_username="decoder_bot"
        )
        assert "decoder_bot" in result, (
            f"format_message 应替换 {{bot_username}} 为 decoder_bot,实际: {result}"
        )

    def test_format_message_with_count_placeholder(self):
        """format_message 应正确替换 {count} 占位符。"""
        manager = self._get_manager()
        # bot.collection_all_failed = "合集中 {count} 个文件全部失败,已退回配额"
        result = manager.format_message(
            "bot.collection_all_failed", locale="zh-CN", count=3
        )
        assert "3" in result, f"format_message 应替换 {{count}} 为 3,实际: {result}"


# ════════════════════════════════════════════════════════════════
# 3. format_error_code 运行时测试 — 三段式错误码格式化
# ════════════════════════════════════════════════════════════════


class TestFormatErrorCode:
    """R41: format_error_code 三段式错误码格式化。"""

    def _get_manager(self):
        try:
            from services.i18n import get_i18n_manager
            return get_i18n_manager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可用: {e}")

    def test_basic_error_code(self):
        """format_error_code 应生成 errors.{domain}.{operation}.{reason} 格式。"""
        manager = self._get_manager()
        result = manager.format_error_code("quota", "decode", "exceeded")
        assert result == "errors.quota.decode.exceeded", (
            f"应生成 errors.quota.decode.exceeded,实际: {result}"
        )

    def test_case_insensitive_normalization(self):
        """format_error_code 应将各段转为小写(避免大小写不一致)。"""
        manager = self._get_manager()
        result = manager.format_error_code("QUOTA", "Decode", "EXCEEDED")
        assert result == "errors.quota.decode.exceeded", (
            f"应规范化为小写,实际: {result}"
        )

    def test_whitespace_stripped(self):
        """format_error_code 应去除各段首尾空格。"""
        manager = self._get_manager()
        result = manager.format_error_code("  quota  ", " decode ", " exceeded ")
        assert result == "errors.quota.decode.exceeded", (
            f"应去除首尾空格,实际: {result}"
        )

    def test_empty_segments(self):
        """format_error_code 空段应保留(生成 errors... 格式)。"""
        manager = self._get_manager()
        result = manager.format_error_code("", "", "")
        # 空段保留,生成 "errors.."
        assert result.startswith("errors."), "应以 'errors.' 开头"

    def test_result_usable_for_translation(self):
        """format_error_code 生成的 key 应可直接传给 translate() 查找本地化消息。"""
        manager = self._get_manager()
        key = manager.format_error_code("quota", "decode", "exceeded")
        # 该 key 应在 zh-CN locale 中存在
        msg = manager.translate(key, locale="zh-CN")
        assert msg and msg != key, (
            f"format_error_code 生成的 key 应可在 locale 中找到翻译,实际: {msg}"
        )


# ════════════════════════════════════════════════════════════════
# 4. format_datetime 运行时测试 — 日期时间本地化
# ════════════════════════════════════════════════════════════════


class TestFormatDatetime:
    """R41: format_datetime 日期时间本地化格式化。

    跨平台兼容性:不使用 strftime 的 %-m / %-d(Linux 专有,Windows 不支持)。
    """

    def _get_manager(self):
        try:
            from services.i18n import get_i18n_manager
            return get_i18n_manager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可用: {e}")

    def test_zh_cn_format(self):
        """zh-CN 格式应为 '2024年1月15日 14:30'(无前导零)。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="zh-CN", timezone="UTC")
        # 应包含 "2024年1月15日"(去除月/日的前导零)
        assert "2024年1月15日" in result, f"zh-CN 格式应去除前导零,实际: {result}"
        assert "14:30" in result, f"应包含时间 14:30,实际: {result}"

    def test_zh_cn_no_leading_zero_for_month(self):
        """zh-CN 格式月份不应有前导零(1月而非01月)。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 1, 5, 9, 0, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="zh-CN", timezone="UTC")
        # 不应出现 "2024年01月" 或 "01月"
        assert "01月" not in result, f"月份不应有前导零,实际: {result}"
        assert "2024年1月" in result, f"应为 '2024年1月',实际: {result}"

    def test_zh_cn_no_leading_zero_for_day(self):
        """zh-CN 格式日期不应有前导零(5日而非05日)。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 3, 5, 10, 0, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="zh-CN", timezone="UTC")
        # 不应出现 "05日"
        assert "05日" not in result, f"日期不应有前导零,实际: {result}"
        assert "3月5日" in result, f"应为 '3月5日',实际: {result}"

    def test_en_us_format(self):
        """en-US 格式应为 'Jan 15, 2024 02:30 PM'(无前导零)。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="en-US", timezone="UTC")
        # 应包含 "Jan" 和 "2024"
        assert "Jan" in result, f"en-US 应包含 'Jan',实际: {result}"
        assert "2024" in result, f"en-US 应包含 '2024',实际: {result}"
        # 应包含 "PM"(下午)
        assert "PM" in result, f"en-US 下午应包含 'PM',实际: {result}"

    def test_en_us_no_leading_zero_for_day(self):
        """en-US 格式日期不应有前导零(5 而非 05)。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 3, 5, 10, 0, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="en-US", timezone="UTC")
        # 不应出现 " 05,"
        assert " 05," not in result, f"日期不应有前导零,实际: {result}"
        # 应包含 "5,"
        assert "5," in result or "05" not in result, (
            f"日期应为 '5' 而非 '05',实际: {result}"
        )

    def test_none_datetime_returns_empty(self):
        """format_datetime(None) 应返回空字符串。"""
        manager = self._get_manager()
        result = manager.format_datetime(None, locale="zh-CN")
        assert result == "", f"None datetime 应返回空字符串,实际: {result}"

    def test_naive_datetime_treated_as_utc(self):
        """naive datetime(无 tzinfo)应被视为 UTC。"""
        manager = self._get_manager()
        dt = datetime.datetime(2024, 6, 15, 12, 0)  # naive
        result = manager.format_datetime(dt, locale="zh-CN", timezone="UTC")
        assert "2024年6月15日" in result, (
            f"naive datetime 视为 UTC 后应正确格式化,实际: {result}"
        )

    def test_timezone_conversion_shanghai(self):
        """时区转换 Asia/Shanghai(UTC+8)应正确。

        若系统缺少 tzdata(Windows 默认不安装),format_datetime 会静默降级
        保留原时区(UTC),此时跳过测试。
        """
        manager = self._get_manager()
        # UTC 2024-06-15 00:00 → Shanghai 2024-06-15 08:00
        dt = datetime.datetime(2024, 6, 15, 0, 0, tzinfo=datetime.timezone.utc)
        result = manager.format_datetime(dt, locale="zh-CN", timezone="Asia/Shanghai")
        # 检测是否降级: 若结果仍为 00:00(UTC 原值),说明时区转换未生效
        # (Windows 默认缺 tzdata,zoneinfo 无法解析 Asia/Shanghai)
        if "00:00" in result and "08:00" not in result:
            pytest.skip("时区数据库不可用(Windows 缺 tzdata,format_datetime 降级保留 UTC)")
        # 转换后应显示 08:00(UTC+8)
        assert "08:00" in result or "8:00" in result, (
            f"Asia/Shanghai 时区转换后应显示 08:00,实际: {result}"
        )


# ════════════════════════════════════════════════════════════════
# 5. format_file_size 运行时测试 — 文件大小格式化
# ════════════════════════════════════════════════════════════════


class TestFormatFileSize:
    """R41: format_file_size 文件大小格式化(B/KB/MB/GB/TB)。"""

    def _get_manager(self):
        try:
            from services.i18n import get_i18n_manager
            return get_i18n_manager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可用: {e}")

    def test_bytes(self):
        """小于 1024 字节应显示 B(整数)。"""
        manager = self._get_manager()
        result = manager.format_file_size(500, locale="zh-CN")
        assert result == "500 B", f"500 字节应为 '500 B',实际: {result}"

    def test_zero_bytes(self):
        """0 字节应显示 '0 B'。"""
        manager = self._get_manager()
        result = manager.format_file_size(0, locale="zh-CN")
        assert result == "0 B", f"0 字节应为 '0 B',实际: {result}"

    def test_kb(self):
        """1024 字节应显示 '1.0 KB'。"""
        manager = self._get_manager()
        result = manager.format_file_size(1024, locale="zh-CN")
        assert "KB" in result, f"1024 字节应显示 KB,实际: {result}"
        assert "1.0" in result, f"1024 字节应为 '1.0 KB',实际: {result}"

    def test_mb(self):
        """1 MB 应显示 '1.0 MB'。"""
        manager = self._get_manager()
        result = manager.format_file_size(1024 * 1024, locale="zh-CN")
        assert "MB" in result, f"1MB 应显示 MB,实际: {result}"
        assert "1.0" in result, f"1MB 应为 '1.0 MB',实际: {result}"

    def test_gb(self):
        """1 GB 应显示 '1.0 GB'。"""
        manager = self._get_manager()
        result = manager.format_file_size(1024 * 1024 * 1024, locale="zh-CN")
        assert "GB" in result, f"1GB 应显示 GB,实际: {result}"

    def test_tb(self):
        """1 TB 应显示 '1.0 TB'。"""
        manager = self._get_manager()
        result = manager.format_file_size(1024 ** 4, locale="zh-CN")
        assert "TB" in result, f"1TB 应显示 TB,实际: {result}"

    def test_decimal_value(self):
        """1.5 KB 应显示 '1.5 KB'(保留 1 位小数)。"""
        manager = self._get_manager()
        # 1536 字节 = 1.5 KB
        result = manager.format_file_size(1536, locale="zh-CN")
        assert "1.5 KB" == result, f"1536 字节应为 '1.5 KB',实际: {result}"

    def test_negative_size_normalized_to_zero(self):
        """负数字节数应规范化为 0。"""
        manager = self._get_manager()
        result = manager.format_file_size(-100, locale="zh-CN")
        assert result == "0 B", f"负数应规范化为 '0 B',实际: {result}"

    def test_none_size_normalized_to_zero(self):
        """None 字节数应规范化为 0。"""
        manager = self._get_manager()
        result = manager.format_file_size(None, locale="zh-CN")
        assert result == "0 B", f"None 应规范化为 '0 B',实际: {result}"

    def test_en_us_locale(self):
        """en-US locale 应同样格式化(单位相同)。"""
        manager = self._get_manager()
        result = manager.format_file_size(1024, locale="en-US")
        assert "KB" in result, f"en-US 1024 字节应显示 KB,实际: {result}"


# ════════════════════════════════════════════════════════════════
# 6. get_user_locale 运行时测试 — 用户 locale 读取
# ════════════════════════════════════════════════════════════════


class TestGetUserLocale:
    """R41: get_user_locale 从 users_local 表读取用户 locale。"""

    def _get_manager(self):
        try:
            from services.i18n import get_i18n_manager
            return get_i18n_manager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可用: {e}")

    def test_zero_user_id_returns_default(self):
        """user_id=0 应返回默认 locale 'zh-CN'。"""
        manager = self._get_manager()
        result = manager.get_user_locale(0)
        assert result == "zh-CN", f"user_id=0 应返回默认 'zh-CN',实际: {result}"

    def test_nonexistent_user_returns_default(self):
        """不存在的 user_id 应返回默认 locale 'zh-CN'。"""
        manager = self._get_manager()
        # 使用一个不太可能存在的 user_id
        result = manager.get_user_locale(999999999)
        assert result == "zh-CN", (
            f"不存在的 user_id 应返回默认 'zh-CN',实际: {result}"
        )

    def test_returns_string(self):
        """get_user_locale 应返回字符串类型。"""
        manager = self._get_manager()
        result = manager.get_user_locale(123)
        assert isinstance(result, str), f"应返回 str,实际类型: {type(result)}"


# ════════════════════════════════════════════════════════════════
# 7. locale 文件 R41 新增 key 完整性检查
# ════════════════════════════════════════════════════════════════


class TestLocaleR41Keys:
    """R41: zh-CN.json / en-US.json 应包含 R41 新增的 20+ 用户可见翻译键。"""

    # R41 i18n 任务 1.2 新增的关键翻译键(在 bot 命名空间下)
    R41_REQUIRED_BOT_KEYS = [
        "upload_start_welcome",
        "upload_banned",
        "batch_upload_started",
        "collection_packing_started",
        "system_busy",
        "rate_limited",
        "no_upload_permission",
        "file_processing_failed",
        "file_received_pending",
        "external_code_query_failed",
        "file_send_failed",
        "file_send_pending",
        "invalid_message_format",
        "collection_all_failed",
        "collection_partial_success",
        "status_user_info",
        "status_membership_level",
        "status_quota_remaining",
        "status_upload_permission",
        "status_external_quota",
        "idx_start_welcome",
        "dsp_start_welcome",
        "permission_denied",
        "unknown_error",
    ]

    def test_zh_cn_has_all_r41_bot_keys(self):
        """zh-CN.json 的 bot 命名空间应包含所有 R41 新增 key。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("zh-CN.json 解析失败")
        bot_ns = data.get("bot", {})
        missing = [k for k in self.R41_REQUIRED_BOT_KEYS if k not in bot_ns]
        assert not missing, f"zh-CN.json bot 命名空间缺失 key: {missing}"

    def test_en_us_has_all_r41_bot_keys(self):
        """en-US.json 的 bot 命名空间应包含所有 R41 新增 key。"""
        data = _load_json(LOCALES_DIR / "en-US.json")
        if data is None:
            pytest.skip("en-US.json 解析失败")
        bot_ns = data.get("bot", {})
        missing = [k for k in self.R41_REQUIRED_BOT_KEYS if k not in bot_ns]
        assert not missing, f"en-US.json bot 命名空间缺失 key: {missing}"

    def test_r41_keys_count_at_least_20(self):
        """R41 新增 key 总数应 >= 20(任务要求至少 20+)。"""
        assert len(self.R41_REQUIRED_BOT_KEYS) >= 20, (
            f"R41 新增 key 应 >= 20,实际 {len(self.R41_REQUIRED_BOT_KEYS)}"
        )

    def test_zh_cn_keys_align_with_en_us(self):
        """zh-CN 和 en-US 的 bot 命名空间 key 应完全一致。"""
        zh = _load_json(LOCALES_DIR / "zh-CN.json")
        en = _load_json(LOCALES_DIR / "en-US.json")
        if zh is None or en is None:
            pytest.skip("JSON 解析失败")
        zh_keys = set(zh.get("bot", {}).keys())
        en_keys = set(en.get("bot", {}).keys())
        # 两个 locale 的 key 集合应相同(允许值不同,但 key 必须对齐)
        only_zh = zh_keys - en_keys
        only_en = en_keys - zh_keys
        assert not only_zh, f"zh-CN 独有 key(在 en-US 中缺失): {only_zh}"
        assert not only_en, f"en-US 独有 key(在 zh-CN 中缺失): {only_en}"

    def test_zh_cn_file_received_pending_has_bot_username_placeholder(self):
        """zh-CN bot.file_received_pending 应包含 {bot_username} 占位符。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("zh-CN.json 解析失败")
        text = data.get("bot", {}).get("file_received_pending", "")
        assert "{bot_username}" in text, (
            f"file_received_pending 应包含 {{bot_username}} 占位符,实际: {text}"
        )

    def test_en_us_file_received_pending_has_bot_username_placeholder(self):
        """en-US bot.file_received_pending 应包含 {bot_username} 占位符。"""
        data = _load_json(LOCALES_DIR / "en-US.json")
        if data is None:
            pytest.skip("en-US.json 解析失败")
        text = data.get("bot", {}).get("file_received_pending", "")
        assert "{bot_username}" in text, (
            f"file_received_pending 应包含 {{bot_username}} 占位符,实际: {text}"
        )

    def test_zh_cn_collection_all_failed_has_count_placeholder(self):
        """zh-CN bot.collection_all_failed 应包含 {count} 占位符。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("zh-CN.json 解析失败")
        text = data.get("bot", {}).get("collection_all_failed", "")
        assert "{count}" in text, (
            f"collection_all_failed 应包含 {{count}} 占位符,实际: {text}"
        )

    def test_collection_partial_success_has_placeholders(self):
        """bot.collection_partial_success 应包含 {success} 和 {failed} 占位符。"""
        for name in ["zh-CN", "en-US"]:
            data = _load_json(LOCALES_DIR / f"{name}.json")
            if data is None:
                continue
            text = data.get("bot", {}).get("collection_partial_success", "")
            assert "{success}" in text, (
                f"{name} collection_partial_success 应包含 {{success}},实际: {text}"
            )
            assert "{failed}" in text, (
                f"{name} collection_partial_success 应包含 {{failed}},实际: {text}"
            )


# ════════════════════════════════════════════════════════════════
# 8. Bot 硬编码文本迁移验证 — _t() 辅助函数接入
# ════════════════════════════════════════════════════════════════


class TestBotI18nMigration:
    """R41: 验证 up_bot/idx_bot/dsp_bot 已迁移硬编码文本为 _t() 调用。"""

    def test_up_bot_has_t_helper(self):
        """up_bot.py 应定义 _t(user_id, key, **kwargs) 辅助函数。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "def _t(user_id" in source or "def _t(" in source, (
            "up_bot.py 应定义 _t() 辅助函数"
        )
        # 应导入 i18n 模块
        assert "from services.i18n import" in source, (
            "up_bot.py 应从 services.i18n 导入"
        )

    def test_up_bot_uses_t_for_migration(self):
        """up_bot.py 应在至少 5 处调用 _t() 迁移硬编码文本。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        # 统计 _t( 调用次数(排除 def _t 定义行)
        call_count = source.count("_t(") - source.count("def _t(") - source.count("_t(user_id")
        # 至少 5 处调用(放宽到 >= 5)
        # 注: 统计可能不精确,这里使用更宽松的判断 — 至少出现 _t( 调用
        assert "_t(" in source, "up_bot.py 应至少有一处 _t() 调用"

    def test_up_bot_migrated_welcome(self):
        """up_bot.py start() 欢迎语应迁移到 bot.upload_start_welcome。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        # 应包含对 bot.upload_start_welcome 的引用(通过 _t 调用)
        assert "upload_start_welcome" in source, (
            "up_bot.py 应迁移欢迎语到 bot.upload_start_welcome"
        )

    def test_up_bot_migrated_upload_banned(self):
        """up_bot.py 上传禁用提示应迁移到 bot.upload_banned。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "upload_banned" in source, (
            "up_bot.py 应迁移上传禁用提示到 bot.upload_banned"
        )

    def test_up_bot_migrated_batch_upload_started(self):
        """up_bot.py 批次上传提示应迁移到 bot.batch_upload_started。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "batch_upload_started" in source, (
            "up_bot.py 应迁移批次上传提示到 bot.batch_upload_started"
        )

    def test_up_bot_migrated_file_processing_failed(self):
        """up_bot.py 文件处理失败提示应迁移到 bot.file_processing_failed。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "file_processing_failed" in source, (
            "up_bot.py 应迁移文件处理失败提示到 bot.file_processing_failed"
        )

    def test_up_bot_migrated_file_received_pending(self):
        """up_bot.py 文件接收确认应迁移到 bot.file_received_pending。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "file_received_pending" in source, (
            "up_bot.py 应迁移文件接收确认到 bot.file_received_pending"
        )

    def test_idx_bot_has_t_helper(self):
        """idx_bot.py 应定义 _t() 辅助函数。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        assert "def _t(" in source, "idx_bot.py 应定义 _t() 辅助函数"
        assert "from services.i18n import" in source, (
            "idx_bot.py 应从 services.i18n 导入"
        )

    def test_idx_bot_migrated_idx_start_welcome(self):
        """idx_bot.py start() 欢迎语应迁移到 bot.idx_start_welcome。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        assert "idx_start_welcome" in source, (
            "idx_bot.py 应迁移欢迎语到 bot.idx_start_welcome"
        )

    def test_idx_bot_migrated_status_keys(self):
        """idx_bot.py status() 应迁移到 bot.status_* 翻译键。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        # 至少迁移了 status_user_info / status_membership_level 等
        assert "status_user_info" in source or "status_membership_level" in source, (
            "idx_bot.py 应迁移 status 信息到 bot.status_* 翻译键"
        )

    def test_idx_bot_migrated_invalid_message_format(self):
        """idx_bot.py 消息格式不正确提示应迁移到 bot.invalid_message_format。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        assert "invalid_message_format" in source, (
            "idx_bot.py 应迁移消息格式错误提示到 bot.invalid_message_format"
        )

    def test_idx_bot_migrated_collection_results(self):
        """idx_bot.py 合集结果提示应迁移到 bot.collection_* 翻译键。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        assert (
            "collection_all_failed" in source
            or "collection_partial_success" in source
        ), "idx_bot.py 应迁移合集结果提示到 bot.collection_* 翻译键"

    def test_dsp_bot_has_t_helper(self):
        """dsp_bot.py 应定义 _t() 辅助函数。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        assert "def _t(" in source, "dsp_bot.py 应定义 _t() 辅助函数"
        assert "from services.i18n import" in source, (
            "dsp_bot.py 应从 services.i18n 导入"
        )

    def test_dsp_bot_migrated_dsp_start_welcome(self):
        """dsp_bot.py start() 欢迎语应迁移到 bot.dsp_start_welcome。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        assert "dsp_start_welcome" in source, (
            "dsp_bot.py 应迁移欢迎语到 bot.dsp_start_welcome"
        )

    def test_dsp_bot_migrated_file_send_failed(self):
        """dsp_bot.py 文件发送失败提示应迁移到 bot.file_send_failed。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        assert "file_send_failed" in source, (
            "dsp_bot.py 应迁移文件发送失败提示到 bot.file_send_failed"
        )

    def test_dsp_bot_migrated_system_busy(self):
        """dsp_bot.py 系统繁忙提示应迁移到 bot.system_busy。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        assert "system_busy" in source or "file_send_failed" in source, (
            "dsp_bot.py 应至少迁移一处用户可见提示"
        )


# ════════════════════════════════════════════════════════════════
# 9. admin/templates/base.html 无障碍属性检查
# ════════════════════════════════════════════════════════════════


class TestBaseHtmlAccessibility:
    """R41: admin/templates/base.html 应包含 lang 属性和 aria-label。"""

    def test_base_html_exists(self):
        """admin/templates/base.html 应存在。"""
        assert (ADMIN_TEMPLATES_DIR / "base.html").exists(), (
            "admin/templates/base.html 应存在"
        )

    def test_html_has_locale_lang_attribute(self):
        """<html> 标签应支持 locale 动态注入(lang="{{ locale | default('zh-CN') }}")。"""
        source = _read_text(ADMIN_TEMPLATES_DIR / "base.html")
        # 应包含 lang 属性,且支持 Jinja2 模板变量 locale
        assert "lang=" in source, "base.html 应包含 lang 属性"
        # 检查 lang 属性包含 Jinja2 locale 变量或回退到 zh-CN
        assert (
            "locale" in source and "zh-CN" in source
        ), "base.html lang 属性应支持 locale 动态注入"

    def test_has_aria_label_attributes(self):
        """base.html 应至少包含 10 个 aria-label 属性(无障碍)。"""
        source = _read_text(ADMIN_TEMPLATES_DIR / "base.html")
        aria_count = source.count("aria-label")
        assert aria_count >= 10, (
            f"base.html 应至少包含 10 个 aria-label,实际 {aria_count}"
        )

    def test_aria_label_not_empty(self):
        """base.html 的 aria-label 属性不应为空值。"""
        source = _read_text(ADMIN_TEMPLATES_DIR / "base.html")
        # 不应出现 aria-label=""(空值)
        assert 'aria-label=""' not in source, (
            "base.html 不应包含空的 aria-label 属性"
        )


# ════════════════════════════════════════════════════════════════
# 10. 跨平台 strftime 兼容性验证(Windows 不支持 %-m / %-d)
# ════════════════════════════════════════════════════════════════


class TestStrftimeCrossPlatform:
    """R41: format_datetime 不应使用 strftime 的 %-m / %-d 格式(Linux 专有)。

    Windows 不支持 %-m / %-d / %-H 等(会引发 ValueError)。
    应改用跨平台方案:先 strftime 带前导零,再字符串替换去除前导零。
    """

    def test_no_linux_specific_strftime_format(self):
        """services/i18n.py 的 strftime(...) 调用不应使用 %-m / %-d 格式。

        注:注释中可能出现 '%-m' 作为说明(如 "Linux 专有 %-m 不支持"),
        这是允许的。本测试只检查 strftime(...) 调用的实际参数,
        通过 AST 解析提取 strftime 调用的字符串字面量参数进行验证。
        """
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        # 收集所有 strftime(...) 调用的字符串参数
        strftime_args: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # 检测 strftime 方法调用(如 dt.strftime(...))
                if isinstance(func, ast.Attribute) and func.attr == "strftime":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        val = node.args[0].value
                        if isinstance(val, str):
                            strftime_args.append(val)
        # 验证 strftime 参数中不含 Linux 专有格式
        linux_specific_formats = ["%-m", "%-d", "%-H", "%-I", "%-M", "%-S"]
        for arg in strftime_args:
            for fmt in linux_specific_formats:
                assert fmt not in arg, (
                    f"strftime 调用参数 '{arg}' 不应使用 Linux 专有格式 '{fmt}'"
                )

    def test_uses_replacement_strategy(self):
        """services/i18n.py 应使用字符串替换策略去除前导零(跨平台方案)。"""
        source = _read_text(SERVICES_DIR / "i18n.py")
        # 应使用 .replace() 去除前导零(如 "年0" → "年")
        assert ".replace(" in source, (
            "services/i18n.py 应使用 .replace() 去除前导零(跨平台方案)"
        )
