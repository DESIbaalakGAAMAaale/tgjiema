import asyncio
import time
from loguru import logger
from config import settings

_cache_active_channel: int | None = None
_cache_ts: float = 0
_CACHE_TTL = 60.0  # 60 秒自动过期
_cache_lock = asyncio.Lock()


async def get_active_storage_channel_id() -> int:
    global _cache_active_channel, _cache_ts
    now = time.monotonic()
    async with _cache_lock:
        if _cache_active_channel is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache_active_channel

        from database.session import get_config
        try:
            val = await get_config("storage_channel_id")
            if val:
                parsed = int(val)
                _cache_active_channel = parsed
                _cache_ts = now
                logger.info(f"[storage_channel] DB配置主存储频道: {parsed}")
                return parsed
        except Exception as e:
            logger.warning(f"[storage_channel] 读取DB配置失败: {e}")

        _cache_active_channel = settings.MAIN_STORAGE_CHANNEL_ID
        _cache_ts = now
        logger.info(f"[storage_channel] 使用settings默认主存储频道: {_cache_active_channel}")
        return _cache_active_channel


async def set_active_storage_channel_id(channel_id: int):
    global _cache_active_channel
    from database.session import set_config
    try:
        await set_config("storage_channel_id", str(channel_id))
        _cache_active_channel = channel_id
        logger.info(f"[storage_channel] 主存储频道已切换到: {channel_id}")
        return True
    except Exception as e:
        logger.error(f"[storage_channel] 设置主存储频道失败: {e}")
        return False


def invalidate_cache():
    global _cache_active_channel
    _cache_active_channel = None