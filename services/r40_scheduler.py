"""R40: 定时任务调度器 — 清理过期数据 + 配额预留 + 指标采集 + 临时封禁自动解封。

职责:
    1. 每小时清理过期配额预留(超过 1 小时未结算的自动退款)
    2. 每天 3:00 清理过期数据(data_lifecycle)
    3. 每 5 分钟采集 RU 使用指标(委托 prometheus_exporter.collect_r40_metrics)
    4. 每小时执行临时封禁自动解封(P1-11: 委托 content_reports.cleanup_expired_bans)

设计原则:
    - 纯 async,通过 run_all.py BOT_RUNNERS 注册为独立进程
    - 所有任务异常均不传播,记录 warning 后继续下一个周期
    - 收到 CancelledError 时优雅退出
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import asyncio
import datetime as _dt

from loguru import logger


async def cleanup_expired_reservations_job() -> None:
    """每小时清理过期配额预留(超过 1 小时未结算的自动退款)。"""
    from services.quota_ledger import cleanup_expired_reservations
    try:
        count = await cleanup_expired_reservations()
        if count > 0:
            logger.info(f"[R40] 清理 {count} 个过期配额预留")
    except Exception as e:
        logger.warning(f"[R40] 清理配额预留异常: {e}")


async def cleanup_expired_bans_job() -> None:
    """每小时执行临时封禁自动解封(P1-11)。

    委托 services.content_reports.cleanup_expired_bans:
    - 批量查询 ban_expires_at 已过期的用户
    - 更新 is_banned=0、ban_expires_at=NULL
    - 为每条变更写 dirty_outbox 确保跨机同步
    """
    try:
        from services.content_reports import cleanup_expired_bans
        count = await cleanup_expired_bans()
        if count > 0:
            logger.info(f"[R40] 自动解封 {count} 个到期临时封禁用户")
    except Exception as e:
        logger.warning(f"[R40] 临时封禁自动解封异常: {e}")


async def cleanup_expired_data_job() -> None:
    """每天 3:00 清理过期数据。"""
    from services.data_lifecycle import cleanup_expired_data
    try:
        count = await cleanup_expired_data(batch_size=5000)
        if count > 0:
            logger.info(f"[R40] 清理 {count} 条过期数据")
    except Exception as e:
        logger.warning(f"[R40] 清理过期数据异常: {e}")


async def collect_ru_metrics_job() -> None:
    """每 5 分钟采集 RU 使用指标(委托 prometheus_exporter)。"""
    try:
        from services.prometheus_exporter import collect_r40_metrics
        await collect_r40_metrics()
    except Exception as e:
        logger.debug(f"[R40] RU 指标采集异常: {e}")


async def run_scheduler() -> None:
    """R40 定时任务调度器主循环。

    调度策略:
        - 每个周期(5 分钟)执行:清理配额预留 + 采集指标
        - 每小时执行:临时封禁自动解封(P1-11,周期计数器 mod 12 == 0)
        - 每天 3:00-3:05 执行:清理过期数据
        - 收到 CancelledError 时优雅退出
    """
    logger.info("[R40] 定时任务调度器已启动")
    # 周期计数器:每 12 个周期(12 * 5 分钟 = 1 小时)执行一次临时封禁解封
    _cycle_count = 0
    while True:
        try:
            now = _dt.datetime.now()
            _cycle_count += 1
            # 每个周期清理配额预留(实际频率由 quota_ledger 内部判断)
            await cleanup_expired_reservations_job()
            # 每个周期采集指标
            await collect_ru_metrics_job()
            # 每小时执行临时封禁自动解封(P1-11)
            if _cycle_count % 12 == 0:
                await cleanup_expired_bans_job()
            # 每天 3:00 清理过期数据(分钟 < 5 避免重复执行)
            if now.hour == 3 and now.minute < 5:
                await cleanup_expired_data_job()
            # 等待 5 分钟
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            logger.info("[R40] 定时任务调度器已停止")
            break
        except Exception as e:
            logger.warning(f"[R40] 调度器异常: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run_scheduler())
