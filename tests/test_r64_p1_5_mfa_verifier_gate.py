"""R64 P1-05: MFA sync verifier 调用门禁测试。

测试范围:
- scripts/check_mfa_verifier_gate.py: AST 静态门禁脚本
  * Rule 1 (import 违规):检测 `from admin.mfa import verify_mfa_receipt`
  * Rule 2 (call 违规):检测 `verify_mfa_receipt(...)` 与
    `obj.verify_mfa_receipt(...)` 调用
  * `verify_mfa_receipt_authoritative(...)` 不被误报
  * `admin/mfa.py` 自身不被误报(定义文件)
  * tests/ 与 scripts/ 目录不被误报(白名单)
  * 当前真实代码库通过门禁(exit 0)

测试策略:
- 创建临时 repo 结构,monkeypatch REPO_ROOT
- 在临时 repo 中放置违规 / 干净 .py 文件
- 调用 check() 验证 exit_code 与 violations 列表
- 单独用例直接对真实代码库运行(无 monkeypatch)验证当前已合规
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── 辅助函数 ──────────────────────────────────────────


def _load_gate_module():
    """通过 importlib 加载 check_mfa_verifier_gate.py 为独立模块实例。

    每次返回全新的模块对象,避免跨用例状态污染。
    """
    spec = importlib.util.spec_from_file_location(
        "_check_mfa_verifier_gate_r64_test",
        SCRIPTS_DIR / "check_mfa_verifier_gate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_check(tmp_path, files):
    """创建临时 repo 结构并运行 check_mfa_verifier_gate.check()。

    Args:
        tmp_path: pytest tmp_path fixture(Path)
        files: dict, {相对路径: 文件内容}
            如 {"bots/bad.py": "from admin.mfa import verify_mfa_receipt\\n"}

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, rule, detail}, ...]
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
        """无 sync verifier 调用的代码 exit 0。"""
        files = {
            "bots/clean_bot.py": (
                "def handler():\n"
                "    return 'ok'\n"
            ),
            "services/normal_service.py": (
                "def process():\n"
                "    return data.fetch()\n"
            ),
            "admin/some_admin.py": (
                "def view():\n"
                "    return None\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 2. Rule 1: import 违规检测
# ════════════════════════════════════════════════════════════════


class TestRule1ImportViolation:
    """Rule 1: 检测 `from admin.mfa import verify_mfa_receipt`。"""

    def test_detects_import_in_bots(self, tmp_path):
        """bots/ 中 `from admin.mfa import verify_mfa_receipt` 违规。"""
        files = {
            "bots/admin_bot/handlers.py": (
                "from admin.mfa import verify_mfa_receipt\n"
                "def handle(token):\n"
                "    return verify_mfa_receipt(token, 1, 'x', 'y')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 1") and v["file"] == "bots/admin_bot/handlers.py"
            for v in violations
        )

    def test_detects_import_in_services(self, tmp_path):
        """services/ 中违规 import 同样被检测。"""
        files = {
            "services/data_lifecycle.py": (
                "from admin.mfa import verify_mfa_receipt\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 1") for v in violations
        )

    def test_detects_import_in_admin_non_mfa(self, tmp_path):
        """admin/ 中除 mfa.py 外的文件违规 import 被检测。"""
        files = {
            "admin/handlers.py": (
                "from admin.mfa import verify_mfa_receipt\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 1") and v["file"] == "admin/handlers.py"
            for v in violations
        )

    def test_import_with_alias_detected(self, tmp_path):
        """`from admin.mfa import verify_mfa_receipt as vr` 仍违规。"""
        files = {
            "services/bad.py": (
                "from admin.mfa import verify_mfa_receipt as vr\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(v["rule"].startswith("Rule 1") for v in violations)

    def test_import_with_other_names_still_violation(self, tmp_path):
        """同时导入 verify_mfa_receipt 与其他名字仍违规。"""
        files = {
            "services/bad.py": (
                "from admin.mfa import verify_mfa_receipt, get_mfa_manager\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(v["rule"].startswith("Rule 1") for v in violations)


# ════════════════════════════════════════════════════════════════
# 3. Rule 2: call 违规检测
# ════════════════════════════════════════════════════════════════


class TestRule2CallViolation:
    """Rule 2: 检测 `verify_mfa_receipt(...)` 调用。"""

    def test_detects_direct_call_in_bots(self, tmp_path):
        """bots/ 中 `verify_mfa_receipt(token, ...)` 直接调用违规。"""
        files = {
            "bots/admin_bot/handlers.py": (
                "def handle(token):\n"
                "    return verify_mfa_receipt(token, 1, 'x', 'y')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 2") and v["file"] == "bots/admin_bot/handlers.py"
            for v in violations
        )

    def test_detects_attribute_call_in_services(self, tmp_path):
        """services/ 中 `manager.verify_mfa_receipt(...)` 属性调用违规。"""
        files = {
            "services/data_lifecycle.py": (
                "def handle(manager, token):\n"
                "    return manager.verify_mfa_receipt(token, 1, 'x', 'y')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 2") and "属性调用" in v["detail"]
            for v in violations
        )

    def test_detects_call_in_admin_non_mfa(self, tmp_path):
        """admin/ 中除 mfa.py 外的文件违规 call 被检测。"""
        files = {
            "admin/handlers.py": (
                "def handle(token):\n"
                "    return verify_mfa_receipt(token, 1, 'x', 'y')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 1
        assert any(
            v["rule"].startswith("Rule 2") and v["file"] == "admin/handlers.py"
            for v in violations
        )


# ════════════════════════════════════════════════════════════════
# 4. verify_mfa_receipt_authoritative 不被误报
# ════════════════════════════════════════════════════════════════


class TestAuthoritativeNotFlagged:
    """`verify_mfa_receipt_authoritative` 合规,不应被误报。"""

    def test_authoritative_import_not_flagged(self, tmp_path):
        """`from admin.mfa import verify_mfa_receipt_authoritative` 合规。"""
        files = {
            "services/data_lifecycle.py": (
                "from admin.mfa import verify_mfa_receipt_authoritative\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_authoritative_call_not_flagged(self, tmp_path):
        """`verify_mfa_receipt_authoritative(...)` 调用合规。"""
        files = {
            "services/data_lifecycle.py": (
                "from admin.mfa import verify_mfa_receipt_authoritative\n"
                "async def handle(token):\n"
                "    return await verify_mfa_receipt_authoritative(\n"
                "        token, 1, 'x', 'y', consume=False\n"
                "    )\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_authoritative_attribute_call_not_flagged(self, tmp_path):
        """`manager.verify_mfa_receipt_authoritative(...)` 属性调用合规。"""
        files = {
            "services/data_lifecycle.py": (
                "async def handle(manager, token):\n"
                "    return await manager.verify_mfa_receipt_authoritative(\n"
                "        token, 1, 'x', 'y'\n"
                "    )\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_authoritative_imported_alongside_sync_still_flags_sync(self, tmp_path):
        """同时导入 sync 与 authoritative 仍报 sync 违规。"""
        files = {
            "services/bad.py": (
                "from admin.mfa import (\n"
                "    verify_mfa_receipt,\n"
                "    verify_mfa_receipt_authoritative,\n"
                ")\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        # 仅 sync 的 import 被报为违规,authoritative 不被误报
        assert exit_code == 1
        rule1 = [v for v in violations if v["rule"].startswith("Rule 1")]
        assert len(rule1) == 1
        assert rule1[0]["detail"] == "from admin.mfa import verify_mfa_receipt"


# ════════════════════════════════════════════════════════════════
# 5. 白名单允许的调用方
# ════════════════════════════════════════════════════════════════


class TestAllowedCallers:
    """白名单中的文件允许调用 sync verifier。"""

    def test_allows_admin_mfa_self(self, tmp_path):
        """admin/mfa.py 自身允许(定义文件,内部可调用)。"""
        files = {
            "admin/mfa.py": (
                "def verify_mfa_receipt(token):\n"
                "    return verify_mfa_receipt(token, 1, 'x', 'y')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_tests(self, tmp_path):
        """tests/ 目录允许调用 sync verifier。"""
        files = {
            "tests/test_mfa.py": (
                "from admin.mfa import verify_mfa_receipt\n"
                "def test_x():\n"
                "    return verify_mfa_receipt('t', 1, 'p', 'a')\n"
            ),
        }
        exit_code, violations = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_allows_scripts(self, tmp_path):
        """scripts/ 目录允许调用 sync verifier。"""
        files = {
            "scripts/run_mfa_check.py": (
                "from admin.mfa import verify_mfa_receipt\n"
                "def run(token):\n"
                "    return verify_mfa_receipt(token, 1, 'x', 'y')\n"
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


# ════════════════════════════════════════════════════════════════
# 7. 真实代码库门禁通过(R64 P1-05 整改验收)
# ════════════════════════════════════════════════════════════════


class TestRealCodebaseCompliant:
    """当前真实代码库应通过门禁(已迁移到 async 权威版本)。"""

    def test_real_codebase_passes_gate(self):
        """对真实代码库运行 check(),应 exit 0 无违规。

        不 monkeypatch REPO_ROOT,直接扫描 /workspace 下的
        bots/ services/ admin/(排除 admin/mfa.py)。
        """
        mod = _load_gate_module()
        # 不替换 REPO_ROOT — 扫描真实仓库
        exit_code, violations = mod.check()
        assert exit_code == 0, (
            f"真实代码库存在 sync verify_mfa_receipt 违规, "
            f"应已迁移到 verify_mfa_receipt_authoritative: {violations}"
        )
        assert violations == []

    def test_real_codebase_script_main_exits_0(self, capsys):
        """直接调用脚本入口 main()(真实代码库),exit 0。"""
        mod = _load_gate_module()
        # 不替换 REPO_ROOT — 扫描真实仓库
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "[OK] MFA verifier gate 通过" in captured.out
