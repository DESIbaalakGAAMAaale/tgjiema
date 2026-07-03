"""管理员通知工具

举报等需要管理员处理的消息必须由 Admin Bot 发出，这样消息上的操作按钮
（report:ban / report:detach / report:block）的回调才会回到 Admin Bot 被处理。
若改用发起方（Idx/Dsp Bot）的 context.bot 发送，回调会回到发起方 Bot，
而它们没有注册 report: 回调处理器，导致按钮点击无任何效果。
"""

from loguru import logger
from telegram import Bot, InlineKeyboardMarkup

from config import settings


async def send_to_admin(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """通过 Admin Bot Token 向管理员发送消息（含可交互按钮）。

    返回是否成功发出。未配置 ADMIN_BOT_TOKEN / ADMIN_TELEGRAM_ID 时返回 False。
    """
    token = settings.ADMIN_BOT_TOKEN
    admin_chat_id = settings.ADMIN_TELEGRAM_ID
    if not token or not admin_chat_id:
        return False
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=admin_chat_id, text=text, reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.error(f"[admin_notify] 通过 Admin Bot 发送管理员消息失败: {e}")
        return False
