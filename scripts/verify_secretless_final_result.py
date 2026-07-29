#!/usr/bin/env python3
"""Fail-closed R83 Step 20 verifier for the finalized Secretless result.json."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

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
REQUIRED_CRITERIA = {
    "job_status_success",
    "all_required_phases_pass",
    "no_step_error_code",
    "no_first_failure",
    "no_validation_errors",
    "identity_complete",
    "identity_matches_expected",
    "event_authoritative",
    "required_phase_count_exact",
}


def verify_result(
    document: dict[str, Any],
    *,
    expected_sha: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_workflow_path: str,
    expected_event: str,
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "head_sha": expected_sha,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
        "workflow_path": expected_workflow_path,
        "event": expected_event,
    }
    if document.get("schema_version") != "secretless-e2e/v1":
        errors.append("RESULT_SCHEMA_MISMATCH")
    if document.get("status") != "completed":
        errors.append("RESULT_NOT_COMPLETED")
    if document.get("job_status") != "success":
        errors.append("RESULT_JOB_NOT_SUCCESS")
    if document.get("result") != "SECRETLESS_FUNCTIONAL_GO":
        errors.append("RESULT_VERDICT_NOT_GO")
    for field, expected in expected_identity.items():
        if str(document.get(field, "")) != expected:
            errors.append(f"RESULT_IDENTITY_MISMATCH:{field}")
    if document.get("error_code") not in (None, ""):
        errors.append("RESULT_ERROR_CODE_PRESENT")
    if document.get("first_failure") is not None:
        errors.append("RESULT_FIRST_FAILURE_PRESENT")
    if document.get("validation_errors") != []:
        errors.append("RESULT_VALIDATION_ERRORS_PRESENT")

    phases = document.get("phases")
    if not isinstance(phases, list):
        errors.append("RESULT_PHASES_INVALID")
        phases = []
    seen: set[int] = set()
    for phase in phases:
        if not isinstance(phase, dict):
            errors.append("RESULT_PHASE_ENTRY_INVALID")
            continue
        step = phase.get("step")
        name = phase.get("name")
        if not isinstance(step, int) or step not in REQUIRED_PHASES:
            errors.append(f"RESULT_UNKNOWN_PHASE:{step}")
            continue
        if step in seen:
            errors.append(f"RESULT_DUPLICATE_PHASE:{step}")
        seen.add(step)
        if name != REQUIRED_PHASES[step]:
            errors.append(f"RESULT_PHASE_NAME_MISMATCH:{step}")
        if phase.get("status") != "success":
            errors.append(f"RESULT_PHASE_NOT_SUCCESS:{step}")
    if seen != set(REQUIRED_PHASES):
        errors.append("RESULT_REQUIRED_PHASE_SET_MISMATCH")
    if document.get("total_phases") != len(REQUIRED_PHASES):
        errors.append("RESULT_TOTAL_PHASE_COUNT_MISMATCH")
    if document.get("passed_phases") != len(REQUIRED_PHASES):
        errors.append("RESULT_PASSED_PHASE_COUNT_MISMATCH")
    if document.get("failed_phases") != 0 or document.get("skipped_phases") != 0:
        errors.append("RESULT_NONPASS_PHASE_COUNT_PRESENT")

    criteria = document.get("go_criteria")
    if not isinstance(criteria, dict):
        errors.append("RESULT_GO_CRITERIA_INVALID")
    else:
        if set(criteria) != REQUIRED_CRITERIA:
            errors.append("RESULT_GO_CRITERIA_SET_MISMATCH")
        for key in REQUIRED_CRITERIA:
            if criteria.get(key) is not True:
                errors.append(f"RESULT_GO_CRITERION_FALSE:{key}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-run-attempt", required=True)
    parser.add_argument("--expected-workflow-path", required=True)
    parser.add_argument("--expected-event", required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::SECRETLESS_FINAL_RESULT_INVALID: {exc}", file=sys.stderr)
        return 1
    if not isinstance(document, dict):
        print("::error::SECRETLESS_FINAL_RESULT_ROOT_INVALID", file=sys.stderr)
        return 1
    errors = verify_result(
        document,
        expected_sha=args.expected_sha,
        expected_run_id=args.expected_run_id,
        expected_run_attempt=args.expected_run_attempt,
        expected_workflow_path=args.expected_workflow_path,
        expected_event=args.expected_event,
    )
    verdict = {
        "schema_version": "secretless-final-verdict/v1",
        "status": "verified" if not errors else "rejected",
        "result": document.get("result"),
        "head_sha": args.expected_sha,
        "run_id": args.expected_run_id,
        "run_attempt": args.expected_run_attempt,
        "workflow_path": args.expected_workflow_path,
        "event": args.expected_event,
        "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "errors": errors,
    }
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(args.output)
        except OSError as exc:
            print(f"::error::SECRETLESS_FINAL_VERDICT_WRITE_FAILED: {exc}", file=sys.stderr)
            return 1
    if errors:
        for error in errors:
            print(f"::error::{error}", file=sys.stderr)
        return 1
    print("SECRETLESS_FINAL_RESULT_VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
