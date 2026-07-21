#!/usr/bin/env python3
"""R65 P1-12: 分支保护 required contexts 与 workflow job 名动态一致性校验。

审计背景(R65 终审报告 P1-12):
    分支保护已通过,但需与 Required contexts 动态一致。
    本轮改善成立。仍需确保 ruleset context 名与实际 job name 双向一致、
    禁止 admin bypass、dismiss stale approvals、required conversation
    resolution、signed commit、独立 reviewer。

整改:
    1. 从 ``.github/workflows/*.yml`` 解析所有 workflow 及其 job 名(含矩阵展开),
       生成"期望 required context"集合。
    2. 与 branch protection ruleset 的 ``required_status_checks.contexts`` 双向比对:
       - 孤儿 context(在 ruleset 但不在 workflow)→ 失败
       - 缺失 context(在 workflow 但不在 ruleset)→ 失败
    3. 任何不一致即 ``exit 1`` 并打印清晰 diff。

运行模式:
    1. **config-file 模式(默认,CI 友好)**: 从 checked-in JSON 文件读取 BP 配置,
       适用于 CI 无 GitHub API 访问权限的场景。
       示例: ``python scripts/check_branch_protection_contexts.py --bp-config .github/branch_protection.json``
    2. **GitHub API 模式**: 通过 ``gh`` CLI 或 token 实时拉取 BP 配置。
       示例: ``python scripts/check_branch_protection_contexts.py --owner maxiuquan --repo tgjiema``

context 名称格式兼容:
    GitHub Actions check-run 的 ``name`` 字段就是 BP 中的 context。
    旧版本误以为是 ``{workflow_name} / {job_name}``,实际就是 ``{job_name}`` 本身
    (矩阵 job 会带 `` (3.11)`` 后缀)。本脚本同时兼容两种格式:
      - BP context 形如 ``"CI / test (3.11)"`` → 归一化为 ``test (3.11)``
      - BP context 形如 ``"test (3.11)"`` → 直接保留

退出码:
    0 — 所有 contexts 与 workflow job 名一致
    1 — 存在孤儿/缺失 context,或 BP 配置无法读取
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — 运行时再提示
    yaml = None


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEFAULT_BP_CONFIG = REPO_ROOT / ".github" / "branch_protection.expected.json"

# R65 P1-12: 自排除 context 列表 — 这些 job 不能作为 BP required context
# (循环依赖:BP 不能要求自身,否则 PR 永久阻塞)。
# verify-branch-protection job 验证 BP 配置,若 BP 要求此 context 通过才能合并,
# 则此 job 本身的 PR 永远无法合并(它必须运行才能验证 BP,但 BP 又要求它先通过)。
SELF_EXCLUDED_CONTEXTS: frozenset[str] = frozenset({
    "verify-branch-protection",
})

# R65 P1-12: 非阻断 context 列表 — 这些 job 在 workflow 中运行,但**设计上**
# 不作为 BP required context(失败不阻断 PR 合并)。
# - production-evidence: 证据生成 job(R64 P1-12: 完整证据是 release 晋级条件,
#   而非 PR 条件;PR 上仅做快速 dry-run 验证,失败不阻断 PR 合并)。
NON_BLOCKING_CONTEXTS: frozenset[str] = frozenset({
    "production-evidence",
})


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class WorkflowInfo:
    """单个 workflow 文件解析结果。"""

    file_name: str  # ci.yml / release-gates.yml / ...
    workflow_name: str  # CI / Release Gates / ...
    jobs: list[str] = field(default_factory=list)
    # push-only job(``if: github.event_name == 'push'``)在 PR 场景不会产生
    # check-run,因此**不应**作为 BP required context。这些 job 被排除出
    # 期望集合,但保留在 excluded_jobs 中供诊断。
    excluded_jobs: list[str] = field(default_factory=list)
    # R65 P1-12: 自排除 job(BP 不能要求自身,循环依赖)
    self_excluded_jobs: list[str] = field(default_factory=list)
    # R65 P1-12: 非阻断 job(设计上不作为 BP required context,失败不阻断 PR)
    non_blocking_jobs: list[str] = field(default_factory=list)

    @property
    def known_workflow_prefixes(self) -> set[str]:
        """workflow_name 以及文件名(去 .yml 后缀)作为 context 前缀候选。"""
        prefixes = {self.workflow_name}
        prefixes.add(self.file_name.removesuffix(".yml").removesuffix(".yaml"))
        return prefixes


@dataclass
class ConsistencyReport:
    """一致性比对结果。"""

    workflow_jobs: set[str] = field(default_factory=set)
    bp_contexts: set[str] = field(default_factory=set)
    orphan_in_bp: set[str] = field(default_factory=set)  # BP 有 / workflow 无
    missing_in_bp: set[str] = field(default_factory=set)  # workflow 有 / BP 无
    matched: set[str] = field(default_factory=set)

    @property
    def is_consistent(self) -> bool:
        return not self.orphan_in_bp and not self.missing_in_bp


# ════════════════════════════════════════════════════════════════
# 1. 从 workflow YAML 提取 job 名
# ════════════════════════════════════════════════════════════════


def _is_push_only_job(job_def: dict[str, Any]) -> bool:
    """检测 job 是否在 PR 场景下不会产生 check-run(不应作为 BP required context)。

    这类 job 在 PR 场景下不会产生 check-run,因此**不应**作为 BP required
    context(否则 PR 会被永久阻塞)。识别启发式覆盖两类:

    1. **push-only job**: ``if`` 字段包含 ``github.event_name == 'push'``
       (或 ``!= 'pull_request'``),且不显式允许 ``pull_request``。
       例: ``sign-image`` / ``publish-attestation``。

    2. **tag-only job**: ``if`` 字段引用 ``refs/tags/`` 或
       ``github.ref_type == 'tag'``,且不显式允许 ``pull_request``。
       例: ``production-promotion-gate``(``if: startsWith(github.ref,
       'refs/tags/v')``)— 仅 release tag (v*.*.*) 触发,PR 场景自动 skipped。

    若 ``if`` 同时显式允许 ``pull_request``(双向兼容),则不算 push-only/tag-only。
    """
    if_value = job_def.get("if")
    if not if_value or not isinstance(if_value, str):
        return False
    if_val = if_value
    # push-only 条件
    has_push_cond = (
        "github.event_name == 'push'" in if_val
        or 'github.event_name == "push"' in if_val
        or "github.event_name != 'pull_request'" in if_val
        or 'github.event_name != "pull_request"' in if_val
    )
    # tag-only 条件(release tag 触发的 job 在 PR 场景也不会产生 check-run)
    has_tag_cond = (
        "refs/tags/" in if_val
        or "github.ref_type == 'tag'" in if_val
        or 'github.ref_type == "tag"' in if_val
    )
    # 同时不能显式允许 pull_request(否则双向兼容,不算 push-only)
    allows_pr = (
        "github.event_name == 'pull_request'" in if_val
        or 'github.event_name == "pull_request"' in if_val
    )
    return (has_push_cond or has_tag_cond) and not allows_pr


def _expand_matrix_jobs(job_id: str, job_def: dict[str, Any]) -> list[str]:
    """展开矩阵 job 为具体的 check-run 名称列表。

    GitHub Actions check-run ``name`` 字段:
      - 普通 job: ``job_id``(如 ``lint`` / ``repo-hygiene``)
      - 矩阵 job: ``job_id (matrix_label)``
        (如 ``test (3.11)`` — matrix.python-version == "3.11")

    矩阵 label 格式取决于 matrix 值:
      - 字符串: ``(value)`` (如 ``(3.11)``)
      - 多维矩阵: ``(v1, v2)`` (如 ``(3.11, linux)``)
      - include 项: 按 include 的具体值生成
    """
    strategy = job_def.get("strategy") or {}
    matrix = strategy.get("matrix")
    if not matrix:
        return [job_id]

    # 收集所有维度名(排除 include/exclude 等保留键)
    reserved_keys = {"include", "exclude"}
    dimension_keys = [k for k in matrix.keys() if k not in reserved_keys]
    dimensions: list[list[str]] = []
    for key in dimension_keys:
        values = matrix[key]
        if not isinstance(values, list):
            continue
        dimensions.append([(key, str(v)) for v in values])

    if not dimensions:
        # 仅含 include,无显式维度 → 至少返回 job_id
        return [job_id]

    # 笛卡尔积展开
    expanded: list[str] = []
    _cartesian_expand(job_id, dimensions, 0, [], expanded)

    # 处理 include(在笛卡尔积之上追加额外组合)
    for inc in matrix.get("include") or []:
        label_parts = []
        for key in dimension_keys:
            if key in inc:
                label_parts.append(str(inc[key]))
        label = ", ".join(label_parts) if label_parts else ""
        expanded.append(f"{job_id} ({label})" if label else job_id)

    # 处理 exclude(从 expanded 中移除匹配项)
    for exc in matrix.get("exclude") or []:
        label_parts = []
        for key in dimension_keys:
            if key in exc:
                label_parts.append(str(exc[key]))
        label = ", ".join(label_parts) if label_parts else ""
        candidate = f"{job_id} ({label})" if label else job_id
        if candidate in expanded:
            expanded.remove(candidate)

    return expanded or [job_id]


def _cartesian_expand(
    job_id: str,
    dimensions: list[list[tuple[str, str]]],
    depth: int,
    current: list[str],
    out: list[str],
) -> None:
    """递归执行笛卡尔积,生成 ``job_id (v1, v2, ...)`` 形式的 check-run 名。"""
    if depth == len(dimensions):
        label = ", ".join(current)
        out.append(f"{job_id} ({label})" if label else job_id)
        return
    for _key, value in dimensions[depth]:
        current.append(value)
        _cartesian_expand(job_id, dimensions, depth + 1, current, out)
        current.pop()


def parse_workflow_file(
    path: Path,
    include_all_jobs: bool = False,
) -> WorkflowInfo:
    """解析单个 workflow YAML 文件,返回 WorkflowInfo。

    Args:
        path: workflow YAML 文件路径
        include_all_jobs: 若为 True,禁用 R65 P1-12 的过滤逻辑
            (push-only / tag-only / self-excluded / non-blocking),
            所有 job 都加入 ``jobs`` 集合(用于 R71 P1-02 校验)。
            R71 P1-02 要求: required_status_checks 必须覆盖所有真实
            release-gates.yml job 名,不做过滤。
            默认 False (R65 P1-12 行为,向后兼容)。
    """
    if yaml is None:
        raise RuntimeError(
            "PyYAML 未安装 — check_branch_protection_contexts.py 需要 PyYAML "
            "解析 workflow YAML。请执行: pip install pyyaml"
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    workflow_name = str(data.get("name", path.stem))
    jobs_dict = data.get("jobs") or {}

    jobs: list[str] = []
    excluded_jobs: list[str] = []
    self_excluded_jobs: list[str] = []
    non_blocking_jobs: list[str] = []
    for job_id, job_def in jobs_dict.items():
        if not isinstance(job_def, dict):
            jobs.append(str(job_id))
            continue
        expanded = _expand_matrix_jobs(str(job_id), job_def)
        # R71 P1-02 模式 (include_all_jobs=True): 跳过所有过滤,
        # 所有 workflow job 都加入 jobs 集合(用于校验 BP 包含全部
        # release-gates.yml job 名)。
        if include_all_jobs:
            jobs.extend(expanded)
            continue
        # R65 P1-12: 自排除 job(BP 不能要求自身,循环依赖)
        # verify-branch-protection 验证 BP 配置,若 BP 要求此 context 通过
        # 才能合并,则此 job 的 PR 永远无法合并(循环依赖)。
        if any(e in SELF_EXCLUDED_CONTEXTS for e in expanded):
            self_excluded_jobs.extend(expanded)
            continue
        # R65 P1-12: 非阻断 job(设计上不作为 BP required context)
        # production-evidence 是证据生成 job,失败不阻断 PR 合并(R64 P1-12)。
        if any(e in NON_BLOCKING_CONTEXTS for e in expanded):
            non_blocking_jobs.extend(expanded)
            continue
        # push-only job(``if: github.event_name == 'push'``)在 PR 场景不会
        # 产生 check-run,因此**不应**作为 BP required context(否则 PR
        # 永久阻塞)。这些 job 被排除出期望集合,但仍记录供诊断。
        if _is_push_only_job(job_def):
            excluded_jobs.extend(expanded)
            continue
        jobs.extend(expanded)

    return WorkflowInfo(
        file_name=path.name,
        workflow_name=workflow_name,
        jobs=jobs,
        excluded_jobs=excluded_jobs,
        self_excluded_jobs=self_excluded_jobs,
        non_blocking_jobs=non_blocking_jobs,
    )


def parse_workflows_dir(
    workflows_dir: Path,
    include_all_jobs: bool = False,
    workflow_files: list[str] | None = None,
) -> list[WorkflowInfo]:
    """解析 workflows 目录下的 .yml/.yaml 文件。

    Args:
        workflows_dir: workflows 目录路径
        include_all_jobs: 透传给 parse_workflow_file (R71 P1-02 模式)
        workflow_files: 若指定,只解析文件名匹配的 workflow 文件
            (如 ``["release-gates.yml"]`` 只解析 release-gates.yml)。
            默认 None — 解析目录下所有 .yml/.yaml 文件。
    """
    if not workflows_dir.exists():
        raise FileNotFoundError(f"workflows 目录不存在: {workflows_dir}")

    workflows: list[WorkflowInfo] = []
    if workflow_files:
        # 仅解析指定的 workflow 文件 (R71 P1-02: 只校验 release-gates.yml)
        for fname in workflow_files:
            for ext in (".yml", ".yaml", ""):
                candidate = workflows_dir / (fname if fname.endswith(ext) else fname + ext)
                if candidate.exists():
                    workflows.append(
                        parse_workflow_file(candidate, include_all_jobs=include_all_jobs)
                    )
                    break
        return workflows

    for path in sorted(workflows_dir.glob("*.yml")):
        workflows.append(parse_workflow_file(path, include_all_jobs=include_all_jobs))
    for path in sorted(workflows_dir.glob("*.yaml")):
        workflows.append(parse_workflow_file(path, include_all_jobs=include_all_jobs))
    return workflows


def collect_expected_jobs(workflows: list[WorkflowInfo]) -> set[str]:
    """汇总所有 workflow 的 job 名集合(用于与 BP contexts 比对)。"""
    expected: set[str] = set()
    for wf in workflows:
        expected.update(wf.jobs)
    return expected


# ════════════════════════════════════════════════════════════════
# 2. context 名归一化(兼容 "{workflow} / {job}" 与 "{job}" 两种格式)
# ════════════════════════════════════════════════════════════════


def normalize_context(context: str, known_workflow_prefixes: set[str]) -> str:
    """把 BP context 归一化为 job 名。

    GitHub Actions 的 check-run name 通常是 ``{job_id}`` 或 ``{job_id} ({matrix})``,
    但有些仓库会在 BP 中配置为 ``{workflow_name} / {job_id}`` 形式。
    本函数:
      - 若 context 以 ``"{prefix} / "`` 开头(prefix 是已知 workflow name 或文件名),
        则去掉前缀,返回 ``{job_id}`` 部分。
      - 否则原样返回。
    """
    for prefix in known_workflow_prefixes:
        sep = f"{prefix} / "
        if context.startswith(sep):
            return context[len(sep):]
    return context


# ════════════════════════════════════════════════════════════════
# 3. 比对 workflow jobs 与 BP contexts
# ════════════════════════════════════════════════════════════════


def compare_contexts(
    workflows: list[WorkflowInfo],
    bp_contexts: list[str],
) -> ConsistencyReport:
    """双向比对 workflow jobs 与 BP contexts。

    规则:
      - 每个 BP context 归一化后必须能在 workflow jobs 中找到(否则为孤儿)
      - 每个 workflow job 必须在 BP contexts 中出现(直接或带前缀)(否则为缺失)
    """
    all_workflow_prefixes: set[str] = set()
    for wf in workflows:
        all_workflow_prefixes.update(wf.known_workflow_prefixes)

    expected_jobs = collect_expected_jobs(workflows)

    # 归一化 BP contexts
    normalized_bp: set[str] = set()
    raw_to_normalized: dict[str, str] = {}
    for ctx in bp_contexts:
        norm = normalize_context(ctx, all_workflow_prefixes)
        normalized_bp.add(norm)
        raw_to_normalized[ctx] = norm

    report = ConsistencyReport(
        workflow_jobs=expected_jobs,
        bp_contexts=set(bp_contexts),
    )

    # 孤儿: BP 中有,workflow 中无
    for ctx_raw, norm in raw_to_normalized.items():
        if norm in expected_jobs:
            report.matched.add(ctx_raw)
        else:
            report.orphan_in_bp.add(ctx_raw)

    # 缺失: workflow 中有,BP 中无(无论以哪种格式)
    for job in expected_jobs:
        if job in normalized_bp:
            continue  # 直接匹配
        # 检查是否有 BP context 归一化后等于此 job(已在上面覆盖)
        # 若仍找不到,标记为缺失
        report.missing_in_bp.add(job)

    return report


# ════════════════════════════════════════════════════════════════
# 4. 读取 BP 配置(config-file 模式 / GitHub API 模式)
# ════════════════════════════════════════════════════════════════


def read_bp_from_config_file(path: Path) -> dict[str, Any]:
    """从 checked-in JSON 文件读取 BP 配置。

    支持两种 schema:
      1. 完整 GitHub API 响应:
         ``{"required_status_checks": {"contexts": [...]}, ...}``
      2. 精简 schema:
         ``{"contexts": [...]}`` 或 ``{"required_status_checks": {"contexts": [...]}}``
    """
    if not path.exists():
        raise FileNotFoundError(f"BP 配置文件不存在: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data


def read_bp_from_github_api(
    owner: str, repo: str, branch: str = "master"
) -> dict[str, Any]:
    """通过 gh CLI 拉取 BP 配置(需 gh 已登录或 GH_TOKEN 环境变量)。"""
    cmd = [
        "gh", "api",
        f"repos/{owner}/{repo}/branches/{branch}/protection",
    ]
    env = os.environ.copy()
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gh CLI 未安装或不在 PATH 中 — 请安装 gh 或改用 --bp-config 模式"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"gh api 调用失败 (exit={exc.returncode}): "
            f"{exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc
    return json.loads(result.stdout)


def extract_bp_contexts(bp_data: dict[str, Any]) -> list[str]:
    """从 BP 配置中提取 required_status_checks.contexts。"""
    rsc = bp_data.get("required_status_checks") or {}
    contexts = rsc.get("contexts") or []
    if not isinstance(contexts, list):
        return []
    return [str(c) for c in contexts]


# ════════════════════════════════════════════════════════════════
# 5. 报告输出
# ════════════════════════════════════════════════════════════════


def format_diff(report: ConsistencyReport, workflows: list[WorkflowInfo]) -> str:
    """打印清晰的 diff 报告。"""
    lines: list[str] = []
    lines.append("════════════════════════════════════════════════════════════════")
    lines.append("R65 P1-12: 分支保护 contexts 动态一致性检查")
    lines.append("════════════════════════════════════════════════════════════════")
    lines.append("")

    # workflow 概览
    lines.append("─── Workflow job 概览 ───")
    for wf in workflows:
        lines.append(f"  [{wf.file_name}] name={wf.workflow_name}")
        for job in sorted(wf.jobs):
            lines.append(f"    - {job}")
        if wf.excluded_jobs:
            lines.append(f"    (push-only job 已排除,不作为 required context:)")
            for job in sorted(wf.excluded_jobs):
                lines.append(f"      ~ {job}")
        if wf.self_excluded_jobs:
            lines.append(f"    (自排除 job — BP 循环依赖,不能作为 required context:)")
            for job in sorted(wf.self_excluded_jobs):
                lines.append(f"      x {job}")
        if wf.non_blocking_jobs:
            lines.append(f"    (非阻断 job — 设计上不作为 required context,失败不阻断 PR:)")
            for job in sorted(wf.non_blocking_jobs):
                lines.append(f"      n {job}")
    lines.append("")

    # BP contexts
    lines.append("─── Branch Protection required_status_checks.contexts ───")
    for ctx in sorted(report.bp_contexts):
        lines.append(f"  - {ctx}")
    lines.append("")

    # matched
    lines.append(f"─── 匹配的 context ({len(report.matched)} 个) ───")
    for ctx in sorted(report.matched):
        lines.append(f"  ✓ {ctx}")
    lines.append("")

    # orphan
    lines.append(f"─── 孤儿 context(在 BP 但不在 workflow,共 {len(report.orphan_in_bp)} 个)───")
    if report.orphan_in_bp:
        for ctx in sorted(report.orphan_in_bp):
            lines.append(f"  ✗ {ctx}")
    else:
        lines.append("  (无)")
    lines.append("")

    # missing
    lines.append(f"─── 缺失 context(在 workflow 但不在 BP,共 {len(report.missing_in_bp)} 个)───")
    if report.missing_in_bp:
        for job in sorted(report.missing_in_bp):
            lines.append(f"  ✗ {job}")
    else:
        lines.append("  (无)")
    lines.append("")

    # 最终结论
    if report.is_consistent:
        lines.append("✓ PASS: BP required contexts 与 workflow job 名完全一致")
    else:
        lines.append("✗ FAIL: BP required contexts 与 workflow job 名不一致")
        lines.append("")
        lines.append("修复建议:")
        lines.append("  1. 自动检测当前实际 check-runs 并配置 BP:")
        lines.append("       bash scripts/detect_branch_protection_contexts.sh > contexts.json")
        lines.append("       bash scripts/configure_branch_protection.sh")
        lines.append("  2. 或更新 checked-in BP 配置文件后重跑本脚本:")
        lines.append("       python scripts/check_branch_protection_contexts.py --bp-config <file>")
    lines.append("════════════════════════════════════════════════════════════════")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 6. CLI 入口
# ════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_branch_protection_contexts.py",
        description=(
            "R65 P1-12: 校验 GitHub Branch Protection 的 required contexts "
            "与 .github/workflows/*.yml 中的 job 名动态一致。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  # config-file 模式(CI 友好,无需 GitHub API)\n"
            "  python scripts/check_branch_protection_contexts.py \\\n"
            "      --bp-config .github/branch_protection.expected.json\n\n"
            "  # GitHub API 模式(需 gh CLI 已登录)\n"
            "  python scripts/check_branch_protection_contexts.py \\\n"
            "      --owner maxiuquan --repo tgjiema --branch master\n\n"
            "  # 自定义 workflows 目录\n"
            "  python scripts/check_branch_protection_contexts.py \\\n"
            "      --bp-config <file> --workflows-dir .github/workflows\n"
        ),
    )
    parser.add_argument(
        "--bp-config",
        type=Path,
        default=None,
        help=(
            "BP 配置 JSON 文件路径(config-file 模式)。"
            "支持完整 GitHub API 响应 schema 或精简 {\"contexts\": [...]} schema。"
            "未指定 --owner/--repo 时,默认读取 .github/branch_protection.expected.json。"
        ),
    )
    parser.add_argument(
        "--owner",
        type=str,
        default=None,
        help="GitHub 仓库 owner(API 模式,需配合 --repo 使用)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="GitHub 仓库名(API 模式,需配合 --owner 使用)",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default="master",
        help="受保护分支名(默认 master)",
    )
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=DEFAULT_WORKFLOWS_DIR,
        help=f"workflow YAML 目录(默认 {DEFAULT_WORKFLOWS_DIR})",
    )
    parser.add_argument(
        "--workflow-files",
        type=str,
        default=None,
        help=(
            "逗号分隔的 workflow 文件名列表,只解析这些文件(如 "
            "'release-gates.yml')。默认 None — 解析目录下所有 .yml/.yaml。"
            "R71 P1-02: 校验 BP contexts 时应限定 release-gates.yml,"
            "避免其他 workflow(如 promote-rc.yml)的 job 被误判为 missing。"
        ),
    )
    parser.add_argument(
        "--include-all-jobs",
        action="store_true",
        help=(
            "R71 P1-02 模式: 禁用 R65 P1-12 的过滤逻辑"
            "(push-only / tag-only / self-excluded / non-blocking)。"
            "所有 workflow job 都加入期望集合(用于校验 BP 包含全部 "
            "release-gates.yml job 名)。默认 False — 保持 R65 P1-12 行为。"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出报告(便于 CI 解析)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # R71 P1-02 模式: --workflow-files 逗号分隔 → list
    workflow_files: list[str] | None = None
    if args.workflow_files:
        workflow_files = [s.strip() for s in args.workflow_files.split(",") if s.strip()]

    # 解析 workflows
    try:
        workflows = parse_workflows_dir(
            args.workflows_dir,
            include_all_jobs=args.include_all_jobs,
            workflow_files=workflow_files,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: 解析 workflows 失败: {exc}", file=sys.stderr)
        return 1

    if not workflows:
        print(f"ERROR: workflows 目录无 .yml/.yaml 文件: {args.workflows_dir}", file=sys.stderr)
        return 1

    # 读取 BP 配置 — 优先 API 模式,否则 config-file 模式
    bp_data: dict[str, Any] | None = None
    if args.owner and args.repo:
        try:
            bp_data = read_bp_from_github_api(args.owner, args.repo, args.branch)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: 通过 GitHub API 读取 BP 配置失败: {exc}", file=sys.stderr)
            print(
                "  提示: 可改用 config-file 模式: "
                "python scripts/check_branch_protection_contexts.py --bp-config <file>",
                file=sys.stderr,
            )
            return 1
    else:
        bp_config_path = args.bp_config or DEFAULT_BP_CONFIG
        try:
            bp_data = read_bp_from_config_file(bp_config_path)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(
                "  提示: 未指定 --owner/--repo 时,默认读取 checked-in BP 配置: \n"
                f"    {DEFAULT_BP_CONFIG}\n"
                "  请创建该文件,或通过 --bp-config 指定其他路径,或通过 --owner/--repo "
                "切换到 GitHub API 模式。",
                file=sys.stderr,
            )
            return 1
        except json.JSONDecodeError as exc:
            print(f"ERROR: BP 配置 JSON 解析失败: {exc}", file=sys.stderr)
            return 1

    bp_contexts = extract_bp_contexts(bp_data)
    if not bp_contexts:
        print(
            "ERROR: BP 配置中未找到 required_status_checks.contexts,或 contexts 为空",
            file=sys.stderr,
        )
        return 1

    # 比对
    report = compare_contexts(workflows, bp_contexts)

    # 输出
    if args.json:
        report_json = {
            "consistent": report.is_consistent,
            "workflow_count": len(workflows),
            "workflows": [
                {
                    "file": wf.file_name,
                    "name": wf.workflow_name,
                    "jobs": sorted(wf.jobs),
                    "excluded_push_only_jobs": sorted(wf.excluded_jobs),
                    "self_excluded_jobs": sorted(wf.self_excluded_jobs),
                    "non_blocking_jobs": sorted(wf.non_blocking_jobs),
                }
                for wf in workflows
            ],
            "bp_contexts": sorted(report.bp_contexts),
            "matched": sorted(report.matched),
            "orphan_in_bp": sorted(report.orphan_in_bp),
            "missing_in_bp": sorted(report.missing_in_bp),
        }
        print(json.dumps(report_json, ensure_ascii=False, indent=2))
    else:
        print(format_diff(report, workflows))

    return 0 if report.is_consistent else 1


if __name__ == "__main__":
    sys.exit(main())
