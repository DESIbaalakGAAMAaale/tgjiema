import datetime

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from database import (
    get_users_col, get_file_records_col, get_decode_logs_col,
    get_config, set_config,
    get_relay_config, set_relay_config,
    get_all_code_bot_routes, set_code_bot_route, delete_code_bot_route,
    get_all_bot_decode_intervals, set_bot_decode_interval, delete_bot_decode_interval,
    add_spare_channel, remove_spare, list_spare_pool,
    get_rotation_config, set_rotation_config,
    get_user_cached, update_user_and_invalidate,
)
from utils.time_utils import format_datetime

from .menus import (
    _auth_required, _quota_display, MEMBERSHIP_LEVELS, LEVEL_ALIAS,
)
from .display import (
    _ensure_user, _get_status_text, _get_health_text, _get_topology_text,
    _get_logs_page_text, _get_users_page_text,
    _get_configs_text,
)
from .conversation import _conv_end


@_auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .menus import _build_menu
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
        await update.message.reply_text("用法:/user <用户ID>")
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
        f"🆔 ID:{user.get('user_id')}\n"
        f"📝 用户名:@{user.get('username') or 'N/A'}\n"
        f"👤 昵称:{user.get('first_name') or 'N/A'}\n"
        f"🏅 会员等级:{MEMBERSHIP_LEVELS.get(level, level)}\n"
        f"🔒 是否封禁:{'是 ❌' if user.get('is_banned') else '否 ✅'}\n"
        f"📤 允许上传:{'是 ✅' if user.get('can_upload') else '否 ❌'}\n"
        f"📅 解码配额:{_quota_display(user.get('daily_decode_quota'))}/天\n"
        f"📊 今日已用:{user.get('quota_used_today', 0)}次\n"
        f"🌐 外部码配额:{_quota_display(user.get('external_decode_quota'))}/天\n"
        f"🌐 外部已用:{user.get('external_used_today', 0)}次\n"
        f"📅 注册时间:{format_datetime(user.get('created_at'))}\n"
        f"🔄 更新时间:{format_datetime(user.get('updated_at'))}"
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
        await update.message.reply_text("用法:/set_level <用户ID> <1|2|3>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return
    level = LEVEL_ALIAS.get(args[1].lower())
    if not level:
        await update.message.reply_text("❌ 等级:1=免费 2=基础 3=高级")
        return

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
    await update_user_and_invalidate(user_id)  # 使缓存失效
    await update.message.reply_text(f"✅ 用户 {user_id} 已设置为 {MEMBERSHIP_LEVELS[level]}")


@_auth_required
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/ban <用户ID>")
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
    await update_user_and_invalidate(user_id)
    await update.message.reply_text(f"✅ 用户 {user_id} 已封禁")


@_auth_required
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/unban <用户ID>")
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
    await update_user_and_invalidate(user_id)
    await update.message.reply_text(f"✅ 用户 {user_id} 已解封")


@_auth_required
async def set_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法:/set_quota <用户ID> <每日解码配额(-1为不限)>")
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
    await update_user_and_invalidate(user_id)
    await update.message.reply_text(f"✅ 用户 {user_id} 每日解码配额已设为 {_quota_display(quota)}")


@_auth_required
async def set_external_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法:/set_external_quota <用户ID> <外部码配额(-1为不限,0为禁止)>")
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
    await update_user_and_invalidate(user_id)
    await update.message.reply_text(f"✅ 用户 {user_id} 外部码配额已设为 {_quota_display(quota)}")


@_auth_required
async def file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/file <文件码>")
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
        f"🔑 文件码:{file_code}\n"
        f"👤 上传者:{record.get('uploader_id')}\n"
        f"📦 文件类型:{type_desc}\n"
        f"📊 状态:{record.get('status', 'active')}\n"
        f"📈 请求次数:{record.get('request_count', 0)}\n"
        f"📅 创建时间:{format_datetime(record.get('create_time'))}\n"
        f"📺 主频道:{record.get('primary_channel_id')}\n"
        f"🔄 备份数:{len(backups)}个频道\n"
    )
    note = record.get("note", "")
    if note:
        msg += f"📝 备注:{note}\n"
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

    msg = f"📁 文件列表 (第{page}/{total_pages}页,共{total}个)\n"
    if search:
        msg += f"🔍 搜索:{search}\n"
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
        await update.message.reply_text("用法:/delete_file <文件码>")
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
    # 失效缓存
    try:
        from database.cache import invalidate_file_record
        invalidate_file_record(file_code)
    except Exception:
        pass
    await update.message.reply_text(f"✅ 文件 {file_code} 已删除")


@_auth_required
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])
    await update.message.reply_text(await _get_logs_page_text(page))


@_auth_required
async def relay_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/relay_code <验证码>\n\n"
            "用于解码机器人登录 Telegram 用户账号时提交验证码。\n"
            "验证码(6位)会发送到该账号已登录的 Telegram 客户端。"
        )
        return
    code = args[0].strip()
    if not code.isdigit() or len(code) not in (5, 6):
        await update.message.reply_text("❌ 验证码格式不正确,应为 5-6 位数字")
        return

    await set_config("relay_auth_code", code)
    await update.message.reply_text(
        f"✅ 验证码 `{code}` 已提交\n"
        f"解码机器人将在几秒内自动获取并使用。"
    )


@_auth_required
async def relay_set_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/relay_set_api <手机号>\n\n"
            "示例:/relay_set_api +8613800138000\n\n"
            "API_ID 和 API_HASH 从 .env 自动读取。\n"
            "配置后保存在数据库中,解码机器人下次重启时生效。"
        )
        return
    phone = args[0].strip()

    from services.relay_pool import relay_pool
    from config import settings
    api_id = settings.RELAY_API_ID
    api_hash = settings.RELAY_API_HASH
    if not api_id or not api_hash:
        await update.message.reply_text(
            "❌ 中继 API 配置未设置\n"
            "请在 .env 文件中配置 RELAY_API_ID 和 RELAY_API_HASH\n"
            "（从 https://my.telegram.org 申请）"
        )
        return

    await set_relay_config(api_id, api_hash, phone)
    await update.message.reply_text(
        f"✅ 中继账号已配置\n"
        f"API_ID:{api_id}\n"
        f"手机号:{phone[:3]}****{phone[-2:] if len(phone) > 5 else ''}\n\n"
        f"⚠️ 配置已保存到数据库,解码机器人下次重启时生效。\n"
        f"⚠️ 请确保该账号未开启二步验证。\n"
        f"🔐 建议在配置完成后立即删除本聊天记录中的密钥信息。"
    )


@_auth_required
async def relay_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await get_config("relay_auth_pending")
    if pending == "1":
        await update.message.reply_text(
            "⏳ 中继正在等待验证码\n\n"
            "Telegram 已发送 6 位验证码到中继账号的已登录客户端,\n"
            "请查看并提交:/relay_code <验证码>"
        )
    else:
        await update.message.reply_text(
            "✅ 中继当前不需要验证码\n\n"
            "如果解码机器人在等待验证码但此处显示不需要,\n"
            "可能是状态同步延迟,请稍后重试或查看状态面板。"
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
            "请使用 /relay_add 添加中继账号,或通过管理面板配置。"
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
    """添加中继账号(命令行方式)"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/relay_add <手机号>\n\n"
            "示例:/relay_add +8613800138000\n\n"
            "API_ID 和 API_HASH 从 .env 自动读取。\n"
            "添加后需要提交验证码完成登录,或直接在管理面板中配置。"
        )
        return
    phone = args[0].strip()

    from services.relay_pool import relay_pool
    from config import settings
    api_id = settings.RELAY_API_ID
    api_hash = settings.RELAY_API_HASH
    if not api_id or not api_hash:
        await update.message.reply_text(
            "❌ 中继 API 配置未设置\n"
            "请在 .env 文件中配置 RELAY_API_ID 和 RELAY_API_HASH\n"
            "（从 https://my.telegram.org 申请）"
        )
        return

    try:
        instance = await relay_pool.add_account(api_id, api_hash, phone)
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await update.message.reply_text(
            f"✅ 中继账号已添加到池中\n"
            f"  API_ID: {api_id}\n"
            f"  API_HASH: {api_hash[:8]}...\n"
            f"  手机号: {masked}\n\n"
            f"解码机器人将自动检测新账号并连接。\n"
            f"如需要登录验证码,请使用 /relay_code 提交。"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 添加失败: {e}")


@_auth_required
async def relay_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除中继账号"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/relay_remove <手机号>\n\n"
            "示例:/relay_remove +8613800138000"
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


# ─── 系统配置管理 ────────────────────────────────────────────────


@_auth_required
async def settings_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await _get_configs_text()
        await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"[settings] 获取配置失败: {e}")
        await update.message.reply_text(f"❌ 获取配置失败: {e}")


@_auth_required
async def set_storage_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/set_storage_channel <频道ID>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return
    await set_config("storage_channel_id", str(channel_id))
    await update.message.reply_text(f"✅ 主存储频道已设为 {channel_id}\n⚠️ 需重启所有机器人后生效")


@_auth_required
async def set_file_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/set_file_prefix <前缀>\n当前:/settings 查看")
        return
    prefix = args[0].strip()
    await set_config("file_code_prefix", prefix)
    await update.message.reply_text(f"✅ 文件码前缀已设为 {prefix}\n⚠️ 需重启 up_bot 后生效")


@_auth_required
async def set_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/set_force_join <频道ID> [加群链接]")
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
    await update.message.reply_text(f"✅ 强制加群频道已设为 {channel_id}\n" + (f"🔗 链接:{link}" if link else "") + " ✅热更新")


@_auth_required
async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法:/set_username <upload|decoder|sender> <@用户名>")
        return
    bot_role = args[0].lower()
    username = args[1].lstrip("@")
    key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
    key = key_map.get(bot_role)
    if not key:
        await update.message.reply_text("❌ 角色必须是 upload、decoder 或 sender")
        return
    await set_config(key, username)
    await update.message.reply_text(f"✅ {bot_role} 机器人用户名已设为 @{username} ✅热更新")


@_auth_required
async def set_quota_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法:/set_quota_default <1|2|3> <日配额> [外部码日配额]")
        return
    level = LEVEL_ALIAS.get(args[0].lower())
    if not level:
        await update.message.reply_text("❌ 等级:1=免费 2=基础 3=高级")
        return
    try:
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ 配额必须是数字(-1 表示不限)")
        return
    await set_config(f"quota_default_{level}", str(quota))
    msg = f"✅ {level} 日配额已设为 {_quota_display(quota)}"

    if len(args) >= 3:
        try:
            ext_quota = int(args[2])
        except ValueError:
            ext_quota = quota
        await set_config(f"quota_external_{level}", str(ext_quota))
        msg += f",外部码配额 {_quota_display(ext_quota)}"

    msg += "\n⚠️ 已有用户的配额不受影响 ✅热更新"
    await update.message.reply_text(msg)


@_auth_required
async def set_r2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("用法:/set_r2 <账号ID> <AccessKey> <SecretKey> [桶名]")
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
        await update.message.reply_text("用法:/set_db_backup <间隔分钟> <开/关>")
        return
    try:
        interval = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 间隔必须是数字(分钟)")
        return
    enabled = args[1].lower() in ("开", "1", "true", "yes", "on")
    await set_config("db_backup_interval", str(interval))
    await set_config("db_backup_enabled", "true" if enabled else "false")
    await update.message.reply_text(
        f"✅ 数据库备份:间隔 {interval} 分钟,状态:{'开启' if enabled else '关闭'} ✅热更新"
    )


# ─── 工厂重置 ────────────────────────────────────────────────────


_FACTORY_RESET_TABLES = [
    "file_records", "decode_logs", "pending_uploads",
    "users", "backup_config",
]


@_auth_required
async def factory_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        await update.message.reply_text(
            "⚠️️ ️ 危险的工厂重置!\n\n"
            "此操作将清空以下所有数据:\n"
            "• 📦 file_records(文件记录)\n"
            "• 📋 decode_logs(解码日志)\n"
            "• 📤 pending_uploads(上传队列)\n"
            "• 👤 users(用户数据)\n"
            "• ⚙️ backup_config(系统配置与备份配置)\n\n"
            "频道中的消息不会被删除,但备份状态会重置,全量备份将被重新触发。\n\n"
            "🔴 如果您确认,请发送:\n"
            "/factory_reset confirm\n\n"
            "🔴 最终确认请发送:\n"
            "/factory_reset confirm I_UNDERSTAND"
        )
        return

    if args[0] != "confirm":
        await update.message.reply_text("❌ 请先使用 /factory_reset 查看说明")
        return

    if len(args) < 2 or args[1] != "I_UNDERSTAND":
        await update.message.reply_text(
            "⚠️ 二次确认\n\n"
            "发送以下命令执行最终重置:\n"
            "/factory_reset confirm I_UNDERSTAND\n\n"
            "此操作不可撤销!所有用户数据、文件码、配置将被永久清空。\n"
            "频道消息需要手动删除。"
        )
        return

    msg = await update.message.reply_text("🔄 正在执行工厂重置...")

    from database.session import CockroachDBClient

    client = CockroachDBClient()
    client.configure(settings.COCKROACHDB_URL)
    await client.connect()

    cleared = []
    errors = []
    for table in _FACTORY_RESET_TABLES:
        try:
            sql = f"DELETE FROM {table}"
            await client.execute(sql)
            cleared.append(table)
        except Exception as e:
            errors.append(f"{table}: {e}")
            logger.error(f"[factory_reset] 清空 {table} 失败: {e}")

    await client.close()

    if errors:
        await msg.edit_text(
            "⚠️ 工厂重置部分完成!\n\n"
            f"已清空 {len(cleared)} 张表: {', '.join(cleared)}\n\n"
            "以下表清空失败:\n" + "\n".join(f"  • {e}" for e in errors) + "\n\n"
            "请检查数据库状态后重试。"
        )
    else:
        await msg.edit_text(
            "✅ 工厂重置完成!\n\n"
            f"已清空 {len(cleared)} 张表:{', '.join(cleared)}\n\n"
            "⚠️ 存储频道的消息不会被自动删除。\n"
            "如需清空存储频道,请手动执行:\n"
            "  /purge_channel <频道ID>\n\n"
            "🔄 请重启所有机器人以使配置生效。"
        )


@_auth_required
async def purge_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/purge_channel <频道ID>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return

    await update.message.reply_text(
        f"⚠️ Telegram Bot API 不支持批量删除频道消息。\n\n"
        f"请手动处理频道 {channel_id}:\n"
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
            "用法:/add_code_route <前缀> <机器人用户名>\n\n"
            "设置文件码前缀对应的解码机器人。\n"
            "当文件码以指定前缀开头时,中继将路由到该机器人解码。\n\n"
            "示例:\n"
            "/add_code_route qqfile qqfile_bot\n"
            "/add_code_route tgwenjian mydecoder_bot"
        )
        return
    prefix = args[0].strip().lower()
    bot_username = args[1].strip().lower().lstrip("@")
    # 输入验证: 前缀只能包含字母数字和下划线,长度 1-50
    if not prefix or len(prefix) > 50 or not all(c.isalnum() or c == '_' for c in prefix):
        await update.message.reply_text(
            "❌ 前缀格式无效:只能包含字母、数字和下划线,长度 1-50 字符。"
        )
        return
    # bot_username 只能包含字母、数字、下划线和 bot 后缀
    if not bot_username or len(bot_username) > 32 or not all(c.isalnum() or c == '_' for c in bot_username):
        await update.message.reply_text(
            "❌ 机器人用户名格式无效:只能包含字母、数字和下划线,长度 1-32 字符。"
        )
        return
    await set_code_bot_route(prefix, bot_username)
    await update.message.reply_text(
        f"✅ 文件码路由已设置\n"
        f"  前缀:{prefix}\n"
        f"  目标机器人:@{bot_username}\n\n"
        f"以 `{prefix}` 开头的文件码将通过 @{bot_username} 解码。"
    )


@_auth_required
async def remove_code_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/remove_code_route <前缀>\n\n"
            "删除文件码前缀路由配置。\n"
            "删除后该前缀的文件码将恢复原有解码规则。\n\n"
            "示例:/remove_code_route qqfile"
        )
        return
    prefix = args[0].strip().lower()
    await delete_code_bot_route(prefix)
    await update.message.reply_text(f"✅ 文件码前缀路由已删除:{prefix}")


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
            "用法:/set_bot_interval <机器人用户名> <间隔秒数>\n\n"
            "设置向指定机器人发送解码请求的最小间隔时间。\n"
            "某些机器人限制每个文件码之间的解码间隔,此设置可自动等待。\n\n"
            "示例:\n"
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
            "用法:/remove_bot_interval <机器人用户名>\n\n"
            "删除指定机器人的解码间隔配置。\n\n"
            "示例:/remove_bot_interval qqfile_bot"
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


# ─── 备用池管理 ──────────────────────────────────────────────────

@_auth_required
async def spare_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/spare_add <频道ID> [账号名]\n\n"
            "添加备用频道到备用池。\n"
            "• 指定账号名:该频道封禁后优先补充同账号频道的空缺\n"
            "• 不指定账号名:作为通用备用池频道,补充任意空缺\n\n"
            "示例:\n"
            "/spare_add -1001234567890\n"
            "/spare_add -1001234567890 账号1"
        )
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return
    account_name = args[1] if len(args) > 1 else None
    await add_spare_channel(channel_id, account_name)
    acc_info = f" (账号: {account_name})" if account_name else " (通用备用池)"
    await update.message.reply_text(f"✅ 备用频道已添加\n  频道ID: {channel_id}{acc_info}")


@_auth_required
async def spare_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/spare_remove <频道ID>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 频道ID必须是数字")
        return
    await remove_spare(channel_id)
    await update.message.reply_text(f"✅ 已从备用池移除频道: {channel_id}")


@_auth_required
async def spare_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spares = await list_spare_pool()
    if not spares:
        await update.message.reply_text("📭 备用池为空\n\n使用 /spare_add 添加备用频道。")
        return
    msg = "🔄 备用池频道列表\n\n"
    for s in spares:
        used = "🔴已用" if s.get("is_used") else "🟢可用"
        acc = s.get("account_name") or "通用"
        msg += f"  {used} {s['channel_id']} — {acc}\n"
    msg += f"\n共 {len(spares)} 个备用频道\n使用 /spare_add 添加 | /spare_remove 删除"
    await update.message.reply_text(msg)


# ─── 轮转配置管理 ──────────────────────────────────────────────

@_auth_required
async def rotation_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法:/rotation_set <参数名> <值>\n\n"
            "可配置参数:\n"
            "• active_window_size — 活跃窗口大小(每组几个活跃频道,默认 3)\n"
            "• files_per_slot — 每个频道接收文件数后切换(默认 500)\n"
            "• time_per_slot — 每个频道活跃时间(秒)后切换(默认 3600)\n\n"
            "示例:\n"
            "/rotation_set files_per_slot 200\n"
            "/rotation_set time_per_slot 1800\n"
            "/rotation_set active_window_size 5"
        )
        return
    key = args[0].strip()
    value = args[1].strip()
    valid_keys = {"active_window_size", "files_per_slot", "time_per_slot"}
    if key not in valid_keys:
        await update.message.reply_text(f"❌ 无效参数: {key}\n有效参数: {', '.join(sorted(valid_keys))}")
        return
    try:
        int(value)
    except ValueError:
        await update.message.reply_text("❌ 值必须是数字")
        return
    db_key = f"rotation_{key}"
    await set_rotation_config(db_key, value)
    label_map = {
        "active_window_size": "活跃窗口大小",
        "files_per_slot": "每频道文件数",
        "time_per_slot": "每频道时间(秒)",
    }
    await update.message.reply_text(
        f"✅ 轮转配置已更新\n  {label_map.get(key, key)}: {value}\n\n"
        f"Mon Bot 将在下一轮自动加载新配置。"
    )


@_auth_required
async def rotation_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = [
        ("rotation_active_window_size", "active_window_size", "活跃窗口大小"),
        ("rotation_files_per_slot", "files_per_slot", "每频道文件数"),
        ("rotation_time_per_slot", "time_per_slot", "每频道时间(秒)"),
    ]
    msg = "🔄 轮转配置\n\n"
    for db_key, fallback_key, label in keys:
        val = await get_rotation_config(db_key)
        if val is None:
            val = str(getattr(settings, f"ROTATION_{fallback_key.upper()}", "—"))
        msg += f"  {label}: {val}\n"
    msg += "\n使用 /rotation_set 修改配置"
    await update.message.reply_text(msg)


# ─── 拓扑查看 ──────────────────────────────────────────────────

@_auth_required
async def topology(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _get_topology_text()
    await update.message.reply_text(text)


@_auth_required
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("conv_state"):
        _conv_end(context)
        await update.message.reply_text("❌ 操作已取消。")
    else:
        await update.message.reply_text("当前没有正在进行的操作。")


# ─── 帮助命令 ──────────────────────────────────────────────────

@_auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "管理员面板 模块说明\n\n"
        "系统状态\n"
        "  /status — 系统概览（进程运行状态、各Bot状态）\n"
        "  /health — 健康检查（频道状态、降级情况）\n\n"
        "用户管理\n"
        "  /users [关键词] [页码] — 用户列表\n"
        "  /user <用户ID> — 查看用户详情（等级、配额、封禁状态）\n"
        "  /set_level <用户ID> <free|basic|premium> — 设置用户等级\n"
        "  /set_quota <用户ID> <日配额> — 设置用户解码配额\n"
        "  /set_external_quota <用户ID> <配额> — 设置外部码配额\n"
        "  /ban <用户ID> — 封禁用户\n"
        "  /unban <用户ID> — 解封用户\n\n"
        "文件管理\n"
        "  /files [页码] — 文件列表\n"
        "  /file <文件ID> — 查看文件详情\n"
        "  /delete_file <文件ID> — 删除文件\n"
        "  /purge_channel <频道ID> — 清理频道所有文件\n\n"
        "中继管理（外部码解码用）\n"
        "  /relay_set_api <手机号> — 配置中继账号（API从.env自动读取）\n"
        "  /relay_code <验证码> — 提交中继验证码\n"
        "  /relay_pending — 查看待处理的中继请求\n"
        "  /relay_list — 查看中继实例列表\n"
        "  /relay_add <手机号> — 添加中继实例\n"
        "  /relay_remove <手机号> — 移除中继实例\n"
        "  /relay_reset_stats — 重置中继统计\n\n"
        "系统配置\n"
        "  /settings — 查看全部配置\n"
        "  /set_storage_channel <频道ID> — 主存储频道（需重启）\n"
        "  /set_file_prefix <前缀> — 文件码前缀（需重启）\n"
        "  /set_force_join <频道ID> <链接> — 强制加群（热更新）\n"
        "  /set_username <upload|decoder|sender> <@用户名> — 机器人用户名（热更新）\n"
        "  /set_quota_default <free|basic|premium> <配额> — 默认日配额（热更新）\n"
        "  /set_r2 <账号ID> <AccessKey> <SecretKey> — R2备份配置（需重启）\n"
        "  /set_db_backup <间隔分钟> <on|off> — 数据库自动备份（热更新）\n"
        "  /factory_reset — 恢复出厂设置\n\n"
        "文件码路由（第三方机器人迁移用）\n"
        "  /add_code_route <前缀> <机器人用户名> — 添加路由规则\n"
        "  /remove_code_route <前缀> — 删除路由规则\n"
        "  /code_routes — 查看路由表\n\n"
        "Bot 限流（解码间隔控制）\n"
        "  /set_bot_interval <机器人用户名> <秒数> — 设置解码间隔\n"
        "  /remove_bot_interval <机器人用户名> — 删除解码间隔\n"
        "  /bot_intervals — 查看限流配置\n\n"
        "环形拓扑\n"
        "  /topology — 查看环形拓扑结构\n"
        "  /spare_add <频道ID> — 添加备用频道\n"
        "  /spare_remove <频道ID> — 移除备用频道\n"
        "  /spare_list — 查看备用池\n"
        "  /rotation_set <参数> <值> — 设置轮转参数\n"
        "  /rotation_view — 查看轮转配置\n\n"
        "解码日志\n"
        "  /logs [页码] — 查看解码日志\n\n"
        "使用 /cancel 取消当前交互操作\n"
        "点击按钮操作更便捷，推荐使用菜单面板。"
    )
    await update.message.reply_text(msg)