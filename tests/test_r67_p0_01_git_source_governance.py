"""R67 P0-01: Git source governance 测试。

R71 Wave 6 P1-01/02/03 整改说明:
    R71 Wave 6 用单一 "R71 Solo Founder Branch Ruleset" 替换 R67/R70 两个旧
    ruleset。solo founder(@maxiuquan 是唯一开发者)模式:
        - required_reviewers: 0(无审批死锁)
        - require_code_owner_review: false(CODEOWNERS 保留但不阻断)
        - strict_merge: true(current-SHA,不允许 stale parent commit)
        - bypass_actors: [](无 admin/app bypass;紧急情况通过 record_break_glass.py)
    本测试文件已同步更新,反映 solo-founder 语义。

测试覆盖矩阵:
    A. branch_ruleset.expected.json schema 与内容(8 个)
    B. configure_branch_ruleset.sh 脚本静态检查(6 个)
    C. verify_branch_ruleset.sh 脚本静态检查(5 个)
    D. verify_git_source_governance.sh 脚本静态检查(7 个)
    E. 集成 — BP expected 与 ruleset expected 互补(4 个)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_JSON = REPO_ROOT / ".github" / "branch_ruleset.expected.json"
CONFIGURE_SH = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_branch_ruleset.sh"
GIT_GOVERNANCE_SH = REPO_ROOT / "scripts" / "verify_git_source_governance.sh"
BP_EXPECTED_JSON = REPO_ROOT / ".github" / "branch_protection.expected.json"
TAG_RULESET_EXPECTED_JSON = REPO_ROOT / ".github" / "tag_ruleset.expected.json"


# ════════════════════════════════════════════════════════════════
# A. branch_ruleset.expected.json schema 与内容
# ════════════════════════════════════════════════════════════════

class TestBranchRulesetExpectedJson:
    """验证期望配置文件的内容符合 R67 P0-01 要求。"""

    @pytest.fixture(scope="class")
    def expected(self):
        return json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))

    def test_file_exists(self):
        assert EXPECTED_JSON.exists(), "应存在 .github/branch_ruleset.expected.json"

    def test_target_is_branch(self, expected):
        assert expected["target"] == "branch"

    def test_enforcement_is_active(self, expected):
        assert expected["enforcement"] == "active"

    def test_source_type_is_repository(self, expected):
        assert expected["source_type"] == "Repository"

    def test_ref_name_includes_master_and_main(self, expected):
        includes = expected["conditions"]["ref_name"]["include"]
        assert "refs/heads/master" in includes
        assert "refs/heads/main" in includes

    def test_rules_contain_required_signatures(self, expected):
        rule_types = [r["type"] for r in expected["rules"]]
        assert "required_signatures" in rule_types

    def test_rules_contain_deletion_and_non_fast_forward_and_update(self, expected):
        """禁止删除 / force push / 直接 update(必须走 PR)。"""
        rule_types = [r["type"] for r in expected["rules"]]
        assert "deletion" in rule_types
        assert "non_fast_forward" in rule_types
        assert "update" in rule_types

    def test_rules_contain_pull_request_with_zero_reviewers(self, expected):
        """R71 Wave 6 P1-01: solo founder — required_approving_review_count == 0(无审批死锁)。

        旧 R67 P0-01 要求 >= 2 reviewers,但对 solo founder(@maxiuquan 是唯一
        开发者)造成审批死锁。R71 Wave 6 改为 0。

        R71 fix: Rulesets API 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        """
        pr_rules = [r for r in expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1, "应有 1 个 pull_request 规则"
        params = pr_rules[0]["parameters"]
        assert params["required_approving_review_count"] == 0, (
            "R71 P1-01: solo founder 模式 required_approving_review_count 必须为 0"
        )
        assert params["dismiss_stale_reviews_on_push"] is True
        assert params["required_review_thread_resolution"] is True
        # R71 P1-01: require_code_owner_review 必须为 false
        assert params.get("require_code_owner_review") is False, (
            "R71 P1-01: solo founder 模式 require_code_owner_review 必须为 false "
            "(CODEOWNERS 保留但不阻断)"
        )

    def test_rules_contain_required_status_checks_with_strict_required_status_checks_policy(
        self, expected
    ):
        """R71 Wave 6 P1-02/03: required_status_checks 必须存在且
        strict_required_status_checks_policy=true。

        R71 fix: Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        """
        rsc_rules = [r for r in expected["rules"] if r["type"] == "required_status_checks"]
        assert len(rsc_rules) == 1, "应有 1 个 required_status_checks 规则"
        params = rsc_rules[0]["parameters"]
        assert params.get("strict_required_status_checks_policy") is True, (
            "R71 P1-03: strict_required_status_checks_policy 必须为 true "
            "(current-SHA,不允许 stale parent commit)"
        )
        # R71 P1-02: 必须包含 R71 Wave 2/4/5 新增的 context
        # R72 P1-06: compose-runtime-e2e / verify-rc-identity 是 tag-only,
        # 在 PR 场景不产生 check-run,已从 required_status_checks 移除
        # (避免合并死锁)。仅保留 validate-oci-rootfs(PR 场景会运行)。
        contexts = [c["context"] for c in params.get("required_status_checks", [])]
        for ctx in ("validate-oci-rootfs", "bind-runtime-config"):
            assert ctx in contexts, (
                f"R71 P1-02: required_status_checks 必须包含 R71 新增 context: {ctx}"
            )

    def test_bypass_actors_is_empty(self, expected):
        """R67 P0-01: 禁止 admin bypass — bypass_actors 必须为空。"""
        assert expected["bypass_actors"] == [], (
            "bypass_actors 必须为空(R67 P0-01 禁止 admin bypass)"
        )


# ════════════════════════════════════════════════════════════════
# B. configure_branch_ruleset.sh 脚本静态检查
# ════════════════════════════════════════════════════════════════

class TestConfigureBranchRulesetScript:
    """验证配置脚本的关键内容。"""

    def test_script_exists_and_executable(self):
        assert CONFIGURE_SH.exists()
        assert CONFIGURE_SH.stat().st_mode & 0o100, "脚本应可执行"

    def test_script_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"bash -n 失败:\n{result.stderr}"

    def test_script_contains_required_rules(self):
        content = CONFIGURE_SH.read_text(encoding="utf-8")
        # 关键规则类型必须在 payload 中出现
        for rule in ("deletion", "non_fast_forward", "update", "required_signatures", "pull_request"):
            assert f'"type": "{rule}"' in content, f"缺少规则: {rule}"

    def test_script_bypass_actors_empty(self):
        content = CONFIGURE_SH.read_text(encoding="utf-8")
        # bypass_actors 必须为空
        assert "bypass_actors: []" in content, "bypass_actors 应为空数组"

    def test_script_required_approving_review_count_default_zero(self):
        """R71 Wave 6 P1-01: solo founder — REQUIRED_REVIEWERS 默认 0(无审批死锁)。

        旧 R67 P0-01 默认 2 reviewers,但对 solo founder 造成审批死锁。
        R71 Wave 6 改为 0。

        R71 fix: Rulesets API payload 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        bash 变量名 REQUIRED_REVIEWERS 保留(脚本内部使用)。
        """
        content = CONFIGURE_SH.read_text(encoding="utf-8")
        assert "required_approving_review_count" in content
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-0}"' in content, (
            "R71 P1-01: configure_branch_ruleset.sh 应默认 REQUIRED_REVIEWERS=0 "
            "(solo founder,无审批死锁)"
        )
        # 不应保留旧的 :-2 默认值
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"' not in content, (
            "R71 P1-01: 不应保留 R67 旧默认值 :-2(solo founder 应为 :-0)"
        )

    def test_script_has_idempotency_check(self):
        content = CONFIGURE_SH.read_text(encoding="utf-8")
        # 必须有按 name 查找现有 ruleset 的逻辑(幂等性)
        assert "EXISTING_RULESET_ID" in content
        assert "PUT" in content and "POST" in content


# ════════════════════════════════════════════════════════════════
# C. verify_branch_ruleset.sh 脚本静态检查
# ════════════════════════════════════════════════════════════════

class TestVerifyBranchRulesetScript:
    """验证验证脚本的关键内容。"""

    def test_script_exists_and_executable(self):
        assert VERIFY_SH.exists()
        assert VERIFY_SH.stat().st_mode & 0o100

    def test_script_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(VERIFY_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_script_checks_all_required_rules(self):
        content = VERIFY_SH.read_text(encoding="utf-8")
        for rule in ("deletion", "non_fast_forward", "update", "required_signatures", "pull_request"):
            assert rule in content, f"验证脚本未检查规则: {rule}"

    def test_script_checks_bypass_actors_empty(self):
        content = VERIFY_SH.read_text(encoding="utf-8")
        assert "bypass_actors" in content
        assert "length == 0" in content, "应断言 bypass_actors 长度为 0"

    def test_script_fail_closed_on_missing_ruleset(self):
        content = VERIFY_SH.read_text(encoding="utf-8")
        # 未找到 ruleset 必须退出 1(fail-closed)
        assert "未找到 branch ruleset" in content
        assert "exit 1" in content


# ════════════════════════════════════════════════════════════════
# D. verify_git_source_governance.sh 脚本静态检查
# ════════════════════════════════════════════════════════════════

class TestVerifyGitSourceGovernanceScript:
    """验证 Git source governance 脚本。"""

    def test_script_exists_and_executable(self):
        assert GIT_GOVERNANCE_SH.exists()
        assert GIT_GOVERNANCE_SH.stat().st_mode & 0o100

    def test_script_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(GIT_GOVERNANCE_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_script_checks_git_verify_commit(self):
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        assert "git verify-commit" in content
        assert "%G?" in content, "应使用 %G? 检查签名状态"

    def test_script_checks_github_api_verification(self):
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        assert "api.github.com" in content
        assert ".commit.verification.verified" in content
        assert "verification.verified=false" in content, "verified=false 必须 fail"

    def test_script_checks_tag_signature(self):
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        assert "refs/tags/" in content
        assert "git verify-tag" in content
        assert "annotated" in content, "应检查 tag 是 annotated"

    def test_script_checks_tag_commit_consistency(self):
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        # tag 指向的 commit 必须与 release candidate 一致
        assert "EXPECTED_TAG_COMMIT" in content
        assert "tag 指向 commit" in content or "release candidate" in content

    def test_script_distinct_commit_and_tag_verification(self):
        """P1-02: source commit 与 annotated tag 必须分别验证。"""
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        # 必须有独立的 tag 验证段(在 commit 验证之后)
        assert "tag 签名不能替代 commit 签名" in content, (
            "应明确 tag 签名与 commit 签名是不同的信任层"
        )

    def test_script_returns_nonzero_on_unsigned_commit(self):
        """未签名 commit 必须失败(不能仅 warning)。

        R68 P0-05 更新:X(本地无签名/U)状态不再直接 fail,
        而是以 GitHub API verification 为权威源。
        - 本地 X/U + GitHub API verified=false → fail
        - 本地 X/U + GitHub API verified=true  → pass(GitHub 持有公钥)
        - 本地 B/R(明确签名错误)              → 直接 fail
        - 本地 E(用户密钥过期)                 → 直接 fail(R68 P0-05)
        - 本地 E(GitHub web-flow 签名)         → 走 GitHub API fallback(R71 RC49)
          原因:GitHub squash/rebase merge commit 由 GitHub 用 noreply@github.com 签名,
          本地 %G?=E 是信任库中的 GitHub GPG 公钥过期,GitHub API 是权威源

        本测试验证 B(bad signature)状态仍直接 fail(不软化)。
        """
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        # B(bad signature)状态仍必须直接 fail
        assert 'B) fail "commit 签名验证失败' in content
        # R(revoked)状态仍必须直接 fail
        assert 'R) fail "commit 签名已撤销' in content
        # E(expired key)+ 用户签名必须 fail(R68 P0-05)
        assert 'fail "commit 签名无法验证(E — expired key,用户密钥过期)"' in content
        # R71 RC49: E + GitHub web-flow 签名走 GitHub API fallback(IS_GITHUB_SIGNED=true)
        assert 'IS_GITHUB_SIGNED' in content
        assert 'noreply@github.com' in content
        # GitHub API verified=false 必须 fail(权威源)
        assert 'GitHub API verification.verified=false' in content


# ════════════════════════════════════════════════════════════════
# E. 集成 — BP expected 与 ruleset expected 互补
# ════════════════════════════════════════════════════════════════

class TestBpAndRulesetComplementary:
    """验证 branch_protection.expected.json 与 branch_ruleset.expected.json 互补。"""

    @pytest.fixture(scope="class")
    def bp_expected(self):
        return json.loads(BP_EXPECTED_JSON.read_text(encoding="utf-8"))

    @pytest.fixture(scope="class")
    def ruleset_expected(self):
        return json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))

    def test_both_files_exist(self):
        assert BP_EXPECTED_JSON.exists()
        assert EXPECTED_JSON.exists()

    def test_both_require_signatures(self, bp_expected, ruleset_expected):
        """BP 与 ruleset 都要求签名(双重保护)。"""
        # BP API required_signatures
        assert bp_expected.get("required_signatures", {}).get("enabled") is True
        # Ruleset required_signatures rule
        rule_types = [r["type"] for r in ruleset_expected["rules"]]
        assert "required_signatures" in rule_types

    def test_ruleset_provides_pr_review_rules_not_in_bp(self, ruleset_expected):
        """ruleset 提供 PR review 规则(reviewers / stale dismissal / conversation resolution)。

        BP 的 required_pull_request_reviews 字段无法配置 conversation resolution,
        必须由 ruleset 补充。
        """
        pr_rules = [r for r in ruleset_expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1
        params = pr_rules[0]["parameters"]
        assert params["required_review_thread_resolution"] is True

    def test_tag_ruleset_already_exists_for_tags(self):
        """R66 P1-11 已有 tag ruleset;R67 P0-01 新增 branch ruleset,两者互补。"""
        assert TAG_RULESET_EXPECTED_JSON.exists()
        tag_ruleset = json.loads(TAG_RULESET_EXPECTED_JSON.read_text(encoding="utf-8"))
        assert tag_ruleset["target"] == "tags"
