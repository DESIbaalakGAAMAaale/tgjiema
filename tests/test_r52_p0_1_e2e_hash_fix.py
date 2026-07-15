"""R52 P0-1: E2E 密码哈希同 step GITHUB_ENV 问题修复的单元测试。

测试目标: 验证拆分出的纯模块 admin/passwords.py 的 hash_password / verify_password
行为正确,以及 _verify_password 向后兼容别名可用。

加载策略: 通过 importlib 按文件路径加载 admin/passwords.py,避免触发
admin/__init__.py 的 Settings / FastAPI / database 等重依赖副作用
(与 E2E 中 admin.passwords 的纯模块定位一致)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_PASSWORDS_PY = Path(__file__).resolve().parent.parent / "admin" / "passwords.py"
_INIT_PY = Path(__file__).resolve().parent.parent / "admin" / "__init__.py"


@pytest.fixture(scope="module")
def pw():
    """按文件路径加载 admin/passwords.py 为独立模块(不触发 admin 包初始化)。"""
    spec = importlib.util.spec_from_file_location("_r52_admin_passwords", _PASSWORDS_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None, "无法为 admin/passwords.py 创建 loader"
    spec.loader.exec_module(mod)
    return mod


# --- 场景 1: hash_password 输出格式正确(5 段,$ 分隔) ---
def test_hash_password_format(pw):
    h = pw.hash_password("any_password")
    parts = h.split("$")
    assert len(parts) == 5, f"hash parts count != 5: {len(parts)}"
    assert parts[1] == pw.PBKDF2_ALGORITHM, f"wrong algorithm: {parts[1]}"
    assert parts[2] == str(pw.PBKDF2_ITERATIONS), f"wrong iterations: {parts[2]}"
    assert len(parts[3]) == 32, f"salt_hex length != 32: {len(parts[3])}"
    assert len(parts[4]) == 64, f"hash_hex length != 64: {len(parts[4])}"


# --- 场景 2: verify_password 正确密码返回 True ---
def test_verify_password_correct(pw):
    h = pw.hash_password("test_bootstrap_pw")
    assert pw.verify_password("test_bootstrap_pw", h) is True


# --- 场景 3: verify_password 错误密码返回 False ---
def test_verify_password_wrong(pw):
    h = pw.hash_password("test_bootstrap_pw")
    assert pw.verify_password("wrong_password", h) is False


# --- 场景 4: verify_password 空密码返回 False ---
def test_verify_password_empty_password(pw):
    h = pw.hash_password("test_bootstrap_pw")
    assert pw.verify_password("", h) is False


# --- 场景 5: verify_password 空 hash 返回 False ---
def test_verify_password_empty_hash(pw):
    assert pw.verify_password("test_bootstrap_pw", "") is False


# --- 场景 6: verify_password 格式错误 hash 返回 False ---
@pytest.mark.parametrize(
    "bad_hash",
    [
        "not_a_hash",                              # 仅 1 段
        "$bcrypt$10$abc$def",                       # 错误算法
        "$pbkdf2-sha256$abc$cd$ef",                 # iterations 非数字
        "$pbkdf2-sha256$100$cd$ef",                 # iterations 过低(< 10k 防御)
        "$pbkdf2-sha256$200000$zz$ff",              # salt 非 hex
        "pbkdf2-sha256$200000$cd$ef",               # 缺少前导 $ → 4 段
    ],
)
def test_verify_password_malformed_hash(pw, bad_hash):
    assert pw.verify_password("test_bootstrap_pw", bad_hash) is False


# --- 场景 7: hash_password 不同调用产生不同 salt(随机性) ---
def test_hash_password_random_salt(pw):
    h1 = pw.hash_password("same_password")
    h2 = pw.hash_password("same_password")
    assert h1 != h2, "salt 应随机,同一密码两次哈希应不同"
    # 但两次都能被正确验证
    assert pw.verify_password("same_password", h1) is True
    assert pw.verify_password("same_password", h2) is True


# --- 场景 8: _verify_password 向后兼容别名可用 ---
def test_verify_password_backward_compat_alias(pw):
    # 别名指向同一函数对象
    assert pw._verify_password is pw.verify_password
    # 别名可正常调用(签名 (plaintext, stored) 不变)
    h = pw.hash_password("compat_pw")
    assert pw._verify_password("compat_pw", h) is True
    assert pw._verify_password("nope", h) is False
    # 静态校验: admin/__init__.py 已将 _verify_password 从 passwords.py 导入(向后兼容)
    # R53 P0-3: 导入改为多行 from admin.passwords import (...) 形式
    init_src = _INIT_PY.read_text(encoding="utf-8")
    assert (
        "from admin.passwords import verify_password as _verify_password" in init_src
        or (
            "from admin.passwords import (" in init_src
            and "verify_password as _verify_password" in init_src
        )
    ), "admin/__init__.py 必须从 passwords.py 导入 _verify_password 以保持向后兼容"
