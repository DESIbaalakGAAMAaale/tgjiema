"""R35 Batch 2: P1 Writer/DLQ 修复测试。

被测模块:
- ``database.redis_queue`` — push() / push_dead() 的 attempts 持久传递
- ``database.dlq_worker`` — _retry_message 携带 attempts 回主 Stream
- ``database.cache_store`` — writer_transaction 上下文管理器、begin_writer_tx 异常安全性

P1 修复对应:
- P1-1: DLQ attempts 重置修复(push 携带 attempts,push_dead 从 msg 读取 +1,
  permanent=True 标记永久死信,DLQWorker 携带 attempts 回主 Stream)
- P1-2: monkey-patch commit 异常安全性(begin_writer_tx BEGIN IMMEDIATE 失败时恢复 commit,
  writer_transaction 上下文管理器自动 ROLLBACK)
- P1-3: writer_inbox 保留期(由 settings.py 默认值测试,此处不重复)

测试策略:
- redis_queue / dlq_worker: 使用 AsyncMock 模拟 Redis 客户端与函数依赖。
- cache_store: 使用真实临时 SQLite 文件(与 test_m1_cache_store_tables.py 一致)。
- 不依赖真实 Redis,所有测试在本地可运行。
"""
import inspect
import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from database import redis_queue


# ───────────────────────── P1-1: push() 携带 attempts ─────────────────────────


class TestPushCarriesAttempts:
    """R35 P1-1: push() 把 attempts 字段写入消息体。"""

    @pytest.mark.asyncio
    async def test_push_writes_attempts_zero(self, mock_redis, monkeypatch):
        """attempts=0(默认):消息体包含 attempts:0。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota", data={"user_id": 1},
        )
        assert ok is True
        args = mock_redis.xadd.await_args.args
        msg = json.loads(args[1]["data"])
        assert msg["attempts"] == 0

    @pytest.mark.asyncio
    async def test_push_writes_attempts_nonzero(self, mock_redis, monkeypatch):
        """attempts=2(DLQ 重试回主 Stream):消息体携带 attempts:2。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        ok = await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota", data={"user_id": 1},
            attempts=2,
        )
        assert ok is True
        args = mock_redis.xadd.await_args.args
        msg = json.loads(args[1]["data"])
        # 关键断言: attempts 被持久化到消息体
        assert msg["attempts"] == 2


# ───────────────────────── P1-1: push_dead 读取 attempts ─────────────────────────


class TestPushDeadReadsAttempts:
    """R35 P1-1: push_dead 从 msg 中读取 existing attempts 并 +1。

    验证向后兼容:
    - 显式 attempts > 0:使用显式值(向后兼容旧调用)
    - attempts=0(默认):从 msg.attempts 读取并 +1
    - permanent=True:直接设为 max_attempts + next_retry_at=None
    """

    @pytest.mark.asyncio
    async def test_push_dead_increments_from_msg_attempts(self, mock_redis, monkeypatch):
        """msg 中含 attempts=2,push_dead(attempts=0) → dead_msg.attempts=3(递增)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # 模拟从 DLQ 重试回主 Stream 的消息(已被 push 写入 attempts=2)
        msg = {"op_type": "upsert", "attempts": 2, "method_name": "upsert_user_quota"}
        ok = await redis_queue.push_dead(msg, reason="retry failed", attempts=0)
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # 关键: attempts 从 2 递增到 3(而非重置为 0)
        assert dead_msg["attempts"] == 3
        # 3 >= max_attempts(3) → 永久死信
        assert dead_msg["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_push_dead_explicit_attempts_takes_priority(self, mock_redis, monkeypatch):
        """显式 attempts > 0 时优先使用显式值(向后兼容)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # msg 中有 attempts=5,但显式传入 attempts=1
        msg = {"op_type": "upsert", "attempts": 5}
        ok = await redis_queue.push_dead(msg, reason="explicit", attempts=1)
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # 显式 attempts=1 优先(向后兼容旧调用)
        assert dead_msg["attempts"] == 1
        # 1 < max_attempts(3) → 可重试
        assert dead_msg["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_push_dead_no_attempts_in_msg(self, mock_redis, monkeypatch):
        """msg 中无 attempts 字段(旧消息格式):默认从 0 递增到 1。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        msg = {"op_type": "upsert"}  # 无 attempts 字段
        ok = await redis_queue.push_dead(msg, reason="first failure")
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # 默认 +1: 0 → 1
        assert dead_msg["attempts"] == 1
        assert dead_msg["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_push_dead_permanent_flag(self, mock_redis, monkeypatch):
        """permanent=True: 直接设为 max_attempts,next_retry_at=None(永久死信)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        # msg 中 attempts=0,但 permanent=True
        msg = {"op_type": "upsert", "attempts": 0}
        ok = await redis_queue.push_dead(
            msg, reason="TypeError: bad signature", permanent=True,
        )
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # permanent → attempts 设为 max_attempts(3)
        assert dead_msg["attempts"] == 3
        assert dead_msg["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_push_dead_permanent_overrides_explicit_attempts(self, mock_redis, monkeypatch):
        """permanent=True 优先于显式 attempts(永久死信永远不重试)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        msg = {"op_type": "upsert"}
        # 显式 attempts=1 但 permanent=True
        ok = await redis_queue.push_dead(
            msg, reason="TypeError", attempts=1, permanent=True,
        )
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # permanent 优先: attempts = max_attempts(3)
        assert dead_msg["attempts"] == 3
        assert dead_msg["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_push_dead_at_threshold_not_permanent(self, mock_redis, monkeypatch):
        """attempts=2(未达 max=3):next_retry_at 不为 None(仍可重试一次)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        msg = {"op_type": "upsert", "attempts": 1}  # existing=1
        ok = await redis_queue.push_dead(msg, reason="retry", attempts=0)
        assert ok is True
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # existing=1 → +1 → 2,2 < 3 → 仍可重试
        assert dead_msg["attempts"] == 2
        assert dead_msg["next_retry_at"] is not None


# ───────────────────────── P1-1: DLQWorker 携带 attempts ─────────────────────────


class TestDLQWorkerCarriesAttempts:
    """R35 P1-1: DLQWorker._retry_message 调用 push() 时携带 attempts=new_attempts。"""

    @pytest.mark.asyncio
    async def test_retry_message_carries_attempts(self, monkeypatch):
        """重试消息时 push() 收到 attempts=new_attempts(dead_msg.attempts + 1)。"""
        from database import dlq_worker

        worker = dlq_worker.DLQWorker()
        msg_id = "1700000000-0"
        # dead_msg.attempts=1, max_attempts=3 → new_attempts=2
        dead_msg = {
            "original": {
                "op_type": "upsert", "table": "user_quota",
                "method_name": "upsert_user_quota",
                "data": {"user_id": 1}, "redis_key": "",
                "message_id": "uuid-1", "created_at": 1000.0,
            },
            "reason": "first failure",
            "message_id": "uuid-1",
            "attempts": 1,
            "max_attempts": 3,
            "failed_at": 1000.0,
            "next_retry_at": time.time() - 10,  # 已到期
        }
        monkeypatch.setattr(
            redis_queue, "get_dead_messages",
            AsyncMock(return_value=[(msg_id, dead_msg)]),
        )
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)
        monkeypatch.setattr(
            redis_queue, "delete_dead_message",
            AsyncMock(return_value=True),
        )

        retried = await worker._process_dead_messages()

        assert retried == 1
        assert worker.retried_count == 1
        # 关键断言: push 被调用时携带 attempts=2(dead_msg.attempts + 1)
        mock_push.assert_awaited_once()
        _, kwargs = mock_push.await_args
        assert kwargs["attempts"] == 2

    @pytest.mark.asyncio
    async def test_retry_message_at_max_attempts_not_retried(self, monkeypatch):
        """attempts >= max_attempts 的消息不会被重试(不调用 push)。"""
        from database import dlq_worker

        worker = dlq_worker.DLQWorker()
        msg_id = "1700000001-0"
        # attempts=3 已达 max_attempts,不应重试
        dead_msg = {
            "original": {"op_type": "upsert", "method_name": "upsert_user_quota"},
            "reason": "permanent",
            "message_id": "uuid-2",
            "attempts": 3,
            "max_attempts": 3,
            "failed_at": 1000.0,
            "next_retry_at": None,  # 永久死信
        }
        monkeypatch.setattr(
            redis_queue, "get_dead_messages",
            AsyncMock(return_value=[(msg_id, dead_msg)]),
        )
        mock_push = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push", mock_push)

        retried = await worker._process_dead_messages()

        assert retried == 0
        assert worker.retried_count == 0
        assert worker.permanent_fail_count == 1
        mock_push.assert_not_awaited()


# ───────────────────────── P1-2: writer_transaction 上下文管理器 ─────────────────────────


# 检查 CacheStore 是否为真实类(conftest 在 aiosqlite 缺失时注入 MagicMock)
from database import cache_store as _cs_module_check

_CACHE_STORE_AVAILABLE = inspect.isclass(_cs_module_check.CacheStore)


@pytest.mark.skipif(
    not _CACHE_STORE_AVAILABLE,
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestWriterTransaction:
    """R35 P1-2: writer_transaction() 异步上下文管理器。

    验证:
    - 正常退出时 COMMIT
    - 异常退出时 ROLLBACK
    - 上下文内 _in_writer_tx=True
    - 退出后 _in_writer_tx=False
    """

    @pytest_asyncio.fixture
    async def store(self):
        """创建临时文件数据库的 CacheStore 实例。"""
        from database import cache_store as cs_module
        tmpdir = tempfile.mkdtemp(prefix="r35_p1_2_test_")
        db_path = Path(tmpdir) / "test_cache.db"
        original_path = cs_module.DB_PATH
        cs_module.DB_PATH = db_path
        try:
            s = cs_module.CacheStore()
            await s.init()
            yield s
            await s.close()
        finally:
            cs_module.DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_writer_transaction_commit_on_success(self, store):
        """正常退出:COMMIT + _in_writer_tx 复位为 False。"""
        # 在事务内执行一条 INSERT(用 writer_inbox 表测试)
        async with store.writer_transaction():
            # 事务中 _in_writer_tx 应为 True
            assert store._in_writer_tx is True
            await store._db.execute(
                "INSERT OR IGNORE INTO writer_inbox "
                "(message_id, method_name, stream_id, created_at, processed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test-commit-001", "test_method", "test-stream", time.time(), time.time()),
            )
        # 退出后 _in_writer_tx 应为 False(commit 已恢复)
        assert store._in_writer_tx is False

        # 验证数据已提交(开新事务能读到)
        cursor = await store._db.execute(
            "SELECT message_id FROM writer_inbox WHERE message_id = ?",
            ("test-commit-001",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "test-commit-001"

    @pytest.mark.asyncio
    async def test_writer_transaction_rollback_on_exception(self, store):
        """异常退出:ROLLBACK + _in_writer_tx 复位为 False + 数据未提交。"""
        with pytest.raises(RuntimeError, match="test rollback"):
            async with store.writer_transaction():
                assert store._in_writer_tx is True
                await store._db.execute(
                    "INSERT OR IGNORE INTO writer_inbox "
                    "(message_id, method_name, stream_id, created_at, processed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("test-rollback-001", "test_method", "test-stream",
                     time.time(), time.time()),
                )
                # 抛异常触发 ROLLBACK
                raise RuntimeError("test rollback")

        # 退出后 _in_writer_tx 应为 False(rollback 已恢复)
        assert store._in_writer_tx is False

        # 验证数据已回滚(查不到)
        cursor = await store._db.execute(
            "SELECT message_id FROM writer_inbox WHERE message_id = ?",
            ("test-rollback-001",),
        )
        row = await cursor.fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_writer_transaction_commit_method_restored(self, store):
        """退出后 commit 方法恢复正常(不再是 no-op)。"""
        original_commit = store._db.commit
        async with store.writer_transaction():
            # 事务中 commit 已被替换为 no-op
            # 使用 != 而非 is not: bound method 每次访问创建新对象
            assert store._db.commit != original_commit
        # 退出后 commit 应恢复为原始方法
        # 使用 == 而非 is: bound method 每次访问创建新对象,is 比较总为 False
        assert store._db.commit == original_commit

    @pytest.mark.asyncio
    async def test_writer_transaction_rollback_restores_commit(self, store):
        """异常 ROLLBACK 后 commit 方法也恢复正常。"""
        original_commit = store._db.commit
        with pytest.raises(RuntimeError):
            async with store.writer_transaction():
                # 使用 != 而非 is not: bound method 每次访问创建新对象
                assert store._db.commit != original_commit
                raise RuntimeError("trigger rollback")
        # ROLLBACK 后 commit 应恢复
        # 使用 == 而非 is: bound method 每次访问创建新对象,is 比较总为 False
        assert store._db.commit == original_commit


# ───────────────────────── P1-2: begin_writer_tx 异常安全性 ─────────────────────────


@pytest.mark.skipif(
    not _CACHE_STORE_AVAILABLE,
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestBeginWriterTxExceptionSafety:
    """R35 P1-2: begin_writer_tx 在 BEGIN IMMEDIATE 失败时恢复 commit 方法。

    旧实现: BEGIN IMMEDIATE 失败时 commit 仍为 no-op,后续调用静默丢数据。
    新实现: try/except 捕获 BEGIN 异常,恢复 commit + 重置 _in_writer_tx。
    """

    @pytest_asyncio.fixture
    async def store(self):
        """创建临时文件数据库的 CacheStore 实例。"""
        from database import cache_store as cs_module
        tmpdir = tempfile.mkdtemp(prefix="r35_p1_2_begin_")
        db_path = Path(tmpdir) / "test_cache.db"
        original_path = cs_module.DB_PATH
        cs_module.DB_PATH = db_path
        try:
            s = cs_module.CacheStore()
            await s.init()
            yield s
            await s.close()
        finally:
            cs_module.DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_begin_writer_tx_restores_commit_on_failure(self, store):
        """BEGIN IMMEDIATE 失败(模拟异常):commit 方法恢复 + _in_writer_tx=False。"""
        original_commit = store._db.commit

        # 模拟 BEGIN IMMEDIATE 抛异常(如数据库锁超时)
        original_execute = store._db.execute

        async def _fail_on_begin(sql, *args, **kwargs):
            if "BEGIN" in sql.upper():
                raise RuntimeError("database is locked")
            return await original_execute(sql, *args, **kwargs)

        store._db.execute = _fail_on_begin

        # begin_writer_tx 应抛异常(不静默吞)
        with pytest.raises(RuntimeError, match="database is locked"):
            await store.begin_writer_tx()

        # 关键断言: 异常后 commit 方法应已恢复(不是 no-op)
        # 使用 == 而非 is: bound method 每次访问创建新对象,is 比较总为 False
        assert store._db.commit == original_commit
        # _in_writer_tx 应已重置为 False
        assert store._in_writer_tx is False

    @pytest.mark.asyncio
    async def test_begin_writer_tx_normal_case(self, store):
        """正常情况下:BEGIN IMMEDIATE 成功,commit 被替换为 no-op,_in_writer_tx=True。"""
        original_commit = store._db.commit
        try:
            await store.begin_writer_tx()
            # 事务中: _in_writer_tx=True,commit 被替换
            assert store._in_writer_tx is True
            # 使用 != 而非 is not: bound method 每次访问创建新对象
            assert store._db.commit != original_commit
        finally:
            # 清理: 手动 ROLLBACK(避免影响后续测试)
            await store.rollback_writer_tx()
        # ROLLBACK 后: _in_writer_tx=False,commit 恢复
        assert store._in_writer_tx is False
        # 使用 == 而非 is: bound method 每次访问创建新对象,is 比较总为 False
        assert store._db.commit == original_commit


# ───────────────────────── P1-2: WriterCommand Protocol ─────────────────────────


@pytest.mark.skipif(
    not _CACHE_STORE_AVAILABLE,
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestWriterCommandProtocol:
    """R35 P1-2: WriterCommand Protocol 类型定义(纯类型提示,不强制实现)。"""

    def test_writer_command_protocol_exists(self):
        """WriterCommand Protocol 已定义在 cache_store 模块。"""
        from database import cache_store as cs_module
        assert hasattr(cs_module, "WriterCommand")
        assert inspect.isclass(cs_module.WriterCommand)

    def test_writer_command_protocol_runtime_checkable(self):
        """WriterCommand 是 runtime_checkable Protocol(可用 isinstance 检查)。"""
        from database import cache_store as cs_module

        # 任意带 execute 方法的对象都应被识别为实现 WriterCommand
        class FakeCommand:
            async def execute(self, conn, *args, **kwargs):
                return None

        # runtime_checkable Protocol 支持 isinstance 检查(只检查方法存在性,不检查签名)
        assert isinstance(FakeCommand(), cs_module.WriterCommand)

    def test_writer_command_protocol_no_method_not_instance(self):
        """没有 execute 方法的对象不被识别为 WriterCommand。"""
        from database import cache_store as cs_module

        class NoExecute:
            pass

        # runtime_checkable Protocol: 没有 execute 方法 → 不是实例
        assert not isinstance(NoExecute(), cs_module.WriterCommand)


# ───────────────────────── P1-3: writer_inbox 保留期配置 ─────────────────────────


class TestWriterInboxRetentionConfig:
    """R35 P1-3: writer_inbox 保留期从 168(7天)改为 2160(90天)。

    此测试验证 settings.py 源码中的默认值,而非运行时行为
    (测试环境 conftest 注入 MagicMock 作为 settings,无法读取真实默认值)。
    """

    def test_default_retention_is_2160_hours(self):
        """settings.py 中 WRITER_INBOX_RETENTION_HOURS 默认值为 2160(90天)。

        通过读取源文件验证,兼容测试环境的 MagicMock settings 注入。
        """
        import re
        # 读取 settings.py 源文件(不依赖 config 模块加载)
        settings_file = Path(__file__).parent.parent / "config" / "settings.py"
        source = settings_file.read_text(encoding="utf-8")
        # 匹配 "WRITER_INBOX_RETENTION_HOURS: int = <number>"
        match = re.search(
            r"WRITER_INBOX_RETENTION_HOURS\s*:\s*int\s*=\s*(\d+)",
            source,
        )
        assert match, (
            f"未在 {settings_file} 中找到 WRITER_INBOX_RETENTION_HOURS 字段定义"
        )
        value = int(match.group(1))
        assert value == 2160, (
            f"WRITER_INBOX_RETENTION_HOURS 应为 2160(90天),"
            f"实际: {value}"
        )

    def test_retention_covers_90_days(self):
        """2160 小时 = 90 天,覆盖 Stream/DLQ/停机/人工处理窗口。"""
        hours = 2160
        days = hours / 24
        assert days == 90
        # 必须远大于 7 天(原值),确保覆盖长停机 + 人工处理窗口
        assert hours > 168


# ───────────────────────── P1-1: 端到端 attempts 持久传递链 ─────────────────────────


class TestAttemptsEndToEndChain:
    """R35 P1-1: attempts 在 主 Stream → DLQ → 主 Stream 链路中持久传递。

    验证完整重试闭环:
    1. 消息首次入主 Stream(attempts=0)
    2. db_writer 处理失败 → push_dead 从 msg 读取 attempts(0)+1=1
    3. DLQWorker 重试 → push 回主 Stream 携带 attempts=2(dead_msg.attempts+1)
    4. db_writer 再次失败 → push_dead 从 msg 读取 attempts(2)+1=3
    5. attempts=3 >= max_attempts(3) → 永久死信
    """

    @pytest.mark.asyncio
    async def test_full_retry_chain_attempts_increments(self, mock_redis, monkeypatch):
        """完整重试链: attempts 0 → 1 → 2 → 3(永久死信)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))
        max_attempts = 3  # 与 conftest mock_settings 一致

        # 步骤1: 首次失败(msg.attempts=0) → dead_msg.attempts=1
        msg_first_fail = {"op_type": "upsert", "attempts": 0, "method_name": "test"}
        await redis_queue.push_dead(msg_first_fail, reason="first fail")
        args = mock_redis.xadd.await_args_list[-1].args
        dead_msg_1 = json.loads(args[1]["data"])
        assert dead_msg_1["attempts"] == 1
        assert dead_msg_1["next_retry_at"] is not None  # 1 < 3,可重试

        # 步骤2: DLQWorker 重试 → push 回主 Stream 携带 attempts=dead_msg_1.attempts+1=2
        await redis_queue.push(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota", data={},
            attempts=dead_msg_1["attempts"] + 1,  # =2
        )
        args = mock_redis.xadd.await_args_list[-1].args
        main_msg = json.loads(args[1]["data"])
        assert main_msg["attempts"] == 2  # 主 Stream 消息携带 attempts=2

        # 步骤3: db_writer 再次失败 → push_dead 从 msg.attempts(2)读取 +1=3
        await redis_queue.push_dead(main_msg, reason="second fail")
        args = mock_redis.xadd.await_args_list[-1].args
        dead_msg_2 = json.loads(args[1]["data"])
        assert dead_msg_2["attempts"] == 3  # 2 + 1 = 3
        # 3 >= max_attempts(3) → 永久死信
        assert dead_msg_2["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_permanent_flag_short_circuits_chain(self, mock_redis, monkeypatch):
        """permanent=True 立即终重试链(TypeError 等不可重试错误)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # msg.attempts=0,但 permanent=True → 直接永久死信
        msg = {"op_type": "upsert", "attempts": 0, "method_name": "test"}
        await redis_queue.push_dead(
            msg, reason="TypeError: bad signature", permanent=True,
        )
        args = mock_redis.xadd.await_args.args
        dead_msg = json.loads(args[1]["data"])
        # attempts = max_attempts(3),next_retry_at = None
        assert dead_msg["attempts"] == 3
        assert dead_msg["next_retry_at"] is None
