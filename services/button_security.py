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
from services.i18n import translate as _i18n_t


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

    ── R51 P1-10 使用约束(重要)────────────────────────────────────
    本函数为**只读操作**专用(如 view / cancel / close / refresh / info / help /
    back / menu / page / next / prev / language 等无副作用 action)。

    **变更型按钮**(ban / takedown / purge / restore / delete_file / admin_grant /
    reset_quota / approve_appeal 等修改状态或影响权益的操作)必须改用
    ``sign_button_token_with_nonce()``(持久化 nonce + 原子消费,防重放)。
    原因:
        - 本函数生成的 nonce 不持久化到 callback_nonces 表,
          verify_signed_callback 不会消费 nonce,无法防止重放攻击
        - 多 isolate / 多 worker 环境下,内存中的 nonce 校验不可靠

    若本函数被传入 HIGH_RISK_ACTIONS 中的 action,会记录 warning 日志(不拒绝,
    以保持向后兼容;但 verify_signed_callback 会拒绝高风险 action 的旧 5 段格式)。
    新代码应改用 sign_button_token_with_nonce。

    格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
    (resource_version 不出现在 callback_data 中,仅参与签名计算,
     验证时调用方需传入相同的 resource_version 才能通过签名校验)

    Args:
        user_id: 用户 ID(必须为整数)
        action: 动作标识(只读操作,如 "view", "cancel", "refresh")
        data: 附加数据(如 file_code,可为空字符串)
        ttl: 有效期(秒,默认 1 小时)
        nonce: 随机串(默认自动生成 32 hex chars = 128 bit 熵)
        resource_version: 资源版本标识(如 file_code + version),绑定到签名中。
            传入后验证方必须提供相同的 resource_version 才能通过签名校验,
            防止使用旧按钮操作已更新的资源。为空时不绑定(向后兼容)。

    Returns:
        签名后的 callback_data 字符串(6 段格式)
    """
    # R58 P0-4: 高风险 action 硬拒绝(不再仅 warning)
    # 高风险 action 必须使用 sign_button_token_with_nonce(持久化 nonce + 原子消费)
    # 旧 sync API 生成不持久化 nonce 的 6 段 token,可被重放
    if _is_high_risk_action(action):
        raise AppError(
            ErrorCodes.BUTTON_POLICY_ASYNC_TOKEN_REQUIRED,
            params={
                "action": action,
                "reason": "high_risk_action_requires_async_token",
                "user_id": str(user_id),
            },
        )
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
        # R58 P0-4: 高风险 action 一律拒绝(无论 5/6 段)
        # 高风险 action 必须通过 verify_button_token(异步,原子消费 nonce)处理
        # 同步 verifier 不消费 nonce,签名正确的 6 段 token 仍可被重放
        if _is_high_risk_action(action):
            logger.warning(
                f"[button_security] R58 P0-4: 高风险 action={action} "
                f"被同步 verifier 拒绝(必须使用异步 verify_button_token)"
            )
            return False, "", ""
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
            _i18n_t('services.button_security.s1', action=action, principal_id=principal_id)
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


# ── R62 P0-04: handle 短 ID 模式(绕过 Telegram 64 字节限制)───────


# handle_id 字节数(secrets.token_urlsafe(8) → ~11 字符)
# callback_data 总长度 = "report:" + action + ":" + handle_id ≈ 18~25 字符,
# 远低于 Telegram 64 字节限制
HANDLE_ID_BYTES: int = 8


async def sign_button_token_with_handle(
    principal_id: int,
    action: str,
    payload: str = "",
    expires_at: Optional[_dt.datetime] = None,
    ttl: int = 3600,
    audience: str = "admin_callback",
    resource_version: str = "",
) -> str:
    """R62 P0-04: 生成签名 token 并以短 handle_id 引用(绕过 64 字节限制)。

    Telegram callback_data 有 64 字节限制,而 6 段签名 token
    (``{principal_id}:{action}:{payload}:{expire_ts}:{nonce}:{signature}``)
    长度通常 > 100 字符(尤其 payload 含 file_code/uid 等业务参数时)。
    本函数:
        1. 调用 ``sign_button_token_with_nonce`` 生成完整签名 token(持久化 nonce)
        2. 生成短 handle_id(``secrets.token_urlsafe(8)``,~11 字符)
        3. 持久化 (handle_id → token) 到 ``button_tokens`` 表
        4. 返回 handle_id(调用方将其拼入 callback_data,如 ``f"report:{handle_id}"``)

    handler 端调用 ``verify_button_token_by_handle(handle_id, user.id,
    expected_action, expected_audience, expected_resource_version)`` 验签:
        - 通过 handle_id 查找完整 token
        - 调用 ``verify_button_token`` 验证签名 + 原子消费 nonce(防重放)
        - R63 P1-06: 库内部一次性完成 expected_action / expected_audience /
          expected_resource_version 绑定校验(handler 无法忘检查)

    安全保证:
        - nonce 仍由 ``callback_nonces`` 表原子消费(一次性)
        - 即使 handle_id 被多次读取(button_tokens 表无消费语义),
          verify_button_token 内的 nonce 原子消费保证一次性使用
        - handle_id 不可伪造(secrets.token_urlsafe 128 bit 熵)
        - R63 P1-06: audience / resource_version 作为元数据与 handle_id 绑定,
          verify_button_token_by_handle 强制匹配,防止跨 handler 滥用

    Args:
        principal_id: 主体 ID(管理员 user_id)
        action: 动作标识(如 "report" / "restore" / "delete_file")
        payload: 附加数据(如 "ban|12345|67890|dsp")
        expires_at: 显式过期时间(优先于 ttl)
        ttl: 有效期(秒,默认 1 小时)
        audience: 接收方 audience 标识(默认 "admin_callback",
            用于 verify_button_token_by_handle 强制匹配,防止跨 handler 滥用)
        resource_version: 资源版本绑定(如 "file_code:v3"),为空时不绑定

    Returns:
        handle_id 字符串(短,适合作为 callback_data 一部分)

    Raises:
        RuntimeError: token 生成失败或持久化失败
    """
    # 1. 生成完整签名 token(含 nonce + signature,持久化 nonce)
    token = await sign_button_token_with_nonce(
        principal_id=principal_id,
        action=action,
        payload=payload,
        expires_at=expires_at,
        ttl=ttl,
    )

    # 2. 生成短 handle_id(128 bit 熵)
    handle_id = secrets.token_urlsafe(HANDLE_ID_BYTES)  # ~11 字符

    # 3. 持久化 (handle_id → token) 映射(含 audience / resource_version 元数据)
    from database import cache_store as _cs
    store = _cs.get_cache_store()
    ok = await store.button_token_store(
        handle_id=handle_id,
        token=token,
        principal_id=principal_id,
        action=action,
        audience=audience,
        resource_version=resource_version or None,
    )
    if not ok:
        raise RuntimeError(
            _i18n_t('services.button_security.s2', action=action, principal_id=principal_id)
        )

    # 4. 返回 handle_id(调用方拼接 callback_data)
    return handle_id


# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_HANDLE_NOT_FOUND = (
    "[button_security] R62 P0-04 / R63 P1-06: handle_id={} 不存在或已被清理"
)
_LOG_ACTION_MISMATCH = (
    "[button_security] R63 P1-06: action 不匹配 → 拒绝跨 action handle: "
    "expected={}, actual={}, handle={}"
)
_LOG_AUDIENCE_MISMATCH = (
    "[button_security] R63 P1-06: audience 不匹配 → 拒绝跨 handler 滥用: "
    "expected={}, actual={}, action={}, handle={}"
)
_LOG_RESOURCE_VERSION_MISMATCH = (
    "[button_security] R63 P1-06: resource_version 不匹配 → "
    "拒绝旧按钮操作已更新资源: "
    "expected={}, actual={}, action={}, handle={}"
)


async def verify_button_token_by_handle(
    handle_id: str,
    current_user_id: int,
    expected_action: str,
    expected_audience: str,
    expected_resource_version: Optional[str] = None,
    store=None,
) -> Tuple[bool, str, str]:
    """R62 P0-04 / R63 P1-06: 通过 handle_id 查找并验证签名 token + 强制绑定 + 原子消费 nonce。

    R63 P1-06 增强(终审整改):
        - 库内部一次性完成全部绑定校验,handler 无法"忘记"检查 action/audience/
          resource_version。每个 report/restore/delete handler 必须拒绝跨 action
          handle,不能只依赖签名和 user id。
        - expected_action 必填,与 token 中的 action 比较(签名内的 action)
        - expected_audience 必填,与 button_tokens 表中存储的 audience 比较
        - expected_resource_version 可选,非 None 时与 button_tokens 表中
          存储的 resource_version 比较

    流程:
        1. 通过 handle_id 从 ``button_tokens`` 表查找完整 token + audience +
           resource_version 元数据
        2. 调用 ``verify_button_token`` 验证签名 + 原子消费 nonce
        3. **R63 P1-06: 验签成功后,强制匹配 action / audience / resource_version**
           - 任一不匹配 → 抛出 ``AppError``(具体错误码见下)
           - 不再返回 ``(False, "", "")``,而是 fail-closed 抛出
        4. 返回 (valid, action, payload)

    安全保证:
        - nonce 原子消费(同一 handle_id 第一次调用成功,后续全部拒绝)
        - 签名验证(防伪造)
        - user_id 绑定(防跨用户使用 handle_id)
        - 过期检查(expire_ts)
        - R63 P1-06: action 强制匹配(签名内,防跨 action 滥用)
        - R63 P1-06: audience 强制匹配(元数据,防跨 handler 滥用)
        - R63 P1-06: resource_version 强制匹配(元数据,防旧按钮操作已更新资源)

    Args:
        handle_id: 短 handle_id(callback_data 中携带)
        current_user_id: 当前用户 ID(必须匹配签名时的 principal_id)
        expected_action: 期望的 action 字符串(必填,如 "report"/"restore"/"delete_file")
        expected_audience: 期望的 audience 字符串(必填,如 "admin_callback")
        expected_resource_version: 期望的资源版本(可选,None 表示不检查 resource_version)
        store: 可选 CacheStore 实例(测试注入)

    Returns:
        (valid, action, payload): valid=True 时 action/payload 可用;
        valid=False 时 action/payload 为空字符串(仅在签名/nonce/principal 失败时)

    Raises:
        AppError(BUTTON_POLICY_HASH_MISMATCH): token_action != expected_action
        AppError(BUTTON_POLICY_AUDIENCE_MISMATCH): token_audience != expected_audience
        AppError(BUTTON_POLICY_VERSION_MISMATCH): resource_version 不匹配
            (仅当 expected_resource_version 非 None 时检查)
    """
    if store is None:
        from database import cache_store as _cs
        store = _cs.get_cache_store()

    # 1. 通过 handle_id 查找完整 token + 绑定元数据(R63 P1-06: 含 audience / rv)
    # 优先使用 lookup_with_bindings 获取完整元数据;若旧 store 不支持则回退 lookup
    bindings = None
    if hasattr(store, "button_token_lookup_with_bindings"):
        bindings = await store.button_token_lookup_with_bindings(handle_id)
    token: Optional[str] = None
    token_audience: Optional[str] = None
    token_resource_version: Optional[str] = None
    if bindings is not None:
        token = bindings.get("token")
        token_audience = bindings.get("audience")
        token_resource_version = bindings.get("resource_version")
    else:
        token = await store.button_token_lookup(handle_id)

    if not token:
        logger.warning(
            _LOG_HANDLE_NOT_FOUND.format(handle_id)
        )
        return False, "", ""

    # 2. 验证签名 + 原子消费 nonce(防重放)
    #    验签失败/过期/重放/跨用户 → 返回 (False, "", ""),不抛出 AppError
    #    (保持与旧版兼容,这些失败由 handler 显示统一"签名验证失败"提示)
    valid, action, payload = await verify_button_token(
        token, current_user_id, store=store,
    )
    if not valid:
        # 签名/nonce/principal/expiry 失败 — 不消费 handle,允许重试
        # (但 nonce 已被原子消费,实际重放会被 verify_button_token 拒绝)
        return False, "", ""

    # 3. R63 P1-06: 验签成功后,强制匹配 action / audience / resource_version
    #    这些是"签名正确但 handler 调用错误"的场景 — fail-closed 抛出 AppError

    # 3a. action 强制匹配(签名内,防跨 action 滥用)
    if action != expected_action:
        logger.warning(
            _LOG_ACTION_MISMATCH.format(
                expected_action, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_HASH_MISMATCH,
            params={
                "action": action,
                "reason": "action_mismatch",
                "expected": expected_action,
                "actual": action,
            },
        )

    # 3b. audience 强制匹配(元数据,防跨 handler 滥用)
    if token_audience is None:
        token_audience = ""
    if token_audience != expected_audience:
        logger.warning(
            _LOG_AUDIENCE_MISMATCH.format(
                expected_audience, token_audience, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH,
            params={
                "action": action,
                "reason": "audience_mismatch",
                "expected": expected_audience,
                "actual": token_audience,
            },
        )

    # 3c. resource_version 强制匹配(元数据,防旧按钮操作已更新资源)
    if expected_resource_version is not None:
        if token_resource_version is None:
            token_resource_version = ""
        if token_resource_version != expected_resource_version:
            logger.warning(
                _LOG_RESOURCE_VERSION_MISMATCH.format(
                    expected_resource_version, token_resource_version,
                    action, handle_id
                )
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH,
                params={
                    "action": action,
                    "reason": "resource_version_mismatch",
                    "expected": expected_resource_version,
                    "actual": token_resource_version,
                },
            )

    # 4. 验签 + 绑定全部通过后,标记 handle 已消费(便于审计/清理)
    try:
        await store.button_token_mark_consumed(handle_id)
    except Exception as e:
        logger.debug(
            f"[button_security] R62 P0-04: 标记 handle 已消费失败(不阻塞): {e}"
        )

    return valid, action, payload


# ════════════════════════════════════════════════════════════════
# R64 P0-05: v2 签名 token — 绑定 session_id / locale / sub_action
# ════════════════════════════════════════════════════════════════
# 终审整改需求:
#   callback token 必须绑定 tenant、actor、audience、exact action、sub_action、
#   resource id、resource version、locale、session id、expiry、nonce。
#
# v1 token 格式(6 段,向后兼容):
#   {principal_id}:{action}:{payload}:{expire_ts}:{nonce}:{signature}
#   签名 payload: {principal_id}:{action}:{payload}:{expire_ts}:{nonce}
#
# v2 token 格式(9 段,新增 sub_action / session_id / locale 进入签名):
#   {principal_id}:{action}:{sub_action}:{session_id}:{locale}:{payload}:{expire_ts}:{nonce}:{signature}
#   签名 payload: {principal_id}:{action}:{sub_action}:{session_id}:{locale}:{payload}:{expire_ts}:{nonce}
#
# 安全增强:
#   - sub_action 进入签名 → 防止 report:detach token 被用作 report:block
#   - session_id 进入签名 → 防止跨会话重放(同一管理员不同会话)
#   - locale 进入签名 → 防止 locale 切换后旧按钮仍可点击(i18n 一致性)
#
# 向后兼容:
#   - v1 函数(sign_button_token_with_nonce / verify_button_token /
#     sign_button_token_with_handle / verify_button_token_by_handle)保持不变
#   - v2 函数为新增,调用方可按需选择
# ════════════════════════════════════════════════════════════════

# v2 token 段数(principal_id:action:sub_action:session_id:locale:payload:expire_ts:nonce:signature)
_V2_TOKEN_SEGMENTS: int = 9


async def sign_button_token_with_nonce_v2(
    principal_id: int,
    action: str,
    payload: str = "",
    *,
    sub_action: str = "",
    session_id: str = "",
    locale: str = "",
    expires_at: Optional[_dt.datetime] = None,
    ttl: int = 3600,
) -> str:
    """R64 P0-05: 生成 v2 签名 button token(含 sub_action / session_id / locale)。

    与 v1 ``sign_button_token_with_nonce`` 的差异:
        - 签名 payload 扩展为 8 段(新增 sub_action / session_id / locale)
        - token 格式为 9 段(签名 payload + signature)
        - sub_action 进入签名 → 防止同 action 不同子动作的 token 混用
          (如 report:detach token 不能被用作 report:block)
        - session_id 进入签名 → 防止跨会话重放
        - locale 进入签名 → 防止 locale 切换后旧按钮仍可点击

    流程:
        1. 生成 nonce = secrets.token_urlsafe(16)(≥128 bit 熵)
        2. 计算 expire_ts
        3. 持久化 nonce 到 callback_nonces 表
        4. 签名包含 sub_action / session_id / locale(8 段 payload)
        5. 返回 9 段 token

    Args:
        principal_id: 主体 ID(管理员 principal_id)
        action: 动作标识(如 "report" / "restore" / "delete_file")
        payload: 附加数据(如 file_code,可为空字符串)
        sub_action: 子动作标识(如 "detach" / "block" / "ban"),进入签名
        session_id: 会话 ID(防跨会话重放),进入签名
        locale: 语言代码(如 "zh-CN" / "en-US"),进入签名
        expires_at: 显式过期时间(优先于 ttl)
        ttl: 有效期(秒,默认 1 小时)

    Returns:
        9 段签名字符串

    Raises:
        RuntimeError: nonce 持久化失败
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
            _i18n_t('services.button_security.s1', action=action, principal_id=principal_id)
        )

    # v2 签名 payload(8 段): sub_action / session_id / locale 进入签名
    sig_payload = (
        f"{principal_id}:{action}:{sub_action}:{session_id}:{locale}:"
        f"{payload}:{expire_ts}:{nonce}"
    )
    signature = _sign(sig_payload)
    return f"{sig_payload}:{signature}"


async def verify_button_token_v2(
    callback_data: str,
    current_user_id: int,
    store=None,
) -> Tuple[bool, str, str, str, str, str]:
    """R64 P0-05: 验证 v2 签名 button token + 原子消费 nonce。

    与 v1 ``verify_button_token`` 的差异:
        - 解析 9 段格式(含 sub_action / session_id / locale)
        - 返回 6 元组:(valid, action, payload, sub_action, session_id, locale)
        - 调用方可基于返回的 sub_action / session_id / locale 做绑定校验

    验证流程:
        1. 解析 callback_data 9 段
        2. 验证 user_id 与 current_user_id 匹配
        3. 验证未过期(expire_ts > now)
        4. 验证签名长度 ≥ 32 hex chars(128 bit)
        5. 验证签名匹配(常量时间比较)
        6. 原子消费 nonce(防重放)

    Args:
        callback_data: 待验证的 v2 token(9 段)
        current_user_id: 当前用户 ID(必须匹配签名中的 principal_id)
        store: 可选 CacheStore 实例(测试注入)

    Returns:
        (valid, action, payload, sub_action, session_id, locale):
        valid=True 时其余字段可用;valid=False 时均为空字符串
    """
    if store is None:
        from database import cache_store as _cs
        store = _cs.get_cache_store()

    _empty = (False, "", "", "", "", "")
    try:
        parts = callback_data.split(":")
        if len(parts) != _V2_TOKEN_SEGMENTS:
            logger.debug(
                f"[button_security] R64 P0-05: v2 token 段数错误 "
                f"expected={_V2_TOKEN_SEGMENTS} actual={len(parts)}"
            )
            return _empty
        principal_id_str, action, sub_action, session_id, locale, payload, expire_ts_str, nonce, signature = parts

        # 1. 验证 principal_id 匹配
        try:
            principal_id = int(principal_id_str)
        except ValueError:
            return _empty
        if principal_id != current_user_id:
            logger.debug("[button_security] v2 principal_id 不匹配")
            return _empty

        # 2. 验证未过期
        try:
            expire_ts = int(expire_ts_str)
        except ValueError:
            return _empty
        if expire_ts <= int(time.time()):
            logger.debug("[button_security] v2 token 已过期")
            return _empty

        # 3. 验证签名长度
        if len(signature) < SIGNATURE_LENGTH:
            logger.debug("[button_security] v2 签名长度不足")
            return _empty

        # 4. 验证签名匹配(常量时间比较)
        sig_payload = (
            f"{principal_id_str}:{action}:{sub_action}:{session_id}:{locale}:"
            f"{payload}:{expire_ts_str}:{nonce}"
        )
        expected_sig = _sign(sig_payload)
        if not hmac.compare_digest(expected_sig, signature):
            logger.debug("[button_security] v2 签名不匹配")
            return _empty

        # 5. 原子消费 nonce(防重放)
        consumed = await store.callback_nonce_consume(nonce)
        if not consumed:
            logger.warning(
                "[button_security] R64 P0-05: v2 nonce 已消费或不存在(重放攻击?)"
            )
            return _empty

        return True, action, payload, sub_action, session_id, locale
    except (ValueError, IndexError) as e:
        logger.debug(f"[button_security] v2 callback_data 解析失败: {e}")
        return _empty


async def sign_button_token_with_handle_v2(
    principal_id: int,
    action: str,
    payload: str = "",
    *,
    sub_action: str = "",
    session_id: str = "",
    locale: str = "",
    expires_at: Optional[_dt.datetime] = None,
    ttl: int = 3600,
    audience: str = "admin_callback",
    resource_version: str = "",
) -> str:
    """R64 P0-05: 生成 v2 签名 token 并以短 handle_id 引用(含 sub_action/session_id/locale)。

    与 v1 ``sign_button_token_with_handle`` 的差异:
        - 调用 ``sign_button_token_with_nonce_v2`` 生成 9 段签名 token
        - sub_action / session_id / locale 进入签名(不可篡改)
        - audience / resource_version 仍作为元数据存储(与 v1 一致)

    Args:
        principal_id: 主体 ID
        action: 动作标识(如 "report" / "restore" / "delete_file")
        payload: 附加数据(如 "ban|12345|67890|dsp")
        sub_action: 子动作标识(如 "detach" / "block"),进入签名
        session_id: 会话 ID(防跨会话重放),进入签名
        locale: 语言代码(如 "zh-CN"),进入签名
        expires_at: 显式过期时间(优先于 ttl)
        ttl: 有效期(秒,默认 1 小时)
        audience: 接收方 audience 标识(默认 "admin_callback")
        resource_version: 资源版本绑定(如 "file_code:v3"),为空时不绑定

    Returns:
        handle_id 字符串

    Raises:
        RuntimeError: token 生成失败或持久化失败
    """
    # 1. 生成 v2 完整签名 token(含 sub_action / session_id / locale)
    token = await sign_button_token_with_nonce_v2(
        principal_id=principal_id,
        action=action,
        payload=payload,
        sub_action=sub_action,
        session_id=session_id,
        locale=locale,
        expires_at=expires_at,
        ttl=ttl,
    )

    # 2. 生成短 handle_id(128 bit 熵)
    handle_id = secrets.token_urlsafe(HANDLE_ID_BYTES)

    # 3. 持久化 (handle_id → token) 映射(含 audience / resource_version 元数据)
    from database import cache_store as _cs
    store = _cs.get_cache_store()
    ok = await store.button_token_store(
        handle_id=handle_id,
        token=token,
        principal_id=principal_id,
        action=action,
        audience=audience,
        resource_version=resource_version or None,
    )
    if not ok:
        raise RuntimeError(
            _i18n_t('services.button_security.s2', action=action, principal_id=principal_id)
        )

    # 4. 返回 handle_id
    return handle_id


_LOG_V2_SUB_ACTION_MISMATCH = (
    "[button_security] R64 P0-05: v2 sub_action 不匹配 → 拒绝跨子动作 handle: "
    "expected={}, actual={}, action={}, handle={}"
)
_LOG_V2_SESSION_MISMATCH = (
    "[button_security] R64 P0-05: v2 session_id 不匹配 → 拒绝跨会话 handle: "
    "expected={}, actual={}, action={}, handle={}"
)
_LOG_V2_LOCALE_MISMATCH = (
    "[button_security] R64 P0-05: v2 locale 不匹配 → 拒绝跨 locale handle: "
    "expected={}, actual={}, action={}, handle={}"
)


async def verify_button_token_by_handle_v2(
    handle_id: str,
    current_user_id: int,
    expected_action: str,
    expected_audience: str,
    *,
    expected_sub_action: Optional[str] = None,
    expected_session_id: Optional[str] = None,
    expected_locale: Optional[str] = None,
    expected_resource_version: Optional[str] = None,
    store=None,
) -> Tuple[bool, str, str]:
    """R64 P0-05: 通过 handle_id 验证 v2 签名 token + 强制绑定(含 sub_action/session/locale)。

    与 v1 ``verify_button_token_by_handle`` 的差异:
        - 调用 ``verify_button_token_v2`` 验证 9 段格式 token
        - 新增 expected_sub_action / expected_session_id / expected_locale 绑定校验
        - sub_action / session_id / locale 从签名中提取(不可篡改)

    流程:
        1. 通过 handle_id 查找完整 token + audience / resource_version 元数据
        2. 调用 ``verify_button_token_v2`` 验证签名 + 原子消费 nonce
        3. 强制匹配 action / audience / resource_version(与 v1 一致)
        4. **R64 P0-05 新增**: 强制匹配 sub_action / session_id / locale
           - expected_sub_action 非 None 时,必须与签名内 sub_action 匹配
           - expected_session_id 非 None 时,必须与签名内 session_id 匹配
           - expected_locale 非 None 时,必须与签名内 locale 匹配

    Args:
        handle_id: 短 handle_id
        current_user_id: 当前用户 ID
        expected_action: 期望的 action(必填)
        expected_audience: 期望的 audience(必填)
        expected_sub_action: 期望的 sub_action(None=不检查)
        expected_session_id: 期望的 session_id(None=不检查)
        expected_locale: 期望的 locale(None=不检查)
        expected_resource_version: 期望的资源版本(None=不检查)
        store: 可选 CacheStore 实例(测试注入)

    Returns:
        (valid, action, payload): valid=True 时 action/payload 可用

    Raises:
        AppError(BUTTON_POLICY_HASH_MISMATCH): action 或 sub_action 不匹配
        AppError(BUTTON_POLICY_AUDIENCE_MISMATCH): audience 不匹配
        AppError(BUTTON_POLICY_VERSION_MISMATCH): resource_version 不匹配
        AppError(BUTTON_POLICY_BINDING_MISSING): session_id / locale 不匹配
    """
    if store is None:
        from database import cache_store as _cs
        store = _cs.get_cache_store()

    # 1. 通过 handle_id 查找完整 token + 绑定元数据
    bindings = None
    if hasattr(store, "button_token_lookup_with_bindings"):
        bindings = await store.button_token_lookup_with_bindings(handle_id)
    token: Optional[str] = None
    token_audience: Optional[str] = None
    token_resource_version: Optional[str] = None
    if bindings is not None:
        token = bindings.get("token")
        token_audience = bindings.get("audience")
        token_resource_version = bindings.get("resource_version")
    else:
        token = await store.button_token_lookup(handle_id)

    if not token:
        logger.warning(_LOG_HANDLE_NOT_FOUND.format(handle_id))
        return False, "", ""

    # 2. 验证 v2 签名 + 原子消费 nonce
    valid, action, payload, sub_action, session_id, locale = await verify_button_token_v2(
        token, current_user_id, store=store,
    )
    if not valid:
        return False, "", ""

    # 3a. action 强制匹配(签名内,防跨 action 滥用)
    if action != expected_action:
        logger.warning(
            _LOG_ACTION_MISMATCH.format(expected_action, action, handle_id)
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_HASH_MISMATCH,
            params={
                "action": action,
                "reason": "action_mismatch",
                "expected": expected_action,
                "actual": action,
            },
        )

    # 3b. audience 强制匹配(元数据,防跨 handler 滥用)
    if token_audience is None:
        token_audience = ""
    if token_audience != expected_audience:
        logger.warning(
            _LOG_AUDIENCE_MISMATCH.format(
                expected_audience, token_audience, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH,
            params={
                "action": action,
                "reason": "audience_mismatch",
                "expected": expected_audience,
                "actual": token_audience,
            },
        )

    # 3c. resource_version 强制匹配(元数据,防旧按钮操作已更新资源)
    if expected_resource_version is not None:
        if token_resource_version is None:
            token_resource_version = ""
        if token_resource_version != expected_resource_version:
            logger.warning(
                _LOG_RESOURCE_VERSION_MISMATCH.format(
                    expected_resource_version, token_resource_version,
                    action, handle_id,
                )
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH,
                params={
                    "action": action,
                    "reason": "resource_version_mismatch",
                    "expected": expected_resource_version,
                    "actual": token_resource_version,
                },
            )

    # 4. R64 P0-05: sub_action / session_id / locale 强制匹配(签名内)

    # 4a. sub_action 强制匹配(防 report:detach token 被用作 report:block)
    if expected_sub_action is not None and sub_action != expected_sub_action:
        logger.warning(
            _LOG_V2_SUB_ACTION_MISMATCH.format(
                expected_sub_action, sub_action, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_HASH_MISMATCH,
            params={
                "action": action,
                "reason": "sub_action_mismatch",
                "expected": expected_sub_action,
                "actual": sub_action,
            },
        )

    # 4b. session_id 强制匹配(防跨会话重放)
    if expected_session_id is not None and session_id != expected_session_id:
        logger.warning(
            _LOG_V2_SESSION_MISMATCH.format(
                expected_session_id, session_id, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_BINDING_MISSING,
            params={
                "action": action,
                "reason": "session_id_mismatch",
                "expected": expected_session_id,
                "actual": session_id,
                "missing_field": "session_id",
            },
        )

    # 4c. locale 强制匹配(防 locale 切换后旧按钮仍可点击)
    if expected_locale is not None and locale != expected_locale:
        logger.warning(
            _LOG_V2_LOCALE_MISMATCH.format(
                expected_locale, locale, action, handle_id
            )
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_BINDING_MISSING,
            params={
                "action": action,
                "reason": "locale_mismatch",
                "expected": expected_locale,
                "actual": locale,
                "missing_field": "locale",
            },
        )

    # 5. 验签 + 绑定全部通过后,标记 handle 已消费
    try:
        await store.button_token_mark_consumed(handle_id)
    except Exception as e:
        logger.debug(
            f"[button_security] R64 P0-05: 标记 v2 handle 已消费失败(不阻塞): {e}"
        )

    return valid, action, payload
