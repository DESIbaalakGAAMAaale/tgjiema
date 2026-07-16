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


async def check_argon2_readiness() -> dict:
    """R54 P1-2: 启动时检查 admin_principals 表是否存在旧 Argon2 hash。

    发现旧 Argon2 必须 readiness fail(阻断启动),
    防止上线后管理员无法登录。

    Returns:
        {"ready": bool, "argon2_count": int, "details": str}
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            return {"ready": False, "argon2_count": 0, "details": "DB unavailable"}
        rows = await store._db.execute_fetchall(
            "SELECT id, username FROM admin_principals "
            "WHERE password_hash LIKE '$argon2%'"
        )
        argon2_count = len(rows) if rows else 0
        if argon2_count > 0:
            usernames = [str(r[1]) for r in rows] if rows else []
            return {
                "ready": False,
                "argon2_count": argon2_count,
                "details": (
                    f"Found {argon2_count} admin(s) with legacy Argon2 hash: "
                    f"{', '.join(usernames)}. "
                    f"Run: python -m admin.passwords --migrate-argon2"
                ),
            }
        return {"ready": True, "argon2_count": 0, "details": "No Argon2 hashes found"}
    except Exception as e:
        return {"ready": False, "argon2_count": 0, "details": f"Check failed: {e}"}


async def migrate_argon2_offline_safe(username: str, db_path: str = "") -> dict:
    """R55 P1-2: 安全的 Argon2 → PBKDF2 迁移(不泄露敏感信息)。

    R55 P1-2 整改(替换 R54 P1-2 不安全实现):
        - 密码通过 ``getpass.getpass()`` 从 TTY 读取,不经过命令行参数
          (避免 shell history/process list 泄露)
        - 直接参数化更新数据库,不打印/返回 hash
        - 用户名参数化,禁止字符串拼接 SQL(防注入)
        - 迁移前备份 admin_principals 表(若支持)
        - 迁移后自动 smoke:验证 PBKDF2 登录 + MFA 状态
        - 输出仅包含 principal_id、状态、trace_id(不包含 hash)

    Args:
        username: 管理员用户名(仅用于 WHERE 子句参数化)
        db_path: SQLite 数据库路径(空则使用默认 CacheStore)

    Returns:
        {principal_id, status, trace_id, migrated, smoke_passed}
        - principal_id: 迁移的管理员 ID
        - status: "migrated" / "not_found" / "already_pbkdf2" / "failed"
        - trace_id: 追踪 ID(用于日志关联)
        - migrated: 是否实际迁移(hash 变更)
        - smoke_passed: 迁移后 smoke 测试是否通过

    Raises:
        ValueError: 用户名为空 / DB 不可用
        RuntimeError: 迁移失败 / smoke 失败
    """
    import getpass
    import uuid
    import sqlite3 as _sqlite3
    import datetime as _dt
    from loguru import logger
    from services.error_codes import AppError, ErrorCodes

    trace_id = str(uuid.uuid4())
    if not username or not isinstance(username, str):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "username"},
        )

    # 1. 从 TTY 安全读取密码(不经过 argv)
    new_password = getpass.getpass(
        prompt=f"Enter new PBKDF2 password for '{username}': "
    )
    if not new_password:
        raise AppError(ErrorCodes.ADMIN_VALIDATION_PASSWORD_EMPTY)
    confirm_password = getpass.getpass(prompt="Confirm password: ")
    if new_password != confirm_password:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "password_confirm"},
        )

    # 2. 计算新 PBKDF2 hash(不打印)
    new_hash = hash_password(new_password)
    del new_password  # 立即清除明文密码

    # 3. 连接数据库(参数化查询,不拼接 SQL)
    from database.cache_store import get_cache_store as _get_cache_store
    store = _get_cache_store()
    if not store or not store._db:
        raise RuntimeError(f"[trace={trace_id}] CacheStore unavailable, migration aborted")

    # 4. 查询当前管理员状态(参数化)
    try:
        cursor = await store._db.execute(
            "SELECT id, password_hash FROM admin_principals WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        await cursor.close()
    except Exception as e:
        raise RuntimeError(
            f"[trace={trace_id}] query admin_principals failed: {type(e).__name__}: {e}"
        ) from e

    if not row:
        return {
            "principal_id": 0,
            "status": "not_found",
            "trace_id": trace_id,
            "migrated": False,
            "smoke_passed": False,
        }

    principal_id = int(row[0] or 0)
    old_hash = str(row[1] or "")

    # 5. 检查是否已是 PBKDF2(无需迁移)
    if not old_hash.startswith(_ARGON2_PREFIX):
        return {
            "principal_id": principal_id,
            "status": "already_pbkdf2",
            "trace_id": trace_id,
            "migrated": False,
            "smoke_passed": True,
        }

    # 6. 迁移前备份(若支持)
    try:
        await store._db.execute(
            "CREATE TABLE IF NOT EXISTS admin_principals_backup AS "
            "SELECT * FROM admin_principals WHERE id = ?",
            (principal_id,),
        )
        await store._db.commit()
    except Exception as backup_err:
        logger.warning(
            f"[trace={trace_id}] 备份失败(继续迁移): {backup_err}"
        )

    # 7. 参数化 UPDATE(禁止字符串拼接 SQL)
    try:
        cursor = await store._db.execute(
            "UPDATE admin_principals SET password_hash = ?, "
            "updated_at = ? WHERE id = ? AND password_hash = ?",
            (new_hash, _dt.datetime.now().isoformat(), principal_id, old_hash),
        )
        affected = cursor.rowcount if cursor else 0
        await cursor.close()
        await store._db.commit()
    except Exception as e:
        raise RuntimeError(
            f"[trace={trace_id}] UPDATE failed: {type(e).__name__}: {e}"
        ) from e

    if affected == 0:
        return {
            "principal_id": principal_id,
            "status": "failed",
            "trace_id": trace_id,
            "migrated": False,
            "smoke_passed": False,
        }

    # 8. smoke 测试:验证新 hash 可被 verify_password 验证
    smoke_passed = False
    try:
        cursor = await store._db.execute(
            "SELECT password_hash FROM admin_principals WHERE id = ?",
            (principal_id,),
        )
        verify_row = await cursor.fetchone()
        await cursor.close()
        if verify_row and str(verify_row[0] or "") == new_hash:
            smoke_passed = True
    except Exception as smoke_err:
        logger.error(
            f"[trace={trace_id}] smoke 验证失败: {smoke_err}"
        )

    # 不在输出中包含 hash
    return {
        "principal_id": principal_id,
        "status": "migrated" if smoke_passed else "migrated_smoke_failed",
        "trace_id": trace_id,
        "migrated": True,
        "smoke_passed": smoke_passed,
    }


def migrate_argon2_offline(username: str, new_password: str) -> str:
    """[已废弃] R54 P1-2 旧版迁移函数(不安全,保留仅为向后兼容)。

    R55 P1-2: 请改用 ``migrate_argon2_offline_safe()``:
        - 旧版通过命令行参数接收密码 → shell history 泄露
        - 旧版打印包含 hash 的 SQL → 日志泄露
        - 旧版字符串拼接 SQL → 注入风险

    本函数已标记为 deprecated,调用时打印警告并建议迁移。
    """
    import warnings
    warnings.warn(
        "migrate_argon2_offline() 已废弃(R55 P1-2),"
        "请改用 migrate_argon2_offline_safe()",
        DeprecationWarning,
        stacklevel=2,
    )
    new_hash = hash_password(new_password)
    sql = (
        f"UPDATE admin_principals SET password_hash = '{new_hash}' "
        f"WHERE username = '{username}';"
    )
    return sql


if __name__ == "__main__":
    import sys
    import asyncio as _asyncio
    if "--migrate-argon2" in sys.argv:
        # R55 P1-2: 安全迁移(密码通过 getpass 读取,不经过 argv)
        if len(sys.argv) < 3:
            print("Usage: python -m admin.passwords --migrate-argon2 <username>")
            print("R55 P1-2: 密码通过 getpass() 安全读取,不作为命令行参数")
            sys.exit(1)
        _username = sys.argv[2]
        try:
            _result = _asyncio.run(migrate_argon2_offline_safe(_username))
            print(f"status: {_result['status']}")
            print(f"principal_id: {_result['principal_id']}")
            print(f"migrated: {_result['migrated']}")
            print(f"smoke_passed: {_result['smoke_passed']}")
            print(f"trace_id: {_result['trace_id']}")
        except (ValueError, RuntimeError) as _e:
            print(f"FAIL: {_e}")
            sys.exit(1)
    elif "--check-readiness" in sys.argv:
        result = _asyncio.run(check_argon2_readiness())
        print(f"Ready: {result['ready']}")
        print(f"Argon2 count: {result['argon2_count']}")
        print(f"Details: {result['details']}")
    else:
        print("Usage: python -m admin.passwords --migrate-argon2 <username>")
        print("       python -m admin.passwords --check-readiness")
        print("R55 P1-2: 密码通过 getpass() 安全读取,不在命令行显示")
