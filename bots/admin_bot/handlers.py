import datetime

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from database import (
    get_users_col, get_file_records_col, get_file_record_cached, get_config, set_config,
    get_all_code_bot_routes, set_code_bot_route, delete_code_bot_route,
    get_all_bot_decode_intervals, set_bot_decode_interval, delete_bot_decode_interval,
    add_spare_channel, remove_spare, list_spare_pool,
    get_rotation_config, set_rotation_config,
    update_user_and_invalidate,
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
    await _ensure_user(user_id)

    update_doc = {
        "$set": {
            "membership_level": level,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
    # 使 SQLite 配额缓存失效，下次解码时从 CRDB 重新加载
    from database.cache_store import invalidate_user_quota_cache
    await invalidate_user_quota_cache(user_id)
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
    await _ensure_user(user_id)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
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
    await _ensure_user(user_id)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
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
    await _ensure_user(user_id)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
    )
    await update_user_and_invalidate(user_id)
    from database.cache_store import invalidate_user_quota_cache
    await invalidate_user_quota_cache(user_id)
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
    await _ensure_user(user_id)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
    )
    await update_user_and_invalidate(user_id)
    from database.cache_store import invalidate_user_quota_cache
    await invalidate_user_quota_cache(user_id)
    await update.message.reply_text(f"✅ 用户 {user_id} 外部码配额已设为 {_quota_display(quota)}")


@_auth_required
async def file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("用法:/file <文件码>")
        return
    file_code = args[0]

    # A2: 走缓存,避免每次直查 CRDB
    record = await get_file_record_cached(file_code)
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
            import re
            query["file_code"] = {"$regex": re.escape(search), "$options": "i"}

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
async def set_access_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置取件码访问次数上限（0=不限制）

    同时更新 file_records.max_requests(CRDB)和本地缓存。
    """
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法:/set_access_limit <文件码> <最大访问次数(0=不限制)>")
        return
    file_code = args[0]
    try:
        max_requests = int(args[1])
        if max_requests < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ 最大访问次数必须是非负整数（0=不限制）")
        return

    # 同时更新 CRDB 和本地缓存（update_file_record_and_invalidate 双写）
    from database import update_file_record_and_invalidate
    try:
        await update_file_record_and_invalidate(
            file_code, {"$set": {"max_requests": max_requests}}
        )
    except Exception as e:
        logger.error(f"[Admin] set_access_limit 失败 code={file_code}: {e}")
        await update.message.reply_text(f"❌ 设置失败: {e}")
        return

    limit_text = f"{max_requests} 次" if max_requests > 0 else "不限制"
    await update.message.reply_text(f"✅ 文件码 {file_code} 访问次数限制已设为 {limit_text}")


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
    if len(args) < 2:
        await update.message.reply_text(
            "用法:/relay_code <手机号> <验证码>\n\n"
            "用于解码机器人登录 Telegram 用户账号时提交验证码。\n"
            "验证码(6位)会发送到该账号已登录的 Telegram 客户端。"
        )
        return
    phone = args[0].strip()
    # 手机号规范化:确保以 + 开头
    if not phone.startswith("+"):
        phone = "+" + phone
    code = args[1].strip()
    if not code.isdigit() or len(code) not in (5, 6):
        await update.message.reply_text("❌ 验证码格式不正确,应为 5-6 位数字")
        return

    # P1-13:回执中对登录码掩码(复用 _mask_secret),避免明文泄露到聊天记录。
    # 注意:验证码仍写入 config 存储,因为解码机器人(relay 实例,运行在 mon 进程)
    # 需要通过 config 跨进程握手拿到明文以完成 Telethon sign_in——这是当前架构下
    # 必需的跨进程传递方式(详见本次整改报告的设计选择说明)。明文在 relay 实例读取后即被清空
    #(_wait_for_admin_code 读取后立即 set_config(..., "")),滞留窗口仅数秒至至多 5 分钟握手超时。
    await set_config(f"relay_auth_code:{phone}", code)
    await update.message.reply_text(
        f"✅ 验证码已提交至 {phone}\n"
        f"🔑 验证码: `{_mask_secret(code)}`\n"
        f"解码机器人将在几秒内自动获取并使用。\n\n"
        f"🔐 出于安全考虑,回执仅显示掩码,登录码明文不会留存于聊天记录。"
    )


@_auth_required
async def relay_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """提交中继账号二步验证密码。

    用法:
      /relay_password <手机号> <密码>
      /relay_password <密码>  (从 pending 状态自动检测手机号)
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/relay_password <手机号> <二步验证密码>\n\n"
            "用于中继账号开启了二步验证(Two-Step Verification)时提交密码。\n"
            "如果只有一个账号在等待密码,可省略手机号:\n"
            "  /relay_password <密码>\n\n"
            "⚠️ 密码明文不会留存于聊天记录(提交后立即清除)。"
        )
        return

    # 判断是否省略了手机号(只有一个参数=密码,两个参数=手机号+密码)
    if len(args) == 1:
        password = args[0].strip()
        phone = ""
        # 从 pending 状态自动检测手机号
        try:
            from services.relay_pool import relay_pool
            from database import get_config as _get_cfg
            if relay_pool._initialized:
                for inst in relay_pool.instances:
                    if await _get_cfg(f"relay_password_pending:{inst.phone}") == "1":
                        phone = inst.phone
                        break
        except Exception:
            pass
        if not phone:
            await update.message.reply_text("❌ 无法确定中继账号,请使用 /relay_password <手机号> <密码> 提交")
            return
    else:
        phone = args[0].strip()
        # 手机号规范化:确保以 + 开头
        if not phone.startswith("+"):
            phone = "+" + phone
        password = " ".join(args[1:]).strip()

    if not password:
        await update.message.reply_text("❌ 密码不能为空")
        return

    from database import set_config
    await set_config(f"relay_auth_password:{phone}", password)
    await update.message.reply_text(
        f"✅ 二步验证密码已提交至 {phone[:3]}****{phone[-2:] if len(phone) > 5 else '***'}\n"
        f"🔐 出于安全考虑,密码明文将在使用后立即清除。"
    )


@_auth_required
async def relay_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看所有中继账号的验证码等待状态。"""
    from database.session import get_collection
    col = get_collection("relay_accounts")
    accounts = await col.find({}, limit=20)
    pending_phones = []
    for acct in accounts:
        phone = acct.get("phone", "")
        if phone:
            pending = await get_config(f"relay_auth_pending:{phone}")
            if pending == "1":
                pending_phones.append(phone)
    if pending_phones:
        phones_str = "\n".join(f"  • {p}" for p in pending_phones)
        await update.message.reply_text(
            f"⏳ 以下中继账号正在等待验证码：\n{phones_str}\n\n"
            "请使用 /relay_code <手机号> <验证码> 提交验证码。"
        )
    else:
        await update.message.reply_text(
            "✅ 当前没有中继账号在等待验证码。\n\n"
            "如需添加中继账号，请使用 /relay_add <手机号>。"
        )


@_auth_required
async def relay_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有中继账号及使用统计"""
    from services.relay_pool import relay_pool
    if not relay_pool._initialized:
        try:
            await relay_pool.init()
        except Exception as e:
            logger.warning(f"[Admin] 中继池初始化失败: {e}")
            pass
    pool_status = await relay_pool.get_pool_status()
    if not pool_status:
        await update.message.reply_text(
            "⚠️ 中继账号池为空\n"
            "请使用 /relay_add 添加中继账号,或通过管理面板配置。"
        )
        return
    msg = f"🔐 中继账号池 ({len(pool_status)} 个账号)\n\n"
    STATUS_ICON = {"online": "✅", "banned": "❌", "floodwait": "⏳", "offline": "❌",
                   "connecting": "🔄", "pending_auth": "⏳", "pending_password": "⏳", "unknown": "⚪"}
    for i, ps in enumerate(pool_status, 1):
        status = ps.get("status", "unknown")
        icon = STATUS_ICON.get(status, "⚪")
        phone = ps["phone"]
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        info = ps.get("status_info", "")
        msg += f"{i}. {icon} {masked}\n"
        msg += f"   状态: {status}"
        if info:
            msg += f" — {info}"
        msg += "\n"
        msg += f"   今日: {ps['today_requests']} | 累计: {ps['total_requests']} | 均耗: {ps['avg_wait_ms']:.0f}ms\n\n"
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
        await relay_pool.add_account(api_id, api_hash, phone)
        # C3: 通知 idx_bot 进程的 relay_pool 增量同步新账号
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception as notify_err:
            logger.warning(f"[Admin] notify_relay_change 失败(非致命): {notify_err}")
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await update.message.reply_text(
            f"✅ 中继账号已添加到池中\n"
            f"  API_ID: {str(api_id)[:4]}...\n"
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
        # C3: 通知 idx_bot 进程的 relay_pool 移除该账号
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception as notify_err:
            logger.warning(f"[Admin] notify_relay_change 失败(非致命): {notify_err}")
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


def _mask_secret(secret: str) -> str:
    """掩码密钥/验证码,避免明文泄露到聊天记录(P1-13)。

    显示首尾少量字符,中间以 **** 替代。短于等于 8 位的整体打码,避免泄露过多。
    """
    secret = secret or ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}****{secret[-4:]}"


@_auth_required
async def set_r2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("用法:/set_r2 <账号ID> <AccessKey> <SecretKey> [桶名]")
        return
    await set_config("r2_account_id", args[0])
    await set_config("r2_access_key", args[1])
    # P2-4: R2 Secret Key 比照 relay api_hash 做 Fernet 加密存储
    from database.relay_db import encrypt as _encrypt_secret
    await set_config("r2_secret_key", _encrypt_secret(args[2]))
    if len(args) >= 4:
        await set_config("r2_bucket", args[3])

    # P1-13: 回显时掩码密钥,避免明文泄露到聊天记录
    await update.message.reply_text(
        "✅ R2 配置已保存\n"
        f"🔑 AccessKey: {_mask_secret(args[1])}\n"
        f"🔒 SecretKey: {_mask_secret(args[2])}\n"
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
    "users", "backup_config", "codes", "external_code_mapping",
    "jobs", "spare_pool",
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
            "• ⚙️ backup_config(系统配置)\n"
            "• 🔑 codes(文件码)\n"
            "• 🔗 external_code_mapping(外部码映射)\n"
            "• 📨 jobs(派工队列)\n"
            "• 🔄 spare_pool(备用池)\n\n"
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
    try:
        # 使用事务保证原子性：任一表失败则整体回滚，避免部分清空的不一致状态
        await client.execute("BEGIN")
        for table in _FACTORY_RESET_TABLES:
            try:
                sql = f"DELETE FROM {table}"
                await client.execute(sql)
                cleared.append(table)
            except Exception as e:
                errors.append(f"{table}: {e}")
                logger.error(f"[factory_reset] 清空 {table} 失败: {e}")
                # 任一表失败则回滚整个事务
                await client.execute("ROLLBACK")
                cleared = []  # 清空已记录的"成功"列表，因为已回滚
                break
        else:
            await client.execute("COMMIT")
    finally:
        # 确保连接一定关闭,避免连接泄漏
        try:
            await client.close()
        except Exception as e:
            logger.warning(f"[factory_reset] client.close 异常: {e}")

    # 同步清空本地 SQLite 缓存，防止脏行回写重新推回 CRDB
    sqlite_cleared = []
    try:
        import aiosqlite
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent / "data" / "cache_store.db"
        async with aiosqlite.connect(str(db_path)) as local_db:
            local_tables = [
                "cells_local", "file_records_local", "users_local",
                "codes_local", "local_job_queue", "external_code_mapping_local",
                # 关键:防止后台 flush 任务将残留数据回写 CRDB
                "decode_log_buffer", "code_changes",
                # 其他缓存表
                "user_quota", "heartbeat_local", "bot_heartbeat",
                "cells_snapshot", "cells_change_notify", "counter_snapshot",
                "pending_notify", "dsp_notify", "cache_backup", "kv_store",
            ]
            for tbl in local_tables:
                try:
                    await local_db.execute(f"DELETE FROM {tbl}")
                    sqlite_cleared.append(tbl)
                except Exception as e:
                    logger.warning(f"[Admin] 清空本地表 {tbl} 失败: {e}")
                    pass  # 表可能不存在，忽略
            await local_db.commit()
        if sqlite_cleared:
            logger.info(f"[factory_reset] 已清空本地缓存: {', '.join(sqlite_cleared)}")
    except Exception as e:
        logger.warning(f"[factory_reset] 清空本地缓存失败: {e}")

    # P1-14:失效内存缓存 + 拓扑缓存,确保 reset 后历史文件码/记录不可再解码,
    # 管理面板不再显示旧数据。直接调用各缓存的 clear/invalidate 公共方法,不戳私有属性。
    try:
        from database.cache import (
            get_code_cache, get_file_record_cache, get_user_cache,
            get_user_codes_cache, get_config_cache, clear_negative_caches,
        )
        from bots.admin_bot import display as _display
        from storage.delivery_resolver import invalidate_cell_cache
        from database.session import invalidate_cell_by_channel_cache

        # 1) 取件码 / 文件记录 / 用户 / 用户码列表 / 系统配置 内存缓存
        get_code_cache().clear()
        get_file_record_cache().clear()
        get_user_cache().clear()
        get_user_codes_cache().clear()
        get_config_cache().clear()
        clear_negative_caches()

        # 2) 管理面板 cells 缓存
        await _display.invalidate_cells_cache()

        # 3) 投递解析器拓扑缓存 + 按 channel 的 cell 进程内缓存
        invalidate_cell_cache()
        invalidate_cell_by_channel_cache()

        logger.info("[factory_reset] 已失效内存缓存与拓扑缓存,重置后历史数据不可再解码")
    except Exception as e:
        logger.warning(f"[factory_reset] 失效内存缓存失败: {e}")

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


# ─── C3: 频道槽位运行时增减 ──────────────────────────────────

@_auth_required
async def cell_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加频道槽位到环形拓扑(默认 shadow1 状态,不破坏现有 active 拓扑)"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法:/cell_add <slot_id> <channel_id> [account_name] [status]\n\n"
            "添加频道槽位到环形拓扑。\n"
            "• slot_id: 槽位标识(如 a3、s3a)\n"
            "• channel_id: 频道 ID(如 -1001234567890)\n"
            "• account_name: 账号名(可选)\n"
            "• status: 状态(可选,默认 shadow1,可选 active/shadow1/shadow2/r100)\n\n"
            "示例:\n"
            "/cell_add a3 -1001234567890 账号1\n"
            "/cell_add s3a -1001234567890 账号1 shadow1"
        )
        return
    slot_id = args[0].strip()
    try:
        channel_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ channel_id 必须是数字")
        return
    account_name = args[2] if len(args) > 2 else ""
    status = args[3] if len(args) > 3 else "shadow1"
    if status not in ("active", "shadow1", "shadow2", "r100"):
        await update.message.reply_text("❌ status 必须是 active/shadow1/shadow2/r100 之一")
        return
    from database.cache_store import get_cache_store
    store = get_cache_store()
    # 检查 slot_id 是否已存在
    existing = await store.get_all_cells_local()
    if any(c.get("slot_id") == slot_id for c in existing):
        await update.message.reply_text(f"❌ slot_id {slot_id} 已存在")
        return
    if any(c.get("channel_id") == channel_id for c in existing):
        await update.message.reply_text(f"❌ channel_id {channel_id} 已被其他槽位占用")
        return
    # 构造新 cell 记录
    import time as _time
    import datetime as _dt
    now_ts = _time.time()
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    new_cell = {
        "slot_id": slot_id,
        "channel_id": channel_id,
        "status": status,
        "next_active_chat_id": None,
        "prev_slot_id": None,
        "demoted_to_channel_id": None,
        "account_name": account_name,
        "is_r100": 1 if status == "r100" else 0,
        "last_heartbeat": None,
        "last_synced_msg_id": 0,
        "degrade_count": 0,
        "file_count": 0,
        "rotation_started_at": None,
        "updated_at": now_ts,
        "crdb_synced": 0,  # 标记为脏,mon_bot 会异步同步到 CRDB
    }
    try:
        await store.bulk_upsert_cells_local([new_cell])
        # 失效 admin_bot 自己的 cells 缓存
        from bots.admin_bot.display import invalidate_cells_cache
        await invalidate_cells_cache()
        await update.message.reply_text(
            f"✅ 已添加槽位\n"
            f"  slot_id: {slot_id}\n"
            f"  channel_id: {channel_id}\n"
            f"  account: {account_name or '(无)'}\n"
            f"  status: {status}\n\n"
            f"其他 bot 将在 5-60 秒内感知变更。\n"
            f"注意:新槽位的环形指针(next/prev)为空,如需接入环请用 /topology 查看。"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 添加失败: {e}")


@_auth_required
async def cell_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除频道槽位(拒绝移除 active 状态,需先降级)"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法:/cell_remove <slot_id>\n\n"
            "从环形拓扑移除频道槽位。\n"
            "• 自动修复环形链表指针(前驱/后继)\n"
            "• 拒绝移除 active 状态的槽位(请先等待轮转或手动降级)\n\n"
            "示例:/cell_remove s3a"
        )
        return
    slot_id = args[0].strip()
    from database.cache_store import get_cache_store
    store = get_cache_store()
    # 检查槽位是否存在及状态
    existing = await store.get_all_cells_local()
    target = None
    for c in existing:
        if c.get("slot_id") == slot_id:
            target = c
            break
    if not target:
        await update.message.reply_text(f"❌ slot_id {slot_id} 不存在")
        return
    if target.get("status") == "active":
        await update.message.reply_text(
            f"❌ 拒绝移除 active 状态的槽位 {slot_id}\n"
            f"请先等待轮转使其变为 shadow,或通过其他方式降级后再移除。"
        )
        return
    try:
        deleted = await store.delete_cell_local(slot_id)
        if deleted:
            # 失效 admin_bot 自己的 cells 缓存
            from bots.admin_bot.display import invalidate_cells_cache
            await invalidate_cells_cache()
            await update.message.reply_text(
                f"✅ 已移除槽位 {slot_id}\n"
                f"  channel_id: {target.get('channel_id')}\n"
                f"  环形链表指针已修复\n"
                f"其他 bot 将在 5-60 秒内感知变更。"
            )
        else:
            await update.message.reply_text(f"❌ 移除失败: slot_id {slot_id} 不存在")
    except Exception as e:
        await update.message.reply_text(f"❌ 移除失败: {e}")


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


# ─── 白名单热管理（中继账号 & 采集器账号） ──────────────────────────

@_auth_required
async def relay_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """中继账号白名单热管理
    用法:
      /relay_whitelist              — 查看当前白名单
      /relay_whitelist add <用户ID>  — 添加中继账号
      /relay_whitelist remove <用户ID> — 移除中继账号
      /relay_whitelist clear        — 清空白名单（回退到 .env 配置）
    """
    from database import (
        get_relay_whitelist, add_relay_whitelist, remove_relay_whitelist, delete_config,
    )
    args = context.args
    if not args:
        ids = await get_relay_whitelist()
        if not ids:
            await update.message.reply_text(
                "📋 中继账号白名单（当前为空）\n\n"
                "未配置白名单时，所有 EXTERNAL_RELAY/EXTERNAL_DONE 请求将被拒绝。\n"
                "使用 /relay_whitelist add <用户ID> 添加中继账号 ✅热更新"
            )
            return
        msg = "📋 中继账号白名单\n\n"
        for uid in sorted(ids):
            msg += f"  • `{uid}`\n"
        msg += f"\n共 {len(ids)} 个账号 ✅热更新\n"
        msg += "使用 /relay_whitelist add/remove <用户ID> 增删"
        await update.message.reply_text(msg)
        return

    action = args[0].lower()
    if action == "add":
        if len(args) < 2:
            await update.message.reply_text("用法:/relay_whitelist add <用户ID>")
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字")
            return
        added = await add_relay_whitelist(user_id)
        if added:
            await update.message.reply_text(f"✅ 中继账号 `{user_id}` 已添加到白名单 ✅热更新")
        else:
            await update.message.reply_text(f"⚠️ 中继账号 `{user_id}` 已在白名单中")
    elif action == "remove":
        if len(args) < 2:
            await update.message.reply_text("用法:/relay_whitelist remove <用户ID>")
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字")
            return
        removed = await remove_relay_whitelist(user_id)
        if removed:
            await update.message.reply_text(f"✅ 中继账号 `{user_id}` 已从白名单移除 ✅热更新")
        else:
            await update.message.reply_text(f"⚠️ 中继账号 `{user_id}` 不在白名单中")
    elif action == "clear":
        await delete_config("relay_account_ids")
        await update.message.reply_text("✅ 中继账号白名单已清空（回退到 .env 配置） ✅热更新")
    else:
        await update.message.reply_text(
            "用法:\n"
            "  /relay_whitelist              — 查看白名单\n"
            "  /relay_whitelist add <用户ID>  — 添加\n"
            "  /relay_whitelist remove <用户ID> — 移除\n"
            "  /relay_whitelist clear        — 清空"
        )


@_auth_required
async def collector_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """采集器账号白名单热管理
    用法:
      /collector_whitelist              — 查看当前白名单
      /collector_whitelist add <用户ID>  — 添加采集器账号
      /collector_whitelist remove <用户ID> — 移除采集器账号
      /collector_whitelist clear        — 清空白名单（回退到 .env 配置）
    """
    from database import (
        get_collector_whitelist, add_collector_whitelist, remove_collector_whitelist, delete_config,
    )
    args = context.args
    if not args:
        ids = await get_collector_whitelist()
        if not ids:
            await update.message.reply_text(
                "📋 采集器账号白名单（当前为空）\n\n"
                "未配置白名单时，外挂采集器推送将被拒绝。\n"
                "使用 /collector_whitelist add <用户ID> 添加采集器账号 ✅热更新"
            )
            return
        msg = "📋 采集器账号白名单\n\n"
        for uid in sorted(ids):
            msg += f"  • `{uid}`\n"
        msg += f"\n共 {len(ids)} 个账号 ✅热更新\n"
        msg += "使用 /collector_whitelist add/remove <用户ID> 增删"
        await update.message.reply_text(msg)
        return

    action = args[0].lower()
    if action == "add":
        if len(args) < 2:
            await update.message.reply_text("用法:/collector_whitelist add <用户ID>")
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字")
            return
        added = await add_collector_whitelist(user_id)
        if added:
            await update.message.reply_text(f"✅ 采集器账号 `{user_id}` 已添加到白名单 ✅热更新")
        else:
            await update.message.reply_text(f"⚠️ 采集器账号 `{user_id}` 已在白名单中")
    elif action == "remove":
        if len(args) < 2:
            await update.message.reply_text("用法:/collector_whitelist remove <用户ID>")
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字")
            return
        removed = await remove_collector_whitelist(user_id)
        if removed:
            await update.message.reply_text(f"✅ 采集器账号 `{user_id}` 已从白名单移除 ✅热更新")
        else:
            await update.message.reply_text(f"⚠️ 采集器账号 `{user_id}` 不在白名单中")
    elif action == "clear":
        await delete_config("collector_account_ids")
        await update.message.reply_text("✅ 采集器账号白名单已清空（回退到 .env 配置） ✅热更新")
    else:
        await update.message.reply_text(
            "用法:\n"
            "  /collector_whitelist              — 查看白名单\n"
            "  /collector_whitelist add <用户ID>  — 添加\n"
            "  /collector_whitelist remove <用户ID> — 移除\n"
            "  /collector_whitelist clear        — 清空"
        )


# ─── 拓扑查看 ──────────────────────────────────────────────────

@_auth_required
async def topology(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _get_topology_text()
    await update.message.reply_text(text)


@_auth_required
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("conv_state"):
        await _conv_end(context)
        await update.message.reply_text("❌ 操作已取消。")
    else:
        await update.message.reply_text("当前没有正在进行的操作。")


# ─── 帮助命令 ──────────────────────────────────────────────────

@_auth_required
async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """数据库恢复命令。

    用法:
      /restore                  — 列出可用备份(最新在前,显示序号)
      /restore <序号>           — 显示确认按钮,点击后全量覆盖恢复
      /restore <序号> table:xxx,yyy — 只恢复指定表(逗号分隔)
      /restore <序号> merge:yes — 增量补充(冲突保留现有,不删现有数据)
    table: 和 merge: 可组合使用,顺序不限。
    """
    args = context.args

    # 无参数:列出备份
    if not args:
        await _restore_list_backups(update, context)
        return

    # 解析序号
    try:
        seq = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ 序号必须是数字。使用 /restore 查看备份列表。")
        return

    # 解析可选参数 table: 和 merge:
    tables = None
    merge = False
    for arg in args[1:]:
        if arg.startswith("table:"):
            tables_str = arg[len("table:"):]
            tables = [t.strip() for t in tables_str.split(",") if t.strip()]
            if not tables:
                await update.message.reply_text("❌ table: 后必须指定表名(逗号分隔)")
                return
        elif arg.startswith("merge:"):
            merge_val = arg[len("merge:"):].lower()
            merge = merge_val in ("yes", "1", "true", "on")

    # 查备份列表,取出对应序号的 key
    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await update.message.reply_text(f"❌ 读取备份列表失败: {e}")
        return

    if not backups:
        await update.message.reply_text("📭 R2 中没有可用的备份")
        return

    if seq < 1 or seq > len(backups):
        await update.message.reply_text(f"❌ 序号超出范围(1-{len(backups)})。使用 /restore 查看列表。")
        return

    target = backups[seq - 1]
    key = target.get("key", "")
    size = target.get("size", 0)
    last_mod = target.get("last_modified", "")

    # 构造确认按钮
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    cb_data = f"restore:confirm|{seq}|{'1' if merge else '0'}"
    if tables:
        cb_data += f"|table:{','.join(tables)}"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认恢复", callback_data=cb_data),
        ],
        [
            InlineKeyboardButton("❌ 取消", callback_data="restore:cancel"),
        ],
    ])

    scope_text = f"仅恢复表: {', '.join(tables)}" if tables else "全量恢复(所有表)"
    if merge:
        mode_text = "增量补充(冲突保留现有数据,不删除任何记录)"
    else:
        mode_text = "覆盖恢复(先清空目标表,再写入备份数据)"
    await update.message.reply_text(
        "⚠️ 数据库恢复确认\n\n"
        f"📁 备份文件: `{key}`\n"
        f"📏 大小: {size} 字节\n"
        f"🕒 备份时间: {last_mod}\n"
        f"📋 恢复范围: {scope_text}\n"
        f"🔧 恢复模式: {mode_text}\n\n"
        + ("" if merge else "🔴 警告: 覆盖模式会先 TRUNCATE 清空目标表!\n")
        + "🔴 建议先停止相关 Bot 服务再恢复。\n\n"
        "点击下方按钮确认或取消:",
        reply_markup=kb,
    )


async def _restore_list_backups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出 R2 中的备份文件。"""
    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await update.message.reply_text(f"❌ 读取备份列表失败: {e}")
        return

    if not backups:
        await update.message.reply_text("📭 R2 中没有可用的备份")
        return

    lines = [f"📦 可用备份(共 {len(backups)} 份,最新在前)\n"]
    lines.append("序号  大小      备份时间                 文件名")
    lines.append("─" * 70)
    # 只显示最近 20 条,避免消息过长
    show_count = min(len(backups), 20)
    for i in range(show_count):
        b = backups[i]
        key = b.get("key", "")
        size = b.get("size", 0)
        last_mod = b.get("last_modified", "")[:19]
        # 从 key 提取短文件名
        short_name = key.split("/")[-1] if "/" in key else key
        lines.append(f" {i+1:3d}  {size:>7}  {last_mod}  {short_name}")

    if len(backups) > 20:
        lines.append(f"\n(仅显示最近 20 份,共 {len(backups)} 份)")

    lines.append("\n用法:")
    lines.append("  /restore <序号>                   — 全量覆盖恢复")
    lines.append("  /restore <序号> table:xxx,yyy     — 仅恢复指定表")
    lines.append("  /restore <序号> merge:yes         — 增量补充(冲突保留现有,不删现有数据)")
    lines.append("  table: 与 merge:yes 可组合使用,顺序不限")

    await update.message.reply_text("\n".join(lines))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 管理员命令手册\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 系统状态\n"
        "  /status — 系统概览(进程状态、各Bot状态)\n"
        "  /health — 健康检查(频道状态、降级情况)\n"
        "  /topology — 查看环形拓扑结构\n\n"
        "👤 用户管理\n"
        "  /users [关键词] [页码] — 用户列表\n"
        "  /user <用户ID> — 查看用户详情\n"
        "  /set_level <用户ID> <1|2|3> — 设置等级(1=免费 2=基础 3=高级)\n"
        "  /set_quota <用户ID> <日配额> — 解码配额(-1=不限 0=禁止)\n"
        "  /set_external_quota <用户ID> <配额> — 外部码配额(-1=不限 0=禁止)\n"
        "  /ban <用户ID> — 封禁用户\n"
        "  /unban <用户ID> — 解封用户\n\n"
        "📁 文件管理\n"
        "  /files [页码] — 文件列表\n"
        "  /file <文件码> — 查看文件详情\n"
        "  /delete_file <文件码> — 删除文件\n"
        "  /set_access_limit <文件码> <次数> — 访问次数限制(0=不限)\n"
        "  /purge_channel <频道ID> — 清理频道(需手动操作)\n\n"
        "🔐 中继管理(外部码解码用)\n"
        "  /relay_add <手机号> — 添加中继账号(交互式登录:手机号→验证码→密码)\n"
        "  /relay_code <手机号> <验证码> — 提交登录验证码(旧账号重新登录用)\n"
        "  /relay_password <手机号> <密码> — 提交二步验证密码(旧账号重新登录用)\n"
        "  /relay_list — 查看中继账号列表\n"
        "  /relay_pending — 查看待处理验证码\n"
        "  /relay_remove <手机号> — 移除中继账号\n"
        "  /relay_reset_stats — 重置使用统计\n"
        "  /relay_whitelist [add|remove|clear] [用户ID] — 中继白名单(热更新)\n"
        "  /collector_whitelist [add|remove|clear] [用户ID] — 采集器白名单(热更新)\n\n"
        "⚙️ 系统配置\n"
        "  /settings — 查看全部配置\n"
        "  /set_storage_channel <频道ID> — 主存储频道(需重启)\n"
        "  /set_file_prefix <前缀> — 文件码前缀(需重启)\n"
        "  /set_force_join <频道ID> [链接] — 强制加群(热更新)\n"
        "  /set_username <upload|decoder|sender> <@用户名> — Bot用户名(热更新)\n"
        "  /set_quota_default <1|2|3> <配额> [外部码配额] — 默认配额(热更新)\n"
        "  /set_r2 <账号ID> <AccessKey> <SecretKey> [桶名] — R2备份(需重启)\n"
        "  /set_db_backup <间隔分钟> <on|off> — 自动备份(热更新)\n"
        "  /restore [序号] [table:xxx,yyy] [merge:yes] — 数据库恢复\n"
        "  /factory_reset — 恢复出厂设置(危险)\n\n"
        "🗺️ 文件码路由(第三方机器人迁移用)\n"
        "  /add_code_route <前缀> <机器人用户名> — 添加路由\n"
        "  /remove_code_route <前缀> — 删除路由\n"
        "  /code_routes — 查看路由表\n\n"
        "⏱️ Bot限流(解码间隔控制)\n"
        "  /set_bot_interval <机器人用户名> <秒数> — 设置间隔(0=取消)\n"
        "  /remove_bot_interval <机器人用户名> — 删除限流\n"
        "  /bot_intervals — 查看限流配置\n\n"
        "🔄 环形拓扑与轮转\n"
        "  /cell_add <slot_id> <channel_id> [账号名] [状态] — 添加槽位\n"
        "  /cell_remove <slot_id> — 移除槽位(拒绝active)\n"
        "  /spare_add <频道ID> [账号名] — 添加备用频道\n"
        "  /spare_remove <频道ID> — 移除备用频道\n"
        "  /spare_list — 查看备用池\n"
        "  /rotation_set <参数> <值> — 轮转参数\n"
        "  /rotation_view — 查看轮转配置\n\n"
        "📋 解码日志\n"
        "  /logs [页码] — 查看解码日志\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 /cancel 取消当前交互操作\n"
        "💡 推荐使用菜单面板(发送 /start 打开)"
    )
    await update.message.reply_text(msg)