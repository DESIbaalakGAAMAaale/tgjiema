"""R76 §10.F: compose-runtime-e2e secretless 凭证注入测试套件。

历史背景:
    R72 RC64 曾要求 compose-runtime-e2e backup_restore 阶段必须配置真实
    R2 凭证(不能 mock)。R76 §10.F 已显式废弃该要求,改为 secretless 模式:

      - 删除: TEST_*BOT_TOKEN、R2 生产 secret、生产 CRDB secret 作为
        PR/普通 push 必需输入。
      - 替换: docker compose -f docker-compose.yml -f docker-compose.secretless.yml
        overlay;job 内生成临时 MinIO/HMAC/restore key 并立即 mask;
        CRDB 使用本地 insecure 实例;所有 secretless 服务限定到临时
        network 和 volume,job 结束销毁。

    参见 docs/tgjiema R76 正式上线前全仓终审与一次性整改报告 §10.A / §10.F。

测试策略:
    - 静态解析 release-gates.yml 验证 secretless 注入逻辑(不依赖 GitHub Actions 运行)
    - 严格验证不引用任何真实 R2/CRDB/Bot token secret
    - 严格验证 R2_ENDPOINT 不出现(已替换为 S3_ENDPOINT_URL=http://minio:9000)
    - 严格遵守 R76 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

# R76 §10.F: 必须注入到 .env.shared 的 secretless 环境变量
# (替代 R72 RC64 的 R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
#  R2_BUCKET_NAME / R2_ENDPOINT / BACKUP_KEK 真实生产凭证)
REQUIRED_SECRETLESS_ENV_VARS = [
    "OBJECT_STORAGE_BACKEND",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET_NAME",
    "BACKUP_SIGNING_KEY",
    "COCKROACHDB_URL",
    "APP_ENV",
    "SECRETLESS_MODE",
    "PROVIDER_BACKEND",
    "PROVIDER_BASE_URL",
    "PROVIDER_CONTRACT_TOKEN",
]

# R76 §10.F: 禁止在 compose-runtime-e2e 引用的真实生产 secret
# (R72 RC64 旧要求已废弃,出现即违规)
FORBIDDEN_REAL_SECRETS = [
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "R2_ENDPOINT",
    "BACKUP_KEK",
    "COCKROACHDB_URL",  # 真实生产 CRDB secret(secrets.COCKROACHDB_*)
]


def _read_workflow() -> str:
    """读取 release-gates.yml 源码。"""
    return RELEASE_GATES_PATH.read_text(encoding="utf-8")


def _parse_workflow() -> dict:
    """解析 release-gates.yml 为 dict。"""
    with RELEASE_GATES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _extract_compose_runtime_e2e_block(source: str) -> str:
    """提取 compose-runtime-e2e job 区块源码。

    从 `compose-runtime-e2e:` 起到下一个顶层 job 或文件末尾。
    """
    idx = source.find("compose-runtime-e2e:")
    assert idx >= 0, "R76 §10.F: 未找到 compose-runtime-e2e job 定义"
    # 匹配下一个顶层 job(2 空格缩进 + 标识符 + 冒号)
    next_job_match = re.search(r"\n  [a-z][a-z0-9-]*:", source[idx + 30:])
    if next_job_match:
        return source[idx: idx + 30 + next_job_match.start()]
    return source[idx:]


# ════════════════════════════════════════════════════════════════
# A. release-gates.yml 必须包含 secretless 凭证注入逻辑
# ════════════════════════════════════════════════════════════════


class TestSecretlessCredentialsInjectionStep:
    """R76 §10.F: compose-runtime-e2e job 必须注入 secretless 凭证到 .env.shared。

    替代 R72 RC64 TestR2CredentialsInjectionStep(已废弃)。
    """

    def test_release_gates_file_exists(self):
        """release-gates.yml 必须存在。"""
        assert RELEASE_GATES_PATH.is_file(), (
            f"R76 §10.F: release-gates.yml 必须存在: {RELEASE_GATES_PATH}"
        )

    def test_compose_runtime_e2e_job_exists(self):
        """compose-runtime-e2e job 必须在 workflow 中定义。"""
        workflow = _parse_workflow()
        jobs = workflow.get("jobs", {})
        assert "compose-runtime-e2e" in jobs, (
            "R76 §10.F: release-gates.yml 必须包含 compose-runtime-e2e job"
        )

    def test_secretless_credentials_injected_into_env_shared(self):
        """secretless 凭证必须被注入到 .env.shared(供 db_restore / db_backup 读取)。

        R76 §10.F: MinIO + 本地 CRDB + provider-sim 凭证写入 .env.shared,
        替代 R72 RC64 的真实 R2 凭证注入。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)

        for var in REQUIRED_SECRETLESS_ENV_VARS:
            # 期望格式: echo "VAR=..." >> .env.shared 或 > .env.shared
            # (COCKROACHDB_URL 首次写入用 > 覆盖,其余变量用 >> 追加)
            pattern = rf'echo\s+"{var}=[^"]*"\s+>>?\s+\.env\.shared'
            match = re.search(pattern, job_block)
            assert match, (
                f"R76 §10.F: compose-runtime-e2e job 必须将 {var} 注入到 .env.shared, "
                f"期望匹配: {pattern}"
            )

    def test_generate_secretless_credentials_step_exists(self):
        """Generate secretless CI credentials 步骤必须存在。

        R76 §10.F: 替代 R72 RC64 的 'Generate minimal secrets placeholders' 步骤,
        生成单次 run 临时凭证(MinIO root / provider token / backup signing key),
        job 结束销毁。
        """
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            name = step.get("name", "")
            if "Generate secretless CI credentials" in name:
                generate_step = step
                break
        assert generate_step is not None, (
            "R76 §10.F: 未找到 'Generate secretless CI credentials' step "
            "(替代 R72 RC64 'Generate minimal secrets placeholders')"
        )

    def test_minio_credentials_use_runtime_generated_values(self):
        """MinIO 凭证必须使用运行时生成的临时值,不引用 secrets 上下文。

        R76 §10.F: CI_MINIO_ROOT_USER / CI_MINIO_ROOT_PASSWORD 在 job 内
        通过 openssl rand 生成,导出到 GITHUB_ENV 后供后续 step 使用。
        禁止读取 secrets.R2_* / secrets.MINIO_*。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)

        # 验证 MinIO 凭证引用 ${{ env.CI_MINIO_* }}(运行时生成)
        # 注:实际 workflow 使用 shell 变量 ${CI_MINIO_ROOT_USER} 而非
        # ${{ env.* }} 表达式,因为它们在同一个 run 块内
        assert "CI_MINIO_ROOT_USER=" in job_block, (
            "R76 §10.F: 必须生成 CI_MINIO_ROOT_USER 临时凭证"
        )
        assert "CI_MINIO_ROOT_PASSWORD=" in job_block, (
            "R76 §10.F: 必须生成 CI_MINIO_ROOT_PASSWORD 临时凭证"
        )
        # 必须使用 openssl rand 生成随机凭证
        assert "openssl rand" in job_block, (
            "R76 §10.F: 临时凭证必须使用 openssl rand 生成(非硬编码)"
        )
        # 必须 mask 临时凭证
        assert "::add-mask::" in job_block, (
            "R76 §10.F: 临时凭证必须通过 ::add-mask:: 屏蔽日志输出"
        )

    def test_object_storage_backend_is_minio(self):
        """OBJECT_STORAGE_BACKEND 必须为 minio(替代 R2)。

        R76 §10.F: secretless 模式下使用 MinIO 作为 S3 兼容对象存储,
        禁止使用 R2 后端(需真实生产凭证)。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        pattern = r'echo\s+"OBJECT_STORAGE_BACKEND=minio"\s+>>\s+\.env\.shared'
        assert re.search(pattern, job_block), (
            "R76 §10.F: OBJECT_STORAGE_BACKEND 必须设为 minio"
        )

    def test_s3_endpoint_url_points_to_local_minio(self):
        """S3_ENDPOINT_URL 必须指向本地 MinIO 容器(http://minio:9000)。

        R76 §10.F: 禁止使用 https:// 前缀,禁止指向真实 R2 endpoint。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        pattern = r'echo\s+"S3_ENDPOINT_URL=http://minio:9000"\s+>>\s+\.env\.shared'
        assert re.search(pattern, job_block), (
            "R76 §10.F: S3_ENDPOINT_URL 必须指向 http://minio:9000(本地 MinIO)"
        )

    def test_crdb_url_uses_insecure_local_instance(self):
        """COCKROACHDB_URL 必须使用本地 insecure CRDB 实例。

        R76 §10.F: 禁止读取生产 CRDB secret,使用本地 insecure 实例(仅 CI 网络)。
        R76 §10.M: CRDB v24.1 insecure 模式强制 listen-addr port=26257,
        sql-addr 用 26258 避免与 listen-addr 同 host:port 冲突,客户端用 26258。
        R79 §10.2: 单 CRDB 拓扑 — 服务名为 cockroachdb(覆盖基础服务键),
        不再使用第二套 cockroachdb-secretless 服务。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        # 期望: postgresql://root@cockroachdb:26258/...?sslmode=disable
        pattern = r'cockroachdb:26258.*sslmode=disable'
        assert re.search(pattern, job_block), (
            "R76 §10.F / R79 §10.2: COCKROACHDB_URL 必须使用 cockroachdb:26258 "
            "insecure 实例(sslmode=disable)"
        )
        # R79 §10.2: 双 CRDB 拓扑禁止回归
        assert "cockroachdb-secretless" not in job_block, (
            "R79 §10.2: 不得再引用 cockroachdb-secretless — "
            "单 CRDB 拓扑使用基础服务键 cockroachdb"
        )

    def test_secretless_crdb_identity_exported_before_compose_render(self):
        """完整 Compose 图渲染前必须导出 isolated CRDB identity。

        Docker Compose 会先插值 merged graph 的全部服务，再选择要启动的
        infrastructure targets；因此只写 .env.shared 中的 COCKROACHDB_URL 不够。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        required_exports = {
            "SECRETLESS_CRDB_HOST": "cockroachdb",
            "SECRETLESS_CRDB_SQL_PORT": "26258",
            "SECRETLESS_CRDB_DATABASE": "tgjiema",
            "SECRETLESS_CRDB_URL": (
                "postgresql://root@cockroachdb:26258/tgjiema?sslmode=disable"
            ),
        }
        for name, value in required_exports.items():
            expected = f'echo "{name}={value}" >> "$GITHUB_ENV"'
            assert expected in job_block, (
                f"R83 RC runtime: compose-runtime-e2e 必须在 compose config 前导出 {name}"
            )

        export_pos = job_block.index('echo "SECRETLESS_CRDB_HOST=cockroachdb"')
        render_pos = job_block.index(
            "docker compose -f docker-compose.yml -f docker-compose.secretless.yml"
        )
        assert export_pos < render_pos, (
            "R83 RC runtime: SECRETLESS_CRDB_* 必须在 merged Compose graph 渲染前导出"
        )

    def test_crdb_version_probe_uses_native_cli_contract(self):
        """CRDB 就绪后的版本探针不得混用 psql 选项或截断管道。

        CockroachDB CLI 不支持 PostgreSQL psql 的 ``-t``；在
        ``set -o pipefail`` 下使用 ``head`` 截断输出也可能以 SIGPIPE/rc=4
        将健康基础设施误判失败。探针必须完整捕获输出并验证非空。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        probe_start = job_block.index("if ! CRDB_VERSION_OUTPUT=$(docker compose")
        probe_end = job_block.index('echo "=== Secretless infrastructure ready ==="')
        probe = job_block[probe_start:probe_end]

        assert 'cockroach sql --insecure --host=localhost:26258' in probe
        assert '-e "SELECT version();"' in probe
        assert " -t " not in probe
        assert "| head" not in probe
        assert 'if [ -z "${CRDB_VERSION_OUTPUT//[[:space:]]/}" ]; then' in probe
        assert 'if ! CRDB_VERSION_OUTPUT=$(docker compose' in probe
        assert 'echo "::error::CockroachDB version probe failed"' in probe
        assert 'echo "::error::CockroachDB version probe returned empty output"' in probe
        assert probe.count("exit 1") >= 2

    def test_runtime_e2e_dependencies_installed_before_execution(self):
        """RC runtime job 必须在执行编排器前安装并探测 runtime 依赖。"""
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        install_pos = job_block.index(
            "Install Compose Runtime E2E dependencies (fail-closed)"
        )
        requirements_pos = job_block.index(
            "python -m pip install -r requirements.txt", install_pos
        )
        import_probe_pos = job_block.index(
            'python -c "import loguru;', requirements_pos
        )
        execute_pos = job_block.index(
            "python scripts/compose_runtime_e2e.py", import_probe_pos
        )

        assert install_pos < requirements_pos < import_probe_pos < execute_pos
        dependency_step = job_block[install_pos:execute_pos]
        assert "set -euo pipefail" in dependency_step
        assert "continue-on-error" not in dependency_step
        assert "|| true" not in dependency_step

    def test_secretless_mode_flag_enabled(self):
        """SECRETLESS_MODE=true 必须写入 .env.shared。

        R76 §10.F: secretless 模式标识,production 不允许此标志
        (由 settings.py validator 强制)。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        pattern = r'echo\s+"SECRETLESS_MODE=true"\s+>>\s+\.env\.shared'
        assert re.search(pattern, job_block), (
            "R76 §10.F: SECRETLESS_MODE=true 必须写入 .env.shared"
        )

    def test_app_env_is_test(self):
        """APP_ENV 必须为 test(允许 contract provider + minio backend)。

        R76 §10.F: production 不允许 SECRETLESS_MODE=true / contract / minio。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        pattern = r'echo\s+"APP_ENV=test"\s+>>\s+\.env\.shared'
        assert re.search(pattern, job_block), (
            "R76 §10.F: APP_ENV 必须为 test(secretless 模式不允许 production)"
        )


# ════════════════════════════════════════════════════════════════
# B. 禁止引用真实生产 secret(R76 §10.F 负向验收)
# ════════════════════════════════════════════════════════════════


class TestNoRealProductionSecretsReferenced:
    """R76 §10.F: compose-runtime-e2e job 禁止引用任何真实生产 secret。

    R72 RC64 旧要求(R2_ACCOUNT_ID / R2_ACCESS_KEY_ID 等真实凭证)已废弃,
    出现 ${{ secrets.R2_* }} / ${{ secrets.COCKROACHDB_* }} 即违规。
    """

    def test_no_r2_secrets_referenced(self):
        """禁止引用 ${{ secrets.R2_* }} 真实 R2 生产凭证。"""
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        # 检查 ${{ secrets.R2_* }} 引用
        r2_secret_refs = re.findall(r'\$\{\{\s*secrets\.R2_\w+\s*\}\}', job_block)
        assert not r2_secret_refs, (
            f"R76 §10.F: compose-runtime-e2e job 禁止引用真实 R2 secret, "
            f"发现: {r2_secret_refs}"
        )

    def test_no_cockroachdb_secrets_referenced(self):
        """禁止引用 ${{ secrets.COCKROACHDB_* }} 真生产 CRDB 凭证。

        R76 §10.F: CRDB 使用本地 insecure 实例,不读取生产 key。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        crdb_secret_refs = re.findall(
            r'\$\{\{\s*secrets\.COCKROACHDB_\w+\s*\}\}', job_block,
        )
        assert not crdb_secret_refs, (
            f"R76 §10.F: compose-runtime-e2e job 禁止引用真实 COCKROACHDB secret, "
            f"发现: {crdb_secret_refs}"
        )

    def test_no_test_bot_token_secrets_referenced(self):
        """禁止引用 ${{ secrets.TEST_*BOT_TOKEN }} 真实 Bot token。

        R76 §10.F: secretless 模式下 bot token 使用固定 ci-local-token,
        provider-sim 只接受 ci-local-token,不访问公网。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        bot_token_refs = re.findall(
            r'\$\{\{\s*secrets\.(?:TEST_\w*BOT_TOKEN|UP_BOT_TOKEN|IDX_BOT_TOKEN|'
            r'DSP_BOT_TOKEN|MON_BOT_TOKEN|ADMIN_BOT_TOKEN)\s*\}\}',
            job_block,
        )
        assert not bot_token_refs, (
            f"R76 §10.F: compose-runtime-e2e job 禁止引用真实 Bot token secret, "
            f"发现: {bot_token_refs}"
        )

    def test_no_r2_endpoint_https_prefix(self):
        """R2_ENDPOINT(若出现)不得含 https:// 前缀(用户硬约束)。

        R76 §10.F: 实际已用 S3_ENDPOINT_URL=http://minio:9000 替代,
        但仍需确保不出现 R2_ENDPOINT=https://... 硬编码。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        bad_pattern = r'echo\s+"R2_ENDPOINT=https?://'
        assert not re.search(bad_pattern, job_block), (
            f"R76 §10.F / 用户硬约束: R2_ENDPOINT 不得含 https:// 前缀, "
            f"匹配到: {re.search(bad_pattern, job_block)}"
        )

    def test_no_warning_skip_on_missing_secret(self):
        """缺 secret 时禁止 warning / skip / 伪 PASS。

        R76 §10.F: 删除"缺 secret 时 warning、skip 或伪 PASS"分支。
        所有外部边界使用协议兼容模拟服务,无需真实凭证。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        # 检查 warning/skip/continue-on-error 模式
        bad_patterns = [
            r'::warning::.*secret',
            r'continue-on-error:\s*true',
            r'if:\s*failure\(\).*\n\s*continue-on-error',
        ]
        for pattern in bad_patterns:
            assert not re.search(pattern, job_block, re.IGNORECASE), (
                f"R76 §10.F: 禁止缺 secret 时 warning/skip/伪 PASS, "
                f"匹配模式 {pattern}"
            )


# ════════════════════════════════════════════════════════════════
# C. backup_restore 阶段关联性验证
# ════════════════════════════════════════════════════════════════


class TestBackupRestorePhaseUsesSecretlessCredentials:
    """R76 §10.F: backup_restore 阶段(通过 backup_once)使用 secretless 凭证。"""

    def test_compose_runtime_e2e_uses_rc_tag_trigger(self):
        """compose-runtime-e2e 必须仅在 rc-v* tag 触发(需 secretless 凭证)。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        if_expr = job.get("if", "")
        assert "rc-v" in if_expr, (
            f"R76 §10.F: compose-runtime-e2e 必须仅在 rc-v* tag 触发, "
            f"实际 if: {if_expr}"
        )

    def test_compose_runtime_e2e_uses_rc_candidate_environment(self):
        """compose-runtime-e2e 必须使用 rc-candidate environment(secrets 审计)。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        env_name = job.get("environment", "")
        assert env_name == "rc-candidate", (
            f"R76 §10.F: compose-runtime-e2e 必须使用 rc-candidate environment, "
            f"实际: {env_name}"
        )

    def test_compose_runtime_e2e_uses_secretless_compose_overlay(self):
        """必须使用 docker-compose.secretless.yml overlay 启动隔离测试环境。

        R76 §10.F: 使用 docker compose -f docker-compose.yml -f docker-compose.secretless.yml
        提供 provider-sim/MinIO/CRDB,不读取任何真实外部凭证。
        """
        source = _read_workflow()
        job_block = _extract_compose_runtime_e2e_block(source)
        assert "docker-compose.secretless.yml" in job_block, (
            "R76 §10.F: 必须使用 docker-compose.secretless.yml overlay "
            "启动 secretless 隔离测试环境"
        )
        # 验证使用 -f 参数组合
        pattern = r'docker\s+compose\s+-f\s+docker-compose\.yml\s+-f\s+docker-compose\.secretless\.yml'
        assert re.search(pattern, job_block), (
            "R76 §10.F: 必须使用 `docker compose -f docker-compose.yml "
            "-f docker-compose.secretless.yml` 组合命令"
        )
