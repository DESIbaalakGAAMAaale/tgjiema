"""R40 §9.2: Entitlement Service — 统一套餐/配额/限制判定。

职责:
- 根据用户会员等级(free/basic/premium)判定可用配额、文件大小、并发数、保留期、队列优先级
- 统一入口 check() 供 Up/Idx/Dsp Bot 在上传/解码/合集等操作前调用
- 管理员可通过 set_user_plan() 动态调整用户套餐

数据来源:
- users_local.membership_level: 用户会员等级
- quota_reservations: 当日配额预留/消耗(与 quota_ledger 配合)
- user_quota: 外部解码配额(沿用 M1 机制)
- config/settings.py: 套餐默认值

设计要点:
- 纯函数式 + async,不持有状态
- 所有写入操作后调用 store.add_dirty_outbox() 确保跨机同步
- Plan/Quota/Limits/EntitlementResult 使用 dataclass,便于序列化和类型检查
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, asdict
from loguru import logger

from database.cache_store import get_cache_store
from config import settings
from services.error_codes import AppError, ErrorCodes


# ─── 会员等级常量 ──────────────────────────────────────────────
MEMBERSHIP_FREE = "free"
MEMBERSHIP_BASIC = "basic"
MEMBERSHIP_PREMIUM = "premium"


# ─── 数据结构定义 ──────────────────────────────────────────────

@dataclass
class Plan:
    """套餐定义"""
    name: str
    daily_quota: int           # 每日配额(-1=无限)
    external_daily_quota: int  # 外部解码配额(-1=无限, 0=不允许)
    max_file_size: int         # 单文件大小上限(bytes)
    max_concurrent: int        # 最大并发上传
    retention_days: int        # 保留期(天)
    priority_queue: str        # 队列优先级: high/normal/low
    max_collection_items: int  # 集合最大文件数


@dataclass
class Quota:
    """配额使用情况"""
    daily_limit: int
    used_today: int
    remaining: int
    external_limit: int
    external_used: int


@dataclass
class Limits:
    """功能限制"""
    max_file_size: int
    max_concurrent: int
    retention_days: int
    priority_queue: str
    max_collection_items: int


@dataclass
class EntitlementResult:
    """权限校验结果"""
    allowed: bool
    reason: str = ""
    plan: str = ""
    quota: Quota | None = None
    limits: Limits | None = None


# ─── 套餐定义(从 settings 读取默认值) ──────────────────────────
_PLANS: dict[str, Plan] = {
    MEMBERSHIP_FREE: Plan(
        name="free",
        daily_quota=settings.FREE_DAILY_QUOTA,
        external_daily_quota=settings.FREE_EXTERNAL_DAILY_QUOTA,
        max_file_size=50 * 1024 * 1024,        # 50MB
        max_concurrent=1,
        retention_days=7,
        priority_queue="normal",
        max_collection_items=10,
    ),
    MEMBERSHIP_BASIC: Plan(
        name="basic",
        daily_quota=settings.BASIC_DAILY_QUOTA,
        external_daily_quota=settings.BASIC_EXTERNAL_DAILY_QUOTA,
        max_file_size=500 * 1024 * 1024,       # 500MB
        max_concurrent=3,
        retention_days=30,
        priority_queue="normal",
        max_collection_items=50,
    ),
    MEMBERSHIP_PREMIUM: Plan(
        name="premium",
        daily_quota=settings.PREMIUM_DAILY_QUOTA,
        external_daily_quota=settings.PREMIUM_EXTERNAL_DAILY_QUOTA,
        max_file_size=2 * 1024 * 1024 * 1024,  # 2GB
        max_concurrent=10,
        retention_days=90,
        priority_queue="high",
        max_collection_items=200,
    ),
}


async def get_plan(user_id: int) -> Plan:
    """获取用户套餐(从 users_local.membership_level 读取)。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        Plan 对象;用户不存在时返回 free 套餐
    """
    store = get_cache_store()
    user = await store.get_user_local(user_id)
    level = MEMBERSHIP_FREE
    if user:
        level = user.get("membership_level", MEMBERSHIP_FREE)
    return _PLANS.get(level, _PLANS[MEMBERSHIP_FREE])


async def get_quota(user_id: int) -> Quota:
    """获取用户当日配额使用情况。

    日配额从 quota_reservations 表统计今日已消耗(reserved + settled),
    外部配额从 user_quota 表读取(沿用 M1 机制)。

    R51 P1-2 fail-closed 改造:
    - 配额消耗查询失败时不再默认 used=0(fail-OPEN),
      改为 raise AppError(ENTITLEMENT_QUOTA_QUERY_FAILED)
    - 防止 DB 异常时被误判为"今日未消耗"导致超额放行

    Args:
        user_id: Telegram 用户 ID

    Returns:
        Quota 对象

    Raises:
        AppError(ENTITLEMENT_QUOTA_QUERY_FAILED): 配额消耗查询失败
    """
    plan = await get_plan(user_id)
    store = get_cache_store()

    # 统计今日配额消耗: settled 用 actual_amount, reserved 用 amount
    # R51 P1-2: fail-closed — DB 异常时 raise AppError(不再默认 used=0)
    used_today = 0
    if store._db:
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
            if rows:
                used_today = int(rows[0][0] or 0)
        except Exception as e:
            logger.error(
                f"[Entitlements] get_quota 查询配额消耗失败 user={user_id}(fail-closed 拒绝): {e}"
            )
            raise AppError(
                ErrorCodes.ENTITLEMENT_QUOTA_QUERY_FAILED,
                params={
                    "user_id": user_id, "reason": "quota_reservations_query_failed",
                    "detail": f"{type(e).__name__}: {e}",
                },
            ) from e

    daily_limit = plan.daily_quota
    # -1 表示无限
    remaining = -1 if daily_limit == -1 else max(0, daily_limit - used_today)

    # 外部配额从 user_quota 表读取(沿用 M1 机制)
    external_limit = plan.external_daily_quota
    external_used = 0
    quota_data = await store.get_user_quota(user_id)
    if quota_data:
        ext_date = quota_data.get("ext_quota_date")
        if ext_date:
            try:
                ext_dt = datetime.datetime.fromisoformat(str(ext_date)).date()
                if ext_dt == datetime.datetime.now().date():
                    external_used = int(quota_data.get("ext_used_today", 0) or 0)
            except (ValueError, TypeError):
                pass
        else:
            # 无日期记录视为今日未用
            external_used = 0

    return Quota(
        daily_limit=daily_limit,
        used_today=used_today,
        remaining=remaining,
        external_limit=external_limit,
        external_used=external_used,
    )


async def get_limits(user_id: int) -> Limits:
    """获取用户功能限制(基于套餐等级)。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        Limits 对象
    """
    plan = await get_plan(user_id)
    return Limits(
        max_file_size=plan.max_file_size,
        max_concurrent=plan.max_concurrent,
        retention_days=plan.retention_days,
        priority_queue=plan.priority_queue,
        max_collection_items=plan.max_collection_items,
    )


async def check(user_id: int, action: str, resource: dict | None = None) -> EntitlementResult:
    """统一权限校验。

    action 枚举:
        - upload: 上传文件
        - decode: 系统码解码
        - external_decode: 外部码解码
        - create_collection: 创建合集
        - add_to_collection: 向合集添加文件

    resource 根据 action 不同:
        - upload: {"file_size": int}
        - create_collection: {"file_count": int}
        - add_to_collection: {"current_count": int, "new_count": int}

    Args:
        user_id: Telegram 用户 ID
        action: 操作类型
        resource: 操作相关资源参数

    Returns:
        EntitlementResult, allowed=True 表示允许
    """
    resource = resource or {}
    store = get_cache_store()

    # 检查用户是否被封禁
    user = await store.get_user_local(user_id)
    if user and user.get("is_banned"):
        return EntitlementResult(
            allowed=False,
            reason="您的账户已被禁用",
        )

    plan = await get_plan(user_id)
    quota = await get_quota(user_id)
    limits = await get_limits(user_id)

    plan_name = plan.name

    if action == "upload":
        # 检查文件大小
        file_size = int(resource.get("file_size", 0) or 0)
        if file_size > plan.max_file_size:
            max_mb = plan.max_file_size // (1024 * 1024)
            return EntitlementResult(
                allowed=False,
                reason=f"文件大小超过套餐限制({max_mb}MB),请升级会员",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )
        # 检查每日配额(premium 无限)
        if plan.daily_quota != -1 and quota.remaining <= 0:
            return EntitlementResult(
                allowed=False,
                reason=f"今日上传次数已用完({plan.daily_quota}次),请明天再试或升级会员",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "decode":
        # 检查每日解码配额
        if plan.daily_quota != -1 and quota.remaining <= 0:
            return EntitlementResult(
                allowed=False,
                reason=f"今日解码次数已用完({plan.daily_quota}次),请明天再试或升级会员",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "external_decode":
        # 检查外部解码权限
        if plan.external_daily_quota == 0:
            return EntitlementResult(
                allowed=False,
                reason="您没有非本系统码的解码权限,升级会员即可解锁此功能",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )
        # 检查外部解码配额(-1 表示无限)
        if plan.external_daily_quota != -1 and quota.external_used >= plan.external_daily_quota:
            return EntitlementResult(
                allowed=False,
                reason=f"今日非本系统码解码次数已用完({plan.external_daily_quota}次),请明天再试",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "create_collection":
        # 检查合集文件数上限
        file_count = int(resource.get("file_count", 0) or 0)
        if file_count > plan.max_collection_items:
            return EntitlementResult(
                allowed=False,
                reason=f"合集文件数超过套餐限制({plan.max_collection_items}个),请升级会员",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "add_to_collection":
        # 检查合集已有文件数 + 新增数
        current_count = int(resource.get("current_count", 0) or 0)
        new_count = int(resource.get("new_count", 1) or 1)
        if current_count + new_count > plan.max_collection_items:
            return EntitlementResult(
                allowed=False,
                reason=f"合集文件数将超过套餐限制({plan.max_collection_items}个)",
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    else:
        return EntitlementResult(
            allowed=False,
            reason=f"未知的操作类型: {action}",
        )

    return EntitlementResult(
        allowed=True,
        plan=plan_name,
        quota=quota,
        limits=limits,
    )


async def set_user_plan(user_id: int, plan_name: str, admin_id: int = 0) -> bool:
    """设置用户套餐(管理员操作)。

    更新 users_local.membership_level 和 user_quota 表的配额上限,
    并写入 audit_log 审计记录。

    R51 P1-2 事务化改造:
    - users_local / user_quota / audit_log / dirty_outbox 全部在同一 transaction 内
    - 任一写入失败 → 整个事务回滚,避免"半提交"
    - 失败时 raise AppError(ENTITLEMENT_SET_PLAN_TX_FAILED)(不再返回 False 静默)
    - 保持 API 签名兼容(只新增异常,不破坏现有调用)

    Args:
        user_id: 目标用户 ID
        plan_name: 套餐名(free/basic/premium)
        admin_id: 操作管理员 ID

    Returns:
        True 表示成功

    Raises:
        AppError(ENTITLEMENT_SET_PLAN_TX_FAILED): 事务失败
    """
    if plan_name not in _PLANS:
        logger.warning(f"[Entitlements] set_user_plan 无效套餐名: {plan_name}")
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": "invalid_plan_name",
            },
        )

    plan = _PLANS[plan_name]
    store = get_cache_store()
    now = datetime.datetime.now().isoformat()

    if not store._db:
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": "cache_store_unavailable",
            },
        )

    # R51 P1-2: 单一事务包裹所有写入(users_local + user_quota + audit_log + dirty_outbox)
    try:
        async with store.transaction() as tx:
            # 1. 更新 users_local.membership_level(同事务)
            await tx.execute(
                "UPDATE users_local SET membership_level = ?, updated_at = ? "
                "WHERE user_id = ?",
                (plan_name, now, user_id),
            )
            # 写 dirty_outbox upsert(同事务,确保跨机同步)
            await store.add_dirty_outbox(
                "users_local", str(user_id),
                operation="upsert", connection=tx,
            )

            # 2. 更新 user_quota 配额上限(若记录存在则更新,不存在则创建)
            quota_data = await store.get_user_quota(user_id)
            if quota_data:
                new_quota = {
                    "level": plan_name,
                    "daily_quota": plan.daily_quota,
                    "used_today": quota_data.get("used_today", 0),
                    "quota_date": quota_data.get("quota_date", now),
                    "ext_quota": plan.external_daily_quota,
                    "ext_used_today": quota_data.get("ext_used_today", 0),
                    "ext_quota_date": quota_data.get("ext_quota_date", now),
                    "synced_at": quota_data.get("synced_at", 0),
                }
            else:
                new_quota = {
                    "level": plan_name,
                    "daily_quota": plan.daily_quota,
                    "used_today": 0,
                    "quota_date": now,
                    "ext_quota": plan.external_daily_quota,
                    "ext_used_today": 0,
                    "ext_quota_date": now,
                    "synced_at": 0,
                }
            # 直接执行 INSERT OR REPLACE(替代 upsert_user_quota,
            # 后者会自行 commit 破坏事务边界)
            await tx.execute(
                "INSERT OR REPLACE INTO user_quota "
                "(user_id, level, daily_quota, used_today, quota_date, "
                "ext_quota, ext_used_today, ext_quota_date, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    new_quota["level"],
                    new_quota["daily_quota"],
                    new_quota["used_today"],
                    new_quota["quota_date"],
                    new_quota["ext_quota"],
                    new_quota["ext_used_today"],
                    new_quota["ext_quota_date"],
                    new_quota["synced_at"],
                ),
            )
            # 写 dirty_outbox upsert(同事务)
            await store.add_dirty_outbox(
                "user_quota", str(user_id),
                operation="upsert", connection=tx,
            )

            # 3. 写入审计日志(同事务)
            await tx.execute(
                "INSERT INTO audit_log "
                "(actor_id, actor_type, action, target_type, target_id, "
                "details, created_at) "
                "VALUES (?, 'admin', 'set_plan', 'user', ?, ?, ?)",
                (admin_id, str(user_id),
                 json.dumps({"plan": plan_name, "admin_id": admin_id}), now),
            )
            # 写 dirty_outbox upsert(同事务)
            await store.add_dirty_outbox(
                "audit_log", "last",
                operation="upsert", connection=tx,
            )
        # 事务自动 COMMIT(store.transaction 退出时)
        logger.info(
            f"[Entitlements] 用户 {user_id} 套餐已设置为 {plan_name}(操作人: {admin_id})"
        )
        return True
    except AppError:
        # 已是 AppError,直接透传
        raise
    except Exception as e:
        logger.error(
            f"[Entitlements] set_user_plan 事务失败 user={user_id} plan={plan_name}: {e}"
        )
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": f"{type(e).__name__}: {e}",
            },
        ) from e


async def get_plan_features(plan_name: str) -> dict:
    """获取套餐功能特性(用于展示给用户)。

    Args:
        plan_name: 套餐名(free/basic/premium)

    Returns:
        套餐功能字典,未知套餐返回空字典
    """
    plan = _PLANS.get(plan_name)
    if plan is None:
        return {}
    return {
        "name": plan.name,
        "daily_quota": "无限" if plan.daily_quota == -1 else plan.daily_quota,
        "external_daily_quota": "不允许" if plan.external_daily_quota == 0 else (
            "无限" if plan.external_daily_quota == -1 else plan.external_daily_quota
        ),
        "max_file_size_mb": plan.max_file_size // (1024 * 1024),
        "max_concurrent": plan.max_concurrent,
        "retention_days": plan.retention_days,
        "priority_queue": plan.priority_queue,
        "max_collection_items": plan.max_collection_items,
    }
