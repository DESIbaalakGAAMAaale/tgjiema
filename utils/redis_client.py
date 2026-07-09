"""Redis Stream 客户端封装(C1: 事件驱动替代 dsp_notify 轮询)

设计:
- REDIS_URL 为空时禁用 Redis,所有方法返回 None/False/空(降级到 SQLite 轮询)
- 延迟初始化:首次调用时创建连接池
- 连接失败不抛异常,返回 None/False(降级)
- 连接失败后周期性重试(60s),避免临时网络抖动导致永久降级
- Stream + 消费者组幂等创建
"""
import os
import socket
import time
from typing import Any
from loguru import logger

STREAM_NAME = "dsp_jobs"
CONSUMER_GROUP = "dsp_grp"

_redis_client: Any = None
_redis_init_attempted: bool = False
_redis_available: bool = False
_redis_last_attempt_ts: float = 0
_REDIS_RETRY_INTERVAL = 60.0  # 失败后 60 秒重试


def get_consumer_name() -> str:
    """生成唯一消费者名(主机名+PID),防多进程撞名。"""
    return f"dw-{socket.gethostname()}-{os.getpid()}"


async def get_redis() -> Any:
    """获取 Redis 客户端(延迟初始化)。未配置或连接失败返回 None。"""
    global _redis_client, _redis_init_attempted, _redis_available, _redis_last_attempt_ts
    if _redis_available and _redis_client:
        return _redis_client
    now = time.time()
    # 首次尝试 或 失败后周期性重试
    if _redis_init_attempted and (now - _redis_last_attempt_ts) < _REDIS_RETRY_INTERVAL:
        return None
    _redis_init_attempted = True
    _redis_last_attempt_ts = now
    from config import settings
    if not settings.REDIS_URL:
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        await _redis_client.ping()
        _redis_available = True
        logger.info(f"[Redis] 连接成功,Stream 事件驱动已启用")
        await _init_consumer_group()
    except Exception as e:
        logger.warning(f"[Redis] 连接失败,降级到 SQLite 轮询(60s 后重试): {e}")
        _redis_available = False
        _redis_client = None
    return _redis_client if _redis_available else None


async def _init_consumer_group():
    """幂等创建消费者组(MKSTREAM)。"""
    if not _redis_client:
        return
    try:
        await _redis_client.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info(f"[Redis] 消费者组 {CONSUMER_GROUP} 已创建")
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.debug(f"[Redis] 创建消费者组异常(可能已存在): {e}")


async def xadd_job(crdb_id: int) -> bool:
    """生产者:向 Stream 写入 job 通知。失败返回 False(降级到 SQLite)。"""
    redis = await get_redis()
    if not redis:
        return False
    try:
        from config import settings
        maxlen = getattr(settings, "REDIS_STREAM_MAXLEN", 10000)
        await redis.xadd(
            STREAM_NAME,
            {"crdb_id": str(crdb_id)},
            maxlen=maxlen,
            approximate=True,
        )
        return True
    except Exception as e:
        logger.warning(f"[Redis] xadd 失败(降级到 SQLite): {e}")
        return False


async def xreadgroup_jobs(consumer: str, count: int = 10, block: int = 5000) -> list[tuple[str, dict]]:
    """消费者:从 Stream 读取 job 通知(BLOCK)。

    返回 [(message_id, {"crdb_id": "..."}), ...]
    失败/超时返回空列表(降级到 SQLite 轮询)。
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        resp = await redis.xreadgroup(
            CONSUMER_GROUP,
            consumer,
            {STREAM_NAME: ">"},
            count=count,
            block=block,
        )
        result = []
        for _stream, messages in resp:
            for msg_id, fields in messages:
                result.append((msg_id, fields))
        return result
    except Exception as e:
        logger.debug(f"[Redis] xreadgroup 异常(降级到 SQLite): {e}")
        return []


async def xack_job(message_id: str) -> bool:
    """确认 job 已处理。失败返回 False(由 reclaim 兜底)。"""
    redis = await get_redis()
    if not redis:
        return False
    try:
        await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
        return True
    except Exception as e:
        logger.debug(f"[Redis] xack 异常: {e}")
        return False


async def xautoclaim_stale_jobs(min_idle_ms: int = 600000, count: int = 10) -> list[tuple[str, dict]]:
    """回收 PEL 中陈旧的孤儿条目(worker 崩溃后未 XACK 的消息)。

    使用 XAUTOCLAIM 认领 idle 时间超过 min_idle_ms 的条目。
    返回 [(message_id, fields), ...],失败返回空列表。
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        # XAUTOCLAIM 返回 (next_start_id, [(msg_id, fields), ...], deleted_ids)
        resp = await redis.xautoclaim(
            STREAM_NAME,
            CONSUMER_GROUP,
            get_consumer_name(),  # 用动态消费者名认领
            min_idle_time=min_idle_ms,
            count=count,
            start_id="0-0",
        )
        result = []
        if isinstance(resp, (list, tuple)) and len(resp) >= 2:
            for msg_id, fields in resp[1]:
                result.append((msg_id, fields))
        return result
    except Exception as e:
        logger.debug(f"[Redis] xautoclaim 异常: {e}")
        return []


async def close_redis():
    """关闭 Redis 连接(进程退出时调用)。"""
    global _redis_client, _redis_available
    if _redis_client:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
    _redis_client = None
    _redis_available = False
