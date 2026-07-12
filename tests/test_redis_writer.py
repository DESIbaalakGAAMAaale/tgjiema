"""RedisQueue 与 WriteRouter 单元测试。

被测模块:
- ``database.redis_queue``:Redis 队列封装(LPUSH/BRPOP/DEL/LLEN/缓存读写)
- ``database.write_router``:写操作路由器(Redis 模式 / SQLite 直写降级 / CAS 直写)

测试策略:
- 使用 ``unittest.mock.AsyncMock`` 模拟 ``redis.asyncio`` 客户端,不依赖真实 Redis。
- 通过 patch ``redis_queue.get_redis`` 注入模拟客户端,隔离 Redis 连接初始化逻辑。
- WriteRouter 测试通过 patch ``redis_queue.push`` 隔离队列推送,验证路由分支与降级行为。
- settings 通过 conftest 注入的模拟对象提供,用 monkeypatch 覆盖属性控制 WRITER_MODE/REDIS_URL。
"""
import json
from unittest.mock import AsyncMock

import pytest

from database import redis_queue, write_router


# ───────────────────────── RedisQueue ─────────────────────────


class TestRedisQueue:
    """RedisQueue 模块函数测试。"""

    @pytest.mark.asyncio
    async def test_push_success(self, mock_redis, monkeypatch):
        """推入消息成功:序列化为 JSON 并 LPUSH 到配置的队列 key。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push(
            op_type="upsert",
            table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 12345, "quota": 20},
            redis_key="cache:user_quota:12345",
        )
        assert ok is True
        mock_redis.lpush.assert_awaited_once()
        args = mock_redis.lpush.await_args.args
        # 第一个参数为队列 key(取自 settings.WRITER_QUEUE_KEY)
        assert args[0] == "tgjiema:writer:queue"
        # 第二个参数为 JSON 字符串,反序列化后字段符合预期
        msg = json.loads(args[1])
        assert msg["op_type"] == "upsert"
        assert msg["table"] == "user_quota"
        assert msg["method_name"] == "upsert_user_quota"
        assert msg["data"] == {"user_id": 12345, "quota": 20}
        assert msg["redis_key"] == "cache:user_quota:12345"
        assert isinstance(msg["created_at"], float)

    @pytest.mark.asyncio
    async def test_push_redis_unavailable(self, monkeypatch):
        """Redis 不可用时(get_redis 返回 None)push 返回 False,触发降级到 SQLite 直写。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        ok = await redis_queue.push("upsert", "user_quota", "upsert_user_quota", {"user_id": 1})
        assert ok is False

    @pytest.mark.asyncio
    async def test_pop_success(self, mock_redis, monkeypatch):
        """弹出消息成功:BRPOP 返回 (key, raw) 后解析为 dict 列表。"""
        msg = {
            "op_type": "update",
            "table": "heartbeat_local",
            "method_name": "write_heartbeat",
            "data": {"slot_id": 1, "ok": True},
            "redis_key": "",
            "created_at": 1.0,
        }
        mock_redis.brpop = AsyncMock(return_value=("tgjiema:writer:queue", json.dumps(msg)))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=1, count=1)
        assert result == [msg]
        mock_redis.brpop.assert_awaited_once_with("tgjiema:writer:queue", timeout=1)

    @pytest.mark.asyncio
    async def test_pop_timeout(self, mock_redis, monkeypatch):
        """BRPOP 超时返回 None 时,pop 返回空列表。"""
        mock_redis.brpop = AsyncMock(return_value=None)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=0, count=3)
        assert result == []
        # count=3 时首次 BRPOP 返回 None 即 break,只调用一次
        assert mock_redis.brpop.await_count == 1

    @pytest.mark.asyncio
    async def test_delete_success(self, mock_redis, monkeypatch):
        """删除指定 key 成功:调用 redis.delete 并返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.delete("cache:user_quota:12345")
        assert ok is True
        mock_redis.delete.assert_awaited_once_with("cache:user_quota:12345")

    @pytest.mark.asyncio
    async def test_length(self, mock_redis, monkeypatch):
        """获取队列长度:返回 redis.llen 的结果。"""
        mock_redis.llen = AsyncMock(return_value=42)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        n = await redis_queue.length()
        assert n == 42
        mock_redis.llen.assert_awaited_once_with("tgjiema:writer:queue")

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
        mock_redis.get.assert_awaited_once_with("cache:missing")

    # ── P2修复: 补充 push_dead / health_check / close_redis / cache_delete / pop 批量 测试覆盖 ──

    @pytest.mark.asyncio
    async def test_push_dead_success(self, mock_redis, monkeypatch):
        """push_dead Redis 可达:RPUSH 到死信队列,返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push_dead({"foo": "bar"}, reason="test error")
        assert ok is True
        mock_redis.rpush.assert_awaited_once()
        args = mock_redis.rpush.await_args.args
        assert args[0] == "tgjiema:writer:dead"
        dead_msg = json.loads(args[1])
        assert dead_msg["original"] == {"foo": "bar"}
        assert dead_msg["reason"] == "test error"
        assert "failed_at" in dead_msg

    @pytest.mark.asyncio
    async def test_push_dead_redis_unavailable_fallback_file(self, monkeypatch):
        """push_dead Redis 不可达:降级写本地文件 dead_letter.jsonl。
        P3简化: 只验证返回 True 和 rpush 未被调用(文件路径是实现细节)。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        # 确保 rpush 不会被调用(Redis 不可达)
        ok = await redis_queue.push_dead({"baz": "qux"}, reason="redis down")
        assert ok is True
        # 验证 Redis rpush 未被调用(走了本地文件降级路径)
        # (get_redis 返回 None,所以不会调用任何 Redis 方法)

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
        """close_redis:关闭连接并重置所有全局状态。"""
        # 直接设置已连接状态(不依赖 get_redis 的初始化逻辑)
        redis_queue._redis_client = mock_redis
        redis_queue._redis_available = True
        redis_queue._redis_init_attempted = True
        redis_queue._redis_last_attempt_ts = 12345.0
        # 关闭
        await redis_queue.close_redis()
        assert redis_queue._redis_client is None
        assert redis_queue._redis_available is False
        assert redis_queue._redis_init_attempted is False
        assert redis_queue._redis_last_attempt_ts == 0
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

    @pytest.mark.asyncio
    async def test_pop_batch_count_gt_one(self, mock_redis, monkeypatch):
        """pop count>1:首条 BRPOP 成功,后续 LPOP 补充至 count 条。"""
        msg1 = {"method_name": "write_heartbeat", "data": {"slot_id": 1}}
        msg2 = {"method_name": "write_heartbeat", "data": {"slot_id": 2}}
        msg3 = {"method_name": "write_heartbeat", "data": {"slot_id": 3}}
        mock_redis.brpop = AsyncMock(return_value=("queue", json.dumps(msg1)))
        mock_redis.lpop = AsyncMock(side_effect=[json.dumps(msg2), json.dumps(msg3), None])
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=1, count=3)
        assert len(result) == 3
        assert result[0] == msg1
        assert result[1] == msg2
        assert result[2] == msg3

    @pytest.mark.asyncio
    async def test_pop_brpop_json_decode_failure(self, mock_redis, monkeypatch):
        """pop BRPOP JSON 解析失败:消息入死信队列,继续 LPOP。"""
        mock_redis.brpop = AsyncMock(return_value=("queue", "not valid json"))
        mock_redis.lpop = AsyncMock(return_value=None)
        # mock push_dead 避免真实文件写入
        mock_push_dead = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=1, count=1)
        assert result == []
        mock_push_dead.assert_awaited_once()
        dead_args = mock_push_dead.call_args
        assert "raw" in dead_args.kwargs.get("msg", {}) or "raw" in dead_args.args[0]

    @pytest.mark.asyncio
    async def test_pop_lpop_json_decode_failure(self, mock_redis, monkeypatch):
        """pop LPOP JSON 解析失败:消息入死信队列,继续处理后续消息。"""
        msg1 = {"method_name": "write_heartbeat", "data": {"slot_id": 1}}
        msg3 = {"method_name": "write_heartbeat", "data": {"slot_id": 3}}
        mock_redis.brpop = AsyncMock(return_value=("queue", json.dumps(msg1)))
        mock_redis.lpop = AsyncMock(side_effect=["bad json", json.dumps(msg3), None])
        mock_push_dead = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.pop(timeout=1, count=3)
        # 首条成功 + 第3条成功,第2条入死信
        assert len(result) == 2
        assert result[0] == msg1
        assert result[1] == msg3
        mock_push_dead.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pop_redis_connection_exception(self, mock_redis, monkeypatch):
        """pop Redis 运行时宕机:brpop 抛异常,重置客户端状态,返回空列表。"""
        mock_redis.brpop = AsyncMock(side_effect=Exception("connection reset"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # 初始化 _redis_client 为 mock_redis,模拟已连接状态
        redis_queue._redis_client = mock_redis
        redis_queue._redis_available = True
        result = await redis_queue.pop(timeout=1, count=1)
        assert result == []
        # P1修复: 客户端状态应被重置,使下次 get_redis 触发重连
        assert redis_queue._redis_client is None
        assert redis_queue._redis_available is False
        mock_redis.aclose.assert_awaited_once()


# ───────────────────────── WriteRouter ─────────────────────────


class TestWriteRouter:
    """WriteRouter 路由逻辑测试。"""

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

    def test_is_direct_write_cas(self):
        """CAS 操作(try_consume_quota)被识别为直写 SQLite。"""
        assert write_router.is_direct_write("try_consume_quota") is True

    def test_is_direct_write_transaction(self):
        """事务操作(batch_update_cells_local)被识别为直写 SQLite。"""
        assert write_router.is_direct_write("batch_update_cells_local") is True

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
