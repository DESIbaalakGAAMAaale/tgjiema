"""R64 P1-06: Sink typed adapter + import-boundary AST 门禁测试。

P1-06 整改:
    1. 建立 ``services/sink_adapters/`` typed adapter 包
       (``telegram_adapter`` / ``web_adapter``),只接受
       ``UserMessage | ErrorEnvelope``,拒绝裸 str。
    2. 建立 ``scripts/check_sink_import_boundary.py`` AST 门禁,
       阻止新增直调(存量 baseline ratchet)。
    3. 修复高风险 ``str(e)`` 进 params、``query.message.text`` 语言继承。

测试组织:
    - ``TestTelegramAdapterTypeEnforcement``  — typed adapter 拒绝裸 str
    - ``TestTelegramAdapterAcceptsUserMessage`` — typed adapter 接受 UserMessage
    - ``TestTelegramAdapterNoLanguageInheritance`` — 不拼接 query.message.text
    - ``TestWebAdapterTypeEnforcement`` — web adapter 拒绝裸 str
    - ``TestWebAdapterJsonResponse`` — web adapter 构造 JSONResponse
    - ``TestSinkImportBoundaryScanner`` — AST 门禁脚本检测违规
    - ``TestSinkImportBoundaryBaseline`` — baseline ratchet 机制
    - ``TestCallbackPyR64P1_06Fixes`` — callback.py 高风险问题修复回归
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 让测试能导入 scripts/ 下的模块 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 被测对象
from services.user_message import ErrorEnvelope, UserMessage  # noqa: E402
from services.sink_adapters import (  # noqa: E402
    safe_edit_message_text,
    safe_reply_text,
    safe_send_message,
    json_response,
)
from services.sink_adapters import telegram_adapter  # noqa: E402
from services.sink_adapters import web_adapter  # noqa: E402

import check_sink_import_boundary as scanner  # noqa: E402

# 真实 baseline 文件路径(用于集成测试)
REAL_BASELINE = SCRIPTS_DIR / "sink_import_boundary_baseline.json"

# callback.py 绝对路径(用于 P1-06 修复回归测试)
CALLBACK_PATH = REPO_ROOT / "bots" / "admin_bot" / "callback.py"


# ════════════════════════════════════════════════════════════════
# 1. Telegram adapter 类型强制(拒绝裸 str)
# ════════════════════════════════════════════════════════════════


class TestTelegramAdapterTypeEnforcement:
    """R64 P1-06: Telegram sink typed adapter 拒绝裸 str。"""

    @pytest.mark.asyncio
    async def test_safe_reply_text_rejects_str(self):
        """safe_reply_text 拒绝裸 str payload(TypeError)。"""
        update = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_reply_text(update, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_send_message_rejects_str(self):
        """safe_send_message 拒绝裸 str payload(TypeError)。"""
        bot = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_send_message(bot, 123, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_rejects_str(self):
        """safe_edit_message_text 拒绝裸 str payload(TypeError)。"""
        query = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_edit_message_text(query, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_reply_text_rejects_int(self):
        """safe_reply_text 拒绝非 str/非 UserMessage/非 ErrorEnvelope 类型。"""
        update = MagicMock()
        with pytest.raises(TypeError):
            await safe_reply_text(update, 12345)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_rejects_dict(self):
        """safe_edit_message_text 拒绝 dict payload。"""
        query = MagicMock()
        with pytest.raises(TypeError):
            await safe_edit_message_text(query, {"msg": "x"})  # type: ignore[arg-type]

    def test_validate_payload_helper_rejects_none(self):
        """_validate_payload 辅助函数拒绝 None。"""
        with pytest.raises(TypeError):
            telegram_adapter._validate_payload(None)  # type: ignore[arg-type]

    def test_type_error_message_mentions_user_message(self):
        """TypeError 信息明确指出整改路径(UserMessage.from_key / ErrorEnvelope)。"""
        with pytest.raises(TypeError) as exc_info:
            telegram_adapter._validate_payload("bare string")
        msg = str(exc_info.value)
        assert "UserMessage" in msg or "ErrorEnvelope" in msg, (
            f"TypeError 信息应提到 UserMessage/ErrorEnvelope: {msg}"
        )


# ════════════════════════════════════════════════════════════════
# 2. Telegram adapter 接受 UserMessage / ErrorEnvelope
# ════════════════════════════════════════════════════════════════


class TestTelegramAdapterAcceptsUserMessage:
    """R64 P1-06: Telegram sink typed adapter 接受 UserMessage。"""

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_with_user_message(self):
        """safe_edit_message_text 接受 UserMessage,渲染后调用 query.edit_message_text。"""
        # 构造 mock query
        query = MagicMock()
        query.edit_message_text = AsyncMock(return_value="edited")

        # 构造 UserMessage(使用已存在的 i18n key)
        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 12345},
        )

        await safe_edit_message_text(query, payload)

        # 验证 edit_message_text 被调用,text 为渲染后的本地化字符串
        query.edit_message_text.assert_awaited_once()
        call_kwargs = query.edit_message_text.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs.args[0]
        assert isinstance(text_arg, str)
        # 应包含 uid=12345(渲染后的本地化字符串)
        assert "12345" in text_arg, (
            f"渲染后的文本应包含 uid=12345: {text_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_with_error_envelope(self):
        """safe_edit_message_text 接受 ErrorEnvelope,渲染后调用 query.edit_message_text。"""
        from services.error_codes import AppError, ErrorCodes

        # 构造 mock query
        query = MagicMock()
        query.edit_message_text = AsyncMock(return_value="edited")

        # 构造 ErrorEnvelope
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FC_TEST"},
        )
        payload = ErrorEnvelope(app_error)

        await safe_edit_message_text(query, payload)

        query.edit_message_text.assert_awaited_once()
        call_kwargs = query.edit_message_text.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs.args[0]
        assert isinstance(text_arg, str)
        assert "FC_TEST" in text_arg, (
            f"渲染后的文本应包含 file_code=FC_TEST: {text_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_safe_send_message_with_user_message(self):
        """safe_send_message 接受 UserMessage,渲染后调用 bot.send_message。"""
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value="sent")

        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 999},
        )

        await safe_send_message(bot, chat_id=123, payload=payload)

        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs.args[0]
        assert isinstance(text_arg, str)
        assert "999" in text_arg

    @pytest.mark.asyncio
    async def test_safe_reply_text_with_user_message(self):
        """safe_reply_text 接受 UserMessage,渲染后调用底层 reply_text。"""
        # update.message.reply_text 走 utils.flood_waiter.safe_reply_text
        # 这里 mock 整个 update.message
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock(return_value="replied")
        update.message.chat_id = 123

        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 42},
        )

        # mock i18n_manager 避免依赖全局单例
        manager = MagicMock()
        manager.format_message = MagicMock(return_value="✅ 已封禁用户 42")

        # mock utils.flood_waiter.safe_reply_text 避免 isinstance(message, Message)
        # 在 conftest 注入的 MagicMock telegram 环境下抛 TypeError
        raw_reply = AsyncMock(return_value="replied")
        with patch("utils.flood_waiter.safe_reply_text", raw_reply):
            await safe_reply_text(update, payload, i18n_manager=manager)

        # 验证 raw_reply 被调用,text 参数包含 uid=42
        raw_reply.assert_awaited_once()
        call_args = raw_reply.call_args
        # raw_reply(message, text) — text 是第二个位置参数
        text_arg = (
            call_args.args[1] if len(call_args.args) >= 2
            else call_args.kwargs.get("text", "")
        )
        assert "42" in text_arg, (
            f"渲染后的文本应包含 uid=42: {text_arg!r}"
        )


# ════════════════════════════════════════════════════════════════
# 3. Telegram adapter 不拼接 query.message.text(语言继承修复)
# ════════════════════════════════════════════════════════════════


class TestTelegramAdapterNoLanguageInheritance:
    """R64 P1-06: safe_edit_message_text 不拼接 query.message.text(避免语言继承)。

    旧模式:``query.edit_message_text(query.message.text + "\\n\\n" + ...)``
    会让新消息继承旧消息的 locale(用户切换语言后再次点击按钮时,
    旧消息仍是切换前的语言)。

    新模式:``safe_edit_message_text(query, UserMessage.from_key(...))``
    直接用新的 i18n key 替换整个消息,不依赖旧消息文本。
    """

    @pytest.mark.asyncio
    async def test_edit_message_text_does_not_read_query_message_text(self):
        """safe_edit_message_text 不读取 query.message.text(不继承旧消息)。"""
        query = MagicMock()
        query.edit_message_text = AsyncMock(return_value="edited")

        # 让 query.message.text 为"旧消息文本(英文)",验证不被继承
        query.message = MagicMock()
        query.message.text = "OLD ENGLISH MESSAGE TEXT"

        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 1},
        )

        await safe_edit_message_text(query, payload)

        # 渲染后的 text 不应包含 "OLD ENGLISH MESSAGE TEXT"
        call_kwargs = query.edit_message_text.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs.args[0]
        assert "OLD ENGLISH MESSAGE TEXT" not in text_arg, (
            f"safe_edit_message_text 不应拼接 query.message.text: {text_arg!r}"
        )

    def test_telegram_adapter_source_has_no_query_message_text_concat(self):
        """telegram_adapter.py 源码不应含 query.message.text 拼接模式。"""
        src = telegram_adapter.__file__
        assert src is not None, "telegram_adapter 模块应有 __file__"
        content = Path(src).read_text(encoding="utf-8")
        # 源码不应有 `query.message.text +` 拼接(旧模式)
        assert "query.message.text +" not in content, (
            "telegram_adapter.py 不应拼接 query.message.text(避免语言继承)"
        )


# ════════════════════════════════════════════════════════════════
# 4. Web adapter 类型强制(拒绝裸 str)
# ════════════════════════════════════════════════════════════════


class TestWebAdapterTypeEnforcement:
    """R64 P1-06: Web sink typed adapter 拒绝裸 str。"""

    def test_json_response_rejects_str(self):
        """json_response 拒绝裸 str payload(TypeError)。"""
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            json_response("hardcoded string")  # type: ignore[arg-type]

    def test_json_response_rejects_int(self):
        """json_response 拒绝 int payload。"""
        with pytest.raises(TypeError):
            json_response(12345)  # type: ignore[arg-type]

    def test_json_response_rejects_dict(self):
        """json_response 拒绝 dict payload。"""
        with pytest.raises(TypeError):
            json_response({"msg": "x"})  # type: ignore[arg-type]

    def test_json_response_rejects_none(self):
        """json_response 拒绝 None payload。"""
        with pytest.raises(TypeError):
            json_response(None)  # type: ignore[arg-type]

    def test_type_error_message_mentions_user_message(self):
        """TypeError 信息明确指出整改路径。"""
        with pytest.raises(TypeError) as exc_info:
            web_adapter._validate_payload("bare string")
        msg = str(exc_info.value)
        assert "UserMessage" in msg or "ErrorEnvelope" in msg


# ════════════════════════════════════════════════════════════════
# 5. Web adapter 构造 JSONResponse
# ════════════════════════════════════════════════════════════════


class TestWebAdapterJsonResponse:
    """R64 P1-06: Web adapter 构造 JSONResponse,响应体只暴露 safe 字段。"""

    def test_json_response_with_user_message(self):
        """json_response 接受 UserMessage,构造 JSONResponse 含 message 字段。"""
        from fastapi.responses import JSONResponse

        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 12345},
        )
        response = json_response(payload, status_code=200)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 200
        # 解析 body
        body = json.loads(response.body.decode("utf-8"))
        assert "message" in body
        assert "12345" in body["message"]

    def test_json_response_with_trace_id(self):
        """json_response 含 trace_id 时,响应体暴露 trace_id(供用户引用)。"""
        payload = UserMessage.from_key(
            "admin.callback.button_security.operation_failed",
            params={"trace_id": "abc123def456"},
            trace_id="abc123def456",
        )
        response = json_response(payload, status_code=500)
        body = json.loads(response.body.decode("utf-8"))
        assert body.get("trace_id") == "abc123def456"

    def test_json_response_with_error_envelope(self):
        """json_response 接受 ErrorEnvelope,响应体含 error_code + trace_id。"""
        from services.error_codes import AppError, ErrorCodes

        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FC_TEST"},
        )
        payload = ErrorEnvelope(app_error)
        response = json_response(payload, status_code=500)
        body = json.loads(response.body.decode("utf-8"))
        assert "message" in body
        assert "error_code" in body
        assert body["error_code"] == app_error.code
        # file_code 透传(safe param)
        assert body.get("file_code") == "FC_TEST"

    def test_json_response_extra_fields_appended(self):
        """json_response 接受 **extra,追加 safe 字段到响应体。"""
        payload = UserMessage.from_key(
            "admin.callback.button_security.ban_success",
            params={"uid": 1},
        )
        response = json_response(payload, status_code=200, status="ok")
        body = json.loads(response.body.decode("utf-8"))
        assert body.get("status") == "ok"

    def test_json_response_does_not_leak_internal_exception(self):
        """json_response 响应体不包含内部 exception str(只暴露 trace_id)。

        R64 P1-06 核心要求:内部 exception 仅进入结构化日志,
        用户面响应体只暴露 trace_id 供引用。
        """
        # 模拟内部异常场景:trace_id 进 params,但 exception 不进
        trace_id = "trace_abc_123"
        payload = UserMessage.from_key(
            "admin.callback.button_security.operation_failed",
            params={"trace_id": trace_id},
            trace_id=trace_id,
        )
        response = json_response(payload, status_code=500)
        body = json.loads(response.body.decode("utf-8"))
        body_str = json.dumps(body)
        # 不应包含典型内部异常关键字
        assert "ValueError" not in body_str
        assert "Traceback" not in body_str
        assert "DatabaseError" not in body_str


# ════════════════════════════════════════════════════════════════
# 6. Sink import-boundary AST 门禁脚本检测违规
# ════════════════════════════════════════════════════════════════


class TestSinkImportBoundaryScanner:
    """R64 P1-06: AST 门禁脚本检测 import / call 违规。"""

    def test_scanner_module_importable(self):
        """check_sink_import_boundary 模块可导入。"""
        assert hasattr(scanner, "collect_findings"), (
            "scanner 应暴露 collect_findings 函数"
        )
        assert hasattr(scanner, "main"), "scanner 应暴露 main 函数"

    def test_scanner_constants_defined(self):
        """scanner 模块常量已定义。"""
        assert scanner.SCAN_DIRS == ["bots", "services"]
        assert "services/sink_adapters/" in scanner.ALLOWED_PREFIXES
        assert "admin/" in scanner.ALLOWED_PREFIXES
        assert "tests/" in scanner.ALLOWED_PREFIXES
        # Rule 1: import 违规配置
        assert "services/sink_adapters/telegram_adapter.py" in (
            scanner.TELEGRAM_IMPORT_ALLOWED_FILES
        )
        # Rule 2: call 违规检测的 sink 方法名
        assert "reply_text" in scanner.DISALLOWED_SINK_METHODS
        assert "send_message" in scanner.DISALLOWED_SINK_METHODS
        assert "edit_message_text" in scanner.DISALLOWED_SINK_METHODS

    def test_find_import_violations_detects_telegram_import(self, tmp_path):
        """Rule 1: 检测 `from telegram import Bot` 违规。"""
        source = "from telegram import Bot\nbot = Bot(token='x')\n"
        tree = __import__("ast").parse(source)
        violations = scanner._find_import_violations(tree, "bots/test_x.py")
        assert len(violations) >= 1, (
            f"应检测到 from telegram import Bot 违规: {violations}"
        )
        assert violations[0][1] == "Rule 1 (import 违规)"

    def test_find_import_violations_allows_in_adapter(self, tmp_path):
        """Rule 1: adapter 文件中 `from telegram import Bot` 不算违规。"""
        source = "from telegram import Bot\nbot = Bot(token='x')\n"
        tree = __import__("ast").parse(source)
        violations = scanner._find_import_violations(
            tree, "services/sink_adapters/telegram_adapter.py",
        )
        assert len(violations) == 0, (
            f"adapter 文件中导入 telegram 不应算违规: {violations}"
        )

    def test_find_import_violations_detects_fastapi_jsonresponse(self, tmp_path):
        """Rule 1: 检测 `from fastapi.responses import JSONResponse` 违规(非 admin/)。"""
        source = (
            "from fastapi.responses import JSONResponse\n"
            "resp = JSONResponse(content={})\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_import_violations(tree, "services/test_x.py")
        assert len(violations) >= 1, (
            f"应检测到 JSONResponse import 违规: {violations}"
        )

    def test_find_import_violations_allows_jsonresponse_in_admin(self, tmp_path):
        """Rule 1: admin/ 目录中 JSONResponse import 不算违规(暂豁免)。"""
        source = (
            "from fastapi.responses import JSONResponse\n"
            "resp = JSONResponse(content={})\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_import_violations(tree, "admin/__init__.py")
        assert len(violations) == 0, (
            f"admin/ 中 JSONResponse import 不应算违规: {violations}"
        )

    def test_find_call_violations_detects_edit_message_text(self, tmp_path):
        """Rule 2: 检测 `query.edit_message_text(...)` 调用违规。"""
        source = (
            "async def f(query):\n"
            "    await query.edit_message_text('hello')\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_call_violations(tree)
        assert len(violations) >= 1, (
            f"应检测到 query.edit_message_text 调用违规: {violations}"
        )
        assert violations[0][1] == "Rule 2 (call 违规)"

    def test_find_call_violations_detects_reply_text(self, tmp_path):
        """Rule 2: 检测 `update.message.reply_text(...)` 调用违规。"""
        source = (
            "async def f(update):\n"
            "    await update.message.reply_text('hello')\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_call_violations(tree)
        assert len(violations) >= 1, (
            f"应检测到 update.message.reply_text 调用违规: {violations}"
        )

    def test_find_call_violations_detects_send_message(self, tmp_path):
        """Rule 2: 检测 `context.bot.send_message(...)` 调用违规。"""
        source = (
            "async def f(context):\n"
            "    await context.bot.send_message(chat_id=1, text='x')\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_call_violations(tree)
        assert len(violations) >= 1, (
            f"应检测到 context.bot.send_message 调用违规: {violations}"
        )

    def test_find_call_violations_no_false_positive_for_safe_adapter(self, tmp_path):
        """Rule 2: 不应误报 safe_edit_message_text(adapter 自身调用)。"""
        # safe_edit_message_text 不在 DISALLOWED_SINK_METHODS 中
        source = (
            "from services.sink_adapters import safe_edit_message_text\n"
            "async def f(query, payload):\n"
            "    await safe_edit_message_text(query, payload)\n"
        )
        tree = __import__("ast").parse(source)
        violations = scanner._find_call_violations(tree)
        # safe_edit_message_text 的 attr="safe_edit_message_text"
        # 不在 DISALLOWED_SINK_METHODS 中,不应被检测
        assert len(violations) == 0, (
            f"safe_edit_message_text 不应被误报: {violations}"
        )

    def test_collect_findings_returns_list(self):
        """collect_findings 返回 list[dict](每项含 file/line/rule/detail)。

        R65 P1-01: 全部 486 项存量违规已迁移到 typed adapter,真实违规数=0。
        此处只验证返回结构,不强制 > 0(strict 模式下 0 违规是目标状态)。
        """
        findings = scanner.collect_findings()
        assert isinstance(findings, list)
        # R65 P1-01: 迁移完成后违规数应为 0(strict 模式门禁)
        assert len(findings) == 0, (
            f"R65 P1-01 整改后应无 sink import-boundary 违规,实际 {len(findings)} 项: "
            f"{[v['file'] for v in findings[:5]]}"
        )
        for v in findings:
            assert "file" in v
            assert "line" in v
            assert "rule" in v
            assert "detail" in v

    def test_scanner_does_not_flag_sink_adapters_package(self):
        """scanner 不应把 sink_adapters 包内的违规计入(sink_adapters 在白名单)。"""
        findings = scanner.collect_findings()
        adapter_violations = [
            v for v in findings
            if v["file"].startswith("services/sink_adapters/")
        ]
        assert adapter_violations == [], (
            f"services/sink_adapters/ 不应被扫描(白名单): "
            f"{[v['file'] for v in adapter_violations]}"
        )

    def test_scanner_does_not_flag_user_message_module(self):
        """scanner 不应把 services/user_message.py 的违规计入(白名单)。"""
        findings = scanner.collect_findings()
        um_violations = [
            v for v in findings
            if v["file"] == "services/user_message.py"
        ]
        assert um_violations == [], (
            f"services/user_message.py 不应被扫描(白名单): {um_violations}"
        )


# ════════════════════════════════════════════════════════════════
# 7. Sink import-boundary baseline ratchet 机制
# ════════════════════════════════════════════════════════════════


class TestSinkImportBoundaryBaseline:
    """R64 P1-06: baseline ratchet 机制(只能减少不能增加存量违规)。"""

    def test_baseline_file_exists(self):
        """baseline 文件已生成并提交。"""
        assert REAL_BASELINE.exists(), (
            f"baseline 文件应存在: {REAL_BASELINE}"
        )

    def test_baseline_json_valid(self):
        """baseline 文件是合法 JSON。

        R65 P1-01: baseline 文件已更新为 violation_count=0(作为历史参考保留,
        CI/Release 切换为 --strict 模式,不再使用 baseline)。
        """
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "violation_count" in data
        assert isinstance(data["violation_count"], int)
        # R65 P1-01: 迁移完成后 baseline violation_count 应为 0
        assert data["violation_count"] == 0, (
            f"R65 P1-01 整改后 baseline violation_count 应为 0(历史参考),"
            f"实际 {data['violation_count']}"
        )

    def test_baseline_ratchet_passes(self):
        """--baseline 模式:当前违规数 <= baseline 时 exit 0。"""
        result = subprocess.run(
            [
                sys.executable,
                "scripts/check_sink_import_boundary.py",
                "--baseline",
                "scripts/sink_import_boundary_baseline.json",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"--baseline 模式应 exit 0(当前 <= baseline)。\n"
            f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
        )
        assert "R64 P1-06 通过" in output, (
            f"输出应包含 'R64 P1-06 通过': {output[:300]}"
        )

    def test_strict_mode_passes_with_zero_violations(self):
        """--strict 模式:0 违规时 exit 0(R65 P1-01 整改目标)。

        R64 P1-06 阶段:strict 模式会因 486 项存量违规而 exit 1(未来目标)。
        R65 P1-01 整改后:全部违规已迁移到 typed adapter,strict 模式 exit 0。
        """
        result = subprocess.run(
            [
                sys.executable,
                "scripts/check_sink_import_boundary.py",
                "--strict",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        # R65 P1-01: 迁移完成后 strict 模式应 exit 0
        assert result.returncode == 0, (
            f"--strict 模式应 exit 0(0 违规)。"
            f"\nstdout: {result.stdout[:500]}"
            f"\nstderr: {result.stderr[:500]}"
        )
        assert "strict 模式通过" in result.stdout, (
            f"strict 模式输出应包含 'strict 模式通过': {result.stdout[:300]}"
        )

    def test_generate_baseline_creates_valid_file(self, tmp_path):
        """--generate-baseline 生成合法 baseline 文件。

        R65 P1-01: 迁移完成后生成的 baseline violation_count=0。
        """
        baseline_path = tmp_path / "test_baseline.json"
        result = subprocess.run(
            [
                sys.executable,
                "scripts/check_sink_import_boundary.py",
                "--generate-baseline",
                str(baseline_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, (
            f"--generate-baseline 应 exit 0。\n"
            f"stdout: {result.stdout[:300]}\nstderr: {result.stderr[:300]}"
        )
        assert baseline_path.exists(), "baseline 文件应被创建"
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert "violation_count" in data
        # R65 P1-01: 迁移完成后 violation_count=0
        assert data["violation_count"] == 0, (
            f"R65 P1-01 整改后生成的 baseline violation_count 应为 0,"
            f"实际 {data['violation_count']}"
        )

    def test_baseline_ratchet_fails_when_violations_increase(self, tmp_path, monkeypatch):
        """baseline ratchet:违规数 > baseline 时 exit 1。

        R65 P1-01: 真实代码库已无违规(current_count=0),无法通过 subprocess
        触发 ratchet fail。改为直接调用 scanner.main() 并 monkeypatch
        collect_findings 返回一个伪造违规,验证 ratchet 逻辑仍正确阻断。
        """
        # 构造一个 baseline.violation_count=0 的小 baseline
        fake_baseline = tmp_path / "fake_baseline.json"
        fake_baseline.write_text(
            json.dumps({
                "description": "fake baseline with 0 violations",
                "violation_count": 0,
            }),
            encoding="utf-8",
        )
        # monkeypatch collect_findings 返回 1 个伪造违规
        # (模拟代码库出现 1 项新增违规的场景)
        fake_findings = [{
            "file": "bots/fake_bot.py",
            "line": 1,
            "rule": "Rule 2 (call 违规)",
            "detail": "update.message.reply_text(...) — 伪造违规用于测试 ratchet",
        }]
        monkeypatch.setattr(scanner, "collect_findings", lambda: fake_findings)
        # 注入 --baseline argv
        monkeypatch.setattr(
            sys, "argv",
            [
                "check_sink_import_boundary.py",
                "--baseline",
                str(fake_baseline),
            ],
        )
        # 直接调用 main(),应返回 1(伪造违规 1 > baseline 0)
        returncode = scanner.main()
        assert returncode == 1, (
            f"ratchet 失败应 exit 1(伪造违规 1 > baseline=0)。returncode={returncode}"
        )


# ════════════════════════════════════════════════════════════════
# 8. callback.py 高风险问题修复回归(P1-06 整改)
# ════════════════════════════════════════════════════════════════


class TestCallbackPyR64P1_06Fixes:
    """R64 P1-06: callback.py 高风险问题修复回归。

    验证:
        1. callback.py 不再含 `params={'error': str(result.error)}` 模式
        2. callback.py 不再含 `params={'error': str(e)}` 模式
        3. callback.py 不再含 `query.message.text + "\n\n" + render_for_send` 模式
        4. callback.py 错误路径使用 trace_id 而非 error detail
    """

    def test_callback_file_exists(self):
        """前置条件:callback.py 文件存在。"""
        assert CALLBACK_PATH.exists(), f"callback.py 不存在: {CALLBACK_PATH}"

    def test_no_str_error_in_params(self):
        """callback.py 不再含 `params={'error': str(result.error)}` 模式。"""
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        assert "params={'error': str(result.error)}" not in content, (
            "callback.py 不应再含 params={'error': str(result.error)} "
            "(内部异常不进用户面 params)"
        )

    def test_no_str_e_in_params(self):
        """callback.py 不再含 `params={'error': str(e)}` 模式。"""
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        assert "params={'error': str(e)}" not in content, (
            "callback.py 不应再含 params={'error': str(e)} "
            "(内部异常不进用户面 params)"
        )

    def test_no_query_message_text_concat_with_render_for_send(self):
        """callback.py 不再含 `query.message.text + ... + render_for_send(...)` 模式。"""
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        # 检测 query.message.text 后接 + ... + render_for_send 的模式
        # (旧模式: query.message.text + "\n\n" + render_for_send(...))
        import re
        # 匹配 query.message.text + <任意> + render_for_send
        pattern = re.compile(
            r'query\.message\.text\s*\+\s*[^,)]+\+\s*render_for_send\s*\('
        )
        matches = pattern.findall(content)
        assert not matches, (
            f"callback.py 不应再含 query.message.text + ... + render_for_send(...) "
            f"模式(语言继承修复): {matches}"
        )

    def test_error_paths_use_trace_id_param(self):
        """callback.py 错误路径使用 trace_id 而非 error detail。"""
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        # 应包含 params={'trace_id': trace_id} 模式(替换原 params={'error': ...})
        assert "params={'trace_id': trace_id}" in content, (
            "callback.py 错误路径应使用 params={'trace_id': trace_id} "
            "(替换原 params={'error': ...})"
        )

    def test_error_paths_log_to_logger_error(self):
        """callback.py 错误路径将内部异常记录到 logger.error(结构化日志)。"""
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        # 应包含 logger.error(_LOG_REPORT_*_FAILED.format(...)) 模式
        assert "_LOG_REPORT_BAN_FAILED" in content, (
            "callback.py 应使用 _LOG_REPORT_BAN_FAILED 日志常量"
        )
        assert "_LOG_REPORT_DETACH_FAILED" in content, (
            "callback.py 应使用 _LOG_REPORT_DETACH_FAILED 日志常量"
        )
        assert "_LOG_REPORT_BLOCK_FAILED" in content, (
            "callback.py 应使用 _LOG_REPORT_BLOCK_FAILED 日志常量"
        )
        assert "_LOG_REPORT_HANDLER_FAILED" in content, (
            "callback.py 应使用 _LOG_REPORT_HANDLER_FAILED 日志常量"
        )

    def test_no_query_message_text_concat_for_ignore(self):
        """callback.py report:ignore 不再拼接 query.message.text。

        注:仅检查实际代码(去除注释行),避免修复说明性注释中提到
        ``query.message.text`` 字样而被误判为违规。
        """
        content = CALLBACK_PATH.read_text(encoding="utf-8")
        # report:ignore 分支不应拼接 query.message.text
        # 查找 report:ignore 分支代码
        import re
        ignore_match = re.search(
            r'if data == "report:ignore":.*?(?=\n    #|\n    if |\n    parts|\Z)',
            content,
            re.DOTALL,
        )
        assert ignore_match is not None, "未找到 report:ignore 分支"
        ignore_body = ignore_match.group(0)
        # 去除注释行(以 # 开头的行)再检查,避免修复说明性注释
        # 中提到 query.message.text 字样而被误判
        code_lines = [
            line for line in ignore_body.splitlines()
            if not line.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        # 检查实际代码不拼接 query.message.text(注意 + 号表示拼接)
        # 实际违规模式:query.message.text + 或 query.message.text)
        assert "query.message.text +" not in code_only, (
            f"report:ignore 分支不应拼接 query.message.text: {code_only[:200]}"
        )
        assert "query.message.text)" not in code_only, (
            f"report:ignore 分支不应直接传 query.message.text: {code_only[:200]}"
        )


# ════════════════════════════════════════════════════════════════
# 9. locale 文件 R64 P1-06 修复回归
# ════════════════════════════════════════════════════════════════


class TestLocaleFilesR64P1_06Fixes:
    """R64 P1-06: locale 文件中 ban_failed/detach_failed/block_failed/operation_failed
    使用 {trace_id} 而非 {error}(内部异常不进用户面消息)。
    """

    LOCALES_DIR = REPO_ROOT / "locales"

    # 修改的 4 个 key(从 {error} 改为 {trace_id})
    FIXED_KEYS = [
        "admin.callback.button_security.ban_failed",
        "admin.callback.button_security.detach_failed",
        "admin.callback.button_security.block_failed",
        "admin.callback.button_security.operation_failed",
    ]

    def _load_flat(self, locale: str) -> dict:
        """加载 locale JSON 并扁平化为点分 key → value dict。"""
        path = self.LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        flat: dict[str, str] = {}

        def _flatten(obj, prefix):
            for k, v in obj.items():
                full = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    _flatten(v, full)
                elif isinstance(v, str):
                    flat[full] = v
                else:
                    flat[full] = str(v)

        _flatten(data, "")
        return flat

    @pytest.mark.parametrize("key", FIXED_KEYS)
    def test_fixed_keys_use_trace_id_not_error(self, key):
        """修复后的 key 使用 {trace_id} 而非 {error}。"""
        zh = self._load_flat("zh-CN")[key]
        en = self._load_flat("en-US")[key]
        assert "{trace_id}" in zh, (
            f"zh-CN key={key} 应包含 {{trace_id}}: {zh!r}"
        )
        assert "{trace_id}" in en, (
            f"en-US key={key} 应包含 {{trace_id}}: {en!r}"
        )
        assert "{error}" not in zh, (
            f"zh-CN key={key} 不应再包含 {{error}}: {zh!r}"
        )
        assert "{error}" not in en, (
            f"en-US key={key} 不应再包含 {{error}}: {en!r}"
        )

    @pytest.mark.parametrize("key", FIXED_KEYS)
    def test_fixed_keys_isomorphic(self, key):
        """修复后的 key 在 zh-CN/en-US 中 ICU 参数同构(都是 {trace_id})。"""
        import re
        zh = self._load_flat("zh-CN")[key]
        en = self._load_flat("en-US")[key]
        zh_params = set(re.findall(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}', zh))
        en_params = set(re.findall(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}', en))
        assert zh_params == en_params, (
            f"key={key} ICU 参数不同构: zh-CN={zh_params}, en-US={en_params}"
        )
        assert zh_params == {"trace_id"}, (
            f"key={key} 参数应仅为 {{trace_id}}: {zh_params}"
        )
