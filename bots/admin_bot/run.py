import asyncio
import os
from services.sink_adapters.telegram_helpers import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from loguru import logger


from .menus import TOKEN, AUTHORIZED_USER_ID
from utils.monitor import metrics
from utils.task_utils import create_safe_task
from .handlers import (
    start, status, health, user_detail, users_list, set_level, ban_user, unban_user,
    set_quota, set_external_quota, file_detail, files_list, delete_file, logs,
    relay_code, relay_password, relay_pending, relay_list, relay_add, relay_remove,
    relay_reset_stats, settings_view,
    set_file_prefix, set_force_join, set_username, set_quota_default, set_r2,
    set_db_backup, factory_reset, purge_channel, add_code_route, remove_code_route,
    list_code_routes, set_bot_interval, remove_bot_interval, list_bot_intervals,
    spare_add, spare_remove, spare_list, rotation_set, rotation_view, topology,
    relay_whitelist, collector_whitelist, cell_add, cell_remove,
    cancel_conversation, help_command, restore, set_access_limit,
    # R40 新增管理命令
    cmd_reports, cmd_takedown, cmd_ban_user, cmd_unban_user,
    cmd_pending_approvals, cmd_approve, cmd_reject, cmd_roles,
    cmd_assign_role, cmd_maintenance, cmd_repair_console, cmd_backups,
    cmd_ru_report, cmd_tasks,
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

    # R48 P1-b: 每次 Bot 启动时显式触发 production secret 检查(fail-closed)
    from services.button_security import validate_production_config
    validate_production_config()

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
    app.add_handler(CommandHandler("set_access_limit", set_access_limit))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("relay_code", relay_code))
    app.add_handler(CommandHandler("relay_password", relay_password))
    app.add_handler(CommandHandler("relay_pending", relay_pending))
    app.add_handler(CommandHandler("relay_list", relay_list))
    app.add_handler(CommandHandler("relay_add", relay_add))
    app.add_handler(CommandHandler("relay_remove", relay_remove))
    app.add_handler(CommandHandler("relay_reset_stats", relay_reset_stats))
    app.add_handler(CommandHandler("settings", settings_view))
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
    app.add_handler(CommandHandler("cell_add", cell_add))
    app.add_handler(CommandHandler("cell_remove", cell_remove))
    app.add_handler(CommandHandler("relay_whitelist", relay_whitelist))
    app.add_handler(CommandHandler("collector_whitelist", collector_whitelist))
    app.add_handler(CommandHandler("cancel", cancel_conversation))
    app.add_handler(CommandHandler("restore", restore))
    app.add_handler(CommandHandler("help", help_command))
    # R40 新增管理命令
    app.add_handler(CommandHandler("reports", cmd_reports))
    app.add_handler(CommandHandler("takedown", cmd_takedown))
    app.add_handler(CommandHandler("ban_user", cmd_ban_user))
    app.add_handler(CommandHandler("unban_user", cmd_unban_user))
    app.add_handler(CommandHandler("pending_approvals", cmd_pending_approvals))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("assign_role", cmd_assign_role))
    app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    app.add_handler(CommandHandler("repair_console", cmd_repair_console))
    app.add_handler(CommandHandler("backups", cmd_backups))
    app.add_handler(CommandHandler("ru_report", cmd_ru_report))
    # R40 P1-1: 任务中心(查看所有用户任务)
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_conversation))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu:|action:|usage:|interactive:|conv:|report:|restore:)"))

    async def health_ping():
        while True:
            await metrics.ping_bot("admin_bot")
            await report_bot_heartbeat("admin_bot")
            await asyncio.sleep(30)

    create_safe_task(health_ping(), name="health-ping")

    # R71 RC28: CI 模式跳过 async with app: — app.start() → bot.initialize()
    # → get_me() 会用占位符 token 调用 Telegram API → 401 → 崩溃 → restart loop。
    _is_ci = (
        os.getenv("CI", "").lower() in ("true", "1")
        or os.getenv("GITHUB_ACTIONS", "").lower() in ("true", "1")
    )
    from run_all import _set_stop_event
    stop_event = asyncio.Event()
    _set_stop_event(stop_event)

    if _is_ci:
        logger.warning("[Admin] CI 模式: 跳过 Application 启动(占位符 token)")
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[Admin] 收到停止信号,正在优雅关闭...")
            logger.info("[Admin] 优雅关闭完成")
    else:
        async with app:
            await app.start()
            await app.updater.start_polling()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                logger.info("[Admin] 收到停止信号,正在优雅关闭 polling...")
                try:
                    await asyncio.wait_for(app.updater.stop(), timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning("[Admin] polling 关闭超时(15s),强制继续")
                except Exception as e:
                    logger.warning(f"[Admin] polling 关闭异常: {e}")
                try:
                    await asyncio.wait_for(app.stop(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning("[Admin] app.stop 超时(10s),强制继续")
                except Exception as e:
                    logger.warning(f"[Admin] app.stop 异常: {e}")
                # 取消所有剩余后台任务,防止进程卡死无法退出
                import os as _os
                pending = asyncio.all_tasks() - {asyncio.current_task()}
                for task in pending:
                    task.cancel()
                if pending:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=5.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(f"[Admin] {len(pending)} 个后台任务未在 5s 内完成")
                logger.info("[Admin] 优雅关闭完成")
                # 如果仍有未完成任务,强制退出避免 systemd SIGKILL
                remaining = asyncio.all_tasks() - {asyncio.current_task()}
                if any(not t.done() for t in remaining):
                    logger.warning(f"[Admin] {sum(1 for t in remaining if not t.done())} 个任务仍运行,强制退出")
                    _os._exit(0)


def run():
    """启动 Admin Bot (使用 asyncio.run 标准模式)"""
    import asyncio
    asyncio.run(_async_main())