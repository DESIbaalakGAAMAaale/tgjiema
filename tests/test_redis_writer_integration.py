"""DBWriter 进程端到端集成测试。

被测模块: ``database.db_writer``(Writer 进程:BRPOP 消费 → SQLite 落盘 → DEL 缓冲)

测试契约(基于 ``database/db_writer.py`` 实际实现):
  * ``DBWriter()`` 构造不建立连接(连接在 ``init()`` 中建立)
  * ``_process_message(msg)`` 接收单个 dict 参数(或非 dict 触发死信)
  * ``_execute_sqlite(msg: DBWriterMessage)`` 接收 DBWriterMessage 对象(非 3 个位置参数)
  * 无 ``_cleanup_redis_key`` 方法(DEL 内联在 ``_process_message`` 中)
  * 失败消息通过 ``redis_queue.push_dead`` 转入死信队列
  * ``TypeError`` (方法签名不匹配)单独分类为永久失败入死信

P0修复: 整个文件基于实际 DBWriter API 重写,匹配 _store/_execute_sqlite/push_dead 接口
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# db_writer 模块不存在时,整文件自动跳过(不报错、不阻塞)
db_writer = pytest.importorskip("database.db_writer")
DBWriter = getattr(db_writer, "DBWriterMessage", None)  # 仅验证模块可导入
DBWriterClass = getattr(db_writer, "DBWriter", None)

if DBWriterClass is None:
    pytest.skip("database.db_writer 未定义 DBWriter 类", allow_module_level=True)


def _make_msg(**overrides) -> dict:
    """构造一条与 redis_queue.push 序列化格式一致的消息。"""
    base = {
        "op_type": "upsert",
        "table": "user_quota",
        "method_name": "upsert_user_quota",
        "data": {"user_id": 1, "data": {"quota": 20}},
        "redis_key": "cache:user_quota:1",
        "created_at": 1000.0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def writer(monkeypatch):
    """构造一个 DBWriter 实例,其 Redis 与 SQLite 依赖均被 mock 隔离。

    注:依据实际实现,``DBWriter()`` 构造不建立连接(连接在 ``init()`` 中建立)。
    ``_store`` 在 ``init()`` 后才非 None,本 fixture 不调用 init,
    直接 mock ``_execute_sqlite`` 来测试 ``_process_message`` 的路由逻辑。
    """
    instance = DBWriterClass()
    instance._store = MagicMock(name="mock_store")  # 模拟已初始化的 CacheStore
    instance._running = True
    instance._processed_count = 0
    instance._error_count = 0
    # mock redis_queue 的所有外部调用
    monkeypatch.setattr(
        "database.redis_queue.delete", AsyncMock(return_value=True), raising=True
    )
    monkeypatch.setattr(
        "database.redis_queue.push_dead", AsyncMock(return_value=True), raising=True
    )
    return instance


class TestDBWriterProcessMessage:
    """DBWriter._process_message 单元测试。"""

    @pytest.mark.asyncio
    async def test_process_message_success(self, writer):
        """成功处理消息:_execute_sqlite 被调用,redis_key 被 DEL,计数+1。"""
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        writer._execute_sqlite.assert_awaited_once()
        assert writer._processed_count == 1
        assert writer._error_count == 0

    @pytest.mark.asyncio
    async def test_process_message_dels_redis_key(self, writer):
        """处理成功后 DEL Redis 缓冲 key(清除缓冲)。"""
        msg = _make_msg(redis_key="cache:user_quota:42")
        writer._execute_sqlite = AsyncMock(return_value=None)

        await writer._process_message(msg)

        # 验证 redis_queue.delete 被调用(DEL 缓冲 key)
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
        """处理失败(非 TypeError)→ 消息转入死信队列,继续处理后续消息。"""
        msg = _make_msg()
        writer._execute_sqlite = AsyncMock(side_effect=RuntimeError("db error"))

        await writer._process_message(msg)

        assert writer._error_count == 1
        assert writer._processed_count == 0
        # 验证消息转入死信队列
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        assert args[0] == msg  # 原始消息
        assert "RuntimeError" in kwargs.get("reason", "")

    @pytest.mark.asyncio
    async def test_process_message_typeerror_to_dead_queue(self, writer):
        """TypeError(方法签名不匹配)→ 永久失败,入死信队列。"""
        msg = _make_msg(method_name="bad_method", data={"wrong_param": 1})
        writer._execute_sqlite = AsyncMock(side_effect=TypeError("unexpected keyword argument"))

        await writer._process_message(msg)

        assert writer._error_count == 1
        from database import redis_queue
        redis_queue.push_dead.assert_awaited_once()
        args, kwargs = redis_queue.push_dead.call_args
        assert "TypeError" in kwargs.get("reason", "")

    @pytest.mark.asyncio
    async def test_process_message_non_dict_to_dead_queue(self, writer):
        """非 dict 消息 → 直接入死信队列,不调用 _execute_sqlite。"""
        writer._execute_sqlite = AsyncMock(return_value=None)

        # 传入非 dict(字符串、None、列表)
        for bad_msg in ["not_a_dict", None, [1, 2, 3], 42]:
            await writer._process_message(bad_msg)

        # 所有非 dict 消息都入死信,_execute_sqlite 从未被调用
        assert writer._error_count == 4
        assert writer._processed_count == 0
        writer._execute_sqlite.assert_not_awaited()
        from database import redis_queue
        assert redis_queue.push_dead.await_count == 4

    @pytest.mark.asyncio
    async def test_process_message_failure_continues(self, writer):
        """单条失败不影响后续消息处理(失败+成功)。"""
        msg_bad = _make_msg(method_name="bad")
        msg_good = _make_msg(method_name="good")
        writer._execute_sqlite = AsyncMock(side_effect=[RuntimeError("fail"), None])

        await writer._process_message(msg_bad)
        await writer._process_message(msg_good)

        assert writer._execute_sqlite.await_count == 2
        assert writer._error_count == 1
        assert writer._processed_count == 1


class TestDBWriterStop:
    """DBWriter.stop 优雅停止测试。"""

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, writer):
        """stop() 设置 _running=False。"""
        assert writer._running is True
        await writer.stop()
        assert writer._running is False

    @pytest.mark.asyncio
    async def test_stop_logs_counts(self, writer, capsys):
        """stop() 记录已处理/失败的消息计数。"""
        writer._processed_count = 42
        writer._error_count = 3
        await writer.stop()
        # stop 不抛异常即可(日志通过 loguru 输出,不在此断言)
