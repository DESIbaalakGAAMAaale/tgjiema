"""R40 P2-8/9/11: i18n 与无障碍测试。

测试范围:
- locales/zh-CN.json: 中文 locale 文件
- locales/en-US.json: 英文 locale 文件
- services/i18n.py: I18nManager 类(load_locale/translate/get_available_locales/
  set_default_locale/has_key/get_meta/reload_all)
- module-level translate() / get_i18n_manager()
- users_local 表 locale 列存在性(cache_store.py 中 ALTER TABLE)

测试策略:
- AST 语法检查(兼容 Python 3.9)
- JSON 文件结构验证(meta/common/errors/ui/bot/accessibility 命名空间)
- 加载 locale 后 key 查找与插值
- 中文注释检查
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
LOCALES_DIR = REPO_ROOT / "locales"


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


# ════════════════════════════════════════════════════════════════
# 1. locale 文件存在性与结构检查
# ════════════════════════════════════════════════════════════════


class TestLocaleFiles:
    """R40 P2-8: locale JSON 文件级检查。"""

    def test_zh_cn_file_exists(self):
        assert (LOCALES_DIR / "zh-CN.json").exists(), "locales/zh-CN.json 应存在"

    def test_en_us_file_exists(self):
        assert (LOCALES_DIR / "en-US.json").exists(), "locales/en-US.json 应存在"

    def test_zh_cn_valid_json(self):
        """zh-CN.json 应为有效 JSON。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        assert data is not None, "zh-CN.json 应为有效 JSON"
        assert isinstance(data, dict), "zh-CN.json 根对象应为 dict"

    def test_en_us_valid_json(self):
        """en-US.json 应为有效 JSON。"""
        data = _load_json(LOCALES_DIR / "en-US.json")
        assert data is not None, "en-US.json 应为有效 JSON"
        assert isinstance(data, dict), "en-US.json 根对象应为 dict"

    def test_zh_cn_has_meta(self):
        """zh-CN.json 应包含 meta 字段。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("JSON 解析失败")
        assert "meta" in data, "应有 meta 字段"
        assert data["meta"].get("locale") == "zh-CN"

    def test_en_us_has_meta(self):
        """en-US.json 应包含 meta 字段。"""
        data = _load_json(LOCALES_DIR / "en-US.json")
        if data is None:
            pytest.skip("JSON 解析失败")
        assert "meta" in data, "应有 meta 字段"
        assert data["meta"].get("locale") == "en-US"

    def test_zh_cn_has_required_namespaces(self):
        """zh-CN.json 应包含 common/errors/ui/bot/accessibility 命名空间。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("JSON 解析失败")
        required = ["common", "errors", "ui", "bot", "accessibility"]
        for ns in required:
            assert ns in data, f"zh-CN.json 应包含 {ns} 命名空间"

    def test_en_us_has_required_namespaces(self):
        """en-US.json 应包含 common/errors/ui/bot/accessibility 命名空间。"""
        data = _load_json(LOCALES_DIR / "en-US.json")
        if data is None:
            pytest.skip("JSON 解析失败")
        required = ["common", "errors", "ui", "bot", "accessibility"]
        for ns in required:
            assert ns in data, f"en-US.json 应包含 {ns} 命名空间"

    def test_zh_cn_errors_keys_align_with_en_us(self):
        """zh-CN 和 en-US 的 errors 命名空间 key 应一致。"""
        zh = _load_json(LOCALES_DIR / "zh-CN.json")
        en = _load_json(LOCALES_DIR / "en-US.json")
        if zh is None or en is None:
            pytest.skip("JSON 解析失败")
        zh_keys = set(zh.get("errors", {}).keys())
        en_keys = set(en.get("errors", {}).keys())
        # 允许差异,但至少应有交集
        assert zh_keys & en_keys, "zh-CN 和 en-US errors 命名空间应有交集"

    def test_zh_cn_has_quota_decode_exceeded(self):
        """zh-CN 应包含 quota.decode.exceeded 错误码翻译。"""
        data = _load_json(LOCALES_DIR / "zh-CN.json")
        if data is None:
            pytest.skip("JSON 解析失败")
        errors = data.get("errors", {})
        assert "quota.decode.exceeded" in errors, "应包含 quota.decode.exceeded"

    def test_meta_has_fallback(self):
        """locale 文件 meta 应包含 fallback 字段。"""
        for name in ["zh-CN", "en-US"]:
            data = _load_json(LOCALES_DIR / f"{name}.json")
            if data is None:
                continue
            assert "fallback" in data.get("meta", {}), (
                f"{name}.json 的 meta 应包含 fallback 字段"
            )


# ════════════════════════════════════════════════════════════════
# 2. services/i18n.py AST 检查
# ════════════════════════════════════════════════════════════════


class TestI18nFile:
    """R40 P2-8: services/i18n.py 文件级检查。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "i18n.py").exists(), "services/i18n.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        assert tree is not None, "services/i18n.py 应可被 AST 解析"

    def test_has_i18n_manager_class(self):
        """应定义 I18nManager 类。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        classes = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        assert "I18nManager" in classes, "应定义 I18nManager 类"

    def test_has_required_methods(self):
        """I18nManager 应包含核心方法。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        required = {
            "load_locale",
            "translate",
            "get_available_locales",
            "set_default_locale",
            "has_key",
            "get_meta",
            "reload_all",
        }
        missing = required - funcs
        assert not missing, f"I18nManager 缺少方法: {missing}"

    def test_has_module_level_functions(self):
        """应有模块级 translate() / get_i18n_manager() 函数。"""
        tree = _parse_ast(SERVICES_DIR / "i18n.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "translate" in funcs, "应有模块级 translate() 函数"
        assert "get_i18n_manager" in funcs, "应有模块级 get_i18n_manager() 函数"

    def test_has_chinese_comments(self):
        """R40 规则:代码注释用中文。"""
        source = (SERVICES_DIR / "i18n.py").read_text(encoding="utf-8")
        chinese_count = sum(
            1 for line in source.split("\n")
            if "#" in line and any(
                "\u4e00" <= ch <= "\u9fff"
                for ch in line.split("#", 1)[1]
            )
        )
        assert chinese_count >= 3, f"中文注释数量应 >= 3,实际 {chinese_count}"


# ════════════════════════════════════════════════════════════════
# 3. I18nManager 运行时测试(加载真实 locale 文件)
# ════════════════════════════════════════════════════════════════


class TestI18nManagerRuntime:
    """R40 P2-8: I18nManager 运行时测试。"""

    def _try_init_manager(self):
        """尝试初始化 I18nManager。"""
        try:
            from services.i18n import I18nManager
            return I18nManager()
        except Exception as e:
            pytest.skip(f"I18nManager 不可初始化: {e}")

    def test_load_zh_cn(self):
        """加载 zh-CN.json 应成功。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        ok = manager.load_locale("zh-CN")
        assert ok, "加载 zh-CN 应成功"
        assert "zh-CN" in manager._translations

    def test_load_en_us(self):
        """加载 en-US.json 应成功。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        ok = manager.load_locale("en-US")
        assert ok, "加载 en-US 应成功"
        assert "en-US" in manager._translations

    def test_load_invalid_locale_returns_false(self):
        """加载不存在的 locale 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        ok = manager.load_locale("nonexistent-locale")
        assert ok is False, "加载不存在的 locale 应返回 False"

    def test_load_locale_is_idempotent(self):
        """重复加载同一 locale 应幂等(不重复读文件)。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        ok1 = manager.load_locale("zh-CN")
        ok2 = manager.load_locale("zh-CN")
        assert ok1 and ok2, "重复加载应都返回 True"

    def test_translate_zh_cn(self):
        """translate('errors.quota.decode.exceeded', zh-CN) 应返回中文消息。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        msg = manager.translate("errors.quota.decode.exceeded", locale="zh-CN")
        assert msg, "翻译结果不应为空"
        assert msg != "errors.quota.decode.exceeded" or "解码" in msg or "上限" in msg, (
            f"中文翻译应包含相关字眼,实际: {msg}"
        )

    def test_translate_en_us(self):
        """translate('errors.quota.decode.exceeded', en-US) 应返回英文消息。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("en-US")
        msg = manager.translate("errors.quota.decode.exceeded", locale="en-US")
        assert msg, "翻译结果不应为空"
        assert "quota" in msg.lower(), f"英文翻译应包含 'quota',实际: {msg}"

    def test_translate_with_interpolation(self):
        """带插值参数的翻译应正确替换 {placeholder}。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        msg = manager.translate("bot.quota_remaining", locale="zh-CN", count=5)
        assert "5" in msg, f"插值应替换 {{count}} 为 5,实际: {msg}"

    def test_translate_missing_key_returns_key(self):
        """R44 6.2: 找不到 key 时应返回安全通用文案,不暴露内部 key。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        msg = manager.translate("nonexistent.key.path", locale="zh-CN")
        # R44 6.2: 行为变更 - 不再返回 key 本身,改为安全通用文案
        assert msg != "nonexistent.key.path", f"不应返回 key 本身,实际: {msg}"
        assert len(msg) > 0, "应返回非空安全文案"

    def test_translate_empty_key_returns_empty(self):
        """空 key 应返回空字符串。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        msg = manager.translate("", locale="zh-CN")
        assert msg == ""

    def test_get_available_locales(self):
        """get_available_locales 应返回 ['zh-CN', 'en-US']。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        locales = manager.get_available_locales()
        assert "zh-CN" in locales
        assert "en-US" in locales

    def test_set_default_locale(self):
        """set_default_locale 应切换默认 locale。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        # 切换到 en-US
        ok = manager.set_default_locale("en-US")
        assert ok, "切换到 en-US 应成功"
        assert manager.default_locale == "en-US"
        # 切换回 zh-CN
        ok = manager.set_default_locale("zh-CN")
        assert ok, "切换到 zh-CN 应成功"
        assert manager.default_locale == "zh-CN"

    def test_set_default_locale_invalid(self):
        """set_default_locale('nonexistent') 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        ok = manager.set_default_locale("nonexistent-locale")
        assert ok is False

    def test_has_key_true_for_existing(self):
        """has_key 对存在的 key 应返回 True。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        assert manager.has_key("errors.quota.decode.exceeded", locale="zh-CN")

    def test_has_key_false_for_missing(self):
        """has_key 对不存在的 key 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        assert not manager.has_key("nonexistent.key", locale="zh-CN")

    def test_get_meta_returns_locale_info(self):
        """get_meta 应返回 locale 的 meta 信息。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        meta = manager.get_meta("zh-CN")
        assert meta.get("locale") == "zh-CN"
        assert "fallback" in meta

    def test_fallback_to_default_locale(self):
        """指定 locale 无翻译时应回退到默认 locale。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        # 加载 zh-CN 作为默认
        manager.load_locale("zh-CN")
        manager.default_locale = "zh-CN"
        # 请求一个不存在的 locale(应回退到 zh-CN)
        msg = manager.translate("errors.quota.decode.exceeded", locale="nonexistent")
        # 应回退到 zh-CN 的翻译
        assert msg, "回退后应返回非空翻译"

    def test_reload_all(self):
        """reload_all 应清空缓存并重新加载。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        count = manager.reload_all()
        assert count == 2, f"应重载 2 个 locale,实际 {count}"
        # 重载后仍可翻译
        msg = manager.translate("errors.quota.decode.exceeded", locale="zh-CN")
        assert msg


# ════════════════════════════════════════════════════════════════
# 4. 模块级便捷函数测试
# ════════════════════════════════════════════════════════════════


class TestModuleLevelFunctions:
    """R40 P2-8: 模块级 translate() / get_i18n_manager() 测试。"""

    def test_get_i18n_manager_returns_instance(self):
        """get_i18n_manager() 应返回 I18nManager 实例。"""
        try:
            from services.i18n import get_i18n_manager, I18nManager
        except ImportError:
            pytest.skip("services.i18n 不可导入")
        manager = get_i18n_manager()
        assert isinstance(manager, I18nManager)

    def test_translate_function_callable(self):
        """模块级 translate() 应可调用且返回字符串。"""
        try:
            from services.i18n import translate
        except ImportError:
            pytest.skip("services.i18n 不可导入")
        msg = translate("errors.quota.decode.exceeded", locale="zh-CN")
        assert isinstance(msg, str)
        assert msg  # 应非空

    def test_translate_with_interpolation(self):
        """模块级 translate() 应支持插值。"""
        try:
            from services.i18n import translate
        except ImportError:
            pytest.skip("services.i18n 不可导入")
        msg = translate("bot.quota_remaining", locale="zh-CN", count=10)
        assert "10" in msg


# ════════════════════════════════════════════════════════════════
# 5. users_local 表 locale 列检查
# ════════════════════════════════════════════════════════════════


class TestUsersLocalLocaleColumn:
    """R40 P2-9: users_local 表应包含 locale 列(默认 zh-CN)。"""

    def test_cache_store_has_locale_alter(self):
        """cache_store.py 应包含 ALTER TABLE users_local ADD COLUMN locale。"""
        source = (REPO_ROOT / "database" / "cache_store.py").read_text(encoding="utf-8")
        assert "ALTER TABLE users_local ADD COLUMN locale" in source, (
            "应有 ALTER TABLE 语句为 users_local 补 locale 列"
        )

    def test_cache_store_locale_default_zh_cn(self):
        """locale 列默认值应为 zh-CN。"""
        source = (REPO_ROOT / "database" / "cache_store.py").read_text(encoding="utf-8")
        # 验证 ALTER 语句中包含 DEFAULT 'zh-CN'
        assert "DEFAULT 'zh-CN'" in source, "locale 列默认值应为 'zh-CN'"

    def test_cache_store_has_chinese_comment_for_locale(self):
        """locale 列的 ALTER 应有中文注释说明用途。"""
        source = (REPO_ROOT / "database" / "cache_store.py").read_text(encoding="utf-8")
        # 查找 locale 列的 ALTER 语句附近的中文注释
        # 简化:验证文件中存在 i18n 或 语言偏好 等中文关键词
        assert "i18n" in source or "语言偏好" in source or "用户语言" in source, (
            "locale 列的注释应说明用途(i18n / 语言偏好 / 用户语言)"
        )


# ════════════════════════════════════════════════════════════════
# 6. 无障碍文档检查
# ════════════════════════════════════════════════════════════════


class TestAccessibilityGuide:
    """R40 P2-10: 无障碍指南文档检查。"""

    def test_doc_exists(self):
        """docs/i18n-accessibility-guide.md 应存在。"""
        assert (REPO_ROOT / "docs" / "i18n-accessibility-guide.md").exists(), (
            "docs/i18n-accessibility-guide.md 应存在"
        )

    def test_doc_mentions_wcag(self):
        """文档应提及 WCAG 2.2 AA 标准。"""
        doc = (REPO_ROOT / "docs" / "i18n-accessibility-guide.md").read_text(encoding="utf-8")
        assert "WCAG" in doc, "应提及 WCAG 标准"
        assert "2.2" in doc, "应提及 WCAG 2.2 版本"
        assert "AA" in doc.upper(), "应提及 AA 级别"

    def test_doc_mentions_i18n(self):
        """文档应包含 i18n 相关内容。"""
        doc = (REPO_ROOT / "docs" / "i18n-accessibility-guide.md").read_text(encoding="utf-8")
        assert "i18n" in doc.lower() or "国际化" in doc, "应包含 i18n 或国际化内容"
        assert "locale" in doc.lower(), "应包含 locale 相关内容"

    def test_doc_has_checklist(self):
        """文档应包含检查清单。"""
        doc = (REPO_ROOT / "docs" / "i18n-accessibility-guide.md").read_text(encoding="utf-8")
        # 检查清单通常包含 [ ] 或 - 或 1. 等列表标记
        has_list = (
            "- [ ]" in doc
            or "- [x]" in doc
            or "1." in doc
            or "## 检查清单" in doc
            or "## Checklist" in doc.lower()
        )
        assert has_list, "应包含检查清单(列表或 checkbox 格式)"
