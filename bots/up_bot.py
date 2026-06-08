"""Up Bot — 上传机器人（环形冗余架构版）
职责：预铺 A 槽 + 接收用户文件 → 转发到当前 Active A 槽
"""

import asyncio
import datetime
import json
from collections import defaultdict

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from loguru import logger

from config import settings
from database import get_pending_uploads_col, get_active_cells
from services.permission import check_upload_permission
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics
from utils.force_join import check_force_join, three_bot_reminder

TOKEN = settings.UPLOAD_BOT_TOKEN

_pending_media_groups: dict[str, dict] = {}
_active_a_slots: list[dict] = []


async def _refresh_active_slots():
    """刷新当前 Active A 槽列表（从 cells 表读取）。"""
    global _active_a_slots
    try:
        _active_a_slots = await get_active_cells()
        logger.info(f"[Up] 刷新 Active 槽位: {len(_active_a_slots)} 个")
    except Exception as e:
        logger.error(f"[Up] 刷新槽位失败: {e}")


async def _get_upload_target_channel() -> int:
    """选择上传目标频道：取第一个 active 槽位。"""
    if not _active_a_slots:
        await _refresh_active_slots()
    if _active_a_slots:
        return _active_a_slots[0]["channel_id"]
    # 兜底：使用 settings 中的默认存储频道
    return settings.STORAGE_CHANNEL_ID


# ─── 以下逻辑与原来基本相同，仅 channel 选择改为环形槽位 ───


def _detect_file_type(update: Update) -> str:
    if update.message.photo:
        return "photo"
    if update.message.video:
        return "video"
    if update.message.document:
        return "document"
    if update.message.audio:
        return "audio"
    if update.message.voice:
        return "audio"
    if update.message.animation:
        return "animation"
    return "document"


def _extract_file_meta(update: Update) -> dict:
    msg = update.message
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id}
    if msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id}
    if msg.voice:
        return {"type": "audio", "file_id": msg.voice.file_id}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id}
    return {"type": "document", "file_id": ""}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await update.message.reply_text(
        "欢迎使用上传机器人。\n\n"
        "📤 **单次上传**：直接发送文件，立即生成文件码\n\n"
        "📦 **批次上传**（所有文件共用一个文件码）：\n"
        "  /start_upload - 开始批次上传\n"
        "  发送多个文件...\n"
        "  /end_upload - 结束批次，生成文件码\n\n"
        "所有用户（含免费用户）均可上传文件。"
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
    }
    await update.message.reply_text(
        "📦 已进入批次上传模式，请发送文件。\n"
        "发送 /end_upload 结束并生成文件码。\n"
        "发送 /cancel_upload 取消本次上传。"
    )


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        del context.user_data["batch"]
        await update.message.reply_text("批次上传已取消。")
    else:
        await update.message.reply_text("当前没有进行中的批次上传。")


async def end_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    batch = context.user_data.pop("batch", None)
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传，请先使用 /start_upload 开始。")
        return

    pending_mgids = list(_pending_media_groups.keys())
    for mgid in pending_mgids:
        grp = _pending_media_groups.get(mgid)
        if grp and grp.get("timer"):
            grp["timer"].cancel()
        await _flush_batch_media_group(mgid, context, batch)

    channel_msg_ids = batch["pinned_msg_ids"]

    if not channel_msg_ids:
        await update.message.reply_text("没有接收到任何文件，批次已取消。")
        return

    sent_msg = await update.message.reply_text(
        f"📦 {len(channel_msg_ids)} 个文件已接收，"
        f"文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您"
    )

    type_str = json.dumps(dict(batch["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in channel_msg_ids)
    batch_file_meta_str = json.dumps(batch["files_meta"])

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user.id,
            "primary_channel_id": await _get_upload_target_channel(),
            "primary_channel_msg_id": channel_msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
        })
        logger.info(f"[Up] 批次文件写入pending_uploads: user={user.id}")
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
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
    file_type = _detect_file_type(update)

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
        batch["files_meta"].append(_extract_file_meta(update))
        try:
            target_ch = await _get_upload_target_channel()
            forwarded = await update.message.copy(chat_id=target_ch)
            batch["pinned_msg_ids"].append(forwarded.message_id)
        except Exception as e:
            logger.error(f"[Up] 批次上传复制文件到存储频道失败: {e}")
        await update.message.reply_text(f"✅ 已接收：{file_type}")


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
            forwarded = await up.message.copy(chat_id=target_ch)
            batch["pinned_msg_ids"].append(forwarded.message_id)
            batch["files_meta"].append(_extract_file_meta(up))
            copied += 1
        except Exception as e:
            logger.error(f"[Up] 批次媒体组复制文件到存储频道失败: {e}")
    first = grp["updates"][0]
    type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items()))
    await context.bot.send_message(
        chat_id=first.effective_chat.id,
        text=f"✅ 已接收媒体组：{type_desc}（{copied}个文件）",
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not global_rate_limiter.acquire():
        await update.message.reply_text("系统繁忙，请稍后重试。")
        return
    if not user_rate_limiter.acquire(user.id):
        await update.message.reply_text("操作过于频繁，请稍后重试。")
        return
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限。")
        return

    file_type = _detect_file_type(update)
    file_types = {file_type: 1}
    await _process_upload(user.id, update, context, file_types)


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限。")
        return

    file_type = _detect_file_type(update)

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
            forwarded = await up.message.copy(chat_id=target_ch)
            all_mids.append(forwarded.message_id)
            all_meta.append(_extract_file_meta(up))
        except Exception as e:
            logger.error(f"[Up] 媒体组复制文件到存储频道失败: {e}")

    if not all_mids:
        metrics.record_error("up_bot")
        try:
            await context.bot.send_message(chat_id=user_id, text="文件处理失败，请稍后重试。")
        except Exception:
            pass
        return

    type_str = json.dumps(file_types)
    batch_ids_str = ",".join(str(mid) for mid in all_mids)
    batch_file_meta_str = json.dumps(all_meta)

    sent_msg = await context.bot.send_message(
        chat_id=user_id,
        text=f"文件已接收，文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您",
    )

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user_id,
            "primary_channel_id": target_ch,
            "primary_channel_msg_id": all_mids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
        })
        logger.info(f"[Up] 媒体组文件写入pending_uploads: user={user_id}")
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        try:
            await context.bot.send_message(chat_id=user_id, text="文件处理失败，请稍后重试。")
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
        forwarded = await update.message.copy(chat_id=main_channel)
        channel_msg_id = forwarded.message_id
    except Exception as e:
        logger.error(f"[Up] 转发文件到存储频道失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    sent_msg = await update.message.reply_text(
        f"文件已接收，文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给您"
    )

    type_str = json.dumps(file_types)

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user_id,
            "primary_channel_id": main_channel,
            "primary_channel_msg_id": channel_msg_id,
            "file_types": type_str,
            "batch_msg_ids": "",
            "batch_file_meta": "",
            "status_msg_id": sent_msg.message_id,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "processed": 0,
        })
        logger.info(f"[Up] 文件写入pending_uploads: user={user_id}")
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    metrics.upload_count += 1
    metrics.record_processed("up_bot")


async def _poll_code_sent(app: Application):
    from database.session import get_pending_uploads_col
    pending_col = get_pending_uploads_col()
    edited_ids = set()

    while True:
        try:
            rows = await pending_col.find({"processed": 1}, sort=("id", -1), limit=20)
            for row in rows:
                row_id = row.get("id")
                uploader_id = row.get("uploader_id")
                status_msg_id = row.get("status_msg_id")
                if not status_msg_id or row_id in edited_ids:
                    continue
                try:
                    await app.bot.edit_message_text(
                        chat_id=uploader_id,
                        message_id=status_msg_id,
                        text=f"✅ 文件码已由 @{settings.DECODER_BOT_USERNAME} 生成，请前往获取",
                    )
                    edited_ids.add(row_id)
                except Exception:
                    edited_ids.add(row_id)
        except Exception as e:
            logger.error(f"[Up] _poll_code_sent 异常: {e}")
        await asyncio.sleep(2)


async def _init():
    from database import init_db
    await init_db()
    await _refresh_active_slots()


def run():
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    logger.info(f"[Up] 启动上传机器人 (Up Bot)...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_upload", start_upload))
    app.add_handler(CommandHandler("end_upload", end_upload))
    app.add_handler(CommandHandler("cancel_upload", cancel_upload))

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

    loop.create_task(health_ping())
    loop.create_task(slot_refresh_loop())
    loop.create_task(_poll_code_sent(app))
    app.run_polling()


if __name__ == "__main__":
    run()