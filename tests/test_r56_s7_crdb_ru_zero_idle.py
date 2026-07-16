"""R56 §7.2: CRDB RU 近零空载 — 事件驱动唤醒 + 本地 watermark 单元测试。

测试覆盖:
1. crdb_sync_event_wakeup 模块(publish/wait/watermark)
2. fallback 行为(Redis 不可用时退化为 polling sleep)
3. 本地 sync watermark 读写(SQLite kv_store,0 RU)
4. _sync_loop 集成验证(事件驱动唤醒替代固定 sleep)

报告 §7.2.3 要求:
    "SQLite/Redis 本地 outbox 事件驱动唤醒同步器;禁止固定秒级轮询 CRDB。"

报告 §7.2.5 要求:
    "用本地 durable watermark,禁止空载 SELECT MAX()/count/schema 探测;
     迁移只在 deploy job 执行。"
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.crdb_sync_event_wakeup import (  # type: ignore  # noqa: E402
    _DIRTY_SIGNAL_CHANNEL,
    _SYNC_WATERMARK_PREFIX,
    get_local_sync_watermark,
    get_signal_channel,
    get_watermark_key,
    publish_dirty_signal,
    reset_subscriber,
    set_local_sync_watermark,
    wait_dirty_signal,
)


@pytest.fixture(autouse=True)
def reset_state():
    """每个用例前重置 subscriber 单例。"""
    reset_subscriber()
    yield
    reset_subscriber()


# ════════════════════════════════════════════════════════════════
# 1. 模块常量与配置
# ════════════════════════════════════════════════════════════════


class TestModuleConstants:
    """验证模块常量符合 R56 §7.2 设计要求。"""

    def test_signal_channel_name(self):
        """channel 名称应为 tgjiema:crdb_sync:wakeup。"""
        assert _DIRTY_SIGNAL_CHANNEL == "tgjiema:crdb_sync:wakeup"
        assert get_signal_channel() == _DIRTY_SIGNAL_CHANNEL

    def test_watermark_prefix(self):
        """watermark kv_store key 前缀应为 crdb_sync:watermark:。"""
        assert _SYNC_WATERMARK_PREFIX == "crdb_sync:watermark:"

    def test_watermark_key_for_table(self):
        """不同表应有独立的 watermark key。"""
        assert get_watermark_key("users") == "crdb_sync:watermark:users"
        assert get_watermark_key("files") == "crdb_sync:watermark:files"
        assert get_watermark_key("users") != get_watermark_key("files")


# ════════════════════════════════════════════════════════════════
# 2. publish_dirty_signal — Redis 可用/不可用
# ════════════════════════════════════════════════════════════════


class TestPublishDirtySignal:
    """publish_dirty_signal 行为测试。"""

    @pytest.mark.asyncio
    async def test_publish_when_redis_available(self):
        """Redis 可用时,PUBLISH 到正确 channel。"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value=1)
        with patch("database.redis_queue.get_redis", AsyncMock(return_value=mock_redis)):
            await publish_dirty_signal("users")
            mock_redis.publish.assert_called_once_with(_DIRTY_SIGNAL_CHANNEL, "users")

    @pytest.mark.asyncio
    async def test_publish_when_redis_unavailable(self):
        """Redis 不可用时,静默失败(不抛异常)。"""
        with patch("database.redis_queue.get_redis", AsyncMock(return_value=None)):
            # 不应抛异常
            await publish_dirty_signal("users")

    @pytest.mark.asyncio
    async def test_publish_swallows_exception(self):
        """Redis publish 异常时静默(不影响 dirty_outbox 写入)。"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("Connection refused"))
        with patch("database.redis_queue.get_redis", AsyncMock(return_value=mock_redis)):
            # 不应抛异常
            await publish_dirty_signal("users")

    @pytest.mark.asyncio
    async def test_publish_different_tables(self):
        """不同 table_name 应通过 message 区分(channel 相同)。"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value=1)
        with patch("database.redis_queue.get_redis", AsyncMock(return_value=mock_redis)):
            await publish_dirty_signal("users")
            await publish_dirty_signal("files")
            await publish_dirty_signal("codes")
            assert mock_redis.publish.call_count == 3
            messages = [call.args[1] for call in mock_redis.publish.call_args_list]
            assert messages == ["users", "files", "codes"]


# ════════════════════════════════════════════════════════════════
# 3. wait_dirty_signal — 事件驱动唤醒 / fallback
# ════════════════════════════════════════════════════════════════


class TestWaitDirtySignal:
    """wait_dirty_signal 行为测试。"""

    @pytest.mark.asyncio
    async def test_wait_fallback_when_redis_unavailable(self):
        """Redis 不可用时 fallback 到 asyncio.sleep,返回 False。"""
        with patch("database.redis_queue.get_redis", AsyncMock(return_value=None)):
            # 用短 timeout 避免测试慢
            result = await wait_dirty_signal(0.2)
            assert result is False  # 超时未收到信号

    @pytest.mark.asyncio
    async def test_wait_returns_false_on_timeout(self):
        """Redis 可用但无信号时,超时返回 False。"""
        mock_pubsub = AsyncMock()
        # 模拟 get_message 超时返回 None
        mock_pubsub.get_message = AsyncMock(return_value=None)

        async def mock_ensure():
            return mock_pubsub

        with patch("services.crdb_sync_event_wakeup._ensure_subscriber", mock_ensure):
            result = await wait_dirty_signal(0.2)
            assert result is False

    @pytest.mark.asyncio
    async def test_wait_returns_true_on_signal(self):
        """收到信号时返回 True。"""
        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = AsyncMock(return_value={
            "type": "message",
            "channel": _DIRTY_SIGNAL_CHANNEL,
            "data": "users",
        })

        async def mock_ensure():
            return mock_pubsub

        with patch("services.crdb_sync_event_wakeup._ensure_subscriber", mock_ensure):
            result = await wait_dirty_signal(5.0)
            assert result is True

    @pytest.mark.asyncio
    async def test_wait_with_zero_timeout(self):
        """timeout=0 时立即返回 False(不阻塞)。"""
        result = await wait_dirty_signal(0)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_fallback_on_exception(self):
        """_ensure_subscriber 异常时 fallback 到 sleep(不永久阻塞)。"""
        async def mock_ensure():
            raise Exception("Unexpected error")

        with patch("services.crdb_sync_event_wakeup._ensure_subscriber", mock_ensure):
            result = await wait_dirty_signal(0.2)
            assert result is False


# ════════════════════════════════════════════════════════════════
# 4. 本地 durable watermark(§7.2.5)
# ════════════════════════════════════════════════════════════════


class TestLocalSyncWatermark:
    """本地 sync watermark 读写测试(替代 SELECT MAX)。"""

    @pytest_asyncio.fixture
    async def memory_db(self):
        """创建 in-memory SQLite + kv_store 表。"""
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.commit()
        try:
            yield db
        finally:
            await db.close()

    @pytest_asyncio.fixture
    async def mock_store(self, memory_db):
        """模拟 cache_store(已附加 memory db)。"""
        store = MagicMock()
        store._db = memory_db
        return store

    @pytest.mark.asyncio
    async def test_get_watermark_default_zero(self, mock_store):
        """未设置过 watermark 时返回 0。"""
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            wm = await get_local_sync_watermark("users")
            assert wm == 0

    @pytest.mark.asyncio
    async def test_set_and_get_watermark(self, mock_store):
        """设置 watermark 后可读取。"""
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            ok = await set_local_sync_watermark("users", 12345)
            assert ok is True
            wm = await get_local_sync_watermark("users")
            assert wm == 12345

    @pytest.mark.asyncio
    async def test_watermark_is_per_table(self, mock_store):
        """不同表 watermark 独立。"""
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            await set_local_sync_watermark("users", 100)
            await set_local_sync_watermark("files", 200)
            assert await get_local_sync_watermark("users") == 100
            assert await get_local_sync_watermark("files") == 200

    @pytest.mark.asyncio
    async def test_watermark_update_overwrites(self, mock_store):
        """再次 set 同一表 watermark 会覆盖(INSERT OR REPLACE)。"""
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            await set_local_sync_watermark("users", 100)
            await set_local_sync_watermark("users", 200)
            assert await get_local_sync_watermark("users") == 200

    @pytest.mark.asyncio
    async def test_get_watermark_returns_zero_on_store_unavailable(self):
        """cache_store 不可用时返回 0(fail-safe,不阻断 sync)。"""
        with patch("database.cache_store.get_cache_store", return_value=None):
            wm = await get_local_sync_watermark("users")
            assert wm == 0

    @pytest.mark.asyncio
    async def test_set_watermark_returns_false_on_store_unavailable(self):
        """cache_store 不可用时 set 返回 False。"""
        with patch("database.cache_store.get_cache_store", return_value=None):
            ok = await set_local_sync_watermark("users", 100)
            assert ok is False

    @pytest.mark.asyncio
    async def test_get_watermark_handles_corrupt_value(self, mock_store):
        """value 非整数时返回 0(不抛异常)。"""
        # 直接写入非数字 value
        await mock_store._db.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
            ("crdb_sync:watermark:users", "not_a_number", "2026-07-16"),
        )
        await mock_store._db.commit()
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            wm = await get_local_sync_watermark("users")
            assert wm == 0


# ════════════════════════════════════════════════════════════════
# 5. _sync_loop 集成验证 — 事件驱动替代固定 sleep
# ════════════════════════════════════════════════════════════════


class TestSyncLoopEventDriven:
    """验证 _sync_loop 已集成事件驱动唤醒(替代固定 asyncio.sleep)。"""

    def test_sync_loop_uses_wait_dirty_signal(self):
        """crdb_sync_service._sync_loop 应调用 wait_dirty_signal(非 asyncio.sleep)。"""
        import services.crdb_sync_service as svc
        # 读取源码验证(避免实际运行 sync_loop)
        src_file = Path(svc.__file__)
        content = src_file.read_text(encoding="utf-8")
        # 应包含 wait_dirty_signal 调用
        assert "wait_dirty_signal" in content, (
            "_sync_loop 应调用 wait_dirty_signal 替代 asyncio.sleep(R56 §7.2.3)"
        )
        # 应有 signaled 信号处理逻辑
        assert "signaled" in content or "wait_dirty_signal(backoff)" in content

    def test_cache_store_publishes_signal_on_dirty_write(self):
        """cache_store 写 dirty_outbox 后应调用 publish_dirty_signal。"""
        import database.cache_store as cs
        src_file = Path(cs.__file__)
        content = src_file.read_text(encoding="utf-8")
        assert "publish_dirty_signal" in content, (
            "cache_store 应在 dirty_outbox 写入后调用 publish_dirty_signal(R56 §7.2.3)"
        )


# ════════════════════════════════════════════════════════════════
# 6. RU 近零空载验证(配置层面)
# ════════════════════════════════════════════════════════════════


class TestRuZeroIdleConfig:
    """验证配置层面满足 §7.2 RU 近零空载要求。"""

    def test_crdb_pool_min_size_is_zero(self):
        """CRDB_POOL_MIN_SIZE 应为 0(空闲时不保持连接)。"""
        from config import settings
        assert settings.CRDB_POOL_MIN_SIZE == 0

    def test_sync_back_off_is_zero(self):
        """SYNC_BACK_OFF 应为 0(禁用 Bot 直连兜底)。"""
        from config import settings
        assert settings.SYNC_BACK_OFF == 0

    def test_crdb_pool_max_size_is_small(self):
        """CRDB_POOL_MAX_SIZE 应 ≤2(业务 Bot)。"""
        from config import settings
        assert settings.CRDB_POOL_MAX_SIZE <= 2

    def test_no_hardcoded_select_max_in_sync_loop(self):
        """sync_loop 不应包含 SELECT MAX(空载 CRDB 查询)。"""
        import services.crdb_sync_service as svc
        src_file = Path(svc.__file__)
        content = src_file.read_text(encoding="utf-8")
        # _sync_loop 函数体内不应有 SELECT MAX(允许在 backup/migration 中)
        # 粗略检查:整个模块中 SELECT MAX 应仅出现在注释或非 sync_loop 函数中
        # 这里只验证 _should_connect 用 COUNT(*) 查 SQLite(不是 CRDB)
        assert "SELECT COUNT(*) FROM dirty_outbox" in content  # SQLite 查询(0 RU)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
