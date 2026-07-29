from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from scripts import verify_secretless_final_result as verifier
from scripts.verify_secretless_final_result import REQUIRED_PHASES, verify_result


def _result() -> dict:
    criteria = {
        "job_status_success": True,
        "all_required_phases_pass": True,
        "no_step_error_code": True,
        "no_first_failure": True,
        "no_validation_errors": True,
        "identity_complete": True,
        "identity_matches_expected": True,
        "event_authoritative": True,
        "required_phase_count_exact": True,
    }
    return {
        "schema_version": "secretless-e2e/v1",
        "status": "completed",
        "head_sha": "a" * 40,
        "run_id": "123",
        "run_attempt": "1",
        "workflow_path": ".github/workflows/secretless-contract-e2e.yml",
        "event": "push",
        "job_status": "success",
        "result": "SECRETLESS_FUNCTIONAL_GO",
        "error_code": None,
        "first_failure": None,
        "phases": [
            {"step": step, "name": name, "status": "success"}
            for step, name in REQUIRED_PHASES.items()
        ],
        "total_phases": 9,
        "passed_phases": 9,
        "failed_phases": 0,
        "skipped_phases": 0,
        "validation_errors": [],
        "go_criteria": criteria,
    }


def _verify(document: dict) -> list[str]:
    return verify_result(
        document,
        expected_sha="a" * 40,
        expected_run_id="123",
        expected_run_attempt="1",
        expected_workflow_path=".github/workflows/secretless-contract-e2e.yml",
        expected_event="push",
    )


def test_final_result_accepts_only_complete_current_run_go() -> None:
    assert _verify(_result()) == []


def test_final_result_rejects_old_sha_even_when_verdict_says_go() -> None:
    document = _result()
    document["head_sha"] = "b" * 40
    assert "RESULT_IDENTITY_MISMATCH:head_sha" in _verify(document)


def test_final_result_rejects_missing_or_duplicate_phase() -> None:
    document = _result()
    document["phases"] = deepcopy(document["phases"][:-1])
    document["phases"].append(deepcopy(document["phases"][0]))
    errors = _verify(document)
    assert "RESULT_DUPLICATE_PHASE:7" in errors
    assert "RESULT_REQUIRED_PHASE_SET_MISMATCH" in errors


def test_final_result_rejects_false_go_criterion() -> None:
    document = _result()
    document["go_criteria"]["identity_complete"] = False
    assert "RESULT_GO_CRITERION_FALSE:identity_complete" in _verify(document)


def test_final_verifier_writes_machine_readable_verified_evidence(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    verdict_path = tmp_path / "step20" / "final-verdict.json"
    result_path.write_text(json.dumps(_result()), encoding="utf-8")

    rc = verifier.main([
        "--result", str(result_path),
        "--expected-sha", "a" * 40,
        "--expected-run-id", "123",
        "--expected-run-attempt", "1",
        "--expected-workflow-path", ".github/workflows/secretless-contract-e2e.yml",
        "--expected-event", "push",
        "--output", str(verdict_path),
    ])

    assert rc == 0
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["schema_version"] == "secretless-final-verdict/v1"
    assert verdict["status"] == "verified"
    assert verdict["errors"] == []
    assert verdict["head_sha"] == "a" * 40


def test_final_verifier_persists_rejected_evidence(tmp_path: Path) -> None:
    document = _result()
    document["head_sha"] = "b" * 40
    result_path = tmp_path / "result.json"
    verdict_path = tmp_path / "step20" / "final-verdict.json"
    result_path.write_text(json.dumps(document), encoding="utf-8")

    rc = verifier.main([
        "--result", str(result_path),
        "--expected-sha", "a" * 40,
        "--expected-run-id", "123",
        "--expected-run-attempt", "1",
        "--expected-workflow-path", ".github/workflows/secretless-contract-e2e.yml",
        "--expected-event", "push",
        "--output", str(verdict_path),
    ])

    assert rc == 1
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["status"] == "rejected"
    assert "RESULT_IDENTITY_MISMATCH:head_sha" in verdict["errors"]
