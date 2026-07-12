"""R40 内容安全与合规模块测试覆盖。

测试范围:
- content_reports: 11 方法(举报/下架/封禁/申诉/审计闭环)
- content_policy: 10 方法(含 2 dataclass: PolicyResult / FileMeta)
- data_lifecycle: 8 方法(导出/删除/保留期/访问日志)

测试策略:
- AST 语法检查(兼容 Python 3.9,不依赖运行时 import)
- 文件存在性检查
- 关键 async/sync 函数存在性检查
- dataclass 定义检查(content_policy)
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
    """提取 AST 中所有 async def 函数名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _get_all_funcs(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 def / async def 函数名(含同步与异步)。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _get_dataclasses(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 @dataclass 装饰的类名。"""
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            # @dataclass
            if isinstance(dec, ast.Name) and dec.id == "dataclass":
                result.add(node.name)
            # @dataclasses.dataclass
            elif isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
                result.add(node.name)
    return result


# ════════════════════════════════════════════════════════════════
# 1. content_reports.py 测试
# ════════════════════════════════════════════════════════════════

class TestContentReports:
    """R40 §9.4: 内容安全与合规 — 举报/下架/封禁/申诉/审计闭环。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "content_reports.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "content_reports.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "content_reports.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "create_report", "takedown_content", "ban_user", "unban_user",
            "appeal_report", "resolve_report", "list_reports", "get_report",
            "check_user_banned", "format_report", "_notify_user",
        }
        missing = required - funcs
        assert not missing, f"content_reports.py 缺少方法: {missing}"

    def test_has_status_constants(self):
        """content_reports.py 应定义举报状态常量。"""
        source = (SERVICES_DIR / "content_reports.py").read_text(encoding="utf-8")
        assert "REPORT_STATUS_PENDING" in source
        assert "REPORT_STATUS_TAKEDOWN" in source
        assert "REPORT_STATUS_RESOLVED" in source
        assert "REPORT_STATUS_REJECTED" in source

    def test_has_target_type_constants(self):
        """content_reports.py 应定义举报目标类型常量。"""
        source = (SERVICES_DIR / "content_reports.py").read_text(encoding="utf-8")
        assert "TARGET_TYPE_FILE" in source
        assert "TARGET_TYPE_USER" in source
        assert "TARGET_TYPE_CODE" in source


# ════════════════════════════════════════════════════════════════
# 2. content_policy.py 测试(含 dataclass 检查)
# ════════════════════════════════════════════════════════════════

class TestContentPolicy:
    """R40 §9.4: 文件策略插件 — 类型/大小/恶意内容检查。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "content_policy.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "content_policy.py")
        assert tree is not None

    def test_has_required_functions(self):
        """content_policy.py 包含同步与异步函数,使用 _get_all_funcs 提取。"""
        tree = _parse_ast(SERVICES_DIR / "content_policy.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_all_funcs(tree)
        required = {
            "register_plugin", "unregister_plugin", "list_plugins",
            "check_file", "_check_file_size", "_check_file_type",
            "_check_file_name", "_init_builtin_plugins",
        }
        missing = required - funcs
        assert not missing, f"content_policy.py 缺少方法: {missing}"

    def test_has_dataclass_definitions(self):
        """content_policy.py 应定义 PolicyResult 和 FileMeta dataclass。"""
        tree = _parse_ast(SERVICES_DIR / "content_policy.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        dataclasses = _get_dataclasses(tree)
        required = {"PolicyResult", "FileMeta"}
        missing = required - dataclasses
        assert not missing, f"content_policy.py 缺少 dataclass: {missing}"

    def test_has_default_constants(self):
        """content_policy.py 应定义默认阈值常量。"""
        source = (SERVICES_DIR / "content_policy.py").read_text(encoding="utf-8")
        assert "DEFAULT_MAX_FILE_SIZE" in source
        assert "DEFAULT_BLOCKED_EXTENSIONS" in source
        assert "DEFAULT_MAX_FILENAME_LEN" in source


# ════════════════════════════════════════════════════════════════
# 3. data_lifecycle.py 测试
# ════════════════════════════════════════════════════════════════

class TestDataLifecycle:
    """R40 §9.4: 数据生命周期 — 导出/删除/保留期/访问日志。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "data_lifecycle.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "data_lifecycle.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "data_lifecycle.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {
            "export_user_data", "delete_user_data", "set_retention",
            "get_retention", "cleanup_expired_data", "log_admin_access",
            "list_admin_access_logs", "check_retention_compliance",
        }
        missing = required - funcs
        assert not missing, f"data_lifecycle.py 缺少方法: {missing}"

    def test_has_retention_constants(self):
        """data_lifecycle.py 应定义保留期常量。"""
        source = (SERVICES_DIR / "data_lifecycle.py").read_text(encoding="utf-8")
        assert "DEFAULT_RETENTION_DAYS" in source
        assert "RETENTION_PERMANENT" in source
