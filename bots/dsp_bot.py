"""Dsp Bot - 发送机器人(环形冗余架构)
职责: 从 jobs 表轮询任务, 通过 delivery_resolver 解析最佳频道, 以媒体组发送给用户
替代: sender_bot,数据源从 send_queue 改为 jobs 表
"""

import asyncio
import datetime
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
from database import get_file_record_cached, get_pending_jobs_count_local, set_cell_status, get_active_or_shadow_cell
from storage.delivery_resolver import resolve_delivery_channel, try_deliver
from utils.per_channel_limiter import _channel_limiter
from utils.monitor import metrics
from utils.dynamic_rate_limiter import dynamic_rate_limiter
from utils.force_join import check_force_join, three_bot_reminder
from utils.flood_waiter import (
    safe_reply_text,
    safe_send_message,
    safe_send_media_group,
) 
from utils.task_utils import create_safe_task

TOKEN = settings.SENDER_BOT_TOKEN
PAGE_SIZE = settings.PAGE_SIZE

# ─── 4 worker 并发控制 ───
# 最大并发发送数(25 个余量给 copy_message 等其他 API 调用)
_SEND_CONCURRENCY = settings.SEND_CONCURRENCY
_send_semaphore = asyncio.Semaphore(_SEND_CONCURRENCY)

# ─── Dsp 侧频道路由降级(Mon Bot 补充机制)───
# 当 Mon Bot 不可用时,Dsp 通过发送失败率自动触发降级,作为兜底方案
# 阈值比 Mon 更保守,仅窗口内多次失败才触发,避免误判
_channel_failures: dict[int, list[float]] = {}  # channel_id -> [failure_timestamps]
_CHANNEL_FAILURE_THRESHOLD = settings.CHANNEL_FAILURE_THRESHOLD  # 60 秒内失败 N 次触发降级
_CHANNEL_FAILURE_WINDOW = settings.CHANNEL_FAILURE_WINDOW  # 统计窗口(秒)

_pagination_states: dict[str, dict] = {}
_PAGE_STATE_TTL = 300  # 5 分钟过期清理


async def _cleanup_page_states():
    """定期清理过期的分页状态"""
    while True:
        try:
            now = time.time()
            expired = [k for k, v in _pagination_states.items() if now - v.get("created_at", 0) > _PAGE_STATE_TTL]
            for k in expired:
                _pagination_states.pop(k, None)
        except Exception as e:
            logger.error(f"[Dsp] 清理分页状态异常: {e}")
        await asyncio.sleep(60)


async def _cleanup_channel_failures():
    """定期清理超过 10 分钟未访问的频道失败记录,防止内存泄漏"""
    while True:
        try:
            now = time.time()
            stale = [
                ch_id for ch_id, timestamps in _channel_failures.items()
                if not timestamps or now - max(timestamps) > 600
            ]
            for ch_id in stale:
                _channel_failures.pop(ch_id, None)
            if stale:
                logger.debug(f"[Dsp] 清理频道失败记录: {len(stale)} 个")
        except Exception as e:
            logger.error(f"[Dsp] 清理频道失败记录异常: {e}")
        await asyncio.sleep(300)


async def _cleanup_channel_limiter_loop():
    """定期清理按频道限流器中超过 5 分钟未访问的条目"""
    while True:
        try:
            await _channel_limiter.cleanup_stale()
        except Exception as e:
            logger.error(f"[Dsp] 清理频道限流器异常: {e}")
        await asyncio.sleep(300)


async def _retry_dead_jobs():
    """每小时重试一次死信队列中的 job"""
    from database import get_and_reset_dead_jobs
    while True:
        try:
            dead_ids = await get_and_reset_dead_jobs(max_count=10)
            if dead_ids:
                logger.info(f"[Dsp] 死信重试: 重置 {len(dead_ids)} 个 job")
        except Exception as e:
            logger.error(f"[Dsp] 死信重试异常: {e}")
        await asyncio.sleep(3600)


def _record_channel_failure(channel_id: int):
    """记录频道路由发送失败时间戳,用于 Dsp 侧降级检测"""
    now = time.time()
    if channel_id not in _channel_failures:
        _channel_failures[channel_id] = []
    _channel_failures[channel_id].append(now)
    logger.debug(f"[Dsp] 频道 {channel_id} 发送失败记录(当前窗口内失败 {len(_channel_failures[channel_id])})")


async def _check_channel_degrade(channel_id: int):
    """检查某个频道是否在窗口内失败次数超过阈值,触发降级(兜底机制)
    
    作为 Mon Bot 的补充,仅在 Mon 不可用时作为兜底
    阈值设置更保守(3次/60秒),避免误触发短暂网络波动
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

    # 超过阈值触发降级
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
            logger.warning(f"[Dsp] 频道 {channel_id} 为 R100 槽位,跳过降级")
            _channel_failures.pop(channel_id, None)
            return

        await set_cell_status(slot_id, "lost")
        logger.warning(
            f"[Dsp] 频道降级触发: {slot_id} (channel={channel_id}) "
            f"status={current_status}→lost, "
            f"窗口内失败{fail_count}次/{_CHANNEL_FAILURE_WINDOW}s"
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
            buttons.append(InlineKeyboardButton(f"●{p}", callback_data="noop"))
        else:
            buttons.append(InlineKeyboardButton(str(p), callback_data=f"pg|{file_code}|{p}"))
    return buttons


# ─── 核心: worker 并发处理 jobs ───

async def process_queue(bot):
    """启动 worker 并发处理 jobs 队列。每个 worker 批量拉取后并发发送。"""
    num_workers = 2  # 2 个 worker 并发拉取 + 内部并发发送,足以支撑 25 并发上限
    logger.info(f"[Dsp] 启动 {num_workers} 个worker,并发上限 {_SEND_CONCURRENCY}")
    workers = [create_safe_task(_dsp_worker(bot, i), name=f"dsp-worker-{i}") for i in range(num_workers)]
    for w in workers:
        await w


def _raw_jobs_to_results(raw_jobs: list[dict]) -> list:
    """将 SQLite 原始行转换为 JobResult 列表"""
    from database import JobResult
    results = []
    for rj in raw_jobs:
        storage_ids = []
        raw_ids = rj.get("storage_msg_ids", "")
        if raw_ids:
            try:
                storage_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            except (json.JSONDecodeError, TypeError):
                storage_ids = []
        results.append(JobResult(
            job_id=rj["crdb_id"],
            code=rj["code"],
            target_user_id=rj["target_user_id"],
            storage_channel_id=rj["storage_channel_id"],
            storage_msg_ids=storage_ids,
            batch_file_meta=rj.get("batch_file_meta", ""),
            task_type=rj.get("task_type", "single"),
            protect_content=rj.get("protect_content", False),
            retry_count=rj.get("retry_count", 0),
        ))
    return results


async def _send_one_job(bot: Any, job, worker_id: int, store) -> bool:
    """发送单个 job（由 worker 并发调用，内部处理 semaphore/限速/失败）"""
    # 死信检查
    if job.retry_count >= 3:
        await store.update_local_job_status(job.job_id, "dead", dead_reason=f"重试次数已达上限({job.retry_count})")
        logger.warning(f"[Dsp-{worker_id}] 死信标记: job={job.job_id}, code={job.code}, retry={job.retry_count}")
        return False

    # 动态限速
    await dynamic_rate_limiter.acquire(get_pending_jobs_count_local)

    # 等待 semaphore
    try:
        await asyncio.wait_for(_send_semaphore.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        await store.update_local_job_status(job.job_id, "retried", retry_count=job.retry_count + 1)
        logger.debug(f"[Dsp-{worker_id}] semaphore 等待超时 job={job.job_id}")
        return False

    send_ok = False
    try:
        if job.task_type == "batch":
            send_ok = await _process_batch_job(bot, job, bot_id=worker_id)
        else:
            send_ok = await _process_single_job(bot, job, bot_id=worker_id)
        if send_ok:
            await _send_report_button(bot, job.target_user_id, job.code)
    except Exception as e:
        logger.error(f"[Dsp-{worker_id}] 发送异常(retry={job.retry_count}): {e}")
    finally:
        _send_semaphore.release()

    if not send_ok:
        if job.retry_count >= 3:
            await store.update_local_job_status(job.job_id, "dead", dead_reason=f"发送失败,已重试{job.retry_count}次: {job.code}")
        else:
            await store.update_local_job_status(job.job_id, "retried", retry_count=job.retry_count + 1)
            logger.info(f"[Dsp-{worker_id}] 重试入队: job={job.job_id}, code={job.code}, retry={job.retry_count}→{job.retry_count + 1}")

        # Dsp 侧降级检查
        for ch_id in list(_channel_failures.keys()):
            await _check_channel_degrade(ch_id)

    return send_ok


async def _dsp_worker(bot: Any, worker_id: int):
    """worker: 批量拉取 jobs → 并发发送
    D: 从本地 SQLite 队列消费,零 CRDB RU
    """
    from database.cache_store import get_cache_store
    store = get_cache_store()

    while True:
        try:
            # 批量拉取 pending jobs
            raw_jobs = await store.get_local_pending_jobs(10)
            if not raw_jobs:
                if await store.has_new_dsp_job():
                    continue
                await asyncio.sleep(2)
                continue

            # 转换为 JobResult 并标记 dispatched
            jobs = _raw_jobs_to_results(raw_jobs)
            for job in jobs:
                await store.mark_local_job_dispatched(job.job_id)

            # 并发发送这批 jobs
            tasks = [asyncio.create_task(_send_one_job(bot, job, worker_id, store)) for job in jobs]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"[Dsp-{worker_id}] 队列处理异常: {e}")
            await asyncio.sleep(1)


async def _process_single_job(bot, job, bot_id: int = 1):
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

    # ── 使用 copy_message 发送 ──
    resolved = await resolve_delivery_channel(job.storage_channel_id)
    success = await try_deliver(bot, job.target_user_id, resolved.channel_id, msg_id, protect_content=protect_content, bot_id=bot_id)

    if not success:
        _record_channel_failure(resolved.channel_id)
        # 环形降级:沿环找下一个可用频道,避免重复尝试同一频道
        tried = {resolved.channel_id}
        current_id = resolved.channel_id
        for _ in range(10):  # 最多尝试 10 个降级槽位(原 5 个不够)
            next_resolved = await resolve_delivery_channel(current_id)
            if next_resolved.channel_id in tried:
                break
            tried.add(next_resolved.channel_id)
            success = await try_deliver(bot, job.target_user_id, next_resolved.channel_id, msg_id, protect_content=protect_content, bot_id=bot_id)
            if success:
                break
            _record_channel_failure(next_resolved.channel_id)
            current_id = next_resolved.channel_id

    if success:
        logger.info(f"[Dsp] 发送成功: 用户 {job.target_user_id}, 码:{job.code}")
        metrics.send_success_count += 1
        await metrics.record_processed("dsp_bot")
        return True

    # 所有槽位均不可用,记录失败
    logger.error(f"[Dsp] 发送失败(所有槽位不可用): 码{job.code}, 尝试频道数{len(tried)}")
    metrics.send_fail_count += 1
    await metrics.record_error("dsp_bot")
    try:
        await safe_send_message(bot, chat_id=job.target_user_id, text="文件发送失败，请稍后重试或联系管理员")
    except Exception:
        pass
    return False


async def _process_batch_job(bot, job, bot_id: int = 1) -> bool:
    logger.info(
        f"[Dsp] 批量发送: 用户 {job.target_user_id}, "
        f"共{len(job.storage_msg_ids)} 个文件, 码:{job.code}"
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
        await _fallback_single_send(bot, job, bot_id=bot_id)
        return

    total_pages = (len(file_meta_list) + PAGE_SIZE - 1) // PAGE_SIZE
    page_key = None

    if total_pages > 1:
        # 加 user_id 避免多用户同码键冲突
        page_key = f"{job.code}:{job.target_user_id}"
        _pagination_states[page_key] = {
            "channel_msg_ids": job.storage_msg_ids,
            "batch_file_meta": file_meta_list,
            "total_pages": total_pages,
            "chat_id": job.target_user_id,
            "storage_channel_id": job.storage_channel_id,
            "created_at": time.time(),
            "file_code": job.code,
            "target_user_id": job.target_user_id,
        }

    result = await _send_page(
        bot, job.target_user_id, job.code,
        file_meta_list, page=1, total_pages=total_pages,
        storage_channel_id=job.storage_channel_id,
        page_key=page_key if total_pages > 1 else None,
    )
    return result


async def _fallback_single_send(bot, job, bot_id: int = 1):
    protect_content = getattr(job, "protect_content", False)
    for i, mid in enumerate(job.storage_msg_ids):
        # 使用信号量控制并发,避免 Telegram API 限流
        try:
            await asyncio.wait_for(_send_semaphore.acquire(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[Dsp] 信号量超时,跳过消息 {mid}")
            continue
        try:
            resolved = await resolve_delivery_channel(job.storage_channel_id)
            if await try_deliver(bot, job.target_user_id, resolved.channel_id, mid, protect_content=protect_content, bot_id=bot_id):
                metrics.send_success_count += 1
            else:
                metrics.send_fail_count += 1
        finally:
            _send_semaphore.release()
        # 每条消息之间间隔 0.15s,避免同一个频道/同用户超过限制
        if i < len(job.storage_msg_ids) - 1:
            await asyncio.sleep(0.15)
    await metrics.record_processed("dsp_bot")


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages, storage_channel_id=None, page_key=None) -> bool:
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = file_meta_list[start:end]

    input_media = [_build_input_media(meta) for meta in page_items]
    try:
        await safe_send_media_group(bot, chat_id=chat_id, media=input_media)
        logger.info(f"[Dsp] 媒体组发送成功: {chat_id}, 码:{file_code}, 第{page}/{total_pages}页({len(input_media)}张)")
        metrics.send_success_count += 1
        await metrics.record_processed("dsp_bot")
    except Exception as e:
        logger.error(f"[Dsp] 媒体组发送失败: {e}")
        metrics.send_fail_count += 1
        await metrics.record_error("dsp_bot")
        try:
            await safe_send_message(bot, chat_id=chat_id, text="文件发送失败，请稍后重试或联系管理员")
        except Exception:
            pass
        return False

    if total_pages > 1 and page < total_pages:
        # 使用 page_key 避免多用户同码键冲突
        pk = page_key or file_code
        keyboard = _build_pagination_keyboard(pk, page, total_pages)
        total_files = len(file_meta_list)
        sent_msg = await safe_send_message(
            bot, chat_id=chat_id,
            text=f"[{page}/{total_pages} 页] 共{total_files} 个文件",
            reply_markup=keyboard,
        )
        state = _pagination_states.get(pk)
        if state and sent_msg:
            state["last_pagination_msg_id"] = sent_msg.message_id
        # 翻页提示之间间隔 0.3s,避免用户聊天被消息淹
        await asyncio.sleep(0.3)

    return True


# ─── 举报按钮 ───

async def _send_report_button(bot, chat_id: int, file_code: str):
    """发送成功后追加举报按钮"""
    try:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ 举报", callback_data=f"report_req|{file_code}")
        ]])
        await safe_send_message(bot, chat_id=chat_id, text="文件已送达", reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"[Dsp] 发送举报按钮失败: {e}")


_report_debounce: dict[str, float] = {}

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户点击举报按钮，推送消息给管理员"""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("report_req|"):
        return

    file_code = data.split("|", 1)[1]
    reporter = update.effective_user
    if not reporter:
        return

    # 60 秒防抖
    key = f"{reporter.id}:{file_code}"
    now = time.time()
    if key in _report_debounce and now - _report_debounce[key] < 60:
        await query.answer("已提交举报，请勿重复操作", show_alert=True)
        return
    _report_debounce[key] = now

    # 查文件记录获取上传者
    try:
        # A2: 走缓存,避免每次直查 CRDB
        file_record = await get_file_record_cached(file_code)
        if not file_record:
            await query.answer("文件记录不存在", show_alert=True)
            return
    except Exception as e:
        logger.error(f"[Dsp][report] 查询文件失败: {e}")
        await query.answer("系统繁忙，请稍后重试", show_alert=True)
        return

    uploader_id = file_record.get("uploader_id", 0)
    reporter_username = f"@{reporter.username}" if reporter.username else str(reporter.id)

    report_text = (
        f"🚨 文件举报\n\n"
        f"📁 文件码: {file_code}\n"
        f"👤 上传者: {uploader_id}\n"
        f"👤 举报人: {reporter.id} ({reporter_username})\n"
        f"📋 来源: Dsp Bot\n"
        f"⏰ 时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 封禁上传者", callback_data=f"report:ban|{uploader_id}")],
        [InlineKeyboardButton("🔗 脱钩文件码", callback_data=f"report:detach|{file_code}")],
        [InlineKeyboardButton("🚫 限制举报人", callback_data=f"report:block|{file_code}|{reporter.id}")],
        [InlineKeyboardButton("✅ 忽略", callback_data="report:ignore")],
    ])

    try:
        admin_token = settings.ADMIN_BOT_TOKEN
        admin_chat_id = settings.ADMIN_TELEGRAM_ID
        if admin_token and admin_chat_id:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=report_text,
                reply_markup=keyboard,
            )
        await query.answer("举报已提交，管理员将尽快处理", show_alert=True)
    except Exception as e:
        logger.error(f"[Dsp][report] 推送管理员失败: {e}")
        await query.answer("举报提交失败，请稍后重试", show_alert=True)


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

    page_key = parts[1]  # 格式: file_code:user_id
    try:
        page = int(parts[2])
    except ValueError:
        await query.answer()
        return

    # 使用 get 保留状态（支持多页翻页），结束或过期时才清理
    state = _pagination_states.get(page_key)
    if not state:
        await query.answer("会话已过期,请重新发送文件码。", show_alert=True)
        return

    # 检查 TTL
    if time.time() - state.get("created_at", 0) > _PAGE_STATE_TTL:
        _pagination_states.pop(page_key, None)
        await query.answer("会话已过期,请重新发送文件码。", show_alert=True)
        return

    file_meta_list = state["batch_file_meta"]
    total_pages = state["total_pages"]
    file_code = state.get("file_code", page_key)

    if page < 1 or page > total_pages:
        await query.answer("无效的页码")
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
        page_key=page_key,
    )

    if page >= total_pages:
        _pagination_states.pop(page_key, None)

    await query.answer()


# ─── 运行 ───

async def _init():
    from database import init_db
    await init_db()


async def _async_main():
    await _init()
    from database.cache_store import report_bot_heartbeat
    await report_bot_heartbeat("dsp_bot")

    logger.info("[Dsp] 启动发送机器人 (Dsp Bot)...")
    app = Application.builder().token(TOKEN).build()
    bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pagination_callback))
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^report_req\|"))

    await metrics.ping_bot("dsp_bot")

    async def health_ping():
        while True:
            await metrics.ping_bot("dsp_bot")
            await asyncio.sleep(30)

    # H: 启动时一次性从 CRDB 同步，补齐 Dsp 离线期间遗漏的 job
    async def startup_sync():
        from database.session import sync_jobs_from_crdb_to_sqlite
        from database.cache_store import get_cache_store
        store = get_cache_store()
        logger.info("[Dsp] 启动同步: 从 CRDB 补齐可能遗漏的 jobs...")
        try:
            await sync_jobs_from_crdb_to_sqlite(100)
        except Exception as e:
            logger.warning(f"[Dsp] 启动同步异常: {e}")
        # 之后每 5 分钟轻量同步 + 清理旧记录
        while True:
            await asyncio.sleep(300)
            try:
                await sync_jobs_from_crdb_to_sqlite(100)
                await store.cleanup_local_jobs(7)
            except Exception as e:
                logger.debug(f"[Dsp] 周期同步异常: {e}")

    # D: Sync Back - 每 30 秒同步本地状态变更回 CRDB
    async def sync_back_loop():
        from database.session import sync_local_jobs_to_crdb
        while True:
            try:
                await sync_local_jobs_to_crdb()
            except Exception as e:
                logger.debug(f"[SyncBack] 同步异常: {e}")
            await asyncio.sleep(30)

    loop = asyncio.get_running_loop()
    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(startup_sync(), name="startup-sync")          # H: 启动同步 + 周期兜底
    create_safe_task(sync_back_loop(), name="sync-back")          # D: 新增
    create_safe_task(process_queue(bot), name="process-queue")
    create_safe_task(_cleanup_page_states(), name="cleanup-page-states")
    create_safe_task(_cleanup_channel_failures(), name="cleanup-channel-failures")
    create_safe_task(_cleanup_channel_limiter_loop(), name="cleanup-channel-limiter")
    create_safe_task(_retry_dead_jobs(), name="retry-dead-jobs")
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
    """启动 Dsp Bot(使用 asyncio.run 标准模式)"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
