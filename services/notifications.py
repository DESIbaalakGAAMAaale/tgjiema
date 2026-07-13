"""R40 §9.1.4: 可靠通知 — 跨 Bot 推送给用户。

为系统事件(文件就绪、R100 延迟、副本不足、过期前提醒、恢复完成等)
提供可靠通知能力,通知先落库,由各 Bot 异步推送。

设计要点:
- payload 存 JSON 字符串(如 {"file_code": "ABC123", "delay_minutes": 30})
- format_notification 按类型返回不同文案(含图标 + 操作建议)
- 通过 get_cache_store() 获取 CacheStore 单例
- 每次写入后调用 add_dirty_outbox(table_name="notifications", pk=str(notif_id))
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store

# 通知类型常量
NOTIF_TYPE_READY = "ready"                    # 文件就绪
NOTIF_TYPE_R100_DELAY = "r100_delay"         # R100 副本延迟
NOTIF_TYPE_REPLICA_SHORT = "replica_short"    # 副本不足
NOTIF_TYPE_EXPIRY_WARNING = "expiry_warning"  # 过期前提醒
NOTIF_TYPE_RECOVERY = "recovery"              # 恢复完成
NOTIF_TYPE_TAKEDOWN = "takedown"              # 内容下架
NOTIF_TYPE_BAN = "ban"                        # 账号封禁

# 通知图标(用户可读)
_NOTIF_ICONS = {
    NOTIF_TYPE_READY: "✅",
    NOTIF_TYPE_R100_DELAY: "⏳",
    NOTIF_TYPE_REPLICA_SHORT: "⚠️",
    NOTIF_TYPE_EXPIRY_WARNING: "⏰",
    NOTIF_TYPE_RECOVERY: "🔄",
    NOTIF_TYPE_TAKEDOWN: "🚫",
    NOTIF_TYPE_BAN: "🔨",
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


async def send(user_id: int, notif_type: str, payload: dict) -> int:
    """发送通知给用户,返回 notif_id。

    Args:
        user_id: 用户 ID
        notif_type: 通知类型(NOTIF_TYPE_*)
        payload: 通知载荷(JSON 序列化存储)

    Returns:
        notif_id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[notifications] CacheStore 未初始化")
        return 0
    now = _dt.datetime.now().isoformat()
    payload_json = _safe_json_dumps(payload)
    # R40 P0-5: 业务表 + dirty_outbox 同事务
    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """INSERT INTO notifications (user_id, type, payload, is_read, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (user_id, notif_type, payload_json, now),
            )
            notif_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if notif_id:
                await store.add_dirty_outbox("notifications", str(notif_id), connection=tx)
                logger.info(
                    f"[notifications] 发送通知 id={notif_id} "
                    f"user_id={user_id} type={notif_type}"
                )
        return notif_id
    except Exception as e:
        logger.warning(f"[notifications] send 失败: {e}")
        return 0


async def mark_read(notif_id: int) -> bool:
    """标记已读。

    Args:
        notif_id: 通知 ID

    Returns:
        True=成功;False=失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        # R40 P2-4: 同时写入 read_at,供 Prometheus 通知投递延迟指标采集
        now = _dt.datetime.now().isoformat()
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE notifications SET is_read = 1, read_at = ? WHERE id = ?",
                (now, notif_id),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox("notifications", str(notif_id), connection=tx)
        return ok
    except Exception as e:
        logger.warning(f"[notifications] mark_read 失败: {e}")
        return False


async def mark_all_read(user_id: int) -> int:
    """标记用户所有通知已读,返回数量。

    Args:
        user_id: 用户 ID

    Returns:
        实际标记数量
    """
    store = get_cache_store()
    if not store._db:
        return 0
    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        # R40 P2-4: 同时写入 read_at,供 Prometheus 通知投递延迟指标采集
        now = _dt.datetime.now().isoformat()
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE notifications SET is_read = 1, read_at = ? "
                "WHERE user_id = ? AND is_read = 0",
                (now, user_id),
            )
            count = int(cursor.rowcount) if cursor else 0
            if count > 0:
                # 用 user:<id> 作为 pk 范围标记,触发批量同步
                await store.add_dirty_outbox(
                    "notifications", f"user:{user_id}", connection=tx,
                )
                logger.info(
                    f"[notifications] 标记 {count} 条已读 user_id={user_id}"
                )
        return count
    except Exception as e:
        logger.warning(f"[notifications] mark_all_read 失败: {e}")
        return 0


async def list_unread(user_id: int, limit: int = 20) -> list[dict]:
    """列出未读通知。

    Args:
        user_id: 用户 ID
        limit: 返回条数上限(1-100)

    Returns:
        通知字典列表(按 created_at 倒序)
    """
    store = get_cache_store()
    if not store._db:
        return []
    limit = max(1, min(100, int(limit)))
    try:
        cursor = await store._db.execute(
            """SELECT id, user_id, type, payload, is_read, created_at
               FROM notifications WHERE user_id = ? AND is_read = 0
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "type": r[2],
                "payload": _safe_json_loads(r[3]),
                "is_read": bool(r[4]), "created_at": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[notifications] list_unread 失败: {e}")
        return []


async def list_all(user_id: int, page: int = 1, page_size: int = 20) -> dict:
    """分页列出所有通知。

    Args:
        user_id: 用户 ID
        page: 页码(从 1 开始)
        page_size: 每页条数(1-100)

    Returns:
        {items, total, page, page_size, total_pages}
    """
    store = get_cache_store()
    default = {
        "items": [], "total": 0,
        "page": page, "page_size": page_size, "total_pages": 0,
    }
    if not store._db:
        return default
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    try:
        # 总数
        c_cursor = await store._db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        c_row = await c_cursor.fetchone()
        total = int(c_row[0]) if c_row else 0
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        offset = (page - 1) * page_size
        cursor = await store._db.execute(
            """SELECT id, user_id, type, payload, is_read, created_at
               FROM notifications WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (user_id, page_size, offset),
        )
        rows = await cursor.fetchall()
        items = [
            {
                "id": r[0], "user_id": r[1], "type": r[2],
                "payload": _safe_json_loads(r[3]),
                "is_read": bool(r[4]), "created_at": r[5],
            }
            for r in rows
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    except Exception as e:
        logger.warning(f"[notifications] list_all 失败: {e}")
        return default


async def broadcast(notif_type: str, payload: dict,
                    user_ids: list[int] | None = None) -> int:
    """批量发送给多个用户(None=所有用户),返回发送数量。

    Args:
        notif_type: 通知类型
        payload: 通知载荷
        user_ids: 目标用户 ID 列表(None=所有未封禁用户)

    Returns:
        实际发送数量
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    payload_json = _safe_json_dumps(payload)
    try:
        if user_ids is None:
            # 所有未封禁用户(从 users_local 拉取)
            cursor = await store._db.execute(
                """SELECT user_id FROM users_local
                   WHERE (is_banned = 0 OR is_banned IS NULL)
                     AND deleted_at IS NULL"""
            )
            rows = await cursor.fetchall()
            user_ids = [r[0] for r in rows if r[0]]
        if not user_ids:
            return 0
        sent = 0
        # R40 P0-5: 批量写入 + dirty_outbox 同事务(任一失败回滚整批)
        async with store.transaction() as tx:
            for uid in user_ids:
                try:
                    cur = await tx.execute(
                        """INSERT INTO notifications
                           (user_id, type, payload, is_read, created_at)
                           VALUES (?, ?, ?, 0, ?)""",
                        (uid, notif_type, payload_json, now),
                    )
                    if cur and cur.lastrowid:
                        nid = int(cur.lastrowid)
                        await store.add_dirty_outbox(
                            "notifications", str(nid), connection=tx,
                        )
                        sent += 1
                except Exception as inner_e:
                    logger.warning(
                        f"[notifications] broadcast 单条失败 user_id={uid}: {inner_e}"
                    )
        logger.info(
            f"[notifications] broadcast 发送 {sent}/{len(user_ids)} type={notif_type}"
        )
        return sent
    except Exception as e:
        logger.warning(f"[notifications] broadcast 失败: {e}")
        return 0


async def format_notification(notif: dict) -> str:
    """格式化通知为用户可读文本(含图标 + 操作建议)。

    Args:
        notif: send / list_unread / list_all 返回的通知字典

    Returns:
        多行纯文本(避免 Telegram markdown 解析问题)
    """
    if not notif:
        return "通知不存在"
    ntype = notif.get("type", "") or ""
    icon = _NOTIF_ICONS.get(ntype, "📨")
    payload = notif.get("payload")
    if not isinstance(payload, dict):
        payload = _safe_json_loads(payload) or {}
    # 按类型构造文案
    if ntype == NOTIF_TYPE_READY:
        file_code = payload.get("file_code", "")
        return (
            f"{icon} 文件就绪\n"
            f"文件码: {file_code}\n"
            f"可使用 /get {file_code} 取件"
        )
    if ntype == NOTIF_TYPE_R100_DELAY:
        delay = payload.get("delay_minutes", 0)
        return (
            f"{icon} R100 副本延迟\n"
            f"预计 {delay} 分钟后完成\n"
            f"请稍后再试"
        )
    if ntype == NOTIF_TYPE_REPLICA_SHORT:
        ready = payload.get("ready_count", 0)
        total = payload.get("total_count", 0)
        return (
            f"{icon} 副本不足\n"
            f"仅 {ready}/{total} 副本就绪\n"
            f"系统正在补充副本"
        )
    if ntype == NOTIF_TYPE_EXPIRY_WARNING:
        file_code = payload.get("file_code", "")
        hours = payload.get("hours_remaining", 24)
        return (
            f"{icon} 过期前提醒\n"
            f"文件 {file_code} 将在 {hours} 小时后过期\n"
            f"如需保留,请及时续期"
        )
    if ntype == NOTIF_TYPE_RECOVERY:
        count = payload.get("recovered_count", 0)
        return (
            f"{icon} 恢复完成\n"
            f"{count} 个文件已修复\n"
            f"可正常使用文件码取件"
        )
    if ntype == NOTIF_TYPE_TAKEDOWN:
        target = payload.get("target_code", "")
        reason = payload.get("reason", "")
        if reason:
            return (
                f"{icon} 内容下架\n"
                f"文件 {target} 已被下架\n"
                f"原因: {reason}"
            )
        return (
            f"{icon} 内容下架\n"
            f"文件 {target} 已被下架"
        )
    if ntype == NOTIF_TYPE_BAN:
        reason = payload.get("reason", "")
        if reason:
            return (
                f"{icon} 账号封禁\n"
                f"原因: {reason}"
            )
        return f"{icon} 您的账号已被封禁"
    # 未知类型,展示原始内容(截断避免消息过长)
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)[:200]
    return f"{icon} 通知\n类型: {ntype}\n内容: {payload_str}"
