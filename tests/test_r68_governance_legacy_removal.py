"""R68 终审整改回归测试.

覆盖 R68 P0:
    - P0-02: PR lenient mode 已删除(verify_branch_ruleset.sh / verify_branch_protection.sh)
    - P0-04: CODEOWNERS 存在且覆盖关键治理路径
    - P0-05: verify_git_source_governance.sh 以 GitHub API 为权威源(本地缺 GPG 公钥不 fail)
    - P0-07: Dockerfile 使用 allowlist COPY(不再 COPY . .)
    - P0-07: services/db_restore.py 被 .dockerignore 排除
    - P0-07: Dockerfile 物理排除 legacy restore CLI

R68 审查基线:
    master HEAD: da97baac63868c0f2e3105699ecb992e6c3e4301
    Release Gates: failure (verify-branch-protection / verify-branch-ruleset / verify-git-source-governance)
    裁定: DEVELOPMENT PASS / STAGING HOLD / PRODUCTION NO-GO
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# P0-02: PR lenient mode 已删除
# ════════════════════════════════════════════════════════════════

class TestR68P0_02NoPrLenientMode:
    """R68 P0-02: 删除治理类 PR lenient success."""

    def test_verify_branch_ruleset_no_pr_lenient(self):
        """verify_branch_ruleset.sh 不包含 PR lenient mode (WARN + exit 0)."""
        script = REPO_ROOT / "scripts" / "verify_branch_ruleset.sh"
        content = script.read_text(encoding="utf-8")

        # 不应包含 PR lenient mode 的标志性输出
        assert "PR lenient mode" not in content, (
            "verify_branch_ruleset.sh 仍包含 PR lenient mode — 违反 R68 P0-02"
        )
        assert "PASS (PR lenient mode)" not in content, (
            "verify_branch_ruleset.sh 仍包含 PR lenient pass 输出 — 违反 R68 P0-02"
        )

        # 不应在 pull_request 事件下 exit 0 绕过 ruleset 缺失
        assert not re.search(
            r'GITHUB_EVENT_NAME.*pull_request.*\n.*exit\s+0',
            content,
            re.DOTALL,
        ), "verify_branch_ruleset.sh 在 PR 事件下 exit 0 绕过 — 违反 R68 P0-02"

    def test_verify_branch_protection_no_pr_lenient(self):
        """verify_branch_protection.sh 不包含 PR lenient mode."""
        script = REPO_ROOT / "scripts" / "verify_branch_protection.sh"
        content = script.read_text(encoding="utf-8")

        assert "PR lenient mode" not in content, (
            "verify_branch_protection.sh 仍包含 PR lenient mode — 违反 R68 P0-02"
        )
        assert "PASS (PR lenient mode)" not in content, (
            "verify_branch_protection.sh 仍包含 PR lenient pass 输出 — 违反 R68 P0-02"
        )

    def test_verify_branch_ruleset_fail_closed_on_missing(self):
        """ruleset 不存在时必须 exit 1(fail-closed),无论 PR 还是 push."""
        script = REPO_ROOT / "scripts" / "verify_branch_ruleset.sh"
        content = script.read_text(encoding="utf-8")

        # 找到 "未找到 branch ruleset" 的处理块
        # 必须包含 exit 1,不能包含 exit 0
        missing_block_match = re.search(
            r'未找到 branch ruleset.*?(?=\necho|\Z)',
            content,
            re.DOTALL,
        )
        assert missing_block_match, "未找到 ruleset 缺失处理逻辑"
        missing_block = missing_block_match.group(0)
        assert "exit 1" in missing_block, (
            "ruleset 缺失时必须 exit 1(fail-closed)— 违反 R68 P0-02"
        )


# ════════════════════════════════════════════════════════════════
# P0-04: CODEOWNERS 存在且覆盖关键路径
# ════════════════════════════════════════════════════════════════

class TestR68P0_04Codeowners:
    """R68 P0-04: CODEOWNERS 配置独立 reviewer."""

    @pytest.fixture
    def codeowners_content(self):
        codeowners = REPO_ROOT / ".github" / "CODEOWNERS"
        assert codeowners.exists(), ".github/CODEOWNERS 不存在 — 违反 R68 P0-04"
        return codeowners.read_text(encoding="utf-8")

    def test_codeowners_exists(self, codeowners_content):
        """CODEOWNERS 文件存在."""
        assert codeowners_content, "CODEOWNERS 为空"

    def test_codeowners_covers_governance_scripts(self, codeowners_content):
        """CODEOWNERS 覆盖治理脚本."""
        required_paths = [
            "verify_branch_ruleset.sh",
            "verify_branch_protection.sh",
            "verify_git_source_governance.sh",
            "configure_branch_ruleset.sh",
            "configure_branch_protection.sh",
        ]
        for path in required_paths:
            assert path in codeowners_content, (
                f"CODEOWNERS 未覆盖治理脚本 {path} — 违反 R68 P0-04"
            )

    def test_codeowners_covers_dockerfile(self, codeowners_content):
        """CODEOWNERS 覆盖 Dockerfile."""
        assert "Dockerfile" in codeowners_content, (
            "CODEOWNERS 未覆盖 Dockerfile — 违反 R68 P0-04"
        )

    def test_codeowners_covers_restore_files(self, codeowners_content):
        """CODEOWNERS 覆盖 restore/backup/production guard."""
        required = [
            "db_restore.py",
            "db_backup.py",
            "backup_dr_validate.py",
            "_production_guard.py",
        ]
        for path in required:
            assert path in codeowners_content, (
                f"CODEOWNERS 未覆盖 restore 文件 {path} — 违反 R68 P0-04"
            )

    def test_codeowners_covers_scanner(self, codeowners_content):
        """CODEOWNERS 覆盖 scanner allowlist."""
        assert "check_restore_no_legacy_writer.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 scanner allowlist — 违反 R68 P0-04"
        )

    def test_codeowners_covers_workflows(self, codeowners_content):
        """CODEOWNERS 覆盖 GitHub Actions workflows."""
        assert ".github/workflows/" in codeowners_content, (
            "CODEOWNERS 未覆盖 .github/workflows/ — 违反 R68 P0-04"
        )


# ════════════════════════════════════════════════════════════════
# P0-05: verify_git_source_governance.sh 以 GitHub API 为权威源
# ════════════════════════════════════════════════════════════════

class TestR68P0_05GitSourceGovernance:
    """R68 P0-05: Git source governance 以 GitHub API 为权威源."""

    def test_no_hard_fail_on_missing_gpg_key(self):
        """本地缺 GPG 公钥(U/X)不应硬失败,应以 GitHub API 验证为准."""
        script = REPO_ROOT / "scripts" / "verify_git_source_governance.sh"
        content = script.read_text(encoding="utf-8")

        # U/X 状态不应直接 fail
        # 检查 U 和 X case 不包含 fail 调用
        u_case = re.search(r'U\)\s+.*?(?=Y\))', content, re.DOTALL)
        if u_case:
            assert "fail" not in u_case.group(0).lower(), (
                "U(unknown validity)状态不应 fail — GitHub API 是权威源(R68 P0-05)"
            )

        x_case = re.search(r'X\)\s+.*?(?=\n\s+[A-Z]\))', content, re.DOTALL)
        if x_case:
            assert "fail" not in x_case.group(0).lower(), (
                "X(unsigned)状态不应 fail — GitHub API 是权威源(R68 P0-05)"
            )

    def test_github_api_verification_is_authoritative(self):
        """GitHub API verification.verified=true 即视为签名有效."""
        script = REPO_ROOT / "scripts" / "verify_git_source_governance.sh"
        content = script.read_text(encoding="utf-8")

        # GitHub API verified=true 时不应 fail
        assert "GitHub API verification.verified=true" in content, (
            "脚本应输出 GitHub API 验证成功 — R68 P0-05"
        )
        # GitHub API verified=false 时应 fail
        assert re.search(
            r'verified.*false.*\n.*fail',
            content,
            re.IGNORECASE | re.DOTALL,
        ), "GitHub API verified=false 必须 fail — R68 P0-05"

    def test_b_r_e_states_still_hard_fail(self):
        """B(bad)/R(revoked)/E(expired)签名状态仍必须硬失败."""
        script = REPO_ROOT / "scripts" / "verify_git_source_governance.sh"
        content = script.read_text(encoding="utf-8")

        for state in ["B", "R", "E"]:
            pattern = rf'{state}\)\s+fail\s+"'
            assert re.search(pattern, content), (
                f"签名状态 {state} 必须 fail — 不应被软化(R68 P0-05)"
            )


# ════════════════════════════════════════════════════════════════
# P0-07: Dockerfile allowlist COPY + legacy 物理排除
# ════════════════════════════════════════════════════════════════

class TestR68P0_07DockerfileAllowlist:
    """R68 P0-07: Dockerfile 使用显式 allowlist COPY."""

    @pytest.fixture
    def dockerfile_content(self):
        dockerfile = REPO_ROOT / "Dockerfile"
        assert dockerfile.exists(), "Dockerfile 不存在"
        return dockerfile.read_text(encoding="utf-8")

    def test_no_copy_dot_dot(self, dockerfile_content):
        """Dockerfile 不再使用 COPY . .(必须 allowlist) — 只检查实际指令行,不检查注释."""
        # 只检查以 COPY 开头的实际指令行(忽略注释)
        copy_lines = [
            line.strip() for line in dockerfile_content.splitlines()
            if line.strip().startswith("COPY") and not line.strip().startswith("#")
        ]
        for line in copy_lines:
            # 允许 COPY --from=builder(多阶段构建),不允许 COPY . .
            assert not re.match(r'^COPY\s+\.\s+\.\s*$', line), (
                f"Dockerfile 仍使用 COPY . . — 违反 R68 P0-07(必须 allowlist COPY): {line}"
            )

    def test_explicit_allowlist_copy(self, dockerfile_content):
        """Dockerfile 使用显式目录 COPY."""
        required_copies = [
            "COPY run_all.py",
            "COPY services/",
            "COPY bots/",
            "COPY admin/",
            "COPY config/",
            "COPY database/",
            "COPY locales/",
            "COPY utils/",
            "COPY storage/",
        ]
        for copy_stmt in required_copies:
            assert copy_stmt in dockerfile_content, (
                f"Dockerfile 缺少 allowlist COPY: {copy_stmt} — 违反 R68 P0-07"
            )

    def test_r68_p0_07_comment_present(self, dockerfile_content):
        """Dockerfile 包含 R68 P0-07 整改注释."""
        assert "R68 P0-07" in dockerfile_content, (
            "Dockerfile 缺少 R68 P0-07 整改注释"
        )


class TestR68P0_07DockerignoreExcludesLegacy:
    """R68 P0-07: .dockerignore 排除 legacy restore CLI."""

    @pytest.fixture
    def dockerignore_content(self):
        dockerignore = REPO_ROOT / ".dockerignore"
        assert dockerignore.exists(), ".dockerignore 不存在"
        return dockerignore.read_text(encoding="utf-8")

    def test_db_restore_excluded(self, dockerignore_content):
        """services/db_restore.py 被 .dockerignore 排除."""
        assert "services/db_restore.py" in dockerignore_content, (
            ".dockerignore 未排除 services/db_restore.py — 违反 R68 P0-07"
        )

    def test_r68_p0_07_comment_present(self, dockerignore_content):
        """dockerignore 包含 R68 P0-07 注释."""
        assert "R68 P0-07" in dockerignore_content, (
            ".dockerignore 缺少 R68 P0-07 注释"
        )

    def test_scripts_excluded(self, dockerignore_content):
        """scripts/ 被排除(legacy CLI 入口)."""
        assert "scripts/" in dockerignore_content, (
            ".dockerignore 未排除 scripts/ — 违反 R67 P0-06"
        )

    def test_tests_excluded(self, dockerignore_content):
        """tests/ 被排除(测试代码不进入生产镜像)."""
        assert "tests/" in dockerignore_content, (
            ".dockerignore 未排除 tests/ — 违反 R67 P0-06"
        )


class TestR68P0_07LegacyRestoreNotInRuntime:
    """R68 P0-07: legacy restore CLI 未被运行时引用."""

    def test_run_all_does_not_import_db_restore(self):
        """run_all.py 不导入 services.db_restore."""
        run_all = REPO_ROOT / "run_all.py"
        content = run_all.read_text(encoding="utf-8")

        # 不应存在模块级 import services.db_restore
        assert not re.search(
            r'^(from\s+services\.db_restore|import\s+services\.db_restore)',
            content,
            re.MULTILINE,
        ), "run_all.py 不应在模块级导入 services.db_restore — R68 P0-07"

    def test_backup_dr_validate_uses_delayed_import(self):
        """backup_dr_validate.py 对 db_restore 使用延迟导入(函数级)."""
        bdr = REPO_ROOT / "services" / "backup_dr_validate.py"
        content = bdr.read_text(encoding="utf-8")

        # 找到所有 from services.db_restore import
        imports = re.findall(
            r'^(\s*)(from\s+services\.db_restore\s+import)',
            content,
            re.MULTILINE,
        )
        for indent, _ in imports:
            # 延迟导入有缩进(函数级),模块级导入无缩进或与 def 同级
            assert indent, (
                "backup_dr_validate.py 对 db_restore 必须使用延迟导入(函数级)— R68 P0-07"
            )


# ════════════════════════════════════════════════════════════════
# P0-09: Compose 安全属性 hard fail(验证现有状态)
# ════════════════════════════════════════════════════════════════

class TestR68P0_09ComposeSecurityHardFail:
    """R68 P0-09: Compose 安全属性必须 hard fail(不是 WARN-only)."""

    def test_compose_smoke_script_is_hard_fail(self):
        """check_compose_runtime_smoke.py 是 hard fail(exit 1 on violation)."""
        script = REPO_ROOT / "scripts" / "check_compose_runtime_smoke.py"
        content = script.read_text(encoding="utf-8")

        # 不应包含 WARN-only 语义
        assert "WARN-only" not in content, (
            "check_compose_runtime_smoke.py 不应是 WARN-only — R68 P0-09"
        )

    def test_compose_has_security_constraints(self):
        """docker-compose.yml 所有服务有安全约束."""
        import yaml
        compose = REPO_ROOT / "docker-compose.yml"
        with open(compose, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        services = data.get("services", {})
        assert len(services) > 0, "docker-compose.yml 无服务定义"

        for svc_name, svc_config in services.items():
            # cap_drop 必须包含 ALL
            cap_drop = svc_config.get("cap_drop", [])
            if isinstance(cap_drop, str):
                cap_drop = [cap_drop]
            assert "ALL" in cap_drop, (
                f"服务 {svc_name} 缺少 cap_drop: ALL — R68 P0-09"
            )

            # security_opt 必须包含 no-new-privileges:true
            security_opt = svc_config.get("security_opt", [])
            assert any("no-new-privileges" in s for s in security_opt), (
                f"服务 {svc_name} 缺少 no-new-privileges — R68 P0-09"
            )

            # read_only 必须为 true
            assert svc_config.get("read_only") is True, (
                f"服务 {svc_name} 缺少 read_only: true — R68 P0-09"
            )


# ════════════════════════════════════════════════════════════════
# 治理配置脚本存在性验证(管理员操作前置条件)
# ════════════════════════════════════════════════════════════════

class TestR68GovernanceConfigScripts:
    """R68: 治理配置脚本存在且幂等(供管理员执行)."""

    def test_configure_branch_ruleset_exists(self):
        """configure_branch_ruleset.sh 存在(幂等配置脚本)."""
        script = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
        assert script.exists(), "configure_branch_ruleset.sh 不存在"

    def test_configure_branch_protection_exists(self):
        """configure_branch_protection.sh 存在(幂等配置脚本)."""
        script = REPO_ROOT / "scripts" / "configure_branch_protection.sh"
        assert script.exists(), "configure_branch_protection.sh 不存在"

    def test_configure_branch_ruleset_idempotent(self):
        """configure_branch_ruleset.sh 是幂等脚本(PUT 更新或 POST 创建)."""
        script = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
        content = script.read_text(encoding="utf-8")
        # 幂等脚本应包含 PUT 或 update 逻辑
        assert "PUT" in content or "update" in content.lower(), (
            "configure_branch_ruleset.sh 应支持幂等更新(PUT)"
        )

    def test_configure_branch_protection_idempotent(self):
        """configure_branch_protection.sh 是幂等脚本(PUT 覆盖)."""
        script = REPO_ROOT / "scripts" / "configure_branch_protection.sh"
        content = script.read_text(encoding="utf-8")
        assert "PUT" in content, (
            "configure_branch_protection.sh 应使用 PUT 覆盖(幂等)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
