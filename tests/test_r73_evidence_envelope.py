#!/usr/bin/env python3
"""R73 §5.7: Typed evidence envelope tests.

测试覆盖(对应 R73 §5.7 P0-02 / P1-04 / P1-05 要求):
    1. master run envelope 不可晋级 (gate_level=development)
    2. failed RC envelope 不可晋级 (overall_conclusion=failure)
    3. successful RC envelope 在所有字段齐全时可晋级
    4. 篡改 payload_digest 被拒绝

R73 P1-05 新增覆盖:
    5. production run envelope 不可晋级 (gate_level=production)
    6. event/ref 检查 (workflow_dispatch / pull_request 不可晋级)
    7. 上下文感知分级校验 (_validate_tiered_invariants)
    8. promotion_eligible 显式覆盖 (CLI / workflow 可传入)

额外覆盖:
    - canonical_payload_digest 确定性(相同输入→相同输出)
    - envelope_to_file / load_envelope 往返一致性
    - validate_envelope 基础校验(枚举/类型/必填字段)
    - verify_rc_3x.verify_evidence_envelopes() 集成验证
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 模块加载 — 直接按文件路径加载,避免触发 scripts/__init__ 副作用
# ════════════════════════════════════════════════════════════════


def _load_envelope_module():
    """加载 scripts.evidence_envelope 模块。"""
    spec = importlib.util.spec_from_file_location(
        "evidence_envelope",
        REPO_ROOT / "scripts" / "evidence_envelope.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_verify_rc_3x_module():
    """加载 scripts.verify_rc_3x 模块(含 verify_evidence_envelopes)。"""
    spec = importlib.util.spec_from_file_location(
        "verify_rc_3x",
        REPO_ROOT / "scripts" / "verify_rc_3x.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════
# 测试常量与 fixtures
# ════════════════════════════════════════════════════════════════

VALID_SOURCE_SHA = "a" * 40  # 40-hex
VALID_IMAGE_REPO_DIGEST = f"ghcr.io/owner/repo@sha256:{'b' * 64}"
VALID_RUNTIME_CONFIG_DIGEST = f"sha256:{'c' * 64}"
VALID_WORKFLOW_PATH = ".github/workflows/release-gates.yml"
DEFAULT_PAYLOAD = {"gate_results": [{"name": "digest_pull", "passed": True}]}


@pytest.fixture
def envelope_mod():
    """提供 evidence_envelope 模块。"""
    return _load_envelope_module()


@pytest.fixture
def verify_mod():
    """提供 verify_rc_3x 模块。"""
    return _load_verify_rc_3x_module()


def _build_rc_success_envelope(mod):
    """构造一个完整有效的 RC success envelope(所有字段齐全)。"""
    return mod.build_evidence_envelope(
        gate_level="rc",
        event="push",
        ref="refs/tags/rc-v2026-07-26",
        source_sha=VALID_SOURCE_SHA,
        run_id=123456,
        run_attempt=1,
        workflow_path=VALID_WORKFLOW_PATH,
        overall_conclusion="success",
        payload=DEFAULT_PAYLOAD,
        image_repo_digest=VALID_IMAGE_REPO_DIGEST,
        runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
    )


# ════════════════════════════════════════════════════════════════
# 1. master run envelope 不可晋级
# ════════════════════════════════════════════════════════════════


class TestMasterRunNonPromotable:
    """master run(gate_level=development)永远不可晋级。"""

    def test_development_gate_level_not_promotable(self, envelope_mod):
        """R73 §5.7: gate_level=development → is_promotion_eligible=False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=100,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",  # 即使 success 也不可晋级
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        # envelope 自身的 promotion_eligible 字段必须为 False
        assert envelope["promotion_eligible"] is False
        # gate_level 必须为 development
        assert envelope["gate_level"] == "development"
        # 权威审计也必须返回 False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_development_envelope_still_valid(self, envelope_mod):
        """development envelope 结构合法(只是不可晋级)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=100,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, f"development envelope 应通过结构校验: {errors}"

    def test_development_without_digests_not_promotable(self, envelope_mod):
        """master run 即使 success,缺少 digest 也不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=100,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=None,  # master run 可能无 image digest
            runtime_config_digest=None,
        )
        assert envelope["promotion_eligible"] is False
        assert envelope_mod.is_promotion_eligible(envelope) is False


# ════════════════════════════════════════════════════════════════
# 2. failed RC envelope 不可晋级
# ════════════════════════════════════════════════════════════════


class TestFailedRcNonPromotable:
    """failed/cancelled/skipped RC run 永远不可晋级。"""

    def test_failure_conclusion_not_promotable(self, envelope_mod):
        """overall_conclusion=failure → is_promotion_eligible=False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=200,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="failure",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        assert envelope["promotion_eligible"] is False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_blocked_conclusion_not_promotable(self, envelope_mod):
        """overall_conclusion=blocked → is_promotion_eligible=False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=201,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="blocked",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        assert envelope["promotion_eligible"] is False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_failed_rc_still_valid_structure(self, envelope_mod):
        """failed RC envelope 结构合法(只是不可晋级)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=200,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="failure",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, f"failed RC envelope 应通过结构校验: {errors}"


# ════════════════════════════════════════════════════════════════
# 3. successful RC envelope 可晋级(所有字段齐全)
# ════════════════════════════════════════════════════════════════


class TestSuccessfulRcPromotable:
    """successful RC envelope 在所有字段齐全时可晋级。"""

    def test_rc_success_with_digests_promotable(self, envelope_mod):
        """gate_level=rc + success + 所有字段齐全 → is_promotion_eligible=True。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        assert envelope["promotion_eligible"] is True
        assert envelope_mod.is_promotion_eligible(envelope) is True

    def test_rc_success_valid_structure(self, envelope_mod):
        """successful RC envelope 通过结构校验。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, f"errors: {errors}"

    def test_rc_success_missing_image_digest_not_promotable(self, envelope_mod):
        """RC success 但缺少 image_repo_digest → 不可晋级(no missing digest)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=300,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=None,  # 缺少 digest
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        # envelope 字段仍为 True(build 仅基于 gate_level + conclusion)
        assert envelope["promotion_eligible"] is True
        # 但权威审计必须返回 False(defense in depth)
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_rc_success_missing_runtime_config_digest_not_promotable(
        self, envelope_mod
    ):
        """RC success 但缺少 runtime_config_digest → 不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=301,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=None,  # 缺少 digest
        )
        assert envelope["promotion_eligible"] is True
        assert envelope_mod.is_promotion_eligible(envelope) is False


# ════════════════════════════════════════════════════════════════
# 4. 篡改 payload_digest 被拒绝
# ════════════════════════════════════════════════════════════════


class TestTamperedPayloadDigestRejected:
    """篡改 payload_digest 必须被拒绝。"""

    def test_tampered_payload_digest_format_rejected(self, envelope_mod):
        """payload_digest 改为非法格式 → validate_envelope 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        # 篡改:替换为非法格式
        envelope["payload_digest"] = "sha256:tampered"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("payload_digest" in e for e in errors)

    def test_tampered_payload_digest_string_rejected(self, envelope_mod):
        """payload_digest 改为非 sha256 字符串 → validate_envelope 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["payload_digest"] = "TAMPERED-DIGEST"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("payload_digest" in e for e in errors)

    def test_tampered_payload_digest_blocks_promotion(self, envelope_mod):
        """篡改 payload_digest → is_promotion_eligible 返回 False。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["payload_digest"] = "sha256:tampered"
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_tampered_wrong_valid_digest_blocks_promotion(self, envelope_mod):
        """payload_digest 改为另一个合法 sha256 值 → 结构校验通过,
        但与原始 payload 不匹配(无法在无 payload 时检测内容篡改,
        此测试验证结构校验不会误判)。

        注:envelope 不包含 payload 本身,无法在加载时重算 digest
        对比。内容篡改需通过 payload+envelope 配对校验(超出本模块范围)。
        此测试验证:另一个合法 digest 仍通过结构校验,但 envelope
        的 promotion_eligible 字段不变(仍为 True,因为 build 时已计算)。
        """
        envelope = _build_rc_success_envelope(envelope_mod)
        # 替换为另一个合法的 64-hex sha256(全 0)
        envelope["payload_digest"] = "sha256:" + "0" * 64
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, "合法格式的 digest 应通过结构校验"
        # 注意:内容篡改需通过 verify_rc_3x 的 payload+envelope 配对检测


# ════════════════════════════════════════════════════════════════
# 5. canonical_payload_digest 确定性
# ════════════════════════════════════════════════════════════════


class TestCanonicalPayloadDigest:
    """canonical_payload_digest 确定性与格式。"""

    def test_deterministic(self, envelope_mod):
        """相同 payload 多次计算 → 相同 digest。"""
        payload = {"b": 2, "a": 1, "c": [3, 2, 1]}
        d1 = envelope_mod.canonical_payload_digest(payload)
        d2 = envelope_mod.canonical_payload_digest(payload)
        assert d1 == d2

    def test_key_order_independent(self, envelope_mod):
        """不同 key 顺序的相同 payload → 相同 digest(canonical 排序)。"""
        p1 = {"a": 1, "b": 2}
        p2 = {"b": 2, "a": 1}
        assert envelope_mod.canonical_payload_digest(p1) == \
            envelope_mod.canonical_payload_digest(p2)

    def test_format(self, envelope_mod):
        """digest 格式为 sha256:<64-hex>。"""
        digest = envelope_mod.canonical_payload_digest({"k": "v"})
        assert digest.startswith("sha256:")
        hex_part = digest[len("sha256:"):]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_different_payloads_different_digest(self, envelope_mod):
        """不同 payload → 不同 digest。"""
        d1 = envelope_mod.canonical_payload_digest({"k": "v1"})
        d2 = envelope_mod.canonical_payload_digest({"k": "v2"})
        assert d1 != d2


# ════════════════════════════════════════════════════════════════
# 6. envelope_to_file / load_envelope 往返一致性
# ════════════════════════════════════════════════════════════════


class TestEnvelopeFileIO:
    """envelope_to_file / load_envelope 往返一致性。"""

    def test_round_trip(self, envelope_mod, tmp_path):
        """写入后读回 → 与原 envelope 一致。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        path = tmp_path / "rc-candidate-evidence-test.json"
        envelope_mod.envelope_to_file(envelope, path)
        loaded = envelope_mod.load_envelope(path)
        assert loaded == envelope

    def test_canonical_json_format(self, envelope_mod, tmp_path):
        """写入文件为 canonical JSON(sorted keys + compact separators)。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        path = tmp_path / "evidence.json"
        envelope_mod.envelope_to_file(envelope, path)
        raw = path.read_text(encoding="utf-8")
        # canonical JSON 不应含多余空白(逗号后无空格)
        assert ", " not in raw
        assert ": " not in raw
        # 应能直接 json.loads
        assert json.loads(raw) == envelope

    def test_load_nonexistent_raises(self, envelope_mod, tmp_path):
        """加载不存在的文件 → FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            envelope_mod.load_envelope(tmp_path / "missing.json")

    def test_load_non_dict_raises(self, envelope_mod, tmp_path):
        """加载非 dict JSON → ValueError。"""
        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError):
            envelope_mod.load_envelope(path)

    def test_creates_parent_dirs(self, envelope_mod, tmp_path):
        """写入文件时自动创建父目录。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        path = tmp_path / "nested" / "dir" / "evidence.json"
        envelope_mod.envelope_to_file(envelope, path)
        assert path.exists()


# ════════════════════════════════════════════════════════════════
# 7. validate_envelope 基础校验
# ════════════════════════════════════════════════════════════════


class TestValidateEnvelope:
    """validate_envelope 结构/类型/枚举校验。"""

    def test_valid_envelope_passes(self, envelope_mod):
        envelope = _build_rc_success_envelope(envelope_mod)
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True
        assert errors == []

    def test_missing_field_rejected(self, envelope_mod):
        """缺少必填字段 → 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        del envelope["gate_level"]
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("gate_level" in e for e in errors)

    def test_invalid_gate_level_rejected(self, envelope_mod):
        """gate_level 不在枚举内 → 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["gate_level"] = "staging"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("gate_level" in e for e in errors)

    def test_invalid_conclusion_rejected(self, envelope_mod):
        """overall_conclusion 不在枚举内 → 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["overall_conclusion"] = "cancelled"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("overall_conclusion" in e for e in errors)

    def test_invalid_source_sha_rejected(self, envelope_mod):
        """source_sha 非 40-hex → 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["source_sha"] = "not-a-sha"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("source_sha" in e for e in errors)

    def test_non_dict_envelope_rejected(self, envelope_mod):
        """envelope 非 dict → 拒绝。"""
        valid, errors = envelope_mod.validate_envelope("not a dict")  # type: ignore
        assert valid is False
        assert len(errors) >= 1

    def test_run_id_must_be_int(self, envelope_mod):
        """run_id 为字符串 → 拒绝(类型错误)。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["run_id"] = "123"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("run_id" in e for e in errors)

    def test_run_id_bool_rejected(self, envelope_mod):
        """run_id 为 bool(True)→ 拒绝(bool 是 int 子类,需显式排除)。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["run_id"] = True
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False

    def test_promotion_eligible_must_be_bool(self, envelope_mod):
        """promotion_eligible 非 bool → 拒绝。"""
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["promotion_eligible"] = "true"
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("promotion_eligible" in e for e in errors)


# ════════════════════════════════════════════════════════════════
# 8. verify_rc_3x.verify_evidence_envelopes() 集成验证
# ════════════════════════════════════════════════════════════════


class TestVerifyEvidenceEnvelopes:
    """verify_rc_3x.verify_evidence_envelopes() 集成测试。"""

    def test_valid_rc_envelopes_pass(self, envelope_mod, verify_mod, tmp_path):
        """多个合法 RC envelope → verdict.passed=True。"""
        paths = []
        for i in range(3):
            env = _build_rc_success_envelope(envelope_mod)
            env["run_id"] = 1000 + i
            path = tmp_path / f"rc-candidate-evidence-{i}.json"
            envelope_mod.envelope_to_file(env, path)
            paths.append(path)

        verdict = verify_mod.verify_evidence_envelopes(paths, expected_gate_level="rc")
        assert verdict["passed"] is True
        assert verdict["checked"] == 3
        assert verdict["passed_count"] == 3
        assert verdict["rejected_count"] == 0
        assert verdict["failed_count"] == 0

    def test_development_envelope_rejected_for_rc(self, envelope_mod, verify_mod, tmp_path):
        """development-level envelope 在 RC 期望下被拒绝。"""
        env = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=100,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        path = tmp_path / "development-evidence-001.json"
        envelope_mod.envelope_to_file(env, path)

        verdict = verify_mod.verify_evidence_envelopes([path], expected_gate_level="rc")
        assert verdict["passed"] is False
        assert verdict["rejected_count"] == 1
        r = verdict["results"][0]
        assert r["rejected"] is True
        assert "development" in r["rejected_reason"]

    def test_non_promotable_rc_envelope_rejected(self, envelope_mod, verify_mod, tmp_path):
        """promotion_eligible=false 的 RC envelope 被拒绝。"""
        env = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=200,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="failure",  # 失败 → promotion_eligible=false
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        path = tmp_path / "rc-candidate-evidence-failed.json"
        envelope_mod.envelope_to_file(env, path)

        verdict = verify_mod.verify_evidence_envelopes([path], expected_gate_level="rc")
        assert verdict["passed"] is False
        assert verdict["rejected_count"] == 1
        r = verdict["results"][0]
        assert r["rejected"] is True
        assert "promotion_eligible=false" in r["rejected_reason"]

    def test_missing_envelope_rejected(self, verify_mod, tmp_path):
        """文件不含 envelope(无 schema_version)→ 失败。"""
        path = tmp_path / "not-an-envelope.json"
        path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

        verdict = verify_mod.verify_evidence_envelopes([path], expected_gate_level="rc")
        assert verdict["passed"] is False
        assert verdict["failed_count"] == 1
        r = verdict["results"][0]
        assert r["has_envelope"] is False

    def test_nonexistent_file_failed(self, verify_mod, tmp_path):
        """文件不存在 → 失败。"""
        path = tmp_path / "missing.json"
        verdict = verify_mod.verify_evidence_envelopes([path], expected_gate_level="rc")
        assert verdict["passed"] is False
        assert verdict["failed_count"] == 1
        assert verdict["results"][0]["exists"] is False

    def test_structured_verdict_shape(self, envelope_mod, verify_mod, tmp_path):
        """verdict 结构完整(顶层字段齐全)。"""
        env = _build_rc_success_envelope(envelope_mod)
        path = tmp_path / "rc-candidate-evidence-001.json"
        envelope_mod.envelope_to_file(env, path)

        verdict = verify_mod.verify_evidence_envelopes([path], expected_gate_level="rc")
        for key in ("passed", "checked", "passed_count", "failed_count",
                    "rejected_count", "expected_gate_level", "results"):
            assert key in verdict, f"verdict 缺少字段: {key}"
        r = verdict["results"][0]
        for key in ("path", "exists", "has_envelope", "valid", "errors",
                    "gate_level", "promotion_eligible",
                    "audit_promotion_eligible", "rejected", "rejected_reason"):
            assert key in r, f"per-artifact verdict 缺少字段: {key}"


# ════════════════════════════════════════════════════════════════
# 9. R73 P1-05: production run envelope 不可晋级
# ════════════════════════════════════════════════════════════════


class TestProductionRunNonPromotable:
    """R73 P1-05: production-v* tag push 生成 production 级 envelope,永远不可晋级。

    production envelope 是部署记录(deployment record),不是 promotion candidate。
    即便所有 gates success,production envelope 的 promotion_eligible 必须为 False。
    """

    def test_production_gate_level_not_promotable(self, envelope_mod):
        """R73 P1-05: gate_level=production → is_promotion_eligible=False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=400,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",  # 即使 success 也不可晋级
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        # envelope 自身的 promotion_eligible 字段必须为 False
        assert envelope["promotion_eligible"] is False
        # gate_level 必须为 production
        assert envelope["gate_level"] == "production"
        # 权威审计也必须返回 False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_production_envelope_still_valid(self, envelope_mod):
        """production envelope 结构合法(只是不可晋级)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=401,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, f"production envelope 应通过结构校验: {errors}"

    def test_production_with_explicit_promotion_eligible_overridden_to_false(
        self, envelope_mod
    ):
        """显式传入 promotion_eligible=True 仍被 _validate_tiered_invariants 拒绝。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=402,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,  # 显式覆盖为 True (恶意/错误调用)
        )
        # envelope 自身字段为 True (caller override)
        assert envelope["promotion_eligible"] is True
        # 但 validate_envelope 必须拒绝 (tiered invariants 拦截)
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("production" in e and "promotion_eligible" in e for e in errors), \
            f"应拒绝 production envelope 的 promotion_eligible=True: {errors}"
        # 权威审计也必须返回 False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_production_without_digests_not_promotable(self, envelope_mod):
        """production envelope 缺少 digest 也不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=403,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=None,
            runtime_config_digest=None,
        )
        assert envelope["promotion_eligible"] is False
        assert envelope_mod.is_promotion_eligible(envelope) is False


# ════════════════════════════════════════════════════════════════
# 10. R73 P1-05: event/ref 检查 (只有 push + rc-v* tag 可晋级)
# ════════════════════════════════════════════════════════════════


class TestEventRefChecks:
    """R73 P1-05: is_promotion_eligible 必须审计 event 与 ref。

    只有 event=push + ref=refs/tags/rc-v* 的 envelope 才可晋级。
    workflow_dispatch / pull_request / 非 rc-v* ref 一律不可晋级。
    """

    def test_workflow_dispatch_event_not_promotable(self, envelope_mod):
        """event=workflow_dispatch → 不可晋级(即便 gate_level=rc + success)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="workflow_dispatch",  # 非 push
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=500,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,  # 显式覆盖为 True
        )
        # envelope 自身字段为 True (caller override)
        assert envelope["promotion_eligible"] is True
        # 但权威审计必须返回 False (event != push)
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_pull_request_event_not_promotable(self, envelope_mod):
        """event=pull_request → 不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="pull_request",  # 非 push
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=501,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,
        )
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_master_ref_not_promotable(self, envelope_mod):
        """ref=refs/heads/master → 不可晋级(即便 gate_level=rc)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",  # 错误的 gate_level for master ref
            event="push",
            ref="refs/heads/master",  # 非 rc-v* tag
            source_sha=VALID_SOURCE_SHA,
            run_id=502,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,
        )
        # 权威审计必须返回 False (ref 不匹配 rc-v*)
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_production_ref_not_promotable(self, envelope_mod):
        """ref=refs/tags/production-v* → 不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",  # 错误的 gate_level for production ref
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=503,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,
        )
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_non_rc_tag_ref_not_promotable(self, envelope_mod):
        """ref=refs/tags/v1.0.0 (非 rc-v* 前缀)→ 不可晋级。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/v1.0.0",  # 非 rc-v* 前缀
            source_sha=VALID_SOURCE_SHA,
            run_id=504,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,
        )
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_main_branch_ref_not_promotable(self, envelope_mod):
        """ref=refs/heads/main → 不可晋级(等同于 master)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/heads/main",
            source_sha=VALID_SOURCE_SHA,
            run_id=505,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,
        )
        assert envelope_mod.is_promotion_eligible(envelope) is False


# ════════════════════════════════════════════════════════════════
# 11. R73 P1-05: 上下文感知分级校验 (_validate_tiered_invariants)
# ════════════════════════════════════════════════════════════════


class TestTieredInvariants:
    """R73 P1-05: _validate_tiered_invariants 上下文感知校验。

    validate_envelope 会调用 _validate_tiered_invariants,根据 event + ref
    交叉校验 gate_level / promotion_eligible,防止 caller 构造错误 envelope。
    """

    def test_master_run_with_rc_gate_level_rejected(self, envelope_mod):
        """master run (push + refs/heads/master) 的 gate_level 必须为 development。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",  # 错误:master run 应为 development
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=600,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("master" in e and "development" in e for e in errors), \
            f"应拒绝 master run 的 gate_level=rc: {errors}"

    def test_master_run_with_production_gate_level_rejected(self, envelope_mod):
        """master run 的 gate_level 不能为 production。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=601,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("master" in e and "development" in e for e in errors)

    def test_master_run_promotion_eligible_true_rejected(self, envelope_mod):
        """master run 的 promotion_eligible 必须为 False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/master",
            source_sha=VALID_SOURCE_SHA,
            run_id=602,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,  # 错误:master run 应为 False
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("master" in e and "promotion_eligible" in e for e in errors), \
            f"应拒绝 master run 的 promotion_eligible=True: {errors}"

    def test_rc_run_with_development_gate_level_rejected(self, envelope_mod):
        """RC run (push + refs/tags/rc-v*) 的 gate_level 必须为 rc。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",  # 错误:RC run 应为 rc
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=603,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("rc" in e.lower() and "gate_level" in e for e in errors), \
            f"应拒绝 RC run 的 gate_level=development: {errors}"

    def test_rc_run_with_production_gate_level_rejected(self, envelope_mod):
        """RC run 的 gate_level 不能为 production。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="production",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=604,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("rc" in e.lower() and "gate_level" in e for e in errors)

    def test_failed_rc_run_with_promotion_eligible_true_rejected(self, envelope_mod):
        """failed RC run (overall_conclusion=failure) 的 promotion_eligible 必须为 False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=605,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="failure",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,  # 错误:failed RC run 应为 False
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("failed" in e.lower() and "promotion_eligible" in e for e in errors), \
            f"应拒绝 failed RC run 的 promotion_eligible=True: {errors}"

    def test_production_run_with_rc_gate_level_rejected(self, envelope_mod):
        """production run (push + refs/tags/production-v*) 的 gate_level 必须为 production。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",  # 错误:production run 应为 production
            event="push",
            ref="refs/tags/production-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=606,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is False
        assert any("production" in e and "gate_level" in e for e in errors), \
            f"应拒绝 production run 的 gate_level=rc: {errors}"

    def test_workflow_dispatch_not_tiered(self, envelope_mod):
        """workflow_dispatch 事件不参与 tiered invariants (permissive default)。

        非 push 事件不强制 gate_level 与 ref 的对应关系,
        但 is_promotion_eligible 仍会审计 event/ref (defense in depth)。
        """
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",  # 任意 gate_level (workflow_dispatch 不分级)
            event="workflow_dispatch",
            ref="refs/heads/master",  # 任意 ref
            source_sha=VALID_SOURCE_SHA,
            run_id=607,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, \
            f"workflow_dispatch 事件应通过 tiered invariants (permissive): {errors}"
        # 但 is_promotion_eligible 仍返回 False (event != push)
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_main_branch_tiered_like_master(self, envelope_mod):
        """refs/heads/main 与 refs/heads/master 同等对待 (tiered invariants)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="development",
            event="push",
            ref="refs/heads/main",
            source_sha=VALID_SOURCE_SHA,
            run_id=608,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
        )
        valid, errors = envelope_mod.validate_envelope(envelope)
        assert valid is True, f"refs/heads/main 应通过 tiered invariants: {errors}"


# ════════════════════════════════════════════════════════════════
# 12. R73 P1-05: promotion_eligible 显式覆盖
# ════════════════════════════════════════════════════════════════


class TestPromotionEligibleOverride:
    """R73 P1-05: promotion_eligible 显式覆盖参数。

    build_evidence_envelope 接受 promotion_eligible 参数,允许 workflow
    步骤基于 needs.*.result 决定 eligibility。当 None 时,从 gate_level +
    overall_conclusion 自动计算。
    """

    def test_auto_computed_when_none(self, envelope_mod):
        """promotion_eligible=None → 自动计算 (rc + success = True)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=700,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=None,  # 自动计算
        )
        assert envelope["promotion_eligible"] is True

    def test_auto_computed_false_for_failure(self, envelope_mod):
        """promotion_eligible=None + failure → 自动计算为 False。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=701,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="failure",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=None,  # 自动计算
        )
        assert envelope["promotion_eligible"] is False

    def test_explicit_true_override(self, envelope_mod):
        """promotion_eligible=True 显式覆盖 (workflow 决定 eligibility)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=702,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=True,  # 显式覆盖
        )
        assert envelope["promotion_eligible"] is True
        # is_promotion_eligible 应返回 True (所有条件满足)
        assert envelope_mod.is_promotion_eligible(envelope) is True

    def test_explicit_false_override(self, envelope_mod):
        """promotion_eligible=False 显式覆盖 (即便 rc + success 也不可晋级)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=703,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=False,  # 显式覆盖
        )
        assert envelope["promotion_eligible"] is False
        # is_promotion_eligible 必须返回 False (envelope 自身字段为 False)
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_explicit_false_blocks_promotion_even_with_digests(self, envelope_mod):
        """显式 False 即便所有 digest 齐全也阻断晋级 (defense in depth)。"""
        envelope = envelope_mod.build_evidence_envelope(
            gate_level="rc",
            event="push",
            ref="refs/tags/rc-v2026-07-26",
            source_sha=VALID_SOURCE_SHA,
            run_id=704,
            run_attempt=1,
            workflow_path=VALID_WORKFLOW_PATH,
            overall_conclusion="success",
            payload=DEFAULT_PAYLOAD,
            image_repo_digest=VALID_IMAGE_REPO_DIGEST,
            runtime_config_digest=VALID_RUNTIME_CONFIG_DIGEST,
            promotion_eligible=False,  # 显式阻断
        )
        # 所有其他条件满足,但 envelope 自身字段为 False
        assert envelope_mod.is_promotion_eligible(envelope) is False


# ════════════════════════════════════════════════════════════════
# 13. R73 P1-05: CLI build / validate 集成
# ════════════════════════════════════════════════════════════════


class TestCliBuildValidate:
    """R73 P1-05: evidence_envelope.py CLI build / validate 子命令集成测试。"""

    def test_cli_build_development_envelope(self, envelope_mod, tmp_path):
        """CLI build 子命令构建 development envelope (master push 场景)。"""
        import subprocess
        output = tmp_path / "development-evidence.json"
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "build",
                "--gate-level", "development",
                "--promotion-eligible", "false",
                "--source-sha", VALID_SOURCE_SHA,
                "--run-id", "800",
                "--run-attempt", "1",
                "--event", "push",
                "--ref", "refs/heads/master",
                "--overall-conclusion", "success",
                "--image-repo-digest", VALID_IMAGE_REPO_DIGEST,
                "--runtime-config-digest", VALID_RUNTIME_CONFIG_DIGEST,
                "--output", str(output),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, f"CLI build failed: {rc.stderr}"
        assert output.exists()
        envelope = envelope_mod.load_envelope(output)
        assert envelope["gate_level"] == "development"
        assert envelope["promotion_eligible"] is False
        assert envelope["event"] == "push"
        assert envelope["ref"] == "refs/heads/master"

    def test_cli_build_rc_envelope(self, envelope_mod, tmp_path):
        """CLI build 子命令构建 rc envelope (rc-v* tag push 场景)。"""
        import subprocess
        output = tmp_path / "rc-candidate-evidence.json"
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "build",
                "--gate-level", "rc",
                "--promotion-eligible", "true",
                "--source-sha", VALID_SOURCE_SHA,
                "--run-id", "801",
                "--run-attempt", "1",
                "--event", "push",
                "--ref", "refs/tags/rc-v2026-07-26",
                "--overall-conclusion", "success",
                "--image-repo-digest", VALID_IMAGE_REPO_DIGEST,
                "--runtime-config-digest", VALID_RUNTIME_CONFIG_DIGEST,
                "--output", str(output),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, f"CLI build failed: {rc.stderr}"
        envelope = envelope_mod.load_envelope(output)
        assert envelope["gate_level"] == "rc"
        assert envelope["promotion_eligible"] is True
        assert envelope_mod.is_promotion_eligible(envelope) is True

    def test_cli_build_production_envelope(self, envelope_mod, tmp_path):
        """CLI build 子命令构建 production envelope (production-v* tag 场景)。"""
        import subprocess
        output = tmp_path / "production-deployment-evidence.json"
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "build",
                "--gate-level", "production",
                "--promotion-eligible", "false",
                "--source-sha", VALID_SOURCE_SHA,
                "--run-id", "802",
                "--run-attempt", "1",
                "--event", "push",
                "--ref", "refs/tags/production-v2026-07-26",
                "--overall-conclusion", "success",
                "--image-repo-digest", VALID_IMAGE_REPO_DIGEST,
                "--runtime-config-digest", VALID_RUNTIME_CONFIG_DIGEST,
                "--output", str(output),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, f"CLI build failed: {rc.stderr}"
        envelope = envelope_mod.load_envelope(output)
        assert envelope["gate_level"] == "production"
        assert envelope["promotion_eligible"] is False
        assert envelope_mod.is_promotion_eligible(envelope) is False

    def test_cli_build_rejects_invalid_tiered_combination(self, envelope_mod, tmp_path):
        """CLI build 拒绝 master run 的 gate_level=rc (tiered invariants)。"""
        import subprocess
        output = tmp_path / "invalid.json"
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "build",
                "--gate-level", "rc",  # 错误:master run 应为 development
                "--promotion-eligible", "true",
                "--source-sha", VALID_SOURCE_SHA,
                "--run-id", "803",
                "--run-attempt", "1",
                "--event", "push",
                "--ref", "refs/heads/master",
                "--overall-conclusion", "success",
                "--image-repo-digest", VALID_IMAGE_REPO_DIGEST,
                "--runtime-config-digest", VALID_RUNTIME_CONFIG_DIGEST,
                "--output", str(output),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode != 0, "CLI build 应拒绝 master run 的 gate_level=rc"
        assert "self-validation" in rc.stderr or "master" in rc.stderr

    def test_cli_validate_valid_envelope(self, envelope_mod, tmp_path):
        """CLI validate 子命令校验合法 envelope。"""
        import subprocess
        envelope = _build_rc_success_envelope(envelope_mod)
        path = tmp_path / "valid.json"
        envelope_mod.envelope_to_file(envelope, path)
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "validate",
                str(path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, f"CLI validate failed: {rc.stderr}"
        assert "PASS" in rc.stdout

    def test_cli_validate_invalid_envelope(self, envelope_mod, tmp_path):
        """CLI validate 子命令拒绝篡改的 envelope。"""
        import subprocess
        envelope = _build_rc_success_envelope(envelope_mod)
        envelope["gate_level"] = "staging"  # 非法枚举值
        path = tmp_path / "invalid.json"
        envelope_mod.envelope_to_file(envelope, path)
        rc = subprocess.run(
            [
                sys.executable, "scripts/evidence_envelope.py", "validate",
                str(path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert rc.returncode != 0
        assert "FAIL" in rc.stderr or "gate_level" in rc.stderr
