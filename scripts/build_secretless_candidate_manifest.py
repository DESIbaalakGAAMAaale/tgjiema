#!/usr/bin/env python3
"""Build the R83 Step 14 current-SHA-bound Secretless candidate manifest."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "secretless-e2e"
STATE_DIR = ARTIFACT_ROOT / "state"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

REQUIRED_ARTIFACTS: dict[str, str] = {
    "compose_resolved": "compose-resolved.json",
    "service_graph": "service-graph.json",
    "normal_transaction": "phases/10-normal-transaction.json",
    "fault_matrix": "phases/11-fault-matrix.json",
    "backup_restore": "phases/12-backup-restore.json",
    "switch_rollback": "phases/13-switch-rollback.json",
    "full_backup": "step12/full_backup_to_s3_contract_store.json",
    "corruption_negative": "step12/corrupt_payload_negative.json",
    "blank_restore": "step12/blank_restore_from_s3_contract_store.json",
    "actual_switch": "step13/actual_switch.json",
    "switch_probe_failure": "step13/switch_probe_failure.json",
    "actual_rollback": "step13/actual_rollback.json",
    "target_cleanup": "step13/target_cleanup.json",
    "backup_state": "state/backup-state.json",
    "restore_state": "state/restore-state.json",
}


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 1


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _read_object(path: Path, *, schema_version: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    if value.get("schema_version") != schema_version:
        raise ValueError(
            f"schema_version mismatch for {path}: expected={schema_version!r}, "
            f"actual={value.get('schema_version')!r}"
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def resolve_compose_service_image(
    compose_path: Path,
    service: str,
) -> str:
    """Resolve a service image from the current run's Compose config."""
    try:
        document = json.loads(compose_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid resolved Compose JSON {compose_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TypeError("resolved Compose root must be an object")
    services = document.get("services")
    if services is None:
        raise ValueError("resolved Compose config is missing services object")
    if not isinstance(services, dict):
        raise TypeError("resolved Compose services must be an object")
    service_config = services.get(service)
    if service_config is None:
        raise ValueError(f"resolved Compose config is missing service {service!r}")
    if not isinstance(service_config, dict):
        raise TypeError(f"resolved Compose service {service!r} must be an object")
    image_ref = service_config.get("image")
    if not isinstance(image_ref, str):
        raise TypeError(f"resolved Compose service {service!r} image must be a string")
    image_ref = image_ref.strip()
    if not image_ref:
        raise ValueError(f"resolved Compose service {service!r} image is empty")
    if "${" in image_ref or "}" in image_ref:
        raise ValueError(
            f"resolved Compose service {service!r} image contains an unresolved placeholder"
        )
    if any(character.isspace() for character in image_ref):
        raise ValueError(f"resolved Compose service {service!r} image contains whitespace")
    return image_ref


def _image_digest(image_ref: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker image inspect failed for {image_ref!r}: {result.stderr.strip()}"
        )
    digest = result.stdout.strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"docker image ID is not an immutable sha256 digest: {digest!r}")
    return digest


def build_manifest(
    *,
    artifact_dir: Path,
    output: Path,
    image_ref: str,
    signing_key_hex: str,
    expected_sha: str,
) -> dict[str, Any]:
    source_sha = _git("rev-parse", "HEAD")
    tree_sha = _git("rev-parse", "HEAD^{tree}")
    if not _SHA40_RE.fullmatch(source_sha) or not _SHA40_RE.fullmatch(tree_sha):
        raise ValueError("git source/tree identity is not 40-hex")
    if expected_sha and expected_sha != source_sha:
        raise ValueError(
            f"current SHA mismatch: expected={expected_sha!r}, actual={source_sha!r}"
        )

    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "").strip()
    event = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    ref = os.environ.get("GITHUB_REF", "").strip()
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise ValueError("GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be numeric")
    if event != "push":
        raise ValueError(f"Secretless candidate requires push event, got {event!r}")
    if not ref.startswith(("refs/heads/", "refs/tags/")):
        raise ValueError(f"invalid GITHUB_REF for push: {ref!r}")

    try:
        signing_key = bytes.fromhex(signing_key_hex)
    except ValueError as exc:
        raise ValueError("signing key must be hex") from exc
    if len(signing_key) < 32:
        raise ValueError("signing key must contain at least 32 bytes")

    backup_state = _read_object(
        artifact_dir / "state" / "backup-state.json",
        schema_version="secretless-backup-state/v1",
    )
    restore_state = _read_object(
        artifact_dir / "state" / "restore-state.json",
        schema_version="secretless-restore-state/v1",
    )
    for state_name, state in (("backup", backup_state), ("restore", restore_state)):
        if state.get("head_sha") != source_sha:
            raise ValueError(f"{state_name} state is not bound to current SHA")
    for key in ("backup_id", "payload_key", "manifest_key", "complete_key"):
        if restore_state.get(key) != backup_state.get(key):
            raise ValueError(f"restore state does not bind exact backup field {key!r}")
    if restore_state.get("source_identity") == restore_state.get("target_identity"):
        raise ValueError("source and target database identities must differ")

    artifacts: list[dict[str, str]] = []
    for name, relative in REQUIRED_ARTIFACTS.items():
        path = artifact_dir / relative
        if not path.is_file():
            raise ValueError(f"required Step 14 artifact is missing: {relative}")
        artifacts.append({"name": name, "path": relative, "sha256": _sha256(path)})

    runtime_config = artifact_dir / "compose-resolved.json"
    image_digest = _image_digest(image_ref)
    workflow_path = ".github/workflows/secretless-contract-e2e.yml"
    manifest: dict[str, Any] = {
        "schema_version": "secretless-candidate-manifest/v1",
        "kind": "secretless-candidate-manifest",
        "source_sha": source_sha,
        "tree_sha": tree_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_path": workflow_path,
        "event": event,
        "ref": ref,
        "conclusion": "success",
        "image_digest": image_digest,
        "runtime_config_digest": _sha256(runtime_config),
        "artifact_identity": {
            "workflow_path": workflow_path,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "event": event,
            "source_sha": source_sha,
        },
        "backup_id": str(backup_state["backup_id"]),
        "restore_operation_id": str(restore_state["operation_id"]),
        "source_database_identity": str(restore_state["source_identity"]),
        "target_database_identity": str(restore_state["target_identity"]),
        "artifacts": artifacts,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "payload_digest": "",
        "signature_identity": f"secretless-run-{run_id}-{run_attempt}",
        "signature": "",
    }
    payload = {k: v for k, v in manifest.items() if k not in {"payload_digest", "signature"}}
    manifest["payload_digest"] = "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    manifest["signature"] = hmac.new(signing_key, _canonical(signed), hashlib.sha256).hexdigest()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=ARTIFACT_ROOT / "step14" / "candidate-manifest.json",
    )
    parser.add_argument(
        "--image-ref",
        help="Explicit built image reference; defaults to db_backup in resolved Compose JSON",
    )
    parser.add_argument(
        "--resolved-compose",
        type=Path,
        help="Current-run docker compose config --format json output",
    )
    parser.add_argument("--compose-service", default="db_backup")
    parser.add_argument("--signing-key", required=True)
    parser.add_argument("--expected-sha", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    try:
        image_ref = (args.image_ref or "").strip()
        if args.resolved_compose is not None:
            resolved_image_ref = resolve_compose_service_image(
                args.resolved_compose.resolve(),
                args.compose_service,
            )
            if image_ref and image_ref != resolved_image_ref:
                raise ValueError(
                    "explicit image reference does not match resolved Compose service image"
                )
            image_ref = resolved_image_ref
        if not image_ref:
            image_ref = resolve_compose_service_image(
                args.artifact_dir.resolve() / "compose-resolved.json",
                args.compose_service,
            )
        manifest = build_manifest(
            artifact_dir=args.artifact_dir.resolve(),
            output=args.output.resolve(),
            image_ref=image_ref,
            signing_key_hex=args.signing_key,
            expected_sha=args.expected_sha.strip(),
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail(f"SECRETLESS_CANDIDATE_MANIFEST_BUILD_FAILED: {exc}")
    print(
        "Secretless candidate manifest built: "
        f"source_sha={manifest['source_sha']} image_digest={manifest['image_digest']} "
        f"artifacts={len(manifest['artifacts'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
