"""Mon 监控机器人
职责：频道健康监控 + 自动降级调度
与 admin_bot 完全分离，admin 负责管理配置，mon 负责运行时监控。
"""

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from config import settings
from database import init_db, close_db, get_cells_col, get_active_cells
from services.mon import MonScheduler
from utils.monitor import metrics

logger = logging.getLogger("mon_bot")

TOKEN = settings.MON_BOT_TOKEN or settings.ADMIN_BOT_TOKEN
RECOVERY_INTERVAL = getattr(settings, "MON_CHECK_INTERVAL", 60)


class MonBot:
    """Mon 监控机器人（后台轮询模式，不对外提供用户命令）。"""

    def __init__(self):
        self.bot = Bot(token=TOKEN)
        self.scheduler = MonScheduler()
        self._running = False

    async def start(self):
        """启动 Mon 主循环。"""
        await init_db()
        await self.bot.initialize()
        self._running = True
        logger.info("[Mon] 监控机器人已启动")

        while self._running:
            try:
                # 1. 对所有 active + shadow 槽位发心跳
                ok = await self.scheduler.heartbeat_all(self.bot)
                logger.info(f"[Mon] 心跳: {ok} 个槽位正常")

                # 2. 降级检查
                alerts = await self.scheduler.run_degrade_check()
                if alerts:
                    for msg in alerts:
                        logger.warning(msg)
                        metrics.increment("mon.degrade")

                # 3. 报告当前拓扑状态
                await self._report_status()

            except Exception as e:
                logger.error(f"[Mon] 调度异常: {e}")

            await asyncio.sleep(RECOVERY_INTERVAL)

    async def stop(self):
        """停止 Mon。"""
        self._running = False
        await self.bot.shutdown()
        await close_db()
        logger.info("[Mon] 监控机器人已停止")

    async def _report_status(self):
        """输出当前拓扑健康状态到日志。"""
        cells = await get_active_cells()
        active_count = len(cells)
        total = len(await get_cells_col().find({}))
        lost = len(await get_cells_col().find({"status": "lost"}))
        r100 = len(await get_cells_col().find({"status": "r100"}))
        logger.info(
            f"[Mon] 拓扑: {active_count}/{total} 活跃, {lost} 失联, {r100} R100"
        )


async def run_mon():
    """启动 Mon 监控机器人。"""
    mon = MonBot()
    try:
        await mon.start()
    except KeyboardInterrupt:
        await mon.stop()
    except Exception as e:
        logger.error(f"[Mon] 异常退出: {e}")
        await mon.stop()


if __name__ == "__main__":
    asyncio.run(run_mon())