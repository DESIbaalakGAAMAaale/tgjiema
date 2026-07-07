"""P0-1 回归：idx_bot 的中继投递 / 中继媒体必须校验受信中继白名单（fail-closed）。

- handle_relay_delivery：未授权发送者不得投递文件（白名单为空或非包含均拒绝）。
- _handle_relay_file_media：同上，未授权不得经 RELAY_FILE 媒体投递。

被测函数：bots/idx_bot.py :: handle_relay_delivery / _handle_relay_file_media
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

from bots import idx_bot
import database


def _make_update(user_id, text=None, caption=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    if text is not None:
        update.message.text = text
    if caption is not None:
        update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def test_relay_delivery_rejects_unauthorized_sender(monkeypatch):
    # 白名单为空 → 默认拒绝（fail-closed）
    monkeypatch.setattr(
        database, "get_relay_whitelist", AsyncMock(return_value=set())
    )
    dispatch = AsyncMock()
    gfr = AsyncMock()
    monkeypatch.setattr(idx_bot, "_dispatch_to_dsp", dispatch)
    monkeypatch.setattr(idx_bot, "get_file_record_cached", gfr)

    update = _make_update(12345, text="RELAY_DELIVER:999:somecode")
    context = MagicMock()

    async def _run():
        return await idx_bot.handle_relay_delivery(update, context)

    # 未授权：消息被丢弃（返回 None），未写入任何 jobs
    assert asyncio.run(_run()) is None
    dispatch.assert_not_called()
    gfr.assert_not_called()

    # 白名单非空但 sender 不在其中，同样拒绝
    monkeypatch.setattr(
        database, "get_relay_whitelist", AsyncMock(return_value={99999})
    )
    update2 = _make_update(12345, text="RELAY_DELIVER:999:other")

    async def _run2():
        return await idx_bot.handle_relay_delivery(update2, context)

    asyncio.run(_run2())
    dispatch.assert_not_called()
    gfr.assert_not_called()


def test_relay_file_media_rejects_unauthorized_sender(monkeypatch):
    monkeypatch.setattr(
        database, "get_relay_whitelist", AsyncMock(return_value=set())
    )
    dispatch = AsyncMock()
    gfr = AsyncMock()
    monkeypatch.setattr(idx_bot, "_dispatch_to_dsp", dispatch)
    monkeypatch.setattr(idx_bot, "get_file_record_cached", gfr)

    update = _make_update(12345, caption="RELAY_FILE:999:somecode\nstorage:1")
    context = MagicMock()

    async def _run():
        return await idx_bot._handle_relay_file_media(update, context)

    # 未授权：返回 True（表示已处理/已拒绝），未写入
    assert asyncio.run(_run()) is True
    dispatch.assert_not_called()
    gfr.assert_not_called()
