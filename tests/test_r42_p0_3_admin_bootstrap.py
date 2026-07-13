"""R42 P0-3: AdminPrincipal 与 RBAC bootstrap 映射证明测试。

测试范围:
- database/cache_store.py: bootstrap_admin_principal() / get_admin_principal_record()
  / list_admin_principal_roles()
- admin/__init__.py: AdminPrincipal.from_persistent_record() / verify_admin_bootstrap()
  / require_readiness()
- admin/sessions.py: validate_session() 从持久化身份表读取
- services/rbac.py: check_permission() 优先从 admin_principal_roles 表查询 + fallback
- config/settings.py: ADMIN_PRINCIPAL_ID / USERNAME / BOOTSTRAP_ROLES 配置项

测试用例(15+):
1.  bootstrap_admin_principal() 创建 admin_principals 记录
2.  bootstrap 幂等(重复调用不报错)
3.  bootstrap 写 audit_log
4.  verify_admin_bootstrap() 在 principal 存在且 super_admin 时返回 True
5.  verify_admin_bootstrap() 在 principal 不存在时返回 False
6.  verify_admin_bootstrap() 在 principal 无 super_admin 角色时返回 False
7.  ADMIN_PRINCIPAL_ID=0 时跳过 bootstrap(返回 False)
8.  ADMIN_PRINCIPAL_ID > 0 但 admin_principals 表无记录时,bootstrap 创建记录
9.  rbac.check_permission 从 admin_principal_roles 表读取(有 super_admin → 通配)
10. rbac.check_permission fallback 到默认角色(无 admin_principal_roles 记录)
11. rbac.check_permission 异常时 fail-closed(返回 False)
12. 启动 readiness 在 verify 失败时抛 RuntimeError
13. AdminPrincipal.from_persistent_record 正确构造对象
14. validate_session 返回的 principal_id 与 ADMIN_PRINCIPAL_ID 一致
15. 整个 bootstrap 在单 transaction 中(失败回滚)

测试策略:
- 使用临时 SQLite 数据库(real_store fixture)隔离生产数据
- 使用 monkeypatch 设置 settings.ADMIN_PRINCIPAL_ID 等配置
- AST 检查 + 行为测试结合
"""
from __future__ import annotations

import ast
import shutil
import tempfile
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
RBAC_FILE = REPO_ROOT / "services" / "rbac.py"
SETTINGS_FILE = REPO_ROOT / "config" / "settings.py"
CACHE_STORE_FILE = REPO_ROOT / "database" / "cache_store.py"


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


def _get_classes(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite 数据库
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    设置 _cs_module._store 使 get_cache_store() 返回测试 store。
    同时初始化 rbac_roles 默认角色(super_admin 等)供 check_permission 测试。
    """
    tmpdir = tempfile.mkdtemp(prefix="r42_p0_3_test_")
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
            pass  # 角色初始化失败不阻塞测试(部分用例不需要角色)
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_admin_principal_id(monkeypatch):
    """设置 settings.ADMIN_PRINCIPAL_ID 为指定值。"""
    import config

    def _set(value: int):
        monkeypatch.setattr(config.settings, "ADMIN_PRINCIPAL_ID", value, raising=False)
        monkeypatch.setattr(
            config.settings, "ADMIN_PRINCIPAL_USERNAME", "admin", raising=False
        )
        monkeypatch.setattr(
            config.settings, "ADMIN_PRINCIPAL_BOOTSTRAP_ROLES", "super_admin", raising=False
        )

    return _set


# ════════════════════════════════════════════════════════════════
# 1. 静态检查:配置项 + DDL + 函数定义
# ════════════════════════════════════════════════════════════════


class TestStaticChecks:
    """静态检查:配置项、DDL、函数定义存在性。"""

    def test_settings_has_admin_principal_id(self):
        """config/settings.py 应定义 ADMIN_PRINCIPAL_ID 配置项。"""
        source = SETTINGS_FILE.read_text(encoding="utf-8")
        assert "ADMIN_PRINCIPAL_ID" in source, "应定义 ADMIN_PRINCIPAL_ID 配置项"
        assert "ADMIN_PRINCIPAL_USERNAME" in source, "应定义 ADMIN_PRINCIPAL_USERNAME 配置项"
        assert "ADMIN_PRINCIPAL_BOOTSTRAP_ROLES" in source, "应定义 ADMIN_PRINCIPAL_BOOTSTRAP_ROLES"

    def test_cache_store_has_admin_principals_ddl(self):
        """cache_store.py 应包含 admin_principals 表 DDL。"""
        source = CACHE_STORE_FILE.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS admin_principals" in source, (
            "应创建 admin_principals 表"
        )
        assert "CREATE TABLE IF NOT EXISTS admin_principal_roles" in source, (
            "应创建 admin_principal_roles 表"
        )

    def test_admin_init_has_from_persistent_record(self):
        """admin/__init__.py 的 AdminPrincipal 应有 from_persistent_record 类方法。"""
        tree = _parse_ast(ADMIN_INIT)
        assert tree is not None
        sync_funcs = _get_sync_funcs(tree)
        assert "from_persistent_record" in sync_funcs, (
            "AdminPrincipal 应定义 from_persistent_record 类方法"
        )

    def test_admin_init_has_verify_admin_bootstrap(self):
        """admin/__init__.py 应定义 verify_admin_bootstrap async 函数。"""
        tree = _parse_ast(ADMIN_INIT)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "verify_admin_bootstrap" in async_funcs, (
            "应定义 verify_admin_bootstrap async 函数"
        )
        assert "require_readiness" in async_funcs, "应定义 require_readiness async 函数"

    def test_rbac_has_principal_role_functions(self):
        """services/rbac.py 应定义 get_principal_role_name / list_principal_permissions。"""
        tree = _parse_ast(RBAC_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "get_principal_role_name" in async_funcs, (
            "应定义 get_principal_role_name async 函数"
        )
        assert "list_principal_permissions" in async_funcs, (
            "应定义 list_principal_permissions async 函数"
        )

    def test_cache_store_has_bootstrap_admin_principal(self):
        """cache_store.py 应定义 bootstrap_admin_principal async 方法。"""
        tree = _parse_ast(CACHE_STORE_FILE)
        assert tree is not None
        async_funcs = _get_async_funcs(tree)
        assert "bootstrap_admin_principal" in async_funcs, (
            "应定义 bootstrap_admin_principal async 方法"
        )
        assert "get_admin_principal_record" in async_funcs
        assert "list_admin_principal_roles" in async_funcs


# ════════════════════════════════════════════════════════════════
# 2. bootstrap_admin_principal() 行为测试
# ════════════════════════════════════════════════════════════════


class TestBootstrapAdminPrincipal:
    """bootstrap_admin_principal() 行为测试。"""

    @pytest.mark.asyncio
    async def test_bootstrap_creates_admin_principals_record(self, real_store):
        """用例 1: bootstrap 创建 admin_principals 记录。"""
        ok = await real_store.bootstrap_admin_principal(
            principal_id=1001, username="admin", roles=["super_admin"]
        )
        assert ok is True, "bootstrap 应返回 True"

        record = await real_store.get_admin_principal_record(1001)
        assert record is not None, "记录应存在"
        assert record["id"] == 1001
        assert record["username"] == "admin"
        assert record["is_active"] is True
        assert "super_admin" in record["roles"]

    @pytest.mark.asyncio
    async def test_bootstrap_is_idempotent(self, real_store):
        """用例 2: bootstrap 幂等(重复调用不报错)。"""
        # 第一次 bootstrap
        ok1 = await real_store.bootstrap_admin_principal(
            principal_id=2002, username="admin1", roles=["super_admin"]
        )
        assert ok1 is True
        # 第二次 bootstrap(幂等)
        ok2 = await real_store.bootstrap_admin_principal(
            principal_id=2002, username="admin1", roles=["super_admin"]
        )
        assert ok2 is True, "重复 bootstrap 不应报错"

        # 验证记录只有一条
        record = await real_store.get_admin_principal_record(2002)
        assert record is not None
        assert record["id"] == 2002

        # 验证角色映射不重复
        roles = await real_store.list_admin_principal_roles(2002)
        assert roles == ["super_admin"], "角色应不重复"

    @pytest.mark.asyncio
    async def test_bootstrap_writes_audit_log(self, real_store):
        """用例 3: bootstrap 写 audit_log(action=bootstrap_admin_principal)。"""
        await real_store.bootstrap_admin_principal(
            principal_id=3003, username="audit_admin", roles=["super_admin"]
        )
        # 查询 audit_log
        rows = await real_store._db.execute_fetchall(
            "SELECT action, target_id FROM audit_log WHERE action = 'bootstrap_admin_principal' "
            "AND target_id = ?",
            ("3003",),
        )
        assert len(rows) >= 1, "应写入 bootstrap_admin_principal audit_log"
        assert rows[0][0] == "bootstrap_admin_principal"

    @pytest.mark.asyncio
    async def test_bootstrap_rejects_invalid_principal_id(self, real_store):
        """用例 7: ADMIN_PRINCIPAL_ID=0 时 bootstrap 返回 False(拒绝)。"""
        ok = await real_store.bootstrap_admin_principal(
            principal_id=0, username="admin", roles=["super_admin"]
        )
        assert ok is False, "principal_id=0 应返回 False"

        ok_neg = await real_store.bootstrap_admin_principal(
            principal_id=-1, username="admin", roles=["super_admin"]
        )
        assert ok_neg is False, "principal_id<0 应返回 False"

    @pytest.mark.asyncio
    async def test_bootstrap_creates_record_when_not_exists(self, real_store):
        """用例 8: ADMIN_PRINCIPAL_ID > 0 但表无记录时,bootstrap 创建记录。"""
        # 确认表无记录
        record_before = await real_store.get_admin_principal_record(4004)
        assert record_before is None
        # bootstrap
        ok = await real_store.bootstrap_admin_principal(
            principal_id=4004, username="new_admin", roles=["super_admin"]
        )
        assert ok is True
        record_after = await real_store.get_admin_principal_record(4004)
        assert record_after is not None, "bootstrap 后记录应存在"
        assert record_after["username"] == "new_admin"

    @pytest.mark.asyncio
    async def test_bootstrap_transaction_rollback_on_failure(self, real_store):
        """用例 15: bootstrap 在单 transaction 中,失败时回滚。

        通过 mock transaction 抛异常验证回滚:bootstrap 失败时不应写入任何记录。
        """
        # 使用 patch 让 transaction 抛异常
        original_transaction = real_store.transaction

        class _FakeTxError:
            def __init__(self):
                pass

            async def __aenter__(self):
                raise RuntimeError("模拟事务失败")

            async def __aexit__(self, *args):
                return False

        # 替换 transaction 为抛异常的版本
        with patch.object(real_store, "transaction", return_value=_FakeTxError()):
            ok = await real_store.bootstrap_admin_principal(
                principal_id=5005, username="rollback_admin", roles=["super_admin"]
            )
        assert ok is False, "事务失败应返回 False"
        # 验证记录未写入(回滚)
        record = await real_store.get_admin_principal_record(5005)
        assert record is None, "事务失败后不应有记录"


# ════════════════════════════════════════════════════════════════
# 3. verify_admin_bootstrap() + require_readiness() 测试
# ════════════════════════════════════════════════════════════════


class TestVerifyAdminBootstrap:
    """verify_admin_bootstrap() 行为测试。"""

    @pytest.mark.asyncio
    async def test_verify_returns_true_when_super_admin_exists(self, real_store, mock_admin_principal_id):
        """用例 4: principal 存在且 super_admin 时返回 True。"""
        mock_admin_principal_id(6006)
        await real_store.bootstrap_admin_principal(
            principal_id=6006, username="admin", roles=["super_admin"]
        )
        # 延迟导入(测试环境需在 store 初始化后导入)
        from admin import verify_admin_bootstrap
        ok = await verify_admin_bootstrap()
        assert ok is True, "有 super_admin 时应返回 True"

    @pytest.mark.asyncio
    async def test_verify_returns_false_when_no_principals(self, real_store):
        """用例 5: 无 admin_principals 记录时返回 False。"""
        from admin import verify_admin_bootstrap
        ok = await verify_admin_bootstrap()
        assert ok is False, "无记录时应返回 False"

    @pytest.mark.asyncio
    async def test_verify_returns_false_when_no_super_admin_role(self, real_store):
        """用例 6: principal 无 super_admin 角色时返回 False。"""
        # bootstrap 一个只有 ops 角色的 principal
        await real_store.bootstrap_admin_principal(
            principal_id=7007, username="ops_admin", roles=["ops"]
        )
        from admin import verify_admin_bootstrap
        ok = await verify_admin_bootstrap()
        assert ok is False, "无 super_admin 角色时应返回 False"

    @pytest.mark.asyncio
    async def test_verify_returns_false_when_configured_id_not_exists(
        self, real_store, mock_admin_principal_id
    ):
        """ADMIN_PRINCIPAL_ID > 0 但对应记录不存在时返回 False。"""
        mock_admin_principal_id(99999)  # 不存在的 ID
        # 先 bootstrap 另一个 principal(有 super_admin)以满足"至少一条 super_admin"条件
        await real_store.bootstrap_admin_principal(
            principal_id=8008, username="other_admin", roles=["super_admin"]
        )
        from admin import verify_admin_bootstrap
        ok = await verify_admin_bootstrap()
        assert ok is False, "ADMIN_PRINCIPAL_ID 对应记录不存在时应返回 False"

    @pytest.mark.asyncio
    async def test_require_readiness_raises_runtime_error_on_failure(self, real_store):
        """用例 12: require_readiness 在 verify 失败时抛 RuntimeError。"""
        from admin import require_readiness
        with pytest.raises(RuntimeError, match="admin bootstrap not verified"):
            await require_readiness()

    @pytest.mark.asyncio
    async def test_require_readiness_passes_when_bootstrap_done(
        self, real_store, mock_admin_principal_id
    ):
        """require_readiness 在 bootstrap 完成后正常通过(不抛异常)。"""
        mock_admin_principal_id(9009)
        await real_store.bootstrap_admin_principal(
            principal_id=9009, username="admin", roles=["super_admin"]
        )
        from admin import require_readiness
        # 不抛异常即通过
        await require_readiness()


# ════════════════════════════════════════════════════════════════
# 4. AdminPrincipal.from_persistent_record 测试
# ════════════════════════════════════════════════════════════════


class TestFromPersistentRecord:
    """AdminPrincipal.from_persistent_record 测试。"""

    def test_from_persistent_record_constructs_correctly(self):
        """用例 13: from_persistent_record 正确构造 AdminPrincipal 对象。"""
        from admin import AdminPrincipal
        record = {
            "id": 12345,
            "username": "test_admin",
            "roles": ["super_admin", "ops"],
            "is_active": True,
            "password_hash": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        principal = AdminPrincipal.from_persistent_record(record)
        assert principal.id == 12345
        assert principal.username == "test_admin"
        assert "super_admin" in principal.roles
        assert "ops" in principal.roles

    def test_from_persistent_record_empty_record(self):
        """from_persistent_record 空记录时返回 id=0 的占位对象。"""
        from admin import AdminPrincipal
        principal = AdminPrincipal.from_persistent_record(None)
        assert principal.id == 0
        assert principal.username == ""
        assert principal.roles == []

        principal2 = AdminPrincipal.from_persistent_record({})
        assert principal2.id == 0

    def test_from_persistent_record_corrupt_data(self):
        """from_persistent_record 数据损坏时返回占位对象(不抛异常)。"""
        from admin import AdminPrincipal
        record = {"id": "not_a_number", "username": "bad"}
        principal = AdminPrincipal.from_persistent_record(record)
        assert principal.id == 0, "损坏数据应返回 id=0"


# ════════════════════════════════════════════════════════════════
# 5. rbac.check_permission 测试
# ════════════════════════════════════════════════════════════════


class TestCheckPermission:
    """rbac.check_permission 行为测试。"""

    @pytest.mark.asyncio
    async def test_check_permission_reads_from_admin_principal_roles(self, real_store):
        """用例 9: check_permission 从 admin_principal_roles 表读取(super_admin 通配)。"""
        # bootstrap 一个 super_admin principal
        await real_store.bootstrap_admin_principal(
            principal_id=11001, username="rbac_admin", roles=["super_admin"]
        )
        from services.rbac import check_permission
        # super_admin 应有所有权限(通配 *)
        ok = await check_permission(11001, "users:ban")
        assert ok is True, "super_admin 应有 users:ban 权限"
        ok2 = await check_permission(11001, "any:permission")
        assert ok2 is True, "super_admin 应有通配权限"

    @pytest.mark.asyncio
    async def test_check_permission_fallback_to_default_roles(self, real_store):
        """用例 10: check_permission fallback 到默认角色映射。

        场景: principal 无 admin_principal_roles 记录,也无 rbac_user_roles 记录,
        check_permission 应 fallback 到 _DEFAULT_ROLE_PERMISSIONS。
        """
        # 此 principal 未 bootstrap,无任何角色映射
        from services.rbac import check_permission
        # 无角色 → 无 admin_principal_roles → 无 rbac_user_roles → fallback 默认空 → False
        ok = await check_permission(12002, "users:ban")
        assert ok is False, "无角色记录时应返回 False(fail-closed)"

    @pytest.mark.asyncio
    async def test_check_permission_fail_closed_on_exception(self, real_store):
        """用例 11: check_permission 异常时 fail-closed 返回 False。"""
        # 通过 mock get_principal_role_name 抛异常
        with patch(
            "services.rbac.get_principal_role_name",
            new=MagicMock(side_effect=Exception("DB 故障模拟")),
        ):
            from services.rbac import check_permission
            ok = await check_permission(13003, "users:ban")
            assert ok is False, "异常时应 fail-closed 返回 False"

    @pytest.mark.asyncio
    async def test_check_permission_ops_role_specific_permission(self, real_store):
        """ops 角色有 maintenance 权限但无 users:ban 权限。"""
        await real_store.bootstrap_admin_principal(
            principal_id=14004, username="ops_user", roles=["ops"]
        )
        from services.rbac import check_permission
        # ops 有 maintenance:enable 权限(在 _DEFAULT_ROLE_PERMISSIONS 中)
        ok_maint = await check_permission(14004, "maintenance:enable")
        assert ok_maint is True, "ops 应有 maintenance:enable 权限"
        # ops 无 users:ban 权限
        ok_ban = await check_permission(14004, "users:ban")
        assert ok_ban is False, "ops 不应有 users:ban 权限"


# ════════════════════════════════════════════════════════════════
# 6. validate_session 集成测试
# ════════════════════════════════════════════════════════════════


class TestValidateSessionWithPersistentIdentity:
    """validate_session 从持久化身份表读取的集成测试。"""

    @pytest.mark.asyncio
    async def test_validate_session_returns_principal_id_matching_config(
        self, real_store, mock_admin_principal_id
    ):
        """用例 14: validate_session 返回的 principal_id 与 ADMIN_PRINCIPAL_ID 一致。"""
        configured_id = 15005
        mock_admin_principal_id(configured_id)
        # bootstrap 持久化身份
        await real_store.bootstrap_admin_principal(
            principal_id=configured_id, username="admin", roles=["super_admin"]
        )
        # 创建 session
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        principal = AdminPrincipal(
            id=configured_id, username="admin", roles=["super_admin"]
        )
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id, "session 创建应成功"

        # validate_session 应返回从持久化表读取的 principal
        validated = await manager.validate_session(session_id)
        assert validated is not None, "session 应有效"
        assert validated.id == configured_id, (
            "validate_session 返回的 principal_id 应与 ADMIN_PRINCIPAL_ID 一致"
        )
        assert validated.username == "admin"

    @pytest.mark.asyncio
    async def test_validate_session_fallback_when_no_persistent_record(self, real_store):
        """无持久化记录时 fallback 到 session 数据。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        manager = SessionManager()
        # 使用一个未 bootstrap 的 principal_id
        principal = AdminPrincipal(
            id=16006, username="fallback_admin", roles=["super_admin"]
        )
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        validated = await manager.validate_session(session_id)
        assert validated is not None, "fallback 到 session 数据应有效"
        assert validated.id == 16006
        assert validated.username == "fallback_admin"

    @pytest.mark.asyncio
    async def test_validate_session_returns_none_for_expired(self, real_store):
        """过期 session 返回 None。"""
        from admin import AdminPrincipal
        from admin.sessions import SessionManager
        # 创建 TTL=60s 的 manager
        manager = SessionManager(ttl_seconds=60)
        principal = AdminPrincipal(id=17007, username="admin", roles=["super_admin"])
        session_id = await manager.create_session(principal, mfa_verified=True)
        assert session_id
        # 手动将 session 数据中的 expires_at_ts 改为过去时间
        from admin.sessions import _load_session_data, _save_session_data
        import time
        data = await _load_session_data(session_id)
        assert data is not None
        data["expires_at_ts"] = int(time.time()) - 1  # 已过期
        await _save_session_data(session_id, data)
        # validate 应返回 None
        validated = await manager.validate_session(session_id)
        assert validated is None, "过期 session 应返回 None"
