"""Idx Bot - 解码机器人(环形冗余架构)
职责:生成文件码 + 解码(内部/外部) -> 写 jobs 派工表
与原来的 decoder_bot 功能一致,区别是解码后调用 enqueue_job() 而非 queue_manager
"""

import asyncio
import datetime
try:
    import orjson as json
except ImportError:
    import json
import re
import time
from collections import defaultdict, deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from loguru import logger

from config import settings
from database import (
    get_file_records_col,
    get_file_record_cached,
    get_decode_logs_col,
    get_pending_uploads_col,
    get_codes_col,
    make_file_record,
    make_decode_log,
    make_code_entry,
    save_code_bot_mapping,
    get_bot_for_code,
    resolve_bot_for_code,
    get_bot_decode_interval,
    get_active_cells_local,
    enqueue_job,
    get_system_code_for_external,
    get_pending_jobs_count_local,
)
from services.code_generator import generate_unique_code, is_valid_code_format, extract_code_and_bot_from_message
from services.permission import check_decode_permission, get_or_create_user
from services.relay_pool import relay_pool
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics
from utils.dynamic_rate_limiter import dynamic_rate_limiter
from utils.task_utils import create_safe_task
from utils.force_join import check_force_join, three_bot_reminder
from utils.flood_waiter import safe_send_message, safe_reply_text

TOKEN = settings.DECODER_BOT_TOKEN

_pending_external: dict[str, deque[tuple[int, str, float]]] = defaultdict(deque)
_ext_lock = asyncio.Lock()
_PENDING_TTL = settings.PENDING_TTL

# ─── Quota 同步至 CRDB（SQLite First，每 6h 批量写入）─────────
_QUOTA_SYNC_INTERVAL = 21600  # 6 小时


async def _quota_sync_loop():
    """后台任务: 每6小时将 SQLite 配额同步到 CRDB"""
    while True:
        await asyncio.sleep(_QUOTA_SYNC_INTERVAL)
        try:
            from services.permission import sync_quotas_to_crdb
            await sync_quotas_to_crdb()
        except Exception as e:
            logger.error(f"[Quota] sync loop error: {e}")


async def _enqueue_external(bot_username: str, user_id: int, code: str):
    async with _ext_lock:
        _pending_external[bot_username].append((user_id, code, time.time()))


async def _dequeue_external(bot_username: str) -> tuple[int, str]:
    async with _ext_lock:
        q = _pending_external.get(bot_username)
        while q:
            entry = q.popleft()
            if time.time() - entry[2] < _PENDING_TTL:
                return entry[0], entry[1]
        return None, None


async def _cleanup_stale_pending():
    now = time.time()
    async with _ext_lock:
        stale = []
        for bot, q in _pending_external.items():
            while q and now - q[0][2] >= _PENDING_TTL:
                q.popleft()
            if not q:
                stale.append(bot)
        for bot in stale:
            del _pending_external[bot]


async def _relay_pending_cleanup(bot_username: str):
    await _dequeue_external(bot_username)

_external_media_groups: dict[str, tuple[int, str, float]] = {}
_MEDIA_GROUP_TTL = settings.EXTERNAL_MEDIA_GROUP_TTL
_bot_last_request: dict[str, float] = {}


async def _wait_bot_interval(bot_username: str):
    interval = await get_bot_decode_interval(bot_username)
    if interval <= 0:
        return
    last = _bot_last_request.get(bot_username, 0)
    elapsed = time.time() - last
    if elapsed < interval:
        await asyncio.sleep(interval - elapsed)
    _bot_last_request[bot_username] = time.time()


async def _track_external_media_group(media_group_id: str, user_id: int, code: str):
    async with _ext_lock:
        _external_media_groups[media_group_id] = (user_id, code, time.time())


async def _get_external_media_group_user(media_group_id: str) -> tuple[int, str]:
    async with _ext_lock:
        entry = _external_media_groups.pop(media_group_id, None)
        if entry and time.time() - entry[2] < _MEDIA_GROUP_TTL:
            return entry[0], entry[1]
        return None, None


async def _cleanup_media_groups():
    now = time.time()
    async with _ext_lock:
        stale = [k for k, v in _external_media_groups.items() if now - v[2] >= _MEDIA_GROUP_TTL]
        for k in stale:
            _external_media_groups.pop(k, None)


_MEDIA_GROUP_BUFFER_WAIT = settings.MEDIA_GROUP_BUFFER_WAIT
_media_group_buffer: dict[str, dict] = {}


def _parse_storage_ids_from_caption(caption: str) -> list[int]:
    if not caption:
        return []
    for line in caption.split("\n"):
        if line.startswith("STORAGE_IDS:"):
            return [int(x) for x in line[len("STORAGE_IDS:"):].split(",") if x.strip().isdigit()]
    return []


# ─── 通道选择: cells 表获取 active 槽位 ───

# ─── 活跃频道本地缓存(避免每次解码都查询 cells) ───
_active_channels_cache: list[dict] = []
_active_channels_index = 0
_ch_lock = asyncio.Lock()


async def _refresh_active_channels():
    """每60 秒刷新一次活跃频道缓存, 与 Up Bot 对齐"""
    global _active_channels_cache, _active_channels_index
    try:
        _active_channels_cache = await get_active_cells_local()
        _active_channels_index = 0
    except Exception as e:
        logger.warning(f"[Idx] 刷新活跃频道失败: {e}")
        pass


async def _get_storage_channel() -> int:
    """获取当前活跃存储频道(从本地缓存, 每60 秒刷新一次)"""
    global _active_channels_index
    if _active_channels_cache:
        async with _ch_lock:
            idx = _active_channels_index % len(_active_channels_cache)
            _active_channels_index += 1
        return _active_channels_cache[idx]["channel_id"]
    # 缓存未就绪, 回退 DB 查询
    try:
        cells = await get_active_cells_local()
        if cells:
            return cells[0]["channel_id"]
    except Exception as e:
        logger.warning(f"[Idx] 获取存储频道失败: {e}")
        pass
    return 0


# ─── 入队新方jobs ───

async def _dispatch_to_dsp(
    target_user_id: int,
    code: str,
    storage_channel_id: int,
    msg_ids: list[int],
    batch_file_meta: str = "",
    protect_content: bool = False,
):
    """将解码结果写jobs Dsp Bot 轮询发送"""
    try:
        await enqueue_job(
            code=code,
            target_user_id=target_user_id,
            storage_channel_id=storage_channel_id,
            storage_msg_ids=msg_ids,
            batch_file_meta=batch_file_meta,
            task_type="batch" if len(msg_ids) > 1 else "single",
            protect_content=protect_content,
        )
        logger.info(
            f"[Idx] 已写 jobs  user={target_user_id}, code={code}, "
            f"{len(msg_ids)} 个文件\n"
        )
    except Exception as e:
        logger.error(f"[Idx] 写入 jobs 失败 (user={target_user_id}, code={code}): {e}")
        raise


async def _flush_media_group_buffer(media_group_id: str):
    await asyncio.sleep(_MEDIA_GROUP_BUFFER_WAIT)
    entry = _media_group_buffer.pop(media_group_id, None)
    if not entry:
        return

    bot = entry["bot"]
    msgs = entry["msgs"]
    user_id = entry["user_id"]
    code = entry["code"]
    source = entry.get("source", "external")

    if not msgs:
        return

    if source == "relay":
        await asyncio.sleep(2)
        try:
            storage_ids = set()
            for _, _, cap in msgs:
                ids = _parse_storage_ids_from_caption(cap)
                storage_ids.update(ids)
            storage_ids = sorted(storage_ids)

            if not storage_ids:
                logger.warning(f"[Idx] 中继媒体 无STORAGE_IDS (code={code})")
                return

            # A2: 走缓存,避免每次直查 CRDB
            record = await get_file_record_cached(code)
            if not record:
                logger.warning(f"[Idx] 中继媒体 DB无记(code={code})")
                return

            storage_channel = record.get("primary_channel_id") or await _get_storage_channel()
            await _dispatch_to_dsp(user_id, code, storage_channel, list(storage_ids))

        except Exception as e:
            logger.error(f"[Idx] 中继媒体组处理失(code={code}): {e}")
            try:
                await safe_send_message(bot, chat_id=user_id, text="外部文件发送失败，请稍后重试或联系管理员。")
            except Exception:
                pass
        return

    # 外部媒体组 Bot 间不可达,仅记录日志
    # 外部码解码走中继系统(真实 Telegram 账号),中继账号上传到存储频道后RELAY_BATCH 通知
    logger.warning(f"[Idx][mg_buf] 外部媒体组无法直接处理(Bot 间无权限),code={code}, {len(msgs)}条消息")
    return


# ─── 中继处理 ───

async def handle_relay_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.startswith(("RELAY_DELIVER:", "RELAY_RENEW:", "RELAY_ERROR:", "RELAY_BATCH:")):
        return

    is_renew = text.startswith("RELAY_RENEW:")
    is_error = text.startswith("RELAY_ERROR:")
    is_batch = text.startswith("RELAY_BATCH:")
    parts = text.split(":", 2)
    if len(parts) != 3:
        return
    try:
        target_user_id = int(parts[1])
    except ValueError:
        return
    code = parts[2]

    if is_error:
        logger.info(f"[Idx] 外部机器人返回错 code={code}")
        await safe_send_message(context.bot, chat_id=target_user_id, text="外部文件码查询失败，该码可能已失效或暂时不可用，请稍后重试。")
        return

    if is_renew:
        logger.info(f"[Idx] 记录已过 {code}")
        await safe_send_message(context.bot, chat_id=target_user_id, text=f"文件 {code} 的缓存已过期，请重新发送该码以获取最新文件。")
        return

    if is_batch:
        if "\n" in code:
            code = code.split("\n")[0].strip()
        storage_ids = _parse_storage_ids_from_caption(text)
        logger.info(f"[Idx] RELAY_BATCH: user={target_user_id}, code={code}, storage_ids={storage_ids}")
        if not storage_ids:
            return

        # A2: 走缓存，避免每次直查 CRDB
        record = await get_file_record_cached(code)
        if not record:
            return

        storage_channel = record.get("primary_channel_id") or await _get_storage_channel()
        try:
            await _dispatch_to_dsp(target_user_id, code, storage_channel, list(storage_ids))
        except Exception as e:
            logger.error(f"[Idx] RELAY_BATCH 写入 jobs 失败: {e}")
            await safe_send_message(context.bot, chat_id=target_user_id, text="文件发送失败，请稍后重试。")
            return

        if context:
            try:
                await safe_send_message(context.bot, chat_id=target_user_id, text=f"您请求的文件 {code} 将由 @{settings.SENDER_BOT_USERNAME} 发送给你，请查收。")
            except Exception:
                pass
        return

    logger.info(f"[Idx] 中继代发: user {target_user_id}, code {code}")

    # A2: 走缓存，避免每次直查 CRDB
    record = await get_file_record_cached(code)
    if not record:
        await safe_send_message(context.bot, chat_id=target_user_id, text=f"您请求的文件 {code} 已处理，请重新发送该码获取文件。")
        return

    storage_channel = record.get("primary_channel_id") or await _get_storage_channel()
    msg_ids_raw = record.get("batch_msg_ids") or ""
    if not isinstance(msg_ids_raw, str):
        msg_ids_raw = str(msg_ids_raw)
    msg_ids = [int(mid) for mid in msg_ids_raw.split(",") if mid.strip().isdigit()]
    if not msg_ids:
        msg_ids = [record.get("primary_channel_msg_id")]

    try:
        await _dispatch_to_dsp(target_user_id, code, storage_channel, msg_ids, record.get("batch_file_meta", ""))
    except Exception as e:
        logger.error(f"[Idx] 中继代发写入 jobs 失败: {e}")
        await safe_send_message(context.bot, chat_id=target_user_id, text="文件发送失败，请稍后重试。")
        return

    try:
        await safe_send_message(context.bot, chat_id=target_user_id, text=f"您请求的文件 {code} 将由 @{settings.SENDER_BOT_USERNAME} 发送给你，请查收。")
    except Exception:
        pass


# ─── 命令处理 ───

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    try:
        await get_or_create_user(user.id, username=user.username, first_name=user.first_name)
    except Exception as e:
        logger.error(f"[Idx][start] 创建用户失败 (user={user.id}): {e}")
        await safe_reply_text(update.message, "系统繁忙,请稍后重试")
        return

    # 标记用户已启动 idx bot
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.mark_user_started(user.id, "idx")

    await safe_reply_text(update.message,
        "欢迎使用文件解码机器\n\n"
        "发送文件码即可获取对应文件。\n"
        "发/status 查看您的会员状态和今日剩余解码次数。\n"
        "发/help 查看帮助信息\n"
        + three_bot_reminder()
    )

    # 补发用户启动前暂存的文件码（发送成功一条才删一条，避免发送失败丢码）
    try:
        pending_codes = await store.get_pending_file_codes(user.id)
        sent_count = 0
        for pc in pending_codes:
            try:
                if pc["ext_code"]:
                    await safe_send_message(context.bot, chat_id=user.id,
                        text=f"外部文件 {pc['ext_code']} 已就绪，请重新发送文件码即可查收。")
                else:
                    note_line = f"备注：{pc['note']}" if pc["note"] else ""
                    await safe_send_message(context.bot, chat_id=user.id,
                        text=f"文件码：{pc['file_code']}\n{note_line}\n\n"
                             f"📤 发送文件 @{settings.UPLOAD_BOT_USERNAME}\n"
                             f"🔍 收码解码 @{settings.DECODER_BOT_USERNAME}\n"
                             f"📥 收取文件 @{settings.SENDER_BOT_USERNAME}")
                await store.delete_pending_file_code(pc["id"])
                sent_count += 1
            except Exception as send_err:
                logger.warning(f"[Idx][start] 补发文件码失败 (user={user.id}, code={pc['file_code']}): {send_err}，该码保留在暂存表中")
                break
        if sent_count:
            logger.info(f"[Idx][start] 补发 {sent_count} 条暂存文件码给用户 {user.id}")
    except Exception as e:
        logger.error(f"[Idx][start] 补发暂存文件码失败 (user={user.id}): {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        "文件解码机器使用帮助\n\n"
        "1. 获取文件:直接发送文件码即可获取文件。\n"
        "2. 上传文件:请使用上传机器人发送文上传后会自动收到文件码。\n"
        "3. 分享文件:将文件码分享给其他用对方发送给我即可获取文件。\n\n"
        "会员权益:\n"
        f"- 免费用户:每日解码 {settings.FREE_DAILY_QUOTA} 仅限本系统文件码\n"
        f"- 基础会员:每日解码 {settings.BASIC_DAILY_QUOTA} 可上可解码非本系统文件码\n"
        f"- 高级会员:无限解码,可上可解码非本系统文件码\n\n"
        "文件码格式说\n"
        "码的开头即对应机器人的用户Telegram 机器人必须以 bot 结尾)。\n"
        f"本系统码{settings.FILE_CODE_PREFIX}_a1b2c3d4e5f6_3p_2v_1d\n"
        "外部码如:QQfile2_bot:qq10ad1e0200_6V\n"
        "系统会根_bot 自动识别目标机器人并路由解码。\n\n"
        "文件码永久有不会过期。\n\n"
        "如有问题请联系管理员"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    try:
        db_user = await get_or_create_user(user.id)
    except Exception as e:
        logger.error(f"[Idx][status] 获取用户信息失败: {e}")
        await safe_reply_text(update.message, "系统繁忙,无法获取用户信息,请稍后重试")
        return
    level_map = {"free": "免费用户", "basic": "基础会员", "premium": "高级会员"}
    level_name = level_map.get(db_user.get("membership_level"), "未知")

    # 从本地 SQLite 读取配额（更准确）
    from database.cache_store import get_user_quota
    local_q = await get_user_quota(user.id)
    if local_q:
        total = local_q.get("daily_quota", settings.FREE_DAILY_QUOTA)
        used = local_q.get("used_today", 0)
        ext_quota = local_q.get("ext_quota", 0)
        ext_used = local_q.get("ext_used_today", 0)
    else:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        quota_date_str = db_user.get("quota_date")
        quota_date = None
        if quota_date_str:
            try:
                quota_date = datetime.datetime.fromisoformat(quota_date_str).date()
            except (ValueError, TypeError):
                pass
        total = db_user.get("daily_decode_quota", settings.FREE_DAILY_QUOTA)
        used = db_user.get("quota_used_today", 0) if quota_date == today else 0
        ext_quota = db_user.get("external_decode_quota", 0)
        ext_date_str = db_user.get("external_quota_date")
        ext_date = None
        if ext_date_str:
            try:
                ext_date = datetime.datetime.fromisoformat(ext_date_str).date()
            except (ValueError, TypeError):
                pass
        ext_used = db_user.get("external_used_today", 0) if ext_date == today else 0

    if db_user.get("membership_level") == "premium":
        quota_str = "无限"
    else:
        quota_str = f"{max(0, total - used)}/{total}"

    if ext_quota == -1:
        ext_str = "不限"
    elif ext_quota == 0:
        ext_str = "无权"
    else:
        ext_str = f"{max(0, ext_quota - ext_used)}/{ext_quota}"

    await safe_reply_text(update.message,
        f"用户状态\n"
        f"会员等级:{level_name}\n"
        f"今日剩余解码次数:{quota_str}\n"
        f"上传权限:{'有' if db_user.get('can_upload') else '无'}\n"
        f"外部码解码配{ext_str}"
    )


# ─── pending_uploads 轮询(生成文件 ───

async def _process_one_pending(app: Application, row: dict):
    """处理单个 pending_upload 记录,生成文件码并通知用户。
    由 _process_pending_uploads 并发调用。
    """
    pend_id = row.get("id")
    uploader_id = row.get("uploader_id")
    channel_id = row.get("primary_channel_id")
    message_id = row.get("primary_channel_msg_id")
    file_types = row.get("file_types", {})
    logger.debug(f"[Idx][poll] raw file_types from CRDB: type={type(file_types).__name__}, value={file_types!r}")
    # _row_to_dict 已经保证 file_types 是 dict（空为 {}），此处仅做兜底
    if isinstance(file_types, str):
        try:
            file_types = json.loads(file_types)
        except (json.JSONDecodeError, TypeError):
            file_types = {}
    if not isinstance(file_types, dict):
        logger.warning(f"[Idx][poll] file_types 类型异常，重置为空: type={type(file_types).__name__}")
        file_types = {}
    # 如果 file_types 为空（Up Bot context.user_data 丢失），从 batch_file_meta 推断
    if not file_types:
        batch_meta_raw = row.get("batch_file_meta", [])
        logger.debug(f"[Idx][poll] file_types 为空，尝试从 batch_file_meta 推断: type={type(batch_meta_raw).__name__}, value={batch_meta_raw!r}")
        if isinstance(batch_meta_raw, str) and batch_meta_raw:
            try:
                meta_list = json.loads(batch_meta_raw)
                if isinstance(meta_list, list):
                    for m in meta_list:
                        if isinstance(m, dict) and "type" in m:
                            file_types[m["type"]] = file_types.get(m["type"], 0) + 1
                logger.debug(f"[Idx][poll] 从 batch_file_meta(str) 推断 file_types: {file_types}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[Idx][poll] batch_file_meta JSON解析失败: {e}")
        elif isinstance(batch_meta_raw, list):
            for m in batch_meta_raw:
                if isinstance(m, dict) and "type" in m:
                    file_types[m["type"]] = file_types.get(m["type"], 0) + 1
            logger.debug(f"[Idx][poll] 从 batch_file_meta(list) 推断 file_types: {file_types}")
        else:
            logger.warning(f"[Idx][poll] batch_file_meta 无法用于推断: type={type(batch_meta_raw).__name__}")
    logger.debug(f"[Idx][poll] 最终 file_types 用于生成文件码: {file_types}")
    batch_msg_ids_str = row.get("batch_msg_ids", "")
    batch_file_meta_raw = row.get("batch_file_meta", "")
    # 如果 _row_to_dict 返回了 list，序列化为 JSON 字符串用于存储
    if isinstance(batch_file_meta_raw, list):
        result = json.dumps(batch_file_meta_raw) if batch_file_meta_raw else ""
        if isinstance(result, bytes):
            result = result.decode()
        batch_file_meta_str = result
    elif isinstance(batch_file_meta_raw, bytes):
        batch_file_meta_str = batch_file_meta_raw.decode()
    else:
        batch_file_meta_str = batch_file_meta_raw if batch_file_meta_raw else ""
    note = row.get("note", "")
    protect_content = row.get("protect_content", settings.DEFAULT_PROTECT_CONTENT)
    file_ttl_days = row.get("file_ttl_days", settings.DEFAULT_FILE_TTL_DAYS)

    pending_col = get_pending_uploads_col()

    if not uploader_id or not channel_id or not message_id:
        await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
        return

    try:
        file_code = await generate_unique_code(file_types)
    except Exception as e:
        logger.error(f"[Idx][poll] 生成文件码失败(uploader={uploader_id}): {e}")
        try:
            await safe_send_message(app.bot, chat_id=uploader_id, text="文件处理失败，请稍后重试或联系管理员。")
        except Exception as e:
            logger.warning(f"[Idx] 通知用户处理失败: {e}")
            pass
        await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
        return

    # 写入 file_records
    try:
        files_col = get_file_records_col()
        record = make_file_record(
            file_code=file_code,
            uploader_id=uploader_id,
            primary_channel_id=channel_id,
            primary_channel_msg_id=message_id,
            file_types=file_types,
            batch_msg_ids=batch_msg_ids_str,
            batch_file_meta=batch_file_meta_str,
            note=note,
            protect_content=protect_content,
            file_ttl_days=file_ttl_days,
        )
        await files_col.insert_one(record)
        # 同步写入 SQLite 本地缓存（0 CRDB RU 后续读取）
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().upsert_file_record_local(record, mark_dirty=False)
        except Exception as cache_err:
            logger.debug(f"[Idx][poll] upsert_file_record_local 失败 code={file_code}: {cache_err}")
    except Exception as e:
        logger.error(f"[Idx][poll] DB写入失败 (code={file_code}): {e}")
        try:
            await safe_send_message(app.bot, chat_id=uploader_id, text="文件处理失败，请稍后重试")
        except Exception:
            pass
        await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
        return

    # 写入 codes 表（含 expire_time，省一次 UPDATE）
    try:
        actual_ttl_days = file_ttl_days if file_ttl_days else settings.DEFAULT_FILE_TTL_DAYS
        if actual_ttl_days == 0:
            # 0 = 永久有效，设置远期过期时间
            expire_dt = datetime.datetime(2099, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)
        else:
            expire_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=actual_ttl_days)
        codes_col = get_codes_col()
        ce = make_code_entry(
            code=file_code,
            uploader_id=uploader_id,
            file_types=file_types,
            batch_msg_ids=batch_msg_ids_str,
            batch_file_meta=batch_file_meta_str,
            primary_channel_id=channel_id,
            note=note,
            expire_time=expire_dt.isoformat(),
        )
        await codes_col.insert_one(ce)
        # 同步写入 SQLite 本地缓存
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().upsert_code_local(ce, mark_dirty=False)
        except Exception as cache_err:
            logger.debug(f"[Idx][poll] upsert_code_local 失败 code={file_code}: {cache_err}")
        # 同时写入 code_cache,后续解码查缓存即
        from database.cache import get_code_cache
        get_code_cache().set(f"code:{file_code}", ce)
        # E2: 递增用户码计数
        try:
            from utils.shared_counters import incr_user_code_count
            incr_user_code_count(uploader_id, 1)
        except Exception as counter_err:
            logger.debug(f"[Idx][poll] incr_user_code_count 失败 user={uploader_id}: {counter_err}")
    except Exception as e:
        logger.error(f"[Idx][poll] codes表写入失败(code={file_code}): {e}")

    # ── 外部文件:写入外部码映射 ──
    ext_code = None
    if note:
        try:
            note_parsed = json.loads(note)
            if isinstance(note_parsed, dict) and note_parsed.get("type") == "external":
                ext_code = note_parsed.get("code", "")
        except (json.JSONDecodeError, TypeError):
            pass
    if ext_code:
        try:
            from database import set_external_code_mapping
            await set_external_code_mapping(ext_code, file_code, bot_username="")
            logger.info(f"[Idx][poll] 外部码映射已写入: {ext_code} {file_code}")
        except Exception as e:
            logger.error(f"[Idx][poll] 外部码映射写入失败(code={ext_code}): {e}")

    # 通知上传者（总是尝试发送，失败则暂存等 /start 后补发）
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if ext_code:
            msg_text = f"外部文件 {ext_code} 已就绪，请重新发送文件码即可查收。"
        else:
            note_line = f"备注：{note}" if note else ""
            msg_text = (f"文件码：{file_code}\n"
                     f"{note_line}\n\n"
                     f"📤 发送文件 @{settings.UPLOAD_BOT_USERNAME}\n"
                     f"🔍 收码解码 @{settings.DECODER_BOT_USERNAME}\n"
                     f"📥 收取文件 @{settings.SENDER_BOT_USERNAME}")
        try:
            await safe_send_message(app.bot, chat_id=uploader_id, text=msg_text)
            logger.info(f"[Idx][poll] 文件码已发送给用户 {uploader_id}: {file_code}")
        except Exception as send_err:
            # 发送失败（用户未 /start idx），暂存等 /start 后补发
            await store.add_pending_file_code(uploader_id, file_code, note, ext_code or "")
            logger.info(f"[Idx][poll] 用户 {uploader_id} 未 /start idx，文件码 {file_code} 已暂存: {send_err}")
    except Exception as e:
        logger.error(f"[Idx][poll] 发送文件码失败 (code={file_code}): {e}")

    await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
    metrics.decode_count += 1
    await metrics.record_processed("idx_bot")


async def _process_pending_uploads(app: Application):
    """处理 pending_uploads: 先查本地 SQLite 通知(0 RU),有信号才查 CRDB,并发处理多条。
    S-2: 用 CAS 原子认领，避免多 worker 同时处理同一 pending → 重复生成码。
    原子语义: UPDATE ... WHERE id=$1 AND processed=0 且仅当该行未被认领过。
    """
    from database.cache_store import get_cache_store
    store = get_cache_store()

    while True:
        try:
            if not await store.wait_for_new_upload(timeout=30.0):
                continue

            pending_col = get_pending_uploads_col()
            processed_any = False
            while True:
                # 先查询候选行
                candidates = await pending_col.find(
                    {"processed": 0},
                    limit=10,
                    projection=["id", "uploader_id", "primary_channel_id", "primary_channel_msg_id",
                                "file_types", "batch_msg_ids", "batch_file_meta", "note",
                                "protect_content", "file_ttl_days"],
                )

                if not candidates:
                    break

                processed_any = True
                tasks = []
                for row in candidates:
                    pend_id = row["id"]
                    # S-2: CAS 原子认领：仅当 processed 仍为 0 时标记为 1，只有一个 worker 能成功
                    result = await pending_col.update_one(
                        {"id": pend_id, "processed": 0},
                        {"$set": {"processed": 1}}
                    )
                    # 认领成功才处理
                    if result and result.matched_count > 0:
                        tasks.append(asyncio.create_task(_process_one_pending(app, row)))

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            if not processed_any:
                await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"[Idx][poll] pending_uploads 轮询异常: {e}")
            await asyncio.sleep(5)


# ─── 内部码解───

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    raw_text = context.user_data.pop("_override_text", None) or update.message.text.strip()
    print(f"[DEBUG handle_code] raw_text={raw_text!r}", flush=True)

    # 1. 先检查消息中是否包含内部文件码（最高优先级）
    # 只要消息中出现 FILE_CODE_PREFIX，后面无论是什么，均视为内部码
    prefix = settings.FILE_CODE_PREFIX
    internal_match = re.search(r'(' + re.escape(prefix) + r'\S+)', raw_text)
    print(f"[DEBUG handle_code] prefix={prefix!r}, internal_match={internal_match.group(1)!r if internal_match else None}", flush=True)

    if internal_match:
        # 内部码：优先走本地解码
        text = internal_match.group(1)
        print(f"[DEBUG handle_code] extracted internal code={text!r}", flush=True)
        is_external = False
    else:
        # 2. 没有内部码 → 提取 bot 用户名走第三方码
        code, bot_username = extract_code_and_bot_from_message(raw_text)
        if bot_username:
            text = code
            is_external = True
        else:
            await safe_reply_text(update.message, "消息格式不正确，请发送文件码或包含 bot 用户名的消息")
            return

    if not await global_rate_limiter.acquire():
        await safe_reply_text(update.message, "系统繁忙,请稍后重试")
        return
    if not await user_rate_limiter.acquire(user.id):
        await safe_reply_text(update.message, "操作过于频繁,请稍后重试")
        return

    # ── 动态限速：根据 jobs 队列长度自动调节 ──
    await dynamic_rate_limiter.acquire(get_pending_jobs_count_local)

    await get_or_create_user(user.id, username=user.username, first_name=user.first_name)

    if is_external:
        # 第三方码：走中继或目标 bot
        await handle_external_code(update, context, user.id, text, bot_username, result=None)
        return

    # 内部码：查 codes 表
    result = await check_decode_permission(user.id, text)

    if not result.allowed:
        await safe_reply_text(update.message, result.reason)
        return

    file_record = result.file_record

    # ── 举报拦截：脱钩或限制举报人 ──
    if file_record and file_record.get("status") == "detached":
        # 配额已在 check_decode_permission 中预扣,此处拦截需回滚
        if result.quota_consumed:
            from services.permission import refund_user_quota
            await refund_user_quota(user.id, is_external=False)
        await safe_reply_text(update.message, "文件不存在或已被删除")
        return
    blocked = file_record.get("blocked_users")
    if isinstance(blocked, list) and user.id in blocked:
        # 同上,回滚预扣
        if result.quota_consumed:
            from services.permission import refund_user_quota
            await refund_user_quota(user.id, is_external=False)
        await safe_reply_text(update.message, "文件不存在或已被删除")
        return

    storage_channel = file_record.get("primary_channel_id") or await _get_storage_channel()

    try:
        # 写入本地 SQLite 缓冲(0 RU),后台6 小时 flush CRDB
        from database.cache_store import get_decode_log_buffer
        log_doc = make_decode_log(file_code=text, requester_id=user.id, status="queued")
        await get_decode_log_buffer().insert(log_doc)
    except Exception as e:
        logger.error(f"[Idx][handle_code] 解码日志缓冲写入失败: {e}")

    batch_ids_str = file_record.get("batch_msg_ids") or ""
    if not isinstance(batch_ids_str, str):
        batch_ids_str = str(batch_ids_str)
    msg_ids = []
    if batch_ids_str:
        msg_ids = [int(mid) for mid in batch_ids_str.split(",") if mid.strip().isdigit()]
    if not msg_ids:
        primary_mid = file_record.get("primary_channel_msg_id")
        if primary_mid is None:
            logger.error(f"[Idx] primary_channel_msg_id 为空，无法发送: code={text}")
            if result.quota_consumed:
                from services.permission import refund_user_quota
                await refund_user_quota(user.id, is_external=False)
            await safe_reply_text(update.message, "文件记录异常，请联系管理员")
            return
        msg_ids = [primary_mid]

    batch_file_meta_str = file_record.get("batch_file_meta") or ""
    protect_content = file_record.get("protect_content", False)

    try:
        await _dispatch_to_dsp(user.id, text, storage_channel, msg_ids, batch_file_meta_str, protect_content)
    except Exception as e:
        logger.error(f"[Idx][handle_code] jobs 失败 (user={user.id}, code={text}): {e}")
        # 投递失败,回滚预扣配额
        if result.quota_consumed:
            from services.permission import refund_user_quota
            await refund_user_quota(user.id, is_external=False)
        await safe_reply_text(update.message, "系统繁忙，文件发送请求失败，请稍后重试")
        return

    # 配额已在 check_decode_permission 中预扣(原子条件递增),投递成功无需再递增

    await safe_reply_text(
        update.message,
        f"文件将由 @{settings.SENDER_BOT_USERNAME} 发送给你请查收。",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ 举报", callback_data=f"report_req|{text}")
        ]])
    )

    metrics.decode_count += 1
    await metrics.record_processed("idx_bot")
    logger.info(f"[Idx][handle_code] 用户 {user.id} 请求文件{text}")


# ─── 举报回调 ───

_report_debounce: dict[str, float] = {}

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户点击举报按钮，推送消息给管理员"""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("report_req|"):
        return

    file_code = data.split("|", 1)[1]
    reporter = update.effective_user
    if not reporter:
        return

    # 60 秒防抖
    key = f"{reporter.id}:{file_code}"
    now = time.time()
    # N-L9: 同时清理过期条目（>120s），防止字典无限增长
    stale = [k for k, v in _report_debounce.items() if now - v > 120]
    for k in stale:
        _report_debounce.pop(k, None)
    if key in _report_debounce and now - _report_debounce[key] < 60:
        await query.answer("已提交举报，请勿重复操作", show_alert=True)
        return
    _report_debounce[key] = now

    # 查文件记录获取上传者
    try:
        file_record = await get_file_record_cached(file_code)
        if not file_record:
            await query.answer("文件记录不存在", show_alert=True)
            return
    except Exception as e:
        logger.error(f"[Idx][report] 查询文件失败: {e}")
        await query.answer("系统繁忙，请稍后重试", show_alert=True)
        return

    uploader_id = file_record.get("uploader_id", 0)
    reporter_username = f"@{reporter.username}" if reporter.username else str(reporter.id)

    report_text = (
        f"🚨 文件举报\n\n"
        f"📁 文件码: {file_code}\n"
        f"👤 上传者: {uploader_id}\n"
        f"👤 举报人: {reporter.id} ({reporter_username})\n"
        f"📋 来源: Idx Bot\n"
        f"⏰ 时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 封禁上传者", callback_data=f"report:ban|{uploader_id}")],
        [InlineKeyboardButton("🔗 脱钩文件码", callback_data=f"report:detach|{file_code}")],
        [InlineKeyboardButton("🚫 限制举报人", callback_data=f"report:block|{file_code}|{reporter.id}")],
        [InlineKeyboardButton("✅ 忽略", callback_data="report:ignore")],
    ])

    # 通过 Admin Bot 发送，确保操作按钮回调能回到 Admin Bot 处理
    from utils.admin_notify import send_to_admin
    try:
        await send_to_admin(report_text, keyboard)
        await query.answer("举报已提交，管理员将尽快处理", show_alert=True)
    except Exception as e:
        logger.error(f"[Idx][report] 推送管理员失败: {e}")
        await query.answer("举报提交失败，请稍后重试", show_alert=True)


# ─── 用户文件码管理 ───────────────────────────────────────────────

_PAGE_SIZE = 12  # 每页 12 条


def _format_code_status(code_entry: dict) -> str:
    """格式化文件码状态摘要"""
    status = code_entry.get("status", "active")
    if status == "offline":
        status_icon = "🚫"
        status_text = "已下架"
    elif status == "expired":
        status_icon = "⏰"
        status_text = "已过期"
    else:
        status_icon = "✅"
        status_text = "正常"

    note = code_entry.get("note", "")
    expire_time = code_entry.get("expire_time", "")

    parts = [f"   状态: {status_icon} {status_text}"]
    if note:
        # 截断过长的备注
        display_note = note[:20] + ("..." if len(note) > 20 else "")
        parts.append(f"备注: {display_note}")
    if expire_time:
        try:
            exp_dt = datetime.datetime.fromisoformat(expire_time)
            parts.append(f"到期: {exp_dt.strftime('%Y-%m-%d')}")
        except (ValueError, TypeError):
            pass

    return "\n".join(parts)


async def my_codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出用户所有文件码（分页）
    E2: 计数走本地缓存; E7: 列表查询走内存缓存
    """
    user = update.effective_user
    if not user:
        return

    # 从 page 参数获取页码
    page = 1
    if context.args and context.args[0].isdigit():
        page = max(1, int(context.args[0]))

    # E2: 用户码计数走本地缓存(0 RU)
    from utils.shared_counters import get_user_code_count
    from database.cache import get_user_codes_cache

    total_rows = get_user_code_count(user.id)
    if total_rows <= 0:
        # 首次访问,按需同步基线
        codes_col = get_codes_col()
        total_rows = await codes_col.count_documents({"uploader_id": user.id})
        if total_rows == 0:
            await safe_reply_text(update.message, "您还没有上传过文件码。")
            return
        # N-M15: 将基线传入 get_user_code_count，确保后续调用使用正确基线
        total_rows = get_user_code_count(user.id, base=total_rows)

    total_pages = max(1, (total_rows + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    skip = (page - 1) * _PAGE_SIZE

    # E7: 列表查询走内存缓存(5 分钟 TTL)
    codes_cache = get_user_codes_cache()
    cache_key = f"user_codes:{user.id}:{page}"
    cached = codes_cache.get(cache_key)
    if cached is not None:
        rows = cached
    else:
        codes_col = get_codes_col()
        rows = await codes_col.find(
            {"uploader_id": user.id},
            sort=("created_at", -1),
            skip=skip,
            limit=_PAGE_SIZE,
        )
        rows = list(rows)
        codes_cache.set(cache_key, rows)

    items = []
    for i, row in enumerate(rows, 1):
        code = row.get("code", "")
        detail = _format_code_status(row)
        items.append(f"{i}. {code}\n{detail}")

    header = f"📋 我的文件码（共 {total_rows} 个，显示 {skip + 1}-{min(skip + _PAGE_SIZE, total_rows)}）"
    page_info = f"\n\n共 {total_pages} 页，当前第 {page} 页"

    # 构建内联键盘
    kb = []
    if page > 1:
        kb.append(InlineKeyboardButton("⬅ 上一页", callback_data=f"mycode:page|{page - 1}"))
    if page < total_pages:
        kb.append(InlineKeyboardButton("下一页 ➡", callback_data=f"mycode:page|{page + 1}"))

    reply = header + "\n\n" + "\n\n".join(items) + page_info
    keyboard = InlineKeyboardMarkup([kb]) if kb else None

    await update.message.reply_text(reply, reply_markup=keyboard)


async def my_code_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """翻页回调"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    if not data.startswith("mycode:page|"):
        return

    try:
        page = int(data.split("|")[1])
    except (ValueError, IndexError):
        return

    # 复用 my_codes_command 逻辑
    context.args = [str(page)]
    await my_codes_command(update, context)


async def my_code_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看文件码详情"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:detail|"):
        return

    code = data.split("|", 1)[1]

    # 权限校验：只能查看自己的文件码
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        # 静默：不透露该码是否存在
        await query.edit_message_text("操作失败")
        return

    # 构建详情
    status = code_entry.get("status", "active")
    if status == "offline":
        status_text = "🚫 已下架"
    elif status == "expired":
        status_text = "⏰ 已过期"
    else:
        status_text = "✅ 正常"

    note = code_entry.get("note", "")
    expire_time = code_entry.get("expire_time", "")
    created_at = code_entry.get("created_at", "")
    file_types_raw = code_entry.get("file_types", "{}")

    # 解析 file_types
    try:
        ft = json.loads(file_types_raw) if isinstance(file_types_raw, str) else file_types_raw
        type_parts = [f"{v}{k}" for k, v in ft.items()] if isinstance(ft, dict) else []
        type_text = ", ".join(type_parts) if type_parts else "未知"
    except (json.JSONDecodeError, TypeError):
        type_text = "未知"

    detail_lines = [
        "📋 文件码详情",
        "",
        f"码: {code}",
        f"状态: {status_text}",
    ]
    if note:
        detail_lines.append(f"备注: {note}")
    if expire_time:
        try:
            exp_dt = datetime.datetime.fromisoformat(expire_time)
            detail_lines.append(f"有效期: {exp_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except (ValueError, TypeError):
            detail_lines.append(f"有效期: {expire_time}")
    if created_at:
        try:
            cr_dt = datetime.datetime.fromisoformat(created_at)
            detail_lines.append(f"上传时间: {cr_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except (ValueError, TypeError):
            detail_lines.append(f"上传时间: {created_at}")
    detail_lines.append(f"文件类型: {type_text}")

    # 构建操作按钮
    kb = [
        [
            InlineKeyboardButton("✏️ 修改备注", callback_data=f"mycode:edit_note|{code}"),
            InlineKeyboardButton("⏰ 设置有效期", callback_data=f"mycode:set_expiry|{code}"),
        ],
    ]
    if status == "offline":
        kb.append([InlineKeyboardButton("✅ 恢复上架", callback_data=f"mycode:toggle_status|{code}|active")])
    else:
        kb.append([InlineKeyboardButton("🚫 立刻下架", callback_data=f"mycode:toggle_status|{code}|offline")])

    kb.extend([
        [InlineKeyboardButton("📊 查看统计", callback_data=f"mycode:stats|{code}")],
        [InlineKeyboardButton("🔙 返回列表", callback_data="mycode:list")],
    ])

    await query.edit_message_text("\n".join(detail_lines), reply_markup=InlineKeyboardMarkup(kb))


async def my_code_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """返回列表"""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    # 复用 my_codes_command
    context.args = ["1"]
    await my_codes_command(update, context)


async def my_code_edit_note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """修改备注"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:edit_note|"):
        return

    code = data.split("|", 1)[1]

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    # 保存当前 code 到用户上下文，等待输入
    context.user_data["_manage_code"] = code
    context.user_data["_manage_action"] = "edit_note"

    old_note = code_entry.get("note", "")
    await query.edit_message_text(
        f"📝 修改备注\n\n"
        f"当前备注: {old_note if old_note else '(空)'}\n\n"
        f"请输入新备注（发送 /cancel 取消）",
    )


async def my_code_set_expiry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置有效期"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:set_expiry|"):
        return

    code = data.split("|", 1)[1]

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    context.user_data["_manage_code"] = code
    context.user_data["_manage_action"] = "set_expiry"

    expire_time = code_entry.get("expire_time", "")
    if expire_time:
        try:
            exp_dt = datetime.datetime.fromisoformat(expire_time)
            expire_text = exp_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            expire_text = expire_time
    else:
        expire_text = "永久"

    kb = [
        [
            InlineKeyboardButton("1天", callback_data=f"mycode:expiry_pick|{code}|1"),
            InlineKeyboardButton("7天", callback_data=f"mycode:expiry_pick|{code}|7"),
            InlineKeyboardButton("30天", callback_data=f"mycode:expiry_pick|{code}|30"),
        ],
        [
            InlineKeyboardButton("90天", callback_data=f"mycode:expiry_pick|{code}|90"),
            InlineKeyboardButton("自定义", callback_data=f"mycode:expiry_custom|{code}"),
            InlineKeyboardButton("永久", callback_data=f"mycode:expiry_pick|{code}|0"),
        ],
        [InlineKeyboardButton("🔙 返回", callback_data=f"mycode:detail|{code}")],
    ]

    await query.edit_message_text(
        f"⏰ 设置有效期\n\n"
        f"当前有效期: {expire_text}\n\n"
        f"请选择新有效期：",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def my_code_expiry_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """快捷选择有效期"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:expiry_pick|"):
        return

    parts = data.split("|")
    code = parts[1]
    days = int(parts[2])

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    # 计算新过期时间
    if days == 0:
        new_expire = None  # 永久
    else:
        new_expire = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()

    # 写入缓冲
    from database.cache_store import get_code_change_buffer
    await get_code_change_buffer().insert(code, "expiry", new_expire or "NULL", user.id)

    # 更新本地缓存:直接 invalidate,下次查询从 CRDB/SQLite 重新加载
    from database.cache import invalidate_code_entry
    # J: 同步删除 SQLite 持久化缓存
    invalidate_code_entry(code)
    # E7: 失效用户码列表缓存
    from database.cache import invalidate_user_codes
    invalidate_user_codes(user.id)

    await query.edit_message_text(
        f"✅ 有效期已设置为 {days} 天后\n\n"
        f"[{code}]"
    )


async def my_code_expiry_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """自定义有效期"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    parts = data.split("|")
    code = parts[1]

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code})
    if not code_entry or code_entry.get("uploader_id") != (update.effective_user and update.effective_user.id):
        await query.edit_message_text("操作失败")
        return

    context.user_data["_manage_code"] = code
    context.user_data["_manage_action"] = "set_expiry_custom"

    await query.edit_message_text(
        "请输入自定义有效期，格式：\n"
        "1. 天数，如 15（表示 15 天后过期）\n"
        "2. ISO 时间，如 2026-12-31T23:59:59\n"
        "3. 0 或 permanent 表示永久有效\n\n"
        "（发送 /cancel 取消）"
    )


async def my_code_toggle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """下架/恢复上架"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:toggle_status|"):
        return

    parts = data.split("|")
    code = parts[1]
    new_status = parts[2]

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    action_text = "下架" if new_status == "offline" else "恢复上架"

    # 二次确认
    kb = [
        [
            InlineKeyboardButton(f"确认{action_text}", callback_data=f"mycode:confirm_{new_status}|{code}"),
            InlineKeyboardButton("取消", callback_data=f"mycode:detail|{code}"),
        ]
    ]

    await query.edit_message_text(
        f"⚠️ 确认{action_text}\n\n"
        f"文件码: {code}\n"
        f"此操作后其他人将无法解码此文件。\n\n"
        f"确定要继续吗？",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def my_code_confirm_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """确认执行下架/恢复"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    # 格式: mycode:confirm_offline|CODE 或 mycode:confirm_active|CODE
    if not data.startswith("mycode:confirm_"):
        return

    parts = data.split("|")
    new_status = parts[0].replace("mycode:confirm_", "")
    code = parts[1]

    if new_status not in ("offline", "active"):
        await query.edit_message_text("操作失败")
        return

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    # 写入缓冲
    from database.cache_store import get_code_change_buffer
    await get_code_change_buffer().insert(code, "status", new_status, user.id)

    # 更新本地缓存:直接 invalidate,下次查询会从 CRDB/SQLite 重新加载最新状态
    # 避免直接突变 cache 私有属性破坏 QueryCache 封装(并发读可能读到半突变状态)
    from database.cache import invalidate_code_entry
    # J: 同步删除 SQLite 持久化缓存
    invalidate_code_entry(code)
    # F1: 下架/上架时同步 local_job_queue 状态
    if new_status == "offline":
        try:
            from utils.shared_counters import decr_user_code_count
            decr_user_code_count(user.id, 1)
        except Exception as counter_err:
            logger.debug(f"[Idx] decr_user_code_count 失败 user={user.id} code={code}: {counter_err}")
    else:
        # N-L8: 恢复上架时递增用户码计数，与下架对称
        try:
            from utils.shared_counters import incr_user_code_count
            incr_user_code_count(user.id, 1)
        except Exception as counter_err:
            logger.debug(f"[Idx] incr_user_code_count 失败 user={user.id} code={code}: {counter_err}")
    # E7: 失效用户码列表缓存
    from database.cache import invalidate_user_codes
    invalidate_user_codes(user.id)

    status_text = "下架" if new_status == "offline" else "恢复上架"
    await query.edit_message_text(f"✅ 已{status_text}文件码 [{code}]")


async def my_code_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看文件码统计"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data
    if not data.startswith("mycode:stats|"):
        return

    code = data.split("|", 1)[1]

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await query.edit_message_text("操作失败")
        return

    # 从 decode_log_buffer 查解码次数（SQLite 本地）
    from database.cache_store import get_decode_log_buffer
    buf = get_decode_log_buffer()
    decode_count = 0
    last_decode = ""
    if buf._db:
        try:
            row = await buf._db.execute_fetchall(
                "SELECT COUNT(*) as cnt, MAX(request_time) as last_time "
                "FROM decode_log_buffer WHERE file_code = ?",
                (code,),
            )
            if row:
                decode_count = row[0][0] if row[0][0] else 0
                last_decode = row[0][1] if row[0][1] else ""
        except Exception:
            pass

    # 也从 CRDB 查历史 decode_logs
    try:
        decode_logs_col = get_decode_logs_col()
        cr_count = await decode_logs_col.count_documents({"file_code": code})
        if cr_count > decode_count:
            decode_count = cr_count
    except Exception:
        pass

    stats_lines = [
        "📊 文件码统计",
        "",
        f"码: {code}",
        f"总解码次数: {decode_count}",
    ]
    if last_decode:
        try:
            dt = datetime.datetime.fromisoformat(last_decode)
            stats_lines.append(f"最近解码: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except (ValueError, TypeError):
            pass

    kb = [
        [InlineKeyboardButton("🔙 返回详情", callback_data=f"mycode:detail|{code}")],
    ]

    await query.edit_message_text("\n".join(stats_lines), reply_markup=InlineKeyboardMarkup(kb))


async def my_code_manage_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户输入的备注/有效期文本"""
    user = update.effective_user
    if not user:
        return

    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("_manage_code", None)
        context.user_data.pop("_manage_action", None)
        await update.message.reply_text("操作已取消")
        return

    action = context.user_data.get("_manage_action")
    code = context.user_data.get("_manage_code")

    if not action or not code:
        return

    # 权限校验
    codes_col = get_codes_col()
    code_entry = await codes_col.find_one({"code": code, "uploader_id": user.id})
    if not code_entry:
        await update.message.reply_text("操作失败")
        context.user_data.pop("_manage_code", None)
        context.user_data.pop("_manage_action", None)
        return

    from database.cache_store import get_code_change_buffer

    if action == "edit_note":
        await get_code_change_buffer().insert(code, "note", text, user.id)
        # 更新缓存:直接 invalidate,下次查询从 CRDB/SQLite 重新加载
        from database.cache import invalidate_code_entry
        # J: 同步删除 SQLite 持久化缓存
        invalidate_code_entry(code)
        # E7: 失效用户码列表缓存
        from database.cache import invalidate_user_codes
        invalidate_user_codes(user.id)
        await update.message.reply_text(f"✅ 备注已更新为: {text}")

    elif action == "set_expiry_custom":
        try:
            days_val = int(text)
            if days_val == 0:
                new_expire = None
            else:
                new_expire = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_val)).isoformat()
        except ValueError:
            # 尝试解析 ISO 时间
            try:
                exp_dt = datetime.datetime.fromisoformat(text)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
                new_expire = exp_dt.isoformat()
            except ValueError:
                await update.message.reply_text("格式不正确，请输入天数（如 15）或 ISO 时间（如 2026-12-31T23:59:59）")
                return

        if new_expire:
            await get_code_change_buffer().insert(code, "expiry", new_expire, user.id)
        else:
            await get_code_change_buffer().insert(code, "expiry", "NULL", user.id)

        # 更新缓存:直接 invalidate,下次查询从 CRDB/SQLite 重新加载
        from database.cache import invalidate_code_entry
        # J: 同步删除 SQLite 持久化缓存
        invalidate_code_entry(code)
        # E7: 失效用户码列表缓存
        from database.cache import invalidate_user_codes
        invalidate_user_codes(user.id)

        await update.message.reply_text("✅ 有效期已更新")

    context.user_data.pop("_manage_code", None)
    context.user_data.pop("_manage_action", None)


async def my_code_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消操作"""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.pop("_manage_code", None)
    context.user_data.pop("_manage_action", None)
    await query.edit_message_text("操作已取消")


# ─── 外部码处───

def _resolve_bot_username(update: Update) -> str | None:
    if update.effective_chat and update.effective_chat.username:
        return update.effective_chat.username
    msg = update.message
    if msg and msg.forward_origin and hasattr(msg.forward_origin, "sender_user"):
        sender = msg.forward_origin.sender_user
        if sender and sender.username:
            return sender.username
    if msg and hasattr(msg, "forward_from") and msg.forward_from:
        return msg.forward_from.username
    return None


async def handle_external_code(update, context, user_id, code, bot_username, result):
    if bot_username is None and result:
        bot_username = result.external_bot_username

    # ── 配额检查（仅检查不递增，投递成功后再递增）──
    if result is None:
        from services.permission import check_decode_permission
        result = await check_decode_permission(user_id, code)
        if not result.allowed:
            await safe_reply_text(update.message, result.reason)
            return

    original_code = context.user_data.pop("_original_external_code", None)
    if original_code:
        code = original_code
        bot_username = context.user_data.pop("_extracted_bot", bot_username)
        create_safe_task(save_code_bot_mapping(code, bot_username), name="save_code_bot_mapping")

    # ── 检查外部码映射:如果有系统码映射,直接走本地解码流──
    system_code = await get_system_code_for_external(code)
    if system_code:
        logger.info(f"[Idx][external] 外部{code} 命中映射 系统{system_code}")
        # A2: 走缓存,避免每次直查 CRDB
        file_record = await get_file_record_cached(system_code)
        if file_record:
            storage_channel = file_record.get("primary_channel_id") or await _get_storage_channel()
            batch_ids_str = file_record.get("batch_msg_ids") or ""
            if not isinstance(batch_ids_str, str):
                batch_ids_str = str(batch_ids_str)
            msg_ids = [int(mid) for mid in batch_ids_str.split(",") if mid.strip().isdigit()]
            if not msg_ids:
                msg_ids = [file_record.get("primary_channel_msg_id")]

            batch_file_meta_str = file_record.get("batch_file_meta") or ""
            protect_content = file_record.get("protect_content", False)
            try:
                await _dispatch_to_dsp(user_id, system_code, storage_channel, msg_ids, batch_file_meta_str, protect_content)
                # 配额已在 check_decode_permission 中预扣,投递成功无需再递增
                await safe_reply_text(update.message,
                    f"文件 {code} 已缓存，正在发送，请查收。\n"
                    f"(系统 {system_code})"
                )
                metrics.decode_count += 1
                await metrics.record_processed("idx_bot")
                return
            except Exception as e:
                logger.error(f"[Idx][external] 映射调度失败 (ext={code}, sys={system_code}): {e}")
                # 投递失败,回滚预扣配额
                if result.quota_consumed:
                    from services.permission import refund_user_quota
                    await refund_user_quota(user_id, is_external=True)
                await safe_reply_text(update.message, "外部文件码发送失败，请稍后重试")
                return
        else:
            logger.warning(f"[Idx][external] 映射的系统码 {system_code} file_record,回退到外部查")

    actual_bot = await resolve_bot_for_code(code, bot_username)
    if actual_bot != bot_username:
        logger.info(f"[Idx][external] 路由覆盖: {code} @{bot_username}→@{actual_bot}")
        bot_username = actual_bot

    await _wait_bot_interval(bot_username)

    remaining_info = ""
    parts = []
    if result and result.remaining_quota >= 0:
        parts.append(f"总解码剩{result.remaining_quota}")
    if result and result.remaining_external_quota >= 0:
        parts.append(f"外部码剩{result.remaining_external_quota}")
    remaining_info = " | ".join(parts)

    # 从账号池获取最优中继账负载均衡)
    account = await relay_pool.get_best_account()

    if account:
        import time as _time
        start = _time.time()
        ok = False
        try:
            ok = await account.send_external_code(bot_username, code, user_id)
        except Exception as e:
            logger.warning(f"[Idx][external] 中继发送异常: {e}")
            ok = False
        finally:
            duration_ms = int((_time.time() - start) * 1000)
            # 无论成功/失败/异常,都释放账号,避免池泄漏
            await relay_pool.release_account(account, duration_ms)
        if ok:
            await _enqueue_external(bot_username, user_id, code)
            # 配额已在 check_decode_permission 中预扣,投递成功无需再递增
            await safe_reply_text(update.message, f"正在查询外部文件请稍候查收。\n{remaining_info}")
            metrics.decode_count += 1
            await metrics.record_processed("idx_bot")
            return
        # 中继返回 False（可能是账号忙/发送失败/码已映射本地缓存），
        # 在回退直接发送前重新查询一次系统码映射（可能刚被其他中继同步完成）
        try:
            system_code_retry = await get_system_code_for_external(code)
            if system_code_retry:
                file_record_retry = await get_file_record_cached(system_code_retry)
                if file_record_retry:
                    storage_channel_r = file_record_retry.get("primary_channel_id") or await _get_storage_channel()
                    batch_ids_str_r = file_record_retry.get("batch_msg_ids") or ""
                    if not isinstance(batch_ids_str_r, str):
                        batch_ids_str_r = str(batch_ids_str_r)
                    msg_ids_r = [int(mid) for mid in batch_ids_str_r.split(",") if mid.strip().isdigit()]
                    if not msg_ids_r:
                        msg_ids_r = [file_record_retry.get("primary_channel_msg_id")]
                    batch_file_meta_r = file_record_retry.get("batch_file_meta") or ""
                    protect_content_r = file_record_retry.get("protect_content", False)
                    await _dispatch_to_dsp(user_id, system_code_retry, storage_channel_r, msg_ids_r, batch_file_meta_r, protect_content_r)
                    await safe_reply_text(update.message,
                        f"文件 {code} 已缓存，正在发送，请查收。\n"
                        f"(系统 {system_code_retry})"
                    )
                    metrics.decode_count += 1
                    await metrics.record_processed("idx_bot")
                    return
        except Exception as e:
            logger.warning(f"[Idx][external] 中继返回False后重试系统码映射失败: {e}")
        # 中继发送失败,继续回退到直接发送;配额暂不回滚(后续路径可能成功)

    try:
        await safe_send_message(context.bot, chat_id=bot_username, text=code)
        await _enqueue_external(bot_username, user_id, code)
        # 配额已在 check_decode_permission 中预扣,投递成功无需再递增
        await safe_reply_text(update.message, f"正在查询外部文件请稍候查收。\n{remaining_info}")
        metrics.decode_count += 1
        await metrics.record_processed("idx_bot")
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"[Idx][external] 无法发送到 @{bot_username}: {err_msg}")
        # 所有投递路径均失败,回滚预扣配额
        if result.quota_consumed:
            from services.permission import refund_user_quota
            await refund_user_quota(user_id, is_external=True)
        if "chat not found" in err_msg.lower() or "nobody is using" in err_msg.lower():
            await safe_reply_text(update.message,
                f"机器人 @{bot_username} 未找到，请检查文件码中的机器人用户名是否正确"
            )
        else:
            await safe_reply_text(update.message, "外部码解码功能暂不可用，请联系管理员配置用户中继")


async def handle_external_file_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = _resolve_bot_username(update)
    if not bot_username:
        return
    target_user_id, code = await _dequeue_external(bot_username)
    if target_user_id is None:
        return

    # 边缘情况:第三方 bot 直接向 Idx Bot 回复文件
    # Telegram 不允许 Bot 间直接交互,此路径实际不可用
    # 外部码解码走中继系统(真实 Telegram 账号),中继完成后再上游通知 Idx Bot
    logger.warning(f"[Idx][ext_resp] 收到外部文件但无法处理(Bot 间无权限),code={code}, from=@{bot_username}")


async def handle_external_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = _resolve_bot_username(update)
    if not bot_username:
        return
    target_user_id, code = await _dequeue_external(bot_username)
    if target_user_id is None:
        return

    logger.warning(f"[Idx][ext_media] 收到外部媒体但无法处bot 间无权限),code={code}, from=@{bot_username}")


async def _handle_relay_file_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").strip()
    if not caption.startswith("RELAY_FILE:"):
        return False

    rest = caption[len("RELAY_FILE:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return False
    try:
        target_user_id = int(rest[:user_end])
    except ValueError:
        return False
    code_part = rest[user_end + 1:].split("\n\n", 1)[0].strip()

    try:
        storage_ids = _parse_storage_ids_from_caption(caption)
        if not storage_ids:
            return True

        # A2: 走缓存,避免每次直查 CRDB
        record = await get_file_record_cached(code_part)
        if not record:
            try:
                await safe_send_message(context.bot, chat_id=target_user_id, text=f"您请求的文件 {code_part} 发送失败，请稍后重试。")
            except Exception:
                pass
            return True

        storage_channel = record.get("primary_channel_id") or await _get_storage_channel()
        await _dispatch_to_dsp(target_user_id, code_part, storage_channel, list(storage_ids))

        logger.info(f"[Idx][relay_file] 已写 jobs: user {target_user_id}, code={code_part}")
    except Exception as e:
        logger.error(f"[Idx][relay_file] 处理失败 (code={code_part}): {e}")
        try:
            await safe_send_message(context.bot, chat_id=target_user_id, text=f"您请求的文件 {code_part} 发送失败，请稍后重试。")
        except Exception:
            pass
    return True


# ─── 消息路由 ───

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    # 优先检查是否有进行中的文件码管理操作
    if context.user_data.get("_manage_action"):
        await my_code_manage_text_handler(update, context)
        return

    if text.startswith(("RELAY_DELIVER:", "RELAY_RENEW:", "RELAY_ERROR:", "RELAY_BATCH:")):
        await handle_relay_delivery(update, context)
        return

    if is_valid_code_format(text.strip()):
        await handle_code(update, context)
        return

    code, bot_username = extract_code_and_bot_from_message(text)
    if code and bot_username:
        context.user_data["_original_external_code"] = code
        context.user_data["_extracted_bot"] = bot_username
        context.user_data["_override_text"] = f"{bot_username}:{code}"
        await handle_code(update, context)
        return

    clean_text = text.strip()
    if clean_text and len(clean_text) >= 4 and not re.search(r'[\u4e00-\u9fff]', clean_text):
        known_bot = await get_bot_for_code(clean_text)
        if known_bot:
            logger.info(f"[Idx] 命中无头码缓存: code={clean_text}, bot={known_bot}")
            context.user_data["_original_external_code"] = clean_text
            context.user_data["_extracted_bot"] = known_bot
            context.user_data["_override_text"] = f"{known_bot}:{clean_text}"
            await handle_code(update, context)
            return

        # ── 通配符前缀匹配：用 code_routes 中的前缀匹配未知码 ──
        from database import get_all_code_bot_routes
        routes = await get_all_code_bot_routes()
        if routes:
            best_prefix = ""
            best_bot = ""
            for prefix, bot_username in routes.items():
                if clean_text.startswith(prefix) and len(prefix) > len(best_prefix):
                    best_prefix = prefix
                    best_bot = bot_username
            if best_bot:
                logger.info(f"[Idx] 通配符匹配: code={clean_text}, prefix={best_prefix}, bot={best_bot}")
                context.user_data["_original_external_code"] = clean_text
                context.user_data["_extracted_bot"] = best_bot
                context.user_data["_override_text"] = f"{best_bot}:{clean_text}"
                create_safe_task(save_code_bot_mapping(clean_text, best_bot), name="save_code_bot_mapping")
                await handle_code(update, context)
                return


# ─── 运行 ───

async def _init():
    from database import init_db
    await init_db()


async def _async_main():
    await _init()
    from database.cache_store import report_bot_heartbeat
    await report_bot_heartbeat("idx_bot")

    logger.info("[Idx] 启动解码机器(Idx Bot)...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("my_codes", my_codes_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^report_req\|"))
    # 文件码管理回调
    app.add_handler(CallbackQueryHandler(my_code_page_callback, pattern=r"^mycode:page\|"))
    app.add_handler(CallbackQueryHandler(my_code_list_callback, pattern=r"^mycode:list"))
    app.add_handler(CallbackQueryHandler(my_code_detail_callback, pattern=r"^mycode:detail\|"))
    app.add_handler(CallbackQueryHandler(my_code_edit_note_callback, pattern=r"^mycode:edit_note\|"))
    app.add_handler(CallbackQueryHandler(my_code_set_expiry_callback, pattern=r"^mycode:set_expiry\|"))
    app.add_handler(CallbackQueryHandler(my_code_expiry_pick_callback, pattern=r"^mycode:expiry_pick\|"))
    app.add_handler(CallbackQueryHandler(my_code_expiry_custom_callback, pattern=r"^mycode:expiry_custom\|"))
    app.add_handler(CallbackQueryHandler(my_code_toggle_status_callback, pattern=r"^mycode:toggle_status\|"))
    app.add_handler(CallbackQueryHandler(my_code_confirm_status_callback, pattern=r"^mycode:confirm_"))
    app.add_handler(CallbackQueryHandler(my_code_stats_callback, pattern=r"^mycode:stats\|"))
    app.add_handler(CallbackQueryHandler(my_code_cancel_callback, pattern=r"^mycode:cancel"))

    media_filter = (
        filters.Document.ALL | filters.VIDEO | filters.PHOTO
        | filters.AUDIO | filters.VOICE | filters.ANIMATION
    )

    async def _route_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message is None:
            return
        try:
            mg_id = update.message.media_group_id
            if mg_id:
                caption = (update.message.caption or "").strip()
                if mg_id not in _media_group_buffer:
                    user_id = None
                    code = None
                    source = None
                    if caption.startswith("RELAY_FILE:"):
                        source = "relay"
                        parts = caption.split(":", 3)
                        if len(parts) >= 3:
                            try:
                                user_id = int(parts[1])
                            except ValueError:
                                pass
                            code = parts[2].split("\n")[0]
                    else:
                        source = "external"
                        bot_username = _resolve_bot_username(update)
                        if bot_username:
                            user_id, code = await _dequeue_external(bot_username)
                    if not user_id or not code:
                        return
                    timer = create_safe_task(_flush_media_group_buffer(mg_id), name=f"flush-mg-{mg_id}")
                    _media_group_buffer[mg_id] = {
                        "source": source, "bot": context.bot, "msgs": [],
                        "user_id": user_id, "code": code, "timer": timer,
                    }
                _media_group_buffer[mg_id]["msgs"].append(
                    (update.message.chat_id, update.message.message_id, caption)
                )
                return
            if await _handle_relay_file_media(update, context):
                return
            await handle_external_file_response(update, context)
        except Exception as e:
            logger.error(f"[Idx][_route_media] 异常: {e}")

    app.add_handler(MessageHandler(media_filter, _route_media))

    await metrics.ping_bot("idx_bot")

    async def health_ping():
        while True:
            await metrics.ping_bot("idx_bot")
            await report_bot_heartbeat("idx_bot")
            await asyncio.sleep(30)

    async def cleanup_loop():
        while True:
            await _cleanup_stale_pending()
            await _cleanup_media_groups()
            await asyncio.sleep(60)

    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(_process_pending_uploads(app), name="process-pending")
    create_safe_task(cleanup_loop(), name="cleanup")
    create_safe_task(relay_pool.start_all(), name="relay-start")
    from database.cache import dump_cache_to_disk_loop
    create_safe_task(dump_cache_to_disk_loop(), name="dump-cache")
    # Decode Logs 缓冲 flush 后台任务
    from database.cache import _flush_decode_log_buffer_loop
    create_safe_task(_flush_decode_log_buffer_loop(), name="flush-decode-logs")
    # request_count 批量 flush 后台任务
    from database.cache import _flush_request_count_loop
    create_safe_task(_flush_request_count_loop(), name="flush-request-count")
    # 热表增量同步：每 120 秒从 CRDB 拉取新记录到 SQLite
    from database.cache import _sync_local_tables_loop
    create_safe_task(_sync_local_tables_loop(), name="sync-local-tables")
    # Quota 同步后台任务
    create_safe_task(_quota_sync_loop(), name="quota-sync")
    # Active Channels 刷新后台任务
    async def _channel_refresh_loop():
        while True:
            await asyncio.sleep(60)
            await _refresh_active_channels()
    create_safe_task(_channel_refresh_loop(), name="channel-refresh")

    # 文件码变更批量 flush CRDB 后台任务
    async def _code_changes_sync_loop():
        """每 5 分钟将 SQLite code_changes 批量 flush 到 CRDB
        
        使用 CASE WHEN 批量 UPDATE，替代 N+1 循环，大幅减少 RU 消耗。
        """
        while True:
            await asyncio.sleep(300)
            try:
                from database.cache_store import get_code_change_buffer
                buf = get_code_change_buffer()
                changes = await buf.get_unsynced(limit=100)
                if not changes:
                    continue

                codes_col = get_codes_col()
                synced_ids = []
                
                # 按 change_type 分组，每组执行一条批量 SQL
                for ctype, field in (("note", "note"), ("expiry", "expire_time"), ("status", "status")):
                    group = [c for c in changes if c["change_type"] == ctype]
                    if not group:
                        continue
                    
                    params = []
                    cases = []
                    for c in group:
                        params.append(c["code"])
                        if ctype == "expiry":
                            val = None if c["new_value"] == "NULL" else c["new_value"]
                        else:
                            val = c["new_value"]
                        params.append(val)
                        cases.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
                    
                    placeholders = ", ".join([f"${i * 2 + 1}" for i in range(len(group))])
                    sql = (
                        f"UPDATE codes SET {field} = CASE code {' '.join(cases)} END "
                        f"WHERE code IN ({placeholders})"
                    )
                    try:
                        await codes_col.execute_raw(sql, params)
                        synced_ids.extend(c["id"] for c in group)
                    except Exception as e:
                        logger.error(f"[CodeChanges] batch flush failed (type={ctype}): {e}")

                if synced_ids:
                    await buf.mark_synced(synced_ids)
                    logger.info(f"[CodeChanges] flushed {len(synced_ids)} changes to CRDB")
            except Exception as e:
                logger.error(f"[CodeChanges] sync loop error: {e}")
    create_safe_task(_code_changes_sync_loop(), name="code-changes-sync")

    async with app:
        await app.start()
        await app.updater.start_polling()
        # 等待停止信号
        try:
            stop_event = asyncio.Event()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            # 关闭前同步配额到 CRDB
            from services.permission import sync_quotas_to_crdb
            await sync_quotas_to_crdb()
            await app.updater.stop()
            await app.stop()


def run():
    """启动 Idx Bot(使用 asyncio.run 标准模式)"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
