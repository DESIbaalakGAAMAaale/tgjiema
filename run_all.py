import multiprocessing
import asyncio
import sys
import time

from loguru import logger
from config import settings
from database import init_db, close_db


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


def run_admin():
    import uvicorn
    from admin import app
    uvicorn.run(
        app,
        host=settings.ADMIN_WEB_HOST,
        port=settings.ADMIN_WEB_PORT,
        log_level="info",
    )


BOT_RUNNERS = {
    "upload": run_upload_bot,
    "decoder": run_decoder_bot,
    "sender": run_sender_bot,
    "backup1": run_backup_bot_1,
    "backup2": run_backup_bot_2,
    "backup3": run_backup_bot_3,
    "admin": run_admin,
}


async def _init_resources():
    await init_db()
    logger.info("MongoDB 数据库连接初始化完成")


def main():
    logger.add(
        "logs/tgjiema_{time}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.LOG_LEVEL,
    )

    asyncio.run(_init_resources())

    args = sys.argv[1:]
    if not args:
        args = ["all"]

    if "all" in args:
        processes = []
        for name, runner in BOT_RUNNERS.items():
            p = multiprocessing.Process(target=runner, name=name, daemon=True)
            p.start()
            processes.append(p)
            logger.info(f"启动 {name} (PID: {p.pid})")
            time.sleep(1)

        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
            for p in processes:
                p.terminate()
            for p in processes:
                p.join(timeout=5)
            asyncio.run(close_db())
            logger.info("所有进程已关闭")
    else:
        processes = []
        for arg in args:
            if arg in BOT_RUNNERS:
                p = multiprocessing.Process(
                    target=BOT_RUNNERS[arg], name=arg, daemon=True
                )
                p.start()
                processes.append(p)
                logger.info(f"启动 {arg} (PID: {p.pid})")
                time.sleep(1)
            else:
                logger.warning(f"未知的组件: {arg}")

        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
            for p in processes:
                p.terminate()
            for p in processes:
                p.join(timeout=5)
            asyncio.run(close_db())
            logger.info("所有进程已关闭")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()