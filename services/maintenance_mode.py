"""R40 §9.3: 维护模式 — 停止新上传 + 排空队列 + 备份 + 迁移 + 验证 + 恢复。

职责:
    一键式维护模式,按顺序执行:
    1. enable() — 写 maintenance_state(enabled=1) + 通知所有 Bot 停止新请求
    2. drain_queues() — 等待 dirty_outbox / local_job_queue 清空
    3. trigger_backup() — 触发立即备份(委托 services/disaster_recovery.py)
    4. run_migration() — 可选,执行迁移
    5. verify() — 验证系统就绪
    6. disable() — 关闭维护模式

设计原则:
    - 纯函数式 + async
    - 通过 database.cache_store.get_cache_store() 获取单例
    - 维护状态存 maintenance_state 表(id=1 单行)
    - audit_log 内联实现(services/audit_log.py 不存在)
    - 写操作后调用 store.add_dirty_outbox(table_name, pk) 确保跨机同步
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import time as _time
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store


MAINTENANCE_KEY = "maintenance_mode"
MAINTENANCE_STATE_ID = 1  # maintenance_state 表单行 id


# ─── 内联 audit_log 实现(services/audit_log.py 不存在) ─────────

async def _write_audit_log(actor_id: int, action: str,
                           target_type: str = "", target_id: str = "",
                           details: str = "", actor_type: str = "admin") -> int:
    """内联审计日志写入。

    Args:
        actor_id: 操作者 ID(0 表示系统)
        action: 动作名(如 enable_maintenance / disable_maintenance)
        target_type: 目标类型(如 maintenance_state)
        target_id: 目标 ID
        details: 详情(自由文本)
        actor_type: 操作者类型(admin/system/user)

    Returns:
        新插入行 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    try:
        cursor = await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
            (actor_id, actor_type, action, target_type, target_id, details,
             _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        # 写 dirty_outbox 同步到 CRDB
        if cursor and cursor.lastrowid:
            await store.add_dirty_outbox("audit_log", str(cursor.lastrowid))
            return int(cursor.lastrowid)
        return 0
    except Exception as e:
        logger.error(f"[Maintenance] _write_audit_log 失败: {e}")
        return 0


# ─── 维护模式核心 API ──────────────────────────────────────────

async def enable(reason: str, started_by: int = 0) -> bool:
    """开启维护模式。

    步骤:
        1. 写 maintenance_state(enabled=1, reason, started_by, started_at)
        2. 写 audit_log
        3. 通知所有 Bot 停止接受新请求(通过 maintenance_state 状态,
           Bot 在每次请求前调用 is_enabled() 检查)

    Args:
        reason: 维护原因(记入 audit_log.details)
        started_by: 操作者 admin_id

    Returns:
        True 开启成功, False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False

    now_iso = _dt.datetime.now().isoformat()
    try:
        # UPSERT maintenance_state(id=1)
        await store._db.execute(
            """INSERT INTO maintenance_state (id, enabled, reason, started_by, started_at, ended_at)
               VALUES (?, 1, ?, ?, ?, NULL)
               ON CONFLICT(id) DO UPDATE SET
                   enabled = 1, reason = excluded.reason,
                   started_by = excluded.started_by,
                   started_at = excluded.started_at,
                   ended_at = NULL""",
            (MAINTENANCE_STATE_ID, reason, started_by, now_iso),
        )
        await store._db.commit()

        # 写 dirty_outbox 同步到 CRDB
        await store.add_dirty_outbox("maintenance_state", str(MAINTENANCE_STATE_ID))

        # 写 audit_log
        await _write_audit_log(
            actor_id=started_by,
            action="enable_maintenance",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=reason,
        )

        logger.warning(
            f"[Maintenance] 维护模式已开启: reason={reason} started_by={started_by}"
        )
        return True
    except Exception as e:
        logger.error(f"[Maintenance] enable 失败: {e}")
        return False


async def disable(ended_by: int = 0) -> bool:
    """关闭维护模式。

    Args:
        ended_by: 操作者 admin_id

    Returns:
        True 关闭成功, False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False

    now_iso = _dt.datetime.now().isoformat()
    try:
        await store._db.execute(
            """UPDATE maintenance_state
               SET enabled = 0, ended_at = ?
               WHERE id = ?""",
            (now_iso, MAINTENANCE_STATE_ID),
        )
        await store._db.commit()

        # 写 dirty_outbox 同步到 CRDB
        await store.add_dirty_outbox("maintenance_state", str(MAINTENANCE_STATE_ID))

        # 写 audit_log
        await _write_audit_log(
            actor_id=ended_by,
            action="disable_maintenance",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=f"维护模式已关闭 at {now_iso}",
        )

        logger.info(
            f"[Maintenance] 维护模式已关闭: ended_by={ended_by}"
        )
        return True
    except Exception as e:
        logger.error(f"[Maintenance] disable 失败: {e}")
        return False


async def is_enabled() -> bool:
    """检查是否处于维护模式。

    Bot 在每次请求前调用此函数,返回 True 时拒绝新请求。
    """
    store = get_cache_store()
    if not store._db:
        return False
    try:
        rows = await store._db.execute_fetchall(
            "SELECT enabled FROM maintenance_state WHERE id = ?",
            (MAINTENANCE_STATE_ID,),
        )
        if rows and rows[0]:
            return bool(rows[0][0])
        return False
    except Exception:
        return False


async def get_status() -> dict:
    """获取维护模式状态。

    Returns:
        {enabled, reason, started_by, started_at, duration_seconds}
    """
    store = get_cache_store()
    if not store._db:
        return {
            "enabled": False, "reason": "", "started_by": 0,
            "started_at": None, "duration_seconds": 0,
        }
    try:
        rows = await store._db.execute_fetchall(
            """SELECT enabled, reason, started_by, started_at, ended_at
               FROM maintenance_state WHERE id = ?""",
            (MAINTENANCE_STATE_ID,),
        )
        if not rows or not rows[0]:
            return {
                "enabled": False, "reason": "", "started_by": 0,
                "started_at": None, "duration_seconds": 0,
            }
        enabled = bool(rows[0][0])
        reason = rows[0][1] or ""
        started_by = rows[0][2] or 0
        started_at = rows[0][3]
        ended_at = rows[0][4]

        # 计算持续时间
        duration_seconds = 0
        if started_at:
            try:
                started_dt = _dt.datetime.fromisoformat(started_at)
                end_dt = (_dt.datetime.fromisoformat(ended_at)
                          if ended_at else _dt.datetime.now())
                duration_seconds = int((end_dt - started_dt).total_seconds())
            except (ValueError, TypeError):
                pass

        return {
            "enabled": enabled,
            "reason": reason,
            "started_by": started_by,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
        }
    except Exception as e:
        logger.error(f"[Maintenance] get_status 失败: {e}")
        return {
            "enabled": False, "reason": "", "started_by": 0,
            "started_at": None, "duration_seconds": 0,
        }


# ─── 队列排空 ──────────────────────────────────────────────────

async def drain_queues(timeout_seconds: int = 300) -> dict:
    """排空队列(等待 dirty_outbox / local_job_queue 清空)。

    Args:
        timeout_seconds: 最长等待时间(秒)

    Returns:
        {drained: bool, remaining_outbox, remaining_jobs, timeout: bool}
    """
    store = get_cache_store()
    deadline = _time.time() + timeout_seconds

    remaining_outbox = 0
    remaining_jobs = 0

    while _time.time() < deadline:
        try:
            # 检查 dirty_outbox 未处理数
            outbox_rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0"
            )
            remaining_outbox = outbox_rows[0][0] if outbox_rows else 0

            # 检查 local_job_queue pending 数
            job_rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM local_job_queue WHERE status = 'pending'"
            )
            remaining_jobs = job_rows[0][0] if job_rows else 0

            if remaining_outbox == 0 and remaining_jobs == 0:
                logger.info(
                    "[Maintenance] drain_queues 完成: outbox=0, jobs=0"
                )
                return {
                    "drained": True,
                    "remaining_outbox": 0,
                    "remaining_jobs": 0,
                    "timeout": False,
                }

            # 等待 5 秒后重试(异步 sleep,不阻塞事件循环)
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"[Maintenance] drain_queues 检查异常: {e}")
            await asyncio.sleep(5)

    # 超时
    logger.warning(
        f"[Maintenance] drain_queues 超时({timeout_seconds}s): "
        f"outbox={remaining_outbox}, jobs={remaining_jobs}"
    )
    return {
        "drained": False,
        "remaining_outbox": remaining_outbox,
        "remaining_jobs": remaining_jobs,
        "timeout": True,
    }


async def check_readiness() -> dict:
    """检查维护前就绪状态。

    检查系统是否可以安全进入维护模式(无关键任务在执行)。

    Returns:
        {ready, pending_uploads, pending_jobs, unprocessed_outbox,
         active_replication}
        ready=True 表示可以安全进入维护
    """
    store = get_cache_store()
    pending_uploads = 0
    pending_jobs = 0
    unprocessed_outbox = 0
    active_replication = 0

    if store._db:
        try:
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM local_job_queue WHERE status = 'pending'"
            )
            pending_jobs = rows[0][0] if rows else 0
            pending_uploads = pending_jobs  # 简化:pending jobs 视为 pending uploads
        except Exception:
            pass

        try:
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0"
            )
            unprocessed_outbox = rows[0][0] if rows else 0
        except Exception:
            pass

        try:
            rows = await store._db.execute_fetchall(
                """SELECT COUNT(*) FROM replication_tasks
                   WHERE status IN ('PLANNED', 'COPYING', 'COPIED_UNVERIFIED')"""
            )
            active_replication = rows[0][0] if rows else 0
        except Exception:
            pass

    # 就绪条件:无 pending jobs + 无未处理 outbox + 无活跃复制任务
    ready = (pending_jobs == 0 and unprocessed_outbox == 0
             and active_replication == 0)

    return {
        "ready": ready,
        "pending_uploads": pending_uploads,
        "pending_jobs": pending_jobs,
        "unprocessed_outbox": unprocessed_outbox,
        "active_replication": active_replication,
    }


# ─── 完整维护工作流 ────────────────────────────────────────────

async def execute_maintenance_workflow(reason: str,
                                      started_by: int = 0) -> dict:
    """执行完整维护工作流。

    步骤:
        1. enable(reason) — 开启维护模式
        2. drain_queues() — 排空队列
        3. trigger_backup() — 触发立即备份
        4. run_migration() (可选,占位) — 执行迁移
        5. verify() — 验证系统就绪
        6. disable() — 关闭维护模式

    Args:
        reason: 维护原因
        started_by: 操作者 admin_id

    Returns:
        {steps: [{name, success, duration_seconds, error}],
         success: bool, duration_seconds}
    """
    workflow_start = _time.time()
    steps: list[dict] = []
    overall_success = True

    # 步骤 1: enable
    step_start = _time.time()
    try:
        ok = await enable(reason, started_by)
        steps.append({
            "name": "enable", "success": ok,
            "duration_seconds": _time.time() - step_start,
            "error": "" if ok else "enable() 返回 False",
        })
        if not ok:
            overall_success = False
    except Exception as e:
        overall_success = False
        steps.append({
            "name": "enable", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 2: drain_queues
    step_start = _time.time()
    try:
        drain_result = await drain_queues(timeout_seconds=300)
        drained = drain_result.get("drained", False)
        steps.append({
            "name": "drain_queues", "success": drained,
            "duration_seconds": _time.time() - step_start,
            "error": ("超时未排空" if drain_result.get("timeout")
                      else ""),
        })
        if not drained:
            overall_success = False
    except Exception as e:
        overall_success = False
        steps.append({
            "name": "drain_queues", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 3: trigger_backup
    step_start = _time.time()
    try:
        # 委托给 disaster_recovery 模块(避免循环依赖,延迟导入)
        from services import disaster_recovery
        backup_id = await disaster_recovery.trigger_backup()
        ok = bool(backup_id)
        steps.append({
            "name": "trigger_backup", "success": ok,
            "duration_seconds": _time.time() - step_start,
            "error": "" if ok else "trigger_backup() 返回空",
            "backup_id": backup_id,
        })
        if not ok:
            overall_success = False
    except Exception as e:
        overall_success = False
        steps.append({
            "name": "trigger_backup", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 4: run_migration(占位,可由运维后续扩展)
    step_start = _time.time()
    try:
        # 占位:迁移由 migration_runner 单独执行,此处仅记录跳过
        steps.append({
            "name": "run_migration", "success": True,
            "duration_seconds": _time.time() - step_start,
            "error": "skipped (运维单独执行)",
        })
    except Exception as e:
        steps.append({
            "name": "run_migration", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 5: verify
    step_start = _time.time()
    try:
        readiness = await check_readiness()
        ok = readiness.get("ready", False)
        steps.append({
            "name": "verify", "success": ok,
            "duration_seconds": _time.time() - step_start,
            "error": "" if ok else f"就绪检查未通过: {readiness}",
            "readiness": readiness,
        })
    except Exception as e:
        steps.append({
            "name": "verify", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 6: disable(无论前面步骤是否成功,都尝试关闭维护模式)
    step_start = _time.time()
    try:
        ok = await disable(ended_by=started_by)
        steps.append({
            "name": "disable", "success": ok,
            "duration_seconds": _time.time() - step_start,
            "error": "" if ok else "disable() 返回 False",
        })
    except Exception as e:
        steps.append({
            "name": "disable", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    total_duration = _time.time() - workflow_start
    return {
        "steps": steps,
        "success": overall_success,
        "duration_seconds": total_duration,
    }
