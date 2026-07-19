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


# ════════════════════════════════════════════════════════════════
# R65 P1-01: Telegram 类工厂/构造辅助
# ════════════════════════════════════════════════════════════════
# 业务模块(bots/、services/)禁止 ``from telegram import ...``(sink
# import-boundary 门禁 Rule 1)。但部分场景需要运行时构造 telegram 对象:
#   - InlineKeyboardMarkup / InlineKeyboardButton(用户面 reply_markup)
#   - Bot(token=...)(用于初始化 bot 实例,如 mon_bot 通知)
#   - InputMedia*(媒体组发送)
# 本节提供工厂函数,业务模块通过这些工厂间接构造 telegram 对象,
# 避免在业务模块中导入 telegram 包。


def build_inline_keyboard(buttons: Any) -> Any:
    """R65 P1-01: 构造 ``telegram.InlineKeyboardMarkup``(业务模块工厂)。

    业务模块(bots/、services/)禁止 ``from telegram import
    InlineKeyboardMarkup``。本工厂在 adapter 层(sink_adapters/ 白名单)
    内部导入 telegram 包,业务模块通过 ``build_inline_keyboard(...)``
    间接构造。

    Args:
        buttons: 二维列表(与 ``InlineKeyboardMarkup`` 构造器参数一致):
            - ``list[list[InlineKeyboardButton]]`` — 已构造的按钮对象
            - ``list[list[tuple]]`` / ``list[list[dict]]`` — 透传给
              ``InlineKeyboardMarkup``,由其内部解析
            也接受单层 list(自动包装为 [[btn], ...],便于传 BACK_BTN 等)。

    Returns:
        ``telegram.InlineKeyboardMarkup`` 实例
    """
    from telegram import InlineKeyboardMarkup
    # 兼容单层 list(如 BACK_BTN = [[InlineKeyboardButton(...)]] 已是二维,
    # 但若调用方传入 [InlineKeyboardButton(...)] 单层,自动包装)
    if buttons is None:
        return None
    if not isinstance(buttons, list) or not buttons:
        return InlineKeyboardMarkup(buttons)
    # 判断第一层元素是否是 list(二维)— 若是则直接传,否则包装
    first = buttons[0]
    if isinstance(first, list):
        return InlineKeyboardMarkup(buttons)
    # 单层 list → 包装为二维
    return InlineKeyboardMarkup([buttons])


def build_bot(token: str, **kwargs: Any) -> Any:
    """R65 P1-01: 构造 ``telegram.Bot`` 实例(业务模块工厂)。

    业务模块(bots/、services/)禁止 ``from telegram import Bot``。本工厂
    在 adapter 层(sink_adapters/ 白名单)内部导入 telegram 包,业务模块
    通过 ``build_bot(token)`` 间接构造 Bot 实例。

    Args:
        token: Bot token(由 ``@BotFather`` 颁发)
        **kwargs: 透传给 ``Bot`` 构造器的额外参数

    Returns:
        ``telegram.Bot`` 实例(未初始化,调用方需 ``await bot.initialize()``)
    """
    from telegram import Bot
    return Bot(token=token, **kwargs)


def build_input_media(media_type: str, **kwargs: Any) -> Any:
    """R65 P1-01: 构造 ``telegram.InputMedia*`` 对象(业务模块工厂)。

    业务模块(bots/、services/)禁止 ``from telegram import InputMediaPhoto``
    等。本工厂在 adapter 层内部根据 ``media_type`` 选择对应的 InputMedia
    类构造对象。

    Args:
        media_type: 媒体类型,可选值:
            - ``"photo"`` → ``InputMediaPhoto``
            - ``"video"`` → ``InputMediaVideo``
            - ``"document"`` → ``InputMediaDocument``
            - ``"audio"`` → ``InputMediaAudio``
            - ``"animation"`` → ``InputMediaAnimation``
        **kwargs: 透传给对应 ``InputMedia*`` 构造器的参数(如 media、caption)

    Returns:
        对应的 ``telegram.InputMedia*`` 实例

    Raises:
        ValueError: ``media_type`` 不在支持的类型中
    """
    from telegram import (
        InputMediaAnimation,
        InputMediaAudio,
        InputMediaDocument,
        InputMediaPhoto,
        InputMediaVideo,
    )
    mapping = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "document": InputMediaDocument,
        "audio": InputMediaAudio,
        "animation": InputMediaAnimation,
    }
    cls = mapping.get(media_type)
    if cls is None:
        raise ValueError(
            f"不支持的 media_type={media_type!r},可选: "
            f"{sorted(mapping.keys())}"
        )
    return cls(**kwargs)


async def safe_answer_callback_query(
    query: Any,
    text: Optional[str] = None,
    show_alert: bool = False,
    **kwargs: Any,
) -> Any:
    """R65 P1-01: Typed adapter: ``query.answer(...)``。

    ``answer_callback_query`` 用于应答 callback query(关闭按钮 loading
    动画、弹出提示)。本 adapter 接受可选的 ``text``(str 或 None)和
    ``show_alert`` 标志,内部调用 ``query.answer(...)``。

    与 ``safe_reply_text`` 不同,本 adapter 接受裸 str(因为 callback
    answer 是轻量提示,通常不需要结构化 UserMessage;若需要本地化,
    调用方应在传入前通过 ``_t()`` / ``_i18n_t()`` 渲染)。

    Args:
        query: telegram.CallbackQuery(需含 .answer 方法)
        text: 可选的提示文本(str,可为 None)
        show_alert: 是否以 alert 弹窗形式显示(默认 False)
        **kwargs: 透传给 ``query.answer`` 的额外参数

    Returns:
        原生 API 的返回值
    """
    if text is not None:
        kwargs.setdefault("text", text)
    kwargs.setdefault("show_alert", show_alert)
    return await query.answer(**kwargs)
