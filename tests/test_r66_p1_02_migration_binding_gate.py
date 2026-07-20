"""R66 P1-02: migration gate 统一校验口径测试 — catalog-only SOFT + release-binding HARD。

审计背景(R66 终审报告 P1-02):
    branch `migration-manifest-gate` 只检查结构/hash/策略,真正 HEAD 绑定在后续
    sign-image 内临时重生后验证。应统一为一种 release artifact 模型,禁止一个 gate
    对仓库旧 manifest 报 success、另一个 job 静默重写它。

R66 P1-02 整改(本次变更):
    1. check_migration_manifest.py 新增 --require-signed-binding 参数:
       - 默认 False: 签名绑定缺失仅 WARN(catalog-only gate 用 SOFT)
       - True: 签名绑定缺失从 WARN 升级为 HARD FAIL(release-binding gate 用 HARD)
    2. release-gates.yml 新增 migration-binding-gate job:
       - needs: [sign-image] — 在 sign-image 之后运行,确保签名已生成
       - if: 同 sign-image (master/main push 或 release tag)
       - 下载 release-gates-signed-* artifact (含 release-manifest.json + manifest 签名)
       - 运行 check_migration_manifest.py --strict --require-signed-binding (HARD)
    3. publish-attestation needs: 增加 migration-binding-gate
    4. release-summary 聚合: migration-binding-gate 与 sign-image/publish-attestation
       同等处理(master/tag push 必须 success,PR 允许 skipped)
    5. verify_branch_protection.sh / configure_branch_protection.sh: WORKFLOW_JOBS /
       EXPECTED_RG_JOBS 增加 migration-binding-gate (15 个 Release Gates job)

    统一校验口径:
      - catalog-only gate (migration-manifest-gate, early parallel): SOFT (WARN)
      - release-binding gate (migration-binding-gate, after sign-image): HARD (FAIL)

测试覆盖矩阵:
    A. check_migration_manifest.py --require-signed-binding 参数
    B. migration-binding-gate job 存在与配置正确
    C. publish-attestation needs 包含 migration-binding-gate
    D. release-summary 聚合逻辑包含 migration-binding-gate
    E. verify_branch_protection.sh / configure_branch_protection.sh 包含 migration-binding-gate
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
CHECK_MM_SCRIPT = REPO_ROOT / "scripts" / "check_migration_manifest.py"
VBP_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_branch_protection.sh"
CONFIGURE_BP_SCRIPT = REPO_ROOT / "scripts" / "configure_branch_protection.sh"


@pytest.fixture(scope="module")
def workflow_yaml():
    """加载 release-gates.yml 并解析为 dict。"""
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def check_mm_content():
    """读取 check_migration_manifest.py 源码。"""
    with open(CHECK_MM_SCRIPT, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def vbp_script_content():
    """读取 verify_branch_protection.sh 源码。"""
    with open(VBP_SCRIPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def configure_bp_script_content():
    """读取 configure_branch_protection.sh 源码。"""
    with open(CONFIGURE_BP_SCRIPT, "r", encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════
# A. check_migration_manifest.py --require-signed-binding 参数
# ════════════════════════════════════════════════════════════════


class TestCheckMigrationManifestRequireSignedBinding:
    """R66 P1-02: check_migration_manifest.py 新增 --require-signed-binding 参数。"""

    def test_require_signed_binding_arg_exists(self, check_mm_content):
        """check_migration_manifest.py 必须包含 --require-signed-binding 参数。"""
        assert "--require-signed-binding" in check_mm_content, (
            "R66 P1-02: check_migration_manifest.py 必须包含 --require-signed-binding 参数"
        )

    def test_verify_manifest_has_require_signed_binding_param(self, check_mm_content):
        """verify_manifest() 函数签名必须包含 require_signed_binding 参数。"""
        assert "require_signed_binding: bool = False" in check_mm_content, (
            "R66 P1-02: verify_manifest() 必须有 require_signed_binding: bool = False 参数"
        )

    def test_require_signed_binding_makes_warning_hard_fail(self, check_mm_content):
        """--require-signed-binding 时,签名绑定缺失从 WARN 升级为 HARD FAIL。

        检查源码:require_signed_binding 为 True 时,errors.append(而非 warnings.append)。
        """
        # 必须存在 require_signed_binding 条件分支
        assert "if require_signed_binding:" in check_mm_content, (
            "R66 P1-02: 必须有 if require_signed_binding: 条件分支"
        )
        # require_signed_binding=True 时调用 errors.append(HARD FAIL)
        # 检查 require_signed_binding 块内含 errors.append
        lines = check_mm_content.splitlines()
        in_require_block = False
        has_errors_append_in_require = False
        for line in lines:
            stripped = line.strip()
            if "if require_signed_binding:" in stripped:
                in_require_block = True
                continue
            if in_require_block:
                if stripped.startswith("if ") or stripped.startswith("else:") or stripped.startswith("elif "):
                    # 离开 require_signed_binding 块
                    in_require_block = False
                    continue
                if "errors.append" in stripped:
                    has_errors_append_in_require = True
                    break
        assert has_errors_append_in_require, (
            "R66 P1-02: require_signed_binding=True 时必须 errors.append(HARD FAIL),"
            "而非 warnings.append(SOFT WARN)"
        )

    def test_main_passes_require_signed_binding_to_verify_manifest(self, check_mm_content):
        """main() 必须将 args.require_signed_binding 传给 verify_manifest()。"""
        assert "require_signed_binding=args.require_signed_binding" in check_mm_content, (
            "R66 P1-02: main() 必须传 require_signed_binding=args.require_signed_binding"
        )

    def test_r66_p1_02_marker_in_source(self, check_mm_content):
        """check_migration_manifest.py 必须包含 R66 P1-02 标记(便于审计)。"""
        assert "R66 P1-02" in check_mm_content, (
            "R66 P1-02: check_migration_manifest.py 必须包含 R66 P1-02 标记"
        )


# ════════════════════════════════════════════════════════════════
# B. migration-binding-gate job 存在与配置正确
# ════════════════════════════════════════════════════════════════


class TestMigrationBindingGateJob:
    """R66 P1-02: release-gates.yml 必须包含 migration-binding-gate job。"""

    def test_migration_binding_gate_job_exists(self, workflow_yaml):
        """release-gates.yml 必须包含 migration-binding-gate job。"""
        assert "migration-binding-gate" in workflow_yaml["jobs"], (
            "R66 P1-02: release-gates.yml 必须包含 migration-binding-gate job"
        )

    def test_migration_binding_gate_needs_sign_image(self, workflow_yaml):
        """migration-binding-gate 必须 needs: [sign-image](在签名后运行)。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "sign-image" in needs, (
            "R66 P1-02: migration-binding-gate 必须 needs: [sign-image]"
            "(确保签名完成后才做 HARD 绑定校验)"
        )

    def test_migration_binding_gate_if_includes_master(self, workflow_yaml):
        """migration-binding-gate if: 必须包含 refs/heads/master。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        if_cond = job.get("if", "")
        assert "refs/heads/master" in if_cond, (
            f"R66 P1-02: migration-binding-gate if: 必须包含 refs/heads/master,实际: {if_cond}"
        )

    def test_migration_binding_gate_if_includes_tag(self, workflow_yaml):
        """R66 P1-01/P1-02: migration-binding-gate if: 必须包含 refs/tags/v*。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        if_cond = job.get("if", "")
        assert "refs/tags/v" in if_cond or "startsWith(github.ref, 'refs/tags/v'" in if_cond, (
            f"R66 P1-02: migration-binding-gate if: 必须包含 refs/tags/v*,实际: {if_cond}"
        )

    def test_migration_binding_gate_if_excludes_pr(self, workflow_yaml):
        """migration-binding-gate if: 必须要求 github.event_name == 'push'。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        if_cond = job.get("if", "")
        assert "github.event_name == 'push'" in if_cond, (
            f"R66 P1-02: migration-binding-gate if: 必须要求 github.event_name == 'push'"
        )

    def test_migration_binding_gate_uses_require_signed_binding(self, workflow_yaml):
        """migration-binding-gate 的 run 块必须调用 --require-signed-binding。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        steps = job.get("steps", [])
        all_run = ""
        for step in steps:
            all_run += step.get("run", "") + "\n"
        assert "--require-signed-binding" in all_run, (
            "R66 P1-02: migration-binding-gate 必须调用 "
            "check_migration_manifest.py --strict --require-signed-binding (HARD)"
        )

    def test_migration_binding_gate_downloads_signed_artifact(self, workflow_yaml):
        """migration-binding-gate 必须下载 release-gates-signed-* artifact。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        steps = job.get("steps", [])
        has_download = False
        for step in steps:
            uses = step.get("uses", "")
            if "download-artifact" in uses:
                with_block = step.get("with", {})
                name = with_block.get("name", "")
                if "release-gates-signed" in name:
                    has_download = True
                    break
        assert has_download, (
            "R66 P1-02: migration-binding-gate 必须下载 release-gates-signed-* artifact"
            "(含 release-manifest.json + manifest 签名)"
        )

    def test_migration_binding_gate_has_python_setup(self, workflow_yaml):
        """migration-binding-gate 必须设置 Python(运行 check_migration_manifest.py)。"""
        job = workflow_yaml["jobs"].get("migration-binding-gate", {})
        steps = job.get("steps", [])
        has_python = False
        for step in steps:
            uses = step.get("uses", "")
            if "setup-python" in uses:
                has_python = True
                break
        assert has_python, (
            "R66 P1-02: migration-binding-gate 必须设置 Python(运行 check_migration_manifest.py)"
        )


# ════════════════════════════════════════════════════════════════
# C. publish-attestation needs 包含 migration-binding-gate
# ════════════════════════════════════════════════════════════════


class TestPublishAttestationNeedsMigrationBindingGate:
    """R66 P1-02: publish-attestation 必须 needs migration-binding-gate。"""

    def test_publish_attestation_needs_migration_binding_gate(self, workflow_yaml):
        """publish-attestation 的 needs 必须包含 migration-binding-gate。"""
        pa = workflow_yaml["jobs"].get("publish-attestation", {})
        needs = pa.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "migration-binding-gate" in needs, (
            "R66 P1-02: publish-attestation 必须 needs migration-binding-gate,"
            "确保 attestation 绑定的 catalog 已通过 HARD 校验"
        )

    def test_publish_attestation_needs_both_manifest_gates(self, workflow_yaml):
        """publish-attestation 必须 needs migration-manifest-gate AND migration-binding-gate。

        - migration-manifest-gate (early, catalog-only SOFT)
        - migration-binding-gate (after sign-image, HARD binding)
        两个口径都需要通过才允许生成 attestation。
        """
        pa = workflow_yaml["jobs"].get("publish-attestation", {})
        needs = pa.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "migration-manifest-gate" in needs, (
            "publish-attestation 必须 needs migration-manifest-gate (catalog-only SOFT)"
        )
        assert "migration-binding-gate" in needs, (
            "R66 P1-02: publish-attestation 必须 needs migration-binding-gate (HARD binding)"
        )


# ════════════════════════════════════════════════════════════════
# D. release-summary 聚合逻辑包含 migration-binding-gate
# ════════════════════════════════════════════════════════════════


class TestReleaseSummaryIncludesMigrationBindingGate:
    """R66 P1-02: release-summary 必须聚合 migration-binding-gate。"""

    def test_release_summary_needs_migration_binding_gate(self, workflow_yaml):
        """release-summary 的 needs 必须包含 migration-binding-gate。"""
        rs = workflow_yaml["jobs"].get("release-summary", {})
        needs = rs.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "migration-binding-gate" in needs, (
            "R66 P1-02: release-summary 必须 needs migration-binding-gate"
        )

    def test_release_summary_env_has_migration_binding_gate(self, workflow_yaml):
        """release-summary 的 env 必须包含 MIGRATION_BINDING_GATE。"""
        rs = workflow_yaml["jobs"].get("release-summary", {})
        env = rs.get("env", {})
        assert "MIGRATION_BINDING_GATE" in env, (
            "R66 P1-02: release-summary env 必须包含 MIGRATION_BINDING_GATE"
        )
        assert "migration-binding-gate" in env["MIGRATION_BINDING_GATE"], (
            "MIGRATION_BINDING_GATE 必须引用 needs.migration-binding-gate.result"
        )

    def test_release_summary_aggregate_checks_migration_binding_gate(self, workflow_yaml):
        """release-summary 聚合循环必须检查 migration-binding-gate。"""
        rs = workflow_yaml["jobs"].get("release-summary", {})
        steps = rs.get("steps", [])
        aggregate_run = ""
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_run = step.get("run", "")
                break
        assert "migration-binding-gate" in aggregate_run, (
            "R66 P1-02: release-summary 聚合循环必须检查 migration-binding-gate"
        )
        assert "MIGRATION_BINDING_GATE" in aggregate_run, (
            "R66 P1-02: release-summary 聚合循环必须引用 MIGRATION_BINDING_GATE 环境变量"
        )

    def test_release_summary_treats_migration_binding_gate_like_sign_image(self, workflow_yaml):
        """release-summary 必须将 migration-binding-gate 与 sign-image/publish-attestation
        同等处理(master/tag push 必须 success,PR 允许 skipped)。

        检查聚合步骤的 run 块:migration-binding-gate 应在 sign-image/publish-attestation
        的判断分支中(允许 PR skipped)。
        """
        rs = workflow_yaml["jobs"].get("release-summary", {})
        steps = rs.get("steps", [])
        aggregate_run = ""
        for step in steps:
            name = step.get("name", "")
            if "Verify all required jobs succeeded" in name:
                aggregate_run = step.get("run", "")
                break
        # migration-binding-gate 应与 sign-image / publish-attestation 在同一判断分支
        # 查找 "sign-image" 或 "publish-attestation" 的判断行,确认 migration-binding-gate 也在
        assert "migration-binding-gate" in aggregate_run, (
            "R66 P1-02: 聚合步骤必须包含 migration-binding-gate 判断"
        )
        # 检查 sign-image / publish-attestation / migration-binding-gate 在同一 if 分支
        # (查找包含 "sign-image" 与 "publish-attestation" 的 if 行,验证也含 migration-binding-gate)
        lines = aggregate_run.splitlines()
        found_combined = False
        for line in lines:
            if "sign-image" in line and "publish-attestation" in line:
                if "migration-binding-gate" in line:
                    found_combined = True
                    break
        assert found_combined, (
            "R66 P1-02: migration-binding-gate 必须与 sign-image/publish-attestation "
            "在同一判断分支(master/tag push 必须 success,PR 允许 skipped)"
        )


# ════════════════════════════════════════════════════════════════
# E. verify_branch_protection.sh / configure_branch_protection.sh 包含 migration-binding-gate
# ════════════════════════════════════════════════════════════════


class TestBranchProtectionScriptsIncludeMigrationBindingGate:
    """R66 P1-02: BP 脚本必须包含 migration-binding-gate。"""

    def test_vbp_script_includes_migration_binding_gate(self, vbp_script_content):
        """verify_branch_protection.sh WORKFLOW_JOBS 必须包含 migration-binding-gate。"""
        assert "migration-binding-gate" in vbp_script_content, (
            "R66 P1-02: verify_branch_protection.sh WORKFLOW_JOBS 必须包含 migration-binding-gate"
        )

    def test_configure_bp_script_includes_migration_binding_gate(self, configure_bp_script_content):
        """configure_branch_protection.sh EXPECTED_RG_JOBS 必须包含 migration-binding-gate。"""
        assert "migration-binding-gate" in configure_bp_script_content, (
            "R66 P1-02: configure_branch_protection.sh EXPECTED_RG_JOBS 必须包含 migration-binding-gate"
        )

    def test_vbp_script_release_gates_list_has_15_jobs(self, vbp_script_content):
        """verify_branch_protection.sh Release Gates 列表应至少 15 个 job。"""
        # 提取 Release Gates 行
        rg_line = ""
        for line in vbp_script_content.splitlines():
            if "Release Gates|" in line:
                rg_line = line
                break
        assert rg_line, "未找到 Release Gates| 行"
        # 提取 job 列表
        jobs_str = rg_line.split("|", 1)[1]
        jobs = [j.strip() for j in jobs_str.split(",")]
        assert len(jobs) >= 15, (
            f"R66 P1-02: Release Gates 列表应至少 15 个 job,实际 {len(jobs)} 个"
        )
        assert "migration-binding-gate" in jobs, (
            "Release Gates 列表必须包含 migration-binding-gate"
        )

    def test_configure_bp_script_expected_rg_jobs_has_15(self, configure_bp_script_content):
        """configure_branch_protection.sh EXPECTED_RG_JOBS 应至少 15 个 job。"""
        # 提取 EXPECTED_RG_JOBS 数组
        in_array = False
        jobs = []
        for line in configure_bp_script_content.splitlines():
            stripped = line.strip()
            if "EXPECTED_RG_JOBS=(" in stripped:
                in_array = True
                continue
            if in_array:
                if stripped == ")":
                    break
                # 提取引号内的 job 名
                if '"' in stripped:
                    job = stripped.split('"')[1] if '"' in stripped else stripped
                    jobs.append(job)
        assert len(jobs) >= 15, (
            f"R66 P1-02: EXPECTED_RG_JOBS 应至少 15 个 job,实际 {len(jobs)} 个: {jobs}"
        )
        assert "migration-binding-gate" in jobs, (
            "EXPECTED_RG_JOBS 必须包含 migration-binding-gate"
        )
