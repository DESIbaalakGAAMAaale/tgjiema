from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from scripts import build_secretless_candidate_manifest as builder
from scripts import validate_candidate_manifest as validator


def _write_resolved_compose(path: Path, image: object = "tgjiema-db_backup:ci") -> None:
    path.write_text(
        json.dumps({"services": {"db_backup": {"image": image}}}),
        encoding="utf-8",
    )


def test_resolve_compose_service_image_reads_current_run_image(tmp_path: Path) -> None:
    resolved = tmp_path / "compose-resolved.json"
    _write_resolved_compose(resolved)

    assert builder.resolve_compose_service_image(resolved, "db_backup") == "tgjiema-db_backup:ci"


@pytest.mark.parametrize(
    ("document", "error_type", "message"),
    [
        ({"services": {}}, ValueError, "missing service"),
        ({"services": {"db_backup": {}}}, TypeError, "image must be a string"),
        ({"services": {"db_backup": {"image": None}}}, TypeError, "image must be a string"),
        ({"services": {"db_backup": {"image": 7}}}, TypeError, "image must be a string"),
        ({"services": {"db_backup": {"image": "   "}}}, ValueError, "image is empty"),
        (
            {"services": {"db_backup": {"image": "${TGJIEMA_IMAGE}"}}},
            ValueError,
            "unresolved placeholder",
        ),
    ],
)
def test_resolve_compose_service_image_fails_closed(
    tmp_path: Path,
    document: dict,
    error_type: type[Exception],
    message: str,
) -> None:
    resolved = tmp_path / "compose-resolved.json"
    resolved.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(error_type, match=message):
        builder.resolve_compose_service_image(resolved, "db_backup")


def test_validator_import_does_not_exit_when_jsonschema_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(validator, "jsonschema", None)
    monkeypatch.setattr(validator, "Draft202012Validator", None)

    valid, errors = validator.validate_manifest(Path("missing.json"), Path("missing.schema.json"))

    assert not valid
    assert errors == [
        "jsonschema library required for candidate manifest validation; "
        "install the declared project dependencies"
    ]


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _manifest(artifact_dir: Path, key: bytes) -> dict:
    evidence = artifact_dir / "step13" / "actual_rollback.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"status":"success"}\n', encoding="utf-8")
    source_sha = "a" * 40
    manifest = {
        "schema_version": "secretless-candidate-manifest/v1",
        "kind": "secretless-candidate-manifest",
        "source_sha": source_sha,
        "tree_sha": "b" * 40,
        "run_id": "123",
        "run_attempt": "1",
        "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
        "event": "push",
        "ref": "refs/heads/r73-remediation-final",
        "conclusion": "success",
        "image_digest": "sha256:" + "c" * 64,
        "runtime_config_digest": "sha256:" + "d" * 64,
        "artifact_identity": {
            "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
            "run_id": "123",
            "run_attempt": "1",
            "event": "push",
            "source_sha": source_sha,
        },
        "backup_id": "backup-1",
        "restore_operation_id": "restore-1",
        "source_database_identity": "1" * 16,
        "target_database_identity": "2" * 16,
        "artifacts": [
            {
                "name": "actual_rollback",
                "path": "step13/actual_rollback.json",
                "sha256": "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
        "generated_at": "2026-07-29T00:00:00+00:00",
        "payload_digest": "",
        "signature_identity": "secretless-run-123-1",
        "signature": "",
    }
    payload = {k: v for k, v in manifest.items() if k not in {"payload_digest", "signature"}}
    manifest["payload_digest"] = "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    manifest["signature"] = hmac.new(key, _canonical(signed), hashlib.sha256).hexdigest()
    return manifest


def test_secretless_manifest_semantics_artifacts_payload_and_signature(tmp_path: Path) -> None:
    key = bytes.fromhex("ab" * 32)
    manifest = _manifest(tmp_path, key)

    valid, errors = validator.validate_semantics(
        manifest,
        {
            "run_id": "123",
            "run_attempt": "1",
            "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
            "event": "push",
            "ref": "refs/heads/r73-remediation-final",
            "source_sha": "a" * 40,
        },
    )
    assert valid, errors
    valid, errors = validator.verify_artifacts(manifest, tmp_path)
    assert valid, errors
    valid, errors = validator.verify_payload_digest(manifest)
    assert valid, errors
    valid, errors = validator.verify_manifest_signature(
        manifest,
        {"secretless-run-123-1": key},
    )
    assert valid, errors


def test_secretless_manifest_rejects_identity_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, bytes.fromhex("ab" * 32))
    manifest["artifact_identity"]["source_sha"] = "f" * 40
    valid, errors = validator.validate_semantics(manifest)
    assert not valid
    assert any("artifact_identity.source_sha" in error for error in errors)


def test_secretless_manifest_rejects_artifact_path_escape(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, bytes.fromhex("ab" * 32))
    manifest["artifacts"][0]["path"] = "../outside.json"
    valid, errors = validator.verify_artifacts(manifest, tmp_path)
    assert not valid
    assert any("escapes artifact directory" in error for error in errors)


def test_secretless_manifest_rejects_artifact_digest_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, bytes.fromhex("ab" * 32))
    (tmp_path / "step13" / "actual_rollback.json").write_text(
        '{"status":"tampered"}\n', encoding="utf-8"
    )
    valid, errors = validator.verify_artifacts(manifest, tmp_path)
    assert not valid
    assert any("digest mismatch" in error for error in errors)
