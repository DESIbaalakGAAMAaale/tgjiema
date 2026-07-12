"""R36 §6.3 + R37 P0-3 + R38 P0-4: 单一 crdb_sync 服务 — 唯一 CRDB 同步事实源。

架构(R37 P0-3 收口):
  Up / Idx / Dsp / Mon / Admin → SQLite 本地权威状态 + dirty_event
  → crdb_sync 独占消费 dirty rows → CRDB 批量 UPSERT
  → 成功后在 SQLite 标记 crdb_synced

R37 P0-3 强制收口:
  1. Bot 直连 CRDB 兜底路径(sync_back_loop / sync_dirty_cells)默认禁用(SYNC_BACK_OFF=0)
  2. crdb_sync 使用 sync_leader 租约(互斥),防止多实例并发同步
  3. 同步循环:有 dirty 时受控 batch cadence(2s),无 dirty 时退避(60s→1800s)
  4. 异常分支指数退避,不会快速重试消耗 RU

R38 P0-4 leader 租约原子 CAS:
  - 原版本用 SQLite kv_store get_kv → 判断 → set_kv,两步非原子,
    两个进程可同时通过 "查询为空" 判定并同时获得 leader。
  - 新版本优先使用 Redis SET key value NX PX <ttl_ms> 原子获取租约,
    renew 用 Lua 脚本 compare-and-renew(KEYS[1]==ARGV[1] 才 PEXPIRE),
    防止两个进程同时持有 leader。
  - 独立 renewal task:每 TTL/3(约 30s)续约一次,不受主循环 sleep 影响。
  - 每批写前调用 renew_leader() 校验 fencing token,丢租约立即停止同步
    并关闭 CRDB pool。
  - Redis 不可用时降级到原 SQLite KV 逻辑(记录 warning)。

CRDB 连接者仅允许:crdb_sync、migration、restore、低频备份和受控 Admin。
业务 Bot 不持有 CRDB 凭证,不调用 CRDB SDK(SYNC_BACK_OFF=0 时)。

启动方式:
  systemd: tgjiema-crdb_sync.service
  手动: python -m services.crdb_sync_service
"""
import asyncio
import json
import os
import signal
import sys
import time
import uuid

from loguru import logger


# 默认同步间隔(秒)
DEFAULT_SYNC_INTERVAL = 60   # 无 dirty 时基础间隔
MAX_BACKOFF = 1800           # 30 分钟上限(无 dirty 时退避)
# R37 P0-3: 有 dirty 时受控 batch cadence(2s),避免无节流高频查询
DIRTY_BATCH_INTERVAL = 2     # 有 dirty 时每 2s 处理一批
# R37 P0-3: 每轮最大处理时间预算(秒),超时分片到下一轮
MAX_BATCH_TIME_BUDGET = 30    # 单轮最多 30s
# R38 P1-1: CRDB pool 空闲关闭阈值(秒),连续 5 分钟无 dirty 主动 close_db()
CRDB_IDLE_CLOSE_THRESHOLD = 300  # 5 分钟


# ── R37 P0-3 / R38 P0-4: sync_leader 租约 fencing ──

_LEADER_KEY = "crdb_sync/leader"
_LEADER_TTL = 90  # 租约时长(秒),与 settings.CRDB_SYNC_LEADER_LEASE 一致
_LEADER_TTL_MS = _LEADER_TTL * 1000  # 毫秒,用于 Redis PX 参数
# R38 P0-4: 独立 renewal task 间隔 = TTL/3(约 30s),不受主循环 sleep 影响
_LEADER_RENEWAL_INTERVAL = max(_LEADER_TTL // 3, 10)

# R38 P0-4: 进程启动时生成一次 leader_id(实例字段),
# 整个进程生命周期复用,避免每次 acquire 生成新 id 导致 renew 校验失败。
_LEADER_ID: str = f"crdb_sync-{os.getpid()}-{uuid.uuid4().hex[:12]}"

# R38 P0-4: compare-and-renew Lua 脚本(原子):
# KEYS[1] = leader key, ARGV[1] = 当前 leader_id, ARGV[2] = 新 TTL(毫秒)
# 仅当 GET key == 当前 leader_id 时才 PEXPIRE 续约,否则返回 0
_RENEW_LEADER_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

# R38 P0-4: 释放租约 Lua 脚本(原子,仅 owner 可释放)
_RELEASE_LEADER_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# R38 P0-4: Redis 不可用降级标志(True=使用 SQLite KV fallback)
_redis_leader_fallback: bool = False


async def _get_redis_client():
    """R38 P0-4: 获取 Redis 客户端(复用项目 redis_queue 模块的连接)。

    Redis 不可用时返回 None,调用方降级到 SQLite KV 逻辑。
    """
    try:
        from database.redis_queue import get_redis
        return await get_redis()
    except Exception as e:
        logger.debug(f"[crdb_sync] Redis 客户端获取失败(fallback 到 SQLite KV): {e}")
        return None


async def _acquire_leader_lease() -> bool:
    """R38 P0-4: 原子获取 sync_leader 租约。

    优先使用 Redis SET key value NX PX <ttl_ms> 原子 CAS:
    - SET NX:仅当 key 不存在时才设置(原子性由 Redis 单线程保证)
    - PX <ttl_ms>:同时设置过期时间,避免设置后崩溃导致永久锁
    - 返回 True:获得租约;False:已被其他实例持有

    Redis 不可用时降级到原 SQLite kv_store 逻辑(get_kv → 判断 → set_kv),
    记录 warning(非原子,可能出现双 leader,仅在单实例或维护场景可接受)。

    Returns:
        True if 当前进程获得租约;False if 其他实例持有租约
    """
    global _redis_leader_fallback

    redis_client = await _get_redis_client()
    if redis_client is not None:
        try:
            # R38 P0-4: 原子 SET NX PX(等价于 SET key value NX PX ttl_ms)
            # value = leader_id(用于后续 renew/release 校验)
            ok = await redis_client.set(
                _LEADER_KEY, _LEADER_ID, nx=True, px=_LEADER_TTL_MS,
            )
            if ok:
                _redis_leader_fallback = False
                logger.info(
                    f"[crdb_sync] 获得 leader 租约(Redis CAS, "
                    f"leader_id={_LEADER_ID}, TTL={_LEADER_TTL}s)"
                )
                return True
            # 已被其他实例持有
            logger.debug(
                f"[crdb_sync] leader 租约已被其他实例持有(Redis SET NX 返回 None)"
            )
            return False
        except Exception as e:
            logger.warning(
                f"[crdb_sync] Redis 租约获取异常,降级到 SQLite KV fallback: {e}"
            )
            _redis_leader_fallback = True

    # R38 P0-4: Redis 不可用 → SQLite KV fallback(非原子,记录 warning)
    if not _redis_leader_fallback:
        _redis_leader_fallback = True
        logger.warning(
            "[crdb_sync] Redis 不可用,降级到 SQLite KV leader 租约(非原子 CAS,"
            "多实例部署可能出现双 leader,建议修复 Redis 后切回原子模式)"
        )

    store = _get_cache_store_safe()
    if store is None:
        logger.error("[crdb_sync] cache_store 不可用,无法获取 leader 租约")
        return False
    now = time.time()
    current = await store.get_kv(_LEADER_KEY)
    if current:
        try:
            data = json.loads(current)
            expire_at = data.get("expire_at", 0)
            leader_id = data.get("leader_id", "")
            if now < expire_at and leader_id != _LEADER_ID:
                logger.debug(f"[crdb_sync] leader 租约被 {leader_id} 持有,等待")
                return False
        except (json.JSONDecodeError, TypeError):
            pass
    lease_data = json.dumps({
        "leader_id": _LEADER_ID,
        "expire_at": now + _LEADER_TTL,
        "acquired_at": now,
        "fallback": "sqlite_kv",
    })
    await store.set_kv(_LEADER_KEY, lease_data)
    logger.info(
        f"[crdb_sync] 获得 leader 租约(SQLite KV fallback, "
        f"leader_id={_LEADER_ID}, TTL={_LEADER_TTL}s)"
    )
    return True


async def _renew_leader_lease() -> bool:
    """R38 P0-4: 续约 leader 租约(原子 compare-and-renew)。

    Redis 模式用 Lua 脚本原子校验 + 续约:
        if GET key == leader_id then PEXPIRE key ttl_ms else return 0
    防止两个进程同时续约各自的过期时间。

    Returns:
        True:续约成功(仍是 leader)
        False:租约被抢占或丢失(必须立即停止同步 + 关闭 CRDB pool)
    """
    if not _redis_leader_fallback:
        redis_client = await _get_redis_client()
        if redis_client is not None:
            try:
                # R38 P0-4: Lua 原子 compare-and-renew
                result = await redis_client.eval(
                    _RENEW_LEADER_LUA, 1, _LEADER_KEY, _LEADER_ID, _LEADER_TTL_MS,
                )
                if result:
                    logger.debug(
                        f"[crdb_sync] leader 租约续约成功(Lua CAS, "
                        f"leader_id={_LEADER_ID})"
                    )
                    return True
                logger.warning(
                    f"[crdb_sync] leader 租约续约失败(Lua 返回 0,租约被抢占或丢失)"
                )
                return False
            except Exception as e:
                logger.warning(
                    f"[crdb_sync] Redis Lua 续约异常,降级到 SQLite KV: {e}"
                )
                # 降级路径见下方 SQLite fallback

    # SQLite KV fallback(非原子,记录 warning)
    store = _get_cache_store_safe()
    if store is None:
        return False
    now = time.time()
    current = await store.get_kv(_LEADER_KEY)
    if not current:
        # 租约丢失,尝试重新获取
        return await _acquire_leader_lease()
    try:
        data = json.loads(current)
        leader_id = data.get("leader_id", "")
        if not leader_id or leader_id != _LEADER_ID:
            logger.warning(
                f"[crdb_sync] leader 租约被 {leader_id} 抢占,停止同步"
            )
            return False
        data["expire_at"] = now + _LEADER_TTL
        data["renewed_at"] = now
        await store.set_kv(_LEADER_KEY, json.dumps(data))
        return True
    except (json.JSONDecodeError, TypeError):
        return await _acquire_leader_lease()


async def _release_leader_lease():
    """R38 P0-4: 释放 leader 租约(优雅关闭时调用,仅 owner 可释放)。"""
    if not _redis_leader_fallback:
        redis_client = await _get_redis_client()
        if redis_client is not None:
            try:
                # R38 P0-4: Lua 原子释放(仅 owner 可释放,避免误删他人租约)
                await redis_client.eval(
                    _RELEASE_LEADER_LUA, 1, _LEADER_KEY, _LEADER_ID,
                )
                logger.info("[crdb_sync] 释放 leader 租约(Redis Lua)")
                return
            except Exception as e:
                logger.debug(f"[crdb_sync] Redis 释放异常(fallback SQLite): {e}")

    # SQLite KV fallback
    store = _get_cache_store_safe()
    if store is None:
        return
    try:
        await store.set_kv(_LEADER_KEY, "")
        logger.info("[crdb_sync] 释放 leader 租约(SQLite KV fallback)")
    except Exception as e:
        logger.debug(f"[crdb_sync] 释放租约异常(可忽略): {e}")


def _get_cache_store_safe():
    """R38 P0-4: 安全获取 cache_store(避免循环导入异常)。"""
    try:
        from database.cache_store import get_cache_store
        return get_cache_store()
    except Exception as e:
        logger.debug(f"[crdb_sync] cache_store 不可用: {e}")
        return None


# ── R38 P1-1: CRDB 懒加载状态 ──
# _crdb_pool_connected: CRDB pool 是否已建立(懒加载,初始 False)
# _last_dirty_seen_ts: 上次检测到 dirty 的时间戳(用于空闲关闭判断)
_crdb_pool_connected: bool = False
_last_dirty_seen_ts: float = 0.0


async def _init_sqlite_only():
    """R38 P1-1: 仅初始化 SQLite cache_store,不创建 CRDB 连接池。

    启动时先调用此函数,确保 SQLite 可读写(leader 租约 / dirty_outbox)。
    CRDB pool 由 _lazy_connect_crdb() 在检测到 dirty 时按需创建,
    避免无 dirty 时仍占用 CRDB 连接产生空载 RU 消耗。

    leader 租约检查只走 Redis 或 SQLite kv_store,不触发 CRDB。
    """
    store = _get_cache_store_safe()
    if store is None:
        logger.error("[crdb_sync] R38 P1-1: cache_store 不可用,无法初始化 SQLite")
        raise RuntimeError("cache_store unavailable for SQLite-only init")
    await store.init()
    logger.info("[crdb_sync] R38 P1-1: SQLite cache_store 已初始化(CRDB pool 暂不连接)")


async def _lazy_connect_crdb():
    """R38 P1-1: 懒加载 CRDB pool — 仅在检测到 dirty 时调用。

    幂等:已连接时直接返回,不重复创建。
    """
    global _crdb_pool_connected
    if _crdb_pool_connected:
        return
    from database.session import _client
    from config import settings as _settings
    if not _settings.COCKROACHDB_URL:
        logger.warning("[crdb_sync] R38 P1-1: COCKROACHDB_URL 未配置,跳过 CRDB 懒加载")
        return
    _client.configure(_settings.COCKROACHDB_URL)
    # connect_runtime_only 会幂等初始化 SQLite cache_store(已 init 时跳过实际工作)
    await _client.connect_runtime_only()
    _crdb_pool_connected = True
    logger.info("[crdb_sync] R38 P1-1: CRDB pool 已懒加载连接(检测到 dirty)")


async def _close_crdb_only():
    """R38 P1-1: 仅关闭 CRDB pool,保留 SQLite(cache_store)以继续 leader 租约检查。

    连续 CRDB_IDLE_CLOSE_THRESHOLD 秒无 dirty 时调用,降低空闲 RU 消耗。
    leader 租约检查只走 Redis 或 SQLite kv_store,不依赖 CRDB pool。
    """
    global _crdb_pool_connected
    if not _crdb_pool_connected:
        return
    from database.session import _client
    try:
        await _client.close()  # 仅关 _pool,不关 cache_store
        _crdb_pool_connected = False
        logger.info(
            f"[crdb_sync] R38 P1-1: CRDB pool 已空闲关闭"
            f"(连续 {CRDB_IDLE_CLOSE_THRESHOLD}s 无 dirty)"
        )
    except Exception as e:
        logger.debug(f"[crdb_sync] R38 P1-1: 关闭 CRDB pool 异常(可忽略): {e}")
        _crdb_pool_connected = False


async def _sync_loop(name: str, sync_func, get_dirty_func, mark_synced_func):
    """R37 P0-3 / R38 P0-4 / R38 P1-1: 通用同步循环 — dirty 驱动 + 受控 cadence + 退避 + leader fencing + CRDB 懒加载。

    改进点(vs R36):
    - 有 dirty 时:处理一批,short sleep(DIRTY_BATCH_INTERVAL=2s),再查
    - 无 dirty 时:退避翻倍(60s→1800s 上限)
    - 异常时:指数退避(不快速重试,避免消耗 RU)
    - 每轮检查 leader 租约,非 leader 不执行同步

    R38 P0-4: 每批写前调用 _renew_leader_lease() 校验 fencing token,
    丢租约立即停止同步 + 关闭 CRDB pool(由调用方处理 close_db)。

    R38 P1-1: CRDB 懒加载 —
    - get_dirty_func 查 SQLite(不触发 CRDB)
    - 检测到 dirty 才调用 _lazy_connect_crdb() 建立 CRDB pool
    - 连续 CRDB_IDLE_CLOSE_THRESHOLD 秒无 dirty,调用 _close_crdb_only()
      释放 CRDB 连接,降低空载 RU
    - leader 租约检查只走 Redis / SQLite kv_store,不依赖 CRDB pool
    """
    global _last_dirty_seen_ts
    backoff = DEFAULT_SYNC_INTERVAL
    while True:
        try:
            # R38 P0-4: 每批写前校验 fencing token(renew_leader 返回 0 = 丢租约)
            if not await _renew_leader_lease():
                logger.error(
                    f"[crdb_sync] {name}: leader 租约丢失或被抢占,"
                    f"停止同步循环(等待重新获取或主进程退出)"
                )
                # R38 P1-1: 关闭 CRDB pool 避免越权写入(只关 CRDB,保留 SQLite)
                await _close_crdb_only()
                # 等待 lease TTL 过期后尝试重新获取
                await asyncio.sleep(_LEADER_TTL // 2)
                if not await _acquire_leader_lease():
                    # 重新获取失败,继续等待下一轮
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    await asyncio.sleep(backoff)
                    continue
                # 重新获得租约后不立即连 CRDB(R38 P1-1: 等待 dirty 检测后再懒加载)

            # R38 P1-1: get_dirty 只查 SQLite,不触发 CRDB
            dirty = await get_dirty_func()
            if dirty:
                # R38 P1-1: 检测到 dirty,懒加载 CRDB pool(如未连接)
                await _lazy_connect_crdb()
                _last_dirty_seen_ts = time.time()

                # R37 P0-3: 有 dirty 时受控 batch cadence,不长退避
                batch_start = time.time()
                await sync_func()
                batch_elapsed = time.time() - batch_start
                logger.debug(
                    f"[crdb_sync] {name}: 处理 {len(dirty)} 条 dirty "
                    f"(耗时 {batch_elapsed:.1f}s),{DIRTY_BATCH_INTERVAL}s 后再查"
                )
                # 有 dirty 时短 sleep,再查(不退避)
                await asyncio.sleep(DIRTY_BATCH_INTERVAL)
                backoff = DEFAULT_SYNC_INTERVAL  # 重置退避
                continue
            else:
                # R38 P1-1: 无 dirty,检查是否需要空闲关闭 CRDB pool
                if _crdb_pool_connected and _last_dirty_seen_ts > 0:
                    idle_seconds = time.time() - _last_dirty_seen_ts
                    if idle_seconds >= CRDB_IDLE_CLOSE_THRESHOLD:
                        await _close_crdb_only()
                        # 关闭后重置时间戳,避免重复触发关闭逻辑
                        _last_dirty_seen_ts = 0.0
                # 无 dirty:退避翻倍(上限 30min)
                backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as e:
            logger.warning(f"[crdb_sync] {name} 同步异常: {e}")
            # R37 P0-3: 异常时指数退避(不快速重试)
            backoff = min(backoff * 2, MAX_BACKOFF)
        await asyncio.sleep(backoff)


async def _leader_renewal_task():
    """R38 P0-4: 独立 leader 租约续约 task。

    与主同步循环解耦,每 _LEADER_RENEWAL_INTERVAL(约 30s = TTL/3)续约一次,
    不受主循环 backoff(可达 30min)影响,避免无 dirty 时租约过期被抢占。

    续约失败时记录 error,主循环下一轮会检测到并停止同步。
    """
    logger.info(
        f"[crdb_sync] leader renewal task 启动(间隔 {_LEADER_RENEWAL_INTERVAL}s)"
    )
    while True:
        try:
            await asyncio.sleep(_LEADER_RENEWAL_INTERVAL)
            ok = await _renew_leader_lease()
            if not ok:
                logger.error(
                    "[crdb_sync] renewal task: 续约失败,租约已丢失/被抢占,"
                    "等待主循环检测并停止同步"
                )
        except asyncio.CancelledError:
            logger.info("[crdb_sync] leader renewal task 收到取消信号,退出")
            raise
        except Exception as e:
            logger.warning(f"[crdb_sync] renewal task 异常(忽略,下一轮重试): {e}")


async def _sync_jobs():
    """R36 §6.4.4: jobs 同步 - 调用 sync_local_jobs_to_crdb。"""
    from database.session import sync_local_jobs_to_crdb
    await sync_local_jobs_to_crdb()


async def _get_dirty_jobs():
    from database.cache_store import get_cache_store
    return await get_cache_store().get_local_unsynced_jobs()


async def _sync_cells():
    """R36 §6.4.4: cells 同步 - 仅异常事件和路由变更,心跳/计数器不回写。"""
    from database.session import sync_dirty_cells_to_crdb
    await sync_dirty_cells_to_crdb()


async def _get_dirty_cells():
    from database.cache_store import get_cache_store
    return await get_cache_store().get_dirty_cells_local(50)


async def main():
    """crdb_sync 主入口:并发运行同步循环(jobs + cells)。"""
    from config import settings

    role = settings.SERVICE_ROLE or os.environ.get("SERVICE_ROLE", "")
    if role != "crdb_sync":
        logger.warning(
            f"[crdb_sync] SERVICE_ROLE={role or '(空)'},期望 'crdb_sync'。"
            f"建议通过 systemd Environment=SERVICE_ROLE=crdb_sync 注入"
        )

    logger.info("[crdb_sync] 启动 — 单一 CRDB 同步事实源(R37 P0-3 / R38 P0-4 收口)")
    logger.info(
        f"[crdb_sync] 退避策略: 有 dirty={DIRTY_BATCH_INTERVAL}s cadence, "
        f"无 dirty={DEFAULT_SYNC_INTERVAL}s→{MAX_BACKOFF}s 退避"
    )
    logger.info(
        f"[crdb_sync] leader 租约: TTL={_LEADER_TTL}s, "
        f"renewal 间隔={_LEADER_RENEWAL_INTERVAL}s "
        f"(R38 P0-4: Redis SET NX PX + Lua compare-and-renew)"
    )
    logger.info(
        f"[crdb_sync] R38 P1-1: CRDB 懒加载 — 启动时只初始化 SQLite,"
        f"检测到 dirty 才连接 CRDB pool,连续 {CRDB_IDLE_CLOSE_THRESHOLD}s 无 dirty 主动 close"
    )

    # R38 P1-1: 启动时只初始化 SQLite cache_store(不连 CRDB)
    # CRDB pool 在 _sync_loop 检测到 dirty 时由 _lazy_connect_crdb() 懒加载
    await _init_sqlite_only()

    # R37 P0-3: 启动时获取 leader 租约(只走 Redis/SQLite,不触发 CRDB)
    if not await _acquire_leader_lease():
        logger.warning(
            "[crdb_sync] 未获得 leader 租约(其他实例可能正在运行),"
            "等待 30s 后重试"
        )
        await asyncio.sleep(30)
        if not await _acquire_leader_lease():
            logger.error("[crdb_sync] 仍无法获得 leader 租约,退出")
            sys.exit(1)

    # R38 P1-1: 不在此处 init_db()(避免立即建立 CRDB pool)
    # CRDB pool 由 _sync_loop → _lazy_connect_crdb() 在检测到 dirty 时按需创建
    from database import close_db

    # 并发运行同步循环 + R38 P0-4: 独立 leader renewal task
    tasks = [
        asyncio.create_task(
            _sync_loop("jobs", _sync_jobs, _get_dirty_jobs, None),
            name="sync-jobs",
        ),
        asyncio.create_task(
            _sync_loop("cells", _sync_cells, _get_dirty_cells, None),
            name="sync-cells",
        ),
        asyncio.create_task(
            _leader_renewal_task(),
            name="leader-renewal",
        ),
    ]

    # 优雅关闭
    def _shutdown():
        logger.info("[crdb_sync] 收到停止信号,取消所有同步循环")
        for t in tasks:
            t.cancel()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, AttributeError):
            # Windows 不支持 add_signal_handler,忽略(靠 KeyboardInterrupt 兜底)
            pass

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        # R37 P0-3: 优雅关闭时释放 leader 租约
        await _release_leader_lease()
        await close_db()
        logger.info("[crdb_sync] 已停止")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
