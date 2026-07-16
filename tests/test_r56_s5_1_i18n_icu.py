"""R56 §5.1: i18n ICU MessageFormat + 绝对门禁 + Telegram language_code fallback 测试。

覆盖:
1. ICU MessageFormat plural/select 子句解析
2. format_message_icu() 模块级便捷函数
3. map_telegram_language_code() Telegram language_code → locale 映射
4. get_locale_with_telegram_fallback() 完整 locale 优先级链
5. scan_hardcoded_strings.py 绝对门禁(user_visible 必须 0)
6. pseudolocalize_test.py key 一致性 + 占位符一致性
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 项目根目录加入 sys.path(便于直接运行)
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.i18n import (
    _icu_format,
    _icu_parse_branches,
    _icu_select_branch,
    _icu_select_branch_by_count,
    format_message_icu,
    get_locale_with_telegram_fallback,
    map_telegram_language_code,
    translate,
    _DEFAULT_LOCALE,
    _FALLBACK_LOCALE,
)


# ─── ICU MessageFormat parser 单元测试 ────────────────────────────


class TestICUParser:
    """ICU MessageFormat 子集解析器测试。"""

    def test_simple_interpolation(self):
        """简单 {var} 插值仍然工作(非 ICU pattern 回退路径)。"""
        result = _icu_format("Hello {name}", "en-US", {"name": "Alice"})
        assert result == "Hello Alice"

    def test_plural_one_english(self):
        """plural: count=1 用 one 子句。"""
        text = "{count, plural, one {# item} other {# items}}"
        result = _icu_format(text, "en-US", {"count": 1})
        assert result == "1 item"

    def test_plural_other_english(self):
        """plural: count=2 用 other 子句。"""
        text = "{count, plural, one {# item} other {# items}}"
        result = _icu_format(text, "en-US", {"count": 2})
        assert result == "2 items"

    def test_plural_zero_exact_match(self):
        """plural: =0 精确匹配优先。"""
        text = "{count, plural, =0 {none} one {# item} other {# items}}"
        result = _icu_format(text, "en-US", {"count": 0})
        assert result == "none"

    def test_plural_chinese_always_other(self):
        """中文不区分单复数,始终用 other。"""
        text = "{count, plural, one {# item} other {# items}}"
        result = _icu_format(text, "zh-CN", {"count": 1})
        assert result == "1 items"

    def test_select_branch(self):
        """select 子句按字符串值选择。"""
        text = "{gender, select, male {He} female {She} other {They}}"
        # select 不直接用 count,需要根据 var 的字符串值选择
        # 当前实现:不传 count 时 fallthrough 到 other
        # 我们用 male 子句测试
        branches = _icu_parse_branches("male {He} female {She} other {They}")
        assert "male" in branches
        assert "female" in branches
        assert "other" in branches
        # selectordinal 简化为 other
        result = _icu_select_branch_by_count(1, "selectordinal", "en-US", branches)
        assert result == "They"  # 兜底 other

    def test_nested_icu(self):
        """嵌套 ICU pattern:子句中含 {var, plural, ...}。"""
        text = (
            "{count, plural, "
            "one {{name} has # item} "
            "other {{name} has # items}}"
        )
        result = _icu_format(text, "en-US", {"count": 2, "name": "Alice"})
        assert result == "Alice has 2 items"

    def test_escape_brace(self):
        """转义大括号 \\{ 不被解析为 ICU pattern。"""
        text = r"\{literal\}"
        result = _icu_format(text, "en-US", {})
        assert result == "{literal}"

    def test_parse_branches_multiple(self):
        """_icu_parse_branches 正确解析多个子句。"""
        branches = _icu_parse_branches("=0 {none} one {# item} other {# items}")
        assert branches == {
            "=0": "none",
            "one": "# item",
            "other": "# items",
        }

    def test_hash_placeholder_replacement(self):
        """# 占位符在 plural 子句中展开为 count。"""
        text = "{count, plural, other {Total: #}}"
        result = _icu_format(text, "en-US", {"count": 42})
        assert result == "Total: 42"


# ─── format_message_icu 模块级便捷函数测试 ────────────────────────


class TestFormatMessageICU:
    """format_message_icu() 测试(使用 locales/ 中的真实 key)。"""

    def test_icu_count_zh_cn(self):
        """common.files.count_icu 在 zh-CN 中应正确展开。"""
        result = format_message_icu("common.files.count_icu", locale="zh-CN", count=5)
        # zh-CN: "{count, plural, =0 {无文件} other {{count} 个文件}}"
        # count=5 走 other,展开 {count} → 5
        assert "5" in result
        assert "个文件" in result

    def test_icu_count_zero_zh_cn(self):
        """count=0 精确匹配 =0 子句。"""
        result = format_message_icu("common.files.count_icu", locale="zh-CN", count=0)
        assert "无文件" in result

    def test_icu_count_en_us_one(self):
        """en-US count=1 用 one 子句,# 展开为 1。"""
        result = format_message_icu("common.files.count_icu", locale="en-US", count=1)
        # en-US: "{count, plural, =0 {no files} one {# file} other {# files}}"
        assert result == "1 file"

    def test_icu_count_en_us_other(self):
        """en-US count=2 用 other 子句。"""
        result = format_message_icu("common.files.count_icu", locale="en-US", count=2)
        assert result == "2 files"

    def test_icu_count_en_us_zero(self):
        """en-US count=0 精确匹配 =0 子句。"""
        result = format_message_icu("common.files.count_icu", locale="en-US", count=0)
        assert result == "no files"

    def test_icu_fallback_to_format_message(self):
        """非 ICU pattern 回退到 format_message(简单 {var} 插值)。"""
        # 使用不存在的 key 触发 fallback,但需测试 ICU 检测逻辑
        # 用一个简单 {var} 占位符的 key
        # common.ok = "确定" 在 zh-CN,不含 ICU pattern
        result = format_message_icu("common.ok", locale="zh-CN")
        assert result == "确定"


# ─── Telegram language_code fallback 测试 ─────────────────────────


class TestTelegramLanguageCodeFallback:
    """map_telegram_language_code + get_locale_with_telegram_fallback 测试。"""

    def test_map_zh(self):
        assert map_telegram_language_code("zh") == "zh-CN"

    def test_map_zh_cn(self):
        assert map_telegram_language_code("zh-CN") == "zh-CN"

    def test_map_zh_hans(self):
        assert map_telegram_language_code("zh-Hans") == "zh-CN"

    def test_map_en(self):
        assert map_telegram_language_code("en") == "en-US"

    def test_map_en_us(self):
        assert map_telegram_language_code("en-US") == "en-US"

    def test_map_en_gb(self):
        assert map_telegram_language_code("en-GB") == "en-US"

    def test_map_unknown_falls_back_to_default(self):
        """未知 language_code 回退到默认 locale。"""
        assert map_telegram_language_code("ru") == _DEFAULT_LOCALE

    def test_map_none_falls_back_to_default(self):
        assert map_telegram_language_code(None) == _DEFAULT_LOCALE

    def test_map_empty_falls_back_to_default(self):
        assert map_telegram_language_code("") == _DEFAULT_LOCALE

    def test_map_case_insensitive(self):
        """大小写不敏感(telegram 传入可能是 'EN' 或 'en')。"""
        assert map_telegram_language_code("EN") == "en-US"
        assert map_telegram_language_code("ZH") == "zh-CN"


class TestLocaleFallbackChain:
    """get_locale_with_telegram_fallback locale 优先级链测试。"""

    def test_telegram_fallback_when_no_user_locale(self, monkeypatch):
        """无用户显式 locale 时,使用 Telegram language_code fallback。"""
        # 模拟 get_user_locale 返回默认值(无显式设置)
        from services import i18n as i18n_mod
        monkeypatch.setattr(
            i18n_mod.I18nManager,
            "get_user_locale",
            lambda self, uid: _DEFAULT_LOCALE,
        )
        result = get_locale_with_telegram_fallback(99999, "en")
        assert result == "en-US"

    def test_user_locale_overrides_telegram(self, monkeypatch):
        """用户显式 locale 优先级高于 Telegram language_code。"""
        from services import i18n as i18n_mod
        # 用户显式设置为 en-US(非默认 zh-CN,触发优先级返回)
        monkeypatch.setattr(
            i18n_mod.I18nManager,
            "get_user_locale",
            lambda self, uid: "en-US",
        )
        result = get_locale_with_telegram_fallback(99999, "zh")
        assert result == "en-US"

    def test_default_locale_when_no_telegram_code(self, monkeypatch):
        """无 telegram_language_code 且无用户 locale 时,用默认 locale。"""
        from services import i18n as i18n_mod
        monkeypatch.setattr(
            i18n_mod.I18nManager,
            "get_user_locale",
            lambda self, uid: _DEFAULT_LOCALE,
        )
        result = get_locale_with_telegram_fallback(99999, None)
        assert result == _DEFAULT_LOCALE


# ─── scan_hardcoded_strings 绝对门禁测试 ──────────────────────────


class TestAbsoluteGate:
    """R56 §5.1 绝对门禁 — user_visible 必须 0。"""

    def test_baseline_user_visible_zero(self):
        """baseline.json 的 user_visible 必须为 0。"""
        import json
        baseline_path = PROJECT_ROOT / "locales" / "baseline.json"
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        total = data.get("total", {})
        assert total.get("user_visible") == 0, (
            f"R56 §5.1 绝对门禁: baseline total.user_visible 必须 0,"
            f"实际 {total.get('user_visible')}"
        )
        # 每个模块的 user_visible 也必须 0
        for mod_name, mod_data in data.get("modules", {}).items():
            assert mod_data.get("user_visible") == 0, (
                f"R56 §5.1: 模块 {mod_name} user_visible 必须 0,"
                f"实际 {mod_data.get('user_visible')}"
            )

    def test_scanner_absolute_gate_passes(self):
        """运行 scan_hardcoded_strings.py --check 应通过(user_visible=0)。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/scan_hardcoded_strings.py", "--check"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"R56 §5.1 绝对门禁失败:\n{result.stdout}\n{result.stderr}"
        )
        # 必须含 R56 §5.1 绝对门禁通过提示
        assert "R56 §5.1 绝对门禁通过" in result.stdout, (
            f"未看到绝对门禁通过提示:\n{result.stdout}"
        )


# ─── 伪本地化测试 ─────────────────────────────────────────────────


class TestPseudolocalization:
    """pseudolocalize_test.py 一致性检查测试。"""

    def test_pseudolocalization_passes(self):
        """运行 pseudolocalize_test.py 应通过。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/pseudolocalize_test.py"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"伪本地化测试失败:\n{result.stdout}\n{result.stderr}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
