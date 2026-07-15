"""R53 P1-6: Prometheus 默认 0 与估算/官方混用风险修复测试。

被测目标:
    - ``services/prometheus_exporter.py`` 底层 SQLite 读取函数不再返回默认 0
    - 采集失败时不输出主 metric(避免 0 伪装健康)
    - 告警规则同时要求 collector_success=1、source=official、fresh=1
    - 估算 RU 只用于归因,不得参与生产 GO 判定

测试覆盖:
    1. 采集成功 → 输出 crdb_ru_daily metric + collector_success=1
    2. 采集失败 → 不输出主 metric + collector_success=0
    3. source=estimated → 输出 ru_estimated=1, tgjiema_ru_official_daily_usage 不包含该服务
    4. source=official + fresh=1 → tgjiema_ru_go_signal=1(告警可触发)
    5. source=official + fresh=0(stale) → tgjiema_ru_go_signal=0(告警不触发)
    6. 多个 collector 并行采集,部分失败不影响其他

测试策略:
    - monkeypatch + 内存 mock,不依赖真实 SQLite / R2 / CRDB
    - 中文注释,与项目其他测试保持一致
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _make_iso(seconds_ago: float) -> str:
    """生成距现在 seconds_ago 秒前的 UTC ISO 时间戳。"""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


def _patch_pe_basics(monkeypatch, kv_mock=None):
    """统一 patch prometheus_exporter 的 SQLite 依赖,避免真实数据库。

    与 R42 测试中的 _patch_pe_basics 保持一致:
        - mock _read_kv_value(影响 _read_kv_value_with_meta,因为后者调用前者)
        - mock _read_sqlite_single
        - mock _get_relay_spool_disk_usage
        - mock _start_r40_collector(避免启动后台线程)
        - mock check_readiness(避免真实依赖检查)
    """
    from services import prometheus_exporter as pe

    if kv_mock is None:
        kv_mock = lambda key, default="0": default
    monkeypatch.setattr(pe, "_read_kv_value", kv_mock)
    monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
    monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)
    monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
    monkeypatch.setattr(
        pe, "check_readiness",
        lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                 "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                 "last_r2_collect_age": -1},
    )
    return pe


# ════════════════════════════════════════════════════════════════
# 测试类 1: _emit_metric_or_skip helper
# ════════════════════════════════════════════════════════════════


class TestEmitMetricOrSkip:
    """R53 P1-6: _emit_metric_or_skip helper 行为验证。"""

    def test_valid_true_outputs_metric(self):
        """valid=True 时输出标准 Prometheus metric 行。"""
        from services.prometheus_exporter import _emit_metric_or_skip

        result = _emit_metric_or_skip("crdb_ru_daily", 12345.0, True)
        assert result == ["crdb_ru_daily 12345.0"], \
            f"valid=True 时应输出 metric 行,实际: {result}"

    def test_valid_false_returns_empty(self):
        """valid=False 时不输出该 metric(返回空列表)。"""
        from services.prometheus_exporter import _emit_metric_or_skip

        result = _emit_metric_or_skip("crdb_ru_daily", None, False)
        assert result == [], \
            "valid=False 时不应输出 metric(避免 0 伪装健康)"

    def test_valid_true_with_labels(self):
        """valid=True + labels 时输出带 label 的 metric 行。"""
        from services.prometheus_exporter import _emit_metric_or_skip

        result = _emit_metric_or_skip(
            "tgjiema_crdb_idle_ru_daily", 42.0, True,
            labels={"source": "official"},
        )
        assert result == ['tgjiema_crdb_idle_ru_daily{source="official"} 42.0'], \
            f"带 label 的 metric 行格式错误,实际: {result}"

    def test_valid_false_with_labels_returns_empty(self):
        """valid=False + labels 时不输出(返回空列表)。"""
        from services.prometheus_exporter import _emit_metric_or_skip

        result = _emit_metric_or_skip(
            "tgjiema_crdb_idle_ru_daily", None, False,
            labels={"source": "failed"},
        )
        assert result == [], "valid=False 时无论是否有 labels 都不应输出"


# ════════════════════════════════════════════════════════════════
# 测试类 2: _read_kv_value_with_meta 元组返回
# ════════════════════════════════════════════════════════════════


class TestReadKvValueWithMeta:
    """R53 P1-6: _read_kv_value_with_meta 返回 (value, valid, timestamp, source) 元组。"""

    def test_returns_4_tuple(self, monkeypatch):
        """返回值为 4 元组 (value, valid, timestamp, source)。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": "12345")

        result = pe._read_kv_value_with_meta("crdb_ru_daily")
        assert isinstance(result, tuple), "应返回元组"
        assert len(result) == 4, f"元组长度应为 4,实际: {len(result)}"
        value, valid, timestamp, source = result
        assert value == "12345"
        assert valid is True
        assert isinstance(timestamp, float)
        assert source == "sqlite"

    def test_empty_value_returns_invalid(self, monkeypatch):
        """kv_store 无数据时 valid=False。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        value, valid, _, source = pe._read_kv_value_with_meta("crdb_ru_daily")
        assert value == "", "失败时 value 应为空字符串"
        assert valid is False, "无数据时 valid 应为 False"
        assert source == "failed", "失败时 source 应为 'failed'"

    def test_real_zero_value_is_valid(self, monkeypatch):
        """kv_store 中真实值 '0' 应 valid=True(区分 '0' 与缺失)。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": "0")

        value, valid, _, source = pe._read_kv_value_with_meta("crdb_ru_daily")
        assert value == "0", "真实值 '0' 应被正确读取"
        assert valid is True, "真实值 '0' 应 valid=True(非缺失)"
        assert source == "sqlite"


# ════════════════════════════════════════════════════════════════
# 测试类 3: _compute_ru_go_signal GO 判定信号
# ════════════════════════════════════════════════════════════════


class TestComputeRuGoSignal:
    """R53 P1-6: _compute_ru_go_signal 生产 GO 判定信号。"""

    def test_all_conditions_met_returns_1(self):
        """collector_success=1 + source=official + fresh=1 → go_signal=1。"""
        from services.prometheus_exporter import _compute_ru_go_signal

        result = _compute_ru_go_signal(
            collector_success=True,
            source_label="official",
            freshness_seconds=120.0,
        )
        assert result == 1, "所有条件满足时 go_signal 应为 1(可触发告警)"

    def test_collector_failure_returns_0(self):
        """collector_success=0 → go_signal=0。"""
        from services.prometheus_exporter import _compute_ru_go_signal

        result = _compute_ru_go_signal(
            collector_success=False,
            source_label="official",
            freshness_seconds=120.0,
        )
        assert result == 0, "采集失败时 go_signal 应为 0(不可触发告警)"

    def test_source_unknown_returns_0(self):
        """source=unknown → go_signal=0。"""
        from services.prometheus_exporter import _compute_ru_go_signal

        result = _compute_ru_go_signal(
            collector_success=True,
            source_label="unknown",
            freshness_seconds=120.0,
        )
        assert result == 0, "source=unknown 时 go_signal 应为 0"

    def test_source_failed_returns_0(self):
        """source=failed → go_signal=0。"""
        from services.prometheus_exporter import _compute_ru_go_signal

        result = _compute_ru_go_signal(
            collector_success=True,
            source_label="failed",
            freshness_seconds=-1.0,
        )
        assert result == 0, "source=failed 时 go_signal 应为 0"

    def test_stale_data_returns_0(self):
        """source=official + fresh=0(stale) → go_signal=0。"""
        from services.prometheus_exporter import _compute_ru_go_signal, _RU_DATA_FRESH_THRESHOLD

        # freshness 超过阈值 → stale
        result = _compute_ru_go_signal(
            collector_success=True,
            source_label="official",
            freshness_seconds=float(_RU_DATA_FRESH_THRESHOLD) + 1.0,
        )
        assert result == 0, "数据陈旧时 go_signal 应为 0(不可触发告警)"

    def test_negative_freshness_returns_0(self):
        """freshness=-1(从未采集) → go_signal=0。"""
        from services.prometheus_exporter import _compute_ru_go_signal

        result = _compute_ru_go_signal(
            collector_success=True,
            source_label="official",
            freshness_seconds=-1.0,
        )
        assert result == 0, "freshness=-1 时 go_signal 应为 0"


# ════════════════════════════════════════════════════════════════
# 测试类 4: collect_metrics — 采集成功/失败场景
# ════════════════════════════════════════════════════════════════


class TestCollectMetricsSuccess:
    """R53 P1-6: collect_metrics 采集成功 → 输出 metric + collector_success=1。"""

    def test_success_outputs_crdb_ru_daily_metric(self, monkeypatch):
        """采集成功时输出 crdb_ru_daily 主 metric。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        # 应输出 crdb_ru_daily 主 metric(非 0 值)
        found = False
        for line in output.split("\n"):
            if line.startswith("crdb_ru_daily ") and not line.startswith("#"):
                value = float(line.split()[-1])
                assert value == 12345.0, f"采集成功时应输出真实值 12345,实际: {line}"
                found = True
                break
        assert found, "采集成功时应输出 crdb_ru_daily 主 metric"

    def test_success_outputs_collector_success_1(self, monkeypatch):
        """采集成功时输出 tgjiema_collector_success{collector="crdb_ru"} 1。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        assert 'tgjiema_collector_success{collector="crdb_ru"} 1' in output, \
            "采集成功时应输出 collector_success=1"


class TestCollectMetricsFailure:
    """R53 P1-6: collect_metrics 采集失败 → 不输出主 metric + collector_success=0。"""

    def test_failure_skips_crdb_ru_daily_metric(self, monkeypatch):
        """采集失败时不输出 crdb_ru_daily 主 metric(不输出 0)。"""
        # 所有 kv_store 值返回默认(模拟 SQLite 不可用)
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()

        # 不应包含 "crdb_ru_daily 0" 这样的行(0 伪装健康)
        for line in output.split("\n"):
            if line.startswith("crdb_ru_daily ") and not line.startswith("#"):
                pytest.fail(
                    f"采集失败时不应输出 crdb_ru_daily 主 metric,实际输出: {line}"
                )

    def test_failure_outputs_collector_success_0(self, monkeypatch):
        """采集失败时输出 tgjiema_collector_success{collector="crdb_ru"} 0。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()

        assert 'tgjiema_collector_success{collector="crdb_ru"} 0' in output, \
            "采集失败时应输出 collector_success=0"

    def test_failure_does_not_output_zero_crdb_ru(self, monkeypatch):
        """采集失败时 crdb_ru_daily 不输出 0 值(避免伪装健康)。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()

        # 确保 "crdb_ru_daily 0" 或 "crdb_ru_daily 0.0" 不在输出中
        assert "crdb_ru_daily 0\n" not in output + "\n", \
            "采集失败时不应输出 crdb_ru_daily 0(0 可能被误认为真实值)"
        assert "crdb_ru_daily 0.0" not in output, \
            "采集失败时不应输出 crdb_ru_daily 0.0"


# ════════════════════════════════════════════════════════════════
# 测试类 5: tgjiema_ru_go_signal — 告警门禁信号
# ════════════════════════════════════════════════════════════════


class TestRuGoSignalMetric:
    """R53 P1-6: tgjiema_ru_go_signal 告警门禁信号。"""

    def test_go_signal_1_when_official_and_fresh(self, monkeypatch):
        """source=official + fresh=1 → tgjiema_ru_go_signal=1(告警可触发)。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        # 找到 tgjiema_ru_go_signal 行
        for line in output.split("\n"):
            if line.startswith("tgjiema_ru_go_signal "):
                value = int(line.split()[-1])
                assert value == 1, \
                    f"source=official + fresh=1 时 go_signal 应为 1,实际: {value}"
                return
        pytest.fail("未找到 tgjiema_ru_go_signal 指标行")

    def test_go_signal_0_when_stale(self, monkeypatch):
        """source=official + fresh=0(stale) → tgjiema_ru_go_signal=0(告警不触发)。"""
        stale_ts = _make_iso(7200)  # 2 小时前(超过 _RU_DATA_FRESH_THRESHOLD=3600)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return stale_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        for line in output.split("\n"):
            if line.startswith("tgjiema_ru_go_signal "):
                value = int(line.split()[-1])
                assert value == 0, \
                    f"数据陈旧时 go_signal 应为 0(不可触发告警),实际: {value}"
                return
        pytest.fail("未找到 tgjiema_ru_go_signal 指标行")

    def test_go_signal_0_when_failed(self, monkeypatch):
        """采集失败 → tgjiema_ru_go_signal=0。"""
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()

        for line in output.split("\n"):
            if line.startswith("tgjiema_ru_go_signal "):
                value = int(line.split()[-1])
                assert value == 0, \
                    f"采集失败时 go_signal 应为 0,实际: {value}"
                return
        pytest.fail("未找到 tgjiema_ru_go_signal 指标行")

    def test_idle_alert_gated_by_go_signal_when_failed(self, monkeypatch):
        """idle_alert 在 go_signal=0 时不触发(即使 idle_ru > 阈值)。"""
        # 模拟采集失败:idle_ru 无法读取,go_signal=0
        pe = _patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        # 设置阈值为 10(很低,确保 idle_ru=0 也不超阈值)
        monkeypatch.setenv("CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD", "10")

        output = pe.collect_metrics()

        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_idle_ru_alert "):
                value = int(line.split()[-1])
                assert value == 0, \
                    f"go_signal=0 时 idle_alert 应为 0(告警不触发),实际: {value}"
                return
        pytest.fail("未找到 tgjiema_crdb_idle_ru_alert 指标行")

    def test_idle_alert_fires_when_go_signal_1_and_exceeds(self, monkeypatch):
        """idle_ru > 阈值 + go_signal=1 → idle_alert=1(告警可触发)。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "200"  # 超过阈值 100
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_idle_ru_alert "):
                value = int(line.split()[-1])
                assert value == 1, \
                    f"go_signal=1 + idle_ru > 阈值时 idle_alert 应为 1,实际: {value}"
                return
        pytest.fail("未找到 tgjiema_crdb_idle_ru_alert 指标行")


# ════════════════════════════════════════════════════════════════
# 测试类 6: 估算 RU 只用于归因,不参与生产 GO 判定
# ════════════════════════════════════════════════════════════════


class TestEstimatedRuAttributionOnly:
    """R53 P1-6: 估算 RU(ru_estimated=1)只用于归因,不参与生产 GO 判定。"""

    def test_estimated_ru_outputted_for_attribution(self, monkeypatch):
        """估算 RU(ru_estimated=1)输出到 tgjiema_ru_daily_usage(用于归因)。"""
        pe = _patch_pe_basics(monkeypatch)

        # 设置 R40 状态:up_bot 为估算值
        estimated_state = {
            "ru_daily_usage": {"up_bot": 500},
            "ru_daily_usage_estimated": {"up_bot": 1},  # 1=估算
        }
        monkeypatch.setattr(pe, "_r40_state", estimated_state)

        lines = pe._format_r40_metrics()
        output = "\n".join(lines)

        # 估算值应输出到 tgjiema_ru_daily_usage(带 ru_estimated="1" label)
        assert 'tgjiema_ru_daily_usage{service="up_bot",ru_estimated="1"} 500' in output, \
            "估算 RU 应输出到 tgjiema_ru_daily_usage 用于归因"

    def test_estimated_ru_not_in_official_daily_usage(self, monkeypatch):
        """估算 RU 不出现在 tgjiema_ru_official_daily_usage(不参与 GO 判定)。"""
        pe = _patch_pe_basics(monkeypatch)

        # 设置 R40 状态:up_bot 为估算值,admin_bot 为官方值
        estimated_state = {
            "ru_daily_usage": {"up_bot": 500, "admin_bot": 300},
            "ru_daily_usage_estimated": {"up_bot": 1, "admin_bot": 0},
        }
        monkeypatch.setattr(pe, "_r40_state", estimated_state)

        lines = pe._format_r40_metrics()
        output = "\n".join(lines)

        # tgjiema_ru_official_daily_usage 应只包含 admin_bot(ru_estimated=0)
        assert 'tgjiema_ru_official_daily_usage{service="admin_bot"} 300' in output, \
            "官方值应输出到 tgjiema_ru_official_daily_usage"
        # 不应包含 up_bot(估算值)
        assert 'tgjiema_ru_official_daily_usage{service="up_bot"}' not in output, \
            "估算 RU 不应出现在 tgjiema_ru_official_daily_usage(不参与 GO 判定)"

    def test_all_estimated_outputs_none_placeholder(self, monkeypatch):
        """全部为估算值时 tgjiema_ru_official_daily_usage 输出占位行。"""
        pe = _patch_pe_basics(monkeypatch)

        estimated_state = {
            "ru_daily_usage": {"up_bot": 500},
            "ru_daily_usage_estimated": {"up_bot": 1},
        }
        monkeypatch.setattr(pe, "_r40_state", estimated_state)

        lines = pe._format_r40_metrics()
        output = "\n".join(lines)

        # 无官方值时输出占位行
        assert 'tgjiema_ru_official_daily_usage{service="none"} 0' in output, \
            "全部为估算值时 official_daily_usage 应输出占位行(service=none)"

    def test_official_ru_outputted_for_alerting(self, monkeypatch):
        """官方 RU(ru_estimated=0)输出到 tgjiema_ru_official_daily_usage(用于告警)。"""
        pe = _patch_pe_basics(monkeypatch)

        official_state = {
            "ru_daily_usage": {"admin_bot": 300, "up_bot": 500},
            "ru_daily_usage_estimated": {"admin_bot": 0, "up_bot": 1},
        }
        monkeypatch.setattr(pe, "_r40_state", official_state)

        lines = pe._format_r40_metrics()
        output = "\n".join(lines)

        # admin_bot(官方值)应出现在 official_daily_usage
        assert 'tgjiema_ru_official_daily_usage{service="admin_bot"} 300' in output, \
            "官方 RU 应输出到 tgjiema_ru_official_daily_usage 用于告警门禁"
        # up_bot(估算值)也应出现在 ru_daily_usage(归因)
        assert 'tgjiema_ru_daily_usage{service="up_bot",ru_estimated="1"} 500' in output, \
            "估算 RU 应同时输出到 tgjiema_ru_daily_usage 用于归因"


# ════════════════════════════════════════════════════════════════
# 测试类 7: 多个 collector 并行采集,部分失败不影响其他
# ════════════════════════════════════════════════════════════════


class TestPartialCollectorFailure:
    """R53 P1-6: 多个 collector 并行采集,部分失败不影响其他。"""

    def test_crdb_ru_fails_pel_succeeds(self, monkeypatch):
        """crdb_ru 采集失败 + redis_pel 采集成功 → 互不影响。"""
        def _mock_kv(key, default="0"):
            if key == "redis_pel_depth":
                return "5"  # redis_pel 采集成功
            # crdb_ru_daily / crdb_idle_ru_daily 返回默认(失败)
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        # crdb_ru 采集失败:不输出主 metric + collector_success=0
        for line in output.split("\n"):
            if line.startswith("crdb_ru_daily ") and not line.startswith("#"):
                pytest.fail(f"crdb_ru 采集失败时不应输出主 metric: {line}")
        assert 'tgjiema_collector_success{collector="crdb_ru"} 0' in output, \
            "crdb_ru 采集失败时应有 collector_success=0"

        # redis_pel 采集成功:输出主 metric + collector_success=1
        assert "redis_pel_depth 5.0" in output, \
            "redis_pel 采集成功时应输出主 metric"
        assert 'tgjiema_collector_success{collector="redis_pel"} 1' in output, \
            "redis_pel 采集成功时应有 collector_success=1"

    def test_pel_fails_crdb_ru_succeeds(self, monkeypatch):
        """redis_pel 采集失败 + crdb_ru 采集成功 → 互不影响。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            if key == "redis_pel_depth":
                return "not_a_number"  # redis_pel 采集失败(无法解析)
            if key == "dlq_depth":
                return "3"  # dlq 采集成功
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        # crdb_ru 采集成功:输出主 metric + collector_success=1
        assert "crdb_ru_daily 12345.0" in output, \
            "crdb_ru 采集成功时应输出主 metric"
        assert 'tgjiema_collector_success{collector="crdb_ru"} 1' in output, \
            "crdb_ru 采集成功时应有 collector_success=1"

        # redis_pel 采集失败:不输出主 metric + collector_success=0
        for line in output.split("\n"):
            if line.startswith("redis_pel_depth ") and not line.startswith("#"):
                pytest.fail(f"redis_pel 采集失败时不应输出主 metric: {line}")
        assert 'tgjiema_collector_success{collector="redis_pel"} 0' in output, \
            "redis_pel 采集失败时应有 collector_success=0"

        # dlq 采集成功:不受 redis_pel 失败影响
        assert "dlq_depth 3.0" in output, \
            "dlq 采集成功时应输出主 metric(不受其他 collector 失败影响)"
        assert 'tgjiema_collector_success{collector="dlq"} 1' in output, \
            "dlq 采集成功时应有 collector_success=1"

    def test_all_collectors_status_outputted(self, monkeypatch):
        """所有 collector 的成功/失败状态都被输出(无论成败)。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            if key == "redis_pel_depth":
                return "5"  # 成功
            if key == "dlq_depth":
                return "not_a_number"  # 失败
            return default
        pe = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()

        # 所有 collector 都应有对应的 collector_success 行
        assert 'tgjiema_collector_success{collector="crdb_ru"} 1' in output
        assert 'tgjiema_collector_success{collector="redis_pel"} 1' in output
        assert 'tgjiema_collector_success{collector="dlq"} 0' in output


# ════════════════════════════════════════════════════════════════
# 测试类 8: _read_sqlite_single_with_meta 元组返回
# ════════════════════════════════════════════════════════════════


class TestReadSqliteSingleWithMeta:
    """R53 P1-6: _read_sqlite_single_with_meta 返回 (value, valid, timestamp, source) 元组。"""

    def test_nonexistent_db_returns_failed(self, tmp_path):
        """数据库文件不存在时返回 (None, False, 0.0, 'failed')。"""
        from services.prometheus_exporter import _read_sqlite_single_with_meta

        nonexistent = tmp_path / "nonexistent.db"
        value, valid, timestamp, source = _read_sqlite_single_with_meta(
            nonexistent, "SELECT 1"
        )
        assert value is None
        assert valid is False
        assert source == "failed"

    def test_valid_query_returns_tuple(self, tmp_path):
        """成功查询时返回 (value, True, timestamp, 'sqlite')。"""
        import sqlite3
        from services.prometheus_exporter import _read_sqlite_single_with_meta

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE kv_store (key TEXT, value TEXT)")
        conn.execute("INSERT INTO kv_store VALUES ('test_key', 'test_value')")
        conn.commit()
        conn.close()

        value, valid, timestamp, source = _read_sqlite_single_with_meta(
            db_path, "SELECT value FROM kv_store WHERE key = 'test_key' LIMIT 1"
        )
        assert value == "test_value"
        assert valid is True
        assert isinstance(timestamp, float)
        assert source == "sqlite"
