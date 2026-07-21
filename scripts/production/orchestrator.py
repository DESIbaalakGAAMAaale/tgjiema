"""R67 Wave 4: production evidence 编排器(orchestrator)。

编排 6 类 production evidence 生成 + verify_production_promotion 严格门禁 +
consume_evidence_for_promotion 单次使用语义。

R67 审计要求:
    - Wave 3 — 真实环境证据(6 类 artifact)
    - Wave 4 — RC tag 正式演练(production evidence 全通过)
    - P1-11 — 防重放(nonce/attestation_digest/time_window/consumed)

本编排器提供:
    1. orchestrate_production_evidence() — 生成全部 6 类证据 + 严格门禁验证
    2. promote_candidate() — 消费证据(单次使用)+ digest 锁定部署 + 环境审批
    3. verify_promotion_readiness() — 检查 candidate 是否满足 promotion 全部门禁

使用方法:
    # 1. 生成 production evidence
    result = orchestrate_production_evidence(
        output_dir=Path("production-evidence"),
        dry_run=False,
    )

    # 2. 检查 promotion 就绪状态
    readiness = verify_promotion_readiness(
        evidence_path=Path("production-evidence/production_evidence_index.json"),
        candidate_tag="rc-2026-07-21-v1",
        environment_id="production-vps-01",
        deploy_ref="ghcr.io/owner/repo@sha256:abc...",
        approval_record={...},
        executed_by="ops@example.com",
    )

    # 3. 消费证据用于 promotion(单次使用)
    promote_candidate(
        evidence_path=Path("production-evidence/production_evidence_index.json"),
        candidate_tag="rc-2026-07-21-v1",
        environment_id="production-vps-01",
        consumed_by="ops@example.com",
        expected_attestation_digest="sha256:att-abc",
    )
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# 项目根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.generate_production_evidence import (
    REQUIRED_ARTIFACT_TYPES,
    consume_evidence_for_promotion,
    verify_production_promotion,
)
from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier
from scripts.production.environment_approval import EnvironmentApprovalGate


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _get_commit_sha() -> str:
    """获取当前 git HEAD SHA(失败回退到 GITHUB_SHA 或 unknown)。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha and len(sha) >= 7:
                return sha
    except Exception:
        pass
    import os
    return os.environ.get("GITHUB_SHA", "unknown")


class ProductionEvidenceOrchestrator:
    """R67 Wave 4: production evidence 编排器。

    编排 6 类证据生成 + verify_production_promotion 严格门禁 +
    consume_evidence_for_promotion 单次使用语义。
    """

    def __init__(
        self,
        *,
        output_dir: Path | str,
        repo_root: Path | str | None = None,
    ) -> None:
        """初始化编排器。

        Args:
            output_dir: 证据输出目录
            repo_root: 仓库根目录(默认自动检测)
        """
        self.output_dir = Path(output_dir)
        self.repo_root = Path(repo_root) if repo_root else _REPO_ROOT

    def orchestrate(
        self,
        *,
        dry_run: bool = False,
        evidence_types: list[str] | None = None,
        extra_args: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """编排 production evidence 生成 + 严格门禁验证。

        Args:
            dry_run: True=dry-run 模式(不执行真实测试);False=production 模式
            evidence_types: 要生成的证据类型列表(默认全部 6 类)
            extra_args: {evidence_type: [extra_args]} 额外参数

        Returns:
            {
                "evidence_mode": "dry_run" | "production",
                "evidence_path": str,
                "promotion_allowed": bool,
                "verification_result": dict,
                "artifacts_generated": int,
                "failures": list[str],
            }
        """
        # 调用 generate_production_evidence.py 生成证据
        cmd = [
            sys.executable,
            str(self.repo_root / "scripts" / "generate_production_evidence.py"),
            "--all" if evidence_types is None else "--only",
            "--output-dir", str(self.output_dir),
        ]
        if evidence_types is not None:
            cmd.extend(evidence_types)
        if dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--production")

        # 添加额外参数
        if extra_args:
            for etype, args in extra_args.items():
                cmd.extend(args)

        result = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=3600,  # 1 小时超时(production 模式可能更久)
        )

        if result.returncode != 0:
            return {
                "evidence_mode": "dry_run" if dry_run else "production",
                "evidence_path": "",
                "promotion_allowed": False,
                "verification_result": {
                    "error": f"generate_production_evidence.py 退出码 {result.returncode}",
                    "stderr": result.stderr[-2000:] if result.stderr else "",
                },
                "artifacts_generated": 0,
                "failures": ["evidence 生成失败"],
            }

        # 查找 evidence 索引文件
        evidence_path = self.output_dir / "production_evidence_index.json"
        if not evidence_path.exists():
            # 回退查找
            import glob
            candidates = sorted(self.output_dir.glob("*evidence*.json"))
            if candidates:
                evidence_path = candidates[-1]
            else:
                return {
                    "evidence_mode": "dry_run" if dry_run else "production",
                    "evidence_path": "",
                    "promotion_allowed": False,
                    "verification_result": {"error": "evidence 索引文件未找到"},
                    "artifacts_generated": 0,
                    "failures": ["evidence 索引文件未找到"],
                }

        # 严格门禁验证
        try:
            verification = verify_production_promotion(evidence_path)
        except Exception as e:
            return {
                "evidence_mode": "dry_run" if dry_run else "production",
                "evidence_path": str(evidence_path),
                "promotion_allowed": False,
                "verification_result": {"error": str(e)},
                "artifacts_generated": 0,
                "failures": [f"verify_production_promotion 失败: {e}"],
            }

        # 统计 artifacts
        try:
            evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
            artifacts_count = len(evidence_data.get("artifacts", []))
        except (json.JSONDecodeError, OSError):
            artifacts_count = 0

        return {
            "evidence_mode": "dry_run" if dry_run else "production",
            "evidence_path": str(evidence_path),
            "promotion_allowed": verification.get("production_promotion_allowed", False),
            "verification_result": verification,
            "artifacts_generated": artifacts_count,
            "failures": [] if verification.get("production_promotion_allowed") else [
                "verify_production_promotion 拒绝晋级"
            ],
        }


def orchestrate_production_evidence(
    *,
    output_dir: Path | str,
    dry_run: bool = False,
    evidence_types: list[str] | None = None,
    extra_args: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """便捷函数:编排 production evidence 生成 + 严格门禁验证。"""
    orchestrator = ProductionEvidenceOrchestrator(output_dir=output_dir)
    return orchestrator.orchestrate(
        dry_run=dry_run,
        evidence_types=evidence_types,
        extra_args=extra_args,
    )


def verify_promotion_readiness(
    *,
    evidence_path: Path | str,
    candidate_tag: str,
    environment_id: str,
    deploy_ref: str,
    release_manifest_digest: str,
    approval_record: dict[str, Any],
    executed_by: str,
    attestation_subject_digest: str = "",
    verify_only_3x_digest: str = "",
) -> dict[str, Any]:
    """R67 Wave 4: 验证 candidate 是否满足全部 promotion 门禁。

    检查项:
        1. production evidence 严格门禁(verify_production_promotion)
        2. 环境审批门禁(EnvironmentApprovalGate)
        3. digest 锁定部署验证(DigestPinnedDeployVerifier)

    Args:
        evidence_path: evidence JSON 文件路径
        candidate_tag: 候选 RC tag
        environment_id: 部署环境 ID
        deploy_ref: 部署引用(必须含 @sha256: digest)
        release_manifest_digest: release manifest 中的 image digest
        approval_record: 环境审批记录
        executed_by: 执行部署的用户/服务账号
        attestation_subject_digest: attestation subject digest(可选)
        verify_only_3x_digest: verify-only-3x 验证记录中的 digest(可选)

    Returns:
        {
            "ready": bool,  # 全部门禁通过
            "evidence_gate": {...},
            "approval_gate": {...},
            "deploy_gate": {...},
            "failures": list[str],
        }
    """
    failures: list[str] = []

    # 1. production evidence 严格门禁
    try:
        evidence_result = verify_production_promotion(evidence_path)
        evidence_allowed = evidence_result.get("production_promotion_allowed", False)
        if not evidence_allowed:
            failures.append("production evidence 门禁未通过")
    except Exception as e:
        evidence_result = {"error": str(e), "production_promotion_allowed": False}
        evidence_allowed = False
        failures.append(f"production evidence 门禁异常: {e}")

    # 2. 环境审批门禁
    approval_gate = EnvironmentApprovalGate(
        candidate_tag=candidate_tag,
        environment_id=environment_id,
    )
    approval_result = approval_gate.verify(
        approval_record, executed_by=executed_by,
    )
    if not approval_result["approved"]:
        failures.append(f"环境审批门禁未通过: {approval_result['reason']}")

    # 3. digest 锁定部署验证
    deploy_verifier = DigestPinnedDeployVerifier(
        release_manifest_digest=release_manifest_digest,
        attestation_subject_digest=attestation_subject_digest,
        verify_only_3x_digest=verify_only_3x_digest,
    )
    deploy_result = deploy_verifier.verify_deploy_ref(deploy_ref)
    if not deploy_result["verified"]:
        failures.append(f"digest 锁定部署验证未通过: {deploy_result['reason']}")

    return {
        "ready": len(failures) == 0,
        "evidence_gate": {
            "passed": evidence_allowed,
            "result": evidence_result,
        },
        "approval_gate": approval_result,
        "deploy_gate": deploy_result,
        "failures": failures,
    }


def promote_candidate(
    *,
    evidence_path: Path | str,
    candidate_tag: str,
    environment_id: str,
    consumed_by: str,
    expected_attestation_digest: str,
) -> dict[str, Any]:
    """R67 P1-11: 消费 production evidence 用于指定 candidate 的 promotion。

    单次使用语义:每个 evidence artifact 只能被一个 candidate 消费一次。
    重复消费(跨候选复用)会抛 AppError(EVIDENCE_ALREADY_CONSUMED)。

    Args:
        evidence_path: evidence JSON 文件路径
        candidate_tag: 当前 candidate 的 tag
        environment_id: 当前部署环境 ID
        consumed_by: 执行 promotion 的用户/服务账号
        expected_attestation_digest: 当前 release manifest attestation digest

    Returns:
        更新后的 evidence dict(每个 artifact 含 consumed=true)

    Raises:
        AppError(EVIDENCE_ALREADY_CONSUMED): 任一 artifact 已被其他 candidate 消费
        AppError(PRODUCTION_EVIDENCE_INSUFFICIENT): 校验失败
    """
    return consume_evidence_for_promotion(
        evidence_path,
        candidate_tag=candidate_tag,
        consumed_by=consumed_by,
        expected_environment_id=environment_id,
        expected_attestation_digest=expected_attestation_digest,
    )
