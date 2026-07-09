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
import subprocess
import sys
import time
from collections import defaultdict
from typing import Optional

import asyncio

# 防止双模块导入:run_all.py 作为主脚本运行时加载为 __main__,
# 但子模块用 `from run_all import xxx` 会再次导入 run_all 创建副本,
# 导致全局变量(_stop_event 等)分裂为两份,信号 handler 读到的是 None。
# 此行让 run_all 指向 __main__,确保所有模块共享同一份全局变量。
sys.modules.setdefault('run_all', sys.modules[__name__])

from loguru import logger
from config import settings
from utils.logging_config import setup_logging, LOG_FORMAT

try:
    import uvloop
    uvloop.install()
    logger.info("[RunAll] uvloop 已启用")
except ImportError:
    logger.info("[RunAll] uvloop 未安装,使用默认事件循环")


# 全局停止信号事件,各 Bot 的 _async_main 通过 set 它来优雅退出
_stop_event: Optional[asyncio.Event] = None


def _register_sigterm_handler():
    """在子进程中注册信号处理函数,优雅关闭避免幽灵 polling 连接。

    systemd 默认发 SIGTERM,但 service 配置可能改成 SIGINT,所以两个都注册。
    收到信号后 set 全局 _stop_event,让 _async_main 的 stop_event.wait() 返回,
    从而走 finally 块执行 app.updater.stop() 优雅关闭 polling。
    Windows 无 SIGTERM,使用 SIGBREAK 替代。
    """
    def _signal_handler(signum, frame):
        # 优先 set 事件,让事件循环走正常的 finally 优雅关闭路径
        if _stop_event is not None:
            try:
                _stop_event.set()
                return
            except Exception:
                pass
        # 兜底:事件不可用时退回 KeyboardInterrupt
        raise KeyboardInterrupt

    signals_to_register = []
    if platform.system() == "Windows":
        signals_to_register.append(signal.SIGBREAK)
        signals_to_register.append(signal.SIGINT)
    else:
        signals_to_register.append(signal.SIGTERM)
        signals_to_register.append(signal.SIGINT)

    for sig in signals_to_register:
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, AttributeError, OSError):
            # 非 主线程 或 平台不支持 时跳过,不影响启动
            pass


def _set_stop_event(event: asyncio.Event):
    """由各 Bot 的 _async_main 调用,注册全局停止事件。"""
    global _stop_event
    _stop_event = event


def run_up_bot():
    os.environ["BOT_ROLE"] = "up_bot"
    _register_sigterm_handler()
    from bots.up_bot import run
    run()


def run_idx_bot():
    os.environ["BOT_ROLE"] = "idx_bot"
    _register_sigterm_handler()
    from bots.idx_bot import run
    run()


def run_dsp_bot():
    os.environ["BOT_ROLE"] = "dsp_bot"
    _register_sigterm_handler()
    from bots.dsp_bot import run
    run()


def run_mon_bot():
    os.environ["BOT_ROLE"] = "mon_bot"
    _register_sigterm_handler()
    from bots.mon_bot import run_mon
    import asyncio
    asyncio.run(run_mon())


def run_admin_bot():
    os.environ["BOT_ROLE"] = "admin_bot"
    _register_sigterm_handler()
    from bots.admin_bot import run
    run()


def run_admin():
    os.environ["BOT_ROLE"] = "admin_web"
    # admin(uvicorn)不注册自定义 SIGTERM handler——uvicorn 内部自己管理信号,
    # 自定义 handler 会干扰 uvicorn 的优雅关闭,导致进程无法退出被 SIGKILL。
    import uvicorn
    from admin import app
    uvicorn.run(
        app,
        host=settings.ADMIN_WEB_HOST,
        port=settings.ADMIN_WEB_PORT,
        log_level="info",
    )


def run_db_backup():
    os.environ["BOT_ROLE"] = "db_backup"
    # db_backup 不注册自定义 SIGTERM handler——asyncio.run 内部通过
    # CancelledError 传播信号,自定义 raise KeyboardInterrupt 会绕过清理路径。
    import asyncio
    from services.db_backup import run_db_backup as _run
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("[db_backup] 收到中断信号,已停止")


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
                    # Windows: p.terminate() 是 TerminateProcess 硬杀,不触发 SIGBREAK handler
                    # 改用 CTRL_BREAK_EVENT 通知子进程优雅关闭(需子进程在同一控制台进程组)
                    try:
                        os.kill(p.pid, signal.CTRL_BREAK_EVENT)
                    except (OSError, AttributeError, ValueError):
                        # CTRL_BREAK_EVENT 不可用时退回硬杀(优于让进程残留)
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
    每 5 分钟最多重启 3 次,超过后进入冷却期,冷却期结束后重置计数。
    """
    # 重启计数:{name: [(timestamp, ...)]}
    restart_history: dict[str, list[float]] = defaultdict(list)
    # 冷却截止时间:{name: timestamp},超过此时间后才允许重置计数并重启
    cooldown_until: dict[str, float] = {}
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
                    # 清理历史记录,避免内存泄漏
                    restart_history.pop(name, None)
                    cooldown_until.pop(name, None)
                    continue

                # 限流检查:窗口内重启次数
                now = time.time()
                # 检查是否在冷却期
                if name in cooldown_until and now < cooldown_until[name]:
                    logger.warning(
                        f"[RunAll] {name} 冷却中,剩余 {int(cooldown_until[name] - now)}s"
                    )
                    time.sleep(5)
                    continue
                # 冷却期已过,重置计数
                if name in cooldown_until:
                    cooldown_until.pop(name, None)
                    restart_history[name].clear()
                    logger.info(f"[RunAll] {name} 冷却期已过,重置重启计数")

                history = restart_history[name]
                history[:] = [t for t in history if now - t < restart_window]
                if len(history) >= max_restart:
                    # 进入冷却期
                    cooldown_until[name] = now + cooldown_period
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
    # C12: 统一结构化日志基线（移除默认 handler，添加格式化的 stderr handler）
    setup_logging(level=settings.LOG_LEVEL)

    logger.add(
        "logs/tgjiema_{time}.log",
        format=LOG_FORMAT,
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

    # Windows 下需要 CREATE_NEW_PROCESS_GROUP 才能让 CTRL_BREAK_EVENT 生效
    # Linux 下不需要(用 SIGINT),传 0 即可
    creationflags = 0
    if platform.system() == "Windows":
        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        except AttributeError:
            creationflags = 0

    def _start(name, runner):
        p = multiprocessing.Process(
            target=runner, name=name, daemon=True,
            creationflags=creationflags,
        )
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

    # 主进程也注册 SIGTERM(子进程在各 run_* 函数中自行注册)
    _register_sigterm_handler()

    try:
        _monitor_and_restart(processes, running_flag)
    except KeyboardInterrupt:
        running_flag.value = 0
        _shutdown(processes)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()