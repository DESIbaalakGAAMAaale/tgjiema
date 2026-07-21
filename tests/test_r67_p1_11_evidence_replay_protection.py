#!/usr/bin/env python3
"""R67 P1-11: RU/soak/restore artifact 防重放测试。

测试目标(对应 R67 P1-11 整改要求):
    每份证据加入 nonce、environment ID、commit/tree/image/attestation digest、
    时间窗、执行器和审批者;promotion 消费后标记 consumed,禁止跨候选复用。

覆盖:
    1. REQUIRED_ARTIFACT_FIELDS 含 4 个新防重放字段(nonce / attestation_digest
       / time_window / consumed)
    2. verify_production_promotion() 强制校验新字段存在(任一缺失即失败)
    3. consume_evidence_for_promotion() 单次使用语义:
       a. 首次消费成功,标记 consumed=true + consumed_at + consumed_by +
          consumed_candidate
       b. 同 candidate 重复消费幂等(返回当前 evidence)
       c. 跨 candidate 复用抛 AppError(EVIDENCE_ALREADY_CONSUMED)
       d. environment_id 不匹配抛 AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)
       e. attestation_digest 不匹配抛 AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)
       f. nonce 缺失抛 AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)
       g. 过期 artifact 抛 AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)
    4. ErrorCodes.EVIDENCE_ALREADY_CONSUMED 错误码存在且可用
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_evidence_module():
    """加载 generate_production_evidence 模块(避免包导入副作用)。"""
    spec = importlib.util.spec_from_file_location(
        "generate_production_evidence",
        REPO_ROOT / "scripts" / "generate_production_evidence.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _past_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _make_artifact(artifact_type: str, **overrides) -> dict:
    """构造合法 artifact(含全部 R67 P1-11 防重放字段)。"""
    art = {
        "artifact_type": artifact_type,
        "environment_id": "prod-env-test",
        "commit_sha": "abc123def456789012345678901234567890abcd",
        "image_digest": "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "started_at": _past_iso(days=2),
        "ended_at": _past_iso(days=1),
        "expires_at": _future_iso(days=7),
        "raw_data_digest": "sha256:rawdata0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "executed_by": "release-manager@example.com",
        "approved_by": "approver@example.com",
        "signature": "cosign-signature-base64-encoded-payload",
        # R67 P1-11: 防重放字段
        "nonce": f"nonce-{artifact_type.lower()}-001",
        "attestation_digest": "sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "time_window": {
            "started_at": _past_iso(days=2),
            "ended_at": _past_iso(days=1),
        },
        "consumed": False,
    }
    art.update(overrides)
    return art


def _make_evidence(artifacts: list[dict] | None = None) -> dict:
    """构造合法 production evidence。"""
    if artifacts is None:
        artifacts = [
            _make_artifact(t) for t in (
                "SOAK_7DAY", "RESTORE_3X", "OUTBOX_FAULT_INJECTION",
                "RU_72H", "SUPPLY_CHAIN", "RC_VERIFY_3X",
            )
        ]
    return {
        "schema_version": "r64_p1_12_v1",
        "evidence_mode": "production",
        "production_promotion_allowed": False,
        "generated_at": _past_iso(days=1),
        "flags": {"skip": [], "dry_run": False},
        "signature": {
            "method": "cosign",
            "verified": True,
            "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----",
            "sig": "base64-encoded-signature",
        },
        "artifacts": artifacts,
    }


def _write_evidence(tmp_path: Path, evidence: dict, name: str = "ev.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ════════════════════════════════════════════════════════════════
# 1. REQUIRED_ARTIFACT_FIELDS 含 4 个新防重放字段
# ════════════════════════════════════════════════════════════════


class TestRequiredFieldsIncludeReplayProtection:
    """验证 REQUIRED_ARTIFACT_FIELDS 含 R67 P1-11 防重放字段。"""

    def test_required_fields_include_replay_protection_fields(self) -> None:
        """REQUIRED_ARTIFACT_FIELDS 必须含 nonce / attestation_digest /
        time_window / consumed。"""
        module = _load_evidence_module()
        required = module.REQUIRED_ARTIFACT_FIELDS
        for field in ("nonce", "attestation_digest", "time_window", "consumed"):
            assert field in required, (
                f"REQUIRED_ARTIFACT_FIELDS 必须含 R67 P1-11 防重放字段: {field}"
            )

    def test_required_fields_count_is_15(self) -> None:
        """REQUIRED_ARTIFACT_FIELDS 总数为 15(原 11 + R67 P1-11 新增 4)。"""
        module = _load_evidence_module()
        assert len(module.REQUIRED_ARTIFACT_FIELDS) == 15


# ════════════════════════════════════════════════════════════════
# 2. verify_production_promotion 强制校验新字段存在
# ════════════════════════════════════════════════════════════════


class TestVerifyProductionPromotionEnforcesReplayFields:
    """verify_production_promotion 必须检测防重放字段缺失。"""

    @pytest.mark.parametrize(
        "missing_field",
        ["nonce", "attestation_digest", "time_window", "consumed"],
    )
    def test_missing_replay_field_fails_verification(
        self, tmp_path: Path, missing_field: str
    ) -> None:
        """任一防重放字段缺失,verify_production_promotion 必须抛
        AppError(PRODUCTION_EVIDENCE_INSUFFICIENT)。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        # 删除第一个 artifact 的某个防重放字段
        evidence["artifacts"][0].pop(missing_field, None)
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT


# ════════════════════════════════════════════════════════════════
# 3. consume_evidence_for_promotion 单次使用语义
# ════════════════════════════════════════════════════════════════


class TestConsumeEvidenceForPromotion:
    """验证 consume_evidence_for_promotion 的单次使用语义。"""

    def test_first_consume_succeeds_and_marks_consumed(self, tmp_path: Path) -> None:
        """a. 首次消费成功,标记 consumed=true + consumed_at + consumed_by +
        consumed_candidate。"""
        module = _load_evidence_module()

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        result = module.consume_evidence_for_promotion(
            path,
            candidate_tag="rc-2025-07-21-v1",
            consumed_by="release-manager@example.com",
            expected_environment_id="prod-env-test",
            expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        )

        # 每个 artifact 标记 consumed
        for art in result["artifacts"]:
            assert art["consumed"] is True
            assert art["consumed_at"]
            assert art["consumed_by"] == "release-manager@example.com"
            assert art["consumed_candidate"] == "rc-2025-07-21-v1"

        # 顶层也有最后消费记录
        assert result["last_consumed_candidate"] == "rc-2025-07-21-v1"
        assert result["last_consumed_by"] == "release-manager@example.com"
        assert result["last_consumed_at"]

        # 文件已更新(读回验证)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["artifacts"][0]["consumed"] is True
        assert persisted["artifacts"][0]["consumed_candidate"] == "rc-2025-07-21-v1"

    def test_same_candidate_reconsume_is_idempotent(self, tmp_path: Path) -> None:
        """b. 同 candidate 重复消费幂等(返回当前 evidence,不抛错)。"""
        module = _load_evidence_module()

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        # 首次消费
        module.consume_evidence_for_promotion(
            path,
            candidate_tag="rc-2025-07-21-v1",
            consumed_by="release-manager@example.com",
            expected_environment_id="prod-env-test",
            expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        )
        # 同 candidate 再次消费 — 幂等
        result = module.consume_evidence_for_promotion(
            path,
            candidate_tag="rc-2025-07-21-v1",
            consumed_by="release-manager@example.com",
            expected_environment_id="prod-env-test",
            expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        )
        for art in result["artifacts"]:
            assert art["consumed"] is True
            assert art["consumed_candidate"] == "rc-2025-07-21-v1"

    def test_cross_candidate_reuse_raises_already_consumed(
        self, tmp_path: Path
    ) -> None:
        """c. 跨 candidate 复用抛 AppError(EVIDENCE_ALREADY_CONSUMED)。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        # candidate A 消费
        module.consume_evidence_for_promotion(
            path,
            candidate_tag="rc-2025-07-21-v1",
            consumed_by="release-manager@example.com",
            expected_environment_id="prod-env-test",
            expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        )
        # candidate B 试图复用 — 必须抛 EVIDENCE_ALREADY_CONSUMED
        with pytest.raises(AppError) as exc_info:
            module.consume_evidence_for_promotion(
                path,
                candidate_tag="rc-2025-07-22-v2",
                consumed_by="release-manager@example.com",
                expected_environment_id="prod-env-test",
                expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        assert exc_info.value.code == ErrorCodes.EVIDENCE_ALREADY_CONSUMED, (
            f"跨候选复用必须抛 EVIDENCE_ALREADY_CONSUMED,实际: {exc_info.value.code}"
        )

    def test_environment_id_mismatch_fails(self, tmp_path: Path) -> None:
        """d. environment_id 不匹配抛 PRODUCTION_EVIDENCE_INSUFFICIENT。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.consume_evidence_for_promotion(
                path,
                candidate_tag="rc-2025-07-21-v1",
                consumed_by="release-manager@example.com",
                expected_environment_id="different-env",
                expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT

    def test_attestation_digest_mismatch_fails(self, tmp_path: Path) -> None:
        """e. attestation_digest 不匹配抛 PRODUCTION_EVIDENCE_INSUFFICIENT。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.consume_evidence_for_promotion(
                path,
                candidate_tag="rc-2025-07-21-v1",
                consumed_by="release-manager@example.com",
                expected_environment_id="prod-env-test",
                expected_attestation_digest="sha256:different-digest",
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT

    def test_missing_nonce_fails(self, tmp_path: Path) -> None:
        """f. nonce 缺失抛 PRODUCTION_EVIDENCE_INSUFFICIENT。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        # 删除每个 artifact 的 nonce
        for art in evidence["artifacts"]:
            art.pop("nonce", None)
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.consume_evidence_for_promotion(
                path,
                candidate_tag="rc-2025-07-21-v1",
                consumed_by="release-manager@example.com",
                expected_environment_id="prod-env-test",
                expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT

    def test_expired_artifact_fails(self, tmp_path: Path) -> None:
        """g. 过期 artifact 抛 PRODUCTION_EVIDENCE_INSUFFICIENT。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_evidence()
        # 把每个 artifact 的 expires_at 设为过去
        for art in evidence["artifacts"]:
            art["expires_at"] = _past_iso(days=1)
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.consume_evidence_for_promotion(
                path,
                candidate_tag="rc-2025-07-21-v1",
                consumed_by="release-manager@example.com",
                expected_environment_id="prod-env-test",
                expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT

    def test_atomic_write_persists_consumed_state(self, tmp_path: Path) -> None:
        """consume 后文件原子写入,后续读取应看到 consumed=true。"""
        module = _load_evidence_module()

        evidence = _make_evidence()
        path = _write_evidence(tmp_path, evidence)

        module.consume_evidence_for_promotion(
            path,
            candidate_tag="rc-2025-07-21-v1",
            consumed_by="release-manager@example.com",
            expected_environment_id="prod-env-test",
            expected_attestation_digest="sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        )

        # 重新读文件
        persisted = json.loads(path.read_text(encoding="utf-8"))
        for art in persisted["artifacts"]:
            assert art["consumed"] is True
        # .tmp 文件不应残留(原子写入后已 replace)
        assert not path.with_suffix(path.suffix + ".tmp").exists()


# ════════════════════════════════════════════════════════════════
# 4. ErrorCodes.EVIDENCE_ALREADY_CONSUMED 错误码
# ════════════════════════════════════════════════════════════════


class TestEvidenceAlreadyConsumedErrorCode:
    """验证 EVIDENCE_ALREADY_CONSUMED 错误码存在且可用。"""

    def test_error_code_exists(self) -> None:
        """ErrorCodes.EVIDENCE_ALREADY_CONSUMED 必须存在。"""
        from services.error_codes import ErrorCodes
        assert hasattr(ErrorCodes, "EVIDENCE_ALREADY_CONSUMED")
        assert ErrorCodes.EVIDENCE_ALREADY_CONSUMED == "PRODUCTION.EVIDENCE.ALREADY_CONSUMED"

    def test_error_code_in_enum(self) -> None:
        """ErrorEnum 应含 EVIDENCE_ALREADY_CONSUMED(R56 §5.2 自动构建)。"""
        from services.error_codes import ErrorEnum
        assert hasattr(ErrorEnum, "EVIDENCE_ALREADY_CONSUMED")

    def test_can_raise_apperror_with_code(self) -> None:
        """能成功抛出 AppError(EVIDENCE_ALREADY_CONSUMED)。"""
        from services.error_codes import AppError, ErrorCodes
        with pytest.raises(AppError) as exc_info:
            raise AppError(
                ErrorCodes.EVIDENCE_ALREADY_CONSUMED,
                params={
                    "artifact_type": "SOAK_7DAY",
                    "consumed_candidate": "rc-old",
                    "candidate_tag": "rc-new",
                },
            )
        assert exc_info.value.code == ErrorCodes.EVIDENCE_ALREADY_CONSUMED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
