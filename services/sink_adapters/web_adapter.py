"""R64 P1-06: FastAPI/Web sink typed adapter。

整改背景(R64 终审报告 P1-06):
    业务模块禁止在 admin/ 路由中直接构造 ``JSONResponse(content={...: str(e)})``,
    内部异常细节不应进入用户面响应体。本 adapter 强制 ``UserMessage |
    ErrorEnvelope`` 输入,内部异常仅记录到结构化日志,响应体只暴露
    safe params + trace_id。

adapter 职责:
    1. 类型级强制 ``UserMessage | ErrorEnvelope`` 输入(拒绝裸 str)。
    2. 内部经 ``render_for_send`` 渲染为本地化字符串后构造 JSONResponse。
    3. 响应体只包含 ``message`` / ``error_code`` / ``trace_id`` / safe params;
       内部 exception 不进入响应体。
"""
from __future__ import annotations

from typing import Any, Optional, Union

from fastapi.responses import JSONResponse

from services.user_message import (
    ErrorEnvelope,
    UserMessage,
    render_for_send,
)


# ════════════════════════════════════════════════════════════════
# i18n manager 延迟解析(避免模块导入时的循环依赖)
# ════════════════════════════════════════════════════════════════
def _resolve_i18n_manager(i18n_manager: Optional[Any] = None) -> Any:
    """延迟解析 i18n manager。

    优先使用调用方显式传入的 ``i18n_manager``;未传入时回退到
    ``services.i18n.get_i18n_manager()`` 单例。
    """
    if i18n_manager is not None:
        return i18n_manager
    from services.i18n import get_i18n_manager
    return get_i18n_manager()


# ════════════════════════════════════════════════════════════════
# Typed payload 校验
# ════════════════════════════════════════════════════════════════
_MSG_REJECTS_STR = (
    "Web sink adapter 不接受裸 str,请使用 UserMessage.from_key(...) "
    "或 ErrorEnvelope(...) 包装用户可见消息(类型边界在 adapter 层强制)"
)
_MSG_REJECTS_TYPE = (
    "Web sink adapter 仅接受 UserMessage | ErrorEnvelope,实际类型 "
    "{type_name};请使用 UserMessage.from_key(...) 或 ErrorEnvelope(...) "
    "包装用户可见消息"
)


def _validate_payload(payload: Any) -> Union[UserMessage, ErrorEnvelope]:
    """类型级校验:payload 必须为 ``UserMessage | ErrorEnvelope``,拒绝裸 str。"""
    if isinstance(payload, str):
        raise TypeError(_MSG_REJECTS_STR)
    if isinstance(payload, (UserMessage, ErrorEnvelope)):
        return payload
    raise TypeError(_MSG_REJECTS_TYPE.format(type_name=type(payload).__name__))


# ════════════════════════════════════════════════════════════════
# Typed adapter API
# ════════════════════════════════════════════════════════════════
def json_response(
    payload: Union[UserMessage, ErrorEnvelope],
    status_code: int = 200,
    i18n_manager: Optional[Any] = None,
    **extra: Any,
) -> JSONResponse:
    """Typed adapter: ``fastapi.responses.JSONResponse``。

    只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str。内部经
    ``render_for_send`` 渲染为本地化字符串后构造 JSONResponse。

    响应体结构:
        {
            "message": "<本地化消息>",
            "error_code": "<错误码,仅 ErrorEnvelope>",
            "trace_id": "<追踪 ID,仅当 payload 含 trace_id>",
            ...safe params(已过滤敏感字段),
            ...extra(调用方追加的 safe 字段,如 ``{"status": "ok"}``)
        }

    内部 exception ``str(e)`` 不进入响应体(由调用方记录到结构化日志,
    通过 ``trace_id`` 让用户引用)。

    Args:
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例(禁止裸 str)
        status_code: HTTP 状态码(默认 200)
        i18n_manager: 可选的 I18nManager(未传入时使用 get_i18n_manager() 单例)
        **extra: 追加到响应体的 safe 字段(调用方负责确保不包含敏感信息)

    Returns:
        ``fastapi.responses.JSONResponse`` 实例

    Raises:
        TypeError: payload 为裸 str 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    validated = _validate_payload(payload)
    manager = _resolve_i18n_manager(i18n_manager)
    text = render_for_send(validated, manager)

    # 构造响应体:只暴露 safe 字段(内部 exception 不进入)
    body: dict[str, Any] = {"message": text}

    # 提取 UserMessage 字段(error_code / trace_id / safe params)
    if isinstance(validated, ErrorEnvelope):
        user_msg = validated.to_user_message()
    else:
        user_msg = validated

    if user_msg.error_code:
        body["error_code"] = user_msg.error_code
    if user_msg.trace_id:
        body["trace_id"] = user_msg.trace_id
    # safe params(params 已在 UserMessage 构造时经过 _sanitize_params 过滤)
    # 以扁平形式附加到响应体(便于前端按 key 引用)
    for k, v in user_msg.params.items():
        # 避免覆盖顶层字段(message/error_code/trace_id)
        if k not in body:
            body[k] = v

    # 追加调用方提供的 extra 字段
    for k, v in extra.items():
        if k not in body:
            body[k] = v

    return JSONResponse(status_code=status_code, content=body)
