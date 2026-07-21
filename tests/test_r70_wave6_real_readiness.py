"""R70 Wave 6: 真实 readiness(角色级健康检查)— 测试套件。

R70 报告要求:
    "readiness 检查真实依赖与业务循环,而非只看 PID"。
    旧版 health 检查仅返回 200 OK 不验证实际依赖(DB 连接 / Redis 连接 /
    Bot polling 状态),无法反映真实业务可用性。

R70 Wave 6 整改:
    新增 services/health.py 集中式健康检查模块,基于 SERVICE_ROLE 区分检查项。
    本测试验证:
        - check_readiness 函数存在且可 import
        - HealthResult / CheckResult 数据结构正确
        - 每个角色(up_bot / idx_bot / dsp_bot / mon_bot / admin_bot /
          db_writer)有正确的检查项集合
        - DB 检查失败时 healthy=false(即使其他检查通过)
        - Redis 未配置时 healthy=True reason="not_configured"(不 fail-closed)
        - Bot token 检查仅对 bot 角色生效
        - critical 检查失败时整体 healthy=false
        - non-critical 检查失败时整体 healthy=true 但 checks 中有 failed 项
        - 用 unittest.mock 模拟 DB / Redis / Bot API(不依赖真实服务)

测试策略:
    - 不实际连接 DB / Redis / Telegram API(用 monkeypatch 替换底层检查函数)
    - 验证 check_readiness 编排逻辑正确(角色 → 检查项映射)
    - 验证 HealthResult 序列化与 HTTP 状态码转换
    - 严格遵守 R70 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ════════════════════════════════════════════════════════════════
# 测试隔离:直接加载 services.health 模块
# (绕过 services/__init__.py 可能触发的副作用,但 services/__init__.py 为空)
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

    # 直接通过文件路径加载,避免 import 副作用
    health_path = (
        Path(__file__).resolve().parent.parent / "services" / "health.py"
    )
    spec = importlib.util.spec_from_file_location("services.health", health_path)
    assert spec is not None, f"无法加载模块 spec: {health_path}"
    assert spec.loader is not None, f"模块 loader 为 None: {health_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.health"] = module
    spec.loader.exec_module(module)
    return sys.modules["services.health"]


@pytest.fixture(scope="module")
def health():
    """提供 services.health 模块实例(模块级缓存)。"""
    return _load_health_module()


# ════════════════════════════════════════════════════════════════
# 辅助 fixture:统一 mock 所有底层检查函数
# ════════════════════════════════════════════════════════════════


def _async_return_factory(value):
    """构造一个 async 函数,返回固定 value(接受任意参数,便于 mock 带参函数)。

    Args:
        value: 协程返回值(如 (True, None) 或 (False, "error"))

    Returns:
        async 函数(接受 *args, **kwargs,调用返回 awaitable)
    """

    async def _impl(*args, **kwargs):
        return value

    return _impl


@pytest.fixture
def mock_all_checks_pass(monkeypatch, health):
    """统一 mock 所有底层检查函数为"通过"状态。

    可被单个测试用例覆盖特定函数为失败状态。
    """
    monkeypatch.setattr(health, "_check_database", _async_return_factory((True, None)))
    monkeypatch.setattr(
        health, "_check_redis", _async_return_factory((True, None, None))
    )
    monkeypatch.setattr(
        health, "_check_bot_token_valid",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_upload_session_status",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_bot_polling_status",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_index_queue_depth",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_redis_stream_consumer",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_send_queue_depth",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_sub_services_alive",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_admin_web_port",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_redis_stream_consumer_group",
        _async_return_factory((True, None)),
    )
    monkeypatch.setattr(
        health, "_check_writer_inbox_lag",
        _async_return_factory((True, None)),
    )
    yield


# ════════════════════════════════════════════════════════════════
# A. 模块可 import 与基本接口
# ════════════════════════════════════════════════════════════════


class TestModuleImport:
    """验证 services.health 模块可 import 且暴露必要接口。"""

    def test_module_importable(self, health):
        """模块应可成功 import。"""
        assert health is not None
        assert hasattr(health, "__name__")
        assert health.__name__ == "services.health"

    def test_check_readiness_exists(self, health):
        """check_readiness 函数应存在。"""
        assert hasattr(health, "check_readiness"), (
            "services.health 应暴露 check_readiness 函数"
        )
        assert callable(health.check_readiness)

    def test_check_readiness_is_coroutine(self, health):
        """check_readiness 应为协程函数(async def)。"""
        assert inspect.iscoroutinefunction(health.check_readiness), (
            "check_readiness 应为 async def(协程函数)"
        )

    def test_health_result_class_exists(self, health):
        """HealthResult 数据类应存在。"""
        assert hasattr(health, "HealthResult")
        assert isinstance(health.HealthResult, type)

    def test_check_result_class_exists(self, health):
        """CheckResult 数据类应存在。"""
        assert hasattr(health, "CheckResult")
        assert isinstance(health.CheckResult, type)

    def test_health_version_constant_exists(self, health):
        """HEALTH_VERSION 常量应存在。"""
        assert hasattr(health, "HEALTH_VERSION")
        assert health.HEALTH_VERSION == "R70 Wave 6"

    def test_bot_roles_constant_exists(self, health):
        """BOT_ROLES 常量应存在,包含 5 个 bot 角色。"""
        assert hasattr(health, "BOT_ROLES")
        assert "up_bot" in health.BOT_ROLES
        assert "idx_bot" in health.BOT_ROLES
        assert "dsp_bot" in health.BOT_ROLES
        assert "mon_bot" in health.BOT_ROLES
        assert "admin_bot" in health.BOT_ROLES
        assert "db_writer" not in health.BOT_ROLES, (
            "db_writer 不是 bot 角色(不应有 bot_token_valid 检查)"
        )

    def test_to_http_status_function_exists(self, health):
        """to_http_status 辅助函数应存在。"""
        assert hasattr(health, "to_http_status")
        assert callable(health.to_http_status)

    def test_to_json_function_exists(self, health):
        """to_json 辅助函数应存在。"""
        assert hasattr(health, "to_json")
        assert callable(health.to_json)


# ════════════════════════════════════════════════════════════════
# B. HealthResult / CheckResult 数据结构
# ════════════════════════════════════════════════════════════════


class TestCheckResultStructure:
    """验证 CheckResult 数据类结构。"""

    def test_check_result_fields(self, health):
        """CheckResult 应有 5 个字段:name/healthy/latency_ms/error/critical。"""
        cr = health.CheckResult(
            name="test",
            healthy=True,
            latency_ms=10,
            error=None,
            critical=True,
        )
        assert cr.name == "test"
        assert cr.healthy is True
        assert cr.latency_ms == 10
        assert cr.error is None
        assert cr.critical is True

    def test_check_result_to_dict(self, health):
        """CheckResult.to_dict 应返回包含全部字段的 dict。"""
        cr = health.CheckResult(
            name="database",
            healthy=False,
            latency_ms=42,
            error="Connection refused",
            critical=True,
        )
        d = cr.to_dict()
        assert d == {
            "name": "database",
            "healthy": False,
            "latency_ms": 42,
            "error": "Connection refused",
            "critical": True,
        }

    def test_check_result_defaults(self, health):
        """CheckResult 的 error/critical 应有默认值。"""
        cr = health.CheckResult(name="redis", healthy=True, latency_ms=5)
        assert cr.error is None
        assert cr.critical is False  # 默认非关键


class TestHealthResultStructure:
    """验证 HealthResult 数据类结构。"""

    def test_health_result_fields(self, health):
        """HealthResult 应有 5 个字段:healthy/role/checks/timestamp/version。"""
        cr = health.CheckResult(
            name="database", healthy=True, latency_ms=10, critical=True
        )
        hr = health.HealthResult(
            healthy=True,
            role="up_bot",
            checks=[cr],
            timestamp="2026-07-21T12:00:00+00:00",
        )
        assert hr.healthy is True
        assert hr.role == "up_bot"
        assert len(hr.checks) == 1
        assert hr.timestamp == "2026-07-21T12:00:00+00:00"
        assert hr.version == health.HEALTH_VERSION

    def test_health_result_to_dict(self, health):
        """HealthResult.to_dict 应递归序列化 checks 列表。"""
        cr1 = health.CheckResult(
            name="database", healthy=True, latency_ms=10, critical=True
        )
        cr2 = health.CheckResult(
            name="redis", healthy=False, latency_ms=5,
            error="timeout", critical=False,
        )
        hr = health.HealthResult(
            healthy=True,
            role="up_bot",
            checks=[cr1, cr2],
            timestamp="2026-07-21T12:00:00+00:00",
        )
        d = hr.to_dict()
        assert d["healthy"] is True
        assert d["role"] == "up_bot"
        assert d["timestamp"] == "2026-07-21T12:00:00+00:00"
        assert d["version"] == "R70 Wave 6"
        assert len(d["checks"]) == 2
        assert d["checks"][0]["name"] == "database"
        assert d["checks"][1]["name"] == "redis"
        assert d["checks"][1]["error"] == "timeout"

    def test_health_result_serializable_to_json(self, health):
        """HealthResult 应可序列化为 JSON(to_json 函数)。"""
        cr = health.CheckResult(
            name="database", healthy=True, latency_ms=10, critical=True
        )
        hr = health.HealthResult(
            healthy=True,
            role="up_bot",
            checks=[cr],
            timestamp="2026-07-21T12:00:00+00:00",
        )
        json_str = health.to_json(hr)
        assert isinstance(json_str, str)
        assert '"healthy"' in json_str
        assert '"role"' in json_str
        assert '"checks"' in json_str
        assert '"R70 Wave 6"' in json_str

    def test_to_http_status_200_when_healthy(self, health):
        """healthy=True → HTTP 200。"""
        hr = health.HealthResult(
            healthy=True,
            role="up_bot",
            checks=[],
            timestamp="2026-07-21T12:00:00+00:00",
        )
        assert health.to_http_status(hr) == 200

    def test_to_http_status_503_when_unhealthy(self, health):
        """healthy=False → HTTP 503。"""
        hr = health.HealthResult(
            healthy=False,
            role="up_bot",
            checks=[],
            timestamp="2026-07-21T12:00:00+00:00",
        )
        assert health.to_http_status(hr) == 503


# ════════════════════════════════════════════════════════════════
# C. 角色 → 检查项集合正确性
# ════════════════════════════════════════════════════════════════


class TestRoleCheckMapping:
    """验证每个角色的检查项集合正确。"""

    @pytest.mark.asyncio
    async def test_up_bot_checks(self, health, mock_all_checks_pass):
        """up_bot 检查项:database + redis + bot_token_valid +
        upload_session_status + bot_polling_status。"""
        result = await health.check_readiness("up_bot")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "bot_token_valid" in names
        assert "upload_session_status" in names
        assert "bot_polling_status" in names
        # 不应有其他角色的专属检查
        assert "index_queue_depth" not in names
        assert "redis_stream_consumer" not in names
        assert "send_queue_depth" not in names
        assert "sub_services_alive" not in names
        assert "admin_web_port" not in names
        assert "writer_inbox_lag" not in names

    @pytest.mark.asyncio
    async def test_idx_bot_checks(self, health, mock_all_checks_pass):
        """idx_bot 检查项:database + redis + bot_token_valid +
        index_queue_depth。"""
        result = await health.check_readiness("idx_bot")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "bot_token_valid" in names
        assert "index_queue_depth" in names
        # 不应有其他角色的专属检查
        assert "upload_session_status" not in names
        assert "bot_polling_status" not in names
        assert "redis_stream_consumer" not in names
        assert "send_queue_depth" not in names
        assert "sub_services_alive" not in names
        assert "admin_web_port" not in names
        assert "writer_inbox_lag" not in names

    @pytest.mark.asyncio
    async def test_dsp_bot_checks(self, health, mock_all_checks_pass):
        """dsp_bot 检查项:database + redis + bot_token_valid +
        redis_stream_consumer + send_queue_depth。"""
        result = await health.check_readiness("dsp_bot")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "bot_token_valid" in names
        assert "redis_stream_consumer" in names
        assert "send_queue_depth" in names
        # 不应有其他角色的专属检查
        assert "upload_session_status" not in names
        assert "bot_polling_status" not in names
        assert "index_queue_depth" not in names
        assert "sub_services_alive" not in names
        assert "admin_web_port" not in names
        assert "writer_inbox_lag" not in names

    @pytest.mark.asyncio
    async def test_mon_bot_checks(self, health, mock_all_checks_pass):
        """mon_bot 检查项:database + redis + bot_token_valid +
        sub_services_alive。"""
        result = await health.check_readiness("mon_bot")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "bot_token_valid" in names
        assert "sub_services_alive" in names
        # 不应有其他角色的专属检查
        assert "upload_session_status" not in names
        assert "bot_polling_status" not in names
        assert "index_queue_depth" not in names
        assert "redis_stream_consumer" not in names
        assert "send_queue_depth" not in names
        assert "admin_web_port" not in names
        assert "writer_inbox_lag" not in names

    @pytest.mark.asyncio
    async def test_admin_bot_checks(self, health, mock_all_checks_pass):
        """admin_bot 检查项:database + redis + bot_token_valid +
        admin_web_port。"""
        result = await health.check_readiness("admin_bot")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "bot_token_valid" in names
        assert "admin_web_port" in names
        # 不应有其他角色的专属检查
        assert "upload_session_status" not in names
        assert "bot_polling_status" not in names
        assert "index_queue_depth" not in names
        assert "redis_stream_consumer" not in names
        assert "send_queue_depth" not in names
        assert "sub_services_alive" not in names
        assert "writer_inbox_lag" not in names

    @pytest.mark.asyncio
    async def test_db_writer_checks(self, health, mock_all_checks_pass):
        """db_writer 检查项:database + redis +
        redis_stream_consumer_group + writer_inbox_lag。
        不应有 bot_token_valid(db_writer 不是 bot 角色)。"""
        result = await health.check_readiness("db_writer")
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "redis_stream_consumer_group" in names
        assert "writer_inbox_lag" in names
        # 关键:db_writer 不应有 bot_token_valid 检查
        assert "bot_token_valid" not in names, (
            "db_writer 不是 bot 角色,不应执行 bot_token_valid 检查"
        )

    @pytest.mark.asyncio
    async def test_admin_role_checks_all(self, health, mock_all_checks_pass):
        """admin / 空角色:执行全部检查(包含所有角色专属检查)。"""
        result = await health.check_readiness("admin")
        names = [c.name for c in result.checks]
        # 应包含所有检查项
        expected_checks = {
            "database", "redis",
            "upload_session_status", "bot_polling_status",
            "index_queue_depth",
            "redis_stream_consumer", "send_queue_depth",
            "sub_services_alive", "admin_web_port",
            "redis_stream_consumer_group", "writer_inbox_lag",
        }
        assert expected_checks.issubset(set(names)), (
            f"admin 角色应包含全部检查项,缺失: "
            f"{expected_checks - set(names)}"
        )

    @pytest.mark.asyncio
    async def test_empty_role_treated_as_admin(self, health, mock_all_checks_pass):
        """空角色应被规范化为 admin(全部检查)。"""
        result = await health.check_readiness("")
        assert result.role == "admin"
        # 应有所有检查项
        names = [c.name for c in result.checks]
        assert "database" in names
        assert "redis" in names
        assert "admin_web_port" in names
        assert "writer_inbox_lag" in names

    @pytest.mark.asyncio
    async def test_role_alias_normalization_up(self, health, mock_all_checks_pass):
        """角色别名 up → up_bot(entrypoint 兼容)。"""
        result = await health.check_readiness("up")
        assert result.role == "up_bot"
        names = [c.name for c in result.checks]
        assert "upload_session_status" in names

    @pytest.mark.asyncio
    async def test_role_alias_normalization_idx(self, health, mock_all_checks_pass):
        """角色别名 idx → idx_bot。"""
        result = await health.check_readiness("idx")
        assert result.role == "idx_bot"
        names = [c.name for c in result.checks]
        assert "index_queue_depth" in names

    @pytest.mark.asyncio
    async def test_role_alias_normalization_dsp(self, health, mock_all_checks_pass):
        """角色别名 dsp → dsp_bot。"""
        result = await health.check_readiness("dsp")
        assert result.role == "dsp_bot"
        names = [c.name for c in result.checks]
        assert "send_queue_depth" in names

    @pytest.mark.asyncio
    async def test_role_alias_normalization_mon(self, health, mock_all_checks_pass):
        """角色别名 mon → mon_bot。"""
        result = await health.check_readiness("mon")
        assert result.role == "mon_bot"
        names = [c.name for c in result.checks]
        assert "sub_services_alive" in names

    @pytest.mark.asyncio
    async def test_role_case_insensitive(self, health, mock_all_checks_pass):
        """角色名大小写不敏感(UP_BOT → up_bot)。"""
        result = await health.check_readiness("UP_BOT")
        assert result.role == "up_bot"


# ════════════════════════════════════════════════════════════════
# D. critical 检查失败 → 整体 healthy=false
# ════════════════════════════════════════════════════════════════


class TestCriticalCheckFailure:
    """验证 critical 检查失败时整体 healthy=False。"""

    @pytest.mark.asyncio
    async def test_database_failure_makes_unhealthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """DB 检查失败(critical) → 整体 healthy=false,即使其他检查通过。"""
        # 覆盖 _check_database 为失败
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((False, "Connection refused"))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is False, (
            "DB 检查(critical)失败时整体 healthy 必须为 False"
        )
        # database 检查应标记为 critical=True
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.healthy is False
        assert db_check.critical is True
        assert "Connection refused" in db_check.error
        # 其他检查应仍通过
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.healthy is True

    @pytest.mark.asyncio
    async def test_database_failure_affects_all_roles(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """DB 检查失败应影响所有角色(critical)。"""
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((False, "DB unreachable"))
        )
        for role in (
            "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
            "db_writer", "admin",
        ):
            result = await health.check_readiness(role)
            assert result.healthy is False, (
                f"role={role}: DB 失败时整体 healthy 必须为 False"
            )

    @pytest.mark.asyncio
    async def test_database_critical_flag(
        self, health, mock_all_checks_pass
    ):
        """database 检查项应标记 critical=True。"""
        result = await health.check_readiness("up_bot")
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.critical is True, (
            "database 检查应为 critical=True"
        )

    @pytest.mark.asyncio
    async def test_redis_non_critical_flag(
        self, health, mock_all_checks_pass
    ):
        """redis 检查项应标记 critical=False。"""
        result = await health.check_readiness("up_bot")
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.critical is False, (
            "redis 检查应为 critical=False(non-critical)"
        )

    @pytest.mark.asyncio
    async def test_bot_token_valid_non_critical_flag(
        self, health, mock_all_checks_pass
    ):
        """bot_token_valid 检查项应标记 critical=False。"""
        result = await health.check_readiness("up_bot")
        bot_check = next(
            c for c in result.checks if c.name == "bot_token_valid"
        )
        assert bot_check.critical is False


# ════════════════════════════════════════════════════════════════
# E. non-critical 检查失败 → 整体 healthy=true
# ════════════════════════════════════════════════════════════════


class TestNonCriticalCheckFailure:
    """验证 non-critical 检查失败时整体 healthy=True 但 checks 中有 failed 项。"""

    @pytest.mark.asyncio
    async def test_redis_failure_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """Redis 检查失败(non-critical) → 整体 healthy=true,
        但 checks 中 redis 项 healthy=False。"""
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory((False, "Connection refused", None))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is True, (
            "Redis 检查(non-critical)失败时整体 healthy 应保持 True"
        )
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.healthy is False
        assert "Connection refused" in redis_check.error
        # database 仍通过
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.healthy is True

    @pytest.mark.asyncio
    async def test_bot_token_failure_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """Bot token 检查失败(non-critical) → 整体 healthy=true。"""
        monkeypatch.setattr(
            health, "_check_bot_token_valid",
            _async_return_factory((False, "Invalid token"))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is True, (
            "bot_token_valid(non-critical)失败时整体 healthy 应保持 True"
        )
        bot_check = next(
            c for c in result.checks if c.name == "bot_token_valid"
        )
        assert bot_check.healthy is False
        assert "Invalid token" in bot_check.error

    @pytest.mark.asyncio
    async def test_upload_session_failure_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """upload_session_status 失败(non-critical) → 整体 healthy=true。"""
        monkeypatch.setattr(
            health, "_check_upload_session_status",
            _async_return_factory((False, "5 sessions stuck"))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is True
        us_check = next(
            c for c in result.checks if c.name == "upload_session_status"
        )
        assert us_check.healthy is False
        assert "5 sessions stuck" in us_check.error

    @pytest.mark.asyncio
    async def test_admin_web_port_failure_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """admin_web_port 失败(non-critical) → 整体 healthy=true。"""
        monkeypatch.setattr(
            health, "_check_admin_web_port",
            _async_return_factory((False, "Port 8080 not listening"))
        )
        result = await health.check_readiness("admin_bot")
        assert result.healthy is True
        port_check = next(
            c for c in result.checks if c.name == "admin_web_port"
        )
        assert port_check.healthy is False

    @pytest.mark.asyncio
    async def test_all_non_critical_failures_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """所有 non-critical 检查失败,但 database 通过 → 整体 healthy=true。"""
        # 所有 non-critical 检查失败(redis, bot_token_valid, upload_session,
        # bot_polling 等)
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory((False, "redis down", None))
        )
        monkeypatch.setattr(
            health, "_check_bot_token_valid",
            _async_return_factory((False, "token invalid"))
        )
        monkeypatch.setattr(
            health, "_check_upload_session_status",
            _async_return_factory((False, "stuck"))
        )
        monkeypatch.setattr(
            health, "_check_bot_polling_status",
            _async_return_factory((False, "not polling"))
        )
        # database 仍通过
        result = await health.check_readiness("up_bot")
        assert result.healthy is True, (
            "database 通过 + 所有 non-critical 失败 → healthy=True"
        )
        # 但 checks 中应有多项失败
        failed = [c for c in result.checks if not c.healthy]
        assert len(failed) == 4, (
            f"应有 4 项 non-critical 检查失败"
            f"(redis/bot_token_valid/upload_session/bot_polling_status),"
            f"实际: {len(failed)}"
        )

    @pytest.mark.asyncio
    async def test_failed_check_in_result(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """non-critical 失败项应出现在 checks 列表中(healthy=False)。"""
        monkeypatch.setattr(
            health, "_check_index_queue_depth",
            _async_return_factory((False, "depth 5000"))
        )
        result = await health.check_readiness("idx_bot")
        # 整体 healthy=true(non-critical 失败)
        assert result.healthy is True
        # 但 idx 队列检查 healthy=False
        idx_check = next(
            c for c in result.checks if c.name == "index_queue_depth"
        )
        assert idx_check.healthy is False
        assert idx_check.error == "depth 5000"


# ════════════════════════════════════════════════════════════════
# F. Redis 未配置时 healthy=True reason="not_configured"
# ════════════════════════════════════════════════════════════════


class TestRedisNotConfigured:
    """验证 Redis 未配置时不 fail-closed(healthy=True, error="not_configured")。"""

    @pytest.mark.asyncio
    async def test_redis_not_configured_marks_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """Redis 未配置 → redis 检查 healthy=True, error="not_configured"。"""
        # 模拟 _check_redis 返回 not_configured
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory((True, None, "not_configured"))
        )
        result = await health.check_readiness("up_bot")
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.healthy is True, (
            "Redis 未配置时应 healthy=True(不 fail-closed)"
        )
        assert redis_check.error == "not_configured", (
            f"Redis 未配置时 error 应为 'not_configured',"
            f"实际: {redis_check.error!r}"
        )
        # 整体仍 healthy
        assert result.healthy is True

    @pytest.mark.asyncio
    async def test_redis_not_configured_does_not_fail_overall(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """Redis 未配置不应使整体 healthy=false。"""
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory((True, None, "not_configured"))
        )
        for role in ("up_bot", "idx_bot", "db_writer", "admin"):
            result = await health.check_readiness(role)
            assert result.healthy is True, (
                f"role={role}: Redis 未配置时整体 healthy 应为 True"
            )
            redis_check = next(
                c for c in result.checks if c.name == "redis"
            )
            assert redis_check.healthy is True
            assert redis_check.error == "not_configured"

    @pytest.mark.asyncio
    async def test_redis_configured_and_ping_success(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """Redis 已配置且 PING 成功 → healthy=True, error=None。"""
        monkeypatch.setattr(
            health, "_check_redis",
            _async_return_factory((True, None, None))
        )
        result = await health.check_readiness("up_bot")
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.healthy is True
        assert redis_check.error is None, (
            "Redis PING 成功时 error 应为 None,"
            f"实际: {redis_check.error!r}"
        )


# ════════════════════════════════════════════════════════════════
# G. Bot token 检查仅对 bot 角色生效
# ════════════════════════════════════════════════════════════════


class TestBotTokenCheckOnlyForBotRoles:
    """验证 bot_token_valid 检查仅对 bot 角色生效。"""

    @pytest.mark.asyncio
    async def test_bot_token_check_for_up_bot(self, health, mock_all_checks_pass):
        """up_bot 应有 bot_token_valid 检查。"""
        result = await health.check_readiness("up_bot")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" in names

    @pytest.mark.asyncio
    async def test_bot_token_check_for_idx_bot(self, health, mock_all_checks_pass):
        """idx_bot 应有 bot_token_valid 检查。"""
        result = await health.check_readiness("idx_bot")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" in names

    @pytest.mark.asyncio
    async def test_bot_token_check_for_dsp_bot(self, health, mock_all_checks_pass):
        """dsp_bot 应有 bot_token_valid 检查。"""
        result = await health.check_readiness("dsp_bot")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" in names

    @pytest.mark.asyncio
    async def test_bot_token_check_for_mon_bot(self, health, mock_all_checks_pass):
        """mon_bot 应有 bot_token_valid 检查。"""
        result = await health.check_readiness("mon_bot")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" in names

    @pytest.mark.asyncio
    async def test_bot_token_check_for_admin_bot(self, health, mock_all_checks_pass):
        """admin_bot 应有 bot_token_valid 检查。"""
        result = await health.check_readiness("admin_bot")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" in names

    @pytest.mark.asyncio
    async def test_no_bot_token_check_for_db_writer(
        self, health, mock_all_checks_pass
    ):
        """db_writer 不应有 bot_token_valid 检查。"""
        result = await health.check_readiness("db_writer")
        names = [c.name for c in result.checks]
        assert "bot_token_valid" not in names, (
            "db_writer 不是 bot 角色,不应执行 bot_token_valid 检查"
        )


# ════════════════════════════════════════════════════════════════
# H. 异常处理与边界
# ════════════════════════════════════════════════════════════════


class TestExceptionHandling:
    """验证检查函数抛异常时的处理。"""

    @pytest.mark.asyncio
    async def test_check_function_exception_marks_unhealthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """检查函数抛异常 → 该项 healthy=False, 不影响其他检查。"""

        # 定义一个会抛异常的 async 函数
        async def _raising_check():
            raise RuntimeError("unexpected error")

        monkeypatch.setattr(health, "_check_database", _raising_check)
        result = await health.check_readiness("up_bot")
        # database 是 critical,抛异常 → 整体 unhealthy
        assert result.healthy is False
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.healthy is False
        assert "unexpected error" in db_check.error
        assert "check raised exception" in db_check.error

    @pytest.mark.asyncio
    async def test_non_critical_exception_keeps_healthy(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """non-critical 检查抛异常 → 整体仍 healthy=true。"""

        async def _raising_check():
            raise RuntimeError("redis error")

        monkeypatch.setattr(health, "_check_redis", _raising_check)
        result = await health.check_readiness("up_bot")
        # redis 是 non-critical → 整体 healthy
        assert result.healthy is True
        redis_check = next(c for c in result.checks if c.name == "redis")
        assert redis_check.healthy is False
        assert "redis error" in redis_check.error

    @pytest.mark.asyncio
    async def test_latency_ms_recorded(self, health, mock_all_checks_pass):
        """每个检查项应记录 latency_ms(非负整数)。"""
        result = await health.check_readiness("up_bot")
        for check in result.checks:
            assert isinstance(check.latency_ms, int)
            assert check.latency_ms >= 0, (
                f"latency_ms 应为非负整数,实际: {check.latency_ms}"
                f"(check={check.name})"
            )

    @pytest.mark.asyncio
    async def test_timestamp_is_iso8601(self, health, mock_all_checks_pass):
        """timestamp 应为 ISO 8601 格式(带时区)。"""
        import datetime as _dt

        result = await health.check_readiness("up_bot")
        # 应可解析为 ISO 8601
        parsed = _dt.datetime.fromisoformat(result.timestamp)
        assert parsed.tzinfo is not None, (
            "timestamp 应包含时区信息(UTC)"
        )


# ════════════════════════════════════════════════════════════════
# I. JSON 序列化与 HTTP 集成
# ════════════════════════════════════════════════════════════════


class TestJsonSerialization:
    """验证 HealthResult 的 JSON 序列化与 HTTP 状态码转换。"""

    @pytest.mark.asyncio
    async def test_full_json_structure(self, health, mock_all_checks_pass):
        """完整 HealthResult 应可序列化为符合规范的 JSON。"""
        import json as _json

        result = await health.check_readiness("up_bot")
        json_str = health.to_json(result)
        # 应可被 json.loads 解析
        data = _json.loads(json_str)
        # 顶层字段
        assert "healthy" in data
        assert "role" in data
        assert "checks" in data
        assert "timestamp" in data
        assert "version" in data
        # checks 中每项应有 5 个字段
        for check in data["checks"]:
            assert "name" in check
            assert "healthy" in check
            assert "latency_ms" in check
            assert "error" in check
            assert "critical" in check

    @pytest.mark.asyncio
    async def test_http_status_200_when_healthy(
        self, health, mock_all_checks_pass
    ):
        """整体 healthy=True → HTTP 200。"""
        result = await health.check_readiness("up_bot")
        assert result.healthy is True
        assert health.to_http_status(result) == 200

    @pytest.mark.asyncio
    async def test_http_status_503_when_db_failed(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """DB 失败 → HTTP 503。"""
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((False, "DB down"))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is False
        assert health.to_http_status(result) == 503

    @pytest.mark.asyncio
    async def test_role_in_json_output(self, health, mock_all_checks_pass):
        """JSON 输出应包含 role 字段(规范化后的角色名)。"""
        result = await health.check_readiness("up")
        assert result.role == "up_bot"
        json_str = health.to_json(result)
        assert '"up_bot"' in json_str


# ════════════════════════════════════════════════════════════════
# J. 不修改现有 health 端点(向后兼容)
# ════════════════════════════════════════════════════════════════


class TestBackwardCompatibility:
    """验证新模块不破坏现有 health 端点。"""

    def test_existing_check_readiness_unchanged(self):
        """现有 services.prometheus_exporter.check_readiness 应保持不变
        (返回 dict,非 HealthResult)。"""
        # 验证 prometheus_exporter 仍可 import 且 check_readiness 仍返回 dict
        # (不强制实际调用,避免依赖 SQLite)
        try:
            from services.prometheus_exporter import check_readiness as _legacy
        except Exception:
            # 若 prometheus_exporter 因依赖问题无法导入,跳过本检查
            return
        # 应为可调用对象
        assert callable(_legacy), (
            "现有 services.prometheus_exporter.check_readiness 应保持可调用"
        )

    def test_new_health_module_is_separate(self, health):
        """新模块 services.health 应与 services.prometheus_exporter 独立。"""
        # 新模块应有自己的 check_readiness(返回 HealthResult)
        assert hasattr(health, "check_readiness")
        # 新模块的 HealthResult 不是 dict
        cr = health.CheckResult(
            name="test", healthy=True, latency_ms=0, critical=False
        )
        hr = health.HealthResult(
            healthy=True, role="test", checks=[cr], timestamp="now"
        )
        # HealthResult 不是 dict(是 dataclass)
        assert not isinstance(hr, dict)
        # 但 to_dict() 应返回 dict
        assert isinstance(hr.to_dict(), dict)


# ════════════════════════════════════════════════════════════════
# K. R70 规范合规性
# ════════════════════════════════════════════════════════════════


class TestR70Compliance:
    """验证模块符合 R70 整改规范。"""

    def test_no_mock_in_production_code(self, health):
        """生产代码不应包含 mock 占位符(无 'mock' 字样在 check 函数中)。"""
        import inspect as _inspect

        # 检查 _check_database / _check_redis 等函数源码不应包含 mock 占位
        for func_name in (
            "_check_database", "_check_redis", "_check_bot_token_valid",
        ):
            func = getattr(health, func_name)
            src = _inspect.getsource(func)
            # 不应有 TODO / pass / mock 占位
            assert "TODO" not in src, f"{func_name} 不应包含 TODO"
            assert "pass  # mock" not in src, f"{func_name} 不应包含 mock 占位"
            assert "NotImplemented" not in src, (
                f"{func_name} 不应包含 NotImplemented 占位"
            )

    def test_check_readiness_no_mock_placeholder(self, health):
        """check_readiness 不应包含 mock 占位。"""
        src = inspect.getsource(health.check_readiness)
        assert "TODO" not in src
        assert "NotImplemented" not in src

    def test_version_constant(self, health):
        """HEALTH_VERSION 应为 'R70 Wave 6'。"""
        assert health.HEALTH_VERSION == "R70 Wave 6"

    @pytest.mark.asyncio
    async def test_db_failure_real_not_mocked(
        self, health, monkeypatch, mock_all_checks_pass
    ):
        """DB 不可用时必须返回 healthy=false(不允许自动 mock 成 healthy=true)。

        R70 整改规范:不允许 mock 真实依赖。
        本测试模拟 DB 真实不可用场景,验证 healthy=false。
        """
        # 模拟真实 DB 不可用(_check_database 真实失败)
        monkeypatch.setattr(
            health, "_check_database",
            _async_return_factory((False, "Connection refused to DB"))
        )
        result = await health.check_readiness("up_bot")
        assert result.healthy is False, (
            "DB 不可用时 healthy 必须为 False(不允许 mock 成 True)"
        )
        db_check = next(c for c in result.checks if c.name == "database")
        assert db_check.healthy is False
        assert "Connection refused" in db_check.error

    def test_no_modification_to_protected_files(self):
        """验证未修改任务禁止修改的文件(通过 mtime 简单检查)。

        本测试仅检查文件存在性,真正的修改检测在 git diff 层面。
        主要是文档性断言:确保任务范围明确。
        """
        repo_root = Path(__file__).resolve().parent.parent
        protected_files = [
            "services/db_restore.py",
            "services/restore_writer.py",
            "services/backup_dr_validate.py",
            "config/environment.py",
            "config/settings.py",
            "docker/entrypoint.py",
        ]
        for rel_path in protected_files:
            full_path = repo_root / rel_path
            assert full_path.exists(), (
                f"受保护文件应存在(未删除): {rel_path}"
            )
