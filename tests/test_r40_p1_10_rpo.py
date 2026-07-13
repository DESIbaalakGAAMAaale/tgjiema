"""R40 P1-10: RPO 无备份时不应误判合规测试。

问题:
    R40 P0-7 已修复 ``get_last_backup_age`` 返回 None(无备份)与
    ``get_rpo_rto`` 的 ``rpo_compliant=False``(无备份时违规),
    但下游仍有 2 处遗漏:

    1. ``format_disaster_status`` 在 ``last_backup_age=None`` 时,
       使用 ``status.get("last_backup_age", 0)`` 默认 0,
       输出"最近备份距今=0s",与 RPO 合规判定矛盾(看起来刚备份但实际无备份)。
       应改为 None 时输出"无备份"。

    2. ``admin/templates/disaster_recovery.html`` 引用 ``last_backup_at``
       (时间戳字符串),但 ``get_rpo_rto()`` 返回 ``last_backup_age``
       (秒数),字段名不匹配,模板永远显示 N/A,无法反映真实备份时间。
       且当无备份时未提示"无备份"。

    3. ``services/prometheus_exporter.py`` 的 ``backup_age_seconds`` 指标
       无备份时返回 -1(便于告警区分),但需测试覆盖以保证不回归。

整改:
    1. ``format_disaster_status``: None 时输出"无备份",不再显示"0s"。
    2. ``admin/templates/disaster_recovery.html``: 引用正确字段 ``last_backup_age``,
       并在 None 时显示"无备份"提示;同时新增 RPO 合规状态显示。
    3. ``prometheus_exporter`` 无备份返回 -1 已实现,补充回归测试。

测试策略:
    - 使用 _FakeCacheStore(无 last_backup_at 键)模拟无备份场景
    - monkeypatch get_cache_store 注入 fake
    - 直接断言 format_disaster_status 输出文本
    - 直接读模板源码断言字段引用
    - 直接读 prometheus_exporter 源码断言 -1 分支
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ════════════════════════════════════════════════════════════════
# 辅助: FakeCacheStore(无 last_backup_at 键 → 模拟无备份)
# ════════════════════════════════════════════════════════════════

class _FakeCacheStore:
    """模拟 cache_store:仅提供 get_kv/set_kv 接口。

    无 last_backup_at 键时 get_kv 返回 None,模拟"无备份"场景。
    """

    def __init__(self, kv: dict[str, str] | None = None):
        self._kv = kv or {}
        # _db 属性为 None(避免触发事务路径)
        self._db = None

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


# ════════════════════════════════════════════════════════════════
# 1. get_last_backup_age 无备份返回 None(P0-7 已修复,P1-10 回归测试)
# ════════════════════════════════════════════════════════════════

class TestGetLastBackupAgeNone:
    """R40 P1-10: get_last_backup_age 无备份时返回 None(回归测试)。"""

    @pytest.mark.asyncio
    async def test_no_backup_returns_none(self, monkeypatch):
        """无 last_backup_at 键时,get_last_backup_age 应返回 None。"""
        cache = _FakeCacheStore(kv={})  # 空 kv,无任何键
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)

        from services.disaster_recovery import get_last_backup_age
        result = await get_last_backup_age()
        assert result is None, \
            f"无备份时 get_last_backup_age 应返回 None,实际: {result}"

    @pytest.mark.asyncio
    async def test_with_backup_returns_seconds(self, monkeypatch):
        """有 last_backup_at 键时,get_last_backup_age 应返回正秒数。"""
        import datetime as _dt
        recent = (_dt.datetime.now() - _dt.timedelta(seconds=120)).isoformat()
        cache = _FakeCacheStore(kv={"last_backup_at": recent})
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)

        from services.disaster_recovery import get_last_backup_age
        result = await get_last_backup_age()
        assert result is not None, "有备份时应返回非 None"
        assert result > 0, f"2 分钟前的备份应返回正秒数,实际: {result}"
        # 容忍 ±10 秒误差(测试执行时间)
        assert 100 <= result <= 200, \
            f"2 分钟前的备份应约 120s,实际: {result}"


# ════════════════════════════════════════════════════════════════
# 2. get_rpo_rto 无备份时 rpo_compliant=False(P0-7 已修复,P1-10 回归测试)
# ════════════════════════════════════════════════════════════════

class TestGetRpoRtoNone:
    """R40 P1-10: get_rpo_rto 无备份时 rpo_compliant=False(回归测试)。"""

    @pytest.mark.asyncio
    async def test_no_backup_rpo_not_compliant(self, monkeypatch):
        """无备份时 rpo_compliant 必须为 False(违规)。"""
        cache = _FakeCacheStore(kv={})
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)

        from services.disaster_recovery import get_rpo_rto
        result = await get_rpo_rto()
        assert result["rpo_compliant"] is False, \
            f"无备份时 rpo_compliant 必须为 False,实际: {result['rpo_compliant']}"
        # last_backup_age 应为 None(便于 UI 区分"无备份" vs "刚备份")
        assert result["last_backup_age"] is None, \
            f"无备份时 last_backup_age 应为 None,实际: {result['last_backup_age']}"

    @pytest.mark.asyncio
    async def test_with_backup_rpo_compliant(self, monkeypatch):
        """有备份且在 RPO 窗口内时 rpo_compliant=True。"""
        import datetime as _dt
        # 1 分钟前备份,在 RPO 窗口内
        recent = (_dt.datetime.now() - _dt.timedelta(seconds=60)).isoformat()
        cache = _FakeCacheStore(kv={"last_backup_at": recent})
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)
        # 设置 RPO 为 6 小时,确保 1 分钟前的备份在窗口内
        import config
        monkeypatch.setattr(config.settings, "BACKUP_RPO_SECONDS", 21600, raising=False)
        monkeypatch.setattr(config.settings, "BACKUP_RTO_SECONDS", 1800, raising=False)

        from services.disaster_recovery import get_rpo_rto
        result = await get_rpo_rto()
        assert result["rpo_compliant"] is True, \
            f"1 分钟前备份应 RPO 合规(窗口 6h),实际: {result['rpo_compliant']}"
        assert result["last_backup_age"] is not None


# ════════════════════════════════════════════════════════════════
# 3. format_disaster_status 处理 None(P1-10 主修复点)
# ════════════════════════════════════════════════════════════════

class TestFormatDisasterStatusHandlesNone:
    """R40 P1-10: format_disaster_status 应正确处理 last_backup_age=None。"""

    @pytest.mark.asyncio
    async def test_no_backup_shows_wu_beifen(self, monkeypatch):
        """无备份(last_backup_age=None)时,文本应包含"无备份"而非"0s"。"""
        # 模拟无备份场景:get_rpo_rto 返回 last_backup_age=None, rpo_compliant=False
        status = {
            "rpo_seconds": 21600,  # 6 小时
            "rto_seconds": 1800,
            "last_backup_age": None,  # 无备份
            "estimated_recovery_time": 1800,
            "rpo_compliant": False,
            "rto_compliant": True,
        }
        from services.disaster_recovery import format_disaster_status
        text = await format_disaster_status(status)

        # 不应误显示"最近备份距今=0s"(0 ≤ rpo_seconds 会导致看起来合规)
        assert "最近备份距今=0s" not in text, \
            f"无备份时不应显示'0s'(易误判合规),实际文本: {text}"
        assert "最近备份距今=None" not in text, \
            f"无备份时不应显示'None'(技术细节外露),实际文本: {text}"
        # 应明确提示"无备份"
        assert "无备份" in text, \
            f"无备份时应显示'无备份'提示,实际文本: {text}"

    @pytest.mark.asyncio
    async def test_with_backup_shows_seconds(self, monkeypatch):
        """有备份(last_backup_age=120)时,文本应显示具体秒数。"""
        status = {
            "rpo_seconds": 21600,
            "rto_seconds": 1800,
            "last_backup_age": 120,  # 2 分钟前
            "estimated_recovery_time": 1800,
            "rpo_compliant": True,
            "rto_compliant": True,
        }
        from services.disaster_recovery import format_disaster_status
        text = await format_disaster_status(status)

        # 应显示具体秒数
        assert "最近备份距今=120s" in text, \
            f"有备份时应显示秒数'120s',实际文本: {text}"

    @pytest.mark.asyncio
    async def test_no_backup_rpo_violation_shown(self, monkeypatch):
        """无备份时,文本应明确显示 RPO 违规(✗ 违规)。"""
        status = {
            "rpo_seconds": 21600,
            "rto_seconds": 1800,
            "last_backup_age": None,
            "estimated_recovery_time": 1800,
            "rpo_compliant": False,  # 无备份 → 违规
            "rto_compliant": True,
        }
        from services.disaster_recovery import format_disaster_status
        text = await format_disaster_status(status)

        # RPO 行应显示违规(✗ 违规)
        assert "✗ 违规" in text, \
            f"无备份时 RPO 应显示违规,实际文本: {text}"
        # RPO 行不应显示合规
        # 提取 [RPO] 行单独验证
        rpo_line = [ln for ln in text.split("\n") if "[RPO]" in ln]
        assert rpo_line, "文本应包含 [RPO] 行"
        assert "✓ 合规" not in rpo_line[0], \
            f"无备份时 RPO 行不应显示合规,实际: {rpo_line[0]}"

    @pytest.mark.asyncio
    async def test_full_text_format_with_none(self, monkeypatch):
        """format_disaster_status 完整输出在无备份时应一致可用。"""
        status = {
            "rpo_seconds": 21600,
            "rto_seconds": 1800,
            "last_backup_age": None,
            "estimated_recovery_time": 1800,
            "rpo_compliant": False,
            "rto_compliant": True,
            "enabled": True,
            "interval_minutes": 360,
            "retention_days": 7,
        }
        from services.disaster_recovery import format_disaster_status
        text = await format_disaster_status(status)

        # 应包含灾备标题
        assert "灾备状态" in text
        # 应包含 RPO/RTO 行
        assert "[RPO]" in text
        assert "[RTO]" in text
        # 应包含无备份提示
        assert "无备份" in text
        # 不应抛异常(None 处理后输出可读)


# ════════════════════════════════════════════════════════════════
# 4. Admin 模板 disaster_recovery.html 字段引用与 None 处理
# ════════════════════════════════════════════════════════════════

class TestAdminTemplateHandlesNone:
    """R40 P1-10: 灾备控制台模板应正确引用字段并处理 None。"""

    def test_template_references_last_backup_age(self):
        """模板应引用 last_backup_age(秒数),而非错误的 last_backup_at。"""
        template_path = (
            Path(__file__).parent.parent
            / "admin" / "templates" / "disaster_recovery.html"
        )
        src = template_path.read_text(encoding="utf-8")
        # 必须引用 last_backup_age(get_rpo_rto 实际返回的字段)
        assert "last_backup_age" in src, \
            "disaster_recovery.html 必须引用 last_backup_age 字段(get_rpo_rto 返回的秒数)"

    def test_template_handles_none_last_backup_age(self):
        """模板应在 last_backup_age 为 None 时显示'无备份'提示。"""
        template_path = (
            Path(__file__).parent.parent
            / "admin" / "templates" / "disaster_recovery.html"
        )
        src = template_path.read_text(encoding="utf-8")
        # 应有 None 判断(显示"无备份")
        # Jinja2 语法: {% if rpo_rto.last_backup_age is none %} 或 is not none
        assert ("is none" in src or "is not none" in src or "无备份" in src), \
            "disaster_recovery.html 必须处理 last_backup_age 为 None 的情况(显示无备份)"

    def test_template_shows_rpo_compliance(self):
        """模板应显示 RPO 合规状态(便于管理员直观判断)。"""
        template_path = (
            Path(__file__).parent.parent
            / "admin" / "templates" / "disaster_recovery.html"
        )
        src = template_path.read_text(encoding="utf-8")
        # 应引用 rpo_compliant 字段
        assert "rpo_compliant" in src, \
            "disaster_recovery.html 应显示 RPO 合规状态(rpo_compliant 字段)"


# ════════════════════════════════════════════════════════════════
# 5. Prometheus exporter 无备份返回 -1(回归测试)
# ════════════════════════════════════════════════════════════════

class TestPrometheusExporterBackupAge:
    """R40 P1-10: prometheus_exporter 无备份时 backup_age_seconds=-1。"""

    def test_exporter_returns_negative_one_when_no_backup(self):
        """prometheus_exporter 源码应有"无备份返回 -1"分支。"""
        exporter_path = (
            Path(__file__).parent.parent
            / "services" / "prometheus_exporter.py"
        )
        src = exporter_path.read_text(encoding="utf-8")
        # 必须有 -1.0 或 -1 分支(无备份时)
        assert "-1" in src, \
            "prometheus_exporter.py 必须在无备份时返回 -1(便于告警区分)"

    def test_exporter_backup_age_help_text(self):
        """backup_age_seconds 指标的 HELP 应说明 -1 含义。"""
        exporter_path = (
            Path(__file__).parent.parent
            / "services" / "prometheus_exporter.py"
        )
        src = exporter_path.read_text(encoding="utf-8")
        # HELP 文本应说明 -1 表示无备份
        assert "backup_age_seconds" in src, \
            "prometheus_exporter.py 必须定义 backup_age_seconds 指标"
        # HELP 注释应包含 -1 或 never 说明
        help_section_match = False
        for line in src.split("\n"):
            if "backup_age_seconds" in line and "HELP" in line:
                if "-1" in line or "never" in line.lower():
                    help_section_match = True
                    break
        assert help_section_match, \
            "backup_age_seconds 的 HELP 注释应说明 -1 表示无备份"


# ════════════════════════════════════════════════════════════════
# 6. 集成测试:无备份场景下完整流程
# ════════════════════════════════════════════════════════════════

class TestNoBackupEndToEnd:
    """R40 P1-10: 无备份场景下 get_rpo_rto → format_disaster_status 全流程。"""

    @pytest.mark.asyncio
    async def test_no_backup_full_flow(self, monkeypatch):
        """无备份时:get_rpo_rto 返回 None+违规,format_disaster_status 显示无备份。"""
        cache = _FakeCacheStore(kv={})  # 无任何键
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)

        from services.disaster_recovery import get_rpo_rto, format_disaster_status

        # 1. 获取 RPO/RTO 状态
        status = await get_rpo_rto()
        # 验证无备份场景的关键字段
        assert status["last_backup_age"] is None, "无备份时 last_backup_age 应为 None"
        assert status["rpo_compliant"] is False, "无备份时 rpo_compliant 应为 False"

        # 2. 格式化为文本
        text = await format_disaster_status(status)
        # 3. 验证文本不误显示"0s"
        assert "最近备份距今=0s" not in text, \
            f"无备份时不应误显示'0s',实际: {text}"
        # 4. 验证文本明确提示无备份
        assert "无备份" in text, \
            f"无备份时应提示'无备份',实际: {text}"
        # 5. 验证 RPO 违规明确显示
        assert "✗ 违规" in text, \
            f"无备份时 RPO 应显示违规,实际: {text}"

    @pytest.mark.asyncio
    async def test_with_backup_full_flow(self, monkeypatch):
        """有备份时:get_rpo_rto 返回秒数+合规,format_disaster_status 显示秒数。"""
        import datetime as _dt
        recent = (_dt.datetime.now() - _dt.timedelta(seconds=300)).isoformat()
        cache = _FakeCacheStore(kv={"last_backup_at": recent})
        monkeypatch.setattr("services.disaster_recovery.get_cache_store", lambda: cache)
        # 设置 RPO 为 6 小时,确保 5 分钟前的备份在窗口内
        import config
        monkeypatch.setattr(config.settings, "BACKUP_RPO_SECONDS", 21600, raising=False)
        monkeypatch.setattr(config.settings, "BACKUP_RTO_SECONDS", 1800, raising=False)

        from services.disaster_recovery import get_rpo_rto, format_disaster_status

        status = await get_rpo_rto()
        assert status["last_backup_age"] is not None
        assert status["last_backup_age"] > 0
        assert status["rpo_compliant"] is True  # 5 分钟 < 6 小时 RPO

        text = await format_disaster_status(status)
        # 应显示秒数(允许一定误差)
        assert "最近备份距今=" in text
        assert "s" in text
        # 应显示合规
        assert "✓ 合规" in text
        # 不应显示无备份
        assert "无备份" not in text
