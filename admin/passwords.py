"""R52 P0-1 / R53 P0-3: 纯密码哈希/验证模块(无 Settings 依赖)。

将 PBKDF2 hash/verify 函数从 admin/__init__.py 拆出,避免 E2E 验证时
import 整个 admin 模块触发 Settings 校验副作用。

R53 P0-3 整改:
- Argon2 不再受支持(生成与校验统一用 PBKDF2)
- 新增 ``is_argon2_hash`` 辅助函数用于识别旧 Argon2 hash(迁移诊断)
- PBKDF2 校验增加最大 iterations 限制(防止错误配置造成 CPU/内存消耗)
- 精确校验 salt_hex 长度(32)和 hash_hex 长度(64)

格式约定(统一 PBKDF2,不再支持 Argon2):
    $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>

按 ``$`` 拆分得到 5 段:
    ['', 'pbkdf2-sha256', '<iterations>', '<salt_hex>', '<hash_hex>']
其中 salt 为 16 字节(32 hex 字符),hash 为 32 字节 SHA256(64 hex 字符)。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


PBKDF2_ALGORITHM = "pbkdf2-sha256"
PBKDF2_ITERATIONS = 200000
PBKDF2_SALT_BYTES = 16
PBKDF2_HASH_BYTES = 32  # SHA256 = 32 bytes = 64 hex chars
# R39 P1-12: 防御性下限,拒绝过低的迭代次数(与原 admin._verify_password 保持一致)
_PBKDF2_MIN_ITERATIONS = 10_000
# R53 P0-3: 防御性上限,拒绝过高的迭代次数(防止错误配置造成 CPU/内存消耗)
_PBKDF2_MAX_ITERATIONS = 1_000_000

# R53 P0-3: 旧 Argon2 hash 前缀(仅用于迁移诊断,不再用于生成/校验)
_ARGON2_PREFIX = "$argon2"


def is_argon2_hash(stored_hash: str) -> bool:
    """R53 P0-3: 检测存储的哈希是否为旧 Argon2 格式。

    用于迁移诊断 — 识别 admin_principals 表中可能存在的旧 Argon2 hash,
    提示运维重新生成 PBKDF2 hash。

    Args:
        stored_hash: 存储的哈希字符串

    Returns:
        True 表示为旧 Argon2 格式(需迁移);False 表示非 Argon2(含空字符串/None)
    """
    if not stored_hash:
        return False
    return stored_hash.startswith(_ARGON2_PREFIX)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成 PBKDF2 哈希字符串。

    格式: $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>

    每次调用使用随机 salt,因此同一密码会产生不同哈希。

    Args:
        password: 待哈希的明文密码
        iterations: PBKDF2 迭代次数(必须在 10_000 ~ 1_000_000 范围内)

    Returns:
        PBKDF2 哈希字符串

    Raises:
        ValueError: when iterations is out of allowed range
    """
    # R53 P0-3: iterations range validation (prevent CPU/memory exhaustion)
    if iterations < _PBKDF2_MIN_ITERATIONS or iterations > _PBKDF2_MAX_ITERATIONS:
        raise ValueError(
            f"PBKDF2 iterations {iterations} out of allowed range"
            f"[{_PBKDF2_MIN_ITERATIONS}, {_PBKDF2_MAX_ITERATIONS}]"
        )
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"${PBKDF2_ALGORITHM}${iterations}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码与存储的 PBKDF2 哈希是否匹配。

    使用 hmac.compare_digest 防止时序攻击。空密码 / 空 hash / 格式错误
    一律返回 False(不抛异常)。

    R53 P0-3 整改:
    - iterations 超过上限(_PBKDF2_MAX_ITERATIONS)时抛 ValueError
      (防止错误配置造成 CPU/内存消耗,属于配置错误而非格式错误)
    - salt_hex / hash_hex 长度不匹配时抛 ValueError
      (合法 hex 但长度错误属于配置错误,应显式报错而非静默返回 False)
    - 非 hex 字符仍返回 False(格式错误,非配置错误)

    Args:
        password: 待验证的明文密码
        stored_hash: 存储的 PBKDF2 哈希字符串

    Returns:
        True 表示密码匹配;False 表示不匹配或格式错误

    Raises:
        ValueError: iterations 超出上限,或 salt_hex/hash_hex 长度不匹配
    """
    if not stored_hash or not password:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 5:
        return False
    if parts[1] != PBKDF2_ALGORITHM:
        return False
    try:
        iterations = int(parts[2])
    except (ValueError, TypeError):
        return False
    # R39 P1-12: 防御 — 拒绝过低的迭代次数(静默返回 False)
    if iterations < _PBKDF2_MIN_ITERATIONS:
        return False
    # R53 P0-3: defense — reject excessively high iterations (prevent CPU/memory exhaustion)
    if iterations > _PBKDF2_MAX_ITERATIONS:
        raise ValueError(
            f"PBKDF2 iterations {iterations} exceeds maximum {_PBKDF2_MAX_ITERATIONS}"
        )

    salt_hex = parts[3]
    hash_hex = parts[4]

    # 先尝试验证 hex 合法性(非 hex 字符返回 False,不抛异常)
    try:
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False

    # R53 P0-3: 精确校验 salt_hex / hash_hex 长度
    # (合法 hex 但长度错误属于配置错误,抛 ValueError 而非静默返回 False)
    if len(salt_hex) != PBKDF2_SALT_BYTES * 2:
        raise ValueError(
            f"PBKDF2 salt_hex length must be {PBKDF2_SALT_BYTES * 2},"
            f"got {len(salt_hex)}"
        )
    if len(hash_hex) != PBKDF2_HASH_BYTES * 2:
        raise ValueError(
            f"PBKDF2 hash_hex length must be {PBKDF2_HASH_BYTES * 2},"
            f"got {len(hash_hex)}"
        )

    actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_hash, expected_hash)


# 向后兼容别名(admin/__init__.py 原有函数名)
_verify_password = verify_password
