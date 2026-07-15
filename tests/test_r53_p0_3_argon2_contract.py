"""R53 P0-3: Admin Argon2 契约断裂修复测试。

背景:
    admin/__init__.py 仍会将 ``$argon2id$`` 识别为受支持哈希,且当 argon2-cffi
    存在时 ``generate_password_hash()`` 生成 Argon2id。但新的
    ``admin/passwords.verify_password()`` 只接受 PBKDF2。结果:已有 Argon2 管理员
    密码或新生成的 Argon2 密码会被识别为"合法哈希",实际登录却永久失败。

R53 P0-3 整改:
- 彻底移除 Argon2 生成与识别,统一用 PBKDF2
- 新增 ``is_argon2_hash`` 辅助函数识别旧 Argon2 hash(迁移诊断)
- PBKDF2 校验增加最大 iterations 限制(1_000_000)
- 精确校验 salt_hex 长度(32)和 hash_hex 长度(64)
- 启动自检 ``detect_legacy_argon2_hashes`` 扫描 admin_principals 表

测试覆盖:
    1. PBKDF2 正向验证(正确密码)
    2. PBKDF2 反向验证(错误密码)
    3. 旧 Argon2 hash 识别(is_argon2_hash 返回 True)
    4. 旧 Argon2 hash 验证失败(verify_password 返回 False,不抛异常)
    5. 错误密码使用恒定时间比较(hmac.compare_digest)
    6. iterations 超过最大值时抛 ValueError
    7. salt_hex 长度不为 32 时抛 ValueError
    8. hash_hex 长度不为 64 时抛 ValueError
    9. 格式错误的 hash(段数不对)返回 False
    10. generate_password_hash 生成的 hash 用 verify_password 验证通过
    11. detect_legacy_argon2_hashes 在无 Argon2 时返回空列表,有 Argon2 时返回列表

测试策略:
    - admin/passwords.py 通过 importlib 按文件路径加载(避免触发 admin 包初始化)
    - generate_password_hash / detect_legacy_argon2_hashes 通过 mock 依赖后从 admin 导入
    - 中文注释,英文 raise 消息
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
if "telegram" not in sys.modules:
    sys.modules["telegram"] = MagicMock()
if "telegram.ext" not in sys.modules:
    sys.modules["telegram.ext"] = MagicMock()

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PASSWORDS_PY = _REPO_ROOT / "admin" / "passwords.py"
_INIT_PY = _REPO_ROOT / "admin" / "__init__.py"


# ════════════════════════════════════════════════════════════════
# 辅助 fixture: 按文件路径加载 admin/passwords.py 为独立模块
# ════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def pw():
    """按文件路径加载 admin/passwords.py(不触发 admin 包初始化)。"""
    spec = importlib.util.spec_from_file_location("_r53_admin_passwords", _PASSWORDS_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None, "无法为 admin/passwords.py 创建 loader"
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════
# 1. PBKDF2 正向/反向验证
# ════════════════════════════════════════════════════════════════

class TestPbkdf2Verification:
    """PBKDF2 正向/反向验证测试。"""

    def test_pbkdf2_correct_password(self, pw):
        """PBKDF2 正向验证:正确密码返回 True。"""
        h = pw.hash_password("my_secret_password")
        assert pw.verify_password("my_secret_password", h) is True

    def test_pbkdf2_wrong_password(self, pw):
        """PBKDF2 反向验证:错误密码返回 False。"""
        h = pw.hash_password("my_secret_password")
        assert pw.verify_password("wrong_password", h) is False


# ════════════════════════════════════════════════════════════════
# 2. 旧 Argon2 hash 识别与验证
# ════════════════════════════════════════════════════════════════

class TestLegacyArgon2Detection:
    """旧 Argon2 hash 识别与验证测试。"""

    def test_is_argon2_hash_returns_true_for_argon2id(self, pw):
        """is_argon2_hash 正确识别 $argon2id$ 前缀。"""
        argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$hashhash"
        assert pw.is_argon2_hash(argon2_hash) is True

    def test_is_argon2_hash_returns_true_for_argon2i(self, pw):
        """is_argon2_hash 正确识别 $argon2i$ 前缀。"""
        argon2_hash = "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$hashhash"
        assert pw.is_argon2_hash(argon2_hash) is True

    def test_is_argon2_hash_returns_false_for_pbkdf2(self, pw):
        """is_argon2_hash 对 PBKDF2 hash 返回 False。"""
        h = pw.hash_password("test")
        assert pw.is_argon2_hash(h) is False

    def test_is_argon2_hash_returns_false_for_empty(self, pw):
        """is_argon2_hash 对空字符串返回 False。"""
        assert pw.is_argon2_hash("") is False
        assert pw.is_argon2_hash(None) is False

    def test_argon2_hash_verify_returns_false(self, pw):
        """旧 Argon2 hash 验证失败:verify_password 返回 False,不抛异常。

        R53 P0-3 核心契约:Argon2 hash 不再受支持,verify_password 必须返回
        False 而非抛异常(避免登录流程 500 错误)。
        """
        argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$hashhash"
        # 不应抛异常
        result = pw.verify_password("any_password", argon2_hash)
        assert result is False, "旧 Argon2 hash 应返回 False,而非抛异常"


# ════════════════════════════════════════════════════════════════
# 3. 恒定时间比较验证
# ════════════════════════════════════════════════════════════════

class TestConstantTimeComparison:
    """错误密码使用恒定时间比较(hmac.compare_digest)。"""

    def test_uses_hmac_compare_digest(self, pw):
        """verify_password 内部使用 hmac.compare_digest(源码静态校验)。"""
        source = _PASSWORDS_PY.read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source, (
            "verify_password 必须使用 hmac.compare_digest 防止时序攻击"
        )

    def test_wrong_password_returns_false_not_raise(self, pw):
        """错误密码返回 False 而非抛异常(恒定时间比较不泄露信息)。"""
        h = pw.hash_password("correct_password")
        # 多次错误尝试均应返回 False,不抛异常
        for wrong in ["", "wrong", "WRONG", "correct_password ", " correct_password"]:
            assert pw.verify_password(wrong, h) is False


# ════════════════════════════════════════════════════════════════
# 4. iterations 范围校验
# ════════════════════════════════════════════════════════════════

class TestIterationsValidation:
    """PBKDF2 iterations 范围校验测试。"""

    def test_verify_raises_when_iterations_exceeds_max(self, pw):
        """iterations 超过最大值(1_000_000)时抛 ValueError。

        R53 P0-3: 防止错误配置造成 CPU/内存消耗(DoS)。
        """
        # 构造 iterations=2_000_000 的 hash(超过 1_000_000 上限)
        # 使用合法的 salt_hex(32)和 hash_hex(64)确保不会因长度问题提前返回
        salt_hex = "ab" * 16  # 32 chars
        hash_hex = "cd" * 32  # 64 chars
        bad_hash = f"$pbkdf2-sha256$2000000${salt_hex}${hash_hex}"
        with pytest.raises(ValueError, match="iterations"):
            pw.verify_password("test", bad_hash)

    def test_hash_password_raises_when_iterations_exceeds_max(self, pw):
        """hash_password 在 iterations 超过最大值时抛 ValueError。"""
        with pytest.raises(ValueError, match="iterations"):
            pw.hash_password("test", iterations=2_000_000)

    def test_hash_password_raises_when_iterations_below_min(self, pw):
        """hash_password 在 iterations 低于最小值时抛 ValueError。"""
        with pytest.raises(ValueError, match="iterations"):
            pw.hash_password("test", iterations=1000)

    def test_verify_returns_false_when_iterations_below_min(self, pw):
        """verify_password 在 iterations 低于最小值时返回 False(静默拒绝)。"""
        salt_hex = "ab" * 16
        hash_hex = "cd" * 32
        bad_hash = f"$pbkdf2-sha256$1000${salt_hex}${hash_hex}"
        assert pw.verify_password("test", bad_hash) is False


# ════════════════════════════════════════════════════════════════
# 5. salt_hex / hash_hex 长度校验
# ════════════════════════════════════════════════════════════════

class TestSaltHashLengthValidation:
    """salt_hex / hash_hex 精确长度校验测试。"""

    def test_verify_raises_when_salt_hex_length_wrong(self, pw):
        """salt_hex 长度不为 32 时抛 ValueError。"""
        # salt_hex 长度为 16(应为 32),hash_hex 长度正确
        short_salt = "ab" * 8  # 16 chars
        correct_hash = "cd" * 32  # 64 chars
        bad_hash = f"$pbkdf2-sha256$200000${short_salt}${correct_hash}"
        with pytest.raises(ValueError, match="salt_hex"):
            pw.verify_password("test", bad_hash)

    def test_verify_raises_when_hash_hex_length_wrong(self, pw):
        """hash_hex 长度不为 64 时抛 ValueError。"""
        # salt_hex 长度正确,hash_hex 长度为 32(应为 64)
        correct_salt = "ab" * 16  # 32 chars
        short_hash = "cd" * 16  # 32 chars
        bad_hash = f"$pbkdf2-sha256$200000${correct_salt}${short_hash}"
        with pytest.raises(ValueError, match="hash_hex"):
            pw.verify_password("test", bad_hash)


# ════════════════════════════════════════════════════════════════
# 6. 格式错误 hash 返回 False
# ════════════════════════════════════════════════════════════════

class TestMalformedHashReturnsFalse:
    """格式错误的 hash(段数不对)返回 False,不抛异常。"""

    @pytest.mark.parametrize("bad_hash", [
        "not_a_hash",                              # 仅 1 段
        "$pbkdf2-sha256$200000$salt$hash",         # salt/hash 非 hex(但也长度不对)
        "pbkdf2-sha256$200000$salt$hash",           # 缺少前导 $ → 4 段
        "$bcrypt$10$abc$def$extra",                # 错误算法,6 段
        "",                                         # 空字符串
        "$pbkdf2-sha256$abc$cd$ef",                 # iterations 非数字 → 返回 False
    ])
    def test_malformed_hash_returns_false(self, pw, bad_hash):
        """格式错误的 hash 返回 False,不抛异常。"""
        assert pw.verify_password("test", bad_hash) is False

    def test_wrong_algorithm_returns_false(self, pw):
        """非 PBKDF2 算法的 hash 返回 False(5 段但算法名错误)。"""
        bad_hash = "$bcrypt$200000$abcd$efgh"
        assert pw.verify_password("test", bad_hash) is False


# ════════════════════════════════════════════════════════════════
# 7. generate_password_hash + verify_password 端到端验证
# ════════════════════════════════════════════════════════════════

def _ensure_admin_importable():
    """注入 admin 模块所需的重依赖 mock,使 admin 可安全导入。

    与 test_r40_admin_templates.py 中的 _ensure_admin_dependencies 类似,
    但仅注入 generate_password_hash / detect_legacy_argon2_hashes 所需的最小依赖。
    """
    # database: conftest 可能已构造轻量包,补齐 admin 需要的属性
    if "database" not in sys.modules:
        db = types.ModuleType("database")
        sys.modules["database"] = db
    db = sys.modules["database"]
    if not hasattr(db, "get_users_col"):
        db.get_users_col = MagicMock()
    if not hasattr(db, "get_file_records_col"):
        db.get_file_records_col = MagicMock()
    if not hasattr(db, "get_decode_logs_col"):
        db.get_decode_logs_col = MagicMock()
    if not hasattr(db, "init_db"):
        db.init_db = AsyncMock()
    if not hasattr(db, "close_db"):
        db.close_db = AsyncMock()

    # database.cache_store
    if "database.cache_store" not in sys.modules:
        cs = types.ModuleType("database.cache_store")
        cs.get_cache_store = MagicMock(return_value=MagicMock())
        cs.get_all_bot_heartbeats = AsyncMock(return_value={})
        sys.modules["database.cache_store"] = cs
        setattr(db, "cache_store", cs)

    # utils 包
    if "utils" not in sys.modules:
        sys.modules["utils"] = types.ModuleType("utils")
    if "utils.monitor" not in sys.modules:
        mon = types.ModuleType("utils.monitor")
        mon.metrics = MagicMock()
        mon.metrics.bots = {}
        mon.metrics.backup_count = 0
        mon.metrics.backup_fail_count = 0
        sys.modules["utils.monitor"] = mon
        setattr(sys.modules["utils"], "monitor", mon)
    if "utils.shared_counters" not in sys.modules:
        sc = types.ModuleType("utils.shared_counters")
        sc.status_counters = {
            "total_users": 0, "total_files": 0, "active_files": 0,
            "today_decodes": 0, "total_logs": 0,
        }
        sc.status_counters_initialized = True
        sc.status_counters_loaded_at = 0
        sys.modules["utils.shared_counters"] = sc
        setattr(sys.modules["utils"], "shared_counters", sc)

    # config.settings 补齐 admin 专用属性
    import config
    s = config.settings
    if not hasattr(s, "ADMIN_USERNAME"):
        s.ADMIN_USERNAME = "admin"
    if not hasattr(s, "ADMIN_PASSWORD"):
        s.ADMIN_PASSWORD = "$pbkdf2-sha256$200000$" + "ab" * 16 + "$" + "cd" * 32
    if not hasattr(s, "ADMIN_LOGIN_WINDOW"):
        s.ADMIN_LOGIN_WINDOW = 300
    if not hasattr(s, "ADMIN_LOGIN_MAX_FAIL"):
        s.ADMIN_LOGIN_MAX_FAIL = 5
    if not hasattr(s, "ADMIN_COUNT_CACHE_TTL"):
        s.ADMIN_COUNT_CACHE_TTL = 60
    if not hasattr(s, "ADMIN_SEARCH_MAX_LENGTH"):
        s.ADMIN_SEARCH_MAX_LENGTH = 50
    if not hasattr(s, "ADMIN_PAGE_SIZE"):
        s.ADMIN_PAGE_SIZE = 20
    if not hasattr(s, "ADMIN_FILES_PAGE_SIZE"):
        s.ADMIN_FILES_PAGE_SIZE = 50
    if not hasattr(s, "CSRF_COOKIE_SECURE"):
        s.CSRF_COOKIE_SECURE = False
    if not hasattr(s, "ENVIRONMENT"):
        s.ENVIRONMENT = "test"


class TestGeneratePasswordHashEndToEnd:
    """generate_password_hash 生成的 hash 用 verify_password 验证通过。"""

    def test_generate_password_hash_produces_valid_pbkdf2(self):
        """generate_password_hash 生成的 PBKDF2 hash 可被 verify_password 验证通过。

        R53 P0-3 核心契约:generate_password_hash 永远生成 PBKDF2(不再生成 Argon2),
        且生成的 hash 可被 admin.passwords.verify_password 验证通过。
        """
        _ensure_admin_importable()
        from admin import generate_password_hash
        from admin.passwords import verify_password, is_argon2_hash

        password = "e2e_test_password_123"
        generated_hash = generate_password_hash(password)

        # 生成的 hash 必须是 PBKDF2 格式(非 Argon2)
        assert is_argon2_hash(generated_hash) is False, (
            "generate_password_hash 不应生成 Argon2 hash(R53 P0-3)"
        )
        assert generated_hash.startswith("$pbkdf2-sha256$"), (
            "generate_password_hash 必须生成 PBKDF2 格式 hash"
        )

        # verify_password 必须验证通过
        assert verify_password(password, generated_hash) is True, (
            "generate_password_hash 生成的 hash 必须可被 verify_password 验证通过"
        )
        # 错误密码必须验证失败
        assert verify_password("wrong_password", generated_hash) is False

    def test_generate_password_hash_never_generates_argon2(self):
        """generate_password_hash 永远不生成 Argon2 hash(源码静态校验)。

        R53 P0-3: 彻底移除 Argon2 生成分支。
        """
        source = _INIT_PY.read_text(encoding="utf-8")
        # generate_password_hash 函数体内不应包含 _argon2_hasher 调用
        # (查找函数定义到下一个函数定义之间的内容)
        func_start = source.find("def generate_password_hash(")
        assert func_start != -1, "generate_password_hash 函数未找到"
        func_end = source.find("\n\n\n", func_start)
        if func_end == -1:
            func_end = len(source)
        func_body = source[func_start:func_end]
        assert "_argon2_hasher" not in func_body, (
            "generate_password_hash 不应包含 _argon2_hasher 调用(R53 P0-3 已移除 Argon2 生成)"
        )
        assert "hash_password(password" in func_body, (
            "generate_password_hash 应委托 admin.passwords.hash_password 生成 PBKDF2"
        )

    def test_is_hashed_password_rejects_argon2(self):
        """_is_hashed_password 不再将 Argon2 识别为受支持哈希。

        R53 P0-3: 移除 $argon2id$ 识别为受支持哈希的分支。
        """
        _ensure_admin_importable()
        from admin import _is_hashed_password

        argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$hashhash"
        pbkdf2_hash = "$pbkdf2-sha256$200000$" + "ab" * 16 + "$" + "cd" * 32

        # PBKDF2 仍受支持
        assert _is_hashed_password(pbkdf2_hash) is True
        # Argon2 不再受支持(需迁移)
        assert _is_hashed_password(argon2_hash) is False, (
            "_is_hashed_password 不应将 Argon2 识别为受支持哈希(R53 P0-3)"
        )
        # 明文不受支持
        assert _is_hashed_password("plaintext_password") is False
        assert _is_hashed_password("") is False


# ════════════════════════════════════════════════════════════════
# 8. detect_legacy_argon2_hashes 启动自检
# ════════════════════════════════════════════════════════════════

class TestDetectLegacyArgon2Hashes:
    """detect_legacy_argon2_hashes 启动自检测试。

    无 Argon2 hash 时返回空列表;有 Argon2 hash 时返回 hash 列表。
    """

    def test_returns_empty_when_no_argon2_hashes(self):
        """无 Argon2 hash 时返回空列表。"""
        _ensure_admin_importable()
        from admin import detect_legacy_argon2_hashes

        # 构造 mock store:所有记录均为 PBKDF2 hash
        mock_store = MagicMock()
        mock_db = MagicMock()
        mock_store._db = mock_db
        mock_db.execute_fetchall = AsyncMock(return_value=[
            (1, "admin", "$pbkdf2-sha256$200000$abcd$efgh"),
            (2, "ops", "$pbkdf2-sha256$200000$ijkl$mnop"),
        ])

        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = asyncio_run(detect_legacy_argon2_hashes())

        assert result == [], "无 Argon2 hash 时应返回空列表"

    def test_returns_list_when_argon2_hashes_exist(self):
        """有 Argon2 hash 时返回 hash 列表。"""
        _ensure_admin_importable()
        from admin import detect_legacy_argon2_hashes

        # 构造 mock store:包含 1 条 PBKDF2 + 2 条 Argon2
        mock_store = MagicMock()
        mock_db = MagicMock()
        mock_store._db = mock_db
        mock_db.execute_fetchall = AsyncMock(return_value=[
            (1, "admin", "$pbkdf2-sha256$200000$abcd$efgh"),
            (2, "ops_argon2", "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$hash"),
            (3, "legacy_user", "$argon2i$v=19$m=32768,t=2,p=1$c2FsdA$hash"),
        ])

        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = asyncio_run(detect_legacy_argon2_hashes())

        assert len(result) == 2, "应检测到 2 条 Argon2 hash 记录"
        usernames = [r["username"] for r in result]
        assert "ops_argon2" in usernames
        assert "legacy_user" in usernames
        # 每条记录包含 id / username / hash_prefix
        for record in result:
            assert "id" in record
            assert "username" in record
            assert "hash_prefix" in record

    def test_returns_empty_when_db_unavailable(self):
        """DB 不可用时返回空列表(不阻塞启动)。"""
        _ensure_admin_importable()
        from admin import detect_legacy_argon2_hashes

        mock_store = MagicMock()
        mock_store._db = None  # DB 未初始化

        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = asyncio_run(detect_legacy_argon2_hashes())

        assert result == [], "DB 不可用时应返回空列表(不阻塞启动)"

    def test_returns_empty_when_db_query_raises(self):
        """DB 查询异常时返回空列表(不阻塞启动)。"""
        _ensure_admin_importable()
        from admin import detect_legacy_argon2_hashes

        mock_store = MagicMock()
        mock_db = MagicMock()
        mock_store._db = mock_db
        mock_db.execute_fetchall = AsyncMock(side_effect=RuntimeError("db error"))

        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = asyncio_run(detect_legacy_argon2_hashes())

        assert result == [], "DB 查询异常时应返回空列表(不阻塞启动)"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def asyncio_run(coro):
    """同步运行 async 协程(测试辅助)。

    使用 asyncio.run 而非 get_event_loop()(Python 3.10+ 弃用)。
    """
    import asyncio
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════
# 9. i18n key 存在性校验
# ════════════════════════════════════════════════════════════════

class TestI18nKeys:
    """R53 P0-3: 新增 i18n key 在 zh-CN 和 en-US 中都存在。"""

    def test_legacy_argon2_detected_key_in_zh_cn(self):
        """zh-CN.json 包含 admin.auth.legacy_argon2_detected key。"""
        import json
        locale_path = _REPO_ROOT / "locales" / "zh-CN.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        admin_section = data.get("admin", {})
        auth_section = admin_section.get("auth", {})
        assert "legacy_argon2_detected" in auth_section, (
            "zh-CN.json admin.auth.legacy_argon2_detected key 必须存在"
        )

    def test_legacy_argon2_detected_key_in_en_us(self):
        """en-US.json 包含 admin.auth.legacy_argon2_detected key。"""
        import json
        locale_path = _REPO_ROOT / "locales" / "en-US.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        admin_section = data.get("admin", {})
        auth_section = admin_section.get("auth", {})
        assert "legacy_argon2_detected" in auth_section, (
            "en-US.json admin.auth.legacy_argon2_detected key 必须存在"
        )

    def test_invalid_pbkdf2_iterations_key_in_zh_cn(self):
        """zh-CN.json 包含 admin.auth.invalid_pbkdf2_iterations key。"""
        import json
        locale_path = _REPO_ROOT / "locales" / "zh-CN.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        admin_section = data.get("admin", {})
        auth_section = admin_section.get("auth", {})
        assert "invalid_pbkdf2_iterations" in auth_section, (
            "zh-CN.json admin.auth.invalid_pbkdf2_iterations key 必须存在"
        )

    def test_invalid_pbkdf2_iterations_key_in_en_us(self):
        """en-US.json 包含 admin.auth.invalid_pbkdf2_iterations key。"""
        import json
        locale_path = _REPO_ROOT / "locales" / "en-US.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        admin_section = data.get("admin", {})
        auth_section = admin_section.get("auth", {})
        assert "invalid_pbkdf2_iterations" in auth_section, (
            "en-US.json admin.auth.invalid_pbkdf2_iterations key 必须存在"
        )

    def test_placeholder_consistency(self):
        """zh-CN 和 en-US 的占位符集合一致。"""
        import json
        import re

        def extract_placeholders(text):
            return set(re.findall(r"\{(\w+)\}", text))

        zh_path = _REPO_ROOT / "locales" / "zh-CN.json"
        en_path = _REPO_ROOT / "locales" / "en-US.json"
        with open(zh_path, "r", encoding="utf-8") as f:
            zh_data = json.load(f)
        with open(en_path, "r", encoding="utf-8") as f:
            en_data = json.load(f)

        for key in ("legacy_argon2_detected", "invalid_pbkdf2_iterations"):
            zh_val = zh_data["admin"]["auth"][key]
            en_val = en_data["admin"]["auth"][key]
            assert extract_placeholders(zh_val) == extract_placeholders(en_val), (
                f"admin.auth.{key} 占位符不一致: "
                f"zh={extract_placeholders(zh_val)} vs en={extract_placeholders(en_val)}"
            )
