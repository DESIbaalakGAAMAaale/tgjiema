"""R40 §9.1.1: 统一任务中心 — 跨 Bot 任务状态聚合。

为上传、索引、复制、取件、失败重试等操作提供统一的状态视图,
用户可随时查看任务进度和预计完成时间(ETA)。

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
    try:
        cursor = await store._db.execute(
            """INSERT INTO tasks (task_type, user_id, status, progress, eta_seconds,
                                   payload, trace_id, created_at, updated_at)
               VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?)""",
            (task_type, user_id, STATUS_PENDING, payload_json, tid, now, now),
        )
        await store._db.commit()
        task_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        if task_id:
            # 写 dirty_outbox 触发 CRDB 同步
            await store.add_dirty_outbox("tasks", str(task_id))
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
    try:
        cursor = await store._db.execute(
            """UPDATE tasks
               SET progress = ?, eta_seconds = ?, status = ?, updated_at = ?
               WHERE id = ? AND status NOT IN (?, ?, ?)""",
            (progress, max(0, int(eta_seconds)), new_status, now,
             task_id, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED),
        )
        await store._db.commit()
        ok = bool(cursor and cursor.rowcount > 0)
        if ok:
            await store.add_dirty_outbox("tasks", str(task_id))
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
    try:
        cursor = await store._db.execute(
            """UPDATE tasks
               SET status = ?, progress = 100, result = ?, updated_at = ?
               WHERE id = ? AND status NOT IN (?, ?)""",
            (STATUS_COMPLETED, result_json, now,
             task_id, STATUS_COMPLETED, STATUS_CANCELLED),
        )
        await store._db.commit()
        ok = bool(cursor and cursor.rowcount > 0)
        if ok:
            await store.add_dirty_outbox("tasks", str(task_id))
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
    try:
        cursor = await store._db.execute(
            """UPDATE tasks
               SET status = ?, error = ?, updated_at = ?
               WHERE id = ? AND status NOT IN (?, ?)""",
            (STATUS_FAILED, error, now,
             task_id, STATUS_COMPLETED, STATUS_CANCELLED),
        )
        await store._db.commit()
        ok = bool(cursor and cursor.rowcount > 0)
        if ok:
            await store.add_dirty_outbox("tasks", str(task_id))
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
    try:
        cursor = await store._db.execute(
            """UPDATE tasks
               SET status = ?, updated_at = ?
               WHERE id = ? AND status NOT IN (?, ?, ?)""",
            (STATUS_CANCELLED, now,
             task_id, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED),
        )
        await store._db.commit()
        ok = bool(cursor and cursor.rowcount > 0)
        if ok:
            await store.add_dirty_outbox("tasks", str(task_id))
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


async def list_user_tasks(user_id: int, status: str | None = None, limit: int = 20) -> list[dict]:
    """列出用户任务,可按状态过滤。

    Args:
        user_id: 用户 ID
        status: 可选状态过滤(pending/running/completed/failed/cancelled)
        limit: 返回条数上限(1-100)

    Returns:
        任务字典列表(按 created_at 倒序)
    """
    store = get_cache_store()
    if not store._db:
        return []
    limit = max(1, min(100, int(limit)))
    try:
        if status:
            cursor = await store._db.execute(
                """SELECT id, task_type, user_id, status, progress, eta_seconds,
                          payload, result, error, trace_id, created_at, updated_at
                   FROM tasks WHERE user_id = ? AND status = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, status, limit),
            )
        else:
            cursor = await store._db.execute(
                """SELECT id, task_type, user_id, status, progress, eta_seconds,
                          payload, result, error, trace_id, created_at, updated_at
                   FROM tasks WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            )
        rows = await cursor.fetchall()
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
    except Exception as e:
        logger.warning(f"[task_center] list_user_tasks 失败: {e}")
        return []


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
