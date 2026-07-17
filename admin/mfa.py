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

R47 P1-b 整改:
    10. mfa_failures 表 schema 变更 — 旧 (principal_id, failed_at REAL, PRIMARY KEY(principal_id, failed_at))
        存在同秒/同毫秒碰撞导致 INSERT OR IGNORE 丢失记录。
        新 schema: id INTEGER AUTOINCREMENT 主键 + failed_at_ms INTEGER 毫秒时间戳,
        彻底消除碰撞。旧表保留为 mfa_failures_old 备份,数据迁移到新表。
    11. TOTP timestep 原子消费 — 使用 INSERT OR IGNORE + rowcount 判定重放,
        消除"先查询再插入"的竞态窗口(原 _is_totp_replayed 只查询不消费)。
    12. valid_window=1 记录实际匹配 timestep — 遍历 [current-1, current, current+1]
        对每个 timestep 精确 verify(valid_window=0),记录实际匹配的 timestep,
        防止同一 code 在 prev/next timestep 可重用。
    13. _record_mfa_failure 不阻塞 — DB 写入失败仅 warning,不 fail-closed
        (避免因记录失败而锁定用户)。
    14. cleanup_expired_mfa_records — retention 24h 清理 mfa_used_totp / mfa_failures。

设计原则:
    - 真实实现使用 pyotp.TOTP 验证 6 位 TOTP 代码
    - fail-closed:pyotp 未安装或异常时返回 False(拒绝验证)
    - 密钥存储到 SQLite kv_store,key 前缀 admin:mfa:secret:<user_id>
    - 允许 ±30s 时间漂移(valid_window=1)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Any, Optional

from loguru import logger

from services.error_codes import AppError, ErrorCodes

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


def _find_matching_timestep(secret: str, code: str) -> Optional[int]:
    """R47 P1-b: 查找 code 实际匹配的 timestep(遍历 [current-1, current, current+1])。

    pyotp.TOTP(secret).verify(code, valid_window=1) 可能匹配 current/prev/next
    三个 timestep 中的任意一个,但无法告知具体匹配了哪个。若总用当前 timestep 记录,
    同一 code 在 prev/next timestep 仍可重用。

    本函数对每个 timestep 单独 verify(valid_window=0),返回实际匹配的 timestep,
    供 _consume_totp_timestep 做原子消费。

    Args:
        secret: TOTP 密钥(base32 编码的 32 字符)
        code: 用户输入的 6 位验证码

    Returns:
        匹配的 timestep(int);无匹配返回 None
    """
    if not secret or not code:
        return None
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        # 遍历 [current-1, current, current+1],对每个 timestep 精确匹配
        for offset in (-1, 0, 1):
            timestep = current_timestep + offset
            # for_time 接受 Unix 时间戳;timestep * 30 是该 timestep 的起始时间戳
            if totp.verify(code, for_time=timestep * 30, valid_window=0):
                return timestep
        return None
    except ImportError:
        logger.error("[admin.mfa] pyotp 未安装,_find_matching_timestep 失败(fail-closed)")
        return None
    except Exception as e:
        logger.warning(f"[admin.mfa] _find_matching_timestep 异常: {e}")
        return None


# ─── R45 §7.2 / R46 P1: TOTP 重放保护 + 错误限流辅助函数 ─────────


def _get_store():
    """R46 P1: 安全获取 cache_store(避免循环导入异常)。"""
    try:
        from database.cache_store import get_cache_store
        return get_cache_store()
    except Exception:
        return None


async def _is_totp_replayed(principal_id: int, code: str) -> bool:
    """R46 P1: 检查 TOTP 是否已被使用(重放检测)— 只读查询。

    R46 P1 变更:
        - 从 SQLite mfa_used_totp 表查询(跨进程共享)
        - 使用 timestep = int(time.time() // 30) 而非明文 code
        - L1 缓存:先查内存,未命中查 SQLite
        - store 不可用时 fail-closed(返回 True,视为重放,拒绝验证)

    R47 P1-b 变更:
        - 此函数仅作只读查询(向后兼容 + 测试用)。
        - verify_totp_code 不再调用此函数,改用 _consume_totp_timestep
          (INSERT OR IGNORE + rowcount)做原子消费,消除竞态窗口。

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


async def _consume_totp_timestep(principal_id: int, timestep: int) -> bool:
    """R47 P1-b: 原子消费 TOTP timestep(INSERT OR IGNORE + rowcount 判定)。

    使用 UNIQUE(principal_id, timestep) 约束作为原子消费原语,消除
    "先查询再插入"的竞态窗口:
        - rowcount=1 → 首次使用,返回 True(消费成功)
        - rowcount=0 → UNIQUE 冲突,已被消费(重放),返回 False
        - store 不可用或异常 → fail-closed(返回 False,拒绝验证)

    同时更新 L1 缓存(_used_totp_codes),保持与 _is_totp_replayed 一致。

    Args:
        principal_id: 管理员 principal ID
        timestep: 实际匹配的 timestep(由 _find_matching_timestep 返回)

    Returns:
        True=首次使用(消费成功);False=重放或 fail-closed
    """
    if not principal_id or timestep is None:
        return False
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        # store 不可用,fail-closed(视为已消费,拒绝验证)
        logger.warning("[admin.mfa] _consume_totp_timestep: store 不可用,fail-closed")
        return False
    try:
        cursor = await store._db.execute(
            "INSERT OR IGNORE INTO mfa_used_totp "
            "(principal_id, timestep, used_at) VALUES (?, ?, ?)",
            (principal_id, timestep, time.time()),
        )
        await store._db.commit()
        # rowcount=1 → 插入成功(首次使用);rowcount=0 → UNIQUE 冲突(重放)
        rowcount = cursor.rowcount if cursor is not None else 0
        # 更新 L1 缓存(无论首次还是重放,该 timestep 都已标记为已用)
        if principal_id not in _used_totp_codes:
            _used_totp_codes[principal_id] = set()
        _used_totp_codes[principal_id].add(timestep)
        if rowcount >= 1:
            return True
        # rowcount=0 → 重放(已被消费)
        return False
    except Exception as e:
        logger.warning(f"[admin.mfa] _consume_totp_timestep 原子消费失败,fail-closed: {e}")
        return False


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
    """R46 P1 / R47 P1-b: 记录一次 MFA 验证失败(写入 SQLite + L1 缓存)。

    R46 P1 变更:
        - 写入 SQLite mfa_failures 表(跨进程共享)
        - 同时更新 L1 缓存

    R47 P1-b 变更:
        - mfa_failures 表 schema 改为 (id AUTOINCREMENT, principal_id, failed_at_ms INTEGER, created_at)
        - 时间戳用整数毫秒(failed_at_ms)替代浮点秒(failed_at),消除同毫秒碰撞
        - 不阻塞:DB 写入失败仅记录 warning,不返回 False(避免因记录失败而锁定用户)

    Args:
        principal_id: 管理员 principal ID

    Returns:
        总是返回 True(不阻塞调用方);store 不可用时仅 warning
    """
    if not principal_id:
        return True
    now = time.time()
    now_ms = int(now * 1000)
    # 更新 L1 缓存
    if principal_id not in _mfa_failures:
        _mfa_failures[principal_id] = []
    _mfa_failures[principal_id].append(now)
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    _mfa_failures[principal_id] = [ts for ts in _mfa_failures[principal_id] if ts > cutoff]
    # 写入 SQLite 权威层(R47 P1-b: 新 schema 使用 failed_at_ms 毫秒整数)
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        # R47 P1-b: store 不可用时不阻塞(仅 warning,不 fail-closed)
        logger.warning("[admin.mfa] _record_mfa_failure: store 不可用(忽略,不阻塞)")
        return True
    try:
        import datetime as _dt
        await store._db.execute(
            "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
            "VALUES (?, ?, ?)",
            (principal_id, now_ms, _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        return True
    except Exception as e:
        # R47 P1-b: DB 写入失败不阻塞(仅 warning,避免因记录失败而锁定用户)
        logger.warning(f"[admin.mfa] _record_mfa_failure 写入 SQLite 失败(忽略,不阻塞): {e}")
        return True


async def _is_locked(principal_id: int) -> bool:
    """R46 P1 / R47 P1-b: 检查 principal 是否因连续错误 TOTP 被锁定 — SQLite 权威查询。

    R46 P1 变更:
        - 从 SQLite mfa_failures 表查询最近 5 分钟失败次数
        - L1 缓存:先查内存,未命中或未达阈值时查 SQLite
        - store 不可用时 fail-closed(返回 True,视为已锁定)

    R47 P1-b 变更:
        - 查询条件改为 failed_at_ms > cutoff_ms(毫秒整数)

    Args:
        principal_id: 管理员 principal ID

    Returns:
        True 表示已锁定(应拒绝验证);False 表示未锁定
    """
    if not principal_id:
        return False
    now = time.time()
    cutoff = now - _MFA_LOCK_DURATION_SECONDS
    cutoff_ms = int(cutoff * 1000)
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
            "SELECT COUNT(*) FROM mfa_failures WHERE principal_id = ? AND failed_at_ms > ?",
            (principal_id, cutoff_ms),
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


async def cleanup_expired_mfa_records(retention_hours: int = 24) -> dict:
    """R47 P1-b: 清理过期的 MFA 记录(retention 默认 24 小时)。

    删除:
        - mfa_used_totp 中 used_at < (now - retention_hours * 3600) 的记录
        - mfa_failures 中 failed_at_ms < (now_ms - retention_hours * 3600 * 1000) 的记录

    由 retention worker 定期调用(r40_scheduler 每天执行),防止表无限增长。

    Args:
        retention_hours: 保留时长(小时),默认 24

    Returns:
        {"deleted_used_totp": int, "deleted_failures": int}
    """
    result = {"deleted_used_totp": 0, "deleted_failures": 0}
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        logger.warning("[admin.mfa] cleanup_expired_mfa_records: store 不可用,跳过清理")
        return result
    try:
        now = time.time()
        now_ms = int(now * 1000)
        cutoff_used_at = now - retention_hours * 3600
        cutoff_failed_ms = now_ms - retention_hours * 3600 * 1000
        # 清理 mfa_used_totp(used_at 是浮点秒)
        cursor = await store._db.execute(
            "DELETE FROM mfa_used_totp WHERE used_at < ?",
            (cutoff_used_at,),
        )
        result["deleted_used_totp"] = cursor.rowcount if cursor is not None else 0
        # 清理 mfa_failures(failed_at_ms 是整数毫秒)
        cursor = await store._db.execute(
            "DELETE FROM mfa_failures WHERE failed_at_ms < ?",
            (cutoff_failed_ms,),
        )
        result["deleted_failures"] = cursor.rowcount if cursor is not None else 0
        await store._db.commit()
        if result["deleted_used_totp"] > 0 or result["deleted_failures"] > 0:
            logger.info(
                f"[R47] MFA 记录清理: used_totp={result['deleted_used_totp']}, "
                f"failures={result['deleted_failures']}"
            )
        return result
    except Exception as e:
        logger.warning(f"[admin.mfa] cleanup_expired_mfa_records 清理失败: {e}")
        return result


def reset_mfa_state_for_testing() -> None:
    """R45 §7.2: 测试辅助函数 — 重置 MFA 模块级 L1 缓存状态。

    仅供单元测试调用,生产代码不应使用。
    R46 P1: 仅清除 L1 缓存,SQLite 权威层需测试自行清理。
    """
    _used_totp_codes.clear()
    _mfa_failures.clear()


# ─── R59 P0-03: MFA receipt 签发与验证(服务端签名短期 token) ─────────
# R59 P0-03 要求:
#   - receipt 为服务端签名的短期 token(PASETO-like, HMAC-SHA256, 无外部依赖)
#   - payload 字段: jti/sub/purpose/action_hash/amr/iat/exp
#   - DB 记录字段: jti/sub/purpose/action_hash/amr/iat/exp/used_at/consumed_at
#   - purpose 必须匹配高风险动作; sub 必须匹配批准人;
#     action_hash 必须匹配 request_hash(防篡改绑定具体请求)
#   - TTL 2-5 分钟(默认 300s)
#   - 以 jti 原子消费(INSERT OR IGNORE + rowcount, 参考 _consume_totp_timestep)
#   - 签名密钥从 MFA_RECEIPT_SIGNING_KEY 读取, 缺失 fail-closed
#   - 禁止仅靠前端 TOTP 文本或伪造时间戳
#
# 设计说明:
#   - token 格式 v4.public.<payload_b64>.<signature_b64>(PASETO-like; 签名使用
#     HMAC-SHA256 对称密钥, 避免引入 pyjwt/paseto 等外部库依赖)。注意:尽管前缀
#     沿用 PASETO v4.public 格式, 此处为对称签名(HMAC), 非非对称(Ed25519)。
#   - 签发(issue)不写 DB: token 自包含且服务端签名, 签名即发行凭证。
#   - 验证(verify)为纯密码学 + 字段校验, 不写 DB, 不消费(可多次调用查看)。
#   - 消费(consume)用 INSERT OR IGNORE + rowcount 原子记录一次性使用, 与
#     _consume_totp_timestep 同一模式(jti PRIMARY KEY 保证唯一消费)。
#   - 与现有 TOTP 机制叠加: receipt 层在 _record_totp_usage/_consume_totp_timestep
#     之上, 不修改既有 TOTP 函数签名与行为。

# 签名密钥环境变量名(缺失时 fail-closed)
_MFA_RECEIPT_KEY_ENV = "MFA_RECEIPT_SIGNING_KEY"
# token 版本前缀(PASETO-like)
_MFA_RECEIPT_TOKEN_PREFIX = "v4.public"
# 默认有效期 5 分钟(R59 建议 2-5 分钟)
_MFA_RECEIPT_DEFAULT_TTL = 300


def _get_mfa_receipt_signing_key() -> bytes:
    """R59 P0-03: 从环境变量读取 receipt 签名密钥(fail-closed)。

    密钥缺失或为空时抛 AppError(AUTH.MFA.RECEIPT_INVALID,
    reason=signing_key_missing), 拒绝签发/验证(fail-closed, 防止无密钥时
    绕过签名校验)。

    Returns:
        签名密钥(bytes, UTF-8 编码)

    Raises:
        AppError: 环境变量 MFA_RECEIPT_SIGNING_KEY 未设置或为空
    """
    raw = os.environ.get(_MFA_RECEIPT_KEY_ENV, "").strip()
    if not raw:
        logger.error(
            f"[admin.mfa] 环境变量 {_MFA_RECEIPT_KEY_ENV} 未设置, "
            f"receipt 签发/验证 fail-closed"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={"reason": "signing_key_missing"},
        )
    return raw.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    """base64url 编码(去除填充, 符合 PASETO 规范)。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """base64url 解码(自动补齐缺失的填充)。"""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign_receipt_payload(payload_b64: str, key: bytes) -> str:
    """对 payload_b64 计算 HMAC-SHA256 签名, 返回 base64url 编码的签名。

    签名对象为 token 中的 payload_b64 字符串(ASCII 字节), 而非原始 JSON。
    这样验证时直接对收到的 payload_b64 重算签名并常量时间比较, 无需关心
    JSON 序列化是否一致。

    Args:
        payload_b64: base64url 编码的 payload 字符串
        key: 签名密钥(bytes)

    Returns:
        base64url 编码(无填充)的 HMAC-SHA256 签名字符串
    """
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(sig)


def issue_mfa_receipt(
    principal_id: int,
    purpose: str,
    action_hash: str,
    amr: list[str],
    ttl_seconds: int = _MFA_RECEIPT_DEFAULT_TTL,
) -> str:
    """R59 P0-03: 签发 MFA receipt(服务端签名短期 token)。

    生成一个 PASETO-like 的短期 receipt token, payload 含 jti/sub/purpose/
    action_hash/amr/iat/exp 字段, 使用 HMAC-SHA256 签名。receipt 用于证明
    批准人(principal_id)已通过 MFA 验证, 授权执行特定高风险动作
    (purpose + action_hash)。

    签发不写 DB: token 自包含且服务端签名, 签名即发行凭证; 一次性消费由
    consume_mfa_receipt 以 jti 原子记录(INSERT OR IGNORE + rowcount)。

    Args:
        principal_id: 批准人 principal ID(写入 sub, 必须非空)
        purpose: 高风险动作用途(如 "approval.execute"/"backup.restore", 必须非空)
        action_hash: 请求摘要 request_hash(防篡改绑定具体请求, 必须非空)
        amr: 认证方式参考列表(如 ["totp"]/["totp", "passcode"])
        ttl_seconds: 有效期(秒), 默认 300(5 分钟), R59 建议 2-5 分钟

    Returns:
        token 字符串, 格式: v4.public.<payload_b64>.<signature_b64>

    Raises:
        AppError: 签名密钥未配置(reason=signing_key_missing)或参数无效
                  (reason=invalid_issue_params), 错误码 AUTH.MFA.RECEIPT_INVALID
    """
    if not principal_id or not purpose or not action_hash:
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": principal_id,
                "reason": "invalid_issue_params",
            },
        )
    # fail-closed: 密钥缺失抛 AppError
    key = _get_mfa_receipt_signing_key()
    now = int(time.time())
    payload = {
        "jti": uuid.uuid4().hex,
        "sub": int(principal_id),
        "purpose": purpose,
        "action_hash": action_hash,
        "amr": list(amr) if amr else [],
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    # 紧凑 + 排序序列化, 保证 payload 字节确定性(签名实际覆盖 payload_b64)
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    signature = _sign_receipt_payload(payload_b64, key)
    logger.info(
        f"[admin.mfa] 签发 MFA receipt jti={payload['jti']} "
        f"sub={principal_id} purpose={purpose} ttl={ttl_seconds}s"
    )
    return f"{_MFA_RECEIPT_TOKEN_PREFIX}.{payload_b64}.{signature}"


def verify_mfa_receipt(
    token: str,
    expected_principal_id: int,
    expected_purpose: str,
    expected_action_hash: str,
) -> dict[str, Any]:
    """R59 P0-03: 验证 MFA receipt(签名 + 字段 + 有效期)。

    校验流程:
      1. 解析 token 格式(v4.public.<payload_b64>.<signature_b64>)
      2. 重算 HMAC-SHA256 签名, 常量时间比较(防伪造/时序攻击)
      3. 解码 payload, 校验 exp 未过期(防伪造时间戳)
      4. 校验 sub == expected_principal_id(批准人匹配)
      5. 校验 purpose == expected_purpose(动作用途匹配)
      6. 校验 action_hash == expected_action_hash(请求摘要匹配)

    本函数为纯密码学 + 字段校验, 不写 DB, 不消费 receipt(可多次调用查看)。
    一次性消费由 consume_mfa_receipt(jti) 原子完成。调用方典型流程:
    verify(token) → 取 payload["jti"] → 在动作事务中 consume(jti)。

    Args:
        token: receipt token 字符串
        expected_principal_id: 期望的批准人 principal ID
        expected_purpose: 期望的动作用途
        expected_action_hash: 期望的请求摘要(request_hash)

    Returns:
        解码后的 payload dict(含 jti/sub/purpose/action_hash/amr/iat/exp)

    Raises:
        AppError: 任何校验失败(格式/签名/过期/sub/purpose/action_hash 不匹配,
                  或签名密钥未配置), 错误码 AUTH.MFA.RECEIPT_INVALID,
                  params.reason 标识具体失败原因
    """
    if not token or not isinstance(token, str):
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "empty_token",
            },
        )
    # 1. 解析 token 格式: v4.public.<payload_b64>.<signature_b64>
    #    前缀 "v4.public" 自身含 ".", 因此先校验前缀再分割剩余部分
    #    (payload_b64 / signature_b64 为 base64url 无填充, 不含 ".")
    expected_prefix = _MFA_RECEIPT_TOKEN_PREFIX + "."
    if not token.startswith(expected_prefix):
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "malformed_token",
            },
        )
    rest = token[len(expected_prefix):]
    rest_parts = rest.split(".")
    if len(rest_parts) != 2 or not rest_parts[0] or not rest_parts[1]:
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "malformed_token",
            },
        )
    payload_b64, signature = rest_parts
    # 2. 签名验证(fail-closed: 密钥缺失由 _get_mfa_receipt_signing_key 抛 AppError)
    key = _get_mfa_receipt_signing_key()
    expected_sig = _sign_receipt_payload(payload_b64, key)
    if not hmac.compare_digest(expected_sig, signature):
        logger.warning(
            f"[admin.mfa] MFA receipt 签名不匹配 "
            f"expected_principal={expected_principal_id}"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "signature_mismatch",
            },
        )
    # 3. 解码 payload
    try:
        payload_json = _b64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception as e:
        logger.warning(f"[admin.mfa] MFA receipt payload 解码失败: {e}")
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "payload_decode_failed",
            },
        )
    # 4. 有效期校验(防伪造时间戳: 以服务端当前时间为准)
    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, int) or now >= exp:
        logger.warning(
            f"[admin.mfa] MFA receipt 已过期 jti={payload.get('jti')} "
            f"exp={exp} now={now}"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "expired",
            },
        )
    # 5. sub 校验(批准人匹配)
    sub = payload.get("sub")
    if sub != int(expected_principal_id):
        logger.warning(
            f"[admin.mfa] MFA receipt sub 不匹配 "
            f"expected={expected_principal_id} actual={sub}"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "sub_mismatch",
            },
        )
    # 6. purpose 校验(动作用途匹配)
    if payload.get("purpose") != expected_purpose:
        logger.warning(
            f"[admin.mfa] MFA receipt purpose 不匹配 "
            f"expected={expected_purpose} actual={payload.get('purpose')}"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "purpose_mismatch",
            },
        )
    # 7. action_hash 校验(请求摘要匹配)
    if payload.get("action_hash") != expected_action_hash:
        logger.warning(
            f"[admin.mfa] MFA receipt action_hash 不匹配 "
            f"expected_principal={expected_principal_id}"
        )
        raise AppError(
            ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
            params={
                "user_id": expected_principal_id,
                "reason": "action_hash_mismatch",
            },
        )
    return payload


async def consume_mfa_receipt(jti: str) -> bool:
    """R59 P0-03: 原子消费 MFA receipt(一次性使用, INSERT OR IGNORE + rowcount)。

    参考 _consume_totp_timestep 模式: 使用 jti PRIMARY KEY 作为原子消费原语,
    消除"先查询再插入"的竞态窗口:
        - rowcount=1 → 首次消费, 返回 True(消费成功)
        - rowcount=0 → jti 已存在(重放/已消费), 返回 False
        - store 不可用或异常 → fail-closed(返回 False, 拒绝执行高风险动作)

    记录 used_at/consumed_at 时间戳供审计追溯。其余元数据(sub/purpose/
    action_hash/amr/iat/exp)存于签名 token 中, 由调用方在审计时通过 jti 关联。

    R59 P0-03 "在同一事务中以 jti 原子消费": 调用方应将本函数与高风险动作放在
    同一 DB 事务中执行(同一连接), 以保证"消费成功 ⟺ 动作执行"的原子性。本函数
    默认 commit(与 _consume_totp_timestep 一致); 如需跨表事务原子性, 调用方
    可在同一连接上自行管理事务边界(先 consume 再执行动作, 一并 commit/rollback)。

    Args:
        jti: receipt 唯一 ID(来自 verify_mfa_receipt 返回的 payload["jti"])

    Returns:
        True=首次消费成功; False=已消费(重放)/store 不可用/异常(fail-closed)
    """
    if not jti:
        return False
    store = _get_store()
    if not store or not getattr(store, "_db", None):
        # store 不可用, fail-closed(视为消费失败, 拒绝执行)
        logger.warning("[admin.mfa] consume_mfa_receipt: store 不可用, fail-closed")
        return False
    now = int(time.time())
    try:
        # 幂等确保表存在(与 cache_store.init 建表语句一致, IF NOT EXISTS 安全;
        # 防止 init 未运行时 consume 失败, 便于隔离测试)
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS mfa_receipts (
                jti          TEXT PRIMARY KEY,
                sub          BIGINT,
                purpose      TEXT,
                action_hash  TEXT,
                amr          TEXT,
                iat          INTEGER,
                exp          INTEGER,
                used_at      INTEGER,
                consumed_at  INTEGER
            )"""
        )
        # 原子消费: jti PRIMARY KEY 唯一约束 + INSERT OR IGNORE
        cursor = await store._db.execute(
            "INSERT OR IGNORE INTO mfa_receipts (jti, used_at, consumed_at) "
            "VALUES (?, ?, ?)",
            (jti, now, now),
        )
        await store._db.commit()
        # rowcount=1 → 插入成功(首次消费); rowcount=0 → UNIQUE 冲突(重放)
        rowcount = cursor.rowcount if cursor is not None else 0
        if rowcount >= 1:
            logger.info(f"[admin.mfa] MFA receipt 消费成功 jti={jti}")
            return True
        logger.warning(f"[admin.mfa] MFA receipt 重放被拒绝(已消费) jti={jti}")
        return False
    except Exception as e:
        logger.warning(
            f"[admin.mfa] consume_mfa_receipt 原子消费失败, fail-closed: {e}"
        )
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

        R45 §7.2 增强:
          1. TOTP 重放保护 — code 已使用过则拒绝(防止中间人重放)
          2. 错误限流 — 连续 5 次错误锁定 5 分钟
          3. 验证成功后记录 code 到已用集合 + 清除失败计数

        R46 P1 整改:
          4. 重放保护 + 失败计数持久化到 SQLite(跨进程共享)
          5. store 不可用或 DB 写入失败时 fail-closed(返回 False)

        R47 P1-b 整改:
          6. 原子消费 timestep — 使用 INSERT OR IGNORE + rowcount 判定重放,
             消除"先查询再插入"竞态(原 _is_totp_replayed 只查询不消费)。
          7. valid_window=1 记录实际匹配 timestep — 遍历 [current-1, current, current+1]
             对每个 timestep 精确 verify,记录实际匹配的 timestep。
          8. _record_mfa_failure 不阻塞 — DB 写入失败仅 warning。

        流程:
          1. 检查锁定状态(锁定中直接返回 False)
          2. 获取 secret
          3. 查找实际匹配的 timestep(_find_matching_timestep)
          4. 原子消费 timestep(_consume_totp_timestep:INSERT OR IGNORE + rowcount)
             - rowcount=1 → 首次使用,清除失败计数,返回 True
             - rowcount=0 → 重放,记录失败,返回 False
          5. 无匹配 timestep → 记录失败,返回 False

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
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return False
            secret = await store.get_kv(_make_secret_key(user_id))
            if not secret:
                return False
            # R47 P1-b: 2. 查找实际匹配的 timestep(遍历 [current-1, current, current+1])
            matched_timestep = _find_matching_timestep(secret, code)
            if matched_timestep is None:
                # 无匹配 → 验证失败,记录失败次数(不阻塞)
                await _record_mfa_failure(user_id)
                if await _is_locked(user_id):
                    logger.warning(
                        f"[admin.mfa] principal={user_id} 因连续错误 TOTP "
                        f"被锁定 {_MFA_LOCK_DURATION_SECONDS}s"
                    )
                return False
            # R47 P1-b: 3. 原子消费 timestep(INSERT OR IGNORE + rowcount 判定重放)
            consumed = await _consume_totp_timestep(user_id, matched_timestep)
            if not consumed:
                # rowcount=0 → 重放(已被消费),记录失败(不阻塞)
                logger.warning(
                    f"[admin.mfa] TOTP timestep={matched_timestep} 重放被拒绝 "
                    f"principal={user_id}"
                )
                await _record_mfa_failure(user_id)
                if await _is_locked(user_id):
                    logger.warning(
                        f"[admin.mfa] principal={user_id} 因连续错误 TOTP "
                        f"被锁定 {_MFA_LOCK_DURATION_SECONDS}s"
                    )
                return False
            # R47 P1-b: 4. 首次使用 → 清除失败计数
            # (L1 缓存已由 _consume_totp_timestep 更新,无需调用 _record_totp_usage)
            if not await _clear_mfa_failures(user_id):
                # R46 P1: 清除失败 → fail-closed(失败计数残留可能导致误锁定)
                logger.warning(
                    f"[admin.mfa] 清除 mfa_failures 失败,fail-closed"
                )
                return False
            return True
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
            raise


# 模块级单例
_mfa_manager: Optional[MFAManager] = None


def get_mfa_manager() -> MFAManager:
    """获取 MFAManager 单例。"""
    global _mfa_manager
    if _mfa_manager is None:
        _mfa_manager = MFAManager()
    return _mfa_manager
