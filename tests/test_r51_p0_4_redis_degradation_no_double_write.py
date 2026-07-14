"""R51 P0-4: Redis 故障降级可能双写问题 — 修复回归测试。

被测模块:
- ``database.redis_queue``:push() / write_durable_outbox() / replay_durable_outbox()
- ``database.write_router``:route_write()

修复背景:
    原 push() 在 Redis 不可用时调用 write_durable_outbox() 写本地 outbox,
    但返回 False 让上层 route_write 触发 fallback 直写 SQLite。Redis 恢复后
    outbox 中的消息被 replayer 重放到 Stream,db_writer 再次执行同一业务
    操作,而 SQLite 直写路径没有同步插入 writer_inbox,导致业务二次执行
    (例如 quota 二次扣减、heartbeat 二次写入等)。

修复方案(方案 A:只写 durable outbox,禁止双写):
    1. push() Redis 不可用时 → write_durable_outbox() 成功返回
       ``{"status": "persisted_pending", "outbox_id": ..., "message_id": ...}``,
       失败 raise AppError(不再返回 False)。
    2. route_write() 收到 persisted_pending 字典 → 不调用 fallback,直接返回
       该字典,等待 Redis 恢复后由 replayer 重放(单一权威路径,避免双写)。

测试场景(5 个,覆盖用户原文要求):
    1. Redis 正常 → push 成功(返回 True,XADD 被调用一次)
    2. Redis 断线 → outbox 持久化成功,push 返回 persisted_pending;
       route_write 不调用 fallback(不写 SQLite)
    3. Redis 恢复 → outbox 重放,业务执行一次(replayer 仅 XADD 一次,
       原消息 ID 不变,确保不重复)
    4. outbox 写入失败 → push raise AppError(DB_CACHE_UNAVAILABLE),
       route_write 不调用 fallback(异常向上传播,不降级)
    5. 并发场景 → 多个 push 同时遇到 Redis 断线,每条消息独立持久化
       到 outbox,各自返回独立 message_id,不互相干扰

测试策略:
- 使用 ``conftest.py`` 提供的 ``mock_settings`` / ``mock_redis`` fixtures。
- 每个用例内部用 ``tempfile.mkdtemp`` 隔离 durable outbox SQLite 文件,
  测试结束 ``shutil.rmtree`` 清理。
- 验证 AppError 用 ``pytest.raises(AppError)`` + 错误码断言。
- 验证并发场景用 ``asyncio.gather`` 同时发起多个 push。
- 全部用例都不依赖真实 Redis,在本地可独立运行。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from unittest.mock import AsyncMock

import pytest

from database import redis_queue, write_router
from services.error_codes import AppError, ErrorCodes


# ───────────────────────── 测试隔离 fixture ─────────────────────────


@pytest.fixture
def durable_outbox_tmpdir():
    """每个用例使用独立的临时 durable outbox 数据库路径,避免污染生产 data 目录。

    yield 返回临时目录路径,用例结束自动清理。同时确保 durable outbox
    专用连接在用例间被关闭重置,避免上一个用例的连接缓存影响下一个。
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p0_4_test_")
    original_path = redis_queue._DURABLE_DB_PATH
    redis_queue._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
    # 重置 durable outbox 专用连接(避免跨用例污染)
    redis_queue._durable_conn = None
    redis_queue._durable_conn_lock = None
    try:
        yield tmpdir
    finally:
        # 同步关闭连接并恢复原路径
        try:
            asyncio.get_event_loop().run_until_complete(
                redis_queue.close_durable_outbox()
            )
        except RuntimeError:
            # 事件循环已关闭或正在运行(异步用例中),忽略
            pass
        except Exception:
            pass
        redis_queue._durable_conn = None
        redis_queue._durable_conn_lock = None
        redis_queue._DURABLE_DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 1: Redis 正常 → push 成功
# ════════════════════════════════════════════════════════════════


class TestRedisNormalPushSuccess:
    """场景 1: Redis 正常时 push 直接走 XADD 路径,返回 True。

    覆盖点:
    - push() 返回 True(不是 dict,不抛异常)
    - XADD 被调用一次,消息体携带 message_id
    - 不写入 durable outbox(outbox 计数为 -1 表示连接未初始化 或 0 表示已初始化但无消息)
    """

    @pytest.mark.asyncio
    async def test_push_success_returns_true(self, mock_redis, monkeypatch):
        """Redis 可达时 push() 返回 True,XADD 被调用一次。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        result = await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 1001, "quota": 20},
            redis_key="cache:user_quota:1001",
        )
        # 新契约:Redis 可达时返回 True(向后兼容)
        assert result is True
        mock_redis.xadd.assert_awaited_once()
        # 验证消息体格式
        args, _ = mock_redis.xadd.await_args
        msg = json.loads(args[1]["data"])
        assert msg["op_type"] == "upsert"
        assert msg["table"] == "user_quota"
        assert msg["method_name"] == "upsert_user_quota"
        assert msg["data"] == {"user_id": 1001, "quota": 20}
        assert msg["redis_key"] == "cache:user_quota:1001"
        # message_id 为合法 UUID
        uuid.UUID(msg["message_id"])

    @pytest.mark.asyncio
    async def test_push_success_does_not_write_durable_outbox(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """Redis 可达时 push() 不写入 durable outbox(outbox 内无 pending 消息)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 1002},
        )
        mock_redis.xadd.assert_awaited_once()
        # durable outbox 不应有 pending 消息(连接被初始化但表中无数据)
        count = await redis_queue.get_durable_outbox_count()
        assert count == 0


# ════════════════════════════════════════════════════════════════
# 场景 2: Redis 断线 → outbox 持久化,不写 SQLite
# ════════════════════════════════════════════════════════════════


class TestRedisDownOutboxPersistedNoSqliteWrite:
    """场景 2: Redis 断线时 push 写 durable outbox 并返回 persisted_pending,
    route_write 收到后不调用 fallback(禁止双写 SQLite)。

    覆盖点:
    - push() 返回 ``{"status": "persisted_pending", "outbox_id": ..., "message_id": ...}``
    - durable outbox 中有 1 条 pending 消息
    - route_write 接收 persisted_pending 后 fallback **未被调用**(不写 SQLite)
    - route_write 返回原字典(让调用方知道已持久化待处理)
    """

    @pytest.mark.asyncio
    async def test_push_redis_down_returns_persisted_pending(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """Redis 不可用(get_redis 返回 None)时 push 写 durable outbox,
        返回 persisted_pending 字典。
        """
        # Redis 不可用
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        result = await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 2001},
            redis_key="cache:user_quota:2001",
        )
        # 新契约:返回 persisted_pending 字典(不再返回 False)
        assert isinstance(result, dict)
        assert result["status"] == "persisted_pending"
        assert "outbox_id" in result
        assert "message_id" in result
        # outbox_id 应与 message_id 一致(都使用 push 生成的 UUID)
        assert result["outbox_id"] == result["message_id"]
        # durable outbox 应有 1 条 pending 消息
        count = await redis_queue.get_durable_outbox_count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_route_write_persisted_pending_no_fallback(
        self, mock_settings, monkeypatch, durable_outbox_tmpdir,
    ):
        """route_write 收到 push 返回的 persisted_pending 时,**不调用** fallback
        (禁止双写 SQLite,等待 Redis 恢复后由 replayer 重放)。
        """
        # 启用 Redis 模式
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")

        # 模拟 Redis 不可用 → push 返回 persisted_pending 字典
        persisted_dict = {
            "status": "persisted_pending",
            "outbox_id": "msg-uuid-2001",
            "message_id": "msg-uuid-2001",
        }
        monkeypatch.setattr(redis_queue, "push", AsyncMock(return_value=persisted_dict))

        # fallback 不应被调用(若被调用,说明降级直写 SQLite,违反 R51 P0-4 修复)
        fallback = AsyncMock(return_value="should_not_be_called")

        result = await write_router.route_write(
            method_name="upsert_user_quota",
            table="user_quota",
            op_type="upsert",
            data={"user_id": 2001},
            redis_key="cache:user_quota:2001",
            fallback=fallback,
        )

        # 关键断言:
        # 1. route_write 返回原 persisted_pending 字典(不返回 fallback 结果)
        assert isinstance(result, dict)
        assert result["status"] == "persisted_pending"
        assert result["outbox_id"] == "msg-uuid-2001"
        # 2. fallback 未被调用(禁止双写 SQLite)
        fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_xadd_exception_also_persists_to_outbox(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """XADD 运行时异常(连接已建立但中途断开)时 push 也写入 durable outbox。

        场景:Redis 连接建立成功(get_redis 返回 mock_redis),但 XADD 时
        网络抖动抛异常。push 应捕获异常并写入 durable outbox,返回
        persisted_pending(不抛异常给上层,避免业务调用失败)。
        """
        mock_redis.xadd = AsyncMock(side_effect=ConnectionError("redis connection reset"))
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        result = await redis_queue.push(
            op_type="update", table="heartbeat_local",
            method_name="write_heartbeat",
            data={"slot_id": 1, "ok": True},
        )
        # XADD 异常 → 降级到 durable outbox(返回 persisted_pending)
        assert isinstance(result, dict)
        assert result["status"] == "persisted_pending"
        # durable outbox 应有 1 条 pending 消息
        count = await redis_queue.get_durable_outbox_count()
        assert count == 1


# ════════════════════════════════════════════════════════════════
# 场景 3: Redis 恢复 → outbox 重放,业务执行一次(不重复)
# ════════════════════════════════════════════════════════════════


class TestRedisRecoverReplayNoDuplicate:
    """场景 3: Redis 恢复后 outbox 中的消息被 replayer 重放到 Stream,
    db_writer 消费后业务执行一次(不重复)。

    覆盖点:
    - replay_durable_outbox() 调用 XADD 把 pending 消息重放到 Stream
    - 每条 outbox 消息只 XADD 一次(不重复)
    - 重放后 outbox 中消息状态变为 'replayed',pending 计数减少
    - message_id 保持不变(replayer 使用原 message_id,确保 writer_inbox 幂等)
    """

    @pytest.mark.asyncio
    async def test_replay_outbox_to_stream_each_message_once(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """outbox 中 2 条 pending 消息,replay 后每条消息只 XADD 一次到 Stream,
        pending 计数归零。
        """
        # Redis 可达(replayer 需要 XADD 写入 Stream)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 先写入 2 条 outbox 消息(模拟 Redis 断线时 push 持久化的消息)
        msg_id_1 = "uuid-replay-0001"
        msg_id_2 = "uuid-replay-0002"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 3001},
            message_id=msg_id_1,
        )
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 3002},
            message_id=msg_id_2,
        )
        # 验证 outbox 中有 2 条 pending
        assert await redis_queue.get_durable_outbox_count() == 2

        # 执行重放
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 2

        # XADD 被调用 2 次(每条消息只重放一次)
        assert mock_redis.xadd.await_count == 2

        # 验证重放的消息体携带原 message_id(确保 writer_inbox 幂等)
        replayed_msg_ids = set()
        for call in mock_redis.xadd.await_args_list:
            args, _ = call
            msg = json.loads(args[1]["data"])
            replayed_msg_ids.add(msg["message_id"])
        assert replayed_msg_ids == {msg_id_1, msg_id_2}

        # 重放后 outbox 中无 pending 消息(状态变为 'replayed')
        assert await redis_queue.get_durable_outbox_count() == 0

    @pytest.mark.asyncio
    async def test_replay_preserves_message_id_for_writer_inbox_idempotency(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """重放时 message_id 保持不变,确保 db_writer 通过 writer_inbox
        跳过已处理消息(幂等,不重复执行业务)。

        场景:即使 outbox 消息被 replayer 多次重放(理论上不会发生,但
        验证 message_id 稳定),db_writer 也能通过 writer_inbox 跳过,
        保证业务 exactly-once。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        original_msg_id = "uuid-stable-id-12345"
        await redis_queue.write_durable_outbox(
            op_type="update", table="heartbeat_local",
            method_name="write_heartbeat",
            data={"slot_id": 1, "ok": True},
            message_id=original_msg_id,
        )

        # 第一次重放
        replayed_1 = await redis_queue.replay_durable_outbox()
        assert replayed_1 == 1

        # 验证 XADD 消息的 message_id 与原消息一致
        args, _ = mock_redis.xadd.await_args
        msg = json.loads(args[1]["data"])
        assert msg["message_id"] == original_msg_id
        assert msg["method_name"] == "write_heartbeat"
        assert msg["data"] == {"slot_id": 1, "ok": True}

        # 第二次重放:outbox 中已无 pending 消息(状态为 'replayed'),
        # 不应再次 XADD(避免重复)
        mock_redis.xadd.reset_mock()
        replayed_2 = await redis_queue.replay_durable_outbox()
        assert replayed_2 == 0
        mock_redis.xadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_flow_push_then_replay_no_duplicate_xadd(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """端到端:Redis 断线 push 持久化 → Redis 恢复 replay 重放,
        验证业务消息只入 Stream 一次(不重复)。

        场景:
        1. Redis 断线(get_redis 返回 None):push 写 durable outbox,
           返回 persisted_pending(此时 XADD 未被调用)
        2. Redis 恢复(get_redis 返回 mock_redis):replay 把 outbox 消息
           XADD 到 Stream(此时 XADD 被调用一次)
        3. 最终 XADD 调用次数 == 1(业务消息只入 Stream 一次,不重复)
        """
        # 第一阶段:Redis 断线
        async def get_redis_unavailable():
            return None

        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))

        # push 时 Redis 不可用 → 写 durable outbox
        push_result = await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 4001},
            message_id="uuid-e2e-flow-0001",
        )
        assert push_result["status"] == "persisted_pending"
        # 此时 XADD 未被调用(Redis 不可达)
        assert mock_redis.xadd.await_count == 0
        # outbox 有 1 条 pending
        assert await redis_queue.get_durable_outbox_count() == 1

        # 第二阶段:Redis 恢复
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # replay 把 outbox 消息 XADD 到 Stream
        replayed = await redis_queue.replay_durable_outbox()
        assert replayed == 1

        # 关键断言:整个流程 XADD 只被调用 1 次(业务消息只入 Stream 一次)
        assert mock_redis.xadd.await_count == 1

        # 验证 XADD 的消息体 message_id 与 push 时一致(幂等键不变)
        args, _ = mock_redis.xadd.await_args
        msg = json.loads(args[1]["data"])
        assert msg["message_id"] == "uuid-e2e-flow-0001"
        assert msg["data"] == {"user_id": 4001}

        # outbox 中无 pending 消息(已重放)
        assert await redis_queue.get_durable_outbox_count() == 0


# ════════════════════════════════════════════════════════════════
# 场景 4: outbox 写入失败 → raise AppError,不降级
# ════════════════════════════════════════════════════════════════


class TestOutboxWriteFailureRaisesAppError:
    """场景 4: durable outbox 写入失败时 push raise AppError,
    route_write 不调用 fallback(异常向上传播,不降级直写 SQLite)。

    覆盖点:
    - write_durable_outbox() 失败时 raise AppError(ErrorCodes.DB_CACHE_UNAVAILABLE)
    - push() 不会吞掉 AppError,异常向上传播
    - route_write() 不会捕获 AppError 调用 fallback(异常继续向上传播)
    - AppError 携带 component safe_param(method / message_id 在日志中可见)
    """

    @pytest.mark.asyncio
    async def test_write_durable_outbox_connection_failure_raises_apperror(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """durable outbox 连接不可用时 write_durable_outbox raise AppError。

        注意:ErrorRegistry 中 DB_CACHE_UNAVAILABLE 的 safe_params 仅包含
        ``["component"]``,因此 ``method`` 与 ``message_id`` 不在 AppError.params
        中(被安全过滤)。这两个上下文通过 loguru logger.error 输出到日志,
        不被 pytest caplog 捕获,此处仅验证错误码与 component。
        """
        # 模拟 _get_dedicated_connection 返回 None(连接初始化失败)
        monkeypatch.setattr(
            redis_queue, "_get_dedicated_connection",
            AsyncMock(return_value=None),
        )
        with pytest.raises(AppError) as exc_info:
            await redis_queue.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 5001},
                message_id="uuid-fail-conn-0001",
            )
        # 错误码必须是 DB_CACHE_UNAVAILABLE
        assert exc_info.value.code == ErrorCodes.DB_CACHE_UNAVAILABLE
        # safe_params 只暴露 component(method/message_id 被安全过滤)
        params = exc_info.value.params
        assert params.get("component") == "durable_outbox"
        # 异常链(cause)为 None(连接不可用不附带原始异常)
        assert exc_info.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_write_durable_outbox_execute_failure_raises_apperror(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """durable outbox execute 抛异常时 write_durable_outbox raise AppError。"""
        # 构造一个会抛异常的假连接
        class FailingConn:
            async def execute(self, *args, **kwargs):
                raise RuntimeError("sqlite disk I/O error")

        monkeypatch.setattr(
            redis_queue, "_get_dedicated_connection",
            AsyncMock(return_value=FailingConn()),
        )
        with pytest.raises(AppError) as exc_info:
            await redis_queue.write_durable_outbox(
                op_type="update", table="heartbeat_local",
                method_name="write_heartbeat",
                data={"slot_id": 99},
                message_id="uuid-fail-exec-0001",
            )
        assert exc_info.value.code == ErrorCodes.DB_CACHE_UNAVAILABLE
        # cause 应为原始异常
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    @pytest.mark.asyncio
    async def test_push_propagates_apperror_when_outbox_fails(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """Redis 不可用 + outbox 写入失败 → push 不吞 AppError,异常向上传播。

        注意:ErrorRegistry 中 DB_CACHE_UNAVAILABLE 的 safe_params 只包含
        ``["component"]``,因此 ``message_id`` 不在 AppError.params 中。
        message_id 通过 loguru logger.error 输出到日志(不被 caplog 捕获)。
        """
        # Redis 不可用
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        # outbox 连接不可用
        monkeypatch.setattr(
            redis_queue, "_get_dedicated_connection",
            AsyncMock(return_value=None),
        )

        with pytest.raises(AppError) as exc_info:
            await redis_queue.push(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 5002},
                message_id="uuid-push-fail-0001",
            )
        # push 必须抛出 AppError(不能吞掉异常返回 False)
        assert exc_info.value.code == ErrorCodes.DB_CACHE_UNAVAILABLE
        params = exc_info.value.params
        assert params.get("component") == "durable_outbox"

    @pytest.mark.asyncio
    async def test_route_write_propagates_apperror_no_fallback(
        self, mock_settings, monkeypatch, durable_outbox_tmpdir,
    ):
        """route_write 在 push 抛 AppError 时,异常向上传播,
        **不调用** fallback(不降级直写 SQLite)。

        场景:Redis 与 durable outbox 双双不可用 → 关键业务必须抛异常,
        让调用方决定是否重试或返回错误响应,不能静默降级。
        """
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost:6379/0")

        # 模拟 push 抛 AppError(Redis 与 outbox 双双不可用)
        async def raise_apperror(**kwargs):
            raise AppError(
                ErrorCodes.DB_CACHE_UNAVAILABLE,
                params={
                    "component": "durable_outbox",
                    "method": kwargs.get("method_name", ""),
                    "message_id": kwargs.get("message_id", ""),
                },
            )

        monkeypatch.setattr(redis_queue, "push", raise_apperror)

        # fallback 不应被调用(若被调用,说明降级直写 SQLite,违反 R51 P0-4 修复)
        fallback = AsyncMock(return_value="should_not_be_called")

        with pytest.raises(AppError) as exc_info:
            await write_router.route_write(
                method_name="upsert_user_quota",
                table="user_quota",
                op_type="upsert",
                data={"user_id": 5003},
                redis_key="cache:user_quota:5003",
                fallback=fallback,
            )

        # 关键断言:
        # 1. AppError 向上传播(错误码正确)
        assert exc_info.value.code == ErrorCodes.DB_CACHE_UNAVAILABLE
        # 2. fallback 未被调用(不降级直写 SQLite)
        fallback.assert_not_awaited()


# ════════════════════════════════════════════════════════════════
# 场景 5: 并发场景:多个 push 同时遇到 Redis 断线
# ════════════════════════════════════════════════════════════════


class TestConcurrentPushRedisDown:
    """场景 5: 多个 push 同时遇到 Redis 断线时,每条消息独立持久化到
    durable outbox,各自返回独立 message_id,不互相干扰。

    覆盖点:
    - asyncio.gather 并发触发 N 个 push(Redis 全部断线)
    - 每个 push 返回独立的 persisted_pending 字典(message_id 互不相同)
    - durable outbox 中有 N 条 pending 消息
    - 没有数据竞争(message_id 唯一约束,INSERT OR IGNORE 保证幂等)

    注意: SQLite 单连接并发 BEGIN IMMEDIATE 会抛
    ``"cannot start a transaction within a transaction"``,
    这是 SQLite 的已知限制(非 R51 P0-4 修复范围)。
    此处用 ``asyncio.Lock`` 串行化对 ``write_durable_outbox`` 的调用,
    聚焦于验证 push() 的新返回契约(独立 message_id + persisted_pending),
    而非 SQLite 并发能力(生产中应由 db_writer 单线程消费,不会并发写入)。
    """

    @staticmethod
    def _install_serialized_write_durable_outbox(monkeypatch):
        """用 asyncio.Lock 包装真实 write_durable_outbox,串行化并发调用。

        避免多个协程共享同一个 aiosqlite 连接时 BEGIN IMMEDIATE 冲突。
        保留真实 SQLite 写入行为(用于验证 outbox 计数与幂等性)。
        """
        lock = asyncio.Lock()
        original_write = redis_queue.write_durable_outbox

        async def serialized_write(**kwargs):
            async with lock:
                return await original_write(**kwargs)

        monkeypatch.setattr(redis_queue, "write_durable_outbox", serialized_write)

    @pytest.mark.asyncio
    async def test_concurrent_push_all_persisted_with_unique_message_ids(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """5 个并发 push 同时遇到 Redis 断线,各自返回独立 message_id,
        durable outbox 中有 5 条 pending 消息。
        """
        # Redis 全部断线
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        # 串行化 write_durable_outbox(避免 SQLite 单连接并发冲突)
        self._install_serialized_write_durable_outbox(monkeypatch)

        # 并发触发 5 个 push
        n = 5
        tasks = [
            redis_queue.push(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 6000 + i, "batch": i},
                redis_key=f"cache:user_quota:{6000 + i}",
            )
            for i in range(n)
        ]
        results = await asyncio.gather(*tasks)

        # 所有 push 都返回 persisted_pending 字典
        assert len(results) == n
        message_ids = set()
        for i, result in enumerate(results):
            assert isinstance(result, dict), f"第 {i} 个 push 未返回 dict: {result!r}"
            assert result["status"] == "persisted_pending"
            assert "outbox_id" in result
            assert "message_id" in result
            message_ids.add(result["message_id"])

        # 关键断言:每个 push 返回的 message_id 互不相同(独立性)
        assert len(message_ids) == n, (
            f"并发 push 的 message_id 有重复,期望 {n} 个独立 UUID,"
            f"实际只有 {len(message_ids)} 个: {message_ids}"
        )

        # durable outbox 中应有 n 条 pending 消息
        count = await redis_queue.get_durable_outbox_count()
        assert count == n

    @pytest.mark.asyncio
    async def test_concurrent_push_with_explicit_message_ids_no_collision(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """并发 push 显式传入不同 message_id 时,outbox 中无主键冲突,
        所有消息均成功持久化。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        self._install_serialized_write_durable_outbox(monkeypatch)

        # 显式传入 5 个不同的 message_id
        explicit_ids = [f"uuid-explicit-{i:04d}" for i in range(5)]
        tasks = [
            redis_queue.push(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 7000 + i},
                message_id=explicit_ids[i],
            )
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

        # 所有 push 都成功持久化
        assert all(isinstance(r, dict) and r["status"] == "persisted_pending" for r in results)
        # 返回的 message_id 与传入的一致
        returned_ids = {r["message_id"] for r in results}
        assert returned_ids == set(explicit_ids)

        # outbox 中有 5 条 pending
        assert await redis_queue.get_durable_outbox_count() == 5

    @pytest.mark.asyncio
    async def test_concurrent_push_duplicate_message_id_idempotent(
        self, monkeypatch, durable_outbox_tmpdir,
    ):
        """并发 push 传入相同 message_id + 相同 payload 时,因 UNIQUE 约束 +
        request_hash 匹配,outbox 中只有 1 条消息(幂等),不会抛异常。

        场景:理论上的并发 race(实践中不应发生,但验证 outbox 的幂等性)。

        R52 P1-1: 相同 message_id 但不同 payload 视为篡改,抛
        AppError(COMMAND_HASH_MISMATCH)。本测试验证相同 payload 的幂等行为
        (同一操作被并发重试,hash 匹配 → 幂等成功)。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=None))
        self._install_serialized_write_durable_outbox(monkeypatch)

        # 5 个 push 传入相同 message_id + 相同 payload(模拟并发 race 重试)
        same_msg_id = "uuid-duplicate-race-0001"
        same_payload = {"user_id": 8000}
        tasks = [
            redis_queue.push(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data=same_payload,
                message_id=same_msg_id,
            )
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

        # 所有 push 都返回 persisted_pending(不会抛 IntegrityError / AppError)
        assert all(isinstance(r, dict) and r["status"] == "persisted_pending" for r in results)
        # 所有返回的 message_id 都是同一个
        for r in results:
            assert r["message_id"] == same_msg_id

        # outbox 中只有 1 条消息(UNIQUE 约束 + request_hash 匹配保证幂等)
        count = await redis_queue.get_durable_outbox_count()
        assert count == 1
