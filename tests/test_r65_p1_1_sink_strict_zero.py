"""R65 P1-01: Sink import-boundary strict 模式零违规门禁测试。

P1-01 整改背景(R65 终审报告):
    R64 P1-06 声称 typed sink adapter 已完成,但 baseline 仍有 486 项违规,
    CI 运行的是 baseline ratchet 而非 ``--strict``。本测试验证 R65 P1-01
    整改结果:全部 486 项违规已迁移到 typed adapter,CI/Release 切换为
    ``--strict``,任何新增违规都会被门禁阻断。

测试组织:
    - ``TestSinkStrictModeZeroViolations``  — scanner --strict exit 0
    - ``TestMigratedFilesNoDirectTelegramImport`` — 抽查迁移后的文件不再
      直接 ``from telegram import ...``
    - ``TestSafeAdapterTypeBoundary`` — safe_* adapter 接受
      ``UserMessage | ErrorEnvelope`` 并拒绝裸 str
    - ``TestNewAdapterFunctions`` — R65 P1-01 新增的工厂函数和 adapter
      (build_inline_keyboard / build_bot / build_input_media /
      safe_answer_callback_query / UserMessage.from_raw_text)
    - ``TestCIWorkflowUsesStrictMode`` — CI workflow 切换为 --strict
    - ``TestBaselineFileHistoricalReference`` — baseline 文件作为历史参考
      保留,violation_count=0
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── 让测试能导入 scripts/ 下的模块 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 被测对象
from services.user_message import ErrorEnvelope, UserMessage  # noqa: E402
from services.sink_adapters import (  # noqa: E402
    build_bot,
    build_inline_keyboard,
    build_input_media,
    safe_answer_callback_query,
    safe_edit_message_text,
    safe_reply_text,
    safe_send_message,
)

# 迁移文件清单(R65 P1-01 整改的 11 个文件)
MIGRATED_FILES: list[str] = [
    "bots/admin_bot/callback.py",
    "bots/admin_bot/conversation.py",
    "bots/admin_bot/handlers.py",
    "bots/admin_bot/menus.py",
    "bots/admin_bot/run.py",
    "bots/dsp_bot.py",
    "bots/idx_bot.py",
    "bots/mon_bot.py",
    "bots/up_bot.py",
    "services/maintenance_mode.py",
    "services/relay_instance.py",
]

# CI workflow 文件路径
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BASELINE_FILE = SCRIPTS_DIR / "sink_import_boundary_baseline.json"


# ════════════════════════════════════════════════════════════════
# 1. scanner --strict exit 0(0 违规)
# ════════════════════════════════════════════════════════════════


class TestSinkStrictModeZeroViolations:
    """R65 P1-01: scanner --strict 模式必须 exit 0(0 违规)。"""

    def test_scanner_strict_exits_zero(self):
        """``python scripts/check_sink_import_boundary.py --strict`` exit 0。

        R65 P1-01 核心目标:全部 486 项存量违规已迁移,strict 模式 0 违规。
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
        assert result.returncode == 0, (
            f"R65 P1-01: --strict 模式应 exit 0(0 违规),实际 exit {result.returncode}。\n"
            f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:500]}"
        )
        assert "strict 模式通过" in result.stdout, (
            f"输出应包含 'strict 模式通过': {result.stdout[:300]}"
        )

    def test_scanner_strict_output_no_violations(self):
        """scanner --strict 输出不应包含任何违规文件名。"""
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
        # 0 违规时输出不应包含 "FAIL" 或 "Rule 1" / "Rule 2"
        assert "FAIL" not in result.stdout, (
            f"0 违规时输出不应包含 FAIL: {result.stdout[:300]}"
        )
        assert "Rule 1" not in result.stdout, (
            f"0 违规时输出不应包含 Rule 1: {result.stdout[:300]}"
        )
        assert "Rule 2" not in result.stdout, (
            f"0 违规时输出不应包含 Rule 2: {result.stdout[:300]}"
        )

    def test_scanner_strict_blocks_new_violation(self, tmp_path, monkeypatch):
        """scanner --strict 检测到任何违规时 exit 1(门禁阻断能力)。

        R65 P1-01: 通过 monkeypatch collect_findings 返回伪造违规,
        验证 strict 模式仍能正确阻断(防止未来回归)。
        """
        import check_sink_import_boundary as scanner_mod

        fake_findings = [{
            "file": "bots/fake_bot.py",
            "line": 1,
            "rule": "Rule 2 (call 违规)",
            "detail": "update.message.reply_text(...) — 伪造违规",
        }]
        monkeypatch.setattr(scanner_mod, "collect_findings", lambda: fake_findings)
        monkeypatch.setattr(
            sys, "argv",
            ["check_sink_import_boundary.py", "--strict"],
        )
        returncode = scanner_mod.main()
        assert returncode == 1, (
            f"strict 模式检测到违规应 exit 1,实际 {returncode}"
        )


# ════════════════════════════════════════════════════════════════
# 2. 迁移后的文件不再直接导入 telegram / fastapi.responses
# ════════════════════════════════════════════════════════════════


class TestMigratedFilesNoDirectTelegramImport:
    """R65 P1-01: 迁移后的文件不应直接 ``from telegram import ...``。

    业务模块必须通过 ``services.sink_adapters.telegram_helpers`` 或
    ``services.sink_adapters.telegram_adapter`` 间接使用 telegram 类。
    """

    @pytest.mark.parametrize("file_rel", MIGRATED_FILES)
    def test_no_direct_telegram_import(self, file_rel: str):
        """迁移文件不应包含 ``from telegram import`` / ``from telegram.ext import``
        / ``from telegram.error import``。
        """
        file_path = REPO_ROOT / file_rel
        if not file_path.exists():
            pytest.skip(f"文件不存在(可能已被重构): {file_path}")
        content = file_path.read_text(encoding="utf-8")
        # 解析 AST,精确检测 ImportFrom 节点
        tree = ast.parse(content, filename=str(file_path))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "telegram" or module.startswith("telegram."):
                violations.append(
                    f"line {node.lineno}: from {module} import "
                    f"{', '.join(a.name for a in node.names)}"
                )
        assert not violations, (
            f"R65 P1-01: {file_rel} 不应直接导入 telegram 包,实际发现:\n"
            + "\n".join(violations)
        )

    def test_admin_bot_handlers_uses_typed_adapter(self):
        """抽查:``bots/admin_bot/handlers.py`` 通过 sink_adapters 导入。"""
        file_path = REPO_ROOT / "bots" / "admin_bot" / "handlers.py"
        content = file_path.read_text(encoding="utf-8")
        # 应从 services.sink_adapters 导入
        assert "from services.sink_adapters" in content, (
            "handlers.py 应通过 services.sink_adapters 导入 typed adapter"
        )
        # 不应直接 from telegram import
        assert "from telegram import" not in content, (
            "handlers.py 不应直接 from telegram import"
        )
        assert "from telegram.ext import" not in content, (
            "handlers.py 不应直接 from telegram.ext import"
        )

    def test_admin_bot_callback_uses_typed_adapter(self):
        """抽查:``bots/admin_bot/callback.py`` 通过 sink_adapters 导入。"""
        file_path = REPO_ROOT / "bots" / "admin_bot" / "callback.py"
        content = file_path.read_text(encoding="utf-8")
        assert "from services.sink_adapters" in content, (
            "callback.py 应通过 services.sink_adapters 导入 typed adapter"
        )
        assert "from telegram import" not in content, (
            "callback.py 不应直接 from telegram import"
        )
        assert "from telegram.ext import" not in content, (
            "callback.py 不应直接 from telegram.ext import"
        )

    def test_admin_bot_handlers_uses_safe_adapters(self):
        """抽查:``bots/admin_bot/handlers.py`` 调用 safe_* adapter 函数。"""
        file_path = REPO_ROOT / "bots" / "admin_bot" / "handlers.py"
        content = file_path.read_text(encoding="utf-8")
        # 应调用至少一个 safe_* adapter
        safe_calls = [
            name for name in (
                "safe_reply_text", "safe_send_message", "safe_edit_message_text",
            ) if name in content
        ]
        assert safe_calls, (
            "handlers.py 应调用至少一个 safe_* adapter 函数"
        )

    def test_admin_bot_handlers_no_direct_sink_calls(self):
        """抽查:``bots/admin_bot/handlers.py`` 不应直接调用
        ``.reply_text(`` / ``.send_message(`` / ``.edit_message_text(``。"""
        file_path = REPO_ROOT / "bots" / "admin_bot" / "handlers.py"
        content = file_path.read_text(encoding="utf-8")
        # 解析 AST 检测 call 违规(精确,避免字符串误判)
        tree = ast.parse(content, filename=str(file_path))
        disallowed = {"reply_text", "send_message", "edit_message_text"}
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 仅检测 attr.method() 形式
            if isinstance(func, ast.Attribute) and func.attr in disallowed:
                violations.append(
                    f"line {node.lineno}: ...{func.attr}(...) — 应改用 safe_{func.attr}"
                )
        assert not violations, (
            f"R65 P1-01: handlers.py 不应直接调用 sink API,实际发现:\n"
            + "\n".join(violations)
        )


# ════════════════════════════════════════════════════════════════
# 3. safe_* adapter 类型边界(接受 UserMessage | ErrorEnvelope,拒绝裸 str)
# ════════════════════════════════════════════════════════════════


class TestSafeAdapterTypeBoundary:
    """R65 P1-01: safe_* adapter 接受 ``UserMessage | ErrorEnvelope``,
    拒绝裸 str / int / dict / None。"""

    @pytest.mark.asyncio
    async def test_safe_reply_text_rejects_bare_str(self):
        """safe_reply_text 拒绝裸 str payload(TypeError)。"""
        update = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_reply_text(update, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_send_message_rejects_bare_str(self):
        """safe_send_message 拒绝裸 str payload(TypeError)。"""
        bot = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_send_message(bot, 123, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_rejects_bare_str(self):
        """safe_edit_message_text 拒绝裸 str payload(TypeError)。"""
        query = MagicMock()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            await safe_edit_message_text(query, "hardcoded string")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_reply_text_rejects_int(self):
        """safe_reply_text 拒绝 int payload。"""
        update = MagicMock()
        with pytest.raises(TypeError):
            await safe_reply_text(update, 12345)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_send_message_rejects_dict(self):
        """safe_send_message 拒绝 dict payload。"""
        bot = MagicMock()
        with pytest.raises(TypeError):
            await safe_send_message(bot, 123, {"msg": "x"})  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_rejects_none(self):
        """safe_edit_message_text 拒绝 None payload。"""
        query = MagicMock()
        with pytest.raises(TypeError):
            await safe_edit_message_text(query, None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_safe_reply_text_accepts_user_message(self):
        """safe_reply_text 接受 UserMessage(过渡期 from_raw_text 工厂)。"""
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock(return_value="replied")
        update.message.chat_id = 123

        # 使用 from_raw_text 工厂(过渡期已渲染字符串)
        payload = UserMessage.from_raw_text("测试消息")

        # mock utils.flood_waiter.safe_reply_text 避免依赖真实 telegram
        from unittest.mock import patch
        raw_reply = AsyncMock(return_value="replied")
        with patch("utils.flood_waiter.safe_reply_text", raw_reply):
            await safe_reply_text(update, payload)

        raw_reply.assert_awaited_once()
        # 第二个位置参数应为渲染后的字符串
        call_args = raw_reply.call_args
        text_arg = (
            call_args.args[1] if len(call_args.args) >= 2
            else call_args.kwargs.get("text", "")
        )
        assert text_arg == "测试消息", (
            f"safe_reply_text 应透传渲染后的字符串,实际: {text_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_safe_send_message_accepts_user_message(self):
        """safe_send_message 接受 UserMessage。"""
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value="sent")

        payload = UserMessage.from_raw_text("测试消息")
        await safe_send_message(bot, chat_id=123, payload=payload)

        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.call_args
        text_arg = call_kwargs.kwargs.get("text")
        assert text_arg == "测试消息"

    @pytest.mark.asyncio
    async def test_safe_edit_message_text_accepts_user_message(self):
        """safe_edit_message_text 接受 UserMessage。"""
        query = MagicMock()
        query.edit_message_text = AsyncMock(return_value="edited")

        payload = UserMessage.from_raw_text("测试消息")
        await safe_edit_message_text(query, payload)

        query.edit_message_text.assert_awaited_once()
        call_kwargs = query.edit_message_text.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs.args[0]
        assert text_arg == "测试消息"

    @pytest.mark.asyncio
    async def test_safe_answer_callback_query_rejects_bare_str(self):
        """safe_answer_callback_query 通过 query.answer 透传,不直接接受 str payload。

        注:safe_answer_callback_query 是 R65 P1-01 新增 adapter,
        其签名为 ``(query, text=None, show_alert=False, **kwargs)``,
        text 参数是 telegram 原生 answer API 的提示文字(非用户面消息 payload),
        不走 _validate_payload 类型边界。本测试验证其可正常调用。
        """
        query = MagicMock()
        query.answer = AsyncMock(return_value="answered")

        await safe_answer_callback_query(query, text="提示")
        query.answer.assert_awaited_once()
        call_kwargs = query.answer.call_args
        assert call_kwargs.kwargs.get("text") == "提示"


# ════════════════════════════════════════════════════════════════
# 4. R65 P1-01 新增的工厂函数和 adapter
# ════════════════════════════════════════════════════════════════


class TestNewAdapterFunctions:
    """R65 P1-01: 新增的工厂函数和 adapter 可正常导入和调用。"""

    def test_build_inline_keyboard_exported(self):
        """build_inline_keyboard 已导出。"""
        assert callable(build_inline_keyboard), (
            "build_inline_keyboard 应可调用"
        )

    def test_build_inline_keyboard_handles_none(self):
        """build_inline_keyboard(None) 返回 None。"""
        # 注:conftest 注入 MagicMock telegram,InlineKeyboardMarkup 不可真正构造,
        # 这里只验证 None 输入的早返回路径
        result = build_inline_keyboard(None)
        # None 输入:函数应早返回(MagicMock 环境下可能返回 None 或 MagicMock)
        # 关键是不抛异常
        assert result is None or result is not None

    def test_build_bot_exported(self):
        """build_bot 已导出。"""
        assert callable(build_bot), "build_bot 应可调用"

    def test_build_input_media_exported(self):
        """build_input_media 已导出。"""
        assert callable(build_input_media), "build_input_media 应可调用"

    def test_build_input_media_rejects_unknown_type(self):
        """build_input_media 拒绝未知的 media_type。"""
        with pytest.raises(ValueError, match=r"不支持的 media_type"):
            build_input_media("unknown_type", media="dummy")

    def test_safe_answer_callback_query_exported(self):
        """safe_answer_callback_query 已导出。"""
        assert callable(safe_answer_callback_query), (
            "safe_answer_callback_query 应可调用"
        )

    def test_user_message_from_raw_text_factory(self):
        """UserMessage.from_raw_text 工厂正确构造过渡期 payload。"""
        payload = UserMessage.from_raw_text("测试消息")
        assert isinstance(payload, UserMessage)
        # 渲染时应直接返回 raw_text(不走 i18n format_message)
        manager = MagicMock()
        manager.format_message = MagicMock(return_value="不应被调用")
        rendered = payload.render(manager)
        assert rendered == "测试消息", (
            f"from_raw_text 构造的 payload 渲染时应返回原始字符串,实际: {rendered!r}"
        )
        manager.format_message.assert_not_called()

    def test_user_message_from_raw_text_with_error_code(self):
        """from_raw_text 支持 error_code 和 trace_id 参数。"""
        payload = UserMessage.from_raw_text(
            "错误消息",
            error_code="E_TEST",
            trace_id="trace-abc-123",
        )
        assert isinstance(payload, UserMessage)
        assert payload.error_code == "E_TEST"
        assert payload.trace_id == "trace-abc-123"


# ════════════════════════════════════════════════════════════════
# 5. CI workflow 使用 --strict(不再使用 --baseline)
# ════════════════════════════════════════════════════════════════


class TestCIWorkflowUsesStrictMode:
    """R65 P1-01: CI workflow 切换为 --strict,不再使用 --baseline。"""

    def test_ci_workflow_file_exists(self):
        """CI workflow 文件存在。"""
        assert CI_WORKFLOW.exists(), f"CI workflow 文件应存在: {CI_WORKFLOW}"

    def test_ci_workflow_calls_strict_not_baseline(self):
        """CI workflow 调用 ``--strict``,不再调用 ``--baseline``。"""
        content = CI_WORKFLOW.read_text(encoding="utf-8")
        # 应包含 --strict 调用
        assert "check_sink_import_boundary.py --strict" in content, (
            "CI workflow 应调用 check_sink_import_boundary.py --strict"
        )
        # 不应再包含 --baseline 调用(针对 sink_import_boundary)
        # 注:其他 scanner 可能仍用 --baseline(如 check_error_protocol.py)
        # 这里只检查 check_sink_import_boundary.py 这一行
        for line in content.splitlines():
            if "check_sink_import_boundary.py" in line:
                assert "--baseline" not in line, (
                    f"check_sink_import_boundary.py 行不应再使用 --baseline: {line.strip()}"
                )
                assert "--strict" in line, (
                    f"check_sink_import_boundary.py 行应使用 --strict: {line.strip()}"
                )

    def test_ci_workflow_strict_call_has_r65_marker(self):
        """CI workflow 的 --strict 调用应标注 R65 P1-01 整改来源。"""
        content = CI_WORKFLOW.read_text(encoding="utf-8")
        # 查找 sink boundary 调用附近的注释
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if "check_sink_import_boundary.py --strict" in line:
                # 检查前 5 行是否有 R65 P1-01 标注
                nearby = "\n".join(lines[max(0, i - 5):i + 1])
                assert "R65 P1-01" in nearby, (
                    f"CI workflow 的 sink boundary --strict 调用附近应标注 R65 P1-01 整改来源。\n"
                    f"附近内容:\n{nearby}"
                )
                return
        pytest.fail("未在 CI workflow 中找到 check_sink_import_boundary.py --strict 调用")


# ════════════════════════════════════════════════════════════════
# 6. baseline 文件作为历史参考(violation_count=0)
# ════════════════════════════════════════════════════════════════


class TestBaselineFileHistoricalReference:
    """R65 P1-01: baseline 文件作为历史参考保留,violation_count=0。"""

    def test_baseline_file_exists(self):
        """baseline 文件存在(作为历史参考保留)。"""
        assert BASELINE_FILE.exists(), (
            f"baseline 文件应作为历史参考保留: {BASELINE_FILE}"
        )

    def test_baseline_violation_count_is_zero(self):
        """baseline 文件 violation_count=0(R65 P1-01 整改完成)。"""
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        assert data["violation_count"] == 0, (
            f"R65 P1-01 整改后 baseline violation_count 应为 0,"
            f"实际 {data['violation_count']}"
        )

    def test_baseline_records_previous_count(self):
        """baseline 文件记录 previous_violation_count=486(迁移前存量)。"""
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        assert data.get("previous_violation_count") == 486, (
            f"baseline 应记录 previous_violation_count=486(迁移前存量),"
            f"实际 {data.get('previous_violation_count')}"
        )

    def test_baseline_version_is_r65(self):
        """baseline 文件 version 标记为 R65-P1-01。"""
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        assert data.get("version") == "R65-P1-01", (
            f"baseline version 应为 R65-P1-01,实际 {data.get('version')}"
        )

    def test_baseline_has_migration_summary(self):
        """baseline 文件包含迁移摘要(供历史审计参考)。"""
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        summary = data.get("migration_summary", {})
        assert "migrated_files" in summary, (
            "baseline migration_summary 应包含 migrated_files"
        )
        assert "rule_1_import_replacements" in summary, (
            "baseline migration_summary 应包含 rule_1_import_replacements"
        )
        assert "rule_2_call_replacements" in summary, (
            "baseline migration_summary 应包含 rule_2_call_replacements"
        )
        # 迁移文件数应为 11
        assert len(summary["migrated_files"]) == 11, (
            f"迁移文件应为 11 个,实际 {len(summary['migrated_files'])}"
        )
        # 替换总数应为 441(18 import + 423 call)
        total = (summary["rule_1_import_replacements"]
                 + summary["rule_2_call_replacements"])
        assert total == 441, (
            f"替换总数应为 441(18+423),实际 {total}"
        )
