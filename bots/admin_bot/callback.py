from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from loguru import logger

from database import (
    get_file_records_col,
    get_all_code_bot_routes,
    get_all_code_bot_routes_regex,
    get_all_bot_decode_intervals,
    list_spare_pool,
    get_rotation_config,
    update_file_record_and_invalidate,
)
from services.i18n import translate as _i18n_t
from database.cache import invalidate_file_record
from config import settings

from .menus import (
    _build_menu, BACK_BTN,
    AUTHORIZED_USER_ID,
)
from .display import (
    _get_status_text, _get_health_text, _get_topology_text,
    _get_logs_page_text, _get_users_page_text, _get_relay_status_text,
    _get_configs_text,
)
from .conversation import _conv_start, _conv_end


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer(_i18n_t('bot.admin_bot.callback.s5'), show_alert=True)
        return

    await query.answer()

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
        text = await _get_topology_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:logs":
        text = await _get_logs_page_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:users":
        text = await _get_users_page_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:files":
        files_col = get_file_records_col()
        from utils.shared_counters import status_counters
        total = status_counters.get("total_files", 0)
        files = await files_col.find(sort=("create_time", -1), limit=10)
        total_pages = max(1, (total + 10 - 1) // 10)
        text = _i18n_t('bot.admin_bot.callback.s25', total_pages=total_pages, total=total)
        for f in files:
            status_icon = "✅" if f.get("status") == "active" else "🗑️"
            fc = f.get("file_code", "N/A")
            uploader = f.get("uploader_id", "?")
            text += _i18n_t('bot.admin_bot.callback.s28', status_icon=status_icon, fc=fc, uploader=uploader)
        if total_pages > 1:
            text += _i18n_t('bot.admin_bot.callback.s29')
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_status":
        text = await _get_relay_status_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_pending":
        from database import get_config as _get_cfg
        # R31-2: 遍历中继实例检查各账号的 pending 状态，而非读全局键
        pending_found = False
        try:
            from services.relay_pool import relay_pool
            if relay_pool._initialized:
                for inst in relay_pool.instances:
                    if await _get_cfg(f"relay_auth_pending:{inst.phone}") == "1":
                        pending_found = True
                        break
        except Exception:
            pending_found = await _get_cfg("relay_auth_pending") == "1"
        if pending_found:
            text = _i18n_t('bot.admin_bot.callback.s31')
        else:
            text = _i18n_t('bot.admin_bot.callback.s32')
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_whitelist":
        from database import get_relay_whitelist
        wl = await get_relay_whitelist()
        if wl:
            text = _i18n_t('bot.admin_bot.callback.s34') + "\n".join(f"  • {uid}" for uid in sorted(wl))
        else:
            text = _i18n_t('bot.admin_bot.callback.s33')
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:collector_whitelist":
        from database import get_collector_whitelist
        wl = await get_collector_whitelist()
        if wl:
            text = _i18n_t('bot.admin_bot.callback.s36') + "\n".join(f"  • {uid}" for uid in sorted(wl))
        else:
            text = _i18n_t('bot.admin_bot.callback.s35')
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:settings":
        text = await _get_configs_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:code_routes":
        routes = await get_all_code_bot_routes()
        regex_routes = await get_all_code_bot_routes_regex()
        if not routes and not regex_routes:
            text = _i18n_t('bot.admin_bot.callback.s37')
        else:
            text = _i18n_t('bot.admin_bot.callback.s38')
            if routes:
                text += _i18n_t('bot.admin_bot.callback.s39')
                for prefix in sorted(routes.keys()):
                    text += f"  • `{prefix}` → @{routes[prefix]}\n"
            if regex_routes:
                if routes:
                    text += "\n"
                text += _i18n_t('bot.admin_bot.callback.s40')
                for rid, bot, pattern in regex_routes:
                    text += f"  • [{rid}] `{pattern}` → @{bot}\n"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:bot_intervals":
        intervals = await get_all_bot_decode_intervals()
        if not intervals:
            text = _i18n_t('bot.admin_bot.callback.s41')
        else:
            text = _i18n_t('bot.admin_bot.callback.s42')
            for bot in sorted(intervals.keys()):
                text += _i18n_t('bot.admin_bot.callback.s43', bot=bot, intervals_bot=intervals[bot])
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:topology":
        text = await _get_topology_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:spare_list":
        spares = await list_spare_pool()
        if not spares:
            text = _i18n_t('bot.admin_bot.callback.s44')
        else:
            text = _i18n_t('bot.admin_bot.callback.s45')
            for s in spares:
                used = _i18n_t('bot.admin_bot.callback.s49') if s.get("is_used") else _i18n_t('bot.admin_bot.callback.s50')
                acc = s.get("account_name") or _i18n_t('bot.admin_bot.callback.s51')
                text += f"  {used} {s['channel_id']} — {acc}\n"
            text += _i18n_t('bot.admin_bot.callback.s46', len_spares=len(spares))
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:rotation_view":
        keys = [
            ("rotation_active_window_size", "active_window_size", _i18n_t('bot.admin_bot.callback.s52')),
            ("rotation_files_per_slot", "files_per_slot", _i18n_t('bot.admin_bot.callback.s53')),
            ("rotation_time_per_slot", "time_per_slot", _i18n_t('bot.admin_bot.callback.s54')),
        ]
        text = _i18n_t('bot.admin_bot.callback.s47')
        for db_key, fallback_key, label in keys:
            val = await get_rotation_config(db_key)
            if val is None:
                val = str(getattr(settings, f"ROTATION_{fallback_key.upper()}", "—"))
            text += f"  {label}: {val}\n"
        text += _i18n_t('bot.admin_bot.callback.s48')
        await query.edit_message_text(text, reply_markup=back_kb)

    # ─── 交互式操作入口 ──────────────────────────────────────────
    elif data.startswith("interactive:"):
        context.user_data.pop("conv_state", None)
        context.user_data.pop("conv_data", None)
        context.user_data.pop("conv_started_at", None)
        action = data[len("interactive:"):]

        prompts = {
            # 文件码路由
            "add_code_route": (
                "add_code_route:prefix",
                _i18n_t('bot.admin_bot.callback.s55')
            ),
            "remove_code_route": (
                "remove_code_route:prefix",
                _i18n_t('bot.admin_bot.callback.s56')
            ),
            "add_code_route_regex": (
                "add_code_route_regex:pattern",
                _i18n_t('bot.admin_bot.callback.s57')
            ),
            "remove_code_route_regex": (
                "remove_code_route_regex:id",
                _i18n_t('bot.admin_bot.callback.s58')
            ),
            # Bot限流
            "set_bot_interval": (
                "set_bot_interval:bot",
                _i18n_t('bot.admin_bot.callback.s59')
            ),
            "remove_bot_interval": (
                "remove_bot_interval:bot",
                _i18n_t('bot.admin_bot.callback.s60')
            ),
            # 用户管理
            "user_detail": (
                "user_detail:id",
                _i18n_t('bot.admin_bot.callback.s61')
            ),
            "set_level": (
                "set_level:user_id",
                _i18n_t('bot.admin_bot.callback.s62')
            ),
            "ban": (
                "ban:user_id",
                _i18n_t('bot.admin_bot.callback.s63')
            ),
            "unban": (
                "unban:user_id",
                _i18n_t('bot.admin_bot.callback.s64')
            ),
            "set_quota": (
                "set_quota:user_id",
                _i18n_t('bot.admin_bot.callback.s65')
            ),
            "set_external_quota": (
                "set_external_quota:user_id",
                _i18n_t('bot.admin_bot.callback.s66')
            ),
            # 文件管理
            "file_detail": (
                "file_detail:code",
                _i18n_t('bot.admin_bot.callback.s67')
            ),
            "delete_file": (
                "delete_file:code",
                _i18n_t('bot.admin_bot.callback.s68')
            ),
            # 中继
            "relay_code": (
                "relay_code:code",
                _i18n_t('bot.admin_bot.callback.s69')
            ),
            "relay_add": (
                "relay_add:phone",
                _i18n_t('bot.admin_bot.callback.s70')
            ),
            "relay_password": (
                "relay_password:password",
                _i18n_t('bot.admin_bot.callback.s71')
            ),
            "relay_remove": (
                "relay_remove:phone",
                _i18n_t('bot.admin_bot.callback.s72')
            ),
            "collector_wl_add": (
                "collector_wl_add:user_id",
                _i18n_t('bot.admin_bot.callback.s73')
            ),
            "collector_wl_remove": (
                "collector_wl_remove:user_id",
                _i18n_t('bot.admin_bot.callback.s74')
            ),
            "set_access_limit": (
                "set_access_limit:code",
                _i18n_t('bot.admin_bot.callback.s75')
            ),
            "cell_add": (
                "cell_add:slot_id",
                _i18n_t('bot.admin_bot.callback.s76')
            ),
            "cell_remove": (
                "cell_remove:slot_id",
                _i18n_t('bot.admin_bot.callback.s77')
            ),
            # 系统配置
            "set_file_prefix": (
                "set_file_prefix:prefix",
                _i18n_t('bot.admin_bot.callback.s78')
            ),
            "set_force_join": (
                "set_force_join:channel_id",
                _i18n_t('bot.admin_bot.callback.s79')
            ),
            "set_username": (
                "set_username:role",
                _i18n_t('bot.admin_bot.callback.s80')
            ),
            "set_quota_default": (
                "set_quota_default:level",
                _i18n_t('bot.admin_bot.callback.s81')
            ),
            "set_r2": (
                "set_r2:account_id",
                _i18n_t('bot.admin_bot.callback.s82')
            ),
            "set_db_backup": (
                "set_db_backup:interval",
                _i18n_t('bot.admin_bot.callback.s83')
            ),
            # 备用池
            "spare_add": (
                "spare_add:channel_id",
                _i18n_t('bot.admin_bot.callback.s84')
            ),
            "spare_remove": (
                "spare_remove:channel_id",
                _i18n_t('bot.admin_bot.callback.s85')
            ),
            # 轮转配置
            "rotation_set": (
                "rotation_set:key",
                _i18n_t('bot.admin_bot.callback.s86')
            ),
        }

        entry = prompts.get(action)
        if entry:
            state, prompt = entry
            await _conv_start(update, context, state, prompt)
        else:
            await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s87', action=action), reply_markup=back_kb)

    # ─── 举报处理 ──────────────────────────────────────────────
    elif data.startswith("report:"):
        await _handle_report_action(update, context, data)

    elif data == "conv:cancel":
        await _conv_end(context)
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s88'),
            reply_markup=back_kb,
        )

    # ─── 数据库恢复 ──────────────────────────────────────────────
    elif data.startswith("restore:"):
        await _handle_restore_action(update, context, data)

    # ─── 文件删除二次确认(P2-8)────────────────────────────────────
    elif data.startswith("delfile|") or data.startswith("delfile_cancel|"):
        await _handle_delete_file_action(update, context, data)


# ─── 举报动作处理 ──────────────────────────────────────────────

async def _handle_report_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """处理管理员对举报的操作：封禁/脱钩/限制/忽略"""
    import datetime as _dt
    from database import update_user_and_invalidate

    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer(_i18n_t('bot.admin_bot.callback.s6'), show_alert=True)
        return

    if data == "report:ignore":
        await query.edit_message_text(
            query.message.text + _i18n_t('bot.admin_bot.callback.s20'),
            reply_markup=None,
        )
        return

    parts = data.split("|")
    action = parts[0]  # report:ban, report:detach, report:block

    # 解析举报人信息和来源 bot(格式: report:xxx|...|reporter_id|source)
    reporter_id_str = None
    source_bot = None
    if len(parts) >= 4 and parts[-1] in ("idx", "dsp"):
        source_bot = parts[-1]
        reporter_id_str = parts[-2]

    try:
        if action == "report:ban":
            if len(parts) < 2:
                await query.answer(_i18n_t('bot.admin_bot.callback.s23'), show_alert=True)
                return
            uid = int(parts[1])
            await update_user_and_invalidate(uid, {
                "$set": {"is_banned": True, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()},
            })
            # 跨进程通知 idx_bot/dsp_bot 失效用户缓存
            try:
                from database.cache_store import get_cache_store
                store = get_cache_store()
                await store.notify_record_change("user", str(uid))
            except Exception as e:
                logger.warning(f"[Admin][report:ban] 通知记录变更失败: {e}")
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已封禁用户 {uid}",
                reply_markup=None,
            )
            # 通知举报人
            if reporter_id_str and source_bot:
                await _notify_reporter(reporter_id_str, source_bot, _i18n_t('bot.admin_bot.callback.s24'))

        elif action == "report:detach":
            if len(parts) < 2:
                await query.answer(_i18n_t('bot.admin_bot.callback.s26'), show_alert=True)
                return
            file_code = parts[1]
            # PRE-06: 用 update_file_record_and_invalidate 一次性双写 CRDB+SQLite 并失效内存缓存
            try:
                await update_file_record_and_invalidate(file_code, {"$set": {"status": "detached"}})
            except Exception as e:
                logger.error(f"[Admin][report:detach] 双写失败: {e}")
                invalidate_file_record(file_code)
            # 跨进程通知 idx_bot/dsp_bot 失效文件记录缓存
            try:
                from database.cache_store import get_cache_store
                store = get_cache_store()
                await store.notify_record_change("file", file_code)
            except Exception as e:
                logger.warning(f"[Admin][report:detach] 通知记录变更失败: {e}")
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已脱钩文件码 {file_code}",
                reply_markup=None,
            )
            # 通知举报人
            if reporter_id_str and source_bot:
                await _notify_reporter(reporter_id_str, source_bot, _i18n_t('bot.admin_bot.callback.s27'))

        elif action == "report:block":
            if len(parts) < 3:
                await query.answer(_i18n_t('bot.admin_bot.callback.s30'), show_alert=True)
                return
            file_code = parts[1]
            reporter_id = int(parts[2])
            # P2-5/F-L4: 用 $addToSet 去重写入,防止同一举报人重复入列
            try:
                await update_file_record_and_invalidate(file_code, {"$addToSet": {"blocked_users": reporter_id}})
            except Exception as e:
                logger.error(f"[Admin][report:block] 双写失败: {e}")
                invalidate_file_record(file_code)
            # 跨进程通知 idx_bot/dsp_bot 失效文件记录缓存
            try:
                from database.cache_store import get_cache_store
                store = get_cache_store()
                await store.notify_record_change("file", file_code)
            except Exception as e:
                logger.warning(f"[Admin][report:block] 通知记录变更失败: {e}")
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已限制举报人 {reporter_id} 解码 {file_code}",
                reply_markup=None,
            )

    except Exception as e:
        logger.error(f"[Admin][report] 操作失败: {e}")
        await query.answer(f"操作失败: {e}", show_alert=True)


async def _notify_reporter(reporter_id_str: str, source_bot: str, message: str):
    """通过来源 Bot 向举报人发送受理通知。

    source_bot: "idx" 或 "dsp",对应使用 DECODER_BOT_TOKEN 或 SENDER_BOT_TOKEN。
    """
    from telegram import Bot
    from config import settings
    try:
        reporter_id = int(reporter_id_str)
    except (ValueError, TypeError):
        return

    token = None
    if source_bot == "idx":
        token = settings.DECODER_BOT_TOKEN
    elif source_bot == "dsp":
        token = settings.SENDER_BOT_TOKEN
    if not token:
        logger.warning(f"[Admin][notify_reporter] 未配置 {source_bot} bot token,跳过通知")
        return

    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=reporter_id, text=message)
    except Exception as e:
        logger.warning(f"[Admin][notify_reporter] 通过 {source_bot} 通知用户 {reporter_id} 失败: {e}")


# ─── 数据库恢复动作处理 ──────────────────────────────────────────

async def _handle_restore_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """处理数据库恢复的确认/取消按钮。"""
    query = update.callback_query
    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer(_i18n_t('bot.admin_bot.callback.s7'), show_alert=True)
        return

    back_kb = InlineKeyboardMarkup(BACK_BTN)

    if data == "restore:cancel":
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s8'), reply_markup=back_kb)
        return

    # restore:confirm|<seq>|<0或1>|table:xxx,yyy  (merge标志和table部分均可选)
    if not data.startswith("restore:confirm|"):
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s9'), reply_markup=back_kb)
        return

    parts = data.split("|")
    if len(parts) < 2:
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s10'), reply_markup=back_kb)
        return

    try:
        seq = int(parts[1])
    except ValueError:
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s21'), reply_markup=back_kb)
        return

    # 解析 merge 标志(parts[2],0 或 1)和 table 列表(parts[3],可选)
    merge = False
    tables = None
    if len(parts) >= 3:
        # parts[2] 是 merge 标志(0/1)
        merge = parts[2] == "1"
    if len(parts) >= 4 and parts[3].startswith("table:"):
        tables = [t.strip() for t in parts[3][len("table:"):].split(",") if t.strip()]

    # 先给用户一个"正在恢复"的反馈
    mode_label = _i18n_t('bot.admin_bot.callback.s1') if merge else _i18n_t('bot.admin_bot.callback.s2')
    await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s3', mode_label=mode_label))

    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s22', e=e), reply_markup=back_kb)
        return

    if not backups or seq < 1 or seq > len(backups):
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s11'), reply_markup=back_kb)
        return

    key = backups[seq - 1].get("key", "")
    logger.info(f"[Admin][restore] 开始提交恢复审批: key={key}, tables={tables}, merge={merge}, 操作者={user.id}")

    # R40 P0-8: 通过 CommandBus 强制 RBAC + 审批门禁(灾备恢复必须审批)
    # 审批通过后,approval_workflow 会自动触发 execute_approved_action 执行实际恢复
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_restore_backup_command,
    )
    cb_principal = CBPrincipal(id=user.id, name=user.username or "", source="bot")
    command = make_restore_backup_command(
        backup_id=key, tables=tables, merge=merge,
    )
    bus = CommandBus()
    try:
        result = await bus.execute(command, cb_principal)
    except Exception as e:
        logger.error(f"[Admin][restore] 提交恢复审批失败: {e}")
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s89', error=e, backup_key=key),
            reply_markup=back_kb,
        )
        return

    if result.approval_required:
        # 灾备恢复必须审批,告知用户审批 ID
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s12', mode_label=mode_label, result_approval_id=result.approval_id, key=key, mode_label_4=mode_label, user_id=user.id),
            reply_markup=back_kb,
        )
        return

    if result.success:
        # 不应到达此处(restore_backup 必须审批),但保持健壮性
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s13', key=key),
            reply_markup=back_kb,
        )
    else:
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s14', result_error=result.error, key=key),
            reply_markup=back_kb,
        )


# ─── 文件删除二次确认处理 ──────────────────────────────────────────

async def _handle_delete_file_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """处理文件删除的二次确认/取消按钮(P2-8)。"""
    query = update.callback_query
    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer(_i18n_t('bot.admin_bot.callback.s15'), show_alert=True)
        return

    back_kb = InlineKeyboardMarkup(BACK_BTN)

    if data.startswith("delfile_cancel|"):
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s16'), reply_markup=back_kb)
        return

    # delfile|{file_code}
    parts = data.split("|", 1)
    if len(parts) < 2:
        await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s17'), reply_markup=back_kb)
        return
    file_code = parts[1]

    # R41 P1-8: 高风险操作必须走 CommandBus(强制 RBAC + 审计 + 幂等)
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_delete_file_command,
    )
    cb_principal = CBPrincipal(id=user.id, name=user.username or "", source="bot")
    command = make_delete_file_command(file_code=file_code)
    bus = CommandBus()
    cb_result = await bus.execute(command, cb_principal)

    if cb_result.approval_required:
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s18', cb_result_approval_id=cb_result.approval_id, file_code=file_code),
            reply_markup=back_kb,
        )
        return
    if not cb_result.success:
        await query.edit_message_text(
            _i18n_t('bot.admin_bot.callback.s19', cb_result_error=cb_result.error),
            reply_markup=back_kb,
        )
        return
    # 失效缓存(handler 已更新 CRDB,此处仅清进程内缓存)
    try:
        invalidate_file_record(file_code)
    except Exception:
        pass
    await query.edit_message_text(_i18n_t('bot.admin_bot.callback.s4', file_code=file_code), reply_markup=back_kb)