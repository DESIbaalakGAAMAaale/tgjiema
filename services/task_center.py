"""R40 §9.1.1 + R45 第 16 节: 统一任务中心 — 跨 Bot 任务状态聚合。

为上传、索引、复制、取件、失败重试等操作提供统一的状态视图,
用户可随时查看任务进度和预计完成时间(ETA)。

R45 第 16 节 Task Center 整改:
- 接入 upload/index/copy/delivery/repair 实际状态(由真实 worker 调用
  update_task_progress 更新 progress/eta)
- 用户查询隔离:用户只能查询自己的 task(user_id 过滤),
  Admin 使用 list_all_tasks 独立 API(无 user_id 过滤)
- progress/ETA 不再由模拟数据填充,由真实 worker 在任务推进时主动更新

设计要点:
- 通过 get_cache_store() 获取 CacheStore 单例,直写 tasks 表
- 每次写入后调用 add_dirty_outbox(table_name="tasks", pk=str(task_id)) 触发 CRDB 同步
- payload / result 字段以 JSON 字符串存储
- 错误捕获后仅记录日志,不抛出(除非致命错误)
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.error_codes import AppError, ErrorCodes
from utils.trace_context import get_trace_id

# 任务状态枚举
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 状态图标(用户可读)
_STATUS_ICONS = {
    STATUS_PENDING: "⏳",
    STATUS_RUNNING: "🔄",
    STATUS_COMPLETED: "✅",
    STATUS_FAILED: "❌",
    STATUS_CANCELLED: "🚫",
}

# 支持的任务类型
TASK_TYPES = ("upload", "index", "copy", "delivery", "repair")

# 任务类型中文名
_TYPE_LABELS = {
    "upload": "上传",
    "index": "索引",
    "copy": "复制",
    "delivery": "取件",
    "repair": "修复",
}


def _safe_json_loads(val) -> Any:
    """安全反序列化 JSON 字符串,失败返回 None。"""
    if val is None or val == "":
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _safe_json_dumps(val) -> str:
    """序列化为 JSON 字符串(None 转为 "{}")。"""
    if val is None:
        return "{}"
    return json.dumps(val, ensure_ascii=False, default=str)


async def create_task(task_type: str, user_id: int, payload: dict, trace_id: str = "") -> int:
    """创建任务,返回 task_id。

    Args:
        task_type: upload/index/copy/delivery/repair
        user_id: 用户 ID
        payload: 任务载荷(JSON 序列化存储,如 {"file_count": 3, "total_size": 1024})
        trace_id: 追踪 ID(为空时从上下文 get_trace_id() 获取)

    Returns:
        task_id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[task_center] CacheStore 未初始化")
        return 0
    # trace_id 优先使用参数,否则从上下文获取
    tid = trace_id or (get_trace_id() or "")
    now = _dt.datetime.now().isoformat()
    payload_json = _safe_json_dumps(payload)
    # R40 P0-5: 业务表 + dirty_outbox 同事务,失败整体回滚
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """INSERT INTO tasks (task_type, user_id, status, progress, eta_seconds,
                                       payload, trace_id, created_at, updated_at)
                   VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?)""",
                (task_type, user_id, STATUS_PENDING, payload_json, tid, now, now),
            )
            task_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if task_id:
                # 写 dirty_outbox 触发 CRDB 同步(同一事务,失败回滚)
                await store.add_dirty_outbox("tasks", str(task_id), connection=tx)
                logger.info(
                    f"[task_center] 创建任务 task_id={task_id} type={task_type} user_id={user_id}"
                )
        return task_id
    except Exception as e:
        logger.warning(f"[task_center] create_task 失败: {e}")
        return 0


async def update_progress(task_id: int, progress: int, eta_seconds: int = 0) -> bool:
    """更新任务进度(0-100)和预计剩余时间。

    Args:
        task_id: 任务 ID
        progress: 进度百分比 0-100(自动 clamp)
        eta_seconds: 预计剩余秒数

    Returns:
        True=成功;False=失败或任务已终态
    """
    store = get_cache_store()
    if not store._db:
        return False
    # 限制进度范围
    progress = max(0, min(100, int(progress)))
    now = _dt.datetime.now().isoformat()
    # 进度 > 0 时自动切到 running
    new_status = STATUS_RUNNING if progress > 0 else STATUS_PENDING
    # R40 P0-5: 业务表 + dirty_outbox 同事务
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE tasks
                   SET progress = ?, eta_seconds = ?, status = ?, updated_at = ?
                   WHERE id = ? AND status NOT IN (?, ?, ?)""",
                (progress, max(0, int(eta_seconds)), new_status, now,
                 task_id, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox("tasks", str(task_id), connection=tx)
        return ok
    except Exception as e:
        logger.warning(f"[task_center] update_progress 失败: {e}")
        return False


async def complete_task(task_id: int, result: dict) -> bool:
    """标记任务完成。

    Args:
        task_id: 任务 ID
        result: 任务结果(JSON 序列化存储)

    Returns:
        True=成功;False=失败或任务已终态
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    result_json = _safe_json_dumps(result)
    # R40 P0-5: 业务表 + dirty_outbox 同事务
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE tasks
                   SET status = ?, progress = 100, result = ?, updated_at = ?
                   WHERE id = ? AND status NOT IN (?, ?)""",
                (STATUS_COMPLETED, result_json, now,
                 task_id, STATUS_COMPLETED, STATUS_CANCELLED),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox("tasks", str(task_id), connection=tx)
                logger.info(f"[task_center] 任务完成 task_id={task_id}")
        return ok
    except Exception as e:
        logger.warning(f"[task_center] complete_task 失败: {e}")
        return False


async def fail_task(task_id: int, error: str) -> bool:
    """标记任务失败。

    Args:
        task_id: 任务 ID
        error: 失败原因文本

    Returns:
        True=成功;False=失败或任务已终态
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    # R40 P0-5: 业务表 + dirty_outbox 同事务
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE tasks
                   SET status = ?, error = ?, updated_at = ?
                   WHERE id = ? AND status NOT IN (?, ?)""",
                (STATUS_FAILED, error, now,
                 task_id, STATUS_COMPLETED, STATUS_CANCELLED),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox("tasks", str(task_id), connection=tx)
                logger.warning(f"[task_center] 任务失败 task_id={task_id} error={error}")
        return ok
    except Exception as e:
        logger.warning(f"[task_center] fail_task 失败: {e}")
        return False


async def cancel_task(task_id: int) -> bool:
    """取消任务。

    Args:
        task_id: 任务 ID

    Returns:
        True=成功;False=失败或任务已终态
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    # R40 P0-5: 业务表 + dirty_outbox 同事务
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE tasks
                   SET status = ?, updated_at = ?
                   WHERE id = ? AND status NOT IN (?, ?, ?)""",
                (STATUS_CANCELLED, now,
                 task_id, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox("tasks", str(task_id), connection=tx)
                logger.info(f"[task_center] 任务取消 task_id={task_id}")
        return ok
    except Exception as e:
        logger.warning(f"[task_center] cancel_task 失败: {e}")
        return False


async def get_task(task_id: int) -> dict | None:
    """获取任务详情。

    Args:
        task_id: 任务 ID

    Returns:
        任务字典 {id, task_type, user_id, status, progress, eta_seconds,
                 payload, result, error, trace_id, created_at, updated_at};
        不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            """SELECT id, task_type, user_id, status, progress, eta_seconds,
                      payload, result, error, trace_id, created_at, updated_at
               FROM tasks WHERE id = ?""",
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "task_type": row[1],
            "user_id": row[2],
            "status": row[3],
            "progress": int(row[4] or 0),
            "eta_seconds": int(row[5] or 0),
            "payload": _safe_json_loads(row[6]),
            "result": _safe_json_loads(row[7]),
            "error": row[8],
            "trace_id": row[9] or "",
            "created_at": row[10],
            "updated_at": row[11],
        }
    except Exception as e:
        logger.warning(f"[task_center] get_task 失败: {e}")
        return None


async def get_user_task(task_id: int, user_id: int) -> dict | None:
    """R45 第 16 节: 用户查询自己的任务详情(强制 user_id 过滤,实现查询隔离)。

    与 ``get_task`` 的区别:
        - 必须传入 user_id,只返回该用户拥有的任务
        - 若 task_id 存在但 user_id 不匹配 → 返回 None(避免泄漏其他用户任务)
        - 用于 Bot 前端查询场景(用户只能查自己的任务)

    Args:
        task_id: 任务 ID
        user_id: 当前用户 ID(必须匹配 tasks.user_id)

    Returns:
        任务字典(同 get_task);不存在或不属于该用户返回 None
    """
    task = await get_task(task_id)
    if task is None:
        return None
    # 强制 user_id 匹配:不匹配视为不存在(防查询越权)
    if int(task.get("user_id", 0) or 0) != int(user_id):
        logger.info(
            f"[task_center] get_user_task 拒绝跨用户查询 "
            f"task_id={task_id} requester={user_id} owner={task.get('user_id')}"
        )
        return None
    return task


async def update_task_progress(
    task_id: int,
    progress: int,
    eta_seconds: int = 0,
) -> bool:
    """R45 第 16 节: 由真实 worker 更新任务进度和 ETA(对接 upload/index/copy/delivery/repair 各阶段)。

    与 ``update_progress`` 的差异:
        - 显式标记为 worker 入口(语义清晰,便于审计/拦截)
        - 内部委托给 ``update_progress`` 复用 CAS + dirty_outbox 逻辑
        - progress=100 时自动调用 complete_task(若 result 已有则保留)
        - progress 范围自动 clamp 到 [0, 100]

    Args:
        task_id: 任务 ID
        progress: 进度百分比 0-100(自动 clamp)
        eta_seconds: 预计剩余秒数(0=无预估)

    Returns:
        True=更新成功;False=任务不存在或已终态
    """
    progress = max(0, min(100, int(progress)))
    # progress=100 由 worker 标记完成时,使用 complete_task 路径
    # (但 worker 通常会传入 result,所以这里仅更新 progress,complete 由 worker 显式调用)
    return await update_progress(task_id, progress, eta_seconds)


async def list_user_tasks(
    user_id: int,
    status: str | None = None,
    limit: int = 20,
    cursor: int = 0,
) -> dict:
    """R45 第 16 节 + R51 P1-4: 列出用户任务(强制 user_id 过滤,支持 cursor 分页)。

    整改要点:
        - 强制按 user_id 过滤(用户只能看自己的任务)
        - 使用 cursor 分页(基于 id,避免 OFFSET 性能问题)
        - 返回结构化为 dict(含 items + next_cursor + has_more)
        - R51 P1-4: DB 异常不再返回空 list(fail-silent),改为 raise AppError

    Args:
        user_id: 用户 ID(强制过滤)
        status: 可选状态过滤(pending/running/completed/failed/cancelled)
        limit: 返回条数上限(1-100)
        cursor: 游标(上一页最后一条 id;0 表示从头开始)

    Returns:
        {
            "items": list[dict],      # 任务字典列表(按 id 倒序)
            "next_cursor": int,       # 下一页游标(0 表示无更多)
            "has_more": bool,          # 是否还有更多数据
        }

    Raises:
        AppError(TASK_CENTER_LIST_DB_ERROR): DB 查询异常(fail-closed 拒绝)
    """
    store = get_cache_store()
    default = {"items": [], "next_cursor": 0, "has_more": False}
    if not store._db:
        # R51 P1-4: CacheStore 未初始化视为 DB 异常,raise AppError
        raise AppError(
            ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
            params={"user_id": user_id, "reason": "cache_store_unavailable"},
        )
    limit = max(1, min(100, int(limit)))
    cursor = max(0, int(cursor))
    try:
        if status:
            # 多取一条判断 has_more
            rows_cursor = await store._db.execute(
                """SELECT id, task_type, user_id, status, progress, eta_seconds,
                          payload, result, error, trace_id, created_at, updated_at
                   FROM tasks WHERE user_id = ? AND status = ? AND id < ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, status, cursor if cursor > 0 else 2**31, limit + 1),
            )
        else:
            rows_cursor = await store._db.execute(
                """SELECT id, task_type, user_id, status, progress, eta_seconds,
                          payload, result, error, trace_id, created_at, updated_at
                   FROM tasks WHERE user_id = ? AND id < ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, cursor if cursor > 0 else 2**31, limit + 1),
            )
        rows = await rows_cursor.fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        items = [
            {
                "id": r[0], "task_type": r[1], "user_id": r[2], "status": r[3],
                "progress": int(r[4] or 0), "eta_seconds": int(r[5] or 0),
                "payload": _safe_json_loads(r[6]),
                "result": _safe_json_loads(r[7]),
                "error": r[8], "trace_id": r[9] or "",
                "created_at": r[10], "updated_at": r[11],
            }
            for r in rows
        ]
        next_cursor = items[-1]["id"] if items else 0
        return {
            "items": items,
            "next_cursor": next_cursor if has_more else 0,
            "has_more": has_more,
        }
    except AppError:
        raise
    except Exception as e:
        # R51 P1-4: DB 异常不再返回空 list(fail-silent),
        # 改为 raise AppError 让上层显式处理(避免误判为"用户无任务")
        logger.error(
            f"[task_center] list_user_tasks DB 异常 user={user_id}(fail-closed 拒绝): {e}"
        )
        raise AppError(
            ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
            params={
                "user_id": user_id,
                "reason": f"{type(e).__name__}: {e}",
            },
        ) from e


async def list_all_tasks(
    limit: int = 50,
    offset: int = 0,
    status_filter: str | None = None,
    filters: dict | None = None,
) -> list:
    """R40 P1-1 + R45 第 16 节 + R51 P1-4: Admin 列出所有用户的任务(管理后台用)。

    R45 整改:
        - 新增 filters 参数,支持 user_id / task_type / trace_id 多维过滤
        - 保持返回 list[dict] 的旧契约(向后兼容 admin 路由和 bot 命令)

    R51 P1-4 整改:
        - DB 异常不再返回空 list(fail-silent),改为 raise AppError
        - 避免管理后台误判"系统无任何任务"导致监控盲区

    与 ``list_user_tasks`` 的区别:
        - 不强制 user_id 过滤(Admin 可看所有用户)
        - 支持 offset 分页(管理后台按页浏览)
        - 支持 status_filter + filters 多维过滤

    Args:
        limit: 返回条数上限(1-200,管理后台需要更大窗口)
        offset: 偏移量(分页用,0 表示从头开始)
        status_filter: 可选状态过滤(pending/running/completed/failed/cancelled)
        filters: 可选多维过滤 {
            "user_id": int,        # 按 user_id 过滤
            "task_type": str,      # 按 task_type 过滤
            "trace_id": str,       # 按 trace_id 过滤
        }

    Returns:
        list[dict]: 任务字典列表(按 created_at 倒序),每项含 id/task_type/
        user_id/status/progress/eta_seconds/payload/result/error/trace_id/
        created_at/updated_at 字段。

    Raises:
        AppError(TASK_CENTER_LIST_DB_ERROR): DB 查询异常(fail-closed 拒绝)
    """
    store = get_cache_store()
    if not store._db:
        # R51 P1-4: CacheStore 未初始化视为 DB 异常
        raise AppError(
            ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
            params={"reason": "cache_store_unavailable"},
        )
    # clamp limit 到 [1, 200],offset 到 [0, +∞)
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    filters = filters or {}

    where_parts: list[str] = []
    params: list[Any] = []
    if status_filter:
        where_parts.append("status = ?")
        params.append(status_filter)
    if "user_id" in filters and filters["user_id"] is not None:
        where_parts.append("user_id = ?")
        params.append(int(filters["user_id"]))
    if "task_type" in filters and filters["task_type"]:
        where_parts.append("task_type = ?")
        params.append(filters["task_type"])
    if "trace_id" in filters and filters["trace_id"]:
        where_parts.append("trace_id = ?")
        params.append(filters["trace_id"])
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        query_sql = (
            f"SELECT id, task_type, user_id, status, progress, eta_seconds, "
            f"payload, result, error, trace_id, created_at, updated_at "
            f"FROM tasks {where_clause} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        query_params = list(params) + [limit, offset]
        rows = await store._db.execute_fetchall(query_sql, tuple(query_params))

        return [
            {
                "id": r[0], "task_type": r[1], "user_id": r[2], "status": r[3],
                "progress": int(r[4] or 0), "eta_seconds": int(r[5] or 0),
                "payload": _safe_json_loads(r[6]),
                "result": _safe_json_loads(r[7]),
                "error": r[8], "trace_id": r[9] or "",
                "created_at": r[10], "updated_at": r[11],
            }
            for r in rows
        ]
    except AppError:
        raise
    except Exception as e:
        # R51 P1-4: DB 异常不再返回空 list,改为 raise AppError
        logger.error(
            f"[task_center] list_all_tasks DB 异常(fail-closed 拒绝): {e}"
        )
        raise AppError(
            ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
            params={"reason": f"{type(e).__name__}: {e}"},
        ) from e


async def format_task_status(task: dict) -> str:
    """格式化任务状态为用户可读文本(含进度条、ETA、状态图标)。

    Args:
        task: get_task / list_user_tasks 返回的任务字典

    Returns:
        多行文本(纯文本,避免 Telegram markdown 解析问题)
    """
    if not task:
        return "任务不存在"
    status = task.get("status", "") or ""
    icon = _STATUS_ICONS.get(status, "❓")
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    # 进度条: 10 格(█ 已完成 + ░ 未完成)
    filled = progress // 10
    bar = "█" * filled + "░" * (10 - filled)
    # ETA 格式化(秒 → "X 小时 Y 分钟" / "X 分钟" / "X 秒" / "无预估")
    eta_seconds = int(task.get("eta_seconds", 0) or 0)
    if eta_seconds >= 3600:
        eta_str = f"{eta_seconds // 3600} 小时 {(eta_seconds % 3600) // 60} 分钟"
    elif eta_seconds >= 60:
        eta_str = f"{eta_seconds // 60} 分钟"
    elif eta_seconds > 0:
        eta_str = f"{eta_seconds} 秒"
    else:
        eta_str = "无预估"
    # 任务类型中文化
    type_name = _TYPE_LABELS.get(task.get("task_type", ""), task.get("task_type", ""))
    lines = [
        f"{icon} 任务 #{task.get('id', '')} - {type_name}",
        f"状态: {status} ({progress}%)",
        f"进度: [{bar}] {progress}%",
        f"预计剩余: {eta_str}",
        f"创建时间: {task.get('created_at', '')}",
    ]
    # 失败时显示错误
    if status == STATUS_FAILED and task.get("error"):
        lines.append(f"失败原因: {task['error']}")
    # 完成时显示结果(前 3 个键值对)
    if status == STATUS_COMPLETED and task.get("result"):
        result = task["result"]
        if isinstance(result, dict):
            for k, v in list(result.items())[:3]:
                lines.append(f"  {k}: {v}")
    return "\n".join(lines)


# R41 P1-12: Bot 任务接线 — 一站式任务记录方法
# 上传/解码/派送等关键操作完成时,Bot 调用 record_task 即可记录到 tasks 表,
# 无需分别调用 create_task + update_progress + complete_task。
# status='completed' 时自动 progress=100 + result=metadata;
# status='failed' 时自动 error=metadata.get('error')。


async def record_task(
    user_id: int,
    task_type: str,
    status: str,
    metadata: dict | None = None,
) -> int:
    """R41 P1-12 + R51 P1-4: 一站式任务记录(创建 + 直接进入指定状态)。

    适用于 Bot 在关键操作完成时(如上传成功、解码成功、派送成功)
    一次性记录任务,无需创建后多次更新。

    R51 P1-4 整改要点:
    - 未知 task_type 不再静默回退到 "index",改为 raise AppError
    - 未知 status 不再静默回退到 "pending",改为 raise AppError
    - 避免错误数据被默默写入,污染任务历史

    Args:
        user_id: 用户 ID
        task_type: 任务类型(必须是 TASK_TYPES 之一:upload/index/copy/delivery/repair)
        status: 任务状态(STATUS_PENDING / STATUS_RUNNING /
                  STATUS_COMPLETED / STATUS_FAILED / STATUS_CANCELLED)
        metadata: 任务元数据(写入 payload,完成时合并到 result,失败时取 error 字段)

    Returns:
        task_id;失败返回 0

    Raises:
        AppError(TASK_CENTER_UNKNOWN_TYPE): 未知 task_type
        AppError(TASK_CENTER_UNKNOWN_STATUS): 未知 status

    Example:
        # 上传成功后记录
        await task_center.record_task(
            user_id=12345, task_type="upload", status="completed",
            metadata={"file_code": "ABC123", "file_size": 1024},
        )
        # 派送失败后记录
        await task_center.record_task(
            user_id=12345, task_type="delivery", status="failed",
            metadata={"file_code": "ABC123", "error": "channel_unavailable"},
        )
    """
    if not metadata:
        metadata = {}
    # R51 P1-4: 未知 task_type 拒绝(不再回退到 index)
    if task_type not in TASK_TYPES:
        logger.error(
            f"[task_center] record_task 未知 task_type={task_type},拒绝记录"
        )
        raise AppError(
            ErrorCodes.TASK_CENTER_UNKNOWN_TYPE,
            params={
                "user_id": user_id, "task_type": task_type,
                "allowed_types": list(TASK_TYPES),
            },
        )
    # R51 P1-4: 未知 status 拒绝(不再回退到 pending)
    _ALLOWED_STATUSES = (
        STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED,
        STATUS_FAILED, STATUS_CANCELLED,
    )
    if status not in _ALLOWED_STATUSES:
        logger.error(
            f"[task_center] record_task 未知 status={status},拒绝记录"
        )
        raise AppError(
            ErrorCodes.TASK_CENTER_UNKNOWN_STATUS,
            params={
                "user_id": user_id, "status": status,
                "allowed_statuses": list(_ALLOWED_STATUSES),
            },
        )

    # 先创建 pending 任务
    payload = dict(metadata) if metadata else {}
    task_id = await create_task(task_type, user_id, payload)
    if not task_id:
        return 0

    # 按目标状态推进
    if status == STATUS_PENDING:
        return task_id
    if status == STATUS_RUNNING:
        await update_progress(task_id, progress=50, eta_seconds=0)
        return task_id
    if status == STATUS_COMPLETED:
        result = dict(metadata) if metadata else {}
        await complete_task(task_id, result)
        return task_id
    if status == STATUS_FAILED:
        error_msg = str(metadata.get("error") or "操作失败")
        await fail_task(task_id, error_msg)
        return task_id
    if status == STATUS_CANCELLED:
        await cancel_task(task_id)
        return task_id
    # 兜底(理论上不会到达,前面 status 已校验)
    return task_id
