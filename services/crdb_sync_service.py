"""R36 §6.3: 单一 crdb_sync 服务 — 唯一 CRDB 同步事实源。

架构:
  Up / Idx / Dsp / Mon / Admin → SQLite 本地权威状态 + dirty_event
  → crdb_sync 独占消费 dirty rows → CRDB 批量 UPSERT
  → 成功后在 SQLite 标记 crdb_synced

CRDB 连接者仅允许:crdb_sync、migration、restore、低频备份和受控 Admin。
其他业务 Bot 的周期同步(dsp_bot sync_back_loop / mon_bot sync_dirty_cells)
作为兜底保留,中长期完全由 crdb_sync 接管(可设置 SYNC_BACK_OFF=0 禁用)。

启动方式:
  systemd: tgjiema-crdb_sync.service
  手动: python -m services.crdb_sync_service

R36 §6.4.6: metrics/心跳/channel health/临时 job progress 永不写 CRDB(本服务不处理)。
R36 §6.4.7: 同步采用 updated_at + pk 幂等 UPSERT 批量提交,禁止逐行网络往返。
"""
import asyncio
import os
import signal
import sys

from loguru import logger


# 默认同步间隔(秒):有 dirty 时立即处理,无 dirty 时退避
DEFAULT_SYNC_INTERVAL = 60  # 1 分钟基础间隔
MAX_BACKOFF = 1800          # 30 分钟上限


async def _sync_loop(name: str, sync_func, get_dirty_func, mark_synced_func):
    """通用同步循环: dirty 驱动 + 退避。

    Args:
        name: 循环名(用于日志)
        sync_func: 同步函数(无参数,内部完成批量 UPSERT + 标记 synced)
        get_dirty_func: 查询 dirty 数量的函数(返回 list)
        mark_synced_func: 标记单条已同步的函数(本服务不直接调用,sync_func 内部完成)
    """
    backoff = DEFAULT_SYNC_INTERVAL
    while True:
        try:
            dirty = await get_dirty_func()
            if dirty:
                await sync_func()
                backoff = DEFAULT_SYNC_INTERVAL
                logger.debug(f"[crdb_sync] {name}: 处理 {len(dirty)} 条 dirty,重置退避")
            else:
                # 无 dirty:退避翻倍(上限 30min)
                backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as e:
            logger.warning(f"[crdb_sync] {name} 同步异常: {e}")
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

    logger.info("[crdb_sync] 启动 — 单一 CRDB 同步事实源(R36 §6.3)")
    logger.info(
        f"[crdb_sync] 退避策略: 有 dirty={DEFAULT_SYNC_INTERVAL}s, "
        f"无 dirty 翻倍至 {MAX_BACKOFF}s 上限"
    )
    logger.info("[crdb_sync] 同步循环: jobs + cells(其他表 dirty 由各 Bot 直写兜底)")

    # 初始化数据库连接
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
        await close_db()
        logger.info("[crdb_sync] 已停止")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
