"""Redis Queue 封装(方案B: 写入缓冲层)

设计:
- LPUSH: 写操作推入队列(<0.1ms 返回)
- BRPOP: Writer 进程阻塞消费(串行落盘 SQLite)
- DEL: Writer 写完后清除缓冲 key
- LLEN: mon_bot 监控队列积压
- REDIS_URL 为空时所有方法返回 False(降级到 SQLite 直写)

消息格式(JSON):
{
    "op_type": "upsert" | "update" | "delete" | "insert",
    "table": "heartbeat_local" | "user_quota" | ...,
    "method_name": "write_heartbeat" | "upsert_user_quota" | ...,
    "data": { ... 方法参数 ... },
    "redis_key": "cache:user_quota:12345" | "",
    "created_at": 1234567890.123
}
"""
import json
import time
from typing import Any, Optional

from loguru import logger


_redis_client: Any = None
_redis_init_attempted: bool = False
_redis_available: bool = False
_redis_last_attempt_ts: float = 0
_REDIS_RETRY_INTERVAL = 60.0  # 失败后 60 秒重试


async def get_redis() -> Any:
    """获取 Redis 客户端(延迟初始化)。未配置或连接失败返回 None。"""
    global _redis_client, _redis_init_attempted, _redis_available, _redis_last_attempt_ts
    if _redis_available and _redis_client:
        return _redis_client
    now = time.time()
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
        logger.info("[RedisQueue] 连接成功,Writer 缓冲层已启用")
    except Exception as e:
        logger.warning(f"[RedisQueue] 连接失败,降级到 SQLite 直写(60s 后重试): {e}")
        _redis_available = False
        _redis_client = None
    return _redis_client if _redis_available else None


async def push(op_type: str, table: str, method_name: str, data: dict,
               redis_key: str = "") -> bool:
    """推入写操作到 Redis Queue(LPUSH)。

    Args:
        op_type: 操作类型(upsert/update/delete/insert)
        table: 目标 SQLite 表名
        method_name: 调用的 cache_store 方法名(Writer 用于分派)
        data: 方法参数字典
        redis_key: 关联的 Redis 缓存 key(Writer 写完后 DEL,空字符串表示无缓存)

    Returns:
        True 推入成功, False 降级到 SQLite 直写
    """
    redis = await get_redis()
    if not redis:
        return False
    try:
        from config import settings
        msg = {
            "op_type": op_type,
            "table": table,
            "method_name": method_name,
            "data": data,
            "redis_key": redis_key,
            "created_at": time.time(),
        }
        await redis.lpush(settings.WRITER_QUEUE_KEY, json.dumps(msg))
        return True
    except Exception as e:
        logger.warning(f"[RedisQueue] push 失败(降级到 SQLite): {e}")
        return False


async def pop(timeout: int = 0, count: int = 1) -> list[dict]:
    """从 Redis Queue 弹出消息(BRPOP,阻塞)。

    Args:
        timeout: 阻塞超时秒数,0 表示永久阻塞
        count: 单次弹出数量(减少往返,实际用循环实现)

    Returns:
        消息列表,空列表表示超时或降级
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        from config import settings
        result = []
        for _ in range(count):
            item = await redis.brpop(settings.WRITER_QUEUE_KEY, timeout=timeout)
            if item is None:
                break
            _key, raw = item
            msg = json.loads(raw)
            result.append(msg)
        return result
    except Exception as e:
        logger.debug(f"[RedisQueue] pop 异常: {e}")
        return []


async def delete(key: str) -> bool:
    """删除指定 key(Writer 写完 SQLite 后清除缓冲)。"""
    redis = await get_redis()
    if not redis or not key:
        return False
    try:
        await redis.delete(key)
        return True
    except Exception as e:
        logger.debug(f"[RedisQueue] delete 异常: {e}")
        return False


async def length() -> int:
    """获取队列长度(mon_bot 监控积压)。"""
    redis = await get_redis()
    if not redis:
        return -1
    try:
        from config import settings
        return await redis.llen(settings.WRITER_QUEUE_KEY)
    except Exception as e:
        logger.debug(f"[RedisQueue] length 异常: {e}")
        return -1


async def health_check() -> bool:
    """健康检查(用于 db_writer 启动时验证 Redis 可达)。"""
    redis = await get_redis()
    if not redis:
        return False
    try:
        await redis.ping()
        return True
    except Exception:
        return False


async def cache_get(key: str) -> Optional[str]:
    """读取缓存(读路径优先 Redis,未命中返回 None)。"""
    redis = await get_redis()
    if not redis:
        return None
    try:
        return await redis.get(key)
    except Exception as e:
        logger.debug(f"[RedisQueue] cache_get 异常: {e}")
        return None


async def cache_set(key: str, value: str, ttl: int = 5) -> bool:
    """写入缓存(读路径回填,带 TTL)。"""
    redis = await get_redis()
    if not redis:
        return False
    try:
        await redis.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.debug(f"[RedisQueue] cache_set 异常: {e}")
        return False


async def cache_delete(key: str) -> bool:
    """失效缓存(写操作后清除对应读缓存,保证一致性)。"""
    redis = await get_redis()
    if not redis:
        return False
    try:
        await redis.delete(key)
        return True
    except Exception as e:
        logger.debug(f"[RedisQueue] cache_delete 异常: {e}")
        return False


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
