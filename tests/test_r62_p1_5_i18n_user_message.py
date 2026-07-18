"""R62 P1-05: 统一用户面消息类型 UserMessage + scanner cross-function 分析测试。

审计报告 P1-05 整改覆盖:
    1. ``UserMessage`` 统一用户面消息类型(frozen dataclass + i18n key + 脱敏 params)
    2. scanner cross-function source-to-sink 分析(变量回溯 / 函数返回值传播)
    3. 自动枚举 FastAPI / Telegram / WebSocket / SSE / mail / notification / template sinks
    4. ``--fail-on-unknown-sink`` 生产构建门禁(未知 sink 来源失败关闭)
    5. params 脱敏(过滤 password / secret / token 等敏感字段)

测试组织:
    - ``TestUserMessageCreation``         — 创建 / 字段 / 工厂方法
    - ``TestUserMessageRender``           — render() 本地化字符串
    - ``TestUserMessageFromError``        — from_error() 从 AppError 转换
    - ``TestUserMessageFrozen``           — frozen 不可变
    - ``TestUserMessageParamSanitization``— params 脱敏
    - ``TestScannerCrossFunction``       — cross-function 变量追踪
    - ``TestScannerFailOnUnknownSink``   — --fail-on-unknown-sink 门禁
    - ``TestEnumerateUserFacingSinks``   — 自动枚举各类 sink
    - ``TestScannerRegistryAndExempt``   — 注册表 / exempt / 协议常量
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# 让测试能导入 scripts/scan_hardcoded_strings.py
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_hardcoded_strings as scan  # noqa: E402
from services.user_message import (  # noqa: E402
    UserMessage,
    _is_sensitive_key,
    _sanitize_params,
)


# ─── 辅助函数 ──────────────────────────────────────────────────

def _ptypes(findings) -> list[str]:
    """从 findings 抽取 pattern_type 列表(便于断言 call-chain)。"""
    return [pt for _ln, pt, _ct in findings]


def _texts(findings) -> list[str]:
    """从 findings 抽取字面量文本列表(便于断言)。"""
    return [ct for _ln, _pt, ct in findings]


def _categories(sinks) -> list[str]:
    """从 enumerate_user_facing_sinks 结果抽取分类列表。"""
    return [cat for _ln, cat, _repr in sinks]


# ===========================================================================
# 1. UserMessage 创建 / 字段 / 工厂方法
# ===========================================================================

class TestUserMessageCreation:
    """R62 P1-05: UserMessage 创建与字段。"""

    def test_user_message_creation_with_all_fields(self):
        """UserMessage 创建时所有字段正确保存。"""
        msg = UserMessage(
            message_key="bot.upload_banned",
            locale="zh-CN",
            params={"file_code": "ABC123"},
            error_code="UPLOAD.COPY.TELEGRAM_TIMEOUT",
            trace_id="trace-abc-001",
        )
        assert msg.message_key == "bot.upload_banned"
        assert msg.locale == "zh-CN"
        assert msg.params == {"file_code": "ABC123"}
        assert msg.error_code == "UPLOAD.COPY.TELEGRAM_TIMEOUT"
        assert msg.trace_id == "trace-abc-001"

    def test_user_message_default_values(self):
        """UserMessage 默认值:locale=zh-CN / params={} / error_code=None / trace_id=None。"""
        msg = UserMessage(message_key="bot.upload_banned")
        assert msg.locale == "zh-CN"
        assert msg.params == {}
        assert msg.error_code is None
        assert msg.trace_id is None

    def test_user_message_from_key_factory(self):
        """UserMessage.from_key() 工厂方法构造(语义化入口)。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            locale="zh-CN",
            params={"file_code": "ABC"},
            error_code="ERR_001",
            trace_id="trace-xyz",
        )
        assert msg.message_key == "bot.upload_banned"
        assert msg.locale == "zh-CN"
        assert msg.params == {"file_code": "ABC"}
        assert msg.error_code == "ERR_001"
        assert msg.trace_id == "trace-xyz"

    def test_user_message_from_key_with_no_params(self):
        """UserMessage.from_key() 不传 params 时默认空 dict。"""
        msg = UserMessage.from_key("bot.upload_banned")
        assert msg.params == {}


# ===========================================================================
# 2. UserMessage.render() 本地化字符串
# ===========================================================================

class TestUserMessageRender:
    """R62 P1-05: UserMessage.render() 通过 i18n_manager 渲染本地化字符串。"""

    def test_render_produces_localized_string_with_simple_var(self):
        """render() 用 {count} 占位符插值产生正确的本地化字符串。

        使用 locales/zh-CN.json 中的 ``bot.quota_remaining``:
            "今日剩余配额 {count} 次"
        """
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        msg = UserMessage.from_key(
            "bot.quota_remaining",
            locale="zh-CN",
            params={"count": 5},
        )
        rendered = msg.render(manager)
        # 应展开为 "今日剩余配额 5 次"
        assert "5" in rendered, f"rendered 应包含 count=5: {rendered}"
        assert "今日剩余配额" in rendered, f"rendered 应包含中文文案: {rendered}"

    def test_render_produces_localized_string_without_params(self):
        """render() 无 params 时仍能渲染(无插值的 key)。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        msg = UserMessage.from_key("bot.upload_banned", locale="zh-CN")
        rendered = msg.render(manager)
        # 应渲染为 "您被禁止使用上传功能"
        assert "禁止" in rendered, f"rendered 应包含 '禁止': {rendered}"

    def test_render_uses_locale_for_localization(self):
        """render() 根据 locale 字段选择对应语言。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        # zh-CN 渲染
        msg_zh = UserMessage.from_key("bot.upload_banned", locale="zh-CN")
        rendered_zh = msg_zh.render(manager)
        # en-US 渲染(若存在 en-US 翻译)
        msg_en = UserMessage.from_key("bot.upload_banned", locale="en-US")
        rendered_en = msg_en.render(manager)
        # zh-CN 必须含中文
        assert "禁止" in rendered_zh, f"zh-CN 应含中文: {rendered_zh}"
        # 两次渲染均非空(具体内容取决于 locale 文件)
        assert rendered_zh
        assert rendered_en


# ===========================================================================
# 3. UserMessage.from_error() 从 AppError 转换
# ===========================================================================

class TestUserMessageFromError:
    """R62 P1-05: UserMessage.from_error() 桥接 AppError → UserMessage。"""

    def test_from_error_converts_app_error_correctly(self):
        """from_error() 正确转换 AppError 的所有字段。

        AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT) 的 envelope:
            - message_key = "errors.upload.copy.telegram_timeout"
            - params = {"file_code": ...} (经 safe_params 白名单过滤)
            - code = "UPLOAD.COPY.TELEGRAM_TIMEOUT"
            - trace_id = 自动生成 UUID
        """
        from services.error_codes import AppError, ErrorCodes
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE_ABC"},
        )
        msg = UserMessage.from_error(app_error, locale="zh-CN")
        # message_key 应来自 envelope
        assert msg.message_key == app_error.envelope.message_key, (
            f"message_key 应来自 envelope: {msg.message_key}"
        )
        # error_code 应为 AppError.code
        assert msg.error_code == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        # trace_id 应贯穿全链路
        assert msg.trace_id == app_error.trace_id
        assert msg.trace_id == app_error.envelope.trace_id
        # locale 应正确传递
        assert msg.locale == "zh-CN"

    def test_from_error_renders_localized_message(self):
        """from_error() 产出的 UserMessage 可正确渲染为本地化字符串。"""
        from services.i18n import get_i18n_manager
        from services.error_codes import AppError, ErrorCodes
        manager = get_i18n_manager()
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE_XYZ"},
        )
        msg = UserMessage.from_error(app_error, locale="zh-CN")
        rendered = msg.render(manager)
        # 应包含 file_code 插值
        assert "FILE_XYZ" in rendered, (
            f"rendered 应包含 file_code=FILE_XYZ: {rendered}"
        )

    def test_from_error_preserves_trace_id_for_audit(self):
        """from_error() 保留 trace_id 用于全链路日志关联。"""
        from services.error_codes import AppError, ErrorCodes
        custom_trace_id = "trace-from-error-001"
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "abc"},
            trace_id=custom_trace_id,
        )
        msg = UserMessage.from_error(app_error, locale="zh-CN")
        assert msg.trace_id == custom_trace_id


# ===========================================================================
# 4. UserMessage frozen 不可变
# ===========================================================================

class TestUserMessageFrozen:
    """R62 P1-05: UserMessage 是 frozen dataclass(不可变)。"""

    def test_user_message_is_frozen_dataclass(self):
        """UserMessage 应使用 @dataclass(frozen=True) 装饰。"""
        assert dataclasses.is_dataclass(UserMessage)
        params = getattr(UserMessage, "__dataclass_params__", None)
        assert params is not None, "UserMessage 应有 __dataclass_params__"
        assert params.frozen is True, "UserMessage 应为 frozen=True"

    def test_user_message_cannot_set_attribute(self):
        """frozen UserMessage 不允许设置新属性(FrozenInstanceError)。"""
        msg = UserMessage(message_key="bot.upload_banned")
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.message_key = "other.key"  # type: ignore[misc]

    def test_user_message_cannot_set_params(self):
        """frozen UserMessage 不允许修改 params 字段。"""
        msg = UserMessage.from_key("bot.upload_banned", params={"a": 1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.params = {"b": 2}  # type: ignore[misc]

    def test_user_message_cannot_delete_attribute(self):
        """frozen UserMessage 不允许删除属性。"""
        msg = UserMessage(message_key="bot.upload_banned")
        with pytest.raises(dataclasses.FrozenInstanceError):
            del msg.message_key  # type: ignore[misc]


# ===========================================================================
# 5. UserMessage params 脱敏
# ===========================================================================

class TestUserMessageParamSanitization:
    """R62 P1-05: params 在构造时即脱敏(过滤敏感字段)。"""

    def test_sensitive_key_detection(self):
        """_is_sensitive_key 正确识别敏感 key。"""
        # 敏感 key
        assert _is_sensitive_key("password") is True
        assert _is_sensitive_key("api_token") is True
        assert _is_sensitive_key("user_secret") is True
        assert _is_sensitive_key("credential") is True
        assert _is_sensitive_key("private_key") is True
        assert _is_sensitive_key("session_id") is True
        assert _is_sensitive_key("cookie_value") is True
        assert _is_sensitive_key("api_key") is True
        # 大小写不敏感
        assert _is_sensitive_key("PASSWORD") is True
        assert _is_sensitive_key("ApiToken") is True

    def test_non_sensitive_key_not_flagged(self):
        """_is_sensitive_key 不应误报非敏感 key。"""
        assert _is_sensitive_key("file_code") is False
        assert _is_sensitive_key("user_id") is False
        assert _is_sensitive_key("channel_id") is False
        assert _is_sensitive_key("count") is False
        assert _is_sensitive_key("code") is False

    def test_sanitize_params_filters_password(self):
        """_sanitize_params 过滤 password 字段。"""
        params = {"file_code": "ABC", "password": "super_secret_pwd"}
        sanitized = _sanitize_params(params)
        assert sanitized == {"file_code": "ABC"}
        assert "password" not in sanitized

    def test_sanitize_params_filters_secret_and_token(self):
        """_sanitize_params 过滤 secret / token / api_key 等多种敏感字段。"""
        params = {
            "user_id": 123,
            "secret": "api_secret_value",
            "access_token": "tok_abc",
            "api_key": "key_xyz",
            "credential": "cred_001",
        }
        sanitized = _sanitize_params(params)
        # 仅 user_id 应保留
        assert sanitized == {"user_id": 123}
        for sensitive_key in ("secret", "access_token", "api_key", "credential"):
            assert sensitive_key not in sanitized, (
                f"{sensitive_key} 应被过滤: {sanitized}"
            )

    def test_sanitize_params_filters_none_values(self):
        """_sanitize_params 过滤 None 值(避免 'None' 字面量泄漏)。"""
        params = {"file_code": "ABC", "user_id": None, "channel_id": None}
        sanitized = _sanitize_params(params)
        assert sanitized == {"file_code": "ABC"}

    def test_sanitize_params_filters_long_strings(self):
        """_sanitize_params 过滤超长字符串(>200,可能含密文/哈希)。"""
        long_str = "x" * 201
        params = {"file_code": "ABC", "maybe_hash": long_str}
        sanitized = _sanitize_params(params)
        assert sanitized == {"file_code": "ABC"}
        assert "maybe_hash" not in sanitized

    def test_sanitize_params_returns_defensive_copy(self):
        """_sanitize_params 返回新 dict(不修改原 dict)。"""
        original = {"file_code": "ABC", "password": "pwd"}
        sanitized = _sanitize_params(original)
        # 原 dict 不应被修改
        assert original == {"file_code": "ABC", "password": "pwd"}
        # sanitized 应是新 dict
        assert sanitized is not original
        assert sanitized == {"file_code": "ABC"}

    def test_sanitize_params_handles_empty_and_none(self):
        """_sanitize_params 处理 None / 空 dict。"""
        assert _sanitize_params(None) == {}
        assert _sanitize_params({}) == {}

    def test_user_message_sanitizes_params_on_construction(self):
        """UserMessage 构造时即过滤敏感字段(防御性拷贝)。"""
        msg = UserMessage(
            message_key="bot.upload_banned",
            params={
                "file_code": "ABC",
                "password": "super_secret",
                "api_token": "tok_xyz",
            },
        )
        # 敏感字段应被过滤
        assert "password" not in msg.params
        assert "api_token" not in msg.params
        # 非敏感字段应保留
        assert msg.params == {"file_code": "ABC"}

    def test_user_message_params_defensive_copy_isolated(self):
        """UserMessage.params 与构造时传入的 dict 隔离(后续修改不影响)。"""
        original_params = {"file_code": "ABC", "password": "pwd"}
        msg = UserMessage(message_key="bot.upload_banned", params=original_params)
        # 修改原 dict 不应影响 UserMessage.params
        original_params["file_code"] = "MODIFIED"
        original_params["new_key"] = "new_value"
        assert msg.params == {"file_code": "ABC"}, (
            f"UserMessage.params 应隔离,不受原 dict 修改影响: {msg.params}"
        )


# ===========================================================================
# 6. Scanner cross-function 变量追踪
# ===========================================================================

class TestScannerCrossFunction:
    """R62 P1-05: scanner cross-function source-to-sink 分析。"""

    def test_cross_function_detects_variable_tracked_literal(self):
        """cross-function 检测变量追踪的字符串字面量。

        场景:变量赋值字面量后传入 sink(wrapper / 别名漏检修复)
            msg = "Please log in first"
            reply_text(msg)
        → 应被 cross-function 标记(pattern_type 'sink:reply_text.var<msg>')
        """
        src = (
            'def handler(update):\n'
            '    msg = "Please log in first"\n'
            '    update.message.reply_text(msg)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 finding(变量 msg 回溯到字面量)
        assert len(findings) == 1, f"期望 1 条 cross-function finding,实际 {findings}"
        _ln, ptype, text = findings[0]
        # ptype 应含 sink:reply_text + var<msg>
        assert ptype.startswith('sink:reply_text'), f"ptype={ptype}"
        assert 'var<msg>' in ptype, f"ptype 应含 var<msg>: {ptype}"
        # text 应是字面量内容
        assert text == 'Please log in first', f"text={text}"

    def test_cross_function_detects_kwarg_variable_tracked_literal(self):
        """cross-function 检测 kwarg 变量追踪(text= / detail= 等)。"""
        src = (
            'def handler():\n'
            '    msg = "Welcome back"\n'
            '    send_message(chat_id=123, text=msg)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 finding(text= msg 回溯到字面量)
        assert len(findings) == 1, f"期望 1 条 finding,实际 {findings}"
        _ln, ptype, _text = findings[0]
        # ptype 应含 send_message + text + var<msg>
        assert 'send_message' in ptype, f"ptype={ptype}"
        assert 'text' in ptype, f"ptype 应含 text kwarg: {ptype}"
        assert 'var<msg>' in ptype, f"ptype 应含 var<msg>: {ptype}"

    def test_cross_function_exempt_call_not_flagged(self):
        """exempt 函数(_i18n_t)的返回值作为 sink 参数时不应被标记。"""
        src = (
            'from services.i18n import translate as _i18n_t\n'
            'def handler(update):\n'
            '    msg = _i18n_t("bot.welcome")\n'
            '    update.message.reply_text(msg)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # _i18n_t 是 exempt,变量回溯到 exempt 调用 → 不标记
        assert findings == [], (
            f"exempt Call (_i18n_t) 不应被标记,但得到 findings={findings}"
        )

    def test_cross_function_user_message_exempt_not_flagged(self):
        """UserMessage.from_key() 返回值作为 sink 参数时不应被标记。"""
        src = (
            'from services.user_message import UserMessage\n'
            'from services.i18n import get_i18n_manager\n'
            'def handler(update):\n'
            '    msg = UserMessage.from_key("bot.welcome")\n'
            '    update.message.reply_text(msg.render(get_i18n_manager()))\n'
        )
        # 注意:UserMessage.from_key 返回 UserMessage 对象,render() 才转字符串
        # sink 参数是 msg.render(...) 这个 Call → exempt(UserMessage.from_key 是 exempt,
        # 但 render 不是,这里仅验证 _build_user_message 类似的模式不会误报)
        # 实际 render(...) 不在 PYTHON_SINK_FUNCS,所以不会被识别为 sink
        findings = scan.scan_python_content_cross_function(src)
        # reply_text 位置参数是 Call(render),不展开 — 无 finding(默认 --fail-on-unknown-sink=False)
        # 这是 cross-function 的设计选择(避免位置参数 unknown_call 误伤)
        assert findings == [], (
            f"UserMessage.render() 模式不应被标记: {findings}"
        )

    def test_cross_function_protocol_constant_not_flagged(self):
        """协议常量("OK" / "200" 等)流入 sink 不应被标记。"""
        src = (
            'def handler():\n'
            '    status = "OK"\n'
            '    send_message(chat_id=1, text=status)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # "OK" 是协议常量 → 不标记
        assert findings == [], (
            f"协议常量不应被标记,但得到 findings={findings}"
        )

    def test_cross_function_detects_sse_yield(self):
        """cross-function 检测 SSE yield f"data: ..." 模式。"""
        src = (
            'async def sse_handler():\n'
            '    yield f"data: hello world\\n\\n"\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 SSE yield finding
        sse_findings = [f for f in findings if f[1] == 'sse:yield']
        assert len(sse_findings) == 1, (
            f"期望 1 条 sse:yield finding,实际 {findings}"
        )
        _ln, ptype, text = sse_findings[0]
        assert ptype == 'sse:yield', f"ptype={ptype}"
        assert 'data: hello' in text, f"text 应含 'data: hello': {text}"

    def test_cross_function_detects_sse_yield_constant(self):
        """cross-function 检测 SSE yield "data: ..." 常量模式。"""
        src = (
            'async def sse_handler():\n'
            '    yield "data: ping\\n\\n"\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        sse_findings = [f for f in findings if f[1] == 'sse:yield']
        assert len(sse_findings) == 1, f"期望 1 条 sse:yield,实际 {findings}"

    def test_cross_function_detects_fastapi_dict_return(self):
        """cross-function 检测 FastAPI dict 返回:return {"message": "..."}。"""
        src = (
            'def handler():\n'
            '    return {"message": "Operation succeeded"}\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 fastapi:return_dict finding
        fastapi_findings = [
            f for f in findings if f[1].startswith('fastapi:return_dict')
        ]
        assert len(fastapi_findings) == 1, (
            f"期望 1 条 fastapi:return_dict finding,实际 {findings}"
        )
        _ln, ptype, text = fastapi_findings[0]
        assert ptype == 'fastapi:return_dict.dict[message]', f"ptype={ptype}"
        assert text == 'Operation succeeded', f"text={text}"

    def test_cross_function_detects_websocket_send_variable(self):
        """cross-function 检测 websocket.send(变量) — WebSocket receiver.send()。

        注意:_describe_sink_chain 通过 _get_call_name 取方法名(websocket.send → "send"),
        所以 ptype 是 'sink:send.var<msg>'(不带 ws: 前缀)。
        WebSocket 身份通过 _is_websocket_send_call 双重匹配(receiver + 方法)确认。
        """
        src = (
            'async def ws_handler(websocket):\n'
            '    msg = "Connection established"\n'
            '    await websocket.send(msg)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 sink:send finding(websocket.send 识别为 sink,变量回溯到字面量)
        ws_findings = [f for f in findings if 'sink:send' in f[1]]
        assert len(ws_findings) == 1, (
            f"期望 1 条 sink:send finding,实际 {findings}"
        )
        _ln, ptype, text = ws_findings[0]
        assert ptype.startswith('sink:send'), f"ptype={ptype}"
        assert 'var<msg>' in ptype, f"ptype 应含 var<msg>: {ptype}"
        assert text == 'Connection established', f"text={text}"

    def test_cross_function_recurses_through_chained_variables(self):
        """cross-function 递归回溯链式变量(a → b → 字面量)。

        场景:变量赋值自另一个变量,链尾是字面量
            raw = "Welcome"
            msg = raw
            reply_text(msg)
        → _trace_variable_source 返回 kind="literal", chain=('var<msg>', 'var<raw>')
        → _follow_var_chain_to_literal 沿链回溯到 "Welcome"
        → 应被标记(避免 wrapper / 别名漏检,审计 P1-05 核心要求)
        """
        src = (
            'def handler(update):\n'
            '    raw = "Welcome"\n'
            '    msg = raw\n'
            '    update.message.reply_text(msg)\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 应有 1 条 finding(链式回溯 a → b → 字面量)
        assert len(findings) == 1, f"期望 1 条 finding,实际 {findings}"
        _ln, ptype, text = findings[0]
        # chain 应含 var<msg> + var<raw>
        assert 'var<msg>' in ptype, f"ptype={ptype}"
        assert 'var<raw>' in ptype, f"ptype 应含 var<raw>: {ptype}"
        assert text == 'Welcome', f"text={text}"

    def test_trace_variable_source_handles_chained_variables(self):
        """_trace_variable_source 正确处理链式变量(单元测试)。"""
        import ast
        src = (
            'def handler():\n'
            '    raw = "literal_value"\n'
            '    msg = raw\n'
            '    pass\n'
        )
        tree = ast.parse(src)
        func = tree.body[0]
        assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        var_map = scan._build_function_var_map(func)
        # msg -> raw -> "literal_value"
        kind, chain = scan._trace_variable_source("msg", var_map)
        assert kind == "literal", f"kind 应为 literal: {kind}"
        assert chain == ("var<msg>", "var<raw>"), f"chain 应含 var<msg> + var<raw>: {chain}"
        # _follow_var_chain_to_literal 沿链回溯到字面量
        text = scan._follow_var_chain_to_literal("msg", var_map)
        assert text == "literal_value", f"text 应为字面量: {text}"


# ===========================================================================
# 7. Scanner --fail-on-unknown-sink 门禁
# ===========================================================================

class TestScannerFailOnUnknownSink:
    """R62 P1-05: --fail-on-unknown-sink 生产构建门禁(未知 sink 来源失败关闭)。"""

    def test_fail_on_unknown_sink_fails_on_unknown_function(self):
        """--fail-on-unknown-sink 在未知 sink 函数上失败。

        场景:sink 参数是未知函数调用(非 exempt)
            send_message(text=some_func())
        → --fail-on-unknown-sink=True 时标记 pattern_type 'unknown_call'
        """
        src = (
            'def handler():\n'
            '    send_message(chat_id=1, text=some_func())\n'
        )
        findings = scan.scan_python_content_cross_function(
            src, fail_on_unknown_sink=True,
        )
        # 应有 1 条 unknown_call finding
        unknown_findings = [f for f in findings if 'unknown_call' in f[1]]
        assert len(unknown_findings) == 1, (
            f"期望 1 条 unknown_call finding,实际 {findings}"
        )
        _ln, ptype, text = unknown_findings[0]
        assert 'unknown_call' in ptype, f"ptype={ptype}"
        # text 应是函数名
        assert text == 'some_func', f"text 应是函数名: {text}"

    def test_fail_on_unknown_sink_fails_on_variable_from_unknown_call(self):
        """--fail-on-unknown-sink 在变量赋值自未知函数时失败。

        场景:变量赋值来自非 exempt 函数 → sink 参数为该变量
            msg = some_func()
            send_message(text=msg)
        → --fail-on-unknown-sink=True 时标记 'unknown_call'
        """
        src = (
            'def handler():\n'
            '    msg = some_func()\n'
            '    send_message(chat_id=1, text=msg)\n'
        )
        findings = scan.scan_python_content_cross_function(
            src, fail_on_unknown_sink=True,
        )
        unknown_findings = [f for f in findings if 'unknown_call' in f[1]]
        assert len(unknown_findings) == 1, (
            f"期望 1 条 unknown_call finding,实际 {findings}"
        )

    def test_fail_on_unknown_sink_passes_on_exempt_call(self):
        """--fail-on-unknown-sink 对 exempt 函数(_i18n_t)不标记。"""
        src = (
            'from services.i18n import translate as _i18n_t\n'
            'def handler():\n'
            '    send_message(chat_id=1, text=_i18n_t("bot.welcome"))\n'
        )
        findings = scan.scan_python_content_cross_function(
            src, fail_on_unknown_sink=True,
        )
        # _i18n_t 是 exempt → 不标记
        assert findings == [], (
            f"exempt Call 不应被标记,但得到 findings={findings}"
        )

    def test_fail_on_unknown_sink_passes_on_user_message(self):
        """--fail-on-unknown-sink 对 UserMessage.from_key() 不标记。"""
        src = (
            'from services.user_message import UserMessage\n'
            'def handler():\n'
            '    send_message(chat_id=1, text=UserMessage.from_key("bot.welcome"))\n'
        )
        findings = scan.scan_python_content_cross_function(
            src, fail_on_unknown_sink=True,
        )
        # UserMessage.from_key 是 exempt → 不标记
        assert findings == [], (
            f"UserMessage.from_key() 不应被标记: {findings}"
        )

    def test_fail_on_unknown_sink_default_off(self):
        """默认 fail_on_unknown_sink=False(未知函数调用不标记)。"""
        src = (
            'def handler():\n'
            '    send_message(chat_id=1, text=some_func())\n'
        )
        findings = scan.scan_python_content_cross_function(src)
        # 默认 False → 不标记 unknown_call(避免开发环境噪声)
        unknown_findings = [f for f in findings if 'unknown_call' in f[1]]
        assert unknown_findings == [], (
            f"默认 fail_on_unknown_sink=False 时不应标记 unknown_call: {findings}"
        )


# ===========================================================================
# 8. 自动枚举各类 sink (enumerate_user_facing_sinks)
# ===========================================================================

class TestEnumerateUserFacingSinks:
    """R62 P1-05: scanner 自动枚举 FastAPI/Telegram/WebSocket/SSE/mail/
    notification/template/http_exception sinks。"""

    def test_enumerate_fastapi_sinks(self):
        """自动枚举 FastAPI 响应 sink(JSONResponse / HTMLResponse / Response)。"""
        src = (
            'from fastapi.responses import JSONResponse, HTMLResponse\n'
            'def handler():\n'
            '    return JSONResponse(content={"msg": "ok"})\n'
            '    return HTMLResponse(content="<p>hi</p>")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 2 个 fastapi sink
        assert cats.count('fastapi') == 2, (
            f"期望 2 个 fastapi sink,实际 categories={cats}"
        )

    def test_enumerate_telegram_sinks(self):
        """自动枚举 Telegram Bot sink(reply_text / send_message / answer_*)。"""
        src = (
            'def handler(update, context):\n'
            '    update.message.reply_text("hi")\n'
            '    context.bot.send_message(chat_id=1, text="msg")\n'
            '    query.answer_callback_query(text="ack")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 3 个 telegram sink
        assert cats.count('telegram') == 3, (
            f"期望 3 个 telegram sink,实际 categories={cats}"
        )

    def test_enumerate_websocket_sinks(self):
        """自动枚举 WebSocket sink(websocket.send / ws.send_text / socket.send_json)。"""
        src = (
            'async def ws_handler(websocket):\n'
            '    await websocket.send("hi")\n'
            '    await websocket.send_text("hello")\n'
            '    await websocket.send_json({"k": "v"})\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 3 个 websocket sink
        assert cats.count('websocket') == 3, (
            f"期望 3 个 websocket sink,实际 categories={cats}, sinks={sinks}"
        )

    def test_enumerate_websocket_with_ws_receiver_name(self):
        """WebSocket receiver 名为 'ws' 时也应被识别。"""
        src = (
            'async def handler(ws):\n'
            '    await ws.send_text("hi")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        assert 'websocket' in cats, (
            f"ws.send_text 应识别为 websocket sink: {cats}"
        )

    def test_enumerate_sse_sinks(self):
        """自动枚举 SSE sink(yield f"data: ..." 模式)。"""
        src = (
            'async def sse_handler():\n'
            '    yield f"data: hello\\n\\n"\n'
            '    yield "data: world\\n\\n"\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 2 个 sse sink
        assert cats.count('sse') == 2, (
            f"期望 2 个 sse sink,实际 categories={cats}"
        )

    def test_enumerate_mail_sinks(self):
        """自动枚举邮件 sink(send_mail / send_email / email_message)。"""
        src = (
            'def handler():\n'
            '    send_mail(subject="Hi", body="Welcome")\n'
            '    send_email(to="x@y.com", subject="Hi")\n'
            '    email_message(content="Body")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 3 个 mail sink
        assert cats.count('mail') == 3, (
            f"期望 3 个 mail sink,实际 categories={cats}"
        )

    def test_enumerate_notification_sinks(self):
        """自动枚举通知 sink(notify / push_notification / send_notification)。"""
        src = (
            'def handler():\n'
            '    notify(message="Hi")\n'
            '    push_notification(text="Hello")\n'
            '    send_notification(content="Body")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 3 个 notification sink
        assert cats.count('notification') == 3, (
            f"期望 3 个 notification sink,实际 categories={cats}"
        )

    def test_enumerate_template_sinks(self):
        """自动枚举模板 sink(TemplateResponse / render / render_template)。"""
        src = (
            'def handler(request):\n'
            '    return TemplateResponse(request, "x.html", context={})\n'
            '    render("template.html")\n'
            '    render_template("template.html")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 3 个 template sink
        assert cats.count('template') == 3, (
            f"期望 3 个 template sink,实际 categories={cats}"
        )

    def test_enumerate_http_exception_sinks(self):
        """自动枚举 HTTPException sink。"""
        src = (
            'def handler():\n'
            '    raise HTTPException(status_code=404, detail="Not found")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        # 应有 1 个 http_exception sink
        assert cats.count('http_exception') == 1, (
            f"期望 1 个 http_exception sink,实际 categories={cats}"
        )

    def test_enumerate_sse_starlette_event_source_response(self):
        """EventSourceResponse 是 fastapi 类 sink(SSE 响应)。"""
        src = (
            'from sse_starlette import EventSourceResponse\n'
            'def handler():\n'
            '    return EventSourceResponse(content={"event": "ping"})\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        cats = _categories(sinks)
        assert 'fastapi' in cats, (
            f"EventSourceResponse 应识别为 fastapi sink: {cats}"
        )

    def test_enumerate_skips_exempt_funcs(self):
        """豁免函数(_i18n_t / UserMessage / AppError)不计入 sink 出口。"""
        src = (
            'from services.i18n import translate as _i18n_t\n'
            'from services.user_message import UserMessage\n'
            'from services.error_codes import AppError\n'
            'def handler():\n'
            '    _i18n_t("bot.welcome")\n'
            '    UserMessage.from_key("bot.welcome")\n'
            '    AppError("ERR_CODE")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        # 豁免函数不应被枚举为 sink
        assert sinks == [], (
            f"豁免函数不应被枚举为 sink: {sinks}"
        )

    def test_enumerate_skips_logger_calls(self):
        """logger.*/print() 不算用户面 sink。"""
        src = (
            'from loguru import logger\n'
            'def handler():\n'
            '    logger.info("processing")\n'
            '    logger.warning("warning")\n'
            '    print("debug")\n'
        )
        sinks = scan.enumerate_user_facing_sinks(src)
        # logger / print 不应被枚举为 sink
        assert sinks == [], (
            f"logger/print 不应被枚举为 sink: {sinks}"
        )


# ===========================================================================
# 9. Scanner 注册表 / exempt / 协议常量
# ===========================================================================

class TestScannerRegistryAndExempt:
    """R62 P1-05: scanner 注册表完整性 + exempt + 协议常量。"""

    def test_cross_function_analysis_version_constant(self):
        """CROSS_FUNCTION_ANALYSIS_VERSION 常量已定义(独立于 SCANNER_VERSION)。"""
        assert hasattr(scan, 'CROSS_FUNCTION_ANALYSIS_VERSION')
        assert scan.CROSS_FUNCTION_ANALYSIS_VERSION == '7.0-r62-p1-05'

    def test_user_message_in_exempt_funcs(self):
        """UserMessage / from_key / from_error 已加入 PYTHON_EXEMPT_FUNCS。"""
        assert 'UserMessage' in scan.PYTHON_EXEMPT_FUNCS
        assert 'from_key' in scan.PYTHON_EXEMPT_FUNCS
        assert 'from_error' in scan.PYTHON_EXEMPT_FUNCS

    def test_event_source_response_in_sink_funcs(self):
        """EventSourceResponse 已加入 PYTHON_SINK_FUNCS(SSE 响应)。"""
        assert 'EventSourceResponse' in scan.PYTHON_SINK_FUNCS

    def test_websocket_receiver_names_defined(self):
        """_WEBSOCKET_RECEIVER_NAMES 集合包含 websocket/ws/socket/websockets。"""
        assert 'websocket' in scan._WEBSOCKET_RECEIVER_NAMES
        assert 'ws' in scan._WEBSOCKET_RECEIVER_NAMES
        assert 'socket' in scan._WEBSOCKET_RECEIVER_NAMES
        assert 'websockets' in scan._WEBSOCKET_RECEIVER_NAMES

    def test_websocket_send_methods_defined(self):
        """_WEBSOCKET_SEND_METHODS 集合包含 send/send_text/send_json/send_bytes。"""
        assert 'send' in scan._WEBSOCKET_SEND_METHODS
        assert 'send_text' in scan._WEBSOCKET_SEND_METHODS
        assert 'send_json' in scan._WEBSOCKET_SEND_METHODS
        assert 'send_bytes' in scan._WEBSOCKET_SEND_METHODS

    def test_cross_function_user_facing_kwargs_defined(self):
        """_CROSS_FUNCTION_USER_FACING_KWARGS 集合包含关键用户面向 kwargs。"""
        for kwarg in ('text', 'detail', 'message', 'caption', 'description',
                      'content', 'context', 'subject', 'body', 'data',
                      'payload', 'event', 'text_data', 'json_data'):
            assert kwarg in scan._CROSS_FUNCTION_USER_FACING_KWARGS, (
                f"{kwarg} 应在 _CROSS_FUNCTION_USER_FACING_KWARGS 中"
            )

    def test_cross_function_user_facing_kwargs_excludes_structural(self):
        """_CROSS_FUNCTION_USER_FACING_KWARGS 不含结构性 kwargs(reply_markup 等)。

        这避免 cross-function 扫描器误报 reply_markup / parse_mode /
        disable_notification 等结构性 kwargs(非用户面文本)。
        """
        for structural in ('reply_markup', 'parse_mode', 'disable_notification',
                           'chat_id', 'message_id', 'status_code', 'headers'):
            assert structural not in scan._CROSS_FUNCTION_USER_FACING_KWARGS, (
                f"{structural} 不应在 _CROSS_FUNCTION_USER_FACING_KWARGS 中(结构性 kwargs)"
            )

    def test_classify_sink_category_returns_correct_categories(self):
        """_classify_sink_category 按出口类型分类。"""
        import ast
        # HTTPException → http_exception
        node = ast.parse('HTTPException(status_code=404, detail="x")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'HTTPException') == 'http_exception'
        # JSONResponse → fastapi
        node = ast.parse('JSONResponse(content={})').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'JSONResponse') == 'fastapi'
        # send_message → telegram
        node = ast.parse('send_message(text="x")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'send_message') == 'telegram'
        # send_mail → mail
        node = ast.parse('send_mail(subject="x")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'send_mail') == 'mail'
        # notify → notification
        node = ast.parse('notify(message="x")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'notify') == 'notification'
        # TemplateResponse → template
        node = ast.parse('TemplateResponse("x.html")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'TemplateResponse') == 'template'

    def test_classify_sink_category_returns_none_for_non_sink(self):
        """_classify_sink_category 对非用户面调用返回 None。"""
        import ast
        # 非用户面调用 → None
        node = ast.parse('len("x")').body[0].value  # type: ignore[attr-defined]
        assert scan._classify_sink_category(node, 'len') is None

    def test_is_websocket_send_call_detection(self):
        """_is_websocket_send_call 双重匹配 receiver + 方法名。"""
        import ast
        # 匹配:websocket.send
        tree = ast.parse('websocket.send("hi")')
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert scan._is_websocket_send_call(call) is True
        # 匹配:ws.send_text
        tree = ast.parse('ws.send_text("hi")')
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert scan._is_websocket_send_call(call) is True
        # 不匹配:非 websocket receiver
        tree = ast.parse('queue.send("hi")')
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert scan._is_websocket_send_call(call) is False
        # 不匹配:websocket.recv(非 send 方法)
        tree = ast.parse('websocket.recv()')
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert scan._is_websocket_send_call(call) is False

    def test_is_sse_yield_data_detection(self):
        """_is_sse_yield_data 检测 yield f"data: ..." 模式。"""
        import ast
        # 匹配:yield f"data: ..."
        tree = ast.parse('def f():\n    yield f"data: hi\\n\\n"')
        yield_node = tree.body[0].body[0].value  # type: ignore[attr-defined]
        assert scan._is_sse_yield_data(yield_node) is True
        # 匹配:yield "data: ..."
        tree = ast.parse('def f():\n    yield "data: hi\\n\\n"')
        yield_node = tree.body[0].body[0].value  # type: ignore[attr-defined]
        assert scan._is_sse_yield_data(yield_node) is True
        # 不匹配:非 data: 前缀
        tree = ast.parse('def f():\n    yield "hello"')
        yield_node = tree.body[0].body[0].value  # type: ignore[attr-defined]
        assert scan._is_sse_yield_data(yield_node) is False


# ===========================================================================
# 10. CLI 命令入口(端到端验证)
# ===========================================================================

class TestScannerCLICommands:
    """R62 P1-05: scanner CLI 命令入口(--cross-function / --enumerate-sinks /
    --fail-on-unknown-sink)端到端可用。

    cmd_cross_function / cmd_enumerate_sinks 内部固定扫描
    ``bots/**/*.py`` / ``admin/**/*.py`` / ``services/**/*.py`` 三个 glob 模式,
    因此临时测试文件需放入对应的子目录才会被扫描到。
    """

    def test_cmd_cross_function_runs_on_clean_file(self, tmp_path):
        """cmd_cross_function 在无违规文件上运行(退出码 0)。"""
        # 构造一个无违规的 Python 文件(全部用 _i18n_t)
        src = (
            'from services.i18n import translate as _i18n_t\n'
            'def handler(update):\n'
            '    update.message.reply_text(_i18n_t("bot.welcome"))\n'
        )
        # 临时 bots/ 子目录,匹配 cmd_cross_function 的 glob 模式
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        test_file = bots_dir / "test_handler.py"
        test_file.write_text(src, encoding='utf-8')
        # cmd_cross_function(root, fail_on_unknown_sink=False)
        exit_code = scan.cmd_cross_function(tmp_path, fail_on_unknown_sink=False)
        # 无违规 → 退出码 0
        assert exit_code == 0, (
            f"无违规文件应退出 0,实际 {exit_code}"
        )

    def test_cmd_cross_function_detects_violation(self, tmp_path, capsys):
        """cmd_cross_function 检测到违规时退出码 1。"""
        src = (
            'def handler(update):\n'
            '    msg = "Please log in first"\n'
            '    update.message.reply_text(msg)\n'
        )
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        test_file = bots_dir / "test_handler.py"
        test_file.write_text(src, encoding='utf-8')
        exit_code = scan.cmd_cross_function(tmp_path, fail_on_unknown_sink=False)
        # 有违规 → 退出码 1
        assert exit_code == 1, (
            f"有违规文件应退出 1,实际 {exit_code}"
        )

    def test_cmd_enumerate_sinks_lists_telegram_sinks(self, tmp_path, capsys):
        """cmd_enumerate_sinks 在含 telegram sink 的文件上运行并列出 sink。"""
        src = (
            'def handler(update, context):\n'
            '    update.message.reply_text("hi")\n'
            '    context.bot.send_message(chat_id=1, text="msg")\n'
        )
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        test_file = bots_dir / "test_handler.py"
        test_file.write_text(src, encoding='utf-8')
        exit_code = scan.cmd_enumerate_sinks(tmp_path)
        # enumerate_sinks 总是退出 0(仅列出 sink,不做门禁)
        assert exit_code == 0, f"cmd_enumerate_sinks 应退出 0,实际 {exit_code}"
        captured = capsys.readouterr()
        # 输出应含 telegram 分类
        assert 'telegram' in captured.out, (
            f"输出应含 'telegram' 分类: {captured.out}"
        )
