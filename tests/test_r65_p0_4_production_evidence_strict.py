"""R65 P0-04: 生产运行证据严格门禁 — 单元测试。

测试覆盖:
1. scripts/generate_production_evidence.py:
   - dry_run 模式输出 evidence_mode="dry_run" + production_promotion_allowed=false
   - verify_production_promotion() 严格校验 5 类 artifact + 签名 + 过期 + --skip
2. services/error_codes.py:
   - PRODUCTION_EVIDENCE_INSUFFICIENT 错误码注册(http=412, critical)
   - safe_params=["reason", "missing"](params 长度上限 100 字符)
3. .github/workflows/release-gates.yml:
   - production-promotion-gate job 仅在 v*.*.* tag 触发
   - release-summary 报告 production_promotion_allowed

R65 P0-04 整改要点:
    - 默认 evidence_mode=dry_run,输出文件名含 dry_run,JSON 含
      evidence_mode="dry_run" + production_promotion_allowed=false,
      严禁出现 "production passed" 等通过性断言。
    - production promotion 只接受独立、签名、不可变、未过期的真实证据 artifact。
    - 每类证据含 environment_id / commit_sha / image_digest / started_at /
      ended_at / raw_data_digest / executed_by / approved_by / signature。
    - SOAK_7DAY / RESTORE_3X / OUTBOX_FAULT_INJECTION / RU_72H / SUPPLY_CHAIN
      任一缺失/过期即阻断;--skip 在 production 模式下被禁止。
    - verify_production_promotion() 强制校验以上条件,失败抛
      AppError(ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT)。

12 个测试场景:
    1. dry_run 模式输出 evidence_mode="dry_run" + production_promotion_allowed=false
    2. verify_production_promotion() 拒绝 dry_run 证据
    3. 拒绝未签名证据(signature 块缺失)
    4. 拒绝已过期证据(expires_at < now)
    5. 拒绝缺失 SOAK_7DAY artifact
    6. 拒绝缺失 RESTORE_3X artifact
    7. 拒绝缺失 OUTBOX_FAULT_INJECTION artifact
    8. 拒绝缺失 RU_72H artifact
    9. 拒绝缺失 SUPPLY_CHAIN artifact
    10. 拒绝使用 --skip 标志(flags.skip 非空)
    11. 接受完整、已签名、未过期的 production 证据
    12. 每个 artifact 含全部必需字段
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 辅助函数 — 构造合法/非法的 production evidence JSON fixture
# ════════════════════════════════════════════════════════════════


def _load_evidence_module():
    """加载 generate_production_evidence 模块(避免 main 触发 argparse)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "generate_production_evidence",
        REPO_ROOT / "scripts" / "generate_production_evidence.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _future_iso(days: int = 7) -> str:
    """返回未来 N 天的 ISO8601 字符串(UTC)。"""
    future = datetime.now(timezone.utc) + timedelta(days=days)
    return future.isoformat()


def _past_iso(days: int = 1) -> str:
    """返回过去 N 天的 ISO8601 字符串(UTC)。"""
    past = datetime.now(timezone.utc) - timedelta(days=days)
    return past.isoformat()


def _make_valid_artifact(artifact_type: str) -> dict:
    """构造一个合法的、未过期的、已签名的 production artifact。

    R67 P1-11: 防重放字段 — nonce / attestation_digest / time_window / consumed。
    """
    return {
        "artifact_type": artifact_type,
        "environment_id": f"prod-env-{artifact_type.lower()}",
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
        "nonce": f"nonce-{artifact_type.lower()}-{_past_iso(days=2)}",
        "attestation_digest": "sha256:attestation0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "time_window": {
            "started_at": _past_iso(days=2),
            "ended_at": _past_iso(days=1),
        },
        "consumed": False,
    }


def _make_valid_production_evidence() -> dict:
    """构造一个完整的合法 production evidence JSON 对象。

    包含:
    - evidence_mode="production"
    - 顶层 signature 块(cosign, verified=true)
    - flags.skip 为空
    - artifacts 数组含全部 6 类必需 artifact(均未过期、字段完整)
      R67 P0-04 新增 RC_VERIFY_3X
    - R67 P1-11: 每个 artifact 含防重放字段(nonce / attestation_digest /
      time_window / consumed)
    """
    artifact_types = [
        "SOAK_7DAY",
        "RESTORE_3X",
        "OUTBOX_FAULT_INJECTION",
        "RU_72H",
        "SUPPLY_CHAIN",
        "RC_VERIFY_3X",
    ]
    return {
        "schema_version": "r64_p1_12_v1",
        "evidence_mode": "production",
        "production_promotion_allowed": False,  # verify_production_promotion 通过后会设为 True
        "generated_at": _past_iso(days=1),
        "flags": {
            "skip": [],
            "dry_run": False,
        },
        "signature": {
            "method": "cosign",
            "verified": True,
            "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----",
            "sig": "base64-encoded-signature",
        },
        "artifacts": [_make_valid_artifact(at) for at in artifact_types],
    }


def _write_evidence(tmp_path: Path, evidence: dict, name: str = "production_evidence.json") -> Path:
    """将 evidence dict 写入 tmp_path 下的 JSON 文件,返回路径。"""
    p = tmp_path / name
    p.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ════════════════════════════════════════════════════════════════
# 1. dry_run 模式输出标记
# ════════════════════════════════════════════════════════════════


class TestDryRunModeMarking:
    """R65 P0-04: dry_run 模式必须显式标记,不可被消费为 production 证据。"""

    def test_dry_run_evidence_marked_with_evidence_mode_and_promotion_false(self, tmp_path):
        """场景 1:dry_run 模式输出 evidence_mode="dry_run" + production_promotion_allowed=false。

        generate_evidence() 默认 evidence_mode=dry_run,summary 必须包含:
            - evidence_mode == "dry_run"
            - production_promotion_allowed == False
        且严禁出现 "production passed" 等通过性断言。
        """
        module = _load_evidence_module()

        # 用 mock 避免实际运行证据脚本(_run_evidence 返回 passed 即可)
        def mock_run(*args, **kwargs):
            return {
                "evidence_type": "supply_chain",
                "status": "passed",
                "exit_code": 0,
                "duration_seconds": 1.0,
                "report_path": None,
                "report_size_bytes": 0,
                "error": None,
            }

        from unittest.mock import patch
        with patch.object(module, "_run_evidence", side_effect=mock_run):
            import asyncio
            summary = asyncio.run(module.generate_evidence(
                evidence_types=["supply_chain"],
                output_dir=tmp_path,
                dry_run=True,
                extra_args_map={},
                # 默认 evidence_mode=dry_run
            ))

        # 必须显式标记 evidence_mode="dry_run"
        assert summary.get("evidence_mode") == "dry_run", (
            "R65 P0-04: dry_run 模式必须输出 evidence_mode='dry_run'"
        )
        # production_promotion_allowed 必须为 False
        assert summary.get("production_promotion_allowed") is False, (
            "R65 P0-04: dry_run 模式 production_promotion_allowed 必须为 False"
        )
        # flags.skip 字段存在
        assert "flags" in summary, "R65 P0-04: summary 必须含 flags 字段"
        assert "skip" in summary["flags"], "flags 必须含 skip 子字段"
        assert "dry_run" in summary["flags"], "flags 必须含 dry_run 子字段"
        # dry_run 模式下生成的 dry_run 副本文件必须存在(文件名含 dry_run + commit)
        dry_run_files = list(tmp_path.glob("production_evidence_dry_run_*.json"))
        assert dry_run_files, (
            "R65 P0-04: dry_run 模式必须生成 production_evidence_dry_run_<commit>.json 副本"
        )
        # 副本文件内容也必须含 evidence_mode="dry_run"
        copy_data = json.loads(dry_run_files[0].read_text(encoding="utf-8"))
        assert copy_data.get("evidence_mode") == "dry_run"
        assert copy_data.get("production_promotion_allowed") is False


# ════════════════════════════════════════════════════════════════
# 2. verify_production_promotion — 拒绝场景
# ════════════════════════════════════════════════════════════════


class TestVerifyProductionPromotionRejects:
    """R65 P0-04: verify_production_promotion() 拒绝各种非法证据。"""

    def test_rejects_dry_run_evidence(self, tmp_path):
        """场景 2:拒绝 dry_run 证据(evidence_mode != "production")。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["evidence_mode"] = "dry_run"  # 改为 dry_run
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)

        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT, (
            "R65 P0-04: dry_run 证据必须抛 PRODUCTION_EVIDENCE_INSUFFICIENT"
        )
        # params 必须含 reason + missing
        params = exc_info.value.params
        assert "reason" in params, "params 必须含 reason"
        assert "missing" in params, "params 必须含 missing"
        # missing 应包含 EVIDENCE_MODE_PRODUCTION 标记
        assert "EVIDENCE_MODE_PRODUCTION" in params["missing"], (
            "missing 应包含 EVIDENCE_MODE_PRODUCTION 标记"
        )

    def test_rejects_unsigned_evidence(self, tmp_path):
        """场景 3:拒绝未签名证据(顶层 signature 块缺失或未 verified)。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        # 3a: signature 块完全缺失
        evidence = _make_valid_production_evidence()
        del evidence["signature"]
        path = _write_evidence(tmp_path, evidence, name="unsigned_1.json")

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "FILE_SIGNATURE" in exc_info.value.params["missing"]

        # 3b: signature.verified 不为 true
        evidence2 = _make_valid_production_evidence()
        evidence2["signature"]["verified"] = False
        path2 = _write_evidence(tmp_path, evidence2, name="unsigned_2.json")

        with pytest.raises(AppError) as exc_info2:
            module.verify_production_promotion(path2)
        assert exc_info2.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "FILE_SIGNATURE_VERIFIED" in exc_info2.value.params["missing"]

        # 3c: signature.method 不在 (cosign, gpg)
        evidence3 = _make_valid_production_evidence()
        evidence3["signature"]["method"] = "xor"
        path3 = _write_evidence(tmp_path, evidence3, name="unsigned_3.json")

        with pytest.raises(AppError) as exc_info3:
            module.verify_production_promotion(path3)
        assert exc_info3.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "FILE_SIGNATURE_METHOD" in exc_info3.value.params["missing"]

    def test_rejects_expired_evidence(self, tmp_path):
        """场景 4:拒绝已过期证据(expires_at < now)。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        # 将 SOAK_7DAY 的 expires_at 改为过去
        for art in evidence["artifacts"]:
            if art["artifact_type"] == "SOAK_7DAY":
                art["expires_at"] = _past_iso(days=1)
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        # SOAK_7DAY 应在 missing 中(因过期)
        assert "SOAK_7DAY" in exc_info.value.params["missing"], (
            "过期的 SOAK_7DAY 应在 missing 列表中"
        )

    def test_rejects_missing_soak_7day(self, tmp_path):
        """场景 5:拒绝缺失 SOAK_7DAY artifact。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["artifacts"] = [
            a for a in evidence["artifacts"] if a["artifact_type"] != "SOAK_7DAY"
        ]
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "SOAK_7DAY" in exc_info.value.params["missing"], (
            "missing 必须包含缺失的 SOAK_7DAY"
        )

    def test_rejects_missing_restore_3x(self, tmp_path):
        """场景 6:拒绝缺失 RESTORE_3X artifact。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["artifacts"] = [
            a for a in evidence["artifacts"] if a["artifact_type"] != "RESTORE_3X"
        ]
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "RESTORE_3X" in exc_info.value.params["missing"]

    def test_rejects_missing_outbox_fault_injection(self, tmp_path):
        """场景 7:拒绝缺失 OUTBOX_FAULT_INJECTION artifact。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["artifacts"] = [
            a for a in evidence["artifacts"]
            if a["artifact_type"] != "OUTBOX_FAULT_INJECTION"
        ]
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "OUTBOX_FAULT_INJECTION" in exc_info.value.params["missing"]

    def test_rejects_missing_ru_72h(self, tmp_path):
        """场景 8:拒绝缺失 RU_72H artifact。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["artifacts"] = [
            a for a in evidence["artifacts"] if a["artifact_type"] != "RU_72H"
        ]
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "RU_72H" in exc_info.value.params["missing"]

    def test_rejects_missing_supply_chain(self, tmp_path):
        """场景 9:拒绝缺失 SUPPLY_CHAIN artifact。"""
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["artifacts"] = [
            a for a in evidence["artifacts"] if a["artifact_type"] != "SUPPLY_CHAIN"
        ]
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "SUPPLY_CHAIN" in exc_info.value.params["missing"]

    def test_rejects_skip_flag(self, tmp_path):
        """场景 10:拒绝使用 --skip 标志(flags.skip 非空)。

        R65 P0-04: production promotion 禁止使用 --skip,
        即使 5 类 artifact 齐全,只要 flags.skip 非空也必须拒绝。
        """
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        evidence = _make_valid_production_evidence()
        evidence["flags"]["skip"] = ["soak"]  # 标记使用了 --skip soak
        path = _write_evidence(tmp_path, evidence)

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        assert "NO_SKIP_FLAG" in exc_info.value.params["missing"], (
            "使用 --skip 必须在 missing 中标记 NO_SKIP_FLAG"
        )


# ════════════════════════════════════════════════════════════════
# 3. verify_production_promotion — 接受场景
# ════════════════════════════════════════════════════════════════


class TestVerifyProductionPromotionAccepts:
    """R65 P0-04: verify_production_promotion() 接受合法证据。"""

    def test_accepts_complete_signed_unexpired_evidence(self, tmp_path):
        """场景 11:接受完整、已签名、未过期的 production 证据。

        证据满足全部条件:
        - evidence_mode="production"
        - signature.method="cosign", signature.verified=true
        - 5 类 artifact 齐全(均未过期)
        - 每个 artifact 字段完整
        - flags.skip 为空
        """
        module = _load_evidence_module()

        evidence = _make_valid_production_evidence()
        path = _write_evidence(tmp_path, evidence)

        # 不抛异常,返回 evidence dict
        result = module.verify_production_promotion(path)

        # 返回的 evidence 应标记 production_promotion_allowed=True
        assert result.get("production_promotion_allowed") is True, (
            "R65 P0-04: 校验通过的 evidence 必须标记 production_promotion_allowed=True"
        )
        # evidence_mode 仍为 production
        assert result.get("evidence_mode") == "production"

    def test_each_artifact_has_all_required_fields(self, tmp_path):
        """场景 12:每个 artifact 含全部必需字段。

        REQUIRED_ARTIFACT_FIELDS(15 个,R67 P1-11 新增 4 个防重放字段):
            artifact_type / environment_id / commit_sha / image_digest /
            started_at / ended_at / expires_at / raw_data_digest /
            executed_by / approved_by / signature
            + nonce / attestation_digest / time_window / consumed  # R67 P1-11

        本测试同时验证:
        - 模块暴露 REQUIRED_ARTIFACT_FIELDS 常量
        - 合法 fixture 中每个 artifact 都含全部字段
        - 任一字段缺失会被 verify_production_promotion 检测到
        """
        module = _load_evidence_module()
        from services.error_codes import AppError, ErrorCodes

        # 12a: 模块必须暴露 REQUIRED_ARTIFACT_FIELDS 常量
        assert hasattr(module, "REQUIRED_ARTIFACT_FIELDS"), (
            "generate_production_evidence 必须暴露 REQUIRED_ARTIFACT_FIELDS 常量"
        )
        required = module.REQUIRED_ARTIFACT_FIELDS
        expected_fields = {
            "artifact_type", "environment_id", "commit_sha", "image_digest",
            "started_at", "ended_at", "expires_at", "raw_data_digest",
            "executed_by", "approved_by", "signature",
            # R67 P1-11: 防重放字段
            "nonce", "attestation_digest", "time_window", "consumed",
        }
        assert set(required) == expected_fields, (
            f"REQUIRED_ARTIFACT_FIELDS 必须含全部 15 个字段,实际: {set(required)}"
        )
        # 必须有 15 个字段
        assert len(required) == 15, (
            f"REQUIRED_ARTIFACT_FIELDS 应有 15 个字段,实际 {len(required)} 个"
        )

        # 12b: 合法 fixture 每个 artifact 含全部字段
        evidence = _make_valid_production_evidence()
        for art in evidence["artifacts"]:
            for field in required:
                assert field in art, (
                    f"fixture 中 {art['artifact_type']} 缺少字段 {field}"
                )

        # 12c: 删除任一字段会被检测到(以 SOAK_7DAY 的 signature 字段为例)
        evidence_bad = _make_valid_production_evidence()
        for art in evidence_bad["artifacts"]:
            if art["artifact_type"] == "SOAK_7DAY":
                del art["signature"]  # 删除必需字段
        path = _write_evidence(tmp_path, evidence_bad, name="missing_field.json")

        with pytest.raises(AppError) as exc_info:
            module.verify_production_promotion(path)
        assert exc_info.value.code == ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT
        # SOAK_7DAY 应在 missing 中(因缺字段)
        assert "SOAK_7DAY" in exc_info.value.params["missing"], (
            "缺字段的 artifact 应在 missing 中标记"
        )


# ════════════════════════════════════════════════════════════════
# 4. 错误码注册验证(辅助测试)
# ════════════════════════════════════════════════════════════════


class TestErrorCodeRegistration:
    """R65 P0-04: PRODUCTION_EVIDENCE_INSUFFICIENT 错误码注册正确。"""

    def test_error_code_registered_with_correct_attributes(self):
        """错误码必须以 http=412 / critical / safe_params=[reason, missing] 注册。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        assert ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT == \
            "PRODUCTION.EVIDENCE.INSUFFICIENT"

        definition = ErrorRegistry.get(ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT)
        assert definition is not None, (
            "PRODUCTION_EVIDENCE_INSUFFICIENT 必须在 ErrorRegistry 中注册"
        )
        assert definition.http_status == 412, (
            f"http_status 应为 412,实际 {definition.http_status}"
        )
        assert definition.severity == "critical", (
            f"severity 应为 critical,实际 {definition.severity}"
        )
        assert definition.retryable is False, (
            "production evidence 不足不可重试(必须重新采集证据)"
        )
        assert "reason" in definition.safe_params, (
            "safe_params 必须包含 reason"
        )
        assert "missing" in definition.safe_params, (
            "safe_params 必须包含 missing"
        )
        # R61 P1-05: presentation / show_retry_button / audit_level 必填
        assert definition.presentation is not None
        assert definition.show_retry_button is False
        assert definition.audit_level == "critical"

    def test_error_code_i18n_messages_render(self):
        """错误码 i18n 消息在 zh-CN / en-US 下均可正确渲染。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # zh-CN
        env_zh = ErrorRegistry.create_envelope(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={"reason": "missing", "missing": "SOAK_7DAY"},
            locale="zh-CN",
        )
        assert "生产证据不足" in env_zh.message, (
            f"zh-CN 消息应含 '生产证据不足',实际: {env_zh.message}"
        )
        assert "SOAK_7DAY" in env_zh.message

        # en-US
        env_en = ErrorRegistry.create_envelope(
            ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
            params={"reason": "missing", "missing": "SOAK_7DAY"},
            locale="en-US",
        )
        assert "Production evidence insufficient" in env_en.message, (
            f"en-US 消息应含 'Production evidence insufficient',实际: {env_en.message}"
        )
        assert "SOAK_7DAY" in env_en.message


# ════════════════════════════════════════════════════════════════
# 5. release-gates.yml workflow 验证
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesWorkflow:
    """R65 P0-04: release-gates.yml 含 production-promotion-gate job。"""

    @pytest.fixture
    def workflow_path(self):
        return REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

    @pytest.fixture
    def workflow_content(self, workflow_path):
        return workflow_path.read_text(encoding="utf-8")

    @pytest.fixture
    def workflow_yaml(self, workflow_path):
        import yaml
        return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    def test_workflow_triggers_on_version_tags(self, workflow_yaml):
        """workflow 必须在 rc-v*/production-v* tag push 时触发。

        R70 P0-10: master/staging/RC/production 命名空间完全分离。
        旧的 v*.*.* tag 已废弃,改为 rc-v*(RC candidate)+ production-v*(production)。
        """
        push_config = workflow_yaml.get(True, {})  # YAML "on:" 被解析为 True
        # on: 可能被解析为 True(Python yaml 怪癖)
        if push_config is True or not isinstance(push_config, dict):
            # 检查 raw content
            content = (REPO_ROOT / ".github" / "workflows" / "release-gates.yml").read_text()
            assert "tags:" in content, "workflow 必须配置 tags 触发器"
            assert "rc-v*" in content, "workflow 必须以 rc-v* tag 模式触发(RC candidate)"
            assert "production-v*" in content, "workflow 必须以 production-v* tag 模式触发(production)"
            return
        push_dict = push_config.get("push", {})
        assert "tags" in push_dict, "workflow push 触发器必须含 tags"
        tags = push_dict["tags"]
        assert "rc-v*" in tags, "workflow 必须以 rc-v* tag 模式触发(RC candidate)"
        assert "production-v*" in tags, "workflow 必须以 production-v* tag 模式触发(production)"

    def test_workflow_has_production_promotion_gate_job(self, workflow_yaml):
        """workflow 必须含 production-promotion-gate job。"""
        jobs = workflow_yaml.get("jobs", {})
        assert "production-promotion-gate" in jobs, (
            "R65 P0-04: release-gates.yml 必须含 production-promotion-gate job"
        )

    def test_production_promotion_gate_runs_only_on_version_tags(self, workflow_yaml):
        """production-promotion-gate 必须仅在 production-v* tag 或 workflow_dispatch 上运行。

        R70 P0-10: production 部署通过 production-v* tag 或 workflow_dispatch 触发
        (配合 R71 P0-11 RC 身份核验输入)。旧的 v*.*.* tag 已废弃。
        """
        job = workflow_yaml["jobs"]["production-promotion-gate"]
        if_cond = job.get("if", "")
        assert "startsWith(github.ref" in if_cond, (
            f"production-promotion-gate 的 if 必须检查 github.ref tag,实际: {if_cond}"
        )
        assert "refs/tags/production-v" in if_cond, (
            f"production-promotion-gate 的 if 必须匹配 refs/tags/production-v* ,实际: {if_cond}"
        )

    def test_production_promotion_gate_needs_production_evidence(self, workflow_yaml):
        """production-promotion-gate 必须依赖 production-evidence job。"""
        job = workflow_yaml["jobs"]["production-promotion-gate"]
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "production-evidence" in needs, (
            "production-promotion-gate 必须依赖 production-evidence job"
        )

    def test_production_promotion_gate_calls_verify_promotion(self, workflow_content):
        """production-promotion-gate 步骤必须调用 verify_production_promotion()。

        通过 `python scripts/generate_production_evidence.py --verify-promotion`
        间接调用 verify_production_promotion 函数。
        """
        assert "production-promotion-gate" in workflow_content
        assert "--verify-promotion" in workflow_content, (
            "production-promotion-gate 必须通过 --verify-promotion 调用 verify_production_promotion"
        )

    def test_release_summary_includes_production_promotion_gate(self, workflow_yaml):
        """release-summary 必须依赖 production-promotion-gate job。"""
        rs = workflow_yaml["jobs"]["release-summary"]
        needs = rs.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "production-promotion-gate" in needs, (
            "release-summary 必须依赖 production-promotion-gate,以聚合 promotion 状态"
        )

    def test_release_summary_reports_production_promotion_allowed(self, workflow_content):
        """release-summary 必须输出 production_promotion_allowed: true/false。"""
        # 在 release-summary 步骤中查找 production_promotion_allowed 输出
        assert "production_promotion_allowed" in workflow_content, (
            "release-summary 必须在输出中报告 production_promotion_allowed 标记"
        )

    def test_production_evidence_job_continues_for_prs(self, workflow_yaml):
        """production-evidence job 必须在 PR/push 时继续运行(生成 dry-run)。"""
        pe = workflow_yaml["jobs"].get("production-evidence", {})
        # 必须有 if: always()(PR/push 都运行)
        if_cond = pe.get("if", "")
        assert "always()" in if_cond, (
            "production-evidence 必须以 if: always() 在 PR/push 上继续运行"
        )
