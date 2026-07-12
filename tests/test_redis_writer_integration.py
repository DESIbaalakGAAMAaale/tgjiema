"""DBWriter 进程端到端集成测试。

被测模块: ``database.db_writer``(Writer 进程:XREADGROUP 消费 → 幂等检查 → 原子事务 → XACK 确认)

R34 修复: 业务写与 writer_inbox 在同一 BEGIN IMMEDIATE...COMMIT 事务中执行,
确保崩溃恢复时不会重复执行业务写(真正的 exactly-once)。

测试契约(基于 ``database/db_writer.py`` R34 实现):
  * ``DBWriter()`` 构造不建立连接(连接在 ``init()`` 中建立)
  * ``_process_message(msg)`` 接收单个 dict 参数(或非 dict 触发死信)
  * ``_execute_atomic(msg: DBWriterMessage)`` 在单一 SQLite 事务中执行:
      begin_writer_tx → INSERT inbox → _execute_sqlite → commit_writer_tx
  * ``_execute_sqlite(msg: DBWriterMessage)`` 接收 DBWriterMessage 对象
  * 失败消息通过 ``redis_queue.push_dead`` 转入死信队列(返回 True 时才 XACK)
  * R34: inbox 在事务内 INSERT,冲突时(rowcount==0)抛 ``_InboxConflict`` → XACK 跳过
  * R34: 方法名白名单校验,未授权方法抛 ``ValueError``
  * R34: 缺少 message_id 的消息安全送入 DLQ(不再执行)
  * R34: 非 dict 消息没有 stream_id 信息,不调用 ``msg.get()``(bug 已修复)

故障注入测试(R33 P1-4 + R34 P0-1):
  * 模拟 SIGKILL 后重启:消息留在 pending,XAUTOCLAIM 回收后幂等跳过
  * 模拟 XACK 失败:消息留在 pending,下次回收后 inbox 命中跳过
  * R34: inbox INSERT 失败 → ROLLBACK → 入死信
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# db_writer 模块不存在时,整文件自动跳过(不报错、不阻塞)
db_writer = pytest.importorskip("database.db_writer")
DBWriterMessage = getattr(db_writer, "DBWriterMessage", None)
DBWriterClass = getattr(db_writer, "DBWriter", None)
_InboxConflict = getattr(db_writer, "_InboxConflict", None)

if DBWriterClass is None:
    pytest.skip("database.db_writer 未定义 DBWriter 类", allow_module_level=True)


def _make_msg(**overrides) -> dict:
    """构造一条与 redis_queue.push 序列化格式一致的消息。
    R33: 包含 message_id 和 _stream_id 字段。
    """
    base = {
        "op_type": "upsert",
        "table": "user_quota",
        "method_name": "upsert_user_quota",
        "data": {"user_id": 1, "quota": 20},
        "redis_key": "cache:user_quota:1",
        "message_id": "msg-uuid-test-001",
        "created_at": 1000.0,
        "_stream_id": "1700000000-0",
    }
    base.update(overrides)
    return base


@pytest.fixture
def writer(monkeypatch):
    """构造一个 DBWriter 实例,其 Redis 与 SQLite 依赖均被 mock 隔离。

    R34: _store mock 包含 Writer 事务方法(begin/commit/rollback_writer_tx)
    以及 _db.execute 返回 mock cursor(rowcount=1 表示 INSERT 成功)。
    """
    instance = DBWriterClass()
    instance._store = MagicMock(name="mock_store")
    instance._store.check_writer_inbox = AsyncMock(return_value=False)
    instance._store.write_writer_inbox = AsyncMock(return_value=None)
    # R34: mock Writer 事务方法
    instance._store.begin_writer_tx = AsyncMock(return_value=None)
    instance._store.commit_writer_tx = AsyncMock(return_value=None)
    instance._store.rollback_writer_tx = AsyncMock(return_value=None)
    # R34: mock _db.execute 返回 mock cursor(rowcount=1 表示 INSERT 成功)
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    instance._store._db = MagicMock()
    instance._store._db.execute = AsyncMock(return_value=mock_cursor)
    instance._running = True
    instance._processed_count = 0
    instance._error_count = 0
    instance._skipped_count = 0
    # mock redis_queue
    monkeypatch.setattr("database.redis_queue.delete", AsyncMock(return_value=True), raising=True)
    monkeypatch.setattr("database.redis_queue.push_dead", AsyncMock(return_value=True), raising=True)
    monkeypatch.setattr("database.redis_queue.ack", AsyncMock(return_value=1), raising=True)
    return instance


class TestDBWriterProcessMessage:
    """DBWriter._process_message 单元测试。

    R34: 包含幂等检查 → 原子事务(begin → INSERT inbox → _execute_sqlite → commit)
    → DEL 缓存 → XACK 确认。
    """

    @pytest.mark.asyncio
    async def test_process_message_success(self, writer):
        """成功处理消息:_execute_sqlite 被调用,事务 begin/commit 被调用,
        计数+1,XACK 确认。
        """
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        writer._execute_sqlite.assert_awaited_once()
        # 验证传入的参数类型为 DBWriterMessage
        args = writer._execute_sqlite.await_args.args
        passed_msg = args[0]
        assert hasattr(passed_msg, "method_name")
        assert passed_msg.method_name == "upsert_user_quota"
        assert passed_msg.redis_key == "cache:user_quota:1"
        assert passed_msg.message_id == "msg-uuid-test-001"
        assert passed_msg.stream_id == "1700000000-0"
        assert writer._processed_count == 1
        assert writer._error_count == 0
        # R34: 事务方法被调用
        writer._store.begin_writer_tx.assert_awaited_once()
        writer._store.commit_writer_tx.assert_awaited_once()
        writer._store.rollback_writer_tx.assert_not_awaited()
        # R34: inbox INSERT 通过 _db.execute 执行
        writer._store._db.execute.assert_awaited_once()
        # XACK 被调用
        from database import redis_queue
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_process_message_dels_redis_key(self, writer):
        """处理成功后 DEL Redis 缓冲 key(清除缓冲)。"""
        msg = _make_msg(redis_key="cache:user_quota:42")
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        from database import redis_queue
        redis_queue.delete.assert_awaited_once_with("cache:user_quota:42")

    @pytest.mark.asyncio
    async def test_process_message_no_redis_key_no_del(self, writer):
        """redis_key 为空时不调用 DEL。"""
        msg = _make_msg(redis_key="")
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        from database import redis_queue
        redis_queue.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_message_failure_to_dead_queue(self, writer):
        """处理失败(非 TypeError)→ ROLLBACK → 消息转入死信队列 + XACK(移出 pending)。"""
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(side_effect=RuntimeError("db error"))

        await writer._process_message(msg)

        assert writer._error_count == 1
        assert writer._processed_count == 0
        # R34: 失败时事务回滚
        writer._store.rollback_writer_tx.assert_awaited_once()
        # commit 未被调用(因为业务写失败)
        writer._store.commit_writer_tx.assert_not_awaited()
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        assert args[0] == msg
        assert "RuntimeError" in kwargs.get("reason", "")
        # R34 P0-2: push_dead 返回 True 时 XACK(已入死信,不需要原地重处理)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_process_message_typeerror_to_dead_queue(self, writer):
        """R34: 不在白名单的方法名 → ValueError(不是 TypeError)→ 入死信队列 + XACK。"""
        msg = _make_msg(method_name="bad_method", data={"wrong_param": 1})
        # _execute_sqlite 不应被调用(白名单校验在 _execute_atomic 开头)
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        assert writer._error_count == 1
        # 白名单校验失败,_execute_sqlite 不应被调用
        writer._execute_sqlite.assert_not_awaited()
        # 事务也不应被启动(白名单校验在 begin_writer_tx 之前)
        writer._store.begin_writer_tx.assert_not_awaited()
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        reason = kwargs.get("reason", "")
        # 验证 reason 包含 "ValueError" 或 "未授权"
        assert "ValueError" in reason or "未授权" in reason, (
            f"reason 应包含 ValueError 或 未授权,实际: {reason}"
        )
        # XACK 移出 pending(push_dead 返回 True)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_process_message_non_dict_to_dead_queue(self, writer):
        """非 dict 消息 → 直接入死信队列,不调用 _execute_sqlite。

        R34 修复: 非 dict 消息没有 stream_id 信息,不调用 msg.get()。
        """
        writer._execute_sqlite = AsyncMock(return_value=None)

        for bad_msg in ["not_a_dict", None, [1, 2, 3], 42]:
            await writer._process_message(bad_msg)

        assert writer._error_count == 4
        assert writer._processed_count == 0
        writer._execute_sqlite.assert_not_awaited()
        from database import redis_queue
        assert redis_queue.push_dead.await_count == 4
        # R34 修复后: 非 dict 消息不调用 XACK(没有 stream_id 信息)
        redis_queue.ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_message_failure_continues(self, writer):
        """单条失败不影响后续消息处理(失败+成功)。

        R34: 使用白名单内的方法名(write_heartbeat),第一条失败(RuntimeError),
        第二条成功。
        """
        msg_bad = _make_msg(method_name="write_heartbeat", message_id="bad-1")
        msg_good = _make_msg(method_name="write_heartbeat", message_id="good-1")
        writer._execute_sqlite = AsyncMock(side_effect=[RuntimeError("fail"), None])

        await writer._process_message(msg_bad)
        await writer._process_message(msg_good)

        assert writer._execute_sqlite.await_count == 2
        assert writer._error_count == 1
        assert writer._processed_count == 1


class TestDBWriterIdempotency:
    """R33/R34: writer_inbox 幂等性测试。

    场景:消息处理成功后 XACK 前崩溃 → XAUTOCLAIM 回收 → inbox 检查命中跳过。
    R34: inbox 在事务内 INSERT,冲突时(rowcount==0)抛 _InboxConflict → XACK 跳过。
    """

    @pytest.mark.asyncio
    async def test_idempotent_skip_already_processed(self, writer):
        """已处理的消息(inbox 命中)直接 XACK 跳过,不重复执行。"""
        msg = _make_msg(message_id="already-done-001")
        # inbox 检查返回 True(已处理)
        writer._store.check_writer_inbox = AsyncMock(return_value=True)
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        # 不执行 SQLite 写
        writer._execute_sqlite.assert_not_awaited()
        # 不启动 Writer 事务(在事务前就跳过)
        writer._store.begin_writer_tx.assert_not_awaited()
        # _skipped_count +1
        assert writer._skipped_count == 1
        assert writer._processed_count == 0
        # XACK 跳过
        from database import redis_queue
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_idempotent_skip_no_message_id(self, writer):
        """R34: 缺少 message_id 的消息被安全送入 DLQ(不再执行)。"""
        msg = _make_msg(message_id="")
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        # 不执行业务写
        writer._execute_sqlite.assert_not_awaited()
        # 不启动 Writer 事务
        writer._store.begin_writer_tx.assert_not_awaited()
        # _error_count +1(无效消息)
        assert writer._error_count == 1
        assert writer._processed_count == 0
        # push_dead 被调用,reason="missing message_id"
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        assert kwargs.get("reason") == "missing message_id"
        # XACK 被调用(push_dead 返回 True,且 stream_id 非空)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_idempotent_inbox_write_failure_continues(self, writer):
        """R34: inbox 在事务内写入,如果 INSERT 失败 → ROLLBACK → 入死信。"""
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(return_value=None)
        # 模拟 _db.execute(inbox INSERT) 抛异常
        writer._store._db.execute = AsyncMock(side_effect=Exception("inbox insert fail"))

        await writer._process_message(msg)

        # 业务写不应被调用(inbox INSERT 在业务写之前失败)
        writer._execute_sqlite.assert_not_awaited()
        # 事务回滚
        writer._store.rollback_writer_tx.assert_awaited_once()
        # commit 未被调用
        writer._store.commit_writer_tx.assert_not_awaited()
        # 入死信
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        assert "inbox insert fail" in kwargs.get("reason", "") or "Exception" in kwargs.get("reason", "")
        # XACK 被调用(push_dead 返回 True)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])
        # _error_count +1
        assert writer._error_count == 1
        assert writer._processed_count == 0

    @pytest.mark.asyncio
    async def test_inbox_conflict_skip(self, writer):
        """R34 新增: cursor.rowcount == 0 → _InboxConflict → XACK 跳过,
        _execute_sqlite 不被调用,_skipped_count == 1。
        """
        msg = _make_msg(message_id="conflict-001")
        # mock cursor.rowcount == 0 表示 INSERT OR IGNORE 命中冲突(已处理)
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        writer._store._db.execute = AsyncMock(return_value=mock_cursor)
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        # 业务写不应被调用(inbox 冲突)
        writer._execute_sqlite.assert_not_awaited()
        # 事务回滚(冲突时需要 ROLLBACK)
        writer._store.rollback_writer_tx.assert_awaited_once()
        # commit 未被调用
        writer._store.commit_writer_tx.assert_not_awaited()
        # _skipped_count +1
        assert writer._skipped_count == 1
        assert writer._processed_count == 0
        # 不入死信(冲突是幂等跳过,不是错误)
        from database import redis_queue
        redis_queue.push_dead.assert_not_awaited()
        # XACK 被调用(消息从 pending 移除)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])


class TestDBWriterCrashRecovery:
    """R33/R34: 故障注入测试 — 崩溃恢复场景。

    模拟 SIGKILL 崩溃后重启,验证:
    1. 消息留在 pending(Streams 特性,不丢失)
    2. XAUTOCLAIM 回收 pending 消息
    3. inbox 检查命中,已处理的消息 XACK 跳过(exactly-once)
    4. 未处理的消息正常执行
    """
    @pytest.mark.asyncio
    async def test_crash_after_sqlite_before_xack(self, writer):
        """模拟:SQLite 写成功 + inbox 写成功(事务 COMMIT) → XACK 前崩溃。

        恢复后 XAUTOCLAIM 回收该消息,inbox 检查命中 → XACK 跳过(不重复执行)。
        """
        msg = _make_msg(message_id="crash-test-001")
        # 第一次处理:SQLite 写成功 + 事务 COMMIT 成功
        writer._execute_sqlite = AsyncMock(return_value=None)
        # 模拟 XACK 前崩溃:ack 抛异常(但 _safe_ack 会捕获,不传播)
        from database import redis_queue
        redis_queue.ack = AsyncMock(side_effect=Exception("process killed before ack"))

        await writer._process_message(msg)
        # 数据已写入(事务已 COMMIT)
        assert writer._processed_count == 1
        # R34: 事务已提交
        writer._store.commit_writer_tx.assert_awaited_once()

        # 恢复后:XAUTOCLAIM 回收该消息
        # 重新处理时 inbox 检查应返回 True
        redis_queue.ack = AsyncMock(return_value=1)
        writer._store.check_writer_inbox = AsyncMock(return_value=True)
        # 重置计数
        writer._processed_count = 0
        writer._skipped_count = 0

        await writer._process_message(msg)

        # inbox 命中,不重复执行 SQLite 写
        writer._execute_sqlite.assert_awaited_once()  # 只在第一次调用
        assert writer._processed_count == 0
        assert writer._skipped_count == 1
        # XACK 成功(消息从 pending 移除)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_crash_before_inbox_write(self, writer):
        """R34 重写: inbox 在业务写之前写入(同一事务)。

        模拟: begin_writer_tx 成功但 _db.execute(inbox INSERT) 抛异常
        → ROLLBACK → 入死信。
        """
        msg = _make_msg(message_id="crash-test-002")
        # 模拟 inbox INSERT 失败
        writer._store._db.execute = AsyncMock(side_effect=Exception("crashed before inbox write"))
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        # 业务写不应被调用(inbox INSERT 在业务写之前失败)
        writer._execute_sqlite.assert_not_awaited()
        # 事务已回滚
        writer._store.rollback_writer_tx.assert_awaited_once()
        # commit 未被调用
        writer._store.commit_writer_tx.assert_not_awaited()
        # 入死信
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        # XACK 被调用(push_dead 返回 True)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])
        # _error_count +1
        assert writer._error_count == 1
        assert writer._processed_count == 0

    @pytest.mark.asyncio
    async def test_message_stays_in_pending_on_crash(self, mock_redis, monkeypatch):
        """R33 P0 核心验证:消息进入 pending 后,即使 db_writer 崩溃也不丢失。

        XREADGROUP 消费的消息进入 pending 列表,不会被删除。
        只有 XACK 后才从 pending 移除。崩溃后消息仍在 pending。
        """
        from database import redis_queue

        msg = {
            "method_name": "write_heartbeat",
            "data": {"slot_id": 1},
            "message_id": "crash-003",
            "created_at": 1.0,
        }
        # XREADGROUP 返回消息(进入 pending)
        mock_redis.xreadgroup = AsyncMock(return_value=[
            ("stream", [("1700000000-0", {"data": json.dumps(msg)})])
        ])
        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        # XACK 未被调用(模拟崩溃)
        mock_redis.xack = AsyncMock(return_value=0)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 消费消息(进入 pending)
        result = await redis_queue.pop(timeout=1, count=1)
        assert len(result) == 1
        assert result[0]["_stream_id"] == "1700000000-0"

        # 模拟崩溃:不调用 ack()
        # 验证消息仍在 pending(通过 XPENDING 查询)
        mock_redis.xpending = AsyncMock(return_value=(1, "1700000000-0", "1700000000-0", []))
        pending_info = await redis_queue.get_pending_info()
        assert pending_info["total"] == 1  # 消息仍在 pending

    @pytest.mark.asyncio
    async def test_xautoclaim_recovers_pending_after_crash(self, mock_redis, monkeypatch):
        """R33 P0: XAUTOCLAIM 回收崩溃遗留的 pending 消息。

        场景:db_writer 崩溃后重启,pop() 优先 XAUTOCLAIM 回收 pending 消息。
        """
        from database import redis_queue

        pending_msg = {
            "method_name": "write_heartbeat",
            "data": {"slot_id": 42},
            "message_id": "pending-recover-001",
            "created_at": 1.0,
        }
        # XAUTOCLAIM 返回回收的 pending 消息
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
        assert result[0]["message_id"] == "pending-recover-001"
        # XREADGROUP 未被调用(回收的消息已满足 count)
        mock_redis.xreadgroup.assert_not_awaited()


class TestDBWriterStop:
    """DBWriter.stop 优雅停止测试。"""

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, writer):
        """stop() 设置 _running=False。"""
        assert writer._running is True
        await writer.stop()
        assert writer._running is False

    @pytest.mark.asyncio
    async def test_stop_logs_counts(self, writer):
        """stop() 记录已处理/失败/跳过的消息计数。"""
        writer._processed_count = 42
        writer._error_count = 3
        writer._skipped_count = 5
        await writer.stop()
        # stop 不抛异常即可(日志通过 loguru 输出)

    # ── P0回归: DEL 失败不导致已成功写入的消息入死信队列 ──

    @pytest.mark.asyncio
    async def test_process_message_del_failure_no_dead_queue(self, writer):
        """P0回归: SQLite 写成功后 DEL 失败 → 仅记录 WARNING,不入死信队列。

        R34: 事务已 COMMIT,DEL 失败只是缓存未清除,不影响已落盘数据。
        R34: 仍执行 XACK(数据已落盘,下次读会回填)。
        """
        msg = _make_msg(redis_key="cache:user_quota:42")
        writer._execute_sqlite = AsyncMock(return_value=None)
        from database import redis_queue
        # mock DEL 抛异常
        original_delete = redis_queue.delete
        redis_queue.delete = AsyncMock(side_effect=Exception("redis delete failed"))

        try:
            await writer._process_message(msg)
        finally:
            redis_queue.delete = original_delete

        # SQLite 写成功 → processed_count +1
        assert writer._processed_count == 1
        # DEL 失败 → error_count 不增加
        assert writer._error_count == 0
        # 事务已 COMMIT
        writer._store.commit_writer_tx.assert_awaited_once()
        # 不入死信队列(避免重放时重复写入)
        redis_queue.push_dead.assert_not_awaited()
        # R34: 仍 XACK(数据已落盘)
        redis_queue.ack.assert_awaited_once_with(["1700000000-0"])

    @pytest.mark.asyncio
    async def test_xack_failure_does_not_rollback_sqlite(self, writer):
        """R33/R34 P0: XACK 失败不回滚 SQLite(数据已落盘)。

        XACK 失败时消息留在 pending,下次 XAUTOCLAIM 回收后
        inbox 检查命中 → XACK 跳过(不会重复执行)。
        """
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(return_value=None)
        from database import redis_queue
        redis_queue.ack = AsyncMock(side_effect=Exception("xack failed"))

        await writer._process_message(msg)

        # SQLite 写成功(不回滚)
        assert writer._processed_count == 1
        # R34: 事务已 COMMIT(不回滚)
        writer._store.commit_writer_tx.assert_awaited_once()
        writer._store.rollback_writer_tx.assert_not_awaited()
        # 消息留在 pending(下次回收处理)


# ── DBWriter.init / close / start 测试 ──

class TestDBWriterInit:
    """DBWriter.init 初始化测试。
    R33: init 包含 ensure_consumer_group() 调用。
    """

    @pytest.mark.asyncio
    async def test_init_sqlite_mode_exits(self, mock_settings, monkeypatch):
        """init 在 WRITER_MODE=sqlite 时优雅退出(sys.exit 0)。"""
        instance = DBWriterClass()
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "sqlite")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost")
        with pytest.raises(SystemExit) as exc_info:
            await instance.init()
        assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_init_redis_empty_exits(self, mock_settings, monkeypatch):
        """init 在 REDIS_URL 为空时优雅退出(sys.exit 0)。"""
        instance = DBWriterClass()
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "")
        with pytest.raises(SystemExit) as exc_info:
            await instance.init()
        assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_init_redis_unavailable_raises_runtimeerror(self, mock_settings, monkeypatch):
        """init 在 Redis 不可达时抛 RuntimeError(供 run_db_writer 捕获后 exit 1)。"""
        instance = DBWriterClass()
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost")
        monkeypatch.setattr("database.redis_queue.health_check", AsyncMock(return_value=False))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
        with pytest.raises(RuntimeError, match="Redis 不可达"):
            await instance.init()

    @pytest.mark.asyncio
    async def test_init_consumer_group_failure_raises(self, mock_settings, monkeypatch):
        """R33: Consumer Group 创建失败时抛 RuntimeError。"""
        instance = DBWriterClass()
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost")
        monkeypatch.setattr("database.redis_queue.health_check", AsyncMock(return_value=True))
        monkeypatch.setattr("database.redis_queue.ensure_consumer_group", AsyncMock(return_value=False))
        with pytest.raises(RuntimeError, match="Consumer Group 创建失败"):
            await instance.init()

    @pytest.mark.asyncio
    async def test_init_success(self, mock_settings, monkeypatch):
        """init 在 Redis 可达 + Consumer Group 创建 + CacheStore 初始化成功后正常返回。"""
        instance = DBWriterClass()
        monkeypatch.setattr(mock_settings, "WRITER_MODE", "redis")
        monkeypatch.setattr(mock_settings, "REDIS_URL", "redis://localhost")
        monkeypatch.setattr("database.redis_queue.health_check", AsyncMock(return_value=True))
        monkeypatch.setattr("database.redis_queue.ensure_consumer_group", AsyncMock(return_value=True))
        mock_store = MagicMock()
        mock_store.init = AsyncMock(return_value=None)
        monkeypatch.setattr("database.db_writer.CacheStore", MagicMock(return_value=mock_store))
        await instance.init()
        assert instance._store is mock_store


class TestDBWriterClose:
    """DBWriter.close 资源清理测试。"""

    @pytest.mark.asyncio
    async def test_close_with_store(self, monkeypatch):
        """close 在 _store 存在时调用其 close 并清理。"""
        instance = DBWriterClass()
        mock_store = MagicMock()
        mock_store.close = AsyncMock(return_value=None)
        instance._store = mock_store
        monkeypatch.setattr("database.redis_queue.close_redis", AsyncMock(return_value=None))
        await instance.close()
        mock_store.close.assert_awaited_once()
        assert instance._store is None

    @pytest.mark.asyncio
    async def test_close_without_store(self, monkeypatch):
        """close 在 _store 为 None 时只关闭 Redis 连接。"""
        instance = DBWriterClass()
        instance._store = None
        mock_close_redis = AsyncMock(return_value=None)
        monkeypatch.setattr("database.redis_queue.close_redis", mock_close_redis)
        await instance.close()
        mock_close_redis.assert_awaited_once()


class TestDBWriterStart:
    """DBWriter.start 消费循环测试。
    R33: 使用 XREADGROUP 消费(通过 redis_queue.pop)。
    """

    @pytest.mark.asyncio
    async def test_start_processes_messages_then_stops(self, monkeypatch):
        """start 处理消息后收到 CancelledError 优雅停止。"""
        instance = DBWriterClass()
        instance._store = MagicMock()
        instance._store.check_writer_inbox = AsyncMock(return_value=False)
        instance._process_message = AsyncMock(return_value=None)

        msg = _make_msg()
        call_count = [0]
        async def mock_pop(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg]
            raise asyncio.CancelledError()

        monkeypatch.setattr("database.redis_queue.pop", mock_pop)

        with pytest.raises(asyncio.CancelledError):
            await instance.start()

        instance._process_message.assert_awaited_once_with(msg)
        assert instance._running is False

    @pytest.mark.asyncio
    async def test_start_empty_messages_sleep_and_continue(self, monkeypatch):
        """start 收到空消息时 sleep 后继续(防止忙等),最后被 CancelledError 停止。"""
        instance = DBWriterClass()
        instance._store = MagicMock()

        call_count = [0]
        async def mock_pop(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return []
            raise asyncio.CancelledError()

        monkeypatch.setattr("database.redis_queue.pop", mock_pop)
        sleep_called = [False]
        original_sleep = asyncio.sleep
        async def mock_sleep(seconds):
            sleep_called[0] = True
            await original_sleep(0)
        monkeypatch.setattr("asyncio.sleep", mock_sleep)

        with pytest.raises(asyncio.CancelledError):
            await instance.start()

        assert sleep_called[0] is True
