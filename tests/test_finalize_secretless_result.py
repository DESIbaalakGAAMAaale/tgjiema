"""Tests for scripts/finalize_secretless_result.py — R80 P0-04 fix.

Verifies that the all([]) == True false-GO vulnerability is eliminated:
GO requires all 9 required phases to exist, be unique, name-matched, and
status=success.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import finalize_secretless_result as mod
from finalize_secretless_result import (
    REQUIRED_PHASES,
    _validate_phases,
    build_result,
)

ALL_PHASES = {
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


def _write_identity(output: Path, event: str = "push") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "head_sha": "abc123",
                "run_id": "123",
                "run_attempt": "1",
                "workflow_path": "secretless-contract-e2e.yml",
                "event": event,
            }
        ),
        encoding="utf-8",
    )


def _write_phase(phases_dir: Path, step: int, name: str, status: str = "success",
                 error_code: str = "") -> None:
    phases_dir.mkdir(parents=True, exist_ok=True)
    doc = {"step": step, "name": name, "status": status, "error_code": error_code}
    fname = f"step-{step:02d}-{name}.json"
    (phases_dir / fname).write_text(json.dumps(doc), encoding="utf-8")


def _write_all_success(phases_dir: Path) -> None:
    for step, name in ALL_PHASES.items():
        _write_phase(phases_dir, step, name, status="success")


def test_zero_phases_never_go(tmp_path, monkeypatch):
    """R80 P0-04 core: zero phases must NEVER produce GO (all([]) trap)."""
    monkeypatch.setattr(mod, "PHASES_DIR", tmp_path / "phases")
    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["go_criteria"]["all_required_phases_pass"] is False
    assert result["total_phases"] == 9  # all 9 enriched as skipped
    assert result["skipped_phases"] == 9


def test_one_success_phase_never_go(tmp_path, monkeypatch):
    """A single successful phase is not enough — all 9 required."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    _write_phase(phases_dir, 7, "infrastructure", status="success")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["passed_phases"] == 1
    assert result["skipped_phases"] == 8


def test_missing_required_phase_never_go(tmp_path, monkeypatch):
    """8 of 9 phases (missing step 12) must yield NO_GO + missing_phase error."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        if step == 12:
            continue
        _write_phase(phases_dir, step, name, status="success")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert any("missing_phase" in e for e in result["validation_errors"])
    assert any("step=12" in e for e in result["validation_errors"])


def test_duplicate_phase_rejected(tmp_path, monkeypatch):
    """Duplicate step (10 twice) must yield NO_GO + duplicate_phase error."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        _write_phase(phases_dir, step, name, status="success")
    # Duplicate step 10 with a different file name
    (phases_dir / "step-10-duplicate.json").write_text(
        json.dumps({"step": 10, "name": "normal-transaction", "status": "success"}),
        encoding="utf-8",
    )

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert any("duplicate_phase" in e for e in result["validation_errors"])


def test_unknown_phase_rejected(tmp_path, monkeypatch):
    """An unknown phase (step=99) must yield NO_GO + unknown_phase error."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    _write_all_success(phases_dir)
    _write_phase(phases_dir, 99, "bogus", status="success")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert any("unknown_phase" in e for e in result["validation_errors"])


def test_phase_name_mismatch_rejected(tmp_path, monkeypatch):
    """Step 11 with wrong name must yield NO_GO + phase_name_mismatch error."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        if step == 11:
            _write_phase(phases_dir, step, "wrong-name", status="success")
        else:
            _write_phase(phases_dir, step, name, status="success")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert any("phase_name_mismatch" in e for e in result["validation_errors"])


def test_failure_phase_never_go(tmp_path, monkeypatch):
    """All 9 phases present but one failure must yield NO_GO."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        status = "failure" if step == 8 else "success"
        _write_phase(phases_dir, step, name, status=status,
                     error_code="MIGRATION_FAILED" if step == 8 else "")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["failed_phases"] == 1
    assert result["first_failure"]["step"] == 8


def test_skipped_phase_never_go(tmp_path, monkeypatch):
    """All 9 phases present but one skipped must yield NO_GO."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        status = "skipped" if step == 13 else "success"
        _write_phase(phases_dir, step, name, status=status)

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["skipped_phases"] == 1
    assert result["first_failure"]["step"] == 13


def test_all_required_phases_success_go(tmp_path, monkeypatch):
    """All 9 phases success + identity + push event + job success => GO."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    _write_all_success(phases_dir)

    output = tmp_path / "result.json"
    _write_identity(output, event="push")

    result = build_result(job_status="success", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_GO"
    assert result["total_phases"] == 9
    assert result["passed_phases"] == 9
    assert result["failed_phases"] == 0
    assert result["skipped_phases"] == 0
    assert result["validation_errors"] == []
    assert result["first_failure"] is None
    assert all(result["go_criteria"].values())


def test_cleanup_error_does_not_replace_primary_failure(tmp_path, monkeypatch):
    """first_failure must point to the original phase failure (step 8),
    not be overwritten by cleanup/secondary errors."""
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    for step, name in ALL_PHASES.items():
        if step == 8:
            _write_phase(phases_dir, step, name, status="failure",
                         error_code="MIGRATION_FAILED")
        else:
            _write_phase(phases_dir, step, name, status="success")

    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="failure", error_code="", output=output)

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["first_failure"] is not None
    assert result["first_failure"]["step"] == 8
    assert result["first_failure"]["name"] == "migration"
    assert result["first_failure"]["error_code"] == "MIGRATION_FAILED"
    # primary error_code falls back to first_failure's error_code
    assert result["error_code"] == "MIGRATION_FAILED"


def test_step12_subphase_failure_replaces_phase_not_executed(tmp_path, monkeypatch):
    phases_dir = tmp_path / "phases"
    step12_dir = tmp_path / "step12"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    monkeypatch.setattr(mod, "STEP12_DIR", step12_dir)
    monkeypatch.setattr(mod, "FAILURE_SUMMARY", tmp_path / "failure-summary.json")
    for step, name in ALL_PHASES.items():
        if step >= 12:
            continue
        _write_phase(phases_dir, step, name, status="success")
    step12_dir.mkdir(parents=True)
    (step12_dir / "full_backup_to_s3_contract_store.json").write_text(
        json.dumps({
            "phases": [{
                "phase": "full_backup_to_s3_contract_store",
                "status": "fail",
                "error": "BACKUP_CONTRACT_PREFLIGHT_FAILED: missing bucket",
                "returncode": 1,
                "readiness_checks": [{
                    "check": "backup_contract_preflight",
                    "status": "fail",
                    "error_code": "BACKUP_CONTRACT_PREFLIGHT_FAILED",
                }],
            }],
        }),
        encoding="utf-8",
    )
    (step12_dir / "full_backup_to_s3_contract_store.stderr.log").write_text(
        "first error", encoding="utf-8",
    )
    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="failure", error_code="", output=output)

    assert result["first_failure"]["step"] == 12
    assert result["first_failure"]["subphase"] == "full_backup_to_s3_contract_store"
    assert result["first_failure"]["error_code"] == "BACKUP_CONTRACT_PREFLIGHT_FAILED"
    assert result["first_failure"]["returncode"] == 1
    summary = json.loads((tmp_path / "failure-summary.json").read_text(encoding="utf-8"))
    assert len(summary["log_sha256"]["stderr.log"]) == 64


def test_step12_expected_corruption_failure_is_not_primary_failure(tmp_path, monkeypatch):
    """合法 expected-failure 可含内部非零 rc，但 wrapper pass 后不是首错。"""
    phases_dir = tmp_path / "phases"
    step12_dir = tmp_path / "step12"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    monkeypatch.setattr(mod, "STEP12_DIR", step12_dir)
    monkeypatch.setattr(mod, "FAILURE_SUMMARY", tmp_path / "failure-summary.json")
    for step, name in ALL_PHASES.items():
        if step >= 12:
            continue
        _write_phase(phases_dir, step, name, status="success")
    step12_dir.mkdir(parents=True)
    (step12_dir / "full_backup_to_s3_contract_store.json").write_text(
        json.dumps({"phases": [{"status": "pass", "returncode": 0}]}),
        encoding="utf-8",
    )
    (step12_dir / "corrupt_payload_negative.json").write_text(
        json.dumps({
            "phases": [{
                "status": "pass",
                "returncode": 0,
                "evidence": {
                    "expected_failure": True,
                    "error_code": "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH",
                    "command": {"returncode": 1},
                },
            }],
        }),
        encoding="utf-8",
    )
    (step12_dir / "blank_restore_from_s3_contract_store.json").write_text(
        json.dumps({
            "phases": [{
                "status": "fail",
                "error": "RESTORE_TARGET_NOT_BLANK",
                "returncode": 1,
            }],
        }),
        encoding="utf-8",
    )
    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="failure", error_code="", output=output)

    assert result["first_failure"]["subphase"] == "blank_restore_from_s3_contract_store"
    assert result["first_failure"]["error_code"] == "RESTORE_TARGET_NOT_BLANK"


def test_step12_nested_command_returncode_is_preferred(tmp_path, monkeypatch):
    phases_dir = tmp_path / "phases"
    step12_dir = tmp_path / "step12"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    monkeypatch.setattr(mod, "STEP12_DIR", step12_dir)
    monkeypatch.setattr(mod, "FAILURE_SUMMARY", tmp_path / "failure-summary.json")
    for step, name in ALL_PHASES.items():
        if step >= 12:
            continue
        _write_phase(phases_dir, step, name, status="success")
    step12_dir.mkdir(parents=True)
    (step12_dir / "full_backup_to_s3_contract_store.json").write_text(
        json.dumps({
            "phases": [{
                "status": "fail",
                "returncode": 1,
                "error": "BACKUP_COMMAND_FAILED",
                "evidence": {"command": {"returncode": 17}},
            }],
        }),
        encoding="utf-8",
    )
    output = tmp_path / "result.json"
    _write_identity(output)

    result = build_result(job_status="failure", error_code="", output=output)

    assert result["first_failure"]["returncode"] == 17


def test_current_run_identity_mismatch_never_go(tmp_path, monkeypatch):
    phases_dir = tmp_path / "phases"
    monkeypatch.setattr(mod, "PHASES_DIR", phases_dir)
    _write_all_success(phases_dir)
    output = tmp_path / "result.json"
    _write_identity(output, event="push")

    result = build_result(
        job_status="success",
        error_code="",
        output=output,
        expected_identity={
            "head_sha": "different-sha",
            "run_id": "123",
            "run_attempt": "1",
            "workflow_path": "secretless-contract-e2e.yml",
            "event": "push",
        },
    )

    assert result["result"] == "SECRETLESS_FUNCTIONAL_NO_GO"
    assert result["go_criteria"]["identity_matches_expected"] is False


def test_validate_phases_direct_empty():
    """Direct unit check on _validate_phases: empty list never passes."""
    all_pass, enriched, errors = _validate_phases([])
    assert all_pass is False
    assert len(enriched) == 9
    assert len(errors) == 9
    assert all("missing_phase" in e for e in errors)


def test_required_phases_constant():
    """REQUIRED_PHASES must be exactly the 9 expected phases."""
    assert REQUIRED_PHASES == ALL_PHASES
    assert len(REQUIRED_PHASES) == 9
