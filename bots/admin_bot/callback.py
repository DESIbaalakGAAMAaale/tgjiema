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
        await query.answer("⛔ 无权限", show_alert=True)
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
        text = f"📁 文件列表 (第1/{total_pages}页，共{total}个)\n\n"
        for f in files:
            status_icon = "✅" if f.get("status") == "active" else "🗑️"
            fc = f.get("file_code", "N/A")
            uploader = f.get("uploader_id", "?")
            text += f"{status_icon} {fc} (上传者:{uploader})\n"
        if total_pages > 1:
            text += "\n使用 /files 2 查看下一页"
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
            text = "⏳ 中继正在等待验证码\n\nTelegram 已发送 6 位验证码到中继账号的已登录客户端，请查看并使用 /relay_code 提交。"
        else:
            text = "✅ 中继当前不需要验证码。"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:relay_whitelist":
        from database import get_relay_whitelist
        wl = await get_relay_whitelist()
        if wl:
            text = "🔐 中继白名单\n\n" + "\n".join(f"  • {uid}" for uid in sorted(wl))
        else:
            text = "🔐 中继白名单\n\n❌ 白名单为空（添加中继账号时会自动加入）\n\n可通过 /relay_whitelist add <用户ID> 手动添加"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:collector_whitelist":
        from database import get_collector_whitelist
        wl = await get_collector_whitelist()
        if wl:
            text = "📦 采集器白名单\n\n" + "\n".join(f"  • {uid}" for uid in sorted(wl))
        else:
            text = "📦 采集器白名单\n\n❌ 白名单为空\n\n点击下方「➕ 添加采集器」按钮添加"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:settings":
        text = await _get_configs_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:code_routes":
        routes = await get_all_code_bot_routes()
        regex_routes = await get_all_code_bot_routes_regex()
        if not routes and not regex_routes:
            text = "📭 尚未配置文件码路由。"
        else:
            text = "🗺️ 文件码路由表\n\n"
            if routes:
                text += "【前缀路由】\n"
                for prefix in sorted(routes.keys()):
                    text += f"  • `{prefix}` → @{routes[prefix]}\n"
            if regex_routes:
                if routes:
                    text += "\n"
                text += "【正则路由】\n"
                for rid, bot, pattern in regex_routes:
                    text += f"  • [{rid}] `{pattern}` → @{bot}\n"
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

    elif data == "action:topology":
        text = await _get_topology_text()
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:spare_list":
        spares = await list_spare_pool()
        if not spares:
            text = "📭 备用池为空\n\n使用 /spare_add 添加备用频道。"
        else:
            text = "🔄 备用池频道列表\n\n"
            for s in spares:
                used = "🔴已用" if s.get("is_used") else "🟢可用"
                acc = s.get("account_name") or "通用"
                text += f"  {used} {s['channel_id']} — {acc}\n"
            text += f"\n共 {len(spares)} 个备用频道"
        await query.edit_message_text(text, reply_markup=back_kb)

    elif data == "action:rotation_view":
        keys = [
            ("rotation_active_window_size", "active_window_size", "活跃窗口大小"),
            ("rotation_files_per_slot", "files_per_slot", "每频道文件数"),
            ("rotation_time_per_slot", "time_per_slot", "每频道时间(秒)"),
        ]
        text = "🔄 轮转配置\n\n"
        for db_key, fallback_key, label in keys:
            val = await get_rotation_config(db_key)
            if val is None:
                val = str(getattr(settings, f"ROTATION_{fallback_key.upper()}", "—"))
            text += f"  {label}: {val}\n"
        text += "\n使用 /rotation_set 修改配置"
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
                "🗺️ 新增文件码前缀路由\n\n请输入文件码前缀（例如：qqfile）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "remove_code_route": (
                "remove_code_route:prefix",
                "🗺️ 删除文件码前缀路由\n\n请输入要删除的路由前缀（例如：qqfile）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "add_code_route_regex": (
                "add_code_route_regex:pattern",
                "🗺️ 新增文件码正则路由\n\n"
                "用于 40位hash / emoji 等非前缀式第三方码。\n\n"
                "请输入正则表达式：\n\n"
                "示例：\n"
                "• 40位hex码：^[a-f0-9]{40}$\n"
                "• emoji码：^[\\U0001F000-\\U0001FAFF]+$\n"
                "• 自定义长度：^[a-zA-Z0-9]{32}$\n\n"
                "支持 \\Uxxxxxxxx 和 \\uxxxx 转义序列。\n\n"
                "❌ 如需取消请点击下方按钮。"
            ),
            "remove_code_route_regex": (
                "remove_code_route_regex:id",
                "🗺️ 删除文件码正则路由\n\n请输入要删除的路由 ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
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
            # 中继
            "relay_code": (
                "relay_code:code",
                "🔑 提交验证码\n\n请输入 Telegram 发送的 6 位验证码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "relay_add": (
                "relay_add:phone",
                "➕ 添加中继账号\n\n请输入手机号(含区号,如 +8613800138000)：\n\n登录流程:输入手机号 → 收到验证码 → 输入验证码 → 如有二步验证则输入密码 → 成功后写入\n❌ 如需取消请点击下方按钮。"
            ),
            "relay_password": (
                "relay_password:password",
                "🔒 提交二步验证密码\n\n请输入该中继账号的二步验证密码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "relay_remove": (
                "relay_remove:phone",
                "➖ 移除中继账号\n\n请输入要移除的中继账号手机号(含区号)：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "collector_wl_add": (
                "collector_wl_add:user_id",
                "➕ 添加采集器白名单\n\n请输入 Telegram 用户ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "collector_wl_remove": (
                "collector_wl_remove:user_id",
                "➖ 移除采集器白名单\n\n请输入要移除的 Telegram 用户ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "set_access_limit": (
                "set_access_limit:code",
                "🔢 设置访问次数限制\n\n请输入文件码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "cell_add": (
                "cell_add:slot_id",
                "➕ 添加频道槽位\n\n请输入槽位ID(如 a3、s3a)：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "cell_remove": (
                "cell_remove:slot_id",
                "➖ 移除频道槽位\n\n请输入要移除的槽位ID(如 s3a)：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 系统配置
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
            # 备用池
            "spare_add": (
                "spare_add:channel_id",
                "🔄 添加备用频道\n\n请输入频道ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "spare_remove": (
                "spare_remove:channel_id",
                "🔄 移除备用频道\n\n请输入要移除的频道ID：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 轮转配置
            "rotation_set": (
                "rotation_set:key",
                "⏳ 设置轮转参数\n\n请选择参数：\n"
                "  active_window_size — 活跃窗口大小（每组几个活跃频道）\n"
                "  files_per_slot — 每频道文件数后切换\n"
                "  time_per_slot — 每频道活跃时间（秒）后切换\n\n"
                "请输入参数名：\n\n❌ 如需取消请点击下方按钮。"
            ),
        }

        entry = prompts.get(action)
        if entry:
            state, prompt = entry
            await _conv_start(update, context, state, prompt)
        else:
            await query.edit_message_text(f"❌ 未知操作：{action}", reply_markup=back_kb)

    # ─── 举报处理 ──────────────────────────────────────────────
    elif data.startswith("report:"):
        await _handle_report_action(update, context, data)

    elif data == "conv:cancel":
        await _conv_end(context)
        await query.edit_message_text(
            "❌ 操作已取消。",
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
        await query.answer("⛔ 无权限", show_alert=True)
        return

    if data == "report:ignore":
        await query.edit_message_text(
            query.message.text + "\n\n✅ 已忽略",
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
                await query.answer("参数缺失", show_alert=True)
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
                await _notify_reporter(reporter_id_str, source_bot, "您的举报已受理生效，违规用户已被封禁。")

        elif action == "report:detach":
            if len(parts) < 2:
                await query.answer("参数缺失", show_alert=True)
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
                await _notify_reporter(reporter_id_str, source_bot, "您的举报已受理生效，文件已经移除。")

        elif action == "report:block":
            if len(parts) < 3:
                await query.answer("参数缺失", show_alert=True)
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
        await query.answer("⛔ 无权限", show_alert=True)
        return

    back_kb = InlineKeyboardMarkup(BACK_BTN)

    if data == "restore:cancel":
        await query.edit_message_text("❌ 恢复操作已取消。", reply_markup=back_kb)
        return

    # restore:confirm|<seq>|<0或1>|table:xxx,yyy  (merge标志和table部分均可选)
    if not data.startswith("restore:confirm|"):
        await query.edit_message_text("❌ 未知恢复操作", reply_markup=back_kb)
        return

    parts = data.split("|")
    if len(parts) < 2:
        await query.edit_message_text("❌ 参数缺失", reply_markup=back_kb)
        return

    try:
        seq = int(parts[1])
    except ValueError:
        await query.edit_message_text("❌ 序号无效", reply_markup=back_kb)
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
    mode_label = "增量补充" if merge else "覆盖恢复"
    await query.edit_message_text(f"⏳ 正在提交{mode_label}恢复请求,请稍候...")

    from services.db_backup import list_backups
    try:
        backups = await list_backups()
    except Exception as e:
        await query.edit_message_text(f"❌ 读取备份列表失败: {e}", reply_markup=back_kb)
        return

    if not backups or seq < 1 or seq > len(backups):
        await query.edit_message_text("❌ 备份序号无效", reply_markup=back_kb)
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
            f"❌ 提交恢复审批失败: {e}\n\n备份文件: `{key}`",
            reply_markup=back_kb,
        )
        return

    if result.approval_required:
        # 灾备恢复必须审批,告知用户审批 ID
        await query.edit_message_text(
            f"⏳ {mode_label}恢复已提交审批,审批通过后自动执行\n\n"
            f"审批 ID: {result.approval_id}\n"
            f"备份文件: `{key}`\n"
            f"恢复模式: {mode_label}\n"
            f"操作者: {user.id}",
            reply_markup=back_kb,
        )
        return

    if result.success:
        # 不应到达此处(restore_backup 必须审批),但保持健壮性
        await query.edit_message_text(
            f"✅ 恢复已执行\n\n备份文件: `{key}`",
            reply_markup=back_kb,
        )
    else:
        await query.edit_message_text(
            f"❌ 提交恢复审批失败: {result.error}\n\n备份文件: `{key}`",
            reply_markup=back_kb,
        )


# ─── 文件删除二次确认处理 ──────────────────────────────────────────

async def _handle_delete_file_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """处理文件删除的二次确认/取消按钮(P2-8)。"""
    query = update.callback_query
    user = update.effective_user
    if not user or user.id != AUTHORIZED_USER_ID:
        await query.answer("⛔ 无权限", show_alert=True)
        return

    back_kb = InlineKeyboardMarkup(BACK_BTN)

    if data.startswith("delfile_cancel|"):
        await query.edit_message_text("❌ 删除操作已取消。", reply_markup=back_kb)
        return

    # delfile|{file_code}
    parts = data.split("|", 1)
    if len(parts) < 2:
        await query.edit_message_text("❌ 参数缺失", reply_markup=back_kb)
        return
    file_code = parts[1]

    files_col = get_file_records_col()
    result = await files_col.update_one(
        {"file_code": file_code},
        {"$set": {"status": "deleted"}},
    )
    if result.matched_count == 0:
        await query.edit_message_text(f"❌ 文件码 {file_code} 不存在", reply_markup=back_kb)
        return
    # 失效缓存
    try:
        invalidate_file_record(file_code)
    except Exception:
        pass
    await query.edit_message_text(f"✅ 文件 {file_code} 已删除", reply_markup=back_kb)