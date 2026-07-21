#!/usr/bin/env python3
"""R67 P1-08: scripts/ 目录跳过策略 — 区分 offline recovery tool 与测试脚本。

测试目标(对应 R67 P1-08 整改要求):
    1. `scripts/_script_categories.py` 的三类分类清单正确完整:
       - OFFLINE_RECOVERY_TOOLS(5 个 .sh 离线恢复/生产运维工具)
       - GATE_SCANNERS(47 个 .py CI 静态门禁扫描器/生成器)
       - GOVERNANCE_SCRIPTS(10 个 .sh 治理配置/验证脚本)

    2. `is_skippable_script()` 语义正确:
       - 仅 GATE_SCANNERS 返回 True(可跳过自扫描,避免自引用噪声)
       - OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 返回 False(必须被扫描)
       - 未分类的 scripts/ 文件返回 False(fail-closed,防止新运维脚本被默认跳过)
       - 非 scripts/ 文件返回 False(不在本函数处理范围)

    3. 7 个原"整体跳过 scripts/"的扫描器已迁移到细粒度判断:
       - check_button_nonce_coverage.py
       - check_commandbus_gate.py
       - check_mfa_verifier_gate.py
       - check_notification_legacy_send.py
       - check_restore_no_legacy_writer.py
       - check_sink_import_boundary.py
       - scan_hardcoded_strings.py

       每个扫描器必须:
       a. 不再在 ALLOWED_PREFIXES / WHITELIST_DIR_PREFIXES / SKIP_PATTERNS 中
          包含整目录前缀 "scripts/"
       b. 导入 `_is_skippable_script_p1_08`(fail-closed fallback: ImportError 时
          返回 False,即不跳过任何 scripts/ 文件)
       c. 在白名单/跳过判断函数中调用 `_is_skippable_script_p1_08(rel)`

    4. `get_required_scan_scripts()` 返回 OFFLINE_RECOVERY_TOOLS +
       GOVERNANCE_SCRIPTS(用于其他测试验证任何 scanner 的跳过清单不得
       包含这些文件)。

R67 P1-08 整改要点:
    tests/ 整体跳过合理(测试逃生舱),但 scripts/ 整体跳过不合理 —
    scripts/ 含真实运维入口(offline recovery tools)和治理脚本
    (governance scripts),触达生产数据/治理状态,必须与生产代码同等
    接受 capability/approval/MFA 审查。仅 CI 静态门禁扫描器自身可跳过
    自扫描(避免自引用噪声)。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# 项目根目录(tests/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts._script_categories import (
    GATE_SCANNERS,
    GOVERNANCE_SCRIPTS,
    OFFLINE_RECOVERY_TOOLS,
    _normalize_rel_path,
    get_required_scan_scripts,
    is_gate_scanner,
    is_governance_script,
    is_offline_recovery_tool,
    is_skippable_script,
)


# ════════════════════════════════════════════════════════════════
# 1. 分类清单内容正确性
# ════════════════════════════════════════════════════════════════


class TestCategoryContents:
    """验证三类分类清单的内容完整且正确。"""

    def test_offline_recovery_tools_exact_membership(self) -> None:
        """OFFLINE_RECOVERY_TOOLS 必须恰好包含这 5 个 .sh 文件。"""
        expected = frozenset({
            "scripts/full_machine_recovery.sh",
            "scripts/blank_vps_recovery_test.sh",
            "scripts/chaos_bot_fault_injection.sh",
            "scripts/ru_72h_verification.sh",
            "scripts/soak_test_7day.sh",
        })
        assert OFFLINE_RECOVERY_TOOLS == expected
        assert len(OFFLINE_RECOVERY_TOOLS) == 5

    def test_governance_scripts_exact_membership(self) -> None:
        """GOVERNANCE_SCRIPTS 必须恰好包含这 10 个 .sh 治理脚本。"""
        expected = frozenset({
            "scripts/configure_branch_protection.sh",
            "scripts/configure_branch_ruleset.sh",
            "scripts/configure_tag_ruleset.sh",
            "scripts/detect_branch_protection_contexts.sh",
            "scripts/verify_branch_protection.sh",
            "scripts/verify_branch_ruleset.sh",
            "scripts/verify_deps.sh",
            "scripts/verify_docker_digest.sh",
            "scripts/verify_git_source_governance.sh",
            "scripts/verify_tag_ruleset.sh",
        })
        assert GOVERNANCE_SCRIPTS == expected
        assert len(GOVERNANCE_SCRIPTS) == 10

    def test_gate_scanners_contains_self_and_key_scanners(self) -> None:
        """GATE_SCANNERS 必须包含本模块自身和关键扫描器。"""
        # 本模块自身(避免自引用噪声)
        assert "scripts/_script_categories.py" in GATE_SCANNERS
        # 7 个被 P1-08 整改的扫描器自身应在 GATE_SCANNERS 中
        for scanner in (
            "scripts/check_button_nonce_coverage.py",
            "scripts/check_commandbus_gate.py",
            "scripts/check_mfa_verifier_gate.py",
            "scripts/check_notification_legacy_send.py",
            "scripts/check_restore_no_legacy_writer.py",
            "scripts/check_sink_import_boundary.py",
            "scripts/scan_hardcoded_strings.py",
        ):
            assert scanner in GATE_SCANNERS, (
                f"{scanner} 应在 GATE_SCANNERS 中(避免自引用噪声)"
            )

    def test_gate_scanners_all_are_python_files(self) -> None:
        """GATE_SCANNERS 应全部是 .py 文件(扫描器是 Python 脚本)。"""
        for path in GATE_SCANNERS:
            assert path.endswith(".py"), (
                f"GATE_SCANNERS 应只含 .py 文件,发现: {path}"
            )
            assert path.startswith("scripts/"), (
                f"GATE_SCANNERS 路径应以 scripts/ 开头: {path}"
            )

    def test_offline_recovery_and_governance_all_are_shell_scripts(self) -> None:
        """OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 应全部是 .sh 文件。"""
        for path in OFFLINE_RECOVERY_TOOLS | GOVERNANCE_SCRIPTS:
            assert path.endswith(".sh"), (
                f"运维/治理脚本应为 .sh 文件,发现: {path}"
            )
            assert path.startswith("scripts/"), (
                f"路径应以 scripts/ 开头: {path}"
            )

    def test_categories_are_disjoint(self) -> None:
        """三类清单不得有交集(一个文件只能属于一类)。"""
        assert OFFLINE_RECOVERY_TOOLS.isdisjoint(GOVERNANCE_SCRIPTS)
        assert OFFLINE_RECOVERY_TOOLS.isdisjoint(GATE_SCANNERS)
        assert GOVERNANCE_SCRIPTS.isdisjoint(GATE_SCANNERS)

    def test_all_offline_recovery_tools_exist_on_disk(self) -> None:
        """OFFLINE_RECOVERY_TOOLS 中的文件必须实际存在于磁盘上。"""
        for rel in OFFLINE_RECOVERY_TOOLS:
            assert (REPO_ROOT / rel).is_file(), (
                f"OFFLINE_RECOVERY_TOOLS 引用的文件不存在: {rel}"
            )

    def test_all_governance_scripts_exist_on_disk(self) -> None:
        """GOVERNANCE_SCRIPTS 中的文件必须实际存在于磁盘上。"""
        for rel in GOVERNANCE_SCRIPTS:
            assert (REPO_ROOT / rel).is_file(), (
                f"GOVERNANCE_SCRIPTS 引用的文件不存在: {rel}"
            )

    def test_all_gate_scanners_exist_on_disk(self) -> None:
        """GATE_SCANNERS 中的文件必须实际存在于磁盘上。"""
        for rel in GATE_SCANNERS:
            assert (REPO_ROOT / rel).is_file(), (
                f"GATE_SCANNERS 引用的文件不存在: {rel}"
            )


# ════════════════════════════════════════════════════════════════
# 2. is_skippable_script() / 分类判定函数语义
# ════════════════════════════════════════════════════════════════


class TestIsSkippableScript:
    """验证 is_skippable_script() 的语义正确性。"""

    @pytest.mark.parametrize("rel_path", sorted(OFFLINE_RECOVERY_TOOLS))
    def test_offline_recovery_tools_not_skippable(self, rel_path: str) -> None:
        """OFFLINE_RECOVERY_TOOLS 不得被跳过(必须被扫描)。"""
        assert is_skippable_script(rel_path) is False
        assert is_offline_recovery_tool(rel_path) is True

    @pytest.mark.parametrize("rel_path", sorted(GOVERNANCE_SCRIPTS))
    def test_governance_scripts_not_skippable(self, rel_path: str) -> None:
        """GOVERNANCE_SCRIPTS 不得被跳过(必须被扫描)。"""
        assert is_skippable_script(rel_path) is False
        assert is_governance_script(rel_path) is True

    @pytest.mark.parametrize("rel_path", sorted(GATE_SCANNERS))
    def test_gate_scanners_are_skippable(self, rel_path: str) -> None:
        """GATE_SCANNERS 可被跳过(避免自引用噪声)。"""
        assert is_skippable_script(rel_path) is True
        assert is_gate_scanner(rel_path) is True

    def test_unclassified_scripts_not_skippable(self) -> None:
        """未分类的 scripts/ 文件必须 fail-closed(默认不跳过)。"""
        unclassified = (
            "scripts/some_new_recovery_tool.sh",
            "scripts/random_helper.py",
            "scripts/temp_test_script.py",
        )
        for path in unclassified:
            assert is_skippable_script(path) is False, (
                f"未分类的 scripts/ 文件应 fail-closed(不跳过): {path}"
            )

    def test_non_scripts_paths_not_skippable(self) -> None:
        """非 scripts/ 路径返回 False(不在本函数处理范围)。"""
        non_scripts = (
            "services/restore_orchestrator.py",
            "tests/test_r67_p1_08.py",
            "bots/idx_bot.py",
            "",
        )
        for path in non_scripts:
            assert is_skippable_script(path) is False, (
                f"非 scripts/ 路径应返回 False: {path}"
            )

    def test_path_normalization_handles_leading_dot(self) -> None:
        """路径标准化应处理前导 ./ 或 / 前缀。"""
        assert is_skippable_script("./scripts/scan_hardcoded_strings.py") is True
        assert is_skippable_script("./scripts/full_machine_recovery.sh") is False
        assert _normalize_rel_path("./scripts/foo.py") == "scripts/foo.py"
        assert _normalize_rel_path("/scripts/foo.py") == "scripts/foo.py"
        assert _normalize_rel_path("scripts/foo.py") == "scripts/foo.py"

    def test_get_required_scan_scripts_returns_offline_plus_governance(self) -> None:
        """get_required_scan_scripts() 必须返回 OFFLINE + GOVERNANCE 并集。"""
        expected = OFFLINE_RECOVERY_TOOLS | GOVERNANCE_SCRIPTS
        assert get_required_scan_scripts() == expected
        assert isinstance(get_required_scan_scripts(), frozenset)


# ════════════════════════════════════════════════════════════════
# 3. 7 个扫描器迁移验证
# ════════════════════════════════════════════════════════════════


# R67 P1-08: 7 个原"整体跳过 scripts/"的扫描器
# 每个扫描器迁移后必须满足两个条件:
#   (a) 源码中不再包含整目录前缀 "scripts/"(在 ALLOWED_PREFIXES /
#       WHITELIST_DIR_PREFIXES / SKIP_PATTERNS 中)
#   (b) 源码中导入了 `_is_skippable_script_p1_08`(fail-closed fallback)
SCANNERS_TO_VERIFY: tuple[str, ...] = (
    "scripts.check_button_nonce_coverage",
    "scripts.check_commandbus_gate",
    "scripts.check_mfa_verifier_gate",
    "scripts.check_notification_legacy_send",
    "scripts.check_restore_no_legacy_writer",
    "scripts.check_sink_import_boundary",
    "scripts.scan_hardcoded_strings",
)


def _scanner_source(scanner_module: str) -> str:
    """读取扫描器模块源码。"""
    path = REPO_ROOT / (scanner_module.replace(".", "/") + ".py")
    return path.read_text(encoding="utf-8")


class TestScannerMigration:
    """验证 7 个扫描器已迁移到细粒度判断。"""

    @pytest.mark.parametrize("scanner_module", SCANNERS_TO_VERIFY)
    def test_no_scripts_prefix_in_allowlist(self, scanner_module: str) -> None:
        """扫描器源码中不应再出现整目录跳过 'scripts/'。

        检测策略:扫描源码中所有形如 "scripts/" 的字符串字面量,
        确保它们是注释或描述性文本(如 "scripts/ 细粒度判断"),
        而非 ALLOWED_PREFIXES / WHITELIST_DIR_PREFIXES / SKIP_PATTERNS
        列表中的元素。

        通过重新加载模块并检查具体的白名单常量,而非全文 grep。
        """
        mod = importlib.import_module(scanner_module)
        # 收集模块中所有 list[str] 类型的白名单常量
        allowlist_names = (
            "ALLOWED_PREFIXES",
            "WHITELIST_DIR_PREFIXES",
            "SKIP_PATTERNS",
        )
        for name in allowlist_names:
            if not hasattr(mod, name):
                continue
            value = getattr(mod, name)
            if not isinstance(value, (list, tuple, set, frozenset)):
                continue
            for item in value:
                assert item != "scripts/", (
                    f"{scanner_module}.{name} 仍包含整目录前缀 'scripts/' "
                    f"— 必须改为细粒度 is_skippable_script() 判断"
                )

    @pytest.mark.parametrize("scanner_module", SCANNERS_TO_VERIFY)
    def test_imports_is_skippable_script_p1_08(
        self, scanner_module: str
    ) -> None:
        """扫描器必须导入 `_is_skippable_script_p1_08`(fail-closed fallback)。"""
        source = _scanner_source(scanner_module)
        # 必须有 import 语句
        assert "is_skippable_script as _is_skippable_script_p1_08" in source, (
            f"{scanner_module} 必须导入 is_skippable_script(别名 "
            f"_is_skippable_script_p1_08)"
        )
        # 必须有 fail-closed fallback(ImportError 时定义返回 False 的函数)
        assert "return False" in source, (
            f"{scanner_module} 必须包含 fail-closed fallback "
            f"(ImportError 时返回 False,不跳过任何 scripts/ 文件)"
        )

    @pytest.mark.parametrize("scanner_module", SCANNERS_TO_VERIFY)
    def test_calls_is_skippable_script_in_is_allowed(
        self, scanner_module: str
    ) -> None:
        """扫描器的白名单判断函数中必须调用 _is_skippable_script_p1_08。

        各扫描器的判断函数名可能不同(_is_allowed / _is_whitelisted /
        is_skipped),通过源码扫描确认 _is_skippable_script_p1_08 被实际
        调用(不仅仅是导入)。
        """
        source = _scanner_source(scanner_module)
        # 必须有调用(函数名后跟 ()
        assert "_is_skippable_script_p1_08(" in source, (
            f"{scanner_module} 必须在白名单判断函数中调用 "
            f"_is_skippable_script_p1_08(rel_path)"
        )

    @pytest.mark.parametrize("scanner_module", SCANNERS_TO_VERIFY)
    def test_scanner_itself_is_in_gate_scanners(
        self, scanner_module: str
    ) -> None:
        """扫描器自身路径必须在 GATE_SCANNERS 中(否则会被自身扫描)。"""
        rel_path = scanner_module.replace(".", "/") + ".py"
        assert rel_path in GATE_SCANNERS, (
            f"{rel_path} 必须在 GATE_SCANNERS 中,否则会被自身扫描产生噪声"
        )


# ════════════════════════════════════════════════════════════════
# 4. 运行时行为验证 — 端到端
# ════════════════════════════════════════════════════════════════


class TestScannerRuntimeBehavior:
    """验证扫描器在运行时正确应用细粒度判断。"""

    def test_offline_recovery_tool_not_allowed_by_commandbus_gate(self) -> None:
        """check_commandbus_gate 不得允许 OFFLINE_RECOVERY_TOOLS 跳过。

        模拟:对 OFFLINE_RECOVERY_TOOLS 中的 .sh 文件调用 _is_allowed,
        应返回 False(必须被扫描)。
        注意:.sh 文件不是 Python,实际不会被 AST 扫描,但 _is_allowed
        必须不返回 True(否则若未来加入 .py 运维脚本会被默认跳过)。
        """
        from scripts.check_commandbus_gate import _is_allowed

        for rel in OFFLINE_RECOVERY_TOOLS:
            path = REPO_ROOT / rel
            # .sh 文件实际不会被扫描(SCAN_DIRS 限制),但 _is_allowed 应返回 False
            # 验证 fail-closed:即使路径不在白名单中,_is_allowed 也返回 False
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 OFFLINE_RECOVERY_TOOLS 返回 True: {rel}"
            )

    def test_gate_scanner_allowed_by_commandbus_gate(self) -> None:
        """check_commandbus_gate 应允许 GATE_SCANNERS 跳过(避免自引用噪声)。"""
        from scripts.check_commandbus_gate import _is_allowed

        # 选取一个 GATE_SCANNERS 中的 .py 文件验证
        sample_gate = "scripts/check_error_protocol.py"
        assert sample_gate in GATE_SCANNERS
        path = REPO_ROOT / sample_gate
        assert _is_allowed(path) is True, (
            f"GATE_SCANNERS 应可跳过自扫描: {sample_gate}"
        )

    def test_offline_recovery_tool_not_allowed_by_sink_boundary(self) -> None:
        """check_sink_import_boundary 不得允许 OFFLINE_RECOVERY_TOOLS 跳过。"""
        from scripts.check_sink_import_boundary import _is_allowed

        for rel in OFFLINE_RECOVERY_TOOLS:
            path = REPO_ROOT / rel
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 OFFLINE_RECOVERY_TOOLS 返回 True: {rel}"
            )

    def test_offline_recovery_tool_not_allowed_by_notification_legacy(self) -> None:
        """check_notification_legacy_send 不得允许 OFFLINE_RECOVERY_TOOLS 跳过。"""
        from scripts.check_notification_legacy_send import _is_allowed

        for rel in OFFLINE_RECOVERY_TOOLS:
            path = REPO_ROOT / rel
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 OFFLINE_RECOVERY_TOOLS 返回 True: {rel}"
            )

    def test_governance_scripts_not_allowed_by_commandbus_gate(self) -> None:
        """check_commandbus_gate 不得允许 GOVERNANCE_SCRIPTS 跳过。"""
        from scripts.check_commandbus_gate import _is_allowed

        for rel in GOVERNANCE_SCRIPTS:
            path = REPO_ROOT / rel
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 GOVERNANCE_SCRIPTS 返回 True: {rel}"
            )

    def test_governance_scripts_not_allowed_by_sink_boundary(self) -> None:
        """check_sink_import_boundary 不得允许 GOVERNANCE_SCRIPTS 跳过。"""
        from scripts.check_sink_import_boundary import _is_allowed

        for rel in GOVERNANCE_SCRIPTS:
            path = REPO_ROOT / rel
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 GOVERNANCE_SCRIPTS 返回 True: {rel}"
            )

    def test_governance_scripts_not_allowed_by_notification_legacy(self) -> None:
        """check_notification_legacy_send 不得允许 GOVERNANCE_SCRIPTS 跳过。"""
        from scripts.check_notification_legacy_send import _is_allowed

        for rel in GOVERNANCE_SCRIPTS:
            path = REPO_ROOT / rel
            assert _is_allowed(path) is False, (
                f"_is_allowed 不应对 GOVERNANCE_SCRIPTS 返回 True: {rel}"
            )


# ════════════════════════════════════════════════════════════════
# 5. 端到端:扫描器退出码 = 0(无违规)
# ════════════════════════════════════════════════════════════════


class TestScannerExitCodes:
    """验证 7 个扫描器在当前代码库上运行后退出码为 0(无违规)。

    P1-08 整改不应引入新的违规 — 整改只是收紧白名单(从"整体跳过 scripts/"
    改为"细粒度判断"),实际被扫描的 OFFLINE_RECOVERY_TOOLS 是 .sh 文件,
    Python AST 扫描器不会扫描 .sh 文件,因此不会引入新违规。
    """

    @pytest.mark.parametrize(
        "scanner_module,expected_exit_code",
        [
            ("scripts.check_button_nonce_coverage", 0),
            ("scripts.check_commandbus_gate", 0),
            ("scripts.check_mfa_verifier_gate", 0),
            ("scripts.check_notification_legacy_send", 0),
            ("scripts.check_restore_no_legacy_writer", 0),
            ("scripts.check_sink_import_boundary", 0),
            # scan_hardcoded_strings 通过 --check 子命令验证(单独测试)
        ],
    )
    def test_scanner_exits_zero(
        self,
        scanner_module: str,
        expected_exit_code: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """每个扫描器的 check() 函数应返回退出码 0。"""
        mod = importlib.import_module(scanner_module)
        # 大多数扫描器提供 check() 函数,返回 (exit_code, ...)
        if not hasattr(mod, "check"):
            pytest.skip(f"{scanner_module} 无 check() 函数")
        # 捕获 stdout(扫描器会打印进度)
        import io
        import contextlib

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = mod.check()
        # check() 返回 (exit_code, ...) 或 exit_code
        if isinstance(result, tuple):
            exit_code = result[0]
        else:
            exit_code = result
        assert exit_code == expected_exit_code, (
            f"{scanner_module}.check() 返回退出码 {exit_code},"
            f"期望 {expected_exit_code}。\n"
            f"stdout: {stdout.getvalue()[:500]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
