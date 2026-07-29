#!/usr/bin/env python3
"""将 Secretless phase evidence 安全转换为结构化 GitHub annotation。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:secret|password|token|access[_-]?key|signing[_-]?key|kek)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+(@)"),
)
_SENSITIVE_FLAGS = {
    "--access-key",
    "--secret-key",
    "--signing-key",
    "--token",
    "--password",
    "--kek",
}


def redact_text(value: str) -> str:
    """脱敏日志中的凭证形态，不改变普通诊断文本。"""
    result = value
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1***REDACTED***\2" if pattern.groups >= 2 else r"\1***REDACTED***"
        result = pattern.sub(replacement, result)
    return result


def redact_argv(argv: object) -> list[str]:
    if not isinstance(argv, list):
        return []
    output: list[str] = []
    redact_next = False
    for raw in argv:
        item = str(raw)
        if redact_next:
            output.append("***REDACTED***")
            redact_next = False
            continue
        name, separator, _value = item.partition("=")
        if name.lower() in _SENSITIVE_FLAGS:
            if separator:
                output.append(f"{name}=***REDACTED***")
            else:
                output.append(name)
                redact_next = True
            continue
        output.append(redact_text(item))
    return output


def extract_returncode(item: dict[str, Any], wrapper_returncode: int | None) -> int | None:
    paths = (
        ("evidence", "command", "returncode"),
        ("evidence", "command_returncode"),
        ("evidence", "validate_returncode"),
        ("evidence", "restore_returncode"),
        ("command", "returncode"),
        ("returncode",),
    )
    for path in paths:
        value: object = item
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return wrapper_returncode


def tail_text(value: object, max_chars: int) -> str:
    text = redact_text(str(value or ""))
    if len(text) <= max_chars:
        return text
    return f"...[truncated {len(text) - max_chars} chars]...{text[-max_chars:]}"


def escape_annotation(value: object) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def build_report(
    evidence: Path,
    *,
    phase: str,
    wrapper_returncode: int | None,
    stdout_log: Path | None,
    stderr_log: Path | None,
    max_chars: int,
) -> dict[str, Any]:
    try:
        raw = evidence.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read evidence: {exc}") from exc
    if not raw.strip():
        raise ValueError("evidence is empty")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"evidence JSON is invalid: {exc}") from exc
    if not isinstance(doc, dict):
        raise TypeError("evidence root must be a JSON object")
    results = doc.get("phases") or doc.get("results") or []
    if results and not isinstance(results, list):
        raise ValueError("phases/results must be an array")
    item = results[-1] if results else doc
    if not isinstance(item, dict):
        raise TypeError("selected phase evidence must be a JSON object")

    command = item.get("evidence", {}).get("command", {}) if isinstance(item.get("evidence"), dict) else {}
    argv = command.get("argv", item.get("argv", [])) if isinstance(command, dict) else item.get("argv", [])
    checks = item.get("readiness_checks") or []
    failed_check = next(
        (
            str(check.get("error_code") or check.get("check") or "unknown")
            for check in checks
            if isinstance(check, dict)
            and str(check.get("status", "")).lower() not in ("pass", "success")
        ),
        "",
    )
    error = redact_text(str(item.get("error") or item.get("blocking_reason") or "unknown"))

    logs: dict[str, str] = {}
    for name, path in (("stdout", stdout_log), ("stderr", stderr_log)):
        if path is None:
            logs[name] = tail_text(item.get(name, ""), max_chars)
            continue
        try:
            logs[name] = tail_text(path.read_text(encoding="utf-8", errors="replace"), max_chars)
        except OSError as exc:
            logs[name] = f"log unavailable: {type(exc).__name__}"

    return {
        "schema_version": "secretless-phase-failure/v1",
        "phase": phase,
        "status": str(item.get("status", "unknown")),
        "error": error,
        "error_code": failed_check or error.split(":", 1)[0][:160],
        "returncode": extract_returncode(item, wrapper_returncode),
        "argv": redact_argv(argv),
        "failed_readiness_check": failed_check,
        "readiness_checks": checks,
        "logs": logs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--wrapper-returncode", type=int)
    parser.add_argument("--stdout-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--max-chars", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.max_chars < 256:
        parser.error("--max-chars must be at least 256")

    try:
        report = build_report(
            args.evidence,
            phase=args.phase,
            wrapper_returncode=args.wrapper_returncode,
            stdout_log=args.stdout_log,
            stderr_log=args.stderr_log,
            max_chars=args.max_chars,
        )
    except (TypeError, ValueError) as exc:
        report = {
            "schema_version": "secretless-phase-failure/v1",
            "phase": args.phase,
            "status": "evidence-invalid",
            "error": redact_text(str(exc)),
            "error_code": "PHASE_EVIDENCE_INVALID",
            "returncode": args.wrapper_returncode,
            "argv": [],
            "failed_readiness_check": "",
            "readiness_checks": [],
            "logs": {},
        }

    title = escape_annotation(f"Secretless Step 12/{args.phase}")
    message = escape_annotation(
        f"error_code={report['error_code']} returncode={report['returncode']} error={report['error']}"
    )
    print(f"::error title={title}::{message}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "evidence-invalid" else 2


if __name__ == "__main__":
    sys.exit(main())
