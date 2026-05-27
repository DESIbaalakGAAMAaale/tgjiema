import asyncio
import datetime
import json
import time
from collections import defaultdict, deque

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from loguru import logger

from config import settings
from database import (
    get_file_records_col,
    get_decode_logs_col,
    get_pending_uploads_col,
    make_file_record,
    make_decode_log,
)
from services.code_generator import generate_unique_code, is_valid_code_format
from services.permission import check_decode_permission, get_or_create_user
from services.queue_manager import enqueue_send_task, enqueue_batch_send_task
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.channel_selector import channel_selector
from utils.monitor import metrics
from utils.force_join import check_force_join, three_bot_reminder
from services.user_relay import user_relay

TOKEN = settings.DECODER_BOT_TOKEN
MAIN_CHANNEL_ID = settings.MAIN_STORAGE_CHANNEL_ID

_pending_external: dict[str, deque[tuple[int, str, float]]] = defaultdict(deque)
_PENDING_TTL = 300


def _enqueue_external(bot_username: str, user_id: int, code: str):
    _pending_external[bot_username].append((user_id, code, time.time()))


def _dequeue_external(bot_username: str) -> tuple[int, str]:
    q = _pending_external.get(bot_username)
    while q:
        entry = q.popleft()
        if time.time() - entry[2] < _PENDING_TTL:
            return entry[0], entry[1]
    return None, None


def _cleanup_stale_pending():
    now = time.time()
    stale = []
    for bot, q in _pending_external.items():
        while q and now - q[0][2] >= _PENDING_TTL:
            q.popleft()
        if not q:
            stale.append(bot)
    for bot in stale:
        del _pending_external[bot]


user_relay.set_pending_cleanup(lambda bot_username: _dequeue_external(bot_username))


_external_media_groups: dict[str, tuple[int, str, float]] = {}
_MEDIA_GROUP_TTL = 300


def _track_external_media_group(media_group_id: str, user_id: int, code: str):
    _external_media_groups[media_group_id] = (user_id, code, time.time())


def _get_external_media_group_user(media_group_id: str) -> tuple[int, str]:
    entry = _external_media_groups.pop(media_group_id, None)
    if entry and time.time() - entry[2] < _MEDIA_GROUP_TTL:
        return entry[0], entry[1]
    return None, None


def _cleanup_media_groups():
    now = time.time()
    stale = [k for k, v in _external_media_groups.items() if now - v[2] >= _MEDIA_GROUP_TTL]
    for k in stale:
        _external_media_groups.pop(k, None)


_MEDIA_GROUP_BUFFER_WAIT = 3
_media_group_buffer: dict[str, dict] = {}


def _parse_storage_ids_from_caption(caption: str) -> list[int]:
    if not caption:
        return []
    for line in caption.split("\n"):
        if line.startswith("STORAGE_IDS:"):
            ids_str = line[len("STORAGE_IDS:"):]
            return [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
    return []


async def _enqueue_storage_ids(
    user_id: int, code: str, storage_ids: list[int], context=None
):
    files_col = get_file_records_col()
    record = await files_col.find_one({"file_code": code})
    if not record:
        logger.warning(f"[_enqueue_storage_ids] DB 无记录 (code={code})")
        return

    storage_channel = record.get("primary_channel_id", MAIN_CHANNEL_ID)

    meta_raw = record.get("batch_file_meta") or ""
    try:
        stored_meta = (
            json.loads(meta_raw)
            if isinstance(meta_raw, str) and meta_raw
            else (meta_raw if isinstance(meta_raw, list) else [])
        )
    except (json.JSONDecodeError, TypeError):
        stored_meta = []
    if not isinstance(stored_meta, list):
        stored_meta = []

    meta_by_msg_id: dict[str, dict] = {}
    for entry in stored_meta:
        if isinstance(entry, dict) and entry.get("msg_id") is not None:
            meta_by_msg_id[str(entry["msg_id"])] = entry

    file_ids_str = str(record.get("file_ids") or "")
    all_file_ids = [f for f in file_ids_str.split(",") if f.strip()] if file_ids_str else []

    batch_all_ids = str(record.get("batch_msg_ids") or "")
    all_msg_ids = []
    primary_mid = record.get("primary_channel_msg_id")
    if primary_mid:
        all_msg_ids.append(str(primary_mid))
    if batch_all_ids:
        for mid in batch_all_ids.split(","):
            m = mid.strip()
            if m and m not in all_msg_ids:
                all_msg_ids.append(m)

    msg_id_to_file_id = {}
    for i, mid_str in enumerate(all_msg_ids):
        msg_id_to_file_id[mid_str] = all_file_ids[i] if i < len(all_file_ids) else ""

    batch_file_meta = []
    storage_channel = record.get("primary_channel_id", MAIN_CHANNEL_ID)
    for sid in storage_ids:
        sid_str = str(sid)
        stored_entry = meta_by_msg_id.get(sid_str)
        if stored_entry:
            batch_file_meta.append({
                "msg_id": str(sid),
                "file_id": stored_entry.get("file_id", ""),
                "type": stored_entry.get("type", "document"),
            })
        else:
            fid = msg_id_to_file_id.get(sid_str, "")
            batch_file_meta.append({"msg_id": str(sid), "file_id": fid, "type": "document"})

    if len(storage_ids) > 1 and any(m.get("file_id") for m in batch_file_meta):
        await enqueue_batch_send_task(
            target_user_id=user_id,
            channel_id=storage_channel,
            channel_msg_ids=list(storage_ids),
            batch_file_meta=json.dumps(batch_file_meta),
            file_code=code,
        )
    else:
        for sid in storage_ids:
            await enqueue_send_task(
                target_user_id=user_id,
                channel_id=storage_channel,
                message_id=sid,
                file_code=code,
            )

    logger.info(
        f"[_enqueue_storage_ids] 已入队: user={user_id}, code={code}, "
        f"{len(storage_ids)} 个文件"
    )

    if context:
        from services.code_generator import extract_bot_username
        target_bot_username = extract_bot_username(code) or ""
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"解码完成！文件已由 @{settings.SENDER_BOT_USERNAME} 发送给您，请前往查收。"
                ),
            )
        except Exception:
            pass


def _extract_media_info(msg):
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.audio:
        return msg.audio.file_id, "audio"
    if msg.voice:
        return msg.voice.file_id, "voice"
    if msg.animation:
        return msg.animation.file_id, "animation"
    if msg.document:
        return msg.document.file_id, "document"
    return None, "document"


async def _flush_media_group_buffer(media_group_id: str):
    await asyncio.sleep(_MEDIA_GROUP_BUFFER_WAIT)
    entry = _media_group_buffer.pop(media_group_id, None)
    if not entry:
        return

    bot = entry["bot"]
    msgs = entry["msgs"]
    user_id = entry["user_id"]
    code = entry["code"]
    source = entry.get("source", "external")

    if not msgs:
        return

    if source == "relay":
        await asyncio.sleep(2)
        try:
            storage_ids = set()
            for _, _, cap in msgs:
                ids = _parse_storage_ids_from_caption(cap)
                storage_ids.update(ids)
            storage_ids = sorted(storage_ids)

            if not storage_ids:
                logger.warning(f"[_flush_media_group_buffer] 中继媒体组: 无STORAGE_IDS (code={code})")
                return

            files_col = get_file_records_col()
            record = await files_col.find_one({"file_code": code})
            if not record:
                logger.warning(f"[_flush_media_group_buffer] 中继媒体组: DB无记录 (code={code})")
                return

            storage_channel = record.get("primary_channel_id", MAIN_CHANNEL_ID)
            file_ids_str = record.get("file_ids") or ""
            all_file_ids = [f for f in file_ids_str.split(",") if f.strip()] if file_ids_str else []

            batch_all_ids = record.get("batch_msg_ids") or ""
            all_msg_ids = []
            primary_mid = record.get("primary_channel_msg_id")
            if primary_mid:
                all_msg_ids.append(str(primary_mid))
            if batch_all_ids:
                for mid in batch_all_ids.split(","):
                    m = mid.strip()
                    if m and m not in all_msg_ids:
                        all_msg_ids.append(m)

            msg_id_to_file_id = {}
            for i, mid_str in enumerate(all_msg_ids):
                msg_id_to_file_id[mid_str] = all_file_ids[i] if i < len(all_file_ids) else ""

            batch_file_meta = []
            for sid in storage_ids:
                sid_str = str(sid)
                fid = msg_id_to_file_id.get(sid_str, "")
                batch_file_meta.append({"file_id": fid, "type": "document"})

            if len(storage_ids) > 1 and any(m.get("file_id") for m in batch_file_meta):
                await enqueue_batch_send_task(
                    target_user_id=user_id,
                    channel_id=storage_channel,
                    channel_msg_ids=list(storage_ids),
                    batch_file_meta=json.dumps(batch_file_meta),
                    file_code=code,
                )
            else:
                for sid in storage_ids:
                    await enqueue_send_task(
                        target_user_id=user_id,
                        channel_id=storage_channel,
                        message_id=sid,
                        file_code=code,
                    )

            logger.info(
                f"[_flush_media_group_buffer] 中继媒体组已入队: user={user_id}, code={code}, "
                f"{len(storage_ids)} 个文件"
            )
        except Exception as e:
            logger.error(f"[_flush_media_group_buffer] 中继媒体组处理失败 (code={code}): {e}")
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text="外部文件发送失败，请稍后重试或联系管理员。",
                )
            except Exception:
                pass
        return

    channel_msg_ids = []
    batch_file_meta = []

    for chat_id, msg_id, orig_caption in msgs:
        try:
            copy_kwargs: dict = {
                "chat_id": MAIN_CHANNEL_ID,
                "from_chat_id": chat_id,
                "message_id": msg_id,
            }

            forwarded = await bot.copy_message(**copy_kwargs)
            channel_msg_ids.append(forwarded.message_id)

            fid, ftype = _extract_media_info(forwarded)
            batch_file_meta.append({
                "file_id": fid,
                "type": ftype,
            })

            if code:
                await _cache_external_file(None, code, forwarded.message_id)

        except Exception as e:
            logger.error(f"[_flush_media_group_buffer] copy消息失败 (mg_id={media_group_id}): {e}")

    if not channel_msg_ids:
        logger.warning(f"[_flush_media_group_buffer] 无有效消息 (mg_id={media_group_id})")
        return

    try:
        await enqueue_batch_send_task(
            target_user_id=user_id,
            channel_id=MAIN_CHANNEL_ID,
            channel_msg_ids=channel_msg_ids,
            batch_file_meta=json.dumps(batch_file_meta),
            file_code=code,
        )
        logger.info(
            f"[_flush_media_group_buffer] 外部媒体组已入队: user={user_id}, code={code}, "
            f"{len(channel_msg_ids)} 个文件"
        )
    except Exception as e:
        logger.error(f"[_flush_media_group_buffer] 入队失败: {e}")
        try:
            await bot.send_message(
                chat_id=user_id,
                text="外部文件发送失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass


async def _cache_external_file(
    context, code: str, message_id: int
):
    try:
        files_col = get_file_records_col()
        existing = await files_col.find_one({"file_code": code})
        if existing:
            batch = existing.get("batch_msg_ids", "") or ""
            if not isinstance(batch, str):
                batch = str(batch)
            batch_ids = [mid for mid in batch.split(",") if mid.strip()]
            if str(message_id) not in batch_ids:
                batch_ids.append(str(message_id))
            await files_col.update_one(
                {"file_code": code},
                {"$set": {"batch_msg_ids": ",".join(batch_ids)}},
            )
            logger.info(
                f"[_cache_external_file] 外部码 {code} 追加 msg_id={message_id}，batch={batch_ids}"
            )
        else:
            record = make_file_record(
                file_code=code,
                uploader_id=0,
                primary_channel_id=MAIN_CHANNEL_ID,
                primary_channel_msg_id=message_id,
                file_types={},
            )
            await files_col.insert_one(record)
            logger.info(f"[_cache_external_file] 外部码已缓存到本地: {code}")
    except Exception as e:
        logger.error(f"[_cache_external_file] 缓存外部码失败 (code={code}, msg_id={message_id}): {e}")


async def handle_relay_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.startswith(("RELAY_DELIVER:", "RELAY_RENEW:", "RELAY_ERROR:", "RELAY_BATCH:")):
        return
    if not user_relay.relay_user_id or update.effective_user.id != user_relay.relay_user_id:
        return

    is_renew = text.startswith("RELAY_RENEW:")
    is_error = text.startswith("RELAY_ERROR:")
    is_batch = text.startswith("RELAY_BATCH:")
    parts = text.split(":", 2)
    if len(parts) != 3:
        return
    try:
        target_user_id = int(parts[1])
    except ValueError:
        return
    code = parts[2]

    if is_error:
        reason = code.split(":", 1)[0] if ":" in code else ""
        logger.info(
            f"[handle_relay_delivery] 外部机器人返回错误: code={code}, reason={reason}"
        )
        await context.bot.send_message(
            chat_id=target_user_id,
            text="外部文件码查询失败，该码可能已失效或暂时不可用，请稍后重试。",
        )
        return

    if is_renew:
        logger.info(
            f"[handle_relay_delivery] 记录已过期，通知用户重新请求: {code}"
        )
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"文件码 {code} 的缓存已过期，请重新发送该码以获取最新文件。",
        )
        return

    if is_batch:
        if "\n" in code:
            code = code.split("\n")[0].strip()
        storage_ids = _parse_storage_ids_from_caption(text)
        logger.info(
            f"[handle_relay_delivery] RELAY_BATCH: user={target_user_id}, code={code}, "
            f"storage_ids={storage_ids}"
        )
        if not storage_ids:
            return

        await _enqueue_storage_ids(target_user_id, code, storage_ids, context)
        return

    logger.info(
        f"[handle_relay_delivery] 中继代发: 用户 {target_user_id}, 码 {code}"
    )

    files_col = get_file_records_col()
    record = await files_col.find_one({"file_code": code})
    if not record:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"您请求的文件码 {code} 已处理，请重新发送该码获取文件。",
        )
        return

    selected_channel = channel_selector.select_channel(
        preferred_channel_id=record.get("primary_channel_id")
    )
    msg_ids_raw = record.get("batch_msg_ids") or ""
    if not isinstance(msg_ids_raw, str):
        msg_ids_raw = str(msg_ids_raw)
    msg_ids = [int(mid) for mid in msg_ids_raw.split(",") if mid.strip().isdigit()]
    if not msg_ids:
        msg_ids = [record.get("primary_channel_msg_id")]

    if len(msg_ids) > 1:
        await enqueue_batch_send_task(
            target_user_id=target_user_id,
            channel_id=selected_channel,
            channel_msg_ids=msg_ids,
            batch_file_meta=record.get("batch_file_meta", ""),
            file_code=code,
            page=1,
        )
    else:
        await enqueue_send_task(
            target_user_id=target_user_id,
            channel_id=selected_channel,
            message_id=msg_ids[0],
            file_code=code,
        )
    await context.bot.send_message(
        chat_id=target_user_id,
        text=f"您请求的文件码 {code} 已就绪，正在发送，请查收。",
    )
    logger.info(f"[handle_relay_delivery] 已入队发送: 用户 {target_user_id}, 码 {code}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    try:
        await get_or_create_user(
            user.id,
            username=user.username,
            first_name=user.first_name,
        )
    except Exception as e:
        logger.error(f"[start] 创建用户失败 (user={user.id}): {e}")
        await update.message.reply_text("系统繁忙，请稍后重试。")
        return
    await update.message.reply_text(
        "欢迎使用文件解码机器人！\n\n"
        "发送文件码即可获取对应文件。\n"
        "发送 /status 查看您的会员状态和今日剩余解码次数。\n"
        "发送 /help 查看帮助信息。"
        + three_bot_reminder()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    await update.message.reply_text(
        "文件解码机器人 使用帮助\n\n"
        "1. 获取文件：直接发送文件码即可获取文件。\n"
        "2. 上传文件：请使用上传机器人发送文件，上传后会自动收到文件码。\n"
        "3. 分享文件：将文件码分享给其他用户，对方发送给我即可获取文件。\n\n"
        "会员权益：\n"
        f"- 免费用户：每日解码 {settings.FREE_DAILY_QUOTA} 次，仅限本系统文件码\n"
        f"- 基础会员：每日解码 {settings.BASIC_DAILY_QUOTA} 次，可上传，可解码非本系统文件码\n"
        f"- 高级会员：无限解码，可上传，可解码非本系统文件码\n\n"
        "文件码格式说明：\n"
        "码的开头即对应机器人的用户名（Telegram 机器人必须以 bot 结尾）。\n"
        f"本系统码如：{settings.FILE_CODE_PREFIX}_a1b2c3d4e5f6_3p_2v_1d\n"
        "外部码如：QQfile2_bot:qq10ad1e0200_6V\n"
        "系统会根据 _bot 自动识别目标机器人并路由解码。\n\n"
        "文件码永久有效，不会过期。\n\n"
        "如有问题请联系管理员。"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    try:
        db_user = await get_or_create_user(user.id)
    except Exception as e:
        logger.error(f"[status] 获取用户信息失败 (user={user.id}): {e}")
        await update.message.reply_text("系统繁忙，无法获取用户信息，请稍后重试。")
        return
    level_map = {
        "free": "免费用户",
        "basic": "基础会员",
        "premium": "高级会员",
    }
    level_name = level_map.get(db_user.get("membership_level"), "未知")

    today = datetime.datetime.now(datetime.UTC).date()
    quota_date_str = db_user.get("quota_date")
    quota_date = None
    if quota_date_str:
        try:
            quota_date = datetime.datetime.fromisoformat(quota_date_str).date()
        except (ValueError, TypeError):
            pass

    total = db_user.get("daily_decode_quota", settings.FREE_DAILY_QUOTA)
    if quota_date == today:
        used = db_user.get("quota_used_today", 0)
    else:
        used = 0

    if db_user.get("membership_level") == "premium":
        quota_str = "无限"
    else:
        remaining = max(0, total - used)
        quota_str = f"{remaining}/{total}"

    external_quota_date_str = db_user.get("external_quota_date")
    external_quota_date = None
    if external_quota_date_str:
        try:
            external_quota_date = datetime.datetime.fromisoformat(external_quota_date_str).date()
        except (ValueError, TypeError):
            pass

    ext_quota = db_user.get("external_decode_quota", 0)
    if external_quota_date == today:
        ext_used = db_user.get("external_used_today", 0)
    else:
        ext_used = 0

    if ext_quota == -1:
        ext_str = "不限"
    elif ext_quota == 0:
        ext_str = "无权限"
    else:
        ext_remaining = max(0, ext_quota - ext_used)
        ext_str = f"{ext_remaining}/{ext_quota}"

    await update.message.reply_text(
        f"用户状态\n"
        f"会员等级：{level_name}\n"
        f"今日剩余解码次数：{quota_str}\n"
        f"上传权限：{'有' if db_user.get('can_upload') else '无'}\n"
        f"外部码解码配额：{ext_str}"
    )


async def _process_pending_uploads(app: Application):
    while True:
        try:
            pending_col = get_pending_uploads_col()
            rows = await pending_col.find({"processed": 0}, limit=5)

            for row in rows:
                pend_id = row.get("id")
                uploader_id = row.get("uploader_id")
                channel_id = row.get("primary_channel_id")
                message_id = row.get("primary_channel_msg_id")
                file_types = row.get("file_types", {})
                if not isinstance(file_types, dict):
                    file_types = {}
                batch_msg_ids_str = row.get("batch_msg_ids", "")
                batch_file_meta_str = row.get("batch_file_meta", "")

                if not uploader_id or not channel_id or not message_id:
                    await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
                    continue

                try:
                    file_code = await generate_unique_code(file_types)
                except Exception as e:
                    logger.error(f"[poll] 生成文件码失败 (uploader={uploader_id}): {e}")
                    try:
                        await app.bot.send_message(
                            chat_id=uploader_id,
                            text="文件处理失败，请稍后重试或联系管理员。",
                        )
                    except Exception:
                        pass
                    await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
                    continue

                try:
                    files_col = get_file_records_col()
                    record = make_file_record(
                        file_code=file_code,
                        uploader_id=uploader_id,
                        primary_channel_id=channel_id,
                        primary_channel_msg_id=message_id,
                        file_types=file_types,
                        batch_msg_ids=batch_msg_ids_str,
                        batch_file_meta=batch_file_meta_str,
                    )
                    await files_col.insert_one(record)
                except Exception as e:
                    logger.error(f"[poll] 数据库写入失败 (uploader={uploader_id}, code={file_code}): {e}")
                    try:
                        await app.bot.send_message(
                            chat_id=uploader_id,
                            text="文件处理失败，请稍后重试或联系管理员。",
                        )
                    except Exception:
                        pass
                    await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
                    continue

                try:
                    type_map = {
                        "photo": "张图片", "video": "个视频", "document": "个文档",
                        "audio": "个音频", "animation": "个动画",
                    }
                    type_desc = " ".join(
                        f"{v}{type_map.get(k, k)}"
                        for k, v in sorted(file_types.items())
                    ) if file_types else "文件"
                    await app.bot.send_message(
                        chat_id=uploader_id,
                        text=f"您的文件码已生成：{file_code}\n"
                             f"文件内容：{type_desc}\n"
                             f"有效期：永久有效\n"
                             f"发送此码给 @{settings.DECODER_BOT_USERNAME} 即可获取文件，"
                             f"文件将由 @{settings.SENDER_BOT_USERNAME} 发送给您。",
                    )
                    logger.info(f"[poll] 文件码已发送给用户 {uploader_id}: {file_code}")
                except Exception as e:
                    logger.error(f"[poll] 向用户 {uploader_id} 发送文件码失败 (code={file_code}): {e}")

                await pending_col.update_one({"id": pend_id}, {"$set": {"processed": 1}})
                metrics.decode_count += 1
                metrics.record_processed("decoder_bot")

        except Exception as e:
            logger.error(f"[poll] pending_uploads 轮询异常: {e}")

        await asyncio.sleep(2)


async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
    text = update.message.text.strip()

    if not is_valid_code_format(text):
        return

    if not global_rate_limiter.acquire():
        await update.message.reply_text("系统繁忙，请稍后重试。")
        return
    if not user_rate_limiter.acquire(user.id):
        await update.message.reply_text("操作过于频繁，请稍后重试。")
        return

    await get_or_create_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
    )

    result = await check_decode_permission(user.id, text)

    if not result.allowed:
        await update.message.reply_text(result.reason)
        return

    if result.is_external:
        if user_relay.is_ready and user_relay.relay_user_id:
            from utils.code_decoder import is_likely_bot_api_file_id
            if is_likely_bot_api_file_id(text):
                await user_relay.deliver_cached(user.id, text)
                await update.message.reply_text("正在发送文件，请稍候...")
                return
        await handle_external_code(update, context, user.id, text, result)
        return

    file_record = result.file_record
    selected_channel = channel_selector.select_channel(
        preferred_channel_id=file_record.get("primary_channel_id")
    )

    try:
        logs_col = get_decode_logs_col()
        log_doc = make_decode_log(
            file_code=text,
            requester_id=user.id,
            status="queued",
            source_channel_id=selected_channel,
        )
        await logs_col.insert_one(log_doc)
    except Exception as e:
        logger.error(f"[handle_code] 解码日志写入失败 (user={user.id}, code={text}): {e}")

    batch_ids_str = file_record.get("batch_msg_ids") or ""
    if not isinstance(batch_ids_str, str):
        batch_ids_str = str(batch_ids_str)
    msg_ids = []
    if batch_ids_str:
        msg_ids = [int(mid) for mid in batch_ids_str.split(",") if mid.strip().isdigit()]
    if not msg_ids:
        msg_ids = [file_record.get("primary_channel_msg_id")]

    batch_file_meta_str = file_record.get("batch_file_meta") or ""

    try:
        if len(msg_ids) > 1:
            await enqueue_batch_send_task(
                target_user_id=user.id,
                channel_id=selected_channel,
                channel_msg_ids=msg_ids,
                batch_file_meta=batch_file_meta_str,
                file_code=text,
                page=1,
            )
        else:
            await enqueue_send_task(
                target_user_id=user.id,
                channel_id=selected_channel,
                message_id=msg_ids[0],
                file_code=text,
            )
        logger.info(f"[handle_code] 已入队发送任务 (user={user.id}, code={text}, channel={selected_channel})")
    except Exception as e:
        logger.error(f"[handle_code] 入队发送任务失败 (user={user.id}, code={text}): {e}")
        await update.message.reply_text("系统繁忙，文件发送请求失败，请稍后重试。")
        return

    remaining_info = ""
    if result.remaining_quota >= 0:
        remaining_info = f"今日剩余解码次数：{result.remaining_quota}"

    await update.message.reply_text(
        f"文件将由 @{settings.SENDER_BOT_USERNAME} 发送给您，请查收。\n{remaining_info}"
    )

    metrics.decode_count += 1
    metrics.record_processed("decoder_bot")
    logger.info(f"[handle_code] 用户 {user.id} 请求文件码 {text}，频道 {selected_channel}")


def _resolve_bot_username(update: Update) -> str | None:
    if update.effective_chat and update.effective_chat.username:
        return update.effective_chat.username

    msg = update.message
    if msg and msg.forward_origin and hasattr(msg.forward_origin, "sender_user"):
        sender = msg.forward_origin.sender_user
        if sender and sender.username:
            return sender.username

    if msg and hasattr(msg, "forward_from") and msg.forward_from:
        return msg.forward_from.username

    return None


async def handle_external_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    code: str,
    result,
):
    bot_username = result.external_bot_username
    logger.info(f"[handle_external_code] 用户 {user_id} 请求外部码 {code}，目标机器人 @{bot_username}")

    remaining_info = ""
    parts = []
    if result.remaining_quota >= 0:
        parts.append(f"总解码剩余：{result.remaining_quota}")
    if result.remaining_external_quota >= 0:
        parts.append(f"外部码剩余：{result.remaining_external_quota}")
    if parts:
        remaining_info = " | ".join(parts)

    if user_relay.is_ready:
        ok = await user_relay.send_external_code(bot_username, code, user_id)
        if ok:
            _enqueue_external(bot_username, user_id, code)
            await update.message.reply_text(
                f"正在查询外部文件码，请稍候查收。\n{remaining_info}"
            )
            metrics.decode_count += 1
            metrics.record_processed("decoder_bot")
            return

    try:
        await context.bot.send_message(
            chat_id=f"@{bot_username}",
            text=code,
        )
        _enqueue_external(bot_username, user_id, code)
        await update.message.reply_text(
            f"正在查询外部文件码，请稍候查收。\n{remaining_info}"
        )
        metrics.decode_count += 1
        metrics.record_processed("decoder_bot")
        return
    except Exception as e:
        err_msg = str(e)
        logger.warning(
            f"[handle_external_code] 无法发送到 @{bot_username} "
            f"(user={user_id}, code={code}): {err_msg}"
        )
        if "chat not found" in err_msg.lower() or "nobody is using" in err_msg.lower():
            await update.message.reply_text(
                f"机器人 @{bot_username} 未找到，请检查文件码中的机器人用户名是否正确。"
            )
        else:
            await update.message.reply_text(
                "外部码解码功能暂不可用，请联系管理员配置用户中继。"
            )
        return


async def handle_external_file_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = _resolve_bot_username(update)
    if not bot_username:
        return

    target_user_id, code = _dequeue_external(bot_username)
    if target_user_id is None:
        return

    logger.info(
        f"[handle_external_file_response] 收到外部机器人 @{bot_username} 的文件响应，转发给用户 {target_user_id}，码 {code}"
    )

    forwarded = None
    try:
        forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
        await _cache_external_file(context, code, forwarded.message_id)
        logger.info(f"[handle_external_file_response] 外部码 {code} 的文件已缓存到本地频道 {MAIN_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"[handle_external_file_response] 缓存外部文件到本地频道失败 (code={code}): {e}")
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="外部文件转发失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
        return

    try:
        await enqueue_send_task(
            target_user_id=target_user_id,
            channel_id=MAIN_CHANNEL_ID,
            message_id=forwarded.message_id,
            file_code=code,
        )
        logger.info(f"[handle_external_file_response] 外部文件已入队 sender_bot: 用户 {target_user_id}, 码 {code}")
    except Exception as e:
        logger.error(f"[handle_external_file_response] 入队发送失败 (code={code}): {e}")


async def handle_external_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = _resolve_bot_username(update)
    if not bot_username:
        return

    target_user_id, code = _dequeue_external(bot_username)
    if target_user_id is None:
        return

    logger.info(
        f"[handle_external_media] 收到外部机器人 @{bot_username} 的媒体响应，转发给用户 {target_user_id}，码 {code}"
    )

    forwarded = None
    try:
        forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
        await _cache_external_file(context, code, forwarded.message_id)
    except Exception as e:
        logger.error(f"[handle_external_media] 缓存外部媒体文件到本地频道失败 (code={code}): {e}")
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="外部文件转发失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
        return

    try:
        await enqueue_send_task(
            target_user_id=target_user_id,
            channel_id=MAIN_CHANNEL_ID,
            message_id=forwarded.message_id,
            file_code=code,
        )
    except Exception as e:
        logger.error(f"[handle_external_media] 入队发送失败 (code={code}): {e}")


async def _handle_relay_file_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").strip()
    if not caption.startswith("RELAY_FILE:"):
        return False
    if not user_relay.relay_user_id or not update.effective_user:
        return False
    if update.effective_user.id != user_relay.relay_user_id:
        return False

    rest = caption[len("RELAY_FILE:"):]
    user_end = rest.find(":")
    if user_end == -1:
        return False
    try:
        target_user_id = int(rest[:user_end])
    except ValueError:
        logger.warning(f"[RELAY_FILE] 无效的 user_id: {rest[:user_end]}")
        return False
    after_user = rest[user_end + 1:]
    code_part = after_user.split("\n\n", 1)[0].strip()
    orig_caption = after_user.split("\n\n", 1)[1] if "\n\n" in after_user else ""

    if not target_user_id or not code_part:
        return False

    try:
        storage_ids = _parse_storage_ids_from_caption(caption)

        if not storage_ids:
            logger.warning(f"[RELAY_FILE] 无可用的 STORAGE_IDS (code={code_part})")
            return True

        files_col = get_file_records_col()
        record = await files_col.find_one({"file_code": code_part})
        if not record:
            logger.warning(f"[RELAY_FILE] 数据库中未找到文件记录: {code_part}")
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"您请求的文件码 {code_part} 发送失败，请稍后重试或联系管理员。",
                )
            except Exception:
                pass
            return True

        storage_channel = record.get("primary_channel_id", MAIN_CHANNEL_ID)

        file_ids_str = record.get("file_ids") or ""
        all_file_ids = [f for f in file_ids_str.split(",") if f.strip()] if file_ids_str else []

        batch_all_ids = record.get("batch_msg_ids") or ""
        all_msg_ids = []
        primary_mid = record.get("primary_channel_msg_id")
        if primary_mid:
            all_msg_ids.append(str(primary_mid))
        if batch_all_ids:
            for mid in batch_all_ids.split(","):
                m = mid.strip()
                if m and m not in all_msg_ids:
                    all_msg_ids.append(m)

        msg_id_to_file_id = {}
        for i, mid_str in enumerate(all_msg_ids):
            msg_id_to_file_id[mid_str] = all_file_ids[i] if i < len(all_file_ids) else ""

        batch_file_meta = []
        for sid in storage_ids:
            sid_str = str(sid)
            fid = msg_id_to_file_id.get(sid_str, "")
            batch_file_meta.append({"file_id": fid, "type": "document"})

        if len(storage_ids) > 1 and any(m.get("file_id") for m in batch_file_meta):
            await enqueue_batch_send_task(
                target_user_id=target_user_id,
                channel_id=storage_channel,
                channel_msg_ids=list(storage_ids),
                batch_file_meta=json.dumps(batch_file_meta),
                file_code=code_part,
            )
        else:
            for sid in storage_ids:
                await enqueue_send_task(
                    target_user_id=target_user_id,
                    channel_id=storage_channel,
                    message_id=sid,
                    file_code=code_part,
                )

        logger.info(f"[RELAY_FILE] 已入队 sender_bot: 用户 {target_user_id} (code={code_part})")
    except Exception as e:
        logger.error(f"[RELAY_FILE] 处理中继文件失败 (user={target_user_id}, code={code_part}): {e}")
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"您请求的文件码 {code_part} 发送失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text or ""

        if text.startswith(("RELAY_DELIVER:", "RELAY_RENEW:", "RELAY_ERROR:", "RELAY_BATCH:")):
            await handle_relay_delivery(update, context)
            return

        if is_valid_code_format(text.strip()):
            await handle_code(update, context)
            return
    except Exception as e:
        logger.error(f"[handle_message] 处理消息异常 (user={update.effective_user.id if update.effective_user else 'unknown'}): {e}")
        try:
            await update.message.reply_text("处理请求时发生错误，请稍后重试。")
        except Exception:
            pass


async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    logger.info("启动解码机器人...")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    media_filter = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.PHOTO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
    )

    async def _route_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message is None:
            return
        try:
            mg_id = update.message.media_group_id
            if mg_id:
                caption = (update.message.caption or "").strip()

                if mg_id not in _media_group_buffer:
                    user_id = None
                    code = None
                    source = None

                    if caption.startswith("RELAY_FILE:"):
                        source = "relay"
                        parts = caption.split(":", 3)
                        if len(parts) >= 3:
                            try:
                                user_id = int(parts[1])
                            except ValueError:
                                pass
                            code = parts[2].split("\n")[0]
                    else:
                        source = "external"
                        bot_username = _resolve_bot_username(update)
                        if bot_username:
                            user_id, code = _dequeue_external(bot_username)

                    if not user_id or not code:
                        logger.warning(f"[_route_media] 无法确定媒体组用户/码 (mg_id={mg_id})")
                        return

                    timer = asyncio.create_task(_flush_media_group_buffer(mg_id))
                    _media_group_buffer[mg_id] = {
                        "source": source,
                        "bot": context.bot,
                        "msgs": [],
                        "user_id": user_id,
                        "code": code,
                        "timer": timer,
                    }

                _media_group_buffer[mg_id]["msgs"].append(
                    (update.message.chat_id, update.message.message_id, caption)
                )
                return

            if await _handle_relay_file_media(update, context):
                return
            await handle_external_file_response(update, context)
        except Exception as e:
            logger.error(f"[_route_media] 处理媒体消息异常: {e}")

    app.add_handler(MessageHandler(media_filter, _route_media))

    metrics.ping_bot("decoder_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("decoder_bot")
            await asyncio.sleep(30)

    async def cleanup_loop():
        while True:
            _cleanup_stale_pending()
            _cleanup_media_groups()
            await asyncio.sleep(60)

    loop.create_task(health_ping())
    loop.create_task(_process_pending_uploads(app))
    loop.create_task(cleanup_loop())
    loop.create_task(user_relay.start())
    app.run_polling()


if __name__ == "__main__":
    run()