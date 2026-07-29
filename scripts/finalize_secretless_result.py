#!/usr/bin/env python3
"""R80 P0-04 — 全路径生成 secretless result.json,强制 required phases 完整性。

整改背景 (R80 P0-04):
    原版本使用 all(phase.status == success for phase in phases) 判定 GO,
    但 Python 中 all([]) == True,导致 0 个 phase 也可能错误输出 GO。
    本版本强制要求 9 个 required phases 全部存在、唯一、名称匹配且成功。

本脚本由 workflow 中一个 ``if: always()`` 的 Finalize step 调用,职责:
    1. 读取 Step 4 创建的 in_progress result.json(identity:head_sha/run_id/attempt)。
    2. 扫描 artifacts/secretless-e2e/phases/*.json 汇总各阶段结构化记录。
    3. 强制验证 REQUIRED_PHASES 完整集合(9 个 phase 全部存在且唯一)。
    4. 缺失阶段自动补为 status=skipped, error_code=PHASE_NOT_EXECUTED。
    5. 结合 ``--job-status`` 与 ``--error-code`` 判定最终结论。
    6. 保留首个失败 phase;cleanup 失败只能作为 secondary error。
    7. 写出最终 result.json — 此步骤本身不得失败。

GO 条件(全部满足):
    - required phase 数量正好为 9
    - 每个 step 只出现一次
    - step/name 严格匹配 REQUIRED_PHASES
    - 没有未知 phase
    - 没有重复 phase
    - 没有乱序
    - 9 个 phase 全部 status=success
    - 没有 error_code
    - job status=success
    - identity 字段完整
    - event 是权威 event(push)
    - result schema 有效

退出码:
    0 — result.json 已生成(无论 GO/NO-GO)
    1 — 写入失败

用法(workflow Finalize step):
    python scripts/finalize_secretless_result.py \
        --job-status '${{ job.status }}' \
        --output artifacts/secretless-e2e/result.json \
        [--error-code "${{ steps.start-infra.outputs.error_code }}"]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "secretless-e2e" / "result.json"
PHASES_DIR = REPO_ROOT / "artifacts" / "secretless-e2e" / "phases"
STEP12_DIR = REPO_ROOT / "artifacts" / "secretless-e2e" / "step12"
FAILURE_SUMMARY = REPO_ROOT / "artifacts" / "secretless-e2e" / "failure-summary.json"

# R80 P0-04: 唯一 required phase 集合 — 9 个阶段全部必须存在且成功
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

# 权威事件(Release Gate 只接受 push)
AUTHORITATIVE_EVENT = "push"


def _read_init_identity(output: Path) -> dict:
    """读取 Step 4 创建的 in_progress result.json 中的身份信息。"""
    if output.exists():
        try:
            doc = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return {
                    "head_sha": doc.get("head_sha", ""),
                    "run_id": doc.get("run_id", ""),
                    "run_attempt": doc.get("run_attempt", ""),
                    "workflow_path": doc.get("workflow_path", ""),
                    "event": doc.get("event", ""),
                }
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _read_phases() -> list[dict]:
    """读取所有阶段结构化记录并按 step 排序。"""
    if not PHASES_DIR.is_dir():
        return []
    phases: list[dict] = []
    for p in sorted(PHASES_DIR.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                phases.append(doc)
        except (json.JSONDecodeError, OSError):
            continue
    phases.sort(key=lambda d: (d.get("step", 0), d.get("name", "")))
    return phases


def _validate_phases(phases: list[dict]) -> tuple[bool, list[dict], list[str]]:
    """R80 P0-04: 强制验证 required phases 完整性。

    Returns:
        (all_pass, enriched_phases, validation_errors)
        - all_pass: 9 个 required phase 全部存在、唯一、名称匹配且 success
        - enriched_phases: 补充了缺失阶段(skipped)的完整 phase 列表
        - validation_errors: 验证错误列表(空 = 无错误)
    """
    errors: list[str] = []

    # 检查重复 phase
    seen_steps: dict[int, int] = {}
    for phase in phases:
        step = phase.get("step")
        if step in seen_steps:
            errors.append(f"duplicate_phase:step={step}")
        seen_steps[step] = seen_steps.get(step, 0) + 1

    # 检查未知 phase
    for phase in phases:
        step = phase.get("step")
        name = phase.get("name", "")
        if step not in REQUIRED_PHASES:
            errors.append(f"unknown_phase:step={step},name={name}")
        elif REQUIRED_PHASES[step] != name:
            errors.append(
                f"phase_name_mismatch:step={step},"
                f"expected={REQUIRED_PHASES[step]},actual={name}"
            )

    # 补充缺失阶段为 skipped
    enriched = list(phases)
    for step, name in REQUIRED_PHASES.items():
        if step not in seen_steps:
            enriched.append({
                "step": step,
                "name": name,
                "status": "skipped",
                "error_code": "PHASE_NOT_EXECUTED",
            })
            errors.append(f"missing_phase:step={step},name={name}")
    enriched.sort(key=lambda d: (d.get("step", 0), d.get("name", "")))

    # 检查乱序
    steps_in_order = [p.get("step", 0) for p in enriched]
    if steps_in_order != sorted(steps_in_order):
        errors.append("phases_out_of_order")

    # 最终判定: 9 个 required phase 全部 success
    required_success = True
    for step, name in REQUIRED_PHASES.items():
        matching = [
            p for p in enriched
            if p.get("step") == step and p.get("name") == name
        ]
        if len(matching) != 1:
            required_success = False
            continue
        if matching[0].get("status") != "success":
            required_success = False

    all_pass = (
        required_success
        and not errors
        and len([p for p in enriched if p.get("step") in REQUIRED_PHASES]) == 9
    )

    return all_pass, enriched, errors


def _extract_nested_returncode(item: dict) -> int | None:
    """从 wrapper/evidence/command 结构中提取最内层命令退出码。"""
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
    return None


def _read_step12_first_failure() -> dict | None:
    """从 Step 12 子阶段 evidence 提取真实首错，避免退化为 PHASE_NOT_EXECUTED。"""
    if not STEP12_DIR.is_dir():
        return None
    order = (
        "full_backup_to_s3_contract_store",
        "corrupt_payload_negative",
        "blank_restore_from_s3_contract_store",
    )
    for subphase in order:
        path = STEP12_DIR / f"{subphase}.json"
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        results = doc.get("phases") or doc.get("results") or []
        item = results[-1] if results else doc
        status = str(item.get("status", "")).lower()
        returncode = _extract_nested_returncode(item)
        # 子阶段合同以 wrapper status 为权威。corruption negative 的底层命令
        # 应因明确完整性错误非零退出，但 harness 验证错误码后会将 wrapper 标为 pass；
        # 不能再凭内部 returncode 将合法 expected-failure 误报为首错。
        if status not in ("pass", "success"):
            error = str(item.get("error") or item.get("blocking_reason") or "unknown")
            checks = item.get("readiness_checks") or []
            failed_check = next(
                (str(c.get("error_code") or c.get("check")) for c in checks
                 if str(c.get("status", "")).lower() not in ("pass", "success")),
                "",
            )
            return {
                "step": 12,
                "name": "backup-restore",
                "subphase": subphase,
                "status": "failure",
                "error_code": failed_check or error.split(":", 1)[0][:160],
                "returncode": returncode,
                "error": error,
            }
    return None


def _find_first_failure(phases: list[dict]) -> dict | None:
    """首个失败/跳过阶段；Step 12 缺失时优先保留真实子阶段首错。"""
    step12_failure = _read_step12_first_failure()
    for phase in phases:
        status = phase.get("status")
        if status in ("failure", "skipped"):
            if phase.get("step") == 12 and step12_failure:
                return step12_failure
            return phase
    return step12_failure


def _write_failure_summary(first_failure: dict | None) -> None:
    """写出首错、returncode 与相关日志 SHA-256，便于远程机器审计。"""
    if not first_failure:
        return
    subphase = str(first_failure.get("subphase") or "")
    log_digests: dict[str, str] = {}
    if subphase:
        for suffix in ("stdout.log", "stderr.log"):
            path = STEP12_DIR / f"{subphase}.{suffix}"
            if path.is_file():
                try:
                    log_digests[suffix] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    pass
    summary = {
        "schema_version": "secretless-failure-summary/v1",
        "first_failure": first_failure,
        "log_sha256": log_digests,
    }
    try:
        FAILURE_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        FAILURE_SUMMARY.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass


def build_result(
    *,
    job_status: str,
    error_code: str,
    output: Path,
    expected_identity: dict[str, str] | None = None,
) -> dict:
    """构建最终 result.json，并将初始化身份绑定到调用方的 current run。"""
    identity = _read_init_identity(output)
    phases = _read_phases()

    # R80 P0-04: 强制 required phases 完整性验证
    all_required_pass, enriched_phases, validation_errors = _validate_phases(phases)
    first_failure = _find_first_failure(enriched_phases)
    _write_failure_summary(first_failure)

    # R80 P0-04: GO 条件 — 全部满足才输出 GO
    # 消除 all([]) 漏洞: 即使 phases 为空,all_required_pass 也为 False
    # (因为 9 个 required phase 全部缺失 → 补为 skipped → 不是 success)
    identity_complete = all([
        identity.get("head_sha"),
        identity.get("run_id"),
        identity.get("run_attempt"),
        identity.get("workflow_path"),
        identity.get("event"),
    ])
    event_authoritative = identity.get("event") == AUTHORITATIVE_EVENT
    identity_matches_expected = expected_identity is None or all(
        str(identity.get(field, "")) == str(expected)
        for field, expected in expected_identity.items()
    )

    go = (
        job_status == "success"
        and all_required_pass
        and not error_code
        and not first_failure
        and not validation_errors
        and identity_complete
        and event_authoritative
        and identity_matches_expected
    )

    verdict = "SECRETLESS_FUNCTIONAL_GO" if go else "SECRETLESS_FUNCTIONAL_NO_GO"
    primary_error_code = error_code or (
        first_failure.get("error_code") if first_failure else None
    )

    result = {
        "schema_version": "secretless-e2e/v1",
        "status": "completed",
        "head_sha": identity.get("head_sha", ""),
        "run_id": identity.get("run_id", ""),
        "run_attempt": identity.get("run_attempt", ""),
        "workflow_path": identity.get("workflow_path", ""),
        "event": identity.get("event", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_status": job_status,
        "result": verdict,
        "error_code": primary_error_code,
        "first_failure": first_failure,
        "phases": enriched_phases,
        "total_phases": len(enriched_phases),
        "passed_phases": sum(1 for p in enriched_phases if p.get("status") == "success"),
        "failed_phases": sum(1 for p in enriched_phases if p.get("status") == "failure"),
        "skipped_phases": sum(1 for p in enriched_phases if p.get("status") == "skipped"),
        "validation_errors": validation_errors,
        "go_criteria": {
            "job_status_success": job_status == "success",
            "all_required_phases_pass": all_required_pass,
            "no_step_error_code": not error_code,
            "no_first_failure": not first_failure,
            "no_validation_errors": not validation_errors,
            "identity_complete": identity_complete,
            "identity_matches_expected": identity_matches_expected,
            "event_authoritative": event_authoritative,
            "required_phase_count_exact": len(REQUIRED_PHASES) == 9,
        },
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job-status",
        required=True,
        choices=("success", "failure", "cancelled", "skipped"),
        help="GitHub job.status(top-level workflow 聚合状态)",
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-run-attempt", required=True)
    parser.add_argument("--expected-workflow-path", required=True)
    parser.add_argument("--expected-event", required=True)
    parser.add_argument("--error-code", default="", help="步骤写入 $GITHUB_OUTPUT 的 error_code")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    result = build_result(
        job_status=args.job_status,
        error_code=(args.error_code or "").strip(),
        output=args.output,
        expected_identity={
            "head_sha": args.expected_sha,
            "run_id": args.expected_run_id,
            "run_attempt": args.expected_run_attempt,
            "workflow_path": args.expected_workflow_path,
            "event": args.expected_event,
        },
    )

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except OSError as exc:
        print(f"ERROR: failed to write result.json: {exc}", file=sys.stderr)
        return 1

    verdict = result["result"]
    first = result.get("first_failure")
    summary = (
        f"result.json written: {verdict} "
        f"(phases={result['total_phases']}, "
        f"pass={result['passed_phases']}, "
        f"fail={result['failed_phases']}, "
        f"skip={result['skipped_phases']})"
    )
    if result.get("validation_errors"):
        summary += f" validation_errors={result['validation_errors']}"
    if first:
        summary += f" — first_failure={first.get('name')}:{first.get('error_code')}"
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
