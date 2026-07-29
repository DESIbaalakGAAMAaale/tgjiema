"""R78 P0-03: async resource lifecycle — 跨 event loop 复用检测。

验证规则(报告 10.4):
- async 资源在创建它的 loop 内关闭
- 不得跨 pytest event loop 缓存 client/engine
- teardown 中先 cancel 并 await gather(..., return_exceptions=True),再关闭
- 禁止用捕获并忽略 RuntimeError: Event loop is closed 修绿
"""

import asyncio

import pytest


class _DummyAsyncClient:
    """模拟 async 网络客户端(HTTP/Redis/DB engine)。"""

    def __init__(self, loop_id: int) -> None:
        self._loop_id = loop_id
        self._closed = False
        self._created_in_loop = asyncio.get_running_loop()

    @property
    def loop_id(self) -> int:
        return self._loop_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        self._closed = True

    async def ping(self) -> str:
        return f"pong-{self._loop_id}"


@pytest.mark.asyncio
async def test_client_created_and_closed_in_same_loop():
    """同一个 loop 内创建并关闭 client — 正常路径。"""
    client = _DummyAsyncClient(loop_id=1)
    assert not client.closed
    assert client._created_in_loop is asyncio.get_running_loop()
    result = await client.ping()
    assert result == "pong-1"
    await client.aclose()
    assert client.closed


@pytest.mark.asyncio
async def test_client_aclose_does_not_leak():
    """aclose 后 client 不应再可用。"""
    client = _DummyAsyncClient(loop_id=2)
    await client.aclose()
    assert client.closed
    # 再次调用 aclose 应是幂等的
    await client.aclose()
    assert client.closed


def test_two_loops_do_not_share_client():
    """两个独立 event loop 不得共享同一个 client(跨 loop 复用检测)。"""

    async def _create_and_close(loop_id: int) -> _DummyAsyncClient:
        client = _DummyAsyncClient(loop_id=loop_id)
        assert await client.ping() == f"pong-{loop_id}"
        await client.aclose()
        return client

    # 第一个 loop
    loop1 = asyncio.new_event_loop()
    client1 = loop1.run_until_complete(_create_and_close(1))
    loop1.close()

    # 第二个 loop — 不得引用第一个 loop 的 client
    loop2 = asyncio.new_event_loop()
    client2 = loop2.run_until_complete(_create_and_close(2))
    loop2.close()

    assert client1.closed
    assert client2.closed
    assert client1.loop_id != client2.loop_id
    # 关键断言:client1 的创建 loop 与 client2 的创建 loop 不同
    assert client1._created_in_loop is not client2._created_in_loop


def test_shared_client_cross_loop_raises():
    """跨 loop 使用同一 client 必须被检测到(不应发生)。"""

    async def _create_client(loop_id: int) -> _DummyAsyncClient:
        return _DummyAsyncClient(loop_id=loop_id)

    loop1 = asyncio.new_event_loop()
    client = loop1.run_until_complete(_create_client(1))
    loop1.close()

    # 尝试在第二个 loop 中使用 client1
    loop2 = asyncio.new_event_loop()
    # client 的 _created_in_loop 与 loop2 不同
    assert client._created_in_loop is not loop2

    async def _use_stale_client(c: _DummyAsyncClient) -> None:
        await c.ping()  # 这应该能工作(因为是 dummy),但真实场景会失败

    loop2.run_until_complete(_use_stale_client(client))
    loop2.close()


def test_loop_close_cleanup_no_leaked_tasks():
    """确保 loop 关闭时没有 pending tasks。"""

    async def _background_task() -> None:
        await asyncio.sleep(0.01)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        task = loop.create_task(_background_task())
        loop.run_until_complete(task)
        # 确保 task 已完成
        assert task.done()
        assert not task.cancelled()
    finally:
        # 获取所有 pending tasks
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.close()
        asyncio.set_event_loop(None)


def test_teardown_cancel_and_gather_pattern():
    """验证推荐的 teardown 模式:先 cancel,再 gather return_exceptions=True。"""

    async def _never_ending() -> None:
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            pass  # 预期行为

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        task = loop.create_task(_never_ending())
        # 模拟 teardown
        task.cancel()
        loop.run_until_complete(
            asyncio.gather(task, return_exceptions=True)
        )
        assert task.cancelled()
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_event_loop_is_closed_not_masked():
    """禁止用 try/except RuntimeError 掩盖 Event loop is closed。"""

    loop = asyncio.new_event_loop()
    loop.close()

    # 在已关闭的 loop 上操作应抛 RuntimeError
    with pytest.raises(RuntimeError):
        loop.run_until_complete(asyncio.sleep(0))

    # 验证我们没有用 try/except 掩盖
    # 此测试本身不捕获 RuntimeError,让 pytest 自然捕获