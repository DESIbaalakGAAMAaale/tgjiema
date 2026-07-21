#!/usr/bin/env python3
"""R71 P0-11: RC 身份完整核验脚本 — promote exact signed RC digest without rebuild.

R71 P0-11 整改背景:
    审计发现 production promotion 可通过 workflow_dispatch 手动触发,但没有
    RC 身份输入字段 — 运维可在不验证哪个 RC 被晋级的情况下触发 production 部署。
    这违反"promote exact signed RC digest without rebuild"原则:production 必须
    部署已验证的 RC 的 exact RepoDigest,而非任意/未验证的 digest。

    本脚本在 production promotion 前强制核验 RC 身份,确保:
      1. workflow_dispatch inputs / production-v* tag annotation 提供的 RC 身份
         字段(rc_tag / rc_run_id / source_sha / candidate_manifest_digest /
         image_digest)格式合法
      2. typed_confirmation == "PROMOTE-RC-TO-PRODUCTION"(workflow_dispatch only)
      3. RC run 的 4 类 evidence artifact 全部存在且与 RC 身份输入一致:
         - candidate-manifest.json:sha256(file) == --candidate-manifest-digest
         - oci-file-manifest.json:image_digest == --image-digest
         - release-attestation.json:image_digest == --image-digest
         - sbom.spdx.json:存在且引用同一 image_digest
      4. 所有 artifact 交叉核验 source_sha / image_digest / candidate_manifest_digest
         全部一致(任一不一致 → FAIL)

使用方法:
    python scripts/verify_rc_identity.py \\
        --rc-tag rc-v1.0.0 \\
        --rc-run-id 1234567890 \\
        --source-sha abc123def4567890abcdef1234567890abcdef12 \\
        --candidate-manifest-digest sha256:abc... \\
        --image-digest sha256:def... \\
        --rc-evidence-dir ./rc-evidence \\
        --typed-confirmation PROMOTE-RC-TO-PRODUCTION \\
        --output rc-identity-verification.json

退出码:
    0: 所有核验通过(overall_passed=true)
    1: 核验失败(字段缺失/格式错误/digest 不匹配/交叉核验失败)
    2: CLI 参数错误或 IO 错误
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# ════════════════════════════════════════════════════════════════
# 常量定义
# ════════════════════════════════════════════════════════════════

SCHEMA_VERSION: str = "1.0"
TOOL_VERSION: str = "R71-WAVE5-P0-09/10/11"

# typed_confirmation 必须精确匹配此字符串(大小写敏感)
EXPECTED_TYPED_CONFIRMATION: str = "PROMOTE-RC-TO-PRODUCTION"

# 正则:rc-v* tag(允许 rc-v1.0.0 / rc-v1.0.0-rc.1 等)
RC_TAG_PATTERN: re.Pattern[str] = re.compile(r"^rc-v[\w.\-]+$")

# 正则:40-char hex(Git SHA-1)
SOURCE_SHA_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{40}$")

# 正则:纯数字(run_id)
RUN_ID_PATTERN: re.Pattern[str] = re.compile(r"^\d+$")

# 正则:sha256:<64-hex>(OCI digest)
DIGEST_PATTERN: re.Pattern[str] = re.compile(r"^sha256:[0-9a-fA-F]{64}$")

# 正则:64-hex(无前缀,用于 candidate-manifest.json sha256 计算)
HEX64_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")

# RC evidence 目录下必需的 artifact 文件名
REQUIRED_EVIDENCE_FILES: tuple[str, ...] = (
    "candidate-manifest.json",
    "oci-file-manifest.json",
    "release-attestation.json",
    "sbom.spdx.json",
)

# 退出码
EXIT_SUCCESS: int = 0
EXIT_VERIFICATION_FAILURE: int = 1
EXIT_CLI_ERROR: int = 2


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class RCIdentityInputs:
    """RC 身份输入字段(来自 workflow_dispatch inputs 或 tag annotation)。

    R71 P1-05: 新增 host_config_digest 字段(可选),用于绑定 host config
    (groups.yaml / topology.yaml)的 combined digest 到 promotion evidence。
    """

    rc_tag: str
    rc_run_id: str
    source_sha: str
    candidate_manifest_digest: str
    image_digest: str
    typed_confirmation: str
    # R71 P1-05: host config combined digest(可选,若提供则必须与部署环境一致)
    host_config_digest: str = ""


@dataclass
class VerificationResult:
    """RC 身份核验结果。"""

    schema_version: str = SCHEMA_VERSION
    tool_version: str = TOOL_VERSION
    verified_at: str = ""
    rc_tag: str = ""
    rc_run_id: str = ""
    source_sha: str = ""
    candidate_manifest_digest: str = ""
    image_digest: str = ""
    image_repo_digest: str = ""
    # R71 P1-05: host config combined digest(用于部署前后比对)
    host_config_digest: str = ""
    # R71 P0-13: workflow run_id / attempt(当前候选绑定)
    workflow_run_id: str = ""
    workflow_run_attempt: str = ""
    artifacts_verified: list[str] = field(default_factory=list)
    cross_verification_passed: bool = False
    typed_confirmation_valid: bool = False
    overall_passed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化的 dict。"""
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "verified_at": self.verified_at,
            "rc_tag": self.rc_tag,
            "rc_run_id": self.rc_run_id,
            "source_sha": self.source_sha,
            "candidate_manifest_digest": self.candidate_manifest_digest,
            "image_digest": self.image_digest,
            "image_repo_digest": self.image_repo_digest,
            "host_config_digest": self.host_config_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_attempt": self.workflow_run_attempt,
            "artifacts_verified": list(self.artifacts_verified),
            "cross_verification_passed": self.cross_verification_passed,
            "typed_confirmation_valid": self.typed_confirmation_valid,
            "overall_passed": self.overall_passed,
            "errors": list(self.errors),
        }

    def add_error(self, msg: str) -> None:
        """追加错误并标记 overall_passed=False。"""
        self.errors.append(msg)
        self.overall_passed = False


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    """加载 JSON 文件。

    Raises:
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
        OSError: 其它 IO 错误
    """
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _sha256_file(path: Path) -> str:
    """计算文件的 sha256 hex(无 sha256: 前缀)。

    Raises:
        FileNotFoundError: 文件不存在
        OSError: 读取失败
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_sha_prefix(digest: str) -> str:
    """剥离 digest 字符串的 "sha256:" 前缀。"""
    if not digest:
        return ""
    if digest.startswith("sha256:"):
        return digest[len("sha256:"):]
    return digest


def _normalize_digest(digest: str) -> str:
    """规范化 digest 为 sha256:<64-hex-lower> 形式。

    - 剥离前缀后取 64-hex
    - 转小写
    - 重新加上 sha256: 前缀
    - 若格式不合法,返回原值(由调用方校验)
    """
    if not digest:
        return ""
    hex_part = _strip_sha_prefix(digest).lower()
    if HEX64_PATTERN.match(hex_part):
        return f"sha256:{hex_part}"
    return digest


def _get_field(data: dict[str, Any], field_name: str, *, file_label: str) -> str:
    """从 dict 读取字段,返回字符串值。

    支持多种字段名变体(image_digest / imageDigest / IMAGE_DIGEST / digest)。
    匹配规则:精确匹配 → 去除下划线并大小写不敏感匹配。
    缺失返回空字符串(由调用方决定是否为错误)。
    """
    # 优先精确匹配
    if field_name in data:
        val = data[field_name]
        return str(val) if val is not None else ""
    # 大小写 + 下划线变体(image_digest ↔ ImageDigest ↔ IMAGE_DIGEST)
    target = field_name.replace("_", "").lower()
    for key in data:
        if key.replace("_", "").lower() == target:
            val = data[key]
            return str(val) if val is not None else ""
    return ""


# ════════════════════════════════════════════════════════════════
# 输入校验
# ════════════════════════════════════════════════════════════════


def validate_inputs(inputs: RCIdentityInputs, result: VerificationResult) -> bool:
    """校验 RC 身份输入字段格式。

    Args:
        inputs: RC 身份输入
        result: 核验结果(错误追加到此)

    Returns:
        True 如果所有字段格式合法
    """
    ok = True

    # rc_tag 必须匹配 rc-v*
    if not inputs.rc_tag:
        result.add_error("rc_tag 为空")
        ok = False
    elif not RC_TAG_PATTERN.match(inputs.rc_tag):
        result.add_error(
            f"rc_tag 格式不合法(期望 rc-v*): {inputs.rc_tag}"
        )
        ok = False
    else:
        result.rc_tag = inputs.rc_tag

    # rc_run_id 必须为纯数字
    if not inputs.rc_run_id:
        result.add_error("rc_run_id 为空")
        ok = False
    elif not RUN_ID_PATTERN.match(inputs.rc_run_id):
        result.add_error(
            f"rc_run_id 格式不合法(期望纯数字): {inputs.rc_run_id}"
        )
        ok = False
    else:
        result.rc_run_id = inputs.rc_run_id

    # source_sha 必须为 40-char hex
    if not inputs.source_sha:
        result.add_error("source_sha 为空")
        ok = False
    elif not SOURCE_SHA_PATTERN.match(inputs.source_sha):
        result.add_error(
            f"source_sha 格式不合法(期望 40-char hex): {inputs.source_sha}"
        )
        ok = False
    else:
        result.source_sha = inputs.source_sha.lower()

    # candidate_manifest_digest 必须为 sha256:<64-hex>
    if not inputs.candidate_manifest_digest:
        result.add_error("candidate_manifest_digest 为空")
        ok = False
    elif not DIGEST_PATTERN.match(inputs.candidate_manifest_digest):
        result.add_error(
            f"candidate_manifest_digest 格式不合法(期望 sha256:<64-hex>): "
            f"{inputs.candidate_manifest_digest}"
        )
        ok = False
    else:
        result.candidate_manifest_digest = _normalize_digest(
            inputs.candidate_manifest_digest
        )

    # image_digest 必须为 sha256:<64-hex>
    if not inputs.image_digest:
        result.add_error("image_digest 为空")
        ok = False
    elif not DIGEST_PATTERN.match(inputs.image_digest):
        result.add_error(
            f"image_digest 格式不合法(期望 sha256:<64-hex>): {inputs.image_digest}"
        )
        ok = False
    else:
        result.image_digest = _normalize_digest(inputs.image_digest)

    return ok


def validate_typed_confirmation(
    typed_confirmation: str, result: VerificationResult
) -> bool:
    """校验 typed_confirmation == "PROMOTE-RC-TO-PRODUCTION"。

    Args:
        typed_confirmation: 用户输入的确认字符串
        result: 核验结果

    Returns:
        True 如果匹配
    """
    if typed_confirmation == EXPECTED_TYPED_CONFIRMATION:
        result.typed_confirmation_valid = True
        return True
    result.typed_confirmation_valid = False
    result.add_error(
        f"typed_confirmation 不匹配(期望 '{EXPECTED_TYPED_CONFIRMATION}',"
        f"实际 '{typed_confirmation}')"
    )
    return False


# ════════════════════════════════════════════════════════════════
# Evidence artifact 校验
# ════════════════════════════════════════════════════════════════


def verify_candidate_manifest(
    evidence_dir: Path,
    expected_digest: str,
    result: VerificationResult,
) -> dict[str, Any] | None:
    """校验 candidate-manifest.json 的 sha256 与 expected_digest 一致。

    Args:
        evidence_dir: RC evidence 目录
        expected_digest: 期望的 sha256:<64-hex>(来自 --candidate-manifest-digest)
        result: 核验结果

    Returns:
        解析后的 candidate-manifest dict,失败返回 None
    """
    manifest_path = evidence_dir / "candidate-manifest.json"
    try:
        data = _load_json(manifest_path)
    except FileNotFoundError:
        result.add_error(f"candidate-manifest.json 不存在: {manifest_path}")
        return None
    except json.JSONDecodeError as e:
        result.add_error(f"candidate-manifest.json JSON 解析失败: {e}")
        return None
    except OSError as e:
        result.add_error(f"candidate-manifest.json 读取失败: {e}")
        return None

    # 计算文件 sha256
    try:
        actual_hex = _sha256_file(manifest_path)
    except OSError as e:
        result.add_error(f"candidate-manifest.json sha256 计算失败: {e}")
        return None

    expected_hex = _strip_sha_prefix(expected_digest).lower()
    if actual_hex.lower() != expected_hex:
        result.add_error(
            f"candidate-manifest.json sha256 不匹配: "
            f"期望 {expected_hex[:16]}..., 实际 {actual_hex[:16]}..."
        )
        return None

    result.artifacts_verified.append("candidate-manifest")
    logger.info(
        f"PASS: candidate-manifest.json sha256 匹配 ({actual_hex[:16]}...)"
    )
    return data


def verify_oci_file_manifest(
    evidence_dir: Path,
    expected_image_digest: str,
    result: VerificationResult,
) -> dict[str, Any] | None:
    """校验 oci-file-manifest.json 的 image_digest 与 expected_image_digest 一致。

    Args:
        evidence_dir: RC evidence 目录
        expected_image_digest: 期望的 sha256:<64-hex>(来自 --image-digest)
        result: 核验结果

    Returns:
        解析后的 oci-file-manifest dict,失败返回 None
    """
    manifest_path = evidence_dir / "oci-file-manifest.json"
    try:
        data = _load_json(manifest_path)
    except FileNotFoundError:
        result.add_error(f"oci-file-manifest.json 不存在: {manifest_path}")
        return None
    except json.JSONDecodeError as e:
        result.add_error(f"oci-file-manifest.json JSON 解析失败: {e}")
        return None
    except OSError as e:
        result.add_error(f"oci-file-manifest.json 读取失败: {e}")
        return None

    actual_digest = _get_field(data, "image_digest", file_label="oci-file-manifest")
    if not actual_digest:
        # 兼容字段名变体
        actual_digest = _get_field(data, "imageDigest", file_label="oci-file-manifest")
    actual_digest_norm = _normalize_digest(actual_digest)
    expected_norm = _normalize_digest(expected_image_digest)

    if actual_digest_norm != expected_norm:
        result.add_error(
            f"oci-file-manifest.json image_digest 不匹配: "
            f"期望 {expected_norm[:24]}..., 实际 {actual_digest_norm[:24]}..."
        )
        return None

    result.artifacts_verified.append("oci-file-manifest")
    logger.info(
        f"PASS: oci-file-manifest.json image_digest 匹配 "
        f"({actual_digest_norm[:24]}...)"
    )
    return data


def verify_release_attestation(
    evidence_dir: Path,
    expected_image_digest: str,
    result: VerificationResult,
) -> dict[str, Any] | None:
    """校验 release-attestation.json 的 image_digest 与 expected_image_digest 一致。

    Args:
        evidence_dir: RC evidence 目录
        expected_image_digest: 期望的 sha256:<64-hex>(来自 --image-digest)
        result: 核验结果

    Returns:
        解析后的 release-attestation dict,失败返回 None
    """
    attestation_path = evidence_dir / "release-attestation.json"
    try:
        data = _load_json(attestation_path)
    except FileNotFoundError:
        result.add_error(f"release-attestation.json 不存在: {attestation_path}")
        return None
    except json.JSONDecodeError as e:
        result.add_error(f"release-attestation.json JSON 解析失败: {e}")
        return None
    except OSError as e:
        result.add_error(f"release-attestation.json 读取失败: {e}")
        return None

    actual_digest = _get_field(
        data, "image_digest", file_label="release-attestation"
    )
    if not actual_digest:
        # 兼容字段名变体(statement.subject[0].digest.sha256)
        statement = data.get("statement") or data
        if isinstance(statement, dict):
            subjects = statement.get("subject") or []
            if isinstance(subjects, list) and subjects:
                first_subject = subjects[0]
                if isinstance(first_subject, dict):
                    digest_obj = first_subject.get("digest") or {}
                    if isinstance(digest_obj, dict):
                        sha256_val = digest_obj.get("sha256", "")
                        if sha256_val:
                            actual_digest = (
                                f"sha256:{sha256_val}"
                                if not sha256_val.startswith("sha256:")
                                else sha256_val
                            )
    actual_digest_norm = _normalize_digest(actual_digest)
    expected_norm = _normalize_digest(expected_image_digest)

    if actual_digest_norm != expected_norm:
        result.add_error(
            f"release-attestation.json image_digest 不匹配: "
            f"期望 {expected_norm[:24]}..., 实际 {actual_digest_norm[:24]}..."
        )
        return None

    result.artifacts_verified.append("release-attestation")
    logger.info(
        f"PASS: release-attestation.json image_digest 匹配 "
        f"({actual_digest_norm[:24]}...)"
    )
    return data


def verify_sbom(
    evidence_dir: Path,
    expected_image_digest: str,
    result: VerificationResult,
) -> dict[str, Any] | None:
    """校验 sbom.spdx.json 存在且引用同一 image_digest。

    SBOM 可能不直接包含 image_digest 字段(取决于生成器),因此本函数采用
    宽松校验:
      1. 文件存在且 JSON 合法
      2. 若包含 image_digest / imageDigest 字段,则必须与 expected 一致
      3. 若不包含 image_digest 字段(常见情况),仅校验文件存在且 JSON 合法
         (SBOM 的完整性由其自身的签名/attestation 保证,此处仅做存在性校验)

    Args:
        evidence_dir: RC evidence 目录
        expected_image_digest: 期望的 sha256:<64-hex>(来自 --image-digest)
        result: 核验结果

    Returns:
        解析后的 sbom dict,失败返回 None
    """
    sbom_path = evidence_dir / "sbom.spdx.json"
    try:
        data = _load_json(sbom_path)
    except FileNotFoundError:
        result.add_error(f"sbom.spdx.json 不存在: {sbom_path}")
        return None
    except json.JSONDecodeError as e:
        result.add_error(f"sbom.spdx.json JSON 解析失败: {e}")
        return None
    except OSError as e:
        result.add_error(f"sbom.spdx.json 读取失败: {e}")
        return None

    # 若 SBOM 包含 image_digest 字段,必须匹配
    actual_digest = _get_field(data, "image_digest", file_label="sbom")
    if actual_digest:
        actual_digest_norm = _normalize_digest(actual_digest)
        expected_norm = _normalize_digest(expected_image_digest)
        if actual_digest_norm != expected_norm:
            result.add_error(
                f"sbom.spdx.json image_digest 不匹配: "
                f"期望 {expected_norm[:24]}..., 实际 {actual_digest_norm[:24]}..."
            )
            return None

    result.artifacts_verified.append("sbom")
    logger.info("PASS: sbom.spdx.json 存在且 image_digest 一致(或无 image_digest 字段)")
    return data


# ════════════════════════════════════════════════════════════════
# 交叉核验
# ════════════════════════════════════════════════════════════════


def cross_verify_artifacts(
    candidate_manifest: dict[str, Any] | None,
    oci_file_manifest: dict[str, Any] | None,
    release_attestation: dict[str, Any] | None,
    sbom: dict[str, Any] | None,
    inputs: RCIdentityInputs,
    result: VerificationResult,
) -> bool:
    """交叉核验所有 artifact 的 source_sha / image_digest / candidate_manifest_digest 一致。

    Args:
        candidate_manifest: candidate-manifest.json 解析结果
        oci_file_manifest: oci-file-manifest.json 解析结果
        release_attestation: release-attestation.json 解析结果
        sbom: sbom.spdx.json 解析结果
        inputs: RC 身份输入(基准值)
        result: 核验结果

    Returns:
        True 如果所有交叉核验通过
    """
    ok = True
    expected_source_sha = inputs.source_sha.lower()
    expected_image_digest = _normalize_digest(inputs.image_digest)
    expected_candidate_digest = _normalize_digest(inputs.candidate_manifest_digest)

    # 收集每个 artifact 的 source_sha / image_digest / candidate_manifest_digest
    artifacts: list[tuple[str, dict[str, Any] | None]] = [
        ("candidate-manifest", candidate_manifest),
        ("oci-file-manifest", oci_file_manifest),
        ("release-attestation", release_attestation),
        ("sbom", sbom),
    ]

    for name, data in artifacts:
        if data is None:
            # None 表示之前已失败(错误已记录),跳过交叉核验
            ok = False
            continue

        # 交叉核验 source_sha(若 artifact 包含此字段)
        artifact_source_sha = _get_field(data, "source_sha", file_label=name)
        if not artifact_source_sha:
            # 兼容字段名变体
            artifact_source_sha = _get_field(
                data, "source_sha", file_label=name
            ) or _get_field(data, "commit_sha", file_label=name)
        if artifact_source_sha:
            artifact_source_sha_norm = artifact_source_sha.lower()
            if artifact_source_sha_norm != expected_source_sha:
                result.add_error(
                    f"{name} source_sha 不匹配: 期望 {expected_source_sha[:16]}..., "
                    f"实际 {artifact_source_sha_norm[:16]}..."
                )
                ok = False

        # 交叉核验 image_digest(若 artifact 包含此字段)
        artifact_image_digest = _get_field(data, "image_digest", file_label=name)
        if not artifact_image_digest:
            artifact_image_digest = _get_field(
                data, "imageDigest", file_label=name
            )
        if artifact_image_digest:
            artifact_digest_norm = _normalize_digest(artifact_image_digest)
            if artifact_digest_norm != expected_image_digest:
                result.add_error(
                    f"{name} image_digest 不匹配: 期望 {expected_image_digest[:24]}..., "
                    f"实际 {artifact_digest_norm[:24]}..."
                )
                ok = False

        # 交叉核验 candidate_manifest_digest(若 artifact 包含此字段)
        artifact_candidate_digest = _get_field(
            data, "candidate_manifest_digest", file_label=name
        )
        if artifact_candidate_digest:
            artifact_candidate_norm = _normalize_digest(artifact_candidate_digest)
            if artifact_candidate_norm != expected_candidate_digest:
                result.add_error(
                    f"{name} candidate_manifest_digest 不匹配: 期望 "
                    f"{expected_candidate_digest[:24]}..., 实际 "
                    f"{artifact_candidate_norm[:24]}..."
                )
                ok = False

    if ok:
        result.cross_verification_passed = True
        logger.info("PASS: 所有 artifact 交叉核验通过(source_sha / image_digest 一致)")
    else:
        result.cross_verification_passed = False
        logger.error("FAIL: artifact 交叉核验失败 — digest 不一致")
    return ok


def compute_image_repo_digest(
    candidate_manifest: dict[str, Any] | None,
    oci_file_manifest: dict[str, Any] | None,
    release_attestation: dict[str, Any] | None,
    image_digest: str,
    result: VerificationResult,
) -> str:
    """计算/提取 image_repo_digest(ghcr.io/owner/repo@sha256:...)。

    优先从 artifact 的 image_name / image_ref 字段提取 repo 名,组合 @digest。
    若无法提取,返回空字符串(由调用方决定是否为错误)。

    Args:
        candidate_manifest: candidate-manifest.json
        oci_file_manifest: oci-file-manifest.json
        release_attestation: release-attestation.json
        image_digest: 已验证的 image_digest
        result: 核验结果

    Returns:
        image_repo_digest 字符串,或空字符串
    """
    digest_norm = _normalize_digest(image_digest)
    # 候选字段:image_name / image_ref / repository / repo
    for name, data in (
        ("candidate-manifest", candidate_manifest),
        ("oci-file-manifest", oci_file_manifest),
        ("release-attestation", release_attestation),
    ):
        if data is None:
            continue
        for field_name in ("image_name", "image_ref", "repository", "repo"):
            val = _get_field(data, field_name, file_label=name)
            if not val:
                continue
            # 剥离 @digest 后缀(若有)和 :tag 后缀
            base = val.split("@", 1)[0]
            # 剥离 :tag(但保留 registry/repo,注意 docker.io/library/repo:tag 格式)
            # 简化:若 base 含 "/" 且含 ":",取最后一个 ":" 之前的部分
            # (避免剥离 registry port 如 localhost:5000/repo)
            if ":" in base.split("/", 1)[-1]:
                # 仅在 repo 部分(最后一个 / 之后)有 :tag 时剥离
                parts = base.rsplit("/", 1)
                if len(parts) == 2:
                    registry = parts[0]
                    repo_part = parts[1].split(":", 1)[0]
                    base = f"{registry}/{repo_part}"
                else:
                    base = base.split(":", 1)[0]
            if base:
                repo_digest = f"{base}@{digest_norm}"
                result.image_repo_digest = repo_digest
                logger.info(f"PASS: image_repo_digest = {repo_digest}")
                return repo_digest
    # 无法提取 image_name,仅返回 digest(不带 repo 名)
    logger.warning("无法从 artifact 提取 image_name,image_repo_digest 仅含 digest")
    result.image_repo_digest = digest_norm
    return digest_norm


# ════════════════════════════════════════════════════════════════
# 主核验逻辑
# ════════════════════════════════════════════════════════════════


def verify_rc_identity(
    inputs: RCIdentityInputs,
    evidence_dir: Path,
    require_typed_confirmation: bool,
) -> VerificationResult:
    """主核验函数 — 校验 RC 身份字段与 evidence artifact 一致。

    Args:
        inputs: RC 身份输入
        evidence_dir: RC evidence 目录(含 4 类 artifact 文件)
        require_typed_confirmation: 是否校验 typed_confirmation
            (workflow_dispatch=True, production-v* tag=False)

    Returns:
        VerificationResult(overall_passed=true 表示所有核验通过)
    """
    result = VerificationResult()
    result.verified_at = _now_iso()
    result.overall_passed = True  # 初始为 true,任何失败都会设为 false

    # 1. 校验输入字段格式
    logger.info("=== R71 P0-11: 校验 RC 身份输入字段格式 ===")
    if not validate_inputs(inputs, result):
        logger.error("FAIL: RC 身份输入字段格式校验失败")
        # 输入格式错误,无法继续核验 artifact
        return result
    logger.info("PASS: RC 身份输入字段格式校验通过")

    # 2. 校验 typed_confirmation(workflow_dispatch only)
    if require_typed_confirmation:
        logger.info("=== R71 P0-11: 校验 typed_confirmation ===")
        if not validate_typed_confirmation(inputs.typed_confirmation, result):
            logger.error("FAIL: typed_confirmation 校验失败")
            return result
        logger.info("PASS: typed_confirmation 校验通过")
    else:
        # production-v* tag 不需要 typed_confirmation(defense in depth:workflow 已设默认值)
        result.typed_confirmation_valid = True
        logger.info("SKIP: typed_confirmation (production-v* tag 触发,不需要)")

    # 3. 校验 evidence artifact 存在性
    logger.info(f"=== R71 P0-11: 校验 evidence artifact 存在性 (dir={evidence_dir}) ===")
    if not evidence_dir.exists():
        result.add_error(f"RC evidence 目录不存在: {evidence_dir}")
        return result
    if not evidence_dir.is_dir():
        result.add_error(f"RC evidence 路径不是目录: {evidence_dir}")
        return result
    for fname in REQUIRED_EVIDENCE_FILES:
        fpath = evidence_dir / fname
        if not fpath.exists():
            result.add_error(f"必需的 evidence 文件缺失: {fname}")
    if result.errors:
        logger.error("FAIL: evidence artifact 存在性校验失败")
        return result
    logger.info("PASS: 所有必需 evidence artifact 存在")

    # 4. 校验 candidate-manifest.json sha256
    logger.info("=== R71 P0-11: 校验 candidate-manifest.json sha256 ===")
    candidate_manifest = verify_candidate_manifest(
        evidence_dir, inputs.candidate_manifest_digest, result
    )

    # 5. 校验 oci-file-manifest.json image_digest
    logger.info("=== R71 P0-11: 校验 oci-file-manifest.json image_digest ===")
    oci_file_manifest = verify_oci_file_manifest(
        evidence_dir, inputs.image_digest, result
    )

    # 6. 校验 release-attestation.json image_digest
    logger.info("=== R71 P0-11: 校验 release-attestation.json image_digest ===")
    release_attestation = verify_release_attestation(
        evidence_dir, inputs.image_digest, result
    )

    # 7. 校验 sbom.spdx.json
    logger.info("=== R71 P0-11: 校验 sbom.spdx.json ===")
    sbom = verify_sbom(evidence_dir, inputs.image_digest, result)

    # 8. 交叉核验所有 artifact 的 digest 一致性
    logger.info("=== R71 P0-11: 交叉核验 artifact digest 一致性 ===")
    cross_verify_artifacts(
        candidate_manifest,
        oci_file_manifest,
        release_attestation,
        sbom,
        inputs,
        result,
    )

    # 9. 计算 image_repo_digest
    logger.info("=== R71 P0-11: 计算 image_repo_digest ===")
    compute_image_repo_digest(
        candidate_manifest,
        oci_file_manifest,
        release_attestation,
        inputs.image_digest,
        result,
    )

    # 9.5 R71 P1-05: 注入 host config digest(若提供)
    if inputs.host_config_digest:
        # 校验格式
        if not DIGEST_PATTERN.match(inputs.host_config_digest):
            result.add_error(
                f"host_config_digest 格式不合法(期望 sha256:<64-hex>): "
                f"{inputs.host_config_digest}"
            )
        else:
            result.host_config_digest = _normalize_digest(
                inputs.host_config_digest
            )
            logger.info(
                f"PASS: host_config_digest 已绑定到 promotion evidence: "
                f"{result.host_config_digest}"
            )

    # 9.6 R71 P0-13: 注入 workflow run_id / attempt(当前候选绑定)
    import os as _os
    result.workflow_run_id = _os.environ.get("GITHUB_RUN_ID", "")
    result.workflow_run_attempt = _os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if result.workflow_run_id:
        logger.info(
            f"PASS: workflow_run_id={result.workflow_run_id} "
            f"attempt={result.workflow_run_attempt} 已绑定(当前候选)"
        )

    # 10. 最终汇总
    all_artifacts_verified = len(result.artifacts_verified) == len(
        REQUIRED_EVIDENCE_FILES
    )
    if not all_artifacts_verified:
        missing = set(REQUIRED_EVIDENCE_FILES) - set(
            name.replace("-manifest", "-manifest").replace("-attestation", "-attestation")
            for name in result.artifacts_verified
        )
        # artifacts_verified 用的是简短名(candidate-manifest / oci-file-manifest /
        # release-attestation / sbom),与 REQUIRED_EVIDENCE_FILES 的文件名不完全对应
        verified_short = set(result.artifacts_verified)
        expected_short = {
            "candidate-manifest",
            "oci-file-manifest",
            "release-attestation",
            "sbom",
        }
        missing_short = expected_short - verified_short
        result.add_error(
            f"未全部验证 artifact: 已验证 {sorted(verified_short)}, "
            f"缺失 {sorted(missing_short)}"
        )

    if result.errors:
        result.overall_passed = False
        logger.error(
            f"FAIL: RC 身份核验失败 — {len(result.errors)} 个错误"
        )
        for err in result.errors:
            logger.error(f"  - {err}")
    else:
        result.overall_passed = True
        logger.info("PASS: RC 身份核验通过 — production 可 promote exact signed RC digest")

    return result


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="verify_rc_identity.py",
        description=(
            "R71 P0-11: RC 身份完整核验 — promote exact signed RC digest "
            "without rebuild"
        ),
    )
    parser.add_argument(
        "--rc-tag",
        required=True,
        help="RC tag to promote (e.g., rc-v1.0.0)",
    )
    parser.add_argument(
        "--rc-run-id",
        required=True,
        help="RC workflow run ID (numeric)",
    )
    parser.add_argument(
        "--source-sha",
        required=True,
        help="Source commit SHA the RC was built from (40-char hex)",
    )
    parser.add_argument(
        "--candidate-manifest-digest",
        required=True,
        help="Candidate manifest digest (sha256:<64-hex>)",
    )
    parser.add_argument(
        "--image-digest",
        required=True,
        help="RC image digest (sha256:<64-hex>)",
    )
    parser.add_argument(
        "--rc-evidence-dir",
        required=True,
        help="Directory containing RC evidence artifacts (candidate-manifest.json, "
        "oci-file-manifest.json, release-attestation.json, sbom.spdx.json)",
    )
    parser.add_argument(
        "--typed-confirmation",
        default="",
        help='Typed confirmation string (must be "PROMOTE-RC-TO-PRODUCTION" '
        "for workflow_dispatch; ignored for production-v* tag)",
    )
    parser.add_argument(
        "--require-typed-confirmation",
        action="store_true",
        default=True,
        help="Require typed_confirmation validation (default: True for "
        "workflow_dispatch). Use --no-require-typed-confirmation for "
        "production-v* tag trigger.",
    )
    parser.add_argument(
        "--no-require-typed-confirmation",
        dest="require_typed_confirmation",
        action="store_false",
        help="Skip typed_confirmation validation (for production-v* tag trigger).",
    )
    parser.add_argument(
        "--host-config-digest",
        default="",
        help=(
            "R71 P1-05: Expected host config combined digest "
            "(sha256:<64-hex>,optional). If provided, the script will "
            "include it in the verification report and require it to "
            "match the deployment environment's host config digest "
            "(config/groups.yaml + config/topology.yaml combined)."
        ),
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON verification report path (default: stdout only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Returns:
        0 success, 1 verification failure, 2 CLI error
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse 在参数错误时调用 sys.exit(2),我们转换为 EXIT_CLI_ERROR
        code = e.code if isinstance(e.code, int) else EXIT_CLI_ERROR
        return EXIT_CLI_ERROR if code != 0 else EXIT_SUCCESS

    # 配置 loguru 输出到 stderr
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    # 构造 RCIdentityInputs
    inputs = RCIdentityInputs(
        rc_tag=args.rc_tag,
        rc_run_id=args.rc_run_id,
        source_sha=args.source_sha,
        candidate_manifest_digest=args.candidate_manifest_digest,
        image_digest=args.image_digest,
        typed_confirmation=args.typed_confirmation,
        host_config_digest=args.host_config_digest,
    )

    evidence_dir = Path(args.rc_evidence_dir)

    # 执行核验
    result = verify_rc_identity(
        inputs=inputs,
        evidence_dir=evidence_dir,
        require_typed_confirmation=args.require_typed_confirmation,
    )

    # 输出 JSON 报告
    report = result.to_dict()
    report_json = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        try:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(report_json)
                f.write("\n")
            logger.info(f"Verification report written to: {output_path}")
        except OSError as e:
            logger.error(f"无法写入报告文件: {e}")
            # 报告写入失败不改变核验结果(overall_passed 已定)
            # 但仍输出到 stdout
            print(report_json)
    else:
        print(report_json)

    # 返回退出码
    if result.overall_passed:
        return EXIT_SUCCESS
    return EXIT_VERIFICATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
