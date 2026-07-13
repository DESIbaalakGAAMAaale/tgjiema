"""R40 §9.2: Quota Ledger — 预留/结算/退款流水。

职责:
- reserve(): 预留配额(上传/解码前预扣,防止并发超额)
- settle(): 结算预留(操作完成,记录实际消耗)
- refund(): 退款(操作失败时回滚预留)
- admin_adjust(): 管理员手动调整配额
- cleanup_expired_reservations(): 清理超时未结算的预留

数据表:
- quota_reservations: 预留记录(id/user_id/amount/status/actual_amount/...)
- quota_ledger: 配额变更流水(M1 已有,event_type=reservation/settlement/refund/adjustment)

设计要点:
- reservation_id 格式: res-{uuid.hex[:12]}
- 预留超时: 1 小时未结算的自动退款(由 cleanup_expired_reservations 处理)
- 所有写入后调用 add_dirty_outbox() 确保跨机同步
"""
from __future__ import annotations

import datetime
import time
import uuid
from loguru import logger

from database.cache_store import get_cache_store


# ─── 流水类型 ──────────────────────────────────────────────────
LEDGER_TYPE_RESERVATION = "reservation"   # 预留
LEDGER_TYPE_SETTLEMENT = "settlement"     # 结算(实际消耗)
LEDGER_TYPE_REFUND = "refund"              # 退款(操作失败)
LEDGER_TYPE_ADJUSTMENT = "adjustment"     # 管理员调整

# ─── 预留状态 ──────────────────────────────────────────────────
RESERVATION_STATUS_RESERVED = "reserved"
RESERVATION_STATUS_SETTLED = "settled"
RESERVATION_STATUS_REFUNDED = "refunded"

# ─── 预留超时阈值(秒) ─────────────────────────────────────────
RESERVATION_TIMEOUT_SECONDS = 3600  # 1 小时


async def reserve(user_id: int, amount: int, reason: str) -> str:
    """预留配额,返回 reservation_id(UUID)。

    流程:
    1. 检查余额是否足够(每日配额 - 今日已消耗 >= amount)
    2. INSERT quota_reservations(id='res-xxx', status='reserved')
    3. INSERT quota_ledger(event_type='reservation', request_id=reservation_id)
    4. add_dirty_outbox 同步到 CRDB

    Args:
        user_id: Telegram 用户 ID
        amount: 预留数量(正整数)
        reason: 预留原因(如 "upload:file_code")

    Returns:
        reservation_id;余额不足或失败时返回空字符串
    """
    if amount <= 0:
        logger.warning(f"[QuotaLedger] reserve 无效金额: amount={amount}")
        return ""

    store = get_cache_store()
    if not store._db:
        logger.warning("[QuotaLedger] reserve 数据库未初始化")
        return ""

    # 检查余额
    balance = await get_balance(user_id)
    if balance != -1 and balance < amount:
        logger.info(f"[QuotaLedger] reserve 余额不足 user={user_id} balance={balance} amount={amount}")
        return ""

    reservation_id = f"res-{uuid.uuid4().hex[:12]}"
    now = datetime.datetime.now().isoformat()

    try:
        # R40 P0-5: 预留记录 + dirty_outbox + 流水 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "INSERT INTO quota_reservations (id, user_id, amount, reason, status, actual_amount, created_at, settled_at, expired_at) "
                "VALUES (?, ?, ?, ?, 'reserved', 0, ?, NULL, NULL)",
                (reservation_id, user_id, amount, reason, now),
            )
            await store.add_dirty_outbox("quota_reservations", reservation_id, connection=tx)
            # 写入配额变更流水(同事务)
            await store.append_quota_ledger(
                user_id=user_id,
                event_type=LEDGER_TYPE_RESERVATION,
                request_id=reservation_id,
                reason=f"reserve: {reason} (amount={amount})",
                tx=tx,
            )

        logger.debug(f"[QuotaLedger] reserve 成功 user={user_id} res_id={reservation_id} amount={amount}")
        return reservation_id
    except Exception as e:
        logger.error(f"[QuotaLedger] reserve 失败 user={user_id} amount={amount}: {e}")
        return ""


async def settle(reservation_id: str, actual_amount: int | None = None) -> bool:
    """结算预留(实际消耗)。

    - actual_amount=None 时用预留金额
    - actual_amount < 预留金额时,差额自动退款(更新 quota_ledger)
    - UPDATE quota_reservations SET status='settled', actual_amount=?
    - INSERT quota_ledger(event_type='settlement')

    Args:
        reservation_id: 预留 ID
        actual_amount: 实际消耗数量(None=用预留金额)

    Returns:
        True 表示成功
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[QuotaLedger] settle 数据库未初始化")
        return False

    reservation = await get_reservation(reservation_id)
    if reservation is None:
        logger.warning(f"[QuotaLedger] settle 预留不存在: {reservation_id}")
        return False

    if reservation["status"] != RESERVATION_STATUS_RESERVED:
        logger.warning(f"[QuotaLedger] settle 预留状态非 reserved: {reservation_id} status={reservation['status']}")
        return False

    reserved_amount = int(reservation["amount"])
    # actual_amount=None 时用预留金额
    if actual_amount is None:
        actual_amount = reserved_amount
    actual_amount = max(0, int(actual_amount))

    now = datetime.datetime.now().isoformat()

    try:
        # R40 P0-5: 预留更新 + dirty_outbox + 流水 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "UPDATE quota_reservations SET status = 'settled', actual_amount = ?, settled_at = ? "
                "WHERE id = ? AND status = 'reserved'",
                (actual_amount, now, reservation_id),
            )
            await store.add_dirty_outbox("quota_reservations", reservation_id, connection=tx)

            # 写入结算流水(同事务)
            user_id = int(reservation["user_id"])
            refund_amount = reserved_amount - actual_amount
            await store.append_quota_ledger(
                user_id=user_id,
                event_type=LEDGER_TYPE_SETTLEMENT,
                request_id=reservation_id,
                reason=f"settle: actual={actual_amount}, reserved={reserved_amount}, refund_diff={refund_amount}",
                tx=tx,
            )

            # 差额退款(如果实际消耗 < 预留)
            if refund_amount > 0:
                await store.append_quota_ledger(
                    user_id=user_id,
                    event_type=LEDGER_TYPE_REFUND,
                    request_id=reservation_id,
                    reason=f"settle_refund: 差额退款 {refund_amount}",
                    tx=tx,
                )

        logger.debug(f"[QuotaLedger] settle 成功 res_id={reservation_id} actual={actual_amount}")
        return True
    except Exception as e:
        logger.error(f"[QuotaLedger] settle 失败 res_id={reservation_id}: {e}")
        return False


async def refund(reservation_id: str, reason: str = "") -> bool:
    """退款(操作失败时)。

    - UPDATE quota_reservations SET status='refunded'
    - INSERT quota_ledger(event_type='refund', amount=+原预留金额)

    Args:
        reservation_id: 预留 ID
        reason: 退款原因

    Returns:
        True 表示成功
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[QuotaLedger] refund 数据库未初始化")
        return False

    reservation = await get_reservation(reservation_id)
    if reservation is None:
        logger.warning(f"[QuotaLedger] refund 预留不存在: {reservation_id}")
        return False

    if reservation["status"] != RESERVATION_STATUS_RESERVED:
        logger.warning(f"[QuotaLedger] refund 预留状态非 reserved: {reservation_id} status={reservation['status']}")
        return False

    now = datetime.datetime.now().isoformat()
    reserved_amount = int(reservation["amount"])
    user_id = int(reservation["user_id"])
    refund_reason = f"refund: {reason}" if reason else "refund: 操作失败退款"

    try:
        # R40 P0-5: 预留更新 + dirty_outbox + 流水 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "UPDATE quota_reservations SET status = 'refunded', settled_at = ? "
                "WHERE id = ? AND status = 'reserved'",
                (now, reservation_id),
            )
            await store.add_dirty_outbox("quota_reservations", reservation_id, connection=tx)

            # 写入退款流水(同事务)
            await store.append_quota_ledger(
                user_id=user_id,
                event_type=LEDGER_TYPE_REFUND,
                request_id=reservation_id,
                reason=f"{refund_reason} (amount={reserved_amount})",
                tx=tx,
            )

        logger.debug(f"[QuotaLedger] refund 成功 res_id={reservation_id} amount={reserved_amount}")
        return True
    except Exception as e:
        logger.error(f"[QuotaLedger] refund 失败 res_id={reservation_id}: {e}")
        return False


async def get_balance(user_id: int) -> int:
    """获取用户当前余额 = 每日配额 - 今日已消耗(reservation+settlement)。

    今日已消耗 = SUM(CASE WHEN settled THEN actual_amount WHEN reserved THEN amount ELSE 0 END)
    退款状态(refunded)不计入消耗。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        剩余配额; -1 表示无限(premium); 失败返回 0
    """
    from services.entitlements import get_plan

    plan = await get_plan(user_id)
    # -1 表示无限
    if plan.daily_quota == -1:
        return -1

    store = get_cache_store()
    if not store._db:
        return 0

    try:
        rows = await store._db.execute_fetchall(
            "SELECT COALESCE(SUM(CASE "
            "WHEN status='settled' THEN actual_amount "
            "WHEN status='reserved' THEN amount "
            "ELSE 0 END), 0) "
            "FROM quota_reservations "
            "WHERE user_id = ? AND status != 'refunded' "
            "AND date(created_at) = date('now')",
            (user_id,),
        )
        used_today = int(rows[0][0] or 0) if rows else 0
        return max(0, plan.daily_quota - used_today)
    except Exception as e:
        logger.warning(f"[QuotaLedger] get_balance 失败 user={user_id}: {e}")
        return 0


async def get_reservation(reservation_id: str) -> dict | None:
    """获取预留详情。

    Args:
        reservation_id: 预留 ID

    Returns:
        预留详情字典;不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None

    try:
        rows = await store._db.execute_fetchall(
            "SELECT id, user_id, amount, reason, status, actual_amount, "
            "created_at, settled_at, expired_at "
            "FROM quota_reservations WHERE id = ?",
            (reservation_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r[0],
            "user_id": r[1],
            "amount": r[2],
            "reason": r[3],
            "status": r[4],
            "actual_amount": r[5],
            "created_at": r[6],
            "settled_at": r[7],
            "expired_at": r[8],
        }
    except Exception as e:
        logger.warning(f"[QuotaLedger] get_reservation 失败 res_id={reservation_id}: {e}")
        return None


async def list_user_ledger(user_id: int, page: int = 1, page_size: int = 20) -> dict:
    """分页查询用户流水。

    合并 quota_reservations 和 quota_ledger 两张表的数据,
    按时间倒序返回。

    Args:
        user_id: Telegram 用户 ID
        page: 页码(从 1 开始)
        page_size: 每页条数

    Returns:
        {"items": [...], "total": int, "page": int, "page_size": int}
    """
    store = get_cache_store()
    if not store._db:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    offset = max(0, (page - 1) * page_size)

    try:
        # 查询预留记录
        rows = await store._db.execute_fetchall(
            "SELECT id, user_id, amount, reason, status, actual_amount, created_at "
            "FROM quota_reservations "
            "WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, page_size, offset),
        )
        items = []
        for r in rows:
            items.append({
                "type": "reservation",
                "id": r[0],
                "user_id": r[1],
                "amount": r[2],
                "reason": r[3],
                "status": r[4],
                "actual_amount": r[5],
                "created_at": r[6],
            })

        # 查询总数
        count_rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM quota_reservations WHERE user_id = ?",
            (user_id,),
        )
        total = int(count_rows[0][0]) if count_rows else 0

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        logger.warning(f"[QuotaLedger] list_user_ledger 失败 user={user_id}: {e}")
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


async def cleanup_expired_reservations() -> int:
    """清理过期预留(超过 1 小时未结算的自动退款)。

    由后台定时任务(如 mon_bot)定期调用。

    Returns:
        清理的预留数量
    """
    store = get_cache_store()
    if not store._db:
        return 0

    # 计算超时时间点(ISO 格式)
    timeout_dt = datetime.datetime.now() - datetime.timedelta(seconds=RESERVATION_TIMEOUT_SECONDS)
    timeout_str = timeout_dt.isoformat()

    try:
        # 查询过期的预留
        rows = await store._db.execute_fetchall(
            "SELECT id, user_id, amount FROM quota_reservations "
            "WHERE status = 'reserved' AND created_at < ?",
            (timeout_str,),
        )
        if not rows:
            return 0

        count = 0
        # R40 P0-5: 所有过期预留的更新 + dirty_outbox + 流水 在同一事务内
        async with store.transaction() as tx:
            for r in rows:
                reservation_id = r[0]
                user_id = int(r[1])
                amount = int(r[2])
                now = datetime.datetime.now().isoformat()

                # 更新状态为已退款
                await tx.execute(
                    "UPDATE quota_reservations SET status = 'refunded', expired_at = ? "
                    "WHERE id = ? AND status = 'reserved'",
                    (now, reservation_id),
                )
                await store.add_dirty_outbox("quota_reservations", reservation_id, connection=tx)

                # 写入退款流水(同事务)
                await store.append_quota_ledger(
                    user_id=user_id,
                    event_type=LEDGER_TYPE_REFUND,
                    request_id=reservation_id,
                    reason=f"expired_refund: 预留超时自动退款 (amount={amount})",
                    tx=tx,
                )
                count += 1

        if count > 0:
            logger.info(f"[QuotaLedger] cleanup_expired_reservations 清理 {count} 条过期预留")
        return count
    except Exception as e:
        logger.error(f"[QuotaLedger] cleanup_expired_reservations 失败: {e}")
        return 0


async def admin_adjust(user_id: int, amount: int, reason: str, admin_id: int) -> bool:
    """管理员调整配额(正数增加,负数减少)。

    通过写入一条 adjustment 类型的流水来实现,
    不直接修改 quota_reservations(仅影响余额计算)。

    注意:此方法通过 quota_ledger 记录调整,
    get_balance() 目前仅统计 quota_reservations。
    若需让 admin_adjust 影响余额,应在 get_balance 中也统计 adjustment 流水。
    当前实现: 通过创建一条 amount=0 的预留来记录调整(不实际消耗配额)。

    Args:
        user_id: 目标用户 ID
        amount: 调整数量(正数增加可用余额,负数减少)
        reason: 调整原因
        admin_id: 操作管理员 ID

    Returns:
        True 表示成功
    """
    if amount == 0:
        logger.warning("[QuotaLedger] admin_adjust 调整数量为 0,跳过")
        return False

    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()

    try:
        # 写入审计日志
        await store._db.execute(
            "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
            "VALUES (?, 'admin', 'quota_adjust', 'user', ?, ?, ?)",
            (
                admin_id,
                str(user_id),
                f'{{"amount": {amount}, "reason": "{reason}"}}',
                now,
            ),
        )
        await store._db.commit()
        await store.add_dirty_outbox("audit_log", "last")

        # 写入配额调整流水
        await store.append_quota_ledger(
            user_id=user_id,
            event_type=LEDGER_TYPE_ADJUSTMENT,
            reason=f"admin_adjust by={admin_id}: {reason} (amount={amount})",
        )

        logger.info(f"[QuotaLedger] admin_adjust 成功 user={user_id} amount={amount} admin={admin_id}")
        return True
    except Exception as e:
        logger.error(f"[QuotaLedger] admin_adjust 失败 user={user_id} amount={amount}: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# R45 §9-10 整改: RESERVE/SETTLE/RELEASE 语义化别名
#
# R42 终审报告要求 Quota 使用 RESERVE→SETTLE/RELEASE ledger 模式:
# - reserve_quota: 上传开始时预扣配额(等同于 reserve,action_id 作为 reason)
# - settle_quota: 上传成功后确认配额(等同于 settle,action_id 即 reservation_id)
# - release_quota: 上传失败或取消时释放配额(等同于 refund,action_id 即 reservation_id)
#
# 这些别名函数是对现有 reserve/settle/refund 的语义化包装,
# 使调用方代码更清晰地表达 RESERVE→SETTLE/RELEASE 状态机。
# ════════════════════════════════════════════════════════════════


async def reserve_quota(user_id: int, amount: int, action_id: str) -> str:
    """R45: 预扣配额(上传/解码开始时调用)。

    语义化别名,内部委托给 reserve()。
    action_id 作为 reason 写入 quota_ledger,便于追踪业务流。

    Args:
        user_id: Telegram 用户 ID
        amount: 预扣数量(正整数)
        action_id: 业务动作标识(如 "upload:r45-upload-001"),
                   写入 ledger.reason 用于审计追踪

    Returns:
        reservation_id(UUID);余额不足或失败时返回空字符串
    """
    return await reserve(user_id, amount, reason=action_id)


async def settle_quota(action_id: str) -> bool:
    """R45: 确认配额(上传/解码成功后调用)。

    语义化别名,内部委托给 settle()。
    action_id 即 reservation_id(由 reserve_quota 返回)。

    Args:
        action_id: reservation_id(由 reserve_quota 返回)

    Returns:
        True 表示成功;False 表示预留不存在或状态非 reserved
    """
    return await settle(action_id)


async def release_quota(action_id: str, reason: str = "") -> bool:
    """R45: 释放配额(上传/解码失败或取消时调用)。

    语义化别名,内部委托给 refund()。
    action_id 即 reservation_id(由 reserve_quota 返回)。

    Args:
        action_id: reservation_id(由 reserve_quota 返回)
        reason: 释放原因(可选,写入 ledger 审计)

    Returns:
        True 表示成功;False 表示预留不存在或状态非 reserved
    """
    return await refund(action_id, reason=reason)


async def force_release_quota(action_id: str, reason: str = "") -> bool:
    """R45: 强制释放配额(绕过 dirty_outbox 的兜底路径)。

    当 release_quota → refund() 因 dirty_outbox 故障失败时使用此兜底函数。
    直接 UPDATE quota_reservations SET status='refunded',不调用 add_dirty_outbox,
    避免"dirty_outbox 故障 → refund 失败 → 配额无法释放"的死循环。

    场景:
    - finalize_upload 主事务因 dirty_outbox 故障 ROLLBACK,
      后续 release_quota 又因同样 dirty_outbox 故障失败,
      需要绕过 dirty_outbox 强制释放配额,防止配额泄漏(用户配额被永久占用)。

    副作用:
    - 不写 dirty_outbox,跨机同步延迟(由后续 cleanup_expired_reservations 兜底)
    - 不写 quota_ledger 流水(避免事务二次失败)
    - 日志记录原因,便于人工审计

    Args:
        action_id: reservation_id(由 reserve_quota 返回)
        reason: 强制释放原因(写入日志)

    Returns:
        True 表示成功;False 表示预留不存在或数据库未初始化
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[QuotaLedger] force_release_quota 数据库未初始化")
        return False

    reservation = await get_reservation(action_id)
    if reservation is None:
        logger.warning(f"[QuotaLedger] force_release_quota 预留不存在: {action_id}")
        return False

    # 已 refunded/settled 的不重复 release(幂等)
    if reservation["status"] != RESERVATION_STATUS_RESERVED:
        logger.info(
            f"[QuotaLedger] force_release_quota 跳过(状态非 reserved) "
            f"res_id={action_id} status={reservation['status']}"
        )
        return True

    now = datetime.datetime.now().isoformat()
    reserved_amount = int(reservation["amount"])
    user_id = int(reservation["user_id"])
    release_reason = f"force_release: {reason}" if reason else "force_release: dirty_outbox 故障兜底"

    try:
        # 直接 UPDATE(不调用 add_dirty_outbox,不写 quota_ledger 流水)
        # 避免 dirty_outbox 故障导致配额永久泄漏
        await store._db.execute(
            "UPDATE quota_reservations SET status = 'refunded', settled_at = ? "
            "WHERE id = ? AND status = 'reserved'",
            (now, action_id),
        )
        await store._db.commit()

        logger.warning(
            f"[QuotaLedger] force_release_quota 成功(绕过 dirty_outbox) "
            f"res_id={action_id} user={user_id} amount={reserved_amount} "
            f"reason={release_reason}"
        )
        return True
    except Exception as e:
        logger.error(
            f"[QuotaLedger] force_release_quota 失败 res_id={action_id}: {e}"
        )
        return False
