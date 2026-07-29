"""R67 P1-14: Push candidate 限制 测试。

R67 审计背景:
    P1-14 · 普通 master push 不应生产正式候选
    "仅 release candidate/tag 流程推送并签名可晋级镜像;普通 push 使用临时
    namespace 和短 retention,避免每次修复 commit 产生被误用的签名镜像。"

R67 整改 / R70 P0-10 / R71 P0-10 命名空间分离:
    1. rc-v* tag → 生产命名空间 ghcr.io/<repo> + 签名(可晋级)
    2. production-v* tag → 生产命名空间(defense-in-depth, job-if + guard 拦截)
    3. master/main push → staging 命名空间 ghcr.io/<repo>-staging + 30 天 retention(不签名)
    4. PR/非 master 分支 push → 临时命名空间 ghcr.io/<repo>-ci + 短 retention + 不签名

测试覆盖:
    A. release-gates.yml Compute image tag 步骤命名空间选择逻辑
    B. sign-image job 条件限制(仅 rc-v* tag 签名)
    C. is_production_namespace / retention_days 输出正确性
    D. 临时命名空间 -ci 后缀
    E. P1-14 审计覆盖矩阵
"""
from __future__ import annotations

import sys
import yaml
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


@pytest.fixture(scope="module")
def workflow_yaml() -> dict:
    """加载 release-gates.yml 工作流 YAML。"""
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def compute_image_tag_step(workflow_yaml: dict) -> dict:
    """获取 docker-build job 中的 Compute image tag 步骤。"""
    steps = workflow_yaml["jobs"]["docker-build"]["steps"]
    for step in steps:
        if step.get("name") == "Compute image tag":
            return step
    pytest.fail("Compute image tag 步骤不存在")


@pytest.fixture(scope="module")
def compute_image_tag_script(compute_image_tag_step: dict) -> str:
    """获取 Compute image tag 步骤的 run 脚本。"""
    return compute_image_tag_step["run"]


# ════════════════════════════════════════════════════════════════
# A. 命名空间选择逻辑
# ════════════════════════════════════════════════════════════════

class TestNamespaceSelectionLogic:
    """R67 P1-14 / R70 P0-10: 命名空间选择逻辑验证。"""

    def test_repo_variable_defined(self, compute_image_tag_script: str):
        """脚本中定义 REPO 变量(github.repository)。"""
        assert 'REPO="${{ github.repository }}"' in compute_image_tag_script

    def test_github_ref_variable_defined(self, compute_image_tag_script: str):
        """脚本中定义 GITHUB_REF 变量。"""
        assert 'GITHUB_REF="${{ github.ref }}"' in compute_image_tag_script

    def test_release_tag_uses_production_namespace(self, compute_image_tag_script: str):
        """R70 P0-10: rc-v* tag 使用生产命名空间 ghcr.io/<repo>。"""
        assert 'refs/tags/rc-v*' in compute_image_tag_script
        assert 'IMAGE_NAME="ghcr.io/${REPO}"' in compute_image_tag_script

    def test_master_push_uses_staging_namespace(self, compute_image_tag_script: str):
        """R70 P0-10: master/main push 使用 staging 命名空间(不晋级)。"""
        assert 'refs/heads/master' in compute_image_tag_script
        assert 'refs/heads/main' in compute_image_tag_script
        assert 'ghcr.io/${REPO}-staging' in compute_image_tag_script

    def test_non_master_push_uses_temporary_namespace(self, compute_image_tag_script: str):
        """PR/非 master 分支 push 使用临时命名空间 ghcr.io/<repo>-ci。"""
        assert 'ghcr.io/${REPO}-ci' in compute_image_tag_script

    def test_temporary_namespace_has_ci_suffix(self, compute_image_tag_script: str):
        """临时命名空间必须带 -ci 后缀。"""
        assert 'IMAGE_NAME="ghcr.io/${REPO}-ci"' in compute_image_tag_script


# ════════════════════════════════════════════════════════════════
# B. is_production_namespace / retention_days 输出
# ════════════════════════════════════════════════════════════════

class TestNamespaceOutputs:
    """R67 P1-14 / R70 P0-10: is_production_namespace / retention_days 输出验证。"""

    def test_is_production_namespace_output_defined(self, compute_image_tag_script: str):
        """is_production_namespace 输出已定义。"""
        assert 'is_production_namespace' in compute_image_tag_script

    def test_retention_days_output_defined(self, compute_image_tag_script: str):
        """retention_days 输出已定义。"""
        assert 'retention_days' in compute_image_tag_script

    def test_release_tag_is_production_namespace(self, compute_image_tag_script: str):
        """rc-v* tag → IS_PRODUCTION_NAMESPACE=true。"""
        assert 'IS_PRODUCTION_NAMESPACE="true"' in compute_image_tag_script

    def test_master_push_is_staging_namespace(self, compute_image_tag_script: str):
        """R70 P0-10: master/main push → IS_PRODUCTION_NAMESPACE=false(staging,不晋级)。"""
        assert 'IS_PRODUCTION_NAMESPACE="false"' in compute_image_tag_script

    def test_non_master_push_not_production_namespace(self, compute_image_tag_script: str):
        """PR/非 master push → IS_PRODUCTION_NAMESPACE=false。"""
        assert 'IS_PRODUCTION_NAMESPACE="false"' in compute_image_tag_script

    def test_release_tag_retention_permanent(self, compute_image_tag_script: str):
        """rc-v* tag → RETENTION_DAYS=permanent。"""
        assert 'RETENTION_DAYS="permanent"' in compute_image_tag_script

    def test_master_push_retention_30_days(self, compute_image_tag_script: str):
        """R70 P0-10: master/main push → RETENTION_DAYS=30(staging retention)。"""
        assert 'RETENTION_DAYS="30"' in compute_image_tag_script

    def test_non_master_push_retention_7_days(self, compute_image_tag_script: str):
        """PR/非 master push → RETENTION_DAYS=7(短 retention)。"""
        assert 'RETENTION_DAYS="7"' in compute_image_tag_script

    def test_github_output_emits_is_production_namespace(
        self, compute_image_tag_script: str
    ):
        """$GITHUB_OUTPUT 中输出 is_production_namespace。"""
        assert 'echo "is_production_namespace=${IS_PRODUCTION_NAMESPACE}" >> "$GITHUB_OUTPUT"' in compute_image_tag_script

    def test_github_output_emits_retention_days(
        self, compute_image_tag_script: str
    ):
        """$GITHUB_OUTPUT 中输出 retention_days。"""
        assert 'echo "retention_days=${RETENTION_DAYS}" >> "$GITHUB_OUTPUT"' in compute_image_tag_script


# ════════════════════════════════════════════════════════════════
# C. sign-image job 条件限制
# ════════════════════════════════════════════════════════════════

class TestSignImageCondition:
    """R67 P1-14 / R70 P0-10: sign-image job 条件限制验证。

    签名 = 候选身份标记。仅 rc-v* tag 才签名(RC candidate),
    master/main push 与 PR/非 master 分支 push 不签名。
    """

    def test_sign_image_job_exists(self, workflow_yaml: dict):
        """sign-image job 存在。"""
        assert "sign-image" in workflow_yaml["jobs"]

    def test_sign_image_condition_includes_rc_tag(self, workflow_yaml: dict):
        """R70 P0-10: sign-image 条件包含 rc-v*(RC candidate tag)。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "rc-v" in if_cond

    def test_sign_image_condition_includes_tag_ref(self, workflow_yaml: dict):
        """R70 P0-10: sign-image 条件包含 refs/tags/rc-v*。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "refs/tags/rc-v" in if_cond or "startsWith(github.ref, 'refs/tags/rc-v'" in if_cond

    def test_sign_image_condition_requires_push_event(self, workflow_yaml: dict):
        """sign-image 条件要求 push 事件(排除 PR 触发)。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "push" in if_cond

    def test_sign_image_excludes_pr(self, workflow_yaml: dict):
        """sign-image 在 PR 触发时不运行(if 条件中 push 是必要条件)。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "github.event_name == 'push'" in if_cond

    def test_sign_image_excludes_master_main(self, workflow_yaml: dict):
        """R70 P0-10: sign-image 在 master/main push 时不运行(只匹配 rc-v* tag)。

        master push 只产 staging 命名空间镜像,不签名。
        """
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "refs/heads/master" not in if_cond
        assert "refs/heads/main" not in if_cond

    def test_sign_image_uses_starts_with_for_rc_tags(self, workflow_yaml: dict):
        """R70 P0-10: sign-image 使用 startsWith(github.ref, 'refs/tags/rc-v')。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "startsWith(github.ref, 'refs/tags/rc-v')" in if_cond


# ════════════════════════════════════════════════════════════════
# D. 整体语义验证 — 矩阵覆盖
# ════════════════════════════════════════════════════════════════

class TestPushCandidateMatrix:
    """R67 P1-14 / R70 P0-10: push candidate 矩阵覆盖验证。

    矩阵:
        | 场景                     | namespace              | sign | production_namespace | retention |
        |--------------------------|------------------------|------|----------------------|-----------|
        | rc-v* tag                | ghcr.io/<repo>         | ✓    | true                 | permanent |
        | production-v* tag        | ghcr.io/<repo> (defensive) | — | true                 | permanent |
        | master/main push         | ghcr.io/<repo>-staging | ✗    | false                | 30        |
        | PR                       | ghcr.io/<repo>-ci      | ✗    | false                | 7         |
        | 非 master 分支 push      | ghcr.io/<repo>-ci      | ✗    | false                | 7         |
    """

    def test_release_tag_matrix(self, compute_image_tag_script: str):
        """rc-v* tag → 生产命名空间 + permanent retention。"""
        assert 'refs/tags/rc-v*' in compute_image_tag_script
        assert 'ghcr.io/${REPO}"' in compute_image_tag_script
        assert 'IS_PRODUCTION_NAMESPACE="true"' in compute_image_tag_script
        assert 'RETENTION_DAYS="permanent"' in compute_image_tag_script

    def test_master_push_matrix(self, compute_image_tag_script: str):
        """R70 P0-10: master/main push → staging 命名空间 + 30 天 retention。"""
        assert 'refs/heads/master' in compute_image_tag_script
        assert 'refs/heads/main' in compute_image_tag_script
        assert 'ghcr.io/${REPO}-staging' in compute_image_tag_script
        assert 'RETENTION_DAYS="30"' in compute_image_tag_script

    def test_non_master_push_matrix(self, compute_image_tag_script: str):
        """非 master push → 临时命名空间 + 7 天 retention。"""
        assert 'ghcr.io/${REPO}-ci' in compute_image_tag_script
        assert 'IS_PRODUCTION_NAMESPACE="false"' in compute_image_tag_script
        assert 'RETENTION_DAYS="7"' in compute_image_tag_script

    def test_temporary_namespace_not_signed(self, workflow_yaml: dict):
        """R70 P0-10: 临时命名空间镜像不被签名(sign-image if 仅匹配 rc-v* tag)。"""
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        assert "rc-v" in if_cond
        assert "refs/heads/master" not in if_cond
        assert "refs/heads/main" not in if_cond


# ════════════════════════════════════════════════════════════════
# E. P1-14 审计覆盖矩阵
# ════════════════════════════════════════════════════════════════

class TestP1_14AuditCoverage:
    """R67 P1-14: 审计覆盖矩阵验证。"""

    def test_audit_requirement_only_release_candidate_pushes_signable_image(
        self, compute_image_tag_script: str, workflow_yaml: dict
    ):
        """审计要求 1:仅 rc-v* tag 推送可晋级镜像到生产命名空间。"""
        # 1. 生产命名空间仅 rc-v* tag 使用
        assert 'refs/tags/rc-v*' in compute_image_tag_script
        # 2. master/main 使用 staging 命名空间(不晋级)
        assert 'ghcr.io/${REPO}-staging' in compute_image_tag_script
        # 3. 其他场景使用 -ci 临时命名空间
        assert 'ghcr.io/${REPO}-ci' in compute_image_tag_script

    def test_audit_requirement_normal_push_uses_temporary_namespace(
        self, compute_image_tag_script: str
    ):
        """审计要求 2:普通 push 使用临时 namespace。"""
        assert 'ghcr.io/${REPO}-ci' in compute_image_tag_script

    def test_audit_requirement_short_retention(
        self, compute_image_tag_script: str
    ):
        """审计要求 3:普通 push 使用短 retention。"""
        assert 'RETENTION_DAYS="7"' in compute_image_tag_script

    def test_audit_requirement_avoid_misused_signed_image(
        self, workflow_yaml: dict
    ):
        """审计要求 4:避免每次修复 commit 产生被误用的签名镜像。

        R70 P0-10: sign-image 条件限制为仅 rc-v* tag 签名,
        master/main push 与普通修复 commit(非 master 分支 push)不签名。
        """
        if_cond = workflow_yaml["jobs"]["sign-image"]["if"]
        # 签名条件必须包含 push 事件限制
        assert "github.event_name == 'push'" in if_cond
        # 必须限制为 rc-v* tag
        assert "rc-v" in if_cond
        # 不应包含 master/main push(已废弃)
        assert "refs/heads/master" not in if_cond
        assert "refs/heads/main" not in if_cond

    def test_p1_14_comment_marker_exists(self, compute_image_tag_script: str):
        """P1-14 整改标记存在(便于审计追溯)。"""
        assert "R67 P1-14" in compute_image_tag_script

    def test_p1_14_comment_in_workflow_file(self):
        """P1-14 整改标记存在于 workflow 文件中。"""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "R67 P1-14" in content

    def test_image_tag_still_uses_ci_prefix(self, compute_image_tag_script: str):
        """R73 P1-04: image tag 使用动态前缀模板(rc/production/dev/ci-{sha}-{run_id})。"""
        assert 'IMAGE_TAG="${IMAGE_TAG_PREFIX}-${{ github.sha }}-${{ github.run_id }}"' in compute_image_tag_script

    def test_image_ref_output_preserved(self, compute_image_tag_script: str):
        """image_ref 输出保留(向后兼容)。"""
        assert 'echo "image_ref=${IMAGE_NAME}:${IMAGE_TAG}" >> "$GITHUB_OUTPUT"' in compute_image_tag_script

    def test_name_output_preserved(self, compute_image_tag_script: str):
        """name 输出保留(向后兼容)。"""
        assert 'echo "name=${IMAGE_NAME}" >> "$GITHUB_OUTPUT"' in compute_image_tag_script

    def test_tag_output_preserved(self, compute_image_tag_script: str):
        """tag 输出保留(向后兼容)。"""
        assert 'echo "tag=${IMAGE_TAG}" >> "$GITHUB_OUTPUT"' in compute_image_tag_script
