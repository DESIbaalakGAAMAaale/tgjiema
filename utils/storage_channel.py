import asyncio
import time
from loguru import logger


_cache_active_channel: int | None = None
_cache_ts: float = 0
_CACHE_TTL = 60.0  # 60 秒自动过期
_cache_lock = asyncio.Lock()


async def get_active_storage_channel_id() -> int:
    global _cache_active_channel, _cache_ts
    now = time.monotonic()
    # 快速路径:锁内仅检查缓存,命中则立即返回,避免阻塞
    async with _cache_lock:
        if _cache_active_channel is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache_active_channel

    # 缓存未命中:在锁外执行 DB IO,避免长时间持锁阻塞其他协程
    from database.session import get_config
    parsed_val: int | None = None
    try:
        val = await get_config("storage_channel_id")
        if val:
            parsed_val = int(val)
    except Exception as e:
        logger.warning(f"[storage_channel] 读取DB配置失败: {e}")

    # 锁内更新缓存,避免并发写竞争
    async with _cache_lock:
        # double-check:可能在等待锁期间其他协程已更新缓存
        now2 = time.monotonic()
        if _cache_active_channel is not None and (now2 - _cache_ts) < _CACHE_TTL:
            return _cache_active_channel
        if parsed_val is not None:
            _cache_active_channel = parsed_val
            _cache_ts = now2
            logger.info(f"[storage_channel] DB配置主存储频道: {parsed_val}")
            return parsed_val
        _cache_active_channel = 0
        _cache_ts = now2
        logger.warning(f"[storage_channel] 读取DB配置失败, 回退到默认值 0")
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