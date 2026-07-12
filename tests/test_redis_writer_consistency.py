"""双写一致性与降级逻辑测试。

被测模块: ``database.redis_queue`` / ``database.write_router``

覆盖场景:
- Writer 崩溃后 Redis 队列不丢数据(消息在 Writer 消费前仍保留)。
- CAS 操作直读/写 SQLite 保证强一致(不走 Redis 队列)。
- 写操作后失效读缓存,避免读到旧数据。
- 降级模式与 Redis 模式行为一致(均能完成写操作派发)。
- Redis 不可用时自动降级到 SQLite 直写。
"""
import json
from unittest.mock import AsyncMock

import pytest

from database import redis_queue, write_router


class TestConsistency:
    """双写一致性与降级逻辑测试。"""

    @pytest.mark.asyncio
    async def test_writer_crash_no_data_loss(self, mock_redis, monkeypatch):
        """Writer 崩溃后 Redis 队列不丢数据:push 成功的消息在 Writer 消费前仍保留在队列。"""
        queue_store = []  # 模拟 Redis List 持久存储(LPUSH 写入、BRPOP 消费)

        async def fake_lpush(key, val):
            queue_store.append(val)
            return len(queue_store)

        async def fake_brpop(key, timeout=0):
            # 模拟 Writer 崩溃:永不消费
            return None

        mock_redis.lpush = fake_lpush
        mock_redis.brpop = fake_brpop
        # llen 反映队列真实长度(消息仍在队列)
        mock_redis.llen = AsyncMock(return_value=1)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # bot 推入写操作
        ok = await redis_queue.push(
            "upsert", "user_quota", "upsert_user_quota", {"user_id": 1}
        )
        assert ok is True
        # 模拟 Writer 崩溃:不调用 pop
        # 验证消息仍在队列中(未被消费)
        assert len(queue_store) == 1
        msg = json.loads(queue_store[0])
        assert msg["method_name"] == "upsert_user_quota"

        # 二次验证:length 仍为 1,证明队列未丢数据
        n = await redis_queue.length()
        assert n == 1

    @pytest.mark.asyncio
    async def test_cas_direct_read_sqlite(self, mock_settings, monkeypatch):
        """CAS 操作(try_consume_quota)直读/写 SQLite,不走 Redis 队列(保证强一致)。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value={"ok": True, "rowcount": 1})

        result = await write_router.route_write(
            method_name="try_consume_quota",
            table="user_quota",
            op_type="update",
            data={"user_id": 1, "is_external": False},
            redis_key="cache:user_quota:1",
            fallback=fallback,
        )

        # CAS 必须直写 SQLite 并立即返回结果,不走 Redis 异步队列
        assert result == {"ok": True, "rowcount": 1}
        fallback.assert_awaited_once()
        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_read_cache_invalidation(self, monkeypatch):
        """写操作后失效读缓存:invalidate_cache 调用 cache_delete 清除旧数据。"""
        mock_cache_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "cache_delete", mock_cache_delete)

        # 非空 redis_key:触发失效
        await write_router.invalidate_cache("cache:user_quota:12345")
        mock_cache_delete.assert_awaited_once_with("cache:user_quota:12345")

        # 空 redis_key:不触发失效(避免误删)
        mock_cache_delete.reset_mock()
        await write_router.invalidate_cache("")
        mock_cache_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_mode_consistent(self, mock_settings, monkeypatch):
        """降级模式与 Redis 模式行为一致:两种模式均能完成写操作派发。"""
        # ── Redis 模式:推入队列(返回 True 表示已派发,稍后落盘)──
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        redis_result = await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="insert",
            data={"slot_id": 1},
            redis_key="",
            fallback=AsyncMock(return_value="FB"),
        )
        assert redis_result is True
        assert mock_push.await_count == 1

        # ── 降级模式:直接 fallback(返回结果表示已立即落盘)──
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "sqlite")
        mock_push2 = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push2)
        fallback_fn = AsyncMock(return_value="FB")
        sqlite_result = await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="insert",
            data={"slot_id": 1},
            redis_key="",
            fallback=fallback_fn,
        )
        assert sqlite_result == "FB"
        fallback_fn.assert_awaited_once()
        mock_push2.assert_not_awaited()

        # 两种模式均完成了写操作派发(Redis 模式入队 / 降级模式直接写),行为一致

    @pytest.mark.asyncio
    async def test_redis_unavailable_degrades_gracefully(self, mock_settings, monkeypatch):
        """Redis 不可用时(push 返回 False)自动降级到 SQLite 直写 fallback。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        # 模拟 Redis 不可用:push 返回 False
        mock_push = AsyncMock(return_value=False)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value="degraded_ok")

        result = await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="insert",
            data={"slot_id": 1},
            redis_key="",
            fallback=fallback,
        )

        # Redis 推入失败 → 自动降级到 fallback,返回其结果
        assert result == "degraded_ok"
        mock_push.assert_awaited_once()
        fallback.assert_awaited_once()
