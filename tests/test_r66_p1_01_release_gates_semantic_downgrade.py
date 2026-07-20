"""R66 P1-01: Release Gates 语义降级测试 — push 不签名临时镜像。

审计背景(R66 终审报告 P1-01):
    当前普通 push 会 build/push/sign 新镜像,即使只是新增自审文档。建议将
    PR/push 分为 build-test(可不推送或推送临时 namespace)与 release candidate
    (显式版本/tag/environment)两条路径,避免每个文档提交产生可被误认作候选
    的签名镜像。

R66 P1-01 整改(本次变更):
    sign-image / publish-attestation 的 if: 条件扩展为:
      github.event_name == 'push' && (
        github.ref == 'refs/heads/master' ||
        github.ref == 'refs/heads/main' ||
        startsWith(github.ref, 'refs/tags/v')
      )
    - master/main push: 签名 + 生成 attestation(原行为)
    - release tag (refs/tags/v*) push: 签名 + 生成 attestation(新增,满足 P0-03 tag identity)
    - 其他普通 push / PR: 不签名(语义降级,避免临时镜像被误认作候选)

    release-summary 的聚合逻辑同步更新:
    - RELEASE_TARGET=true (master push) 或 RELEASE_TAG=true (tag push) 时,
      sign-image / publish-attestation 必须 success(不允许 skipped)
    - PR: 允许 skipped(if: 未满足),但 failure/cancelled 仍阻断

测试覆盖矩阵:
    A. sign-image if: 条件覆盖 master push + tag push
    B. publish-attestation if: 条件覆盖 master push + tag push
    C. release-summary 聚合逻辑: tag push 时 sign-image/publish-attestation 不允许 skipped
    D. PR 场景: sign-image/publish-attestation 仍允许 skipped
    E. 非 master 非 tag 的普通 push: sign-image/publish-attestation 不运行(不签名临时镜像)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# 测试环境兼容 — conftest.py 在收集阶段已注入 config/telegram mock
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


# ════════════════════════════════════════════════════════════════
# 辅助函数 / fixtures
# ════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def workflow_yaml():
    """加载 release-gates.yml 并解析为 dict。"""
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_job_if(workflow_yaml: dict, job_name: str) -> str:
    """获取指定 job 的 if: 条件字符串。"""
    job = workflow_yaml["jobs"].get(job_name, {})
    return job.get("if", "")


# ════════════════════════════════════════════════════════════════
# A. sign-image if: 条件覆盖 master push + tag push
# ════════════════════════════════════════════════════════════════


class TestSignImageIfCondition:
    """R66 P1-01: sign-image 的 if: 条件必须覆盖 master push 与 tag push。"""

    def test_sign_image_if_includes_master(self, workflow_yaml):
        """sign-image if: 必须包含 refs/heads/master。"""
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        assert "refs/heads/master" in if_cond, (
            f"sign-image if: 必须包含 refs/heads/master,实际: {if_cond}"
        )

    def test_sign_image_if_includes_main(self, workflow_yaml):
        """sign-image if: 必须包含 refs/heads/main。"""
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        assert "refs/heads/main" in if_cond, (
            f"sign-image if: 必须包含 refs/heads/main,实际: {if_cond}"
        )

    def test_sign_image_if_includes_tag_push(self, workflow_yaml):
        """R66 P1-01: sign-image if: 必须包含 refs/tags/v*(tag push 也要签名)。

        P0-03 要求 tag cosign identity 为 refs/tags/<version>,因此 tag push
        必须触发 sign-image,否则 tag push 产生未签名镜像。
        """
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond, (
            f"R66 P1-01: sign-image if: 必须包含 refs/tags/v*(tag push 也要签名),"
            f"实际: {if_cond}"
        )

    def test_sign_image_if_requires_push_event(self, workflow_yaml):
        """sign-image if: 必须要求 github.event_name == 'push'(不在 PR 上运行)。"""
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        assert "push" in if_cond and "github.event_name" in if_cond, (
            f"sign-image if: 必须要求 github.event_name == 'push',实际: {if_cond}"
        )

    def test_sign_image_if_uses_starts_with_for_tags(self, workflow_yaml):
        """sign-image if: tag 检测应使用 startsWith(github.ref, 'refs/tags/v')。"""
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        assert "startsWith(github.ref, 'refs/tags/v')" in if_cond, (
            f"R66 P1-01: sign-image if: 应使用 startsWith(github.ref, 'refs/tags/v'),"
            f"实际: {if_cond}"
        )


# ════════════════════════════════════════════════════════════════
# B. publish-attestation if: 条件覆盖 master push + tag push
# ════════════════════════════════════════════════════════════════


class TestPublishAttestationIfCondition:
    """R66 P1-01: publish-attestation 的 if: 条件必须覆盖 master push 与 tag push。"""

    def test_publish_attestation_if_includes_master(self, workflow_yaml):
        """publish-attestation if: 必须包含 refs/heads/master。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "refs/heads/master" in if_cond, (
            f"publish-attestation if: 必须包含 refs/heads/master,实际: {if_cond}"
        )

    def test_publish_attestation_if_includes_main(self, workflow_yaml):
        """publish-attestation if: 必须包含 refs/heads/main。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "refs/heads/main" in if_cond, (
            f"publish-attestation if: 必须包含 refs/heads/main,实际: {if_cond}"
        )

    def test_publish_attestation_if_includes_tag_push(self, workflow_yaml):
        """R66 P1-01: publish-attestation if: 必须包含 refs/tags/v*。

        tag push 也必须生成签名 attestation,否则 production-promotion-gate
        无法验证 tag 的 attestation 完整性。
        """
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond, (
            f"R66 P1-01: publish-attestation if: 必须包含 refs/tags/v*,"
            f"实际: {if_cond}"
        )

    def test_publish_attestation_if_requires_push_event(self, workflow_yaml):
        """publish-attestation if: 必须要求 github.event_name == 'push'。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "push" in if_cond and "github.event_name" in if_cond, (
            f"publish-attestation if: 必须要求 github.event_name == 'push',"
            f"实际: {if_cond}"
        )

    def test_publish_attestation_if_uses_starts_with_for_tags(self, workflow_yaml):
        """publish-attestation if: tag 检测应使用 startsWith(github.ref, 'refs/tags/v')。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "startsWith(github.ref, 'refs/tags/v')" in if_cond, (
            f"R66 P1-01: publish-attestation if: 应使用 startsWith(github.ref, 'refs/tags/v'),"
            f"实际: {if_cond}"
        )


# ════════════════════════════════════════════════════════════════
# C. release-summary 聚合逻辑: tag push 时 sign-image/publish-attestation 不允许 skipped
# ════════════════════════════════════════════════════════════════


class TestReleaseSummaryTagPushRequiresSigning:
    """R66 P1-01: release-summary 在 tag push 时要求 sign-image/publish-attestation success。"""

    def test_release_summary_aggregate_step_exists(self, workflow_yaml):
        """release-summary 必须包含 'Verify all required jobs succeeded' 聚合步骤。"""
        release_summary = workflow_yaml["jobs"].get("release-summary", {})
        steps = release_summary.get("steps", [])
        aggregate_step_found = False
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_step_found = True
                break
        assert aggregate_step_found, (
            "release-summary 必须包含 'Verify all required jobs succeeded' 聚合步骤"
        )

    def test_release_summary_aggregate_checks_release_tag(self, workflow_yaml):
        """release-summary 聚合步骤必须检查 RELEASE_TAG (refs/tags/v*)。"""
        release_summary = workflow_yaml["jobs"].get("release-summary", {})
        steps = release_summary.get("steps", [])
        aggregate_run = ""
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_run = step.get("run", "")
                break
        assert "RELEASE_TAG" in aggregate_run, (
            "release-summary 聚合步骤必须计算 RELEASE_TAG 标记"
        )
        assert "refs/tags/v" in aggregate_run, (
            "release-summary 聚合步骤必须检查 refs/tags/v* 前缀"
        )

    def test_release_summary_tag_push_requires_sign_image_success(self, workflow_yaml):
        """R66 P1-01: tag push 时 sign-image 不允许 skipped,必须 success。

        检查聚合步骤的 run 块:sign-image/publish-attestation 的判断逻辑必须
        同时考虑 RELEASE_TARGET=true 或 RELEASE_TAG=true,任一为 true 时
        都要求 conclusion == 'success'。
        """
        release_summary = workflow_yaml["jobs"].get("release-summary", {})
        steps = release_summary.get("steps", [])
        aggregate_run = ""
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_run = step.get("run", "")
                break

        # 必须存在 sign-image / publish-attestation 的特殊判断分支
        assert "sign-image" in aggregate_run, (
            "聚合步骤必须特殊处理 sign-image job"
        )
        assert "publish-attestation" in aggregate_run, (
            "聚合步骤必须特殊处理 publish-attestation job"
        )

        # R66 P1-01 关键:判断条件必须包含 RELEASE_TAG
        # (原 R60 P0-06 仅检查 RELEASE_TARGET,tag push 时允许 skipped,
        #  导致 tag push 产生未签名的镜像;P1-01 修复后 tag push 也要求 success)
        assert "RELEASE_TAG" in aggregate_run, (
            "R66 P1-01: 聚合步骤的 sign-image/publish-attestation 判断必须包含 RELEASE_TAG"
        )

    def test_release_summary_pr_allows_skipped_sign_image(self, workflow_yaml):
        """PR 场景:sign-image/publish-attestation 仍允许 skipped。

        确保语义降级不阻断 PR 合并:PR 上 sign-image/publish-attestation 的 if:
        未满足(skipped),聚合步骤允许 skipped 通过。
        """
        release_summary = workflow_yaml["jobs"].get("release-summary", {})
        steps = release_summary.get("steps", [])
        aggregate_run = ""
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_run = step.get("run", "")
                break

        # PR 场景允许 skipped
        assert "skipped" in aggregate_run, (
            "聚合步骤必须允许 PR 场景下 sign-image/publish-attestation skipped"
        )


# ════════════════════════════════════════════════════════════════
# D. PR 场景: sign-image/publish-attestation 仍允许 skipped
# ════════════════════════════════════════════════════════════════


class TestPRScenarioAllowsSkipped:
    """R66 P1-01: PR 场景 sign-image/publish-attestation 不运行(if: 未满足),允许 skipped。"""

    def test_sign_image_if_excludes_pull_request_event(self, workflow_yaml):
        """sign-image if: 必须要求 github.event_name == 'push',排除 pull_request 事件。"""
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        # 必须显式检查 event_name == 'push',而非 pull_request
        assert "github.event_name == 'push'" in if_cond, (
            f"sign-image if: 必须显式要求 github.event_name == 'push',"
            f"实际: {if_cond}"
        )
        # 不应在 if: 中出现 pull_request 关键字(否则可能在 PR 上运行)
        assert "pull_request" not in if_cond, (
            f"sign-image if: 不应包含 pull_request 关键字(不应在 PR 上运行),"
            f"实际: {if_cond}"
        )

    def test_publish_attestation_if_excludes_pull_request_event(self, workflow_yaml):
        """publish-attestation if: 必须要求 github.event_name == 'push',排除 pull_request。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "github.event_name == 'push'" in if_cond, (
            f"publish-attestation if: 必须显式要求 github.event_name == 'push',"
            f"实际: {if_cond}"
        )
        assert "pull_request" not in if_cond, (
            f"publish-attestation if: 不应包含 pull_request 关键字,"
            f"实际: {if_cond}"
        )


# ════════════════════════════════════════════════════════════════
# E. 非 master 非 tag 的普通 push: sign-image/publish-attestation 不运行
# ════════════════════════════════════════════════════════════════


class TestNonMasterNonTagPushNoSigning:
    """R66 P1-01: 非 master 非 tag 的普通 push(如 chore/* 分支)不签名临时镜像。

    场景:push 到 chore/r66-remediation 分支(非 master/main,非 refs/tags/v*):
      - sign-image if: 不满足 → skipped(不签名)
      - publish-attestation if: 不满足 → skipped(不生成 attestation)
    这是 P1-01 的核心:避免每个文档提交产生可被误认作候选的签名镜像。
    """

    def test_sign_image_if_does_not_match_arbitrary_branch(self, workflow_yaml):
        """sign-image if: 不应匹配任意分支 push(只匹配 master/main + tag)。

        if: 条件中不应出现通配任意分支的模式(如 github.ref != 'refs/tags/*'
        或仅 github.event_name == 'push' 而无 ref 限制)。
        """
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        # 必须显式列出 master/main/tag 条件,而非通配任意 push
        assert "refs/heads/master" in if_cond, (
            "sign-image if: 必须显式列出 refs/heads/master(不能通配任意分支)"
        )
        assert "refs/heads/main" in if_cond, (
            "sign-image if: 必须显式列出 refs/heads/main(不能通配任意分支)"
        )
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond, (
            "sign-image if: 必须显式列出 refs/tags/v*(不能通配任意分支)"
        )

    def test_publish_attestation_if_does_not_match_arbitrary_branch(self, workflow_yaml):
        """publish-attestation if: 不应匹配任意分支 push(只匹配 master/main + tag)。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "refs/heads/master" in if_cond
        assert "refs/heads/main" in if_cond
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond

    def test_sign_image_if_condition_is_strict(self, workflow_yaml):
        """sign-image if: 条件必须是 AND 关系(event=push AND ref∈{master,main,tag})。

        检查 if: 字符串结构:event_name == 'push' 与 ref 检查必须用 && 连接,
        而非 || (否则 PR 也会匹配)。
        """
        if_cond = _get_job_if(workflow_yaml, "sign-image")
        # event_name == 'push' 与 ref 检查之间必须是 && (AND)
        assert "&&" in if_cond, (
            f"sign-image if: 必须使用 && 连接 event_name 与 ref 检查(AND 关系),"
            f"实际: {if_cond}"
        )
        # ref 检查之间用 || (OR:master OR main OR tag)
        assert "||" in if_cond, (
            f"sign-image if: ref 检查之间必须用 || 连接(OR 关系),实际: {if_cond}"
        )

    def test_publish_attestation_if_condition_is_strict(self, workflow_yaml):
        """publish-attestation if: 条件必须是 AND 关系(event=push AND ref∈{master,main,tag})。"""
        if_cond = _get_job_if(workflow_yaml, "publish-attestation")
        assert "&&" in if_cond, (
            f"publish-attestation if: 必须使用 && 连接 event_name 与 ref 检查,"
            f"实际: {if_cond}"
        )
        assert "||" in if_cond, (
            f"publish-attestation if: ref 检查之间必须用 || 连接,实际: {if_cond}"
        )


# ════════════════════════════════════════════════════════════════
# F. production-evidence / production-promotion-gate 依赖链完整性
# ════════════════════════════════════════════════════════════════


class TestProductionChainDependsOnSigning:
    """R66 P1-01: production-evidence 与 production-promotion-gate 依赖链完整性。

    由于 sign-image/publish-attestation 现在在 tag push 上也运行,
    production-evidence (needs: [docker-build, sbom, sign-image, publish-attestation])
    将正确等待 tag push 上的签名完成。production-promotion-gate (tag-only)
    通过 needs: [production-evidence, crdb-ru-72h-attribution-gate] 间接依赖签名。
    """

    def test_production_evidence_needs_sign_image(self, workflow_yaml):
        """production-evidence 必须依赖 sign-image(确保签名完成后才生成证据)。"""
        pe = workflow_yaml["jobs"].get("production-evidence", {})
        needs = pe.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "sign-image" in needs, (
            "production-evidence 必须依赖 sign-image(签名完成才生成证据)"
        )

    def test_production_evidence_needs_publish_attestation(self, workflow_yaml):
        """production-evidence 必须依赖 publish-attestation。"""
        pe = workflow_yaml["jobs"].get("production-evidence", {})
        needs = pe.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "publish-attestation" in needs, (
            "production-evidence 必须依赖 publish-attestation"
        )

    def test_production_promotion_gate_needs_production_evidence(self, workflow_yaml):
        """production-promotion-gate 必须依赖 production-evidence(间接依赖签名)。"""
        ppg = workflow_yaml["jobs"].get("production-promotion-gate", {})
        needs = ppg.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "production-evidence" in needs, (
            "production-promotion-gate 必须依赖 production-evidence(间接依赖签名)"
        )

    def test_production_promotion_gate_if_tag_only(self, workflow_yaml):
        """production-promotion-gate if: 必须仅在 tag push 上运行。"""
        if_cond = _get_job_if(workflow_yaml, "production-promotion-gate")
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond, (
            f"production-promotion-gate if: 必须仅在 tag push 上运行,实际: {if_cond}"
        )
