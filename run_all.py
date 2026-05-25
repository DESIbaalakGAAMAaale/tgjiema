import multiprocessing
import os
import signal
import sys
import time

from loguru import logger
from config import settings


def run_upload_bot():
    from bots.upload_bot import run
    run()


def run_decoder_bot():
    from bots.decoder_bot import run
    run()


def run_sender_bot():
    from bots.sender_bot import run
    run()


def run_backup_bot_1():
    from bots.backup_bot import run_backup_1
    run_backup_1()


def run_backup_bot_2():
    from bots.backup_bot import run_backup_2
    run_backup_2()


def run_backup_bot_3():
    from bots.backup_bot import run_backup_3
    run_backup_3()


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
    "upload": run_upload_bot,
    "decoder": run_decoder_bot,
    "sender": run_sender_bot,
    "backup1": run_backup_bot_1,
    "backup2": run_backup_bot_2,
    "backup3": run_backup_bot_3,
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