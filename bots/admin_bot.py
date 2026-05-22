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
from database import (
    get_users_col, get_file_records_col, get_decode_logs_col,
    get_backup_channels, set_backup_channels,
    get_backup_bot_tokens, set_backup_bot_token, delete_backup_bot_token,
    get_config, set_config,
    get_relay_config, set_relay_config, get_relay_status,
)
from database.models import make_user
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


async def _ensure_user(user_id: int) -> dict:
    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        user = make_user(user_id=user_id)
        await users_col.insert_one(user)
    return user


async def _get_status_text() -> str:
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()
    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})
    active_files = await files_col.count_documents({"status": "active"})
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_decodes = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})
    relay_pending = await get_config("relay_auth_pending")
    relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
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
        f"\n🔐 用户中继：{relay_status}\n"
        f"\n🤖 机器人状态：\n"
    )
    for name, health in metrics.bots.items():
        status_icon = "✅" if health.is_running else "❌"
        msg += f"  {status_icon} {name}: {health.total_processed}次/ {health.total_errors}次错误\n"
    return msg


async def _get_health_text() -> str:
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
    return msg


async def _get_channels_text() -> str:
    tokens = await get_backup_bot_tokens()
    msg = "📺 备份机器人 & 频道配置\n\n"
    for i in (1, 2, 3):
        tk = tokens.get(str(i), "")
        channels = await get_backup_channels(i)
        if tk:
            masked = tk[:8] + "..." + tk[-4:] if len(tk) > 15 else "***"
            msg += f"🤖 backup_bot_{i}: {masked}\n"
        else:
            msg += f"🤖 backup_bot_{i}: (未配置Token)\n"
        if channels:
            msg += f"   频道 ({len(channels)}个):\n"
            for ch in channels:
                msg += f"     • {ch}\n"
        else:
            msg += f"   频道: (空)\n"
        msg += "\n"
    msg += "/add_channel <频道ID> — 新增频道后选择机器人\n"
    msg += "/remove_channel <机器人编号> <频道ID> — 删除备份频道\n"
    msg += "/add_backup_bot <编号> <Token> [频道ID...] — 配置Token及频道\n"
    msg += "/remove_backup_bot <编号> — 删除备份机器人\n"
    msg += "\n⚠️ 修改 Token 后需重启对应备份机器人才生效"
    return msg


async def _get_logs_page_text(page: int = 1) -> str:
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
    return msg


async def _get_users_page_text(search: str = "", page: int = 1) -> str:
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
    if total_pages > 1 and search:
        msg += f"\n使用 /users {search} {page+1} 查看下一页"
    elif total_pages > 1:
        msg += f"\n使用 /users {page+1} 查看下一页"
    return msg


async def _get_relay_status_text() -> str:
    pending = await get_config("relay_auth_pending")
    status = await get_relay_status()
    config = await get_relay_config()

    status_labels = {
        "online": "✅ 在线",
        "connecting": "🔄 连接中",
        "pending_auth": "⏳ 等待验证码",
        "offline": "❌ 离线",
    }

    msg = "🔐 用户中继状态\n\n"
    msg += f"状态：{status_labels.get(status, status)}\n"

    if config.get("api_id"):
        phone = config.get("phone", "")
        masked_phone = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        msg += f"账号：{masked_phone}\n"
        msg += f"API_ID：{config['api_id']}\n"
    else:
        msg += "⚠️ 未配置中继账号\n"
        msg += "请使用下方按钮配置 API_ID / API_HASH / 手机号\n"

    if pending == "1":
        msg += "\n⚠️ 正在等待验证码，请通过 /relay_code 提交"

    return msg


BACK_BTN = [[InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")]]


def _build_menu(menu_id: str) -> tuple[str, InlineKeyboardMarkup]:
    if menu_id == "main":
        text = "🤖 管理员面板 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:sys"),
             InlineKeyboardButton("👤 用户管理", callback_data="menu:user")],
            [InlineKeyboardButton("📁 文件管理", callback_data="menu:file"),
             InlineKeyboardButton("📺 备份频道", callback_data="menu:backup_chan")],
            [InlineKeyboardButton("🤖 备份机器人", callback_data="menu:backup_bot"),
             InlineKeyboardButton("📋 解码日志", callback_data="action:logs")],
            [InlineKeyboardButton("🔐 用户中继", callback_data="menu:relay")],
        ]
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "sys":
        text = "📊 系统状态"
        kb = [
            [InlineKeyboardButton("📈 系统概览", callback_data="action:status"),
             InlineKeyboardButton("❤️ 健康状态", callback_data="action:health")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "user":
        text = (
            "👤 用户管理\n\n"
            "用法参考：\n"
            "/user <id> — 查看用户详情\n"
            "/set_level <id> <等级> — 设置会员等级\n"
            "/ban <id> / /unban <id> — 封禁/解封\n"
            "/set_quota <id> <数量> — 解码配额\n"
            "/set_external_quota <id> <数量> — 外部码配额"
        )
        kb = [
            [InlineKeyboardButton("📋 用户列表", callback_data="action:users")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "file":
        text = (
            "📁 文件管理\n\n"
            "用法参考：\n"
            "/file <code> — 查看文件详情\n"
            "/files [搜索] [页码] — 文件列表\n"
            "/delete_file <code> — 删除文件"
        )
        kb = [
            [InlineKeyboardButton("📂 文件列表", callback_data="action:files")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "backup_chan":
        text = (
            "📺 备份频道管理\n\n"
            "/add_channel <频道ID> — 新增频道后选择机器人\n"
            "/add_channel <编号> <频道ID> — 直接指定机器人\n"
            "/remove_channel <编号> <频道ID> — 删除备份频道"
        )
        kb = [
            [InlineKeyboardButton("📺 查看配置", callback_data="action:channels")],
            [InlineKeyboardButton("➕ 新增频道", callback_data="usage:add_chan"),
             InlineKeyboardButton("➖ 删除频道", callback_data="usage:remove_chan")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "backup_bot":
        text = (
            "🤖 备份机器人管理\n\n"
            "/add_backup_bot <编号> <Token> [频道ID...] — 配置Token及频道\n"
            "/remove_backup_bot <编号> — 删除备份机器人\n\n"
            "⚠️ 修改 Token 后需重启对应备份机器人才生效"
        )
        kb = [
            [InlineKeyboardButton("📺 查看配置", callback_data="action:channels")],
            [InlineKeyboardButton("➕ 新增机器人", callback_data="usage:add_bot"),
             InlineKeyboardButton("➖ 删除机器人", callback_data="usage:remove_bot")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "relay":
        text = (
            "🔐 用户中继管理\n\n"
            "中继账号用于突破 bot-to-bot 私聊限制，\n"
            "使解码机器人可以向其他机器人发送外部码。\n\n"
            "验证码会发送到该账号已登录的 Telegram 客户端，\n"
            "而非手机短信。\n\n"
            "/relay_code <验证码> — 提交登录验证码（6位）\n"
            "/relay_set_api <api_id> <api_hash> <手机号> — 配置账号\n"
            "/relay_pending — 查看是否有待处理的验证码"
        )
        kb = [
            [InlineKeyboardButton("📊 查看状态", callback_data="action:relay_status")],
            [InlineKeyboardButton("⚙️ 配置说明", callback_data="usage:relay_config"),
             InlineKeyboardButton("🔑 验证码说明", callback_data="usage:relay_code")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    return _build_menu("main")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer("⛔ 无权限", show_alert=True)
        return

    back_kb = InlineKeyboardMarkup(BACK_BTN)

    if data.startswith("menu:"):
        menu_id = data.split(":", 1)[1]
        text, markup = _build_menu(menu_id)
        await query.edit_message_text(text, reply_markup=markup)
        return

    if data == "action:status":
        text = await _get_status_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:health":
        text = await _get_health_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:channels":
        text = await _get_channels_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:logs":
        text = await _get_logs_page_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:users":
        text = await _get_users_page_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:files":
        files_col = get_file_records_col()
        total = await files_col.count_documents({})
        files = await files_col.find(sort=("create_time", -1), limit=10)
        total_pages = max(1, (total + 10 - 1) // 10)
        text = f"📁 文件列表 (第1/{total_pages}页，共{total}个)\n\n"
        for f in files:
            status_icon = "✅" if f.get("status") == "active" else "🗑️"
            fc = f.get("file_code", "N/A")
            uploader = f.get("uploader_id", "?")
            text += f"{status_icon} {fc} (上传者:{uploader})\n"
        if total_pages > 1:
            text += f"\n使用 /files 2 查看下一页"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:add_chan":
        text = (
            "➕ 新增备份频道\n\n"
            "方式一：只输入频道ID，再选择由哪个机器人负责\n"
            "  /add_channel -100111222333\n\n"
            "方式二：直接指定机器人编号\n"
            "  /add_channel 2 -100111222333"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:remove_chan":
        text = (
            "➖ 删除备份频道\n\n"
            "请先通过 /channels 确认频道所属的机器人编号\n"
            "然后输入：\n"
            "  /remove_channel <机器人编号> <频道ID>\n\n"
            "示例：/remove_channel 1 -100111222333"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:add_bot":
        text = (
            "➕ 新增备份机器人\n\n"
            "格式：/add_backup_bot <编号> <BotToken> [频道ID ...]\n\n"
            "示例（仅配Token）：\n"
            "  /add_backup_bot 1 8012345678:AAbbcc...\n\n"
            "示例（同时配频道）：\n"
            "  /add_backup_bot 1 8012345678:AAbbcc... -100111 -100222\n\n"
            "⚠️ 配置后需重启对应备份机器人才生效"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:remove_bot":
        text = (
            "➖ 删除备份机器人\n\n"
            "格式：/remove_backup_bot <编号>\n\n"
            "示例：/remove_backup_bot 2\n\n"
            "⚠️ 该备份机器人将在下次重启后不再启动"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_status":
        text = await _get_relay_status_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:relay_config":
        text = (
            "⚙️ 配置中继账号\n\n"
            "中继需要一个真实的 Telegram 用户账号。\n"
            "按以下步骤获取 API 凭据：\n\n"
            "1. 访问 https://my.telegram.org\n"
            "2. 登录你的 Telegram 账号\n"
            "3. 进入 API Development Tools\n"
            "4. 创建应用，获取 api_id 和 api_hash\n\n"
            "配置命令：\n"
            "/relay_set_api <api_id> <api_hash> <手机号>\n\n"
            "示例：\n"
            "/relay_set_api 12345 abc123def456 +8613800138000\n\n"
            "⚠️ api_id 是数字，api_hash 是字符串\n"
            "⚠️ 手机号需包含国家区号，如 +86\n"
            "⚠️ 配置后解码机器人下次重启时生效"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "usage:relay_code":
        text = (
            "🔑 提交验证码\n\n"
            "当中继账号需要登录验证时，Telegram 会发送 6 位验证码\n"
            "到该账号已登录的 Telegram 客户端（非短信）。\n\n"
            "在此提交验证码即可完成登录：\n"
            "/relay_code <验证码>\n\n"
            "示例：/relay_code 123456\n\n"
            "解码机器人在后台轮询等待，提交后几秒内自动完成登录。"
        )
        await query.edit_message_text(text, reply_markup=back_kb)


@_auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _build_menu("main")
    await update.message.reply_text(text, reply_markup=markup)


@_auth_required
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _get_status_text())


@_auth_required
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _get_health_text())


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
    user = await _ensure_user(user_id)

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
    await update.message.reply_text(await _get_users_page_text(search, page))


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
    user = await _ensure_user(user_id)

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
    user = await _ensure_user(user_id)
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
    user = await _ensure_user(user_id)
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
    user = await _ensure_user(user_id)
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
    user = await _ensure_user(user_id)
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
    await update.message.reply_text(await _get_logs_page_text(page))


@_auth_required
async def channels_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _get_channels_text())


@_auth_required
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：\n"
            "  /add_channel <频道ID> — 添加频道后选择机器人\n"
            "  /add_channel <机器人编号> <频道ID> — 直接指定机器人"
        )
        return

    if len(args) == 1:
        try:
            channel_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字")
            return

        all_channels = await get_backup_channels(1) + await get_backup_channels(2) + await get_backup_channels(3)
        if channel_id in all_channels:
            await update.message.reply_text(f"⚠️ 频道 {channel_id} 已被其他机器人管理，请 /channels 查看")
            return

        keyboard = [
            [
                InlineKeyboardButton(f"🤖 backup_bot_1", callback_data=f"assign_chan:1:{channel_id}"),
                InlineKeyboardButton(f"🤖 backup_bot_2", callback_data=f"assign_chan:2:{channel_id}"),
            ],
            [
                InlineKeyboardButton(f"🤖 backup_bot_3", callback_data=f"assign_chan:3:{channel_id}"),
            ],
        ]
        await update.message.reply_text(
            f"📺 频道 {channel_id}\n请选择由哪个备份机器人负责：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    try:
        bot_num = int(args[0])
        channel_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 机器人编号和频道ID必须是数字")
        return
    if bot_num not in (1, 2, 3):
        await update.message.reply_text("❌ 机器人编号必须是 1、2 或 3")
        return

    channels = await get_backup_channels(bot_num)
    if channel_id in channels:
        await update.message.reply_text(f"⚠️ 频道 {channel_id} 已在 backup_bot_{bot_num} 中")
        return

    channels.append(channel_id)
    await set_backup_channels(bot_num, channels)

    tokens = await get_backup_bot_tokens()
    restart_hint = ""
    if str(bot_num) not in tokens:
        restart_hint = "\n\n⚠️ 该机器人尚未配置 Token，请使用 /add_backup_bot 配置后重启"

    await update.message.reply_text(
        f"✅ 频道 {channel_id} 已添加到 backup_bot_{bot_num}\n"
        f"当前 backup_bot_{bot_num} 频道: {channels}\n"
        f"备份机器人每 5 秒自动检测频道变化，新增频道将触发全量同步{restart_hint}"
    )


@_auth_required
async def assign_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("assign_chan:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        return

    bot_num = int(parts[1])
    channel_id = int(parts[2])

    channels = await get_backup_channels(bot_num)
    if channel_id in channels:
        await query.edit_message_text(f"⚠️ 频道 {channel_id} 已在 backup_bot_{bot_num} 中")
        return

    channels.append(channel_id)
    await set_backup_channels(bot_num, channels)

    tokens = await get_backup_bot_tokens()
    restart_hint = ""
    if str(bot_num) not in tokens:
        restart_hint = "\n\n⚠️ 该机器人尚未配置 Token，请使用 /add_backup_bot 配置后重启"

    await query.edit_message_text(
        f"✅ 频道 {channel_id} → backup_bot_{bot_num}\n"
        f"频道列表: {channels}\n"
        f"新增频道将触发全量同步{restart_hint}"
    )


@_auth_required
async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法：/remove_channel <机器人编号(1/2/3)> <频道ID>")
        return
    try:
        bot_num = int(args[0])
        channel_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 机器人编号和频道ID必须是数字")
        return
    if bot_num not in (1, 2, 3):
        await update.message.reply_text("❌ 机器人编号必须是 1、2 或 3")
        return

    channels = await get_backup_channels(bot_num)
    if channel_id not in channels:
        await update.message.reply_text(f"⚠️ 频道 {channel_id} 不在 backup_bot_{bot_num} 中")
        return

    channels.remove(channel_id)
    await set_backup_channels(bot_num, channels)

    await update.message.reply_text(
        f"✅ 频道 {channel_id} 已从 backup_bot_{bot_num} 中移除\n"
        f"当前 backup_bot_{bot_num} 频道: {channels or '(空)'}\n"
        f"备份机器人将在下个周期自动停止向此频道备份"
    )


@_auth_required
async def add_backup_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/add_backup_bot <编号(1/2/3)> <BotToken> [频道ID ...]\n"
            "示例：/add_backup_bot 1 12345:abc -100111 -100222"
        )
        return
    try:
        bot_num = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 编号必须是数字 1、2 或 3")
        return
    if bot_num not in (1, 2, 3):
        await update.message.reply_text("❌ 编号必须是 1、2 或 3")
        return

    token = args[1].strip()
    await set_backup_bot_token(bot_num, token)

    extra_info = ""
    if len(args) > 2:
        channel_ids = []
        for a in args[2:]:
            try:
                channel_ids.append(int(a))
            except ValueError:
                pass
        if channel_ids:
            await set_backup_channels(bot_num, channel_ids)
            extra_info = f"\n频道已配置: {channel_ids}"

    masked = token[:8] + "..." + token[-4:] if len(token) > 15 else "***"
    await update.message.reply_text(
        f"✅ 备份机器人 {bot_num} Token 已保存到数据库 ({masked}){extra_info}\n"
        f"⚠️ 需要重启 backup_bot_{bot_num} 进程才能生效\n"
        f"重启命令: python run_all.py backup{bot_num}"
    )


@_auth_required
async def remove_backup_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/remove_backup_bot <编号(1/2/3)>")
        return
    try:
        bot_num = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 编号必须是数字 1、2 或 3")
        return
    if bot_num not in (1, 2, 3):
        await update.message.reply_text("❌ 编号必须是 1、2 或 3")
        return

    await delete_backup_bot_token(bot_num)
    await update.message.reply_text(
        f"✅ 备份机器人 {bot_num} Token 已从数据库删除\n"
        f"⚠️ 该备份机器人将在下次重启后不再启动"
    )


@_auth_required
async def relay_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/relay_code <验证码>\n\n"
            "用于解码机器人登录 Telegram 用户账号时提交验证码。\n"
            "验证码（6位）会发送到该账号已登录的 Telegram 客户端。"
        )
        return
    code = args[0].strip()
    if not code.isdigit() or len(code) not in (5, 6):
        await update.message.reply_text("❌ 验证码格式不正确，应为 5-6 位数字")
        return

    await set_config("relay_auth_code", code)
    await update.message.reply_text(
        f"✅ 验证码 `{code}` 已提交\n"
        f"解码机器人将在几秒内自动获取并使用。"
    )


@_auth_required
async def relay_set_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "用法：/relay_set_api <api_id> <api_hash> <手机号>\n\n"
            "示例：/relay_set_api 12345 abc123def456 +8613800138000"
        )
        return
    try:
        api_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ api_id 必须是数字")
        return
    api_hash = args[1].strip()
    phone = args[2].strip()

    await set_relay_config(api_id, api_hash, phone)
    await update.message.reply_text(
        f"✅ 中继账号已配置\n"
        f"API_ID：{api_id}\n"
        f"手机号：{phone[:3]}****{phone[-2:] if len(phone) > 5 else ''}\n\n"
        f"⚠️ 配置已保存到数据库，解码机器人下次重启时生效。\n"
        f"⚠️ 请确保该账号未开启二步验证。"
    )


@_auth_required
async def relay_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await get_config("relay_auth_pending")
    if pending == "1":
        await update.message.reply_text(
            "⏳ 中继正在等待验证码\n\n"
            "Telegram 已发送 6 位验证码到中继账号的已登录客户端，\n"
            "请查看并提交：/relay_code <验证码>"
        )
    else:
        await update.message.reply_text(
            "✅ 中继当前不需要验证码\n\n"
            "如果解码机器人在等待验证码但此处显示不需要，\n"
            "可能是状态同步延迟，请稍后重试或查看状态面板。"
        )


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
    app.add_handler(CommandHandler("channels", channels_list))
    app.add_handler(CommandHandler("add_channel", add_channel))
    app.add_handler(CommandHandler("remove_channel", remove_channel))
    app.add_handler(CommandHandler("add_backup_bot", add_backup_bot))
    app.add_handler(CommandHandler("remove_backup_bot", remove_backup_bot))
    app.add_handler(CommandHandler("relay_code", relay_code))
    app.add_handler(CommandHandler("relay_set_api", relay_set_api))
    app.add_handler(CommandHandler("relay_pending", relay_pending))
    app.add_handler(CallbackQueryHandler(assign_channel_callback, pattern=r"^assign_chan:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu:|action:|usage:)"))

    app.run_polling()
