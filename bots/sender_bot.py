import asyncio
import json

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.ext import Application, CallbackQueryHandler
from loguru import logger

from config import settings
from services.queue_manager import dequeue_send_task
from utils.monitor import metrics

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
    elif mtype == "audio":
        return InputMediaAudio(media=fid)
    elif mtype == "animation":
        return InputMediaAnimation(media=fid)
    else:
        return InputMediaDocument(media=fid)


def _build_pagination_keyboard(file_code: str, current_page: int, total_pages: int):
    buttons = []
    if current_page > 1:
        buttons.append(InlineKeyboardButton(
            "<< 上一页", callback_data=f"pg|{file_code}|{current_page - 1}"
        ))
    buttons.append(InlineKeyboardButton(
        f"{current_page}/{total_pages}", callback_data="noop"
    ))
    if current_page < total_pages:
        buttons.append(InlineKeyboardButton(
            "下一页 >>", callback_data=f"pg|{file_code}|{current_page + 1}"
        ))
    return InlineKeyboardMarkup([buttons])


async def process_queue(bot):
    while True:
        try:
            task = await dequeue_send_task()
            if task is None:
                await asyncio.sleep(0.5)
                continue

            if task.task_type == "batch" and task.channel_msg_ids and task.batch_file_meta:
                await _process_batch_task(bot, task)
            else:
                await _process_single_task(bot, task)

        except Exception as e:
            logger.error(f"队列处理异常: {e}")
            await asyncio.sleep(1)


async def _process_single_task(bot, task):
    logger.info(
        f"发送文件: 用户 {task.target_user_id}, "
        f"频道 {task.channel_id}, 消息 {task.message_id}"
    )
    try:
        await bot.copy_message(
            chat_id=task.target_user_id,
            from_chat_id=task.channel_id,
            message_id=task.message_id,
        )
        logger.info(f"文件发送成功: 用户 {task.target_user_id}, 码 {task.file_code}")
        metrics.send_success_count += 1
        metrics.record_processed("sender_bot")
    except Exception as e:
        logger.error(f"文件发送失败: {e}")
        metrics.send_fail_count += 1
        metrics.record_error("sender_bot")
        try:
            await bot.send_message(
                chat_id=task.target_user_id,
                text="文件发送失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass


async def _process_batch_task(bot, task):
    logger.info(
        f"批量发送: 用户 {task.target_user_id}, "
        f"共 {len(task.channel_msg_ids)} 个文件, 码 {task.file_code}"
    )

    file_meta_list = []
    meta_raw = task.batch_file_meta
    if isinstance(meta_raw, list):
        file_meta_list = meta_raw
    elif meta_raw:
        try:
            file_meta_list = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    if not file_meta_list:
        await _fallback_single_send(bot, task)
        return

    total_pages = (len(file_meta_list) + PAGE_SIZE - 1) // PAGE_SIZE
    await _send_page(bot, task.target_user_id, task.file_code, file_meta_list, page=1, total_pages=total_pages)

    if total_pages > 1:
        _pagination_states[task.file_code] = {
            "channel_msg_ids": task.channel_msg_ids,
            "batch_file_meta": file_meta_list,
            "total_pages": total_pages,
            "chat_id": task.target_user_id,
        }


async def _fallback_single_send(bot, task):
    for mid in task.channel_msg_ids:
        try:
            await bot.copy_message(
                chat_id=task.target_user_id,
                from_chat_id=task.channel_id,
                message_id=mid,
            )
            metrics.send_success_count += 1
        except Exception as e:
            logger.error(f"批量回退单发失败 (mid={mid}): {e}")
            metrics.send_fail_count += 1
    metrics.record_processed("sender_bot")


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages):
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = file_meta_list[start:end]

    input_media = [_build_input_media(meta) for meta in page_items]

    try:
        await bot.send_media_group(chat_id=chat_id, media=input_media)
        logger.info(
            f"媒体组发送成功: chat={chat_id}, 码 {file_code}, "
            f"第 {page}/{total_pages} 页, {len(page_items)} 个文件"
        )
        metrics.send_success_count += 1
        metrics.record_processed("sender_bot")
    except Exception as e:
        logger.error(f"媒体组发送失败: {e}")
        metrics.send_fail_count += 1
        metrics.record_error("sender_bot")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="文件发送失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
        return

    if total_pages > 1:
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


async def pagination_callback(update: Update, context):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if not data.startswith("pg|"):
        await query.answer()
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

    # Delete old pagination message
    old_msg_id = state.get("last_pagination_msg_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id, message_id=old_msg_id
            )
        except Exception:
            pass

    await _send_page(
        context.bot, query.message.chat_id, file_code,
        file_meta_list, page=page, total_pages=total_pages,
    )
    await query.answer()


async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    logger.info("启动文件发送机器人...")
    app = Application.builder().token(TOKEN).build()
    bot = app.bot

    app.add_handler(CallbackQueryHandler(pagination_callback))

    metrics.ping_bot("sender_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("sender_bot")
            await asyncio.sleep(30)

    loop.create_task(health_ping())
    loop.create_task(process_queue(bot))
    app.run_polling()


if __name__ == "__main__":
    run()