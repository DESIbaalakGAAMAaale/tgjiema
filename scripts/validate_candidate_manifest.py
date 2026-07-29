#!/usr/bin/env python3
"""R74 §10.9 / R76 O9: Strict candidate manifest validator (三层验证).

Validates a candidate manifest JSON against the JSON Schema
(schemas/rc-candidate-manifest.schema.json).  Used by _promote-verified-rc.yml
Step g to verify the manifest before promotion.

R76 O9 整改: 在原 jsonschema 第一层基础上新增两层验证,组成完整三层结构:

    Layer 1 (schema):  ``validate_manifest()`` — jsonschema Draft 2020-12
                        校验 required / pattern / const / minLength /
                        format:date-time / enum / additionalProperties
    Layer 2 (semantic): ``validate_semantics()`` — tag/ref 对应、peeled=source、
                        run/attempt/workflow/event 与 expected 一致、
                        所有 success 字段为 const(双重检查,不依赖 schema)
    Layer 3 (artifact): ``verify_artifacts()`` — 对每个 artifact 从磁盘重新
                        SHA-256,禁止相信 manifest 自己写的 digest
    Layer 4 (signature):``verify_manifest_signature()`` — canonical JSON 排除
                        signature 字段后验签;key ID 必须在 CI 临时 keyring 中

安全模型:
    - Layer 1 防止 manifest 字段缺失/格式错误(JSON 语法层)
    - Layer 2 防止 manifest 字段间逻辑不一致(语义层 — 如 tag 与 ref 不匹配)
    - Layer 3 防止 manifest 谎报 artifact digest(文件层 — 重新计算比对)
    - Layer 4 防止 manifest 被篡改(签名层 — HMAC 验签)

Usage:
    python scripts/validate_candidate_manifest.py <manifest.json>
    python scripts/validate_candidate_manifest.py --schema <schema.json> <manifest.json>
    python scripts/validate_candidate_manifest.py --semantic --expected-run-id 12345 <manifest.json>
    python scripts/validate_candidate_manifest.py --artifacts --artifact-dir artifacts/ <manifest.json>
    python scripts/validate_candidate_manifest.py --signature --keyring keyring.json <manifest.json>
    python scripts/validate_candidate_manifest.py --all --artifact-dir artifacts/ --keyring keyring.json <manifest.json>

Exit codes (fail-closed — never silently skip validation):
    0: manifest is valid against the schema
    1: validation errors, missing files, parse errors, invalid schema,
       or ``jsonschema`` dependency not installed
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

# ``jsonschema`` 只在 Layer 1 需要。模块可能被只测试 semantic/artifact 层的
# 单元测试导入，因此缺依赖时不能在 import 阶段终止整个 pytest 进程；真正执行
# schema 校验时仍必须 fail-closed。
try:
    import jsonschema
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    jsonschema = None
    Draft202012Validator = None
    FormatChecker = None


def _check_date_time(value: object) -> bool:
    """R76 O9: 严格 RFC 3339 date-time 校验 — naive datetime 直接 False。

    R74 版本仅调用 ``datetime.fromisoformat`` 不校验 timezone,
    导致 ``2024-01-01T00:00:00``(naive,无 tzinfo)通过校验。
    R76 O9 修复: ``dt.utcoffset() is None`` 直接 False(fail-closed),
    要求所有 date-time 必须带时区(UTC 或偏移)。
    """
    if not isinstance(value, str):
        return True  # JSON Schema type validation handles non-strings
    # fromisoformat handles ISO 8601; replace trailing Z with +00:00 for UTC.
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    # R76 O9: naive datetime(无 tzinfo)直接拒绝 — utcoffset() 返回 None
    if dt.utcoffset() is None:
        raise ValueError(
            f"naive datetime without timezone not allowed: {value!r} "
            "(RFC 3339 requires explicit timezone offset or Z)"
        )
    return True


def _format_checker():
    if FormatChecker is None:
        return None
    checker = FormatChecker()
    checker.checks("date-time", raises=(ValueError, TypeError))(_check_date_time)
    return checker


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "schemas" / "rc-candidate-manifest.schema.json"

# R76 O9: artifact digest 字段到文件名的映射(用于 verify_artifacts)
# 每个 *_digest 字段对应的 artifact 文件名(无后缀,文件内容为原始 bytes)
ARTIFACT_DIGEST_FIELDS: dict[str, str] = {
    "image_digest": "image.tar",
    "runtime_config_digest": "runtime_config.json",
    "oci_manifest_digest": "oci_manifest.json",
    "sbom_digest": "sbom.spdx.json",
    "provenance_digest": "provenance.intoto.jsonl",
    "real_transaction_digest": "real_transaction.json",
    "backup_restore_digest": "backup_restore.json",
    "switch_rollback_digest": "switch_rollback.json",
    "runtime_e2e_digest": "runtime_e2e.json",
    "payload_digest": "payload.json",
}
SECRETLESS_MANIFEST_KIND = "secretless-candidate-manifest"

# R76 O9: 所有应为 "success" 的 const 字段(语义层双重检查)
SUCCESS_CONST_FIELDS: dict[str, str] = {
    "conclusion": "success",
    "verify_3x_result": "success",
}

# R76 O9: 所有应为 "closed" 的 const 字段(break_glass 状态)
CLOSED_CONST_FIELDS: dict[str, str] = {
    "break_glass_status": "closed",
}


def validate_manifest(manifest_path: Path, schema_path: Path) -> tuple[bool, list[str]]:
    """Layer 1: Validate ``manifest_path`` against ``schema_path`` using jsonschema.

    Returns ``(valid, errors)``.  Fail-closed: any infrastructure problem
    (missing file, JSON parse error, malformed schema) is reported as a
    validation failure rather than silently skipped.
    """
    if jsonschema is None or Draft202012Validator is None:
        return False, [
            "jsonschema library required for candidate manifest validation; "
            "install the declared project dependencies"
        ]
    if not manifest_path.exists():
        return False, [f"manifest file not found: {manifest_path}"]
    if not schema_path.exists():
        return False, [f"schema file not found: {schema_path}"]

    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"schema JSON parse error: {e}"]

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"manifest JSON parse error: {e}"]

    # Validate the schema itself is a well-formed Draft 2020-12 schema.
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        return False, [f"schema is not a valid Draft 2020-12 schema: {e.message}"]

    # Validate the manifest with format checking (e.g. date-time) enabled so
    # that ``format`` keywords are actually enforced, not just annotated.
    validator = Draft202012Validator(schema, format_checker=_format_checker())
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(manifest),
        key=lambda e: list(e.absolute_path),
    ):
        field_path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"field '{field_path}': {error.message}")

    return len(errors) == 0, errors


def validate_semantics(
    manifest: dict[str, Any], expected: dict[str, Any] | None = None
) -> tuple[bool, list[str]]:
    """Layer 2: R76 O9 语义层验证 — manifest 字段间逻辑一致性。

    校验维度:
        1. tag/ref 对应: ``rc_tag`` 必须是 ``ref`` 的后缀
           (e.g., rc_tag="rc-v1.2.3" ↔ ref="refs/tags/rc-v1.2.3")
        2. peeled=source: ``peeled_commit`` 必须等于 ``source_sha``
           (annotated tag peel 后应指向 source commit)
        3. run/attempt/workflow/event 对应: manifest 字段必须与
           ``expected`` 中的 CI 环境值一致(若 expected 提供)
        4. 所有 success 字段为 const: conclusion="success",
           verify_3x_result="success"(不依赖 schema const,双重检查)
        5. break_glass_status="closed"(不允许逃生舱开启时上线)

    Args:
        manifest: 已解析的 manifest dict
        expected: 期望的 CI 环境值 dict,可包含:
            - ``run_id``: GitHub Actions run ID(str)
            - ``run_attempt``: GitHub Actions run attempt(str)
            - ``workflow_path``: workflow 文件路径
            - ``event``: 触发事件(如 "push")
            - ``ref``: git ref(如 "refs/tags/rc-v1.2.3")
            - ``source_sha``: master SHA(peeled 后应等于 manifest.peeled_commit)

    Returns:
        (valid, errors): valid=True 表示所有语义校验通过
    """
    errors: list[str] = []
    expected = expected or {}

    manifest_kind = str(manifest.get("kind", ""))
    ref = str(manifest.get("ref", ""))
    source_sha = str(manifest.get("source_sha", ""))

    if manifest_kind == SECRETLESS_MANIFEST_KIND:
        artifact_identity = manifest.get("artifact_identity")
        if not isinstance(artifact_identity, dict):
            errors.append("semantic: secretless artifact_identity must be an object")
        else:
            for field in ("workflow_path", "run_id", "run_attempt", "event", "source_sha"):
                if str(artifact_identity.get(field, "")) != str(manifest.get(field, "")):
                    errors.append(
                        f"semantic: artifact_identity.{field} must equal manifest.{field}"
                    )
        if manifest.get("source_database_identity") == manifest.get("target_database_identity"):
            errors.append("semantic: source and target database identities must differ")
    else:
        # 1. tag/ref 对应: rc_tag 必须是 ref 的后缀
        rc_tag = str(manifest.get("rc_tag", ""))
        if rc_tag and ref:
            if not ref.endswith(rc_tag):
                errors.append(
                    f"semantic: rc_tag {rc_tag!r} does not match ref suffix "
                    f"(ref={ref!r}, expected ref to end with rc_tag)"
                )
        elif rc_tag and not ref:
            errors.append("semantic: rc_tag present but ref missing")
        elif ref and not rc_tag:
            errors.append("semantic: ref present but rc_tag missing")

        # 2. peeled=source: peeled_commit 必须等于 source_sha
        peeled = str(manifest.get("peeled_commit", ""))
        if peeled and source_sha and peeled != source_sha:
            errors.append(
                f"semantic: peeled_commit {peeled!r} != source_sha {source_sha!r} "
                "(annotated tag must peel to source commit)"
            )

    # 3. run/attempt/workflow/event 对应(若 expected 提供)
    for field in ("run_id", "run_attempt", "workflow_path", "event", "ref", "source_sha"):
        if field in expected:
            actual = str(manifest.get(field, ""))
            exp_val = str(expected[field])
            if actual != exp_val:
                errors.append(
                    f"semantic: {field} mismatch (expected={exp_val!r}, actual={actual!r})"
                )

    # 4. 所有 success 字段为 const(不依赖 schema const,双重检查)
    success_fields = (
        {"conclusion": "success"}
        if manifest_kind == SECRETLESS_MANIFEST_KIND
        else SUCCESS_CONST_FIELDS
    )
    for field, expected_value in success_fields.items():
        actual = manifest.get(field, "")
        if actual != expected_value:
            errors.append(
                f"semantic: {field} must be {expected_value!r} (actual={actual!r})"
            )

    # 5. production RC manifest 的 break_glass_status 必须 closed。
    if manifest_kind != SECRETLESS_MANIFEST_KIND:
        for field, expected_value in CLOSED_CONST_FIELDS.items():
            actual = manifest.get(field, "")
            if actual != expected_value:
                errors.append(
                    f"semantic: {field} must be {expected_value!r} (actual={actual!r})"
                )

    return len(errors) == 0, errors


def _resolve_artifact_path(artifact_dir: Path, relative_path: str) -> Path | None:
    """Resolve a manifest artifact path without allowing escape from artifact_dir."""
    candidate = (artifact_dir / relative_path).resolve()
    root = artifact_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _verify_secretless_artifacts(
    manifest: dict[str, Any], artifact_dir: Path
) -> tuple[bool, list[str]]:
    """Re-hash the explicit artifact list in a Secretless current-SHA manifest."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False, ["artifact: secretless manifest artifacts must be a non-empty list"]
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            errors.append(f"artifact: artifacts[{index}] must be an object")
            continue
        name = str(item.get("name", ""))
        relative_path = str(item.get("path", ""))
        expected = str(item.get("sha256", ""))
        if not name or name in seen_names:
            errors.append(f"artifact: duplicate or empty name at artifacts[{index}]")
        if not relative_path or relative_path in seen_paths:
            errors.append(f"artifact: duplicate or empty path at artifacts[{index}]")
        seen_names.add(name)
        seen_paths.add(relative_path)
        artifact_path = _resolve_artifact_path(artifact_dir, relative_path)
        if artifact_path is None:
            errors.append(f"artifact: path escapes artifact directory: {relative_path!r}")
            continue
        if not artifact_path.is_file():
            errors.append(f"artifact: file not found: {artifact_path}")
            continue
        try:
            actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"artifact: failed to read {artifact_path}: {exc}")
            continue
        expected_hex = expected.removeprefix("sha256:")
        if not hmac.compare_digest(actual, expected_hex):
            errors.append(
                f"artifact: digest mismatch for {name!r} "
                f"(path={relative_path}, manifest={expected_hex[:12]}..., "
                f"actual={actual[:12]}...)"
            )
    return not errors, errors


def verify_artifacts(
    manifest: dict[str, Any], artifact_dir: Path
) -> tuple[bool, list[str]]:
    """Layer 3: R76 O9 文件层验证 — 对每个 artifact 从磁盘重新 SHA-256。

    R76 O9 安全要求: **禁止相信 manifest 自己写的 digest**。必须从磁盘
    重新读取每个 artifact 文件,计算 SHA-256,与 manifest 中的 digest 比对。

    校验流程:
        1. 遍历 ``ARTIFACT_DIGEST_FIELDS`` 中每个 digest 字段
        2. 在 ``artifact_dir`` 中查找对应的 artifact 文件
        3. 读取文件 bytes,计算 SHA-256
        4. 与 manifest 中的 digest(去除 "sha256:" 前缀)比对
        5. 任一不匹配即 fail-closed

    Args:
        manifest: 已解析的 manifest dict
        artifact_dir: artifact 文件目录(包含 image.tar / sbom.spdx.json 等)

    Returns:
        (valid, errors): valid=True 表示所有 artifact digest 匹配
    """
    errors: list[str] = []

    if not artifact_dir.exists():
        return False, [f"artifact directory not found: {artifact_dir}"]

    if not artifact_dir.is_dir():
        return False, [f"artifact path is not a directory: {artifact_dir}"]

    if manifest.get("kind") == SECRETLESS_MANIFEST_KIND:
        return _verify_secretless_artifacts(manifest, artifact_dir)

    for digest_field, filename in ARTIFACT_DIGEST_FIELDS.items():
        manifest_digest = str(manifest.get(digest_field, ""))
        if not manifest_digest:
            errors.append(
                f"artifact: manifest missing digest field {digest_field!r}"
            )
            continue

        artifact_path = artifact_dir / filename
        if not artifact_path.exists():
            errors.append(
                f"artifact: file not found for {digest_field!r}: {artifact_path}"
            )
            continue

        # 从磁盘重新计算 SHA-256(禁止相信 manifest 自己写的 digest)
        try:
            file_bytes = artifact_path.read_bytes()
        except OSError as e:
            errors.append(
                f"artifact: failed to read {artifact_path}: {e}"
            )
            continue

        actual_digest_hex = hashlib.sha256(file_bytes).hexdigest()
        # manifest 中的 digest 格式为 "sha256:<hex>",剥离前缀后比对
        expected_digest_hex = manifest_digest
        if expected_digest_hex.startswith("sha256:"):
            expected_digest_hex = expected_digest_hex[len("sha256:"):]

        if not hmac.compare_digest(actual_digest_hex, expected_digest_hex):
            errors.append(
                f"artifact: digest mismatch for {digest_field!r} "
                f"(file={filename}, manifest={expected_digest_hex[:12]}..., "
                f"actual={actual_digest_hex[:12]}...)"
            )

    return len(errors) == 0, errors


def verify_manifest_signature(
    manifest: dict[str, Any], keyring: dict[str, bytes] | Path
) -> tuple[bool, list[str]]:
    """Layer 4: R76 O9 签名层验证 — canonical JSON 排除 signature 后验签。

    R76 O9 安全要求:
        - canonical JSON 排除 ``signature`` 字段后计算 HMAC-SHA256
        - 验签使用的 key ID(``signature_identity``)必须在 CI 临时 keyring 中
        - secretless CI 使用单次 run 临时 key,不需要生产密钥

    校验流程:
        1. 从 manifest 读取 ``signature_identity``(key ID)
        2. 从 keyring 查找对应 key(必须在 keyring 中,否则 fail-closed)
        3. 构造 canonical JSON(排除 ``signature`` 字段,sort_keys=True,
           separators=(",", ":"))
        4. 计算 HMAC-SHA256,与 manifest["signature"] 比对
        5. 使用 ``hmac.compare_digest`` 防时序攻击

    Args:
        manifest: 已解析的 manifest dict(含 signature_identity + signature)
        keyring: 密钥字典 {key_id: bytes} 或 keyring JSON 文件路径
                  (JSON 格式: {key_id: hex_str})

    Returns:
        (valid, errors): valid=True 表示签名验证通过
    """
    errors: list[str] = []

    # 解析 keyring(Path → dict[str, bytes])
    if isinstance(keyring, Path):
        if not keyring.exists():
            return False, [f"keyring file not found: {keyring}"]
        try:
            with open(keyring, encoding="utf-8") as f:
                keyring_data = json.load(f)
        except json.JSONDecodeError as e:
            return False, [f"keyring JSON parse error: {e}"]
        # JSON 中 key 为 hex 字符串,转为 bytes
        try:
            keyring = {k: bytes.fromhex(v) for k, v in keyring_data.items()}
        except (TypeError, ValueError) as e:
            return False, [f"keyring hex decode error: {e}"]

    # 1. 从 manifest 读取 key ID
    key_id = str(manifest.get("signature_identity", ""))
    if not key_id:
        return False, ["signature: manifest missing signature_identity field"]

    # 2. 从 keyring 查找 key(必须在 keyring 中,否则 fail-closed)
    signing_key = keyring.get(key_id)
    if signing_key is None:
        return False, [
            f"signature: key_id {key_id!r} not found in keyring "
            "(must be in CI temporary keyring)"
        ]

    # 3. 构造 canonical JSON(排除 signature 字段)
    manifest_without_sig = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = json.dumps(
        manifest_without_sig,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    # 4. 计算 HMAC-SHA256
    expected_sig = hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()
    actual_sig = str(manifest.get("signature", ""))

    # 5. 比对(防时序攻击)
    if not hmac.compare_digest(expected_sig, actual_sig):
        errors.append(
            f"signature: HMAC-SHA256 verification failed for key_id={key_id!r} "
            f"(expected={expected_sig[:12]}..., actual={actual_sig[:12]}...)"
        )

    return len(errors) == 0, errors


def verify_payload_digest(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    """Layer 5: R76 P1-02 payload digest 验证 — canonical payload 一致性。

    R76 P1-02 安全要求:
        - ``payload_digest`` 必须等于 manifest canonical payload 的 SHA-256
        - canonical payload = 排除 ``signature`` 和 ``payload_digest`` 字段后
          的 canonical JSON(sort_keys=True, separators=(",", ":"),
          ensure_ascii=False)
        - 防止 manifest 谎报 payload digest(自比较漏洞)

    校验流程:
        1. 从 manifest 读取 ``payload_digest``(格式: ``sha256:<hex>``)
        2. 构造 canonical payload(排除 signature + payload_digest 字段)
        3. 计算 SHA-256,与 manifest 中的 payload_digest 比对
        4. 使用 ``hmac.compare_digest`` 防时序攻击

    Args:
        manifest: 已解析的 manifest dict(含 payload_digest 字段)

    Returns:
        (valid, errors): valid=True 表示 payload digest 匹配
    """
    errors: list[str] = []

    manifest_payload_digest = str(manifest.get("payload_digest", ""))
    if not manifest_payload_digest:
        return False, ["payload: manifest missing payload_digest field"]

    # 构造 canonical payload(排除 signature + payload_digest 字段)
    canonical_payload = {
        k: v for k, v in manifest.items()
        if k not in ("signature", "payload_digest")
    }
    canonical = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    # 计算 SHA-256
    actual_digest_hex = hashlib.sha256(canonical).hexdigest()
    expected_digest_hex = manifest_payload_digest
    if expected_digest_hex.startswith("sha256:"):
        expected_digest_hex = expected_digest_hex[len("sha256:"):]

    if not hmac.compare_digest(actual_digest_hex, expected_digest_hex):
        errors.append(
            f"payload: payload_digest mismatch "
            f"(manifest={expected_digest_hex[:12]}..., "
            f"actual={actual_digest_hex[:12]}...)"
        )

    return len(errors) == 0, errors


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """加载 manifest JSON 文件(供三层验证共用)。"""
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="R74 §10.9 / R76 O9: Strict candidate manifest validator (三层验证)"
    )
    parser.add_argument("manifest_pos", nargs="?", type=Path, default=None,
                        help="Path to candidate-manifest.json (positional, optional)")
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Path to candidate-manifest.json or schema (if *.schema.json, used as schema)"
    )
    parser.add_argument(
        "--schema", type=Path, default=DEFAULT_SCHEMA,
        help="Path to JSON Schema (default: schemas/rc-candidate-manifest.schema.json)"
    )
    # R76 O9: 三层验证开关(默认仅 Layer 1 schema 校验,向后兼容)
    layer_group = parser.add_argument_group("validation layers (R76 O9)")
    layer_group.add_argument(
        "--semantic", action="store_true",
        help="Layer 2: semantic validation (tag/ref, peeled=source, run/attempt, success const)"
    )
    layer_group.add_argument(
        "--artifacts", action="store_true",
        help="Layer 3: artifact digest verification (recompute SHA-256 from disk)"
    )
    layer_group.add_argument(
        "--signature", action="store_true",
        help="Layer 4: signature verification (HMAC-SHA256 with CI temporary keyring)"
    )
    layer_group.add_argument(
        "--payload", action="store_true",
        help="Layer 5: payload_digest canonical verification (R76 P1-02)"
    )
    layer_group.add_argument(
        "--all", action="store_true",
        help="Run all 5 layers (schema + semantic + artifacts + signature + payload)"
    )
    # R80 Step 14: --strict 等同于 --all
    layer_group.add_argument(
        "--strict", action="store_true",
        help="Strict mode: run all validation layers (alias for --all)"
    )
    # Layer 2 参数
    semantic_group = parser.add_argument_group("semantic layer options")
    semantic_group.add_argument("--expected-run-id", type=str, default=None)
    semantic_group.add_argument("--expected-run-attempt", type=str, default=None)
    semantic_group.add_argument("--expected-workflow-path", type=str, default=None)
    semantic_group.add_argument("--expected-event", type=str, default=None)
    semantic_group.add_argument("--expected-ref", type=str, default=None)
    semantic_group.add_argument("--expected-source-sha", type=str, default=None)
    # Layer 3 参数
    artifact_group = parser.add_argument_group("artifact layer options")
    artifact_group.add_argument(
        "--artifact-dir", type=Path, default=None,
        help="Directory containing artifact files (image.tar, sbom.spdx.json, ...)"
    )
    # Layer 4 参数
    sig_group = parser.add_argument_group("signature layer options")
    sig_group.add_argument(
        "--keyring", type=Path, default=None,
        help="Path to keyring JSON file ({key_id: hex_str})"
    )
    # R80 Step 14: --verification-key (raw HMAC key string, 替代 --keyring 文件)
    sig_group.add_argument(
        "--verification-key", type=str, default=None,
        help="Raw HMAC-SHA256 verification key (hex string, alternative to --keyring)"
    )

    args = parser.parse_args(argv)

    # R80 Step 14: --strict 等同于 --all
    if args.strict:
        args.all = True

    # R80 Step 14: 解析 --manifest 参数(可能是 schema 路径或 manifest 路径)
    manifest_path = args.manifest_pos
    schema_path = args.schema
    if args.manifest is not None:
        manifest_str = str(args.manifest)
        if manifest_str.endswith(".schema.json"):
            # --manifest 指向 schema 文件
            schema_path = args.manifest
        else:
            manifest_path = args.manifest

    # 如果没有 manifest 路径,尝试从 artifact-dir 查找
    if manifest_path is None:
        if args.artifact_dir is not None:
            candidate = args.artifact_dir / "candidate-manifest.json"
            if candidate.is_file():
                manifest_path = candidate
            else:
                # 尝试 phases 子目录
                for p in sorted(args.artifact_dir.rglob("*.json")):
                    if "manifest" in p.name.lower():
                        manifest_path = p
                        break
        if manifest_path is None:
            print("::error::No candidate manifest found. Provide positional arg or --manifest.",
                  file=sys.stderr)
            return 1

    # Layer 1: schema validation(始终执行)
    valid, errors = validate_manifest(manifest_path, schema_path)
    if not valid:
        for err in errors:
            print(f"::error::Layer1 FAIL: {err}", file=sys.stderr)
        return 1
    print(f"Layer1 PASS: candidate manifest schema validated ({manifest_path})")

    # 加载 manifest(供 Layer 2/3/4 使用)
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"::error::FAIL: cannot load manifest for Layer 2/3/4: {e}", file=sys.stderr)
        return 1

    run_semantic = args.semantic or args.all
    run_artifacts = args.artifacts or args.all
    run_signature = args.signature or args.all
    run_payload = args.payload or args.all

    # Layer 2: semantic validation
    if run_semantic:
        expected: dict[str, Any] = {}
        if args.expected_run_id is not None:
            expected["run_id"] = args.expected_run_id
        if args.expected_run_attempt is not None:
            expected["run_attempt"] = args.expected_run_attempt
        if args.expected_workflow_path is not None:
            expected["workflow_path"] = args.expected_workflow_path
        if args.expected_event is not None:
            expected["event"] = args.expected_event
        if args.expected_ref is not None:
            expected["ref"] = args.expected_ref
        if args.expected_source_sha is not None:
            expected["source_sha"] = args.expected_source_sha
        valid, errs = validate_semantics(manifest, expected)
        if not valid:
            for err in errs:
                print(f"::error::Layer2 FAIL: {err}", file=sys.stderr)
            return 1
        print("Layer2 PASS: semantic validation passed")

    # Layer 3: artifact digest verification
    if run_artifacts:
        if args.artifact_dir is None:
            print(
                "::error::Layer3 FAIL: --artifact-dir required for artifact verification",
                file=sys.stderr,
            )
            return 1
        valid, errs = verify_artifacts(manifest, args.artifact_dir)
        if not valid:
            for err in errs:
                print(f"::error::Layer3 FAIL: {err}", file=sys.stderr)
            return 1
        print(f"Layer3 PASS: artifact digests verified from disk ({args.artifact_dir})")

    # Layer 4: signature verification
    if run_signature:
        if args.keyring is not None and args.verification_key is not None:
            print(
                "::error::Layer4 FAIL: use exactly one of --keyring or --verification-key",
                file=sys.stderr,
            )
            return 1
        verification_source: dict[str, bytes] | Path
        if args.keyring is not None:
            verification_source = args.keyring
        elif args.verification_key is not None:
            try:
                raw_key = bytes.fromhex(args.verification_key)
            except ValueError:
                print(
                    "::error::Layer4 FAIL: --verification-key must be a hex string",
                    file=sys.stderr,
                )
                return 1
            if len(raw_key) < 32:
                print(
                    "::error::Layer4 FAIL: --verification-key must contain at least 32 bytes",
                    file=sys.stderr,
                )
                return 1
            key_id = str(manifest.get("signature_identity", ""))
            if not key_id:
                print(
                    "::error::Layer4 FAIL: manifest signature_identity is missing",
                    file=sys.stderr,
                )
                return 1
            verification_source = {key_id: raw_key}
        else:
            print(
                "::error::Layer4 FAIL: --keyring or --verification-key required",
                file=sys.stderr,
            )
            return 1
        valid, errs = verify_manifest_signature(manifest, verification_source)
        if not valid:
            for err in errs:
                print(f"::error::Layer4 FAIL: {err}", file=sys.stderr)
            return 1
        print("Layer4 PASS: manifest signature verified")

    # Layer 5: payload_digest canonical verification (R76 P1-02)
    if run_payload:
        valid, errs = verify_payload_digest(manifest)
        if not valid:
            for err in errs:
                print(f"::error::Layer5 FAIL: {err}", file=sys.stderr)
            return 1
        print("Layer5 PASS: payload_digest canonical verification passed")

    if run_semantic or run_artifacts or run_signature or run_payload:
        print(f"PASS: candidate manifest fully validated ({manifest_path})")
    else:
        print(f"PASS: candidate manifest validated ({manifest_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
