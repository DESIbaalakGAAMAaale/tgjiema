"""Redis Queue 封装(方案B: 写入缓冲层)

设计:
- LPUSH: 写操作推入队列(<0.1ms 返回)
- BRPOP: Writer 进程阻塞消费(串行落盘 SQLite)
- DEL: Writer 写完后清除读缓存 key(以 SQLite 为权威,避免读到旧缓存)
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
import os
import time
from typing import Any, Optional

from loguru import logger


_redis_client: Any = None
_redis_init_attempted: bool = False
_redis_available: bool = False
_redis_last_attempt_ts: float = 0
_REDIS_RETRY_INTERVAL = 60.0  # 失败后 60 秒重试
_redis_init_lock = None  # asyncio.Lock,延迟初始化(P1修复: 防止并发 race)


def _get_init_lock():
    """延迟获取 asyncio.Lock(避免在模块加载时创建事件循环)"""
    global _redis_init_lock
    if _redis_init_lock is None:
        _redis_init_lock = __import__("asyncio").Lock()
    return _redis_init_lock


async def get_redis() -> Any:
    """获取 Redis 客户端(延迟初始化)。未配置或连接失败返回 None。

    P1修复: 用 asyncio.Lock 双检锁防止并发初始化导致 socket 泄漏
    P1修复: 连接失败时 aclose 旧 client 再置 None
    """
    global _redis_client, _redis_init_attempted, _redis_available, _redis_last_attempt_ts
    if _redis_available and _redis_client:
        return _redis_client
    # REDIS_URL 为空时直接返回,不走重试节流
    from config import settings
    if not settings.REDIS_URL:
        return None
    now = time.time()
    if _redis_init_attempted and (now - _redis_last_attempt_ts) < _REDIS_RETRY_INTERVAL:
        return None
    # P1修复: asyncio.Lock 防止多协程并发初始化
    async with _get_init_lock():
        # 双检锁:拿到锁后再次检查(可能其他协程已完成初始化)
        if _redis_available and _redis_client:
            return _redis_client
        _redis_init_attempted = True
        _redis_last_attempt_ts = now
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
            # P1修复: aclose 旧 client 再置 None,避免 socket 泄漏
            if _redis_client is not None:
                try:
                    await _redis_client.aclose()
                except Exception:
                    pass
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
    """从 Redis Queue 弹出消息(BRPOP + LPOP 组合,避免批量退化)。

    P1修复: 首条用 BRPOP(阻塞等待),后续 count-1 条用 LPOP(非阻塞立即返回),
    避免低吞吐下循环 BRPOP 导致的空等。
    P1修复: JSON 解析失败的消息入死信队列,不永久丢失。
    P1修复: Redis 运行时宕机时重置客户端状态,使 get_redis() 下次触发重连,
    避免 db_writer 100% CPU 忙等(过期客户端反复抛异常)。

    Args:
        timeout: 阻塞超时秒数,0 表示永久阻塞(仅对首条生效)
        count: 单次弹出数量

    Returns:
        消息列表,空列表表示超时或降级
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        from config import settings
        result = []
        # 首条:BRPOP 阻塞等待
        item = await redis.brpop(settings.WRITER_QUEUE_KEY, timeout=timeout)
        if item is None:
            return []
        _key, raw = item
        try:
            msg = json.loads(raw)
            result.append(msg)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[RedisQueue] BRPOP 消息 JSON 解析失败,入死信: {e}, raw={raw!r}")
            await push_dead({"raw": raw}, reason=f"JSON decode failed: {e}")
        # 后续 count-1 条:LPOP 非阻塞立即取(不等待)
        for _ in range(count - 1):
            raw2 = await redis.lpop(settings.WRITER_QUEUE_KEY)
            if raw2 is None:
                break  # 队列已空
            try:
                msg2 = json.loads(raw2)
                result.append(msg2)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(f"[RedisQueue] LPOP 消息 JSON 解析失败,入死信: {e}, raw={raw2!r}")
                await push_dead({"raw": raw2}, reason=f"JSON decode failed: {e}")
        return result
    except Exception as e:
        logger.debug(f"[RedisQueue] pop 异常: {e}")
        # P1修复: Redis 运行时宕机时重置客户端状态,使 get_redis() 下次
        # 触发重连(受 60s 节流),避免 db_writer 100% CPU 忙等循环
        global _redis_available, _redis_client
        if _redis_client is not None:
            try:
                await _redis_client.aclose()
            except Exception:
                pass
            _redis_client = None
        _redis_available = False
        return []


async def delete(key: str) -> bool:
    """删除指定 key(Writer 写完 SQLite 后清除读缓存 key)。
    P2修复: 合并 delete 与 cache_delete 的重复逻辑,cache_delete 复用此函数。
    """
    redis = await get_redis()
    if not redis or not key:
        return False
    try:
        await redis.delete(key)
        return True
    except Exception as e:
        logger.debug(f"[RedisQueue] delete 异常: {e}")
        return False


async def push_dead(msg: dict, reason: str = "") -> bool:
    """推入死信队列(P0修复: 处理失败的消息转入死信队列,避免永久丢失)。

    P0修复: Redis 不可达时降级写本地文件 dead_letter.jsonl,避免消息永久丢失。

    Args:
        msg: 原始消息字典(或任意可序列化对象)
        reason: 失败原因(记录在消息中,便于排查)

    Returns:
        True 推入成功, False 失败(此时已降级写本地文件)
    """
    from config import settings
    dead_msg = {
        "original": msg,
        "reason": reason,
        "failed_at": time.time(),
    }
    redis = await get_redis()
    if redis:
        try:
            await redis.rpush(settings.WRITER_DEAD_QUEUE_KEY, json.dumps(dead_msg, default=str))
            return True
        except Exception as e:
            logger.error(f"[RedisQueue] push_dead Redis 失败,降级写本地文件: {e}")
    # P0修复: Redis 不可达时降级写本地文件,避免消息永久丢失
    try:
        dead_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "dead_letter.jsonl"
        )
        os.makedirs(os.path.dirname(dead_file), exist_ok=True)
        with open(dead_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(dead_msg, default=str, ensure_ascii=False) + "\n")
        logger.warning(f"[RedisQueue] 死信已写入本地文件: {dead_file}")
        return True
    except Exception as e:
        logger.error(f"[RedisQueue] push_dead 本地文件也失败(消息已丢失): {e}")
        return False


async def length() -> int:
    """获取队列长度(mon_bot 监控积压)。
    返回 -1 表示 Redis 不可达(调用方需特殊处理此哨兵值)。
    """
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
    """写入缓存(读路径回填,带 TTL)。

    Args:
        ttl: 缓存 TTL 秒数(调用方应从 settings.WRITER_CACHE_TTL_* 传入对应分级 TTL,
             默认 5 秒仅为函数级兜底,不应在生产路径依赖此默认值)
    """
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
    """失效缓存(写操作后清除对应读缓存,保证一致性)。
    P2修复: 复用 delete() 避免重复逻辑(含空 key 检查)。
    """
    return await delete(key)


async def close_redis():
    """关闭 Redis 连接(进程退出时调用)。
    P3修复: 重置所有全局状态,使下次 get_redis() 能重新初始化。
    """
    global _redis_client, _redis_available, _redis_init_attempted, _redis_last_attempt_ts
    if _redis_client:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
    _redis_client = None
    _redis_available = False
    _redis_init_attempted = False
    _redis_last_attempt_ts = 0
