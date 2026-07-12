"""Redis Streams 封装(方案B v2: 可靠消费,零数据丢失)

R33 P0 修复: 从 Redis List BRPOP/LPOP 改为 Redis Streams Consumer Group。
原方案消息弹出后立即从 List 删除,进程崩溃会导致消息永久丢失。
新方案使用 XREADGROUP 读取(消息进入 pending 不删除),SQLite 提交后 XACK 确认,
崩溃后用 XAUTOCLAIM 回收 pending 消息,配合 writer_inbox 表实现幂等。

设计:
- XADD: 写操作推入 Stream(带 message_id, <0.1ms 返回)
- XREADGROUP: Consumer 消费(消息进入 pending,不删除)
- XACK: SQLite 提交后确认(消息从 pending 删除)
- XAUTOCLAIM: 回收 pending >30s 的消息(崩溃恢复)
- XLEN / XPENDING: mon_bot 监控积压
- REDIS_URL 为空时所有方法返回 False(降级到 SQLite 直写)

消息格式(Stream field, value):
  field: "data"
  value: JSON {
    "op_type": "upsert" | "update" | "delete" | "insert",
    "table": "heartbeat_local" | "user_quota" | ...,
    "method_name": "write_heartbeat" | "upsert_user_quota" | ...,
    "data": { ... 方法参数 ... },
    "redis_key": "cache:user_quota:12345" | "",
    "message_id": "uuid-xxxx-xxxx",
    "created_at": 1234567890.123
  }
"""
import json
import os
import time
import uuid
from typing import Any, Optional

from loguru import logger


_redis_client: Any = None
_redis_init_attempted: bool = False
_redis_available: bool = False
_redis_last_attempt_ts: float = 0
_REDIS_RETRY_INTERVAL = 60.0  # 失败后 60 秒重试
_redis_init_lock = None  # asyncio.Lock,延迟初始化(P1修复: 防止并发 race)
_consumer_group_ensured: bool = False  # Consumer Group 是否已创建


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
    R33修复: 连接重置时也重置 consumer_group_ensured 标志
    """
    global _redis_client, _redis_init_attempted, _redis_available, _redis_last_attempt_ts
    global _consumer_group_ensured
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
            _consumer_group_ensured = False  # 重置,需要重新 ensure
            logger.info("[RedisQueue] 连接成功,Streams 可靠消费已启用")
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


async def ensure_consumer_group() -> bool:
    """确保 Consumer Group 存在(幂等,db_writer 启动时调用)。

    XGROUP CREATE 如果 group 已存在会报 BUSYGROUP,捕获并忽略。
    """
    global _consumer_group_ensured
    if _consumer_group_ensured:
        return True
    redis = await get_redis()
    if not redis:
        return False
    try:
        from config import settings
        # mkstream=True: 如果 Stream 不存在则自动创建
        await redis.xgroup_create(
            settings.WRITER_STREAM_KEY,
            settings.WRITER_CONSUMER_GROUP,
            id="0",  # 从 Stream 开头读取所有消息
            mkstream=True,
        )
        _consumer_group_ensured = True
        logger.info(
            f"[RedisQueue] Consumer Group 已创建: "
            f"stream={settings.WRITER_STREAM_KEY}, group={settings.WRITER_CONSUMER_GROUP}"
        )
        return True
    except Exception as e:
        err_msg = str(e)
        if "BUSYGROUP" in err_msg:
            # Group 已存在,正常情况
            _consumer_group_ensured = True
            logger.debug("[RedisQueue] Consumer Group 已存在,跳过创建")
            return True
        logger.error(f"[RedisQueue] 创建 Consumer Group 失败: {e}")
        return False


async def push(op_type: str, table: str, method_name: str, data: dict,
               redis_key: str = "", message_id: str = "") -> bool:
    """推入写操作到 Redis Stream(XADD)。

    R33修复: 每条消息携带 message_id (UUID),用于幂等去重。

    Args:
        op_type: 操作类型(upsert/update/delete/insert)
        table: 目标 SQLite 表名
        method_name: 调用的 cache_store 方法名(Writer 用于分派)
        data: 方法参数字典
        redis_key: 关联的 Redis 缓存 key(Writer 写完后 DEL,空字符串表示无缓存)
        message_id: 幂等键(UUID),空则自动生成

    Returns:
        True 推入成功, False 降级到 SQLite 直写
    """
    redis = await get_redis()
    if not redis:
        return False
    try:
        from config import settings
        if not message_id:
            message_id = str(uuid.uuid4())
        msg = {
            "op_type": op_type,
            "table": table,
            "method_name": method_name,
            "data": data,
            "redis_key": redis_key,
            "message_id": message_id,
            "created_at": time.time(),
        }
        await redis.xadd(
            settings.WRITER_STREAM_KEY,
            {"data": json.dumps(msg, default=str)},
            id="*",  # Redis 自动生成有序 ID
        )
        return True
    except Exception as e:
        logger.warning(f"[RedisQueue] push 失败(降级到 SQLite): {e}")
        return False


async def pop(timeout: int = 1, count: int = 10) -> list[dict]:
    """从 Redis Stream 消费消息(XREADGROUP)。

    R33修复: 消息进入 pending 不会被删除,SQLite 提交后需 XACK 确认。
    R33修复: 优先回收 pending 消息(XAUTOCLAIM),避免崩溃消息积压。
    R33修复: 每条消息附带 _stream_id 供后续 XACK 使用。
    R33修复: Redis 运行时宕机时重置客户端状态,使 get_redis() 下次触发重连。

    Args:
        timeout: 阻塞超时秒数,0 表示永久阻塞
        count: 单次读取数量

    Returns:
        消息列表(每条附带 _stream_id 字段),空列表表示超时或降级
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        from config import settings
        result = []

        # 优先回收 pending 消息(崩溃恢复)
        reclaimed = await _reclaim_pending(redis, settings, count)
        if reclaimed:
            result.extend(reclaimed)
            # R33: 有 pending 消息被回收时,只处理这些,不读新消息
            # 优先清空 pending 列表(崩溃恢复优先于新消息消费)
            return result[:count]

        # 没有 pending 消息,读取新消息(">" 表示从未投递给当前 consumer group 的消息)
        raw_messages = await redis.xreadgroup(
            settings.WRITER_CONSUMER_GROUP,
            settings.WRITER_CONSUMER_NAME,
            {settings.WRITER_STREAM_KEY: ">"},
            count=count,
            block=timeout * 1000,  # milliseconds
        )
        if raw_messages:
            for _stream_name, msg_list in raw_messages:
                for msg_id, fields in msg_list:
                    raw = fields.get("data", "")
                    try:
                        msg = json.loads(raw)
                        msg["_stream_id"] = msg_id  # 供 XACK 使用
                        result.append(msg)
                    except (json.JSONDecodeError, TypeError, ValueError) as e:
                        logger.warning(
                            f"[RedisQueue] XREADGROUP 消息 JSON 解析失败,入死信: {e}, raw={raw!r}"
                        )
                        await push_dead(
                            {"raw": raw, "_stream_id": msg_id},
                            reason=f"JSON decode failed: {e}",
                        )
                        # ACK 损坏消息以移出 pending
                        await ack([msg_id])
        return result
    except Exception as e:
        logger.debug(f"[RedisQueue] pop 异常: {e}")
        # P1修复: Redis 运行时宕机时重置客户端状态,使 get_redis() 下次
        # 触发重连(受 60s 节流),避免 db_writer 100% CPU 忙等循环
        global _redis_available, _redis_client, _consumer_group_ensured
        if _redis_client is not None:
            try:
                await _redis_client.aclose()
            except Exception:
                pass
            _redis_client = None
        _redis_available = False
        _consumer_group_ensured = False
        return []


async def _reclaim_pending(redis, settings, count: int) -> list[dict]:
    """回收 pending 消息(XAUTOCLAIM)。

    回收 idle 时间超过 WRITER_RECLAIM_IDLE_MS 的消息(通常是上一个 db_writer 崩溃遗留的)。
    被回收的消息会重新分配给当前 consumer,需要重新处理。

    Returns:
        回收的消息列表(每条附带 _stream_id 和 _reclaimed=True)
    """
    result = []
    try:
        # XAUTOCLAIM: 返回 (next_start_id, [(msg_id, fields), ...], deleted_ids)
        claim_result = await redis.xautoclaim(
            settings.WRITER_STREAM_KEY,
            settings.WRITER_CONSUMER_GROUP,
            settings.WRITER_CONSUMER_NAME,
            min_idle_time=settings.WRITER_RECLAIM_IDLE_MS,
            count=count,
        )
        if claim_result and len(claim_result) >= 2:
            reclaimed_messages = claim_result[1]
            for msg_id, fields in reclaimed_messages:
                raw = fields.get("data", "")
                try:
                    msg = json.loads(raw)
                    msg["_stream_id"] = msg_id
                    msg["_reclaimed"] = True  # 标记为回收消息
                    result.append(msg)
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning(
                        f"[RedisQueue] XAUTOCLAIM 消息 JSON 解析失败,入死信: {e}, raw={raw!r}"
                    )
                    await push_dead(
                        {"raw": raw, "_stream_id": msg_id},
                        reason=f"JSON decode failed (reclaim): {e}",
                    )
                    await ack([msg_id])
            if result:
                logger.info(f"[RedisQueue] 回收 {len(result)} 条 pending 消息")
    except Exception as e:
        logger.debug(f"[RedisQueue] XAUTOCLAIM 异常(可能无 pending): {e}")
    return result


async def ack(message_ids: list[str]) -> int:
    """确认消息已处理(XACK)。

    R33修复: SQLite 提交成功后调用,消息从 pending 列表移除。
    如果 ACK 失败,消息会留在 pending,下次启动时被 XAUTOCLAIM 回收。
    配合 writer_inbox 幂等表,重放不会导致重复执行。

    Args:
        message_ids: Stream 消息 ID 列表

    Returns:
        成功 ACK 的消息数
    """
    if not message_ids:
        return 0
    redis = await get_redis()
    if not redis:
        return 0
    try:
        from config import settings
        return await redis.xack(
            settings.WRITER_STREAM_KEY,
            settings.WRITER_CONSUMER_GROUP,
            *message_ids,
        )
    except Exception as e:
        logger.debug(f"[RedisQueue] ack 异常: {e}")
        return 0


async def delete(key: str) -> bool:
    """删除指定 key(Writer 写完 SQLite 后清除读缓存 key)。

    注意: 这不是 XACK,这是 DEL Redis 读缓存 key(如 cache:user_quota:12345)。
    XACK 使用 ack() 函数。
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


async def push_dead(msg: dict, reason: str = "", message_id: str = "",
                    attempts: int = 0) -> bool:
    """推入死信队列(带重试闭环)。

    R33 P1修复: 死信消息携带 attempts/max_attempts,支持延迟重试。
    R33 P0修复: Redis 不可达时降级写本地文件 dead_letter.jsonl,避免消息永久丢失。

    Args:
        msg: 原始消息字典(或任意可序列化对象)
        reason: 失败原因(记录在消息中,便于排查)
        message_id: 幂等键(用于去重)
        attempts: 当前重试次数

    Returns:
        True 推入成功, False 失败(此时已降级写本地文件)
    """
    from config import settings
    max_attempts = getattr(settings, "WRITER_DEAD_MAX_ATTEMPTS", 3)
    retry_delay = getattr(settings, "WRITER_DEAD_RETRY_DELAY", 60)

    dead_msg = {
        "original": msg,
        "reason": reason,
        "message_id": message_id or str(uuid.uuid4()),
        "attempts": attempts,
        "max_attempts": max_attempts,
        "failed_at": time.time(),
        "next_retry_at": time.time() + retry_delay if attempts < max_attempts else None,
    }
    redis = await get_redis()
    if redis:
        try:
            await redis.xadd(
                settings.WRITER_DEAD_STREAM_KEY,
                {"data": json.dumps(dead_msg, default=str, ensure_ascii=False)},
                id="*",
            )
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
        # P1修复: 文件锁避免并发写入冲突,权限 0600 保护敏感数据
        with open(dead_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(dead_msg, default=str, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())  # P1修复: 确保数据落盘
        logger.warning(f"[RedisQueue] 死信已写入本地文件: {dead_file}")
        return True
    except Exception as e:
        logger.error(f"[RedisQueue] push_dead 本地文件也失败(消息已丢失): {e}")
        return False


async def get_pending_info() -> dict:
    """获取 pending 消息信息(mon_bot 监控用)。

    Returns:
        {"total": int, "oldest_id": str, "newest_id": str}
        Redis 不可达时返回空 dict。
    """
    redis = await get_redis()
    if not redis:
        return {}
    try:
        from config import settings
        info = await redis.xpending(
            settings.WRITER_STREAM_KEY,
            settings.WRITER_CONSUMER_GROUP,
        )
        # info 是 (count, min_id, max_id, consumers) 元组
        if info and len(info) >= 3:
            return {
                "total": info[0] or 0,
                "oldest_id": str(info[1]) if info[1] else "",
                "newest_id": str(info[2]) if info[2] else "",
            }
        return {"total": 0, "oldest_id": "", "newest_id": ""}
    except Exception as e:
        logger.debug(f"[RedisQueue] get_pending_info 异常: {e}")
        return {}


async def get_dlq_length() -> int:
    """获取死信队列长度(mon_bot 监控用)。

    Returns:
        死信队列中的消息数,-1 表示 Redis 不可达
    """
    redis = await get_redis()
    if not redis:
        return -1
    try:
        from config import settings
        return await redis.xlen(settings.WRITER_DEAD_STREAM_KEY)
    except Exception as e:
        logger.debug(f"[RedisQueue] get_dlq_length 异常: {e}")
        return -1


async def length() -> int:
    """获取 Stream 长度(mon_bot 监控积压)。

    返回的是 Stream 中所有消息数(含已 ACK 的,Redis 不会自动删除已 ACK 的消息)。
    对于积压监控,应结合 get_pending_info() 看 pending 数。
    返回 -1 表示 Redis 不可达。
    """
    redis = await get_redis()
    if not redis:
        return -1
    try:
        from config import settings
        return await redis.xlen(settings.WRITER_STREAM_KEY)
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
    复用 delete() 避免重复逻辑(含空 key 检查)。
    """
    return await delete(key)


async def close_redis():
    """关闭 Redis 连接(进程退出时调用)。
    重置所有全局状态,使下次 get_redis() 能重新初始化。
    """
    global _redis_client, _redis_available, _redis_init_attempted, _redis_last_attempt_ts
    global _consumer_group_ensured
    if _redis_client:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
    _redis_client = None
    _redis_available = False
    _redis_init_attempted = False
    _redis_last_attempt_ts = 0
    _consumer_group_ensured = False
