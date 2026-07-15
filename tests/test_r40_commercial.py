"""R40 商业化权限模块测试覆盖。

测试范围:
- entitlements: 6 方法(含 4 dataclass: Plan / Quota / Limits / EntitlementResult)
- quota_ledger: 8 方法(预留/结算/退款流水)
- rbac: 9 方法(含预定义角色常量)
- approval_workflow: 9 方法(审批工作流)

测试策略:
- AST 语法检查(兼容 Python 3.9,不依赖运行时 import)
- 文件存在性检查
- 关键 async 函数存在性检查
- dataclass 定义检查(entitlements)
- 预定义角色常量检查(rbac)
- 审批状态/操作常量检查(approval_workflow)
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
    """提取 AST 中所有 async def 函数名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _get_dataclasses(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 @dataclass 装饰的类名。"""
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == "dataclass":
                result.add(node.name)
            elif isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
                result.add(node.name)
    return result


# ════════════════════════════════════════════════════════════════
# 1. entitlements.py 测试(含 4 dataclass 检查)
# ════════════════════════════════════════════════════════════════

class TestEntitlements:
    """R40 §9.2: Entitlement Service — 统一套餐/配额/限制判定。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "entitlements.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "entitlements.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "entitlements.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        # R53 P0-5: set_user_plan 已私有化为 _set_user_plan_internal,
        # 公共生产 API 为 set_user_plan_via_command_bus
        required = {
            "get_plan", "get_quota", "get_limits", "check",
            "_set_user_plan_internal", "set_user_plan_via_command_bus",
            "get_plan_features",
        }
        missing = required - funcs
        assert not missing, f"entitlements.py 缺少方法: {missing}"

    def test_has_dataclass_definitions(self):
        """entitlements.py 应定义 Plan / Quota / Limits / EntitlementResult dataclass。"""
        tree = _parse_ast(SERVICES_DIR / "entitlements.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        dataclasses = _get_dataclasses(tree)
        required = {"Plan", "Quota", "Limits", "EntitlementResult"}
        missing = required - dataclasses
        assert not missing, f"entitlements.py 缺少 dataclass: {missing}"

    def test_has_membership_constants(self):
        """entitlements.py 应定义会员等级常量。"""
        source = (SERVICES_DIR / "entitlements.py").read_text(encoding="utf-8")
        assert "MEMBERSHIP_FREE" in source
        assert "MEMBERSHIP_BASIC" in source
        assert "MEMBERSHIP_PREMIUM" in source


# ════════════════════════════════════════════════════════════════
# 2. quota_ledger.py 测试
# ════════════════════════════════════════════════════════════════

class TestQuotaLedger:
    """R40 §9.2: Quota Ledger — 预留/结算/退款流水。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "quota_ledger.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "quota_ledger.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "quota_ledger.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "reserve", "settle", "refund", "get_balance",
            "get_reservation", "list_user_ledger",
            "cleanup_expired_reservations", "admin_adjust",
        }
        missing = required - funcs
        assert not missing, f"quota_ledger.py 缺少方法: {missing}"

    def test_has_ledger_type_constants(self):
        """quota_ledger.py 应定义流水类型常量。"""
        source = (SERVICES_DIR / "quota_ledger.py").read_text(encoding="utf-8")
        assert "LEDGER_TYPE_RESERVATION" in source
        assert "LEDGER_TYPE_SETTLEMENT" in source
        assert "LEDGER_TYPE_REFUND" in source
        assert "LEDGER_TYPE_ADJUSTMENT" in source

    def test_has_reservation_status_constants(self):
        """quota_ledger.py 应定义预留状态常量。"""
        source = (SERVICES_DIR / "quota_ledger.py").read_text(encoding="utf-8")
        assert "RESERVATION_STATUS_RESERVED" in source
        assert "RESERVATION_STATUS_SETTLED" in source
        assert "RESERVATION_STATUS_REFUNDED" in source


# ════════════════════════════════════════════════════════════════
# 3. rbac.py 测试(含预定义角色常量检查)
# ════════════════════════════════════════════════════════════════

class TestRBAC:
    """R40 §9.2: RBAC — 基于角色的访问控制。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "rbac.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "rbac.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "rbac.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "init_default_roles", "create_role", "assign_role", "revoke_role",
            "get_user_role", "check_permission", "list_roles",
            "list_user_permissions", "format_role_info",
        }
        missing = required - funcs
        assert not missing, f"rbac.py 缺少方法: {missing}"

    def test_has_predefined_role_constants(self):
        """rbac.py 应定义预定义角色常量。"""
        source = (SERVICES_DIR / "rbac.py").read_text(encoding="utf-8")
        assert "ROLE_SUPER_ADMIN" in source
        assert "ROLE_SECURITY" in source
        assert "ROLE_OPS" in source
        assert "ROLE_SUPPORT" in source
        assert "ROLE_OPERATOR" in source

    def test_has_permission_constants(self):
        """rbac.py 应定义权限标识常量。"""
        source = (SERVICES_DIR / "rbac.py").read_text(encoding="utf-8")
        assert "PERMISSION_VIEW_USERS" in source
        assert "PERMISSION_BAN_USER" in source
        assert "PERMISSION_APPROVE_TAKEDOWN" in source
        assert "PERMISSION_MANAGE_ROLES" in source


# ════════════════════════════════════════════════════════════════
# 4. approval_workflow.py 测试
# ════════════════════════════════════════════════════════════════

class TestApprovalWorkflow:
    """R40 §9.2: 审批工作流 — 高风险操作二次确认。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "approval_workflow.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "approval_workflow.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "approval_workflow.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "create_approval", "approve", "reject", "cancel",
            "get_approval", "list_pending", "list_by_action",
            "requires_approval", "format_approval",
        }
        missing = required - funcs
        assert not missing, f"approval_workflow.py 缺少方法: {missing}"

    def test_has_approval_action_constants(self):
        """approval_workflow.py 应定义审批操作类型常量。"""
        source = (SERVICES_DIR / "approval_workflow.py").read_text(encoding="utf-8")
        assert "APPROVAL_ACTION_TAKEDOWN" in source
        assert "APPROVAL_ACTION_BAN" in source
        assert "APPROVAL_ACTION_FACTORY_RESET" in source

    def test_has_approval_status_constants(self):
        """approval_workflow.py 应定义审批状态常量。"""
        source = (SERVICES_DIR / "approval_workflow.py").read_text(encoding="utf-8")
        assert "APPROVAL_STATUS_PENDING" in source
        assert "APPROVAL_STATUS_APPROVED" in source
        assert "APPROVAL_STATUS_REJECTED" in source
        assert "APPROVAL_STATUS_CANCELLED" in source
