"""File Bot - 引导机器人
职责:接收用户消息 -> 返回功能引导文本，指引用户使用正确的 Bot
"""

import asyncio

from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import settings

TOKEN = settings.FILE_BOT_TOKEN

# 缓存的消息文本，每 60 秒从配置刷新
_cached_msg: str = ""
_cache_lock = asyncio.Lock()


def _build_default_msg() -> str:
    """用 .env 中的用户名拼接默认引导文本。"""
    upload = settings.UPLOAD_BOT_USERNAME or "UpBot"
    decoder = settings.DECODER_BOT_USERNAME or "IdxBot"
    sender = settings.SENDER_BOT_USERNAME or "DspBot"
    return (
        "文件助手\n\n"
        "请根据需求选择对应机器人：\n\n"
        f"上传文件 → @{upload}\n"
        f"解码/接码 → @{decoder}\n"
        f"接收文件 → @{sender}\n\n"
        "请勿向本机器人发送文件或文件码，本机器人仅提供引导。"
    )


async def _get_reply_msg() -> str:
    """获取回复文本，优先从数据库缓存读取，否则用默认值。"""
    global _cached_msg
    if _cached_msg:
        return _cached_msg
    try:
        from database.session import get_config_cached
        db_msg = await get_config_cached("filebot_msg")
        if db_msg:
            _cached_msg = db_msg
            return _cached_msg
    except Exception:
        pass
    _cached_msg = _build_default_msg()
    return _cached_msg


async def _refresh_msg_loop():
    """每 60 秒刷新缓存文本，支持 admin_bot 热更新。"""
    global _cached_msg
    while True:
        await asyncio.sleep(60)
        try:
            from database.session import get_config_cached
            db_msg = await get_config_cached("filebot_msg")
            if db_msg:
                _cached_msg = db_msg
            else:
                _cached_msg = _build_default_msg()
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await _get_reply_msg()
    await update.message.reply_text(msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await _get_reply_msg()
    await update.message.reply_text(msg)


async def _async_main():
    logger.info("[File] 启动引导机器人 (File Bot)...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    loop = asyncio.get_running_loop()
    loop.create_task(_refresh_msg_loop())

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
    if not TOKEN:
        logger.warning("File Bot Token 未配置（FILE_BOT_TOKEN），跳过启动")
        return
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()