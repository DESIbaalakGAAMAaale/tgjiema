#!/usr/bin/env python3
"""R67 Wave 5: RC tag 正式演练(RC tag drill)。

R67 审计要求(audit report Wave 4 — RC tag 正式演练):
    - signed annotated RC tag。
    - tag workflow、environment approval、production evidence、
      digest-pinned deploy、rollback 全通过。

本脚本编排完整 promotion 演练链路,涵盖 6 个阶段:

    1. Verify signed annotated RC tag
       — git tag -v <tag> 通过(GPG 签名验证)
       — git cat-file -t <tag> == tag(annotated,不是 lightweight)
       — tag 指向的 commit 满足 verified=true(git verify-commit)

    2. Verify tag workflow triggered & succeeded
       — release-gates.yml 在 tag 上触发
       — run conclusion == success(非 cancelled/failure)
       — 同一 SHA 不复用祖先 run(P0-03)

    3. Verify environment approval
       — 使用 Wave 4 EnvironmentApprovalGate
       — 职责分离(approver ≠ executor)
       — candidate_tag 与 environment_id 匹配

    4. Verify production evidence complete
       — 使用 Wave 4 ProductionEvidenceOrchestrator
       — 6 类 artifact 齐全(SOAK/RESTORE/CHAOS/RU/SUPPLY/RC_VERIFY_3X)
       — 防重放字段(nonce/attestation_digest/time_window/consumed)

    5. Verify digest-pinned deploy
       — 使用 Wave 4 DigestPinnedDeployVerifier
       — deploy ref 含 @sha256: 不可变 digest(非 :tag)
       — digest 与 release manifest / attestation / verify-only-3x 一致

    6. Verify rollback capability
       — restore_rollback_targets 表存在
       — 有未过期的 active_pointer 回滚点
       — fencing token 存在(P1-06 recovery reconciler)

使用方法:
    # 演练(dry-run 模式,不调用真实 gh/git/gpg)
    python scripts/rc_tag_drill.py drill \\
        --tag v1.0.0-rc1 \\
        --environment-id production-vps-01 \\
        --deploy-ref ghcr.io/owner/repo@sha256:abc... \\
        --approval-record approval.json \\
        --evidence-path production-evidence/production_evidence_index.json \\
        --release-manifest-digest sha256:abc... \\
        --executed-by ops@example.com \\
        --dry-run \\
        --output-report drill-report.json

    # 验证已完成演练的产物
    python scripts/rc_tag_drill.py verify \\
        --report drill-report.json

    # 仅检查 rollback 能力
    python scripts/rc_tag_drill.py rollback-check \\
        --environment-id production-vps-01

退出码:
    0: 全部 6 个阶段通过
    1: 至少一个阶段失败(查看 report 了解详情)
    2: 参数错误或环境不可用
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# 仓库根目录
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _run_git(args: list[str], repo_root: Path | None = None,
             timeout: int = 10) -> tuple[int, str, str]:
    """运行 git 子命令,返回 (returncode, stdout, stderr)。"""
    cwd = str(repo_root) if repo_root else str(_REPO_ROOT)
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def _run_gh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """运行 gh CLI 子命令,返回 (returncode, stdout, stderr)。"""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def _make_result(stage: str, passed: bool, reason: str,
                 **details: Any) -> dict[str, Any]:
    """构造单阶段验证结果。"""
    return {
        "stage": stage,
        "passed": passed,
        "reason": reason,
        "details": details,
        "verified_at": _now_iso(),
    }


# ════════════════════════════════════════════════════════════════
# 阶段 1: 验证 signed annotated RC tag
# ════════════════════════════════════════════════════════════════

def verify_signed_annotated_tag(
    tag: str,
    repo_root: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """阶段 1: 验证 RC tag 是 signed annotated tag。

    检查项:
        1. tag 存在(git rev-parse 解析成功)
        2. tag 是 annotated(git cat-file -t == "tag",非 "commit")
        3. tag 已签名(git tag -v 退出码 0,GPG 验证通过)
        4. tag 指向的 commit 验证通过(git verify-commit,可选)

    dry_run 模式下,跳过真实 git/gpg 调用,返回模拟成功结果。
    """
    if dry_run:
        return _make_result(
            "verify_signed_annotated_tag",
            passed=True,
            reason="dry-run: 模拟 signed annotated tag 验证通过",
            tag=tag,
            tag_type="tag",
            signature_verified=True,
            commit_verified=True,
            dry_run=True,
        )

    # 1.1 tag 存在性
    rc, out, err = _run_git(["rev-parse", "--verify", f"refs/tags/{tag}"],
                            repo_root=repo_root)
    if rc != 0:
        return _make_result(
            "verify_signed_annotated_tag",
            passed=False,
            reason=f"tag 不存在: {tag!r} (git rev-parse 失败: {err.strip()})",
            tag=tag,
            tag_exists=False,
        )

    tag_sha = out.strip()

    # 1.2 tag 类型(annotated vs lightweight)
    rc, out, err = _run_git(["cat-file", "-t", tag_sha], repo_root=repo_root)
    tag_type = out.strip()
    if rc != 0 or tag_type != "tag":
        return _make_result(
            "verify_signed_annotated_tag",
            passed=False,
            reason=(
                f"tag {tag!r} 不是 annotated tag (类型: {tag_type!r},"
                f"期望 'tag') — lightweight tag 无签名载体"
            ),
            tag=tag,
            tag_sha=tag_sha,
            tag_type=tag_type,
        )

    # 1.3 GPG 签名验证
    rc, out, err = _run_git(["tag", "-v", tag], repo_root=repo_root)
    signature_verified = (rc == 0)
    if not signature_verified:
        return _make_result(
            "verify_signed_annotated_tag",
            passed=False,
            reason=(
                f"tag {tag!r} GPG 签名验证失败 (git tag -v 退出码 {rc}): "
                f"{err.strip() or out.strip()}"
            ),
            tag=tag,
            tag_sha=tag_sha,
            tag_type=tag_type,
            signature_verified=False,
            verify_output=err.strip() or out.strip(),
        )

    # 1.4 tag 指向 commit 的签名验证(可选,失败不阻断)
    rc, out, err = _run_git(["rev-list", "-n", "1", tag], repo_root=repo_root)
    commit_sha = out.strip()
    commit_verified = False
    if commit_sha:
        rc2, _, _ = _run_git(["verify-commit", commit_sha], repo_root=repo_root)
        commit_verified = (rc2 == 0)

    return _make_result(
        "verify_signed_annotated_tag",
        passed=True,
        reason=f"tag {tag!r} 是 signed annotated tag,签名验证通过",
        tag=tag,
        tag_sha=tag_sha,
        tag_type=tag_type,
        signature_verified=True,
        commit_sha=commit_sha,
        commit_verified=commit_verified,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 2: 验证 tag workflow 触发并成功
# ════════════════════════════════════════════════════════════════

def verify_tag_workflow_triggered(
    tag: str,
    repo: str | None = None,
    *,
    dry_run: bool = False,
    workflow_name: str = "release-gates.yml",
) -> dict[str, Any]:
    """阶段 2: 验证 tag workflow 在该 tag 上触发并成功。

    检查项:
        1. release-gates.yml workflow 在 tag push 上触发
        2. run conclusion == success
        3. run 的 head_sha 等于 tag 指向的 commit(同 SHA 验证,不复用祖先 run)

    dry_run 模式下,跳过真实 gh CLI 调用,返回模拟成功结果。
    """
    if dry_run:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=True,
            reason="dry-run: 模拟 tag workflow 触发并成功",
            tag=tag,
            workflow=workflow_name,
            run_id="dry-run-123",
            conclusion="success",
            head_sha="dry-run-commit-sha",
            same_sha=True,
            dry_run=True,
        )

    # 2.1 确定 repo(owner/repo 格式)
    if not repo:
        repo_env = os.environ.get("GITHUB_REPOSITORY", "")
        if repo_env:
            repo = repo_env
        else:
            rc, out, _ = _run_git(["remote", "get-url", "origin"])
            if rc == 0:
                url = out.strip()
                # 解析 git@github.com:owner/repo.git 或 https://github.com/owner/repo.git
                import re
                m = re.search(r"github\.com[:/]([^/]+)/([^/.\s]+)", url)
                if m:
                    repo = f"{m.group(1)}/{m.group(2)}"
    if not repo:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason="无法确定 GitHub 仓库(owner/repo)— 需设置 GITHUB_REPOSITORY 环境变量或 git remote origin",
            tag=tag,
            workflow=workflow_name,
        )

    # 2.2 获取 tag 指向的 commit SHA
    rc, out, _ = _run_git(["rev-list", "-n", "1", tag])
    if rc != 0:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=f"无法解析 tag {tag!r} 指向的 commit",
            tag=tag,
            workflow=workflow_name,
            repo=repo,
        )
    expected_sha = out.strip()

    # 2.3 查询 workflow runs(通过 gh CLI)
    rc, out, err = _run_gh([
        "run", "list",
        "--workflow", workflow_name,
        "--branch", tag,  # tag push 时 branch 字段为 tag 名
        "--repo", repo,
        "--limit", "5",
        "--json", "databaseId,status,conclusion,headSha,event,headBranch",
    ])
    if rc != 0:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=f"gh run list 失败(退出码 {rc}): {err.strip()}",
            tag=tag,
            workflow=workflow_name,
            repo=repo,
            expected_sha=expected_sha,
        )

    try:
        runs = json.loads(out) if out.strip() else []
    except json.JSONDecodeError as e:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=f"gh run list 输出 JSON 解析失败: {e}",
            tag=tag,
            workflow=workflow_name,
            repo=repo,
        )

    if not runs:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=(
                f"未找到 {workflow_name} 在 tag {tag!r} 上的 workflow run — "
                f"tag workflow 未触发或 workflow 配置不覆盖 tag push"
            ),
            tag=tag,
            workflow=workflow_name,
            repo=repo,
            expected_sha=expected_sha,
        )

    # 2.4 找到 head_sha 匹配的 run(同 SHA 验证)
    matching_runs = [r for r in runs if r.get("headSha", "") == expected_sha]
    if not matching_runs:
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=(
                f"workflow run 存在但 head_sha 不匹配 tag commit "
                f"(期望 {expected_sha[:12]},实际 {[r.get('headSha', '')[:12] for r in runs]}) — "
                f"R67 P0-03: 不允许复用祖先 run 的成功结果"
            ),
            tag=tag,
            workflow=workflow_name,
            repo=repo,
            expected_sha=expected_sha,
            available_runs=runs,
        )

    run = matching_runs[0]
    conclusion = run.get("conclusion", "")
    status = run.get("status", "")

    # 2.5 验证 conclusion == success
    if status != "completed":
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=f"workflow run 状态未完成(status={status!r},conclusion={conclusion!r})",
            tag=tag,
            workflow=workflow_name,
            repo=repo,
            run_id=run.get("databaseId"),
            head_sha=expected_sha,
            status=status,
            conclusion=conclusion,
        )

    if conclusion != "success":
        return _make_result(
            "verify_tag_workflow_triggered",
            passed=False,
            reason=f"workflow run 失败(conclusion={conclusion!r},期望 'success')",
            tag=tag,
            workflow=workflow_name,
            repo=repo,
            run_id=run.get("databaseId"),
            head_sha=expected_sha,
            conclusion=conclusion,
        )

    return _make_result(
        "verify_tag_workflow_triggered",
        passed=True,
        reason=f"workflow {workflow_name} 在 tag {tag!r} 上成功(SHA={expected_sha[:12]})",
        tag=tag,
        workflow=workflow_name,
        repo=repo,
        run_id=run.get("databaseId"),
        head_sha=expected_sha,
        same_sha=True,
        conclusion=conclusion,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 3: 验证 environment approval
# ════════════════════════════════════════════════════════════════

def verify_environment_approval(
    approval_record: dict[str, Any],
    candidate_tag: str,
    environment_id: str,
    executed_by: str,
) -> dict[str, Any]:
    """阶段 3: 验证环境审批记录。

    使用 Wave 4 EnvironmentApprovalGate,检查:
        - 审批记录非空
        - candidate_tag 匹配
        - environment_id 匹配
        - 职责分离(approver ≠ executor)
        - 未被撤销
        - 在时间窗内(未过期、非未来)

    Args:
        approval_record: 审批记录 dict(含 approver, candidate_tag,
            environment_id, approved_at, revoked 等字段)
        candidate_tag: 当前候选 RC tag
        environment_id: 部署环境 ID
        executed_by: 执行部署的用户/服务账号
    """
    from scripts.production.environment_approval import EnvironmentApprovalGate

    gate = EnvironmentApprovalGate(
        candidate_tag=candidate_tag,
        environment_id=environment_id,
    )
    result = gate.verify(approval_record, executed_by=executed_by)
    return _make_result(
        "verify_environment_approval",
        passed=result["approved"],
        reason=result["reason"],
        candidate_tag=candidate_tag,
        environment_id=environment_id,
        executed_by=executed_by,
        approval=result,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 4: 验证 production evidence 完整
# ════════════════════════════════════════════════════════════════

def verify_production_evidence_complete(
    evidence_path: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """阶段 4: 验证 production evidence 完整且通过严格门禁。

    使用 verify_production_promotion() 严格门禁,检查:
        - 6 类 artifact 齐全(SOAK/RESTORE/CHAOS/RU/SUPPLY/RC_VERIFY_3X)
        - 每个 artifact 含全部必需字段(含 R67 P1-11 防重放字段)
        - evidence_mode == "production"(非 dry_run)
        - production_promotion_allowed == True
        - 所有 artifact 未过期、未消费

    Args:
        evidence_path: evidence JSON 文件路径
        dry_run: 若 True,跳过严格门禁,仅检查文件存在(测试用)
    """
    from scripts.generate_production_evidence import (
        REQUIRED_ARTIFACT_TYPES,
        verify_production_promotion,
    )

    evidence_path = Path(evidence_path)
    if not evidence_path.exists():
        return _make_result(
            "verify_production_evidence_complete",
            passed=False,
            reason=f"evidence 文件不存在: {evidence_path}",
            evidence_path=str(evidence_path),
        )

    # 读取 evidence 文件,检查基础结构
    try:
        evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _make_result(
            "verify_production_evidence_complete",
            passed=False,
            reason=f"evidence 文件解析失败: {e}",
            evidence_path=str(evidence_path),
        )

    artifacts = evidence_data.get("artifacts", [])
    artifact_types = {a.get("artifact_type") for a in artifacts if isinstance(a, dict)}
    missing_types = set(REQUIRED_ARTIFACT_TYPES) - artifact_types
    if missing_types:
        return _make_result(
            "verify_production_evidence_complete",
            passed=False,
            reason=(
                f"缺少必需 artifact 类型: {sorted(missing_types)} — "
                f"6 类 artifact 必须齐全"
            ),
            evidence_path=str(evidence_path),
            artifact_types_present=sorted(artifact_types),
            missing_types=sorted(missing_types),
        )

    # dry_run 模式:仅检查文件结构,不调用严格门禁
    if dry_run:
        return _make_result(
            "verify_production_evidence_complete",
            passed=True,
            reason="dry-run: evidence 文件结构检查通过(跳过严格门禁)",
            evidence_path=str(evidence_path),
            artifact_count=len(artifacts),
            artifact_types=sorted(artifact_types),
            dry_run=True,
        )

    # 调用严格门禁
    try:
        verification = verify_production_promotion(evidence_path)
    except Exception as e:
        return _make_result(
            "verify_production_evidence_complete",
            passed=False,
            reason=f"verify_production_promotion 抛异常: {e}",
            evidence_path=str(evidence_path),
            error=str(e),
        )

    allowed = verification.get("production_promotion_allowed", False)
    return _make_result(
        "verify_production_evidence_complete",
        passed=allowed,
        reason=(
            "production evidence 严格门禁通过"
            if allowed
            else f"production evidence 严格门禁未通过: {verification.get('errors', [])}"
        ),
        evidence_path=str(evidence_path),
        artifact_count=len(artifacts),
        verification=verification,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 5: 验证 digest-pinned deploy
# ════════════════════════════════════════════════════════════════

def verify_digest_pinned_deploy(
    deploy_ref: str,
    release_manifest_digest: str,
    *,
    attestation_subject_digest: str = "",
    verify_only_3x_digest: str = "",
) -> dict[str, Any]:
    """阶段 5: 验证 deploy ref 使用不可变 digest 锁定。

    使用 Wave 4 DigestPinnedDeployVerifier,检查:
        - deploy_ref 含 @sha256:... 不可变 digest(非 :tag 可变引用)
        - digest 格式合法(sha256: + 64 hex chars)
        - digest 与 release_manifest_digest 一致
        - 若提供 attestation_subject_digest,需一致
        - 若提供 verify_only_3x_digest,需一致

    Args:
        deploy_ref: 部署引用(如 ghcr.io/owner/repo@sha256:abc...)
        release_manifest_digest: release manifest 中的 image digest
        attestation_subject_digest: attestation subject digest(可选)
        verify_only_3x_digest: verify-only-3x 记录中的 digest(可选)
    """
    from scripts.production.digest_pinned_deploy import DigestPinnedDeployVerifier

    verifier = DigestPinnedDeployVerifier(
        release_manifest_digest=release_manifest_digest,
        attestation_subject_digest=attestation_subject_digest,
        verify_only_3x_digest=verify_only_3x_digest,
    )
    result = verifier.verify_deploy_ref(deploy_ref)
    return _make_result(
        "verify_digest_pinned_deploy",
        passed=result["verified"],
        reason=result["reason"],
        deploy_ref=deploy_ref,
        release_manifest_digest=release_manifest_digest,
        deploy_verification=result,
    )


# ════════════════════════════════════════════════════════════════
# 阶段 6: 验证 rollback 能力
# ════════════════════════════════════════════════════════════════

def verify_rollback_capability(
    environment_id: str,
    *,
    rollback_target_path: Path | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """阶段 6: 验证 rollback 能力就绪。

    R67 P1-06 recovery reconciler 要求:部署前必须有可用的 rollback 点。

    检查项:
        1. rollback target 记录存在
        2. active_pointer 非空(有旧版本指针)
        3. expires_at 未过期
        4. fencing_token 存在(P1-06 CAS 防并发)
        5. operation_id 非空(可追溯)

    dry_run 模式下,返回模拟成功结果。

    Args:
        environment_id: 部署环境 ID
        rollback_target_path: rollback target JSON 文件路径
            (生产环境从 CRDB/SQLite 读取;测试用文件模拟)
    """
    if dry_run:
        return _make_result(
            "verify_rollback_capability",
            passed=True,
            reason="dry-run: 模拟 rollback 能力就绪",
            environment_id=environment_id,
            has_rollback_target=True,
            active_pointer_present=True,
            not_expired=True,
            fencing_token_present=True,
            operation_id_present=True,
            dry_run=True,
        )

    # 从文件读取 rollback target(生产环境应从 CRDB/SQLite 读取)
    if rollback_target_path is None:
        rollback_target_path = (
            _REPO_ROOT / "data" / f"rollback_target_{environment_id}.json"
        )
    rollback_target_path = Path(rollback_target_path)

    if not rollback_target_path.exists():
        return _make_result(
            "verify_rollback_capability",
            passed=False,
            reason=(
                f"rollback target 文件不存在: {rollback_target_path} — "
                f"R67 P1-06: 部署前必须建立 rollback 点"
            ),
            environment_id=environment_id,
            rollback_target_path=str(rollback_target_path),
        )

    try:
        target = json.loads(rollback_target_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _make_result(
            "verify_rollback_capability",
            passed=False,
            reason=f"rollback target 文件解析失败: {e}",
            environment_id=environment_id,
            rollback_target_path=str(rollback_target_path),
        )

    # 检查必需字段
    active_pointer = target.get("active_pointer")
    expires_at = target.get("expires_at", "")
    fencing_token = target.get("fencing_token", "")
    operation_id = target.get("operation_id", "")
    target_environment = target.get("environment_id", "")

    failures: list[str] = []

    if not active_pointer:
        failures.append("active_pointer 为空 — 无旧版本指针,无法回滚")
    if not expires_at:
        failures.append("expires_at 缺失 — 无法判断回滚点是否过期")
    else:
        # 检查是否过期
        import datetime as _dt
        try:
            exp_dt = _dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            now = _dt.datetime.now(_dt.timezone.utc)
            if exp_dt < now:
                failures.append(f"rollback target 已过期(expires_at={expires_at})")
        except (ValueError, TypeError):
            failures.append(f"expires_at 格式无效: {expires_at!r}")
    if not fencing_token:
        failures.append("fencing_token 缺失 — R67 P1-06: CAS 防并发 token 必须")
    if not operation_id:
        failures.append("operation_id 缺失 — 无法追溯回滚点对应的操作")
    if target_environment and target_environment != environment_id:
        failures.append(
            f"environment_id 不匹配(rollback target={target_environment!r},"
            f"期望={environment_id!r})"
        )

    if failures:
        return _make_result(
            "verify_rollback_capability",
            passed=False,
            reason="; ".join(failures),
            environment_id=environment_id,
            rollback_target_path=str(rollback_target_path),
            failures=failures,
        )

    return _make_result(
        "verify_rollback_capability",
        passed=True,
        reason=f"rollback 能力就绪(environment={environment_id!r})",
        environment_id=environment_id,
        rollback_target_path=str(rollback_target_path),
        active_pointer_present=True,
        not_expired=True,
        fencing_token_present=True,
        operation_id_present=True,
        expires_at=expires_at,
        operation_id=operation_id,
    )


# ════════════════════════════════════════════════════════════════
# 演练编排:run_drill
# ════════════════════════════════════════════════════════════════

# 演练阶段顺序(6 个阶段)
DRILL_STAGES = (
    "verify_signed_annotated_tag",
    "verify_tag_workflow_triggered",
    "verify_environment_approval",
    "verify_production_evidence_complete",
    "verify_digest_pinned_deploy",
    "verify_rollback_capability",
)


def run_drill(
    *,
    tag: str,
    environment_id: str,
    deploy_ref: str,
    approval_record: dict[str, Any],
    evidence_path: Path | str,
    release_manifest_digest: str,
    executed_by: str,
    attestation_subject_digest: str = "",
    verify_only_3x_digest: str = "",
    rollback_target_path: Path | str | None = None,
    repo: str | None = None,
    dry_run: bool = False,
    workflow_name: str = "release-gates.yml",
) -> dict[str, Any]:
    """R67 Wave 5: 运行完整 RC tag 演练。

    依次执行 6 个阶段,返回综合报告。任一阶段失败不中断后续阶段
    (collect-all 模式),便于一次性看到所有问题。

    Args:
        tag: RC tag 名称(如 v1.0.0-rc1)
        environment_id: 部署环境 ID
        deploy_ref: 部署引用(含 @sha256: digest)
        approval_record: 环境审批记录 dict
        evidence_path: production evidence JSON 文件路径
        release_manifest_digest: release manifest 中的 image digest
        executed_by: 执行部署的用户/服务账号
        attestation_subject_digest: attestation subject digest(可选)
        verify_only_3x_digest: verify-only-3x 记录中的 digest(可选)
        rollback_target_path: rollback target JSON 文件路径(可选)
        repo: GitHub 仓库(owner/repo,可选)
        dry_run: True=dry-run 模式(不调用真实 gh/git/gpg)
        workflow_name: workflow 文件名(默认 release-gates.yml)

    Returns:
        {
            "drill_passed": bool,  # 全部 6 阶段通过
            "stages_passed": int,  # 通过阶段数
            "stages_total": int,   # 总阶段数(6)
            "stages": {stage_name: result_dict, ...},
            "failures": list[str],  # 失败阶段及原因
            "dry_run": bool,
            "drilled_at": str,      # ISO8601 时间戳
            "tag": str,
            "environment_id": str,
        }
    """
    stages: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    # 阶段 1
    r1 = verify_signed_annotated_tag(tag, dry_run=dry_run)
    stages["verify_signed_annotated_tag"] = r1
    if not r1["passed"]:
        failures.append(f"[阶段1] {r1['reason']}")

    # 阶段 2
    r2 = verify_tag_workflow_triggered(tag, repo=repo, dry_run=dry_run,
                                       workflow_name=workflow_name)
    stages["verify_tag_workflow_triggered"] = r2
    if not r2["passed"]:
        failures.append(f"[阶段2] {r2['reason']}")

    # 阶段 3
    r3 = verify_environment_approval(
        approval_record=approval_record,
        candidate_tag=tag,
        environment_id=environment_id,
        executed_by=executed_by,
    )
    stages["verify_environment_approval"] = r3
    if not r3["passed"]:
        failures.append(f"[阶段3] {r3['reason']}")

    # 阶段 4
    r4 = verify_production_evidence_complete(evidence_path, dry_run=dry_run)
    stages["verify_production_evidence_complete"] = r4
    if not r4["passed"]:
        failures.append(f"[阶段4] {r4['reason']}")

    # 阶段 5
    r5 = verify_digest_pinned_deploy(
        deploy_ref=deploy_ref,
        release_manifest_digest=release_manifest_digest,
        attestation_subject_digest=attestation_subject_digest,
        verify_only_3x_digest=verify_only_3x_digest,
    )
    stages["verify_digest_pinned_deploy"] = r5
    if not r5["passed"]:
        failures.append(f"[阶段5] {r5['reason']}")

    # 阶段 6
    r6 = verify_rollback_capability(
        environment_id=environment_id,
        rollback_target_path=rollback_target_path,
        dry_run=dry_run,
    )
    stages["verify_rollback_capability"] = r6
    if not r6["passed"]:
        failures.append(f"[阶段6] {r6['reason']}")

    stages_passed = sum(1 for r in stages.values() if r["passed"])
    return {
        "drill_passed": len(failures) == 0,
        "stages_passed": stages_passed,
        "stages_total": len(DRILL_STAGES),
        "stages": stages,
        "failures": failures,
        "dry_run": dry_run,
        "drilled_at": _now_iso(),
        "tag": tag,
        "environment_id": environment_id,
    }


def verify_drill_report(report_path: Path | str) -> dict[str, Any]:
    """验证已完成的 drill 报告。

    读取 JSON 报告文件,检查:
        - 6 个阶段都存在
        - drill_passed == True
        - dry_run == False(生产 drill 不能是 dry-run)

    Args:
        report_path: drill 报告 JSON 文件路径
    """
    report_path = Path(report_path)
    if not report_path.exists():
        return {
            "valid": False,
            "reason": f"drill 报告不存在: {report_path}",
        }

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {
            "valid": False,
            "reason": f"drill 报告解析失败: {e}",
        }

    stages = report.get("stages", {})
    missing_stages = set(DRILL_STAGES) - set(stages.keys())
    if missing_stages:
        return {
            "valid": False,
            "reason": f"drill 报告缺少阶段: {sorted(missing_stages)}",
            "report": report,
        }

    if not report.get("drill_passed"):
        return {
            "valid": False,
            "reason": f"drill 未通过(failures: {report.get('failures', [])})",
            "report": report,
        }

    if report.get("dry_run"):
        return {
            "valid": False,
            "reason": "drill 报告为 dry-run 模式,不能作为生产 promotion 证据",
            "report": report,
        }

    return {
        "valid": True,
        "reason": "drill 报告验证通过:6 阶段全部成功,非 dry-run",
        "report": report,
    }


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def _load_json_file(path: str) -> dict[str, Any]:
    """加载 JSON 文件,失败抛 argparse.ArgumentTypeError。"""
    p = Path(path)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"文件不存在: {path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"JSON 解析失败: {e}") from e


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rc_tag_drill",
        description="R67 Wave 5: RC tag 正式演练 — signed annotated tag → "
                    "tag workflow → env approval → production evidence → "
                    "digest-pinned deploy → rollback",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # drill 子命令
    drill = sub.add_parser("drill", help="运行完整 RC tag 演练")
    drill.add_argument("--tag", required=True, help="RC tag 名称(如 v1.0.0-rc1)")
    drill.add_argument("--environment-id", required=True, help="部署环境 ID")
    drill.add_argument("--deploy-ref", required=True,
                       help="部署引用(含 @sha256: digest)")
    drill.add_argument("--approval-record", required=True,
                       help="环境审批记录 JSON 文件路径")
    drill.add_argument("--evidence-path", required=True,
                       help="production evidence JSON 文件路径")
    drill.add_argument("--release-manifest-digest", required=True,
                       help="release manifest 中的 image digest")
    drill.add_argument("--executed-by", required=True,
                       help="执行部署的用户/服务账号")
    drill.add_argument("--attestation-subject-digest", default="",
                       help="attestation subject digest(可选)")
    drill.add_argument("--verify-only-3x-digest", default="",
                       help="verify-only-3x 记录中的 digest(可选)")
    drill.add_argument("--rollback-target-path", default=None,
                       help="rollback target JSON 文件路径(可选)")
    drill.add_argument("--repo", default=None,
                       help="GitHub 仓库(owner/repo,可选)")
    drill.add_argument("--workflow-name", default="release-gates.yml",
                       help="workflow 文件名(默认 release-gates.yml)")
    drill.add_argument("--dry-run", action="store_true",
                       help="dry-run 模式(不调用真实 gh/git/gpg)")
    drill.add_argument("--output-report", default=None,
                       help="将 drill 报告写入 JSON 文件(可选)")

    # verify 子命令
    verify = sub.add_parser("verify", help="验证已完成 drill 的报告")
    verify.add_argument("--report", required=True,
                        help="drill 报告 JSON 文件路径")

    # rollback-check 子命令
    rb = sub.add_parser("rollback-check", help="仅检查 rollback 能力")
    rb.add_argument("--environment-id", required=True, help="部署环境 ID")
    rb.add_argument("--rollback-target-path", default=None,
                    help="rollback target JSON 文件路径(可选)")
    rb.add_argument("--dry-run", action="store_true",
                    help="dry-run 模式")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "drill":
        approval_record = _load_json_file(args.approval_record)
        report = run_drill(
            tag=args.tag,
            environment_id=args.environment_id,
            deploy_ref=args.deploy_ref,
            approval_record=approval_record,
            evidence_path=args.evidence_path,
            release_manifest_digest=args.release_manifest_digest,
            executed_by=args.executed_by,
            attestation_subject_digest=args.attestation_subject_digest,
            verify_only_3x_digest=args.verify_only_3x_digest,
            rollback_target_path=args.rollback_target_path,
            repo=args.repo,
            dry_run=args.dry_run,
            workflow_name=args.workflow_name,
        )

        # 输出报告
        print(json.dumps(report, indent=2, ensure_ascii=False))

        # 写入文件(若指定)
        if args.output_report:
            Path(args.output_report).write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return 0 if report["drill_passed"] else 1

    elif args.command == "verify":
        result = verify_drill_report(args.report)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["valid"] else 1

    elif args.command == "rollback-check":
        result = verify_rollback_capability(
            environment_id=args.environment_id,
            rollback_target_path=args.rollback_target_path,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["passed"] else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
