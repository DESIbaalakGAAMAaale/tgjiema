"""Flood Wait 自动退避机制
包装 Telegram API 调用，捕获 FloodWait 异常并自动等待后重试。
"""

import asyncio
import functools
import random
import time
from loguru import logger

from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest, Forbidden

# 按账号隔离的退避状态：key 为 bot_id，value 为退避截止时间戳
_backoff_until: dict[int, float] = {}
_consecutive_floods: dict[int, int] = {}
MAX_BACKOFF = 120  # 最大退避时间（秒）
MAX_CONSECUTIVE_FLOODS = 10  # 连续触发上限,防止异常累加


def reset_backoff(bot_id: int = 0):
    """重置退避状态（成功调用后调用）。"""
    _backoff_until.pop(bot_id, None)
    _consecutive_floods.pop(bot_id, None)


async def api_call_with_backoff(coro_factory, description: str = "", bot_id: int = 0) -> object:
    """执行一个 Telegram API 调用，自动处理 Flood Wait（按 bot_id 隔离退避）。

    注意：coro_factory 是一个返回协程的工厂函数，每次重试都会创建新的协程。
    这是因为协程只能 await 一次，复用已消耗的协程会导致 RuntimeError。

    用法：
        result = await api_call_with_backoff(
            lambda: bot.copy_message(chat_id=..., from_chat_id=..., message_id=...),
            "copy_message",
            bot_id=12345,
        )
    """
    max_retries = 5
    for attempt in range(max_retries):
        # 检查该账号的退避状态
        now = time.monotonic()
        backoff = _backoff_until.get(bot_id, 0)
        if now < backoff:
            wait = backoff - now
            logger.debug(f"[FloodWait] bot_id={bot_id} 退避 {wait:.1f}s")
            await asyncio.sleep(wait)

        try:
            coro = coro_factory()
            result = await coro
            # 成功后重置该账号退避(无论是否重试过)
            reset_backoff(bot_id)
            return result

        except RetryAfter as e:
            wait = e.retry_after
            floods = min(_consecutive_floods.get(bot_id, 0) + 1, MAX_CONSECUTIVE_FLOODS)
            _consecutive_floods[bot_id] = floods
            # 指数退避 + consecutive_floods 叠加 + Jitter 抖动
            extra = min(floods * 5, MAX_BACKOFF)
            jitter = random.uniform(0, 2)
            total_wait = wait + extra + jitter
            _backoff_until[bot_id] = time.monotonic() + total_wait
            logger.warning(
                f"[FloodWait] bot_id={bot_id} {description}: 需等待 {wait}s, "
                f"叠加退避 +{extra}s, 抖动 +{jitter:.1f}s, 总计 {total_wait:.1f}s "
                f"(第 {floods} 次连续触发)"
            )
            # 最后一次重试不再 sleep,避免白等后直接抛错
            if attempt < max_retries - 1:
                await asyncio.sleep(total_wait)

        except BadRequest as e:
            # C3: "message not found" 类错误重试无意义，直接抛出
            # 常见于 failover/rotation 后 shadow 频道没有历史文件
            logger.warning(
                f"[BadRequest] bot_id={bot_id} {description}: {e}"
            )
            raise

        except Forbidden as e:
            # 用户未 /start bot 或已 block bot，重试无意义，直接抛出
            logger.warning(
                f"[Forbidden] bot_id={bot_id} {description}: {e}"
            )
            raise

        except (TimedOut, NetworkError) as e:
            wait = 2 ** attempt  # 指数退避: 2s, 4s, 8s, 16s, 32s
            logger.warning(
                f"[Backoff] bot_id={bot_id} {description}: {type(e).__name__}, "
                f"等待 {wait}s 后重试 (attempt {attempt + 1}/{max_retries})"
            )
            # 最后一次重试不再 sleep,避免白等后直接抛错
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)

    logger.error(f"[FloodWait] bot_id={bot_id} {description}: 超过最大重试次数 {max_retries}")
    raise RuntimeError(f"API call failed after {max_retries} retries: {description}")


def with_flood_backoff(description: str = ""):
    """装饰器：自动处理 Flood Wait 的 API 调用。"""
    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                bot_id = kwargs.pop("bot_id", 0)
                _args, _kwargs = args, kwargs
                return await api_call_with_backoff(
                    lambda: func(*_args, **_kwargs),
                    description or func.__name__,
                    bot_id=bot_id,
                )
            return wrapper
        return func
    return decorator


async def safe_copy_message(bot, chat_id: int, from_chat_id: int, message_id: int, bot_id: int = 0, **kwargs) -> object:
    """安全复制消息（自动退避）。"""
    _cid, _fid, _mid, _kw = chat_id, from_chat_id, message_id, kwargs
    return await api_call_with_backoff(
        lambda: bot.copy_message(chat_id=_cid, from_chat_id=_fid, message_id=_mid, **_kw),
        f"copy_message({from_chat_id}→{chat_id}, msg={message_id})",
        bot_id=bot_id,
    )


async def safe_send_message(bot, chat_id: int, text: str, bot_id: int = 0, **kwargs) -> object:
    """安全发送文本消息（自动退避）。"""
    _cid, _text, _kw = chat_id, text, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_message(chat_id=_cid, text=_text, **_kw),
        f"send_message({chat_id})",
        bot_id=bot_id,
    )


async def safe_reply_text(message, text: str, bot_id: int = 0, **kwargs) -> object:
    """安全回复消息（自动退避），替代 update.message.reply_text()。"""
    from telegram import Message
    chat_id = message.chat_id if isinstance(message, Message) else message.chat.id
    _text, _kw = text, kwargs
    return await api_call_with_backoff(
        lambda: message.reply_text(text=_text, **_kw),
        f"reply_text({chat_id})",
        bot_id=bot_id,
    )


async def safe_send_media_group(bot, chat_id: int, media: list, bot_id: int = 0, **kwargs) -> object:
    """安全发送媒体组（自动退避）。"""
    _cid, _media, _kw = chat_id, media, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_media_group(chat_id=_cid, media=_media, **_kw),
        f"send_media_group({chat_id}, {len(media)} items)",
        bot_id=bot_id,
    )


# ─── file_id 直发函数（避开 copy_message 的频道限速） ──


async def safe_send_photo(bot, chat_id: int, photo: str, bot_id: int = 0, **kwargs):
    """安全发送照片（自动退避）。"""
    _cid, _fid, _kw = chat_id, photo, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_photo(chat_id=_cid, photo=_fid, **_kw),
        f"send_photo({chat_id})",
        bot_id=bot_id,
    )


async def safe_send_video(bot, chat_id: int, video: str, bot_id: int = 0, **kwargs):
    """安全发送视频（自动退避）。"""
    _cid, _fid, _kw = chat_id, video, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_video(chat_id=_cid, video=_fid, **_kw),
        f"send_video({chat_id})",
        bot_id=bot_id,
    )


async def safe_send_audio(bot, chat_id: int, audio: str, bot_id: int = 0, **kwargs):
    """安全发送音频（自动退避）。"""
    _cid, _fid, _kw = chat_id, audio, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_audio(chat_id=_cid, audio=_fid, **_kw),
        f"send_audio({chat_id})",
        bot_id=bot_id,
    )


async def safe_send_animation(bot, chat_id: int, animation: str, bot_id: int = 0, **kwargs):
    """安全发送动画（自动退避）。"""
    _cid, _fid, _kw = chat_id, animation, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_animation(chat_id=_cid, animation=_fid, **_kw),
        f"send_animation({chat_id})",
        bot_id=bot_id,
    )


async def safe_send_document(bot, chat_id: int, document: str, bot_id: int = 0, **kwargs):
    """安全发送文件（自动退避）。"""
    _cid, _fid, _kw = chat_id, document, kwargs
    return await api_call_with_backoff(
        lambda: bot.send_document(chat_id=_cid, document=_fid, **_kw),
        f"send_document({chat_id})",
        bot_id=bot_id,
    )


# ─── 退避状态监控 ───

def is_in_backoff(bot_id: int = 0) -> bool:
    """当前账号是否处于退避状态。"""
    return time.monotonic() < _backoff_until.get(bot_id, 0)


def get_backoff_remaining(bot_id: int = 0) -> float:
    """获取当前账号退避剩余时间（秒）。"""
    return max(0.0, _backoff_until.get(bot_id, 0) - time.monotonic())


def get_flood_stats(bot_id: int = 0) -> dict:
    """获取退避统计。"""
    return {
        "bot_id": bot_id,
        "in_backoff": is_in_backoff(bot_id),
        "remaining_seconds": get_backoff_remaining(bot_id),
        "consecutive_floods": _consecutive_floods.get(bot_id, 0),
    }