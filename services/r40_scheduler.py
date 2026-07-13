"""R40 + R41: 定时任务调度器 — 清理过期数据 + 配额预留 + 指标采集 + 临时封禁自动解封 + 命令租约清理。

职责:
    1. 每小时清理过期配额预留(超过 1 小时未结算的自动退款)
    2. 每天 3:00 清理过期数据(data_lifecycle)
    3. 每 5 分钟采集 RU 使用指标(委托 prometheus_exporter.collect_r40_metrics)
    4. 每小时执行临时封禁自动解封(P1-11: 委托 content_reports.cleanup_expired_bans)
    5. R41 P0-5: 每 60 秒清理过期命令执行租约(委托 command_bus.cleanup_stale_leases)

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


async def cleanup_stale_command_leases_job() -> None:
    """R41 P0-5: 清理过期的命令执行租约(每 60 秒)。

    委托 services.command_bus.cleanup_stale_leases:
    - 将 status='executing' 且 lease_until < now 的记录回退到 'pending'
    - 防止 worker 崩溃后任务永久卡在 executing 状态
    - 回退到 pending 后可被其他 worker 重新认领
    """
    try:
        from services.command_bus import cleanup_stale_leases
        count = await cleanup_stale_leases()
        if count > 0:
            logger.info(f"[R41] 清理 {count} 个过期命令执行租约")
    except Exception as e:
        logger.warning(f"[R41] 清理命令执行租约异常: {e}")


async def _run_lease_cleanup_loop() -> None:
    """R41 P0-5: 命令租约清理子循环(每 60 秒执行一次)。

    独立于主 5 分钟循环,确保过期租约在 60 秒内被回收
    (主循环周期太长会导致僵死任务阻塞重试)。
    """
    while True:
        try:
            await cleanup_stale_command_leases_job()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[R41] 命令租约清理循环异常: {e}")
        await asyncio.sleep(60)


async def approval_executor_drain_job() -> None:
    """R41 P0-4: 消费 command_outbox 表(pending → executing → executed/failed)。

    委托 ``services.approval_executor.drain_once()`` 处理一批 pending 条目:
    - 拉取 status='pending' 且 next_retry_at 已到期的条目
    - CAS claim pending → executing
    - 调用 CommandBus.execute_command_outbox_entry() 执行 handler
    - 成功:status='executed';失败:retry_count + 1,达到 max_retries 则 status='failed'

    独立事务,不与 approve() 事务嵌套(消除 SQLite BEGIN 嵌套风险)。
    """
    try:
        from services.approval_executor import drain_once
        stats = await drain_once()
        if stats.get("total", 0) > 0:
            logger.info(f"[R41] approval_executor drain: {stats}")
    except Exception as e:
        logger.warning(f"[R41] approval_executor drain 异常: {e}")


async def run_approval_executor_loop() -> None:
    """R41 P0-4: ApprovalExecutor 独立主循环(每 30 秒消费一次 command_outbox)。

    与主调度器 ``run_scheduler`` 解耦,确保 30 秒间隔精确执行
    (主调度器周期为 5 分钟,与 30 秒目标不匹配)。

    收到 CancelledError 时优雅退出,异常不传播。
    """
    logger.info("[R41] ApprovalExecutor 主循环已启动(30s 间隔)")
    while True:
        try:
            await approval_executor_drain_job()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("[R41] ApprovalExecutor 主循环已停止")
            break
        except Exception as e:
            # 单轮异常不退出循环,继续下一轮
            logger.warning(f"[R41] ApprovalExecutor 主循环异常: {e}")
            await asyncio.sleep(30)


async def run_scheduler() -> None:
    """R40 定时任务调度器主循环。

    调度策略:
        - 每个周期(5 分钟)执行:清理配额预留 + 采集指标
        - 每小时执行:临时封禁自动解封(P1-11,周期计数器 mod 12 == 0)
        - 每天 3:00-3:05 执行:清理过期数据
        - R41 P0-4: 启动 ApprovalExecutor 独立协程(每 30 秒消费 command_outbox)
        - 收到 CancelledError 时优雅退出(同时取消 ApprovalExecutor 协程)
    """
    logger.info("[R40] 定时任务调度器已启动")
    # R41 P0-4: 启动 ApprovalExecutor 独立循环(30s 间隔)
    approval_exec_task = asyncio.create_task(
        run_approval_executor_loop(),
        name="approval-executor-loop",
    )
    # R41 P0-5: 启动命令租约清理子循环(60s 间隔)
    lease_cleanup_task = asyncio.create_task(
        _run_lease_cleanup_loop(),
        name="lease-cleanup-loop",
    )
    # 周期计数器:每 12 个周期(12 * 5 分钟 = 1 小时)执行一次临时封禁解封
    _cycle_count = 0
    try:
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
    finally:
        # 主调度器退出时取消所有子协程
        approval_exec_task.cancel()
        lease_cleanup_task.cancel()
        for _task in (approval_exec_task, lease_cleanup_task):
            try:
                await _task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    asyncio.run(run_scheduler())
