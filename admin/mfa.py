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

R46 P1 整改:
    7. TOTP 重放保护 + 错误限流持久化到 SQLite(跨进程共享)
       - mfa_used_totp (principal_id, timestep, used_at, PRIMARY KEY(principal_id, timestep))
       - mfa_failures (principal_id, failed_at, PRIMARY KEY(principal_id, failed_at))
    8. 内存字典作为 L1 缓存,SQLite 作为权威层
    9. store 不可用或 DB 写入失败时 fail-closed(返回 False)

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

# ─── R45 §7.2 / R46 P1: TOTP 重放保护 + 错误限流 ─────────────────
# R46 P1: 持久化到 SQLite,内存字典仅作 L1 缓存
# 连续错误 TOTP 次数阈值(达到后锁定)
_MFA_FAIL_MAX_ATTEMPTS = 5
# 锁定时长(秒)— 5 分钟
_MFA_LOCK_DURATION_SECONDS = 5 * 60

# R46 P1: 模块级 L1 缓存(进程内,SQLite 为权威层)
# principal_id -> set of used TOTP timesteps (L1 缓存,SQLite 权威)
_used_totp_codes: dict[int, set[int]] = {}
# principal_id -> list of failure timestamps (L1 缓存,SQLite 权威)
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


# ─── R45 §7.2 / R46 P1: TOTP 重放保护 + 错误限流辅助函数 ─────────


def _get_store():
    """R46 P1: 安全获取 cache_store(避免循环导入异常)。"""
    try:
        from database.cache_store import get_cache_store
        return get_cache_store()
    except Exception:
        return None


async def _is_totp_replayed(principal_id: int, code: str) -> bool:
    """R46 P1: 检查 TOTP 是否已被使用(重放检测)— SQLite 权威查询。

    R46 P1 变更:
        - 从 SQLite mfa_used_totp 表查询(跨进程共享)
        - 使用 timestep = int(time.time() // 30) 而非明文 code
        - L1 缓存:先查内存,未命中查 SQLite
        - store 不可用时 fail-closed(返回 True,视为重放,拒绝验证)

    Args:
        principal_id: 管理员 principal ID
        code: 待检查的 TOTP code(保留签名兼容,实际用 timestep 查询)

    Returns:
        True 表示 timestep 已被使用(重放);False 表示未使用
    """
    if not principal_id or not code:
        return False
    timestep = int(time.time() // 30)
    # L1 缓存:先查内存
    used_set = _used_totp_codes.get(principal_id)
    if used_set and timestep in used_set:
        return True
    # L1 未命中,查 SQLite 权威层
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        # store 不可用,fail-closed(视为重放,拒绝验证)
        logger.warning("[admin.mfa] _is_totp_replayed: store 不可用,fail-closed")
        return True
    try:
        cursor = await store._db.execute(
            "SELECT 1 FROM mfa_used_totp WHERE principal_id = ? AND timestep = ? LIMIT 1",
            (principal_id, timestep),
        )
        row = await cursor.fetchone()
        if row:
            # 写入 L1 缓存
            if principal_id not in _used_totp_codes:
                _used_totp_codes[principal_id] = set()
            _used_totp_codes[principal_id].add(timestep)
            return True
        return False
    except Exception as e:
        logger.warning(f"[admin.mfa] _is_totp_replayed 查询 SQLite 失败,fail-closed: {e}")
        return True


def _record_totp_usage(principal_id: int, code: str) -> None:
    """R46 P1: 记录 TOTP timestep 已使用(L1 缓存更新)。

    R46 P1 变更:
        - SQLite 写入在 verify_totp_code 中完成(防重放)
        - 本函数仅更新 L1 缓存
        - 使用 timestep = int(time.time() // 30)

    Args:
        principal_id: 管理员 principal ID
        code: 已使用的 TOTP code(保留签名兼容,实际用 timestep)
    """
    if not principal_id or not code:
        return
    timestep = int(time.time() // 30)
    if principal_id not in _used_totp_codes:
        _used_totp_codes[principal_id] = set()
    _used_totp_codes[principal_id].add(timestep)


async def _record_mfa_failure(principal_id: int) -> bool:
    """R46 P1: 记录一次 MFA 验证失败(写入 SQLite + L1 缓存)。

    R46 P1 变更:
        - 写入 SQLite mfa_failures 表(跨进程共享)
        - 同时更新 L1 缓存
        - DB 写入失败时返回 False(fail-closed 信号)

    Args:
        principal_id: 管理员 principal ID

    Returns:
        True=写入成功;False=写入失败(调用方应 fail-closed)
    """
    if not principal_id:
        return False
    now = time.time()
    # 更新 L1 缓存
    if principal_id not in _mfa_failures:
        _mfa_failures[principal_id] = []
    _mfa_failures[principal_id].append(now)
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    _mfa_failures[principal_id] = [ts for ts in _mfa_failures[principal_id] if ts > cutoff]
    # 写入 SQLite 权威层
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        logger.warning("[admin.mfa] _record_mfa_failure: store 不可用,fail-closed")
        return False
    try:
        await store._db.execute(
            "INSERT OR IGNORE INTO mfa_failures (principal_id, failed_at) VALUES (?, ?)",
            (principal_id, now),
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(f"[admin.mfa] _record_mfa_failure 写入 SQLite 失败: {e}")
        return False


async def _is_locked(principal_id: int) -> bool:
    """R46 P1: 检查 principal 是否因连续错误 TOTP 被锁定 — SQLite 权威查询。

    R46 P1 变更:
        - 从 SQLite mfa_failures 表查询最近 5 分钟失败次数
        - L1 缓存:先查内存,未命中或未达阈值时查 SQLite
        - store 不可用时 fail-closed(返回 True,视为已锁定)

    Args:
        principal_id: 管理员 principal ID

    Returns:
        True 表示已锁定(应拒绝验证);False 表示未锁定
    """
    if not principal_id:
        return False
    now = time.time()
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    # L1 缓存:先查内存
    failures = _mfa_failures.get(principal_id)
    if failures:
        recent = [ts for ts in failures if ts > cutoff]
        _mfa_failures[principal_id] = recent
        if len(recent) >= _MFA_FAIL_MAX_ATTEMPTS:
            return True
    # L1 未命中或未达阈值,查 SQLite 权威层
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        logger.warning("[admin.mfa] _is_locked: store 不可用,fail-closed")
        return True
    try:
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_failures WHERE principal_id = ? AND failed_at > ?",
            (principal_id, cutoff),
        )
        row = await cursor.fetchone()
        count = int(row[0]) if row else 0
        return count >= _MFA_FAIL_MAX_ATTEMPTS
    except Exception as e:
        logger.warning(f"[admin.mfa] _is_locked 查询 SQLite 失败,fail-closed: {e}")
        return True


async def _clear_mfa_failures(principal_id: int) -> bool:
    """R46 P1: 验证成功后清除 principal 的失败记录(SQLite + L1 缓存)。

    R46 P1 变更:
        - 从 SQLite mfa_failures 表删除
        - 同时清除 L1 缓存
        - DB 删除失败时返回 False(fail-closed 信号)

    Args:
        principal_id: 管理员 principal ID

    Returns:
        True=清除成功;False=清除失败(调用方应 fail-closed)
    """
    if not principal_id:
        return False
    # 清除 L1 缓存
    _mfa_failures.pop(principal_id, None)
    # 清除 SQLite 权威层
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        logger.warning("[admin.mfa] _clear_mfa_failures: store 不可用,fail-closed")
        return False
    try:
        await store._db.execute(
            "DELETE FROM mfa_failures WHERE principal_id = ?",
            (principal_id,),
        )
        await store._db.commit()
        return True
    except Exception as e:
        logger.warning(f"[admin.mfa] _clear_mfa_failures 删除 SQLite 失败: {e}")
        return False


def reset_mfa_state_for_testing() -> None:
    """R45 §7.2: 测试辅助函数 — 重置 MFA 模块级 L1 缓存状态。

    仅供单元测试调用,生产代码不应使用。
    R46 P1: 仅清除 L1 缓存,SQLite 权威层需测试自行清理。
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

        R46 P1 整改:
          4. 重放保护 + 失败计数持久化到 SQLite(跨进程共享)
          5. store 不可用或 DB 写入失败时 fail-closed(返回 False)

        流程:
          1. 检查锁定状态(锁定中直接返回 False)
          2. 检查重放(timestep 已使用过 → 记录失败 + 返回 False)
          3. 校验 TOTP(pyotp)
          4. 成功:写入 mfa_used_totp(防重放)+ 清除失败计数
          5. 失败:记录失败次数到 mfa_failures(可能触发锁定)

        Args:
            user_id: 用户 ID
            code: 6 位验证码字符串

        Returns:
            True=验证通过;False=验证失败/未配置/已锁定/重放
        """
        if not user_id or not code:
            return False
        # R45 §7.2: 1. 检查锁定状态(锁定中直接拒绝)
        if await _is_locked(user_id):
            logger.warning(
                f"[admin.mfa] principal={user_id} 已锁定(连续错误 TOTP "
                f"≥{_MFA_FAIL_MAX_ATTEMPTS} 次,锁定 {_MFA_LOCK_DURATION_SECONDS}s)"
            )
            return False
        # R45 §7.2: 2. 检查重放(timestep 已使用过 → 拒绝 + 记录失败)
        if await _is_totp_replayed(user_id, code):
            logger.warning(
                f"[admin.mfa] TOTP code 重放被拒绝 principal={user_id}"
            )
            await _record_mfa_failure(user_id)
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
                # R46 P1: 验证成功 — 写入 mfa_used_totp(防重放,SQLite 权威)
                timestep = int(time.time() // 30)
                try:
                    await store._db.execute(
                        "INSERT OR IGNORE INTO mfa_used_totp "
                        "(principal_id, timestep, used_at) VALUES (?, ?, ?)",
                        (user_id, timestep, time.time()),
                    )
                    await store._db.commit()
                except Exception as e:
                    # R46 P1: DB 写入失败 → fail-closed(防重放记录丢失,拒绝验证)
                    logger.warning(
                        f"[admin.mfa] 写入 mfa_used_totp 失败,fail-closed: {e}"
                    )
                    return False
                # R45 §7.2: 记录 code 已用(L1 缓存)+ 清除失败计数
                _record_totp_usage(user_id, code)
                if not await _clear_mfa_failures(user_id):
                    # R46 P1: 清除失败 → fail-closed(失败计数残留可能导致误锁定)
                    logger.warning(
                        f"[admin.mfa] 清除 mfa_failures 失败,fail-closed"
                    )
                    return False
            else:
                # R45 §7.2: 验证失败 — 记录失败次数(可能触发锁定)
                if not await _record_mfa_failure(user_id):
                    # R46 P1: DB 写入失败 → fail-closed
                    logger.warning(
                        f"[admin.mfa] 记录 mfa_failure 失败,fail-closed"
                    )
                    return False
                if await _is_locked(user_id):
                    logger.warning(
                        f"[admin.mfa] principal={user_id} 因连续错误 TOTP "
                        f"被锁定 {_MFA_LOCK_DURATION_SECONDS}s"
                    )
            return ok
        except Exception as e:
            logger.debug(f"[admin.mfa] 校验 TOTP 失败: {e}")
            # 异常时也记录失败(fail-closed 倾向,防止通过制造异常绕过限流)
            try:
                await _record_mfa_failure(user_id)
            except Exception as rec_err:
                logger.debug(f"[admin.mfa] 异常路径记录失败异常(忽略): {rec_err}")
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
