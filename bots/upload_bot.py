import asyncio
from collections import defaultdict

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from loguru import logger

from config import settings
from services.permission import check_upload_permission
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics

TOKEN = settings.BOT_TOKENS.get("UPLOAD_BOT", "")
DECODER_BOT_CHAT_ID = settings.DECODER_BOT_CHAT_ID
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
    await update.message.reply_text("欢迎使用上传机器人。请直接发送文件给我，我会为您生成文件码。")


async def _dispatch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        await handle_media_group(update, context)
    else:
        await handle_file(update, context)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not global_rate_limiter.acquire():
        await update.message.reply_text("系统繁忙，请稍后重试。")
        return
    if not user_rate_limiter.acquire(user.id):
        await update.message.reply_text("操作过于频繁，请稍后重试。")
        return

    if not await check_upload_permission(user.id):
        await update.message.reply_text(
            "您没有上传权限。上传功能仅限基础会员和高级会员使用。"
        )
        return

    file_type = _detect_file_type(update)
    file_types = {file_type: 1}

    await _process_upload(user.id, update, context, file_types)


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    media_group_id = update.message.media_group_id

    if not await check_upload_permission(user.id):
        await update.message.reply_text(
            "您没有上传权限。上传功能仅限基础会员和高级会员使用。"
        )
        return

    file_type = _detect_file_type(update)

    if media_group_id not in _pending_media_groups:
        _pending_media_groups[media_group_id] = {
            "user_id": user.id,
            "file_types": defaultdict(int),
            "updates": [],
            "timer": None,
        }

    group = _pending_media_groups[media_group_id]
    group["file_types"][file_type] += 1
    group["updates"].append(update)

    if group["timer"]:
        group["timer"].cancel()

    group["timer"] = asyncio.get_running_loop().call_later(
        1.5, lambda: asyncio.ensure_future(_flush_media_group(media_group_id, context))
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
        forwarded = await update.message.forward(chat_id=MAIN_CHANNEL_ID)
        channel_msg_id = forwarded.message_id
    except Exception as e:
        logger.error(f"转发文件到存储频道失败: {e}")
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    type_str = ",".join(f"{k}:{v}" for k, v in sorted(file_types.items()))

    try:
        internal_msg = (
            f"NEW_FILE\n"
            f"uploader_id:{user_id}\n"
            f"channel_id:{MAIN_CHANNEL_ID}\n"
            f"message_id:{channel_msg_id}\n"
            f"file_types:{type_str}"
        )
        await context.bot.send_message(
            chat_id=DECODER_BOT_CHAT_ID, text=internal_msg
        )
        logger.info(f"上传文件通知已发送: user={user_id}, types={file_types}")
    except Exception as e:
        logger.error(f"通知解码机器人失败: {e}")
        metrics.record_error("upload_bot")
        await update.message.reply_text("文件处理失败，请稍后重试。")
        return

    metrics.upload_count += 1
    metrics.record_processed("upload_bot")


async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(_init())
    logger.info("启动上传机器人...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))
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

    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.create_task(health_ping())
    app.run_polling()


if __name__ == "__main__":
    run()