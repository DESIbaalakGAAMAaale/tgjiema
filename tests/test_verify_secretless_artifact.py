"""Tests for scripts/verify_secretless_artifact.py — R80 P0-05 fail-closed fix.

Verifies that a missing/invalid result.json hard-fails (exit 1) instead of the
old behavior (warning + exit 0), and that a fully valid artifact passes (exit 0).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_secretless_artifact import main as verify_main
from verify_secretless_final_result import REQUIRED_CRITERIA

# The 9 required phases (step -> name).
REQUIRED_PHASES: dict[int, str] = {
    7: "infrastructure",
    8: "migration",
    9: "start-apps",
    10: "normal-transaction",
    11: "fault-matrix",
    12: "backup-restore",
    13: "switch-rollback",
    14: "candidate-manifest",
    15: "deployment-matrix",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SL_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("SL_SHA", "abc123")
    monkeypatch.setenv("SL_RUN_ID", "12345")
    monkeypatch.setenv("SL_ATTEMPT", "1")
    monkeypatch.setenv("SL_ARTIFACT_ID", "8690204530")
    monkeypatch.setenv("SL_ARTIFACT_DIGEST", "sha256:" + "f" * 64)


def _result_path(tmp_path: Path) -> Path:
    return tmp_path / "secretless-e2e" / "result.json"


def _write_result(tmp_path: Path, payload: dict) -> Path:
    path = _result_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_phases() -> list[dict]:
    return [
        {"step": step, "name": name, "status": "success"}
        for step, name in REQUIRED_PHASES.items()
    ]


def _valid_payload() -> dict:
    return {
        "schema_version": "secretless-e2e/v1",
        "event": "push",
        "head_sha": "abc123",
        "run_id": "12345",
        "run_attempt": "1",
        "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
        "status": "completed",
        "job_status": "success",
        "result": "SECRETLESS_FUNCTIONAL_GO",
        "error_code": None,
        "first_failure": None,
        "phases": _valid_phases(),
        "total_phases": 9,
        "passed_phases": 9,
        "failed_phases": 0,
        "skipped_phases": 0,
        "validation_errors": [],
        "go_criteria": {criterion: True for criterion in REQUIRED_CRITERIA},
    }


def test_missing_result_json_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    assert verify_main() == 1


def test_empty_result_json_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    path = _result_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert verify_main() == 1


def test_invalid_json_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    path = _result_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert verify_main() == 1


def test_schema_version_invalid_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    payload["schema_version"] = "wrong"
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_event_not_push_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    payload["event"] = "pull_request"
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_identity_mismatch_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_SHA", "correct")
    payload = _valid_payload()
    payload["head_sha"] = "wrong"
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_missing_phase_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    # Only 8 phases — drop step 12 (backup-restore).
    payload["phases"] = [p for p in _valid_phases() if p["step"] != 12]
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_duplicate_phase_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    phases = _valid_phases()
    # Duplicate step 10.
    phases.append({"step": 10, "name": "normal-transaction", "status": "success"})
    payload["phases"] = phases
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_failed_phase_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    phases = _valid_phases()
    phases[0]["status"] = "failure"
    payload["phases"] = phases
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_result_not_go_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    payload = _valid_payload()
    payload["result"] = "SECRETLESS_FUNCTIONAL_NO_GO"
    _write_result(tmp_path, payload)
    assert verify_main() == 1


def test_missing_artifact_digest_exits_1(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SL_ARTIFACT_DIGEST")
    _write_result(tmp_path, _valid_payload())
    assert verify_main() == 1


def test_release_gate_binds_artifact_api_id_digest_and_terminal_state() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/release-gates.yml").read_text(
        encoding="utf-8"
    )

    job_match = re.search(
        r"(?ms)^  secretless-crdb-closed-loop-gate:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert job_match is not None
    closed_loop_job = job_match.group("body")

    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in closed_loop_job
    assert closed_loop_job.index("uses: actions/checkout@") < closed_loop_job.index(
        "python3 scripts/verify_secretless_artifact.py"
    )
    assert "ART_DIGEST=$(jq -r '.[0].digest" in workflow
    assert 'SL_ARTIFACT_DIR=/tmp/sl-artifact/artifacts' in workflow
    assert 'SL_ARTIFACT_DIR=/tmp/sl-artifact \\' not in workflow
    assert 'SL_ARTIFACT_ID="${ART_ID}" SL_ARTIFACT_DIGEST="${ART_DIGEST}"' in workflow
    assert '"selected_run_id": document["run_id"]' in workflow
    assert '"wait_seconds": int(wait_seconds)' in workflow
    assert '"final_status": status' in workflow
    assert '"final_conclusion": conclusion' in workflow
    assert "Upload Secretless closed-loop verification" in workflow


def test_valid_artifact_exits_0_and_writes_identity_evidence(tmp_path, monkeypatch):
    _set_env(monkeypatch, tmp_path)
    output = tmp_path / "closed-loop.json"
    monkeypatch.setenv("SL_VERIFICATION_OUTPUT", str(output))
    _write_result(tmp_path, _valid_payload())

    assert verify_main() == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "verified"
    assert evidence["artifact_id"] == "8690204530"
    assert evidence["artifact_digest"] == "sha256:" + "f" * 64
    assert evidence["run_id"] == "12345"
