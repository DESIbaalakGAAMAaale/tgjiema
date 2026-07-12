"""R40 §9.3: Repair Console — Outbox/DLQ/Replication/Relay 修复控制台。

职责:
    提供统一的管理员修复入口,集中处理以下四类故障:
    1. dirty_outbox 未处理/死信记录(标记重试或跳过)
    2. DLQ 死信队列(Redis WRITER_DEAD_STREAM_KEY 或本地 dead_letter.jsonl)
    3. replication_tasks 失败任务(重置 attempts 重新调度)
    4. relay 账号风险(banned/restricted/flood_wait 状态修复)

设计原则:
    - 纯函数式 + async,不持有可变状态
    - 通过 database.cache_store.get_cache_store() 获取单例
    - 写操作后调用 store.add_dirty_outbox(table_name, pk) 确保跨机同步
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store


# ─── 1. Outbox 修复 ────────────────────────────────────────────

async def list_outbox(status: str = "unprocessed", page: int = 1,
                     page_size: int = 50) -> dict:
    """列出 dirty_outbox 记录。

    Args:
        status: 状态过滤
            - "unprocessed": processed=0(默认,待同步)
            - "processed":   processed=1(已同步)
            - "dead":         processed=0 且 created_at 距今超过 1 小时(疑似卡死)
            - "failed":       兼容别名,等价于 dead
        page: 页码(从 1 开始)
        page_size: 每页数量

    Returns:
        {items: list[dict], total: int, page: int, page_size: int}
    """
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    # 构造 WHERE 条件
    where_clause = ""
    params: list[Any] = []
    if status == "unprocessed":
        where_clause = "WHERE processed = 0"
    elif status == "processed":
        where_clause = "WHERE processed = 1"
    elif status in ("dead", "failed"):
        # 卡死:未处理且超过 1 小时
        import datetime as _dt
        threshold = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        where_clause = "WHERE processed = 0 AND created_at < ?"
        params.append(threshold)
    # status == "all" 时不加 WHERE

    try:
        # 统计总数
        count_sql = f"SELECT COUNT(*) FROM dirty_outbox {where_clause}"
        count_rows = await store._db.execute_fetchall(count_sql, tuple(params))
        total = count_rows[0][0] if count_rows else 0

        # 分页查询
        offset = max((page - 1) * page_size, 0)
        query_sql = (
            f"SELECT id, table_name, pk, version, operation, payload, "
            f"created_at, processed FROM dirty_outbox {where_clause} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        query_params = list(params) + [page_size, offset]
        rows = await store._db.execute_fetchall(query_sql, tuple(query_params))

        items = []
        for row in rows:
            items.append({
                "id": row[0],
                "table_name": row[1],
                "pk": row[2],
                "version": row[3],
                "operation": row[4],
                "payload": row[5],
                "created_at": row[6],
                "processed": bool(row[7]),
            })

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"[RepairConsole] list_outbox 失败(status={status}): {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def retry_outbox(ids: list[int]) -> int:
    """重试 outbox 记录(标记为未处理)。

    Args:
        ids: dirty_outbox.id 列表

    Returns:
        成功重置的记录数
    """
    if not ids:
        return 0
    store = get_cache_store()
    if not store._db:
        return 0

    placeholders = ",".join("?" * len(ids))
    try:
        cursor = await store._db.execute(
            f"UPDATE dirty_outbox SET processed = 0 WHERE id IN ({placeholders})",
            tuple(ids),
        )
        await store._db.commit()
        affected = cursor.rowcount if cursor else 0
        logger.info(f"[RepairConsole] retry_outbox 重置 {affected} 条记录(ids={ids})")
        return affected
    except Exception as e:
        logger.error(f"[RepairConsole] retry_outbox 失败: {e}")
        return 0


async def skip_outbox(ids: list[int], reason: str = "") -> int:
    """跳过 outbox 记录(标记为已处理 + 写 audit_log)。

    Args:
        ids: dirty_outbox.id 列表
        reason: 跳过原因(记入 audit_log.details)

    Returns:
        成功跳过的记录数
    """
    if not ids:
        return 0
    store = get_cache_store()
    if not store._db:
        return 0

    placeholders = ",".join("?" * len(ids))
    try:
        cursor = await store._db.execute(
            f"UPDATE dirty_outbox SET processed = 1 WHERE id IN ({placeholders})",
            tuple(ids),
        )
        await store._db.commit()
        affected = cursor.rowcount if cursor else 0

        # 写 audit_log
        import datetime as _dt
        await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, 'admin', 'skip_outbox', 'dirty_outbox', ?, ?, '', ?)""",
            (0, json.dumps(ids), reason, _dt.datetime.now().isoformat()),
        )
        await store._db.commit()

        logger.info(
            f"[RepairConsole] skip_outbox 跳过 {affected} 条记录(ids={ids}, reason={reason})"
        )
        return affected
    except Exception as e:
        logger.error(f"[RepairConsole] skip_outbox 失败: {e}")
        return 0


# ─── 2. DLQ 死信队列修复 ────────────────────────────────────────

async def list_dlq(page: int = 1, page_size: int = 50) -> dict:
    """列出死信队列。

    优先从 Redis WRITER_DEAD_STREAM_KEY 读取;Redis 不可达时降级读本地
    data/dead_letter.jsonl 文件。返回所有死信消息,可按 page/page_size 分页。

    注意: 原任务描述提到"查询 writer_inbox 表中 permanent=True 的记录",
    但 writer_inbox 表无 permanent 列(仅用于幂等去重)。实际的 permanent 标记
    存储在死信消息体中(attempts >= max_attempts 或 next_retry_at=None),
    因此本函数从正确的数据源(Redis 死信 Stream / 本地降级文件)读取。

    Args:
        page: 页码(从 1 开始)
        page_size: 每页数量

    Returns:
        {items: list[dict], total: int, page: int, page_size: int}
        items 中每项含: message_id, reason, attempts, max_attempts, failed_at,
                        permanent(bool), original(dict), next_retry_at
    """
    items: list[dict] = []

    # 1. 优先从 Redis 死信 Stream 读取
    try:
        from database import redis_queue
        dead_messages = await redis_queue.get_dead_messages(count=1000)
        for msg_id, dead_msg in dead_messages:
            attempts = int(dead_msg.get("attempts", 0))
            max_attempts = int(dead_msg.get("max_attempts", 3))
            next_retry = dead_msg.get("next_retry_at")
            # permanent 判定: 显式标记或达到上限或无重试时间
            is_permanent = (
                attempts >= max_attempts or next_retry is None
            )
            items.append({
                "message_id": msg_id,
                "source": "redis",
                "reason": dead_msg.get("reason", ""),
                "attempts": attempts,
                "max_attempts": max_attempts,
                "failed_at": dead_msg.get("failed_at"),
                "next_retry_at": next_retry,
                "permanent": is_permanent,
                "original": dead_msg.get("original"),
            })
    except Exception as e:
        logger.debug(f"[RepairConsole] Redis 死信 Stream 读取失败,降级本地文件: {e}")

    # 2. Redis 不可达或为空时,补充读取本地 dead_letter.jsonl
    if not items:
        try:
            dead_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "dead_letter.jsonl"
            )
            if os.path.exists(dead_file):
                with open(dead_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            dead_msg = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        attempts = int(dead_msg.get("attempts", 0))
                        max_attempts = int(dead_msg.get("max_attempts", 3))
                        next_retry = dead_msg.get("next_retry_at")
                        is_permanent = (
                            attempts >= max_attempts or next_retry is None
                        )
                        items.append({
                            "message_id": dead_msg.get("message_id", ""),
                            "source": "file",
                            "reason": dead_msg.get("reason", ""),
                            "attempts": attempts,
                            "max_attempts": max_attempts,
                            "failed_at": dead_msg.get("failed_at"),
                            "next_retry_at": next_retry,
                            "permanent": is_permanent,
                            "original": dead_msg.get("original"),
                        })
        except Exception as e:
            logger.warning(f"[RepairConsole] 读取 dead_letter.jsonl 失败: {e}")

    # 分页
    total = len(items)
    offset = max((page - 1) * page_size, 0)
    paged_items = items[offset:offset + page_size]
    return {
        "items": paged_items, "total": total,
        "page": page, "page_size": page_size,
    }


async def replay_dlq(ids: list[int]) -> int:
    """重放死信(重新入队主 Stream)。

    Args:
        ids: 死信消息 ID 列表(字符串形式,如 Redis Stream 的 msg_id)

    Returns:
        成功重放的死信数量
    """
    if not ids:
        return 0

    # ids 在 list_dlq 中是字符串形式的 message_id(Redis XID 或文件中的 message_id)
    # 此处接受 int 输入但内部转字符串,以兼容 list_dlq 的字符串 ID
    str_ids = [str(i) for i in ids]

    success = 0
    try:
        from database import redis_queue
        # 拉取所有死信消息,匹配指定 msg_id
        all_dead = await redis_queue.get_dead_messages(count=1000)
        matched = [(mid, msg) for mid, msg in all_dead if mid in str_ids]
        for msg_id, dead_msg in matched:
            original = dead_msg.get("original")
            if not isinstance(original, dict):
                logger.warning(
                    f"[RepairConsole] replay_dlq 跳过非 dict 死信: msg_id={msg_id}"
                )
                continue
            # 原子重入主 Stream 并从死信 Stream 删除
            ok = await redis_queue.requeue_from_dlq(msg_id, original)
            if ok:
                success += 1
                logger.info(
                    f"[RepairConsole] replay_dlq 重放成功: msg_id={msg_id}"
                )
            else:
                logger.warning(
                    f"[RepairConsole] replay_dlq 重放失败(原子重入): msg_id={msg_id}"
                )
    except Exception as e:
        logger.error(f"[RepairConsole] replay_dlq 失败: {e}")

    return success


# ─── 3. Replication Tasks 修复 ──────────────────────────────────

async def list_replication_failures(page: int = 1, page_size: int = 50) -> dict:
    """列出副本复制失败任务。

    查询 replication_tasks 表中 status='FAILED' 或 attempts >= max_attempts 的记录。

    Returns:
        {items: list[dict], total: int, page: int, page_size: int}
    """
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    try:
        where_clause = (
            "WHERE status = 'FAILED' OR attempts >= max_attempts"
        )
        count_sql = f"SELECT COUNT(*) FROM replication_tasks {where_clause}"
        count_rows = await store._db.execute_fetchall(count_sql)
        total = count_rows[0][0] if count_rows else 0

        offset = max((page - 1) * page_size, 0)
        query_sql = (
            f"SELECT task_id, group_id, file_unique_id, src_channel_id, "
            f"dst_channel_id, src_msg_id, dst_msg_id, media_group_id, "
            f"task_type, priority, status, prev_status, attempts, "
            f"max_attempts, next_retry_at, last_error, created_at, updated_at "
            f"FROM replication_tasks {where_clause} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        )
        rows = await store._db.execute_fetchall(
            query_sql, (page_size, offset)
        )

        items = []
        for r in rows:
            items.append({
                "task_id": r[0], "group_id": r[1], "file_unique_id": r[2],
                "src_channel_id": r[3], "dst_channel_id": r[4],
                "src_msg_id": r[5], "dst_msg_id": r[6],
                "media_group_id": r[7], "task_type": r[8],
                "priority": r[9], "status": r[10], "prev_status": r[11],
                "attempts": r[12], "max_attempts": r[13],
                "next_retry_at": r[14], "last_error": r[15],
                "created_at": r[16], "updated_at": r[17],
            })

        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"[RepairConsole] list_replication_failures 失败: {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def retry_replication(task_ids: list[int]) -> int:
    """重试副本复制任务(重置 attempts,标记为 PLANNED)。

    Args:
        task_ids: replication_tasks.task_id 列表

    Returns:
        成功重置的任务数
    """
    if not task_ids:
        return 0
    store = get_cache_store()
    if not store._db:
        return 0

    import time as _time
    placeholders = ",".join("?" * len(task_ids))
    try:
        cursor = await store._db.execute(
            f"""UPDATE replication_tasks
                SET status = 'PLANNED', prev_status = status,
                    attempts = 0, next_retry_at = ?, last_error = '',
                    updated_at = ?
                WHERE task_id IN ({placeholders})""",
            (_time.time(), _time.time(), *task_ids),
        )
        await store._db.commit()
        affected = cursor.rowcount if cursor else 0

        # 写 dirty_outbox 同步到 CRDB
        for tid in task_ids:
            await store.add_dirty_outbox("replication_tasks", str(tid))

        logger.info(
            f"[RepairConsole] retry_replication 重置 {affected} 个任务(ids={task_ids})"
        )
        return affected
    except Exception as e:
        logger.error(f"[RepairConsole] retry_replication 失败: {e}")
        return 0


# ─── 4. Relay 账号修复 ──────────────────────────────────────────

async def list_relay_issues() -> dict:
    """列出 relay 池问题账号。

    查询 relay 账号状态: banned/restricted/flood_wait/unknown。

    Returns:
        {issues: list[dict], total: int, by_status: dict}
        issues 中每项含: id, phone, status, status_info, is_active,
                         status_updated_at, last_login_at
    """
    try:
        from database.relay_db import get_relay_db
        relay_db = await get_relay_db()

        # 查询所有非正常状态账号
        rows = await relay_db._db.execute_fetchall(
            """SELECT id, phone, status, status_info, is_active,
                      status_updated_at, last_login_at
               FROM relay_accounts
               WHERE status NOT IN ('ok', 'active', 'unknown', 'deleted')
                  OR (status = 'unknown' AND is_active = 1)
               ORDER BY status_updated_at DESC"""
        )

        issues = []
        by_status: dict[str, int] = {}
        for r in rows:
            status = r[2] or "unknown"
            by_status[status] = by_status.get(status, 0) + 1
            issues.append({
                "id": r[0], "phone": r[1], "status": status,
                "status_info": r[3] or "",
                "is_active": bool(r[4]),
                "status_updated_at": r[5],
                "last_login_at": r[6],
            })

        return {
            "issues": issues, "total": len(issues),
            "by_status": by_status,
        }
    except Exception as e:
        logger.error(f"[RepairConsole] list_relay_issues 失败: {e}")
        return {"issues": [], "total": 0, "by_status": {}}


async def repair_relay(account_id: int) -> bool:
    """修复 relay 账号(重置状态为 unknown + 标记为 active)。

    Args:
        account_id: relay_accounts.id

    Returns:
        True 修复成功, False 失败
    """
    try:
        from database.relay_db import get_relay_db
        relay_db = await get_relay_db()

        import datetime as _dt
        now_iso = _dt.datetime.now().isoformat()
        cursor = await relay_db._db.execute(
            """UPDATE relay_accounts
               SET status = 'unknown', status_info = '',
                   is_active = 1, status_updated_at = ?
               WHERE id = ?""",
            (now_iso, account_id),
        )
        await relay_db._db.commit()
        affected = cursor.rowcount if cursor else 0

        if affected > 0:
            logger.info(
                f"[RepairConsole] repair_relay 重置账号 id={account_id} → unknown/active"
            )
            return True
        return False
    except Exception as e:
        logger.error(f"[RepairConsole] repair_relay 失败(id={account_id}): {e}")
        return False


# ─── 5. 总览 ────────────────────────────────────────────────────

async def get_repair_overview() -> dict:
    """获取修复控制台总览。

    Returns:
        {outbox_unprocessed, outbox_dead, dlq_count,
         replication_failed, relay_issues}
    """
    store = get_cache_store()

    # outbox 计数
    outbox_unprocessed = 0
    outbox_dead = 0
    if store._db:
        try:
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0"
            )
            outbox_unprocessed = rows[0][0] if rows else 0

            import datetime as _dt
            threshold = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0 AND created_at < ?",
                (threshold,),
            )
            outbox_dead = rows[0][0] if rows else 0
        except Exception as e:
            logger.debug(f"[RepairConsole] get_repair_overview outbox 统计失败: {e}")

    # DLQ 计数
    dlq_count = 0
    try:
        from database import redis_queue
        dlq_count = await redis_queue.get_dlq_length()
    except Exception as e:
        logger.debug(f"[RepairConsole] get_repair_overview DLQ 长度获取失败: {e}")

    # 复制失败任务计数
    replication_failed = 0
    if store._db:
        try:
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM replication_tasks "
                "WHERE status = 'FAILED' OR attempts >= max_attempts"
            )
            replication_failed = rows[0][0] if rows else 0
        except Exception as e:
            logger.debug(f"[RepairConsole] get_repair_overview replication 统计失败: {e}")

    # Relay 问题账号计数
    relay_issues_data = await list_relay_issues()
    relay_issues = relay_issues_data.get("total", 0)

    return {
        "outbox_unprocessed": outbox_unprocessed,
        "outbox_dead": outbox_dead,
        "dlq_count": dlq_count,
        "replication_failed": replication_failed,
        "relay_issues": relay_issues,
    }
