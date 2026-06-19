"""Up Bot — 上传机器人(环形冗余架构版)
职责:接收用户文件 → 轮转分发到活跃窗口内的 3 个 A 槽(round-robin)
"""

import asyncio
import datetime
try:
    import orjson as json
except ImportError:
    import json
from collections import defaultdict


def _json_dumps(obj, **kwargs):
    if isinstance(result := json.dumps(obj, **kwargs), bytes):
        return result.decode()
    return result

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from loguru import logger

from config import settings
from database import get_pending_uploads_col, get_active_cells
from database.cache_store import get_cache_store
from services.permission import check_upload_permission
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics
from utils.task_utils import create_safe_task
from utils.force_join import check_force_join, three_bot_reminder
from utils.flood_waiter import safe_copy_message, safe_send_message, safe_send_media_group, safe_reply_text
from utils.file_utils import detect_file_type, extract_file_meta

TOKEN = settings.UPLOAD_BOT_TOKEN

_pending_media_groups: dict[str, dict] = {}
_active_a_slots: list[dict] = []
_active_slot_index: int = 0
# 中继外部文件缓冲区:code → {user_id, msg_ids, files_meta, file_types, timer, flushed}
_external_buffers: dict[str, dict] = {}


async def _refresh_active_slots():
    """刷新当前 Active A 槽列表(从 cells 表读取)。"""
    global _active_a_slots
    try:
        _active_a_slots = await get_active_cells()
        logger.info(f"[Up] 刷新 Active 槽位: {len(_active_a_slots)} 个")
    except Exception as e:
        logger.error(f"[Up] 刷新槽位失败: {e}")


async def _get_upload_target_channel() -> int:
    """选择上传目标频道:在活跃频道间轮转(round-robin)。"""
    global _active_slot_index
    if not _active_a_slots:
        await _refresh_active_slots()
    if not _active_a_slots:
        return settings.STORAGE_CHANNEL_ID
    # 轮转:每次取下一个活跃频道
    idx = _active_slot_index % len(_active_a_slots)
    _active_slot_index += 1
    channel_id = _active_a_slots[idx]["channel_id"]
    logger.debug(f"[Up] 轮转分发 → 频道 {channel_id} (index={idx}/{len(_active_a_slots)})")
    return channel_id


# ─── 以下逻辑与原来基本相同,仅 channel 选择改为环形槽位 ───


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        "欢迎使用上传机器人。\n\n"
        "📤 **单次上传**:直接发送文件,立即生成文件码\n\n"
        "📦 **批次上传**(所有文件共用一个文件码):\n"
        "  /start_upload - 开始批次上传\n"
        "  发送多个文件...\n"
        "  /end_upload - 结束批次,生成文件码\n\n"
        "所有用户(含免费用户)均可上传文件。"
        + three_bot_reminder()
    )


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您被禁止使用上传功能。")
        return

    context.user_data["batch"] = {
        "file_types": defaultdict(int),
        "pinned_msg_ids": [],
        "files_meta": [],
        "note": "",
    }
    await update.message.reply_text(
        "📦 已进入批次上传模式,请发送文件。\n"
        "发送 /end_upload 结束并生成文件码。\n"
        "发送 /cancel_upload 取消本次上传。\n\n"
        "💬 可选:使用 /note 文字 为本次批次添加备注。"
    )


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        del context.user_data["batch"]
        await update.message.reply_text("批次上传已取消。")
    else:
        await update.message.reply_text("当前没有进行中的批次上传。")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置批次上传备注。"""
    if not await check_force_join(update, context):
        return
    batch = context.user_data.get("batch")
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传,请先使用 /start_upload 开始。")
        return
    note_text = " ".join(context.args) if context.args else ""
    if not note_text:
        await update.message.reply_text("用法:/note 备注内容\n例如:/note 这是张三的文件")
        return
    batch["note"] = note_text
    await update.message.reply_text(f"✅ 备注已设置:{note_text}")


async def end_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    batch = context.user_data.pop("batch", None)
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传,请先使用 /start_upload 开始。")
        return

    pending_mgids = list(_pending_media_groups.keys())
    for mgid in pending_mgids:
        grp = _pending_media_groups.get(mgid)
        if grp and grp.get("timer"):
            grp["timer"].cancel()
        await _flush_batch_media_group(mgid, context, batch)

    channel_msg_ids = batch["pinned_msg_ids"]

    if not channel_msg_ids:
        await update.message.reply_text("没有接收到任何文件,批次已取消。")
        return

    sent_msg = await update.message.reply_text(
        f"📦 {len(channel_msg_ids)} 个文件已接收,"
        f"文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您"
    )

    type_str = _json_dumps(dict(batch["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in channel_msg_ids)
    batch_file_meta_str = _json_dumps(batch["files_meta"])

    try:
        pending_col = get_pending_uploads_col()
        opts = context.user_data.pop("upload_options", {})
        await pending_col.insert_one({
            "uploader_id": user.id,
            "primary_channel_id": await _get_upload_target_channel(),
            "primary_channel_msg_id": channel_msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "note": batch.get("note", ""),
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
            "protect_content": opts.get("protect_content", "false") == "true" or settings.DEFAULT_PROTECT_CONTENT,
            "file_ttl_days": int(opts.get("file_ttl", settings.DEFAULT_FILE_TTL_DAYS)) or settings.DEFAULT_FILE_TTL_DAYS,
        })
        logger.info(f"[Up] 批次文件写入pending_uploads: user={user.id}")
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败,请稍后重试。")
        return

    metrics.upload_count += 1
    metrics.record_processed("up_bot")


async def _dispatch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    file_type = detect_file_type(update)

    if update.message.media_group_id:
        mgid = update.message.media_group_id
        if mgid not in _pending_media_groups:
            _pending_media_groups[mgid] = {
                "file_types": defaultdict(int),
                "updates": [],
                "timer": None,
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
        batch["files_meta"].append(extract_file_meta(update))
        try:
            target_ch = await _get_upload_target_channel()
            forwarded = await safe_copy_message(context.bot, target_ch, update.effective_chat.id, update.message.message_id)
            batch["pinned_msg_ids"].append(forwarded.message_id)
        except Exception as e:
            logger.error(f"[Up] 批次上传复制文件到存储频道失败: {e}")
        await update.message.reply_text(f"✅ 已接收:{file_type}")


async def _flush_batch_media_group(mgid: str, context: ContextTypes.DEFAULT_TYPE, batch: dict):
    grp = _pending_media_groups.pop(mgid, None)
    if grp is None:
        return
    file_types = grp["file_types"]
    for k, v in file_types.items():
        batch["file_types"][k] += v
    copied = 0
    target_ch = await _get_upload_target_channel()
    for up in grp["updates"]:
        try:
            forwarded = await safe_copy_message(context.bot, target_ch, up.effective_chat.id, up.message.message_id)
            batch["pinned_msg_ids"].append(forwarded.message_id)
            batch["files_meta"].append(extract_file_meta(up))
            copied += 1
        except Exception as e:
            logger.error(f"[Up] 批次媒体组复制文件到存储频道失败: {e}")
    first = grp["updates"][0]
    type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items()))
    await safe_send_message(context.bot, chat_id=first.effective_chat.id, text=f"✅ 已接收媒体组:{type_desc}({copied}个文件)")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    caption = update.message.caption or ""

    # ── 中继外部文件:中继账号转发的文件,走缓冲区 → 批量写入 pending_uploads ──
    if caption.startswith("EXTERNAL_RELAY:"):
        await _handle_external_relay_file(update, context)
        return

    if not global_rate_limiter.acquire():
        await update.message.reply_text("系统繁忙,请稍后重试。")
        return
    if not user_rate_limiter.acquire(user.id):
        await update.message.reply_text("操作过于频繁,请稍后重试。")
        return
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限。")
        return

    file_type = detect_file_type(update)
    file_types = {file_type: 1}
    await _process_upload(user.id, update, context, file_types)


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限。")
        return

    file_type = detect_file_type(update)

    if update.message.media_group_id not in _pending_media_groups:
        _pending_media_groups[update.message.media_group_id] = {
            "user_id": user.id,
            "file_types": defaultdict(int),
            "updates": [],
            "timer": None,
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
    group = _pending_media_groups.pop(media_group_id, None)
    if group is None:
        return

    user_id = group["user_id"]
    file_types = dict(group["file_types"])

    target_ch = await _get_upload_target_channel()
    all_mids = []
    all_meta = []
    for up in group["updates"]:
        try:
            forwarded = await safe_copy_message(context.bot, target_ch, up.effective_chat.id, up.message.message_id)
            all_mids.append(forwarded.message_id)
            all_meta.append(extract_file_meta(up))
        except Exception as e:
            logger.error(f"[Up] 媒体组复制文件到存储频道失败: {e}")

    if not all_mids:
        metrics.record_error("up_bot")
        try:
            await safe_send_message(context.bot, chat_id=user_id, text="文件处理失败,请稍后重试。")
        except Exception:
            pass
        return

    type_str = _json_dumps(file_types)
    batch_ids_str = ",".join(str(mid) for mid in all_mids)
    batch_file_meta_str = _json_dumps(all_meta)
    note = group["updates"][0].message.caption or ""

    sent_msg = await safe_send_message(
        context.bot, chat_id=user_id,
        text=f"文件已接收,文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您"
    )

    # 发送上传选项
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⚙️ 上传选项(可在发送文件码前修改):",
            reply_markup=_build_upload_options_keyboard(),
        )
    except Exception:
        pass

    try:
        pending_col = get_pending_uploads_col()
        opts = context.user_data.pop("upload_options", {})
        await pending_col.insert_one({
            "uploader_id": user_id,
            "primary_channel_id": target_ch,
            "primary_channel_msg_id": all_mids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "note": note,
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
            "protect_content": opts.get("protect_content", "false") == "true" or settings.DEFAULT_PROTECT_CONTENT,
            "file_ttl_days": int(opts.get("file_ttl", settings.DEFAULT_FILE_TTL_DAYS)) or settings.DEFAULT_FILE_TTL_DAYS,
        })
        logger.info(f"[Up] 媒体组文件写入pending_uploads: user={user_id}")
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        try:
            await safe_send_message(context.bot, chat_id=user_id, text="文件处理失败,请稍后重试。")
        except Exception:
            pass
        return

    metrics.upload_count += 1
    metrics.record_processed("up_bot")


async def _process_upload(
    user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, file_types: dict
):
    main_channel = await _get_upload_target_channel()
    try:
        forwarded = await safe_copy_message(context.bot, main_channel, update.effective_chat.id, update.message.message_id)
        channel_msg_id = forwarded.message_id
    except Exception as e:
        logger.error(f"[Up] 转发文件到存储频道失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败,请稍后重试。")
        return

    sent_msg = await update.message.reply_text(
        f"文件已接收,文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您"
    )

    # 发送上传选项
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⚙️ 上传选项(可在发送文件码前修改):",
            reply_markup=_build_upload_options_keyboard(),
        )
    except Exception:
        pass

    type_str = _json_dumps(file_types)
    note = update.message.caption or ""
    opts = context.user_data.pop("upload_options", {})

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user_id,
            "primary_channel_id": main_channel,
            "primary_channel_msg_id": channel_msg_id,
            "file_types": type_str,
            "batch_msg_ids": "",
            "batch_file_meta": _json_dumps([extract_file_meta(update)]),
            "note": note,
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
            "protect_content": opts.get("protect_content", "false") == "true" or settings.DEFAULT_PROTECT_CONTENT,
            "file_ttl_days": int(opts.get("file_ttl", settings.DEFAULT_FILE_TTL_DAYS)) or settings.DEFAULT_FILE_TTL_DAYS,
        })
        logger.info(f"[Up] 文件写入pending_uploads: user={user_id}")
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败,请稍后重试。")
        return

    metrics.upload_count += 1
    metrics.record_processed("up_bot")


# ─── 上传选项回调 ───

async def upload_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理上传选项按钮回调。"""
    query = update.callback_query
    await query.answer()
    data = query.data  # format: "option|key|value"
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "option":
        return

    user_id = query.from_user.id
    key = parts[1]
    value = parts[2]

    if "upload_options" not in context.user_data:
        context.user_data["upload_options"] = {}
    context.user_data["upload_options"][key] = value

    if key == "protect_content":
        label = "✅ 禁止转发" if value == "true" else "❌ 允许转发"
        await query.edit_message_text(f"已选择:{label}")
    elif key == "file_ttl":
        ttl_labels = {
            "0": "永久有效",
            "1": "1天",
            "7": "7天",
            "30": "30天",
            "90": "90天",
        }
        label = ttl_labels.get(value, f"{value}天")
        await query.edit_message_text(f"文件码有效期:{label}")


def _build_upload_options_keyboard():
    """构建上传选项按钮。"""
    keyboard = [
        [
            InlineKeyboardButton("🔒 禁止转发", callback_data="option|protect_content|true"),
            InlineKeyboardButton("↗️ 允许转发", callback_data="option|protect_content|false"),
        ],
        [
            InlineKeyboardButton("⏱ 永久有效", callback_data="option|file_ttl|0"),
            InlineKeyboardButton("1天", callback_data="option|file_ttl|1"),
        ],
        [
            InlineKeyboardButton("7天", callback_data="option|file_ttl|7"),
            InlineKeyboardButton("30天", callback_data="option|file_ttl|30"),
        ],
        [
            InlineKeyboardButton("90天", callback_data="option|file_ttl|90"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── 中继外部文件处理 ───

async def _handle_external_relay_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理中继账号转发到 Up Bot 的外部文件。
    格式:EXTERNAL_RELAY:{user_id}:{external_code}
    文件先 copy 到存储频道,积累后由 EXTERNAL_DONE 触发批量写入 pending_uploads。
    """
    caption = update.message.caption or ""
    rest = caption[len("EXTERNAL_RELAY:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return
    try:
        external_user_id = int(rest[:user_end])
    except ValueError:
        return
    external_code = rest[user_end + 1:].strip()

    target_ch = await _get_upload_target_channel()
    try:
        forwarded = await safe_copy_message(context.bot, target_ch, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error(f"[Up][ext_relay] copy 到存储频道失败 (code={external_code}): {e}")
        return

    file_type = detect_file_type(update)
    file_meta = extract_file_meta(update)

    if external_code not in _external_buffers:
        _external_buffers[external_code] = {
            "user_id": external_user_id,
            "msg_ids": [],
            "files_meta": [],
            "file_types": defaultdict(int),
            "flushed": False,
        }

    buf = _external_buffers[external_code]
    buf["msg_ids"].append(forwarded.message_id)
    buf["files_meta"].append(file_meta)
    buf["file_types"][file_type] += 1

    # 重置安全超时定时器
    if buf.get("timer"):
        buf["timer"].cancel()
    buf["timer"] = asyncio.get_running_loop().call_later(
        60, lambda: asyncio.ensure_future(_flush_external_buffer(external_code, safe_mode=True))
    )
    logger.debug(f"[Up][ext_relay] 外部文件已缓存 (code={external_code}), 共{len(buf['msg_ids'])}个文件")


async def _handle_external_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 EXTERNAL_DONE 信号:中继账号通知文件收集完毕,触发批量写入。"""
    text = update.message.text or ""
    if not text.startswith("EXTERNAL_DONE:"):
        return
    rest = text[len("EXTERNAL_DONE:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return
    try:
        external_user_id = int(rest[:user_end])
    except ValueError:
        return
    external_code = rest[user_end + 1:].strip()

    await _flush_external_buffer(external_code, safe_mode=False)


async def _flush_external_buffer(external_code: str, safe_mode: bool = False):
    """刷新外部文件缓冲区:写入 pending_uploads。
    如果 safe_mode=True 的 flush 已执行,EXTERNAL_DONE 到达时不应重复处理。
    """
    buf = _external_buffers.get(external_code)
    if buf is None:
        return

    # 防止竞态:safe_mode 的超时 flush 已执行后,EXTERNAL_DONE 不应重复处理
    if buf.get("flushed"):
        return

    # 标记为已处理,防止重复 flush
    buf["flushed"] = True
    _external_buffers.pop(external_code, None)

    if buf.get("timer"):
        buf["timer"].cancel()

    msg_ids = buf.get("msg_ids", [])
    if not msg_ids:
        logger.warning(f"[Up][ext_relay] 外部文件缓冲区为空,跳过 (code={external_code})")
        return

    target_ch = await _get_upload_target_channel()
    type_str = _json_dumps(dict(buf["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in msg_ids)
    batch_file_meta_str = _json_dumps(buf["files_meta"])
    note = _json_dumps({"type": "external", "code": external_code})

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": buf["user_id"],
            "primary_channel_id": target_ch,
            "primary_channel_msg_id": msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "note": note,
            "status_msg_id": 0,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
        })
        logger.info(f"[Up][ext_relay] 外部文件已写入pending_uploads: code={external_code}, {len(msg_ids)}个文件")
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.error(f"[Up][ext_relay] 写入pending_uploads失败 (code={external_code}): {e}")


async def _init():
    from database import init_db
    await init_db()
    await _refresh_active_slots()


async def _async_main():
    await _init()

    logger.info(f"[Up] 启动上传机器人 (Up Bot)...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_upload", start_upload))
    app.add_handler(CommandHandler("end_upload", end_upload))
    app.add_handler(CommandHandler("cancel_upload", cancel_upload))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CallbackQueryHandler(upload_option_callback, pattern=r"^option\|"))

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

    metrics.ping_bot("up_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("up_bot")
            await asyncio.sleep(30)

    async def slot_refresh_loop():
        while True:
            await _refresh_active_slots()
            await asyncio.sleep(60)

    loop = asyncio.get_running_loop()
    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(slot_refresh_loop(), name="slot-refresh")

    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            stop_event = asyncio.Event()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()


def run():
    """启动 Up Bot(使用 asyncio.run 标准模式)。"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()