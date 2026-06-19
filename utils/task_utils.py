"""安全 asyncio Task 创建工具 — 防止异常静默泄漏。

使用 create_safe_task() 替代 asyncio.create_task(),
确保未捕获的异常被记录到日志而非静默丢弃。
"""

import asyncio
import sys
from loguru import logger


def _handle_task_exception(task: asyncio.Task):
    """Done callback: 记录未捕获的异常,防止静默失败。"""
    try:
        exc = task.exception()
        if exc is not None:
            logger.error(
                f"[Task] 后台任务异常: {task.get_name()}: {exc}",
                exc_info=exc,
            )
    except (asyncio.CancelledError, RuntimeError):
        pass
    except Exception as e:
        logger.error(f"[Task] 获取任务异常时出错: {e}")


def create_safe_task(coro, name: str = None) -> asyncio.Task:
    """创建带异常回调的 asyncio Task,防止异常泄漏。"""
    if sys.version_info >= (3, 11):
        task = asyncio.create_task(coro, name=name)
    else:
        task = asyncio.create_task(coro)
    task.add_done_callback(_handle_task_exception)
    return task