"""Up Bot - 上传机器人(环形冗余架构)
职责:接收用户文件 -> 轮转分发到活跃窗口内的 3 个 A 槽 (round-robin)
"""

import asyncio
import datetime
import time
try:
    import orjson as json
except ImportError:
    import json
from collections import defaultdict


def _json_dumps(obj, **kwargs):
    if isinstance(result := json.dumps(obj, **kwargs), bytes):
        return result.decode()
    return result

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from loguru import logger

from config import settings
from database import get_pending_uploads_col, get_active_cells_local
from database.cache_store import get_cache_store
from services.permission import check_upload_permission
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.monitor import metrics
from utils.task_utils import create_safe_task
from utils.force_join import check_force_join, three_bot_reminder
from utils.flood_waiter import safe_copy_message, safe_send_message, safe_reply_text
from utils.file_utils import detect_file_type, extract_file_meta

TOKEN = settings.UPLOAD_BOT_TOKEN

_pending_media_groups: dict[str, dict] = {}
_active_a_slots: list[dict] = []
_active_slot_index: int = 0
_external_buffers: dict[str, dict] = {}
_pending_lock = asyncio.Lock()  # 保护 _active_slot_index 和 dict 操作

async def _cleanup_pending():
    """定期清理超时未完成的 media group 和 external buffer。"""
    while True:
        try:
            now = time.time()
            # 清理超时的 media group (>30s)
            expired_mg = [k for k, v in _pending_media_groups.items() if now - v.get("created_at", 0) > 30]
            for k in expired_mg:
                grp = _pending_media_groups.pop(k, None)
                if grp and grp.get("timer"):
                    grp["timer"].cancel()
                logger.warning(f"[up_bot] 清理超时 media group: {k}")
            # 清理超时的 external buffer (>120s)
            expired_ext = [k for k, v in _external_buffers.items() if now - v.get("created_at", 0) > 120]
            for k in expired_ext:
                buf = _external_buffers.pop(k, None)
                if buf and buf.get("timer"):
                    buf["timer"].cancel()
                logger.warning(f"[up_bot] 清理超时 external buffer: {k}")
        except Exception as e:
            logger.error(f"[up_bot] 清理超时缓冲区异常: {e}")
        await asyncio.sleep(60)


async def _refresh_active_slots():
    """刷新当前 Active A 槽列 (读取 cells 表)"""
    global _active_a_slots
    try:
        _active_a_slots = await get_active_cells_local()
        logger.info(f"[Up] 刷新 Active 槽位: {len(_active_a_slots)} 个")
    except Exception as e:
        logger.error(f"[Up] 刷新槽位失败: {e}")


async def _get_upload_target_channel() -> int:
    """选择上传目标频道:在活跃频道间轮转(round-robin)"""
    global _active_slot_index
    if not _active_a_slots:
        await _refresh_active_slots()
    if not _active_a_slots:
        logger.error("[Up] 无可用活跃槽位，无法处理上传请求")
        raise RuntimeError("无可用活跃槽位，请检查拓扑配置")
    # 轮转:每次取下一个活跃频道
    async with _pending_lock:
        idx = _active_slot_index % len(_active_a_slots)
        _active_slot_index += 1
    channel_id = _active_a_slots[idx]["channel_id"]
    logger.debug(f"[Up] 轮转分发 频道 {channel_id} (index={idx}/{len(_active_a_slots)})")
    return channel_id


# ─── 以下逻辑与原来基本相同，channel 选择改为环形槽位 ───


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await safe_reply_text(update.message,
        "欢迎使用上传机器人。\n\n"
        "📤 **单次上传**: 直接发送文件, 立即生成文件码\n\n"
        "📦 **批次上传** (所有文件共用一个文件码):\n"
        "  /start_upload - 开始批次上传\n"
        "  发送多个文件...\n"
        "  /end_upload - 结束批次,生成文件码\n\n"
        "所有用户均可免费上传文件\n"
        + three_bot_reminder()
    )


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您被禁止使用上传功能")
        return

    context.user_data["batch"] = {
        "file_types": defaultdict(int),
        "pinned_msg_ids": [],
        "files_meta": [],
        "note": "",
    }
    await update.message.reply_text(
        "📦 已进入批次上传模式，请发送文件。\n"
        "发/end_upload 结束并生成文件码。\n"
        "发/cancel_upload 取消本次上传。\n\n"
        "💬 可使用 /note 文字 为本次批次添加备注"
    )


async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        del context.user_data["batch"]
        await update.message.reply_text("批次上传已取消")
    else:
        await update.message.reply_text("当前没有进行中的批次上传")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置批次上传备注"""
    if not await check_force_join(update, context):
        return
    batch = context.user_data.get("batch")
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传,请先使用 /start_upload 开始")
        return
    note_text = " ".join(context.args) if context.args else ""
    if not note_text:
        await update.message.reply_text("用法:/note 备注内容\n例如:/note 这是张三的文件")
        return
    batch["note"] = note_text
    await update.message.reply_text(f"备注已设置为：{note_text}")


async def end_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    batch = context.user_data.pop("batch", None)
    if batch is None:
        await update.message.reply_text("当前没有进行中的批次上传,请先使用 /start_upload 开始")
        return

    # PRE-13: 仅 flush 当前用户的 media group，避免清掉其他用户正在进行中的批次
    pending_mgids = [
        mgid for mgid, grp in _pending_media_groups.items()
        if grp.get("user_id") == user.id
    ]
    for mgid in pending_mgids:
        grp = _pending_media_groups.get(mgid)
        if grp and grp.get("timer"):
            grp["timer"].cancel()
        await _flush_batch_media_group(mgid, context, batch)

    channel_msg_ids = batch["pinned_msg_ids"]

    if not channel_msg_ids:
        await update.message.reply_text("没有接收到任何文件，批次已取消")
        return

    type_str = _json_dumps(dict(batch["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in channel_msg_ids)
    batch_file_meta_str = _json_dumps(batch["files_meta"])

    # 暂存批次数据，等待用户选择有效期→备注→转发权限
    context.user_data["_pending_batch"] = {
        "user_id": user.id,
        "file_types": type_str,
        "batch_msg_ids": batch_ids_str,
        "batch_file_meta": batch_file_meta_str,
        "note": batch.get("note", ""),
        "primary_channel_id": await _get_upload_target_channel(),
        "primary_channel_msg_id": channel_msg_ids[0],
        "total_count": len(channel_msg_ids),
    }

    await update.message.reply_text(
        f"📦 {len(channel_msg_ids)} 个文件已接收\n请选择文件有效期：",
        reply_markup=_build_ttl_keyboard(),
    )


async def _dispatch_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    if "batch" in context.user_data:
        await _collect_batch_file(update, context)
        return

    if update.message.media_group_id:
        await handle_media_group(update, context)
    else:
        await handle_file(update, context)


async def _collect_batch_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch = context.user_data["batch"]
    user = update.effective_user
    file_type = detect_file_type(update)

    if update.message.media_group_id:
        mgid = update.message.media_group_id
        if mgid not in _pending_media_groups:
            _pending_media_groups[mgid] = {
                "user_id": user.id,  # PRE-13: 标记所属用户，end_upload 仅 flush 本人的
                "file_types": defaultdict(int),
                "updates": [],
                "timer": None,
                "created_at": time.time(),  # PRE-12: 供 _cleanup_pending 超时清理判断
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
        batch["files_meta"].append(extract_file_meta(update))
        try:
            target_ch = await _get_upload_target_channel()
            forwarded = await safe_copy_message(context.bot, target_ch, update.effective_chat.id, update.message.message_id)
            batch["pinned_msg_ids"].append(forwarded.message_id)
        except Exception as e:
            logger.error(f"[Up] 批次上传复制文件到存储频道失败: {e}")
        await update.message.reply_text(f"已接{file_type}")


async def _copy_one_media(context, target_ch, up, batch: dict):
    """复制单个媒体文件到存储频道,由 _flush_batch_media_group 并发调用"""
    try:
        forwarded = await safe_copy_message(context.bot, target_ch, up.effective_chat.id, up.message.message_id)
        if forwarded is not None:
            batch["pinned_msg_ids"].append(forwarded.message_id)
            batch["files_meta"].append(extract_file_meta(up))
            return True
        return False
    except Exception as e:
        logger.error(f"[Up] 媒体组复制文件失败: {e}")
        return False


async def _flush_batch_media_group(mgid: str, context: ContextTypes.DEFAULT_TYPE, batch: dict):
    grp = _pending_media_groups.pop(mgid, None)
    if grp is None:
        return
    if not grp.get("updates"):
        return
    file_types = grp["file_types"]
    for k, v in file_types.items():
        batch["file_types"][k] += v
    target_ch = await _get_upload_target_channel()

    # 并发复制所有媒体文件到存储频道
    tasks = [asyncio.create_task(_copy_one_media(context, target_ch, up, batch)) for up in grp["updates"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    copied = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if r is not True)

    first = grp["updates"][0]
    type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items()))
    if failed > 0:
        await safe_send_message(context.bot, chat_id=first.effective_chat.id, text=f"⚠️ 已接收媒体组:{type_desc}(成功{copied}个，失败{failed}个)")
    else:
        await safe_send_message(context.bot, chat_id=first.effective_chat.id, text=f"已接收媒体组:{type_desc}({copied}个文件)")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    caption = update.message.caption or ""

    # ── 中继外部文件:中继账号转发的文走缓冲区 批量写入 pending_uploads ──
    if caption.startswith("EXTERNAL_RELAY:"):
        await _handle_external_relay_file(update, context)
        return

    if not await global_rate_limiter.acquire():
        await update.message.reply_text("系统繁忙,请稍后重试")
        return
    if not await user_rate_limiter.acquire(user.id):
        await update.message.reply_text("操作过于频繁,请稍后重试")
        return
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限")
        return

    file_type = detect_file_type(update)
    file_types = {file_type: 1}
    await _process_upload(user.id, update, context, file_types)


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_upload_permission(user.id):
        await update.message.reply_text("您没有上传权限")
        return

    file_type = detect_file_type(update)

    if update.message.media_group_id not in _pending_media_groups:
        _pending_media_groups[update.message.media_group_id] = {
            "user_id": user.id,
            "file_types": defaultdict(int),
            "updates": [],
            "timer": None,
            "created_at": time.time(),  # PRE-12: 供 _cleanup_pending 超时清理判断
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

    target_ch = await _get_upload_target_channel()
    total_count = len(group["updates"])
    all_mids = []
    all_meta = []
    failed_count = 0

    progress_msg = await safe_send_message(
        context.bot, chat_id=user_id,
        text=f"正在处理 {total_count} 个文件...\n已完成 0/{total_count}"
    )

    for i, up in enumerate(group["updates"]):
        try:
            forwarded = await safe_copy_message(context.bot, target_ch, up.effective_chat.id, up.message.message_id)
            all_mids.append(forwarded.message_id)
            all_meta.append(extract_file_meta(up))
        except Exception as e:
            logger.error(f"[Up] media group copy failed: {e}")
            failed_count += 1
        if (i + 1) % 3 == 0 or i == total_count - 1:
            try:
                await progress_msg.edit_text(f"正在处理 {total_count} 个文件...\n已完成 {i + 1}/{total_count}")
            except Exception:
                pass

    if not all_mids:
        await metrics.record_error("up_bot")
        try:
            await progress_msg.edit_text("文件处理失败，请稍后重试")
        except Exception:
            pass
        return

    note = group["updates"][0].message.caption or ""

    # 编辑进度消息为完成状态（最终确认消息由 _finalize_upload 发出）
    try:
        failed_hint = f"（其中 {failed_count} 个文件处理失败）" if failed_count > 0 else ""
        await progress_msg.edit_text(f"文件处理完成{failed_hint}")
    except Exception:
        pass

    # 暂存媒体组数据，等待用户选择有效期→备注→转发权限
    context.user_data["_pending_media_group"] = {
        "user_id": user_id,
        "primary_channel_id": target_ch,
        "primary_channel_msg_id": all_mids[0],
        "file_types": _json_dumps(file_types),
        "batch_msg_ids": ",".join(str(mid) for mid in all_mids),
        "batch_file_meta": _json_dumps(all_meta),
        "note": note,
        "total_count": total_count,
    }

    # 发送上传选项
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="请选择文件有效期：",
            reply_markup=_build_ttl_keyboard(),
        )
    except Exception:
        pass


async def _process_upload(
    user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, file_types: dict
):
    main_channel = await _get_upload_target_channel()
    try:
        forwarded = await safe_copy_message(context.bot, main_channel, update.effective_chat.id, update.message.message_id)
        channel_msg_id = forwarded.message_id
    except Exception as e:
        logger.error(f"[Up] 转发文件到存储频道失败 {e}")
        await metrics.record_error("up_bot")
        await update.message.reply_text("文件处理失败，请稍后重试")
        return

    # 暂存必要信息
    context.user_data["_main_channel"] = main_channel
    context.user_data["_channel_msg_id"] = channel_msg_id
    context.user_data["_file_types"] = file_types
    context.user_data["_note"] = update.message.caption or ""
    context.user_data["_file_meta"] = extract_file_meta(update)

    # 第一步：发送有效期选择
    await update.message.reply_text(
        "请选择文件有效期：",
        reply_markup=_build_ttl_keyboard(),
    )


# ─── 上传选项回调 ───

async def upload_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理上传选项按钮回调。三步流: 有效期 → 备注 → 转发权限"""
    query = update.callback_query
    await query.answer()
    data = query.data  # format: "opt|key|value"
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "opt":
        return

    user_id = query.from_user.id
    key = parts[1]
    value = parts[2]

    if key == "ttl":
        context.user_data["file_ttl"] = value
        # 批次已通过 /note 设置备注的，跳过备注步骤
        if ("_pending_batch" in context.user_data
                and context.user_data["_pending_batch"].get("note")):
            await query.edit_message_text(
                text="请选择转发权限：",
                reply_markup=_build_protect_keyboard(),
            )
        else:
            await query.edit_message_text(
                text="是否需要添加备注？",
                reply_markup=_build_note_keyboard(),
            )

    elif key == "note":
        if value == "skip":
            await query.edit_message_text(
                text="请选择转发权限：",
                reply_markup=_build_protect_keyboard(),
            )
        elif value == "add":
            context.user_data["awaiting_note_since"] = time.time()
            context.user_data["_note_query_msg_id"] = query.message.message_id
            context.user_data["_note_query_chat_id"] = query.message.chat_id
            await query.edit_message_text(
                text="📝 请发送备注文字（60秒内有效）\n发送任意文字即可，或发送 /cancel_note 跳过",
                reply_markup=None,
            )

    elif key == "protect":
        context.user_data["protect_content"] = value
        await _finalize_upload(query, context, user_id)


async def _finalize_upload(query, context, user_id: int):
    """用户选完所有选项后，写入 pending_uploads 并通知 idx_bot。
    支持三种场景: 单文件 / 媒体组 / 批次上传
    """
    pending_batch = context.user_data.pop("_pending_batch", None)
    pending_mg = context.user_data.pop("_pending_media_group", None)

    try:
        pending_col = get_pending_uploads_col()
        ttl = context.user_data.pop("file_ttl", "0")
        protect = context.user_data.pop("protect_content", str(settings.DEFAULT_PROTECT_CONTENT))
        note = context.user_data.pop("_note", "")

        protect_bool = protect.lower() == "true"
        ttl_days = int(ttl) if ttl.isdigit() else settings.DEFAULT_FILE_TTL_DAYS
        if ttl_days == 0:
            ttl_days = settings.DEFAULT_FILE_TTL_DAYS

        if pending_batch:
            # ── 批次上传 ──
            note = note or pending_batch.get("note", "")
            await pending_col.insert_one({
                "uploader_id": user_id,
                "primary_channel_id": pending_batch["primary_channel_id"],
                "primary_channel_msg_id": pending_batch["primary_channel_msg_id"],
                "file_types": pending_batch["file_types"],
                "batch_msg_ids": pending_batch["batch_msg_ids"],
                "batch_file_meta": pending_batch["batch_file_meta"],
                "note": note,
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
            })
            logger.info(f"[Up] 批次写入pending_uploads: user={user_id}, {pending_batch['total_count']}个文件")

        elif pending_mg:
            # ── 媒体组上传 ──
            await pending_col.insert_one({
                "uploader_id": user_id,
                "primary_channel_id": pending_mg["primary_channel_id"],
                "primary_channel_msg_id": pending_mg["primary_channel_msg_id"],
                "file_types": pending_mg["file_types"],
                "batch_msg_ids": pending_mg["batch_msg_ids"],
                "batch_file_meta": pending_mg["batch_file_meta"],
                "note": note or pending_mg.get("note", ""),
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
            })
            logger.info(f"[Up] 媒体组写入pending_uploads: user={user_id}")

        else:
            # ── 单文件上传 ──
            main_channel = context.user_data.pop("_main_channel", 0)
            channel_msg_id = context.user_data.pop("_channel_msg_id", 0)
            file_types = context.user_data.pop("_file_types", {})
            file_meta = context.user_data.pop("_file_meta", {})

            # PRE-14: 校验存储频道与消息 ID 非零，避免写入无效记录导致 dsp_bot 投递失败
            if not main_channel or not channel_msg_id:
                logger.error(
                    f"[Up] 单文件 _finalize_upload 状态缺失: main_channel={main_channel}, "
                    f"channel_msg_id={channel_msg_id}, user={user_id} — 拒绝写入 pending_uploads"
                )
                await metrics.record_error("up_bot")
                try:
                    await query.edit_message_text(text="文件处理失败：存储频道未就绪，请重新上传")
                except Exception:
                    pass
                return

            await pending_col.insert_one({
                "uploader_id": user_id,
                "primary_channel_id": main_channel,
                "primary_channel_msg_id": channel_msg_id,
                "file_types": _json_dumps(file_types),
                "batch_msg_ids": "",
                "batch_file_meta": _json_dumps([file_meta]),
                "note": note,
                "status_msg_id": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "processed": 0,
                "protect_content": protect_bool,
                "file_ttl_days": ttl_days,
            })
            logger.info(f"[Up] 单文件写入pending_uploads: user={user_id}")

        try:
            await get_cache_store().notify_new_upload()
        except Exception as e:
            logger.warning(f"[Up] 通知 idx_bot 失败(不影响上传): {e}")

        metrics.upload_count += 1
        await metrics.record_processed("up_bot")

        # 清除按钮，显示确认消息
        await query.edit_message_text(
            text=f"文件已接收，文件码将由 @{settings.DECODER_BOT_USERNAME} 发送给你",
            reply_markup=None,
        )
    except Exception as e:
        logger.error(f"[Up] 写入pending_uploads失败: {e}")
        await metrics.record_error("up_bot")
        try:
            await query.edit_message_text(text="文件处理失败，请稍后重试")
        except Exception:
            pass


# ─── 备注文字输入处理 ───

async def _handle_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """当用户处于等待备注输入状态时，捕获文字消息作为备注。"""
    user_id = update.effective_user.id
    note_since = context.user_data.get("awaiting_note_since")
    if note_since is None:
        return  # 不在等待备注状态，忽略

    if time.time() - note_since > 60:
        del context.user_data["awaiting_note_since"]
        await update.message.reply_text("⏰ 备注输入已超时，请重新上传文件")
        return

    # 保存备注
    context.user_data["_note"] = update.message.text
    del context.user_data["awaiting_note_since"]

    await update.message.reply_text(f"✅ 备注已设置：{update.message.text}")

    # 弹出转发权限选择
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="请选择转发权限：",
            reply_markup=_build_protect_keyboard(),
        )
    except Exception as e:
        logger.warning(f"[Up] 发送转发权限选择失败: {e}")


async def cancel_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消/跳过备注输入"""
    if context.user_data.get("awaiting_note_since"):
        del context.user_data["awaiting_note_since"]
        await update.message.reply_text("已跳过备注")
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="请选择转发权限：",
                reply_markup=_build_protect_keyboard(),
            )
        except Exception as e:
            logger.warning(f"[Up] 发送转发权限选择失败: {e}")
    else:
        await update.message.reply_text("当前没有待设置的备注")


def _build_ttl_keyboard():
    """构建有效期选择按钮。"""
    keyboard = [
        [
            InlineKeyboardButton("∞ 永久有效", callback_data="opt|ttl|0"),
            InlineKeyboardButton("1天", callback_data="opt|ttl|1"),
        ],
        [
            InlineKeyboardButton("7天", callback_data="opt|ttl|7"),
            InlineKeyboardButton("30天", callback_data="opt|ttl|30"),
        ],
        [
            InlineKeyboardButton("90天", callback_data="opt|ttl|90"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_protect_keyboard():
    """构建转发权限选择按钮。"""
    keyboard = [
        [
            InlineKeyboardButton("🔒 禁止转发", callback_data="opt|protect|true"),
            InlineKeyboardButton("↗️ 允许转发", callback_data="opt|protect|false"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_note_keyboard():
    """构建备注选择按钮。"""
    keyboard = [
        [InlineKeyboardButton("📝 添加备注", callback_data="opt|note|add")],
        [InlineKeyboardButton("⏭ 跳过", callback_data="opt|note|skip")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── 中继外部文件处理 ───

async def _handle_external_relay_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理中继账号转发到 Up Bot 的外部文件。
    格式:EXTERNAL_RELAY:{user_id}:{external_code}
    文件先 copy 到存储频道，积累后由 EXTERNAL_DONE 触发批量写入 pending_uploads。
    """
    caption = update.message.caption or ""
    rest = caption[len("EXTERNAL_RELAY:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return
    try:
        external_user_id = int(rest[:user_end])
    except ValueError:
        return
    external_code = rest[user_end + 1:].strip()

    buf = _external_buffers.get(external_code)
    # 同一 external_code 的所有文件必须 copy 到同一存储频道，
    # 否则 pending_uploads 的 primary_channel_id 与 batch_msg_ids 中的消息 ID 不匹配（PRE-15 关联修复）
    if buf is not None and buf.get("channel_id"):
        target_ch = buf["channel_id"]
    else:
        target_ch = await _get_upload_target_channel()

    try:
        forwarded = await safe_copy_message(context.bot, target_ch, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error(f"[Up][ext_relay] copy 到存储频道失败 (code={external_code}): {e}")
        return

    file_type = detect_file_type(update)
    file_meta = extract_file_meta(update)

    if external_code not in _external_buffers:
        _external_buffers[external_code] = {
            "user_id": external_user_id,
            "channel_id": target_ch,  # 记录实际存储频道，flush 时复用
            "msg_ids": [],
            "files_meta": [],
            "file_types": defaultdict(int),
            "flushed": False,
            "created_at": time.time(),
        }

    buf = _external_buffers[external_code]
    buf["msg_ids"].append(forwarded.message_id)
    buf["files_meta"].append(file_meta)
    buf["file_types"][file_type] += 1

    # 重置安全超时定时器
    if buf.get("timer"):
        buf["timer"].cancel()
    buf["timer"] = asyncio.get_running_loop().call_later(
        60, lambda: asyncio.ensure_future(_flush_external_buffer(external_code, safe_mode=True))
    )
    logger.debug(f"[Up][ext_relay] 外部文件已缓存 (code={external_code}), 共{len(buf['msg_ids'])}个文件")


async def _handle_external_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 EXTERNAL_DONE 信号:中继账号通知文件收集完毕,触发批量写入"""
    text = update.message.text or ""
    if not text.startswith("EXTERNAL_DONE:"):
        return
    rest = text[len("EXTERNAL_DONE:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return
    try:
        external_user_id = int(rest[:user_end])
    except ValueError:
        return
    external_code = rest[user_end + 1:].strip()

    await _flush_external_buffer(external_code, safe_mode=False)


async def _flush_external_buffer(external_code: str, safe_mode: bool = False):
    """刷新外部文件缓冲写入 pending_uploads
    如果 safe_mode=True，则 flush 已执行，EXTERNAL_DONE 到达时不应重复处理。
    """
    buf = _external_buffers.get(external_code)
    if buf is None:
        return

    # 防止竞争：safe_mode 的超时 flush 已执行后，EXTERNAL_DONE 不应重复处理。
    if buf.get("flushed"):
        return

    # 标记为已处理,防止重复 flush
    buf["flushed"] = True
    _external_buffers.pop(external_code, None)

    if buf.get("timer"):
        buf["timer"].cancel()

    msg_ids = buf.get("msg_ids", [])
    if not msg_ids:
        logger.warning(f"[Up][ext_relay] 外部文件缓冲区为空，跳过 (code={external_code})")
        return

    # 复用 buf 中记录的实际存储频道，而非重新轮选（与 _handle_external_relay_file 保持一致）
    target_ch = buf.get("channel_id")
    if not target_ch:
        logger.error(f"[Up][ext_relay] 缓冲区缺失 channel_id (code={external_code})，跳过")
        return
    type_str = _json_dumps(dict(buf["file_types"]))
    batch_ids_str = ",".join(str(mid) for mid in msg_ids)
    batch_file_meta_str = _json_dumps(buf["files_meta"])
    note = _json_dumps({"type": "external", "code": external_code})

    try:
        pending_col = get_pending_uploads_col()
        # PRE-15: 补全 protect_content / file_ttl_days 字段，与正常上传路径保持一致，
        # 避免 idx_bot 处理 pending_uploads 时因字段缺失而写入不完整的 file_records
        await pending_col.insert_one({
            "uploader_id": buf["user_id"],
            "primary_channel_id": target_ch,
            "primary_channel_msg_id": msg_ids[0],
            "file_types": type_str,
            "batch_msg_ids": batch_ids_str,
            "batch_file_meta": batch_file_meta_str,
            "note": note,
            "status_msg_id": 0,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "processed": 0,
            "protect_content": False,  # 外部中继文件默认允许转发
            "file_ttl_days": 0,  # 0 = 永久有效，与 DEFAULT_FILE_TTL_DAYS 一致
        })
        logger.info(f"[Up][ext_relay] 外部文件已写入pending_uploads: code={external_code}, {len(msg_ids)}个文件")
    except Exception as e:
        logger.error(f"[Up][ext_relay] 写入pending_uploads失败 (code={external_code}): {e}")

    try:
        await get_cache_store().notify_new_upload()
    except Exception as e:
        logger.warning(f"[Up][ext_relay] 通知 idx_bot 失败(不影响上传): {e}")


async def _init():
    from database import init_db
    await init_db()
    await _refresh_active_slots()


async def _async_main():
    await _init()
    from database.cache_store import report_bot_heartbeat
    await report_bot_heartbeat("up_bot")

    logger.info(f"[Up] 启动上传机器人（Up Bot）...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_upload", start_upload))
    app.add_handler(CommandHandler("end_upload", end_upload))
    app.add_handler(CommandHandler("cancel_upload", cancel_upload))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("cancel_note", cancel_note))
    app.add_handler(CallbackQueryHandler(upload_option_callback, pattern=r"^opt\|"))

    # 备注文字输入处理（需在 EXTERNAL_DONE 和 media 之前）
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^EXTERNAL_DONE:"),
        _handle_note_text
    ))

    # 中继外部文件完成信号
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"^EXTERNAL_DONE:"),
        _handle_external_done
    ))

    media_filter = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.PHOTO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
    )
    app.add_handler(MessageHandler(media_filter, _dispatch_media))

    await metrics.ping_bot("up_bot")

    async def health_ping():
        while True:
            await metrics.ping_bot("up_bot")
            await asyncio.sleep(30)

    async def slot_refresh_loop():
        while True:
            await _refresh_active_slots()
            await asyncio.sleep(60)

    loop = asyncio.get_running_loop()
    create_safe_task(health_ping(), name="health-ping")
    create_safe_task(slot_refresh_loop(), name="slot-refresh")
    create_safe_task(_cleanup_pending(), name="cleanup-pending")

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
    """启动 Up Bot(使用 asyncio.run 标准模式)"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
