"""R42 P1-2: 备份孤儿对象 GC(垃圾回收)。

职责:
    周期性调用 ``BackupEngine.cleanup_orphans()``,清理因上传中断
    而残留在 R2 的孤儿对象(payload + manifest 已上传但 COMPLETE marker
    缺失)。防止 R2 存储被无效备份对象长期占用。

设计要点:
    - 仅清理超时孤儿(默认 1 小时未完成 COMPLETE 的备份)
    - 不影响正在进行的备份(刚启动的备份不会被打断)
    - 写 audit_log 记录清理结果,便于审计追溯
    - GC 失败不传播(记录 warning 后继续下一周期)

调用方式:
    1. 独立协程(由 r40_scheduler 注册):
       ``await run_backup_gc_job()``
    2. 单次执行(用于手动触发 / CI 验证):
       ``stats = await run_backup_gc(timeout_seconds=3600)``
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger
from services.i18n import translate as _i18n_t


async def run_backup_gc(timeout_seconds: int = 3600) -> dict:
    """R42 P1-2: 执行一次备份孤儿对象 GC。

    委托 ``BackupEngine.cleanup_orphans(timeout_seconds)``,
    返回扫描/删除/错误统计。

    Args:
        timeout_seconds: 孤儿对象存活时间阈值(秒),默认 3600(1 小时)。
            超过此阈值仍未完成 COMPLETE marker 的备份视为孤儿,会被清理。

    Returns:
        {
            "scanned": int,    # 扫描的 backup_id 总数
            "deleted": int,    # 已删除的孤儿对象数
            "errors": int,     # 删除失败的对象数
            "details": str,    # 文本摘要
        }

    Raises:
        Exception: 仅在 BackupEngine 初始化失败时抛(其余异常被捕获并返回 0 统计)
    """
    try:
        from services.backup_engine import BackupEngine
        engine = BackupEngine()
        stats = await engine.cleanup_orphans(timeout_seconds=timeout_seconds)
        # 保证返回字段完整(防御性)
        return {
            "scanned": int(stats.get("scanned", 0)),
            "deleted": int(stats.get("deleted", 0)),
            "errors": int(stats.get("errors", 0)),
            "details": str(stats.get("details", "")),
        }
    except Exception as e:
        logger.error(f"[backup_gc] R42 P1-2: run_backup_gc 异常: {e}")
        return {
            "scanned": 0,
            "deleted": 0,
            "errors": 1,
            "details": _i18n_t('services.backup_gc.s1', e=e),
        }


async def run_backup_gc_job() -> None:
    """R42 P1-2: 备份 GC 作业便利函数(由 scheduler 周期调用)。

    与 ``run_backup_gc`` 区别:
        - 返回 None(不返回统计,适合 scheduler 直接 await)
        - 异常被捕获不传播(单次失败不影响 scheduler 主循环)
        - 默认超时阈值 3600 秒(1 小时)
    """
    try:
        stats = await run_backup_gc(timeout_seconds=3600)
        deleted = stats.get("deleted", 0)
        errors = stats.get("errors", 0)
        if deleted > 0 or errors > 0:
            logger.info(
                f"[backup_gc] R42 P1-2: GC 完成 "
                f"scanned={stats.get('scanned', 0)} "
                f"deleted={deleted} errors={errors}"
            )
        else:
            logger.debug(
                f"[backup_gc] R42 P1-2: GC 完成(无孤儿对象)"
            )
    except Exception as e:
        # 单次 GC 失败不阻塞 scheduler 主循环
        logger.warning(f"[backup_gc] R42 P1-2: run_backup_gc_job 异常: {e}")


async def _gc_loop(interval_seconds: int = 3600) -> None:
    """R42 P1-2: 备份 GC 主循环(每小时执行一次)。

    独立运行时使用:
        ``python -m services.backup_gc``

    Args:
        interval_seconds: GC 周期(秒),默认 3600(1 小时)
    """
    logger.info(
        f"[backup_gc] R42 P1-2: GC 循环已启动,间隔 {interval_seconds}s"
    )
    # 启动时执行一次
    await run_backup_gc_job()
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await run_backup_gc_job()
        except asyncio.CancelledError:
            logger.info("[backup_gc] R42 P1-2: GC 循环收到取消信号,退出")
            raise
        except Exception as e:
            logger.error(f"[backup_gc] R42 P1-2: GC 循环异常: {e}")
            await asyncio.sleep(60)  # 异常后等待 1 分钟再重试


def _handle_signal(signum, frame) -> None:
    """R42 P1-2: 信号处理(SIGTERM/SIGINT 优雅退出)。"""
    logger.info(f"[backup_gc] R42 P1-2: 收到信号 {signum},准备退出")
    sys.exit(0)


def main() -> None:
    """R42 P1-2: backup_gc 入口(独立 systemd unit)。"""
    import signal
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(_gc_loop())
    except KeyboardInterrupt:
        logger.info("[backup_gc] R42 P1-2: KeyboardInterrupt,退出")


if __name__ == "__main__":
    main()
