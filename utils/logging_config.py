"""统一结构化日志配置基线。

使用 loguru 配置统一格式（时间/级别/模块/消息），默认人类可读，
可选 JSON 格式。在 run_all.py 启动早期调用 setup_logging()。

与 L3 (monitor.increment) 一致：monitor 指标经 loguru 输出。
"""

import sys

from loguru import logger

# 人类可读格式（默认）
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{module}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{message}"
)

# JSON 格式（可选，用于日志聚合系统）
_JSON_FORMAT = (
    '{{"time":"{time:YYYY-MM-DD HH:mm:ss.SSS}",'
    '"level":"{level}",'
    '"module":"{module}",'
    '"function":"{function}",'
    '"line":{line},'
    '"message":"{message}"}}'
)


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """配置 loguru 统一日志格式。

    移除默认 handler，添加格式化的 stderr handler。
    若 run_all.py 后续调用 logger.add() 添加文件 handler，
    应传入 format=LOG_FORMAT 以保持格式一致。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        json_format: 是否使用 JSON 格式输出（默认人类可读）
    """
    logger.remove()  # 移除默认 handler，避免重复输出

    fmt = _JSON_FORMAT if json_format else LOG_FORMAT
    logger.add(
        sys.stderr,
        format=fmt,
        level=level,
        colorize=not json_format,
    )
