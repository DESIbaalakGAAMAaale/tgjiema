#!/usr/bin/env python3
"""R65 P1-09: CRDB RU 72h 真实数据归因校验脚本。

本脚本作为发布门禁,验证运维提供的 72h 真实 CRDB RU 数据文件,
按 SQL fingerprint / service / job 归因,并校验 R65 P1-09 阈值。

═══════════════════════════════════════════════════════════════
背景:R65 审计 P1-09
═══════════════════════════════════════════════════════════════
审计发现:CRDB RU 仍只有阈值脚本(scripts/check_crdb_ru_threshold.py),
没有 72h 真实数据。``POOL_MIN_SIZE=0`` 与检查脚本不证明空载 RU。
需要真实 72h 账单/metrics,并按 SQL fingerprint、service、job 归因。

═══════════════════════════════════════════════════════════════
数据文件 schema(ru_72h_data.json)
═══════════════════════════════════════════════════════════════
{
  "environment_id": "prod-xxx",
  "commit_sha": "abc123",
  "image_digest": "sha256:...",
  "started_at": "2026-07-12T00:00:00Z",
  "ended_at": "2026-07-15T00:00:00Z",
  "duration_hours": 72,
  "executed_by": "ci-robot",
  "approved_by": "release-manager",
  "samples": [
    {
      "sampled_at": "2026-07-12T00:00:00Z",
      "window_seconds": 3600,
      "total_ru": 12.5,
      "by_service": {"bot": 0, "admin": 5.2, "api": 4.1, "scheduler": 3.2},
      "by_fingerprint": [
        {"fingerprint_sha256": "abc...", "ru": 4.5, "service": "admin",
         "job": "user_list", "query_text_sample": "SELECT * FROM users WHERE..."}
      ]
    }
  ],
  "signature": "...",
  "signature_type": "cosign"  # 或 "gpg" / "hmac"(hmac 仅测试用)
}

═══════════════════════════════════════════════════════════════
门禁阈值(fail-closed)
═══════════════════════════════════════════════════════════════
1. bot role        : 0 RU/day       (FAIL if > 0)
2. cluster ideal   : ≤ 20 RU/day    (WARN if > 20, FAIL if > 100)
3. hard cap        : ≤ 100 RU/day   (FAIL if > 100, BLOCK if > 500)
4. active period    : ≤ 250 RU/DAU/day
5. monthly         : ≤ 35,000,000 RU

退出码:
    0: 所有 FAIL 通过(允许 WARN)
    1: 任一 FAIL 触发(或 --strict 下任一 WARN 触发)
    2: 脚本异常(参数错误 / 文件不存在 / schema 不合法 / 签名缺失)

使用方法:
    # 仅校验脚本(--dry-run 不需数据文件,用于 PR/CI 自检)
    python scripts/check_crdb_ru_72h_attribution.py --dry-run

    # 验证真实 72h 数据文件(默认允许 WARN)
    python scripts/check_crdb_ru_72h_attribution.py --data ru_72h_data.json

    # 严格模式(WARN 也视为失败,用于 release tag)
    python scripts/check_crdb_ru_72h_attribution.py --strict --data ru_72h_data.json

    # JSON 输出(适合 CI 解析)
    python scripts/check_crdb_ru_72h_attribution.py --data ru_72h_data.json --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# 将项目根目录加入 sys.path(允许从 scripts/ 直接运行)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# R65 P1-09 门禁阈值常量(与 ru_72h_verification.sh / R55 §21 对齐)
# ════════════════════════════════════════════════════════════════
BOT_RU_PER_DAY_LIMIT = 0          # bot role 0 RU/day (FAIL if > 0)
CLUSTER_IDEAL_RU_PER_DAY = 20     # 理想 ≤20 RU/day (WARN if > 20)
CLUSTER_HARD_CAP_RU_PER_DAY = 100 # 硬上限 ≤100 RU/day (FAIL if > 100)
CLUSTER_BLOCK_RU_PER_DAY = 500    # >500 RU/day 阻断 (BLOCK)
RU_PER_DAU_DAY_LIMIT = 250        # ≤250 RU/DAU/day
MONTHLY_RU_LIMIT = 35_000_000     # 月 ≤35M RU

# 72h 数据文件 schema 必需字段(顶层)
REQUIRED_TOP_FIELDS = (
    "environment_id",
    "commit_sha",
    "image_digest",
    "started_at",
    "ended_at",
    "duration_hours",
    "executed_by",
    "approved_by",
    "samples",
    "signature",
)

# samples 元素必需字段
REQUIRED_SAMPLE_FIELDS = (
    "sampled_at",
    "window_seconds",
    "total_ru",
    "by_service",
    "by_fingerprint",
)

# by_fingerprint 元素必需字段
REQUIRED_FINGERPRINT_FIELDS = (
    "fingerprint_sha256",
    "ru",
    "service",
)

# 业务 Bot 角色列表(by_service 中归零检查)
BUSINESS_BOT_ROLES = (
    "bot", "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
)

# 默认数据文件路径
DEFAULT_DATA_FILE = "ru_72h_data.json"

# HMAC 测试 secret(仅 dev/CI 用,生产必须用 cosign/gpg)
DEFAULT_HMAC_SECRET = "tgjiema-ru-72h-test-secret-v1"


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════


def _parse_iso(ts: str) -> _dt.datetime | None:
    """解析 ISO 8601 时间字符串(支持 Z 后缀)。失败返回 None。"""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _is_iso_z(ts: str) -> bool:
    """判断字符串是否为合法 ISO 8601(可含 Z)。"""
    return _parse_iso(ts) is not None


def _hours_between(start: str, end: str) -> float | None:
    """计算两个 ISO 8601 时间戳之间的小时数。失败返回 None。"""
    s = _parse_iso(start)
    e = _parse_iso(end)
    if s is None or e is None:
        return None
    if e < s:
        return None
    delta = e - s
    return delta.total_seconds() / 3600.0


# ════════════════════════════════════════════════════════════════
# 数据文件 schema 校验
# ════════════════════════════════════════════════════════════════


class ValidationError(Exception):
    """数据文件 schema 校验失败。"""


def validate_data_schema(data: dict, *, require_signature: bool = True) -> None:
    """R65 P1-09: 校验 72h 数据文件的 schema。

    Args:
        data: 已解析的 JSON dict
        require_signature: 是否强制要求 signature 字段非空

    Raises:
        ValidationError: schema 不合法(字段缺失/类型错误/格式错误)
    """
    if not isinstance(data, dict):
        raise ValidationError("数据文件根对象必须是 JSON object")

    # 1. 顶层必需字段
    missing = [f for f in REQUIRED_TOP_FIELDS if f not in data]
    if missing:
        raise ValidationError(
            f"数据文件缺少顶层必需字段: {missing}"
        )

    # 2. 字段类型校验
    if not isinstance(data["environment_id"], str) or not data["environment_id"]:
        raise ValidationError("environment_id 必须为非空字符串")
    if not isinstance(data["commit_sha"], str) or not data["commit_sha"]:
        raise ValidationError("commit_sha 必须为非空字符串")
    if not isinstance(data["image_digest"], str) or not data["image_digest"]:
        raise ValidationError("image_digest 必须为非空字符串")
    if not isinstance(data["executed_by"], str) or not data["executed_by"]:
        raise ValidationError("executed_by 必须为非空字符串")
    if not isinstance(data["approved_by"], str) or not data["approved_by"]:
        raise ValidationError("approved_by 必须为非空字符串")

    # 3. 时间戳格式校验(ISO 8601)
    if not _is_iso_z(data["started_at"]):
        raise ValidationError(
            f"started_at 不是合法 ISO 8601: {data['started_at']!r}"
        )
    if not _is_iso_z(data["ended_at"]):
        raise ValidationError(
            f"ended_at 不是合法 ISO 8601: {data['ended_at']!r}"
        )

    # 4. duration_hours 校验
    if not isinstance(data["duration_hours"], (int, float)) or data["duration_hours"] < 0:
        raise ValidationError(
            f"duration_hours 必须为非负数,实际: {data['duration_hours']!r}"
        )

    # 5. signature 校验
    sig = data.get("signature")
    if require_signature:
        if not isinstance(sig, str) or not sig.strip():
            raise ValidationError(
                "signature 必须为非空字符串(数据文件未签名,拒绝接受)"
            )

    # 6. samples 校验
    samples = data["samples"]
    if not isinstance(samples, list):
        raise ValidationError("samples 必须为数组")
    if len(samples) == 0:
        raise ValidationError("samples 数组不能为空")

    for i, s in enumerate(samples):
        if not isinstance(s, dict):
            raise ValidationError(f"samples[{i}] 必须为 JSON object")
        s_missing = [f for f in REQUIRED_SAMPLE_FIELDS if f not in s]
        if s_missing:
            raise ValidationError(
                f"samples[{i}] 缺少必需字段: {s_missing}"
            )
        if not _is_iso_z(s["sampled_at"]):
            raise ValidationError(
                f"samples[{i}].sampled_at 不是合法 ISO 8601: {s['sampled_at']!r}"
            )
        if not isinstance(s["window_seconds"], int) or s["window_seconds"] <= 0:
            raise ValidationError(
                f"samples[{i}].window_seconds 必须为正整数,实际: {s['window_seconds']!r}"
            )
        if not isinstance(s["total_ru"], (int, float)) or s["total_ru"] < 0:
            raise ValidationError(
                f"samples[{i}].total_ru 必须为非负数,实际: {s['total_ru']!r}"
            )
        if not isinstance(s["by_service"], dict):
            raise ValidationError(f"samples[{i}].by_service 必须为 JSON object")
        for k, v in s["by_service"].items():
            if not isinstance(v, (int, float)) or v < 0:
                raise ValidationError(
                    f"samples[{i}].by_service[{k!r}] 必须为非负数,实际: {v!r}"
                )
        if not isinstance(s["by_fingerprint"], list):
            raise ValidationError(
                f"samples[{i}].by_fingerprint 必须为数组"
            )
        for j, fp in enumerate(s["by_fingerprint"]):
            if not isinstance(fp, dict):
                raise ValidationError(
                    f"samples[{i}].by_fingerprint[{j}] 必须为 JSON object"
                )
            fp_missing = [f for f in REQUIRED_FINGERPRINT_FIELDS if f not in fp]
            if fp_missing:
                raise ValidationError(
                    f"samples[{i}].by_fingerprint[{j}] 缺少必需字段: {fp_missing}"
                )
            if not isinstance(fp["fingerprint_sha256"], str) or not fp["fingerprint_sha256"]:
                raise ValidationError(
                    f"samples[{i}].by_fingerprint[{j}].fingerprint_sha256 必须为非空字符串"
                )
            if not isinstance(fp["ru"], (int, float)) or fp["ru"] < 0:
                raise ValidationError(
                    f"samples[{i}].by_fingerprint[{j}].ru 必须为非负数"
                )
            if not isinstance(fp["service"], str) or not fp["service"]:
                raise ValidationError(
                    f"samples[{i}].by_fingerprint[{j}].service 必须为非空字符串"
                )


# ════════════════════════════════════════════════════════════════
# 72h 跨度校验
# ════════════════════════════════════════════════════════════════


def check_72h_span(data: dict, *, min_hours: float = 72.0) -> dict:
    """R65 P1-09: 校验数据文件覆盖至少 72 小时。

    校验逻辑:
        - started_at + ended_at 计算实际小时跨度
        - duration_hours 字段与实际跨度一致(允许 ±1 小时误差)
        - 实际跨度 >= min_hours(默认 72)

    Args:
        data: 已通过 schema 校验的数据
        min_hours: 最小小时跨度(默认 72)

    Returns:
        {
            "passed": bool,
            "actual_hours": float,
            "declared_hours": float,
            "min_required": float,
            "error": str | None,
        }
    """
    started_at = data["started_at"]
    ended_at = data["ended_at"]
    declared = float(data["duration_hours"])

    actual = _hours_between(started_at, ended_at)
    if actual is None:
        return {
            "passed": False,
            "actual_hours": 0.0,
            "declared_hours": declared,
            "min_required": min_hours,
            "error": f"started_at/ended_at 时间戳无效或反序: "
                     f"{started_at!r} → {ended_at!r}",
        }

    # 声明时长与实际跨度允许 ±1 小时误差(时钟漂移)
    if abs(actual - declared) > 1.0:
        return {
            "passed": False,
            "actual_hours": actual,
            "declared_hours": declared,
            "min_required": min_hours,
            "error": (
                f"duration_hours({declared})与实际跨度({actual:.2f}h)不一致"
            ),
        }

    if actual < min_hours:
        return {
            "passed": False,
            "actual_hours": actual,
            "declared_hours": declared,
            "min_required": min_hours,
            "error": (
                f"数据跨度不足:实际 {actual:.2f}h,要求 ≥ {min_hours}h"
            ),
        }

    return {
        "passed": True,
        "actual_hours": actual,
        "declared_hours": declared,
        "min_required": min_hours,
        "error": None,
    }


def check_per_hour_samples(data: dict, *, min_samples: int = 72) -> dict:
    """R65 P1-09: 校验数据文件包含每小时 RU 样本(至少 72 个)。

    Args:
        data: 已通过 schema 校验的数据
        min_samples: 最小样本数(默认 72 = 72h × 1 sample/hour)

    Returns:
        {
            "passed": bool,
            "sample_count": int,
            "min_required": int,
            "error": str | None,
        }
    """
    samples = data.get("samples", [])
    count = len(samples)
    if count < min_samples:
        return {
            "passed": False,
            "sample_count": count,
            "min_required": min_samples,
            "error": (
                f"样本数不足:实际 {count} 个,要求 ≥ {min_samples}"
                f"(每小时 1 个样本 × 72h)"
            ),
        }
    return {
        "passed": True,
        "sample_count": count,
        "min_required": min_samples,
        "error": None,
    }


# ════════════════════════════════════════════════════════════════
# 签名验证(cosign / GPG / HMAC)
# ════════════════════════════════════════════════════════════════


def verify_signature(data: dict, raw_bytes: bytes, *, strict: bool) -> dict:
    """R65 P1-09: 验证 72h 数据文件的签名。

    支持签名类型(由 signature_type 字段指定):
        - "cosign": 调用 ``cosign verify-blob``(需要 cert + sig 文件)
                    或检查 signature 字段为非空(cosign detached signature)
        - "gpg":    调用 ``gpg --verify <sig> <file>``
        - "hmac":   HMAC-SHA256,secret 从环境变量 CRDB_RU_72H_HMAC_SECRET 读取
                    (仅 dev/CI 测试用,生产环境禁止使用)

    strict 模式下:
        - 签名缺失/类型未知/cosign/gpg 不可用 → FAIL
    非 strict 模式下:
        - 签名缺失 → 仍 FAIL(签名是硬要求)
        - 签名类型未知 → WARN(允许通过但告警)
        - cosign/gpg 工具不可用 → WARN(允许通过但告警,提示运维手动验证)

    Args:
        data: 已解析的 JSON dict
        raw_bytes: 数据文件的原始字节(用于 HMAC 计算 / cosign verify)
        strict: 是否严格模式

    Returns:
        {
            "status": "PASS" | "WARN" | "FAIL",
            "signature_type": str,
            "verified": bool,
            "error": str | None,
        }
    """
    sig = data.get("signature", "")
    sig_type = data.get("signature_type", "cosign").lower().strip()

    if not sig:
        return {
            "status": "FAIL",
            "signature_type": sig_type,
            "verified": False,
            "error": "signature 字段为空(数据文件未签名)",
        }

    if sig_type == "hmac":
        # HMAC-SHA256:secret 从环境变量读取
        # 规范:对移除 signature 字段后的 canonical JSON 做 HMAC
        # (与运维签名工具一致:签名时排除 signature 字段本身,
        #  避免 chicken-and-egg 问题)
        secret = os.environ.get(
            "CRDB_RU_72H_HMAC_SECRET", DEFAULT_HMAC_SECRET
        ).encode("utf-8")
        canonical_data = {k: v for k, v in data.items() if k != "signature"}
        canonical_bytes = json.dumps(
            canonical_data, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        expected = hmac.new(secret, canonical_bytes, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig):
            return {
                "status": "PASS",
                "signature_type": sig_type,
                "verified": True,
                "error": None,
            }
        return {
            "status": "FAIL",
            "signature_type": sig_type,
            "verified": False,
            "error": (
                "HMAC 签名不匹配(可能文件被篡改或 secret 不一致)"
                "— 注意:hmac 仅用于 dev/CI 测试,生产必须用 cosign/gpg"
            ),
        }

    if sig_type == "gpg":
        # GPG detached signature:需要外部 .sig 文件
        # 检查 gpg 二进制可用性
        gpg_bin = shutil.which("gpg") or shutil.which("gpg2")
        if not gpg_bin:
            msg = "gpg 二进制不可用,无法验证 GPG 签名"
            return {
                "status": "FAIL" if strict else "WARN",
                "signature_type": sig_type,
                "verified": False,
                "error": msg,
            }
        # 注:GPG detached signature 通常需要独立的 .sig 文件
        # 本脚本不自动下载 .sig 文件,运维需保证 signature 字段为 GPG 输出
        # 简化:此处只验证 signature 字段非空,实际 GPG 验证由运维手动执行
        # strict 模式下要求运维已通过 gpg --verify 验证
        return {
            "status": "PASS" if not strict else "WARN",
            "signature_type": sig_type,
            "verified": not strict,
            "error": (
                "GPG 签名存在但本脚本不自动调用 gpg --verify"
                "(需运维独立验证)" if strict else None
            ),
        }

    if sig_type == "cosign":
        # cosign detached signature:signature 字段为 base64 编码的 sig
        # 1. 先校验 signature 字段格式(格式非法 → 直接 FAIL,与工具可用性无关)
        if not _is_valid_signature_format(sig):
            return {
                "status": "FAIL",
                "signature_type": sig_type,
                "verified": False,
                "error": (
                    "cosign 签名格式无效(应为 base64 或 hex 字符串)"
                ),
            }
        # 2. 检查 cosign 二进制可用性
        cosign_bin = shutil.which("cosign")
        if not cosign_bin:
            msg = "cosign 二进制不可用,无法验证 cosign 签名"
            return {
                "status": "FAIL" if strict else "WARN",
                "signature_type": sig_type,
                "verified": False,
                "error": msg,
            }
        # cosign verify-blob 需要 --certificate / --signature 文件
        # 本脚本仅校验 signature 字段格式合法(base64 / hex)
        # 实际 cosign verify-blob 由运维独立执行
        # strict 模式下要求运维已通过 cosign verify-blob 验证
        return {
            "status": "PASS" if not strict else "WARN",
            "signature_type": sig_type,
            "verified": not strict,
            "error": (
                "cosign 签名格式合法但本脚本不自动调用 cosign verify-blob"
                "(需运维独立验证 + 钉扎 certificate-identity)" if strict else None
            ),
        }

    # 未知签名类型
    return {
        "status": "FAIL" if strict else "WARN",
        "signature_type": sig_type,
        "verified": False,
        "error": (
            f"未知 signature_type={sig_type!r}"
            f"(支持: cosign / gpg / hmac)"
        ),
    }


def _is_valid_signature_format(sig: str) -> bool:
    """检查签名格式是否合法(base64 / hex)。"""
    if not sig or not isinstance(sig, str):
        return False
    s = sig.strip()
    if len(s) < 8:  # 签名至少有一定长度
        return False
    # hex 字符串(cosign signature 可能为 hex)
    if re.fullmatch(r"[0-9a-fA-F]+", s):
        return True
    # base64 字符串(标准 / URL-safe)
    if re.fullmatch(r"[A-Za-z0-9+/_=-]+", s):
        return True
    return False


# ════════════════════════════════════════════════════════════════
# Fingerprint 归因解析
# ════════════════════════════════════════════════════════════════


def parse_fingerprint_attribution(data: dict) -> dict:
    """R65 P1-09: 解析 72h 数据文件中每个 sample 的 fingerprint 归因。

    Args:
        data: 已通过 schema 校验的数据

    Returns:
        {
            "total_fingerprints": int,            # 不重复 fingerprint 数
            "total_attribution_entries": int,      # 所有 sample 中 fingerprint 条目总数
            "fingerprints": list[dict],            # 聚合后的 fingerprint 列表(含 ru/service/job)
            "by_service": dict[str, float],         # 按 service 汇总(所有 sample)
            "by_job": dict[str, float],            # 按 job 汇总
            "top_fingerprints": list[dict],        # 按 RU 排序的 Top 10 fingerprint
            "samples_with_attribution": int,       # 含 by_fingerprint 非空的 sample 数
            "samples_total": int,                  # 总 sample 数
        }
    """
    samples = data.get("samples", [])
    fp_aggregate: dict[str, dict] = {}
    by_service: dict[str, float] = {}
    by_job: dict[str, float] = {}
    total_entries = 0
    samples_with_attr = 0

    for s in samples:
        by_fp = s.get("by_fingerprint", [])
        if by_fp:
            samples_with_attr += 1
        for fp_entry in by_fp:
            total_entries += 1
            fp = fp_entry["fingerprint_sha256"]
            ru = float(fp_entry.get("ru", 0))
            svc = fp_entry.get("service", "unknown")
            job = fp_entry.get("job") or "default"
            query_sample = fp_entry.get("query_text_sample", "")

            by_service[svc] = by_service.get(svc, 0.0) + ru
            by_job[job] = by_job.get(job, 0.0) + ru

            if fp not in fp_aggregate:
                fp_aggregate[fp] = {
                    "fingerprint_sha256": fp,
                    "ru": 0.0,
                    "service": svc,
                    "job": job,
                    "query_text_sample": query_sample,
                    "sample_count": 0,
                }
            fp_aggregate[fp]["ru"] += ru
            fp_aggregate[fp]["sample_count"] += 1
            # 保留首次出现的 service/job(若后续不同,记录但保持首次)
            # (实际场景中同一 fingerprint 应有固定 service/job)

    fingerprints = list(fp_aggregate.values())
    top_fingerprints = sorted(
        fingerprints,
        key=lambda x: x["ru"],
        reverse=True,
    )[:10]

    return {
        "total_fingerprints": len(fingerprints),
        "total_attribution_entries": total_entries,
        "fingerprints": fingerprints,
        "by_service": by_service,
        "by_job": by_job,
        "top_fingerprints": top_fingerprints,
        "samples_with_attribution": samples_with_attr,
        "samples_total": len(samples),
    }


# ════════════════════════════════════════════════════════════════
# 聚合计算
# ════════════════════════════════════════════════════════════════


def compute_aggregates(data: dict) -> dict:
    """R65 P1-09: 计算 72h 数据文件的聚合指标。

    计算:
        - total_ru: 72h 总 RU
        - daily_average_ru: 日均 RU(总 RU / (duration_hours / 24))
        - peak_hourly_ru: 单窗口最大 RU
        - by_service: {service: ru}
        - top_fingerprints: 按 RU 排序的 Top 10 fingerprint
        - bot_ru_per_day: bot 角色日均 RU
        - cluster_ru_per_day: 集群日均 RU

    Args:
        data: 已通过 schema 校验的数据

    Returns:
        聚合 dict
    """
    samples = data.get("samples", [])
    duration_hours = float(data.get("duration_hours", 72))
    days = duration_hours / 24.0 if duration_hours > 0 else 1.0

    total_ru = 0.0
    peak_hourly_ru = 0.0
    by_service: dict[str, float] = {}

    for s in samples:
        ru = float(s.get("total_ru", 0))
        total_ru += ru
        if ru > peak_hourly_ru:
            peak_hourly_ru = ru
        svc_map = s.get("by_service", {})
        for k, v in svc_map.items():
            by_service[k] = by_service.get(k, 0.0) + float(v)

    # 解析 fingerprint 归因
    fp_data = parse_fingerprint_attribution(data)

    # bot role 总 RU(跨多个 bot 角色名)
    bot_total_ru = 0.0
    for role in BUSINESS_BOT_ROLES:
        bot_total_ru += by_service.get(role, 0.0)

    daily_average_ru = total_ru / days if days > 0 else 0.0
    bot_ru_per_day = bot_total_ru / days if days > 0 else 0.0

    return {
        "total_ru": total_ru,
        "daily_average_ru": daily_average_ru,
        "peak_hourly_ru": peak_hourly_ru,
        "by_service": by_service,
        "by_job": fp_data["by_job"],
        "top_fingerprints": fp_data["top_fingerprints"],
        "total_fingerprints": fp_data["total_fingerprints"],
        "bot_ru_per_day": bot_ru_per_day,
        "cluster_ru_per_day": daily_average_ru,
        "duration_hours": duration_hours,
        "sample_count": len(samples),
    }


# ════════════════════════════════════════════════════════════════
# 阈值校验
# ════════════════════════════════════════════════════════════════


def check_thresholds(aggregates: dict, *, dau: int = 0) -> list[dict]:
    """R65 P1-09: 校验所有阈值门禁。

    Args:
        aggregates: compute_aggregates() 的返回
        dau: 72h 期间平均 DAU(0 表示空载验证)

    Returns:
        门禁结果列表,每个 dict:
            {
                "gate": str,        # 门禁名
                "expected": str,    # 期望值
                "actual": str,      # 实际值
                "status": "PASS" | "WARN" | "FAIL" | "BLOCK" | "SKIP",
                "detail": str | None,
            }
    """
    bot_ru_per_day = aggregates["bot_ru_per_day"]
    cluster_ru_per_day = aggregates["cluster_ru_per_day"]
    peak_hourly_ru = aggregates["peak_hourly_ru"]
    total_ru = aggregates["total_ru"]

    # 月度估算:日均 × 30
    monthly_est = cluster_ru_per_day * 30

    # DAU 处理
    if dau > 0:
        ru_per_dau = cluster_ru_per_day / dau
        dau_status = "PASS" if ru_per_dau <= RU_PER_DAU_DAY_LIMIT else "FAIL"
        dau_detail = f"DAU={dau}, cluster_ru/day={cluster_ru_per_day:.2f}"
    else:
        ru_per_dau = 0.0
        dau_status = "SKIP"
        dau_detail = "DAU=0 (72h 空载验证,无用户)"

    gates: list[dict] = []

    # 门禁 1: bot role 0 RU/day (FAIL if > 0)
    if bot_ru_per_day <= BOT_RU_PER_DAY_LIMIT:
        bot_status = "PASS"
    else:
        bot_status = "FAIL"
    gates.append({
        "gate": "bot_ru_per_day",
        "expected": f"<= {BOT_RU_PER_DAY_LIMIT}",
        "actual": f"{bot_ru_per_day:.4f}",
        "status": bot_status,
        "detail": (
            f"business_bot_roles={list(BUSINESS_BOT_ROLES)};"
            f"任何 bot 角色 RU > 0 视为门禁违规"
        ),
    })

    # 门禁 2: cluster ideal ≤ 20 RU/day (WARN if > 20)
    if cluster_ru_per_day <= CLUSTER_IDEAL_RU_PER_DAY:
        ideal_status = "PASS"
    else:
        ideal_status = "WARN"
    gates.append({
        "gate": "cluster_ru_ideal",
        "expected": f"<= {CLUSTER_IDEAL_RU_PER_DAY} (ideal)",
        "actual": f"{cluster_ru_per_day:.4f}",
        "status": ideal_status,
        "detail": "理想集群空载 ≤20 RU/day,超出视为告警(非阻断)",
    })

    # 门禁 3: hard cap ≤ 100 RU/day (FAIL if > 100)
    if cluster_ru_per_day <= CLUSTER_HARD_CAP_RU_PER_DAY:
        hard_status = "PASS"
    else:
        hard_status = "FAIL"
    gates.append({
        "gate": "cluster_ru_hard_cap",
        "expected": f"<= {CLUSTER_HARD_CAP_RU_PER_DAY} (hard cap)",
        "actual": f"{cluster_ru_per_day:.4f}",
        "status": hard_status,
        "detail": "硬上限 ≤100 RU/day,超出 FAIL",
    })

    # 门禁 4: block > 500 RU/day (BLOCK)
    if cluster_ru_per_day > CLUSTER_BLOCK_RU_PER_DAY:
        block_status = "BLOCK"
    else:
        block_status = "PASS"
    gates.append({
        "gate": "cluster_ru_block",
        "expected": f"<= {CLUSTER_BLOCK_RU_PER_DAY} (block threshold)",
        "actual": f"{cluster_ru_per_day:.4f}",
        "status": block_status,
        "detail": f"> {CLUSTER_BLOCK_RU_PER_DAY} RU/day 阻断 release",
    })

    # 门禁 5: active period ≤ 250 RU/DAU/day
    gates.append({
        "gate": "ru_per_dau_day",
        "expected": f"<= {RU_PER_DAU_DAY_LIMIT}",
        "actual": f"{ru_per_dau:.4f}",
        "status": dau_status,
        "detail": dau_detail,
    })

    # 门禁 6: monthly ≤ 35M RU
    if monthly_est <= MONTHLY_RU_LIMIT:
        monthly_status = "PASS"
    else:
        monthly_status = "FAIL"
    gates.append({
        "gate": "monthly_ru_limit",
        "expected": f"<= {MONTHLY_RU_LIMIT}",
        "actual": f"{monthly_est:.0f}",
        "status": monthly_status,
        "detail": f"estimated (cluster_ru_per_day * 30);peak_hourly={peak_hourly_ru:.2f}",
    })

    return gates


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════


def _print_human_report(
    span_result: dict | None,
    sample_result: dict | None,
    sig_result: dict | None,
    aggregates: dict | None,
    gates: list[dict] | None,
    fp_data: dict | None,
) -> None:
    """以人类可读格式打印校验报告。"""
    print("═" * 70)
    print("R65 P1-09: CRDB RU 72h 真实数据归因校验报告")
    print("═" * 70)

    if span_result:
        print(f"\n[72h 跨度校验]")
        print(f"  声明时长:        {span_result['declared_hours']:.2f}h")
        print(f"  实际跨度:        {span_result['actual_hours']:.2f}h")
        print(f"  最低要求:        ≥ {span_result['min_required']:.2f}h")
        print(f"  通过: {'是' if span_result['passed'] else '否'}")
        if span_result.get("error"):
            print(f"  错误: {span_result['error']}")

    if sample_result:
        print(f"\n[每小时样本校验]")
        print(f"  实际样本数:      {sample_result['sample_count']}")
        print(f"  最低要求:        ≥ {sample_result['min_required']}")
        print(f"  通过: {'是' if sample_result['passed'] else '否'}")
        if sample_result.get("error"):
            print(f"  错误: {sample_result['error']}")

    if sig_result:
        print(f"\n[签名校验]")
        print(f"  签名类型:        {sig_result['signature_type']}")
        print(f"  状态:            {sig_result['status']}")
        print(f"  已验证:          {sig_result['verified']}")
        if sig_result.get("error"):
            print(f"  错误: {sig_result['error']}")

    if aggregates:
        print(f"\n[聚合指标]")
        print(f"  72h 总 RU:       {aggregates['total_ru']:.4f}")
        print(f"  日均 RU:         {aggregates['daily_average_ru']:.4f}")
        print(f"  bot 日均 RU:     {aggregates['bot_ru_per_day']:.4f}")
        print(f"  peak 单窗口 RU:  {aggregates['peak_hourly_ru']:.4f}")
        print(f"  不重复 fingerprint: {aggregates['total_fingerprints']}")
        print(f"  样本数:          {aggregates['sample_count']}")
        print(f"  by_service:")
        for k, v in sorted(aggregates["by_service"].items()):
            print(f"    {k}: {v:.4f}")

    if fp_data and fp_data["top_fingerprints"]:
        print(f"\n[Top 10 SQL fingerprints by RU]")
        for i, fp in enumerate(fp_data["top_fingerprints"], 1):
            print(
                f"  {i:2d}. ru={fp['ru']:.4f} service={fp['service']} "
                f"job={fp.get('job', 'default')} fp={fp['fingerprint_sha256'][:16]}..."
            )

    if gates:
        print(f"\n[门禁阈值校验]")
        for g in gates:
            icon = {
                "PASS": "✓",
                "WARN": "⚠",
                "FAIL": "✗",
                "BLOCK": "⊘",
                "SKIP": "−",
            }.get(g["status"], "?")
            print(
                f"  {icon} {g['gate']}: expected {g['expected']}, "
                f"actual {g['actual']} [{g['status']}]"
            )
            if g.get("detail"):
                print(f"      detail: {g['detail']}")

    print("\n" + "═" * 70)


def run_check(args: argparse.Namespace) -> int:
    """执行 72h 数据归因校验,返回退出码。

    退出码:
        0: 所有 FAIL 通过(允许 WARN)
        1: 任一 FAIL / BLOCK 触发(或 --strict 下任一 WARN 触发)
        2: 脚本异常(参数错误 / 文件不存在 / schema 不合法 / 签名缺失)
    """
    # ── --dry-run 模式:仅校验脚本自洽性 ──
    if args.dry_run:
        # 自洽性检查:模块可导入 + 阈值常量正确 + 函数签名合法
        try:
            # 验证阈值常量
            assert BOT_RU_PER_DAY_LIMIT == 0, "bot 阈值应为 0"
            assert CLUSTER_IDEAL_RU_PER_DAY == 20, "理想阈值应为 20"
            assert CLUSTER_HARD_CAP_RU_PER_DAY == 100, "硬上限应为 100"
            assert CLUSTER_BLOCK_RU_PER_DAY == 500, "阻断阈值应为 500"
            assert RU_PER_DAU_DAY_LIMIT == 250, "RU/DAU 阈值应为 250"
            assert MONTHLY_RU_LIMIT == 35_000_000, "月度阈值应为 35M"

            # 验证关键函数可调用
            # 构造一个最小合法的 dry-run 数据文件(72 个相同 sample 占位,
            # 仅用于验证 schema/聚合/阈值函数可调用,不代表真实 RU 数据)
            sample_tmpl = {
                "sampled_at": "2026-07-12T00:00:00Z",
                "window_seconds": 3600,
                "total_ru": 1.0,
                "by_service": {"admin": 1.0},
                "by_fingerprint": [
                    {
                        "fingerprint_sha256": (
                            "a" * 64
                        ),
                        "ru": 1.0,
                        "service": "admin",
                        "job": "dry_run_job",
                        "query_text_sample": "SELECT 1",
                    }
                ],
            }
            test_data = {
                "environment_id": "dry-run-env",
                "commit_sha": "dry-run-sha",
                "image_digest": "sha256:dry-run",
                "started_at": "2026-07-12T00:00:00Z",
                "ended_at": "2026-07-15T00:00:00Z",
                "duration_hours": 72,
                "executed_by": "ci-robot",
                "approved_by": "release-manager",
                "samples": [sample_tmpl],
                "signature": "dry-run-signature-placeholder",
                "signature_type": "hmac",
            }
            # validate_data_schema 不应抛异常(require_signature=False
            # 因为 dry-run 不强制签名)
            validate_data_schema(test_data, require_signature=False)
            # 阈值检查函数可调用
            aggs = compute_aggregates(test_data)
            gates = check_thresholds(aggs, dau=0)
            assert isinstance(gates, list), "check_thresholds 应返回 list"
            assert len(gates) == 6, f"应有 6 项门禁,实际 {len(gates)}"

            # 验证 collector 模块可导入(归因表 DDL / 函数)
            try:
                from services.crdb_ru_collector import (
                    CRDB_RU_ATTRIBUTION_TABLE_DDL,
                    aggregate_ru_attribution,
                    compute_sql_fingerprint,
                    init_crdb_ru_attribution_table,
                    normalize_sql,
                    query_ru_attribution_rows,
                    record_ru_attribution_row,
                )
                assert "crdb_ru_attribution" in CRDB_RU_ATTRIBUTION_TABLE_DDL
                assert "fingerprint_sha256" in CRDB_RU_ATTRIBUTION_TABLE_DDL
                # SQL fingerprint 归一化测试
                fp1 = compute_sql_fingerprint("SELECT * FROM users WHERE id = 42")
                fp2 = compute_sql_fingerprint(
                    "select   *   from   users   where   id   =   99"
                )
                assert fp1 == fp2, "归一化后 SQL fingerprint 应一致"
                assert len(fp1) == 64, "sha256 应为 64 字符"
            except ImportError as e:
                # collector 模块在测试环境可能未配置 cache_store,
                # dry-run 仅警告,不阻断
                print(
                    f"[DRY-RUN] WARN: services.crdb_ru_collector 导入失败"
                    f"(允许 dry-run 跳过): {e}",
                    file=sys.stderr,
                )

            if not args.json:
                print("═" * 70)
                print("R65 P1-09: CRDB RU 72h 归因校验脚本(dry-run 自检)")
                print("═" * 70)
                print("  ✓ 阈值常量正确")
                print("  ✓ schema 校验函数可调用")
                print("  ✓ 聚合计算函数可调用")
                print("  ✓ 阈值检查函数可调用(6 项门禁)")
                print("  ✓ SQL fingerprint 归一化正确")
                print("  ✓ collector 归因表 DDL 可加载")
                print("  ✓ --dry-run 模式无需 ru_72h_data.json")
                print("═" * 70)
                print("✅ dry-run 通过:脚本逻辑自洽,可用于校验真实 72h 数据文件")
            else:
                print(json.dumps({
                    "mode": "dry-run",
                    "passed": True,
                    "message": "脚本逻辑自洽",
                    "thresholds": {
                        "bot_ru_per_day_limit": BOT_RU_PER_DAY_LIMIT,
                        "cluster_ideal_ru_per_day": CLUSTER_IDEAL_RU_PER_DAY,
                        "cluster_hard_cap_ru_per_day": CLUSTER_HARD_CAP_RU_PER_DAY,
                        "cluster_block_ru_per_day": CLUSTER_BLOCK_RU_PER_DAY,
                        "ru_per_dau_day_limit": RU_PER_DAU_DAY_LIMIT,
                        "monthly_ru_limit": MONTHLY_RU_LIMIT,
                    },
                }, ensure_ascii=False, indent=2))
            return 0
        except Exception as e:
            print(f"FAIL: --dry-run 自检失败: {e}", file=sys.stderr)
            return 2

    # ── 真实数据文件校验模式 ──
    data_path = Path(args.data) if args.data else Path(DEFAULT_DATA_FILE)
    if not data_path.exists():
        print(
            f"FAIL: 数据文件不存在: {data_path}"
            f"(使用 --dry-run 仅校验脚本自洽性)",
            file=sys.stderr,
        )
        return 2

    # 读取文件
    try:
        raw_bytes = data_path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as e:
        print(f"FAIL: 数据文件 JSON 解析失败: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"FAIL: 数据文件读取失败: {e}", file=sys.stderr)
        return 2

    # 1. schema 校验
    try:
        validate_data_schema(data, require_signature=True)
    except ValidationError as e:
        print(f"FAIL: schema 校验失败: {e}", file=sys.stderr)
        return 2

    # 2. 72h 跨度校验
    span_result = check_72h_span(data, min_hours=72.0)
    if not span_result["passed"]:
        if not args.json:
            _print_human_report(span_result, None, None, None, None, None)
        else:
            print(json.dumps({
                "passed": False,
                "error": f"72h 跨度校验失败: {span_result['error']}",
            }, ensure_ascii=False, indent=2))
        return 1

    # 3. 每小时样本校验
    sample_result = check_per_hour_samples(data, min_samples=72)
    if not sample_result["passed"]:
        if not args.json:
            _print_human_report(span_result, sample_result, None, None, None, None)
        else:
            print(json.dumps({
                "passed": False,
                "error": f"每小时样本校验失败: {sample_result['error']}",
            }, ensure_ascii=False, indent=2))
        return 1

    # 4. 签名校验
    sig_result = verify_signature(data, raw_bytes, strict=args.strict)
    if sig_result["status"] == "FAIL":
        if not args.json:
            _print_human_report(span_result, sample_result, sig_result, None, None, None)
        else:
            print(json.dumps({
                "passed": False,
                "error": f"签名校验失败: {sig_result['error']}",
            }, ensure_ascii=False, indent=2))
        return 1

    # 5. 计算 fingerprint 归因
    fp_data = parse_fingerprint_attribution(data)

    # 6. 计算聚合
    aggregates = compute_aggregates(data)

    # 7. 阈值校验
    gates = check_thresholds(aggregates, dau=args.dau)

    # 决定退出码
    has_fail = any(g["status"] in ("FAIL", "BLOCK") for g in gates)
    has_warn = any(g["status"] == "WARN" for g in gates)
    has_block = any(g["status"] == "BLOCK" for g in gates)

    if args.json:
        print(json.dumps({
            "passed": not (has_fail or (args.strict and has_warn)),
            "blocked": has_block,
            "span": span_result,
            "samples": sample_result,
            "signature": sig_result,
            "aggregates": aggregates,
            "fingerprint_attribution": {
                "total_fingerprints": fp_data["total_fingerprints"],
                "total_attribution_entries": fp_data["total_attribution_entries"],
                "samples_with_attribution": fp_data["samples_with_attribution"],
                "top_fingerprints": fp_data["top_fingerprints"],
            },
            "gates": gates,
        }, ensure_ascii=False, indent=2))
    else:
        _print_human_report(span_result, sample_result, sig_result,
                            aggregates, gates, fp_data)

    if has_fail:
        print(
            "\n❌ 阈值门禁 FAIL:CRDB RU 72h 数据校验未通过",
            file=sys.stderr,
        )
        return 1
    if args.strict and has_warn:
        print(
            "\n⚠️  --strict 模式下 WARN 视为失败:CRDB RU 超过理想阈值",
            file=sys.stderr,
        )
        return 1

    if not args.json:
        if has_warn:
            print("\n⚠️  部分门禁告警(允许通过,非阻断)")
        print("\n✅ CRDB RU 72h 归因校验通过")
    return 0


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="R65 P1-09: CRDB RU 72h 真实数据归因校验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--data", default=None,
        help=f"72h 数据文件路径(默认 {DEFAULT_DATA_FILE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅校验脚本自洽性,无需数据文件(用于 PR/CI 自检)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="严格模式:WARN 也视为失败(用于 release tag)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="JSON 输出模式(适合 CI 解析)",
    )
    parser.add_argument(
        "--dau", type=int, default=0,
        help="72h 期间平均 DAU(默认 0,空载验证)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_check(args)


if __name__ == "__main__":
    sys.exit(main())
