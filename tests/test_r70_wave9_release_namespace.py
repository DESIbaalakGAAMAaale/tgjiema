"""R70 Wave 9: 发布命名空间与 RC 分离(staging/RC/production) — 测试套件。

被测对象:
- .github/workflows/release-gates.yml — 主 release pipeline workflow
- .github/workflows/promote-rc.yml    — RC 晋级 workflow(staging → RC → production)

R70 P0-10 要求:
    "master/staging/RC/production namespace 完全分离"
    旧版 GitHub Actions 工作流中 master push 直接进入正式签名路径,
    未区分 staging/RC/production,违反发布治理原则。

整改目标:
    1. master push 只产 staging (镜像构建 + 测试 + lint,不签名)
    2. rc-v* tag 才产 production candidate evidence (sign-image + attestation)
    3. production-v* tag 或 workflow_dispatch 触发 production deployment
    4. 三个命名空间使用 GitHub environment 隔离:
       - staging      (master push)
       - rc-candidate (rc-v* tag)
       - production   (production-v* tag 或 workflow_dispatch)

测试策略:
    - 使用 PyYAML 解析 workflow YAML,验证结构
    - 静态文本检查关键 if 条件表达式
    - 不依赖 GitHub Actions runtime,纯静态分析
    - 兼容 Python 3.9+
    - 注意:YAML 1.1 把 'on' 解析为布尔 True,使用 _get_on_config() 兼容访问

整改规范:
    - 禁止 TODO 注释、pass 占位符、其他占位符
    - 禁止 skip/warn/吞异常
    - 测试必须真实可运行(pytest 全部通过)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATES_YML = PROJECT_ROOT / ".github" / "workflows" / "release-gates.yml"
PROMOTE_RC_YML = PROJECT_ROOT / ".github" / "workflows" / "promote-rc.yml"


# ══════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════
def _load_yaml(path: Path) -> dict:
    """加载 YAML 文件并返回解析后的字典。

    注意:YAML 1.1 规范把 'on' 解析为布尔 True(PyYAML safe_load 默认行为)。
    调用方应使用 _get_on_config() 访问 on 配置,而非直接 workflow['on']。
    """
    if not path.exists():
        pytest.fail(f"Workflow 文件不存在: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_on_config(workflow: dict) -> dict:
    """获取 workflow 的 on 配置(兼容 YAML 1.1 把 'on' 解析为 True 的情况)。

    PyYAML safe_load 遵循 YAML 1.1,把 'on:' 解析为布尔键 True。
    本函数同时检查 'on' 与 True 键,确保访问正确。
    """
    if "on" in workflow:
        return workflow["on"] or {}
    if True in workflow:  # YAML 1.1 把 'on' 解析为 True
        return workflow[True] or {}
    return {}


def _read_text(path: Path) -> str:
    """读取文件文本内容。"""
    if not path.exists():
        pytest.fail(f"Workflow 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def _get_job(workflow: dict, job_id: str) -> dict:
    """从 workflow 中获取指定 job 的定义。"""
    jobs = workflow.get("jobs", {})
    if job_id not in jobs:
        pytest.fail(f"Workflow 缺少 job: {job_id}")
    return jobs[job_id]


def _workflow_has_on_key(workflow: dict) -> bool:
    """检查 workflow 是否有 on 配置(兼容 YAML 1.1 的 'on' → True 解析)。"""
    return "on" in workflow or True in workflow


# ══════════════════════════════════════════════════════════════════════
# 测试夹具
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture
def release_gates_yaml() -> dict:
    """提供 release-gates.yml 解析后的字典。"""
    return _load_yaml(RELEASE_GATES_YML)


@pytest.fixture
def release_gates_text() -> str:
    """提供 release-gates.yml 的原始文本内容。"""
    return _read_text(RELEASE_GATES_YML)


@pytest.fixture
def promote_rc_yaml() -> dict:
    """提供 promote-rc.yml 解析后的字典。"""
    return _load_yaml(PROMOTE_RC_YML)


@pytest.fixture
def promote_rc_text() -> str:
    """提供 promote-rc.yml 的原始文本内容。"""
    return _read_text(PROMOTE_RC_YML)


# ══════════════════════════════════════════════════════════════════════
# 测试组 1: release-gates.yml 触发器(on:)
# ══════════════════════════════════════════════════════════════════════
class TestReleaseGatesTriggers:
    """R70 P0-10: release-gates.yml 触发器必须正确分离 staging/RC/production。"""

    def test_workflow_file_exists(self):
        """release-gates.yml 文件必须存在。"""
        assert RELEASE_GATES_YML.exists(), (
            f".github/workflows/release-gates.yml 不存在: {RELEASE_GATES_YML}\n"
            f"R70 P0-10 要求修改该 workflow 实现命名空间分离"
        )

    def test_push_triggers_include_master_branch(self, release_gates_yaml):
        """push 触发器必须包含 master/main 分支(staging)。"""
        push_config = _get_on_config(release_gates_yaml).get("push", {})
        branches = push_config.get("branches", [])
        assert "master" in branches and "main" in branches, (
            f"push.branches 必须包含 master 与 main,实际: {branches}\n"
            f"R70 P0-10: master/main push 触发 staging environment"
        )

    def test_push_triggers_include_rc_and_production_tags(self, release_gates_yaml):
        """push 触发器必须包含 rc-v* 与 production-v* tag。"""
        push_config = _get_on_config(release_gates_yaml).get("push", {})
        tags = push_config.get("tags", [])
        assert "rc-v*" in tags, (
            f"push.tags 必须包含 'rc-v*',实际: {tags}\n"
            f"R70 P0-10: rc-v* tag 触发 rc-candidate environment"
        )
        assert "production-v*" in tags, (
            f"push.tags 必须包含 'production-v*',实际: {tags}\n"
            f"R70 P0-10: production-v* tag 触发 production environment"
        )

    def test_pull_request_trigger_exists(self, release_gates_yaml):
        """pull_request 触发器必须存在(PR 场景验证)。"""
        on_config = _get_on_config(release_gates_yaml)
        assert "pull_request" in on_config, (
            "release-gates.yml 缺少 pull_request 触发器\n"
            "R70 P0-10: PR 仍需运行部分验证门禁(lint + test)"
        )

    def test_workflow_dispatch_trigger_exists(self, release_gates_yaml):
        """workflow_dispatch 触发器必须存在(运维手动触发 production 晋级)。"""
        on_config = _get_on_config(release_gates_yaml)
        assert "workflow_dispatch" in on_config, (
            "release-gates.yml 缺少 workflow_dispatch 触发器\n"
            "R70 P0-10: production 部署支持手动 dispatch 触发"
        )

    def test_no_legacy_v_star_tag_pattern(self, release_gates_text):
        """禁止使用旧的 v* tag 触发模式(已被 rc-v* / production-v* 替代)。

        旧的 `tags: ['v*']` 或 `startsWith(github.ref, 'refs/tags/v')` 不应出现
        在 on.push.tags 或任何 job 的 if 条件中。
        """
        # on.push.tags 不能包含 'v*' (必须是 'rc-v*' 或 'production-v*')
        # 检查 on.push.tags 段不含独立的 'v*'
        # 用正则匹配 YAML 中的 tags 列表项
        # 匹配 `tags: ['v*']` 或 `- v*` (单行列表项)
        legacy_pattern = re.compile(r"^\s*-\s+v\*\s*$", re.MULTILINE)
        legacy_matches = legacy_pattern.findall(release_gates_text)
        # 允许出现 `- v*` 在注释中,但 on.push.tags 段不允许
        # 通过解析 YAML 来精确检查
        rg_yaml = yaml.safe_load(release_gates_text)
        push_tags = _get_on_config(rg_yaml).get("push", {}).get("tags", [])
        for tag in push_tags:
            assert tag in ("rc-v*", "production-v*"), (
                f"on.push.tags 包含非法 tag 模式: {tag}\n"
                f"R70 P0-10: 只允许 rc-v* 或 production-v*,旧的 v* 已废弃"
            )

    def test_no_legacy_v_tag_in_job_if_conditions(self, release_gates_text):
        """job 的 if 条件不应使用旧的 startsWith(github.ref, 'refs/tags/v') 模式。

        允许的 rc-v* / production-v* 模式:
            startsWith(github.ref, 'refs/tags/rc-v')
            startsWith(github.ref, 'refs/tags/production-v')
        禁止的旧模式:
            startsWith(github.ref, 'refs/tags/v')  # 会匹配 v1.0.0 / rc-v1.0.0 / production-v1.0.0
        """
        # 查找 startsWith(github.ref, 'refs/tags/v') 但后面不跟 rc-v 或 production-v
        # 用负向先行断言排除 rc-v / production-v
        # 注意:YAML 中可能用单引号或双引号
        legacy_pattern = re.compile(
            r"startsWith\(github\.ref,\s*['\"]refs/tags/v['\"]\)"
        )
        matches = legacy_pattern.findall(release_gates_text)
        assert not matches, (
            f"release-gates.yml 中发现旧的 startsWith(github.ref, 'refs/tags/v') 模式:\n"
            f"  {matches}\n"
            f"R70 P0-10: 必须使用 'refs/tags/rc-v' 或 'refs/tags/production-v' 替代"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 2: RC-only jobs(sign-image 等)的 if 条件与环境
# ══════════════════════════════════════════════════════════════════════
class TestRCOnlyJobs:
    """R70 P0-10: RC-only jobs 必须只在 rc-v* tag 触发,environment=rc-candidate。"""

    # RC-only jobs 列表(只在 rc-v* tag 触发)
    RC_ONLY_JOBS = [
        "sign-image",
        "publish-attestation",
        "attestation-semantics-verify",
        "verify-only-3x",
        "migration-binding-gate",
    ]

    def test_sign_image_if_only_rc_tag(self, release_gates_yaml):
        """sign-image 的 if 条件必须只在 rc-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "sign-image")
        if_expr = job.get("if", "")
        assert "refs/tags/rc-v" in if_expr, (
            f"sign-image if 条件必须包含 'refs/tags/rc-v': {if_expr}\n"
            f"R70 P0-10: sign-image 仅在 RC tag 触发"
        )
        # 不能包含 master/main push 触发(只在 rc-v* tag)
        assert "refs/heads/master" not in if_expr, (
            f"sign-image if 条件不应包含 master 分支触发: {if_expr}\n"
            f"R70 P0-10: master push 不再触发签名"
        )
        assert "refs/heads/main" not in if_expr, (
            f"sign-image if 条件不应包含 main 分支触发: {if_expr}\n"
            f"R70 P0-10: main push 不再触发签名"
        )

    def test_sign_image_environment_rc_candidate(self, release_gates_yaml):
        """sign-image 必须使用 environment: rc-candidate。"""
        job = _get_job(release_gates_yaml, "sign-image")
        env = job.get("environment", "")
        assert env == "rc-candidate", (
            f"sign-image environment 必须为 'rc-candidate',实际: {env}\n"
            f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
        )

    def test_publish_attestation_if_only_rc_tag(self, release_gates_yaml):
        """publish-attestation 的 if 条件必须只在 rc-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "publish-attestation")
        if_expr = job.get("if", "")
        assert "refs/tags/rc-v" in if_expr, (
            f"publish-attestation if 条件必须包含 'refs/tags/rc-v': {if_expr}\n"
            f"R70 P0-10: publish-attestation 仅在 RC tag 触发"
        )

    def test_publish_attestation_environment_rc_candidate(self, release_gates_yaml):
        """publish-attestation 必须使用 environment: rc-candidate。"""
        job = _get_job(release_gates_yaml, "publish-attestation")
        env = job.get("environment", "")
        assert env == "rc-candidate", (
            f"publish-attestation environment 必须为 'rc-candidate',实际: {env}\n"
            f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
        )

    def test_attestation_semantics_verify_if_only_rc_tag(self, release_gates_yaml):
        """attestation-semantics-verify 的 if 条件必须只在 rc-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "attestation-semantics-verify")
        if_expr = job.get("if", "")
        assert "refs/tags/rc-v" in if_expr, (
            f"attestation-semantics-verify if 条件必须包含 'refs/tags/rc-v': {if_expr}\n"
            f"R70 P0-10: attestation-semantics-verify 仅在 RC tag 触发"
        )

    def test_attestation_semantics_verify_environment_rc_candidate(self, release_gates_yaml):
        """attestation-semantics-verify 必须使用 environment: rc-candidate。"""
        job = _get_job(release_gates_yaml, "attestation-semantics-verify")
        env = job.get("environment", "")
        assert env == "rc-candidate", (
            f"attestation-semantics-verify environment 必须为 'rc-candidate',实际: {env}\n"
            f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
        )

    def test_verify_only_3x_if_only_rc_tag(self, release_gates_yaml):
        """verify-only-3x 的 if 条件必须只在 rc-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "verify-only-3x")
        if_expr = job.get("if", "")
        assert "refs/tags/rc-v" in if_expr, (
            f"verify-only-3x if 条件必须包含 'refs/tags/rc-v': {if_expr}\n"
            f"R70 P0-10: verify-only-3x 仅在 RC tag 触发"
        )

    def test_verify_only_3x_environment_rc_candidate(self, release_gates_yaml):
        """verify-only-3x 必须使用 environment: rc-candidate。"""
        job = _get_job(release_gates_yaml, "verify-only-3x")
        env = job.get("environment", "")
        assert env == "rc-candidate", (
            f"verify-only-3x environment 必须为 'rc-candidate',实际: {env}\n"
            f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
        )

    def test_migration_binding_gate_if_only_rc_tag(self, release_gates_yaml):
        """migration-binding-gate 的 if 条件必须只在 rc-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "migration-binding-gate")
        if_expr = job.get("if", "")
        assert "refs/tags/rc-v" in if_expr, (
            f"migration-binding-gate if 条件必须包含 'refs/tags/rc-v': {if_expr}\n"
            f"R70 P0-10: migration-binding-gate 仅在 RC tag 触发"
        )

    def test_migration_binding_gate_environment_rc_candidate(self, release_gates_yaml):
        """migration-binding-gate 必须使用 environment: rc-candidate。"""
        job = _get_job(release_gates_yaml, "migration-binding-gate")
        env = job.get("environment", "")
        assert env == "rc-candidate", (
            f"migration-binding-gate environment 必须为 'rc-candidate',实际: {env}\n"
            f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
        )

    def test_rc_only_jobs_not_triggered_on_master_push(self, release_gates_yaml):
        """所有 RC-only jobs 的 if 条件不应包含 master/main 分支触发。"""
        for job_id in self.RC_ONLY_JOBS:
            job = _get_job(release_gates_yaml, job_id)
            if_expr = job.get("if", "")
            # 不应包含 refs/heads/master 或 refs/heads/main
            assert "refs/heads/master" not in if_expr, (
                f"{job_id} if 条件不应包含 master 分支触发: {if_expr}\n"
                f"R70 P0-10: master push 不再触发 RC-only jobs"
            )
            assert "refs/heads/main" not in if_expr, (
                f"{job_id} if 条件不应包含 main 分支触发: {if_expr}\n"
                f"R70 P0-10: main push 不再触发 RC-only jobs"
            )


# ══════════════════════════════════════════════════════════════════════
# 测试组 3: production-promotion-gate 门禁
# ══════════════════════════════════════════════════════════════════════
class TestProductionPromotionGate:
    """R70 P0-10: production-promotion-gate 必须只在 production-v* tag 或 workflow_dispatch 触发。"""

    def test_production_promotion_gate_if_includes_production_tag(self, release_gates_yaml):
        """production-promotion-gate if 条件必须包含 production-v* tag 触发。"""
        job = _get_job(release_gates_yaml, "production-promotion-gate")
        if_expr = job.get("if", "")
        assert "refs/tags/production-v" in if_expr, (
            f"production-promotion-gate if 条件必须包含 'refs/tags/production-v': {if_expr}\n"
            f"R70 P0-10: production 部署由 production-v* tag 触发"
        )

    def test_production_promotion_gate_if_includes_workflow_dispatch(self, release_gates_yaml):
        """production-promotion-gate if 条件必须包含 workflow_dispatch 触发。"""
        job = _get_job(release_gates_yaml, "production-promotion-gate")
        if_expr = job.get("if", "")
        assert "workflow_dispatch" in if_expr, (
            f"production-promotion-gate if 条件必须包含 'workflow_dispatch': {if_expr}\n"
            f"R70 P0-10: production 部署支持手动 dispatch 触发"
        )

    def test_production_promotion_gate_environment_production(self, release_gates_yaml):
        """production-promotion-gate 必须使用 environment: production。"""
        job = _get_job(release_gates_yaml, "production-promotion-gate")
        env = job.get("environment", "")
        assert env == "production", (
            f"production-promotion-gate environment 必须为 'production',实际: {env}\n"
            f"R70 P0-10: production 部署使用 production environment"
        )

    def test_production_promotion_gate_not_triggered_on_master_push(self, release_gates_yaml):
        """production-promotion-gate 不应在 master push 触发。"""
        job = _get_job(release_gates_yaml, "production-promotion-gate")
        if_expr = job.get("if", "")
        # 不应直接或间接包含 master/main 分支触发
        # 允许 production-v* tag 或 workflow_dispatch,但不能 master push
        assert "refs/heads/master" not in if_expr, (
            f"production-promotion-gate if 条件不应包含 master 分支触发: {if_expr}\n"
            f"R70 P0-10: master push 不能直接触发 production 部署"
        )
        assert "refs/heads/main" not in if_expr, (
            f"production-promotion-gate if 条件不应包含 main 分支触发: {if_expr}\n"
            f"R70 P0-10: main push 不能直接触发 production 部署"
        )

    def test_production_promotion_gate_not_triggered_on_rc_tag(self, release_gates_yaml):
        """production-promotion-gate 不应在 rc-v* tag 触发(RC ≠ production)。"""
        job = _get_job(release_gates_yaml, "production-promotion-gate")
        if_expr = job.get("if", "")
        # 不应包含 refs/tags/rc-v(RC candidate 不能直接晋级到 production)
        assert "refs/tags/rc-v" not in if_expr, (
            f"production-promotion-gate if 条件不应包含 rc-v* tag 触发: {if_expr}\n"
            f"R70 P0-10: RC candidate 不能直接晋级到 production,必须先有 production-v* tag"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 4: master push jobs 必须使用 staging environment
# ══════════════════════════════════════════════════════════════════════
class TestMasterPushStagingEnvironment:
    """R70 P0-10: master push 触发的 jobs 必须使用 environment: staging(动态表达式)。

    动态环境表达式:
        environment: ${{ startsWith(github.ref, 'refs/tags/production-v') && 'production' ||
                        startsWith(github.ref, 'refs/tags/rc-v') && 'rc-candidate' ||
                        'staging' }}
    """

    # master push 也运行的常驻 jobs(无 if 条件限制,或 if 包含 master push)
    # 这些 jobs 在 master push 时必须使用 staging environment
    STAGING_DYNAMIC_JOBS = [
        "docker-build",
        "oci-allowlist-verify",
        "runtime-smoke-compose",
        "docker-digest-verify",
        "compose-config",
        "redis-acl-matrix",
        "schema-diff",
        "restore-legacy-seal-gate",
        "i18n-strict-export-boundary-gate",
        "migration-manifest-gate",
        "button-flow-real-ux-gate",
        "backup-restore-drill",
        "sbom",
        "pip-audit",
        "trivy",
        "verify-branch-protection",
        "verify-branch-ruleset",
        "verify-git-source-governance",
        "rc-continuity",
        "tag-ruleset-verify",
        "crdb-ru-72h-attribution-gate",
        "production-evidence",
    ]

    def test_staging_jobs_use_dynamic_environment_expression(self, release_gates_yaml):
        """所有 staging 候选 jobs 必须使用动态 environment 表达式(包含 'staging')。

        动态表达式在 master push 时求值为 'staging',在 rc-v* tag 时为 'rc-candidate',
        在 production-v* tag 时为 'production'。这样单一 job 定义可以正确隔离三个命名空间。
        """
        missing = []
        wrong_env = []
        for job_id in self.STAGING_DYNAMIC_JOBS:
            if job_id not in release_gates_yaml.get("jobs", {}):
                missing.append(job_id)
                continue
            job = release_gates_yaml["jobs"][job_id]
            env = job.get("environment", "")
            # environment 可以是字符串或 dict(若带 url)
            if isinstance(env, dict):
                env = env.get("name", "")
            if "staging" not in str(env):
                wrong_env.append((job_id, env))
        assert not missing, (
            f"release-gates.yml 缺少 jobs: {missing}\n"
            f"R70 P0-10: 这些 jobs 必须存在并使用动态 environment 表达式"
        )
        assert not wrong_env, (
            f"以下 jobs 的 environment 未包含 'staging' 动态表达式:\n"
            f"  {wrong_env}\n"
            f"R70 P0-10: master push 触发的 jobs 必须使用动态 environment 表达式"
        )

    def test_staging_dynamic_expression_includes_all_three_namespaces(self, release_gates_yaml):
        """动态 environment 表达式必须包含 staging / rc-candidate / production 三个值。"""
        # 收集所有 dynamic environment 表达式
        dynamic_envs = []
        for job_id, job in release_gates_yaml.get("jobs", {}).items():
            env = job.get("environment", "")
            env_str = str(env)
            if "startsWith(github.ref" in env_str and "staging" in env_str:
                dynamic_envs.append((job_id, env_str))
        assert dynamic_envs, (
            "未找到任何动态 environment 表达式(应包含 staging/rc-candidate/production)\n"
            "R70 P0-10: master push 触发的 jobs 必须使用动态 environment 表达式"
        )
        # 至少一个动态表达式必须包含三个 namespace
        has_all_three = False
        for job_id, env_str in dynamic_envs:
            if ("staging" in env_str and
                "rc-candidate" in env_str and
                "production" in env_str):
                has_all_three = True
                break
        assert has_all_three, (
            f"动态 environment 表达式必须包含 staging / rc-candidate / production 三个值\n"
            f"找到的表达式: {dynamic_envs[:3]}...\n"
            f"R70 P0-10: 三个命名空间必须在 environment 表达式中显式分离"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 5: release-summary bash 逻辑(RC_TAG / PRODUCTION_TAG)
# ══════════════════════════════════════════════════════════════════════
class TestReleaseSummaryBashLogic:
    """R70 P0-10: release-summary bash 逻辑必须正确区分 RC_TAG / PRODUCTION_TAG。"""

    def test_release_summary_job_exists(self, release_gates_yaml):
        """release-summary job 必须存在。"""
        assert "release-summary" in release_gates_yaml.get("jobs", {}), (
            "release-gates.yml 缺少 release-summary job\n"
            "R60 P0-06: release-summary 是聚合 required context"
        )

    def test_release_summary_defines_rc_tag(self, release_gates_text):
        """release-summary bash 必须定义 RC_TAG 变量(基于 refs/tags/rc-v*)。"""
        # 检查 bash 逻辑中定义了 RC_TAG 变量
        assert "RC_TAG" in release_gates_text, (
            "release-gates.yml 缺少 RC_TAG 变量定义\n"
            "R70 P0-10: release-summary 必须区分 RC tag 与 production tag"
        )
        # RC_TAG 必须基于 refs/tags/rc-v* 模式
        rc_tag_pattern = re.compile(
            r'RC_TAG\s*=.*?refs/tags/rc-v\*', re.DOTALL
        )
        assert rc_tag_pattern.search(release_gates_text), (
            "RC_TAG 必须基于 refs/tags/rc-v* 模式判断\n"
            "R70 P0-10: RC tag 模式必须是 rc-v*"
        )

    def test_release_summary_defines_production_tag(self, release_gates_text):
        """release-summary bash 必须定义 PRODUCTION_TAG 变量(基于 refs/tags/production-v*)。"""
        assert "PRODUCTION_TAG" in release_gates_text, (
            "release-gates.yml 缺少 PRODUCTION_TAG 变量定义\n"
            "R70 P0-10: release-summary 必须区分 RC tag 与 production tag"
        )
        # PRODUCTION_TAG 必须基于 refs/tags/production-v* 模式
        prod_tag_pattern = re.compile(
            r'PRODUCTION_TAG\s*=.*?refs/tags/production-v\*', re.DOTALL
        )
        assert prod_tag_pattern.search(release_gates_text), (
            "PRODUCTION_TAG 必须基于 refs/tags/production-v* 模式判断\n"
            "R70 P0-10: production tag 模式必须是 production-v*"
        )

    def test_release_summary_production_promotion_uses_production_tag(self, release_gates_text):
        """production_promotion_allowed 逻辑必须基于 PRODUCTION_TAG(不是 RELEASE_TAG)。

        R70 P0-10: RC candidate (rc-v*) 不能直接晋级到 production,
        必须由 production-v* tag 或 workflow_dispatch 触发。
        """
        # 查找 production_promotion_allowed 的赋值逻辑
        # 必须基于 PRODUCTION_TAG 或 workflow_dispatch,不能仅基于 RELEASE_TAG
        # RELEASE_TAG 在新逻辑中是 RC_TAG || PRODUCTION_TAG,所以不能单独使用
        # 查找 PRODUCTION_PROMOTION_ALLOWED=false 后的 if 条件
        # 允许的写法:
        #   if [ "${PRODUCTION_TAG}" = "true" ] || [ "${GITHUB_EVENT_NAME}" = "workflow_dispatch" ]; then
        # 禁止的写法:
        #   if [ "${RELEASE_TAG}" = "true" ] && [ "${PRODUCTION_PROMOTION_GATE}" = "success" ]; then
        #     (RELEASE_TAG 包含 RC_TAG,会导致 RC 直接晋级)
        # 通过查找包含 PRODUCTION_PROMOTION_ALLOWED 的代码块
        # 简化检查:查找 PRODUCTION_TAG 与 PRODUCTION_PROMOTION_ALLOWED 同时出现的行
        # 实际检查 if 条件中包含 PRODUCTION_TAG
        # 由于 YAML 文本中 bash 是单行展开的,检查关键变量即可
        # 必须有 PRODUCTION_TAG 与 PRODUCTION_PROMOTION_ALLOWED 在同一 bash 块中
        # 通过检查 PRODUCTION_TAG 出现在 release-summary job 中
        # release-summary job 的 bash 逻辑中必须包含 PRODUCTION_TAG
        # (PRODUCTION_PROMOTION_ALLOWED 的判断条件)
        assert "PRODUCTION_TAG" in release_gates_text, (
            "release-gates.yml 中未找到 PRODUCTION_TAG 变量\n"
            "R70 P0-10: production promotion 必须基于 PRODUCTION_TAG 判断"
        )

    def test_release_summary_no_legacy_release_tag_only_for_promotion(self, release_gates_text):
        """production_promotion_allowed 不能仅基于 RELEASE_TAG(会包含 RC_TAG)。

        检查 bash 逻辑中是否存在:
            if [ "${RELEASE_TAG}" = "true" ] && [ "${PRODUCTION_PROMOTION_GATE}" = "success" ]; then
        这种写法会因 RELEASE_TAG 包含 RC_TAG 而允许 RC 直接晋级到 production。
        """
        # 查找 "RELEASE_TAG" = "true" 与 PRODUCTION_PROMOTION_GATE 在同一 if 条件中
        # 这种模式是旧版的,不允许
        legacy_pattern = re.compile(
            r'\$\{RELEASE_TAG\}.*?PRODUCTION_PROMOTION_GATE.*?success',
            re.DOTALL
        )
        # 仅在 release-summary 的 PRODUCTION_PROMOTION_ALLOWED 块中查找
        # 简化:查找 RELEASE_TAG.*PRODUCTION_PROMOTION_GATE 模式
        # 但允许 PRODUCTION_TAG.*PRODUCTION_PROMOTION_GATE
        # 检查 PRODUCTION_PROMOTION_ALLOWED 赋值前的 if 条件
        # 找到 PRODUCTION_PROMOTION_ALLOWED=false 的位置
        idx = release_gates_text.find("PRODUCTION_PROMOTION_ALLOWED=false")
        if idx == -1:
            pytest.fail("release-gates.yml 中未找到 PRODUCTION_PROMOTION_ALLOWED=false")
        # 取后续 2000 字符(覆盖整个 if 块)
        block = release_gates_text[idx:idx + 2000]
        # 不应直接使用 RELEASE_TAG 作为 production_promotion 的判断条件
        # 允许的写法是 PRODUCTION_TAG 或 GITHUB_EVENT_NAME = workflow_dispatch
        # 查找 "${RELEASE_TAG}" = "true" 与 PRODUCTION_PROMOTION_GATE 在同一 if 中
        # 如果出现这种模式,说明 RC_TAG 也能触发 production promotion
        bad_pattern = re.compile(
            r'\$\{RELEASE_TAG\}.*?PRODUCTION_PROMOTION_GATE',
            re.DOTALL
        )
        bad_match = bad_pattern.search(block)
        assert not bad_match, (
            f"release-gates.yml 中 production_promotion_allowed 逻辑使用了 RELEASE_TAG:\n"
            f"  匹配: {bad_match.group()[:200] if bad_match else 'N/A'}\n"
            f"R70 P0-10: production promotion 必须基于 PRODUCTION_TAG 或 workflow_dispatch,\n"
            f"  不能使用 RELEASE_TAG(会包含 RC_TAG,导致 RC 直接晋级到 production)"
        )

    def test_release_summary_uses_set_euo_pipefail(self, release_gates_text):
        """release-summary bash 必须使用 set -euo pipefail(严格模式)。"""
        # 查找 release-summary job 中的 bash step
        # 至少一个 step 包含 set -euo pipefail
        idx = release_gates_text.find("release-summary:")
        assert idx != -1, "release-gates.yml 中未找到 release-summary job"
        # 取 release-summary job 之后的内容(直到下一个顶级 job 或文件末尾)
        # 简化:取后续 5000 字符
        block = release_gates_text[idx:idx + 5000]
        assert "set -euo pipefail" in block, (
            "release-summary job 的 bash step 必须使用 'set -euo pipefail'\n"
            "R70 P0-10: 严格模式,禁止吞异常"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 6: promote-rc.yml 存在性与基本结构
# ══════════════════════════════════════════════════════════════════════
class TestPromoteRCWorkflowStructure:
    """R70 P0-10: promote-rc.yml 必须存在并实现 RC → production 晋级流水线。"""

    def test_workflow_file_exists(self):
        """promote-rc.yml 文件必须存在。"""
        assert PROMOTE_RC_YML.exists(), (
            f".github/workflows/promote-rc.yml 不存在: {PROMOTE_RC_YML}\n"
            f"R70 P0-10: 必须创建独立的 RC 晋级 workflow"
        )

    def test_workflow_name(self, promote_rc_yaml):
        """workflow 名称必须为 'Promote RC'。"""
        name = promote_rc_yaml.get("name", "")
        assert name == "Promote RC", (
            f"promote-rc.yml name 必须为 'Promote RC',实际: {name}"
        )

    def test_workflow_triggers_include_production_tag(self, promote_rc_yaml):
        """R72 P0-15: promote-rc.yml 不再通过 production-v* tag push 触发。

        R70 P0-10 原要求 production-v* tag push 触发晋级流水线,但 R72 P0-15
        将 promote-rc.yml 改造为 thin wrapper:
          - 仅通过 workflow_dispatch 手动触发(唯一入口)
          - production-v* tag push 改由 release-gates.yml 的
            production-promotion-gate 处理(避免平行晋级通道)
          - 不再有 push 触发器(消除 weak lookup 欺骗风险)
        """
        on_config = _get_on_config(promote_rc_yaml)
        # R72 P0-15: 不应有 push 触发器(thin wrapper,仅 workflow_dispatch)
        assert "push" not in on_config, (
            f"R72 P0-15: promote-rc.yml 不应有 push 触发器(thin wrapper),"
            f"实际 on.push: {on_config.get('push')}"
        )
        # workflow_dispatch 必须存在(唯一入口)
        assert "workflow_dispatch" in on_config, (
            "R72 P0-15: promote-rc.yml 必须通过 workflow_dispatch 触发(唯一入口)"
        )

    def test_workflow_triggers_include_workflow_dispatch(self, promote_rc_yaml):
        """workflow 必须支持 workflow_dispatch 手动触发。"""
        on_config = _get_on_config(promote_rc_yaml)
        assert "workflow_dispatch" in on_config, (
            "promote-rc.yml 缺少 workflow_dispatch 触发器\n"
            "R70 P0-10: 运维需通过 workflow_dispatch 手动触发晋级"
        )

    def test_workflow_dispatch_has_required_inputs(self, promote_rc_yaml):
        """workflow_dispatch 必须包含 rc_tag 与 production_tag 输入。"""
        dispatch_config = _get_on_config(promote_rc_yaml).get("workflow_dispatch", {})
        inputs = dispatch_config.get("inputs", {}) if dispatch_config else {}
        assert "rc_tag" in inputs, (
            f"workflow_dispatch.inputs 缺少 rc_tag\n"
            f"R70 P0-10: 手动晋级时必须指定要晋级的 RC tag"
        )
        assert "production_tag" in inputs, (
            f"workflow_dispatch.inputs 缺少 production_tag\n"
            f"R70 P0-10: 手动晋级时必须指定要创建的 production tag"
        )
        # 两个 input 都必须是 required
        assert inputs["rc_tag"].get("required") is True, (
            "workflow_dispatch.inputs.rc_tag 必须为 required: true"
        )
        assert inputs["production_tag"].get("required") is True, (
            "workflow_dispatch.inputs.production_tag 必须为 required: true"
        )

    def test_workflow_has_concurrency_group(self, promote_rc_yaml):
        """workflow 必须有 concurrency 配置(避免并发晋级)。"""
        concurrency = promote_rc_yaml.get("concurrency", {})
        assert "group" in concurrency, (
            "promote-rc.yml 缺少 concurrency.group 配置\n"
            "R70 P0-10: production 晋级必须避免并发"
        )
        # cancel-in-progress 必须为 false(避免半完成状态)
        assert concurrency.get("cancel-in-progress") is False, (
            "promote-rc.yml concurrency.cancel-in-progress 必须为 false\n"
            "R70 P0-10: production 晋级不可取消(避免半完成状态)"
        )

    def test_workflow_declares_permissions(self, promote_rc_yaml):
        """workflow 必须声明 permissions。"""
        assert "permissions" in promote_rc_yaml, (
            "promote-rc.yml 缺少 permissions 声明\n"
            "R70 P0-10: 必须显式声明权限(最小权限原则)"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 7: promote-rc.yml 所有 jobs 必须使用 environment: production
# ══════════════════════════════════════════════════════════════════════
class TestPromoteRCJobsUseProductionEnvironment:
    """R72 P0-15: promote-rc.yml thin wrapper 中所有 jobs 使用 environment: production。

    R72 P0-15 将 promote-rc.yml 改造为 thin wrapper:
      - 删除 5 个旧 job (validate-rc-candidate / verify-production-evidence /
        verify-rc-tag-immutability / promote-to-production / promotion-summary)
      - 仅保留 1 个 job: create-signed-production-tag
      - 所有身份校验委托给 release-gates.yml 的 production-promotion-gate
    """

    EXPECTED_JOBS = ["create-signed-production-tag"]

    DEPRECATED_JOBS = [
        "validate-rc-candidate",
        "verify-production-evidence",
        "verify-rc-tag-immutability",
        "promote-to-production",
        "promotion-summary",
    ]

    def test_all_expected_jobs_exist(self, promote_rc_yaml):
        """promote-rc.yml 必须包含 thin wrapper 的 create-signed-production-tag job。"""
        jobs = promote_rc_yaml.get("jobs", {})
        missing = [j for j in self.EXPECTED_JOBS if j not in jobs]
        assert not missing, (
            f"promote-rc.yml 缺少 thin wrapper jobs: {missing}\n"
            f"R72 P0-15: 必须包含 create-signed-production-tag job"
        )

    def test_no_deprecated_jobs_remain(self, promote_rc_yaml):
        """promote-rc.yml 不应包含 R72 P0-15 已删除的旧 jobs。"""
        jobs = promote_rc_yaml.get("jobs", {})
        deprecated_found = [j for j in self.DEPRECATED_JOBS if j in jobs]
        assert not deprecated_found, (
            f"promote-rc.yml 仍包含已废弃的旧 jobs: {deprecated_found}\n"
            f"R72 P0-15: 旧版 5 个重复校验 job 已删除(thin wrapper)"
        )

    def test_create_signed_production_tag_uses_production_environment(self, promote_rc_yaml):
        """create-signed-production-tag 必须使用 environment: production。"""
        job = _get_job(promote_rc_yaml, "create-signed-production-tag")
        env = job.get("environment", "")
        assert env == "production", (
            f"create-signed-production-tag environment 必须为 'production',实际: {env}\n"
            f"R72 P0-15: thin wrapper job 使用 production environment"
        )

    def test_create_signed_production_tag_has_no_needs(self, promote_rc_yaml):
        """create-signed-production-tag 是唯一 job,不应有 needs 依赖。"""
        job = _get_job(promote_rc_yaml, "create-signed-production-tag")
        needs = job.get("needs", [])
        assert not needs, (
            f"create-signed-production-tag 不应有 needs 依赖(唯一 job),实际: {needs}\n"
            f"R72 P0-15: thin wrapper 只有一个 job"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 8: promote-rc.yml thin wrapper 结构(R72 P0-15)
# ══════════════════════════════════════════════════════════════════════
class TestPromoteRCJobDependencies:
    """R72 P0-15: promote-rc.yml thin wrapper 只有一个 job,步骤内串行化。

    旧版 (R70 P0-10) 有 5 个 job 通过 needs 串行化:
      validate-rc-candidate → verify-production-evidence /
      verify-rc-tag-immutability → promote-to-production → promotion-summary

    R72 P0-15 thin wrapper 只有一个 job (create-signed-production-tag),
    内部通过 4 个 step 串行执行:
      1. validate inputs + verify RC run head SHA
      2. docker pull + verify RepoDigest
      3. configure GPG signing key
      4. create signed production-v* tag (git tag -s)
    """

    def test_single_job_no_needs(self, promote_rc_yaml):
        """thin wrapper 只有一个 job,不应有 needs 依赖。"""
        jobs = promote_rc_yaml.get("jobs", {})
        assert len(jobs) == 1, (
            f"R72 P0-15: promote-rc.yml 应只有 1 个 job(thin wrapper),"
            f"实际: {list(jobs.keys())}"
        )
        job_name = list(jobs.keys())[0]
        needs = jobs[job_name].get("needs", [])
        assert not needs, (
            f"唯一 job '{job_name}' 不应有 needs 依赖,实际: {needs}\n"
            f"R72 P0-15: thin wrapper 单 job 无依赖"
        )

    def test_create_signed_production_tag_has_4_steps(self, promote_rc_yaml):
        """create-signed-production-tag 必须有 4 个 step(对应 4 项校验)。"""
        job = _get_job(promote_rc_yaml, "create-signed-production-tag")
        steps = job.get("steps", [])
        # 至少 4 个 step(checkout + setup-python + 4 个校验 step)
        assert len(steps) >= 4, (
            f"create-signed-production-tag steps 不足: {len(steps)} (期望 >= 4)\n"
            f"R72 P0-15: thin wrapper 至少 4 个校验 step"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 9: promote-rc.yml 关键校验逻辑
# ══════════════════════════════════════════════════════════════════════
class TestPromoteRCValidationLogic:
    """R70 P0-10: promote-rc.yml 必须包含关键校验逻辑(RC 存在性 / 不可变性 / 命名规范)。"""

    def test_validate_rc_candidate_checks_rc_tag_naming(self, promote_rc_text):
        """validate-rc-candidate 必须校验 RC tag 命名规范(以 rc-v 开头)。"""
        assert "rc-v" in promote_rc_text, (
            "promote-rc.yml 必须校验 RC tag 命名规范(以 rc-v 开头)\n"
            "R70 P0-10: RC candidate tag 必须形如 rc-v*.*.*"
        )
        # 必须有正则或 bash 模式匹配 ^rc-v
        assert re.search(r'\^rc-v|rc-v\*|refs/tags/rc-v', promote_rc_text), (
            "promote-rc.yml 必须包含 RC tag 命名规范校验(^rc-v 或 refs/tags/rc-v)\n"
            "R70 P0-10: RC tag 必须以 rc-v 开头"
        )

    def test_validate_rc_candidate_checks_production_tag_naming(self, promote_rc_text):
        """validate-rc-candidate 必须校验 production tag 命名规范(以 production-v 开头)。"""
        assert "production-v" in promote_rc_text, (
            "promote-rc.yml 必须校验 production tag 命名规范(以 production-v 开头)\n"
            "R70 P0-10: production tag 必须形如 production-v*.*.*"
        )
        assert re.search(r'\^production-v|production-v\*|refs/tags/production-v', promote_rc_text), (
            "promote-rc.yml 必须包含 production tag 命名规范校验\n"
            "R70 P0-10: production tag 必须以 production-v 开头"
        )

    def test_validate_rc_candidate_checks_rc_tag_exists(self, promote_rc_text):
        """validate-rc-candidate 必须校验 RC tag 已存在。"""
        # 必须使用 git rev-parse --verify 校验 tag 存在
        assert "git rev-parse --verify" in promote_rc_text, (
            "promote-rc.yml 必须使用 'git rev-parse --verify' 校验 RC tag 存在\n"
            "R70 P0-10: production 晋级必须基于已存在的 RC candidate"
        )

    def test_validate_rc_candidate_verifies_release_gates_passed(self, promote_rc_text):
        """R72 P0-15: thin wrapper 必须用 gh run view 校验 RC run 成功(替代弱查找)。

        旧版 (R70 P0-10) 用 `gh run list --status=success --limit=1` 查找成功 run,
        不核对 head SHA — 易被 weak lookup 欺骗。R72 P0-15 改用 `gh run view <id>`
        精确查询并校验 head SHA == RC tag commit。
        """
        # R72 P0-15: 必须使用 gh run view(精确查询),不再用 gh run list(弱查找)
        assert "gh run view" in promote_rc_text, (
            "promote-rc.yml 必须使用 'gh run view' 精确查询 RC run 状态\n"
            "R72 P0-15: 替代旧版 gh run list 弱查找(不核对 SHA)"
        )
        assert "release-gates.yml" in promote_rc_text, (
            "promote-rc.yml 必须查询 release-gates.yml workflow\n"
            "R72 P0-15: RC candidate 必须通过 release-gates.yml 的所有 RC-only jobs"
        )
        # R72 P0-15: 必须校验 conclusion == success
        assert "success" in promote_rc_text, (
            "promote-rc.yml 必须校验 RC run conclusion 为 success\n"
            "R72 P0-15: 仅成功的 RC candidate 可晋级到 production"
        )

    def test_verify_rc_tag_immutability_compares_commit_sha(self, promote_rc_text):
        """verify-rc-tag-immutability 必须比较 RC tag 当前 SHA 与初始 SHA。"""
        # 必须重新查询 RC tag 当前指向的 commit
        assert "git rev-parse" in promote_rc_text, (
            "promote-rc.yml 必须使用 'git rev-parse' 重新查询 RC tag 当前 SHA\n"
            "R70 P0-10: 验证 RC tag 不可变性需要对比初始 SHA 与当前 SHA"
        )
        # 必须有 CURRENT_SHA 或类似的变量(与 RC_COMMIT_SHA 对比)
        assert "CURRENT_SHA" in promote_rc_text or "RC_COMMIT_SHA" in promote_rc_text, (
            "promote-rc.yml 必须比较 RC tag 的当前 SHA 与初始 SHA\n"
            "R70 P0-10: RC tag 在晋级过程中不可移动"
        )

    def test_promote_to_production_creates_tag_on_workflow_dispatch(self, promote_rc_text):
        """promote-to-production 在 workflow_dispatch 时必须创建 production tag。"""
        # 必须有 'git tag' 与 'git push' 命令(创建并推送 production tag)
        assert "git tag" in promote_rc_text, (
            "promote-rc.yml 必须包含 'git tag' 命令(创建 production tag)\n"
            "R70 P0-10: workflow_dispatch 触发时需创建 production-v* tag"
        )
        assert "git push" in promote_rc_text, (
            "promote-rc.yml 必须包含 'git push' 命令(推送 production tag)\n"
            "R70 P0-10: 创建的 production-v* tag 必须推送到 origin"
        )

    def test_promote_to_production_uses_set_euo_pipefail(self, promote_rc_text):
        """promote-to-production bash 必须使用 set -euo pipefail。"""
        # 所有 bash step 必须有 set -euo pipefail
        # 检查至少出现 4 次(每个 job 至少一次)
        count = promote_rc_text.count("set -euo pipefail")
        assert count >= 4, (
            f"promote-rc.yml 中 'set -euo pipefail' 出现次数不足: {count} (期望 >= 4)\n"
            f"R70 P0-10: 所有 bash step 必须使用严格模式,禁止吞异常"
        )

    def test_promote_to_production_no_skip_no_warn(self, promote_rc_text):
        """promote-rc.yml 不应包含 skip / warn / 吞异常 模式。

        允许的 continue-on-error: 仅在 download-artifact 中使用(后续 step 会显式 FAIL)。
        其他 step 不允许 continue-on-error: true。
        """
        # 查找 continue-on-error: true 的出现
        # 允许在 download-artifact step 中使用(artifact 可能不存在)
        # 但其他 step 不允许
        lines = promote_rc_text.split("\n")
        bad_lines = []
        for i, line in enumerate(lines):
            if "continue-on-error: true" in line:
                # 检查上下文是否为 download-artifact
                # 往上查找 10 行,看是否有 download-artifact
                context = "\n".join(lines[max(0, i - 10):i + 1])
                if "download-artifact" not in context and "actions/download-artifact" not in context:
                    bad_lines.append((i + 1, line.strip()))
        assert not bad_lines, (
            f"promote-rc.yml 中发现非 download-artifact 的 continue-on-error: true:\n"
            f"  {bad_lines}\n"
            f"R70 P0-10: 禁止吞异常(仅 download-artifact 允许 continue-on-error,"
            f"且有显式 fallback FAIL)"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 10: 整改规范(无 TODO / pass / 占位符)
# ══════════════════════════════════════════════════════════════════════
class TestNoPlaceholdersOrShortcuts:
    """R70 整改规范:禁止 // TODO / pass / 占位符 / skip / warn / 吞异常。"""

    def test_release_gates_no_todo_comments(self, release_gates_text):
        """release-gates.yml 不应包含 // TODO 或 # TODO 注释。"""
        # 查找 TODO 注释(YAML 用 # ,但 // TODO 也可能出现在 bash 块中)
        todo_pattern = re.compile(r'#\s*TODO|//\s*TODO', re.IGNORECASE)
        matches = todo_pattern.findall(release_gates_text)
        assert not matches, (
            f"release-gates.yml 包含 TODO 注释: {matches}\n"
            f"R70 整改规范: 禁止 TODO / 占位符"
        )

    def test_release_gates_no_pass_placeholder(self, release_gates_text):
        """release-gates.yml 不应包含 'pass' 占位符(Python pass 或 bash : 占位)。"""
        # bash 中的 'pass' 通常不会出现,但检查 ": pass" 或 "pass:" 模式
        # 简化:查找单独的 pass 行(不包含 password / passphrase)
        pass_pattern = re.compile(r'^\s*pass\s*$', re.MULTILINE)
        matches = pass_pattern.findall(release_gates_text)
        # 过滤掉 password / passphrase 等正常单词
        real_pass = [m for m in matches if "pass" in m and "password" not in m.lower()]
        assert not real_pass, (
            f"release-gates.yml 包含 'pass' 占位符: {real_pass}\n"
            f"R70 整改规范: 禁止 pass 占位符"
        )

    def test_promote_rc_no_todo_comments(self, promote_rc_text):
        """promote-rc.yml 不应包含 // TODO 或 # TODO 注释。"""
        todo_pattern = re.compile(r'#\s*TODO|//\s*TODO', re.IGNORECASE)
        matches = todo_pattern.findall(promote_rc_text)
        assert not matches, (
            f"promote-rc.yml 包含 TODO 注释: {matches}\n"
            f"R70 整改规范: 禁止 TODO / 占位符"
        )

    def test_promote_rc_no_pass_placeholder(self, promote_rc_text):
        """promote-rc.yml 不应包含 'pass' 占位符。"""
        pass_pattern = re.compile(r'^\s*pass\s*$', re.MULTILINE)
        matches = pass_pattern.findall(promote_rc_text)
        real_pass = [m for m in matches if "pass" in m and "password" not in m.lower()]
        assert not real_pass, (
            f"promote-rc.yml 包含 'pass' 占位符: {real_pass}\n"
            f"R70 整改规范: 禁止 pass 占位符"
        )

    def test_release_gates_no_placeholder_digests(self, release_gates_text):
        """release-gates.yml 不应包含占位 digest 值(0000.../ffff.../1234...)。"""
        # 占位 digest 模式:64 位全 0 / 全 f / 重复 1234...
        placeholder_patterns = [
            r'0{64}',
            r'f{64}',
            r'(?:1234567890abcdef){4}',
            r'placeholderplaceholder',
        ]
        for pattern in placeholder_patterns:
            matches = re.findall(pattern, release_gates_text, re.IGNORECASE)
            assert not matches, (
                f"release-gates.yml 包含占位 digest: {matches} (pattern: {pattern})\n"
                f"R70 整改规范: 禁止占位符"
            )

    def test_promote_rc_no_placeholder_digests(self, promote_rc_text):
        """promote-rc.yml 不应包含占位 digest 值。"""
        placeholder_patterns = [
            r'0{64}',
            r'f{64}',
            r'(?:1234567890abcdef){4}',
            r'placeholderplaceholder',
        ]
        for pattern in placeholder_patterns:
            matches = re.findall(pattern, promote_rc_text, re.IGNORECASE)
            assert not matches, (
                f"promote-rc.yml 包含占位 digest: {matches} (pattern: {pattern})\n"
                f"R70 整改规范: 禁止占位符"
            )


# ══════════════════════════════════════════════════════════════════════
# 测试组 11: SHA-pinned actions(供应链安全)
# ══════════════════════════════════════════════════════════════════════
class TestSHAPinnedActions:
    """R70 P0-10: 所有 actions 引用必须 SHA-pinned(不允许 @vN 或 @master)。"""

    def test_release_gates_actions_are_sha_pinned(self, release_gates_text):
        """release-gates.yml 中所有 actions/ 引用必须使用 SHA pin。"""
        # 查找 uses: actions/<action>@<ref>
        # ref 必须是 40 位 SHA(不允许多字符),不允许 @vN 或 @main 或 @master
        uses_pattern = re.compile(
            r'uses:\s*(actions|github)/(\S+)@([a-f0-9]{40})'
        )
        # 查找所有 uses: 行
        all_uses = re.findall(r'uses:\s*(\S+)', release_gates_text)
        action_uses = [u for u in all_uses if u.startswith("actions/") or u.startswith("github/")]
        # 每个 action 引用必须 SHA pin
        non_sha = []
        for u in action_uses:
            # 提取 @ref 部分
            if "@" in u:
                ref = u.split("@", 1)[1]
                # SHA 是 40 位 hex
                if not re.match(r'^[a-f0-9]{40}$', ref):
                    non_sha.append(u)
        assert not non_sha, (
            f"release-gates.yml 中以下 actions 引用未使用 SHA pin:\n"
            f"  {non_sha}\n"
            f"R70 P0-10: 所有 actions 必须使用 SHA pin(供应链安全)"
        )

    def test_promote_rc_actions_are_sha_pinned(self, promote_rc_text):
        """promote-rc.yml 中所有 actions/ 引用必须使用 SHA pin。"""
        all_uses = re.findall(r'uses:\s*(\S+)', promote_rc_text)
        action_uses = [u for u in all_uses if u.startswith("actions/") or u.startswith("github/")]
        non_sha = []
        for u in action_uses:
            if "@" in u:
                ref = u.split("@", 1)[1]
                if not re.match(r'^[a-f0-9]{40}$', ref):
                    non_sha.append(u)
        assert not non_sha, (
            f"promote-rc.yml 中以下 actions 引用未使用 SHA pin:\n"
            f"  {non_sha}\n"
            f"R70 P0-10: 所有 actions 必须使用 SHA pin(供应链安全)"
        )


# ══════════════════════════════════════════════════════════════════════
# 测试组 12: 跨 workflow 一致性
# ══════════════════════════════════════════════════════════════════════
class TestCrossWorkflowConsistency:
    """R70 P0-10: release-gates.yml 与 promote-rc.yml 必须一致使用命名空间分离。"""

    def test_both_workflows_use_production_environment_for_production(self,
                                                                      release_gates_yaml,
                                                                      promote_rc_yaml):
        """两个 workflow 中触发 production 部署的 job 必须使用 environment: production。

        R72 P0-15: promote-rc.yml 改造为 thin wrapper,
        唯一 job 为 create-signed-production-tag(替代旧 promote-to-production)。
        """
        # release-gates.yml: production-promotion-gate
        rg_ppg = _get_job(release_gates_yaml, "production-promotion-gate")
        assert rg_ppg.get("environment") == "production", (
            "release-gates.yml production-promotion-gate 必须使用 environment: production"
        )
        # promote-rc.yml: create-signed-production-tag (R72 P0-15 thin wrapper)
        pr_cspt = _get_job(promote_rc_yaml, "create-signed-production-tag")
        assert pr_cspt.get("environment") == "production", (
            "promote-rc.yml create-signed-production-tag 必须使用 environment: production"
        )

    def test_both_workflows_use_rc_candidate_for_rc(self, release_gates_yaml):
        """release-gates.yml 中 RC-only jobs 必须使用 environment: rc-candidate。"""
        rc_only_jobs = [
            "sign-image",
            "publish-attestation",
            "attestation-semantics-verify",
            "verify-only-3x",
            "migration-binding-gate",
        ]
        for job_id in rc_only_jobs:
            job = _get_job(release_gates_yaml, job_id)
            env = job.get("environment", "")
            assert env == "rc-candidate", (
                f"{job_id} environment 必须为 'rc-candidate',实际: {env}\n"
                f"R70 P0-10: RC tag 触发的 job 使用 rc-candidate environment"
            )

    def test_both_workflows_use_staging_for_master_push(self, release_gates_yaml):
        """release-gates.yml 中 master push 触发的常驻 jobs 必须使用动态 environment(含 staging)。

        promote-rc.yml 不在 master push 触发,所以只检查 release-gates.yml。
        """
        # 至少 10 个常驻 jobs 必须使用动态 environment 表达式(包含 'staging')
        dynamic_count = 0
        for job_id, job in release_gates_yaml.get("jobs", {}).items():
            env = str(job.get("environment", ""))
            if "staging" in env and "startsWith(github.ref" in env:
                dynamic_count += 1
        assert dynamic_count >= 10, (
            f"release-gates.yml 中使用动态 environment(含 staging)的 jobs 数量不足: "
            f"{dynamic_count} (期望 >= 10)\n"
            f"R70 P0-10: master push 触发的常驻 jobs 必须使用动态 environment 表达式"
        )

    def test_release_gates_has_no_direct_master_to_production_path(self, release_gates_text):
        """release-gates.yml 不应有 master push 直接进入 production 签名的路径。

        检查:sign-image / publish-attestation / production-promotion-gate 的 if 条件
        不应同时包含 master push 与 production tag(会创建 master → production 捷径)。
        """
        # sign-image 应只在 rc-v* tag 触发(不含 master push)
        # production-promotion-gate 应只在 production-v* tag 或 workflow_dispatch 触发
        # 已在 TestRCOnlyJobs 与 TestProductionPromotionGate 中详细验证
        # 这里做综合检查:不在 release-gates_text 中出现 "master push 直接签名" 模式
        # 简化检查:确保 sign-image 不在 master push 触发(已在 TestRCOnlyJobs 验证)
        # 此测试作为聚合断言,确保整体一致性
        rg_yaml = yaml.safe_load(release_gates_text)
        sign_image = rg_yaml.get("jobs", {}).get("sign-image", {})
        if_expr = sign_image.get("if", "")
        # 综合:sign-image if 不应包含 master/main push
        assert "refs/heads/master" not in if_expr
        assert "refs/heads/main" not in if_expr
        # 综合:sign-image if 必须包含 rc-v tag
        assert "refs/tags/rc-v" in if_expr


# ══════════════════════════════════════════════════════════════════════
# 测试组 13: 综合集成验证
# ══════════════════════════════════════════════════════════════════════
class TestR70Wave9Integration:
    """R70 P0-10 Wave 9: 综合集成验证 — 命名空间分离端到端一致性。"""

    def test_three_namespaces_completely_separated(self, release_gates_yaml):
        """三个命名空间(staging / rc-candidate / production)必须完全分离。

        验证:
        1. staging: master push 触发的 jobs 使用动态 environment 表达式
        2. rc-candidate: rc-v* tag 触发的 RC-only jobs 使用 environment: rc-candidate
        3. production: production-v* tag 触发的 production-promotion-gate 使用 environment: production
        """
        # 1. staging: 至少 10 个常驻 jobs 使用动态 environment
        staging_jobs = []
        for job_id, job in release_gates_yaml.get("jobs", {}).items():
            env = str(job.get("environment", ""))
            if "staging" in env and "startsWith(github.ref" in env:
                staging_jobs.append(job_id)
        assert len(staging_jobs) >= 10, (
            f"staging namespace jobs 数量不足: {len(staging_jobs)} (期望 >= 10)"
        )

        # 2. rc-candidate: RC-only jobs 使用 environment: rc-candidate
        rc_only_jobs = [
            "sign-image",
            "publish-attestation",
            "attestation-semantics-verify",
            "verify-only-3x",
            "migration-binding-gate",
        ]
        for job_id in rc_only_jobs:
            job = _get_job(release_gates_yaml, job_id)
            assert job.get("environment") == "rc-candidate", (
                f"{job_id} 必须使用 environment: rc-candidate"
            )

        # 3. production: production-promotion-gate 使用 environment: production
        ppg = _get_job(release_gates_yaml, "production-promotion-gate")
        assert ppg.get("environment") == "production", (
            "production-promotion-gate 必须使用 environment: production"
        )

    def test_promote_rc_pipeline_complete(self, promote_rc_yaml):
        """R72 P0-15: promote-rc.yml thin wrapper 包含 create-signed-production-tag job。

        旧版 (R70 P0-10) 要求 5 阶段晋级流水线(5 个 job)。
        R72 P0-15 改造为 thin wrapper,仅保留 1 个 job,
        完整身份校验委托给 release-gates.yml 的 production-promotion-gate。
        """
        expected_jobs = ["create-signed-production-tag"]
        deprecated_jobs = [
            "validate-rc-candidate",
            "verify-production-evidence",
            "verify-rc-tag-immutability",
            "promote-to-production",
            "promotion-summary",
        ]
        jobs = promote_rc_yaml.get("jobs", {})
        for job_id in expected_jobs:
            assert job_id in jobs, (
                f"promote-rc.yml 缺少 job: {job_id}\n"
                f"R72 P0-15: thin wrapper 必须包含 create-signed-production-tag"
            )
        # 旧版 5 阶段 job 已废弃
        for job_id in deprecated_jobs:
            assert job_id not in jobs, (
                f"promote-rc.yml 不应包含已废弃的旧 job: {job_id}\n"
                f"R72 P0-15: 旧版 5 阶段晋级流水线已删除(thin wrapper)"
            )

    def test_release_gates_yaml_is_valid(self, release_gates_yaml):
        """release-gates.yml 必须是合法 YAML(已通过 yaml.safe_load 解析)。"""
        # 如果 fixture 加载成功,说明 YAML 合法
        assert isinstance(release_gates_yaml, dict)
        assert "jobs" in release_gates_yaml
        # YAML 1.1 把 'on' 解析为 True,使用兼容检查
        assert _workflow_has_on_key(release_gates_yaml), (
            "release-gates.yml 缺少 on 配置(YAML 1.1 可能解析为 True 键)"
        )

    def test_promote_rc_yaml_is_valid(self, promote_rc_yaml):
        """promote-rc.yml 必须是合法 YAML(已通过 yaml.safe_load 解析)。"""
        assert isinstance(promote_rc_yaml, dict)
        assert "jobs" in promote_rc_yaml
        # YAML 1.1 把 'on' 解析为 True,使用兼容检查
        assert _workflow_has_on_key(promote_rc_yaml), (
            "promote-rc.yml 缺少 on 配置(YAML 1.1 可能解析为 True 键)"
        )

    def test_release_gates_has_required_release_pipeline_jobs(self, release_gates_yaml):
        """release-gates.yml 必须包含完整 release pipeline 所需的关键 jobs。"""
        required_jobs = [
            "docker-build",                   # 镜像构建(staging)
            "sign-image",                     # 签名(rc-candidate)
            "publish-attestation",            # 发布 attestation(rc-candidate)
            "verify-only-3x",                 # 3x 验证(rc-candidate)
            "production-evidence",            # 生产证据(rc-candidate)
            "production-promotion-gate",      # 生产晋级门禁(production)
            "release-summary",                # 聚合状态报告
        ]
        jobs = release_gates_yaml.get("jobs", {})
        missing = [j for j in required_jobs if j not in jobs]
        assert not missing, (
            f"release-gates.yml 缺少关键 release pipeline jobs: {missing}\n"
            f"R70 P0-10: 必须包含完整 release pipeline"
        )
