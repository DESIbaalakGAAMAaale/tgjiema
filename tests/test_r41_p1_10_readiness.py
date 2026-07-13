"""R41 P1-10: Prometheus readiness 反映真实依赖状态测试。

测试覆盖:
- check_readiness() 返回 dict 包含必需字段(ready/passed/checks/details/ru_daily_usage 等)
- checks 包含 7 项(sqlite_readable/recent_scrape/key_schema_exists/schema_valid/
  crdb_sync_fresh/r2_collector_fresh/acl_configured)
- ACL 未配置时 acl_configured=False
- ACL 配置完整时 acl_configured=True
- RU 采集失败显示 "unknown"(非 "0")
- schema_valid 检查 backup_schema.validate_schema()
- collect_metrics() 输出包含 tgjiema_readiness_status 指标
- /readiness 路由存在于 prometheus_exporter(MetricsHTTPRequestHandler)
- admin/__init__.py 中 /readiness 路由存在
- admin /health 端点返回 dependencies 字段
- _MFA_EXEMPT_PATHS 包含 /readiness
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── 测试文件路径 ────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ADMIN_INIT_PATH = _PROJECT_ROOT / "admin" / "__init__.py"
_PROM_EXPORTER_PATH = _PROJECT_ROOT / "services" / "prometheus_exporter.py"


# ════════════════════════════════════════════════════════════════
# 1. check_readiness() 返回结构
# ════════════════════════════════════════════════════════════════

class TestCheckReadinessReturnStructure:
    """R41 P1-10: check_readiness() 返回的 dict 结构正确。"""

    def test_returns_dict(self):
        """check_readiness() 应返回 dict。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert isinstance(result, dict), f"应返回 dict,实际: {type(result)}"

    def test_has_ready_field(self):
        """返回值应包含 ready 字段(bool)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "ready" in result, f"缺少 ready 字段: {list(result.keys())}"
        assert isinstance(result["ready"], bool)

    def test_has_passed_field(self):
        """返回值应包含 passed 字段(int,通过的检查项数)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "passed" in result
        assert isinstance(result["passed"], int)
        assert result["passed"] >= 0

    def test_has_checks_field(self):
        """返回值应包含 checks 字段(dict)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "checks" in result
        assert isinstance(result["checks"], dict)

    def test_has_details_field(self):
        """R41 P1-10: 返回值应包含 details 字段(dict)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "details" in result, "R41 P1-10: 缺少 details 字段"
        assert isinstance(result["details"], dict)

    def test_has_ru_daily_usage_field(self):
        """R41 P1-10: 返回值应包含 ru_daily_usage 字段(str)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "ru_daily_usage" in result, "R41 P1-10: 缺少 ru_daily_usage 字段"
        assert isinstance(result["ru_daily_usage"], str), \
            f"ru_daily_usage 应为 str,实际: {type(result['ru_daily_usage'])}"

    def test_has_last_crdb_sync_age_field(self):
        """R41 P1-10: 返回值应包含 last_crdb_sync_age 字段(float)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "last_crdb_sync_age" in result, "R41 P1-10: 缺少 last_crdb_sync_age 字段"
        assert isinstance(result["last_crdb_sync_age"], (int, float))

    def test_has_last_r2_collect_age_field(self):
        """R41 P1-10: 返回值应包含 last_r2_collect_age 字段(float)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "last_r2_collect_age" in result, "R41 P1-10: 缺少 last_r2_collect_age 字段"
        assert isinstance(result["last_r2_collect_age"], (int, float))


# ════════════════════════════════════════════════════════════════
# 2. checks 字段包含 R41 P1-10 新增的 4 项依赖检查
# ════════════════════════════════════════════════════════════════

class TestReadinessChecks:
    """R41 P1-10: checks 字段应包含 4 项新增依赖检查。"""

    def test_checks_includes_schema_valid(self):
        """R41 P1-10: checks 应包含 schema_valid 项。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "schema_valid" in result["checks"], \
            f"checks 缺少 schema_valid: {list(result['checks'].keys())}"

    def test_checks_includes_crdb_sync_fresh(self):
        """R41 P1-10: checks 应包含 crdb_sync_fresh 项。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "crdb_sync_fresh" in result["checks"], \
            f"checks 缺少 crdb_sync_fresh: {list(result['checks'].keys())}"

    def test_checks_includes_r2_collector_fresh(self):
        """R41 P1-10: checks 应包含 r2_collector_fresh 项。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "r2_collector_fresh" in result["checks"], \
            f"checks 缺少 r2_collector_fresh: {list(result['checks'].keys())}"

    def test_checks_includes_acl_configured(self):
        """R41 P1-10: checks 应包含 acl_configured 项。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "acl_configured" in result["checks"], \
            f"checks 缺少 acl_configured: {list(result['checks'].keys())}"

    def test_checks_includes_sqlite_readable(self):
        """checks 应包含原有 sqlite_readable 项(R39 P1-8)。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert "sqlite_readable" in result["checks"]

    def test_checks_all_values_are_bool(self):
        """checks 中所有值都应为 bool。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        for name, val in result["checks"].items():
            assert isinstance(val, bool), \
                f"checks[{name}] 应为 bool,实际: {type(val)}={val}"

    def test_details_has_entry_for_each_check(self):
        """R41 P1-10: details 应为每个 check 提供详细信息。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        for name in result["checks"]:
            assert name in result["details"], \
                f"details 缺少 {name} 项(checks 中存在)"


# ════════════════════════════════════════════════════════════════
# 3. ACL 配置完整性检查
# ════════════════════════════════════════════════════════════════

class TestAclConfiguredCheck:
    """R41 P1-10: acl_configured 检查应反映 REDIS_*_PASSWORD 4 个变量的配置状态。"""

    def test_acl_configured_false_when_no_redis_envs(self, monkeypatch):
        """无任何 REDIS_*_PASSWORD 时,acl_configured 应为 False。"""
        # 清空所有 REDIS_*_PASSWORD 环境变量
        for var in ("REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                    "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert result["checks"]["acl_configured"] is False, \
            f"无 REDIS_*_PASSWORD 时 acl_configured 应为 False,实际: {result['checks']['acl_configured']}"
        assert "FAIL" in result["details"]["acl_configured"], \
            f"details[acl_configured] 应包含 FAIL: {result['details']['acl_configured']}"

    def test_acl_configured_false_when_partial_redis_envs(self, monkeypatch):
        """仅配置 3 个 REDIS_*_PASSWORD(缺 REDIS_ADMIN_PASSWORD)时,acl_configured 应为 False。"""
        monkeypatch.setenv("REDIS_HEALTH_PASSWORD", "health_pwd")
        monkeypatch.setenv("REDIS_WRITER_PASSWORD", "writer_pwd")
        monkeypatch.setenv("REDIS_READER_PASSWORD", "reader_pwd")
        monkeypatch.delenv("REDIS_ADMIN_PASSWORD", raising=False)
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert result["checks"]["acl_configured"] is False, \
            "缺 REDIS_ADMIN_PASSWORD 时 acl_configured 应为 False(R41 P1-9 新增变量)"

    def test_acl_configured_true_when_all_redis_envs_set(self, monkeypatch):
        """4 个 REDIS_*_PASSWORD 全部配置时,acl_configured 应为 True。"""
        monkeypatch.setenv("REDIS_HEALTH_PASSWORD", "health_pwd")
        monkeypatch.setenv("REDIS_WRITER_PASSWORD", "writer_pwd")
        monkeypatch.setenv("REDIS_READER_PASSWORD", "reader_pwd")
        monkeypatch.setenv("REDIS_ADMIN_PASSWORD", "admin_pwd")
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert result["checks"]["acl_configured"] is True, \
            f"4 个 REDIS_*_PASSWORD 全部配置时 acl_configured 应为 True,实际: {result['checks']['acl_configured']}"
        assert "OK" in result["details"]["acl_configured"]

    def test_acl_details_mentions_missing_vars(self, monkeypatch):
        """acl_configured 失败时,details 应提及缺失的变量名。"""
        for var in ("REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                    "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        detail = result["details"]["acl_configured"]
        assert "REDIS_HEALTH_PASSWORD" in detail or "缺失" in detail, \
            f"details 应提及缺失的变量: {detail}"


# ════════════════════════════════════════════════════════════════
# 4. RU 采集失败显示 "unknown"(非 "0")
# ════════════════════════════════════════════════════════════════

class TestRuUnknownOnCollectionFailure:
    """R41 P1-10: RU 采集失败时 ru_daily_usage 应为 "unknown"(不显示 0)。"""

    def test_ru_daily_usage_is_unknown_when_sqlite_missing(self, monkeypatch, tmp_path):
        """SQLite 文件不存在时,ru_daily_usage 应为 "unknown"。"""
        # 指向不存在的 SQLite 路径
        from services.prometheus_exporter import CACHE_STORE_DB
        monkeypatch.setattr(
            "services.prometheus_exporter.CACHE_STORE_DB",
            tmp_path / "nonexistent.db",
        )
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert result["ru_daily_usage"] == "unknown", \
            f"SQLite 不存在时 ru_daily_usage 应为 'unknown',实际: {result['ru_daily_usage']}"

    def test_ru_daily_usage_is_string_not_zero(self, monkeypatch):
        """ru_daily_usage 应为字符串类型(可能是 "unknown" 或数字字符串)。"""
        for var in ("REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                    "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert isinstance(result["ru_daily_usage"], str)
        # 当 SQLite 不可读时,不应为 "0"(应显示 unknown)
        # 测试环境中通常 SQLite 不存在,因此应为 "unknown"
        if not Path(__file__).parent.parent.joinpath("data", "cache_store.db").exists():
            assert result["ru_daily_usage"] == "unknown", \
                f"SQLite 不可读时 ru_daily_usage 应为 'unknown',实际: {result['ru_daily_usage']}"

    def test_ru_daily_usage_never_returns_zero_string_on_failure(self, monkeypatch, tmp_path):
        """SQLite 不可读时,ru_daily_usage 不应为 "0"(应显示 unknown)。"""
        monkeypatch.setattr(
            "services.prometheus_exporter.CACHE_STORE_DB",
            tmp_path / "nonexistent.db",
        )
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert result["ru_daily_usage"] != "0", \
            f"采集失败不应返回 '0'(应显示 'unknown'),实际: {result['ru_daily_usage']}"
        assert result["ru_daily_usage"] != 0, \
            f"采集失败不应返回 0(int),实际: {result['ru_daily_usage']}"


# ════════════════════════════════════════════════════════════════
# 5. schema_valid 检查 backup_schema.validate_schema()
# ════════════════════════════════════════════════════════════════

class TestSchemaValidCheck:
    """R41 P1-10: schema_valid 应调用 backup_schema.validate_schema()。"""

    def test_schema_valid_is_bool(self):
        """schema_valid 应为 bool。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        assert isinstance(result["checks"]["schema_valid"], bool)

    def test_schema_valid_uses_validate_schema(self):
        """schema_valid 应基于 backup_schema.validate_schema() 的 empty_columns 判断。

        validate_schema() 在当前代码库中应返回 is_valid=True(empty_columns 为空)。
        若 is_valid=False 但 empty_columns 也为空,仍认为 schema_valid 通过
        (允许 missing/extra tables,但要求所有表的 columns 已补全)。
        """
        from services.backup_schema import validate_schema
        schema_result = validate_schema()
        # 当前 BACKUP_SCHEMA 中所有表都应有 columns 定义
        assert schema_result["empty_columns"] == [], \
            f"当前 BACKUP_SCHEMA 应无 empty_columns: {schema_result['empty_columns']}"

        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        # 当 empty_columns 为空时,schema_valid 应为 True
        assert result["checks"]["schema_valid"] is True, \
            f"empty_columns 为空时 schema_valid 应为 True,实际: {result['checks']['schema_valid']}"


# ════════════════════════════════════════════════════════════════
# 6. tgjiema_readiness_status 指标
# ════════════════════════════════════════════════════════════════

class TestReadinessStatusMetric:
    """R41 P1-10: collect_metrics() 应输出 tgjiema_readiness_status 指标。"""

    def test_metric_in_collect_metrics_output(self):
        """collect_metrics() 输出应包含 tgjiema_readiness_status 指标行。"""
        from services.prometheus_exporter import collect_metrics
        output = collect_metrics()
        assert "tgjiema_readiness_status" in output, \
            "collect_metrics() 缺少 tgjiema_readiness_status 指标"

    def test_metric_has_help_line(self):
        """tgjiema_readiness_status 应有 HELP 注释行。"""
        from services.prometheus_exporter import collect_metrics
        output = collect_metrics()
        help_pattern = r"# HELP tgjiema_readiness_status .+"
        assert re.search(help_pattern, output), \
            "tgjiema_readiness_status 缺少 HELP 注释行"

    def test_metric_has_type_line(self):
        """tgjiema_readiness_status 应有 TYPE 注释行(gauge)。"""
        from services.prometheus_exporter import collect_metrics
        output = collect_metrics()
        type_pattern = r"# TYPE tgjiema_readiness_status gauge"
        assert re.search(type_pattern, output), \
            "tgjiema_readiness_status 缺少 TYPE gauge 注释行"

    def test_metric_value_is_0_or_1(self):
        """tgjiema_readiness_status 值应为 0 或 1(整数)。"""
        from services.prometheus_exporter import collect_metrics
        output = collect_metrics()
        # 查找指标行(非注释)
        for line in output.splitlines():
            if line.startswith("tgjiema_readiness_status ") and not line.startswith("#"):
                value = line.split()[-1]
                assert value in ("0", "1"), \
                    f"tgjiema_readiness_status 应为 0 或 1,实际: {value}"
                return
        pytest.fail("未找到 tgjiema_readiness_status 指标值行")

    def test_metric_value_matches_ready_field(self):
        """tgjiema_readiness_status 值应与 check_readiness()['ready'] 一致。"""
        from services.prometheus_exporter import collect_metrics, check_readiness
        readiness = check_readiness()
        output = collect_metrics()
        expected = "1" if readiness["ready"] else "0"
        for line in output.splitlines():
            if line.startswith("tgjiema_readiness_status ") and not line.startswith("#"):
                actual = line.split()[-1]
                assert actual == expected, \
                    f"tgjiema_readiness_status={actual} 应与 ready={readiness['ready']} 一致"
                return
        pytest.fail("未找到 tgjiema_readiness_status 指标值行")


# ════════════════════════════════════════════════════════════════
# 7. prometheus_exporter: /readiness 路由存在
# ════════════════════════════════════════════════════════════════

class TestPrometheusReadinessRoute:
    """R41 P1-10: prometheus_exporter 应提供 /readiness 路由。"""

    def test_handler_class_has_readiness_route(self):
        """MetricsHTTPRequestHandler 应处理 /readiness 路径。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        # 查找 /readiness 路由处理
        assert 'self.path == "/readiness"' in content, \
            "MetricsHTTPRequestHandler 应处理 /readiness 路径"

    def test_readiness_returns_json(self):
        """/readiness 应返回 JSON(content-type: application/json)。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        # 查找 /readiness 块的 Content-Type
        readiness_block_start = content.find('self.path == "/readiness"')
        readiness_block = content[readiness_block_start:readiness_block_start + 1500]
        assert "application/json" in readiness_block, \
            "/readiness 应返回 application/json content-type"

    def test_readiness_returns_503_when_not_ready(self):
        """不就绪时 /readiness 应返回 503。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        readiness_block_start = content.find('self.path == "/readiness"')
        readiness_block = content[readiness_block_start:readiness_block_start + 1500]
        assert "503" in readiness_block, \
            "/readiness 不就绪时应返回 503"

    def test_readiness_returns_200_when_ready(self):
        """就绪时 /readiness 应返回 200。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        readiness_block_start = content.find('self.path == "/readiness"')
        readiness_block = content[readiness_block_start:readiness_block_start + 1500]
        assert "200" in readiness_block

    def test_intro_page_mentions_readiness(self):
        """介绍页应提及 /readiness 端点。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        # 查找介绍页端点列表
        assert "/readiness" in content

    def test_handler_docstring_mentions_readiness(self):
        """MetricsHTTPRequestHandler 类文档应提及 /readiness 路由。"""
        if not _PROM_EXPORTER_PATH.exists():
            pytest.skip("prometheus_exporter.py 不存在")
        content = _PROM_EXPORTER_PATH.read_text(encoding="utf-8")
        # 查找 class MetricsHTTPRequestHandler 后的完整 docstring(从第一个 """ 到第二个 """)
        class_match = re.search(
            r'class\s+MetricsHTTPRequestHandler[^:]*:\s*""".*?"""',
            content, re.DOTALL
        )
        if class_match:
            docstring = class_match.group(0)
            assert "/readiness" in docstring, \
                "MetricsHTTPRequestHandler 类文档应提及 /readiness 路由"


# ════════════════════════════════════════════════════════════════
# 8. admin/__init__.py: /readiness 路由 + /health 增强
# ════════════════════════════════════════════════════════════════

class TestAdminReadinessRoute:
    """R41 P1-10: admin 后台应提供 /readiness 路由并增强 /health。"""

    def test_admin_has_readiness_route(self):
        """admin/__init__.py 应定义 /readiness 路由。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        # 查找 @app.get("/readiness") 装饰器
        assert re.search(r'@app\.get\(\s*["\']readiness["\']\s*\)', content) or \
               re.search(r'@app\.get\(\s*["\']/readiness["\']\s*\)', content), \
            "admin/__init__.py 应定义 /readiness 路由"

    def test_readiness_in_mfa_exempt_paths(self):
        """R41 P1-10: /readiness 应在 _MFA_EXEMPT_PATHS 集合中。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        # 查找 _MFA_EXEMPT_PATHS 定义
        exempt_match = re.search(
            r'_MFA_EXEMPT_PATHS\s*=\s*frozenset\(\s*\{([^}]+)\}',
            content, re.DOTALL
        )
        if exempt_match:
            paths = exempt_match.group(1)
            assert "/readiness" in paths, \
                f"_MFA_EXEMPT_PATHS 应包含 /readiness(R41 P1-10): {paths}"

    def test_health_returns_dependencies_field(self):
        """R41 P1-10: /health 端点应返回 dependencies 字段。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        # 查找 /health 路由的实现
        health_match = re.search(
            r'@app\.get\(\s*["\']/health["\']\s*\).*?(?=@app\.|\Z)',
            content, re.DOTALL
        )
        if health_match:
            health_impl = health_match.group(0)
            assert "dependencies" in health_impl, \
                "/health 端点应返回 dependencies 字段(R41 P1-10)"

    def test_health_uses_check_readiness(self):
        """R41 P1-10: /health 应调用 prometheus_exporter.check_readiness()。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        health_match = re.search(
            r'@app\.get\(\s*["\']/health["\']\s*\).*?(?=@app\.|\Z)',
            content, re.DOTALL
        )
        if health_match:
            health_impl = health_match.group(0)
            assert "check_readiness" in health_impl, \
                "/health 应调用 prometheus_exporter.check_readiness()"

    def test_readiness_uses_check_readiness(self):
        """R41 P1-10: /readiness 应调用 prometheus_exporter.check_readiness()。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        readiness_match = re.search(
            r'@app\.get\(\s*["\']/readiness["\']\s*\).*?(?=@app\.|\Z)',
            content, re.DOTALL
        )
        if readiness_match:
            readiness_impl = readiness_match.group(0)
            assert "check_readiness" in readiness_impl, \
                "/readiness 应调用 prometheus_exporter.check_readiness()"

    def test_readiness_returns_ru_daily_usage(self):
        """R41 P1-10: /readiness 应返回 ru_daily_usage 字段。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        readiness_match = re.search(
            r'@app\.get\(\s*["\']/readiness["\']\s*\).*?(?=@app\.|\Z)',
            content, re.DOTALL
        )
        if readiness_match:
            readiness_impl = readiness_match.group(0)
            assert "ru_daily_usage" in readiness_impl, \
                "/readiness 应返回 ru_daily_usage 字段"

    def test_readiness_returns_bots_status(self):
        """R41 P1-10: /readiness 应返回 bots 心跳状态。"""
        if not _ADMIN_INIT_PATH.exists():
            pytest.skip("admin/__init__.py 不存在")
        content = _ADMIN_INIT_PATH.read_text(encoding="utf-8")
        readiness_match = re.search(
            r'@app\.get\(\s*["\']/readiness["\']\s*\).*?(?=@app\.|\Z)',
            content, re.DOTALL
        )
        if readiness_match:
            readiness_impl = readiness_match.group(0)
            assert "bots" in readiness_impl, \
                "/readiness 应返回 bots 字段(Bot 心跳状态)"


# ════════════════════════════════════════════════════════════════
# 9. 模块级状态变量
# ════════════════════════════════════════════════════════════════

class TestModuleLevelState:
    """R41 P1-10: prometheus_exporter 模块应定义依赖状态跟踪变量。"""

    def test_has_last_crdb_sync_ts(self):
        """模块应定义 _last_crdb_sync_ts 变量。"""
        from services.prometheus_exporter import _last_crdb_sync_ts
        assert isinstance(_last_crdb_sync_ts, (int, float))

    def test_has_last_r2_collect_ts(self):
        """模块应定义 _last_r2_collect_ts 变量。"""
        from services.prometheus_exporter import _last_r2_collect_ts
        assert isinstance(_last_r2_collect_ts, (int, float))

    def test_has_acl_configured(self):
        """模块应定义 _acl_configured 变量。"""
        from services.prometheus_exporter import _acl_configured
        assert isinstance(_acl_configured, bool)

    def test_has_schema_valid(self):
        """模块应定义 _schema_valid 变量。"""
        from services.prometheus_exporter import _schema_valid
        assert isinstance(_schema_valid, bool)

    def test_has_fresh_threshold_constants(self):
        """模块应定义新鲜度阈值常量。"""
        from services.prometheus_exporter import (
            _CRDB_SYNC_FRESH_THRESHOLD,
            _R2_COLLECT_FRESH_THRESHOLD,
        )
        assert _CRDB_SYNC_FRESH_THRESHOLD > 0
        assert _R2_COLLECT_FRESH_THRESHOLD > 0


# ════════════════════════════════════════════════════════════════
# 10. 端到端:check_readiness + collect_metrics 一致性
# ════════════════════════════════════════════════════════════════

class TestEndToEndConsistency:
    """R41 P1-10: check_readiness() 与 collect_metrics() 中的 readiness_status 一致。"""

    def test_readiness_status_matches_check_readiness_ready(self):
        """collect_metrics 中的 tgjiema_readiness_status 应等于 check_readiness()['ready']。"""
        from services.prometheus_exporter import check_readiness, collect_metrics
        readiness = check_readiness()
        output = collect_metrics()
        # 提取 tgjiema_readiness_status 值
        status_line = None
        for line in output.splitlines():
            if line.startswith("tgjiema_readiness_status ") and not line.startswith("#"):
                status_line = line
                break
        if status_line is None:
            pytest.fail("collect_metrics 未输出 tgjiema_readiness_status")
        actual_value = int(status_line.split()[-1])
        expected_value = 1 if readiness["ready"] else 0
        assert actual_value == expected_value, \
            f"tgjiema_readiness_status={actual_value} 应与 ready={readiness['ready']} 一致"

    def test_readiness_checks_count_matches_passed(self):
        """check_readiness()['passed'] 应等于 checks 中 True 的数量。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        true_count = sum(1 for v in result["checks"].values() if v)
        assert result["passed"] == true_count, \
            f"passed={result['passed']} 应等于 checks 中 True 的数量={true_count}"

    def test_ready_is_all_checks_true(self):
        """check_readiness()['ready'] 应等于所有 checks 都为 True。"""
        from services.prometheus_exporter import check_readiness
        result = check_readiness()
        all_true = all(result["checks"].values())
        assert result["ready"] == all_true, \
            f"ready={result['ready']} 应等于所有 checks 都为 True={all_true}"
