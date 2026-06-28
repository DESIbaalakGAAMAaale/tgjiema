"""File Bot - 引导机器人（哑巴导航员）
职责：仅响应用户明确指令，不主动、不复读、不进群。
- /start → 完整引导 + 3 按钮
- 其他消息 → 「请发送 /start 获取说明」
- 私聊隔离 + 60s 防抖 + 静默异常
"""

import asyncio
import time

from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from config import settings

TOKEN = settings.FILE_BOT_TOKEN

# ─── 60s 防抖：记录每个用户上次回复时间 ───
_reply_timestamps: dict[int, float] = {}
_DEBOUNCE_SECONDS = 60


def _build_guide() -> str:
    """构建引导文本，使用 .env 中的用户名。"""
    up = settings.UPLOAD_BOT_USERNAME or "UpBot"
    idx = settings.DECODER_BOT_USERNAME or "IdxBot"
    dsp = settings.SENDER_BOT_USERNAME or "DspBot"
    return (
        f"📁 Mfile — 使用指南\n\n"
        f"📤 上传文件\n"
        f"向 @{up} 发送文件，**@{idx} 收到后会自动回复您文件码**。\n"
        f"⚠️ 请先启动 @{idx}（发送 /start），以免无法接收文件码。\n\n"
        f"🔍 解码\n"
        f"向 @{idx} 发送文件码，**@{dsp} 随后会将文件发送给您**。\n"
        f"⚠️ 请先启动 @{dsp}（发送 /start），以免无法接收文件。\n\n"
        f"📥 接收文件\n"
        f"解码成功后，@{dsp} 会自动将文件发送给您。\n\n"
        f"⚠️ 免责声明\n"
        f"用户应对上传内容负责，本服务仅提供功能引导，不对文件内容负责。\n\n"
        f"🔗 快速开始"
    )


def _build_keyboard() -> InlineKeyboardMarkup:
    """构建引导按钮，链接到对应 Bot。"""
    up = settings.UPLOAD_BOT_USERNAME or ""
    idx = settings.DECODER_BOT_USERNAME or ""
    dsp = settings.SENDER_BOT_USERNAME or ""
    buttons = []
    if up:
        buttons.append(InlineKeyboardButton("📤 上传文件", url=f"https://t.me/{up}"))
    if idx:
        buttons.append(InlineKeyboardButton("🔍 解码接码", url=f"https://t.me/{idx}"))
    if dsp:
        buttons.append(InlineKeyboardButton("📥 接收文件", url=f"https://t.me/{dsp}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def _cleanup_debounce():
    """每 30 分钟清理超过 10 分钟未访问的防抖记录，防止内存泄漏。"""
    while True:
        await asyncio.sleep(1800)
        try:
            now = time.time()
            stale = [uid for uid, ts in _reply_timestamps.items() if now - ts > 600]
            for uid in stale:
                _reply_timestamps.pop(uid, None)
            if stale:
                logger.debug(f"[File] 清理防抖记录: {len(stale)} 条")
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — 返回完整引导 + 按钮。"""
    user_id = update.effective_user.id

    # 60s 防抖
    now = time.time()
    last = _reply_timestamps.get(user_id, 0)
    if now - last < _DEBOUNCE_SECONDS:
        return
    _reply_timestamps[user_id] = now

    try:
        await update.message.reply_text(
            _build_guide(),
            reply_markup=_build_keyboard(),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"[File] /start 回复失败 (user={user_id}): {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """非 /start 消息 — 引导用户使用 /start。"""
    user_id = update.effective_user.id

    # 60s 防抖
    now = time.time()
    last = _reply_timestamps.get(user_id, 0)
    if now - last < _DEBOUNCE_SECONDS:
        return
    _reply_timestamps[user_id] = now

    try:
        await update.message.reply_text("请发送 /start 获取说明")
    except Exception as e:
        logger.warning(f"[File] 消息回复失败 (user={user_id}): {e}")


async def _async_main():
    if not TOKEN:
        logger.warning("[File] FILE_BOT_TOKEN 未配置，跳过启动")
        return
    logger.info("[File] 启动引导机器人 (File Bot)...")
    app = Application.builder().token(TOKEN).build()

    # 仅接受私聊
    private_filter = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start, filters=private_filter))
    app.add_handler(MessageHandler(filters.ALL & private_filter, handle_message))

    loop = asyncio.get_running_loop()
    loop.create_task(_cleanup_debounce())

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