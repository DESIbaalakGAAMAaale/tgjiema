"""R45 17.2 / R46 P1: 按钮 callback 签名验证,防止伪造 user_id/role/action。

R46 P1 增强:
    - 添加 nonce 防重放(短随机串)
    - callback_data 格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
    - 服务端重新加载权限与资源状态,不信任 callback 内 user_id/role

设计要点:
    - 签名密钥使用 BOT_TOKEN(每个 bot 唯一,不对外暴露)
    - HMAC-SHA256 截断 16 字符(64 bit),平衡安全与 callback_data 长度限制
    - TTL 默认 1 小时,过期后按钮失效需重新生成
    - 使用 hmac.compare_digest 进行常量时间比较,防止时序攻击
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Tuple

from loguru import logger

from config import settings


def generate_signed_callback(
    user_id: int,
    action: str,
    data: str = "",
    ttl: int = 3600,
    nonce: str = "",
) -> str:
    """生成签名 callback_data。

    R46 P1: 添加 nonce 防重放。

    格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}

    Args:
        user_id: 用户 ID(必须为整数)
        action: 动作标识(如 "confirm", "cancel", "retry", "appeal")
        data: 附加数据(如 file_code,可为空字符串)
        ttl: 有效期(秒,默认 1 小时)
        nonce: 随机串(默认自动生成 8 字符)

    Returns:
        签名后的 callback_data 字符串
    """
    expire_ts = int(time.time()) + ttl
    if not nonce:
        nonce = secrets.token_hex(4)  # 8 字符 nonce
    payload = f"{user_id}:{action}:{data}:{expire_ts}:{nonce}"
    signature = _sign(payload)
    return f"{payload}:{signature}"


def verify_signed_callback(
    callback_data: str,
    current_user_id: int,
) -> Tuple[bool, str, str]:
    """验证签名 callback_data。

    R46 P1: 支持 nonce 字段(向后兼容无 nonce 的旧格式)。

    验证流程:
        1. 解析 callback_data 各字段
        2. 验证 user_id 与 current_user_id 匹配(防止跨用户伪造)
        3. 验证未过期(expire_ts > now)
        4. 验证签名匹配(常量时间比较)

    Args:
        callback_data: 待验证的 callback_data
        current_user_id: 当前用户 ID(必须匹配签名中的 user_id)

    Returns:
        (valid, action, data): valid=True 时 action/data 可用;
        valid=False 时 action/data 为空字符串
    """
    try:
        parts = callback_data.split(":")
        # R46 P1: 支持 5 段(旧格式)和 6 段(新格式带 nonce)
        if len(parts) < 5:
            logger.debug(f"[button_security] callback_data 字段不足: {callback_data}")
            return False, "", ""
        user_id = int(parts[0])
        action = parts[1]
        signature = parts[-1]
        # 判断是否为新的 6 段格式(含 nonce)
        if len(parts) >= 6:
            # 新格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
            data = ":".join(parts[2:-3])
            expire_ts = int(parts[-3])
            nonce = parts[-2]
            payload = f"{user_id}:{action}:{data}:{expire_ts}:{nonce}"
        else:
            # 旧格式(向后兼容): {user_id}:{action}:{data}:{expire_ts}:{signature}
            data = ":".join(parts[2:-2])
            expire_ts = int(parts[-2])
            payload = f"{user_id}:{action}:{data}:{expire_ts}"
        # 验证用户 ID(防止跨用户伪造)
        if user_id != current_user_id:
            logger.debug(
                f"[button_security] user_id 不匹配: expected={current_user_id} "
                f"got={user_id}"
            )
            return False, "", ""
        # 验证过期时间
        if time.time() > expire_ts:
            logger.debug(f"[button_security] callback_data 已过期: expire_ts={expire_ts}")
            return False, "", ""
        # 验证签名(常量时间比较,防止时序攻击)
        expected_sig = _sign(payload)
        if not hmac.compare_digest(signature, expected_sig):
            logger.debug("[button_security] 签名不匹配")
            return False, "", ""
        return True, action, data
    except (ValueError, IndexError) as e:
        logger.debug(f"[button_security] callback_data 解析失败: {e}")
        return False, "", ""


def _sign(payload: str) -> str:
    """使用 BOT_TOKEN 作为密钥签名 payload。

    优先使用 ADMIN_BOT_TOKEN(管理后台按钮),其次 SENDER_BOT_TOKEN(发送 bot 按钮),
    最后回退到固定字符串(仅用于测试环境,生产环境应配置 BOT_TOKEN)。

    Args:
        payload: 待签名的字符串

    Returns:
        HMAC-SHA256 签名的前 16 个十六进制字符(64 bit)
    """
    secret = settings.ADMIN_BOT_TOKEN or settings.SENDER_BOT_TOKEN or "default_secret"
    return hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
