"""R67 Wave 4: 生产证据执行包(production evidence execution package)。

R67 审计背景(Wave 4 — RC tag 正式演练):
    - signed annotated RC tag
    - tag workflow、environment approval、production evidence、
      digest-pinned deploy、rollback 全通过

本包提供:
    1. artifact_builder — 构建带 P1-11 防重放字段(nonce/attestation_digest/
       time_window/consumed)的 production evidence artifact
    2. orchestrator — 编排 6 类证据生成 + verify_production_promotion 严格门禁
    3. environment_approval — 环境审批门禁(production 部署前强制审批)
    4. digest_pinned_deploy — digest 锁定部署验证(image digest 必须与 release
       manifest 一致,禁止 tag漂移)
    5. cli — 统一 CLI 入口(`python -m scripts.production`)

与现有 generate_production_evidence.py 的关系:
    - generate_production_evidence.py 是底层证据生成器(orchestrator 调用)
    - 本包提供更高层的"production execution"语义:artifact 构建、审批门禁、
      digest 锁定部署、promote/consume 单次使用
    - 复用 generate_production_evidence.py 的 EVIDENCE_TYPES /
      REQUIRED_ARTIFACT_TYPES / consume_evidence_for_promotion /
      verify_production_promotion
"""
from __future__ import annotations

# 公开 API
from scripts.production.artifact_builder import (
    ProductionArtifactBuilder,
    build_artifact,
)
from scripts.production.orchestrator import (
    ProductionEvidenceOrchestrator,
    orchestrate_production_evidence,
)
from scripts.production.environment_approval import (
    EnvironmentApprovalGate,
    verify_environment_approval,
)
from scripts.production.digest_pinned_deploy import (
    DigestPinnedDeployVerifier,
    verify_digest_pinned_deploy,
)

__all__ = [
    "ProductionArtifactBuilder",
    "build_artifact",
    "ProductionEvidenceOrchestrator",
    "orchestrate_production_evidence",
    "EnvironmentApprovalGate",
    "verify_environment_approval",
    "DigestPinnedDeployVerifier",
    "verify_digest_pinned_deploy",
]

__version__ = "1.0.0"
