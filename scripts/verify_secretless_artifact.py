#!/usr/bin/env python3
"""R80 P0-05 — 核对 secretless artifact 身份绑定与 GO 判定(fail-closed)。

由 release-gates.yml closed-loop gate 调用。

R80 P0-05 整改:
    原版本在 result.json 缺失时只输出 warning 并 return 0(放行),
    这直接绕过身份绑定、阶段完整性和 GO 判定,违反 fail-closed。
    本版本: 缺失、空文件、非法 JSON、schema 错误、identity 不匹配、
    phase 不完整、result 不是精确 GO,全部 exit 1。

身份绑定校验:
  - artifact result.json 的 head_sha / run_id / attempt 必须与 GitHub API
    当前运行完全一致(拒绝 PR merge SHA / 旧 SHA / 仅摘要绿)。
  - result 必须为 SECRETLESS_FUNCTIONAL_GO。
  - 9 个 required phases 全部存在且 success。
  - event 必须是权威事件(push)。
  - schema_version 必须有效。

环境变量(由 workflow 传入):
  SL_ARTIFACT_DIR   artifact 解压目录
  SL_SHA            期望的 head_sha(= $COMMIT_SHA)
  SL_RUN_ID         期望的 run_id
  SL_ATTEMPT        期望的 run_attempt

退出码:
  0 — 验证通过
  1 — 任何验证失败(fail-closed)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from verify_secretless_final_result import verify_result

# R80: 与 finalize_secretless_result.py 保持一致的 required phases
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

AUTHORITATIVE_EVENT = "push"
VALID_SCHEMA_VERSIONS = ("secretless-e2e/v1",)
WORKFLOW_PATH = ".github/workflows/secretless-contract-e2e.yml"
_ARTIFACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def main() -> int:
    art_dir = Path(os.environ.get("SL_ARTIFACT_DIR", "/tmp/sl-artifact"))
    expected_sha = (os.environ.get("SL_SHA") or "").strip()
    expected_run_id = (os.environ.get("SL_RUN_ID") or "").strip()
    expected_attempt = (os.environ.get("SL_ATTEMPT") or "").strip()
    expected_artifact_id = (os.environ.get("SL_ARTIFACT_ID") or "").strip()
    expected_artifact_digest = (os.environ.get("SL_ARTIFACT_DIGEST") or "").strip().lower()

    if not expected_artifact_id.isdigit():
        print("::error::SECRETLESS_ARTIFACT_ID_INVALID")
        return 1
    if not _ARTIFACT_DIGEST_RE.fullmatch(expected_artifact_digest):
        print("::error::SECRETLESS_ARTIFACT_DIGEST_INVALID")
        return 1

    result_json = art_dir / "secretless-e2e" / "result.json"

    # R80 P0-05: 缺失 result.json 必须硬失败(不再 warning + return 0)
    if not result_json.exists():
        print(f"::error::SECRETLESS_RESULT_MISSING: {result_json} does not exist")
        return 1

    # R80 P0-05: 空文件必须硬失败
    if result_json.stat().st_size == 0:
        print(f"::error::SECRETLESS_RESULT_EMPTY: {result_json} is empty (0 bytes)")
        return 1

    # R80 P0-05: 非法 JSON 必须硬失败
    try:
        art = json.loads(result_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"::error::SECRETLESS_RESULT_INVALID_JSON: {exc}")
        return 1

    if not isinstance(art, dict):
        print("::error::SECRETLESS_RESULT_NOT_OBJECT: result.json is not a JSON object")
        return 1

    # R80: schema version 验证
    schema_version = art.get("schema_version", "")
    if schema_version not in VALID_SCHEMA_VERSIONS:
        print(
            f"::error::SECRETLESS_RESULT_SCHEMA_INVALID: "
            f"schema_version={schema_version!r}, expected one of {VALID_SCHEMA_VERSIONS}"
        )
        return 1

    # R80: event 必须是权威事件
    event = art.get("event", "")
    if event != AUTHORITATIVE_EVENT:
        print(
            f"::error::SECRETLESS_RESULT_EVENT_NOT_AUTHORITATIVE: "
            f"event={event!r}, expected {AUTHORITATIVE_EVENT!r}"
        )
        return 1

    # R80: identity 绑定校验
    expected = {
        "head_sha": expected_sha,
        "run_id": expected_run_id,
        "run_attempt": expected_attempt,
    }
    mismatches = [
        f"{k}: expected={expected[k]!r} actual={art.get(k)!r}"
        for k in expected
        if str(art.get(k) or "") != str(expected[k])
    ]
    if mismatches:
        print("::error::SECRETLESS_IDENTITY_MISMATCH: " + "; ".join(mismatches))
        return 1

    # R83: 复用 Step 20 strict verifier，不只检查摘要字段；status/job_status、
    # error/first_failure、phase 计数与全部 GO criteria 均须成立。
    strict_errors = verify_result(
        art,
        expected_sha=expected_sha,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_attempt,
        expected_workflow_path=WORKFLOW_PATH,
        expected_event=AUTHORITATIVE_EVENT,
    )
    if strict_errors:
        for error in strict_errors:
            print(f"::error::{error}")
        return 1

    evidence_path = (os.environ.get("SL_VERIFICATION_OUTPUT") or "").strip()
    if evidence_path:
        evidence = {
            "schema_version": "secretless-artifact-verification/v1",
            "status": "verified",
            "head_sha": expected_sha,
            "run_id": expected_run_id,
            "run_attempt": expected_attempt,
            "workflow_path": WORKFLOW_PATH,
            "event": AUTHORITATIVE_EVENT,
            "artifact_id": expected_artifact_id,
            "artifact_digest": expected_artifact_digest,
            "result_sha256": "sha256:" + hashlib.sha256(result_json.read_bytes()).hexdigest(),
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        output = Path(evidence_path)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(output)
        except OSError as exc:
            print(f"::error::SECRETLESS_ARTIFACT_VERIFICATION_WRITE_FAILED: {exc}")
            return 1

    print(
        "PASS: artifact verified — API artifact id/digest present, current-run identity bound, "
        "strict Step 20 contract satisfied"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
