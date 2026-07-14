"""R40 + R41 + R42 + R45 + R47 + R48: 定时任务调度器 — 清理过期数据 + 配额预留 + 指标采集 + 临时封禁自动解封 + 命令租约清理 + 备份孤儿 GC + decode_logs 7天保留清理 + MFA 记录 retention + callback_nonces 过期清理。

职责:
    1. 每小时清理过期配额预留(超过 1 小时未结算的自动退款)
    2. 每天 3:00 清理过期数据(data_lifecycle)
    3. 每 5 分钟采集 RU 使用指标(委托 prometheus_exporter.collect_r40_metrics)
    4. 每小时执行临时封禁自动解封(P1-11: 委托 content_reports.cleanup_expired_bans)
    5. R41 P0-5: 每 60 秒清理过期命令执行租约(委托 command_bus.cleanup_stale_leases)
    6. R42 P1-2: 每小时执行备份孤儿对象 GC(委托 backup_gc.run_backup_gc_job)
    7. R45: 每天 3:00 清理 decode_logs 过期记录(7 天保留,委托 decode_logs_cleanup)
       + 启动 run_daily_cleanup_loop 后台兜底任务(create_safe_task)
    8. R47 P1-b: 每天 3:00 清理 MFA 过期记录(24h 保留,
       委托 admin.mfa.cleanup_expired_mfa_records)
    9. R48 P1-b: 每天 3:00 清理 callback_nonces 过期记录
       (过期未消费 + 已消费超 24h,委托 cache_store.callback_nonce_cleanup)

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


async def backup_gc_job() -> None:
    """R42 P1-2: 执行备份孤儿对象 GC(每小时)。

    委托 services.backup_gc.run_backup_gc_job:
    - 清理 R2 中 payload+manifest 已上传但 COMPLETE marker 缺失的孤儿对象
    - 仅清理超时孤儿(默认 1 小时未完成 COMPLETE 的备份)
    - 写 audit_log 记录清理结果
    """
    try:
        from services.backup_gc import run_backup_gc_job
        await run_backup_gc_job()
    except Exception as e:
        logger.warning(f"[R42] 备份孤儿 GC 异常: {e}")


async def retention_cleanup_job() -> None:
    """R42 P1-5: 每天执行 tombstone 物理清理(已备份 + 超 retention_days)。

    委托 services.retention_worker.retention_cleanup_job:
    - 扫描所有 is_tombstone=1 且 deleted_at < now - 30 天的记录
    - 已备份的 tombstone 物理删除 + 写 audit_log
    - 未备份的 tombstone 拒绝删除(等待下次备份后再清理)
    """
    try:
        from services.retention_worker import retention_cleanup_job as _do_retention
        await _do_retention()
    except Exception as e:
        logger.warning(f"[R42] tombstone retention 清理异常: {e}")


async def cleanup_expired_decode_logs_job() -> None:
    """R45: 每天清理 decode_logs 过期记录(7 天保留,凌晨低峰期主路径)。

    委托 services.decode_logs_cleanup.cleanup_expired_decode_logs:
    - 批量删除 request_time < now - 7 days 的记录
    - 每批 500 行,避免一次性大删除造成 RU 峰值
    - 本地 SQLite 清理(零 CRDB RU)
    - 幂等:与 run_daily_cleanup_loop 后台兜底任务重复执行无副作用
    """
    try:
        from services.decode_logs_cleanup import cleanup_expired_decode_logs
        from database.cache_store import get_cache_store
        store = get_cache_store()
        result = await cleanup_expired_decode_logs(store, retention_days=7)
        if result["deleted_count"] > 0:
            logger.info(
                f"[R45] decode_logs 清理: 删除 {result['deleted_count']} 条, "
                f"保留 {result['retention_days']} 天, 截止 {result['cutoff_time']}"
            )
    except Exception as e:
        logger.warning(f"[R45] decode_logs 清理异常: {e}")


async def cleanup_expired_mfa_records_job() -> None:
    """R47 P1-b: 每天清理 MFA 过期记录(24h 保留)。

    委托 admin.mfa.cleanup_expired_mfa_records:
    - 删除 mfa_used_totp 中 used_at < (now - 24h) 的记录
    - 删除 mfa_failures 中 failed_at_ms < (now_ms - 24h) 的记录
    - 本地 SQLite 清理(零 CRDB RU)
    - 幂等:重复执行无副作用
    """
    try:
        from admin.mfa import cleanup_expired_mfa_records
        result = await cleanup_expired_mfa_records(retention_hours=24)
        if result["deleted_used_totp"] > 0 or result["deleted_failures"] > 0:
            logger.info(
                f"[R47] MFA 记录清理: used_totp={result['deleted_used_totp']}, "
                f"failures={result['deleted_failures']}"
            )
    except Exception as e:
        logger.warning(f"[R47] MFA 记录清理异常: {e}")


async def cleanup_expired_callback_nonces_job() -> None:
    """R48 P1-b: 每天清理 callback_nonces 过期记录。

    清理策略:
    - 删除 expires_at < now 的记录(已过期但未消费的 nonce)
    - 删除 consumed_at < (now - 24h) 的记录(已消费超过 24h 的 nonce)

    防止 callback_nonces 表无限增长,定期清理过期和已消费的 nonce。
    本地 SQLite 清理(零 CRDB RU),幂等:重复执行无副作用。
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        now = _dt.datetime.now().isoformat()
        cutoff_24h = (_dt.datetime.now() - _dt.timedelta(hours=24)).isoformat()
        result = await store.callback_nonce_cleanup(
            expired_before=now,
            consumed_before=cutoff_24h,
        )
        if result["deleted_expired"] > 0 or result["deleted_consumed"] > 0:
            logger.info(
                f"[R48] callback_nonces 清理: "
                f"expired={result['deleted_expired']}, "
                f"consumed={result['deleted_consumed']}"
            )
    except Exception as e:
        logger.warning(f"[R48] callback_nonces 清理异常: {e}")


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
        - R42 P1-5: 每天 4:00-4:05 执行 tombstone retention 物理清理
        - R42 P1-2: 每小时执行一次备份孤儿 GC(与临时封禁解封同期)
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
    # R45: 启动 decode_logs 每日清理后台兜底任务(24h 间隔,create_safe_task 防异常静默)
    # 主路径由主循环每天 3:00 调用 cleanup_expired_decode_logs_job,此为兜底
    decode_logs_cleanup_task = None
    try:
        from database.cache_store import get_cache_store as _get_store
        from services.decode_logs_cleanup import run_daily_cleanup_loop
        from utils.task_utils import create_safe_task
        _decode_logs_store = _get_store()
        decode_logs_cleanup_task = create_safe_task(
            run_daily_cleanup_loop(_decode_logs_store, retention_days=7),
            name="decode-logs-cleanup-loop",
        )
    except Exception as _e:
        logger.warning(f"[R45] decode_logs 后台清理任务启动失败(主循环兜底仍可用): {_e}")
    # 周期计数器:每 12 个周期(12 * 5 分钟 = 1 小时)执行一次临时封禁解封
    _cycle_count = 0
    # R42 P1-5: tombstone retention 清理执行日期标记(避免同一天重复执行)
    _retention_executed_date: str = ""
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
                    # R42 P1-2: 同期执行备份孤儿 GC(每小时一次)
                    await backup_gc_job()
                # 每天 3:00 清理过期数据(分钟 < 5 避免重复执行)
                if now.hour == 3 and now.minute < 5:
                    await cleanup_expired_data_job()
                    # R45: 同期清理 decode_logs 过期记录(7 天保留,凌晨低峰期)
                    await cleanup_expired_decode_logs_job()
                    # R47 P1-b: 同期清理 MFA 过期记录(24h 保留)
                    await cleanup_expired_mfa_records_job()
                    # R48 P1-b: 同期清理 callback_nonces 过期记录
                    await cleanup_expired_callback_nonces_job()
                # R42 P1-5: 每天 4:00-4:05 执行 tombstone retention 物理清理
                # (在备份通常完成后,避免与 3:00 数据清理争用资源)
                if (
                    now.hour == 4
                    and now.minute < 5
                    and now.strftime("%Y-%m-%d") != _retention_executed_date
                ):
                    await retention_cleanup_job()
                    _retention_executed_date = now.strftime("%Y-%m-%d")
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
        if decode_logs_cleanup_task is not None:
            decode_logs_cleanup_task.cancel()
        for _task in (approval_exec_task, lease_cleanup_task, decode_logs_cleanup_task):
            if _task is None:
                continue
            try:
                await _task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    asyncio.run(run_scheduler())
