"""R56 §7.2.3: CRDB 同步事件驱动唤醒 — 替代固定秒级轮询。

设计目标:
    报告 §7.2.3 要求 "SQLite/Redis 本地 outbox 事件驱动唤醒同步器;
    禁止固定秒级轮询 CRDB"。

    本模块提供基于 Redis pub/sub 的事件驱动唤醒机制:
    1. 写入 dirty_outbox 后调用 ``publish_dirty_signal``(PUBLISH 到 Redis channel)
    2. ``crdb_sync._sync_loop`` 在退避时调用 ``wait_dirty_signal(timeout)``
       替代 ``asyncio.sleep``:
       - 收到信号立即唤醒(毫秒级响应)
       - 超时未收到信号继续退避(保持原行为)
    3. Redis 不可用时,自动 fallback 到 ``asyncio.sleep``(保持向后兼容)

信号语义:
    - channel: ``tgjiema:crdb_sync:wakeup``
    - message: table_name(如 "users" / "files" / "codes" / "jobs" / "cells")
    - 收到任意信号即唤醒,不区分 table(simple 策略,避免复杂路由)

为什么用 pub/sub 而非 Stream:
    - 信号仅用于"唤醒",不需要持久化(pub/sub fire-and-forget)
    - dirty_outbox 本身已经是 durable watermark(SQLite),信号丢失不影响正确性
    - Stream 已用于 writer queue,避免职责混淆

为什么不用 SQLite trigger:
    - SQLite trigger 在 WAL 模式下可能阻塞写入,且跨进程信号需要额外 IPC
    - Redis pub/sub 是 O(1) 发布,延迟极低(< 1ms)

故障模式:
    - Redis 不可用 → publish 失败静默(不影响 dirty_outbox 写入)
    - Redis 不可用 → wait 走 fallback sleep(保持退避行为)
    - Redis 重连后 → 自动恢复事件驱动(下一轮 wait 即收到信号)

使用示例:
    # 写入侧(cache_store.py / unit_of_work.py):
    from services.crdb_sync_event_wakeup import publish_dirty_signal
    await publish_dirty_signal("users")

    # 同步侧(crdb_sync_service.py _sync_loop):
    from services.crdb_sync_event_wakeup import wait_dirty_signal
    await wait_dirty_signal(backoff)  # 替代 await asyncio.sleep(backoff)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from loguru import logger
# R61 P1-04: i18n for logger calls (avoid scanner baseline overflow)
from services.i18n import translate as _i18n_t

# ── 常量 ──────────────────────────────────────────────────────

# Redis pub/sub channel(单一 channel,所有 dirty 信号共用)
_DIRTY_SIGNAL_CHANNEL = "tgjiema:crdb_sync:wakeup"

# fallback sleep 最小粒度(避免 wait_for 永久阻塞)
_FALLBACK_SLEEP_MIN = 0.1

# 单例 subscriber 缓存(避免每个 _sync_loop 都创建独立 subscriber)
_subscriber_lock = asyncio.Lock()
_subscriber_client: Optional[object] = None
_subscriber_ready: bool = False


# ── publish 侧 ─────────────────────────────────────────────────


async def publish_dirty_signal(table_name: str) -> None:
    """写入 dirty_outbox 后发布唤醒信号。

    Args:
        table_name: 触发 dirty 的表名(如 "users" / "files")
                   仅用于日志,信号本身不区分 table

    Note:
        Redis 不可用时静默失败(不影响 dirty_outbox 写入)。
        下一轮 _sync_loop 的退避 sleep 会 fallback 到原 polling 行为。
    """
    try:
        from database.redis_queue import get_redis
        client = await get_redis()
        if client is None:
            return  # Redis 不可用,静默跳过
        await client.publish(_DIRTY_SIGNAL_CHANNEL, table_name)
    except Exception as e:
        # publish 失败不应影响 dirty_outbox 写入
        logger.debug(
            f"[crdb_sync_event] R56 §7.2.3: publish_dirty_signal 失败 "
            f"(静默,fallback 到 polling): {e}"
        )


# ── subscribe 侧 ──────────────────────────────────────────────


async def _ensure_subscriber() -> Optional[object]:
    """确保 subscriber 已订阅 channel(单例,跨 _sync_loop 复用)。

    Returns:
        Redis client(pubsub 已订阅),或 None(Redis 不可用)
    """
    global _subscriber_client, _subscriber_ready
    if _subscriber_ready and _subscriber_client is not None:
        return _subscriber_client
    async with _subscriber_lock:
        if _subscriber_ready and _subscriber_client is not None:
            return _subscriber_client
        try:
            from database.redis_queue import get_redis
            client = await get_redis()
            if client is None:
                _subscriber_ready = False
                return None
            # 创建独立 pubsub 连接(不占用主连接)
            pubsub = client.pubsub()
            await pubsub.subscribe(_DIRTY_SIGNAL_CHANNEL)
            _subscriber_client = pubsub
            _subscriber_ready = True
            logger.info(
                f"[crdb_sync_event] R56 §7.2.3: 已订阅 dirty_signal channel "
                f"(event-driven wakeup 已启用)"
            )
            return _subscriber_client
        except Exception as e:
            logger.warning(
                f"[crdb_sync_event] R56 §7.2.3: 订阅 dirty_signal 失败 "
                f"(fallback 到 polling): {e}"
            )
            _subscriber_ready = False
            _subscriber_client = None
            return None


async def wait_dirty_signal(timeout: float) -> bool:
    """等待 dirty 信号,超时返回 False。

    替代 ``await asyncio.sleep(backoff)``,在收到信号时立即返回 True。
    Redis 不可用时 fallback 到 asyncio.sleep(timeout),返回 False(保持原行为)。

    Args:
        timeout: 最大等待时长(秒)

    Returns:
        True: 收到 dirty 信号(应立即处理 dirty)
        False: 超时未收到信号(应继续退避)
    """
    if timeout <= 0:
        return False
    _subscriber_failed = False
    try:
        pubsub = await _ensure_subscriber()
    except Exception as e:
        logger.debug(
            f"[crdb_sync_event] R56 §7.2.3: _ensure_subscriber 异常 "
            f"(fallback 到 sleep): {e}"
        )
        await asyncio.sleep(min(timeout, 5.0))
        _subscriber_failed = True
        pubsub = None
    if _subscriber_failed:
        return False
    if pubsub is None:
        # Redis 不可用 → fallback 到 sleep(保持 polling 行为)
        await asyncio.sleep(timeout)
        return False
    try:
        # 用 asyncio.wait_for 限时等待 get_message
        # 注意:redis-py 的 pubsub.get_message 是同步阻塞,需用 async 版本
        # redis.asyncio 的 pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        # 是协程,返回消息 dict 或 None(超时)
        msg = await asyncio.wait_for(
            pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=timeout,
            ),
            timeout=timeout + 1.0,  # 外层 wait_for 兜底,避免 redis 内部超时不准
        )
        if msg is None:
            return False  # 超时
        # 收到信号
        logger.debug(
            f"[crdb_sync_event] R56 §7.2.3: 收到 dirty 信号 "
            f"channel={msg.get('channel')} data={msg.get('data')}"
        )
        return True
    except asyncio.TimeoutError:
        # 超时是预期行为,fall through 到 return False
        logger.debug(_i18n_t("services.crdb_sync_event_wakeup.logger_wait_dirty_signal_timeout"))
    except Exception as e:
        logger.debug(
            f"[crdb_sync_event] R56 §7.2.3: wait_dirty_signal 异常 "
            f"(fallback 到 sleep): {e}"
        )
        # 异常时 fallback 到 sleep(不永久阻塞 sync_loop)
        await asyncio.sleep(min(timeout, 5.0))  # 最多 sleep 5s 避免永久阻塞
    return False


def reset_subscriber() -> None:
    """重置 subscriber 单例(测试用)。

    在每个测试用例前调用,避免跨用例污染 subscriber 连接。
    """
    global _subscriber_client, _subscriber_ready
    _subscriber_client = None
    _subscriber_ready = False


def get_signal_channel() -> str:
    """返回 signal channel 名称(测试用)。"""
    return _DIRTY_SIGNAL_CHANNEL


# ── 本地 durable watermark(§7.2.5)──────────────────────────────


# sync watermark 存储在 SQLite kv_store 中,避免空载 SELECT MAX()/count/schema 探测
# key: "crdb_sync:watermark:<table_name>"
# value: 上次成功同步的 dirty_outbox.id(整数,单调递增)
_SYNC_WATERMARK_PREFIX = "crdb_sync:watermark:"


async def get_local_sync_watermark(table_name: str) -> int:
    """读取本地 sync watermark(上次成功同步的 dirty_outbox.id)。

    替代 ``SELECT MAX(id) FROM dirty_outbox``(空载 CRDB 查询),
    从 SQLite kv_store 读取 watermark(0 RU)。

    Args:
        table_name: 表名(如 "users" / "files")

    Returns:
        上次成功同步的 dirty_outbox.id(0 表示从未同步)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if store is None or store._db is None:
            return 0
        key = _SYNC_WATERMARK_PREFIX + table_name
        cursor = await store._db.execute(
            "SELECT value FROM kv_store WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0
        try:
            value = int(row[0])
        except (ValueError, TypeError):
            value = 0
        return value
    except Exception as e:
        logger.debug(
            f"[crdb_sync_event] R56 §7.2.5: 读取 sync watermark 失败 "
            f"table={table_name}: {e}"
        )
    return 0


async def set_local_sync_watermark(table_name: str, watermark: int) -> bool:
    """更新本地 sync watermark(成功同步一批 dirty 后调用)。

    Args:
        table_name: 表名
        watermark: 本次成功同步的最大 dirty_outbox.id

    Returns:
        True: 更新成功
        False: 更新失败(SQLite 不可用)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if store is None or store._db is None:
            return False
        key = _SYNC_WATERMARK_PREFIX + table_name
        await store._db.execute(
            """INSERT OR REPLACE INTO kv_store (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (key, str(watermark), _now_iso()),
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(
            f"[crdb_sync_event] R56 §7.2.5: 更新 sync watermark 失败 "
            f"table={table_name} watermark={watermark}: {e}"
        )
    return False


def get_watermark_key(table_name: str) -> str:
    """返回 watermark 的 kv_store key(测试用)。"""
    return _SYNC_WATERMARK_PREFIX + table_name


def _now_iso() -> str:
    """当前 ISO 时间戳(避免在每个函数中重复 import datetime)。"""
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat()
