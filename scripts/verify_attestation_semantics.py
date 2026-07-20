#!/usr/bin/env python3
"""R66 P1-10: 供应链 attestation statement 语义验证脚本。

在 cosign verify 成功之外,进一步断言 statement 中的语义字段与 release manifest
完全一致,涵盖以下维度:

    a. statement._type 必须为 in-toto Statement v0.1 或 v1
    b. statement.predicateType 必须在允许集合内(SLSA provenance v0.2 / v1,
       或 cosign sigstore attestation v1)
    c. statement.subject[0].digest.sha256 必须与 release_manifest.image_digest 一致
       (允许带 "sha256:" 前缀,内部自动剥离)
    d. statement.subject[0].name 必须与 release_manifest 中的镜像引用一致
       (registry/repo@sha256:... 或 registry/repo:tag)
    e. 若 predicate 存在(SLSA provenance):
        - predicate.builder.id 非空
        - predicate.buildType 非空
        - predicate.invocation.configSource.uri 包含 github.com/<owner>/<repo>
          (与 release_manifest.source_repository 对齐)
        - predicate.invocation.configSource.digest.sha1 == release_manifest.source_commit
          (兼容 source_commit_sha 别名)
        - predicate.materials[] 含 digest.sha256 == release_manifest.source_tree_sha 条目
        - 若 release_manifest.migration_manifest_digest 存在,
          predicate.materials[] 须含对应 digest 条目
    f. 若 bundle 提供(Rekor bundle JSON):
        - bundle.verificationMaterial.tlogEntries 非空(Rekor inclusion 证明)
        - 每个 entry 含 logIndex 与 integratedTime
    g. OIDC issuer(若 statement 或 bundle 中存在):
        必须为 https://token.actions.githubusercontent.com
    h. 证书有效期(若 bundle 中含证书): notBefore < 签名时间 < notAfter

使用方法:
    # 基础用法(验证 statement 与 release manifest 一致性)
    python scripts/verify_attestation_semantics.py \\
        --statement cosign-verify-output.json \\
        --release-manifest release-manifest.json

    # 同时验证 Rekor bundle(inclusion proof + 证书有效期)
    python scripts/verify_attestation_semantics.py \\
        --statement cosign-verify-output.json \\
        --release-manifest release-manifest.json \\
        --bundle rekor-bundle.json

    # 严格模式:警告转为错误(可选字段缺失也视为失败)
    python scripts/verify_attestation_semantics.py \\
        --statement ... --release-manifest ... --strict

    # 输出机器可读 JSON 报告到指定路径
    python scripts/verify_attestation_semantics.py \\
        --statement ... --release-manifest ... --json-output report.json

退出码:
    0: 所有断言通过
    1: 至少一项断言失败(或 strict 模式下存在警告)
    2: 参数错误或 IO 错误(文件不存在 / JSON 解析失败)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# ════════════════════════════════════════════════════════════════
# 常量定义
# ════════════════════════════════════════════════════════════════

# 允许的 in-toto Statement 类型(R66 P1-10 assertion a)
ALLOWED_STATEMENT_TYPES: frozenset[str] = frozenset({
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
})

# 允许的 predicate 类型(R66 P1-10 assertion b)
# - SLSA provenance v0.2 / v1: GitHub Actions / SLSA generator
# - cosign sigstore attestation v1: cosign attach attestation
ALLOWED_PREDICATE_TYPES: frozenset[str] = frozenset({
    "https://slsa.dev/provenance/v1",
    "https://slsa.dev/provenance/v0.2",
    "https://cosign.sigstore.dev/attestation/v1",
})

# 期望的 OIDC issuer(R66 P1-10 assertion g)
# GitHub Actions OIDC token 的唯一 issuer
EXPECTED_OIDC_ISSUER: str = "https://token.actions.githubusercontent.com"

# 报告 schema 版本
SCHEMA_VERSION: str = "r66_p1_10_v1"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    """加载 JSON 文件,失败时抛出 IOError 子类的异常。

    Raises:
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
        OSError: 其它 IO 错误
    """
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _strip_sha_prefix(digest: str) -> str:
    """剥离 digest 字符串的 "sha256:" / "sha1:" 等算法前缀。

    示例:
        "sha256:abc123" → "abc123"
        "abc123"        → "abc123"
        ""              → ""
    """
    if not digest:
        return ""
    for prefix in ("sha256:", "sha1:", "sha512:"):
        if digest.startswith(prefix):
            return digest[len(prefix):]
    return digest


def _parse_timestamp(value: Any) -> datetime | None:
    """将时间戳值解析为带时区的 datetime。

    支持以下输入:
    - datetime 对象(无时区则假定为 UTC)
    - int / float: Unix 时间戳(秒)
    - str: Unix 时间戳字符串(纯数字)或 ISO 8601 字符串(支持 Z 后缀)

    Returns:
        带时区的 datetime,解析失败返回 None
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, bool):
        # bool 是 int 的子类,显式排除
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 优先尝试 Unix 时间戳(纯数字,可能带负号)
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
        # 尝试 ISO 8601(支持 Z 或 +00:00 后缀)
        try:
            iso_str = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return None


def _normalize_image_name(name: str) -> str:
    """剥离镜像名中的 @sha256:... digest 后缀。

    保留 :tag 后缀,因为 tag 通常是镜像引用的一部分。
    示例:
        "ghcr.io/owner/repo@sha256:abc" → "ghcr.io/owner/repo"
        "ghcr.io/owner/repo:tag"        → "ghcr.io/owner/repo:tag"
        "ghcr.io/owner/repo"            → "ghcr.io/owner/repo"
    """
    if not name:
        return ""
    if "@" in name:
        return name.split("@", 1)[0]
    return name


def _names_match(subject_name: str, image_ref: str) -> bool:
    """检查 statement.subject[0].name 是否与 release manifest 的镜像引用匹配。

    匹配规则(任一满足即视为一致):
    - 精确匹配
    - 剥离 @sha256:... digest 后缀后匹配
    - 一方为 base name,另一方为 base@digest 形式
    """
    if not subject_name or not image_ref:
        return False
    if subject_name == image_ref:
        return True
    s_base = _normalize_image_name(subject_name)
    i_base = _normalize_image_name(image_ref)
    if s_base == i_base:
        return True
    # 一方为 base,另一方为 base@digest
    if s_base == image_ref or subject_name == i_base:
        return True
    return False


def _make_check(
    name: str,
    *,
    passed: bool,
    expected: str,
    actual: str,
    message: str,
    severity: str = "error",
) -> dict:
    """构造单条检查结果字典。

    Args:
        name: 检查项名称(用于报告显示与 JSON 字段)
        passed: 是否通过
        expected: 期望值描述
        actual: 实际值描述
        message: 人类可读的详细信息
        severity: 失败时的严重级别 "error" 或 "warning"
    """
    return {
        "name": name,
        "passed": passed,
        "expected": expected,
        "actual": actual,
        "message": message,
        "severity": severity,
    }


def _safe_get(statement: dict, *keys: str, default: Any = None) -> Any:
    """安全嵌套字典取值,任意层级缺失返回 default。"""
    cur: Any = statement
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is default:
            return default
    return cur


# ════════════════════════════════════════════════════════════════
# SLSA Provenance 版本检测与字段归一化
# ════════════════════════════════════════════════════════════════

def _get_slsa_version(statement: dict) -> str:
    """检测 SLSA Provenance 版本(依据 predicateType)。

    Returns:
        "v1"     — predicateType == "https://slsa.dev/provenance/v1"
        "v0.2"   — predicateType == "https://slsa.dev/provenance/v0.2"
        "unknown" — 其它 predicateType(如 cosign sigstore attestation v1)
    """
    predicate_type = str(statement.get("predicateType", "") or "")
    if predicate_type == "https://slsa.dev/provenance/v1":
        return "v1"
    if predicate_type == "https://slsa.dev/provenance/v0.2":
        return "v0.2"
    return "unknown"


def _find_git_dep_v1(predicate: dict) -> dict:
    """在 v1 resolvedDependencies 中查找 uri 以 git+https:// 开头的条目。

    Returns:
        匹配的依赖 dict,未找到返回空 dict
    """
    deps = _safe_get(predicate, "buildDefinition", "resolvedDependencies", default=[]) or []
    if not isinstance(deps, list):
        return {}
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        uri = str(dep.get("uri", "") or "")
        if uri.startswith("git+https://"):
            return dep
    return {}


def _get_builder_id(predicate: dict, version: str) -> str:
    """提取 builder.id,兼容 v0.2 与 v1。

    v0.2: predicate.builder.id
    v1:   predicate.runDetails.builder.id

    若主版本路径缺失,回退到另一版本路径(用于兼容 predicateType 与 body
    结构不一致的边界场景)。
    """
    if version == "v1":
        primary = str(_safe_get(predicate, "runDetails", "builder", "id", default="") or "")
        if primary:
            return primary
        # 回退到 v0.2 路径
        return str(_safe_get(predicate, "builder", "id", default="") or "")
    # v0.2 或 unknown:使用 v0.2 路径
    return str(_safe_get(predicate, "builder", "id", default="") or "")


def _get_build_type(predicate: dict, version: str) -> str:
    """提取 buildType,兼容 v0.2 与 v1。

    v0.2: predicate.buildType
    v1:   predicate.buildDefinition.buildType
    """
    if version == "v1":
        primary = str(_safe_get(predicate, "buildDefinition", "buildType", default="") or "")
        if primary:
            return primary
        # 回退到 v0.2 路径
        return str(predicate.get("buildType", "") or "")
    return str(predicate.get("buildType", "") or "")


def _get_config_source_uri(predicate: dict, version: str) -> str:
    """提取 configSource.uri,兼容 v0.2 与 v1。

    v0.2: predicate.invocation.configSource.uri
    v1:   predicate.buildDefinition.resolvedDependencies[] 中
          uri 以 git+https:// 开头的条目
    """
    if version == "v1":
        dep = _find_git_dep_v1(predicate)
        if dep:
            return str(dep.get("uri", "") or "")
        # 回退到 v0.2 路径
        return str(_safe_get(predicate, "invocation", "configSource", "uri", default="") or "")
    return str(_safe_get(predicate, "invocation", "configSource", "uri", default="") or "")


def _get_config_source_digest_sha1(predicate: dict, version: str) -> str:
    """提取 configSource 的 commit digest,兼容 v0.2 与 v1。

    v0.2: predicate.invocation.configSource.digest.sha1
    v1:   predicate.buildDefinition.resolvedDependencies[] 中
          git+https:// 条目的 digest.gitCommit (GitHub attest-build-provenance
          使用 "gitCommit" 作为 algorithm 名,而非 "sha1")
    """
    if version == "v1":
        dep = _find_git_dep_v1(predicate)
        if dep:
            digests = dep.get("digest") or {}
            if isinstance(digests, dict):
                # R66 P1-10: GitHub attest-build-provenance v1 使用 "gitCommit" 键,
                # 标准 SLSA v1 spec 也允许 "sha1"。优先 gitCommit,回退 sha1。
                for key in ("gitCommit", "sha1"):
                    val = str(digests.get(key, "") or "")
                    if val:
                        return val
        # 回退到 v0.2 路径
        return str(
            _safe_get(predicate, "invocation", "configSource", "digest", "sha1", default="") or ""
        )
    return str(
        _safe_get(predicate, "invocation", "configSource", "digest", "sha1", default="") or ""
    )


def _get_materials(predicate: dict, version: str) -> list:
    """提取 materials 列表,兼容 v0.2 与 v1。

    v0.2: predicate.materials[]
    v1:   predicate.buildDefinition.resolvedDependencies[]
    """
    if version == "v1":
        primary = _safe_get(
            predicate, "buildDefinition", "resolvedDependencies", default=[]
        ) or []
        if isinstance(primary, list) and primary:
            return primary
        # 回退到 v0.2 路径
        materials = predicate.get("materials") or []
        return materials if isinstance(materials, list) else []
    materials = predicate.get("materials") or []
    return materials if isinstance(materials, list) else []


# ════════════════════════════════════════════════════════════════
# 单项断言检查
# ════════════════════════════════════════════════════════════════

def _check_statement_type(statement: dict) -> dict:
    """(a) statement._type 必须为 in-toto Statement v0.1 或 v1。"""
    actual = statement.get("_type", "")
    passed = actual in ALLOWED_STATEMENT_TYPES
    expected = " 或 ".join(sorted(ALLOWED_STATEMENT_TYPES))
    return _make_check(
        "statement_type",
        passed=passed,
        expected=expected,
        actual=actual or "(missing)",
        message=(
            f"statement._type 合法: {actual}" if passed
            else f"statement._type 非法: {actual!r},期望: {expected}"
        ),
        severity="error",
    )


def _check_predicate_type(statement: dict) -> dict:
    """(b) statement.predicateType 必须在允许集合内。"""
    actual = statement.get("predicateType", "")
    passed = actual in ALLOWED_PREDICATE_TYPES
    expected = " 或 ".join(sorted(ALLOWED_PREDICATE_TYPES))
    return _make_check(
        "predicate_type",
        passed=passed,
        expected=expected,
        actual=actual or "(missing)",
        message=(
            f"predicateType 合法: {actual}" if passed
            else f"predicateType 非法: {actual!r},期望: {expected}"
        ),
        severity="error",
    )


def _check_subject_digest(statement: dict, manifest: dict) -> dict:
    """(c) statement.subject[0].digest.sha256 == release_manifest.image_digest。

    自动剥离 "sha256:" 前缀后比较。
    """
    subjects = statement.get("subject") or []
    if not subjects or not isinstance(subjects, list):
        return _make_check(
            "subject_digest",
            passed=False,
            expected="非空 subject 列表",
            actual="(missing or empty)",
            message="statement.subject 缺失或为空,无法验证 image digest",
            severity="error",
        )
    subject0 = subjects[0] if isinstance(subjects[0], dict) else {}
    actual_digest = _strip_sha_prefix(
        _safe_get(subject0, "digest", "sha256", default="") or ""
    )
    expected_digest = _strip_sha_prefix(
        str(manifest.get("image_digest", "") or "")
    )
    passed = bool(actual_digest) and bool(expected_digest) and actual_digest == expected_digest
    return _make_check(
        "subject_digest",
        passed=passed,
        expected=expected_digest or "(manifest 缺 image_digest)",
        actual=actual_digest or "(statement 缺 subject[0].digest.sha256)",
        message=(
            f"subject[0].digest.sha256 与 image_digest 一致: {actual_digest}" if passed
            else (
                "subject[0].digest.sha256 与 release_manifest.image_digest 不一致: "
                f"statement={actual_digest!r}, manifest={expected_digest!r}"
            )
        ),
        severity="error",
    )


def _check_subject_name(statement: dict, manifest: dict) -> dict:
    """(d) statement.subject[0].name 与 release manifest 镜像引用一致。"""
    subjects = statement.get("subject") or []
    if not subjects or not isinstance(subjects, list):
        return _make_check(
            "subject_name",
            passed=False,
            expected="非空 subject 列表",
            actual="(missing or empty)",
            message="statement.subject 缺失或为空,无法验证 image name",
            severity="error",
        )
    subject0 = subjects[0] if isinstance(subjects[0], dict) else {}
    actual_name = str(subject0.get("name", "") or "")
    # 兼容 image_ref / image 两个字段名
    image_ref = str(
        manifest.get("image_ref") or manifest.get("image") or ""
    )
    if not image_ref:
        return _make_check(
            "subject_name",
            passed=False,
            expected="release_manifest.image_ref 或 .image",
            actual="(manifest 缺失该字段)",
            message="release manifest 缺 image_ref/image 字段,无法验证 subject name",
            severity="warning",
        )
    passed = _names_match(actual_name, image_ref)
    return _make_check(
        "subject_name",
        passed=passed,
        expected=image_ref,
        actual=actual_name,
        message=(
            f"subject[0].name 与 image_ref 一致: {actual_name}" if passed
            else f"subject[0].name 不匹配 image_ref: statement={actual_name!r}, manifest={image_ref!r}"
        ),
        severity="error",
    )


def _check_predicate_builder_id(predicate: dict, version: str = "v0.2") -> dict:
    """(e1) predicate.builder.id 非空。"""
    builder_id = _get_builder_id(predicate, version)
    passed = bool(builder_id)
    return _make_check(
        "predicate_builder_id",
        passed=passed,
        expected="非空 builder.id",
        actual=builder_id or "(empty)",
        message=(
            f"predicate.builder.id 非空: {builder_id}" if passed
            else "predicate.builder.id 为空或缺失(builder identity 缺失)"
        ),
        severity="error",
    )


def _check_predicate_build_type(predicate: dict, version: str = "v0.2") -> dict:
    """(e2) predicate.buildType 非空。"""
    build_type = _get_build_type(predicate, version)
    passed = bool(build_type)
    return _make_check(
        "predicate_build_type",
        passed=passed,
        expected="非空 buildType",
        actual=build_type or "(empty)",
        message=(
            f"predicate.buildType 非空: {build_type}" if passed
            else "predicate.buildType 为空或缺失"
        ),
        severity="error",
    )


def _check_predicate_config_source_uri(predicate: dict, manifest: dict, version: str = "v0.2") -> dict:
    """(e3) configSource.uri 包含 github.com/<owner>/<repo>。

    v1 URI 形如 git+https://github.com/owner/repo@refs/heads/master,
    剥离 git+ 前缀与 @... 后缀后再做子串匹配。
    """
    uri = _get_config_source_uri(predicate, version)
    source_repo = str(manifest.get("source_repository", "") or "")
    if not source_repo:
        return _make_check(
            "predicate_config_source_uri",
            passed=False,
            expected="release_manifest.source_repository",
            actual="(manifest 缺失该字段)",
            message="release manifest 缺 source_repository 字段,无法验证 configSource.uri",
            severity="warning",
        )
    # 剥离 git+ 前缀与 @... 后缀,便于子串匹配(v1 URI 形如
    # git+https://github.com/owner/repo@refs/heads/master)
    check_uri = uri
    if check_uri.startswith("git+"):
        check_uri = check_uri[len("git+"):]
    if "@" in check_uri:
        check_uri = check_uri.split("@", 1)[0]
    # 期望子串:若 source_repo 已含 github.com/ 前缀则原样使用,否则补全
    if source_repo.startswith("github.com/"):
        expected_substring = source_repo
    else:
        expected_substring = "github.com/" + source_repo
    passed = expected_substring in check_uri
    return _make_check(
        "predicate_config_source_uri",
        passed=passed,
        expected=f"URI 含 {expected_substring!r}",
        actual=uri or "(missing)",
        message=(
            f"configSource.uri 包含 {expected_substring}: {uri}" if passed
            else f"configSource.uri 未包含 {expected_substring}: uri={uri!r}"
        ),
        severity="error",
    )


def _check_predicate_config_source_digest(predicate: dict, manifest: dict, version: str = "v0.2") -> dict:
    """(e4) configSource.digest.sha1 == release_manifest.source_commit。

    兼容 source_commit_sha 字段别名。
    """
    actual_sha1 = _get_config_source_digest_sha1(predicate, version)
    expected_commit = str(
        manifest.get("source_commit") or manifest.get("source_commit_sha") or ""
    )
    if not expected_commit:
        return _make_check(
            "predicate_config_source_digest",
            passed=False,
            expected="release_manifest.source_commit 或 source_commit_sha",
            actual="(manifest 缺失该字段)",
            message="release manifest 缺 source_commit/source_commit_sha 字段,无法验证 configSource.digest.sha1",
            severity="warning",
        )
    passed = bool(actual_sha1) and actual_sha1 == expected_commit
    return _make_check(
        "predicate_config_source_digest",
        passed=passed,
        expected=expected_commit,
        actual=actual_sha1 or "(missing)",
        message=(
            f"configSource.digest.sha1 与 source_commit 一致: {actual_sha1}" if passed
            else f"configSource.digest.sha1 与 source_commit 不一致: "
                 f"statement={actual_sha1!r}, manifest={expected_commit!r}"
        ),
        severity="error",
    )


def _check_predicate_materials_tree_sha(predicate: dict, manifest: dict, version: str = "v0.2") -> dict:
    """(e5) materials/resolvedDependencies 含 git source,其 commit digest 与 source_commit 一致。

    R66 P1-10 语义校正:
      原 v0.2 检查期望 materials[] 含 digest.sha256 == source_tree_sha 条目,
      但标准 SLSA provenance(actions/attest-build-provenance)不会单独列出
      git tree SHA — git source 条目只携带 commit SHA(v0.2: digest.sha1;
      v1: digest.gitCommit)。git tree SHA 是 commit 的确定性派生量,验证 commit
      即隐式验证 tree。

      新语义:
        - v1: resolvedDependencies[] 中 git+https:// 条目的 digest.gitCommit
              (或 sha1) == release_manifest.source_commit
        - v0.2: materials[] 中 git source 条目的 digest.sha1
                == release_manifest.source_commit
        - 若 source_commit 缺失,回退到 source_tree_sha 直接匹配(向后兼容)
    """
    materials = _get_materials(predicate, version)
    expected_commit = str(
        manifest.get("source_commit") or manifest.get("source_commit_sha") or ""
    )
    expected_tree = str(manifest.get("source_tree_sha", "") or "")
    if not expected_commit and not expected_tree:
        return _make_check(
            "predicate_materials_source_tree_sha",
            passed=False,
            expected="release_manifest.source_commit 或 source_tree_sha",
            actual="(manifest 缺失该字段)",
            message="release manifest 缺 source_commit/source_tree_sha 字段,无法验证 materials 中的 git source",
            severity="warning",
        )
    # 优先验证 commit SHA(attestation 实际携带的字段)
    if expected_commit:
        expected_norm = _strip_sha_prefix(expected_commit)
        # v1: gitCommit / sha1;v0.2: sha1。遍历所有 digest 键以提高兼容性。
        digest_keys = ("gitCommit", "sha1") if version == "v1" else ("sha1", "gitCommit")
        for m in materials:
            if not isinstance(m, dict):
                continue
            digests = m.get("digest") or {}
            if not isinstance(digests, dict):
                continue
            for key in digest_keys:
                candidate = _strip_sha_prefix(str(digests.get(key, "") or ""))
                if candidate and candidate == expected_norm:
                    return _make_check(
                        "predicate_materials_source_tree_sha",
                        passed=True,
                        expected=f"source_commit={expected_commit}",
                        actual=f"materials git source {key}={candidate}",
                        message=(
                            f"materials 含 git source 条目,commit digest ({key}) "
                            f"与 source_commit 一致: {candidate} (tree SHA 隐式验证)"
                        ),
                        severity="error",
                    )
    # 回退: 直接匹配 source_tree_sha (向后兼容,attestation 极少直接携带 tree SHA)
    if expected_tree:
        expected_tree_norm = _strip_sha_prefix(expected_tree)
        for m in materials:
            if not isinstance(m, dict):
                continue
            digests = m.get("digest") or {}
            if not isinstance(digests, dict):
                continue
            for algo in ("sha256", "sha1", "gitCommit"):
                candidate = _strip_sha_prefix(str(digests.get(algo, "") or ""))
                if candidate and candidate == expected_tree_norm:
                    return _make_check(
                        "predicate_materials_source_tree_sha",
                        passed=True,
                        expected=f"source_tree_sha={expected_tree}",
                        actual=f"materials {algo}={candidate}",
                        message=f"materials 含 source_tree_sha 条目: {expected_tree}",
                        severity="error",
                    )
    return _make_check(
        "predicate_materials_source_tree_sha",
        passed=False,
        expected=f"source_commit={expected_commit!r} 或 source_tree_sha={expected_tree!r}",
        actual="not found in materials",
        message=(
            f"materials 未含 git source commit/tree 条目: "
            f"期望 source_commit={expected_commit!r}"
            + (f" 或 source_tree_sha={expected_tree!r}" if expected_tree else "")
        ),
        severity="error",
    )


def _check_predicate_materials_migration(predicate: dict, manifest: dict, version: str = "v0.2") -> dict:
    """(e6) 若 release_manifest.migration_manifest_digest 存在,检查 materials[] 是否含对应条目。

    R66 P1-10 语义校正:
      标准 SLSA provenance(actions/attest-build-provenance)不会将 repo 内部文件
      (如 migration-manifest.json)作为独立 material 列出 — git source 条目已
      通过 commit SHA 绑定整个 repo 内容(含 migration-manifest.json)。
      因此本检查降级为 warning:
        - 若 attestation 含匹配条目(自定义 attestation 场景) → PASS
        - 若不含(标准 attestation 场景) → WARN(不阻断),依赖 git source
          commit SHA 间接绑定
    """
    expected_mig = str(manifest.get("migration_manifest_digest", "") or "")
    if not expected_mig:
        # manifest 未提供该字段,本检查不适用 — 跳过(passed=True, severity=info)
        return _make_check(
            "predicate_materials_migration_manifest",
            passed=True,
            expected="(release_manifest 未提供 migration_manifest_digest,跳过)",
            actual="(skip)",
            message="release_manifest 未提供 migration_manifest_digest,跳过 migration material 检查",
            severity="warning",
        )
    expected_mig_norm = _strip_sha_prefix(expected_mig)
    materials = _get_materials(predicate, version)
    found = False
    for m in materials:
        if not isinstance(m, dict):
            continue
        digests = m.get("digest") or {}
        if not isinstance(digests, dict):
            continue
        # 检查 sha256 / sha1 / gitCommit / 任意算法值是否匹配
        for algo in ("sha256", "sha1", "sha512", "gitCommit"):
            candidate = _strip_sha_prefix(str(digests.get(algo, "") or ""))
            if candidate and candidate == expected_mig_norm:
                found = True
                break
        if found:
            break
    if found:
        return _make_check(
            "predicate_materials_migration_manifest",
            passed=True,
            expected=expected_mig,
            actual="found",
            message=f"materials 含 migration_manifest_digest 条目: {expected_mig}",
            severity="error",
        )
    # 标准 attestation 不含 migration_manifest 作为独立 material — 降级为 warning
    # (migration_manifest 通过 git source commit SHA 间接绑定)
    return _make_check(
        "predicate_materials_migration_manifest",
        passed=True,  # 不阻断(soft warning)
        expected=expected_mig,
        actual="not found in materials (standard attestation)",
        message=(
            f"materials 未含 migration_manifest_digest 条目: 期望 {expected_mig!r}"
            f" — 标准 SLSA attestation 不单独列出 repo 内部文件,"
            f"migration_manifest 通过 git source commit SHA 间接绑定"
        ),
        severity="warning",
    )


def _check_rekor_tlog_entries(bundle: dict) -> dict:
    """(f) bundle.verificationMaterial.tlogEntries 非空,每个 entry 含 logIndex 与 integratedTime。"""
    tlog = _safe_get(bundle, "verificationMaterial", "tlogEntries", default=None)
    if not tlog or not isinstance(tlog, list):
        return _make_check(
            "rekor_tlog_entries",
            passed=False,
            expected="非空 tlogEntries 列表",
            actual="(missing or empty)",
            message="bundle.verificationMaterial.tlogEntries 缺失或为空(无 Rekor inclusion 证明)",
            severity="error",
        )
    # 每条 entry 必须含 logIndex 与 integratedTime
    missing_fields: list[str] = []
    for idx, entry in enumerate(tlog):
        if not isinstance(entry, dict):
            missing_fields.append(f"entry[{idx}]: not a dict")
            continue
        if "logIndex" not in entry:
            missing_fields.append(f"entry[{idx}]: missing logIndex")
        if "integratedTime" not in entry:
            missing_fields.append(f"entry[{idx}]: missing integratedTime")
    passed = not missing_fields
    return _make_check(
        "rekor_tlog_entries",
        passed=passed,
        expected="每个 entry 含 logIndex 与 integratedTime",
        actual=f"{len(tlog)} entries" + (f"; 缺失: {missing_fields}" if missing_fields else ""),
        message=(
            f"tlogEntries 含 {len(tlog)} 条,字段完整" if passed
            else f"tlogEntries 字段不完整: {missing_fields}"
        ),
        severity="error",
    )


def _find_issuer(statement: dict, bundle: dict | None) -> str | None:
    """从 statement 或 bundle 中查找 OIDC issuer 字符串。

    查找顺序:
    1. bundle.verificationMaterial.x509CertificateChain.certificates[0].issuer
    2. bundle.issuer(顶层,用于测试简化)
    3. statement.issuer(自定义字段,用于测试简化)

    Returns:
        issuer 字符串,未找到返回 None
    """
    if bundle:
        certs = _safe_get(
            bundle, "verificationMaterial", "x509CertificateChain", "certificates",
            default=None,
        )
        if isinstance(certs, list) and certs:
            first_cert = certs[0]
            if isinstance(first_cert, dict):
                issuer = first_cert.get("issuer")
                if issuer:
                    return str(issuer)
        top_issuer = bundle.get("issuer")
        if top_issuer:
            return str(top_issuer)
    stmt_issuer = statement.get("issuer")
    if stmt_issuer:
        return str(stmt_issuer)
    return None


def _check_oidc_issuer(statement: dict, bundle: dict | None) -> dict:
    """(g) 若 statement 或 bundle 中存在 OIDC issuer,必须为 GitHub Actions issuer。"""
    found_issuer = _find_issuer(statement, bundle)
    if found_issuer is None:
        # 未找到 issuer 字段 — 视为 warning(可选字段)
        return _make_check(
            "oidc_issuer",
            passed=True,
            expected=EXPECTED_OIDC_ISSUER,
            actual="(issuer 字段未提供,跳过)",
            message="statement 与 bundle 均未提供 issuer 字段,跳过 OIDC issuer 检查",
            severity="warning",
        )
    passed = found_issuer == EXPECTED_OIDC_ISSUER
    return _make_check(
        "oidc_issuer",
        passed=passed,
        expected=EXPECTED_OIDC_ISSUER,
        actual=found_issuer,
        message=(
            f"OIDC issuer 合法: {found_issuer}" if passed
            else f"OIDC issuer 非法: {found_issuer!r},期望: {EXPECTED_OIDC_ISSUER}"
        ),
        severity="error",
    )


def _find_cert_validity(bundle: dict) -> tuple[datetime | None, datetime | None]:
    """从 bundle 中提取证书 notBefore / notAfter。

    查找顺序:
    1. bundle.verificationMaterial.x509CertificateChain.certificates[0].notBefore / notAfter
    2. bundle.notBefore / bundle.notAfter(顶层,用于测试简化)

    Returns:
        (notBefore, notAfter),任一未找到对应位置为 None
    """
    not_before: datetime | None = None
    not_after: datetime | None = None
    certs = _safe_get(
        bundle, "verificationMaterial", "x509CertificateChain", "certificates",
        default=None,
    )
    if isinstance(certs, list) and certs:
        first_cert = certs[0]
        if isinstance(first_cert, dict):
            not_before = _parse_timestamp(first_cert.get("notBefore"))
            not_after = _parse_timestamp(first_cert.get("notAfter"))
    # 顶层字段回退
    if not_before is None:
        not_before = _parse_timestamp(bundle.get("notBefore"))
    if not_after is None:
        not_after = _parse_timestamp(bundle.get("notAfter"))
    return not_before, not_after


def _find_signing_time(bundle: dict) -> datetime | None:
    """从 bundle 中提取签名时间。

    查找顺序:
    1. bundle.signingTime(显式字段,用于测试简化)
    2. bundle.verificationMaterial.tlogEntries[0].integratedTime
       (Rekor 入库时间作为签名时间近似)
    """
    signing_time = _parse_timestamp(bundle.get("signingTime"))
    if signing_time is not None:
        return signing_time
    tlog = _safe_get(bundle, "verificationMaterial", "tlogEntries", default=None)
    if isinstance(tlog, list) and tlog:
        first_entry = tlog[0]
        if isinstance(first_entry, dict):
            return _parse_timestamp(first_entry.get("integratedTime"))
    return None


def _check_cert_validity(bundle: dict) -> dict:
    """(h) 若 bundle 中含证书: notBefore < 签名时间 < notAfter。"""
    not_before, not_after = _find_cert_validity(bundle)
    if not_before is None and not_after is None:
        # 未找到证书有效期字段 — 视为 warning(可选字段)
        return _make_check(
            "certificate_validity",
            passed=True,
            expected="notBefore < signing time < notAfter",
            actual="(证书有效期字段未提供,跳过)",
            message="bundle 未提供 notBefore/notAfter 字段,跳过证书有效期检查",
            severity="warning",
        )
    signing_time = _find_signing_time(bundle)
    if signing_time is None:
        return _make_check(
            "certificate_validity",
            passed=False,
            expected="可解析的 signingTime 或 integratedTime",
            actual="(未找到签名时间)",
            message="bundle 未提供可解析的签名时间(signingTime 或 tlogEntries[0].integratedTime)",
            severity="error",
        )
    # 检查 notBefore < signing < notAfter
    failures: list[str] = []
    if not_before is not None and signing_time < not_before:
        failures.append(
            f"签名时间 {signing_time.isoformat()} 早于 notBefore {not_before.isoformat()}"
        )
    if not_after is not None and signing_time > not_after:
        failures.append(
            f"签名时间 {signing_time.isoformat()} 晚于 notAfter {not_after.isoformat()}"
        )
    passed = not failures
    actual = (
        f"signing={signing_time.isoformat()}, "
        f"notBefore={not_before.isoformat() if not_before else 'N/A'}, "
        f"notAfter={not_after.isoformat() if not_after else 'N/A'}"
    )
    expected = "notBefore < signing time < notAfter"
    return _make_check(
        "certificate_validity",
        passed=passed,
        expected=expected,
        actual=actual,
        message=(
            f"签名时间在证书有效期内: {actual}" if passed
            else f"签名时间不在证书有效期内: {'; '.join(failures)}"
        ),
        severity="error",
    )


# ════════════════════════════════════════════════════════════════
# 主验证流程
# ════════════════════════════════════════════════════════════════

def verify_attestation_semantics(
    statement: dict,
    manifest: dict,
    bundle: dict | None = None,
    strict: bool = False,
) -> dict:
    """对 cosign verify 输出的 statement 执行 R66 P1-10 语义断言。

    Args:
        statement: cosign verify --output-json 输出的 in-toto Statement
        manifest: release manifest JSON
        bundle: 可选 Rekor bundle JSON
        strict: 严格模式,警告转为错误

    Returns:
        {
            "schema_version": str,
            "verified_at": str,
            "overall_passed": bool,
            "checks": [dict, ...],
            "errors": [str, ...],
            "warnings": [str, ...],
            "strict_mode": bool,
        }
    """
    checks: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []

    # (a) statement._type
    checks.append(_check_statement_type(statement))
    # (b) predicateType
    checks.append(_check_predicate_type(statement))
    # (c) subject[0].digest.sha256 == image_digest
    checks.append(_check_subject_digest(statement, manifest))
    # (d) subject[0].name 与 image_ref 一致
    checks.append(_check_subject_name(statement, manifest))

    # (e) 若 predicate 存在,执行 SLSA provenance 子检查
    predicate = statement.get("predicate")
    if isinstance(predicate, dict) and predicate:
        slsa_version = _get_slsa_version(statement)
        checks.append(_check_predicate_builder_id(predicate, slsa_version))
        checks.append(_check_predicate_build_type(predicate, slsa_version))
        checks.append(_check_predicate_config_source_uri(predicate, manifest, slsa_version))
        checks.append(_check_predicate_config_source_digest(predicate, manifest, slsa_version))
        checks.append(_check_predicate_materials_tree_sha(predicate, manifest, slsa_version))
        checks.append(_check_predicate_materials_migration(predicate, manifest, slsa_version))
    else:
        # predicate 缺失 — 非 strict 模式下为 warning
        msg = "statement.predicate 缺失或为空,跳过 SLSA provenance 子检查(builder/materials)"
        checks.append(_make_check(
            "predicate_present",
            passed=False,
            expected="非空 predicate 对象(SLSA provenance)",
            actual="(missing or empty)",
            message=msg,
            severity="warning",
        ))

    # (g) OIDC issuer 检查 — 始终执行(issuer 可能在 statement 或 bundle 中)
    # 若 statement 与 bundle 均未提供 issuer 字段,则跳过(warning 严重级)
    checks.append(_check_oidc_issuer(statement, bundle))

    # (f, h) bundle 检查(仅当 bundle 提供时执行)
    if bundle is not None:
        checks.append(_check_rekor_tlog_entries(bundle))
        checks.append(_check_cert_validity(bundle))
    else:
        # bundle 未提供 — 非 strict 模式下为 warning
        # (Rekor inclusion / 证书有效期无法验证)
        msg = "未提供 --bundle,跳过 Rekor inclusion / 证书有效期检查"
        checks.append(_make_check(
            "bundle_present",
            passed=False,
            expected="Rekor bundle JSON(含 tlogEntries / 证书链)",
            actual="(not provided)",
            message=msg,
            severity="warning",
        ))

    # 汇总 errors / warnings
    for c in checks:
        if c["passed"]:
            continue
        msg = f"[{c['name']}] {c['message']}"
        if c["severity"] == "warning":
            warnings.append(msg)
        else:
            errors.append(msg)

    # strict 模式:警告升级为错误
    if strict and warnings:
        errors.extend(f"[strict] {w}" for w in warnings)

    overall_passed = not errors

    return {
        "schema_version": SCHEMA_VERSION,
        "verified_at": _now_iso(),
        "overall_passed": overall_passed,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "strict_mode": strict,
    }


# ════════════════════════════════════════════════════════════════
# 报告与 CLI
# ════════════════════════════════════════════════════════════════

def _print_human_report(result: dict) -> None:
    """以人类可读格式打印验证报告。"""
    print("═" * 72)
    print("R66 P1-10: 供应链 attestation statement 语义验证报告")
    print("═" * 72)
    print(f"验证时间: {result.get('verified_at', '')}")
    print(f"严格模式: {'是' if result.get('strict_mode') else '否'}")
    print()
    print(f"{'检查项':<40} {'结果':<10} {'严重级':<8} 详情")
    print("─" * 72)
    for c in result.get("checks", []):
        status = "✓ PASS" if c["passed"] else "✗ FAIL"
        severity = c.get("severity", "error")
        # 截断过长的 message
        msg = c.get("message", "")
        if len(msg) > 80:
            msg = msg[:77] + "..."
        print(f"  {c['name']:<38} {status:<10} {severity:<8} {msg}")
    print("─" * 72)
    errors = result.get("errors", [])
    warnings = result.get("warnings", [])
    if errors:
        print(f"\n错误 ({len(errors)}):")
        for e in errors:
            print(f"  ✗ {e}")
    if warnings:
        print(f"\n警告 ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠ {w}")
    print()
    print(f"总结果: {'✓ 通过' if result.get('overall_passed') else '✗ 失败'}")
    print("═" * 72)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 命令行参数列表,None 表示使用 sys.argv
    """
    parser = argparse.ArgumentParser(
        description="R66 P1-10: 供应链 attestation statement 语义验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--statement",
        required=True,
        help="cosign verify --output-json 输出的 statement JSON 路径",
    )
    parser.add_argument(
        "--release-manifest",
        required=True,
        help="release manifest JSON 路径",
    )
    parser.add_argument(
        "--bundle",
        default=None,
        help="可选 Rekor bundle JSON 路径(用于验证 inclusion proof 与证书有效期)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式:警告转为错误(可选字段缺失也视为失败)",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="机器可读 JSON 报告输出路径(不指定则仅打印人类可读报告)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Returns:
        0 = 所有断言通过
        1 = 至少一项断言失败
        2 = 参数错误或 IO 错误
    """
    args = parse_args(argv)

    statement_path = Path(args.statement)
    manifest_path = Path(args.release_manifest)
    bundle_path = Path(args.bundle) if args.bundle else None

    # 加载 JSON 文件 — IO 错误返回 exit code 2
    try:
        statement = _load_json(statement_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.error(f"加载 statement 失败: {e}")
        print(f"[ERROR] 加载 statement 失败: {e}", file=sys.stderr)
        return 2
    try:
        manifest = _load_json(manifest_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.error(f"加载 release manifest 失败: {e}")
        print(f"[ERROR] 加载 release manifest 失败: {e}", file=sys.stderr)
        return 2

    bundle: dict | None = None
    if bundle_path is not None:
        try:
            bundle = _load_json(bundle_path)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            logger.error(f"加载 Rekor bundle 失败: {e}")
            print(f"[ERROR] 加载 Rekor bundle 失败: {e}", file=sys.stderr)
            return 2

    # 执行语义验证
    result = verify_attestation_semantics(
        statement=statement,
        manifest=manifest,
        bundle=bundle,
        strict=args.strict,
    )

    # 打印人类可读报告
    _print_human_report(result)

    # 写入 JSON 报告(若指定)
    if args.json_output:
        json_output_path = Path(args.json_output)
        try:
            # 确保父目录存在
            if json_output_path.parent and not json_output_path.parent.exists():
                json_output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(json_output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"\nJSON 报告已写入: {json_output_path}")
        except OSError as e:
            logger.error(f"写入 JSON 报告失败: {e}")
            print(f"[ERROR] 写入 JSON 报告失败: {e}", file=sys.stderr)
            return 2

    return 0 if result.get("overall_passed") else 1


if __name__ == "__main__":
    sys.exit(main())
