#!/usr/bin/env python3
"""R71 Wave 7: Runtime Config + Artifacts 身份绑定测试。

测试覆盖:
  R71 P1-04: 严格 TGJIEMA_IMAGE 格式校验
    - 完整正则:<registry>/<repository>@sha256:<64-hex>
    - 拒绝 tag / 短 hash / 其他仓库 / 多余字符
  R71 P1-05: host config digest 绑定
    - config/groups.yaml + config/topology.yaml 计算 sha256 combined digest
    - 写入 deployment manifest / E2E evidence / rollback record
  R71 P0-13: 当前 SHA 证据绑定
    - 所有 evidence 必须记录 GITHUB_SHA / GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT
    - 禁止使用"最近一次成功"或父提交/PR head/旧 run 替代当前候选 SHA

测试策略:
  - 单元测试:正则校验、host config digest、image 解析
  - 集成测试:build_runtime_config_binding 端到端
  - 工作流一致性:bind-runtime-config job 在 release-gates.yml 中存在
  - BP 一致性:bind-runtime-config 在 BP expected configs 中存在
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent

# 被测模块路径
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 将 scripts 目录加入 sys.path 以导入 validate_runtime_config_binding
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from validate_runtime_config_binding import (  # type: ignore[import-not-found]
        HOST_CONFIG_FILES,
        TGJIEMA_IMAGE_PATTERN,
        REGISTRY_DOMAIN_PATTERN,
        DEFAULT_EXPECTED_REGISTRY,
        DEFAULT_EXPECTED_REPOSITORY,
        ImageReference,
        parse_image_reference,
        validate_image_reference,
        compute_host_config_digest,
        compare_host_config_digest,
        build_runtime_config_binding,
    )
    _MODULE_AVAILABLE = True
except ImportError:
    _MODULE_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# R71 P1-04: 严格 TGJIEMA_IMAGE 格式校验
# ════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _MODULE_AVAILABLE, reason="validate_runtime_config_binding 不可用")
class TestR71P1_04ImageReferenceValidation:
    """R71 P1-04: 严格镜像引用格式校验。"""

    VALID_DIGEST = "a" * 64  # 64 位小写 hex

    def test_valid_image_reference(self):
        """合法镜像引用应通过校验。"""
        ref = f"ghcr.io/maxiuquan/tgjiema@sha256:{self.VALID_DIGEST}"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is not None, f"合法镜像应通过校验,错误: {errors}"
        assert parsed.registry == "ghcr.io"
        assert parsed.repository == "maxiuquan/tgjiema"
        assert parsed.digest == f"sha256:{self.VALID_DIGEST}"
        assert parsed.digest_hex == self.VALID_DIGEST

    def test_reject_tag_based_image(self):
        """R71 P1-04: 拒绝基于 tag 的镜像引用。"""
        ref = "ghcr.io/maxiuquan/tgjiema:latest"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None
        assert len(errors) > 0

    def test_reject_short_hash(self):
        """R71 P1-04: 拒绝短 hash(< 64 hex)。"""
        ref = "ghcr.io/maxiuquan/tgjiema@sha256:abc123"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None
        assert len(errors) > 0

    def test_reject_wrong_repository(self):
        """R71 P1-04: 拒绝错误的 repository。"""
        ref = f"ghcr.io/other/repo@sha256:{self.VALID_DIGEST}"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None
        assert any("repository" in e.lower() for e in errors)

    def test_reject_wrong_registry(self):
        """R71 P1-04: 拒绝错误的 registry。"""
        ref = f"docker.io/maxiuquan/tgjiema@sha256:{self.VALID_DIGEST}"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None
        assert any("registry" in e.lower() for e in errors)

    def test_reject_tag_with_digest(self):
        """R71 P1-04: 拒绝同时包含 tag 和 digest 的引用。"""
        ref = f"ghcr.io/maxiuquan/tgjiema:latest@sha256:{self.VALID_DIGEST}"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None

    def test_reject_uppercase_digest(self):
        """R71 P1-04: 拒绝大写 hex digest(要求小写)。"""
        ref = f"ghcr.io/maxiuquan/tgjiema@sha256:{'A' * 64}"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None

    def test_reject_extra_chars(self):
        """R71 P1-04: 拒绝多余字符。"""
        ref = f"ghcr.io/maxiuquan/tgjiema@sha256:{self.VALID_DIGEST}/extra"
        parsed, errors = validate_image_reference(
            ref,
            DEFAULT_EXPECTED_REGISTRY,
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is None

    def test_localhost_registry_allowed(self):
        """R71 P1-04: localhost registry 应被允许(用于本地测试)。"""
        ref = f"localhost:5000/maxiuquan/tgjiema@sha256:{self.VALID_DIGEST}"
        parsed, _ = validate_image_reference(
            ref,
            "localhost:5000",
            DEFAULT_EXPECTED_REPOSITORY,
        )
        assert parsed is not None
        assert parsed.registry == "localhost:5000"

    def test_parse_image_reference_returns_none_on_invalid(self):
        """parse_image_reference 对非法引用返回 None。"""
        ref = "not-a-valid-image-reference"
        parsed = parse_image_reference(ref)
        assert parsed is None

    def test_parse_image_reference_valid(self):
        """parse_image_reference 对合法引用返回 ImageReference。"""
        ref = f"ghcr.io/maxiuquan/tgjiema@sha256:{self.VALID_DIGEST}"
        parsed = parse_image_reference(ref)
        assert parsed is not None
        assert isinstance(parsed, ImageReference)
        assert parsed.raw == ref


# ════════════════════════════════════════════════════════════════
# R71 P1-05: host config digest 绑定
# ════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _MODULE_AVAILABLE, reason="validate_runtime_config_binding 不可用")
class TestR71P1_05HostConfigDigest:
    """R71 P1-05: host config digest 绑定。"""

    def test_host_config_files_constant(self):
        """HOST_CONFIG_FILES 常量包含 groups.yaml 和 topology.yaml。"""
        assert "config/groups.yaml" in HOST_CONFIG_FILES
        assert "config/topology.yaml" in HOST_CONFIG_FILES

    def test_compute_host_config_digest_returns_sha256(self):
        """计算 host config digest 返回 (per_file_list, combined_digest_str)。"""
        existing_files = tuple(
            f for f in HOST_CONFIG_FILES if (REPO_ROOT / f).is_file()
        )
        if not existing_files:
            pytest.skip("config/groups.yaml 和 config/topology.yaml 都不存在")
        per_file, combined = compute_host_config_digest(REPO_ROOT, existing_files)
        assert combined.startswith("sha256:")
        assert len(combined) == len("sha256:") + 64
        # combined digest 应为小写 hex
        hex_part = combined[len("sha256:"):]
        assert hex_part == hex_part.lower()
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_compute_host_config_digest_per_file(self):
        """每个文件都有独立的 HostConfigDigest 记录。"""
        existing_files = tuple(
            f for f in HOST_CONFIG_FILES if (REPO_ROOT / f).is_file()
        )
        if len(existing_files) < 2:
            pytest.skip("需要至少 2 个 host config 文件")
        per_file, _ = compute_host_config_digest(REPO_ROOT, existing_files)
        assert len(per_file) == len(existing_files)
        per_file_paths = {f.path for f in per_file}
        for fname in existing_files:
            assert fname in per_file_paths

    def test_compare_host_config_digest_match(self):
        """相同文件集合计算的 digest 应匹配。"""
        existing_files = tuple(
            f for f in HOST_CONFIG_FILES if (REPO_ROOT / f).is_file()
        )
        if not existing_files:
            pytest.skip("无 host config 文件")
        _, digest1 = compute_host_config_digest(REPO_ROOT, existing_files)
        _, digest2 = compute_host_config_digest(REPO_ROOT, existing_files)
        match, err = compare_host_config_digest(digest1, digest2)
        assert match is True
        assert err == ""

    def test_compare_host_config_digest_mismatch(self):
        """不同 digest 应不匹配。"""
        fake_digest = "sha256:" + "0" * 64
        real_digest = "sha256:" + "f" * 64
        match, err = compare_host_config_digest(real_digest, fake_digest)
        assert match is False
        assert err != ""

    def test_compare_host_config_digest_invalid_format(self):
        """空 digest 格式应失败。"""
        match, err = compare_host_config_digest("", "sha256:" + "0" * 64)
        assert match is False
        assert err != ""

    def test_compute_host_config_digest_missing_file(self):
        """缺失文件应记录在 per_file 列表中(fail-open,但 digest 为空)。"""
        per_file, combined = compute_host_config_digest(
            REPO_ROOT, ("config/nonexistent.yaml",)
        )
        assert len(per_file) == 1
        assert per_file[0].exists is False
        assert per_file[0].sha256 == ""


# ════════════════════════════════════════════════════════════════
# R71 P0-13: 当前 SHA 证据绑定
# ════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _MODULE_AVAILABLE, reason="validate_runtime_config_binding 不可用")
class TestR71P0_13CurrentSHABinding:
    """R71 P0-13: 当前 SHA 证据绑定。"""

    VALID_IMAGE = f"ghcr.io/maxiuquan/tgjiema@sha256:{'a' * 64}"

    def test_build_runtime_config_binding_includes_source_sha(self):
        """build_runtime_config_binding 必须包含 source_sha 字段。"""
        binding = build_runtime_config_binding(
            repo_root=REPO_ROOT,
            image_ref=self.VALID_IMAGE,
        )
        # source_sha 由 _get_source_sha() 自动从 git rev-parse HEAD 或 GITHUB_SHA 读取
        assert hasattr(binding, "source_sha")
        assert binding.source_sha  # 非空(本地有 git 或设置了 GITHUB_SHA)

    def test_build_runtime_config_binding_includes_workflow_run_id(self):
        """build_runtime_config_binding 必须包含 workflow_run_id 字段。"""
        # 设置环境变量模拟 CI 环境
        os.environ["GITHUB_RUN_ID"] = "12345"
        try:
            binding = build_runtime_config_binding(
                repo_root=REPO_ROOT,
                image_ref=self.VALID_IMAGE,
            )
            assert hasattr(binding, "workflow_run_id")
            # 若 CI 环境设置了 GITHUB_RUN_ID,则应被读取
            assert binding.workflow_run_id == "12345"
        finally:
            os.environ.pop("GITHUB_RUN_ID", None)

    def test_build_runtime_config_binding_includes_workflow_run_attempt(self):
        """build_runtime_config_binding 必须包含 workflow_run_attempt 字段。"""
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        try:
            binding = build_runtime_config_binding(
                repo_root=REPO_ROOT,
                image_ref=self.VALID_IMAGE,
            )
            assert hasattr(binding, "workflow_run_attempt")
            assert binding.workflow_run_attempt == "2"
        finally:
            os.environ.pop("GITHUB_RUN_ATTEMPT", None)

    def test_build_runtime_config_binding_to_dict(self):
        """to_dict 输出必须包含所有 R71 Wave 7 必需字段。"""
        binding = build_runtime_config_binding(
            repo_root=REPO_ROOT,
            image_ref=self.VALID_IMAGE,
        )
        d = binding.to_dict()
        required_fields = [
            "schema_version",
            "source_sha",
            "workflow_run_id",
            "workflow_run_attempt",
            "image_reference",
            "host_config_digests",
            "combined_host_config_digest",
        ]
        for field_name in required_fields:
            assert field_name in d, f"to_dict 缺少必需字段: {field_name}"

    def test_build_runtime_config_binding_rejects_invalid_image(self):
        """非法镜像引用应使 binding 失败(overall_passed=False)。"""
        binding = build_runtime_config_binding(
            repo_root=REPO_ROOT,
            image_ref="invalid-image-ref",
        )
        assert binding.overall_passed is False
        assert len(binding.errors) > 0


# ════════════════════════════════════════════════════════════════
# 工作流一致性:bind-runtime-config job 存在
# ════════════════════════════════════════════════════════════════


class TestR71Wave7WorkflowConsistency:
    """R71 Wave 7: 工作流一致性 — bind-runtime-config job 存在。"""

    RELEASE_GATES_YML = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

    def test_release_gates_yml_exists(self):
        """release-gates.yml 必须存在。"""
        assert self.RELEASE_GATES_YML.is_file()

    def test_bind_runtime_config_job_exists(self):
        """bind-runtime-config job 必须在 release-gates.yml 中定义。"""
        if not self.RELEASE_GATES_YML.is_file():
            pytest.skip("release-gates.yml 不存在")
        content = self.RELEASE_GATES_YML.read_text(encoding="utf-8")
        assert "  bind-runtime-config:" in content, (
            "release-gates.yml 缺少 bind-runtime-config job 定义"
        )

    def test_bind_runtime_config_in_release_summary_needs(self):
        """bind-runtime-config 必须在 release-summary 的 needs 数组中。"""
        if not self.RELEASE_GATES_YML.is_file():
            pytest.skip("release-gates.yml 不存在")
        content = self.RELEASE_GATES_YML.read_text(encoding="utf-8")
        # 检查 bind-runtime-config 出现在 needs 数组中
        assert "bind-runtime-config]" in content or "bind-runtime-config," in content, (
            "release-summary 的 needs 数组缺少 bind-runtime-config"
        )

    def test_bind_runtime_config_in_release_summary_env(self):
        """release-summary 必须传递 bind-runtime-config 的 result 到 env。"""
        if not self.RELEASE_GATES_YML.is_file():
            pytest.skip("release-gates.yml 不存在")
        content = self.RELEASE_GATES_YML.read_text(encoding="utf-8")
        assert "BIND_RUNTIME_CONFIG:" in content, (
            "release-summary 的 env 缺少 BIND_RUNTIME_CONFIG 变量"
        )

    def test_bind_runtime_config_in_for_loop_validation(self):
        """release-summary 的 for 循环必须校验 bind-runtime-config。"""
        if not self.RELEASE_GATES_YML.is_file():
            pytest.skip("release-gates.yml 不存在")
        content = self.RELEASE_GATES_YML.read_text(encoding="utf-8")
        assert "bind-runtime-config=${BIND_RUNTIME_CONFIG}" in content, (
            "release-summary 的 for 循环校验缺少 bind-runtime-config 条目"
        )

    def test_bind_runtime_config_uses_strict_regex(self):
        """bind-runtime-config job 必须调用 validate_runtime_config_binding.py
        而非宽松的 @sha256: 子串检查。"""
        if not self.RELEASE_GATES_YML.is_file():
            pytest.skip("release-gates.yml 不存在")
        content = self.RELEASE_GATES_YML.read_text(encoding="utf-8")
        assert "validate_runtime_config_binding.py" in content, (
            "bind-runtime-config job 必须调用 validate_runtime_config_binding.py"
        )
        assert "--mode full-evidence" in content, (
            "bind-runtime-config job 必须使用 --mode full-evidence"
        )


# ════════════════════════════════════════════════════════════════
# BP 一致性:bind-runtime-config context 存在
# ════════════════════════════════════════════════════════════════


class TestR71Wave7BranchProtectionConsistency:
    """R71 Wave 7: BP 一致性 — bind-runtime-config context 存在。"""

    BP_EXPECTED = REPO_ROOT / ".github" / "branch_protection.expected.json"
    BR_EXPECTED = REPO_ROOT / ".github" / "branch_ruleset.expected.json"
    CONFIGURE_SH = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
    VERIFY_SH = REPO_ROOT / "scripts" / "verify_branch_ruleset.sh"

    def test_bp_expected_contains_bind_runtime_config(self):
        """branch_protection.expected.json 必须包含 bind-runtime-config context。"""
        if not self.BP_EXPECTED.is_file():
            pytest.skip("branch_protection.expected.json 不存在")
        data = json.loads(self.BP_EXPECTED.read_text(encoding="utf-8"))
        contexts = data.get("required_status_checks", {}).get("contexts", [])
        assert "bind-runtime-config" in contexts, (
            "branch_protection.expected.json 缺少 bind-runtime-config context"
        )

    def test_br_expected_contains_bind_runtime_config(self):
        """branch_ruleset.expected.json 必须包含 bind-runtime-config context。"""
        if not self.BR_EXPECTED.is_file():
            pytest.skip("branch_ruleset.expected.json 不存在")
        data = json.loads(self.BR_EXPECTED.read_text(encoding="utf-8"))
        checks = []
        for rule in data.get("rules", []):
            if rule.get("type") == "required_status_checks":
                params = rule.get("parameters", {})
                checks = [c.get("context", "") for c in params.get("required_checks", [])]
        assert "bind-runtime-config" in checks, (
            "branch_ruleset.expected.json 缺少 bind-runtime-config context"
        )

    def test_configure_sh_contains_bind_runtime_config(self):
        """configure_branch_ruleset.sh 必须包含 bind-runtime-config。"""
        if not self.CONFIGURE_SH.is_file():
            pytest.skip("configure_branch_ruleset.sh 不存在")
        content = self.CONFIGURE_SH.read_text(encoding="utf-8")
        assert "bind-runtime-config" in content, (
            "configure_branch_ruleset.sh 缺少 bind-runtime-config context"
        )

    def test_verify_sh_contains_bind_runtime_config(self):
        """verify_branch_ruleset.sh 必须包含 bind-runtime-config。"""
        if not self.VERIFY_SH.is_file():
            pytest.skip("verify_branch_ruleset.sh 不存在")
        content = self.VERIFY_SH.read_text(encoding="utf-8")
        assert "bind-runtime-config" in content, (
            "verify_branch_ruleset.sh 缺少 bind-runtime-config context"
        )

    def test_verify_sh_has_r71_wave7_assertion(self):
        """verify_branch_ruleset.sh 必须有 R71 Wave 7 专属断言。"""
        if not self.VERIFY_SH.is_file():
            pytest.skip("verify_branch_ruleset.sh 不存在")
        content = self.VERIFY_SH.read_text(encoding="utf-8")
        assert "R71 Wave 7" in content, (
            "verify_branch_ruleset.sh 缺少 R71 Wave 7 专属断言"
        )


# ════════════════════════════════════════════════════════════════
# CLI 集成测试
# ════════════════════════════════════════════════════════════════


class TestR71Wave7CLIIntegration:
    """R71 Wave 7: CLI 集成测试。"""

    VALID_IMAGE = f"ghcr.io/maxiuquan/tgjiema@sha256:{'a' * 64}"
    INVALID_IMAGE = "ghcr.io/maxiuquan/tgjiema:latest"
    SCRIPT_PATH = SCRIPTS_DIR / "validate_runtime_config_binding.py"

    def test_cli_image_only_mode_valid_image(self):
        """CLI image-only 模式对合法镜像应 exit 0。"""
        if not self.SCRIPT_PATH.is_file():
            pytest.skip("validate_runtime_config_binding.py 不存在")
        result = subprocess.run(
            [
                sys.executable,
                str(self.SCRIPT_PATH),
                "--mode", "image-only",
                "--image", self.VALID_IMAGE,
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"合法镜像应 exit 0,实际 exit {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_cli_image_only_mode_invalid_image(self):
        """CLI image-only 模式对非法镜像应 exit 1(fail-closed)。"""
        if not self.SCRIPT_PATH.is_file():
            pytest.skip("validate_runtime_config_binding.py 不存在")
        result = subprocess.run(
            [
                sys.executable,
                str(self.SCRIPT_PATH),
                "--mode", "image-only",
                "--image", self.INVALID_IMAGE,
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1, (
            f"非法镜像应 exit 1,实际 exit {result.returncode}"
        )

    def test_cli_host_config_mode(self):
        """CLI host-config 模式应计算 host config digest(使用默认 HOST_CONFIG_FILES)。"""
        if not self.SCRIPT_PATH.is_file():
            pytest.skip("validate_runtime_config_binding.py 不存在")
        existing_files = [
            f for f in HOST_CONFIG_FILES if (REPO_ROOT / f).is_file()
        ]
        if not existing_files:
            pytest.skip("无 host config 文件")
        # host-config 模式使用 --repo-root 指定仓库根目录,自动读取 HOST_CONFIG_FILES
        result = subprocess.run(
            [
                sys.executable,
                str(self.SCRIPT_PATH),
                "--mode", "host-config",
                "--repo-root", str(REPO_ROOT),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"host-config 模式应 exit 0,实际 exit {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "combined digest:" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
