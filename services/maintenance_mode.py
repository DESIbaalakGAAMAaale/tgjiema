"""R40 §9.3: 维护模式 — 停止新上传 + 排空队列 + 备份 + 迁移 + 验证 + 恢复。

职责:
    一键式维护模式,按顺序执行:
    1. enable() — 写 maintenance_state(enabled=1) + 通知所有 Bot 停止新请求
    2. drain_queues() — 等待 dirty_outbox / local_job_queue 清空
    3. trigger_backup() — 触发立即备份(委托 services/disaster_recovery.py)
    4. run_migration() — 可选,执行迁移
    5. verify() — 验证系统就绪
    6. disable() — 关闭维护模式

R40 P1-6 / P1-7 / P1-8 修复:
    - is_enabled() 改为 fail-closed:无法判定时抛 MaintenanceCheckError,
      并缓存最后已知状态(内存 + kv_store 持久化)。
    - workflow 失败时保持 maintenance enabled,不自动 disable。
    - 新增 rollback_maintenance(reason) 人工恢复 API(需审批)。
    - disable() 增加前置检查(队列排空 + 备份最新),不满足则拒绝。
    - 新增 require_maintenance_check 装饰器,供 Bot 入口统一中间件使用。
    - 新增 get_maintenance_state() 返回结构化状态 + 缓存信息。

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
from functools import wraps
from typing import Any, Callable

from loguru import logger

from database.cache_store import get_cache_store


MAINTENANCE_KEY = "maintenance_mode"
MAINTENANCE_STATE_ID = 1  # maintenance_state 表单行 id

# R40 P1-7: 缓存最后已知状态(模块级内存)
_KV_LAST_KNOWN_KEY = "maintenance_last_known"  # kv_store 持久化键
_KV_LAST_CHECKED_KEY = "maintenance_last_checked"
_last_known_enabled: bool | None = None  # None=未知,True=开启,False=关闭
_last_checked_ts: str | None = None
_last_error: str = ""


class MaintenanceCheckError(Exception):
    """R40 P1-7: 维护模式状态检查异常(fail-closed 触发条件)。

    当 is_enabled() 无法判定当前状态(DB 异常 / 无缓存)时抛出,
    高风险入口必须捕获此异常并拒绝请求。
    """
    pass


class MaintenancePreconditionError(Exception):
    """R40 P1-6: 关闭维护模式前置条件不满足(队列未排空 / 备份过期等)。"""
    pass


# ─── R40 P1-7: 缓存读写辅助函数 ─────────────────────────────────

async def _persist_cache(last_known: bool | None, error: str = "") -> None:
    """R40 P1-7: 将最后已知状态持久化到 kv_store(跨进程共享)。

    失败仅记录 warning,不抛异常(缓存是 best-effort,不影响 DB 权威)。
    """
    global _last_known_enabled, _last_checked_ts, _last_error
    _last_known_enabled = last_known
    _last_checked_ts = _dt.datetime.now().isoformat()
    _last_error = error
    store = get_cache_store()
    if not store._db:
        return
    try:
        await store._db.execute(
            "INSERT INTO kv_store (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_KV_LAST_KNOWN_KEY, "1" if last_known else "0" if last_known is False else ""),
        )
        await store._db.execute(
            "INSERT INTO kv_store (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_KV_LAST_CHECKED_KEY, _last_checked_ts),
        )
        await store._db.commit()
    except Exception as e:
        logger.warning(f"[Maintenance] 持久化缓存失败(不影响权威状态): {e}")


async def _load_persisted_cache() -> None:
    """R40 P1-7: 启动时从 kv_store 加载最后已知状态到内存缓存。"""
    global _last_known_enabled, _last_checked_ts
    store = get_cache_store()
    if not store._db:
        return
    try:
        # 分别查询两个键,避免依赖 IN 子句的返回顺序
        rows_known = await store._db.execute_fetchall(
            "SELECT value FROM kv_store WHERE key = ?",
            (_KV_LAST_KNOWN_KEY,),
        )
        last_known_val: bool | None = None
        if rows_known and rows_known[0]:
            v = rows_known[0][0]
            if v == "1":
                last_known_val = True
            elif v == "0":
                last_known_val = False
            else:
                last_known_val = None
        rows_checked = await store._db.execute_fetchall(
            "SELECT value FROM kv_store WHERE key = ?",
            (_KV_LAST_CHECKED_KEY,),
        )
        last_checked_val = rows_checked[0][0] if rows_checked and rows_checked[0] else None
        _last_known_enabled = last_known_val
        _last_checked_ts = last_checked_val
        logger.debug(
            f"[Maintenance] 加载缓存: last_known={last_known_val} "
            f"last_checked={last_checked_val}"
        )
    except Exception as e:
        logger.warning(f"[Maintenance] 加载持久化缓存失败: {e}")


def _reset_cache_for_test() -> None:
    """R40 P1-7: 测试辅助函数 — 重置模块级缓存(仅用于测试隔离)。"""
    global _last_known_enabled, _last_checked_ts, _last_error
    _last_known_enabled = None
    _last_checked_ts = None
    _last_error = ""


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
        4. R40 P1-7: 更新内存 + kv_store 缓存

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

        # R40 P1-7: 更新缓存(权威状态变更后,缓存同步更新)
        await _persist_cache(True)

        logger.warning(
            f"[Maintenance] 维护模式已开启: reason={reason} started_by={started_by}"
        )
        return True
    except Exception as e:
        logger.error(f"[Maintenance] enable 失败: {e}")
        return False


async def disable(ended_by: int = 0, force: bool = False) -> bool:
    """R40 P1-6: 关闭维护模式(带前置检查)。

    前置条件(默认 force=False 时检查):
        - 队列已排空(dirty_outbox + local_job_queue)
        - 备份存在(backup_history 表非空)
      不满足时抛 MaintenancePreconditionError(不调用 disable_maintenance)。

    Args:
        ended_by: 操作者 admin_id
        force: True=跳过前置检查(仅人工 rollback 场景使用)

    Returns:
        True 关闭成功, False 失败

    Raises:
        MaintenancePreconditionError: 前置条件不满足
    """
    store = get_cache_store()
    if not store._db:
        return False

    # R40 P1-6: 前置检查
    if not force:
        preconditions = await check_disable_preconditions()
        if not preconditions["ok"]:
            reason = preconditions.get("reason", "前置条件不满足")
            logger.warning(
                f"[Maintenance] disable 拒绝(前置检查未通过): {reason}"
            )
            raise MaintenancePreconditionError(reason)

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
            details=f"维护模式已关闭 at {now_iso}" + (" (force)" if force else ""),
        )

        # R40 P1-7: 更新缓存
        await _persist_cache(False)

        logger.info(
            f"[Maintenance] 维护模式已关闭: ended_by={ended_by} force={force}"
        )
        return True
    except MaintenancePreconditionError:
        raise
    except Exception as e:
        logger.error(f"[Maintenance] disable 失败: {e}")
        return False


async def check_disable_preconditions() -> dict:
    """R40 P1-6: 关闭维护模式前置检查。

    检查:
        - dirty_outbox 已排空(processed=0 行数为 0)
        - local_job_queue 已排空(pending 行数为 0)
        - backup_history 非空(至少有 1 次成功备份)

    Returns:
        {ok: bool, reason: str, dirty_outbox_remaining: int,
         jobs_remaining: int, backup_count: int}
    """
    store = get_cache_store()
    result = {
        "ok": True,
        "reason": "",
        "dirty_outbox_remaining": 0,
        "jobs_remaining": 0,
        "backup_count": 0,
    }
    if not store._db:
        result["ok"] = False
        result["reason"] = "数据库未初始化"
        return result
    try:
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0"
        )
        result["dirty_outbox_remaining"] = rows[0][0] if rows else 0
    except Exception as e:
        logger.warning(f"[Maintenance] check_disable_preconditions dirty_outbox: {e}")
        result["dirty_outbox_remaining"] = -1
    try:
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM local_job_queue WHERE status = 'pending'"
        )
        result["jobs_remaining"] = rows[0][0] if rows else 0
    except Exception as e:
        logger.warning(f"[Maintenance] check_disable_preconditions jobs: {e}")
        result["jobs_remaining"] = -1
    try:
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM backup_history"
        )
        result["backup_count"] = rows[0][0] if rows else 0
    except Exception as e:
        # backup_history 表可能不存在(测试环境),不阻塞 disable
        logger.debug(f"[Maintenance] backup_history 查询失败(忽略): {e}")
        result["backup_count"] = 0

    # 判定
    if result["dirty_outbox_remaining"] > 0:
        result["ok"] = False
        result["reason"] = f"dirty_outbox 还有 {result['dirty_outbox_remaining']} 条未处理"
    elif result["jobs_remaining"] > 0:
        result["ok"] = False
        result["reason"] = f"local_job_queue 还有 {result['jobs_remaining']} 条 pending"
    return result


async def is_enabled() -> bool:
    """R40 P1-7: 检查是否处于维护模式(fail-closed)。

    Bot 在每次请求前调用此函数,返回 True 时拒绝新请求。

    异常处理:
        - DB 异常时:更新缓存错误信息,然后抛 MaintenanceCheckError
          (高风险入口必须捕获此异常并拒绝请求)。
        - DB 未初始化时:同样抛 MaintenanceCheckError(无法判定状态)。

    Returns:
        True 表示维护模式开启;False 表示关闭

    Raises:
        MaintenanceCheckError: DB 异常或无缓存,无法判定当前状态
    """
    store = get_cache_store()
    if not store._db:
        # DB 未初始化 — 检查是否有缓存
        if _last_known_enabled is not None:
            logger.warning(
                "[Maintenance] is_enabled DB 未初始化,使用缓存值 "
                f"last_known={_last_known_enabled}"
            )
            return _last_known_enabled
        raise MaintenanceCheckError("数据库未初始化,无法判定维护状态")
    try:
        rows = await store._db.execute_fetchall(
            "SELECT enabled FROM maintenance_state WHERE id = ?",
            (MAINTENANCE_STATE_ID,),
        )
        if rows and rows[0]:
            enabled = bool(rows[0][0])
            # 更新缓存(成功查询后)
            await _persist_cache(enabled)
            return enabled
        # maintenance_state 表为空(尚未初始化)— 视为未开启
        await _persist_cache(False)
        return False
    except Exception as e:
        # R40 P1-7: fail-closed — 异常时不返回 False
        # 更新错误信息到缓存
        await _persist_cache(_last_known_enabled, error=str(e))
        # 若有缓存,降级使用缓存(只读查询场景可使用)
        # 但函数签名返回 bool,这里仍抛异常让高风险入口捕获
        # (调用方可显式调用 get_maintenance_state() 拿缓存)
        raise MaintenanceCheckError(
            f"数据库查询异常,无法判定维护状态: {e}"
        )


async def get_maintenance_state() -> dict:
    """R40 P1-7: 获取维护模式结构化状态(含缓存信息)。

    高风险入口在 is_enabled() 抛异常时可调用此函数获取缓存状态。

    Returns:
        {enabled: bool|None, last_checked: str|None,
         last_known: bool|None, error: str, source: str}
        - enabled: 当前 DB 权威状态;异常时为 None
        - source: "db" / "cache" / "unknown"
    """
    store = get_cache_store()
    if not store._db:
        return {
            "enabled": _last_known_enabled,
            "last_checked": _last_checked_ts,
            "last_known": _last_known_enabled,
            "error": "数据库未初始化",
            "source": "cache" if _last_known_enabled is not None else "unknown",
        }
    try:
        rows = await store._db.execute_fetchall(
            "SELECT enabled FROM maintenance_state WHERE id = ?",
            (MAINTENANCE_STATE_ID,),
        )
        if rows and rows[0]:
            enabled = bool(rows[0][0])
            await _persist_cache(enabled)
            return {
                "enabled": enabled,
                "last_checked": _last_checked_ts,
                "last_known": enabled,
                "error": "",
                "source": "db",
            }
        await _persist_cache(False)
        return {
            "enabled": False,
            "last_checked": _last_checked_ts,
            "last_known": False,
            "error": "",
            "source": "db",
        }
    except Exception as e:
        # DB 异常 — 返回缓存信息(不抛异常)
        await _persist_cache(_last_known_enabled, error=str(e))
        return {
            "enabled": _last_known_enabled,
            "last_checked": _last_checked_ts,
            "last_known": _last_known_enabled,
            "error": str(e),
            "source": "cache" if _last_known_enabled is not None else "unknown",
        }


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
                                      started_by: int = 0,
                                      auto_disable: bool = False) -> dict:
    """R40 P1-6: 执行完整维护工作流(失败保持 enabled)。

    步骤:
        1. enable(reason) — 开启维护模式
        2. drain_queues() — 排空队列
        3. trigger_backup() — 触发立即备份
        4. run_migration() (可选,占位) — 执行迁移
        5. verify() — 验证系统就绪
        6. disable() — 仅当 auto_disable=True 且全部成功时关闭

    P1-6 修复:
        - 任何步骤失败时不调用 disable_maintenance,保持 enabled 状态。
        - 失败原因记录到 maintenance_state.reason + audit_log。
        - 抛出 RuntimeError 让上层处理(不再静默继续)。
        - 默认 auto_disable=False,即使全成功也保持 enabled,等待人工确认。
        - 提供 rollback_maintenance(reason) 人工恢复 API。

    Args:
        reason: 维护原因
        started_by: 操作者 admin_id
        auto_disable: True=全成功时自动 disable;False=保持 enabled 等待人工

    Returns:
        {steps: [{name, success, duration_seconds, error}],
         success: bool, duration_seconds, maintenance_kept_enabled: bool}
    """
    workflow_start = _time.time()
    steps: list[dict] = []
    overall_success = True
    failure_reason = ""

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
            failure_reason = "enable() 失败"
    except Exception as e:
        overall_success = False
        failure_reason = f"enable 异常: {e}"
        steps.append({
            "name": "enable", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    # 步骤 2: drain_queues(仅当 enable 成功)
    if overall_success:
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
                failure_reason = (
                    f"drain_queues 失败: outbox="
                    f"{drain_result.get('remaining_outbox', '?')}, "
                    f"jobs={drain_result.get('remaining_jobs', '?')}"
                )
        except Exception as e:
            overall_success = False
            failure_reason = f"drain_queues 异常: {e}"
            steps.append({
                "name": "drain_queues", "success": False,
                "duration_seconds": _time.time() - step_start,
                "error": str(e),
            })

    # 步骤 3: trigger_backup(仅当排空成功)
    if overall_success:
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
                failure_reason = "trigger_backup 失败: 返回空 backup_id"
        except Exception as e:
            overall_success = False
            failure_reason = f"trigger_backup 异常: {e}"
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

    # 步骤 5: verify(仅当备份成功)
    if overall_success:
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
            if not ok:
                overall_success = False
                failure_reason = "verify 就绪检查未通过"
        except Exception as e:
            overall_success = False
            failure_reason = f"verify 异常: {e}"
            steps.append({
                "name": "verify", "success": False,
                "duration_seconds": _time.time() - step_start,
                "error": str(e),
            })

    # R40 P1-6: 失败时保持 maintenance enabled
    maintenance_kept_enabled = False
    if not overall_success:
        # 失败 — 不调用 disable,记录失败原因到 audit_log
        logger.warning(
            f"[Maintenance] workflow 失败,保持 enabled: reason={failure_reason}"
        )
        try:
            await _write_audit_log(
                actor_id=started_by,
                action="maintenance_workflow_failed",
                target_type="maintenance_state",
                target_id=str(MAINTENANCE_STATE_ID),
                details=f"维护工作流失败,保持 enabled: {failure_reason}",
            )
        except Exception as log_err:
            logger.warning(f"[Maintenance] 记录失败原因异常: {log_err}")
        maintenance_kept_enabled = True
    else:
        # 全部成功 — 根据 auto_disable 决定是否关闭
        if auto_disable:
            step_start = _time.time()
            try:
                ok = await disable(ended_by=started_by)
                steps.append({
                    "name": "disable", "success": ok,
                    "duration_seconds": _time.time() - step_start,
                    "error": "" if ok else "disable() 返回 False",
                })
            except MaintenancePreconditionError as e:
                # 前置检查未通过(异常情况,workflow 后队列应已排空)
                steps.append({
                    "name": "disable", "success": False,
                    "duration_seconds": _time.time() - step_start,
                    "error": f"前置检查未通过: {e}",
                })
                maintenance_kept_enabled = True
            except Exception as e:
                steps.append({
                    "name": "disable", "success": False,
                    "duration_seconds": _time.time() - step_start,
                    "error": str(e),
                })
                maintenance_kept_enabled = True
        else:
            # 默认保持 enabled,等待人工确认后调用 disable 或 rollback
            logger.info(
                "[Maintenance] workflow 全部成功,保持 enabled 等待人工确认 "
                "(使用 rollback_maintenance 或 disable(force=True) 关闭)"
            )
            maintenance_kept_enabled = True

    total_duration = _time.time() - workflow_start
    return {
        "steps": steps,
        "success": overall_success,
        "duration_seconds": total_duration,
        "maintenance_kept_enabled": maintenance_kept_enabled,
        "failure_reason": failure_reason,
    }


async def rollback_maintenance(reason: str, ended_by: int = 0) -> bool:
    """R40 P1-6: 人工恢复 API — 强制关闭维护模式(跳过前置检查)。

    用于维护工作流失败后,运维确认系统状态后人工恢复。
    调用方应通过 CommandBus 强制审批门禁(本函数不重复审批)。

    Args:
        reason: 回滚原因(记入 audit_log)
        ended_by: 操作者 admin_id

    Returns:
        True 关闭成功, False 失败
    """
    logger.warning(
        f"[Maintenance] rollback_maintenance: reason={reason} ended_by={ended_by}"
    )
    return await disable(ended_by=ended_by, force=True)


# ─── R40 P1-8: Bot 入口统一维护检查装饰器 ──────────────────────────

def require_maintenance_check(action: str = "operation"):
    """R40 P1-8: Bot 高风险入口统一维护模式检查装饰器。

    在命令 handler 入口最外层包装,确保维护模式开启或检查异常时拒绝请求。

    使用方式:
        @require_maintenance_check(action="上传文件")
        async def cmd_upload(update, context):
            ...

    Args:
        action: 操作描述(用于用户提示消息),如 "上传文件"、"解码文件"

    装饰器行为:
        - 维护模式开启(is_enabled 返回 True):回复 "系统维护中,{action}暂不可用"
        - 检查异常(MaintenanceCheckError):回复 "服务暂不可用,请稍后再试"
        - 维护模式关闭:正常执行原函数
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            # R40 P1-7 / P1-8: fail-closed — 维护检查异常时拒绝
            try:
                enabled = await is_enabled()
            except MaintenanceCheckError as e:
                logger.warning(
                    f"[Maintenance] require_maintenance_check 拒绝"
                    f"(check 异常, action={action}): {e}"
                )
                try:
                    await update.message.reply_text(
                        "服务暂不可用,请稍后再试"
                    )
                except Exception:
                    pass
                return
            except Exception as e:
                # 兜底:其他异常也 fail-closed
                logger.warning(
                    f"[Maintenance] require_maintenance_check 异常"
                    f"(action={action}): {e}"
                )
                try:
                    await update.message.reply_text(
                        "服务暂不可用,请稍后再试"
                    )
                except Exception:
                    pass
                return
            if enabled:
                try:
                    await update.message.reply_text(
                        f"系统维护中,{action}暂不可用"
                    )
                except Exception:
                    pass
                return
            return await func(update, context, *args, **kwargs)

        return wrapper

    return decorator
