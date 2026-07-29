"""R65 P1-01: Telegram 运行时类重导出(业务模块导入入口)。

整改背景(R65 终审报告 P1-01):
    业务模块(bots/、services/)禁止 ``from telegram import ...`` /
    ``from telegram.ext import ...`` / ``from telegram.error import ...``
    (sink import-boundary AST 门禁 Rule 1)。

    但部分场景需要运行时使用 telegram 类(非 sink API 调用):
      - ``Application`` / ``CommandHandler`` / ``MessageHandler`` /
        ``CallbackQueryHandler`` / ``filters`` — 用于注册 bot handler
        (在 ``bots/admin_bot/run.py`` 等 entry-point 中)
      - ``Bot`` — 用于初始化 bot 实例(已被 ``build_bot`` 工厂替代,
        保留重导出供极少数无法用工厂的场景)
      - ``InlineKeyboardButton`` — 用于构造按钮对象(已被
        ``build_inline_keyboard`` 工厂替代,保留重导出供过渡期使用)
      - ``InlineKeyboardMarkup`` — 同上
      - ``InputMedia*`` — 媒体组构造(已被 ``build_input_media`` 替代)
      - ``TelegramError`` / ``RetryAfter`` 等 — 异常捕获(无 sink 调用)
      - ``Update`` / ``ContextTypes`` — **仅用于类型注解**(配合
        ``from __future__ import annotations`` 使用字符串注解时不需要
        导入;若使用非字符串注解则可从这里导入)

    本模块位于 ``services/sink_adapters/`` 白名单中,允许直接 ``from
    telegram import ...``。业务模块改为 ``from
    services.sink_adapters.telegram_helpers import ...`` 即可通过门禁。

使用原则:
    - 优先使用 ``services.sink_adapters.telegram_adapter`` 中的 typed
      adapter(``safe_reply_text`` / ``safe_send_message`` /
      ``safe_edit_message_text``)替代 sink API 调用
    - 优先使用 ``build_inline_keyboard`` / ``build_bot`` /
      ``build_input_media`` 工厂替代直接构造 telegram 对象
    - 仅在上述方式都不适用时(如注册 handler、捕获异常),才通过本模块
      导入 telegram 类

新代码示例:
    # ❌ 违规(被门禁阻断):
    from telegram import Bot, Application
    from telegram.ext import CommandHandler
    from telegram.error import RetryAfter

    # ✅ 正确(通过 sink_adapters 重导出):
    from services.sink_adapters.telegram_helpers import (
        Application, CommandHandler, RetryAfter,
    )
    from services.sink_adapters.telegram_adapter import build_bot
    bot = build_bot(token=...)

R76 O2 边界声明:
    本模块仅重导出 Telegram 类(Application/Bot/Update 等),用于 handler 注册、
    类型注解和异常捕获。**Provider client 选择**统一由
    ``services.sink_adapters.telegram_adapter.build_provider_client(settings, token)``
    工厂完成,业务模块不得:
        - 自行 ``Application.builder().token(...).build()`` 选择测试实现;
        - 把 contract adapter(``ContractProviderClient``)伪装为 ``telegram.Bot``
          子类注入 Application;
        - 通过 monkey patch ``bot.get_file``/``bot.download_file`` 冒充真实 Provider。

    contract 模式下不构建 Application,updates 由 ``web_adapter`` 的
    ``/internal/contract/update`` 端点接收并 dispatch(O5 实现)。
"""
from __future__ import annotations

# sink_adapters/ 在 ALLOWED_PREFIXES 白名单中,允许直接导入 telegram 包
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

__all__ = [
    # telegram
    "Bot",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "InputMediaAnimation",
    "InputMediaAudio",
    "InputMediaDocument",
    "InputMediaPhoto",
    "InputMediaVideo",
    "Update",
    # telegram.error
    "BadRequest",
    "Forbidden",
    "NetworkError",
    "RetryAfter",
    "TelegramError",
    "TimedOut",
    # telegram.ext
    "Application",
    "CallbackQueryHandler",
    "CommandHandler",
    "ContextTypes",
    "MessageHandler",
    "filters",
]
