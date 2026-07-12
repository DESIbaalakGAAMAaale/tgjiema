"""RedisQueue 与 WriteRouter 单元测试。

被测模块:
- ``database.redis_queue``:Redis Streams 封装(XADD/XREADGROUP/XACK/XAUTOCLAIM/缓存读写)
- ``database.write_router``:写操作路由器(Redis 模式 / SQLite 直写降级 / CAS 直写)

R33 修复: 从 Redis List (LPUSH/BRPOP/LPOP) 改为 Streams Consumer Group。
  - push: LPUSH → XADD(带 message_id 幂等键)
  - pop: BRPOP → XREADGROUP(消息进入 pending 不删除)+ XAUTOCLAIM(回收)
  - ack: 新增,XACK 确认
  - ensure_consumer_group: 新增,XGROUP CREATE(幂等)
  - push_dead: RPUSH → XADD 到死信 Stream(带 attempts/max_attempts)
  - length/get_pending_info/get_dlq_length: 监控适配 Streams

测试策略:
- 使用 ``unittest.mock.AsyncMock`` 模拟 ``redis.asyncio`` 客户端,不依赖真实 Redis。
- 通过 patch ``redis_queue.get_redis`` 注入模拟客户端,隔离 Redis 连接初始化逻辑。
- WriteRouter 测试通过 patch ``redis_queue.push`` 隔离队列推送,验证路由分支与降级行为。
- settings 通过 conftest 注入的模拟对象提供,用 monkeypatch 覆盖属性控制 WRITER_MODE/REDIS_URL。
"""
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from database import redis_queue, write_router


# ───────────────────────── RedisQueue ─────────────────────────


class TestRedisQueue:
    """RedisQueue 模块函数测试(R33: Streams API)。"""

    @pytest.mark.asyncio
    async def test_push_success(self, mock_redis, monkeypatch):
        """推入消息成功:序列化为 JSON 并 XADD 到配置的 Stream key。
        R33: 消息携带 message_id(UUID)用于幂等去重。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push(
            op_type="upsert",
            table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 12345, "quota": 20},
            redis_key="cache:user_quota:12345",
        )
        assert ok is True
        mock_redis.xadd.assert_awaited_once()
        args, kwargs = mock_redis.xadd.await_args
        # 第一个参数为 Stream key
        assert args[0] == "tgjiema:writer:stream"
        # 第二个参数为 fields dict,{"data": json_str}
        fields = args[1]
        assert "data" in fields
        msg = json.loads(fields["data"])
        assert msg["op_type"] == "upsert"
        assert msg["table"] == "user_quota"
        assert msg["method_name"] == "upsert_user_quota"
        assert msg["data"] == {"user_id": 12345, "quota": 20}
        assert msg["redis_key"] == "cache:user_quota:12345"
        assert isinstance(msg["created_at"], float)
        # R33: message_id 必须是 UUID 格式
        assert "message_id" in msg
        uuid.UUID(msg["message_id"])  # 验证是合法 UUID

    @pytest.mark.asyncio
    async def test_push_with_explicit_message_id(self, mock_redis, monkeypatch):
        """push 支持显式传入 message_id(用于幂等重放)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push(
            op_type="insert", table="t", method_name="m", data={},
            message_id="fixed-id-12345",
        )
        assert ok is True
        args = mock_redis.xadd.await_args.args
        msg = json.loads(args[1]["data"])
        assert msg["message_id"] == "fixed-id-12345"

    @pytest.mark.asyncio
    async def test_push_redis_unavailable(self, monkeypatch):
        """Redis 不可用时(get_redis 返回 None)push 返回 False,触发降级到 SQLite 直写。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        ok = await redis_queue.push("upsert", "user_quota", "upsert_user_quota", {"user_id": 1})
        assert ok is False

    @pytest.mark.asyncio
    async def test_push_xadd_exception_returns_false(self, mock_redis, monkeypatch):
        """XADD 抛异常:push 捕获并返回 False(降级到 SQLite)。"""
        mock_redis.xadd = AsyncMock(side_effect=Exception("redis write error"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push("upsert", "t", "m", {})
        assert ok is False

    @pytest.mark.asyncio
    async def test_pop_success(self, mock_redis, monkeypatch):
        """弹出消息成功:XREADGROUP 返回消息后解析为 dict 列表,附带 _stream_id。
        R33: 消息进入 pending 不删除,SQLite 提交后需 XACK 确认。
        """
        msg = {
            "op_type": "update",
            "table": "heartbeat_local",
            "method_name": "write_heartbeat",
            "data": {"slot_id": 1, "ok": True},
            "redis_key": "",
            "message_id": "uuid-test-1",
            "created_at": 1.0,
        }
        # XREADGROUP 返回格式: [(stream_name, [(msg_id, {fields}), ...])]
        mock_redis.xreadgroup = AsyncMock(return_value=[
            ("tgjiema:writer:stream", [("1700000000-0", {"data": json.dumps(msg)})])
        ])
        # XAUTOCLAIM 返回空(无 pending 可回收)
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=1)
        assert len(result) == 1
        # _stream_id 附加供 XACK 使用
        assert result[0]["_stream_id"] == "1700000000-0"
        assert result[0]["method_name"] == "write_heartbeat"
        assert result[0]["message_id"] == "uuid-test-1"
        # XREADGROUP 被调用,使用 ">" 读取新消息
        mock_redis.xreadgroup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pop_timeout_empty(self, mock_redis, monkeypatch):
        """XREADGROUP 超时返回空列表时,pop 返回空列表。"""
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=0, count=3)
        assert result == []

    @pytest.mark.asyncio
    async def test_pop_priority_reclaim_pending(self, mock_redis, monkeypatch):
        """pop 优先回收 pending 消息(XAUTOCLAIM),再读新消息。
        R33: 崩溃恢复 — pending >30s 的消息被 XAUTOCLAIM 回收重处理。
        """
        pending_msg = {
            "method_name": "write_heartbeat",
            "data": {"slot_id": 99},
            "message_id": "reclaimed-1",
            "created_at": 1.0,
        }
        # XAUTOCLAIM 返回回收的消息
        mock_redis.xautoclaim = AsyncMock(return_value=(
            "1700000001-0",
            [("1700000000-0", {"data": json.dumps(pending_msg)})],
            [],
        ))
        # XREADGROUP 不应被调用(回收的消息已满)
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=1)
        assert len(result) == 1
        assert result[0]["_stream_id"] == "1700000000-0"
        assert result[0]["_reclaimed"] is True
        assert result[0]["message_id"] == "reclaimed-1"
        # XREADGROUP 未被调用(回收消息已满)
        mock_redis.xreadgroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pop_json_decode_failure_to_dead(self, mock_redis, monkeypatch):
        """XREADGROUP 消息 JSON 解析失败:消息入死信队列 + XACK(移出 pending)。"""
        mock_redis.xreadgroup = AsyncMock(return_value=[
            ("stream", [("bad-id", {"data": "not valid json"})])
        ])
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        mock_push_dead = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=1)
        assert result == []
        # 消息入死信
        mock_push_dead.assert_awaited_once()
        # XACK 移出 pending(损坏消息不应留在 pending 重试)
        mock_redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pop_reclaim_json_decode_failure(self, mock_redis, monkeypatch):
        """XAUTOCLAIM 消息 JSON 解析失败:入死信 + XACK。"""
        mock_redis.xautoclaim = AsyncMock(return_value=(
            "0-0",
            [("bad-reclaim-id", {"data": "bad json"})],
            [],
        ))
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        mock_push_dead = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=1)
        assert result == []
        mock_push_dead.assert_awaited_once()
        mock_redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pop_redis_connection_exception(self, mock_redis, monkeypatch):
        """pop Redis 运行时宕机:xreadgroup 抛异常,重置客户端状态,返回空列表。"""
        mock_redis.xreadgroup = AsyncMock(side_effect=Exception("connection reset"))
        mock_redis.xautoclaim = AsyncMock(side_effect=Exception("connection reset"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # 初始化为已连接状态
        redis_queue._redis_client = mock_redis
        redis_queue._redis_available = True

        result = await redis_queue.pop(timeout=1, count=1)
        assert result == []
        # P1修复: 客户端状态应被重置,使下次 get_redis 触发重连
        assert redis_queue._redis_client is None
        assert redis_queue._redis_available is False
        # R33: consumer_group_ensured 也应重置
        assert redis_queue._consumer_group_ensured is False
        mock_redis.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pop_batch_multiple_messages(self, mock_redis, monkeypatch):
        """pop 一次读取多条消息:XREADGROUP count 参数生效。"""
        msg1 = {"method_name": "write_heartbeat", "data": {"slot_id": 1}, "message_id": "1"}
        msg2 = {"method_name": "write_heartbeat", "data": {"slot_id": 2}, "message_id": "2"}
        mock_redis.xreadgroup = AsyncMock(return_value=[
            ("stream", [
                ("id-1", {"data": json.dumps(msg1)}),
                ("id-2", {"data": json.dumps(msg2)}),
            ])
        ])
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.pop(timeout=1, count=10)
        assert len(result) == 2
        assert result[0]["_stream_id"] == "id-1"
        assert result[1]["_stream_id"] == "id-2"

    # ── R33: 新增 ack / ensure_consumer_group / get_pending_info / get_dlq_length 测试 ──

    @pytest.mark.asyncio
    async def test_ack_success(self, mock_redis, monkeypatch):
        """ack 成功:XACK 确认消息,从 pending 列表移除。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        n = await redis_queue.ack(["1700000000-0", "1700000001-0"])
        assert n == 1
        mock_redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ack_empty_list(self, mock_redis, monkeypatch):
        """ack 空列表:直接返回 0,不调用 XACK。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        n = await redis_queue.ack([])
        assert n == 0
        mock_redis.xack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_redis_unavailable(self, monkeypatch):
        """ack Redis 不可达:返回 0。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        n = await redis_queue.ack(["id-1"])
        assert n == 0

    @pytest.mark.asyncio
    async def test_ensure_consumer_group_creates(self, mock_redis, monkeypatch):
        """ensure_consumer_group:首次创建 Consumer Group,返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.ensure_consumer_group()
        assert ok is True
        mock_redis.xgroup_create.assert_awaited_once()
        # 第二次调用应跳过(已创建标志)
        ok2 = await redis_queue.ensure_consumer_group()
        assert ok2 is True
        # xgroup_create 只被调用一次
        assert mock_redis.xgroup_create.await_count == 1

    @pytest.mark.asyncio
    async def test_ensure_consumer_group_busygroup_ignored(self, mock_redis, monkeypatch):
        """ensure_consumer_group:BUSYGROUP(Group 已存在)被忽略,返回 True。"""
        mock_redis.xgroup_create = AsyncMock(
            side_effect=Exception("BUSYGROUP Consumer Group name already exists")
        )
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.ensure_consumer_group()
        assert ok is True
        assert redis_queue._consumer_group_ensured is True

    @pytest.mark.asyncio
    async def test_ensure_consumer_group_other_error(self, mock_redis, monkeypatch):
        """ensure_consumer_group:非 BUSYGROUP 错误返回 False。"""
        mock_redis.xgroup_create = AsyncMock(side_effect=Exception("network error"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.ensure_consumer_group()
        assert ok is False
        assert redis_queue._consumer_group_ensured is False

    @pytest.mark.asyncio
    async def test_ensure_consumer_group_redis_unavailable(self, monkeypatch):
        """ensure_consumer_group Redis 不可达:返回 False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        ok = await redis_queue.ensure_consumer_group()
        assert ok is False

    @pytest.mark.asyncio
    async def test_get_pending_info(self, mock_redis, monkeypatch):
        """get_pending_info:返回 pending 消息总数和边界 ID。"""
        mock_redis.xpending = AsyncMock(return_value=(5, "1700000000-0", "1700000004-0", []))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        info = await redis_queue.get_pending_info()
        assert info["total"] == 5
        assert info["oldest_id"] == "1700000000-0"
        assert info["newest_id"] == "1700000004-0"

    @pytest.mark.asyncio
    async def test_get_pending_info_empty(self, mock_redis, monkeypatch):
        """get_pending_info:无 pending 时返回 total=0。"""
        mock_redis.xpending = AsyncMock(return_value=(0, None, None, []))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        info = await redis_queue.get_pending_info()
        assert info["total"] == 0

    @pytest.mark.asyncio
    async def test_get_pending_info_redis_unavailable(self, monkeypatch):
        """get_pending_info Redis 不可达:返回空 dict。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        info = await redis_queue.get_pending_info()
        assert info == {}

    @pytest.mark.asyncio
    async def test_get_dlq_length(self, mock_redis, monkeypatch):
        """get_dlq_length:返回死信队列消息数。"""
        mock_redis.xlen = AsyncMock(return_value=3)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        n = await redis_queue.get_dlq_length()
        assert n == 3

    @pytest.mark.asyncio
    async def test_get_dlq_length_redis_unavailable(self, monkeypatch):
        """get_dlq_length Redis 不可达:返回 -1。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        n = await redis_queue.get_dlq_length()
        assert n == -1

    @pytest.mark.asyncio
    async def test_length(self, mock_redis, monkeypatch):
        """length:返回 Stream 长度(XLEN)。"""
        mock_redis.xlen = AsyncMock(return_value=42)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        n = await redis_queue.length()
        assert n == 42

    @pytest.mark.asyncio
    async def test_delete_success(self, mock_redis, monkeypatch):
        """删除指定 key 成功:调用 redis.delete 并返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.delete("cache:user_quota:12345")
        assert ok is True
        mock_redis.delete.assert_awaited_once_with("cache:user_quota:12345")

    @pytest.mark.asyncio
    async def test_delete_empty_key(self, mock_redis, monkeypatch):
        """delete 空 key:直接返回 False,不调用 redis.delete。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.delete("")
        assert ok is False
        mock_redis.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_get_set(self, mock_redis, monkeypatch):
        """缓存读写:cache_set 写入成功返回 True,cache_get 读取返回对应值。"""
        mock_redis.get = AsyncMock(return_value="v1")
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        ok = await redis_queue.cache_set("cache:k", "v1", ttl=5)
        assert ok is True
        mock_redis.setex.assert_awaited_once_with("cache:k", 5, "v1")

        val = await redis_queue.cache_get("cache:k")
        assert val == "v1"
        mock_redis.get.assert_awaited_with("cache:k")

    @pytest.mark.asyncio
    async def test_cache_get_miss(self, mock_redis, monkeypatch):
        """缓存未命中:redis.get 返回 None 时 cache_get 返回 None。"""
        mock_redis.get = AsyncMock(return_value=None)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        val = await redis_queue.cache_get("cache:missing")
        assert val is None

    # ── push_dead 测试(R33: 带 attempts/max_attempts/next_retry_at 的重试闭环) ──

    @pytest.mark.asyncio
    async def test_push_dead_success(self, mock_redis, monkeypatch):
        """push_dead Redis 可达:XADD 到死信 Stream,返回 True。
        R33: 消息携带 attempts/max_attempts/next_retry_at 支持延迟重试。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push_dead({"foo": "bar"}, reason="test error", attempts=1)
        assert ok is True
        mock_redis.xadd.assert_awaited_once()
        args = mock_redis.xadd.await_args.args
        assert args[0] == "tgjiema:writer:dead"
        dead_msg = json.loads(args[1]["data"])
        assert dead_msg["original"] == {"foo": "bar"}
        assert dead_msg["reason"] == "test error"
        assert "failed_at" in dead_msg
        # R33: 重试字段
        assert dead_msg["attempts"] == 1
        assert dead_msg["max_attempts"] == 3
        assert dead_msg["next_retry_at"] is not None  # attempts < max_attempts,有下次重试时间

    @pytest.mark.asyncio
    async def test_push_dead_max_attempts_no_retry(self, mock_redis, monkeypatch):
        """push_dead attempts >= max_attempts:next_retry_at 为 None(永久死信)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push_dead({"foo": "bar"}, reason="permanent", attempts=3)
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        assert dead_msg["attempts"] == 3
        assert dead_msg["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_push_dead_redis_unavailable_fallback_file(self, monkeypatch, tmp_path):
        """push_dead Redis 不可达:降级写本地文件 dead_letter.jsonl。
        R33: 确保数据落盘(fsync),避免消息永久丢失。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        # 重定向模块 __file__ 到 tmp_path/subdir/fake.py,
        # 使 dead_file 解析为 tmp_path/data/dead_letter.jsonl(避免污染项目)
        fake_subdir = tmp_path / "subdir"
        fake_subdir.mkdir()
        monkeypatch.setattr(redis_queue, "__file__", str(fake_subdir / "fake.py"))
        ok = await redis_queue.push_dead({"baz": "qux"}, reason="redis down")
        assert ok is True
        # 验证文件已创建且包含死信消息
        dead_file = tmp_path / "data" / "dead_letter.jsonl"
        assert dead_file.exists()

    @pytest.mark.asyncio
    async def test_push_dead_xadd_exception_fallback_file(self, mock_redis, monkeypatch, tmp_path):
        """push_dead XADD 抛异常:降级写本地文件,返回 True。"""
        mock_redis.xadd = AsyncMock(side_effect=Exception("redis write error"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        fake_subdir = tmp_path / "subdir"
        fake_subdir.mkdir()
        monkeypatch.setattr(redis_queue, "__file__", str(fake_subdir / "fake.py"))
        ok = await redis_queue.push_dead({"x": 1}, reason="xadd failed")
        assert ok is True
        dead_file = tmp_path / "data" / "dead_letter.jsonl"
        assert dead_file.exists()

    # ── health_check / close_redis / cache_delete ──

    @pytest.mark.asyncio
    async def test_health_check_ok(self, mock_redis, monkeypatch):
        """health_check Redis 可达:ping 成功返回 True。"""
        mock_redis.ping = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.health_check()
        assert ok is True
        mock_redis.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_health_check_fail(self, monkeypatch):
        """health_check Redis 不可达:get_redis 返回 None,返回 False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        ok = await redis_queue.health_check()
        assert ok is False

    @pytest.mark.asyncio
    async def test_health_check_ping_exception(self, mock_redis, monkeypatch):
        """health_check ping 抛异常:捕获异常返回 False。"""
        mock_redis.ping = AsyncMock(side_effect=Exception("network error"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.health_check()
        assert ok is False

    @pytest.mark.asyncio
    async def test_close_redis_resets_state(self, mock_redis, monkeypatch):
        """close_redis:关闭连接并重置所有全局状态。
        R33: 也重置 _consumer_group_ensured。
        """
        redis_queue._redis_client = mock_redis
        redis_queue._redis_available = True
        redis_queue._redis_init_attempted = True
        redis_queue._redis_last_attempt_ts = 12345.0
        redis_queue._consumer_group_ensured = True
        await redis_queue.close_redis()
        assert redis_queue._redis_client is None
        assert redis_queue._redis_available is False
        assert redis_queue._redis_init_attempted is False
        assert redis_queue._redis_last_attempt_ts == 0
        assert redis_queue._consumer_group_ensured is False
        mock_redis.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_delete_success(self, mock_redis, monkeypatch):
        """cache_delete:复用 delete() 逻辑,删除 key 成功返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.cache_delete("cache:user_quota:1")
        assert ok is True
        mock_redis.delete.assert_awaited_once_with("cache:user_quota:1")

    @pytest.mark.asyncio
    async def test_cache_delete_empty_key(self, mock_redis, monkeypatch):
        """cache_delete:空 key 时直接返回 False,不调用 redis.delete。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.cache_delete("")
        assert ok is False
        mock_redis.delete.assert_not_awaited()


# ───────────────────────── WriteRouter ─────────────────────────


class TestWriteRouter:
    """WriteRouter 路由逻辑测试。

    R33: 非幂等操作(increment_user_quota_used/refund_quota)也移至直写。
    """

    def test_should_use_redis_enabled(self, mock_settings, monkeypatch):
        """WRITER_MODE=redis 且 REDIS_URL 非空时返回 True。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        assert write_router.should_use_redis() is True

    def test_should_use_redis_disabled(self, mock_settings, monkeypatch):
        """WRITER_MODE=sqlite 时返回 False(降级模式)。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "sqlite")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        assert write_router.should_use_redis() is False

    def test_should_use_redis_no_url(self, mock_settings, monkeypatch):
        """REDIS_URL 为空时返回 False。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "")
        assert write_router.should_use_redis() is False

    def test_is_direct_write_cas(self):
        """CAS 操作(try_consume_quota)被识别为直写 SQLite。"""
        assert write_router.is_direct_write("try_consume_quota") is True

    def test_is_direct_write_transaction(self):
        """事务操作(batch_update_cells_local)被识别为直写 SQLite。"""
        assert write_router.is_direct_write("batch_update_cells_local") is True

    def test_is_direct_write_non_idempotent(self):
        """R33: 非幂等操作(increment_user_quota_used/refund_quota)被识别为直写。"""
        assert write_router.is_direct_write("increment_user_quota_used") is True
        assert write_router.is_direct_write("refund_quota") is True

    def test_is_not_direct_write_normal(self):
        """普通写操作(upsert_user_quota)不是直写。"""
        assert write_router.is_direct_write("upsert_user_quota") is False

    @pytest.mark.asyncio
    async def test_route_write_redis_mode(self, mock_settings, monkeypatch):
        """Redis 模式:普通写操作推入队列,返回 True,不调用 fallback。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value="should_not_be_called")

        result = await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="insert",
            data={"slot_id": 1, "ok": True},
            redis_key="",
            fallback=fallback,
        )

        assert result is True
        mock_push.assert_awaited_once_with(
            op_type="insert",
            table="heartbeat_local",
            method_name="write_heartbeat",
            data={"slot_id": 1, "ok": True},
            redis_key="",
        )
        fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_route_write_sqlite_mode(self, mock_settings, monkeypatch):
        """SQLite 模式:直接调用 fallback,不推入 Redis 队列。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "sqlite")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value="sqlite_ok")

        result = await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="insert",
            data={"slot_id": 1},
            redis_key="",
            fallback=fallback,
        )

        assert result == "sqlite_ok"
        fallback.assert_awaited_once()
        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_route_write_redis_push_fail_fallback(self, mock_settings, monkeypatch):
        """Redis 推入失败(push 返回 False)时降级到 fallback。"""
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
    async def test_route_write_cas_direct(self, mock_settings, monkeypatch):
        """CAS 操作(try_consume_quota)直写 SQLite,不走 Redis 队列。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value="cas_ok")

        result = await write_router.route_write(
            method_name="try_consume_quota",
            table="user_quota",
            op_type="update",
            data={"user_id": 1, "is_external": False},
            redis_key="cache:user_quota:1",
            fallback=fallback,
        )

        assert result == "cas_ok"
        fallback.assert_awaited_once()
        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_route_write_non_idempotent_direct(self, mock_settings, monkeypatch):
        """R33: 非幂等操作(increment_user_quota_used)直写 SQLite,不走队列。
        避免重放导致二次扣减。
        """
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        fallback = AsyncMock(return_value="increment_ok")

        result = await write_router.route_write(
            method_name="increment_user_quota_used",
            table="user_quota",
            op_type="update",
            data={"user_id": 1},
            redis_key="cache:user_quota:1",
            fallback=fallback,
        )

        assert result == "increment_ok"
        fallback.assert_awaited_once()
        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalidate_cache_non_empty(self, monkeypatch):
        """invalidate_cache 非空 key:调用 cache_delete。"""
        mock_cache_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "cache_delete", mock_cache_delete)
        await write_router.invalidate_cache("cache:user_quota:12345")
        mock_cache_delete.assert_awaited_once_with("cache:user_quota:12345")

    @pytest.mark.asyncio
    async def test_invalidate_cache_empty(self, monkeypatch):
        """invalidate_cache 空 key:不调用 cache_delete。"""
        mock_cache_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "cache_delete", mock_cache_delete)
        await write_router.invalidate_cache("")
        mock_cache_delete.assert_not_awaited()
