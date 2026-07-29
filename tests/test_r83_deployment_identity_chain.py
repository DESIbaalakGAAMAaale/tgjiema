from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import deployment_state_machine as dsm
from tests.support import deployment_simulator as simulator


def _identity_files(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source_sha = "a" * 40
    source_identity = "1" * 16
    target_identity = "2" * 16
    candidate = {
        "schema_version": dsm.SECRETLESS_CANDIDATE_SCHEMA,
        "kind": "secretless-candidate-manifest",
        "source_sha": source_sha,
        "image_digest": "sha256:" + "b" * 64,
        "runtime_config_digest": "sha256:" + "c" * 64,
        "restore_operation_id": "operation-one",
        "source_database_identity": source_identity,
        "target_database_identity": target_identity,
    }
    rollback_document = {
        "schema_version": dsm.SECRETLESS_ROLLBACK_SCHEMA,
        "status": "success",
        "action": "rollback",
        "head_sha": source_sha,
        "operation_id": "operation-one",
        "source_identity": source_identity,
        "target_identity": target_identity,
        "active_before": {"active_identity": target_identity},
        "active_after": {"active_identity": source_identity},
        "source_business_probe": {"status": "pass"},
        "command": {"argv": ["executor", "rollback"], "returncode": 0},
    }
    rollback = {
        "schema_version": dsm.SECRETLESS_PHASE_ENVELOPE_SCHEMA,
        "overall_passed": True,
        "phases": [
            {
                "phase": dsm.SECRETLESS_ROLLBACK_PHASE,
                "status": "pass",
                "returncode": 0,
                "evidence": rollback_document,
            }
        ],
    }
    candidate_path = tmp_path / "candidate.json"
    rollback_path = tmp_path / "rollback.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    rollback_path.write_text(json.dumps(rollback), encoding="utf-8")
    identity = {
        "source_sha": source_sha,
        "image_digest": candidate["image_digest"],
        "runtime_config_digest": candidate["runtime_config_digest"],
        "source_database_identity": source_identity,
        "target_database_identity": target_identity,
        "rollback_source_identity": source_identity,
        "candidate_manifest_sha256": "sha256:"
        + hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
    }
    return candidate_path, rollback_path, identity


def test_load_secretless_identity_requires_successful_rollback(tmp_path: Path) -> None:
    candidate_path, rollback_path, identity = _identity_files(tmp_path)
    assert dsm._load_secretless_identity(candidate_path, rollback_path) == identity

    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    rollback["phases"][0]["evidence"]["active_after"]["active_identity"] = "f" * 16
    rollback_path.write_text(json.dumps(rollback), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        dsm._load_secretless_identity(candidate_path, rollback_path)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (lambda document: document.update({"schema_version": dsm.SECRETLESS_ROLLBACK_SCHEMA}), "envelope schema"),
        (lambda document: document.update({"overall_passed": False}), "did not pass"),
        (lambda document: document["phases"][0].update({"status": "fail"}), "identity/status"),
        (lambda document: document["phases"][0].update({"returncode": 1}), "phase returncode"),
        (lambda document: document["phases"][0]["evidence"]["command"].update({"returncode": 1}), "nested command"),
        (lambda document: document["phases"][0].pop("evidence"), "evidence must be an object"),
    ],
)
def test_load_secretless_identity_rejects_invalid_phase_envelope(
    tmp_path: Path,
    capsys,
    mutation,
    expected_fragment: str,
) -> None:
    candidate_path, rollback_path, _identity = _identity_files(tmp_path)
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    mutation(rollback)
    rollback_path.write_text(json.dumps(rollback), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        dsm._load_secretless_identity(candidate_path, rollback_path)

    assert exc.value.code == 2
    assert expected_fragment in capsys.readouterr().err


def test_load_secretless_identity_rejects_database_identity_drift(tmp_path: Path) -> None:
    candidate_path, rollback_path, _identity = _identity_files(tmp_path)
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    rollback["phases"][0]["evidence"]["source_identity"] = "f" * 16
    rollback_path.write_text(json.dumps(rollback), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        dsm._load_secretless_identity(candidate_path, rollback_path)

    assert exc.value.code == 2


def test_secretless_init_persists_manifest_and_database_identity(tmp_path: Path) -> None:
    _candidate, _rollback, identity = _identity_files(tmp_path)
    state_path = tmp_path / "state.json"
    machine = dsm.DeploymentStateMachine(state_path)

    deployment_id = machine.init_deployment(
        production_tag="rc-v1.0.83-secretless-success",
        source_sha=identity["source_sha"],
        image_repo_digest="ghcr.io/secretless/tgjiema@" + identity["image_digest"],
        runtime_config_digest=identity["runtime_config_digest"],
        deploy_hook_url="http://127.0.0.1:8099/deploy-hook",
        deploy_probe_url="http://127.0.0.1:8099",
        secretless_identity=identity,
    )

    assert deployment_id.startswith("secretless-")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["secretless_mode"] is True
    assert persisted["identity_restored"] is True
    assert persisted["candidate_manifest_sha256"] == identity["candidate_manifest_sha256"]
    assert persisted["source_database_identity"] == identity["source_database_identity"]
    assert persisted["target_database_identity"] == identity["target_database_identity"]


def test_runtime_config_drift_scenario_preserves_image_and_drifts_config() -> None:
    simulator._state.expected_image_repo_digest = "ghcr.io/example/app@sha256:" + "a" * 64
    simulator._state.expected_runtime_config_digest = "sha256:" + "b" * 64
    simulator._set_scenario("runtime_config_drift")

    response = simulator.health()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["image_repo_digest"] == simulator._state.expected_image_repo_digest
    assert payload["runtime_config_digest"] != simulator._state.expected_runtime_config_digest
    assert payload["scenario"] == "runtime_config_drift"


def test_secretless_verified_requires_restored_source_identity(
    tmp_path: Path, monkeypatch
) -> None:
    state = dsm.DeploymentState(
        current_state=dsm.STATE_DEPLOYED,
        deployment_id="secretless-one",
        secretless_mode=True,
        identity_restored=False,
        deploy_probe_url="http://127.0.0.1:8099",
    )
    machine = dsm.DeploymentStateMachine(tmp_path / "state.json", initial=state)
    machine._persist()
    monkeypatch.setattr(
        dsm,
        "_http_request",
        lambda *_args, **_kwargs: (200, '{"status":"ok"}', {}),
    )

    with pytest.raises(SystemExit) as exc:
        machine.verify_business_probe()
    assert exc.value.code == 1
    assert machine.state.current_state == dsm.STATE_FAILED
