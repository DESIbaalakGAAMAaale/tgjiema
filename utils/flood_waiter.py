"""Flood Wait 自动退避机制
包装 Telegram API 调用，捕获 FloodWait 异常并自动等待后重试。
"""

import asyncio
import functools
import time
from loguru import logger

from telegram.error import RetryAfter, TimedOut, NetworkError

# 全局退避状态，所有 Bot 实例共享
_backoff_until: float = 0.0
_consecutive_floods: int = 0
MAX_BACKOFF = 120  # 最大退避时间（秒）


def reset_backoff():
    """重置退避状态（成功调用后调用）。"""
    global _backoff_until, _consecutive_floods
    _backoff_until = 0.0
    _consecutive_floods = 0


async def api_call_with_backoff(coro, description: str = "") -> object:
    """执行一个 Telegram API 协程，自动处理 Flood Wait。

    用法：
        result = await api_call_with_backoff(
            bot.copy_message(chat_id=..., from_chat_id=..., message_id=...),
            "copy_message",
        )
    """
    global _backoff_until, _consecutive_floods

    max_retries = 5
    for attempt in range(max_retries):
        # 检查全局退避
        now = time.monotonic()
        if now < _backoff_until:
            wait = _backoff_until - now
            logger.debug(f"[FloodWait] 全局退避 {wait:.1f}s")
            await asyncio.sleep(wait)

        try:
            result = await coro
            # 成功后重置退避
            if attempt > 0:
                reset_backoff()
            return result

        except RetryAfter as e:
            wait = e.retry_after
            _consecutive_floods += 1
            # 指数退避 + consecutive_floods 叠加
            extra = min(_consecutive_floods * 5, MAX_BACKOFF)
            total_wait = wait + extra
            _backoff_until = time.monotonic() + total_wait
            logger.warning(
                f"[FloodWait] {description}: 需等待 {wait}s, "
                f"叠加退避 +{extra}s, 总计 {total_wait}s "
                f"(第 {_consecutive_floods} 次连续触发)"
            )
            await asyncio.sleep(total_wait)

        except (TimedOut, NetworkError) as e:
            wait = 2 ** attempt  # 指数退避: 2s, 4s, 8s, 16s, 32s
            logger.warning(
                f"[Backoff] {description}: {type(e).__name__}, "
                f"等待 {wait}s 后重试 (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(wait)

    logger.error(f"[FloodWait] {description}: 超过最大重试次数 {max_retries}")
    raise RuntimeError(f"API call failed after {max_retries} retries: {description}")


def with_flood_backoff(description: str = ""):
    """装饰器：自动处理 Flood Wait 的 API 调用。"""
    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                return await api_call_with_backoff(
                    func(*args, **kwargs),
                    description or func.__name__,
                )
            return wrapper
        return func
    return decorator


async def safe_copy_message(bot, chat_id: int, from_chat_id: int, message_id: int) -> object:
    """安全复制消息（自动退避）。"""
    return await api_call_with_backoff(
        bot.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        ),
        f"copy_message({from_chat_id}→{chat_id}, msg={message_id})",
    )


async def safe_send_message(bot, chat_id: int, text: str, **kwargs) -> object:
    """安全发送文本消息（自动退避）。"""
    return await api_call_with_backoff(
        bot.send_message(chat_id=chat_id, text=text, **kwargs),
        f"send_message({chat_id})",
    )


async def safe_send_media_group(bot, chat_id: int, media: list, **kwargs) -> object:
    """安全发送媒体组（自动退避）。"""
    return await api_call_with_backoff(
        bot.send_media_group(chat_id=chat_id, media=media, **kwargs),
        f"send_media_group({chat_id}, {len(media)} items)",
    )


# ─── 退避状态监控 ───

def is_in_backoff() -> bool:
    """当前是否处于退避状态。"""
    return time.monotonic() < _backoff_until


def get_backoff_remaining() -> float:
    """获取退避剩余时间（秒）。"""
    return max(0.0, _backoff_until - time.monotonic())


def get_flood_stats() -> dict:
    """获取退避统计。"""
    return {
        "in_backoff": is_in_backoff(),
        "remaining_seconds": get_backoff_remaining(),
        "consecutive_floods": _consecutive_floods,
    }