"""Up Bot - 上传机器人(环形冗余架构)
职责:接收用户文件 -> 轮转分发到活跃窗口内的 3 个 A 槽 (round-robin)
"""
from __future__ import annotations

import asyncio
import datetime
import os
import time
import uuid
try:
    import orjson as json
except ImportError:
    import json
from collections import defaultdict


def _json_dumps(obj, **kwargs):
    """序列化对象为 JSON 字符串，兼容 orjson(bytes) 与标准 json(str)。"""
    result = json.dumps(obj, **kwargs)
    if isinstance(result, bytes):
        return result.decode()
    return result

from services.sink_adapters.telegram_helpers import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from loguru import logger

from config import settings
from database import get_pending_uploads_col, get_active_cells_local
from database.cache_store import get_cache_store
from services.permission import check_upload_permission
from services import task_center, upload_receipt, collections as collections_svc, notifications
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics
from utils.task_utils import create_safe_task
from utils.force_join import check_force_join, three_bot_reminder, common_faq
# R65 P1-01: safe_reply_text / safe_send_message / safe_edit_message_text 改用
# services.sink_adapters typed adapter(接受 UserMessage | ErrorEnvelope,
# 拒绝裸 str);safe_copy_message / safe_copy_messages / safe_send_media_group
# 仍从 utils.flood_waiter 导入(sink_adapters 未提供这些 copy/media_group adapter)。
from utils.flood_waiter import safe_copy_message, safe_copy_messages, safe_send_media_group
from services.sink_adapters import (
    safe_reply_text,
    safe_send_message,
    safe_edit_message_text,
    build_provider_client,
)
from services.sink_adapters.provider_protocol import (
    PROVIDER_BACKEND_TELEGRAM as _PROVIDER_BACKEND_TELEGRAM,
    PROVIDER_BACKEND_CONTRACT as _PROVIDER_BACKEND_CONTRACT,
)
from utils.file_utils import detect_file_type, extract_file_meta
from utils.relay_auth import is_relay_sender_allowed
# R48 P1: 统一错误码协议化(替代裸字符串 RuntimeError)
from services.error_codes import AppError, ErrorCodes
# R40 P1-8: 维护模式检查装饰器(应用于高风险入口)
from services.maintenance_mode import require_maintenance_check
# R41 i18n: 国际化翻译(用户可见文本)
from services.i18n import translate as _i18n_t, get_i18n_manager
# R62 P1-05: 统一用户面消息类型 — 所有用户出口应接受 UserMessage 结构化对象,而非裸字符串
from services.user_message import UserMessage


def _t(user_id: int, key: str, **kwargs) -> str:
    """R41 i18n: 获取用户 locale 并翻译 key(带插值)。

    Args:
        user_id: Telegram 用户 ID(用于查询 locale 偏好)
        key: 翻译 key(如 "bot.upload_banned")
        **kwargs: 插值参数

    Returns:
        本地化字符串
    """
    manager = get_i18n_manager()
    locale = manager.get_user_locale(user_id) if user_id else "zh-CN"
    return manager.format_message(key, locale=locale, **kwargs)


# R62 P1-05: 所有用户出口应接受 UserMessage 结构化对象,而非裸字符串。
# 以下 _build_user_message / _reply_user_message 展示 UserMessage 替代裸字符串的
# 代表性模式(1-2 个示例,不强制重构所有现有 _t 调用)。
def _build_user_message(user_id: int, key: str, **kwargs) -> UserMessage:
    """R62 P1-05: 构造 UserMessage 结构化对象(替代裸 _t() 字符串)。

    与 _t() 的区别:
        - _t() 直接返回本地化字符串(丢失结构化信息,易在传播中拼接裸字符串)
        - _build_user_message() 返回 UserMessage 结构化对象,render() 时才转为字符串
        - UserMessage 携带 message_key / locale / params / trace_id,
          便于全链路日志关联和前端按协议渲染

    Args:
        user_id: Telegram 用户 ID(用于查询 locale 偏好)
        key: 翻译 key(如 "bot.upload_banned")
        **kwargs: ICU 插值参数(已脱敏,不含 password/secret/token 等敏感字段)

    Returns:
        UserMessage 实例(可在传播中保持结构化,render() 时才转为本地化字符串)
    """
    manager = get_i18n_manager()
    locale = manager.get_user_locale(user_id) if user_id else "zh-CN"
    # UserMessage.__post_init__ 会自动过滤敏感 params(password/secret/token 等)
    return UserMessage.from_key(key, locale=locale, params=kwargs)


async def _reply_user_message(message, msg: UserMessage, bot_id: int = 0, **kwargs) -> object:
    """R62 P1-05: 通过 UserMessage 结构化对象回复消息(替代裸字符串)。

    代表性示例:展示 UserMessage 替代 safe_reply_text(message, "裸字符串") 的模式。
    UserMessage.render() 时才通过 i18n_manager 转为本地化字符串,
    避免 wrapper / 别名 / 函数返回值传播过程中的裸字符串泄露。

    用法:
        # 旧模式(裸字符串,易在 wrapper 中泄露):
        await safe_reply_text(update.message, _t(user.id, "bot.upload_banned"))
        # 新模式(结构化 UserMessage):
        msg = _build_user_message(user.id, "bot.upload_banned")
        await _reply_user_message(update.message, msg)

    Args:
        message: Telegram Message 对象(update.message)
        msg: UserMessage 实例(message_key + locale + params 已结构化)
        bot_id: bot 账号 ID(用于 FloodWait 退避隔离)
        **kwargs: 额外 Telegram reply_text 参数(parse_mode 等)

    Returns:
        Telegram Message 对象(reply_text 调用结果)
    """
    # R62 P1-05: 渲染 UserMessage 为本地化字符串(惰性渲染,真正写入用户面前才转字符串)
    text = msg.render(get_i18n_manager())
    # R65 P1-01: safe_reply_text 已切换为 typed adapter(接受 UserMessage | ErrorEnvelope),
    # 用 from_raw_text 将已渲染字符串通过类型边界(过渡期;新代码应使用 from_key)。
    # 复用 safe_reply_text 的 FloodWait 退避机制(bot_id 透传给底层 flood_waiter)
    return await safe_reply_text(
        message, UserMessage.from_raw_text(text), bot_id=bot_id, **kwargs
    )

TOKEN = settings.UPLOAD_BOT_TOKEN

_pending_media_groups: dict[str, dict] = {}
_active_a_slots: list[dict] = []
# Manifest: channel_id → group_id 内存缓存(从 cells_local 加载,定期刷新)
_channel_to_group: dict[int, int] = {}
_channel_to_group_ts: float = 0.0
_CHANNEL_GROUP_CACHE_TTL: float = 300.0  # 缓存 300 秒
# 模块级存储：上传元数据，避免 context.user_data 在 callback 间丢失
# P2: 键改为 "user_id:upload_msg_id" 复合键,避免同用户并发上传互相覆盖
_pending_upload_meta: dict[str, dict] = {}
_active_slot_index: int = 0
_external_buffers: dict[str, dict] = {}
# R45: 懒加载 Lock,避免模块导入时 Python 3.9 要求事件循环存在
_mg_lock: asyncio.Lock | None = None
_pending_lock: asyncio.Lock | None = None


def _get_mg_lock() -> asyncio.Lock:
    """懒加载 media group lock。"""
    global _mg_lock
    if _mg_lock is None:
        _mg_lock = asyncio.Lock()
    return _mg_lock


def _get_pending_lock() -> asyncio.Lock:
    """懒加载 pending upload lock。"""
    global _pending_lock
    if _pending_lock is None:
        _pending_lock = asyncio.Lock()
    return _pending_lock
# PRE-15: 追踪已完成的 _finalize_upload 消息 ID，防止 Telegram 重复回调覆盖成功消息
_finalized_msg_ids: set[int] = set()
# 模块级 bot 引用,供 _flush_external_buffer 等非 handler 函数使用
_bot = None
# 媒体组路由:media_group_id -> (external_code, created_at)
# 当 relay 用 grouping=True 发送时,只有第一条消息有 EXTERNAL_RELAY: caption,
# 后续消息靠 media_group_id 匹配此映射,统一走 _handle_external_relay_file
# P2: 带 created_at 时间戳,改为惰性清理(超过 300s 才清),避免每轮全清破坏防重复
_external_mgid_map: dict[str, tuple[str, float]] = {}
# 内存级 file_unique_id 去重：external_code -> (set of file_unique_id, created_at)
_external_fuid_dedup: dict[str, tuple[set[str], float]] = {}
# R45: 媒体组 group-level aggregate — 跟踪每个媒体组内所有文件的状态,
# 只有所有文件都 READY 时才标记 group READY(禁止只按单文件状态判断)。
# 结构: {media_group_id: {"files": {file_unique_id: {state, message_id, channel_id, ...}}, ...}}
_media_group_states: dict[str, dict] = {}


def _decode_external_code(code_part: str) -> str:
    """解码 external_code，支持 hex 编码（H:前缀）和原始格式（向后兼容）。
    emoji 码在 Telethon→Bot API 传输中会变成 NULL 字符，用 hex 编码规避。"""
    code_part = code_part.strip()
    if code_part.startswith("H:"):
        try:
            return bytes.fromhex(code_part[2:]).decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return code_part[2:]
    return code_part


def _extract_file_unique_id(msg) -> str:
    """从 PTB Message 对象提取 file_unique_id(跨 bot 稳定去重键)。"""
    if not msg:
        return ""
    if msg.photo:
        return msg.photo[-1].file_unique_id
    if msg.video:
        return msg.video.file_unique_id
    if msg.document:
        return msg.document.file_unique_id
    if msg.audio:
        return msg.audio.file_unique_id
    if msg.voice:
        return msg.voice.file_unique_id
    if msg.sticker:
        return msg.sticker.file_unique_id
    if msg.animation:
        return msg.animation.file_unique_id
    return ""


# ─── R35 P0-4: upload_session / upload_outbox 接线辅助函数 ───
# 这些函数将 _pending_upload_meta(内存)与 upload_sessions(SQLite 权威)双写,
# 失败时仅记录日志,不影响主上传流程(渐进式接线)。

async def create_upload_session_strict(
    user_id: int,
    source_msg_ids: list | None = None,
    options: dict | None = None,
) -> str:
    """R38 P0-3 / R39 P0-5: 严格创建上传会话(upload_sessions 表),返回 upload_id(UUID)。

    失败时抛 DurabilityError,由调用方决定是否回滚主流程。
    不返回空字符串(避免主流程误以为已创建会话而继续推进状态机)。

    R39 P0-5: 新增 `ok is True` 检查 — 即使 CacheStore.create_upload_session
    未抛异常但返回 False(理论兜底路径),也视为失败抛 DurabilityError,
    避免 CacheStore 静默 return 绕过 strict 检查。
    """
    from utils.exceptions import DurabilityError
    upload_id = str(uuid.uuid4())
    try:
        store = get_cache_store()
        # R39 P0-5: 检查返回值 ok is True (cache_store 现已返回 bool)
        # 若返回 False 或 None(理论兜底),视为失败抛 DurabilityError
        ok = await store.create_upload_session(
            upload_id, user_id,
            source_msg_ids=source_msg_ids,
            options_json=options,
            trace_id=f"up_bot:{upload_id[:8]}",
        )
        if ok is not True:
            # R39 P0-5: cache_store 未抛异常但返回非 True(兜底路径)
            raise DurabilityError(
                f"create upload session returned false (upload_id={upload_id}, ok={ok!r})"
            )
    except DurabilityError:
        # 已经是 DurabilityError,直接向上传播(避免被下面 Exception 分支包装两次)
        raise
    except Exception as e:
        # R38 P0-3 / R39 P0-5: 创建失败抛 DurabilityError,不返回空串
        # R39 P0-5: StoreUnavailable 也会被此分支捕获并包装为 DurabilityError
        raise DurabilityError(
            f"create upload session returned false / failed: {e}"
        ) from e
    return upload_id


async def _create_upload_session_for_upload(
    user_id: int,
    source_msg_ids: list | None = None,
    options: dict | None = None,
) -> str:
    """创建上传会话(upload_sessions 表),返回 upload_id(UUID)。

    R38 P0-3: 改为调用 strict 版本(不捕获异常),让异常传播到上传主流程,
    由主流程决定是否回滚。原版本捕获异常返回空串会导致后续状态机推进
    失败但被静默吞掉,造成数据丢失。
    """
    return await create_upload_session_strict(
        user_id,
        source_msg_ids=source_msg_ids,
        options=options,
    )


async def _transition_upload_session_safe(
    upload_id: str, new_status: str, reason: str = "", **update_fields,
) -> None:
    """安全推进 upload_session 状态(失败不影响主流程)。

    upload_id 为空时直接返回(会话未创建,跳过状态推进)。

    R37 P1-3: 此 *_safe() 包装仅用于 metrics/日志场景的 best-effort 写入。
    权威状态写入必须使用 _transition_upload_session_strict 抛 DurabilityError。
    """
    if not upload_id:
        return
    try:
        await get_cache_store().transition_upload_session(
            upload_id, new_status, reason=reason, **update_fields,
        )
    except Exception as e:
        logger.warning(f"[Up] 推进 upload_session 状态失败 upload_id={upload_id} -> {new_status}: {e}")


async def _transition_upload_session_strict(
    upload_id: str, new_status: str, reason: str = "", **update_fields,
) -> None:
    """R37 P1-3 / R38 P0-3: 权威推进 upload_session 状态(失败抛 DurabilityError)。

    与 _safe 版本的区别: 异常向上传播,由调用方决定是否中断主流程。
    用于权威状态写入(如 MANIFEST_PENDING → READY 等关键状态迁移)。

    R38 P0-3: upload_id 为空抛 DurabilityError(原版本静默 return 会导致
    会话从未创建却被视为已推进,掩盖上游 create_upload_session 失败)。
    """
    from utils.exceptions import DurabilityError
    if not upload_id:
        # R38 P0-3: upload_id 为空抛 DurabilityError,不再静默 return
        raise DurabilityError(
            _i18n_t('bot.up.s4', new_status=new_status)
        )
    try:
        await get_cache_store().transition_upload_session(
            upload_id, new_status, reason=reason, **update_fields,
        )
    except Exception as e:
        raise DurabilityError(
            _i18n_t('bot.up.s17', upload_id=upload_id, new_status=new_status, e=e)
        ) from e


async def _create_outbox_entry_safe(
    outbox_id: str, upload_id: str, user_id: int,
    channel_id: int, msg_ids: list | None = None,
    file_meta: list | None = None, event_type: str = "REGISTER_MANIFEST",
    protect: int = 0,
) -> None:
    """安全创建 upload_outbox 条目(失败不影响主流程)。

    upload_id 为空时直接返回(outbox 依赖 upload_session 关联)。

    R37 P1-3: 此 *_safe() 包装仅用于 metrics/日志场景的 best-effort 写入。
    权威 outbox 写入必须使用 _create_outbox_entry_strict 抛 DurabilityError,
    否则文件已复制但 manifest 不会被登记(永久数据丢失)。
    """
    if not upload_id:
        return
    try:
        await get_cache_store().create_outbox_entry(
            outbox_id, upload_id, "", user_id, channel_id,
            storage_msg_ids=msg_ids,
            batch_file_meta=file_meta,
            task_type="single",
            protect_content=protect,
            event_type=event_type,
        )
    except Exception as e:
        logger.warning(f"[Up] 创建 outbox 条目失败 upload_id={upload_id} event={event_type}: {e}")


async def _create_outbox_entry_strict(
    outbox_id: str, upload_id: str, user_id: int,
    channel_id: int, msg_ids: list | None = None,
    file_meta: list | None = None, event_type: str = "REGISTER_MANIFEST",
    protect: int = 0,
) -> None:
    """R37 P1-3 / R38 P0-3: 权威创建 upload_outbox 条目(失败抛 DurabilityError)。

    用于权威状态写入(REGISTER_MANIFEST / ARCHIVE_R100 等业务事件)。
    outbox 条目缺失会导致 OutboxWorker 无法消费该事件,造成永久数据丢失
    (如 manifest 未登记 → 文件已复制但无法被 Resolver 找到)。

    R38 P0-3: upload_id 为空抛 DurabilityError("missing upload_id"),
    不再静默 return(原版本会让文件已复制但 manifest 未登记的 silent 数据丢失)。
    store 返回 False 抛 DurabilityError("create outbox returned false")。
    """
    from utils.exceptions import DurabilityError
    if not upload_id:
        # R38 P0-3: upload_id 为空抛 DurabilityError("missing upload_id")
        raise DurabilityError(
            f"missing upload_id (event={event_type}, outbox_id={outbox_id})"
        )
    try:
        await get_cache_store().create_outbox_entry(
            outbox_id, upload_id, "", user_id, channel_id,
            storage_msg_ids=msg_ids,
            batch_file_meta=file_meta,
            task_type="single",
            protect_content=protect,
            event_type=event_type,
        )
    except Exception as e:
        # R38 P0-3: store 返回 False / 抛异常 → DurabilityError("create outbox returned false")
        raise DurabilityError(
            f"create outbox returned false: upload_id={upload_id} "
            f"event={event_type}: {e}"
        ) from e


# ─── R45: 媒体组 group-level aggregate + COPIED_UNREGISTERED ───
# R42 终审报告第 9 节整改:
# - Telegram copy 成功但 outbox 写失败时记录 COPIED_UNREGISTERED,不遗失目标 message_id
# - 媒体组要有 group-level aggregate,禁止只按单文件状态判断 READY


async def _append_audit_log_for_unregistered_failure(
    upload_id: str,
    file_unique_id: str,
    channel_id: int,
    message_id: int,
    reason: str,
    error: str,
) -> None:
    """R47 P1-E: 将 unregistered_copies 持久化失败写入 audit_log。

    当 copy 成功但 unregistered_copies 写入失败时,数据可能遗失
    (无 reconciled 行可供启动扫描发现),需写 audit_log 供运维人工 reconcile。
    """
    store = get_cache_store()
    if not store or not store._db:
        return
    from datetime import datetime as _dt
    now_iso = _dt.utcnow().isoformat()
    details = _json_dumps({
        "upload_id": upload_id,
        "file_unique_id": file_unique_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "reason": reason,
        "error": error,
        "action": "unregistered_copy_persist_failed",
    })
    await store._db.execute(
        "INSERT INTO audit_log (actor_id, actor_type, action, target_type, "
        "target_id, details, ip_addr, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (0, "system", "unregistered_copy_persist_failed",
         "unregistered_copy", f"{upload_id}:{file_unique_id}",
         details, "", now_iso),
    )
    await store._db.commit()


async def _mark_copied_unregistered(
    upload_id: str,
    media_group_id: str,
    file_unique_id: str,
    message_id: int,
    channel_id: int,
    reason: str = "",
) -> str:
    """R45/R46 P0-3: 记录 Telegram copy 成功但 outbox 写失败的情况(COPIED_UNREGISTERED)。

    R46 P0-3 整改:
      - 同时写入内存字典(_media_group_states)和持久表(unregistered_copies)。
      - 进程重启后可通过 list_unreconciled_copies() 扫描未 reconciled 行,
        优先补 Manifest 而非重新 copy。
      - Manifest outbox 成功后调用 mark_unregistered_copy_reconciled() 标记完成。

    R47 P1-E 整改(Up Bot 终审报告 17 节):
      - 持久化失败时不静默吞异常,改为 fail-closed:
        - 写入 audit_log(供运维感知 + 人工 reconcile,因启动扫描无法发现此行)
        - 返回 "partial_success" 让上层决定是否重试/reconcile
      - 不允许静默失败 — copy 成功但 unregistered record 写入失败时必须可感知

    Args:
        upload_id: 上传会话 ID
        media_group_id: 媒体组 ID(单文件时可用 file_unique_id 代替)
        file_unique_id: 文件唯一标识
        message_id: Telegram copy 成功返回的存储频道消息 ID
        channel_id: 存储频道 ID
        reason: 失败原因(如 "outbox_write_failed")

    Returns:
        "persisted": 内存 + SQLite 均成功
        "partial_success": 仅内存成功,SQLite 持久化失败(已写 audit_log,
                          下次启动扫描无法发现此行 — 需运维通过 audit_log 人工处理)
    """
    if not media_group_id:
        media_group_id = file_unique_id or upload_id
    # 1. 写入内存字典(向后兼容)
    mg_state = _media_group_states.get(media_group_id)
    if mg_state is None:
        mg_state = {
            "files": {},
            "upload_id": upload_id,
            "user_id": 0,
            "created_at": time.time(),
            "group_state": "pending",
        }
        _media_group_states[media_group_id] = mg_state
    files = mg_state.setdefault("files", {})
    files[file_unique_id] = {
        "state": "COPIED_UNREGISTERED",
        "message_id": message_id,
        "channel_id": channel_id,
        "reason": reason,
        "marked_at": time.time(),
    }
    # 2. R46 P0-3: 写入持久表 unregistered_copies
    # R47 P1-E: 持久化失败时 fail-closed(写 audit_log + 返回 partial_success)
    persist_ok = True
    persist_err: Exception | None = None
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if store and store._db:
            await store.insert_unregistered_copy(
                upload_id=upload_id,
                file_unique_id=file_unique_id,
                channel_id=channel_id,
                message_id=message_id,
                media_group_id=media_group_id,
                reason=reason,
            )
        else:
            persist_ok = False
            persist_err = RuntimeError("cache_store or _db not initialized")
    except Exception as err:
        persist_ok = False
        persist_err = err

    if not persist_ok:
        # R47 P1-E: fail-closed — 持久化失败不静默
        # 写入 audit_log(供运维感知 + 人工 reconcile,因启动扫描无法发现此行)
        try:
            await _append_audit_log_for_unregistered_failure(
                upload_id=upload_id,
                file_unique_id=file_unique_id,
                channel_id=channel_id,
                message_id=message_id,
                reason=reason,
                error=str(persist_err),
            )
        except Exception as audit_err:
            logger.error(
                f"[Up][R47] audit_log 写入也失败(双故障,需人工检查): {audit_err}"
            )
        logger.error(
            f"[Up][R47] unregistered_copies 持久化失败(partial_success,已写 audit_log): "
            f"upload_id={upload_id} fuid={file_unique_id} "
            f"ch={channel_id} msg_id={message_id}: {persist_err}"
        )
    logger.warning(
        f"[Up][R46] COPIED_UNREGISTERED: upload_id={upload_id} "
        f"mg={media_group_id} fuid={file_unique_id} "
        f"msg_id={message_id} ch={channel_id} reason={reason}"
    )
    return "persisted" if persist_ok else "partial_success"


def _evaluate_media_group_state(media_group_id: str) -> str:
    """R45: 根据媒体组内所有文件状态判定 group 级状态(group-level aggregate)。

    禁止只按单文件状态判断 READY — 必须所有文件都 READY 才标记 group READY。

    Returns:
        'ready'   — 所有文件都 READY
        'failed'  — 所有文件都 FAILED
        'partial' — 部分文件 FAILED(但非全部)
        'pending' — 无失败,但仍有文件未 READY(PENDING/COPIED_UNREGISTERED)
    """
    mg_state = _media_group_states.get(media_group_id)
    if not mg_state or not mg_state.get("files"):
        return "pending"
    files = mg_state["files"]
    total = len(files)
    ready_count = 0
    failed_count = 0
    for fuid, fstate in files.items():
        state = fstate.get("state", "PENDING") if isinstance(fstate, dict) else "PENDING"
        if state == "READY":
            ready_count += 1
        elif state == "FAILED":
            failed_count += 1
    if ready_count == total:
        return "ready"
    if failed_count == total:
        return "failed"
    if failed_count > 0:
        return "partial"
    return "pending"


# ─── R35 P0-4 §24: replication_tasks 副本复制任务接线 ───
# 在 copy_messages 调用点创建/推进 replication_tasks 状态机:
# PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED / FAILED
# 异常安全: 所有 replication_tasks 写入失败只记录 warning,不传播到主流程。

async def _create_replication_task_safe(
    group_id: int, file_unique_id: str, src_channel_id: int,
    dst_channel_id: int, src_msg_id: int, media_group_id: str = "",
) -> int:
    """安全创建 replication_task(PLANNED),返回 task_id(0 表示失败)。

    异常安全: 失败只记录 warning,不传播到主流程。
    """
    if not file_unique_id or not group_id:
        return 0
    try:
        return await get_cache_store().create_replication_task(
            group_id, file_unique_id, src_channel_id, dst_channel_id,
            src_msg_id, media_group_id=media_group_id,
        )
    except Exception as e:
        logger.warning(f"[Up] 创建 replication_task 失败(不影响主流程, fuid={file_unique_id}): {e}")
        return None


async def _mark_replication_copying_safe(task_id: int) -> None:
    """安全标记 replication_task 为 COPYING。"""
    if not task_id:
        return
    try:
        await get_cache_store().mark_replication_copying(task_id)
    except Exception as e:
        logger.warning(f"[Up] 标记 replication COPYING 失败(不影响主流程, task_id={task_id}): {e}")


async def _mark_replication_copied_safe(task_id: int, dst_msg_id: int) -> None:
    """安全标记 replication_task 为 COPIED_UNVERIFIED。"""
    if not task_id:
        return
    try:
        await get_cache_store().mark_replication_copied(task_id, dst_msg_id)
    except Exception as e:
        logger.warning(f"[Up] 标记 replication COPIED_UNVERIFIED 失败(不影响主流程, task_id={task_id}): {e}")


async def _mark_replication_committed_safe(task_id: int) -> None:
    """安全标记 replication_task 为 COMMITTED。"""
    if not task_id:
        return
    try:
        await get_cache_store().mark_replication_committed(task_id)
    except Exception as e:
        logger.warning(f"[Up] 标记 replication COMMITTED 失败(不影响主流程, task_id={task_id}): {e}")


async def _mark_replication_failed_safe(task_id: int, reason: str) -> None:
    """安全标记 replication_task 为 FAILED/PLANNED(重试)。"""
    if not task_id:
        return
    try:
        await get_cache_store().mark_replication_failed(task_id, reason)
    except Exception as e:
        logger.warning(f"[Up] 标记 replication FAILED 失败(不影响主流程, task_id={task_id}): {e}")


def _build_input_media(file_meta: dict):
    """从 file_meta 构造 InputMedia* 对象(用于 send_media_group)。
    返回 None 表示该类型不支持媒体组(animation/sticker/voice)。
    """
    ftype = file_meta.get("type", "")
    file_id = file_meta.get("file_id", "")
    if not file_id:
        return None
    if ftype == "photo":
        return InputMediaPhoto(media=file_id)
    if ftype == "video":
        return InputMediaVideo(media=file_id)
    if ftype == "audio":
        return InputMediaAudio(media=file_id)
    if ftype == "document":
        return InputMediaDocument(media=file_id)
    return None


async def _ensure_channel_group_map():
    """刷新 channel_id → group_id 内存缓存(从 cells_local 加载)。

    group_id 从 slot_id 解析: 'a1'→1, 's1a'→1, 's1b'→1, 'a2'→2 ...
    """
    global _channel_to_group, _channel_to_group_ts
    import re as _re
    now = time.time()
    if _channel_to_group_ts > 0 and (now - _channel_to_group_ts) < _CHANNEL_GROUP_CACHE_TTL:
        return
    try:
        store = get_cache_store()
        cells = await store.get_all_cells_local()
        new_map = {}
        for c in cells:
            sid = c.get("slot_id", "")
            chan = c.get("channel_id", 0)
            if not sid or not chan:
                continue
            m = _re.match(r'[as](\d+)[ab]?', sid)
            if m:
                new_map[chan] = int(m.group(1))
        _channel_to_group = new_map
        _channel_to_group_ts = now
        logger.debug(f"[Up] channel→group 映射已刷新: {len(new_map)} 条")
    except Exception as e:
        logger.warning(f"[Up] 刷新 channel→group 映射失败: {e}")


async def _enrich_file_meta_for_replica(
    file_meta: dict,
    target_channel_id: int,
    file_unique_id: str = "",
    media_group_id: str = "",
) -> dict:
    """R36 B0-1: 为 file_meta 注入结构化副本信息字段。

    将 group_id/file_unique_id/media_group_id 写入 file_meta dict,
    使下游 idx_bot → enqueue_job → dsp_bot 无需解析 batch_file_meta JSON,
    直接从结构化字段读取,让 ReplicaAwareResolver 成为真实投递主路径。

    Args:
        file_meta: 原 file_meta dict(含 type/file_id)
        target_channel_id: 目标存储频道(用于查 channel_id→group_id 映射)
        file_unique_id: Telegram 文件唯一标识(跨 bot 稳定去重键)
        media_group_id: Telegram 媒体组 ID(空串表示独立文件)

    Returns:
        新 dict(原 file_meta + group_id/file_unique_id/media_group_id);
        不修改原 dict。原 dict 字段保持不变。
    """
    await _ensure_channel_group_map()
    # channel_id → group_id 映射未命中时返回 0(数据不完整,下游 fail-closed 进入 retry)
    gid = _channel_to_group.get(target_channel_id, 0)
    enriched = dict(file_meta) if isinstance(file_meta, dict) else {"type": "", "file_id": ""}
    enriched["group_id"] = gid
    enriched["file_unique_id"] = file_unique_id or ""
    enriched["media_group_id"] = media_group_id or ""
    return enriched


async def _register_manifest(channel_id: int, message_id: int, msg, media_type: str = "", file_unique_id_override: str = "", media_group_id: str = "", upload_id_override: str = ""):
    """写入 Active 频道后登记 manifest。

    R47 P1-E 整改(Up Bot 终审报告 17 节):
      - 删除吞异常兼容路径,异常向上传播让上层处理。
      - 不允许静默失败 — Manifest 写入失败时 raise,由调用方决定
        是否记录到 unregistered_copies 错误字段或触发重试。
      - 启动扫描 _reconcile_unregistered_copies() 依赖此方法抛异常
        以决定是否标记 reconciled(失败则不标记,下次启动重试)。
      - 删除的旧逻辑:外层 try/except 捕获后调用 _mark_copied_unregistered
        记录 COPIED_UNREGISTERED(吞异常),以及 mark_unregistered_copy_reconciled
        的 `except Exception: pass` 静默吞异常。

    R47 P1-E 修复:新增 upload_id_override 参数。
      - 旧版 `mark_unregistered_copy_reconciled(upload_id=mgid or fuid, ...)` 在
        media_group_id 为空且 upload_id != fuid 时无法匹配持久化行的 PK,
        导致 reconciled_at 永不更新(行持续出现在未对账列表,启动扫描重复补写)。
      - 新版:启动扫描 _reconcile_unregistered_copies 从持久化行读取真实 upload_id
        并通过 upload_id_override 传入,确保 PK 精确匹配。
      - 旧调用方不传此参数时,fallback 为 `mgid or fuid`(保持向后兼容)。

    Args:
        channel_id: 存储频道 ID
        message_id: 该频道中的消息 ID
        msg: PTB Message 对象(用于提取 file_unique_id),或 None
        media_type: 文件类型(document/photo/video...)
        file_unique_id_override: 直接提供 file_unique_id(优先于 msg 提取,用于 copy_messages 返回 MessageId 无 fuid 的场景)
        media_group_id: 媒体组分组键(空串表示独立文件)。mon_bot 据此避免跨批次拆散相册。
                       可传源 media_group_id 或 send_media_group 返回的 media_group_id。
        upload_id_override: 显式提供 upload_id(优先于 mgid/fuid fallback),
                            用于精确匹配 unregistered_copies 表 PK。
                            启动扫描必须传入,否则 PK 不匹配会导致行永不 reconciled。

    Raises:
        RuntimeError: 频道未映射到 group_id(_channel_to_group 映射未刷新)
        Exception: upsert_manifest / mark_unregistered_copy_reconciled 失败时向上传播
    """
    fuid = file_unique_id_override or (_extract_file_unique_id(msg) if msg else "")
    if not fuid:
        return
    # 若未显式传入,尝试从 msg 提取 media_group_id
    mgid = media_group_id or ""
    if not mgid and msg is not None:
        mgid = getattr(msg, "media_group_id", "") or ""
    # R47 P1-E: 删除吞异常兼容路径 — 异常向上传播让上层处理(不静默失败)
    await _ensure_channel_group_map()
    group_id = _channel_to_group.get(channel_id)
    if group_id is None:
        raise RuntimeError(
            _i18n_t('bot.up.s5', channel_id=channel_id)
        )
    store = get_cache_store()
    await store.upsert_manifest(group_id, fuid, channel_id, message_id, media_type, mgid)
    # R47 P1-E: Manifest 成功后标记 unregistered_copies reconciled
    # 异常向上传播(不吞异常) — 调用方可知 reconciled 标记是否成功,
    # 失败时下次启动扫描会发现行仍存在并重试(manifest upsert 幂等)
    # R47 P1-E 修复:优先使用 upload_id_override 精确匹配持久化行 PK,
    # fallback 为 mgid 或 fuid(向后兼容旧调用方)
    await store.mark_unregistered_copy_reconciled(
        upload_id=upload_id_override or mgid or fuid,
        file_unique_id=fuid,
        channel_id=channel_id,
        message_id=message_id,
    )


async def _reconcile_unregistered_copies() -> dict:
    """R47 P1-E: 启动扫描 — 实际补写 Manifest 并标记 reconciled。

    扫描 unregistered_copies 表中所有未对账(reconciled_at IS NULL)行,
    对每行调用 _register_manifest 补写 Manifest,成功后由 _register_manifest
    内部调用 mark_unregistered_copy_reconciled 标记完成。
    失败的行不标记,下次启动重试(_register_manifest 抛异常时不捕获,
    仅记录错误日志,继续处理下一行)。

    与旧版"只 list 不补写"的区别:
      - 旧版:仅调用 list_unreconciled_copies() 列出未对账行,不实际补写 Manifest
      - 新版:对每行实际调用 _register_manifest 补写 Manifest,并标记 reconciled

    Returns:
        统计字典 {"total": N, "reconciled": M, "failed": K}
    """
    store = get_cache_store()
    if not store or not store._db:
        logger.warning("[Up][R47] 启动扫描: cache_store 未就绪,跳过")
        return {"total": 0, "reconciled": 0, "failed": 0}
    try:
        copies = await store.list_unreconciled_copies(limit=500)
    except Exception as e:
        logger.error(f"[Up][R47] 启动扫描 list_unreconciled_copies 失败: {e}")
        return {"total": 0, "reconciled": 0, "failed": 0}
    total = len(copies)
    if total == 0:
        logger.info("[Up][R47] 启动扫描: 无未对账副本")
        return {"total": 0, "reconciled": 0, "failed": 0}
    reconciled = 0
    failed = 0
    logger.info(f"[Up][R47] 启动扫描: 发现 {total} 条未对账副本,开始补写 Manifest")
    for cp in copies:
        upload_id = cp.get("upload_id", "") or ""
        fuid = cp.get("file_unique_id", "") or ""
        mgid = cp.get("media_group_id") or ""
        try:
            channel_id = int(cp.get("channel_id", 0) or 0)
            message_id = int(cp.get("message_id", 0) or 0)
        except (ValueError, TypeError):
            logger.warning(f"[Up][R47] 跳过无效行(channel/message 非数字): {cp}")
            failed += 1
            continue
        if not fuid or not channel_id or not message_id:
            logger.warning(f"[Up][R47] 跳过无效行(关键字段缺失): {cp}")
            failed += 1
            continue
        try:
            # R47 P1-E: 实际补写 Manifest(_register_manifest 不吞异常,失败则 raise)
            # msg=None,file_unique_id_override/media_group_id/upload_id_override 从持久化行读取
            # upload_id_override 必须传入:持久化行 PK 使用真实 upload_id(如 "r47-001"),
            # 若 fallback 为 mgid 或 fuid 将导致 PK 不匹配、reconciled_at 永不更新
            await _register_manifest(
                channel_id=channel_id,
                message_id=message_id,
                msg=None,
                media_type="",  # unregistered_copies 未存储 media_type,留空
                file_unique_id_override=fuid,
                media_group_id=mgid,
                upload_id_override=upload_id,
            )
            reconciled += 1
            logger.info(
                f"[Up][R47] 补写 Manifest 成功并标记 reconciled: "
                f"fuid={fuid} ch={channel_id} msg_id={message_id}"
            )
        except Exception as e:
            failed += 1
            logger.error(
                f"[Up][R47] 补写 Manifest 失败(不标记 reconciled,下次启动重试): "
                f"fuid={fuid} ch={channel_id} msg_id={message_id}: {e}"
            )
    logger.info(
        f"[Up][R47] 启动扫描完成: total={total} reconciled={reconciled} failed={failed}"
    )
    return {"total": total, "reconciled": reconciled, "failed": failed}


async def _check_dedup(target_channel: int, msg) -> dict | None:
    """秒传去重检查:查询该组内是否已存在此文件(file_unique_id)的记录。

    Args:
        target_channel: 目标存储频道(用于解析 group_id)
        msg: PTB Message 对象(用于提取 file_unique_id)

    Returns:
        命中时返回 {channel_id, message_id, media_type, media_group_id};
        未命中或查询失败返回 None。
    """
    fuid = _extract_file_unique_id(msg)
    if not fuid:
        return None
    await _ensure_channel_group_map()
    group_id = _channel_to_group.get(target_channel)
    if group_id is None:
        return None
    try:
        return await get_cache_store().get_existing_file_in_group(group_id, fuid)
    except Exception as e:
        logger.warning(f"[Up] 去重查询失败 (fuid={fuid}): {e}")
        return None


async def _cleanup_pending():
    """定期清理超时未完成的 media group 和 external buffer。"""
    while True:
        try:
            now = time.time()
            async with _get_mg_lock():
                # 清理超时的 media group (>30s)
                expired_mg = [k for k, v in _pending_media_groups.items() if now - v.get("created_at", 0) > 30]
                for k in expired_mg:
                    grp = _pending_media_groups.pop(k, None)
                    if grp and grp.get("timer"):
                        grp["timer"].cancel()
                    logger.warning(f"[up_bot] 清理超时 media group: {k}")
                # 清理超时的 external buffer (>120s)
                expired_ext = [k for k, v in _external_buffers.items() if now - v.get("created_at", 0) > 120]
                for k in expired_ext:
                    buf = _external_buffers.pop(k, None)
                    if buf and buf.get("timer"):
                        buf["timer"].cancel()
                    logger.warning(f"[up_bot] 清理超时 external buffer: {k}")
            # PRE-15: 限制 _finalized_msg_ids 大小，防止内存泄漏
            if len(_finalized_msg_ids) > 10000:
                _finalized_msg_ids.clear()
                logger.debug("[up_bot] 清理 _finalized_msg_ids (超过10000条)")
            # P2: 惰性清理 _external_mgid_map (超过 300s 的旧映射)
            if _external_mgid_map:
                now_ts = time.time()
                expired_mgids = [k for k, (_, ts) in _external_mgid_map.items() if now_ts - ts > 300]
                for k in expired_mgids:
                    _external_mgid_map.pop(k, None)
                if expired_mgids:
                    logger.debug(f"[up_bot] 惰性清理 _external_mgid_map: {len(expired_mgids)} 个")
            # P2: 惰性清理超时的内存级去重集合
            if _external_fuid_dedup:
                now_ts = time.time()
                expired_dedup = [k for k, (_, ts) in _external_fuid_dedup.items() if now_ts - ts > 300]
                for k in expired_dedup:
                    _external_fuid_dedup.pop(k, None)
                if expired_dedup:
                    logger.debug(f"[up_bot] 惰性清理 _external_fuid_dedup: {len(expired_dedup)} 个")
        except Exception as e:
            logger.error(f"[up_bot] 清理超时缓冲区异常: {e}")
        await asyncio.sleep(60)


async def _refresh_active_slots():
    """刷新当前 Active A 槽列 (读取 cells 表)"""
    global _active_a_slots
    try:
        _active_a_slots = await get_active_cells_local()
        logger.debug(f"[Up] 刷新 Active 槽位: {len(_active_a_slots)} 个")
    except Exception as e:
        logger.error(f"[Up] 刷新槽位失败: {e}")


# ─── R100 归档频道缓存 ───
_r100_channel_id: int = 0
_r100_channel_ts: float = 0.0
_R100_CACHE_TTL: float = 600.0  # 缓存 600 秒


async def _get_r100_channel() -> int:
    """获取 R100 归档频道 ID（从 cells 表查询，缓存 600 秒）。"""
    global _r100_channel_id, _r100_channel_ts
    now = time.time()
    if _r100_channel_ts > 0 and (now - _r100_channel_ts) < _R100_CACHE_TTL:
        return _r100_channel_id
    try:
        from database.cache_store import get_cache_store
        cells = await get_cache_store().get_all_cells_local()
        _r100_channel_id = 0
        for c in cells:
            if c.get("is_r100") == 1 or c.get("status") == "r100":
                _r100_channel_id = c.get("channel_id", 0)
                break
        _r100_channel_ts = now
        if _r100_channel_id:
            logger.debug(f"[Up] R100 归档频道: {_r100_channel_id}")
        else:
            logger.debug("[Up] 未找到 R100 归档频道，跳过 R100 归档")
    except Exception as e:
        logger.warning(f"[Up] 获取 R100 频道失败: {e}")
    return _r100_channel_id


async def _forward_to_r100(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int):
    """将文件转发到 R100 归档频道（fire-and-forget，不阻塞主流程）。"""
    try:
        r100_ch = await _get_r100_channel()
        if not r100_ch:
            return
        await safe_copy_message(context.bot, r100_ch, from_chat_id, message_id)
        logger.debug(f"[Up] R100 归档成功: msg_id={message_id} -> channel={r100_ch}")
    except Exception as e:
        logger.warning(f"[Up] R100 归档失败 msg_id={message_id}: {e}")


# ─── R36 B0-2: OutboxWorker 回调函数(strict 版本,异常向上传播以触发重试) ───
# _register_manifest 内部已 try/except 吞异常(向后兼容),这里提供 strict wrapper:
# 失败时抛出异常,让 OutboxWorker 调用 mark_outbox_failed 触发重试/DEAD 流程

async def _outbox_register_manifest_strict(
    channel_id: int, message_id: int, file_meta: dict,
) -> None:
    """OutboxWorker 调用的 REGISTER_MANIFEST 处理器(strict)。

    与 _register_manifest 的区别:
    - 接受 file_meta dict(含 file_unique_id/group_id/media_group_id/type)而非 PTB Message
    - 异常向上传播(不吞异常),让 OutboxWorker 触发重试
    - 幂等保证: upsert_manifest(INSERT OR REPLACE)语义,重复调用不创建重复记录
    """
    fuid = (file_meta or {}).get("file_unique_id", "") if isinstance(file_meta, dict) else ""
    if not fuid:
        # R37 P0-2: file_unique_id 缺失时绝不可静默通过(否则永久丢失 Manifest 且无法恢复)
        # 必须 fail-closed 抛 DurabilityError,让 OutboxWorker 标记 FAILED/DEAD,
        # 保留完整上下文(outbox_id/upload_id/storage_msg_id)供人工修复。
        from utils.exceptions import DurabilityError
        raise DurabilityError(
            _i18n_t('bot.up.s6', channel_id=channel_id, message_id=message_id)
        )
    media_type = (file_meta or {}).get("type", "") if isinstance(file_meta, dict) else ""
    mgid = (file_meta or {}).get("media_group_id", "") if isinstance(file_meta, dict) else ""
    await _ensure_channel_group_map()
    group_id = _channel_to_group.get(channel_id)
    if group_id is None:
        # 频道未映射到 group,manifest 无法注册
        # 抛异常让 OutboxWorker 重试(可能下次刷新 channel→group 映射后命中)
        raise RuntimeError(
            _i18n_t('bot.up.s7', channel_id=channel_id)
        )
    store = get_cache_store()
    await store.upsert_manifest(group_id, fuid, channel_id, message_id, media_type, mgid)


async def _outbox_archive_to_r100_strict(
    storage_channel_id: int, storage_msg_id: int,
) -> None:
    """OutboxWorker 调用的 ARCHIVE_R100 处理器(strict)。

    与 _forward_to_r100 的区别:
    - 源改为 storage 频道(更可靠,避免用户删除原消息导致 R100 失败)
    - 不需要 context 参数(OutboxWorker 仅持有 bot 引用)
    - 异常向上传播(不吞异常),让 OutboxWorker 触发重试
    """
    if not _bot:
        # R48 P1: 协议化错误码替代裸字符串 RuntimeError
        raise AppError(ErrorCodes.UPLOAD_OUTBOX_BOT_UNINITIALIZED)
    r100_ch = await _get_r100_channel()
    if not r100_ch:
        # R100 频道未配置,视为完成(不阻塞,日志已记录)
        logger.warning("[Up][Outbox] R100 频道未配置,跳过 ARCHIVE_R100")
        return
    await safe_copy_message(_bot, r100_ch, storage_channel_id, storage_msg_id)
    logger.debug(
        f"[Up][Outbox] R100 归档成功: storage_msg_id={storage_msg_id} "
        f"-> R100={r100_ch}"
    )


async def _get_upload_target_channel() -> int:
    """选择上传目标频道:在活跃频道间轮转(round-robin)"""
    global _active_slot_index
    if not _active_a_slots:
        await _refresh_active_slots()
    if not _active_a_slots:
        logger.error("[Up] 无可用活跃槽位，无法处理上传请求")
        # R48 P1: 协议化错误码替代裸字符串 RuntimeError
        raise AppError(ErrorCodes.UPLOAD_SLOT_NONE_ACTIVE)
    # 轮转:每次取下一个活跃频道
    # P1-7: 在锁内同时完成取列表引用+取模+读 channel_id,
    # 避免 _refresh_active_slots 替换列表后 idx 越界。
    async with _get_pending_lock():
        idx = _active_slot_index % len(_active_a_slots)
        _active_slot_index += 1
        channel_id = _active_a_slots[idx]["channel_id"]
    logger.debug(f"[Up] 轮转分发 频道 {channel_id} (index={idx}/{len(_active_a_slots)})")
    return channel_id


# ─── 以下逻辑与原来基本相同，channel 选择改为环形槽位 ───


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    user = update.effective_user
    from services.permission import get_or_create_user
    await get_or_create_user(user.id, user.username, user.first_name)
    # R41 i18n: 欢迎语文本走 locale 翻译
    # R65 P1-01: safe_reply_text typed adapter 要求 UserMessage,用 from_raw_text 包装
    await safe_reply_text(update.message,
        UserMessage.from_raw_text(
            "📤 " + _t(user.id, "bot.upload_start_welcome")
            + "\n" + three_bot_reminder()
        )
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        UserMessage.from_raw_text(
            _i18n_t('bot.up.s18')
            + common_faq()
        )
    )


@require_maintenance_check(action=_i18n_t('bot.up.s1'))
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    if not await check_upload_permission(user.id):
        # R41 i18n: 上传禁用文本走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("🚫 " + _t(user.id, "bot.upload_banned")))
        return

    target_channel_id = await _get_upload_target_channel()
    context.user_data["batch"] = {
        "file_types": defaultdict(int),
        "pinned_msg_ids": [],
        "files_meta": [],
        "note": "",
        "target_channel_id": target_channel_id,
        "src_messages": [],
    }
    # R41 i18n: 批次上传模式提示走 locale 翻译
    await safe_reply_text(update.message, UserMessage.from_raw_text("📦 " + _t(user.id, "bot.batch_upload_started")))


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        del context.user_data["batch"]
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s19')))
    else:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s20')))


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置批次上传备注"""
    if not await check_force_join(update, context):
        return
    batch = context.user_data.get("batch")
    if batch is None:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s21')))
        return
    note_text = " ".join(context.args) if context.args else ""
    if not note_text:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s22')))
        return
    batch["note"] = note_text
    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s8', note_text=note_text)))


@require_maintenance_check(action=_i18n_t('bot.up.s2'))
async def new_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始合集打包模式:用户后续发送的文件码将被收集进合集。"""
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    if not await check_upload_permission(user.id):
        # R41 i18n: 上传禁用文本走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("🚫 " + _t(user.id, "bot.upload_banned")))
        return
    if "batch" in context.user_data:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s23')))
        return
    context.user_data["_collecting_collection"] = {
        "codes": [],
        "note": "",
    }
    # R41 i18n: 合集打包模式提示走 locale 翻译
    await safe_reply_text(update.message, UserMessage.from_raw_text("📦 " + _t(user.id, "bot.collection_packing_started")))


async def end_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """结束合集打包,生成合集码并写入 file_records。"""
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    coll = context.user_data.pop("_collecting_collection", None)
    if coll is None:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s24')))
        return

    codes = coll.get("codes", [])
    if not codes:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s25')))
        return

    # 去重并保留顺序
    seen = set()
    unique_codes = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)

    # 生成合集码
    from services.code_generator import build_collection_code
    collection_code = build_collection_code()

    # 写入 file_records(is_collection=1, collection_codes=JSON, 无主频道)
    collection_codes_json = _json_dumps(unique_codes)

    from database import get_file_records_col, make_file_record, get_codes_col, make_code_entry
    try:
        files_col = get_file_records_col()
        record = make_file_record(
            file_code=collection_code,
            uploader_id=user.id,
            primary_channel_id=0,
            primary_channel_msg_id=0,
            file_types={},
            batch_msg_ids="",
            batch_file_meta="",
            note=coll.get("note", ""),
            protect_content=False,
            file_ttl_days=settings.DEFAULT_FILE_TTL_DAYS,
            is_collection=1,
            collection_codes=collection_codes_json,
        )
        await files_col.insert_one(record)
        # 同步写入 SQLite 本地缓存(0 CRDB RU 后续读取)
        try:
            from database.cache_store import get_cache_store
            # R75 P0-07: mark_dirty=True (scanner 要求;CRDB 已写入,dirty_outbox 为冗余兜底)
            await get_cache_store().upsert_file_record_local(record, mark_dirty=True)
        except Exception as cache_err:
            logger.warning(f"[Up][collection] upsert_file_record_local 失败 code={collection_code}: {cache_err}", exc_info=True)
            try:
                from database.cache_store import get_cache_store
                await get_cache_store().upsert_file_record_local(record, mark_dirty=True)
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:end_collection'))
    except Exception as e:
        logger.error(f"[Up][collection] file_records 写入失败 (code={collection_code}): {e}")
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s1')))
        return

    # 写入 codes 表(支持后续 offline 标记与过期检查)
    try:
        actual_ttl_days = settings.DEFAULT_FILE_TTL_DAYS if settings.DEFAULT_FILE_TTL_DAYS else 0
        if actual_ttl_days == 0:
            expire_dt = datetime.datetime(2099, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)
        else:
            expire_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=actual_ttl_days)
        codes_col = get_codes_col()
        ce = make_code_entry(
            code=collection_code,
            uploader_id=user.id,
            file_types={},
            batch_msg_ids="",
            batch_file_meta="",
            primary_channel_id=0,
            note=coll.get("note", ""),
            expire_time=expire_dt.isoformat(),
        )
        await codes_col.insert_one(ce)
        try:
            from database.cache_store import get_cache_store
            # R75 P0-07: mark_dirty=True (scanner 要求;CRDB 已写入,dirty_outbox 为冗余兜底)
            await get_cache_store().upsert_code_local(ce, mark_dirty=True)
        except Exception as cache_err:
            logger.warning(f"[Up][collection] upsert_code_local 失败 code={collection_code}: {cache_err}", exc_info=True)
            try:
                from database.cache_store import get_cache_store
                await get_cache_store().upsert_code_local(ce, mark_dirty=True)
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:end_collection'))
        from database.cache import get_code_cache
        get_code_cache().set(f"code:{collection_code}", ce)
        from database.cache import clear_negative_file
        clear_negative_file(collection_code)
    except Exception as e:
        logger.error(f"[Up][collection] codes 表写入失败(code={collection_code}): {e}")

    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s9', collection_code=collection_code, len_unique_codes=len(unique_codes))))


async def cancel_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消合集打包。"""
    if not await check_force_join(update, context):
        return
    if "_collecting_collection" in context.user_data:
        del context.user_data["_collecting_collection"]
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s26')))
    else:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s27')))


async def note_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """为合集添加备注。"""
    if not await check_force_join(update, context):
        return
    coll = context.user_data.get("_collecting_collection")
    if coll is None:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s28')))
        return
    note_text = " ".join(context.args) if context.args else ""
    if not note_text:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s29')))
        return
    coll["note"] = note_text
    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s10', note_text=note_text)))


async def end_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    batch = context.user_data.pop("batch", None)
    if batch is None:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s30')))
        return

    # PRE-13: 仅 flush 当前用户的 media group，避免清掉其他用户正在进行中的批次
    async with _get_mg_lock():
        pending_mgids = [
            mgid for mgid, grp in _pending_media_groups.items()
            if grp.get("user_id") == user.id
        ]
        for mgid in pending_mgids:
            grp = _pending_media_groups.get(mgid)
            if grp and grp.get("timer"):
                grp["timer"].cancel()
    for mgid in pending_mgids:
        await _flush_batch_media_group(mgid, context, batch)

    src_messages = batch.get("src_messages", [])
    if not src_messages:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s31')))
        return

    target_ch = batch.get("target_channel_id") or await _get_upload_target_channel()
    channel_msg_ids = []
    files_meta = []

    # R35 P0-4: 批次上传创建 upload_session
    batch_src_ids = [s["msg_id"] for s in src_messages]
    upload_id = await _create_upload_session_for_upload(
        user.id,
        source_msg_ids=batch_src_ids,
        options={"file_types": dict(batch.get("file_types", {})), "batch": True},
    )

    # R36 B0-1: 预先为每个 src_messages 的 file_meta 注入结构化副本信息
    # group_id 从 target_ch 解析;file_unique_id/media_group_id 已存在 src_messages 中
    # 在原 dict 上原地修改,后续 files_meta.append(s["file_meta"]) 自动携带新字段
    for _s in src_messages:
        _enriched = await _enrich_file_meta_for_replica(
            _s["file_meta"], target_ch,
            file_unique_id=_s.get("file_unique_id", ""),
            media_group_id=_s.get("media_group_id") or "",
        )
        _s["file_meta"] = _enriched

    # 注:批次路径不做秒传去重。批次内多文件可能来自不同源,channel 绑定复杂,
    # 跨 channel 复用会破坏 primary_channel_id + batch_msg_ids 的一致性。
    # 单文件与媒体组路径已覆盖主要秒传场景(_process_upload / _flush_media_group)。
    # 按 media_group_id 分组:有 mgid 的用 copy_messages 保持相册,无 mgid 的用 send_media_group 整合
    mg_groups: dict[str, list] = {}
    singles: list = []
    for src in src_messages:
        mgid = src.get("media_group_id")
        if mgid:
            mg_groups.setdefault(mgid, []).append(src)
        else:
            singles.append(src)

    # 1. 复制源媒体组(保持相册)
    for mgid, group in mg_groups.items():
        src_chat = group[0]["chat_id"]
        src_msg_ids = [s["msg_id"] for s in group]
        try:
            copied = await safe_copy_messages(context.bot, target_ch, src_chat, src_msg_ids)
            if copied:
                for s, c in zip(group, copied):
                    channel_msg_ids.append(c.message_id)
                    files_meta.append(s["file_meta"])
                    # R36 B0-2: Manifest/R100 注册改由 OutboxWorker 消费 upload_outbox 表完成,
                    # 不再 fire-and-forget create_task(避免进程崩溃后 Manifest 丢失)
        except Exception as e:
            logger.warning(f"[Up] 媒体组 copy_messages 失败,回退逐条: {e}")
            for s in group:
                try:
                    forwarded = await safe_copy_message(context.bot, target_ch, s["chat_id"], s["msg_id"])
                    channel_msg_ids.append(forwarded.message_id)
                    files_meta.append(s["file_meta"])
                except Exception as e2:
                    logger.error(f"[Up] 媒体组逐条复制失败: {e2}")

    # 2. 整合单个文件为媒体组(按类型分组,用 send_media_group + InputMedia*)
    if singles:
        type_groups: dict[str, list] = {}
        for s in singles:
            type_groups.setdefault(s["file_type"], []).append(s)

        for ftype, group in type_groups.items():
            media_list = []
            valid_singles = []
            unsupported = []
            for s in group:
                im = _build_input_media(s["file_meta"])
                if im is not None:
                    media_list.append(im)
                    valid_singles.append(s)
                else:
                    unsupported.append(s)  # animation/sticker/voice 不支持媒体组

            # 支持媒体组且数量>=2:用 send_media_group 整合(每 10 个一组)
            if len(media_list) >= 2:
                for i in range(0, len(media_list), 10):
                    chunk_media = media_list[i:i + 10]
                    chunk_singles = valid_singles[i:i + 10]
                    try:
                        sent = await safe_send_media_group(context.bot, target_ch, chunk_media)
                        if sent:
                            for s, m in zip(chunk_singles, sent):
                                channel_msg_ids.append(m.message_id)
                                files_meta.append(s["file_meta"])
                    except Exception as e:
                        logger.warning(f"[Up] send_media_group 失败,回退 copy_messages: {e}")
                        src_chat = chunk_singles[0]["chat_id"]
                        src_msg_ids = [s["msg_id"] for s in chunk_singles]
                        try:
                            copied = await safe_copy_messages(context.bot, target_ch, src_chat, src_msg_ids)
                            if copied:
                                for s, c in zip(chunk_singles, copied):
                                    channel_msg_ids.append(c.message_id)
                                    files_meta.append(s["file_meta"])
                        except Exception as e2:
                            logger.error(f"[Up] copy_messages fallback 也失败: {e2}")
            elif len(media_list) == 1:
                # 只有 1 个支持的文件,用 copy_message 单独复制
                unsupported.extend(valid_singles)

            # 不支持媒体组的类型 + 单个文件:用 copy_message 单独复制
            for s in unsupported:
                try:
                    forwarded = await safe_copy_message(context.bot, target_ch, s["chat_id"], s["msg_id"])
                    channel_msg_ids.append(forwarded.message_id)
                    files_meta.append(s["file_meta"])
                except Exception as e:
                    logger.error(f"[Up] 单文件复制失败: {e}")

    if not channel_msg_ids:
        # R41 i18n: 文件处理失败提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user.id, "bot.file_processing_failed")))
        await metrics.record_error("up_bot")
        # R35 P0-4: 批次全部文件复制失败,推进 FAILED_RETRYABLE
        await _transition_upload_session_safe(
            upload_id, "FAILED_RETRYABLE", reason="batch_all_copy_failed",
            last_error="batch all copies failed",
        )
        return

    type_str = _json_dumps(dict(batch["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in channel_msg_ids)
    batch_file_meta_str = _json_dumps(files_meta)

    # R35 P0-4: 批次 copy 完成,推进 RECEIVED → COPIED_PRIMARY
    await _transition_upload_session_safe(
        upload_id, "COPIED_PRIMARY", reason="batch_copy_done",
        primary_channel_id=target_ch,
        primary_msg_ids=channel_msg_ids,
    )

    # 暂存批次数据，等待用户选择有效期→备注→转发权限
    context.user_data["_pending_batch"] = {
        "user_id": user.id,
        "file_types": type_str,
        "batch_msg_ids": batch_ids_str,
        "batch_file_meta": batch_file_meta_str,
        "note": batch.get("note", ""),
        "primary_channel_id": target_ch,
        "primary_channel_msg_id": channel_msg_ids[0],
        "total_count": len(channel_msg_ids),
        "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
    }

    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s11', len_channel_msg_ids=len(channel_msg_ids))), reply_markup=_build_ttl_keyboard())


@require_maintenance_check(action=_i18n_t('bot.up.s3'))
async def _dispatch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 外部中继文件优先路由:caption 以 EXTERNAL_RELAY: 开头,或 media_group_id 已登记为外部组
    caption = update.message.caption or ""
    mgid = update.message.media_group_id or ""
    if caption.startswith("EXTERNAL_RELAY:"):
        if mgid:
            # 从 caption 第一行提取 external_code（忽略换行后的原始 caption），登记 media_group_id 映射
            rest = caption[len("EXTERNAL_RELAY:"):]
            first_line = rest.split("\n", 1)[0]
            user_end = first_line.find(":")
            if user_end != -1:
                _external_mgid_map[mgid] = (_decode_external_code(first_line[user_end + 1:]), time.time())
        await _handle_external_relay_file(update, context)
        return
    if mgid and mgid in _external_mgid_map:
        # 媒体组后续消息(无 caption,但 media_group_id 匹配外部中继组)
        _ext_code, _ = _external_mgid_map[mgid]
        await _handle_external_relay_file(update, context, ext_code_override=_ext_code)
        return

    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        await _collect_batch_file(update, context)
        return

    if update.message.media_group_id:
        await handle_media_group(update, context)
    else:
        await handle_file(update, context)


async def _collect_batch_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch = context.user_data["batch"]
    user = update.effective_user
    file_type = detect_file_type(update)

    if update.message.media_group_id:
        mgid = update.message.media_group_id
        async with _get_mg_lock():
            if mgid not in _pending_media_groups:
                _pending_media_groups[mgid] = {
                    "user_id": user.id,  # PRE-13: 标记所属用户，end_upload 仅 flush 本人的
                    "file_types": defaultdict(int),
                    "updates": [],
                    "timer": None,
                    "created_at": time.time(),  # PRE-12: 供 _cleanup_pending 超时清理判断
                }
            grp = _pending_media_groups[mgid]
            grp["file_types"][file_type] += 1
            grp["updates"].append(update)
            if grp["timer"]:
                grp["timer"].cancel()
            grp["timer"] = asyncio.get_running_loop().call_later(
                1.5, lambda: asyncio.ensure_future(
                    _flush_batch_media_group(mgid, context, batch)
                )
            )
    else:
        batch["file_types"][file_type] += 1
        file_meta = extract_file_meta(update)
        batch["files_meta"].append(file_meta)
        # 缓存源消息,不立即复制(end_upload 时批量复制以保持媒体组关系)
        batch["src_messages"].append({
            "chat_id": update.effective_chat.id,
            "msg_id": update.message.message_id,
            "media_group_id": None,
            "file_type": file_type,
            "file_meta": file_meta,
            "file_unique_id": _extract_file_unique_id(update.message),
        })
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s32', file_type=file_type)))


async def _flush_batch_media_group(mgid: str, context: ContextTypes.DEFAULT_TYPE, batch: dict):
    """聚合批次中的媒体组消息到缓存,不立即复制(end_upload 时批量复制保持相册)。"""
    async with _get_mg_lock():
        grp = _pending_media_groups.pop(mgid, None)
    if grp is None or not grp.get("updates"):
        return
    file_types = grp["file_types"]
    for k, v in file_types.items():
        batch["file_types"][k] += v
    for up in grp["updates"]:
        batch["src_messages"].append({
            "chat_id": up.effective_chat.id,
            "msg_id": up.message.message_id,
            "media_group_id": mgid,
            "file_type": detect_file_type(up),
            "file_meta": extract_file_meta(up),
            "file_unique_id": _extract_file_unique_id(up.message),
        })
    first = grp["updates"][0]
    type_desc = " ".join(_i18n_t('bot.up.s12', v=v, k=k) for k, v in sorted(file_types.items()))
    await safe_send_message(
        context.bot,
        chat_id=first.effective_chat.id,
        payload=UserMessage.from_raw_text(
            _i18n_t('bot.up.s33', type_desc=type_desc, len_grp_updates=len(grp['updates']))
        ),
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    caption = update.message.caption or ""

    # ── 中继外部文件:中继账号转发的文走缓冲区 批量写入 pending_uploads ──
    if caption.startswith("EXTERNAL_RELAY:"):
        await _handle_external_relay_file(update, context)
        return

    if not await global_rate_limiter.acquire():
        # R41 i18n: 限速提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user.id, "bot.system_busy")))
        return
    if not await user_rate_limiter.acquire(user.id):
        # R41 i18n: 用户限速提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user.id, "bot.rate_limited")))
        return
    if not await check_upload_permission(user.id):
        # R41 i18n: 上传权限提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("🚫 " + _t(user.id, "bot.no_upload_permission")))
        return

    file_type = detect_file_type(update)
    file_types = {file_type: 1}
    await _process_upload(user.id, update, context, file_types)


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # P2: 媒体组上传同样需要限速,防止数十文件洪泛
    if not await global_rate_limiter.acquire():
        # R41 i18n: 限速提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user.id, "bot.system_busy")))
        return
    if not await user_rate_limiter.acquire(user.id):
        # R41 i18n: 用户限速提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user.id, "bot.rate_limited")))
        return
    if not await check_upload_permission(user.id):
        # R41 i18n: 上传权限提示走 locale 翻译
        await safe_reply_text(update.message, UserMessage.from_raw_text("🚫 " + _t(user.id, "bot.no_upload_permission")))
        return

    file_type = detect_file_type(update)

    async with _get_mg_lock():
        if update.message.media_group_id not in _pending_media_groups:
            _pending_media_groups[update.message.media_group_id] = {
                "user_id": user.id,
                "file_types": defaultdict(int),
                "updates": [],
                "timer": None,
                "created_at": time.time(),  # PRE-12: 供 _cleanup_pending 超时清理判断
            }

        group = _pending_media_groups[update.message.media_group_id]
        group["file_types"][file_type] += 1
        group["updates"].append(update)

        if group["timer"]:
            group["timer"].cancel()

        group["timer"] = asyncio.get_running_loop().call_later(
            1.5, lambda: asyncio.ensure_future(_flush_media_group(update.message.media_group_id, context))
        )


async def _flush_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    async with _get_mg_lock():
        group = _pending_media_groups.pop(media_group_id, None)
    if group is None:
        return

    user_id = group["user_id"]
    file_types = dict(group["file_types"])

    target_ch = await _get_upload_target_channel()
    total_count = len(group["updates"])
    all_mids = []
    all_meta = []
    failed_count = 0

    # 用 copy_messages 一次性批量复制整个媒体组,保留 media_group_id 关系,
    # 这样存储频道里的消息仍是同一媒体组,dsp 用 copyMessages 复制时才能以相册展示。
    # 失败则回退逐条 copy_message(保证可用性优先)。
    updates = group["updates"]
    src_chat_id = updates[0].effective_chat.id
    src_msg_ids = [up.message.message_id for up in updates]

    # R35 P0-4: 收到媒体组后创建 upload_session
    upload_id = await _create_upload_session_for_upload(
        user_id,
        source_msg_ids=src_msg_ids,
        options={"file_types": dict(file_types), "media_group": True},
    )

    progress_msg = await safe_send_message(
        context.bot,
        chat_id=user_id,
        payload=UserMessage.from_raw_text(
            _i18n_t('bot.up.s34', total_count=total_count, total_count_2=total_count)
        ),
    )

    try:
        await progress_msg.edit_text(_i18n_t('bot.up.s35', total_count=total_count, total_count_2=total_count))
    except Exception:
        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))

    # B1 媒体组级秒传去重:整组所有文件都已存在且在同一 channel 才复用
    # (保证 batch_msg_ids 与 primary_channel_id 的一致性)
    dedup_hit = False
    await _ensure_channel_group_map()
    group_id = _channel_to_group.get(target_ch)
    if group_id is not None:
        hit_channel = 0
        hit_mids: list[int] = []
        hit_meta: list = []
        all_found = True
        for up in updates:
            fuid = _extract_file_unique_id(up.message)
            if not fuid:
                all_found = False
                break
            try:
                existing = await get_cache_store().get_existing_file_in_group(group_id, fuid)
            except Exception:
                existing = None
            if not existing:
                all_found = False
                break
            if hit_channel == 0:
                hit_channel = existing["channel_id"]
            elif existing["channel_id"] != hit_channel:
                all_found = False  # 跨 channel,放弃秒传(保持 batch_msg_ids 一致性)
                break
            hit_mids.append(existing["message_id"])
            hit_meta.append(extract_file_meta(up))
        if all_found and hit_mids:
            dedup_hit = True
            target_ch = hit_channel  # 更新为文件实际所在频道
            all_mids = hit_mids
            all_meta = hit_meta
            await metrics.increment("up.dedup_hit", len(hit_mids))
            logger.info(f"[Up] 媒体组秒传去重命中: {len(hit_mids)} 个文件, channel={hit_channel}")
            try:
                await progress_msg.edit_text(f"正在处理 {total_count} 个文件...\n已完成 {total_count}/{total_count}")
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))
            # R35 P0-4: 去重命中,推进 RECEIVED → COPIED_PRIMARY
            await _transition_upload_session_safe(
                upload_id, "COPIED_PRIMARY", reason="dedup_hit",
                primary_channel_id=target_ch,
                primary_msg_ids=all_mids,
                media_group_id=media_group_id,
            )

    if not dedup_hit:
        batch_success = False
        # R35 P0-4 §24: 复制前为每个文件创建 replication_task(PLANNED)
        _mg_task_ids: list[int] = []
        for up in updates:
            _mg_fuid = _extract_file_unique_id(up.message)
            _tid = await _create_replication_task_safe(
                group_id or 0, _mg_fuid, src_chat_id, target_ch,
                up.message.message_id, media_group_id=media_group_id,
            )
            _mg_task_ids.append(_tid)
        for _tid in _mg_task_ids:
            await _mark_replication_copying_safe(_tid)
        try:
            copied = await safe_copy_messages(context.bot, target_ch, src_chat_id, src_msg_ids)
            if copied and len(copied) == total_count:
                # copy_messages 返回 MessageId 列表,顺序与输入一致
                all_mids = [m.message_id for m in copied]
                all_meta = [extract_file_meta(up) for up in updates]
                # R36 B0-2: Manifest/R100 注册改由 OutboxWorker 消费 upload_outbox 表完成
                # (_finalize_upload 中创建 REGISTER_MANIFEST / ARCHIVE_R100 outbox 条目)
                batch_success = True
                # R35 P0-4 §24: 批量 copy 成功,标记每个任务 COPIED_UNVERIFIED → COMMITTED
                for idx, _tid in enumerate(_mg_task_ids):
                    if idx < len(all_mids):
                        await _mark_replication_copied_safe(_tid, all_mids[idx])
                        await _mark_replication_committed_safe(_tid)
                try:
                    await progress_msg.edit_text(_i18n_t('bot.up.s61', total_count=total_count, total_count_2=total_count, total_count_4=total_count))
                except Exception:
                    logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))
                # R35 P0-4: 批量 copy 成功,推进 RECEIVED → COPIED_PRIMARY
                await _transition_upload_session_safe(
                    upload_id, "COPIED_PRIMARY", reason="copy_batch_done",
                    primary_channel_id=target_ch,
                    primary_msg_ids=all_mids,
                    media_group_id=media_group_id,
                )
        except Exception as e:
            logger.warning(f"[Up] copy_messages 批量复制失败,回退逐条: {e}")
            # R35 P0-4 §24: 批量失败,标记所有任务 FAILED
            for _tid in _mg_task_ids:
                await _mark_replication_failed_safe(_tid, f"mg_batch_copy_failed: {e}")

        if not batch_success:
            # 回退逐条 copy_message
            for i, up in enumerate(updates):
                try:
                    forwarded = await safe_copy_message(context.bot, target_ch, up.effective_chat.id, up.message.message_id)
                    all_mids.append(forwarded.message_id)
                    all_meta.append(extract_file_meta(up))
                    # R36 B0-2: Manifest/R100 注册改由 OutboxWorker 消费 upload_outbox 表完成
                    # R35 P0-4 §24: 逐条回退成功,标记 COPIED_UNVERIFIED → COMMITTED
                    if i < len(_mg_task_ids):
                        await _mark_replication_copied_safe(_mg_task_ids[i], forwarded.message_id)
                        await _mark_replication_committed_safe(_mg_task_ids[i])
                except Exception as e:
                    logger.error(f"[Up] media group copy failed: {e}")
                    failed_count += 1
                    # R35 P0-4 §24: 逐条也失败,标记 FAILED
                    if i < len(_mg_task_ids):
                        await _mark_replication_failed_safe(_mg_task_ids[i], f"mg_single_copy_failed: {e}")
                if (i + 1) % 3 == 0 or i == total_count - 1:
                    try:
                        await progress_msg.edit_text(_i18n_t('bot.up.s63', total_count=total_count, i_1=i + 1, total_count_3=total_count))
                    except Exception:
                        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))
            # R35 P0-4: 逐条回退完成(部分成功),推进 COPIED_PRIMARY
            if all_mids:
                await _transition_upload_session_safe(
                    upload_id, "COPIED_PRIMARY", reason="copy_fallback_partial",
                    primary_channel_id=target_ch,
                    primary_msg_ids=all_mids,
                    media_group_id=media_group_id,
                )

    if not all_mids:
        await metrics.record_error("up_bot")
        # R35 P0-4: 全部文件复制失败,推进 FAILED_RETRYABLE
        await _transition_upload_session_safe(
            upload_id, "FAILED_RETRYABLE", reason="all_copy_failed",
            last_error="media_group all copies failed",
        )
        try:
            # R41 i18n: 文件处理失败提示走 locale 翻译
            await progress_msg.edit_text("⚠️ " + _t(user_id, "bot.file_processing_failed"))
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))
        return

    note = group["updates"][0].message.caption or ""

    # 编辑进度消息为完成状态（最终确认消息由 _finalize_upload 发出）
    try:
        failed_hint = _i18n_t('bot.up.s13', failed_count=failed_count) if failed_count > 0 else ""
        await progress_msg.edit_text(_i18n_t('bot.up.s36', failed_hint=failed_hint))
    except Exception:
        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_flush_media_group'))

    # 暂存媒体组数据，等待用户选择有效期→备注→转发权限
    # R36 B0-1: 为每个 file_meta 注入结构化副本信息(group_id/file_unique_id/media_group_id)
    _enriched_all_meta = []
    for _idx, _up in enumerate(updates):
        _mg_fuid = _extract_file_unique_id(_up.message)
        _enriched_all_meta.append(await _enrich_file_meta_for_replica(
            all_meta[_idx] if _idx < len(all_meta) else extract_file_meta(_up),
            target_ch,
            file_unique_id=_mg_fuid,
            media_group_id=media_group_id or "",
        ))
    context.user_data["_pending_media_group"] = {
        "user_id": user_id,
        "primary_channel_id": target_ch,
        "primary_channel_msg_id": all_mids[0],
        "file_types": _json_dumps(file_types),
        "batch_msg_ids": ",".join(str(mid) for mid in all_mids),
        "batch_file_meta": _json_dumps(_enriched_all_meta),
        "note": note,
        "total_count": total_count,
        "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
    }

    # 发送上传选项
    try:
        await safe_send_message(context.bot, payload=UserMessage.from_raw_text(_i18n_t('bot.up.s50')), chat_id=user_id, reply_markup=_build_ttl_keyboard())
    except Exception as e:
        logger.warning(f"[Up] 发送TTL选择键盘失败: {e}")
        pass


async def _process_upload(
    user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, file_types: dict
):
    main_channel = await _get_upload_target_channel()

    # R35 P0-4: 收到文件后立即创建 upload_session(权威持久化层)
    # upload_id 关联到 _pending_upload_meta,供后续 _finalize_upload 查找
    upload_id = await _create_upload_session_for_upload(
        user_id,
        source_msg_ids=[update.message.message_id],
        options={"file_types": file_types},
    )
    # R35 P0-4: 立即写入 user_data,确保 dedup/copy 失败等提前 return 的路径也能追踪 upload_id
    context.user_data["_upload_id"] = upload_id

    # B1 秒传去重:若该文件(file_unique_id)已存在于同组频道,直接复用 msg_id
    existing = await _check_dedup(main_channel, update.message)
    if existing:
        channel_msg_id = existing["message_id"]
        main_channel = existing["channel_id"]  # 文件实际所在频道
        logger.info(f"[Up] 秒传去重命中: reuse channel={main_channel} msg_id={channel_msg_id}")
        await metrics.increment("up.dedup_hit")
        # R35 P0-4: 去重命中也视为 primary copy 完成
        await _transition_upload_session_safe(
            upload_id, "COPIED_PRIMARY", reason="dedup_hit",
            primary_channel_id=main_channel,
            primary_msg_ids=[channel_msg_id],
        )
    else:
        # R35 P0-4 §24: 复制前创建 replication_task(PLANNED)
        _fuid = _extract_file_unique_id(update.message)
        await _ensure_channel_group_map()
        _group_id = _channel_to_group.get(main_channel, 0)
        _repl_task_id = await _create_replication_task_safe(
            _group_id, _fuid, update.effective_chat.id,
            main_channel, update.message.message_id,
        )
        await _mark_replication_copying_safe(_repl_task_id)
        # R80 P0-02: contract 模式无 copyMessage 端点,改用
        # getFile + download + sendDocument 产生完整 receipt 链。
        # R80 Step 11: 可重试异常(RetryAfter/NetworkError)有界重试;
        # 不可重试异常(BadRequest/TimedOut)直接传播,让 web_adapter 标记 "failed"。
        _backend = getattr(settings, "PROVIDER_BACKEND", "telegram")
        if _backend == _PROVIDER_BACKEND_CONTRACT:
            # R81 §10.9: 通过 sink adapter 导入 telegram 异常类型,
            # 避免直接 import telegram.error(sink import boundary 违规)。
            from services.sink_adapters.telegram_adapter import (
                BadRequest as _BadRequest,
                NetworkError as _NetworkError,
                RetryAfter as _RetryAfter,
                TimedOut as _TimedOut,
            )
            _CONTRACT_MAX_ATTEMPTS = 3
            _file_id = update.message.document.file_id
            for _attempt in range(1, _CONTRACT_MAX_ATTEMPTS + 1):
                try:
                    _provider_file = await context.bot.get_file(_file_id)
                    await context.bot.download_file(_provider_file.file_path)
                    _sent = await context.bot.send_document(
                        chat_id=main_channel, document=_file_id,
                    )
                    channel_msg_id = _sent.message_id
                    break  # 成功,退出重试循环
                except _RetryAfter as _ra:
                    # 429 + Retry-After: 按 provider 指定的秒数退避后重试
                    if _attempt >= _CONTRACT_MAX_ATTEMPTS:
                        raise
                    logger.warning(
                        f"[Up] contract 429 限流, {_ra.retry_after}s 后重试 "
                        f"(attempt {_attempt}/{_CONTRACT_MAX_ATTEMPTS})"
                    )
                    await asyncio.sleep(_ra.retry_after)
                except (_BadRequest, _TimedOut):
                    # PTB 21.6: BadRequest/TimedOut 均为 NetworkError 子类,
                    # 必须在 except _NetworkError 之前显式捕获并传播。
                    # 401/403/timeout 不可重试 → 直接传播 → web_adapter 标记 "failed"
                    raise
                except _NetworkError:
                    # 5xx: 指数退避有界重试(仅捕获纯 NetworkError,
                    # BadRequest/TimedOut 已被上方 handler 拦截)
                    if _attempt >= _CONTRACT_MAX_ATTEMPTS:
                        raise
                    _backoff = min(2 ** (_attempt - 1), 4)
                    logger.warning(
                        f"[Up] contract 5xx/网络错误, {_backoff}s 后重试 "
                        f"(attempt {_attempt}/{_CONTRACT_MAX_ATTEMPTS})"
                    )
                    await asyncio.sleep(_backoff)
            await _mark_replication_copied_safe(_repl_task_id, channel_msg_id)
            await _mark_replication_committed_safe(_repl_task_id)
        else:
            try:
                forwarded = await safe_copy_message(context.bot, main_channel, update.effective_chat.id, update.message.message_id)
                channel_msg_id = forwarded.message_id
                # R35 P0-4 §24: copy 返回 dst_msg_id 后先写任务(COPIED_UNVERIFIED)
                await _mark_replication_copied_safe(_repl_task_id, channel_msg_id)
                # R36 B0-2: Manifest/R100 注册改由 OutboxWorker 消费 upload_outbox 表完成
                # (_finalize_upload 中创建 REGISTER_MANIFEST / ARCHIVE_R100 outbox 条目)
                # R35 P0-4 §24: replication_task 标记 COMMITTED(不再依赖 manifest 写入成功)
                await _mark_replication_committed_safe(_repl_task_id)
            except Exception as e:
                logger.error(f"[Up] 转发文件到存储频道失败 {e}")
                await metrics.record_error("up_bot")
                # R35 P0-4 §24: 复制失败,标记 replication_task FAILED
                await _mark_replication_failed_safe(_repl_task_id, f"copy_failed: {e}")
                # R35 P0-4: Telegram copy 失败,推进到 FAILED_RETRYABLE
                await _transition_upload_session_safe(
                    upload_id, "FAILED_RETRYABLE", reason=f"copy_failed: {e}",
                    last_error=str(e),
                )
                await safe_reply_text(update.message, UserMessage.from_raw_text("⚠️ " + _t(user_id, "bot.file_processing_failed")))
                return

        # R36 B0-2: R100 归档改由 OutboxWorker 消费 upload_outbox 表完成(不再 fire-and-forget)
        # R35 P0-4: Telegram copy 成功,推进 RECEIVED → COPIED_PRIMARY
        await _transition_upload_session_safe(
            upload_id, "COPIED_PRIMARY", reason="copy_done",
            primary_channel_id=main_channel,
            primary_msg_ids=[channel_msg_id],
        )

    # 暂存必要信息到模块级 dict（context.user_data 在 callback 间不可靠）
    # P2: 改用 user_id:msg_id 复合键,避免同用户并发上传互相覆盖
    _meta_key = f"{user_id}:{update.message.message_id}"
    # R36 B0-1: 注入结构化副本信息(group_id/file_unique_id/media_group_id),
    # 使下游 idx_bot → enqueue_job → dsp_bot → ReplicaAwareResolver 无需 JSON 解析
    _enriched_meta = await _enrich_file_meta_for_replica(
        extract_file_meta(update),
        main_channel,
        file_unique_id=_extract_file_unique_id(update.message),
        media_group_id=update.message.media_group_id or "",
    )
    _pending_upload_meta[_meta_key] = {
        "main_channel": main_channel,
        "channel_msg_id": channel_msg_id,
        "file_types": file_types,
        "note": update.message.caption or "",
        "file_meta": _enriched_meta,
        "created_at": time.time(),
        "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
    }
    # 同时存 context.user_data（兼容旧逻辑）
    context.user_data["_main_channel"] = main_channel
    context.user_data["_channel_msg_id"] = channel_msg_id
    context.user_data["_file_types"] = file_types
    context.user_data["_note"] = update.message.caption or ""
    context.user_data["_file_meta"] = _enriched_meta
    # P2: 记录上传消息 ID 用于 _finalize_upload 复合键查找
    context.user_data["_upload_msg_id"] = update.message.message_id
    # R35 P0-4: 记录 upload_id 供 _finalize_upload 使用
    context.user_data["_upload_id"] = upload_id

    # 第一步：发送有效期选择
    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s14')), reply_markup=_build_ttl_keyboard())


# ─── 上传选项回调 ───

async def upload_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理上传选项按钮回调。三步流: 有效期 → 备注 → 转发权限"""
    query = update.callback_query
    await query.answer()
    data = query.data  # format: "opt|key|value"
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "opt":
        return

    user_id = query.from_user.id
    key = parts[1]
    value = parts[2]

    if key == "ttl":
        context.user_data["file_ttl"] = value
        # 批次已通过 /note 设置备注的，跳过备注步骤
        if ("_pending_batch" in context.user_data
                and context.user_data["_pending_batch"].get("note")):
            await safe_edit_message_text(query, UserMessage.from_raw_text(_i18n_t('bot.up.s58')), reply_markup=_build_protect_keyboard())
        else:
            await safe_edit_message_text(query, UserMessage.from_raw_text(_i18n_t('bot.up.s59')), reply_markup=_build_note_keyboard())

    elif key == "note":
        if value == "skip":
            await safe_edit_message_text(query, UserMessage.from_raw_text(_i18n_t('bot.up.s62')), reply_markup=_build_protect_keyboard())
        elif value == "add":
            context.user_data["awaiting_note_since"] = time.time()
            context.user_data["_note_query_msg_id"] = query.message.message_id
            context.user_data["_note_query_chat_id"] = query.message.chat_id
            await safe_edit_message_text(query, UserMessage.from_raw_text(_i18n_t('bot.up.s64')), reply_markup=None)

    elif key == "protect":
        context.user_data["protect_content"] = value
        await _finalize_upload(query, context, user_id)


async def _finalize_upload(query, context, user_id: int):
    """用户选完所有选项后，写入 pending_uploads 并通知 idx_bot。
    支持三种场景: 单文件 / 媒体组 / 批次上传
    """
    # PRE-15: 防止 Telegram 重复回调覆盖成功消息
    msg_id = query.message.message_id
    if msg_id in _finalized_msg_ids:
        logger.debug(f"[Up] _finalize_upload 重复回调(消息已处理)，忽略: user={user_id}, msg_id={msg_id}")
        return

    pending_batch = context.user_data.pop("_pending_batch", None)
    pending_mg = context.user_data.pop("_pending_media_group", None)

    # R35 P0-4: 预提取 upload_id(供异常处理中的状态推进使用)
    upload_id = ""
    if pending_batch:
        upload_id = pending_batch.get("upload_id", "")
    elif pending_mg:
        upload_id = pending_mg.get("upload_id", "")

    try:
        pending_col = get_pending_uploads_col()
        ttl = context.user_data.pop("file_ttl", "0")
        protect = context.user_data.pop("protect_content", str(settings.DEFAULT_PROTECT_CONTENT))
        note = context.user_data.pop("_note", "")

        # R40 P0-4: 双写 pending_uploads_local 到 SQLite(Idx Bot 从 SQLite 读取,不再依赖 CRDB 凭证)
        # 失败时仅记录 warning,不影响主流程(CRDB 写入仍为权威源,SQLite 为 Idx Bot 读取源)
        async def _dual_write_pending_local(record: dict):
            try:
                from database.cache_store import get_cache_store
                # R75 P0-07: mark_dirty=True (scanner 要求;Up Bot 直写 CRDB,dirty_outbox 为冗余兜底)
                await get_cache_store().insert_pending_upload_local(record, mark_dirty=True)
                logger.debug(f"[Up] R40 P0-4: pending_uploads_local 双写成功 user={record.get('uploader_id')}")
            except Exception as sqlite_err:
                logger.warning(f"[Up] R40 P0-4: pending_uploads_local 双写失败(不影响主流程): {sqlite_err}")

        protect_bool = protect.lower() == "true"
        # P2: -1=永久有效哨兵,0=使用默认值,正数=指定天数
        try:
            ttl_val = int(ttl)
        except (ValueError, TypeError):
            ttl_val = 0
        if ttl_val == -1:
            ttl_days = 0  # 永久有效
        elif ttl_val == 0:
            ttl_days = settings.DEFAULT_FILE_TTL_DAYS
        else:
            ttl_days = ttl_val

        if pending_batch:
            # ── 批次上传 ──
            note = note or pending_batch.get("note", "")
            upload_id = pending_batch.get("upload_id", "")
            _batch_record = {
                "uploader_id": user_id,
                "primary_channel_id": pending_batch["primary_channel_id"],
                "primary_channel_msg_id": pending_batch["primary_channel_msg_id"],
                "file_types": pending_batch["file_types"],
                "batch_msg_ids": pending_batch["batch_msg_ids"],
                "batch_file_meta": pending_batch["batch_file_meta"],
                "note": note,
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
                "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
            }
            await pending_col.insert_one(_batch_record)
            # R40 P0-4: 同步双写 SQLite local(Idx Bot 读取源)
            await _dual_write_pending_local(_batch_record)
            logger.info(f"[Up] 批次写入pending_uploads: user={user_id}, {pending_batch['total_count']}个文件")
            _finalized_msg_ids.add(msg_id)

        elif pending_mg:
            # ── 媒体组上传 ──
            upload_id = pending_mg.get("upload_id", "")
            _mg_record = {
                "uploader_id": user_id,
                "primary_channel_id": pending_mg["primary_channel_id"],
                "primary_channel_msg_id": pending_mg["primary_channel_msg_id"],
                "file_types": pending_mg["file_types"],
                "batch_msg_ids": pending_mg["batch_msg_ids"],
                "batch_file_meta": pending_mg["batch_file_meta"],
                "note": note or pending_mg.get("note", ""),
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
                "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
            }
            await pending_col.insert_one(_mg_record)
            # R40 P0-4: 同步双写 SQLite local(Idx Bot 读取源)
            await _dual_write_pending_local(_mg_record)
            logger.info(f"[Up] 媒体组写入pending_uploads: user={user_id}")
            _finalized_msg_ids.add(msg_id)

        else:
            # ── 单文件上传 ──
            # 优先从模块级 dict 读取（context.user_data 在 callback 间不可靠）
            # P2: 用 user_id:upload_msg_id 复合键查找,避免并发上传覆盖
            upload_msg_id = context.user_data.pop("_upload_msg_id", 0)
            _meta_key = f"{user_id}:{upload_msg_id}" if upload_msg_id else f"{user_id}:"
            meta = _pending_upload_meta.get(_meta_key) or _pending_upload_meta.get(user_id, {})
            main_channel = context.user_data.pop("_main_channel", 0) or meta.get("main_channel", 0)
            channel_msg_id = context.user_data.pop("_channel_msg_id", 0) or meta.get("channel_msg_id", 0)
            file_types = context.user_data.pop("_file_types", {}) or meta.get("file_types", {})
            file_meta = context.user_data.pop("_file_meta", {}) or meta.get("file_meta", {})
            upload_id = context.user_data.pop("_upload_id", "") or meta.get("upload_id", "")
            logger.debug(f"[Up] _finalize_upload user={user_id} meta_keys={list(meta.keys())} "
                         f"main_channel={main_channel} channel_msg_id={channel_msg_id} file_types={file_types}")
            # file_types 丢失时从 file_meta 推断
            if not file_types and file_meta and isinstance(file_meta, dict) and "type" in file_meta:
                file_types = {file_meta["type"]: 1}
                logger.info(f"[Up] file_types 从 file_meta 推断: {file_types}")

            # PRE-14: 校验存储频道与消息 ID 非零，避免写入无效记录导致 dsp_bot 投递失败
            if not main_channel or not channel_msg_id:
                logger.error(
                    f"[Up] 单文件 _finalize_upload 状态缺失: main_channel={main_channel}, "
                    f"channel_msg_id={channel_msg_id}, user={user_id} — 拒绝写入 pending_uploads"
                )
                await metrics.record_error("up_bot")
                # R35 P0-4: 状态缺失,推进 FAILED_PERMANENT(不可恢复)
                await _transition_upload_session_safe(
                    upload_id, "FAILED_PERMANENT", reason="state_missing",
                    last_error="main_channel or channel_msg_id is zero",
                )
                try:
                    await safe_edit_message_text(query, UserMessage.from_raw_text(_i18n_t('bot.up.s65')))
                except Exception:
                    logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_finalize_upload'))
                return

            await pending_col.insert_one({
                "uploader_id": user_id,
                "primary_channel_id": main_channel,
                "primary_channel_msg_id": channel_msg_id,
                "file_types": _json_dumps(file_types),
                "batch_msg_ids": "",
                "batch_file_meta": _json_dumps([file_meta]),
                "note": note,
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
                "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
            })
            # R40 P0-4: 同步双写 SQLite local(Idx Bot 读取源)
            await _dual_write_pending_local({
                "uploader_id": user_id,
                "primary_channel_id": main_channel,
                "primary_channel_msg_id": channel_msg_id,
                "file_types": _json_dumps(file_types),
                "batch_msg_ids": "",
                "batch_file_meta": _json_dumps([file_meta]),
                "note": note,
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
                "upload_id": upload_id,
            })
            logger.info(f"[Up] 单文件写入pending_uploads: user={user_id}")
            _finalized_msg_ids.add(msg_id)
            # P2: 清理复合键(user_id:upload_msg_id)和旧键(user_id)
            _pending_upload_meta.pop(_meta_key, None)
            _pending_upload_meta.pop(user_id, None)

        # R35 P0-4 / R36 B0-2: pending_uploads 写入成功,推进 COPIED_PRIMARY → MANIFEST_PENDING
        # 并创建 outbox 条目供 OutboxWorker 消费(唯一副作用驱动器)
        await _transition_upload_session_safe(
            upload_id, "MANIFEST_PENDING", reason="pending_uploads_written",
        )
        # 解析 storage_msg_ids 与 file_meta list 供 outbox 使用
        # R36 B0-2: outbox 必须携带每个文件的 file_meta(含 file_unique_id/group_id/media_group_id),
        # 否则 OutboxWorker 无法调用 _register_manifest(需要 file_unique_id 定位 group_id)
        _outbox_storage_ids = []
        _outbox_file_meta = []
        if pending_batch:
            _outbox_storage_ids = [int(x) for x in pending_batch["batch_msg_ids"].split(",") if x.strip().isdigit()]
            _outbox_channel_id = pending_batch["primary_channel_id"]
            # 解析 batch_file_meta JSON 字符串为 list(每个 dict 含 file_unique_id 等结构化字段)
            try:
                _parsed_meta = json.loads(pending_batch["batch_file_meta"]) if pending_batch.get("batch_file_meta") else []
                _outbox_file_meta = _parsed_meta if isinstance(_parsed_meta, list) else []
            except (ValueError, TypeError) as _e:
                logger.warning(f"[Up] 解析 batch file_meta 失败(传空 list): {_e}")
                _outbox_file_meta = []
        elif pending_mg:
            _outbox_storage_ids = [int(x) for x in pending_mg["batch_msg_ids"].split(",") if x.strip().isdigit()]
            _outbox_channel_id = pending_mg["primary_channel_id"]
            try:
                _parsed_meta = json.loads(pending_mg["batch_file_meta"]) if pending_mg.get("batch_file_meta") else []
                _outbox_file_meta = _parsed_meta if isinstance(_parsed_meta, list) else []
            except (ValueError, TypeError) as _e:
                logger.warning(f"[Up] 解析 mg file_meta 失败(传空 list): {_e}")
                _outbox_file_meta = []
        else:
            _outbox_storage_ids = [channel_msg_id] if channel_msg_id else []
            _outbox_channel_id = main_channel
            # 单文件:file_meta 是 dict,包装成 [file_meta]
            _outbox_file_meta = [file_meta] if file_meta else []
        # R37 P1-3: REGISTER_MANIFEST/ARCHIVE_R100 是权威 outbox 事件,
        # 必须用 strict 版本(失败抛 DurabilityError 中断主流程),
        # 否则文件已复制但 manifest 未登记 → 永久数据丢失。
        await _create_outbox_entry_strict(
            f"obx-man-{upload_id}", upload_id, user_id,
            _outbox_channel_id, msg_ids=_outbox_storage_ids,
            file_meta=_outbox_file_meta, event_type="REGISTER_MANIFEST",
            protect=1 if protect_bool else 0,
        )
        await _create_outbox_entry_strict(
            f"obx-r100-{upload_id}", upload_id, user_id,
            _outbox_channel_id, msg_ids=_outbox_storage_ids,
            file_meta=_outbox_file_meta, event_type="ARCHIVE_R100",
            protect=1 if protect_bool else 0,
        )

        try:
            await get_cache_store().notify_new_upload()
        except Exception as e:
            logger.warning(f"[Up] 通知 idx_bot 失败(不影响上传): {e}")

        metrics.upload_count += 1
        await metrics.record_processed("up_bot")

        # R41 P1-12: 上传成功后记录到 TaskCenter(便于用户查看任务进度)
        # 注:此时 file_code 尚未生成(由 idx_bot 后续生成),
        # 这里记录 upload_id + 文件数,作为上传事件的任务追踪
        try:
            _upload_count_meta = 1
            if pending_batch:
                _upload_count_meta = int(pending_batch.get("total_count", 1) or 1)
            elif pending_mg:
                _upload_count_meta = len(
                    [x for x in (pending_mg.get("batch_msg_ids", "") or "").split(",") if x.strip().isdigit()]
                ) or 1
            await task_center.record_task(
                user_id=user_id,
                task_type="upload",
                status="completed",
                metadata={
                    "upload_id": upload_id,
                    "file_count": _upload_count_meta,
                    "channel_id": _outbox_channel_id,
                    "note": "pending_file_code_generation",
                },
            )
        except Exception as task_err:
            logger.warning(f"[Up] R41 P1-12: record_task 失败(不影响上传): {task_err}")

        # 清除按钮，显示确认消息
        # R41 i18n: 文件接收确认走 locale 翻译
        await safe_edit_message_text(query, UserMessage.from_raw_text("📦 " + _t(user_id, "bot.file_received_pending",
                            bot_username=settings.DECODER_BOT_USERNAME)), reply_markup=None)
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        await metrics.record_error("up_bot")
        # R35 P0-4: 写入失败,推进 FAILED_RETRYABLE
        await _transition_upload_session_safe(
            upload_id, "FAILED_RETRYABLE", reason=f"finalize_failed: {e}",
            last_error=str(e),
        )
        try:
            # R41 i18n: 文件处理失败提示走 locale 翻译
            await safe_edit_message_text(query, UserMessage.from_raw_text("⚠️ " + _t(user_id, "bot.file_processing_failed")))
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/up_bot.py:_finalize_upload'))


# ─── 备注文字输入处理 ───

async def _handle_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """当用户处于等待备注输入状态时，捕获文字消息作为备注。"""
    # ── 合集打包模式:捕获用户发送的文件码 ──
    coll = context.user_data.get("_collecting_collection")
    if coll is not None:
        text = update.message.text or ""
        # 按换行/空格分割,过滤出有效文件码
        from services.code_generator import is_valid_code_format
        tokens = []
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) > 1:
                tokens.extend(parts)
            else:
                tokens.append(line)
        new_codes = [t for t in tokens if is_valid_code_format(t)]
        if not new_codes:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s51')))
            return
        coll["codes"].extend(new_codes)
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s37', len_new_codes=len(new_codes), len_coll_codes=len(coll['codes']))))
        return

    user_id = update.effective_user.id
    note_since = context.user_data.get("awaiting_note_since")
    if note_since is None:
        return  # 不在等待备注状态，忽略

    if time.time() - note_since > 60:
        del context.user_data["awaiting_note_since"]
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s38')))
        return

    # 保存备注
    context.user_data["_note"] = update.message.text
    del context.user_data["awaiting_note_since"]

    await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s15', update_message_text=update.message.text)))

    # 弹出转发权限选择
    try:
        await safe_send_message(context.bot, payload=UserMessage.from_raw_text(_i18n_t('bot.up.s52')), chat_id=user_id, reply_markup=_build_protect_keyboard())
    except Exception as e:
        logger.warning(f"[Up] 发送转发权限选择失败: {e}")


async def cancel_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消/跳过备注输入"""
    if context.user_data.get("awaiting_note_since"):
        del context.user_data["awaiting_note_since"]
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s39')))
        try:
            await safe_send_message(context.bot, payload=UserMessage.from_raw_text(_i18n_t('bot.up.s60')), chat_id=update.effective_user.id, reply_markup=_build_protect_keyboard())
        except Exception as e:
            logger.warning(f"[Up] 发送转发权限选择失败: {e}")
    else:
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s2')))


def _build_ttl_keyboard():
    """构建有效期选择按钮。"""
    keyboard = [
        [
            # P2: 用 -1 哨兵区分「永久(0)」与「默认」
            InlineKeyboardButton(_i18n_t('bot.up.s40'), callback_data="opt|ttl|-1"),
            InlineKeyboardButton(_i18n_t('bot.up.s41'), callback_data="opt|ttl|1"),
        ],
        [
            InlineKeyboardButton(_i18n_t('bot.up.s42'), callback_data="opt|ttl|7"),
            InlineKeyboardButton(_i18n_t('bot.up.s43'), callback_data="opt|ttl|30"),
        ],
        [
            InlineKeyboardButton(_i18n_t('bot.up.s44'), callback_data="opt|ttl|90"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_protect_keyboard():
    """构建转发权限选择按钮。"""
    keyboard = [
        [
            InlineKeyboardButton(_i18n_t('bot.up.s45'), callback_data="opt|protect|true"),
            InlineKeyboardButton(_i18n_t('bot.up.s46'), callback_data="opt|protect|false"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_note_keyboard():
    """构建备注选择按钮。"""
    keyboard = [
        [InlineKeyboardButton(_i18n_t('bot.up.s47'), callback_data="opt|note|add")],
        [InlineKeyboardButton(_i18n_t('bot.up.s48'), callback_data="opt|note|skip")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── 中继外部文件处理 ───

async def _handle_external_relay_file(update: Update, context: ContextTypes.DEFAULT_TYPE, ext_code_override: str = ""):
    """处理中继账号转发到 Up Bot 的外部文件。
    格式:EXTERNAL_RELAY:{user_id}:{external_code}
    文件先缓存消息引用，积累后由 EXTERNAL_DONE 触发批量 copy 到存储频道（保持媒体组格式）。
    ext_code_override: 媒体组后续消息(无 caption)时,由 _dispatch_media 传入已解析的 external_code。
    """
    # R30-3: 校验发送者是否为受信中继账号，防止任意用户绕过上传权限/限速注入文件
    # C11: 统一使用 utils.relay_auth.is_relay_sender_allowed（fail-closed 语义一致）
    if not await is_relay_sender_allowed(update.effective_user.id):
        return

    if ext_code_override:
        # 媒体组后续消息:从已有 buffer 获取 user_id,external_code 由参数提供
        external_code = ext_code_override
        orig_caption = ""  # 后续消息无 caption
        async with _get_mg_lock():
            buf = _external_buffers.get(external_code)
            external_user_id = buf["user_id"] if buf else 0
            target_ch = buf["channel_id"] if buf else 0
        if not target_ch:
            # buffer 尚未创建(首条 caption 消息可能延迟到达),跳过等待首条
            logger.debug(f"[Up][ext_relay] 媒体组后续消息到达但 buffer 尚未创建 (code={external_code}), 跳过")
            return
    else:
        caption = update.message.caption or ""
        rest = caption[len("EXTERNAL_RELAY:"):]
        # 第一行是 user_id:code，换行之后是第三方 Bot 的原始 caption
        first_line, _, orig_caption = rest.partition("\n")
        user_end = first_line.find(":")
        if user_end == -1:
            return
        try:
            external_user_id = int(first_line[:user_end])
        except ValueError:
            return
        external_code = _decode_external_code(first_line[user_end + 1:])
        orig_caption = orig_caption.strip()

    caption_src_msg_id = update.message.message_id if orig_caption else None

    # 同一 external_code 的所有文件必须 copy 到同一存储频道
    async with _get_mg_lock():
        buf = _external_buffers.get(external_code)
        if buf is not None and buf.get("channel_id"):
            target_ch = buf["channel_id"]
        else:
            try:
                target_ch = await _get_upload_target_channel()
            except RuntimeError as e:
                logger.error(f"[Up][ext_relay] 无法获取上传目标频道 (code={external_code}): {e}")
                return
            _external_buffers[external_code] = {
                "user_id": external_user_id,
                "channel_id": target_ch,
                "msg_ids": [],
                "pending_copies": [],  # P1: 待批量 copy 的 (chat_id, message_id, file_type, file_meta)
                "files_meta": [],
                "file_types": defaultdict(int),
                "flushed": False,
                "created_at": time.time(),
                "orig_caption": orig_caption or "",
                "caption_src_msg_id": caption_src_msg_id,
            }

    file_type = detect_file_type(update)
    file_meta = extract_file_meta(update)
    # 内存级 file_unique_id 去重：防止并发请求导致同一文件被重复 copy
    fuid = _extract_file_unique_id(update.message)
    # R36 B0-1: 为 file_meta 注入结构化副本信息(group_id/file_unique_id/media_group_id)
    # 使下游 ReplicaAwareResolver 无需解析 batch_file_meta JSON
    file_meta = await _enrich_file_meta_for_replica(
        file_meta, target_ch,
        file_unique_id=fuid,
        media_group_id=update.message.media_group_id or "",
    )
    if fuid:
        async with _get_mg_lock():
            # P2: 惰性清理超 300s 的旧去重集合
            now = time.time()
            if external_code in _external_fuid_dedup:
                _fuids, _ts = _external_fuid_dedup[external_code]
                if now - _ts > 300:
                    _external_fuid_dedup.pop(external_code, None)
            if external_code not in _external_fuid_dedup:
                _external_fuid_dedup[external_code] = (set(), now)
            _fuids, _ts = _external_fuid_dedup[external_code]
            if fuid in _fuids:
                logger.info(f"[Up][ext_relay] 内存去重命中: fuid={fuid} (code={external_code}), 跳过重复文件")
                return
            _fuids.add(fuid)

    # B1 秒传去重:仅复用同 channel 的已有文件
    existing = await _check_dedup(target_ch, update.message)
    if existing and existing["channel_id"] == target_ch:
        # 去重命中:直接记录已有 msg_id,无需 copy
        channel_msg_id = existing["message_id"]
        logger.info(f"[Up][ext_relay] 秒传去重命中: reuse msg_id={channel_msg_id} (code={external_code})")
        await metrics.increment("up.dedup_hit")
        async with _get_mg_lock():
            buf = _external_buffers.get(external_code)
            if buf is None:
                return
            buf["msg_ids"].append(channel_msg_id)
            buf["files_meta"].append(file_meta)
            buf["file_types"][file_type] += 1
            if buf.get("timer"):
                buf["timer"].cancel()
            buf["timer"] = asyncio.get_running_loop().call_later(
                60, lambda: asyncio.ensure_future(_flush_external_buffer(external_code, safe_mode=True))
            )
            msg_count = len(buf["msg_ids"]) + len(buf["pending_copies"])
        logger.debug(f"[Up][ext_relay] 外部文件已缓存(去重) (code={external_code}), 共{msg_count}个文件")
    else:
        # 未命中去重:缓存消息引用,flush 时批量 copy 保持媒体组
        async with _get_mg_lock():
            buf = _external_buffers.get(external_code)
            if buf is None:
                # buffer 被安全超时 flush 清理了,重新创建
                buf = {
                    "user_id": external_user_id,
                    "channel_id": target_ch,
                    "msg_ids": [],
                    "pending_copies": [],
                    "files_meta": [],
                    "file_types": defaultdict(int),
                    "flushed": False,
                    "created_at": time.time(),
                    "orig_caption": orig_caption or "",
                    "caption_src_msg_id": caption_src_msg_id,
                }
                _external_buffers[external_code] = buf
            buf["pending_copies"].append((update.effective_chat.id, update.message.message_id, file_type, file_meta))
            if buf.get("timer"):
                buf["timer"].cancel()
            buf["timer"] = asyncio.get_running_loop().call_later(
                60, lambda: asyncio.ensure_future(_flush_external_buffer(external_code, safe_mode=True))
            )
            msg_count = len(buf["msg_ids"]) + len(buf["pending_copies"])
        logger.debug(f"[Up][ext_relay] 外部文件待批量copy (code={external_code}), 共{msg_count}个文件")


async def _handle_external_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 EXTERNAL_DONE 信号:中继账号通知文件收集完毕,触发批量写入"""
    # 安全校验:仅受信中继账号可触发 flush,防止任意用户提前 flush 外部缓冲区
    # C11: 统一使用 utils.relay_auth.is_relay_sender_allowed（fail-closed 语义一致）
    if not await is_relay_sender_allowed(update.effective_user.id):
        return

    text = update.message.text or ""
    if not text.startswith("EXTERNAL_DONE:"):
        return
    rest = text[len("EXTERNAL_DONE:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return
    try:
        int(rest[:user_end])  # 仅校验 user_id 段为数字，实际 flush 只需 external_code
    except ValueError:
        return
    external_code = rest[user_end + 1:].strip()

    await _flush_external_buffer(external_code, safe_mode=False)


async def _flush_external_buffer(external_code: str, safe_mode: bool = False):
    """刷新外部文件缓冲:先批量 copy 到存储频道,再写入 pending_uploads。
    如果 safe_mode=True，则 flush 已执行，EXTERNAL_DONE 到达时不应重复处理。
    """
    async with _get_mg_lock():
        buf = _external_buffers.get(external_code)
        if buf is None:
            return
        if buf.get("flushed"):
            return
        buf["flushed"] = True
        _external_buffers.pop(external_code, None)
        if buf.get("timer"):
            buf["timer"].cancel()
        # 清理 _external_mgid_map 中对应的映射
        stale_mgids = [k for k, (v, _) in _external_mgid_map.items() if v == external_code]
        for k in stale_mgids:
            _external_mgid_map.pop(k, None)
        # 清理内存级 file_unique_id 去重集合
        _external_fuid_dedup.pop(external_code, None)

    target_ch = buf.get("channel_id")
    if not target_ch:
        logger.error(f"[Up][ext_relay] 缓冲区缺失 channel_id (code={external_code})，跳过")
        return

    msg_ids = list(buf.get("msg_ids", []))
    files_meta = list(buf.get("files_meta", []))
    file_types = buf.get("file_types", defaultdict(int))
    pending_copies = buf.get("pending_copies", [])
    orig_caption = (buf.get("orig_caption") or "").strip()
    caption_src_msg_id = buf.get("caption_src_msg_id")
    ext_user_id = buf.get("user_id", 0)

    # R35 P0-4: 外部中继文件创建 upload_session
    upload_id = await _create_upload_session_for_upload(
        ext_user_id,
        source_msg_ids=[pc[1] for pc in pending_copies] if pending_copies else msg_ids,
        options={"file_types": dict(file_types), "external_code": external_code},
    )

    # P1: 批量 copy 未去重的文件到存储频道(保持媒体组格式)
    # copied_map: src_msg_id -> new_storage_msg_id（用于定位 caption 承载消息）
    copied_map: dict[int, int] = {}
    if pending_copies:
        from utils.flood_waiter import safe_copy_messages
        from_chat = pending_copies[0][0]
        copy_msg_ids = [pc[1] for pc in pending_copies]
        # R35 P0-4 §24: 复制前为每个文件创建 replication_task(PLANNED)
        await _ensure_channel_group_map()
        _ext_group_id = _channel_to_group.get(target_ch, 0)
        _ext_task_ids: list[int] = []
        for pc in pending_copies:
            _pc_fuid = pc[3].get("file_unique_id", "") if isinstance(pc[3], dict) else ""
            _tid = await _create_replication_task_safe(
                _ext_group_id, _pc_fuid, pc[0], target_ch, pc[1],
            )
            _ext_task_ids.append(_tid)
        # 标记 COPYING
        for _tid in _ext_task_ids:
            await _mark_replication_copying_safe(_tid)
        try:
            if len(copy_msg_ids) == 1:
                from utils.flood_waiter import safe_copy_message
                forwarded = await safe_copy_message(_bot, target_ch, from_chat, copy_msg_ids[0])
                new_ids = [forwarded.message_id]
            else:
                results = await safe_copy_messages(_bot, target_ch, from_chat, copy_msg_ids)
                new_ids = [r.message_id for r in results]
            logger.info(f"[Up][ext_relay] 批量 copy 到存储频道: {len(new_ids)}个文件 (code={external_code})")
            for i, (_, src_mid, ft, fm) in enumerate(pending_copies):
                if i < len(new_ids):
                    new_mid = new_ids[i]
                    msg_ids.append(new_mid)
                    files_meta.append(fm)
                    file_types[ft] += 1
                    copied_map[src_mid] = new_mid
                    # R35 P0-4 §24: copy 返回 dst_msg_id 后标记 COPIED_UNVERIFIED
                    if i < len(_ext_task_ids):
                        await _mark_replication_copied_safe(_ext_task_ids[i], new_mid)
        except Exception as e:
            logger.error(f"[Up][ext_relay] 批量 copy 失败,回退逐条 (code={external_code}): {e}")
            # R35 P0-4 §24: 批量失败,标记所有任务 FAILED
            for _tid in _ext_task_ids:
                await _mark_replication_failed_safe(_tid, f"ext_batch_copy_failed: {e}")
            from utils.flood_waiter import safe_copy_message
            for idx, (from_chat, msg_id, ft, fm) in enumerate(pending_copies):
                try:
                    forwarded = await safe_copy_message(_bot, target_ch, from_chat, msg_id)
                    new_mid = forwarded.message_id
                    msg_ids.append(new_mid)
                    files_meta.append(fm)
                    file_types[ft] += 1
                    copied_map[msg_id] = new_mid
                    # R35 P0-4 §24: 逐条回退成功,标记 COPIED_UNVERIFIED
                    if idx < len(_ext_task_ids):
                        await _mark_replication_copied_safe(_ext_task_ids[idx], new_mid)
                except Exception as e2:
                    logger.error(f"[Up][ext_relay] 逐条 copy 也失败 (msg={msg_id}): {e2}")
                    # R35 P0-4 §24: 逐条也失败,标记 FAILED
                    if idx < len(_ext_task_ids):
                        await _mark_replication_failed_safe(_ext_task_ids[idx], f"ext_single_copy_failed: {e2}")

    if not msg_ids:
        logger.warning(f"[Up][ext_relay] 外部文件缓冲区为空，跳过 (code={external_code})")
        # R35 P0-4: 外部文件缓冲区为空,推进 FAILED_RETRYABLE
        await _transition_upload_session_safe(
            upload_id, "FAILED_RETRYABLE", reason="ext_buffer_empty",
            last_error="no msg_ids after copy",
        )
        return

    # R35 P0-4: 外部文件 copy 完成,推进 RECEIVED → COPIED_PRIMARY
    await _transition_upload_session_safe(
        upload_id, "COPIED_PRIMARY", reason="ext_copy_done",
        primary_channel_id=target_ch,
        primary_msg_ids=msg_ids,
    )

    # 如果有第三方原始 caption，编辑存储频道中承载 caption 的消息，去除 EXTERNAL_RELAY 路由前缀
    if orig_caption and caption_src_msg_id and caption_src_msg_id in copied_map:
        target_mid = copied_map[caption_src_msg_id]
        try:
            await _bot.edit_message_caption(chat_id=target_ch, message_id=target_mid, caption=orig_caption)
            logger.info(f"[Up][ext_relay] 已还原第三方原始 caption (code={external_code}, msg_id={target_mid})")
        except Exception as e:
            logger.warning(f"[Up][ext_relay] 编辑存储频道 caption 失败(非致命, code={external_code}): {e}")
    elif not orig_caption and caption_src_msg_id and caption_src_msg_id in copied_map:
        # 无原始 caption 时，清除 EXTERNAL_RELAY 前缀行，使消息无 caption（后续由 dsp_bot 添加标准 caption）
        target_mid = copied_map[caption_src_msg_id]
        try:
            await _bot.edit_message_caption(chat_id=target_ch, message_id=target_mid, caption="")
        except Exception as e:
            logger.debug(f"[Up][ext_relay] 清除 EXTERNAL_RELAY caption 失败(非致命, code={external_code}): {e}")

    type_str = _json_dumps(dict(file_types))
    batch_ids_str = ",".join(str(mid) for mid in msg_ids)
    batch_file_meta_str = _json_dumps(files_meta)
    note_obj: dict = {"type": "external", "code": external_code}
    if orig_caption:
        note_obj["preserve_caption"] = True
    note = _json_dumps(note_obj)

    try:
        pending_col = get_pending_uploads_col()
        _ext_record = {
            "uploader_id": ext_user_id,
            "primary_channel_id": target_ch,
            "primary_channel_msg_id": msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "note": note,
            "status_msg_id": 0,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "processed": 0,
            "protect_content": False,
            "file_ttl_days": 0,
            "upload_id": upload_id,  # R35 P0-4: 关联 upload_session
        }
        await pending_col.insert_one(_ext_record)
        # R40 P0-4: 同步双写 SQLite local(Idx Bot 读取源)
        try:
            from database.cache_store import get_cache_store
            # R75 P0-07: mark_dirty=True (scanner 要求;CRDB 已写入,dirty_outbox 为冗余兜底)
            await get_cache_store().insert_pending_upload_local(_ext_record, mark_dirty=True)
            logger.debug(f"[Up][ext_relay] R40 P0-4: pending_uploads_local 双写成功 code={external_code}")
        except Exception as sqlite_err:
            logger.warning(f"[Up][ext_relay] R40 P0-4: pending_uploads_local 双写失败(非致命): {sqlite_err}")
        logger.info(f"[Up][ext_relay] 外部文件已写入pending_uploads: code={external_code}, {len(msg_ids)}个文件")
    except Exception as e:
        logger.error(f"[Up][ext_relay] 写入pending_uploads失败 (code={external_code}): {e}")
        # R35 P0-4: 写入失败,推进 FAILED_RETRYABLE
        await _transition_upload_session_safe(
            upload_id, "FAILED_RETRYABLE", reason=f"ext_write_failed: {e}",
            last_error=str(e),
        )
        return

    # R35 P0-4: pending_uploads 写入成功,推进 COPIED_PRIMARY → MANIFEST_PENDING
    # 并创建 outbox 条目供 Manifest Worker 消费
    # R37 P1-3: 使用 strict 版本(outbox 是权威事件,失败必须中断主流程)
    await _transition_upload_session_strict(
        upload_id, "MANIFEST_PENDING", reason="ext_pending_uploads_written",
    )
    await _create_outbox_entry_strict(
        f"obx-man-{upload_id}", upload_id, ext_user_id,
        target_ch, msg_ids=msg_ids,
        file_meta=files_meta, event_type="REGISTER_MANIFEST",
        protect=0,
    )
    await _create_outbox_entry_strict(
        f"obx-r100-{upload_id}", upload_id, ext_user_id,
        target_ch, msg_ids=msg_ids,
        file_meta=files_meta, event_type="ARCHIVE_R100",
        protect=0,
    )
    # R35 P0-4 §24: pending_uploads + outbox 写入成功,标记 replication_tasks COMMITTED
    for _tid in _ext_task_ids:
        await _mark_replication_committed_safe(_tid)

    try:
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.warning(f"[Up][ext_relay] 通知 idx_bot 失败(不影响上传): {e}")


# ─── R40 新增命令(状态/任务/合集/通知) ─────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看上传状态: /status <upload_id>"""
    try:
        if not context.args:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s53')))
            return
        upload_id = context.args[0]
        receipt = await upload_receipt.get_upload_status(upload_id)
        if not receipt:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s54')))
            return
        text = await upload_receipt.format_receipt(receipt)
        await safe_reply_text(update.message, UserMessage.from_raw_text(text))
    except Exception as e:
        logger.exception(f"[Up][status] 查询上传状态失败: {e}")
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s3')))


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看个人任务列表: /tasks [状态]"""
    try:
        user = update.effective_user
        if not user:
            return
        status_filter = context.args[0] if context.args else None
        tasks = await task_center.list_user_tasks(user.id, status=status_filter, limit=20)
        if not tasks:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s55')))
            return
        lines = [await task_center.format_task_status(t) for t in tasks]
        await safe_reply_text(update.message, UserMessage.from_raw_text("\n\n".join(lines)))
    except Exception as e:
        logger.exception(f"[Up][tasks] 查询任务列表失败: {e}")
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s3')))


async def cmd_collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看个人合集列表: /collections [页码]"""
    try:
        user = update.effective_user
        if not user:
            return
        page = 1
        if context.args:
            try:
                page = int(context.args[0])
            except ValueError:
                page = 1
        result = await collections_svc.list_collections(owner_id=user.id, page=page, page_size=10)
        items = result.get("items", [])
        if not items:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s56')))
            return
        lines = [_i18n_t('bot.up.s16', result_get_page_1=result.get('page', 1), result_get_total_pages_1=result.get('total_pages', 1), result_get_total_0=result.get('total', 0))]
        for c in items:
            lines.append(_i18n_t('bot.up.s49', c_get_name=c.get('name', '未命名'), c_get_id=c.get('id')))
        await safe_reply_text(update.message, UserMessage.from_raw_text("\n".join(lines)))
    except Exception as e:
        logger.exception(f"[Up][collections] 查询合集列表失败: {e}")
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s3')))


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看未读通知: /notifications"""
    try:
        user = update.effective_user
        if not user:
            return
        items = await notifications.list_unread(user.id, limit=20)
        if not items:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up.s57')))
            return
        lines = [await notifications.format_notification(n) for n in items]
        await safe_reply_text(update.message, UserMessage.from_raw_text("\n\n".join(lines)))
    except Exception as e:
        logger.exception(f"[Up][notifications] 查询通知失败: {e}")
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.up_bot.s3')))


async def _init():
    from database import init_db
    await init_db()
    await _refresh_active_slots()
    # R47 P1-E: 启动扫描 — 补写未对账副本的 Manifest(实际补写,不只是 list)
    # 扫描 unregistered_copies 表,对每条未对账行调用 _register_manifest 补写 Manifest,
    # 成功后标记 reconciled;失败则不标记,下次启动重试
    try:
        stats = await _reconcile_unregistered_copies()
        if stats.get("total", 0) > 0:
            logger.info(f"[Up][R47] 启动扫描统计: {stats}")
    except Exception as e:
        logger.error(f"[Up][R47] 启动扫描异常(不阻塞启动): {e}")


async def _async_main():
    # R48 P1-b: 每次 Bot 启动时显式触发 production secret 检查(fail-closed)
    from services.button_security import validate_production_config
    validate_production_config()

    await _init()
    from database.cache_store import report_bot_heartbeat
    await report_bot_heartbeat("up_bot")
    # A1: 启动计数器定期上报(跨进程聚合)
    from utils.monitor import start_counter_reporter
    asyncio.create_task(start_counter_reporter("up_bot"))

    logger.info("[Up] 启动上传机器人（Up Bot）...")

    # R76 O2: 通过 build_provider_client 工厂统一选择 backend
    # - telegram backend: 返回 telegram.Bot 实例(生产真实使用)
    # - contract backend: 返回 ContractProviderClient 实例(secretless CI/本地测试)
    # 工厂内部校验 PROVIDER_BACKEND / PROVIDER_BASE_URL / PROVIDER_CONTRACT_TOKEN,
    # 业务层不再直接 Application.builder().token(...) 或 from telegram import Bot。
    _provider_backend = getattr(settings, "PROVIDER_BACKEND", _PROVIDER_BACKEND_TELEGRAM)

    global _bot
    if _provider_backend == _PROVIDER_BACKEND_CONTRACT:
        # R76 O2: contract 模式 — 不构建 Application,updates 由 web_adapter
        # /internal/contract/update 端点接收并 dispatch(O5 实现)。
        # bot 由 build_provider_client 构造为 ContractProviderClient,
        # 业务函数(process_queue 等)接收兼容 client,逻辑不变。
        _bot = build_provider_client(settings, TOKEN)
        logger.info(
            "[Up] R76-O2 contract 模式: bot 由 build_provider_client 构造,"
            "updates 等待 web_adapter dispatch"
        )

        # 注册 stop 信号等待(web_adapter 在独立 FastAPI 进程中接收 update,
        # 本进程只负责消费者循环;O6 实现 docker-compose.secretless.yml 启动顺序)
        from run_all import _set_stop_event
        stop_event = asyncio.Event()
        _set_stop_event(stop_event)

        # 启动 outbox worker 和后台任务(与 telegram 模式一致)
        from database.cache_store import get_cache_store

        async def _contract_health_ping():
            while True:
                await metrics.ping_bot("up_bot")
                await report_bot_heartbeat("up_bot")
                await asyncio.sleep(30)

        create_safe_task(_contract_health_ping(), name="health-ping")
        create_safe_task(_cleanup_pending(), name="cleanup-pending")
        from services.outbox_worker import OutboxWorker
        outbox_worker = OutboxWorker(
            store=get_cache_store(),
            register_manifest_fn=_outbox_register_manifest_strict,
            archive_to_r100_fn=_outbox_archive_to_r100_strict,
            notify_upload_failed_fn=None,
            owner=f"up_bot-contract-{__import__('socket').gethostname()}-{__import__('os').getpid()}",
        )
        await outbox_worker.start()

        # R80 P0-02: secretless CI 无 bootstrap,cells_local 为空。
        # 上传流程需要至少一个 active cell 作为目标频道 — 从配置种入。
        from database import get_active_cells_local as _get_cells
        _existing_cells = await _get_cells()
        if not _existing_cells:
            _ch_raw = getattr(settings, "ACCOUNT_1_CHANNELS", "") or "-1002000000001"
            _ch_id = int(str(_ch_raw).split(",")[0].strip())
            await get_cache_store().bulk_upsert_cells_local([{
                "slot_id": 1,
                "channel_id": _ch_id,
                "status": "active",
                "account_name": getattr(settings, "ACCOUNT_1_NAME", "ci-account"),
                "is_r100": 0,
            }])
            await _refresh_active_slots()
            logger.info(f"[Up] R80: contract 模式种入测试 cell channel={_ch_id}")

        # R80 P0-02: 启动 contract web adapter(uvicorn),提供
        # POST /internal/contract/update 和 GET /internal/contract/transactions/{trace_id}
        # 供 E2E 黑盒驱动器从宿主机提交 Update 并查询终态。
        import uvicorn
        from services.sink_adapters.web_adapter import create_contract_app

        _contract_token = getattr(settings, "PROVIDER_CONTRACT_TOKEN", "") or ""
        contract_app = create_contract_app(
            contract_token=_contract_token,
            public_dispatcher=_dispatch_media,
            bot=_bot,
        )
        _uvicorn_config = uvicorn.Config(
            contract_app,
            host="0.0.0.0",
            port=8000,
            log_level="warning",
            access_log=False,
        )
        _uvicorn_server = uvicorn.Server(_uvicorn_config)
        _uvicorn_task = create_safe_task(
            _uvicorn_server.serve(), name="contract-web-adapter"
        )
        logger.info(
            "[Up] R80 P0-02: contract web adapter 已启动 (0.0.0.0:8000)"
        )

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[Up] contract 模式收到停止信号,正在优雅关闭...")
            _uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(outbox_worker.stop(), timeout=10.0)
            except Exception as e:
                logger.warning(f"[Up] OutboxWorker 停止异常: {e}")
            logger.info("[Up] contract 模式关闭完成")
        return

    # telegram backend(生产路径)— 通过工厂构造 bot,但仍需 Application 注册 handler
    _bot = build_provider_client(settings, TOKEN)
    app = Application.builder().token(TOKEN).build()
    # 使用 app.bot 作为 _bot(Application 集成需要 app.bot 而非独立 bot 实例)
    _bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start_upload", start_upload))
    app.add_handler(CommandHandler("end_upload", end_upload))
    app.add_handler(CommandHandler("cancel_upload", cancel_upload))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("cancel_note", cancel_note))
    app.add_handler(CommandHandler("new_collection", new_collection))
    app.add_handler(CommandHandler("end_collection", end_collection))
    app.add_handler(CommandHandler("cancel_collection", cancel_collection))
    app.add_handler(CommandHandler("note_collection", note_collection))
    # R40 新增命令
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("collections", cmd_collections))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CallbackQueryHandler(upload_option_callback, pattern=r"^opt\|"))

    # 备注文字输入处理（需在 EXTERNAL_DONE 和 media 之前）
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^EXTERNAL_DONE:"),
        _handle_note_text
    ))

    # 中继外部文件完成信号
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"^EXTERNAL_DONE:"),
        _handle_external_done
    ))

    media_filter = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.PHOTO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
    )
    app.add_handler(MessageHandler(media_filter, _dispatch_media))

    await metrics.ping_bot("up_bot")

    async def health_ping():
        while True:
            await metrics.ping_bot("up_bot")
            await report_bot_heartbeat("up_bot")
            await asyncio.sleep(30)

    async def slot_refresh_loop():
        while True:
            await _refresh_active_slots()
            await asyncio.sleep(60)

    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(slot_refresh_loop(), name="slot-refresh")
    create_safe_task(_cleanup_pending(), name="cleanup-pending")

    # R36 B0-2: 启动 OutboxWorker(唯一副作用驱动器,消费 upload_outbox 表)
    # Manifest/R100 注册不再 fire-and-forget create_task,改为通过 outbox 表持久化 + worker 消费
    from services.outbox_worker import OutboxWorker
    outbox_worker = OutboxWorker(
        store=get_cache_store(),
        register_manifest_fn=_outbox_register_manifest_strict,
        archive_to_r100_fn=_outbox_archive_to_r100_strict,
        notify_upload_failed_fn=None,  # UPLOAD_FAILED 事件暂未启用
        owner=f"up_bot-{__import__('socket').gethostname()}-{__import__('os').getpid()}",
    )
    await outbox_worker.start()

    # R76 O2: contract 模式已在上方 return;此处为 telegram backend(生产路径),
    # 必须运行完整 Application 生命周期(start_polling + app.run)。
    # 若 TOKEN 为空,Application.start() 会失败 — 这是 fail-closed 行为,
    # CI 应使用 contract 模式而非空 token 占位。
    from run_all import _set_stop_event
    stop_event = asyncio.Event()
    _set_stop_event(stop_event)

    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[Up] 收到停止信号,正在优雅关闭...")
            try:
                await asyncio.wait_for(outbox_worker.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[Up] OutboxWorker 停止超时(10s),强制继续")
            except Exception as e:
                logger.warning(f"[Up] OutboxWorker 停止异常: {e}")
            try:
                await asyncio.wait_for(app.updater.stop(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("[Up] polling 关闭超时(15s),强制继续")
            except Exception as e:
                logger.warning(f"[Up] polling 关闭异常: {e}")
            try:
                await asyncio.wait_for(app.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[Up] app.stop 超时(10s),强制继续")
            except Exception as e:
                logger.warning(f"[Up] app.stop 异常: {e}")
            logger.info("[Up] 优雅关闭完成")


def run():
    """启动 Up Bot(使用 asyncio.run 标准模式)"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
