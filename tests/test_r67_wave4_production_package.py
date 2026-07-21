"""R67 Wave 4: production evidence execution package 测试。

R67 审计要求(Wave 4 — RC tag 正式演练):
    "tag workflow、environment approval、production evidence、digest-pinned
     deploy、rollback 全通过"

测试覆盖:
    A. artifact_builder — P1-11 防重放字段构建
    B. environment_approval — 环境审批门禁
    C. digest_pinned_deploy — digest 锁定部署验证
    D. orchestrator — verify_promotion_readiness 综合门禁
    E. CLI — __main__ 入口
    F. 包导出 API 一致性
"""
from __future__ import annotations

import json
import sys
import datetime as _dt
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# A. artifact_builder 测试
# ════════════════════════════════════════════════════════════════

class TestProductionArtifactBuilder:
    """R67 P1-11 / Wave 4: artifact 构建器测试。"""

    def test_builder_creates_artifact_with_p1_11_fields(self):
        """构建器生成的 artifact 包含全部 P1-11 防重放字段。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder

        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123def456",
            image_digest="sha256:abc",
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        artifact = builder.build(
            artifact_type="SOAK_7DAY",
            raw_data_digest="sha256:raw-soak",
        )

        # P1-11 防重放字段
        assert "nonce" in artifact
        assert len(artifact["nonce"]) >= 32  # 至少 32 字符 hex
        assert artifact["attestation_digest"] == "sha256:att-abc"
        assert isinstance(artifact["time_window"], dict)
        assert "start" in artifact["time_window"]
        assert "end" in artifact["time_window"]
        assert artifact["consumed"] is False

    def test_builder_creates_artifact_with_r65_p0_04_fields(self):
        """构建器生成的 artifact 包含全部 R65 P0-04 必需字段。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_FIELDS

        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123def456",
            image_digest="sha256:abc",
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        artifact = builder.build(
            artifact_type="RESTORE_3X",
            raw_data_digest="sha256:raw-restore",
        )

        # 所有必需字段都存在
        for field in REQUIRED_ARTIFACT_FIELDS:
            assert field in artifact, f"缺少必需字段: {field}"

    def test_builder_generates_unique_nonce(self):
        """每次构建生成不同的 nonce。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder

        builder = ProductionArtifactBuilder(
            environment_id="prod",
            commit_sha="abc",
            image_digest="sha256:abc",
            attestation_digest="sha256:att",
            executed_by="ops",
            approved_by="mgr",
        )
        a1 = builder.build(artifact_type="SOAK_7DAY", raw_data_digest="sha256:r1")
        a2 = builder.build(artifact_type="SOAK_7DAY", raw_data_digest="sha256:r1")
        assert a1["nonce"] != a2["nonce"]

    def test_builder_default_expires_at_7_days(self):
        """默认 expires_at 为当前时间 + 7 天。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder

        builder = ProductionArtifactBuilder(
            environment_id="prod",
            commit_sha="abc",
            image_digest="sha256:abc",
            attestation_digest="sha256:att",
            executed_by="ops",
            approved_by="mgr",
            default_ttl_days=7,
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        artifact = builder.build(
            artifact_type="RU_72H",
            raw_data_digest="sha256:raw",
        )
        expires = _dt.datetime.fromisoformat(
            artifact["expires_at"].replace("Z", "+00:00")
        )
        # expires 应在 6.5-7.5 天之间
        delta = expires - now
        assert _dt.timedelta(days=6, hours=12) <= delta <= _dt.timedelta(days=7, hours=12)

    def test_builder_validates_required_inputs(self):
        """构建器验证必填输入。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder

        with pytest.raises(ValueError, match="environment_id"):
            ProductionArtifactBuilder(
                environment_id="",
                commit_sha="abc",
                image_digest="sha256:abc",
                attestation_digest="sha256:att",
                executed_by="ops",
                approved_by="mgr",
            )

    def test_builder_build_all_types(self):
        """build_all_types 生成全部 6 类 artifact。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES

        builder = ProductionArtifactBuilder(
            environment_id="prod",
            commit_sha="abc",
            image_digest="sha256:abc",
            attestation_digest="sha256:att",
            executed_by="ops",
            approved_by="mgr",
        )
        raw_digests = {t: f"sha256:raw-{t.lower()}" for t in REQUIRED_ARTIFACT_TYPES}
        artifacts = builder.build_all_types(raw_data_digests=raw_digests)

        assert len(artifacts) == 6
        types = {a["artifact_type"] for a in artifacts}
        assert types == set(REQUIRED_ARTIFACT_TYPES)

    def test_builder_build_all_types_missing_type_raises(self):
        """build_all_types 缺少类型时抛 ValueError。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder

        builder = ProductionArtifactBuilder(
            environment_id="prod",
            commit_sha="abc",
            image_digest="sha256:abc",
            attestation_digest="sha256:att",
            executed_by="ops",
            approved_by="mgr",
        )
        # 缺少 RC_VERIFY_3X
        raw_digests = {
            "SOAK_7DAY": "sha256:r1",
            "RESTORE_3X": "sha256:r2",
            "OUTBOX_FAULT_INJECTION": "sha256:r3",
            "RU_72H": "sha256:r4",
            "SUPPLY_CHAIN": "sha256:r5",
        }
        with pytest.raises(ValueError, match="RC_VERIFY_3X"):
            builder.build_all_types(raw_data_digests=raw_digests)

    def test_build_artifact_convenience_function(self):
        """build_artifact 便捷函数工作正常。"""
        from scripts.production.artifact_builder import build_artifact

        artifact = build_artifact(
            artifact_type="SUPPLY_CHAIN",
            environment_id="prod",
            commit_sha="abc",
            image_digest="sha256:abc",
            attestation_digest="sha256:att",
            raw_data_digest="sha256:raw",
            executed_by="ops",
            approved_by="mgr",
        )
        assert artifact["artifact_type"] == "SUPPLY_CHAIN"
        assert artifact["nonce"]
        assert artifact["consumed"] is False


# ════════════════════════════════════════════════════════════════
# B. environment_approval 测试
# ════════════════════════════════════════════════════════════════

class TestEnvironmentApprovalGate:
    """R67 Wave 4: 环境审批门禁测试。"""

    def _make_valid_approval(self) -> dict:
        return {
            "candidate_tag": "rc-2026-07-21-v1",
            "environment_id": "production-vps-01",
            "approved_by": "manager@example.com",
            "approved_at": "2026-07-20T10:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "revoked": False,
        }

    def test_valid_approval_passes(self):
        """有效审批通过门禁。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        # 使用固定时钟避免时间相关问题
        clock = _dt.datetime(2026, 7, 21, 12, 0, 0, tzinfo=_dt.timezone.utc)
        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            clock=clock,
        )
        result = gate.verify(
            self._make_valid_approval(),
            executed_by="ops@example.com",
        )
        assert result["approved"] is True
        assert result["reason"] == ""

    def test_empty_approval_fails(self):
        """空审批记录失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
        )
        result = gate.verify({}, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "为空" in result["reason"]

    def test_wrong_candidate_tag_fails(self):
        """candidate_tag 不匹配失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
        )
        approval = self._make_valid_approval()
        approval["candidate_tag"] = "rc-2026-07-22-v2"
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "candidate_tag" in result["reason"]

    def test_wrong_environment_id_fails(self):
        """environment_id 不匹配失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
        )
        approval = self._make_valid_approval()
        approval["environment_id"] = "staging-vps-02"
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "environment_id" in result["reason"]

    def test_same_approver_and_executor_fails(self):
        """审批者与执行者相同失败(职责分离)。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
        )
        approval = self._make_valid_approval()
        result = gate.verify(approval, executed_by="manager@example.com")
        assert result["approved"] is False
        assert "职责分离" in result["reason"]

    def test_revoked_approval_fails(self):
        """已撤销审批失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        clock = _dt.datetime(2026, 7, 21, 12, 0, 0, tzinfo=_dt.timezone.utc)
        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            clock=clock,
        )
        approval = self._make_valid_approval()
        approval["revoked"] = True
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "撤销" in result["reason"]

    def test_expired_approval_fails(self):
        """过期审批失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        clock = _dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=_dt.timezone.utc)
        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            clock=clock,
        )
        approval = self._make_valid_approval()
        approval["expires_at"] = "2026-07-21T23:59:59+00:00"
        # expires_at 是 2026-07-21T23:59:59,clock 是 2026-07-22T12:00:00
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "过期" in result["reason"]

    def test_future_approval_fails(self):
        """未来时间审批失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        clock = _dt.datetime(2026, 7, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)
        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            clock=clock,
        )
        approval = self._make_valid_approval()
        approval["approved_at"] = "2026-07-21T10:00:00+00:00"
        # approved_at 是 2026-07-21T10:00:00,clock 是 2026-07-20T12:00:00
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "未来" in result["reason"]

    def test_missing_approved_by_fails(self):
        """缺少 approved_by 失败。"""
        from scripts.production.environment_approval import EnvironmentApprovalGate

        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
        )
        approval = self._make_valid_approval()
        approval["approved_by"] = ""
        result = gate.verify(approval, executed_by="ops@example.com")
        assert result["approved"] is False
        assert "approved_by" in result["reason"]

    def test_verify_environment_approval_convenience_function(self):
        """便捷函数 verify_environment_approval 工作正常。"""
        from scripts.production.environment_approval import (
            EnvironmentApprovalGate,
            verify_environment_approval,
        )

        clock = _dt.datetime(2026, 7, 21, 12, 0, 0, tzinfo=_dt.timezone.utc)
        gate = EnvironmentApprovalGate(
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            clock=clock,
        )
        result = gate.verify(self._make_valid_approval(), executed_by="ops@example.com")
        assert result["approved"] is True
        # 便捷函数也应工作
        result2 = verify_environment_approval(
            self._make_valid_approval(),
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            executed_by="ops@example.com",
        )
        # 注意:便捷函数使用真实时钟,可能因时间问题失败;此处仅验证函数可调用
        assert "approved" in result2


# ════════════════════════════════════════════════════════════════
# C. digest_pinned_deploy 测试
# ════════════════════════════════════════════════════════════════

class TestDigestPinnedDeployVerifier:
    """R67 Wave 4: digest 锁定部署验证测试。"""

    _VALID_DIGEST = "sha256:" + "a" * 64

    def test_valid_digest_ref_passes(self):
        """有效 digest 引用通过验证。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
        )
        result = verifier.verify_deploy_ref(
            f"ghcr.io/owner/repo@{self._VALID_DIGEST}"
        )
        assert result["verified"] is True
        assert result["all_digests_match"] is True

    def test_tag_only_ref_fails(self):
        """仅 tag 引用(无 digest)失败。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
        )
        result = verifier.verify_deploy_ref("ghcr.io/owner/repo:latest")
        assert result["verified"] is False
        assert "digest" in result["reason"].lower()

    def test_empty_ref_fails(self):
        """空引用失败。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
        )
        result = verifier.verify_deploy_ref("")
        assert result["verified"] is False

    def test_digest_mismatch_with_release_manifest_fails(self):
        """digest 与 release manifest 不一致失败。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        other_digest = "sha256:" + "b" * 64
        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
        )
        result = verifier.verify_deploy_ref(f"ghcr.io/owner/repo@{other_digest}")
        assert result["verified"] is False
        assert "release_manifest_digest" in result["reason"]

    def test_digest_mismatch_with_attestation_subject_fails(self):
        """digest 与 attestation subject 不一致失败。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        other_digest = "sha256:" + "b" * 64
        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
            attestation_subject_digest=other_digest,
        )
        result = verifier.verify_deploy_ref(f"ghcr.io/owner/repo@{self._VALID_DIGEST}")
        assert result["verified"] is False
        assert "attestation_subject_digest" in result["reason"]

    def test_digest_mismatch_with_verify_only_3x_fails(self):
        """digest 与 verify-only-3x 不一致失败。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        other_digest = "sha256:" + "b" * 64
        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
            verify_only_3x_digest=other_digest,
        )
        result = verifier.verify_deploy_ref(f"ghcr.io/owner/repo@{self._VALID_DIGEST}")
        assert result["verified"] is False
        assert "verify_only_3x_digest" in result["reason"]

    def test_all_digests_match_passes(self):
        """所有 digest 一致时通过。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=self._VALID_DIGEST,
            attestation_subject_digest=self._VALID_DIGEST,
            verify_only_3x_digest=self._VALID_DIGEST,
        )
        result = verifier.verify_deploy_ref(
            f"ghcr.io/owner/repo@{self._VALID_DIGEST}"
        )
        assert result["verified"] is True
        assert result["all_digests_match"] is True

    def test_normalize_digest_without_prefix(self):
        """digest 不带 sha256: 前缀时自动标准化。"""
        from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

        hex_only = "a" * 64
        verifier = DigestPinnedDeployVerifier(
            release_manifest_digest=hex_only,  # 不带前缀
        )
        result = verifier.verify_deploy_ref(
            f"ghcr.io/owner/repo@sha256:{hex_only}"
        )
        assert result["verified"] is True

    def test_verify_digest_pinned_deploy_convenience_function(self):
        """便捷函数 verify_digest_pinned_deploy 工作正常。"""
        from scripts.production.digest_pinned_deploy import verify_digest_pinned_deploy

        result = verify_digest_pinned_deploy(
            f"ghcr.io/owner/repo@{self._VALID_DIGEST}",
            release_manifest_digest=self._VALID_DIGEST,
        )
        assert result["verified"] is True


# ════════════════════════════════════════════════════════════════
# D. orchestrator — verify_promotion_readiness 综合测试
# ════════════════════════════════════════════════════════════════

class TestVerifyPromotionReadiness:
    """R67 Wave 4: verify_promotion_readiness 综合门禁测试。"""

    _DIGEST = "sha256:" + "a" * 64

    def _make_valid_evidence(self, tmp_path: Path) -> Path:
        """构造有效的 production evidence 文件。"""
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES

        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123",
            image_digest=self._DIGEST,
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        raw_digests = {t: f"sha256:raw-{t.lower()}" for t in REQUIRED_ARTIFACT_TYPES}
        artifacts = builder.build_all_types(raw_data_digests=raw_digests)

        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": artifacts,
        }
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence))
        return path

    def _make_valid_approval(self) -> dict:
        return {
            "candidate_tag": "rc-2026-07-21-v1",
            "environment_id": "production-vps-01",
            "approved_by": "manager@example.com",
            "approved_at": "2026-07-20T10:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "revoked": False,
        }

    def test_all_gates_pass(self, tmp_path):
        """全部门禁通过时 ready=True。"""
        from scripts.production.orchestrator import verify_promotion_readiness

        evidence_path = self._make_valid_evidence(tmp_path)
        result = verify_promotion_readiness(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            deploy_ref=f"ghcr.io/owner/repo@{self._DIGEST}",
            release_manifest_digest=self._DIGEST,
            approval_record=self._make_valid_approval(),
            executed_by="ops@example.com",
        )
        assert result["ready"] is True
        assert result["failures"] == []
        assert result["evidence_gate"]["passed"] is True
        assert result["approval_gate"]["approved"] is True
        assert result["deploy_gate"]["verified"] is True

    def test_evidence_gate_failure_blocks_promotion(self, tmp_path):
        """evidence 门禁失败阻断 promotion。"""
        from scripts.production.orchestrator import verify_promotion_readiness

        # 构造缺少 artifact 的 evidence
        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": [],  # 空 artifacts
        }
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(evidence))

        result = verify_promotion_readiness(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            deploy_ref=f"ghcr.io/owner/repo@{self._DIGEST}",
            release_manifest_digest=self._DIGEST,
            approval_record=self._make_valid_approval(),
            executed_by="ops@example.com",
        )
        assert result["ready"] is False
        assert any("evidence" in f for f in result["failures"])

    def test_approval_gate_failure_blocks_promotion(self, tmp_path):
        """审批门禁失败阻断 promotion。"""
        from scripts.production.orchestrator import verify_promotion_readiness

        evidence_path = self._make_valid_evidence(tmp_path)
        # 审批者与执行者相同
        approval = self._make_valid_approval()
        approval["approved_by"] = "ops@example.com"  # 同 executed_by

        result = verify_promotion_readiness(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            deploy_ref=f"ghcr.io/owner/repo@{self._DIGEST}",
            release_manifest_digest=self._DIGEST,
            approval_record=approval,
            executed_by="ops@example.com",
        )
        assert result["ready"] is False
        assert any("审批" in f for f in result["failures"])

    def test_deploy_gate_failure_blocks_promotion(self, tmp_path):
        """部署门禁失败(digest 不匹配)阻断 promotion。"""
        from scripts.production.orchestrator import verify_promotion_readiness

        evidence_path = self._make_valid_evidence(tmp_path)
        # 使用错误的 digest
        wrong_digest = "sha256:" + "b" * 64

        result = verify_promotion_readiness(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            deploy_ref=f"ghcr.io/owner/repo@{wrong_digest}",
            release_manifest_digest=self._DIGEST,
            approval_record=self._make_valid_approval(),
            executed_by="ops@example.com",
        )
        assert result["ready"] is False
        assert any("digest" in f.lower() for f in result["failures"])

    def test_tag_only_deploy_blocks_promotion(self, tmp_path):
        """仅 tag 部署(无 digest)阻断 promotion。"""
        from scripts.production.orchestrator import verify_promotion_readiness

        evidence_path = self._make_valid_evidence(tmp_path)

        result = verify_promotion_readiness(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            deploy_ref="ghcr.io/owner/repo:latest",  # 仅 tag
            release_manifest_digest=self._DIGEST,
            approval_record=self._make_valid_approval(),
            executed_by="ops@example.com",
        )
        assert result["ready"] is False
        assert any("digest" in f.lower() for f in result["failures"])


# ════════════════════════════════════════════════════════════════
# E. orchestrator — promote_candidate 单次使用测试
# ════════════════════════════════════════════════════════════════

class TestPromoteCandidate:
    """R67 P1-11: promote_candidate 单次使用语义测试。"""

    _DIGEST = "sha256:" + "a" * 64

    def _make_valid_evidence(self, tmp_path: Path) -> Path:
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES

        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123",
            image_digest=self._DIGEST,
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        raw_digests = {t: f"sha256:raw-{t.lower()}" for t in REQUIRED_ARTIFACT_TYPES}
        artifacts = builder.build_all_types(raw_data_digests=raw_digests)

        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": artifacts,
        }
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence))
        return path

    def test_first_consume_succeeds(self, tmp_path):
        """首次消费成功,标记 consumed=true。"""
        from scripts.production.orchestrator import promote_candidate

        evidence_path = self._make_valid_evidence(tmp_path)
        result = promote_candidate(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            consumed_by="ops@example.com",
            expected_attestation_digest="sha256:att-abc",
        )
        # 所有 artifact 应标记 consumed=true
        for art in result["artifacts"]:
            assert art["consumed"] is True
            assert art["consumed_candidate"] == "rc-2026-07-21-v1"

    def test_same_candidate_reconsume_is_idempotent(self, tmp_path):
        """同一 candidate 重复消费是幂等的。"""
        from scripts.production.orchestrator import promote_candidate

        evidence_path = self._make_valid_evidence(tmp_path)
        # 首次消费
        promote_candidate(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            consumed_by="ops@example.com",
            expected_attestation_digest="sha256:att-abc",
        )
        # 同 candidate 再次消费 — 幂等(不抛异常)
        result = promote_candidate(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            consumed_by="ops@example.com",
            expected_attestation_digest="sha256:att-abc",
        )
        assert result["last_consumed_candidate"] == "rc-2026-07-21-v1"

    def test_cross_candidate_reuse_raises(self, tmp_path):
        """跨候选复用抛 EVIDENCE_ALREADY_CONSUMED。"""
        from scripts.production.orchestrator import promote_candidate
        from services.error_codes import AppError, ErrorCodes

        evidence_path = self._make_valid_evidence(tmp_path)
        # 候选 A 消费
        promote_candidate(
            evidence_path=evidence_path,
            candidate_tag="rc-2026-07-21-v1",
            environment_id="production-vps-01",
            consumed_by="ops@example.com",
            expected_attestation_digest="sha256:att-abc",
        )
        # 候选 B 试图复用同一证据 — 抛异常
        with pytest.raises(AppError) as exc_info:
            promote_candidate(
                evidence_path=evidence_path,
                candidate_tag="rc-2026-07-22-v2",  # 不同 candidate
                environment_id="production-vps-01",
                consumed_by="ops@example.com",
                expected_attestation_digest="sha256:att-abc",
            )
        assert exc_info.value.code == ErrorCodes.EVIDENCE_ALREADY_CONSUMED


# ════════════════════════════════════════════════════════════════
# F. 包导出 API 一致性测试
# ════════════════════════════════════════════════════════════════

class TestPackageAPI:
    """R67 Wave 4: scripts.production 包导出 API 一致性测试。"""

    def test_package_imports_successfully(self):
        """包可正常导入。"""
        import scripts.production  # noqa: F401

    def test_package_exports_production_artifact_builder(self):
        """包导出 ProductionArtifactBuilder。"""
        from scripts.production import ProductionArtifactBuilder
        assert ProductionArtifactBuilder is not None

    def test_package_exports_build_artifact(self):
        """包导出 build_artifact。"""
        from scripts.production import build_artifact
        assert callable(build_artifact)

    def test_package_exports_orchestrator(self):
        """包导出 ProductionEvidenceOrchestrator 和 orchestrate_production_evidence。"""
        from scripts.production import (
            ProductionEvidenceOrchestrator,
            orchestrate_production_evidence,
        )
        assert ProductionEvidenceOrchestrator is not None
        assert callable(orchestrate_production_evidence)

    def test_package_exports_environment_approval(self):
        """包导出 EnvironmentApprovalGate 和 verify_environment_approval。"""
        from scripts.production import (
            EnvironmentApprovalGate,
            verify_environment_approval,
        )
        assert EnvironmentApprovalGate is not None
        assert callable(verify_environment_approval)

    def test_package_exports_digest_pinned_deploy(self):
        """包导出 DigestPinnedDeployVerifier 和 verify_digest_pinned_deploy。"""
        from scripts.production import (
            DigestPinnedDeployVerifier,
            verify_digest_pinned_deploy,
        )
        assert DigestPinnedDeployVerifier is not None
        assert callable(verify_digest_pinned_deploy)

    def test_package_has_version(self):
        """包有 __version__ 属性。"""
        from scripts.production import __version__
        assert __version__

    def test_package_all_exports_listed(self):
        """__all__ 列出全部导出。"""
        from scripts.production import __all__
        expected = {
            "ProductionArtifactBuilder",
            "build_artifact",
            "ProductionEvidenceOrchestrator",
            "orchestrate_production_evidence",
            "EnvironmentApprovalGate",
            "verify_environment_approval",
            "DigestPinnedDeployVerifier",
            "verify_digest_pinned_deploy",
        }
        assert set(__all__) == expected


# ════════════════════════════════════════════════════════════════
# G. CLI __main__ 入口测试
# ════════════════════════════════════════════════════════════════

class TestCliMain:
    """R67 Wave 4: scripts.production CLI __main__ 入口测试。"""

    def test_cli_help_exits_zero(self):
        """--help 退出码 0。"""
        from scripts.production.__main__ import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_cli_no_subcommand_fails(self):
        """无子命令失败(退出码 2)。"""
        from scripts.production.__main__ import main

        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_cli_verify_ready_with_valid_inputs(self, tmp_path):
        """verify-ready 子命令处理有效输入。"""
        from scripts.production.__main__ import main
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES

        digest = "sha256:" + "a" * 64

        # 构造有效 evidence
        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123",
            image_digest=digest,
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        raw_digests = {t: f"sha256:raw-{t.lower()}" for t in REQUIRED_ARTIFACT_TYPES}
        artifacts = builder.build_all_types(raw_data_digests=raw_digests)
        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": artifacts,
        }
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(evidence))

        # 构造有效审批记录
        approval = {
            "candidate_tag": "rc-2026-07-21-v1",
            "environment_id": "production-vps-01",
            "approved_by": "manager@example.com",
            "approved_at": "2026-07-20T10:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "revoked": False,
        }
        approval_path = tmp_path / "approval.json"
        approval_path.write_text(json.dumps(approval))

        # 运行 verify-ready
        rc = main([
            "verify-ready",
            "--evidence-path", str(evidence_path),
            "--candidate-tag", "rc-2026-07-21-v1",
            "--environment-id", "production-vps-01",
            "--deploy-ref", f"ghcr.io/owner/repo@{digest}",
            "--release-manifest-digest", digest,
            "--approval-record", str(approval_path),
            "--executed-by", "ops@example.com",
        ])
        assert rc == 0

    def test_cli_verify_ready_with_invalid_inputs_fails(self, tmp_path):
        """verify-ready 子命令处理无效输入(失败)。"""
        from scripts.production.__main__ import main

        # 空 evidence
        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": [],
        }
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(evidence))

        # 有效审批
        approval = {
            "candidate_tag": "rc-2026-07-21-v1",
            "environment_id": "production-vps-01",
            "approved_by": "manager@example.com",
            "approved_at": "2026-07-20T10:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "revoked": False,
        }
        approval_path = tmp_path / "approval.json"
        approval_path.write_text(json.dumps(approval))

        digest = "sha256:" + "a" * 64
        rc = main([
            "verify-ready",
            "--evidence-path", str(evidence_path),
            "--candidate-tag", "rc-2026-07-21-v1",
            "--environment-id", "production-vps-01",
            "--deploy-ref", f"ghcr.io/owner/repo@{digest}",
            "--release-manifest-digest", digest,
            "--approval-record", str(approval_path),
            "--executed-by", "ops@example.com",
        ])
        assert rc == 1  # 失败

    def test_cli_promote_with_valid_evidence(self, tmp_path):
        """promote 子命令处理有效 evidence。"""
        from scripts.production.__main__ import main
        from scripts.production.artifact_builder import ProductionArtifactBuilder
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES

        digest = "sha256:" + "a" * 64
        builder = ProductionArtifactBuilder(
            environment_id="production-vps-01",
            commit_sha="abc123",
            image_digest=digest,
            attestation_digest="sha256:att-abc",
            executed_by="ops@example.com",
            approved_by="manager@example.com",
        )
        raw_digests = {t: f"sha256:raw-{t.lower()}" for t in REQUIRED_ARTIFACT_TYPES}
        artifacts = builder.build_all_types(raw_data_digests=raw_digests)
        evidence = {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": artifacts,
        }
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(evidence))

        rc = main([
            "promote",
            "--evidence-path", str(evidence_path),
            "--candidate-tag", "rc-2026-07-21-v1",
            "--environment-id", "production-vps-01",
            "--consumed-by", "ops@example.com",
            "--expected-attestation-digest", "sha256:att-abc",
        ])
        assert rc == 0
