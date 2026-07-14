"""R52 P0-1: 纯密码哈希/验证模块(无 Settings 依赖)。

将 PBKDF2 hash/verify 函数从 admin/__init__.py 拆出,避免 E2E 验证时
import 整个 admin 模块触发 Settings 校验副作用。

格式约定(与 admin.generate_password_hash 的 PBKDF2 降级路径完全一致):
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


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成 PBKDF2 哈希字符串。

    格式: $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>

    每次调用使用随机 salt,因此同一密码会产生不同哈希。
    """
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"${PBKDF2_ALGORITHM}${iterations}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码与存储的 PBKDF2 哈希是否匹配。

    使用 hmac.compare_digest 防止时序攻击。空密码 / 空 hash / 格式错误
    一律返回 False(不抛异常)。
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
        # R39 P1-12: 防御 — 拒绝过低的迭代次数(保留原 admin._verify_password 行为)
        if iterations < _PBKDF2_MIN_ITERATIONS:
            return False
        salt = bytes.fromhex(parts[3])
        expected_hash = bytes.fromhex(parts[4])
    except (ValueError, TypeError):
        return False
    actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_hash, expected_hash)


# 向后兼容别名(admin/__init__.py 原有函数名)
_verify_password = verify_password
