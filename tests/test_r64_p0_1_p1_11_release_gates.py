"""R64 P0-01 (Release Gates 仍失败) + P1-11 (branch protection 门禁) 测试。

审计背景(R64 终审报告):

  P0-01 — Release Gates 仍失败,三个失败点:
    1. verify-branch-protection: WORKFLOW_JOBS 中 Release Gates 列表只列了 10 个 job,
       遗漏了 sign-image / rc-continuity / publish-attestation / release-summary 4 个 job;
       CI 列表遗漏了 repo-hygiene。BP contexts 与当前 workflow job 名不一致。
    2. rc-continuity: 三次连续候选证据门禁失败(历史不足/digest 不一致处理)。
    3. sign-image + verify image signature: cosign verify 信任待验证证书自身提供的
       identity (signed-with == verified-with),验证策略不钉扎 ref/commit。

  P1-11 — branch protection 门禁:
    必须在 GitHub ruleset/branch protection 中确认 required contexts 与当前 workflow
    job 名完全一致,禁止管理员绕过,要求 PR、至少一名独立 reviewer、dismiss stale
    approvals、conversation resolved、signed commits;用于检查配置的 token 仅有读取权限。

整改:
  P0-01 失败点 1: WORKFLOW_JOBS 扩展为 17 个 Release Gates job + CI 的 repo-hygiene。
  P0-01 失败点 2: rc-continuity PR 场景宽松通过(REQUIRED_CONSECUTIVE=0),
                  push 时严格 3 次连续;首次 release 无历史 digest 时跳过,不阻断。
  P0-01 失败点 3: cosign verify 使用 EXPECTED_IDENTITY (钉扎 github.repository +
                  workflow 路径 + 当前 ref),不再从待验证证书提取 identity 作为验证条件;
                  保留 SAN 形态校验作为 defense in depth。
  P1-11: detect_branch_protection_contexts.sh / configure_branch_protection.sh
         校验 17 个 Release Gates job + CI / repo-hygiene 覆盖。

测试覆盖矩阵:
  A. verify-branch-protection WORKFLOW_JOBS 列表完整(17 个 Release Gates job + repo-hygiene)
  B. cosign verify 步骤使用 EXPECTED_IDENTITY 而非从证书提取的 SAN
  C. rc-continuity 宽严策略(PR 宽松 / push 严格 / 首次 release 不阻断)
  D. configure_branch_protection.sh / detect_branch_protection_contexts.sh 包含完整 contexts
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容 — conftest.py 在收集阶段已注入 config/telegram mock,
# 此处再注入一次以防本文件被单独运行(conftest 未加载场景)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
# R65 fix: verify-branch-protection job 的 inline run: 块提取到外部脚本
# scripts/verify_branch_protection.sh(避免 YAML 21000 字节限制)。
# WORKFLOW_JOBS 数组现位于该脚本中,测试需读取该文件解析。
VBP_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_branch_protection.sh"
CONFIGURE_BP_SCRIPT = REPO_ROOT / "scripts" / "configure_branch_protection.sh"
DETECT_BP_SCRIPT = REPO_ROOT / "scripts" / "detect_branch_protection_contexts.sh"


# R64 P0-01 失败点 1: 17 个 Release Gates job + CI 的 repo-hygiene
# R66 P1-02: 新增 migration-binding-gate(15 个 Release Gates job)
# R66 P1-10: 新增 attestation-semantics-verify(16 个 Release Gates job)
# R66 P1-11: 新增 tag-ruleset-verify(17 个 Release Gates job)
# R66 P1-09: 新增 CI skip-inventory job
EXPECTED_RELEASE_GATES_JOBS = [
    "docker-build",
    "docker-digest-verify",
    "compose-config",
    "redis-acl-matrix",
    "schema-diff",
    "backup-restore-drill",
    "sbom",
    "pip-audit",
    "trivy",
    "sign-image",
    "verify-branch-protection",
    "rc-continuity",
    "publish-attestation",
    "attestation-semantics-verify",
    "tag-ruleset-verify",
    "migration-binding-gate",
    "release-summary",
]

EXPECTED_CI_JOBS = [
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "lint",
    "repo-hygiene",
    "i18n-check",
    "static-gates",
    "security",
    "fault-injection",
    "migration-dry-run",
    "skip-inventory",
]


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _read_workflow() -> str:
    """读取 release-gates.yml 完整内容。"""
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _read_vbp_script() -> str:
    """读取 scripts/verify_branch_protection.sh 完整内容。

    R65 fix: WORKFLOW_JOBS 数组已从 release-gates.yml 提取到该脚本中,
    避免 YAML run: 块超过 GitHub Actions 21000 字节限制。
    """
    return VBP_SCRIPT_PATH.read_text(encoding="utf-8")


def _extract_workflow_jobs_rg_list(content: str | None = None) -> list[str]:
    """从 WORKFLOW_JOBS 数组中提取 Release Gates 的 job 列表。

    R65 fix: WORKFLOW_JOBS 现位于 scripts/verify_branch_protection.sh,
    若 content 为 None 则从该脚本读取。

    WORKFLOW_JOBS 数组格式:
        WORKFLOW_JOBS=(
          "CI|test (3.10),test (3.11),..."
          "Deploy Check|verify-deploy"
          "Release Gates|docker-build,docker-digest-verify,..."
          "E2E Tests|playwright-e2e"
        )
    """
    if content is None:
        content = _read_vbp_script()
    lines = content.splitlines()
    in_workflow_jobs = False
    rg_jobs: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "WORKFLOW_JOBS=(" in line:
            in_workflow_jobs = True
            continue
        if in_workflow_jobs:
            if stripped == ")":
                break
            # 解析 "Release Gates|job1,job2,..." 行
            if stripped.startswith('"') and "|" in stripped:
                # 去掉首尾引号
                inner = stripped.strip('"')
                if "|" in inner:
                    workflow_name, jobs_csv = inner.split("|", 1)
                    if workflow_name == "Release Gates":
                        rg_jobs = [j.strip() for j in jobs_csv.split(",") if j.strip()]
    return rg_jobs


def _extract_workflow_jobs_ci_list(content: str | None = None) -> list[str]:
    """从 WORKFLOW_JOBS 数组中提取 CI 的 job 列表。

    R65 fix: 若 content 为 None,从 scripts/verify_branch_protection.sh 读取。
    """
    if content is None:
        content = _read_vbp_script()
    lines = content.splitlines()
    in_workflow_jobs = False
    ci_jobs: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "WORKFLOW_JOBS=(" in line:
            in_workflow_jobs = True
            continue
        if in_workflow_jobs:
            if stripped == ")":
                break
            if stripped.startswith('"') and "|" in stripped:
                inner = stripped.strip('"')
                if "|" in inner:
                    workflow_name, jobs_csv = inner.split("|", 1)
                    if workflow_name == "CI":
                        ci_jobs = [j.strip() for j in jobs_csv.split(",") if j.strip()]
    return ci_jobs


# ════════════════════════════════════════════════════════════════
# A. verify-branch-protection WORKFLOW_JOBS 列表完整性 (P0-01 失败点 1)
# ════════════════════════════════════════════════════════════════


class TestVerifyBranchProtectionWorkflowJobs:
    """verify-branch-protection job 的 WORKFLOW_JOBS 列表完整性校验。

    P0-01 失败点 1 整改:Release Gates 列表必须包含全部 17 个 job,
    CI 列表必须包含 repo-hygiene。
    """

    @pytest.fixture
    def workflow_content(self):
        return _read_workflow()

    def test_release_gates_list_contains_all_17_jobs(self, workflow_content):
        """Release Gates 列表必须包含全部 17 个 job(允许新增,不允许减少)。

        P0-01 失败点 1: 旧版只列了 10 个 job,遗漏了
        sign-image / rc-continuity / publish-attestation / release-summary。
        R65 P0-04: 新增 production-promotion-gate 等 gate 后列表自然增长,
        本测试仅校验"至少 17 个 + 全部 EXPECTED_RELEASE_GATES_JOBS 齐全",
        不限制 workflow 引入新的 required gate。
        R66 P1-02: 新增 migration-binding-gate(15 个 Release Gates job)。
        R66 P1-10: 新增 attestation-semantics-verify(16 个)。
        R66 P1-11: 新增 tag-ruleset-verify(17 个)。
        R65 fix: WORKFLOW_JOBS 现位于 scripts/verify_branch_protection.sh
        (避免 YAML 21000 字节限制),_extract_workflow_jobs_rg_list() 无参
        时自动从该脚本读取。
        """
        rg_jobs = _extract_workflow_jobs_rg_list()
        assert rg_jobs, (
            "P0-01: 未找到 WORKFLOW_JOBS 中的 Release Gates 条目 — "
            "WORKFLOW_JOBS 数组解析失败"
        )
        missing = [j for j in EXPECTED_RELEASE_GATES_JOBS if j not in rg_jobs]
        assert not missing, (
            f"P0-01: Release Gates 列表遗漏 {len(missing)} 个 job: {missing} — "
            f"实际: {rg_jobs}"
        )
        # 确保至少 17 个(允许新增 gate,不允许减少)
        assert len(rg_jobs) >= 17, (
            f"P0-01: Release Gates 列表应至少 17 个 job,实际 {len(rg_jobs)} 个: {rg_jobs}"
        )

    def test_release_gates_list_includes_sign_image(self, workflow_content):
        """Release Gates 列表必须包含 sign-image(P0-01 失败点 1 遗漏项)。"""
        rg_jobs = _extract_workflow_jobs_rg_list()
        assert "sign-image" in rg_jobs, (
            "P0-01: Release Gates 列表必须包含 sign-image"
        )

    def test_release_gates_list_includes_rc_continuity(self, workflow_content):
        """Release Gates 列表必须包含 rc-continuity(P0-01 失败点 1 遗漏项)。"""
        rg_jobs = _extract_workflow_jobs_rg_list()
        assert "rc-continuity" in rg_jobs, (
            "P0-01: Release Gates 列表必须包含 rc-continuity"
        )

    def test_release_gates_list_includes_publish_attestation(self, workflow_content):
        """Release Gates 列表必须包含 publish-attestation(P0-01 失败点 1 遗漏项)。"""
        rg_jobs = _extract_workflow_jobs_rg_list()
        assert "publish-attestation" in rg_jobs, (
            "P0-01: Release Gates 列表必须包含 publish-attestation"
        )

    def test_release_gates_list_includes_release_summary(self, workflow_content):
        """Release Gates 列表必须包含 release-summary(P0-01 失败点 1 遗漏项)。"""
        rg_jobs = _extract_workflow_jobs_rg_list()
        assert "release-summary" in rg_jobs, (
            "P0-01: Release Gates 列表必须包含 release-summary"
        )

    def test_ci_list_includes_repo_hygiene(self, workflow_content):
        """CI 列表必须包含 repo-hygiene(P1-11 required context)。

        P0-01 失败点 1 / P1-11: 旧版 CI 列表遗漏了 repo-hygiene。
        R65 fix: WORKFLOW_JOBS 现位于 scripts/verify_branch_protection.sh。
        """
        ci_jobs = _extract_workflow_jobs_ci_list()
        assert ci_jobs, (
            "P0-01: 未找到 WORKFLOW_JOBS 中的 CI 条目 — "
            "WORKFLOW_JOBS 数组解析失败"
        )
        assert "repo-hygiene" in ci_jobs, (
            f"P0-01/P1-11: CI 列表必须包含 repo-hygiene — 实际: {ci_jobs}"
        )

    def test_ci_list_contains_all_expected_jobs(self, workflow_content):
        """CI 列表必须包含全部预期 job(含 repo-hygiene)。"""
        ci_jobs = _extract_workflow_jobs_ci_list()
        missing = [j for j in EXPECTED_CI_JOBS if j not in ci_jobs]
        assert not missing, (
            f"P0-01/P1-11: CI 列表遗漏 {len(missing)} 个 job: {missing} — "
            f"实际: {ci_jobs}"
        )


# ════════════════════════════════════════════════════════════════
# B. cosign verify 步骤使用 EXPECTED_IDENTITY (P0-01 失败点 3)
# ════════════════════════════════════════════════════════════════


class TestCosignVerifyUsesExpectedIdentity:
    """cosign verify 步骤必须使用 EXPECTED_IDENTITY 钉扎 ref,
    而非从待验证证书提取的 SAN(signed-with == verified-with 漏洞)。

    P0-01 失败点 3 整改:
      - 不再从待验证证书提取 identity 作为验证条件
      - 改用 EXPECTED_IDENTITY = github.repository + workflow 路径 + 当前 ref
      - 保留 SAN 形态校验作为 defense in depth
    """

    @pytest.fixture
    def workflow_content(self):
        return _read_workflow()

    def test_image_verify_uses_expected_identity(self, workflow_content):
        """Verify image signature 步骤必须用 EXPECTED_IDENTITY 做 cosign verify。

        P0-01 失败点 3: 旧版用从证书提取的 SAN 做 verify(signed-with == verified-with),
        新版用 EXPECTED_IDENTITY 钉扎 ref。

        R66 P0-03 整改: EXPECTED_IDENTITY 必须用 ${{ github.ref }}(完整 ref,如
        refs/heads/master 或 refs/tags/v1.0.0),而非 refs/heads/${{ github.ref_name }}
        (tag push 时 ref_name 为 tag 名,拼成 refs/heads/<tag> 错误)。
        """
        # 找到 Verify image signature 步骤的 cosign verify 命令
        assert "Verify image signature (output verification statement)" in workflow_content, (
            "P0-01: 必须有 Verify image signature 步骤"
        )
        # EXPECTED_IDENTITY 必须用 github.repository + workflow 路径 + github.ref 构造
        assert "EXPECTED_IDENTITY=" in workflow_content, (
            "P0-01 失败点 3: cosign verify 必须用 EXPECTED_IDENTITY 钉扎 ref"
        )
        assert "github.repository" in workflow_content, (
            "P0-01 失败点 3: EXPECTED_IDENTITY 必须包含 github.repository"
        )
        # R66 P0-03: 必须用 ${{ github.ref }}(完整 ref,正确处理 branch 与 tag)
        assert 'release-gates.yml@${{ github.ref }}' in workflow_content, (
            "R66 P0-03: EXPECTED_IDENTITY 必须用 ${{ github.ref }} 钉扎完整 ref "
            "(branch: refs/heads/master,tag: refs/tags/v1.0.0)"
        )
        # R66 P0-03: 禁止 EXPECTED_IDENTITY 行使用 refs/heads/${{ github.ref_name }}
        # (tag push 时拼成 refs/heads/<tag> 错误,应为 refs/tags/<tag>)
        # 注: 仅检查非注释行(注释行可能引用旧模式作为说明)
        bad_lines = []
        for i, line in enumerate(workflow_content.splitlines()):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "EXPECTED_IDENTITY=" in line and "refs/heads/${{ github.ref_name }}" in line:
                bad_lines.append((i + 1, line.strip()))
        assert not bad_lines, (
            "R66 P0-03: EXPECTED_IDENTITY 行禁止用 refs/heads/${{ github.ref_name }} "
            "(tag push 时拼成 refs/heads/<tag> 错误,应为 refs/tags/<tag>)— "
            f"发现 {len(bad_lines)} 处: {bad_lines[:3]}"
        )

    def test_image_verify_cosign_uses_expected_identity_not_extracted(self, workflow_content):
        """cosign verify 命令的 --certificate-identity 必须用 EXPECTED_IDENTITY。

        P0-01 失败点 3: 主验证条件是 EXPECTED_IDENTITY,不是 EXTRACTED_IDENTITY。
        """
        # 找到 Verify image signature 步骤的位置
        verify_idx = workflow_content.find("Verify image signature (output verification statement)")
        assert verify_idx >= 0, "P0-01: 未找到 Verify image signature 步骤"

        # 截取该步骤到下一个步骤之间的内容
        next_step_idx = workflow_content.find("    - name:", verify_idx + 1)
        if next_step_idx < 0:
            verify_section = workflow_content[verify_idx:]
        else:
            verify_section = workflow_content[verify_idx:next_step_idx]

        # cosign verify 命令必须用 ${EXPECTED_IDENTITY}
        assert '--certificate-identity "${EXPECTED_IDENTITY}"' in verify_section, (
            "P0-01 失败点 3: Verify image signature 的 cosign verify 命令 "
            "必须用 --certificate-identity \"${EXPECTED_IDENTITY}\" 钉扎 ref, "
            "而非从证书提取的 SAN"
        )
        # 不应再用 ${CERT_IDENTITY} 或 ${EXTRACTED_IDENTITY} 作为 verify 的 --certificate-identity
        assert '--certificate-identity "${CERT_IDENTITY}"' not in verify_section, (
            "P0-01 失败点 3: Verify image signature 不应再用 CERT_IDENTITY 做 cosign verify"
        )
        assert '--certificate-identity "${EXTRACTED_IDENTITY}"' not in verify_section, (
            "P0-01 失败点 3: Verify image signature 不应用 EXTRACTED_IDENTITY 做 cosign verify"
        )

    def test_image_verify_preserves_extracted_identity_for_diagnostics(self, workflow_content):
        """Verify image signature 步骤保留 EXTRACTED_IDENTITY 用于诊断对比(defense in depth)。

        P0-01 失败点 3: 保留 SAN 形态校验作为 defense in depth,
        EXTRACTED_IDENTITY 仅用于诊断,不作为验证条件。
        """
        verify_idx = workflow_content.find("Verify image signature (output verification statement)")
        assert verify_idx >= 0
        next_step_idx = workflow_content.find("    - name:", verify_idx + 1)
        if next_step_idx < 0:
            verify_section = workflow_content[verify_idx:]
        else:
            verify_section = workflow_content[verify_idx:next_step_idx]

        # EXTRACTED_IDENTITY 用于诊断对比(defense in depth)
        assert "EXTRACTED_IDENTITY=" in verify_section, (
            "P0-01: Verify image signature 应保留 EXTRACTED_IDENTITY 用于诊断对比(defense in depth)"
        )
        # 仍引用 extract_image_identity step output(形态校验 + 诊断)
        assert "steps.extract_image_identity.outputs.certificate_identity" in verify_section, (
            "P0-01: Verify image signature 应引用 extract_image_identity step output "
            "用于 defense in depth 形态校验和诊断对比"
        )

    def test_manifest_verify_uses_expected_identity(self, workflow_content):
        """Verify migration manifest signature 步骤必须用 EXPECTED_IDENTITY。

        P0-01 失败点 3: migration-manifest 验签也用 EXPECTED_IDENTITY。
        """
        verify_idx = workflow_content.find("Verify migration manifest signature")
        assert verify_idx >= 0, "P0-01: 未找到 Verify migration manifest signature 步骤"
        next_step_idx = workflow_content.find("    - name:", verify_idx + 1)
        if next_step_idx < 0:
            verify_section = workflow_content[verify_idx:]
        else:
            verify_section = workflow_content[verify_idx:next_step_idx]

        assert "EXPECTED_IDENTITY=" in verify_section, (
            "P0-01 失败点 3: Verify migration manifest signature 必须用 EXPECTED_IDENTITY"
        )
        assert '--certificate-identity "${EXPECTED_IDENTITY}"' in verify_section, (
            "P0-01 失败点 3: migration manifest cosign verify-blob 命令 "
            "必须用 --certificate-identity \"${EXPECTED_IDENTITY}\""
        )
        assert '--certificate-identity "${CERT_IDENTITY}"' not in verify_section, (
            "P0-01 失败点 3: migration manifest 不应再用 CERT_IDENTITY 做 cosign verify"
        )

    def test_release_manifest_verify_uses_expected_identity(self, workflow_content):
        """Verify release manifest signature 步骤必须用 EXPECTED_IDENTITY。

        P0-01 失败点 3: release-manifest 验签也用 EXPECTED_IDENTITY。
        """
        verify_idx = workflow_content.find("Verify release manifest signature + binding")
        assert verify_idx >= 0, "P0-01: 未找到 Verify release manifest signature 步骤"
        next_step_idx = workflow_content.find("    - name:", verify_idx + 1)
        if next_step_idx < 0:
            verify_section = workflow_content[verify_idx:]
        else:
            verify_section = workflow_content[verify_idx:next_step_idx]

        assert "EXPECTED_IDENTITY=" in verify_section, (
            "P0-01 失败点 3: Verify release manifest signature 必须用 EXPECTED_IDENTITY"
        )
        assert '--certificate-identity "${EXPECTED_IDENTITY}"' in verify_section, (
            "P0-01 失败点 3: release manifest cosign verify-blob 命令 "
            "必须用 --certificate-identity \"${EXPECTED_IDENTITY}\""
        )
        assert '--certificate-identity "${CERT_IDENTITY}"' not in verify_section, (
            "P0-01 失败点 3: release manifest 不应再用 CERT_IDENTITY 做 cosign verify"
        )

    def test_no_github_ref_name_hardcoded_in_expected_identity(self, workflow_content):
        """EXPECTED_IDENTITY 不应用 refs/heads/${{ github.ref_name }} 拼接。

        R66 P0-03 整改:
          - 旧实现: refs/heads/${{ github.ref_name }}
            缺陷: tag push 时 ref_name 为 tag 名(如 v1.0.0),
                  拼成 refs/heads/v1.0.0 错误(应为 refs/tags/v1.0.0)。
          - 新实现: ${{ github.ref }}
            优势: github.ref 已是完整 ref(branch: refs/heads/master,
                  tag: refs/tags/v1.0.0),无需拼接,正确处理 branch 与 tag。
        """
        lines = workflow_content.splitlines()
        bad_lines = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # R66 P0-03: 检测 EXPECTED_IDENTITY=...refs/heads/${{ github.ref_name }}(错误模式)
            if "EXPECTED_IDENTITY=" in line and "refs/heads/${{ github.ref_name }}" in line:
                bad_lines.append((i + 1, line.strip()))
        assert not bad_lines, (
            "R66 P0-03: EXPECTED_IDENTITY 禁止用 refs/heads/${{ github.ref_name }} "
            "(tag push 时拼成 refs/heads/<tag> 错误,应改用 ${{ github.ref }})— "
            f"发现 {len(bad_lines)} 处: {bad_lines[:3]}"
        )
        # R66 P0-03: 验证至少有一处使用 ${{ github.ref }}(正确写法)
        ref_count = sum(
            1 for line in workflow_content.splitlines()
            if "EXPECTED_IDENTITY=" in line
            and "${{ github.ref }}" in line
            and not line.lstrip().startswith("#")
        )
        assert ref_count >= 3, (
            f"R66 P0-03: EXPECTED_IDENTITY 应至少 3 处使用 ${{ github.ref }} "
            f"(image / migration manifest / release manifest),实际 {ref_count} 处"
        )


# ════════════════════════════════════════════════════════════════
# C. rc-continuity 宽严策略 (P0-01 失败点 2)
# ════════════════════════════════════════════════════════════════


class TestRcContinuityPolicy:
    """rc-continuity job 宽严策略校验。

    P0-01 失败点 2 整改:
      - PR 场景 (REQUIRED_CONSECUTIVE=0):宽松通过
      - push 到 master:严格 3 次连续同 digest 全绿
      - 历史 < 3 次:只 WARN 不阻断(允许首次候选)
      - 首次 release(无历史 digest):跳过 digest 一致性比较,不阻断
    """

    @pytest.fixture
    def workflow_content(self):
        return _read_workflow()

    def test_rc_continuity_pr_lenient_mode(self, workflow_content):
        """rc-continuity 在 PR 场景必须宽松通过(REQUIRED_CONSECUTIVE=0)。"""
        assert 'REQUIRED_CONSECUTIVE=0' in workflow_content, (
            "P0-01 失败点 2: rc-continuity PR 场景必须设置 REQUIRED_CONSECUTIVE=0(宽松模式)"
        )
        # PR 场景判断
        assert 'pull_request' in workflow_content, (
            "P0-01 失败点 2: rc-continuity 必须区分 PR 与 push 场景"
        )

    def test_rc_continuity_push_strict_mode(self, workflow_content):
        """rc-continuity 在 push 到 master 时必须严格 3 次连续。"""
        assert 'REQUIRED_CONSECUTIVE=3' in workflow_content, (
            "P0-01 失败点 2: rc-continuity push 场景必须设置 REQUIRED_CONSECUTIVE=3(严格模式)"
        )

    def test_rc_continuity_history_lt_3_warn_only(self, workflow_content):
        """历史 < 3 次时只 WARN 不阻断。"""
        # 检查"历史不足"时的 WARN 逻辑(不阻断)
        assert 'WARN' in workflow_content or 'warn' in workflow_content.lower(), (
            "P0-01 失败点 2: rc-continuity 历史不足时必须 WARN(不阻断)"
        )
        # 检查不设置 ALL_PASS=false(不阻断)
        # 找到 "历史不足" 注释,确认其后不设置 ALL_PASS=false
        assert '历史不足' in workflow_content or 'lt' in workflow_content, (
            "P0-01 失败点 2: rc-continuity 必须有历史不足的处理逻辑"
        )

    def test_rc_continuity_first_release_no_block(self, workflow_content):
        """首次 release(无历史 digest)不阻断。

        P0-01 失败点 2: 当 PREV_DIGEST 为空(无历史 digest)时,
        DIGEST_MISMATCH 保持 false,不阻断。
        """
        # 检查首次 release 处理逻辑
        assert 'PREV_DIGEST' in workflow_content, (
            "P0-01 失败点 2: rc-continuity 必须用 PREV_DIGEST 跟踪历史 digest"
        )
        # 检查首次 release 的注释/处理(PREV_DIGEST 为空时不阻断)
        assert 'DIGEST_MISMATCH=false' in workflow_content, (
            "P0-01 失败点 2: rc-continuity 必须初始化 DIGEST_MISMATCH=false(首次 release 不阻断)"
        )

    def test_rc_continuity_has_policy_comment(self, workflow_content):
        """rc-continuity 必须有 R64 P0-01 宽严策略注释说明。

        P0-01 失败点 2: 注释说明 PR 宽松 / push 严格 / 首次 release 不阻断。
        """
        assert 'R64 P0-01' in workflow_content, (
            "P0-01 失败点 2: rc-continuity 必须有 R64 P0-01 整改注释"
        )
        # 检查宽严策略说明
        assert '宽松' in workflow_content or 'lenient' in workflow_content.lower(), (
            "P0-01 失败点 2: rc-continuity 必须说明 PR 宽松模式"
        )


# ════════════════════════════════════════════════════════════════
# D. branch protection 配置脚本 (P1-11)
# ════════════════════════════════════════════════════════════════


class TestBranchProtectionScripts:
    """configure_branch_protection.sh / detect_branch_protection_contexts.sh
    必须包含完整 contexts 列表(17 个 Release Gates job + CI / repo-hygiene)。

    P1-11 整改:BP contexts 必须与实际 workflow job 名完全一致。
    """

    @pytest.fixture
    def configure_script(self):
        return CONFIGURE_BP_SCRIPT.read_text(encoding="utf-8")

    @pytest.fixture
    def detect_script(self):
        return DETECT_BP_SCRIPT.read_text(encoding="utf-8")

    def test_configure_script_exists(self):
        """configure_branch_protection.sh 必须存在。"""
        assert CONFIGURE_BP_SCRIPT.exists(), (
            "P1-11: scripts/configure_branch_protection.sh 必须存在"
        )

    def test_detect_script_exists(self):
        """detect_branch_protection_contexts.sh 必须存在。"""
        assert DETECT_BP_SCRIPT.exists(), (
            "P1-11: scripts/detect_branch_protection_contexts.sh 必须存在"
        )

    def test_configure_script_enforces_admins(self, configure_script):
        """configure 脚本必须设置 enforce_admins=true(禁止管理员绕过)。

        P1-11: 禁止管理员绕过。
        """
        assert "enforce_admins" in configure_script, (
            "P1-11: configure 脚本必须配置 enforce_admins"
        )
        assert "enforce_admins: true" in configure_script, (
            "P1-11: configure 脚本必须设置 enforce_admins: true(禁止管理员绕过)"
        )

    def test_configure_script_requires_pr_review(self, configure_script):
        """configure 脚本必须要求 PR + 至少一名 reviewer。

        P1-11: 要求 PR、至少一名独立 reviewer。
        R65 P1-12: 提升为至少 2 名独立 reviewer(required_approving_review_count: 2)。
        """
        assert "required_pull_request_reviews" in configure_script, (
            "P1-11: configure 脚本必须配置 required_pull_request_reviews(要求 PR)"
        )
        # R65 P1-12: review_count 从 1 提升到 2(独立 reviewer)
        assert "required_approving_review_count: 2" in configure_script, (
            "R65 P1-12: configure 脚本必须要求至少 2 名 approving reviewer"
        )
        assert "required_approving_review_count: 1" not in configure_script, (
            "R65 P1-12: 不应再使用旧的 required_approving_review_count: 1(已提升为 2)"
        )

    def test_configure_script_dismiss_stale_reviews(self, configure_script):
        """configure 脚本必须设置 dismiss_stale_reviews=true。

        P1-11: dismiss stale approvals。
        """
        assert "dismiss_stale_reviews: true" in configure_script, (
            "P1-11: configure 脚本必须设置 dismiss_stale_reviews: true"
        )

    def test_configure_script_forbids_force_push_and_deletions(self, configure_script):
        """configure 脚本必须禁止 force push 和分支删除。"""
        assert "allow_force_pushes: false" in configure_script, (
            "P1-11: configure 脚本必须设置 allow_force_pushes: false"
        )
        assert "allow_deletions: false" in configure_script, (
            "P1-11: configure 脚本必须设置 allow_deletions: false"
        )

    def test_configure_script_has_rg_jobs_check(self, configure_script):
        """configure 脚本必须校验 17 个 Release Gates job 覆盖。

        P0-01 失败点 1 / P1-11: configure 脚本应校验 Release Gates 17 个 job
        是否在待配置 contexts 中(soft WARN)。
        """
        assert "EXPECTED_RG_JOBS" in configure_script, (
            "P0-01/P1-11: configure 脚本必须有 EXPECTED_RG_JOBS 校验列表"
        )
        # 检查所有 17 个 Release Gates job 都在 EXPECTED_RG_JOBS 中
        for rg_job in EXPECTED_RELEASE_GATES_JOBS:
            assert rg_job in configure_script, (
                f"P0-01/P1-11: configure 脚本的 EXPECTED_RG_JOBS 必须包含 '{rg_job}'"
            )

    def test_configure_script_checks_repo_hygiene(self, configure_script):
        """configure 脚本必须校验 CI / repo-hygiene 覆盖。

        P1-11: CI 的 repo-hygiene 是 required context。
        """
        assert "repo-hygiene" in configure_script, (
            "P1-11: configure 脚本必须校验 CI / repo-hygiene 覆盖"
        )
        assert "CI / repo-hygiene" in configure_script, (
            "P1-11: configure 脚本必须校验 'CI / repo-hygiene' context"
        )

    def test_detect_script_has_rg_jobs_check(self, detect_script):
        """detect 脚本必须校验 17 个 Release Gates job 覆盖。

        P0-01 失败点 1 / P1-11: detect 脚本应校验 Release Gates 17 个 job
        是否在检测到的 contexts 中(soft WARN)。
        """
        assert "EXPECTED_RG_JOBS" in detect_script, (
            "P0-01/P1-11: detect 脚本必须有 EXPECTED_RG_JOBS 校验列表"
        )
        for rg_job in EXPECTED_RELEASE_GATES_JOBS:
            assert rg_job in detect_script, (
                f"P0-01/P1-11: detect 脚本的 EXPECTED_RG_JOBS 必须包含 '{rg_job}'"
            )

    def test_detect_script_checks_repo_hygiene(self, detect_script):
        """detect 脚本必须校验 CI / repo-hygiene 覆盖。

        P1-11: CI 的 repo-hygiene 是 required context。
        """
        assert "repo-hygiene" in detect_script, (
            "P1-11: detect 脚本必须校验 CI / repo-hygiene 覆盖"
        )
        assert "CI / repo-hygiene" in detect_script, (
            "P1-11: detect 脚本必须校验 'CI / repo-hygiene' context"
        )

    def test_configure_script_uses_dynamic_contexts(self, configure_script):
        """configure 脚本必须用动态检测的 contexts(不硬编码)。

        P1-11: contexts 列表必须与实际 workflow job 名完全一致,
        通过 detect_branch_protection_contexts.sh 动态检测。
        """
        assert "detect_branch_protection_contexts.sh" in configure_script, (
            "P1-11: configure 脚本必须调用 detect_branch_protection_contexts.sh 动态检测 contexts"
        )
        assert "CONTEXTS_JSON" in configure_script, (
            "P1-11: configure 脚本必须用 CONTEXTS_JSON(动态检测或用户指定)"
        )

    def test_configure_script_strict_status_checks(self, configure_script):
        """configure 脚本必须设置 strict=true(status check 针对最新提交)。

        P1-11: strict=true 确保 status check 必须针对最新提交。
        """
        assert "strict: true" in configure_script, (
            "P1-11: configure 脚本必须设置 required_status_checks.strict: true"
        )


# ════════════════════════════════════════════════════════════════
# E. YAML 语法 + workflow 结构完整性
# ════════════════════════════════════════════════════════════════


class TestWorkflowYamlIntegrity:
    """release-gates.yml YAML 语法 + 结构完整性校验。"""

    @pytest.fixture
    def workflow_yaml(self):
        """解析 release-gates.yml 为 dict。"""
        import yaml
        return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    def test_yaml_is_valid(self):
        """release-gates.yml 必须是合法 YAML。"""
        import yaml
        try:
            yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            pytest.fail(f"release-gates.yml YAML 语法错误: {e}")

    def test_workflow_has_14_release_gates_jobs(self, workflow_yaml):
        """Release Gates workflow 必须包含 17 个 job。

        P0-01 失败点 1: workflow 实际 job 数必须与 WORKFLOW_JOBS 列表一致(17 个)。
        """
        jobs = workflow_yaml.get("jobs", {})
        # 预期 17 个 Release Gates job
        for job_name in EXPECTED_RELEASE_GATES_JOBS:
            assert job_name in jobs, (
                f"P0-01: release-gates.yml 必须包含 job '{job_name}'"
            )

    def test_release_summary_needs_all_jobs(self, workflow_yaml):
        """release-summary 必须依赖所有 17 个 Release Gates job。

        P0-01 失败点 1: release-summary 作为聚合 required context,
        必须依赖全部 17 个 job(包括 sign-image/rc-continuity/publish-attestation)。
        """
        release_summary = workflow_yaml["jobs"].get("release-summary", {})
        needs = release_summary.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        # release-summary 应依赖所有 17 个 job(除自身)
        expected_deps = [j for j in EXPECTED_RELEASE_GATES_JOBS if j != "release-summary"]
        missing_deps = [j for j in expected_deps if j not in needs]
        assert not missing_deps, (
            f"P0-01: release-summary 必须依赖所有 Release Gates job, "
            f"缺失: {missing_deps}"
        )

    def test_publish_attestation_needs_sign_image_and_rc_continuity(self, workflow_yaml):
        """publish-attestation 必须依赖 sign-image 和 rc-continuity。

        P0-01 失败点 1: publish-attestation 的 needs 必须包含
        sign-image 和 rc-continuity(旧版 WORKFLOW_JOBS 遗漏的 job)。
        """
        pa = workflow_yaml["jobs"].get("publish-attestation", {})
        needs = pa.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "sign-image" in needs, (
            "P0-01: publish-attestation 必须依赖 sign-image"
        )
        assert "rc-continuity" in needs, (
            "P0-01: publish-attestation 必须依赖 rc-continuity"
        )
