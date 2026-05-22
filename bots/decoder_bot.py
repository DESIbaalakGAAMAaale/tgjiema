import asyncio
import datetime
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
from services.queue_manager import enqueue_send_task
from utils.rate_limiter import global_rate_limiter, user_rate_limiter
from utils.channel_selector import channel_selector
from utils.monitor import metrics

TOKEN = settings.DECODER_BOT_TOKEN
MAIN_CHANNEL_ID = settings.MAIN_STORAGE_CHANNEL_ID

_pending_external: dict[str, deque[tuple[int, str]]] = defaultdict(deque)


def _enqueue_external(bot_username: str, user_id: int, code: str):
    _pending_external[bot_username].append((user_id, code))


def _dequeue_external(bot_username: str) -> tuple[int, str]:
    q = _pending_external.get(bot_username)
    if q:
        return q.popleft()
    return None, None


_external_media_groups: dict[str, tuple[int, str]] = {}


def _track_external_media_group(media_group_id: str, user_id: int, code: str):
    _external_media_groups[media_group_id] = (user_id, code)


def _get_external_media_group_user(media_group_id: str) -> tuple[int, str]:
    return _external_media_groups.pop(media_group_id, (None, None))


async def _cache_external_file(
    context: ContextTypes.DEFAULT_TYPE, code: str, message_id: int
):
    try:
        files_col = get_file_records_col()
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    today = datetime.datetime.utcnow().date()
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
                             f"您可将其分享给他人，对方通过向我发送此码即可获取文件。",
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
    msg_ids = []
    if batch_ids_str:
        msg_ids = [int(mid) for mid in batch_ids_str.split(",") if mid.strip().isdigit()]
    if not msg_ids:
        msg_ids = [file_record.get("primary_channel_msg_id")]

    try:
        for mid in msg_ids:
            await enqueue_send_task(
                target_user_id=user.id,
                channel_id=selected_channel,
                message_id=mid,
                file_code=text,
            )
    except Exception as e:
        logger.error(f"[handle_code] 入队发送任务失败 (user={user.id}, code={text}): {e}")
        await update.message.reply_text("系统繁忙，文件发送请求失败，请稍后重试。")
        return

    remaining_info = ""
    if result.remaining_quota >= 0:
        remaining_info = f"今日剩余解码次数：{result.remaining_quota}"

    await update.message.reply_text(
        f"文件发送请求已接受，请查收。\n{remaining_info}"
    )

    metrics.decode_count += 1
    metrics.record_processed("decoder_bot")
    logger.info(f"[handle_code] 用户 {user.id} 请求文件码 {text}，频道 {selected_channel}")


async def handle_external_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    code: str,
    result,
):
    bot_username = result.external_bot_username
    logger.info(f"[handle_external_code] 用户 {user_id} 请求外部码 {code}，目标机器人 @{bot_username}")

    try:
        await context.bot.send_message(
            chat_id=f"@{bot_username}",
            text=code,
        )
    except Exception as e:
        logger.error(f"[handle_external_code] 发送外部码到 @{bot_username} 失败 (user={user_id}, code={code}): {e}")
        await update.message.reply_text(
            f"无法联系目标机器人 @{bot_username}，请确认码是否正确或稍后重试。"
        )
        return

    _enqueue_external(bot_username, user_id, code)

    remaining_info = ""
    parts = []
    if result.remaining_quota >= 0:
        parts.append(f"总解码剩余：{result.remaining_quota}")
    if result.remaining_external_quota >= 0:
        parts.append(f"外部码剩余：{result.remaining_external_quota}")
    if parts:
        remaining_info = " | ".join(parts)

    await update.message.reply_text(
        f"已向 @{bot_username} 查询文件，请稍候查收。\n{remaining_info}"
    )

    metrics.decode_count += 1
    metrics.record_processed("decoder_bot")


async def handle_external_file_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = update.effective_chat.username
    if not bot_username:
        return

    target_user_id, code = _dequeue_external(bot_username)
    if target_user_id is None:
        return

    logger.info(
        f"[handle_external_file_response] 收到外部机器人 @{bot_username} 的文件响应，转发给用户 {target_user_id}，码 {code}"
    )

    media_group_id = update.message.media_group_id
    if media_group_id:
        _track_external_media_group(media_group_id, target_user_id, code)

    try:
        await update.message.copy(chat_id=target_user_id)
        logger.info(f"[handle_external_file_response] 外部文件转发成功: 用户 {target_user_id}, 码 {code}")
    except Exception as e:
        logger.error(f"[handle_external_file_response] 转发外部文件给用户 {target_user_id} 失败 (code={code}): {e}")
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="外部文件转发失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
        return

    try:
        forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
        await _cache_external_file(context, code, forwarded.message_id)
        logger.info(f"[handle_external_file_response] 外部码 {code} 的文件已缓存到本地频道 {MAIN_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"[handle_external_file_response] 缓存外部文件到本地频道失败 (code={code}): {e}")


async def handle_external_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    media_group_id = update.message.media_group_id
    if media_group_id:
        user_id, code = _get_external_media_group_user(media_group_id)
        if user_id:
            try:
                await update.message.copy(chat_id=user_id)
            except Exception as e:
                logger.error(f"[handle_external_media] 转发外部媒体组文件给用户 {user_id} 失败 (code={code}): {e}")
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="外部文件转发失败，请稍后重试或联系管理员。",
                    )
                except Exception:
                    pass
                return
            try:
                forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
                await _cache_external_file(context, code, forwarded.message_id)
            except Exception as e:
                logger.error(f"[handle_external_media] 缓存外部媒体组文件到本地频道失败 (code={code}): {e}")
            return

    bot_username = update.effective_chat.username
    if not bot_username:
        return

    target_user_id, code = _dequeue_external(bot_username)
    if target_user_id is None:
        return

    logger.info(
        f"[handle_external_media] 收到外部机器人 @{bot_username} 的媒体响应，转发给用户 {target_user_id}，码 {code}"
    )

    try:
        await update.message.copy(chat_id=target_user_id)
    except Exception as e:
        logger.error(f"[handle_external_media] 转发外部媒体给用户 {target_user_id} 失败 (code={code}): {e}")
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="外部文件转发失败，请稍后重试或联系管理员。",
            )
        except Exception:
            pass
        return

    try:
        forwarded = await update.message.copy(chat_id=MAIN_CHANNEL_ID)
        await _cache_external_file(context, code, forwarded.message_id)
    except Exception as e:
        logger.error(f"[handle_external_media] 缓存外部媒体文件到本地频道失败 (code={code}): {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text or ""

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
        try:
            if update.message.media_group_id:
                await handle_external_media(update, context)
            else:
                await handle_external_file_response(update, context)
        except Exception as e:
            logger.error(f"[_route_media] 处理媒体消息异常: {e}")

    app.add_handler(MessageHandler(media_filter, _route_media))

    metrics.ping_bot("decoder_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("decoder_bot")
            await asyncio.sleep(30)

    loop.create_task(health_ping())
    loop.create_task(_process_pending_uploads(app))
    app.run_polling()


if __name__ == "__main__":
    run()