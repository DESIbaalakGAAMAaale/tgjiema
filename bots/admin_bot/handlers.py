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
from services import (
    content_reports, approval_workflow, rbac, maintenance_mode,
    repair_console, disaster_recovery, ru_cost_center,
)
# R40 P1-8: 维护模式检查装饰器(应用于高风险入口)
from services.maintenance_mode import require_maintenance_check
# R44 6.2: i18n 国际化翻译(管理员可见错误文案)
from services.i18n import get_i18n_manager, translate as _i18n_t


def _t(user_id: int, key: str, **kwargs) -> str:
    """R44 6.2: 获取管理员 locale 并翻译 key(带插值)。

    Args:
        user_id: Telegram 管理员用户 ID(用于查询 locale 偏好)
        key: 翻译 key(如 "bot.admin_bot.usage_user_command")
        **kwargs: 插值参数

    Returns:
        本地化字符串
    """
    manager = get_i18n_manager()
    locale = manager.get_user_locale(user_id) if user_id else "zh-CN"
    return manager.format_message(key, locale=locale, **kwargs)


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
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(_t(admin_id, "bot.admin_bot.usage_user_command"))
        return
    try:
        user_id = int(args[0])
    except ValueError:
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(_t(admin_id, "bot.admin_bot.user_id_must_be_number"))
        return

    user = await _ensure_user(user_id)

    level = user.get("membership_level", "free")
    msg = (
        _i18n_t('bot.admin_bot.handlers.s1', user_get_user_id=user.get('user_id'), user_get_username_or_N_A=user.get('username') or 'N/A', user_get_first_name_or_N_A=user.get('first_name') or 'N/A', MEMBERSHIP_LEVELS_get_level_level=MEMBERSHIP_LEVELS.get(level, level), if_user_get_is_banned_else='是 ❌' if user.get('is_banned') else '否 ✅', if_user_get_can_upload_else='是 ✅' if user.get('can_upload') else '否 ❌', quota_display_user_get_daily_decode_quota=_quota_display(user.get('daily_decode_quota')), user_get_quota_used_today_0=user.get('quota_used_today', 0), quota_display_user_get_external_decode_quota=_quota_display(user.get('external_decode_quota')), user_get_external_used_today_0=user.get('external_used_today', 0), format_datetime_user_get_created_at=format_datetime(user.get('created_at')), format_datetime_user_get_updated_at=format_datetime(user.get('updated_at')))
    )
    await update.message.reply_text(msg)


@_auth_required
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    search = ""
    page = 1
    for arg in args:
        if arg.isdigit():
            page = max(1, int(arg))
        else:
            search = arg
    await update.message.reply_text(await _get_users_page_text(search, page))


@_auth_required
async def set_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s97'))
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s176'))
        return
    level = LEVEL_ALIAS.get(args[1].lower())
    if not level:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s98'))
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
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s53', user_id=user_id, MEMBERSHIP_LEVELS_level=MEMBERSHIP_LEVELS[level]))


@_auth_required
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R41 P1-8: /ban 快捷命令 — 走 CommandBus 强制 RBAC + 审批门禁。

    复用 make_ban_user_command(永久封禁),与 /ban_user 命令逻辑一致。
    """
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s99'))
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s177'))
        return
    reason = args[1] if len(args) > 1 else ""

    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_ban_user_command,
    )
    admin = update.effective_user
    admin_id = admin.id if admin else 0
    admin_name = admin.username if admin else ""
    cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
    command = make_ban_user_command(
        user_id=user_id, reason=reason, duration_days=0,
    )
    bus = CommandBus()
    cb_result = await bus.execute(command, cb_principal)

    if cb_result.approval_required:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s100', cb_result_approval_id=cb_result.approval_id, user_id=user_id)
        )
    elif cb_result.success:
        ok = cb_result.data.get("ban_ok", False) if cb_result.data else False
        if ok:
            admin_id = update.effective_user.id if update.effective_user else 0
            await update.message.reply_text(
                _t(admin_id, "bot.admin_bot.ban_success_permanent", user_id=user_id)
            )
        else:
            admin_id = update.effective_user.id if update.effective_user else 0
            await update.message.reply_text(_t(admin_id, "bot.admin_bot.ban_failed_retry"))
    else:
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(
            _t(admin_id, "bot.admin_bot.ban_failed_with_error", error=cb_result.error)
        )


@_auth_required
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R41 P1-8: /unban 快捷命令 — 走 CommandBus 强制 RBAC(不需审批)。"""
    args = context.args
    if not args:
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(_t(admin_id, "bot.admin_bot.usage_unban_command"))
        return
    try:
        user_id = int(args[0])
    except ValueError:
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(_t(admin_id, "bot.admin_bot.user_id_must_be_number"))
        return

    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_unban_user_command,
    )
    admin = update.effective_user
    admin_id = admin.id if admin else 0
    admin_name = admin.username if admin else ""
    cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
    command = make_unban_user_command(user_id=user_id)
    bus = CommandBus()
    cb_result = await bus.execute(command, cb_principal)

    if cb_result.success:
        ok = cb_result.data.get("unban_ok", False) if cb_result.data else False
        if ok:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s178', user_id=user_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s179'))
    else:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s101', cb_result_error=cb_result.error))


@_auth_required
async def set_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s102'))
        return
    try:
        user_id = int(args[0])
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s180'))
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
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s54', user_id=user_id, quota_display_quota=_quota_display(quota)))


@_auth_required
async def set_external_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s103'))
        return
    try:
        user_id = int(args[0])
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s181'))
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
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s55', user_id=user_id, quota_display_quota=_quota_display(quota)))


@_auth_required
async def file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s104'))
        return
    file_code = args[0]

    # A2: 走缓存,避免每次直查 CRDB
    record = await get_file_record_cached(file_code)
    if record is None:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s105', file_code=file_code))
        return

    file_types = record.get("file_types", {})
    if isinstance(file_types, str):
        import json
        file_types = json.loads(file_types) if file_types else {}
    type_desc = " ".join(_i18n_t('bot.admin_bot.handlers.s106', v=v, k=k) for k, v in sorted(file_types.items())) if file_types else _i18n_t('bot.admin_bot.handlers.s16')

    backups = record.get("backup_channel_msg_ids", [])
    if isinstance(backups, str):
        import json
        backups = json.loads(backups) if backups else []

    msg = (
        _i18n_t('bot.admin_bot.handlers.s2', file_code=file_code, record_get_uploader_id=record.get('uploader_id'), type_desc=type_desc, record_get_status_active=record.get('status', 'active'), record_get_request_count_0=record.get('request_count', 0), format_datetime_record_get_create_time=format_datetime(record.get('create_time')), record_get_primary_channel_id=record.get('primary_channel_id'), len_backups=len(backups))
    )
    note = record.get("note", "")
    if note:
        msg += _i18n_t('bot.admin_bot.handlers.s17', note=note)
    await update.message.reply_text(msg)


@_auth_required
async def files_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    search = ""
    page = 1
    for arg in args:
        if arg.isdigit():
            page = max(1, int(arg))
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
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    skip = (page - 1) * per_page
    files = await files_col.find(query, sort=("create_time", -1), skip=skip, limit=per_page)

    msg = _i18n_t('bot.admin_bot.handlers.s3', page=page, total_pages=total_pages, total=total)
    if search:
        msg += _i18n_t('bot.admin_bot.handlers.s18', search=search)
    msg += "\n"

    for f in files:
        status_icon = "✅" if f.get("status") == "active" else "🗑️"
        fc = f.get("file_code", "N/A")
        uploader = f.get("uploader_id", "?")
        msg += _i18n_t('bot.admin_bot.handlers.s19', status_icon=status_icon, fc=fc, uploader=uploader)

    if total_pages > 1 and page < total_pages:
        ns = f" {search}" if search else ""
        msg += _i18n_t('bot.admin_bot.handlers.s20', ns=ns, page_1=page + 1)

    await update.message.reply_text(msg)


@_auth_required
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s107'))
        return
    file_code = args[0]

    # P2-8: 二次确认,避免误删。先校验文件存在,再弹出确认按钮,实际删除在 callback 中执行。
    record = await get_file_record_cached(file_code)
    if record is None:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s108', file_code=file_code))
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_i18n_t('bot.admin_bot.handlers.s182'), callback_data=f"delfile|{file_code}")],
        [InlineKeyboardButton(_i18n_t('bot.admin_bot.handlers.s183'), callback_data=f"delfile_cancel|{file_code}")],
    ])
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s56', file_code=file_code, record_get_uploader_id=record.get('uploader_id'), record_get_status_active=record.get('status', 'active')),
        reply_markup=kb,
    )


@_auth_required
async def set_access_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置取件码访问次数上限（0=不限制）

    同时更新 file_records.max_requests(CRDB)和本地缓存。
    """
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s109'))
        return
    file_code = args[0]
    try:
        max_requests = int(args[1])
        if max_requests < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s184'))
        return

    # 同时更新 CRDB 和本地缓存（update_file_record_and_invalidate 双写）
    from database import update_file_record_and_invalidate
    try:
        await update_file_record_and_invalidate(
            file_code, {"$set": {"max_requests": max_requests}}
        )
    except Exception as e:
        logger.error(f"[Admin] set_access_limit 失败 code={file_code}: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s277', error=e))
        return

    limit_text = _i18n_t('bot.admin_bot.handlers.s21', max_requests=max_requests) if max_requests > 0 else _i18n_t('bot.admin_bot.handlers.s22')
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s57', file_code=file_code, limit_text=limit_text))


@_auth_required
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    page = 1
    if args and args[0].isdigit():
        page = max(1, int(args[0]))
    await update.message.reply_text(await _get_logs_page_text(page))


@_auth_required
async def relay_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s110')
        )
        return
    phone = args[0].strip()
    # 手机号规范化:确保以 + 开头
    if not phone.startswith("+"):
        phone = "+" + phone
    code = args[1].strip()
    if not code.isdigit() or len(code) not in (5, 6):
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s111'))
        return

    # P1-13:回执中对登录码掩码(复用 _mask_secret),避免明文泄露到聊天记录。
    # 注意:验证码仍写入 config 存储,因为解码机器人(relay 实例,运行在 mon 进程)
    # 需要通过 config 跨进程握手拿到明文以完成 Telethon sign_in——这是当前架构下
    # 必需的跨进程传递方式(详见本次整改报告的设计选择说明)。明文在 relay 实例读取后即被清空
    #(_wait_for_admin_code 读取后立即 set_config(..., "")),滞留窗口仅数秒至至多 5 分钟握手超时。
    await set_config(f"relay_auth_code:{phone}", code)
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s58', phone=phone, mask_secret_code=_mask_secret(code))
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
            _i18n_t('bot.admin_bot.handlers.s112')
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
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s185'))
            return
    else:
        phone = args[0].strip()
        # 手机号规范化:确保以 + 开头
        if not phone.startswith("+"):
            phone = "+" + phone
        password = " ".join(args[1:]).strip()

    if not password:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s113'))
        return

    from database import set_config
    await set_config(f"relay_auth_password:{phone}", password)
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s59', phone_3=phone[:3], phone_2_if_len_phone_5_else=phone[-2:] if len(phone) > 5 else '***')
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
            _i18n_t('bot.admin_bot.handlers.s114', phones_str=phones_str)
        )
    else:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s115')
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
            _i18n_t('bot.admin_bot.handlers.s116')
        )
        return
    msg = _i18n_t('bot.admin_bot.handlers.s4', len_pool_status=len(pool_status))
    STATUS_ICON = {"online": "✅", "banned": "❌", "floodwait": "⏳", "offline": "❌",
                   "connecting": "🔄", "pending_auth": "⏳", "pending_password": "⏳", "unknown": "⚪"}
    for i, ps in enumerate(pool_status, 1):
        status = ps.get("status", "unknown")
        icon = STATUS_ICON.get(status, "⚪")
        phone = ps["phone"]
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        info = ps.get("status_info", "")
        msg += f"{i}. {icon} {masked}\n"
        msg += _i18n_t('bot.admin_bot.handlers.s23', status=status)
        if info:
            msg += f" — {info}"
        msg += "\n"
        msg += _i18n_t('bot.admin_bot.handlers.s24', ps_today_requests=ps['today_requests'], ps_total_requests=ps['total_requests'], ps_avg_wait_ms=ps['avg_wait_ms'])
    await update.message.reply_text(msg)


@_auth_required
async def relay_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加中继账号(命令行方式)"""
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s117')
        )
        return
    phone = args[0].strip()

    from services.relay_pool import relay_pool
    from config import settings
    api_id = settings.RELAY_API_ID
    api_hash = settings.RELAY_API_HASH
    if not api_id or not api_hash:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s118')
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
            _i18n_t('bot.admin_bot.handlers.s119', str_api_id_4=str(api_id)[:4], masked=masked)
        )
    except Exception as e:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s186', e=e))


@_auth_required
async def relay_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除中继账号"""
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s120')
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
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s278', phone=phone))
    else:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s121', phone=phone))


@_auth_required
async def relay_reset_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重置使用统计"""
    from database.relay_db import get_relay_db
    db = await get_relay_db()
    await db.reset_usage()
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s60'))


# ─── 系统配置管理 ────────────────────────────────────────────────


@_auth_required
async def settings_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await _get_configs_text()
        await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"[settings] 获取配置失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s279', error=e))


@_auth_required
async def set_file_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s122'))
        return
    prefix = args[0].strip()
    await set_config("file_code_prefix", prefix)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s61', prefix=prefix))


@_auth_required
async def set_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s123'))
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s187'))
        return
    await set_config("force_join_channel_id", str(channel_id))
    link = args[1] if len(args) > 1 else ""
    if link:
        await set_config("force_join_link", link)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s188', channel_id=channel_id) + (_i18n_t('bot.admin_bot.handlers.s231', link=link) if link else "") + _i18n_t('bot.admin_bot.handlers.s124'))


@_auth_required
async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s125'))
        return
    bot_role = args[0].lower()
    username = args[1].lstrip("@")
    key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
    key = key_map.get(bot_role)
    if not key:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s126'))
        return
    await set_config(key, username)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s62', bot_role=bot_role, username=username))


@_auth_required
async def set_quota_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s127'))
        return
    level = LEVEL_ALIAS.get(args[0].lower())
    if not level:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s128'))
        return
    try:
        quota = int(args[1])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s189'))
        return
    await set_config(f"quota_default_{level}", str(quota))
    msg = _i18n_t('bot.admin_bot.handlers.s5', level=level, quota_display_quota=_quota_display(quota))

    if len(args) >= 3:
        try:
            ext_quota = int(args[2])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s232'))
            return
        await set_config(f"quota_external_{level}", str(ext_quota))
        msg += _i18n_t('bot.admin_bot.handlers.s25', quota_display_ext_quota=_quota_display(ext_quota))

    msg += _i18n_t('bot.admin_bot.handlers.s6')
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
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s129'))
        return

    # R41 P1-8: R2 凭证变更属高风险操作,必须走 CommandBus(强制 RBAC + 审批门禁)
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_set_r2_command,
    )
    account_id = args[0]
    access_key = args[1]
    secret_key = args[2]
    bucket = args[3] if len(args) >= 4 else ""

    admin = update.effective_user
    admin_id = admin.id if admin else 0
    admin_name = admin.username if admin else ""
    cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
    command = make_set_r2_command(
        account_id=account_id, access_key=access_key,
        secret_key=secret_key, bucket=bucket,
    )
    bus = CommandBus()
    cb_result = await bus.execute(command, cb_principal)

    if cb_result.approval_required:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s130', cb_result_approval_id=cb_result.approval_id, mask_secret_access_key=_mask_secret(access_key), mask_secret_secret_key=_mask_secret(secret_key))
        )
        return
    if not cb_result.success:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s131', cb_result_error=cb_result.error))
        return

    # handler 已在审批通过后写入配置,此处仅回显
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s63', mask_secret_access_key=_mask_secret(access_key), mask_secret_secret_key=_mask_secret(secret_key))
    )


@_auth_required
async def set_db_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s132'))
        return
    try:
        interval = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s190'))
        return
    enabled = args[1].lower() in (_i18n_t('bot.admin_bot.handlers.s64'), "1", "true", "yes", "on")
    await set_config("db_backup_interval", str(interval))
    await set_config("db_backup_enabled", "true" if enabled else "false")
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s65', interval=interval, if_enabled_else='开启' if enabled else '关闭')
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
            _i18n_t('bot.admin_bot.handlers.s133')
        )
        return

    if args[0] != "confirm":
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s134'))
        return

    if len(args) < 2 or args[1] != "I_UNDERSTAND":
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s135')
        )
        return

    # R41 P1-8: 工厂重置属最高风险操作,必须走 CommandBus(强制 RBAC + 审批门禁)
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_factory_reset_command,
    )
    admin = update.effective_user
    admin_id = admin.id if admin else 0
    admin_name = admin.username if admin else ""
    cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
    command = make_factory_reset_command(tables=_FACTORY_RESET_TABLES)
    bus = CommandBus()
    cb_result = await bus.execute(command, cb_principal)

    if cb_result.approval_required:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s136', cb_result_approval_id=cb_result.approval_id, join_FACTORY_RESET_TABLES=', '.join(_FACTORY_RESET_TABLES))
        )
        return
    if not cb_result.success:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s137', cb_result_error=cb_result.error)
        )
        return

    msg = await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s66'))

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
            _i18n_t('bot.admin_bot.handlers.s233', len_cleared=len(cleared), join_cleared=', '.join(cleared)) + "\n".join(f"  • {e}" for e in errors) + _i18n_t('bot.admin_bot.handlers.s191')
        )
    else:
        await msg.edit_text(
            _i18n_t('bot.admin_bot.handlers.s138', len_cleared=len(cleared), join_cleared=', '.join(cleared))
        )


@_auth_required
async def purge_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s139'))
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s192'))
        return

    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s67', channel_id=channel_id)
    )


# ─── 文件码前缀路由管理 ──────────────────────────────────────────


@_auth_required
async def add_code_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s140')
        )
        return
    prefix = args[0].strip().lower()
    bot_username = args[1].strip().lower().lstrip("@")
    # 输入验证: 前缀只能包含字母数字和下划线,长度 1-50
    if not prefix or len(prefix) > 50 or not all(c.isalnum() or c == '_' for c in prefix):
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s141')
        )
        return
    # bot_username 只能包含字母、数字、下划线和 bot 后缀
    if not bot_username or len(bot_username) > 32 or not all(c.isalnum() or c == '_' for c in bot_username):
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s142')
        )
        return
    await set_code_bot_route(prefix, bot_username)
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s68', prefix=prefix, bot_username=bot_username, prefix_3=prefix, bot_username_5=bot_username)
    )


@_auth_required
async def remove_code_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s143')
        )
        return
    prefix = args[0].strip().lower()
    await delete_code_bot_route(prefix)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s69', prefix=prefix))


@_auth_required
async def list_code_routes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = await get_all_code_bot_routes()
    if not routes:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s144'))
        return
    msg = _i18n_t('bot.admin_bot.handlers.s7')
    for prefix in sorted(routes.keys()):
        msg += f"  • `{prefix}` → @{routes[prefix]}\n"
    msg += _i18n_t('bot.admin_bot.handlers.s8')
    await update.message.reply_text(msg)


# ─── Bot 解码间隔限流管理 ────────────────────────────────────────


@_auth_required
async def set_bot_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s145')
        )
        return
    bot_username = args[0].strip().lower().lstrip("@")
    try:
        interval = int(args[1])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s193'))
        return
    if interval < 0:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s146'))
        return
    await set_bot_decode_interval(bot_username, interval)
    if interval == 0:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s147', bot_username=bot_username))
    else:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s148', bot_username=bot_username, interval=interval))


@_auth_required
async def remove_bot_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s149')
        )
        return
    bot_username = args[0].strip().lower().lstrip("@")
    await delete_bot_decode_interval(bot_username)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s70', bot_username=bot_username))


@_auth_required
async def list_bot_intervals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    intervals = await get_all_bot_decode_intervals()
    if not intervals:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s150'))
        return
    msg = _i18n_t('bot.admin_bot.handlers.s9')
    for bot in sorted(intervals.keys()):
        msg += _i18n_t('bot.admin_bot.handlers.s26', bot=bot, intervals_bot=intervals[bot])
    msg += _i18n_t('bot.admin_bot.handlers.s10')
    await update.message.reply_text(msg)


# ─── 备用池管理 ──────────────────────────────────────────────────

@_auth_required
async def spare_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s151')
        )
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s194'))
        return
    account_name = args[1] if len(args) > 1 else None
    await add_spare_channel(channel_id, account_name)
    acc_info = _i18n_t('bot.admin_bot.handlers.s27', account_name=account_name) if account_name else _i18n_t('bot.admin_bot.handlers.s28')
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s71', channel_id=channel_id, acc_info=acc_info))


@_auth_required
async def spare_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s152'))
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s195'))
        return
    await remove_spare(channel_id)
    await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s72', channel_id=channel_id))


@_auth_required
async def spare_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spares = await list_spare_pool()
    if not spares:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s153'))
        return
    msg = _i18n_t('bot.admin_bot.handlers.s11')
    for s in spares:
        used = _i18n_t('bot.admin_bot.handlers.s73') if s.get("is_used") else _i18n_t('bot.admin_bot.handlers.s74')
        acc = s.get("account_name") or _i18n_t('bot.admin_bot.handlers.s75')
        msg += f"  {used} {s['channel_id']} — {acc}\n"
    msg += _i18n_t('bot.admin_bot.handlers.s12', len_spares=len(spares))
    await update.message.reply_text(msg)


# ─── C3: 频道槽位运行时增减 ──────────────────────────────────

@_auth_required
async def cell_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加频道槽位到环形拓扑(默认 shadow1 状态,不破坏现有 active 拓扑)"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s154')
        )
        return
    slot_id = args[0].strip()
    try:
        channel_id = int(args[1])
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s196'))
        return
    account_name = args[2] if len(args) > 2 else ""
    status = args[3] if len(args) > 3 else "shadow1"
    if status not in ("active", "shadow1", "shadow2", "r100"):
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s155'))
        return
    from database.cache_store import get_cache_store
    store = get_cache_store()
    # 检查 slot_id 是否已存在
    existing = await store.get_all_cells_local()
    if any(c.get("slot_id") == slot_id for c in existing):
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s156', slot_id=slot_id))
        return
    if any(c.get("channel_id") == channel_id for c in existing):
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s157', channel_id=channel_id))
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
            _i18n_t('bot.admin_bot.handlers.s158', slot_id=slot_id, channel_id=channel_id, account_name_or=account_name or '(无)', status=status)
        )
    except Exception as e:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s197', e=e))


@_auth_required
async def cell_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除频道槽位(拒绝移除 active 状态,需先降级)"""
    args = context.args
    if not args:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s159')
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
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s160', slot_id=slot_id))
        return
    if target.get("status") == "active":
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s161', slot_id=slot_id)
        )
        return
    try:
        deleted = await store.delete_cell_local(slot_id)
        if deleted:
            # 失效 admin_bot 自己的 cells 缓存
            from bots.admin_bot.display import invalidate_cells_cache
            await invalidate_cells_cache()
            await update.message.reply_text(
                _i18n_t('bot.admin_bot.handlers.s198', slot_id=slot_id, target_get_channel_id=target.get('channel_id'))
            )
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s199', slot_id=slot_id))
    except Exception as e:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s200', e=e))


# ─── 轮转配置管理 ──────────────────────────────────────────────

@_auth_required
async def rotation_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s162')
        )
        return
    key = args[0].strip()
    value = args[1].strip()
    valid_keys = {"active_window_size", "files_per_slot", "time_per_slot"}
    if key not in valid_keys:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s163', key=key, join_sorted_valid_keys=', '.join(sorted(valid_keys))))
        return
    try:
        int(value)
    except ValueError:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s201'))
        return
    db_key = f"rotation_{key}"
    await set_rotation_config(db_key, value)
    label_map = {
        "active_window_size": _i18n_t('bot.admin_bot.handlers.s29'),
        "files_per_slot": _i18n_t('bot.admin_bot.handlers.s30'),
        "time_per_slot": _i18n_t('bot.admin_bot.handlers.s31'),
    }
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s76', label_map_get_key_key=label_map.get(key, key), value=value)
    )


@_auth_required
async def rotation_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = [
        ("rotation_active_window_size", "active_window_size", _i18n_t('bot.admin_bot.handlers.s77')),
        ("rotation_files_per_slot", "files_per_slot", _i18n_t('bot.admin_bot.handlers.s78')),
        ("rotation_time_per_slot", "time_per_slot", _i18n_t('bot.admin_bot.handlers.s79')),
    ]
    msg = _i18n_t('bot.admin_bot.handlers.s13')
    for db_key, fallback_key, label in keys:
        val = await get_rotation_config(db_key)
        if val is None:
            val = str(getattr(settings, f"ROTATION_{fallback_key.upper()}", "—"))
        msg += f"  {label}: {val}\n"
    msg += _i18n_t('bot.admin_bot.handlers.s14')
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
                _i18n_t('bot.admin_bot.handlers.s202')
            )
            return
        msg = _i18n_t('bot.admin_bot.handlers.s32')
        for uid in sorted(ids):
            msg += f"  • `{uid}`\n"
        msg += _i18n_t('bot.admin_bot.handlers.s33', len_ids=len(ids))
        msg += _i18n_t('bot.admin_bot.handlers.s34')
        await update.message.reply_text(msg)
        return

    action = args[0].lower()
    if action == "add":
        if len(args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s203'))
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s234'))
            return
        added = await add_relay_whitelist(user_id)
        if added:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s204', user_id=user_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s205', user_id=user_id))
    elif action == "remove":
        if len(args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s235'))
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s262'))
            return
        removed = await remove_relay_whitelist(user_id)
        if removed:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s236', user_id=user_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s237', user_id=user_id))
    elif action == "clear":
        await delete_config("relay_account_ids")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s238'))
    else:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s239')
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
                _i18n_t('bot.admin_bot.handlers.s206')
            )
            return
        msg = _i18n_t('bot.admin_bot.handlers.s35')
        for uid in sorted(ids):
            msg += f"  • `{uid}`\n"
        msg += _i18n_t('bot.admin_bot.handlers.s36', len_ids=len(ids))
        msg += _i18n_t('bot.admin_bot.handlers.s37')
        await update.message.reply_text(msg)
        return

    action = args[0].lower()
    if action == "add":
        if len(args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s207'))
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s240'))
            return
        added = await add_collector_whitelist(user_id)
        if added:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s208', user_id=user_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s209', user_id=user_id))
    elif action == "remove":
        if len(args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s241'))
            return
        try:
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s263'))
            return
        removed = await remove_collector_whitelist(user_id)
        if removed:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s242', user_id=user_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s243', user_id=user_id))
    elif action == "clear":
        await delete_config("collector_account_ids")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s244'))
    else:
        await update.message.reply_text(
            _i18n_t('bot.admin_bot.handlers.s245')
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
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s164'))
    else:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s165'))


# ─── 帮助命令 ──────────────────────────────────────────────────

@_auth_required
@require_maintenance_check(action=_i18n_t('bot.admin_bot.handlers.s42'))
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
        admin_id = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(_t(admin_id, "bot.admin_bot.seq_must_be_number"))
        return

    # 解析可选参数 table: 和 merge:
    tables = None
    merge = False
    for arg in args[1:]:
        if arg.startswith("table:"):
            tables_str = arg[len("table:"):]
            tables = [t.strip() for t in tables_str.split(",") if t.strip()]
            if not tables:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s246'))
                return
        elif arg.startswith("merge:"):
            merge_val = arg[len("merge:"):].lower()
            merge = merge_val in ("yes", "1", "true", "on")

    # 查备份列表,取出对应序号的 key
    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s210', e=e))
        return

    if not backups:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s166'))
        return

    if seq < 1 or seq > len(backups):
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s167', len_backups=len(backups)))
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
            InlineKeyboardButton(_i18n_t('bot.admin_bot.handlers.s211'), callback_data=cb_data),
        ],
        [
            InlineKeyboardButton(_i18n_t('bot.admin_bot.handlers.s212'), callback_data="restore:cancel"),
        ],
    ])

    scope_text = _i18n_t('bot.admin_bot.handlers.s38', join_tables=', '.join(tables)) if tables else _i18n_t('bot.admin_bot.handlers.s39')
    if merge:
        mode_text = _i18n_t('bot.admin_bot.handlers.s40')
    else:
        mode_text = _i18n_t('bot.admin_bot.handlers.s41')
    await update.message.reply_text(
        _i18n_t('bot.admin_bot.handlers.s213', key=key, size=size, last_mod=last_mod, scope_text=scope_text, mode_text=mode_text)
        + ("" if merge else _i18n_t('bot.admin_bot.handlers.s247'))
        + _i18n_t('bot.admin_bot.handlers.s168'),
        reply_markup=kb,
    )


async def _restore_list_backups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出 R2 中的备份文件。"""
    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s214', e=e))
        return

    if not backups:
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s169'))
        return

    lines = [_i18n_t('bot.admin_bot.handlers.s43', len_backups=len(backups))]
    lines.append(_i18n_t('bot.admin_bot.handlers.s44'))
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
        lines.append(_i18n_t('bot.admin_bot.handlers.s80', len_backups=len(backups)))

    lines.append(_i18n_t('bot.admin_bot.handlers.s45'))
    lines.append(_i18n_t('bot.admin_bot.handlers.s46'))
    lines.append(_i18n_t('bot.admin_bot.handlers.s47'))
    lines.append(_i18n_t('bot.admin_bot.handlers.s48'))
    lines.append(_i18n_t('bot.admin_bot.handlers.s49'))

    await update.message.reply_text("\n".join(lines))


@_auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        _i18n_t('bot.admin_bot.handlers.s15')
    )
    await update.message.reply_text(msg)


# ─── R40 新增管理命令(13 条) ──────────────────────────────────

@_auth_required
async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看举报列表: /reports [状态] [页码]"""
    try:
        status_filter = context.args[0] if context.args else None
        page = 1
        if len(context.args) >= 2:
            try:
                page = int(context.args[1])
            except ValueError:
                page = 1
        result = await content_reports.list_reports(status=status_filter, page=page, page_size=20)
        items = result.get("items", [])
        if not items:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s215'))
            return
        lines = [_i18n_t('bot.admin_bot.handlers.s81', result_get_page_1=result.get('page', 1), result_get_total_pages_1=result.get('total_pages', 1), result_get_total_0=result.get('total', 0))]
        for r in items:
            lines.append(await content_reports.format_report(r))
        await update.message.reply_text("\n\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][reports] 查询举报列表失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))


@_auth_required
@require_maintenance_check(action=_i18n_t('bot.admin_bot.handlers.s50'))
async def cmd_takedown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R40 P0-8: 内容下架(通过 CommandBus 强制 RBAC + 审批门禁)。
    用法: /takedown <target_type> <target_id> [reason]
    """
    try:
        if len(context.args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s216'))
            return
        target_type = context.args[0]
        target_id = context.args[1]
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else ""
        user = update.effective_user
        admin_id = user.id if user else 0
        admin_name = user.username if user else ""

        # R40 P0-8: 通过 CommandBus 执行,强制 RBAC 权限校验 + 审批门禁
        from services.command_bus import (
            CommandBus, AdminPrincipal as CBPrincipal, make_takedown_command,
        )
        cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
        command = make_takedown_command(
            target_type=target_type, target_id=str(target_id), reason=reason,
        )
        bus = CommandBus()
        result = await bus.execute(command, cb_principal)

        if result.approval_required:
            await update.message.reply_text(
                _i18n_t('bot.admin_bot.handlers.s217', result_approval_id=result.approval_id, target_type=target_type, target_id=target_id)
            )
        elif result.success:
            ok = result.data.get("takedown_ok", False) if result.data else False
            if ok:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s264', target_type=target_type, target_id=target_id))
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s265'))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s281', error=result.error))
    except Exception as e:
        logger.exception(f"[Admin][takedown] 内容下架失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
@require_maintenance_check(action=_i18n_t('bot.admin_bot.handlers.s51'))
async def cmd_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R40 P0-8: 封禁用户(通过 CommandBus 强制 RBAC + 审批门禁)。
    用法: /ban_user <user_id> [reason] [duration_days(0=永久)]
    """
    try:
        if not context.args:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s218'))
            return
        try:
            user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s248'))
            return
        reason = context.args[1] if len(context.args) > 1 else ""
        duration_days = 0
        if len(context.args) > 2:
            try:
                duration_days = int(context.args[2])
            except ValueError:
                duration_days = 0
        admin = update.effective_user
        admin_id = admin.id if admin else 0
        admin_name = admin.username if admin else ""

        # R40 P0-8: 通过 CommandBus 执行,强制 RBAC 权限校验 + 审批门禁
        from services.command_bus import (
            CommandBus, AdminPrincipal as CBPrincipal, make_ban_user_command,
        )
        cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
        command = make_ban_user_command(
            user_id=user_id, reason=reason, duration_days=duration_days,
        )
        bus = CommandBus()
        result = await bus.execute(command, cb_principal)

        if result.approval_required:
            await update.message.reply_text(
                _i18n_t('bot.admin_bot.handlers.s219', result_approval_id=result.approval_id, user_id=user_id, duration_days=duration_days)
            )
        elif result.success:
            ok = result.data.get("ban_ok", False) if result.data else False
            if ok:
                admin_id = update.effective_user.id if update.effective_user else 0
                await update.message.reply_text(
                    _t(
                        admin_id,
                        "bot.admin_bot.ban_success_duration",
                        user_id=user_id,
                        duration_days=duration_days,
                    )
                )
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s266'))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s283', error=result.error))
    except Exception as e:
        logger.exception(f"[Admin][ban_user] 封禁用户失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
@require_maintenance_check(action=_i18n_t('bot.admin_bot.handlers.s52'))
async def cmd_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R40 P0-8: 解封用户(通过 CommandBus 强制 RBAC;不需审批)。
    用法: /unban_user <user_id>
    """
    try:
        if not context.args:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s220'))
            return
        try:
            user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s249'))
            return
        admin = update.effective_user
        admin_id = admin.id if admin else 0
        admin_name = admin.username if admin else ""

        # R40 P0-8: 通过 CommandBus 执行(解封不需审批,但仍强制 RBAC 校验)
        from services.command_bus import (
            CommandBus, AdminPrincipal as CBPrincipal, make_unban_user_command,
        )
        cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
        command = make_unban_user_command(user_id=user_id)
        bus = CommandBus()
        result = await bus.execute(command, cb_principal)

        if result.success:
            ok = result.data.get("unban_ok", False) if result.data else False
            if ok:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s250', user_id=user_id))
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s251'))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s284', error=result.error))
    except Exception as e:
        logger.exception(f"[Admin][unban_user] 解封用户失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
async def cmd_pending_approvals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """待审批列表: /pending_approvals [页码]"""
    try:
        page = 1
        if context.args:
            try:
                page = int(context.args[0])
            except ValueError:
                page = 1
        result = await approval_workflow.list_pending(page=page, page_size=20)
        items = result.get("items", [])
        if not items:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s221'))
            return
        lines = [_i18n_t('bot.admin_bot.handlers.s82', result_get_page_1=result.get('page', 1), result_get_total_pages_1=result.get('total_pages', 1), result_get_total_0=result.get('total', 0))]
        for a in items:
            lines.append(await approval_workflow.format_approval(a))
        await update.message.reply_text("\n\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][pending_approvals] 查询待审批失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))


@_auth_required
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """批准审批: /approve <approval_id> [note]"""
    try:
        if not context.args:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s222'))
            return
        try:
            approval_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s252'))
            return
        note = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        admin = update.effective_user
        approver_id = admin.id if admin else 0
        ok = await approval_workflow.approve(approval_id, approver_id, note=note)
        if ok:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s223', approval_id=approval_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s285'))
    except Exception as e:
        logger.exception(f"[Admin][approve] 批准审批失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """驳回审批: /reject <approval_id> [reason]"""
    try:
        if not context.args:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s224'))
            return
        try:
            approval_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s253'))
            return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        admin = update.effective_user
        approver_id = admin.id if admin else 0
        ok = await approval_workflow.reject(approval_id, approver_id, reason=reason)
        if ok:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s225', approval_id=approval_id))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s286'))
    except Exception as e:
        logger.exception(f"[Admin][reject] 驳回审批失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看角色列表: /roles"""
    try:
        roles = await rbac.list_roles()
        if not roles:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s226'))
            return
        lines = [_i18n_t('bot.admin_bot.handlers.s83')]
        for r in roles:
            lines.append(await rbac.format_role_info(r))
            lines.append("─" * 30)
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][roles] 查询角色失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))


@_auth_required
async def cmd_assign_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R40 P0-8: 分配角色(通过 CommandBus 强制 RBAC + 审批门禁)。
    用法: /assign_role <user_id> <role_name>
    """
    try:
        if len(context.args) < 2:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s227'))
            return
        try:
            user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s254'))
            return
        role_name = context.args[1]
        admin = update.effective_user
        admin_id = admin.id if admin else 0
        admin_name = admin.username if admin else ""

        # R40 P0-8: 通过 CommandBus 执行,强制 RBAC 权限校验 + 审批门禁
        from services.command_bus import (
            CommandBus, AdminPrincipal as CBPrincipal, make_assign_role_command,
        )
        cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
        command = make_assign_role_command(user_id=user_id, role_name=role_name)
        bus = CommandBus()
        result = await bus.execute(command, cb_principal)

        if result.approval_required:
            await update.message.reply_text(
                _i18n_t('bot.admin_bot.handlers.s228', result_approval_id=result.approval_id, user_id=user_id, role_name=role_name)
            )
        elif result.success:
            ok = result.data.get("assign_ok", False) if result.data else False
            if ok:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s267', user_id=user_id, role_name=role_name))
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s268'))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s287', error=result.error))
    except Exception as e:
        logger.exception(f"[Admin][assign_role] 分配角色失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """R40 P0-8: 维护模式(通过 CommandBus 强制 RBAC + 审批门禁)。
    用法: /maintenance <on|off|status> [reason]
    """
    try:
        if not context.args:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s229'))
            return
        action = context.args[0].lower()
        admin = update.effective_user
        admin_id = admin.id if admin else 0
        admin_name = admin.username if admin else ""

        # R40 P0-8: on/off 走 CommandBus 强制 RBAC + 审批门禁;status 仅查询不走 CommandBus
        from services.command_bus import (
            CommandBus, AdminPrincipal as CBPrincipal,
            make_enable_maintenance_command, make_disable_maintenance_command,
        )

        if action == "on":
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else "manual"
            cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
            command = make_enable_maintenance_command(reason=reason)
            bus = CommandBus()
            result = await bus.execute(command, cb_principal)
            if result.approval_required:
                await update.message.reply_text(
                    _i18n_t('bot.admin_bot.handlers.s255', result_approval_id=result.approval_id, reason=reason)
                )
            elif result.success:
                ok = result.data.get("enable_ok", False) if result.data else False
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s271') if ok else _i18n_t('bot.admin_bot.handlers.s272'))
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s269', result_error=result.error))
        elif action == "off":
            cb_principal = CBPrincipal(id=admin_id, name=admin_name, source="bot")
            command = make_disable_maintenance_command()
            bus = CommandBus()
            result = await bus.execute(command, cb_principal)
            if result.approval_required:
                await update.message.reply_text(
                    _i18n_t('bot.admin_bot.handlers.s270', result_approval_id=result.approval_id)
                )
            elif result.success:
                ok = result.data.get("disable_ok", False) if result.data else False
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s275') if ok else _i18n_t('bot.admin_bot.handlers.s276'))
            else:
                await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s273', result_error=result.error))
        elif action == "status":
            # status 仅查询,不修改状态,不走 CommandBus
            status = await maintenance_mode.get_status()
            lines = [
                _i18n_t('bot.admin_bot.handlers.s256'),
                _i18n_t('bot.admin_bot.handlers.s257', status_get_enabled_False=status.get('enabled', False)),
                _i18n_t('bot.admin_bot.handlers.s258', status_get_reason=status.get('reason', '')),
                _i18n_t('bot.admin_bot.handlers.s259', status_get_started_by_0=status.get('started_by', 0)),
                _i18n_t('bot.admin_bot.handlers.s260', status_get_started_at=status.get('started_at', '')),
                _i18n_t('bot.admin_bot.handlers.s261', status_get_duration_seconds_0=status.get('duration_seconds', 0)),
            ]
            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s288'))
    except Exception as e:
        logger.exception(f"[Admin][maintenance] 维护模式操作失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s282'))


@_auth_required
async def cmd_repair_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """修复控制台总览: /repair_console"""
    try:
        overview = await repair_console.get_repair_overview()
        lines = [
            _i18n_t('bot.admin_bot.handlers.s84'),
            _i18n_t('bot.admin_bot.handlers.s85', overview_get_outbox_unprocessed_0=overview.get('outbox_unprocessed', 0)),
            _i18n_t('bot.admin_bot.handlers.s86', overview_get_outbox_dead_0=overview.get('outbox_dead', 0)),
            _i18n_t('bot.admin_bot.handlers.s87', overview_get_dlq_count_0=overview.get('dlq_count', 0)),
            _i18n_t('bot.admin_bot.handlers.s88', overview_get_replication_failed_0=overview.get('replication_failed', 0)),
            _i18n_t('bot.admin_bot.handlers.s89', overview_get_relay_issues_0=overview.get('relay_issues', 0)),
        ]
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][repair_console] 查询修复总览失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))


@_auth_required
async def cmd_backups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看备份列表与灾备状态: /backups"""
    try:
        backups = await disaster_recovery.list_backups(limit=20)
        rpo_rto = await disaster_recovery.get_rpo_rto()
        schedule = await disaster_recovery.get_backup_schedule()
        lines = [
            _i18n_t('bot.admin_bot.handlers.s90'),
            _i18n_t('bot.admin_bot.handlers.s91', rpo_rto_get_rpo_seconds_0=rpo_rto.get('rpo_seconds', 0), if_rpo_rto_get_rpo_compliant_else='✓合规' if rpo_rto.get('rpo_compliant') else '✗违规'),
            _i18n_t('bot.admin_bot.handlers.s92', rpo_rto_get_rto_seconds_0=rpo_rto.get('rto_seconds', 0), if_rpo_rto_get_rto_compliant_else='✓合规' if rpo_rto.get('rto_compliant') else '✗违规'),
            _i18n_t('bot.admin_bot.handlers.s93', rpo_rto_get_last_backup_age_0=rpo_rto.get('last_backup_age', 0)),
            _i18n_t('bot.admin_bot.handlers.s94', schedule_get_enabled_False=schedule.get('enabled', False), schedule_get_interval_minutes_0=schedule.get('interval_minutes', 0)),
            _i18n_t('bot.admin_bot.handlers.s95', schedule_get_retention_days_0=schedule.get('retention_days', 0)),
            "",
            _i18n_t('bot.admin_bot.handlers.s96', len_backups=len(backups)),
        ]
        for b in backups[:10]:
            lines.append(f"  • {b.get('backup_id', '')} ({b.get('created_at', '')})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][backups] 查询备份失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))


@_auth_required
async def cmd_ru_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """RU 成本报告: /ru_report [start_date YYYYMMDD] [end_date YYYYMMDD]"""
    try:
        import datetime as _dt
        if len(context.args) >= 2:
            start_date = context.args[0]
            end_date = context.args[1]
        else:
            today = _dt.datetime.now().strftime("%Y%m%d")
            start_date = today
            end_date = today
        report = await ru_cost_center.generate_cost_report(start_date, end_date)
        await update.message.reply_text(report)
    except Exception as e:
        logger.exception(f"[Admin][ru_report] 生成 RU 报告失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s289'))


@_auth_required
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看任务中心(所有用户): /tasks [status] [limit]

    R40 P1-1:
        管理员查看所有用户任务,不再过滤 user_id=0。
        可选参数:
            status: pending/running/completed/failed/cancelled
            limit: 返回条数(默认 20,上限 100)

    用法示例:
        /tasks              # 最近 20 条所有任务
        /tasks pending      # 仅 pending 状态
        /tasks running 50   # 50 条 running 任务
    """
    try:
        from services.task_center import list_all_tasks
        status_filter = None
        limit = 20
        if len(context.args) >= 1:
            arg = context.args[0].strip().lower()
            valid_statuses = {"pending", "running", "completed", "failed", "cancelled"}
            if arg in valid_statuses:
                status_filter = arg
            else:
                # 第一个参数不是状态,尝试解析为 limit
                try:
                    limit = max(1, min(100, int(arg)))
                except ValueError:
                    await update.message.reply_text(
                        _i18n_t('bot.admin_bot.handlers.s274')
                    )
                    return
        if len(context.args) >= 2:
            try:
                limit = max(1, min(100, int(context.args[1])))
            except ValueError:
                pass

        tasks = await list_all_tasks(limit=limit, offset=0, status_filter=status_filter)
        if not tasks:
            filter_text = f" (status={status_filter})" if status_filter else ""
            await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s230', filter_text=filter_text))
            return

        lines = [
            _i18n_t('bot.admin_bot.handlers.s170', len_tasks=len(tasks))
            + (f" (status={status_filter})" if status_filter else ""),
            "",
        ]
        for t in tasks[:limit]:
            icon_map = {
                "pending": "⏳", "running": "🔄",
                "completed": "✅", "failed": "❌", "cancelled": "🚫",
            }
            icon = icon_map.get(t.get("status", ""), "❓")
            type_map = {
                "upload": _i18n_t('bot.admin_bot.handlers.s171'), "index": _i18n_t('bot.admin_bot.handlers.s172'), "copy": _i18n_t('bot.admin_bot.handlers.s173'),
                "delivery": _i18n_t('bot.admin_bot.handlers.s174'), "repair": _i18n_t('bot.admin_bot.handlers.s175'),
            }
            type_name = type_map.get(t.get("task_type", ""), t.get("task_type", ""))
            lines.append(
                f"{icon} #{t.get('id', '')} [{type_name}] user={t.get('user_id', '')} "
                f"{t.get('status', '')} ({t.get('progress', 0)}%)"
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception(f"[Admin][tasks] 查询任务失败: {e}")
        await update.message.reply_text(_i18n_t('bot.admin_bot.handlers.s280'))