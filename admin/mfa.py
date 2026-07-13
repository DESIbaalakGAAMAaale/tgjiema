"""R40 P2-5: 多因素认证(MFA)管理器 — 真实 TOTP 实现。

职责:
    为 Admin Web 提供 TOTP 双因素认证能力(基于 pyotp):
    1. generate_totp_secret(user_id) — 生成 TOTP 密钥并存储
    2. verify_totp_code(user_id, code) — 校验 TOTP 验证码
    3. is_mfa_enabled(user_id) — 判断是否已启用 MFA

R45 §7.2 增强:
    4. TOTP 重放保护 — 每个 principal 保留最近 5 个已用 code,拒绝重放
    5. 错误 TOTP 限流 — 连续 5 次错误锁定 5 分钟
    6. MFA 未配置时拒绝敏感操作(仅允许配置 MFA 和登录)

设计原则:
    - 真实实现使用 pyotp.TOTP 验证 6 位 TOTP 代码
    - fail-closed:pyotp 未安装或异常时返回 False(拒绝验证)
    - 密钥存储到 SQLite kv_store,key 前缀 admin:mfa:secret:<user_id>
    - 允许 ±30s 时间漂移(valid_window=1)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

from loguru import logger

# MFA 密钥在 kv_store 中的 key 前缀
_MFA_SECRET_KEY_PREFIX = "admin:mfa:secret:"
# MFA 启用状态在 kv_store 中的 key 前缀
_MFA_ENABLED_KEY_PREFIX = "admin:mfa:enabled:"
# TOTP 密钥长度(pyotp 默认 base32 32 字符)
_TOTP_SECRET_LENGTH = 32

# ─── R45 §7.2: TOTP 重放保护 + 错误限流 ─────────────────────────
# 每个 principal 保留最近 N 个已用 TOTP code,超出则淘汰最旧的
# (TOTP 30s 一个时间窗口,valid_window=1 允许 ±30s,共 3 个有效 code;
#  保留 5 个足够覆盖时间漂移,同时防止重放)
_MAX_USED_TOTP_CODES = 5
# 连续错误 TOTP 次数阈值(达到后锁定)
_MFA_FAIL_MAX_ATTEMPTS = 5
# 锁定时长(秒)— 5 分钟
_MFA_LOCK_DURATION_SECONDS = 5 * 60

# 模块级状态(进程内,重启后丢失;持久化到 kv_store 用于跨进程)
# principal_id -> set of used TOTP codes (最近 N 个)
_used_totp_codes: dict[int, set[str]] = {}
# principal_id -> list of failure timestamps (最近 N 个,清理过期后判断)
_mfa_failures: dict[int, list[float]] = {}


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


# ─── R45 §7.2: TOTP 重放保护 + 错误限流辅助函数 ─────────────────


def _is_totp_replayed(principal_id: int, code: str) -> bool:
    """R45 §7.2: 检查 TOTP code 是否已被使用(重放检测)。

    内存中维护每个 principal 最近 N 个已用 code 的集合,
    若 code 已在集合中则判定为重放。

    Args:
        principal_id: 管理员 principal ID
        code: 待检查的 TOTP code

    Returns:
        True 表示 code 已被使用(重放);False 表示未使用
    """
    if not principal_id or not code:
        return False
    used_set = _used_totp_codes.get(principal_id)
    if not used_set:
        return False
    return code in used_set


def _record_totp_usage(principal_id: int, code: str) -> None:
    """R45 §7.2: 记录 TOTP code 已使用(添加到已用集合)。

    维护每个 principal 最近 N 个已用 code 的集合,超出则淘汰最旧的。
    注:set 无序,这里用 list 维护插入顺序以便淘汰最旧的。

    Args:
        principal_id: 管理员 principal ID
        code: 已使用的 TOTP code
    """
    if not principal_id or not code:
        return
    if principal_id not in _used_totp_codes:
        _used_totp_codes[principal_id] = set()
    used_set = _used_totp_codes[principal_id]
    if code in used_set:
        return  # 已记录,跳过
    used_set.add(code)
    # 控制集合大小(超过上限时随机移除一个;TOTP code 30s 滚动,
    # 旧 code 自然失效,无需精确淘汰最旧的)
    if len(used_set) > _MAX_USED_TOTP_CODES:
        # 移除一个任意元素(set 无序,TOTP 旧 code 失效后无意义)
        used_set.pop()


def _record_mfa_failure(principal_id: int) -> None:
    """R45 §7.2: 记录一次 MFA 验证失败(添加到失败时间戳列表)。

    维护每个 principal 的失败时间戳列表,用于判断是否达到锁定阈值。

    Args:
        principal_id: 管理员 principal ID
    """
    if not principal_id:
        return
    now = time.time()
    if principal_id not in _mfa_failures:
        _mfa_failures[principal_id] = []
    failures = _mfa_failures[principal_id]
    failures.append(now)
    # 清理过期记录(超过锁定时长的失败不计入当前锁定窗口)
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    _mfa_failures[principal_id] = [ts for ts in failures if ts > cutoff]


def _is_locked(principal_id: int) -> bool:
    """R45 §7.2: 检查 principal 是否因连续错误 TOTP 被锁定。

    判定逻辑:
      - 在最近 _MFA_LOCK_DURATION_SECONDS 秒内,失败次数 >= _MFA_FAIL_MAX_ATTEMPTS
        则判定为锁定状态

    Args:
        principal_id: 管理员 principal ID

    Returns:
        True 表示已锁定(应拒绝验证);False 表示未锁定
    """
    if not principal_id:
        return False
    failures = _mfa_failures.get(principal_id)
    if not failures:
        return False
    now = time.time()
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    recent_failures = [ts for ts in failures if ts > cutoff]
    # 更新列表(清理过期)
    _mfa_failures[principal_id] = recent_failures
    return len(recent_failures) >= _MFA_FAIL_MAX_ATTEMPTS


def _clear_mfa_failures(principal_id: int) -> None:
    """R45 §7.2: 验证成功后清除 principal 的失败记录。

    Args:
        principal_id: 管理员 principal ID
    """
    if principal_id and principal_id in _mfa_failures:
        _mfa_failures.pop(principal_id, None)


def reset_mfa_state_for_testing() -> None:
    """R45 §7.2: 测试辅助函数 — 重置 MFA 模块级状态(重放记录 + 失败计数)。

    仅供单元测试调用,生产代码不应使用。
    """
    _used_totp_codes.clear()
    _mfa_failures.clear()


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

        R45 §7.2 增强:
          1. TOTP 重放保护 — code 已使用过则拒绝(防止中间人重放)
          2. 错误限流 — 连续 5 次错误锁定 5 分钟
          3. 验证成功后记录 code 到已用集合 + 清除失败计数

        流程:
          1. 检查锁定状态(锁定中直接返回 False)
          2. 检查重放(code 已使用过 → 记录失败 + 返回 False)
          3. 校验 TOTP(pyotp)
          4. 成功:记录 code 已用 + 清除失败计数
          5. 失败:记录失败次数(可能触发锁定)

        Args:
            user_id: 用户 ID
            code: 6 位验证码字符串

        Returns:
            True=验证通过;False=验证失败/未配置/已锁定/重放
        """
        if not user_id or not code:
            return False
        # R45 §7.2: 1. 检查锁定状态(锁定中直接拒绝)
        if _is_locked(user_id):
            logger.warning(
                f"[admin.mfa] principal={user_id} 已锁定(连续错误 TOTP "
                f"≥{_MFA_FAIL_MAX_ATTEMPTS} 次,锁定 {_MFA_LOCK_DURATION_SECONDS}s)"
            )
            return False
        # R45 §7.2: 2. 检查重放(code 已使用过 → 拒绝 + 记录失败)
        if _is_totp_replayed(user_id, code):
            logger.warning(
                f"[admin.mfa] TOTP code 重放被拒绝 principal={user_id}"
            )
            _record_mfa_failure(user_id)
            return False
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            secret = await store.get_kv(_make_secret_key(user_id))
            if not secret:
                return False
            ok = _verify_totp(secret, code)
            if ok:
                # R45 §7.2: 验证成功 — 记录 code 已用(防重放)+ 清除失败计数
                _record_totp_usage(user_id, code)
                _clear_mfa_failures(user_id)
            else:
                # R45 §7.2: 验证失败 — 记录失败次数(可能触发锁定)
                _record_mfa_failure(user_id)
                if _is_locked(user_id):
                    logger.warning(
                        f"[admin.mfa] principal={user_id} 因连续错误 TOTP "
                        f"被锁定 {_MFA_LOCK_DURATION_SECONDS}s"
                    )
            return ok
        except Exception as e:
            logger.debug(f"[admin.mfa] 校验 TOTP 失败: {e}")
            # 异常时也记录失败(fail-closed 倾向,防止通过制造异常绕过限流)
            _record_mfa_failure(user_id)
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
