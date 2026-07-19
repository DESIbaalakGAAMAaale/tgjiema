"""R40 §9.2: Entitlement Service — 统一套餐/配额/限制判定。

职责:
- 根据用户会员等级(free/basic/premium)判定可用配额、文件大小、并发数、保留期、队列优先级
- 统一入口 check() 供 Up/Idx/Dsp Bot 在上传/解码/合集等操作前调用
- 管理员可通过 set_user_plan_via_command_bus() 动态调整用户套餐(R53 P0-5:
  底层事务函数 _set_user_plan_internal 已私有化,production 环境必须经 CommandBus)

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
import hashlib
import json
from dataclasses import dataclass, asdict
from loguru import logger

from database.cache_store import get_cache_store
from config import settings
from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# ─── 会员等级常量 ──────────────────────────────────────────────
MEMBERSHIP_FREE = "free"
MEMBERSHIP_BASIC = "basic"
MEMBERSHIP_PREMIUM = "premium"


def _get_billing_day_utc_bounds() -> tuple[str, str]:
    """R53 P1-4: 计算当日 ``BILLING_TIMEZONE`` 边界对应的 UTC 时间区间。

    以 ``settings.BILLING_TIMEZONE``(默认 Asia/Shanghai)为基准,
    取当日 00:00:00 到次日 00:00:00 的本地时间区间,
    转换为 UTC ISO 8601 字符串(带 +00:00 后缀)。

    用途:
        - 替代 SQLite 的 ``date('now', 'localtime')``,
          避免依赖容器/宿主机时区(Docker 默认 UTC)
        - 用于参数化查询 ``created_at >= ? AND created_at < ?``

    Returns:
        (start_utc_iso, end_utc_iso):
            start_utc_iso: 当日 BILLING_TIMEZONE 0 点对应的 UTC ISO 字符串
            end_utc_iso:   次日 BILLING_TIMEZONE 0 点对应的 UTC ISO 字符串
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(str(getattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")))
    now_local = datetime.datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + datetime.timedelta(days=1)
    start_utc = start_local.astimezone(datetime.timezone.utc)
    end_utc = end_local.astimezone(datetime.timezone.utc)
    return (start_utc.isoformat(), end_utc.isoformat())


def _get_billing_today_date() -> datetime.date:
    """R53 P1-4: 获取 ``BILLING_TIMEZONE`` 当地今日日期。

    用于 ``ext_quota_date`` 等按本地日期比较的场景,
    替代 ``datetime.datetime.now().date()``(依赖宿主机时区)。

    Returns:
        BILLING_TIMEZONE 当地今日 date 对象
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(str(getattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")))
    return datetime.datetime.now(tz).date()


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

    R52 P1-4 改造:
    - 提供 transaction-aware 版本 get_quota(tx=...)(见 get_user_quota)
    - 本函数保持无 tx 调用兼容(内部使用 store._db 直读)
    - 在外层 transaction 内调用本函数不会触发额外 commit

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
    # R53 P1-4: 不再依赖 SQLite date('now', 'localtime')(受宿主机时区影响),
    # 改用 Python 计算 BILLING_TIMEZONE 当日 0 点对应的 UTC 边界,参数化查询
    used_today = 0
    if store._db:
        try:
            # R53 P1-4: BILLING_TIMEZONE 当日 UTC 边界
            start_utc_iso, end_utc_iso = _get_billing_day_utc_bounds()
            rows = await store._db.execute_fetchall(
                "SELECT COALESCE(SUM(CASE "
                "WHEN status='settled' THEN actual_amount "
                "WHEN status='reserved' THEN amount "
                "ELSE 0 END), 0) "
                "FROM quota_reservations "
                "WHERE user_id = ? AND status != 'refunded' "
                # created_at 由 datetime.now(timezone.utc) 写入(UTC aware ISO),
                # 查询用参数化 UTC 边界匹配 BILLING_TIMEZONE 当日 0 点窗口:
                #   start_utc_iso <= created_at < end_utc_iso
                # 例如 BILLING_TIMEZONE=Asia/Shanghai 时,本地 0 点 = UTC 前一日 16:00
                "AND created_at >= ? AND created_at < ?",
                (user_id, start_utc_iso, end_utc_iso),
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
                # R53 P1-4: ext_quota_date 按 BILLING_TIMEZONE 当地日期比较,
                # 替代 datetime.datetime.now().date()(依赖宿主机时区)
                ext_dt = datetime.datetime.fromisoformat(str(ext_date))
                # 兼容 naive timestamp(旧数据):视为 UTC
                if ext_dt.tzinfo is None:
                    ext_dt = ext_dt.replace(tzinfo=datetime.timezone.utc)
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(str(getattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")))
                if ext_dt.astimezone(tz).date() == _get_billing_today_date():
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


async def get_user_quota(
    user_id: int, *, tx=None,
) -> dict | None:
    """R52 P1-4: 读取用户配额(transaction-aware 版本)。

    与 store.get_user_quota() 的区别:
        - 显式接受可选的 tx 参数,在外层 transaction 内调用时
          复用 tx 的连接(不会触发额外 commit,避免破坏事务边界)
        - tx=None 时退化为 store.get_user_quota()(向后兼容)

    Args:
        user_id: 用户 ID
        tx: 可选的事务连接(store.transaction() 返回的 tx)

    Returns:
        用户配额 dict;未找到返回 None
    """
    store = get_cache_store()
    if tx is not None:
        # R52 P1-4: 复用外层事务连接(不触发额外 commit)
        try:
            cur = await tx.execute(
                "SELECT user_id, level, daily_quota, used_today, quota_date, "
                "ext_quota, ext_used_today, ext_quota_date, synced_at "
                "FROM user_quota WHERE user_id = ?",
                (user_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            if not rows:
                return None
            r = rows[0]
            return {
                "user_id": r[0],
                "level": r[1],
                "daily_quota": r[2],
                "used_today": r[3],
                "quota_date": r[4],
                "ext_quota": r[5],
                "ext_used_today": r[6],
                "ext_quota_date": r[7],
                "synced_at": r[8],
            }
        except Exception as e:
            logger.warning(
                f"[Entitlements] R52 P1-4: get_user_quota(tx) 查询失败 "
                f"user_id={user_id}: {e}"
            )
            return None
    # tx=None → 退化为 store.get_user_quota()
    return await store.get_user_quota(user_id)


async def get_user_version(user_id: int, *, tx=None) -> int:
    """R52 P1-4: 读取用户记录的 version 字段(用于 CAS 并发套餐修改)。

    users_local 表含 version 列(乐观锁),并发 _set_user_plan_internal 时通过
    expected_version 进行 CAS 防止 lost update。

    Args:
        user_id: 用户 ID
        tx: 可选的事务连接

    Returns:
        当前 version;用户不存在或表无 version 列时返回 0
    """
    store = get_cache_store()
    conn = tx if tx is not None else store._db
    if conn is None:
        return 0
    try:
        cur = await conn.execute(
            "SELECT version FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        # version 列可能不存在(老库)→ 退化为 0(无 CAS)
        # R64 P1-07: financial 域禁止 except 块裸 return 0;记录日志后落到函数尾返回
        logger.debug(
            f"[Entitlements] R52 P1-4: get_user_version 失败 "
            f"user_id={user_id}: {e}"
        )
    return 0


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
            reason=_i18n_t('services.entitlements.s3'),
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
                reason=_i18n_t('services.entitlements.s5', max_mb=max_mb),
                plan=plan_name,
                quota=quota,
                limits=limits,
            )
        # 检查每日配额(premium 无限)
        if plan.daily_quota != -1 and quota.remaining <= 0:
            return EntitlementResult(
                allowed=False,
                reason=_i18n_t('services.entitlements.s6', plan_daily_quota=plan.daily_quota),
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "decode":
        # 检查每日解码配额
        if plan.daily_quota != -1 and quota.remaining <= 0:
            return EntitlementResult(
                allowed=False,
                reason=_i18n_t('services.entitlements.s7', plan_daily_quota=plan.daily_quota),
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    elif action == "external_decode":
        # 检查外部解码权限
        if plan.external_daily_quota == 0:
            return EntitlementResult(
                allowed=False,
                reason=_i18n_t('services.entitlements.s8'),
                plan=plan_name,
                quota=quota,
                limits=limits,
            )
        # 检查外部解码配额(-1 表示无限)
        if plan.external_daily_quota != -1 and quota.external_used >= plan.external_daily_quota:
            return EntitlementResult(
                allowed=False,
                reason=_i18n_t('services.entitlements.s9', plan_external_daily_quota=plan.external_daily_quota),
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
                reason=_i18n_t('services.entitlements.s10', plan_max_collection_items=plan.max_collection_items),
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
                reason=_i18n_t('services.entitlements.s12', plan_max_collection_items=plan.max_collection_items),
                plan=plan_name,
                quota=quota,
                limits=limits,
            )

    else:
        return EntitlementResult(
            allowed=False,
            reason=_i18n_t('services.entitlements.s11', action=action),
        )

    return EntitlementResult(
        allowed=True,
        plan=plan_name,
        quota=quota,
        limits=limits,
    )


async def _set_user_plan_internal(
    user_id: int,
    plan_name: str,
    admin_id: int = 0,
    *,
    expected_version: int | None = None,
    request_hash: str = "",
    via_command_bus: bool = False,
) -> bool:
    """设置用户套餐(底层事务实现,私有)。

    R53 P0-5 整改:
    - 原 ``set_user_plan`` 重命名为 ``_set_user_plan_internal``,作为**私有**底层事务函数,
      不再作为公共生产 API。
    - 公共生产入口为 ``set_user_plan_via_command_bus``,必须经 CommandBus 审批。
    - production 环境禁止 ``via_command_bus=False`` 直接修改套餐
      → 抛 ``AppError(ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN)``。
    - production 环境禁止 ``expected_version=None``
      → 抛 ``AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)``。
    - development / test 环境保留向后兼容(允许直接调用,允许 ``expected_version=None``)。

    更新 users_local.membership_level 和 user_quota 表的配额上限,
    并写入 audit_log 审计记录。

    R51 P1-2 事务化改造:
    - users_local / user_quota / audit_log / dirty_outbox 全部在同一 transaction 内
    - 任一写入失败 → 整个事务回滚,避免"半提交"
    - 失败时 raise AppError(ENTITLEMENT_SET_PLAN_TX_FAILED)(不再返回 False 静默)
    - 保持 API 签名兼容(只新增异常,不破坏现有调用)

    R52 P1-4 改造:
    - 支持 CAS 并发套餐修改(expected_version 提供且 > 0 时启用乐观锁)
    - 审计日志包含 old_plan / new_plan / request_hash(短指纹前 16 字符)

    Args:
        user_id: 目标用户 ID
        plan_name: 套餐名(free/basic/premium)
        admin_id: 操作管理员 ID
        expected_version: 可选,CAS 期望版本号(并发套餐修改乐观锁);
            production 环境下不允许 None
        request_hash: 可选,请求指纹(记录到 audit_log,前 16 字符短指纹)
        via_command_bus: 是否经 CommandBus 审批入口调用(True=已审批);
            production 环境下 False 时直接抛 AppError

    Returns:
        True 表示成功

    Raises:
        AppError(ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN): production 环境下
            via_command_bus=False 直接调用(绕过 CommandBus)
        AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED): production 环境下
            expected_version=None(必须 CAS)
        AppError(ENTITLEMENT_SET_PLAN_TX_FAILED): 事务失败
        AppError(ENTITLEMENT_SET_PLAN_CAS_CONFLICT): CAS 版本冲突
    """
    # R53 P0-5: production 环境强制守卫 — 必须经 CommandBus + 必须 CAS
    environment = _get_environment()
    if environment == "production":
        if not via_command_bus:
            logger.error(
                f"[Entitlements] R53 P0-5: production 环境禁止直接修改套餐 "
                f"user={user_id} plan={plan_name}(必须通过 set_user_plan_via_command_bus)"
            )
            raise AppError(
                ErrorCodes.ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN,
                params={
                    "user_id": user_id,
                    "plan_name": plan_name,
                    "environment": environment,
                },
            )
        if expected_version is None:
            logger.error(
                f"[Entitlements] R53 P0-5: production 环境修改套餐必须提供 expected_version "
                f"user={user_id} plan={plan_name}"
            )
            raise AppError(
                ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED,
                params={
                    "user_id": user_id,
                    "plan_name": plan_name,
                    "environment": environment,
                },
            )

    if plan_name not in _PLANS:
        logger.warning(f"[Entitlements] _set_user_plan_internal 无效套餐名: {plan_name}")
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": "invalid_plan_name",
            },
        )

    plan = _PLANS[plan_name]
    store = get_cache_store()
    # R53 P1-4: 统一存 UTC aware timestamp(ISO 带 +00:00),
    # 替代 datetime.datetime.now().isoformat()(naive 本地时间),
    # 与配额查询的 UTC 边界参数化匹配
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if not store._db:
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": "cache_store_unavailable",
            },
        )

    # R52 P1-4: 短指纹(前 16 字符)用于审计日志,避免完整 hash 泄露
    short_hash = (request_hash or "")[:16]

    # R51 P1-2: 单一事务包裹所有写入(users_local + user_quota + audit_log + dirty_outbox)
    try:
        async with store.transaction() as tx:
            # R52 P1-4: 读取旧套餐(用于审计日志)
            old_plan = ""
            try:
                cur = await tx.execute(
                    "SELECT membership_level FROM users_local WHERE user_id = ?",
                    (user_id,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row and row[0]:
                    old_plan = str(row[0])
            except Exception as e:
                logger.debug(
                    f"[Entitlements] R52 P1-4: 读取旧套餐失败 user={user_id}: {e}"
                )

            # R52 P1-4: CAS 并发套餐修改(expected_version > 0 时启用乐观锁)
            if expected_version is not None and expected_version > 0:
                # CAS: UPDATE users_local SET ... WHERE user_id = ? AND version = ?
                # rowcount=0 表示版本冲突(已被其他 worker 修改)
                cursor = await tx.execute(
                    "UPDATE users_local SET membership_level = ?, updated_at = ?, "
                    "version = version + 1 "
                    "WHERE user_id = ? AND version = ?",
                    (plan_name, now, user_id, expected_version),
                )
                if cursor.rowcount == 0:
                    # CAS 冲突 — 查询当前版本以便诊断
                    current_version = await get_user_version(user_id, tx=tx)
                    logger.warning(
                        f"[Entitlements] R52 P1-4: _set_user_plan_internal CAS 冲突 "
                        f"user={user_id} expected_version={expected_version} "
                        f"current_version={current_version}"
                    )
                    raise AppError(
                        ErrorCodes.ENTITLEMENT_SET_PLAN_CAS_CONFLICT,
                        params={
                            "user_id": user_id,
                            "plan_name": plan_name,
                            "expected_version": expected_version,
                            "current_version": current_version,
                        },
                    )
            else:
                # 无 CAS(向后兼容,仅 development/test 允许)
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
            quota_data = await get_user_quota(user_id, tx=tx)
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

            # 3. 写入审计日志(同事务)— R52 P1-4: 包含 old_plan/new_plan/request_hash
            audit_details = {
                "plan": plan_name,
                "admin_id": admin_id,
                "old_plan": old_plan,
                "new_plan": plan_name,
                "request_hash": short_hash,
                "via_command_bus": via_command_bus,
            }
            if expected_version is not None and expected_version > 0:
                audit_details["expected_version"] = expected_version
            await tx.execute(
                "INSERT INTO audit_log "
                "(actor_id, actor_type, action, target_type, target_id, "
                "details, created_at) "
                "VALUES (?, 'admin', 'set_plan', 'user', ?, ?, ?)",
                (admin_id, str(user_id),
                 json.dumps(audit_details, ensure_ascii=False), now),
            )
            # 写 dirty_outbox upsert(同事务)
            await store.add_dirty_outbox(
                "audit_log", "last",
                operation="upsert", connection=tx,
            )
        # 事务自动 COMMIT(store.transaction 退出时)
        logger.info(
            f"[Entitlements] 用户 {user_id} 套餐已设置为 {plan_name}"
            f"(操作人: {admin_id}, old_plan: {old_plan}, "
            f"via_command_bus: {via_command_bus}, hash: {short_hash})"
        )
        return True
    except AppError:
        # 已是 AppError,直接透传
        raise
    except Exception as e:
        logger.error(
            f"[Entitlements] _set_user_plan_internal 事务失败 "
            f"user={user_id} plan={plan_name}: {e}"
        )
        raise AppError(
            ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
            params={
                "user_id": user_id, "plan": plan_name,
                "reason": f"{type(e).__name__}: {e}",
            },
        ) from e


def _get_environment() -> str:
    """R53 P0-5: 读取当前运行环境(production / development / test)。

    用于 ``_set_user_plan_internal`` 守卫,production 环境下禁止直接修改套餐。
    与 ``services.crdb_sync_service._is_production`` 保持一致的取值逻辑。
    """
    try:
        return str(getattr(settings, "ENVIRONMENT", "development"))
    except Exception:
        return "development"


async def set_user_plan_via_command_bus(
    user_id: int,
    plan_name: str,
    principal,
    *,
    action_id: str = "",
    request_hash: str = "",
    expected_version: int | None = None,
) -> dict:
    """R52 P1-4 + R53 P0-5: 通过 CommandBus 审批入口修改用户套餐(唯一公共生产 API)。

    套餐变更为高风险操作(影响用户配额/计费),生产环境必须通过 CommandBus:
    1. 调用 ``claim_execution_approved`` 验证 approval_action_id 处于 approved 状态
    2. 校验 request_hash 防篡改
    3. 通过后调用 ``_set_user_plan_internal(via_command_bus=True)``
    4. 标记 executed/failed

    R53 P0-5 整改:
    - 此函数是**唯一**的公共生产入口,底层 ``_set_user_plan_internal`` 已私有化。
    - production 环境下调用方必须传 ``expected_version``(非 None),否则由
      ``_set_user_plan_internal`` 抛 ``AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)``。
    - ``via_command_bus=True`` 由本函数内部传入,绕过 DIRECT_MUTATION_FORBIDDEN 守卫。

    Args:
        user_id: 目标用户 ID
        plan_name: 套餐名(free/basic/premium)
        principal: AdminPrincipal 对象(操作者身份)
        action_id: 幂等 ID(approval_action_id)
        request_hash: 请求指纹(防篡改)
        expected_version: 可选,CAS 期望版本号;
            production 环境下必须提供(非 None),否则抛 AppError

    Returns:
        dict: {"success": bool}(成功时无 error 键)

    Raises:
        AppError(ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS): action_id 为空
        AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED): production 环境下
            expected_version=None(必须 CAS)
        AppError(ENTITLEMENT_SET_PLAN_TX_FAILED): 事务失败
        AppError(ENTITLEMENT_SET_PLAN_CAS_CONFLICT): CAS 冲突
    """
    from services.command_bus import claim_execution_approved, mark_approved_executed, mark_approved_failed

    if not action_id:
        # action_id 为空 — 调用方未通过 CommandBus,拒绝执行
        logger.warning(
            f"[Entitlements] R52 P1-4: set_user_plan_via_command_bus 缺少 action_id "
            f"user={user_id} plan={plan_name}"
        )
        raise AppError(
            ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS,
            params={
                "user_id": user_id,
                "plan_name": plan_name,
                "caller": "set_user_plan_via_command_bus",
            },
        )

    principal_id = getattr(principal, "id", 0) or 0
    principal_name = getattr(principal, "name", "") or ""
    owner = f"entitlements:{principal_id}:{principal_name}"

    # 1. 验证审批已通过 + hash 防篡改
    # R55 P0-2: request_hash 强制必填(64 位 SHA-256 hex)
    # 若调用方未提供,从 entitlement 参数计算确定性 hash
    if not request_hash:
        _entitlement_payload = {
            "user_id": user_id,
            "plan_name": plan_name,
            "principal_id": principal_id,
            "action_id": action_id,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                _entitlement_payload,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    claimed = await claim_execution_approved(
        action_id=action_id,
        owner=owner,
        request_hash=request_hash,
    )
    if not claimed:
        logger.warning(
            f"[Entitlements] R52 P1-4: CommandBus 认领失败 action_id={action_id} "
            f"user={user_id} plan={plan_name}"
        )
        # 审批无效 → 回写 failed(若已 claim 成功状态机推进,则标记 failed)
        try:
            await mark_approved_failed(
                action_id,
                error="claim_execution_approved returned False",
                retryable=False,
            )
        except Exception as mark_err:
            logger.debug(
                f"[Entitlements] R53 P0-5: 审批无效时 mark_approved_failed 失败 "
                f"action_id={action_id}: {mark_err}"
            )
        raise AppError(
            ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS,
            params={
                "user_id": user_id,
                "plan_name": plan_name,
                "caller": "claim_execution_failed",
            },
        )

    # 2. 通过审批,执行套餐变更(via_command_bus=True 绕过 DIRECT_MUTATION_FORBIDDEN 守卫)
    try:
        await _set_user_plan_internal(
            user_id, plan_name, admin_id=principal_id,
            expected_version=expected_version,
            request_hash=request_hash,
            via_command_bus=True,
        )
        await mark_approved_executed(action_id, result={"success": True})
        logger.info(
            f"[Entitlements] R52 P1-4: 套餐变更经 CommandBus 完成 "
            f"action_id={action_id} user={user_id} plan={plan_name}"
        )
        return {"success": True}
    except AppError as e:
        # CAS 冲突为可重试,标记 retryable;其他失败标记 failed
        retryable = (
            e.code == ErrorCodes.ENTITLEMENT_SET_PLAN_CAS_CONFLICT
        )
        await mark_approved_failed(
            action_id, error=str(e), retryable=retryable,
        )
        logger.warning(
            f"[Entitlements] R52 P1-4: 套餐变更经 CommandBus 失败 "
            f"action_id={action_id} retryable={retryable}: {e}"
        )
        raise
    except Exception as e:
        await mark_approved_failed(action_id, error=str(e), retryable=False)
        logger.error(
            f"[Entitlements] R52 P1-4: 套餐变更经 CommandBus 异常 "
            f"action_id={action_id}: {e}"
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
        "daily_quota": _i18n_t('services.entitlements.s1') if plan.daily_quota == -1 else plan.daily_quota,
        "external_daily_quota": _i18n_t('services.entitlements.s2') if plan.external_daily_quota == 0 else (
            _i18n_t('services.entitlements.s4') if plan.external_daily_quota == -1 else plan.external_daily_quota
        ),
        "max_file_size_mb": plan.max_file_size // (1024 * 1024),
        "max_concurrent": plan.max_concurrent,
        "retention_days": plan.retention_days,
        "priority_queue": plan.priority_queue,
        "max_collection_items": plan.max_collection_items,
    }
