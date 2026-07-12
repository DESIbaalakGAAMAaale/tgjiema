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
