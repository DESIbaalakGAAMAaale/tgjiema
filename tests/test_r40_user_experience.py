"""R40 用户体验模块测试覆盖。

测试范围:
- task_center: 8 方法
- upload_receipt: 4 方法
- collections: 8 方法
- notifications: 7 方法
- user_repair: 5 方法

测试策略:
- AST 语法检查(兼容 Python 3.9,不依赖运行时 import)
- 文件存在性检查
- 关键 async 函数存在性检查
- 关键常量存在性检查(notifications)
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

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


def _get_async_funcs(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 async def 函数名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


# ════════════════════════════════════════════════════════════════
# 1. task_center.py 测试
# ════════════════════════════════════════════════════════════════

class TestTaskCenter:
    """R40 §9.1.1: 统一任务中心。"""

    def test_file_exists(self):
        path = SERVICES_DIR / "task_center.py"
        assert path.exists(), "services/task_center.py 应存在"

    def test_ast_parseable(self):
        path = SERVICES_DIR / "task_center.py"
        tree = _parse_ast(path)
        assert tree is not None, "task_center.py 应可被 AST 解析"

    def test_has_required_functions(self):
        path = SERVICES_DIR / "task_center.py"
        tree = _parse_ast(path)
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {"create_task", "update_progress", "complete_task",
                    "fail_task", "cancel_task", "get_task",
                    "list_user_tasks", "format_task_status"}
        missing = required - funcs
        assert not missing, f"task_center.py 缺少方法: {missing}"

    def test_has_status_constants(self):
        """task_center.py 应定义任务状态常量。"""
        source = (SERVICES_DIR / "task_center.py").read_text(encoding="utf-8")
        assert "STATUS_PENDING" in source
        assert "STATUS_COMPLETED" in source
        assert "STATUS_FAILED" in source
        assert "STATUS_CANCELLED" in source


# ════════════════════════════════════════════════════════════════
# 2. upload_receipt.py 测试
# ════════════════════════════════════════════════════════════════

class TestUploadReceipt:
    """R40 §9.1.2: 上传回执。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "upload_receipt.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "upload_receipt.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "upload_receipt.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {"generate_receipt", "get_receipt", "get_upload_status", "format_receipt"}
        assert required <= funcs, f"缺少: {required - funcs}"


# ════════════════════════════════════════════════════════════════
# 3. collections.py 测试
# ════════════════════════════════════════════════════════════════

class TestCollections:
    """R40 §9.1.3: 文件集合。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "collections.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "collections.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "collections.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {"create_collection", "add_files", "remove_files",
                    "get_collection", "list_collections", "update_version",
                    "check_items_status", "format_collection_info"}
        assert required <= funcs, f"缺少: {required - funcs}"


# ════════════════════════════════════════════════════════════════
# 4. notifications.py 测试
# ════════════════════════════════════════════════════════════════

class TestNotifications:
    """R40 §9.1.4: 可靠通知。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "notifications.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "notifications.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "notifications.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {"send", "mark_read", "mark_all_read", "list_unread",
                    "list_all", "broadcast", "format_notification"}
        assert required <= funcs, f"缺少: {required - funcs}"

    def test_has_notification_type_constants(self):
        """notifications.py 应定义通知类型常量。"""
        source = (SERVICES_DIR / "notifications.py").read_text(encoding="utf-8")
        assert "NOTIF_TYPE_READY" in source
        assert "NOTIF_TYPE_R100_DELAY" in source
        assert "NOTIF_TYPE_REPLICA_SHORT" in source


# ════════════════════════════════════════════════════════════════
# 5. user_repair.py 测试
# ════════════════════════════════════════════════════════════════

class TestUserRepair:
    """R40 §9.1.5: 用户自助修复。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "user_repair.py").exists()

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "user_repair.py")
        assert tree is not None

    def test_has_required_functions(self):
        tree = _parse_ast(SERVICES_DIR / "user_repair.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_async_funcs(tree)
        required = {"reindex_code", "regenerate_code", "get_failure_reason",
                    "check_repair_eligibility", "format_failure_reason"}
        assert required <= funcs, f"缺少: {required - funcs}"

    def test_has_reason_constants(self):
        """user_repair.py 应定义失败原因枚举常量。"""
        source = (SERVICES_DIR / "user_repair.py").read_text(encoding="utf-8")
        assert "REASON_EXPIRED" in source
        assert "REASON_DELETED" in source
        assert "REASON_CHANNEL_LOST" in source
