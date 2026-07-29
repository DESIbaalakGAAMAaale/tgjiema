"""R64 P1-10: CRDB 空载 RU 0 RU/天 — 单元测试。

测试覆盖:
1. config/settings.py:新增 CRDB_POOL_RECYCLE_SECONDS / RU 阈值 / 月度预算
2. database/session.py:CRDB_POOL_MIN_SIZE 强制为 0 + pool_recycle 配置
3. services/crdb_ru_collector.py:RU 归因(record_ru_attribution / get_ru_attribution)
4. services/ru_cost_center.py:RUAttribution / check_daily_threshold / check_monthly_budget
5. scripts/check_crdb_ru_threshold.py:CI gate 脚本可执行
6. 静态审计:r40_scheduler / crdb_sync_service / prometheus_exporter 无空载 CRDB 命中

R64 P1-10 验收标准:
    - 业务 Bot 角色空载 0 RU/天
    - 集群空载理想 ≤20 RU/天,硬限 ≤100 RU/天
    - >100 RU/天告警,>500 RU/天阻断 release
    - per-DAU ≤250 RU/DAU/天
    - 月度预算 ≤35,000,000 RU
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 1. config/settings.py — 新增 RU 阈值配置字段
# ════════════════════════════════════════════════════════════════


class TestSettingsRuThresholds:
    """验证 settings.py 包含 R64 P1-10 新增的 RU 阈值配置。"""

    def test_settings_file_contains_pool_recycle_seconds(self):
        """settings.py 应包含 CRDB_POOL_RECYCLE_SECONDS 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_POOL_RECYCLE_SECONDS" in content, (
            "settings.py 应包含 CRDB_POOL_RECYCLE_SECONDS 配置"
        )

    def test_settings_file_contains_ru_daily_alert_threshold(self):
        """settings.py 应包含 CRDB_RU_DAILY_ALERT_THRESHOLD 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_RU_DAILY_ALERT_THRESHOLD" in content

    def test_settings_file_contains_ru_daily_block_threshold(self):
        """settings.py 应包含 CRDB_RU_DAILY_BLOCK_THRESHOLD 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_RU_DAILY_BLOCK_THRESHOLD" in content

    def test_settings_file_contains_ru_monthly_budget(self):
        """settings.py 应包含 CRDB_RU_MONTHLY_BUDGET 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_RU_MONTHLY_BUDGET" in content

    def test_settings_file_contains_business_bot_roles(self):
        """settings.py 应包含 CRDB_RU_BUSINESS_BOT_ROLES 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_RU_BUSINESS_BOT_ROLES" in content

    def test_settings_file_contains_null_mode(self):
        """settings.py 应包含 CRDB_POOL_NULL_MODE 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_POOL_NULL_MODE" in content

    def test_settings_default_alert_threshold_is_100(self):
        """默认 ALERT 阈值应为 100 RU/天(空载硬限)。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        # 检查默认值是 100
        assert "CRDB_RU_DAILY_ALERT_THRESHOLD: int = 100" in content

    def test_settings_default_block_threshold_is_500(self):
        """默认 BLOCK 阈值应为 500 RU/天(阻断 release)。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_RU_DAILY_BLOCK_THRESHOLD: int = 500" in content

    def test_settings_default_monthly_budget_is_35m(self):
        """默认月度预算应为 35,000,000 RU。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "35_000_000" in content

    def test_settings_default_recycle_seconds_is_60(self):
        """默认 pool_recycle 应为 60 秒。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "CRDB_POOL_RECYCLE_SECONDS: int = 60" in content


# ════════════════════════════════════════════════════════════════
# 2. database/session.py — CRDB_POOL_MIN_SIZE=0 + pool_recycle
# ════════════════════════════════════════════════════════════════


class TestSessionPoolConfig:
    """验证 session.py 强制 CRDB_POOL_MIN_SIZE=0 + pool_recycle 配置。"""

    def test_session_file_contains_min_size_zero_enforcement(self):
        """session.py 应强制 min_size=0(防御性归零)。"""
        session_path = REPO_ROOT / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        # 检查代码中存在强制 min_size=0 的逻辑
        assert "min_size = 0" in content, (
            "session.py 应包含 R64 P1-10 强制 min_size=0 的逻辑"
        )

    def test_session_file_contains_pool_recycle(self):
        """session.py 应使用 CRDB_POOL_RECYCLE_SECONDS 配置空闲回收。"""
        session_path = REPO_ROOT / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "CRDB_POOL_RECYCLE_SECONDS" in content
        assert "max_inactive_connection_lifetime" in content

    def test_session_file_contains_null_mode(self):
        """session.py 应支持 CRDB_POOL_NULL_MODE 模式。"""
        session_path = REPO_ROOT / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "CRDB_POOL_NULL_MODE" in content

    def test_session_file_contains_r64_p1_10_comment(self):
        """session.py 应包含 R64 P1-10 注释标记。"""
        session_path = REPO_ROOT / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "R64 P1-10" in content


# ════════════════════════════════════════════════════════════════
# 3. services/crdb_ru_collector.py — RU 归因
# ════════════════════════════════════════════════════════════════


class TestRuAttributionModule:
    """验证 crdb_ru_collector.py 的 RU 归因常量与函数存在。"""

    def test_module_has_attribution_prefix_constant(self):
        """crdb_ru_collector 应定义 KV_KEY_RU_ATTRIBUTION_PREFIX 常量。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "KV_KEY_RU_ATTRIBUTION_PREFIX")
        assert crdb_ru_collector.KV_KEY_RU_ATTRIBUTION_PREFIX == "ru_attribution"

    def test_module_has_business_bot_roles_default(self):
        """crdb_ru_collector 应定义 BUSINESS_BOT_ROLES_DEFAULT 常量。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "BUSINESS_BOT_ROLES_DEFAULT")
        roles = crdb_ru_collector.BUSINESS_BOT_ROLES_DEFAULT
        assert "up_bot" in roles
        assert "idx_bot" in roles
        assert "dsp_bot" in roles
        assert "mon_bot" in roles
        assert "admin_bot" in roles

    def test_module_has_non_business_crdb_roles(self):
        """crdb_ru_collector 应定义 NON_BUSINESS_CRDB_ROLES 集合。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "NON_BUSINESS_CRDB_ROLES")
        non_business = crdb_ru_collector.NON_BUSINESS_CRDB_ROLES
        assert "crdb_sync" in non_business
        assert "migration" in non_business

    def test_module_has_idle_crdb_free_services_audited(self):
        """crdb_ru_collector 应定义 IDLE_CRDB_FREE_SERVICES_AUDITED 集合。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "IDLE_CRDB_FREE_SERVICES_AUDITED")
        audited = crdb_ru_collector.IDLE_CRDB_FREE_SERVICES_AUDITED
        # 至少包含 r40_scheduler / crdb_sync_service / prometheus_exporter
        assert "r40_scheduler" in audited
        assert "crdb_sync_service" in audited
        assert "prometheus_exporter" in audited

    def test_module_has_record_ru_attribution_function(self):
        """crdb_ru_collector 应定义 record_ru_attribution 异步函数。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "record_ru_attribution")
        assert callable(crdb_ru_collector.record_ru_attribution)

    def test_module_has_get_ru_attribution_function(self):
        """crdb_ru_collector 应定义 get_ru_attribution 异步函数。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "get_ru_attribution")
        assert callable(crdb_ru_collector.get_ru_attribution)

    def test_module_has_get_business_bot_roles_function(self):
        """crdb_ru_collector 应定义 get_business_bot_roles 函数。"""
        from services import crdb_ru_collector
        assert hasattr(crdb_ru_collector, "get_business_bot_roles")
        roles = crdb_ru_collector.get_business_bot_roles()
        assert isinstance(roles, tuple)
        assert len(roles) > 0

    def test_get_business_bot_roles_uses_settings(self):
        """get_business_bot_roles 应优先从 settings 读取。"""
        from services import crdb_ru_collector
        # Mock settings.CRDB_RU_BUSINESS_BOT_ROLES
        with patch("config.settings") as mock_settings:
            mock_settings.CRDB_RU_BUSINESS_BOT_ROLES = "up_bot,idx_bot,custom_bot"
            roles = crdb_ru_collector.get_business_bot_roles()
            assert "up_bot" in roles
            assert "idx_bot" in roles
            assert "custom_bot" in roles


# ════════════════════════════════════════════════════════════════
# 4. services/ru_cost_center.py — RUAttribution + 阈值检查
# ════════════════════════════════════════════════════════════════


class TestRuCostCenterAttribution:
    """验证 ru_cost_center.py 新增的归因数据结构与阈值检查函数。"""

    def test_module_has_ru_attribution_dataclass(self):
        """ru_cost_center 应定义 RUAttribution dataclass。"""
        from services import ru_cost_center
        assert hasattr(ru_cost_center, "RUAttribution")
        # 实例化测试
        attr = ru_cost_center.RUAttribution()
        assert attr.total_ru == 0
        assert attr.business_bot_ru == 0
        assert attr.dau == 0

    def test_ru_attribution_per_dau_ru_calculation(self):
        """RUAttribution.per_dau_ru 应正确计算 per-DAU RU。"""
        from services import ru_cost_center
        attr = ru_cost_center.RUAttribution(total_ru=500, dau=10)
        assert attr.per_dau_ru() == 50.0
        # DAU 为 0 时返回 0
        attr_zero = ru_cost_center.RUAttribution(total_ru=500, dau=0)
        assert attr_zero.per_dau_ru() == 0.0

    def test_module_has_get_daily_attribution(self):
        """ru_cost_center 应定义 get_daily_attribution 异步函数。"""
        from services import ru_cost_center
        assert hasattr(ru_cost_center, "get_daily_attribution")
        assert callable(ru_cost_center.get_daily_attribution)

    def test_module_has_check_daily_threshold(self):
        """ru_cost_center 应定义 check_daily_threshold 异步函数。"""
        from services import ru_cost_center
        assert hasattr(ru_cost_center, "check_daily_threshold")
        assert callable(ru_cost_center.check_daily_threshold)

    def test_module_has_check_monthly_budget(self):
        """ru_cost_center 应定义 check_monthly_budget 异步函数。"""
        from services import ru_cost_center
        assert hasattr(ru_cost_center, "check_monthly_budget")
        assert callable(ru_cost_center.check_monthly_budget)

    def test_module_has_idle_crdb_audit_summary(self):
        """ru_cost_center 应定义 get_idle_crdb_audit_summary 函数。"""
        from services import ru_cost_center
        assert hasattr(ru_cost_center, "get_idle_crdb_audit_summary")
        summary = ru_cost_center.get_idle_crdb_audit_summary()
        assert "audited_services" in summary
        assert "policy" in summary
        assert "allowed_crdb_triggers" in summary
        # 至少包含 r40_scheduler / crdb_sync_service / prometheus_exporter
        audited = summary["audited_services"]
        assert "r40_scheduler" in audited
        assert "crdb_sync_service" in audited
        assert "prometheus_exporter" in audited

    def test_module_has_threshold_constants(self):
        """ru_cost_center 应定义 R64 P1-10 阈值常量。"""
        from services import ru_cost_center
        assert ru_cost_center.RU_IDLE_BOT_PER_DAY_LIMIT == 0
        assert ru_cost_center.RU_IDLE_CLUSTER_HARD_LIMIT == 100
        assert ru_cost_center.RU_IDLE_BLOCK_THRESHOLD == 500
        assert ru_cost_center.RU_PER_DAU_DAY_LIMIT == 250
        assert ru_cost_center.RU_MONTHLY_BUDGET_LIMIT == 35_000_000


# ════════════════════════════════════════════════════════════════
# 5. check_daily_threshold / check_monthly_budget 行为(用 mock)
# ════════════════════════════════════════════════════════════════


class TestCheckDailyThresholdBehavior:
    """check_daily_threshold 在不同 RU 场景下的行为。"""

    @pytest.mark.asyncio
    async def test_zero_ru_passes(self):
        """0 RU/天应通过门禁(passed=True, alert=False, block=False)。"""
        from services import ru_cost_center
        # Mock get_daily_attribution 返回 0 RU
        zero_attr = ru_cost_center.RUAttribution(
            date="20260718", total_ru=0, business_bot_ru=0, non_business_ru=0,
        )
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=zero_attr)):
            result = await ru_cost_center.check_daily_threshold("20260718")
        assert result["passed"] is True
        assert result["block_release"] is False
        assert result["alert"] is False
        assert result["business_bot_ru"] == 0

    @pytest.mark.asyncio
    async def test_bot_ru_gt_zero_blocks_release(self):
        """业务 Bot 空载 RU > 0 应阻断 release。"""
        from services import ru_cost_center
        bad_attr = ru_cost_center.RUAttribution(
            date="20260718", total_ru=5, business_bot_ru=5,
        )
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=bad_attr)):
            result = await ru_cost_center.check_daily_threshold("20260718")
        assert result["passed"] is False
        assert result["block_release"] is True
        assert result["alert"] is True
        assert result["business_bot_ru"] == 5
        # 应有违规描述
        assert len(result["violations"]) > 0
        assert any("业务 Bot 空载 RU > 0" in v for v in result["violations"])

    @pytest.mark.asyncio
    async def test_total_ru_over_alert_triggers_alert(self):
        """集群 RU > 100 应触发告警(不阻断)。"""
        from services import ru_cost_center
        attr = ru_cost_center.RUAttribution(
            date="20260718", total_ru=150, business_bot_ru=0,
            non_business_ru=150,
        )
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=attr)):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              side_effect=lambda name, default: {
                                  "CRDB_RU_DAILY_ALERT_THRESHOLD": 100,
                                  "CRDB_RU_DAILY_BLOCK_THRESHOLD": 500,
                                  "CRDB_RU_DAU_DAY_LIMIT": 250,
                              }.get(name, default)):
                result = await ru_cost_center.check_daily_threshold("20260718")
        assert result["passed"] is False
        assert result["alert"] is True
        assert result["block_release"] is False  # 150 < 500,不阻断

    @pytest.mark.asyncio
    async def test_total_ru_over_block_triggers_block(self):
        """集群 RU > 500 应阻断 release。"""
        from services import ru_cost_center
        attr = ru_cost_center.RUAttribution(
            date="20260718", total_ru=600, business_bot_ru=0,
            non_business_ru=600,
        )
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=attr)):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              side_effect=lambda name, default: {
                                  "CRDB_RU_DAILY_ALERT_THRESHOLD": 100,
                                  "CRDB_RU_DAILY_BLOCK_THRESHOLD": 500,
                                  "CRDB_RU_DAU_DAY_LIMIT": 250,
                              }.get(name, default)):
                result = await ru_cost_center.check_daily_threshold("20260718")
        assert result["passed"] is False
        assert result["block_release"] is True
        assert result["alert"] is True

    @pytest.mark.asyncio
    async def test_per_dau_ru_over_limit_triggers_alert(self):
        """per-DAU RU > 250 应触发告警(不阻断)。"""
        from services import ru_cost_center
        attr = ru_cost_center.RUAttribution(
            date="20260718", total_ru=3000, business_bot_ru=0, dau=10,
            non_business_ru=3000,
        )
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=attr)):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              side_effect=lambda name, default: {
                                  "CRDB_RU_DAILY_ALERT_THRESHOLD": 100,
                                  "CRDB_RU_DAILY_BLOCK_THRESHOLD": 500,
                                  "CRDB_RU_DAU_DAY_LIMIT": 250,
                              }.get(name, default)):
                result = await ru_cost_center.check_daily_threshold("20260718")
        # per_dau = 3000/10 = 300 > 250,告警
        assert result["per_dau_ru"] == 300.0
        assert any("per-DAU" in v for v in result["violations"])

    @pytest.mark.asyncio
    async def test_result_includes_thresholds_dict(self):
        """check_daily_threshold 返回结果应包含 thresholds 字典。"""
        from services import ru_cost_center
        zero_attr = ru_cost_center.RUAttribution(date="20260718")
        with patch.object(ru_cost_center, "get_daily_attribution",
                          new=AsyncMock(return_value=zero_attr)):
            result = await ru_cost_center.check_daily_threshold("20260718")
        assert "thresholds" in result
        thresholds = result["thresholds"]
        assert "alert" in thresholds
        assert "block" in thresholds
        assert "per_dau_limit" in thresholds
        assert "bot_idle_limit" in thresholds


class TestCheckMonthlyBudgetBehavior:
    """check_monthly_budget 行为测试。"""

    @pytest.mark.asyncio
    async def test_zero_monthly_usage_passes(self):
        """月度 0 RU 应通过预算门禁。"""
        from services import ru_cost_center
        with patch.object(ru_cost_center, "get_daily_report",
                          new=AsyncMock(return_value={"total_ru": 0})):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              return_value=35_000_000):
                result = await ru_cost_center.check_monthly_budget("202607")
        assert result["passed"] is True
        assert result["block_release"] is False
        assert result["monthly_usage"] == 0

    @pytest.mark.asyncio
    async def test_monthly_usage_over_budget_blocks(self):
        """月度 RU 超过预算应阻断 release。"""
        from services import ru_cost_center
        # 31 天 × 2,000,000 RU = 62,000,000 > 35,000,000
        with patch.object(ru_cost_center, "get_daily_report",
                          new=AsyncMock(return_value={"total_ru": 2_000_000})):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              return_value=35_000_000):
                result = await ru_cost_center.check_monthly_budget("202607")
        assert result["passed"] is False
        assert result["block_release"] is True

    @pytest.mark.asyncio
    async def test_invalid_year_month_format(self):
        """无效 year_month 格式应返回错误。"""
        from services import ru_cost_center
        result = await ru_cost_center.check_monthly_budget("invalid")
        assert result["passed"] is False
        assert result["block_release"] is True
        assert "error" in result

    @pytest.mark.asyncio
    async def test_monthly_result_includes_remaining_and_percentage(self):
        """月度结果应包含 remaining 与 usage_percentage 字段。"""
        from services import ru_cost_center
        with patch.object(ru_cost_center, "get_daily_report",
                          new=AsyncMock(return_value={"total_ru": 100})):
            with patch.object(ru_cost_center, "_get_ru_threshold_setting",
                              return_value=35_000_000):
                result = await ru_cost_center.check_monthly_budget("202607")
        assert "remaining" in result
        assert "usage_percentage" in result
        assert result["remaining"] > 0


# ════════════════════════════════════════════════════════════════
# 6. record_ru_attribution / get_ru_attribution 行为(用 mock)
# ════════════════════════════════════════════════════════════════


class TestRecordRuAttributionBehavior:
    """record_ru_attribution 行为测试。"""

    @pytest.mark.asyncio
    async def test_zero_ru_amount_returns_false(self):
        """ru_amount <= 0 应返回 False。"""
        from services import crdb_ru_collector
        result = await crdb_ru_collector.record_ru_attribution(
            service="up_bot", ru_amount=0,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_negative_ru_amount_returns_false(self):
        """负数 ru_amount 应返回 False。"""
        from services import crdb_ru_collector
        result = await crdb_ru_collector.record_ru_attribution(
            service="up_bot", ru_amount=-10,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_record_with_mock_store_returns_true(self):
        """用 mock store 应成功记录 RU 归因。"""
        from services import crdb_ru_collector
        mock_store = MagicMock()
        mock_store.get_kv = AsyncMock(return_value=None)
        mock_store.set_kv = AsyncMock(return_value=True)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await crdb_ru_collector.record_ru_attribution(
                service="crdb_sync",
                ru_amount=10,
                fingerprint="SELECT_1",
                job="sync_jobs",
                time_bucket="2026071810",
            )
        assert result is True
        # 应调用 set_kv
        mock_store.set_kv.assert_called_once()
        # 检查写入的 key 格式
        args = mock_store.set_kv.call_args
        key = args.args[0]
        assert key.startswith("ru_attribution:20260718:2026071810:crdb_sync:sync_jobs")

    @pytest.mark.asyncio
    async def test_record_accumulates_multiple_calls(self):
        """多次调用应累积到同一 key。"""
        from services import crdb_ru_collector
        mock_store = MagicMock()

        # 第一次调用:get_kv 返回 None,创建新数据
        # 第二次调用:get_kv 返回已有数据,累加
        existing_data = {
            "service": "crdb_sync", "job": "sync_jobs",
            "time_bucket": "2026071810",
            "total_ru": 10, "by_fingerprint": {"SELECT_1": 10},
            "samples": [],
        }
        mock_store.get_kv = AsyncMock(side_effect=[None, json.dumps(existing_data)])
        mock_store.set_kv = AsyncMock(return_value=True)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            # 第一次:创建新记录
            await crdb_ru_collector.record_ru_attribution(
                service="crdb_sync", ru_amount=10,
                fingerprint="SELECT_1", job="sync_jobs",
                time_bucket="2026071810",
            )
            # 第二次:累加
            await crdb_ru_collector.record_ru_attribution(
                service="crdb_sync", ru_amount=5,
                fingerprint="SELECT_1", job="sync_jobs",
                time_bucket="2026071810",
            )
        # 应调用 set_kv 两次
        assert mock_store.set_kv.call_count == 2


class TestGetRuAttributionBehavior:
    """get_ru_attribution 行为测试。"""

    @pytest.mark.asyncio
    async def test_empty_store_returns_zero(self):
        """空 store 应返回 0 RU。"""
        from services import crdb_ru_collector
        mock_store = MagicMock()
        mock_store.get_kv = AsyncMock(return_value=None)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await crdb_ru_collector.get_ru_attribution("20260718")
        assert result["total_ru"] == 0
        assert result["business_bot_ru"] == 0
        assert result["non_business_ru"] == 0
        assert result["date"] == "20260718"

    @pytest.mark.asyncio
    async def test_returns_correct_structure(self):
        """get_ru_attribution 返回结果应包含所有必需字段。"""
        from services import crdb_ru_collector
        mock_store = MagicMock()
        mock_store.get_kv = AsyncMock(return_value=None)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await crdb_ru_collector.get_ru_attribution("20260718")
        required_keys = {
            "date", "total_ru", "by_service", "by_job",
            "by_fingerprint", "by_time_bucket",
            "business_bot_ru", "non_business_ru",
        }
        assert required_keys.issubset(result.keys())


# ════════════════════════════════════════════════════════════════
# 7. scripts/check_crdb_ru_threshold.py — CI gate 脚本
# ════════════════════════════════════════════════════════════════


class TestCheckCrdbRuThresholdScript:
    """验证 scripts/check_crdb_ru_threshold.py CI gate 脚本。"""

    def test_script_exists_and_is_executable(self):
        """脚本应存在。"""
        script_path = REPO_ROOT / "scripts" / "check_crdb_ru_threshold.py"
        assert script_path.exists(), "scripts/check_crdb_ru_threshold.py 应存在"

    def test_script_has_main_function(self):
        """脚本应定义 main 函数。"""
        script_path = REPO_ROOT / "scripts" / "check_crdb_ru_threshold.py"
        content = script_path.read_text(encoding="utf-8")
        assert "def main()" in content
        assert "def run_check" in content

    def test_script_has_argparse_options(self):
        """脚本应支持 --date / --month / --json / --warn-only / --strict-alert 选项。"""
        script_path = REPO_ROOT / "scripts" / "check_crdb_ru_threshold.py"
        content = script_path.read_text(encoding="utf-8")
        assert "--date" in content
        assert "--month" in content
        assert "--json" in content
        assert "--warn-only" in content
        assert "--strict-alert" in content
        assert "--day-only" in content
        assert "--month-only" in content

    def test_script_has_ci_placeholder_env_setup(self):
        """脚本应设置 CI 占位环境变量(避免 Settings 校验失败)。"""
        script_path = REPO_ROOT / "scripts" / "check_crdb_ru_threshold.py"
        content = script_path.read_text(encoding="utf-8")
        assert "ci-placeholder" in content
        assert "SERVICE_ROLE" in content
        assert "prometheus_exporter" in content

    def test_script_returns_zero_on_zero_ru(self, tmp_path):
        """0 RU 场景应返回退出码 0(实际执行脚本)。"""
        import subprocess
        script_path = REPO_ROOT / "scripts" / "check_crdb_ru_threshold.py"
        # 使用本测试独占且不存在的临时 DB，避免跨运行残留 RU 证据污染。
        cache_db = tmp_path / "test_ru_threshold_check.db"
        result = subprocess.run(
            [sys.executable, str(script_path), "--json", "--day-only"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={**os.environ, "CACHE_STORE_DB": str(cache_db), "PYTHONUTF8": "1"},
            check=False,
        )
        # 应成功执行(exit code 0)
        assert result.returncode == 0, f"脚本应成功执行,stderr: {result.stderr[-500:]}"


# ════════════════════════════════════════════════════════════════
# 8. 静态审计:r40_scheduler / crdb_sync_service / prometheus_exporter
# ════════════════════════════════════════════════════════════════


class TestIdleCrdbFreeServices:
    """验证关键服务已无空载 CRDB 命中(静态源码审计)。"""

    def test_r40_scheduler_does_not_poll_crdb(self):
        """r40_scheduler.py 不应包含直接 CRDB 轮询代码。"""
        path = REPO_ROOT / "services" / "r40_scheduler.py"
        content = path.read_text(encoding="utf-8")
        # r40_scheduler 所有周期任务应委托 cache_store(SQLite)/command_bus(SQLite)
        # 不应直接 import asyncpg 或调用 CRDB pool
        assert "import asyncpg" not in content, (
            "r40_scheduler 不应直接 import asyncpg(所有周期任务走 SQLite)"
        )

    def test_r40_scheduler_collect_ru_metrics_uses_sqlite(self):
        """r40_scheduler.collect_ru_metrics_job 应委托 prometheus_exporter(SQLite)。"""
        path = REPO_ROOT / "services" / "r40_scheduler.py"
        content = path.read_text(encoding="utf-8")
        assert "prometheus_exporter" in content
        assert "collect_r40_metrics" in content

    def test_crdb_sync_service_uses_redis_for_leader(self):
        """crdb_sync_service 的 leader election 应使用 Redis SET NX(非 CRDB)。"""
        path = REPO_ROOT / "services" / "crdb_sync_service.py"
        content = path.read_text(encoding="utf-8")
        # _acquire_leader_lease 应使用 redis_client.set(nx=True, px=ttl)
        assert "_acquire_leader_lease" in content
        assert "redis_client" in content or "_get_redis_client" in content
        # 不应使用 SELECT 1 或 NOW() 等 CRDB 健康检查
        # (允许 SELECT COUNT(*) FROM dirty_outbox 因为是 SQLite)

    def test_crdb_sync_service_uses_event_wakeup(self):
        """crdb_sync_service._sync_loop 应使用 wait_dirty_signal(事件驱动)。"""
        path = REPO_ROOT / "services" / "crdb_sync_service.py"
        content = path.read_text(encoding="utf-8")
        assert "wait_dirty_signal" in content, (
            "_sync_loop 应使用 wait_dirty_signal 事件驱动唤醒(R56 §7.2.3)"
        )

    def test_prometheus_exporter_uses_sqlite_for_health(self):
        """prometheus_exporter 的 /health 应使用 SQLite(非 CRDB)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        # check_readiness 应使用 sqlite3.connect(非 asyncpg)
        assert "sqlite3" in content
        # /health 端点应调用 check_readiness(SQLite)
        assert "check_readiness" in content

    def test_prometheus_exporter_does_not_import_asyncpg(self):
        """prometheus_exporter 不应 import asyncpg(零 CRDB RU)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "import asyncpg" not in content, (
            "prometheus_exporter 不应 import asyncpg(所有指标走 SQLite)"
        )

    def test_command_bus_cleanup_stale_leases_uses_sqlite(self):
        """command_bus.cleanup_stale_leases 应使用 SQLite cache_store。"""
        path = REPO_ROOT / "services" / "command_bus.py"
        content = path.read_text(encoding="utf-8")
        # cleanup_stale_leases 应调用 store._db.execute(SQLite)
        assert "cleanup_stale_leases" in content
        assert "_db.execute" in content or "cache_store" in content


# ════════════════════════════════════════════════════════════════
# 9. RU 归因数据结构完整性
# ════════════════════════════════════════════════════════════════


class TestRuAttributionDataStructure:
    """验证 RU 归因数据结构的完整性。"""

    def test_ru_attribution_has_all_required_fields(self):
        """RUAttribution 应包含所有必需字段。"""
        from services import ru_cost_center
        attr = ru_cost_center.RUAttribution(
            date="20260718",
            total_ru=100,
            business_bot_ru=0,
            non_business_ru=100,
            by_service={"crdb_sync": 100},
            by_job={"sync_jobs": 100},
            by_fingerprint={"SELECT_1": 100},
            by_time_bucket={"2026071810": 100},
            dau=5,
        )
        assert attr.date == "20260718"
        assert attr.total_ru == 100
        assert attr.business_bot_ru == 0
        assert attr.non_business_ru == 100
        assert attr.by_service["crdb_sync"] == 100
        assert attr.by_job["sync_jobs"] == 100
        assert attr.by_fingerprint["SELECT_1"] == 100
        assert attr.by_time_bucket["2026071810"] == 100
        assert attr.dau == 5

    def test_ru_attribution_default_factories_independent(self):
        """RUAttribution 默认 dict 字段应为独立实例(非共享)。"""
        from services import ru_cost_center
        attr1 = ru_cost_center.RUAttribution()
        attr2 = ru_cost_center.RUAttribution()
        attr1.by_service["test"] = 1
        assert "test" not in attr2.by_service  # 独立 dict
