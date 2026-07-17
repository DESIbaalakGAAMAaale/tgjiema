#!/usr/bin/env python3
"""R48 P1-c / R58 P1-4 / R59 §5.1 P1: 模块化 i18n 硬编码字符串扫描与逐模块下降门禁(AST sink-based)。

历史:
    R44 6.3:   引入硬编码用户面向中文字符串扫描,baseline 机制(单一数字)。
    R46 P1:    新增 --ratchet 模式(违规数只减不增)。
    R47 P1-d:  改为**模块化 baseline**,逐模块下降门禁,清零目标 0。
               baseline.json 迁移至按模块分别统计。
    R48 P1-c:  baseline 记录 scanner_version + included_paths;scope 变化单独审批;
               新增 --delta(CI base/head 比对)和 --classify(user_visible / log_only);
               --generate-baseline 强制 --reason,非 master 分支强制 --force;
               每个 user_visible 模块清零目标 0,log_only 鼓励减少但可保留。
    R56 §5.1:  绝对门禁改造 — user_visible 必须 0(不允许通过更新 baseline 消除失败)。
    R58 P1-4:  **sink-based AST 扫描**替代正则+CJK 过滤;不依赖字符集;
               `reply_text/send_message/answer/HTTPException/detail` 等 sink
               的字面量无论中英文都必须禁止,除非来自 `translate/ErrorEnvelope`。
    R59 §5.1 P1: **英文 sink 绝对零基线** — 不再按 CJK/英文区分 user_visible/log_only:
               - 所有 Python sink 字面量(reply_text/send_message/HTTPException/flash 等)→ user_visible
               - 所有 HTML 用户可见文本(标签文本 + placeholder/title/aria-label/alt 属性)→ user_visible
               - 仅 logger.*/logging.*/print() 调用保留 log_only 类别(baseline ratchet)
               - HTML 扫描扩展:不再只检测 CJK,所有英文字面量也纳入检测(英文 sink 绝对零基线)

R59 §5.1 P1 整改要点:
    - 旧版(R58)classify 用 CJK 区分 user_visible/log_only,导致英文 sink 可走 log_only baseline 吸收,
      违反"用户可见 sink 绝对零"原则。
    - 新版(R59)classify 完全按 sink 类型区分:
        * Python sink 调用(任意语言) → user_visible(绝对门禁,必须 0)
        * HTML 用户可见文本/属性(任意语言) → user_visible(绝对门禁,必须 0)
        * logger.*/logging.*/print() 调用 → log_only(baseline ratchet,只允许非增加)
    - HTML 扫描扩展:检测 placeholder/title/aria-label/alt 等用户可见属性;
      检测 >文本< 模式时不再要求 CJK,所有含字母数字的文本均纳入检测。
    - 兼容旧测试:logger ±2 行上下文检测保留(用于 log_only 归类)。

模块划分(贴合真实目录):
    - bots/up_bot.py            (Up)
    - bots/idx_bot.py           (Idx)
    - bots/dsp_bot.py           (Dsp)
    - bots/mon_bot.py           (Mon)
    - bots/admin_bot/           (Admin Bot)
    - admin/                    (admin 后端 Python: admin/*.py)
    - admin/templates/          (Admin Web HTML 模板)
    - admin/static/             (Admin Web 静态资源;当前目录不存在,占位清零门禁)
    - services/                 (服务层;原 R44 未扫描,R47 起纳入)

用法:
    # 模块化检查(CI 默认;任何模块/total 超标 → 退出码 1)
    python scripts/scan_hardcoded_strings.py --check
    python scripts/scan_hardcoded_strings.py          # 等价于 --check

    # CI delta 模式(更精确:比对 git base/head)
    python scripts/scan_hardcoded_strings.py --delta

    # 下降 baseline(修复后更新,只降不升;scope 不变)
    python scripts/scan_hardcoded_strings.py --ratchet

    # 生成/重建 baseline.json(初始基线或 scope 变化时)
    python scripts/scan_hardcoded_strings.py --generate-baseline --reason 'initial baseline'
    python scripts/scan_hardcoded_strings.py --generate-baseline \\
        --reason 'scanner scope change' --allow-scope-change

    # 分类模式:user_visible / log_only
    python scripts/scan_hardcoded_strings.py --classify

    # 查看进度报告
    python scripts/scan_hardcoded_strings.py --report
"""
import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# === 常量 ===

# R59 §5.1 P1: scanner 版本(算法变更:不再按 CJK/英文区分,所有 sink 字面量归 user_visible)
SCANNER_VERSION = "4.0"

# 中文 Unicode 范围(仅用于向后兼容旧测试的 CJK 检测;R59 §5.1 P1 后 classify_finding
# 不再使用此常量做 user_visible/log_only 区分,所有 sink 字面量统一归 user_visible)
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# R58 P1-4: AST sink-based 扫描 — sink 函数名(Python)
# 调用这些方法时,字符串字面量参数被视为用户面向输出
PYTHON_SINK_FUNCS = frozenset({
    # Telegram Bot sendMessage 系列
    'reply_text', 'reply_photo', 'reply_document', 'reply_video', 'reply_audio',
    'reply_animation', 'reply_media_group', 'reply_voice', 'reply_sticker',
    'reply_location', 'reply_venue', 'reply_contact', 'reply_poll',
    'reply_dice', 'reply_chat_action',
    'send_message', 'send_photo', 'send_document', 'send_video', 'send_audio',
    'send_animation', 'send_media_group', 'send_voice', 'send_sticker',
    'send_location', 'send_venue', 'send_contact', 'send_poll',
    'send_chat_action',
    # Callback/inline answer
    'answer_callback_query', 'answer_inline_query',
    'answer_shipping_query', 'answer_pre_checkout_query',
    # Edit message
    'edit_message_text', 'edit_message_caption', 'edit_message_reply_markup',
    # Web framework flash
    'flash',
    # FastAPI HTTPException (with detail= kwarg)
    'HTTPException',
})

# R58 P1-4: sink 关键字参数名 — 字符串字面量传入这些关键字时视为用户面向
PYTHON_SINK_KEYWORDS = frozenset({
    'detail', 'text', 'message', 'caption', 'description',
})

# R58 P1-4: 豁免函数名 — 字符串字面量来自这些函数调用时不算违规
# (说明:字面量已经经过 i18n 查找或结构化错误协议封装)
PYTHON_EXEMPT_FUNCS = frozenset({
    # i18n 查找
    'translate', '_i18n_t', 'format_message_icu', 'format_plural', 'format_error_response',
    'get_i18n_manager', 't', '_', 'gettext', 'ngettext', 'pgettext',
    # 结构化错误协议(替代裸字符串)
    'AppError', 'ErrorEnvelope', 'ValidationError',
})

# R58 P1-4: logger 属性名 — 这些方法调用整体跳过(日志输出不算 user_visible)
LOGGER_ATTRS = frozenset({
    'debug', 'info', 'warning', 'warn', 'error', 'critical', 'exception',
    'log', 'trace',
})

# R58 P1-4: 行级 sink 调用检测正则(用于 classify_finding)
# 匹配 reply_text/send_message/HTTPException 等方法调用
SINK_CALL_LINE_RE = re.compile(
    r'\b(?:' + '|'.join(sorted(PYTHON_SINK_FUNCS)) + r')\s*\('
)
SINK_KWARG_LINE_RE = re.compile(r'\bdetail\s*=')

# 兼容旧测试引用(已弃用,保留以避免 import error)
USER_FACING_PATTERNS = [
    re.compile(r'(?:reply_text|answer|reply_photo|reply_document|send_message)\s*\(\s*f?["\']([^"\']*)["\']'),
    re.compile(r'detail\s*=\s*f?["\']([^"\']*)["\']'),
    re.compile(r'HTTPException\s*\([^)]*detail\s*=\s*f?["\']([^"\']*)["\']'),
    re.compile(r'f["\']([^"\']*[\u4e00-\u9fff][^"\']*)["\']'),
]

# 白名单目录/文件
SKIP_PATTERNS = [
    'tests/',
    'docs/',
    'scripts/',
    '__pycache__',
    '.git/',
    'node_modules/',
    '.claude/',  # Claude IDE 工作目录(worktrees/sessions),非项目代码
]

# 模块基线文件
MODULE_BASELINE_FILE = Path(__file__).parent.parent / 'locales' / 'baseline.json'

# 原 R44 基线(历史参考:bots/+admin 范围)
ORIGINAL_R44_BASELINE = 954

# 模块定义(顺序即报告输出顺序);清零目标均为 0
MODULE_KEYS = [
    'bots/up_bot.py',
    'bots/idx_bot.py',
    'bots/dsp_bot.py',
    'bots/mon_bot.py',
    'bots/admin_bot/',
    'admin/',
    'admin/templates/',
    'admin/static/',
    'services/',
]

# R48 P1-c: 当前 scanner 纳入扫描的路径清单(scope 变化需 --allow-scope-change)
# 与 MODULE_KEYS 一致,作为 baseline.json 的 included_paths 字段
INCLUDED_PATHS = list(MODULE_KEYS)

# R48 P1-c: logger 调用模式(用于 --classify 区分 log_only)
LOGGER_CALL_RE = re.compile(r'\b(?:logger|logging|log)\.[a-zA-Z_]+\s*\(')
PRINT_CALL_RE = re.compile(r'\bprint\s*\(')

# R59 §5.1 P1: HTML 用户可见属性名(属性值硬编码时必须接入 i18n)
# 这些属性的值会被屏幕阅读器读出或直接展示给用户(WCAG 2.2 AA)
HTML_VISIBLE_ATTRS = frozenset({
    'placeholder',   # input/textarea 占位符(用户可见提示)
    'title',         # 元素标题(tooltip + 屏幕reader)
    'aria-label',    # 屏幕阅读器标签(无障碍)
    'alt',           # img 替代文本(图片不可见时展示)
    'aria-describedby',  # 屏幕阅读器描述引用
    'aria-roledescription',  # 屏幕阅读器角色描述
    'aria-placeholder',  # ARIA 占位符
})

# R59 §5.1 P1: HTML 标签之间文本的正则(捕获 > 和 < 之间的非空文本)
_HTML_TEXT_RE = re.compile(r'>([^<>]+)<')

# R59 §5.1 P1: HTML 属性值正则(双引号 + 单引号均支持)
# 形如 aria-label="看板" 或 aria-label='看板'
_HTML_ATTR_DOUBLE_RE = re.compile(
    r'\b(' + '|'.join(sorted(HTML_VISIBLE_ATTRS)) + r')\s*=\s*"([^"]*)"',
    re.IGNORECASE,
)
_HTML_ATTR_SINGLE_RE = re.compile(
    r'\b(' + '|'.join(sorted(HTML_VISIBLE_ATTRS)) + r')\s*=\s*\'([^\']*)\'',
    re.IGNORECASE,
)

# R59 §5.1 P1: <script>/<style> 块整体跳过(CSS/JS 代码不算用户可见文本)
_SCRIPT_STYLE_BLOCK_RE = re.compile(
    r'<(script|style)\b[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)

# R59 §5.1 P1: HTML 注释整体跳过(可能是跨行注释)
_HTML_COMMENT_RE = re.compile(
    r'<!--.*?-->',
    re.DOTALL,
)

# R59 §5.1 P1: 内部协议字符串豁免 — bot 间机器通信协议(非用户可见)
# 这些字符串虽是 send_message 的参数,但实际是 bot 间协议命令,非自然语言文本:
#   - "RELAY_ERROR:user_id:code:reason"  — 中继错误通知(大写协议名 + : 分隔)
#   - "/start param"                      — Telegram deep link 启动命令
#   - "EXTERNAL_DONE:user_id:code"        — 外部解码完成通知
#   - "RELAY_DELIVER:bot:code"            — 中继投递命令
#   - "RELAY_RENEW:bot:code"              — 中继续期命令
# 匹配规则:
#   1. 以大写协议名 + `:` 开头(如 RELAY_ERROR: / EXTERNAL_DONE:)
#   2. 以 `/` + 小写字母开头(Telegram bot 命令,如 /start /help)
# 这些字符串无自然语言文本,不应纳入 user_visible 门禁
_INTERNAL_PROTOCOL_RE = re.compile(
    r'^(?:[A-Z][A-Z_]+:[^:]*:|/[a-z_]+\s)',
)


def _is_internal_protocol(text: str) -> bool:
    """R59 §5.1 P1: 检测字符串是否为内部协议命令(非用户可见)。

    匹配模式:
        - "RELAY_ERROR:123:abc:reason" — 大写协议名 + : 分隔参数
        - "/start param"               — Telegram bot 命令
        - "EXTERNAL_DONE:123:abc"      — 同上

    这些是 bot 间机器通信协议,非用户可见文本,应豁免 user_visible 门禁。

    Args:
        text: 字符串字面量(已展开 f-string 占位符为 {})

    Returns:
        True 表示是内部协议字符串,应跳过;False 表示需继续检测
    """
    return bool(_INTERNAL_PROTOCOL_RE.match(text))


# === 工具函数 ===

def is_skipped(path: Path) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern in str(path):
            return True
    return False


def _strip_string_literals(line: str) -> str:
    """移除字符串字面量,以便准确计算括号深度。

    避免字符串中的括号影响多行调用跟踪。
    """
    return re.sub(r'f?["\'][^"\']*["\']', '""', line)


# === R58 P1-4: AST sink-based 扫描 ===

def _is_logger_or_print_call(node: ast.Call) -> bool:
    """检查 Call 节点是否是 logger.*/logging.*/print() 调用(整体跳过)。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in LOGGER_ATTRS:
            # logger.info(...), logging.warning(...)
            return True
    if isinstance(func, ast.Name):
        if func.id == 'print':
            return True
    return False


def _get_call_name(node: ast.Call) -> str:
    """获取 Call 节点的函数名(用于 sink/exempt 判定)。

    支持形式:
        reply_text(...)             → "reply_text"
        obj.reply_text(...)         → "reply_text"
        HTTPException(...)          → "HTTPException"
        module.func(...)            → "func"
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ''


def _is_sink_call(node: ast.Call) -> bool:
    """R58 P1-4: 检查 Call 节点是否是用户面向 sink 调用。

    判定规则(按优先级):
    1. 豁免函数(translate/_i18n_t/AppError/ErrorEnvelope 等)→ 永远不是 sink
       (即使含 text=/detail= 等 sink 关键字参数,如 _i18n_t('key', text=text))
    2. logger.*/logging.*/print() → 永远不是 sink
    3. 函数名 ∈ PYTHON_SINK_FUNCS(reply_text/send_message/HTTPException/flash 等)→ sink
    4. 含 PYTHON_SINK_KEYWORDS(detail/text/message/...) 关键字参数 → sink
    """
    func = node.func
    name = _get_call_name(node)

    # 豁免函数优先(translate/_i18n_t 等永远不是 sink,即使有 sink kwargs)
    if name in PYTHON_EXEMPT_FUNCS:
        return False

    # logger.*/logging.*/print() 整体跳过(永远不是 sink)
    if _is_logger_or_print_call(node):
        return False

    # 直接 sink 调用
    if name in PYTHON_SINK_FUNCS:
        return True

    # 含 sink 关键字参数(detail="..."/text="..."/message="..."/...)
    for kw in node.keywords:
        if kw.arg in PYTHON_SINK_KEYWORDS:
            return True

    return False


def _is_exempt_call(node: ast.Call) -> bool:
    """R58 P1-4: 检查 Call 节点是否是豁免函数(translate/ErrorEnvelope/AppError 等)。

    字符串字面量来自这些函数调用时不算违规(已经过 i18n 查找或结构化错误封装)。
    """
    name = _get_call_name(node)
    return name in PYTHON_EXEMPT_FUNCS


def _extract_string_literal(node: ast.AST) -> str | None:
    """从 AST 节点抽取字符串字面量(返回 None 表示非字符串)。

    支持:
        - 普通字符串常量: "hello" → "hello"
        - f-string: f"hello {name}" → "hello {}"(占位符替换)
        - f-string 纯字面量: f"hello world" → "hello world"
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string
        parts: list[str] = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
            elif isinstance(val, ast.FormattedValue):
                # f"...{expr}..." 插值占位符
                parts.append('{}')
        return ''.join(parts) if parts else None
    return None


def _describe_sink(node: ast.Call, kwarg_name: str = '') -> str:
    """生成 sink 描述(用于 findings 的 pattern_type 字段)。"""
    name = _get_call_name(node)
    if kwarg_name:
        return f'sink:{name}.{kwarg_name}'
    return f'sink:{name}'


# === 文件扫描 ===

def scan_python_content(content: str) -> list[tuple[int, str, str]]:
    """R58 P1-4: AST sink-based 扫描 — 检测 sink 调用中的字符串字面量。

    替代旧版正则+CJK 过滤;不再依赖字符集,所有 sink 中的字面量都纳入检测:
        - reply_text("...")/send_message("...")/answer("...")/flash("...") 等
        - HTTPException(status_code, detail="...")/HTTPException(500, "...")
        - 任何含 detail=/text=/message=/caption=/description= 关键字的调用

    豁免(不算违规):
        - logger.*/logging.*/print() 调用(整体跳过)
        - 字符串字面量来自 translate()/format_message_icu()/AppError/ErrorEnvelope 等

    Args:
        content: Python 源代码字符串

    Returns:
        [(line_no, pattern_type, content), ...] 字面量内容(截断到 80 字符)
    """
    findings: list[tuple[int, str, str]] = []

    # 解析 AST(语法错误时返回空)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return findings

    # 遍历所有 Call 节点
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # 跳过 logger./logging./print() 调用
        if _is_logger_or_print_call(node):
            continue

        # 检查是否是 sink 调用
        if not _is_sink_call(node):
            continue

        # 检查位置参数中的字符串字面量
        for arg in node.args:
            # 豁免:参数本身是 translate()/format_message_icu()/AppError/ErrorEnvelope 等调用
            if isinstance(arg, ast.Call) and _is_exempt_call(arg):
                continue
            text = _extract_string_literal(arg)
            if text is None:
                continue
            # 跳过空/纯空白字符串(非用户可见)
            if not text.strip():
                continue
            # R59 §5.1 P1: 跳过内部协议字符串(bot 间机器通信,非用户可见)
            # 例如 "RELAY_ERROR:{}:{}:{}" / "/start {}" / "EXTERNAL_DONE:{}:{}"
            if _is_internal_protocol(text):
                continue
            findings.append((node.lineno, _describe_sink(node), text[:80]))

        # 检查关键字参数中的字符串字面量(detail="..." 等)
        for kw in node.keywords:
            # *args/**kwargs 的 kwarg.arg 为 None,跳过
            if kw.arg is None:
                continue
            # 仅当关键字名 ∈ PYTHON_SINK_KEYWORDS 或函数本身是 sink 时才检查
            if kw.arg not in PYTHON_SINK_KEYWORDS and _get_call_name(node) not in PYTHON_SINK_FUNCS:
                continue
            # 豁免:值是 exempt 调用
            if isinstance(kw.value, ast.Call) and _is_exempt_call(kw.value):
                continue
            text = _extract_string_literal(kw.value)
            if text is None:
                continue
            if not text.strip():
                continue
            # R59 §5.1 P1: 跳过内部协议字符串(bot 间机器通信,非用户可见)
            if _is_internal_protocol(text):
                continue
            findings.append((node.lineno, _describe_sink(node, kw.arg), text[:80]))

    return findings


def scan_python_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 Python 文件(读文件后委托给 scan_python_content)。"""
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return []
    return scan_python_content(content)


def scan_html_content(content: str) -> list[tuple[int, str, str]]:
    """R59 §5.1 P1: 扫描 HTML 中的硬编码用户可见文本(不区分中英文)。

    检测范围:
        - HTML 标签之间的文本(`>文本<`):任何含字母数字的文本均纳入检测
          (旧版 R58 仅检测 CJK;R59 §5.1 P1 扩展到英文 — "英文 sink 绝对零基线")
        - HTML 属性值:`placeholder` / `title` / `aria-label` / `alt` 等
          用户可见属性的硬编码值(无论中英文)

    排除:
        - HTML 注释 `<!-- -->` (整体跳过,可能跨行)
        - Jinja2 表达式 `{{ }}` (模板变量已通过 `t()` 路由到 i18n)
        - Jinja2 控制语句 `{% %}` (条件/循环,非用户文本)
        - `<script>` / `<style>` 块内容(CSS/JS 代码不算用户文本)
        - 纯标点/符号文本(如 `:` `|` `>` 等,无字母数字)

    Args:
        content: HTML 源代码字符串

    Returns:
        [(line_no, pattern_type, content), ...] 字面量内容(截断到 80 字符)
        pattern_type 形如 "html_text" 或 "html_attr:aria-label"
    """
    findings: list[tuple[int, str, str]] = []

    # 预处理:移除 <script>...</script> 和 <style>...</style> 块
    # 用换行替换被移除的内容以保留行号(让 finding 行号对齐原始文件)
    def _preserve_lines(match: re.Match) -> str:
        return '\n' * match.group(0).count('\n')

    cleaned_content = _SCRIPT_STYLE_BLOCK_RE.sub(_preserve_lines, content)
    # 移除 HTML 注释(可能跨行),同样保留行号
    cleaned_content = _HTML_COMMENT_RE.sub(_preserve_lines, cleaned_content)

    for i, line in enumerate(cleaned_content.splitlines(), 1):
        # 移除 Jinja2 {{ }} 表达式(已通过 t() 路由到 i18n)
        # 使用非贪婪 .*? 匹配,可处理表达式内含 {} 字典字面量的情况
        # (如 {{ x.get('y', {}).get('z', 0) }} — 旧版 [^}]* 会提前停止)
        cleaned = re.sub(r'\{\{.*?\}\}', '', line)
        # 移除 Jinja2 {% %} 控制语句(条件/循环,非用户文本)
        cleaned = re.sub(r'\{%.*?%\}', '', cleaned)

        # 检测 >文本< 模式(标签之间的文本)
        for match in _HTML_TEXT_RE.finditer(cleaned):
            text = match.group(1).strip()
            # 跳过纯空白/纯标点(无字母数字)
            if text and any(c.isalnum() for c in text):
                findings.append((i, 'html_text', text[:80]))

        # 检测 HTML 属性值(双引号)
        for match in _HTML_ATTR_DOUBLE_RE.finditer(cleaned):
            attr_name = match.group(1).lower()
            attr_value = match.group(2).strip()
            # 跳过纯空白/纯标点(无字母数字)
            if attr_value and any(c.isalnum() for c in attr_value):
                findings.append((i, f'html_attr:{attr_name}', attr_value[:80]))

        # 检测 HTML 属性值(单引号)
        for match in _HTML_ATTR_SINGLE_RE.finditer(cleaned):
            attr_name = match.group(1).lower()
            attr_value = match.group(2).strip()
            if attr_value and any(c.isalnum() for c in attr_value):
                findings.append((i, f'html_attr:{attr_name}', attr_value[:80]))

    return findings


def scan_html_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 HTML 文件(读文件后委托给 scan_html_content)。"""
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return []
    return scan_html_content(content)


def _violation_key(file: str, content: str) -> str:
    """生成违规唯一键(基于文件路径和内容,不依赖行号)。

    路径分隔符归一化为 /,确保 Windows 生成的 baseline 在 Linux CI 中可用。
    """
    return f"{file.replace(chr(92), '/')}::{content}"


def _module_for_file(rel: str) -> str | None:
    """将相对文件路径(使用 / 分隔)映射到所属模块键。

    返回 None 表示未归属任何模块(应在扫描范围之外或无违规)。
    admin/(整个目录)按子目录细分为 admin/ + admin/templates/ + admin/static/,
    以便 Admin Web HTML 模板独立门禁、逐模块清零。
    """
    if rel == 'bots/up_bot.py':
        return 'bots/up_bot.py'
    if rel == 'bots/idx_bot.py':
        return 'bots/idx_bot.py'
    if rel == 'bots/dsp_bot.py':
        return 'bots/dsp_bot.py'
    if rel == 'bots/mon_bot.py':
        return 'bots/mon_bot.py'
    if rel.startswith('bots/admin_bot/'):
        return 'bots/admin_bot/'
    if rel.startswith('admin/templates/'):
        return 'admin/templates/'
    if rel.startswith('admin/static/'):
        return 'admin/static/'
    if rel.startswith('admin/'):
        return 'admin/'
    if rel.startswith('services/'):
        return 'services/'
    return None


def collect_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """收集所有违规,返回 (file, line_no, ptype, content) 列表(file 使用 / 分隔)。

    R47 P1-d: 扫描范围扩展至 services/**/*.py(原 R44 未覆盖)。
    """
    findings = []

    # 扫描 Python 文件
    for pattern in ['bots/**/*.py', 'admin/**/*.py', 'services/**/*.py']:
        for path in root.glob(pattern):
            if is_skipped(path):
                continue
            rel = str(path.relative_to(root)).replace(chr(92), '/')
            for line_no, ptype, content in scan_python_file(path):
                findings.append((rel, line_no, ptype, content))

    # 扫描 HTML 文件
    for path in root.glob('admin/templates/**/*.html'):
        if is_skipped(path):
            continue
        rel = str(path.relative_to(root)).replace(chr(92), '/')
        for line_no, ptype, content in scan_html_file(path):
            findings.append((rel, line_no, ptype, content))

    return findings


def count_by_module(findings) -> dict[str, int]:
    """按模块统计去重后的违规数(以 file::content 为去重键)。

    由于去重键包含文件路径,每个键唯一归属一个模块,
    故各模块计数之和等于全局去重总数。
    """
    sets: dict[str, set[str]] = {m: set() for m in MODULE_KEYS}
    for file, _line, _ptype, content in findings:
        m = _module_for_file(file)
        if m is None:
            continue
        sets[m].add(_violation_key(file, content))
    return {m: len(s) for m, s in sets.items()}


def file_counts(findings) -> list[tuple[str, int]]:
    """返回按违规数降序的 (file, 去重违规数) 列表,用于 top 10 报告。"""
    sets: dict[str, set[str]] = {}
    for file, _line, _ptype, content in findings:
        sets.setdefault(file, set()).add(content)
    return sorted(((f, len(s)) for f, s in sets.items()), key=lambda x: (-x[1], x[0]))


# === baseline I/O(R48 新格式 + 向后兼容旧格式读取)===

def _load_module_baseline() -> dict:
    """加载模块 baseline 文件(locales/baseline.json)。"""
    if not MODULE_BASELINE_FILE.exists():
        return {}
    try:
        return json.loads(MODULE_BASELINE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_module_baseline(
    module_counts: dict[str, int],
    *,
    reason: str = "auto",
    classify_results: dict[str, dict[str, int]] | None = None,
) -> None:
    """写入 baseline.json(R48 P1-c 新格式,可序列化 JSON)。

    Args:
        module_counts: 模块计数 {m: count}
        reason: 更新原因(写入 last_updated_by;--generate-baseline 必填)
        classify_results: --classify 结果 {m: {'user_visible': N, 'log_only': M}}
                          若为 None,默认全部归为 user_visible,log_only=0
    """
    today = date.today().isoformat()

    # 默认分类:全部 user_visible
    if classify_results is None:
        classify_results = {
            m: {'user_visible': module_counts.get(m, 0), 'log_only': 0}
            for m in MODULE_KEYS
        }

    modules_dict = {}
    uv_total = 0
    ll_total = 0
    for m in MODULE_KEYS:
        cnt = int(module_counts.get(m, 0))
        uv = int(classify_results.get(m, {}).get('user_visible', 0))
        ll = int(classify_results.get(m, {}).get('log_only', 0))
        modules_dict[m] = {
            "baseline": cnt,
            "target": 0,  # user_visible 清零目标
            "user_visible": uv,
            "log_only": ll,
        }
        uv_total += uv
        ll_total += ll

    data = {
        "_description": "R48 P1-c: 模块化 i18n 硬编码字符串 baseline(scope 审批 + delta + classify)",
        "_note": (
            "由 --generate-baseline / --ratchet 维护;total 只允许非增加(下降或持平)。"
            "R48: scope 变化需 --allow-scope-change;CI 用 --delta 比对 base/head;"
            "--classify 区分 user_visible(清零目标 0) 与 log_only(鼓励减少)。"
        ),
        "_original_r44_baseline": ORIGINAL_R44_BASELINE,
        "scanner_version": SCANNER_VERSION,
        "included_paths": INCLUDED_PATHS,
        "modules": modules_dict,
        "total": {
            "baseline": sum(modules_dict[m]["baseline"] for m in MODULE_KEYS),
            "target": 0,
            "user_visible": uv_total,
            "log_only": ll_total,
        },
        "last_updated": today,
        "last_updated_by": reason,
    }

    MODULE_BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODULE_BASELINE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def _baseline_module_count(baseline: dict, m: str) -> int:
    """从 baseline dict 读取某模块的计数(兼容新旧格式)。

    新格式: baseline["modules"][m]["baseline"]
    旧格式: baseline[m] (顶层整数)
    """
    try:
        # 新格式优先
        if "modules" in baseline and isinstance(baseline["modules"], dict):
            mod = baseline["modules"].get(m, {})
            if isinstance(mod, dict):
                return int(mod.get("baseline", 0))
            return int(mod)
        # 旧格式
        val = baseline.get(m, 0)
        return int(val) if not isinstance(val, dict) else int(val.get("baseline", 0))
    except Exception:
        return 0


def _baseline_total(baseline: dict) -> int:
    """从 baseline dict 读取 total(兼容新旧格式)。

    新格式: baseline["total"]["baseline"]
    旧格式: baseline["total"] (顶层整数)
    """
    if not baseline:
        return 0
    try:
        if "total" in baseline:
            t = baseline["total"]
            if isinstance(t, dict):
                return int(t.get("baseline", 0))
            return int(t)
    except Exception:
        pass
    return sum(_baseline_module_count(baseline, m) for m in MODULE_KEYS)


def _check_scope_change(baseline: dict) -> tuple[bool, list[str], list[str]]:
    """检查 included_paths 是否发生变化(R48 P1-c)。

    Returns:
        (changed, added, removed)
        - changed: True 表示有变化
        - added: 新增的 paths
        - removed: 删除的 paths
    """
    if not baseline:
        # 无 baseline 视为无 scope 变化(首次生成)
        return False, [], []
    old_paths = baseline.get("included_paths", [])
    if not isinstance(old_paths, list):
        old_paths = []
    new_paths = INCLUDED_PATHS
    added = [p for p in new_paths if p not in old_paths]
    removed = [p for p in old_paths if p not in new_paths]
    return bool(added or removed), added, removed


# === R48 P1-c: --classify 分类逻辑 ===

def classify_finding(file_path: str, file_content: str, line_no: int) -> str:
    """R59 §5.1 P1: 分类单条 finding 为 'user_visible' 或 'log_only'。

    判定规则(R59 §5.1 P1 重构 — 不再按 CJK/英文区分):
    - HTML 文件:
        * 所有 HTML finding(标签文本 + 用户可见属性)→ user_visible
          (旧版 R58 按 CJK 区分;R59 §5.1 P1 改为统一 user_visible,
          实现"英文 sink 绝对零基线")
    - Python:
        * 当前/前后 ±2 行含 logger./logging./print( → log_only(日志记录)
        * 其他 Python sink 调用(reply_text/send_message/HTTPException/flash 等)
          → user_visible(绝对门禁,无论中英文)
        * 默认 → user_visible(保守归类,确保 user_visible 绝对零)

    说明:
        - scan_python_content 已通过 AST 跳过 logger./print 调用,
          本函数的 ±2 行 logger 检测是兜底(用于多行调用上下文)。
        - R58 旧版按 CJK 区分(user_visible=log_only=英文 sink),导致英文用户文本
          可绕过门禁走 log_only baseline 吸收;R59 §5.1 P1 修正此问题。
        - 兼容旧测试 test_classify_finding_checks_nearby_lines:
          logger ±2 行内的 finding 仍归 log_only,超出范围归 user_visible。
    """
    lines = file_content.splitlines() if file_content else []

    # HTML:R59 §5.1 P1 统一归 user_visible(不再按 CJK 区分)
    if not file_path.endswith('.py'):
        return 'user_visible'

    # Python:先检查 logger/print 上下文(±2 行)
    # (保留旧测试 test_classify_finding_checks_nearby_lines 行为)
    for offset in (-2, -1, 0, 1, 2):
        i = line_no - 1 + offset
        if 0 <= i < len(lines):
            line = lines[i]
            if LOGGER_CALL_RE.search(line) or PRINT_CALL_RE.search(line):
                return 'log_only'

    # R59 §5.1 P1: 所有 Python sink 调用(无论中英文)→ user_visible
    # (旧版 R58 此处按 CJK 区分:中文 → user_visible,英文 sink → log_only;
    #  R59 §5.1 P1 修正:英文 sink 也归 user_visible,实现绝对零基线)
    return 'user_visible'


def classify_findings(findings, root: Path) -> dict[str, dict[str, int]]:
    """对 findings 分类,返回 {module: {'user_visible': N, 'log_only': M}}。

    使用 file::content 去重(与 count_by_module 一致),确保各模块之和等于全局总数。
    对于同一 (file, content) 出现多次(不同行号)的情况:
    若任一出现位置在 logger 上下文 → 归为 log_only;否则归为 user_visible。
    """
    file_cache: dict[str, str] = {}

    def get_content(rel: str) -> str:
        if rel not in file_cache:
            try:
                file_cache[rel] = (root / rel).read_text(encoding='utf-8')
            except Exception:
                file_cache[rel] = ''
        return file_cache[rel]

    # 按 (file, content) 分组,记录所有出现行号(避免同一内容被双重计数)
    grouped: dict[tuple[str, str], list[int]] = {}
    for file, line, _ptype, content in findings:
        if _module_for_file(file) is None:
            continue
        key = (file, content)
        grouped.setdefault(key, []).append(line)

    classified: dict[str, dict[str, set[str]]] = {
        m: {'user_visible': set(), 'log_only': set()} for m in MODULE_KEYS
    }

    for (file, content), lines in grouped.items():
        m = _module_for_file(file)
        if m is None:
            continue
        file_content = get_content(file)
        # 任一行号附近有 logger → log_only(保守归类,避免漏计日志文本)
        cls = 'user_visible'
        for line_no in lines:
            if classify_finding(file, file_content, line_no) == 'log_only':
                cls = 'log_only'
                break
        vkey = _violation_key(file, content)
        classified[m][cls].add(vkey)

    return {m: {c: len(s) for c, s in d.items()} for m, d in classified.items()}


# === R48 P1-c: --delta git 比对逻辑 ===

def _git_base_commit() -> str | None:
    """获取 git base commit(origin/master...HEAD 的 merge-base)。"""
    try:
        result = subprocess.run(
            ['git', 'merge-base', 'origin/master', 'HEAD'],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception:
        pass
    return None


def _git_current_branch() -> str | None:
    """获取当前 git 分支名(失败返回 None)。"""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch != 'HEAD':
                return branch
    except Exception:
        pass
    return None


def _git_show_file(commit: str, rel_path: str) -> str | None:
    """读取 git commit 上的文件内容(失败返回 None)。"""
    try:
        result = subprocess.run(
            ['git', 'show', f'{commit}:{rel_path}'],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def _git_list_files_at_commit(commit: str) -> list[str]:
    """列出 git commit 上所有文件路径(使用 / 分隔)。"""
    try:
        result = subprocess.run(
            ['git', 'ls-tree', '-r', '--name-only', commit],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent,
        )
        if result.returncode == 0:
            return [l.replace(chr(92), '/').strip()
                    for l in result.stdout.split('\n') if l.strip()]
    except Exception:
        pass
    return []


def collect_findings_at_commit(root: Path, commit: str) -> list[tuple[str, int, str, str]]:
    """在指定 git commit 上扫描,返回 findings 列表(R48 P1-c --delta 用)。"""
    findings = []
    files = _git_list_files_at_commit(commit)
    for rel in files:
        if is_skipped(Path(rel)):
            continue
        if _module_for_file(rel) is None:
            continue
        content = _git_show_file(commit, rel)
        if content is None:
            continue
        if rel.endswith('.py'):
            for line_no, ptype, text in scan_python_content(content):
                findings.append((rel, line_no, ptype, text))
        elif rel.endswith('.html'):
            for line_no, ptype, text in scan_html_content(content):
                findings.append((rel, line_no, ptype, text))
    return findings


# === 命令实现 ===

def cmd_check(
    module_counts: dict[str, int],
    baseline: dict,
    findings=None,
    root: Path | None = None,
) -> int:
    """模块化门禁检查 — R56 §5.1 绝对门禁:user_visible 必须 0。

    - R56 §5.1: user_visible 绝对门禁 — 任何模块 user_visible > 0 即 fail
      (不允许通过更新 baseline 消除失败;只允许真正接入 i18n)
    - log_only 仍走 baseline ratchet(只允许非增加)
    - 任何模块 log_only 当前 > baseline.log_only → 失败
    - 任何模块 log_only 当前 < baseline.log_only → 提示运行 --ratchet 下降
    """
    if not baseline:
        print(f"❌ 未找到模块 baseline: {MODULE_BASELINE_FILE}")
        print("   请先运行: python scripts/scan_hardcoded_strings.py "
              "--generate-baseline --reason 'initial baseline'")
        print("   并将 locales/baseline.json 提交到 git。")
        return 1

    # R56 §5.1: 基于 classify 计算 user_visible/log_only 计数(绝对门禁依据)
    if findings is not None and root is not None:
        classified = classify_findings(findings, root)
    else:
        # 兜底:未传 findings 时,从 module_counts 推算(假设全部为 user_visible)
        classified = {
            m: {'user_visible': module_counts.get(m, 0), 'log_only': 0}
            for m in MODULE_KEYS
        }

    # R56 §5.1 绝对门禁:user_visible 任何模块 > 0 即 fail
    uv_failed: list[tuple[str, int]] = []
    uv_total_count = 0
    for m in MODULE_KEYS:
        uv = classified.get(m, {}).get('user_visible', 0)
        uv_total_count += uv
        if uv > 0:
            uv_failed.append((m, uv))

    # log_only baseline ratchet(允许持平/下降,不允许增加)
    ll_failed: list[tuple[str, int, int]] = []
    ll_decreased: list[tuple[str, int, int]] = []
    for m in MODULE_KEYS:
        ll = classified.get(m, {}).get('log_only', 0)
        base_ll = 0
        try:
            mod_data = baseline.get("modules", {}).get(m, {})
            if isinstance(mod_data, dict):
                base_ll = int(mod_data.get("log_only", 0))
        except Exception:
            base_ll = 0
        if ll > base_ll:
            ll_failed.append((m, ll, base_ll))
        elif ll < base_ll:
            ll_decreased.append((m, ll, base_ll))

    # 明细表(R56: 同时显示 user_visible + log_only + target)
    print("R56 §5.1 i18n 绝对门禁(--check)")
    print(f"  {'模块':<22}{'user_vis':>10}{'log_only':>10}"
          f"{'base_uv':>10}{'base_ll':>10}{'target':>8}{'距target':>10}{'状态':>8}")
    for m in MODULE_KEYS:
        uv = classified.get(m, {}).get('user_visible', 0)
        ll = classified.get(m, {}).get('log_only', 0)
        try:
            mod_data = baseline.get("modules", {}).get(m, {})
            if isinstance(mod_data, dict):
                base_uv = int(mod_data.get("user_visible", 0))
                base_ll = int(mod_data.get("log_only", 0))
                target = int(mod_data.get("target", 0))
            else:
                base_uv = int(mod_data) if mod_data else 0
                base_ll = 0
                target = 0
        except Exception:
            base_uv = 0
            base_ll = 0
            target = 0
        gap_to_target = ll - target  # 距 target(0)的差距
        if uv > 0:
            status = 'UV_FAIL'
        elif ll > base_ll:
            status = 'LL_FAIL'
        elif ll < base_ll:
            status = 'LL_降'
        else:
            status = 'OK'
        print(f"  {m:<22}{uv:>10}{ll:>10}{base_uv:>10}{base_ll:>10}"
              f"{target:>8}{gap_to_target:>10}{status:>8}")
    ll_total = sum(classified.get(m, {}).get('log_only', 0) for m in MODULE_KEYS)
    total_target = 0
    total_gap = ll_total - total_target
    print(f"  {'total':<22}{uv_total_count:>10}{ll_total:>10}"
          f"{_baseline_total(baseline):>10}{ll_total:>10}"
          f"{total_target:>8}{total_gap:>10}"
          f"{'UV_FAIL' if uv_total_count > 0 else 'OK':>8}")
    print(f"  (原 R44 baseline: {ORIGINAL_R44_BASELINE}, 仅覆盖 bots/+admin 范围)")
    sv = baseline.get("scanner_version", "unknown")
    ip = baseline.get("included_paths", [])
    if not isinstance(ip, list):
        ip = []
    print(f"  (scanner_version: {sv}, included_paths: {len(ip)} 项)")
    print(f"  (R56 §5.1 绝对门禁:user_visible 必须 0,不允许通过更新 baseline 消除失败)")

    # R56 §5.1 绝对门禁判定
    if uv_failed:
        print(f"\n❌ R56 §5.1 绝对门禁失败: {len(uv_failed)} 个模块存在 user_visible 违规")
        for m, uv in uv_failed:
            print(f"   {m}: user_visible={uv} (必须为 0)")
        print("\n请将中文文本接入 i18n(translate/format_message + locale 文件),")
        print("而不是更新 baseline 消除失败。R56 §5.1 绝对门禁不允许此操作。")
        return 1

    # log_only baseline 检查
    if ll_failed:
        print(f"\n❌ {len(ll_failed)} 个模块 log_only 超过 baseline:")
        for m, cur, base in ll_failed:
            print(f"   {m}: log_only={cur} > baseline={base}(+{cur - base})")
        print("\n请修复新增的硬编码字符串并接入 i18n,而非扩大基线。")
        return 1

    if ll_decreased:
        print("\n✓ R56 §5.1 绝对门禁通过:user_visible=0,log_only 未超标。")
        print(f"\n✓ {len(ll_decreased)} 个模块 log_only 已下降,建议运行 --ratchet 下降 baseline:")
        for m, cur, base in ll_decreased:
            print(f"   {m}: log_only {base} → {cur}(-{base - cur})  距target={cur}")
        print("   鼓励每次 PR 至少下降 1 个模块的 baseline,直至清零。")
    else:
        print("\n✓ R56 §5.1 绝对门禁通过:user_visible=0,log_only 未超标。")
    return 0


def cmd_ratchet(module_counts: dict[str, int], baseline: dict,
                findings=None, root: Path | None = None) -> int:
    """下降 baseline:自动更新 baseline.json。

    R48 P1-c / R59 §5.1 P1 规则:
    - included_paths 变化 → 拒绝更新(必须用 --generate-baseline --allow-scope-change)
    - total 只允许非增加(new_total ≤ old_total);否则拒绝更新
    - 模块可升降(只要 total 非增加),以便模块间再平衡
    - R59 §5.1 P1: 写回时保留 user_visible/log_only 分类(不丢失分类信息)
    - 写回后提示提交到 git
    """
    if not baseline:
        print(f"❌ 未找到模块 baseline: {MODULE_BASELINE_FILE}")
        print("   请先运行: python scripts/scan_hardcoded_strings.py "
              "--generate-baseline --reason 'initial baseline'")
        return 1

    # R48: scope 变化检查
    scope_changed, added, removed = _check_scope_change(baseline)
    if scope_changed:
        print("❌ ratchet 拒绝更新: scanner scope 已变化")
        if added:
            print(f"   新增 paths: {added}")
        if removed:
            print(f"   删除 paths: {removed}")
        print("   scope 变化必须用 --generate-baseline --allow-scope-change 单独审批")
        print("   且不能与业务 PR 同时重置基线(单独 PR)")
        return 1

    old_total = _baseline_total(baseline)
    new_total = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    if new_total > old_total:
        print(f"❌ ratchet 拒绝更新: total {new_total} > 旧 {old_total}(只允许非增加)")
        print("   请修复新增违规(--check 查看详情),而非扩大基线。")
        return 1

    # R59 §5.1 P1: 计算分类结果(user_visible / log_only),保留分类信息写回 baseline
    # 避免 --ratchet 把 log_only 错误归为 user_visible(旧版 bug)
    if findings is not None and root is not None:
        classify_results = classify_findings(findings, root)
    else:
        # 兜底:未传 findings 时,假设全部为 user_visible(保守归类)
        classify_results = None

    # 模块可升降;写回当前计数(仅当 total 非增加)
    # 保留原有 reason(若 baseline 已有 last_updated_by)
    reason = baseline.get("last_updated_by", "ratchet")
    _save_module_baseline(module_counts, reason=reason, classify_results=classify_results)
    print(f"✓ baseline 已更新: {MODULE_BASELINE_FILE.name}")
    print(f"   total: {old_total} → {new_total}({new_total - old_total:+d})")
    print("   模块变化:")
    for m in MODULE_KEYS:
        old = _baseline_module_count(baseline, m)
        new = module_counts.get(m, 0)
        if new != old:
            tag = '升' if new > old else '降'
            print(f"     [{tag}] {m}: {old} → {new}({new - old:+d}, 距 target {new})")
    print("\n   请将 locales/baseline.json 提交到 git。")
    return 0


def cmd_report(module_counts: dict[str, int], baseline: dict, findings) -> int:
    """输出模块化进度报告(R48:含分类统计和进度百分比)。"""
    cur_total = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    base_total = _baseline_total(baseline)

    print('=' * 78)
    print('R48 P1-c i18n 模块化 baseline 进度报告')
    print('=' * 78)

    print('\n【总进度】')
    print(f"  当前 total:           {cur_total}")
    print(f"  baseline total:       {base_total}")
    if base_total > 0:
        pct = (base_total - cur_total) / base_total * 100
        print(f"  较 baseline 下降:     {base_total - cur_total}({pct:.1f}%)")
        # R48: 距 target (0) 的进度百分比
        progress_to_zero = (base_total - cur_total) / base_total * 100
        print(f"  距清零目标(0)进度:   {progress_to_zero:.1f}%"
              f"({cur_total}/{base_total} → 0)")
    print(f"  原 R44 baseline:      {ORIGINAL_R44_BASELINE}(仅 bots/+admin 范围)")
    print(f"  清零目标:             0(尚需清理 {cur_total} 处)")

    print('\n【模块明细】(距清零 = 当前计数,目标 0)')
    print(f"  {'模块':<22}{'当前':>7}{'baseline':>10}{'距清零':>8}{'进度':>8}{'状态':>6}")
    for m in MODULE_KEYS:
        cur = module_counts.get(m, 0)
        base = _baseline_module_count(baseline, m)
        if cur > base:
            status = '超标'
        elif cur < base:
            status = '已降'
        else:
            status = '持平'
        if base > 0:
            pct = (base - cur) / base * 100
        else:
            pct = 100.0 if cur == 0 else 0.0
        print(f"  {m:<22}{cur:>7}{base:>10}{cur:>8}{pct:>7.1f}%{status:>6}")

    # R48: 分类统计(若 baseline 含分类信息)
    if baseline and "modules" in baseline and isinstance(baseline["modules"], dict):
        print('\n【分类统计】(baseline 中的 user_visible / log_only)')
        print(f"  {'模块':<22}{'user_visible':>14}{'log_only':>10}{'total':>8}")
        uv_t = 0
        ll_t = 0
        for m in MODULE_KEYS:
            mod_data = baseline.get("modules", {}).get(m, {})
            if not isinstance(mod_data, dict):
                continue
            uv = mod_data.get("user_visible", 0)
            ll = mod_data.get("log_only", 0)
            uv_t += uv
            ll_t += ll
            print(f"  {m:<22}{uv:>14}{ll:>10}{uv + ll:>8}")
        print(f"  {'total':<22}{uv_t:>14}{ll_t:>10}{uv_t + ll_t:>8}")

    print('\n【Top 10 硬编码字符串文件】(按去重违规数)')
    fc = file_counts(findings)
    if not fc:
        print('  (无违规)')
    for i, (f, c) in enumerate(fc[:10], 1):
        print(f"  {i:>2}. {f:<52}{c:>5}")

    print('=' * 78)
    return 0


def cmd_classify(findings, root: Path) -> int:
    """分类模式:统计 user_visible / log_only(R59 §5.1 P1)。"""
    classified = classify_findings(findings, root)

    print('=' * 78)
    print('R59 §5.1 P1 i18n 硬编码字符串分类报告(--classify)')
    print('=' * 78)

    print(f"\n  {'模块':<22}{'user_visible':>14}{'log_only':>10}{'total':>8}")
    uv_total = 0
    ll_total = 0
    for m in MODULE_KEYS:
        uv = classified.get(m, {}).get('user_visible', 0)
        ll = classified.get(m, {}).get('log_only', 0)
        uv_total += uv
        ll_total += ll
        print(f"  {m:<22}{uv:>14}{ll:>10}{uv + ll:>8}")

    print(f"  {'total':<22}{uv_total:>14}{ll_total:>10}{uv_total + ll_total:>8}")

    print("\n说明:")
    print("  user_visible: 在 Telegram 消息、Admin Web、HTTPException detail 等用户可见处")
    print("  log_only:     在 logger./logging./print() 调用中的日志文本")
    print("  清零目标仅对 user_visible 强制(target=0);log_only 鼓励减少但可保留")
    print('=' * 78)
    return 0


def cmd_delta(root: Path) -> int:
    """CI delta 模式:比对 git base commit 与 head 的硬编码字符串数(R48 P1-c)。

    - 读取 git base commit(默认 origin/master...HEAD 的 merge-base)
    - 在 base 和 head 上分别运行扫描
    - 输出每个模块的 base_count / head_count / delta
    - 若 delta > 0(增加),exit 1
    - 若 delta <= 0(减少或持平),exit 0
    """
    base_commit = _git_base_commit()
    if not base_commit:
        print("⚠️  无法获取 git base commit (origin/master...HEAD),跳过 delta 检查")
        print("   (在非 git 环境或无 origin/master 时降级跳过,exit 0)")
        return 0

    print(f"R48 P1-c delta 模式:base={base_commit[:8]}, head=HEAD")

    base_findings = collect_findings_at_commit(root, base_commit)
    head_findings = collect_findings(root)

    base_counts = count_by_module(base_findings)
    head_counts = count_by_module(head_findings)

    print(f"\n  {'模块':<22}{'base':>8}{'head':>8}{'delta':>8}{'状态':>6}")
    any_increase = False
    total_base = 0
    total_head = 0
    for m in MODULE_KEYS:
        b = base_counts.get(m, 0)
        h = head_counts.get(m, 0)
        d = h - b
        total_base += b
        total_head += h
        if d > 0:
            status = '增加'
            any_increase = True
        elif d < 0:
            status = '减少'
        else:
            status = '持平'
        print(f"  {m:<22}{b:>8}{h:>8}{d:>+8}{status:>6}")

    total_delta = total_head - total_base
    print(f"  {'total':<22}{total_base:>8}{total_head:>8}{total_delta:>+8}"
          f"{'增加' if total_delta > 0 else 'OK':>6}")

    if any_increase or total_delta > 0:
        print(f"\n❌ delta > 0:存在新增硬编码字符串"
              f"(base={total_base}, head={total_head}, +{total_delta})")
        print("   请修复新增的硬编码字符串并接入 i18n,而非扩大基线。")
        return 1

    print(f"\n✓ delta <= 0:无新增硬编码字符串"
          f"(base={total_base}, head={total_head}, {total_delta:+d})")
    return 0


def cmd_generate_baseline(
    module_counts: dict[str, int],
    baseline: dict,
    root: Path,
    *,
    reason: str | None,
    allow_scope_change: bool,
    force: bool,
) -> int:
    """生成/重建 baseline.json(R48:强制 reason + scope 审批 + 非 master 强制 force)。"""
    if not reason:
        print("❌ --generate-baseline 必须传 --reason 参数")
        print("   例如: --reason 'scanner scope change' 或 --reason 'initial baseline'")
        print("   reason 会写入 baseline.json 的 last_updated_by 字段,便于审计")
        return 1

    # R48: 非 master 分支必须传 --force(避免在普通 PR 中覆盖 baseline 历史)
    current_branch = _git_current_branch()
    if current_branch and current_branch not in ("master", "main") and not force:
        print(f"❌ 当前分支 '{current_branch}' 不是 master/main,"
              f"--generate-baseline 拒绝运行")
        print("   非 master 分支运行需传 --force 参数")
        print("   (避免在普通 PR 中覆盖 baseline 历史;baseline 更新应单独 PR)")
        return 1

    # R48: scope 变化检查
    scope_changed, added, removed = _check_scope_change(baseline)
    if scope_changed and not allow_scope_change:
        print("ERROR: scanner scope changed, use --allow-scope-change to confirm")
        if added:
            print(f"   新增 paths: {added}")
        if removed:
            print(f"   删除 paths: {removed}")
        print("   scope 变化必须在 commit message 中注明 'scanner scope change'")
        print("   且不能与业务 PR 同时重置基线(单独 PR 审批)")
        return 1

    if scope_changed and allow_scope_change:
        print("⚠️  警告: scanner scope 已变化(--allow-scope-change 已确认)")
        if added:
            print(f"   新增 paths: {added}")
        if removed:
            print(f"   删除 paths: {removed}")
        print("   请在 commit message 中注明 'scanner scope change'")
        print("   且本 PR 应仅含 baseline 范围变化,不包含业务代码修改")
        print()

    print("⚠️  警告: --generate-baseline 仅用于初始基线生成/重建,不应在 PR 中使用。")
    print("   PR 中新增违规应修复后接入 i18n,而非纳入基线。")
    print("   CI 中应使用 --ratchet 下降 baseline(只降不升)或 --delta 比对 base/head。")
    print()

    # R48: 同时计算 classify 结果,写入 baseline.json
    findings = collect_findings(root)
    classify_results = classify_findings(findings, root)

    _save_module_baseline(module_counts, reason=reason, classify_results=classify_results)
    print(f"✓ 模块 baseline 已生成: {MODULE_BASELINE_FILE}")
    print("  模块明细:")
    for m in MODULE_KEYS:
        cnt = module_counts.get(m, 0)
        uv = classify_results.get(m, {}).get('user_visible', 0)
        ll = classify_results.get(m, {}).get('log_only', 0)
        print(f"    {m:<22} total={cnt:>5}  user_visible={uv:>5}  log_only={ll:>5}")
    total_cnt = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    print(f"    {'total':<22} {total_cnt:>11}")
    print(f"\n  scanner_version: {SCANNER_VERSION}")
    print(f"  included_paths: {len(INCLUDED_PATHS)} 项")
    print(f"  last_updated_by: {reason}")
    print("\nwarning: baseline updated, please commit locales/baseline.json separately")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='R48 P1-c 模块化 i18n 硬编码字符串扫描'
                    '(scope 审批 + delta + classify)',
    )
    parser.add_argument('--check', action='store_true',
                        help='模块化门禁检查(默认行为)')
    parser.add_argument('--ratchet', action='store_true',
                        help='下降 baseline.json(scope 不变 + total 只允许非增加)')
    parser.add_argument('--report', action='store_true',
                        help='输出模块化进度报告(含分类统计和进度百分比)')
    parser.add_argument('--generate-baseline', action='store_true',
                        help='生成/重建 baseline.json(需 --reason;scope 变化需 --allow-scope-change)')
    parser.add_argument('--delta', action='store_true',
                        help='CI delta 模式:比对 git base/head 硬编码字符串数(delta>0 → exit 1)')
    parser.add_argument('--classify', action='store_true',
                        help='分类硬编码字符串:user_visible / log_only')
    parser.add_argument('--reason', type=str, default=None,
                        help='--generate-baseline 必填:更新原因(写入 last_updated_by)')
    parser.add_argument('--allow-scope-change', action='store_true',
                        help='允许 scanner scope 变化(included_paths 增删)')
    parser.add_argument('--force', action='store_true',
                        help='非 master 分支强制运行 --generate-baseline')
    args = parser.parse_args(argv)

    root = Path(__file__).parent.parent
    findings = collect_findings(root)
    module_counts = count_by_module(findings)
    baseline = _load_module_baseline()

    if args.generate_baseline:
        return cmd_generate_baseline(
            module_counts, baseline, root,
            reason=args.reason,
            allow_scope_change=args.allow_scope_change,
            force=args.force,
        )

    if args.ratchet:
        return cmd_ratchet(module_counts, baseline, findings=findings, root=root)

    if args.report:
        return cmd_report(module_counts, baseline, findings)

    if args.delta:
        return cmd_delta(root)

    if args.classify:
        return cmd_classify(findings, root)

    # 默认行为:R56 §5.1 绝对门禁(向后兼容 CI 的 `python scripts/scan_hardcoded_strings.py`)
    return cmd_check(module_counts, baseline, findings=findings, root=root)


if __name__ == '__main__':
    sys.exit(main())
