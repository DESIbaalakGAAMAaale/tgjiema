from __future__ import annotations

import json
from pathlib import Path

import scripts.deployment_state_machine as dsm
import scripts.run_secretless_release_gates as gates


def test_compose_config_does_not_fallback_for_real_config_error(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return 15, "invalid interpolation: REQUIRED_VALUE is missing"

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "run_command", fake_run)

    result = gates.stage_3_compose_config()

    assert result.status == "fail"
    assert result.error_code == gates.ERROR_SECRETLESS_COMPOSE_CONFIG_FAILURE
    assert len(calls) == 1
    assert calls[0][-3:] == ["config", "--format", "json"]


def test_compose_config_fallback_is_limited_to_unknown_flag(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            (1, "unknown flag: --format"),
            (0, "services:\n  cockroachdb: {}\n"),
            (0, "service graph valid"),
        ]
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "run_command", fake_run)

    result = gates.stage_3_compose_config()

    assert result.status == "pass"
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert calls[1][-1] == "config"
    assert (tmp_path / "compose-resolved.json").read_text(encoding="utf-8").startswith("services:")


def test_backup_restore_requires_zero_wrapper_rc_for_corruption_negative(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        phase = command[command.index("--phase") + 1]
        if phase == "corrupt_payload_negative":
            return 2, "argparse configuration error"
        return 0, ""

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "run_command", fake_run)

    result = gates.stage_9_backup_restore()

    assert result.status == "fail"
    assert "wrapper_rc=2" in result.detail
    assert len(calls) == 2
    assert calls[1][calls[1].index("--expect") + 1] == "failure"


def test_switch_rollback_uses_r83_phases_and_checks_probe_rc(monkeypatch, tmp_path: Path) -> None:
    phases: list[str] = []

    def fake_run(command, **_kwargs):
        phase = command[command.index("--phase") + 1]
        phases.append(phase)
        if phase == "switch_probe_failure":
            return 2, "unexpected argparse failure"
        return 0, ""

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "run_command", fake_run)

    result = gates.stage_10_switch_rollback()

    assert result.status == "fail"
    assert phases == ["secretless_actual_switch", "switch_probe_failure"]
    assert "wrapper_rc=2" in result.detail


def test_local_stage_summary_never_hardcodes_go(monkeypatch, tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "RESULT_JSON", result_path)
    monkeypatch.setattr(gates, "_resolve_head_sha", lambda: "a" * 40)

    result = gates.stage_13_gen_result([gates.StageResult("preflight", "pass")])
    document = json.loads(result_path.read_text(encoding="utf-8"))

    assert result.status == "pass"
    assert document["schema_version"] == "secretless-local-stage-summary/v1"
    assert document["result"] == "PENDING_STRICT_FINAL_VERIFICATION"
    assert document["result"] != "SECRETLESS_FUNCTIONAL_GO"


def test_deployment_state_file_is_accepted_after_run_subcommand(tmp_path: Path) -> None:
    state_path = tmp_path / "deployment-state.json"
    args = dsm._build_parser().parse_args(
        [
            "run",
            "--state-file", str(state_path),
            "--production-tag", "rc-v1.0.83-secretless-success",
            "--source-sha", "a" * 40,
            "--image-repo-digest", "ghcr.io/secretless/tgjiema@sha256:" + "b" * 64,
            "--runtime-config-digest", "sha256:" + "c" * 64,
            "--deploy-hook-url", "http://localhost:8099/deploy-hook",
            "--deploy-probe-url", "http://localhost:8099",
        ]
    )

    assert args.state_file == state_path


def test_manifest_stage_passes_resolved_compose_to_builder(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    resolved = tmp_path / "compose-resolved.json"
    resolved.write_text('{"services":{"db_backup":{"image":"tgjiema-db_backup:ci"}}}', encoding="utf-8")

    def fake_run(command, **_kwargs):
        commands.append(command)
        return 0, "ok"

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(gates, "run_command", fake_run)
    monkeypatch.setattr(gates, "_resolve_head_sha", lambda: "a" * 40)

    result = gates.stage_11_manifest_verify()

    assert result.status == "pass"
    build_command = commands[0]
    assert build_command[build_command.index("--resolved-compose") + 1] == str(resolved)
    assert build_command[build_command.index("--compose-service") + 1] == "db_backup"
    assert "--image-ref" not in build_command


def test_step14_uses_resolved_compose_image_without_container_lookup() -> None:
    local_runner = Path(gates.__file__).read_text(encoding="utf-8")
    workflow = Path(gates.REPO_ROOT / ".github/workflows/secretless-contract-e2e.yml").read_text(
        encoding="utf-8"
    )

    assert 'docker_compose_cmd(["images", "-q", "db_backup"])' not in local_runner
    assert "images -q db_backup" not in workflow
    assert '"--resolved-compose", str(resolved_compose)' in local_runner
    assert "--resolved-compose artifacts/secretless-e2e/compose-resolved.json" in workflow
    assert "--compose-service db_backup" in workflow


def test_secretless_compose_declares_db_backup_image_identity() -> None:
    overlay = Path(gates.REPO_ROOT / "docker-compose.secretless.yml").read_text(
        encoding="utf-8"
    )
    service_block = overlay.split("  db_backup:\n", 1)[1].split("\n  volumes:", 1)[0]

    assert "image: tgjiema-secretless-db-backup:ci" in service_block
    assert "image: latest" not in service_block


def test_local_release_gate_has_no_random_deployment_identity() -> None:
    source = Path(gates.__file__).read_text(encoding="utf-8")

    assert '"scenario": "digest_drift"' not in source
    assert "ghcr.io/test/tgjiema@sha256:{gen_hex" not in source
    assert '"result": "SECRETLESS_FUNCTIONAL_GO"' not in source
    assert "runtime_config_drift" in source
    assert "verify_secretless_final_result.py" in source


def test_reset_run_owned_artifacts_preserves_unrelated_files(monkeypatch, tmp_path: Path) -> None:
    owned_phase = tmp_path / "phases" / "12-backup-restore.json"
    owned_phase.parent.mkdir(parents=True)
    owned_phase.write_text("old phase", encoding="utf-8")
    owned_result = tmp_path / "result.json"
    owned_result.write_text("old result", encoding="utf-8")
    unrelated = tmp_path / "user-evidence.json"
    unrelated.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(gates, "ARTIFACT_DIR", tmp_path)
    gates._reset_run_owned_artifacts()

    assert not owned_phase.parent.exists()
    assert not owned_result.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_cleanup_nonzero_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(gates, "run_command", lambda *_args, **_kwargs: (19, "volume busy"))

    result = gates.stage_14_cleanup()

    assert result.status == "fail"
    assert result.error_code == gates.ERROR_SECRETLESS_CLEANUP_FAILURE
    assert "rc=19" in result.detail
    assert "volume busy" in result.detail


def test_release_gate_closed_loop_heredoc_uses_yaml_block_indent() -> None:
    workflow = Path(gates.REPO_ROOT / ".github/workflows/release-gates.yml").read_text(
        encoding="utf-8"
    )

    assert '                "${SECRETLESS_CLOSED_LOOP_STATE}" <<\'PY\'\n          import json' in workflow
    assert '\n          PY\n              echo "PASS: secretless-contract-e2e run' in workflow
    assert '\n              import json\n              import sys\n' not in workflow


def test_workflow_phase_markers_write_real_newlines() -> None:
    workflow = Path(gates.REPO_ROOT / ".github/workflows/secretless-contract-e2e.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count('write_text(json.dumps(out,sort_keys=True)+"\\n")') == 2
    assert 'write_text(json.dumps(out,sort_keys=True)+"\\\\n")' not in workflow


def test_workflow_finalizer_uses_current_job_status_and_full_run_identity() -> None:
    workflow = Path(gates.REPO_ROOT / ".github/workflows/secretless-contract-e2e.yml").read_text(
        encoding="utf-8"
    )

    assert '"schema_version": "secretless-e2e/v1"' in workflow
    assert "if: always()" in workflow
    assert "--job-status '${{ job.status }}'" in workflow
    assert "--expected-sha '${{ github.sha }}'" in workflow
    assert "--expected-run-id '${{ github.run_id }}'" in workflow
    assert "--expected-run-attempt '${{ github.run_attempt }}'" in workflow
    assert "--expected-workflow-path '.github/workflows/secretless-contract-e2e.yml'" in workflow
    assert "--expected-event '${{ github.event_name }}'" in workflow
    assert 'name: "Step 19: Re-finalize result after cleanup (always)"' in workflow
    assert '"error_code": "" if returncode == 0 else "SECRETLESS_CLEANUP_FAILED"' in workflow
    assert 'exit "${CLEANUP_RC}"' in workflow
    assert "down -v --remove-orphans || true" not in workflow
    assert workflow.count("scripts/finalize_secretless_result.py") == 2
    assert workflow.index('"schema_version": "secretless-e2e/v1"') < workflow.index("docker --version")
