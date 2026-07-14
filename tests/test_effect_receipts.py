"""R44 G0-2: EffectReceiptManager 测试 — 外部副作用 receipt 持久化 + effectively-once。

测试覆盖:
- check_receipt: 不存在 / pending / completed / failed / DB 异常
- record_pending: 幂等(重复调用不报错)
- record_completed: 写入 external_id
- record_failed: 标记失败
- 端到端: pending → completed 流程

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- 手动创建 effect_receipts 表(R44 任务5 之前 cache_store.init 不创建该表)
- 通过 monkeypatch 注入测试 store 到 EffectReceiptManager 单例
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库 + effect_receipts 表
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时:
    - 设置 ``_cs_module._store`` 为测试实例,使 ``get_cache_store()`` 返回正确的测试 store
    - 手动创建 ``effect_receipts`` 表(R44 任务5 未集成到 cache_store.init 之前)
    """
    tmpdir = tempfile.mkdtemp(prefix="r44_effect_receipts_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        # 手动创建 effect_receipts 表(若已存在则无操作)
        if s._db:
            await s._db.execute(
                """CREATE TABLE IF NOT EXISTS effect_receipts (
                    action_id     TEXT NOT NULL,
                    effect_type   TEXT NOT NULL,
                    target        TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    external_id   TEXT,
                    created_at    TEXT NOT NULL,
                    completed_at  TEXT,
                    PRIMARY KEY (action_id, effect_type, target)
                )"""
            )
            await s._db.commit()
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """每个用例前重置 EffectReceiptManager 单例,避免跨用例污染。"""
    from services import effect_receipts as er_mod
    er_mod._receipt_manager = None
    yield
    er_mod._receipt_manager = None


# ════════════════════════════════════════════════════════════════
# 1. check_receipt 测试
# ════════════════════════════════════════════════════════════════

class TestCheckReceipt:
    """check_receipt 查询行为测试。"""

    @pytest.mark.asyncio
    async def test_check_receipt_returns_none_when_not_exists(self, real_store):
        """receipt 不存在时 check_receipt 应返回 None。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        result = await mgr.check_receipt(
            "nonexistent_action", "telegram_send", "chat_123",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_record_pending_then_check_returns_none(self, real_store):
        """pending 状态的 receipt 不算完成,check_receipt 应返回 None。

        场景: handler 正在执行中(崩溃前未完成),重试时不应跳过 handler。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        await mgr.record_pending("act_pending", "r2_upload", "backups/test.enc")
        result = await mgr.check_receipt("act_pending", "r2_upload", "backups/test.enc")
        assert result is None, "pending 状态的 receipt 不应被 check_receipt 视为完成"

    @pytest.mark.asyncio
    async def test_record_completed_then_check_returns_receipt(self, real_store):
        """completed 状态的 receipt 应被 check_receipt 返回。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_done_001"
        effect_type = "telegram_send"
        target = "chat_456"
        external_id = "msg_789"
        # R48 P0-4: critical effect 必须传非空 request_hash
        request_hash = "hash_done_001"

        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        await mgr.record_completed(action_id, effect_type, target, external_id=external_id)

        result = await mgr.check_receipt(action_id, effect_type, target)
        assert result is not None
        assert result["status"] == "completed"
        assert result["external_id"] == external_id
        assert result["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_record_failed_then_check_returns_none(self, real_store):
        """failed 状态的 receipt 不算完成,check_receipt 应返回 None。

        场景: handler 失败后崩溃重试,应允许重新执行(不跳过)。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_failed_001"
        effect_type = "crdb_upsert"
        target = "users:1"

        await mgr.record_pending(action_id, effect_type, target)
        await mgr.record_failed(action_id, effect_type, target)

        result = await mgr.check_receipt(action_id, effect_type, target)
        assert result is None, "failed 状态的 receipt 不应被 check_receipt 视为完成"

    @pytest.mark.asyncio
    async def test_check_receipt_handles_db_error(self, real_store):
        """数据库异常时 check_receipt 应返回 None(降级执行,不阻塞 handler)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        # 关闭数据库连接,模拟异常
        if real_store._db:
            await real_store._db.close()
            real_store._db = None

        result = await mgr.check_receipt("any", "any", "any")
        assert result is None


# ════════════════════════════════════════════════════════════════
# 2. record_pending 测试
# ════════════════════════════════════════════════════════════════

class TestRecordPending:
    """record_pending 行为测试。"""

    @pytest.mark.asyncio
    async def test_idempotent_record_pending(self, real_store):
        """重复调用 record_pending 不应报错(INSERT OR IGNORE 保证幂等)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_idem_001"
        effect_type = "sqlite_write"
        target = "audit_log:1"

        # 第一次调用应成功
        await mgr.record_pending(action_id, effect_type, target)
        # 第二次调用应不报错(INSERT OR IGNORE)
        await mgr.record_pending(action_id, effect_type, target)
        # 第三次调用应不报错
        await mgr.record_pending(action_id, effect_type, target)

        # 验证 receipt 存在且状态仍为 pending
        cursor = await real_store._db.execute(
            "SELECT status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending"


# ════════════════════════════════════════════════════════════════
# 3. record_completed 测试
# ════════════════════════════════════════════════════════════════

class TestRecordCompleted:
    """record_completed 行为测试。"""

    @pytest.mark.asyncio
    async def test_record_completed_with_external_id(self, real_store):
        """record_completed 应写入 external_id 与 completed_at。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_ext_001"
        effect_type = "r2_upload"
        target = "backups/backup_20260713.enc"
        external_id = "v1.0_abcdef"

        await mgr.record_pending(action_id, effect_type, target)
        await mgr.record_completed(action_id, effect_type, target, external_id=external_id)

        # 校验数据库行
        cursor = await real_store._db.execute(
            "SELECT status, external_id, completed_at FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "completed"
        assert row[1] == external_id
        assert row[2] is not None and row[2] != ""

    @pytest.mark.asyncio
    async def test_record_completed_without_external_id(self, real_store):
        """record_completed 不传 external_id 时应留空。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_no_ext_001"
        effect_type = "crdb_delete"
        target = "file_records:abc123"
        # R48 P0-4: critical effect 必须传非空 request_hash
        request_hash = "hash_no_ext_001"

        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        await mgr.record_completed(action_id, effect_type, target)

        cursor = await real_store._db.execute(
            "SELECT status, external_id FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "completed"
        # external_id 默认值为 ""
        assert row[1] == "" or row[1] is None


# ════════════════════════════════════════════════════════════════
# 4. 端到端流程测试
# ════════════════════════════════════════════════════════════════

class TestEndToEnd:
    """端到端: pending → completed / failed 流程。"""

    @pytest.mark.asyncio
    async def test_pending_to_completed_flow(self, real_store):
        """端到端: pending → completed 完整流程,check_receipt 应返回完成态。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_e2e_ok"
        effect_type = "handler_execute"
        target = "restore_backup"

        # 1. 初始状态: 不存在,check 返回 None
        assert await mgr.check_receipt(action_id, effect_type, target) is None

        # 2. 记录 pending
        await mgr.record_pending(action_id, effect_type, target)
        # pending 状态不算完成
        assert await mgr.check_receipt(action_id, effect_type, target) is None

        # 3. 记录 completed
        await mgr.record_completed(
            action_id, effect_type, target, external_id="restore_result_001",
        )

        # 4. check 应返回完成态
        result = await mgr.check_receipt(action_id, effect_type, target)
        assert result is not None
        assert result["status"] == "completed"
        assert result["external_id"] == "restore_result_001"

    @pytest.mark.asyncio
    async def test_pending_to_failed_flow(self, real_store):
        """端到端: pending → failed 流程,check_receipt 应返回 None(允许重试)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        action_id = "act_e2e_fail"
        effect_type = "telegram_send"
        target = "chat_999"
        # R48 P0-4: critical effect 必须传非空 request_hash
        request_hash = "hash_e2e_fail"

        # 1. 记录 pending
        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )

        # 2. 记录 failed
        await mgr.record_failed(action_id, effect_type, target)

        # 3. check 应返回 None(failed 允许重试)
        assert await mgr.check_receipt(action_id, effect_type, target) is None

        # 4. 重试场景: 重新 record_pending → record_completed
        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        await mgr.record_completed(action_id, effect_type, target, external_id="msg_retry")
        result = await mgr.check_receipt(action_id, effect_type, target)
        assert result is not None
        assert result["status"] == "completed"


# ════════════════════════════════════════════════════════════════
# 5. 单例 get_receipt_manager 测试
# ════════════════════════════════════════════════════════════════

class TestGetReceiptManager:
    """get_receipt_manager 单例行为测试。"""

    def test_get_receipt_manager_creates_singleton(self, real_store):
        """首次调用 get_receipt_manager 应创建单例。"""
        from services.effect_receipts import get_receipt_manager
        mgr = get_receipt_manager(real_store)
        assert mgr is not None

    def test_get_receipt_manager_returns_same_instance(self, real_store):
        """第二次调用 get_receipt_manager 应返回同一实例(单例)。"""
        from services.effect_receipts import get_receipt_manager
        mgr1 = get_receipt_manager(real_store)
        mgr2 = get_receipt_manager()  # 不传 cache_store 也应返回已有单例
        assert mgr1 is mgr2

    def test_get_receipt_manager_returns_none_without_cache_store(self):
        """未初始化时调用 get_receipt_manager() 不传 cache_store 应返回 None。"""
        from services.effect_receipts import get_receipt_manager
        # 单例已被 _reset_receipt_manager_singleton fixture 清空
        result = get_receipt_manager()
        assert result is None
