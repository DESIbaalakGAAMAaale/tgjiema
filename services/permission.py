import datetime
from dataclasses import dataclass
from typing import Optional

from database import get_users_col, get_file_records_col, make_user
from database import get_user_cached, update_user_and_invalidate
from database import get_file_record_cached, update_file_record_and_invalidate
from config import settings
from services.code_generator import extract_bot_username


@dataclass
class DecodeResult:
    allowed: bool
    reason: str = ""
    file_record: Optional[dict] = None
    remaining_quota: int = 0
    remaining_external_quota: int = 0
    is_external: bool = False
    external_bot_username: str = ""


async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    col = get_users_col()
    user = await col.find_one({"user_id": user_id})
    if user is None:
        user = make_user(
            user_id=user_id,
            username=username,
            first_name=first_name,
            membership_level="free",
            daily_decode_quota=settings.FREE_DAILY_QUOTA,
            external_decode_quota=settings.FREE_EXTERNAL_DAILY_QUOTA,
        )
        await col.insert_one(user)
    return user


def is_system_code(file_code: str) -> bool:
    return file_code.startswith(settings.FILE_CODE_PREFIX)


def is_external_code(file_code: str) -> bool:
    return not is_system_code(file_code) and bool(extract_bot_username(file_code))


async def check_upload_permission(user_id: int) -> bool:
    col = get_users_col()
    user = await col.find_one({"user_id": user_id})
    if user is None:
        return False
    if user.get("is_banned"):
        return False
    return True


async def check_decode_permission(user_id: int, file_code: str) -> DecodeResult:
    users_col = get_users_col()
    files_col = get_file_records_col()

    # 使用缓存查询用户
    user = await get_user_cached(user_id)
    if user is None:
        return DecodeResult(allowed=False, reason="用户未注册")

    if user.get("is_banned"):
        return DecodeResult(allowed=False, reason="您的账户已被禁用")

    membership_level = user.get("membership_level", "free")

    bot_username = extract_bot_username(file_code)

    if not is_system_code(file_code):
        if not bot_username:
            return DecodeResult(allowed=False, reason="无效的文件码格式，无法识别目标机器人。")

    today = datetime.datetime.now(datetime.UTC).date()

    def _parse_date(val) -> Optional[datetime.date]:
        if val:
            try:
                return datetime.datetime.fromisoformat(val).date()
            except (ValueError, TypeError):
                pass
        return None

    quota_date = _parse_date(user.get("quota_date"))
    external_quota_date = _parse_date(user.get("external_quota_date"))

    reset_set = {}

    if quota_date is None or quota_date != today:
        reset_set["quota_used_today"] = 0
        reset_set["quota_date"] = datetime.datetime.now(datetime.UTC).isoformat()
        user["quota_used_today"] = 0
        user["quota_date"] = reset_set["quota_date"]
        if membership_level == "free":
            reset_set["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
            user["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
        elif membership_level == "basic":
            reset_set["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
            user["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
        elif membership_level == "premium":
            reset_set["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
            user["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA

    if external_quota_date is None or external_quota_date != today:
        reset_set["external_used_today"] = 0
        reset_set["external_quota_date"] = datetime.datetime.now(datetime.UTC).isoformat()
        user["external_used_today"] = 0
        user["external_quota_date"] = reset_set["external_quota_date"]
        if membership_level == "free":
            reset_set["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
            user["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
        elif membership_level == "basic":
            reset_set["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
            user["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
        elif membership_level == "premium":
            reset_set["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
            user["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA

    if reset_set:
        await update_user_and_invalidate(user_id, {"$set": reset_set})

    quota = user.get("daily_decode_quota", settings.FREE_DAILY_QUOTA)
    used = user.get("quota_used_today", 0)
    if membership_level != "premium" and used >= quota:
        return DecodeResult(
            allowed=False,
            reason=f"今日解码次数已用完（{quota}次），请明天再试",
            remaining_quota=0,
        )

    if not is_system_code(file_code):
        external_quota = user.get("external_decode_quota", 0)
        external_used = user.get("external_used_today", 0)
        if external_quota == 0:
            return DecodeResult(
                allowed=False,
                reason="您没有非本系统码的解码权限。升级会员即可解锁此功能。",
            )
        if external_quota != -1 and external_used >= external_quota:
            return DecodeResult(
                allowed=False,
                reason=f"今日非本系统码解码次数已用完（{external_quota}次），请明天再试",
            )

    # 使用缓存查询文件记录
    file_record = await get_file_record_cached(file_code)
    if file_record is not None and file_record.get("status") == "active":
        # 检查文件码是否过期
        expire_time = file_record.get("expire_time")
        if expire_time is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            expire_dt = expire_time
            if isinstance(expire_time, str):
                try:
                    expire_dt = datetime.fromisoformat(expire_time)
                except (ValueError, TypeError):
                    pass
            if expire_dt < now:
                return DecodeResult(allowed=False, reason="该文件码已过期")
        await update_user_and_invalidate(user_id, {"$inc": {"quota_used_today": 1}})
        if not is_system_code(file_code):
            await update_user_and_invalidate(user_id, {"$inc": {"external_used_today": 1}})
        await update_file_record_and_invalidate(file_code, {"$inc": {"request_count": 1}})
        remaining = -1 if membership_level == "premium" else max(0, quota - (used + 1))
        remaining_ext = -1
        if not is_system_code(file_code):
            ext_q = user.get("external_decode_quota", 0)
            if ext_q != -1:
                remaining_ext = max(0, ext_q - (external_used + 1))
        return DecodeResult(
            allowed=True,
            file_record=file_record,
            remaining_quota=remaining,
            remaining_external_quota=remaining_ext,
        )

    if not is_system_code(file_code):
        await update_user_and_invalidate(user_id, {"$inc": {"quota_used_today": 1, "external_used_today": 1}})
        remaining = -1 if membership_level == "premium" else max(0, quota - (used + 1))
        remaining_ext = -1
        ext_q = user.get("external_decode_quota", 0)
        if ext_q != -1:
            remaining_ext = max(0, ext_q - (external_used + 1))
        return DecodeResult(
            allowed=True,
            is_external=True,
            external_bot_username=bot_username,
            remaining_quota=remaining,
            remaining_external_quota=remaining_ext,
        )

    return DecodeResult(allowed=False, reason="文件码无效")