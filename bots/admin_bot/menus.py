from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import settings
from utils.shared_counters import status_counters as _status_counters, status_counters_initialized as _status_counters_initialized

TOKEN = settings.ADMIN_BOT_TOKEN
AUTHORIZED_USER_ID = settings.ADMIN_TELEGRAM_ID


def _auth_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != AUTHORIZED_USER_ID:
            await update.message.reply_text("⛔ 您没有权限使用此机器人。")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _quota_display(val: int) -> str:
    if val == -1:
        return "不限"
    return str(val)


BACK_BTN = [[InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")]]


def _build_menu(menu_id: str) -> tuple[str, InlineKeyboardMarkup]:
    if menu_id == "main":
        text = "🤖 管理员面板 — 点击按钮操作"
        kb = [
            [InlineKeyboardButton("📊 系统状态", callback_data="menu:sys"),
             InlineKeyboardButton("👤 用户管理", callback_data="menu:user")],
            [InlineKeyboardButton("📁 文件管理", callback_data="menu:file"),
             InlineKeyboardButton(" 解码日志", callback_data="action:logs")],
            [InlineKeyboardButton("🔐 用户中继", callback_data="menu:relay"),
             InlineKeyboardButton("⚙️ 系统配置", callback_data="menu:config")],
            [InlineKeyboardButton("🗺️ 文件码路由", callback_data="menu:code_route"),
             InlineKeyboardButton("⏱️ Bot限流", callback_data="menu:bot_limit")],
            [InlineKeyboardButton("🔗 环形拓扑", callback_data="menu:topology"),
             InlineKeyboardButton("🔄 备用池", callback_data="menu:spare")],
            [InlineKeyboardButton("⏳ 轮转配置", callback_data="menu:rotation")],
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
        text = "⚙️ 系统配置 — 点击按钮操作\n\n⚠️需重启 = 配置需重启所有Bot后生效 | ✅热更新 = 配置即时生效"
        kb = [
            [InlineKeyboardButton("📋 查看全部配置", callback_data="action:settings")],
            [InlineKeyboardButton("📺 主存储频道", callback_data="interactive:set_storage_channel")],
            [InlineKeyboardButton("📝 文件码前缀", callback_data="interactive:set_file_prefix"),
             InlineKeyboardButton("🔒 强制加群", callback_data="interactive:set_force_join")],
            [InlineKeyboardButton("👤 机器人用户名", callback_data="interactive:set_username"),
             InlineKeyboardButton("🎫 默认配额", callback_data="interactive:set_quota_default")],
            [InlineKeyboardButton("☁️ R2备份配置", callback_data="interactive:set_r2"),
             InlineKeyboardButton("💾 DB自动备份", callback_data="interactive:set_db_backup")],
            [InlineKeyboardButton("📋 引导Bot消息", callback_data="interactive:set_filebot_msg")],
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

    if menu_id == "topology":
        text = "🔗 环形冗余拓扑 — 点击查看"
        kb = [
            [InlineKeyboardButton("📋 查看拓扑", callback_data="action:topology")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "spare":
        text = "🔄 备用池管理\n\n管理备用频道池，封禁后自动补充空缺。"
        kb = [
            [InlineKeyboardButton("➕ 添加备用频道", callback_data="interactive:spare_add"),
             InlineKeyboardButton("➖ 移除备用频道", callback_data="interactive:spare_remove")],
            [InlineKeyboardButton("📋 查看备用池", callback_data="action:spare_list")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "rotation":
        text = "⏳ 轮转配置管理\n\n设置活跃频道轮转参数（文件数/时间）。"
        kb = [
            [InlineKeyboardButton("📋 查看配置", callback_data="action:rotation_view"),
             InlineKeyboardButton("⚙️ 修改参数", callback_data="interactive:rotation_set")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    return _build_menu("main")


_CONV_CANCEL_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ 取消操作", callback_data="conv:cancel")],
])