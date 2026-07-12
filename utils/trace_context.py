"""R39 P2-6: trace_id / correlation_id 上下文管理器。

为所有异步操作提供请求级追踪标识,贯穿日志、指标、异常,
便于跨服务/跨模块关联同一业务流程。

设计:
- 使用 contextvars 存储当前请求的 trace_id / correlation_id
- 提供 async 上下文管理器 with_trace() 设置/清理
- 提供 get_trace_id() / get_correlation_id() 在任意位置读取
- 与 loguru 集成: 日志格式中可插入 trace_id 字段

安全:
- trace_id 使用 uuid4(随机,不可猜测,不泄露业务信息)
- 严禁在日志中记录 token / 完整用户名 / 手机号 / 文件码(见 R39 P2-6)
- correlation_id 可由调用方传入(用于跨服务关联),或自动生成

用法:
    from utils.trace_context import with_trace, get_trace_id

    async with with_trace():
        logger.info("处理上传")  # 日志自动包含 trace_id
        # 调用其他模块,trace_id 自动传递
        await process_file()

    # 或指定 correlation_id(跨服务关联)
    async with with_trace(correlation_id="upload-session-abc123"):
        ...
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

# R39 P2-6: 使用 contextvars 保证异步任务隔离(每个 asyncio Task 独立副本)
_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None,
)
_correlation_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "correlation_id", default=None,
)


def get_trace_id() -> Optional[str]:
    """R39 P2-6: 获取当前上下文的 trace_id(无则返回 None)。"""
    return _trace_id_var.get()


def get_correlation_id() -> Optional[str]:
    """R39 P2-6: 获取当前上下文的 correlation_id(无则返回 None)。"""
    return _correlation_id_var.get()


def _generate_trace_id() -> str:
    """R39 P2-6: 生成新的 trace_id(UUID4,32 字符无连字符)。"""
    return uuid.uuid4().hex


@asynccontextmanager
async def with_trace(
    trace_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> AsyncIterator[None]:
    """R39 P2-6: 异步上下文管理器,设置 trace_id / correlation_id。

    Args:
        trace_id: 指定 trace_id(不传则自动生成 UUID4)
        correlation_id: 跨服务关联 ID(不传则为 None,可在上下文内通过 set_correlation_id 设置)

    用法:
        async with with_trace():
            # 此范围内 get_trace_id() 返回非 None
            logger.info("开始处理")
            await some_async_op()

    Yields:
        None(仅设置上下文变量)
    """
    tid = trace_id or _generate_trace_id()
    # 保存当前 token 以便恢复(支持嵌套 with_trace)
    trace_token = _trace_id_var.set(tid)
    corr_token = _correlation_id_var.set(correlation_id)
    try:
        yield
    finally:
        # 恢复上层上下文(支持嵌套)
        _trace_id_var.reset(trace_token)
        _correlation_id_var.reset(corr_token)


def set_correlation_id(correlation_id: str) -> None:
    """R39 P2-6: 在已存在的 trace 上下文内设置/更新 correlation_id。

    用于: trace 开始时未知 correlation_id,后续从外部消息中提取后设置。

    Args:
        correlation_id: 跨服务关联 ID
    """
    _correlation_id_var.set(correlation_id)


def get_log_context() -> dict[str, str]:
    """R39 P2-6: 获取当前日志上下文字典(供 loguru extra 使用)。

    返回示例:
        {"trace_id": "abc123...", "correlation_id": "upload-xyz"}

    可与 loguru 配合:
        logger.bind(**get_log_context()).info("消息")
    """
    ctx: dict[str, str] = {}
    tid = _trace_id_var.get()
    if tid:
        ctx["trace_id"] = tid
    cid = _correlation_id_var.get()
    if cid:
        ctx["correlation_id"] = cid
    return ctx


def format_trace_prefix() -> str:
    """R39 P2-6: 生成日志前缀字符串(无 trace 时返回空串)。

    用于不便使用 logger.bind() 的场景,手动拼接前缀:
        logger.info(f"{format_trace_prefix()} 处理上传")

    返回示例:
        "[trace=abc123...] " 或 ""
    """
    parts: list[str] = []
    tid = _trace_id_var.get()
    if tid:
        parts.append(f"trace={tid[:8]}")
    cid = _correlation_id_var.get()
    if cid:
        parts.append(f"corr={cid[:12]}")
    if not parts:
        return ""
    return f"[{' '.join(parts)}] "


# R39 P2-6: 脱敏检查清单 — 以下字段严禁出现在日志中
SENSITIVE_LOG_FIELDS = frozenset({
    "token", "bot_token", "SECRET_TOKEN", "BOT_TOKEN",
    "api_key", "API_KEY", "CRDB_CLOUD_API_KEY",
    "r2_secret_key", "R2_SECRET_KEY", "r2_access_key", "R2_ACCESS_KEY",
    "password", "ADMIN_PASSWORD", "plaintext_password",
    "phone", "phone_number", "api_hash", "api_id",
    "session_string", "session_file",
    # 文件码完整值不应出现在异常日志中(只记录前 4 字符)
    "file_code_full",
})


def redact_sensitive(value: str, visible_prefix: int = 4) -> str:
    """R39 P2-6: 脱敏敏感值,只保留前 visible_prefix 字符 + ***。

    用于: 必须记录某个字段但需脱敏的场景(如 file_code、user_id 部分)

    Args:
        value: 原始值
        visible_prefix: 保留前缀长度(默认 4)

    Returns:
        脱敏后的字符串(如 "abcd***")
    """
    if not value:
        return ""
    if len(value) <= visible_prefix:
        return "***"
    return value[:visible_prefix] + "***"


def should_redact(field_name: str) -> bool:
    """R39 P2-6: 判断字段名是否敏感(应脱敏或不记录)。"""
    return field_name.lower() in {f.lower() for f in SENSITIVE_LOG_FIELDS}
