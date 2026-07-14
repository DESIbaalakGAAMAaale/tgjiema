#!/usr/bin/env bash
# R48 P0-2: 检测当前仓库实际可用的 status check context 名称
#
# 背景:
#   R48 终审报告指出:GitHub Branch Protection 的 required_status_checks.contexts
#   必须匹配实际 check run 名称。实际名称通常是 "{workflow_name} / {job_name}",
#   而不是简单的 "CI"/"E2E"。硬编码会导致 BP 永久阻断。
#
# 用途:
#   读取最新 master commit 的 check-runs,提取实际 status check context 名称,
#   去重排序后输出 JSON 数组,供 configure_branch_protection.sh 使用。
#
# 用法:
#   # 1) 自动从 git remote 推断 owner/repo(最常用)
#   bash scripts/detect_branch_protection_contexts.sh > contexts.json
#   # 2) 通过位置参数指定
#   bash scripts/detect_branch_protection_contexts.sh owner repo > contexts.json
#   # 3) 通过环境变量指定
#   OWNER=owner REPO=repo bash scripts/detect_branch_protection_contexts.sh > contexts.json
#
# 输出:
#   stdout: JSON 数组,如 ["CI / lint","CI / test (3.11)","Deploy Check / verify-deploy",...]
#   stderr: 人类可读的诊断日志
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
#   - 或 gh CLI 已登录(gh auth login)
#
# 退出码:
#   0 — 成功输出 JSON
#   1 — 鉴权失败 / owner-repo 无法确定 / API 调用失败 / 无 check-runs
set -euo pipefail

# ─── 1. 解析 OWNER / REPO(环境变量优先,其次位置参数,最后 git remote 推断) ───
OWNER="${OWNER:-${1:-}}"
REPO="${REPO:-${2:-}}"

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  # 从 git remote origin 推断 owner/repo
  REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
  if [ -n "$REMOTE_URL" ]; then
    # 兼容 git@github.com:owner/repo.git 与 https://github.com/owner/repo.git
    PARSED=$(echo "$REMOTE_URL" \
      | sed -E 's#.*github\.com[:/]([^/]+)/([^/]+)(\.git)?$#\1 \2#' || true)
    if [ -n "$PARSED" ] && [ "$(echo "$PARSED" | wc -w)" -eq 2 ]; then
      OWNER="${OWNER:-$(echo "$PARSED" | cut -d' ' -f1)}"
      REPO="${REPO:-$(echo "$PARSED" | cut -d' ' -f2)}"
    fi
  fi
fi

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  echo "ERROR: 无法确定 OWNER / REPO" >&2
  echo "  用法 1: OWNER=owner REPO=repo $0" >&2
  echo "  用法 2: $0 owner repo" >&2
  echo "  或确保 git remote origin 指向 GitHub 仓库" >&2
  exit 1
fi

# ─── 2. 鉴权(token 优先,否则用 gh CLI 已登录的凭证) ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login" >&2
    exit 1
  fi
fi

# ─── 3. 工具函数:调用 GitHub API(失败立即退出,不吞错误) ───
call_gh_api() {
  local path="$1"
  if [ "$USE_GH_CLI" = "true" ]; then
    gh api "repos/${OWNER}/${REPO}${path}"
  else
    local resp
    resp=$(curl -sS -f \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${OWNER}/${REPO}${path}")
    echo "$resp"
  fi
}

# ─── 4. 获取 master 分支最新 commit SHA ───
echo "[INFO] 获取 ${OWNER}/${REPO} master 分支最新 commit..." >&2
LATEST_SHA=$(call_gh_api "/commits/master" | jq -r '.sha')

if [ -z "$LATEST_SHA" ] || [ "$LATEST_SHA" = "null" ]; then
  echo "ERROR: 无法获取 master 最新 commit SHA(可能 master 分支不存在或鉴权失败)" >&2
  exit 1
fi
echo "[INFO] 最新 master commit: ${LATEST_SHA}" >&2

# ─── 5. 拉取该 commit 的 check-runs ───
echo "[INFO] 拉取 check-runs..." >&2
CHECK_RUNS_JSON=$(call_gh_api "/commits/${LATEST_SHA}/check-runs")

TOTAL=$(echo "$CHECK_RUNS_JSON" | jq -r '.total_count // 0')
echo "[INFO] 共 ${TOTAL} 个 check-runs" >&2

if [ "$TOTAL" -eq 0 ]; then
  echo "ERROR: 该 commit 没有 check-runs" >&2
  echo "  请先在 master 分支触发至少一次 CI / Deploy Check / Release Gates / E2E workflow," >&2
  echo "  然后再运行本脚本。" >&2
  exit 1
fi

# ─── 6. 提取实际 check context 名称(check run 的 name 字段就是 BP context) ───
# 实际名称格式:
#   - "{workflow_name} / {job_name}"
#   - "{workflow_name} / {job_name} ({matrix_label})"  (矩阵 job)
# 去重 + 排序,输出 JSON 数组到 stdout
CONTEXTS_JSON=$(echo "$CHECK_RUNS_JSON" \
  | jq -r '.check_runs[].name' \
  | sort -u \
  | jq -R . | jq -s .)

CONTEXTS_COUNT=$(echo "$CONTEXTS_JSON" | jq 'length')
echo "[INFO] 检测到 ${CONTEXTS_COUNT} 个唯一 context:" >&2
echo "$CONTEXTS_JSON" | jq -r '.[]' | sed 's/^/  - /' >&2

# ─── 7. 校验四个核心 workflow 至少各有一个 context 覆盖 ───
# R48 P0-2 要求 BP 必须覆盖 CI / Deploy Check / Release Gates / E2E Tests 四个 workflow
MISSING_COVERAGE=()
for prefix in "CI /" "Deploy Check /" "Release Gates /" "E2E Tests /"; do
  if ! echo "$CONTEXTS_JSON" | jq -e --arg p "$prefix" \
        'any(.[]; startswith($p))' > /dev/null; then
    MISSING_COVERAGE+=("$prefix")
  fi
done

if [ "${#MISSING_COVERAGE[@]}" -gt 0 ]; then
  echo "WARN: 以下 workflow 前缀在 check-runs 中未找到:" >&2
  for p in "${MISSING_COVERAGE[@]}"; do
    echo "  - $p" >&2
  done
  echo "  可能该 workflow 尚未在 master 上触发过。" >&2
  echo "  仍输出检测到的 context,但 configure_branch_protection.sh 会要求用户确认。" >&2
fi

# ─── 8. stdout 输出 JSON 数组 ───
echo "$CONTEXTS_JSON"
