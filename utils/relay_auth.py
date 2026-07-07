"""统一中继鉴权 helper。

提供单一入口判断发送者是否为受信中继账号，消除 up_bot / idx_bot 中
重复的白名单检查逻辑。fail-closed 语义：空白名单 → 返回 False。

使用 ``import database`` + 属性访问 ``database.get_relay_whitelist``，
而非 ``from database import get_relay_whitelist``，确保现有测试中
``monkeypatch.setattr(database, "get_relay_whitelist", ...)`` 仍能生效。
"""

import database
from loguru import logger


async def is_relay_sender_allowed(sender_id: int) -> bool:
    """判断发送者是否为受信中继账号。

    fail-closed 语义：
    - 白名单为空 → 返回 False（拒绝所有）
    - 白名单非空且 sender_id 在其中 → 返回 True
    - 白名单非空但 sender_id 不在其中 → 返回 False

    Args:
        sender_id: Telegram 用户 ID

    Returns:
        True 表示允许，False 表示拒绝
    """
    wl = await database.get_relay_whitelist()
    if not wl:
        logger.error(
            "[relay_auth] RELAY_ACCOUNT_IDS 未配置,拒绝中继请求——"
            "请通过 /relay_whitelist add 配置白名单"
        )
        return False
    if sender_id not in wl:
        logger.warning(f"[relay_auth] 拒绝非中继账号的请求: user={sender_id}")
        return False
    return True
