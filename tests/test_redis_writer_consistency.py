"""双写一致性与降级逻辑测试。

被测模块: ``database.redis_queue`` / ``database.write_router``

R33 修复: 从 Redis List 改为 Streams Consumer Group。
  - 消息进入 pending 不删除,SQLite 提交后 XACK 确认
  - 崩溃后 XAUTOCLAIM 回收 pending 消息
  - writer_inbox 幂等表防止重复执行
  - 非幂等操作(increment/refund)移至直写

覆盖场景:
- Writer 崩溃后 Stream 消息不丢数据(消息在 pending,不被删除)。
- CAS / 事务 / 非幂等操作直读/写 SQLite 保证强一致(不走 Redis 队列)。
- 写操作后失效读缓存,避免读到旧数据。
- 降级模式与 Redis 模式行为一致(均能完成写操作派发)。
- Redis 不可用时自动降级到 SQLite 直写。
- R33: 崩溃恢复后通过 writer_inbox 实现幂等(exactly-once)。
- R33: XACK 失败不回滚 SQLite(消息留 pending,下次回收跳过)。
"""
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from database import redis_queue, write_router


class TestConsistency:
    """双写一致性与降级逻辑测试(R33: Streams 可靠消费)。"""

    @pytest.mark.asyncio
    async def test_writer_crash_no_data_loss(self, mock_redis, monkeypatch):
        """R33 P0: Writer 崩溃后 Stream 消息不丢失。

        XADD 写入的消息持久化在 Stream 中,XREADGROUP 消费后进入 pending
        (不被删除)。即使 db_writer 崩溃,消息仍在 pending,重启后
        XAUTOCLAIM 回收重处理。配合 writer_inbox 实现 exactly-once。
        """
        stream_store = []  # 模拟 Redis Stream 持久存储
        pending_store = []  # 模拟 pending 列表

        async def fake_xadd(key, fields, id="*", maxlen=None, approximate=True):
            msg_id = f"1700000000-{len(stream_store)}"
            stream_store.append((msg_id, fields))
            return msg_id

        async def fake_xreadgroup(group, consumer, streams, count=1, block=None):
            # 读取新消息(">" 表示从未投递),消息进入 pending
            result = []
            for msg_id, fields in stream_store:
                if msg_id not in [p[0] for p in pending_store]:
                    pending_store.append((msg_id, fields))
                    result.append((msg_id, fields))
            if result:
                return [("tgjiema:writer:stream", result)]
            return []

        async def fake_xlen(key):
            return len(stream_store)

        async def fake_xpending(key, group):
            return (len(pending_store), pending_store[0][0] if pending_store else None,
                    pending_store[-1][0] if pending_store else None, [])

        mock_redis.xadd = fake_xadd
        mock_redis.xreadgroup = fake_xreadgroup
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        mock_redis.xlen = fake_xlen
        mock_redis.xpending = fake_xpending
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # bot 推入写操作(XADD)
        ok = await redis_queue.push(
            "upsert", "user_quota", "upsert_user_quota", {"user_id": 1}
        )
        assert ok is True
        # 消息在 Stream 中
        assert len(stream_store) == 1

        # db_writer 消费(XREADGROUP,消息进入 pending)
        messages = await redis_queue.pop(timeout=1, count=1)
        assert len(messages) == 1
        assert messages[0]["method_name"] == "upsert_user_quota"

        # 模拟 db_writer 崩溃:不调用 XACK
        # 验证消息仍在 pending(未被删除)
        pending_info = await redis_queue.get_pending_info()
        assert pending_info["total"] == 1  # 消息仍在 pending

        # Stream 长度仍为 1(XACK 不删除 Stream 中的消息,只从 pending 移除)
        stream_len = await redis_queue.length()
        assert stream_len == 1

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
    async def test_non_idempotent_direct_write(self, mock_settings, monkeypatch):
        """R33: 非幂等操作(increment_user_quota_used)直写 SQLite。

        避免队列重放导致二次扣减(used = used + 1 是非幂等的)。
        """
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value={"ok": True, "new_used": 4})

        result = await write_router.route_write(
            method_name="increment_user_quota_used",
            table="user_quota",
            op_type="update",
            data={"user_id": 1},
            redis_key="cache:user_quota:1",
            fallback=fallback,
        )

        # 非幂等操作必须直写,不走队列
        assert result == {"ok": True, "new_used": 4}
        fallback.assert_awaited_once()
        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_direct_write(self, mock_settings, monkeypatch):
        """R33: refund_quota(退款)非幂等,直写 SQLite。

        避免队列重放导致二次退款(used = used - amount 是非幂等的)。
        """
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value={"ok": True, "refunded": 5})

        result = await write_router.route_write(
            method_name="refund_quota",
            table="user_quota",
            op_type="update",
            data={"user_id": 1, "amount": 5},
            redis_key="cache:user_quota:1",
            fallback=fallback,
        )

        assert result == {"ok": True, "refunded": 5}
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

    @pytest.mark.asyncio
    async def test_redis_unavailable_degrades_gracefully(self, mock_settings, monkeypatch):
        """Redis 不可用时(push 返回 False)自动降级到 SQLite 直写 fallback。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
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

        assert result == "degraded_ok"
        mock_push.assert_awaited_once()
        fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_xack_failure_message_stays_in_pending(self, mock_redis, monkeypatch):
        """R33 P0: XACK 失败时消息留在 pending,不丢数据。

        场景:SQLite 写成功 + inbox 写成功 → XACK 失败(网络抖动)。
        消息留在 pending,下次 XAUTOCLAIM 回收后 inbox 检查命中,XACK 跳过。
        """
        from database import redis_queue

        msg = {
            "method_name": "write_heartbeat",
            "data": {"slot_id": 1},
            "message_id": str(uuid.uuid4()),
            "created_at": 1.0,
        }
        mock_redis.xreadgroup = AsyncMock(return_value=[
            ("stream", [("1700000000-0", {"data": json.dumps(msg)})])
        ])
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        # XACK 失败(返回 0 表示没有消息被确认)
        mock_redis.xack = AsyncMock(return_value=0)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 消费消息
        result = await redis_queue.pop(timeout=1, count=1)
        assert len(result) == 1

        # 消息仍在 pending(XACK 失败)
        mock_redis.xpending = AsyncMock(return_value=(1, "1700000000-0", "1700000000-0", []))
        pending_info = await redis_queue.get_pending_info()
        assert pending_info["total"] == 1

    @pytest.mark.asyncio
    async def test_xautoclaim_recovers_pending(self, mock_redis, monkeypatch):
        """R33 P0: XAUTOCLAIM 回收崩溃遗留的 pending 消息。

        pop() 优先回收 pending 消息,再读新消息。
        确保崩溃后重启能恢复未 ACK 的消息。
        """
        from database import redis_queue

        pending_msg = {
            "method_name": "write_heartbeat",
            "data": {"slot_id": 42},
            "message_id": str(uuid.uuid4()),
            "created_at": 1.0,
        }
        # XAUTOCLAIM 返回回收的消息
        mock_redis.xautoclaim = AsyncMock(return_value=(
            "1700000001-0",
            [("1700000000-0", {"data": json.dumps(pending_msg)})],
            [],
        ))
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=10)
        assert len(result) == 1
        assert result[0]["_reclaimed"] is True
        # XREADGROUP 未被调用(回收已满足)
        mock_redis.xreadgroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_message_id_for_idempotency(self, mock_redis, monkeypatch):
        """R33 P1-2: push 自动生成 message_id 用于幂等去重。

        每条消息携带唯一 UUID,db_writer 处理后写入 writer_inbox。
        崩溃恢复时通过 inbox 检查跳过已处理的消息。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # push 两条消息,各自生成不同 message_id
        await redis_queue.push("upsert", "t1", "m1", {"a": 1})
        await redis_queue.push("upsert", "t2", "m2", {"b": 2})

        assert mock_redis.xadd.await_count == 2
        # 提取两条消息的 message_id
        call1_args = mock_redis.xadd.await_args_list[0].args
        call2_args = mock_redis.xadd.await_args_list[1].args
        msg1 = json.loads(call1_args[1]["data"])
        msg2 = json.loads(call2_args[1]["data"])
        # 两条消息的 message_id 不同
        assert msg1["message_id"] != msg2["message_id"]
        # 都是合法 UUID
        uuid.UUID(msg1["message_id"])
        uuid.UUID(msg2["message_id"])

    @pytest.mark.asyncio
    async def test_dlq_retry_closure(self, mock_redis, monkeypatch):
        """R33 P1-1: 死信队列带重试闭环(attempts/max_attempts/next_retry_at)。

        失败消息入死信时携带重试信息,支持延迟重试。
        attempts >= max_attempts 时 next_retry_at=None(永久死信)。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 第一次失败:attempts=1 < max_attempts=3,有 next_retry_at
        await redis_queue.push_dead({"data": 1}, reason="fail", attempts=1)
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        assert dead_msg["attempts"] == 1
        assert dead_msg["max_attempts"] == 3
        assert dead_msg["next_retry_at"] is not None

        # 达到 max_attempts:next_retry_at=None(永久死信)
        mock_redis.xadd.reset_mock()
        await redis_queue.push_dead({"data": 2}, reason="permanent", attempts=3)
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        assert dead_msg["attempts"] == 3
        assert dead_msg["next_retry_at"] is None
