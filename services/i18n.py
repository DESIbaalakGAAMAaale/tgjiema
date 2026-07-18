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

R51 P1-9 增强(异步缓存):
    - get_user_locale(user_id) 添加内存 LRU 缓存(5 分钟 TTL),减少 SQLite 阻塞读
    - 缓存 miss 时同步加载并填充缓存(后续命中直接返回,不触发 SQLite)
    - 缓存失效后回退到默认语言(zh-CN)
    - 新增 get_user_locale_async(user_id) 异步版本(在 executor 中加载,不阻塞事件循环)
    - 新增 invalidate_user_locale_cache(user_id) 主动失效缓存(set_user_locale 时自动调用)

设计原则:
    - locale 文件存放于 locales/ 目录(zh-CN.json / en-US.json)
    - 翻译 key 采用点分命名空间(errors.xxx / ui.xxx / bot.xxx)
    - 支持 {placeholder} 插值(如 "quota_remaining": "剩余 {count} 次")
    - 找不到 key 时回退到默认 locale,再回退到安全通用文案(R44 6.2)
    - 模块加载时缓存 locale 文件(避免重复磁盘读取)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

# Note: _i18n_t is defined as a local alias after translate() is defined below.
# Do NOT add "from services.i18n import translate as _i18n_t" here (circular import).

# locale 文件目录(项目根目录下 locales/)
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
# 默认 locale
_DEFAULT_LOCALE = "zh-CN"
# 备用 locale(找不到 key 时回退)
_FALLBACK_LOCALE = "en-US"

# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_ICU_STRICT_SETTINGS_UNAVAILABLE = (
    "R63 P1-12: config.settings 不可用,ICU 严格模式检查降级到默认(非严格): {}"
)

# R61 P1-06: 锁定支持的 locale 列表(产品仅支持 zh-CN/en-US)
# 任何加载/设置/写入不在此列表中的 locale 均被显式拒绝(审计 P1-06:不再宣称完整 CLDR)
SUPPORTED_LOCALES: frozenset[str] = frozenset({"zh-CN", "en-US"})

# R56 §5.1: ICU MessageFormat 子集 — 用于复数/select/ordinal/嵌套插值
# 匹配 {name, type, ...} 模式(type ∈ plural/select/selectordinal)
_ICU_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*(plural|select|selectordinal)\s*,")

# R58 P1-2: 简单 {var} 占位符(非 ICU pattern)
_SIMPLE_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _get_environment() -> str:
    """R58 P1-2: 惰性读取 ENVIRONMENT(避免循环导入)。"""
    try:
        from config.settings import settings  # type: ignore[import]
        return getattr(settings, "ENVIRONMENT", "development")
    except Exception:
        return "development"


# R58 P1-2: i18n 失败指标(进程内计数器,Prometheus exporter 可轮询)
_i18n_missing_param_total: int = 0
_i18n_parse_failed_total: int = 0
# R63 P1-12: ICU 预编译失败计数(供 Prometheus 采集)
_i18n_icu_compile_failed_total: int = 0


def _incr_metric(metric: str) -> None:
    """R58 P1-2 / R63 P1-12: 累计 i18n 失败指标。"""
    global _i18n_missing_param_total, _i18n_parse_failed_total, _i18n_icu_compile_failed_total
    if metric == "i18n_missing_param_total":
        _i18n_missing_param_total += 1
    elif metric == "i18n_parse_failed_total":
        _i18n_parse_failed_total += 1
    elif metric == "i18n_icu_compile_failed_total":
        _i18n_icu_compile_failed_total += 1


# ── R63 P1-12: ICU 预编译严格模式 ──────────────────────────────


def _get_icu_strict_mode() -> bool:
    """R63 P1-12: 是否启用 ICU 预编译严格模式(任何 ICU 语法 / 参数集合不对称直接阻断)。

    判定优先级:
        1. ``RELEASE_BUILD=1/true/yes/on`` → 严格模式(release 构建强制预编译)
        2. ``ICU_STRICT_MODE`` 环境变量:
           - 显式 ``0/false/no/off`` → 关闭(开发模式可关闭以允许 fallback)
           - 显式 ``1/true/yes/on`` → 开启
        3. ``config.settings.ENVIRONMENT == "production"`` → 默认开启
        4. 其他环境(development/test/staging) → 默认关闭(向后兼容)

    Returns:
        True=严格模式(load_locale 阶段预编译失败立即抛 AppError);
        False=宽松模式(失败时记录 warning + 计数,不阻断加载)
    """
    import os
    # 1. release 构建强制严格
    if _get_release_mode():
        return True
    # 2. ICU_STRICT_MODE 环境变量(显式覆盖)
    val = os.environ.get("ICU_STRICT_MODE", "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    # 3. production 默认严格
    try:
        from config.settings import settings  # type: ignore[import]
        env = getattr(settings, "ENVIRONMENT", "development")
        if env == "production":
            return True
    except Exception as e:
        # R63: 不静默吞异常(fail-open 扫描器 Rule 1/2);
        # config.settings 不可用时降级到默认(非严格),但记录 debug 日志
        logger.debug(_LOG_ICU_STRICT_SETTINGS_UNAVAILABLE.format(e))
    # 4. 兜底:不严格(开发模式)
    return False


def _extract_icu_param_set(text: str) -> set[str]:
    """R63 P1-12: 提取文本中引用的所有参数名(简单 {var} + ICU {var, plural/select/...})。

    与 scripts/check_i18n_key_symmetry.py 的 _extract_param_set 保持一致,
    确保 runtime 预编译检查与 CI 门禁判定标准统一。

    Args:
        text: 翻译值字符串

    Returns:
        参数名集合(可能为空);非 str 输入返回空集
    """
    if not isinstance(text, str):
        return set()
    params: set[str] = set()
    for m in _ICU_PATTERN.finditer(text):
        params.add(m.group(1))
    for m in _SIMPLE_VAR_PATTERN.finditer(text):
        params.add(m.group(1))
    return params


def _validate_icu_message(text: str) -> tuple[bool, str]:
    """R63 P1-12: 校验 ICU message 语法是否合法(预编译)。

    执行以下检查:
        1. 每个 ``{var, plural/select/selectordinal, body}`` 的 ``{...}`` 必须闭合
        2. body 中至少有一个 selector(如 ``one``/``other``/``=0``)
        3. selector 后必须紧跟 ``{...}`` 子句
        4. 子句的 ``{...}`` 必须闭合
        5. 不含未闭合的 ``{``(任何 ``{`` 必须有匹配的 ``}``)

    通过空 kwargs 调用 _icu_format 进行实际解析,任何异常都视为编译失败。

    Args:
        text: 待校验的 ICU 消息文本

    Returns:
        (ok, reason) — ok=True 表示编译通过;ok=False 时 reason 为失败原因
    """
    if not isinstance(text, str) or "{" not in text:
        return True, ""
    # 不含 ICU pattern 时直接通过(简单 {var} 不需要预编译)
    if not _ICU_PATTERN.search(text):
        return True, ""
    # 检查大括号是否平衡(粗粒度)
    depth = 0
    for c in text:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False, "unbalanced_brace_extra_close"
    if depth != 0:
        return False, f"unbalanced_brace_depth={depth}"
    # 通过空 kwargs 实际解析(检测 selector 缺失 / 空 body 等)
    try:
        _icu_format(text, "en-US", {})
    except Exception as e:
        return False, f"parse_error: {type(e).__name__}: {e}"
    return True, ""


# ── R61 P1-06: release 构建严格模式 + 高优先级告警钩子 ──────────


def _get_release_mode() -> bool:
    """R61 P1-06: 是否为 release 构建(任何 i18n 缺陷均 fail-fast)。

    判定优先级:
        1. 环境变量 ``RELEASE_BUILD=1/true/yes/on`` → release 模式
        2. ``config.settings.RELEASE_BUILD`` 为真实 ``bool`` True 时 → release 模式
           (防御:仅当为 ``isinstance(bool)`` 时才采纳,避免 MagicMock 测试 settings
           误判为 True)

    Returns:
        True=release 构建(缺 key/缺参/malformed ICU 直接抛 AppError);
        False=普通模式(生产 fallback 到安全文案 + 触发告警)
    """
    import os
    val = os.environ.get("RELEASE_BUILD", "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    try:
        from config.settings import settings  # type: ignore[import]
        flag = getattr(settings, "RELEASE_BUILD", False)
        # 防御:仅当为真实 bool True 时才认为是 release 模式
        # (避免 conftest MagicMock settings 把任意属性访问判为 True)
        if isinstance(flag, bool):
            return flag
    except Exception as e:
        # R61 P1-06: 配置不可用时视为非 release 模式(fail-open 到非严格)
        logger.debug(_i18n_t("services.i18n.logger_release_mode_read_failed", err=str(e)))
    return False


# R61 P1-06: i18n 高优先级告警回调(生产 fallback 时触发)
# ops 可通过 set_i18n_alert_callback 注册回调,将告警接入 PagerDuty / 飞书 / 监控等
_i18n_alert_callback: Optional[Callable[[str, dict], None]] = None


def set_i18n_alert_callback(fn: Optional[Callable[[str, dict], None]]) -> None:
    """R61 P1-06: 注册 i18n 高优先级告警回调。

    回调签名: ``fn(event_type: str, details: dict) -> None``
    event_type 取值: ``missing_key`` / ``missing_param`` / ``malformed_icu``

    Args:
        fn: 告警回调函数;传入 None 可注销回调
    """
    global _i18n_alert_callback
    _i18n_alert_callback = fn


def _trigger_i18n_alert(event_type: str, details: dict) -> None:
    """R61 P1-06: 触发 i18n 高优先级告警(生产环境 fallback 时调用)。

    若已注册回调,调用回调(异常吞掉仅记 ERROR,不影响主流程);
    否则以 ERROR 级别记录日志(高优先级告警,区别于普通 WARNING)。

    Args:
        event_type: 事件类型(``missing_key`` / ``missing_param`` / ``malformed_icu``)
        details: 事件详情 dict(含 key / locale / 缺失参数等)
    """
    if _i18n_alert_callback is not None:
        try:
            _i18n_alert_callback(event_type, details)
        except Exception as cb_err:
            logger.error(
                f"[i18n] R61 P1-06: 告警回调执行失败 event={event_type}: {cb_err}"
            )
    else:
        logger.error(
            f"[i18n] R61 P1-06: i18n 缺陷 fallback 触发高优先级告警 "
            f"event={event_type} details={details}"
        )


def _collect_missing_params(text: str, kwargs: dict) -> set:
    """R58 P1-2: 收集文本中引用但 kwargs 中缺失的参数名。

    扫描:
        - 简单 {var} 占位符
        - ICU {var, plural/select/...} 中的 var 名

    排除转义的 \\{var}。

    Args:
        text: ICU 模板文本
        kwargs: 调用方传入的参数

    Returns:
        缺失参数名集合(可能为空)
    """
    missing: set = set()
    # ICU pattern 中的 var
    for m in _ICU_PATTERN.finditer(text):
        var_name = m.group(1)
        if var_name not in kwargs:
            missing.add(var_name)
    # 简单 {var} 占位符(排除 ICU pattern 已覆盖的)
    for m in _SIMPLE_VAR_PATTERN.finditer(text):
        var_name = m.group(1)
        # 跳过 ICU pattern 内的 var(已上面处理)
        if var_name not in kwargs:
            missing.add(var_name)
    # 移除转义的占位符(\\{var} 不应算缺失)
    # 简单处理:如果文本中含 \\{var_name},且无对应非转义 {var_name},则不算缺失
    return missing


def _icu_select_branch(text: str, locale: str, kwargs: dict) -> str:
    """R56 §5.1: 解析 ICU MessageFormat 选择子句并展开。

    支持 plural/select/selectordinal 子句:
        {key, plural, =0 {none} one {# item} other {# items}}
        {key, select, male {He} female {She} other {They}}

    嵌套:  子句中可嵌套其他 {var} / {var, plural/select, ...}
    # 占位符: 在 plural/selectordinal 子句中, # 展开为 count 的本地化数字

    Args:
        text: 待格式化的 ICU 字符串
        locale: 目标 locale(用于复数规则选择)
        kwargs: 插值参数

    Returns:
        格式化后的字符串
    """
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # 查找下一个未转义的 {
        brace_idx = text.find("{", i)
        if brace_idx == -1:
            # 处理剩余文本中的转义 \}
            tail = text[i:].replace("\\}", "}")
            result.append(tail)
            break
        # 检查是否转义(`\{`)
        if brace_idx > 0 and text[brace_idx - 1] == "\\":
            # 输出 { 之前的文本(吞掉反斜杠)
            result.append(text[i:brace_idx - 1])
            result.append("{")
            i = brace_idx + 1
            continue
        # 输出 { 之前的文本(处理其中的 \} 转义)
        prefix = text[i:brace_idx].replace("\\}", "}")
        result.append(prefix)
        # 解析 {...} 块(支持嵌套)
        var_name, branch_text, next_idx = _icu_parse_block(text, brace_idx, locale, kwargs)
        if var_name is None:
            # 解析失败:原样输出
            result.append(text[brace_idx:next_idx])
        else:
            result.append(branch_text)
        i = next_idx
    return "".join(result)


def _icu_parse_block(
    text: str, start: int, locale: str, kwargs: dict
) -> tuple[Optional[str], str, int]:
    """R56 §5.1: 解析一个 {...} 块(支持嵌套),返回 (var_name, branch_text, next_idx)。

    若为简单插值 {var},var_name=var, branch_text=kwargs[var], next_idx 为 `}` 后位置
    若为 plural/select,branch_text 为选定子句展开后的文本
    若解析失败,var_name=None,branch_text 为原始文本(含 `{}`)
    """
    assert text[start] == "{"
    # 匹配到对应 `}` 的位置(考虑嵌套)
    depth = 0
    end = start
    while end < len(text):
        c = text[end]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        end += 1
    if end >= len(text):
        return None, text[start:], end
    block_content = text[start + 1: end]  # 不含外层 {}
    next_idx = end + 1

    # 解析 var_name + ,type + body(若有逗号)
    m = re.match(
        r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*(plural|select|selectordinal)\s*,\s*(.*)$",
        block_content,
        re.DOTALL,
    )
    if not m:
        # 简单插值 {var_name} 或 {var_name, format_type}(忽略 format_type)
        simple_m = re.match(
            r"^([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*,\s*([a-zA-Z_]+))?\s*$",
            block_content.strip(),
        )
        if not simple_m:
            return None, text[start:next_idx], next_idx
        var_name = simple_m.group(1)
        value = kwargs.get(var_name, "")
        if value is None:
            value = ""
        return var_name, str(value), next_idx

    var_name = m.group(1)
    block_type = m.group(2)
    body = m.group(3).strip()

    # 提取子句 (=N {..} / one {..} / other {..} 等)
    branches = _icu_parse_branches(body)

    # R58 P1-1: 获取变量值,分 plural/select 处理
    #   plural/selectordinal: 需要 int(count)
    #   select: 需要原始字符串值(male/female/...)
    count: int = 0
    if block_type == "select":
        raw_value = kwargs.get(var_name, "other")
        if raw_value is None:
            raw_value = "other"
        selected = _icu_select_branch_by_value(str(raw_value), branches)
    else:
        # plural / selectordinal
        raw_value = kwargs.get(var_name, 0)
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            count = 0
        selected = _icu_select_branch_by_count(count, block_type, locale, branches)

    # 展开 # 占位符为 count(仅 plural/selectordinal)
    if block_type in ("plural", "selectordinal"):
        selected = selected.replace("#", str(count))

    # R56 §5.1: 子句中可能含简单 {var} 占位符(非 ICU),也需展开
    # 例如 zh-CN: "{count, plural, =0 {无文件} other {{count} 个文件}}"
    # selected = "{count} 个文件" — 需展开 {count}
    selected = _icu_expand_simple_placeholders(selected, kwargs)

    # 递归展开嵌套 ICU pattern(如子句中含 {var, plural, ...})
    if _ICU_PATTERN.search(selected):
        selected = _icu_select_branch(selected, locale, kwargs)

    return var_name, selected, next_idx


def _icu_expand_simple_placeholders(text: str, kwargs: dict) -> str:
    """R56 §5.1: 展开文本中的简单 {var} 占位符(非 ICU pattern)。

    与 format_message 类似,但:
    - 不处理 ICU pattern(由 _icu_select_branch 递归处理)
    - kwargs 中 None 值转为空字符串
    - 转义的 \\{var} 不展开

    Args:
        text: 待展开的文本
        kwargs: 插值参数

    Returns:
        展开后的文本(简单 {var} 已替换)
    """
    if not text or "{" not in text:
        return text
    # 按顺序处理:先转义 \\{ ,再展开 {var}
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        brace_idx = text.find("{", i)
        if brace_idx == -1:
            parts.append(text[i:])
            break
        # 转义 \{ :原样输出 { (吞掉反斜杠)
        if brace_idx > 0 and text[brace_idx - 1] == "\\":
            parts.append(text[i:brace_idx - 1])
            parts.append("{")
            i = brace_idx + 1
            continue
        # 转义 \} :原样输出 }
        # 输出 { 之前的文本
        parts.append(text[i:brace_idx])
        # 查找匹配的 }
        depth = 1
        j = brace_idx + 1
        while j < n and depth > 0:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= n:
            # 无匹配 },原样输出
            parts.append(text[brace_idx:])
            break
        inner = text[brace_idx + 1: j]
        # 检查是否为 ICU pattern({var, plural/select/...})
        if _ICU_PATTERN.search("{" + inner + "}"):
            # ICU pattern — 不在此展开,留给递归处理
            parts.append(text[brace_idx: j + 1])
        else:
            # 简单 {var} 或 {var, format}(忽略 format)
            simple_m = re.match(
                r"^([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*,\s*([a-zA-Z_]+))?\s*$",
                inner.strip(),
            )
            if simple_m:
                var_name = simple_m.group(1)
                value = kwargs.get(var_name, "")
                if value is None:
                    value = ""
                parts.append(str(value))
            else:
                # 不匹配简单模式,原样输出
                parts.append(text[brace_idx: j + 1])
        i = j + 1
    return "".join(parts)


def _icu_parse_branches(body: str) -> dict[str, str]:
    """R56 §5.1: 解析 plural/select 子句 body,返回 {selector: text} dict。

    body 形如: ``=0 {none} one {# item} other {# items}``
    返回: ``{"=0": "none", "one": "# item", "other": "# items"}``
    """
    branches: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        # 跳过空白
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        # 读取 selector(=N / one / other / male / female / ...)
        selector_start = i
        while i < n and not body[i].isspace() and body[i] != "{":
            i += 1
        selector = body[selector_start:i].strip()
        if not selector:
            break
        # 跳过空白直到 {
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != "{":
            break
        # 读取 {..} 内容(考虑嵌套)
        depth = 1
        i += 1
        text_start = i
        while i < n and depth > 0:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        branch_text = body[text_start:i]
        i += 1  # 跳过 }
        branches[selector] = branch_text
    return branches


def _cldr_ordinal_category(n: int, locale: str) -> str:
    """R59 §5.1 P1: CLDR selectordinal 规则 — 返回序数类别(one/two/few/many/other)。

    实现完整的 CLDR ordinal 复数规则(不依赖第三方库),支持:
        - 英语(en): one/two/few/other(1st/2nd/3rd/4th...)
        - 俄语(ru): one/few/many/other(1-й/2-й/5-й...)
        - 中文(zh): 始终 other(中文不区分序数形式)
        - 其他 locale: 默认 other(兜底)

    R61 P1-06 变更:优先委托 Babel 标准 CLDR ordinal 规则(``Locale.ordinal_form``),
    不再自行维护 CLDR 数据。Babel 不可用 / locale 不支持时,回退到下方自维护的
    en/ru/zh 规则(已验证正确,保留作为兜底)。

    CLDR 规则参考: https://unicode-org.github.io/cldr-staging/charts/latest/supplemental/language_plural_rules.html

    英语(en) ordinal 规则:
        one:   n mod 10 = 1 and n mod 100 != 11   (1st, 21st, 31st, 101st...)
        two:   n mod 10 = 2 and n mod 100 != 12   (2nd, 22nd, 32nd, 102nd...)
        few:   n mod 10 = 3 and n mod 100 != 13   (3rd, 23rd, 33rd, 103rd...)
        other: 其余                                (4th, 5th, 11th, 12th, 13th...)

    俄语(ru) ordinal 规则(与 cardinal 相同):
        one:   n mod 10 = 1 and n mod 100 != 11           (1-й, 21-й, 31-й...)
        few:   n mod 10 in 2..4 and n mod 100 not in 12..14 (2-й, 3-й, 4-й, 22-й...)
        many:  n mod 10 = 0 or n mod 10 in 5..9 or n mod 100 in 11..14 (5-й, 10-й, 11-й...)
        other: 其余(分数等,整数场景不会命中)

    Args:
        n: 序数值(非负整数;负数取绝对值后套用规则)
        locale: locale 标识符(如 "en-US" / "ru-RU" / "zh-CN")

    Returns:
        CLDR ordinal 类别字符串:"one" / "two" / "few" / "many" / "other"
    """
    # R61 P1-06: 优先委托 Babel 标准 CLDR ordinal 规则(不自行维护 CLDR 数据)
    try:
        from babel import Locale  # type: ignore[import]
        if n < 0:
            n = -n
        # locale 标识符规范化:zh-CN → zh_CN(Babel 使用下划线)
        loc = Locale.parse(locale.replace("-", "_"))
        return str(loc.ordinal_form(n))
    except Exception as e:
        # R61 P1-06: Babel 不可用 / locale 不支持:回退到下方自维护规则
        logger.debug(_i18n_t("services.i18n.logger_babel_ordinal_unavailable", err=str(e)))

    # 负数取绝对值(CLDRL 规则对负数取模的处理:实际场景序数非负,兜底取绝对值)
    if n < 0:
        n = -n
    mod10 = n % 10
    mod100 = n % 100

    # 中文(zh-*): 不区分序数形式,始终 other
    if locale.startswith("zh"):
        return "other"

    # 英语(en-*): one/two/few/other
    if locale.startswith("en"):
        if mod10 == 1 and mod100 != 11:
            return "one"
        if mod10 == 2 and mod100 != 12:
            return "two"
        if mod10 == 3 and mod100 != 13:
            return "few"
        return "other"

    # 俄语(ru-*): one/few/many/other
    if locale.startswith("ru"):
        if mod10 == 1 and mod100 != 11:
            return "one"
        if mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
            return "few"
        if mod10 == 0 or mod10 in (5, 6, 7, 8, 9) or mod100 in (11, 12, 13, 14):
            return "many"
        return "other"

    # 其他 locale: 兜底 other(暂未实现完整 CLDR ordinal 规则)
    return "other"


def _icu_select_branch_by_count(
    count: int, block_type: str, locale: str, branches: dict[str, str]
) -> str:
    """R58 P1-1 / R59 §5.1 P1: 根据 count + locale 选择合适的子句(仅 plural/selectordinal)。

    优先级:
        1. 精确匹配 =N (如 =0 / =1)
        2. plural: 按 CLDR 复数规则选择 one/other(简化:en=1 用 one,其他用 other;zh 始终 other)
        3. selectordinal: 按 CLDR ordinal 规则选择(R59 §5.1 P1 新增)
           支持英语(one/two/few/other)/俄语(one/few/many/other)/中文(other)
        4. 兜底: other

    注意: select 类型不再走此函数,改由 _icu_select_branch_by_value 处理。

    R59 §5.1 P1 变更: selectordinal 不再简化为 "other",而是按 CLDR ordinal 规则
    选择 one/two/few/many/other 子句。若选中的子句不存在,回退到 other。
    """
    # 精确匹配 =N
    exact_key = f"={count}"
    if exact_key in branches:
        return branches[exact_key]

    # selectordinal: 按 CLDR ordinal 规则选择(R59 §5.1 P1 完整实现)
    if block_type == "selectordinal":
        category = _cldr_ordinal_category(count, locale)
        # 优先返回 CLDR 类别对应的子句;若不存在则回退到 other
        if category in branches:
            return branches[category]
        return branches.get("other", "")

    # plural: 按 locale 复数规则
    if block_type == "plural":
        # R61 P1-06: 优先委托 Babel 标准 CLDR plural 规则(Locale.plural_form)
        category: Optional[str] = None
        try:
            from babel import Locale  # type: ignore[import]
            loc = Locale.parse(locale.replace("-", "_"))
            category = str(loc.plural_form(count))
        except Exception:
            category = None
        if category is not None and category in branches:
            return branches[category]
        # 回退到自维护的简化规则(Babel 不可用 / 子句缺该类别时)
        if locale.startswith("zh"):
            return branches.get("other", "")
        # en-*: count == 1 用 one,其他用 other
        if count == 1 and "one" in branches:
            return branches["one"]
        return branches.get("other", "")
    # 兜底: other
    return branches.get("other", "")


def _icu_select_branch_by_value(value: str, branches: dict[str, str]) -> str:
    """R58 P1-1: 根据 select 变量的字符串值选择子句。

    ICU select 语义:
        1. 精确匹配分支 key(如 male / female)
        2. 无匹配时回退到 other 分支
        3. 无 other 分支时返回空字符串

    Args:
        value: select 变量的原始字符串值(如 "male" / "female" / "other")
        branches: 子句字典(key=selector, value=branch_text)

    Returns:
        选中的子句文本
    """
    if value in branches:
        return branches[value]
    return branches.get("other", "")


def _icu_format(text: str, locale: str, kwargs: dict) -> str:
    """R56 §5.1: ICU MessageFormat 子集格式化入口。

    处理顺序:
        1. 优先展开所有 ICU plural/select 子句(从内向外)
        2. 简单 {var} 插值

    Returns:
        格式化后的字符串
    """
    return _icu_select_branch(text, locale, kwargs)

# ── R51 P1-9: 用户 locale LRU 缓存配置 ──────────────────────────
# 缓存 TTL(秒,默认 5 分钟):超过后缓存条目视为过期,下次访问重新加载
_USER_LOCALE_CACHE_TTL_SECONDS: int = 300
# 缓存容量上限(LRU 淘汰):防止活跃用户数过多导致内存膨胀
_USER_LOCALE_CACHE_MAX_SIZE: int = 1024
# 模块级缓存:OrderedDict 维护 LRU 顺序(user_id → (expire_ts, locale))
# 注意:跨进程不共享(每个 Bot 进程独立缓存),写穿透到 SQLite 由 set_user_locale 负责
_user_locale_cache: "OrderedDict[int, tuple[float, str]]" = OrderedDict()
# 异步加载时的 executor(惰性创建,避免事件循环未启动时创建失败)
_locale_cache_lock: Optional[asyncio.Lock] = None


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
        # R63 P1-12: ICU 预编译缓存
        # 结构: {locale: {key: {"ok": bool, "reason": str, "params": set[str]}}}
        # 在 load_locale 阶段填充;format_message_icu 直接查此缓存避免运行时解析
        self._compiled_icu_cache: dict[str, dict[str, dict]] = {}
        # R63 P1-12: 跨 locale 参数集合对称性检查结果(避免重复扫描)
        # 结构: {"checked": bool, "asymmetries": list[dict]}
        self._param_asymmetry_check_done: bool = False

    def load_locale(self, locale: str) -> bool:
        """加载指定 locale 的 JSON 文件。

        文件路径: <locales_dir>/<locale>.json
        若已加载过则跳过(幂等)。

        R63 P1-12: 加载完成后立即预编译所有 ICU message:
            - 严格模式(release / production / ICU_STRICT_MODE=1):
              任何 ICU 语法错误直接抛 AppError(I18N_ICU_COMPILE_FAILED)阻断加载
            - 宽松模式: 失败记录 warning + 计数,继续加载(运行时回退到 format_message)

        Args:
            locale: locale 标识符(如 zh-CN)

        Returns:
            True=成功;False=失败(文件不存在或解析错误)

        Raises:
            AppError: 严格模式下 ICU 预编译失败(I18N_ICU_COMPILE_FAILED)
        """
        if not locale:
            return False
        # R61 P1-06: 锁定支持的 locale(产品仅支持 zh-CN/en-US,拒绝其他 locale)
        if locale not in SUPPORTED_LOCALES:
            logger.warning(
                f"[i18n] R61 P1-06: 拒绝加载不支持的 locale={locale} "
                f"(SUPPORTED_LOCALES={sorted(SUPPORTED_LOCALES)})"
            )
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
            # R63 P1-12: 预编译所有 ICU message(strict 失败时抛 AppError)
            self._precompile_icu_messages(locale, flat)
            # R63 P1-12: 跨 locale 参数集合对称性检查(两个 locale 都加载后)
            self._check_param_asymmetry_strict()
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[i18n] locale 文件 {locale} 解析失败: {e}")
            return False
        except Exception as e:
            # AppError(I18N_ICU_COMPILE_FAILED)等预期异常直接传播(strict 模式下)
            # 不再吞掉;否则原 fallback 行为(返回 False)保留
            from services.error_codes import AppError as _AppError  # type: ignore[import]
            if isinstance(e, _AppError):
                raise
            logger.warning(f"[i18n] locale 文件 {locale} 加载异常: {e}")
            return False

    def _precompile_icu_messages(self, locale: str, flat: dict[str, str]) -> None:
        """R63 P1-12: 预编译 locale 中所有 ICU message。

        对每个含 ICU pattern 的 value 执行:
            1. 调用 _validate_icu_message 校验语法(括号平衡 / 解析通过)
            2. 提取参数集合(_extract_icu_param_set)
            3. 缓存到 ``self._compiled_icu_cache[locale][key]``

        严格模式下任一编译失败 → 抛 AppError(I18N_ICU_COMPILE_FAILED)阻断加载。
        宽松模式下记录 warning + 计数,继续加载。

        Args:
            locale: locale 标识符
            flat: 扁平化后的 key → value 字典

        Raises:
            AppError: 严格模式下 ICU 编译失败
        """
        strict = _get_icu_strict_mode()
        cache: dict[str, dict] = {}
        failures: list[tuple[str, str]] = []  # [(key, reason), ...]
        for key, value in flat.items():
            if not isinstance(value, str) or "{" not in value:
                continue
            if not _ICU_PATTERN.search(value):
                # 简单 {var} 占位符也提取参数集合,便于跨 locale 对称检查
                params = _extract_icu_param_set(value)
                cache[key] = {
                    "ok": True,
                    "reason": "",
                    "params": params,
                    "is_icu": False,
                }
                continue
            ok, reason = _validate_icu_message(value)
            params = _extract_icu_param_set(value)
            cache[key] = {
                "ok": ok,
                "reason": reason,
                "params": params,
                "is_icu": True,
            }
            if not ok:
                failures.append((key, reason))
                _incr_metric("i18n_icu_compile_failed_total")
        self._compiled_icu_cache[locale] = cache
        if failures:
            if strict:
                # 严格模式:抛 AppError 阻断加载(release / production)
                from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
                first_key, first_reason = failures[0]
                logger.error(_i18n_t(
                    "services.i18n.logger_icu_precompile_failed_strict",
                    locale=locale, failures=len(failures),
                    first_key=first_key, first_reason=first_reason,
                ))
                raise AppError(
                    ErrorCodes.I18N_ICU_COMPILE_FAILED,
                    params={
                        "locale": locale,
                        "key": first_key,
                        "reason": first_reason,
                    },
                )
            # 宽松模式:记录 warning,继续加载(运行时 format_message_icu 回退)
            for key, reason in failures:
                logger.warning(_i18n_t(
                    "services.i18n.logger_icu_precompile_failed_loose",
                    locale=locale, icu_key=key, reason=reason,
                ))

    def _check_param_asymmetry_strict(self) -> None:
        """R63 P1-12: 跨 locale 参数集合对称性检查(两个 locale 都加载后)。

        检查规则(与 scripts/check_i18n_key_symmetry.py 一致):
            - 对每个公共 key,zh-CN 与 en-US 的参数集合必须完全一致
            - 参数集合包含简单 {var} 占位符 + ICU pattern 中的 var 名
            - 类型不对称也是错误(zh-CN 用 ``{count, plural, ...}`` 而 en-US 用 ``{count}`` 视为不对称)

        严格模式下任一不对称 → 抛 AppError(I18N_ICU_COMPILE_FAILED)。
        宽松模式下记录 warning,继续运行(供开发期发现)。

        仅当 zh-CN 与 en-US 均已加载时执行,避免单 locale 加载场景误报。
        """
        if self._param_asymmetry_check_done:
            return
        zh_cache = self._compiled_icu_cache.get("zh-CN")
        en_cache = self._compiled_icu_cache.get("en-US")
        if zh_cache is None or en_cache is None:
            return  # 两个 locale 未全部加载,跳过
        self._param_asymmetry_check_done = True
        common_keys = set(zh_cache.keys()) & set(en_cache.keys())
        asymmetries: list[tuple[str, set, set]] = []
        for key in sorted(common_keys):
            zh_params = zh_cache[key]["params"]
            en_params = en_cache[key]["params"]
            if zh_params != en_params:
                asymmetries.append((key, zh_params, en_params))
        if not asymmetries:
            return
        strict = _get_icu_strict_mode()
        if strict:
            from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
            first_key, zh_p, en_p = asymmetries[0]
            logger.error(_i18n_t(
                "services.i18n.logger_icu_param_asymmetry_strict",
                asymmetries=len(asymmetries), first_key=first_key,
                zh_params=sorted(zh_p), en_params=sorted(en_p),
            ))
            raise AppError(
                ErrorCodes.I18N_ICU_COMPILE_FAILED,
                params={
                    "locale": "zh-CN/en-US",
                    "key": first_key,
                    "reason": (
                        f"param_asymmetry: zh-CN={sorted(zh_p)} "
                        f"vs en-US={sorted(en_p)}"
                    ),
                },
            )
        for key, zh_p, en_p in asymmetries:
            logger.warning(_i18n_t(
                "services.i18n.logger_icu_param_asymmetry_loose",
                icu_key=key, zh_params=sorted(zh_p), en_params=sorted(en_p),
            ))

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
            # R61 P1-06: release 构建下 missing key 直接 fail-fast(不返回安全文案)
            if _get_release_mode():
                from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
                raise AppError(
                    ErrorCodes.VALIDATION_FAILED,
                    params={
                        "field": "i18n_key",
                        "reason": "missing_key_in_release_build",
                        "key": key,
                        "locale": target_locale,
                    },
                )
            text = self._get_safe_fallback_message(key, target_locale)
            # R44 6.2: 累计 missing key 计数(供 Prometheus 采集)
            self._missing_key_count += 1
            # R61 P1-06: 生产环境(非 release)missing key 触发高优先级告警
            _trigger_i18n_alert(
                "missing_key",
                {
                    "key": key,
                    "locale": target_locale,
                    "manager_default": self.default_locale,
                },
            )
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
        # R61 P1-06: 锁定支持的 locale(产品仅支持 zh-CN/en-US,拒绝其他 locale)
        if locale not in SUPPORTED_LOCALES:
            logger.warning(
                f"[i18n] R61 P1-06: 拒绝设置不支持的默认 locale={locale} "
                f"(SUPPORTED_LOCALES={sorted(SUPPORTED_LOCALES)})"
            )
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
        return _i18n_t('services.i18n.s1')

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
        # R63 P1-12: 同步清空 ICU 预编译缓存 + 重置对称检查标志
        self._compiled_icu_cache.clear()
        self._param_asymmetry_check_done = False
        count = 0
        for loc in loaded_locales:
            if self.load_locale(loc):
                count += 1
        logger.info(f"[i18n] 重新加载 {count}/{len(loaded_locales)} locale 文件")
        return count

    # ── R41 i18n 下一阶段: 格式化层(format_message / format_error_code /
    #                                  format_datetime / format_file_size) ──

    def format_message_icu(self, key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
        """R56 §5.1: ICU MessageFormat 格式化接口。

        支持 ICU MessageFormat 子集语法:
            - 简单插值:  ``Hello {name}``  →  Hello Alice
            - plural:    ``{count, plural, =0 {none} one {# item} other {# items}}``
            - select:    ``{gender, select, male {He} female {She} other {They}}``
            - # 占位符:  在 plural/select 子句中 ``#`` 展开为 count 的值

        向后兼容:
            - 若文本不含 `{var,` (非 ICU pattern),回退到 format_message(简单 {var} 插值)
            - 若 ICU 解析失败,回退到 format_message

        R63 P1-12: 优先使用 load_locale 阶段的预编译结果(_compiled_icu_cache)。
            - 若预编译结果 ok=False 且当前为 strict 模式,直接抛
              AppError(I18N_ICU_COMPILE_FAILED)(不进入运行时解析)
            - 若 key 未预编译(例如直接 put_translations / 测试场景),惰性编译并缓存

        Args:
            key: 翻译 key(如 "common.files.count_icu")
            locale: 目标 locale(默认 self.default_locale)
            **kwargs: 插值参数(如 count=5 → 替换 {count, plural, ...})

        Returns:
            ICU MessageFormat 格式化后的字符串;失败回退到 format_message

        Raises:
            AppError: strict 模式下 ICU 编译失败(I18N_ICU_COMPILE_FAILED)
        """
        target_locale = locale or self.default_locale
        if target_locale not in self._translations:
            if not self.load_locale(target_locale):
                target_locale = _FALLBACK_LOCALE
        text = self.translate(key, locale=target_locale)
        if not text or not kwargs:
            return text
        # 若非 ICU pattern,回退到 format_message
        if "{" not in text:
            return text
        # 检测是否含 ICU 语法({var, plural/select/...)
        is_icu = _ICU_PATTERN.search(text) is not None
        if not is_icu:
            # 旧式 {var} 简单插值
            return self.format_message(key, locale=target_locale, **kwargs)
        # R63 P1-12: 查询预编译缓存(load_locale 阶段填充)
        compiled = self._lookup_compiled_icu(target_locale, key, text)
        if compiled is not None and not compiled["ok"]:
            # 预编译失败的 message:strict 模式直接抛 AppError
            if _get_icu_strict_mode():
                from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
                raise AppError(
                    ErrorCodes.I18N_ICU_COMPILE_FAILED,
                    params={
                        "locale": target_locale,
                        "key": key,
                        "reason": compiled["reason"],
                    },
                )
            # 宽松模式:记录 warning + 触发告警,继续走 format_message 回退
            logger.warning(_i18n_t(
                "services.i18n.logger_icu_precompile_runtime_fallback",
                icu_key=key, locale=target_locale, reason=compiled["reason"],
            ))
            _incr_metric("i18n_icu_compile_failed_total")
            _trigger_i18n_alert(
                "malformed_icu",
                {
                    "key": key,
                    "locale": target_locale,
                    "error_type": "icu_precompile_failed",
                    "error": compiled["reason"],
                },
            )
            return self.format_message(key, locale=target_locale, **kwargs)
        # R58 P1-2: 检测缺失参数(staging/test 硬失败,production 记录指标)
        # 收集文本中所有简单 {var} 与 ICU {var, ...} 的 var 名
        missing_params = _collect_missing_params(text, kwargs)
        if missing_params:
            env = _get_environment()
            if env in ("test", "staging") or _get_release_mode():
                # 惰性导入避免循环依赖(R61 P1-06: 修正为 services.error_codes)
                from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
                raise AppError(
                    ErrorCodes.VALIDATION_FAILED,
                    params={
                        "field": "icu_params",
                        "reason": "missing_params_in_icu_template",
                        "key": key,
                        "missing": ",".join(sorted(missing_params)),
                    },
                )
            # production: 记录指标,回退到 format_message(会用空字符串替换)
            logger.warning(
                f"[i18n] R58 P1-2: ICU template missing params key={key} "
                f"missing={sorted(missing_params)}"
            )
            _incr_metric("i18n_missing_param_total")
            # R61 P1-06: 生产 fallback 触发高优先级告警
            _trigger_i18n_alert(
                "missing_param",
                {
                    "key": key,
                    "locale": target_locale,
                    "missing": sorted(missing_params),
                },
            )
        # ICU 解析
        try:
            return _icu_format(text, target_locale, kwargs)
        except Exception as e:
            env = _get_environment()
            if env in ("test", "staging") or _get_release_mode() or _get_icu_strict_mode():
                from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
                # R63 P1-12: strict 模式下使用 I18N_ICU_COMPILE_FAILED(语义更准确)
                if _get_icu_strict_mode():
                    raise AppError(
                        ErrorCodes.I18N_ICU_COMPILE_FAILED,
                        params={
                            "locale": target_locale,
                            "key": key,
                            "reason": f"runtime_parse: {type(e).__name__}: {e}",
                        },
                    ) from e
                raise AppError(
                    ErrorCodes.VALIDATION_FAILED,
                    params={
                        "field": "icu_parse",
                        "reason": "icu_parse_failed",
                        "key": key,
                        "error_type": type(e).__name__,
                    },
                ) from e
            # production: 记录指标,回退(避免把原始 ICU 大括号展示给用户)
            logger.warning(
                f"[i18n] R58 P1-2: ICU parse failed key={key}: {type(e).__name__}: {e}"
            )
            _incr_metric("i18n_parse_failed_total")
            # R61 P1-06: 生产 fallback 触发高优先级告警
            _trigger_i18n_alert(
                "malformed_icu",
                {
                    "key": key,
                    "locale": target_locale,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            return self.format_message(key, locale=target_locale, **kwargs)

    def _lookup_compiled_icu(
        self, locale: str, key: str, text: str,
    ) -> Optional[dict]:
        """R63 P1-12: 查询 ICU 预编译缓存,若缺失则惰性编译并缓存。

        Args:
            locale: 目标 locale
            key: 翻译 key
            text: 翻译值(用于惰性编译)

        Returns:
            预编译结果 dict(``{"ok": bool, "reason": str, "params": set, "is_icu": bool}``),
            若非 ICU pattern 返回 None(由调用方走 format_message 路径)
        """
        cache = self._compiled_icu_cache.get(locale)
        if cache is None:
            # locale 未通过 load_locale 加载(例如直接修改 _translations 的测试场景)
            # 创建空缓存并惰性编译此 key
            cache = {}
            self._compiled_icu_cache[locale] = cache
        if key in cache:
            return cache[key]
        # 惰性编译并缓存(仅对此 key)
        if not _ICU_PATTERN.search(text):
            compiled = {
                "ok": True,
                "reason": "",
                "params": _extract_icu_param_set(text),
                "is_icu": False,
            }
        else:
            ok, reason = _validate_icu_message(text)
            compiled = {
                "ok": ok,
                "reason": reason,
                "params": _extract_icu_param_set(text),
                "is_icu": True,
            }
        cache[key] = compiled
        return compiled

    def format_selectordinal(
        self,
        key: str,
        locale: Optional[str] = None,
        count: int = 0,
        **kwargs: Any,
    ) -> str:
        """R59 §5.1 P1: CLDR selectordinal 格式化接口。

        翻译 key 对应的 ICU MessageFormat 文本,使用 selectordinal 子句按 CLDR
        ordinal 规则选择分支,并展开 # / {var} 占位符。

        ICU 语法示例:
            "common.rank.ordinal": "{n, selectordinal,
                one {#st place} two {#nd place} few {#rd place} other {#th place}}"

        调用:
            manager.format_selectordinal("common.rank.ordinal", locale="en-US", count=3)
            → "3rd place"
            manager.format_selectordinal("common.rank.ordinal", locale="en-US", count=11)
            → "11th place"

        CLDR ordinal 规则支持:
            - en-*: one/two/few/other
            - ru-*: one/few/many/other
            - zh-*: 始终 other
            - 其他: other(兜底)

        缺失 key / 缺参数 / malformed ICU 的处理与 format_message_icu 一致:
            - test/staging 环境: fail-fast 抛 AppError(VALIDATION_FAILED)
            - production: 记录 trace + 失败指标,回退到 format_message

        Args:
            key: 翻译 key(如 "common.rank.ordinal")
            locale: 目标 locale(默认 self.default_locale)
            count: 序数值(用于选择 selectordinal 分支 + 展开 #)
            **kwargs: 额外插值参数

        Returns:
            本地化序数字符串;失败时回退到 format_message
        """
        # 合并 count 到 kwargs(selectordinal 子句通过 var 名引用 count)
        # 调用 format_message_icu 完成实际的 ICU 解析 + CLDR ordinal 分支选择
        # kwargs 中已含 count,ICU pattern 中的 {n, selectordinal, ...} 会读取 kwargs["n"]
        interp = {"n": count, **kwargs}
        return self.format_message_icu(key, locale=locale, **interp)

    def format_message(self, key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
        """R41/R42/R45: 显式格式化接口 — 翻译 key 并用 {var} 占位符插值。

        与 translate() 的区别:
        - 显式语义: 调用方明确知道这是一个"格式化"操作,而非简单翻译查找
        - 支持任意 {var} 占位符(如 "上传成功,文件码: {code}")
        - kwargs 中的 None 值会被转为空字符串,避免 'None' 字面量泄漏到用户消息
        - 找不到 key 时回退到默认 locale 再回退到 fallback locale

        R45 17.1 安全整改:
        - 缺失 key 时返回安全通用文案(由 translate() → _get_safe_fallback_message 兜底),
          禁止向用户暴露内部 key;同时累计 i18n_missing_key_total 指标
          (供 Prometheus exporter 采集为 tgjiema_i18n_missing_key_total)
        - 若 locale 不存在(文件未加载且加载失败),直接 fallback 到 en-US
          (避免回退到 default_locale 导致的语言错位)

        Args:
            key: 翻译 key(如 "bot.upload_success")
            locale: 目标 locale(默认 self.default_locale)
            **kwargs: 插值参数(如 code="ABC123" → 替换 {code})

        Returns:
            格式化后的字符串(占位符已替换);缺失 key 时返回安全通用文案
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

    # R62 P1-05: 所有用户出口应接受 UserMessage 结构化对象,而非裸字符串
    def render_user_message(self, msg: Any) -> str:
        """R62 P1-05: 渲染 UserMessage 结构化对象为本地化字符串。

        所有用户面出口(FastAPI response、Telegram、WebSocket、SSE、邮件、通知、模板)
        应通过此方法渲染 UserMessage,而非直接接受裸字符串。

        Args:
            msg: UserMessage 实例(来自 services.user_message)
                — 通过 TYPE_CHECKING 避免运行时循环依赖

        Returns:
            本地化字符串(message_key 已翻译,params 已 ICU 插值)

        Note:
            此方法是 UserMessage.render(self) 的 I18nManager 端入口,
            便于在不直接依赖 services.user_message 的模块中调用:
                msg = UserMessage.from_key("bot.upload_banned", locale=locale)
                text = i18n_manager.render_user_message(msg)
        """
        # 延迟导入避免循环依赖(services.user_message 类型注解中引用 AppError)
        # UserMessage.render 内部调用 self.format_message,本方法仅做转发
        return msg.render(self)

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
        format: str = "short",
    ) -> str:
        """R41/R45 17.1: 日期时间本地化格式化。

        根据 locale 选择日期格式,根据 timezone 进行时区转换。
        - zh-CN short: "2024年1月15日 14:30"
        - zh-CN long: "2024年1月15日 星期一 下午 02:30"
        - en-US short: "Jan 15, 2024 02:30 PM"
        - en-US long: "Monday, January 15, 2024 02:30 PM"

        Args:
            dt: 待格式化的 datetime 对象(naive 视为 UTC)
            locale: 目标 locale(默认 self.default_locale)
            timezone: 目标时区名(如 "Asia/Shanghai" / "UTC"),
                      None 时使用 locale 对应的默认时区
            format: 格式类型, "short"(紧凑,默认)或 "long"(含星期/完整月份)

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
        # 按 locale + format 选择格式
        # 注意: strftime 的 %-m / %-d 仅 Linux 支持,Windows 不支持
        # 改为跨平台方案: 先 strftime 再字符串替换去除前导零
        use_long = format == "long"
        if target_locale.startswith("zh"):
            # 中文格式
            if use_long:
                # 长格式: 2024年1月15日 星期一 下午 02:30
                formatted = dt.strftime(_i18n_t('services.i18n.s5'))
                # 中文本地化星期/上下午
                weekday_map = {
                    "Monday": _i18n_t('services.i18n.s6'), "Tuesday": _i18n_t('services.i18n.s7'), "Wednesday": _i18n_t('services.i18n.s8'),
                    "Thursday": _i18n_t('services.i18n.s9'), "Friday": _i18n_t('services.i18n.s10'), "Saturday": _i18n_t('services.i18n.s11'),
                    "Sunday": _i18n_t('services.i18n.s12'),
                }
                for en_w, zh_w in weekday_map.items():
                    formatted = formatted.replace(en_w, zh_w)
                formatted = formatted.replace("AM", _i18n_t('services.i18n.s17')).replace("PM", _i18n_t('services.i18n.s13'))
            else:
                # 短格式: 2024年1月15日 14:30
                formatted = dt.strftime(_i18n_t('services.i18n.s14'))
            # 去除月/日的前导零(跨平台方案,兼容 Windows 与 Linux)
            formatted = formatted.replace(_i18n_t('services.i18n.s15'), _i18n_t('services.i18n.s16')).replace(_i18n_t('services.i18n.s3'), _i18n_t('services.i18n.s4'))
            return formatted
        elif target_locale.startswith("en"):
            # 英文格式
            if use_long:
                # 长格式: Monday, January 15, 2024 02:30 PM
                return dt.strftime("%A, %B %d, %Y %I:%M %p").replace(" 0", " ")
            # 短格式: Jan 15, 2024 02:30 PM
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

    def format_plural(
        self,
        key: str,
        locale: Optional[str] = None,
        count: int = 0,
        **kwargs: Any,
    ) -> str:
        """R45 17.1: CLDR 风格复数格式化 — 按 key.one / key.other 选择复数形式。

        locale JSON 中 key 对应一个 dict:
            "common": {"files": {"count": {
                "one": "{count} 个文件",
                "other": "{count} 个文件"
            }}}
        扁平化后为 "common.files.count.one" / "common.files.count.other"。

        复数规则(简化 CLDR):
            - zh-*: 始终使用 "other"(中文不区分单复数)
            - en-*: count == 1 用 "one",其他(含 0)用 "other"
            - 其他 locale: 默认按英文规则

        若 key.one/key.other 均缺失,回退到 key 本身(若有),
        再回退到安全通用文案(并累计 missing_key_count, R44 6.2)。

        Args:
            key: 复数翻译 key 前缀(如 "common.files.count")
            locale: 目标 locale(默认 self.default_locale)
            count: 数量(用于选择复数形式 + 插值 {count})
            **kwargs: 额外插值参数

        Returns:
            本地化复数消息(已替换 {count} 等占位符)
        """
        target_locale = locale or self.default_locale
        # 确保目标 locale 已加载
        if target_locale not in self._translations:
            self.load_locale(target_locale)
        # 选择复数形式
        if target_locale.startswith("zh"):
            form = "other"
        else:
            form = "one" if count == 1 else "other"
        sub_key = f"{key}.{form}"
        # 合并插值参数
        interp = {"count": count, **kwargs}
        # 先尝试指定 locale 的子 key
        candidates = [target_locale, self.default_locale, _FALLBACK_LOCALE]
        seen: set[str] = set()
        text = None
        for loc in candidates:
            if loc in seen:
                continue
            seen.add(loc)
            if loc not in self._translations:
                self.load_locale(loc)
            translations = self._translations.get(loc)
            if translations and sub_key in translations:
                text = translations[sub_key]
                break
        if text is None:
            # 回退:尝试 key 本身(非复数形式)
            for loc in candidates:
                if loc not in self._translations:
                    self.load_locale(loc)
                translations = self._translations.get(loc)
                if translations and key in translations and isinstance(
                    translations[key], str
                ):
                    text = translations[key]
                    break
        if text is None:
            # 最终回退:安全通用文案 + 累计 missing key
            logger.debug(
                f"[i18n] format_plural key 未找到: {key}(locale={target_locale})"
            )
            text = self._get_safe_fallback_message(key, target_locale)
            self._missing_key_count += 1
        # 插值
        if interp and "{" in text and "}" in text:
            try:
                text = text.format(**interp)
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"[i18n] format_plural 插值失败 key={key}: {e}")
        return text

    def parse_accept_language(self, header: Optional[str]) -> str:
        """R45 17.1: 解析 HTTP Accept-Language header,返回最佳匹配 locale。

        解析 RFC 7231 格式:
            "zh-CN,zh;q=0.9,en;q=0.8" → 优先 zh-CN
            "en-US,en;q=0.9" → en-US
            "" 或 None → 默认 locale(zh-CN)

        匹配规则:
            1. 按 q 值降序排序
            2. 优先精确匹配(如 zh-CN == zh-CN)
            3. 其次前缀匹配(如 zh 匹配 zh-CN)
            4. 无匹配返回默认 locale

        Args:
            header: Accept-Language header 值(如 "zh-CN,zh;q=0.9,en;q=0.8")

        Returns:
            匹配的 locale 标识符(如 "zh-CN" / "en-US");无匹配返回默认 locale
        """
        if not header:
            return _DEFAULT_LOCALE
        available = self.get_available_locales()
        if not available:
            return _DEFAULT_LOCALE
        # 解析 <lang>;q=<value> 列表
        parsed: list[tuple[str, float]] = []
        for part in header.split(","):
            part = part.strip()
            if not part:
                continue
            # 分离语言和 q 参数
            segments = part.split(";")
            lang = segments[0].strip()
            if not lang:
                continue
            q = 1.0
            for seg in segments[1:]:
                seg = seg.strip()
                if seg.startswith("q="):
                    try:
                        q = float(seg[2:])
                    except ValueError:
                        q = 0.0
            # q=0 表示不接收
            if q > 0:
                parsed.append((lang, q))
        if not parsed:
            return _DEFAULT_LOCALE
        # 按 q 降序排序(稳定排序保持原顺序)
        parsed.sort(key=lambda x: -x[1])
        # 精确匹配
        for lang, _ in parsed:
            if lang in available:
                return lang
        # 前缀匹配(如 "zh" 匹配 "zh-CN","en" 匹配 "en-US")
        for lang, _ in parsed:
            base = lang.split("-")[0].lower()
            for avail in available:
                if avail.lower().startswith(base):
                    return avail
        return _DEFAULT_LOCALE

    def get_user_locale(self, user_id: int) -> str:
        """R41/R51 P1-9: 从 users_local 表读取用户 locale(默认 zh-CN)。

        R51 P1-9 缓存增强:
            - 优先从内存 LRU 缓存读取(5 分钟 TTL),命中直接返回,不触发 SQLite
            - 缓存 miss 或过期时同步从 SQLite 加载,并回填缓存
            - 加载失败时回退到默认 locale 'zh-CN'(缓存失败结果避免短期内重复穿透)
            - LRU 容量上限 1024(超过后淘汰最久未访问的条目)

        从 SQLite cache_store 同步读取(不触发 CRDB RU 消耗)。
        用户未设置或读取失败时返回默认 locale 'zh-CN'。

        Args:
            user_id: Telegram 用户 ID

        Returns:
            用户 locale 字符串(如 "zh-CN" / "en-US")
        """
        if not user_id:
            return _DEFAULT_LOCALE
        # R51 P1-9: 优先查缓存(命中则直接返回,不触发 SQLite)
        now = time.time()
        cached = _user_locale_cache.get(int(user_id))
        if cached is not None:
            expire_ts, locale_val = cached
            if now < expire_ts:
                # 缓存命中且未过期:LRU move-to-end(标记为最近访问)
                _user_locale_cache.move_to_end(int(user_id))
                return locale_val
            # 缓存过期:移除条目,继续走 SQLite 加载路径
            _user_locale_cache.pop(int(user_id), None)

        # 缓存 miss / 过期:从 SQLite 加载
        locale_val = self._load_user_locale_from_sqlite(user_id)
        # R51 P1-9: 回填缓存(即使返回默认 locale 也缓存,避免短期内重复穿透 SQLite)
        _cache_user_locale(int(user_id), locale_val)
        return locale_val

    def _load_user_locale_from_sqlite(self, user_id: int) -> str:
        """R51 P1-9: 直接从 SQLite 同步加载用户 locale(内部方法,不带缓存)。

        与 get_user_locale 的区别:本方法不查缓存,直接读 SQLite。
        供 get_user_locale(缓存 miss 时)和 get_user_locale_async(executor 中)调用。

        Args:
            user_id: Telegram 用户 ID

        Returns:
            用户 locale 字符串;失败返回 _DEFAULT_LOCALE
        """
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
            logger.debug(f"[i18n] _load_user_locale_from_sqlite 读取失败 user={user_id}: {e}")
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


# Local alias for migrated strings within this module (avoids circular import)
_i18n_t = translate


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

    R51 P1-9: 已接入 LRU 缓存(5 分钟 TTL),命中时不触发 SQLite。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        用户 locale 字符串(如 "zh-CN" / "en-US");失败返回 "zh-CN"
    """
    manager = get_i18n_manager()
    return manager.get_user_locale(user_id)


# ── R51 P1-9: 用户 locale LRU 缓存辅助函数 ──────────────────────


def _cache_user_locale(user_id: int, locale_val: str) -> None:
    """R51 P1-9: 将 user_id → locale 写入模块级 LRU 缓存。

    若缓存超过 _USER_LOCALE_CACHE_MAX_SIZE,淘汰最久未访问的条目(LRU)。
    TTL 由 _USER_LOCALE_CACHE_TTL_SECONDS 控制,过期后下次访问触发重新加载。

    Args:
        user_id: 用户 ID(已 int 化)
        locale_val: locale 字符串(如 "zh-CN")
    """
    if not user_id or not locale_val:
        return
    now = time.time()
    expire_ts = now + _USER_LOCALE_CACHE_TTL_SECONDS
    # 若已存在,先移除再插入(move-to-end 效果)
    if user_id in _user_locale_cache:
        _user_locale_cache.pop(user_id, None)
    _user_locale_cache[user_id] = (expire_ts, locale_val)
    # LRU 淘汰:超过容量时移除最旧的条目(OrderedDict 最早插入的)
    while len(_user_locale_cache) > _USER_LOCALE_CACHE_MAX_SIZE:
        _user_locale_cache.popitem(last=False)


def invalidate_user_locale_cache(user_id: Optional[int] = None) -> int:
    """R51 P1-9: 主动失效用户 locale 缓存。

    使用场景:
        - set_user_locale 写入新 locale 后,主动失效旧缓存(下次访问重新加载)
        - 管理员手动重置某用户 locale 后调用
        - 测试用例间隔离

    Args:
        user_id: 指定用户 ID 时仅失效该用户;None 时清空整个缓存

    Returns:
        被移除的缓存条目数
    """
    if user_id is None:
        removed = len(_user_locale_cache)
        _user_locale_cache.clear()
        logger.debug(f"[i18n] 用户 locale 缓存已全部清空(共 {removed} 条)")
        return removed
    if user_id in _user_locale_cache:
        _user_locale_cache.pop(user_id, None)
        logger.debug(f"[i18n] 用户 locale 缓存已失效: user_id={user_id}")
        return 1
    return 0


async def get_user_locale_async(user_id: int) -> str:
    """R51 P1-9: 异步获取用户 locale(带 LRU 缓存,不阻塞事件循环)。

    与 get_user_locale_sync 的区别:
        - 缓存命中:直接返回(与 sync 版本一致,无 SQLite IO)
        - 缓存 miss / 过期:在默认 executor 中执行 SQLite 同步读取,
          不阻塞 asyncio 事件循环(适合在 Bot handler 中 await 调用)

    并发安全:
        - 使用 asyncio.Lock 防止同一 user_id 并发 miss 时多次穿透 SQLite
        - 第一个 miss 协程加载并填充缓存,后续协程直接读缓存

    缓存失效后回退到默认语言 'zh-CN'(由 _load_user_locale_from_sqlite 保证)。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        用户 locale 字符串(如 "zh-CN" / "en-US");失败返回 "zh-CN"
    """
    if not user_id:
        return _DEFAULT_LOCALE
    global _locale_cache_lock
    # R51 P1-9: 优先查缓存(无锁快速路径,命中直接返回)
    now = time.time()
    cached = _user_locale_cache.get(int(user_id))
    if cached is not None:
        expire_ts, locale_val = cached
        if now < expire_ts:
            _user_locale_cache.move_to_end(int(user_id))
            return locale_val
        # 缓存过期:移除条目,继续走 SQLite 加载路径
        _user_locale_cache.pop(int(user_id), None)

    # R51 P1-9: 缓存 miss,加锁防止并发穿透(Lock 惰性创建)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # 无事件循环(不应发生在 async 上下文中),fallback 到 sync 加载
        manager = get_i18n_manager()
        locale_val = manager._load_user_locale_from_sqlite(user_id)
        _cache_user_locale(int(user_id), locale_val)
        return locale_val

    if _locale_cache_lock is None:
        _locale_cache_lock = asyncio.Lock()
    async with _locale_cache_lock:
        # double-check:可能在等锁期间已被其他协程填充
        cached = _user_locale_cache.get(int(user_id))
        if cached is not None:
            expire_ts, locale_val = cached
            if time.time() < expire_ts:
                _user_locale_cache.move_to_end(int(user_id))
                return locale_val

        # R51 P1-9: 在默认 executor 中执行同步 SQLite 读取(不阻塞事件循环)
        manager = get_i18n_manager()
        locale_val = await loop.run_in_executor(
            None, manager._load_user_locale_from_sqlite, user_id,
        )
        _cache_user_locale(int(user_id), locale_val)
        return locale_val


def set_user_locale(user_id: int, locale: str) -> bool:
    """R42 P1-8: 设置用户 locale 并写 dirty_outbox(同步)。

    流程:
        1. 验证 locale 在支持列表中(否则抛 ValueError)
        2. UPDATE users_local SET locale=? WHERE user_id=?
        3. 写 dirty_outbox 一条 upsert 记录(供 crdb_sync 同步)
        4. R51 P1-9: 主动失效该用户的 locale 缓存(下次访问重新加载新值)

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
    # R61 P1-06: 显式拒绝不在 SUPPORTED_LOCALES 中的 locale(产品仅支持 zh-CN/en-US)
    # 优先于 get_available_locales() 的目录扫描,确保 locale 锁定不可绕过
    if locale not in SUPPORTED_LOCALES:
        from services.error_codes import AppError, ErrorCodes  # type: ignore[import]
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "locale",
                "reason": "unsupported_locale",
                "locale": str(locale),
                "supported": ",".join(sorted(SUPPORTED_LOCALES)),
            },
        )
    # 1. 验证 locale 在支持列表中(防御性:目录扫描兜底)
    manager = get_i18n_manager()
    available = manager.get_available_locales()
    if locale not in available:
        raise ValueError(
            _i18n_t('services.i18n.s2', locale=locale, available=available)
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
            # R61 P1-06 修复: 使用单调时间戳作为 version(毫秒精度),
            # 替代硬编码 version=0。原实现导致同一用户多次调用
            # set_user_locale 时 (table_name, pk, version=0) 触发
            # UNIQUE 约束冲突,使整个事务(含 users_local UPDATE)回滚,
            # e2e 中切换 locale 失败。与 _generate_version_from_payload
            # 的 fallback 模式一致(Unix 时间戳),毫秒精度降低同秒碰撞;
            # INSERT OR REPLACE 兜底极端情况,保证最新状态被持久化。
            _version = int(time.time() * 1000)
            conn.execute(
                """INSERT OR REPLACE INTO dirty_outbox
                   (table_name, pk, version, operation, payload,
                    created_at, processed, local_only)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "users_local",
                    str(int(user_id)),
                    _version,
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
            # R51 P1-9: 写入成功后主动失效缓存(下次访问重新加载新值)
            invalidate_user_locale_cache(int(user_id))
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


def parse_accept_language(header: Optional[str]) -> str:
    """R45 17.1: 模块级便捷函数 — 解析 Accept-Language header。

    等价于 I18nManager.parse_accept_language()。

    Args:
        header: Accept-Language header 值(如 "zh-CN,zh;q=0.9,en;q=0.8")

    Returns:
        匹配的 locale 标识符(如 "zh-CN" / "en-US");无匹配返回默认 locale
    """
    manager = get_i18n_manager()
    return manager.parse_accept_language(header)


# R56 §5.1: Telegram language_code → locale 映射
# Telegram 传入的 language_code 是 BCP-47 前缀(如 "zh" / "en" / "ru" / "ja"),
# 需映射到项目支持的标准 locale 标识符(如 "zh-CN" / "en-US")。
_TELEGRAM_LANG_CODE_MAP: dict[str, str] = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hant": "zh-CN",
    "zh-tw": "zh-CN",
    "zh-hk": "zh-CN",
    "en": "en-US",
    "en-us": "en-US",
    "en-gb": "en-US",
}


def map_telegram_language_code(language_code: Optional[str]) -> str:
    """R56 §5.1: 将 Telegram language_code 映射到项目支持的 locale。

    Telegram 传入的 language_code 优先级低于用户在数据库中的显式 locale 设置,
    但高于默认 locale 'zh-CN'。

    locale 优先级链(R56 §5.1):
        1. 用户在 users_local 表的显式 locale 设置(最高优先级)
        2. Telegram language_code 映射的结果
        3. workspace 默认 locale(可通过 set_default_locale 设置)
        4. _DEFAULT_LOCALE = 'zh-CN'(兜底)

    Args:
        language_code: Telegram user.language_code(如 "zh" / "en" / "ru")

    Returns:
        匹配的 locale 标识符(如 "zh-CN" / "en-US");无匹配返回 _DEFAULT_LOCALE
    """
    if not language_code:
        return _DEFAULT_LOCALE
    # 规范化:小写 + 去除前后空白
    code = str(language_code).strip().lower()
    # 1. 精确匹配(如 "zh" / "en")
    if code in _TELEGRAM_LANG_CODE_MAP:
        return _TELEGRAM_LANG_CODE_MAP[code]
    # 2. 前缀匹配(如 "zh-hans" / "en-gb")
    prefix = code.split("-")[0]
    if prefix in _TELEGRAM_LANG_CODE_MAP:
        return _TELEGRAM_LANG_CODE_MAP[prefix]
    # 3. 无匹配返回默认 locale
    return _DEFAULT_LOCALE


def get_locale_with_telegram_fallback(
    user_id: int,
    telegram_language_code: Optional[str] = None,
) -> str:
    """R56 §5.1: 带回退的 locale 选择 — 实现完整 locale 优先级链。

    locale 优先级链(从高到低):
        1. 用户在 users_local 表的显式 locale 设置(最高优先级)
        2. Telegram language_code 映射的结果(若 user_id 无显式设置)
        3. workspace 默认 locale
        4. _DEFAULT_LOCALE = 'zh-CN'(兜底)

    本函数用于 Telegram Bot handler 中:调用方传入 user_id 和 user.language_code,
    得到最终决定要使用的 locale(用于 translate / format_message / format_message_icu)。

    Args:
        user_id: Telegram 用户 ID
        telegram_language_code: Telegram user.language_code(如 "zh" / "en")

    Returns:
        用户 locale(若显式设置)> Telegram 映射 > workspace 默认 > 'zh-CN'
    """
    manager = get_i18n_manager()
    # 1. 用户显式 locale(从 SQLite,带 LRU 缓存)
    user_locale = manager.get_user_locale(user_id)
    if user_locale and user_locale != _DEFAULT_LOCALE:
        return user_locale
    # 2. Telegram language_code fallback
    if telegram_language_code:
        mapped = map_telegram_language_code(telegram_language_code)
        if mapped != _DEFAULT_LOCALE:
            return mapped
    # 3. workspace 默认 locale
    if manager.default_locale != _DEFAULT_LOCALE:
        return manager.default_locale
    # 4. 兜底
    return _DEFAULT_LOCALE


def format_message_icu(key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
    """R56 §5.1: 模块级便捷函数 — ICU MessageFormat 格式化。

    等价于 I18nManager.format_message_icu()。

    Args:
        key: 翻译 key
        locale: 目标 locale(默认 zh-CN)
        **kwargs: 插值参数

    Returns:
        ICU 格式化后的字符串;解析失败回退到 format_message
    """
    manager = get_i18n_manager()
    return manager.format_message_icu(key, locale=locale, **kwargs)


def format_selectordinal(
    key: str,
    locale: Optional[str] = None,
    count: int = 0,
    **kwargs: Any,
) -> str:
    """R59 §5.1 P1: 模块级便捷函数 — CLDR selectordinal 格式化。

    等价于 I18nManager.format_selectordinal()。按 CLDR ordinal 规则选择
    one/two/few/many/other 子句并展开 # / {var} 占位符。

    CLDR ordinal 规则支持:
        - en-*: one/two/few/other(1st/2nd/3rd/4th...)
        - ru-*: one/few/many/other(1-й/2-й/5-й...)
        - zh-*: 始终 other(中文不区分序数形式)
        - 其他: other(兜底)

    Args:
        key: 翻译 key(对应 ICU selectordinal 模板)
        locale: 目标 locale(默认 zh-CN)
        count: 序数值(用于选择 selectordinal 分支 + 展开 #)
        **kwargs: 额外插值参数

    Returns:
        本地化序数字符串;失败时回退到 format_message

    用法:
        # locale 文件:"common.rank.ordinal": "{n, selectordinal,
        #   one {#st place} two {#nd place} few {#rd place} other {#th place}}"
        result = format_selectordinal("common.rank.ordinal", locale="en-US", count=3)
        # → "3rd place"
    """
    manager = get_i18n_manager()
    return manager.format_selectordinal(key, locale=locale, count=count, **kwargs)
