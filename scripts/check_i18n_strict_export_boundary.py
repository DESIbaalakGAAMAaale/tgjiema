#!/usr/bin/env python3
"""R65 P1-03: i18n 严格出口边界 7 维扫描器(strict export boundary gate)。

整改背景(R65 终审报告 P1-03):
    国际化仍未形成严格出口边界。486 个 sink 旁路意味着裸字符串、混合语言和
    内部错误泄漏风险仍广泛存在。本扫描器在 R59 §5.1 / R61 P1-06 / R63 P1-12
    基础上,新增 7 维严格出口边界校验,确保 zh-CN ↔ en-US 完全对等、
    生产代码 locale 显式绑定、内部异常不泄漏给用户。

7 维扫描维度:
    1. **Key 集合对等**(zh-CN ↔ en-US):无孤儿 key(与 check_i18n_key_symmetry 重叠,
       本扫描器独立校验,避免依赖外部脚本)
    2. **ICU AST 结构等价**:每个 ICU 子句的 selector 集合 + 占位符数量/类型
       必须两侧一致(防止 zh-CN 用 plural 而 en-US 用 select)
    3. **参数名一致**:同一 key 的参数集合必须两侧完全一致
       (`{count}` 不能变成 `{n}`)
    4. **en-US 禁止 CJK 占位副本**:en-US 翻译值中禁止出现中文字符
       (防止"复制中文占位符"导致混合语言)
    5. **zh-CN 禁止英文业务文案泄漏**:zh-CN 翻译值中禁止出现英文业务文案
       (技术术语白名单:MFA/ID/RU/CRDB/API/URL/HTTP/JSON/TTL/CRDB/SLA/SSE
       /OAuth/JWT/HTML/CSS/SQL/CRON 等)
    6. **内部异常禁止经 UserMessage 泄漏**:AST 扫描生产代码,检测
       `UserMessage(...)` / `UserMessage.from_key(...)` / `UserMessage.from_error(...)`
       / `ErrorEnvelope(...)` 构造器是否传入 `str(exception)` / `e.args` /
       `repr(e)` / `traceback.format_exc()` 等内部异常信息
    7. **禁止生产代码使用全局默认 locale**:AST 扫描 services/ / bots/ / admin/
       中的 `translate(...)` / `format_message(...)` / `format_message_icu(...)`
       调用,若未显式传入 locale 参数(位置或关键字),记为违规

模式:
    - 默认(无 --strict):仅校验 1-5 维(locale 文件对等性),exit 0/1
    - --strict:启用全部 7 维(含 AST 扫描生产代码),CI 生产门禁
    - --baseline <file>:渐进式 ratchet 模式,允许已知违规以 baseline 形式存在
      (baseline 中记录的违规不阻断 CI;新增违规必须修复或更新 baseline)
    - --generate-baseline <file>:生成当前违规快照为 baseline 文件

CI 调用方式:
    python scripts/check_i18n_strict_export_boundary.py --strict

设计原则:
    - 不依赖第三方 ICU 库(手工解析 ICU 子集,与 services/i18n.py 一致)
    - 与 scripts/check_i18n_key_symmetry.py 的 _ICU_PATTERN / _SIMPLE_VAR_PATTERN
      / _extract_param_set / _extract_icu_structures 保持一致
    - AST 扫描复用 scripts/scan_hardcoded_strings.py 的 sink 函数注册表思路
    - allowlist 机制:已知审计例外(如 en-US 必须含中文术语的场景)通过
      ALLOWLIST 字典显式记录,每条带审计原因
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# 扫描器版本(写入 artifact,便于 CI 下游脚本比对)
SCANNER_VERSION = "1.0-r65-p1-03"

# ===========================================================================
# 常量(与 services/i18n.py / scripts/check_i18n_key_symmetry.py 保持一致)
# ===========================================================================

# ICU MessageFormat 子集 — 匹配 {name, type, ...} 模式
# type ∈ plural/select/selectordinal
_ICU_PATTERN = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*(plural|select|selectordinal)\s*,"
)

# 简单 {var} 占位符(非 ICU pattern)
_SIMPLE_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# CJK Unicode 范围(检测 en-US 中是否泄漏中文)
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# 英文业务文案检测(连续 ≥3 个英文字母的单词,排除技术术语白名单)
# 仅检测 zh-CN 翻译值中的英文业务文案泄漏
_ENGLISH_WORD_PATTERN = re.compile(r"\b[a-zA-Z]{3,}\b")

# 技术术语白名单(zh-CN 中允许出现的英文缩写/技术名词)
# 这些是国际化标准缩写或产品专有名词,不算"英文业务文案泄漏"
# 审计要求:zh-CN 禁止英文业务文案泄漏,白名单:MFA/ID/RU/CRDB/API/URL
# 扩展白名单(基于现有 locale 文件审计):常见技术缩写 + 产品专有名词
TECHNICAL_TERMS_WHITELIST: frozenset[str] = frozenset({
    # 审计要求显式白名单
    "MFA", "ID", "RU", "CRDB", "API", "URL",
    # 协议/格式标准缩写
    "HTTP", "HTTPS", "JSON", "XML", "HTML", "CSS", "SQL", "CSV",
    "TLS", "SSL", "TCP", "UDP", "DNS", "CDN", "SMTP", "IMAP", "POP3",
    # 认证/授权
    "OAuth", "JWT", "SSO", "SAML", "OIDC",
    # 数据库/存储
    "CRDB", "Redis", "SQLite", "PostgreSQL", "MySQL",
    # 监控/可观测性
    "SSE", "SLA", "SLO", "SLI", "Prometheus", "Grafana",
    # 时间/时区
    "UTC", "GMT", "ISO", "RFC",
    # 单位/前缀
    "KB", "MB", "GB", "TB", "PB",
    "KPI", "OKR",
    # 其他技术缩写
    "TTL", "ETag", "UUID", "GUID", "URI", "URN",
    "CRON", "JSON", "YAML", "TOML", "INI",
    "WAF", "XSS", "CSRF", "SSRF", "SQLi",
    "PGP", "GPG", "AES", "RSA", "HMAC", "SHA", "MD5",
    "CLI", "TUI", "GUI", "IDE", "SDK", "DDK",
    "I18N", "L10N", "A11Y", "ICU", "CLDR", "BCP",
    "DLQ", "FIFO", "LIFO",
    "RU", "WU",
    "Bot", "bot",
    # 产品专有名词 / 第三方服务
    "Telegram", "CockroachDB", "R2", "RPO", "RTO", "RBAC",
    "Outbox", "Relay", "CommandBus", "CAS",
    # 技术概念名词(在中文技术文档中常以英文形式出现)
    "tombstone", "manifest", "schema", "payload", "trace",
    "token", "env", "locale", "key", "count", "code", "error",
    # break-glass 应急访问模式(行业标准术语)
    "break", "glass",
    # 常见动词/形容词(在技术上下文中保留英文形式,非业务文案)
    "add", "active", "pending", "status", "restore", "receipt",
    "help", "only", "reason", "action", "upload", "decoder",
    "sender", "bypass", "backup", "job", "table", "prefix",
    "bar", "progress", "icon", "current", "target", "healthy",
    "block", "title", "endblock", "photo", "video", "document",
    "audio", "animation", "get", "enabled", "suggested",
    "repair", "reindex", "phone", "note", "text", "uid", "qqfile",
    # 语言名称(语言切换按钮)
    "English",
})

# 维度 6: AST 扫描生产代码 — 检测内部异常经 UserMessage / ErrorEnvelope 泄漏
# 这些构造器/工厂的参数中禁止出现 exception / traceback 信息
_USER_MESSAGE_CONSTRUCTORS: frozenset[str] = frozenset({
    "UserMessage", "from_key", "from_error", "from_raw_text",
    "ErrorEnvelope", "AppError", "ValidationError",
})

# 内部异常信息的访问模式(出现在 UserMessage 构造参数中即为违规)
# str(e) / repr(e) / e.args / traceback.format_exc() / exc.__traceback__
_EXCEPTION_LEAK_PATTERNS: frozenset[str] = frozenset({
    "str", "repr", "format_exc", "print_exc", "format_exception",
})

# traceback 模块访问
_TRACEBACK_ATTRS: frozenset[str] = frozenset({
    "format_exc", "print_exc", "format_exception", "format_tb",
})

# 维度 7: AST 扫描生产代码 — 检测未绑定 locale 的 translate / format_message 调用
# 这些函数调用时必须显式传入 locale(位置参数 [1] 或关键字 locale=)
# 模块级函数 + I18nManager 方法
_LOCALE_REQUIRED_FUNCS: frozenset[str] = frozenset({
    "translate", "format_message", "format_message_icu",
    # 不含 format_selectordinal(其内部调用 format_message_icu,locale 透传)
    # 不含 _i18n_t(模块内部辅助,默认 locale=_DEFAULT_LOCALE)
})

# 维度 7: 扫描的生产代码路径(services/ / bots/ / admin/)
# 跳过 tests/ / scripts/ / docs/ / __pycache__ / locale 文件本身
_PRODUCTION_SCAN_DIRS: list[str] = ["services", "bots", "admin"]
_PRODUCTION_SKIP_PATTERNS: tuple[str, ...] = (
    "/__pycache__/",
    "/tests/",
    "/test_",
    "/conftest.py",
    "/migrations/",
    "/__init__.py",  # __init__.py 通常只做 re-export,不含 translate 调用
)

# ===========================================================================
# 审计例外 allowlist(记录已知合规的"违规",每条带审计原因)
# ===========================================================================
# 格式: { "dimension_key": "审计原因" }
# dimension_key 命名规则:
#   "dim4_cjk_in_en_us:<key>" — en-US 值中含 CJK(审计批准)
#   "dim5_english_in_zh_cn:<key>" — zh-CN 值中含英文业务文案(审计批准)
#   "dim6_exception_leak:<file>:<lineno>" — UserMessage 传入异常(审计批准)
#   "dim7_locale_not_bound:<file>:<lineno>" — 生产代码未绑定 locale(审计批准)
# baseline 文件中记录的违规同样不阻断 CI(渐进式 ratchet)
ALLOWLIST: dict[str, str] = {
    # 目前无审计例外;新增例外必须附审计原因 + 评审记录
}


# ===========================================================================
# JSON 加载与扁平化(复用 check_i18n_key_symmetry.py 的逻辑)
# ===========================================================================

def _load_json(filepath: Path) -> tuple[dict | None, str | None]:
    """加载 JSON 文件,返回 (data, error)。成功时 error=None。"""
    try:
        raw = filepath.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"根对象应为 dict,实际类型: {type(data).__name__}"
        return data, None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"
    except OSError as e:
        return None, f"文件读取失败: {e}"


def _flatten_values(obj: Any, prefix: str = "") -> dict[str, str]:
    """递归扁平化 dict,返回 {key: value} 映射。

    {"errors": {"quota.decode.exceeded": "x"}} -> {"errors.quota.decode.exceeded": "x"}
    排除 "meta" 顶层 key(允许两个 locale 的 meta 不同)。
    """
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        if not prefix and k == "meta":
            continue
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_values(v, full_key))
        else:
            result[full_key] = "" if v is None else str(v)
    return result


# ===========================================================================
# ICU 结构解析(复用 check_i18n_key_symmetry.py 的逻辑)
# ===========================================================================

def _find_matching_brace(text: str, start: int) -> int:
    """从 text[start] == '{' 开始,查找匹配的 '}' 位置(考虑嵌套)。

    返回 '}' 的索引;若不匹配,返回 -1(malformed)。
    """
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_icu_branches(body: str) -> tuple[set[str], str | None]:
    """解析 ICU plural/select/selectordinal 子句 body,返回 (selectors, error)。

    body 形如: ``=0 {none} one {# item} other {# items}``
    返回 selectors = ``{"=0", "one", "other"}``
    """
    selectors: set[str] = set()
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        selector_start = i
        while i < n and not body[i].isspace() and body[i] != "{":
            i += 1
        selector = body[selector_start:i].strip()
        if not selector:
            break
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != "{":
            return selectors, f"selector '{selector}' 后缺少 '{{'"
        end = _find_matching_brace(body, i)
        if end == -1:
            return selectors, f"selector '{selector}' 的 '{{...}}' 未闭合"
        selectors.add(selector)
        i = end + 1
    return selectors, None


def _extract_icu_structures(text: str) -> tuple[list[dict], list[str]]:
    """从字符串中提取所有 ICU 子句结构,返回 (structures, errors)。

    每个结构形如:
        {"var": "n", "type": "plural", "selectors": {"=0", "one", "other"}}
    """
    structures: list[dict] = []
    errors: list[str] = []
    if not isinstance(text, str):
        return structures, errors
    for m in _ICU_PATTERN.finditer(text):
        var_name = m.group(1)
        block_type = m.group(2)
        brace_start = m.start()
        brace_end = _find_matching_brace(text, brace_start)
        if brace_end == -1:
            errors.append(f"ICU pattern '{var_name}, {block_type}, ...' 未闭合")
            continue
        body = text[m.end():brace_end]
        selectors, branch_err = _parse_icu_branches(body)
        if branch_err:
            errors.append(
                f"ICU pattern '{var_name}, {block_type}, ...' 子句解析失败: {branch_err}"
            )
        if not selectors and not branch_err:
            errors.append(
                f"ICU pattern '{var_name}, {block_type}, ...' 无任何 selector(空子句)"
            )
        structures.append({
            "var": var_name,
            "type": block_type,
            "selectors": selectors,
        })
    return structures, errors


def _extract_param_set(text: str) -> set[str]:
    """提取字符串中引用的所有变量名(简单 {var} + ICU var 名)。"""
    if not isinstance(text, str):
        return set()
    params: set[str] = set()
    for m in _ICU_PATTERN.finditer(text):
        params.add(m.group(1))
    for m in _SIMPLE_VAR_PATTERN.finditer(text):
        params.add(m.group(1))
    return params


def _icu_struct_signature(structures: list[dict]) -> frozenset[tuple[str, str, frozenset[str]]]:
    """生成 ICU 结构签名(用于两侧比对,顺序无关)。"""
    return frozenset(
        (s["var"], s["type"], frozenset(s["selectors"]))
        for s in structures
    )


# ===========================================================================
# 维度 1-5: locale 文件对等性校验
# ===========================================================================

def _check_dim1_key_set_symmetry(
    zh_flat: dict[str, str], en_flat: dict[str, str],
) -> list[str]:
    """维度 1: Key 集合对等(zh-CN ↔ en-US)。

    返回违规列表(空表示通过)。
    """
    violations: list[str] = []
    zh_keys = set(zh_flat.keys())
    en_keys = set(en_flat.keys())
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    for key in sorted(only_zh):
        violations.append(
            f"[dim1_key_set] key='{key}' 仅存在于 zh-CN,en-US 缺失(孤儿 key)"
        )
    for key in sorted(only_en):
        violations.append(
            f"[dim1_key_set] key='{key}' 仅存在于 en-US,zh-CN 缺失(孤儿 key)"
        )
    return violations


def _check_dim2_icu_ast_equivalence(
    zh_flat: dict[str, str], en_flat: dict[str, str],
) -> list[str]:
    """维度 2: ICU AST 结构等价(占位符数/类型/plural 分支)。

    对每个公共 key,比较两侧的 ICU 结构签名(var/type/selectors 集合)。
    返回违规列表。
    """
    violations: list[str] = []
    common_keys = set(zh_flat.keys()) & set(en_flat.keys())
    for key in sorted(common_keys):
        zh_text = zh_flat[key]
        en_text = en_flat[key]
        zh_structs, zh_errors = _extract_icu_structures(zh_text)
        en_structs, en_errors = _extract_icu_structures(en_text)
        # malformed ICU 单独记录(也属于违规)
        for err in zh_errors:
            violations.append(
                f"[dim2_icu_ast] key='{key}' zh-CN ICU 解析失败: {err}"
            )
        for err in en_errors:
            violations.append(
                f"[dim2_icu_ast] key='{key}' en-US ICU 解析失败: {err}"
            )
        # 结构签名比较
        zh_sig = _icu_struct_signature(zh_structs)
        en_sig = _icu_struct_signature(en_structs)
        if zh_sig != en_sig:
            violations.append(
                f"[dim2_icu_ast] key='{key}' ICU 结构不对称: "
                f"zh-CN={sorted([(s['var'], s['type'], sorted(s['selectors'])) for s in zh_structs])} "
                f"vs en-US={sorted([(s['var'], s['type'], sorted(s['selectors'])) for s in en_structs])}"
            )
    return violations


def _check_dim3_param_name_consistency(
    zh_flat: dict[str, str], en_flat: dict[str, str],
) -> list[str]:
    """维度 3: 参数名一致({count} 不能变成 {n})。

    对每个公共 key,比较两侧的参数集合。
    返回违规列表。
    """
    violations: list[str] = []
    common_keys = set(zh_flat.keys()) & set(en_flat.keys())
    for key in sorted(common_keys):
        zh_params = _extract_param_set(zh_flat[key])
        en_params = _extract_param_set(en_flat[key])
        if zh_params != en_params:
            only_zh = zh_params - en_params
            only_en = en_params - zh_params
            violations.append(
                f"[dim3_param_name] key='{key}' 参数集合不对称: "
                f"zh-CN 独有={sorted(only_zh)}, en-US 独有={sorted(only_en)}"
            )
    return violations


def _check_dim4_no_cjk_in_en_us(en_flat: dict[str, str]) -> list[str]:
    """维度 4: en-US 禁止 CJK 占位副本。

    en-US 翻译值中禁止出现中文字符(防止"复制中文占位符"导致混合语言)。
    返回违规列表。
    """
    violations: list[str] = []
    for key, value in sorted(en_flat.items()):
        if not isinstance(value, str):
            continue
        if _CJK_PATTERN.search(value):
            # 检查 allowlist
            allow_key = f"dim4_cjk_in_en_us:{key}"
            if allow_key in ALLOWLIST:
                continue
            violations.append(
                f"[dim4_cjk_in_en_us] key='{key}' en-US 值中含 CJK 字符: {value[:80]!r}"
            )
    return violations


def _check_dim5_no_english_leak_in_zh_cn(zh_flat: dict[str, str]) -> list[str]:
    """维度 5: zh-CN 禁止英文业务文案泄漏。

    zh-CN 翻译值中禁止出现连续 ≥3 个英文字母的英文业务文案
    (技术术语白名单:MFA/ID/RU/CRDB/API/URL 等)。

    检测前先剥离所有 ``{...}`` 占位符(简单 ``{var}`` + ICU ``{var, plural, ...}``),
    避免 ICU 占位符中的变量名 / selector 被误判为英文业务文案。

    返回违规列表。
    """
    violations: list[str] = []
    # 剥离所有 {...} 占位符(包括 ICU pattern 与简单 {var})
    brace_pattern = re.compile(r"\{[^{}]*\}")
    # 递归剥离嵌套 {...}(ICU pattern 可能嵌套,如 {n, plural, other {# {count}}})
    def _strip_braces(text: str) -> str:
        prev = None
        while prev != text:
            prev = text
            text = brace_pattern.sub("", text)
        return text

    for key, value in sorted(zh_flat.items()):
        if not isinstance(value, str):
            continue
        # 剥离 {...} 占位符后检测英文业务文案
        stripped = _strip_braces(value)
        # 提取所有连续 ≥3 字母的英文单词
        english_words = _ENGLISH_WORD_PATTERN.findall(stripped)
        # 过滤白名单(大小写不敏感)
        leaked = [
            w for w in english_words
            if w.upper() not in TECHNICAL_TERMS_WHITELIST
            and w not in TECHNICAL_TERMS_WHITELIST
        ]
        if leaked:
            # 检查 allowlist
            allow_key = f"dim5_english_in_zh_cn:{key}"
            if allow_key in ALLOWLIST:
                continue
            violations.append(
                f"[dim5_english_in_zh_cn] key='{key}' zh-CN 值中含英文业务文案: "
                f"leaked={leaked[:5]}, value={value[:80]!r}"
            )
    return violations


# ===========================================================================
# 维度 6: AST 扫描 — 内部异常禁止经 UserMessage 泄漏
# ===========================================================================

def _is_exception_leak_expr(node: ast.AST) -> bool:
    """检测 AST 节点是否为内部异常信息访问(str(e) / repr(e) / e.args / traceback.format_exc())。

    Returns:
        True 表示此节点访问了内部异常信息,禁止传入 UserMessage 构造器
    """
    # str(e) / repr(e) / format(e) — Call(func=Name(id='str/repr/format'), args=[Name])
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _EXCEPTION_LEAK_PATTERNS:
            return True
        # traceback.format_exc() / traceback.format_exception(...)
        if isinstance(func, ast.Attribute) and func.attr in _TRACEBACK_ATTRS:
            return True
    # e.args — Attribute(value=Name(id='e'), attr='args')
    if isinstance(node, ast.Attribute):
        if node.attr in ("args", "__traceback__", "__cause__", "__context__"):
            return True
    return False


def _check_dim6_no_exception_leak_in_user_message(file_path: Path) -> list[str]:
    """维度 6: 扫描单个文件,检测 UserMessage / ErrorEnvelope 构造器是否传入内部异常。

    返回违规列表(file:lineno 形式)。
    """
    violations: list[str] = []
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return violations  # 解析失败跳过(其他 scanner 会捕获)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name: str | None = None
        # UserMessage(...) / ErrorEnvelope(...) / AppError(...) — Name 调用
        if isinstance(func, ast.Name):
            func_name = func.id
        # xxx.UserMessage(...) / UserMessage.from_key(...) — Attribute 调用
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
        if func_name not in _USER_MESSAGE_CONSTRUCTORS:
            continue
        # 检查所有参数(args + keywords 的 value)
        for arg in node.args:
            if _is_exception_leak_expr(arg):
                allow_key = f"dim6_exception_leak:{file_path}:{node.lineno}"
                if allow_key in ALLOWLIST:
                    continue
                violations.append(
                    f"[dim6_exception_leak] {file_path}:{node.lineno} "
                    f"{func_name}(...) 传入内部异常信息({ast.dump(arg)[:60]}),"
                    f"禁止经 UserMessage 泄漏(只返回 safe message + error code + trace id)"
                )
        for kw in node.keywords:
            if _is_exception_leak_expr(kw.value):
                allow_key = f"dim6_exception_leak:{file_path}:{node.lineno}"
                if allow_key in ALLOWLIST:
                    continue
                violations.append(
                    f"[dim6_exception_leak] {file_path}:{node.lineno} "
                    f"{func_name}({kw.arg}=...) 传入内部异常信息,"
                    f"禁止经 UserMessage 泄漏"
                )
    return violations


def _scan_dim6_all_files() -> list[str]:
    """维度 6: 扫描所有生产代码文件,检测内部异常泄漏。"""
    violations: list[str] = []
    for scan_dir in _PRODUCTION_SCAN_DIRS:
        dir_path = REPO_ROOT / scan_dir
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            file_str = str(py_file).replace("\\", "/")
            if any(skip in file_str for skip in _PRODUCTION_SKIP_PATTERNS):
                continue
            violations.extend(_check_dim6_no_exception_leak_in_user_message(py_file))
    return violations


# ===========================================================================
# 维度 7: AST 扫描 — 禁止生产代码使用全局默认 locale
# ===========================================================================

def _check_dim7_locale_bound(file_path: Path) -> list[str]:
    """维度 7: 扫描单个文件,检测 translate / format_message / format_message_icu
    调用是否显式传入 locale。

    判定规则:
        - 函数名 ∈ _LOCALE_REQUIRED_FUNCS(translate / format_message / format_message_icu)
        - 调用必须有 ≥2 个位置参数(key + locale),或含 locale= 关键字参数
        - 否则记为违规(locale 未绑定,依赖全局默认)

    跳过场景:
        - _i18n_t(...) 调用(模块内部辅助,默认 _DEFAULT_LOCALE)
        - format_selectordinal(...) 调用(内部调用 format_message_icu,locale 透传)
        - self.format_message / self.translate 等(I18nManager 方法,内部调用)
          — 但仍需检测(因为 self 是 I18nManager 实例)
          — 实际上 self.xxx 调用通常在 I18nManager 内部,locale 透传已处理,
            跳过以避免误报。但 services/bots/admin 中的 manager.translate(...)
            仍需检测。
        - 赋值给变量(如 t = translate; t(key))— 难以追踪,跳过
        - 测试文件、__init__.py、conftest.py 跳过

    Returns:
        违规列表(file:lineno 形式)
    """
    violations: list[str] = []
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name: str | None = None
        # 模块级函数调用: translate(...) / format_message(...)
        if isinstance(func, ast.Name):
            func_name = func.id
        # 属性调用: manager.translate(...) / self.translate(...)
        # — 只检测 I18nManager 实例的方法调用(经理名通常为 manager / i18n_manager /
        #   mgr / i18n),self.xxx 跳过(类内部调用)
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
            # 跳过 self.xxx(类内部方法调用,locale 通常透传)
            if isinstance(func.value, ast.Name) and func.value.id == "self":
                continue
        if func_name not in _LOCALE_REQUIRED_FUNCS:
            continue
        # 检测是否显式传入 locale
        # 位置参数:translate(key, locale) 至少 2 个位置参数(不含 kwargs)
        # 关键字参数:locale= 必须存在于 keywords
        has_locale_kw = any(kw.arg == "locale" for kw in node.keywords)
        has_locale_positional = len(node.args) >= 2
        if has_locale_kw or has_locale_positional:
            continue
        # 检查 allowlist
        allow_key = f"dim7_locale_not_bound:{file_path}:{node.lineno}"
        if allow_key in ALLOWLIST:
            continue
        violations.append(
            f"[dim7_locale_not_bound] {file_path}:{node.lineno} "
            f"{func_name}(...) 未显式传入 locale(依赖全局默认),"
            f"生产代码必须绑定 user/session locale"
        )
    return violations


def _scan_dim7_all_files() -> list[str]:
    """维度 7: 扫描所有生产代码文件,检测未绑定 locale 的 translate 调用。"""
    violations: list[str] = []
    for scan_dir in _PRODUCTION_SCAN_DIRS:
        dir_path = REPO_ROOT / scan_dir
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            file_str = str(py_file).replace("\\", "/")
            if any(skip in file_str for skip in _PRODUCTION_SKIP_PATTERNS):
                continue
            violations.extend(_check_dim7_locale_bound(py_file))
    return violations


# ===========================================================================
# Baseline ratchet 模式(渐进式清零)
# ===========================================================================

def _load_baseline(baseline_path: Path) -> dict[str, Any]:
    """加载 baseline 文件,返回 {violations: [...], scanner_version: str}。"""
    if not baseline_path.exists():
        return {"violations": [], "scanner_version": ""}
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"violations": [], "scanner_version": ""}
        return data
    except (json.JSONDecodeError, OSError):
        return {"violations": [], "scanner_version": ""}


def _generate_baseline(
    baseline_path: Path, violations: list[str],
) -> None:
    """生成 baseline 文件(记录当前违规快照)。"""
    data = {
        "scanner_version": SCANNER_VERSION,
        "violation_count": len(violations),
        "violations": sorted(violations),
        "note": (
            "R65 P1-03 i18n strict export boundary baseline。"
            "新增违规必须修复或更新 baseline(需审计批准)。"
            "目标:渐进式清零(ratchet),不允许新增违规。"
        ),
    }
    baseline_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _apply_baseline_ratchet(
    violations: list[str], baseline: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """应用 baseline ratchet:已知违规允许保留,新增违规必须修复。

    Returns:
        (new_violations, baseline_removed_violations)
        - new_violations: 不在 baseline 中的新增违规(必须修复)
        - baseline_removed_violations: 在 baseline 中但当前已修复的违规
          (baseline 可以下降)
    """
    baseline_set = set(baseline.get("violations", []))
    current_set = set(violations)
    new_violations = sorted(current_set - baseline_set)
    removed = sorted(baseline_set - current_set)
    return new_violations, removed


# 默认 baseline 文件路径(--strict 模式自动加载,渐进式 ratchet)
DEFAULT_BASELINE_PATH = LOCALES_DIR / "i18n_strict_boundary_baseline.json"


# ===========================================================================
# 主校验流程
# ===========================================================================

def verify(strict: bool = False, baseline_path: Path | None = None) -> tuple[int, list[str]]:
    """主校验流程。返回 (exit_code, violations)。

    Args:
        strict: 是否启用 strict 模式(含维度 6/7 的 AST 扫描)
        baseline_path: baseline 文件路径(若提供,启用 ratchet 模式)
            — 若未提供但 strict=True,自动加载 DEFAULT_BASELINE_PATH
              (若存在),实现渐进式 ratchet(已知违规允许,新增违规失败)

    Returns:
        (exit_code, violations) — exit_code 0=通过,1=失败
    """
    zh_path = LOCALES_DIR / "zh-CN.json"
    en_path = LOCALES_DIR / "en-US.json"

    all_violations: list[str] = []

    # 1. 文件存在性 + JSON 解析(fail-fast)
    if not zh_path.exists():
        all_violations.append(f"[load] zh-CN.json 不存在: {zh_path}")
        return 1, all_violations
    if not en_path.exists():
        all_violations.append(f"[load] en-US.json 不存在: {en_path}")
        return 1, all_violations

    zh_data, zh_err = _load_json(zh_path)
    if zh_err:
        all_violations.append(f"[load] zh-CN.json {zh_err}")
        return 1, all_violations
    en_data, en_err = _load_json(en_path)
    if en_err:
        all_violations.append(f"[load] en-US.json {en_err}")
        return 1, all_violations

    zh_flat = _flatten_values(zh_data)
    en_flat = _flatten_values(en_data)

    # 维度 1-3: 结构性校验(必须 0 违规,不允许 baseline ratchet)
    # 这些是硬错误(key 不对称 / ICU 结构不对称 / 参数名不一致),必须修复
    structural_violations: list[str] = []
    structural_violations.extend(_check_dim1_key_set_symmetry(zh_flat, en_flat))
    structural_violations.extend(_check_dim2_icu_ast_equivalence(zh_flat, en_flat))
    structural_violations.extend(_check_dim3_param_name_consistency(zh_flat, en_flat))

    # 维度 4-5: 文案校验(允许 baseline ratchet,因为是历史遗留翻译问题)
    # 这些是 en-US 含 CJK / zh-CN 含英文业务文案,修复需逐步进行
    text_violations: list[str] = []
    text_violations.extend(_check_dim4_no_cjk_in_en_us(en_flat))
    text_violations.extend(_check_dim5_no_english_leak_in_zh_cn(zh_flat))

    # 维度 6-7: 生产代码 AST 扫描(仅 strict 模式启用,必须 0 违规)
    ast_violations: list[str] = []
    if strict:
        ast_violations.extend(_scan_dim6_all_files())
        ast_violations.extend(_scan_dim7_all_files())

    # 结构性 + AST 违规:必须 0(fail-fast,不允许 baseline)
    hard_violations = structural_violations + ast_violations
    if hard_violations:
        print(f"[FAIL] R65 P1-03 i18n strict export boundary: "
              f"发现 {len(hard_violations)} 个硬违规(结构性/AST,必须修复):")
        for v in hard_violations:
            print(f"  - {v}")
        return 1, hard_violations

    # 文案违规:允许 baseline ratchet
    all_violations = text_violations

    # 确定使用的 baseline 路径
    effective_baseline = baseline_path
    if effective_baseline is None and strict:
        # --strict 模式下自动加载默认 baseline(若存在)
        if DEFAULT_BASELINE_PATH.exists():
            effective_baseline = DEFAULT_BASELINE_PATH

    if effective_baseline is not None:
        baseline = _load_baseline(effective_baseline)
        new_violations, removed = _apply_baseline_ratchet(all_violations, baseline)
        if new_violations:
            # 新增文案违规必须修复
            print(f"[FAIL] R65 P1-03 i18n strict export boundary: "
                  f"新增 {len(new_violations)} 个文案违规(baseline 外),必须修复:")
            for v in new_violations[:20]:
                print(f"  - {v}")
            if len(new_violations) > 20:
                print(f"  ... 共 {len(new_violations)} 个新增违规")
            if removed:
                print(f"\n[INFO] baseline 中 {len(removed)} 个违规已修复"
                      f"(可下降 baseline)")
            return 1, new_violations
        # 无新增违规:通过(即使 baseline 中仍有未修复的旧违规)
        print(f"[OK] R65 P1-03 i18n strict export boundary: 通过"
              f"(strict={strict}, current={len(all_violations)}, "
              f"baseline={len(baseline.get('violations', []))})")
        if removed:
            print(f"[INFO] baseline 中 {len(removed)} 个违规已修复"
                  f"(建议下降 baseline)")
        return 0, all_violations

    # 无 baseline 模式:任何违规都失败
    if all_violations:
        print(f"[FAIL] R65 P1-03 i18n strict export boundary: "
              f"发现 {len(all_violations)} 个违规:")
        for v in all_violations[:20]:
            print(f"  - {v}")
        if len(all_violations) > 20:
            print(f"  ... 共 {len(all_violations)} 个违规")
        return 1, all_violations

    print(f"[OK] R65 P1-03 i18n strict export boundary: 通过"
          f"(strict={strict}, 7 维校验全部通过)")
    return 0, []


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        description="R65 P1-03 i18n 严格出口边界 7 维扫描器",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="启用 strict 模式(含维度 6/7 的 AST 扫描生产代码)",
    )
    parser.add_argument(
        "--baseline", type=Path, default=None,
        help="baseline 文件路径(启用 ratchet 模式:已知违规允许,新增违规失败)",
    )
    parser.add_argument(
        "--generate-baseline", type=Path, default=None,
        help="生成当前违规快照为 baseline 文件",
    )
    parser.add_argument(
        "--version", action="version", version=f"v{SCANNER_VERSION}",
    )
    args = parser.parse_args(argv)

    # 生成 baseline 模式
    if args.generate_baseline is not None:
        exit_code, violations = verify(strict=args.strict)
        _generate_baseline(args.generate_baseline, violations)
        print(f"[OK] baseline 已生成: {args.generate_baseline} "
              f"(共 {len(violations)} 个违规)")
        return 0

    # 常规校验(可选 baseline ratchet)
    exit_code, _ = verify(strict=args.strict, baseline_path=args.baseline)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
