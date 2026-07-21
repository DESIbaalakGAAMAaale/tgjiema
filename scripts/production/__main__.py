"""R67 Wave 4: scripts.production CLI 入口(`python -m scripts.production`)。

子命令:
    orchestrate   — 编排 6 类 production evidence 生成 + 严格门禁验证
    verify-ready  — 验证 candidate 是否满足全部 promotion 门禁
    promote       — 消费 production evidence 用于 promotion(单次使用)

使用方法:
    python -m scripts.production orchestrate \\
        --output-dir production-evidence --production

    python -m scripts.production verify-ready \\
        --evidence-path production-evidence/production_evidence_index.json \\
        --candidate-tag rc-2026-07-21-v1 \\
        --environment-id production-vps-01 \\
        --deploy-ref ghcr.io/owner/repo@sha256:abc... \\
        --release-manifest-digest sha256:abc... \\
        --approval-record approval.json \\
        --executed-by ops@example.com

    python -m scripts.production promote \\
        --evidence-path production-evidence/production_evidence_index.json \\
        --candidate-tag rc-2026-07-21-v1 \\
        --environment-id production-vps-01 \\
        --consumed-by ops@example.com \\
        --expected-attestation-digest sha256:att-abc
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_orchestrate(args: argparse.Namespace) -> int:
    from scripts.production.orchestrator import orchestrate_production_evidence

    result = orchestrate_production_evidence(
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("promotion_allowed") else 1


def _cmd_verify_ready(args: argparse.Namespace) -> int:
    from scripts.production.orchestrator import verify_promotion_readiness

    # 加载审批记录
    approval_record = json.loads(Path(args.approval_record).read_text())

    result = verify_promotion_readiness(
        evidence_path=Path(args.evidence_path),
        candidate_tag=args.candidate_tag,
        environment_id=args.environment_id,
        deploy_ref=args.deploy_ref,
        release_manifest_digest=args.release_manifest_digest,
        approval_record=approval_record,
        executed_by=args.executed_by,
        attestation_subject_digest=args.attestation_subject_digest or "",
        verify_only_3x_digest=args.verify_only_3x_digest or "",
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("ready") else 1


def _cmd_promote(args: argparse.Namespace) -> int:
    from scripts.production.orchestrator import promote_candidate
    from services.error_codes import AppError

    try:
        result = promote_candidate(
            evidence_path=Path(args.evidence_path),
            candidate_tag=args.candidate_tag,
            environment_id=args.environment_id,
            consumed_by=args.consumed_by,
            expected_attestation_digest=args.expected_attestation_digest,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except AppError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.production",
        description="R67 Wave 4: production evidence execution package",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # orchestrate
    p_orch = subparsers.add_parser(
        "orchestrate",
        help="编排 6 类 production evidence 生成 + 严格门禁验证",
    )
    p_orch.add_argument("--output-dir", required=True, help="证据输出目录")
    p_orch.add_argument("--dry-run", action="store_true", help="dry-run 模式")
    p_orch.add_argument("--production", action="store_true", help="production 模式")
    p_orch.set_defaults(func=_cmd_orchestrate)

    # verify-ready
    p_ready = subparsers.add_parser(
        "verify-ready",
        help="验证 candidate 是否满足全部 promotion 门禁",
    )
    p_ready.add_argument("--evidence-path", required=True)
    p_ready.add_argument("--candidate-tag", required=True)
    p_ready.add_argument("--environment-id", required=True)
    p_ready.add_argument("--deploy-ref", required=True)
    p_ready.add_argument("--release-manifest-digest", required=True)
    p_ready.add_argument("--approval-record", required=True, help="审批记录 JSON 路径")
    p_ready.add_argument("--executed-by", required=True)
    p_ready.add_argument("--attestation-subject-digest", default="")
    p_ready.add_argument("--verify-only-3x-digest", default="")
    p_ready.set_defaults(func=_cmd_verify_ready)

    # promote
    p_promote = subparsers.add_parser(
        "promote",
        help="消费 production evidence 用于 promotion(单次使用)",
    )
    p_promote.add_argument("--evidence-path", required=True)
    p_promote.add_argument("--candidate-tag", required=True)
    p_promote.add_argument("--environment-id", required=True)
    p_promote.add_argument("--consumed-by", required=True)
    p_promote.add_argument("--expected-attestation-digest", required=True)
    p_promote.set_defaults(func=_cmd_promote)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
