"""DBWriter 进程端到端集成测试。

被测模块: ``database.db_writer``(Writer 进程:BRPOP 消费 → SQLite 落盘 → DEL 缓冲)

重要说明:
- 当前 ``database.db_writer`` 尚未实现时,本文件全部用例自动跳过(``pytest.importorskip``),
  不会报错,也不会阻塞 CI。
- 用例依据 ``docs/redis_writer_design.md`` 的设计契约编写:
  * ``DBWriter`` 类含 ``_running`` / ``_process_message(msg)`` /
    ``_execute_sqlite(op_type, table, data)`` / ``_cleanup_redis_key(key)`` 等成员。
  * 消息格式与 ``redis_queue.push`` 序列化格式一致(op_type/table/method_name/
    data/redis_key/created_at)。
- ``db_writer`` 实现后若 API 与设计契约存在差异,需相应调整断言。
"""
import inspect
import json
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest

# db_writer 模块不存在时,整文件自动跳过(不报错、不阻塞)
db_writer = pytest.importorskip("database.db_writer")
DBWriter = getattr(db_writer, "DBWriter", None)

if DBWriter is None:
    # 模块存在但未定义 DBWriter 类时同样跳过
    pytest.skip("database.db_writer 未定义 DBWriter 类", allow_module_level=True)


def _make_msg(**overrides) -> dict:
    """构造一条与 redis_queue.push 序列化格式一致的消息。"""
    base = {
        "op_type": "upsert",
        "table": "user_quota",
        "method_name": "upsert_user_quota",
        "data": {"user_id": 1, "quota": 20},
        "redis_key": "cache:user_quota:1",
        "created_at": 1000.0,
    }
    base.update(overrides)
    return base


async def _maybe_await(value):
    """统一处理同步/异步方法返回值(设计契约未限定 _process_message/stop 是否为 async)。"""
    if inspect.isawaitable(value):
        return await value
    return value


@pytest.fixture
def writer(monkeypatch):
    """构造一个 DBWriter 实例,其 Redis 与 SQLite 依赖均被 mock 隔离。

    注:依据设计契约,``DBWriter()`` 构造不应建立真实连接(连接在 ``start()`` 中建立)。
    若实现版本构造函数需要参数,需调整本 fixture。
    """
    instance = DBWriter()
    # 注入模拟的 SQLite 连接与运行标志,避免触达真实 IO
    instance._db = MagicMock(name="mock_sqlite_conn")
    instance._running = True
    # Writer 内部通过 database.redis_queue 调用 pop/delete;这里仅 stub,默认不消费
    monkeypatch.setattr(
        "database.redis_queue.pop", AsyncMock(return_value=[]), raising=True
    )
    monkeypatch.setattr(
        "database.redis_queue.delete", AsyncMock(return_value=True), raising=True
    )
    return instance


class TestDBWriterIntegration:
    """DBWriter 进程端到端集成测试。"""

    @pytest.mark.asyncio
    async def test_writer_consumes_and_writes_sqlite(self, writer):
        """Writer 消费 Redis Queue 中的消息并写入 SQLite(触发 _execute_sqlite)。"""
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(return_value=None)
        writer._cleanup_redis_key = AsyncMock(return_value=None)

        await _maybe_await(writer._process_message(msg))

        writer._execute_sqlite.assert_awaited_once_with("upsert", "user_quota", msg["data"])

    @pytest.mark.asyncio
    async def test_writer_dels_redis_key_after_write(self, writer):
        """Writer 写完 SQLite 后 DEL Redis 缓冲 key(触发 _cleanup_redis_key)。"""
        msg = _make_msg(redis_key="cache:user_quota:1")
        writer._execute_sqlite = AsyncMock(return_value=None)
        writer._cleanup_redis_key = AsyncMock(return_value=None)

        await _maybe_await(writer._process_message(msg))

        # 写入后必须清除缓冲 key,避免下次读到旧数据
        writer._execute_sqlite.assert_awaited_once()
        writer._cleanup_redis_key.assert_awaited_once_with("cache:user_quota:1")

    @pytest.mark.asyncio
    async def test_writer_graceful_shutdown(self, writer):
        """Writer 收到 SIGTERM/stop 后优雅停止(_running 置 False)。"""
        assert writer._running is True
        await _maybe_await(writer.stop())
        assert writer._running is False

    @pytest.mark.asyncio
    async def test_writer_batch_consume(self, writer):
        """Writer 批量消费多条消息:每条都触发一次 _execute_sqlite。"""
        msgs = [_make_msg(method_name="m%d" % i, data={"i": i}) for i in range(3)]
        writer._execute_sqlite = AsyncMock(return_value=None)
        writer._cleanup_redis_key = AsyncMock(return_value=None)

        for m in msgs:
            await _maybe_await(writer._process_message(m))

        assert writer._execute_sqlite.await_count == 3

    @pytest.mark.asyncio
    async def test_writer_message_failure_continues(self, writer):
        """单条消息处理失败(_execute_sqlite 抛异常)不影响后续消息处理。"""
        msg_bad = _make_msg(method_name="bad", data={"i": 0})
        msg_good = _make_msg(method_name="good", data={"i": 1})
        # 第一条抛异常,第二条正常
        writer._execute_sqlite = AsyncMock(side_effect=[Exception("db error"), None])
        writer._cleanup_redis_key = AsyncMock(return_value=None)

        # 第一条失败:吞掉异常,模拟 Writer 循环的容错(继续处理下一条)
        with suppress(Exception):
            await _maybe_await(writer._process_message(msg_bad))

        # 第二条应继续被处理
        await _maybe_await(writer._process_message(msg_good))

        # 两条都尝试过 _execute_sqlite(失败 + 成功)
        assert writer._execute_sqlite.await_count == 2
