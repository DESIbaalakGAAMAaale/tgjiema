"""环形冗余架构 v2 运行入口
启动 5 个主进程:up / idx / dsp / mon / admin_bot
+ admin web + db_backup
+ file_bot 已独立部署至 CF Workers,不在此启动
启动时自动初始化拓扑(无需手动运行 seed_topology.py)
+ 子进程崩溃自动重启(带限流保护,永不删除进程记录)

支持两种运行模式:
  python run_all.py               → 多进程模式(所有 Bot,内部监控重启)
  python run_all.py --standalone up  → 独立模式(单 Bot 直接运行,交给 systemd 管理)
"""

import multiprocessing
import os
import platform
import signal
import sys
import time
from collections import defaultdict

from loguru import logger
from config import settings

try:
    import uvloop
    uvloop.install()
    print("[RunAll] uvloop 已启用")
except ImportError:
    print("[RunAll] uvloop 未安装,使用默认事件循环")


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


def _shutdown(processes: dict):
    """优雅关闭所有进程。"""
    logger.info("正在优雅关闭进程...")
    for name, p in processes.items():
        if p.is_alive():
            try:
                if platform.system() == "Windows":
                    p.terminate()
                else:
                    os.kill(p.pid, signal.SIGINT)
            except Exception:
                p.terminate()
    for name, p in processes.items():
        p.join(timeout=5)
    for name, p in processes.items():
        if p.is_alive():
            p.terminate()
            p.join(timeout=2)
    logger.info("所有进程已关闭")


def _auto_seed():
    """启动前自动初始化拓扑(静默,不交互)。
    仅用于多进程模式。独立模式下各 Bot 自行调用 init_db()，
    拓扑已由部署脚本预初始化，无需重复执行。
    auto_seed() 是幂等的（cells 已存在则跳过），失败说明 CRDB 不可达，
    应直接退出而非静默继续。
    """
    import asyncio

    try:
        from admin.seed_topology import auto_seed
        asyncio.run(auto_seed())
        logger.info("[seed] 拓扑初始化完成")
    except Exception as e:
        logger.error(f"[seed] 拓扑初始化失败（CRDB 可能不可达），退出: {e}")
        sys.exit(1)


def _monitor_and_restart(processes: dict, running_flag: multiprocessing.Value):
    """监控子进程,崩溃后自动重启(带限流保护,永不删除进程记录)。
    每 5 分钟最多重启 3 次,超过后记录日志但不删除,冷却期后重置计数。
    """
    # 重启计数:{name: [(timestamp, ...)]}
    restart_history: dict[str, list[float]] = defaultdict(list)
    max_restart = getattr(settings, "MAX_RESTART_COUNT", 3)
    restart_window = getattr(settings, "MAX_RESTART_WINDOW", 300)
    # 冷却期:10 分钟
    cooldown_period = settings.RESTART_COOLDOWN

    while running_flag.value:
        for name, p in list(processes.items()):
            if not p.is_alive():
                exitcode = p.exitcode
                logger.warning(f"[RunAll] {name} 进程已退出 (exitcode={exitcode})")

                if exitcode is not None and exitcode == 0:
                    logger.info(f"[RunAll] {name} 正常退出 (exitcode=0),从监控列表移除")
                    processes.pop(name, None)
                    continue

                # 限流检查:窗口内重启次数
                now = time.time()
                history = restart_history[name]
                history[:] = [t for t in history if now - t < restart_window]
                if len(history) >= max_restart:
                    # 检查冷却期:如果最后一次重启已超过冷却期,重置计数
                    last_restart = history[-1] if history else 0
                    if now - last_restart > cooldown_period:
                        logger.info(f"[RunAll] {name} 冷却期已过,重置重启计数")
                        history.clear()
                    else:
                        logger.warning(
                            f"[RunAll] {name} 在 {restart_window}s 内重启 {len(history)} 次,"
                            f"已达上限 {max_restart},进入冷却期({cooldown_period}s),暂停自动重启"
                        )
                        time.sleep(5)
                        continue

                history.append(now)
                logger.info(f"[RunAll] {name} 3秒后自动重启 (第{len(history)}次)")
                time.sleep(3)

                if name in BOT_RUNNERS:
                    new_p = multiprocessing.Process(
                        target=BOT_RUNNERS[name], name=name, daemon=True
                    )
                    new_p.start()
                    processes[name] = new_p
                    logger.info(f"[RunAll] {name} 已重启 (PID: {new_p.pid})")

        time.sleep(5)


def _run_standalone(name: str):
    """独立模式:直接在主进程运行单个 Bot,崩溃后进程退出,交给 systemd 重启。"""
    logger.info(f"[Standalone] 启动 {name} (独立模式,由 systemd 管理)")
    runner = BOT_RUNNERS[name]
    runner()


def main():
    logger.add(
        "logs/tgjiema_{time}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.LOG_LEVEL,
    )

    args = sys.argv[1:]

    # ── 启动前自动初始化拓扑（仅多进程模式需要）──
    # 独立模式下各 Bot 自行调用 init_db()，拓扑已预初始化
    if not (args and args[0] == "--standalone"):
        _auto_seed()

    # ── 独立模式:--standalone <bot_name> ──
    if args and args[0] == "--standalone":
        if len(args) < 2:
            logger.error("--standalone 需要指定 bot 名称,例如: python run_all.py --standalone up")
            sys.exit(1)
        bot_name = args[1]
        if bot_name not in BOT_RUNNERS:
            logger.error(f"未知的组件: {bot_name},可用: {list(BOT_RUNNERS.keys())}")
            sys.exit(1)
        _run_standalone(bot_name)
        return

    if not args:
        args = ["all"]

    processes: dict[str, multiprocessing.Process] = {}

    def _start(name, runner):
        p = multiprocessing.Process(target=runner, name=name, daemon=True)
        p.start()
        logger.info(f"启动 {name} (PID: {p.pid})")
        processes[name] = p

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

    # 运行标志(进程间共享),用于控制监控循环退出
    running_flag = multiprocessing.Value('i', 1)

    try:
        _monitor_and_restart(processes, running_flag)
    except KeyboardInterrupt:
        running_flag.value = 0
        _shutdown(processes)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()