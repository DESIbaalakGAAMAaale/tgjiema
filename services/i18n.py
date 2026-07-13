"""R40 P2-8 / R41 i18n 下一阶段: 国际化(i18n)管理器。

职责:
    为系统提供多语言支持,从 JSON locale 文件加载翻译:
    1. I18nManager — 单例管理器(支持运行时切换 locale)
    2. translate(key, locale=None, **kwargs) — 翻译查找 + 插值
    3. load_locale(locale) — 加载 locale JSON 文件
    4. get_available_locales() — 列出可用 locale
    5. set_default_locale(locale) — 设置默认 locale
    6. format_message(key, locale, **kwargs) — R41: 显式格式化接口(支持 {var} 占位符)
    7. format_error_code(domain, operation, reason) — R41: 三段式错误码格式化
    8. format_datetime(dt, locale, timezone) — R41: 日期时间本地化格式化
    9. format_file_size(bytes, locale) — R41: 文件大小格式化(B/KB/MB/GB)
    10. get_user_locale(user_id) — R41: 从 users_local 表读取用户语言偏好(默认 zh-CN)

设计原则:
    - locale 文件存放于 locales/ 目录(zh-CN.json / en-US.json)
    - 翻译 key 采用点分命名空间(errors.xxx / ui.xxx / bot.xxx)
    - 支持 {placeholder} 插值(如 "quota_remaining": "剩余 {count} 次")
    - 找不到 key 时回退到默认 locale,再回退到安全通用文案(R44 6.2)
    - 模块加载时缓存 locale 文件(避免重复磁盘读取)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import datetime
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
        # R44 6.2: missing key 计数器(供 Prometheus 采集)
        self._missing_key_count: int = 0

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
        4. 安全通用文案(按 key 前缀选择,R44 6.2)

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
            # R44 6.2: 找不到翻译时返回安全通用文案,不暴露内部 key
            logger.debug(f"[i18n] 翻译 key 未找到: {key}(locale={target_locale})")
            text = self._get_safe_fallback_message(key, target_locale)
            # R44 6.2: 累计 missing key 计数(供 Prometheus 采集)
            self._missing_key_count += 1
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

    def _get_safe_fallback_message(self, key: str, locale: str) -> str:
        """R44 6.2: key 缺失时返回安全通用文案,不暴露内部 key。

        回退策略:
            1. 按 key 前缀(errors./bot./ui./admin./common.)选择通用 fallback key
            2. 尝试从目标 locale 读取 fallback key 翻译
            3. 失败则尝试默认 locale 与 fallback locale
            4. 最终兜底为硬编码安全文案(中英文双语)

        Args:
            key: 原始翻译 key(用于判断前缀)
            locale: 目标 locale

        Returns:
            安全通用文案字符串
        """
        # 按 key 前缀选择通用 fallback key
        prefix_map = {
            "errors.": "errors.generic.fallback",
            "bot.": "bot.unknown_error",
            "ui.": "common.no_data",
            "admin.": "admin.generic.fallback",
            "common.": "common.no_data",
            "accessibility.": "common.no_data",
        }
        fallback_key = "errors.generic.fallback"
        for prefix, fk in prefix_map.items():
            if key.startswith(prefix):
                fallback_key = fk
                break
        # 尝试从目标/默认/fallback locale 读取 fallback key
        candidates = [locale, self.default_locale, _FALLBACK_LOCALE]
        seen: set[str] = set()
        for loc in candidates:
            if loc in seen:
                continue
            seen.add(loc)
            if loc not in self._translations:
                self.load_locale(loc)
            translations = self._translations.get(loc)
            if translations and fallback_key in translations:
                return translations[fallback_key]
        # 最终兜底:硬编码安全文案(中英文双语)
        if locale and locale.startswith("en"):
            return "An error occurred. Please try again later."
        return "操作失败,请稍后重试。"

    def get_missing_key_count(self) -> int:
        """R44 6.2: 返回 missing key 累计计数。

        供 Prometheus exporter 采集为 tgjiema_i18n_missing_key_total 指标。

        Returns:
            累计 missing key 次数
        """
        return self._missing_key_count

    def reset_missing_key_count(self) -> None:
        """R44 6.2: 重置 missing key 计数为 0。

        供运维巡检或单元测试在重置后重新统计使用。
        """
        self._missing_key_count = 0
        logger.debug("[i18n] missing key 计数器已重置")

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

    # ── R41 i18n 下一阶段: 格式化层(format_message / format_error_code /
    #                                  format_datetime / format_file_size) ──

    def format_message(self, key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
        """R41/R42 P1-8: 显式格式化接口 — 翻译 key 并用 {var} 占位符插值。

        与 translate() 的区别:
        - 显式语义: 调用方明确知道这是一个"格式化"操作,而非简单翻译查找
        - 支持任意 {var} 占位符(如 "上传成功,文件码: {code}")
        - kwargs 中的 None 值会被转为空字符串,避免 'None' 字面量泄漏到用户消息
        - 找不到 key 时回退到默认 locale 再回退到 key 本身(与 translate 一致)

        R42 P1-8 变更:
        - 缺失 key 时返回 key 本身(不抛异常,由 translate() 兜底保证)
        - 若 locale 不存在(文件未加载且加载失败),直接 fallback 到 en-US
          (避免回退到 default_locale 导致的语言错位)

        Args:
            key: 翻译 key(如 "bot.upload_success")
            locale: 目标 locale(默认 self.default_locale)
            **kwargs: 插值参数(如 code="ABC123" → 替换 {code})

        Returns:
            格式化后的字符串(占位符已替换);缺失 key 时返回 key 本身
        """
        # R42 P1-8: 若 locale 不存在(文件未加载且加载失败),直接 fallback 到 en-US
        target_locale = locale or self.default_locale
        if target_locale not in self._translations:
            if not self.load_locale(target_locale):
                target_locale = _FALLBACK_LOCALE  # en-US
        text = self.translate(key, locale=target_locale)
        if not text or not kwargs:
            return text
        # 将 None 值转为空字符串,避免 'None' 字面量泄漏
        safe_kwargs = {
            k: ("" if v is None else str(v))
            for k, v in kwargs.items()
        }
        if "{" in text and "}" in text:
            try:
                return text.format(**safe_kwargs)
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"[i18n] format_message 插值失败 key={key}: {e}")
                return text
        return text

    def format_error_code(self, domain: str, operation: str, reason: str) -> str:
        """R41: 三段式错误码格式化 — domain.operation.reason → "errors.{domain}.{operation}.{reason}"。

        用于生成统一的错误码键,便于 Bot 端通过 translate() 查找本地化错误消息。

        Args:
            domain: 错误域(如 "quota" / "file" / "user" / "system")
            operation: 触发操作(如 "decode" / "upload" / "ban")
            reason: 失败原因(如 "exceeded" / "not_found" / "expired")

        Returns:
            点分错误码键(如 "errors.quota.decode.exceeded"),
            可直接传给 translate() 查找本地化消息
        """
        # 规范化各段: 去除首尾空格 + 转为小写(避免大小写不一致导致 key 查找失败)
        d = (domain or "").strip().lower()
        o = (operation or "").strip().lower()
        r = (reason or "").strip().lower()
        # 拼接为 "errors.{domain}.{operation}.{reason}"
        return f"errors.{d}.{o}.{r}"

    def format_datetime(
        self,
        dt: datetime.datetime,
        locale: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> str:
        """R41: 日期时间本地化格式化。

        根据 locale 选择日期格式,根据 timezone 进行时区转换。
        - zh-CN: "2024年1月15日 14:30"
        - en-US: "Jan 15, 2024 02:30 PM"

        Args:
            dt: 待格式化的 datetime 对象(naive 视为 UTC)
            locale: 目标 locale(默认 self.default_locale)
            timezone: 目标时区名(如 "Asia/Shanghai" / "UTC"),
                      None 时使用 locale 对应的默认时区

        Returns:
            本地化日期时间字符串
        """
        if dt is None:
            return ""
        target_locale = locale or self.default_locale
        # 若 dt 为 naive datetime,视为 UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # 时区转换
        if timezone:
            try:
                tz = datetime.timezone(datetime.timedelta(0)) if timezone.upper() == "UTC" else None
                # 优先用 zoneinfo(Python 3.9+),失败时用固定偏移
                if tz is None:
                    try:
                        from zoneinfo import ZoneInfo
                        tz = ZoneInfo(timezone)
                    except Exception:
                        # 降级: 不做时区转换,保留原时区
                        logger.debug(f"[i18n] format_datetime 无法解析时区 {timezone},保留原时区")
                        tz = dt.tzinfo
                dt = dt.astimezone(tz)
            except Exception as e:
                logger.debug(f"[i18n] format_datetime 时区转换失败 {timezone}: {e}")
        # 按 locale 选择格式
        # 注意: strftime 的 %-m / %-d 仅 Linux 支持,Windows 不支持
        # 改为跨平台方案: 先 strftime 再字符串替换去除前导零
        if target_locale.startswith("zh"):
            # 中文格式: 2024年1月15日 14:30
            formatted = dt.strftime("%Y年%m月%d日 %H:%M")
            # 去除月/日的前导零(跨平台方案,兼容 Windows 与 Linux)
            formatted = formatted.replace("年0", "年").replace("月0", "月")
            return formatted
        elif target_locale.startswith("en"):
            # 英文格式: Jan 15, 2024 02:30 PM
            # 用 %d(带前导零)再去除,保证跨平台一致性
            formatted = dt.strftime("%b %d, %Y %I:%M %p")
            # 去除日的前导零(如 "Jan 05" → "Jan 5")
            parts = formatted.split(" ", 1)
            if len(parts) == 2 and parts[1].startswith("0"):
                parts[1] = parts[1][1:]
            return " ".join(parts)
        else:
            # 其他 locale: ISO 8601 紧凑格式(可读且无歧义)
            return dt.strftime("%Y-%m-%d %H:%M")

    def format_file_size(self, size_bytes: int, locale: Optional[str] = None) -> str:
        """R41: 文件大小格式化(B/KB/MB/GB)。

        按 locale 选择单位显示(zh-CN: "1.5 MB",en-US: "1.5 MB")。
        小于 1024 字节时显示 B,否则递进到 KB/MB/GB。

        Args:
            size_bytes: 文件字节数
            locale: 目标 locale(默认 self.default_locale)

        Returns:
            本地化文件大小字符串(如 "1.5 MB" / "1024 B")
        """
        if size_bytes is None or size_bytes < 0:
            size_bytes = 0
        target_locale = locale or self.default_locale
        # 单位列表(zh-CN 和 en-US 单位相同,仅小数点分隔符不同)
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        unit_idx = 0
        while size >= 1024.0 and unit_idx < len(units) - 1:
            size /= 1024.0
            unit_idx += 1
        # B 整数显示,KB/MB/GB 保留 1 位小数
        if unit_idx == 0:
            return f"{int(size)} {units[unit_idx]}"
        # 中文 locale 用小数点(与英文一致,避免全角句号)
        return f"{size:.1f} {units[unit_idx]}"

    def get_user_locale(self, user_id: int) -> str:
        """R41: 从 users_local 表读取用户 locale(默认 zh-CN)。

        从 SQLite cache_store 同步读取(不触发 CRDB RU 消耗)。
        用户未设置或读取失败时返回默认 locale 'zh-CN'。

        Args:
            user_id: Telegram 用户 ID

        Returns:
            用户 locale 字符串(如 "zh-CN" / "en-US")
        """
        if not user_id:
            return _DEFAULT_LOCALE
        try:
            # 同步读取 SQLite(避免在 Bot handler 中引入 async 复杂性)
            import sqlite3
            from database.cache_store import DB_PATH
            if not Path(DB_PATH).exists():
                return _DEFAULT_LOCALE
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
            cursor = conn.execute(
                "SELECT locale FROM users_local WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                locale_val = str(row[0]).strip()
                # 校验 locale 格式(避免脏数据)
                if locale_val and len(locale_val) <= 10:
                    return locale_val
        except Exception as e:
            logger.debug(f"[i18n] get_user_locale 读取失败 user={user_id}: {e}")
        return _DEFAULT_LOCALE


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


# ── R42 P1-8: i18n 完整接入 — 错误响应结构 / 用户 locale 写入 /
#                复数规则 / Admin principal locale ──────────────────


def format_error_response(
    code: str,
    message_key: str,
    params: Optional[dict] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """R42 P1-8: 格式化错误响应结构。

    返回统一的错误响应 dict,前端/Bot 拿到 message_key 后,
    根据用户 locale 调用 format_message() / translate() 进行本地化渲染。
    这样错误码与文案解耦,前端可自由切换语言而无需后端重新生成。

    Args:
        code: 稳定的错误码(如 "QUOTA_DECODE_EXCEEDED" / "FILE_NOT_FOUND")
        message_key: 翻译 key(如 "errors.quota.decode.exceeded"),
                     前端用此 key 查找 locale 文件中的本地化消息
        params: 插值参数(如 {"count": 5}),用于替换消息中的 {count} 占位符;
                None 时返回空 dict
        trace_id: 链路追踪 ID(用于跨服务日志关联);None 时返回空字符串

    Returns:
        {"code": code, "message_key": message_key, "params": params or {},
         "trace_id": trace_id or ""}
    """
    return {
        "code": code,
        "message_key": message_key,
        "params": params or {},
        "trace_id": trace_id or "",
    }


def get_user_locale_sync(user_id: int) -> str:
    """R42 P1-8: 同步从 users_local 表读取用户 locale(默认 zh-CN)。

    模块级便捷函数,等价于 I18nManager.get_user_locale()。
    从 SQLite cache_store 同步读取(不触发 CRDB RU 消耗)。
    用户未设置或读取失败时返回默认 locale 'zh-CN'。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        用户 locale 字符串(如 "zh-CN" / "en-US");失败返回 "zh-CN"
    """
    manager = get_i18n_manager()
    return manager.get_user_locale(user_id)


def set_user_locale(user_id: int, locale: str) -> bool:
    """R42 P1-8: 设置用户 locale 并写 dirty_outbox(同步)。

    流程:
        1. 验证 locale 在支持列表中(否则抛 ValueError)
        2. UPDATE users_local SET locale=? WHERE user_id=?
        3. 写 dirty_outbox 一条 upsert 记录(供 crdb_sync 同步)

    Args:
        user_id: Telegram 用户 ID
        locale: 目标 locale(必须在 get_available_locales() 返回的列表中)

    Returns:
        True=成功;False=失败(DB 不可用 / 用户不存在 / IO 异常)

    Raises:
        ValueError: locale 不在支持列表中
    """
    if not user_id:
        return False
    # 1. 验证 locale 在支持列表中
    manager = get_i18n_manager()
    available = manager.get_available_locales()
    if locale not in available:
        raise ValueError(
            f"不支持的 locale: {locale},当前支持列表: {available}"
        )
    # 2. 同步写入 SQLite + dirty_outbox
    try:
        import sqlite3
        # 动态导入 DB_PATH(便于测试 monkeypatch)
        from database import cache_store as _cs
        db_path = str(_cs.DB_PATH)
        if not Path(db_path).exists():
            return False
        # R44 6.2: timeout 2→15 秒,与主 CacheStore busy_timeout=15000 一致,避免 SQLITE_BUSY
        conn = sqlite3.connect(db_path, timeout=15)
        try:
            # R44 6.2: 设置 busy_timeout PRAGMA,与其他 SQLite 连接协调写锁争用
            conn.execute("PRAGMA busy_timeout=15000")
            # UPDATE users_local SET locale=? WHERE user_id=?
            conn.execute(
                "UPDATE users_local SET locale=? WHERE user_id=?",
                (locale, int(user_id)),
            )
            # 写 dirty_outbox(供 crdb_sync 同步到 CRDB)
            conn.execute(
                """INSERT INTO dirty_outbox
                   (table_name, pk, version, operation, payload,
                    created_at, processed, local_only)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "users_local",
                    str(int(user_id)),
                    0,
                    "upsert",
                    json.dumps(
                        {"user_id": int(user_id), "locale": locale},
                        ensure_ascii=False,
                    ),
                    datetime.datetime.now().isoformat(),
                    0,
                    0,
                ),
            )
            conn.commit()
            logger.info(
                f"[i18n] set_user_locale 成功 user={user_id} locale={locale}"
            )
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(
            f"[i18n] set_user_locale 失败 user={user_id} locale={locale}: {e}"
        )
        return False


def format_plural(
    count: int,
    singular_key: str,
    plural_key: str,
    locale: Optional[str] = None,
) -> str:
    """R42 P1-8: 根据语言规则返回单数/复数形式的本地化消息。

    语言规则:
        - 中文(zh-*):不区分单复数,始终使用 singular_key
        - 英文(en-*):count == 1 用 singular_key,count != 1 用 plural_key
          (count=0 用 plural,符合英文 "0 items" 习惯)
        - 其他语言:默认按英文规则

    Args:
        count: 数量(用于选择单复数形式 + 插值)
        singular_key: 单数形式的翻译 key(如 "bot.file_count_singular")
        plural_key: 复数形式的翻译 key(如 "bot.file_count_plural")
        locale: 目标 locale(默认 zh-CN)

    Returns:
        本地化消息(已替换 {count} 占位符);key 缺失时返回安全通用文案(R44 6.2)
    """
    manager = get_i18n_manager()
    target_locale = locale or _DEFAULT_LOCALE
    # 中文不区分单复数
    if target_locale.startswith("zh"):
        return manager.translate(singular_key, locale=target_locale, count=count)
    # 英文规则: count == 1 用 singular, 其他用 plural (包括 0)
    if count == 1:
        return manager.translate(singular_key, locale=target_locale, count=count)
    return manager.translate(plural_key, locale=target_locale, count=count)


def get_principal_locale(principal_id: int) -> str:
    """R42 P1-8: 获取 admin principal 的 locale(默认 zh-CN)。

    Admin 后台调用此函数获取当前登录管理员的 locale,
    用于本地化后台错误消息 / 通知文案。

    设计说明:
        - principal_id 是 admin 的整数 ID(基于 username 哈希生成),
          通常不在 users_local 表中(管理员非普通 Telegram 用户);
        - 先尝试从 users_local 读取(支持管理员同时也是普通用户的场景);
        - 读取失败或无记录时返回默认 locale 'zh-CN'。

    Args:
        principal_id: admin 的整数 ID(AdminPrincipal.id)

    Returns:
        principal 的 locale(默认 zh-CN)
    """
    if not principal_id:
        return _DEFAULT_LOCALE
    # 先尝试从 users_local 读取(principal 可能也是普通用户)
    locale = get_user_locale_sync(principal_id)
    if locale and locale != _DEFAULT_LOCALE:
        return locale
    # 默认 zh-CN(principal 通常无 users_local 记录)
    return _DEFAULT_LOCALE
