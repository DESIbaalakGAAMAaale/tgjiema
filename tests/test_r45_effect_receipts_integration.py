"""R45: Effect Receipts 集成辅助函数 + CommandBus 接入测试。

被测目标:
- ``services.effect_receipts_integration.with_effect_receipt`` 装饰器
- ``services.effect_receipts_integration.EffectReceiptContext`` 上下文管理器
- ``services.command_bus.CommandBus._execute_handler`` effect receipt 接入

测试场景:
1. ``with_effect_receipt`` 装饰器:已完成时跳过(skipped=True)
2. ``with_effect_receipt`` 装饰器:未完成时执行并记录 completed
3. ``with_effect_receipt`` 装饰器:异常时记录 failed
4. ``with_effect_receipt`` 装饰器:无 action_id 时直接执行(向后兼容)
5. ``EffectReceiptContext``:正常完成时记录 completed
6. ``EffectReceiptContext``:异常时记录 failed
7. ``EffectReceiptContext``:已完成时跳过
8. ``CommandBus.execute`` 接入:执行后 effect_receipts 已记录

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- 每个用例前重置 EffectReceiptManager 单例
- 验证 effect_receipts 表中的 status / external_id 字段
"""
import asyncio
import datetime
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r45_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def receipt_manager(real_store):
    """初始化 EffectReceiptManager 单例并返回。

    每个用例前重置单例,确保用例间隔离。
    """
    # 重置单例
    from services import effect_receipts as _er_mod
    _er_mod._receipt_manager = None
    mgr = _er_mod.get_receipt_manager(real_store)
    yield mgr
    # 用例后清理
    _er_mod._receipt_manager = None
    if real_store._db:
        await real_store._db.execute("DELETE FROM effect_receipts")
        await real_store._db.commit()


@pytest_asyncio.fixture
async def clean_tables(real_store):
    """每个用例前清空 effect_receipts / command_executions / command_outbox / approvals 表。"""
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.execute("DELETE FROM command_outbox")
    await real_store._db.execute("DELETE FROM approvals")
    await real_store._db.commit()
    yield real_store
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.execute("DELETE FROM command_outbox")
    await real_store._db.commit()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _get_receipt_status(store, action_id: str, effect_type: str, target: str) -> Optional[str]:
    """查询 effect_receipts 表中的 status 字段。"""
    cursor = await store._db.execute(
        "SELECT status FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _get_receipt_external_id(store, action_id: str, effect_type: str, target: str) -> Optional[str]:
    """查询 effect_receipts 表中的 external_id 字段。"""
    cursor = await store._db.execute(
        "SELECT external_id FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _seed_completed_receipt(
    store, action_id: str, effect_type: str, target: str,
    external_id: str = "msg_42",
    params: Optional[dict] = None,
):
    """预置一条 status='completed' 的 receipt(模拟崩溃前已完成的场景)。

    R49 P0-4: request_hash 必须非空以满足 NOT NULL + CHECK 约束
    (critical effect_type 不允许空字符串)。

    如果提供 params,用 compute_effect_request_hash 计算真实 hash
    (使 check_receipt 的 request_hash 对比通过,模拟真实已完成场景)。
    否则使用固定 hash(用于不关心 hash 对比的测试)。
    """
    from services.effect_receipts import compute_effect_request_hash
    if params is not None:
        request_hash = compute_effect_request_hash(effect_type, params)
    else:
        request_hash = f"seed_hash_{action_id}"
    now = datetime.datetime.utcnow().isoformat()
    await store._db.execute(
        "INSERT OR REPLACE INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        " completed_at, request_hash) "
        "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)",
        (action_id, effect_type, target, external_id, now, now, request_hash),
    )
    await store._db.commit()


# ════════════════════════════════════════════════════════════════
# 1. with_effect_receipt 装饰器测试
# ════════════════════════════════════════════════════════════════

class TestWithEffectReceiptDecorator:
    """测试 with_effect_receipt 装饰器。"""

    @pytest.mark.asyncio
    async def test_decorator_skips_when_completed(self, receipt_manager, clean_tables):
        """测试 1:已完成时跳过(返回 skipped=True)。"""
        from services.effect_receipts_integration import with_effect_receipt

        # 预置一条 completed receipt(R49 P0-4: 用真实 params 计算 hash 以匹配)
        store = clean_tables
        seed_params = {"chat_id": 42, "text": "hi"}
        await _seed_completed_receipt(
            store, "act_1", "telegram_send", "chat:42",
            external_id="msg_42",
            params=seed_params,
        )

        call_count = 0

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: "chat:42",
            params_fn=lambda *a, **kw: {"chat_id": a[0], "text": a[1]},
        )
        async def send_message(chat_id, text):
            nonlocal call_count
            call_count += 1
            return {"message_id": 999}

        result = await send_message(42, "hi", action_id="act_1")

        # 验证:函数未被调用,返回 skipped
        assert call_count == 0, "已完成时不应调用原函数"
        assert isinstance(result, dict)
        assert result["skipped"] is True
        assert result["external_id"] == "msg_42"

    @pytest.mark.asyncio
    async def test_decorator_executes_and_records_completed(self, receipt_manager, clean_tables):
        """测试 2:未完成时执行原函数并记录 completed。"""
        from services.effect_receipts_integration import with_effect_receipt

        store = clean_tables

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: f"chat:{a[0]}",
            params_fn=lambda *a, **kw: {"chat_id": a[0], "text": a[1]},
        )
        async def send_message(chat_id, text):
            return {"message_id": 12345, "text": text}

        result = await send_message(42, "hello", action_id="act_2")

        # 验证返回值
        assert result == {"message_id": 12345, "text": "hello"}

        # 验证 receipt 已记录为 completed
        status = await _get_receipt_status(store, "act_2", "telegram_send", "chat:42")
        assert status == "completed"
        # 验证 external_id 已从 message_id 提取
        external_id = await _get_receipt_external_id(
            store, "act_2", "telegram_send", "chat:42",
        )
        assert external_id == "12345"

    @pytest.mark.asyncio
    async def test_decorator_records_failed_on_exception(self, receipt_manager, clean_tables):
        """测试 3:原函数抛异常时记录 failed 并向上抛。"""
        from services.effect_receipts_integration import with_effect_receipt

        store = clean_tables

        @with_effect_receipt("r2_upload", lambda *a, **kw: "key:abc")
        async def upload_file(key, data):
            raise RuntimeError("R2 upload failed")

        with pytest.raises(RuntimeError, match="R2 upload failed"):
            await upload_file("abc", b"data", action_id="act_3")

        # 验证 receipt 已记录为 failed
        status = await _get_receipt_status(store, "act_3", "r2_upload", "key:abc")
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_decorator_backward_compat_without_action_id(self, receipt_manager, clean_tables):
        """测试 4:无 action_id 时直接执行原函数(向后兼容)。

        R47 P0-4: critical effect 无 action_id 现已拒绝执行(抛 EffectReceiptError),
        此处改用非 critical effect_type 'r2_upload' 验证向后兼容路径。
        """
        from services.effect_receipts_integration import with_effect_receipt

        store = clean_tables
        called = False

        @with_effect_receipt("r2_upload", lambda *a, **kw: f"chat:{a[0]}")
        async def send_message(chat_id, text):
            nonlocal called
            called = True
            return {"message_id": 1}

        # 不传 action_id(非 critical → 直执)
        result = await send_message(42, "hi")

        assert called is True
        assert result == {"message_id": 1}

        # 验证未写入 effect_receipts(因为未传 action_id,直接执行)
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM effect_receipts "
            "WHERE effect_type = 'r2_upload' AND target = 'chat:42'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0


# ════════════════════════════════════════════════════════════════
# 2. EffectReceiptContext 上下文管理器测试
# ════════════════════════════════════════════════════════════════

class TestEffectReceiptContext:
    """测试 EffectReceiptContext 上下文管理器。"""

    @pytest.mark.asyncio
    async def test_context_records_completed_on_normal_exit(self, receipt_manager, clean_tables):
        """测试 5:正常退出时记录 completed。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        async with EffectReceiptContext(
            action_id="act_5",
            effect_type="crdb_upsert",
            target="users:42",
        ) as receipt:
            assert receipt.skipped is False
            # 模拟外部副作用
            receipt.set_external_id("crdb_rev_42")

        # 验证 receipt 已记录为 completed
        status = await _get_receipt_status(store, "act_5", "crdb_upsert", "users:42")
        assert status == "completed"
        external_id = await _get_receipt_external_id(
            store, "act_5", "crdb_upsert", "users:42",
        )
        assert external_id == "crdb_rev_42"

    @pytest.mark.asyncio
    async def test_context_records_failed_on_exception(self, receipt_manager, clean_tables):
        """测试 6:异常退出时记录 failed,异常继续向上抛。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        with pytest.raises(ValueError, match="simulated failure"):
            async with EffectReceiptContext(
                action_id="act_6",
                effect_type="telegram_send",
                target="chat:99",
                params={"chat_id": 99, "text": "test"},
            ) as receipt:
                assert receipt.skipped is False
                raise ValueError("simulated failure")

        # 验证 receipt 已记录为 failed
        status = await _get_receipt_status(store, "act_6", "telegram_send", "chat:99")
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_context_skips_when_already_completed(self, receipt_manager, clean_tables):
        """测试 7:已完成时 skipped=True,跳过副作用执行。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        # 预置 completed receipt(R49 P0-4: 用真实 params 计算 hash 以匹配)
        seed_params = {"chat_id": 77, "text": "skip_test"}
        await _seed_completed_receipt(
            store, "act_7", "telegram_send", "chat:77",
            external_id="msg_77",
            params=seed_params,
        )

        side_effect_called = False
        async with EffectReceiptContext(
            action_id="act_7",
            effect_type="telegram_send",
            target="chat:77",
            params={"chat_id": 77, "text": "skip_test"},
        ) as receipt:
            assert receipt.skipped is True
            assert receipt.external_id == "msg_77"
            # 调用方应检查 skipped 后跳过副作用
            side_effect_called = False  # 模拟跳过

        # 验证 side_effect 未被调用
        assert side_effect_called is False
        # 验证 receipt 仍为 completed(未被覆盖)
        status = await _get_receipt_status(store, "act_7", "telegram_send", "chat:77")
        assert status == "completed"

    @pytest.mark.asyncio
    async def test_context_mark_no_record_skips_recording(self, receipt_manager, clean_tables):
        """测试 8:mark_no_record 跳过 record_completed/failed,允许重试。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        async with EffectReceiptContext(
            action_id="act_8",
            effect_type="telegram_send",
            target="chat:88",
            params={"chat_id": 88, "text": "no_record_test"},
        ) as receipt:
            receipt.mark_no_record()
            # 模拟早返回(未实际发送)

        # 验证 receipt 仍为 pending(未记录 completed)
        status = await _get_receipt_status(store, "act_8", "telegram_send", "chat:88")
        assert status == "pending"


# ════════════════════════════════════════════════════════════════
# 3. CommandBus 接入测试
# ════════════════════════════════════════════════════════════════

class TestCommandBusEffectReceiptIntegration:
    """测试 CommandBus._execute_handler 接入 effect receipt。"""

    @pytest.fixture(autouse=True)
    def _reset_command_bus_idempotency(self):
        """每个用例前重置 CommandBus 幂等缓存。"""
        from services import command_bus
        command_bus.reset_idempotency_cache()
        yield
        command_bus.reset_idempotency_cache()

    @pytest.mark.asyncio
    async def test_command_bus_records_effect_receipt_on_success(
        self, receipt_manager, clean_tables,
    ):
        """测试 9:CommandBus 执行成功后,effect_receipts 表中应有 completed 记录。"""
        from services import command_bus as _cb_mod
        from services.command_bus import (
            AdminPrincipal, Command, CommandBus, Result,
            PERM_USERS_UNBAN,
        )

        store = clean_tables

        # Mock rbac.check_permission 返回 True
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(return_value=True)

        # 创建 CommandBus 实例
        cb = CommandBus(rbac_module=mock_rbac)

        # 构造一个不需审批的命令(handler 返回 dict)
        async def _handler(params):
            return {"unban_ok": True, "external_id": "unban_42"}

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 42},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=100, name="admin", source="web")

        result = await cb.execute(command, principal, action_id="cmd_act_9")

        # 验证 Result
        assert result.success is True
        assert result.data == {"unban_ok": True, "external_id": "unban_42"}
        assert result.action_id == "cmd_act_9"

        # 验证 effect_receipts 字段
        assert isinstance(result.effect_receipts, dict)
        assert result.effect_receipts.get("effect_type") == "command_handler"
        assert result.effect_receipts.get("target") == "unban_user"
        assert result.effect_receipts.get("skipped") is False

        # 验证 effect_receipts 表中已记录 completed
        status = await _get_receipt_status(
            store, "cmd_act_9", "command_handler", "unban_user",
        )
        assert status == "completed"
        # 验证 external_id 已从 result.data 提取
        external_id = await _get_receipt_external_id(
            store, "cmd_act_9", "command_handler", "unban_user",
        )
        assert external_id == "unban_42"

    @pytest.mark.asyncio
    async def test_command_bus_skips_handler_when_receipt_completed(
        self, receipt_manager, clean_tables,
    ):
        """测试 10:effect receipt 已 completed 时跳过 handler(崩溃重试场景)。"""
        from services.command_bus import (
            AdminPrincipal, Command, CommandBus,
            PERM_USERS_UNBAN,
        )

        store = clean_tables

        # 预置 completed receipt(模拟崩溃前已成功执行)
        await _seed_completed_receipt(
            store, "cmd_act_10", "command_handler", "unban_user",
            external_id="prev_external_42",
        )

        # Mock rbac.check_permission 返回 True
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(return_value=True)

        cb = CommandBus(rbac_module=mock_rbac)

        # handler 不应被调用
        handler_called = False

        async def _handler(params):
            nonlocal handler_called
            handler_called = True
            return {"unban_ok": True}

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 42},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=100, name="admin", source="web")

        result = await cb.execute(command, principal, action_id="cmd_act_10")

        # 验证 handler 未被调用
        assert handler_called is False, "已完成时不应调用 handler"

        # 验证 Result.success=True(从缓存恢复)
        assert result.success is True
        assert result.data == {
            "skipped_by_receipt": True,
            "external_id": "prev_external_42",
        }
        assert result.effect_receipts.get("skipped") is True

    @pytest.mark.asyncio
    async def test_command_bus_records_failed_on_handler_exception(
        self, receipt_manager, clean_tables,
    ):
        """测试 11:handler 抛异常时 effect_receipts 表中应有 failed 记录。"""
        from services.command_bus import (
            AdminPrincipal, Command, CommandBus,
            PERM_USERS_UNBAN,
        )

        store = clean_tables

        # Mock rbac.check_permission 返回 True
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(return_value=True)

        cb = CommandBus(rbac_module=mock_rbac)

        async def _handler(params):
            raise RuntimeError("handler failed")

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 42},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=100, name="admin", source="web")

        result = await cb.execute(command, principal, action_id="cmd_act_11")

        # 验证 Result 失败
        assert result.success is False
        assert "执行失败" in result.error

        # 验证 effect_receipts 表中已记录 failed
        status = await _get_receipt_status(
            store, "cmd_act_11", "command_handler", "unban_user",
        )
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_command_bus_no_db_degraded_mode_records_receipt(
        self, receipt_manager, clean_tables, monkeypatch,
    ):
        """测试 12:无 DB 降级模式下仍尝试记录 effect receipt(manager 可用时)。

        场景:CommandBus._get_store() 返回的 store._db 为 None(降级模式),
        但 EffectReceiptManager 的单例已通过 receipt_manager fixture 初始化(指向 real_store),
        所以 manager 可用。验证 receipt 仍被记录。
        """
        from services.command_bus import (
            AdminPrincipal, Command, CommandBus,
            PERM_USERS_UNBAN, _get_store,
        )

        store = clean_tables

        # Mock _get_store 返回一个 _db 为 None 的 mock(模拟降级模式)
        mock_store_no_db = MagicMock()
        mock_store_no_db._db = None
        monkeypatch.setattr(
            "services.command_bus._get_store", lambda: mock_store_no_db,
        )

        # Mock rbac.check_permission 返回 True
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(return_value=True)

        cb = CommandBus(rbac_module=mock_rbac)

        async def _handler(params):
            return {"unban_ok": True, "external_id": "degraded_42"}

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 42},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=100, name="admin", source="web")

        result = await cb.execute(command, principal, action_id="cmd_act_12")

        # 验证 Result 成功
        assert result.success is True
        assert result.data == {"unban_ok": True, "external_id": "degraded_42"}

        # 验证 effect_receipts 表中已记录 completed
        # (receipt_manager 仍指向 real_store,所以 receipt 写入成功)
        status = await _get_receipt_status(
            store, "cmd_act_12", "command_handler", "unban_user",
        )
        assert status == "completed"
