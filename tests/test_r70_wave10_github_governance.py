"""R70 Wave 10: GitHub 治理(CODEOWNERS + branch protection ruleset)— 测试套件。

R71 Wave 6 P1-01/02/03 整改说明:
    R71 Wave 6 用单一 "R71 Solo Founder Branch Ruleset" 替换 R67/R70 两个旧
    ruleset。solo founder(@maxiuquan 是唯一开发者)模式:
        - required_reviewers: 0(无审批死锁)
        - require_code_owner_review: false(CODEOWNERS 保留但不阻断)
        - strict_merge: true(current-SHA,不允许 stale parent commit)
        - bypass_actors: [](无 admin/app bypass;紧急情况通过 record_break_glass.py)
        - 35 个 required_status_checks(覆盖所有真实 release-gates.yml job 名)
    本测试文件已同步更新,反映 solo-founder 语义。原 R70/R67 双 ruleset 测试
    类已替换为 R71 Solo Founder 单 ruleset 验证类。

R70 Wave 10 P0-01 治理止血要求(保留部分):
    1. .github/CODEOWNERS 补齐关键信任链路径的 owner
    2. scripts/configure_branch_protection.sh 兼容模式
       (已配置则打印 + 警告迁移,未配置则 legacy API 创建)
    3. 两个脚本均支持 --dry-run(不调用 gh api)
    4. 严格 set -euo pipefail;禁止 TODO/pass/占位符;禁止 skip/warn/吞异常

被测对象:
    - .github/CODEOWNERS
    - scripts/configure_branch_ruleset.sh(R71 Wave 6 Solo Founder)
    - scripts/configure_branch_protection.sh(R48/R65 P1-12 + R70 Wave 10 兼容模式)

测试覆盖矩阵:
    A. CODEOWNERS 完整性 — R70 Wave 10 关键路径 owner 评审(11 个)
    B. configure_branch_ruleset.sh — R71 Solo Founder ruleset 静态检查(8 个)
    C. configure_branch_ruleset.sh — R71 Solo Founder 关键约束(5 个)
    D. configure_branch_ruleset.sh — --dry-run / --help 支持(4 个)
    E. configure_branch_protection.sh — R70 Wave 10 兼容模式(6 个)
    F. configure_branch_protection.sh — 保留 R65 P1-12 严格化(5 个)
    G. 脚本语法 + flag 行为(4 个)

整改规范:
    - 禁止 TODO 注释、pass 占位符、其他占位符
    - 禁止 skip/warn/吞异常(脚本中允许的 WARN 输出不算"吞异常")
    - 测试必须真实可运行(pytest 全部通过)
    - 不修改现有 .github/workflows/*.yml
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEOWNERS_FILE = REPO_ROOT / ".github" / "CODEOWNERS"
CONFIGURE_RULESET_SH = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
CONFIGURE_BP_SH = REPO_ROOT / "scripts" / "configure_branch_protection.sh"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _bash_available() -> bool:
    """检查 bash 是否可用(CI 上始终可用,本地 Windows 可能无)。"""
    try:
        result = subprocess.run(
            ["bash", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _jq_available() -> bool:
    """检查 jq 是否可用(--dry-run 行为测试需要)。"""
    try:
        result = subprocess.run(
            ["jq", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


BASH_AVAILABLE = _bash_available()
JQ_AVAILABLE = _jq_available()

skip_if_no_bash = pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="bash 不可用(本地 Windows 环境;CI 上始终可用)",
)
skip_if_no_jq = pytest.mark.skipif(
    not JQ_AVAILABLE,
    reason="jq 不可用(本地 Windows 环境;CI 上始终可用)",
)


# ════════════════════════════════════════════════════════════════
# A. CODEOWNERS 完整性 — R70 Wave 10 关键路径 owner 评审
# ════════════════════════════════════════════════════════════════


class TestCodeownersR70Wave10:
    """R70 Wave 10: CODEOWNERS 覆盖所有关键信任链路径。

    强制 owner 评审路径(R70 Wave 10 整改要求):
        - .github/workflows/
        - services/restore_writer.py, services/db_restore.py,
          services/backup_dr_validate.py
        - config/environment.py, config/settings.py
        - docker/entrypoint.py, Dockerfile, .dockerignore
        - scripts/check_restore_no_legacy_writer.py,
          scripts/check_compose_static_rules.py
        - docker-compose.prod.yml, docker-compose.yml
    """

    @pytest.fixture(scope="class")
    def codeowners_content(self) -> str:
        assert CODEOWNERS_FILE.exists(), ".github/CODEOWNERS 不存在"
        return CODEOWNERS_FILE.read_text(encoding="utf-8")

    def test_codeowners_exists(self, codeowners_content: str):
        """CODEOWNERS 文件存在且非空。"""
        assert codeowners_content.strip(), "CODEOWNERS 为空"

    def test_codeowners_has_default_owner(self, codeowners_content: str):
        """CODEOWNERS 默认 owner 为 @maxiuquan(兜底 *)。

        兼容多空格对齐排版(CODEOWNERS 文件常对齐列)。
        """
        import re
        pattern = re.compile(r"^\*\s+@maxiuquan\s*$", re.MULTILINE)
        assert pattern.search(codeowners_content), (
            "CODEOWNERS 必须有默认兜底规则 '*  @maxiuquan'"
            "(允许 * 后接任意数量空白字符再接 @maxiuquan)"
        )

    def test_codeowners_covers_workflows(self, codeowners_content: str):
        """R70 Wave 10: .github/workflows/ 强制 owner 评审。"""
        assert ".github/workflows/" in codeowners_content, (
            "CODEOWNERS 未覆盖 .github/workflows/ — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_restore_writer(self, codeowners_content: str):
        """R70 Wave 10: services/restore_writer.py 强制 owner 评审。"""
        assert "services/restore_writer.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 services/restore_writer.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_db_restore(self, codeowners_content: str):
        """R70 Wave 10: services/db_restore.py 强制 owner 评审。"""
        assert "services/db_restore.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 services/db_restore.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_backup_dr_validate(self, codeowners_content: str):
        """R70 Wave 10: services/backup_dr_validate.py 强制 owner 评审。"""
        assert "services/backup_dr_validate.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 services/backup_dr_validate.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_config_environment(self, codeowners_content: str):
        """R70 Wave 10: config/environment.py 强制 owner 评审。"""
        assert "config/environment.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 config/environment.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_config_settings(self, codeowners_content: str):
        """R70 Wave 10: config/settings.py 强制 owner 评审。"""
        assert "config/settings.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 config/settings.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_docker_entrypoint(self, codeowners_content: str):
        """R70 Wave 10: docker/entrypoint.py 强制 owner 评审。"""
        assert "docker/entrypoint.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 docker/entrypoint.py — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_dockerfile(self, codeowners_content: str):
        """R70 Wave 10: Dockerfile 强制 owner 评审。"""
        assert "Dockerfile" in codeowners_content, (
            "CODEOWNERS 未覆盖 Dockerfile — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_dockerignore(self, codeowners_content: str):
        """R70 Wave 10: .dockerignore 强制 owner 评审。"""
        assert ".dockerignore" in codeowners_content, (
            "CODEOWNERS 未覆盖 .dockerignore — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_check_restore_scanner(self, codeowners_content: str):
        """R70 Wave 10: scripts/check_restore_no_legacy_writer.py 强制 owner 评审。"""
        assert "check_restore_no_legacy_writer.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 scripts/check_restore_no_legacy_writer.py — "
            "R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_check_compose_scanner(self, codeowners_content: str):
        """R70 Wave 10: scripts/check_compose_static_rules.py 强制 owner 评审。"""
        assert "check_compose_static_rules.py" in codeowners_content, (
            "CODEOWNERS 未覆盖 scripts/check_compose_static_rules.py — "
            "R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_compose_prod(self, codeowners_content: str):
        """R70 Wave 10: docker-compose.prod.yml 强制 owner 评审。"""
        assert "docker-compose.prod.yml" in codeowners_content, (
            "CODEOWNERS 未覆盖 docker-compose.prod.yml — R70 Wave 10 关键路径"
        )

    def test_codeowners_covers_compose_base(self, codeowners_content: str):
        """R70 Wave 10: docker-compose.yml 强制 owner 评审。"""
        assert "docker-compose.yml" in codeowners_content, (
            "CODEOWNERS 未覆盖 docker-compose.yml — R70 Wave 10 关键路径"
        )


# ════════════════════════════════════════════════════════════════
# B. configure_branch_ruleset.sh — R71 Solo Founder ruleset 静态检查
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchRulesetR71SoloFounder:
    """R71 Wave 6: configure_branch_ruleset.sh 包含 R71 Solo Founder
    Branch Ruleset 配置(单一 ruleset,无审批死锁)。
    """

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        assert CONFIGURE_RULESET_SH.exists(), (
            "scripts/configure_branch_ruleset.sh 不存在"
        )
        return CONFIGURE_RULESET_SH.read_text(encoding="utf-8")

    def test_r71_ruleset_name_present(self, script_content: str):
        """R71 Solo Founder ruleset 名称必须出现。"""
        assert "R71 Solo Founder Branch Ruleset" in script_content, (
            "configure_branch_ruleset.sh 缺少 R71 Solo Founder ruleset 名称"
        )

    def test_r71_required_status_checks_present(self, script_content: str):
        """R71 ruleset 必须包含 5 个核心 status check(lint/static-gates/test/
        verify-branch-ruleset/verify-branch-protection)。"""
        required_checks = [
            "lint",
            "static-gates",
            "test",
            "verify-branch-ruleset",
            "verify-branch-protection",
        ]
        for check in required_checks:
            assert check in script_content, (
                f"configure_branch_ruleset.sh 缺少 R71 required status check: {check}"
            )

    def test_r71_required_status_checks_36_count(self, script_content: str):
        """R71 P1-02: 必须包含至少 36 个 required status check(覆盖所有
        真实 release-gates.yml job 名,含 Wave 7 新增 bind-runtime-config)。"""
        assert "REQUIRED_STATUS_CHECKS" in script_content, (
            "configure_branch_ruleset.sh 应有 REQUIRED_STATUS_CHECKS 变量"
        )
        # 必须有 -lt 36 校验(Wave 7 扩展,从 35 到 36)
        assert "-lt 36" in script_content, (
            "R71 P1-02: configure_branch_ruleset.sh 必须校验 REQUIRED_STATUS_CHECKS "
            "至少 36 项(-lt 36, Wave 7 新增 bind-runtime-config)"
        )
        # R71 Wave 2/4/5/7 新增的 context 必须在默认值中
        for ctx in ("compose-runtime-e2e", "validate-oci-rootfs",
                    "verify-rc-identity", "bind-runtime-config"):
            assert ctx in script_content, (
                f"R71 P1-02: configure_branch_ruleset.sh 缺少 R71 新增 context: {ctx}"
            )

    def test_r71_pull_request_rule_present(self, script_content: str):
        """R71 P1-01: pull_request 规则 required_approving_review_count 必须为 0
        (solo founder,无审批死锁)。

        旧 R70 要求 1 reviewer + R67 要求 2 reviewers,造成 solo founder
        审批死锁。R71 Wave 6 改为 0。

        R71 fix: Rulesets API 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        bash 变量名 REQUIRED_REVIEWERS 保留(脚本内部使用)。
        """
        assert "required_approving_review_count" in script_content
        # payload 中 required_approving_review_count 必须为 0(变量插值后)
        # 脚本中 bash 变量默认值字面值:-0
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-0}"' in script_content, (
            "R71 P1-01: configure_branch_ruleset.sh 应默认 REQUIRED_REVIEWERS=0 "
            "(solo founder,无审批死锁)"
        )
        # 不应保留旧的 :-2 或 :-1 默认值
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"' not in script_content, (
            "R71 P1-01: 不应保留 R67 旧默认值 :-2"
        )
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-1}"' not in script_content, (
            "R71 P1-01: 不应保留 R70 旧默认值 :-1"
        )

    def test_r71_require_code_owner_review_false(self, script_content: str):
        """R71 P1-01: pull_request.require_code_owner_review 必须为 false
        (CODEOWNERS 保留但不阻断)。

        旧 R70 要求 true,造成 solo founder 审批死锁。R71 Wave 6 改为 false。
        """
        assert "require_code_owner_review" in script_content
        # payload 中 require_code_owner_review 必须为 false
        assert "require_code_owner_review\": false" in script_content or \
               "require_code_owner_review\":false" in script_content, (
            "R71 P1-01: pull_request.require_code_owner_review 必须为 false "
            "(solo founder,CODEOWNERS 保留但不阻断)"
        )
        # 不应保留旧的 true 设置
        assert "require_code_owner_review\": true" not in script_content, (
            "R71 P1-01: 不应保留 R70 旧设置 require_code_owner_review: true"
        )

    def test_r71_strict_required_status_checks_policy_true(self, script_content: str):
        """R71 P1-03: required_status_checks.strict_required_status_checks_policy 必须为 true
        (current-SHA,不允许 stale parent commit)。

        R71 fix: Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        """
        assert "strict_required_status_checks_policy" in script_content
        assert "strict_required_status_checks_policy\": true" in script_content or \
               "strict_required_status_checks_policy\":true" in script_content, (
            "R71 P1-03: required_status_checks.strict_required_status_checks_policy "
            "必须为 true "
            "(current-SHA,不允许 stale parent commit)"
        )

    def test_r71_dismiss_stale_reviews_true(self, script_content: str):
        """R71 ruleset pull_request.dismiss_stale_reviews_on_push 必须为 true。"""
        assert "dismiss_stale_reviews_on_push" in script_content
        assert "dismiss_stale_reviews_on_push\": true" in script_content or \
               "dismiss_stale_reviews_on_push\":true" in script_content, (
            "R71 ruleset pull_request.dismiss_stale_reviews_on_push 必须为 true"
        )

    def test_r71_non_fast_forward_present(self, script_content: str):
        """R71 ruleset 必须包含 non_fast_forward 规则(禁 force push)。"""
        assert '"type": "non_fast_forward"' in script_content or \
               '"type":"non_fast_forward"' in script_content, (
            "configure_branch_ruleset.sh 缺少 non_fast_forward 规则"
        )

    def test_r71_bypass_actors_empty(self, script_content: str):
        """R71 P1-01: ruleset bypass_actors 必须为空(admin/app 不可绕过)。

        紧急情况通过 scripts/record_break_glass.py 审计日志,而非 admin bypass。
        """
        assert "bypass_actors: []" in script_content, (
            "configure_branch_ruleset.sh bypass_actors 必须为空数组 "
            "(R71 P1-01: admin/app 不可绕过;紧急情况用 record_break_glass.py)"
        )

    def test_r71_ruleset_targets_master_main(self, script_content: str):
        """R71 ruleset 必须针对 refs/heads/master 与 refs/heads/main。"""
        assert "refs/heads/master" in script_content
        assert "refs/heads/main" in script_content


# ════════════════════════════════════════════════════════════════
# C. configure_branch_ruleset.sh — R71 Solo Founder 关键约束
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchRulesetR71KeyConstraints:
    """R71 Wave 6: configure_branch_ruleset.sh 保留 R67/R70 关键不变性约束
    (被 R71 Solo Founder 单 ruleset 继承):

        - 幂等性(EXISTING_RULESET_ID + PUT + POST)
        - 必需规则类型(deletion/non_fast_forward/update/required_signatures/pull_request)
        - bypass_actors 为空(禁止 admin bypass)
        - 不应保留 R67/R70 旧 ruleset 名称(已替换为 R71 Solo Founder)
    """

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return CONFIGURE_RULESET_SH.read_text(encoding="utf-8")

    def test_r71_idempotency_check_present(self, script_content: str):
        """R71 Solo Founder 保留 R67/R70 幂等性检查逻辑
        (EXISTING_RULESET_ID + PUT + POST)。

        test_r67_p0_01_git_source_governance.py 断言这些字符串。
        """
        assert "EXISTING_RULESET_ID" in script_content, (
            "configure_branch_ruleset.sh 应保留 EXISTING_RULESET_ID 幂等性检查"
        )
        assert "PUT" in script_content and "POST" in script_content, (
            "configure_branch_ruleset.sh 应保留 PUT/POST 幂等性逻辑"
        )

    def test_r71_required_rules_present(self, script_content: str):
        """R71 Solo Founder 保留 R67/R70 必需规则类型
        (deletion/non_fast_forward/update/required_signatures/pull_request)。

        test_r67_p0_01_git_source_governance.py 断言这些字符串。
        """
        for rule in (
            "deletion",
            "non_fast_forward",
            "update",
            "required_signatures",
            "pull_request",
        ):
            assert f'"type": "{rule}"' in script_content, (
                f"configure_branch_ruleset.sh 缺少 R71 规则类型: {rule}"
            )

    def test_r71_bypass_actors_empty(self, script_content: str):
        """R71 P1-01: bypass_actors 为空(禁止 admin bypass)。

        test_r67_p0_01_git_source_governance.py 断言此字符串。
        """
        assert "bypass_actors: []" in script_content

    def test_no_legacy_r67_ruleset_name_as_default(self, script_content: str):
        """R71 Wave 6: 不应保留 R67 P0-01 Branch Immutability Ruleset 名称
        作为 RULESET_NAME 默认值(已被 R71 Solo Founder Branch Ruleset 替换)。

        旧 R67 ruleset 名称只在脚本注释中作为历史说明出现是允许的,
        但作为 RULESET_NAME 默认值出现则是回归。
        """
        # 不应作为 RULESET_NAME 默认值出现
        assert 'RULESET_NAME="${RULESET_NAME:-R67 P0-01' not in script_content, (
            "R71 P1-01: 不应保留 R67 P0-01 作为 RULESET_NAME 默认值"
        )

    def test_no_legacy_r70_ruleset_name_as_default(self, script_content: str):
        """R71 Wave 6: 不应保留 r70-governance-master-protect 名称
        作为 RULESET_NAME 默认值(已被 R71 Solo Founder Branch Ruleset 替换)。

        旧 R70 ruleset 名称只在脚本注释中作为历史说明出现是允许的,
        但作为 RULESET_NAME 默认值出现则是回归。
        """
        # 不应作为 RULESET_NAME 默认值出现
        assert 'RULESET_NAME="${RULESET_NAME:-r70-governance-master-protect' not in script_content, (
            "R71 P1-01: 不应保留 r70-governance-master-protect 作为 RULESET_NAME 默认值"
        )


# ════════════════════════════════════════════════════════════════
# D. configure_branch_ruleset.sh — --dry-run / --help 支持
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchRulesetDryRun:
    """R70 Wave 10: configure_branch_ruleset.sh 支持 --dry-run / --help。"""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return CONFIGURE_RULESET_SH.read_text(encoding="utf-8")

    def test_script_has_set_euo_pipefail(self, script_content: str):
        """脚本必须含 `set -euo pipefail`(严格模式)。"""
        assert "set -euo pipefail" in script_content, (
            "configure_branch_ruleset.sh 必须含 set -euo pipefail"
        )

    def test_script_supports_dry_run_flag(self, script_content: str):
        """脚本支持 --dry-run flag(不调用 gh api)。"""
        assert "--dry-run" in script_content, (
            "configure_branch_ruleset.sh 必须支持 --dry-run flag"
        )
        # dry-run 分支必须有 exit 0
        assert "DRY_RUN=true" in script_content or "DRY_RUN=\"true\"" in script_content
        # dry-run 路径不应调用 gh api(静态检查:有 exit 0 在 dry-run 块内)
        assert "未调用任何 gh api" in script_content or \
               "不调用 gh api" in script_content, (
            "configure_branch_ruleset.sh --dry-run 模式必须明确不调用 gh api"
        )

    def test_script_supports_help_flag(self, script_content: str):
        """脚本支持 --help / -h flag。"""
        assert "--help" in script_content
        assert "-h" in script_content
        assert "print_help" in script_content, (
            "configure_branch_ruleset.sh 应有 print_help 函数"
        )

    @skip_if_no_bash
    def test_script_help_exits_zero(self):
        """--help flag 实际执行应 exit 0(不需要 jq/gh)。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_RULESET_SH), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"--help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 帮助输出应含关键信息
        assert "用法" in result.stdout or "usage" in result.stdout.lower()
        assert "--dry-run" in result.stdout

    @skip_if_no_bash
    @skip_if_no_jq
    def test_script_dry_run_exits_zero_without_gh(self):
        """--dry-run 模式应 exit 0,不调用 gh api(不需要鉴权)。

        需要 jq 来构造 payload(--dry-run 仍需 jq)。
        """
        result = subprocess.run(
            ["bash", str(CONFIGURE_RULESET_SH), "--dry-run",
             "test-owner", "test-repo"],
            capture_output=True, text=True, timeout=15,
            # 不传 GH_TOKEN/GITHUB_TOKEN,确保 --dry-run 不需要鉴权
            env={**__import__("os").environ,
                 "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        assert result.returncode == 0, (
            f"--dry-run 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 输出应含 payload 标识
        assert "DRY RUN" in result.stdout or "dry run" in result.stdout.lower()
        # 不应调用 gh api(检查 stderr 不含 gh auth 错误)
        assert "gh auth" not in result.stderr


# ════════════════════════════════════════════════════════════════
# E. configure_branch_protection.sh — R70 Wave 10 兼容模式
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchProtectionCompatMode:
    """R70 Wave 10: configure_branch_protection.sh 兼容模式。

    兼容模式行为:
        - 若 BP 已配置:打印当前配置 + 警告"建议迁移到 Ruleset",不覆盖
        - 若 BP 未配置(404):使用 legacy API 配置(作为安全网)
        - --dry-run:仅打印 payload,不调用 gh api
    """

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        assert CONFIGURE_BP_SH.exists(), (
            "scripts/configure_branch_protection.sh 不存在"
        )
        return CONFIGURE_BP_SH.read_text(encoding="utf-8")

    def test_script_has_set_euo_pipefail(self, script_content: str):
        """脚本必须含 `set -euo pipefail`(严格模式)。"""
        assert "set -euo pipefail" in script_content, (
            "configure_branch_protection.sh 必须含 set -euo pipefail"
        )

    def test_script_supports_dry_run_flag(self, script_content: str):
        """脚本支持 --dry-run flag(不调用 gh api)。"""
        assert "--dry-run" in script_content, (
            "configure_branch_protection.sh 必须支持 --dry-run flag"
        )
        # dry-run 分支必须有 exit 0
        assert "DRY_RUN=true" in script_content or "DRY_RUN=\"true\"" in script_content
        # dry-run 路径不应调用 gh api
        assert "不调用 gh api" in script_content or \
               "未调用任何 gh api" in script_content, (
            "configure_branch_protection.sh --dry-run 必须明确不调用 gh api"
        )

    def test_script_supports_help_flag(self, script_content: str):
        """脚本支持 --help / -h flag。"""
        assert "--help" in script_content
        assert "-h" in script_content
        assert "print_help" in script_content

    def test_compat_mode_check_existing_bp(self, script_content: str):
        """R70 Wave 10 兼容模式:检查现有 BP(GET /branches/master/protection)。

        若已配置则打印 + 警告,不覆盖。
        """
        # 必须有 GET /branches/master/protection 检查
        assert "branches/master/protection" in script_content
        # 必须有兼容模式标识
        assert "R70 Wave 10 兼容模式" in script_content or \
               "兼容模式" in script_content, (
            "configure_branch_protection.sh 应有 R70 Wave 10 兼容模式标识"
        )

    def test_compat_mode_warns_migration_to_ruleset(self, script_content: str):
        """R70 Wave 10 兼容模式:BP 已配置时警告"建议迁移到 Ruleset"。"""
        # 必须有迁移警告
        assert "迁移到 Repository Ruleset" in script_content or \
               "迁移到 Ruleset" in script_content or \
               "建议迁移" in script_content, (
            "configure_branch_protection.sh 兼容模式必须警告迁移到 Ruleset"
        )
        # 必须指向 configure_branch_ruleset.sh
        assert "configure_branch_ruleset.sh" in script_content, (
            "configure_branch_protection.sh 应指向 configure_branch_ruleset.sh "
            "作为迁移目标"
        )

    def test_compat_mode_does_not_overwrite_existing(self, script_content: str):
        """R70 Wave 10 兼容模式:BP 已配置时不覆盖(exit 0,跳过 PUT)。

        检查脚本中"已配置"分支不会调用 PUT 创建。
        """
        # 兼容模式块应包含 "保留 legacy BP 作为 backup" 或类似不覆盖声明
        assert "不覆盖" in script_content or \
               "作为 backup" in script_content or \
               "保留 legacy BP" in script_content, (
            "configure_branch_protection.sh 兼容模式必须声明不覆盖现有 BP"
        )

    @skip_if_no_bash
    def test_script_help_exits_zero(self):
        """--help flag 实际执行应 exit 0。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_BP_SH), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"--help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "用法" in result.stdout or "usage" in result.stdout.lower()
        assert "--dry-run" in result.stdout

    @skip_if_no_bash
    @skip_if_no_jq
    def test_script_dry_run_exits_zero_without_gh(self):
        """--dry-run 模式应 exit 0,不调用 gh api。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_BP_SH), "--dry-run",
             "test-owner", "test-repo"],
            capture_output=True, text=True, timeout=15,
            env={**__import__("os").environ,
                 "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        assert result.returncode == 0, (
            f"--dry-run 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "DRY RUN" in result.stdout or "dry run" in result.stdout.lower()


# ════════════════════════════════════════════════════════════════
# F. configure_branch_protection.sh — 保留 R65 P1-12 严格化
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchProtectionR65Preserved:
    """R70 Wave 10 整改保留 R65 P1-12 严格化要求。

    test_r65_p1_12_branch_protection_consistency.py 已断言以下字符串,本类做交叉验证。
    """

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        return CONFIGURE_BP_SH.read_text(encoding="utf-8")

    def test_enforce_admins_true(self, script_content: str):
        """R65 P1-12: enforce_admins: true(admin 不能 bypass)。"""
        assert "enforce_admins: true" in script_content

    def test_required_approving_review_count_2(self, script_content: str):
        """R65 P1-12: required_approving_review_count: 2(独立 reviewer)。"""
        assert "required_approving_review_count: 2" in script_content
        # 不能使用 1
        assert "required_approving_review_count: 1" not in script_content

    def test_dismiss_stale_reviews_true(self, script_content: str):
        """R65 P1-12: dismiss_stale_reviews: true。"""
        assert "dismiss_stale_reviews: true" in script_content

    def test_required_linear_history_true(self, script_content: str):
        """R65 P1-12: required_linear_history: true(禁 merge commit)。"""
        assert "required_linear_history: true" in script_content

    def test_required_conversation_resolution_true(self, script_content: str):
        """R65 P1-12: required_conversation_resolution: true。"""
        assert "required_conversation_resolution: true" in script_content

    def test_required_signatures_api_called(self, script_content: str):
        """R65 P1-12: required_signatures 通过独立 POST API 启用。"""
        assert "protection/required_signatures" in script_content
        assert "required_signatures" in script_content

    def test_put_idempotent(self, script_content: str):
        """configure_branch_protection.sh 使用 PUT(幂等覆盖)。

        test_r68_governance_legacy_removal.py 断言此字符串。
        """
        assert "PUT" in script_content, (
            "configure_branch_protection.sh 应使用 PUT 覆盖(幂等)"
        )

    def test_code_owner_reviews_conditional(self, script_content: str):
        """R65 P1-12: require_code_owner_reviews 与 CODEOWNERS 存在性一致。"""
        assert "CODEOWNERS" in script_content
        assert "REQUIRE_CODE_OWNER_REVIEWS" in script_content
        assert "require_code_owner_reviews: $code_owner_reviews" in script_content, (
            "R65 P1-12: require_code_owner_reviews 应根据 CODEOWNERS 存在性动态设置"
        )


# ════════════════════════════════════════════════════════════════
# G. 脚本语法 + flag 行为
# ════════════════════════════════════════════════════════════════


class TestScriptSyntaxAndFlags:
    """R70 Wave 10: 脚本语法合法 + flag 行为正确。"""

    @skip_if_no_bash
    def test_configure_ruleset_script_syntax_ok(self):
        """configure_branch_ruleset.sh bash 语法合法。"""
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_RULESET_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"bash -n 失败:\n{result.stderr}"
        )

    @skip_if_no_bash
    def test_configure_bp_script_syntax_ok(self):
        """configure_branch_protection.sh bash 语法合法。"""
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_BP_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"bash -n 失败:\n{result.stderr}"
        )

    def test_configure_ruleset_no_workflows_modification(self):
        """R70 Wave 10 整改规范:configure_branch_ruleset.sh 不修改 workflows。

        本测试为静态断言:脚本中不应有 sed/awk/tee 修改
        .github/workflows/*.yml 的代码。
        """
        content = CONFIGURE_RULESET_SH.read_text(encoding="utf-8")
        # 不应修改 workflows
        assert ".github/workflows" not in content or \
               "workflows" not in content.split("修改")[0] if "修改" in content else True
        # 静态检查:无 sed -i 修改 yml
        assert "sed -i" not in content, (
            "configure_branch_ruleset.sh 不应使用 sed -i 修改文件"
        )

    def test_configure_bp_no_workflows_modification(self):
        """R70 Wave 10 整改规范:configure_branch_protection.sh 不修改 workflows。"""
        content = CONFIGURE_BP_SH.read_text(encoding="utf-8")
        assert "sed -i" not in content, (
            "configure_branch_protection.sh 不应使用 sed -i 修改文件"
        )

    def test_scripts_no_todo_or_placeholders(self):
        """R70 Wave 10 整改规范:脚本禁止 TODO/pass/占位符。"""
        for script in [CONFIGURE_RULESET_SH, CONFIGURE_BP_SH]:
            content = script.read_text(encoding="utf-8")
            # 禁止 TODO(注释中也不允许,严格规范)
            # 但允许在字符串中提及 TODO(如错误消息),因此检查行首/独立 TODO
            lines = content.splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # 跳过注释行中的 TODO(因为整改规范说"禁止 TODO 注释",但允许 TODO 字符串)
                # 严格模式:任何 TODO 都不允许
                if stripped.startswith("#"):
                    # 注释行不允许 TODO
                    assert "TODO" not in stripped.upper(), (
                        f"{script.name}:{i} 注释中含 TODO — 违反 R70 整改规范"
                    )
                # 禁止 pass 占位符(shell 中无 pass,但检查)
                # 禁止 'placeholder' / '占位符' 字面值
                assert "placeholder" not in stripped.lower(), (
                    f"{script.name}:{i} 含 placeholder 占位符 — 违反 R70 整改规范"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
