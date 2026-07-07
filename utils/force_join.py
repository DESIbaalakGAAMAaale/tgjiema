"""强制加群校验 — 检查用户是否已加入指定频道。

安全模型（P1-17 整改）:
  默认 fail-closed（拒绝放行），仅在明确判定用户已加入时才放行。
  异常分两类处理:
    - Forbidden / BadRequest:视为「明确非成员 / 配置错误」→ 拒绝放行(return False)。
    - NetworkError / TimedOut 等瞬态错误:默认也拒绝放行(return False)并告警;
      仅在运维显式开启 FORCE_JOIN_FAIL_OPEN 环境变量(Telegram 大面积故障时)才临时放行。
  通用未预期异常同样 fail-closed，避免误放行绕过加群限制。

注意:settings 在函数内读取，不在模块顶层 import 期读取（参考 utils/rate_limiter 的反面教材，
避免在 import 阶段触发配置/网络副作用）。
"""

import os

from loguru import logger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import NetworkError, TimedOut, BadRequest, Forbidden


def _fail_open_enabled() -> bool:
    """运维逃生开关:Telegram 大面积故障时临时放行。默认关闭(fail-closed)。"""
    val = os.getenv("FORCE_JOIN_FAIL_OPEN", "false")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # 延迟读取 settings，避免在模块 import 期访问配置（P1-17）
    from config import settings

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
    except (BadRequest, Forbidden) as e:
        # 明确非成员 / 频道配置错误(bot 不在频道、频道不存在等):
        # 视为「未满足加群条件」，fail-closed 拒绝放行。
        logger.error(
            f"强制加群:明确拒绝(非成员/配置异常),channel={channel_id},user={user.id}: {e}"
        )
        return False
    except (NetworkError, TimedOut) as e:
        # 瞬态网络/超时错误:默认 fail-closed 拒绝放行,避免误放行绕过加群限制。
        if _fail_open_enabled():
            logger.warning(f"强制加群:网络异常,运维放行开关已开启,临时放行: {e}")
            return True
        logger.warning(f"强制加群:网络异常,默认拒绝放行(安全默认): {e}")
        return False
    except Exception as e:
        # 未预期异常:默认 fail-closed 拒绝放行。
        if _fail_open_enabled():
            logger.warning(f"强制加群:未预期异常,运维放行开关已开启,临时放行: {e}")
            return True
        logger.error(f"强制加群:未预期异常,默认拒绝放行(安全默认): {e}")
        return False

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
    from config import settings
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
