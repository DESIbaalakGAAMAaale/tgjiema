"""R40 管理运维模块测试覆盖。

测试范围:
- repair_console: 10 方法(Outbox/DLQ/Replication/Relay 修复控制台)
- topology_view: 7 方法(拓扑可视化)
- ru_cost_center: 7 方法(RU 成本中心)
- maintenance_mode: 8 方法(维护模式,含 MAINTENANCE_KEY 常量)
- disaster_recovery: 13 方法(灾备控制台)

测试策略:
- AST 语法检查(兼容 Python 3.9,不依赖运行时 import)
- 文件存在性检查
- 关键 async 函数存在性检查(含私有辅助函数)
- MAINTENANCE_KEY 常量检查(maintenance_mode)
- 关键常量存在性检查
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


def _get_async_funcs(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 async def 函数名(含私有辅助函数)。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


# ════════════════════════════════════════════════════════════════
# 1. repair_console.py 测试
# ════════════════════════════════════════════════════════════════

class TestRepairConsole:
    """R40 §9.3: Repair Console — Outbox/DLQ/Replication/Relay 修复控制台。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "repair_console.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "repair_console.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "repair_console.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "list_outbox", "retry_outbox", "skip_outbox",
            "list_dlq", "replay_dlq",
            "list_replication_failures", "retry_replication",
            "list_relay_issues", "repair_relay", "get_repair_overview",
        }
        missing = required - funcs
        assert not missing, f"repair_console.py 缺少方法: {missing}"


# ════════════════════════════════════════════════════════════════
# 2. topology_view.py 测试
# ════════════════════════════════════════════════════════════════

class TestTopologyView:
    """R40 §9.3: 拓扑可视化 — 副本因子/频道健康/FloodWait/账号风险/R100延迟。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "topology_view.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "topology_view.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "topology_view.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "get_topology", "get_channel_health", "get_account_risk",
            "get_r100_delay", "get_replica_status",
            "format_topology", "get_health_summary",
        }
        missing = required - funcs
        assert not missing, f"topology_view.py 缺少方法: {missing}"

    def test_has_replication_factor_constant(self):
        """topology_view.py 应定义副本因子目标常量。"""
        source = (SERVICES_DIR / "topology_view.py").read_text(encoding="utf-8")
        assert "DEFAULT_TARGET_REPLICATION_FACTOR" in source


# ════════════════════════════════════════════════════════════════
# 3. ru_cost_center.py 测试
# ════════════════════════════════════════════════════════════════

class TestRUCostCenter:
    """R40 §9.3: RU 成本中心 — 按服务/功能/千次操作计算 RU。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "ru_cost_center.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "ru_cost_center.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "ru_cost_center.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "record_usage", "get_daily_report", "get_cost_by_service",
            "get_cost_per_1k", "get_ru_budget", "check_ru_alert",
            "generate_cost_report",
        }
        missing = required - funcs
        assert not missing, f"ru_cost_center.py 缺少方法: {missing}"

    def test_has_ru_unit_constants(self):
        """ru_cost_center.py 应定义 RU 单价常量。"""
        source = (SERVICES_DIR / "ru_cost_center.py").read_text(encoding="utf-8")
        assert "RU_PER_READ" in source
        assert "RU_PER_WRITE" in source
        assert "RU_PER_QUERY" in source

    def test_has_threshold_constants(self):
        """ru_cost_center.py 应定义告警阈值常量。"""
        source = (SERVICES_DIR / "ru_cost_center.py").read_text(encoding="utf-8")
        assert "WARNING_THRESHOLD" in source
        assert "CRITICAL_THRESHOLD" in source


# ════════════════════════════════════════════════════════════════
# 4. maintenance_mode.py 测试(含 MAINTENANCE_KEY 常量检查)
# ════════════════════════════════════════════════════════════════

class TestMaintenanceMode:
    """R40 §9.3: 维护模式 — 停止新上传 + 排空队列 + 备份 + 迁移 + 验证 + 恢复。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "maintenance_mode.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "maintenance_mode.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "maintenance_mode.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "_write_audit_log", "enable", "disable", "is_enabled",
            "get_status", "drain_queues", "check_readiness",
            "execute_maintenance_workflow",
        }
        missing = required - funcs
        assert not missing, f"maintenance_mode.py 缺少方法: {missing}"

    def test_has_maintenance_key_constant(self):
        """maintenance_mode.py 应定义 MAINTENANCE_KEY 常量。"""
        source = (SERVICES_DIR / "maintenance_mode.py").read_text(encoding="utf-8")
        assert "MAINTENANCE_KEY" in source

    def test_has_maintenance_state_id_constant(self):
        """maintenance_mode.py 应定义 MAINTENANCE_STATE_ID 常量。"""
        source = (SERVICES_DIR / "maintenance_mode.py").read_text(encoding="utf-8")
        assert "MAINTENANCE_STATE_ID" in source


# ════════════════════════════════════════════════════════════════
# 5. disaster_recovery.py 测试
# ════════════════════════════════════════════════════════════════

class TestDisasterRecovery:
    """R40 §9.3: 灾备控制台 — 备份/key_id/恢复演练/RPO-RTO/校验。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "disaster_recovery.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "disaster_recovery.py")
        assert tree is not None

    def test_has_required_functions(self):
        """disaster_recovery.py 共 13 个 async 方法(含 3 个私有辅助函数)。"""
        tree = _parse_ast(SERVICES_DIR / "disaster_recovery.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            # 公开 API (10)
            "list_backups", "get_backup_info", "trigger_backup",
            "verify_backup", "restore", "get_rpo_rto",
            "run_recovery_drill", "get_recovery_history",
            "get_backup_schedule", "format_disaster_status",
            # 私有辅助 (3)
            "_append_backup_history", "_append_recovery_history",
            "_write_audit_log",
        }
        missing = required - funcs
        assert not missing, f"disaster_recovery.py 缺少方法: {missing}"

    def test_has_kv_constants(self):
        """disaster_recovery.py 应定义 kv_store 键名常量。"""
        source = (SERVICES_DIR / "disaster_recovery.py").read_text(encoding="utf-8")
        assert "KV_BACKUP_HISTORY" in source
        assert "KV_RECOVERY_HISTORY" in source
        assert "KV_LAST_BACKUP_AT" in source

    def test_has_rpo_rto_constants(self):
        """disaster_recovery.py 应定义 RPO/RTO 默认目标常量。"""
        source = (SERVICES_DIR / "disaster_recovery.py").read_text(encoding="utf-8")
        assert "DEFAULT_RPO_SECONDS" in source
        assert "DEFAULT_RTO_SECONDS" in source
