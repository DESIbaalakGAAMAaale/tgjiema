"""R36 Batch 4: H1-H3 高优先级修复测试。

H1: DLQ XADD 后 XDEL 非原子双操作 → requeue_from_dlq() Lua 脚本原子化
H2: safe_trim() 仅检查单一 Consumer Group → XINFO GROUPS 遍历全部 group
H3: 异常安全双写降级为日志 → DurabilityError 业务必需写入失败必抛

被测模块:
- database.redis_queue: requeue_from_dlq / safe_trim / _stream_id_less
- database.dlq_worker: _retry_message 使用 requeue_from_dlq(原子 XADD+XDEL)
- database.db_writer: _execute_sqlite 检查返回值, _process_message 捕获 DurabilityError
- utils.exceptions: DurabilityError 定义

测试策略:
- redis_queue / dlq_worker: 使用 AsyncMock 模拟 Redis 客户端,不依赖真实 Redis
- db_writer: 使用 MagicMock 模拟 CacheStore,隔离 SQLite 依赖
- _stream_id_less: 纯函数,直接断言
"""
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from database import redis_queue
from utils.exceptions import DurabilityError


# ───────────────────────── H1: requeue_from_dlq ─────────────────────────


class TestRequeueFromDlq:
    """R36 H1: requeue_from_dlq() 原子 XADD + XDEL(Lua 脚本)。"""

    @pytest.mark.asyncio
    async def test_success_returns_true(self, mock_redis, monkeypatch):
        """Lua 脚本返回新 msg_id → True。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.eval = AsyncMock(return_value="1700000500-0")

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="1700000000-0",
            msg_data={"op_type": "upsert", "method_name": "write_heartbeat"},
        )

        assert ok is True
        mock_redis.eval.assert_awaited_once()
        # 验证 eval 参数: script, numkeys, KEYS[1], KEYS[2], ARGV[1], ARGV[2]
        args = mock_redis.eval.await_args.args
        assert args[1] == 2  # numkeys
        assert args[2] == "tgjiema:writer:stream"   # KEYS[1] 主 Stream
        assert args[3] == "tgjiema:writer:dead"    # KEYS[2] 死信 Stream

    @pytest.mark.asyncio
    async def test_redis_unavailable_returns_false(self, monkeypatch):
        """Redis 不可达 → False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="1700000000-0",
            msg_data={"op_type": "upsert"},
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_empty_dead_msg_id_returns_false(self, mock_redis, monkeypatch):
        """dead_msg_id 为空 → False,不调用 eval。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="",
            msg_data={"op_type": "upsert"},
        )

        assert ok is False
        mock_redis.eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_msg_data_returns_false(self, mock_redis, monkeypatch):
        """msg_data 为空 dict → False(空 dict 是 falsy)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="1700000000-0",
            msg_data={},
        )

        assert ok is False
        mock_redis.eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lua_returns_none(self, mock_redis, monkeypatch):
        """Lua 脚本返回 false(redis-py 转为 None)→ False。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.eval = AsyncMock(return_value=None)

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="1700000000-0",
            msg_data={"op_type": "upsert"},
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_eval_exception_returns_false(self, mock_redis, monkeypatch):
        """eval 抛异常 → False(不传播)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.eval = AsyncMock(side_effect=RuntimeError("Lua error"))

        ok = await redis_queue.requeue_from_dlq(
            dead_msg_id="1700000000-0",
            msg_data={"op_type": "upsert"},
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_msg_data_json_serialized(self, mock_redis, monkeypatch):
        """msg_data 被 JSON 序列化后传入 ARGV[1],dead_msg_id 传入 ARGV[2]。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.eval = AsyncMock(return_value="1700000500-0")

        msg_data = {
            "op_type": "upsert",
            "table": "heartbeat_local",
            "method_name": "write_heartbeat",
            "data": {"bot_name": "up_bot"},
            "message_id": "uuid-test",
            "attempts": 1,
        }
        await redis_queue.requeue_from_dlq("1700000000-0", msg_data)

        args = mock_redis.eval.await_args.args
        # ARGV[1] 是第 5 个位置参数(script, numkeys, K1, K2, ARGV1, ARGV2)
        argv1_json = args[4]
        parsed = json.loads(argv1_json)
        assert parsed["op_type"] == "upsert"
        assert parsed["method_name"] == "write_heartbeat"
        assert parsed["attempts"] == 1
        # ARGV[2] 是死信 msg_id
        assert args[5] == "1700000000-0"


# ───────────────────────── H2: safe_trim 多 group ─────────────────────────


class TestSafeTrimMultiGroup:
    """R36 H2: safe_trim() 遍历所有 Consumer Group,取全局最保守水位。"""

    @pytest.mark.asyncio
    async def test_multi_group_takes_global_min_pending(self, mock_redis, monkeypatch):
        """多个 group,取全局最小 pending ID(最保守)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # 两个 group
        mock_redis.xinfo_groups = AsyncMock(return_value=[
            {"name": "group-a", "consumers": 1, "pending": 1,
             "last-delivered-id": "1700000200-0"},
            {"name": "group-b", "consumers": 1, "pending": 1,
             "last-delivered-id": "1700000300-0"},
        ])
        # group-a 的 pending min = 1700000100-0(更小,全局最小)
        # group-b 的 pending min = 1700000250-0(更大)
        def xpending_side_effect(stream, group, *args, **kwargs):
            if group == "group-a":
                return {"pending": 1, "min": "1700000100-0",
                        "max": "1700000200-0", "consumers": []}
            elif group == "group-b":
                return {"pending": 1, "min": "1700000250-0",
                        "max": "1700000300-0", "consumers": []}
            return {"pending": 0, "min": None, "max": None, "consumers": []}
        mock_redis.xpending = AsyncMock(side_effect=xpending_side_effect)
        mock_redis.xtrim = AsyncMock(return_value=5)

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 5
        # 验证 xtrim 用 minid 参数
        kwargs = mock_redis.xtrim.await_args.kwargs
        assert "minid" in kwargs
        # minid 应基于 1700000100(全局最小 pending 的 timestamp - 1)
        # _compute_safe_trim_id: min(pending_ts - 1, safe_ts)
        # pending_ts=1700000100 远小于 safe_ts(now-24h),所以 actual_safe_ts=1700000099
        assert kwargs["minid"].startswith("1700000099-")

    @pytest.mark.asyncio
    async def test_single_group_backward_compat(self, mock_redis, monkeypatch):
        """单个 group(向后兼容):正常裁剪。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.xinfo_groups = AsyncMock(return_value=[
            {"name": "tgjiema-writer-group", "consumers": 1, "pending": 1,
             "last-delivered-id": "1700000200-0"},
        ])
        mock_redis.xpending = AsyncMock(
            return_value={"pending": 1, "min": "1700000100-0",
                          "max": "1700000200-0", "consumers": []}
        )
        mock_redis.xtrim = AsyncMock(return_value=3)

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 3

    @pytest.mark.asyncio
    async def test_no_group_returns_zero(self, mock_redis, monkeypatch):
        """无 Consumer Group → 0(保守不裁剪)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.xinfo_groups = AsyncMock(return_value=[])

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 0
        mock_redis.xtrim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_xinfo_groups_failure_returns_zero(self, mock_redis, monkeypatch):
        """xinfo_groups 失败 → 0(保守不裁剪)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.xinfo_groups = AsyncMock(side_effect=RuntimeError("NOGROUP"))

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 0
        mock_redis.xtrim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_unavailable_returns_zero(self, monkeypatch):
        """Redis 不可达 → 0。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 0

    @pytest.mark.asyncio
    async def test_no_pending_uses_last_delivered_id(self, mock_redis, monkeypatch):
        """所有 group 无 pending,用 last-delivered-id 作为保守水位。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.xinfo_groups = AsyncMock(return_value=[
            {"name": "group-a", "consumers": 1, "pending": 0,
             "last-delivered-id": "1700000500-0"},
        ])
        mock_redis.xpending = AsyncMock(
            return_value={"pending": 0, "min": None, "max": None, "consumers": []}
        )
        mock_redis.xtrim = AsyncMock(return_value=10)

        trimmed = await redis_queue.safe_trim()

        # 无 pending,用 last-delivered-id → 裁剪 10 条
        assert trimmed == 10

    @pytest.mark.asyncio
    async def test_tuple_format_xinfo_groups_with_pending(self, mock_redis, monkeypatch):
        """xinfo_groups 返回 tuple 格式(旧版 redis-py 兼容),有 pending 时裁剪。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # 旧版 redis-py 可能返回 tuple[name, consumers, pending, ...]
        mock_redis.xinfo_groups = AsyncMock(return_value=[
            ("group-a", 1, 1, "extra-field"),
        ])
        mock_redis.xpending = AsyncMock(
            return_value={"pending": 1, "min": "1700000100-0",
                          "max": "1700000200-0", "consumers": []}
        )
        mock_redis.xtrim = AsyncMock(return_value=5)

        trimmed = await redis_queue.safe_trim()

        # tuple 路径能取 group_name[0],xpending 查询到 pending → 裁剪
        assert trimmed == 5

    @pytest.mark.asyncio
    async def test_xpending_tuple_format(self, mock_redis, monkeypatch):
        """xpending 返回 tuple 格式(count, min_id, max_id, consumers),兼容解析。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        mock_redis.xinfo_groups = AsyncMock(return_value=[
            {"name": "group-a", "consumers": 1, "pending": 1,
             "last-delivered-id": "1700000200-0"},
        ])
        # tuple 格式: (count, min_id, max_id, consumers)
        mock_redis.xpending = AsyncMock(
            return_value=(1, "1700000100-0", "1700000200-0", [])
        )
        mock_redis.xtrim = AsyncMock(return_value=7)

        trimmed = await redis_queue.safe_trim()

        assert trimmed == 7


# ───────────────────────── H2: _stream_id_less ─────────────────────────


class TestStreamIdLess:
    """R36 H2: _stream_id_less() Stream ID 比较函数。"""

    def test_less_by_timestamp(self):
        """时间戳不同:a < b → True。"""
        assert redis_queue._stream_id_less("1700000000-0", "1700000001-0") is True

    def test_greater_by_timestamp(self):
        """时间戳不同:a > b → False。"""
        assert redis_queue._stream_id_less("1700000001-0", "1700000000-0") is False

    def test_less_by_seq_same_timestamp(self):
        """时间戳相同,seq 不同:a < b → True。"""
        assert redis_queue._stream_id_less("1700000000-0", "1700000000-1") is True

    def test_equal(self):
        """完全相等:a 不小于 b → False。"""
        assert redis_queue._stream_id_less("1700000000-0", "1700000000-0") is False

    def test_parse_failure_returns_false(self):
        """解析失败 → False(保守不更新最小)。"""
        assert redis_queue._stream_id_less("invalid", "1700000000-0") is False

    def test_none_values(self):
        """None 值 → False(保守)。"""
        assert redis_queue._stream_id_less(None, "1700000000-0") is False


# ───────────────────────── H1: dlq_worker._retry_message ─────────────────────────


class TestDlqWorkerRetryMessage:
    """R36 H1: dlq_worker._retry_message() 使用 requeue_from_dlq()。"""

    @pytest.mark.asyncio
    async def test_uses_requeue_from_dlq(self, monkeypatch):
        """_retry_message 调用 requeue_from_dlq,不再调用 push + delete_dead_message。"""
        from database import dlq_worker

        mock_requeue = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "requeue_from_dlq", mock_requeue)
        # 如果仍调用旧方法,这些 mock 不应被调用
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        mock_delete = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "delete_dead_message", mock_delete)

        worker = dlq_worker.DLQWorker()
        dead_msg = {
            "original": {
                "op_type": "upsert",
                "table": "user_quota",
                "method_name": "upsert_user_quota",
                "data": {"user_id": 12345, "quota": 20},
                "redis_key": "cache:user_quota:12345",
                "message_id": "uuid-test-1",
                "created_at": 1000000.0,
            },
            "reason": "test error",
            "message_id": "uuid-test-1",
            "attempts": 0,
            "max_attempts": 3,
            "failed_at": 1000000.0,
            "next_retry_at": time.time() - 10,  # 已到期
        }

        ok = await worker._retry_message("1700000000-0", dead_msg)

        assert ok is True
        # 关键: 使用 requeue_from_dlq(原子操作)
        mock_requeue.assert_awaited_once()
        # 关键: 不再调用 push 和 delete_dead_message(旧的非原子双操作)
        mock_push.assert_not_awaited()
        mock_delete.assert_not_awaited()
        # 验证 requeue_from_dlq 参数
        _, kwargs = mock_requeue.await_args
        assert kwargs["dead_msg_id"] == "1700000000-0"
        assert kwargs["msg_data"]["op_type"] == "upsert"
        assert kwargs["msg_data"]["method_name"] == "upsert_user_quota"
        assert kwargs["msg_data"]["message_id"] == "uuid-test-1"
        assert kwargs["msg_data"]["attempts"] == 1  # 0 + 1

    @pytest.mark.asyncio
    async def test_requeue_failure_returns_false(self, monkeypatch):
        """requeue_from_dlq 返回 False → 保留死信(返回 False)。"""
        from database import dlq_worker

        mock_requeue = AsyncMock(return_value=False)
        monkeypatch.setattr(redis_queue, "requeue_from_dlq", mock_requeue)

        worker = dlq_worker.DLQWorker()
        dead_msg = {
            "original": {"op_type": "upsert", "table": "user_quota",
                         "method_name": "upsert_user_quota",
                         "data": {}, "message_id": "uuid-test-2"},
            "reason": "test",
            "message_id": "uuid-test-2",
            "attempts": 1,
            "max_attempts": 3,
            "failed_at": 1000000.0,
            "next_retry_at": time.time() - 10,
        }

        ok = await worker._retry_message("1700000001-0", dead_msg)

        assert ok is False

    @pytest.mark.asyncio
    async def test_requeue_exception_returns_false(self, monkeypatch):
        """requeue_from_dlq 抛异常 → 保留死信(返回 False)。"""
        from database import dlq_worker

        mock_requeue = AsyncMock(side_effect=RuntimeError("Redis down"))
        monkeypatch.setattr(redis_queue, "requeue_from_dlq", mock_requeue)

        worker = dlq_worker.DLQWorker()
        dead_msg = {
            "original": {"op_type": "upsert", "table": "user_quota",
                         "method_name": "upsert_user_quota",
                         "data": {}, "message_id": "uuid-test-3"},
            "reason": "test",
            "message_id": "uuid-test-3",
            "attempts": 0,
            "max_attempts": 3,
            "failed_at": 1000000.0,
            "next_retry_at": time.time() - 10,
        }

        ok = await worker._retry_message("1700000002-0", dead_msg)

        assert ok is False

    @pytest.mark.asyncio
    async def test_original_not_dict_returns_false(self, monkeypatch):
        """dead_msg.original 非 dict → 返回 False。"""
        from database import dlq_worker

        worker = dlq_worker.DLQWorker()
        dead_msg = {
            "original": "not a dict",  # 非 dict
            "reason": "corrupted",
            "message_id": "uuid-test-4",
            "attempts": 0,
            "max_attempts": 3,
            "failed_at": 1000000.0,
            "next_retry_at": time.time() - 10,
        }

        ok = await worker._retry_message("1700000003-0", dead_msg)

        assert ok is False


# ───────────────────────── H3: DurabilityError ─────────────────────────


class TestDurabilityError:
    """R36 H3: DurabilityError 异常定义。"""

    def test_is_exception_subclass(self):
        """DurabilityError 是 Exception 子类。"""
        assert issubclass(DurabilityError, Exception)

    def test_can_be_raised(self):
        """DurabilityError 可被 raise。"""
        with pytest.raises(DurabilityError, match="test message"):
            raise DurabilityError("test message")


# ───────────────────────── H3: db_writer._execute_sqlite ─────────────────────────

# 延迟导入 db_writer(与 test_redis_writer_integration.py 一致)
db_writer_module = pytest.importorskip("database.db_writer")
DBWriterClass = getattr(db_writer_module, "DBWriter", None)
DBWriterMessage = getattr(db_writer_module, "DBWriterMessage", None)

if DBWriterClass is None:
    pytest.skip("database.db_writer 未定义 DBWriter 类", allow_module_level=True)


def _make_writer_msg(method_name: str, **data):
    """构造 DBWriterMessage 用于 _execute_sqlite 测试。"""
    return DBWriterMessage(
        op_type="upsert",
        table="test_table",
        method_name=method_name,
        data=data,
        redis_key="",
        message_id="msg-uuid-test",
        created_at=time.time(),
        stream_id="1700000000-0",
    )


class TestExecuteSqliteDurability:
    """R36 H3: _execute_sqlite 对业务必需方法检查返回值,失败抛 DurabilityError。"""

    @pytest.mark.asyncio
    async def test_bool_method_returns_false_raises(self):
        """bool 返回的业务方法(transition_upload_session)返回 False → 抛 DurabilityError。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.transition_upload_session = AsyncMock(return_value=False)

        msg = _make_writer_msg(
            "transition_upload_session",
            upload_id="upload-123", new_status="COPIED_PRIMARY",
        )

        with pytest.raises(DurabilityError, match="transition_upload_session"):
            await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_bool_method_returns_true_no_error(self):
        """bool 返回的业务方法返回 True → 不抛。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.transition_upload_session = AsyncMock(return_value=True)

        msg = _make_writer_msg(
            "transition_upload_session",
            upload_id="upload-123", new_status="COPIED_PRIMARY",
        )

        # 不应抛异常
        await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_int_method_returns_zero_raises(self):
        """int 返回的业务方法(create_replication_task)返回 0 → 抛 DurabilityError。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.create_replication_task = AsyncMock(return_value=0)

        msg = _make_writer_msg(
            "create_replication_task",
            group_id=1, file_unique_id="file-1", src_channel_id=100,
            dst_channel_id=200, src_msg_id=1000,
        )

        with pytest.raises(DurabilityError, match="create_replication_task"):
            await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_int_method_returns_nonzero_no_error(self):
        """int 返回的业务方法返回非零 → 不抛。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.create_replication_task = AsyncMock(return_value=42)

        msg = _make_writer_msg(
            "create_replication_task",
            group_id=1, file_unique_id="file-1", src_channel_id=100,
            dst_channel_id=200, src_msg_id=1000,
        )

        await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_none_return_method_no_durability_check(self):
        """返回 None 的方法(create_upload_session)不检查返回值(None 是正常返回)。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.create_upload_session = AsyncMock(return_value=None)

        msg = _make_writer_msg(
            "create_upload_session",
            upload_id="upload-123", user_id=10050,
        )

        # 不应抛 DurabilityError(None 是正常返回)
        await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_non_durability_method_no_check(self):
        """非业务必需方法(write_heartbeat)不检查返回值。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.write_heartbeat = AsyncMock(return_value=None)

        msg = _make_writer_msg(
            "write_heartbeat",
            bot_name="up_bot",
        )

        await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_mark_replication_committed_false_raises(self):
        """mark_replication_committed 返回 False → 抛 DurabilityError。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.mark_replication_committed = AsyncMock(return_value=False)

        msg = _make_writer_msg(
            "mark_replication_committed",
            task_id=1,
        )

        with pytest.raises(DurabilityError, match="mark_replication_committed"):
            await writer._execute_sqlite(msg)

    @pytest.mark.asyncio
    async def test_confirm_delivery_receipt_false_raises(self):
        """confirm_delivery_receipt 返回 False → 抛 DurabilityError。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.confirm_delivery_receipt = AsyncMock(return_value=False)

        msg = _make_writer_msg(
            "confirm_delivery_receipt",
            job_id=1, source_msg_id=100, sent_msg_id=200,
        )

        with pytest.raises(DurabilityError, match="confirm_delivery_receipt"):
            await writer._execute_sqlite(msg)


class TestProcessMessageDurabilityError:
    """R36 H3: _process_message 捕获 DurabilityError 入永久死信。"""

    @pytest.mark.asyncio
    async def test_durability_error_enters_permanent_dlq(self, monkeypatch):
        """DurabilityError → push_dead(permanent=True) → ACK,ROLLBACK 被调用。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.check_writer_inbox = AsyncMock(return_value=False)
        writer._store.begin_writer_tx = AsyncMock(return_value=None)
        writer._store.commit_writer_tx = AsyncMock(return_value=None)
        writer._store.rollback_writer_tx = AsyncMock(return_value=None)
        # inbox INSERT 成功(rowcount=1)
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        writer._store._db = MagicMock()
        writer._store._db.execute = AsyncMock(return_value=mock_cursor)
        # transition_upload_session 返回 False → 抛 DurabilityError
        writer._store.transition_upload_session = AsyncMock(return_value=False)

        # mock redis_queue
        mock_push_dead = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        monkeypatch.setattr(redis_queue, "ack", AsyncMock(return_value=1))
        monkeypatch.setattr(redis_queue, "delete", AsyncMock(return_value=True))

        msg = {
            "op_type": "update",
            "table": "upload_sessions",
            "method_name": "transition_upload_session",
            "data": {"upload_id": "upload-123", "new_status": "COPIED_PRIMARY"},
            "redis_key": "",
            "message_id": "msg-uuid-durability-test",
            "created_at": time.time(),
            "_stream_id": "1700000000-0",
        }

        await writer._process_message(msg)

        # 验证 DurabilityError 被捕获,error_count +1
        assert writer._error_count == 1
        # 验证 push_dead 被调用,且 permanent=True(永久死信,不重试)
        mock_push_dead.assert_awaited_once()
        _, kwargs = mock_push_dead.await_args
        assert kwargs["permanent"] is True
        assert "DurabilityError" in kwargs["reason"]
        # 验证 rollback 被调用(DurabilityError 触发 ROLLBACK)
        writer._store.rollback_writer_tx.assert_awaited_once()
        # 验证 commit 未被调用(ROLLBACK 而非 COMMIT)
        writer._store.commit_writer_tx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_durability_error_dlq_failure_no_ack(self, monkeypatch):
        """DurabilityError + DLQ 写入失败 → 不 ACK(消息保留 pending等待重试)。"""
        writer = DBWriterClass()
        writer._store = MagicMock()
        writer._store.check_writer_inbox = AsyncMock(return_value=False)
        writer._store.begin_writer_tx = AsyncMock(return_value=None)
        writer._store.commit_writer_tx = AsyncMock(return_value=None)
        writer._store.rollback_writer_tx = AsyncMock(return_value=None)
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        writer._store._db = MagicMock()
        writer._store._db.execute = AsyncMock(return_value=mock_cursor)
        # create_replication_task 返回 0 → 抛 DurabilityError
        writer._store.create_replication_task = AsyncMock(return_value=0)

        # mock push_dead 返回 False(DLQ 写入失败)
        mock_push_dead = AsyncMock(return_value=False)
        monkeypatch.setattr(redis_queue, "push_dead", mock_push_dead)
        mock_ack = AsyncMock(return_value=1)
        monkeypatch.setattr(redis_queue, "ack", mock_ack)

        msg = {
            "op_type": "insert",
            "table": "replication_tasks",
            "method_name": "create_replication_task",
            "data": {"group_id": 1, "file_unique_id": "f-1",
                     "src_channel_id": 100, "dst_channel_id": 200, "src_msg_id": 1000},
            "redis_key": "",
            "message_id": "msg-uuid-dlq-fail",
            "created_at": time.time(),
            "_stream_id": "1700000000-0",
        }

        await writer._process_message(msg)

        # 验证 DurabilityError 被捕获
        assert writer._error_count == 1
        assert writer._dead_fail_count == 1
        # 验证 ACK 未被调用(DLQ 失败,消息保留 pending)
        mock_ack.assert_not_awaited()
        # 验证 push_dead 被调用(permanent=True)
        mock_push_dead.assert_awaited_once()
        _, kwargs = mock_push_dead.await_args
        assert kwargs["permanent"] is True
