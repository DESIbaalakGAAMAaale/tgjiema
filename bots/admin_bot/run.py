import asyncio
from loguru import logger
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from .menus import TOKEN, AUTHORIZED_USER_ID
from utils.monitor import metrics
from utils.task_utils import create_safe_task
from .handlers import (
    start, status, health, user_detail, users_list, set_level, ban_user, unban_user,
    set_quota, set_external_quota, file_detail, files_list, delete_file, logs,
    relay_code, relay_set_api, relay_pending, relay_list, relay_add, relay_remove,
    relay_reset_stats, settings_view, set_storage_channel,
    set_file_prefix, set_force_join, set_username, set_quota_default, set_r2,
    set_db_backup, factory_reset, purge_channel, add_code_route, remove_code_route,
    list_code_routes, set_bot_interval, remove_bot_interval, list_bot_intervals,
    spare_add, spare_remove, spare_list, rotation_set, rotation_view, topology,
    relay_whitelist, collector_whitelist,
    cancel_conversation, help_command,
)
from .callback import menu_callback
from .conversation import handle_conversation


async def _init():
    """数据库初始化。"""
    from database import init_db
    await init_db()


async def _async_main():
    if not TOKEN:
        logger.warning("管理员机器人 Token 未配置（ADMIN_BOT_TOKEN），跳过启动")
        return
    if not AUTHORIZED_USER_ID:
        logger.warning("管理员 Telegram ID 未配置（ADMIN_TELEGRAM_ID），跳过启动")
        return

    logger.info("启动管理员机器人...")

    await _init()
    from database.cache_store import report_bot_heartbeat
    await report_bot_heartbeat("admin_bot")
    await metrics.ping_bot("admin_bot")

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
    app.add_handler(CommandHandler("relay_code", relay_code))
    app.add_handler(CommandHandler("relay_set_api", relay_set_api))
    app.add_handler(CommandHandler("relay_pending", relay_pending))
    app.add_handler(CommandHandler("relay_list", relay_list))
    app.add_handler(CommandHandler("relay_add", relay_add))
    app.add_handler(CommandHandler("relay_remove", relay_remove))
    app.add_handler(CommandHandler("relay_reset_stats", relay_reset_stats))
    app.add_handler(CommandHandler("settings", settings_view))
    app.add_handler(CommandHandler("set_storage_channel", set_storage_channel))
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
    app.add_handler(CommandHandler("spare_add", spare_add))
    app.add_handler(CommandHandler("spare_remove", spare_remove))
    app.add_handler(CommandHandler("spare_list", spare_list))
    app.add_handler(CommandHandler("rotation_set", rotation_set))
    app.add_handler(CommandHandler("rotation_view", rotation_view))
    app.add_handler(CommandHandler("topology", topology))
    app.add_handler(CommandHandler("relay_whitelist", relay_whitelist))
    app.add_handler(CommandHandler("collector_whitelist", collector_whitelist))
    app.add_handler(CommandHandler("cancel", cancel_conversation))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_conversation))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu:|action:|usage:|interactive:|conv:|report:)"))

    async def health_ping():
        while True:
            await metrics.ping_bot("admin_bot")
            await report_bot_heartbeat("admin_bot")
            await asyncio.sleep(30)

    create_safe_task(health_ping(), name="health-ping")

    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            stop_event = asyncio.Event()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await app.updater.stop()
            finally:
                await app.stop()


def run():
    """启动 Admin Bot (使用 asyncio.run 标准模式)"""
    import asyncio
    asyncio.run(_async_main())