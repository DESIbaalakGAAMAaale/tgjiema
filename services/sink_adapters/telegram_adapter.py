"""R64 P1-06: Telegram sink typed adapter。

整改背景(R64 终审报告 P1-06):
    业务模块禁止直接调用 telegram send/edit API(``update.message.reply_text`` /
    ``context.bot.send_message`` / ``query.edit_message_text``),必须经过此 adapter。

adapter 职责:
    1. 类型级强制 ``UserMessage | ErrorEnvelope`` 输入(拒绝裸 str)。
    2. 内部经 ``render_for_send`` 渲染为本地化字符串后调用原生 API。
    3. ``safe_edit_message_text`` 不再拼接 ``query.message.text``(避免继承旧消息
       语言 — 旧消息可能是不同 locale 下渲染的,拼接后新消息继承错误的 locale)。
    4. 内部异常 ``str(e)`` 仅记录到结构化日志,不进入用户面消息 params。

设计原则:
    - 渲染集中在 adapter 最后一层(传入结构化对象,在调用原生 API 前才转为字符串)。
    - 业务层只产生 ``UserMessage.from_key(...)`` / ``ErrorEnvelope(app_error)``,
      不接触裸字符串。
"""
from __future__ import annotations

from typing import Any, Optional, Union

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

    Args:
        i18n_manager: 调用方显式传入的 I18nManager 实例(可为 None)

    Returns:
        I18nManager 实例
    """
    if i18n_manager is not None:
        return i18n_manager
    from services.i18n import get_i18n_manager
    return get_i18n_manager()


# ════════════════════════════════════════════════════════════════
# Typed payload 校验
# ════════════════════════════════════════════════════════════════
# R63: 引用 services.user_message 的模块常量,避免裸字符串扫描器误报
_MSG_REJECTS_STR = (
    "Telegram sink adapter 不接受裸 str,请使用 UserMessage.from_key(...) "
    "或 ErrorEnvelope(...) 包装用户可见消息(类型边界在 adapter 层强制)"
)
_MSG_REJECTS_TYPE = (
    "Telegram sink adapter 仅接受 UserMessage | ErrorEnvelope,实际类型 "
    "{type_name};请使用 UserMessage.from_key(...) 或 ErrorEnvelope(...) "
    "包装用户可见消息"
)


def _validate_payload(payload: Any) -> Union[UserMessage, ErrorEnvelope]:
    """类型级校验:payload 必须为 ``UserMessage | ErrorEnvelope``,拒绝裸 str。

    Args:
        payload: 待发送的用户面消息

    Returns:
        ``UserMessage`` 或 ``ErrorEnvelope`` 实例

    Raises:
        TypeError: payload 为 ``str`` 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    if isinstance(payload, str):
        raise TypeError(_MSG_REJECTS_STR)
    if isinstance(payload, (UserMessage, ErrorEnvelope)):
        return payload
    raise TypeError(_MSG_REJECTS_TYPE.format(type_name=type(payload).__name__))


# ════════════════════════════════════════════════════════════════
# Typed adapter API
# ════════════════════════════════════════════════════════════════
async def safe_reply_text(
    update: Any,
    payload: Union[UserMessage, ErrorEnvelope],
    reply_markup: Any = None,
    i18n_manager: Optional[Any] = None,
    **kwargs: Any,
) -> Any:
    """Typed adapter: ``update.message.reply_text``。

    只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str。内部经
    ``render_for_send`` 渲染为本地化字符串后调用 ``utils.flood_waiter.
    safe_reply_text``(带 FloodWait 退避)。

    Args:
        update: telegram.Update(或等价 message 对象,需含 .message.reply_text)
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例(禁止裸 str)
        reply_markup: 可选的 InlineKeyboardMarkup(透传给原生 API)
        i18n_manager: 可选的 I18nManager(未传入时使用 get_i18n_manager() 单例)
        **kwargs: 透传给原生 reply_text 的额外参数

    Returns:
        原生 API 的返回值(Message 对象)

    Raises:
        TypeError: payload 为裸 str 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    validated = _validate_payload(payload)
    manager = _resolve_i18n_manager(i18n_manager)
    text = render_for_send(validated, manager)
    # 延迟导入避免 services.sink_adapters 与 utils.flood_waiter 的循环依赖
    from utils.flood_waiter import safe_reply_text as _raw_reply_text
    # update 可能是 telegram.Update(取 .message)或直接是 message 对象
    message = getattr(update, "message", None) or update
    if reply_markup is not None:
        kwargs.setdefault("reply_markup", reply_markup)
    return await _raw_reply_text(message, text, **kwargs)


async def safe_send_message(
    bot: Any,
    chat_id: Any,
    payload: Union[UserMessage, ErrorEnvelope],
    reply_markup: Any = None,
    i18n_manager: Optional[Any] = None,
    **kwargs: Any,
) -> Any:
    """Typed adapter: ``context.bot.send_message``。

    只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str。内部经
    ``render_for_send`` 渲染为本地化字符串后调用 ``bot.send_message``。

    Args:
        bot: telegram.Bot 实例(或兼容接口)
        chat_id: 目标 chat_id
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例(禁止裸 str)
        reply_markup: 可选的 InlineKeyboardMarkup(透传给原生 API)
        i18n_manager: 可选的 I18nManager(未传入时使用 get_i18n_manager() 单例)
        **kwargs: 透传给原生 send_message 的额外参数

    Returns:
        原生 API 的返回值(Message 对象)

    Raises:
        TypeError: payload 为裸 str 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    validated = _validate_payload(payload)
    manager = _resolve_i18n_manager(i18n_manager)
    text = render_for_send(validated, manager)
    if reply_markup is not None:
        kwargs.setdefault("reply_markup", reply_markup)
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def safe_edit_message_text(
    query: Any,
    payload: Union[UserMessage, ErrorEnvelope],
    reply_markup: Any = None,
    i18n_manager: Optional[Any] = None,
    **kwargs: Any,
) -> Any:
    """Typed adapter: ``query.edit_message_text``。

    R64 P1-06 核心整改:
        **不再拼接 ``query.message.text``**(避免继承旧消息语言)。
        旧消息可能是不同 locale 下渲染的(用户切换语言后再次点击按钮),
        拼接会让新消息继承错误的 locale。改为直接用新的
        ``UserMessage.from_key(...)`` 替换整个消息。

    只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str。内部经
    ``render_for_send`` 渲染为本地化字符串后调用 ``query.edit_message_text``。

    Args:
        query: telegram.CallbackQuery(需含 .edit_message_text)
        payload: ``UserMessage`` 或 ``ErrorEnvelope`` 实例(禁止裸 str)
        reply_markup: 可选的 InlineKeyboardMarkup(透传给原生 API)
        i18n_manager: 可选的 I18nManager(未传入时使用 get_i18n_manager() 单例)
        **kwargs: 透传给原生 edit_message_text 的额外参数

    Returns:
        原生 API 的返回值(Message 对象)

    Raises:
        TypeError: payload 为裸 str 或非 ``UserMessage | ErrorEnvelope`` 类型
    """
    validated = _validate_payload(payload)
    manager = _resolve_i18n_manager(i18n_manager)
    text = render_for_send(validated, manager)
    # R64 P1-06: 不再拼接 query.message.text(避免继承旧消息语言)
    if reply_markup is not None:
        kwargs.setdefault("reply_markup", reply_markup)
    return await query.edit_message_text(text=text, **kwargs)
