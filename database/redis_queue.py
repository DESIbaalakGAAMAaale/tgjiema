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

# R34 P1-5: fcntl 仅在 Unix/Linux 可用,Windows 跳过文件锁(VPS 部署目标为 Linux)
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # Windows 环境,跳过文件锁


_redis_client: Any = None
_redis_init_attempted: bool = False
_redis_available: bool = False
_redis_last_attempt_ts: float = 0
_REDIS_RETRY_INTERVAL = 60.0  # 失败后 60 秒重试
_redis_init_lock = None  # asyncio.Lock,延迟初始化(P1修复: 防止并发 race)
_consumer_group_ensured: bool = False  # Consumer Group 是否已创建

# R36 H1: 死信重入主 Stream 的 Lua 脚本(原子 XADD + XDEL)
# KEYS[1] = 主 Stream key, KEYS[2] = 死信 Stream key
# ARGV[1] = 消息 JSON, ARGV[2] = 死信 msg_id
# 顺序: 先 XADD 回主 Stream,成功后才 XDEL 死信,避免消息丢失
_REQUEUE_LUA_SCRIPT = """
local new_id = redis.call('XADD', KEYS[1], '*', 'data', ARGV[1])
if new_id then
  redis.call('XDEL', KEYS[2], ARGV[2])
  return new_id
end
return false
"""


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
               redis_key: str = "", message_id: str = "",
               attempts: int = 0) -> bool:
    """推入写操作到 Redis Stream(XADD)。

    R33修复: 每条消息携带 message_id (UUID),用于幂等去重。
    R35 P1-1修复: 消息体携带 attempts 字段,DLQ 重试回主 Stream 时
    递增 attempts,避免每次重试都从 0 开始导致无限重试。

    Args:
        op_type: 操作类型(upsert/update/delete/insert)
        table: 目标 SQLite 表名
        method_name: 调用的 cache_store 方法名(Writer 用于分派)
        data: 方法参数字典
        redis_key: 关联的 Redis 缓存 key(Writer 写完后 DEL,空字符串表示无缓存)
        message_id: 幂等键(UUID),空则自动生成
        attempts: 当前重试次数(0=首次入队,>0 表示从 DLQ 重试回主 Stream 的次数)

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
            # R35 P1-1: 携带 attempts 字段,让失败后 push_dead 能读取当前重试次数
            "attempts": int(attempts) if attempts else 0,
        }
        await redis.xadd(
            settings.WRITER_STREAM_KEY,
            {"data": json.dumps(msg, default=str)},
            id="*",  # Redis 自动生成有序 ID
            # R35 P0-2: 不设置 maxlen — MAXLEN 会裁剪尚未 ACK 的 pending 消息正文,
            # 导致 XAUTOCLAIM 无法恢复。改由独立 safe_trim() 维护。
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


async def safe_trim() -> int:
    """安全裁剪 Stream(XTRIM MINID),不删除任何 Consumer Group 的 pending 消息。

    R35 P0-2 修复: MAXLEN 裁剪可能删除尚未 ACK 的 pending 消息正文。
    改用 MINID 策略:只裁剪所有 Consumer Group 的最小 pending ID 之前的消息。

    R36 H2 修复: 原版仅查询 WRITER_CONSUMER_GROUP 单一 group 的 pending,
    多 group 场景下可能裁剪其他 group 未消费消息。现改用 XINFO GROUPS 获取
    全部 group,逐组收集最小 pending ID / last-delivered-id,取全局最保守水位。

    流程:
    1. XINFO GROUPS 获取 Stream 上所有 Consumer Group
    2. 对每个 group 查询 XPENDING 概要,取该 group 最小 pending ID
    3. 收集所有 group 的最小 pending ID,取全局最小(最保守)
    4. 对无 pending 的 group,用 last-delivered-id 作为保守水位
    5. 额外保留 24 小时安全窗口(不裁剪最近的消息)
    6. XTRIM MINID < 安全水位

    Returns:
        被裁剪的消息数,Redis 不可达或查询失败时返回 0(保守不裁剪)
    """
    redis = await get_redis()
    if not redis:
        return 0
    try:
        from config import settings
        # 1. XINFO GROUPS 获取所有 Consumer Group
        try:
            groups = await redis.xinfo_groups(settings.WRITER_STREAM_KEY)
        except Exception as e:
            # 查询失败:保守不裁剪(避免删掉未消费消息)
            logger.debug(f"[RedisQueue] safe_trim xinfo_groups 失败,不裁剪: {e}")
            return 0

        # groups 为空或 None:无 Consumer Group,保守不裁剪
        if not groups:
            logger.debug("[RedisQueue] safe_trim 无 Consumer Group,不裁剪")
            return 0

        # 2. 逐组收集最小 pending ID 和 last-delivered-id
        global_min_pending = None        # 全局最小 pending ID(所有 group 中最小的)
        global_min_delivered = None      # 全局最小 last-delivered-id(无 pending 时的保守水位)
        has_any_pending = False

        for group_info in groups:
            # redis-py xinfo_groups 返回 list[dict],含 name/consumers/pending/last-delivered-id
            # decode_responses 可能影响格式,兼容 dict 与 tuple 两种
            group_name = None
            last_delivered = None
            if isinstance(group_info, dict):
                group_name = group_info.get('name')
                last_delivered = group_info.get('last-delivered-id')
            elif isinstance(group_info, (list, tuple)) and len(group_info) >= 1:
                # 旧版 redis-py 可能返回 tuple[name, consumers, pending, ...]
                group_name = group_info[0]
            if not group_name:
                continue

            # 查询该 group 的 pending 概要
            try:
                pending_info = await redis.xpending(
                    settings.WRITER_STREAM_KEY, group_name,
                )
            except Exception:
                pending_info = None

            # 解析 pending_info(redis-py 返回 dict 或 tuple,两种格式都支持)
            group_pending_count = 0
            group_min_pending = None
            if pending_info and isinstance(pending_info, dict):
                group_pending_count = pending_info.get('pending', 0) or 0
                if group_pending_count:
                    group_min_pending = pending_info.get('min')
            elif pending_info and isinstance(pending_info, (tuple, list)) and len(pending_info) >= 2:
                group_pending_count = pending_info[0] or 0
                if group_pending_count:
                    group_min_pending = pending_info[1]

            # 收集该 group 的最小 pending ID,取全局最小(最保守)
            if group_pending_count and group_min_pending:
                has_any_pending = True
                if global_min_pending is None or _stream_id_less(
                    str(group_min_pending), str(global_min_pending)
                ):
                    global_min_pending = str(group_min_pending)

            # 收集该 group 的 last-delivered-id(无 pending 时的保守水位)
            if last_delivered:
                if global_min_delivered is None or _stream_id_less(
                    str(last_delivered), str(global_min_delivered)
                ):
                    global_min_delivered = str(last_delivered)

        # 3. 计算安全裁剪水位
        # 优先用 pending ID(最保守,保留未 ACK 消息)
        # 其次用 last-delivered-id(该 group 已处理到此位置,裁剪之前的消息安全)
        # 都没有:保守不裁剪
        if has_any_pending and global_min_pending:
            safe_min_id = _compute_safe_trim_id(global_min_pending, retention_hours=24)
        elif global_min_delivered:
            safe_min_id = _compute_safe_trim_id(global_min_delivered, retention_hours=24)
        else:
            # 无 pending 也无 last-delivered-id:保守不裁剪
            return 0

        if not safe_min_id:
            return 0

        trimmed = await redis.xtrim(
            settings.WRITER_STREAM_KEY,
            minid=safe_min_id,
            approximate=False,  # 精确裁剪,不使用 ~
        )
        return trimmed
    except Exception as e:
        logger.debug(f"[RedisQueue] safe_trim 异常: {e}")
        return 0


def _compute_safe_trim_id(min_pending_id: Optional[str], retention_hours: int = 24) -> str:
    """计算安全裁剪水位 ID。

    R35 P0-2: 保留 pending 消息 + 额外安全窗口。

    Args:
        min_pending_id: 最小的 pending 消息 ID(如 "1700000000-0"),None 表示无 pending
        retention_hours: 安全保留窗口(小时),不裁剪此时间内的消息

    Returns:
        安全裁剪水位 ID(如 "1699999000-0"),裁剪此 ID 之前的消息
    """
    now_ts = int(time.time())
    safe_ts = now_ts - retention_hours * 3600

    if min_pending_id:
        try:
            # Stream ID 格式: <timestamp>-<seq>
            pending_ts = int(min_pending_id.split('-')[0])
            # 取 min(pending_ts, safe_ts) 的较小值,确保不裁剪 pending
            # 但不能裁剪 pending 本身,所以用 pending_ts - 1
            actual_safe_ts = min(pending_ts - 1, safe_ts)
        except (ValueError, IndexError):
            actual_safe_ts = safe_ts
    else:
        actual_safe_ts = safe_ts

    return f"{actual_safe_ts}-0"


def _stream_id_less(a: str, b: str) -> bool:
    """比较两个 Redis Stream ID,判断 a < b。

    R36 H2: safe_trim 多 group 场景取全局最小 pending ID 时使用。

    Stream ID 格式: <timestamp>-<seq>(如 "1700000000-0")
    先比较 timestamp,相同则比较 seq。

    Args:
        a, b: 两个 Stream ID 字符串

    Returns:
        True 表示 a < b, False 表示 a >= b 或解析失败(保守认为不小于)
    """
    try:
        ta, sa = str(a).split('-', 1)
        tb, sb = str(b).split('-', 1)
        ta_i, sa_i, tb_i, sb_i = int(ta), int(sa), int(tb), int(sb)
        if ta_i != tb_i:
            return ta_i < tb_i
        return sa_i < sb_i
    except (ValueError, IndexError):
        # 解析失败:保守认为 a 不小于 b(不更新全局最小)
        return False


async def trim_stream() -> int:
    """[已废弃] 使用 safe_trim() 替代。

    R35 P0-2: 此方法使用 MAXLEN 裁剪,可能删除 pending 消息正文。
    保留为别名,内部调用 safe_trim()。

    Returns:
        被裁剪的消息数,Redis 不可达时返回 0
    """
    return await safe_trim()


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
                    attempts: int = 0, permanent: bool = False) -> bool:
    """推入死信队列(带重试闭环)。

    R33 P1修复: 死信消息携带 attempts/max_attempts,支持延迟重试。
    R33 P0修复: Redis 不可达时降级写本地文件 dead_letter.jsonl,避免消息永久丢失。

    R35 P1-1 修复: 消息协议携带 attempts 字段,此函数从原 msg 读取 existing attempts
    并 +1(避免 DLQ 重试回主 Stream 后 attempts 被重置为 0 导致无限重试)。
    显式 attempts 参数优先(向后兼容,用于 db_writer 标记永久错误时 attempts=99)。
    permanent=True 时直接设为永久死信(next_retry_at=None),用于 TypeError 等不可重试错误。

    Args:
        msg: 原始消息字典(或任意可序列化对象)。如果 msg 是 dict 且含 "attempts" 字段,
            则从中读取当前重试次数(existing_attempts)
        reason: 失败原因(记录在消息中,便于排查)
        message_id: 幂等键(用于去重)
        attempts: 显式指定重试次数(向后兼容,优先于 msg 中的 existing attempts)。
            - 默认 0 时,使用 existing_attempts + 1(从 msg 读取并递增)
            - 非 0 时,使用此值(让 db_writer 可以显式标记永久错误)
        permanent: True 表示永久死信(next_retry_at=None,不重试),用于 TypeError 等
            不可重试错误。True 时 attempts 设为 max_attempts(确保下次直接被永久隔离)。

    Returns:
        True 推入成功, False 失败(此时已降级写本地文件)
    """
    from config import settings
    max_attempts = getattr(settings, "WRITER_DEAD_MAX_ATTEMPTS", 3)
    retry_delay = getattr(settings, "WRITER_DEAD_RETRY_DELAY", 60)

    # R35 P1-1: 从 msg 中读取 existing attempts(用于 DLQ 重试回主 Stream 后递增)
    existing_attempts = 0
    if isinstance(msg, dict):
        try:
            existing_attempts = int(msg.get("attempts", 0) or 0)
        except (TypeError, ValueError):
            existing_attempts = 0

    # 计算最终 attempts:
    # - permanent=True: 直接设为 max_attempts(下次就达到上限,永久隔离)
    # - 显式 attempts > 0(向后兼容): 用显式值(db_writer 标记永久错误时 attempts=99)
    # - attempts=0(默认): 从 msg 读取并 +1(DLQ 重试回主 Stream 后递增)
    if permanent:
        new_attempts = max_attempts
    elif attempts > 0:
        new_attempts = attempts
    else:
        new_attempts = existing_attempts + 1

    # 判断是否为永久死信(permanent 显式标记 或 attempts >= max_attempts)
    is_permanent = permanent or new_attempts >= max_attempts

    dead_msg = {
        "original": msg,
        "reason": reason,
        "message_id": message_id or str(uuid.uuid4()),
        "attempts": new_attempts,
        "max_attempts": max_attempts,
        "failed_at": time.time(),
        # 永久死信: next_retry_at=None(DLQWorker 不再重试,保留等待人工审核)
        "next_retry_at": None if is_permanent else time.time() + retry_delay,
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
        # R34 P1-5: 使用文件锁避免并发写入冲突,权限 0600 保护敏感数据
        # os.open() 可直接设置文件权限为 0600(open() 受 umask 影响)
        line = json.dumps(dead_msg, default=str, ensure_ascii=False) + "\n"
        fd = os.open(dead_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if _fcntl is not None:
                # 获取排他锁(Linux/Mac); Windows 跳过
                _fcntl.flock(fd, _fcntl.LOCK_EX)
            os.write(fd, line.encode('utf-8'))
            os.fsync(fd)  # 确保数据落盘
        finally:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            os.close(fd)
        logger.warning(f"[RedisQueue] 死信已写入本地文件: {dead_file}")
        return True
    except Exception as e:
        logger.error(f"[RedisQueue] push_dead 本地文件也失败(消息已丢失): {e}")
        return False


async def get_dead_messages(count: int = 100) -> list[tuple[str, dict]]:
    """读取死信队列消息(XRANGE)。

    R34 P1-1: 供 DLQ Worker 消费死信 Stream,实现重试闭环。

    Args:
        count: 最多读取的消息数(默认 100)

    Returns:
        [(msg_id, dead_msg_dict), ...] — 死信消息列表。
        Redis 不可达或解析失败时返回空列表。
    """
    redis = await get_redis()
    if not redis:
        return []
    try:
        from config import settings
        result = await redis.xrange(
            settings.WRITER_DEAD_STREAM_KEY,
            count=count,
        )
        messages = []
        for msg_id, fields in result:
            raw = fields.get("data", "")
            try:
                dead_msg = json.loads(raw)
                messages.append((msg_id, dead_msg))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[RedisQueue] 死信消息 JSON 解析失败: {e}, raw={raw!r}")
        return messages
    except Exception as e:
        logger.debug(f"[RedisQueue] get_dead_messages 异常: {e}")
        return []


async def delete_dead_message(msg_id: str) -> bool:
    """从死信队列删除消息(XDEL)。

    R34 P1-1: DLQ Worker 重试成功后调用,从死信 Stream 移除已重试的消息。

    Args:
        msg_id: 死信 Stream 消息 ID

    Returns:
        True 删除成功, False 失败(Redis 不可达或 msg_id 为空)
    """
    redis = await get_redis()
    if not redis or not msg_id:
        return False
    try:
        from config import settings
        await redis.xdel(settings.WRITER_DEAD_STREAM_KEY, msg_id)
        return True
    except Exception as e:
        logger.debug(f"[RedisQueue] delete_dead_message 异常: {e}")
        return False


async def requeue_from_dlq(dead_msg_id: str, msg_data: dict) -> bool:
    """R36 H1: 原子地将死信消息重新入队主 Stream 并从死信 Stream 删除。

    使用 Lua 脚本保证 XADD(回主 Stream)+ XDEL(死信 Stream)原子性,
    替代旧的非原子双操作(push() + delete_dead_message())。

    解决的问题:
    - 旧方案 XADD 成功但 XDEL 失败 → 死信重复重入,依赖 inbox 去重(脆弱)
    - 旧方案 XADD 失败后仍可能 XDEL → 消息永久丢失(P0-3 已加返回值检查,但非原子)
    - 新方案 Lua 脚本内顺序执行: 先 XADD 成功才 XDEL,任一失败脚本回滚(Redis 单线程原子)

    Args:
        dead_msg_id: 死信 Stream 中的消息 ID(XDEL 目标)
        msg_data: 要 XADD 回主 Stream 的消息字典(需包含 op_type/table/method_name/data
                  等标准字段,与 push() 输出格式一致)

    Returns:
        True 重入成功(Lua 返回新 msg_id), False 失败
        (Redis 不可达 / 参数为空 / Lua 执行失败 / XADD 失败返回 false)
    """
    redis = await get_redis()
    if not redis:
        return False
    if not dead_msg_id or not msg_data:
        return False
    try:
        from config import settings
        msg_json = json.dumps(msg_data, default=str, ensure_ascii=False)
        # eval(script, numkeys, *keys_and_args)
        # KEYS[1]=主 Stream, KEYS[2]=死信 Stream, ARGV[1]=消息 JSON, ARGV[2]=死信 msg_id
        result = await redis.eval(
            _REQUEUE_LUA_SCRIPT,
            2,  # numkeys
            settings.WRITER_STREAM_KEY,      # KEYS[1] 主 Stream
            settings.WRITER_DEAD_STREAM_KEY,  # KEYS[2] 死信 Stream
            msg_json,                          # ARGV[1] 消息 JSON
            dead_msg_id,                       # ARGV[2] 死信 msg_id
        )
        # Lua 返回 new_id(字符串,真值)或 false(redis-py 转为 None)
        if result:
            return True
        return False
    except Exception as e:
        logger.warning(f"[RedisQueue] requeue_from_dlq 失败: {e}")
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
