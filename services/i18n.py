"""R40 P2-8: 国际化(i18n)管理器。

职责:
    为系统提供多语言支持,从 JSON locale 文件加载翻译:
    1. I18nManager — 单例管理器(支持运行时切换 locale)
    2. translate(key, locale=None, **kwargs) — 翻译查找 + 插值
    3. load_locale(locale) — 加载 locale JSON 文件
    4. get_available_locales() — 列出可用 locale
    5. set_default_locale(locale) — 设置默认 locale

设计原则:
    - locale 文件存放于 locales/ 目录(zh-CN.json / en-US.json)
    - 翻译 key 采用点分命名空间(errors.xxx / ui.xxx / bot.xxx)
    - 支持 {placeholder} 插值(如 "quota_remaining": "剩余 {count} 次")
    - 找不到 key 时回退到默认 locale,再回退到 key 本身
    - 模块加载时缓存 locale 文件(避免重复磁盘读取)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# locale 文件目录(项目根目录下 locales/)
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
# 默认 locale
_DEFAULT_LOCALE = "zh-CN"
# 备用 locale(找不到 key 时回退)
_FALLBACK_LOCALE = "en-US"


class I18nManager:
    """R40 P2-8: 国际化管理器。

    用法:
        manager = I18nManager()
        # 加载 locale 文件
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        # 翻译
        msg = manager.translate("errors.quota.decode.exceeded", locale="zh-CN")
        # 带插值
        msg = manager.translate("bot.quota_remaining", locale="zh-CN", count=5)
    """

    def __init__(self, locales_dir: Path = _LOCALES_DIR, default_locale: str = _DEFAULT_LOCALE):
        """初始化 i18n 管理器。

        Args:
            locales_dir: locale 文件目录
            default_locale: 默认 locale(如 zh-CN)
        """
        self.locales_dir = Path(locales_dir)
        self.default_locale = default_locale
        # locale → 翻译字典缓存(扁平化 key → value)
        self._translations: dict[str, dict[str, str]] = {}
        # locale → meta 信息
        self._meta: dict[str, dict] = {}
        # 已加载的 locale 文件路径
        self._loaded_files: set[Path] = set()

    def load_locale(self, locale: str) -> bool:
        """加载指定 locale 的 JSON 文件。

        文件路径: <locales_dir>/<locale>.json
        若已加载过则跳过(幂等)。

        Args:
            locale: locale 标识符(如 zh-CN)

        Returns:
            True=成功;False=失败(文件不存在或解析错误)
        """
        if not locale:
            return False
        if locale in self._translations:
            return True  # 已加载
        filepath = self.locales_dir / f"{locale}.json"
        if not filepath.exists():
            logger.warning(f"[i18n] locale 文件不存在: {filepath}")
            return False
        try:
            raw = filepath.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning(f"[i18n] locale 文件 {locale} 根对象应为 dict")
                return False
            # 提取 meta 并单独存储
            meta = data.pop("meta", {})
            self._meta[locale] = meta if isinstance(meta, dict) else {}
            # 扁平化嵌套 dict 为点分 key
            flat = {}
            self._flatten_dict(data, prefix="", output=flat)
            self._translations[locale] = flat
            self._loaded_files.add(filepath)
            logger.info(
                f"[i18n] 加载 locale={locale} keys={len(flat)} "
                f"fallback={self._meta[locale].get('fallback', '无')}"
            )
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[i18n] locale 文件 {locale} 解析失败: {e}")
            return False
        except Exception as e:
            logger.warning(f"[i18n] locale 文件 {locale} 加载异常: {e}")
            return False

    def _flatten_dict(self, d: dict, prefix: str, output: dict[str, str]) -> None:
        """递归扁平化嵌套 dict 为点分 key。

        {"errors": {"quota": {"decode": "x"}}}
        → {"errors.quota.decode": "x"}

        Args:
            d: 待扁平化的 dict
            prefix: 当前 key 前缀(如 "errors")
            output: 输出 dict(扁平化后的 key → value)
        """
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                self._flatten_dict(value, full_key, output)
            elif isinstance(value, str):
                output[full_key] = value
            else:
                # 非 str 值转为 str(如 int/bool)
                output[full_key] = str(value)

    def translate(
        self,
        key: str,
        locale: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """翻译查找 + 插值。

        查找顺序:
        1. 指定 locale 的翻译
        2. 默认 locale 的翻译
        3. fallback locale 的翻译
        4. key 本身(最后的兜底)

        Args:
            key: 翻译 key(如 "errors.quota.decode.exceeded")
            locale: 目标 locale(默认 self.default_locale)
            **kwargs: 插值参数(如 count=5 → 替换 {count})

        Returns:
            翻译后的字符串
        """
        if not key:
            return ""
        target_locale = locale or self.default_locale
        # 确保目标 locale 已加载
        if target_locale not in self._translations:
            self.load_locale(target_locale)
        # 查找顺序:目标 locale → 默认 locale → fallback locale
        candidates = [target_locale, self.default_locale, _FALLBACK_LOCALE]
        # 去重(避免重复查找相同 locale)
        seen = set()
        text = None
        for loc in candidates:
            if loc in seen:
                continue
            seen.add(loc)
            translations = self._translations.get(loc)
            if translations is None:
                # 尝试加载
                if self.load_locale(loc):
                    translations = self._translations.get(loc)
            if translations and key in translations:
                text = translations[key]
                break
        if text is None:
            # 找不到翻译,返回 key 本身
            logger.debug(f"[i18n] 翻译 key 未找到: {key}(locale={target_locale})")
            text = key
        # 插值(支持 {placeholder} 格式)
        if kwargs and "{" in text and "}" in text:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"[i18n] 插值失败 key={key}: {e}")
        return text

    def get_available_locales(self) -> list[str]:
        """列出可用的 locale 标识符。

        扫描 locales_dir 下的 *.json 文件,返回文件名(不含扩展名)。

        Returns:
            locale 标识符列表(如 ["zh-CN", "en-US"])
        """
        if not self.locales_dir.exists():
            return []
        locales = []
        for p in sorted(self.locales_dir.glob("*.json")):
            locales.append(p.stem)
        return locales

    def set_default_locale(self, locale: str) -> bool:
        """设置默认 locale。

        Args:
            locale: 目标 locale(如 zh-CN)

        Returns:
            True=成功;False=locale 不可用
        """
        if not locale:
            return False
        # 确保已加载
        if locale not in self._translations:
            if not self.load_locale(locale):
                return False
        self.default_locale = locale
        logger.info(f"[i18n] 默认 locale 已设置为: {locale}")
        return True

    def get_meta(self, locale: str) -> dict:
        """获取 locale 的 meta 信息。

        Args:
            locale: 目标 locale

        Returns:
            meta dict(含 name/version/fallback 等);不存在返回空 dict
        """
        if locale not in self._meta:
            self.load_locale(locale)
        return self._meta.get(locale, {})

    def has_key(self, key: str, locale: Optional[str] = None) -> bool:
        """检查 key 是否存在于指定 locale 中。

        Args:
            key: 翻译 key
            locale: 目标 locale(默认 self.default_locale)

        Returns:
            True=存在;False=不存在
        """
        target_locale = locale or self.default_locale
        if target_locale not in self._translations:
            self.load_locale(target_locale)
        translations = self._translations.get(target_locale, {})
        return key in translations

    def reload_all(self) -> int:
        """重新加载所有已加载的 locale 文件(用于开发期热更新)。

        Returns:
            成功重载的 locale 数量
        """
        loaded_locales = list(self._translations.keys())
        self._translations.clear()
        self._meta.clear()
        self._loaded_files.clear()
        count = 0
        for loc in loaded_locales:
            if self.load_locale(loc):
                count += 1
        logger.info(f"[i18n] 重新加载 {count}/{len(loaded_locales)} locale 文件")
        return count


# 模块级单例
_i18n_manager: Optional[I18nManager] = None


def get_i18n_manager() -> I18nManager:
    """获取 I18nManager 单例。"""
    global _i18n_manager
    if _i18n_manager is None:
        _i18n_manager = I18nManager()
        # 启动时预加载默认 locale 和 fallback locale
        _i18n_manager.load_locale(_DEFAULT_LOCALE)
        _i18n_manager.load_locale(_FALLBACK_LOCALE)
    return _i18n_manager


def translate(key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
    """模块级便捷函数:翻译查找。

    Args:
        key: 翻译 key(如 "errors.quota.decode.exceeded")
        locale: 目标 locale(默认 zh-CN)
        **kwargs: 插值参数

    Returns:
        翻译后的字符串
    """
    manager = get_i18n_manager()
    return manager.translate(key, locale=locale, **kwargs)
