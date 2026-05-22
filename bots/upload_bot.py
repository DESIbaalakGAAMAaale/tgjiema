import asyncio
import datetime
import json
from collections import defaultdict

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from loguru import logger

from config import settings
from database import get_pending_uploads_col
from services.permission import check_upload_permission
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics

TOKEN = settings.UPLOAD_BOT_TOKEN
MAIN_CHANNEL_ID = settings.MAIN_STORAGE_CHANNEL_ID

_pending_media_groups: dict[str, dict] = {}


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "欢迎使用上传机器人。\n\n"
        "📤 **单次上传**：直接发送文件，立即生成文件码\n\n"
        "📦 **批次上传**（所有文件共用一个文件码）：\n"
        "  /start_upload - 开始批次上传\n"
        "  发送多个文件...\n"
        "  /end_upload - 结束批次，生成文件码\n\n"
        "所有用户（含免费用户）均可上传文件。"
    )


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您被禁止使用上传功能。")
        return

    context.user_data["batch"] = {
        "file_types": defaultdict(int),
        "file_infos": [],
    }
    await update.message.reply_text(
        "📦 已进入批次上传模式，请发送文件。\n"
        "发送 /end_upload 结束并生成文件码。\n"
        "发送 /cancel_upload 取消本次上传。"
    )


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "batch" in context.user_data:
        del context.user_data["batch"]
        await update.message.reply_text("批次上传已取消。")
    else:
        await update.message.reply_text("当前没有进行中的批次上传。")


async def end_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    batch = context.user_data.pop("batch", None)
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传，请先使用 /start_upload 开始。")
        return

    file_types = dict(batch["file_types"])
    file_infos = batch["file_infos"]

    if not file_infos:
        await update.message.reply_text("没有接收到任何文件，批次已取消。")
        return

    await update.message.reply_text(
        f"📦 正在处理 {len(file_infos)} 个文件，请稍候..."
    )

    channel_msg_ids = []
    for info in file_infos:
        try:
            forwarded = await info["update"].message.copy(chat_id=MAIN_CHANNEL_ID)
            channel_msg_ids.append(forwarded.message_id)
        except Exception as e:
            logger.error(f"批次上传转发文件到存储频道失败: {e}")

    if not channel_msg_ids:
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    type_str = json.dumps(file_types)
    batch_ids_str = ",".join(str(mid) for mid in channel_msg_ids)

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user.id,
            "primary_channel_id": MAIN_CHANNEL_ID,
            "primary_channel_msg_id": channel_msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "created_at": datetime.datetime.utcnow().isoformat(),
            "processed": 0,
        })
        logger.info(f"批次文件写入pending_uploads: user={user.id}, types={file_types}")
    except Exception as e:
        logger.error(f"写入pending_uploads失败: {e}")
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    await update.message.reply_text(
        f"📦 {len(file_infos)} 个文件已接收，正在生成文件码..."
    )

    metrics.upload_count += 1
    metrics.record_processed("upload_bot")


async def _dispatch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        batch["file_infos"].append({"update": update})
        await update.message.reply_text(f"✅ 已接收：{file_type}")


async def _flush_batch_media_group(mgid: str, context: ContextTypes.DEFAULT_TYPE, batch: dict):
    grp = _pending_media_groups.pop(mgid, None)
    if grp is None:
        return
    file_types = grp["file_types"]
    first = grp["updates"][0]
    for k, v in file_types.items():
        batch["file_types"][k] += v
    batch["file_infos"].append({"update": first})
    type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items()))
    await context.bot.send_message(
        chat_id=first.effective_chat.id,
        text=f"✅ 已接收媒体组：{type_desc}",
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
    first_update = group["updates"][0]

    await _process_upload(user_id, first_update, context, file_types)


async def _process_upload(
    user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, file_types: dict
):
    await update.message.reply_text("文件已接收，正在处理...")

    try:
        forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
        channel_msg_id = forwarded.message_id
    except Exception as e:
        logger.error(f"转发文件到存储频道失败: {e}")
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    type_str = json.dumps(file_types)

    try:
        pending_col = get_pending_uploads_col()
        await pending_col.insert_one({
            "uploader_id": user_id,
            "primary_channel_id": MAIN_CHANNEL_ID,
            "primary_channel_msg_id": channel_msg_id,
            "file_types": type_str,
            "batch_msg_ids": "",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "processed": 0,
        })
        logger.info(f"文件写入pending_uploads: user={user_id}, types={file_types}")
    except Exception as e:
        logger.error(f"写入pending_uploads失败: {e}")
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    await update.message.reply_text("文件已接收，正在生成文件码...")

    metrics.upload_count += 1
    metrics.record_processed("upload_bot")


async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    logger.info("启动上传机器人...")
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

    metrics.ping_bot("upload_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("upload_bot")
            await asyncio.sleep(30)

    loop.create_task(health_ping())
    app.run_polling()


if __name__ == "__main__":
    run()
