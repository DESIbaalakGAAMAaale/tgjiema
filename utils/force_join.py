from loguru import logger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import NetworkError, TimedOut, BadRequest, Forbidden

from config import settings


async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channel_id = settings.FORCE_JOIN_CHANNEL_ID
    if not channel_id:
        return True

    user = update.effective_user
    if not user:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat_id=channel_id, user_id=user.id
        )
        if member.status not in ("left", "kicked"):
            return True
    except (NetworkError, TimedOut) as e:
        # 网络/超时错误:不应误判用户未加群,放行避免误伤
        logger.warning(f"强制加群检查网络异常,放行: {e}")
        return True
    except (BadRequest, Forbidden) as e:
        # 配置错误(bot 不在频道/频道不存在):告警并放行,避免阻塞所有用户
        logger.error(f"强制加群配置异常,请检查 FORCE_JOIN_CHANNEL_ID 和 bot 权限: {e}")
        return True
    except Exception as e:
        # 未预期异常:安全默认放行,避免误伤用户
        logger.warning(f"强制加群检查未预期异常,放行: {e}")
        return True

    channel_link = settings.FORCE_JOIN_CHANNEL_LINK
    if channel_link:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 加入频道", url=channel_link)],
        ])
        text = "⚠️ 使用前请先加入频道,加入后重新发送指令即可。"
    else:
        keyboard = None
        text = "⚠️ 使用前请先加入指定频道,加入后重新发送指令即可。"
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    elif update.callback_query:
        await update.callback_query.answer(
            "请先加入频道后再操作。", show_alert=True
        )
    return False


def three_bot_reminder() -> str:
    up = settings.UPLOAD_BOT_USERNAME
    de = settings.DECODER_BOT_USERNAME
    se = settings.SENDER_BOT_USERNAME
    channel_link = settings.FORCE_JOIN_CHANNEL_LINK
    lines = ["\n⚠️ 使用前请先启动以下三个机器人:"]
    if up:
        lines.append(f"  1️⃣ 上传机器人:@{up}")
    if de:
        lines.append(f"  2️⃣ 解码机器人:@{de}")
    if se:
        lines.append(f"  3️⃣ 发送机器人:@{se}")
    lines.append("\n请确保已向这三个机器人均发送过 /start 命令,否则系统无法正常工作。")
    if channel_link:
        lines.append(f"\n📢 官方频道: {channel_link}")
    return "\n".join(lines)