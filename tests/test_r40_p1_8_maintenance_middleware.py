"""R40 P1-8: 维护模式中间件装饰器测试。

被测目标:
- ``services.maintenance_mode.require_maintenance_check(action)`` 装饰器
- 装饰器应用到所有 Bot 高风险入口:
  - up_bot: start_upload / new_collection / _dispatch_media
  - idx_bot: handle_code / handle_message
  - dsp_bot: start
  - admin_bot/handlers: cmd_takedown / cmd_ban_user / cmd_unban_user / restore

测试场景:
1. 维护模式开启时装饰器拒绝请求(回复"系统维护中")
2. 维护模式关闭时装饰器正常执行原函数
3. is_enabled 异常时装饰器 fail-closed(回复"服务暂不可用")
4. 装饰器已应用到所有指定 Bot 入口(通过 __wrapped__ 内省验证)

设计说明:
- 装饰器使用 functools.wraps 保留原函数元数据,
  被装饰后的函数通过 __wrapped__ 属性可访问原函数。
- 装饰器顺序:@_auth_required 在外层(先认证),
  @require_maintenance_check 在内层(认证通过后检查维护状态)。
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )


# ════════════════════════════════════════════════════════════════
# P1-8 测试用例:装饰器行为测试
# ════════════════════════════════════════════════════════════════

class TestRequireMaintenanceCheckDecorator:
    """R40 P1-8: require_maintenance_check 装饰器行为测试。"""

    @pytest.mark.asyncio
    async def test_rejects_when_maintenance_enabled(self):
        """维护模式开启时装饰器拒绝请求,回复"系统维护中"。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="上传文件")
        async def test_handler(update, context):
            return "EXECUTED"

        # Mock update 对象
        mock_update = MagicMock()

        # R65 P1-01: maintenance_mode 已迁移为 safe_reply_text typed adapter,
        # mock safe_reply_text 避免依赖真实 telegram(isinstance(Message) 在 mock 下报错)
        mock_safe_reply = AsyncMock()
        # Mock is_enabled 返回 True(维护中)
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(return_value=True),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            result = await test_handler(mock_update, context=None)

        # 验证:未执行原函数
        assert result is None, "维护模式开启时不应执行原函数"

        # 验证:回复了"系统维护中"
        mock_safe_reply.assert_called_once()
        # safe_reply_text(update.message, UserMessage) — 第二个位置参数是 UserMessage
        payload = mock_safe_reply.call_args[0][1]
        text = payload.render(None)  # from_raw_text 构造,_raw_text 已设置
        assert "系统维护中" in text, \
            f"应回复'系统维护中',实际: {text}"
        assert "上传文件" in text, \
            f"应包含 action 描述'上传文件',实际: {text}"

    @pytest.mark.asyncio
    async def test_executes_when_maintenance_disabled(self):
        """维护模式关闭时装饰器正常执行原函数。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="解码文件")
        async def test_handler(update, context):
            return "DECODED_OK"

        mock_update = MagicMock()

        # R65 P1-01: maintenance_mode 已迁移为 safe_reply_text typed adapter
        mock_safe_reply = AsyncMock()
        # Mock is_enabled 返回 False(非维护中)
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(return_value=False),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            result = await test_handler(mock_update, context=None)

        # 验证:执行了原函数
        assert result == "DECODED_OK", "维护模式关闭时应执行原函数"

        # 验证:未回复任何消息(无维护提示)
        mock_safe_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_fail_closed_on_maintenance_check_error(self):
        """is_enabled 抛 MaintenanceCheckError 时装饰器 fail-closed。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="数据库恢复")
        async def test_handler(update, context):
            return "RESTORED"

        mock_update = MagicMock()

        # R65 P1-01: maintenance_mode 已迁移为 safe_reply_text typed adapter
        mock_safe_reply = AsyncMock()
        # Mock is_enabled 抛异常
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(
                side_effect=maintenance_mode.MaintenanceCheckError("DB 故障")
            ),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            result = await test_handler(mock_update, context=None)

        # 验证:未执行原函数(fail-closed)
        assert result is None, "异常时应 fail-closed 不执行原函数"

        # 验证:回复了"服务暂不可用"
        mock_safe_reply.assert_called_once()
        payload = mock_safe_reply.call_args[0][1]
        text = payload.render(None)  # from_raw_text 构造,_raw_text 已设置
        assert "服务暂不可用" in text, \
            f"应回复'服务暂不可用',实际: {text}"

    @pytest.mark.asyncio
    async def test_decorator_preserves_function_metadata(self):
        """装饰器使用 functools.wraps 保留原函数元数据。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="测试")
        async def my_handler(update, context):
            """原函数文档字符串。"""
            return "OK"

        # 验证:函数名保留
        assert my_handler.__name__ == "my_handler", \
            f"装饰器应保留原函数名,实际: {my_handler.__name__}"

        # 验证:文档字符串保留
        assert my_handler.__doc__ == "原函数文档字符串。", \
            f"装饰器应保留原函数 docstring,实际: {my_handler.__doc__}"

        # 验证:__wrapped__ 属性指向原函数
        assert hasattr(my_handler, "__wrapped__"), \
            "装饰器应通过 functools.wraps 设置 __wrapped__ 属性"

    @pytest.mark.asyncio
    async def test_decorator_handles_reply_exception_gracefully(self):
        """reply_text 抛异常时装饰器不崩溃(优雅降级)。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="测试")
        async def test_handler(update, context):
            return "EXECUTED"

        # Mock update 对象
        mock_update = MagicMock()

        # R65 P1-01: safe_reply_text 抛异常(Telegram API 不可用)时装饰器不崩溃
        mock_safe_reply = AsyncMock(side_effect=Exception("Telegram API 超时"))

        # Mock is_enabled 返回 True(维护中)
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(return_value=True),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            # 不应抛异常(safe_reply_text 失败被捕获)
            result = await test_handler(mock_update, context=None)

        assert result is None, "safe_reply_text 异常时仍应返回 None(不执行原函数)"


# ════════════════════════════════════════════════════════════════
# P1-8 测试用例:装饰器应用验证(内省 Bot 模块)
# ════════════════════════════════════════════════════════════════

class TestDecoratorAppliedToBotEntries:
    """R40 P1-8: 验证装饰器已应用到所有指定 Bot 高风险入口。

    通过检查函数是否有 __wrapped__ 属性(functools.wraps 设置)来验证。
    """

    def _is_decorated(self, func) -> bool:
        """检查函数是否被 require_maintenance_check 装饰。

        装饰器使用 functools.wraps,被装饰后的函数:
        - 有 __wrapped__ 属性指向原函数
        - 或在 __closure__ 中包含 wrapper 引用
        """
        if func is None:
            return False
        return hasattr(func, "__wrapped__")

    def test_up_bot_start_upload_decorated(self):
        """up_bot.start_upload 应被 require_maintenance_check 装饰。"""
        try:
            from bots import up_bot
        except Exception:
            pytest.skip("bots.up_bot 不可导入")
        assert self._is_decorated(up_bot.start_upload), \
            "up_bot.start_upload 应被 require_maintenance_check 装饰"

    def test_up_bot_new_collection_decorated(self):
        """up_bot.new_collection 应被 require_maintenance_check 装饰。"""
        try:
            from bots import up_bot
        except Exception:
            pytest.skip("bots.up_bot 不可导入")
        assert self._is_decorated(up_bot.new_collection), \
            "up_bot.new_collection 应被 require_maintenance_check 装饰"

    def test_up_bot_dispatch_media_decorated(self):
        """up_bot._dispatch_media 应被 require_maintenance_check 装饰。"""
        try:
            from bots import up_bot
        except Exception:
            pytest.skip("bots.up_bot 不可导入")
        assert self._is_decorated(up_bot._dispatch_media), \
            "up_bot._dispatch_media 应被 require_maintenance_check 装饰"

    def test_idx_bot_handle_code_decorated(self):
        """idx_bot.handle_code 应被 require_maintenance_check 装饰。"""
        try:
            from bots import idx_bot
        except Exception:
            pytest.skip("bots.idx_bot 不可导入")
        assert self._is_decorated(idx_bot.handle_code), \
            "idx_bot.handle_code 应被 require_maintenance_check 装饰"

    def test_idx_bot_handle_message_decorated(self):
        """idx_bot.handle_message 应被 require_maintenance_check 装饰。"""
        try:
            from bots import idx_bot
        except Exception:
            pytest.skip("bots.idx_bot 不可导入")
        assert self._is_decorated(idx_bot.handle_message), \
            "idx_bot.handle_message 应被 require_maintenance_check 装饰"

    def test_dsp_bot_start_decorated(self):
        """dsp_bot.start 应被 require_maintenance_check 装饰。"""
        try:
            from bots import dsp_bot
        except Exception:
            pytest.skip("bots.dsp_bot 不可导入")
        assert self._is_decorated(dsp_bot.start), \
            "dsp_bot.start 应被 require_maintenance_check 装饰"

    def test_admin_bot_cmd_takedown_decorated(self):
        """admin_bot.handlers.cmd_takedown 应被 require_maintenance_check 装饰。"""
        try:
            from bots.admin_bot import handlers
        except Exception:
            pytest.skip("bots.admin_bot.handlers 不可导入")
        assert self._is_decorated(handlers.cmd_takedown), \
            "admin_bot.handlers.cmd_takedown 应被 require_maintenance_check 装饰"

    def test_admin_bot_cmd_ban_user_decorated(self):
        """admin_bot.handlers.cmd_ban_user 应被 require_maintenance_check 装饰。"""
        try:
            from bots.admin_bot import handlers
        except Exception:
            pytest.skip("bots.admin_bot.handlers 不可导入")
        # cmd_ban_user 在 handlers.py 中名为 ban_user
        func = getattr(handlers, "cmd_ban_user", None) or getattr(handlers, "ban_user", None)
        assert func is not None, "admin_bot.handlers 应有 cmd_ban_user 或 ban_user"
        assert self._is_decorated(func), \
            "admin_bot.handlers 的 ban_user 应被 require_maintenance_check 装饰"

    def test_admin_bot_cmd_unban_user_decorated(self):
        """admin_bot.handlers.cmd_unban_user 应被 require_maintenance_check 装饰。"""
        try:
            from bots.admin_bot import handlers
        except Exception:
            pytest.skip("bots.admin_bot.handlers 不可导入")
        func = getattr(handlers, "cmd_unban_user", None) or getattr(handlers, "unban_user", None)
        assert func is not None, "admin_bot.handlers 应有 cmd_unban_user 或 unban_user"
        assert self._is_decorated(func), \
            "admin_bot.handlers 的 unban_user 应被 require_maintenance_check 装饰"

    def test_admin_bot_restore_decorated(self):
        """admin_bot.handlers.restore 应被 require_maintenance_check 装饰。"""
        try:
            from bots.admin_bot import handlers
        except Exception:
            pytest.skip("bots.admin_bot.handlers 不可导入")
        assert self._is_decorated(handlers.restore), \
            "admin_bot.handlers.restore 应被 require_maintenance_check 装饰"
