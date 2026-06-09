"""Dsp Bot — 发送机器人（环形冗余架构版）
职责：从 jobs 表轮询任务 → 通过 delivery_resolver 解析最佳频道 → 媒体组发送给用户
替代原 sender_bot，数据源从 send_queue 改为 jobs 表。
"""

import asyncio
import json

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from loguru import logger

from config import settings
from database import dequeue_job, get_file_records_col
from storage.delivery_resolver import resolve_delivery_channel, try_deliver
from utils.monitor import metrics
from utils.force_join import check_force_join, three_bot_reminder

TOKEN = settings.SENDER_BOT_TOKEN
PAGE_SIZE = 10

_pagination_states: dict[str, dict] = {}


def _build_input_media(meta: dict):
    mtype = meta.get("type", "document")
    fid = meta.get("file_id", "")
    if mtype == "photo":
        return InputMediaPhoto(media=fid)
    elif mtype == "video":
        return InputMediaVideo(media=fid)
    elif mtype in ("audio", "voice"):
        return InputMediaAudio(media=fid)
    elif mtype == "animation":
        return InputMediaAnimation(media=fid)
    else:
        return InputMediaDocument(media=fid)


def _build_pagination_keyboard(file_code: str, current_page: int, total_pages: int):
    keyboard = []
    nav_row = []
    if current_page > 1:
        nav_row.append(InlineKeyboardButton("⏮ 首页", callback_data=f"pg|{file_code}|1"))
        nav_row.append(InlineKeyboardButton("◀ 上页", callback_data=f"pg|{file_code}|{current_page - 1}"))
    nav_row.append(InlineKeyboardButton(f"第{current_page}/{total_pages}页", callback_data="noop"))
    if current_page < total_pages:
        nav_row.append(InlineKeyboardButton("下页 ▶", callback_data=f"pg|{file_code}|{current_page + 1}"))
        nav_row.append(InlineKeyboardButton("末页 ⏭", callback_data=f"pg|{file_code}|{total_pages}"))
    keyboard.append(nav_row)
    if total_pages > 1:
        page_row = _build_page_number_buttons(file_code, current_page, total_pages)
        if page_row:
            keyboard.append(page_row)
    return InlineKeyboardMarkup(keyboard)


def _build_page_number_buttons(file_code: str, current_page: int, total_pages: int):
    WINDOW_SIZE = 8
    window = min(WINDOW_SIZE, total_pages)
    start = max(1, min(current_page - window // 2, total_pages - window + 1))
    buttons = []
    for p in range(start, start + window):
        if p == current_page:
            buttons.append(InlineKeyboardButton(f"● {p}", callback_data="noop"))
        else:
            buttons.append(InlineKeyboardButton(str(p), callback_data=f"pg|{file_code}|{p}"))
    return buttons


# ─── 核心：从 jobs 表拉取任务 ───

async def process_queue(bot):
    """不断从 jobs 表拉取待派工任务，发送给用户。"""
    idle_count = 0
    while True:
        try:
            job = await dequeue_job()
            if job is None:
                idle_count += 1
                sleep_time = min(0.05 * (1.6 ** min(idle_count, 12)), 10.0)
                await asyncio.sleep(sleep_time)
                continue

            idle_count = 0

            if job.task_type == "batch" and job.storage_msg_ids and job.batch_file_meta:
                await _process_batch_job(bot, job)
            else:
                await _process_single_job(bot, job)

        except Exception as e:
            logger.error(f"[Dsp] 队列处理异常: {e}")
            await asyncio.sleep(1)


async def _process_single_job(bot, job):
    logger.info(
        f"[Dsp] 发送文件: 用户 {job.target_user_id}, "
        f"频道 {job.storage_channel_id}, 消息 {job.storage_msg_ids[0] if job.storage_msg_ids else '?'}"
    )
    msg_id = job.storage_msg_ids[0] if job.storage_msg_ids else 0
    if not msg_id:
        metrics.send_fail_count += 1
        return

    # 使用 delivery_resolver 解析频道 + 降级
    resolved = await resolve_delivery_channel(job.storage_channel_id)
    success = await try_deliver(bot, job.target_user_id, resolved.channel_id, msg_id)

    if not success:
        # 环形降级：沿环找下一个可用频道
        from storage.delivery_resolver import resolve_delivery_channel as _resolve
        next_resolved = await _resolve(resolved.channel_id)
        if next_resolved.channel_id != resolved.channel_id:
            success = await try_deliver(bot, job.target_user_id, next_resolved.channel_id, msg_id)

    if success:
        logger.info(f"[Dsp] 发送成功: 用户 {job.target_user_id}, 码 {job.code}")
        metrics.send_success_count += 1
        metrics.record_processed("dsp_bot")
        return

    logger.error(f"[Dsp] 发送失败（所有槽位不可用）: 码 {job.code}")
    metrics.send_fail_count += 1
    metrics.record_error("dsp_bot")
    try:
        await bot.send_message(chat_id=job.target_user_id, text="文件发送失败，请稍后重试或联系管理员。")
    except Exception:
        pass


async def _try_deliver_to_user(bot, chat_id, from_channel, msg_id) -> bool:
    """尝试从指定频道复制消息给用户（在 delivery_resolver 中定义的轻量版，避免循环导入）。"""
    from storage.delivery_resolver import try_deliver as _td
    return await _td(bot, chat_id, from_channel, msg_id)


async def _process_batch_job(bot, job):
    logger.info(
        f"[Dsp] 批量发送: 用户 {job.target_user_id}, "
        f"共 {len(job.storage_msg_ids)} 个文件, 码 {job.code}"
    )

    file_meta_list = []
    meta_raw = job.batch_file_meta
    if isinstance(meta_raw, list):
        file_meta_list = meta_raw
    elif meta_raw:
        try:
            file_meta_list = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    if not file_meta_list:
        await _fallback_single_send(bot, job)
        return

    total_pages = (len(file_meta_list) + PAGE_SIZE - 1) // PAGE_SIZE

    if total_pages > 1:
        _pagination_states[job.code] = {
            "channel_msg_ids": job.storage_msg_ids,
            "batch_file_meta": file_meta_list,
            "total_pages": total_pages,
            "chat_id": job.target_user_id,
            "storage_channel_id": job.storage_channel_id,
        }

    await _send_page(
        bot, job.target_user_id, job.code,
        file_meta_list, page=1, total_pages=total_pages,
        storage_channel_id=job.storage_channel_id,
    )


async def _fallback_single_send(bot, job):
    for mid in job.storage_msg_ids:
        resolved = await resolve_delivery_channel(job.storage_channel_id)
        if await try_deliver(bot, job.target_user_id, resolved.channel_id, mid):
            metrics.send_success_count += 1
        else:
            metrics.send_fail_count += 1
    metrics.record_processed("dsp_bot")


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages, storage_channel_id=None):
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = file_meta_list[start:end]

    input_media = [_build_input_media(meta) for meta in page_items]
    try:
        await bot.send_media_group(chat_id=chat_id, media=input_media)
        logger.info(f"[Dsp] 媒体组发送成功: {chat_id}, 码 {file_code}, 第{page}/{total_pages}页, {len(input_media)}个")
        metrics.send_success_count += 1
        metrics.record_processed("dsp_bot")
    except Exception as e:
        logger.error(f"[Dsp] 媒体组发送失败: {e}")
        metrics.send_fail_count += 1
        metrics.record_error("dsp_bot")
        try:
            await bot.send_message(chat_id=chat_id, text="文件发送失败，请稍后重试或联系管理员。")
        except Exception:
            pass
        return

    if total_pages > 1 and page < total_pages:
        keyboard = _build_pagination_keyboard(file_code, page, total_pages)
        total_files = len(file_meta_list)
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"第 {page}/{total_pages} 页（共 {total_files} 个文件）",
            reply_markup=keyboard,
        )
        state = _pagination_states.get(file_code)
        if state:
            state["last_pagination_msg_id"] = sent_msg.message_id


# ─── 命令处理 ───

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await update.message.reply_text(
        "欢迎使用文件发送机器人！\n\n"
        "此机器人用于接收解码后的文件，无需手动操作。\n"
        "当您通过解码机器人获取文件码后，文件会自动发送给您。"
        + three_bot_reminder()
    )


async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return
    if not data.startswith("pg|"):
        await query.answer()
        return

    if not await check_force_join(update, context):
        return

    parts = data.split("|")
    if len(parts) != 3:
        await query.answer()
        return

    file_code = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        await query.answer()
        return

    state = _pagination_states.get(file_code)
    if not state:
        await query.answer("会话已过期，请重新发送文件码。", show_alert=True)
        return

    file_meta_list = state["batch_file_meta"]
    total_pages = state["total_pages"]

    if page < 1 or page > total_pages:
        await query.answer("无效的页码。")
        return

    old_msg_id = state.get("last_pagination_msg_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=old_msg_id)
        except Exception:
            pass

    await _send_page(
        context.bot, query.message.chat_id, file_code,
        file_meta_list, page=page, total_pages=total_pages,
        storage_channel_id=state.get("storage_channel_id"),
    )

    if page >= total_pages:
        _pagination_states.pop(file_code, None)

    await query.answer()


# ─── 运行 ───

async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    logger.info("[Dsp] 启动发送机器人 (Dsp Bot)...")
    app = Application.builder().token(TOKEN).build()
    bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pagination_callback))

    metrics.ping_bot("dsp_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("dsp_bot")
            await asyncio.sleep(30)

    loop.create_task(health_ping())
    loop.create_task(process_queue(bot))
    from database.cache import dump_cache_to_disk_loop
    loop.create_task(dump_cache_to_disk_loop())
    app.run_polling()


if __name__ == "__main__":
    run()