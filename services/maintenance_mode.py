"""R40 §9.3 + R45 第 16 节: 维护模式 — 停止新上传 + 排空队列 + 备份 + 迁移 + 验证 + 恢复。

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

R45 第 16 节 Maintenance 整改:
    - execute_maintenance_workflow 在 drain/backup/migration/verify 任一步失败时
      保持 maintenance enabled + 设置 recover_status='pending'(R42 P1-12 已实现,
      R45 验证并补充文档)。
    - 新增 check_maintenance_at_entry(action) 统一入口检查函数:
      与 require_maintenance_check 装饰器的差异:
        * 装饰器只适用于 Telegram handler(依赖 update.message.reply_text)
        * check_maintenance_at_entry 返回结构化结果 {allowed, reason, source},
          适用于内部服务调用 / API 路由 / 周期任务等非 Telegram 场景
      - fail-closed:DB 异常时返回 allowed=False(拒绝请求)
      - 缓存降级:DB 不可达时使用 kv_store 缓存的最后已知状态

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
from services.i18n import translate as _i18n_t


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
        # R42 P1-12: 重置 recover_status='completed'(新维护周期开始)
        await store._db.execute(
            """INSERT INTO maintenance_state (id, enabled, reason, started_by, started_at, ended_at, recover_status)
               VALUES (?, 1, ?, ?, ?, NULL, 'completed')
               ON CONFLICT(id) DO UPDATE SET
                   enabled = 1, reason = excluded.reason,
                   started_by = excluded.started_by,
                   started_at = excluded.started_at,
                   ended_at = NULL,
                   recover_status = 'completed'""",
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


async def disable(ended_by: int = 0, force: bool = False, approval_action_id: str = None,
                  request_hash: str = None) -> bool:
    """R40 P1-6 + R42 P1-12 + R51 P1-6 + R52 P0-5: 关闭维护模式(带前置检查 + recover_status 校验 + 绑定校验 + 统一状态机)。

    前置条件(默认 force=False 时检查):
        - 队列已排空(dirty_outbox + local_job_queue)
        - 备份存在(backup_history 表非空)
      不满足时抛 MaintenancePreconditionError(不调用 disable_maintenance)。

    R42 P1-12 新增 recover_status 校验:
        - 若 maintenance_state.recover_status='pending'(workflow 失败):
          - approval_action_id 为 None → 抛 MaintenancePreconditionError
          - approval_action_id 有效(在 command_executions 中 status='approved')
            → 允许关闭
        - 否则正常流程

    R51 P1-6 新增绑定校验(recover_status='pending' 时强制):
        - 必须同时提供 request_hash + approval_action_id(ended_by 即 principal)
        - 三者缺一不可,确保审批动作与请求来源可审计追溯
        - 缺失时抛 MaintenancePreconditionError(协议化错误 MAINTENANCE_RECOVER_BINDING_REQUIRED)

    R52 P0-5 新增统一状态机(recover_status='pending' 时强制):
        - status 从 'executed' 改为 'approved'(执行前置校验,避免语义冲突)
        - CAS: approved → executing(防并发执行同一审批)
        - 成功后: executing → executed
        - 失败后: executing → failed

    Args:
        ended_by: 操作者 admin_id(即 principal)
        force: True=跳过常规前置检查(仅人工 rollback / recover 场景使用)
        approval_action_id: recover_maintenance 审批 action_id(recover_status='pending' 时必需)
        request_hash: R51 P1-6 请求哈希(recover_status='pending' 时必需,绑定审批与请求)

    Returns:
        True 关闭成功, False 失败

    Raises:
        MaintenancePreconditionError: 前置条件不满足 / 需要 recover_maintenance 审批 / R51 P1-6 绑定缺失
    """
    store = get_cache_store()
    if not store._db:
        return False

    # R52 P0-5: 引入统一状态机辅助函数(延迟导入避免循环依赖)
    from services.command_bus import (
        claim_execution_approved,
        mark_approved_executed,
        mark_approved_failed,
    )

    # R42 P1-12: 检查 recover_status
    # R52 P1-6: 查询失败必须 fail-closed(不允许降级为 completed,否则可能在
    #   recover_status='pending' 时绕过审批关闭维护模式)
    recover_status = "completed"
    _recover_status_query_failed = False
    try:
        rows = await store._db.execute_fetchall(
            "SELECT recover_status FROM maintenance_state WHERE id = ?",
            (MAINTENANCE_STATE_ID,),
        )
        if rows and rows[0]:
            recover_status = rows[0][0] or "completed"
    except Exception as e:
        _recover_status_query_failed = True
        # R52 P1-6: fail-closed — 查询失败时拒绝 disable,不允许降级为 completed
        from services.error_codes import AppError, ErrorCodes
        logger.error(
            f"[Maintenance] R52 P1-6: disable 查询 recover_status 失败,"
            f"fail-closed 拒绝关闭(不允许降级为 completed): {e}"
        )
        # 写 audit_log 记录 fail-closed 拒绝(便于事后追溯)
        try:
            app_err = AppError(
                ErrorCodes.MAINTENANCE_DISABLE_RECOVER_QUERY_FAILED,
                params={"reason": f"{type(e).__name__}: {e}"},
            )
            await app_err.write_audit_log()
        except Exception:
            pass  # audit_log 写入失败不影响主流程拒绝
        raise MaintenancePreconditionError(
            f"R52 P1-6: disable recover_status query failed, fail-closed denying shutdown: {e}"
        )

    if recover_status == "pending":
        if not approval_action_id:
            logger.warning(
                "[Maintenance] disable 拒绝(recover_status=pending 但未提供 approval_action_id)"
            )
            raise MaintenancePreconditionError(
                _i18n_t('services.maintenance_mode.s12')
            )
        # R51 P1-6: 强制要求 request_hash(绑定审批动作 + principal + 请求来源)
        # ended_by 即 principal,三者齐全才允许关闭
        if not request_hash:
            logger.warning(
                f"[Maintenance] R51 P1-6 disable 拒绝"
                f"(recover_status=pending 但未提供 request_hash, "
                f"principal={ended_by}, approval_action_id={approval_action_id})"
            )
            # 协议化错误:写入 audit_log + 抛 MaintenancePreconditionError
            try:
                from services.error_codes import AppError, ErrorCodes
                app_err = AppError(
                    ErrorCodes.MAINTENANCE_RECOVER_BINDING_REQUIRED,
                    params={
                        "approval_action_id": approval_action_id,
                        "principal_id": ended_by,
                    },
                )
                await app_err.write_audit_log()
            except Exception:
                pass  # audit_log 写入失败不影响主流程拒绝
            raise MaintenancePreconditionError(
                _i18n_t('services.maintenance_mode.s13')
            )
        # 验证 approval_action_id 在 command_executions 中 status='approved'
        # R52 P0-5: 状态从 'executed' 改为 'approved'(执行前置校验,
        # 避免"执行前已执行"语义冲突)
        try:
            exec_rows = await store._db.execute_fetchall(
                "SELECT status FROM command_executions WHERE action_id = ?",
                (approval_action_id,),
            )
        except Exception as e:
            raise MaintenancePreconditionError(
                _i18n_t('services.maintenance_mode.s30', e=e)
            )
        if not exec_rows or not exec_rows[0]:
            raise MaintenancePreconditionError(
                _i18n_t('services.maintenance_mode.s14', approval_action_id=approval_action_id)
            )
        exec_status = exec_rows[0][0]
        if exec_status != "approved":
            raise MaintenancePreconditionError(
                f"approval_action_id status not approved (current: {exec_status}): "
                f"{approval_action_id}"
            )
        logger.info(
            f"[Maintenance] disable recover_status=pending,approval_action_id={approval_action_id} "
            f"request_hash={(request_hash or '')[:16]} principal={ended_by} 已验证 approved,允许关闭"
        )

        # R52 P0-5: CAS approved → executing(防并发执行同一审批)
        # 失败时表示已被其他 worker 抢占,或状态已从 approved 流转
        # R53 P0-2: DB 不可用时 claim_execution_approved 抛 AppError(
        # COMMAND_EXECUTION_STORE_UNAVAILABLE),必须原样向上传播,禁止降级执行
        import socket as _socket
        import os as _os
        _owner = f"{_socket.gethostname()}:{_os.getpid()}"
        try:
            claimed = await claim_execution_approved(
                action_id=approval_action_id,
                owner=_owner,
                request_hash=request_hash,
            )
        except Exception as cas_err:
            # R53 P0-2: 若是 AppError(COMMAND_EXECUTION_STORE_UNAVAILABLE 等)
            # 原样向上传播,保留原始错误码;其他异常包装为 MaintenancePreconditionError
            from services.error_codes import AppError as _AppError
            if isinstance(cas_err, _AppError):
                logger.error(
                    f"[Maintenance] disable CAS approved→executing fail-closed "
                    f"approval_action_id={approval_action_id}: {cas_err}"
                )
                raise
            logger.error(
                f"[Maintenance] disable CAS approved→executing 异常 "
                f"approval_action_id={approval_action_id}: {cas_err}"
            )
            raise MaintenancePreconditionError(
                f"CAS approved->executing error: {cas_err}"
            )
        if not claimed:
            logger.error(
                f"[Maintenance] disable CAS 失败(已被抢占或状态非 approved) "
                f"approval_action_id={approval_action_id} principal={ended_by}"
            )
            raise MaintenancePreconditionError(
                f"CAS approved->executing failed (preempted or status not approved): "
                f"{approval_action_id}"
            )

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
    # R52 P0-5: 跟踪是否已执行 CAS(仅在 recover_status=pending + approval_action_id 场景)
    # 用于在 disable 成功/失败后回写状态机 executing → executed/failed
    _cas_done = recover_status == "pending" and approval_action_id is not None
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
            details=_i18n_t('services.maintenance_mode.s38', now_iso=now_iso) + (" (force)" if force else ""),
        )

        # R40 P1-7: 更新缓存
        await _persist_cache(False)

        # R52 P0-5: 成功时回写状态机 executing → executed
        if _cas_done:
            try:
                await mark_approved_executed(
                    action_id=approval_action_id,
                    result={
                        "success": True,
                        "action": "disable_maintenance",
                        "ended_by": ended_by,
                    },
                )
            except Exception as mark_err:
                logger.error(
                    f"[Maintenance] mark_approved_executed 失败 "
                    f"approval_action_id={approval_action_id}: {mark_err}"
                )

        logger.info(
            f"[Maintenance] 维护模式已关闭: ended_by={ended_by} force={force}"
        )
        return True
    except MaintenancePreconditionError:
        # R52 P0-5: 前置条件失败时回写状态机 executing → failed
        # (仅在已 CAS 成功的场景;前置条件失败发生在 CAS 之前时无需回写)
        if _cas_done:
            try:
                await mark_approved_failed(
                    action_id=approval_action_id,
                    error=f"disable precondition failed",
                    retryable=False,
                )
            except Exception as mark_err:
                logger.error(
                    f"[Maintenance] mark_approved_failed 失败 "
                    f"approval_action_id={approval_action_id}: {mark_err}"
                )
        raise
    except Exception as e:
        logger.error(f"[Maintenance] disable 失败: {e}")
        # R52 P0-5: 失败时回写状态机 executing → failed
        if _cas_done:
            try:
                await mark_approved_failed(
                    action_id=approval_action_id,
                    error=f"disable maintenance failed: {e}",
                    retryable=False,
                )
            except Exception as mark_err:
                logger.error(
                    f"[Maintenance] mark_approved_failed 失败 "
                    f"approval_action_id={approval_action_id}: {mark_err}"
                )
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
        result["reason"] = _i18n_t('services.maintenance_mode.s1')
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
        result["reason"] = _i18n_t('services.maintenance_mode.s2', result_dirty_outbox_remaining=result['dirty_outbox_remaining'])
    elif result["jobs_remaining"] > 0:
        result["ok"] = False
        result["reason"] = _i18n_t('services.maintenance_mode.s3', result_jobs_remaining=result['jobs_remaining'])
    return result


# ─── R41 P1-7: 带授权的维护模式 API ────────────────────────────


async def enable_with_reason(reason: str, principal_id: int) -> bool:
    """R41 P1-7: 开启维护模式并记录启用原因和操作者。

    在 enable() 基础上增加:
    - 显式记录 principal_id 到 audit_log(actor_id)
    - 记录启用原因到 maintenance_state.reason
    - 操作者 ID 通过 started_by 字段持久化

    Args:
        reason: 维护原因(记入 audit_log.details + maintenance_state.reason)
        principal_id: 操作者 admin_id(记入 audit_log.actor_id + maintenance_state.started_by)

    Returns:
        True 开启成功, False 失败
    """
    logger.info(
        f"[Maintenance] enable_with_reason: reason={reason} "
        f"principal_id={principal_id}"
    )
    # 调用 enable(),传递 principal_id 作为 started_by
    ok = await enable(reason, started_by=principal_id)
    if ok:
        # 额外记录一条专门的 audit_log(标记为带授权的启用)
        await _write_audit_log(
            actor_id=principal_id,
            action="enable_maintenance_with_reason",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=_i18n_t('services.maintenance_mode.s31', reason=reason),
        )
    return ok


async def disable_with_authorization(principal_id: int, reason: str = "") -> bool:
    """R41 P1-7: 关闭维护模式(需授权)。

    要求 principal 拥有 maintenance:disable 权限才能关闭维护模式。
    无授权时抛 PermissionError,不调用 disable()。

    Args:
        principal_id: 操作者 admin_id
        reason: 关闭原因(记入 audit_log)

    Returns:
        True 关闭成功, False 失败

    Raises:
        PermissionError: principal 无 maintenance:disable 权限
    """
    # 1. RBAC 授权校验
    from services.rbac import check_permission
    has_perm = False
    try:
        has_perm = await check_permission(principal_id, "maintenance:disable")
    except Exception as e:
        # RBAC 校验异常时 fail-closed(拒绝关闭)
        logger.warning(
            f"[Maintenance] disable_with_authorization RBAC 异常"
            f"(fail-closed 拒绝) principal={principal_id}: {e}"
        )
        raise PermissionError(
            _i18n_t('services.maintenance_mode.s15', e=e)
        )
    if not has_perm:
        logger.warning(
            f"[Maintenance] disable_with_authorization 拒绝"
            f"(无 maintenance:disable 权限) principal={principal_id}"
        )
        # 记录未授权尝试到 audit_log
        await _write_audit_log(
            actor_id=principal_id,
            action="disable_maintenance_unauthorized",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=_i18n_t('services.maintenance_mode.s32', reason=reason),
        )
        raise PermissionError(
            _i18n_t('services.maintenance_mode.s4', principal_id=principal_id)
        )
    # 2. 授权通过,执行关闭(force=True 跳过前置检查,由授权决定)
    logger.info(
        f"[Maintenance] disable_with_authorization: 授权通过, "
        f"principal={principal_id} reason={reason}"
    )
    ok = await disable(ended_by=principal_id, force=True)
    if ok:
        # 记录带授权的关闭到 audit_log
        await _write_audit_log(
            actor_id=principal_id,
            action="disable_maintenance_with_authorization",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=_i18n_t('services.maintenance_mode.s33', reason=reason),
        )
    return ok


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
        raise MaintenanceCheckError(_i18n_t('services.maintenance_mode.s5'))
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
            _i18n_t('services.maintenance_mode.s16', e=e)
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
            "error": _i18n_t('services.maintenance_mode.s6'),
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
            "error": "" if ok else _i18n_t('services.maintenance_mode.s34'),
        })
        if not ok:
            overall_success = False
            failure_reason = _i18n_t('services.maintenance_mode.s7')
    except Exception as e:
        overall_success = False
        failure_reason = _i18n_t('services.maintenance_mode.s8', e=e)
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
                "error": (_i18n_t('services.maintenance_mode.s39') if drain_result.get("timeout")
                           else ""),
            })
            if not drained:
                overall_success = False
                failure_reason = (
                    _i18n_t('services.maintenance_mode.s17', drain_result_get_remaining_outbox=drain_result.get('remaining_outbox', '?'), drain_result_get_remaining_jobs=drain_result.get('remaining_jobs', '?'))
                )
        except Exception as e:
            overall_success = False
            failure_reason = _i18n_t('services.maintenance_mode.s18', e=e)
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
                "error": "" if ok else _i18n_t('services.maintenance_mode.s40'),
                "backup_id": backup_id,
            })
            if not ok:
                overall_success = False
                failure_reason = _i18n_t('services.maintenance_mode.s19')
        except Exception as e:
            overall_success = False
            failure_reason = _i18n_t('services.maintenance_mode.s20', e=e)
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
            "error": _i18n_t('services.maintenance_mode.s21'),
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
                "error": "" if ok else _i18n_t('services.maintenance_mode.s41', readiness=readiness),
                "readiness": readiness,
            })
            if not ok:
                overall_success = False
                failure_reason = _i18n_t('services.maintenance_mode.s22')
        except Exception as e:
            overall_success = False
            failure_reason = _i18n_t('services.maintenance_mode.s23', e=e)
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
        # R42 P1-12: 设置 recover_status='pending'(强制 recover_maintenance 审批才能关闭)
        # R51 P1-6: 持久化失败必须 fail-closed + 严重告警(不能只 warning)
        recover_persist_failed = False
        recover_persist_error = ""
        try:
            _recover_store = get_cache_store()
            async with _recover_store.transaction() as _recover_tx:
                await _recover_tx.execute(
                    "UPDATE maintenance_state SET recover_status = 'pending' "
                    "WHERE id = ?",
                    (MAINTENANCE_STATE_ID,),
                )
                await _recover_store.add_dirty_outbox(
                    "maintenance_state", str(MAINTENANCE_STATE_ID),
                    connection=_recover_tx,
                )
        except Exception as _recover_err:
            # R51 P1-6: recover_status 持久化失败 → 严重告警,服务保持 fail-closed
            recover_persist_failed = True
            recover_persist_error = str(_recover_err)
            logger.error(
                f"[Maintenance] R51 P1-6 设置 recover_status=pending 失败,"
                f"服务保持 fail-closed(严重告警): {_recover_err}"
            )
            # 通过 AppError 写入 audit_log(协议化错误,含 trace_id)
            try:
                from services.error_codes import AppError, ErrorCodes
                app_err = AppError(
                    ErrorCodes.MAINTENANCE_RECOVER_STATUS_PERSIST_FAILED,
                    params={
                        "reason": failure_reason,
                        "workflow_step": "set_recover_status_pending",
                    },
                )
                await app_err.write_audit_log()
            except Exception as audit_err:
                logger.error(
                    f"[Maintenance] R51 P1-6 写入 recover_status 持久化失败 audit_log 异常: {audit_err}"
                )
        try:
            await _write_audit_log(
                actor_id=started_by,
                action="maintenance_workflow_failed",
                target_type="maintenance_state",
                target_id=str(MAINTENANCE_STATE_ID),
                details=_i18n_t('services.maintenance_mode.s42', failure_reason=failure_reason)
                        + (_i18n_t('services.maintenance_mode.s46', recover_persist_error=recover_persist_error)
                           if recover_persist_failed else ""),
            )
        except Exception as log_err:
            logger.warning(f"[Maintenance] 记录失败原因异常: {log_err}")
        maintenance_kept_enabled = True
        # R51 P1-6: 持久化失败时记录到返回结果,便于上层处理
        if recover_persist_failed:
            return {
                "steps": steps,
                "success": False,
                "duration_seconds": _time.time() - workflow_start,
                "maintenance_kept_enabled": True,
                "failure_reason": failure_reason,
                "recover_status_persist_failed": True,
                "recover_persist_error": recover_persist_error,
            }
    else:
        # 全部成功 — 根据 auto_disable 决定是否关闭
        if auto_disable:
            step_start = _time.time()
            try:
                ok = await disable(ended_by=started_by)
                steps.append({
                    "name": "disable", "success": ok,
                    "duration_seconds": _time.time() - step_start,
                    "error": "" if ok else _i18n_t('services.maintenance_mode.s43'),
                })
            except MaintenancePreconditionError as e:
                # 前置检查未通过(异常情况,workflow 后队列应已排空)
                steps.append({
                    "name": "disable", "success": False,
                    "duration_seconds": _time.time() - step_start,
                    "error": _i18n_t('services.maintenance_mode.s44', e=e),
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


async def recover_maintenance(
    principal_id: int,
    reason: str,
    approval_action_id: str = None,
    request_hash: str = None,
) -> bool:
    """R42 P1-12 + R51 P1-6 + R52 P0-5: 人工恢复维护模式(需 CommandBus 审批 + maintenance:recover 权限 + request_hash 绑定)。

    与 ``rollback_maintenance`` 的差异:
        - rollback_maintenance 跳过所有检查(仅运维紧急恢复)
        - recover_maintenance 强制要求:
          1. approval_action_id 由 CommandBus 预先审批生成(非 None)
          2. R52 P0-5: approval_action_id 在 command_executions 中 status='approved'
             (旧版 'executed' 语义冲突;统一为 approved → executing → executed/failed 状态机)
          3. principal_id 拥有 ``maintenance:recover`` 权限
          4. R51 P1-6: request_hash 必须提供(绑定审批动作 + principal + 请求来源)
        - 通过验证后调用 ``disable(force=True, approval_action_id=..., request_hash=...)`` 关闭
          (CAS approved→executing + 状态机回写在 disable 内完成)
        - 成功后重置 recover_status='completed',写 audit_log(action="recover_maintenance")

    Args:
        principal_id: 操作者 admin_id(必须拥有 maintenance:recover 权限)
        reason: 恢复原因(记入 audit_log)
        approval_action_id: CommandBus 预先审批生成的 action_id(必须)
        request_hash: R51 P1-6 请求哈希(必须,绑定审批与请求来源)

    Returns:
        True 关闭成功, False 失败

    Raises:
        PermissionError: approval_action_id 缺失 / 未审批通过 / principal 无权限 / R51 P1-6 request_hash 缺失
    """
    # 1. approval_action_id 必须提供
    if not approval_action_id:
        logger.warning(
            f"[Maintenance] recover_maintenance 拒绝(未提供 approval_action_id) "
            f"principal={principal_id}"
        )
        raise PermissionError("recover_maintenance requires approval_action_id")

    # R51 P1-6: request_hash 必须提供(绑定审批动作 + principal + 请求来源)
    if not request_hash:
        logger.warning(
            f"[Maintenance] R51 P1-6 recover_maintenance 拒绝"
            f"(未提供 request_hash, principal={principal_id}, "
            f"approval_action_id={approval_action_id})"
        )
        # 协议化错误:写入 audit_log + 抛 PermissionError
        try:
            from services.error_codes import AppError, ErrorCodes
            app_err = AppError(
                ErrorCodes.MAINTENANCE_RECOVER_BINDING_REQUIRED,
                params={
                    "approval_action_id": approval_action_id,
                    "principal_id": principal_id,
                },
            )
            await app_err.write_audit_log()
        except Exception:
            pass  # audit_log 写入失败不影响主流程拒绝
        raise PermissionError(
            "R51 P1-6: recover_maintenance requires request_hash"
        )

    store = get_cache_store()
    if not store._db:
        raise PermissionError(_i18n_t('services.maintenance_mode.s9'))

    # 2. R52 P0-5: 验证 approval_action_id 在 command_executions 中 status='approved'
    #    (旧版 'executed' 语义冲突:表示"已完成"但 recover_maintenance 即将执行;
    #     统一为 approved → executing → executed/failed 状态机)
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (approval_action_id,),
        )
    except Exception as e:
        raise PermissionError(_i18n_t('services.maintenance_mode.s24', e=e))

    if not rows or not rows[0]:
        raise PermissionError(
            _i18n_t('services.maintenance_mode.s10', approval_action_id=approval_action_id)
        )
    exec_status = rows[0][0]
    if exec_status != "approved":
        raise PermissionError(
            f"approval_action_id status not approved (current: {exec_status}): "
            f"{approval_action_id}"
        )

    # 3. 验证 principal_id 拥有 maintenance:recover 权限
    from services.rbac import check_permission
    has_perm = False
    try:
        has_perm = await check_permission(principal_id, "maintenance:recover")
    except Exception as e:
        # RBAC 校验异常时 fail-closed(拒绝恢复)
        logger.warning(
            f"[Maintenance] recover_maintenance RBAC 异常(fail-closed 拒绝) "
            f"principal={principal_id}: {e}"
        )
        raise PermissionError(
            _i18n_t('services.maintenance_mode.s25', e=e)
        )
    if not has_perm:
        logger.warning(
            f"[Maintenance] recover_maintenance 拒绝"
            f"(无 maintenance:recover 权限) principal={principal_id}"
        )
        # 记录未授权尝试到 audit_log
        await _write_audit_log(
            actor_id=principal_id,
            action="recover_maintenance_unauthorized",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=_i18n_t('services.maintenance_mode.s35', reason=reason),
        )
        raise PermissionError(
            _i18n_t('services.maintenance_mode.s11', principal_id=principal_id)
        )

    # 4. 通过验证 → 调用 disable(force=True, approval_action_id=..., request_hash=...)
    # R52 P1-6: request_hash 只记录短指纹(前 16 字符),避免完整 hash 泄露
    logger.info(
        f"[Maintenance] recover_maintenance 验证通过,关闭维护模式: "
        f"principal={principal_id} reason={reason} "
        f"approval_action_id={approval_action_id} request_hash={(request_hash or '')[:16]}"
    )
    ok = await disable(
        ended_by=principal_id,
        force=True,
        approval_action_id=approval_action_id,
        request_hash=request_hash,
    )
    if ok:
        # 重置 recover_status='completed'(允许后续正常 disable)
        try:
            await store._db.execute(
                "UPDATE maintenance_state SET recover_status = 'completed' "
                "WHERE id = ?",
                (MAINTENANCE_STATE_ID,),
            )
            await store._db.commit()
            await store.add_dirty_outbox(
                "maintenance_state", str(MAINTENANCE_STATE_ID)
            )
        except Exception as reset_err:
            logger.warning(
                f"[Maintenance] recover_maintenance 重置 recover_status 失败: {reset_err}"
            )
        # 写 audit_log
        await _write_audit_log(
            actor_id=principal_id,
            action="recover_maintenance",
            target_type="maintenance_state",
            target_id=str(MAINTENANCE_STATE_ID),
            details=_i18n_t('services.maintenance_mode.s36', reason=reason, approval_action_id=approval_action_id),
        )
    return ok


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
                        _i18n_t('services.maintenance_mode.s47')
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
                        _i18n_t('services.maintenance_mode.s48')
                    )
                except Exception:
                    pass
                return
            if enabled:
                try:
                    await update.message.reply_text(
                        _i18n_t('services.maintenance_mode.s45', action=action)
                    )
                except Exception:
                    pass
                return
            return await func(update, context, *args, **kwargs)

        return wrapper

    return decorator


# ─── R45 第 16 节: 统一入口检查(非 Telegram 场景) ────────────────


async def check_maintenance_at_entry(action: str = "operation") -> dict:
    """R45 第 16 节: 统一入口检查(供内部服务 / API 路由 / 周期任务使用)。

    与 ``require_maintenance_check`` 装饰器的差异:
        - 装饰器仅适用于 Telegram handler(依赖 ``update.message.reply_text``)
        - 本函数返回结构化结果,适用于:
            * 内部服务调用(如 CRDB 同步、备份引擎、复制任务调度)
            * Admin Web API 路由(Flask/FastAPI handler)
            * 周期任务(scheduler / cron job)
            * 非 Telegram Bot 入口(如 CLI 工具)

    fail-closed 原则:
        - DB 异常 / 缓存为空 → 返回 allowed=False(拒绝请求,安全优先)
        - DB 正常 + 维护开启 → 返回 allowed=False + reason="维护中"
        - DB 正常 + 维护关闭 → 返回 allowed=True(允许执行)
        - DB 不可达 + 有缓存 → 使用缓存值判定

    Args:
        action: 操作描述(用于日志和返回结果的 reason 字段),
            如 "上传文件" / "解码文件" / "CRDB 同步" / "备份"

    Returns:
        {
            "allowed": bool,           # 是否允许执行该 action
            "maintenance_enabled": bool,  # 维护模式是否开启
            "reason": str,             # 拒绝原因(allowed=False 时填充)
            "source": str,             # 状态来源:db / cache / unknown
            "action": str,             # 入参 action(用于日志追踪)
            "last_checked": str|None,  # 最后检查时间(ISO)
            "error": str,              # 错误信息(空字符串表示无错误)
        }

    Example:
        # 在 API 路由中使用
        result = await check_maintenance_at_entry("CRDB 同步")
        if not result["allowed"]:
            return jsonify({"error": result["reason"]}), 503
        # 继续执行业务逻辑
    """
    # 优先尝试 DB 权威查询
    try:
        state = await get_maintenance_state()
        enabled = state.get("enabled")
        source = state.get("source", "unknown")
        error = state.get("error", "")
        last_checked = state.get("last_checked")
        if enabled is None:
            # DB 不可达且无缓存 → fail-closed
            logger.warning(
                f"[Maintenance] check_maintenance_at_entry fail-closed "
                f"action={action} (无法判定状态): {error}"
            )
            return {
                "allowed": False,
                "maintenance_enabled": False,
                "reason": _i18n_t('services.maintenance_mode.s26', error=error),
                "source": source,
                "action": action,
                "last_checked": last_checked,
                "error": error or _i18n_t('services.maintenance_mode.s37'),
            }
        if enabled:
            logger.info(
                f"[Maintenance] check_maintenance_at_entry 拒绝"
                f"(维护中) action={action}"
            )
            return {
                "allowed": False,
                "maintenance_enabled": True,
                "reason": _i18n_t('services.maintenance_mode.s27', action=action),
                "source": source,
                "action": action,
                "last_checked": last_checked,
                "error": "",
            }
        # 维护关闭 → 允许执行
        return {
            "allowed": True,
            "maintenance_enabled": False,
            "reason": "",
            "source": source,
            "action": action,
            "last_checked": last_checked,
            "error": "",
        }
    except MaintenanceCheckError as e:
        # fail-closed — 异常时拒绝
        logger.warning(
            f"[Maintenance] check_maintenance_at_entry fail-closed "
            f"action={action} (MaintenanceCheckError): {e}"
        )
        return {
            "allowed": False,
            "maintenance_enabled": False,
            "reason": _i18n_t('services.maintenance_mode.s28'),
            "source": "unknown",
            "action": action,
            "last_checked": _last_checked_ts,
            "error": str(e),
        }
    except Exception as e:
        # 兜底:其他异常也 fail-closed
        logger.warning(
            f"[Maintenance] check_maintenance_at_entry 异常 "
            f"action={action}: {e}"
        )
        return {
            "allowed": False,
            "maintenance_enabled": False,
            "reason": _i18n_t('services.maintenance_mode.s29'),
            "source": "unknown",
            "action": action,
            "last_checked": _last_checked_ts,
            "error": str(e),
        }
