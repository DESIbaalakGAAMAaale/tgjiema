"""R42 P1-10: CRDB RU 指标官方来源验证 + unknown 状态识别。

测试覆盖:
    - crdb_ru_collector.get_ru_status() 区分 "official" / "unknown" / "failed"
        * CRDB API 失败且无 kv_store 历史 → source="failed"
        * CRDB API 成功且数据新鲜 → source="official"
        * 数据陈旧(时间戳 ≥ 阈值)→ source="unknown"
        * 时间戳缺失但有 RU 值 → source="unknown"
        * API 异常 → source="failed"
    - crdb_ru_collector.is_data_fresh()
        * 数据新鲜返回 True
        * 数据陈旧返回 False
        * 数据缺失返回 False
        * 自定义 max_age_seconds
    - crdb_ru_collector._parse_iso_datetime()
        * ISO 时间戳解析
        * Z 后缀解析
        * 无效字符串返回 None
        * 空字符串返回 None
    - crdb_ru_collector.write_ru_to_kv_store()
        * 同时写入 crdb_ru_daily 和 crdb_ru_last_collected_at
    - prometheus_exporter._compute_crdb_ru_source_label()
        * source="official" 时 freshness < 阈值
        * source="unknown" 时数据陈旧
        * source="unknown" 时时间戳缺失
        * source="failed" 时无数据
        * source_gauge_value 正确(0/1/2)
    - prometheus_exporter.collect_metrics()
        * 包含 tgjiema_crdb_ru_source gauge
        * 包含 tgjiema_crdb_ru_freshness_seconds gauge
        * source="failed"/"unknown" 时 idle_ru 显示 -1
        * source="official" 时 idle_ru 显示真实值

测试策略:
    - 全部使用 monkeypatch + 内存 mock,不依赖真实 SQLite / R2 / CRDB
    - 中文注释,与项目其他测试保持一致
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 辅助: mock cache_store(用于 get_ru_status 读取 kv_store)
# ════════════════════════════════════════════════════════════════


class _FakeCacheStore:
    """模拟 cache_store:支持 get_kv/set_kv,_db 属性用于通过 getattr 检查。"""

    def __init__(self):
        self._kv: dict[str, str] = {}
        # _db 为非 None 表示 SQLite 已初始化
        # get_ru_status 通过 getattr(store, "_db", None) 判断
        self._db = MagicMock(name="fake_aiosqlite_connection")

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


def _install_fake_cache_store(monkeypatch, fake_store: _FakeCacheStore):
    """注入 fake cache_store 到 services.crdb_ru_collector 模块。

    get_ru_status() 内部通过 ``from database.cache_store import get_cache_store``
    懒加载,因此 monkeypatch 替换 ``database.cache_store.get_cache_store`` 即可。
    """
    fake_module = MagicMock(name="fake_cache_store_module")
    fake_module.get_cache_store = lambda: fake_store
    monkeypatch.setitem(sys.modules, "database.cache_store", fake_module)
    return fake_store


def _make_iso(seconds_ago: float) -> str:
    """生成距现在 seconds_ago 秒前的 UTC ISO 时间戳。"""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


# ════════════════════════════════════════════════════════════════
# 测试类 1: get_ru_status — source="failed"
# ════════════════════════════════════════════════════════════════


class TestGetRuStatusFailed:
    """R42 P1-10: get_ru_status() 在采集失败场景下应返回 source="failed"。"""

    @pytest.mark.asyncio
    async def test_api_failure_no_kv_history_returns_failed(self, monkeypatch):
        """CRDB API 失败 + kv_store 无历史数据 → source="failed"。"""
        from services import crdb_ru_collector as collector

        # mock CRDB API 调用失败
        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        # mock cache_store 无任何 RU 数据
        fake_store = _FakeCacheStore()
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        assert status["source"] == "failed"
        assert status["ru_value"] is None
        assert status["freshness_seconds"] is None
        assert status["last_collected_at"] == ""
        assert "failed" in status["details"] or "API" in status["details"]

    @pytest.mark.asyncio
    async def test_api_exception_returns_failed(self, monkeypatch):
        """CRDB API 抛异常 + kv_store 无数据 → source="failed"。"""
        from services import crdb_ru_collector as collector

        async def _raise_fetch():
            raise RuntimeError("网络中断")
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _raise_fetch)

        fake_store = _FakeCacheStore()
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        # API 异常被捕获,返回 None → 与无数据相同路径 → failed
        assert status["source"] == "failed"
        assert status["ru_value"] is None

    @pytest.mark.asyncio
    async def test_api_failure_with_stale_kv_history_returns_unknown(self, monkeypatch):
        """CRDB API 失败 + kv_store 有陈旧历史 → source="unknown"(非 failed)。

        场景:collector 上次成功采集过,但本次 API 调用失败,
        且 kv_store 中的数据已陈旧(超过阈值)→ source="unknown"。
        这区别于"从未采集过"的 failed,提醒运维 collector 可能中断。
        """
        from services import crdb_ru_collector as collector

        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        fake_store = _FakeCacheStore()
        # kv_store 有历史 RU 值
        fake_store._kv["crdb_ru_daily"] = "5000"
        # 但时间戳是 2 小时前(陈旧)
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(7200)
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        # API 失败但有历史值 → 使用 kv_store 值
        # 但时间戳陈旧 → source="unknown"(非 failed)
        assert status["source"] == "unknown"
        assert status["ru_value"] == 5000
        assert status["freshness_seconds"] is not None
        # int() 截断可能导致 7200.0 → 7199,容忍 1 秒截断误差
        assert status["freshness_seconds"] >= 7199


# ════════════════════════════════════════════════════════════════
# 测试类 2: get_ru_status — source="official"
# ════════════════════════════════════════════════════════════════


class TestGetRuStatusOfficial:
    """R42 P1-10: get_ru_status() 在采集成功场景下应返回 source="official"。"""

    @pytest.mark.asyncio
    async def test_api_success_fresh_data_returns_official(self, monkeypatch):
        """CRDB API 成功 + kv_store 时间戳新鲜 → source="official"。"""
        from services import crdb_ru_collector as collector

        async def _ok_fetch():
            return 12345.0
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _ok_fetch)

        fake_store = _FakeCacheStore()
        # kv_store 时间戳为 5 分钟前(新鲜)
        fake_store._kv["crdb_ru_daily"] = "12000"
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(300)
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        assert status["source"] == "official"
        # 优先使用 API 最新值
        assert status["ru_value"] == 12345
        assert status["freshness_seconds"] is not None
        assert status["freshness_seconds"] < collector.RU_DATA_FRESH_THRESHOLD
        assert status["last_collected_at"] != ""

    @pytest.mark.asyncio
    async def test_api_success_no_timestamp_returns_unknown(self, monkeypatch):
        """CRDB API 成功 + kv_store 缺时间戳 → source="unknown"。

        场景:API 返回了 RU 值,但 collector 从未写入时间戳(异常状态),
        无法判断数据新鲜度 → 视为 unknown。
        """
        from services import crdb_ru_collector as collector

        async def _ok_fetch():
            return 8000.0
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _ok_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "8000"
        # 故意不写入 crdb_ru_last_collected_at
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        # 有 RU 值但时间戳缺失 → unknown
        assert status["source"] == "unknown"
        assert status["freshness_seconds"] is None
        assert status["ru_value"] == 8000


# ════════════════════════════════════════════════════════════════
# 测试类 3: get_ru_status — source="unknown"
# ════════════════════════════════════════════════════════════════


class TestGetRuStatusUnknown:
    """R42 P1-10: get_ru_status() 在数据陈旧场景下应返回 source="unknown"。"""

    @pytest.mark.asyncio
    async def test_stale_data_returns_unknown(self, monkeypatch):
        """kv_store 时间戳陈旧(≥ 1 小时)→ source="unknown"。"""
        from services import crdb_ru_collector as collector

        # API 返回 None(模拟本次采集失败)
        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "9999"
        # 时间戳为 2 小时前(陈旧,超过 RU_DATA_FRESH_THRESHOLD=3600)
        stale_ts = _make_iso(7200)
        fake_store._kv["crdb_ru_last_collected_at"] = stale_ts
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()

        assert status["source"] == "unknown"
        assert status["ru_value"] == 9999
        assert status["freshness_seconds"] is not None
        # int() 截断可能导致 7200.0 → 7199,容忍 1 秒截断误差
        assert status["freshness_seconds"] >= 7199
        # details 中应提示"陈旧"
        assert "陈旧" in status["details"] or "stale" in status["details"].lower()

    @pytest.mark.asyncio
    async def test_threshold_boundary_returns_official(self, monkeypatch):
        """时间戳恰好等于阈值(3599 秒)→ source="official"(严格小于阈值)。"""
        from services import crdb_ru_collector as collector

        async def _ok_fetch():
            return 100.0
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _ok_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "100"
        # 3500 秒前(< 3600 阈值)
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(3500)
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()
        assert status["source"] == "official"

    @pytest.mark.asyncio
    async def test_threshold_boundary_at_threshold_returns_unknown(self, monkeypatch):
        """时间戳恰好等于阈值(3600 秒)→ source="unknown"(>= 阈值视为陈旧)。"""
        from services import crdb_ru_collector as collector

        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "200"
        # 3650 秒前(略超 3600 阈值,留出余量避免时钟漂移影响)
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(3650)
        _install_fake_cache_store(monkeypatch, fake_store)

        status = await collector.get_ru_status()
        assert status["source"] == "unknown"


# ════════════════════════════════════════════════════════════════
# 测试类 4: is_data_fresh()
# ════════════════════════════════════════════════════════════════


class TestIsDataFresh:
    """R42 P1-10: is_data_fresh() 数据新鲜度判断。"""

    @pytest.mark.asyncio
    async def test_fresh_data_returns_true(self, monkeypatch):
        """数据新鲜时 is_data_fresh 返回 True。"""
        from services import crdb_ru_collector as collector

        async def _ok_fetch():
            return 500.0
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _ok_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "500"
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(120)
        _install_fake_cache_store(monkeypatch, fake_store)

        assert await collector.is_data_fresh() is True

    @pytest.mark.asyncio
    async def test_stale_data_returns_false(self, monkeypatch):
        """数据陈旧时 is_data_fresh 返回 False。"""
        from services import crdb_ru_collector as collector

        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "500"
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(7200)
        _install_fake_cache_store(monkeypatch, fake_store)

        assert await collector.is_data_fresh() is False

    @pytest.mark.asyncio
    async def test_no_data_returns_false(self, monkeypatch):
        """无任何数据时 is_data_fresh 返回 False。"""
        from services import crdb_ru_collector as collector

        async def _fail_fetch():
            return None
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _fail_fetch)

        fake_store = _FakeCacheStore()
        _install_fake_cache_store(monkeypatch, fake_store)

        assert await collector.is_data_fresh() is False

    @pytest.mark.asyncio
    async def test_custom_max_age_seconds(self, monkeypatch):
        """自定义 max_age_seconds:5 分钟前的数据在默认阈值下新鲜,
        但在 max_age=60 下应判为陈旧。"""
        from services import crdb_ru_collector as collector

        async def _ok_fetch():
            return 100.0
        monkeypatch.setattr(collector, "fetch_ru_from_crdb_cloud", _ok_fetch)

        fake_store = _FakeCacheStore()
        fake_store._kv["crdb_ru_daily"] = "100"
        # 5 分钟前(300 秒,默认阈值 3600 下新鲜)
        fake_store._kv["crdb_ru_last_collected_at"] = _make_iso(300)
        _install_fake_cache_store(monkeypatch, fake_store)

        # 默认阈值 3600 秒下应新鲜
        assert await collector.is_data_fresh() is True
        # 自定义阈值 60 秒下应陈旧(300 秒 > 60 秒)
        assert await collector.is_data_fresh(max_age_seconds=60) is False


# ════════════════════════════════════════════════════════════════
# 测试类 5: _parse_iso_datetime()
# ════════════════════════════════════════════════════════════════


class TestParseIsoDatetime:
    """R42 P1-10: _parse_iso_datetime() ISO 时间戳解析。"""

    def test_parse_plain_iso(self):
        """解析无时区后缀的 ISO 时间戳。"""
        from services import crdb_ru_collector as collector

        dt = collector._parse_iso_datetime("2026-07-13T10:00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.day == 13
        assert dt.hour == 10

    def test_parse_z_suffix(self):
        """解析带 Z 后缀的 UTC ISO 时间戳。"""
        from services import crdb_ru_collector as collector

        dt = collector._parse_iso_datetime("2026-07-13T10:00:00Z")
        assert dt is not None
        assert dt.year == 2026
        # Z 应被替换为 +00:00,故 tzinfo 不为 None
        assert dt.tzinfo is not None

    def test_parse_microseconds(self):
        """解析带微秒的 ISO 时间戳。"""
        from services import crdb_ru_collector as collector

        dt = collector._parse_iso_datetime("2026-07-13T10:00:00.123456")
        assert dt is not None
        assert dt.microsecond == 123456

    def test_parse_invalid_returns_none(self):
        """无效字符串返回 None。"""
        from services import crdb_ru_collector as collector

        assert collector._parse_iso_datetime("not a timestamp") is None
        assert collector._parse_iso_datetime("2026/07/13 10:00:00") is None

    def test_parse_empty_returns_none(self):
        """空字符串返回 None。"""
        from services import crdb_ru_collector as collector

        assert collector._parse_iso_datetime("") is None
        assert collector._parse_iso_datetime(None) is None  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# 测试类 6: write_ru_to_kv_store()
# ════════════════════════════════════════════════════════════════


class TestWriteRuToKvStore:
    """R42 P1-10: write_ru_to_kv_store 同时写入 crdb_ru_daily 与 crdb_ru_last_collected_at。"""

    @pytest.mark.asyncio
    async def test_writes_both_ru_value_and_timestamp(self, monkeypatch):
        """写入成功后,kv_store 中应同时存在 crdb_ru_daily 和 crdb_ru_last_collected_at。"""
        from services import crdb_ru_collector as collector

        fake_store = _FakeCacheStore()
        _install_fake_cache_store(monkeypatch, fake_store)

        result = await collector.write_ru_to_kv_store(12345.6)

        assert result is True
        assert fake_store._kv["crdb_ru_daily"] == "12345.6"
        # 时间戳应为非空 ISO 字符串
        ts = fake_store._kv.get("crdb_ru_last_collected_at", "")
        assert ts != ""
        # 验证时间戳可被解析回 datetime
        from datetime import datetime as _dt
        # 替换 Z 后缀以便 fromisoformat 解析
        ts_clean = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        parsed = _dt.fromisoformat(ts_clean)
        assert parsed is not None

    @pytest.mark.asyncio
    async def test_write_failure_returns_false(self, monkeypatch):
        """cache_store 写入失败时返回 False。"""
        from services import crdb_ru_collector as collector

        # mock 一个会抛异常的 cache_store
        failing_store = _FakeCacheStore()

        async def _raise_set(key, value):
            raise RuntimeError("SQLite 锁竞争")
        failing_store.set_kv = _raise_set  # type: ignore[assignment]

        _install_fake_cache_store(monkeypatch, failing_store)

        result = await collector.write_ru_to_kv_store(100.0)
        assert result is False


# ════════════════════════════════════════════════════════════════
# 测试类 7: prometheus_exporter._compute_crdb_ru_source_label()
# ════════════════════════════════════════════════════════════════


class TestComputeCrdbRuSourceLabel:
    """R42 P1-10: prometheus_exporter._compute_crdb_ru_source_label 状态判定。"""

    def test_official_when_data_fresh(self, monkeypatch):
        """R54 P1-1: 新鲜数据 + 显式 source=official_cloud_api → source="official", gauge=1。"""
        from services import prometheus_exporter as pe

        recent_ts = _make_iso(120)  # 2 分钟前
        def _mock_kv(key, default=""):
            if key == "crdb_ru_daily":
                return "5000"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            # R54 P1-1: 显式 source 由 crdb_ru_collector 写入
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)

        source, freshness, gauge = pe._compute_crdb_ru_source_label()

        assert source == "official"
        assert 0 <= freshness < pe._RU_DATA_FRESH_THRESHOLD
        assert gauge == 1

    def test_unknown_when_data_stale(self, monkeypatch):
        """R54 P1-1: 有显式 source 但数据陈旧 → source="unknown", gauge=0。"""
        from services import prometheus_exporter as pe

        stale_ts = _make_iso(7200)  # 2 小时前
        def _mock_kv(key, default=""):
            if key == "crdb_ru_daily":
                return "5000"
            if key == "crdb_ru_last_collected_at":
                return stale_ts
            # R54 P1-1: 即使有显式 source,数据陈旧也降级为 unknown
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)

        source, freshness, gauge = pe._compute_crdb_ru_source_label()

        assert source == "unknown"
        assert freshness >= pe._RU_DATA_FRESH_THRESHOLD
        assert gauge == 0

    def test_unknown_when_timestamp_missing(self, monkeypatch):
        """有 RU 值但无时间戳 → source="unknown", freshness=-1。"""
        from services import prometheus_exporter as pe

        def _mock_kv(key, default=""):
            if key == "crdb_ru_daily":
                return "5000"
            # crdb_ru_last_collected_at 缺失
            return default
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)

        source, freshness, gauge = pe._compute_crdb_ru_source_label()

        assert source == "unknown"
        assert freshness < 0  # -1 表示从未采集或时间戳缺失
        assert gauge == 0

    def test_failed_when_no_data(self, monkeypatch):
        """无 RU 值且无时间戳 → source="failed", gauge=2。"""
        from services import prometheus_exporter as pe

        monkeypatch.setattr(pe, "_read_kv_value", lambda key, default="": default)
        source, freshness, gauge = pe._compute_crdb_ru_source_label()

        assert source == "failed"
        assert freshness < 0
        assert gauge == 2

    def test_failed_when_ru_value_invalid(self, monkeypatch):
        """RU 值无法解析为 float(如 "abc")→ 与无数据等价 → source="failed"。"""
        from services import prometheus_exporter as pe

        def _mock_kv(key, default=""):
            if key == "crdb_ru_daily":
                return "not_a_number"
            return default
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)

        source, freshness, gauge = pe._compute_crdb_ru_source_label()
        # ru_value=None 且 freshness<0 → failed
        assert source == "failed"
        assert gauge == 2


# ════════════════════════════════════════════════════════════════
# 测试类 8: collect_metrics 暴露 RU source/freshness 指标
# ════════════════════════════════════════════════════════════════


class TestPrometheusRuMetrics:
    """R42 P1-10: collect_metrics 暴露 tgjiema_crdb_ru_source / freshness_seconds 指标。"""

    def _patch_pe_basics(self, monkeypatch, kv_mock=None):
        """统一 patch prometheus_exporter 的 SQLite 依赖,避免真实数据库。"""
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

    def test_collect_metrics_includes_crdb_ru_source_gauge(self, monkeypatch):
        """collect_metrics 输出应包含 tgjiema_crdb_ru_source gauge。"""
        pe = self._patch_pe_basics(monkeypatch)

        output = pe.collect_metrics()
        assert "tgjiema_crdb_ru_source" in output
        assert "# HELP tgjiema_crdb_ru_source" in output
        assert "# TYPE tgjiema_crdb_ru_source gauge" in output

    def test_collect_metrics_includes_crdb_ru_freshness_gauge(self, monkeypatch):
        """collect_metrics 输出应包含 tgjiema_crdb_ru_freshness_seconds gauge。"""
        pe = self._patch_pe_basics(monkeypatch)

        output = pe.collect_metrics()
        assert "tgjiema_crdb_ru_freshness_seconds" in output
        assert "# HELP tgjiema_crdb_ru_freshness_seconds" in output
        assert "# TYPE tgjiema_crdb_ru_freshness_seconds gauge" in output

    def test_collect_metrics_ru_source_value_failed_when_no_data(self, monkeypatch):
        """无数据时 tgjiema_crdb_ru_source=2(failed)。"""
        pe = self._patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()
        # 找到 tgjiema_crdb_ru_source 行
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_ru_source "):
                # 应为 "tgjiema_crdb_ru_source 2"
                assert line.endswith(" 2"), \
                    f"无数据时 ru_source 应为 2(failed),实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_ru_source 指标行")

    def test_collect_metrics_ru_source_value_official_when_fresh(self, monkeypatch):
        """R54 P1-1: 显式 source=official_cloud_api + 数据新鲜 → tgjiema_crdb_ru_source=1(official)。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "50"
            # R54 P1-1: 显式 source 由 crdb_ru_collector 写入
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_ru_source "):
                assert line.endswith(" 1"), \
                    f"数据新鲜时 ru_source 应为 1(official),实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_ru_source 指标行")

    def test_collect_metrics_ru_source_value_unknown_when_stale(self, monkeypatch):
        """R54 P1-1: 有显式 source 但数据陈旧 → tgjiema_crdb_ru_source=0(unknown)。"""
        stale_ts = _make_iso(7200)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return stale_ts
            # R54 P1-1: 即使有显式 source,数据陈旧也降级为 unknown
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_ru_source "):
                assert line.endswith(" 0"), \
                    f"数据陈旧时 ru_source 应为 0(unknown),实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_ru_source 指标行")

    def test_collect_metrics_idle_ru_negative_when_failed(self, monkeypatch):
        """source="failed" 时 tgjiema_crdb_idle_ru_daily 应显示 -1。"""
        pe = self._patch_pe_basics(monkeypatch, kv_mock=lambda key, default="0": default)

        output = pe.collect_metrics()
        # 找到 tgjiema_crdb_idle_ru_daily 行
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_idle_ru_daily{"):
                # 应包含 source="failed" 与 -1.0
                assert 'source="failed"' in line, \
                    f"failed 状态下 source label 应为 'failed',实际: {line}"
                assert line.endswith(" -1.0"), \
                    f"failed 时 idle_ru 应显示 -1.0,实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_idle_ru_daily 指标行")

    def test_collect_metrics_idle_ru_negative_when_unknown(self, monkeypatch):
        """source="unknown" 时 tgjiema_crdb_idle_ru_daily 应显示 -1。"""
        stale_ts = _make_iso(7200)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return stale_ts
            if key == "crdb_idle_ru_daily":
                return "50"
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_idle_ru_daily{"):
                assert 'source="unknown"' in line, \
                    f"unknown 状态下 source label 应为 'unknown',实际: {line}"
                assert line.endswith(" -1.0"), \
                    f"unknown 时 idle_ru 应显示 -1.0,实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_idle_ru_daily 指标行")

    def test_collect_metrics_idle_ru_real_value_when_official(self, monkeypatch):
        """R54 P1-1: 显式 source=official_cloud_api + 数据新鲜 → source="official"。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            if key == "crdb_idle_ru_daily":
                return "42"
            # R54 P1-1: 显式 source
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_idle_ru_daily{"):
                assert 'source="official"' in line, \
                    f"official 状态下 source label 应为 'official',实际: {line}"
                assert line.endswith(" 42.0"), \
                    f"official 时 idle_ru 应显示真实值 42.0,实际: {line}"
                return
        pytest.fail("未找到 tgjiema_crdb_idle_ru_daily 指标行")

    def test_collect_metrics_freshness_seconds_correct_when_official(self, monkeypatch):
        """R54 P1-1: 数据新鲜 + 显式 source → freshness 应为非负数(< 阈值)。"""
        recent_ts = _make_iso(120)
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            if key == "crdb_ru_last_collected_at":
                return recent_ts
            # R54 P1-1: 显式 source
            if key == "crdb_ru_source":
                return "official_cloud_api"
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_ru_freshness_seconds "):
                value = float(line.split()[-1])
                assert 0 <= value < pe._RU_DATA_FRESH_THRESHOLD, \
                    f"新鲜数据 freshness 应 < {pe._RU_DATA_FRESH_THRESHOLD},实际: {value}"
                return
        pytest.fail("未找到 tgjiema_crdb_ru_freshness_seconds 指标行")

    def test_collect_metrics_freshness_negative_when_no_timestamp(self, monkeypatch):
        """无时间戳时 tgjiema_crdb_ru_freshness_seconds 应为 -1。"""
        def _mock_kv(key, default="0"):
            if key == "crdb_ru_daily":
                return "12345"
            # 无 crdb_ru_last_collected_at
            return default
        pe = self._patch_pe_basics(monkeypatch, kv_mock=_mock_kv)

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_crdb_ru_freshness_seconds "):
                value = float(line.split()[-1])
                assert value < 0, \
                    f"无时间戳时 freshness 应为 -1,实际: {value}"
                return
        pytest.fail("未找到 tgjiema_crdb_ru_freshness_seconds 指标行")


# ════════════════════════════════════════════════════════════════
# 测试类 9: RU_DATA_FRESH_THRESHOLD 常量
# ════════════════════════════════════════════════════════════════


class TestRuDataFreshThreshold:
    """R42 P1-10: 数据新鲜度阈值常量应一致(3600 秒)。"""

    def test_collector_threshold_is_3600(self):
        """crdb_ru_collector.RU_DATA_FRESH_THRESHOLD 应为 3600。"""
        from services import crdb_ru_collector as collector
        assert collector.RU_DATA_FRESH_THRESHOLD == 3600

    def test_exporter_threshold_matches_collector(self):
        """prometheus_exporter._RU_DATA_FRESH_THRESHOLD 应与 collector 一致(3600)。"""
        from services import prometheus_exporter as pe
        from services import crdb_ru_collector as collector
        assert pe._RU_DATA_FRESH_THRESHOLD == collector.RU_DATA_FRESH_THRESHOLD
        assert pe._RU_DATA_FRESH_THRESHOLD == 3600
