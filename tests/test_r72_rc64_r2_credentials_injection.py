"""R72 RC64: compose-runtime-e2e R2 凭证注入测试套件。

R72 RC64 整改背景:
    R72 报告 P0-10 要求 compose-runtime-e2e backup_restore 阶段必须配置真实
    R2 凭证(不能 mock)。原 release-gates.yml 中 compose-runtime-e2e job
    "Generate minimal secrets placeholders" 步骤仅写入 COCKROACHDB_URL,
    未注入 R2 凭证和 BACKUP_KEK,导致 backup_once() 严格 fail-closed 抛
    AppError,backup_restore 阶段无法通过。

RC64 修复:
    1. 在 .env.shared 注入 R2 凭证(db_restore 走 db_writer 服务,只加载 .env.shared)
    2. 在 .env.secrets.db_backup 冗余注入 R2 凭证(db_backup 服务加载)
    3. step env 块声明 6 个 GitHub Secrets 引用,凭证来源审计可追溯

测试策略:
    - 静态解析 release-gates.yml 验证注入逻辑(不依赖 GitHub Actions 运行)
    - 严格验证 R2_ENDPOINT 不含 https:// 前缀(用户硬约束)
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

# 必需的 R2 凭证 secrets 列表
REQUIRED_R2_SECRETS = [
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "R2_ENDPOINT",
    "BACKUP_KEK",
]


def _read_workflow() -> str:
    """读取 release-gates.yml 源码。"""
    return RELEASE_GATES_PATH.read_text(encoding="utf-8")


def _parse_workflow() -> dict:
    """解析 release-gates.yml 为 dict。"""
    with RELEASE_GATES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ════════════════════════════════════════════════════════════════
# A. release-gates.yml 必须包含 R2 凭证注入逻辑
# ════════════════════════════════════════════════════════════════


class TestR2CredentialsInjectionStep:
    """R72 RC64: compose-runtime-e2e job 必须注入 R2 凭证到 .env.shared。"""

    def test_release_gates_file_exists(self):
        """release-gates.yml 必须存在。"""
        assert RELEASE_GATES_PATH.is_file(), (
            f"R72 RC64: release-gates.yml 必须存在: {RELEASE_GATES_PATH}"
        )

    def test_compose_runtime_e2e_job_exists(self):
        """compose-runtime-e2e job 必须在 workflow 中定义。"""
        workflow = _parse_workflow()
        jobs = workflow.get("jobs", {})
        assert "compose-runtime-e2e" in jobs, (
            "R72 RC64: release-gates.yml 必须包含 compose-runtime-e2e job"
        )

    def test_r2_credentials_injected_into_env_shared(self):
        """R2 凭证必须被注入到 .env.shared(供 db_restore 读取)。

        db_restore 通过 `docker compose run db_writer` 执行,db_writer 的
        env_file 只有 .env.shared,所以 R2 凭证必须写入 .env.shared。
        """
        source = _read_workflow()
        # 找到 compose-runtime-e2e job 区块
        idx = source.find("compose-runtime-e2e:")
        assert idx >= 0, "R72 RC64: 未找到 compose-runtime-e2e job 定义"
        # 取 job 区块(到下一个顶层 job 或文件末尾)
        next_job_match = re.search(r"\n  [a-z][a-z0-9-]*:", source[idx + 30:])
        if next_job_match:
            job_block = source[idx: idx + 30 + next_job_match.start()]
        else:
            job_block = source[idx:]

        for secret in REQUIRED_R2_SECRETS:
            pattern = rf'echo\s+"{secret}=\$\{{{secret}\}}"\s+>>\s+\.env\.shared'
            match = re.search(pattern, job_block)
            assert match, (
                f"R72 RC64: compose-runtime-e2e job 必须将 {secret} 注入到 .env.shared, "
                f"期望匹配: {pattern}"
            )

    def test_r2_credentials_injected_into_db_backup_secrets(self):
        """R2 凭证必须冗余注入到 .env.secrets.db_backup(db_backup 服务加载)。"""
        source = _read_workflow()
        idx = source.find("compose-runtime-e2e:")
        assert idx >= 0, "R72 RC64: 未找到 compose-runtime-e2e job 定义"
        next_job_match = re.search(r"\n  [a-z][a-z0-9-]*:", source[idx + 30:])
        if next_job_match:
            job_block = source[idx: idx + 30 + next_job_match.start()]
        else:
            job_block = source[idx:]

        for secret in REQUIRED_R2_SECRETS:
            pattern = rf'echo\s+"{secret}=\$\{{{secret}\}}"\s+>>\s+\.env\.secrets\.db_backup'
            match = re.search(pattern, job_block)
            assert match, (
                f"R72 RC64: compose-runtime-e2e job 必须将 {secret} 冗余注入到 "
                f".env.secrets.db_backup, 期望匹配: {pattern}"
            )


# ════════════════════════════════════════════════════════════════
# B. step env 块必须声明 GitHub Secrets 引用
# ════════════════════════════════════════════════════════════════


class TestStepEnvSecretsReferences:
    """R72 RC64: step env 块必须声明所有 R2 secrets 引用。"""

    def test_step_env_block_exists_in_generate_secrets_step(self):
        """Generate minimal secrets placeholders 步骤必须有 env 块。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            if step.get("name") == "Generate minimal secrets placeholders":
                generate_step = step
                break
        assert generate_step is not None, (
            "R72 RC64: 未找到 'Generate minimal secrets placeholders' step"
        )
        assert "env" in generate_step, (
            "R72 RC64: 'Generate minimal secrets placeholders' step 必须有 env 块"
        )

    def test_all_r2_secrets_referenced_in_step_env(self):
        """step env 块必须引用所有 6 个 R2 secrets。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            if step.get("name") == "Generate minimal secrets placeholders":
                generate_step = step
                break
        assert generate_step is not None
        env_block = generate_step["env"]
        for secret in REQUIRED_R2_SECRETS:
            assert secret in env_block, (
                f"R72 RC64: step env 块必须引用 {secret}, "
                f"实际 env keys: {list(env_block.keys())}"
            )

    def test_step_env_uses_secrets_context(self):
        """step env 值必须从 ${{ secrets.* }} 上下文读取。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            if step.get("name") == "Generate minimal secrets placeholders":
                generate_step = step
                break
        assert generate_step is not None
        env_block = generate_step["env"]
        for secret, value in env_block.items():
            assert isinstance(value, str) and "${{ secrets." in value, (
                f"R72 RC64: step env[{secret}] 必须从 secrets 上下文读取, "
                f"实际值: {value}"
            )


# ════════════════════════════════════════════════════════════════
# C. R2_ENDPOINT 硬约束:必须是纯主机名,不含 https:// 前缀
# ════════════════════════════════════════════════════════════════


class TestR2EndpointHostnameConstraint:
    """R72 RC64: R2_ENDPOINT 必须是纯主机名(用户硬约束)。

    用户硬约束: "R2_ENDPOINT in .env must be a pure hostname
    (no https:// prefix or backticks)"。这条规则也适用于 CI 注入的值。
    """

    def test_r2_endpoint_value_does_not_contain_https_prefix(self):
        """R2_ENDPOINT 值不能包含 https:// 前缀。"""
        source = _read_workflow()
        # 在 compose-runtime-e2e job 区块内检查
        idx = source.find("compose-runtime-e2e:")
        assert idx >= 0
        next_job_match = re.search(r"\n  [a-z][a-z0-9-]*:", source[idx + 30:])
        if next_job_match:
            job_block = source[idx: idx + 30 + next_job_match.start()]
        else:
            job_block = source[idx:]
        # 验证 .env.shared 和 .env.secrets.db_backup 注入时未硬编码 https://
        bad_pattern = r'echo\s+"R2_ENDPOINT=https?://'
        assert not re.search(bad_pattern, job_block), (
            f"R72 RC64: R2_ENDPOINT 注入不得包含 https:// 前缀, "
            f"匹配到: {re.search(bad_pattern, job_block)}"
        )

    def test_r2_endpoint_value_uses_secret_reference(self):
        """R2_ENDPOINT 应通过 ${{ secrets.R2_ENDPOINT }} 引用,不硬编码。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            if step.get("name") == "Generate minimal secrets placeholders":
                generate_step = step
                break
        assert generate_step is not None
        env_block = generate_step["env"]
        assert "R2_ENDPOINT" in env_block
        assert "${{ secrets.R2_ENDPOINT }}" in env_block["R2_ENDPOINT"], (
            f"R72 RC64: R2_ENDPOINT 必须从 secrets 上下文读取, "
            f"实际值: {env_block['R2_ENDPOINT']}"
        )


# ════════════════════════════════════════════════════════════════
# D. backup_restore 阶段关联性验证
# ════════════════════════════════════════════════════════════════


class TestBackupRestorePhaseUsesR2Credentials:
    """R72 RC64: backup_restore 阶段(通过 backup_once)间接使用 R2 凭证。"""

    def test_compose_runtime_e2e_uses_rc_tag_trigger(self):
        """compose-runtime-e2e 必须仅在 rc-v* tag 触发(需要真实凭证)。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        if_expr = job.get("if", "")
        assert "rc-v" in if_expr, (
            f"R72 RC64: compose-runtime-e2e 必须仅在 rc-v* tag 触发, "
            f"实际 if: {if_expr}"
        )

    def test_compose_runtime_e2e_uses_rc_candidate_environment(self):
        """compose-runtime-e2e 必须使用 rc-candidate environment(secrets 审计)。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        env_name = job.get("environment", "")
        assert env_name == "rc-candidate", (
            f"R72 RC64: compose-runtime-e2e 必须使用 rc-candidate environment, "
            f"实际: {env_name}"
        )
