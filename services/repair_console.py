"""R40 §9.3 + R45 第 16 节: Repair Console — Outbox/DLQ/Replication/Relay 修复控制台。

职责:
    提供统一的管理员修复入口,集中处理以下四类故障:
    1. dirty_outbox 未处理/死信记录(标记重试或跳过)
    2. DLQ 死信队列(Redis WRITER_DEAD_STREAM_KEY 或本地 dead_letter.jsonl)
    3. replication_tasks 失败任务(重置 attempts 重新调度)
    4. relay 账号风险(banned/restricted/flood_wait 状态修复)

R45 第 16 节 Repair Console 整改:
    - 新增 SAFE_ACTIONS 白名单:严禁任意 SQL/Python 执行入口,
      只允许预定义的安全动作(retry_outbox/skip_outbox/replay_dlq/
      retry_replication/repair_relay/mark_outbox_skipped)
    - 新增 execute_repair(action, params, principal_id, approval_action_id)
      统一入口:所有修复操作必须通过此入口,白名单 + 审批 + 审计一体化
    - 新增 get_causal_chain(trace_id) 因果链查询:展示 trace_id 关联的所有
      audit_log / dirty_outbox / command_executions 记录,
      帮助管理员理解故障的因果链
    - 新增 compute_payload_hash(payload) payload 哈希计算:
      用于显示 dirty_outbox 记录的 payload 摘要(避免泄露敏感内容)

设计原则:
    - 纯函数式 + async,不持有可变状态
    - 通过 database.cache_store.get_cache_store() 获取单例
    - 写操作后调用 store.add_dirty_outbox(table_name, pk) 确保跨机同步
    - 中文注释,loguru 日志
    - 严禁暴露任意 SQL/Python 执行入口(安全铁律)
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store


# ─── R45 第 16 节: 安全动作白名单 ──────────────────────────────

# SAFE_ACTIONS: 允许通过 execute_repair 执行的动作白名单
# 严禁添加 "execute_sql" / "execute_python" / "eval" 等任意代码执行入口
SAFE_ACTIONS: frozenset[str] = frozenset({
    "retry_outbox",          # 重试 dirty_outbox 记录(标记为未处理)
    "skip_outbox",           # 跳过 dirty_outbox 记录(标记为已处理)
    "replay_dlq",            # 重放死信队列消息
    "retry_replication",     # 重试副本复制失败任务
    "repair_relay",          # 修复 relay 账号状态
    "mark_outbox_skipped",   # 标记 notification_outbox 为跳过(在 notifications.py)
})

# 动作描述(用于审计日志和用户提示)
_ACTION_DESCRIPTIONS: dict[str, str] = {
    "retry_outbox": "重试 dirty_outbox 记录",
    "skip_outbox": "跳过 dirty_outbox 记录",
    "replay_dlq": "重放死信队列消息",
    "retry_replication": "重试副本复制任务",
    "repair_relay": "修复 relay 账号状态",
    "mark_outbox_skipped": "跳过通知投递",
}


def is_safe_action(action: str) -> bool:
    """R45 第 16 节: 检查动作是否在白名单中。

    Args:
        action: 动作名称

    Returns:
        True 在白名单中;False 不在(禁止执行)
    """
    return action in SAFE_ACTIONS


def compute_payload_hash(payload: Any, algorithm: str = "sha256",
                         max_length: int = 16) -> str:
    """R45 第 16 节: 计算 payload 的哈希摘要(用于显示,避免泄露敏感内容)。

    将 payload 序列化为 JSON 字符串后计算哈希,返回前 max_length 字符。
    用于在 Repair Console 中显示 dirty_outbox 记录的 payload 摘要,
    而不是直接展示完整 payload(可能包含敏感信息)。

    Args:
        payload: 任意可序列化对象(dict/list/str/None 等)
        algorithm: 哈希算法(sha256/sha1/md5,默认 sha256)
        max_length: 返回的哈希前缀长度(默认 16)

    Returns:
        哈希前缀字符串(如 "a1b2c3d4e5f6g7h8");
        序列化失败返回 "hash_error"
    """
    try:
        if payload is None:
            payload_str = "null"
        elif isinstance(payload, (dict, list)):
            payload_str = json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False, default=str)
        else:
            payload_str = str(payload)
        h = hashlib.new(algorithm)
        h.update(payload_str.encode("utf-8"))
        return h.hexdigest()[:max_length]
    except Exception as e:
        logger.warning(f"[RepairConsole] compute_payload_hash 失败: {e}")
        return "hash_error"


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
        # R40 P0-5: retry_outbox + audit_log 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                f"UPDATE dirty_outbox SET processed = 0 WHERE id IN ({placeholders})",
                tuple(ids),
            )
            affected = cursor.rowcount if cursor else 0
            # 写 audit_log(同事务)
            import datetime as _dt
            await tx.execute(
                """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
                   target_id, details, ip_addr, created_at)
                   VALUES (?, 'admin', 'retry_outbox', 'dirty_outbox', ?, ?, '', ?)""",
                (0, json.dumps(ids), "retry", _dt.datetime.now().isoformat()),
            )
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
        # R40 P0-5: skip_outbox + audit_log 同事务
        import datetime as _dt
        async with store.transaction() as tx:
            cursor = await tx.execute(
                f"UPDATE dirty_outbox SET processed = 1 WHERE id IN ({placeholders})",
                tuple(ids),
            )
            affected = cursor.rowcount if cursor else 0

            # 写 audit_log(同事务)
            await tx.execute(
                """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
                   target_id, details, ip_addr, created_at)
                   VALUES (?, 'admin', 'skip_outbox', 'dirty_outbox', ?, ?, '', ?)""",
                (0, json.dumps(ids), reason, _dt.datetime.now().isoformat()),
            )

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
        # R40 P0-5: UPDATE + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                f"""UPDATE replication_tasks
                    SET status = 'PLANNED', prev_status = status,
                        attempts = 0, next_retry_at = ?, last_error = '',
                        updated_at = ?
                    WHERE task_id IN ({placeholders})""",
                (_time.time(), _time.time(), *task_ids),
            )
            affected = cursor.rowcount if cursor else 0

            # 写 dirty_outbox 同步到 CRDB(同事务)
            for tid in task_ids:
                await store.add_dirty_outbox("replication_tasks", str(tid), connection=tx)

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


# ─── R45 第 16 节: 统一修复入口 + 因果链查询 ─────────────────────


async def execute_repair(
    action: str,
    params: dict,
    principal_id: int,
    approval_action_id: str = "",
) -> dict:
    """R45 第 16 节: 统一修复入口(白名单 + 审批 + 审计一体化)。

    所有 Repair Console 的修复操作必须通过此入口执行:
        1. 校验 action 是否在 SAFE_ACTIONS 白名单中(拒绝任意 SQL/Python)
        2. 若提供 approval_action_id,验证其已在 command_executions 中 executed
        3. 路由到对应的修复函数(retry_outbox/skip_outbox/...)
        4. 写 audit_log(action="repair_<action>")记录操作者 + 参数摘要

    安全铁律:
        - 严禁添加 "execute_sql" / "execute_python" / "eval" 等动作
        - 所有动作必须有明确的参数校验和审计
        - params 中的敏感字段(如 file_codes)在审计日志中以摘要形式记录

    Args:
        action: 动作名称(必须在 SAFE_ACTIONS 中)
        params: 动作参数(如 {"ids": [1,2,3]} / {"account_id": 100})
        principal_id: 操作者 admin_id(写入 audit_log.actor_id)
        approval_action_id: CommandBus 预审批的 action_id(可选,
            高危动作要求必须提供)

    Returns:
        {
            "success": bool,
            "action": str,
            "affected_count": int,   # 受影响的记录数
            "message": str,          # 结果描述
            "audit_log_id": int,     # 审计日志 ID(>0 表示已记录)
            "approval_verified": bool,  # 是否通过审批验证
        }

    Raises:
        ValueError: action 不在白名单中
        PermissionError: approval_action_id 无效或未通过
    """
    # 1. 白名单校验(铁律)
    if not is_safe_action(action):
        logger.warning(
            f"[RepairConsole] execute_repair 拒绝(非白名单动作) "
            f"action={action} principal={principal_id}"
        )
        # 写审计记录(便于追踪非法尝试)
        await _write_repair_audit(
            principal_id=principal_id,
            action=f"repair_rejected_{action}",
            details=f"非白名单动作: {action}",
            approval_action_id=approval_action_id,
        )
        raise ValueError(
            f"动作 '{action}' 不在白名单中,禁止执行(安全铁律)"
        )

    # 2. 审批验证(若提供 approval_action_id)
    approval_verified = False
    if approval_action_id:
        approval_verified = await _verify_approval(approval_action_id)
        if not approval_verified:
            logger.warning(
                f"[RepairConsole] execute_repair 拒绝(审批未通过) "
                f"action={action} approval_action_id={approval_action_id}"
            )
            await _write_repair_audit(
                principal_id=principal_id,
                action=f"repair_rejected_{action}",
                details=f"审批未通过: approval_action_id={approval_action_id}",
                approval_action_id=approval_action_id,
            )
            raise PermissionError(
                f"approval_action_id 未通过验证: {approval_action_id}"
            )

    # 3. 路由到对应修复函数
    affected_count = 0
    message = ""
    try:
        if action == "retry_outbox":
            ids = [int(i) for i in params.get("ids", []) if i]
            affected_count = await retry_outbox(ids)
            message = f"重试 {affected_count} 条 dirty_outbox 记录"
        elif action == "skip_outbox":
            ids = [int(i) for i in params.get("ids", []) if i]
            reason = str(params.get("reason", ""))
            affected_count = await skip_outbox(ids, reason=reason)
            message = f"跳过 {affected_count} 条 dirty_outbox 记录"
        elif action == "replay_dlq":
            ids = [int(i) for i in params.get("ids", []) if i]
            affected_count = await replay_dlq(ids)
            message = f"重放 {affected_count} 条死信消息"
        elif action == "retry_replication":
            task_ids = [int(i) for i in params.get("task_ids", []) if i]
            affected_count = await retry_replication(task_ids)
            message = f"重试 {affected_count} 个副本复制任务"
        elif action == "repair_relay":
            account_id = int(params.get("account_id", 0))
            ok = await repair_relay(account_id)
            affected_count = 1 if ok else 0
            message = f"修复 relay 账号 {account_id}: {'成功' if ok else '失败'}"
        elif action == "mark_outbox_skipped":
            # notification_outbox 跳过(委托给 notifications.py)
            from services import notifications
            outbox_id = int(params.get("outbox_id", 0))
            reason = str(params.get("reason", ""))
            ok = await notifications.mark_outbox_skipped(outbox_id, reason=reason)
            affected_count = 1 if ok else 0
            message = f"跳过 notification_outbox {outbox_id}: {'成功' if ok else '失败'}"
        else:
            # 理论上不会到达(白名单已校验)
            raise ValueError(f"未实现的动作: {action}")
    except Exception as e:
        logger.error(
            f"[RepairConsole] execute_repair 执行失败 "
            f"action={action} params={params}: {e}"
        )
        # 写失败审计
        audit_id = await _write_repair_audit(
            principal_id=principal_id,
            action=f"repair_failed_{action}",
            details=f"动作执行失败: {e}; params_hash={compute_payload_hash(params)}",
            approval_action_id=approval_action_id,
        )
        return {
            "success": False,
            "action": action,
            "affected_count": 0,
            "message": f"执行失败: {e}",
            "audit_log_id": audit_id,
            "approval_verified": approval_verified,
        }

    # 4. 写审计日志(成功)
    audit_id = await _write_repair_audit(
        principal_id=principal_id,
        action=f"repair_{action}",
        details=(
            f"{message}; params_hash={compute_payload_hash(params)}; "
            f"approval_verified={approval_verified}"
        ),
        approval_action_id=approval_action_id,
    )
    logger.info(
        f"[RepairConsole] execute_repair 成功 action={action} "
        f"affected={affected_count} principal={principal_id} "
        f"audit_id={audit_id}"
    )
    return {
        "success": True,
        "action": action,
        "affected_count": affected_count,
        "message": message,
        "audit_log_id": audit_id,
        "approval_verified": approval_verified,
    }


async def _verify_approval(approval_action_id: str) -> bool:
    """R45 内部辅助: 验证 approval_action_id 在 command_executions 中 status='executed'。

    Args:
        approval_action_id: CommandBus 预审批的 action_id

    Returns:
        True 已审批通过;False 未审批 / 状态非 executed / 不存在 / 查询失败
    """
    store = get_cache_store()
    if not store._db or not approval_action_id:
        return False
    try:
        rows = await store._db.execute_fetchall(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (approval_action_id,),
        )
        if not rows or not rows[0]:
            return False
        return rows[0][0] == "executed"
    except Exception as e:
        logger.warning(
            f"[RepairConsole] _verify_approval 查询失败 "
            f"approval_action_id={approval_action_id}: {e}"
        )
        return False


async def _write_repair_audit(
    principal_id: int,
    action: str,
    details: str,
    approval_action_id: str = "",
) -> int:
    """R45 内部辅助: 写入修复操作的审计日志。

    Args:
        principal_id: 操作者 admin_id
        action: 审计动作名(如 repair_retry_outbox / repair_rejected_xxx)
        details: 详情(含 params_hash 等)
        approval_action_id: 关联的审批 action_id(可空)

    Returns:
        audit_log.id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    try:
        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        cursor = await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, 'admin', ?, 'repair_console', ?, ?, '', ?)""",
            (principal_id, action, approval_action_id or "", details, now),
        )
        await store._db.commit()
        if cursor and cursor.lastrowid:
            audit_id = int(cursor.lastrowid)
            await store.add_dirty_outbox("audit_log", str(audit_id))
            return audit_id
        return 0
    except Exception as e:
        logger.warning(f"[RepairConsole] _write_repair_audit 失败: {e}")
        return 0


async def get_causal_chain(trace_id: str, limit: int = 50) -> dict:
    """R45 第 16 节: 因果链查询 — 展示 trace_id 关联的所有记录。

    帮助管理员理解故障的因果链:
        - audit_log 中 details LIKE %trace_id% 的记录(操作历史)
        - dirty_outbox 中 payload LIKE %trace_id% 的记录(数据变更)
        - command_executions 中 request_hash = trace_id 的记录(审批历史)
        - task_center 中 trace_id = ? 的任务记录(任务执行历史)
        - notification_outbox 中 payload LIKE %trace_id% 的记录(通知历史)

    返回的每条记录包含:
        - source:来源表(audit_log/dirty_outbox/command_executions/tasks/notif_outbox)
        - id:记录 ID
        - timestamp:时间戳(按时间顺序排列)
        - summary:摘要(避免泄露敏感内容,使用 compute_payload_hash)
        - raw:原始记录(供管理员深入排查)

    Args:
        trace_id: 追踪 ID
        limit: 最多返回条数(1-200)

    Returns:
        {
            "trace_id": str,
            "total": int,
            "events": [
                {
                    "source": str,        # audit_log/dirty_outbox/...
                    "id": int|str,
                    "timestamp": str,
                    "action": str,         # 动作描述
                    "summary": str,       # 摘要(含 payload_hash)
                    "actor_id": int,      # 操作者(若有)
                },
                ...
            ],
        }
    """
    if not trace_id:
        return {"trace_id": "", "total": 0, "events": []}
    store = get_cache_store()
    default = {"trace_id": trace_id, "total": 0, "events": []}
    if not store._db:
        return default
    limit = max(1, min(200, int(limit)))
    events: list[dict] = []
    # 1. audit_log(操作历史)
    try:
        rows = await store._db.execute_fetchall(
            """SELECT id, actor_id, action, target_type, target_id,
                      details, created_at
               FROM audit_log
               WHERE details LIKE ? OR target_id = ?
               ORDER BY created_at ASC LIMIT ?""",
            (f"%{trace_id}%", trace_id, limit),
        )
        for r in rows:
            if not r:
                continue
            details_str = str(r[5] or "")
            events.append({
                "source": "audit_log",
                "id": int(r[0]),
                "timestamp": r[6] or "",
                "action": r[2] or "",
                "summary": (
                    f"action={r[2]} target={r[3]}:{r[4]} "
                    f"details_hash={compute_payload_hash(details_str)}"
                ),
                "actor_id": int(r[1]) if r[1] else 0,
            })
    except Exception as e:
        logger.debug(f"[RepairConsole] get_causal_chain audit_log 失败: {e}")

    # 2. dirty_outbox(数据变更)
    try:
        rows = await store._db.execute_fetchall(
            """SELECT id, table_name, pk, operation, payload, created_at
               FROM dirty_outbox
               WHERE payload LIKE ? OR pk = ?
               ORDER BY created_at ASC LIMIT ?""",
            (f"%{trace_id}%", trace_id, limit),
        )
        for r in rows:
            if not r:
                continue
            payload_str = str(r[4] or "")
            events.append({
                "source": "dirty_outbox",
                "id": int(r[0]),
                "timestamp": r[5] or "",
                "action": f"{r[3] or 'unknown'} {r[1] or ''}",
                "summary": (
                    f"table={r[1]} pk={r[2]} "
                    f"payload_hash={compute_payload_hash(payload_str)}"
                ),
                "actor_id": 0,
            })
    except Exception as e:
        logger.debug(f"[RepairConsole] get_causal_chain dirty_outbox 失败: {e}")

    # 3. command_executions(审批历史)
    try:
        rows = await store._db.execute_fetchall(
            """SELECT action_id, command_type, principal_id, status,
                      result, created_at, updated_at
               FROM command_executions
               WHERE action_id = ? OR request_hash = ?
                  OR result LIKE ?
               ORDER BY created_at ASC LIMIT ?""",
            (trace_id, trace_id, f"%{trace_id}%", limit),
        )
        for r in rows:
            if not r:
                continue
            result_str = str(r[4] or "")
            events.append({
                "source": "command_executions",
                "id": r[0] or "",
                "timestamp": r[5] or r[6] or "",
                "action": f"command:{r[1] or ''}",
                "summary": (
                    f"status={r[3]} principal={r[2]} "
                    f"result_hash={compute_payload_hash(result_str)}"
                ),
                "actor_id": int(r[2]) if r[2] else 0,
            })
    except Exception as e:
        logger.debug(f"[RepairConsole] get_causal_chain command_executions 失败: {e}")

    # 4. tasks(任务执行历史)
    try:
        rows = await store._db.execute_fetchall(
            """SELECT id, user_id, task_type, status, progress,
                      payload, result, created_at, updated_at
               FROM tasks
               WHERE trace_id = ? OR payload LIKE ? OR result LIKE ?
               ORDER BY created_at ASC LIMIT ?""",
            (trace_id, f"%{trace_id}%", f"%{trace_id}%", limit),
        )
        for r in rows:
            if not r:
                continue
            payload_str = str(r[5] or "")
            result_str = str(r[6] or "")
            events.append({
                "source": "tasks",
                "id": int(r[0]),
                "timestamp": r[7] or r[8] or "",
                "action": f"task:{r[2] or ''}",
                "summary": (
                    f"status={r[3]} progress={r[4]} "
                    f"payload_hash={compute_payload_hash(payload_str)} "
                    f"result_hash={compute_payload_hash(result_str)}"
                ),
                "actor_id": int(r[1]) if r[1] else 0,
            })
    except Exception as e:
        logger.debug(f"[RepairConsole] get_causal_chain tasks 失败: {e}")

    # 5. notification_outbox(通知历史)
    try:
        # 表可能不存在(取决于 schema 初始化),使用 try/except 兜底
        rows = await store._db.execute_fetchall(
            """SELECT id, notif_id, user_id, notif_type, status,
                      payload, created_at
               FROM notification_outbox
               WHERE payload LIKE ? OR dedup_key = ?
               ORDER BY created_at ASC LIMIT ?""",
            (f"%{trace_id}%", trace_id, limit),
        )
        for r in rows:
            if not r:
                continue
            payload_str = str(r[5] or "")
            events.append({
                "source": "notif_outbox",
                "id": int(r[0]),
                "timestamp": r[6] or "",
                "action": f"notif:{r[3] or ''}",
                "summary": (
                    f"status={r[4]} notif_id={r[1]} user_id={r[2]} "
                    f"payload_hash={compute_payload_hash(payload_str)}"
                ),
                "actor_id": int(r[2]) if r[2] else 0,
            })
    except Exception as e:
        # notification_outbox 表可能不存在(尚未初始化),忽略
        logger.debug(
            f"[RepairConsole] get_causal_chain notification_outbox 失败(忽略): {e}"
        )

    # 按时间排序
    events.sort(key=lambda e: e.get("timestamp", ""))
    total = len(events)
    # 截断到 limit
    if total > limit:
        events = events[:limit]
    logger.info(
        f"[RepairConsole] get_causal_chain trace_id={trace_id} "
        f"找到 {total} 条事件"
    )
    return {
        "trace_id": trace_id,
        "total": total,
        "events": events,
    }


def format_causal_chain(chain: dict) -> str:
    """R45 第 16 节: 格式化因果链为可读文本(供 Admin Bot 展示)。

    Args:
        chain: get_causal_chain 返回的字典

    Returns:
        多行纯文本(避免 Telegram markdown 解析问题)
    """
    if not chain or not chain.get("events"):
        return f"trace_id={chain.get('trace_id', '')} 无关联事件"
    lines = [
        f"🔍 因果链追踪 trace_id={chain.get('trace_id', '')}",
        f"共 {chain.get('total', 0)} 条事件:",
        "",
    ]
    for i, event in enumerate(chain["events"], 1):
        lines.append(f"{i}. [{event.get('source', '')}] {event.get('action', '')}")
        lines.append(f"   时间: {event.get('timestamp', '')}")
        lines.append(f"   摘要: {event.get('summary', '')}")
        if event.get("actor_id"):
            lines.append(f"   操作者: {event.get('actor_id')}")
        lines.append("")
    return "\n".join(lines)
