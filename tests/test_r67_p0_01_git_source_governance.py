"""R67 P0-01: Git source governance 测试。

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

    def test_rules_contain_pull_request_with_2_reviewers(self, expected):
        pr_rules = [r for r in expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1, "应有 1 个 pull_request 规则"
        params = pr_rules[0]["parameters"]
        assert params["required_reviewers"] >= 2
        assert params["dismiss_stale_reviews_on_push"] is True
        assert params["required_review_thread_resolution"] is True

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

    def test_script_required_reviewers_at_least_2(self):
        content = CONFIGURE_SH.read_text(encoding="utf-8")
        assert "required_reviewers" in content
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"' in content

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
        """未签名 commit 必须失败(不能仅 warning)。"""
        content = GIT_GOVERNANCE_SH.read_text(encoding="utf-8")
        assert 'X) fail "commit 无签名' in content


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
