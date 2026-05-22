import datetime
import re

from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import settings
from database import get_users_col, get_file_records_col, get_decode_logs_col
from utils.monitor import metrics

TOKEN = settings.ADMIN_BOT_TOKEN
AUTHORIZED_USER_ID = settings.ADMIN_TELEGRAM_ID
MEMBERSHIP_LEVELS = {"free": "免费用户", "basic": "基础会员", "premium": "高级会员"}


def _auth_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != AUTHORIZED_USER_ID:
            await update.message.reply_text("⛔ 您没有权限使用此机器人。")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _format_datetime(dt) -> str:
    if dt is None:
        return "N/A"
    if isinstance(dt, datetime.datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(dt, str):
        try:
            return datetime.datetime.fromisoformat(dt).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return dt
    return str(dt)


def _quota_display(val: int) -> str:
    if val == -1:
        return "不限"
    return str(val)


@_auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 管理员机器人已启动\n\n"
        "可用命令：\n\n"
        "📊 系统状态\n"
        "  /status - 系统概览\n"
        "  /health - 机器人运行状态\n\n"
        "👤 用户管理\n"
        "  /user <id> - 查看用户详情\n"
        "  /users [关键词] [页码] - 用户列表\n"
        "  /set_level <id> <等级> - 设置会员等级 (free/basic/premium)\n"
        "  /ban <id> - 封禁用户\n"
        "  /unban <id> - 解封用户\n"
        "  /set_quota <id> <数量> - 设置每日解码配额\n"
        "  /set_external_quota <id> <数量> - 设置外部码配额\n\n"
        "📁 文件管理\n"
        "  /file <code> - 查看文件详情\n"
        "  /files [搜索] [页码] - 文件列表\n"
        "  /delete_file <code> - 删除文件\n\n"
        "📋 日志\n"
        "  /logs [页码] - 解码日志"
    )


@_auth_required
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()

    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})
    active_files = await files_col.count_documents({"status": "active"})
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_decodes = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})

    msg = (
        f"📊 系统概览\n\n"
        f"👤 总用户数：{total_users}\n"
        f"📁 总文件数：{total_files}\n"
        f"✅ 活跃文件：{active_files}\n"
        f"🔄 今日解码：{today_decodes}\n"
        f"📤 发送成功：{metrics.send_success_count}\n"
        f"📤 发送失败：{metrics.send_fail_count}\n"
        f"💾 备份成功：{metrics.backup_count}\n"
        f"❌ 备份失败：{metrics.backup_fail_count}\n"
        f"\n🤖 机器人状态：\n"
    )

    for name, health in metrics.bots.items():
        status_icon = "✅" if health.is_running else "❌"
        msg += f"  {status_icon} {name}: {health.total_processed}次/ {health.total_errors}次错误\n"

    await update.message.reply_text(msg)


@_auth_required
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🤖 机器人健康状态\n\n"
    for name, health in metrics.bots.items():
        status_icon = "✅" if health.is_running else "❌"
        last_ping = _format_datetime(health.last_ping)
        msg += (
            f"{status_icon} {name}\n"
            f"  最后活跃：{last_ping}\n"
            f"  处理次数：{health.total_processed}\n"
            f"  错误次数：{health.total_errors}\n"
        )
    await update.message.reply_text(msg)


@_auth_required
async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/user <用户ID>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return

    level = user.get("membership_level", "free")
    msg = (
        f"👤 用户详情\n\n"
        f"🆔 ID：{user.get('user_id')}\n"
        f"📝 用户名：@{user.get('username') or 'N/A'}\n"
        f"👤 昵称：{user.get('first_name') or 'N/A'}\n"
        f"🏅 会员等级：{MEMBERSHIP_LEVELS.get(level, level)}\n"
        f"🔒 是否封禁：{'是 ❌' if user.get('is_banned') else '否 ✅'}\n"
        f"📤 允许上传：{'是 ✅' if user.get('can_upload') else '否 ❌'}\n"
        f"📅 解码配额：{_quota_display(user.get('daily_decode_quota'))}/天\n"
        f"📊 今日已用：{user.get('quota_used_today', 0)}次\n"
        f"🌐 外部码配额：{_quota_display(user.get('external_decode_quota'))}/天\n"
        f"🌐 外部已用：{user.get('external_used_today', 0)}次\n"
        f"📅 注册时间：{_format_datetime(user.get('created_at'))}\n"
        f"🔄 更新时间：{_format_datetime(user.get('updated_at'))}"
    )
    await update.message.reply_text(msg)


@_auth_required
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    search = ""
    page = 1
    for arg in args:
        if arg.isdigit():
            page = int(arg)
        else:
            search = arg

    per_page = 10
    users_col = get_users_col()
    query = {}
    if search:
        if search.isdigit():
            query["user_id"] = int(search)
        else:
            query["$or"] = [
                {"username": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
            ]

    total = await users_col.count_documents(query)
    skip = (page - 1) * per_page
    users = await users_col.find(query, sort=("created_at", -1), skip=skip, limit=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    msg = f"👤 用户列表 (第{page}/{total_pages}页，共{total}人)\n"
    if search:
        msg += f"🔍 搜索：{search}\n"
    msg += "\n"

    for u in users:
        level_icon = {"free": "🆓", "basic": "🥇", "premium": "👑"}.get(u.get("membership_level", "free"), "🆓")
        ban_icon = "🔒" if u.get("is_banned") else ""
        name = u.get("username") or u.get("first_name") or f"ID:{u.get('user_id')}"
        msg += f"{level_icon}{ban_icon} {u.get('user_id')} - @{name}\n"

    if total_pages > 1:
        msg += f"\n使用 /users {search} {page+1} 查看下一页" if not search else f"\n使用 /users {search} {page+1} 查看下一页"

    await update.message.reply_text(msg)


@_auth_required
async def set_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法：/set_level <用户ID> <free|basic|premium>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return
    level = args[1].lower()
    if level not in MEMBERSHIP_LEVELS:
        await update.message.reply_text("❌ 等级必须是 free、basic 或 premium")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return

    update_doc = {
        "$set": {
            "membership_level": level,
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }
    }
    if level == "free":
        update_doc["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
        update_doc["$set"]["can_upload"] = True
        update_doc["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
        update_doc["$set"]["external_used_today"] = 0
    elif level == "basic":
        update_doc["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
        update_doc["$set"]["can_upload"] = True
        update_doc["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
        update_doc["$set"]["external_used_today"] = 0
    elif level == "premium":
        update_doc["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
        update_doc["$set"]["can_upload"] = True
        update_doc["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
        update_doc["$set"]["external_used_today"] = 0

    await users_col.update_one({"user_id": user_id}, update_doc)
    await update.message.reply_text(f"✅ 用户 {user_id} 已设置为 {MEMBERSHIP_LEVELS[level]}")


@_auth_required
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/ban <用户ID>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": True, "updated_at": datetime.datetime.utcnow().isoformat()}},
    )
    await update.message.reply_text(f"✅ 用户 {user_id} 已封禁")


@_auth_required
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/unban <用户ID>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": False, "updated_at": datetime.datetime.utcnow().isoformat()}},
    )
    await update.message.reply_text(f"✅ 用户 {user_id} 已解封")


@_auth_required
async def set_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法：/set_quota <用户ID> <每日解码配额(-1为不限)>")
        return
    try:
        user_id = int(args[0])
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 用户ID和配额必须是数字")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.utcnow().isoformat()}},
    )
    await update.message.reply_text(f"✅ 用户 {user_id} 每日解码配额已设为 {_quota_display(quota)}")


@_auth_required
async def set_external_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法：/set_external_quota <用户ID> <外部码配额(-1为不限，0为禁止)>")
        return
    try:
        user_id = int(args[0])
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 用户ID和配额必须是数字")
        return

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        await update.message.reply_text(f"❌ 用户 {user_id} 不存在")
        return
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.utcnow().isoformat()}},
    )
    await update.message.reply_text(f"✅ 用户 {user_id} 外部码配额已设为 {_quota_display(quota)}")


@_auth_required
async def file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/file <文件码>")
        return
    file_code = args[0]

    files_col = get_file_records_col()
    record = await files_col.find_one({"file_code": file_code})
    if record is None:
        await update.message.reply_text(f"❌ 文件码 {file_code} 不存在")
        return

    file_types = record.get("file_types", {})
    if isinstance(file_types, str):
        import json
        file_types = json.loads(file_types) if file_types else {}
    type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items())) if file_types else "外部缓存文件"

    backups = record.get("backup_channel_msg_ids", [])
    if isinstance(backups, str):
        import json
        backups = json.loads(backups) if backups else []

    msg = (
        f"📁 文件详情\n\n"
        f"🔑 文件码：{file_code}\n"
        f"👤 上传者：{record.get('uploader_id')}\n"
        f"📦 文件类型：{type_desc}\n"
        f"📊 状态：{record.get('status', 'active')}\n"
        f"📈 请求次数：{record.get('request_count', 0)}\n"
        f"📅 创建时间：{_format_datetime(record.get('create_time'))}\n"
        f"📺 主频道：{record.get('primary_channel_id')}\n"
        f"🔄 备份数：{len(backups)}个频道\n"
    )
    await update.message.reply_text(msg)


@_auth_required
async def files_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    search = ""
    page = 1
    for arg in args:
        if arg.isdigit():
            page = int(arg)
        else:
            search = arg

    per_page = 10
    files_col = get_file_records_col()
    query = {}
    if search:
        if search.isdigit():
            query["uploader_id"] = int(search)
        else:
            query["file_code"] = {"$regex": search, "$options": "i"}

    total = await files_col.count_documents(query)
    skip = (page - 1) * per_page
    files = await files_col.find(query, sort=("create_time", -1), skip=skip, limit=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    msg = f"📁 文件列表 (第{page}/{total_pages}页，共{total}个)\n"
    if search:
        msg += f"🔍 搜索：{search}\n"
    msg += "\n"

    for f in files:
        status_icon = "✅" if f.get("status") == "active" else "🗑️"
        fc = f.get("file_code", "N/A")
        uploader = f.get("uploader_id", "?")
        msg += f"{status_icon} {fc} (上传者:{uploader})\n"

    if total_pages > 1:
        ns = f" {search}" if search else ""
        msg += f"\n使用 /files{ns} {page+1} 查看下一页"

    await update.message.reply_text(msg)


@_auth_required
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/delete_file <文件码>")
        return
    file_code = args[0]

    files_col = get_file_records_col()
    result = await files_col.update_one(
        {"file_code": file_code},
        {"$set": {"status": "deleted"}},
    )
    if result.matched_count == 0:
        await update.message.reply_text(f"❌ 文件码 {file_code} 不存在")
        return
    await update.message.reply_text(f"✅ 文件 {file_code} 已删除")


@_auth_required
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    per_page = 15
    logs_col = get_decode_logs_col()
    total = await logs_col.count_documents({})
    skip = (page - 1) * per_page
    logs_data = await logs_col.find(sort=("request_time", -1), skip=skip, limit=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    msg = f"📋 解码日志 (第{page}/{total_pages}页)\n\n"
    for log in logs_data:
        status_icon = "✅" if log.get("status") == "success" else "⏳" if log.get("status") == "queued" else "❌"
        fc = (log.get("file_code") or "")[:30]
        requester = log.get("requester_id", "?")
        t = _format_datetime(log.get("request_time"))
        msg += f"{status_icon} [{t}] {fc} - 用户{requester}\n"

    if total_pages > 1:
        msg += f"\n使用 /logs {page+1} 查看下一页"

    await update.message.reply_text(msg)


async def _init():
    from database import init_db
    await init_db()


def run():
    if not TOKEN:
        logger.warning("管理员机器人 Token 未配置（ADMIN_BOT_TOKEN），跳过启动")
        return
    if not AUTHORIZED_USER_ID:
        logger.warning("管理员 Telegram ID 未配置（ADMIN_TELEGRAM_ID），跳过启动")
        return

    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(_init())
    logger.info("启动管理员机器人...")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("user", user_detail))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("set_level", set_level))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("set_quota", set_quota))
    app.add_handler(CommandHandler("set_external_quota", set_external_quota))
    app.add_handler(CommandHandler("file", file_detail))
    app.add_handler(CommandHandler("files", files_list))
    app.add_handler(CommandHandler("delete_file", delete_file))
    app.add_handler(CommandHandler("logs", logs))

    app.run_polling()
