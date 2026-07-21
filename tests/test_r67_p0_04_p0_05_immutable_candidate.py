"""R67 P0-04 / P0-05: 不可变候选 + 代码门禁 vs 生产证据门禁分离 测试。

R67 审计背景:
    P0-04: Release Gates 最终为 success,但依赖 attempt 4,不能视为稳定发布证据。
           同一 image digest 必须连续 3 次通过完整验证链,且 3 次都首次成功。
    P0-05: 普通 push 的结果容易被描述为 production passed;没有真实 ru_72h_data
           时,RU job 仍显示 success。

R67 整改:
    P0-04:
      1. Build Once — docker-build 一次性构建并输出不可变 image digest
      2. Verify Many — verify-only-3x job 对同一 digest 连续运行 3 次完整验证链
      3. Promote Once — 3 次都首次成功后才允许晋级
      4. GHCR 重试策略:瞬态错误重试,非瞬态错误立即失败
      5. Registry 传播 SLI:首次可拉取时间/尝试次数/错误类型/总等待时间
    P0-05:
      1. 代码门禁(verify-only-3x): 同一 digest 3 次验证不可变性
      2. 生产证据门禁(production-evidence): 真实环境证据
         (SOAK/RESTORE/CHAOS/RU/SUPPLY/RC_VERIFY_3X)
      3. 6 类必需 artifact(新增 RC_VERIFY_3X)
      4. promotion-promotion-gate 依赖 verify-only-3x(代码门禁)

测试覆盖:
    A. verify_rc_3x.py 脚本单元测试
        - 错误分类(瞬态 vs 非瞬态)
        - 重试策略(瞬态重试,非瞬态立即失败)
        - 验证链(12 项验证)
    B. generate_production_evidence.py RC_VERIFY_3X 集成
        - EVIDENCE_TYPES 包含 rc_verify_3x
        - REQUIRED_ARTIFACT_TYPES 包含 RC_VERIFY_3X
        - verify_production_promotion 校验 6 类 artifact
    C. release-gates.yml 工作流结构
        - verify-only-3x job 存在
        - production-promotion-gate 依赖 verify-only-3x
        - release-summary 包含 verify-only-3x
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# A. verify_rc_3x.py 错误分类测试
# ════════════════════════════════════════════════════════════════

class TestErrorClassification:
    """R67 P0-04: GHCR 错误分类(瞬态 vs 非瞬态)。"""

    def test_transient_errors_detected(self):
        """瞬态错误被正确识别(允许重试)。"""
        from scripts.verify_rc_3x import _is_transient_error
        transient_msgs = [
            "manifest unknown",
            "Error: 404 Not Found",
            "429 Too Many Requests",
            "500 Internal Server Error",
            "502 Bad Gateway",
            "503 Service Unavailable",
            "timeout after 30s",
            "connection reset by peer",
        ]
        for msg in transient_msgs:
            assert _is_transient_error(msg), f"应识别为瞬态错误: {msg}"

    def test_fatal_errors_not_transient(self):
        """非瞬态错误不被识别为瞬态(立即失败)。"""
        from scripts.verify_rc_3x import _is_transient_error
        fatal_msgs = [
            "401 Unauthorized",
            "403 Forbidden",
            "permission denied",
            "TLS certificate verification failed",
            "x509 certificate expired",
            "digest mismatch",
            "signature mismatch",
            "signature verification failed",
            "malformed manifest",
        ]
        for msg in fatal_msgs:
            assert not _is_transient_error(msg), f"不应识别为瞬态错误: {msg}"

    def test_fatal_errors_detected(self):
        """非瞬态错误被正确识别(立即失败)。"""
        from scripts.verify_rc_3x import _is_fatal_error
        fatal_msgs = [
            "401 Unauthorized",
            "403 Forbidden",
            "permission denied",
            "access denied",
            "TLS certificate verification failed",
            "x509 certificate expired",
            "digest mismatch",
            "signature mismatch",
            "malformed manifest",
        ]
        for msg in fatal_msgs:
            assert _is_fatal_error(msg), f"应识别为非瞬态错误: {msg}"

    def test_unknown_errors_not_transient(self):
        """未知错误不被识别为瞬态(fail-closed)。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert not _is_transient_error("some weird unknown error")


# ════════════════════════════════════════════════════════════════
# B. verify_rc_3x.py 重试策略测试
# ════════════════════════════════════════════════════════════════

class TestRetryStrategy:
    """R67 P0-04: 重试策略(瞬态重试,非瞬态立即失败)。"""

    def test_fatal_error_no_retry(self):
        """非瞬态错误立即失败,不重试。"""
        from scripts.verify_rc_3x import _pull_with_retry
        # 模拟 docker pull 总是返回 401 错误
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            mock_run.return_value = (1, "", "401 Unauthorized")
            result = _pull_with_retry("ghcr.io/test@sha256:abc", max_attempts=5)
        assert not result["success"]
        assert result["attempts"] == 1  # 立即失败,不重试
        assert result["fatal_error"] is not None
        assert "fatal" in result["fatal_error"].lower()

    def test_transient_error_retried(self):
        """瞬态错误会重试,直到成功。"""
        from scripts.verify_rc_3x import _pull_with_retry
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            # 前 2 次失败,第 3 次成功
            mock_run.side_effect = [
                (1, "", "manifest unknown"),
                (1, "", "manifest unknown"),
                (0, "Pull complete", ""),
            ]
            with patch("scripts.verify_rc_3x.time.sleep"):  # 加速测试
                result = _pull_with_retry("ghcr.io/test@sha256:abc", max_attempts=5)
        assert result["success"]
        assert result["attempts"] == 3
        assert result["first_success_time"] is not None

    def test_max_attempts_exceeded(self):
        """瞬态错误重试次数耗尽后失败。"""
        from scripts.verify_rc_3x import _pull_with_retry
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            mock_run.return_value = (1, "", "manifest unknown")
            with patch("scripts.verify_rc_3x.time.sleep"):  # 加速测试
                result = _pull_with_retry(
                    "ghcr.io/test@sha256:abc", max_attempts=3, total_budget=3600,
                )
        assert not result["success"]
        assert result["attempts"] == 3
        assert result["fatal_error"] == "max_attempts_exceeded"


# ════════════════════════════════════════════════════════════════
# C. generate_production_evidence.py RC_VERIFY_3X 集成测试
# ════════════════════════════════════════════════════════════════

class TestRCVerify3xEvidenceType:
    """R67 P0-04/P0-05: RC_VERIFY_3X 作为新的证据类型。"""

    def test_evidence_types_includes_rc_verify_3x(self):
        """EVIDENCE_TYPES 包含 rc_verify_3x。"""
        from scripts.generate_production_evidence import EVIDENCE_TYPES
        assert "rc_verify_3x" in EVIDENCE_TYPES
        assert EVIDENCE_TYPES["rc_verify_3x"]["script"] == "scripts/verify_rc_3x.py"

    def test_required_artifact_types_includes_rc_verify_3x(self):
        """REQUIRED_ARTIFACT_TYPES 包含 RC_VERIFY_3X。"""
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES
        assert "RC_VERIFY_3X" in REQUIRED_ARTIFACT_TYPES

    def test_evidence_type_to_artifact_type_mapping(self):
        """EVIDENCE_TYPE_TO_ARTIFACT_TYPE 包含 rc_verify_3x → RC_VERIFY_3X 映射。"""
        from scripts.generate_production_evidence import EVIDENCE_TYPE_TO_ARTIFACT_TYPE
        assert EVIDENCE_TYPE_TO_ARTIFACT_TYPE["rc_verify_3x"] == "RC_VERIFY_3X"

    def test_required_artifact_types_count_is_6(self):
        """R67 P0-05: 6 类必需 artifact(原 5 类 + RC_VERIFY_3X)。"""
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES
        assert len(REQUIRED_ARTIFACT_TYPES) == 6


# ════════════════════════════════════════════════════════════════
# D. verify_production_promotion 校验 6 类 artifact
# ════════════════════════════════════════════════════════════════

class TestVerifyProductionPromotionWithRCVerify3X:
    """R67 P0-05: verify_production_promotion 必须校验 6 类 artifact。"""

    def _make_valid_evidence(self) -> dict:
        """构造一个完整的有效 evidence 文件(6 类 artifact 齐全)。

        R67 P1-11: 每条 artifact 必须包含 nonce/attestation_digest/time_window/consumed
        字段(防重放),否则 verify_production_promotion 拒绝晋级。
        """
        from scripts.generate_production_evidence import REQUIRED_ARTIFACT_TYPES
        return {
            "evidence_mode": "production",
            "signature": {"method": "gpg", "verified": True},
            "flags": {},
            "artifacts": [
                {
                    "artifact_type": atype,
                    "environment_id": "production-vps-01",
                    "commit_sha": "abc123def456",
                    "image_digest": "sha256:abc",
                    "started_at": "2026-07-20T00:00:00+00:00",
                    "ended_at": "2026-07-20T01:00:00+00:00",
                    "expires_at": "2099-12-31T23:59:59+00:00",
                    "raw_data_digest": "sha256:raw",
                    "executed_by": "ops@example.com",
                    "approved_by": "manager@example.com",
                    "signature": "gpg-signature-base64",
                    # R67 P1-11: 防重放字段
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

    def test_valid_evidence_with_rc_verify_3x_passes(self, tmp_path):
        """完整 6 类 artifact(含 RC_VERIFY_3X)的 evidence 通过校验。"""
        from scripts.generate_production_evidence import verify_production_promotion
        evidence = self._make_valid_evidence()
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence))
        result = verify_production_promotion(path)
        assert result.get("production_promotion_allowed") is True

    def test_missing_rc_verify_3x_fails(self, tmp_path):
        """缺少 RC_VERIFY_3X artifact 时校验失败。"""
        from scripts.generate_production_evidence import verify_production_promotion
        from services.error_codes import AppError
        evidence = self._make_valid_evidence()
        # 删除 RC_VERIFY_3X artifact
        evidence["artifacts"] = [
            a for a in evidence["artifacts"]
            if a["artifact_type"] != "RC_VERIFY_3X"
        ]
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence))
        with pytest.raises(AppError) as exc_info:
            verify_production_promotion(path)
        # 错误信息中应包含 RC_VERIFY_3X
        assert "RC_VERIFY_3X" in str(exc_info.value) or \
               "RC_VERIFY_3X" in exc_info.value.params.get("missing", "")


# ════════════════════════════════════════════════════════════════
# E. release-gates.yml 工作流结构测试
# ════════════════════════════════════════════════════════════════

class TestWorkflowStructure:
    """R67 P0-04/P0-05: release-gates.yml 工作流结构验证。"""

    @pytest.fixture(scope="class")
    def workflow(self):
        import yaml
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        with open(path) as f:
            return yaml.safe_load(f)

    def test_verify_only_3x_job_exists(self, workflow):
        """verify-only-3x job 存在。"""
        assert "verify-only-3x" in workflow["jobs"]

    def test_verify_only_3x_needs_docker_build(self, workflow):
        """verify-only-3x 依赖 docker-build(build once)。"""
        needs = workflow["jobs"]["verify-only-3x"]["needs"]
        assert "docker-build" in needs
        assert "sign-image" in needs
        assert "publish-attestation" in needs

    def test_verify_only_3x_only_on_push(self, workflow):
        """verify-only-3x 仅在 push 事件运行(PR 不运行)。"""
        if_cond = workflow["jobs"]["verify-only-3x"]["if"]
        assert "push" in if_cond

    def test_production_promotion_gate_needs_verify_only_3x(self, workflow):
        """R67 P0-05: production-promotion-gate 依赖 verify-only-3x(代码门禁)。"""
        needs = workflow["jobs"]["production-promotion-gate"]["needs"]
        assert "verify-only-3x" in needs

    def test_release_summary_needs_verify_only_3x(self, workflow):
        """release-summary 包含 verify-only-3x。"""
        needs = workflow["jobs"]["release-summary"]["needs"]
        assert "verify-only-3x" in needs

    def test_release_summary_env_includes_verify_only_3x(self, workflow):
        """release-summary env 包含 VERIFY_ONLY_3X 变量。"""
        env = workflow["jobs"]["release-summary"]["env"]
        # env 变量名是 VERIFY_ONLY_3X(大写,用下划线)
        assert "VERIFY_ONLY_3X" in env

    def test_docker_build_has_classified_retry(self, workflow):
        """R67 P0-04: docker-build 的 pull 步骤包含分类重试策略。"""
        steps = workflow["jobs"]["docker-build"]["steps"]
        # 找到 "Verify image pull by digest" 步骤
        pull_step = None
        for step in steps:
            if step.get("name") == "Verify image pull by digest":
                pull_step = step
                break
        assert pull_step is not None, "必须有 'Verify image pull by digest' 步骤"
        run_script = pull_step["run"]
        # 必须包含分类错误检查
        assert "401" in run_script or "unauthorized" in run_script.lower()
        assert "TLS" in run_script or "certificate" in run_script.lower()
        assert "digest mismatch" in run_script.lower()
