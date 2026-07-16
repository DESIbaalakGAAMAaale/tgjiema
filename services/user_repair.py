"""R40 §9.1.5: 用户自助修复 — 重新索引 + 重新生成码 + 失败原因。

为用户提供文件码失效后的自助修复能力:
- 失效码重新索引(创建 repair 任务)
- 重新生成文件码(旧码标记 deprecated)
- 查看失败原因(不暴露内部频道信息)

设计要点:
- reindex_code / regenerate_code 都创建 task(task_type="repair")
- get_failure_reason 不返回 channel_id / msg_id 等内部信息
  (只返回用户可理解的原因枚举 + 建议操作)
- format_failure_reason 对 file_code 脱敏(只显示前 4 字符 + ***)
- 通过 get_cache_store() 获取 CacheStore 单例
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.task_center import create_task, update_progress, complete_task, fail_task
from services.code_generator import build_file_code, build_collection_code
from services.i18n import translate as _i18n_t


# 失败原因枚举
REASON_EXPIRED = "expired"            # 文件已过期
REASON_DELETED = "deleted"            # 文件已删除
REASON_CHANNEL_LOST = "channel_lost"  # 存储频道丢失
REASON_CORRUPTED = "corrupted"        # 文件数据损坏
REASON_UNKNOWN = "unknown"            # 未知原因

# 失败原因中文名
_REASON_LABELS = {
    REASON_EXPIRED: _i18n_t('services.user_repair.s1'),
    REASON_DELETED: _i18n_t('services.user_repair.s2'),
    REASON_CHANNEL_LOST: _i18n_t('services.user_repair.s3'),
    REASON_CORRUPTED: _i18n_t('services.user_repair.s4'),
    REASON_UNKNOWN: _i18n_t('services.user_repair.s5'),
}


def _safe_json_loads(val) -> Any:
    """安全反序列化 JSON 字符串,失败返回 None。"""
    if val is None or val == "":
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _is_expired(expire_time) -> bool:
    """检查 expire_time 是否已过期(支持 ISO 字符串或时间戳)。"""
    if not expire_time:
        return False
    try:
        if isinstance(expire_time, str):
            exp = _dt.datetime.fromisoformat(expire_time)
        else:
            exp = _dt.datetime.fromtimestamp(float(expire_time))
        return _dt.datetime.now() >= exp
    except (ValueError, TypeError):
        return False


def _mask_code(code: str) -> str:
    """脱敏文件码,只显示前 4 字符 + ***。"""
    if not code:
        return ""
    if len(code) <= 4:
        return "***"
    return code[:4] + "***"


async def reindex_code(file_code: str, user_id: int) -> int:
    """重新索引失效码,返回 task_id。

    检查文件是否可修复(未物理删除),创建 reindex 任务。

    Args:
        file_code: 失效的文件码
        user_id: 用户 ID

    Returns:
        task_id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[user_repair] CacheStore 未初始化")
        return 0
    # 检查用户是否有权修复该文件
    eligibility = await check_repair_eligibility(file_code, user_id)
    if not eligibility.get("eligible"):
        logger.warning(
            f"[user_repair] 无权修复 file_code={_mask_code(file_code)} "
            f"reason={eligibility.get('reason')}"
        )
        return 0
    # 检查文件是否可修复
    reason = await get_failure_reason(file_code)
    if reason and not reason.get("can_repair"):
        logger.warning(
            f"[user_repair] 文件不可修复 file_code={_mask_code(file_code)} "
            f"reason={reason.get('reason')}"
        )
        return 0
    # 创建 repair 任务
    task_id = await create_task(
        "repair", user_id,
        {
            "file_code": file_code,
            "action": "reindex",
            "original_reason": reason.get("reason") if reason else REASON_UNKNOWN,
        },
    )
    if task_id:
        logger.info(
            f"[user_repair] 创建修复任务 task_id={task_id} "
            f"file_code={_mask_code(file_code)}"
        )
    return task_id


async def regenerate_code(old_code: str, user_id: int) -> dict:
    """重新生成文件码(旧码失效时)。

    Args:
        old_code: 旧文件码
        user_id: 用户 ID

    Returns:
        {old_code, new_code, task_id};失败 new_code 为空字符串,task_id 为 0
    """
    store = get_cache_store()
    default = {"old_code": old_code, "new_code": "", "task_id": 0}
    if not store._db:
        return default
    # 权限校验
    eligibility = await check_repair_eligibility(old_code, user_id)
    if not eligibility.get("eligible"):
        logger.warning(
            f"[user_repair] 无权重生成码 file_code={_mask_code(old_code)} "
            f"reason={eligibility.get('reason')}"
        )
        return default
    try:
        # 查旧文件记录以获取 file_types(用于生成同类型新码)
        cursor = await store._db.execute(
            "SELECT file_types, is_collection FROM file_records_local WHERE file_code = ?",
            (old_code,),
        )
        row = await cursor.fetchone()
        new_code = ""
        if row:
            file_types = _safe_json_loads(row[0]) or {}
            is_collection = int(row[1] or 0)
            # 重新生成码(集合码 vs 文件码)
            if is_collection:
                new_code = build_collection_code()
            else:
                new_code = build_file_code(
                    file_types if isinstance(file_types, dict) else {}
                )
        else:
            # 记录不存在,生成默认文件码
            new_code = build_file_code({})
        if not new_code:
            logger.warning(
                f"[user_repair] 生成新码失败 old_code={_mask_code(old_code)}"
            )
            return default
        # 标记旧码为 deprecated(软标记,不物理删除)
        now = _dt.datetime.now().isoformat()
        await store._db.execute(
            """UPDATE file_records_local
               SET status = 'deprecated', updated_at = ?
               WHERE file_code = ?""",
            (now, old_code),
        )
        await store._db.commit()
        await store.add_dirty_outbox("file_records_local", old_code)
        # 创建 repair 任务跟踪
        task_id = await create_task(
            "repair", user_id,
            {
                "action": "regenerate",
                "old_code": old_code,
                "new_code": new_code,
            },
        )
        logger.info(
            f"[user_repair] 重新生成码 task_id={task_id} "
            f"old={_mask_code(old_code)} new={_mask_code(new_code)}"
        )
        return {
            "old_code": old_code,
            "new_code": new_code,
            "task_id": task_id,
        }
    except Exception as e:
        logger.warning(f"[user_repair] regenerate_code 失败: {e}")
        return default


async def get_failure_reason(file_code: str) -> dict | None:
    """获取文件码失败原因(不暴露内部频道信息)。

    Args:
        file_code: 文件码

    Returns:
        {file_code, status, reason, can_repair, suggested_action}
        reason 枚举: expired/deleted/channel_lost/corrupted/unknown
        不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            """SELECT status, deleted_at, expire_time
               FROM file_records_local WHERE file_code = ?""",
            (file_code,),
        )
        row = await cursor.fetchone()
        if not row:
            # 文件记录不存在
            return {
                "file_code": file_code,
                "status": "missing",
                "reason": REASON_UNKNOWN,
                "can_repair": False,
                "suggested_action": _i18n_t('services.user_repair.s15'),
            }
        status = (row[0] or "active").lower()
        deleted_at = row[1]
        expire_time = row[2]
        # 判断失败原因(不返回 channel_id / msg_id 等内部信息)
        if deleted_at or status == "deleted":
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_DELETED,
                "can_repair": False,
                "suggested_action": _i18n_t('services.user_repair.s16'),
            }
        if status == "expired" or _is_expired(expire_time):
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_EXPIRED,
                "can_repair": True,
                "suggested_action": _i18n_t('services.user_repair.s17'),
            }
        if status in ("channel_lost", "channel_unavailable"):
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_CHANNEL_LOST,
                "can_repair": True,
                "suggested_action": _i18n_t('services.user_repair.s18'),
            }
        if status in ("corrupted", "invalid"):
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_CORRUPTED,
                "can_repair": False,
                "suggested_action": _i18n_t('services.user_repair.s19'),
            }
        if status == "deprecated":
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_UNKNOWN,
                "can_repair": False,
                "suggested_action": _i18n_t('services.user_repair.s20'),
            }
        if status in ("active", "ready"):
            # 文件正常,不需要修复
            return {
                "file_code": file_code,
                "status": status,
                "reason": REASON_UNKNOWN,
                "can_repair": False,
                "suggested_action": _i18n_t('services.user_repair.s21'),
            }
        # 其他未知状态,默认可尝试修复
        return {
            "file_code": file_code,
            "status": status,
            "reason": REASON_UNKNOWN,
            "can_repair": True,
            "suggested_action": _i18n_t('services.user_repair.s11'),
        }
    except Exception as e:
        logger.warning(f"[user_repair] get_failure_reason 失败: {e}")
        return None


async def check_repair_eligibility(file_code: str, user_id: int) -> dict:
    """检查用户是否有权修复该文件码。

    Args:
        file_code: 文件码
        user_id: 用户 ID

    Returns:
        {eligible, reason, file_owner_id}
    """
    store = get_cache_store()
    default = {"eligible": False, "reason": _i18n_t('services.user_repair.s6'), "file_owner_id": 0}
    if not store._db:
        return default
    try:
        cursor = await store._db.execute(
            """SELECT uploader_id, status, deleted_at
               FROM file_records_local WHERE file_code = ?""",
            (file_code,),
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "eligible": False,
                "reason": _i18n_t('services.user_repair.s22'),
                "file_owner_id": 0,
            }
        uploader_id = row[0]
        status = (row[1] or "active").lower()
        deleted_at = row[2]
        # 物理删除的文件不可修复
        if deleted_at or status == "deleted":
            return {
                "eligible": False,
                "reason": _i18n_t('services.user_repair.s23'),
                "file_owner_id": uploader_id or 0,
            }
        # 只有文件所有者可修复(安全:防止跨用户修复)
        if uploader_id != user_id:
            return {
                "eligible": False,
                "reason": _i18n_t('services.user_repair.s24'),
                "file_owner_id": uploader_id or 0,
            }
        return {
            "eligible": True,
            "reason": "OK",
            "file_owner_id": uploader_id or 0,
        }
    except Exception as e:
        logger.warning(f"[user_repair] check_repair_eligibility 失败: {e}")
        return default


async def format_failure_reason(reason: dict) -> str:
    """格式化失败原因为用户可读文本(不含内部频道信息)。

    Args:
        reason: get_failure_reason 返回的字典

    Returns:
        多行纯文本(避免 Telegram markdown 解析问题)
    """
    if not reason:
        return _i18n_t('services.user_repair.s7')
    file_code = reason.get("file_code", "")
    # 脱敏文件码,只显示前 4 字符
    masked_code = _mask_code(file_code)
    reason_enum = reason.get("reason", REASON_UNKNOWN)
    reason_text = _REASON_LABELS.get(reason_enum, _i18n_t('services.user_repair.s8'))
    can_repair = bool(reason.get("can_repair", False))
    suggested = reason.get("suggested_action", "")
    icon = "🔧" if can_repair else "🚫"
    lines = [
        _i18n_t('services.user_repair.s9', icon=icon, masked_code=masked_code),
        _i18n_t('services.user_repair.s10', reason_text=reason_text),
    ]
    if suggested:
        lines.append(_i18n_t('services.user_repair.s12', suggested=suggested))
    if can_repair:
        lines.append(_i18n_t('services.user_repair.s13', masked_code=masked_code))
    else:
        lines.append(_i18n_t('services.user_repair.s14'))
    return "\n".join(lines)
