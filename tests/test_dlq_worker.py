"""DLQ Worker 单元测试。

被测模块: ``database.dlq_worker`` — 死信队列重试闭环消费者。

R34 P1-1: 验证 DLQ Worker 能正确扫描死信 Stream,
将到期的可重试消息 XADD 回主 Stream + XDEL 从死信删除,
永久失败消息保留等待人工审核。

测试策略:
- 使用 ``unittest.mock.AsyncMock`` 模拟 ``redis_queue`` 模块函数
  (get_dead_messages / delete_dead_message / push / health_check),
  不依赖真实 Redis。
- 通过 ``monkeypatch.setattr`` 注入模拟函数,隔离 Redis 连接初始化逻辑。
- ``compute_backoff_delay`` 是纯函数,直接断言计算结果。
"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from database import dlq_worker, redis_queue


def _make_dead_msg(
    *,
    attempts: int = 0,
    max_attempts: int = 3,
    next_retry_at: float = None,
    message_id: str = "uuid-test-1",
    reason: str = "test error",
    original: dict = None,
) -> dict:
    """构造死信消息字典(格式与 redis_queue.push_dead 输出一致)。"""
    if next_retry_at is None:
        # 默认设为过去时间(可重试)
        next_retry_at = time.time() - 10
    if original is None:
        original = {
            "op_type": "upsert",
            "table": "user_quota",
            "method_name": "upsert_user_quota",
            "data": {"user_id": 12345, "quota": 20},
            "redis_key": "cache:user_quota:12345",
            "message_id": message_id,
            "created_at": 1000000.0,
        }
    return {
        "original": original,
        "reason": reason,
        "message_id": message_id,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "failed_at": 1000000.0,
        "next_retry_at": next_retry_at,
    }


# ───────────────────────── DLQWorker ─────────────────────────


class TestDLQWorker:
    """DLQWorker 类测试。"""

    @pytest.mark.asyncio
    async def test_retry_message(self, mock_redis, monkeypatch):
        """可重试消息被 XADD 回主 Stream + XDEL 从死信删除。

        条件: next_retry_at <= now 且 attempts < max_attempts。
        """
        worker = dlq_worker.DLQWorker()
        msg_id = "1700000000-0"
        dead_msg = _make_dead_msg(
            attempts=0,
            max_attempts=3,
            next_retry_at=time.time() - 10,  # 已到期
        )

        # Mock redis_queue 函数
        monkeypatch.setattr(
            redis_queue,
            "get_dead_messages",
            AsyncMock(return_value=[(msg_id, dead_msg)]),
        )
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        mock_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "delete_dead_message", mock_delete)

        retried = await worker._process_dead_messages()

        # 验证重试成功
        assert retried == 1
        assert worker.retried_count == 1
        assert worker.processed_count == 1
        assert worker.permanent_fail_count == 0

        # 验证 XADD 回主 Stream(保留原 message_id)
        mock_push.assert_awaited_once()
        _, kwargs = mock_push.await_args
        assert kwargs["op_type"] == "upsert"
        assert kwargs["table"] == "user_quota"
        assert kwargs["method_name"] == "upsert_user_quota"
        assert kwargs["data"] == {"user_id": 12345, "quota": 20}
        assert kwargs["message_id"] == "uuid-test-1"  # 保留原 message_id

        # 验证 XDEL 从死信删除
        mock_delete.assert_awaited_once_with(msg_id)

    @pytest.mark.asyncio
    async def test_permanent_failure(self, monkeypatch):
        """attempts >= max_attempts 的消息不重试,保留在死信 Stream。

        条件: attempts >= max_attempts → 永久死信。
        """
        worker = dlq_worker.DLQWorker()
        msg_id = "1700000001-0"
        dead_msg = _make_dead_msg(
            attempts=3,           # 已达最大重试次数
            max_attempts=3,
            next_retry_at=None,   # 永久死信(push_dead 在 attempts >= max 时设为 None)
        )

        monkeypatch.setattr(
            redis_queue,
            "get_dead_messages",
            AsyncMock(return_value=[(msg_id, dead_msg)]),
        )
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        mock_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "delete_dead_message", mock_delete)

        retried = await worker._process_dead_messages()

        # 验证未重试
        assert retried == 0
        assert worker.retried_count == 0
        assert worker.permanent_fail_count == 1
        assert worker.processed_count == 1

        # 不应 XADD 回主 Stream
        mock_push.assert_not_awaited()
        # 不应 XDEL(保留在死信 Stream 等待人工审核)
        mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_yet_time(self, monkeypatch):
        """next_retry_at > now 的消息不重试,等待下次扫描。

        条件: next_retry_at 在未来 → 未到期,跳过。
        """
        worker = dlq_worker.DLQWorker()
        msg_id = "1700000002-0"
        dead_msg = _make_dead_msg(
            attempts=1,
            max_attempts=3,
            next_retry_at=time.time() + 3600,  # 1 小时后到期
        )

        monkeypatch.setattr(
            redis_queue,
            "get_dead_messages",
            AsyncMock(return_value=[(msg_id, dead_msg)]),
        )
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        mock_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "delete_dead_message", mock_delete)

        retried = await worker._process_dead_messages()

        # 验证未重试(未到期)
        assert retried == 0
        assert worker.retried_count == 0
        assert worker.permanent_fail_count == 0  # 不是永久失败,只是未到期
        assert worker.processed_count == 1

        # 不应 XADD
        mock_push.assert_not_awaited()
        # 不应 XDEL
        mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exponential_backoff(self, monkeypatch):
        """验证指数退避延迟计算: base_delay * 2^attempts + jitter。

        抖动通过 mock random.uniform 固定为 0,确保断言确定性。
        """
        # Mock random.uniform 返回 0(消除抖动,便于断言)
        monkeypatch.setattr("database.dlq_worker.random.uniform", lambda a, b: 0.0)

        # attempts=1, base_delay=60 → 60 * 2^1 = 120
        delay = dlq_worker.compute_backoff_delay(attempts=1, base_delay=60)
        assert delay == 120.0

        # attempts=2, base_delay=60 → 60 * 2^2 = 240
        delay = dlq_worker.compute_backoff_delay(attempts=2, base_delay=60)
        assert delay == 240.0

        # attempts=3, base_delay=60 → 60 * 2^3 = 480
        delay = dlq_worker.compute_backoff_delay(attempts=3, base_delay=60)
        assert delay == 480.0

        # attempts=5, base_delay=60 → 60 * 2^5 = 1920(指数上限 5)
        delay = dlq_worker.compute_backoff_delay(attempts=5, base_delay=60)
        assert delay == 1920.0

        # attempts=10, base_delay=60 → 60 * 2^5 = 1920(指数被限制在 5)
        delay = dlq_worker.compute_backoff_delay(attempts=10, base_delay=60)
        assert delay == 1920.0

        # 验证抖动:不 mock 时 delay 应在 [base * 2^attempts, base * 2^attempts + base * 0.1] 范围内
        monkeypatch.undo()  # 恢复 random.uniform
        base = 60
        attempts = 2
        expected_base = base * (2 ** min(attempts, 5))
        for _ in range(100):
            d = dlq_worker.compute_backoff_delay(attempts=attempts, base_delay=base)
            assert expected_base <= d <= expected_base + base * 0.1

    @pytest.mark.asyncio
    async def test_redis_unavailable(self, mock_settings, monkeypatch):
        """Redis 不可达时 init() 返回 False(优雅降级,不抛异常)。

        条件: health_check 返回 False → init() 返回 False,Worker 不启动。
        """
        # 设置 Redis URL 非空(通过第一道检查)
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")

        # Mock health_check 返回 False(Redis 不可达)
        monkeypatch.setattr(redis_queue, "health_check", AsyncMock(return_value=False))

        worker = dlq_worker.DLQWorker()
        ok = await worker.init()

        # 验证优雅降级:返回 False,不抛异常
        assert ok is False

    # ── 补充: _is_retryable 纯函数测试 ──

    def test_is_retryable_eligible(self):
        """可重试: next_retry_at <= now 且 attempts < max_attempts。"""
        now = 1000000.0
        dead_msg = _make_dead_msg(
            attempts=0,
            max_attempts=3,
            next_retry_at=now - 10,  # 已到期
        )
        assert dlq_worker.DLQWorker._is_retryable(dead_msg, now) is True

    def test_is_retryable_not_yet(self):
        """不可重试: next_retry_at > now(未到期)。"""
        now = 1000000.0
        dead_msg = _make_dead_msg(
            attempts=0,
            max_attempts=3,
            next_retry_at=now + 3600,  # 未来
        )
        assert dlq_worker.DLQWorker._is_retryable(dead_msg, now) is False

    def test_is_retryable_max_attempts(self):
        """不可重试: attempts >= max_attempts。"""
        now = 1000000.0
        dead_msg = _make_dead_msg(
            attempts=3,
            max_attempts=3,
            next_retry_at=now - 10,  # 已到期但超过最大次数
        )
        assert dlq_worker.DLQWorker._is_retryable(dead_msg, now) is False

    def test_is_retryable_none_next_retry(self):
        """不可重试: next_retry_at 为 None(永久死信)。"""
        now = 1000000.0
        dead_msg = _make_dead_msg(
            attempts=2,
            max_attempts=3,
            next_retry_at=None,  # 永久死信
        )
        assert dlq_worker.DLQWorker._is_retryable(dead_msg, now) is False

    # ── 补充: init() 在 WRITER_MODE=sqlite 时不启动 ──

    @pytest.mark.asyncio
    async def test_init_sqlite_mode_skips(self, mock_settings, monkeypatch):
        """WRITER_MODE=sqlite 时 init() 返回 False(不需要 DLQ Worker)。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "sqlite")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")

        worker = dlq_worker.DLQWorker()
        ok = await worker.init()
        assert ok is False

    @pytest.mark.asyncio
    async def test_init_no_redis_url_skips(self, mock_settings, monkeypatch):
        """REDIS_URL 为空时 init() 返回 False。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "")

        worker = dlq_worker.DLQWorker()
        ok = await worker.init()
        assert ok is False

    @pytest.mark.asyncio
    async def test_init_success(self, mock_settings, monkeypatch):
        """Redis 可达 + WRITER_MODE=redis 时 init() 返回 True。"""
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(redis_queue, "health_check", AsyncMock(return_value=True))

        worker = dlq_worker.DLQWorker()
        ok = await worker.init()
        assert ok is True

    # ── 补充: _process_dead_messages 空队列/非 dict 消息 ──

    @pytest.mark.asyncio
    async def test_process_empty_dead_queue(self, monkeypatch):
        """死信队列为空时 _process_dead_messages 返回 0。"""
        monkeypatch.setattr(
            redis_queue, "get_dead_messages", AsyncMock(return_value=[])
        )
        worker = dlq_worker.DLQWorker()
        retried = await worker._process_dead_messages()
        assert retried == 0
        assert worker.processed_count == 0

    @pytest.mark.asyncio
    async def test_process_non_dict_dead_msg_skipped(self, monkeypatch):
        """死信消息非 dict 类型时跳过(不崩溃)。"""
        monkeypatch.setattr(
            redis_queue,
            "get_dead_messages",
            AsyncMock(return_value=[("bad-id", "not a dict")]),
        )
        worker = dlq_worker.DLQWorker()
        retried = await worker._process_dead_messages()
        assert retried == 0
        assert worker.processed_count == 1  # 已扫描但跳过


# ───────────────────────── redis_queue 新增方法 ─────────────────────────


class TestRedisQueueDeadMessages:
    """redis_queue.get_dead_messages / delete_dead_message 测试。"""

    @pytest.mark.asyncio
    async def test_get_dead_messages_success(self, mock_redis, monkeypatch):
        """get_dead_messages: XRANGE 读取死信消息并解析 JSON。"""
        dead_msg = _make_dead_msg(attempts=1, next_retry_at=time.time() - 5)
        mock_redis.xrange = AsyncMock(return_value=[
            ("1700000000-0", {"data": json.dumps(dead_msg, default=str)}),
        ])
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        messages = await redis_queue.get_dead_messages(count=100)
        assert len(messages) == 1
        msg_id, parsed = messages[0]
        assert msg_id == "1700000000-0"
        assert parsed["attempts"] == 1
        assert parsed["original"]["method_name"] == "upsert_user_quota"

    @pytest.mark.asyncio
    async def test_get_dead_messages_redis_unavailable(self, monkeypatch):
        """get_dead_messages: Redis 不可达时返回空列表。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        messages = await redis_queue.get_dead_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_get_dead_messages_json_decode_failure(self, mock_redis, monkeypatch):
        """get_dead_messages: JSON 解析失败的消息被跳过(不崩溃)。"""
        mock_redis.xrange = AsyncMock(return_value=[
            ("bad-id", {"data": "not valid json"}),
            ("good-id", {"data": json.dumps(_make_dead_msg())}),
        ])
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        messages = await redis_queue.get_dead_messages()
        assert len(messages) == 1  # 坏消息被跳过,只返回好的
        assert messages[0][0] == "good-id"

    @pytest.mark.asyncio
    async def test_delete_dead_message_success(self, mock_redis, monkeypatch):
        """delete_dead_message: XDEL 成功返回 True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.delete_dead_message("1700000000-0")
        assert ok is True
        mock_redis.xdel.assert_awaited_once_with("tgjiema:writer:dead", "1700000000-0")

    @pytest.mark.asyncio
    async def test_delete_dead_message_empty_id(self, mock_redis, monkeypatch):
        """delete_dead_message: 空 msg_id 返回 False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.delete_dead_message("")
        assert ok is False
        mock_redis.xdel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_dead_message_redis_unavailable(self, monkeypatch):
        """delete_dead_message: Redis 不可达返回 False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        ok = await redis_queue.delete_dead_message("some-id")
        assert ok is False
