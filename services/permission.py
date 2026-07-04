"""权限和配额检查 — SQLite First 架构

配额读写优先走本地 SQLite（零 CRDB RU）：
- 检查配额：SQLite → CRDB 兜底
- 递增使用量：直接写 SQLite
- 后台任务每 6h 批量同步 SQLite → CRDB
"""

import datetime
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from database import get_users_col, make_user
from database import get_user_cached
from database import get_file_record_cached
from database.cache_store import (
    get_user_quota, upsert_user_quota, increment_user_quota_used,
    try_consume_quota, refund_quota,
)
from config import settings
from services.code_generator import extract_bot_username


# ─── 统一的文件码过期检查 ────────────────────────────────────────
_CHINA_TZ_FOR_EXPIRY = datetime.timezone(datetime.timedelta(hours=8))


def check_code_expired(file_record: dict) -> tuple[bool, str]:
    expire_time = file_record.get("expire_time")
    if expire_time is None:
        return False, ""
    ttl_days = file_record.get("file_ttl_days", 0)
    if isinstance(ttl_days, str):
        try:
            ttl_days = int(ttl_days)
        except ValueError:
            ttl_days = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    expire_dt = expire_time
    if isinstance(expire_time, str):
        try:
            expire_dt = datetime.datetime.fromisoformat(expire_time)
        except (ValueError, TypeError):
            return False, ""
    if expire_dt.tzinfo is None:
        expire_dt = expire_dt.replace(tzinfo=_CHINA_TZ_FOR_EXPIRY)
    if expire_dt.tzinfo != datetime.timezone.utc:
        expire_dt = expire_dt.astimezone(datetime.timezone.utc)
    if expire_dt < now:
        return True, "该文件码已过期"
    return False, ""


@dataclass
class DecodeResult:
    allowed: bool
    reason: str = ""
    file_record: Optional[dict] = None
    remaining_quota: int = 0
    remaining_external_quota: int = 0
    is_external: bool = False
    external_bot_username: str = ""
    quota_consumed: bool = False  # 是否已预扣配额(投递失败时需要 refund)


async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    # A1: 优先走三级缓存(内存→SQLite→CRDB),避免每次直查 CRDB
    user = await get_user_cached(user_id)
    if user is not None:
        return user
    # 缓存未命中,创建新用户
    user = make_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        membership_level="free",
        daily_decode_quota=settings.FREE_DAILY_QUOTA,
        external_decode_quota=settings.FREE_EXTERNAL_DAILY_QUOTA,
    )
    col = get_users_col()
    try:
        await col.insert_one(user)
        # 同步写入 SQLite 本地缓存
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().upsert_user_local(user, mark_dirty=False)
        except Exception as cache_err:
            logger.debug(f"[Permission] upsert_user_local 失败 user={user_id}: {cache_err}")
        # F1: 新用户注册,递增本地计数器
        try:
            from utils.shared_counters import incr_total_users
            incr_total_users()
        except Exception as counter_err:
            logger.debug(f"[Permission] incr_total_users 失败 user={user_id}: {counter_err}")
    except Exception as e:
        # 并发插入冲突，重新查询；其他异常原样抛出
        error_str = str(e).lower()
        if "duplicate" in error_str or "unique" in error_str or "already exists" in error_str:
            user = await get_user_cached(user_id) or await col.find_one({"user_id": user_id})
            if not user:
                raise
        else:
            raise
    # 写入缓存(插入成功后才写,避免缓存脏数据)
    try:
        from database.cache import get_user_cache
        get_user_cache().set(f"user:{user_id}", user)
    except Exception as cache_err:
        logger.debug(f"[Permission] user_cache.set 失败 user={user_id}: {cache_err}")
    return user


def is_system_code(file_code: str) -> bool:
    return file_code.startswith(settings.FILE_CODE_PREFIX)


def is_external_code(file_code: str) -> bool:
    return not is_system_code(file_code) and bool(extract_bot_username(file_code))


async def check_upload_permission(user_id: int) -> bool:
    # A1: 走缓存,避免每次直查 CRDB
    user = await get_user_cached(user_id)
    if user is None:
        return False
    if user.get("is_banned"):
        return False
    return True


# ─── 配额本地 SQLite 工具函数 ──────────────────────────────────

_CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _quota_for_level(level: str) -> tuple[int, int]:
    """根据会员等级返回 (daily_quota, ext_quota)。集中管理避免散落多处不一致。"""
    return {
        "free": (settings.FREE_DAILY_QUOTA, settings.FREE_EXTERNAL_DAILY_QUOTA),
        "basic": (settings.BASIC_DAILY_QUOTA, settings.BASIC_EXTERNAL_DAILY_QUOTA),
        "premium": (settings.PREMIUM_DAILY_QUOTA, settings.PREMIUM_EXTERNAL_DAILY_QUOTA),
    }.get(level, (settings.FREE_DAILY_QUOTA, settings.FREE_EXTERNAL_DAILY_QUOTA))


async def _init_quota_from_crdb(user_id: int) -> dict:
    """从 CRDB 读取用户配额并写入本地 SQLite。返回写入后的数据。"""
    user = await get_user_cached(user_id)
    if user is None:
        return {}
    today = datetime.datetime.now(_CHINA_TZ).date()
    qd = user.get("quota_date")
    eqd = user.get("external_quota_date")
    qd_today = False
    eqd_today = False
    if qd:
        try:
            qd_today = datetime.datetime.fromisoformat(qd).date() == today
        except (ValueError, TypeError):
            pass
    if eqd:
        try:
            eqd_today = datetime.datetime.fromisoformat(eqd).date() == today
        except (ValueError, TypeError):
            pass
    data = {
        "level": user.get("membership_level", "free"),
        "daily_quota": user.get("daily_decode_quota", settings.FREE_DAILY_QUOTA),
        "used_today": user.get("quota_used_today", 0) if qd_today else 0,
        "quota_date": user.get("quota_date"),
        "ext_quota": user.get("external_decode_quota", 0),
        "ext_used_today": user.get("external_used_today", 0) if eqd_today else 0,
        "ext_quota_date": user.get("external_quota_date"),
        "synced_at": 0,
    }
    await upsert_user_quota(user_id, data)
    data["user_id"] = user_id
    return data


async def _get_quota_sqlite_first(user_id: int) -> dict:
    """从 SQLite 读取配额，未找到则从 CRDB 兜底并缓存到 SQLite。"""
    q = await get_user_quota(user_id)
    if q is not None:
        return q
    return await _init_quota_from_crdb(user_id)


async def _reset_quota_if_needed(q: dict, now_date: datetime.date) -> dict:
    """检查是否需要重置配额日期（跨天）。需要重置则返回新的数据。"""
    changed = False
    qd = q.get("quota_date")
    eqd = q.get("ext_quota_date")
    need_reset = False
    need_ext_reset = False
    if qd:
        try:
            need_reset = datetime.datetime.fromisoformat(str(qd)).date() != now_date
        except (ValueError, TypeError):
            need_reset = True
    else:
        need_reset = True
    if eqd:
        try:
            need_ext_reset = datetime.datetime.fromisoformat(str(eqd)).date() != now_date
        except (ValueError, TypeError):
            need_ext_reset = True
    else:
        need_ext_reset = True
    if need_reset:
        q["used_today"] = 0
        q["quota_date"] = datetime.datetime.now(_CHINA_TZ).isoformat()
        # 使用集中函数,避免多处硬编码不一致;同时与 level 变化时的同步逻辑保持一致
        q["daily_quota"], _ = _quota_for_level(q.get("level", "free"))
        changed = True
    if need_ext_reset:
        q["ext_used_today"] = 0
        q["ext_quota_date"] = datetime.datetime.now(_CHINA_TZ).isoformat()
        _, q["ext_quota"] = _quota_for_level(q.get("level", "free"))
        changed = True
    if changed:
        await upsert_user_quota(q["user_id"], q)
    return q


# ─── 核心：解码权限检查（SQLite First）───────────────────────────


async def check_decode_permission(user_id: int, file_code: str) -> DecodeResult:
    # 从 CRDB 获取用户基础信息（等级/封禁状态）
    user = await get_user_cached(user_id)
    if user is None:
        return DecodeResult(allowed=False, reason="用户未注册")
    if user.get("is_banned"):
        return DecodeResult(allowed=False, reason="您的账户已被禁用")

    membership_level = user.get("membership_level", "free")
    bot_username = extract_bot_username(file_code)
    if not is_system_code(file_code):
        if not bot_username:
            return DecodeResult(allowed=False, reason="无效的文件码格式,无法识别目标机器人。")

    # ─── SQLite First 配额 ─────────────────────────────────
    today = datetime.datetime.now(_CHINA_TZ).date()
    q = await _get_quota_sqlite_first(user_id)
    if not q:
        return DecodeResult(allowed=False, reason="系统繁忙,请稍后重试")
    # 如果等级变了（管理员手动修改），同步到 SQLite,并立即更新配额上限,避免升级后当天仍按旧配额
    if q.get("level") != membership_level:
        q["level"] = membership_level
        new_daily, new_ext = _quota_for_level(membership_level)
        q["daily_quota"] = new_daily
        q["ext_quota"] = new_ext
        await upsert_user_quota(user_id, q)

    q = await _reset_quota_if_needed(q, today)

    quota = q.get("daily_quota", settings.FREE_DAILY_QUOTA)
    used = q.get("used_today", 0)

    # Premium 不限量
    if membership_level != "premium" and used >= quota:
        return DecodeResult(
            allowed=False,
            reason=f"今日解码次数已用完({quota}次),请明天再试",
            remaining_quota=0,
        )

    if not is_system_code(file_code):
        ext_quota = q.get("ext_quota", 0)
        ext_used = q.get("ext_used_today", 0)
        if ext_quota == 0:
            return DecodeResult(
                allowed=False,
                reason="您没有非本系统码的解码权限。升级会员即可解锁此功能。",
            )
        if ext_quota != -1 and ext_used >= ext_quota:
            return DecodeResult(
                allowed=False,
                reason=f"今日非本系统码解码次数已用完({ext_quota}次),请明天再试",
            )

    # 查找文件记录（系统码走 CRDB，外部码不查）
    file_record = None
    if is_system_code(file_code):
        file_record = await get_file_record_cached(file_code)
        if file_record is not None and file_record.get("status") == "active":
            # I: codes 表走 B1 缓存，避免每次解码直查 CRDB(1 RU)
            from database import get_code_entry_cached
            code_entry = await get_code_entry_cached(file_code)
            if code_entry and code_entry.get("status") == "offline":
                return DecodeResult(allowed=False, reason="文件不存在或已被删除")
            expired, reason = check_code_expired(file_record)
            if expired:
                return DecodeResult(allowed=False, reason=reason)
            from database.cache import incr_request_count
            await incr_request_count(file_code)
        else:
            return DecodeResult(allowed=False, reason="文件码无效")

    # ─── 原子预扣配额(乐观锁),解决 TOCTOU 竞态 ────
    # 并发场景下,若多个请求同时通过上面的"检查",再各自在投递成功后递增,
    # 会导致用户超额使用(used 超过 quota)。
    # 改为:在检查通过后立即原子条件递增,投递失败时由调用方 refund。
    is_ext = not is_system_code(file_code)
    consumed = await try_consume_quota(user_id, is_external=is_ext)
    if not consumed:
        # 配额在 check 与 consume 之间被并发请求耗尽
        return DecodeResult(
            allowed=False,
            reason="今日解码次数已用完,请明天再试",
        )

    remaining = -1 if membership_level == "premium" else max(0, quota - used - 1)
    remaining_ext = -1
    if is_ext:
        ext_q = q.get("ext_quota", 0)
        if ext_q != -1:
            remaining_ext = max(0, ext_q - ext_used - 1)

    return DecodeResult(
        allowed=True,
        file_record=file_record,
        remaining_quota=remaining,
        remaining_external_quota=remaining_ext,
        is_external=is_ext,
        external_bot_username=bot_username or "",
        quota_consumed=True,
    )


# ─── 批量同步 SQLite → CRDB（由后台任务每 6h 调用）────────────

async def sync_quotas_to_crdb():
    """遍历 SQLite 中有变动的配额记录，逐条写回 CRDB。"""
    from database.cache_store import get_unsynced_quotas, mark_quota_synced
    rows = await get_unsynced_quotas()
    if not rows:
        return
    users_col = get_users_col()
    for r in rows:
        try:
            uid = r["user_id"]
            update_fields = {
                "quota_used_today": r["used_today"],
                "quota_date": r["quota_date"],
                "external_used_today": r["ext_used_today"],
                "external_quota_date": r["ext_quota_date"],
                "daily_decode_quota": r["daily_quota"],
                "external_decode_quota": r["ext_quota"],
                "membership_level": r["level"],
            }
            await users_col.update_one(
                {"user_id": uid},
                {"$set": update_fields},
            )
            # 使缓存失效
            from database import update_user_and_invalidate
            await update_user_and_invalidate(uid)
            await mark_quota_synced(uid)
        except Exception as e:
            logger.error(f"[QuotaSync] sync user {r.get('user_id')} failed: {e}")


# ─── 配额回滚(投递失败时调用)─────────────────────────────────


async def refund_user_quota(user_id: int, is_external: bool = False):
    """投递失败时回滚预扣的配额。与 check_decode_permission 中的 try_consume_quota 配对。

    幂等:对 premium/不限量用户不操作;对正常用户递减 1 且不低于 0。
    """
    try:
        await refund_quota(user_id, is_external=is_external)
    except Exception as e:
        # 不抛出:避免影响投递失败处理流程。但必须记录日志,便于运维监控配额泄漏并手动补偿
        logger.warning(f"[Quota] refund 失败 user={user_id} ext={is_external}: {e}")
