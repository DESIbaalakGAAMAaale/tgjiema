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

R60 §10 整改要点(本次):
    - 旧版(R59)classify_finding 用"±2 行内出现 logger/print 即归 log_only"的邻行猜测,
      会把附近真正的用户 sink 错分为 log_only;且同一 (file, content) 任一出现位于日志附近
      就把全部重复项归 log_only(假阴性,绕过 user_visible 绝对门禁)。
    - 新版(R60)分类基于 AST call node 的真实父调用(scan_python_content 用 ast.walk 确定):
        * 字符串字面量的父 Call 是 logger.*/logging.*/print() → log_only(pattern_type 'log:*')
        * 字符串字面量的父 Call 是 sink(reply_text/send_message/HTTPException/flash 等)→ user_visible('sink:*')
        * 不再做 ±2 行邻行猜测;logger/print 调用的字面量改为在 scan 阶段直接收集为 log_only
    - 同一 (file, content) 多次出现:任一出现是 user_visible(sink)即归 user_visible;
      仅当全部出现都是 log_only 时才归 log_only(user_visible 优先,杜绝假阴性)。
    - HTML 扫描改用 html.parser.HTMLParser 遍历 DOM(替代纯正则),Jinja {{ }}/{% %} 在回调中剥离。

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
from html.parser import HTMLParser
from pathlib import Path

# === 常量 ===

# R60 §10: scanner 版本(分类基于 AST call node 真实父调用,移除 ±2 行邻行猜测;
# HTML 改用 html.parser 遍历 DOM,不再纯正则)
# R61 P1-07: sink 注册表扩展(render context / Response / JSONResponse / HTMLResponse
# / 邮件 / 通知)+ 递归 dict/list/tuple 抽取 + 协议常量豁免 + call-chain 输出
# R62 P1-05: cross-function source-to-sink 分析(变量回溯 / 函数返回值传播)
# + auto-enumerate FastAPI/Telegram/WebSocket/SSE/mail/notification/template sinks
# + --fail-on-unknown-sink 生产构建门禁(未知 sink 失败关闭)
SCANNER_VERSION = "6.0"
# R62 P1-05: cross-function 分析能力标记(独立于 SCANNER_VERSION,避免破坏 baseline 兼容)
CROSS_FUNCTION_ANALYSIS_VERSION = "7.0-r62-p1-05"

# 中文 Unicode 范围(仅用于向后兼容旧测试的 CJK 检测;R59 §5.1 P1 后 classify_finding
# 不再使用此常量做 user_visible/log_only 区分,所有 sink 字面量统一归 user_visible)
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# R58 P1-4: AST sink-based 扫描 — sink 函数名(Python)
# 调用这些方法时,字符串字面量参数被视为用户面向输出
# R61 P1-07: 新增 sink 必须先在此注册,才能被 scan_python_content 识别为 user_visible。
# 新增覆盖审计要求的用户面向出口:
#   - render context(TemplateResponse/render/render_template 的 context=)
#   - Response/JSONResponse/HTMLResponse(content= 携带用户可见文本)
#   - HTML/JS DOM(HTMLResponse content= 直接内联 HTML)
#   - email and notification(send_mail/send_email/notify/push_notification)
# R62 P1-05: 新增 WebSocket/SSE 用户面向出口 sink(EventSourceResponse);
#   WebSocket 的 send() 方法因过于通用(可能用于非用户面 sink),通过
#   _WEBSOCKET_RECEIVER_NAMES 单独检测(websocket.send / ws.send / socket.send),
#   不直接加入此集合。
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
    # R61 P1-07: Web 框架响应 sink — content/context 携带用户可见文本
    # JSONResponse(content={"msg": "..."}) / HTMLResponse(content="<html>...")
    # / Response(content="...") — 注意 RedirectResponse 不在此列(仅携带 URL,非用户文本)
    'JSONResponse', 'HTMLResponse', 'PlainResponse', 'Response',
    # render context — TemplateResponse(request, name, context={...}) / render_template
    'TemplateResponse', 'render', 'render_template',
    # R61 P1-07: 邮件 / 通知 sink — body/subject/text 携带用户可见文本
    'send_mail', 'send_email', 'email_message',
    'notify', 'push_notification', 'send_notification',
    # R62 P1-05: SSE (Server-Sent Events) 响应 sink — content= 携带用户可见事件流
    # EventSourceResponse(content={"event": "...", "data": "..."}) — sse_starlette
    'EventSourceResponse',
})

# R62 P1-05: WebSocket receiver 名称 — 这些 receiver 的 .send() / .send_text() /
# .send_json() 方法被视为用户面向 sink(websocket.send / ws.send / socket.send)。
# 单独检测以避免通用的 send() 方法误伤(如 queue.send / channel.send 等非用户面调用)。
_WEBSOCKET_RECEIVER_NAMES = frozenset({
    'websocket', 'ws', 'socket', 'websockets',
})

# R62 P1-05: WebSocket send 方法名(receiver ∈ _WEBSOCKET_RECEIVER_NAMES 时触发)
_WEBSOCKET_SEND_METHODS = frozenset({
    'send', 'send_text', 'send_json', 'send_bytes',
})

# R62 P1-05: SSE yield data 前缀正则 — yield f"data: ..." 模式
# (Server-Sent Events 的标准格式:每行 "data: <payload>\n\n")
_SSE_YIELD_DATA_RE = re.compile(r'^\s*data\s*:')

# R62 P1-05: cross-function 分析时检查的用户面向 kwargs 集合
# 仅这些 kwargs 的变量 / Call 来源会被 cross-function 扫描器检查,
# 避免 reply_markup / parse_mode / disable_notification 等结构性 kwargs 误伤。
# (PYTHON_SINK_KEYWORDS 已含 detail/text/message/caption/description,
#  此处补充 content/context/subject/body/data/payload/event 等 sink 函数特有 kwarg)
_CROSS_FUNCTION_USER_FACING_KWARGS = frozenset({
    # 来自 PYTHON_SINK_KEYWORDS(detail/text/message/caption/description)
    'detail', 'text', 'message', 'caption', 'description',
    # sink 函数特有的用户面向 kwargs(JSONResponse/HTMLResponse/TemplateResponse/send_mail 等)
    'content', 'context', 'subject', 'body', 'data', 'payload', 'event',
    # WebSocket send_text / send_json 的常见参数名
    'text_data', 'json_data',
})

# R58 P1-4: sink 关键字参数名 — 字符串字面量传入这些关键字时视为用户面向
# R61 P1-07: content/context/subject/body 不加入此集合(过于通用,会误伤非 sink 调用);
# 这些关键字通过所属函数 ∈ PYTHON_SINK_FUNCS 触发检查(JSONResponse/HTMLResponse/
# TemplateResponse/send_mail 等已注册为 sink func,其全部 kwarg 均被检查)。
PYTHON_SINK_KEYWORDS = frozenset({
    'detail', 'text', 'message', 'caption', 'description',
})

# R61 P1-07: 结构性 kwargs — 即使在 sink 调用中也不是用户可见文本
# (HTTP 头/状态码/cookie/编码 等结构性参数; RedirectResponse 的 url 也属此类,
#  但 RedirectResponse 已从 PYTHON_SINK_FUNCS 移除,此处保留 url 以防 Response 子类使用)
# 这些 kwargs 的值(含嵌套 dict/list 字面量)不被扫描
SINK_STRUCTURAL_KWARGS = frozenset({
    'status_code', 'headers', 'url', 'content_type', 'media_type',
    'cookies', 'background', 'charset',
})

# R58 P1-4: 豁免函数名 — 字符串字面量来自这些函数调用时不算违规
# (说明:字面量已经经过 i18n 查找或结构化错误协议封装)
# R62 P1-05: 新增 UserMessage / from_key / from_error — 统一用户面消息类型
# (UserMessage.render() 经过 i18n 本地化,其 message_key 字面量是 i18n key,非用户文本)
PYTHON_EXEMPT_FUNCS = frozenset({
    # i18n 查找
    'translate', '_i18n_t', 'format_message_icu', 'format_plural', 'format_error_response',
    'get_i18n_manager', 't', '_', 'gettext', 'ngettext', 'pgettext',
    # 结构化错误协议(替代裸字符串)
    'AppError', 'ErrorEnvelope', 'ValidationError',
    # R62 P1-05: 统一用户面消息类型(替代裸字符串)
    # UserMessage(...) / UserMessage.from_key(...) / UserMessage.from_error(...)
    # — 这些构造器接受 i18n key 字面量,render() 时才转为本地化字符串
    'UserMessage', 'from_key', 'from_error',
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

# R59 §5.1 P1: HTML 用户可见属性名(属性值硬编码时必须接入 i18n)
# 这些属性的值会被屏幕阅读器读出或直接展示给用户(WCAG 2.2 AA)
# R60 §10: 由 _HardcodedHTMLScanner(HTMLParser)在 start tag 回调中检查
HTML_VISIBLE_ATTRS = frozenset({
    'placeholder',   # input/textarea 占位符(用户可见提示)
    'title',         # 元素标题(tooltip + 屏幕reader)
    'aria-label',    # 屏幕阅读器标签(无障碍)
    'alt',           # img 替代文本(图片不可见时展示)
    'aria-describedby',  # 屏幕阅读器描述引用
    'aria-roledescription',  # 屏幕阅读器角色描述
    'aria-placeholder',  # ARIA 占位符
})

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


# R61 P1-07: 协议常量豁免 — 非用户可见的结构性字符串(允许流入 sink)
# 审计要求:所有字符串字面量默认可疑,只有协议常量可显式豁免。
# 仅协议/结构常量(非自然语言文本)可豁免;技术标识符(表名/列名/键前缀)不豁免,
# 因为它们若流入用户面向 sink 仍可能是用户可见文本。
PROTOCOL_CONSTANT_EXACT = frozenset({
    # HTTP / JSON 协议状态值(机器可读,非自然语言)
    'OK', 'FAIL', 'ok', 'fail', 'not_ready', 'unknown', 'ready', 'passed',
    'true', 'false', 'null', 'none', 'None', 'True', 'False',
    # HTTP 状态码
    '200', '201', '204', '301', '302', '303', '304', '400', '401', '403',
    '404', '405', '409', '410', '422', '429', '500', '501', '502', '503', '504',
    # 单字符分隔符 / 标点(结构性,非用户文本)
    '', ' ', ':', ',', ';', '-', '_', '/', '.', '|', '=', '?', '!', '@', '#',
    # 格式占位符(str.format / printf / loguru 标记)
    '{}', '%s', '%d', '%r', '%i', '%f',
})

# 协议常量正则(匹配上述集合未覆盖的结构性模式)
_PROTOCOL_CONSTANT_RE = re.compile(
    r'^(?:'
    r'\{\}|'                         # {}
    r'\{[a-zA-Z_][a-zA-Z0-9_]*\}|'   # {key}
    r'\{[^{}]*\}|'                   # {anything} — loguru format markers / f-string 占位
    r'%[sdrifo]|'                    # %s %d %r 等
    r'\d{3}|'                        # HTTP 状态码 200/404
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|'  # UUID
    r'0x[0-9a-fA-F]+|'              # hex
    r'\d+|'                          # 纯数字
    r'[:,\-_/\.|]'                   # 单字符分隔符
    r')$'
)


def _is_protocol_constant(text: str) -> bool:
    """R61 P1-07: 检测字符串是否为协议/结构常量(非用户可见,允许流入 sink)。

    协议常量包括:
        - HTTP/JSON 状态值:"OK" / "FAIL" / "ok" / "not_ready" / "unknown"
        - HTTP 状态码:"200" / "404" / "503"
        - 布尔/null 字面量:"true" / "false" / "null" / "none"
        - 格式占位符:"{}" / "{key}" / "%s" / "%d" / loguru markers
        - 单字符分隔符:":" / "," / "-" / "_" / "/" / "." / "|"
        - UUID / hex / 纯数字

    这些是机器可读的结构性字符串,非自然语言文本,允许流入用户面向 sink。

    Args:
        text: 字符串字面量(已展开 f-string 占位符为 {})

    Returns:
        True 表示是协议常量,应跳过;False 表示需继续检测
    """
    if text in PROTOCOL_CONSTANT_EXACT:
        return True
    return bool(_PROTOCOL_CONSTANT_RE.match(text))


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


def _is_potential_string_arg(node: ast.AST) -> bool:
    """R61 P1-07: 检查节点是否可能携带字符串字面量(用于 sink 关键字触发判定)。

    用于 _is_sink_call:仅当 sink 关键字(如 text=)的值是字符串型时才识别为 sink,
    避免 text=True(text=True 是 subprocess.run 的布尔标志,非用户文本)等误触发。

    判定为字符串型的节点:
        - str 常量(Constant str)
        - f-string(JoinedStr)
        - dict/list/tuple/set 字面量(可能嵌套字符串)
        - IfExp 条件表达式(可能分支含字符串,如 x if cond else "msg")
    """
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return True
    if isinstance(node, ast.IfExp):
        return True
    return False


def _is_sink_call(node: ast.Call) -> bool:
    """R58 P1-4: 检查 Call 节点是否是用户面向 sink 调用。

    判定规则(按优先级):
    1. 豁免函数(translate/_i18n_t/AppError/ErrorEnvelope/UserMessage 等)→ 永远不是 sink
       (即使含 text=/detail= 等 sink 关键字参数,如 _i18n_t('key', text=text))
    2. logger.*/logging.*/print() → 永远不是 sink
    3. 函数名 ∈ PYTHON_SINK_FUNCS(reply_text/send_message/HTTPException/flash 等)→ sink
    4. R62 P1-05: WebSocket receiver.send(...) — receiver ∈ _WEBSOCKET_RECEIVER_NAMES
       且方法 ∈ _WEBSOCKET_SEND_METHODS(websocket.send / ws.send_text / socket.send_json)
    5. 含 PYTHON_SINK_KEYWORDS(detail/text/message/...) 关键字参数
       且该参数值是字符串型(str/f-string/dict/list/tuple)→ sink
       (R61 P1-07: 限制为字符串型值,避免 text=True/text=False 等布尔标志误触发,
        如 subprocess.run(..., text=True, [...]) 不应被识别为 sink)
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

    # R62 P1-05: WebSocket receiver.send(...) 模式检测
    # websocket.send("hello") / ws.send_text("...") / socket.send_json({...})
    # 通过 receiver 变量名 + 方法名双重匹配,避免通用的 send() 误伤
    if _is_websocket_send_call(node):
        return True

    # 含 sink 关键字参数(detail="..."/text="..."/message="..."/...)
    # R61 P1-07: 仅当值为字符串型时才触发(避免 text=True 布尔标志误触发)
    for kw in node.keywords:
        if kw.arg in PYTHON_SINK_KEYWORDS and _is_potential_string_arg(kw.value):
            return True

    return False


def _is_websocket_send_call(node: ast.Call) -> bool:
    """R62 P1-05: 检查 Call 节点是否是 WebSocket receiver.send(...) 调用。

    匹配模式:
        websocket.send("hello")      — receiver="websocket", method="send"
        ws.send_text("hello")        — receiver="ws", method="send_text"
        socket.send_json({"k": "v"}) — receiver="socket", method="send_json"
        websockets.send(...)         — receiver="websockets", method="send"

    通过 receiver 变量名 ∈ _WEBSOCKET_RECEIVER_NAMES 与方法名 ∈
    _WEBSOCKET_SEND_METHODS 双重匹配,避免通用的 send() 方法误伤
    (如 queue.send / channel.send 等非用户面调用)。
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _WEBSOCKET_SEND_METHODS:
        return False
    # receiver 必须是简单 Name 节点(websocket / ws / socket / websockets)
    if not isinstance(func.value, ast.Name):
        return False
    return func.value.id in _WEBSOCKET_RECEIVER_NAMES


def _is_sse_yield_data(node: ast.AST) -> bool:
    """R62 P1-05: 检查 Yield 节点是否是 SSE data: ... 模式。

    匹配模式:
        yield f"data: {payload}\\n\\n"   — JoinedStr 含 "data:" 前缀
        yield "data: hello\\n\\n"        — Constant str 含 "data:" 前缀

    SSE (Server-Sent Events) 的标准格式为 "data: <payload>\\n\\n",
    这些 payload 直接发送到客户端,属用户面向 sink。
    """
    if not isinstance(node, ast.Yield):
        return False
    if node.value is None:
        return False
    val = node.value
    # 处理 f-string(JoinedStr)— 检查首个字符串部分是否以 "data:" 开头
    if isinstance(val, ast.JoinedStr):
        for part in val.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if _SSE_YIELD_DATA_RE.match(part.value):
                    return True
                return False  # 首个字面量部分不匹配,即视为非 SSE
        return False
    # 处理普通字符串常量
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return bool(_SSE_YIELD_DATA_RE.match(val.value))
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


def _describe_sink_chain(node: ast.Call, kwarg_name: str, chain: tuple[str, ...]) -> str:
    """R61 P1-07: 生成带 call-chain 的 sink 描述。

    call-chain = 从 sink 调用到字面量的路径,用于 CI 输出 file:line:call-chain。
    例:
        reply_text("hello")                 → sink:reply_text
        HTTPException(detail="...")         → sink:HTTPException.detail
        JSONResponse(content={"msg": "x"})  → sink:JSONResponse.content.dict[msg]
        send_message(chat_id, [a, "b"])     → sink:send_message.[1]

    Args:
        node: sink Call 节点
        kwarg_name: 关键字参数名(位置参数为 '')
        chain: 嵌套路径元组(如 ('dict[msg]',) / ('[1]',));直接字面量为 ()
    """
    name = _get_call_name(node)
    parts = []
    if kwarg_name:
        parts.append(kwarg_name)
    parts.extend(chain)
    if parts:
        return f'sink:{name}.{".".join(parts)}'
    return f'sink:{name}'


def _extract_sink_strings(node: ast.AST, chain: tuple[str, ...] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """R61 P1-07: 递归从 sink 参数节点抽取字符串字面量(支持 dict/list/tuple 嵌套)。

    taint / source-to-sink 模型的核心:
        - 字符串字面量(Constant str / f-string)是 source(默认可疑)
        - 字面量流入 sink(JSONResponse/send_message/HTTPException 等)即 tainted
        - 字面量若来自 exempt Call(_i18n_t/translate/ErrorEnvelope 等)则不算违规
          (exempt Call 节点本身不被深入,其内部的 i18n key 字面量不被收集)

    返回 [(text, chain), ...]:
        - text: 字面量内容(f-string 占位符展开为 {})
        - chain: 从参数根到字面量的路径(如 ('dict[status]',) / ('[1]',))
          直接字面量参数 chain 为 ()

    跳过:
        - exempt Call(_i18n_t('key') 等)— 字面量是 i18n key,非用户可见文本
        - 非 exempt Call(str.format / str.join 等)— 不深入其参数
          (避免 .format() 模板字面量被误报;模板已在直接参数处捕获)
    """
    results: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        results.append((node.value, chain))
    elif isinstance(node, ast.JoinedStr):
        # f-string: f"hello {name}" → "hello {}"
        parts: list[str] = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
            elif isinstance(val, ast.FormattedValue):
                parts.append('{}')
        if parts:
            results.append((''.join(parts), chain))
    elif isinstance(node, ast.Call):
        # exempt 函数(_i18n_t/translate/ErrorEnvelope 等)→ 跳过
        if _is_exempt_call(node):
            return results
        # 非 exempt Call → 不深入(避免误报 .format()/.join() 等模板字面量)
        return results
    elif isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            key_label = '?'
            if k is not None:
                kl = _extract_string_literal(k)
                if kl is not None:
                    key_label = kl
            results.extend(_extract_sink_strings(v, chain + (f'dict[{key_label}]',)))
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for i, elt in enumerate(node.elts):
            results.extend(_extract_sink_strings(elt, chain + (f'[{i}]',)))
    elif isinstance(node, ast.IfExp):
        # R61 P1-07: 条件表达式 x if cond else "msg" — 递归到 body/orelse 两个分支
        # (覆盖 _i18n_t('ok') if cond else "FAIL: ..." 这类混合写法)
        results.extend(_extract_sink_strings(node.body, chain))
        results.extend(_extract_sink_strings(node.orelse, chain))
    return results


# === 文件扫描 ===

def scan_python_content(content: str) -> list[tuple[int, str, str]]:
    """R60 §10 / R61 P1-07: AST sink-based 扫描 — 基于 call node 真实父调用分类字面量。

    R60 §10 整改(移除 ±2 行邻行猜测):
        字符串字面量的分类由其 AST 父 Call 节点直接决定,不再依赖邻行 logger/print:
        - 父 Call 是 logger.*/logging.*/print() → 字面量归 log_only(pattern_type 'log:*')
        - 父 Call 是 sink(reply_text/send_message/HTTPException/flash 等),
          或含 detail=/text=/message=/caption=/description= 关键字参数
          → 字面量归 user_visible(pattern_type 'sink:*')
        - 其他调用(非 sink 非 logger)→ 不收集

    R61 P1-07 扩展(taint / source-to-sink):
        - sink 注册表扩展:新增 JSONResponse/HTMLResponse/TemplateResponse/
          send_mail/notify 等用户面向出口(新 sink 必须先在 PYTHON_SINK_FUNCS 注册)
        - 递归抽取:对 sink 调用的 dict/list/tuple 参数递归收集字面量
          (覆盖 JSONResponse(content={"msg": "..."}) / TemplateResponse(context={...}))
        - call-chain:pattern_type 携带从 sink 到字面量的路径
          (如 sink:JSONResponse.content.dict[msg]),用于 CI 输出 file:line:call-chain
        - 协议常量豁免:_is_protocol_constant("OK"/"ok"/"200"/"{}" 等)允许流入 sink

    豁免(不算违规):
        - 字符串字面量来自 translate()/format_message_icu()/AppError/ErrorEnvelope 等
          (exempt Call 节点不被深入,其内部 i18n key 字面量不被收集)
        - 内部协议字符串(RELAY_ERROR:/ /start 等,bot 间机器通信)
        - 协议常量("OK"/"ok"/"200"/"{}"/单字符分隔符等,机器可读结构常量)

    Args:
        content: Python 源代码字符串

    Returns:
        [(line_no, pattern_type, content), ...] 字面量内容(截断到 80 字符)
        pattern_type 形如:
            'sink:reply_text' / 'sink:HTTPException.detail'
            'sink:JSONResponse.content.dict[msg]' (R61 P1-07 嵌套 call-chain)
            'log:info' / 'log:warning.kwarg'
    """
    findings: list[tuple[int, str, str]] = []

    # 解析 AST(语法错误时返回空)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return findings

    # 遍历所有 Call 节点(分类依据 = AST 真实父 Call,不做邻行猜测)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        is_logger = _is_logger_or_print_call(node)
        # logger/print 调用:字面量归 log_only(R60 §10:不再跳过,改为直接收集)
        # sink 调用:字面量归 user_visible
        # 其他调用:跳过
        if not is_logger and not _is_sink_call(node):
            continue

        if is_logger:
            # R60 §10: logger.*/logging.*/print() 的字符串字面量参数 → log_only
            call_name = _get_call_name(node)
            for arg in node.args:
                text = _extract_string_literal(arg)
                if text is None or not text.strip():
                    continue
                if _is_internal_protocol(text):
                    continue
                findings.append((node.lineno, f'log:{call_name}', text[:80]))
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                text = _extract_string_literal(kw.value)
                if text is None or not text.strip():
                    continue
                if _is_internal_protocol(text):
                    continue
                findings.append((node.lineno, f'log:{call_name}.{kw.arg}', text[:80]))
            continue

        # R61 P1-07: sink 调用 → user_visible,递归抽取字面量(支持 dict/list/tuple 嵌套)
        # 位置参数:递归收集字面量 + call-chain
        for arg in node.args:
            # 豁免:参数本身是 translate()/format_message_icu()/AppError/ErrorEnvelope 等调用
            if isinstance(arg, ast.Call) and _is_exempt_call(arg):
                continue
            for text, chain in _extract_sink_strings(arg, ()):
                if not text.strip():
                    continue
                # R59 §5.1 P1: 跳过内部协议字符串(bot 间机器通信,非用户可见)
                if _is_internal_protocol(text):
                    continue
                # R61 P1-07: 跳过协议常量(机器可读结构常量,非用户可见)
                if _is_protocol_constant(text):
                    continue
                ptype = _describe_sink_chain(node, '', chain)
                findings.append((node.lineno, ptype, text[:80]))

        # 关键字参数:递归收集字面量 + call-chain
        for kw in node.keywords:
            # *args/**kwargs 的 kwarg.arg 为 None,跳过
            if kw.arg is None:
                continue
            # 仅当关键字名 ∈ PYTHON_SINK_KEYWORDS 或函数本身是 sink 时才检查
            if kw.arg not in PYTHON_SINK_KEYWORDS and _get_call_name(node) not in PYTHON_SINK_FUNCS:
                continue
            # R61 P1-07: 跳过结构性 kwargs(HTTP 头/状态码/cookie/URL 等,非用户可见文本)
            if kw.arg in SINK_STRUCTURAL_KWARGS:
                continue
            # 豁免:值是 exempt 调用
            if isinstance(kw.value, ast.Call) and _is_exempt_call(kw.value):
                continue
            for text, chain in _extract_sink_strings(kw.value, ()):
                if not text.strip():
                    continue
                if _is_internal_protocol(text):
                    continue
                # R61 P1-07: 跳过协议常量(机器可读结构常量,非用户可见)
                if _is_protocol_constant(text):
                    continue
                ptype = _describe_sink_chain(node, kw.arg, chain)
                findings.append((node.lineno, ptype, text[:80]))

    return findings


# === R62 P1-05: cross-function source-to-sink 分析 ===


def _build_function_var_map(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.AST]:
    """R62 P1-05: 构建函数内变量名 → 赋值右值 AST 节点映射(单函数作用域)。

    收集 func_node 内所有 ``x = expr`` / ``x: type = expr`` / ``x := expr``
    形式的赋值,记录最后一次赋值的右值 AST 节点(用于 sink 参数变量回溯)。

    局限性(显式声明,避免误用):
        - 仅覆盖单函数作用域(不跨函数,不跨模块,不追 import)
        - 仅记录最后一次赋值(简化模型,不模拟控制流)
        - 不展开 comprehension / walrus 内的赋值(避免过度复杂)
        - 仅用于 cross-function 启发式,不替代 mypy/pyright 的真实类型推断

    Args:
        func_node: ast.FunctionDef / ast.AsyncFunctionDef 节点

    Returns:
        {变量名: 赋值右值 AST 节点};无赋值时返回空 dict
    """
    var_map: dict[str, ast.AST] = {}
    for node in ast.walk(func_node):
        # 普通赋值: x = expr / x: type = expr
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    var_map[target.id] = node.value
        # 注解赋值: x: type = expr (无 value 时跳过)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                var_map[node.target.id] = node.value
    return var_map


def _trace_variable_source(
    var_name: str,
    var_map: dict[str, ast.AST],
    depth: int = 0,
    _max_depth: int = 3,
) -> tuple[str | None, tuple[str, ...]]:
    """R62 P1-05: 回溯变量到其来源(字符串字面量 / 函数调用 / 未知)。

    递归追踪:变量 → 赋值右值 → 若右值仍是变量,继续回溯(最多 _max_depth 层)。

    返回 (source_kind, chain):
        - source_kind="literal"  : 右值是字符串字面量 / f-string → 需标记(违规)
        - source_kind="exempt"   : 右值是 exempt 调用(_i18n_t / AppError / UserMessage
          等)→ 不标记(已 i18n / 结构化)
        - source_kind="unknown_call": 右值是非 exempt 函数调用 → 需 --fail-on-unknown-sink
        - source_kind="unknown"  : 右值是其他形式(属性访问 / 子脚本 / 字面量非 str 等)
        - source_kind=None       : 变量未在 var_map 中找到(参数 / 全局 / 跨函数)
        - chain: 从 sink 到字面量来源的路径(如 ('var<msg>',) / ('var<msg>.call',)
    """
    if depth > _max_depth:
        return ("unknown", ())
    value = var_map.get(var_name)
    if value is None:
        return (None, ())
    # 字符串字面量 / f-string → 标记
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return ("literal", (f"var<{var_name}>",))
    if isinstance(value, ast.JoinedStr):
        return ("literal", (f"var<{var_name}>",))
    # exempt 函数调用(_i18n_t / AppError / UserMessage 等)→ 不标记
    if isinstance(value, ast.Call) and _is_exempt_call(value):
        return ("exempt", (f"var<{var_name}>",))
    # 非 exempt 函数调用 → 未知来源(--fail-on-unknown-sink 时标记)
    if isinstance(value, ast.Call):
        return ("unknown_call", (f"var<{var_name}>",))
    # 右值是另一个变量 → 继续回溯
    if isinstance(value, ast.Name):
        sub_kind, sub_chain = _trace_variable_source(
            value.id, var_map, depth + 1, _max_depth,
        )
        return (sub_kind, (f"var<{var_name}>",) + sub_chain)
    # 其他形式(属性访问 / 子脚本 / 字面量非 str 等)→ 未知
    return ("unknown", (f"var<{var_name}>",))


def _is_user_message_returning_call(node: ast.Call) -> bool:
    """R62 P1-05: 检查 Call 节点是否返回 UserMessage / ErrorEnvelope / AppError。

    用于 cross-function 分析:当 sink 参数是函数调用时,
    若该函数返回 UserMessage/ErrorEnvelope(已结构化),则不标记;
    否则视为未知来源(--fail-on-unknown-sink 时标记)。

    匹配模式(基于 exempt 函数名):
        - UserMessage(...) / UserMessage.from_key(...) / UserMessage.from_error(...)
        - AppError(...) / ErrorEnvelope(...)
        - _i18n_t(...) / translate(...) / format_message_icu(...)
    """
    return _is_exempt_call(node)


def _follow_var_chain_to_literal(
    var_name: str,
    var_map: dict[str, ast.AST],
    _max_depth: int = 5,
) -> str | None:
    """R62 P1-05: 沿变量赋值链回溯,提取最终的字符串字面量。

    当 ``_trace_variable_source`` 返回 ``kind="literal"`` 时,实际字面量可能在
    链的末尾(如 ``a = b; b = "literal"``),需沿链回溯到 Constant / JoinedStr。

    防御性设计:用 ``seen`` 集合防止循环引用(如 ``a = b; b = a``)导致的死循环。

    Args:
        var_name: 起点 sink 参数变量名(如 ``msg``)
        var_map: 函数内变量赋值映射(来自 ``_build_function_var_map``)
        _max_depth: 最大回溯深度(防御性,避免无限递归)

    Returns:
        最终的字符串字面量(Constant str);若链中存在非字面量节点则返回 None。
        f-string(JoinedStr)通过 ``_extract_string_literal`` 抽取首段字面量。
    """
    seen: set[str] = set()
    current = var_name
    depth = 0
    while current and current not in seen and depth <= _max_depth:
        seen.add(current)
        value = var_map.get(current)
        if value is None:
            return None
        # 字符串字面量 → 直接返回
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        # f-string → 抽取首段字面量(占位符展开为 {})
        if isinstance(value, ast.JoinedStr):
            return _extract_string_literal(value)
        # 链中下一环节是另一个变量 → 继续回溯
        if isinstance(value, ast.Name):
            current = value.id
            depth += 1
            continue
        # 其他形式(Call / Attribute / Subscript 等)→ 不是字面量
        return None
    return None


def scan_python_content_cross_function(
    content: str,
    *,
    fail_on_unknown_sink: bool = False,
) -> list[tuple[int, str, str]]:
    """R62 P1-05: cross-function source-to-sink 分析(增量于 scan_python_content)。

    在 scan_python_content 已有检测之上,新增:
        1. 变量回溯:当 sink 参数是 ``Name``(变量引用)时,回溯到函数内赋值;
           若赋值右值是字符串字面量 / f-string → 标记(pattern_type 'sink:<name>.var')
        2. 函数返回值传播:当 sink 参数是非 exempt 函数调用时,若 --fail-on-unknown-sink
           开启 → 标记(pattern_type 'sink:<name>.unknown_call')
        3. SSE yield:f"..." / yield "..." 含 "data:" 前缀 → 标记(pattern_type 'sse:yield')
        4. FastAPI dict 返回:return {"message": "..."} → 标记(pattern_type 'fastapi:return_dict')

    Args:
        content: Python 源代码字符串
        fail_on_unknown_sink: True 时,非 exempt 函数调用作为 sink 参数被标记
            (生产构建门禁:未知 sink 来源失败关闭)

    Returns:
        [(line_no, pattern_type, content), ...] 仅包含 cross-function 新增的 findings
        (不重复 scan_python_content 已检测的直接字面量)
    """
    findings: list[tuple[int, str, str]] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return findings

    # 收集所有函数定义(含 async)
    func_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    for func in func_nodes:
        var_map = _build_function_var_map(func)
        # 遍历函数内所有 Call 节点
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not _is_sink_call(node):
                continue
            sink_name = _get_call_name(node)
            # WebSocket sink 名修正(websocket.send → ws:send)
            if _is_websocket_send_call(node):
                sink_name = f"ws:{node.func.attr}"
            # 检查位置参数 + 关键字参数(变量 / Call 来源)
            # 位置参数:仅检查 "literal"(字符串字面量回溯),不检查 "unknown_call"
            #   (位置参数可能是 chat_id 等非用户文本,unknown_call 会误伤)
            # 关键字参数:仅检查用户面向 kwargs(text/detail/message/content/context/
            #   subject/body 等),不检查结构性 kwargs(reply_markup/parse_mode 等)
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    kind, chain = _trace_variable_source(arg.id, var_map)
                    if kind == "literal":
                        # R62 P1-05: 沿变量赋值链回溯到最终字面量(支持 a=b; b="literal")
                        # 旧实现仅取 var_map.get(arg.id),无法处理链式赋值,
                        # 导致 wrapper / 别名漏检(审计 P1-05 明确要求覆盖)。
                        text = _follow_var_chain_to_literal(arg.id, var_map)
                        if text and text.strip() and not _is_internal_protocol(text) \
                                and not _is_protocol_constant(text):
                            ptype = _describe_sink_chain(node, '', chain)
                            findings.append((node.lineno, ptype, text[:80]))
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if kw.arg in SINK_STRUCTURAL_KWARGS:
                    continue
                # R62 P1-05: cross-function 仅检查用户面向 kwargs
                # (text/detail/message/content/context/subject/body 等),
                # 跳过结构性 kwargs(reply_markup/parse_mode/disable_notification 等)
                if kw.arg not in _CROSS_FUNCTION_USER_FACING_KWARGS:
                    continue
                arg = kw.value
                kw_name = kw.arg
                # 情况 2:变量 → 回溯赋值
                if isinstance(arg, ast.Name):
                    kind, chain = _trace_variable_source(arg.id, var_map)
                    if kind == "literal":
                        # 变量回溯到字面量 — 标记(避免 wrapper / 别名漏检)
                        # R62 P1-05: 沿链回溯到最终字面量(支持 a=b; b="literal")
                        text = _follow_var_chain_to_literal(arg.id, var_map)
                        if text and text.strip() and not _is_internal_protocol(text) \
                                and not _is_protocol_constant(text):
                            ptype = _describe_sink_chain(node, kw_name, chain)
                            findings.append((node.lineno, ptype, text[:80]))
                    elif kind == "unknown_call" and fail_on_unknown_sink:
                        # 变量赋值自非 exempt 函数 → 未知来源(--fail-on-unknown-sink)
                        ptype = _describe_sink_chain(node, kw_name, chain)
                        findings.append((node.lineno, f"{ptype}.unknown_call", arg.id[:80]))
                # 情况 3:非 exempt 函数调用(--fail-on-unknown-sink 时标记)
                elif isinstance(arg, ast.Call) and fail_on_unknown_sink:
                    if not _is_exempt_call(arg):
                        # 非 exempt 函数调用作为 sink 参数 → 未知来源
                        call_name = _get_call_name(arg)
                        ptype = _describe_sink_chain(node, kw_name, ())
                        findings.append(
                            (node.lineno, f"{ptype}.unknown_call", call_name[:80])
                        )

    # 检测 SSE yield f"data: ..." 模式
    for node in ast.walk(tree):
        if isinstance(node, ast.Yield) and _is_sse_yield_data(node):
            # 抽取字面量文本
            val = node.value
            text = _extract_string_literal(val)
            if text and text.strip():
                findings.append((node.lineno, 'sse:yield', text[:80]))

    # 检测 FastAPI dict 返回:return {"message": "..."} / return {"error": "..."}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        ret = node.value
        if not isinstance(ret, ast.Dict):
            continue
        # 检查 dict 字面量是否含用户面向 key(message/msg/error/detail/description)
        user_facing_keys = frozenset({"message", "msg", "error", "detail", "description"})
        for k, v in zip(ret.keys, ret.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            if k.value not in user_facing_keys:
                continue
            # 抽取 value 字面量
            text = _extract_string_literal(v)
            if text and text.strip() and not _is_internal_protocol(text) \
                    and not _is_protocol_constant(text):
                ptype = f"fastapi:return_dict.dict[{k.value}]"
                findings.append((node.lineno, ptype, text[:80]))

    return findings


def enumerate_user_facing_sinks(content: str) -> list[tuple[int, str, str]]:
    """R62 P1-05: 自动枚举用户面向 sink(检测 FastAPI/Telegram/WebSocket/SSE/
    mail/notification/template 出口)。

    用于审计报告:列出所有 sink 调用点(无论参数是否违规),便于人工确认
    所有用户面出口已纳入 PYTHON_SINK_FUNCS 注册表(新 sink 必须先注册)。

    与 scan_python_content 的区别:
        - scan_python_content: 检测违规字面量(命中 sink 即 flag)
        - enumerate_user_facing_sinks: 列出所有 sink 调用点(用于 sink 清单审计)

    返回 [(line_no, sink_category, sink_repr), ...]:
        - sink_category: 'fastapi' / 'telegram' / 'websocket' / 'sse' /
          'mail' / 'notification' / 'template' / 'http_exception' / 'unknown'
        - sink_repr: sink 函数名 + 关键参数(截断到 80 字符)
    """
    sinks: list[tuple[int, str, str]] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return sinks

    for node in ast.walk(tree):
        # Yield SSE 模式
        if isinstance(node, ast.Yield) and _is_sse_yield_data(node):
            sinks.append((node.lineno, 'sse', 'yield data:...'))
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_name(node)
        if not name:
            continue
        # 豁免函数(UserMessage / _i18n_t / AppError 等)不算 sink 出口
        if name in PYTHON_EXEMPT_FUNCS:
            continue
        # logger.* 调用不算用户面 sink
        if _is_logger_or_print_call(node):
            continue

        category = _classify_sink_category(node, name)
        if category is None:
            continue
        sink_repr = name
        # WebSocket 用 ws:send / ws:send_text 表示
        if _is_websocket_send_call(node):
            sink_repr = f"ws:{node.func.attr}"
        sinks.append((node.lineno, category, sink_repr[:80]))

    return sinks


def _classify_sink_category(node: ast.Call, name: str) -> str | None:
    """R62 P1-05: 将 sink 调用按出口类型分类(用于 enumerate_user_facing_sinks)。

    返回 None 表示不是用户面 sink(不纳入枚举)。
    """
    # FastAPI HTTP 异常 / 响应
    if name == 'HTTPException':
        return 'http_exception'
    if name in {'JSONResponse', 'HTMLResponse', 'PlainResponse',
                'Response', 'EventSourceResponse'}:
        return 'fastapi'
    # 模板渲染
    if name in {'TemplateResponse', 'render', 'render_template'}:
        return 'template'
    # Web framework flash (Flask/Starlette)
    if name == 'flash':
        return 'fastapi'
    # Telegram Bot
    if name in {'reply_text', 'reply_photo', 'reply_document', 'reply_video',
                'reply_audio', 'reply_animation', 'reply_media_group',
                'reply_voice', 'reply_sticker', 'reply_location', 'reply_venue',
                'reply_contact', 'reply_poll', 'reply_dice', 'reply_chat_action',
                'send_message', 'send_photo', 'send_document', 'send_video',
                'send_audio', 'send_animation', 'send_media_group', 'send_voice',
                'send_sticker', 'send_location', 'send_venue', 'send_contact',
                'send_poll', 'send_chat_action',
                'answer_callback_query', 'answer_inline_query',
                'answer_shipping_query', 'answer_pre_checkout_query',
                'edit_message_text', 'edit_message_caption',
                'edit_message_reply_markup'}:
        return 'telegram'
    # 邮件
    if name in {'send_mail', 'send_email', 'email_message'}:
        return 'mail'
    # 通知
    if name in {'notify', 'push_notification', 'send_notification'}:
        return 'notification'
    # WebSocket
    if _is_websocket_send_call(node):
        return 'websocket'
    # 含 sink 关键字参数的未知调用(detail= / text= / message=)
    for kw in node.keywords:
        if kw.arg in PYTHON_SINK_KEYWORDS and _is_potential_string_arg(kw.value):
            return 'unknown_sink_kwarg'
    return None


def scan_python_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 Python 文件(读文件后委托给 scan_python_content)。"""
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return []
    return scan_python_content(content)


class _HardcodedHTMLScanner(HTMLParser):
    """R60 §10: HTML DOM 遍历扫描器(基于 html.parser.HTMLParser,替代纯正则)。

    遍历 DOM 节点收集:
        - 标签之间的文本(handle_data):任何含字母数字的文本均纳入检测
        - 用户可见属性值(placeholder/title/aria-label/alt 等)

    自动跳过 HTML 注释(HTMLParser 路由到 handle_comment,不传入 handle_data)。
    <script>/<style> 内容在 CDATA 模式下传入 handle_data,本扫描器通过 _in_cdata
    标记跳过(CSS/JS 代码不算用户文本)。
    Jinja2 {{ }}/{% %} 表达式在回调中剥离(模板变量已通过 t() 路由到 i18n)。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.findings: list[tuple[int, str, str]] = []
        self._in_cdata = False  # 是否在 <script>/<style> 内部

    @staticmethod
    def _strip_jinja(text: str) -> str:
        """剥离 Jinja2 {{ }} 表达式、{% %} 控制语句、{# #} 注释,以及由 HTML 属性嵌套双引号
        导致的悬空 Jinja 片段(如 ``{{ t(`` 或 ``,k) }}``)。

        R60 §10 修复(消除 6+ 个 HTML 误报):
            - 旧版 ``.*?`` 未带 DOTALL,跨行 Jinja 块(尤其是 ``{# ... #}`` 多行注释)
              无法被剥离 → 多行注释 bleed 到 html_text finding 中。
            - 旧版只剥离完整 ``{{ ... }}`` / ``{% ... %}``,不处理 HTMLParser 在
              ``aria-label="{{ t("...") }}"`` 嵌套双引号处截断属性值而残留的悬空
              ``{{ t(`` 或孤儿 ``,k) }}`` 片段(以及模板里 ``<{`` 笔误导致的孤儿 ``}}``)。
            - 新版反复剥离:完整块 → 悬空 opener(``{{`` 到结尾)→ 孤儿 closer(开头到 ``}}``),
              直至稳定。
        """
        prev = None
        while prev != text:
            prev = text
            # 完整 Jinja 块(带 DOTALL,可跨行)
            text = re.sub(r'\{\{.*?\}\}', '', text, flags=re.DOTALL)
            text = re.sub(r'\{%.*?%\}', '', text, flags=re.DOTALL)
            text = re.sub(r'\{#.*?#\}', '', text, flags=re.DOTALL)
            # 悬空 opener(如 ``{{ t(`` — HTMLParser 在嵌套双引号处截断属性值,
            # 留下未闭合的 Jinja 表达式开头;从 ``{{`` / ``{%`` / ``{#`` 到字符串结尾全部剥离)
            text = re.sub(r'\{\{.*$', '', text, flags=re.DOTALL)
            text = re.sub(r'\{%.*$', '', text, flags=re.DOTALL)
            text = re.sub(r'\{#.*$', '', text, flags=re.DOTALL)
            # 孤儿 closer(如 ``,k) }}`` 或 ``{ status.get('reason', '未提供') }}`` —
            # 上一段属性值/文本被截断后(或模板 ``<{`` 笔误)留下的 ``}}`` / ``%}`` 闭合;
            # 仅当整段文本不含 opener(``{{`` / ``{%`` / ``{#``)时才剥离,避免误伤
            # 合法静态文本;从字符串开头到第一个 ``}}`` / ``%}`` (含)全部剥离)
            if '{{' not in text and '{%' not in text and '{#' not in text:
                text = re.sub(r'^.*?\}\}', '', text, flags=re.DOTALL)
                text = re.sub(r'^.*?%\}', '', text, flags=re.DOTALL)
        return text

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._in_cdata = True
        # 检查用户可见属性(placeholder/title/aria-label/alt 等)
        lineno = self.getpos()[0]
        for name, value in attrs:
            if value is None:
                continue
            name_lower = name.lower()
            if name_lower not in HTML_VISIBLE_ATTRS:
                continue
            cleaned = self._strip_jinja(value).strip()
            # 跳过纯空白/纯标点(无字母数字)
            if cleaned and any(c.isalnum() for c in cleaned):
                self.findings.append((lineno, f'html_attr:{name_lower}', cleaned[:80]))

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._in_cdata = False

    def handle_data(self, data):
        if self._in_cdata:
            return  # 跳过 <script>/<style> 内容(CSS/JS 代码不算用户文本)
        # 剥离 Jinja2 表达式(模板变量已通过 t() 路由到 i18n)
        cleaned = self._strip_jinja(data)
        text = cleaned.strip()
        # 跳过纯空白/纯标点(无字母数字)
        if text and any(c.isalnum() for c in text):
            lineno = self.getpos()[0]
            self.findings.append((lineno, 'html_text', text[:80]))


def scan_html_content(content: str) -> list[tuple[int, str, str]]:
    """R59 §5.1 P1 / R60 §10: 扫描 HTML 中的硬编码用户可见文本(不区分中英文)。

    R60 §10: 改用 html.parser.HTMLParser 遍历 DOM(替代纯正则),
    Jinja {{ }}/{% %} 在回调中剥离。

    检测范围:
        - HTML 标签之间的文本(>文本<):任何含字母数字的文本均纳入检测
          (旧版 R58 仅检测 CJK;R59 §5.1 P1 扩展到英文 — "英文 sink 绝对零基线")
        - HTML 属性值:`placeholder` / `title` / `aria-label` / `alt` 等
          用户可见属性的硬编码值(无论中英文)

    排除:
        - HTML 注释 `<!-- -->` (HTMLParser 自动路由到 handle_comment,不传入 handle_data)
        - Jinja2 表达式 `{{ }}` (模板变量已通过 `t()` 路由到 i18n)
        - Jinja2 控制语句 `{% %}` (条件/循环,非用户文本)
        - `<script>` / `<style>` 块内容(CSS/JS 代码不算用户文本,通过 _in_cdata 跳过)
        - 纯标点/符号文本(如 `:` `|` `>` 等,无字母数字)

    Args:
        content: HTML 源代码字符串

    Returns:
        [(line_no, pattern_type, content), ...] 字面量内容(截断到 80 字符)
        pattern_type 形如 "html_text" 或 "html_attr:aria-label"
    """
    scanner = _HardcodedHTMLScanner()
    scanner.feed(content)
    scanner.close()
    return scanner.findings


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

def classify_finding(file_path: str, pattern_type: str) -> str:
    """R60 §10: 分类单条 finding 为 'user_visible' 或 'log_only'。

    分类基于 pattern_type(AST 父调用的直接编码,由 scan_python_content /
    scan_html_content 产出):
    - pattern_type 以 'log:' 开头 → log_only
      (字符串字面量直接是 logger.*/logging.*/print() 调用的参数)
    - 其他(pattern_type 以 'sink:' / 'html_text' / 'html_attr:' 开头,或未知前缀)
      → user_visible(绝对门禁,无论中英文)

    R60 §10 整改(移除 ±2 行邻行猜测):
        - 旧版用"±2 行内出现 logger/print 即归 log_only"的邻行猜测,会把附近真正的
          用户 sink 错分为 log_only(假阴性,绕过 user_visible 绝对门禁);且同一
          (file, content) 任一出现位于日志附近就把全部重复项归 log_only。
        - 新版分类完全基于 AST call node 的真实父调用(scan 阶段已编码到 pattern_type),
          不再做邻行猜测;logger/print 调用的字面量在 scan 阶段直接收集为 'log:*'。
    """
    if pattern_type.startswith('log:'):
        return 'log_only'
    return 'user_visible'


def classify_findings(findings, root: Path) -> dict[str, dict[str, int]]:
    """对 findings 分类,返回 {module: {'user_visible': N, 'log_only': M}}。

    R60 §10: 分类基于 pattern_type(AST 父调用的直接编码),不再做 ±2 行邻行猜测。
    使用 file::content 去重(与 count_by_module 一致),确保各模块之和等于全局总数。
    对于同一 (file, content) 出现多次(不同行号/不同 pattern_type)的情况:
    任一出现是 user_visible(sink:*/html_*)即归 user_visible;
    仅当全部出现都是 log_only(log:*)时才归 log_only(user_visible 优先,杜绝假阴性)。

    注意:root 参数保留用于向后兼容(调用方/测试 monkeypatch 仍传入),R60 §10 后
    分类不再需要读取文件内容(pattern_type 已编码父调用信息)。
    """
    # 按 (file, content) 分组,记录所有出现的 pattern_type
    grouped: dict[tuple[str, str], list[str]] = {}
    for file, _line, ptype, content in findings:
        if _module_for_file(file) is None:
            continue
        key = (file, content)
        grouped.setdefault(key, []).append(ptype)

    classified: dict[str, dict[str, set[str]]] = {
        m: {'user_visible': set(), 'log_only': set()} for m in MODULE_KEYS
    }

    for (file, content), ptypes in grouped.items():
        m = _module_for_file(file)
        if m is None:
            continue
        # R60 §10: user_visible 优先 — 任一出现是 sink:*/html_* 即归 user_visible
        # (仅当全部出现都是 log:* 时才归 log_only,杜绝"邻行 logger 污染全部重复项")
        cls = 'log_only' if all(
            classify_finding(file, p) == 'log_only' for p in ptypes
        ) else 'user_visible'
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

def _collect_user_visible_findings(findings) -> list[tuple[str, int, str, str]]:
    """R61 P1-07: 从 findings 中筛选 user_visible 违规(非 log:* 的 sink:*/html_*)。

    用于 --check 失败时输出 file:line:call-chain 明细,辅助定位修复点。
    返回 [(file, line_no, ptype, content), ...] 按 (file, line_no) 排序。
    """
    uv = [
        (f, ln, pt, ct)
        for f, ln, pt, ct in findings
        if _module_for_file(f) is not None
        and classify_finding(f, pt) == 'user_visible'
    ]
    uv.sort(key=lambda x: (x[0], x[1]))
    return uv


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
        # R61 P1-07: 输出每条 user_visible 违规的 file:line:call-chain 明细
        if findings is not None:
            uv_findings = _collect_user_visible_findings(findings)
            if uv_findings:
                print(f"\n   user_visible 违规明细(file:line: call-chain):")
                for f, ln, pt, ct in uv_findings:
                    print(f"     {f}:{ln}: {pt}  <-  \"{ct}\"")
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


# === R62 P1-05: cross-function / --fail-on-unknown-sink 命令 ===


def cmd_cross_function(root: Path, *, fail_on_unknown_sink: bool = False) -> int:
    """R62 P1-05: cross-function source-to-sink 分析命令。

    扫描所有 Python 源文件,执行 cross-function 变量回溯 + 函数返回值传播检测,
    输出新增 findings(增量于 scan_python_content)。

    Args:
        root: 项目根目录
        fail_on_unknown_sink: True 时,非 exempt 函数调用作为 sink 参数被标记
            (生产构建门禁:未知 sink 来源失败关闭)

    Exit code:
        0 = 无新增 finding
        1 = 存在 cross-function finding(变量回溯到字面量 / 未知 sink 来源)
    """
    print('=' * 78)
    print('R62 P1-05 cross-function source-to-sink 分析')
    print(f"  cross-function 分析版本: {CROSS_FUNCTION_ANALYSIS_VERSION}")
    print(f"  fail_on_unknown_sink: {fail_on_unknown_sink}")
    print('=' * 78)

    total_findings = 0
    by_category: dict[str, int] = {}
    by_file: dict[str, int] = {}

    for pattern in ['bots/**/*.py', 'admin/**/*.py', 'services/**/*.py']:
        for path in root.glob(pattern):
            if is_skipped(path):
                continue
            try:
                content = path.read_text(encoding='utf-8')
            except Exception:
                continue
            rel = str(path.relative_to(root)).replace(chr(92), '/')
            findings = scan_python_content_cross_function(
                content, fail_on_unknown_sink=fail_on_unknown_sink,
            )
            if not findings:
                continue
            for line_no, ptype, text in findings:
                print(f"  {rel}:{line_no}: {ptype}  <-  \"{text}\"")
                total_findings += 1
                # 按类别统计(prefix:)
                cat = ptype.split('.')[0] if '.' in ptype else ptype
                by_category[cat] = by_category.get(cat, 0) + 1
                by_file[rel] = by_file.get(rel, 0) + 1

    print()
    print(f"  cross-function findings 总计: {total_findings}")
    if by_category:
        print("  按类别:")
        for cat, cnt in sorted(by_category.items(), key=lambda x: (-x[1], x[0])):
            print(f"    {cat:<24} {cnt:>5}")
    if by_file:
        print("  按文件(Top 10):")
        for f, cnt in sorted(by_file.items(), key=lambda x: (-x[1], x[0]))[:10]:
            print(f"    {f:<52} {cnt:>5}")
    print('=' * 78)
    if total_findings > 0:
        print("❌ 发现 cross-function findings")
        print("   请将变量赋值改为直接 _i18n_t() / UserMessage.from_key() 调用,")
        print("   或使用 UserMessage 结构化对象替代裸字符串变量传播。")
        return 1
    print("✓ 无 cross-function findings")
    return 0


def cmd_enumerate_sinks(root: Path) -> int:
    """R62 P1-05: 自动枚举所有用户面向 sink(审计报告)。

    扫描所有 Python 源文件,列出所有 sink 调用点(无论参数是否违规),
    按出口类型(fastapi / telegram / websocket / sse / mail / notification /
    template / http_exception)分类输出。

    用于审计确认:所有用户面出口已纳入 PYTHON_SINK_FUNCS 注册表
    (新 sink 必须先注册,生产构建未知 sink 失败关闭)。

    Exit code:
        始终 0(仅生成报告,不做门禁判定)
    """
    print('=' * 78)
    print('R62 P1-05 用户面向 sink 自动枚举报告')
    print('=' * 78)

    by_category: dict[str, int] = {}
    by_file_cat: dict[str, dict[str, int]] = {}
    total_sinks = 0

    for pattern in ['bots/**/*.py', 'admin/**/*.py', 'services/**/*.py']:
        for path in root.glob(pattern):
            if is_skipped(path):
                continue
            try:
                content = path.read_text(encoding='utf-8')
            except Exception:
                continue
            rel = str(path.relative_to(root)).replace(chr(92), '/')
            sinks = enumerate_user_facing_sinks(content)
            if not sinks:
                continue
            by_file_cat.setdefault(rel, {})
            for _ln, cat, _repr in sinks:
                by_category[cat] = by_category.get(cat, 0) + 1
                by_file_cat[rel][cat] = by_file_cat[rel].get(cat, 0) + 1
                total_sinks += 1

    print(f"\n  sink 总计: {total_sinks}")
    print("\n【按出口类型】")
    print(f"  {'类别':<22}{'数量':>8}")
    for cat, cnt in sorted(by_category.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {cat:<22}{cnt:>8}")
    print("\n【按文件 Top 10】")
    print(f"  {'文件':<42}{'类别':<22}{'数量':>8}")
    file_totals = [(f, sum(c.values())) for f, c in by_file_cat.items()]
    for f, _ in sorted(file_totals, key=lambda x: (-x[1], x[0]))[:10]:
        for cat, cnt in sorted(by_file_cat[f].items(),
                              key=lambda x: (-x[1], x[0])):
            print(f"  {f:<42}{cat:<22}{cnt:>8}")
    print('=' * 78)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='R48 P1-c 模块化 i18n 硬编码字符串扫描'
                    '(scope 审批 + delta + classify + R62 cross-function)',
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
    # R62 P1-05: cross-function source-to-sink 分析(变量回溯 / 函数返回值传播)
    parser.add_argument('--cross-function', action='store_true',
                        help='R62 P1-05: cross-function source-to-sink 分析'
                            '(变量回溯到字面量 / 函数返回值传播检测)')
    # R62 P1-05: 生产构建门禁 — 未知 sink 来源失败关闭
    parser.add_argument('--fail-on-unknown-sink', action='store_true',
                        help='R62 P1-05: 生产构建门禁 — 非 exempt 函数调用作为 sink '
                            '参数时失败(失败关闭,需配合 --cross-function 使用)')
    # R62 P1-05: 自动枚举用户面向 sink(FastAPI/Telegram/WebSocket/SSE/
    # mail/notification/template)用于审计确认
    parser.add_argument('--enumerate-sinks', action='store_true',
                        help='R62 P1-05: 自动枚举所有用户面向 sink(FastAPI/Telegram/'
                            'WebSocket/SSE/mail/notification/template)')
    args = parser.parse_args(argv)

    root = Path(__file__).parent.parent

    # R62 P1-05: cross-function 分析(独立命令,不加载 baseline)
    if args.cross_function or args.fail_on_unknown_sink:
        return cmd_cross_function(
            root, fail_on_unknown_sink=args.fail_on_unknown_sink,
        )

    # R62 P1-05: 自动枚举 sink(独立审计命令,不加载 baseline)
    if args.enumerate_sinks:
        return cmd_enumerate_sinks(root)

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
