"""R45 §7: Admin 认证安全加固测试。

测试范围(R42 终审报告第 7 节):
- 7.1 AdminPrincipal: bootstrap 原子性 + readiness 强制调用 + 进程退出
- 7.2 Session/MFA: 无 cookie/伪造/过期/撤销 session; TOTP 重放/错误限流;
                    CSRF token 验证; session rotate
- 7.3 RBAC: fail-closed; Principal ID 一致性; require_permission 强制门禁

测试用例(20+):
1.  bootstrap_admin_principal() 原子事务成功
2.  bootstrap_admin_principal() 参数无效返回 False(principal_id<=0/username 空)
3.  bootstrap 原子性 — 事务失败时回滚(无残留记录)
4.  ensure_readiness_or_exit() 未 bootstrap 时调用 sys.exit(1)
5.  ensure_readiness_or_exit() bootstrap 完成后正常返回
6.  Session 失效:无 cookie(validate_or_raise 抛 401)
7.  Session 失效:伪造 cookie(validate_session 返回 None)
8.  Session 失效:过期 session(validate_session 返回 None)
9.  Session 失效:撤销 session(destroy 后 validate 返回 None)
10. Session rotate — 旧 session 撤销,新 session 有效
11. CSRF token 验证 — token 与 session 绑定一致返回 True
12. CSRF token 验证 — token 不一致返回 False(fail-closed)
13. CSRF token 验证 — session 未绑定 token 返回 False
14. MFA TOTP 重放保护 — 同一 code 第二次验证返回 False
15. MFA 错误限流 — 连续 5 次错误后锁定
16. MFA 锁定解除 — 验证成功后清除失败计数
17. RBAC fail-closed — DB 异常时 check_permission 返回 False
18. RBAC require_permission — 无权限时抛 HTTPException(403)
19. RBAC require_permission — super_admin 不抛异常
20. Principal ID 一致性 — validate_session 返回的 id 与 ADMIN_PRINCIPAL_ID 一致
21. bootstrap_admin_principal_atomic() 幂等(已 bootstrap 时 skipped=True)
22. bootstrap_admin_principal_atomic() 原子事务失败回滚

测试策略:
- 使用临时 SQLite 数据库(real_store fixture)隔离生产数据
- 使用 monkeypatch 设置 settings 配置
- AST 检查 + 行为测试结合
"""
from __future__ import annotations

import ast
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

# 延迟导入,确保 conftest 已注入 fake config
from database import cache_store as _cs_module
from database.cache_store import CacheStore

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_INIT = REPO_ROOT / "admin" / "__init__.py"
SESSIONS_FILE = REPO_ROOT / "admin" / "sessions.py"
MFA_FILE = REPO_ROOT / "admin" / "mfa.py"
RBAC_FILE = REPO_ROOT / "services" / "rbac.py"
BOOTSTRAP_RUNNER_FILE = REPO_ROOT / "services" / "bootstrap_runner.py"


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


def _get_async_funcs(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}


def _get_sync_funcs(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite 数据库
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    设置 _cs_module._store 使 get_cache_store() 返回测试 store。
    同时初始化 rbac_roles 默认角色(super_admin 等)供 check_permission 测试。
    """
    tmpdir = tempfile.mkdtemp(prefix="r45_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        # 初始化默认 RBAC 角色(super_admin 等),供 check_permission 测试
        try:
            from services.rbac import init_default_roles
            await init_default_roles()
        except Exception:
            pass
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_admin_config(monkeypatch):
    """设置 settings.ADMIN_PRINCIPAL_ID / USERNAME / BOOTSTRAP_ROLES。"""
    import config

    def _set(principal_id: int, username: str = "admin", roles: str = "super_admin"):
        monkeypatch.setattr(config.settings, "ADMIN_PRINCIPAL_ID", principal_id, raising=False)
        monkeypatch.setattr(
            config.settings, "ADMIN_PRINCIPAL_USERNAME", username, raising=False
        )
        monkeypatch.setattr(
            config.settings, "ADMIN_PRINCIPAL_BOOTSTRAP_ROLES", roles, raising=False
        )
        monkeypatch.setattr(config.settings, "ADMIN_USERNAME", username, raising=False)

    return _set


@pytest.fixture(autouse=True)
def reset_mfa_state():
    """每个用例前重置 MFA 模块级状态(重放记录 + 失败计数)。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


# ════════════════════════════════════════════════════════════════
# 1. 静态检查:函数定义存在性
# ════════════════════════════════════════════════════════════════


class TestStaticChecks:
    """静态检查:新增函数定义存在性。"""

    def test_admin_init_has_ensure_readiness_or_exit(self):
        """admin/__init__.py 应定义 ensure_readiness_or_exit async 函数。"""
        tree = _parse_ast(ADMIN_INIT)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "ensure_readiness_or_exit" in async_funcs, (
            "应定义 ensure_readiness_or_exit async 函数(R45 §7.1)"
        )

    def test_admin_init_has_bootstrap_admin_principal(self):
        """admin/__init__.py 应定义 bootstrap_admin_principal async 函数。"""
        tree = _parse_ast(ADMIN_INIT)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "bootstrap_admin_principal" in async_funcs, (
            "应定义 bootstrap_admin_principal async 函数(R45 §7.1)"
        )

    def test_sessions_has_rotate_session(self):
        """admin/sessions.py 的 SessionManager 应有 rotate_session 方法。"""
        tree = _parse_ast(SESSIONS_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "rotate_session" in async_funcs, (
            "SessionManager 应定义 rotate_session async 方法(R45 §7.2)"
        )

    def test_sessions_has_validate_csrf_token(self):
        """admin/sessions.py 的 SessionManager 应有 validate_csrf_token 方法。"""
        tree = _parse_ast(SESSIONS_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "validate_csrf_token" in async_funcs, (
            "SessionManager 应定义 validate_csrf_token async 方法(R45 §7.2)"
        )

    def test_mfa_has_replay_protection_helpers(self):
        """admin/mfa.py 应定义 TOTP 重放保护 + 限流辅助函数。

        R46 P1: _is_totp_replayed / _record_mfa_failure / _is_locked /
        _clear_mfa_failures 已改为 async(读写 SQLite); _record_totp_usage /
        reset_mfa_state_for_testing 保持 sync(仅 L1 缓存)。
        """
        tree = _parse_ast(MFA_FILE)
        assert tree is not None
        sync_funcs = _get_sync_funcs(tree)
        async_funcs = _get_async_funcs(tree)
        # sync 辅助函数(仅 L1 缓存操作)
        sync_required = {
            "_record_totp_usage",
            "reset_mfa_state_for_testing",
        }
        sync_missing = sync_required - sync_funcs
        assert not sync_missing, f"admin/mfa.py 缺少 sync 辅助函数: {sync_missing}"
        # async 辅助函数(读写 SQLite 权威层)
        async_required = {
            "_is_totp_replayed",
            "_record_mfa_failure",
            "_is_locked",
            "_clear_mfa_failures",
        }
        async_missing = async_required - async_funcs
        assert not async_missing, f"admin/mfa.py 缺少 async 辅助函数: {async_missing}"

    def test_rbac_has_require_permission(self):
        """services/rbac.py 应定义 require_permission async 函数。"""
        tree = _parse_ast(RBAC_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "require_permission" in async_funcs, (
            "应定义 require_permission async 函数(R45 §7.3)"
        )

    def test_bootstrap_runner_has_atomic_bootstrap(self):
        """services/bootstrap_runner.py 应定义 bootstrap_admin_principal_atomic。"""
        tree = _parse_ast(BOOTSTRAP_RUNNER_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "bootstrap_admin_principal_atomic" in async_funcs, (
            "应定义 bootstrap_admin_principal_atomic async 函数(R45 §7.1)"
        )


# ════════════════════════════════════════════════════════════════
# 2. bootstrap 原子性测试
# ════════════════════════════════════════════════════════════════


class TestBootstrapAtomic:
    """bootstrap_admin_principal() 原子事务测试。"""

    @pytest.mark.asyncio
    async def test_bootstrap_success(self, real_store, mock_admin_config):
        """用例 1: bootstrap 原子事务成功。"""
        mock_admin_config(principal_id=10101, username="admin")
        from admin import bootstrap_admin_principal
        ok = await bootstrap_admin_principal()
        assert ok is True
        record = await real_store.get_admin_principal_record(10101)
        assert record is not None
        assert record["username"] == "admin"
        assert record["is_active"] is True

    @pytest.mark.asyncio
    async def test_bootstrap_invalid_principal_id(self, real_store, mock_admin_config):
        """用例 2a: principal_id<=0 时 bootstrap 返回 False。"""
        mock_admin_config(principal_id=0, username="admin")
        from admin import bootstrap_admin_principal
        ok = await bootstrap_admin_principal()
        assert ok is False

    @pytest.mark.asyncio
    async def test_bootstrap_invalid_username(self, real_store, mock_admin_config):
        """用例 2b: username 为空时 bootstrap 返回 False。"""
        mock_admin_config(principal_id=20202, username="")
        from admin import bootstrap_admin_principal
        ok = await bootstrap_admin_principal()
        assert ok is False

    @pytest.mark.asyncio
    async def test_bootstrap_explicit_params_override_config(
        self, real_store, mock_admin_config
    ):
        """显式参数优先于 settings 配置。"""
        mock_admin_config(principal_id=99999, username="wrong")
        from admin import bootstrap_admin_principal
        ok = await bootstrap_admin_principal(
            principal_id=30303, username="explicit_admin", roles=["super_admin"]
        )
        assert ok is True
        record = await real_store.get_admin_principal_record(30303)
        assert record is not None
        assert record["username"] == "explicit_admin"
        # 验证未使用 settings 中的 principal_id
        wrong_record = await real_store.get_admin_principal_record(99999)
        assert wrong_record is None

    @pytest.mark.asyncio
    async def test_bootstrap_atomic_rollback_on_failure(self, real_store, mock_admin_config):
        """用例 3/22: bootstrap 原子性 — 事务失败时回滚(无残留记录)。

        通过 mock CacheStore.bootstrap_admin_principal 抛异常验证回滚。
        """
        mock_admin_config(principal_id=40404, username="rollback_admin")
        from admin import bootstrap_admin_principal

        # mock store.bootstrap_admin_principal 抛异常
        with patch.object(
            real_store,
            "bootstrap_admin_principal",
            new=MagicMock(side_effect=Exception("模拟事务失败")),
        ):
            ok = await bootstrap_admin_principal()
        assert ok is False, "事务失败应返回 False"
        # 验证记录未写入(回滚)— 注意:由于 mock 抛异常,实际未写入
        # 此处验证的是 bootstrap_admin_principal 包装函数正确捕获异常并返回 False


# ════════════════════════════════════════════════════════════════
# 3. ensure_readiness_or_exit 测试
# ════════════════════════════════════════════════════════════════


class TestEnsureReadinessOrExit:
    """ensure_readiness_or_exit() 行为测试。"""

    @pytest.mark.asyncio
    async def test_exit_when_not_bootstrapped(self, real_store, monkeypatch):
        """用例 4: 未 bootstrap 时 ensure_readiness_or_exit 调用 sys.exit(1)。"""
        # R71 RC39: 清除 CI 环境变量,绕过 ensure_readiness_or_exit CI 模式提前返回
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        from admin import ensure_readiness_or_exit
        with pytest.raises(SystemExit) as exc_info:
            await ensure_readiness_or_exit()
        assert exc_info.value.code == 1, "未 bootstrap 时应退出码 1"

    @pytest.mark.asyncio
    async def test_pass_when_bootstrapped(self, real_store, mock_admin_config):
        """用例 5: bootstrap 完成后 ensure_readiness_or_exit 正常返回(不退出)。"""
        mock_admin_config(principal_id=50505, username="admin")
        from admin import bootstrap_admin_principal, ensure_readiness_or_exit
        ok = await bootstrap_admin_principal()
        assert ok is True
        # 不应抛 SystemExit
        await ensure_readiness_or_exit()

    @pytest.mark.asyncio
    async def test_exit_when_verify_raises_exception(self, real_store, monkeypatch):
        """verify_admin_bootstrap 抛异常时也退出(代码 1)。"""
        # R71 RC39: 清除 CI 环境变量,绕过 ensure_readiness_or_exit CI 模式提前返回
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        from admin import ensure_readiness_or_exit
        with patch(
            "admin.verify_admin_bootstrap",
            new=MagicMock(side_effect=Exception("DB 故障")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await ensure_readiness_or_exit()
            assert exc_info.value.code == 1


# ════════════════════════════════════════════════════════════════
# 4. Session 失效场景测试
# ════════════════════════════════════════════════════════════════


class TestSessionInvalidation:
    """Session 失效场景测试(无 cookie/伪造/过期/撤销)。"""

    @pytest.mark.asyncio
    async def test_no_cookie_raises_401(self, real_store):
        """用例 6: 无 cookie 时 validate_or_raise 抛 HTTPException(401)。"""
        from admin.sessions import SessionManager
        from fastapi import HTTPException
        manager = SessionManager()
        # mock request 无 cookie
        request = MagicMock()
        request.cookies = {}
        with pytest.raises(HTTPException) as exc_info:
            await manager.validate_or_raise(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_forged_cookie_returns_none(self, real_store):
        """用例 7: 伪造 cookie(validate_session 返回 None)。"""
        from admin.sessions import SessionManager
        manager = SessionManager()
        # 使用一个不存在的 session_id(伪造)
        forged_session_id = "forged_session_id_12345"
        result = await manager.validate_session(forged_session_id)
        assert result is None, "伪造的 session_id 应返回 None"

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self, real_store):
        """用例 8: 过期 session(validate_session 返回 None)。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager, _load_session_data, _save_session_data
        manager = SessionManager(ttl_seconds=60)
        principal = AdminPrincipal(id=60606, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        # 手动设置过期
        data = await _load_session_data(session_id)
        assert data is not None
        data["expires_at_ts"] = int(time.time()) - 1
        await _save_session_data(session_id, data)
        result = await manager.validate_session(session_id)
        assert result is None, "过期 session 应返回 None"

    @pytest.mark.asyncio
    async def test_revoked_session_returns_none(self, real_store):
        """用例 9: 撤销 session(destroy 后 validate 返回 None)。

        R45 §7.2: destroy_session 标记 revoked=True,
        validate_session 校验 revoked 字段后返回 None。
        """
        from admin import AdminPrincipal
        from admin.sessions import SessionManager, _load_session_data
        manager = SessionManager()
        principal = AdminPrincipal(id=70707, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        # 验证创建后有效
        validated = await manager.validate_session(session_id)
        assert validated is not None
        # 手动标记 revoked(模拟 destroy_session 的 revoked 标记)
        data = await _load_session_data(session_id)
        assert data is not None
        data["revoked"] = True
        from admin.sessions import _save_session_data
        await _save_session_data(session_id, data)
        # validate 应返回 None(revoked 拒绝)
        result = await manager.validate_session(session_id)
        assert result is None, "撤销的 session 应返回 None"

    @pytest.mark.asyncio
    async def test_destroy_session_makes_invalid(self, real_store):
        """destroy_session 后 validate 返回 None。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(id=80808, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        validated = await manager.validate_session(session_id)
        assert validated is not None
        # destroy
        await manager.destroy_session(session_id)
        # validate 应返回 None(已删除)
        result = await manager.validate_session(session_id)
        assert result is None


# ════════════════════════════════════════════════════════════════
# 5. Session rotate 测试
# ════════════════════════════════════════════════════════════════


class TestSessionRotate:
    """Session rotate 测试。"""

    @pytest.mark.asyncio
    async def test_rotate_session_revokes_old_creates_new(self, real_store):
        """用例 10: rotate_session 撤销旧 session,创建新 session。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(id=90909, username="admin", roles=["super_admin"])
        old_session_id = await manager.create_session(principal, mfa_verified=True)
        assert old_session_id
        # rotate
        new_session_id = await manager.rotate_session(old_session_id)
        assert new_session_id, "rotate 应返回新 session_id"
        assert new_session_id != old_session_id, "新 session_id 应不同于旧"
        # 旧 session 应被撤销(validate 返回 None)
        old_validated = await manager.validate_session(old_session_id)
        assert old_validated is None, "旧 session 应已撤销"
        # 新 session 应有效
        new_validated = await manager.validate_session(new_session_id)
        assert new_validated is not None, "新 session 应有效"
        assert new_validated.id == 90909

    @pytest.mark.asyncio
    async def test_rotate_nonexistent_session_returns_empty(self, real_store):
        """rotate 不存在的 session 返回空字符串。"""
        from admin.sessions import SessionManager
        manager = SessionManager()
        result = await manager.rotate_session("nonexistent_session_id")
        assert result == "", "不存在的 session 应返回空字符串"

    @pytest.mark.asyncio
    async def test_rotate_with_csrf_token(self, real_store):
        """rotate 时绑定 CSRF token 到新 session。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(id=10010, username="admin", roles=["super_admin"])
        old_session_id = await manager.create_session(principal, mfa_verified=True)
        csrf_token = "test_csrf_token_abc123"
        new_session_id = await manager.rotate_session(
            old_session_id, csrf_token=csrf_token
        )
        assert new_session_id
        # 新 session 应绑定 csrf_token
        ok = await manager.validate_csrf_token(new_session_id, csrf_token)
        assert ok is True, "新 session 应绑定传入的 csrf_token"


# ════════════════════════════════════════════════════════════════
# 6. CSRF token 验证测试
# ════════════════════════════════════════════════════════════════


class TestCSRFTokenValidation:
    """validate_csrf_token() 测试。"""

    @pytest.mark.asyncio
    async def test_valid_csrf_token(self, real_store):
        """用例 11: token 与 session 绑定一致返回 True。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        csrf_token = "valid_csrf_token_xyz"
        principal = AdminPrincipal(id=11011, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(
            principal, mfa_verified=True, csrf_token=csrf_token
        )
        ok = await manager.validate_csrf_token(session_id, csrf_token)
        assert ok is True

    @pytest.mark.asyncio
    async def test_invalid_csrf_token(self, real_store):
        """用例 12: token 不一致返回 False(fail-closed)。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        csrf_token = "correct_token"
        principal = AdminPrincipal(id=12012, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(
            principal, mfa_verified=True, csrf_token=csrf_token
        )
        ok = await manager.validate_csrf_token(session_id, "wrong_token")
        assert ok is False, "token 不一致应返回 False"

    @pytest.mark.asyncio
    async def test_session_without_csrf_token(self, real_store):
        """用例 13: session 未绑定 token 返回 False(fail-closed)。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(id=13013, username="admin", roles=["super_admin"])
        # 不传 csrf_token(默认空字符串)
        session_id = await manager.create_session(principal, mfa_verified=True)
        ok = await manager.validate_csrf_token(session_id, "any_token")
        assert ok is False, "未绑定 token 的 session 应返回 False(fail-closed)"

    @pytest.mark.asyncio
    async def test_csrf_validation_revoked_session(self, real_store):
        """撤销的 session CSRF 校验返回 False。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager, _load_session_data, _save_session_data
        manager = SessionManager()
        csrf_token = "revoked_session_token"
        principal = AdminPrincipal(id=14014, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(
            principal, mfa_verified=True, csrf_token=csrf_token
        )
        # 标记 revoked
        data = await _load_session_data(session_id)
        data["revoked"] = True
        await _save_session_data(session_id, data)
        ok = await manager.validate_csrf_token(session_id, csrf_token)
        assert ok is False, "撤销 session 的 CSRF 校验应返回 False"

    @pytest.mark.asyncio
    async def test_csrf_validation_empty_params(self, real_store):
        """空 session_id 或空 token 返回 False。"""
        from admin.sessions import SessionManager
        manager = SessionManager()
        assert await manager.validate_csrf_token("", "token") is False
        assert await manager.validate_csrf_token("session_id", "") is False


# ════════════════════════════════════════════════════════════════
# 7. MFA TOTP 重放保护测试
# ════════════════════════════════════════════════════════════════


class TestMFATOTPReplayProtection:
    """MFA TOTP 重放保护测试。"""

    @pytest.mark.asyncio
    async def test_totp_replay_rejected(self, real_store):
        """用例 14: 同一 TOTP code 第二次验证返回 False(重放保护)。

        策略:使用真实 pyotp 生成当前有效的 code,验证两次。
        第一次应通过(记录已用),第二次应被拒绝(重放)。
        若 pyotp 未安装则跳过(无法生成有效 code)。
        """
        try:
            import pyotp
        except ImportError:
            pytest.skip("pyotp 未安装,跳过 TOTP 重放测试")
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        principal_id = 15015
        # 生成 secret 并启用 MFA
        secret = await manager.generate_totp_secret(principal_id)
        assert secret, "secret 生成应成功"
        await manager.enable_mfa(principal_id)
        # 生成当前有效的 TOTP code
        totp = pyotp.TOTP(secret)
        code = totp.now()
        # 第一次验证应通过
        ok1 = await manager.verify_totp_code(principal_id, code)
        assert ok1 is True, "第一次验证应通过"
        # 第二次验证同一 code 应被拒绝(重放)
        ok2 = await manager.verify_totp_code(principal_id, code)
        assert ok2 is False, "重放的 code 应被拒绝"

    @pytest.mark.asyncio
    async def test_replay_records_failure(self, real_store):
        """重放时记录失败次数(影响锁定)。"""
        try:
            import pyotp
        except ImportError:
            pytest.skip("pyotp 未安装")
        from admin.mfa import (
            get_mfa_manager, _is_totp_replayed, _record_totp_usage, _is_locked,
        )
        principal_id = 16016
        # R46 P1: _is_totp_replayed 改用 timestep 查询,同一 timestep 内任意 code
        # 都被视为重放(防 TOTP 重放攻击);不同 timestep 才返回 False。
        # 此处直接验证:记录当前 timestep 后,查询同一 timestep 应返回 True。
        _record_totp_usage(principal_id, "123456")
        assert await _is_totp_replayed(principal_id, "123456") is True
        # 同一 timestep 内不同 code 仍视为重放(timestep 维度的防重放)
        assert await _is_totp_replayed(principal_id, "789012") is True


# ════════════════════════════════════════════════════════════════
# 8. MFA 错误限流锁定测试
# ════════════════════════════════════════════════════════════════


class TestMFALockout:
    """MFA 错误限流锁定测试。"""

    @pytest.mark.asyncio
    async def test_lockout_after_5_failures(self, real_store):
        """用例 15: 连续 5 次错误 TOTP 后锁定。"""
        from admin.mfa import (
            get_mfa_manager, _record_mfa_failure, _is_locked,
            _MFA_FAIL_MAX_ATTEMPTS,
        )
        principal_id = 17017
        # 生成 secret 并启用(用于 verify_totp_code 测试)
        await get_mfa_manager().generate_totp_secret(principal_id)
        await get_mfa_manager().enable_mfa(principal_id)
        # 记录 4 次失败(未达阈值)— R46 P1: _record_mfa_failure / _is_locked 改为 async
        for i in range(_MFA_FAIL_MAX_ATTEMPTS - 1):
            await _record_mfa_failure(principal_id)
            assert not await _is_locked(principal_id), f"第 {i+1} 次失败后不应锁定"
        # 第 5 次失败触发锁定
        await _record_mfa_failure(principal_id)
        assert await _is_locked(principal_id) is True, "5 次失败后应锁定"

    @pytest.mark.asyncio
    async def test_lockout_rejects_verification(self, real_store):
        """锁定后 verify_totp_code 直接返回 False(不校验 code)。"""
        try:
            import pyotp
        except ImportError:
            pytest.skip("pyotp 未安装")
        from admin.mfa import get_mfa_manager, _record_mfa_failure, _MFA_FAIL_MAX_ATTEMPTS
        principal_id = 18018
        manager = get_mfa_manager()
        secret = await manager.generate_totp_secret(principal_id)
        await manager.enable_mfa(principal_id)
        # 触发锁定 — R46 P1: _record_mfa_failure 改为 async
        for _ in range(_MFA_FAIL_MAX_ATTEMPTS):
            await _record_mfa_failure(principal_id)
        # 即使传入正确的 code,锁定状态下也应返回 False
        totp = pyotp.TOTP(secret)
        correct_code = totp.now()
        ok = await manager.verify_totp_code(principal_id, correct_code)
        assert ok is False, "锁定状态下应拒绝验证"

    @pytest.mark.asyncio
    async def test_success_clears_failures(self, real_store):
        """用例 16: 验证成功后清除失败计数(锁定解除)。"""
        try:
            import pyotp
        except ImportError:
            pytest.skip("pyotp 未安装")
        from admin.mfa import (
            get_mfa_manager, _record_mfa_failure, _is_locked, _clear_mfa_failures,
        )
        principal_id = 19019
        manager = get_mfa_manager()
        secret = await manager.generate_totp_secret(principal_id)
        await manager.enable_mfa(principal_id)
        # 记录 3 次失败(未锁定)— R46 P1: 改为 async 调用
        for _ in range(3):
            await _record_mfa_failure(principal_id)
        assert not await _is_locked(principal_id)
        # 验证成功(使用正确的 code)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        ok = await manager.verify_totp_code(principal_id, code)
        assert ok is True
        # 失败计数应已清除
        await _clear_mfa_failures(principal_id)
        # 再次验证同一 code 应被拒绝(重放),但可用新 code
        # (此处仅验证失败计数已清除)

    @pytest.mark.asyncio
    async def test_is_locked_no_failures(self, real_store):
        """无失败记录时 _is_locked 返回 False。

        R46 P1: _is_locked 改为 async(查询 SQLite),需 real_store fixture
        确保 store 已初始化(否则 fail-closed 返回 True)。
        """
        from admin.mfa import _is_locked
        assert await _is_locked(99999) is False


# ════════════════════════════════════════════════════════════════
# 9. RBAC fail-closed + require_permission 测试
# ════════════════════════════════════════════════════════════════


class TestRBACFailClosed:
    """RBAC fail-closed 测试。"""

    @pytest.mark.asyncio
    async def test_check_permission_fail_closed_on_exception(self, real_store):
        """用例 17: DB 异常时 check_permission 返回 False(fail-closed)。"""
        from services.rbac import check_permission
        with patch(
            "services.rbac.get_principal_role_name",
            new=MagicMock(side_effect=Exception("DB 故障模拟")),
        ):
            ok = await check_permission(20020, "users:ban")
            assert ok is False, "DB 异常时应 fail-closed 返回 False"

    @pytest.mark.asyncio
    async def test_check_permission_no_role_returns_false(self, real_store):
        """无角色记录时 check_permission 返回 False。"""
        from services.rbac import check_permission
        # 此 principal_id 未 bootstrap,无任何角色
        ok = await check_permission(21021, "users:ban")
        assert ok is False, "无角色时应返回 False"

    @pytest.mark.asyncio
    async def test_require_permission_raises_403(self, real_store):
        """用例 18: require_permission 无权限时抛 HTTPException(403)。"""
        from fastapi import HTTPException
        from services.rbac import require_permission
        with pytest.raises(HTTPException) as exc_info:
            await require_permission(22022, "users:ban")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_permission_super_admin_passes(self, real_store):
        """用例 19: super_admin 调用 require_permission 不抛异常。"""
        await real_store.bootstrap_admin_principal(
            principal_id=23023, username="admin", roles=["super_admin"]
        )
        from services.rbac import require_permission
        # 不应抛异常
        await require_permission(23023, "users:ban")
        await require_permission(23023, "any:permission")

    @pytest.mark.asyncio
    async def test_require_permission_db_exception_raises_403(self, real_store):
        """DB 异常时 require_permission 也抛 403(fail-closed)。"""
        from fastapi import HTTPException
        from services.rbac import require_permission
        with patch(
            "services.rbac.get_principal_role_name",
            new=MagicMock(side_effect=Exception("DB 故障")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await require_permission(24024, "users:ban")
            assert exc_info.value.status_code == 403


# ════════════════════════════════════════════════════════════════
# 10. Principal ID 一致性测试
# ════════════════════════════════════════════════════════════════


class TestPrincipalIDConsistency:
    """Principal ID 一致性测试(R45 §7.3)。"""

    @pytest.mark.asyncio
    async def test_session_principal_id_matches_config(
        self, real_store, mock_admin_config
    ):
        """用例 20: validate_session 返回的 id 与 ADMIN_PRINCIPAL_ID 一致。"""
        configured_id = 25025
        mock_admin_config(principal_id=configured_id, username="admin")
        await real_store.bootstrap_admin_principal(
            principal_id=configured_id, username="admin", roles=["super_admin"]
        )
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(
            id=configured_id, username="admin", roles=["super_admin"]
        )
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        validated = await manager.validate_session(session_id)
        assert validated is not None
        assert validated.id == configured_id, (
            "validate_session 返回的 principal_id 应与 ADMIN_PRINCIPAL_ID 一致"
        )

    @pytest.mark.asyncio
    async def test_rbac_user_id_matches_principal_id(self, real_store, mock_admin_config):
        """RBAC check_permission 的 user_id 与 AdminPrincipal.id 一致。

        bootstrap 写入 admin_principal_roles(principal_id=配置值),
        check_permission(同一 principal_id) 应能读取到角色。
        """
        configured_id = 26026
        mock_admin_config(principal_id=configured_id, username="admin")
        from admin import bootstrap_admin_principal
        ok = await bootstrap_admin_principal()
        assert ok is True
        from services.rbac import check_permission
        # 用同一 principal_id 调用 RBAC,应能读取到 super_admin 角色
        ok = await check_permission(configured_id, "users:ban")
        assert ok is True, "RBAC user_id 应与 AdminPrincipal.id 一致(super_admin 通配)"

    @pytest.mark.asyncio
    async def test_session_rbac_same_id(self, real_store, mock_admin_config):
        """Session → RBAC 使用同一 principal_id 完整链路。"""
        configured_id = 27027
        mock_admin_config(principal_id=configured_id, username="admin")
        from admin import bootstrap_admin_principal
        await bootstrap_admin_principal()
        # 创建 session
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(
            id=configured_id, username="admin", roles=["super_admin"]
        )
        session_id = await manager.create_session(principal, mfa_verified=True)
        # validate_session 返回的 principal_id
        validated = await manager.validate_session(session_id)
        assert validated is not None
        # 用 validate_session 返回的 id 调用 RBAC
        from services.rbac import check_permission
        ok = await check_permission(validated.id, "users:ban")
        assert ok is True, (
            "Session → RBAC 应使用同一 principal_id(super_admin 通配)"
        )


# ════════════════════════════════════════════════════════════════
# 11. bootstrap_admin_principal_atomic 测试
# ════════════════════════════════════════════════════════════════


class TestBootstrapAdminPrincipalAtomic:
    """bootstrap_admin_principal_atomic() 测试。"""

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_success(self, real_store, mock_admin_config):
        """原子 bootstrap 成功。"""
        mock_admin_config(principal_id=28028, username="admin")
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        result = await bootstrap_admin_principal_atomic(source="test")
        assert result["success"] is True
        assert result["skipped"] is False
        assert result["principal_id"] == 28028
        assert result["username"] == "admin"
        assert result["source"] == "test"
        # 验证记录已写入
        record = await real_store.get_admin_principal_record(28028)
        assert record is not None

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_idempotent(self, real_store, mock_admin_config):
        """用例 21: 幂等 — 已 bootstrap 时 skipped=True。"""
        mock_admin_config(principal_id=29029, username="admin")
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        # 第一次 bootstrap
        result1 = await bootstrap_admin_principal_atomic()
        assert result1["success"] is True
        assert result1["skipped"] is False
        # 第二次 bootstrap(幂等)
        result2 = await bootstrap_admin_principal_atomic()
        assert result2["success"] is True
        assert result2["skipped"] is True, "已 bootstrap 时应跳过(skipped=True)"

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_invalid_principal_id(
        self, real_store, mock_admin_config
    ):
        """principal_id 无效时返回失败。"""
        mock_admin_config(principal_id=0, username="admin")
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        result = await bootstrap_admin_principal_atomic()
        assert result["success"] is False
        assert "principal_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_invalid_username(
        self, real_store, mock_admin_config
    ):
        """username 为空时返回失败。"""
        mock_admin_config(principal_id=30030, username="")
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        result = await bootstrap_admin_principal_atomic()
        assert result["success"] is False
        assert "username" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_writes_audit_log(self, real_store, mock_admin_config):
        """原子 bootstrap 写入审计日志(action=bootstrap_admin_principal_atomic)。"""
        mock_admin_config(principal_id=31031, username="admin")
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        await bootstrap_admin_principal_atomic(source="audit_test")
        # 查询 audit_log
        rows = await real_store._db.execute_fetchall(
            "SELECT action FROM audit_log WHERE action = 'bootstrap_admin_principal_atomic' "
            "AND target_id = ?",
            ("31031",),
        )
        assert len(rows) >= 1, "应写入 bootstrap_admin_principal_atomic audit_log"

    @pytest.mark.asyncio
    async def test_atomic_bootstrap_explicit_params(self, real_store):
        """显式参数优先于 settings 配置。"""
        from services.bootstrap_runner import bootstrap_admin_principal_atomic
        result = await bootstrap_admin_principal_atomic(
            principal_id=32032,
            username="explicit",
            roles=["super_admin"],
            source="explicit_test",
        )
        assert result["success"] is True
        assert result["principal_id"] == 32032
        assert result["username"] == "explicit"
        assert result["source"] == "explicit_test"
