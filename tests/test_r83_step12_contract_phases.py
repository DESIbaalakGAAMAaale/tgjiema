"""R83 Step 12 harness 回归测试：corruption copy 与 exact CRDB restore。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compose_runtime_e2e as cre

_BACKUP_STATE = {
    "schema_version": "secretless-backup-state/v1",
    "head_sha": "a" * 40,
    "backup_id": "backup-one",
    "payload_key": "db_backup/payload.enc",
    "manifest_key": "db_backup/manifest.json",
    "complete_key": "db_backup/COMPLETE.COMPLETE",
}


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(cre, "_docker_available", lambda: True)
    monkeypatch.setattr(cre, "_load_backup_state", lambda: dict(_BACKUP_STATE))
    monkeypatch.setattr(cre, "_secretless_compose_cmd", lambda args: ["docker", *args])
    monkeypatch.setattr(cre, "_env_to_compose_run_flags", lambda _env: [])
    monkeypatch.setattr(cre, "_s3_env_override", dict)
    cre._STORAGE_CONFIG.update({
        "storage_backend": "minio",
        "endpoint": "http://localhost:9000",
        "bucket": "bucket",
        "access_key": "access",
        "secret_key": "secret",
        "signing_key": "signing",
        "expect": "failure",
    })


def _restore_document() -> dict:
    snapshot = {
        "users": {"row_count": 1, "field_hash": "f" * 64, "columns": ["user_id"]}
    }
    return {
        "schema_version": "secretless-crdb-restore/v1",
        "status": "success",
        "backup_id": _BACKUP_STATE["backup_id"],
        "payload_key": _BACKUP_STATE["payload_key"],
        "manifest_key": _BACKUP_STATE["manifest_key"],
        "complete_key": _BACKUP_STATE["complete_key"],
        "manifest_sha256": "1" * 64,
        "ciphertext_sha256": "2" * 64,
        "plaintext_sha256": "3" * 64,
        "source_identity": "source-id",
        "target_identity": "target-id",
        "source_database": "tgjiema",
        "target_database": "tgjiema_restore_run_one",
        "target_dsn_sha256": "4" * 64,
        "target_before": {"user_table_count": 0, "blank": True},
        "target_schema": {"users": ["user_id"]},
        "target_after": snapshot,
        "payload_snapshot": snapshot,
        "source_unchanged": True,
        "schema_fingerprint_verified": True,
        "manifest_digest_verified": True,
        "payload_digest_verified": True,
        "complete_marker_verified": True,
        "business_probe": {"status": "pass", "row_count": 1},
    }


def test_blank_restore_uses_exact_executor_and_persists_target_state(
    tmp_path, monkeypatch
):
    _configure(monkeypatch)
    evidence_path = tmp_path / "data" / "restore-evidence.json"
    evidence_path.parent.mkdir()
    state_path = tmp_path / "restore-state.json"
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cre, "_RESTORE_STATE_FILE", state_path)
    monkeypatch.setattr(cre, "_SECRETLESS_STATE_DIR", tmp_path)
    monkeypatch.setattr(cre, "_BACKUP_ID_FILE", tmp_path / "backup-state.json")
    (tmp_path / "backup-state.json").write_text(
        json.dumps(_BACKUP_STATE), encoding="utf-8"
    )

    captured: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        document = _restore_document()
        operation_index = command.index("--operation-id") + 1
        document["operation_id"] = command[operation_index]
        evidence_path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "restore-ok", "")

    monkeypatch.setattr(cre, "_run", fake_run)
    result = cre.phase_blank_restore_from_s3_contract_store(timeout=30)

    assert result.status == "pass"
    command = captured["command"]
    assert command[command.index("-m") + 1] == "services.secretless_backup_contract"
    assert "restore-crdb" in command
    assert "services.db_backup" not in command
    for option, field in (
        ("--backup-id", "backup_id"),
        ("--payload-key", "payload_key"),
        ("--manifest-key", "manifest_key"),
        ("--complete-key", "complete_key"),
    ):
        assert command[command.index(option) + 1] == _BACKUP_STATE[field]
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["target_database"] == "tgjiema_restore_run_one"
    assert persisted["source_identity"] != persisted["target_identity"]


def test_blank_restore_rejects_incomplete_integrity_evidence(tmp_path, monkeypatch):
    _configure(monkeypatch)
    evidence_path = tmp_path / "data" / "restore-evidence.json"
    evidence_path.parent.mkdir()
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)

    def fake_run(command, **_kwargs):
        document = _restore_document()
        document["operation_id"] = command[command.index("--operation-id") + 1]
        document["source_unchanged"] = False
        evidence_path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cre, "_run", fake_run)
    result = cre.phase_blank_restore_from_s3_contract_store(timeout=30)

    assert result.status == "fail"
    assert "source_unchanged" in (result.error or "")


@pytest.mark.parametrize(
    ("returncode", "error_code"),
    [
        (2, "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH"),
        (1, "BACKUP.RESTORE.PAYLOAD_DOWNLOAD_FAILED"),
        (1, "BACKUP.RESTORE.DECRYPT_FAILED"),
        (1, "S3_CONFIG_INVALID"),
    ],
)
def test_corruption_negative_rejects_wrong_failure_class(returncode, error_code):
    assert cre._is_expected_corruption_failure(
        expect="failure",
        returncode=returncode,
        validation={"status": "failure", "error_code": error_code},
    ) is False


def test_corruption_negative_accepts_only_exact_digest_failure():
    assert cre._is_expected_corruption_failure(
        expect="failure",
        returncode=1,
        validation={
            "status": "failure",
            "error_code": "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH",
        },
    ) is True


def _install_corruption_store(
    monkeypatch,
    *,
    readback_mismatch: bool = False,
    delete_noop: bool = False,
    mutate_original_after_copy: bool = False,
) -> dict[str, bytes]:
    from storage import r2 as r2_module

    objects = {_BACKUP_STATE["payload_key"]: b"authoritative-payload"}
    state = {"corruption_uploaded": False}

    class FakeStore:
        def configure(self, **_kwargs):
            return None

        async def connect(self):
            return None

        async def close(self):
            return None

        async def list_objects(self, *, prefix: str, max_keys: int):
            return [
                {"key": key}
                for key in sorted(objects)
                if key.startswith(prefix)
            ][:max_keys]

        async def download(self, key: str):
            content = objects[key]
            if key == _BACKUP_STATE["payload_key"]:
                if mutate_original_after_copy and state["corruption_uploaded"]:
                    return content + b"-changed"
                return content
            if readback_mismatch:
                return content + b"-mismatch"
            return content

        async def upload(self, key: str, content: bytes, _content_type: str):
            objects[key] = content
            state["corruption_uploaded"] = True

        async def delete(self, key: str):
            if not delete_noop:
                objects.pop(key, None)

    monkeypatch.setattr(r2_module, "R2Storage", FakeStore)
    return objects


def _configure_corruption_phase(tmp_path: Path, monkeypatch) -> dict[str, bytes]:
    _configure(monkeypatch)
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    return _install_corruption_store(monkeypatch)


def test_corruption_timeout_still_deletes_copy_and_preserves_original(
    tmp_path, monkeypatch
):
    objects = _configure_corruption_phase(tmp_path, monkeypatch)

    def timeout_run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 30, output="partial", stderr="timeout")

    monkeypatch.setattr(cre, "_run", timeout_run)
    result = cre.phase_corrupt_payload_negative(timeout=30)

    assert result.status == "fail"
    assert result.error == "DR_VALIDATE_TIMEOUT: 30s"
    assert objects == {_BACKUP_STATE["payload_key"]: b"authoritative-payload"}
    cleanup = next(
        check
        for check in result.readiness_checks
        if check["check"] == "corruption_copy_cleanup"
    )
    assert cleanup["status"] == "pass"
    assert cleanup["copy_deleted"] is True
    assert cleanup["original_unchanged"] is True


def test_corruption_readback_mismatch_cleans_partial_copy(tmp_path, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    objects = _install_corruption_store(monkeypatch, readback_mismatch=True)

    result = cre.phase_corrupt_payload_negative(timeout=30)

    assert result.status == "fail"
    assert "CORRUPTION_COPY_READBACK_MISMATCH" in (result.error or "")
    assert objects == {_BACKUP_STATE["payload_key"]: b"authoritative-payload"}


def test_corruption_delete_verification_failure_is_fail_closed(
    tmp_path, monkeypatch
):
    _configure(monkeypatch)
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    objects = _install_corruption_store(monkeypatch, delete_noop=True)
    monkeypatch.setattr(
        cre,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", ""),
    )

    result = cre.phase_corrupt_payload_negative(timeout=30)

    assert result.status == "fail"
    assert result.error == "CORRUPTION_COPY_CLEANUP_OR_ORIGINAL_INTEGRITY_FAILED"
    assert any(".secretless-corruption/" in key for key in objects)


def test_corruption_original_digest_change_is_fail_closed(tmp_path, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    objects = _install_corruption_store(
        monkeypatch,
        mutate_original_after_copy=True,
    )
    monkeypatch.setattr(
        cre,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", ""),
    )

    result = cre.phase_corrupt_payload_negative(timeout=30)

    assert result.status == "fail"
    assert result.error == "CORRUPTION_COPY_CLEANUP_OR_ORIGINAL_INTEGRITY_FAILED"
    assert not any(".secretless-corruption/" in key for key in objects)
    cleanup = next(
        check
        for check in result.readiness_checks
        if check["check"] == "corruption_copy_cleanup"
    )
    assert cleanup["original_unchanged"] is False


def _switch_document(action: str) -> dict:
    base = {
        "schema_version": "secretless-switch-contract/v1",
        "status": "success",
        "action": action,
        "head_sha": _BACKUP_STATE["head_sha"],
        "operation_id": "operation-one",
        "source_identity": "source-id",
        "target_identity": "target-id",
    }
    if action == "switch":
        base.update({
            "active_before": {"active_identity": "source-id"},
            "active_after": {"active_identity": "target-id"},
            "target_business_probe": {"status": "pass"},
        })
    elif action == "rollback":
        base.update({
            "active_before": {"active_identity": "target-id"},
            "active_after": {"active_identity": "source-id"},
            "source_business_probe": {"status": "pass"},
        })
    return base


def _configure_switch(monkeypatch, document: dict, returncode: int = 0) -> None:
    monkeypatch.setattr(cre, "_docker_available", lambda: True)
    monkeypatch.setattr(cre, "_load_restore_state", lambda: {
        **_BACKUP_STATE,
        "operation_id": "operation-one",
        "source_identity": "source-id",
        "target_identity": "target-id",
        "source_database": "tgjiema",
        "target_database": "tgjiema_restore_run_one",
        "target_dsn_sha256": "4" * 64,
    })
    monkeypatch.setattr(
        cre,
        "_run_secretless_switch_action",
        lambda **_kwargs: (
            subprocess.CompletedProcess(["executor"], returncode, "", ""),
            document,
            ["executor"],
        ),
    )


def test_switch_executor_binds_restore_state_into_data_mount(tmp_path, monkeypatch):
    state_path = tmp_path / "artifacts" / "state" / "restore-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    evidence_path = tmp_path / "data"
    evidence_path.mkdir()
    monkeypatch.setattr(cre, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cre, "_RESTORE_STATE_FILE", state_path)
    monkeypatch.setattr(cre, "_load_restore_state", lambda: {"status": "valid"})
    monkeypatch.setattr(cre, "_s3_env_override", dict)

    captured: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output_path = evidence_path / command[command.index("--output-json") + 1].split("/")[-1]
        output_path.write_text('{"status":"success"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cre, "_run", fake_run)
    cre._run_secretless_switch_action(action="switch", timeout=30)

    command = captured["command"]
    mount = command[command.index("-v") + 1]
    assert mount == f"{state_path.resolve().as_posix()}:/app/data/restore-state.json:ro"
    assert command[command.index("--state-file") + 1] == "/app/data/restore-state.json"
    assert "/app/artifacts/secretless-e2e/state/restore-state.json" not in command


def test_secretless_switch_requires_target_identity_and_business_probe(monkeypatch):
    _configure_switch(monkeypatch, _switch_document("switch"))

    result = cre.phase_secretless_actual_switch(timeout=30)

    assert result.status == "pass"
    assert result.evidence["active_before"]["active_identity"] == "source-id"
    assert result.evidence["active_after"]["active_identity"] == "target-id"


def test_secretless_switch_rejects_target_identity_drift(monkeypatch):
    document = _switch_document("switch")
    document["active_after"]["active_identity"] = "wrong-target"
    _configure_switch(monkeypatch, document)

    result = cre.phase_secretless_actual_switch(timeout=30)

    assert result.status == "fail"
    assert "target_active" in (result.error or "")


def test_switch_probe_accepts_only_active_target_http_503(monkeypatch):
    document = {
        "schema_version": "secretless-switch-contract/v1",
        "status": "expected_failure",
        "action": "probe",
        "head_sha": _BACKUP_STATE["head_sha"],
        "operation_id": "operation-one",
        "active_identity": "target-id",
        "http_status": 503,
        "error_code": "SWITCH_PROBE_HTTP_503",
        "rollback_required": True,
    }
    _configure_switch(monkeypatch, document)
    cre._STORAGE_CONFIG["expect"] = "no-production-tag"

    result = cre.phase_switch_probe_failure(timeout=30)

    assert result.status == "pass"


def test_switch_probe_rejects_network_failure_as_503(monkeypatch):
    document = {
        "status": "failure",
        "active_identity": "target-id",
        "http_status": 0,
        "error_code": "NETWORK_ERROR",
        "rollback_required": False,
    }
    _configure_switch(monkeypatch, document, returncode=1)
    cre._STORAGE_CONFIG["expect"] = "no-production-tag"

    result = cre.phase_switch_probe_failure(timeout=30)

    assert result.status == "fail"
    assert "http_503_observed" in (result.error or "")


def test_secretless_rollback_restores_source_and_business_probe(monkeypatch):
    _configure_switch(monkeypatch, _switch_document("rollback"))

    result = cre.phase_secretless_actual_rollback(timeout=30)

    assert result.status == "pass"
    assert result.evidence["active_after"]["active_identity"] == "source-id"
