"""R51 P0-5: Notification Outbox 可被吞异常问题 — 修复验证测试。

被测目标:
- services/notifications.py: send() 异常处理 + persist_only + CAS
- database/cache_store.py: notification_outbox 唯一约束 DDL

测试场景:
1. 正常 send → notifications + notification_outbox + dirty_outbox 全部写入
2. outbox 写入失败 → transaction 回滚,notifications 也不写入(无孤儿)
3. persist_only=True → 只写 notifications,不写 outbox
4. dedup_key 重复 → 唯一约束阻止重复插入
5. 并发 send 同一 dedup_key → CAS 防止重复 attempts
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock telegram(python-telegram-bot 未安装于测试环境)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

import pytest
import pytest_asyncio

from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(共享给所有 service 模块)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def store():
    """创建使用临时文件数据库的 CacheStore 实例。

    设置 _cs_module._store 让所有模块(notifications)的 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p0_5_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def reset_outbox_schema():
    """每个用例前重置 notifications outbox schema 标记。"""
    from services import notifications
    notifications._reset_outbox_schema_for_test()
    yield
    notifications._reset_outbox_schema_for_test()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _count_notifications(store, user_id: int) -> int:
    """统计用户的通知数量。"""
    rows = await store._db.execute_fetchall(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
        (user_id,),
    )
    return int(rows[0][0]) if rows else 0


async def _count_outbox(store, user_id: int) -> int:
    """统计用户的 outbox 记录数量。"""
    rows = await store._db.execute_fetchall(
        "SELECT COUNT(*) FROM notification_outbox WHERE user_id = ?",
        (user_id,),
    )
    return int(rows[0][0]) if rows else 0


async def _count_dirty_outbox(store, table_name: str) -> int:
    """统计 dirty_outbox 中指定表的记录数量。"""
    rows = await store._db.execute_fetchall(
        "SELECT COUNT(*) FROM dirty_outbox WHERE table_name = ?",
        (table_name,),
    )
    return int(rows[0][0]) if rows else 0


# ════════════════════════════════════════════════════════════════
# 测试 1: 正常 send → notifications + notification_outbox + dirty_outbox 全部写入
# ════════════════════════════════════════════════════════════════

class TestSendNormalFlow:
    """正常 send 流程:notifications + notification_outbox + dirty_outbox 同事务写入。"""

    @pytest.mark.asyncio
    async def test_send_writes_all_three_tables(self, store, reset_outbox_schema):
        """send() 默认(persist_only=False)同事务写 notifications + outbox + dirty_outbox。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5001, notif_type="ready",
            payload={"file_code": "R51_001"},
        )
        assert notif_id > 0
        # 验证 notifications 表有记录
        assert await _count_notifications(store, 5001) == 1
        # 验证 notification_outbox 表有记录
        assert await _count_outbox(store, 5001) == 1
        # 验证 dirty_outbox 有 notifications + notification_outbox 记录
        dirty_notifs = await _count_dirty_outbox(store, "notifications")
        dirty_outbox = await _count_dirty_outbox(store, "notification_outbox")
        assert dirty_notifs >= 1, "dirty_outbox 应有 notifications 记录"
        assert dirty_outbox >= 1, "dirty_outbox 应有 notification_outbox 记录"

    @pytest.mark.asyncio
    async def test_send_outbox_status_pending(self, store, reset_outbox_schema):
        """send() 写入的 outbox 记录 status='pending'。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5002, notif_type="ready",
            payload={"file_code": "R51_002"},
        )
        assert notif_id > 0
        rows = await store._db.execute_fetchall(
            "SELECT status, attempts, dedup_key, window_start "
            "FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] == "pending"   # status
        assert rows[0][1] == 0           # attempts
        assert rows[0][2] == ""          # dedup_key(无 dedup_key 时为空)
        assert rows[0][3] is None        # window_start(无 dedup_key 时为 NULL)


# ════════════════════════════════════════════════════════════════
# 测试 2: outbox 写入失败 → transaction 回滚,notifications 也不写入(无孤儿)
# ════════════════════════════════════════════════════════════════

class TestSendOutboxFailureRollback:
    """outbox 写入失败时 transaction 回滚,避免孤儿通知。"""

    @pytest.mark.asyncio
    async def test_outbox_failure_rolls_back_notifications(
        self, store, reset_outbox_schema
    ):
        """outbox 写入失败 → notifications 也不写入(无孤儿)。"""
        from services import notifications
        # 先正常发送一条(确保 schema 已初始化)
        await notifications.send(5003, "ready", {"file_code": "INIT"})

        # Mock tx.execute 在第二次调用(写 notification_outbox)时抛异常
        # 策略:patch transaction 内的 execute,让 INSERT INTO notification_outbox 失败
        original_execute = store._db.execute

        call_count = {"n": 0}

        async def mock_execute(sql, params=None):
            # 检测到 INSERT INTO notification_outbox → 抛异常
            if "INSERT INTO notification_outbox" in sql:
                call_count["n"] += 1
                raise RuntimeError("simulated outbox write failure")
            # 其他 SQL 正常执行(但注意:事务内的 INSERT INTO notifications 也会走这里)
            return await original_execute(sql, params)

        # patch store._db.execute(在事务内,tx 就是 store._db)
        with patch.object(store._db, "execute", side_effect=mock_execute):
            notif_id = await notifications.send(
                user_id=5004, notif_type="ready",
                payload={"file_code": "R51_ORPHAN"},
            )

        # send() 应返回 0(失败)
        assert notif_id == 0, "outbox 写入失败时 send() 应返回 0"
        # 验证 notifications 表没有孤儿记录
        assert await _count_notifications(store, 5004) == 0, \
            "outbox 写入失败时 notifications 也不应写入(无孤儿)"
        # 验证 notification_outbox 表没有记录
        assert await _count_outbox(store, 5004) == 0

    @pytest.mark.asyncio
    async def test_outbox_failure_no_partial_commit(
        self, store, reset_outbox_schema
    ):
        """outbox 写入失败不会留下任何半提交状态。"""
        from services import notifications
        # 记录初始状态
        initial_notifs = await _count_notifications(store, 5005)
        initial_outbox = await _count_outbox(store, 5005)

        # 让 notification_outbox 的 INSERT 失败
        original_execute = store._db.execute

        async def mock_execute(sql, params=None):
            if "INSERT INTO notification_outbox" in sql:
                raise RuntimeError("simulated outbox constraint violation")
            return await original_execute(sql, params)

        with patch.object(store._db, "execute", side_effect=mock_execute):
            result = await notifications.send(
                user_id=5005, notif_type="ready",
                payload={"file_code": "R51_PARTIAL"},
            )

        assert result == 0
        # 验证没有任何新增记录
        assert await _count_notifications(store, 5005) == initial_notifs
        assert await _count_outbox(store, 5005) == initial_outbox


# ════════════════════════════════════════════════════════════════
# 测试 3: persist_only=True → 只写 notifications,不写 outbox
# ════════════════════════════════════════════════════════════════

class TestPersistOnlyMode:
    """persist_only=True 仅写 notifications 表(历史型通知)。"""

    @pytest.mark.asyncio
    async def test_persist_only_skips_outbox(self, store, reset_outbox_schema):
        """persist_only=True → 只写 notifications,不写 outbox。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5006, notif_type="ready",
            payload={"file_code": "R51_HISTORY"},
            persist_only=True,
        )
        assert notif_id > 0
        # notifications 表有记录
        assert await _count_notifications(store, 5006) == 1
        # notification_outbox 表没有记录
        assert await _count_outbox(store, 5006) == 0, \
            "persist_only=True 不应写 notification_outbox"

    @pytest.mark.asyncio
    async def test_persist_only_dirty_outbox_only_notifications(
        self, store, reset_outbox_schema
    ):
        """persist_only=True → dirty_outbox 只有 notifications 记录,无 outbox 记录。"""
        from services import notifications
        # 记录初始 dirty_outbox 数量
        initial_dirty_notifs = await _count_dirty_outbox(store, "notifications")
        initial_dirty_outbox = await _count_dirty_outbox(store, "notification_outbox")

        await notifications.send(
            user_id=5007, notif_type="ready",
            payload={"file_code": "R51_DIRTY"},
            persist_only=True,
        )
        # dirty_outbox 中 notifications 记录数应增加
        assert await _count_dirty_outbox(store, "notifications") > initial_dirty_notifs
        # dirty_outbox 中 notification_outbox 记录数应不变
        assert await _count_dirty_outbox(store, "notification_outbox") == initial_dirty_outbox

    @pytest.mark.asyncio
    async def test_default_persist_only_is_false(self, store, reset_outbox_schema):
        """默认 persist_only=False → 写 outbox(可投递)。"""
        from services import notifications
        # 不传 persist_only(默认 False)
        notif_id = await notifications.send(
            user_id=5008, notif_type="ready",
            payload={"file_code": "R51_DEFAULT"},
        )
        assert notif_id > 0
        assert await _count_outbox(store, 5008) == 1, \
            "默认 persist_only=False 应写 notification_outbox"


# ════════════════════════════════════════════════════════════════
# 测试 4: dedup_key 重复 → 唯一约束阻止重复插入
# ════════════════════════════════════════════════════════════════

class TestDedupUniqueConstraint:
    """dedup_key + window_start 唯一约束阻止重复插入。"""

    @pytest.mark.asyncio
    async def test_duplicate_dedup_key_blocked_by_unique_constraint(
        self, store, reset_outbox_schema
    ):
        """同一 user_id + dedup_key + window_start 的重复插入被唯一约束阻止。"""
        from services import notifications
        # 第一次发送(带 dedup_key)
        id1 = await notifications.send(
            user_id=5009, notif_type="ready",
            payload={"file_code": "R51_DEDUP_1", "_dedup_key": "task_complete:5009"},
        )
        assert id1 > 0
        # 验证第一次写入成功
        assert await _count_outbox(store, 5009) == 1

        # 第二次发送(相同 user_id + dedup_key,同一时间窗口)
        # 由于 window_start 相同(整点对齐),唯一约束应阻止重复
        id2 = await notifications.send(
            user_id=5009, notif_type="ready",
            payload={"file_code": "R51_DEDUP_2", "_dedup_key": "task_complete:5009"},
        )
        # R53 P1-1: send() 委托 send_with_dedup_contract(),dedup 命中时
        # 返回现有权威记录的 notif_id(>0),而非 0(旧行为返回 0 无法区分 dedup/error)
        assert id2 == id1, \
            "同一 dedup_key + window 的重复插入应返回现有 notif_id(去重命中)"
        # outbox 表仍只有 1 条记录(第二次被阻止)
        assert await _count_outbox(store, 5009) == 1
        # notifications 表也只有 1 条记录(事务回滚)
        assert await _count_notifications(store, 5009) == 1

    @pytest.mark.asyncio
    async def test_different_dedup_keys_allowed(self, store, reset_outbox_schema):
        """不同 dedup_key 不冲突,均可写入。"""
        from services import notifications
        id1 = await notifications.send(
            user_id=5010, notif_type="ready",
            payload={"file_code": "R51_A", "_dedup_key": "key_a"},
        )
        id2 = await notifications.send(
            user_id=5010, notif_type="ready",
            payload={"file_code": "R51_B", "_dedup_key": "key_b"},
        )
        assert id1 > 0
        assert id2 > 0
        assert id1 != id2
        assert await _count_outbox(store, 5010) == 2

    @pytest.mark.asyncio
    async def test_same_dedup_key_different_users_allowed(
        self, store, reset_outbox_schema
    ):
        """同一 dedup_key 不同 user_id 不冲突。"""
        from services import notifications
        id1 = await notifications.send(
            user_id=5011, notif_type="ready",
            payload={"file_code": "R51_U1", "_dedup_key": "shared_key"},
        )
        id2 = await notifications.send(
            user_id=5012, notif_type="ready",
            payload={"file_code": "R51_U2", "_dedup_key": "shared_key"},
        )
        assert id1 > 0
        assert id2 > 0
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_window_start_stored_in_outbox(
        self, store, reset_outbox_schema
    ):
        """有 dedup_key 时,window_start 被存储(非 NULL)。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5013, notif_type="ready",
            payload={"file_code": "R51_WIN", "_dedup_key": "win_test"},
        )
        assert notif_id > 0
        rows = await store._db.execute_fetchall(
            "SELECT dedup_key, window_start FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] == "win_test"
        assert rows[0][1] is not None, "有 dedup_key 时 window_start 应为非 NULL"


# ════════════════════════════════════════════════════════════════
# 测试 5: 并发 send 同一 dedup_key → CAS 防止重复 attempts
# ════════════════════════════════════════════════════════════════

class TestCASConcurrency:
    """CAS(Compare-And-Swap)防止并发重复 attempts。"""

    @pytest.mark.asyncio
    async def test_cas_delivered_skips_terminal_state(
        self, store, reset_outbox_schema
    ):
        """outbox 已 delivered 后,再次 record_receipt('delivered') 不重复更新。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5014, notif_type="ready",
            payload={"file_code": "R51_CAS_D"},
        )
        assert notif_id > 0
        # 第一次投递成功
        r1 = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=5014,
            channel="telegram", status="delivered",
        )
        assert r1 > 0
        # 验证 outbox 状态为 delivered
        rows = await store._db.execute_fetchall(
            "SELECT status, delivered_at FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "delivered"
        first_delivered_at = rows[0][1]
        # 第二次投递成功(CAS 应跳过,因为已为终态)
        r2 = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=5014,
            channel="telegram", status="delivered",
        )
        # receipt 仍然写入(receipt_id > 0),但 outbox 不更新
        assert r2 > 0
        # outbox 状态仍为 delivered,delivered_at 不变
        rows = await store._db.execute_fetchall(
            "SELECT status, delivered_at FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "delivered"
        assert rows[0][1] == first_delivered_at, "CAS 应跳过已终态的 outbox"

    @pytest.mark.asyncio
    async def test_cas_failed_increments_attempts_only_for_active(
        self, store, reset_outbox_schema
    ):
        """outbox 已 skipped 后,再次 record_receipt('failed') 不增加 attempts。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5015, notif_type="ready",
            payload={"file_code": "R51_CAS_F"},
        )
        assert notif_id > 0
        # 手动设 max_attempts=1,使一次失败就 skipped
        await store._db.execute(
            "UPDATE notification_outbox SET max_attempts = 1 WHERE notif_id = ?",
            (notif_id,),
        )
        await store._db.commit()
        # 第一次失败 → attempts=1 >= max_attempts=1 → skipped
        r1 = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=5015,
            channel="telegram", status="failed", error="first_fail",
        )
        assert r1 > 0
        rows = await store._db.execute_fetchall(
            "SELECT status, attempts FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "skipped"
        assert rows[0][1] == 1
        # 第二次失败(CAS 应跳过,因为已为 skipped 终态)
        r2 = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=5015,
            channel="telegram", status="failed", error="second_fail",
        )
        assert r2 > 0  # receipt 仍写入
        # attempts 不应增加(CAS 跳过)
        rows = await store._db.execute_fetchall(
            "SELECT status, attempts FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "skipped"
        assert rows[0][1] == 1, "CAS 应阻止 skipped 状态下的 attempts 递增"

    @pytest.mark.asyncio
    async def test_cas_mark_skipped_only_for_pending_failed(
        self, store, reset_outbox_schema
    ):
        """mark_outbox_skipped 仅对 pending/failed 生效,终态跳过。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5016, notif_type="ready",
            payload={"file_code": "R51_CAS_S"},
        )
        assert notif_id > 0
        # 先标记为 delivered
        await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=5016,
            channel="telegram", status="delivered",
        )
        # 获取 outbox_id
        rows = await store._db.execute_fetchall(
            "SELECT id, status FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        outbox_id = rows[0][0]
        assert rows[0][1] == "delivered"
        # 尝试标记 skipped(CAS 应跳过,因为已为 delivered 终态)
        ok = await notifications.mark_outbox_skipped(outbox_id, reason="should_not_apply")
        assert ok is False, "CAS 应跳过已 delivered 的 outbox"
        # 状态仍为 delivered
        rows = await store._db.execute_fetchall(
            "SELECT status FROM notification_outbox WHERE id = ?",
            (outbox_id,),
        )
        assert rows[0][0] == "delivered"

    @pytest.mark.asyncio
    async def test_cas_mark_skipped_for_pending_succeeds(
        self, store, reset_outbox_schema
    ):
        """mark_outbox_skipped 对 pending 状态正常生效。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=5017, notif_type="ready",
            payload={"file_code": "R51_CAS_P"},
        )
        assert notif_id > 0
        rows = await store._db.execute_fetchall(
            "SELECT id, status FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        outbox_id = rows[0][0]
        assert rows[0][1] == "pending"
        # 标记 skipped 应成功
        ok = await notifications.mark_outbox_skipped(outbox_id, reason="user_gone")
        assert ok is True
        rows = await store._db.execute_fetchall(
            "SELECT status, last_error FROM notification_outbox WHERE id = ?",
            (outbox_id,),
        )
        assert rows[0][0] == "skipped"
        assert "user_gone" in rows[0][1]


# ════════════════════════════════════════════════════════════════
# 测试 6: dispatch_notification 与唯一约束协同
# ════════════════════════════════════════════════════════════════

class TestDispatchWithUniqueConstraint:
    """dispatch_notification 与唯一约束协同工作。"""

    @pytest.mark.asyncio
    async def test_dispatch_dedup_first_call_succeeds(
        self, store, reset_outbox_schema
    ):
        """dispatch_notification 第一次调用成功,写 outbox + dedup_key。"""
        from services import notifications
        notif_id = await notifications.dispatch_notification(
            user_id=5018, type="ready",
            content={"file_code": "R51_DISP"},
            dedup_key="dispatch:5018:1",
        )
        assert notif_id > 0
        # outbox 应有记录,且 dedup_key 已存储
        rows = await store._db.execute_fetchall(
            "SELECT dedup_key, window_start FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "dispatch:5018:1"
        assert rows[0][1] is not None

    @pytest.mark.asyncio
    async def test_dispatch_dedup_second_call_returns_zero(
        self, store, reset_outbox_schema
    ):
        """dispatch_notification 同一 dedup_key 第二次调用返回 0(去重命中)。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=5019, type="ready",
            content={"file_code": "R51_DISP2"},
            dedup_key="dispatch:5019:dup",
        )
        assert id1 > 0
        id2 = await notifications.dispatch_notification(
            user_id=5019, type="ready",
            content={"file_code": "R51_DISP2"},
            dedup_key="dispatch:5019:dup",
        )
        assert id2 == 0, "同一 dedup_key 第二次调用应返回 0(去重)"


# ════════════════════════════════════════════════════════════════
# 测试 7: ErrorCodes 注册验证
# ════════════════════════════════════════════════════════════════

class TestErrorCodesRegistered:
    """R51 P0-5 新增的 ErrorCodes 已正确注册。"""

    def test_outbox_write_failed_code_registered(self):
        """NOTIFICATION_OUTBOX_WRITE_FAILED 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        assert ErrorRegistry.is_registered(
            ErrorCodes.NOTIFICATION_OUTBOX_WRITE_FAILED
        ), "NOTIFICATION_OUTBOX_WRITE_FAILED 应已注册"

    def test_outbox_duplicate_code_registered(self):
        """NOTIFICATION_OUTBOX_DUPLICATE 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        assert ErrorRegistry.is_registered(
            ErrorCodes.NOTIFICATION_OUTBOX_DUPLICATE
        ), "NOTIFICATION_OUTBOX_DUPLICATE 应已注册"

    def test_outbox_write_failed_definition(self):
        """NOTIFICATION_OUTBOX_WRITE_FAILED 的 ErrorDefinition 正确。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        defn = ErrorRegistry.get(ErrorCodes.NOTIFICATION_OUTBOX_WRITE_FAILED)
        assert defn.code == "NOTIFICATION.OUTBOX.WRITE_FAILED"
        assert defn.http_status == 500
        assert defn.retryable is True
        assert defn.severity == "error"
        assert "user_id" in defn.safe_params
        assert "notif_type" in defn.safe_params

    def test_outbox_duplicate_definition(self):
        """NOTIFICATION_OUTBOX_DUPLICATE 的 ErrorDefinition 正确。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        defn = ErrorRegistry.get(ErrorCodes.NOTIFICATION_OUTBOX_DUPLICATE)
        assert defn.code == "NOTIFICATION.OUTBOX.DUPLICATE"
        assert defn.http_status == 409
        assert defn.retryable is False
        assert defn.severity == "info"
        assert "user_id" in defn.safe_params
        assert "dedup_key" in defn.safe_params
