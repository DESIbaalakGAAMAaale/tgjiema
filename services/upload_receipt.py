"""R40 §9.1.2: 上传回执 — 向用户返回结构化上传结果。

为上传完成后的文件提供结构化回执,支持 /status <upload_id> 查询进度。
回执数据存入 upload_sessions.options_json 扩展字段(避免新增表)。

设计要点:
- generate_receipt 同时创建一个 upload 任务(调用 task_center.create_task)
- get_upload_status 联合查询 upload_sessions + tasks 表
- format_receipt 返回纯文本(避免 Telegram markdown 解析问题)
- 通过 get_cache_store() 获取 CacheStore 单例
"""
import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.task_center import create_task


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


def _format_size(size_bytes: int) -> str:
    """格式化文件大小为人类可读文本(B/KB/MB/GB)。"""
    size_bytes = int(size_bytes or 0)
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes} B"


async def generate_receipt(upload_id: str, user_id: int, file_count: int,
                           total_size: int, ttl_days: int = 7,
                           primary_status: str = "ready") -> dict:
    """生成上传回执,存入 upload_sessions 表(扩展字段)。

    Args:
        upload_id: 上传会话 ID
        user_id: 用户 ID
        file_count: 文件数
        total_size: 总字节数
        ttl_days: 保护期天数(默认 7)
        primary_status: 主副本状态(ready/uploading/failed)

    Returns:
        {upload_id, file_count, total_size, ttl_days, primary_status,
         file_codes, created_at, task_id}
    """
    store = get_cache_store()
    now = _dt.datetime.now().isoformat()
    now_ts = _dt.datetime.now().timestamp()
    receipt = {
        "upload_id": upload_id,
        "user_id": user_id,
        "file_count": int(file_count),
        "total_size": int(total_size),
        "ttl_days": int(ttl_days),
        "primary_status": primary_status,
        "file_codes": [],  # 调用方可在文件索引完成后填充
        "created_at": now,
    }
    if not store._db:
        logger.warning("[upload_receipt] CacheStore 未初始化")
        return receipt
    try:
        # 查 upload_sessions 是否存在
        cursor = await store._db.execute(
            "SELECT upload_id, options_json FROM upload_sessions WHERE upload_id = ?",
            (upload_id,),
        )
        row = await cursor.fetchone()
        if row:
            # 已存在会话,合并 options_json(保留其他字段)
            existing_options = _safe_json_loads(row[1]) or {}
            if not isinstance(existing_options, dict):
                existing_options = {}
            existing_options["receipt"] = receipt
            await store._db.execute(
                "UPDATE upload_sessions SET options_json = ?, updated_at = ? WHERE upload_id = ?",
                (_safe_json_dumps(existing_options), now_ts, upload_id),
            )
            await store._db.commit()
        else:
            # 不存在会话,创建一个 READY 状态的占位会话(便于后续 /status 查询)
            await store._db.execute(
                """INSERT OR IGNORE INTO upload_sessions
                   (upload_id, user_id, status, options_json, trace_id,
                    created_at, updated_at)
                   VALUES (?, ?, 'READY', ?, '', ?, ?)""",
                (upload_id, user_id, _safe_json_dumps({"receipt": receipt}),
                 now_ts, now_ts),
            )
            await store._db.commit()
        # 写 dirty_outbox 触发 CRDB 同步
        await store.add_dirty_outbox("upload_sessions", upload_id)
        # 同时创建一个 upload 任务供 /status 跟踪进度
        task_id = await create_task(
            "upload", user_id,
            {
                "upload_id": upload_id,
                "file_count": int(file_count),
                "total_size": int(total_size),
                "ttl_days": int(ttl_days),
            },
        )
        receipt["task_id"] = task_id
        logger.info(
            f"[upload_receipt] 生成回执 upload_id={upload_id} "
            f"file_count={file_count} task_id={task_id}"
        )
    except Exception as e:
        logger.warning(f"[upload_receipt] generate_receipt 失败: {e}")
    return receipt


async def get_receipt(upload_id: str) -> dict | None:
    """获取上传回执。

    Args:
        upload_id: 上传会话 ID

    Returns:
        回执字典;不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            "SELECT options_json FROM upload_sessions WHERE upload_id = ?",
            (upload_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        options = _safe_json_loads(row[0])
        if not isinstance(options, dict):
            return None
        return options.get("receipt")
    except Exception as e:
        logger.warning(f"[upload_receipt] get_receipt 失败: {e}")
        return None


async def get_upload_status(upload_id: str) -> dict:
    """获取上传状态(含进度、ETA、文件列表)。

    联合查询 upload_sessions + tasks 表(通过 receipt.task_id 关联)。

    Args:
        upload_id: 上传会话 ID

    Returns:
        {status, progress, file_count, file_codes, primary_status, eta_seconds}
        status 枚举: pending/uploading/copied/indexing/ready/failed
    """
    store = get_cache_store()
    default = {
        "status": "pending",
        "progress": 0,
        "file_count": 0,
        "file_codes": [],
        "primary_status": "unknown",
        "eta_seconds": 0,
    }
    if not store._db:
        return default
    try:
        cursor = await store._db.execute(
            """SELECT upload_id, user_id, status, options_json
               FROM upload_sessions WHERE upload_id = ?""",
            (upload_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return default
        session_status = (row[2] or "").lower()
        options = _safe_json_loads(row[3]) or {}
        if not isinstance(options, dict):
            options = {}
        receipt = options.get("receipt") or {}
        # 映射 upload_sessions 状态到对外状态
        # upload_sessions 状态: RECEIVED/COPIED_PRIMARY/MANIFESTED/
        #                       OPTIONS_PENDING/INDEX_PENDING/READY/ABORTED/EXPIRED
        # 对外状态: pending/uploading/copied/indexing/ready/failed
        status_map = {
            "received": "uploading",
            "copied_primary": "copied",
            "manifested": "copied",
            "options_pending": "indexing",
            "index_pending": "indexing",
            "ready": "ready",
            "aborted": "failed",
            "expired": "failed",
        }
        external_status = status_map.get(session_status, session_status or "pending")
        # 查关联任务获取进度和 ETA
        progress = 0
        eta_seconds = 0
        task_id = receipt.get("task_id") if isinstance(receipt, dict) else None
        if task_id:
            t_cursor = await store._db.execute(
                "SELECT progress, eta_seconds, status FROM tasks WHERE id = ?",
                (task_id,),
            )
            t_row = await t_cursor.fetchone()
            if t_row:
                progress = int(t_row[0] or 0)
                eta_seconds = int(t_row[1] or 0)
                task_status = (t_row[2] or "").lower()
                # 任务状态覆盖会话状态
                if task_status == "completed":
                    external_status = "ready"
                    progress = 100
                elif task_status == "failed":
                    external_status = "failed"
                elif task_status == "running" and progress > 0:
                    # 任务进行中,根据进度推断阶段
                    if progress < 50:
                        external_status = "uploading"
                    else:
                        external_status = "indexing"
        file_codes = receipt.get("file_codes", []) if isinstance(receipt, dict) else []
        primary_status = receipt.get("primary_status", "unknown") if isinstance(receipt, dict) else "unknown"
        return {
            "status": external_status,
            "progress": progress,
            "file_count": receipt.get("file_count", 0) if isinstance(receipt, dict) else 0,
            "file_codes": file_codes if isinstance(file_codes, list) else [],
            "primary_status": primary_status,
            "eta_seconds": eta_seconds,
        }
    except Exception as e:
        logger.warning(f"[upload_receipt] get_upload_status 失败: {e}")
        return default


async def format_receipt(receipt: dict) -> str:
    """格式化回执为用户可读消息。

    Args:
        receipt: generate_receipt / get_receipt 返回的回执字典

    Returns:
        多行纯文本(避免 Telegram markdown 解析问题)
    """
    if not receipt:
        return "上传回执不存在"
    lines = [
        "📤 上传回执",
        f"上传 ID: {receipt.get('upload_id', '')}",
        f"文件数: {receipt.get('file_count', 0)}",
        f"总大小: {_format_size(receipt.get('total_size', 0))}",
        f"保护期: {receipt.get('ttl_days', 0)} 天",
        f"主副本状态: {receipt.get('primary_status', 'unknown')}",
    ]
    # 文件码列表(若已生成)
    file_codes = receipt.get("file_codes") or []
    if file_codes:
        lines.append(f"文件码: {', '.join(file_codes)}")
    lines.append("")
    lines.append(f"查询状态: /status {receipt.get('upload_id', '')}")
    return "\n".join(lines)
