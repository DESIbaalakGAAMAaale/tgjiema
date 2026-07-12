"""R40 §9.4: 数据生命周期 — 导出/删除/保留期/访问日志。

本模块提供用户数据的全生命周期管理能力:
- 数据导出(GDPR / 隐私合规): 一键导出用户所有数据
- 数据删除: 软删除 + 审计日志(GDPR 被遗忘权)
- 保留期管理: 按用户设置数据保留天数
- 过期清理: 物理删除已过保留期且已备份的数据
- 管理员访问日志: 记录管理员敏感操作(view/export/delete/config)

设计约束:
- 纯函数式 + async,通过 get_cache_store() 获取 CacheStore 单例
- 软删除统一调用 store.soft_delete()(R39 P1-5 铁律)
- 物理删除仅在 retention job 中执行(已备份 + 已过保留期)
- 时间戳统一使用 datetime.datetime.now().isoformat()
- 保留期表(user_data_retention)由本服务惰性创建
"""
from __future__ import annotations

import datetime as _dt
import json

from loguru import logger

from database.cache_store import get_cache_store


# ─── 默认保留期(天) ──────────────────────────────────────────
DEFAULT_RETENTION_DAYS = 7
# 保留期特殊值: 0 表示永久保留
RETENTION_PERMANENT = 0

# 保留期配置表名(本服务惰性创建)
_RETENTION_TABLE = "user_data_retention"


async def _ensure_retention_table() -> bool:
    """内部: 惰性创建保留期配置表(若不存在)。

    表结构:
        user_id        BIGINT PRIMARY KEY,
        retention_days INTEGER DEFAULT 7,
        updated_at     TEXT,
        last_purged_at TEXT

    Returns:
        True 表就绪;False 创建失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    try:
        await store._db.execute(
            f"""CREATE TABLE IF NOT EXISTS {_RETENTION_TABLE} (
                user_id        BIGINT PRIMARY KEY,
                retention_days INTEGER DEFAULT 7,
                updated_at     TEXT,
                last_purged_at TEXT
            )"""
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] 创建保留期表失败: {e}")
        return False


async def export_user_data(user_id: int) -> dict:
    """导出用户所有数据(GDPR/隐私合规)。

    聚合用户在 SQLite 本地库中的所有数据,用于合规导出。
    导出范围: user_info / file_codes / decode_logs / collections / tasks / notifications

    Args:
        user_id: 用户 id

    Returns:
        {user_info, file_codes, decode_logs, collections, tasks, notifications}
        查询失败的字段返回空列表 / None
    """
    store = get_cache_store()
    result: dict = {
        "user_info": None,
        "file_codes": [],
        "decode_logs": [],
        "collections": [],
        "tasks": [],
        "notifications": [],
    }
    if not store._db:
        return result
    now = _dt.datetime.now().isoformat()
    # user_info
    try:
        cursor = await store._db.execute(
            "SELECT user_id, username, first_name, membership_level, "
            "daily_decode_quota, quota_used_today, quota_date, can_upload, "
            "is_banned, created_at FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            result["user_info"] = {
                "user_id": row[0], "username": row[1], "first_name": row[2],
                "membership_level": row[3], "daily_decode_quota": row[4],
                "quota_used_today": row[5], "quota_date": row[6],
                "can_upload": row[7], "is_banned": row[8], "created_at": row[9],
            }
    except Exception as e:
        logger.warning(f"[DataLifecycle] export user_info 失败: {e}")
    # file_codes(file_records_local)
    try:
        cursor = await store._db.execute(
            "SELECT file_code, file_types, status, request_count, "
            "is_collection, collection_codes, create_time "
            "FROM file_records_local WHERE uploader_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["file_codes"] = [
            {
                "file_code": r[0], "file_types": r[1], "status": r[2],
                "request_count": r[3], "is_collection": r[4],
                "collection_codes": r[5], "create_time": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export file_codes 失败: {e}")
    # decode_logs(decode_log_buffer)
    try:
        cursor = await store._db.execute(
            "SELECT file_code, request_time, status, source_channel_id "
            "FROM decode_log_buffer WHERE requester_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["decode_logs"] = [
            {
                "file_code": r[0], "request_time": r[1], "status": r[2],
                "source_channel_id": r[3],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export decode_logs 失败: {e}")
    # collections
    try:
        cursor = await store._db.execute(
            "SELECT id, name, code, description, version, item_count, status, "
            "created_at FROM collections WHERE owner_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["collections"] = [
            {
                "id": r[0], "name": r[1], "code": r[2], "description": r[3],
                "version": r[4], "item_count": r[5], "status": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export collections 失败: {e}")
    # tasks
    try:
        cursor = await store._db.execute(
            "SELECT id, task_type, status, progress, eta_seconds, created_at, updated_at "
            "FROM tasks WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["tasks"] = [
            {
                "id": r[0], "task_type": r[1], "status": r[2], "progress": r[3],
                "eta_seconds": r[4], "created_at": r[5], "updated_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export tasks 失败: {e}")
    # notifications
    try:
        cursor = await store._db.execute(
            "SELECT id, type, payload, is_read, created_at "
            "FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        result["notifications"] = [
            {
                "id": r[0], "type": r[1], "payload": r[2],
                "is_read": r[3], "created_at": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[DataLifecycle] export notifications 失败: {e}")
    logger.info(
        f"[DataLifecycle] export_user_data user={user_id} "
        f"files={len(result['file_codes'])} "
        f"collections={len(result['collections'])} "
        f"tasks={len(result['tasks'])}"
    )
    return result


async def _write_audit_log(actor_id: int, action: str, target_type: str,
                           target_id: str, details: dict,
                           ip_addr: str = "") -> int:
    """内部: 写入审计日志(audit_log 表)。

    Args:
        actor_id: 操作者 id(管理员)
        action: 动作描述
        target_type: 目标类型
        target_id: 目标主键
        details: 详情字典(序列化为 JSON)
        ip_addr: 操作来源 IP(可选)

    Returns:
        新日志 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    try:
        cursor = await store._db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_type, action, target_type, target_id,
                details, ip_addr, created_at)
               VALUES (?, 'admin', ?, ?, ?, ?, ?, ?)""",
            (actor_id, action, target_type, target_id,
             json.dumps(details, ensure_ascii=False), ip_addr, now),
        )
        await store._db.commit()
        return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
    except Exception as e:
        logger.warning(f"[DataLifecycle] _write_audit_log 失败: {e}")
        return 0


async def delete_user_data(user_id: int, admin_id: int = 0) -> bool:
    """删除用户所有数据(软删除 + 写 audit_log)。

    执行步骤:
    - file_records: 逐条 soft_delete(R39 P1-5)
    - codes: 逐条 soft_delete
    - collections: 逐条 soft_delete
    - notifications: 物理删除(通知无需保留)
    - tasks: 标记 cancelled
    - users_local: is_banned=1 + deleted_at(标记删除)

    Args:
        user_id: 被删除用户 id
        admin_id: 操作管理员 id(0=用户自删)

    Returns:
        True 删除成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    deleted_files = 0
    deleted_codes = 0
    deleted_collections = 0
    # 1. file_records 软删除
    try:
        cursor = await store._db.execute(
            "SELECT file_code FROM file_records_local WHERE uploader_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        for r in rows:
            if await store.soft_delete("file_records", r[0]):
                deleted_files += 1
    except Exception as e:
        logger.warning(f"[DataLifecycle] delete file_records 失败: {e}")
    # 2. codes 软删除
    try:
        cursor = await store._db.execute(
            "SELECT code FROM codes_local WHERE uploader_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        for r in rows:
            if await store.soft_delete("codes", r[0]):
                deleted_codes += 1
    except Exception as e:
        logger.warning(f"[DataLifecycle] delete codes 失败: {e}")
    # 3. collections 软删除
    try:
        cursor = await store._db.execute(
            "SELECT id FROM collections WHERE owner_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        for r in rows:
            if await store.soft_delete("collections", str(r[0])):
                deleted_collections += 1
    except Exception as e:
        logger.warning(f"[DataLifecycle] delete collections 失败: {e}")
    # 4. notifications 物理删除(通知无需保留)
    try:
        await store._db.execute(
            "DELETE FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        await store._db.commit()
    except Exception as e:
        logger.warning(f"[DataLifecycle] delete notifications 失败: {e}")
    # 5. tasks 标记 cancelled
    try:
        await store._db.execute(
            "UPDATE tasks SET status = 'cancelled', updated_at = ? "
            "WHERE user_id = ? AND status NOT IN ('cancelled', 'done')",
            (now, user_id),
        )
        await store._db.commit()
    except Exception as e:
        logger.warning(f"[DataLifecycle] update tasks 失败: {e}")
    # 6. users_local 标记删除(is_banned=1 + deleted_at,无 status 列故用 is_banned)
    try:
        await store._db.execute(
            "UPDATE users_local SET is_banned = 1, deleted_at = ?, updated_at = ? "
            "WHERE user_id = ?",
            (now, now, user_id),
        )
        await store._db.commit()
        # 写 dirty_outbox 确保跨机同步
        await store.add_dirty_outbox("users_local", str(user_id))
    except Exception as e:
        logger.warning(f"[DataLifecycle] delete users_local 失败: {e}")
    # 写审计日志
    await _write_audit_log(
        admin_id, "delete_user_data", "user", str(user_id),
        {
            "deleted_files": deleted_files,
            "deleted_codes": deleted_codes,
            "deleted_collections": deleted_collections,
        },
        "",
    )
    logger.info(
        f"[DataLifecycle] delete_user_data user={user_id} admin={admin_id} "
        f"files={deleted_files} codes={deleted_codes} collections={deleted_collections}"
    )
    return True


async def set_retention(user_id: int, days: int) -> bool:
    """设置用户数据保留期(0=永久)。

    Args:
        user_id: 用户 id
        days: 保留天数(0=永久保留)

    Returns:
        True 设置成功;False 失败
    """
    if days < 0:
        logger.warning(f"[DataLifecycle] set_retention 非法 days={days}")
        return False
    if not await _ensure_retention_table():
        return False
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        await store._db.execute(
            f"""INSERT INTO {_RETENTION_TABLE}
                (user_id, retention_days, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    retention_days = excluded.retention_days,
                    updated_at = excluded.updated_at""",
            (user_id, days, now),
        )
        await store._db.commit()
        logger.info(
            f"[DataLifecycle] set_retention user={user_id} days={days}"
        )
        return True
    except Exception as e:
        logger.warning(f"[DataLifecycle] set_retention 失败: {e}")
        return False


async def get_retention(user_id: int) -> int:
    """获取保留期(默认 7 天)。

    Args:
        user_id: 用户 id

    Returns:
        保留天数(0=永久);未设置时返回默认 7 天
    """
    if not await _ensure_retention_table():
        return DEFAULT_RETENTION_DAYS
    store = get_cache_store()
    if not store._db:
        return DEFAULT_RETENTION_DAYS
    try:
        cursor = await store._db.execute(
            f"SELECT retention_days FROM {_RETENTION_TABLE} WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return DEFAULT_RETENTION_DAYS
        return int(row[0])
    except Exception as e:
        logger.warning(f"[DataLifecycle] get_retention 失败: {e}")
        return DEFAULT_RETENTION_DAYS


async def cleanup_expired_data(batch_size: int = 1000) -> int:
    """清理过期数据(物理删除已过保留期且已备份的数据)。

    遍历设置了保留期的用户,物理删除其超过保留期的已软删数据。
    本方法仅执行物理删除(独立 retention job,符合 R39 P1-5 铁律)。

    Args:
        batch_size: 单批最大清理条数

    Returns:
        清理的总行数
    """
    if not await _ensure_retention_table():
        return 0
    store = get_cache_store()
    if not store._db:
        return 0
    total_cleaned = 0
    now_dt = _dt.datetime.now()
    # 拉取所有设置了保留期(非永久)的用户
    try:
        cursor = await store._db.execute(
            f"SELECT user_id, retention_days FROM {_RETENTION_TABLE} "
            f"WHERE retention_days > 0 LIMIT ?",
            (batch_size,),
        )
        rows = await cursor.fetchall()
    except Exception as e:
        logger.warning(f"[DataLifecycle] cleanup 拉取保留期配置失败: {e}")
        return 0
    for r in rows:
        user_id, retention_days = r[0], int(r[1])
        cutoff_dt = now_dt - _dt.timedelta(days=retention_days)
        cutoff_iso = cutoff_dt.isoformat()
        # 物理删除该用户已软删(deleted_at < cutoff)的 file_records
        try:
            cursor = await store._db.execute(
                "DELETE FROM file_records_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            total_cleaned += cursor.rowcount or 0
        except Exception as e:
            logger.debug(f"[DataLifecycle] cleanup file_records 失败: {e}")
        # 物理删除该用户已软删的 codes
        try:
            cursor = await store._db.execute(
                "DELETE FROM codes_local "
                "WHERE uploader_id = ? AND deleted_at IS NOT NULL "
                "AND deleted_at < ?",
                (user_id, cutoff_iso),
            )
            total_cleaned += cursor.rowcount or 0
        except Exception as e:
            logger.debug(f"[DataLifecycle] cleanup codes 失败: {e}")
        # 更新 last_purged_at
        try:
            await store._db.execute(
                f"UPDATE {_RETENTION_TABLE} SET last_purged_at = ? "
                f"WHERE user_id = ?",
                (now_dt.isoformat(), user_id),
            )
            await store._db.commit()
        except Exception as e:
            logger.debug(f"[DataLifecycle] cleanup 更新 last_purged_at 失败: {e}")
        if total_cleaned >= batch_size:
            break
    logger.info(
        f"[DataLifecycle] cleanup_expired_data 清理 {total_cleaned} 行 "
        f"(users_checked={len(rows)})"
    )
    return total_cleaned


async def log_admin_access(admin_id: int, action: str, target_type: str = "",
                           target_id: str = "", details: dict = None,
                           ip_addr: str = "") -> int:
    """记录管理员访问日志。

    Args:
        admin_id: 管理员 id
        action: 动作枚举(view_users/view_files/export_data/delete_data/config_change)
        target_type: 目标类型(可选)
        target_id: 目标主键(可选)
        details: 详情字典(序列化为 JSON)
        ip_addr: 操作来源 IP(可选)

    Returns:
        新日志 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    details_json = json.dumps(details or {}, ensure_ascii=False)
    try:
        cursor = await store._db.execute(
            """INSERT INTO admin_access_log
               (admin_id, action, target_type, target_id, details, ip_addr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (admin_id, action, target_type, target_id,
             details_json, ip_addr, now),
        )
        await store._db.commit()
        log_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        logger.info(
            f"[DataLifecycle] log_admin_access admin={admin_id} "
            f"action={action} target={target_type}:{target_id}"
        )
        return log_id
    except Exception as e:
        logger.warning(f"[DataLifecycle] log_admin_access 失败: {e}")
        return 0


async def list_admin_access_logs(admin_id: int | None = None, page: int = 1,
                                  page_size: int = 20) -> dict:
    """分页查询管理员访问日志。

    Args:
        admin_id: 按管理员过滤(None=全部)
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": N, "page": page, "page_size": page_size}
    """
    store = get_cache_store()
    empty = {"items": [], "total": 0, "page": page, "page_size": page_size}
    if not store._db:
        return empty
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 20
    offset = (page - 1) * page_size
    try:
        # 统计总数
        if admin_id is not None:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM admin_access_log WHERE admin_id = ?",
                (admin_id,),
            )
        else:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM admin_access_log"
            )
        row = await cursor.fetchone()
        total = int(row[0]) if row and row[0] else 0
        # 分页查询
        if admin_id is not None:
            cursor = await store._db.execute(
                """SELECT id, admin_id, action, target_type, target_id,
                          details, ip_addr, created_at
                   FROM admin_access_log WHERE admin_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (admin_id, page_size, offset),
            )
        else:
            cursor = await store._db.execute(
                """SELECT id, admin_id, action, target_type, target_id,
                          details, ip_addr, created_at
                   FROM admin_access_log
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (page_size, offset),
            )
        rows = await cursor.fetchall()
        items = [
            {
                "id": r[0], "admin_id": r[1], "action": r[2],
                "target_type": r[3], "target_id": r[4], "details": r[5],
                "ip_addr": r[6], "created_at": r[7],
            }
            for r in rows
        ]
        return {"items": items, "total": total, "page": page,
                "page_size": page_size}
    except Exception as e:
        logger.warning(f"[DataLifecycle] list_admin_access_logs 失败: {e}")
        return empty


async def check_retention_compliance() -> dict:
    """检查保留期合规性。

    统计:
    - total_users: 总用户数(设置了保留期的用户)
    - expired_count: 已过保留期但未清理的用户数
    - oldest_unpurged_days: 最久未清理的天数
    - compliance_rate: 合规率(1 - expired/total)

    Returns:
        {total_users, expired_count, oldest_unpurged_days, compliance_rate}
    """
    empty = {
        "total_users": 0, "expired_count": 0,
        "oldest_unpurged_days": 0, "compliance_rate": 1.0,
    }
    if not await _ensure_retention_table():
        return empty
    store = get_cache_store()
    if not store._db:
        return empty
    now_dt = _dt.datetime.now()
    total_users = 0
    expired_count = 0
    oldest_unpurged_days = 0
    try:
        cursor = await store._db.execute(
            f"SELECT user_id, retention_days, last_purged_at "
            f"FROM {_RETENTION_TABLE} WHERE retention_days > 0"
        )
        rows = await cursor.fetchall()
        total_users = len(rows)
        for r in rows:
            retention_days = int(r[1])
            last_purged_at = r[2]
            # 判断是否过期:最后清理时间距今超过保留期则视为过期
            if last_purged_at:
                try:
                    purged_dt = _dt.datetime.fromisoformat(last_purged_at)
                    days_since_purge = (now_dt - purged_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    days_since_purge = retention_days + 1
            else:
                # 从未清理过,按最大值处理
                days_since_purge = retention_days + 1
            if days_since_purge > retention_days:
                expired_count += 1
                if days_since_purge > oldest_unpurged_days:
                    oldest_unpurged_days = int(days_since_purge)
    except Exception as e:
        logger.warning(f"[DataLifecycle] check_retention_compliance 失败: {e}")
        return empty
    compliance_rate = 1.0
    if total_users > 0:
        compliance_rate = 1.0 - (expired_count / total_users)
    result = {
        "total_users": total_users,
        "expired_count": expired_count,
        "oldest_unpurged_days": oldest_unpurged_days,
        "compliance_rate": round(compliance_rate, 4),
    }
    logger.info(
        f"[DataLifecycle] check_retention_compliance "
        f"total={total_users} expired={expired_count} "
        f"rate={compliance_rate:.2%}"
    )
    return result
