"""P1-14 回归：/factory_reset 执行后必须失效全部内存缓存与拓扑缓存，
确保 reset 后历史文件码 / 记录不可再解码，管理面板不再显示旧数据。

被测函数：bots/admin_bot/handlers.py :: factory_reset
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

from bots.admin_bot import handlers
from bots.admin_bot import display
import database.cache as dbcache
import database.session as dbsession
import storage.delivery_resolver as dr


def _mock_cache():
    c = MagicMock()
    c.clear = MagicMock()
    return c


def test_factory_reset_clears_in_memory_cache(monkeypatch):
    # 与 conftest 中 fake settings.ADMIN_TELEGRAM_ID (=0) 对齐，才能通过 _auth_required
    AUTHORIZED = 0
    update = MagicMock()
    update.effective_user.id = AUTHORIZED
    update.message.reply_text = AsyncMock()
    update.message.edit_text = AsyncMock()
    context = MagicMock()
    context.args = ["confirm", "I_UNDERSTAND"]

    # 1) DB 客户端（CockroachDBClient）全程 mock，避免真实连接
    fake_client = MagicMock()
    fake_client.configure = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.execute = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(dbsession, "CockroachDBClient", lambda: fake_client)

    # 2) 内存缓存对象（返回可断言 .clear 调用的 MagicMock）
    code_cache = _mock_cache()
    file_cache = _mock_cache()
    user_cache = _mock_cache()
    user_codes_cache = _mock_cache()
    config_cache = _mock_cache()
    monkeypatch.setattr(dbcache, "get_code_cache", lambda: code_cache)
    monkeypatch.setattr(dbcache, "get_file_record_cache", lambda: file_cache)
    monkeypatch.setattr(dbcache, "get_user_cache", lambda: user_cache)
    monkeypatch.setattr(dbcache, "get_user_codes_cache", lambda: user_codes_cache)
    monkeypatch.setattr(dbcache, "get_config_cache", lambda: config_cache)
    clear_neg = MagicMock()
    monkeypatch.setattr(dbcache, "clear_negative_caches", clear_neg)

    # 3) 拓扑 / 投递缓存失效函数
    display_invalidate = MagicMock()
    monkeypatch.setattr(display, "invalidate_cells_cache", display_invalidate)
    invalidate_cell_by_channel = MagicMock()
    monkeypatch.setattr(
        dbsession, "invalidate_cell_by_channel_cache", invalidate_cell_by_channel
    )
    # storage.delivery_resolver.invalidate_cell_cache 已是 stub MagicMock

    async def _run():
        await handlers.factory_reset(update, context)

    asyncio.run(_run())

    code_cache.clear.assert_called()
    file_cache.clear.assert_called()
    user_cache.clear.assert_called()
    user_codes_cache.clear.assert_called()
    config_cache.clear.assert_called()
    clear_neg.assert_called()
    display_invalidate.assert_called()
    dr.invalidate_cell_cache.assert_called()
    invalidate_cell_by_channel.assert_called()
