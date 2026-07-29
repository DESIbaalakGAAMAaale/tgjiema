"""R67 Wave 5: RC tag 正式演练(RC tag drill)测试。

R67 审计要求(audit report Wave 4 — RC tag 正式演练):
    - signed annotated RC tag。
    - tag workflow、environment approval、production evidence、
      digest-pinned deploy、rollback 全通过。

测试覆盖:
    A. 阶段 1 — verify_signed_annotated_tag (signed annotated tag 验证)
        - dry_run 模式通过
        - tag 不存在 → 失败
        - lightweight tag (非 annotated) → 失败
        - annotated tag 但未签名 → 失败
        - signed annotated tag → 通过
        - mock git 子命令链路验证
    B. 阶段 2 — verify_tag_workflow_triggered (tag workflow 触发验证)
        - dry_run 模式通过
        - 无法确定 repo → 失败
        - 无 workflow run → 失败
        - head_sha 不匹配 → 失败(P0-03 同 SHA 验证)
        - workflow 未完成 → 失败
        - workflow 失败(conclusion != success)→ 失败
        - workflow 成功 + SHA 匹配 → 通过
    C. 阶段 3 — verify_environment_approval (环境审批验证)
        - 使用 Wave 4 EnvironmentApprovalGate
        - 审批通过 / 审批拒绝(approver==executor)/ candidate_tag 不匹配
    D. 阶段 4 — verify_production_evidence_complete (production evidence 验证)
        - 文件不存在 → 失败
        - 缺少 artifact 类型 → 失败
        - dry_run 模式 → 跳过严格门禁
        - 严格门禁通过 → 通过
        - 严格门禁拒绝 → 失败
    E. 阶段 5 — verify_digest_pinned_deploy (digest 锁定部署验证)
        - 使用 Wave 4 DigestPinnedDeployVerifier
        - digest 匹配 / digest 不匹配 / :tag 引用(非 @sha256:)
    F. 阶段 6 — verify_rollback_capability (rollback 能力验证)
        - dry_run 模式通过
        - 文件不存在 → 失败
        - active_pointer 缺失 → 失败
        - 已过期 → 失败
        - fencing_token 缺失 → 失败(P1-06)
        - environment_id 不匹配 → 失败
        - 完整有效 → 通过
    G. run_drill 编排
        - 全部通过(dry_run)
        - 部分失败(collect-all 模式)
        - 6 阶段都执行
    H. verify_drill_report
        - 报告不存在 / 缺少阶段 / drill 未通过 / dry_run 报告 / 通过
    I. CLI
        - --help / drill / verify / rollback-check 子命令
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.rc_tag_drill import (
    DRILL_STAGES,
    _make_result,
    main,
    run_drill,
    verify_digest_pinned_deploy,
    verify_drill_report,
    verify_environment_approval,
    verify_production_evidence_complete,
    verify_rollback_capability,
    verify_signed_annotated_tag,
    verify_tag_workflow_triggered,
)


# ════════════════════════════════════════════════════════════════
# 测试常量与 fixtures
# ════════════════════════════════════════════════════════════════

VALID_DIGEST = "a" * 64
VALID_DEPLOY_REF = f"ghcr.io/owner/repo@sha256:{VALID_DIGEST}"


def _make_valid_approval_record(*, approver="manager@example.com",
                                candidate_tag="v1.0.0-rc1",
                                environment_id="production-vps-01"):
    """构造有效的审批记录(供 Wave 4 EnvironmentApprovalGate 通过)。

    Wave 4 EnvironmentApprovalGate 使用 approved_by 字段(非 approver),
    与 production evidence artifact 的 approved_by 字段保持一致。
    """
    return {
        "approved_by": approver,  # EnvironmentApprovalGate 期望的字段名
        "candidate_tag": candidate_tag,
        "environment_id": environment_id,
        "approved_at": "2026-07-20T10:00:00+00:00",
        "expires_at": "2099-12-31T23:59:59+00:00",
        "revoked": False,
    }


def _make_valid_evidence(tmp_path: Path) -> Path:
    """构造有效的 production evidence 文件(6 类 artifact 齐全)。"""
    from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES
    evidence = {
        "evidence_mode": "production",
        "signature": {"method": "gpg", "verified": True},
        "flags": {},
        "artifacts": [
            {
                "artifact_type": atype,
                "environment_id": "production-vps-01",
                "commit_sha": "abc123def456",
                "image_digest": f"sha256:{VALID_DIGEST}",
                "started_at": "2026-07-20T00:00:00+00:00",
                "ended_at": "2026-07-20T01:00:00+00:00",
                "expires_at": "2099-12-31T23:59:59+00:00",
                "raw_data_digest": "sha256:raw",
                "executed_by": "ops@example.com",
                "approved_by": "manager@example.com",
                "signature": "gpg-signature-base64",
                "nonce": f"nonce-{atype}-{uuid.uuid4().hex}",
                "attestation_digest": f"sha256:att-{atype}",
                "time_window": {
                    "start": "2026-07-20T00:00:00+00:00",
                    "end": "2026-07-20T01:00:00+00:00",
                },
                "consumed": False,
            }
            for atype in REQUIRED_ARTIFACT_TYPES
        ],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _make_valid_rollback_target(tmp_path: Path,
                                environment_id="production-vps-01") -> Path:
    """构造有效的 rollback target 文件。"""
    target = {
        "environment_id": environment_id,
        "active_pointer": {"version": "v1.0.0", "image": "ghcr.io/owner/repo:old"},
        "expires_at": "2099-12-31T23:59:59+00:00",
        "fencing_token": "fence-token-abc-123",
        "operation_id": "op-2026-07-20-001",
    }
    path = tmp_path / f"rollback_target_{environment_id}.json"
    path.write_text(json.dumps(target), encoding="utf-8")
    return path


# ════════════════════════════════════════════════════════════════
# A. 阶段 1 — verify_signed_annotated_tag
# ════════════════════════════════════════════════════════════════

class TestVerifySignedAnnotatedTag:
    """阶段 1: signed annotated RC tag 验证。"""

    def test_dry_run_passes(self):
        """dry_run 模式直接通过,不调用 git。"""
        result = verify_signed_annotated_tag("v1.0.0-rc1", dry_run=True)
        assert result["passed"] is True
        assert result["details"]["dry_run"] is True
        assert result["details"]["signature_verified"] is True

    @patch("scripts.rc_tag_drill._run_git")
    def test_tag_not_exists_fails(self, mock_git):
        """tag 不存在 → 失败。"""
        # git rev-parse --verify refs/tags/<tag> 失败
        mock_git.return_value = (1, "", "unknown revision")
        result = verify_signed_annotated_tag("v1.0.0-nonexistent")
        assert result["passed"] is False
        assert "tag 不存在" in result["reason"]
        assert result["details"]["tag_exists"] is False

    @patch("scripts.rc_tag_drill._run_git")
    def test_lightweight_tag_fails(self, mock_git):
        """lightweight tag (非 annotated) → 失败。"""
        # 模拟:rev-parse 成功,cat-file 返回 "commit"(lightweight)
        mock_git.side_effect = [
            (0, "abc123\n", ""),  # rev-parse --verify
            (0, "commit\n", ""),  # cat-file -t (lightweight tag)
        ]
        result = verify_signed_annotated_tag("v1.0.0-lightweight")
        assert result["passed"] is False
        assert "annotated" in result["reason"].lower() or "lightweight" in result["reason"].lower()
        assert result["details"]["tag_type"] == "commit"

    @patch("scripts.rc_tag_drill._run_git")
    def test_unsigned_annotated_tag_fails(self, mock_git):
        """annotated tag 但未签名 → 失败。"""
        mock_git.side_effect = [
            (0, "abc123\n", ""),  # rev-parse --verify
            (0, "tag\n", ""),     # cat-file -t (annotated)
            (1, "", "gpg: signature verification failed"),  # tag -v
        ]
        result = verify_signed_annotated_tag("v1.0.0-unsigned")
        assert result["passed"] is False
        assert "GPG 签名验证失败" in result["reason"]
        assert result["details"]["signature_verified"] is False

    @patch("scripts.rc_tag_drill._run_git")
    def test_signed_annotated_tag_passes(self, mock_git):
        """signed annotated tag → 通过。"""
        mock_git.side_effect = [
            (0, "abc123\n", ""),          # rev-parse --verify
            (0, "tag\n", ""),             # cat-file -t (annotated)
            (0, "", "gpg: Good signature"),  # tag -v (签名验证通过)
            (0, "def456\n", ""),          # rev-list -n 1 (commit SHA)
            (0, "", ""),                  # verify-commit (可选,通过)
        ]
        result = verify_signed_annotated_tag("v1.0.0-rc1")
        assert result["passed"] is True
        assert result["details"]["signature_verified"] is True
        assert result["details"]["tag_type"] == "tag"
        assert result["details"]["commit_sha"] == "def456"


# ════════════════════════════════════════════════════════════════
# B. 阶段 2 — verify_tag_workflow_triggered
# ════════════════════════════════════════════════════════════════

class TestVerifyTagWorkflowTriggered:
    """阶段 2: tag workflow 触发验证。"""

    def test_dry_run_passes(self):
        """dry_run 模式直接通过。"""
        result = verify_tag_workflow_triggered("v1.0.0-rc1", dry_run=True)
        assert result["passed"] is True
        assert result["details"]["dry_run"] is True
        assert result["details"]["conclusion"] == "success"

    @patch("scripts.rc_tag_drill._run_git")
    def test_no_repo_fails(self, mock_git, monkeypatch):
        """无法确定 repo → 失败。"""
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        # git remote get-url 失败
        mock_git.return_value = (1, "", "")
        result = verify_tag_workflow_triggered("v1.0.0-rc1")
        assert result["passed"] is False
        assert "无法确定 GitHub 仓库" in result["reason"]

    @patch("scripts.rc_tag_drill._run_gh")
    @patch("scripts.rc_tag_drill._run_git")
    def test_no_workflow_runs_fails(self, mock_git, mock_gh):
        """无 workflow run → 失败。"""
        mock_git.side_effect = [
            (0, "git@github.com:owner/repo.git\n", ""),  # remote get-url
            (0, "abc123\n", ""),                          # rev-list -n 1
        ]
        mock_gh.return_value = (0, "[]", "")  # 空 run 列表
        result = verify_tag_workflow_triggered("v1.0.0-rc1")
        assert result["passed"] is False
        assert "未找到" in result["reason"] or "workflow run" in result["reason"]

    @patch("scripts.rc_tag_drill._run_gh")
    @patch("scripts.rc_tag_drill._run_git")
    def test_sha_mismatch_fails(self, mock_git, mock_gh):
        """head_sha 不匹配 tag commit → 失败(P0-03 同 SHA 验证)。"""
        mock_git.side_effect = [
            (0, "git@github.com:owner/repo.git\n", ""),
            (0, "abc123\n", ""),  # tag 指向的 commit
        ]
        # run 存在但 head_sha 不同(祖先 run 复用)
        runs = [{"databaseId": 1, "status": "completed", "conclusion": "success",
                 "headSha": "different789", "event": "push", "headBranch": "v1.0.0-rc1"}]
        mock_gh.return_value = (0, json.dumps(runs), "")
        result = verify_tag_workflow_triggered("v1.0.0-rc1")
        assert result["passed"] is False
        assert "head_sha" in result["reason"] or "SHA" in result["reason"] or "祖先" in result["reason"]

    @patch("scripts.rc_tag_drill._run_gh")
    @patch("scripts.rc_tag_drill._run_git")
    def test_workflow_in_progress_fails(self, mock_git, mock_gh):
        """workflow 未完成 → 失败。"""
        # 显式传入 repo,跳过 git remote get-url 调用
        # (CI 环境下 GITHUB_REPOSITORY 已设置,会导致 mock side_effect 顺序失配)
        mock_git.side_effect = [
            (0, "abc123\n", ""),  # rev-list -n 1 <tag>
        ]
        runs = [{"databaseId": 1, "status": "in_progress", "conclusion": "",
                 "headSha": "abc123", "event": "push", "headBranch": "v1.0.0-rc1"}]
        mock_gh.return_value = (0, json.dumps(runs), "")
        result = verify_tag_workflow_triggered("v1.0.0-rc1", repo="owner/repo")
        assert result["passed"] is False
        assert "未完成" in result["reason"]

    @patch("scripts.rc_tag_drill._run_gh")
    @patch("scripts.rc_tag_drill._run_git")
    def test_workflow_failure_conclusion_fails(self, mock_git, mock_gh):
        """workflow conclusion=failure → 失败。"""
        # 显式传入 repo,跳过 git remote get-url 调用
        # (CI 环境下 GITHUB_REPOSITORY 已设置,会导致 mock side_effect 顺序失配)
        mock_git.side_effect = [
            (0, "abc123\n", ""),  # rev-list -n 1 <tag>
        ]
        runs = [{"databaseId": 1, "status": "completed", "conclusion": "failure",
                 "headSha": "abc123", "event": "push", "headBranch": "v1.0.0-rc1"}]
        mock_gh.return_value = (0, json.dumps(runs), "")
        result = verify_tag_workflow_triggered("v1.0.0-rc1", repo="owner/repo")
        assert result["passed"] is False
        assert "失败" in result["reason"] or "failure" in result["reason"].lower()

    @patch("scripts.rc_tag_drill._run_gh")
    @patch("scripts.rc_tag_drill._run_git")
    def test_workflow_success_same_sha_passes(self, mock_git, mock_gh):
        """workflow success + SHA 匹配 → 通过。"""
        # 显式传入 repo,跳过 git remote get-url 调用
        # (CI 环境下 GITHUB_REPOSITORY 已设置,会导致 mock side_effect 顺序失配)
        mock_git.side_effect = [
            (0, "abc123\n", ""),  # rev-list -n 1 <tag>
        ]
        runs = [{"databaseId": 123, "status": "completed", "conclusion": "success",
                 "headSha": "abc123", "event": "push", "headBranch": "v1.0.0-rc1"}]
        mock_gh.return_value = (0, json.dumps(runs), "")
        result = verify_tag_workflow_triggered("v1.0.0-rc1", repo="owner/repo")
        assert result["passed"] is True
        assert result["details"]["conclusion"] == "success"
        assert result["details"]["same_sha"] is True


# ════════════════════════════════════════════════════════════════
# C. 阶段 3 — verify_environment_approval
# ════════════════════════════════════════════════════════════════

class TestVerifyEnvironmentApproval:
    """阶段 3: 环境审批验证(使用 Wave 4 EnvironmentApprovalGate)。"""

    def test_valid_approval_passes(self):
        approval = _make_valid_approval_record()
        result = verify_environment_approval(
            approval_record=approval,
            candidate_tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            executed_by="ops@example.com",
        )
        assert result["passed"] is True

    def test_same_person_approver_executor_fails(self):
        """职责分离:approver == executor → 失败。"""
        approval = _make_valid_approval_record(approver="same@example.com")
        result = verify_environment_approval(
            approval_record=approval,
            candidate_tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            executed_by="same@example.com",
        )
        assert result["passed"] is False
        assert "职责分离" in result["reason"] or "approver" in result["reason"].lower()

    def test_wrong_candidate_tag_fails(self):
        """candidate_tag 不匹配 → 失败。"""
        approval = _make_valid_approval_record(candidate_tag="v1.0.0-rc1")
        result = verify_environment_approval(
            approval_record=approval,
            candidate_tag="v2.0.0-rc1",  # 不同的 tag
            environment_id="production-vps-01",
            executed_by="ops@example.com",
        )
        assert result["passed"] is False
        assert "candidate_tag" in result["reason"].lower() or "tag" in result["reason"].lower()

    def test_wrong_environment_id_fails(self):
        """environment_id 不匹配 → 失败。"""
        approval = _make_valid_approval_record(environment_id="production-vps-01")
        result = verify_environment_approval(
            approval_record=approval,
            candidate_tag="v1.0.0-rc1",
            environment_id="staging-vps-99",  # 不同的环境
            executed_by="ops@example.com",
        )
        assert result["passed"] is False


# ════════════════════════════════════════════════════════════════
# D. 阶段 4 — verify_production_evidence_complete
# ════════════════════════════════════════════════════════════════

class TestVerifyProductionEvidenceComplete:
    """阶段 4: production evidence 完整性验证。"""

    def test_file_not_exists_fails(self, tmp_path):
        result = verify_production_evidence_complete(tmp_path / "missing.json")
        assert result["passed"] is False
        assert "不存在" in result["reason"]

    def test_missing_artifact_type_fails(self, tmp_path):
        """缺少 artifact 类型 → 失败。"""
        evidence = {
            "evidence_mode": "production",
            "artifacts": [
                {"artifact_type": "SOAK_7DAY"},
                # 缺少其它 5 类
            ],
        }
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        result = verify_production_evidence_complete(path, dry_run=True)
        assert result["passed"] is False
        assert "缺少" in result["reason"] or "artifact" in result["reason"].lower()

    def test_dry_run_skips_strict_gate(self, tmp_path):
        """dry_run 模式跳过严格门禁,仅检查文件结构。"""
        path = _make_valid_evidence(tmp_path)
        result = verify_production_evidence_complete(path, dry_run=True)
        assert result["passed"] is True
        assert result["details"]["dry_run"] is True
        assert result["details"]["artifact_count"] == 6

    def test_strict_gate_passes_with_valid_evidence(self, tmp_path):
        """完整有效 evidence → 严格门禁通过。"""
        path = _make_valid_evidence(tmp_path)
        result = verify_production_evidence_complete(path, dry_run=False)
        assert result["passed"] is True
        assert "production_promotion_allowed" in str(result["details"]["verification"])

    def test_strict_gate_fails_with_invalid_evidence(self, tmp_path):
        """evidence_mode=dry_run → 严格门禁拒绝。"""
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES
        evidence = {
            "evidence_mode": "dry_run",  # 非 production
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": [
                {
                    "artifact_type": atype,
                    "environment_id": "production-vps-01",
                    "commit_sha": "abc123",
                    "image_digest": f"sha256:{VALID_DIGEST}",
                    "started_at": "2026-07-20T00:00:00+00:00",
                    "ended_at": "2026-07-20T01:00:00+00:00",
                    "expires_at": "2099-12-31T23:59:59+00:00",
                    "raw_data_digest": "sha256:raw",
                    "executed_by": "ops@example.com",
                    "approved_by": "manager@example.com",
                    "signature": "gpg-sig",
                    "nonce": f"n-{atype}-{uuid.uuid4().hex}",
                    "attestation_digest": f"sha256:att-{atype}",
                    "time_window": {"start": "2026-07-20T00:00:00+00:00",
                                    "end": "2026-07-20T01:00:00+00:00"},
                    "consumed": False,
                }
                for atype in REQUIRED_ARTIFACT_TYPES
            ],
        }
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        result = verify_production_evidence_complete(path, dry_run=False)
        assert result["passed"] is False


# ════════════════════════════════════════════════════════════════
# E. 阶段 5 — verify_digest_pinned_deploy
# ════════════════════════════════════════════════════════════════

class TestVerifyDigestPinnedDeploy:
    """阶段 5: digest 锁定部署验证(使用 Wave 4 DigestPinnedDeployVerifier)。"""

    def test_valid_digest_match_passes(self):
        result = verify_digest_pinned_deploy(
            deploy_ref=VALID_DEPLOY_REF,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
        )
        assert result["passed"] is True

    def test_tag_only_ref_fails(self):
        """:tag 引用(非 @sha256:)→ 失败。"""
        result = verify_digest_pinned_deploy(
            deploy_ref="ghcr.io/owner/repo:v1.0.0",  # 用 tag 而非 digest
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
        )
        assert result["passed"] is False

    def test_digest_mismatch_fails(self):
        """digest 不匹配 release_manifest → 失败。"""
        result = verify_digest_pinned_deploy(
            deploy_ref=VALID_DEPLOY_REF,
            release_manifest_digest="sha256:" + "b" * 64,  # 不同的 digest
        )
        assert result["passed"] is False

    def test_all_digests_match_passes(self):
        """manifest + attestation + verify-only-3x 三处 digest 一致 → 通过。"""
        result = verify_digest_pinned_deploy(
            deploy_ref=VALID_DEPLOY_REF,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
            attestation_subject_digest=f"sha256:{VALID_DIGEST}",
            verify_only_3x_digest=f"sha256:{VALID_DIGEST}",
        )
        assert result["passed"] is True


# ════════════════════════════════════════════════════════════════
# F. 阶段 6 — verify_rollback_capability
# ════════════════════════════════════════════════════════════════

class TestVerifyRollbackCapability:
    """阶段 6: rollback 能力验证(P1-06 recovery reconciler)。"""

    def test_dry_run_passes(self):
        result = verify_rollback_capability("production-vps-01", dry_run=True)
        assert result["passed"] is True
        assert result["details"]["dry_run"] is True

    def test_file_not_exists_fails(self, tmp_path):
        path = tmp_path / "missing.json"
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is False
        assert "不存在" in result["reason"]

    def test_missing_active_pointer_fails(self, tmp_path):
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({
            "environment_id": "production-vps-01",
            "active_pointer": "",  # 空
            "expires_at": "2099-12-31T23:59:59+00:00",
            "fencing_token": "fence-abc",
            "operation_id": "op-001",
        }), encoding="utf-8")
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is False
        assert "active_pointer" in result["reason"]

    def test_expired_target_fails(self, tmp_path):
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({
            "environment_id": "production-vps-01",
            "active_pointer": {"version": "v0.9.0"},
            "expires_at": "2020-01-01T00:00:00+00:00",  # 已过期
            "fencing_token": "fence-abc",
            "operation_id": "op-001",
        }), encoding="utf-8")
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is False
        assert "过期" in result["reason"]

    def test_missing_fencing_token_fails(self, tmp_path):
        """fencing_token 缺失 → 失败(P1-06 CAS 防并发)。"""
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({
            "environment_id": "production-vps-01",
            "active_pointer": {"version": "v0.9.0"},
            "expires_at": "2099-12-31T23:59:59+00:00",
            "fencing_token": "",  # 缺失
            "operation_id": "op-001",
        }), encoding="utf-8")
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is False
        assert "fencing_token" in result["reason"]

    def test_environment_mismatch_fails(self, tmp_path):
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({
            "environment_id": "staging-vps-99",  # 不同的环境
            "active_pointer": {"version": "v0.9.0"},
            "expires_at": "2099-12-31T23:59:59+00:00",
            "fencing_token": "fence-abc",
            "operation_id": "op-001",
        }), encoding="utf-8")
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is False
        assert "environment_id" in result["reason"].lower() or "不匹配" in result["reason"]

    def test_valid_target_passes(self, tmp_path):
        path = _make_valid_rollback_target(tmp_path)
        result = verify_rollback_capability(
            "production-vps-01", rollback_target_path=path,
        )
        assert result["passed"] is True
        assert result["details"]["fencing_token_present"] is True


# ════════════════════════════════════════════════════════════════
# G. run_drill 编排
# ════════════════════════════════════════════════════════════════

class TestRunDrill:
    """run_drill 综合编排测试。"""

    def test_full_drill_dry_run_passes(self, tmp_path):
        """dry_run 模式:全部 6 阶段通过。"""
        evidence_path = _make_valid_evidence(tmp_path)
        approval = _make_valid_approval_record()
        report = run_drill(
            tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            deploy_ref=VALID_DEPLOY_REF,
            approval_record=approval,
            evidence_path=evidence_path,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
            executed_by="ops@example.com",
            dry_run=True,
        )
        assert report["drill_passed"] is True
        assert report["stages_passed"] == 6
        assert report["stages_total"] == 6
        assert report["dry_run"] is True
        assert len(report["failures"]) == 0
        # 6 个阶段都存在
        for stage in DRILL_STAGES:
            assert stage in report["stages"]

    def test_drill_collects_all_failures(self, tmp_path):
        """部分阶段失败:collect-all 模式不中断后续阶段。"""
        # 用无效 evidence(缺少 artifact 类型)触发阶段 4 失败
        bad_evidence = tmp_path / "evidence.json"
        bad_evidence.write_text(json.dumps({
            "evidence_mode": "production",
            "artifacts": [{"artifact_type": "SOAK_7DAY"}],  # 缺少其它 5 类
        }), encoding="utf-8")
        approval = _make_valid_approval_record()
        report = run_drill(
            tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            deploy_ref="ghcr.io/owner/repo:v1.0.0",  # 用 :tag 触发阶段 5 失败
            approval_record=approval,
            evidence_path=bad_evidence,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
            executed_by="ops@example.com",
            dry_run=True,  # 阶段 1/2/6 dry-run 通过
        )
        assert report["drill_passed"] is False
        assert report["stages_passed"] < 6
        # 阶段 4 (evidence) 和阶段 5 (deploy) 应失败
        assert not report["stages"]["verify_production_evidence_complete"]["passed"]
        assert not report["stages"]["verify_digest_pinned_deploy"]["passed"]
        # 但其它阶段仍执行(dry-run 通过)
        assert report["stages"]["verify_signed_annotated_tag"]["passed"]
        assert report["stages"]["verify_tag_workflow_triggered"]["passed"]
        assert report["stages"]["verify_environment_approval"]["passed"]
        assert report["stages"]["verify_rollback_capability"]["passed"]

    def test_drill_six_stages_always_present(self, tmp_path):
        """6 个阶段总是出现在报告中(无论通过/失败)。"""
        evidence_path = _make_valid_evidence(tmp_path)
        approval = _make_valid_approval_record()
        report = run_drill(
            tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            deploy_ref=VALID_DEPLOY_REF,
            approval_record=approval,
            evidence_path=evidence_path,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
            executed_by="ops@example.com",
            dry_run=True,
        )
        assert set(report["stages"].keys()) == set(DRILL_STAGES)

    def test_drill_report_structure(self, tmp_path):
        """drill 报告结构完整(顶层字段齐全)。"""
        evidence_path = _make_valid_evidence(tmp_path)
        approval = _make_valid_approval_record()
        report = run_drill(
            tag="v1.0.0-rc1",
            environment_id="production-vps-01",
            deploy_ref=VALID_DEPLOY_REF,
            approval_record=approval,
            evidence_path=evidence_path,
            release_manifest_digest=f"sha256:{VALID_DIGEST}",
            executed_by="ops@example.com",
            dry_run=True,
        )
        assert "drill_passed" in report
        assert "stages_passed" in report
        assert "stages_total" in report
        assert "stages" in report
        assert "failures" in report
        assert "dry_run" in report
        assert "drilled_at" in report
        assert "tag" in report
        assert "environment_id" in report


# ════════════════════════════════════════════════════════════════
# H. verify_drill_report
# ════════════════════════════════════════════════════════════════

class TestVerifyDrillReport:
    """verify_drill_report 测试。"""

    def test_report_not_exists(self, tmp_path):
        result = verify_drill_report(tmp_path / "missing.json")
        assert result["valid"] is False
        assert "不存在" in result["reason"]

    def test_report_missing_stages(self, tmp_path):
        """报告缺少阶段 → 无效。"""
        report = {
            "drill_passed": True,
            "dry_run": False,
            "stages": {"verify_signed_annotated_tag": {"passed": True}},  # 仅 1 个
        }
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        result = verify_drill_report(path)
        assert result["valid"] is False
        assert "缺少" in result["reason"]

    def test_report_drill_not_passed(self, tmp_path):
        """drill_passed=False → 无效。"""
        report = {
            "drill_passed": False,
            "dry_run": False,
            "stages": {s: {"passed": True} for s in DRILL_STAGES},
            "failures": ["[阶段1] 模拟失败"],
        }
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        result = verify_drill_report(path)
        assert result["valid"] is False
        assert "未通过" in result["reason"]

    def test_report_dry_run_invalid(self, tmp_path):
        """dry_run=True 的报告不能作为生产证据 → 无效。"""
        report = {
            "drill_passed": True,
            "dry_run": True,
            "stages": {s: {"passed": True} for s in DRILL_STAGES},
            "failures": [],
        }
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        result = verify_drill_report(path)
        assert result["valid"] is False
        assert "dry-run" in result["reason"].lower() or "dry_run" in result["reason"]

    def test_report_valid_production_drill(self, tmp_path):
        """6 阶段全通过 + 非 dry_run → 有效。"""
        report = {
            "drill_passed": True,
            "dry_run": False,
            "stages": {s: {"passed": True} for s in DRILL_STAGES},
            "failures": [],
            "drilled_at": "2026-07-20T10:00:00+00:00",
            "tag": "v1.0.0-rc1",
            "environment_id": "production-vps-01",
        }
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        result = verify_drill_report(path)
        assert result["valid"] is True


# ════════════════════════════════════════════════════════════════
# I. CLI
# ════════════════════════════════════════════════════════════════

class TestCli:
    """CLI 入口测试。"""

    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_no_subcommand_fails(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_rollback_check_dry_run(self, capsys):
        rc = main(["rollback-check", "--environment-id", "test-env", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["passed"] is True

    def test_drill_dry_run(self, capsys, tmp_path):
        """完整 drill dry-run 模式:6 阶段通过,返回 0。"""
        evidence_path = _make_valid_evidence(tmp_path)
        approval = _make_valid_approval_record()
        approval_path = tmp_path / "approval.json"
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        report_path = tmp_path / "report.json"

        rc = main([
            "drill",
            "--tag", "v1.0.0-rc1",
            "--environment-id", "production-vps-01",
            "--deploy-ref", VALID_DEPLOY_REF,
            "--approval-record", str(approval_path),
            "--evidence-path", str(evidence_path),
            "--release-manifest-digest", f"sha256:{VALID_DIGEST}",
            "--executed-by", "ops@example.com",
            "--dry-run",
            "--output-report", str(report_path),
        ])
        assert rc == 0
        # 报告文件已写入
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["drill_passed"] is True

    def test_verify_valid_report(self, capsys, tmp_path):
        """verify 子命令:验证有效的 drill 报告。"""
        report = {
            "drill_passed": True,
            "dry_run": False,
            "stages": {s: {"passed": True} for s in DRILL_STAGES},
            "failures": [],
        }
        path = tmp_path / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        rc = main(["verify", "--report", str(path)])
        assert rc == 0


# ════════════════════════════════════════════════════════════════
# J. DRILL_STAGES 常量
# ════════════════════════════════════════════════════════════════

class TestDrillStagesConstant:
    """DRILL_STAGES 常量结构验证。"""

    def test_six_stages(self):
        assert len(DRILL_STAGES) == 6

    def test_stage_order(self):
        """6 个阶段按 R67 审计要求顺序排列。"""
        assert DRILL_STAGES == (
            "verify_signed_annotated_tag",
            "verify_tag_workflow_triggered",
            "verify_environment_approval",
            "verify_production_evidence_complete",
            "verify_digest_pinned_deploy",
            "verify_rollback_capability",
        )

    def test_make_result_structure(self):
        """_make_result 返回结构完整。"""
        r = _make_result("test_stage", True, "test reason", key="value")
        assert r["stage"] == "test_stage"
        assert r["passed"] is True
        assert r["reason"] == "test reason"
        assert r["details"]["key"] == "value"
        assert "verified_at" in r
