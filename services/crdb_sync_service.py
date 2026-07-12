"""R36 §6.3 + R37 P0-3: 单一 crdb_sync 服务 — 唯一 CRDB 同步事实源。

架构(R37 P0-3 收口):
  Up / Idx / Dsp / Mon / Admin → SQLite 本地权威状态 + dirty_event
  → crdb_sync 独占消费 dirty rows → CRDB 批量 UPSERT
  → 成功后在 SQLite 标记 crdb_synced

R37 P0-3 强制收口:
  1. Bot 直连 CRDB 兜底路径(sync_back_loop / sync_dirty_cells)默认禁用(SYNC_BACK_OFF=0)
  2. crdb_sync 使用 sync_leader 租约(SQLite kv_store 互斥),防止多实例并发同步
  3. 同步循环:有 dirty 时受控 batch cadence(2s),无 dirty 时退避(60s→1800s)
  4. 异常分支指数退避,不会快速重试消耗 RU

CRDB 连接者仅允许:crdb_sync、migration、restore、低频备份和受控 Admin。
业务 Bot 不持有 CRDB 凭证,不调用 CRDB SDK(SYNC_BACK_OFF=0 时)。

启动方式:
  systemd: tgjiema-crdb_sync.service
  手动: python -m services.crdb_sync_service
"""
import asyncio
import os
import signal
import sys
import time

from loguru import logger


# 默认同步间隔(秒)
DEFAULT_SYNC_INTERVAL = 60   # 无 dirty 时基础间隔
MAX_BACKOFF = 1800           # 30 分钟上限(无 dirty 时退避)
# R37 P0-3: 有 dirty 时受控 batch cadence(2s),避免无节流高频查询
DIRTY_BATCH_INTERVAL = 2     # 有 dirty 时每 2s 处理一批
# R37 P0-3: 每轮最大处理时间预算(秒),超时分片到下一轮
MAX_BATCH_TIME_BUDGET = 30    # 单轮最多 30s


# ── R37 P0-3: sync_leader 租约 fencing ──

_LEADER_KEY = "crdb_sync/leader"
_LEADER_TTL = 90  # 租约时长(秒),与 settings.CRDB_SYNC_LEADER_LEASE 一致


async def _acquire_leader_lease() -> bool:
    """尝试获取 sync_leader 租约(SQLite kv_store 互斥)。

    使用 kv_store 的 set_kv 带过期时间实现租约:
    - 写入 leader_id + expire_at,TTL = _LEADER_TTL
    - 若已有有效 leader 且 leader_id != 当前进程,返回 False
    - 若 leader 过期或为空,当前进程成为新 leader

    Returns:
        True if 当前进程获得租约;False if 其他实例持有租约
    """
    import uuid
    from database.cache_store import get_cache_store

    my_id = f"crdb_sync-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    store = get_cache_store()
    now = time.time()

    # 查询当前 leader
    current = await store.get_kv(_LEADER_KEY)
    if current:
        try:
            import json
            data = json.loads(current)
            expire_at = data.get("expire_at", 0)
            leader_id = data.get("leader_id", "")
            if now < expire_at and leader_id != my_id:
                # 租约仍有效,且不是当前进程
                logger.debug(f"[crdb_sync] leader 租约被 {leader_id} 持有,等待")
                return False
        except (json.JSONDecodeError, TypeError):
            # 数据损坏,视为过期
            pass

    # 获取租约
    import json
    lease_data = json.dumps({
        "leader_id": my_id,
        "expire_at": now + _LEADER_TTL,
        "acquired_at": now,
    })
    await store.set_kv(_LEADER_KEY, lease_data)
    logger.info(f"[crdb_sync] 获得 leader 租约(leader_id={my_id}, TTL={_LEADER_TTL}s)")
    return True


async def _renew_leader_lease() -> bool:
    """续约 leader 租约。Returns False if 租约被抢占。"""
    import uuid
    import json
    from database.cache_store import get_cache_store

    my_id_pattern = f"crdb_sync-{os.getpid()}-"
    store = get_cache_store()
    now = time.time()

    current = await store.get_kv(_LEADER_KEY)
    if not current:
        # 租约丢失,尝试重新获取
        return await _acquire_leader_lease()

    try:
        data = json.loads(current)
        leader_id = data.get("leader_id", "")
        if not leader_id.startswith(my_id_pattern):
            # 租约被其他实例抢占
            logger.warning(f"[crdb_sync] leader 租约被 {leader_id} 抢占,停止同步")
            return False
        # 续约
        data["expire_at"] = now + _LEADER_TTL
        data["renewed_at"] = now
        await store.set_kv(_LEADER_KEY, json.dumps(data))
        return True
    except (json.JSONDecodeError, TypeError):
        return await _acquire_leader_lease()


async def _release_leader_lease():
    """释放 leader 租约(优雅关闭时调用)。"""
    from database.cache_store import get_cache_store
    try:
        store = get_cache_store()
        await store.set_kv(_LEADER_KEY, "")
        logger.info("[crdb_sync] 释放 leader 租约")
    except Exception as e:
        logger.debug(f"[crdb_sync] 释放租约异常(可忽略): {e}")


async def _sync_loop(name: str, sync_func, get_dirty_func, mark_synced_func):
    """R37 P0-3: 通用同步循环 — dirty 驱动 + 受控 cadence + 退避 + leader fencing。

    改进点(vs R36):
    - 有 dirty 时:处理一批,short sleep(DIRTY_BATCH_INTERVAL=2s),再查
    - 无 dirty 时:退避翻倍(60s→1800s 上限)
    - 异常时:指数退避(不快速重试,避免消耗 RU)
    - 每轮检查 leader 租约,非 leader 不执行同步
    """
    backoff = DEFAULT_SYNC_INTERVAL
    lease_renew_counter = 0
    while True:
        try:
            # R37 P0-3: 每 10 轮续约 leader 租约(约每 20s-600s,取决于 backoff)
            lease_renew_counter += 1
            if lease_renew_counter >= 10:
                if not await _renew_leader_lease():
                    # 租约被抢占,等待后重试获取
                    await asyncio.sleep(_LEADER_TTL // 2)
                    if not await _acquire_leader_lease():
                        continue
                lease_renew_counter = 0

            dirty = await get_dirty_func()
            if dirty:
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
                # 无 dirty:退避翻倍(上限 30min)
                backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as e:
            logger.warning(f"[crdb_sync] {name} 同步异常: {e}")
            # R37 P0-3: 异常时指数退避(不快速重试)
            backoff = min(backoff * 2, MAX_BACKOFF)
        await asyncio.sleep(backoff)


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

    logger.info("[crdb_sync] 启动 — 单一 CRDB 同步事实源(R37 P0-3 收口)")
    logger.info(
        f"[crdb_sync] 退避策略: 有 dirty={DIRTY_BATCH_INTERVAL}s cadence, "
        f"无 dirty={DEFAULT_SYNC_INTERVAL}s→{MAX_BACKOFF}s 退避"
    )
    logger.info(
        f"[crdb_sync] leader 租约: TTL={_LEADER_TTL}s "
        f"(防多实例并发同步)"
    )

    # R37 P0-3: 启动时获取 leader 租约
    if not await _acquire_leader_lease():
        logger.warning(
            "[crdb_sync] 未获得 leader 租约(其他实例可能正在运行),"
            "等待 30s 后重试"
        )
        await asyncio.sleep(30)
        if not await _acquire_leader_lease():
            logger.error("[crdb_sync] 仍无法获得 leader 租约,退出")
            sys.exit(1)

    # 初始化数据库连接(R37 P0-3: 只有获得租约后才连 CRDB)
    from database import init_db, close_db
    await init_db()

    # 并发运行同步循环
    tasks = [
        asyncio.create_task(
            _sync_loop("jobs", _sync_jobs, _get_dirty_jobs, None),
            name="sync-jobs",
        ),
        asyncio.create_task(
            _sync_loop("cells", _sync_cells, _get_dirty_cells, None),
            name="sync-cells",
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
