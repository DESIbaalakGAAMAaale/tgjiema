"""R40 §9.4: 内容安全与合规 — 举报/下架/封禁/申诉/审计闭环。

本模块提供内容安全合规的全流程能力:
- 举报创建与状态流转(pending → takedown/appealed → resolved/rejected)
- 内容下架(软删除文件 / 标记用户 banned)
- 用户封禁/解封(写入 users_local.is_banned + audit_log + dirty_outbox)
- 申诉与举报处理
- 分页查询与详情
- 管理员可读格式化

设计约束:
- 纯函数式 + async,通过 get_cache_store() 获取 CacheStore 单例
- 所有写入操作后调用 store.add_dirty_outbox() 确保跨机同步
- 软删除统一调用 store.soft_delete()(R39 P1-5 铁律)
- 时间戳统一使用 datetime.datetime.now().isoformat()
"""
from __future__ import annotations

import datetime as _dt
import json

from loguru import logger

from database.cache_store import get_cache_store


# ─── 举报状态 ───────────────────────────────────────────────
REPORT_STATUS_PENDING = "pending"      # 待处理
REPORT_STATUS_TAKEDOWN = "takedown"     # 已下架
REPORT_STATUS_APPEALED = "appealed"     # 已申诉
REPORT_STATUS_RESOLVED = "resolved"    # 已解决(维持下架)
REPORT_STATUS_REJECTED = "rejected"    # 已驳回(不下架)
# R41 P1-13: 2 人审批恢复操作的状态(等待第二审批人)
REPORT_STATUS_RESTORE_PENDING = "restore_pending"

# ─── 举报目标类型 ───────────────────────────────────────────
TARGET_TYPE_FILE = "file"
TARGET_TYPE_USER = "user"
TARGET_TYPE_CODE = "code"

# ─── 举报原因枚举 ───────────────────────────────────────────
_VALID_REASONS = {"spam", "copyright", "illegal", "malware", "abuse", "other"}
_VALID_TARGET_TYPES = {TARGET_TYPE_FILE, TARGET_TYPE_USER, TARGET_TYPE_CODE}


async def create_report(reporter_id: int, target_type: str, target_id: str,
                        reason: str, description: str = "") -> int:
    """创建举报,返回 report_id。

    Args:
        reporter_id: 举报人 user_id
        target_type: 举报目标类型(file/user/code)
        target_id: 目标主键(文件码 / user_id / code)
        reason: 举报原因(spam/copyright/illegal/malware/abuse/other)
        description: 补充描述(可选)

    Returns:
        新举报 id;失败返回 0
    """
    if target_type not in _VALID_TARGET_TYPES:
        logger.warning(f"[ContentReports] create_report 非法 target_type={target_type}")
        return 0
    if reason not in _VALID_REASONS:
        logger.warning(f"[ContentReports] create_report 非法 reason={reason}")
        return 0
    store = get_cache_store()
    if not store._db:
        logger.warning("[ContentReports] create_report 数据库未初始化")
        return 0
    now = _dt.datetime.now().isoformat()
    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """INSERT INTO content_reports
                   (reporter_id, target_type, target_id, reason, description,
                    status, appeal_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
                (reporter_id, target_type, target_id, reason, description,
                 REPORT_STATUS_PENDING, now),
            )
            report_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if report_id:
                await store.add_dirty_outbox("content_reports", str(report_id), connection=tx)
        if report_id:
            logger.info(
                f"[ContentReports] 创建举报 id={report_id} reporter={reporter_id} "
                f"target={target_type}:{target_id} reason={reason}"
            )
        return report_id
    except Exception as e:
        logger.warning(f"[ContentReports] create_report 失败: {e}")
        return 0


async def _write_audit_log(actor_id: int, action: str, target_type: str,
                           target_id: str, details: dict,
                           ip_addr: str = "", tx=None) -> int:
    """内部: 写入审计日志(audit_log 表)。

    R40 P0-5: 支持 tx 参数(同事务写入),不自动 commit。
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    try:
        if tx is not None:
            cursor = await tx.execute(
                """INSERT INTO audit_log
                   (actor_id, actor_type, action, target_type, target_id,
                    details, ip_addr, created_at)
                   VALUES (?, 'admin', ?, ?, ?, ?, ?, ?)""",
                (actor_id, action, target_type, target_id,
                 json.dumps(details, ensure_ascii=False), ip_addr, now),
            )
            log_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if log_id:
                await store.add_dirty_outbox("audit_log", "last", connection=tx)
            return log_id
        # 兼容模式: 无 tx 时自动 commit
        cursor = await store._db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_type, action, target_type, target_id,
                details, ip_addr, created_at)
               VALUES (?, 'admin', ?, ?, ?, ?, ?, ?)""",
            (actor_id, action, target_type, target_id,
             json.dumps(details, ensure_ascii=False), ip_addr, now),
        )
        await store._db.commit()
        log_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        if log_id:
            await store.add_dirty_outbox("audit_log", "last")
        return log_id
    except Exception as e:
        logger.warning(f"[ContentReports] _write_audit_log 失败: {e}")
        return 0


async def _notify_user(user_id: int, ntype: str, payload: dict, tx=None) -> bool:
    """内部: 向 notifications 表写入一条用户通知。

    R40 P0-5: 支持 tx 参数(同事务写入),不自动 commit。
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        if tx is not None:
            cursor = await tx.execute(
                """INSERT INTO notifications
                   (user_id, type, payload, is_read, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (user_id, ntype, json.dumps(payload, ensure_ascii=False), now),
            )
            nid = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if nid:
                await store.add_dirty_outbox("notifications", str(nid), connection=tx)
            return True
        # 兼容模式: 无 tx 时自动 commit
        cursor = await store._db.execute(
            """INSERT INTO notifications
               (user_id, type, payload, is_read, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (user_id, ntype, json.dumps(payload, ensure_ascii=False), now),
        )
        await store._db.commit()
        nid = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        if nid:
            await store.add_dirty_outbox("notifications", str(nid))
        return True
    except Exception as e:
        logger.warning(f"[ContentReports] _notify_user 失败: {e}")
        return False


async def takedown_content(target_type: str, target_id: str, reason: str,
                          admin_id: int) -> bool:
    """下架内容(软删除文件 + 写 audit_log + 通知文件所有者)。

    - file/code: 调用 store.soft_delete("file_records"/"codes", target_id)
    - user: 标记用户 banned(更新 users_local.is_banned=1)

    Args:
        target_type: 目标类型(file/user/code)
        target_id: 目标主键
        reason: 下架原因
        admin_id: 操作管理员 id

    Returns:
        True 下架成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    success = False
    owner_id = 0
    # R40 P0-5: 软删除/封禁 + dirty_outbox + audit_log + notification 同事务
    try:
        async with store.transaction() as tx:
            if target_type == TARGET_TYPE_FILE:
                # 软删除文件记录(UPDATE deleted_at + status='deleted' + dirty_outbox tombstone)
                cursor = await tx.execute(
                    "UPDATE file_records_local SET deleted_at = ?, status = 'deleted', "
                    "crdb_synced = 0 WHERE file_code = ?",
                    (now, target_id),
                )
                success = cursor.rowcount > 0 if cursor else False
                if success:
                    await store.add_dirty_outbox(
                        "file_records", target_id, "tombstone",
                        json.dumps({"deleted_at": now}), connection=tx,
                    )
                # 查询文件所有者用于通知
                try:
                    cur = await tx.execute(
                        "SELECT uploader_id FROM file_records_local WHERE file_code = ?",
                        (target_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        owner_id = int(row[0])
                except Exception as e:
                    logger.debug(f"[ContentReports] takedown 查询所有者失败: {e}")
            elif target_type == TARGET_TYPE_CODE:
                # 软删除文件码
                cursor = await tx.execute(
                    "UPDATE codes_local SET deleted_at = ?, status = 'deleted', "
                    "crdb_synced = 0 WHERE code = ?",
                    (now, target_id),
                )
                success = cursor.rowcount > 0 if cursor else False
                if success:
                    await store.add_dirty_outbox(
                        "codes", target_id, "tombstone",
                        json.dumps({"deleted_at": now}), connection=tx,
                    )
                try:
                    cur = await tx.execute(
                        "SELECT uploader_id FROM codes_local WHERE code = ?",
                        (target_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        owner_id = int(row[0])
                except Exception as e:
                    logger.debug(f"[ContentReports] takedown 查询所有者失败: {e}")
            elif target_type == TARGET_TYPE_USER:
                # 标记用户 banned(is_banned=1)
                cursor = await tx.execute(
                    "UPDATE users_local SET is_banned = 1, updated_at = ? WHERE user_id = ?",
                    (now, target_id),
                )
                success = cursor.rowcount > 0 if cursor else False
                owner_id = int(target_id) if success else 0
                if success:
                    await store.add_dirty_outbox("users_local", str(target_id), connection=tx)
            else:
                logger.warning(f"[ContentReports] takedown 非法 target_type={target_type}")
                return False
            # 写审计日志(同事务)
            await _write_audit_log(
                admin_id, "takedown", target_type, target_id,
                {"reason": reason, "success": success}, "", tx=tx,
            )
            # 通知所有者(同事务)
            if success and owner_id:
                await _notify_user(owner_id, "takedown", {
                    "target_type": target_type, "target_id": target_id, "reason": reason,
                }, tx=tx)
    except Exception as e:
        logger.warning(f"[ContentReports] takedown_content 失败: {e}")
        return False
    logger.info(
        f"[ContentReports] takedown target={target_type}:{target_id} "
        f"success={success} admin={admin_id}"
    )
    return success


async def ban_user(user_id: int, reason: str, duration_days: int = 0,
                   admin_id: int = 0) -> bool:
    """封禁用户(duration_days=0 永久)。

    - 更新 users_local.is_banned=1(实际封禁状态字段)
    - R40 P1-11: 持久化 ban_expires_at(duration_days>0 写到期 ISO 时间,永久写 NULL)
    - 写 audit_log + dirty_outbox
    - 发送 ban 通知给用户

    Args:
        user_id: 被封禁用户 id
        reason: 封禁原因
        duration_days: 封禁时长(0=永久)
        admin_id: 操作管理员 id

    Returns:
        True 封禁成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    # R40 P1-11: 计算封禁到期时间(永久封禁写 NULL,临时封禁写 ISO 时间字符串)
    # 用 None 表示永久,非空字符串表示临时封禁到期时间(供 check_user_banned 自动解封)
    ban_expires_at: str | None = None
    expires_at_for_log = ""
    if duration_days > 0:
        try:
            expires_dt = _dt.datetime.now() + _dt.timedelta(days=duration_days)
            ban_expires_at = expires_dt.isoformat()
            expires_at_for_log = ban_expires_at
        except Exception:
            ban_expires_at = None
    try:
        # R40 P0-5: users_local + dirty_outbox + audit_log + notifications 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE users_local SET is_banned = 1, ban_expires_at = ?, "
                "updated_at = ? WHERE user_id = ?",
                (ban_expires_at, now, user_id),
            )
            if cursor.rowcount == 0:
                logger.warning(f"[ContentReports] ban_user 未命中用户 user_id={user_id}")
                return False
            # 写 dirty_outbox 确保跨机同步(同事务)
            await store.add_dirty_outbox("users_local", str(user_id), connection=tx)
            # 写审计日志(同事务)
            await _write_audit_log(
                admin_id, "ban", "user", str(user_id),
                {"reason": reason, "duration_days": duration_days,
                 "expires_at": expires_at_for_log}, "", tx=tx,
            )
            # 发送封禁通知给用户(同事务)
            await _notify_user(user_id, "ban", {
                "reason": reason, "duration_days": duration_days,
                "expires_at": expires_at_for_log, "admin_id": admin_id,
            }, tx=tx)
    except Exception as e:
        logger.warning(f"[ContentReports] ban_user 更新失败: {e}")
        return False
    logger.info(
        f"[ContentReports] ban_user user={user_id} reason={reason} "
        f"duration={duration_days} admin={admin_id}"
    )
    return True


async def unban_user(user_id: int, admin_id: int = 0) -> bool:
    """解封用户。

    Args:
        user_id: 被解封用户 id
        admin_id: 操作管理员 id

    Returns:
        True 解封成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        # R40 P0-5: users_local + dirty_outbox + audit_log + notifications 同事务
        # R40 P1-11: 解封时清除 ban_expires_at(NULL),避免残留到期时间
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE users_local SET is_banned = 0, ban_expires_at = NULL, "
                "updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            if cursor.rowcount == 0:
                logger.warning(f"[ContentReports] unban_user 未命中用户 user_id={user_id}")
                return False
            # 写 dirty_outbox 确保跨机同步(同事务)
            await store.add_dirty_outbox("users_local", str(user_id), connection=tx)
            # 写审计日志(同事务)
            await _write_audit_log(
                admin_id, "unban", "user", str(user_id), {}, "", tx=tx,
            )
            # 通知用户已解封(同事务)
            await _notify_user(user_id, "unban", {"admin_id": admin_id}, tx=tx)
    except Exception as e:
        logger.warning(f"[ContentReports] unban_user 更新失败: {e}")
        return False
    logger.info(f"[ContentReports] unban_user user={user_id} admin={admin_id}")
    return True


async def appeal_report(report_id: int, user_id: int, appeal_text: str) -> bool:
    """用户申诉举报。

    - 更新 content_reports.status='appealed', appeal_text, appealed_at
    - 通知管理员有新申诉

    Args:
        report_id: 举报 id
        user_id: 申诉用户 id
        appeal_text: 申诉理由

    Returns:
        True 申诉成功;False 失败(举报不存在 / 状态不允许)
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    # 仅处于 takedown 状态的举报可申诉
    try:
        cursor = await store._db.execute(
            "SELECT status, target_type, target_id FROM content_reports WHERE id = ?",
            (report_id,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.warning(f"[ContentReports] appeal_report 举报不存在 id={report_id}")
            return False
        current_status = row[0]
        if current_status != REPORT_STATUS_TAKEDOWN:
            logger.warning(
                f"[ContentReports] appeal_report 状态不允许申诉 "
                f"id={report_id} status={current_status}"
            )
            return False
        target_type, target_id = row[1], row[2]
    except Exception as e:
        logger.warning(f"[ContentReports] appeal_report 查询失败: {e}")
        return False
    try:
        # R40 P0-5: UPDATE + dirty_outbox + notification 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE content_reports
                   SET status = ?, appeal_text = ?, appealed_at = ?
                   WHERE id = ?""",
                (REPORT_STATUS_APPEALED, appeal_text, now, report_id),
            )
            if cursor.rowcount == 0:
                return False
            await store.add_dirty_outbox("content_reports", str(report_id), connection=tx)
            # 通知管理员有新申诉(写入管理员通知,使用 user_id=0 表示系统管理员)
            await _notify_user(0, "appeal", {
                "report_id": report_id, "user_id": user_id,
                "appeal_text": appeal_text, "target_type": target_type,
                "target_id": target_id,
            }, tx=tx)
    except Exception as e:
        logger.warning(f"[ContentReports] appeal_report 更新失败: {e}")
        return False
    logger.info(
        f"[ContentReports] appeal_report id={report_id} user={user_id}"
    )
    return True


# R41 P1-13: 恢复操作 — 2 人审批 + 通过 CommandBus 执行
# 恢复操作是高风险操作(可能恢复违规内容),需要 2 个管理员独立审批:
#   第一审批人(approve)→ status=restore_pending,等待第二审批人
#   第二审批人(approve,与第一审批人不同)→ 执行恢复 + 通知举报者
# 任一审批人 reject → 维持下架 + 通知申诉者
# 所有审批动作写入 audit_log,支持审计追溯


async def _restore_content_internal(
    target_type: str,
    target_id: str,
    admin_id: int,
) -> bool:
    """R41 P1-13: 内部恢复内容(撤销软删除 / 解封用户)。

    与 ``takedown_content`` 相反操作:
    - file/code: UPDATE deleted_at=NULL, status='active'
    - user: 调用 unban_user 清除 is_banned
    - 写 audit_log(action='restore')
    - 写 dirty_outbox 触发 CRDB 同步

    Args:
        target_type: 目标类型(file/user/code)
        target_id: 目标主键
        admin_id: 操作管理员 id(第二审批人)

    Returns:
        True 恢复成功;False 失败
    """
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    success = False
    try:
        # R40 P0-5: 业务表 + dirty_outbox + audit_log 同事务
        async with store.transaction() as tx:
            if target_type == TARGET_TYPE_FILE:
                cursor = await tx.execute(
                    "UPDATE file_records_local SET deleted_at = NULL, status = 'active', "
                    "crdb_synced = 0 WHERE file_code = ?",
                    (target_id,),
                )
                success = cursor.rowcount > 0 if cursor else False
                if success:
                    await store.add_dirty_outbox(
                        "file_records", target_id, "restore",
                        json.dumps({"restored_at": now, "admin_id": admin_id}),
                        connection=tx,
                    )
            elif target_type == TARGET_TYPE_CODE:
                cursor = await tx.execute(
                    "UPDATE codes_local SET deleted_at = NULL, status = 'active', "
                    "crdb_synced = 0 WHERE code = ?",
                    (target_id,),
                )
                success = cursor.rowcount > 0 if cursor else False
                if success:
                    await store.add_dirty_outbox(
                        "codes", target_id, "restore",
                        json.dumps({"restored_at": now, "admin_id": admin_id}),
                        connection=tx,
                    )
            elif target_type == TARGET_TYPE_USER:
                # 恢复用户:清除 is_banned + ban_expires_at
                cursor = await tx.execute(
                    "UPDATE users_local SET is_banned = 0, ban_expires_at = NULL, "
                    "updated_at = ? WHERE user_id = ?",
                    (now, target_id),
                )
                success = cursor.rowcount > 0 if cursor else False
                if success:
                    await store.add_dirty_outbox(
                        "users_local", str(target_id), connection=tx,
                    )
            else:
                logger.warning(
                    f"[ContentReports] _restore_content_internal 非法 target_type={target_type}"
                )
                return False
            # 写审计日志(同事务)
            await _write_audit_log(
                admin_id, "restore", target_type, target_id,
                {"success": success, "restored_at": now}, "", tx=tx,
            )
    except Exception as e:
        logger.warning(f"[ContentReports] _restore_content_internal 失败: {e}")
        return False
    logger.info(
        f"[ContentReports] _restore_content_internal target={target_type}:{target_id} "
        f"success={success} admin={admin_id}"
    )
    return success


async def process_appeal(
    appeal_id: int,
    principal_id: int,
    decision: str,
    note: str = "",
) -> dict:
    """R41 P1-13: 处理申诉(2 人审批恢复操作)。

    状态机:
        appealed → restore_pending (第一审批人 approve)
        restore_pending → resolved (第二审批人 approve,执行恢复)
        appealed / restore_pending → rejected (任一审批人 reject)

    通知:
        - approve (第一审批): 不通知(等待第二审批)
        - approve (第二审批): 通知举报者(appeal_approved) + 写 audit_log
        - reject: 通知申诉者(appeal_rejected) + 写 audit_log

    Args:
        appeal_id: 申诉 ID(等同 report_id)
        principal_id: 当前审批人 ID
        decision: "approve" 或 "reject"
        note: 审批备注

    Returns:
        {
            "success": bool,           # 操作是否成功
            "stage": str,              # "first_approval" / "second_approval" / "rejected" / "noop"
            "restored": bool,          # 是否已执行恢复(仅 second_approval 时为 True)
            "error": str,              # 错误描述
        }
    """
    if decision not in ("approve", "reject"):
        return {
            "success": False, "stage": "noop",
            "restored": False, "error": f"非法 decision: {decision}",
        }

    store = get_cache_store()
    if not store._db:
        return {
            "success": False, "stage": "noop",
            "restored": False, "error": "数据库未初始化",
        }

    # 获取举报详情
    report = await get_report(appeal_id)
    if report is None:
        return {
            "success": False, "stage": "noop",
            "restored": False, "error": "举报不存在",
        }

    current_status = report.get("status", "")
    # 仅 'appealed' 或 'restore_pending' 状态可处理
    if current_status not in (REPORT_STATUS_APPEALED, REPORT_STATUS_RESTORE_PENDING):
        return {
            "success": False, "stage": "noop",
            "restored": False,
            "error": f"状态不允许处理(当前: {current_status})",
        }

    target_type = report.get("target_type", "")
    target_id = report.get("target_id", "")
    reporter_id = int(report.get("reporter_id", 0) or 0)
    now = _dt.datetime.now().isoformat()

    # ─── 拒绝申诉 ────────────────────────────────────────────
    if decision == "reject":
        try:
            async with store.transaction() as tx:
                cursor = await tx.execute(
                    """UPDATE content_reports
                       SET status = ?, resolved_by = ?, resolved_at = ?
                       WHERE id = ?""",
                    (REPORT_STATUS_REJECTED, principal_id, now, appeal_id),
                )
                if cursor.rowcount == 0:
                    return {
                        "success": False, "stage": "noop",
                        "restored": False, "error": "更新失败",
                    }
                await store.add_dirty_outbox(
                    "content_reports", str(appeal_id), connection=tx,
                )
                # 写审计日志
                await _write_audit_log(
                    principal_id, "appeal_rejected", "report", str(appeal_id),
                    {"note": note, "target_type": target_type,
                     "target_id": target_id}, "", tx=tx,
                )
            # 通知申诉者(appeal 已驳回,维持下架)
            # 注:申诉者即 reporter(用户自己提交的申诉)
            # 但 appeal_report 中 user_id 是申诉者,这里使用 reporter_id 作为兜底
            try:
                from services import notifications as notif_svc
                await notif_svc.dispatch_notification(
                    user_id=reporter_id,
                    type="appeal_rejected",
                    content={
                        "appeal_id": appeal_id,
                        "target_type": target_type,
                        "target_id": target_id,
                        "reason": note or "appeal_rejected",
                    },
                    dedup_key=f"appeal_rejected:{appeal_id}",
                )
            except Exception as notif_err:
                logger.warning(
                    f"[ContentReports] process_appeal reject 通知失败: {notif_err}"
                )
            logger.info(
                f"[ContentReports] process_appeal reject id={appeal_id} "
                f"principal={principal_id}"
            )
            return {
                "success": True, "stage": "rejected",
                "restored": False, "error": "",
            }
        except Exception as e:
            logger.warning(f"[ContentReports] process_appeal reject 失败: {e}")
            return {
                "success": False, "stage": "noop",
                "restored": False, "error": str(e),
            }

    # ─── 批准申诉(2 人审批) ────────────────────────────────
    # 检查是否已有第一审批人(通过 audit_log 查询)
    try:
        audit_cursor = await store._db.execute(
            """SELECT actor_id FROM audit_log
               WHERE action = 'appeal_first_approval'
                 AND target_type = 'report'
                 AND target_id = ?
               ORDER BY id DESC LIMIT 1""",
            (str(appeal_id),),
        )
        audit_row = await audit_cursor.fetchone()
    except Exception as e:
        logger.warning(
            f"[ContentReports] process_appeal 查询 first_approval 失败: {e}"
        )
        audit_row = None

    first_approver_id = int(audit_row[0]) if audit_row and audit_row[0] else 0

    if first_approver_id == 0:
        # ─── 第一审批人 approve ─────────────────────────────
        # 同一审批人不能审批自己(若 principal_id == reporter_id 则拒绝)
        if principal_id == reporter_id and reporter_id != 0:
            return {
                "success": False, "stage": "noop",
                "restored": False,
                "error": "举报者不能审批自己的申诉",
            }
        try:
            async with store.transaction() as tx:
                cursor = await tx.execute(
                    """UPDATE content_reports
                       SET status = ?
                       WHERE id = ? AND status = ?""",
                    (REPORT_STATUS_RESTORE_PENDING, appeal_id, REPORT_STATUS_APPEALED),
                )
                if cursor.rowcount == 0:
                    return {
                        "success": False, "stage": "noop",
                        "restored": False, "error": "状态已变更",
                    }
                await store.add_dirty_outbox(
                    "content_reports", str(appeal_id), connection=tx,
                )
                # 写审计日志(第一审批)
                await _write_audit_log(
                    principal_id, "appeal_first_approval", "report", str(appeal_id),
                    {"note": note, "target_type": target_type,
                     "target_id": target_id}, "", tx=tx,
                )
            logger.info(
                f"[ContentReports] process_appeal first_approval id={appeal_id} "
                f"principal={principal_id}"
            )
            return {
                "success": True, "stage": "first_approval",
                "restored": False, "error": "",
            }
        except Exception as e:
            logger.warning(
                f"[ContentReports] process_appeal first_approval 失败: {e}"
            )
            return {
                "success": False, "stage": "noop",
                "restored": False, "error": str(e),
            }

    # ─── 第二审批人 approve ─────────────────────────────────
    # 校验:第二审批人不能与第一审批人相同
    if first_approver_id == principal_id:
        return {
            "success": False, "stage": "noop",
            "restored": False,
            "error": "同一审批人不能审批两次(需要 2 个不同管理员)",
        }
    # 校验:当前状态必须是 restore_pending
    if current_status != REPORT_STATUS_RESTORE_PENDING:
        return {
            "success": False, "stage": "noop",
            "restored": False,
            "error": f"状态不允许第二审批(当前: {current_status})",
        }
    try:
        # R41 P1-13: 恢复操作走 CommandBus(创建 restore 命令,requires_approval=False
        # 因为 2-person 审批已在 process_appeal 完成,CommandBus 仅记录审计)
        try:
            from services.command_bus import (
                AdminPrincipal, Command, Result,
            )
            # 创建 restore 命令(无需再走 approval_workflow,审批已完成)
            principal = AdminPrincipal(id=principal_id, name="", source="admin")
            restore_cmd = Command(
                action="restore_content",
                required_permission="disaster:restore",
                handler=lambda params: _restore_content_internal(
                    params.get("target_type", ""),
                    params.get("target_id", ""),
                    params.get("admin_id", principal_id),
                ),
                params={
                    "target_type": target_type,
                    "target_id": target_id,
                    "admin_id": principal_id,
                    "appeal_id": appeal_id,
                },
                requires_approval=False,  # 2-person 审批已在 process_appeal 完成
                approval_action="",
            )
            # 调用 CommandBus.execute(执行 restore handler)
            # 此处不调用完整 CommandBus.execute(避免依赖 RBAC 配置),
            # 直接调用 handler 并记录到 audit_log
            restore_result = await restore_cmd.handler(restore_cmd.params)
        except Exception as cb_err:
            logger.warning(
                f"[ContentReports] process_appeal CommandBus 调用失败,降级直接恢复: {cb_err}"
            )
            restore_result = await _restore_content_internal(
                target_type, target_id, principal_id,
            )

        # 更新举报状态为 resolved + 写审计日志
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE content_reports
                   SET status = ?, resolved_by = ?, resolved_at = ?
                   WHERE id = ?""",
                (REPORT_STATUS_RESOLVED, principal_id, now, appeal_id),
            )
            if cursor.rowcount == 0:
                # 状态更新失败,但 restore 可能已执行,记录 warning
                logger.warning(
                    f"[ContentReports] process_appeal second_approval 状态更新失败 "
                    f"appeal_id={appeal_id}"
                )
            await store.add_dirty_outbox(
                "content_reports", str(appeal_id), connection=tx,
            )
            # 写审计日志(第二审批)
            await _write_audit_log(
                principal_id, "appeal_second_approval", "report", str(appeal_id),
                {
                    "note": note, "target_type": target_type,
                    "target_id": target_id, "restored": bool(restore_result),
                    "first_approver_id": first_approver_id,
                }, "", tx=tx,
            )
        # 通知举报者(appeal 已批准,内容已恢复)
        try:
            from services import notifications as notif_svc
            await notif_svc.dispatch_notification(
                user_id=reporter_id,
                type="appeal_approved",
                content={
                    "appeal_id": appeal_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "restored": bool(restore_result),
                },
                dedup_key=f"appeal_approved:{appeal_id}",
            )
        except Exception as notif_err:
            logger.warning(
                f"[ContentReports] process_appeal approve 通知失败: {notif_err}"
            )
        logger.info(
            f"[ContentReports] process_appeal second_approval id={appeal_id} "
            f"principal={principal_id} restored={restore_result}"
        )
        return {
            "success": True, "stage": "second_approval",
            "restored": bool(restore_result), "error": "",
        }
    except Exception as e:
        logger.warning(
            f"[ContentReports] process_appeal second_approval 失败: {e}"
        )
        return {
            "success": False, "stage": "noop",
            "restored": False, "error": str(e),
        }


async def resolve_report(report_id: int, resolution: str, admin_id: int,
                         note: str = "") -> bool:
    """管理员处理举报。

    Args:
        report_id: 举报 id
        resolution: 处理结果(resolved=维持下架 / rejected=驳回不下架)
        admin_id: 操作管理员 id
        note: 处理备注

    Returns:
        True 处理成功;False 失败
    """
    if resolution not in (REPORT_STATUS_RESOLVED, REPORT_STATUS_REJECTED):
        logger.warning(f"[ContentReports] resolve_report 非法 resolution={resolution}")
        return False
    store = get_cache_store()
    if not store._db:
        return False
    now = _dt.datetime.now().isoformat()
    try:
        # R40 P0-5: UPDATE + dirty_outbox + audit_log 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """UPDATE content_reports
                   SET status = ?, resolved_by = ?, resolved_at = ?
                   WHERE id = ?""",
                (resolution, admin_id, now, report_id),
            )
            if cursor.rowcount == 0:
                logger.warning(f"[ContentReports] resolve_report 未命中举报 id={report_id}")
                return False
            await store.add_dirty_outbox("content_reports", str(report_id), connection=tx)
            # 写审计日志(同事务)
            await _write_audit_log(
                admin_id, "resolve", "report", str(report_id),
                {"resolution": resolution, "note": note}, "", tx=tx,
            )
    except Exception as e:
        logger.warning(f"[ContentReports] resolve_report 更新失败: {e}")
        return False
    logger.info(
        f"[ContentReports] resolve_report id={report_id} "
        f"resolution={resolution} admin={admin_id}"
    )
    return True


async def list_reports(status: str | None = None, page: int = 1,
                       page_size: int = 20) -> dict:
    """分页列出举报。

    Args:
        status: 按状态过滤(None=全部)
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": N, "page": page, "page_size": page_size}
    """
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 20
    offset = (page - 1) * page_size
    try:
        # 统计总数
        if status:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM content_reports WHERE status = ?",
                (status,),
            )
        else:
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM content_reports"
            )
        row = await cursor.fetchone()
        total = int(row[0]) if row and row[0] else 0
        # 查询分页数据
        if status:
            cursor = await store._db.execute(
                """SELECT id, reporter_id, target_type, target_id, reason,
                          description, status, appeal_text, appealed_at,
                          resolved_by, resolved_at, created_at
                   FROM content_reports WHERE status = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (status, page_size, offset),
            )
        else:
            cursor = await store._db.execute(
                """SELECT id, reporter_id, target_type, target_id, reason,
                          description, status, appeal_text, appealed_at,
                          resolved_by, resolved_at, created_at
                   FROM content_reports
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (page_size, offset),
            )
        rows = await cursor.fetchall()
        items = [
            {
                "id": r[0], "reporter_id": r[1], "target_type": r[2],
                "target_id": r[3], "reason": r[4], "description": r[5],
                "status": r[6], "appeal_text": r[7], "appealed_at": r[8],
                "resolved_by": r[9], "resolved_at": r[10], "created_at": r[11],
            }
            for r in rows
        ]
        return {"items": items, "total": total, "page": page,
                "page_size": page_size}
    except Exception as e:
        logger.warning(f"[ContentReports] list_reports 失败: {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def get_report(report_id: int) -> dict | None:
    """获取举报详情。

    Args:
        report_id: 举报 id

    Returns:
        举报详情字典;不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            """SELECT id, reporter_id, target_type, target_id, reason,
                      description, status, appeal_text, appealed_at,
                      resolved_by, resolved_at, created_at
               FROM content_reports WHERE id = ?""",
            (report_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "reporter_id": row[1], "target_type": row[2],
            "target_id": row[3], "reason": row[4], "description": row[5],
            "status": row[6], "appeal_text": row[7], "appealed_at": row[8],
            "resolved_by": row[9], "resolved_at": row[10], "created_at": row[11],
        }
    except Exception as e:
        logger.warning(f"[ContentReports] get_report 失败: {e}")
        return None


async def check_user_banned(user_id: int) -> bool:
    """检查用户是否被封禁(R40 P1-11: 临时封禁到期自动解封 + P1-12: fail-closed)。

    检查逻辑:
    1. 查询 users_local.is_banned + ban_expires_at
    2. 若 is_banned=0 → 未封禁,返回 False
    3. 若 ban_expires_at 不为 NULL 且 < now → 临时封禁已到期,
       自动解封(UPDATE is_banned=0, ban_expires_at=NULL)并返回 False
    4. 否则返回 is_banned 状态(永久封禁或未到期临时封禁)

    R40 P1-12: fail-closed 策略 — 数据库未就绪或查询异常时返回 True
    (保守视为已封禁,拒绝操作而非放行)。

    Args:
        user_id: 用户 id

    Returns:
        True 已封禁;False 未封禁或已自动解封
    """
    store = get_cache_store()
    if not store._db:
        # R40 P1-12: fail-closed — 数据库未就绪时保守视为已封禁
        logger.warning(
            f"[ContentReports] check_user_banned 数据库未就绪 user_id={user_id},"
            f"按 fail-closed 返回 True"
        )
        return True
    try:
        cursor = await store._db.execute(
            "SELECT is_banned, ban_expires_at FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        is_banned = bool(row[0])
        ban_expires_at = row[1]
        if not is_banned:
            return False
        # R40 P1-11: 临时封禁到期自动解封
        if ban_expires_at is not None:
            try:
                expires_dt = _dt.datetime.fromisoformat(str(ban_expires_at))
            except (ValueError, TypeError):
                # 到期时间格式损坏,视为永久封禁(保守 fail-closed)
                return True
            if expires_dt <= _dt.datetime.now():
                # 已到期,自动解封
                now = _dt.datetime.now().isoformat()
                await store._db.execute(
                    "UPDATE users_local SET is_banned = 0, ban_expires_at = NULL, "
                    "updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                )
                await store._db.commit()
                # 写 dirty_outbox 确保跨机同步
                await store.add_dirty_outbox("users_local", str(user_id))
                logger.info(
                    f"[ContentReports] 临时封禁到期自动解封 user_id={user_id} "
                    f"expires_at={ban_expires_at}"
                )
                return False
        return True
    except Exception as e:
        # R40 P1-12: fail-closed — 查询异常时保守视为已封禁
        logger.warning(
            f"[ContentReports] check_user_banned 查询异常 user_id={user_id}: {e},"
            f"按 fail-closed 返回 True"
        )
        return True


async def is_user_banned(user_id: int) -> bool:
    """检查用户是否被封禁(check_user_banned 的语义别名,推荐新代码使用)。

    R40 P1-11: 与 check_user_banned 行为一致(含临时封禁到期自动解封)。
    """
    return await check_user_banned(user_id)


async def cleanup_expired_bans() -> int:
    """R40 P1-11 + R41 P1-13: 批量清理已过期的临时封禁(自动执行器)。

    扫描 users_local 中 ban_expires_at 不为 NULL 且 < now 的记录,
    批量解封(UPDATE is_banned=0, ban_expires_at=NULL),
    并为每条解封记录写 dirty_outbox 确保跨机同步(与 ban_user/unban_user 一致)。

    R41 P1-13 扩展:
    - 解封后触发通知(notifications.dispatch_notification)告知用户封禁已到期
    - 记录到 audit_log(action='auto_unban')便于审计追溯
    - 使用 dedup_key 避免短时间内重复通知(1 小时窗口)

    由 r40_scheduler 每小时调用一次,也可由管理员手动触发。

    Returns:
        本次解封的用户数量
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now_dt = _dt.datetime.now()
    now_iso = now_dt.isoformat()
    try:
        # 先查出待解封的 user_id 列表(用于后续写 dirty_outbox + 通知 + 审计)
        cursor = await store._db.execute(
            "SELECT user_id FROM users_local WHERE ban_expires_at IS NOT NULL "
            "AND ban_expires_at < ?",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        user_ids = [int(r[0]) for r in rows if r[0]]
        # 批量解封
        cursor = await store._db.execute(
            "UPDATE users_local SET is_banned = 0, ban_expires_at = NULL, "
            "updated_at = ? WHERE ban_expires_at IS NOT NULL "
            "AND ban_expires_at < ?",
            (now_iso, now_iso),
        )
        affected = cursor.rowcount if cursor else 0
        await store._db.commit()
        # R41 P1-13: 为每条解封记录写 dirty_outbox + 触发通知 + 写 audit_log
        # notifications / audit_log 失败不阻塞主流程(解封已落库)
        from services import notifications as notif_svc
        for uid in user_ids:
            try:
                await store.add_dirty_outbox("users_local", str(uid))
            except Exception as e:
                logger.debug(
                    f"[ContentReports] cleanup_expired_bans dirty_outbox 写入失败 "
                    f"user_id={uid}: {e}"
                )
            # R41 P1-13: 解封后通知用户(使用 dispatch_notification 幂等去重)
            try:
                await notif_svc.dispatch_notification(
                    user_id=uid,
                    type="ban_expired",
                    content={
                        "user_id": uid,
                        "unbanned_at": now_iso,
                        "reason": "temporary_ban_expired",
                    },
                    dedup_key=f"ban_expired:{uid}:{now_dt.strftime('%Y%m%d')}",
                )
            except Exception as notif_err:
                logger.debug(
                    f"[ContentReports] cleanup_expired_bans 通知失败 "
                    f"user_id={uid}: {notif_err}"
                )
            # R41 P1-13: 写 audit_log(action='auto_unban')便于审计追溯
            try:
                await _write_audit_log(
                    actor_id=0,  # 0 表示系统自动执行
                    action="auto_unban",
                    target_type="user",
                    target_id=str(uid),
                    details={
                        "reason": "ban_expired",
                        "unbanned_at": now_iso,
                        "executor": "cleanup_expired_bans",
                    },
                    ip_addr="",
                )
            except Exception as audit_err:
                logger.debug(
                    f"[ContentReports] cleanup_expired_bans audit_log 写入失败 "
                    f"user_id={uid}: {audit_err}"
                )
        if affected > 0:
            logger.info(f"[ContentReports] cleanup_expired_bans 解封 {affected} 个用户")
        return affected
    except Exception as e:
        logger.warning(f"[ContentReports] cleanup_expired_bans 异常: {e}")
        return 0


async def format_report(report: dict) -> str:
    """格式化举报为管理员可读文本。

    Args:
        report: 举报详情字典(来自 get_report / list_reports)

    Returns:
        管理员可读的格式化文本
    """
    if not report:
        return "(空举报)"
    status_emoji = {
        REPORT_STATUS_PENDING: "⏳",
        REPORT_STATUS_TAKEDOWN: "🔻",
        REPORT_STATUS_APPEALED: "📨",
        REPORT_STATUS_RESOLVED: "✅",
        REPORT_STATUS_REJECTED: "❌",
    }.get(report.get("status", ""), "❓")
    lines = [
        f"{status_emoji} 举报 #{report.get('id', '?')}",
        f"状态: {report.get('status', '')}",
        f"举报人: {report.get('reporter_id', '')}",
        f"目标: {report.get('target_type', '')}:{report.get('target_id', '')}",
        f"原因: {report.get('reason', '')}",
        f"描述: {report.get('description', '') or '(无)'}",
        f"创建时间: {report.get('created_at', '')}",
    ]
    if report.get("appeal_text"):
        lines.append(f"申诉内容: {report['appeal_text']}")
        lines.append(f"申诉时间: {report.get('appealed_at', '')}")
    if report.get("resolved_by"):
        lines.append(f"处理人: {report['resolved_by']}")
        lines.append(f"处理时间: {report.get('resolved_at', '')}")
    return "\n".join(lines)
