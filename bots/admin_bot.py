import asyncio
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
    get_relay_config, set_relay_config,
    get_all_code_bot_routes, set_code_bot_route, delete_code_bot_route,
    get_all_bot_decode_intervals, set_bot_decode_interval, delete_bot_decode_interval,
)
from database.models import make_user
from utils.monitor import metrics
from utils.storage_channel import get_active_storage_channel_id, set_active_storage_channel_id, invalidate_cache

TOKEN = settings.ADMIN_BOT_TOKEN
AUTHORIZED_USER_ID = settings.ADMIN_TELEGRAM_ID
MEMBERSHIP_LEVELS = {"free": "免费用户", "basic": "基础会员", "premium": "高级会员"}
LEVEL_ALIAS = {"1": "free", "2": "basic", "3": "premium", "free": "free", "basic": "basic", "premium": "premium"}


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
    if isinstance(dt, (datetime.datetime, float)):
        if isinstance(dt, float):
            if dt == 0:
                return "N/A"
            dt = datetime.datetime.fromtimestamp(dt, tz=datetime.UTC)
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
    today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    today_decodes = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})
    relay_pending = await get_config("relay_auth_pending")
    try:
        from services.relay_pool import relay_pool
        if not relay_pool._initialized:
            await relay_pool.init()
        pool_status = await relay_pool.get_pool_status()
        if pool_status:
            ready = sum(1 for p in pool_status if p["is_ready"])
            relay_status = f"✅ 账号池 {ready}/{len(pool_status)} 就绪"
        else:
            relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    except Exception:
        relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    active_channel = await get_active_storage_channel_id()
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
        f"\n📺 当前主存储频道：{active_channel}\n"
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
    active_channel = await get_active_storage_channel_id()
    msg = f"📺 主存储频道 & 备份配置\n\n"
    msg += f"📌 当前主存储频道：{active_channel}\n"
    msg += f"  通过 /promote_channel <频道ID> 可将备份频道切换为主频道\n\n"
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
    from services.relay_pool import relay_pool
    pending = await get_config("relay_auth_pending")

    if not relay_pool._initialized:
        try:
            await relay_pool.init()
        except Exception:
            pass

    msg = "🔐 中继账号池状态\n\n"
    pool_status = await relay_pool.get_pool_status()
    if not pool_status:
        msg += "⚠️ 无中继账号\n"
        msg += "请使用下方按钮配置中继账号\n"
    else:
        msg += f"账号池: {len(pool_status)} 个账号\n\n"
        for i, ps in enumerate(pool_status, 1):
            ready = "✅" if ps["is_ready"] else "❌"
            busy = "🔴" if ps["is_busy"] else "⚪"
            phone = ps["phone"]
            masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
            msg += f"{i}. {ready}{busy} {masked}\n"
            msg += f"   今日请求: {ps['today_requests']}, 累计: {ps['total_requests']}, 平均: {ps['avg_wait_ms']:.0f}ms\n"
        ready_count = sum(1 for p in pool_status if p["is_ready"])
        msg += f"\n就绪: {ready_count}/{len(pool_status)}"

    if pending == "1":
        msg += "\n⚠️ 正在等待验证码，请通过 /relay_code 提交"

    return msg


_CONFIG_SETTINGS_MAP = {
    "storage_channel_id": "MAIN_STORAGE_CHANNEL_ID",
    "decoder_chat_id": "DECODER_BOT_CHAT_ID",
    "file_code_prefix": "FILE_CODE_PREFIX",
    "force_join_channel_id": "FORCE_JOIN_CHANNEL_ID",
    "force_join_link": "FORCE_JOIN_CHANNEL_LINK",
    "upload_bot_username": "UPLOAD_BOT_USERNAME",
    "decoder_bot_username": "DECODER_BOT_USERNAME",
    "sender_bot_username": "SENDER_BOT_USERNAME",
    "quota_default_free": "FREE_DAILY_QUOTA",
    "quota_external_free": "FREE_EXTERNAL_DAILY_QUOTA",
    "quota_default_basic": "BASIC_DAILY_QUOTA",
    "quota_external_basic": "BASIC_EXTERNAL_DAILY_QUOTA",
    "quota_default_premium": "PREMIUM_DAILY_QUOTA",
    "quota_external_premium": "PREMIUM_EXTERNAL_DAILY_QUOTA",
    "r2_account_id": "R2_ACCOUNT_ID",
    "r2_access_key": "R2_ACCESS_KEY_ID",
    "r2_secret_key": "R2_SECRET_ACCESS_KEY",
    "r2_bucket": "R2_BUCKET_NAME",
    "r2_endpoint": "R2_ENDPOINT",
    "db_backup_interval": "DB_BACKUP_INTERVAL_MINUTES",
    "db_backup_enabled": "DB_BACKUP_ENABLED",
}


def _config_fallback(key: str) -> str:
    attr_name = _CONFIG_SETTINGS_MAP.get(key)
    if attr_name:
        val = getattr(settings, attr_name, None)
        if val is not None:
            str_val = str(val)
            if str_val and str_val not in ("0", "-1000000000000"):
                return str_val
    return settings.get_config_default(key)


async def _get_configs_text() -> str:
    cfg_keys = [
        ("storage_channel_id", "📺 主存储频道"),
        ("decoder_chat_id", "🤖 解码机器人对话"),
        ("file_code_prefix", "📝 文件码前缀"),
        ("force_join_channel_id", "🔒 强制加群频道"),
        ("force_join_link", "🔗 加群链接"),
        ("upload_bot_username", "📤 上传机器人"),
        ("decoder_bot_username", "🔓 解码机器人"),
        ("sender_bot_username", "📨 发送机器人"),
    ]

    quota_keys = [
        ("quota_default_free", "🆓 免费用户日配额"),
        ("quota_external_free", "🆓 免费外部码配额"),
        ("quota_default_basic", "🥇 基础会员日配额"),
        ("quota_external_basic", "🥇 基础外部码配额"),
        ("quota_default_premium", "👑 高级会员日配额"),
        ("quota_external_premium", "👑 高级外部码配额"),
    ]

    r2_keys = [
        ("r2_account_id", "☁️ R2 账号ID"),
        ("r2_access_key", "🔑 R2 Access Key"),
        ("r2_secret_key", "🔒 R2 Secret Key"),
        ("r2_bucket", "🪣 R2 桶名"),
        ("r2_endpoint", "🔗 R2 Endpoint"),
    ]

    backup_keys = [
        ("db_backup_interval", "💾 DB备份间隔(分钟)"),
        ("db_backup_enabled", "💾 DB备份"),
    ]

    msg = "⚙️ 系统配置\n\n"

    msg += "📌 基础配置\n"
    for key, label in cfg_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        display = val if val else "❌ 未配置"
        msg += f"  {label}：{display}\n"

    msg += "\n🎫 默认配额\n"
    for key, label in quota_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        try:
            display = _quota_display(int(val)) if val else "未配置"
        except (ValueError, TypeError):
            display = str(val) if val else "未配置"
        msg += f"  {label}：{display}\n"

    r2_keys_to_check = ["r2_account_id", "r2_access_key", "r2_secret_key"]
    r2_vals = await asyncio.gather(*(get_config(k) for k in r2_keys_to_check))
    r2_configured = any(v for v in r2_vals if v)
    if not r2_configured:
        r2_check = lambda k: _config_fallback(k) != settings.get_config_default(k)
        r2_configured = any(r2_check(k) for k in r2_keys_to_check)
    msg += f"\n☁️ R2 备份：{'✅ 已配置' if r2_configured else '❌ 未配置'}\n"

    for key, label in backup_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        display = val if val else "未配置"
        if key == "db_backup_enabled":
            display = "✅ 开启" if display.lower() in ("true", "1", "on") else "❌ 关闭"
        msg += f"  {label}：{display}\n"

    msg += "\n使用 /set_* 命令修改配置，或点击菜单按钮操作。"
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
            [InlineKeyboardButton("🔐 用户中继", callback_data="menu:relay"),
             InlineKeyboardButton("⚙️ 系统配置", callback_data="menu:config")],
            [InlineKeyboardButton("🗺️ 文件码路由", callback_data="menu:code_route"),
             InlineKeyboardButton("⏱️ Bot限流", callback_data="menu:bot_limit")],
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
        text = "👤 用户管理 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📋 用户列表", callback_data="action:users"),
             InlineKeyboardButton("👤 查询用户", callback_data="interactive:user_detail")],
            [InlineKeyboardButton("🏅 设置等级", callback_data="interactive:set_level"),
             InlineKeyboardButton("🔒 封禁用户", callback_data="interactive:ban")],
            [InlineKeyboardButton("🔓 解封用户", callback_data="interactive:unban"),
             InlineKeyboardButton("📤 解码配额", callback_data="interactive:set_quota")],
            [InlineKeyboardButton("🌐 外部码配额", callback_data="interactive:set_external_quota")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "file":
        text = "📁 文件管理 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📂 文件列表", callback_data="action:files"),
             InlineKeyboardButton("🔍 查询文件", callback_data="interactive:file_detail")],
            [InlineKeyboardButton("🗑️ 删除文件", callback_data="interactive:delete_file")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "backup_chan":
        text = "📺 主存储频道 & 备份管理 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📺 查看配置", callback_data="action:channels")],
            [InlineKeyboardButton("➕ 新增频道", callback_data="interactive:add_channel"),
             InlineKeyboardButton("➖ 删除频道", callback_data="interactive:remove_channel")],
            [InlineKeyboardButton("⬆️ 提升主频道", callback_data="interactive:promote_channel")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "backup_bot":
        text = (
            "🤖 备份机器人管理 — 点击按钮操作\n\n"
            "⚠️ 修改 Token 后需重启对应备份机器人才生效"
        )
        kb = [
            [InlineKeyboardButton("📺 查看配置", callback_data="action:channels"),
             InlineKeyboardButton("➕ 新增机器人", callback_data="interactive:add_backup_bot")],
            [InlineKeyboardButton("➖ 删除机器人", callback_data="interactive:remove_backup_bot"),
             InlineKeyboardButton("🔄 重置备份", callback_data="interactive:backup_reset")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "relay":
        text = "🔐 用户中继管理 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📊 查看状态", callback_data="action:relay_status"),
             InlineKeyboardButton("⚙️ 配置账号", callback_data="interactive:relay_set_api")],
            [InlineKeyboardButton("🔑 提交验证码", callback_data="interactive:relay_code"),
             InlineKeyboardButton("📋 查看待处理", callback_data="action:relay_pending")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "config":
        text = "⚙️ 系统配置 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📋 查看全部配置", callback_data="action:settings")],
            [InlineKeyboardButton("📺 主存储频道", callback_data="interactive:set_storage_channel"),
             InlineKeyboardButton("🤖 解码对话", callback_data="interactive:set_decoder_chat")],
            [InlineKeyboardButton("📝 文件码前缀", callback_data="interactive:set_file_prefix"),
             InlineKeyboardButton("🔒 强制加群", callback_data="interactive:set_force_join")],
            [InlineKeyboardButton("👤 机器人用户名", callback_data="interactive:set_username"),
             InlineKeyboardButton("🎫 默认配额", callback_data="interactive:set_quota_default")],
            [InlineKeyboardButton("☁️ R2备份配置", callback_data="interactive:set_r2"),
             InlineKeyboardButton("💾 DB自动备份", callback_data="interactive:set_db_backup")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "code_route":
        text = (
            "🗺️ 文件码前缀路由\n\n"
            "当第三方机器人迁移后，可通过此功能将特定前缀的文件码\n"
            "路由到指定的新机器人解码。\n\n"
            "点击下方按钮开始操作："
        )
        kb = [
            [InlineKeyboardButton("➕ 新增路由", callback_data="interactive:add_code_route"),
             InlineKeyboardButton("➖ 删除路由", callback_data="interactive:remove_code_route")],
            [InlineKeyboardButton("📋 查看路由表", callback_data="action:code_routes")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "bot_limit":
        text = (
            "⏱️ Bot 解码间隔限流\n\n"
            "某些机器人限制每个文件码之间的解码间隔时间。\n"
            "设置后，系统会自动等待满足间隔再发送下一个请求。\n\n"
            "点击下方按钮开始操作："
        )
        kb = [
            [InlineKeyboardButton("➕ 新增限流", callback_data="interactive:set_bot_interval"),
             InlineKeyboardButton("➖ 删除限流", callback_data="interactive:remove_bot_interval")],
            [InlineKeyboardButton("📋 查看限流配置", callback_data="action:bot_intervals")],
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

    elif data == "usage:reset_backup":
        text = (
            "🔄 重置备份状态\n\n"
            "当手动清空了备份频道后，需要重置备份机器人的同步状态\n"
            "以触发全量重新备份。\n\n"
            "命令：/backup_reset <backup_bot_N>\n\n"
            "示例：/backup_reset backup_bot_1\n\n"
            "该命令会：\n"
            "1. 清零备份机器人的游标状态\n"
            "2. 清除所有文件记录中该频道的旧备份信息\n"
            "3. 备份机器人重启后执行全量备份"
        )
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_status":
        text = await _get_relay_status_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_pending":
        from database import get_config as _get_cfg
        pending = await _get_cfg("relay_auth_pending")
        if pending == "1":
            text = "⏳ 中继正在等待验证码\n\nTelegram 已发送 6 位验证码到中继账号的已登录客户端，请查看并使用 /relay_code 提交。"
        else:
            text = "✅ 中继当前不需要验证码。"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:settings":
        text = await _get_configs_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:code_routes":
        routes = await get_all_code_bot_routes()
        if not routes:
            text = "📭 尚未配置文件码前缀路由。"
        else:
            text = "🗺️ 文件码前缀路由表\n\n"
            for prefix in sorted(routes.keys()):
                text += f"  • `{prefix}` → @{routes[prefix]}\n"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:bot_intervals":
        intervals = await get_all_bot_decode_intervals()
        if not intervals:
            text = "📭 尚未配置 Bot 解码间隔。"
        else:
            text = "⏱️ Bot 解码间隔配置\n\n"
            for bot in sorted(intervals.keys()):
                text += f"  • @{bot} → {intervals[bot]} 秒\n"
        await query.edit_message_text(text, reply_markup=back_kb)

    # ─── 交互式操作入口 ──────────────────────────────────────────
    elif data.startswith("interactive:"):
        context.user_data.pop("conv_state", None)
        context.user_data.pop("conv_data", None)
        action = data[len("interactive:"):]

        prompts = {
            # 文件码路由
            "add_code_route": (
                "add_code_route:prefix",
                "🗺️ 新增文件码前缀路由\n\n请输入文件码前缀（例如：qqfile）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "remove_code_route": (
                "remove_code_route:prefix",
                "🗺️ 删除文件码前缀路由\n\n请输入要删除的路由前缀（例如：qqfile）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # Bot限流
            "set_bot_interval": (
                "set_bot_interval:bot",
                "⏱️ 新增 Bot 解码间隔限流\n\n请输入目标机器人用户名（不需要 @，例如：qqfile_bot）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "remove_bot_interval": (
                "remove_bot_interval:bot",
                "⏱️ 删除 Bot 解码间隔限流\n\n请输入要删除限流的机器人用户名（例如：qqfile_bot）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 用户管理
            "user_detail": (
                "user_detail:id",
                "👤 查询用户\n\n请输入用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_level": (
                "set_level:user_id",
                "🏅 设置会员等级\n\n请输入用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "ban": (
                "ban:user_id",
                "🔒 封禁用户\n\n请输入要封禁的用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "unban": (
                "unban:user_id",
                "🔓 解封用户\n\n请输入要解封的用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_quota": (
                "set_quota:user_id",
                "📤 设置解码配额\n\n请输入用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_external_quota": (
                "set_external_quota:user_id",
                "🌐 设置外部码配额\n\n请输入用户ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 文件管理
            "file_detail": (
                "file_detail:code",
                "🔍 查询文件\n\n请输入文件码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "delete_file": (
                "delete_file:code",
                "🗑️ 删除文件\n\n请输入要删除的文件码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 频道管理
            "add_channel": (
                "add_channel:id",
                "📺 新增备份频道\n\n请输入频道ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "remove_channel": (
                "remove_channel:bot_num",
                "➖ 删除备份频道\n\n请输入频道所属的机器人编号（1/2/3）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "promote_channel": (
                "promote_channel:id",
                "⬆️ 提升主频道\n\n请输入要提升为主频道的频道ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 备份机器人
            "add_backup_bot": (
                "add_backup_bot:bot_num",
                "🤖 新增备份机器人\n\n请输入机器人编号（1/2/3）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "remove_backup_bot": (
                "remove_backup_bot:bot_num",
                "➖ 删除备份机器人\n\n请输入要删除的机器人编号（1/2/3）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "backup_reset": (
                "backup_reset:bot_name",
                "🔄 重置备份状态\n\n请输入备份机器人名称（backup_bot_1 / backup_bot_2 / backup_bot_3）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 中继
            "relay_code": (
                "relay_code:code",
                "🔑 提交验证码\n\n请输入 Telegram 发送的 6 位验证码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "relay_set_api": (
                "relay_set_api:api_id",
                "⚙️ 配置中继账号\n\n第一步：请输入 API_ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 系统配置
            "set_storage_channel": (
                "set_storage_channel:id",
                "📺 设置主存储频道\n\n请输入频道ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_decoder_chat": (
                "set_decoder_chat:id",
                "🤖 设置解码机器人对话\n\n请输入 ChatID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_file_prefix": (
                "set_file_prefix:prefix",
                "📝 设置文件码前缀\n\n请输入新的文件码前缀（例如：tgwenjian）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_force_join": (
                "set_force_join:channel_id",
                "🔒 设置强制加群频道\n\n请输入频道ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_username": (
                "set_username:role",
                "👤 设置机器人用户名\n\n请输入角色（upload / decoder / sender）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_quota_default": (
                "set_quota_default:level",
                "🎫 设置默认配额\n\n请输入会员等级（1=免费 / 2=基础 / 3=高级）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_r2": (
                "set_r2:account_id",
                "☁️ 配置 R2 备份\n\n第一步：请输入 R2 账号ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_db_backup": (
                "set_db_backup:interval",
                "💾 配置 DB 自动备份\n\n请输入备份间隔（分钟）：\n\n❌ 如需取消请点击下方按钮。"
            ),
        }

        entry = prompts.get(action)
        if entry:
            state, prompt = entry
            await _conv_start(update, context, state, prompt)
        else:
            await query.edit_message_text(f"❌ 未知操作：{action}", reply_markup=back_kb)

    elif data == "conv:cancel":
        _conv_end(context)
        await query.edit_message_text(
            "❌ 操作已取消。",
            reply_markup=back_kb,
        )

    elif data.startswith("conv_sel_bot:"):
        parts = data.split(":")
        if len(parts) == 3:
            _, channel_id_str, bot_num_str = parts
            try:
                channel_id = int(channel_id_str)
                bot_num = int(bot_num_str)
            except ValueError:
                await query.edit_message_text("❌ 参数错误", reply_markup=back_kb)
                return
        else:
            await query.edit_message_text("❌ 参数错误", reply_markup=back_kb)
            return

        channels = await get_backup_channels(bot_num)
        if channel_id in channels:
            await query.edit_message_text(
                f"⚠️ 频道 {channel_id} 已在 Bot {bot_num} 的备份列表中。",
                reply_markup=back_kb,
            )
            _conv_end(context)
            return

        channels.append(channel_id)
        await set_backup_channels(bot_num, channels)

        all_chan = await get_all_backup_channels()
        if channel_id not in all_chan:
            from config import settings as _settings
            _settings.ALL_BACKUP_CHANNELS = list(set(all_chan + [channel_id]))

        _conv_end(context)
        await query.edit_message_text(
            f"✅ 已添加频道 {channel_id} 到 Bot {bot_num} 的备份列表。",
            reply_markup=back_kb,
        )


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
        await update.message.reply_text("用法：/set_level <用户ID> <1|2|3>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return
    level = LEVEL_ALIAS.get(args[1].lower())
    if not level:
        await update.message.reply_text("❌ 等级：1=免费 2=基础 3=高级")

    users_col = get_users_col()
    user = await _ensure_user(user_id)

    update_doc = {
        "$set": {
            "membership_level": level,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
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
        {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}},
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
        {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}},
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
        {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}},
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
        {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}},
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
        f"⚠️ 请确保该账号未开启二步验证。\n"
        f"🔐 建议在配置完成后立即删除本聊天记录中的密钥信息。"
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


@_auth_required
async def relay_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有中继账号及使用统计"""
    from services.relay_pool import relay_pool
    if not relay_pool._initialized:
        try:
            await relay_pool.init()
        except Exception:
            pass
    pool_status = await relay_pool.get_pool_status()
    if not pool_status:
        await update.message.reply_text(
            "⚠️ 中继账号池为空\n"
            "请使用 /relay_add 添加中继账号，或通过管理面板配置。"
        )
        return
    msg = f"🔐 中继账号池 ({len(pool_status)} 个账号)\n\n"
    for i, ps in enumerate(pool_status, 1):
        ready = "✅" if ps["is_ready"] else "❌"
        busy = "🔴忙" if ps["is_busy"] else "⚪空闲"
        phone = ps["phone"]
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        msg += f"{i}. {ready}{busy} {masked}\n"
        msg += f"   今日: {ps['today_requests']} | 累计: {ps['total_requests']} | 平均: {ps['avg_wait_ms']:.0f}ms\n\n"
    await update.message.reply_text(msg)


@_auth_required
async def relay_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加中继账号（命令行方式）"""
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "用法：/relay_add <api_id> <api_hash> <手机号>\n\n"
            "示例：/relay_add 12345 abc123def456 +8613800138000\n\n"
            "添加后需要提交验证码完成登录，或直接在管理面板中配置。"
        )
        return
    try:
        api_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ api_id 必须是数字")
        return
    api_hash = args[1].strip()
    phone = args[2].strip()

    from services.relay_pool import relay_pool
    try:
        instance = await relay_pool.add_account(api_id, api_hash, phone)
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await update.message.reply_text(
            f"✅ 中继账号已添加到池中\n"
            f"  API_ID: {api_id}\n"
            f"  API_HASH: {api_hash[:8]}...\n"
            f"  手机号: {masked}\n\n"
            f"解码机器人将自动检测新账号并连接。\n"
            f"如需要登录验证码，请使用 /relay_code 提交。"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 添加失败: {e}")


@_auth_required
async def relay_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除中继账号"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/relay_remove <手机号>\n\n"
            "示例：/relay_remove +8613800138000"
        )
        return
    phone = args[0].strip()

    from services.relay_pool import relay_pool
    removed = await relay_pool.remove_account(phone)
    if removed:
        await update.message.reply_text(f"✅ 已移除中继账号: {phone}")
    else:
        await update.message.reply_text(f"❌ 未找到该手机号的中继账号: {phone}")


@_auth_required
async def relay_reset_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重置使用统计"""
    from database.relay_db import get_relay_db
    db = await get_relay_db()
    await db.reset_usage()
    await update.message.reply_text("✅ 中继账号使用统计已重置")


@_auth_required
async def backup_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/backup_reset <backup_bot_1|backup_bot_2|backup_bot_3>\n\n"
            "重置备份机器人的同步状态，下次重启后将执行全量备份。\n"
            "适用于：手动清空了备份频道后，强制重新备份所有文件。"
        )
        return

    bot_name = args[0].strip()
    if bot_name not in ("backup_bot_1", "backup_bot_2", "backup_bot_3"):
        await update.message.reply_text(
            "❌ 无效的备份机器人名称，请使用 backup_bot_1、backup_bot_2 或 backup_bot_3"
        )
        return

    files_col = get_file_records_col()
    await set_config(f"backup_{bot_name}_full_sync_done", "0")
    await set_config(f"backup_{bot_name}_last_synced_id", "0")
    await set_config(f"backup_{bot_name}_quick_cursor", "0")

    channels = await get_backup_channels(int(bot_name[-1]))
    cleared_count = 0
    if channels:
        records = await files_col.find(
            {"backup_channel_msg_ids": {"$ne": "", "$ne": None}},
            limit=500,
        )
        for record in records:
            backups = record.get("backup_channel_msg_ids") or []
            if isinstance(backups, str):
                try:
                    import json
                    backups = json.loads(backups)
                except Exception:
                    continue
            if not isinstance(backups, list):
                continue
            new_backups = [
                b for b in backups
                if isinstance(b, dict) and b.get("channel_id") not in channels
            ]
            if len(new_backups) != len(backups):
                await files_col.update_one(
                    {"file_code": record["file_code"]},
                    {"$set": {"backup_channel_msg_ids": new_backups}},
                )
                cleared_count += 1

    await update.message.reply_text(
        f"✅ 已重置 {bot_name} 的备份状态\n"
        f"• 同步状态已清零\n"
        f"• 清理了 {cleared_count} 条文件记录中的旧备份信息\n\n"
        f"⚠️ 备份机器人重启后将执行全量备份到频道 {channels}"
    )


@_auth_required
async def promote_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/promote_channel <频道ID>\n\n"
            "将指定的备份频道提升为新的主存储频道。\n"
            "所有机器人将立即切换到新频道，无需重启。\n"
            "原主频道将不再接收新文件上传，但已有文件的备份记录仍有效。"
        )
        return
    try:
        new_channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return

    all_backups = list(settings.ALL_BACKUP_CHANNELS)
    if not all_backups:
        await update.message.reply_text("❌ 没有任何备份频道可提升")
        return

    if new_channel_id not in all_backups:
        await update.message.reply_text(
            f"❌ 频道 {new_channel_id} 不在备份频道列表中。\n"
            f"当前备份频道: {all_backups}\n"
            f"请先用 /channels 确认备份频道配置。"
        )
        return

    old_channel = await get_active_storage_channel_id()
    if new_channel_id == old_channel:
        await update.message.reply_text(f"⚠️ 频道 {new_channel_id} 已经是当前主频道")
        return

    success = await set_active_storage_channel_id(new_channel_id)
    if not success:
        await update.message.reply_text("❌ 切换主存储频道失败，请检查数据库连接。")
        return

    invalidate_cache()
    await update.message.reply_text(
        f"✅ 主存储频道已切换！\n\n"
        f"旧主频道：{old_channel}\n"
        f"新主频道：{new_channel_id}\n\n"
        f"📤 upload_bot 将上传新文件到新频道\n"
        f"🤖 backup_bot 将实时监控新频道的新消息\n"
        f"📨 sender_bot 发送时优先使用新频道\n\n"
        f"⚠️ 旧频道中已存在的文件仍可通过 sender_bot 的备用频道机制获取\n"
        f"⚠️ 建议将原主频道添加为备份频道"
    )


# ─── 系统配置管理 ────────────────────────────────────────────────


@_auth_required
async def settings_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _get_configs_text()
    await update.message.reply_text(text)


@_auth_required
async def set_storage_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/set_storage_channel <频道ID>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return
    await set_config("storage_channel_id", str(channel_id))
    await update.message.reply_text(f"✅ 主存储频道已设为 {channel_id}\n⚠️ 需重启所有机器人后生效")


@_auth_required
async def set_decoder_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/set_decoder_chat <ChatID>")
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ChatID必须是数字")
        return
    await set_config("decoder_chat_id", str(chat_id))
    await update.message.reply_text(f"✅ 解码机器人对话已设为 {chat_id}")


@_auth_required
async def set_file_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/set_file_prefix <前缀>\n当前：/settings 查看")
        return
    prefix = args[0].strip()
    await set_config("file_code_prefix", prefix)
    await update.message.reply_text(f"✅ 文件码前缀已设为 {prefix}\n⚠️ 需重启 upload_bot 后生效")


@_auth_required
async def set_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/set_force_join <频道ID> [加群链接]")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return
    await set_config("force_join_channel_id", str(channel_id))
    link = args[1] if len(args) > 1 else ""
    if link:
        await set_config("force_join_link", link)
    await update.message.reply_text(f"✅ 强制加群频道已设为 {channel_id}\n" + (f"🔗 链接：{link}" if link else ""))


@_auth_required
async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/set_username <upload|decoder|sender> <@用户名>")
        return
    bot_role = args[0].lower()
    username = args[1].lstrip("@")
    key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
    key = key_map.get(bot_role)
    if not key:
        await update.message.reply_text("❌ 角色必须是 upload、decoder 或 sender")
        return
    await set_config(key, username)
    await update.message.reply_text(f"✅ {bot_role} 机器人用户名已设为 @{username}")


@_auth_required
async def set_quota_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/set_quota_default <1|2|3> <日配额> [外部码日配额]")
        return
    level = LEVEL_ALIAS.get(args[0].lower())
    if not level:
        await update.message.reply_text("❌ 等级：1=免费 2=基础 3=高级")
        return
    try:
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 配额必须是数字（-1 表示不限）")
        return
    await set_config(f"quota_default_{level}", str(quota))
    msg = f"✅ {level} 日配额已设为 {_quota_display(quota)}"

    if len(args) >= 3:
        try:
            ext_quota = int(args[2])
        except ValueError:
            ext_quota = quota
        await set_config(f"quota_external_{level}", str(ext_quota))
        msg += f"，外部码配额 {_quota_display(ext_quota)}"

    msg += "\n⚠️ 已有用户的配额不受影响"
    await update.message.reply_text(msg)


@_auth_required
async def set_r2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("用法：/set_r2 <账号ID> <AccessKey> <SecretKey> [桶名]")
        return
    await set_config("r2_account_id", args[0])
    await set_config("r2_access_key", args[1])
    await set_config("r2_secret_key", args[2])
    if len(args) >= 4:
        await set_config("r2_bucket", args[3])
    await update.message.reply_text(
        "✅ R2 配置已保存\n"
        "⚠️ 需重启服务后生效\n"
        "🔐 建议在配置完成后立即删除本聊天记录中的密钥信息。"
    )


@_auth_required
async def set_db_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/set_db_backup <间隔分钟> <开/关>")
        return
    try:
        interval = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 间隔必须是数字（分钟）")
        return
    enabled = args[1].lower() in ("开", "1", "true", "yes", "on")
    await set_config("db_backup_interval", str(interval))
    await set_config("db_backup_enabled", "true" if enabled else "false")
    await update.message.reply_text(
        f"✅ 数据库备份：间隔 {interval} 分钟，状态：{'开启' if enabled else '关闭'}"
    )


# ─── 工厂重置 ────────────────────────────────────────────────────


_FACTORY_RESET_TABLES = [
    "file_records", "decode_logs", "pending_uploads",
    "send_queue", "users", "backup_config",
]


@_auth_required
async def factory_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        await update.message.reply_text(
            "⚠️️ ️ 危险的工厂重置！\n\n"
            "此操作将清空以下所有数据：\n"
            "• 📦 file_records（文件记录）\n"
            "• 📋 decode_logs（解码日志）\n"
            "• 📤 pending_uploads（上传队列）\n"
            "• 📨 send_queue（发送队列）\n"
            "• 👤 users（用户数据）\n"
            "• ⚙️ backup_config（系统配置与备份配置）\n\n"
            "频道中的消息不会被删除，但备份状态会重置，全量备份将被重新触发。\n\n"
            "🔴 如果您确认，请发送：\n"
            "/factory_reset confirm\n\n"
            "🔴 最终确认请发送：\n"
            "/factory_reset confirm I_UNDERSTAND"
        )
        return

    if args[0] != "confirm":
        await update.message.reply_text("❌ 请先使用 /factory_reset 查看说明")
        return

    if len(args) < 2 or args[1] != "I_UNDERSTAND":
        await update.message.reply_text(
            "⚠️ 二次确认\n\n"
            "发送以下命令执行最终重置：\n"
            "/factory_reset confirm I_UNDERSTAND\n\n"
            "此操作不可撤销！所有用户数据、文件码、配置将被永久清空。\n"
            "频道消息需要手动删除。"
        )
        return

    msg = await update.message.reply_text("🔄 正在执行工厂重置...")

    from database.session import CockroachDBClient

    client = CockroachDBClient()
    client.configure(settings.COCKROACHDB_URL)
    await client.connect()

    cleared = []
    for table in _FACTORY_RESET_TABLES:
        try:
            sql = f"DELETE FROM {table}"
            await client.execute(sql)
            cleared.append(table)
        except Exception as e:
            logger.warning(f"[factory_reset] 清空 {table} 失败: {e}")

    for prefix in ("backup_backup_bot_1_cursor", "backup_backup_bot_2_cursor", "backup_backup_bot_3_cursor"):
        try:
            await client.execute("DELETE FROM app_config WHERE config_key = $1", [prefix])
        except Exception:
            pass

    await msg.edit_text(
        "✅ 工厂重置完成！\n\n"
        f"已清空 {len(cleared)} 张表：{', '.join(cleared)}\n"
        "已重置备份游标\n\n"
        "⚠️ 存储频道的消息不会被自动删除。\n"
        "如需清空存储频道，请手动执行：\n"
        "  /purge_channel <频道ID>\n\n"
        "🔄 请重启所有机器人以使配置生效。"
    )


@_auth_required
async def purge_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/purge_channel <频道ID>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return

    await update.message.reply_text(
        f"⚠️ Telegram Bot API 不支持批量删除频道消息。\n\n"
        f"请手动处理频道 {channel_id}：\n"
        f"1. 打开频道管理界面\n"
        f"2. 删除所有消息\n"
        f"3. 或直接创建一个新的测试频道\n\n"
        f"完成后使用 /set_storage_channel <新频道ID> 重新配置。"
    )


# ─── 文件码前缀路由管理 ──────────────────────────────────────────


@_auth_required
async def add_code_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/add_code_route <前缀> <机器人用户名>\n\n"
            "设置文件码前缀对应的解码机器人。\n"
            "当文件码以指定前缀开头时，中继将路由到该机器人解码。\n\n"
            "示例：\n"
            "/add_code_route qqfile qqfile_bot\n"
            "/add_code_route tgwenjian mydecoder_bot"
        )
        return
    prefix = args[0].strip().lower()
    bot_username = args[1].strip().lower().lstrip("@")
    await set_code_bot_route(prefix, bot_username)
    await update.message.reply_text(
        f"✅ 文件码路由已设置\n"
        f"  前缀：{prefix}\n"
        f"  目标机器人：@{bot_username}\n\n"
        f"以 `{prefix}` 开头的文件码将通过 @{bot_username} 解码。"
    )


@_auth_required
async def remove_code_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/remove_code_route <前缀>\n\n"
            "删除文件码前缀路由配置。\n"
            "删除后该前缀的文件码将恢复原有解码规则。\n\n"
            "示例：/remove_code_route qqfile"
        )
        return
    prefix = args[0].strip().lower()
    await delete_code_bot_route(prefix)
    await update.message.reply_text(f"✅ 文件码前缀路由已删除：{prefix}")


@_auth_required
async def list_code_routes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = await get_all_code_bot_routes()
    if not routes:
        await update.message.reply_text("📭 尚未配置任何文件码前缀路由。\n\n使用 /add_code_route 添加。")
        return
    msg = "🗺️ 文件码前缀路由表\n\n"
    for prefix in sorted(routes.keys()):
        msg += f"  • `{prefix}` → @{routes[prefix]}\n"
    msg += "\n使用 /remove_code_route <前缀> 删除。"
    await update.message.reply_text(msg)


# ─── Bot 解码间隔限流管理 ────────────────────────────────────────


@_auth_required
async def set_bot_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/set_bot_interval <机器人用户名> <间隔秒数>\n\n"
            "设置向指定机器人发送解码请求的最小间隔时间。\n"
            "某些机器人限制每个文件码之间的解码间隔，此设置可自动等待。\n\n"
            "示例：\n"
            "/set_bot_interval qqfile_bot 3\n"
            "/set_bot_interval tgfile_bot 5\n\n"
            "设为 0 表示不限间隔。"
        )
        return
    bot_username = args[0].strip().lower().lstrip("@")
    try:
        interval = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 间隔秒数必须是数字")
        return
    if interval < 0:
        await update.message.reply_text("❌ 间隔秒数不能为负数")
        return
    await set_bot_decode_interval(bot_username, interval)
    if interval == 0:
        await update.message.reply_text(f"✅ 已取消 @{bot_username} 的解码间隔限制")
    else:
        await update.message.reply_text(f"✅ @{bot_username} 的解码间隔已设为 {interval} 秒")


@_auth_required
async def remove_bot_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：/remove_bot_interval <机器人用户名>\n\n"
            "删除指定机器人的解码间隔配置。\n\n"
            "示例：/remove_bot_interval qqfile_bot"
        )
        return
    bot_username = args[0].strip().lower().lstrip("@")
    await delete_bot_decode_interval(bot_username)
    await update.message.reply_text(f"✅ 已删除 @{bot_username} 的解码间隔配置")


@_auth_required
async def list_bot_intervals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    intervals = await get_all_bot_decode_intervals()
    if not intervals:
        await update.message.reply_text("📭 尚未配置任何 bot 解码间隔。\n\n使用 /set_bot_interval 添加。")
        return
    msg = "⏱️ Bot 解码间隔配置\n\n"
    for bot in sorted(intervals.keys()):
        msg += f"  • @{bot} → {intervals[bot]} 秒\n"
    msg += "\n使用 /remove_bot_interval <bot> 删除。"
    await update.message.reply_text(msg)


@_auth_required
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("conv_state"):
        _conv_end(context)
        await update.message.reply_text("❌ 操作已取消。")
    else:
        await update.message.reply_text("当前没有正在进行的操作。")


# ─── 交互式对话系统 ──────────────────────────────────────────────

_CONV_CANCEL_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ 取消操作", callback_data="conv:cancel")],
])


async def _conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    query = update.callback_query
    await query.edit_message_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)


async def _conv_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    await update.message.reply_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)


def _conv_end(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("conv_state", None)
    context.user_data.pop("conv_data", None)


@_auth_required
async def handle_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("conv_state")
    if not state:
        return

    text = update.message.text.strip()
    data = context.user_data.get("conv_data", {})

    # ─── Helper: execute and end ─────────────────────────────────
    async def _end(msg: str):
        _conv_end(context)
        await update.message.reply_text(msg)

    async def _ask(next_state: str, prompt: str, extra_data: dict | None = None):
        if extra_data:
            context.user_data["conv_data"].update(extra_data)
        context.user_data["conv_state"] = next_state
        await update.message.reply_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)

    # ─── 文件码前缀路由 ──────────────────────────────────────────
    if state == "add_code_route:prefix":
        await _ask("add_code_route:bot",
                    f"✅ 前缀已记录：`{text.lower()}`\n\n请输入目标机器人用户名（不需要 @）：",
                    {"prefix": text.lower()})

    elif state == "add_code_route:bot":
        bot_username = text.lstrip("@").lower()
        await set_code_bot_route(data["prefix"], bot_username)
        await _end(f"✅ 文件码路由已设置\n  前缀：`{data['prefix']}`\n  目标机器人：@{bot_username}")

    elif state == "remove_code_route:prefix":
        prefix = text.lower()
        await delete_code_bot_route(prefix)
        await _end(f"✅ 文件码前缀路由已删除：`{prefix}`")

    # ─── Bot 解码间隔 ────────────────────────────────────────────
    elif state == "set_bot_interval:bot":
        await _ask("set_bot_interval:seconds",
                    f"✅ Bot 已记录：@{text.lstrip('@')}\n\n请输入解码间隔秒数（输入 0 取消限制）：",
                    {"bot": text.lstrip("@").lower()})

    elif state == "set_bot_interval:seconds":
        try:
            interval = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字（秒数），例如：3")
            return
        if interval < 0:
            await update.message.reply_text("❌ 间隔秒数不能为负数，请重新输入：")
            return
        await set_bot_decode_interval(data["bot"], interval)
        msg = f"✅ 已取消 @{data['bot']} 的解码间隔限制" if interval == 0 else f"✅ @{data['bot']} 的解码间隔已设为 {interval} 秒"
        await _end(msg)

    elif state == "remove_bot_interval:bot":
        await delete_bot_decode_interval(text.lstrip("@").lower())
        await _end(f"✅ 已删除 @{text.lstrip('@')} 的解码间隔配置")

    # ─── 用户管理 ────────────────────────────────────────────────
    elif state == "user_detail:id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        users_col = get_users_col()
        user = await _ensure_user(user_id)
        level = user.get("membership_level", "free")
        await _end(
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

    elif state == "set_level:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        await _ask("set_level:level",
                    f"✅ 用户已记录：{text}\n\n请输入会员等级（1=免费 / 2=基础 / 3=高级）：",
                    {"user_id": int(text)})

    elif state == "set_level:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await update.message.reply_text("❌ 请输入 1（免费）、2（基础）或 3（高级）：")
            return
        user_id = data["user_id"]
        users_col = get_users_col()
        await _ensure_user(user_id)
        update_doc = {"$set": {"membership_level": level, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}}
        if level == "free":
            update_doc["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        elif level == "basic":
            update_doc["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        elif level == "premium":
            update_doc["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        await users_col.update_one({"user_id": user_id}, update_doc)
        await _end(f"✅ 用户 {user_id} 已设置为 {MEMBERSHIP_LEVELS[level]}")

    elif state == "ban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await _end(f"✅ 用户 {user_id} 已封禁")

    elif state == "unban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await _end(f"✅ 用户 {user_id} 已解封")

    elif state == "set_quota:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        await _ask("set_quota:quota",
                    f"✅ 用户已记录：{text}\n\n请输入每日解码配额（-1 为不限，0 为禁止）：",
                    {"user_id": int(text)})

    elif state == "set_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字，请重新输入（-1 为不限）：")
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await _end(f"✅ 用户 {data['user_id']} 每日解码配额已设为 {_quota_display(quota)}")

    elif state == "set_external_quota:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字，请重新输入：")
            return
        await _ask("set_external_quota:quota",
                    f"✅ 用户已记录：{text}\n\n请输入外部码配额（-1 不限，0 禁止）：",
                    {"user_id": int(text)})

    elif state == "set_external_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字，请重新输入（-1 不限，0 禁止）：")
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await _end(f"✅ 用户 {data['user_id']} 外部码配额已设为 {_quota_display(quota)}")

    # ─── 文件管理 ────────────────────────────────────────────────
    elif state == "file_detail:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        record = await files_col.find_one({"file_code": file_code})
        if record is None:
            await _end(f"❌ 文件码 {file_code} 不存在")
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
        await _end(
            f"📁 文件详情\n\n"
            f"🔑 文件码：{file_code}\n"
            f"👤 上传者：{record.get('uploader_id')}\n"
            f"📦 文件类型：{type_desc}\n"
            f"📊 状态：{record.get('status', 'active')}\n"
            f"📈 请求次数：{record.get('request_count', 0)}\n"
            f"📅 创建时间：{_format_datetime(record.get('create_time'))}\n"
            f"📺 主频道：{record.get('primary_channel_id')}\n"
            f"🔄 备份数：{len(backups)}个频道"
        )

    elif state == "delete_file:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        result = await files_col.update_one({"file_code": file_code}, {"$set": {"status": "deleted"}})
        if result.matched_count == 0:
            await _end(f"❌ 文件码 {file_code} 不存在")
        else:
            await _end(f"✅ 文件 {file_code} 已删除")

    # ─── 频道管理 ────────────────────────────────────────────────
    elif state == "add_channel:id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字，请重新输入：")
            return
        context.user_data["conv_data"]["channel_id"] = channel_id
        context.user_data["conv_state"] = "add_channel:select_bot"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Bot 1", callback_data=f"conv_sel_bot:{channel_id}:1"),
             InlineKeyboardButton("🤖 Bot 2", callback_data=f"conv_sel_bot:{channel_id}:2"),
             InlineKeyboardButton("🤖 Bot 3", callback_data=f"conv_sel_bot:{channel_id}:3")],
            [InlineKeyboardButton("❌ 取消", callback_data="conv:cancel")],
        ])
        await update.message.reply_text(
            f"✅ 频道已记录：{channel_id}\n\n请选择该频道由哪个备份机器人负责：",
            reply_markup=kb,
        )

    elif state == "remove_channel:bot_num":
        try:
            bot_num = int(text)
        except ValueError:
            await update.message.reply_text("❌ 编号必须是数字（1/2/3），请重新输入：")
            return
        if bot_num not in (1, 2, 3):
            await update.message.reply_text("❌ 编号只能是 1、2 或 3，请重新输入：")
            return
        await _ask("remove_channel:channel_id",
                    f"✅ 机器人已选择：Bot {bot_num}\n\n请输入要删除的频道ID：",
                    {"bot_num": bot_num})

    elif state == "remove_channel:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字，请重新输入：")
            return
        channels = await get_backup_channels(data["bot_num"])
        if channel_id not in channels:
            await _end(f"❌ 频道 {channel_id} 不在 Bot {data['bot_num']} 的备份列表中")
            return
        channels = [c for c in channels if c != channel_id]
        await set_backup_channels(data["bot_num"], channels)
        await _end(f"✅ 已从 Bot {data['bot_num']} 删除备份频道 {channel_id}")

    elif state == "promote_channel:id":
        try:
            new_channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字，请重新输入：")
            return
        all_backups = list(settings.ALL_BACKUP_CHANNELS)
        if not all_backups:
            await _end("❌ 没有任何备份频道可提升")
            return
        if new_channel_id not in all_backups:
            await _end(f"❌ 频道 {new_channel_id} 不在备份频道列表中。\n当前备份频道: {all_backups}")
            return
        old_channel = await get_active_storage_channel_id()
        if new_channel_id == old_channel:
            await _end(f"⚠️ 频道 {new_channel_id} 已经是当前主频道")
            return
        success = await set_active_storage_channel_id(new_channel_id)
        if not success:
            await _end("❌ 切换主存储频道失败，请检查数据库连接。")
            return
        invalidate_cache()
        await _end(
            f"✅ 主存储频道已切换！\n\n"
            f"旧主频道：{old_channel}\n"
            f"新主频道：{new_channel_id}\n\n"
            f"📤 upload_bot 将上传新文件到新频道\n"
            f"🤖 backup_bot 将实时监控新频道的新消息\n"
            f"📨 sender_bot 发送时优先使用新频道"
        )

    # ─── 备份机器人 ──────────────────────────────────────────────
    elif state == "add_backup_bot:bot_num":
        try:
            bot_num = int(text)
        except ValueError:
            await update.message.reply_text("❌ 编号必须是数字（1/2/3），请重新输入：")
            return
        if bot_num not in (1, 2, 3):
            await update.message.reply_text("❌ 编号只能是 1、2 或 3，请重新输入：")
            return
        await _ask("add_backup_bot:token",
                    f"✅ 编号已记录：Bot {bot_num}\n\n请输入 Bot Token（一串字符）：\n\n也可附带频道ID，格式：\n<Token> <频道ID1> <频道ID2>...",
                    {"bot_num": bot_num})

    elif state == "add_backup_bot:token":
        parts = text.split()
        token = parts[0].strip()
        await set_backup_bot_token(data["bot_num"], token)
        extra_info = ""
        if len(parts) > 1:
            channel_ids = []
            for a in parts[1:]:
                try:
                    channel_ids.append(int(a))
                except ValueError:
                    pass
            if channel_ids:
                await set_backup_channels(data["bot_num"], channel_ids)
                extra_info = f"\n频道已配置: {channel_ids}"
        masked = token[:8] + "..." + token[-4:] if len(token) > 15 else "***"
        await _end(
            f"✅ 备份机器人 {data['bot_num']} Token 已保存 ({masked}){extra_info}\n"
            f"⚠️ 需要重启 backup_bot_{data['bot_num']} 进程才能生效"
        )

    elif state == "remove_backup_bot:bot_num":
        try:
            bot_num = int(text)
        except ValueError:
            await update.message.reply_text("❌ 编号必须是数字（1/2/3），请重新输入：")
            return
        if bot_num not in (1, 2, 3):
            await update.message.reply_text("❌ 编号只能是 1、2 或 3，请重新输入：")
            return
        await delete_backup_bot_token(bot_num)
        await _end(f"✅ 已删除备份机器人 {bot_num} 配置\n⚠️ 重启后该备份机器人将不再启动")

    elif state == "backup_reset:bot_name":
        bot_name = text.strip()
        if bot_name not in ("backup_bot_1", "backup_bot_2", "backup_bot_3"):
            await update.message.reply_text("❌ 请输入 backup_bot_1、backup_bot_2 或 backup_bot_3：")
            return
        files_col = get_file_records_col()
        await set_config(f"backup_{bot_name}_full_sync_done", "0")
        await set_config(f"backup_{bot_name}_last_synced_id", "0")
        await set_config(f"backup_{bot_name}_quick_cursor", "0")
        channels = await get_backup_channels(int(bot_name[-1]))
        cleared_count = 0
        if channels:
            records = await files_col.find({"backup_channel_msg_ids": {"$ne": "", "$ne": None}}, limit=500)
            for record in records:
                backups = record.get("backup_channel_msg_ids") or []
                if isinstance(backups, str):
                    try:
                        import json
                        backups = json.loads(backups)
                    except Exception:
                        continue
                if not isinstance(backups, list):
                    continue
                new_backups = [b for b in backups if isinstance(b, dict) and b.get("channel_id") not in channels]
                if len(new_backups) != len(backups):
                    await files_col.update_one({"file_code": record["file_code"]}, {"$set": {"backup_channel_msg_ids": new_backups}})
                    cleared_count += 1
        await _end(
            f"✅ 已重置 {bot_name} 的备份状态\n"
            f"• 同步状态已清零\n"
            f"• 清理了 {cleared_count} 条文件记录中的旧备份信息\n"
            f"⚠️ 备份机器人重启后将执行全量备份到频道 {channels}"
        )

    # ─── 中继 ────────────────────────────────────────────────────
    elif state == "relay_code:code":
        code = text.strip()
        # 验证码写入 DB，由 idx_bot 的 relay_instance 自动读取
        await set_config("relay_auth_code", code)
        await set_config("relay_auth_pending", "1")
        await _end(f"✅ 验证码 `{code}` 已提交\n中继实例将在几秒内自动获取并使用。")

    elif state == "relay_set_api:api_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ API_ID 必须是数字，请重新输入：")
            return
        await _ask("relay_set_api:api_hash",
                    f"✅ API_ID 已记录：{text}\n\n第二步：请输入 API_HASH：",
                    {"api_id": text.strip()})

    elif state == "relay_set_api:api_hash":
        await _ask("relay_set_api:phone",
                    f"✅ API_HASH 已记录：{text}\n\n第三步：请输入手机号（含区号，如 +8613800138000）：",
                    {"api_hash": text.strip()})

    elif state == "relay_set_api:phone":
        from services.relay_pool import relay_pool
        phone = text.strip()
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]
        instance = await relay_pool.add_account(api_id, api_hash, phone)
        # 开始登录
        try:
            await instance.login_with_credentials(api_id, api_hash, phone)
            await _end(
                f"✅ 中继账号已添加并登录成功\n"
                f"  API_ID: {api_id}\n"
                f"  API_HASH: {api_hash[:8]}...\n"
                f"  手机号: {phone}\n\n"
                f"该账号已加入账号池，可立即使用。"
            )
        except RuntimeError as e:
            await _end(f"❌ 登录失败: {e}")

    # ─── 系统配置 ────────────────────────────────────────────────
    elif state == "set_storage_channel:id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字，请重新输入：")
            return
        await set_config("storage_channel_id", str(channel_id))
        await _end(f"✅ 主存储频道已设为 {channel_id}\n⚠️ 需重启所有机器人后生效")

    elif state == "set_decoder_chat:id":
        try:
            chat_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ChatID必须是数字，请重新输入：")
            return
        await set_config("decoder_chat_id", str(chat_id))
        await _end(f"✅ 解码机器人对话已设为 {chat_id}")

    elif state == "set_file_prefix:prefix":
        prefix = text.strip()
        await set_config("file_code_prefix", prefix)
        await _end(f"✅ 文件码前缀已设为 {prefix}\n⚠️ 需重启 upload_bot 后生效")

    elif state == "set_force_join:channel_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字，请重新输入：")
            return
        await _ask("set_force_join:link",
                    f"✅ 频道已记录：{text}\n\n请输入加群链接（如无可直接发送 0 跳过）：",
                    {"channel_id": int(text)})

    elif state == "set_force_join:link":
        link = text.strip()
        if link == "0":
            link = ""
        await set_config("force_join_channel_id", str(data["channel_id"]))
        if link:
            await set_config("force_join_link", link)
        await _end(f"✅ 强制加群频道已设为 {data['channel_id']}" + (f"\n🔗 链接：{link}" if link else ""))

    elif state == "set_username:role":
        role = text.lower()
        if role not in ("upload", "decoder", "sender"):
            await update.message.reply_text("❌ 角色必须是 upload、decoder 或 sender，请重新输入：")
            return
        await _ask("set_username:name",
                    f"✅ 角色已记录：{role}\n\n请输入 @用户名（不需要 @）：",
                    {"role": role})

    elif state == "set_username:name":
        key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
        key = key_map.get(data["role"])
        username = text.lstrip("@")
        await set_config(key, username)
        await _end(f"✅ {data['role']} 机器人用户名已设为 @{username}")

    elif state == "set_quota_default:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await update.message.reply_text("❌ 请输入 1（免费）、2（基础）或 3（高级）：")
            return
        await _ask("set_quota_default:quota",
                    f"✅ 等级已记录：{text}\n\n请输入每日默认解码配额（-1 为不限）：",
                    {"level": level})

    elif state == "set_quota_default:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字（-1 表示不限），请重新输入：")
            return
        await set_config(f"quota_default_{data['level']}", str(quota))
        await _ask("set_quota_default:ext_quota",
                    f"✅ 解码配额已记录：{_quota_display(quota)}\n\n请输入外部码默认配额（-1 不限，0 禁止，直接发送 0 跳过）：",
                    {"quota": quota})

    elif state == "set_quota_default:ext_quota":
        try:
            ext_quota = int(text)
        except ValueError:
            ext_quota = 0
        if ext_quota > 0:
            await set_config(f"quota_external_{data['level']}", str(ext_quota))
        msg = f"✅ {data['level']} 日配额已设为 {_quota_display(data['quota'])}"
        if ext_quota > 0:
            msg += f"，外部码配额 {_quota_display(ext_quota)}"
        await _end(msg)

    elif state == "set_r2:account_id":
        await _ask("set_r2:access_key",
                    f"✅ 账号ID已记录：{text}\n\n第二步：请输入 R2 Access Key ID：",
                    {"account_id": text.strip()})

    elif state == "set_r2:access_key":
        await _ask("set_r2:secret_key",
                    f"✅ Access Key 已记录：{text[:8]}...\n\n第三步：请输入 R2 Secret Access Key：",
                    {"access_key": text.strip()})

    elif state == "set_r2:secret_key":
        await _ask("set_r2:bucket",
                    f"✅ Secret Key 已记录：{text[:8]}...\n\n第四步：请输入桶名（Bucket Name，直接发送 0 跳过）：",
                    {"secret_key": text.strip()})

    elif state == "set_r2:bucket":
        bucket = text.strip()
        if bucket == "0":
            bucket = ""
        await set_config("r2_account_id", data["account_id"])
        await set_config("r2_access_key_id", data["access_key"])
        await set_config("r2_secret_access_key", data["secret_key"])
        if bucket:
            await set_config("r2_bucket", bucket)
        await _end(f"✅ R2 备份配置已保存\n  Bucket: {bucket or '(默认)'}\n⚠️ 需重启后生效")

    elif state == "set_db_backup:interval":
        try:
            interval = int(text)
        except ValueError:
            await update.message.reply_text("❌ 间隔分钟数必须是数字，请重新输入：")
            return
        await _ask("set_db_backup:enabled",
                    f"✅ 间隔已记录：{interval} 分钟\n\n请输入开关状态（on / off）：",
                    {"interval": interval})

    elif state == "set_db_backup:enabled":
        on_off = text.strip().lower()
        if on_off not in ("on", "off"):
            await update.message.reply_text("❌ 请输入 on 或 off：")
            return
        await set_config("db_backup_interval_minutes", str(data["interval"]))
        await set_config("db_backup_enabled", "1" if on_off == "on" else "0")
        await _end(f"✅ DB 自动备份已{'开启' if on_off == 'on' else '关闭'}，间隔 {data['interval']} 分钟")

    # 未知状态 → 清理
    else:
        _conv_end(context)
        await update.message.reply_text("⏳ 对话已超时，请重新点击按钮开始操作。")


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
    app.add_handler(CommandHandler("relay_list", relay_list))
    app.add_handler(CommandHandler("relay_add", relay_add))
    app.add_handler(CommandHandler("relay_remove", relay_remove))
    app.add_handler(CommandHandler("relay_reset_stats", relay_reset_stats))
    app.add_handler(CommandHandler("backup_reset", backup_reset))
    app.add_handler(CommandHandler("promote_channel", promote_channel))
    app.add_handler(CommandHandler("settings", settings_view))
    app.add_handler(CommandHandler("set_storage_channel", set_storage_channel))
    app.add_handler(CommandHandler("set_decoder_chat", set_decoder_chat))
    app.add_handler(CommandHandler("set_file_prefix", set_file_prefix))
    app.add_handler(CommandHandler("set_force_join", set_force_join))
    app.add_handler(CommandHandler("set_username", set_username))
    app.add_handler(CommandHandler("set_quota_default", set_quota_default))
    app.add_handler(CommandHandler("set_r2", set_r2))
    app.add_handler(CommandHandler("set_db_backup", set_db_backup))
    app.add_handler(CommandHandler("factory_reset", factory_reset))
    app.add_handler(CommandHandler("purge_channel", purge_channel))
    app.add_handler(CommandHandler("add_code_route", add_code_route))
    app.add_handler(CommandHandler("remove_code_route", remove_code_route))
    app.add_handler(CommandHandler("code_routes", list_code_routes))
    app.add_handler(CommandHandler("set_bot_interval", set_bot_interval))
    app.add_handler(CommandHandler("remove_bot_interval", remove_bot_interval))
    app.add_handler(CommandHandler("bot_intervals", list_bot_intervals))
    app.add_handler(CommandHandler("cancel", cancel_conversation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_conversation))
    app.add_handler(CallbackQueryHandler(assign_channel_callback, pattern=r"^assign_chan:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu:|action:|usage:|interactive:|conv:)"))

    app.run_polling()
