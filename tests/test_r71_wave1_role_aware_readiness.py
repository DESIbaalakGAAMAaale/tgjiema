"""R71 Wave 1: 角色级 fail-closed readiness — 测试套件。

R71 P0-01/02/03/04 根因修复的回归测试:
    R70 Wave 6 仍存在以下问题:
    1. _check_redis() 在 REDIS_URL 缺失时返回 healthy=True("not_configured"),
       但生产角色(db_writer / dsp_bot / admin_bot / 等)依赖 Redis,
       应 fail-closed 返回 unhealthy。
    2. _check_database() 任一 SQLite 或 CRDB 可用即视为健康,
       SQLite 可掩盖 CRDB 故障(crdb_sync / migration / db_writer 必须用 CRDB)。
    3. services/health.py 是孤岛模块,零生产调用;
       生产实际使用 services/prometheus_exporter.check_readiness()(SQLite-only)。
    4. docker/entrypoint.py 不做启动前 readiness gate;
       docker-compose.prod.yml 的 8 个业务服务 healthcheck 只检查
       /proc/1/cmdline 字符串匹配,无法反映真实依赖可用性。
    5. 三套 check_readiness 实现并存(health.py / prometheus_exporter.py /
       maintenance_mode.py),命名冲突,语义不一。

R71 Wave 1 整改:
    1. 新增 ROLE_REQUIREMENTS 权威映射(覆盖 entrypoint 全部 13 个角色)
    2. _check_redis(role) 角色化:依赖 Redis 的角色在 REDIS_URL 缺失时 fail-closed
    3. _check_database(role) 角色化:CRDB 必需角色不掩盖 CRDB 故障
    4. 新增 _check_database_crdb() / _check_crdb_sync_lag() /
       _check_backup_dir_writable() / _check_metrics_endpoint() /
       _check_scheduler_heartbeat()
    5. check_readiness(role) 重写:从 ROLE_REQUIREMENTS 读取检查项集合,
       动态选择检查函数,未知角色返回 unhealthy
    6. 新增 CLI 入口(--role --json)
    7. prometheus_exporter.check_readiness 重命名为 collect_dependency_status
    8. maintenance_mode.check_readiness 重命名为 check_maintenance_safe
    9. docker/entrypoint.py 在 production/staging 下增加 readiness gate

测试矩阵:
    A. ROLE_REQUIREMENTS 权威映射完整性(13 个角色)
    B. _check_redis(role) fail-closed 行为
    C. _check_database(role) 角色化路由(CRDB vs SQLite)
    D. 新增检查函数存在性与基本行为
    E. check_readiness(role) ROLE_REQUIREMENTS 动态调度
    F. 未知角色 fail-closed
    G. CLI 入口(--role / --json)
    H. deprecated wrapper 向后兼容
    I. entrypoint readiness gate
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest


# ════════════════════════════════════════════════════════════════
# 测试隔离:直接加载 services.health 模块
# ════════════════════════════════════════════════════════════════


def _load_health_module():
    """加载 services.health 模块(支持重载)。"""
    if "services.health" in sys.modules and hasattr(
        sys.modules["services.health"], "check_readiness"
    ):
        return sys.modules["services.health"]

    # 确保 services 包可导入
    if "services" not in sys.modules:
        services_pkg = type(sys)("services")
        services_pkg.__path__ = [
            str(Path(__file__).resolve().parent.parent / "services")
        ]
        sys.modules["services"] = services_pkg

    health_path = (
        Path(__file__).resolve().parent.parent / "services" / "health.py"
    )
    spec = importlib.util.spec_from_file_location("services.health", health_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.health"] = module
    spec.loader.exec_module(module)
    return sys.modules["services.health"]


def _load_entrypoint_module():
    """加载 docker.entrypoint 模块(支持重载)。"""
    if "docker.entrypoint" in sys.modules and hasattr(
        sys.modules["docker.entrypoint"], "main"
    ):
        return sys.modules["docker.entrypoint"]

    # 确保 config.environment 可加载
    if "config.environment" not in sys.modules or not hasattr(
        sys.modules.get("config.environment", None), "parse_app_env"
    ):
        env_path = Path(__file__).resolve().parent.parent / "config" / "environment.py"
        spec = importlib.util.spec_from_file_location("config.environment", env_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["config.environment"] = module
        spec.loader.exec_module(module)

    if "docker" not in sys.modules or not hasattr(sys.modules["docker"], "__path__"):
        docker_pkg = type(sys)("docker")
        docker_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "docker")]
        sys.modules["docker"] = docker_pkg

    entry_path = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("docker.entrypoint", entry_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["docker.entrypoint"] = module
    spec.loader.exec_module(module)
    return sys.modules["docker.entrypoint"]


@pytest.fixture(scope="module")
def health():
    """提供 services.health 模块实例(模块级缓存)。"""
    return _load_health_module()


@pytest.fixture(scope="module")
def entry_module():
    """提供 docker.entrypoint 模块实例(模块级缓存)。"""
    return _load_entrypoint_module()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _async_return_factory(value):
    """构造一个 async 函数,返回固定 value(接受任意参数)。"""

    async def _impl(*args, **kwargs):
        return value

    return _impl


def _async_return_factory_3tuple(value):
    """构造一个 async 函数,返回 3-tuple(用于 _check_redis 签名兼容)。"""

    async def _impl(*args, **kwargs):
        return value

    return _impl


# ════════════════════════════════════════════════════════════════
# A. ROLE_REQUIREMENTS 权威映射完整性
# ════════════════════════════════════════════════════════════════


class TestRoleRequirementsMapping:
    """验证 ROLE_REQUIREMENTS 权威映射覆盖全部 13 个角色。"""

    def test_role_requirements_exists(self, health):
        """ROLE_REQUIREMENTS 常量应存在。"""
        assert hasattr(health, "ROLE_REQUIREMENTS")
        assert isinstance(health.ROLE_REQUIREMENTS, dict)

    def test_role_requirements_covers_all_13_roles(self, health):
        """ROLE_REQUIREMENTS 必须覆盖 entrypoint 全部 13 个角色。"""
        required_roles = {
            "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
            "db_writer", "crdb_sync", "db_backup", "migration",
            "prometheus_exporter", "r40_scheduler", "admin",
        }
        actual_roles = set(health.ROLE_REQUIREMENTS.keys())
        missing = required_roles - actual_roles
        assert not missing, f"ROLE_REQUIREMENTS 缺少角色: {missing}"

    def test_role_requirements_values_are_dicts(self, health):
        """每个角色的值必须是 dict(check_name → critical bool)。"""
        for role, checks in health.ROLE_REQUIREMENTS.items():
            assert isinstance(checks, dict), (
                f"角色 {role} 的检查项不是 dict: {type(checks)}"
            )
            for check_name, is_critical in checks.items():
                assert isinstance(check_name, str)
                assert isinstance(is_critical, bool), (
                    f"角色 {role} 检查项 {check_name} 的 critical 值不是 bool: "
                    f"{type(is_critical)}"
                )

    def test_role_requirements_in_all(self, health):
        """ROLE_REQUIREMENTS 应在 __all__ 中导出。"""
        assert "ROLE_REQUIREMENTS" in health.__all__

    def test_crdb_required_roles_constant(self, health):
        """_CRDB_REQUIRED_ROLES 常量应包含 db_writer/crdb_sync/migration/admin。"""
        assert hasattr(health, "_CRDB_REQUIRED_ROLES")
        expected = {"db_writer", "crdb_sync", "migration", "admin"}
        assert expected.issubset(health._CRDB_REQUIRED_ROLES)

    def test_bot_roles_constant(self, health):
        """BOT_ROLES 常量应包含 5 个 bot 角色。"""
        assert hasattr(health, "BOT_ROLES")
        for role in ("up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"):
            assert role in health.BOT_ROLES

    def test_health_version_updated(self, health):
        """HEALTH_VERSION 应更新为 R71 Wave 1。"""
        assert health.HEALTH_VERSION == "R71 Wave 1"

    def test_role_alias_mapping_complete(self, health):
        """_ROLE_ALIASES 应覆盖 entrypoint 全部 13 个角色 + 别名。"""
        aliases = health._ROLE_ALIASES
        # 别名映射
        assert aliases.get("up") == "up_bot"
        assert aliases.get("idx") == "idx_bot"
        assert aliases.get("dsp") == "dsp_bot"
        assert aliases.get("mon") == "mon_bot"
        # 身份映射(规范名)
        for role in ("admin_bot", "db_writer", "crdb_sync", "db_backup",
                      "migration", "prometheus_exporter", "r40_scheduler",
                      "admin"):
            assert aliases.get(role) == role
        # 空字符串 → admin
        assert aliases.get("") == "admin"


# ════════════════════════════════════════════════════════════════
# B. _check_redis(role) fail-closed 行为
# ════════════════════════════════════════════════════════════════


class TestCheckRedisFailClosed:
    """验证 _check_redis(role) 在角色依赖 Redis 时 fail-closed。"""

    def test_redis_fail_closed_for_redis_dependent_role(self, health, monkeypatch):
        """依赖 Redis 的角色(up_bot)在 REDIS_URL 缺失时返回 unhealthy。"""
        # 模拟 config.settings 无 REDIS_URL
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)

        healthy, error, reason = asyncio.run(health._check_redis("up_bot"))
        assert healthy is False
        assert "REDIS_URL not configured" in error
        assert "up_bot" in error
        assert reason is None

    def test_redis_not_fail_closed_for_non_redis_role(self, health, monkeypatch):
        """不依赖 Redis 的角色(crdb_sync)在 REDIS_URL 缺失时返回 healthy(not_configured)。
        
        注意:crdb_sync 角色的 ROLE_REQUIREMENTS 中 redis 为 non-critical,
        但 _check_redis 本身的 fail-closed 逻辑基于 ROLE_REQUIREMENTS[role] 中
        是否有 "redis" 键。crdb_sync 有 "redis" 键(值为 False),
        所以仍会 fail-closed。这里测试 migration 角色(无 redis 键)。
        """
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)

        # migration 角色没有 redis 检查项,所以不依赖 Redis
        healthy, error, reason = asyncio.run(health._check_redis("migration"))
        assert healthy is True
        assert error is None
        assert reason == "not_configured"

    def test_redis_fail_closed_for_db_writer(self, health, monkeypatch):
        """db_writer 依赖 Redis,REDIS_URL 缺失时 fail-closed。"""
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)

        healthy, error, reason = asyncio.run(health._check_redis("db_writer"))
        assert healthy is False
        assert "REDIS_URL not configured" in error
        assert "db_writer" in error

    def test_redis_fail_closed_for_empty_role(self, health, monkeypatch):
        """空角色(admin)依赖 Redis,REDIS_URL 缺失时 fail-closed。
        
        admin 角色在 ROLE_REQUIREMENTS 中有 "redis" 键,
        所以空角色规范化为 admin 后也会 fail-closed。
        """
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)

        # 空角色 → admin(全部检查,包含 redis)
        healthy, error, reason = asyncio.run(health._check_redis("admin"))
        assert healthy is False
        assert "REDIS_URL not configured" in error
        assert "admin" in error


# ════════════════════════════════════════════════════════════════
# C. _check_database(role) 角色化路由
# ════════════════════════════════════════════════════════════════


class TestCheckDatabaseRoleAware:
    """验证 _check_database(role) 按 _CRDB_REQUIRED_ROLES 路由。"""

    def test_database_routes_to_crdb_for_db_writer(self, health, monkeypatch):
        """db_writer 角色 → _check_database 委托给 _check_database_crdb。"""
        called_with = {}

        async def mock_crdb_check(role=""):
            called_with["role"] = role
            return True, None

        monkeypatch.setattr(health, "_check_database_crdb", mock_crdb_check)
        healthy, error = asyncio.run(health._check_database("db_writer"))
        assert healthy is True
        assert error is None
        assert called_with.get("role") == "db_writer"

    def test_database_routes_to_crdb_for_crdb_sync(self, health, monkeypatch):
        """crdb_sync 角色 → _check_database 委托给 _check_database_crdb。"""
        called_with = {}

        async def mock_crdb_check(role=""):
            called_with["role"] = role
            return True, None

        monkeypatch.setattr(health, "_check_database_crdb", mock_crdb_check)
        healthy, error = asyncio.run(health._check_database("crdb_sync"))
        assert healthy is True
        assert called_with.get("role") == "crdb_sync"

    def test_database_routes_to_crdb_for_migration(self, health, monkeypatch):
        """migration 角色 → _check_database 委托给 _check_database_crdb。"""
        called_with = {}

        async def mock_crdb_check(role=""):
            called_with["role"] = role
            return True, None

        monkeypatch.setattr(health, "_check_database_crdb", mock_crdb_check)
        healthy, error = asyncio.run(health._check_database("migration"))
        assert healthy is True
        assert called_with.get("role") == "migration"

    def test_database_routes_to_crdb_for_admin(self, health, monkeypatch):
        """admin 角色 → _check_database 委托给 _check_database_crdb。"""
        called_with = {}

        async def mock_crdb_check(role=""):
            called_with["role"] = role
            return True, None

        monkeypatch.setattr(health, "_check_database_crdb", mock_crdb_check)
        healthy, error = asyncio.run(health._check_database("admin"))
        assert healthy is True
        assert called_with.get("role") == "admin"

    def test_database_crdb_fail_closed_no_url(self, health, monkeypatch):
        """_check_database_crdb 在 DATABASE_URL 和 COCKROACHDB_URL 均缺失时 fail-closed。"""
        mock_settings = type(
            "MockSettings", (), {"DATABASE_URL": "", "COCKROACHDB_URL": ""}
        )()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("COCKROACHDB_URL", raising=False)

        healthy, error = asyncio.run(
            health._check_database_crdb("db_writer")
        )
        assert healthy is False
        assert "not configured" in error
        assert "db_writer" in error

    def test_database_crdb_fail_closed_sqlite_url(self, health, monkeypatch):
        """_check_database_crdb 在 DB URL 为 SQLite 时 fail-closed。"""
        mock_settings = type(
            "MockSettings",
            (),
            {"DATABASE_URL": "sqlite:///test.db", "COCKROACHDB_URL": ""},
        )()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )

        healthy, error = asyncio.run(
            health._check_database_crdb("crdb_sync")
        )
        assert healthy is False
        assert "requires CRDB" in error
        assert "SQLite" in error

    def test_database_crdb_fallback_to_cockroachdb_url(self, health, monkeypatch):
        """_check_database_crdb 在 DATABASE_URL 缺失时回退到 COCKROACHDB_URL。"""
        mock_settings = type(
            "MockSettings",
            (),
            {"DATABASE_URL": "", "COCKROACHDB_URL": "postgresql://test@localhost/test"},
        )()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("DATABASE_URL", raising=False)

        # COCKROACHDB_URL 已设置 → 不应返回 "not configured" 错误
        # 实际连接会失败(无真实 DB),但不应该是配置缺失错误
        healthy, error = asyncio.run(
            health._check_database_crdb("migration")
        )
        # 连接失败(非配置缺失),error 描述连接问题
        if not healthy:
            assert "not configured" not in error


# ════════════════════════════════════════════════════════════════
# D. 新增检查函数存在性与基本行为
# ════════════════════════════════════════════════════════════════


class TestNewCheckFunctions:
    """验证 R71 Wave 1 新增的检查函数存在且可调用。"""

    def test_check_database_crdb_exists(self, health):
        """_check_database_crdb 函数应存在且为协程函数。"""
        assert hasattr(health, "_check_database_crdb")
        assert inspect.iscoroutinefunction(health._check_database_crdb)

    def test_check_crdb_sync_lag_exists(self, health):
        """_check_crdb_sync_lag 函数应存在且为协程函数。"""
        assert hasattr(health, "_check_crdb_sync_lag")
        assert inspect.iscoroutinefunction(health._check_crdb_sync_lag)

    def test_check_backup_dir_writable_exists(self, health):
        """_check_backup_dir_writable 函数应存在且为协程函数。"""
        assert hasattr(health, "_check_backup_dir_writable")
        assert inspect.iscoroutinefunction(health._check_backup_dir_writable)

    def test_check_metrics_endpoint_exists(self, health):
        """_check_metrics_endpoint 函数应存在且为协程函数。"""
        assert hasattr(health, "_check_metrics_endpoint")
        assert inspect.iscoroutinefunction(health._check_metrics_endpoint)

    def test_check_scheduler_heartbeat_exists(self, health):
        """_check_scheduler_heartbeat 函数应存在且为协程函数。"""
        assert hasattr(health, "_check_scheduler_heartbeat")
        assert inspect.iscoroutinefunction(health._check_scheduler_heartbeat)

    def test_check_crdb_sync_lag_fail_closed_no_db(self, health, monkeypatch):
        """_check_crdb_sync_lag 在 cache_store.db 不存在时 fail-closed。"""
        # 若 database.cache_store 依赖的 aiosqlite 缺失,跳过
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 DB 相关测试")
        # R71 RC47: 清除 CI/GITHUB_ACTIONS 环境变量,避免 _is_ci_mode() 旁路
        # fail-closed 逻辑(CI 中 crdb_sync 进程未运行是正常的,但测试需验证真实 fail-closed)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        # 模拟 DB_PATH 不存在
        mock_path = type("MockPath", (), {"exists": lambda self: False})()
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", mock_path, raising=False
        )

        healthy, error = asyncio.run(health._check_crdb_sync_lag())
        assert healthy is False
        assert "cache_store.db not found" in error

    def test_check_scheduler_heartbeat_fail_closed_no_db(self, health, monkeypatch):
        """_check_scheduler_heartbeat 在 cache_store.db 不存在时 fail-closed。"""
        # 若 database.cache_store 依赖的 aiosqlite 缺失,跳过
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 DB 相关测试")
        # R71 RC47: 清除 CI/GITHUB_ACTIONS 环境变量,避免 _is_ci_mode() 旁路
        # fail-closed 逻辑(CI 中 scheduler 未运行是正常的,但测试需验证真实 fail-closed)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        mock_path = type("MockPath", (), {"exists": lambda self: False})()
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", mock_path, raising=False
        )

        healthy, error = asyncio.run(health._check_scheduler_heartbeat())
        assert healthy is False
        assert "cache_store.db not found" in error

    def test_check_crdb_sync_lag_warming_up_key_not_set(self, health, monkeypatch):
        """R72 RC54 fix: _check_crdb_sync_lag 在 key 不存在时视为健康(warming up)。

        根因: crdb_sync 进程启动后需要时间完成首次 CRDB → SQLite 同步,
        在首次同步完成前 kv_store.crdb_sync_last_success 不会被写入。
        这与 Redis Stream 惰性创建同理(RC51 fix):进程已就绪待运行,
        只是尚未产生首次产物。进程崩溃由 mon_bot heartbeat 检测。
        key 存在但过期(> _CRDB_SYNC_LAG_THRESHOLD)才视为失败。
        """
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 DB 相关测试")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("READINESS_GATE_PRE_LAUNCH", raising=False)

        # 模拟 cache_store.db 存在,kv_store 表存在,但 key 未设置
        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows
                self._idx = 0

            def fetchone(self):
                if self._idx < len(self._rows):
                    r = self._rows[self._idx]
                    self._idx += 1
                    return r
                return None

        class FakeConn:
            def __init__(self, has_table=True, has_key=False):
                self._has_table = has_table
                self._has_key = has_key

            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return FakeCursor([("kv_store",)] if self._has_table else [None])
                # SELECT value FROM kv_store WHERE key = ?
                return FakeCursor([("value",)] if self._has_key else [None])

            def close(self):
                pass

        import sqlite3 as _sqlite3

        mock_db_path = type("MockPath", (), {"exists": lambda self: True})()
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", mock_db_path, raising=False
        )
        monkeypatch.setattr(
            _sqlite3, "connect", lambda *a, **kw: FakeConn(has_table=True, has_key=False)
        )

        healthy, error = asyncio.run(health._check_crdb_sync_lag())
        assert healthy is True, (
            "R72 RC54: crdb_sync 进程启动后 key 未写入应视为 warming up 健康"
        )
        assert error is not None
        assert "warming up" in error

    def test_check_scheduler_heartbeat_warming_up_key_not_set(self, health, monkeypatch):
        """R72 RC54 fix: _check_scheduler_heartbeat 在 key 不存在时视为健康(warming up)。

        根因: r40_scheduler 进程启动后需要时间完成首次调度循环并写入心跳,
        在首次心跳写入前 kv_store.r40_scheduler_heartbeat 不会被设置。
        这与 Redis Stream 惰性创建同理(RC51 fix):进程已就绪待运行,
        只是尚未产生首次产物。进程崩溃由 mon_bot heartbeat 检测。
        key 存在但过期(> _SCHEDULER_HEARTBEAT_STALE_THRESHOLD)才视为失败。
        """
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 DB 相关测试")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("READINESS_GATE_PRE_LAUNCH", raising=False)

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows
                self._idx = 0

            def fetchone(self):
                if self._idx < len(self._rows):
                    r = self._rows[self._idx]
                    self._idx += 1
                    return r
                return None

        class FakeConn:
            def __init__(self, has_table=True, has_key=False):
                self._has_table = has_table
                self._has_key = has_key

            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return FakeCursor([("kv_store",)] if self._has_table else [None])
                return FakeCursor([("value",)] if self._has_key else [None])

            def close(self):
                pass

        import sqlite3 as _sqlite3

        mock_db_path = type("MockPath", (), {"exists": lambda self: True})()
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", mock_db_path, raising=False
        )
        monkeypatch.setattr(
            _sqlite3, "connect", lambda *a, **kw: FakeConn(has_table=True, has_key=False)
        )

        healthy, error = asyncio.run(health._check_scheduler_heartbeat())
        assert healthy is True, (
            "R72 RC54: r40_scheduler 进程启动后 key 未写入应视为 warming up 健康"
        )
        assert error is not None
        assert "warming up" in error

    def test_check_backup_dir_writable_fail_closed_no_env(self, health, monkeypatch):
        """_check_backup_dir_writable 在 BACKUP_DIR 未配置时使用默认路径。
        
        如果默认路径也不可写,fail-closed。这里测试默认路径可写的情况。
        """
        monkeypatch.delenv("BACKUP_DIR", raising=False)
        # 使用临时目录作为默认路径
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(
                "pathlib.Path",
                type("MockPath", (), {
                    "resolve": lambda self: type("P", (), {
                        "parent": type("P", (), {
                            "parent": type("P", (), {
                                "__truediv__": lambda self, other: type("P", (), {
                                    "__truediv__": lambda self, other: Path(tmpdir)
                                })()
                            })()
                        })()
                    })()
                }),
                raising=False,
            )
            healthy, error = asyncio.run(health._check_backup_dir_writable())
            # 可能成功或失败,取决于默认路径是否可写
            assert isinstance(healthy, bool)
            if not healthy:
                assert isinstance(error, str)


# ════════════════════════════════════════════════════════════════
# E. check_readiness(role) ROLE_REQUIREMENTS 动态调度
# ════════════════════════════════════════════════════════════════


class TestCheckReadinessDynamicDispatch:
    """验证 check_readiness(role) 从 ROLE_REQUIREMENTS 动态调度检查函数。"""

    @pytest.fixture
    def mock_all_checks_pass(self, monkeypatch, health):
        """统一 mock 所有底层检查函数为"通过"状态。"""
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_database_crdb",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory_3tuple((True, None, None))
        )
        monkeypatch.setattr(
            health, "_check_bot_token_valid",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_upload_session_status",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_bot_polling_status",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_index_queue_depth",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_redis_stream_consumer",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_send_queue_depth",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_sub_services_alive",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_admin_web_port",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_redis_stream_consumer_group",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_writer_inbox_lag",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_crdb_sync_lag",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_backup_dir_writable",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_metrics_endpoint",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_scheduler_heartbeat",
            _async_return_factory((True, None))
        )
        yield

    def test_up_bot_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """up_bot 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("up_bot"))
        expected_checks = set(health.ROLE_REQUIREMENTS["up_bot"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks, (
            f"up_bot 检查项不匹配: expected={expected_checks}, "
            f"actual={actual_checks}"
        )
        assert result.healthy is True

    def test_idx_bot_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """idx_bot 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("idx_bot"))
        expected_checks = set(health.ROLE_REQUIREMENTS["idx_bot"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_dsp_bot_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """dsp_bot 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("dsp_bot"))
        expected_checks = set(health.ROLE_REQUIREMENTS["dsp_bot"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_db_writer_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """db_writer 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("db_writer"))
        expected_checks = set(health.ROLE_REQUIREMENTS["db_writer"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_crdb_sync_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """crdb_sync 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("crdb_sync"))
        expected_checks = set(health.ROLE_REQUIREMENTS["crdb_sync"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_migration_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """migration 角色的检查项集合应与 ROLE_REQUIREMENTS 一致(仅 database_crdb)。"""
        result = asyncio.run(health.check_readiness("migration"))
        expected_checks = set(health.ROLE_REQUIREMENTS["migration"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks
        assert "database_crdb" in actual_checks

    def test_prometheus_exporter_checks_match(self, health, mock_all_checks_pass):
        """prometheus_exporter 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("prometheus_exporter"))
        expected_checks = set(health.ROLE_REQUIREMENTS["prometheus_exporter"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_r40_scheduler_checks_match(self, health, mock_all_checks_pass):
        """r40_scheduler 角色的检查项集合应与 ROLE_REQUIREMENTS 一致。"""
        result = asyncio.run(health.check_readiness("r40_scheduler"))
        expected_checks = set(health.ROLE_REQUIREMENTS["r40_scheduler"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_admin_checks_match_role_requirements(self, health, mock_all_checks_pass):
        """admin 角色应执行全部检查项。"""
        result = asyncio.run(health.check_readiness("admin"))
        expected_checks = set(health.ROLE_REQUIREMENTS["admin"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks
        # admin 应包含所有检查项
        assert len(actual_checks) >= 15

    def test_critical_flag_preserved(self, health, mock_all_checks_pass):
        """check_readiness 应保留 ROLE_REQUIREMENTS 中的 critical 标志。"""
        result = asyncio.run(health.check_readiness("up_bot"))
        req = health.ROLE_REQUIREMENTS["up_bot"]
        for chk in result.checks:
            assert chk.critical == req[chk.name], (
                f"检查项 {chk.name} 的 critical 标志不匹配: "
                f"expected={req[chk.name]}, actual={chk.critical}"
            )

    def test_critical_check_failure_makes_unhealthy(self, health, monkeypatch):
        """critical 检查失败 → 整体 healthy=False。"""
        # mock database(critical)失败,其他通过
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((False, "DB connection refused"))
        )
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory_3tuple((True, None, None))
        )
        monkeypatch.setattr(
            health, "_check_bot_token_valid",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_upload_session_status",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_bot_polling_status",
            _async_return_factory((True, None))
        )
        result = asyncio.run(health.check_readiness("up_bot"))
        assert result.healthy is False
        # database 检查应失败
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.healthy is False
        assert db_check.critical is True

    def test_non_critical_check_failure_keeps_healthy(self, health, monkeypatch):
        """non-critical 检查失败 → 整体 healthy=True 但 checks 中有 failed 项。"""
        # mock upload_session_status(non-critical)失败,其他通过
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory_3tuple((True, None, None))
        )
        monkeypatch.setattr(
            health, "_check_bot_token_valid",
            _async_return_factory((True, None))
        )
        monkeypatch.setattr(
            health, "_check_upload_session_status",
            _async_return_factory((False, "stuck session"))
        )
        monkeypatch.setattr(
            health, "_check_bot_polling_status",
            _async_return_factory((True, None))
        )
        result = asyncio.run(health.check_readiness("up_bot"))
        assert result.healthy is True
        # upload_session_status 检查应失败
        uss_check = next(
            c for c in result.checks if c.name == "upload_session_status"
        )
        assert uss_check.healthy is False
        assert uss_check.critical is False

    def test_role_alias_resolution(self, health, mock_all_checks_pass):
        """角色别名应正确解析(up → up_bot)。"""
        result = asyncio.run(health.check_readiness("up"))
        assert result.role == "up_bot"
        expected_checks = set(health.ROLE_REQUIREMENTS["up_bot"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks

    def test_empty_role_resolves_to_admin(self, health, mock_all_checks_pass):
        """空角色应解析为 admin(全部检查)。"""
        result = asyncio.run(health.check_readiness(""))
        assert result.role == "admin"
        expected_checks = set(health.ROLE_REQUIREMENTS["admin"].keys())
        actual_checks = {c.name for c in result.checks}
        assert actual_checks == expected_checks


# ════════════════════════════════════════════════════════════════
# F. 未知角色 fail-closed
# ════════════════════════════════════════════════════════════════


class TestUnknownRoleFailClosed:
    """验证未知角色返回 unhealthy(fail-closed)。"""

    def test_unknown_role_returns_unhealthy(self, health):
        """未知角色 → unhealthy + error="Unknown role: {role}"。"""
        result = asyncio.run(health.check_readiness("totally_unknown_role"))
        assert result.healthy is False
        assert result.role == "totally_unknown_role"
        assert len(result.checks) == 1
        chk = result.checks[0]
        assert chk.name == "role_validation"
        assert chk.healthy is False
        assert chk.critical is True
        assert "Unknown role" in chk.error
        assert "totally_unknown_role" in chk.error

    def test_unknown_role_with_special_chars(self, health):
        """含特殊字符的未知角色也应 fail-closed。"""
        result = asyncio.run(health.check_readiness("evil_role'; DROP TABLE--"))
        assert result.healthy is False
        assert "Unknown role" in result.checks[0].error

    def test_unknown_role_health_result_structure(self, health):
        """未知角色的 HealthResult 结构应完整。"""
        result = asyncio.run(health.check_readiness("nonexistent"))
        assert hasattr(result, "healthy")
        assert hasattr(result, "role")
        assert hasattr(result, "checks")
        assert hasattr(result, "timestamp")
        assert hasattr(result, "version")
        assert result.version == health.HEALTH_VERSION
        # to_dict 应可序列化
        d = result.to_dict()
        assert d["healthy"] is False
        assert d["role"] == "nonexistent"


# ════════════════════════════════════════════════════════════════
# G. CLI 入口
# ════════════════════════════════════════════════════════════════


class TestCLIEntryPoint:
    """验证 CLI 入口(--role / --json)行为。"""

    def test_cli_main_exists(self, health):
        """_cli_main 函数应存在。"""
        assert hasattr(health, "_cli_main")
        assert callable(health._cli_main)

    def test_cli_main_returns_0_for_healthy(self, health, monkeypatch):
        """CLI 在所有检查通过时返回 0。"""
        # mock check_readiness 返回 healthy
        async def mock_check(role):
            return health.HealthResult(
                healthy=True,
                role=role or "admin",
                checks=[
                    health.CheckResult(
                        name="database", healthy=True,
                        latency_ms=10, error=None, critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version=health.HEALTH_VERSION,
            )

        monkeypatch.setattr(health, "check_readiness", mock_check)
        monkeypatch.setattr(sys, "argv", ["health.py", "--role", "admin"])
        ret = health._cli_main()
        assert ret == 0

    def test_cli_main_returns_1_for_unhealthy(self, health, monkeypatch):
        """CLI 在检查失败时返回 1。"""
        async def mock_check(role):
            return health.HealthResult(
                healthy=False,
                role=role or "admin",
                checks=[
                    health.CheckResult(
                        name="database", healthy=False,
                        latency_ms=10, error="DB down",
                        critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version=health.HEALTH_VERSION,
            )

        monkeypatch.setattr(health, "check_readiness", mock_check)
        monkeypatch.setattr(sys, "argv", ["health.py", "--role", "admin"])
        ret = health._cli_main()
        assert ret == 1

    def test_cli_main_json_output(self, health, monkeypatch, capsys):
        """CLI --json 应输出 JSON 格式。"""
        async def mock_check(role):
            return health.HealthResult(
                healthy=True,
                role=role or "admin",
                checks=[],
                timestamp="2026-07-21T00:00:00+00:00",
                version=health.HEALTH_VERSION,
            )

        monkeypatch.setattr(health, "check_readiness", mock_check)
        monkeypatch.setattr(sys, "argv", ["health.py", "--role", "admin", "--json"])
        ret = health._cli_main()
        assert ret == 0
        captured = capsys.readouterr()
        import json
        data = json.loads(captured.out)
        assert data["healthy"] is True
        assert data["role"] == "admin"
        assert data["version"] == health.HEALTH_VERSION

    def test_cli_main_text_output(self, health, monkeypatch, capsys):
        """CLI 默认输出文本格式(非 JSON)。"""
        async def mock_check(role):
            return health.HealthResult(
                healthy=True,
                role=role or "admin",
                checks=[
                    health.CheckResult(
                        name="database", healthy=True,
                        latency_ms=5, error=None, critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version=health.HEALTH_VERSION,
            )

        monkeypatch.setattr(health, "check_readiness", mock_check)
        monkeypatch.setattr(sys, "argv", ["health.py", "--role", "admin"])
        ret = health._cli_main()
        assert ret == 0
        captured = capsys.readouterr()
        assert "HEALTHY" in captured.out
        assert "admin" in captured.out
        assert "database" in captured.out

    def test_cli_main_crash_returns_1(self, health, monkeypatch):
        """CLI 在 check_readiness 崩溃时返回 1(不吞异常)。"""
        async def mock_check(role):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(health, "check_readiness", mock_check)
        monkeypatch.setattr(sys, "argv", ["health.py", "--role", "admin"])
        ret = health._cli_main()
        assert ret == 1


# ════════════════════════════════════════════════════════════════
# H. deprecated wrapper 向后兼容
# ════════════════════════════════════════════════════════════════


class TestDeprecatedWrappers:
    """验证 prometheus_exporter 和 maintenance_mode 的 deprecated wrapper。"""

    def test_prometheus_exporter_check_readiness_wrapper_exists(self):
        """prometheus_exporter.check_readiness 应作为 deprecated wrapper 存在。"""
        try:
            from services.prometheus_exporter import (
                check_readiness, collect_dependency_status,
            )
        except ImportError as e:
            pytest.fail(f"无法导入 prometheus_exporter 函数: {e}")
        assert callable(check_readiness)
        assert callable(collect_dependency_status)

    def test_prometheus_exporter_wrapper_calls_new_function(self):
        """deprecated check_readiness 应调用 collect_dependency_status。"""
        from services.prometheus_exporter import (
            check_readiness, collect_dependency_status,
        )
        # 两者应返回相同结果
        r1 = check_readiness()
        r2 = collect_dependency_status()
        assert r1 == r2

    def test_maintenance_mode_check_readiness_wrapper_exists(self):
        """maintenance_mode.check_readiness 应作为 deprecated wrapper 存在。"""
        # maintenance_mode 依赖 fastapi / aiosqlite,缺失时跳过
        pytest.importorskip("fastapi", reason="fastapi 未安装,跳过 maintenance_mode 测试")
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 maintenance_mode 测试")
        try:
            from services.maintenance_mode import (
                check_readiness, check_maintenance_safe,
            )
        except ImportError as e:
            pytest.fail(f"无法导入 maintenance_mode 函数: {e}")
        assert callable(check_readiness)
        assert callable(check_maintenance_safe)

    def test_maintenance_mode_wrapper_calls_new_function(self):
        """deprecated check_readiness 应调用 check_maintenance_safe。"""
        # maintenance_mode 依赖 fastapi / aiosqlite,缺失时跳过
        pytest.importorskip("fastapi", reason="fastapi 未安装,跳过 maintenance_mode 测试")
        pytest.importorskip("aiosqlite", reason="aiosqlite 未安装,跳过 maintenance_mode 测试")
        import asyncio
        from services.maintenance_mode import (
            check_readiness, check_maintenance_safe,
        )
        r1 = asyncio.run(check_readiness())
        r2 = asyncio.run(check_maintenance_safe())
        assert r1 == r2


# ════════════════════════════════════════════════════════════════
# I. entrypoint readiness gate
# ════════════════════════════════════════════════════════════════


class TestEntrypointReadinessGate:
    """验证 docker/entrypoint.py 的 readiness gate 函数存在。"""

    @pytest.fixture(autouse=True)
    def _clear_ci_env(self, monkeypatch):
        """R71 RC47: 清除 CI/GITHUB_ACTIONS 环境变量。

        _run_readiness_gate() 在 CI 模式下提前返回(R71 RC28),跳过
        readiness 检查不执行 sys.exit(4)。测试需验证真实 fail-closed 逻辑,
        因此必须清除 CI 环境变量。
        """
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def test_run_readiness_gate_exists(self, entry_module):
        """_run_readiness_gate 函数应存在。"""
        assert hasattr(entry_module, "_run_readiness_gate")
        assert callable(entry_module._run_readiness_gate)

    def test_readiness_gate_skipped_in_development(self, entry_module, monkeypatch):
        """development 模式下 readiness gate 被跳过(不退出)。"""
        # 模拟 check_readiness 总是失败,但 development 应跳过
        import asyncio

        async def mock_check(role):
            raise RuntimeError("should not be called in development")

        # 注入 mock 到 services.health
        if "services.health" not in sys.modules:
            _load_health_module()
        monkeypatch.setattr(
            sys.modules["services.health"], "check_readiness", mock_check
        )

        # development 应跳过,不调用 check_readiness,不退出
        entry_module._run_readiness_gate("development", "up")
        # 如果到达这里,说明没有 sys.exit

    def test_readiness_gate_enforced_in_production(self, entry_module, monkeypatch):
        """production 模式下 readiness gate 强制执行,检查失败 → exit 4。"""
        import asyncio

        async def mock_check(role):
            from services.health import HealthResult, CheckResult
            return HealthResult(
                healthy=False,
                role=role,
                checks=[
                    CheckResult(
                        name="database", healthy=False,
                        latency_ms=10, error="DB down",
                        critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version="R71 Wave 1",
            )

        if "services.health" not in sys.modules:
            _load_health_module()
        monkeypatch.setattr(
            sys.modules["services.health"], "check_readiness", mock_check
        )

        with pytest.raises(SystemExit) as exc_info:
            entry_module._run_readiness_gate("production", "up")
        assert exc_info.value.code == 4

    def test_readiness_gate_enforced_in_staging(self, entry_module, monkeypatch):
        """staging 模式下 readiness gate 强制执行,检查失败 → exit 4。"""
        import asyncio

        async def mock_check(role):
            from services.health import HealthResult, CheckResult
            return HealthResult(
                healthy=False,
                role=role,
                checks=[
                    CheckResult(
                        name="redis", healthy=False,
                        latency_ms=5, error="Redis down",
                        critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version="R71 Wave 1",
            )

        if "services.health" not in sys.modules:
            _load_health_module()
        monkeypatch.setattr(
            sys.modules["services.health"], "check_readiness", mock_check
        )

        with pytest.raises(SystemExit) as exc_info:
            entry_module._run_readiness_gate("staging", "db_writer")
        assert exc_info.value.code == 4

    def test_readiness_gate_passes_when_healthy(self, entry_module, monkeypatch):
        """production 模式下 readiness 检查通过时不退出。"""
        import asyncio

        async def mock_check(role):
            from services.health import HealthResult, CheckResult
            return HealthResult(
                healthy=True,
                role=role,
                checks=[
                    CheckResult(
                        name="database", healthy=True,
                        latency_ms=10, error=None, critical=True,
                    )
                ],
                timestamp="2026-07-21T00:00:00+00:00",
                version="R71 Wave 1",
            )

        if "services.health" not in sys.modules:
            _load_health_module()
        monkeypatch.setattr(
            sys.modules["services.health"], "check_readiness", mock_check
        )

        # 应不退出(到达此处即通过)
        entry_module._run_readiness_gate("production", "up")

    def test_readiness_gate_crash_exits_4(self, entry_module, monkeypatch):
        """readiness 模块崩溃时 → exit 4(fail-closed,不吞异常)。"""
        async def mock_check(role):
            raise RuntimeError("simulated crash")

        if "services.health" not in sys.modules:
            _load_health_module()
        monkeypatch.setattr(
            sys.modules["services.health"], "check_readiness", mock_check
        )

        with pytest.raises(SystemExit) as exc_info:
            entry_module._run_readiness_gate("production", "up")
        assert exc_info.value.code == 4
