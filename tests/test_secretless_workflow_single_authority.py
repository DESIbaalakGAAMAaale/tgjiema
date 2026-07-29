"""Validate single-authority trigger model for secretless-contract-e2e workflow.

R80 P0-03: Only ONE automatic trigger source (push) may exist.
- No pull_request trigger (eliminates same-SHA dual-run cancellation).
- Concurrency group identity uses github.sha (not PR head sha).
- Release Gate closed-loop queries event=push, consistent with the authoritative trigger.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "secretless-contract-e2e.yml"
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return the parsed dict."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_on_block(workflow: dict) -> dict:
    """Retrieve the 'on' trigger block.

    PyYAML parses the bare key ``on:`` as boolean True.
    Handle both ``True`` and the string ``'on'`` for robustness.
    """
    triggers = workflow.get(True) or workflow.get("on")
    assert triggers is not None, "Workflow must have an 'on' trigger block"
    return triggers


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parsed secretless-contract-e2e.yml."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    return _load_yaml(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def release_gates() -> dict:
    """Parsed release-gates.yml."""
    assert RELEASE_GATES_PATH.exists(), f"Release gates file not found: {RELEASE_GATES_PATH}"
    return _load_yaml(RELEASE_GATES_PATH)


class TestSingleAuthorityTriggerModel:
    """R80 P0-03: single authoritative event model — push only."""

    def test_single_automatic_trigger_source(self, workflow: dict):
        """Only push (and manual workflow_dispatch) may trigger automatically.

        pull_request must NOT be present — it would create a second run for
        the same commit SHA in a single-author (same-repo PR) workflow.
        """
        triggers = _get_on_block(workflow)

        # push must exist as the sole automatic trigger
        assert "push" in triggers, "push trigger must exist"

        # pull_request must NOT exist
        assert "pull_request" not in triggers, (
            "pull_request trigger must not exist — it causes same-SHA dual runs "
            "that cancel each other via concurrency group"
        )

        # workflow_dispatch is acceptable (manual, not automatic)
        allowed_automatic = {"push"}
        automatic_triggers = set(triggers.keys()) - {"workflow_dispatch"}
        assert automatic_triggers == allowed_automatic, (
            f"Expected only {allowed_automatic} as automatic triggers, "
            f"got {automatic_triggers}"
        )

    def test_push_covers_every_pr_head_branch(self, workflow: dict):
        """Every same-repository PR head must receive an authoritative push run.

        Release Gates requires a successful event=push run for the exact current
        SHA. A remediation-only branch filter leaves ordinary fix branches with
        no possible evidence and forces the gate to time out fail-closed.
        """
        triggers = _get_on_block(workflow)
        push = triggers.get("push")
        assert isinstance(push, dict), "push trigger must declare branch coverage"

        branches = push.get("branches")
        assert branches == ["**"], (
            "push trigger must cover every branch so each PR candidate SHA has "
            f"one authoritative run, got: {branches!r}"
        )
        assert "branches-ignore" not in push, (
            "push trigger must not exclude PR head branches from authoritative runs"
        )

    def test_concurrency_uses_github_sha(self, workflow: dict):
        """Concurrency group must use github.sha for identity.

        Using github.sha (the push commit) ensures the concurrency identity is
        stable and tied to the authoritative event.  It must NOT reference
        pull_request head sha which would create a separate identity space.
        """
        concurrency = workflow.get("concurrency")
        assert concurrency is not None, "Workflow must declare a concurrency block"

        group = concurrency.get("group", "")
        assert "github.sha" in group, (
            f"Concurrency group must contain 'github.sha', got: {group!r}"
        )
        assert "pull_request" not in group, (
            f"Concurrency group must NOT reference pull_request, got: {group!r}"
        )

    def test_release_gate_accepts_push_event(self, release_gates: dict):
        """The closed-loop gate must query event=push.

        The secretless-crdb-closed-loop-gate job in release-gates.yml verifies
        that a successful secretless-contract-e2e run exists for the current SHA.
        It must filter by event=push to match the authoritative trigger.
        """
        jobs = release_gates.get("jobs", {})
        gate_job = jobs.get("secretless-crdb-closed-loop-gate")
        assert gate_job is not None, (
            "release-gates.yml must contain job 'secretless-crdb-closed-loop-gate'"
        )

        # Serialize the job to search for the event=push filter in the run script
        steps = gate_job.get("steps", [])
        run_scripts = [
            step.get("run", "") for step in steps if "run" in step
        ]
        combined_script = "\n".join(run_scripts)

        # The gh api call must include -f "event=push"
        assert 'event=push' in combined_script, (
            "secretless-crdb-closed-loop-gate must query with event=push "
            "to match the authoritative trigger source"
        )

        # Must NOT accept pull_request event as equivalent evidence
        assert 'event=pull_request' not in combined_script, (
            "Gate must NOT accept event=pull_request — only push runs are authoritative"
        )
        assert "SECRETLESS_RUN_NOT_FOUND_FOR_CURRENT_SHA" in combined_script
        assert "SECRETLESS_RUN_TERMINAL_TIMEOUT" in combined_script
        assert "SECRETLESS_RUN_TERMINAL_FAILURE" in combined_script
        assert "/actions/runs/${RUN_ID}/artifacts" in combined_script
        assert "ART_COUNT" in combined_script
        assert "sort_by(.run_number, .run_attempt) | last" in combined_script

    def test_no_pr_run_can_cancel_push_run(self, workflow: dict):
        """Without a pull_request trigger, no PR-spawned run can cancel the push run.

        In a single-author same-repo workflow, a PR head commit is identical to
        the push commit.  If both push and pull_request triggers existed, two runs
        would share the same concurrency group (github.sha) and cancel-in-progress
        would kill the first run.  Removing pull_request eliminates this entirely.
        """
        triggers = _get_on_block(workflow)

        # No pull_request trigger means no second run for the same SHA
        assert "pull_request" not in triggers, (
            "pull_request trigger would spawn a second run sharing the same "
            "concurrency group (github.sha), causing cancel-in-progress to "
            "terminate the authoritative push run"
        )

        # Confirm cancel-in-progress is set (safe now that only one trigger exists)
        concurrency = workflow.get("concurrency", {})
        assert concurrency.get("cancel-in-progress") is True, (
            "cancel-in-progress should be true — safe with single trigger source"
        )
