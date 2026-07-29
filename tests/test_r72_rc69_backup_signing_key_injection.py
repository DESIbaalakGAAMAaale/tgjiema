"""R72 RC69: compose-runtime-e2e BACKUP_SIGNING_KEY 注入测试套件。

R72 RC69 整改背景:
    R72 报告 P0-10 要求 compose-runtime-e2e backup_restore 阶段必须完成
    backup → restore round-trip 验证。RC68 已修复 capability-seal 问题
    (ALLOW_LEGACY_RESTORE=1),但 restore 步骤在 capability-seal 通过后
    仍 fail-closed,错误信息:
        R63 P0-06: BACKUP_SIGNING_KEY 未配置,无法验证 COMPLETE marker 签名。
        AppError: 备份恢复信任链令牌缺失或无效,已拒绝恢复

    根因:
        backup_once() 用 BACKUP_SIGNING_KEY 对 COMPLETE marker 做 HMAC 签名;
        restore 通过同一密钥验证签名。两者必须使用相同密钥才能完成 round-trip。
        RC64 已注入 R2 凭证和 BACKUP_KEK,但漏注入 BACKUP_SIGNING_KEY,
        导致 strict service validate_backup_completeness() 在 signing_key 为空时
        返回 invalid(fail-closed,不允许跳过验签)。

RC69 修复:
    1. 在 .env.shared 注入 BACKUP_SIGNING_KEY(db_restore 走 db_writer 服务,
       只加载 .env.shared)
    2. 在 .env.secrets.db_backup 冗余注入 BACKUP_SIGNING_KEY
    3. step env 块新增 BACKUP_SIGNING_KEY: ${{ secrets.BACKUP_SIGNING_KEY }}
    4. 生成 32 字节十六进制 HMAC 密钥,通过 gh secret set 添加到 GitHub Secrets

测试策略:
    - 静态解析 release-gates.yml 验证注入逻辑(不依赖 GitHub Actions 运行)
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

# RC69 新增的必需 secret
REQUIRED_RC69_SECRET = "BACKUP_SIGNING_KEY"


def _read_workflow() -> str:
    """读取 release-gates.yml 源码。"""
    return RELEASE_GATES_PATH.read_text(encoding="utf-8")


def _parse_workflow() -> dict:
    """解析 release-gates.yml 为 dict。"""
    with RELEASE_GATES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _extract_compose_runtime_e2e_job_block() -> str:
    """提取 compose-runtime-e2e job 的完整 YAML 文本块。

    用于在 job 区块内做正则匹配,避免误匹配其他 job。
    """
    source = _read_workflow()
    idx = source.find("compose-runtime-e2e:")
    assert idx >= 0, "R72 RC69: 未找到 compose-runtime-e2e job 定义"
    # 取到下一个顶层 job 或文件末尾
    next_job_match = re.search(r"\n  [a-z][a-z0-9-]*:", source[idx + 30:])
    if next_job_match:
        return source[idx: idx + 30 + next_job_match.start()]
    return source[idx:]


# ════════════════════════════════════════════════════════════════
# A. release-gates.yml 必须包含 BACKUP_SIGNING_KEY 注入逻辑
# ════════════════════════════════════════════════════════════════


class TestBackupSigningKeyInjectionStep:
    """R72 RC69: compose-runtime-e2e job 必须注入 BACKUP_SIGNING_KEY。"""

    def test_release_gates_file_exists(self):
        """release-gates.yml 必须存在。"""
        assert RELEASE_GATES_PATH.is_file(), (
            f"R72 RC69: release-gates.yml 必须存在: {RELEASE_GATES_PATH}"
        )

    def test_compose_runtime_e2e_job_exists(self):
        """compose-runtime-e2e job 必须在 workflow 中定义。"""
        workflow = _parse_workflow()
        jobs = workflow.get("jobs", {})
        assert "compose-runtime-e2e" in jobs, (
            "R72 RC69: release-gates.yml 必须包含 compose-runtime-e2e job"
        )

    def test_backup_signing_key_injected_into_env_shared(self):
        """BACKUP_SIGNING_KEY 必须被注入到 .env.shared(供 db_restore 读取)。

        db_restore 通过 `docker compose run db_writer` 执行,db_writer 的
        env_file 只有 .env.shared,所以 BACKUP_SIGNING_KEY 必须写入 .env.shared。
        缺失会导致:
            R63 P0-06: BACKUP_SIGNING_KEY 未配置,无法验证 COMPLETE marker 签名。
            AppError: 备份恢复信任链令牌缺失或无效
        """
        job_block = _extract_compose_runtime_e2e_job_block()
        pattern = r'echo\s+"BACKUP_SIGNING_KEY=\$\{BACKUP_SIGNING_KEY\}"\s+>>\s+\.env\.shared'
        assert re.search(pattern, job_block), (
            "R72 RC69: compose-runtime-e2e job 必须将 BACKUP_SIGNING_KEY 注入到 "
            f".env.shared, 期望匹配: {pattern}"
        )

    def test_backup_signing_key_injected_into_db_backup_secrets(self):
        """BACKUP_SIGNING_KEY 必须冗余注入到 .env.secrets.db_backup。"""
        job_block = _extract_compose_runtime_e2e_job_block()
        pattern = (
            r'echo\s+"BACKUP_SIGNING_KEY=\$\{BACKUP_SIGNING_KEY\}"\s+>>\s+'
            r'\.env\.secrets\.db_backup'
        )
        assert re.search(pattern, job_block), (
            "R72 RC69: compose-runtime-e2e job 必须将 BACKUP_SIGNING_KEY 冗余注入到 "
            f".env.secrets.db_backup, 期望匹配: {pattern}"
        )

    def test_backup_signing_key_comment_documents_round_trip(self):
        """BACKUP_SIGNING_KEY 注入处必须有注释说明 round-trip 用途。

        防止后续维护者误删此注入(以为是冗余配置)。
        """
        job_block = _extract_compose_runtime_e2e_job_block()
        # 在 BACKUP_SIGNING_KEY 注入附近查找注释
        idx = job_block.find("BACKUP_SIGNING_KEY=${BACKUP_SIGNING_KEY}")
        assert idx >= 0, "R72 RC69: 未找到 BACKUP_SIGNING_KEY 注入"
        # 查找注入前 200 字符内是否包含 "round-trip" 或 "签名" 关键词
        preceding = job_block[max(0, idx - 300): idx]
        assert (
            "round-trip" in preceding or "签名" in preceding or "HMAC" in preceding
        ), (
            "R72 RC69: BACKUP_SIGNING_KEY 注入处必须有注释说明 "
            "round-trip/HMAC/签名 用途,实际前文: " + preceding[-200:]
        )


# ════════════════════════════════════════════════════════════════
# B. step env 块必须声明 BACKUP_SIGNING_KEY secrets 引用
# ════════════════════════════════════════════════════════════════


class TestStepEnvBackupSigningKeyReference:
    """R72 RC69: step env 块必须声明 BACKUP_SIGNING_KEY 引用。"""

    def test_step_env_block_contains_backup_signing_key(self):
        """Generate secretless CI credentials step 的 env 块必须包含 BACKUP_SIGNING_KEY。

        R76 §10.F: 步骤名从 'Generate minimal secrets placeholders' 重命名为
        'Generate secretless CI credentials (single-run temporary)',但 env 块
        仍需声明 BACKUP_SIGNING_KEY(非 secretless 模式从 secrets 读取,
        secretless 模式由脚本生成 CI_BACKUP_SIGNING_KEY 覆盖)。
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
            "R72 RC69 / R76 §10.F: 未找到 'Generate secretless CI credentials' step "
            "(替代 R72 RC64 'Generate minimal secrets placeholders')"
        )
        assert "env" in generate_step, (
            "R72 RC69: 'Generate secretless CI credentials' step 必须有 env 块"
        )
        env_block = generate_step["env"]
        assert REQUIRED_RC69_SECRET in env_block, (
            f"R72 RC69: step env 块必须引用 {REQUIRED_RC69_SECRET}, "
            f"实际 env keys: {list(env_block.keys())}"
        )

    def test_step_env_backup_signing_key_uses_secrets_context(self):
        """BACKUP_SIGNING_KEY 必须从 ${{ secrets.* }} 上下文读取。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            name = step.get("name", "")
            if "Generate secretless CI credentials" in name:
                generate_step = step
                break
        assert generate_step is not None
        env_block = generate_step["env"]
        value = env_block[REQUIRED_RC69_SECRET]
        assert isinstance(value, str) and "${{ secrets." in value, (
            f"R72 RC69: step env[{REQUIRED_RC69_SECRET}] 必须从 secrets 上下文读取, "
            f"实际值: {value}"
        )

    def test_step_env_backup_signing_key_references_correct_secret(self):
        """BACKUP_SIGNING_KEY 必须引用同名 GitHub Secret。"""
        workflow = _parse_workflow()
        job = workflow["jobs"]["compose-runtime-e2e"]
        steps = job["steps"]
        generate_step = None
        for step in steps:
            name = step.get("name", "")
            if "Generate secretless CI credentials" in name:
                generate_step = step
                break
        assert generate_step is not None
        env_block = generate_step["env"]
        value = env_block[REQUIRED_RC69_SECRET]
        expected = "${{ secrets.BACKUP_SIGNING_KEY }}"
        assert value == expected, (
            f"R72 RC69: step env[{REQUIRED_RC69_SECRET}] 必须为 {expected}, "
            f"实际值: {value}"
        )


# ════════════════════════════════════════════════════════════════
# C. 源码层验证: db_restore 严格 fail-closed 在 signing_key 缺失时
# ════════════════════════════════════════════════════════════════


class TestDbRestoreFailsClosedWithoutSigningKey:
    """R72 RC69: db_restore.run_restore() 必须在 BACKUP_SIGNING_KEY 缺失时 fail-closed。

    这是 RC69 注入 BACKUP_SIGNING_KEY 的根本原因 — 如果不注入,
    db_restore.run_restore() 会因 strict service 验签失败而拒绝恢复。
    """

    def test_db_restore_checks_signing_key_presence(self):
        """db_restore.run_restore() 必须检查 BACKUP_SIGNING_KEY 非空。"""
        db_restore_path = REPO_ROOT / "services" / "db_restore.py"
        assert db_restore_path.is_file(), (
            f"R72 RC69: db_restore.py 必须存在: {db_restore_path}"
        )
        source = db_restore_path.read_text(encoding="utf-8")
        # 验证源码包含 signing_key 缺失检查
        assert "BACKUP_SIGNING_KEY" in source, (
            "R72 RC69: db_restore.py 必须引用 BACKUP_SIGNING_KEY"
        )
        # 验证 fail-closed 路径:signing_key 为空时 raise AppError
        assert "BACKUP_RESTORE_TRUST_CHAIN_REQUIRED" in source, (
            "R72 RC69: db_restore.py 必须在 signing_key 缺失时 "
            "raise BACKUP_RESTORE_TRUST_CHAIN_REQUIRED"
        )

    def test_db_backup_uses_signing_key_for_complete_marker(self):
        """db_backup 必须使用 BACKUP_SIGNING_KEY 签名 COMPLETE marker。"""
        db_backup_path = REPO_ROOT / "services" / "db_backup.py"
        assert db_backup_path.is_file(), (
            f"R72 RC69: db_backup.py 必须存在: {db_backup_path}"
        )
        source = db_backup_path.read_text(encoding="utf-8")
        # 验证 db_backup 从 settings 读取 BACKUP_SIGNING_KEY
        assert "BACKUP_SIGNING_KEY" in source, (
            "R72 RC69: db_backup.py 必须引用 BACKUP_SIGNING_KEY"
        )
        # 验证传给 strict service 用于 COMPLETE marker 签名
        assert "signing_key" in source, (
            "R72 RC69: db_backup.py 必须将 BACKUP_SIGNING_KEY 作为 signing_key 传入"
        )

    def test_backup_dr_validate_strict_signing_key_required(self):
        """backup_dr_validate.py 必须强制 signing_key 参数(fail-closed)。"""
        validate_path = REPO_ROOT / "services" / "backup_dr_validate.py"
        assert validate_path.is_file(), (
            f"R72 RC69: backup_dr_validate.py 必须存在: {validate_path}"
        )
        source = validate_path.read_text(encoding="utf-8")
        # R59 P0-04: signing_key 为空时返回 invalid(fail-closed, no skip verify)
        assert "signing_key is required" in source or "signing_key 缺失" in source, (
            "R72 RC69: backup_dr_validate.py 必须在 signing_key 为空时返回 invalid"
        )


# ════════════════════════════════════════════════════════════════
# D. Settings 类必须定义 BACKUP_SIGNING_KEY 字段
# ════════════════════════════════════════════════════════════════


class TestSettingsDefinesBackupSigningKeyField:
    """R72 RC69: config/settings.py 必须定义 BACKUP_SIGNING_KEY 字段。

    根因: pydantic Settings 类不会加载未定义的字段(即使 .env 中有此变量)。
    RC69 第一版只注入了环境变量,但未在 Settings 中定义字段,
    导致 getattr(settings, "BACKUP_SIGNING_KEY", b"") 仍返回空值。
    """

    def test_settings_file_exists(self):
        """config/settings.py 必须存在。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        assert settings_path.is_file(), (
            f"R72 RC69: config/settings.py 必须存在: {settings_path}"
        )

    def test_settings_defines_backup_signing_key_field(self):
        """Settings 类必须显式定义 BACKUP_SIGNING_KEY 字段。"""
        settings_path = REPO_ROOT / "config" / "settings.py"
        source = settings_path.read_text(encoding="utf-8")
        # 匹配 `BACKUP_SIGNING_KEY: str = ""` 或类似定义
        pattern = r"BACKUP_SIGNING_KEY\s*:\s*str\s*=\s*"
        assert re.search(pattern, source), (
            "R72 RC69: config/settings.py 必须定义 BACKUP_SIGNING_KEY: str = ... 字段,"
            "否则 pydantic 不会从环境变量加载此值"
        )


# ════════════════════════════════════════════════════════════════
# E. 调用方必须将 str 转换为 bytes(hmac.new 要求 bytes)
# ════════════════════════════════════════════════════════════════


class TestSigningKeyStrToBytesConversion:
    """R72 RC69: 调用方必须将 BACKUP_SIGNING_KEY 从 str 转换为 bytes。

    根因: Settings 中定义为 str(环境变量总是 str),但 hmac.new(key, ...)
    要求 key 为 bytes。如果不做 encode 转换,backup 签名时会抛 TypeError。
    """

    def test_db_backup_encodes_signing_key_to_bytes(self):
        """db_backup.py 必须将 BACKUP_SIGNING_KEY 从 str 转换为 bytes。"""
        db_backup_path = REPO_ROOT / "services" / "db_backup.py"
        source = db_backup_path.read_text(encoding="utf-8")
        # 验证存在 encode("utf-8") 转换
        assert "encode(\"utf-8\")" in source or "encode('utf-8')" in source, (
            "R72 RC69: db_backup.py 必须将 BACKUP_SIGNING_KEY (str) "
            "encode 为 bytes(hmac.new 要求 bytes)"
        )
        # 验证不再直接使用 b"" 默认值(应该用 str 默认值 "")
        # 旧代码: getattr(settings, "BACKUP_SIGNING_KEY", b"") or b""
        # 新代码: getattr(settings, "BACKUP_SIGNING_KEY", "") or ""
        old_pattern = r'getattr\(settings,\s*"BACKUP_SIGNING_KEY",\s*b""\)\s*or\s*b""'
        assert not re.search(old_pattern, source), (
            "R72 RC69: db_backup.py 不得再使用 `getattr(..., b'') or b''` 旧模式,"
            "应改为 str 默认值 + encode 转换"
        )

    def test_db_restore_encodes_signing_key_to_bytes(self):
        """db_restore.py 必须将 BACKUP_SIGNING_KEY 从 str 转换为 bytes。"""
        db_restore_path = REPO_ROOT / "services" / "db_restore.py"
        source = db_restore_path.read_text(encoding="utf-8")
        assert "encode(\"utf-8\")" in source or "encode('utf-8')" in source, (
            "R72 RC69: db_restore.py 必须将 BACKUP_SIGNING_KEY (str) "
            "encode 为 bytes(hmac.new 要求 bytes)"
        )
        # 旧代码模式不应再存在
        old_pattern = r'getattr\(settings,\s*"BACKUP_SIGNING_KEY",\s*b""\)\s*or\s*b""'
        assert not re.search(old_pattern, source), (
            "R72 RC69: db_restore.py 不得再使用 `getattr(..., b'') or b''` 旧模式,"
            "应改为 str 默认值 + encode 转换"
        )
