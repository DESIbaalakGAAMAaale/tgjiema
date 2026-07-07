"""P1-10 回归：database/cache.py :: invalidate_code_entry 通过 fire-and-forget 异步任务
删除 SQLite 持久化缓存，任务完成后从 _pending_tasks 中移除（持有强引用防止 GC 提前回收
导致删除丢失）。

被测函数：database/cache.py :: invalidate_code_entry / _pending_tasks
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import database.cache as cache
import database.cache_store as cache_store


def test_invalidate_code_entry_persists(monkeypatch):
    # 用可控的假 store 替换 get_cache_store（database.cache_store 在 conftest 中为 MagicMock）
    store = MagicMock()
    store.delete = AsyncMock()
    monkeypatch.setattr(cache_store, "get_cache_store", lambda: store)

    cache._pending_tasks.clear()

    async def _run():
        cache.invalidate_code_entry("TESTCODE")
        # 运行中的事件循环里，应创建任务并加入 _pending_tasks
        assert len(cache._pending_tasks) == 1
        task = next(iter(cache._pending_tasks))
        await task               # 等待异步删除完成
        await asyncio.sleep(0)   # 让 add_done_callback 执行
        # 完成后应自动 discard，防止被 GC 提前回收
        assert task not in cache._pending_tasks

    asyncio.run(_run())

    # SQLite 持久化缓存 delete 被调用，参数为 "code:TESTCODE"
    assert store.delete.await_count >= 1
    store.delete.assert_called_with("code:TESTCODE")
