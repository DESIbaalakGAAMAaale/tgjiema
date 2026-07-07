"""P1-17 回归：utils/force_join.py :: check_force_join 默认 fail-closed（瞬态网络错误拒绝放行），
仅在 FORCE_JOIN_FAIL_OPEN 环境变量显式开启时临时放行（Telegram 大面积故障的运维逃生开关）。

被测函数：utils/force_join.py :: check_force_join
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

import utils.force_join as fj
import config


def _make_update(user_id):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _make_member(status):
    """构造一个具有确定 status 的成员对象（供 get_chat_member 返回）。"""
    m = MagicMock()
    m.status = status
    return m


def test_force_join_failclosed(monkeypatch):
    # 需要 FORCE_JOIN_CHANNEL_ID 非 0，否则会直接放行（视为未配置频道）
    monkeypatch.setattr(config.settings, "FORCE_JOIN_CHANNEL_ID", 1)

    async def _run(member_or_exc, fail_open):
        monkeypatch.setenv("FORCE_JOIN_FAIL_OPEN", "true" if fail_open else "false")
        update = _make_update(12345)
        context = MagicMock()
        # 异常类 → 作为 side_effect 抛出；成员对象 → 作为 return_value 直接返回。
        # 注意：AsyncMock 的 side_effect 若为非可调用对象，await 后得到的是其
        # return_value 而非该对象本身，因此成员对象必须用 return_value。
        if isinstance(member_or_exc, Exception):
            context.bot.get_chat_member = AsyncMock(side_effect=member_or_exc)
        else:
            context.bot.get_chat_member = AsyncMock(return_value=member_or_exc)
        return await fj.check_force_join(update, context)

    # 明确非成员 / 频道配置错误 → 拒绝（fail-closed）
    assert asyncio.run(_run(BadRequest("nope"), False)) is False
    assert asyncio.run(_run(Forbidden("no"), False)) is False

    # 瞬态网络错误：默认 fail-closed 拒绝
    assert asyncio.run(_run(NetworkError("net"), False)) is False
    assert asyncio.run(_run(TimedOut("timeout"), False)) is False

    # 运维逃生开关开启时，瞬态错误临时放行
    assert asyncio.run(_run(NetworkError("net"), True)) is True

    # 已加入成员（member / administrator / creator）→ 放行
    assert asyncio.run(_run(_make_member("member"), False)) is True
    assert asyncio.run(_run(_make_member("administrator"), False)) is True
    assert asyncio.run(_run(_make_member("creator"), False)) is True

    # 明确非成员（status=left / kicked）→ 拒绝
    assert asyncio.run(_run(_make_member("left"), False)) is False
    assert asyncio.run(_run(_make_member("kicked"), False)) is False
