"""Dsp Bot — 发送机器人(环形冗余架构版)
职责:从 jobs 表轮询任务 → 通过 delivery_resolver 解析最佳频道 → 媒体组发送给用户
替代原 sender_bot,数据源从 send_queue 改为 jobs 表。
"""

import asyncio
import time
try:
    import orjson as json
except ImportError:
    import json
from typing import Any

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from loguru import logger

from config import settings
from database import dequeue_jobs, get_file_records_col, reenqueue_job, mark_job_dead, set_cell_status, get_active_or_shadow_cell
from storage.delivery_resolver import resolve_delivery_channel, try_deliver
from utils.monitor import metrics
from utils.force_join import check_force_join, three_bot_reminder
from utils.flood_waiter import (
    safe_send_message,
    safe_send_media_group,
    safe_send_photo,
    safe_send_video,
    safe_send_audio,
    safe_send_animation,
    safe_send_document,
    safe_reply_text,
)
from utils.task_utils import create_safe_task

TOKEN = settings.SENDER_BOT_TOKEN
PAGE_SIZE = settings.PAGE_SIZE

# ─── 多 worker 并发控制 ───
# 最大并发发送数(留 5 个余量给 copy_message 等其他 API 调用)
_SEND_CONCURRENCY = settings.SEND_CONCURRENCY
_send_semaphore = asyncio.Semaphore(_SEND_CONCURRENCY)

# ─── Dsp 侧频道路由降级(Mon Bot 补充机制)───
# 当 Mon Bot 不可用时,Dsp 通过发送失败率自动触发降级,作为兜底。
# 阈值比 Mon 更保守,仅窗口内多次失败才触发,避免误判。
_channel_failures: dict[int, list[float]] = {}  # channel_id -> [failure_timestamps]
_CHANNEL_FAILURE_THRESHOLD = settings.CHANNEL_FAILURE_THRESHOLD  # 60 秒内失败 N 次触发降级
_CHANNEL_FAILURE_WINDOW = settings.CHANNEL_FAILURE_WINDOW  # 统计窗口(秒)

_pagination_states: dict[str, dict] = {}


def _record_channel_failure(channel_id: int):
    """记录频道路由发送失败时间戳,用于 Dsp 侧降级检测。"""
    now = time.time()
    if channel_id not in _channel_failures:
        _channel_failures[channel_id] = []
    _channel_failures[channel_id].append(now)
    logger.debug(f"[Dsp] 频道 {channel_id} 发送失败记录 (当前窗口内失败: {len(_channel_failures[channel_id])})")


async def _check_channel_degrade(channel_id: int):
    """检查某个频道是否在窗口内失败次数超过阈值,触发降级(兜底机制)。
    
    作为 Mon Bot 的补充,仅在 Mon 不可用时作为兜底。
    阈值设置更保守(3次/60秒),避免误触发短暂网络波动。
    """
    if channel_id not in _channel_failures:
        return

    now = time.time()
    # 清理过期记录
    _channel_failures[channel_id] = [
        ts for ts in _channel_failures[channel_id]
        if now - ts <= _CHANNEL_FAILURE_WINDOW
    ]

    fail_count = len(_channel_failures[channel_id])
    if fail_count < _CHANNEL_FAILURE_THRESHOLD:
        return

    # 超过阈值,触发降级
    try:
        cell = await get_active_or_shadow_cell(channel_id)
        if not cell:
            logger.warning(f"[Dsp] 降级失败: 频道 {channel_id} 不在 cells 表中")
            _channel_failures.pop(channel_id, None)
            return

        slot_id = cell.get("slot_id")
        current_status = cell.get("status", "")
        if current_status == "lost":
            # 已经是 lost 状态,无需重复降级
            _channel_failures.pop(channel_id, None)
            return
        if current_status == "r100":
            # R100 槽位永不自降
            logger.warning(f"[Dsp] 频道 {channel_id} 是 R100 槽位,跳过降级")
            _channel_failures.pop(channel_id, None)
            return

        await set_cell_status(slot_id, "lost")
        logger.warning(
            f"[Dsp] 频道降级触发: {slot_id} (channel={channel_id}) "
            f"status={current_status}→lost, "
            f"窗口内失败 {fail_count} 次/{_CHANNEL_FAILURE_WINDOW}s"
        )
        # 降级成功后清除该频道失败记录
        _channel_failures.pop(channel_id, None)
    except Exception as e:
        logger.error(f"[Dsp] 频道降级异常 (channel={channel_id}): {e}")


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


# ─── 核心:多 worker 并发处理 jobs 表 ───

async def process_queue(bot):
    """启动多个 worker 并发处理 jobs 队列。"""
    workers = []
    num_workers = min(4, _SEND_CONCURRENCY)  # 最多 4 个 worker
    logger.info(f"[Dsp] 启动 {num_workers} 个 worker,并发上限 {_SEND_CONCURRENCY}")
    for i in range(num_workers):
        w = create_safe_task(_dsp_worker(bot, i), name=f"dsp-worker-{i}")
        workers.append(w)

    for w in workers:
        await w


async def _dsp_worker(bot: Any, worker_id: int):
    """单个 worker 循环拉取任务。
    策略:维护本地队列,空时批量从 CRDB dequeue_jobs(10),有任务时本地消费不查 DB。
    无事时最多 30 秒查一次本地通知,几乎零消耗。
    """
    from database.cache_store import get_cache_store
    store = get_cache_store()

    local_queue: list = []  # 本地任务队列,有任务时直接处理不查 CRDB
    idle_count = 0
    while True:
        try:
            # 本地队列有任务 → 直接处理
            if not local_queue:
                # 先查本地通知(零 RU)
                has_job = await store.has_new_dsp_job()
                if not has_job:
                    idle_count += 1
                    sleep_time = min(2.0 * (1.6 ** min(idle_count, 12)), 30.0)
                    await asyncio.sleep(sleep_time)
                    continue

                # 有本地通知 → 批量从 CRDB 拉取
                local_queue = await dequeue_jobs(10)
                if not local_queue:
                    idle_count += 1
                    await asyncio.sleep(1)
                    continue

            idle_count = 0
            job = local_queue.pop(0)  # FIFO 消费本地队列

            # 死信检查:retry_count >= 3 直接标记为 dead,不再尝试
            if job.retry_count >= 3:
                await mark_job_dead(job.job_id, f"重试次数已达上限({job.retry_count})")
                logger.warning(f"[Dsp-{worker_id}] 死信标记: job={job.job_id}, code={job.code}, retry={job.retry_count}")
                continue

            # 等待 semaphore(最多 10 秒)
            try:
                await asyncio.wait_for(
                    _send_semaphore.acquire(), timeout=10.0
                )
            except asyncio.TimeoutError:
                # 等超时,重新入队
                await reenqueue_job(job.job_id)
                logger.debug(f"[Dsp-{worker_id}] semaphore 等待超时,重新入队 job={job.job_id}")
                await asyncio.sleep(1)
                continue

            send_ok = False
            try:
                if job.task_type == "batch":
                    await _process_batch_job(bot, job)
                    send_ok = True
                else:
                    send_ok = await _process_single_job(bot, job)
            except Exception as e:
                logger.error(f"[Dsp-{worker_id}] 发送异常 (retry={job.retry_count}): {e}")
            finally:
                _send_semaphore.release()

            # 发送失败处理:根据 retry_count 决定重试还是死信
            if not send_ok:
                if job.retry_count >= 2:
                    await mark_job_dead(job.job_id, f"发送失败(已重试{job.retry_count}次): {job.code}")
                    logger.warning(f"[Dsp-{worker_id}] 死信: job={job.job_id}, code={job.code}, retry={job.retry_count}")
                else:
                    await reenqueue_job(job.job_id)
                    logger.info(f"[Dsp-{worker_id}] 重试入队: job={job.job_id}, code={job.code}, retry={job.retry_count}→{job.retry_count + 1}")

                # Dsp 侧降级检测(Mon Bot 补充机制,兜底)
                for ch_id in list(_channel_failures.keys()):
                    await _check_channel_degrade(ch_id)

        except Exception as e:
            logger.error(f"[Dsp-{worker_id}] 队列处理异常: {e}")
            await asyncio.sleep(1)


async def _send_file_direct(bot, job) -> bool:
    """直接用 file_id 向用户发文件,不走 copy_message(避开频道限速)。

    返回 True 表示成功,False 表示回退到 copy_message。
    """
    meta_raw = getattr(job, "batch_file_meta", None)
    if not meta_raw:
        return False
    if isinstance(meta_raw, list):
        file_meta = meta_raw[0]
    else:
        try:
            parsed = json.loads(meta_raw)
            if isinstance(parsed, list):
                file_meta = parsed[0]
            else:
                file_meta = parsed
        except (json.JSONDecodeError, TypeError, IndexError):
            return False

    fid = file_meta.get("file_id", "")
    mtype = file_meta.get("type", "document")
    if not fid:
        return False

    protect_content = getattr(job, "protect_content", False)
    kwargs = {"chat_id": job.target_user_id, "protect_content": protect_content}

    try:
        if mtype == "photo":
            await safe_send_photo(bot, photo=fid, **kwargs)
        elif mtype == "video":
            await safe_send_video(bot, video=fid, **kwargs)
        elif mtype in ("audio", "voice"):
            await safe_send_audio(bot, audio=fid, **kwargs)
        elif mtype == "animation":
            await safe_send_animation(bot, animation=fid, **kwargs)
        else:
            await safe_send_document(bot, document=fid, **kwargs)
        logger.info(f"[Dsp] file_id 直发成功: 用户 {job.target_user_id}, 码 {job.code}")
        return True
    except Exception:
        return False


async def _process_single_job(bot, job):
    logger.info(
        f"[Dsp] 发送文件: 用户 {job.target_user_id}, "
        f"频道 {job.storage_channel_id}, 消息 {job.storage_msg_ids[0] if job.storage_msg_ids else '?'}"
    )
    msg_id = job.storage_msg_ids[0] if job.storage_msg_ids else 0
    if not msg_id:
        metrics.send_fail_count += 1
        return False

    # protect_content 已从 jobs 表直接获取,无需再查 file_records
    protect_content = getattr(job, "protect_content", False)

    # ── 优先走 file_id 直发(避开 copy_message 的频道限速) ──
    if await _send_file_direct(bot, job):
        metrics.send_success_count += 1
        metrics.record_processed("dsp_bot")
        return True

    # ── 直发失败,回退到 copy_message ──
    resolved = await resolve_delivery_channel(job.storage_channel_id)
    success = await try_deliver(bot, job.target_user_id, resolved.channel_id, msg_id, protect_content=protect_content)

    if not success:
        _record_channel_failure(resolved.channel_id)
        # 环形降级:沿环找下一个可用频道
        next_resolved = await resolve_delivery_channel(resolved.channel_id)
        if next_resolved.channel_id != resolved.channel_id:
            success = await try_deliver(bot, job.target_user_id, next_resolved.channel_id, msg_id, protect_content=protect_content)
            if not success:
                _record_channel_failure(next_resolved.channel_id)

    if success:
        logger.info(f"[Dsp] 发送成功: 用户 {job.target_user_id}, 码 {job.code}")
        metrics.send_success_count += 1
        metrics.record_processed("dsp_bot")
        return True

    logger.error(f"[Dsp] 发送失败(所有槽位不可用): 码 {job.code}")
    metrics.send_fail_count += 1
    metrics.record_error("dsp_bot")
    try:
        await safe_send_message(bot, chat_id=job.target_user_id, text="文件发送失败,请稍后重试或联系管理员。")
    except Exception:
        pass
    return False


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
    protect_content = getattr(job, "protect_content", False)
    for i, mid in enumerate(job.storage_msg_ids):
        resolved = await resolve_delivery_channel(job.storage_channel_id)
        if await try_deliver(bot, job.target_user_id, resolved.channel_id, mid, protect_content=protect_content):
            metrics.send_success_count += 1
        else:
            metrics.send_fail_count += 1
        # 每条消息之间间隔 0.15s,避免同频道/同用户超限
        if i < len(job.storage_msg_ids) - 1:
            await asyncio.sleep(0.15)
    metrics.record_processed("dsp_bot")


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages, storage_channel_id=None):
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = file_meta_list[start:end]

    input_media = [_build_input_media(meta) for meta in page_items]
    try:
        await safe_send_media_group(bot, chat_id=chat_id, media=input_media)
        logger.info(f"[Dsp] 媒体组发送成功: {chat_id}, 码 {file_code}, 第{page}/{total_pages}页, {len(input_media)}个")
        metrics.send_success_count += 1
        metrics.record_processed("dsp_bot")
    except Exception as e:
        logger.error(f"[Dsp] 媒体组发送失败: {e}")
        metrics.send_fail_count += 1
        metrics.record_error("dsp_bot")
        try:
            await safe_send_message(bot, chat_id=chat_id, text="文件发送失败,请稍后重试或联系管理员。")
        except Exception:
            pass
        return

    if total_pages > 1 and page < total_pages:
        keyboard = _build_pagination_keyboard(file_code, page, total_pages)
        total_files = len(file_meta_list)
        sent_msg = await safe_send_message(
            bot, chat_id=chat_id,
            text=f"第 {page}/{total_pages} 页(共 {total_files} 个文件)",
            reply_markup=keyboard,
        )
        state = _pagination_states.get(file_code)
        if state and sent_msg:
            state["last_pagination_msg_id"] = sent_msg.message_id
        # 翻页提示之间间隔 0.3s,避免用户聊天被消息淹没
        await asyncio.sleep(0.3)


# ─── 命令处理 ───

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        "欢迎使用文件发送机器人!\n\n"
        "此机器人用于接收解码后的文件,无需手动操作。\n"
        "当您通过解码机器人获取文件码后,文件会自动发送给您。"
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
        await query.answer("请先加入频道")
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
        await query.answer("会话已过期,请重新发送文件码。", show_alert=True)
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


async def _async_main():
    await _init()

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

    loop = asyncio.get_running_loop()
    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(process_queue(bot), name="process-queue")
    from database.cache import dump_cache_to_disk_loop
    create_safe_task(dump_cache_to_disk_loop(), name="dump-cache")

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
    """启动 Dsp Bot(使用 asyncio.run 标准模式)。"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()