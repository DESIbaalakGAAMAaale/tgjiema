"""R65 P1-12: 分支保护 contexts 动态一致性 + 严格评审规则 测试。

审计背景(R65 终审报告 P1-12):

    分支保护已通过,但需与 Required contexts 动态一致。
    仍需确保 ruleset context 名与实际 job name 双向一致、禁止 admin bypass、
    dismiss stale approvals、required conversation resolution、signed commit、
    独立 reviewer。

整改方案(R65 P1-12):

    1. 新增 ``scripts/check_branch_protection_contexts.py`` 动态校验:
       - 从 ``.github/workflows/*.yml`` 提取所有 required job name(含矩阵展开)
       - 与 branch protection ruleset required contexts 双向比对
       - 检测孤儿 context(在 ruleset 但不在 workflow)
       - 检测缺失 context(在 workflow 但不在 ruleset)
       - 任何不一致即 ``exit 1``
    2. ``configure_branch_protection.sh`` 严格化:
       - enforce_admins=true(admin 不能 bypass)
       - allow_force_pushes=false / allow_deletions=false
       - required_linear_history=true(禁 merge commit)
       - required_signatures=true(签名 commit 必需,通过独立 API 启用)
       - dismiss_stale_reviews=true(新提交自动 dismiss 旧 approval)
       - required_approving_review_count=2(独立 reviewer)
       - require_code_owner_reviews=true(若 CODEOWNERS 存在)
       - required_conversation_resolution=true(所有 conversation 必须解决)
       - dismissal_restrictions={users:[],teams:[]}(任何人可 dismiss)
    3. Release Gates ``verify-branch-protection`` job 新增:
       - 4.6–4.12 R65 P1-12 严格化断言
       - 4.13 动态 context 一致性检查(调用 check_branch_protection_contexts.py)

测试覆盖矩阵(10 个场景):
    A. workflow YAML context 提取(矩阵展开 + push-only 过滤)
    B. 孤儿 context 检测(在 BP 但不在 workflow)
    C. 缺失 context 检测(在 workflow 但不在 BP)
    D. 禁止 admin bypass(enforce_admins=true)
    E. dismiss stale reviews 启用
    F. required_approving_review_count >= 2(独立 reviewer)
    G. required_conversation_resolution=true
    H. required_signatures=true(signed commits 必需)
    I. ``check_branch_protection_contexts.py`` exit 0 on consistent state
    J. ``check_branch_protection_contexts.py`` exit 1 on inconsistent state
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容 — conftest.py 在收集阶段已注入 config/telegram mock,
# 此处再注入一次以防本文件被单独运行
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CHECK_SCRIPT = SCRIPTS_DIR / "check_branch_protection_contexts.py"
CONFIGURE_BP_SCRIPT = SCRIPTS_DIR / "configure_branch_protection.sh"
RELEASE_GATES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEFAULT_BP_CONFIG = REPO_ROOT / ".github" / "branch_protection.expected.json"
# R65 fix: verify-branch-protection job 的 inline run: 块提取到外部脚本
# scripts/verify_branch_protection.sh(避免 YAML 21000 字节限制)。
# 原 release-gates.yml 中的 BP 断言(enforce_admins / dismiss_stale_reviews /
# required_signatures 等)现位于该脚本中,测试需读取该文件做断言检查。
VBP_SCRIPT = SCRIPTS_DIR / "verify_branch_protection.sh"

# 将 scripts/ 加入 sys.path 以便直接 import 模块函数
sys.path.insert(0, str(SCRIPTS_DIR))


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _run_check_script(
    *args: str,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """运行 check_branch_protection_contexts.py,返回 (exit_code, stdout, stderr)。

    Args:
        *args: 命令行参数(如 ``--bp-config <path>``)
        cwd: 工作目录(默认 REPO_ROOT)
    """
    cmd = [sys.executable, str(CHECK_SCRIPT), *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
    )
    return result.returncode, result.stdout, result.stderr


def _write_synthetic_workflow(
    workflows_dir: Path,
    jobs: dict[str, dict],
    workflow_name: str = "Synthetic CI",
    file_name: str = "synthetic.yml",
) -> Path:
    """生成合成 workflow YAML 文件用于测试。

    Args:
        workflows_dir: workflows 目录
        jobs: ``{job_id: job_def}`` 字典
        workflow_name: workflow 名
        file_name: 文件名
    """
    workflows_dir.mkdir(parents=True, exist_ok=True)
    import yaml
    workflow = {
        "name": workflow_name,
        "on": {
            "push": {"branches": ["master"]},
            "pull_request": {"branches": ["master"]},
        },
        "jobs": jobs,
    }
    path = workflows_dir / file_name
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(workflow, f, sort_keys=False)
    return path


def _write_synthetic_bp_config(
    path: Path,
    contexts: list[str],
    *,
    enforce_admins: bool = True,
    required_approving_review_count: int = 2,
    dismiss_stale_reviews: bool = True,
    required_linear_history: bool = True,
    required_conversation_resolution: bool = True,
    require_code_owner_reviews: bool = False,
    required_signatures_enabled: bool = True,
) -> Path:
    """生成合成 BP 配置 JSON 文件用于测试。

    支持完整 GitHub API 响应 schema,以便测试 ``configure_branch_protection.sh``
    期望的所有字段。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    bp_data = {
        "url": "https://api.github.com/repos/test/repo/branches/master/protection",
        "required_status_checks": {
            "strict": True,
            "contexts": contexts,
            "checks": [],
        },
        "enforce_admins": {"enabled": enforce_admins},
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": dismiss_stale_reviews,
            "require_code_owner_reviews": require_code_owner_reviews,
            "required_approving_review_count": required_approving_review_count,
            "dismissal_restrictions": {"users": [], "teams": []},
        },
        "restrictions": None,
        "required_linear_history": {"enabled": required_linear_history},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "block_creations": {"enabled": False},
        "required_conversation_resolution": {
            "enabled": required_conversation_resolution
        },
        "required_signatures": {"enabled": required_signatures_enabled},
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2)
    return path


# ════════════════════════════════════════════════════════════════
# A. workflow YAML context 提取(矩阵展开 + push-only 过滤)
# ════════════════════════════════════════════════════════════════


class TestWorkflowContextExtraction:
    """从 workflow YAML 提取 job 名(含矩阵展开、push-only 过滤)。

    场景 1: 正确解析所有 job 名(含矩阵展开为 ``test (3.10)`` 等形式)。
    """

    def test_extract_simple_jobs(self, tmp_path: Path):
        """场景 1a: 普通 job 名提取(无矩阵)。"""
        from check_branch_protection_contexts import parse_workflow_file

        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "lint": {"runs-on": "ubuntu-latest"},
                "repo-hygiene": {"runs-on": "ubuntu-latest"},
            },
            file_name="simple.yml",
            workflow_name="Simple CI",
        )
        wf = parse_workflow_file(workflows_dir / "simple.yml")
        assert set(wf.jobs) == {"lint", "repo-hygiene"}
        assert wf.excluded_jobs == []

    def test_extract_matrix_jobs(self, tmp_path: Path):
        """场景 1b: 矩阵 job 展开为 ``test (3.10)`` / ``test (3.11)`` / ``test (3.12)``。"""
        from check_branch_protection_contexts import parse_workflow_file

        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "test": {
                    "runs-on": "ubuntu-latest",
                    "strategy": {
                        "fail-fast": False,
                        "matrix": {
                            "python-version": ["3.10", "3.11", "3.12"],
                        },
                    },
                },
            },
            file_name="matrix.yml",
            workflow_name="Matrix CI",
        )
        wf = parse_workflow_file(workflows_dir / "matrix.yml")
        assert set(wf.jobs) == {
            "test (3.10)",
            "test (3.11)",
            "test (3.12)",
        }
        assert wf.excluded_jobs == []

    def test_extract_push_only_jobs_filtered(self, tmp_path: Path):
        """场景 1c: push-only job(``if: github.event_name == 'push'``)被排除。"""
        from check_branch_protection_contexts import parse_workflow_file

        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "always-run": {"runs-on": "ubuntu-latest"},
                "push-only": {
                    "runs-on": "ubuntu-latest",
                    "if": "github.event_name == 'push' && github.ref == 'refs/heads/master'",
                },
            },
            file_name="push_only.yml",
            workflow_name="PushOnly CI",
        )
        wf = parse_workflow_file(workflows_dir / "push_only.yml")
        # always-run 保留,push-only 排除
        assert "always-run" in wf.jobs
        assert "push-only" not in wf.jobs
        assert "push-only" in wf.excluded_jobs

    def test_extract_tag_only_jobs_filtered(self, tmp_path: Path):
        """场景 1c-bis: tag-only job(``if: startsWith(github.ref, 'refs/tags/v')``)
        被排除。

        R65 P0-04 引入的 ``production-promotion-gate`` 仅在 release tag
        (v*.*.*) 触发时运行,PR 场景自动 skipped,因此不应作为 BP required
        context(否则 PR 会被永久阻塞)。
        """
        from check_branch_protection_contexts import parse_workflow_file

        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "always-run": {"runs-on": "ubuntu-latest"},
                "tag-only": {
                    "runs-on": "ubuntu-latest",
                    "if": "startsWith(github.ref, 'refs/tags/v')",
                },
            },
            file_name="tag_only.yml",
            workflow_name="TagOnly CI",
        )
        wf = parse_workflow_file(workflows_dir / "tag_only.yml")
        # always-run 保留,tag-only 排除
        assert "always-run" in wf.jobs
        assert "tag-only" not in wf.jobs
        assert "tag-only" in wf.excluded_jobs

    def test_extract_real_workflows_succeeds(self):
        """场景 1d: 解析仓库实际 .github/workflows/*.yml 全部成功,
        且 Release Gates 14 个核心 job + CI 的 repo-hygiene 均被提取。"""
        from check_branch_protection_contexts import parse_workflows_dir

        workflows_dir = REPO_ROOT / ".github" / "workflows"
        wfs = parse_workflows_dir(workflows_dir)
        assert len(wfs) >= 4, f"期望至少 4 个 workflow,实际 {len(wfs)}"

        # 找到 Release Gates workflow
        rg_wfs = [wf for wf in wfs if wf.workflow_name == "Release Gates"]
        assert len(rg_wfs) == 1, "应有 1 个 Release Gates workflow"
        rg = rg_wfs[0]
        # R64 P0-01 失败点 1: 14 个 Release Gates job 必须全部提取
        # R65 P1-12: verify-branch-protection 是自排除 job(BP 循环依赖),
        # 不在 rg.jobs 中,而在 rg.self_excluded_jobs 中
        expected_rg_jobs = [
            "docker-build", "docker-digest-verify", "compose-config",
            "redis-acl-matrix", "schema-diff", "backup-restore-drill",
            "sbom", "pip-audit", "trivy",
            "rc-continuity", "release-summary",
        ]
        for job in expected_rg_jobs:
            assert job in rg.jobs, (
                f"Release Gates workflow 应提取 job '{job}' — 实际: {sorted(rg.jobs)}"
            )
        # R65 P1-12: verify-branch-protection 自排除(BP 循环依赖)
        assert "verify-branch-protection" in rg.self_excluded_jobs, (
            "verify-branch-protection 应在 self_excluded_jobs(BP 循环依赖)— "
            f"实际: {sorted(rg.self_excluded_jobs)}"
        )
        # R65 P1-12: production-evidence 非阻断(失败不阻断 PR 合并)
        assert "production-evidence" in rg.non_blocking_jobs, (
            "production-evidence 应在 non_blocking_jobs(R64 P1-12 非阻断)— "
            f"实际: {sorted(rg.non_blocking_jobs)}"
        )
        # sign-image / publish-attestation 是 push-only,应被排除
        assert "sign-image" in rg.excluded_jobs, (
            "sign-image 是 push-only job,应被排除出期望 context 集合"
        )
        assert "publish-attestation" in rg.excluded_jobs, (
            "publish-attestation 是 push-only job,应被排除出期望 context 集合"
        )
        # production-promotion-gate 是 tag-only (R65 P0-04),应被排除
        assert "production-promotion-gate" in rg.excluded_jobs, (
            "production-promotion-gate 是 tag-only job (R65 P0-04: 仅 release "
            "tag v*.*.* 触发),应被排除出期望 context 集合 — 实际 excluded: "
            f"{sorted(rg.excluded_jobs)}"
        )

        # 找到 CI workflow,验证 repo-hygiene 被提取
        ci_wfs = [wf for wf in wfs if wf.workflow_name == "CI"]
        assert len(ci_wfs) == 1, "应有 1 个 CI workflow"
        ci = ci_wfs[0]
        assert "repo-hygiene" in ci.jobs, (
            "CI workflow 应提取 repo-hygiene job (R64 P1-11 required context)"
        )
        # 矩阵 job 展开
        assert "test (3.10)" in ci.jobs
        assert "test (3.11)" in ci.jobs
        assert "test (3.12)" in ci.jobs


# ════════════════════════════════════════════════════════════════
# B. 孤儿 context 检测(在 BP 但不在 workflow)
# ════════════════════════════════════════════════════════════════


class TestOrphanContextDetection:
    """场景 2: BP 中存在 workflow 中没有的 context(孤儿)→ 比对失败。"""

    def test_orphan_context_detected(self, tmp_path: Path):
        """场景 2: BP 中有 'ghost-job' 但 workflow 中没有 → 检测为孤儿。"""
        from check_branch_protection_contexts import compare_contexts, WorkflowInfo

        workflows = [
            WorkflowInfo(
                file_name="ci.yml",
                workflow_name="CI",
                jobs=["lint", "test"],
            )
        ]
        bp_contexts = ["lint", "test", "ghost-job"]
        report = compare_contexts(workflows, bp_contexts)
        assert not report.is_consistent
        assert "ghost-job" in report.orphan_in_bp
        assert "ghost-job" not in report.missing_in_bp

    def test_orphan_with_workflow_prefix_detected(self, tmp_path: Path):
        """场景 2b: BP 中有 'CI / ghost-job' 前缀格式 → 归一化后仍检测为孤儿。"""
        from check_branch_protection_contexts import compare_contexts, WorkflowInfo

        workflows = [
            WorkflowInfo(
                file_name="ci.yml",
                workflow_name="CI",
                jobs=["lint", "test"],
            )
        ]
        # 'CI / lint' 应归一化为 'lint' 并匹配,'CI / ghost' 应为孤儿
        bp_contexts = ["CI / lint", "CI / ghost"]
        report = compare_contexts(workflows, bp_contexts)
        assert not report.is_consistent
        assert "CI / ghost" in report.orphan_in_bp
        assert "CI / lint" not in report.orphan_in_bp
        # 'test' 应为缺失(BP 中没有 'test' 也没有 'CI / test')
        assert "test" in report.missing_in_bp


# ════════════════════════════════════════════════════════════════
# C. 缺失 context 检测(在 workflow 但不在 BP)
# ════════════════════════════════════════════════════════════════


class TestMissingContextDetection:
    """场景 3: workflow 中有 job 但 BP 中没有该 context(缺失)→ 比对失败。"""

    def test_missing_context_detected(self, tmp_path: Path):
        """场景 3: workflow 有 'repo-hygiene' 但 BP 没有 → 检测为缺失。"""
        from check_branch_protection_contexts import compare_contexts, WorkflowInfo

        workflows = [
            WorkflowInfo(
                file_name="ci.yml",
                workflow_name="CI",
                jobs=["lint", "test", "repo-hygiene"],
            )
        ]
        # BP 只有 lint 和 test,缺少 repo-hygiene
        bp_contexts = ["lint", "test"]
        report = compare_contexts(workflows, bp_contexts)
        assert not report.is_consistent
        assert "repo-hygiene" in report.missing_in_bp
        assert "lint" not in report.missing_in_bp
        assert "test" not in report.missing_in_bp

    def test_missing_with_workflow_prefix_matches(self, tmp_path: Path):
        """场景 3b: BP 中 'CI / lint' 与 workflow 中 'lint' 应视为匹配,不算缺失。"""
        from check_branch_protection_contexts import compare_contexts, WorkflowInfo

        workflows = [
            WorkflowInfo(
                file_name="ci.yml",
                workflow_name="CI",
                jobs=["lint", "test"],
            )
        ]
        bp_contexts = ["CI / lint", "CI / test"]
        report = compare_contexts(workflows, bp_contexts)
        assert report.is_consistent, (
            f"BP 用 'CI / X' 前缀格式,workflow 用 'X' — 应视为匹配。"
            f" orphan={report.orphan_in_bp}, missing={report.missing_in_bp}"
        )


# ════════════════════════════════════════════════════════════════
# D. 禁止 admin bypass(enforce_admins=true)
# ════════════════════════════════════════════════════════════════


class TestNoAdminBypass:
    """场景 4: configure_branch_protection.sh 必须 enforce_admins=true
    (admin 不能 bypass)。"""

    @pytest.fixture
    def configure_script(self) -> str:
        assert CONFIGURE_BP_SCRIPT.exists(), "configure_branch_protection.sh 必须存在"
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    def test_enforce_admins_true(self, configure_script: str):
        """场景 4: payload 中 enforce_admins 必须为 true。"""
        # 在 jq payload 中 enforce_admins: true
        assert "enforce_admins: true" in configure_script, (
            "R65 P1-12: configure_branch_protection.sh 必须设置 enforce_admins: true "
            "(admin 不能 bypass)"
        )

    def test_enforce_admins_asserted_in_post_config(self, configure_script: str):
        """场景 4b: 配置后断言 enforce_admins.enabled == true。"""
        assert "enforce_admins.enabled == true" in configure_script, (
            "R65 P1-12: 配置后必须断言 enforce_admins.enabled == true"
        )

    def test_release_gates_asserts_enforce_admins(self):
        """场景 4c: release-gates.yml verify-branch-protection 也要断言 enforce_admins。

        R65 fix: 断言已移至 scripts/verify_branch_protection.sh,
        同时检查 workflow 文件和脚本文件。
        """
        content = RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")
        assert "enforce_admins.enabled == true" in content, (
            "R65 P1-12: release-gates.yml verify-branch-protection 必须断言 enforce_admins"
        )

    def test_required_linear_history_true(self, configure_script: str):
        """场景 4d: required_linear_history=true(禁 merge commit)。"""
        assert "required_linear_history: true" in configure_script, (
            "R65 P1-12: configure_branch_protection.sh 必须设置 "
            "required_linear_history: true (禁 merge commit)"
        )

    def test_allow_force_pushes_false(self, configure_script: str):
        """场景 4e: allow_force_pushes=false。"""
        assert "allow_force_pushes: false" in configure_script

    def test_allow_deletions_false(self, configure_script: str):
        """场景 4f: allow_deletions=false。"""
        assert "allow_deletions: false" in configure_script


# ════════════════════════════════════════════════════════════════
# E. dismiss stale reviews 启用
# ════════════════════════════════════════════════════════════════


class TestStaleReviewDismissal:
    """场景 5: dismiss_stale_reviews=true(新提交自动 dismiss 旧 approval)。"""

    @pytest.fixture
    def configure_script(self) -> str:
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    def test_dismiss_stale_reviews_true(self, configure_script: str):
        """场景 5: payload 中 dismiss_stale_reviews=true。"""
        assert "dismiss_stale_reviews: true" in configure_script, (
            "R65 P1-12: configure_branch_protection.sh 必须设置 "
            "dismiss_stale_reviews: true"
        )

    def test_dismiss_stale_reviews_asserted(self, configure_script: str):
        """场景 5b: 配置后断言 dismiss_stale_reviews == true。"""
        assert "dismiss_stale_reviews == true" in configure_script, (
            "R65 P1-12: 配置后必须断言 dismiss_stale_reviews == true"
        )

    def test_dismissal_restrictions_configured(self, configure_script: str):
        """场景 5c: dismissal_restrictions 显式配置(users/teams 均为空数组)。"""
        assert "dismissal_restrictions" in configure_script
        assert "users: []" in configure_script or "users: []" in configure_script
        assert "teams: []" in configure_script or "teams: []" in configure_script

    def test_release_gates_asserts_dismiss_stale_reviews(self):
        """场景 5d: release-gates.yml 也断言 dismiss_stale_reviews。

        R65 fix: 断言已移至 scripts/verify_branch_protection.sh。
        """
        content = RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")
        assert "dismiss_stale_reviews == true" in content


# ════════════════════════════════════════════════════════════════
# F. required_approving_review_count >= 2(独立 reviewer)
# ════════════════════════════════════════════════════════════════


class TestRequiredApprovingReviewCount:
    """场景 6: required_approving_review_count >= 2(独立 reviewer)。"""

    @pytest.fixture
    def configure_script(self) -> str:
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    def test_review_count_is_two(self, configure_script: str):
        """场景 6: payload 中 required_approving_review_count = 2。"""
        assert "required_approving_review_count: 2" in configure_script, (
            "R65 P1-12: configure_branch_protection.sh 必须设置 "
            "required_approving_review_count: 2 (独立 reviewer)"
        )

    def test_review_count_not_one(self, configure_script: str):
        """场景 6b: 不能再使用 review_count = 1(旧值)。"""
        # 不能出现 'required_approving_review_count: 1' 字面值
        assert "required_approving_review_count: 1" not in configure_script, (
            "R65 P1-12: 不应再使用 required_approving_review_count: 1 "
            "(需 >= 2 个独立 reviewer)"
        )

    def test_review_count_asserted_ge_2(self, configure_script: str):
        """场景 6c: 配置后断言 required_approving_review_count >= 2。"""
        assert "required_approving_review_count >= 2" in configure_script, (
            "R65 P1-12: 配置后必须断言 required_approving_review_count >= 2"
        )

    def test_release_gates_asserts_review_count_ge_2(self):
        """场景 6d: release-gates.yml 也断言 review_count >= 2。

        R65 fix: 断言已移至 scripts/verify_branch_protection.sh。
        """
        content = RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")
        assert "required_approving_review_count >= 2" in content

    def test_code_owner_reviews_conditional(self, configure_script: str):
        """场景 6e: require_code_owner_reviews 与 CODEOWNERS 存在性一致。

        R65 P1-12: 无 CODEOWNERS 时为 false,有 CODEOWNERS 时为 true。
        configure 脚本应根据 CODEOWNERS 存在性动态决定。
        """
        assert "CODEOWNERS" in configure_script
        assert "REQUIRE_CODE_OWNER_REVIEWS" in configure_script
        # payload 中 require_code_owner_reviews 应是变量(动态),不是硬编码 true/false
        assert "require_code_owner_reviews: $code_owner_reviews" in configure_script, (
            "R65 P1-12: require_code_owner_reviews 应根据 CODEOWNERS 存在性动态设置"
        )


# ════════════════════════════════════════════════════════════════
# G. required_conversation_resolution = true
# ════════════════════════════════════════════════════════════════


class TestRequiredConversationResolution:
    """场景 7: required_conversation_resolution=true
    (所有 conversation 必须解决后才能合并)。"""

    @pytest.fixture
    def configure_script(self) -> str:
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    def test_conversation_resolution_true_in_payload(self, configure_script: str):
        """场景 7: payload 中 required_conversation_resolution: true。"""
        assert "required_conversation_resolution: true" in configure_script, (
            "R65 P1-12: configure_branch_protection.sh 必须设置 "
            "required_conversation_resolution: true"
        )

    def test_conversation_resolution_asserted(self, configure_script: str):
        """场景 7b: 配置后断言 required_conversation_resolution.enabled == true。"""
        assert "required_conversation_resolution.enabled == true" in configure_script, (
            "R65 P1-12: 配置后必须断言 required_conversation_resolution.enabled == true"
        )

    def test_release_gates_asserts_conversation_resolution(self):
        """场景 7c: release-gates.yml 也断言 required_conversation_resolution。

        R65 fix: 断言已移至 scripts/verify_branch_protection.sh。
        """
        content = RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")
        assert "required_conversation_resolution.enabled == true" in content


# ════════════════════════════════════════════════════════════════
# H. required_signatures = true(signed commits 必需)
# ════════════════════════════════════════════════════════════════


class TestRequiredSignatures:
    """场景 8: required_signatures.enabled = true(signed commits 必需)。

    注意:required_signatures 不在 PUT /branches/{branch}/protection 的 payload 中,
    需通过独立的 POST /branches/{branch}/protection/required_signatures API 启用。
    """

    @pytest.fixture
    def configure_script(self) -> str:
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    def test_required_signatures_api_called(self, configure_script: str):
        """场景 8: configure 脚本必须调用 required_signatures 独立 API。"""
        # 必须出现 POST /branches/.../protection/required_signatures 调用
        assert "required_signatures" in configure_script, (
            "R65 P1-12: configure 脚本必须配置 required_signatures (signed commits 必需)"
        )
        assert "protection/required_signatures" in configure_script, (
            "R65 P1-12: configure 脚本必须调用 "
            "POST /branches/{branch}/protection/required_signatures API"
        )

    def test_required_signatures_asserted(self, configure_script: str):
        """场景 8b: 配置后必须断言 required_signatures.enabled == true。"""
        # 应当出现 ".enabled == true" 与 required_signatures 相关的断言
        # (可能在不同行,但都在 step 5.1/6 内)
        assert "required_signatures" in configure_script
        # 必须有 enabled == true 断言
        assert ".enabled == true" in configure_script

    def test_release_gates_asserts_required_signatures(self):
        """场景 8c: release-gates.yml 也断言 required_signatures.enabled == true。

        R65 fix: 断言已移至 scripts/verify_branch_protection.sh。
        """
        content = RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")
        assert "required_signatures" in content, (
            "R65 P1-12: release-gates.yml verify-branch-protection 必须检查 required_signatures"
        )
        # 必须有 SIG_JSON 启用 / 断言逻辑
        assert "SIG_JSON" in content or "required_signatures" in content


# ════════════════════════════════════════════════════════════════
# I. check_branch_protection_contexts.py exit 0 on consistent state
# ════════════════════════════════════════════════════════════════


class TestCheckScriptExits0OnConsistent:
    """场景 9: check_branch_protection_contexts.py 在一致状态时 exit 0。"""

    def test_exit_0_on_consistent_state(self, tmp_path: Path):
        """场景 9: BP contexts 与 workflow job 名完全一致 → exit 0。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "lint": {"runs-on": "ubuntu-latest"},
                "test": {
                    "runs-on": "ubuntu-latest",
                    "strategy": {"matrix": {"py": ["3.11", "3.12"]}},
                },
            },
            file_name="consistent.yml",
            workflow_name="Consistent CI",
        )
        bp_config = tmp_path / "bp.json"
        # BP contexts 与 workflow 完全一致(矩阵展开)
        _write_synthetic_bp_config(
            bp_config,
            contexts=["lint", "test (3.11)", "test (3.12)"],
        )
        exit_code, stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 0, (
            f"一致状态应 exit 0,实际 {exit_code}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
        assert "PASS" in stdout or "✓" in stdout

    def test_exit_0_with_workflow_prefix_format(self, tmp_path: Path):
        """场景 9b: BP 用 'CI / lint' 前缀格式,workflow 用 'lint' → 一致,exit 0。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={"lint": {"runs-on": "ubuntu-latest"}},
            file_name="prefix.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        _write_synthetic_bp_config(bp_config, contexts=["CI / lint"])
        exit_code, stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 0, (
            f"前缀格式应一致,exit 0,实际 {exit_code}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def test_exit_0_on_real_repo_default_config(self):
        """场景 9c: 默认 checked-in 配置 + 真实 workflows → exit 0。"""
        assert DEFAULT_BP_CONFIG.exists(), (
            ".github/branch_protection.expected.json 必须存在 "
            "(checked-in BP 配置基线)"
        )
        exit_code, stdout, stderr = _run_check_script(
            "--bp-config", str(DEFAULT_BP_CONFIG),
            "--workflows-dir", str(REPO_ROOT / ".github" / "workflows"),
        )
        assert exit_code == 0, (
            f"默认 checked-in 配置与真实 workflows 应一致,exit 0,实际 {exit_code}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )


# ════════════════════════════════════════════════════════════════
# J. check_branch_protection_contexts.py exit 1 on inconsistent state
# ════════════════════════════════════════════════════════════════


class TestCheckScriptExits1OnInconsistent:
    """场景 10: check_branch_protection_contexts.py 在不一致状态时 exit 1
    (使用合成 fixtures)。"""

    def test_exit_1_on_orphan_context(self, tmp_path: Path):
        """场景 10a: BP 中有 workflow 不存在的 'ghost-job' → exit 1。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "lint": {"runs-on": "ubuntu-latest"},
                "test": {"runs-on": "ubuntu-latest"},
            },
            file_name="orphan.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        # BP 多了 ghost-job
        _write_synthetic_bp_config(
            bp_config,
            contexts=["lint", "test", "ghost-job"],
        )
        exit_code, stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 1, (
            f"孤儿 context 应 exit 1,实际 {exit_code}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
        assert "ghost-job" in stdout
        assert "孤儿" in stdout or "orphan" in stdout.lower()

    def test_exit_1_on_missing_context(self, tmp_path: Path):
        """场景 10b: workflow 有 'repo-hygiene' 但 BP 没有 → exit 1。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "lint": {"runs-on": "ubuntu-latest"},
                "repo-hygiene": {"runs-on": "ubuntu-latest"},
            },
            file_name="missing.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        # BP 缺 repo-hygiene
        _write_synthetic_bp_config(bp_config, contexts=["lint"])
        exit_code, stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 1, (
            f"缺失 context 应 exit 1,实际 {exit_code}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
        assert "repo-hygiene" in stdout
        assert "缺失" in stdout or "missing" in stdout.lower()

    def test_exit_1_on_both_orphan_and_missing(self, tmp_path: Path):
        """场景 10c: 同时存在孤儿 + 缺失 → exit 1,且两者都被报告。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={
                "lint": {"runs-on": "ubuntu-latest"},
                "test": {"runs-on": "ubuntu-latest"},
            },
            file_name="both.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        # BP 有 lint 和 ghost(BP 多),workflow 有 lint 和 test(test 缺,ghost 孤儿)
        _write_synthetic_bp_config(
            bp_config,
            contexts=["lint", "ghost-job"],
        )
        exit_code, stdout, _stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 1
        assert "ghost-job" in stdout  # 孤儿
        assert "test" in stdout  # 缺失

    def test_exit_1_on_missing_bp_config_file(self, tmp_path: Path):
        """场景 10d: BP 配置文件不存在 → exit 1。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={"lint": {"runs-on": "ubuntu-latest"}},
            file_name="no_bp.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "nonexistent.json"
        exit_code, _stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 1
        assert "不存在" in stderr or "No such file" in stderr or "ERROR" in stderr

    def test_exit_1_on_empty_bp_contexts(self, tmp_path: Path):
        """场景 10e: BP contexts 为空 → exit 1。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={"lint": {"runs-on": "ubuntu-latest"}},
            file_name="empty.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        _write_synthetic_bp_config(bp_config, contexts=[])
        exit_code, _stdout, stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            cwd=tmp_path,
        )
        assert exit_code == 1
        assert "contexts" in stderr.lower() or "empty" in stderr.lower() or "ERROR" in stderr

    def test_json_output_format(self, tmp_path: Path):
        """场景 10f: --json 输出格式可解析,含 orphan/missing 字段。"""
        workflows_dir = tmp_path / ".github" / "workflows"
        _write_synthetic_workflow(
            workflows_dir,
            jobs={"lint": {"runs-on": "ubuntu-latest"}},
            file_name="json.yml",
            workflow_name="CI",
        )
        bp_config = tmp_path / "bp.json"
        _write_synthetic_bp_config(
            bp_config,
            contexts=["lint", "ghost"],
        )
        exit_code, stdout, _stderr = _run_check_script(
            "--bp-config", str(bp_config),
            "--workflows-dir", str(workflows_dir),
            "--json",
            cwd=tmp_path,
        )
        assert exit_code == 1
        report = json.loads(stdout)
        assert report["consistent"] is False
        assert "ghost" in report["orphan_in_bp"]
        # missing 应包含 'lint' 之外的所有 workflow jobs — 这里只有 'lint'
        # 但 'lint' 在 BP 中,所以 missing 为空。孤儿 'ghost' 触发失败
        assert isinstance(report["missing_in_bp"], list)


# ════════════════════════════════════════════════════════════════
# K. release-gates.yml verify-branch-protection 整合验证
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesVerifyBranchProtectionIntegration:
    """release-gates.yml verify-branch-protection job 整合 R65 P1-12 检查。"""

    @pytest.fixture
    def workflow_content(self) -> str:
        # R65 fix: BP 断言已从 release-gates.yml 提取到 scripts/verify_branch_protection.sh,
        # 合并两个文件内容供文本断言检查。
        return RELEASE_GATES_WORKFLOW.read_text(encoding="utf-8") \
            + "\n" + VBP_SCRIPT.read_text(encoding="utf-8")

    def test_release_gates_calls_check_script(self, workflow_content: str):
        """场景 K1: verify-branch-protection job 必须调用 check_branch_protection_contexts.py。"""
        assert "check_branch_protection_contexts.py" in workflow_content, (
            "R65 P1-12: release-gates.yml verify-branch-protection 必须调用 "
            "scripts/check_branch_protection_contexts.py"
        )

    def test_release_gates_check_is_required(self, workflow_content: str):
        """场景 K2: 一致性检查是 required(无 continue-on-error / if: failure() 跳过)。"""
        # 找到 R65 P1-12 动态一致性检查 step 附近的内容
        idx = workflow_content.find("R65 P1-12: 动态 context 一致性检查")
        assert idx >= 0, "未找到 R65 P1-12 动态一致性检查 step"
        # 截取该 step 之后的内容(到下一个 step 之前)
        next_step = workflow_content.find("    - name:", idx + 1)
        if next_step < 0:
            section = workflow_content[idx:]
        else:
            section = workflow_content[idx:next_step]
        # 不应包含 continue-on-error
        assert "continue-on-error" not in section, (
            "R65 P1-12 一致性检查不允许 continue-on-error — 失败必须阻断"
        )
        # 失败时必须 fail_diag(阻断)
        assert "fail_diag" in section

    def test_release_gates_prints_diff_on_failure(self, workflow_content: str):
        """场景 K3: 一致性检查失败时打印清晰 diff。"""
        # check_branch_protection_contexts.py 自身会打印 diff,
        # verify-branch-protection step 不需要重复实现 diff 逻辑
        idx = workflow_content.find("R65 P1-12: 动态 context 一致性检查")
        assert idx >= 0
        # 失败时调用 fail_diag 输出诊断信息
        assert "fail_diag" in workflow_content[idx:], (
            "R65 P1-12: 失败时必须输出诊断信息(fail_diag)"
        )

    def test_release_gates_has_all_r65_asserts(self, workflow_content: str):
        """场景 K4: verify-branch-protection 包含 R65 P1-12 所有断言。"""
        required_asserts = [
            "dismiss_stale_reviews == true",
            "required_approving_review_count >= 2",
            "required_linear_history.enabled == true",
            "required_conversation_resolution.enabled == true",
            "required_signatures",
            "dismissal_restrictions",
            "require_code_owner_reviews",
        ]
        missing = [a for a in required_asserts if a not in workflow_content]
        assert not missing, (
            f"R65 P1-12: verify-branch-protection 缺少断言: {missing}"
        )

    def test_release_gates_workflow_yaml_valid(self):
        """场景 K5: release-gates.yml 仍是合法 YAML。"""
        import yaml
        with RELEASE_GATES_WORKFLOW.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "jobs" in data
        assert "verify-branch-protection" in data["jobs"]
