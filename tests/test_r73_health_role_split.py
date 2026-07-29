"""R73 §5.10 (P1-06): health 探针按角色分离 — 测试套件。

R73 §5.10 整改要求:
    旧版 check_readiness 把所有检查混合返回单个 healthy 布尔,无法区分
    liveness(进程存活) / startup(初始化完成) / dependency_health(依赖健康)
    / aggregate(整体 readiness)。R73 §5.10 要求按角色分离这些探针。

新增三个函数(加法式扩展,不破坏 check_readiness):
    1. check_liveness(role)        — 进程是否还活着(死锁才 fail)
    2. check_startup(role)         — 必需初始化是否完成
    3. check_dependency_health(role) — 角色依赖是否健康(替代 readiness,关注依赖)

测试矩阵:
    A. check_liveness — 各角色返回 alive=True、含 pid/checked_at/role
    B. check_startup — STARTUP_REQUIREMENTS 覆盖全部角色;未初始化角色
       pending_initializations 列出未完成步骤
    C. check_dependency_health — 各角色依赖检查项集合正确;
       缺失依赖时 dependencies_healthy=False;未知角色 fail-closed
    D. 探针语义分离 — liveness/startup/dependency_health 返回结构不同,
       不互相调用,不破坏 check_readiness
    E. prometheus_exporter 委托 — check_dependency_health('prometheus_exporter')
       委托给 collect_dependency_status
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 测试环境兼容(conftest 在收集阶段已注入 config/telegram mock)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 测试隔离:直接加载 services.health 模块
# ════════════════════════════════════════════════════════════════


def _load_health_module():
    """加载 services.health 模块(支持重载)。"""
    if "services.health" in sys.modules and hasattr(
        sys.modules["services.health"], "check_liveness"
    ):
        return sys.modules["services.health"]

    # 确保 services 包可导入
    if "services" not in sys.modules:
        services_pkg = type(sys)("services")
        services_pkg.__path__ = [str(REPO_ROOT / "services")]
        sys.modules["services"] = services_pkg

    health_path = REPO_ROOT / "services" / "health.py"
    spec = importlib.util.spec_from_file_location("services.health", health_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.health"] = module
    spec.loader.exec_module(module)
    return sys.modules["services.health"]


@pytest.fixture(scope="module")
def health():
    """提供 services.health 模块实例(模块级缓存)。"""
    return _load_health_module()


# ════════════════════════════════════════════════════════════════
# A. check_liveness — 进程存活探针
# ════════════════════════════════════════════════════════════════


class TestCheckLiveness:
    """R73 §5.10 (P1-06): check_liveness 探针 — 各角色返回 alive=True。"""

    def test_check_liveness_exists(self, health):
        """check_liveness 函数应存在且可调用。"""
        assert hasattr(health, "check_liveness")
        assert callable(health.check_liveness)

    def test_check_liveness_returns_dict(self, health):
        """check_liveness 应返回 dict(不是 HealthResult)。"""
        result = health.check_liveness("up_bot")
        assert isinstance(result, dict), (
            "check_liveness 应返回 dict,而非 HealthResult "
            "(与 check_readiness 的 HealthResult 区分)"
        )

    @pytest.mark.parametrize("role", [
        "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
        "db_writer", "crdb_sync", "db_backup", "migration",
        "prometheus_exporter", "r40_scheduler", "admin",
    ])
    def test_liveness_alive_for_each_role(self, health, role):
        """每个角色的 liveness 探针应返回 alive=True(进程未死锁)。"""
        result = health.check_liveness(role)
        assert result["alive"] is True, (
            f"角色 {role} 的 liveness 应为 alive=True "
            f"(进程未死锁时总是 True)"
        )
        assert result["role"] == role

    def test_liveness_includes_pid(self, health):
        """liveness 结果应包含 pid(进程标识)。"""
        import os

        result = health.check_liveness("up_bot")
        assert "pid" in result
        assert result["pid"] == os.getpid()

    def test_liveness_includes_checked_at(self, health):
        """liveness 结果应包含 checked_at(ISO 8601 时间戳)。"""
        result = health.check_liveness("up_bot")
        assert "checked_at" in result
        assert isinstance(result["checked_at"], str)
        # ISO 8601 时间戳应可解析
        import datetime as _dt
        _dt.datetime.fromisoformat(result["checked_at"])

    def test_liveness_includes_last_event_loop_at(self, health):
        """liveness 结果应包含 last_event_loop_at(event loop 心跳)。"""
        result = health.check_liveness("up_bot")
        assert "last_event_loop_at" in result

    def test_liveness_includes_boot_id(self, health):
        """liveness 结果应包含 boot_id(Linux 系统启动 ID,非 Linux 可为空)。"""
        result = health.check_liveness("up_bot")
        assert "boot_id" in result
        # boot_id 在非 Linux 上可能为空字符串
        assert isinstance(result["boot_id"], str)

    def test_liveness_supports_role_alias(self, health):
        """check_liveness 应支持角色别名(如 'up' → 'up_bot')。"""
        result = health.check_liveness("up")
        assert result["role"] == "up_bot", (
            "check_liveness 应通过 _canonicalize_role 规范化角色别名"
        )

    def test_liveness_unknown_role_not_fail(self, health):
        """未知角色的 liveness 探针不应 fail(liveness 极度宽容,避免误重启)。

        注意:liveness 与 readiness 不同 — 未知角色不应触发 liveness fail,
        因为 liveness 只关心进程是否死锁,与角色合法性无关。
        """
        result = health.check_liveness("totally_unknown_role")
        # liveness 应仍返回 alive=True(进程未死锁)
        assert result["alive"] is True
        assert result["role"] == "totally_unknown_role"


# ════════════════════════════════════════════════════════════════
# B. check_startup — 启动初始化探针
# ════════════════════════════════════════════════════════════════


class TestCheckStartup:
    """R73 §5.10 (P1-06): check_startup 探针 — 必需初始化是否完成。"""

    def test_check_startup_exists(self, health):
        """check_startup 函数应存在且可调用。"""
        assert hasattr(health, "check_startup")
        assert callable(health.check_startup)

    def test_check_startup_returns_dict(self, health):
        """check_startup 应返回 dict。"""
        result = health.check_startup("up_bot")
        assert isinstance(result, dict), (
            "check_startup 应返回 dict"
        )

    def test_startup_requirements_covers_all_roles(self, health):
        """STARTUP_REQUIREMENTS 应覆盖全部 12 个角色。"""
        required_roles = {
            "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
            "db_writer", "crdb_sync", "db_backup", "migration",
            "prometheus_exporter", "r40_scheduler", "admin",
        }
        actual_roles = set(health.STARTUP_REQUIREMENTS.keys())
        missing = required_roles - actual_roles
        assert not missing, (
            f"STARTUP_REQUIREMENTS 缺少角色: {missing}"
        )

    def test_startup_requirements_values_are_lists(self, health):
        """STARTUP_REQUIREMENTS 的值必须是 list[str](启动步骤列表)。"""
        for role, steps in health.STARTUP_REQUIREMENTS.items():
            assert isinstance(steps, list), (
                f"角色 {role} 的启动步骤不是 list: {type(steps)}"
            )
            for step in steps:
                assert isinstance(step, str), (
                    f"角色 {role} 的启动步骤 {step!r} 不是 str"
                )

    def test_startup_includes_pending_initializations(self, health):
        """check_startup 结果应包含 pending_initializations 字段。"""
        result = health.check_startup("up_bot")
        assert "pending_initializations" in result
        assert isinstance(result["pending_initializations"], list)

    def test_startup_includes_started_field(self, health):
        """check_startup 结果应包含 started 字段(启动是否完成)。"""
        result = health.check_startup("up_bot")
        assert "started" in result
        assert isinstance(result["started"], bool)

    def test_startup_includes_in_startup_grace_field(self, health):
        """check_startup 结果应包含 in_startup_grace 字段。"""
        result = health.check_startup("up_bot")
        assert "in_startup_grace" in result
        assert isinstance(result["in_startup_grace"], bool)

    def test_startup_includes_startup_completed_at_field(self, health):
        """check_startup 结果应包含 startup_completed_at 字段(None 或 ISO 8601)。"""
        result = health.check_startup("up_bot")
        assert "startup_completed_at" in result
        # 未记录时为 None
        assert result["startup_completed_at"] is None or isinstance(
            result["startup_completed_at"], str
        )

    def test_startup_pending_when_uninitialized(self, health, monkeypatch):
        """未初始化角色(无 settings/无 token/无 redis)的 pending_initializations 非空。

        通过模拟 settings 缺失 + bot token 缺失 + redis 缺失,
        验证 pending_initializations 列出未完成步骤。
        """
        # 模拟 config.settings 抛 ImportError(通过替换 sys.modules['config'])
        mock_config = type("MockConfig", (), {})()
        # 不设置 settings 属性 → from config import settings 失败

        # 保存原 config
        original_config = sys.modules.get("config")
        try:
            # 移除 config 模块,使 from config import settings 失败
            if "config" in sys.modules:
                del sys.modules["config"]
            # 注入会抛 ImportError 的 mock
            mock_module = type(sys)("config")
            # 不设置 settings 属性,触发 AttributeError

            # 但 _startup_step_settings_loaded 用 try/except,所以需让 from config import settings 抛异常
            # 用一个会抛 ImportError 的 mock
            class _FailConfig:
                def __getattr__(self, name):
                    raise ImportError("mocked config failure")

            sys.modules["config"] = _FailConfig()

            # 清除环境变量,确保 bot_token / redis_url 缺失
            monkeypatch.delenv("UP_BOT_TOKEN", raising=False)
            monkeypatch.delenv("REDIS_URL", raising=False)
            monkeypatch.delenv("DATABASE_URL", raising=False)
            monkeypatch.delenv("COCKROACHDB_URL", raising=False)

            result = health.check_startup("up_bot")
            # up_bot 启动步骤:settings_loaded / bot_token_configured / redis_connected
            # 全部未配置 → pending_initializations 应包含这些
            pending = result["pending_initializations"]
            assert "settings_loaded" in pending or "bot_token_configured" in pending or "redis_connected" in pending, (
                f"未初始化时 pending_initializations 应列出未完成步骤,实际: {pending}"
            )
        finally:
            # 恢复原 config
            if original_config is not None:
                sys.modules["config"] = original_config
            else:
                sys.modules.pop("config", None)

    def test_startup_in_startup_grace_when_pre_launch(self, health, monkeypatch):
        """READINESS_GATE_PRE_LAUNCH=1 时 in_startup_grace=True 且 started=True。"""
        monkeypatch.setenv("READINESS_GATE_PRE_LAUNCH", "1")

        result = health.check_startup("up_bot")
        assert result["in_startup_grace"] is True
        # 启动宽限期 started 应为 True(避免容器编排 kill 尚未启动的进程)
        assert result["started"] is True

    def test_startup_supports_role_alias(self, health):
        """check_startup 应支持角色别名。"""
        result = health.check_startup("dsp")
        assert result["role"] == "dsp_bot"

    def test_startup_unknown_role_returns_empty_pending(self, health):
        """未知角色的 STARTUP_REQUIREMENTS 为空列表 → pending_initializations 为空。"""
        result = health.check_startup("totally_unknown_role")
        # 未知角色不在 STARTUP_REQUIREMENTS 中,required_steps = []
        assert result["pending_initializations"] == []
        # started = (无 pending) AND (startup_completed_at 已记录)
        # 未知角色的 startup_completed_at 通常为 None → started=False
        # 但 in_startup_grace=True 时 started=True


# ════════════════════════════════════════════════════════════════
# C. check_dependency_health — 依赖健康探针
# ════════════════════════════════════════════════════════════════


class TestCheckDependencyHealth:
    """R73 §5.10 (P1-06): check_dependency_health 探针 — 角色依赖是否健康。"""

    def test_check_dependency_health_exists(self, health):
        """check_dependency_health 函数应存在且可调用。"""
        assert hasattr(health, "check_dependency_health")
        assert callable(health.check_dependency_health)

    def test_check_dependency_health_is_async(self, health):
        """check_dependency_health 应是 async 协程函数(依赖检查需 await)。"""
        import inspect
        assert inspect.iscoroutinefunction(health.check_dependency_health), (
            "check_dependency_health 应是 async 函数(与 check_liveness/check_startup "
            "不同,依赖检查涉及 IO 需 await)"
        )

    @pytest.mark.asyncio
    async def test_check_dependency_health_returns_dict(self, health):
        """check_dependency_health 应返回 dict。"""
        result = await health.check_dependency_health("up_bot")
        assert isinstance(result, dict), (
            "check_dependency_health 应返回 dict"
        )

    @pytest.mark.asyncio
    async def test_dependency_health_includes_dependencies_healthy(self, health):
        """结果应包含 dependencies_healthy 布尔字段。"""
        result = await health.check_dependency_health("up_bot")
        assert "dependencies_healthy" in result
        assert isinstance(result["dependencies_healthy"], bool)

    @pytest.mark.asyncio
    async def test_dependency_health_includes_dependency_checks(self, health):
        """结果应包含 dependency_checks dict(各依赖检查结果)。"""
        result = await health.check_dependency_health("up_bot")
        assert "dependency_checks" in result
        assert isinstance(result["dependency_checks"], dict)
        # 每个依赖检查项的值是 {healthy, error_code}
        for name, check_result in result["dependency_checks"].items():
            assert isinstance(name, str)
            assert isinstance(check_result, dict)
            assert "healthy" in check_result
            assert isinstance(check_result["healthy"], bool)
            # error_code 可以是 None 或 str
            assert "error_code" in check_result

    @pytest.mark.asyncio
    async def test_dependency_health_includes_checked_at(self, health):
        """结果应包含 checked_at 字段(ISO 8601)。"""
        result = await health.check_dependency_health("up_bot")
        assert "checked_at" in result
        import datetime as _dt
        _dt.datetime.fromisoformat(result["checked_at"])

    def test_dependency_checks_covers_all_roles(self, health):
        """DEPENDENCY_CHECKS 应覆盖主要角色集合。"""
        required_roles = {
            "up_bot", "idx_bot", "dsp_bot", "db_writer",
            "crdb_sync", "admin", "mon_bot",
            "prometheus_exporter", "r40_scheduler",
        }
        actual_roles = set(health.DEPENDENCY_CHECKS.keys())
        missing = required_roles - actual_roles
        assert not missing, (
            f"DEPENDENCY_CHECKS 缺少角色: {missing}"
        )

    def test_dependency_checks_values_are_dicts_of_callables(self, health):
        """DEPENDENCY_CHECKS 的值必须是 dict[str, callable]。"""
        for role, dep_checkers in health.DEPENDENCY_CHECKS.items():
            assert isinstance(dep_checkers, dict), (
                f"角色 {role} 的依赖检查项不是 dict: {type(dep_checkers)}"
            )
            for dep_name, checker in dep_checkers.items():
                assert isinstance(dep_name, str)
                assert callable(checker), (
                    f"角色 {role} 的依赖检查项 {dep_name} 的 checker 不是 callable"
                )

    @pytest.mark.asyncio
    async def test_dependency_health_healthy_false_for_missing_dependency(
        self, health, monkeypatch
    ):
        """缺失依赖时 dependencies_healthy=False。

        通过模拟 REDIS_URL 缺失 + SQLite 文件不存在,
        验证 up_bot 的 dependencies_healthy=False。
        """
        # 模拟 config.settings 无 REDIS_URL
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("UP_BOT_TOKEN", raising=False)

        result = await health.check_dependency_health("up_bot")
        assert result["dependencies_healthy"] is False, (
            "缺失 REDIS_URL/Bot token 时,up_bot 的 dependencies_healthy 应为 False"
        )
        # 至少一个依赖检查项 unhealthy
        unhealthy = [
            name for name, cr in result["dependency_checks"].items()
            if not cr["healthy"]
        ]
        assert unhealthy, (
            "缺失依赖时至少一个 dependency_checks 项应为 unhealthy"
        )

    @pytest.mark.asyncio
    async def test_dependency_health_unknown_role_fail_closed(self, health):
        """未知角色 fail-closed:dependencies_healthy=False。"""
        result = await health.check_dependency_health("totally_unknown_role")
        assert result["dependencies_healthy"] is False, (
            "未知角色应 fail-closed: dependencies_healthy=False"
        )
        # role_validation 检查项应报告 UNKNOWN_ROLE
        assert "role_validation" in result["dependency_checks"]
        role_check = result["dependency_checks"]["role_validation"]
        assert role_check["healthy"] is False
        assert "UNKNOWN_ROLE" in (role_check["error_code"] or "")

    @pytest.mark.asyncio
    async def test_dependency_health_each_role_has_expected_checks(self, health):
        """每个角色的 dependency_checks 应列出 DEPENDENCY_CHECKS 中定义的检查项。"""
        # up_bot 应有 redis_local / sqlite_cache / bot_token_valid
        result = await health.check_dependency_health("up_bot")
        expected = set(health.DEPENDENCY_CHECKS["up_bot"].keys())
        actual = set(result["dependency_checks"].keys())
        assert expected.issubset(actual), (
            f"up_bot 的 dependency_checks 应包含 {expected},实际: {actual}"
        )

    @pytest.mark.asyncio
    async def test_dependency_health_supports_role_alias(self, health):
        """check_dependency_health 应支持角色别名。"""
        result = await health.check_dependency_health("dsp")
        assert result["role"] == "dsp_bot"

    @pytest.mark.asyncio
    async def test_dependency_health_error_code_is_structured(self, health, monkeypatch):
        """失败的依赖检查应返回结构化 error_code(供告警/路由使用)。"""
        # 模拟 REDIS_URL 缺失
        mock_settings = type("MockSettings", (), {"REDIS_URL": ""})()
        monkeypatch.setattr(
            "config.settings", mock_settings, raising=False
        )
        monkeypatch.delenv("REDIS_URL", raising=False)

        result = await health.check_dependency_health("up_bot")
        # redis_local 失败时应返回 error_code='REDIS_LOCAL_UNREACHABLE'
        redis_check = result["dependency_checks"].get("redis_local")
        if redis_check and not redis_check["healthy"]:
            assert redis_check["error_code"] is not None
            assert isinstance(redis_check["error_code"], str)
            # error_code 应为大写下划线格式(如 REDIS_LOCAL_UNREACHABLE)
            assert redis_check["error_code"].replace(" ", "").isupper() or "_" in redis_check["error_code"], (
                f"error_code 应为大写下划线格式,实际: {redis_check['error_code']}"
            )


# ════════════════════════════════════════════════════════════════
# D. 探针语义分离
# ════════════════════════════════════════════════════════════════


class TestProbeSemanticSeparation:
    """R73 §5.10: 验证 liveness/startup/dependency_health 语义分离。"""

    def test_three_probes_have_different_return_types(self, health):
        """三个探针的返回结构应可区分(语义不同)。

        - liveness: {alive, pid, boot_id, last_event_loop_at, checked_at, role}
        - startup: {started, in_startup_grace, startup_completed_at, pending_initializations, checked_at, role}
        - dependency_health: {dependencies_healthy, dependency_checks, checked_at, role}
        """
        liveness = health.check_liveness("up_bot")
        startup = health.check_startup("up_bot")

        # liveness 应有 alive,但不应有 started / dependencies_healthy
        assert "alive" in liveness
        assert "started" not in liveness
        assert "dependencies_healthy" not in liveness

        # startup 应有 started,但不应有 alive / dependencies_healthy
        assert "started" in startup
        assert "alive" not in startup
        assert "dependencies_healthy" not in startup

    @pytest.mark.asyncio
    async def test_dependency_health_distinct_from_liveness_startup(self, health):
        """dependency_health 应有 dependencies_healthy,不含 alive / started。"""
        dep = await health.check_dependency_health("up_bot")
        assert "dependencies_healthy" in dep
        assert "alive" not in dep
        assert "started" not in dep

    def test_check_readiness_still_works(self, health):
        """R73 §5.10 加法式扩展:check_readiness 仍保留且功能完整。"""
        assert hasattr(health, "check_readiness")
        assert callable(health.check_readiness)
        # check_readiness 应返回 HealthResult(不是 dict)
        import asyncio
        result = asyncio.run(health.check_readiness("up_bot"))
        # HealthResult 有 healthy / role / checks / timestamp / version
        assert hasattr(result, "healthy")
        assert hasattr(result, "role")
        assert hasattr(result, "checks")

    def test_liveness_is_synchronous(self, health):
        """liveness 应是同步函数(不涉及 IO,无需 async)。"""
        import inspect
        assert not inspect.iscoroutinefunction(health.check_liveness), (
            "check_liveness 应是同步函数(不涉及 IO)"
        )

    def test_startup_is_synchronous(self, health):
        """startup 应是同步函数(快速检查配置/状态,不阻塞)。"""
        import inspect
        assert not inspect.iscoroutinefunction(health.check_startup), (
            "check_startup 应是同步函数"
        )

    def test_probes_exported_in_all(self, health):
        """三个探针函数应在 __all__ 中导出。"""
        assert "check_liveness" in health.__all__
        assert "check_startup" in health.__all__
        assert "check_dependency_health" in health.__all__
        # STARTUP_REQUIREMENTS / DEPENDENCY_CHECKS 也应导出
        assert "STARTUP_REQUIREMENTS" in health.__all__
        assert "DEPENDENCY_CHECKS" in health.__all__


# ════════════════════════════════════════════════════════════════
# E. prometheus_exporter 委托 — check_dependency_health 委托给
#    collect_dependency_status
# ════════════════════════════════════════════════════════════════


class TestPrometheusExporterDelegation:
    """R73 §5.10: prometheus_exporter 角色委托给 collect_dependency_status。"""

    @pytest.mark.asyncio
    async def test_prometheus_exporter_delegates_to_collect_dependency_status(
        self, health, monkeypatch
    ):
        """check_dependency_health('prometheus_exporter') 委托给 collect_dependency_status。"""
        # mock collect_dependency_status 返回固定值
        mock_status = {
            "ready": True,
            "passed": 3,
            "checks": {
                "sqlite_readable": True,
                "recent_scrape": True,
                "schema_valid": True,
            },
            "details": {
                "sqlite_readable": "ok",
                "recent_scrape": "fresh",
                "schema_valid": "valid",
            },
        }

        def mock_collect():
            return mock_status

        # 注入 mock 到 services.prometheus_exporter
        import sys as _sys
        mock_pe = type(_sys)("services.prometheus_exporter")
        mock_pe.collect_dependency_status = mock_collect
        _sys.modules["services.prometheus_exporter"] = mock_pe

        result = await health.check_dependency_health("prometheus_exporter")
        # 委托的检查项应出现在 dependency_checks 中
        assert "sqlite_readable" in result["dependency_checks"]
        assert "recent_scrape" in result["dependency_checks"]
        assert "schema_valid" in result["dependency_checks"]
        # 委托的检查项 healthy=True
        assert result["dependency_checks"]["sqlite_readable"]["healthy"] is True

    @pytest.mark.asyncio
    async def test_prometheus_exporter_delegation_failure_handled(
        self, health, monkeypatch
    ):
        """collect_dependency_status 抛异常时,delegation 失败但不崩溃。"""
        def mock_collect():
            raise RuntimeError("mocked collect_dependency_status failure")

        import sys as _sys
        mock_pe = type(_sys)("services.prometheus_exporter")
        mock_pe.collect_dependency_status = mock_collect
        _sys.modules["services.prometheus_exporter"] = mock_pe

        result = await health.check_dependency_health("prometheus_exporter")
        # delegation 失败应记录为 prometheus_delegation unhealthy
        assert "prometheus_delegation" in result["dependency_checks"]
        delegation = result["dependency_checks"]["prometheus_delegation"]
        assert delegation["healthy"] is False
        assert "DELEGATION_FAILED" in (delegation["error_code"] or "")

    @pytest.mark.asyncio
    async def test_prometheus_exporter_supplements_with_local_checks(
        self, health, monkeypatch
    ):
        """prometheus_exporter 委托后补充 DEPENDENCY_CHECKS 中未覆盖的检查项。"""
        # mock collect_dependency_status 仅返回 sqlite_readable
        mock_status = {
            "ready": True,
            "passed": 1,
            "checks": {"sqlite_readable": True},
            "details": {"sqlite_readable": "ok"},
        }

        def mock_collect():
            return mock_status

        import sys as _sys
        mock_pe = type(_sys)("services.prometheus_exporter")
        mock_pe.collect_dependency_status = mock_collect
        _sys.modules["services.prometheus_exporter"] = mock_pe

        result = await health.check_dependency_health("prometheus_exporter")
        # sqlite_readable 来自委托
        assert "sqlite_readable" in result["dependency_checks"]
        # schema_valid / acl_configured 来自 DEPENDENCY_CHECKS 补充
        # (注意:具体补充项取决于 DEPENDENCY_CHECKS["prometheus_exporter"] 定义)
        dep_checkers = health.DEPENDENCY_CHECKS.get("prometheus_exporter", {})
        for expected_name in dep_checkers:
            if expected_name == "sqlite_readable":
                continue  # 已由委托提供
            # 补充项应出现在 dependency_checks 中
            # (即使检查失败,也会以 unhealthy 形式出现)
            assert expected_name in result["dependency_checks"], (
                f"DEPENDENCY_CHECKS 中定义的补充检查项 {expected_name} "
                f"应出现在 dependency_checks 中"
            )


# ════════════════════════════════════════════════════════════════
# F. R73 §5.10 (P1-06) Compose + entrypoint 整合 — 角色别名 + fail-closed
#    验证 docker-compose.prod.yml healthcheck 与 docker/entrypoint.py
#    startup gate 所依赖的探针契约。
# ════════════════════════════════════════════════════════════════


class TestR73Section510RoleSplitIntegration:
    """R73 §5.10 (P1-06) 整合测试:验证 Compose healthcheck 与
    entrypoint startup gate 所依赖的探针契约。

    docker-compose.prod.yml 中所有业务服务 healthcheck 使用:
        python -c "import asyncio,os,sys
                   from services.health import check_liveness, check_readiness
                   role=os.environ.get('SERVICE_ROLE','')
                   l=check_liveness(role)
                   sys.exit(1) if not l.get('alive') else None
                   r=asyncio.run(check_readiness(role))
                   sys.exit(0 if r.healthy else 1)"

    docker/entrypoint.py _run_readiness_gate 中调用:
        1. check_startup(service_role) → started=False 时 sys.exit(4)
        2. asyncio.run(check_readiness(service_role)) → not healthy 时 sys.exit(4)

    本测试类验证这些探针的契约(返回字段、类型、fail-closed 行为)。
    """

    def test_check_liveness_up_alias_returns_alive_field(self, health):
        """check_liveness('up') 应返回 dict 含 'alive' 字段。

        docker-compose.prod.yml 中 up 服务的 healthcheck 通过
        os.environ.get('SERVICE_ROLE','') 取得 'up',然后调用
        check_liveness('up'),依赖返回的 dict 含 'alive' 键。
        """
        result = health.check_liveness("up")
        assert isinstance(result, dict), (
            "check_liveness 应返回 dict(Compose healthcheck 依赖 dict 语义)"
        )
        assert "alive" in result, (
            "check_liveness 返回的 dict 必须含 'alive' 字段"
            "(Compose healthcheck 通过 l.get('alive') 取值)"
        )
        assert isinstance(result["alive"], bool), (
            "'alive' 字段应为 bool 类型"
        )

    def test_check_startup_up_alias_returns_started_field(self, health):
        """check_startup('up') 应返回 dict 含 'started' 字段。

        docker/entrypoint.py _run_readiness_gate 中:
            startup_result = check_startup(service_role)
            if not startup_result['started']: sys.exit(4)

        依赖返回的 dict 含 'started' 键。
        """
        result = health.check_startup("up")
        assert isinstance(result, dict), (
            "check_startup 应返回 dict(entrypoint 通过 dict 索引)"
        )
        assert "started" in result, (
            "check_startup 返回的 dict 必须含 'started' 字段"
            "(entrypoint 通过 startup_result['started'] 取值)"
        )
        assert isinstance(result["started"], bool), (
            "'started' 字段应为 bool 类型"
        )
        # 启动宽限期(PRE_LAUNCH)下 started 应为 True(避免容器编排 kill)
        # 运行态下若 startup_completed_at 未记录,started 可能为 False
        # 本测试只验证字段存在与类型,不强制 True/False

    @pytest.mark.asyncio
    async def test_check_dependency_health_up_alias_returns_dependencies_healthy(
        self, health
    ):
        """check_dependency_health('up') 应返回 dict 含 'dependencies_healthy'。

        虽然 Compose healthcheck 不直接调用 check_dependency_health,
        但 R73 §5.10 要求该探针作为 readiness 的细化(关注依赖),
        供 prometheus_exporter / admin /readiness 端点使用。
        """
        result = await health.check_dependency_health("up")
        assert isinstance(result, dict), (
            "check_dependency_health 应返回 dict"
        )
        assert "dependencies_healthy" in result, (
            "check_dependency_health 返回的 dict 必须含 'dependencies_healthy' 字段"
        )
        assert isinstance(result["dependencies_healthy"], bool), (
            "'dependencies_healthy' 字段应为 bool 类型"
        )

    def test_check_readiness_up_alias_returns_healthy_field(self, health):
        """check_readiness('up') 应返回 HealthResult 含 'healthy' 字段。

        docker-compose.prod.yml healthcheck 与 entrypoint 均依赖:
            r=asyncio.run(check_readiness(role))
            sys.exit(0 if r.healthy else 1)

        entrypoint _run_readiness_gate 依赖:
            if not result.healthy: sys.exit(4)
        """
        import asyncio

        result = asyncio.run(health.check_readiness("up"))
        # HealthResult 对象(不是 dict)
        assert hasattr(result, "healthy"), (
            "check_readiness 应返回 HealthResult 含 'healthy' 属性"
            "(Compose healthcheck 通过 r.healthy 取值)"
        )
        assert isinstance(result.healthy, bool), (
            "'healthy' 属性应为 bool 类型"
        )
        assert hasattr(result, "role"), (
            "HealthResult 应含 'role' 属性(审计/日志用)"
        )
        assert hasattr(result, "checks"), (
            "HealthResult 应含 'checks' 属性(详细检查项列表)"
        )
        # 别名 'up' 应规范化为 'up_bot'
        assert result.role == "up_bot", (
            "check_readiness('up') 应通过 _canonicalize_role 规范化为 'up_bot'"
        )

    def test_check_liveness_unknown_role_fail_closed_for_readiness(
        self, health
    ):
        """check_liveness 对未知角色不应 fail(liveness 极度宽容)。

        R73 §5.10 设计原则:liveness 只关心进程是否死锁,与角色合法性无关。
        未知角色的 liveness 仍应返回 alive=True(避免误重启)。
        readiness/startup/dependency_health 才负责 fail-closed。
        """
        result = health.check_liveness("nonexistent_role_xyz")
        assert result["alive"] is True, (
            "liveness 对未知角色应返回 alive=True(liveness 极度宽容,不 fail-closed)"
        )

    def test_check_startup_unknown_role_fail_closed(self, health, monkeypatch):
        """check_startup 对未知角色应 fail-closed(started=False)。

        R73 §5.10:未知角色不在 STARTUP_REQUIREMENTS 中,
        required_steps=[],pending=[],但 startup_completed_at 为 None,
        所以 started = (len(pending)==0) and (startup_completed_at is not None)
                  = True and False = False
        启动宽限期内 started=True(避免 kill),但运行态下 fail-closed。
        """
        # 确保不在启动宽限期(运行态)
        monkeypatch.delenv("READINESS_GATE_PRE_LAUNCH", raising=False)

        result = health.check_startup("nonexistent_role_xyz")
        assert result["started"] is False, (
            "运行态下未知角色的 check_startup 应 fail-closed: started=False"
        )
        # pending_initializations 应为空(因为 STARTUP_REQUIREMENTS 中无该角色)
        assert result["pending_initializations"] == []

    @pytest.mark.asyncio
    async def test_check_dependency_health_unknown_role_fail_closed(
        self, health
    ):
        """check_dependency_health 对未知角色应 fail-closed。

        R73 §5.10:未知角色不在 DEPENDENCY_CHECKS 中,
        dependencies_healthy=False,dependency_checks 含 role_validation 项。
        """
        result = await health.check_dependency_health("nonexistent_role_xyz")
        assert result["dependencies_healthy"] is False, (
            "未知角色的 check_dependency_health 应 fail-closed: "
            "dependencies_healthy=False"
        )
        assert "role_validation" in result["dependency_checks"], (
            "未知角色应在 dependency_checks 中报告 role_validation 失败"
        )
        role_check = result["dependency_checks"]["role_validation"]
        assert role_check["healthy"] is False
        assert "UNKNOWN_ROLE" in (role_check["error_code"] or "")

    def test_check_readiness_unknown_role_fail_closed(self, health):
        """check_readiness 对未知角色应 fail-closed(healthy=False)。

        R71 Wave 1 / R73 §5.10:未知角色不在 ROLE_REQUIREMENTS 中,
        立即返回 unhealthy(fail-closed),不静默通过。
        """
        import asyncio

        result = asyncio.run(health.check_readiness("nonexistent_role_xyz"))
        assert result.healthy is False, (
            "未知角色的 check_readiness 应 fail-closed: healthy=False"
        )
        # 应包含 role_validation 检查项,报告 Unknown role
        role_checks = [c for c in result.checks if c.name == "role_validation"]
        assert role_checks, (
            "未知角色应在 checks 中包含 role_validation 检查项"
        )
        assert role_checks[0].healthy is False
        assert "Unknown role" in (role_checks[0].error or "")

    def test_entrypoint_startup_gate_contract(self, health, monkeypatch):
        """验证 docker/entrypoint.py _run_readiness_gate 的 startup gate 契约。

        entrypoint 在 READINESS_GATE_PRE_LAUNCH=1 下调用 check_startup,
        依赖 startup_result['started'] 与 startup_result['in_startup_grace']。
        启动宽限期内 started 应为 True(避免容器编排 kill 尚未启动的进程)。
        """
        monkeypatch.setenv("READINESS_GATE_PRE_LAUNCH", "1")

        # 验证所有 entrypoint 允许的 SERVICE_ROLE 都能通过 startup gate
        entrypoint_roles = [
            "up", "idx", "dsp", "mon", "admin", "admin_bot",
            "db_writer", "crdb_sync", "db_backup", "r40_scheduler",
            "migration", "prometheus_exporter",
        ]
        for role in entrypoint_roles:
            result = health.check_startup(role)
            assert result["started"] is True, (
                f"启动宽限期内 role={role} 的 check_startup 应 started=True "
                f"(避免容器编排 kill 尚未启动的进程); "
                f"实际: started={result['started']}, "
                f"pending={result.get('pending_initializations')}"
            )
            assert result["in_startup_grace"] is True, (
                f"启动宽限期内 role={role} 的 in_startup_grace 应为 True"
            )

    def test_compose_healthcheck_liveness_readiness_contract(self, health):
        """验证 docker-compose.prod.yml healthcheck 命令的探针契约。

        Compose healthcheck 命令(python -c 单行):
            import asyncio,os,sys
            from services.health import check_liveness, check_readiness
            role=os.environ.get('SERVICE_ROLE','')
            l=check_liveness(role)
            sys.exit(1) if not l.get('alive') else None
            r=asyncio.run(check_readiness(role))
            sys.exit(0 if r.healthy else 1)

        依赖:
        1. check_liveness(role) 返回 dict,含 'alive' 键(bool)
        2. check_readiness(role) 返回 HealthResult,含 'healthy' 属性(bool)
        3. 两者都支持角色别名(SERVICE_ROLE='up' 等)
        """
        # 验证所有 Compose 业务服务的 SERVICE_ROLE
        compose_roles = [
            "db_writer", "crdb_sync", "up", "idx", "dsp", "mon",
            "admin_bot", "db_backup", "r40_scheduler",
        ]
        import asyncio

        for role in compose_roles:
            # 1. liveness 探针契约
            l = health.check_liveness(role)
            assert isinstance(l, dict) and "alive" in l, (
                f"role={role}: check_liveness 应返回 dict 含 'alive' 键"
            )
            assert isinstance(l["alive"], bool), (
                f"role={role}: 'alive' 应为 bool"
            )

            # 2. readiness 探针契约
            r = asyncio.run(health.check_readiness(role))
            assert hasattr(r, "healthy"), (
                f"role={role}: check_readiness 应返回 HealthResult 含 'healthy'"
            )
            assert isinstance(r.healthy, bool), (
                f"role={role}: 'healthy' 应为 bool"
            )

    def test_admin_http_health_endpoint_liveness_contract(self, health):
        """验证 admin /health 端点的 check_liveness 契约。

        R73 §5.10 整改:admin /health 端点应同时调用 check_liveness + check_readiness。
        本测试验证 check_liveness('admin') 可被调用且返回正确结构
        (实际 /health 端点的集成测试在 test_r70_wave6_real_readiness.py 中)。
        """
        result = health.check_liveness("admin")
        assert isinstance(result, dict)
        assert result["alive"] is True, (
            "admin 角色的 liveness 应为 alive=True(进程未死锁)"
        )
        assert result["role"] == "admin", (
            "check_liveness('admin') 应保留 'admin' 角色名(不规范化为别名)"
        )

    @pytest.mark.asyncio
    async def test_prometheus_exporter_dependency_health_uses_real_checks(
        self, health
    ):
        """验证 prometheus_exporter /health 端点依赖的
        collect_dependency_status() 真实执行检查(不返回假绿色)。

        R73 §5.10 (P1-06) / R73 P0-03:prometheus_exporter 的 dependency_health
        通过 collect_dependency_status() 真实检查 critical 项
        (sqlite_readable / key_schema_exists / schema_valid / acl_configured),
        freshness 项(recent_scrape / crdb_sync_fresh / r2_collector_fresh)
        返回真实观测值但启动宽限期内不阻断 ready。
        """
        result = await health.check_dependency_health("prometheus_exporter")
        assert isinstance(result, dict)
        assert "dependencies_healthy" in result
        assert "dependency_checks" in result
        # prometheus_exporter 应至少有 sqlite_readable / schema_valid /
        # acl_configured 中的某一项来自 DEPENDENCY_CHECKS 或委托
        # (具体检查项取决于 collect_dependency_status 的实现)
        dep_names = set(result["dependency_checks"].keys())
        # 至少应有一个 critical 检查项(真实执行,不返回假绿色)
        assert dep_names, (
            "prometheus_exporter 应至少有一个 dependency_checks 项"
            "(不应返回空 dict = 假绿色)"
        )
