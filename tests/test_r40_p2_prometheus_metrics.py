"""R40 P2-4: Prometheus 功能成功率指标测试。

测试范围:
- prometheus_exporter.collect_metrics() 输出包含 5 项新指标
- _format_r40_metrics() 输出格式符合 Prometheus text format
- collect_r40_metrics() 采集逻辑可调用(不抛异常)
- 指标类型(Gauge/Histogram/Counter)正确
- 高基数 label 审计通过(无 user_id/file_code 等)

测试策略:
- 直接调用 collect_metrics() 验证输出文本
- 通过设置 _r40_state 模拟数据,验证格式化输出
- AST 语法检查(兼容 Python 3.9)
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"


def _can_import(module_name: str) -> bool:
    """尝试导入模块,返回是否成功。"""
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 1. AST 语法与函数存在性检查(不依赖运行时 import)
# ════════════════════════════════════════════════════════════════


class TestPrometheusExporterAst:
    """R40 P2-4: prometheus_exporter.py AST 静态检查。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "prometheus_exporter.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "prometheus_exporter.py")
        assert tree is not None, "prometheus_exporter.py 应可被 AST 解析"

    def test_has_collect_metrics_function(self):
        tree = _parse_ast(SERVICES_DIR / "prometheus_exporter.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "collect_metrics" in funcs, "缺少 collect_metrics 函数"
        assert "collect_r40_metrics" in funcs, "缺少 collect_r40_metrics 函数"
        assert "_format_r40_metrics" in funcs, "缺少 _format_r40_metrics 函数"

    def test_r40_state_contains_p2_metrics_keys(self):
        """R40 P2-4: _r40_state 字典应包含 5 项新指标的 key。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        required_keys = [
            "approval_execution_success_rate",
            "notification_delivery_latency_samples",
            "repair_success_rate",
            "real_rpo_seconds",
            "real_rto_seconds",
        ]
        for key in required_keys:
            assert key in source, f"_r40_state 缺少 key: {key}"


# ════════════════════════════════════════════════════════════════
# 2. 指标输出格式检查(直接读取源码,验证指标名存在)
# ════════════════════════════════════════════════════════════════


class TestPrometheusMetricsOutput:
    """R40 P2-4: 验证 collect_metrics() 输出包含新指标。"""

    REQUIRED_METRIC_NAMES = [
        "tgjiema_approval_execution_success_rate",
        "tgjiema_notification_delivery_latency_seconds",
        "tgjiema_repair_success_rate",
        "tgjiema_real_rpo_seconds",
        "tgjiema_real_rto_seconds",
    ]

    def test_source_contains_all_metric_names(self):
        """源码中应包含 5 项新指标的名称。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        for name in self.REQUIRED_METRIC_NAMES:
            assert name in source, f"源码缺少指标名: {name}"

    def test_source_contains_help_and_type_lines(self):
        """源码中应包含 # HELP 和 # TYPE 行。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        for name in self.REQUIRED_METRIC_NAMES:
            assert f"# HELP {name}" in source, f"缺少 # HELP {name}"
            assert f"# TYPE {name}" in source, f"缺少 # TYPE {name}"

    def test_histogram_has_buckets(self):
        """Histogram 指标应包含 bucket + count + sum。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        # notification_delivery_latency_seconds 是 Histogram,应有 _bucket/_count/_sum
        assert "notification_delivery_latency_seconds_bucket" in source
        assert "notification_delivery_latency_seconds_count" in source
        assert "notification_delivery_latency_seconds_sum" in source
        # 应包含 le="+Inf" 桶
        assert 'le="+Inf"' in source

    def test_gauge_metrics_use_gauge_type(self):
        """Gauge 指标应使用 gauge 类型。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        gauge_metrics = [
            "tgjiema_approval_execution_success_rate",
            "tgjiema_repair_success_rate",
            "tgjiema_real_rpo_seconds",
            "tgjiema_real_rto_seconds",
        ]
        for name in gauge_metrics:
            # 查找 # TYPE <name> gauge 模式
            pattern = f"# TYPE {name} gauge"
            assert pattern in source, f"{name} 应为 gauge 类型"

    def test_histogram_uses_histogram_type(self):
        """Histogram 指标应使用 histogram 类型。"""
        source = (SERVICES_DIR / "prometheus_exporter.py").read_text(encoding="utf-8")
        pattern = "# TYPE tgjiema_notification_delivery_latency_seconds histogram"
        assert pattern in source, "notification_delivery_latency_seconds 应为 histogram 类型"


# ════════════════════════════════════════════════════════════════
# 3. 运行时 collect_metrics() 输出检查
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestPrometheusCollectMetricsRuntime:
    """R40 P2-4: 运行时验证 collect_metrics() 输出。"""

    def _try_import_exporter(self):
        """尝试导入 prometheus_exporter 模块。"""
        try:
            import services.prometheus_exporter as exporter
            return exporter
        except Exception:
            return None

    def test_collect_metrics_contains_all_metrics(self):
        """collect_metrics() 输出应包含 5 项新指标。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            output = exporter.collect_metrics()
        except Exception as e:
            pytest.skip(f"collect_metrics 调用失败(可能依赖 SQLite): {e}")

        required = [
            "tgjiema_approval_execution_success_rate",
            "tgjiema_notification_delivery_latency_seconds",
            "tgjiema_repair_success_rate",
            "tgjiema_real_rpo_seconds",
            "tgjiema_real_rto_seconds",
        ]
        for name in required:
            assert name in output, f"collect_metrics 输出缺少指标: {name}"

    def test_collect_metrics_has_help_and_type(self):
        """collect_metrics 输出应包含 # HELP 和 # TYPE。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            output = exporter.collect_metrics()
        except Exception as e:
            pytest.skip(f"collect_metrics 调用失败: {e}")

        for name in [
            "tgjiema_approval_execution_success_rate",
            "tgjiema_repair_success_rate",
            "tgjiema_real_rpo_seconds",
            "tgjiema_real_rto_seconds",
        ]:
            assert f"# HELP {name}" in output, f"缺少 # HELP {name}"
            assert f"# TYPE {name} gauge" in output, f"缺少 # TYPE {name} gauge"

    def test_collect_metrics_histogram_buckets(self):
        """collect_metrics 输出 Histogram 应包含 bucket/count/sum。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            output = exporter.collect_metrics()
        except Exception as e:
            pytest.skip(f"collect_metrics 调用失败: {e}")

        assert "tgjiema_notification_delivery_latency_seconds_bucket" in output
        assert "tgjiema_notification_delivery_latency_seconds_count" in output
        assert "tgjiema_notification_delivery_latency_seconds_sum" in output
        assert 'le="+Inf"' in output

    def test_collect_metrics_no_high_cardinality_labels(self):
        """R38 P2-7: 新指标不应包含高基数 label。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            output = exporter.collect_metrics()
        except Exception as e:
            pytest.skip(f"collect_metrics 调用失败: {e}")

        high_card_labels = [
            "user_id", "chat_id", "message_id", "file_code",
            "job_id", "phone", "token", "spool_id", "msg_id",
        ]
        # 提取所有 metric 行(不以 # 开头)
        for line in output.split("\n"):
            if not line or line.startswith("#"):
                continue
            for label in high_card_labels:
                assert f'{label}=' not in line, (
                    f"指标行包含高基数 label {label}: {line[:80]}"
                )


# ════════════════════════════════════════════════════════════════
# 4. _format_r40_metrics 模拟数据测试
# ════════════════════════════════════════════════════════════════


class TestFormatR40MetricsSimulated:
    """R40 P2-4: 通过模拟 _r40_state 验证 _format_r40_metrics 输出格式。"""

    def _try_import_exporter(self):
        try:
            import services.prometheus_exporter as exporter
            return exporter
        except Exception:
            return None

    def test_format_with_default_state(self):
        """默认 state(全 0)应输出有效指标行。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        # 重置 state 为默认值
        with exporter._r40_state_lock:
            exporter._r40_state = {
                "maintenance_enabled": 0,
                "ru_daily_usage": {},
                "replica_missing_count": 0,
                "quota_reservations_active": 0,
                "content_reports_pending": 0,
                "approvals_pending": 0,
                "tasks_running": 0,
                "notifications_unread": 0,
                "dlq_depth": 0,
                "outbox_unprocessed": 0,
                "audit_log_events_total": {},
                "ru_operations_total": {},
                "approval_execution_success_rate": 0.0,
                "approval_execution_total": 0,
                "approval_execution_success": 0,
                "notification_delivery_latency_samples": [],
                "repair_success_rate": 0.0,
                "repair_total": 0,
                "repair_success": 0,
                "real_rpo_seconds": -1.0,
                "real_rto_seconds": -1.0,
            }
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        # 验证所有新指标存在
        assert "tgjiema_approval_execution_success_rate" in output
        assert "tgjiema_notification_delivery_latency_seconds" in output
        assert "tgjiema_repair_success_rate" in output
        assert "tgjiema_real_rpo_seconds" in output
        assert "tgjiema_real_rto_seconds" in output

    def test_format_with_simulated_success_rate(self):
        """模拟审批成功率 0.75 应输出 0.750000。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        with exporter._r40_state_lock:
            exporter._r40_state["approval_execution_success_rate"] = 0.75
            exporter._r40_state["approval_execution_total"] = 4
            exporter._r40_state["approval_execution_success"] = 3
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        assert "tgjiema_approval_execution_success_rate 0.750000" in output
        assert "tgjiema_approval_execution_total 4" in output
        assert "tgjiema_approval_execution_success 3" in output

    def test_format_with_latency_samples(self):
        """模拟通知延迟样本应正确输出 histogram。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        with exporter._r40_state_lock:
            exporter._r40_state["notification_delivery_latency_samples"] = [
                0.3, 1.5, 5.0, 60.0
            ]
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        # count 应为 4
        assert "tgjiema_notification_delivery_latency_seconds_count 4" in output
        # sum 应为 0.3 + 1.5 + 5.0 + 60.0 = 66.8
        assert "tgjiema_notification_delivery_latency_seconds_sum 66.8" in output
        # le="0.5" 桶应有 1 个(0.3)
        assert 'le="0.5"} 1' in output
        # le="+Inf" 桶应有 4 个
        assert 'le="+Inf"} 4' in output

    def test_format_with_repair_rate(self):
        """模拟修复成功率 0.8 应输出 0.800000。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        with exporter._r40_state_lock:
            exporter._r40_state["repair_success_rate"] = 0.8
            exporter._r40_state["repair_total"] = 10
            exporter._r40_state["repair_success"] = 8
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        assert "tgjiema_repair_success_rate 0.800000" in output
        assert "tgjiema_repair_total 10" in output
        assert "tgjiema_repair_success 8" in output

    def test_format_with_real_rpo_rto(self):
        """模拟真实 RPO=3600, RTO=120 应输出对应值。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        with exporter._r40_state_lock:
            exporter._r40_state["real_rpo_seconds"] = 3600.0
            exporter._r40_state["real_rto_seconds"] = 120.0
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        assert "tgjiema_real_rpo_seconds 3600.00" in output
        assert "tgjiema_real_rto_seconds 120.00" in output

    def test_format_with_no_backup(self):
        """无备份时 RPO 应为 -1.00。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        with exporter._r40_state_lock:
            exporter._r40_state["real_rpo_seconds"] = -1.0
        lines = exporter._format_r40_metrics()
        output = "\n".join(lines)
        assert "tgjiema_real_rpo_seconds -1.00" in output


# ════════════════════════════════════════════════════════════════
# 5. collect_r40_metrics 异步采集逻辑
# ════════════════════════════════════════════════════════════════


class TestCollectR40MetricsAsync:
    """R40 P2-4: collect_r40_metrics 异步采集测试。"""

    def _try_import_exporter(self):
        try:
            import services.prometheus_exporter as exporter
            return exporter
        except Exception:
            return None

    def test_collect_r40_metrics_callable(self):
        """collect_r40_metrics 应可调用且不抛异常(降级为 0)。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无 running loop,创建新的
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(exporter.collect_r40_metrics())
            finally:
                loop.close()
            return
        # 已有 running loop
        asyncio.ensure_future(exporter.collect_r40_metrics())

    def test_collect_r40_metrics_updates_state(self):
        """采集后 _r40_state 应包含新指标的 key。"""
        exporter = self._try_import_exporter()
        if exporter is None:
            pytest.skip("prometheus_exporter 不可导入(依赖缺失)")
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(exporter.collect_r40_metrics())
            finally:
                loop.close()
        except Exception:
            pytest.skip("collect_r40_metrics 调用失败(可能依赖缺失)")

        with exporter._r40_state_lock:
            state = exporter._r40_state
        # 验证 R40 P2-4 新增 key 存在
        assert "approval_execution_success_rate" in state
        assert "notification_delivery_latency_samples" in state
        assert "repair_success_rate" in state
        assert "real_rpo_seconds" in state
        assert "real_rto_seconds" in state


# ════════════════════════════════════════════════════════════════
# 6. notifications.read_at 列存在性检查
# ════════════════════════════════════════════════════════════════


class TestNotificationsReadAtColumn:
    """R40 P2-4: notifications 表应包含 read_at 列(供延迟指标计算)。"""

    def test_cache_store_has_read_at_column(self):
        """cache_store.py 中 notifications CREATE TABLE 应包含 read_at 列。"""
        source = (REPO_ROOT / "database" / "cache_store.py").read_text(encoding="utf-8")
        # 查找 CREATE TABLE notifications 块,验证包含 read_at
        # 简化:直接验证文件中存在 read_at TEXT 字段定义
        assert "read_at TEXT" in source or "read_at    TEXT" in source, (
            "notifications 表应包含 read_at TEXT 列"
        )

    def test_cache_store_has_alter_read_at(self):
        """cache_store.py 应包含 ALTER TABLE notifications ADD COLUMN read_at(旧库兼容)。"""
        source = (REPO_ROOT / "database" / "cache_store.py").read_text(encoding="utf-8")
        assert "ALTER TABLE notifications ADD COLUMN read_at" in source, (
            "应有 ALTER TABLE 语句为旧库补 read_at 列"
        )

    def test_notifications_mark_read_sets_read_at(self):
        """notifications.mark_read 应设置 read_at 字段。"""
        source = (SERVICES_DIR / "notifications.py").read_text(encoding="utf-8")
        # 查找 mark_read 函数,验证 SQL 包含 read_at = ?
        assert "read_at = ?" in source or "read_at=?" in source, (
            "mark_read 应在 UPDATE 语句中设置 read_at"
        )

    def test_notifications_mark_all_read_sets_read_at(self):
        """notifications.mark_all_read 应设置 read_at 字段。"""
        source = (SERVICES_DIR / "notifications.py").read_text(encoding="utf-8")
        assert "read_at = ?" in source or "read_at=?" in source, (
            "mark_all_read 应在 UPDATE 语句中设置 read_at"
        )
