#!/usr/bin/env python3
"""R73 §5.7: Typed evidence envelope for release-gate artifacts.

Every artifact produced by the release-gates workflow carries a typed
envelope with the following structure (R73 §5.7):

    {
      "schema_version": "1",
      "gate_level": "development|rc|production",
      "event": "push|workflow_dispatch",
      "ref": "refs/heads/master|refs/tags/rc-v...|refs/tags/production-v...",
      "source_sha": "40-hex",
      "run_id": 0,
      "run_attempt": 1,
      "workflow_path": ".github/workflows/release-gates.yml",
      "overall_conclusion": "success|failure|blocked",
      "promotion_eligible": false,
      "image_repo_digest": "ghcr.io/...@sha256:...",
      "runtime_config_digest": "sha256:...",
      "generated_at": "RFC3339",
      "payload_digest": "sha256:..."
    }

Promotion rules (R73 §5.7):
    - master run only generates gate_level=development and promotion_eligible=false
    - RC run only generates gate_level=rc and (if all gates pass) promotion_eligible=true
    - failed/cancelled/skipped run NEVER generates promotable evidence
    - artifact names are tiered:
        * development-evidence-*
        * rc-candidate-evidence-*
        * production-deployment-evidence-*

This module provides builders, validators, and audit helpers for the
typed envelope. ``is_promotion_eligible`` is the authoritative
consumer-side audit (defense in depth — it does NOT trust the
envelope's own ``promotion_eligible`` field).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# R73 §5.7: Envelope schema version
ENVELOPE_SCHEMA_VERSION = "1"

# All required fields in a typed envelope (R73 §5.7)
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "gate_level",
    "event",
    "ref",
    "source_sha",
    "run_id",
    "run_attempt",
    "workflow_path",
    "overall_conclusion",
    "promotion_eligible",
    "image_repo_digest",
    "runtime_config_digest",
    "generated_at",
    "payload_digest",
)

# Valid enum values (R73 §5.7)
VALID_GATE_LEVELS: frozenset[str] = frozenset({"development", "rc", "production"})
VALID_CONCLUSIONS: frozenset[str] = frozenset({"success", "failure", "blocked"})

# 40-char lowercase hex (git SHA-1). R73 §5.7 spec says "40-hex".
_SOURCE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# sha256 digest format: "sha256:" + 64 lowercase hex
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now_iso() -> str:
    """Current UTC time as RFC3339/ISO8601 string."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def canonical_payload_digest(payload: dict) -> str:
    """R73 §5.7: Compute sha256 digest of canonical JSON of payload.

    Canonical JSON = sorted keys + compact separators (no whitespace),
    ensuring byte-stable reproducibility across implementations.

    Args:
        payload: arbitrary JSON-serializable dict.

    Returns:
        ``"sha256:<64-hex>"`` digest string.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_evidence_envelope(
    gate_level: str,
    event: str,
    ref: str,
    source_sha: str,
    run_id: int,
    run_attempt: int,
    workflow_path: str = ".github/workflows/release-gates.yml",
    overall_conclusion: str = "success",
    payload: dict | None = None,
    image_repo_digest: str | None = None,
    runtime_config_digest: str | None = None,
    promotion_eligible: bool | None = None,
) -> dict:
    """R73 §5.7: Build a typed evidence envelope.

    Computes ``payload_digest`` (canonical sha256 of payload) and
    ``generated_at`` (RFC3339) automatically. The ``promotion_eligible``
    flag is set per R73 §5.7 rules:

        - True only when gate_level="rc" AND overall_conclusion="success"
        - False otherwise (master runs, failures, blocked, etc.)

    Failed/cancelled/skipped runs NEVER produce promotable evidence —
    callers should pass overall_conclusion="failure" or "blocked" for
    those cases, which forces promotion_eligible=False.

    R73 P1-05: Callers MAY pass ``promotion_eligible`` explicitly to
    override the auto-computed value (e.g. workflow gate_level step
    decides eligibility based on ``needs.*.result``). When None, the
    value is auto-computed from gate_level + overall_conclusion.

    Args:
        gate_level: "development" | "rc" | "production"
        event: "push" | "workflow_dispatch" | "pull_request"
        ref: git ref (e.g. refs/heads/master, refs/tags/rc-v...)
        source_sha: 40-hex git commit SHA
        run_id: GitHub Actions run_id (non-negative int)
        run_attempt: run attempt number (>= 1)
        workflow_path: workflow file path
            (e.g. .github/workflows/release-gates.yml)
        overall_conclusion: "success" | "failure" | "blocked"
        payload: the evidence payload dict (envelope wraps this).
            May be None — defaults to empty dict.
        image_repo_digest: OCI image repo digest
            (e.g. ghcr.io/owner/repo@sha256:...). May be None or
            empty string (normalized to None) for non-promotable
            envelopes (e.g. development runs without a published image).
        runtime_config_digest: runtime config digest (sha256:...).
            May be None or empty string (normalized to None) for
            non-promotable envelopes.
        promotion_eligible: explicit override for the eligibility flag.
            When None, auto-computed as
            ``gate_level == "rc" and overall_conclusion == "success"``.

    Returns:
        Envelope dict with all R73 §5.7 required fields.
    """
    # Normalize empty-string digests to None (callers may pass "" from
    # CLI args / workflow outputs when no digest is available).
    if image_repo_digest is not None and not image_repo_digest:
        image_repo_digest = None
    if runtime_config_digest is not None and not runtime_config_digest:
        runtime_config_digest = None

    if payload is None:
        payload = {}

    payload_digest = canonical_payload_digest(payload)
    if promotion_eligible is None:
        promotion_eligible = (
            gate_level == "rc" and overall_conclusion == "success"
        )
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "gate_level": gate_level,
        "event": event,
        "ref": ref,
        "source_sha": source_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_path": workflow_path,
        "overall_conclusion": overall_conclusion,
        "promotion_eligible": promotion_eligible,
        "image_repo_digest": image_repo_digest,
        "runtime_config_digest": runtime_config_digest,
        "generated_at": _now_iso(),
        "payload_digest": payload_digest,
    }


def validate_envelope(envelope: dict) -> tuple[bool, list[str]]:
    """R73 §5.7: Validate a typed evidence envelope.

    Checks:
        - All required fields present
        - Types correct (str / int / bool)
        - gate_level in {development, rc, production}
        - overall_conclusion in {success, failure, blocked}
        - source_sha matches 40-hex pattern
        - run_id >= 0, run_attempt >= 1
        - payload_digest matches ``sha256:<64-hex>`` format

    ``image_repo_digest`` and ``runtime_config_digest`` may be None
    (for non-promotable envelopes) or a non-empty str. Use
    ``is_promotion_eligible`` for the stricter audit that rejects
    missing digests.

    Args:
        envelope: envelope dict to validate.

    Returns:
        ``(valid, errors)`` — valid is True only if errors is empty.
    """
    errors: list[str] = []
    if not isinstance(envelope, dict):
        return False, [
            f"envelope must be a dict, got {type(envelope).__name__}"
        ]

    # 1. All required fields present
    for field in REQUIRED_ENVELOPE_FIELDS:
        if field not in envelope:
            errors.append(f"missing required field: {field}")
    if errors:
        return False, errors

    # 2. schema_version
    sv = envelope["schema_version"]
    if not isinstance(sv, str):
        errors.append(
            f"schema_version must be str, got {type(sv).__name__}"
        )
    elif sv != ENVELOPE_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {ENVELOPE_SCHEMA_VERSION!r}, "
            f"got {sv!r}"
        )

    # 3. gate_level enum
    gl = envelope["gate_level"]
    if not isinstance(gl, str):
        errors.append(
            f"gate_level must be str, got {type(gl).__name__}"
        )
    elif gl not in VALID_GATE_LEVELS:
        errors.append(
            f"gate_level must be one of {sorted(VALID_GATE_LEVELS)}, "
            f"got {gl!r}"
        )

    # 4. event / ref / workflow_path
    for field in ("event", "ref", "workflow_path"):
        val = envelope[field]
        if not isinstance(val, str):
            errors.append(
                f"{field} must be str, got {type(val).__name__}"
            )

    # 5. source_sha (40-hex)
    sha = envelope["source_sha"]
    if not isinstance(sha, str):
        errors.append(
            f"source_sha must be str, got {type(sha).__name__}"
        )
    elif not _SOURCE_SHA_PATTERN.fullmatch(sha):
        errors.append(
            f"source_sha must be 40-hex (lowercase), got {sha!r}"
        )

    # 6. run_id (int >= 0), exclude bool
    rid = envelope["run_id"]
    if isinstance(rid, bool) or not isinstance(rid, int):
        errors.append(
            f"run_id must be int, got {type(rid).__name__}"
        )
    elif rid < 0:
        errors.append(f"run_id must be >= 0, got {rid}")

    # 7. run_attempt (int >= 1), exclude bool
    ra = envelope["run_attempt"]
    if isinstance(ra, bool) or not isinstance(ra, int):
        errors.append(
            f"run_attempt must be int, got {type(ra).__name__}"
        )
    elif ra < 1:
        errors.append(f"run_attempt must be >= 1, got {ra}")

    # 8. overall_conclusion enum
    oc = envelope["overall_conclusion"]
    if not isinstance(oc, str):
        errors.append(
            f"overall_conclusion must be str, got {type(oc).__name__}"
        )
    elif oc not in VALID_CONCLUSIONS:
        errors.append(
            f"overall_conclusion must be one of "
            f"{sorted(VALID_CONCLUSIONS)}, got {oc!r}"
        )

    # 9. promotion_eligible (bool)
    pe = envelope["promotion_eligible"]
    if not isinstance(pe, bool):
        errors.append(
            f"promotion_eligible must be bool, "
            f"got {type(pe).__name__}"
        )

    # 10. image_repo_digest / runtime_config_digest
    #     May be None (non-promotable) or non-empty str.
    for digest_field in ("image_repo_digest", "runtime_config_digest"):
        val = envelope[digest_field]
        if val is None:
            continue
        if not isinstance(val, str):
            errors.append(
                f"{digest_field} must be str or None, "
                f"got {type(val).__name__}"
            )
        elif not val:
            errors.append(
                f"{digest_field} must be non-empty str or None, "
                f"got empty str"
            )

    # 11. generated_at (str)
    if not isinstance(envelope["generated_at"], str):
        errors.append(
            f"generated_at must be str, "
            f"got {type(envelope['generated_at']).__name__}"
        )

    # 12. payload_digest (sha256:<64-hex>)
    pd = envelope["payload_digest"]
    if not isinstance(pd, str):
        errors.append(
            f"payload_digest must be str, got {type(pd).__name__}"
        )
    elif not _SHA256_DIGEST_PATTERN.fullmatch(pd):
        errors.append(
            f"payload_digest must be 'sha256:<64-hex>', got {pd!r}"
        )

    # 13. R73 P1-05: Context-aware tiered validation (defense in depth).
    #     Cross-check gate_level / promotion_eligible against event + ref
    #     so a malformed envelope cannot claim eligibility it is not
    #     entitled to. These rules mirror the workflow gate_level step.
    _validate_tiered_invariants(envelope, errors)

    return (len(errors) == 0), errors


def _validate_tiered_invariants(envelope: dict, errors: list[str]) -> None:
    """R73 P1-05: Enforce tiered gate_level / promotion_eligible rules.

    The rules (mirroring ``release-gates.yml`` gate_level step):
        - master run (push + refs/heads/master):
            * gate_level MUST be "development"
            * promotion_eligible MUST be False
        - RC run (push + refs/tags/rc-v*):
            * gate_level MUST be "rc"
            * If overall_conclusion != "success" → promotion_eligible
              MUST be False (failed/cancelled/skipped runs never
              produce promotable evidence)
        - production run (push + refs/tags/production-v*):
            * gate_level MUST be "production"
            * promotion_eligible MUST be False (production evidence is
              a deployment record, not a promotion candidate)

    Non-push events (workflow_dispatch / pull_request) are not tiered
    by these invariants — they fall back to the permissive default
    (any gate_level allowed, eligibility audited separately by
    ``is_promotion_eligible``).

    This is defense in depth: even if a caller crafts an envelope with
    a wrong gate_level, ``validate_envelope`` rejects it. The consumer-
    side ``is_promotion_eligible`` adds the authoritative audit.
    """
    event = envelope.get("event")
    ref = envelope.get("ref")
    gate_level = envelope.get("gate_level")
    oc = envelope.get("overall_conclusion")
    pe = envelope.get("promotion_eligible")

    if not isinstance(event, str) or not isinstance(ref, str):
        return  # type errors already reported above

    if event != "push":
        return  # only push events are tiered

    # master run → development, never promotable
    if ref == "refs/heads/master" or ref == "refs/heads/main":
        if gate_level != "development":
            errors.append(
                f"master run (ref={ref!r}) must have "
                f"gate_level='development', got {gate_level!r}"
            )
        if pe is not False:
            errors.append(
                f"master run (ref={ref!r}) must have "
                f"promotion_eligible=False, got {pe!r}"
            )
        return

    # RC run → rc, failed RC never promotable
    if ref.startswith("refs/tags/rc-v"):
        if gate_level != "rc":
            errors.append(
                f"RC run (ref={ref!r}) must have gate_level='rc', "
                f"got {gate_level!r}"
            )
        if oc != "success" and pe is not False:
            errors.append(
                f"failed RC run (overall_conclusion={oc!r}) must have "
                f"promotion_eligible=False, got {pe!r}"
            )
        return

    # production run → production, never promotable (deployment record)
    if ref.startswith("refs/tags/production-v"):
        if gate_level != "production":
            errors.append(
                f"production run (ref={ref!r}) must have "
                f"gate_level='production', got {gate_level!r}"
            )
        if pe is not False:
            errors.append(
                f"production run (ref={ref!r}) must have "
                f"promotion_eligible=False, got {pe!r}"
            )
        return


def is_promotion_eligible(envelope: dict) -> bool:
    """R73 §5.7 / P1-05: Authoritative promotion-eligibility audit.

    Returns True ONLY when ALL of:
        - validate_envelope passes (all required fields + tiered
          invariants)
        - gate_level == "rc"
        - overall_conclusion == "success"
        - envelope's own promotion_eligible == True
          (defense in depth: an envelope that claims False is not
          promotable even if other fields look valid)
        - event == "push" (only push-triggered RC runs are promotable;
          workflow_dispatch / pull_request are not)
        - ref starts with "refs/tags/rc-v" (only RC tag pushes are
          promotable; master push / production tag / other refs are not)
        - No missing digest (image_repo_digest and
          runtime_config_digest are non-empty strings)

    This is the consumer-side audit. It does NOT blindly trust the
    envelope's own ``promotion_eligible`` field — it cross-checks the
    underlying gate_level / conclusion / event / ref / digests. A
    malformed envelope that claims ``promotion_eligible=True`` but is
    missing a digest, or was triggered by a non-push event, or points
    at a non-RC-tag ref, will be rejected.

    Args:
        envelope: envelope dict to audit.

    Returns:
        True if the envelope is promotion-eligible.
    """
    valid, _errors = validate_envelope(envelope)
    if not valid:
        return False
    if envelope["gate_level"] != "rc":
        return False
    if envelope["overall_conclusion"] != "success":
        return False
    # R73 P1-05: envelope's own flag must also be True (defense in
    # depth — a caller that built the envelope with promotion_eligible
    # override = False is not promotable, regardless of other fields).
    if envelope.get("promotion_eligible") is not True:
        return False
    # R73 P1-05: only push-triggered runs are promotable
    if envelope.get("event") != "push":
        return False
    # R73 P1-05: only RC tag pushes are promotable
    ref = envelope.get("ref")
    if not isinstance(ref, str) or not ref.startswith("refs/tags/rc-v"):
        return False
    # No missing digest — both must be non-empty strings
    for digest_field in ("image_repo_digest", "runtime_config_digest"):
        val = envelope.get(digest_field)
        if not isinstance(val, str) or not val:
            return False
    return True


def envelope_to_file(envelope: dict, path: Path | str) -> None:
    """R73 §5.7: Write envelope to file as canonical JSON.

    Canonical JSON = sorted keys + compact separators (no whitespace),
    ensuring byte-stable reproducibility for audit/digest verification.

    Args:
        envelope: envelope dict.
        path: output file path. Parent directories are created.
    """
    p = Path(path)
    if p.parent:
        p.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    p.write_text(canonical, encoding="utf-8")


def load_envelope(path: Path | str) -> dict:
    """R73 §5.7: Load envelope from file.

    Args:
        path: envelope file path (JSON).

    Returns:
        Envelope dict.

    Raises:
        FileNotFoundError: file does not exist.
        json.JSONDecodeError: file is not valid JSON.
        ValueError: root JSON value is not a dict.
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"envelope file must contain a JSON object, "
            f"got {type(data).__name__}"
        )
    return data


# ════════════════════════════════════════════════════════════════
# R73 P1-05: CLI entry — supports `build` and `validate` subcommands
# for use in release-gates.yml workflow (generate-evidence-envelope
# job) and _promote-verified-rc.yml promotion verification.
# ════════════════════════════════════════════════════════════════


def _str_to_bool(value: str) -> bool:
    """Parse a CLI bool argument (case-insensitive)."""
    return value.strip().lower() in ("true", "1", "yes", "y", "on")


def _cli_build(args: argparse.Namespace) -> int:
    """CLI: build evidence envelope and write to --output path."""
    # Coerce run_id / run_attempt to int (CLI args come as strings).
    try:
        run_id = int(args.run_id)
    except (TypeError, ValueError):
        print(
            f"::error::--run-id must be an integer, got {args.run_id!r}",
            file=sys.stderr,
        )
        return 2
    try:
        run_attempt = int(args.run_attempt)
    except (TypeError, ValueError):
        print(
            f"::error::--run-attempt must be an integer, "
            f"got {args.run_attempt!r}",
            file=sys.stderr,
        )
        return 2

    promotion_eligible: bool | None
    if args.promotion_eligible is None:
        promotion_eligible = None
    else:
        promotion_eligible = _str_to_bool(args.promotion_eligible)

    envelope = build_evidence_envelope(
        gate_level=args.gate_level,
        event=args.event,
        ref=args.ref,
        source_sha=args.source_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        workflow_path=args.workflow_path,
        overall_conclusion=args.overall_conclusion,
        payload={},  # CLI build wraps an empty payload (digest computed)
        image_repo_digest=args.image_repo_digest or None,
        runtime_config_digest=args.runtime_config_digest or None,
        promotion_eligible=promotion_eligible,
    )

    valid, errors = validate_envelope(envelope)
    if not valid:
        print(
            "::error::built envelope failed self-validation:",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    envelope_to_file(envelope, output_path)
    print(f"PASS: evidence envelope written to {output_path}")
    print(
        f"  gate_level={envelope['gate_level']} "
        f"promotion_eligible={envelope['promotion_eligible']} "
        f"overall_conclusion={envelope['overall_conclusion']}"
    )
    return 0


def _cli_validate(args: argparse.Namespace) -> int:
    """CLI: validate an evidence envelope file."""
    path = Path(args.envelope_path)
    try:
        envelope = load_envelope(path)
    except FileNotFoundError:
        print(f"::error::envelope file not found: {path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(
            f"::error::envelope file is not valid JSON: {e}",
            file=sys.stderr,
        )
        return 2
    except ValueError as e:
        print(f"::error::envelope file invalid: {e}", file=sys.stderr)
        return 2

    valid, errors = validate_envelope(envelope)
    if not valid:
        print(
            f"::error::FAIL: envelope validation failed ({path}):",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    eligible = is_promotion_eligible(envelope)
    print(f"PASS: envelope valid ({path})")
    print(
        f"  gate_level={envelope['gate_level']} "
        f"promotion_eligible={envelope['promotion_eligible']} "
        f"overall_conclusion={envelope['overall_conclusion']} "
        f"audit_promotion_eligible={eligible}"
    )
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evidence_envelope",
        description=(
            "R73 §5.7 / P1-05: typed evidence envelope builder & "
            "validator. Subcommands: build, validate."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # build subcommand
    p_build = sub.add_parser(
        "build",
        help="Build a typed evidence envelope and write to --output.",
    )
    p_build.add_argument(
        "--gate-level", required=True,
        choices=["development", "rc", "production"],
        help="Gate level tier (R73 P1-05).",
    )
    p_build.add_argument(
        "--promotion-eligible", default=None,
        help="Override promotion_eligible flag (true/false). "
             "If omitted, auto-computed from gate_level + conclusion.",
    )
    p_build.add_argument(
        "--source-sha", required=True,
        help="40-char hex git commit SHA.",
    )
    p_build.add_argument(
        "--run-id", required=True,
        help="GitHub Actions run_id (integer).",
    )
    p_build.add_argument(
        "--run-attempt", required=True,
        help="GitHub Actions run_attempt (integer >= 1).",
    )
    p_build.add_argument(
        "--workflow-path", default=".github/workflows/release-gates.yml",
        help="Workflow file path.",
    )
    p_build.add_argument(
        "--event", required=True,
        help="Triggering event (push / workflow_dispatch / pull_request).",
    )
    p_build.add_argument(
        "--ref", required=True,
        help="Git ref (e.g. refs/heads/master, refs/tags/rc-v...).",
    )
    p_build.add_argument(
        "--overall-conclusion", default="success",
        choices=["success", "failure", "blocked"],
        help="Overall conclusion of the workflow run.",
    )
    p_build.add_argument(
        "--image-repo-digest", default="",
        help="OCI image RepoDigest (ghcr.io/...@sha256:...). "
             "Empty for non-promotable envelopes.",
    )
    p_build.add_argument(
        "--runtime-config-digest", default="",
        help="Runtime config digest (sha256:...). "
             "Empty for non-promotable envelopes.",
    )
    p_build.add_argument(
        "--output", required=True,
        help="Output envelope JSON path.",
    )

    # validate subcommand
    p_val = sub.add_parser(
        "validate",
        help="Validate an evidence envelope JSON file.",
    )
    p_val.add_argument(
        "envelope_path",
        help="Path to envelope JSON file.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        return _cli_build(args)
    if args.command == "validate":
        return _cli_validate(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
