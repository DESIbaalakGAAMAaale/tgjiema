"""R42 P1-11: CommandBus/RBAC 静态强制 AST gate 测试。

测试范围:
- scripts/check_commandbus_gate.py: AST 静态门禁脚本
  * 检测高风险 API 直接调用(backup_engine.restore / content_reports.takedown_file /
    content_reports.ban_user / rbac.assign_role / rbac.revoke_role /
    maintenance_mode.disable / users.purge_user / credentials.rotate)
  * 白名单允许的调用方(services/approval_executor.py / services/command_bus.py /
    services/approval_workflow.py / tests/ / scripts/)
  * maintenance_mode.disable_with_authorization 不被误报
  * AST 解析错误时不误报

测试策略:
- 创建临时 repo 结构,monkeypatch REPO_ROOT
- 在临时 repo 中放置违规 / 干净 .py 文件
- 调用 check() 验证 exit_code 和 violations 列表
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── 辅助函数 ──────────────────────────────────────────


def _load_gate_module():
    """通过 importlib 加载 check_commandbus_gate.py 为独立模块实例。

    每次返回全新的模块对象,避免跨用例状态污染。
    """
    spec = importlib.util.spec_from_file_location(
        "_check_commandbus_gate_r42_test",
        SCRIPTS_DIR / "check_commandbus_gate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_check(tmp_path, files):
    """创建临时 repo 结构并运行 check_commandbus_gate.check()。

    Args:
        tmp_path: pytest tmp_path fixture(Path)
        files: dict, {相对路径: 文件内容}
            如 {"bots/bad.py": "backup_engine.restore()\\n"}

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, module, method}, ...]
    """
    fake_root = tmp_path / "fake_repo"
    fake_root.mkdir()
    for rel_path, content in files.items():
        full_path = fake_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    mod = _load_gate_module()
    # 替换 REPO_ROOT 为临时目录,使 check() 仅扫描临时 repo
    mod.REPO_ROOT = fake_root
    return mod.check()


# ════════════════════════════════════════════════════════════════
# 1. 干净代码
# ════════════════════════════════════════════════════════════════


class TestCleanCode:
    """干净代码(无违规调用)应 exit 0。"""

    def test_clean_code_exits_0(self, tmp_path):
        """无违规调用的代码 exit 0。"""
        files = {
            "bots/clean_bot.py": (
                "def handler():\n"
                "    return 'ok'\n"
            ),
            "services/normal_service.py": (
                "def process():\n"
                "    return data.fetch()\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 2. 违规检测(bots/ / admin/ / services/r40_scheduler.py)
# ════════════════════════════════════════════════════════════════


class TestViolationDetection:
    """在禁止位置检测到违规调用应 exit 1。"""

    def test_violation_in_bots(self, tmp_path):
        """bots/ 中有违规调用时 exit 1。"""
        files = {
            "bots/admin_bot/handlers.py": (
                "def handle_restore():\n"
                "    backup_engine.restore()\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "backup_engine" and v["method"] == "restore"
            for v in violations
        )

    def test_violation_in_admin(self, tmp_path):
        """admin/ 中有违规调用时 exit 1。"""
        files = {
            "admin/handlers.py": (
                "def assign():\n"
                "    rbac.assign_role(user_id, 'admin')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "rbac" and v["method"] == "assign_role"
            for v in violations
        )

    def test_violation_in_r40_scheduler(self, tmp_path):
        """services/r40_scheduler.py 中有违规调用时 exit 1。"""
        files = {
            "services/r40_scheduler.py": (
                "def scheduled_ban():\n"
                "    content_reports.ban_user(user_id)\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "content_reports" and v["method"] == "ban_user"
            for v in violations
        )


# ════════════════════════════════════════════════════════════════
# 3. 白名单允许的调用方
# ════════════════════════════════════════════════════════════════


class TestAllowedCallers:
    """白名单中的调用方允许直接调用高风险 API。"""

    def test_allows_approval_executor(self, tmp_path):
        """services/approval_executor.py 允许调用 backup_engine.restore。"""
        files = {
            "services/approval_executor.py": (
                "def execute():\n"
                "    backup_engine.restore()\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_command_bus(self, tmp_path):
        """services/command_bus.py 允许调用 rbac.assign_role。"""
        files = {
            "services/command_bus.py": (
                "def dispatch():\n"
                "    rbac.assign_role(user_id, 'admin')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_approval_workflow(self, tmp_path):
        """services/approval_workflow.py 允许调用 content_reports.takedown_file。"""
        files = {
            "services/approval_workflow.py": (
                "def workflow():\n"
                "    content_reports.takedown_file(file_id)\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_tests(self, tmp_path):
        """tests/ 目录允许调用高风险 API。"""
        files = {
            "tests/test_something.py": (
                "def test_restore():\n"
                "    backup_engine.restore(target='production')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_scripts(self, tmp_path):
        """scripts/ 目录允许调用高风险 API。"""
        files = {
            "scripts/run_migration.py": (
                "def migrate():\n"
                "    rbac.revoke_role(user_id, 'admin')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 4. 具体违规模式检测
# ════════════════════════════════════════════════════════════════


class TestSpecificPatterns:
    """检测具体的高风险 API 调用模式。"""

    def test_detects_backup_engine_restore_production(self, tmp_path):
        """检测 backup_engine.restore(target='production') 违规。"""
        files = {
            "bots/bad_bot.py": (
                "def handle():\n"
                "    backup_engine.restore(target='production')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "backup_engine" and v["method"] == "restore"
            for v in violations
        )

    def test_detects_content_reports_takedown_file(self, tmp_path):
        """检测 content_reports.takedown_file() 违规。"""
        files = {
            "bots/bad_bot.py": (
                "def handle():\n"
                "    content_reports.takedown_file('file123')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "content_reports" and v["method"] == "takedown_file"
            for v in violations
        )

    def test_detects_content_reports_ban_user(self, tmp_path):
        """检测 content_reports.ban_user() 违规。"""
        files = {
            "bots/bad_bot.py": (
                "def handle():\n"
                "    content_reports.ban_user(user_id)\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "content_reports" and v["method"] == "ban_user"
            for v in violations
        )

    def test_detects_rbac_assign_role(self, tmp_path):
        """检测 rbac.assign_role() 违规。"""
        files = {
            "bots/bad_bot.py": (
                "def handle():\n"
                "    rbac.assign_role(user_id, 'admin')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "rbac" and v["method"] == "assign_role"
            for v in violations
        )

    def test_detects_maintenance_mode_disable(self, tmp_path):
        """检测 maintenance_mode.disable() 违规(允许 disable_with_authorization)。"""
        files = {
            "bots/bad_bot.py": (
                "def handle():\n"
                "    maintenance_mode.disable()\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["module"] == "maintenance_mode" and v["method"] == "disable"
            for v in violations
        )


# ════════════════════════════════════════════════════════════════
# 5. maintenance_mode.disable_with_authorization 不被误报
# ════════════════════════════════════════════════════════════════


class TestMaintenanceModeWithAuthorization:
    """maintenance_mode.disable_with_authorization 不应被标记为违规。"""

    def test_disable_with_authorization_not_flagged(self, tmp_path):
        """disable_with_authorization 不等于 disable,不应被标记。"""
        files = {
            "bots/good_bot.py": (
                "def handle():\n"
                "    maintenance_mode.disable_with_authorization(auth_token)\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 6. AST 解析错误不误报
# ════════════════════════════════════════════════════════════════


class TestAstParseError:
    """AST 解析错误时不应误报(跳过该文件)。"""

    def test_ast_parse_error_no_false_positive(self, tmp_path):
        """语法错误的文件不应导致误报(跳过,exit 0)。"""
        files = {
            "bots/syntax_error.py": (
                "def broken(:\n"
                "    this is not valid python !!!\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []
