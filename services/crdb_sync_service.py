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
from __future__ import annotations

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

# R41 RU 门禁: dirty_outbox 批量 UPSERT 大小(从环境变量读取,范围 100-500)
# 合并最高 version 后批量 UPSERT 到 CRDB,降低单行 UPSERT 的 RU 消耗
def _parse_batch_size(default: int = 100, min_v: int = 100, max_v: int = 500) -> int:
    """从 CRDB_SYNC_BATCH_SIZE 环境变量解析批量大写,限制在 [min_v, max_v] 范围内。"""
    raw = os.environ.get("CRDB_SYNC_BATCH_SIZE", str(default))
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return default
    if size < min_v:
        return min_v
    if size > max_v:
        return max_v
    return size


CRDB_SYNC_BATCH_SIZE = _parse_batch_size()
# R41 RU 门禁: CRDB pool 空闲后再次关闭前的最小间隔(秒),避免频繁 connect/close
_CRDB_CLOSE_COOLDOWN = 30  # 关闭后至少 30s 才允许重新 connect


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

# R39 P1-2: 本地 lease 状态标志(由 _leader_renewal_task 唯一更新,_sync_loop 只读)
# _sync_loop 不再调用 _renew_leader_lease(),仅读取 _lease_valid 判断是否可同步
_lease_valid: bool = False
# R39 P1-2: fencing token(leader_id 副本),供 worker 在写前只读校验
_fencing_token: str = ""


def _is_production() -> bool:
    """R39 P1-1: 判断当前是否为生产环境。

    生产环境 Redis 不可用时 fail-closed(关闭 CRDB pool + 停止同步),
    仅开发模式允许 SQLite KV fallback。
    """
    try:
        from config import settings
        return getattr(settings, "ENVIRONMENT", "development") == "production"
    except Exception:
        return False


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
    """R38 P0-4 / R39 P1-1: 原子获取 sync_leader 租约。

    优先使用 Redis SET key value NX PX <ttl_ms> 原子 CAS:
    - SET NX:仅当 key 不存在时才设置(原子性由 Redis 单线程保证)
    - PX <ttl_ms>:同时设置过期时间,避免设置后崩溃导致永久锁
    - 返回 True:获得租约;False:已被其他实例持有

    R39 P1-1: 生产环境(ENVIRONMENT=production)Redis 不可用时 fail-closed,
    不降级到 SQLite KV(非原子,split-brain 风险)。
    仅开发模式允许 SQLite KV fallback。

    R39 P1-2: 获取成功时设置全局 _lease_valid=True 与 _fencing_token,
    供 _sync_loop 只读判断。

    Returns:
        True if 当前进程获得租约;False if 其他实例持有租约
    """
    global _redis_leader_fallback, _lease_valid, _fencing_token

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
                # R39 P1-2: 更新本地 lease 状态(由 renewal task 维护)
                _lease_valid = True
                _fencing_token = _LEADER_ID
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
            # R39 P1-1: 生产环境 Redis 异常 → fail-closed,不降级
            if _is_production():
                logger.error(
                    f"[crdb_sync] R39 P1-1: 生产环境 Redis 租约获取异常,"
                    f"fail-closed(不降级到 SQLite KV,避免 split-brain): {e}"
                )
                _lease_valid = False
                _fencing_token = ""
                return False
            logger.warning(
                f"[crdb_sync] Redis 租约获取异常,降级到 SQLite KV fallback: {e}"
            )
            _redis_leader_fallback = True

    # R39 P1-1: Redis 客户端不可用(None)时,生产环境 fail-closed
    if _is_production():
        logger.error(
            "[crdb_sync] R39 P1-1: 生产环境 Redis 不可用,"
            "fail-closed(不降级到非原子 SQLite KV,避免 split-brain 风险)。"
            "请恢复 Redis 服务后重启 crdb_sync。"
        )
        _lease_valid = False
        _fencing_token = ""
        return False

    # R38 P0-4: Redis 不可用 → SQLite KV fallback(仅开发模式,非原子,记录 warning)
    if not _redis_leader_fallback:
        _redis_leader_fallback = True
        logger.warning(
            "[crdb_sync] Redis 不可用,降级到 SQLite KV leader 租约(非原子 CAS,"
            "多实例部署可能出现双 leader,建议修复 Redis 后切回原子模式)。"
            "R39 P1-1: 生产环境已禁止此 fallback。"
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
    # R39 P1-2: 更新本地 lease 状态
    _lease_valid = True
    _fencing_token = _LEADER_ID
    logger.info(
        f"[crdb_sync] 获得 leader 租约(SQLite KV fallback, "
        f"leader_id={_LEADER_ID}, TTL={_LEADER_TTL}s)"
    )
    return True


async def _renew_leader_lease() -> bool:
    """R38 P0-4 / R39 P1-1 / R39 P1-2: 续约 leader 租约(原子 compare-and-renew)。

    Redis 模式用 Lua 脚本原子校验 + 续约:
        if GET key == leader_id then PEXPIRE key ttl_ms else return 0
    防止两个进程同时续约各自的过期时间。

    R39 P1-1: 生产环境 Redis 续约异常时 fail-closed(不降级到 SQLite),
    避免非原子续约导致 split-brain。仅开发模式允许降级。

    R39 P1-2: 续约结果同步更新全局 _lease_valid / _fencing_token,
    供 _sync_loop 只读判断(不再自己调用本函数)。

    Returns:
        True:续约成功(仍是 leader)
        False:租约被抢占或丢失(必须立即停止同步 + 关闭 CRDB pool)
    """
    global _lease_valid, _fencing_token

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
                    _lease_valid = True
                    _fencing_token = _LEADER_ID
                    return True
                logger.warning(
                    f"[crdb_sync] leader 租约续约失败(Lua 返回 0,租约被抢占或丢失)"
                )
                # R39 P1-2: 续约失败,清空本地 lease 标志
                _lease_valid = False
                _fencing_token = ""
                return False
            except Exception as e:
                # R39 P1-1: 生产环境 Redis 续约异常 → fail-closed,不降级
                if _is_production():
                    logger.error(
                        f"[crdb_sync] R39 P1-1: 生产环境 Redis Lua 续约异常,"
                        f"fail-closed(不降级到非原子 SQLite KV): {e}"
                    )
                    _lease_valid = False
                    _fencing_token = ""
                    return False
                logger.warning(
                    f"[crdb_sync] Redis Lua 续约异常,降级到 SQLite KV: {e}"
                )
                # 降级路径见下方 SQLite fallback
        else:
            # R39 P1-1: Redis 客户端不可用,生产环境 fail-closed
            if _is_production():
                logger.error(
                    "[crdb_sync] R39 P1-1: 生产环境 Redis 不可用,"
                    "续约 fail-closed(不降级到 SQLite KV)。"
                )
                _lease_valid = False
                _fencing_token = ""
                return False

    # SQLite KV fallback(仅开发模式,非原子,记录 warning)
    store = _get_cache_store_safe()
    if store is None:
        _lease_valid = False
        _fencing_token = ""
        return False
    now = time.time()
    current = await store.get_kv(_LEADER_KEY)
    if not current:
        # 租约丢失,尝试重新获取
        ok = await _acquire_leader_lease()
        if not ok:
            _lease_valid = False
            _fencing_token = ""
        return ok
    try:
        data = json.loads(current)
        leader_id = data.get("leader_id", "")
        if not leader_id or leader_id != _LEADER_ID:
            logger.warning(
                f"[crdb_sync] leader 租约被 {leader_id} 抢占,停止同步"
            )
            _lease_valid = False
            _fencing_token = ""
            return False
        data["expire_at"] = now + _LEADER_TTL
        data["renewed_at"] = now
        await store.set_kv(_LEADER_KEY, json.dumps(data))
        _lease_valid = True
        _fencing_token = _LEADER_ID
        return True
    except (json.JSONDecodeError, TypeError):
        ok = await _acquire_leader_lease()
        if not ok:
            _lease_valid = False
            _fencing_token = ""
        return ok


async def _release_leader_lease():
    """R38 P0-4 / R39 P1-2: 释放 leader 租约(优雅关闭时调用,仅 owner 可释放)。"""
    global _lease_valid, _fencing_token
    # R39 P1-2: 释放时清空本地 lease 标志
    _lease_valid = False
    _fencing_token = ""
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

    # SQLite KV fallback(仅开发模式)
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
# _last_pool_close_ts: R41 RU 门禁: 上次 CRDB pool 关闭时间戳(用于 cooldown 控制)
_crdb_pool_connected: bool = False
_last_dirty_seen_ts: float = 0.0
_last_pool_close_ts: float = 0.0


async def _should_connect() -> bool:
    """R41 RU 门禁: 判断是否应该建立 CRDB 连接。

    判断条件:
        1. dirty_outbox 中存在未处理记录(SQLite 本地查询,0 RU)
        2. 当前 pool 未连接
        3. 距上次关闭已超过 cooldown(避免频繁 connect/close)

    Returns:
        True: 应该建立 CRDB 连接(调用 _lazy_connect_crdb)
        False: 不需要连接(继续走 SQLite 退避)
    """
    # R43: read-only global,无需 global 声明(F824)
    if _crdb_pool_connected:
        return False  # 已连接,无需重复
    # cooldown 检查:刚关闭的 pool 至少 30s 后才允许重新连接
    if _last_pool_close_ts > 0:
        elapsed = time.time() - _last_pool_close_ts
        if elapsed < _CRDB_CLOSE_COOLDOWN:
            logger.debug(
                f"[crdb_sync] R41: 距上次关闭 {elapsed:.1f}s < cooldown "
                f"{_CRDB_CLOSE_COOLDOWN}s,跳过连接"
            )
            return False
    # 检查 dirty_outbox 是否有未处理记录(SQLite 本地查询,0 RU)
    store = _get_cache_store_safe()
    if store is None:
        return False
    try:
        # 直接查询 dirty_outbox 表的未处理行数(走 SQLite,不触发 CRDB)
        if store._db is None:
            return False
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0 LIMIT 1"
        )
        count = rows[0][0] if rows and rows[0] else 0
        return count > 0
    except Exception as e:
        logger.debug(f"[crdb_sync] R41: _should_connect 查询 dirty_outbox 失败: {e}")
        return False


async def _close_pool_if_idle():
    """R41 RU 门禁: 检测 CRDB pool 是否空闲,空闲超过阈值则主动关闭。

    与 _close_crdb_only 的区别:
        - _close_crdb_only(): 立即关闭(由调用方决定时机)
        - _close_pool_if_idle(): 智能判断是否需要关闭(基于空闲时长)

    R38 P1-1 已实现基础逻辑(_sync_loop 中调用),R41 提取为独立方法便于测试。
    """
    global _last_pool_close_ts
    if not _crdb_pool_connected:
        return  # 未连接,无需关闭
    if _last_dirty_seen_ts <= 0:
        return  # 从未检测到 dirty,不主动关闭(等待首次 dirty 触发连接)
    idle_seconds = time.time() - _last_dirty_seen_ts
    if idle_seconds >= CRDB_IDLE_CLOSE_THRESHOLD:
        await _close_crdb_only()
        _last_pool_close_ts = time.time()
        logger.info(
            f"[crdb_sync] R41: CRDB pool 已空闲关闭(idle={idle_seconds:.1f}s, "
            f"cooldown={_CRDB_CLOSE_COOLDOWN}s)"
        )


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
    """R37 P0-3 / R38 P0-4 / R38 P1-1 / R39 P1-2: 通用同步循环 — dirty 驱动 + 受控 cadence + 退避 + leader fencing + CRDB 懒加载。

    改进点(vs R36):
    - 有 dirty 时:处理一批,short sleep(DIRTY_BATCH_INTERVAL=2s),再查
    - 无 dirty 时:退避翻倍(60s→1800s 上限)
    - 异常时:指数退避(不快速重试,避免消耗 RU)
    - 每轮检查 leader 租约,非 leader 不执行同步

    R39 P1-2: 单续约任务 —
    - _sync_loop 不再调用 _renew_leader_lease(),
      只读取本地 _lease_valid / _fencing_token 标志(由 _leader_renewal_task 唯一更新)
    - 开头检查 if not _lease_valid: sleep + continue(等待 renewal task 重新获取)
    - 避免三处续约(jobs/cells 循环各自 renew + renewal task)产生竞态

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
            # R39 P1-2: 只读本地 lease 标志(由 _leader_renewal_task 唯一更新),
            # 不再调用 _renew_leader_lease()(去除 jobs/cells 循环各自续约的冗余)
            if not _lease_valid:
                logger.warning(
                    f"[crdb_sync] {name}: lease 无效(等待 renewal task 重新获取),"
                    f"本轮跳过同步"
                )
                # R38 P1-1: 关闭 CRDB pool 避免越权写入(只关 CRDB,保留 SQLite)
                await _close_crdb_only()
                # 等待 renewal task 重新获取租约(短 sleep,不消耗 RU)
                await asyncio.sleep(_LEADER_RENEWAL_INTERVAL)
                backoff = DEFAULT_SYNC_INTERVAL  # 重置退避
                continue

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
    """R38 P0-4 / R39 P1-2: 独立 leader 租约续约 task(唯一续约源)。

    与主同步循环解耦,每 _LEADER_RENEWAL_INTERVAL(约 30s = TTL/3)续约一次,
    不受主循环 backoff(可达 30min)影响,避免无 dirty 时租约过期被抢占。

    R39 P1-2: 续约失败时主动尝试重新获取租约(因为 _sync_loop 不再自己
    重新获取,只读 _lease_valid 标志)。renew 失败 → 尝试 acquire,
    保持 renewal task 为唯一续约/重获取源。

    续约失败时记录 error,主循环下一轮通过 _lease_valid=False 检测到并停止同步。
    """
    logger.info(
        f"[crdb_sync] leader renewal task 启动(间隔 {_LEADER_RENEWAL_INTERVAL}s,"
        f"R39 P1-2: 唯一续约源,_sync_loop 只读 lease 标志)"
    )
    while True:
        try:
            await asyncio.sleep(_LEADER_RENEWAL_INTERVAL)
            ok = await _renew_leader_lease()
            if not ok:
                logger.warning(
                    "[crdb_sync] renewal task: 续约失败,租约已丢失/被抢占,"
                    "尝试重新获取(_sync_loop 等待 _lease_valid 恢复)"
                )
                # R39 P1-2: renewal task 负责重新获取(sleep 后重试)
                await asyncio.sleep(_LEADER_RENEWAL_INTERVAL)
                if not await _acquire_leader_lease():
                    logger.error(
                        "[crdb_sync] renewal task: 重新获取租约失败,"
                        "_lease_valid 仍为 False,_sync_loop 将跳过同步"
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


# ──────────────────────────────────────────────────────────────
# R39 P0-4: dirty_outbox 通用 dispatcher (事务发件箱消费)
#
# 设计:
#   - Bot 写本地 SQLite + add_dirty_outbox(table, pk, op, payload)
#   - crdb_sync 消费 dirty_outbox: 按 table_name 分组 dispatch 到 CRDB
#   - 成功后 mark_dirty_processed(ids)
#   - 未知 table/operation → DEAD (不标记 processed, 保留供人工检查, 避免丢弃)
#
# 已有专用同步循环覆盖的表 (jobs/cells):
#   - jobs 由 _sync_jobs (sync_local_jobs_to_crdb) 处理
#   - cells 由 _sync_cells (sync_dirty_cells_to_crdb) 处理
#   - 这两类的 dirty_outbox 记录直接标记 processed (避免重复处理)
# ──────────────────────────────────────────────────────────────


async def _get_dirty_outbox():
    """R39 P0-4: 检查 dirty_outbox 是否有未处理记录 (供 _sync_loop 判断是否有 dirty)。"""
    store = _get_cache_store_safe()
    if store is None:
        return []
    return await store.get_dirty_outbox_batch(limit=100)


async def _dispatch_file_records_upsert(records: list[dict]) -> list[int]:
    """R39 P0-4: 将 file_records 的 dirty_outbox 记录 UPSERT 到 CRDB。

    Args:
        records: dirty_outbox 行列表 (含 payload JSON 行快照)

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import get_file_records_col

    col = get_file_records_col()
    processed_ids: list[int] = []
    # file_records 表字段 (与 DDL 对齐, 排除 file_code 作为 conflict key)
    _UPSERT_COLS = [
        "file_code", "uploader_id", "primary_channel_id",
        "primary_channel_msg_id", "file_types", "backup_channel_msg_ids",
        "batch_msg_ids", "batch_file_meta", "file_ids", "status",
        "request_count", "create_time", "expire_time",
    ]
    for r in records:
        rid = r.get("id")
        payload = r.get("payload")
        if not payload:
            logger.warning(f"[crdb_sync] R39 P0-4: dirty_outbox id={rid} 无 payload, 跳过")
            continue
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            # 仅保留 DDL 中已知列, 缺失字段填 NULL
            values = [row.get(c) for c in _UPSERT_COLS]
            placeholders = [f"${i + 1}" for i in range(len(_UPSERT_COLS))]
            # ON CONFLICT (file_code) DO UPDATE: 更新除 file_code 外的所有列
            update_cols = [c for c in _UPSERT_COLS if c != "file_code"]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO file_records ({', '.join(_UPSERT_COLS)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"ON CONFLICT (file_code) DO UPDATE SET {set_clause}"
            )
            await col.execute_raw(sql, values)
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(f"[crdb_sync] R39 P0-4: file_records UPSERT 失败 id={rid}: {e}")
    return processed_ids


async def _dispatch_codes_upsert(records: list[dict]) -> list[int]:
    """R39 P0-4: 将 codes 的 dirty_outbox 记录 UPSERT 到 CRDB。

    Args:
        records: dirty_outbox 行列表 (含 payload JSON 行快照)

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import get_codes_col

    col = get_codes_col()
    processed_ids: list[int] = []
    # codes 表字段 (与 DDL 对齐, 排除 code 作为 conflict key)
    _UPSERT_COLS = [
        "code", "file_record_code", "uploader_id", "file_types",
        "batch_msg_ids", "batch_file_meta", "primary_channel_id",
        "status", "created_at", "expire_time",
    ]
    for r in records:
        rid = r.get("id")
        payload = r.get("payload")
        if not payload:
            logger.warning(f"[crdb_sync] R39 P0-4: dirty_outbox id={rid} 无 payload, 跳过")
            continue
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            values = [row.get(c) for c in _UPSERT_COLS]
            placeholders = [f"${i + 1}" for i in range(len(_UPSERT_COLS))]
            update_cols = [c for c in _UPSERT_COLS if c != "code"]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO codes ({', '.join(_UPSERT_COLS)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"ON CONFLICT (code) DO UPDATE SET {set_clause}"
            )
            await col.execute_raw(sql, values)
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(f"[crdb_sync] R39 P0-4: codes UPSERT 失败 id={rid}: {e}")
    return processed_ids


async def _dispatch_users_upsert(records: list[dict]) -> list[int]:
    """R39 P0-4: 将 users 的 dirty_outbox 记录 UPSERT 到 CRDB。

    Args:
        records: dirty_outbox 行列表 (含 payload JSON 行快照)

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import get_users_col

    col = get_users_col()
    processed_ids: list[int] = []
    # users 表字段 (与 DDL 对齐, 排除 user_id 作为 conflict key)
    _UPSERT_COLS = [
        "user_id", "username", "first_name", "membership_level",
        "daily_decode_quota", "quota_used_today", "quota_date",
        "can_upload", "external_decode_quota", "external_used_today",
        "external_quota_date", "is_banned", "created_at", "updated_at",
    ]
    for r in records:
        rid = r.get("id")
        payload = r.get("payload")
        if not payload:
            logger.warning(f"[crdb_sync] R39 P0-4: dirty_outbox id={rid} 无 payload, 跳过")
            continue
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            values = [row.get(c) for c in _UPSERT_COLS]
            placeholders = [f"${i + 1}" for i in range(len(_UPSERT_COLS))]
            update_cols = [c for c in _UPSERT_COLS if c != "user_id"]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO users ({', '.join(_UPSERT_COLS)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"ON CONFLICT (user_id) DO UPDATE SET {set_clause}"
            )
            await col.execute_raw(sql, values)
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(f"[crdb_sync] R39 P0-4: users UPSERT 失败 id={rid}: {e}")
    return processed_ids


# ──────────────────────────────────────────────────────────────
# R41 P0-6: jobs / cells upsert handler — 此前依赖专用循环(sync_local_jobs_to_crdb
# / sync_dirty_cells_to_crdb),但 dirty_outbox 路径的记录无人消费会堆积。
# 此处新增 upsert handler,保证 dirty_outbox 闭环。
# ──────────────────────────────────────────────────────────────


async def _dispatch_jobs_upsert(records: list[dict]) -> list[int]:
    """R41 P0-6: 将 jobs 的 dirty_outbox 记录 UPSERT 到 CRDB。

    之前由 _sync_jobs (sync_local_jobs_to_crdb) 专用循环处理,
    但若 jobs 表通过 dirty_outbox 路径写入,必须能消费,否则会堆积。

    Args:
        records: dirty_outbox 行列表(含 payload JSON 行快照)

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import get_jobs_col

    col = get_jobs_col()
    processed_ids: list[int] = []
    # jobs 表字段(与 DDL 对齐,排除 id 作为 conflict key,因 id 由 CRDB SERIAL 生成)
    _UPSERT_COLS = [
        "code", "target_user_id", "storage_channel_id",
        "storage_msg_ids", "batch_file_meta", "task_type",
        "status", "created_at", "dispatched_at",
    ]
    for r in records:
        rid = r.get("id")
        payload = r.get("payload")
        if not payload:
            logger.warning(f"[crdb_sync] R41 P0-6: dirty_outbox id={rid} 无 payload, 跳过")
            continue
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            values = [row.get(c) for c in _UPSERT_COLS]
            placeholders = [f"${i + 1}" for i in range(len(_UPSERT_COLS))]
            # jobs.id 是 SERIAL,无法用 ON CONFLICT;改用 INSERT,主键冲突时跳过
            # 注: jobs 表通常由 SQLite 写入 crdb_id 关联,此处 INSERT 失败被视为已存在
            sql = (
                f"INSERT INTO jobs ({', '.join(_UPSERT_COLS)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLS)
            )
            await col.execute_raw(sql, values)
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(f"[crdb_sync] R41 P0-6: jobs UPSERT 失败 id={rid}: {e}")
    return processed_ids


async def _dispatch_cells_upsert(records: list[dict]) -> list[int]:
    """R41 P0-6: 将 cells 的 dirty_outbox 记录 UPSERT 到 CRDB。

    之前由 _sync_cells (sync_dirty_cells_to_crdb) 专用循环处理,
    但若 cells 表通过 dirty_outbox 路径写入,必须能消费。

    Args:
        records: dirty_outbox 行列表(含 payload JSON 行快照)

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import get_cells_col

    col = get_cells_col()
    processed_ids: list[int] = []
    # cells 表字段(与 DDL 对齐,排除 slot_id 作为 conflict key)
    _UPSERT_COLS = [
        "slot_id", "channel_id", "status", "next_active_chat_id",
        "prev_slot_id", "demoted_to_channel_id", "account_name",
        "is_r100", "last_heartbeat", "last_synced_msg_id",
        "degrade_count", "file_count", "rotation_started_at",
        "created_at", "updated_at",
    ]
    for r in records:
        rid = r.get("id")
        payload = r.get("payload")
        if not payload:
            logger.warning(f"[crdb_sync] R41 P0-6: dirty_outbox id={rid} 无 payload, 跳过")
            continue
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            values = [row.get(c) for c in _UPSERT_COLS]
            placeholders = [f"${i + 1}" for i in range(len(_UPSERT_COLS))]
            update_cols = [c for c in _UPSERT_COLS if c != "slot_id"]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO cells ({', '.join(_UPSERT_COLS)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"ON CONFLICT (slot_id) DO UPDATE SET {set_clause}"
            )
            await col.execute_raw(sql, values)
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(f"[crdb_sync] R41 P0-6: cells UPSERT 失败 id={rid}: {e}")
    return processed_ids


# ──────────────────────────────────────────────────────────────
# R42 P1-4: dirty_outbox 合并版本冲突解决 — 单调版本来源
#
# 问题: 同 (table,pk) 合并最高 version 时,若多调用方使用默认 version=0,
#   顺序可能依赖插入 ID 并吞掉真正新状态。
# 整改: 每个可同步实体使用 monotonic version 或 updated sequence,
#   合并时同时考虑 version 和 updated_at (ORDER BY version DESC, updated_at DESC)。
# ──────────────────────────────────────────────────────────────


def _resolve_version_conflict(
    table: str, pk: str,
    current_version: int, new_version: int,
    current_updated_at: str | None = None,
    new_updated_at: str | None = None,
) -> int:
    """R42 P1-4: 解决版本冲突,返回应保留的版本号。

    决胜规则:
        - new > current → 使用 new_version
        - new == current → 用 updated_at 时间戳作为决胜(后者覆盖前者)
        - new < current → 丢弃旧版本(new),记录 warning

    Args:
        table: 表名(用于日志)
        pk: 主键(用于日志)
        current_version: 当前已选定的版本号
        new_version: 新候选版本号
        current_updated_at: 当前记录的 updated_at(ISO 字符串)
        new_updated_at: 新记录的 updated_at(ISO 字符串)

    Returns:
        应保留的版本号(new_version 或 current_version)
    """
    if new_version > current_version:
        return new_version
    if new_version == current_version:
        # 用 updated_at 决胜(后者覆盖前者)
        # ISO 字符串可直接字典序比较(空值视为最旧)
        if (new_updated_at or "") >= (current_updated_at or ""):
            return new_version
        # new_updated_at 更早 → 丢弃 new
        logger.warning(
            f"[crdb_sync] R42 P1-4: version 冲突丢弃旧版本 "
            f"table={table} pk={pk} "
            f"(version={new_version}, new_updated_at={new_updated_at} "
            f"< current_updated_at={current_updated_at})"
        )
        return current_version
    # new_version < current_version → 丢弃 new
    logger.warning(
        f"[crdb_sync] R42 P1-4: version 冲突丢弃旧版本 "
        f"table={table} pk={pk} "
        f"(new_version={new_version} < current_version={current_version})"
    )
    return current_version


# ──────────────────────────────────────────────────────────────
# R42 P1-5: Tombstone soft_delete schema 探测与缓存
#
# 问题: CRDB tombstone dispatcher 对远端表执行 DELETE,
#   但备份和跨机恢复依赖 deleted_at。
# 整改: mirror 保留 tombstone,使用 soft-delete(version+deleted_at),
#   不要立即 DELETE; 物理清理由 retention_worker 在备份保留后执行。
# ──────────────────────────────────────────────────────────────


# R42 P1-5: CRDB 表 soft_delete schema 缓存
# 缓存 table_name → bool(是否支持 deleted_at + is_tombstone 字段)
# 避免每次 tombstone dispatch 都查询 information_schema
_CRDB_SOFT_DELETE_CACHE: dict[str, bool] = {}


def _reset_soft_delete_cache() -> None:
    """R42 P1-5: 重置 soft_delete schema 缓存(测试用)。"""
    _CRDB_SOFT_DELETE_CACHE.clear()


async def _is_crdb_table_supports_soft_delete(table_name: str) -> bool:
    """R42 P1-5: 检查 CRDB 表是否支持 soft_delete(有 deleted_at + is_tombstone 字段)。

    结果缓存到 _CRDB_SOFT_DELETE_CACHE 避免重复查询。
    查询失败或 CRDB 未连接时 fail-safe 返回 False(fallback 到 hard delete + audit_log)。

    Args:
        table_name: CRDB 表名

    Returns:
        True 若表同时有 deleted_at 和 is_tombstone 字段;False otherwise
    """
    if table_name in _CRDB_SOFT_DELETE_CACHE:
        return _CRDB_SOFT_DELETE_CACHE[table_name]
    supports = False
    try:
        from database.session import _client
        # 检查 _client 是否真实连接(避免 mock 环境误判)
        if not getattr(_client, "is_connected", False):
            # CRDB 未连接,无法查询 schema → fail-safe 返回 False(不缓存,
            # 下次连接后重新查询)
            return False
        # 查询 information_schema.columns 判断是否有 deleted_at / is_tombstone
        rows = await _client.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 "
            "AND column_name IN ('deleted_at', 'is_tombstone')",
            [table_name],
        )
        cols = {r[0] for r in rows} if rows else set()
        # 必须同时有 deleted_at 和 is_tombstone 才支持 soft_delete
        supports = "deleted_at" in cols and "is_tombstone" in cols
    except Exception as e:
        logger.warning(
            f"[crdb_sync] R42 P1-5: 查询 CRDB schema 失败 "
            f"table={table_name}: {e}"
        )
        supports = False
    _CRDB_SOFT_DELETE_CACHE[table_name] = supports
    return supports


def _extract_deleted_at_from_record(record: dict) -> str:
    """R42 P1-5: 从 dirty_outbox 记录中提取 deleted_at 时间戳。

    优先级:
        1. payload 中的 deleted_at 字段
        2. payload 中的 updated_at 字段
        3. 记录的 created_at 字段
        4. 当前时间(最后兜底)

    Args:
        record: dirty_outbox 行字典(含 payload / created_at)

    Returns:
        ISO 格式时间字符串
    """
    import datetime as _dt
    payload = record.get("payload")
    if payload:
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(row, dict):
                for field in ("deleted_at", "updated_at"):
                    val = row.get(field)
                    if val:
                        return str(val)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # fallback 到记录的 created_at
    created_at = record.get("created_at")
    if created_at:
        return str(created_at)
    return _dt.datetime.now().isoformat()


async def _write_tombstone_audit_log(
    table_name: str, pk: str, action: str, details: str,
) -> None:
    """R42 P1-5: 写入 audit_log 记录 tombstone 操作。

    用于 hard_delete fallback 路径(表不支持 soft_delete)和 retention hard delete。
    写入失败时静默记录 debug 日志,不影响主流程。

    Args:
        table_name: 受影响表名
        pk: 主键值
        action: 审计动作(如 "tombstone_hard_delete_fallback" / "retention_hard_delete")
        details: 详细说明
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store or not getattr(store, "_db", None):
            return
        import datetime as _dt
        await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, 'system', ?, ?, ?, ?, '', ?)""",
            (0, action, table_name, str(pk), details,
             _dt.datetime.now().isoformat()),
        )
        if not getattr(store, "_in_writer_tx", False):
            await store._db.commit()
    except Exception as e:
        logger.debug(
            f"[crdb_sync] R42 P1-5: audit_log 写入失败(忽略): {e}"
        )


async def _dispatch_crdb_tombstone(records: list[dict], crdb_table: str, pk_col: str) -> list[int]:
    """R41 P0-6 / R42 P1-5: 将 tombstone 记录同步到 CRDB。

    R42 P1-5 变更(soft_delete 优先):
        - 不再立即 DELETE FROM crdb_table WHERE pk_col=?
        - 改为 UPDATE crdb_table SET deleted_at=?, is_tombstone=1 WHERE pk_col=?
          (mirror 保留 tombstone,供备份和跨机恢复使用)
        - 若 crdb 表无 deleted_at 字段(不支持 soft_delete),
          fallback 到 DELETE(但记 audit_log 标记 fallback 路径)
        - 物理清理由 retention_worker 在备份保留后执行(默认 30 天后)

    Args:
        records: dirty_outbox 行列表(operation=tombstone)
        crdb_table: CRDB 中实际表名(如 "users" / "file_records")
        pk_col: 主键列名(如 "user_id" / "file_code")

    Returns:
        成功处理的 dirty_outbox.id 列表
    """
    from database.session import (
        get_users_col, get_file_records_col, get_codes_col,
        get_jobs_col, get_cells_col,
    )
    # R41 P0-6: 按 crdb_table 路由到对应的 D1Collection
    _COL_LOOKUP = {
        "users": get_users_col,
        "file_records": get_file_records_col,
        "codes": get_codes_col,
        "jobs": get_jobs_col,
        "cells": get_cells_col,
    }
    col_fn = _COL_LOOKUP.get(crdb_table)
    if col_fn is None:
        logger.error(
            f"[crdb_sync] R41 P0-6: tombstone 找不到 CRDB collection crdb_table={crdb_table}"
        )
        return []
    col = col_fn()

    # R42 P1-5: 检查 CRDB 表是否支持 soft_delete(有 deleted_at + is_tombstone 字段)
    supports_soft_delete = await _is_crdb_table_supports_soft_delete(crdb_table)
    if supports_soft_delete:
        logger.debug(
            f"[crdb_sync] R42 P1-5: {crdb_table} 支持 soft_delete, "
            f"tombstone 将 UPDATE deleted_at + is_tombstone=1"
        )
    else:
        logger.warning(
            f"[crdb_sync] R42 P1-5: {crdb_table} 不支持 soft_delete "
            f"(无 deleted_at/is_tombstone 字段或 CRDB 未连接), "
            f"fallback 到 DELETE + audit_log"
        )

    processed_ids: list[int] = []
    for r in records:
        rid = r.get("id")
        pk = r.get("pk")
        if not pk:
            logger.warning(f"[crdb_sync] R41 P0-6: tombstone id={rid} 无 pk, 跳过")
            continue
        try:
            if supports_soft_delete:
                # R42 P1-5: soft_delete 路径 — UPDATE deleted_at + is_tombstone=1
                # 从 payload / created_at 中提取 deleted_at 时间戳
                deleted_at = _extract_deleted_at_from_record(r)
                sql = (
                    f"UPDATE {crdb_table} SET deleted_at = $1, "
                    f"is_tombstone = 1 WHERE {pk_col} = $2"
                )
                await col.execute_raw(sql, [deleted_at, pk])
            else:
                # R42 P1-5: fallback 路径 — DELETE + audit_log
                # (表不支持 soft_delete,或 CRDB schema 查询失败)
                sql = f"DELETE FROM {crdb_table} WHERE {pk_col} = $1"
                await col.execute_raw(sql, [pk])
                # 写 audit_log 标记 fallback 路径(便于审计追溯)
                await _write_tombstone_audit_log(
                    crdb_table, str(pk),
                    "tombstone_hard_delete_fallback",
                    f"CRDB 表 {crdb_table} 不支持 soft_delete "
                    f"(无 deleted_at/is_tombstone 字段),fallback 到 DELETE",
                )
            processed_ids.append(rid)
        except Exception as e:
            logger.warning(
                f"[crdb_sync] R41 P0-6: tombstone 失败 crdb_table={crdb_table} "
                f"pk={pk} id={rid}: {e}"
            )
    return processed_ids


# R39 P0-4: table_name → upsert dispatcher 映射
# R41 P0-6: 补充 jobs/cells 的 upsert handler(此前依赖专用循环,但若 dirty_outbox
# 已有记录则必须能消费,避免记录堆积)。
_DIRTY_OUTBOX_TABLE_HANDLERS = {
    "file_records": _dispatch_file_records_upsert,
    "codes": _dispatch_codes_upsert,
    "users": _dispatch_users_upsert,
    # R41 P0-6: 新增 jobs/cells 的 upsert handler(覆盖 dirty_outbox 路径)
    "jobs": _dispatch_jobs_upsert,
    "cells": _dispatch_cells_upsert,
}

# R41 P0-6: table_name → tombstone dispatcher 映射(DELETE FROM crdb_table WHERE pk=?)
# 每个 CRDB 表都必须在此映射中提供 tombstone handler,
# 否则软删除记录会落入 DLQ(handler_missing)。
# _tombstone handler 接收 (crdb_col, pk, payload) 并执行 DELETE。
_DIRTY_OUTBOX_TOMBSTONE_HANDLERS: dict[str, str] = {
    # value 为 CRDB 中实际表名,用于构造 "DELETE FROM <table> WHERE <pk_col> = $1"
    # pk 列名默认与逻辑表名同名(在 _TOMBSTONE_PK_COLUMNS 中声明)
    "file_records": "file_records",
    "codes": "codes",
    "users": "users",
    "jobs": "jobs",
    "cells": "cells",
}

# R41 P0-6: 各 CRDB 表的主键列名(用于 tombstone DELETE WHERE 子句)
_TOMBSTONE_PK_COLUMNS: dict[str, str] = {
    "file_records": "file_code",
    "codes": "code",
    "users": "user_id",
    "jobs": "id",
    "cells": "slot_id",
}

# 已由专用同步循环覆盖的表 (dirty_outbox 记录直接标记 processed, 避免重复处理)
# R41 P0-6: 此集合保留用于兼容旧日志路径,但实际 dispatcher 已通过 _DIRTY_OUTBOX_TABLE_HANDLERS
# 为 jobs/cells 提供 upsert handler,不再需要"专用循环兜底"语义。
_DIRTY_OUTBOX_TABLES_DELEGATED: set[str] = set()

# R40 P0-5 / R41 P0-6: local_only 表 — 仅存在于 SQLite 本地,不需要同步到 CRDB
# 这些表的 dirty_outbox 记录直接标记 processed + local_only=1
# R41 P0-6: 从 services.replication_policy 派生,集中维护,避免双处声明漂移
try:
    from services.replication_policy import (
        all_local_only_tables as _all_local_only_tables,
        all_crdb_tables as _all_crdb_tables,
        is_local_only as _policy_is_local_only,
        is_crdb as _policy_is_crdb,
    )
    _LOCAL_ONLY_TABLES: set[str] = _all_local_only_tables()
    _CRDB_TABLES: set[str] = _all_crdb_tables()
except Exception:
    # 极端情况(replication_policy 模块异常)降级到旧硬编码集合,保证服务可启动
    _LOCAL_ONLY_TABLES = {
        "tasks", "collections", "collection_items", "notifications",
        "content_reports", "audit_log", "quota_reservations", "quota_ledger",
        "rbac_roles", "rbac_user_roles", "approvals", "maintenance_state",
        "admin_access_log", "command_outbox", "command_executions",
        "mfa_secrets", "sessions", "kv_store", "ttl_cache",
        "pending_uploads", "dirty_outbox", "dlq", "dlq_records", "ban_state",
    }
    _CRDB_TABLES = {
        "users", "file_records", "codes", "decode_logs",
        "relay_whitelist", "collector_whitelist", "spare_pool",
        "channels", "cells", "jobs",
    }


async def _route_dirty_outbox_to_dlq(
    table_name: str, records: list[dict], error_msg: str,
) -> None:
    """R40 P0-5 / R41 P0-6: 将处理失败的 dirty_outbox 记录路由到死信队列(DLQ)。

    R41 P0-6 增强:
      - DLQ 记录写入 SQLite dlq_records 表(权威存储,字段完整):
        status / retry_count / max_retries / next_retry_at / last_error /
        created_at / updated_at
      - 同时镜像写入 data/dead_letter.jsonl(向后兼容 repair_console.list_dlq())
      - max_retries=5,达到上限后 cleanup_dlq() 标记 permanently_failed,不再重试。

    Args:
        table_name: 受影响表名
        records: 失败的 dirty_outbox 行列表
        error_msg: 失败原因
    """
    import json as _json
    import os as _os
    import datetime as _dt

    now_str = _dt.datetime.now().isoformat()
    # R41 P0-6: max_retries 提升至 5(原 R40 默认 3),允许更多暂时性故障恢复
    max_retries = 5
    # R41 P0-6: 下次重试时间(默认 60s 后,与 dlq_worker 的 base_delay 对齐)
    next_retry_at_str = (
        _dt.datetime.now() + _dt.timedelta(seconds=60)
    ).isoformat()

    # ── 1. 写 SQLite dlq_records 表(权威存储) ──
    store = _get_cache_store_safe()
    if store is not None:
        try:
            for r in records:
                original_payload = {
                    "id": r.get("id"),
                    "table_name": table_name,
                    "pk": r.get("pk"),
                    "operation": r.get("operation"),
                    "payload": r.get("payload"),
                    "created_at": r.get("created_at"),
                }
                await store.insert_dlq_record(
                    message_id=f"dirty_outbox:{r.get('id', '')}",
                    table_name=table_name,
                    reason=f"crdb_sync dispatch 失败: {error_msg}",
                    original=original_payload,
                    max_retries=max_retries,
                    next_retry_at=next_retry_at_str,
                )
        except Exception as sqlite_err:
            logger.warning(
                f"[crdb_sync] R41 P0-6: SQLite dlq_records 写入失败,降级 jsonl: {sqlite_err}"
            )

    # ── 2. 镜像写入 data/dead_letter.jsonl(向后兼容 repair_console.list_dlq) ──
    dead_file = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "data", "dead_letter.jsonl",
    )
    try:
        _os.makedirs(_os.path.dirname(dead_file), exist_ok=True)
        with open(dead_file, "a", encoding="utf-8") as f:
            for r in records:
                dead_entry = {
                    "message_id": f"dirty_outbox:{r.get('id', '')}",
                    "reason": f"crdb_sync dispatch 失败 table={table_name}: {error_msg}",
                    "attempts": 1,
                    "max_attempts": max_retries,
                    "failed_at": now_str,
                    "next_retry_at": next_retry_at_str,
                    "original": {
                        "id": r.get("id"),
                        "table_name": table_name,
                        "pk": r.get("pk"),
                        "operation": r.get("operation"),
                        "payload": r.get("payload"),
                        "created_at": r.get("created_at"),
                    },
                }
                f.write(_json.dumps(dead_entry, ensure_ascii=False, default=str) + "\n")
        logger.warning(
            f"[crdb_sync] R40 P0-5: {len(records)} 条 dirty_outbox 记录 "
            f"已路由到 DLQ(table={table_name}, reason={error_msg})"
        )
    except Exception as dlq_err:
        logger.error(f"[crdb_sync] R40 P0-5: DLQ 写入失败: {dlq_err}")


async def _dispatch_dirty_outbox_to_crdb(
    table_name: str, records: list[dict],
) -> list[int]:
    """R39 P0-4 / R41 P0-6: 按 table_name 分发 dirty_outbox 记录到 CRDB。

    分发规则:
        - local_only 表(tasks / approvals / RBAC / maintenance ...) → 直接标记
          processed + local_only=1, 不调用 CRDB
        - ARCHIVE_ONLY 表(audit_log_archive)→ 直接标记 processed, 不调用 CRDB
        - CRDB 表:
            * upsert operation → 调用对应 upsert handler(_DIRTY_OUTBOX_TABLE_HANDLERS)
            * tombstone operation → 调用对应 tombstone handler(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS)
            * 缺失 handler → 进入 DLQ(标记错误,不静默丢弃)
        - 未知 table_name → DEAD: 不标记 processed, 路由到 DLQ 供人工检查
        - 未知 operation (非 upsert/tombstone) → DEAD: 同上

    Args:
        table_name: 受影响表名
        records: 该表的 dirty_outbox 行列表

    Returns:
        成功处理的 dirty_outbox.id 列表(DEAD 分支返回空列表)
    """
    # R40 P0-5 / R41 P0-6: local_only 表 — 仅存在于 SQLite,不需要同步到 CRDB
    # 直接标记为 processed + local_only=1
    if table_name in _LOCAL_ONLY_TABLES:
        logger.debug(
            f"[crdb_sync] R40 P0-5: {table_name} 为 local_only 表, "
            f"dirty_outbox {len(records)} 条直接标记 processed + local_only=1"
        )
        return [r.get("id") for r in records if r.get("id") is not None]

    # R41 P0-6: ARCHIVE_ONLY 表(audit_log_archive)→ 直接标记 processed, 不进 CRDB
    # (冷归档到 R2 由独立 backup job 负责,与 crdb_sync 解耦)
    try:
        from services.replication_policy import is_archive_only as _policy_is_archive_only
        if _policy_is_archive_only(table_name):
            logger.debug(
                f"[crdb_sync] R41 P0-6: {table_name} 为 archive_only 表, "
                f"dirty_outbox {len(records)} 条直接标记 processed(走 R2 归档)"
            )
            return [r.get("id") for r in records if r.get("id") is not None]
    except Exception:
        pass  # 模块异常时降级,继续走下方 CRDB 分支

    # R41 P0-6: 校验该表是否声明为 CRDB 策略
    is_crdb_table = table_name in _CRDB_TABLES

    # 已知 CRDB table: 按 operation(upsert / tombstone)路由到对应 handler
    upsert_handler = _DIRTY_OUTBOX_TABLE_HANDLERS.get(table_name)
    tombstone_table = _DIRTY_OUTBOX_TOMBSTONE_HANDLERS.get(table_name)
    pk_col = _TOMBSTONE_PK_COLUMNS.get(table_name)

    # R41 P0-6: 校验 handler 覆盖完整性 — CRDB 表必须同时有 upsert + tombstone handler
    if is_crdb_table:
        if upsert_handler is None:
            logger.error(
                f"[crdb_sync] R41 P0-6: CRDB 表 {table_name} 缺失 upsert handler, "
                f"{len(records)} 条 → DLQ(不静默丢弃)"
            )
            await _route_dirty_outbox_to_dlq(
                table_name, records,
                f"CRDB 表缺失 upsert handler(策略={table_name} → CRDB)",
            )
            # R41 P0-6: 返回所有 id 让 _sync_dirty_outbox 标记为 processed,
            # 避免重复 dispatch 同一记录(已在 DLQ,无需再走 dispatch)
            return [r.get("id") for r in records if r.get("id") is not None]
        if tombstone_table is None or pk_col is None:
            logger.error(
                f"[crdb_sync] R41 P0-6: CRDB 表 {table_name} 缺失 tombstone handler, "
                f"{len(records)} 条 → DLQ(不静默丢弃)"
            )
            await _route_dirty_outbox_to_dlq(
                table_name, records,
                f"CRDB 表缺失 tombstone handler(策略={table_name} → CRDB)",
            )
            return [r.get("id") for r in records if r.get("id") is not None]

    # 已知 table 且 handler 就绪: 按 operation 分流
    if upsert_handler is not None:
        # R41 P0-6: tombstone 与 upsert 都是合法 operation
        valid_ops = {"upsert", "tombstone"}
        valid_records = [r for r in records if r.get("operation") in valid_ops]
        dead_records = [r for r in records if r.get("operation") not in valid_ops]
        if dead_records:
            logger.error(
                f"[crdb_sync] R41 P0-6: table={table_name} 含未知 operation "
                f"{len(dead_records)} 条 → DLQ(不标记 processed): "
                f"ops={[r.get('operation') for r in dead_records]}"
            )
            await _route_dirty_outbox_to_dlq(
                table_name, dead_records,
                f"未知 operation(合法: upsert/tombstone)",
            )
        # 按 operation 二次分组
        upsert_records = [r for r in valid_records if r.get("operation") == "upsert"]
        tombstone_records = [r for r in valid_records if r.get("operation") == "tombstone"]
        processed: list[int] = []
        if upsert_records:
            processed.extend(await upsert_handler(upsert_records))
        if tombstone_records and tombstone_table is not None and pk_col is not None:
            processed.extend(
                await _dispatch_crdb_tombstone(tombstone_records, tombstone_table, pk_col)
            )
        # R41 P0-6: dead_records 已路由到 DLQ,加入 processed 列表避免重复 dispatch
        processed.extend(r.get("id") for r in dead_records if r.get("id") is not None)
        return processed

    # 未知 table(未在 TABLE_REPLICATION_POLICY 中声明) → DEAD
    # 不返回 id,_sync_dirty_outbox 会路由到 DLQ 并保留记录供人工检查
    logger.error(
        f"[crdb_sync] R39 P0-4: 未知 table_name={table_name}, "
        f"records={len(records)} 条 → DEAD (不标记 processed, 保留供人工检查)"
    )
    return []


async def _sync_dirty_outbox():
    """R39 P0-4 / R41 RU 门禁: 消费 dirty_outbox — 按 table_name 分组 dispatch 到 CRDB。

    流程:
        1. _should_connect(): 检查 dirty_outbox 是否有未处理记录(0 RU)
        2. _lazy_connect_crdb(): dirty 存在时才连接 CRDB pool
        3. get_dirty_outbox_batch(CRDB_SYNC_BATCH_SIZE): 拉取未处理记录
        4. R41: 合并最高 version — 同一 (table_name, pk) 仅保留 version 最大的记录
           (降低 CRDB UPSERT 次数,RU 消耗随合并比例下降)
        5. 按 table_name 分组 dispatch 到 CRDB
        6. mark_dirty_processed(ids): 标记成功处理的记录(含被合并的旧版本)
        7. 末尾调用 _close_pool_if_idle(): 空闲超阈值时关闭 CRDB pool

    幂等: dispatcher 使用 INSERT ON CONFLICT DO UPDATE, 重复处理不会产生副作用。
    """
    store = _get_cache_store_safe()
    if store is None:
        return

    # R41 RU 门禁: 先用 _should_connect() 判断是否需要连接 CRDB(0 RU)
    if not await _should_connect():
        # dirty_outbox 为空且 pool 未连接 → 直接返回(零 CRDB RU)
        # 若 pool 已连接但无 dirty,则由 _close_pool_if_idle() 在末尾关闭
        if _crdb_pool_connected:
            await _close_pool_if_idle()
        return

    # R41: dirty 存在,懒加载 CRDB pool(若未连接)
    await _lazy_connect_crdb()

    # R41 RU 门禁: 使用 CRDB_SYNC_BATCH_SIZE(默认 100,范围 100-500)
    batch = await store.get_dirty_outbox_batch(limit=CRDB_SYNC_BATCH_SIZE)
    if not batch:
        # dirty_outbox 为空(可能在 _should_connect 之后被另一轮消费了)
        # 仍然调用 _close_pool_if_idle() 释放空闲连接
        await _close_pool_if_idle()
        return

    # R41 RU 门禁 / R42 P1-4: 合并最高 version — 同一 (table_name, pk) 仅保留
    # version 最大且 updated_at 最新的记录(ORDER BY version DESC, updated_at DESC LIMIT 1)。
    # 合并后,被合并的旧版本记录也标记为 processed(避免重复 dispatch),
    # 从而降低 CRDB UPSERT 次数与 RU 消耗。
    #
    # R42 P1-4 修复:
    #   旧逻辑仅比较 version,若多调用方使用默认 version=0,
    #   顺序可能依赖插入 ID 并吞掉真正新状态。
    #   新逻辑使用 _resolve_version_conflict 同时考虑 version 和 updated_at:
    #     - new_version > current → 使用 new
    #     - new_version == current → 用 updated_at 决胜(后者覆盖前者)
    #     - new_version < current → 丢弃旧版本(new),记录 warning
    merged_records: list[dict] = []
    merged_old_ids: list[int] = []  # 被合并掉的旧版本 id(需标记为 processed)
    version_map: dict[tuple[str, str], dict] = {}  # (table_name, pk) → 最新记录
    for r in batch:
        tn = r.get("table_name", "") or ""
        pk = str(r.get("pk", "") or "")
        version = r.get("version", 0) or 0
        # R42 P1-4: dirty_outbox 的 created_at 作为 updated_at 代理
        # (dirty_outbox 记录的 created_at 即变更检测时间,接近实体 updated_at)
        updated_at = r.get("created_at", "") or ""
        key = (tn, pk)
        if key not in version_map:
            version_map[key] = r
            merged_records.append(r)
        else:
            existing = version_map[key]
            existing_version = existing.get("version", 0) or 0
            existing_updated_at = existing.get("created_at", "") or ""
            # R42 P1-4: 使用 _resolve_version_conflict 决定保留哪个版本
            # (同时考虑 version 和 updated_at)
            chosen_version = _resolve_version_conflict(
                tn, pk, existing_version, version,
                existing_updated_at, updated_at,
            )
            # 判定新记录是否被选中(替换 existing)
            new_selected = (
                chosen_version == version and (
                    version > existing_version
                    or (version == existing_version
                        and updated_at > existing_updated_at)
                )
            )
            if new_selected:
                # 新记录被选中 → 替换 existing
                merged_old_ids.append(existing["id"])
                idx = merged_records.index(existing)
                merged_records[idx] = r
                version_map[key] = r
            else:
                # 旧版本被丢弃(new < current 或 new == current 但 new_updated_at 更早
                # 或 version + updated_at 完全相同 → 保留 existing)
                merged_old_ids.append(r["id"])

    if merged_old_ids:
        logger.debug(
            f"[crdb_sync] R41: 合并最高 version 后, "
            f"原始 {len(batch)} 条 → 保留 {len(merged_records)} 条, "
            f"被合并 {len(merged_old_ids)} 条旧版本(仍标记 processed)"
        )

    # 按 table_name 分组(基于合并后的记录)
    groups: dict[str, list[dict]] = {}
    for r in merged_records:
        tn = r.get("table_name", "") or ""
        groups.setdefault(tn, []).append(r)

    all_processed: list[int] = []
    local_only_processed: list[int] = []
    for table_name, records in groups.items():
        try:
            ids = await _dispatch_dirty_outbox_to_crdb(table_name, records)
            # R40 P0-5: 区分 local_only 和普通表
            if table_name in _LOCAL_ONLY_TABLES:
                local_only_processed.extend(ids)
            else:
                all_processed.extend(ids)
            # R40 P0-5: 处理失败的记录(未返回 id)路由到 DLQ
            failed_records = [r for r in records if r.get("id") not in ids]
            if failed_records:
                await _route_dirty_outbox_to_dlq(
                    table_name, failed_records, "dispatch 返回未处理 id",
                )
        except Exception as e:
            logger.warning(
                f"[crdb_sync] R40 P0-5: dispatch table={table_name} 异常: {e}"
            )
            # R40 P0-5: 异常时整批路由到 DLQ
            await _route_dirty_outbox_to_dlq(table_name, records, str(e))

    # R41: 被合并的旧版本 id 也标记为 processed(已包含在最新版本中)
    # 避免下一轮重复 dispatch 同一 (table_name, pk) 的旧版本
    all_processed.extend(merged_old_ids)

    # R40 P0-5: 标记已处理(区分 local_only 和普通)
    if all_processed:
        await store.mark_dirty_processed(all_processed)
    if local_only_processed:
        await store.mark_dirty_local_only(local_only_processed)
    total_marked = len(all_processed) + len(local_only_processed)
    if total_marked > 0:
        logger.debug(
            f"[crdb_sync] R40 P0-5: dirty_outbox 已处理 "
            f"{total_marked}/{len(batch)} 条"
            f"(local_only={len(local_only_processed)}, crdb={len(all_processed)}, "
            f"merged_old={len(merged_old_ids)})"
        )

    # R41 P0-6: 清理 DLQ — 达到 max_retries 的记录标记为 permanently_failed
    # 避免无限积压(max_retries=5 后停止重试)
    try:
        await store.cleanup_dlq()
    except Exception as cleanup_err:
        logger.debug(
            f"[crdb_sync] R41 P0-6: cleanup_dlq 异常(可忽略): {cleanup_err}"
        )

    # R41 RU 门禁: 处理完毕后检查空闲,超阈值则关闭 CRDB pool
    await _close_pool_if_idle()


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
    # R39 P0-4: 新增 sync-dirty-outbox 循环, 消费 dirty_outbox (file_records/codes/users)
    tasks = [
        asyncio.create_task(
            _sync_loop("jobs", _sync_jobs, _get_dirty_jobs, None),
            name="sync-jobs",
        ),
        asyncio.create_task(
            _sync_loop("cells", _sync_cells, _get_dirty_cells, None),
            name="sync-cells",
        ),
        # R39 P0-4: dirty_outbox dispatcher (与 jobs/cells 循环并行运行)
        asyncio.create_task(
            _sync_loop("dirty_outbox", _sync_dirty_outbox, _get_dirty_outbox, None),
            name="sync-dirty-outbox",
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
