USER_DOC = "users"
FILE_RECORDS_DOC = "file_records"
DECODE_LOGS_DOC = "decode_logs"

MEMBERSHIP_LEVELS = ("free", "basic", "premium")
FILE_STATUSES = ("active", "expired", "deleted")


def make_user(
    user_id: int,
    username: str = None,
    first_name: str = None,
    membership_level: str = "free",
    daily_decode_quota: int = 3,
    quota_used_today: int = 0,
    quota_date: str = None,
    can_upload: bool = True,
    external_decode_quota: int = 0,
    external_used_today: int = 0,
    external_quota_date: str = None,
    is_banned: bool = False,
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "membership_level": membership_level,
        "daily_decode_quota": daily_decode_quota,
        "quota_used_today": quota_used_today,
        "quota_date": quota_date or now.isoformat(),
        "can_upload": can_upload,
        "external_decode_quota": external_decode_quota,
        "external_used_today": external_used_today,
        "external_quota_date": external_quota_date or now.isoformat(),
        "is_banned": is_banned,
        "created_at": now,
        "updated_at": now,
    }


def make_file_record(
    file_code: str,
    uploader_id: int,
    primary_channel_id: int,
    primary_channel_msg_id: int,
    file_types: dict,
    backup_channel_msg_ids: list = None,
    status: str = "active",
    request_count: int = 0,
    batch_msg_ids: str = "",
    batch_file_meta: str = "",
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "file_code": file_code,
        "uploader_id": uploader_id,
        "primary_channel_id": primary_channel_id,
        "primary_channel_msg_id": primary_channel_msg_id,
        "file_types": file_types,
        "backup_channel_msg_ids": backup_channel_msg_ids or [],
        "batch_msg_ids": batch_msg_ids,
        "batch_file_meta": batch_file_meta,
        "status": status,
        "request_count": request_count,
        "create_time": now,
        "expire_time": None,
    }


def make_decode_log(
    file_code: str,
    requester_id: int,
    status: str = "queued",
    source_channel_id: int = None,
):
    from datetime import datetime, timezone

    return {
        "file_code": file_code,
        "requester_id": requester_id,
        "request_time": datetime.now(timezone.utc),
        "status": status,
        "source_channel_id": source_channel_id,
    }


# ─── 环形冗余架构 新模型 ──────────────────────────────────────

CELL_STATUSES = ("active", "shadow1", "shadow2", "r100", "lost")


def make_cell(
    slot_id: str,
    channel_id: int,
    status: str = "shadow1",
    next_active_chat_id: int = None,
    prev_slot_id: str = None,
    is_r100: bool = False,
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "slot_id": slot_id,
        "channel_id": channel_id,
        "status": status,
        "next_active_chat_id": next_active_chat_id,
        "prev_slot_id": prev_slot_id,
        "is_r100": 1 if is_r100 else 0,
        "last_heartbeat": now.isoformat(),
        "last_synced_msg_id": 0,
        "degrade_count": 0,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def make_code_entry(
    code: str,
    uploader_id: int,
    file_types: dict = None,
    batch_msg_ids: str = "",
    batch_file_meta: str = "",
    primary_channel_id: int = 0,
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    import json as _json
    return {
        "code": code,
        "file_record_code": code,
        "uploader_id": uploader_id,
        "file_types": _json.dumps(file_types) if file_types else "{}",
        "batch_msg_ids": batch_msg_ids,
        "batch_file_meta": batch_file_meta,
        "primary_channel_id": primary_channel_id,
        "status": "active",
        "created_at": now.isoformat(),
    }


def make_job(
    code: str,
    target_user_id: int,
    storage_channel_id: int,
    storage_msg_ids: list[int],
    batch_file_meta: str = "",
    task_type: str = "single",
):
    from datetime import datetime, timezone
    import json as _json

    return {
        "code": code,
        "target_user_id": target_user_id,
        "storage_channel_id": storage_channel_id,
        "storage_msg_ids": _json.dumps(storage_msg_ids),
        "batch_file_meta": batch_file_meta,
        "task_type": task_type,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_rotate_log(
    from_slot_id: str,
    to_slot_id: str,
    from_status: str,
    to_status: str,
    reason: str,
    triggered_by: str = "mon",
):
    from datetime import datetime, timezone

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "from_slot_id": from_slot_id,
        "to_slot_id": to_slot_id,
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
        "triggered_by": triggered_by,
    }