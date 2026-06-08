"""环形冗余架构 v2 运行入口
启动 5 个主进程：up / idx / dsp / mon / admin_bot
+ admin web + db_backup
"""

import multiprocessing
import os
import signal
import sys
import time

from loguru import logger
from config import settings


def run_up_bot():
    from bots.up_bot import run
    run()


def run_idx_bot():
    from bots.idx_bot import run
    run()


def run_dsp_bot():
    from bots.dsp_bot import run
    run()


def run_mon_bot():
    from bots.mon_bot import run_mon
    import asyncio
    asyncio.run(run_mon())


def run_admin_bot():
    from bots.admin_bot import run
    run()


def run_admin():
    import uvicorn
    from admin import app
    uvicorn.run(
        app,
        host=settings.ADMIN_WEB_HOST,
        port=settings.ADMIN_WEB_PORT,
        log_level="info",
    )


def run_db_backup():
    import asyncio
    from services.db_backup import run_db_backup as _run
    asyncio.run(_run())


BOT_RUNNERS = {
    "up": run_up_bot,
    "idx": run_idx_bot,
    "dsp": run_dsp_bot,
    "mon": run_mon_bot,
    "admin_bot": run_admin_bot,
    "admin": run_admin,
    "db_backup": run_db_backup,
}


def _shutdown(processes):
    logger.info("正在优雅关闭进程...")
    for p in processes:
        if p.is_alive():
            try:
                os.kill(p.pid, signal.SIGINT)
            except Exception:
                p.terminate()
    for p in processes:
        p.join(timeout=5)
    for p in processes:
        if p.is_alive():
            p.terminate()
            p.join(timeout=2)
    logger.info("所有进程已关闭")


def main():
    logger.add(
        "logs/tgjiema_{time}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.LOG_LEVEL,
    )

    args = sys.argv[1:]
    if not args:
        args = ["all"]

    processes = []

    def _start(name, runner):
        p = multiprocessing.Process(target=runner, name=name, daemon=True)
        p.start()
        logger.info(f"启动 {name} (PID: {p.pid})")
        processes.append(p)

    if "all" in args:
        for name, runner in BOT_RUNNERS.items():
            _start(name, runner)
            time.sleep(1)
    else:
        for arg in args:
            if arg in BOT_RUNNERS:
                _start(arg, BOT_RUNNERS[arg])
                time.sleep(1)
            else:
                logger.warning(f"未知的组件: {arg}")

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        _shutdown(processes)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()