"""R40 §9.1.4 + R45 第 16 节: 可靠通知 — 跨 Bot 推送给用户。

为系统事件(文件就绪、R100 延迟、副本不足、过期前提醒、恢复完成等)
提供可靠通知能力,通知先落库,由各 Bot 异步推送。

R45 第 16 节 Notifications 整改:
- notification_outbox 模式:通知先写 outbox(pending),由各 Bot 异步投递,
  投递成功后写 delivery_receipt 并将 outbox 状态置为 delivered
- delivery_receipt:记录每条通知的实际投递结果(channel/bot/delivered_at),
  支持失败重试(outbox 中 status=failed 的记录可被重新处理)
- dedup_key 已在 R41 P1-12 实现(dispatch_notification 基于 dedup_key
  1 小时窗口去重),R45 在 outbox 模式下也保留 dedup_key 字段,
  避免 outbox 层重复入队

设计要点:
- payload 存 JSON 字符串(如 {"file_code": "ABC123", "delay_minutes": 30})
- format_notification 按类型返回不同文案(含图标 + 操作建议)
- 通过 get_cache_store() 获取 CacheStore 单例
- 每次写入后调用 add_dirty_outbox(table_name="notifications", pk=str(notif_id))
- R45:notification_outbox / notification_receipts 表通过幂等 CREATE TABLE IF NOT EXISTS
  在 _ensure_outbox_schema() 中创建(避免修改 cache_store.py)
  注:使用 notification_receipts 而非 delivery_receipts,因为后者已被
  cache_store.py M1-4 投递回执表占用(不同 schema,无 notif_id 列)
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

# R45:outbox 状态常量
OUTBOX_STATUS_PENDING = "pending"      # 待投递
OUTBOX_STATUS_DELIVERED = "delivered"  # 已投递成功
OUTBOX_STATUS_FAILED = "failed"        # 投递失败(可重试)
OUTBOX_STATUS_SKIPPED = "skipped"      # 跳过(如 dedup 命中)

# R45:outbox schema 是否已初始化(进程级标记,避免每次调用都尝试 CREATE)
_outbox_schema_initialized: bool = False


async def _ensure_outbox_schema() -> bool:
    """R45 第 16 节: 幂等创建 notification_outbox / notification_receipts 表。

    由于 cache_store.py 不在本任务文件范围内,通过模块级幂等 CREATE TABLE
    在首次访问时创建所需表。多次调用安全(已创建则忽略错误)。

    注:使用 notification_receipts 而非 delivery_receipts,因后者已被
        cache_store.py M1-4 投递回执表占用(不同 schema,无 notif_id 列)。

    表结构:
        notification_outbox:
            - id INTEGER PRIMARY KEY AUTOINCREMENT
            - notif_id INTEGER NOT NULL     # 关联 notifications.id
            - user_id INTEGER NOT NULL
            - notif_type TEXT NOT NULL
            - dedup_key TEXT DEFAULT ''       # 去重键(可空)
            - window_start TEXT               # R51 P0-5: 去重窗口起始时间(整点对齐)
            - payload TEXT                    # 通知内容快照(JSON)
            - status TEXT DEFAULT 'pending'   # pending/delivered/failed/skipped
            - attempts INTEGER DEFAULT 0      # 投递尝试次数
            - max_attempts INTEGER DEFAULT 3
            - last_error TEXT DEFAULT ''
            - created_at TEXT
            - delivered_at TEXT               # 投递成功时间
            - updated_at TEXT
        notification_receipts:
            - id INTEGER PRIMARY KEY AUTOINCREMENT
            - notif_id INTEGER NOT NULL
            - outbox_id INTEGER               # 关联 notification_outbox.id(可空)
            - user_id INTEGER NOT NULL
            - channel TEXT                    # 投递渠道(如 telegram/dsp_bot)
            - status TEXT NOT NULL            # delivered/failed
            - error TEXT DEFAULT ''
            - delivered_at TEXT
            - created_at TEXT
    """
    global _outbox_schema_initialized
    if _outbox_schema_initialized:
        return True
    store = get_cache_store()
    if not store._db:
        return False
    try:
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS notification_outbox (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                notif_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                notif_type   TEXT NOT NULL,
                dedup_key    TEXT DEFAULT '',
                window_start TEXT,
                payload      TEXT,
                status       TEXT DEFAULT 'pending',
                attempts     INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error   TEXT DEFAULT '',
                created_at   TEXT,
                delivered_at TEXT,
                updated_at   TEXT
            )"""
        )
        # R51 P0-5: 旧库补 window_start 列(幂等,已存在则忽略)
        try:
            await store._db.execute(
                "ALTER TABLE notification_outbox ADD COLUMN window_start TEXT"
            )
        except Exception:
            pass  # 列已存在,忽略
        await store._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notif_outbox_status "
            "ON notification_outbox(status, created_at)"
        )
        await store._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notif_outbox_user "
            "ON notification_outbox(user_id, status)"
        )
        # R51 P0-5: (user_id, dedup_key, window_start) 唯一约束 — 防并发重复投递
        await store._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedup "
            "ON notification_outbox(user_id, dedup_key, window_start) "
            "WHERE dedup_key IS NOT NULL AND dedup_key != ''"
        )
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS notification_receipts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                notif_id     INTEGER NOT NULL,
                outbox_id    INTEGER,
                user_id      INTEGER NOT NULL,
                channel      TEXT,
                status       TEXT NOT NULL,
                error        TEXT DEFAULT '',
                delivered_at TEXT,
                created_at   TEXT
            )"""
        )
        await store._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_receipts_notif "
            "ON notification_receipts(notif_id)"
        )
        await store._db.commit()
        _outbox_schema_initialized = True
        logger.debug("[notifications] outbox schema 初始化完成")
        return True
    except Exception as e:
        logger.warning(f"[notifications] _ensure_outbox_schema 失败(忽略): {e}")
        return False


def _reset_outbox_schema_for_test() -> None:
    """测试辅助函数:重置 schema 初始化标记(用于测试隔离)。"""
    global _outbox_schema_initialized
    _outbox_schema_initialized = False


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


def _compute_window_start(now: _dt.datetime | None = None) -> str:
    """R51 P0-5: 计算去重窗口起始时间(整点对齐,1 小时窗口)。

    将当前时间向下取整到整点,作为 dedup 窗口的起始时间。
    同一小时内多次调用返回相同的 window_start,配合唯一约束防并发重复。

    Args:
        now: 当前时间(可选,默认 datetime.now())

    Returns:
        整点对齐的 ISO 格式时间字符串
    """
    dt = now or _dt.datetime.now()
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


async def send(
    user_id: int,
    notif_type: str,
    payload: dict,
    *,
    persist_only: bool = False,
) -> int:
    """发送通知给用户,返回 notif_id。

    R45 第 16 节: 同事务写入 notification_outbox(pending),供各 Bot 异步投递。
    R51 P0-5: outbox 写入失败必须抛出并回滚整个 transaction(避免孤儿通知)。
              仅历史型通知可设 persist_only=True 跳过 outbox。

    Args:
        user_id: 用户 ID
        notif_type: 通知类型(NOTIF_TYPE_*)
        payload: 通知载荷(JSON 序列化存储)
        persist_only: True=仅写 notifications 表(历史型通知,不投递);
                      False=同事务写 notifications + notification_outbox(默认,可投递)

    Returns:
        notif_id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[notifications] CacheStore 未初始化")
        return 0
    now = _dt.datetime.now().isoformat()
    payload_json = _safe_json_dumps(payload)
    # R45: 预先确保 outbox 表存在(幂等)
    await _ensure_outbox_schema()
    # 提取 dedup_key(若 payload 中包含,用于 outbox 去重)
    dedup_key = ""
    if isinstance(payload, dict):
        dedup_key = str(payload.get("_dedup_key", "")) or ""
    # R51 P0-5: 计算 dedup 窗口起始时间(仅当有 dedup_key 时)
    window_start = _compute_window_start() if dedup_key else None
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
                # R51 P0-5: persist_only=True 时跳过 outbox(仅历史型通知)
                if persist_only:
                    logger.info(
                        f"[notifications] persist_only=True 跳过 outbox "
                        f"notif_id={notif_id} user_id={user_id}"
                    )
                else:
                    # R45/R51 P0-5: 同事务写入 notification_outbox(pending)
                    # outbox 写入失败必须抛出 → 触发 transaction rollback(避免孤儿)
                    await tx.execute(
                        """INSERT INTO notification_outbox
                           (notif_id, user_id, notif_type, dedup_key, window_start,
                            payload, status, attempts, max_attempts, last_error,
                            created_at, delivered_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 3, '', ?, NULL, ?)""",
                        (notif_id, user_id, notif_type, dedup_key, window_start,
                         payload_json, now, now),
                    )
                    await store.add_dirty_outbox(
                        "notification_outbox", str(notif_id), connection=tx,
                    )
        return notif_id
    except Exception as e:
        # R51 P0-5: outbox 写入失败已触发 transaction rollback,无孤儿状态
        # 使用 ErrorCodes 协议化错误码记录日志(便于审计追踪)
        from services.error_codes import ErrorCodes
        # R51 P0-5: 区分唯一约束冲突(并发重复)与其他写入失败
        err_msg = str(e).lower()
        if "unique" in err_msg or "constraint" in err_msg:
            logger.info(
                f"[notifications] send 去重命中 code={ErrorCodes.NOTIFICATION_OUTBOX_DUPLICATE} "
                f"user_id={user_id} dedup_key={dedup_key}: {e}"
            )
        else:
            logger.warning(
                f"[notifications] send 失败 code={ErrorCodes.NOTIFICATION_OUTBOX_WRITE_FAILED} "
                f"user_id={user_id} type={notif_type}: {e}"
            )
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


# R41 P1-12: 幂等通知投递 — 同一 dedup_key 1 小时内不重复投递
# 用于 TaskCenter / 内容安全等模块在事件触发后通知用户,
# 避免短时间内重复触发同一通知(如重试任务多次完成)。
_NOTIF_DEDUP_TTL_SECONDS = 3600  # 去重窗口 1 小时


async def dispatch_notification(
    user_id: int,
    type: str,
    content: dict,
    dedup_key: str = "",
) -> int:
    """R41 P1-12: 幂等通知投递(基于 dedup_key 1 小时去重)。

    与 ``send()`` 的区别:
        - ``send()`` 无去重,每次调用都会插入新通知
        - ``dispatch_notification()`` 基于 dedup_key 去重:
          同一 dedup_key 在最近 1 小时内已投递过 → 跳过(返回 0)
          未投递 → 写入通知并记录 dedup_key + 投递时间

    Args:
        user_id: 用户 ID
        type: 通知类型(NOTIF_TYPE_*)
        content: 通知内容(等同于 send() 的 payload)
        dedup_key: 去重键(为空时不进行去重,等同 send())

    Returns:
        notif_id(>0 投递成功);0 表示因去重跳过或失败

    Example:
        # TaskCenter 完成任务后触发通知(同 dedup_key 1 小时内不重复)
        await notifications.dispatch_notification(
            user_id=12345,
            type="ready",
            content={"file_code": "ABC123"},
            dedup_key=f"task_complete:{task_id}",
        )
    """
    if not dedup_key:
        # 无 dedup_key,直接调用 send()(不进行去重)
        return await send(user_id, type, content)

    store = get_cache_store()
    if not store._db:
        logger.warning("[notifications] dispatch_notification CacheStore 未初始化")
        return 0

    # 计算去重窗口起始时间(1 小时前)
    now_dt = _dt.datetime.now()
    window_start_iso = (now_dt - _dt.timedelta(seconds=_NOTIF_DEDUP_TTL_SECONDS)).isoformat()

    # 查询 notifications 表中同一 dedup_key + user_id 是否在窗口内已存在
    # 注:dedup_key 存储在 payload 中(避免修改 schema),格式: {"_dedup_key": "..."}
    try:
        cursor = await store._db.execute(
            """SELECT id FROM notifications
               WHERE user_id = ?
                 AND payload LIKE ?
                 AND created_at >= ?
               ORDER BY id DESC LIMIT 1""",
            (user_id, f'%"_dedup_key": "{dedup_key}"%', window_start_iso),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            # 已在窗口内投递过,跳过(幂等去重)
            logger.info(
                f"[notifications] dispatch_notification 去重命中 "
                f"user_id={user_id} dedup_key={dedup_key} existing_id={row[0]}"
            )
            return 0
    except Exception as e:
        # 查询失败时降级为直接发送(fail-open,不阻塞业务)
        logger.warning(
            f"[notifications] dispatch_notification 去重查询失败,降级为 send(): {e}"
        )
        return await send(user_id, type, content)

    # 注入 dedup_key 到 content,以便后续去重查询
    enriched_content = dict(content) if content else {}
    enriched_content["_dedup_key"] = dedup_key
    enriched_content["_dedup_window"] = _NOTIF_DEDUP_TTL_SECONDS

    # 调用 send() 写入通知(同事务模式)
    notif_id = await send(user_id, type, enriched_content)
    if notif_id:
        logger.info(
            f"[notifications] dispatch_notification 投递成功 "
            f"id={notif_id} user_id={user_id} type={type} dedup_key={dedup_key}"
        )
    return notif_id


# ─── R45 第 16 节: notification_outbox + delivery_receipt 管理 ───


async def record_notification_receipt(
    notif_id: int,
    user_id: int,
    channel: str,
    status: str,
    error: str = "",
    outbox_id: int | None = None,
) -> int:
    """R45 第 16 节: 记录通知投递回执(delivery_receipt)。

    各 Bot 在实际投递后(无论成功/失败)调用本函数:
        - 成功投递:status='delivered',error='',写 delivered_at
        - 投递失败:status='failed',error=失败原因,触发后续重试

    同时联动 notification_outbox 表:
        - status='delivered' → outbox.status='delivered' + delivered_at
        - status='failed'    → outbox.status='failed' + attempts += 1 + last_error
          (attempts 达到 max_attempts 时不再重试,标记为 skipped)

    Args:
        notif_id: notifications.id
        user_id: 用户 ID
        channel: 投递渠道(如 "telegram" / "dsp_bot" / "admin_bot")
        status: delivered / failed
        error: 失败原因(delivered 时为空)
        outbox_id: notification_outbox.id(可选,无则按 notif_id 查找最新 pending/failed)

    Returns:
        receipt_id(>0 成功);0 失败
    """
    store = get_cache_store()
    if not store._db:
        return 0
    await _ensure_outbox_schema()
    now = _dt.datetime.now().isoformat()
    try:
        async with store.transaction() as tx:
            # 1. 写 notification_receipts
            cursor = await tx.execute(
                """INSERT INTO notification_receipts
                   (notif_id, outbox_id, user_id, channel, status,
                    error, delivered_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (notif_id, outbox_id, user_id, channel, status,
                 error, now if status == "delivered" else None, now),
            )
            receipt_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            # 2. 联动 notification_outbox(若未指定 outbox_id,按 notif_id 查找最新)
            if not outbox_id:
                ob_cur = await tx.execute(
                    """SELECT id, attempts, max_attempts FROM notification_outbox
                       WHERE notif_id = ?
                       ORDER BY id DESC LIMIT 1""",
                    (notif_id,),
                )
                ob_row = await ob_cur.fetchone()
                if ob_row:
                    outbox_id = int(ob_row[0])
            if outbox_id:
                if status == "delivered":
                    # R51 P0-5: CAS — 仅当当前状态为 pending/sending/failed 时才更新
                    # 若 affected_rows=0,说明已被其他 worker 处理为终态(delivered/skipped),跳过
                    cas_cur = await tx.execute(
                        """UPDATE notification_outbox
                           SET status = 'delivered', delivered_at = ?,
                               updated_at = ?, last_error = ''
                           WHERE id = ?
                             AND status IN ('pending', 'sending', 'failed')""",
                        (now, now, outbox_id),
                    )
                    cas_affected = int(cas_cur.rowcount) if cas_cur else 0
                else:
                    # status == 'failed' → 增加 attempts,判断是否超限
                    # R51 P0-5: CAS — 仅当当前状态为 pending/sending/failed 时才更新
                    cas_cur = await tx.execute(
                        """UPDATE notification_outbox
                           SET attempts = attempts + 1,
                               last_error = ?, updated_at = ?,
                               status = CASE
                                   WHEN attempts + 1 >= max_attempts
                                   THEN 'skipped'
                                   ELSE 'failed'
                               END
                           WHERE id = ?
                             AND status IN ('pending', 'sending', 'failed')""",
                        (error[:500], now, outbox_id),
                    )
                    cas_affected = int(cas_cur.rowcount) if cas_cur else 0
                if cas_affected > 0:
                    await store.add_dirty_outbox(
                        "notification_outbox", str(outbox_id), connection=tx,
                    )
                else:
                    logger.info(
                        f"[notifications] CAS 跳过 outbox_id={outbox_id} "
                        f"(已被其他 worker 处理或终态)"
                    )
            # 3. dirty_outbox for notification_receipts
            if receipt_id:
                await store.add_dirty_outbox(
                    "notification_receipts", str(receipt_id), connection=tx,
                )
        logger.info(
            f"[notifications] record_notification_receipt "
            f"notif_id={notif_id} channel={channel} status={status} "
            f"receipt_id={receipt_id}"
        )
        return receipt_id
    except Exception as e:
        logger.warning(f"[notifications] record_notification_receipt 失败: {e}")
        return 0


async def get_pending_outbox(limit: int = 50) -> list[dict]:
    """R45 第 16 节: 获取待投递(pending/failed)的 outbox 记录。

    供各 Bot 拉取需要投递的通知。failed 状态记录可被重新投递(只要未超 max_attempts)。

    Args:
        limit: 最多返回条数(1-200)

    Returns:
        [{id, notif_id, user_id, notif_type, dedup_key, payload,
          status, attempts, max_attempts, last_error,
          created_at, delivered_at, updated_at}, ...]
    """
    store = get_cache_store()
    if not store._db:
        return []
    await _ensure_outbox_schema()
    limit = max(1, min(200, int(limit)))
    try:
        rows = await store._db.execute_fetchall(
            """SELECT id, notif_id, user_id, notif_type, dedup_key, payload,
                      status, attempts, max_attempts, last_error,
                      created_at, delivered_at, updated_at
               FROM notification_outbox
               WHERE status IN ('pending', 'failed')
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        )
        return [
            {
                "id": r[0], "notif_id": r[1], "user_id": r[2],
                "notif_type": r[3], "dedup_key": r[4] or "",
                "payload": _safe_json_loads(r[5]),
                "status": r[6], "attempts": int(r[7] or 0),
                "max_attempts": int(r[8] or 3),
                "last_error": r[9] or "",
                "created_at": r[10], "delivered_at": r[11],
                "updated_at": r[12],
            }
            for r in rows if r
        ]
    except Exception as e:
        logger.warning(f"[notifications] get_pending_outbox 失败: {e}")
        return []


async def mark_outbox_skipped(outbox_id: int, reason: str = "") -> bool:
    """R45 第 16 节: 标记 outbox 记录为跳过(不再投递)。

    用于人工干预或检测到通知已无意义(如用户已注销)时跳过投递。

    Args:
        outbox_id: notification_outbox.id
        reason: 跳过原因(记入 last_error)

    Returns:
        True 成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        async with store.transaction() as tx:
            # R51 P0-5: CAS — 仅当当前状态为 pending/failed 时才标记 skipped
            # 若 affected_rows=0,说明已被其他 worker 处理为终态(delivered/skipped),跳过
            cursor = await tx.execute(
                """UPDATE notification_outbox
                   SET status = 'skipped', last_error = ?, updated_at = ?
                   WHERE id = ?
                     AND status IN ('pending', 'failed')""",
                (reason[:500], now, outbox_id),
            )
            ok = bool(cursor and cursor.rowcount > 0)
            if ok:
                await store.add_dirty_outbox(
                    "notification_outbox", str(outbox_id), connection=tx,
                )
            else:
                logger.info(
                    f"[notifications] mark_outbox_skipped CAS 跳过 "
                    f"outbox_id={outbox_id}(已为终态)"
                )
        return ok
    except Exception as e:
        logger.warning(f"[notifications] mark_outbox_skipped 失败: {e}")
        return False


async def get_outbox_stats() -> dict:
    """R45 第 16 节: 获取 outbox 统计信息(供监控/Prometheus)。

    Returns:
        {pending, delivered, failed, skipped, total,
         oldest_pending_age_seconds, delivery_success_rate}
    """
    store = get_cache_store()
    default = {
        "pending": 0, "delivered": 0, "failed": 0, "skipped": 0,
        "total": 0, "oldest_pending_age_seconds": 0,
        "delivery_success_rate": 0.0,
    }
    if not store._db:
        return default
    await _ensure_outbox_schema()
    try:
        rows = await store._db.execute_fetchall(
            """SELECT status, COUNT(*) FROM notification_outbox
               GROUP BY status"""
        )
        stats = dict(default)
        total = 0
        for r in rows:
            if not r:
                continue
            status, count = r[0], int(r[1])
            if status in stats:
                stats[status] = count
            total += count
        stats["total"] = total
        # 最早的 pending 记录存在时间
        oldest_rows = await store._db.execute_fetchall(
            """SELECT created_at FROM notification_outbox
               WHERE status = 'pending'
               ORDER BY created_at ASC LIMIT 1"""
        )
        if oldest_rows and oldest_rows[0] and oldest_rows[0][0]:
            try:
                oldest_dt = _dt.datetime.fromisoformat(oldest_rows[0][0])
                stats["oldest_pending_age_seconds"] = int(
                    (_dt.datetime.now() - oldest_dt).total_seconds()
                )
            except (ValueError, TypeError):
                pass
        # 投递成功率 = delivered / (delivered + failed + skipped)
        denom = stats["delivered"] + stats["failed"] + stats["skipped"]
        stats["delivery_success_rate"] = (
            round(stats["delivered"] / denom, 4) if denom > 0 else 0.0
        )
        return stats
    except Exception as e:
        logger.warning(f"[notifications] get_outbox_stats 失败: {e}")
        return default
