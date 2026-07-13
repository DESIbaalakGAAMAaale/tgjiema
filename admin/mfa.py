"""R40 P2-5: 多因素认证(MFA)管理器 — 真实 TOTP 实现。

职责:
    为 Admin Web 提供 TOTP 双因素认证能力(基于 pyotp):
    1. generate_totp_secret(user_id) — 生成 TOTP 密钥并存储
    2. verify_totp_code(user_id, code) — 校验 TOTP 验证码
    3. is_mfa_enabled(user_id) — 判断是否已启用 MFA

设计原则:
    - 真实实现使用 pyotp.TOTP 验证 6 位 TOTP 代码
    - fail-closed:pyotp 未安装或异常时返回 False(拒绝验证)
    - 密钥存储到 SQLite kv_store,key 前缀 admin:mfa:secret:<user_id>
    - 允许 ±30s 时间漂移(valid_window=1)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import secrets
from typing import Optional

from loguru import logger

# MFA 密钥在 kv_store 中的 key 前缀
_MFA_SECRET_KEY_PREFIX = "admin:mfa:secret:"
# MFA 启用状态在 kv_store 中的 key 前缀
_MFA_ENABLED_KEY_PREFIX = "admin:mfa:enabled:"
# TOTP 密钥长度(pyotp 默认 base32 32 字符)
_TOTP_SECRET_LENGTH = 32


def _make_secret_key(user_id: int) -> str:
    """构造 MFA 密钥的 kv_store key。"""
    return f"{_MFA_SECRET_KEY_PREFIX}{user_id}"


def _make_enabled_key(user_id: int) -> str:
    """构造 MFA 启用状态的 kv_store key。"""
    return f"{_MFA_ENABLED_KEY_PREFIX}{user_id}"


def _generate_totp_secret() -> str:
    """生成 TOTP 密钥(基于 pyotp.random_base32())。

    优先使用 pyotp.random_base32() 生成 base32 编码的 32 字符密钥;
    pyotp 未安装时降级到 secrets.token_hex(16)(兼容占位实现,但建议安装 pyotp)。

    Returns:
        32 字符密钥字符串
    """
    try:
        import pyotp
        return pyotp.random_base32(length=_TOTP_SECRET_LENGTH)
    except ImportError:
        # pyotp 未安装,降级到 hex(仅开发环境,生产建议安装 pyotp)
        logger.warning("[admin.mfa] pyotp 未安装,降级到 hex 密钥(建议安装 pyotp>=2.9.0)")
        return secrets.token_hex(_TOTP_SECRET_LENGTH // 2)


def _verify_totp(secret: str, code: str) -> bool:
    """校验 TOTP 验证码(基于 pyotp)。

    使用 pyotp.TOTP(secret).verify(code, valid_window=1) 验证 6 位 TOTP 代码。
    valid_window=1 允许 ±30s 时间漂移(防止客户端时钟偏差导致验证失败)。

    fail-closed:pyotp 未安装或异常时返回 False,防止绕过 MFA。

    Args:
        secret: TOTP 密钥(base32 编码的 32 字符)
        code: 用户输入的 6 位验证码

    Returns:
        True=验证通过;False=验证失败或异常
    """
    if not secret or not code:
        return False
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)
    except ImportError:
        # pyotp 未安装时,fail-closed(拒绝验证)
        logger.error("[admin.mfa] pyotp 未安装,MFA 验证失败(fail-closed)")
        return False
    except Exception as e:
        logger.warning(f"[admin.mfa] TOTP 验证异常: {e}")
        return False


class MFAManager:
    """R40 P2-5: 多因素认证(MFA)管理器。

    用法:
        manager = MFAManager()
        # 启用 MFA(管理员首次设置)
        secret = await manager.generate_totp_secret(user_id)
        # 展示二维码供用户扫描...
        # 验证用户输入的 6 位验证码
        ok = await manager.verify_totp_code(user_id, code)
        if ok:
            await manager.enable_mfa(user_id)
        # 判断是否已启用
        if await manager.is_mfa_enabled(user_id):
            # 强制要求 MFA 验证码
    """

    async def generate_totp_secret(self, user_id: int) -> str:
        """为用户生成 TOTP 密钥并存储。

        Args:
            user_id: 用户 ID(管理员 principal_id)

        Returns:
            32 字符 TOTP 密钥;失败返回空字符串
        """
        if not user_id:
            return ""
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return ""
            secret = _generate_totp_secret()
            await store.set_kv(_make_secret_key(user_id), secret)
            logger.info(f"[admin.mfa] 生成 TOTP 密钥 user_id={user_id}")
            return secret
        except Exception as e:
            logger.warning(f"[admin.mfa] 生成 TOTP 密钥失败: {e}")
            return ""

    async def verify_totp_code(self, user_id: int, code: str) -> bool:
        """校验用户的 TOTP 验证码。

        占位实现始终返回 False,确保强制启用 MFA 时无法通过验证。

        Args:
            user_id: 用户 ID
            code: 6 位验证码字符串

        Returns:
            True=验证通过;False=验证失败或未配置
        """
        if not user_id or not code:
            return False
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            secret = await store.get_kv(_make_secret_key(user_id))
            if not secret:
                return False
            return _verify_totp(secret, code)
        except Exception as e:
            logger.debug(f"[admin.mfa] 校验 TOTP 失败: {e}")
            return False

    async def is_mfa_enabled(self, user_id: int) -> bool:
        """判断用户是否已启用 MFA。

        Args:
            user_id: 用户 ID

        Returns:
            True=已启用;False=未启用或查询失败
        """
        if not user_id:
            return False
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            enabled = await store.get_kv(_make_enabled_key(user_id))
            return enabled == "1"
        except Exception as e:
            logger.debug(f"[admin.mfa] 查询 MFA 启用状态失败: {e}")
            return False

    async def enable_mfa(self, user_id: int) -> bool:
        """启用用户的 MFA(在用户首次验证通过后调用)。

        Args:
            user_id: 用户 ID

        Returns:
            True=成功;False=失败
        """
        if not user_id:
            return False
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            await store.set_kv(_make_enabled_key(user_id), "1")
            logger.info(f"[admin.mfa] 启用 MFA user_id={user_id}")
            return True
        except Exception as e:
            logger.warning(f"[admin.mfa] 启用 MFA 失败: {e}")
            return False

    async def disable_mfa(self, user_id: int) -> bool:
        """禁用用户的 MFA。

        Args:
            user_id: 用户 ID

        Returns:
            True=成功;False=失败
        """
        if not user_id:
            return False
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            await store._db.execute(
                "DELETE FROM kv_store WHERE key = ?",
                (_make_enabled_key(user_id),),
            )
            await store._db.execute(
                "DELETE FROM kv_store WHERE key = ?",
                (_make_secret_key(user_id),),
            )
            await store._db.commit()
            logger.info(f"[admin.mfa] 禁用 MFA user_id={user_id}")
            return True
        except Exception as e:
            logger.warning(f"[admin.mfa] 禁用 MFA 失败: {e}")
            return False


# 模块级单例
_mfa_manager: Optional[MFAManager] = None


def get_mfa_manager() -> MFAManager:
    """获取 MFAManager 单例。"""
    global _mfa_manager
    if _mfa_manager is None:
        _mfa_manager = MFAManager()
    return _mfa_manager
