"""R65 P1-09: CRDB RU 72h 真实数据 + SQL fingerprint/service/job 归因 — 测试套件。

测试覆盖范围(9 个场景):
    1. 数据文件 schema 校验(validate_data_schema)
    2. 72h 跨度校验(check_72h_span) — 拒绝 < 72h
    3. 签名校验(verify_signature) — 拒绝未签名 / HMAC 不匹配
    4. fingerprint 归因解析(parse_fingerprint_attribution)
    5. RU 阈值检查(check_thresholds) — bot=0/cluster ≤100/... 等
    6. --strict 模式(WARN 也失败)
    7. --dry-run 模式(无数据,仅校验脚本)
    8. 聚合计算(compute_aggregates) — by_service / top 10 fingerprints
    9. 退出码(0 on success, 1 on fail)

被测代码引用:
    - scripts/check_crdb_ru_72h_attribution.py — 72h 数据校验脚本
    - services/crdb_ru_collector.py — collector(SQL fingerprint 归因 + SQLite 表)

测试策略:
    - 所有测试不依赖真实 CRDB/Redis(使用 mock + 临时 SQLite DB)
    - 数据文件使用合成 fixture(HMAC 签名,不依赖 cosign/GPG)
    - 退出码测试通过 subprocess.run 执行脚本
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 顶部 mock telegram 模块(避免 import 失败,与现有测试一致)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# 导入被测模块
from scripts.check_crdb_ru_72h_attribution import (  # type: ignore  # noqa: E402
    BOT_RU_PER_DAY_LIMIT,
    CLUSTER_BLOCK_RU_PER_DAY,
    CLUSTER_HARD_CAP_RU_PER_DAY,
    CLUSTER_IDEAL_RU_PER_DAY,
    DEFAULT_HMAC_SECRET,
    MONTHLY_RU_LIMIT,
    RU_PER_DAU_DAY_LIMIT,
    ValidationError,
    check_72h_span,
    check_per_hour_samples,
    check_thresholds,
    compute_aggregates,
    main as script_main,
    parse_fingerprint_attribution,
    validate_data_schema,
    verify_signature,
)


# ════════════════════════════════════════════════════════════════
# 测试常量(与脚本中的阈值常量对齐)
# ════════════════════════════════════════════════════════════════


SCRIPT_PATH = REPO_ROOT / "scripts" / "check_crdb_ru_72h_attribution.py"
COLLECTOR_PATH = REPO_ROOT / "services" / "crdb_ru_collector.py"


# ════════════════════════════════════════════════════════════════
# Fixture:合成 72h 数据文件(HMAC 签名)
# ════════════════════════════════════════════════════════════════


def _make_sample(sampled_at: str, total_ru: float = 5.0,
                 by_service: dict | None = None,
                 by_fingerprint: list | None = None) -> dict:
    """构造一个采样点 fixture。"""
    if by_service is None:
        by_service = {"admin": 2.0, "api": 2.0, "scheduler": 1.0, "bot": 0.0}
    if by_fingerprint is None:
        by_fingerprint = [
            {
                "fingerprint_sha256": hashlib.sha256(b"select 1").hexdigest(),
                "ru": 2.0,
                "service": "admin",
                "job": "user_list",
                "query_text_sample": "SELECT * FROM users WHERE id = 1",
            },
            {
                "fingerprint_sha256": hashlib.sha256(b"select 2").hexdigest(),
                "ru": 2.0,
                "service": "api",
                "job": "api_handler",
                "query_text_sample": "SELECT * FROM files WHERE id = 2",
            },
            {
                "fingerprint_sha256": hashlib.sha256(b"select 3").hexdigest(),
                "ru": 1.0,
                "service": "scheduler",
                "job": "cron_tick",
                "query_text_sample": "SELECT 1",
            },
        ]
    return {
        "sampled_at": sampled_at,
        "window_seconds": 3600,
        "total_ru": total_ru,
        "by_service": by_service,
        "by_fingerprint": by_fingerprint,
    }


def _make_72h_data(
    *,
    started_at: str = "2026-07-12T00:00:00Z",
    duration_hours: int = 72,
    total_ru_per_hour: float = 5.0,
    bot_ru_per_hour: float = 0.0,
    include_signature: bool = True,
    signature_type: str = "hmac",
    include_by_fingerprint: bool = True,
    sample_count: int | None = None,
) -> dict:
    """构造合成 72h 数据文件 fixture。

    Args:
        started_at: 起始时间(ISO 8601)
        duration_hours: 跨度(默认 72)
        total_ru_per_hour: 每小时总 RU
        bot_ru_per_hour: 每小时 bot RU(应 = 0)
        include_signature: 是否包含 signature 字段
        signature_type: 签名类型
        include_by_fingerprint: 是否包含 by_fingerprint 字段
        sample_count: 采样点数(默认 = duration_hours)
    """
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(hours=duration_hours)
    ended_at = end_dt.isoformat().replace("+00:00", "Z")

    if sample_count is None:
        sample_count = duration_hours

    samples = []
    for i in range(sample_count):
        ts = (start_dt + timedelta(hours=i)).isoformat().replace("+00:00", "Z")
        by_service = {
            "admin": total_ru_per_hour * 0.4,
            "api": total_ru_per_hour * 0.4,
            "scheduler": total_ru_per_hour * 0.2,
            "bot": bot_ru_per_hour,
        }
        by_fp = []
        if include_by_fingerprint:
            by_fp = [
                {
                    "fingerprint_sha256": hashlib.sha256(b"select 1").hexdigest(),
                    "ru": total_ru_per_hour * 0.4,
                    "service": "admin",
                    "job": "user_list",
                    "query_text_sample": "SELECT * FROM users WHERE id = 1",
                },
                {
                    "fingerprint_sha256": hashlib.sha256(b"select 2").hexdigest(),
                    "ru": total_ru_per_hour * 0.4,
                    "service": "api",
                    "job": "api_handler",
                    "query_text_sample": "SELECT * FROM files WHERE id = 2",
                },
                {
                    "fingerprint_sha256": hashlib.sha256(b"select 3").hexdigest(),
                    "ru": total_ru_per_hour * 0.2,
                    "service": "scheduler",
                    "job": "cron_tick",
                    "query_text_sample": "SELECT 1",
                },
            ]
        samples.append(_make_sample(
            sampled_at=ts,
            total_ru=total_ru_per_hour,
            by_service=by_service,
            by_fingerprint=by_fp,
        ))

    data: dict = {
        "environment_id": "prod-test-env-001",
        "commit_sha": "abc123def456789012345678901234567890abcd",
        "image_digest": "sha256:testdigest" + "a" * 52,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_hours": duration_hours,
        "executed_by": "ci-robot",
        "approved_by": "release-manager",
        "samples": samples,
        "signature_type": signature_type,
    }
    if include_signature:
        # 计算 HMAC 签名(基于 canonicalized JSON 字节)
        raw_bytes = _canonicalize_for_signature(data)
        secret = os.environ.get(
            "CRDB_RU_72H_HMAC_SECRET", DEFAULT_HMAC_SECRET
        ).encode("utf-8")
        data["signature"] = hmac.new(secret, raw_bytes, hashlib.sha256).hexdigest()
    else:
        data["signature"] = ""
    return data


def _canonicalize_for_signature(data: dict) -> bytes:
    """计算签名时使用的 canonical 字节(signature 字段移除后)。

    与脚本中 verify_signature 一致:对原始文件字节做 HMAC,
    因此测试中也对完整 dict (移除 signature 字段后)做 JSON canonicalize。
    """
    d = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _write_data_file(data: dict, path: Path) -> bytes:
    """将数据 dict 写入临时文件,返回写入的字节(用于 HMAC 校验)。

    注意:脚本读取的是文件原始字节做 HMAC,因此测试中文件内容必须
    与 verify_signature 计算的 HMAC 输入一致。
    """
    # 与 verify_signature 一致:对文件原始字节做 HMAC
    raw_bytes = json.dumps(data, sort_keys=False, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw_bytes)
    return raw_bytes


@pytest.fixture
def hmac_signed_data(tmp_path):
    """生成一份 HMAC 签名的 72h 数据文件 + 字节内容。

    注意:本 fixture 计算 HMAC 时使用的字节序列必须与
    verify_signature(data, raw_bytes, ...) 中的 canonical_bytes 一致:
        - 排除 signature 字段
        - sort_keys=True
        - ensure_ascii=False
    """
    # 1. 构造数据(不含 signature 字段)
    data_template = _make_72h_data(include_signature=False)
    # 2. 用 canonical 字节计算 HMAC(与脚本 verify_signature 一致)
    canonical_bytes = _canonicalize_for_signature(data_template)
    secret = os.environ.get(
        "CRDB_RU_72H_HMAC_SECRET", DEFAULT_HMAC_SECRET
    ).encode("utf-8")
    sig = hmac.new(secret, canonical_bytes, hashlib.sha256).hexdigest()
    data_template["signature"] = sig
    # 3. 写入文件(包含 signature 字段)
    #    verify_signature 内部会重新 canonicalize 移除 signature 字段,
    #    所以文件序列化方式不影响 HMAC 校验结果。
    final_bytes = json.dumps(
        data_template, sort_keys=False, ensure_ascii=False
    ).encode("utf-8")
    data_file = tmp_path / "ru_72h_data.json"
    data_file.write_bytes(final_bytes)
    return {
        "path": data_file,
        "data": data_template,
        "raw_bytes": final_bytes,
    }


# ════════════════════════════════════════════════════════════════
# 场景 1:数据文件 schema 校验
# ════════════════════════════════════════════════════════════════


class TestSchemaValidation:
    """场景 1:数据文件 schema 校验。"""

    def test_valid_data_passes_schema(self, hmac_signed_data):
        """合法的 72h 数据文件应通过 schema 校验。"""
        # 不强制签名(测试本身已签名)
        validate_data_schema(hmac_signed_data["data"], require_signature=True)

    def test_missing_top_field_rejected(self, hmac_signed_data):
        """缺少顶层必需字段应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        del data["environment_id"]
        with pytest.raises(ValidationError, match="environment_id"):
            validate_data_schema(data, require_signature=True)

    def test_empty_environment_id_rejected(self, hmac_signed_data):
        """空 environment_id 应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        data["environment_id"] = ""
        with pytest.raises(ValidationError, match="environment_id"):
            validate_data_schema(data, require_signature=True)

    def test_invalid_started_at_rejected(self, hmac_signed_data):
        """非法 ISO 8601 started_at 应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        data["started_at"] = "not-a-date"
        with pytest.raises(ValidationError, match="started_at"):
            validate_data_schema(data, require_signature=True)

    def test_invalid_duration_hours_rejected(self, hmac_signed_data):
        """负数 duration_hours 应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        data["duration_hours"] = -1
        with pytest.raises(ValidationError, match="duration_hours"):
            validate_data_schema(data, require_signature=True)

    def test_empty_signature_rejected_when_required(self, hmac_signed_data):
        """require_signature=True 时空 signature 应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        data["signature"] = ""
        with pytest.raises(ValidationError, match="signature"):
            validate_data_schema(data, require_signature=True)

    def test_empty_signature_allowed_when_not_required(self, hmac_signed_data):
        """require_signature=False 时空 signature 允许通过。"""
        data = dict(hmac_signed_data["data"])
        data["signature"] = ""
        validate_data_schema(data, require_signature=False)

    def test_empty_samples_rejected(self, hmac_signed_data):
        """空 samples 数组应被拒绝。"""
        data = dict(hmac_signed_data["data"])
        data["samples"] = []
        with pytest.raises(ValidationError, match="samples"):
            validate_data_schema(data, require_signature=True)

    def test_sample_missing_field_rejected(self, hmac_signed_data):
        """sample 缺少必需字段应被拒绝。"""
        data = hmac_signed_data["data"]
        bad_sample = dict(data["samples"][0])
        del bad_sample["total_ru"]
        data["samples"][0] = bad_sample
        with pytest.raises(ValidationError, match="total_ru"):
            validate_data_schema(data, require_signature=True)

    def test_fingerprint_missing_field_rejected(self, hmac_signed_data):
        """by_fingerprint 元素缺少必需字段应被拒绝。"""
        data = hmac_signed_data["data"]
        bad_fp = dict(data["samples"][0]["by_fingerprint"][0])
        del bad_fp["service"]
        data["samples"][0]["by_fingerprint"][0] = bad_fp
        with pytest.raises(ValidationError, match="service"):
            validate_data_schema(data, require_signature=True)

    def test_negative_ru_rejected(self, hmac_signed_data):
        """负数 total_ru 应被拒绝。"""
        data = hmac_signed_data["data"]
        bad_sample = dict(data["samples"][0])
        bad_sample["total_ru"] = -1.0
        data["samples"][0] = bad_sample
        with pytest.raises(ValidationError, match="total_ru"):
            validate_data_schema(data, require_signature=True)

    def test_non_dict_root_rejected(self):
        """非 dict 根对象应被拒绝。"""
        with pytest.raises(ValidationError, match="JSON object"):
            validate_data_schema("not-a-dict", require_signature=True)
        with pytest.raises(ValidationError, match="JSON object"):
            validate_data_schema([], require_signature=True)


# ════════════════════════════════════════════════════════════════
# 场景 2:72h 跨度校验(拒绝 < 72h)
# ════════════════════════════════════════════════════════════════


class Test72hSpanCheck:
    """场景 2:72h 跨度校验。"""

    def test_exactly_72h_passes(self):
        """正好 72 小时应通过。"""
        data = _make_72h_data(duration_hours=72)
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is True
        assert abs(result["actual_hours"] - 72.0) < 0.1

    def test_more_than_72h_passes(self):
        """超过 72 小时应通过。"""
        data = _make_72h_data(duration_hours=80)
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is True
        assert result["actual_hours"] >= 80.0

    def test_less_than_72h_fails(self):
        """小于 72 小时应失败。"""
        data = _make_72h_data(duration_hours=48)
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is False
        assert "72" in result["error"]

    def test_71h_fails(self):
        """71 小时也应失败(边界检查)。"""
        data = _make_72h_data(duration_hours=71)
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is False

    def test_declared_duration_mismatch_fails(self):
        """duration_hours 与实际跨度不一致应失败。"""
        data = _make_72h_data(duration_hours=72)
        # 篡改 duration_hours(但 started_at/ended_at 仍为 72h)
        data["duration_hours"] = 100
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is False
        assert "duration_hours" in result["error"] or "不一致" in result["error"]

    def test_reversed_timestamps_fails(self):
        """started_at > ended_at 应失败。"""
        data = _make_72h_data(duration_hours=72)
        data["started_at"], data["ended_at"] = data["ended_at"], data["started_at"]
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is False

    def test_invalid_timestamp_fails(self):
        """非法时间戳应失败。"""
        data = _make_72h_data(duration_hours=72)
        data["started_at"] = "not-a-date"
        result = check_72h_span(data, min_hours=72.0)
        assert result["passed"] is False


# ════════════════════════════════════════════════════════════════
# 场景 3:签名校验(拒绝未签名)
# ════════════════════════════════════════════════════════════════


class TestSignatureVerification:
    """场景 3:签名校验。"""

    def test_valid_hmac_passes(self, hmac_signed_data):
        """合法 HMAC 签名应通过。"""
        result = verify_signature(
            hmac_signed_data["data"],
            hmac_signed_data["raw_bytes"],
            strict=False,
        )
        assert result["status"] == "PASS"
        assert result["verified"] is True

    def test_unsigned_data_fails(self, hmac_signed_data):
        """未签名(signature 为空)应 FAIL。"""
        data = dict(hmac_signed_data["data"])
        data["signature"] = ""
        result = verify_signature(data, hmac_signed_data["raw_bytes"], strict=False)
        assert result["status"] == "FAIL"
        assert "未签名" in result["error"] or "为空" in result["error"]

    def test_unsigned_data_fails_strict(self, hmac_signed_data):
        """未签名在 strict 模式下也应 FAIL。"""
        data = dict(hmac_signed_data["data"])
        data["signature"] = ""
        result = verify_signature(data, hmac_signed_data["raw_bytes"], strict=True)
        assert result["status"] == "FAIL"

    def test_tampered_data_fails_hmac(self, hmac_signed_data):
        """篡改数据后 HMAC 不匹配应 FAIL。"""
        data = hmac_signed_data["data"]
        # 篡改某个 sample 的 RU(签名已不匹配)
        data["samples"][0]["total_ru"] = 999.0
        result = verify_signature(
            data, hmac_signed_data["raw_bytes"], strict=False
        )
        assert result["status"] == "FAIL"
        assert "HMAC" in result["error"] or "不匹配" in result["error"]

    def test_wrong_secret_fails(self, hmac_signed_data, monkeypatch):
        """错误 secret 应使 HMAC 验证失败。"""
        monkeypatch.setenv("CRDB_RU_72H_HMAC_SECRET", "wrong-secret")
        result = verify_signature(
            hmac_signed_data["data"],
            hmac_signed_data["raw_bytes"],
            strict=False,
        )
        assert result["status"] == "FAIL"

    def test_unknown_signature_type_warns_non_strict(self, hmac_signed_data):
        """未知 signature_type 在非 strict 模式下应 WARN(不阻断)。"""
        data = dict(hmac_signed_data["data"])
        data["signature_type"] = "unknown-type"
        # signature 字段保持非空(否则会先因签名空 FAIL)
        data["signature"] = "a" * 64
        result = verify_signature(data, b"raw-bytes", strict=False)
        assert result["status"] == "WARN"

    def test_unknown_signature_type_fails_strict(self, hmac_signed_data):
        """未知 signature_type 在 strict 模式下应 FAIL。"""
        data = dict(hmac_signed_data["data"])
        data["signature_type"] = "unknown-type"
        data["signature"] = "a" * 64
        result = verify_signature(data, b"raw-bytes", strict=True)
        assert result["status"] == "FAIL"

    def test_cosign_signature_with_invalid_format_fails(self, hmac_signed_data):
        """cosign 签名但 signature 格式非法应 FAIL。"""
        data = dict(hmac_signed_data["data"])
        data["signature_type"] = "cosign"
        data["signature"] = "!!!invalid-format!!!"  # 非 base64/hex
        result = verify_signature(data, b"raw-bytes", strict=False)
        assert result["status"] == "FAIL"
        assert "格式" in result["error"] or "format" in result["error"].lower()


# ════════════════════════════════════════════════════════════════
# 场景 4:fingerprint 归因解析
# ════════════════════════════════════════════════════════════════


class TestFingerprintAttributionParsing:
    """场景 4:fingerprint 归因解析。"""

    def test_parse_basic_attribution(self, hmac_signed_data):
        """应正确解析每个 sample 的 fingerprint 归因。"""
        result = parse_fingerprint_attribution(hmac_signed_data["data"])
        assert result["samples_total"] == 72
        assert result["samples_with_attribution"] == 72
        # 3 个不同 fingerprint(admin/api/scheduler)
        assert result["total_fingerprints"] == 3
        assert result["total_attribution_entries"] == 72 * 3

    def test_by_service_aggregation(self, hmac_signed_data):
        """应按 service 正确聚合。"""
        result = parse_fingerprint_attribution(hmac_signed_data["data"])
        # admin + api 各占 40%,scheduler 占 20%
        assert "admin" in result["by_service"]
        assert "api" in result["by_service"]
        assert "scheduler" in result["by_service"]
        # 72 个 sample × 5 RU/h × 0.4 = 144 RU
        assert abs(result["by_service"]["admin"] - 144.0) < 0.1
        assert abs(result["by_service"]["api"] - 144.0) < 0.1
        assert abs(result["by_service"]["scheduler"] - 72.0) < 0.1

    def test_by_job_aggregation(self, hmac_signed_data):
        """应按 job 正确聚合。"""
        result = parse_fingerprint_attribution(hmac_signed_data["data"])
        assert "user_list" in result["by_job"]
        assert "api_handler" in result["by_job"]
        assert "cron_tick" in result["by_job"]

    def test_top_fingerprints_sorted_by_ru(self, hmac_signed_data):
        """top_fingerprints 应按 RU 降序排序(最多 10 个)。"""
        result = parse_fingerprint_attribution(hmac_signed_data["data"])
        top = result["top_fingerprints"]
        assert len(top) <= 10
        # 验证降序
        for i in range(len(top) - 1):
            assert top[i]["ru"] >= top[i + 1]["ru"]

    def test_empty_fingerprint_list(self):
        """sample 中 by_fingerprint 为空时应返回空归因。"""
        data = _make_72h_data(include_by_fingerprint=False)
        result = parse_fingerprint_attribution(data)
        assert result["total_fingerprints"] == 0
        assert result["total_attribution_entries"] == 0
        assert result["samples_with_attribution"] == 0

    def test_query_text_sample_preserved(self):
        """query_text_sample 字段应被保留。"""
        data = _make_72h_data()
        # 添加一个特殊 fingerprint 测试 query_text_sample 保留
        result = parse_fingerprint_attribution(data)
        for fp in result["fingerprints"]:
            assert "query_text_sample" in fp
            assert isinstance(fp["query_text_sample"], str)


# ════════════════════════════════════════════════════════════════
# 场景 5:RU 阈值检查
# ════════════════════════════════════════════════════════════════


class TestThresholdChecks:
    """场景 5:RU 阈值检查(bot=0/cluster ≤100 等)。"""

    def test_zero_bot_ru_passes(self):
        """bot role 0 RU/day 应 PASS。"""
        data = _make_72h_data(bot_ru_per_hour=0.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        bot_gate = next(g for g in gates if g["gate"] == "bot_ru_per_day")
        assert bot_gate["status"] == "PASS"
        assert float(bot_gate["actual"]) == 0.0

    def test_nonzero_bot_ru_fails(self):
        """bot role > 0 RU/day 应 FAIL。"""
        data = _make_72h_data(bot_ru_per_hour=1.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        bot_gate = next(g for g in gates if g["gate"] == "bot_ru_per_day")
        assert bot_gate["status"] == "FAIL"

    def test_cluster_ideal_passes_when_below_20(self):
        """cluster ≤ 20 RU/day 应 PASS。"""
        # 72h × 5 RU/h = 360 RU;360 / 3 days = 120 RU/day → 太高
        # 用 1 RU/h:72 / 3 = 24 → 仍 > 20;用 0.5 RU/h:36/3=12 → PASS
        data = _make_72h_data(total_ru_per_hour=0.5)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        ideal_gate = next(g for g in gates if g["gate"] == "cluster_ru_ideal")
        assert ideal_gate["status"] == "PASS"

    def test_cluster_ideal_warns_when_above_20_below_100(self):
        """cluster 20 < RU/day ≤ 100 应 WARN。"""
        # 24 RU/h × 72 = 1728;1728 / 3 = 576/day → 超 100 (FAIL)
        # 用 3 RU/h:216/3 = 72/day → 在 (20, 100] 区间 → WARN
        data = _make_72h_data(total_ru_per_hour=3.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        ideal_gate = next(g for g in gates if g["gate"] == "cluster_ru_ideal")
        hard_gate = next(g for g in gates if g["gate"] == "cluster_ru_hard_cap")
        assert ideal_gate["status"] == "WARN"
        assert hard_gate["status"] == "PASS"

    def test_cluster_hard_cap_fails_when_above_100(self):
        """cluster > 100 RU/day 应 FAIL。"""
        # 5 RU/h × 72 = 360;360/3 = 120/day → > 100 → FAIL
        data = _make_72h_data(total_ru_per_hour=5.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        hard_gate = next(g for g in gates if g["gate"] == "cluster_ru_hard_cap")
        assert hard_gate["status"] == "FAIL"

    def test_cluster_block_when_above_500(self):
        """cluster > 500 RU/day 应 BLOCK。"""
        # 22 RU/h × 72 = 1584;1584/3 = 528/day → > 500 → BLOCK
        data = _make_72h_data(total_ru_per_hour=22.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        block_gate = next(g for g in gates if g["gate"] == "cluster_ru_block")
        assert block_gate["status"] == "BLOCK"

    def test_ru_per_dau_skip_when_dau_zero(self):
        """DAU=0 时 RU/DAU 门禁应 SKIP。"""
        data = _make_72h_data()
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        dau_gate = next(g for g in gates if g["gate"] == "ru_per_dau_day")
        assert dau_gate["status"] == "SKIP"

    def test_ru_per_dau_passes_below_250(self):
        """RU/DAU ≤ 250 应 PASS。"""
        data = _make_72h_data(total_ru_per_hour=5.0)  # 120 RU/day
        aggs = compute_aggregates(data)
        # DAU=1 → 120/1 = 120 ≤ 250 → PASS
        gates = check_thresholds(aggs, dau=1)
        dau_gate = next(g for g in gates if g["gate"] == "ru_per_dau_day")
        assert dau_gate["status"] == "PASS"

    def test_ru_per_dau_fails_above_250(self):
        """RU/DAU > 250 应 FAIL。"""
        data = _make_72h_data(total_ru_per_hour=5.0)  # 120 RU/day
        aggs = compute_aggregates(data)
        # DAU=0.1 → 120/0.1 = 1200 > 250 → FAIL
        # 但 DAU 必须为整数,改用 DAU=1 + 更高的 RU
        data = _make_72h_data(total_ru_per_hour=10.0)  # 240 RU/day
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=1)  # 240/1 = 240 ≤ 250 → PASS
        # 用 total_ru_per_hour=15.0:360/1=360 > 250 → FAIL
        data = _make_72h_data(total_ru_per_hour=15.0)
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=1)
        dau_gate = next(g for g in gates if g["gate"] == "ru_per_dau_day")
        assert dau_gate["status"] == "FAIL"

    def test_monthly_limit_passes_below_35m(self):
        """月度估算 ≤ 35M 应 PASS。"""
        data = _make_72h_data(total_ru_per_hour=5.0)  # 120/day × 30 = 3600 ≤ 35M
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        monthly_gate = next(g for g in gates if g["gate"] == "monthly_ru_limit")
        assert monthly_gate["status"] == "PASS"

    def test_six_gates_returned(self):
        """应返回 6 项门禁。"""
        data = _make_72h_data()
        aggs = compute_aggregates(data)
        gates = check_thresholds(aggs, dau=0)
        assert len(gates) == 6

    def test_threshold_constants_correct(self):
        """阈值常量应与 R65 P1-09 规范一致。"""
        assert BOT_RU_PER_DAY_LIMIT == 0
        assert CLUSTER_IDEAL_RU_PER_DAY == 20
        assert CLUSTER_HARD_CAP_RU_PER_DAY == 100
        assert CLUSTER_BLOCK_RU_PER_DAY == 500
        assert RU_PER_DAU_DAY_LIMIT == 250
        assert MONTHLY_RU_LIMIT == 35_000_000


# ════════════════════════════════════════════════════════════════
# 场景 6:--strict 模式(WARN 也失败)
# ════════════════════════════════════════════════════════════════


class TestStrictMode:
    """场景 6:--strict 模式行为。"""

    def test_strict_mode_fails_on_warn(self, tmp_path):
        """--strict 模式下 WARN 应导致 exit 1。"""
        # 构造一个 cluster > 20 但 ≤ 100 的数据(WARN 但不 FAIL)
        # 3 RU/h × 72 = 216;216/3 = 72/day → (20, 100] → WARN
        data = _make_72h_data(
            total_ru_per_hour=3.0,
            bot_ru_per_hour=0.0,
            include_signature=True,
            signature_type="hmac",
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            [
                "python3", str(SCRIPT_PATH),
                "--strict", "--data", str(data_file),
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        # strict 模式下 WARN → exit 1
        assert result.returncode == 1, (
            f"--strict 下 WARN 应 exit 1,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_non_strict_mode_passes_on_warn(self, tmp_path):
        """非 strict 模式下 WARN 应 exit 0(允许通过)。"""
        data = _make_72h_data(
            total_ru_per_hour=3.0,
            bot_ru_per_hour=0.0,
            include_signature=True,
            signature_type="hmac",
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            [
                "python3", str(SCRIPT_PATH),
                "--data", str(data_file),
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"非 strict 模式 WARN 应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_strict_mode_passes_when_all_pass(self, tmp_path):
        """所有门禁 PASS 时 --strict 也应 exit 0。"""
        # 0.5 RU/h × 72 = 36;36/3 = 12/day → ≤ 20 → PASS
        data = _make_72h_data(
            total_ru_per_hour=0.5,
            bot_ru_per_hour=0.0,
            include_signature=True,
            signature_type="hmac",
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            [
                "python3", str(SCRIPT_PATH),
                "--strict", "--data", str(data_file),
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"--strict 下所有 PASS 应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )


# ════════════════════════════════════════════════════════════════
# 场景 7:--dry-run 模式(无数据,仅校验脚本)
# ════════════════════════════════════════════════════════════════


class TestDryRunMode:
    """场景 7:--dry-run 模式行为。"""

    def test_dry_run_exits_zero(self):
        """--dry-run 应 exit 0(无需数据文件)。"""
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"--dry-run 应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_dry_run_no_data_file_required(self, tmp_path):
        """--dry-run 不应要求 ru_72h_data.json 存在。"""
        # 在空目录中运行(确保无 ru_72h_data.json)
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0

    def test_dry_run_json_output(self):
        """--dry-run --json 应输出 JSON。"""
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run", "--json"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["mode"] == "dry-run"
        assert data["passed"] is True
        assert "thresholds" in data
        assert data["thresholds"]["bot_ru_per_day_limit"] == 0

    def test_dry_run_validates_threshold_constants(self):
        """--dry-run 应校验阈值常量。"""
        # 通过验证 dry-run 输出中包含所有阈值
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run", "--json"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        data = json.loads(result.stdout)
        t = data["thresholds"]
        assert t["bot_ru_per_day_limit"] == 0
        assert t["cluster_ideal_ru_per_day"] == 20
        assert t["cluster_hard_cap_ru_per_day"] == 100
        assert t["cluster_block_ru_per_day"] == 500
        assert t["ru_per_dau_day_limit"] == 250
        assert t["monthly_ru_limit"] == 35_000_000

    def test_dry_run_validates_script_self_consistency(self):
        """--dry-run 应验证脚本逻辑自洽(模块导入/函数调用)。"""
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        # 输出应包含自检通过标记
        assert "dry-run 通过" in result.stdout or "脚本逻辑自洽" in result.stdout


# ════════════════════════════════════════════════════════════════
# 场景 8:聚合计算(total by service / top 10 fingerprints)
# ════════════════════════════════════════════════════════════════


class TestAggregateComputation:
    """场景 8:聚合计算。"""

    def test_total_ru_computation(self):
        """应正确计算 72h 总 RU。"""
        # 5 RU/h × 72 = 360
        data = _make_72h_data(total_ru_per_hour=5.0)
        aggs = compute_aggregates(data)
        assert abs(aggs["total_ru"] - 360.0) < 0.1

    def test_daily_average_computation(self):
        """应正确计算日均 RU(总 / 3 天)。"""
        # 5 RU/h × 72 = 360;360 / 3 = 120
        data = _make_72h_data(total_ru_per_hour=5.0)
        aggs = compute_aggregates(data)
        assert abs(aggs["daily_average_ru"] - 120.0) < 0.1

    def test_peak_hourly_ru_computation(self):
        """应正确计算 peak 单窗口 RU。"""
        data = _make_72h_data(total_ru_per_hour=5.0)
        # 修改一个 sample 为更高的 RU
        data["samples"][10]["total_ru"] = 50.0
        aggs = compute_aggregates(data)
        assert aggs["peak_hourly_ru"] == 50.0

    def test_by_service_aggregation(self):
        """应按 service 正确聚合(by_service dict)。"""
        data = _make_72h_data(total_ru_per_hour=5.0)
        aggs = compute_aggregates(data)
        # admin + api 各 40% × 360 = 144;scheduler 20% × 360 = 72
        assert abs(aggs["by_service"]["admin"] - 144.0) < 0.1
        assert abs(aggs["by_service"]["api"] - 144.0) < 0.1
        assert abs(aggs["by_service"]["scheduler"] - 72.0) < 0.1

    def test_top_10_fingerprints_sorted(self):
        """top_fingerprints 应按 RU 降序,最多 10 个。"""
        # 构造 12 个不同 fingerprint(超过 10,验证截断)
        data = _make_72h_data(total_ru_per_hour=12.0)
        # 修改每个 sample 的 by_fingerprint 为 12 个不同 fingerprint
        for i, s in enumerate(data["samples"]):
            by_fp = []
            for j in range(12):
                ru = float(j + 1)
                by_fp.append({
                    "fingerprint_sha256": hashlib.sha256(
                        f"fp_{j}".encode()
                    ).hexdigest(),
                    "ru": ru,
                    "service": "api",
                    "job": f"job_{j}",
                    "query_text_sample": f"SELECT {j}",
                })
            s["by_fingerprint"] = by_fp
        aggs = compute_aggregates(data)
        top = aggs["top_fingerprints"]
        assert len(top) == 10  # 最多 10
        # 验证降序:fp_11 (ru=12 × 72 = 864) > fp_10 (ru=11 × 72 = 792) > ...
        for i in range(len(top) - 1):
            assert top[i]["ru"] >= top[i + 1]["ru"]
        # fp_11 的 RU 应最大
        assert top[0]["ru"] == 12 * 72
        # top 应排除 ru 最小的 fp_0 和 fp_1(共 12 个但 top=10)
        assert top[-1]["ru"] == 3 * 72  # fp_2

    def test_bot_ru_per_day_computation(self):
        """应正确计算 bot role 日均 RU。"""
        # bot 0.5 RU/h × 72 = 36;36/3 = 12/day
        data = _make_72h_data(bot_ru_per_hour=0.5)
        aggs = compute_aggregates(data)
        assert abs(aggs["bot_ru_per_day"] - 12.0) < 0.1

    def test_bot_ru_per_day_zero_when_no_bot(self):
        """无 bot role RU 时 bot_ru_per_day 应为 0。"""
        data = _make_72h_data(bot_ru_per_hour=0.0)
        aggs = compute_aggregates(data)
        assert aggs["bot_ru_per_day"] == 0.0

    def test_total_fingerprints_count(self):
        """total_fingerprints 应正确计数不重复 fingerprint。"""
        data = _make_72h_data()
        aggs = compute_aggregates(data)
        # 默认 fixture 有 3 个不同 fingerprint
        assert aggs["total_fingerprints"] == 3

    def test_sample_count(self):
        """sample_count 应等于 samples 数组长度。"""
        data = _make_72h_data()
        aggs = compute_aggregates(data)
        assert aggs["sample_count"] == 72


# ════════════════════════════════════════════════════════════════
# 场景 9:退出码(0 on success, 1 on fail)
# ════════════════════════════════════════════════════════════════


class TestExitCodes:
    """场景 9:脚本退出码。"""

    def test_exit_zero_on_success(self, tmp_path):
        """所有门禁 PASS 时应 exit 0。"""
        data = _make_72h_data(
            total_ru_per_hour=0.5,  # 12/day → ≤ 20 → PASS
            bot_ru_per_hour=0.0,
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_exit_one_on_bot_ru_fail(self, tmp_path):
        """bot role RU > 0 时应 exit 1。"""
        data = _make_72h_data(
            total_ru_per_hour=0.5,
            bot_ru_per_hour=1.0,  # bot > 0 → FAIL
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1, (
            f"bot RU > 0 应 exit 1,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_exit_one_on_hard_cap_fail(self, tmp_path):
        """cluster > 100 RU/day 应 exit 1。"""
        # 5 RU/h × 72 = 360;360/3 = 120/day → > 100 → FAIL
        data = _make_72h_data(
            total_ru_per_hour=5.0,
            bot_ru_per_hour=0.0,
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1, (
            f"cluster > 100 应 exit 1,实际 {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_exit_one_on_block(self, tmp_path):
        """cluster > 500 RU/day 应 exit 1 (BLOCK)。"""
        # 22 RU/h × 72 = 1584;1584/3 = 528/day → > 500 → BLOCK
        data = _make_72h_data(
            total_ru_per_hour=22.0,
            bot_ru_per_hour=0.0,
        )
        data_file = tmp_path / "ru_72h_data.json"
        _write_data_file(data, data_file)
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1

    def test_exit_two_on_missing_data_file(self, tmp_path):
        """数据文件不存在应 exit 2。"""
        result = subprocess.run(
            [
                "python3", str(SCRIPT_PATH),
                "--data", str(tmp_path / "nonexistent.json"),
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2

    def test_exit_two_on_invalid_json(self, tmp_path):
        """数据文件非合法 JSON 应 exit 2。"""
        data_file = tmp_path / "ru_72h_data.json"
        data_file.write_text("not-json-content", encoding="utf-8")
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2

    def test_exit_two_on_schema_violation(self, tmp_path):
        """schema 校验失败应 exit 2。"""
        # 缺少顶层字段
        data = {"samples": []}
        data_file = tmp_path / "ru_72h_data.json"
        data_file.write_text(json.dumps(data), encoding="utf-8")
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2

    def test_exit_two_on_unsigned_data(self, tmp_path):
        """未签名数据文件应 exit 2(schema 校验失败)。"""
        data = _make_72h_data(include_signature=False)
        data["signature"] = ""  # 明确空签名
        data_file = tmp_path / "ru_72h_data.json"
        data_file.write_text(json.dumps(data), encoding="utf-8")
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--data", str(data_file)],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2

    def test_exit_zero_on_dry_run(self):
        """--dry-run 应 exit 0。"""
        result = subprocess.run(
            ["python3", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0


# ════════════════════════════════════════════════════════════════
# 附加:collector 模块测试(crdb_ru_attribution 表 + SQL fingerprint)
# ════════════════════════════════════════════════════════════════


class TestCollectorAttributionTable:
    """collector 模块:crdb_ru_attribution 表与 SQL fingerprint 函数测试。"""

    def test_normalize_sql_basic(self):
        """normalize_sql 应折叠大小写、去除字面量。"""
        from services.crdb_ru_collector import normalize_sql
        # 大小写折叠
        assert "select" in normalize_sql("SELECT * FROM users")
        # 数字字面量替换
        n = normalize_sql("SELECT * FROM users WHERE id = 42")
        assert "42" not in n
        assert "?" in n
        # 字符串字面量替换
        n = normalize_sql("SELECT * FROM users WHERE name = 'John'")
        assert "John" not in n
        # 注释去除
        n = normalize_sql("SELECT 1 -- this is a comment")
        assert "comment" not in n
        # 多行注释
        n = normalize_sql("SELECT /* block */ 1")
        assert "block" not in n

    def test_compute_sql_fingerprint_deterministic(self):
        """compute_sql_fingerprint 应对等价 SQL 产生相同 fingerprint。"""
        from services.crdb_ru_collector import compute_sql_fingerprint
        fp1 = compute_sql_fingerprint("SELECT * FROM users WHERE id = 42")
        fp2 = compute_sql_fingerprint("select * from users where id = 99")
        fp3 = compute_sql_fingerprint("SELECT   *   FROM   users   WHERE   id   =   1")
        assert fp1 == fp2 == fp3
        assert len(fp1) == 64  # sha256 hex 长度

    def test_compute_sql_fingerprint_different_sql(self):
        """不同 SQL 应产生不同 fingerprint。"""
        from services.crdb_ru_collector import compute_sql_fingerprint
        fp1 = compute_sql_fingerprint("SELECT * FROM users")
        fp2 = compute_sql_fingerprint("SELECT * FROM files")
        assert fp1 != fp2

    def test_compute_sql_fingerprint_empty_sql(self):
        """空 SQL 应产生确定性 fingerprint(非空字符串)。"""
        from services.crdb_ru_collector import compute_sql_fingerprint
        fp = compute_sql_fingerprint("")
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_truncate_query_sample(self):
        """truncate_query_sample 应限制到 200 字符。"""
        from services.crdb_ru_collector import (
            QUERY_TEXT_SAMPLE_MAX_LEN,
            truncate_query_sample,
        )
        assert QUERY_TEXT_SAMPLE_MAX_LEN == 200
        short = "SELECT 1"
        assert truncate_query_sample(short) == short
        long_sql = "SELECT * FROM users WHERE " + "x = 1 AND " * 50
        truncated = truncate_query_sample(long_sql)
        assert len(truncated) == 200

    def test_crdb_ru_attribution_table_ddl(self):
        """DDL 应包含审计要求的字段。"""
        from services.crdb_ru_collector import CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "crdb_ru_attribution" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "fingerprint_sha256" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "service" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "job" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "ru_consumed" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "sampled_at" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "sample_window_seconds" in CRDB_RU_ATTRIBUTION_TABLE_DDL
        assert "query_text_sample" in CRDB_RU_ATTRIBUTION_TABLE_DDL

    def test_init_creates_table(self, tmp_path):
        """init_crdb_ru_attribution_table 应创建表。"""
        import sqlite3
        from services.crdb_ru_collector import init_crdb_ru_attribution_table
        db_path = str(tmp_path / "test_attr.db")
        assert init_crdb_ru_attribution_table(db_path) is True
        # 验证表已创建
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='crdb_ru_attribution'"
            )
            assert cursor.fetchone() is not None
            # 验证字段
            cursor = conn.execute("PRAGMA table_info(crdb_ru_attribution)")
            cols = {row[1] for row in cursor.fetchall()}
            expected = {
                "id", "fingerprint_sha256", "service", "job",
                "ru_consumed", "sampled_at", "sample_window_seconds",
                "query_text_sample",
            }
            assert expected.issubset(cols), f"缺少字段: {expected - cols}"
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_record_and_query_attribution_row(self, tmp_path):
        """应能写入并查询 attribution 行。"""
        from services.crdb_ru_collector import (
            init_crdb_ru_attribution_table,
            query_ru_attribution_rows,
            record_ru_attribution_row,
        )
        db_path = str(tmp_path / "test_attr.db")
        assert init_crdb_ru_attribution_table(db_path) is True

        # 写入一行
        ok = await record_ru_attribution_row(
            fingerprint_sha256="a" * 64,
            service="admin",
            ru_consumed=1.5,
            sampled_at="2026-07-12T00:00:00+00:00",
            sample_window_seconds=3600,
            job="user_list",
            query_text_sample="SELECT * FROM users",
            db_path=db_path,
        )
        assert ok is True

        # 查询
        rows = query_ru_attribution_rows(db_path=db_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["fingerprint_sha256"] == "a" * 64
        assert r["service"] == "admin"
        assert r["job"] == "user_list"
        assert r["ru_consumed"] == 1.5
        assert r["sample_window_seconds"] == 3600
        assert "SELECT" in r["query_text_sample"]

    @pytest.mark.asyncio
    async def test_record_attribution_truncates_query_sample(self, tmp_path):
        """写入时应自动截断 query_text_sample 到 200 字符。"""
        from services.crdb_ru_collector import (
            init_crdb_ru_attribution_table,
            query_ru_attribution_rows,
            record_ru_attribution_row,
        )
        db_path = str(tmp_path / "test_attr.db")
        assert init_crdb_ru_attribution_table(db_path) is True
        long_sql = "SELECT * FROM users WHERE " + "x = 1 AND " * 50
        ok = await record_ru_attribution_row(
            fingerprint_sha256="b" * 64,
            service="api",
            ru_consumed=2.0,
            sampled_at="2026-07-12T01:00:00+00:00",
            job="api_handler",
            query_text_sample=long_sql,
            db_path=db_path,
        )
        assert ok is True
        rows = query_ru_attribution_rows(db_path=db_path)
        assert len(rows[0]["query_text_sample"]) == 200

    def test_aggregate_ru_attribution(self, tmp_path):
        """aggregate_ru_attribution 应聚合 by_service / top_fingerprints。"""
        from services.crdb_ru_collector import (
            aggregate_ru_attribution,
            init_crdb_ru_attribution_table,
        )
        import sqlite3
        db_path = str(tmp_path / "test_attr.db")
        assert init_crdb_ru_attribution_table(db_path) is True
        # 直接插入多条记录
        conn = sqlite3.connect(db_path)
        try:
            for i in range(5):
                conn.execute(
                    "INSERT INTO crdb_ru_attribution "
                    "(fingerprint_sha256, service, job, ru_consumed, "
                    " sampled_at, sample_window_seconds, query_text_sample) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"fp_{i % 3}".ljust(64, "0"),  # 3 个不同 fingerprint
                        "admin" if i % 2 == 0 else "api",
                        "user_list" if i % 2 == 0 else "api_handler",
                        float(i + 1),
                        f"2026-07-12T{i:02d}:00:00+00:00",
                        3600,
                        f"SELECT {i}",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        result = aggregate_ru_attribution(db_path=db_path)
        # 5 行 RU = 1+2+3+4+5 = 15
        assert result["total_ru"] == 15.0
        assert result["sample_count"] == 5
        # by_service:admin (i=0,2,4) = 1+3+5=9;api (i=1,3) = 2+4=6
        assert result["by_service"]["admin"] == 9.0
        assert result["by_service"]["api"] == 6.0
        # 3 个不同 fingerprint
        assert len(result["by_fingerprint"]) == 3
        # top_fingerprints 按 RU 降序
        top = result["top_fingerprints"]
        assert len(top) <= 10
        for i in range(len(top) - 1):
            assert top[i]["ru"] >= top[i + 1]["ru"]

    def test_query_ru_attribution_filters(self, tmp_path):
        """query_ru_attribution_rows 应支持 service / fingerprint 过滤。"""
        from services.crdb_ru_collector import (
            init_crdb_ru_attribution_table,
            query_ru_attribution_rows,
        )
        import sqlite3
        db_path = str(tmp_path / "test_attr.db")
        assert init_crdb_ru_attribution_table(db_path) is True
        conn = sqlite3.connect(db_path)
        try:
            conn.executemany(
                "INSERT INTO crdb_ru_attribution "
                "(fingerprint_sha256, service, job, ru_consumed, "
                " sampled_at, sample_window_seconds, query_text_sample) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("a" * 64, "admin", "j1", 1.0,
                     "2026-07-12T00:00:00+00:00", 3600, "SELECT 1"),
                    ("b" * 64, "api", "j2", 2.0,
                     "2026-07-12T01:00:00+00:00", 3600, "SELECT 2"),
                    ("a" * 64, "admin", "j1", 3.0,
                     "2026-07-12T02:00:00+00:00", 3600, "SELECT 1"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        # 过滤 service=admin → 2 行
        rows = query_ru_attribution_rows(service="admin", db_path=db_path)
        assert len(rows) == 2
        # 过滤 fingerprint → 2 行
        rows = query_ru_attribution_rows(
            fingerprint_sha256="a" * 64, db_path=db_path
        )
        assert len(rows) == 2
        # 时间范围过滤
        rows = query_ru_attribution_rows(
            start_time="2026-07-12T01:00:00+00:00",
            db_path=db_path,
        )
        assert len(rows) == 2  # 01:00 和 02:00

    def test_query_ru_attribution_nonexistent_db_returns_empty(self, tmp_path):
        """DB 不存在时应返回空列表(不抛异常)。"""
        from services.crdb_ru_collector import query_ru_attribution_rows
        rows = query_ru_attribution_rows(
            db_path=str(tmp_path / "nonexistent.db")
        )
        assert rows == []


# ════════════════════════════════════════════════════════════════
# 附加:CI 工作流文件检查
# ════════════════════════════════════════════════════════════════


class TestCIWorkflowIntegration:
    """验证 release-gates.yml 中 crdb-ru-72h-attribution-gate job 配置正确。"""

    def test_workflow_has_crdb_ru_72h_attribution_gate_job(self):
        """release-gates.yml 应包含 crdb-ru-72h-attribution-gate job。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "crdb-ru-72h-attribution-gate:" in content, (
            "release-gates.yml 应定义 crdb-ru-72h-attribution-gate job"
        )

    def test_workflow_job_runs_dry_run_for_pr(self):
        """PR/push 场景应运行 --dry-run。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "check_crdb_ru_72h_attribution.py --dry-run" in content, (
            "PR/push 场景应运行 --dry-run"
        )

    def test_workflow_job_runs_strict_for_release_tag(self):
        """release tag 场景应运行 --strict --data。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "--strict" in content
        assert "--data" in content

    def test_publish_attestation_does_not_depend_on_gate(self):
        """R66 P0-05: publish-attestation (master-only code-release gate)
        不应依赖 crdb-ru-72h-attribution-gate(该 job 在 push 场景为 dry-run,
        evidence_status=not_applicable,不应计入 code-release 的 production evidence)。

        production-promotion-gate (tag-only) 才依赖 crdb-ru-72h-attribution-gate
        且要求 evidence_status=production。
        """
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        # 找到 publish-attestation 段落
        pa_start = content.find("publish-attestation:")
        assert pa_start != -1
        # 截取 publish-attestation 段落(到下一个顶级 job 之前)
        next_job_idx = content.find("\n  #", pa_start + 100)
        if next_job_idx < 0:
            next_job_idx = pa_start + 5000
        # 找到下一个顶级 job(以两空格 + 字母开头,且非 publish-attestation 段内)
        # 简化:取 needs 行后的范围
        pa_section = content[pa_start:pa_start + 3000]
        # 找到 needs 行
        needs_line_start = pa_section.find("needs:")
        assert needs_line_start != -1, "publish-attestation 必须有 needs"
        # 取 needs 行后到下一个非缩进字段
        needs_section = pa_section[needs_line_start:needs_line_start + 800]
        assert "crdb-ru-72h-attribution-gate" not in needs_section, (
            "R66 P0-05: publish-attestation (master-only) 不应依赖 "
            "crdb-ru-72h-attribution-gate (push 场景为 not_applicable,不应计入 code-release)"
        )

    def test_production_promotion_gate_depends_on_gate_and_requires_evidence(self):
        """R66 P0-05: production-promotion-gate (tag-only) 必须依赖
        crdb-ru-72h-attribution-gate 且要求 evidence_status=production。
        """
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        # 找到 production-promotion-gate 段落
        ppg_start = content.find("production-promotion-gate:")
        assert ppg_start != -1
        ppg_section = content[ppg_start:ppg_start + 5000]
        # needs 必须包含 crdb-ru-72h-attribution-gate
        assert "crdb-ru-72h-attribution-gate" in ppg_section, (
            "R66 P0-05: production-promotion-gate needs 必须包含 crdb-ru-72h-attribution-gate"
        )
        # 必须有校验 evidence_status=production 的步骤
        assert "evidence_status" in ppg_section, (
            "R66 P0-05: production-promotion-gate 必须校验 evidence_status"
        )
        assert "production" in ppg_section, (
            "R66 P0-05: production-promotion-gate 必须要求 evidence_status=production"
        )

    def test_gate_job_outputs_evidence_status(self):
        """R66 P0-05: crdb-ru-72h-attribution-gate job 必须输出 evidence_status。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        # 找到 crdb-ru-72h-attribution-gate 段落
        gate_start = content.find("crdb-ru-72h-attribution-gate:")
        assert gate_start != -1
        gate_section = content[gate_start:gate_start + 3000]
        # 必须有 outputs 段
        assert "outputs:" in gate_section, (
            "R66 P0-05: crdb-ru-72h-attribution-gate 必须有 outputs 段"
        )
        # 必须输出 evidence_status
        assert "evidence_status:" in gate_section, (
            "R66 P0-05: crdb-ru-72h-attribution-gate 必须输出 evidence_status"
        )
        # 必须输出 evidence_mode
        assert "evidence_mode:" in gate_section, (
            "R66 P0-05: crdb-ru-72h-attribution-gate 必须输出 evidence_mode"
        )
        # 必须区分 not_applicable 与 production
        assert "not_applicable" in gate_section, (
            "R66 P0-05: crdb-ru-72h-attribution-gate 必须输出 not_applicable (PR/push)"
        )
        assert "production" in gate_section, (
            "R66 P0-05: crdb-ru-72h-attribution-gate 必须输出 production (release tag)"
        )

    def test_release_summary_includes_gate(self):
        """release-summary env 应包含 CRDB_RU_72H_ATTRIBUTION_GATE。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        assert "CRDB_RU_72H_ATTRIBUTION_GATE:" in content
        assert "crdb-ru-72h-attribution-gate=${CRDB_RU_72H_ATTRIBUTION_GATE}" in content

    def test_workflow_branch_protection_includes_gate(self):
        """branch protection WORKFLOW_JOBS Release Gates 列表应包含新 job。"""
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = wf_path.read_text(encoding="utf-8")
        # 在 WORKFLOW_JOBS 数组中查找
        assert "crdb-ru-72h-attribution-gate,publish-attestation" in content or \
               "crdb-ru-72h-attribution-gate" in content

    def test_workflow_yaml_valid(self):
        """release-gates.yml 应为合法 YAML。"""
        import yaml
        wf_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        data = yaml.safe_load(open(wf_path))
        assert "jobs" in data
        assert "crdb-ru-72h-attribution-gate" in data["jobs"]


# ════════════════════════════════════════════════════════════════
# 附加:数据文件 schema 文档化检查
# ════════════════════════════════════════════════════════════════


class TestDataFileSchemaDoc:
    """验证 ru_72h_data.json schema 在脚本中清晰文档化。"""

    def test_script_documents_schema(self):
        """脚本应在 docstring 中文档化 ru_72h_data.json schema。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        # 关键 schema 字段应在脚本中提及
        for field in ("environment_id", "commit_sha", "image_digest",
                      "started_at", "ended_at", "duration_hours",
                      "executed_by", "approved_by", "samples",
                      "signature", "signature_type"):
            assert field in content, f"脚本应文档化 schema 字段: {field}"

    def test_script_documents_thresholds(self):
        """脚本应文档化 6 项门禁阈值。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        for threshold_label in (
            "0 RU/day",      # bot role
            "≤ 20 RU/day",  # cluster ideal
            "≤ 100 RU/day", # hard cap
            "> 500",        # block
            "250",          # RU/DAU
            "35,000,000",   # monthly
        ):
            assert threshold_label in content or threshold_label.replace(",", "") in content, (
                f"脚本应文档化阈值: {threshold_label}"
            )

    def test_script_supports_signature_types(self):
        """脚本应支持 cosign/gpg/hmac 三种签名类型。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "cosign" in content
        assert "gpg" in content
        assert "hmac" in content
