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
from database import get_file_record_cached, get_pending_jobs_count_local
from storage.delivery_resolver import resolve_delivery_channel, try_deliver, try_deliver_batch, invalidate_cell_cache
from utils.per_channel_limiter import _channel_limiter
from utils.monitor import metrics
from utils.dynamic_rate_limiter import dynamic_rate_limiter
from utils.force_join import check_force_join, three_bot_reminder, common_faq
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
_cf_lock = asyncio.Lock()
_CHANNEL_FAILURE_THRESHOLD = settings.CHANNEL_FAILURE_THRESHOLD  # 60 秒内失败 N 次触发降级
_CHANNEL_FAILURE_WINDOW = settings.CHANNEL_FAILURE_WINDOW  # 统计窗口(秒)

_pagination_states: dict[str, dict] = {}
_pg_lock = asyncio.Lock()
_PAGE_STATE_TTL = 300  # 5 分钟过期清理

# P1-2: 已成功投递的 msg_id 跟踪,避免重试时重复投递
# job_id -> set(storage_msg_id),job 完成或死信时清理
# R35 §21.2: 保留为内存缓存层,delivery_receipts 表为权威持久化层(双写)
_sent_msg_tracker: dict[int, set[int]] = {}


# ─── R35 §21.2 / §22: delivery_receipts 持久化 + ReplicaAwareResolver 接线 ───


async def _upsert_delivery_receipt_safe(
    store, job_id: int, source_msg_id: int, target_user_id: int,
    status: str = "PENDING", sent_msg_id: int | None = None,
    media_group_id: str = "", group_receipt_id: str = "",
) -> None:
    """异常安全地写入/更新投递回执(失败仅记录 warning,不传播到主流程)。

    作为 _sent_msg_tracker 的持久化权威层:
    - 投递前写 PENDING
    - 投递成功后由 _confirm_delivery_receipt_safe 升级为 CONFIRMED
    - 投递失败后由 _mark_delivery_failed_safe 标记 FAILED
    """
    if store is None:
        return
    try:
        await store.upsert_delivery_receipt(
            job_id, source_msg_id, target_user_id,
            sent_msg_id=sent_msg_id,
            media_group_id=media_group_id,
            group_receipt_id=group_receipt_id,
            status=status,
        )
    except Exception as e:
        logger.warning(
            f"[Dsp] upsert_delivery_receipt 失败(忽略) "
            f"job={job_id}, msg={source_msg_id}, status={status}: {e}"
        )


async def _confirm_delivery_receipt_safe(
    store, job_id: int, source_msg_id: int, sent_msg_id: int,
) -> None:
    """异常安全地确认投递回执(status='CONFIRMED', confirmed_at=now)。

    与 _sent_msg_tracker.setdefault(...).add(mid) 双写,保证向后兼容。
    """
    if store is None:
        return
    try:
        await store.confirm_delivery_receipt(job_id, source_msg_id, sent_msg_id)
    except Exception as e:
        logger.warning(
            f"[Dsp] confirm_delivery_receipt 失败(忽略) "
            f"job={job_id}, msg={source_msg_id}: {e}"
        )


async def _mark_delivery_failed_safe(
    store, job_id: int, source_msg_id: int, reason: str,
) -> None:
    """异常安全地标记投递失败(status='FAILED', attempts+1)。"""
    if store is None:
        return
    try:
        await store.mark_delivery_failed(job_id, source_msg_id, reason)
    except Exception as e:
        logger.warning(
            f"[Dsp] mark_delivery_failed 失败(忽略) "
            f"job={job_id}, msg={source_msg_id}: {e}"
        )


def _extract_replica_info(job) -> tuple[str, int | None, str, bool]:
    """从 job 提取 (file_unique_id, group_id, media_group_id, is_structured_new_job) 供 ReplicaAwareResolver 使用。

    R36 B0-1: 优先从结构化字段(job.group_id / job.file_unique_id / job.media_group_id)读取,
    缺失时 fallback 到 batch_file_meta JSON 解析(向后兼容旧 job)。

    返回 (file_unique_id, group_id, media_group_id, is_structured_new_job):
    - file_unique_id: Telegram 文件唯一标识(跨 bot 稳定去重键)
    - group_id: 拓扑组号(1-5),None 表示未知(旧 job 或解析失败)
    - media_group_id: Telegram 媒体组 ID(相册分组键)
    - is_structured_new_job: True 表示这是新格式 job(结构化字段已写入),
      调用方应严格按 fail-closed 处理(缺 group_id 进入 retry,不静默 fallback 到拓扑猜测)
    """
    # 1. 优先从结构化字段读取(R36 B0-1)
    fuid_struct = getattr(job, "file_unique_id", "") or ""
    gid_struct = getattr(job, "group_id", 0) or 0
    mgid_struct = getattr(job, "media_group_id", "") or ""

    # 判断是否为新格式 job: file_unique_id 结构化字段非空,或 group_id > 0
    is_structured_new = bool(fuid_struct) or gid_struct > 0
    if is_structured_new:
        # 新格式 job: group_id > 0 时返回 int,否则返回 None(数据不完整)
        return fuid_struct, (gid_struct if gid_struct > 0 else None), mgid_struct, True

    # 2. 旧 job: fallback 到 batch_file_meta JSON 解析(向后兼容)
    raw = getattr(job, "batch_file_meta", None)
    if not raw:
        return "", None, "", False
    # 兼容 str(JSON) / list 两种格式
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except Exception:
            return "", None, "", False
    elif isinstance(raw, list):
        items = raw
    else:
        return "", None, "", False
    if not items or not isinstance(items, list):
        return "", None, "", False
    first = items[0] if isinstance(items[0], dict) else {}
    fuid = first.get("file_unique_id", "") or ""
    # 旧 job batch_file_meta 中可能已包含 group_id(up_bot 已升级但 SQLite 列未升级的过渡态)
    gid_raw = first.get("group_id")
    gid: int | None = None
    if gid_raw is not None:
        try:
            gid_int = int(gid_raw)
            gid = gid_int if gid_int > 0 else None
        except (TypeError, ValueError):
            gid = None
    mgid = first.get("media_group_id", "") or ""
    return fuid, gid, mgid, False


async def _try_replica_aware_resolve(
    store, file_unique_id: str, group_id: int | None,
    preferred_channels: list[int] | None = None,
    exclude_channels: set[int] | None = None,
) -> tuple[int, int] | None:
    """尝试使用 ReplicaAwareResolver 按 manifest 副本解析频道。

    R35 §22: Dsp 不再按拓扑猜测频道,优先以 manifest 副本为准。
    当且仅当输入包含 file_unique_id 和 group_id 时启用;否则返回 None,由调用方 fallback。
    Manifest 查询失败时返回 None,由调用方走 fail-closed 重试路径(普通下载降级)。
    """
    if not file_unique_id or group_id is None or store is None:
        return None
    try:
        from services.delivery_resolver import ReplicaAwareResolver
        resolver = ReplicaAwareResolver(store)
        # P2-3: 默认 fail_closed=True(商用安全优先),查询失败返回 None
        # 由调用方走 fail-closed 重试路径(普通下载降级)
        return await resolver.resolve_channel_for_file(
            file_unique_id, group_id,
            preferred_channels=preferred_channels,
            exclude_channels=exclude_channels,
            fail_closed=True,
        )
    except Exception as e:
        logger.warning(
            f"[Dsp] ReplicaAwareResolver 异常(降级拓扑解析) "
            f"fuid={file_unique_id}, group={group_id}: {e}"
        )
        return None


def _get_store_safe():
    """安全获取 cache_store 实例(失败返回 None,不抛异常)。"""
    try:
        from database.cache_store import get_cache_store
        return get_cache_store()
    except Exception as e:
        logger.warning(f"[Dsp] 获取 cache_store 失败(忽略 delivery_receipts 双写): {e}")
        return None


async def _cleanup_page_states():
    """定期清理过期的分页状态"""
    while True:
        try:
            now = time.time()
            async with _pg_lock:
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
            async with _cf_lock:
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
    """每小时重试本地死信队列中的 job（0 RU）"""
    from database.cache_store import get_cache_store
    from utils.redis_client import xadd_job
    store = get_cache_store()
    while True:
        try:
            dead_jobs = await store.get_local_dead_jobs(limit=10, max_dead_retry=2)
            if dead_jobs:
                for dj in dead_jobs:
                    await store.retry_local_dead_job(dj["crdb_id"])
                    # C1: 重试入队后重新投递到 Redis Stream,避免 Redis 模式下重试 job 饥饿
                    try:
                        await xadd_job(dj["crdb_id"])
                    except Exception:
                        pass  # 降级:notify_dsp_new_job 已写入 dsp_notify
                logger.info(f"[Dsp] 本地死信重试: 重置 {len(dead_jobs)} 个 job")
        except Exception as e:
            logger.error(f"[Dsp] 死信重试异常: {e}")
        await asyncio.sleep(3600)


async def _reclaim_stale_pel_loop():
    """N-R1: 周期性回收 Redis PEL 中的孤儿条目(worker 崩溃后未 XACK 的消息)。

    SQLite 的 reclaim_stale_dispatched(600s) 已重投 job 到 pending,
    这里只需 XACK 丢弃 PEL 中的陈旧条目,防止 PEL 无限膨胀。
    """
    from utils.redis_client import xautoclaim_stale_jobs, xack_job
    while True:
        try:
            stale = await xautoclaim_stale_jobs(min_idle_ms=600000, count=20)
            for msg_id, _fields in stale:
                await xack_job(msg_id)  # 丢弃孤儿条目,job 已由 SQLite 兜底重投
            if stale:
                logger.info(f"[Dsp] PEL 回收: 清理 {len(stale)} 个孤儿条目")
        except Exception as e:
            logger.warning(f"[Dsp] PEL 回收异常: {e}")
        await asyncio.sleep(300)


async def _watch_cells_change():
    """PRE-02: 定期检查 cells_change_notify，发现 mon_bot 轮转/降级后立即失效 delivery_resolver 缓存。

    mon_bot 通过 _bump_cells_version 写入 cells_change_notify 表（跨进程 SQLite 共享）。
    本任务每 5 秒检查一次，若有变更则调用 invalidate_cell_cache() 清空 per-entry 缓存，
    强制 resolve_delivery_channel 重新读取 cells_local，避免投递到已降级的频道。
    """
    from database.cache_store import get_cache_store
    store = get_cache_store()
    last_version = 0
    while True:
        try:
            changed, new_version = await store.has_cells_change(last_version)
            if changed:
                invalidate_cell_cache()  # 清空全部 per-entry 缓存
                last_version = new_version
                logger.info(f"[Dsp] cells 变更检测(version={new_version})，已失效 delivery_resolver 缓存")
        except Exception as e:
            logger.warning(f"[Dsp] cells 变更检测异常: {e}")
        await asyncio.sleep(5)


async def _record_channel_failure(channel_id: int):
    """记录频道路由发送失败时间戳,用于 Dsp 侧降级检测"""
    now = time.time()
    async with _cf_lock:
        if channel_id not in _channel_failures:
            _channel_failures[channel_id] = []
        _channel_failures[channel_id].append(now)
    logger.debug(f"[Dsp] 频道 {channel_id} 发送失败记录(当前窗口内失败 {len(_channel_failures[channel_id])})")


async def _check_channel_degrade(channel_id: int):
    """检查某个频道是否在窗口内失败次数超过阈值,触发降级(兜底机制)
    
    作为 Mon Bot 的补充,仅在 Mon 不可用时作为兜底
    阈值设置更保守(3次/60秒),避免误触发短暂网络波动
    """
    async with _cf_lock:
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

    # 超过阈值触发降级(兜底机制,Dsp 侧作为 Mon 的补充)
    try:
        from database.session import get_cell_by_channel_local
        from database.cache_store import get_cache_store
        cell = await get_cell_by_channel_local(channel_id)
        if not cell:
            logger.warning(f"[Dsp] 降级失败: 频道 {channel_id} 不在 cells 中")
            async with _cf_lock:
                _channel_failures.pop(channel_id, None)
            return

        slot_id = cell.get("slot_id")
        current_status = cell.get("status", "")
        if current_status in ("lost", "shadow2"):
            async with _cf_lock:
                _channel_failures.pop(channel_id, None)
            return
        if current_status == "r100":
            logger.warning(f"[Dsp] 频道 {channel_id} 为 R100 槽位,跳过降级")
            async with _cf_lock:
                _channel_failures.pop(channel_id, None)
            return

        store = get_cache_store()
        # 类型安全:degrade_count 可能是 None/str,统一转 int
        try:
            raw_dc = cell.get("degrade_count", 0)
            new_degrade_count = int(raw_dc) + 1 if raw_dc is not None else 1
        except (TypeError, ValueError):
            new_degrade_count = 1
        await store.update_cell_fields_local(slot_id, {
            "status": "lost",
            "degrade_count": new_degrade_count,
            "next_active_chat_id": None,
        }, mark_dirty=True)
        logger.warning(
            f"[Dsp] 频道降级触发(兜底): {slot_id} (channel={channel_id}) "
            f"status={current_status}→lost, fail={fail_count}"
        )
        async with _cf_lock:
            _channel_failures.pop(channel_id, None)
    except Exception as e:
        logger.error(f"[Dsp] 频道降级异常 (channel={channel_id}): {e}")


async def _build_delivery_caption(file_code: str, total_count: int = 1) -> str:
    """构建发送给用户的媒体组/文件 caption，包含文件总数、备注、文件码。"""
    lines = [f"文件获取完毕 文件总数：{total_count}"]

    try:
        record = await get_file_record_cached(file_code)
        note = ""
        if record:
            note_val = record.get("note")
            # note 可能是 str 或 dict（CRDB JSONB 反序列化），统一转为 str 检查
            if isinstance(note_val, dict):
                # dict 类型：检查是否是外部中继的内部标记
                _nt = None
                for k, v in note_val.items():
                    if k == "type":
                        _nt = v
                if _nt != "external":
                    # 非外部标记的 dict，尝试提取有意义的备注文本
                    note = str(note_val)
            elif isinstance(note_val, str):
                note_raw = note_val.strip()
                # 外部中继的 note 是 JSON 对象（{"type":"external",...}），不作为备注显示
                # 用字符串检查避免 orjson hash 表异常导致 dict.get() 返回 None
                if note_raw and not ('"type"' in note_raw and '"external"' in note_raw):
                    note = note_raw
        if note:
            lines.append(f"备注：{note}")
    except Exception:
        pass

    lines.append(f"文件码：{file_code}")
    return "\n".join(lines)


async def _edit_sent_caption(bot: Any, chat_id: int, message_id: int, caption: str):
    """给已发送的消息/媒体组第一条消息编辑 caption，失败静默。"""
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
    except Exception as e:
        logger.debug(f"[Dsp] edit_caption 失败(非致命, msg={message_id}): {e}")


async def _should_preserve_caption(file_code: str) -> bool:
    """检查是否应保留第三方 Bot 原始 caption（不被标准模板覆盖）。
    当外部中继的 note 中标记 preserve_caption=True 时返回 True。
    用字符串检查避免 orjson hash 表异常导致 dict.get() 返回 None。"""
    try:
        record = await get_file_record_cached(file_code)
        if not record:
            return False
        note = record.get("note") or ""
        if not note or not isinstance(note, str):
            return False
        return '"preserve_caption"' in note and '"true"' in note.lower()
    except Exception:
        return False


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
    # P2: 用 supervisor 监控 worker,worker 异常退出时记录错误并结束
    workers = [create_safe_task(_dsp_worker(bot, i), name=f"dsp-worker-{i}") for i in range(num_workers)]
    try:
        for w in workers:
            await w
    finally:
        # worker 异常退出时取消其他 worker,避免部分运行
        for w in workers:
            if not w.done():
                w.cancel()
        # 记录异常退出的 worker
        for i, w in enumerate(workers):
            if w.done() and w.exception():
                logger.error(f"[Dsp] worker-{i} 异常退出: {w.exception()}")


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
            group_id=rj.get("group_id", 0) or 0,
            file_unique_id=rj.get("file_unique_id", "") or "",
            media_group_id=rj.get("media_group_id", "") or "",
        ))
    return results


async def _send_one_job(bot: Any, job, worker_id: int, store) -> bool:
    """发送单个 job（由 worker 并发调用，内部处理 semaphore/限速/失败）"""
    # 死信检查
    if job.retry_count >= 3:
        _sent_msg_tracker.pop(job.job_id, None)
        await store.update_local_job_status(job.job_id, "dead", dead_reason=f"重试次数已达上限({job.retry_count})")
        logger.warning(f"[Dsp-{worker_id}] 死信标记: job={job.job_id}, code={job.code}, retry={job.retry_count}")
        return False

    # 检查用户是否已向 dsp 发送 /start，未启动则标记 waiting_start（不计入重试）
    if not await store.is_user_started(job.target_user_id, "dsp"):
        await store.mark_job_waiting_start(job.job_id)
        logger.info(f"[Dsp-{worker_id}] 用户 {job.target_user_id} 未 /start dsp，job={job.job_id} 标记 waiting_start")
        return False

    # 动态限速
    await dynamic_rate_limiter.acquire(get_pending_jobs_count_local)

    # P1-2: 过滤已成功投递的 msg_id,避免重试时重复投递
    # R35 §21.2: 优先以持久化 delivery_receipts 为权威源(进程重启后内存丢失仍可恢复),
    #            _sent_msg_tracker 仅作内存加速缓存
    sent_set = _sent_msg_tracker.setdefault(job.job_id, set())
    try:
        persisted_sent_ids = await store.get_sent_msg_ids_for_job(job.job_id)
    except Exception as e:
        logger.warning(f"[Dsp-{worker_id}] 读取持久化 receipts 失败(降级内存): {e}")
        persisted_sent_ids = []
    if persisted_sent_ids:
        # 合并到内存缓存(后续过滤以合并后的集合为准)
        sent_set.update(persisted_sent_ids)
    original_ids = list(job.storage_msg_ids)
    if sent_set:
        job.storage_msg_ids = [mid for mid in job.storage_msg_ids if mid not in sent_set]
        if not job.storage_msg_ids:
            # 所有 msg_id 已成功投递,标记完成
            logger.info(f"[Dsp-{worker_id}] job={job.job_id} 所有文件已投递,跳过重试")
            _sent_msg_tracker.pop(job.job_id, None)
            await store.update_local_job_status(job.job_id, "done")
            await _send_report_button(bot, job.target_user_id, job.code)
            return True
        logger.info(f"[Dsp-{worker_id}] job={job.job_id} 跳过已投递 {len(original_ids) - len(job.storage_msg_ids)}/{len(original_ids)} 个,剩余 {len(job.storage_msg_ids)} 个")

    # 等待 semaphore
    try:
        await asyncio.wait_for(_send_semaphore.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        # P2: 信号量超时后显式重试入队,避免 job 滞留 dispatched 最多 600s
        logger.warning(f"[Dsp-{worker_id}] 信号量获取超时,重试入队: job={job.job_id}")
        new_retry = job.retry_count + 1
        if new_retry >= 3:
            _sent_msg_tracker.pop(job.job_id, None)
            await store.update_local_job_status(job.job_id, "dead", dead_reason=f"信号量获取超时,已重试{job.retry_count}次")
        else:
            await store.retry_local_job(job.job_id, new_retry)
            try:
                from utils.redis_client import xadd_job
                await xadd_job(job.job_id)
            except Exception:
                pass
        return False

    send_ok = False
    try:
        if job.task_type == "batch":
            send_ok = await _process_batch_job(bot, job, bot_id=worker_id)
        else:
            send_ok = await _process_single_job(bot, job, bot_id=worker_id)
        if send_ok:
            # 状态更新失败时不能让 send_ok 保持 True,否则 job 残留在 dispatched 状态,
            # 600秒后会被 reclaim_stale_dispatched 回收重新派发,导致用户收到重复文件
            try:
                await store.update_local_job_status(job.job_id, "done")
            except Exception as status_err:
                logger.error(
                    f"[Dsp-{worker_id}] job={job.job_id} 状态更新失败: {status_err}"
                    f",重置 send_ok=False 触发重试,避免重复投递"
                )
                send_ok = False
                raise
            _sent_msg_tracker.pop(job.job_id, None)
            await _send_report_button(bot, job.target_user_id, job.code)
    except Exception as e:
        logger.error(f"[Dsp-{worker_id}] 发送异常(retry={job.retry_count}): {e}")
    finally:
        _send_semaphore.release()

    if not send_ok:
        new_retry = job.retry_count + 1
        if new_retry >= 3:
            _sent_msg_tracker.pop(job.job_id, None)
            await store.update_local_job_status(job.job_id, "dead", dead_reason=f"发送失败,已重试{job.retry_count}次: {job.code}")
        else:
            await store.retry_local_job(job.job_id, new_retry)
            # C1: 重试入队后重新投递到 Redis Stream,避免 Redis 模式下重试 job 饥饿
            try:
                from utils.redis_client import xadd_job
                await xadd_job(job.job_id)
            except Exception:
                pass  # 降级:notify_dsp_new_job 已写入 dsp_notify
            logger.info(f"[Dsp-{worker_id}] 重试入队: job={job.job_id}, code={job.code}, retry={job.retry_count}→{new_retry}")

        # Dsp 侧降级检查
        async with _cf_lock:
            ch_ids = list(_channel_failures.keys())
        for ch_id in ch_ids:
            await _check_channel_degrade(ch_id)

    return send_ok


async def _dsp_worker(bot: Any, worker_id: int):
    """worker: 批量拉取 jobs → 并发发送
    D: 从本地 SQLite 队列消费,零 CRDB RU
    C1: 优先使用 Redis Stream XREADGROUP(事件驱动),降级到 dsp_notify 轮询
    """
    from database.cache_store import get_cache_store
    from utils.redis_client import xreadgroup_jobs, xack_job, get_redis, get_consumer_name
    store = get_cache_store()
    consumer_name = f"{get_consumer_name()}-{worker_id}"
    round_counter = 0

    while True:
        round_counter += 1
        try:
            # C1: 优先从 Redis Stream 读取(事件驱动,BLOCK 5s)
            redis = await get_redis()
            redis_messages = []
            if redis:
                redis_messages = await xreadgroup_jobs(consumer_name, count=10, block=5000)

            if redis_messages:
                # Redis 模式:按 crdb_id 查 job + CAS 认领 + 并发发送 + XACK
                claimed_jobs = []
                msg_id_map = {}  # crdb_id → msg_id
                for msg_id, fields in redis_messages:
                    try:
                        crdb_id = int(fields.get("crdb_id", 0))
                    except (ValueError, TypeError):
                        crdb_id = 0
                    if crdb_id <= 0:
                        await xack_job(msg_id)
                        continue
                    # CAS 认领
                    if await store.mark_local_job_dispatched(crdb_id):
                        job_dict = await store.get_local_job_by_crdb_id(crdb_id)
                        if job_dict:
                            job = _raw_jobs_to_results([job_dict])[0]
                            claimed_jobs.append(job)
                            msg_id_map[crdb_id] = msg_id
                    else:
                        # 已被认领或不存在,ACK 避免重复消费
                        await xack_job(msg_id)

                if claimed_jobs:
                    tasks = [asyncio.create_task(_send_one_job(bot, job, worker_id, store)) for job in claimed_jobs]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    # 发送后 XACK(无论成功失败,重试会重新 XADD)
                    for job in claimed_jobs:
                        msg_id = msg_id_map.get(job.job_id)
                        if msg_id:
                            await xack_job(msg_id)

                # C1 兜底:Redis 消息不足时补充 SQLite pending(防止 MAXLEN 截断的 job 饥饿)
                # 当 Redis 拉取的消息少于 count(10),说明 Stream 已无积压,此时检查 SQLite
                if len(redis_messages) < 10:
                    fallback_jobs = await store.get_local_pending_jobs(5)
                    if fallback_jobs:
                        fb_claimed = []
                        for rj in fallback_jobs:
                            if await store.mark_local_job_dispatched(rj["crdb_id"]):
                                fb_claimed.append(_raw_jobs_to_results([rj])[0])
                        if fb_claimed:
                            fb_tasks = [asyncio.create_task(_send_one_job(bot, j, worker_id, store)) for j in fb_claimed]
                            await asyncio.gather(*fb_tasks, return_exceptions=True)
                # N-R3: 每 50 轮强制轮询 SQLite pending,防止持续满载下被 MAXLEN 裁剪的 job 饥饿
                if len(redis_messages) >= 10 and (round_counter % 50 == 0):
                    # 强制补充 SQLite pending 检查
                    fallback_jobs = await store.get_local_pending_jobs(5)
                    if fallback_jobs:
                        fb_claimed = []
                        for rj in fallback_jobs:
                            if await store.mark_local_job_dispatched(rj["crdb_id"]):
                                fb_claimed.append(_raw_jobs_to_results([rj])[0])
                        if fb_claimed:
                            fb_tasks = [asyncio.create_task(_send_one_job(bot, j, worker_id, store)) for j in fb_claimed]
                            await asyncio.gather(*fb_tasks, return_exceptions=True)
                continue

            # 降级/兜底:SQLite 轮询(处理 Redis 超时无消息 或 Redis 不可用)
            raw_jobs = await store.get_local_pending_jobs(10)
            if not raw_jobs:
                if not redis:
                    # Redis 不可用时才等待通知(Redis 可用但无消息时不等待,继续循环)
                    await store.wait_for_dsp_job(timeout=2.0)
                continue

            jobs = _raw_jobs_to_results(raw_jobs)
            # CAS 认领:仅发送成功标记为 dispatched 的 job,防止多 worker 重复发送
            claimed_jobs = []
            for job in jobs:
                if await store.mark_local_job_dispatched(job.job_id):
                    claimed_jobs.append(job)

            if not claimed_jobs:
                continue
            tasks = [asyncio.create_task(_send_one_job(bot, job, worker_id, store)) for job in claimed_jobs]
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
        await metrics.record_send_fail()
        return False

    # protect_content 已从 jobs 表直接获取,无需再查 file_records
    protect_content = getattr(job, "protect_content", False)

    # R35 §21.2: 投递前写 PENDING receipt + 幂等检查(已 SENT/CONFIRMED 则跳过)
    store = _get_store_safe()
    if store is not None:
        try:
            existing = await store.get_delivery_receipts_by_job(job.job_id)
            already_sent = {
                r["source_msg_id"] for r in existing
                if r.get("status") in ("SENT", "CONFIRMED")
            }
            if msg_id in already_sent:
                logger.info(
                    f"[Dsp] msg={msg_id} 已有持久化投递记录,跳过 job={job.job_id}"
                )
                _sent_msg_tracker.setdefault(job.job_id, set()).add(msg_id)
                return True
        except Exception as e:
            logger.warning(f"[Dsp] 检查 delivery_receipt 幂等失败(忽略): {e}")
    await _upsert_delivery_receipt_safe(
        store, job.job_id, msg_id, job.target_user_id, status="PENDING"
    )

    # R35 §22: 优先尝试 ReplicaAwareResolver(按 manifest 副本解析,fail-closed)
    # R36 B0-1: 优先从结构化字段读取,使 Resolver 成为真实投递主路径
    fuid, gid, mgid, is_structured_new = _extract_replica_info(job)
    sent_msg_id: int | None = None
    tried: set[int] = set()
    if fuid and gid is not None:
        replica = await _try_replica_aware_resolve(
            store, fuid, gid,
            preferred_channels=[job.storage_channel_id],
        )
        if replica is not None:
            replica_channel, replica_msg_id = replica
            logger.info(
                f"[Dsp] ReplicaAwareResolver 命中 "
                f"ch={replica_channel}, msg={replica_msg_id}, fuid={fuid}"
            )
            tried.add(replica_channel)
            sent_msg_id = await try_deliver(
                bot, job.target_user_id, replica_channel, replica_msg_id,
                protect_content=protect_content, bot_id=bot_id,
                original_channel_id=job.storage_channel_id,
            )
        else:
            # R36 B0-1: Resolver 查询失败(无副本/manifest 不可达),
            # 对新格式 job(fuid 已知)严格 fail-closed: 进入 retry 等待 manifest 同步,
            # 不静默 fallback 到拓扑猜测(避免投递到无副本的频道导致用户收到错误文件)
            if is_structured_new:
                logger.error(
                    f"[Dsp] 新 job Resolver 失败(fail-closed),进入 retry: "
                    f"job={job.job_id}, code={job.code}, fuid={fuid}, gid={gid}"
                )
                await _mark_delivery_failed_safe(
                    store, job.job_id, msg_id,
                    reason=f"resolver_failed_fail_closed fuid={fuid} gid={gid}",
                )
                return False  # 触发 retry_local_job,不 fallback 到拓扑猜测

    # R36 B0-1: 新 job 缺 group_id(数据不完整),进入 retry/reconciliation
    # 不静默走旧拓扑猜测,避免投递到无副本的频道
    if is_structured_new and fuid and gid is None:
        logger.error(
            f"[Dsp] 新 job 缺 group_id(数据不完整),进入 retry: "
            f"job={job.job_id}, code={job.code}, fuid={fuid}"
        )
        await _mark_delivery_failed_safe(
            store, job.job_id, msg_id,
            reason=f"missing_group_id fuid={fuid}",
        )
        return False  # 触发 retry_local_job,等待数据修复

    # ── 使用 copy_message 发送(fallback 路径,仅用于旧 job 或 Resolver 命中失败时)──
    resolved = None
    if not sent_msg_id:
        resolved = await resolve_delivery_channel(job.storage_channel_id)
        sent_msg_id = await try_deliver(bot, job.target_user_id, resolved.channel_id, msg_id, protect_content=protect_content, bot_id=bot_id, original_channel_id=job.storage_channel_id)

    if not sent_msg_id and resolved is not None:
        await _record_channel_failure(resolved.channel_id)
        # 环形降级:沿环找下一个可用频道,避免重复尝试同一频道
        from storage.delivery_resolver import _walk_ring_for_channel
        tried.add(resolved.channel_id)
        current_id = resolved.channel_id
        for _ in range(10):  # 最多尝试 10 个降级槽位
            next_resolved = await _walk_ring_for_channel(current_id)
            if next_resolved.channel_id in tried:
                break
            tried.add(next_resolved.channel_id)
            sent_msg_id = await try_deliver(bot, job.target_user_id, next_resolved.channel_id, msg_id, protect_content=protect_content, bot_id=bot_id, original_channel_id=job.storage_channel_id)
            if sent_msg_id:
                break
            await _record_channel_failure(next_resolved.channel_id)
            current_id = next_resolved.channel_id

        # C3: 环形降级耗尽后,尝试原始存储频道(消息实际存储位置,即使已降级消息仍存在)
        if not sent_msg_id and job.storage_channel_id not in tried:
            logger.info(f"[Dsp] 环形降级耗尽,尝试原始存储频道: {job.storage_channel_id}")
            sent_msg_id = await try_deliver(bot, job.target_user_id, job.storage_channel_id, msg_id, protect_content=protect_content, bot_id=bot_id, original_channel_id=job.storage_channel_id)

    if sent_msg_id:
        logger.info(f"[Dsp] 发送成功: 用户 {job.target_user_id}, 码:{job.code}")
        # R35 §21.2: 双写 — 内存缓存层(_sent_msg_tracker) + 持久化权威层(delivery_receipts)
        _sent_msg_tracker.setdefault(job.job_id, set()).add(msg_id)
        await _confirm_delivery_receipt_safe(store, job.job_id, msg_id, sent_msg_id)
        # 第三方 Bot 原始 caption 需原样保留，不覆盖为标准模板
        if not await _should_preserve_caption(job.code):
            caption = await _build_delivery_caption(job.code, total_count=1)
            await _edit_sent_caption(bot, job.target_user_id, sent_msg_id, caption)
        await metrics.record_send_success()
        await metrics.record_processed("dsp_bot")
        return True

    # 所有槽位均不可用,记录失败
    logger.error(f"[Dsp] 发送失败(所有槽位不可用): 码{job.code}, 尝试频道数{len(tried) or 1}")
    # R35 §21.2: 持久化失败回执(status=PENDING → FAILED)
    await _mark_delivery_failed_safe(
        store, job.job_id, msg_id,
        reason=f"all_channels_unavailable tried={len(tried) or 1}",
    )
    await metrics.record_send_fail()
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
        return await _fallback_single_send(bot, job, bot_id=bot_id)

    total_pages = (len(file_meta_list) + PAGE_SIZE - 1) // PAGE_SIZE
    page_key = None

    if total_pages > 1:
        # 加 user_id 避免多用户同码键冲突
        page_key = f"{job.code}:{job.target_user_id}"
        async with _pg_lock:
            _pagination_states[page_key] = {
                "channel_msg_ids": job.storage_msg_ids,
                "batch_file_meta": file_meta_list,
                "total_pages": total_pages,
                "chat_id": job.target_user_id,
                "storage_channel_id": job.storage_channel_id,
                "created_at": time.time(),
                "file_code": job.code,
                "target_user_id": job.target_user_id,
                "protect_content": getattr(job, "protect_content", False),
            }

    result = await _send_page(
        bot, job.target_user_id, job.code,
        file_meta_list, page=1, total_pages=total_pages,
        storage_channel_id=job.storage_channel_id,
        page_key=page_key if total_pages > 1 else None,
        storage_msg_ids=job.storage_msg_ids,
        protect_content=getattr(job, "protect_content", False),
        bot_id=bot_id,
        job_id=job.job_id,
    )
    return result


async def _fallback_single_send(bot, job, bot_id: int = 1):
    """兜底逐个发送（媒体组发送失败时使用）。

    注意：调用方 _send_one_job 已持有 _send_semaphore，此处不再获取，
    避免双重获取导致低并发时死锁。消息逐个发送本身已串行，无需额外限流。
    S-4: 返回值反映实际发送结果，不再恒为 True。
    P1-2: 成功投递的 msg_id 记入 _sent_msg_tracker,重试时跳过。
    R35 §21.2: 同时双写 delivery_receipts(PENDING/CONFIRMED/FAILED)。
    """
    protect_content = getattr(job, "protect_content", False)
    all_success = True
    first_sent_mid: int | None = None
    store = _get_store_safe()
    for i, mid in enumerate(job.storage_msg_ids):
        # R35 §21.2: 投递前写 PENDING receipt(异常安全)
        await _upsert_delivery_receipt_safe(
            store, job.job_id, mid, job.target_user_id, status="PENDING"
        )
        try:
            resolved = await resolve_delivery_channel(job.storage_channel_id)
            sent_mid = await try_deliver(bot, job.target_user_id, resolved.channel_id, mid, protect_content=protect_content, bot_id=bot_id, original_channel_id=job.storage_channel_id)
            if sent_mid:
                # R35 §21.2: 双写 — 内存缓存 + 持久化权威层
                _sent_msg_tracker.setdefault(job.job_id, set()).add(mid)
                await _confirm_delivery_receipt_safe(store, job.job_id, mid, sent_mid)
                await metrics.record_send_success()
                if first_sent_mid is None:
                    first_sent_mid = sent_mid
            else:
                # R35 §21.2: 持久化失败回执
                await _mark_delivery_failed_safe(
                    store, job.job_id, mid,
                    reason=f"fallback_copy_failed ch={resolved.channel_id}",
                )
                await metrics.record_send_fail()
                all_success = False
        except Exception as e:
            logger.error(f"[Dsp] 兜底发送异常 (msg={mid}): {e}")
            await _mark_delivery_failed_safe(
                store, job.job_id, mid, reason=f"exception:{type(e).__name__}"
            )
            await metrics.record_send_fail()
            all_success = False
        # 每条消息之间间隔 0.15s,避免同一个频道/同用户超过限制
        if i < len(job.storage_msg_ids) - 1:
            await asyncio.sleep(0.15)
    # 第一条消息添加 caption（第三方原始 caption 保留时跳过）
    if first_sent_mid and not await _should_preserve_caption(job.code):
        caption = await _build_delivery_caption(job.code, total_count=len(job.storage_msg_ids))
        await _edit_sent_caption(bot, job.target_user_id, first_sent_mid, caption)
    await metrics.record_processed("dsp_bot")
    return all_success


async def _send_page(bot, chat_id, file_code, file_meta_list, page, total_pages, storage_channel_id=None, page_key=None, storage_msg_ids=None, protect_content=False, bot_id=1, job_id: int | None = None) -> bool:
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE

    # caption 只在第一页添加（多页分页时后续页不重复附加 caption）；第三方原始 caption 保留时跳过
    preserve = await _should_preserve_caption(file_code)
    caption = (await _build_delivery_caption(file_code, total_count=len(file_meta_list))) if (page == 1 and not preserve) else None
    first_sent_msg_id: int | None = None

    # 使用 copy_message 从存储频道复制（跨Bot file_id 不可用，必须走 copy 路径）
    if storage_msg_ids and storage_channel_id:
        page_msg_ids = storage_msg_ids[start:end]

        # 优先用 copy_messages 批量复制(保持媒体组相册形态),失败再回退逐条 copy_message
        resolved = None
        batch_copied_ids = None
        try:
            resolved = await resolve_delivery_channel(storage_channel_id)
            batch_copied_ids = await try_deliver_batch(
                bot, chat_id, resolved.channel_id, page_msg_ids,
                protect_content=protect_content, bot_id=bot_id,
                original_channel_id=storage_channel_id,
            )
        except Exception as e:
            batch_copied_ids = None
            logger.warning(f"[Dsp] _send_page 批量复制异常,回退逐条: {e}")

        if batch_copied_ids:
            logger.info(f"[Dsp] 分页发送成功(批量相册): {chat_id}, 码:{file_code}, 第{page}/{total_pages}页({len(page_msg_ids)}个文件)")
            # P1-2 + R35 §21.2: 双写 — 内存缓存 + 持久化权威层(仅 job 调用路径)
            if job_id is not None:
                store = _get_store_safe()
                for idx, mid in enumerate(page_msg_ids):
                    _sent_msg_tracker.setdefault(job_id, set()).add(mid)
                    sent_mid_for_receipt = batch_copied_ids[idx] if idx < len(batch_copied_ids) else None
                    await _confirm_delivery_receipt_safe(store, job_id, mid, sent_mid_for_receipt or 0)
            first_sent_msg_id = batch_copied_ids[0]
            await metrics.record_send_success()
            await metrics.record_processed("dsp_bot")
        else:
            # 回退逐条 copy_message(批量失败原因:影子映射不全/BadRequest/部分消息损坏等)
            # resolved 可能为 None(resolve_delivery_channel 异常),回退用原始 storage_channel_id
            fallback_channel = resolved.channel_id if resolved else storage_channel_id
            fallback_status = resolved.status if resolved else "unknown"
            success_count = 0
            fail_details = []
            store = _get_store_safe() if job_id is not None else None
            for i, mid in enumerate(page_msg_ids):
                # R35 §21.2: 投递前写 PENDING receipt(仅 job 调用路径)
                if job_id is not None:
                    await _upsert_delivery_receipt_safe(
                        store, job_id, mid, chat_id, status="PENDING"
                    )
                try:
                    sent_mid = await try_deliver(bot, chat_id, fallback_channel, mid, protect_content=protect_content, bot_id=bot_id, original_channel_id=storage_channel_id)
                    if sent_mid:
                        success_count += 1
                        # P1-2 + R35 §21.2: 双写
                        if job_id is not None:
                            _sent_msg_tracker.setdefault(job_id, set()).add(mid)
                            await _confirm_delivery_receipt_safe(store, job_id, mid, sent_mid)
                        if first_sent_msg_id is None:
                            first_sent_msg_id = sent_mid
                    else:
                        # R35 §21.2: 持久化失败回执
                        if job_id is not None:
                            await _mark_delivery_failed_safe(
                                store, job_id, mid,
                                reason=f"page_copy_failed ch={fallback_channel}/{fallback_status}",
                            )
                        fail_details.append(f"msg={mid}(channel={fallback_channel}/{fallback_status})")
                        logger.warning(f"[Dsp] _send_page copy 失败 (msg={mid}, resolved_channel={fallback_channel}/{fallback_status}, original_channel={storage_channel_id})")
                except Exception as e:
                    # R35 §21.2: 异常时也持久化失败回执
                    if job_id is not None:
                        await _mark_delivery_failed_safe(
                            store, job_id, mid, reason=f"exception:{type(e).__name__}"
                        )
                    fail_details.append(f"msg={mid}(exc={type(e).__name__})")
                    logger.error(f"[Dsp] _send_page copy 异常 (msg={mid}): {e}")
                if i < len(page_msg_ids) - 1:
                    await asyncio.sleep(0.15)
            if success_count > 0:
                logger.info(f"[Dsp] 分页发送成功(逐条回退): {chat_id}, 码:{file_code}, 第{page}/{total_pages}页({success_count}/{len(page_msg_ids)}个文件)")
                await metrics.record_send_success()
                await metrics.record_processed("dsp_bot")
            else:
                logger.error(
                    f"[Dsp] 分页发送全部失败: {chat_id}, 码:{file_code}, 第{page}/{total_pages}页,"
                    f"storage_channel={storage_channel_id}, msg_ids={page_msg_ids}, 失败详情={fail_details}"
                )
                await metrics.record_send_fail()
                await metrics.record_error("dsp_bot")
                try:
                    await safe_send_message(bot, chat_id=chat_id, text="文件发送失败，请稍后重试或联系管理员")
                except Exception:
                    pass
                return False
    else:
        # 旧路径：media group 方式（file_id 跨Bot 不可用，仅作兜底）
        page_items = file_meta_list[start:end]
        input_media = [_build_input_media(meta) for meta in page_items]
        try:
            sent_msgs = await safe_send_media_group(bot, chat_id=chat_id, media=input_media)
            if sent_msgs:
                first_sent_msg_id = sent_msgs[0].message_id
            logger.info(f"[Dsp] 媒体组发送成功: {chat_id}, 码:{file_code}, 第{page}/{total_pages}页({len(input_media)}张)")
            await metrics.record_send_success()
            await metrics.record_processed("dsp_bot")
        except Exception as e:
            logger.error(f"[Dsp] 媒体组发送失败: {e}")
            await metrics.record_send_fail()
            await metrics.record_error("dsp_bot")
            try:
                await safe_send_message(bot, chat_id=chat_id, text="文件发送失败，请稍后重试或联系管理员")
            except Exception:
                pass
            return False

    # 第一页发送成功后，给第一条消息编辑 caption（显示文件总数+备注+文件码）
    if caption and first_sent_msg_id:
        await _edit_sent_caption(bot, chat_id, first_sent_msg_id, caption)

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
        async with _pg_lock:
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
        logger.warning(f"[Dsp] 发送举报按钮失败: {e}")


_report_debounce: dict[str, float] = {}
_REPORT_DEBOUNCE_TTL = 300  # 5 分钟,超过此时间的记录可清理
_report_debounce_last_gc = 0.0  # 上次 GC 时间,用于惰性清理

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户点击举报按钮，推送消息给管理员"""
    query = update.callback_query

    data = query.data
    if not data.startswith("report_req|"):
        await query.answer()
        return

    file_code = data.split("|", 1)[1]
    reporter = update.effective_user
    if not reporter:
        await query.answer()
        return

    # 60 秒防抖
    key = f"{reporter.id}:{file_code}"
    now = time.time()
    # 惰性 GC:每 60 秒清理一次过期记录,避免 _report_debounce 无限增长导致内存泄漏
    global _report_debounce_last_gc
    if now - _report_debounce_last_gc > 60:
        expired_keys = [k for k, ts in _report_debounce.items() if now - ts > _REPORT_DEBOUNCE_TTL]
        for k in expired_keys:
            _report_debounce.pop(k, None)
        _report_debounce_last_gc = now
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
        [InlineKeyboardButton("🔒 封禁上传者", callback_data=f"report:ban|{uploader_id}|{reporter.id}|dsp")],
        [InlineKeyboardButton("🔗 脱钩文件码", callback_data=f"report:detach|{file_code}|{reporter.id}|dsp")],
        [InlineKeyboardButton("🚫 限制举报人", callback_data=f"report:block|{file_code}|{reporter.id}")],
        [InlineKeyboardButton("✅ 忽略", callback_data="report:ignore")],
    ])

    # 通过 Admin Bot 发送，确保操作按钮回调能回到 Admin Bot 处理
    from utils.admin_notify import send_to_admin
    try:
        await send_to_admin(report_text, keyboard)
        await query.answer("举报已提交，管理员将尽快处理", show_alert=True)
    except Exception as e:
        logger.error(f"[Dsp][report] 推送管理员失败: {e}")
        await query.answer("举报提交失败，请稍后重试", show_alert=True)


# ─── 命令处理 ───

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    user = update.effective_user

    # 标记用户已启动 dsp bot，并恢复 waiting_start 的 jobs
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.mark_user_started(user.id, "dsp")
    reactivated_ids = await store.reactivate_waiting_start_jobs(user.id)
    # C1: 恢复的 job 重新投递到 Redis Stream,避免 Redis 模式下饥饿
    if reactivated_ids:
        try:
            from utils.redis_client import xadd_job
            for crdb_id in reactivated_ids:
                try:
                    await xadd_job(crdb_id)
                except Exception:
                    pass  # 降级:dsp_notify 已写入
        except Exception:
            pass
    logger.info(f"[Dsp][start] 用户 {user.id} 已启动，waiting_start jobs 已恢复({len(reactivated_ids)}个)")

    await safe_reply_text(update.message,
        "📥 欢迎使用文件发送机器人！\n\n"
        "此机器人用于接收解码后的文件，无需手动操作。\n"
        "当您通过解码机器人获取文件码后，文件会自动发送给您。\n"
        "发送 /help 查看帮助信息"
        + three_bot_reminder()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        "📥 文件发送机器人使用帮助\n\n"
        "可用命令：\n"
        "/start — 启动机器人 / 查看欢迎语\n"
        "/help — 查看本帮助\n\n"
        "使用说明：\n"
        "1. 本机器人自动接收解码后的文件，无需手动操作。\n"
        "2. 在解码机器人发送文件码后，文件会自动发送到此处。\n"
        "3. 如遇文件异常，可点击文件下方的「举报」按钮通知管理员处理。\n"
        "4. 文件较多时支持分页浏览，点击翻页按钮即可查看。"
        + common_faq()
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
    async with _pg_lock:
        state = _pagination_states.get(page_key)
    if not state:
        await query.answer("会话已过期，请重新发送文件码。", show_alert=True)
        return

    # 检查 TTL
    if time.time() - state.get("created_at", 0) > _PAGE_STATE_TTL:
        async with _pg_lock:
            _pagination_states.pop(page_key, None)
        await query.answer("会话已过期，请重新发送文件码。", show_alert=True)
        return

    file_meta_list = state["batch_file_meta"]
    total_pages = state["total_pages"]
    file_code = state.get("file_code", page_key)

    if page < 1 or page > total_pages:
        await query.answer("无效的页码")
        return

    # P1-3: 翻页前校验文件状态(detached/offline 则拒绝),避免下架文件借分页继续扩散
    try:
        record = await get_file_record_cached(file_code)
        if not record:
            await query.answer("文件记录不存在", show_alert=True)
            return
        status = record.get("status")
        if status in ("detached", "offline"):
            await query.answer("该文件已下架，无法继续浏览", show_alert=True)
            # 清理会话,避免后续翻页继续触发
            async with _pg_lock:
                _pagination_states.pop(page_key, None)
            return
    except Exception as e:
        logger.warning(f"[Dsp] 翻页前文件状态校验异常(码={file_code}): {e}")

    old_msg_id = state.get("last_pagination_msg_id")
    # P1-3: 优先用 state 中的原始收件人 chat_id,避免转发后任意点击者触发重投
    target_chat_id = state.get("chat_id") or query.message.chat_id
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=target_chat_id, message_id=old_msg_id)
        except Exception:
            pass

    await _send_page(
        context.bot, target_chat_id, file_code,
        file_meta_list, page=page, total_pages=total_pages,
        storage_channel_id=state.get("storage_channel_id"),
        page_key=page_key,
        storage_msg_ids=state.get("channel_msg_ids"),
        protect_content=state.get("protect_content", False),
    )

    if page >= total_pages:
        async with _pg_lock:
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
    # A1: 启动计数器定期上报(跨进程聚合)
    from utils.monitor import start_counter_reporter
    asyncio.create_task(start_counter_reporter("dsp_bot"))

    logger.info("[Dsp] 启动发送机器人 (Dsp Bot)...")
    app = Application.builder().token(TOKEN).build()
    bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    # report_callback 必须先于分页回调，且分页回调需限定 pattern，
    # 否则无 pattern 的 pagination_callback 会吞掉 report_req| 回调，导致举报按钮失效。
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^report_req\|"))
    app.add_handler(CallbackQueryHandler(pagination_callback, pattern=r"^(pg\||noop$)"))

    await metrics.ping_bot("dsp_bot")

    async def health_ping():
        while True:
            await metrics.ping_bot("dsp_bot")
            await report_bot_heartbeat("dsp_bot")
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
        # 之后每 6 小时轻量同步 + 清理旧记录(兜底,正常流程零 CRDB 依赖)
        while True:
            await asyncio.sleep(21600)
            try:
                await sync_jobs_from_crdb_to_sqlite(100)
                await store.cleanup_local_jobs(7)
            except Exception as e:
                logger.warning(f"[Dsp] 周期同步异常: {e}")

    # D: Sync Back - R36 §6.4.4: dirty 驱动 + 退避(无 dirty 时 1m→5m→30m,关闭连接)
    async def sync_back_loop():
        """R36 §6.4.4: 取消固定 120s 同步,改为 dirty 驱动 + 退避。

        - 有 dirty job 时:处理所有 dirty,下次 60s 后再检查
        - 连续无 dirty 时退避:60s → 300s(5min) → 1800s(30min),上限 30min
        - 任何一次发现 dirty 后,退避重置为 60s
        - sync_local_jobs_to_crdb 内部已含 dirty 检查,无 dirty 时直接返回,不会建立 CRDB 连接
        """
        from database.session import sync_local_jobs_to_crdb
        from database.cache_store import get_cache_store
        backoff_seconds = 60  # 初始 1min
        max_backoff = 1800    # 上限 30min
        while True:
            try:
                # 先查 dirty 数量(0 RU,SQLite 本地查询),决定是否需要 CRDB 连接
                store = get_cache_store()
                unsynced = await store.get_local_unsynced_jobs()
                if unsynced:
                    # 有 dirty:执行同步,重置退避到 60s
                    await sync_local_jobs_to_crdb()
                    backoff_seconds = 60
                else:
                    # 无 dirty:退避翻倍(上限 30min),不建立 CRDB 连接
                    backoff_seconds = min(backoff_seconds * 2, max_backoff)
                    logger.debug(f"[SyncBack] 无 dirty job,退避至 {backoff_seconds}s 后再检查")
            except Exception as e:
                logger.warning(f"[SyncBack] 同步异常: {e}")
            await asyncio.sleep(backoff_seconds)

    # E: 回收超时 dispatched jobs,防止进程崩溃导致 job 永久丢失
    async def reclaim_dispatched_loop():
        from database.cache_store import get_cache_store
        from utils.redis_client import xadd_job
        store = get_cache_store()
        while True:
            try:
                reclaimed_ids = await store.reclaim_stale_dispatched(timeout_seconds=600)
                # C1: 回收的 job 重新投递到 Redis Stream,避免 Redis 模式下饥饿
                for crdb_id in reclaimed_ids:
                    try:
                        await xadd_job(crdb_id)
                    except Exception:
                        pass  # 降级:notify_dsp_new_job 已写入 dsp_notify
            except Exception as e:
                logger.warning(f"[Dsp] 回收 dispatched jobs 异常: {e}")
            await asyncio.sleep(120)

    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(startup_sync(), name="startup-sync")          # H: 启动同步 + 周期兜底
    create_safe_task(sync_back_loop(), name="sync-back")          # D: 新增
    create_safe_task(process_queue(bot), name="process-queue")
    create_safe_task(_cleanup_page_states(), name="cleanup-page-states")
    create_safe_task(_cleanup_channel_failures(), name="cleanup-channel-failures")
    create_safe_task(_cleanup_channel_limiter_loop(), name="cleanup-channel-limiter")
    create_safe_task(_retry_dead_jobs(), name="retry-dead-jobs")
    create_safe_task(reclaim_dispatched_loop(), name="reclaim-dispatched")  # E: 回收超时 dispatched
    create_safe_task(_reclaim_stale_pel_loop(), name="reclaim-stale-pel")  # N-R1: 回收 PEL 孤儿条目
    create_safe_task(_watch_cells_change(), name="watch-cells-change")  # PRE-02: 失效 delivery_resolver 缓存
    # 监听文件/用户记录变更通知(admin_bot 举报处理触发),失效本进程内存缓存
    async def _watch_record_change():
        """每 3 秒检查 file_record_change_notify,发现变更后失效对应内存缓存。

        admin_bot 处理举报(脱钩/封禁/限制)后通过 notify_record_change 写入 SQLite 通知表。
        本任务检测到变更后逐个失效 file/user 缓存,确保脱钩封禁等实时生效。
        """
        from database.cache_store import get_cache_store
        from database.cache import invalidate_file_record, get_user_cache
        store = get_cache_store()
        last_version = 0
        while True:
            try:
                changes, new_version = await store.consume_record_changes(last_version)
                if changes:
                    for change_type, record_key in changes:
                        if change_type == "file":
                            invalidate_file_record(record_key)
                        elif change_type == "user":
                            try:
                                uid = int(record_key)
                                get_user_cache().invalidate(f"user:{uid}")
                                from database.cache import clear_negative_user
                                clear_negative_user(uid)
                            except (ValueError, TypeError):
                                pass
                    last_version = new_version
                    logger.info(f"[Dsp] 记录变更检测(version={new_version}),已失效 {len(changes)} 条缓存")
            except Exception as e:
                logger.warning(f"[Dsp] 记录变更检测异常: {e}")
            await asyncio.sleep(3)
    create_safe_task(_watch_record_change(), name="watch-record-change")
    from database.cache import dump_cache_to_disk_loop
    create_safe_task(dump_cache_to_disk_loop(), name="dump-cache")

    async with app:
        await app.start()
        await app.updater.start_polling()
        # 注册全局停止事件,让信号 handler 能 set 它触发优雅关闭
        from run_all import _set_stop_event
        stop_event = asyncio.Event()
        _set_stop_event(stop_event)
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[Dsp] 收到停止信号,正在优雅关闭 polling...")
            try:
                await asyncio.wait_for(app.updater.stop(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("[Dsp] polling 关闭超时(15s),强制继续")
            except Exception as e:
                logger.warning(f"[Dsp] polling 关闭异常: {e}")
            try:
                await asyncio.wait_for(app.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[Dsp] app.stop 超时(10s),强制继续")
            except Exception as e:
                logger.warning(f"[Dsp] app.stop 异常: {e}")
            # C1: 关闭 Redis 连接
            try:
                from utils.redis_client import close_redis
                await asyncio.wait_for(close_redis(), timeout=5.0)
            except Exception as e:
                logger.debug(f"[Dsp] close_redis 异常: {e}")
            logger.info("[Dsp] 优雅关闭完成")


def run():
    """启动 Dsp Bot(使用 asyncio.run 标准模式)"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
