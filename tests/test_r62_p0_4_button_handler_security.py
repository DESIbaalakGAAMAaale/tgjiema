"""R62 P0-04: 按钮式高风险操作迁移到签名 token + CommandBus + MFA/审批 流程。

测试覆盖:
1. _handle_report_action 修复后行为(签名 token + CommandBus,不再直接调破坏性 API)
   - 旧格式 report:ban|uid|reporter|source 被拒绝(无签名)
   - 新格式 report:ban:{handle_id} 通过 verify_button_token_by_handle 验证
   - 无效/过期/重放 handle_id 均被拒绝
   - sub_action 不匹配被拒绝
2. _handle_restore_action 修复后行为
   - 旧格式 restore:confirm|seq|merge|table 被拒绝
   - 新格式 restore:confirm:{handle_id} 通过验证
3. _handle_delete_file_action 修复后行为
   - 旧格式 delfile|{file_code} 被拒绝
   - 新格式 delfile|{handle_id} 通过验证
4. AST 扫描验证
   - _handle_report_action 不再直接调用 update_user_and_invalidate
   - _handle_report_action 调用 verify_button_token_by_handle
5. 按钮渲染点
   - dsp_bot.py / idx_bot.py / admin_bot/handlers.py 调用 sign_button_token_with_handle
6. 按钮 handler 门禁
   - gate scanner 通过(0 violations)
   - inventory 中 _handle_report_action/_handle_restore_action/_handle_delete_file_action
     全部 routes_through_command_bus=True / uses_signed_token_api=True

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
- monkeypatch 替换 database.cache_store.DB_PATH 指向临时路径
- monkeypatch 替换 get_cache_store 返回测试 store
- 固定 ADMIN_BOT_TOKEN 避免 MagicMock 干扰 HMAC
- AST 解析 _handle_report_action 函数体验证不再调用破坏性 API
"""
from __future__ import annotations

import ast
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 MagicMock)──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# Fixture
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略:
    1. 临时目录下的 test_r62_p0_4.db
    2. 直接替换 database.cache_store.DB_PATH
    3. 替换 database.cache_store.get_cache_store 返回测试 store
       (sign_button_token_with_nonce / sign_button_token_with_handle 内部调用)
    4. 同步替换 database.cache_store._store 单例
       (rbac.py / data_lifecycle.py 等模块通过 from database.cache_store import
       get_cache_store 持有原函数引用,只替换 get_cache_store 属性无法影响它们;
       必须同时替换 _store 单例,否则跨测试文件会发生 DB 污染)
    5. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r62_p0_4_test_")
    db_path = Path(tmpdir) / "test_r62_p0_4.db"
    original_path = _cs_module.DB_PATH
    original_get_store = _cs_module.get_cache_store
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module.get_cache_store = lambda: s
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module.get_cache_store = original_get_store
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r62_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r62_test_sender_bot_token")
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    # 提供 ADMIN_TELEGRAM_ID(dsp_bot/idx_bot/admin_bot 按钮渲染用)
    monkeypatch.setattr(config.settings, "ADMIN_TELEGRAM_ID", "999999")


# ════════════════════════════════════════════════════════════════
# 1. sign_button_token_with_handle / verify_button_token_by_handle 基础
# ════════════════════════════════════════════════════════════════


class TestHandleBasedTokenBasics:
    """R62 P0-04: handle 短 ID 模式签名 token 基础行为。"""

    @pytest.mark.asyncio
    async def test_sign_returns_short_handle(self, store):
        """sign_button_token_with_handle 返回短 handle_id(适合作为 callback_data)。"""
        from services.button_security import sign_button_token_with_handle
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        # handle_id 应为 secrets.token_urlsafe(8) 输出(~11 字符,URL-safe)
        assert isinstance(handle_id, str)
        assert len(handle_id) <= 16, (
            f"handle_id 应足够短以适配 Telegram 64 字节 callback_data 限制,"
            f"实际长度: {len(handle_id)}"
        )
        assert len(handle_id) >= 8, (
            f"handle_id 应有足够熵(>=8 字符),实际长度: {len(handle_id)}"
        )

    @pytest.mark.asyncio
    async def test_callback_data_within_telegram_limit(self, store):
        """完整 callback_data(report:ban:{handle_id}) 应 < 64 字节。"""
        from services.button_security import sign_button_token_with_handle
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        callback_data = f"report:ban:{handle_id}"
        assert len(callback_data.encode()) <= 64, (
            f"Telegram callback_data 限制 64 字节,实际长度: "
            f"{len(callback_data.encode())} (callback_data={callback_data})"
        )

    @pytest.mark.asyncio
    async def test_verify_valid_handle(self, store):
        """handle_id 验证通过 → (True, action, payload)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        valid, action, payload = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is True
        assert action == "report"
        assert payload == "ban|12345|67890|dsp"

    @pytest.mark.asyncio
    async def test_verify_replay_rejected(self, store):
        """重放攻击:同一 handle_id 第二次 verify 应失败(nonce 原子消费)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        # 第一次 verify 成功
        valid1, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid1 is True
        # 第二次 verify 应失败(nonce 已被原子消费)
        valid2, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid2 is False, "重放应被拒绝(nonce 原子消费)"

    @pytest.mark.asyncio
    async def test_verify_expired_token_rejected(self, store):
        """过期 token: ttl=-1 → expire_ts 已过期,verify 应失败。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
            ttl=-1,  # 立即过期
        )
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is False, "过期 token 应被拒绝"

    @pytest.mark.asyncio
    async def test_verify_wrong_user_rejected(self, store):
        """跨用户使用 handle_id: principal_id 不匹配,verify 应失败。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=9999, store=store,  # 不同 user
            expected_action="report",
            expected_audience="admin_callback",
        )
        assert valid is False, "跨用户使用 handle_id 应被拒绝"

    @pytest.mark.asyncio
    async def test_verify_unknown_handle_rejected(self, store):
        """未知 handle_id(伪造的)应被拒绝。"""
        from services.button_security import verify_button_token_by_handle
        valid, _, _ = await verify_button_token_by_handle(
            "nonexistent_handle_xyz", current_user_id=1001, store=store,
            expected_action="report",
            expected_audience="admin_callback",
        )
        assert valid is False, "未知 handle_id 应被拒绝(防伪造)"


# ════════════════════════════════════════════════════════════════
# 2. _handle_report_action 旧/新格式行为
# ════════════════════════════════════════════════════════════════


class TestHandleReportActionCallbackFormat:
    """R62 P0-04: _handle_report_action 拒绝旧格式,接受新格式(handle_id)。"""

    def _make_update(self, callback_data: str, user_id: int = 999999):
        """构造 Telegram Update mock(callback_query.data = callback_data)。

        使用 AsyncMock 包装 query.answer / edit_message_text 等 async 方法。
        """
        update = MagicMock()
        query = MagicMock()
        query.data = callback_data
        # query.answer / edit_message_text 是 async 方法,需要用 AsyncMock
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.text = ""
        user = MagicMock()
        user.id = user_id
        user.username = "admin"
        update.callback_query = query
        update.effective_user = user
        return update

    @pytest.mark.asyncio
    async def test_old_format_ban_rejected(self, store, monkeypatch):
        """旧格式 report:ban|uid|reporter|source 无签名,被拒绝。

        data.split(":", 2) → ["report", "ban|12345|67890|dsp"] (仅 2 段)
        _handle_report_action 应走"格式无效(缺少 handle_id)"分支。
        """
        from bots.admin_bot import callback as cb_module

        async def _fake_verify_should_not_be_called(handle_id, current_user_id, store=None, **kwargs):
            raise AssertionError("旧格式 report:ban|... 不应进入 verify")

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify_should_not_be_called,
        )
        # AUTHORIZED_USER_ID 在 menus.py 中是模块级常量,monkeypatch 替换它
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("report:ban|12345|67890|dsp")
        await cb_module._handle_report_action(update, MagicMock(), "report:ban|12345|67890|dsp")
        # 应显示过期/格式无效提示(不再继续走签名验证 / 破坏性 API)
        update.callback_query.edit_message_text.assert_called_once()
        edited_text = (update.callback_query.edit_message_text.call_args.kwargs.get("text") or (update.callback_query.edit_message_text.call_args[0][0] if update.callback_query.edit_message_text.call_args[0] else ""))
        assert "过期" in edited_text or "格式无效" in edited_text or "签名验证失败" in edited_text, (
            f"旧格式应被拒绝,实际提示: {edited_text}"
        )

    @pytest.mark.asyncio
    async def test_new_format_ban_with_invalid_handle_rejected(self, store, monkeypatch):
        """新格式 report:ban:{handle_id} 但 handle 无效 → 拒绝。"""
        from bots.admin_bot import callback as cb_module

        async def _fake_verify(handle_id, current_user_id, store=None, **kwargs):
            return False, "", ""

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("report:ban:invalid_handle_id")
        await cb_module._handle_report_action(update, MagicMock(), "report:ban:invalid_handle_id")
        update.callback_query.edit_message_text.assert_called_once()
        edited_text = (update.callback_query.edit_message_text.call_args.kwargs.get("text") or (update.callback_query.edit_message_text.call_args[0][0] if update.callback_query.edit_message_text.call_args[0] else ""))
        assert "签名验证失败" in edited_text, (
            f"无效 handle 应触发签名验证失败提示,实际: {edited_text}"
        )

    @pytest.mark.asyncio
    async def test_new_format_ban_with_sub_action_mismatch(self, store, monkeypatch):
        """callback sub_action 与 payload 第一段不匹配 → 拒绝。"""
        from bots.admin_bot import callback as cb_module

        async def _fake_verify(handle_id, current_user_id, store=None, **kwargs):
            # 返回的 payload 第一段是 "detach",但 callback 是 "ban"
            return True, "report", "detach|12345|67890|dsp"

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("report:ban:valid_handle")
        await cb_module._handle_report_action(update, MagicMock(), "report:ban:valid_handle")
        update.callback_query.edit_message_text.assert_called_once()
        edited_text = (update.callback_query.edit_message_text.call_args.kwargs.get("text") or (update.callback_query.edit_message_text.call_args[0][0] if update.callback_query.edit_message_text.call_args[0] else ""))
        assert "不匹配" in edited_text, (
            f"sub_action 不匹配应被拒绝,实际: {edited_text}"
        )

    @pytest.mark.asyncio
    async def test_report_ignore_no_signature_required(self, store, monkeypatch):
        """report:ignore 是低风险,不需要签名 token。"""
        from bots.admin_bot import callback as cb_module

        # 让 verify 失败(若误调,会触发测试失败)
        async def _fake_verify_should_not_be_called(handle_id, current_user_id, store=None, **kwargs):
            raise AssertionError("report:ignore 不应调用 verify_button_token_by_handle")

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify_should_not_be_called,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("report:ignore")
        await cb_module._handle_report_action(update, MagicMock(), "report:ignore")
        # report:ignore 走 query.edit_message_text(query.message.text + ..., reply_markup=None)
        update.callback_query.edit_message_text.assert_called_once()


# ════════════════════════════════════════════════════════════════
# 3. _handle_restore_action / _handle_delete_file_action 旧/新格式
# ════════════════════════════════════════════════════════════════


class TestHandleRestoreAndDeleteCallbackFormat:
    """R62 P0-04: _handle_restore_action / _handle_delete_file_action 旧格式被拒绝。"""

    def _make_update(self, callback_data: str, user_id: int = 999999):
        """构造 Telegram Update mock(callback_query.data = callback_data)。

        使用 AsyncMock 包装 query.answer / edit_message_text 等 async 方法。
        """
        update = MagicMock()
        query = MagicMock()
        query.data = callback_data
        # query.answer / edit_message_text 是 async 方法,需要用 AsyncMock
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.text = ""
        user = MagicMock()
        user.id = user_id
        user.username = "admin"
        update.callback_query = query
        update.effective_user = user
        return update

    @pytest.mark.asyncio
    async def test_restore_old_format_rejected(self, store, monkeypatch):
        """旧格式 restore:confirm|seq|merge|table 无签名,被拒绝。"""
        from bots.admin_bot import callback as cb_module

        async def _fake_verify_should_not_be_called(handle_id, current_user_id, store=None, **kwargs):
            raise AssertionError("旧格式 restore 不应进入 verify")

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify_should_not_be_called,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("restore:confirm|1|1|table:users")
        await cb_module._handle_restore_action(update, MagicMock(), "restore:confirm|1|1|table:users")
        # 旧格式不匹配 "restore:confirm:" 前缀(因 | 而非 :),走默认分支
        update.callback_query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_new_format_invalid_handle_rejected(self, store, monkeypatch):
        """新格式 restore:confirm:{handle_id} 但 handle 无效 → 拒绝。"""
        from bots.admin_bot import callback as cb_module

        async def _fake_verify(handle_id, current_user_id, store=None, **kwargs):
            return False, "", ""

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("restore:confirm:invalid_handle")
        await cb_module._handle_restore_action(update, MagicMock(), "restore:confirm:invalid_handle")
        update.callback_query.edit_message_text.assert_called_once()
        edited_text = (update.callback_query.edit_message_text.call_args.kwargs.get("text") or (update.callback_query.edit_message_text.call_args[0][0] if update.callback_query.edit_message_text.call_args[0] else ""))
        assert "签名验证失败" in edited_text, (
            f"无效 handle 应触发签名验证失败提示,实际: {edited_text}"
        )

    @pytest.mark.asyncio
    async def test_delfile_old_format_rejected(self, store, monkeypatch):
        """旧格式 delfile|{file_code} 因 file_code 非合法 handle → verify 失败 → 拒绝。

        注意:delfile|{file_code} 旧格式与 delfile|{handle_id} 新格式语法相同,
        防线在 verify 阶段(file_code 不在 button_tokens 表中 → verify 返回 False)。
        """
        from bots.admin_bot import callback as cb_module

        async def _fake_verify_fail(handle_id, current_user_id, store=None, **kwargs):
            # 旧格式 file_code 不是合法 handle_id,verify 应返回 False
            return False, "", ""

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify_fail,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("delfile|file_abc123")
        await cb_module._handle_delete_file_action(update, MagicMock(), "delfile|file_abc123")
        update.callback_query.edit_message_text.assert_called_once()
        edited_text = (update.callback_query.edit_message_text.call_args.kwargs.get("text") or (update.callback_query.edit_message_text.call_args[0][0] if update.callback_query.edit_message_text.call_args[0] else ""))
        assert "签名验证失败" in edited_text, (
            f"旧格式 file_code 应触发签名验证失败,实际: {edited_text}"
        )

    @pytest.mark.asyncio
    async def test_delfile_cancel_no_signature_required(self, store, monkeypatch):
        """delfile_cancel|{file_code} 是低风险取消,不需要签名 token。"""
        from bots.admin_bot import callback as cb_module

        async def _fake_verify_should_not_be_called(handle_id, current_user_id, store=None, **kwargs):
            raise AssertionError("delfile_cancel 不应调用 verify_button_token_by_handle")

        monkeypatch.setattr(
            "services.button_security.verify_button_token_by_handle",
            _fake_verify_should_not_be_called,
        )
        monkeypatch.setattr(cb_module, "AUTHORIZED_USER_ID", 999999)

        update = self._make_update("delfile_cancel|file_abc123")
        await cb_module._handle_delete_file_action(update, MagicMock(), "delfile_cancel|file_abc123")
        update.callback_query.edit_message_text.assert_called_once()


# ════════════════════════════════════════════════════════════════
# 4. AST 扫描验证:_handle_report_action 不再直接调破坏性 API
# ════════════════════════════════════════════════════════════════


class TestAstScanReportAction:
    """AST 扫描:验证 _handle_report_action 已移除直接破坏性 API 调用。"""

    def _parse_callback_module(self):
        """解析 bots/admin_bot/callback.py 的 AST。"""
        cb_path = REPO_ROOT / "bots" / "admin_bot" / "callback.py"
        source = cb_path.read_text(encoding="utf-8")
        return ast.parse(source)

    def _get_handler_node(self, tree, name: str):
        """从 AST 中提取指定函数节点。"""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
        return None

    def _get_call_names(self, func_node) -> set[str]:
        """提取函数体内所有调用的函数名(直接名 + 属性链名)。"""
        names: set[str] = set()
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                # 收集完整属性链(如 bus.execute)
                parts = []
                cur = func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                names.add(".".join(reversed(parts)))
                # 也收集短名(如 execute)
                names.add(func.attr)
        return names

    def test_report_action_does_not_call_destructive_apis(self):
        """_handle_report_action 不再直接调用 update_user_and_invalidate /
        update_file_record_and_invalidate / delete_user_data 等破坏性 API。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_report_action")
        assert handler is not None, "_handle_report_action 函数应在 bots/admin_bot/callback.py 中"
        names = self._get_call_names(handler)
        forbidden = {
            "update_user_and_invalidate",
            "update_file_record_and_invalidate",
            "delete_user_data",
            "purge_data",
            "purge_channel",
            "factory_reset",
        }
        intersection = forbidden & names
        assert not intersection, (
            f"_handle_report_action 不应直接调用破坏性 API,实际调用了: {intersection}"
        )

    def test_report_action_calls_verify_button_token_by_handle(self):
        """_handle_report_action 应调用 verify_button_token_by_handle(handle 短 ID 模式)。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_report_action")
        assert handler is not None
        names = self._get_call_names(handler)
        assert "verify_button_token_by_handle" in names, (
            "_handle_report_action 应调用 verify_button_token_by_handle,"
            f"实际调用集合: {sorted(names)}"
        )

    def test_report_action_uses_command_bus(self):
        """_handle_report_action 应通过 CommandBus 路由(make_*_command + bus.execute)。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_report_action")
        assert handler is not None
        names = self._get_call_names(handler)
        # 必须包含至少一个 make_*_command 工厂
        expected_factories = {
            "make_ban_user_command",
            "make_detach_file_command",
            "make_block_user_for_file_command",
        }
        assert expected_factories & names, (
            f"_handle_report_action 应调用至少一个 CommandBus 工厂({expected_factories}),"
            f"实际: {sorted(names)}"
        )
        assert "bus.execute" in names or "execute" in names, (
            "_handle_report_action 应调用 bus.execute"
        )

    def test_restore_action_uses_verify_by_handle(self):
        """_handle_restore_action 应调用 verify_button_token_by_handle。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_restore_action")
        assert handler is not None
        names = self._get_call_names(handler)
        assert "verify_button_token_by_handle" in names, (
            "_handle_restore_action 应调用 verify_button_token_by_handle"
        )

    def test_delete_file_action_uses_verify_by_handle(self):
        """_handle_delete_file_action 应调用 verify_button_token_by_handle。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_delete_file_action")
        assert handler is not None
        names = self._get_call_names(handler)
        assert "verify_button_token_by_handle" in names, (
            "_handle_delete_file_action 应调用 verify_button_token_by_handle"
        )


# ════════════════════════════════════════════════════════════════
# 5. 按钮渲染点:调用 sign_button_token_with_handle
# ════════════════════════════════════════════════════════════════


class TestButtonRenderingPoints:
    """R62 P0-04: 按钮渲染点应调用 sign_button_token_with_handle。"""

    def _get_call_names_in_func(self, file_path: str, func_name: str) -> set[str]:
        """解析指定文件中指定函数的所有调用名。"""
        path = REPO_ROOT / file_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    names: set[str] = set()
                    for sub in ast.walk(node):
                        if not isinstance(sub, ast.Call):
                            continue
                        func = sub.func
                        if isinstance(func, ast.Name):
                            names.add(func.id)
                        elif isinstance(func, ast.Attribute):
                            parts = []
                            cur = func
                            while isinstance(cur, ast.Attribute):
                                parts.append(cur.attr)
                                cur = cur.value
                            if isinstance(cur, ast.Name):
                                parts.append(cur.id)
                            names.add(".".join(reversed(parts)))
                            names.add(func.attr)
                    return names
        return set()

    def test_dsp_bot_report_callback_calls_sign_handle(self):
        """bots/dsp_bot.py:report_callback 应调用 sign_button_token_with_handle。"""
        names = self._get_call_names_in_func(
            "bots/dsp_bot.py", "report_callback",
        )
        assert "sign_button_token_with_handle" in names, (
            f"dsp_bot.report_callback 应调用 sign_button_token_with_handle,"
            f"实际: {sorted(names)}"
        )

    def test_idx_bot_report_callback_calls_sign_handle(self):
        """bots/idx_bot.py:report_callback 应调用 sign_button_token_with_handle。"""
        names = self._get_call_names_in_func(
            "bots/idx_bot.py", "report_callback",
        )
        assert "sign_button_token_with_handle" in names, (
            f"idx_bot.report_callback 应调用 sign_button_token_with_handle,"
            f"实际: {sorted(names)}"
        )

    def test_admin_bot_delete_file_calls_sign_handle(self):
        """bots/admin_bot/handlers.py:delete_file 应调用 sign_button_token_with_handle。"""
        names = self._get_call_names_in_func(
            "bots/admin_bot/handlers.py", "delete_file",
        )
        assert "sign_button_token_with_handle" in names, (
            f"admin_bot.handlers.delete_file 应调用 sign_button_token_with_handle,"
            f"实际: {sorted(names)}"
        )

    def test_admin_bot_restore_calls_sign_handle(self):
        """bots/admin_bot/handlers.py:restore 应调用 sign_button_token_with_handle。"""
        names = self._get_call_names_in_func(
            "bots/admin_bot/handlers.py", "restore",
        )
        assert "sign_button_token_with_handle" in names, (
            f"admin_bot.handlers.restore 应调用 sign_button_token_with_handle,"
            f"实际: {sorted(names)}"
        )


# ════════════════════════════════════════════════════════════════
# 6. 按钮 handler 门禁(inventory + gate scanner)
# ════════════════════════════════════════════════════════════════


class TestButtonHandlerGateIntegration:
    """R62 P0-04: 门禁扫描器整体通过(0 violations)。"""

    def test_inventory_has_three_handlers_marked_secure(self):
        """inventory 中 _handle_report_action / _handle_restore_action /
        _handle_delete_file_action 全部标记为安全(CommandBus + 签名 token)。"""
        inventory_path = REPO_ROOT / "scripts" / "button_handler_inventory.json"
        import json
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        targets = {
            "_handle_report_action",
            "_handle_restore_action",
            "_handle_delete_file_action",
        }
        for h in inventory["handlers"]:
            if h["handler"] in targets:
                assert h["is_high_risk"], f"{h['handler']} 应为高风险"
                assert h["routes_through_command_bus"], (
                    f"{h['handler']} 应 routes_through_command_bus=True"
                )
                assert not h["calls_destructive_api"], (
                    f"{h['handler']} 不应 calls_destructive_api=True"
                )
                assert h["uses_signed_token_api"], (
                    f"{h['handler']} 应 uses_signed_token_api=True"
                )
                assert h["bypass_reason"] is None, (
                    f"{h['handler']} bypass_reason 应为 None"
                )

    def test_gate_scanner_passes_with_zero_violations(self):
        """check_button_handler_gate.py 应 exit 0(0 violations)。"""
        import importlib.util
        gate_path = REPO_ROOT / "scripts" / "check_button_handler_gate.py"
        # 加载并执行 gate 模块
        spec = importlib.util.spec_from_file_location(
            "r62_p0_4_gate", gate_path,
        )
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        inventory_path = REPO_ROOT / "scripts" / "button_handler_inventory.json"
        metadata_path = REPO_ROOT / "scripts" / "button_handler_metadata.json"
        import json
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        exit_code, new_violations, all_violations = gate.check(
            inventory=inventory,
            metadata=metadata,
            strict=True,
        )
        assert exit_code == 0, (
            f"R62 P0-04 修复后 strict 模式应 exit 0,"
            f"实际 exit_code={exit_code}, violations={all_violations}"
        )
        assert len(new_violations) == 0, (
            f"R62 P0-04 修复后不应有新增违规,实际: {new_violations}"
        )

    def test_baseline_violations_cleared(self):
        """metadata.json: baseline.violation_count == 0 且 violations 为空。"""
        metadata_path = REPO_ROOT / "scripts" / "button_handler_metadata.json"
        import json
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        baseline = metadata.get("baseline", {})
        assert baseline.get("violation_count") == 0, (
            f"baseline.violation_count 应为 0,实际: {baseline.get('violation_count')}"
        )
        assert baseline.get("violations") == [], (
            f"baseline.violations 应为空,实际: {baseline.get('violations')}"
        )

    def test_report_action_sidecar_metadata_no_todo(self):
        """sidecar 中 _handle_report_action 的所有字段不应包含 TODO。"""
        metadata_path = REPO_ROOT / "scripts" / "button_handler_metadata.json"
        import json
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        report_meta = metadata["handlers"].get("_handle_report_action")
        assert report_meta is not None, "_handle_report_action sidecar 条目应存在"
        for field, value in report_meta.items():
            if isinstance(value, str):
                assert "TODO" not in value, (
                    f"sidecar 字段 '{field}' 不应包含 TODO(已修复): {value}"
                )
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        assert "TODO" not in item, (
                            f"sidecar 字段 '{field}' 列表项不应包含 TODO: {item}"
                        )


# ════════════════════════════════════════════════════════════════
# 7. handle 短 ID 长度安全检查
# ════════════════════════════════════════════════════════════════


class TestHandleIdLengthSafety:
    """handle 短 ID 长度安全检查(熵 + Telegram 64 字节限制)。"""

    @pytest.mark.asyncio
    async def test_handle_id_meets_entropy_floor(self, store):
        """handle_id 应有足够熵(>= 8 字符,128 bit)。"""
        from services.button_security import sign_button_token_with_handle, HANDLE_ID_BYTES
        # HANDLE_ID_BYTES = 8 → secrets.token_urlsafe(8) ≈ 11 字符
        assert HANDLE_ID_BYTES >= 8, (
            f"HANDLE_ID_BYTES 应 >= 8(128 bit 熵),实际: {HANDLE_ID_BYTES}"
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report", payload="ban|12345|67890|dsp",
        )
        # token_urlsafe(8) 输出 ~11 字符
        assert len(handle_id) >= 10, (
            f"handle_id 长度应 >= 10 字符(token_urlsafe(8) 输出),实际: {len(handle_id)}"
        )

    @pytest.mark.asyncio
    async def test_all_callback_data_patterns_within_64_bytes(self, store):
        """所有按钮 callback_data 模式应 < 64 字节(Telegram 限制)。

        模式:
          - report:ban:{handle_id}         ≈ 9 + 11 = 20
          - report:detach:{handle_id}      ≈ 12 + 11 = 23
          - report:block:{handle_id}       ≈ 12 + 11 = 23
          - restore:confirm:{handle_id}    ≈ 16 + 11 = 27
          - delfile|{handle_id}            ≈ 8 + 11 = 19
        """
        from services.button_security import sign_button_token_with_handle
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report", payload="test",
        )
        patterns = [
            f"report:ban:{handle_id}",
            f"report:detach:{handle_id}",
            f"report:block:{handle_id}",
            f"restore:confirm:{handle_id}",
            f"delfile|{handle_id}",
        ]
        for p in patterns:
            assert len(p.encode()) <= 64, (
                f"callback_data '{p}' 超过 Telegram 64 字节限制 "
                f"(实际 {len(p.encode())} 字节)"
            )
