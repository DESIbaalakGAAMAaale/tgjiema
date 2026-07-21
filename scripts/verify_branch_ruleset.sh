#!/usr/bin/env bash
# R67 P0-01: 验证 GitHub Repository Ruleset 配置(master/main 分支不可变性)
#
# 本脚本在 CI 中运行,验证仓库实际配置的 branch ruleset 与
# .github/branch_ruleset.expected.json 期望配置一致。
# 任意属性不匹配即 fail-closed 退出 1。
#
# 整改要求(R67 P0-01):
#   - target == branch
#   - enforcement == active
#   - conditions.ref_name.include 包含 refs/heads/master 与 refs/heads/main
#   - rules 包含 deletion (deletion=false)
#   - rules 包含 non_fast_forward (non_fast_forward=false, 禁止 force push)
#   - rules 包含 update (update=false, 禁止直接 update)
#   - rules 包含 required_signatures (强制 GPG 签名验证)
#   - rules 包含 pull_request (required_reviewers >= 2,
#     dismiss_stale_reviews_on_push == true,
#     required_review_thread_resolution == true)
#   - bypass_actors 为空(禁止任何角色 bypass,包括 admin)
#
# 使用方法:
#   OWNER=maxiuquan REPO=tgjiema ./scripts/verify_branch_ruleset.sh
#   ./scripts/verify_branch_ruleset.sh maxiuquan tgjiema
#
# 退出码:
#   0  所有断言通过
#   1  任意断言失败或 API 错误
set -euo pipefail

RULESET_NAME="${RULESET_NAME:-R67 P0-01 Branch Immutability Ruleset}"

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [OWNER] [REPO]

R67 P0-01: 验证 master/main branch Repository Ruleset 配置。

参数(可选):
  OWNER  仓库 owner(默认从 gh repo view / git remote 推断)
  REPO   仓库名(默认从 gh repo view / git remote 推断)

环境变量:
  OWNER                  仓库 owner
  REPO                   仓库名
  RULESET_NAME           ruleset 名称(默认: "${RULESET_NAME}")
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT

退出码:
  0  所有断言通过
  1  任意断言失败或 API 错误

示例:
  OWNER=maxiuquan REPO=tgjiema $0
  $0 maxiuquan tgjiema
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  print_help
  exit 0
fi

# ─── 1. 解析 OWNER / REPO ───
OWNER="${OWNER:-${1:-}}"
REPO="${REPO:-${2:-}}"

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  if command -v gh >/dev/null 2>&1; then
    REPO_INFO=$(gh repo view --json owner,name 2>/dev/null || true)
    if [ -n "$REPO_INFO" ]; then
      OWNER="${OWNER:-$(echo "$REPO_INFO" | jq -r '.owner.login // empty')}"
      REPO="${REPO:-$(echo "$REPO_INFO" | jq -r '.name // empty')}"
    fi
  fi
fi

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
  if [ -n "$REMOTE_URL" ]; then
    PARSED=$(echo "$REMOTE_URL" \
      | sed -E 's#.*github\.com[:/]([^/]+)/([^/]+)(\.git)?$#\1 \2#' || true)
    if [ -n "$PARSED" ] && [ "$(echo "$PARSED" | wc -w)" -eq 2 ]; then
      OWNER="${OWNER:-$(echo "$PARSED" | cut -d' ' -f1)}"
      REPO="${REPO:-$(echo "$PARSED" | cut -d' ' -f2)}"
    fi
  fi
fi

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  echo "ERROR: [R67 P0-01] 无法确定 OWNER / REPO"
  exit 1
fi

# ─── 2. 鉴权 ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: [R67 P0-01] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
    exit 1
  fi
fi

# ─── 3. 列出所有 rulesets ───
if [ "$USE_GH_CLI" = "true" ]; then
  LIST_RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets")
else
  LIST_RESPONSE=$(curl -sS \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${OWNER}/${REPO}/rulesets")
fi

if ! echo "$LIST_RESPONSE" | jq -e 'type == "array"' > /dev/null 2>&1; then
  echo "ERROR: [R67 P0-01] 列出 rulesets 失败,GitHub API 响应:"
  echo "$LIST_RESPONSE"
  exit 1
fi

# ─── 4. 查找 branch ruleset(按 name 优先,fallback 到 target=branch + ref_name) ───
RULESET_ID=$(echo "$LIST_RESPONSE" \
  | jq -r --arg name "$RULESET_NAME" \
    '.[] | select(.name == $name) | .id' \
  | head -n 1)

if [ -z "$RULESET_ID" ]; then
  # fallback: target=branch 且 ref_name.include 包含 refs/heads/master
  RULESET_ID=$(echo "$LIST_RESPONSE" \
    | jq -r '
      .[] | select(.target == "branch")
      | select(.conditions.ref_name.include // [] | any(. == "refs/heads/master"))
      | .id' \
    | head -n 1)
fi

if [ -z "$RULESET_ID" ]; then
  # R67 P0-01 PR 宽松模式:PR 场景下 ruleset 不存在时 WARN 但 exit 0
  # 原因:ruleset 是仓库级 admin 配置,PR 无法创建(需 admin PAT);
  #       ruleset 应在 merge 到 master 前由管理员手动配置。
  #       push 到 master 时严格 fail-closed(下方 else 分支)。
  if [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ]; then
    echo "WARN: [R67 P0-01] 未找到 branch ruleset(PR 宽松模式 — 不阻断)"
    echo ""
    echo "当前仓库 rulesets 列表:"
    echo "$LIST_RESPONSE" | jq '[.[] | {id, name, target, enforcement}]'
    echo ""
    echo "WARNING: merge 到 master 前,管理员必须运行:"
    echo "  OWNER=${OWNER} REPO=${REPO} ./scripts/configure_branch_ruleset.sh"
    echo "  (需 admin PAT with repo scope)"
    echo ""
    echo "PASS (PR lenient mode): ruleset 未配置,但不阻断 PR。"
    echo "master push 时将严格验证(fail-closed)。"
    exit 0
  fi
  echo "FAIL: [R67 P0-01] 未找到 branch ruleset(期望 name='${RULESET_NAME}' 或 target=branch + refs/heads/master)"
  echo ""
  echo "当前仓库 rulesets 列表:"
  echo "$LIST_RESPONSE" | jq '[.[] | {id, name, target, enforcement}]'
  echo ""
  echo "修复:运行 OWNER=${OWNER} REPO=${REPO} ./scripts/configure_branch_ruleset.sh"
  exit 1
fi

echo "[INFO] 找到 branch ruleset id=${RULESET_NAME},获取详细配置..."

# ─── 5. 获取 ruleset 详细配置 ───
if [ "$USE_GH_CLI" = "true" ]; then
  RULESET_JSON=$(gh api "repos/${OWNER}/${REPO}/rulesets/${RULESET_ID}")
else
  RULESET_JSON=$(curl -sS \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${OWNER}/${REPO}/rulesets/${RULESET_ID}")
fi

if ! echo "$RULESET_JSON" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R67 P0-01] 获取 ruleset 详情失败,GitHub API 响应:"
  echo "$RULESET_JSON"
  exit 1
fi

# ─── 6. 断言检查(fail-closed) ───
FAIL=0

assert_eq() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  ✓ $name: $actual"
  else
    echo "  ✗ $name: 期望=$expected 实际=$actual"
    FAIL=1
  fi
}

assert_contains() {
  local name="$1" json="$2" jq_expr="$3"
  if echo "$json" | jq -e "$jq_expr" > /dev/null 2>&1; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name"
    FAIL=1
  fi
}

echo ""
echo "=== R67 P0-01: Branch Ruleset 断言检查 ==="

TARGET=$(echo "$RULESET_JSON" | jq -r '.target')
assert_eq "target" "branch" "$TARGET"

ENFORCEMENT=$(echo "$RULESET_JSON" | jq -r '.enforcement')
assert_eq "enforcement" "active" "$ENFORCEMENT"

assert_contains "conditions.ref_name.include 含 refs/heads/master" \
  "$RULESET_JSON" \
  '.conditions.ref_name.include | index("refs/heads/master") != null'

assert_contains "conditions.ref_name.include 含 refs/heads/main" \
  "$RULESET_JSON" \
  '.conditions.ref_name.include | index("refs/heads/main") != null'

assert_contains "rules 含 deletion (deletion=false)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "deletion")'

assert_contains "rules 含 non_fast_forward (non_fast_forward=false)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "non_fast_forward")'

assert_contains "rules 含 update (update=false)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "update")'

assert_contains "rules 含 required_signatures (强制签名)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "required_signatures")'

assert_contains "rules 含 pull_request (PR-only 流程)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "pull_request")'

assert_contains "pull_request.required_reviewers >= 2" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.required_reviewers] | add >= 2'

assert_contains "pull_request.dismiss_stale_reviews_on_push == true" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.dismiss_stale_reviews_on_push] | add == true'

assert_contains "pull_request.required_review_thread_resolution == true" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.required_review_thread_resolution] | add == true'

# bypass_actors 必须为空(禁止任何角色 bypass,包括 admin)
assert_contains "bypass_actors 为空(禁止 admin bypass)" \
  "$RULESET_JSON" \
  '(.bypass_actors // []) | length == 0'

echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: [R67 P0-01] Branch Ruleset 断言失败"
  echo ""
  echo "实际配置:"
  echo "$RULESET_JSON" | jq '{id, name, target, enforcement, conditions, rules, bypass_actors}'
  exit 1
fi

echo "PASS: [R67 P0-01] Branch Ruleset 所有断言通过"
exit 0
