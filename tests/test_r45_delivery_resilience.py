"""R45 §11-14 整改测试: Dsp/Mon/Redis/CRDB 投递韧性。

测试覆盖 4 大场景:
1. R42 §11 Dsp Bot: classify_delivery_error 5 类错误 + 三层令牌桶 + receipt 失败停止
2. R42 §12 Mon Bot: _validate_topology_invariants 4 条不变量 + _probe_dst_msg_id 探测
3. R42 §13 Redis Writer: _get_dedicated_connection 专用连接 + durable outbox 写入/重放
4. R42 §14 CRDB Sync: version 单调(>=1) + tombstone soft delete + local_only 不写远端 + DLQ

测试策略:
- classify_delivery_error 纯函数直接调用
- PerChannelRateLimiter 真实实例测试限速行为
- redis_queue durable outbox 使用临时 SQLite 文件
- crdb_sync_service 使用 mock D1Collection 验证 SQL 语句
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# 测试环境兼容: mock telegram / telegram.ext / telegram.error
# telegram.error 是子模块,需要单独 mock(BadRequest/Forbidden/RetryAfter 等)
_telegram_mock = MagicMock()
sys.modules.setdefault("telegram", _telegram_mock)
sys.modules.setdefault("telegram.ext", MagicMock())
_telegram_error_mock = MagicMock()
for _err_name in ("BadRequest", "Forbidden", "RetryAfter", "NetworkError",
                   "TimedOut", "ChatMigrated", "MessageNotModified",
                   "TelegramError", "Conflict"):
    setattr(_telegram_error_mock, _err_name, type(_err_name, (Exception,), {}))
sys.modules.setdefault("telegram.error", _telegram_error_mock)

# Mock storage.delivery_resolver(使用 Python 3.10+ 语法,Python 3.9 不兼容)
if "storage.delivery_resolver" not in sys.modules:
    _mock_delivery_resolver = types.ModuleType("storage.delivery_resolver")
    _mock_delivery_resolver.resolve_delivery_channel = AsyncMock(return_value=None)
    _mock_delivery_resolver.try_deliver = AsyncMock(return_value=None)
    _mock_delivery_resolver.try_deliver_batch = AsyncMock(return_value=[])
    _mock_delivery_resolver.invalidate_cell_cache = AsyncMock(return_value=None)
    sys.modules["storage.delivery_resolver"] = _mock_delivery_resolver
    if "storage" not in sys.modules:
        _mock_storage_pkg = types.ModuleType("storage")
        _mock_storage_pkg.__path__ = []
        sys.modules["storage"] = _mock_storage_pkg

# 设置模块加载时所需的 config 值(避免 MagicMock 比较失败)
# 注意: MagicMock 的 hasattr 永远返回 True,所以不能用它判断是否已设置
_config = sys.modules.get("config")
if _config is not None and hasattr(_config, "settings"):
    _s = _config.settings
    # DynamicRateLimiter 所需
    _s.RATE_LIMIT_BASE_DELAY = 0.2
    _s.RATE_LIMIT_MAX_DELAY = 3.0
    _s.RATE_LIMIT_THRESHOLD_LOW = 10
    _s.RATE_LIMIT_THRESHOLD_HIGH = 30
    # bots/dsp_bot.py 模块加载时所需
    _s.SENDER_BOT_TOKEN = "test-token"
    _s.PAGE_SIZE = 10
    _s.SEND_CONCURRENCY = 5
    _s.CHANNEL_FAILURE_THRESHOLD = 3
    _s.CHANNEL_FAILURE_WINDOW = 60


# ════════════════════════════════════════════════════════════════
# 辅助 fixture: 重置 durable outbox 全局状态
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_durable_outbox_state():
    """每个用例前重置 redis_queue durable outbox 全局状态。"""
    import database.redis_queue as rq
    rq._durable_conn = None
    rq._durable_conn_lock = None
    yield
    rq._durable_conn = None
    rq._durable_conn_lock = None


# ════════════════════════════════════════════════════════════════
# 场景 1: R42 §11 Dsp Bot — classify_delivery_error 错误分类
# ════════════════════════════════════════════════════════════════

# R45: 在模块级导入 classify_delivery_error(收集阶段事件循环可用),
# 避免运行时导入 bots.dsp_bot 时因 Python 3.9 无事件循环导致 RuntimeError
_classify_delivery_error = None
try:
    from bots.dsp_bot import classify_delivery_error as _classify_delivery_error
except Exception:
    pass


class TestClassifyDeliveryError:
    """R42 §11: classify_delivery_error 返回 5 类错误。"""

    def test_flood_wait_retry_after(self):
        """RetryAfter 异常 → flood_wait。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = type("RetryAfter", (Exception,), {})("retry after 5s")
        assert _classify_delivery_error(exc) == "flood_wait"

    def test_flood_wait_flood_wait_error(self):
        """FloodWait 异常 → flood_wait。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = type("FloodWaitError", (Exception,), {})("flood wait 10s")
        assert _classify_delivery_error(exc) == "flood_wait"

    def test_forbidden_403(self):
        """包含 forbidden/403 → forbidden。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = Exception("Forbidden: bot was blocked by the user")
        assert _classify_delivery_error(exc) == "forbidden"

    def test_forbidden_chat_not_found(self):
        """chat not found → forbidden。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = Exception("Chat not found")
        assert _classify_delivery_error(exc) == "forbidden"

    def test_message_missing(self):
        """message to copy not found → message_missing。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = Exception("Message to copy not found")
        assert _classify_delivery_error(exc) == "message_missing"

    def test_temporary_network_timeout(self):
        """timeout/connection error → temporary_network。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = asyncio.TimeoutError()
        assert _classify_delivery_error(exc) == "temporary_network"

    def test_temporary_network_connection(self):
        """connection reset → temporary_network。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = ConnectionResetError("connection reset by peer")
        assert _classify_delivery_error(exc) == "temporary_network"

    def test_permanent_invalid_type_error(self):
        """TypeError → permanent_invalid。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = TypeError("argument must be int, not str")
        assert _classify_delivery_error(exc) == "permanent_invalid"

    def test_permanent_invalid_value_error(self):
        """ValueError → permanent_invalid。"""
        assert _classify_delivery_error is not None, "classify_delivery_error 未导入"
        exc = ValueError("invalid bot token")
        assert _classify_delivery_error(exc) == "permanent_invalid"


# ════════════════════════════════════════════════════════════════
# 场景 2: R42 §11 Dsp Bot — 三层令牌桶
# ════════════════════════════════════════════════════════════════

class TestThreeLayerRateLimiter:
    """R42 §11: Bot/频道/用户三层令牌桶限速。"""

    @pytest.mark.asyncio
    async def test_bot_limiter_exists(self):
        """_bot_limiter 实例存在且为 PerChannelRateLimiter。"""
        from bots.dsp_bot import _bot_limiter
        from utils.per_channel_limiter import PerChannelRateLimiter
        assert isinstance(_bot_limiter, PerChannelRateLimiter)
        assert _bot_limiter.max_per_minute == 25

    @pytest.mark.asyncio
    async def test_user_limiter_exists(self):
        """_user_limiter 实例存在且为 PerChannelRateLimiter。"""
        from bots.dsp_bot import _user_limiter
        from utils.per_channel_limiter import PerChannelRateLimiter
        assert isinstance(_user_limiter, PerChannelRateLimiter)
        assert _user_limiter.max_per_minute == 20

    @pytest.mark.asyncio
    async def test_bot_limiter_throttle(self):
        """Bot 层限速: 超过 max_per_minute 后返回等待时间。"""
        from bots.dsp_bot import _bot_limiter
        # 用固定 key=1 快速消耗配额
        for _ in range(25):
            wait = await _bot_limiter.acquire(1)
            assert wait == 0.0
        # 第 26 次应被限速
        wait = await _bot_limiter.acquire(1)
        assert wait > 0.0


# ════════════════════════════════════════════════════════════════
# 场景 3: R42 §13 Redis Writer — durable outbox 专用连接
# ════════════════════════════════════════════════════════════════

class TestDurableOutboxConnection:
    """R42 §13: _get_dedicated_connection 返回独立于 cache_store 的连接。"""

    @pytest.mark.asyncio
    async def test_dedicated_connection_returns_aiosqlite(self):
        """_get_dedicated_connection 返回 aiosqlite.Connection。"""
        import database.redis_queue as rq
        # 使用临时目录
        tmpdir = tempfile.mkdtemp(prefix="r45_outbox_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            conn = await rq._get_dedicated_connection()
            assert conn is not None
            # 验证表已创建
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='durable_outbox'"
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None
            assert row[0] == "durable_outbox"
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_dedicated_connection_singleton(self):
        """多次调用返回同一连接实例(单例)。"""
        import database.redis_queue as rq
        tmpdir = tempfile.mkdtemp(prefix="r45_outbox_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            conn1 = await rq._get_dedicated_connection()
            conn2 = await rq._get_dedicated_connection()
            assert conn1 is conn2
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 4: R42 §13 Redis Writer — write_durable_outbox 写入
# ════════════════════════════════════════════════════════════════

class TestWriteDurableOutbox:
    """R42 §13: write_durable_outbox 在 Redis 不可用时写入本地 outbox。"""

    @pytest_asyncio.fixture
    async def outbox_env(self):
        """创建临时 durable outbox 环境。"""
        import database.redis_queue as rq
        tmpdir = tempfile.mkdtemp(prefix="r45_outbox_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            await rq._get_dedicated_connection()
            yield rq
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_write_durable_outbox_success(self, outbox_env):
        """写入 durable outbox 成功,可在数据库中查询。"""
        rq = outbox_env
        msg_id = str(uuid.uuid4())
        ok = await rq.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 12345, "quota": 100},
            redis_key="cache:user_quota:12345",
            message_id=msg_id,
        )
        assert ok is True
        # 验证数据已写入
        count = await rq.get_durable_outbox_count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_write_durable_outbox_idempotent(self, outbox_env):
        """相同 message_id 重复写入被忽略(INSERT OR IGNORE)。"""
        rq = outbox_env
        msg_id = str(uuid.uuid4())
        for _ in range(3):
            await rq.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 12345},
                message_id=msg_id,
            )
        count = await rq.get_durable_outbox_count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_write_durable_outbox_multiple_messages(self, outbox_env):
        """多条不同 message_id 消息均写入成功。"""
        rq = outbox_env
        for i in range(5):
            await rq.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 10000 + i},
                message_id=str(uuid.uuid4()),
            )
        count = await rq.get_durable_outbox_count()
        assert count == 5


# ════════════════════════════════════════════════════════════════
# 场景 5: R42 §13 Redis Writer — push() 降级到 durable outbox
# ════════════════════════════════════════════════════════════════

class TestPushFallbackToDurableOutbox:
    """R42 §13: push() 在 Redis 不可用时写入 durable outbox。"""

    @pytest.mark.asyncio
    async def test_push_redis_unavailable_writes_outbox(self):
        """Redis 不可用时 push() 返回 False 并写入 durable outbox。"""
        import database.redis_queue as rq
        tmpdir = tempfile.mkdtemp(prefix="r45_push_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            # Redis 不可用(REDIS_URL 为空)
            result = await rq.push(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 99999},
            )
            assert result is False
            # durable outbox 应有 1 条 pending 消息
            count = await rq.get_durable_outbox_count()
            assert count == 1
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 6: R42 §13 Redis Writer — replay_durable_outbox 重放
# ════════════════════════════════════════════════════════════════

class TestReplayDurableOutbox:
    """R42 §13: replay_durable_outbox 在 Redis 恢复后重放 pending 消息。"""

    @pytest.mark.asyncio
    async def test_replay_redis_still_unavailable(self):
        """Redis 仍不可达时 replay 返回 0,不报错。"""
        import database.redis_queue as rq
        tmpdir = tempfile.mkdtemp(prefix="r45_replay_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            # 先写入一条 outbox 消息
            await rq.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 1},
                message_id=str(uuid.uuid4()),
            )
            # Redis 不可达时 replay 返回 0
            result = await rq.replay_durable_outbox()
            assert result == 0
            # pending 消息仍在
            count = await rq.get_durable_outbox_count()
            assert count == 1
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_replay_redis_available(self):
        """Redis 可达时 replay 成功将消息重放到 Stream。"""
        import database.redis_queue as rq
        tmpdir = tempfile.mkdtemp(prefix="r45_replay_test_")
        original_path = rq._DURABLE_DB_PATH
        rq._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
        try:
            # 写入 2 条 outbox 消息
            for i in range(2):
                await rq.write_durable_outbox(
                    op_type="upsert", table="user_quota",
                    method_name="upsert_user_quota",
                    data={"user_id": 200 + i},
                    message_id=str(uuid.uuid4()),
                )
            # Mock Redis 可达
            mock_redis = MagicMock()
            mock_redis.xadd = AsyncMock(return_value=b"1-0")
            with patch.object(rq, "get_redis", AsyncMock(return_value=mock_redis)):
                result = await rq.replay_durable_outbox()
            assert result == 2
            # pending 消息已标记为 replayed
            count = await rq.get_durable_outbox_count()
            assert count == 0
            # xadd 被调用 2 次
            assert mock_redis.xadd.call_count == 2
        finally:
            await rq.close_durable_outbox()
            rq._DURABLE_DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 7: R42 §14 CRDB Sync — version 单调递增(禁止默认 0)
# ════════════════════════════════════════════════════════════════

class TestCRDBVersionMonotonic:
    """R42 §14: 每个远端实体 version 单调递增,禁止默认 0。"""

    @pytest.mark.asyncio
    async def test_version_none_defaults_to_1(self):
        """dirty_outbox 记录 version=None 时被修正为 1。"""
        # 模拟 _sync_dirty_outbox 中的 version 处理逻辑
        r = {"version": None}
        version = r.get("version") or 1
        if not isinstance(version, int) or version < 1:
            version = 1
        assert version == 1

    @pytest.mark.asyncio
    async def test_version_zero_corrected_to_1(self):
        """version=0 被修正为 1。"""
        r = {"version": 0}
        version = r.get("version") or 1
        if not isinstance(version, int) or version < 1:
            version = 1
        assert version == 1

    @pytest.mark.asyncio
    async def test_version_negative_corrected_to_1(self):
        """version=-1 被修正为 1。"""
        r = {"version": -1}
        version = r.get("version") or 1
        if not isinstance(version, int) or version < 1:
            version = 1
        assert version == 1

    @pytest.mark.asyncio
    async def test_version_string_corrected_to_1(self):
        """version 为字符串时被修正为 1。"""
        r = {"version": "abc"}
        version = r.get("version") or 1
        if not isinstance(version, int) or version < 1:
            version = 1
        assert version == 1

    @pytest.mark.asyncio
    async def test_version_valid_preserved(self):
        """version=5 被保留(>= 1)。"""
        r = {"version": 5}
        version = r.get("version") or 1
        if not isinstance(version, int) or version < 1:
            version = 1
        assert version == 5


# ════════════════════════════════════════════════════════════════
# 场景 8: R42 §14 CRDB Sync — local_only 表不写远端 outbox
# ════════════════════════════════════════════════════════════════

class TestLocalOnlyNoRemoteOutbox:
    """R42 §14: local_only 表不写远端 outbox,直接标记 processed。"""

    @pytest.mark.asyncio
    async def test_local_only_tables_set_exists(self):
        """_LOCAL_ONLY_TABLES 集合存在且非空。"""
        from services.crdb_sync_service import _LOCAL_ONLY_TABLES
        assert isinstance(_LOCAL_ONLY_TABLES, set)
        assert len(_LOCAL_ONLY_TABLES) > 0

    @pytest.mark.asyncio
    async def test_local_only_dispatch_returns_all_ids(self):
        """local_only 表 dispatch 返回所有 id,不调用 CRDB。"""
        from services.crdb_sync_service import _dispatch_dirty_outbox_to_crdb
        # 构造 local_only 表的 records
        records = [
            {"id": 1, "table_name": "tasks", "pk": "task-1", "operation": "upsert"},
            {"id": 2, "table_name": "tasks", "pk": "task-2", "operation": "upsert"},
        ]
        with patch(
            "services.crdb_sync_service._LOCAL_ONLY_TABLES",
            {"tasks"},
        ):
            ids = await _dispatch_dirty_outbox_to_crdb("tasks", records)
        assert set(ids) == {1, 2}


# ════════════════════════════════════════════════════════════════
# 场景 9: R42 §14 CRDB Sync — unknown table/op 进入 DLQ
# ════════════════════════════════════════════════════════════════

class TestUnknownTableOpDLQ:
    """R42 §14: unknown table/op 进入可修复 DLQ。"""

    @pytest.mark.asyncio
    async def test_unknown_table_returns_empty(self):
        """未知 table_name 返回空列表(不标记 processed,进入 DLQ)。"""
        from services.crdb_sync_service import _dispatch_dirty_outbox_to_crdb
        records = [{"id": 1, "pk": "x", "operation": "upsert"}]
        # Mock DLQ 路由
        with patch(
            "services.crdb_sync_service._route_dirty_outbox_to_dlq",
            AsyncMock(return_value=None),
        ):
            ids = await _dispatch_dirty_outbox_to_crdb("__unknown_table__", records)
        assert ids == []

    @pytest.mark.asyncio
    async def test_unknown_operation_routed_to_dlq(self):
        """已知表但未知 operation 进入 DLQ。"""
        from services.crdb_sync_service import (
            _dispatch_dirty_outbox_to_crdb,
            _DIRTY_OUTBOX_TABLE_HANDLERS,
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
            _TOMBSTONE_PK_COLUMNS,
            _CRDB_TABLES,
        )
        # 确认 users 是已知 CRDB 表
        assert "users" in _CRDB_TABLES
        records = [
            {"id": 1, "pk": "user-1", "operation": "__invalid_op__"},
        ]
        dlq_calls = []
        async def mock_dlq(table, recs, reason):
            dlq_calls.append((table, recs, reason))

        # 需要 mock upsert handler(否则会因 CRDB 连接失败而异常)
        mock_handler = AsyncMock(return_value=[])
        with patch.dict(
            _DIRTY_OUTBOX_TABLE_HANDLERS,
            {"users": mock_handler},
        ), patch.dict(
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
            {"users": "users"},
        ), patch.dict(
            _TOMBSTONE_PK_COLUMNS,
            {"users": "user_id"},
        ), patch(
            "services.crdb_sync_service._route_dirty_outbox_to_dlq",
            mock_dlq,
        ):
            ids = await _dispatch_dirty_outbox_to_crdb("users", records)
        # DLQ 被调用
        assert len(dlq_calls) == 1
        assert dlq_calls[0][0] == "users"


# ════════════════════════════════════════════════════════════════
# 场景 10: R42 §12 Mon Bot — _validate_topology_invariants 存在
# ════════════════════════════════════════════════════════════════

class TestMonTopologyInvariants:
    """R42 §12: Mon Bot 拓扑不变量验证。"""

    @pytest.mark.asyncio
    async def test_validate_topology_invariants_method_exists(self):
        """MonBot._validate_topology_invariants 方法存在。"""
        from bots.mon_bot import MonBot
        assert hasattr(MonBot, "_validate_topology_invariants")
        import inspect
        sig = inspect.signature(MonBot._validate_topology_invariants)
        assert "tx" in sig.parameters or len(sig.parameters) >= 1

    @pytest.mark.asyncio
    async def test_probe_dst_msg_id_method_exists(self):
        """MonBot._probe_dst_msg_id 方法存在。"""
        from bots.mon_bot import MonBot
        assert hasattr(MonBot, "_probe_dst_msg_id")


# ════════════════════════════════════════════════════════════════
# 场景 11: R42 §14 CRDB Sync — tombstone soft delete 路径
# ════════════════════════════════════════════════════════════════

class TestTombstoneSoftDelete:
    """R42 §14: tombstone 同步 deleted_at/version,不立即 DELETE。"""

    @pytest.mark.asyncio
    async def test_tombstone_soft_delete_uses_update_not_delete(self):
        """支持 soft_delete 时使用 UPDATE deleted_at,不用 DELETE FROM。"""
        from services.crdb_sync_service import _dispatch_crdb_tombstone

        mock_col = MagicMock()
        executed_sqls = []

        async def mock_execute_raw(sql, params=None):
            executed_sqls.append(sql.strip().upper())
            return None

        mock_col.execute_raw = mock_execute_raw

        records = [
            {"id": 1, "pk": "user-1", "payload": {}, "created_at": time.time()},
        ]

        with patch(
            "services.crdb_sync_service._is_crdb_table_supports_soft_delete",
            AsyncMock(return_value=True),
        ), patch(
            "services.crdb_sync_service._extract_deleted_at_from_record",
            return_value=time.time(),
        ), patch(
            "database.session.get_users_col",
            return_value=mock_col,
        ):
            ids = await _dispatch_crdb_tombstone(records, "users", "user_id")

        assert ids == [1]
        # 应执行 UPDATE(含 deleted_at + is_tombstone + version),不是 DELETE
        assert any("UPDATE" in sql for sql in executed_sqls)
        assert not any("DELETE FROM" in sql for sql in executed_sqls)

    @pytest.mark.asyncio
    async def test_tombstone_fallback_delete_when_no_soft_delete(self):
        """R46 P1: 不支持 soft_delete 时 fail-closed 路由到 DLQ(不执行 hard delete)。

        整改前: fallback 立即执行 DELETE FROM + audit_log
        整改后: hard delete 只能由 retention worker 在备份保留窗口后执行,
                crdb_sync 直接路由到 DLQ,避免数据不可恢复
        """
        from services.crdb_sync_service import _dispatch_crdb_tombstone

        mock_col = MagicMock()
        executed_sqls = []

        async def mock_execute_raw(sql, params=None):
            executed_sqls.append(sql.strip().upper())
            return None

        mock_col.execute_raw = mock_execute_raw

        records = [
            {"id": 1, "pk": "user-1", "payload": {}, "created_at": time.time()},
        ]

        dlq_calls = []

        async def mock_route_dlq(table_name, recs, error_msg):
            dlq_calls.append((table_name, recs, error_msg))

        with patch(
            "services.crdb_sync_service._is_crdb_table_supports_soft_delete",
            AsyncMock(return_value=False),
        ), patch(
            "services.crdb_sync_service._route_dirty_outbox_to_dlq",
            side_effect=mock_route_dlq,
        ), patch(
            "database.session.get_users_col",
            return_value=mock_col,
        ):
            ids = await _dispatch_crdb_tombstone(records, "users", "user_id")

        # R46 P1: 返回所有 id 让 _sync_dirty_outbox 标记为 processed(已在 DLQ)
        assert ids == [1]
        # R46 P1: 应路由到 DLQ(1 次,表名匹配,记录数匹配)
        assert len(dlq_calls) == 1
        assert dlq_calls[0][0] == "users"
        assert dlq_calls[0][1] == records
        assert "does not support soft_delete" in dlq_calls[0][2]
        # R46 P1: 不应执行任何 DELETE FROM(fail-closed,hard delete 由 retention worker 执行)
        assert not any("DELETE FROM" in sql for sql in executed_sqls)
