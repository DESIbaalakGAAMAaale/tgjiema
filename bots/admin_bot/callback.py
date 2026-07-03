from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from loguru import logger

from database import (
    get_file_records_col,
    get_all_code_bot_routes,
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
            # 中继
            "relay_code": (
                "relay_code:code",
                "🔑 提交验证码\n\n请输入 Telegram 发送的 6 位验证码：\n\n❌ 如需取消请点击下方按钮。"
            ),
            "relay_set_api": (
                "relay_set_api:phone",
                "⚙️ 配置中继账号\n\n请输入手机号(含区号,如 +8613800138000)：\n\n❌ 如需取消请点击下方按钮。"
            ),
            # 系统配置
            "set_storage_channel": (
                "set_storage_channel:id",
                "📺 设置主存储频道\n\n请输入频道ID（数字）：\n\n❌ 如需取消请点击下方按钮。"
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
        _conv_end(context)
        await query.edit_message_text(
            "❌ 操作已取消。",
            reply_markup=back_kb,
        )


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

    try:
        if action == "report:ban":
            if len(parts) < 2:
                await query.answer("参数缺失", show_alert=True)
                return
            uid = int(parts[1])
            await update_user_and_invalidate(uid, {
                "$set": {"is_banned": True, "updated_at": _dt.datetime.now(_dt.UTC).isoformat()},
            })
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已封禁用户 {uid}",
                reply_markup=None,
            )

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
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已脱钩文件码 {file_code}",
                reply_markup=None,
            )

        elif action == "report:block":
            if len(parts) < 3:
                await query.answer("参数缺失", show_alert=True)
                return
            file_code = parts[1]
            reporter_id = int(parts[2])
            # PRE-06: 用 update_file_record_and_invalidate 一次性双写 CRDB+SQLite 并失效内存缓存
            try:
                await update_file_record_and_invalidate(file_code, {"$push": {"blocked_users": reporter_id}})
            except Exception as e:
                logger.error(f"[Admin][report:block] 双写失败: {e}")
                invalidate_file_record(file_code)
            await query.edit_message_text(
                query.message.text + f"\n\n✅ 已限制举报人 {reporter_id} 解码 {file_code}",
                reply_markup=None,
            )

    except Exception as e:
        logger.error(f"[Admin][report] 操作失败: {e}")
        await query.answer(f"操作失败: {e}", show_alert=True)