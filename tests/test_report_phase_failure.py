from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import report_phase_failure as mod


def test_build_report_extracts_nested_returncode_and_redacts(tmp_path):
    evidence = tmp_path / "phase.json"
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    evidence.write_text(
        json.dumps({
            "phases": [{
                "status": "fail",
                "returncode": 1,
                "error": "restore failed token=super-secret",
                "evidence": {
                    "command": {
                        "returncode": 17,
                        "argv": [
                            "python",
                            "restore.py",
                            "--secret-key",
                            "secret-value",
                            "--access-key=access-value",
                        ],
                    },
                },
                "readiness_checks": [
                    {"check": "restore_triggered", "status": "fail"},
                ],
            }],
        }),
        encoding="utf-8",
    )
    stdout.write_text("ok", encoding="utf-8")
    stderr.write_text("Authorization: Bearer abcdef\npassword=hunter2", encoding="utf-8")

    report = mod.build_report(
        evidence,
        phase="blank_restore",
        wrapper_returncode=1,
        stdout_log=stdout,
        stderr_log=stderr,
        max_chars=512,
    )

    assert report["returncode"] == 17
    assert report["failed_readiness_check"] == "restore_triggered"
    serialized = json.dumps(report)
    assert "super-secret" not in serialized
    assert "secret-value" not in serialized
    assert "access-value" not in serialized
    assert "hunter2" not in serialized
    assert "abcdef" not in serialized


def test_tail_text_truncates_and_keeps_tail():
    result = mod.tail_text("prefix-" + "x" * 500 + "-tail", 256)
    assert result.startswith("...[truncated ")
    assert result.endswith("-tail")
    assert len(result) < 320


def test_build_report_rejects_malformed_and_empty_json(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{broken", encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")

    for path in (malformed, empty):
        try:
            mod.build_report(
                path,
                phase="phase",
                wrapper_returncode=1,
                stdout_log=None,
                stderr_log=None,
                max_chars=512,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"{path.name} must be rejected")


def test_annotation_escaping():
    escaped = mod.escape_annotation("a%b:c,d\ne\r")
    assert escaped == "a%25b%3Ac%2Cd%0Ae%0D"


def test_main_emits_structured_fallback_for_invalid_evidence(tmp_path, capsys):
    evidence = tmp_path / "bad.json"
    evidence.write_text("[]", encoding="utf-8")

    rc = mod.main([
        "--evidence",
        str(evidence),
        "--phase",
        "blank_restore",
        "--wrapper-returncode",
        "2",
    ])

    output = capsys.readouterr().out
    assert rc == 2
    assert "::error title=" in output
    assert "PHASE_EVIDENCE_INVALID" in output
    assert '"returncode": 2' in output
