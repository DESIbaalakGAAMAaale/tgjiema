"""R45 17.2 / R46 P1 / R47 P1-a: 按钮 callback 签名验证,防止伪造 user_id/role/action。

R46 P1 增强:
    - 添加 nonce 防重放(短随机串)
    - callback_data 格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
    - 服务端重新加载权限与资源状态,不信任 callback 内 user_id/role

R47 P1-a 终审整改:
    - callback nonce 原子消费(callback_nonces 表,UPDATE WHERE consumed_at IS NULL)
      防止回调被并发重放,同一 nonce 只能消费一次
    - production 缺 BOT_TOKEN 启动失败(fail-closed),禁止回退 default_secret
    - 签名至少 128 bit(32 hex chars),原 64 bit 强度不足
    - 高风险 action(ban/takedown/purge/restore/admin_grant 等)必须使用 6 段格式(含 nonce),
      旧 5 段格式仅允许低风险 action(查看/取消/语言选择等)向后兼容
    - 新增 sign_button_token_with_nonce:持久化 nonce 到 callback_nonces 表
    - 新增 verify_button_token:async 验证 + 原子消费 nonce

设计要点:
    - 签名密钥使用 BOT_TOKEN(每个 bot 唯一,不对外暴露)
    - HMAC-SHA256 截断 32 字符(128 bit),平衡安全与 callback_data 长度限制
    - TTL 默认 1 小时,过期后按钮失效需重新生成
    - 使用 hmac.compare_digest 进行常量时间比较,防止时序攻击
    - 随机 nonce 使用 secrets.token_urlsafe(16)(≥128 bit 熵)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import secrets
import time
from typing import Optional, Tuple

from loguru import logger

from config import settings

# R50 P1-1: 统一错误码协议化(替代裸字符串 RuntimeError)
from services.error_codes import AppError, ErrorCodes


# ── R47 P1-a: 签名长度(32 hex chars = 128 bit)──────────────────
SIGNATURE_LENGTH: int = 32
# ── R47 P1-a: nonce 熵(16 字节 = 128 bit)────────────────────────
NONCE_BYTES: int = 16


# ── R47 P1-a: 高风险 action 集合 ─────────────────────────────────
# 这些 action 必须使用 6 段格式(含 nonce + 服务端原子消费),
# 禁止旧 5 段格式(无 nonce,无法防重放)
HIGH_RISK_ACTIONS: frozenset[str] = frozenset({
    # 账号封禁/解封
    "ban", "unban",
    # 内容下架/恢复
    "takedown", "release_takedown",
    # 文件清除/恢复
    "purge", "purge_file", "restore", "restore_file",
    # 删除文件
    "delete_file", "delete",
    # 管理员授权/撤销
    "admin_grant", "admin_revoke",
    # 密钥轮转
    "rotate_keys",
    # 配额重置
    "reset_quota",
    # 紧急访问
    "break_glass",
    # 强制登出
    "force_logout",
    # 申诉审批(影响其他用户权益)
    "approve_appeal", "reject_appeal",
    # 配置变更
    "update_config", "reload_config",
})


def _check_production_secret() -> None:
    """R47 P1-a: production 环境必须配置 BOT_TOKEN,禁止 default_secret。

    production: ADMIN_BOT_TOKEN 或 SENDER_BOT_TOKEN 至少一个必须配置,否则启动失败。
    development/test: 允许回退到 default_secret(便于本地开发零依赖)。

    Raises:
        RuntimeError: production 环境且 BOT_TOKEN 缺失时
    """
    # 用 str() 兜底 MagicMock(测试环境 settings 是 MagicMock)
    env = str(getattr(settings, "ENVIRONMENT", "") or "")
    if env != "production":
        return
    admin_token = str(getattr(settings, "ADMIN_BOT_TOKEN", "") or "")
    sender_token = str(getattr(settings, "SENDER_BOT_TOKEN", "") or "")
    if not admin_token and not sender_token:
        # R50 P1-1: 协议化为 PRODUCTION_BOT_TOKEN_MISSING
        raise AppError(
            ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING,
            params={"environment": env},
        )


def validate_production_config() -> None:
    """R48 P1-b: 供每个 Bot 启动时显式调用的 production 配置校验。

    R48 终审整改:production secret 检查必须在每个 Bot 启动时实际触发,
    而不是仅依赖模块被导入时偶然执行。各 Bot 的 _async_main / run 函数
    应在启动时调用本函数,确保 fail-closed。

    与 _check_production_secret 的区别:
        - _check_production_secret 在模块导入时自动调用(可能因 conftest MagicMock
          settings 而不触发)
        - validate_production_config 供 Bot 启动时显式调用,确保每次启动都检查
        - 内部调用 _check_production_secret,行为一致

    Raises:
        RuntimeError: production 环境且 BOT_TOKEN 缺失时
    """
    _check_production_secret()


# 模块初始化时检查 production 配置(fail-closed)
_check_production_secret()


def _get_signing_secret() -> str:
    """获取签名密钥。

    优先 ADMIN_BOT_TOKEN(管理后台按钮),其次 SENDER_BOT_TOKEN(发送 bot 按钮),
    最后回退到固定字符串(仅 development/test 允许;production 已在
    _check_production_secret 拦截)。

    Returns:
        签名密钥字符串
    """
    secret = str(getattr(settings, "ADMIN_BOT_TOKEN", "") or "")
    if not secret:
        secret = str(getattr(settings, "SENDER_BOT_TOKEN", "") or "")
    if not secret:
        # 仅 development/test 允许(production 已在模块初始化时拦截)
        secret = "default_secret"
    return secret


def _sign(payload: str) -> str:
    """使用 BOT_TOKEN 作为密钥签名 payload。

    R47 P1-a: 签名长度从 16 hex chars(64 bit)提升到 32 hex chars(128 bit)。

    Args:
        payload: 待签名的字符串

    Returns:
        HMAC-SHA256 签名的前 32 个十六进制字符(128 bit)
    """
    secret = _get_signing_secret()
    return hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:SIGNATURE_LENGTH]


def _is_high_risk_action(action: str) -> bool:
    """R47 P1-a: 判断 action 是否为高风险(必须使用 6 段格式 + nonce 原子消费)。"""
    return action in HIGH_RISK_ACTIONS


def generate_signed_callback(
    user_id: int,
    action: str,
    data: str = "",
    ttl: int = 3600,
    nonce: str = "",
    resource_version: str = "",
) -> str:
    """生成签名 callback_data(向后兼容旧接口,不持久化 nonce)。

    R46 P1: 添加 nonce 防重放。
    R47 P1-a: nonce 熵提升到 128 bit(32 hex chars)。
    R48 P1-b: 添加 resource_version 参数,绑定资源版本(防止旧按钮操作已更新资源)。

    注意:本函数不持久化 nonce 到 callback_nonces 表,
    高风险 action 应改用 sign_button_token_with_nonce(异步,持久化 nonce)。

    格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
    (resource_version 不出现在 callback_data 中,仅参与签名计算,
     验证时调用方需传入相同的 resource_version 才能通过签名校验)

    Args:
        user_id: 用户 ID(必须为整数)
        action: 动作标识(如 "confirm", "cancel", "retry", "appeal")
        data: 附加数据(如 file_code,可为空字符串)
        ttl: 有效期(秒,默认 1 小时)
        nonce: 随机串(默认自动生成 32 hex chars = 128 bit 熵)
        resource_version: 资源版本标识(如 file_code + version),绑定到签名中。
            传入后验证方必须提供相同的 resource_version 才能通过签名校验,
            防止使用旧按钮操作已更新的资源。为空时不绑定(向后兼容)。

    Returns:
        签名后的 callback_data 字符串(6 段格式)
    """
    expire_ts = int(time.time()) + ttl
    if not nonce:
        # R47 P1-a: 128 bit 熵(原 32 bit 不足)
        nonce = secrets.token_hex(NONCE_BYTES)  # 32 hex chars
    # R48 P1-b: resource_version 参与签名(非空时绑定资源版本)
    if resource_version:
        sig_payload = f"{user_id}:{action}:{data}:{resource_version}:{expire_ts}:{nonce}"
    else:
        sig_payload = f"{user_id}:{action}:{data}:{expire_ts}:{nonce}"
    signature = _sign(sig_payload)
    # callback_data 格式不变(6 段),resource_version 仅在签名 payload 中
    return f"{user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}"


def verify_signed_callback(
    callback_data: str,
    current_user_id: int,
    resource_version: str = "",
) -> Tuple[bool, str, str]:
    """验证签名 callback_data(同步,向后兼容,不消费 nonce)。

    R46 P1: 支持 nonce 字段(向后兼容无 nonce 的旧格式)。
    R47 P1-a 增强:
        - 高风险 action 必须使用 6 段格式(含 nonce),旧 5 段格式直接拒绝
        - 签名长度必须 ≥ 32 hex chars(128 bit)
    R48 P1-b: 添加 resource_version 参数,验证资源版本绑定。
        - 签名时绑定了 resource_version 的 callback,验证时必须传入相同值
        - 防止使用旧按钮操作已更新的资源(资源版本不匹配 → 签名不匹配 → 拒绝)
        - 为空时不检查 resource_version(向后兼容旧 callback)

    注意:本函数为同步 legacy 接口,不执行 nonce 原子消费。
    高风险 action 应改用 verify_button_token(异步,原子消费 nonce)。

    验证流程:
        1. 解析 callback_data 各字段
        2. 验证 user_id 与 current_user_id 匹配(防止跨用户伪造)
        3. 验证未过期(expire_ts > now)
        4. R47 P1-a: 高风险 action 必须为 6 段格式(含 nonce)
        5. R47 P1-a: 验证签名长度 ≥ 32 hex chars(128 bit)
        6. R48 P1-b: 根据 resource_version 是否传入,构造对应的签名 payload
        7. 验证签名匹配(常量时间比较)

    Args:
        callback_data: 待验证的 callback_data
        current_user_id: 当前用户 ID(必须匹配签名中的 user_id)
        resource_version: 资源版本标识(如 file_code + version)。
            签名时绑定了 resource_version 的 callback 必须传入相同值才能通过。
            为空时不检查(向后兼容)。

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
            has_nonce = True
        else:
            # 旧格式(向后兼容): {user_id}:{action}:{data}:{expire_ts}:{signature}
            data = ":".join(parts[2:-2])
            expire_ts = int(parts[-2])
            has_nonce = False
            nonce = ""
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
        # R47 P1-a: 高风险 action 必须使用 6 段格式(含 nonce)
        if _is_high_risk_action(action) and not has_nonce:
            logger.warning(
                f"[button_security] R47 P1-a: 高风险 action={action} "
                f"必须使用 6 段格式(含 nonce),拒绝旧 5 段格式"
            )
            return False, "", ""
        # R47 P1-a: 验证签名长度 ≥ 32 hex chars(128 bit)
        if len(signature) < SIGNATURE_LENGTH:
            logger.debug(
                f"[button_security] 签名长度不足: {len(signature)} < {SIGNATURE_LENGTH}"
            )
            return False, "", ""
        # R48 P1-b: 根据 resource_version 构造签名 payload
        # 签名时若绑定了 resource_version,payload 中包含该字段;
        # 验证时必须传入相同值才能通过签名校验
        if has_nonce:
            if resource_version:
                expected_payload = f"{user_id}:{action}:{data}:{resource_version}:{expire_ts}:{nonce}"
            else:
                expected_payload = f"{user_id}:{action}:{data}:{expire_ts}:{nonce}"
        else:
            if resource_version:
                expected_payload = f"{user_id}:{action}:{data}:{resource_version}:{expire_ts}"
            else:
                expected_payload = f"{user_id}:{action}:{data}:{expire_ts}"
        # 验证签名(常量时间比较,防止时序攻击)
        expected_sig = _sign(expected_payload)
        if not hmac.compare_digest(signature, expected_sig):
            logger.debug("[button_security] 签名不匹配(可能 resource_version 不匹配)")
            return False, "", ""
        return True, action, data
    except (ValueError, IndexError) as e:
        logger.debug(f"[button_security] callback_data 解析失败: {e}")
        return False, "", ""


async def sign_button_token_with_nonce(
    principal_id: int,
    action: str,
    payload: str = "",
    expires_at: Optional[_dt.datetime] = None,
    ttl: int = 3600,
) -> str:
    """R47 P1-a: 生成带 nonce 的签名 button token(持久化到 callback_nonces 表)。

    流程:
        1. 生成 nonce = secrets.token_urlsafe(16)(≥128 bit 熵)
        2. 计算 expire_ts(默认 ttl=3600 秒后,或显式 expires_at)
        3. 调用 store.callback_nonce_create 持久化 nonce 到 callback_nonces 表
        4. 签名包含 nonce(6 段格式)
        5. 返回 token: {principal_id}:{action}:{payload}:{expire_ts}:{nonce}:{signature}

    高风险 action(ban/takedown/purge/restore/admin_grant 等)必须使用本函数
    生成 token,配套 verify_button_token 进行原子消费。

    Args:
        principal_id: 主体 ID(管理员 principal_id)
        action: 动作标识(如 "approval_callback", "ban")
        payload: 附加数据(如 file_code,可为空字符串)
        expires_at: 显式过期时间(优先于 ttl)
        ttl: 有效期(秒,默认 1 小时)

    Returns:
        6 段签名字符串

    Raises:
        RuntimeError: nonce 持久化失败(store 不可用或 DB 错误)
    """
    # 计算过期时间
    if expires_at is not None:
        expire_ts = int(expires_at.timestamp())
        expires_at_dt = expires_at
    else:
        expire_ts = int(time.time()) + ttl
        expires_at_dt = _dt.datetime.fromtimestamp(expire_ts)

    # 生成 nonce(≥128 bit 熵)
    nonce = secrets.token_urlsafe(NONCE_BYTES)

    # 持久化到 callback_nonces 表
    from database import cache_store as _cs
    store = _cs.get_cache_store()
    expires_at_iso = expires_at_dt.isoformat()
    ok = await store.callback_nonce_create(
        nonce=nonce,
        principal_id=principal_id,
        action=action,
        expires_at=expires_at_iso,
    )
    if not ok:
        raise RuntimeError(
            f"R47 P1-a: callback_nonce_create 失败"
            f"(action={action}, principal_id={principal_id})"
        )

    # 签名(含 nonce)
    sig_payload = f"{principal_id}:{action}:{payload}:{expire_ts}:{nonce}"
    signature = _sign(sig_payload)
    return f"{sig_payload}:{signature}"


async def verify_button_token(
    callback_data: str,
    current_user_id: int,
    store=None,
) -> Tuple[bool, str, str]:
    """R47 P1-a: 验证签名 button token + 原子消费 nonce(防重放)。

    与 verify_signed_callback 的区别:
        - 本函数为 async,签名验证通过后调用 store.callback_nonce_consume
        - 同一 nonce 只能消费一次(UPDATE WHERE consumed_at IS NULL 原子操作)
        - 并发调用只有第一个成功,后续全部拒绝
        - 高风险 action 必须使用本函数(verify_signed_callback 不消费 nonce)

    验证流程:
        1. 解析 callback_data 各字段
        2. 验证 user_id 与 current_user_id 匹配
        3. 验证未过期(expire_ts > now)
        4. R47 P1-a: 高风险 action 必须为 6 段格式(含 nonce)
        5. R47 P1-a: 验证签名长度 ≥ 32 hex chars(128 bit)
        6. 验证签名匹配(常量时间比较)
        7. R47 P1-a: 6 段格式,原子消费 nonce(防重放)

    Args:
        callback_data: 待验证的 callback_data
        current_user_id: 当前用户 ID(必须匹配签名中的 user_id)
        store: 可选 CacheStore 实例(测试注入;默认通过 get_cache_store() 获取)

    Returns:
        (valid, action, data): valid=True 时 action/data 可用;
        valid=False 时 action/data 为空字符串
    """
    try:
        parts = callback_data.split(":")
        if len(parts) < 5:
            logger.debug(f"[button_security] callback_data 字段不足: {callback_data}")
            return False, "", ""
        user_id = int(parts[0])
        action = parts[1]
        signature = parts[-1]
        # 判断是否为新的 6 段格式(含 nonce)
        if len(parts) >= 6:
            data = ":".join(parts[2:-3])
            expire_ts = int(parts[-3])
            nonce = parts[-2]
            has_nonce = True
            payload = f"{user_id}:{action}:{data}:{expire_ts}:{nonce}"
        else:
            data = ":".join(parts[2:-2])
            expire_ts = int(parts[-2])
            has_nonce = False
            nonce = ""
            payload = f"{user_id}:{action}:{data}:{expire_ts}"
        # 验证用户 ID
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
        # R47 P1-a: 高风险 action 必须使用 6 段格式(含 nonce)
        if _is_high_risk_action(action) and not has_nonce:
            logger.warning(
                f"[button_security] R47 P1-a: 高风险 action={action} "
                f"必须使用 6 段格式(含 nonce),拒绝旧 5 段格式"
            )
            return False, "", ""
        # R47 P1-a: 验证签名长度 ≥ 32 hex chars(128 bit)
        if len(signature) < SIGNATURE_LENGTH:
            logger.debug(
                f"[button_security] 签名长度不足: {len(signature)} < {SIGNATURE_LENGTH}"
            )
            return False, "", ""
        # 验证签名(常量时间比较)
        expected_sig = _sign(payload)
        if not hmac.compare_digest(signature, expected_sig):
            logger.debug("[button_security] 签名不匹配")
            return False, "", ""
        # R47 P1-a: 6 段格式,原子消费 nonce(防重放)
        if has_nonce and nonce:
            if store is None:
                from database import cache_store as _cs
                store = _cs.get_cache_store()
            consumed = await store.callback_nonce_consume(nonce)
            if not consumed:
                logger.warning(
                    f"[button_security] R47 P1-a: nonce 已消费或不存在,拒绝重放: "
                    f"action={action}"
                )
                return False, "", ""
        return True, action, data
    except (ValueError, IndexError) as e:
        logger.debug(f"[button_security] callback_data 解析失败: {e}")
        return False, "", ""
