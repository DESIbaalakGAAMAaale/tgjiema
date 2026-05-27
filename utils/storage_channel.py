from loguru import logger
from config import settings

_cache_active_channel: int | None = None


async def get_active_storage_channel_id() -> int:
    global _cache_active_channel
    if _cache_active_channel is not None:
        return _cache_active_channel

    from database.session import get_config
    try:
        val = await get_config("storage_channel_id")
        if val:
            parsed = int(val)
            _cache_active_channel = parsed
            logger.info(f"[storage_channel] DB配置主存储频道: {parsed}")
            return parsed
    except Exception as e:
        logger.warning(f"[storage_channel] 读取DB配置失败: {e}")

    _cache_active_channel = settings.MAIN_STORAGE_CHANNEL_ID
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