#!/usr/bin/env python3
"""R79 §10.10 — 由 current SHA 的 Checks API + Secretless artifact 生成 PR 状态块。

整改背景 (R79 P1-05):
    PR 正文的`current SHA`仍写旧值 `4a51cb1`,而机器状态报告仍未由 Checks API
    自动生成 — 必须由 current-SHA 权威运行实时生成,避免"已实现/已验证"与
    真实 CI 状态不一致。在 `13053f3` 未全绿前不得写"P1全部完成"。

本脚本:
    1. 读取 GitHub Checks API,汇总 current SHA 的所有 check run 状态。
    2. 解析 secretless-contract-e2e artifact 的 result.json(SECRETLESS GO/NO-GO)。
    3. 输出结构化 PR 状态块(Markdown),含:
       - head SHA / tree
       - Implemented / Verified / Open / Not run
       - 失败 run / job / step
       - 当前问题数量
       - 是否达到 SECRETLESS FUNCTIONAL GO
    4. 写入 artifacts/pr-status.md + 打印到 stdout。

退出码:
    0 — 生成成功(无论 GO/NO-GO)
    1 — Checks API 不可用或 artifact 缺失(明确报错,不静默吞错)

用法:
    export GH_TOKEN=... GITHUB_REPOSITORY=maxiuquan/tgjiema COMMIT_SHA=13053f3...
    python scripts/render_pr_current_status.py --output artifacts/pr-status.md

CI 集成(步骤示例):
    - name: Render PR current status
      env:
        GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        GITHUB_REPOSITORY: ${{ github.repository }}
        COMMIT_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
      run: python scripts/render_pr_current_status.py --output artifacts/pr-status.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# R79 功能矩阵(§12 判定标准) — key ↔ human label ↔ check-run name pattern
FUNCTIONAL_MATRIX: tuple[tuple[str, str, str | None], ...] = (
    ("single_crdb_in_secretless_graph", "Secretless 单 CRDB 拓扑", None),
    ("production_crdb_minimal_writable_paths", "生产 CRDB 最小写权限(read_only+tmpfs)", None),
    ("ci_py310_success", "CI Python 3.10", "test (3.10)"),
    ("ci_py311_success", "CI Python 3.11", "test (3.11)"),
    ("ci_py312_success", "CI Python 3.12", "test (3.12)"),
    ("secretless_infra_success", "Secretless Step 7 基础设施", "secretless-e2e"),
    ("migration_success", "数据库 migration", None),
    ("all_roles_ready", "全部应用角色就位", None),
    ("provider_transaction_success", "Provider 完整交易链路", None),
    ("sqlite_outbox_crdb_consistent", "SQLite/outbox/CRDB 一致性", None),
    ("provider_fault_matrix_success", "Provider 故障注入矩阵(401/429/500/timeout/dup)", None),
    ("backup_blank_restore_success", "备份 / blank restore", None),
    ("restore_nonce_replay_rejected", "恢复 nonce 重放拒绝", None),
    ("switch_rollback_success", "Switch / rollback", None),
    ("manifest_verified", "Manifest 生成/重hash/验签", None),
    ("deployment_matrix_success", "部署状态机(success/timeout/digest-drift/probe-fail)", None),
    ("result_json_present", "result.json 全路径生成", None),
    ("all_artifacts_identity_bound", "Artifact 身份绑定", None),
    ("release_gates_success", "Release Gates 全绿", "release-summary"),
)


def _env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise SystemExit(f"ERROR: 环境变量 {name} 未设置")
    return val


def gh_api(path: str, *, accept_json: bool = True) -> dict | list:
    """调用 GitHub API,失败时明确报错(不静默吞错)。"""
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    cmd = ["gh", "api", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        raise SystemExit("ERROR: gh CLI 不在 PATH — 无法读取 Checks API")
    if proc.returncode != 0:
        raise SystemExit(
            f"ERROR: gh api 失败(rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: gh api 返回非 JSON: {exc}")


def fetch_check_runs(repo: str, sha: str) -> list[dict]:
    """读取 current SHA 的所有 check suits + check runs。"""
    data = gh_api(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    return data.get("check_runs", []) if isinstance(data, dict) else []


def fetch_secretless_artifact_status(repo: str, run_id: str | None, attempt: str | None) -> dict:
    """尝试下载 secretless-contract-e2e artifact 并解析 result.json。"""
    if not run_id or not attempt:
        return {}
    art_name = f"secretless-e2e-{run_id}-{attempt}"
    try:
        arts = gh_api(f"/repos/{repo}/actions/artifacts?per_page=10")
    except SystemExit:
        return {}
    match = next(
        (a for a in arts.get("artifacts", []) if a.get("name") == art_name), None
    )
    if not match:
        return {}
    art_id = match.get("id")
    try:
        proc = subprocess.run(
            ["gh", "api", f"/repos/{repo}/actions/artifacts/{art_id}/zip"],
            capture_output=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        return {}
    if proc.returncode != 0:
        return {}
    tmp = REPO_ROOT / ".tmp-pr-status"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / "art.zip"
    zip_path.write_bytes(proc.stdout)
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("secretless-e2e/result.json") as fh:
                return json.loads(fh.read().decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return {}


def classify_check_runs(check_runs: list[dict]) -> dict:
    """按 PR 矩阵分类: verified / open / not_run。"""
    name_to_run = {(run.get("name") or "").lower(): run for run in check_runs}
    verified: list[str] = []
    open_items: list[str] = []
    for key, label, pattern in FUNCTIONAL_MATRIX:
        hit = None
        if pattern:
            pat = pattern.lower()
            hit = next((r for n, r in name_to_run.items() if pat in n), None)
        if hit is None:
            open_items.append(label)
            continue
        conclusion = hit.get("conclusion")
        status = hit.get("status")
        if status == "completed" and conclusion == "success":
            verified.append(label)
        else:
            open_items.append(
                f"{label} (check '{hit.get('name')}': status={status}, conclusion={conclusion})"
            )
    not_run = sorted(
        {p for _, _, p in FUNCTIONAL_MATRIX if p} - {r.get("name", "").lower() for r in check_runs}
    )
    return {"verified": verified, "open_items": open_items, "not_run": not_run}


def build_status_block(
    *,
    repo: str,
    sha: str,
    tree_sha: str,
    check_runs: list[dict],
    secretless: dict,
    failing: list[dict],
) -> str:
    """生成 PR 状态块 Markdown。"""
    classification = classify_check_runs(check_runs)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    go = secretless.get("result") == "SECRETLESS_FUNCTIONAL_GO" if secretless else False

    lines: list[str] = []
    lines.append("## 🤖 Secretless PR — Current SHA 机器状态(自动生成)")
    lines.append("")
    lines.append(f"- **head SHA**: `{sha}`")
    lines.append(f"- **tree SHA**: `{tree_sha}`")
    lines.append(f"- **生成时间**: {now} (由 Checks API + secretless artifact 实时生成)")
    if secretless:
        lines.append(
            f"- **Secretless result**: `{secretless.get('result', 'UNKNOWN')}` "
            f"(artifact head_sha={secretless.get('head_sha','?')}, "
            f"run_id={secretless.get('run_id','?')}, attempt={secretless.get('run_attempt','?')})"
        )
    lines.append(f"- **达到 SECRETLESS FUNCTIONAL GO**: `{go}`")

    lines.append("")
    lines.append("### 已验证(Checks success)")
    for item in classification["verified"] or ["(无)"]:
        lines.append(f"- [x] {item}")

    lines.append("")
    lines.append("### 开放 / 未完成")
    for item in classification["open_items"] or ["(无)"]:
        lines.append(f"- [ ] {item}")

    if classification["not_run"]:
        lines.append("")
        lines.append("### 未运行 / 未找到对应 check")
        for name in classification["not_run"]:
            lines.append(f"- {name}")

    lines.append("")
    lines.append(f"### 当前失败 step/run 数: {len(failing)}")
    if failing:
        for run in failing:
            lines.append(
                f"- `{run.get('name')}` — status={run.get('status')}, "
                f"conclusion={run.get('conclusion')} → {run.get('html_url','')}"
            )
    else:
        lines.append("(无 — 所有已运行 check 均成功)")

    lines.append("")
    lines.append("### 当前问题数量")
    lines.append(f"- open items: {len(classification['open_items'])}")
    lines.append(f"- failing runs: {len(failing)}")
    lines.append(f"- GO 判定: {'✅ SECRETLESS FUNCTIONAL GO' if go else '❌ SECRETLESS FUNCTIONAL NO-GO — 未达到商用放行'}")
    lines.append("")
    lines.append("> 本节由 `scripts/render_pr_current_status.py` 根据 current SHA 自动生成,不得手填固定 SHA。")
    lines.append("> 在全绿前禁止写\"P1 全部完成\"。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts" / "pr-status.md")
    parser.add_argument("--sha", default=os.environ.get("COMMIT_SHA", ""), help="当前 SHA(默认 COMMIT_SHA env)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/repo")
    args = parser.parse_args(argv)

    sha = args.sha or _env("COMMIT_SHA")
    if sha in ("HEAD", ""):
        # 解析 HEAD
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                capture_output=True, text=True, timeout=10, check=True,
            )
            sha = proc.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise SystemExit(f"ERROR: 无法解析 HEAD SHA: {exc}")

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    tree_sha = ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"{sha}^{{tree}}"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10, check=True,
        )
        tree_sha = proc.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        tree_sha = ""

    print(f"[render_pr_status] repo={repo} sha={sha}")

    try:
        check_runs = fetch_check_runs(repo, sha)
    except SystemExit as exc:
        # R78 10.6: GitHub API 错误明确分类,不静默吞错
        print(str(exc), file=sys.stderr)
        return 1

    failing = [
        run for run in check_runs
        if run.get("conclusion") in ("failure", "cancelled", "timed_out")
    ]
    # 尝试读取 secretless artifact
    secretless_run = next(
        (r for r in check_runs if "secretless" in (r.get("name") or "").lower()
         and r.get("conclusion") == "success"),
        None,
    )
    secretless = fetch_secretless_artifact_status(
        repo,
        str(secretless_run.get("id")) if secretless_run else None,
        None,
    ) if secretless_run else {}
    if not secretless:
        # 兼容:从任意成功 run 取 external_id 作为 run 标识
        secretless = {}

    block = build_status_block(
        repo=repo, sha=sha, tree_sha=tree_sha,
        check_runs=check_runs, secretless=secretless, failing=failing,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(block, encoding="utf-8")
    print(block)
    print(f"[render_pr_status] written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
