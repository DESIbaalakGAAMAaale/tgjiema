import asyncio
import json

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from loguru import logger

from config import settings
from services.queue_manager import dequeue_send_task
from database import get_file_records_col
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
    success = await _try_copy(bot, task.target_user_id, task.channel_id, task.message_id)
    if success:
        logger.info(f"文件发送成功: 用户 {task.target_user_id}, 码 {task.file_code}")
        metrics.send_success_count += 1
        metrics.record_processed("sender_bot")
        return

    success = await _try_fallback_copy(bot, task)
    if success:
        logger.info(f"文件通过备用频道发送成功: 用户 {task.target_user_id}, 码 {task.file_code}")
        metrics.send_success_count += 1
        metrics.record_processed("sender_bot")
        return

    logger.error(f"文件发送失败（所有频道均不可用）: 码 {task.file_code}")
    metrics.send_fail_count += 1
    metrics.record_error("sender_bot")
    try:
        await bot.send_message(
            chat_id=task.target_user_id,
            text="文件发送失败，请稍后重试或联系管理员。",
        )
    except Exception:
        pass


async def _try_copy(bot, chat_id, from_chat_id, message_id) -> bool:
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
        return True
    except Exception:
        return False


async def _try_fallback_copy(bot, task) -> bool:
    if not task.file_code:
        return False
    col = get_file_records_col()
    record = await col.find_one({"file_code": task.file_code})
    if not record:
        return False
    backup_info = record.get("backup_channel_msg_ids") or []
    if isinstance(backup_info, str):
        return False

    for entry in backup_info:
        channel_id = entry.get("channel_id")
        msg_ids = entry.get("backed_msg_ids") or entry.get("msg_ids") or []
        if not msg_ids:
            continue
        msg_id = msg_ids[0]
        if await _try_copy(bot, task.target_user_id, channel_id, msg_id):
            return True
    return False


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
    await _send_page(bot, task.target_user_id, task.file_code, file_meta_list, page=1, total_pages=total_pages, storage_channel_id=task.channel_id)

    if total_pages > 1:
        _pagination_states[task.file_code] = {
            "channel_msg_ids": task.channel_msg_ids,
            "batch_file_meta": file_meta_list,
            "total_pages": total_pages,
            "chat_id": task.target_user_id,
            "storage_channel_id": task.channel_id,
        }


async def _fallback_single_send(bot, task):
    for mid in task.channel_msg_ids:
        if await _try_copy(bot, task.target_user_id, task.channel_id, mid):
            metrics.send_success_count += 1
            continue
        backup_record = None
        if task.file_code:
            col = get_file_records_col()
            backup_record = await col.find_one({"file_code": task.file_code})
        if backup_record:
            backup_info = backup_record.get("backup_channel_msg_ids") or []
            if not isinstance(backup_info, str):
                for entry in backup_info:
                    ch_id = entry.get("channel_id")
                    bk_mids = entry.get("backed_msg_ids") or entry.get("msg_ids") or []
                    if not bk_mids:
                        continue
                    if await _try_copy(bot, task.target_user_id, ch_id, bk_mids[0]):
                        metrics.send_success_count += 1
                        break
                else:
                    metrics.send_fail_count += 1
                    continue
            else:
                metrics.send_fail_count += 1
        else:
            metrics.send_fail_count += 1
    metrics.record_processed("sender_bot")


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages, storage_channel_id=None):
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = file_meta_list[start:end]

    if storage_channel_id and all("msg_id" in meta for meta in page_items):
        msg_ids = sorted(int(meta["msg_id"]) for meta in page_items)
        try:
            await bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=storage_channel_id,
                message_ids=msg_ids,
            )
            logger.info(
                f"媒体组发送成功(复制): chat={chat_id}, 码 {file_code}, "
                f"第 {page}/{total_pages} 页, {len(msg_ids)} 个文件"
            )
            metrics.send_success_count += 1
            metrics.record_processed("sender_bot")
        except Exception as e:
            logger.error(f"媒体组发送失败(复制): {e}")
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
    else:
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
        storage_channel_id=state.get("storage_channel_id"),
    )

    if page >= total_pages:
        _pagination_states.pop(file_code, None)

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

    app.add_handler(CommandHandler("start", start))
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