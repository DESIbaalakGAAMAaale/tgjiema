#!/usr/bin/env bash
# R67 P0-01: 配置 GitHub Repository Ruleset 实现 master/main 分支不可变性
# (强制 GPG 签名 commits / 禁止 force push / 禁止删除 / 禁止 admin bypass / PR-only)
#
# R67 P0-01 整改说明(审计报告要求):
#   为 master/main 和 v* tag 建立 active ruleset:require signed commits、
#   禁止 force push/delete、禁止 admin bypass。禁止直接 push;只允许 PR +
#   两名独立 reviewer + stale approval dismissal + conversation resolution。
#   重新以签名提交承载 R67 修复;不要仅在后续 commit 补签,因为历史 unsigned
#   commit 仍在发布祖先链。
#
#   本脚本与 branch_protection.expected.json 互补:
#     - branch protection (PUT /branches/{branch}/protection) 提供
#       required_status_checks / enforce_admins / required_signatures(legacy API)
#     - repository ruleset (POST /repos/{owner}/{repo}/rulesets) 提供更现代的
#       声明式规则,含 pull_request 规则(reviewers / stale dismissal /
#       conversation resolution)。两者并行存在,GitHub 会合并生效。
#
#   本脚本通过 GitHub REST API(POST /repos/{owner}/{repo}/rulesets
#   或 PUT /repos/{owner}/{repo}/rulesets/{id})配置一个针对
#   refs/heads/master 与 refs/heads/main 的 Repository Ruleset,强制以下规则:
#     - deletion             false — 禁止删除 master/main
#     - non_fast_forward     false — 禁止 force push(历史不可变)
#     - update               false — 禁止直接 update(必须走 PR)
#     - required_signatures  true  — 所有推送 commit 必须经过 GPG 签名验证
#     - pull_request         2 名 reviewer + stale dismissal + conversation resolution
#
#   bypass_actors 为空:禁止任何角色(包括 admin)bypass。
#   R67 P0-01 明确要求"禁止 admin bypass"。
#
# 使用方法:
#   OWNER=maxiuquan REPO=tgjiema ./scripts/configure_branch_ruleset.sh
#   ./scripts/configure_branch_ruleset.sh maxiuquan tgjiema
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量(admin scope)
#   - 或 gh CLI 已登录(gh auth login with admin scope)
#
# 幂等性: 若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。
# 退出码: 0 成功 / 1 API 失败或参数错误。
set -euo pipefail

RULESET_NAME="${RULESET_NAME:-R67 P0-01 Branch Immutability Ruleset}"
RULESET_DESCRIPTION="${RULESET_DESCRIPTION:-R67 P0-01: master/main branch 不可变 ruleset — 强制 GPG 签名 commits、禁止 force push/delete、禁止 admin bypass。所有变更必须通过 PR(2 名独立 reviewer + conversation resolution)。}"

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [OWNER] [REPO]

R67 P0-01: 为 refs/heads/master 与 refs/heads/main 配置 GitHub Repository
Ruleset,强制分支不可变性 + 签名 commits + PR-only 流程。

必需的规则:
  - deletion             false — 禁止删除 master/main
  - non_fast_forward     false — 禁止 force push(历史不可变)
  - update               false — 禁止直接 update(必须走 PR)
  - required_signatures  true  — 所有推送 commit 必须经过 GPG 签名验证
  - pull_request         2 名 reviewer + stale dismissal + conversation resolution
  - bypass_actors        []    — 禁止任何角色(包括 admin)bypass

参数(可选,可通过环境变量或位置参数指定):
  OWNER  仓库 owner(默认从 gh repo view / git remote 推断)
  REPO   仓库名(默认从 gh repo view / git remote 推断)

环境变量:
  OWNER                  仓库 owner
  REPO                   仓库名
  RULESET_NAME           ruleset 名称
  RULESET_DESCRIPTION    ruleset 描述
  REQUIRED_REVIEWERS     PR 必需 reviewer 数量(默认: 2)
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT(admin scope,若未设置则使用 gh CLI)

鉴权(二选一):
  - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
  - 或 gh CLI 已登录(gh auth login with admin scope)

幂等性:
  若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。

退出码:
  0  成功
  1  API 失败或参数错误

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
  echo "  用法 1: OWNER=owner REPO=repo $0"
  echo "  用法 2: $0 owner repo"
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

# ─── 3. 必需 reviewer 数量 ───
REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"
if ! [[ "$REQUIRED_REVIEWERS" =~ ^[0-9]+$ ]] || [ "$REQUIRED_REVIEWERS" -lt 1 ]; then
  echo "ERROR: [R67 P0-01] REQUIRED_REVIEWERS 必须为正整数(实际: $REQUIRED_REVIEWERS)"
  exit 1
fi

# ─── 4. 构造 ruleset payload ───
# R67 P0-01: bypass_actors 为空,禁止任何角色(包括 admin)bypass
PAYLOAD=$(jq -n \
  --arg name "$RULESET_NAME" \
  --arg description "$RULESET_DESCRIPTION" \
  --argjson required_reviewers "$REQUIRED_REVIEWERS" '{
  name: $name,
  description: $description,
  target: "branch",
  source_type: "Repository",
  enforcement: "active",
  bypass_actors: [],
  conditions: {
    ref_name: {
      include: ["refs/heads/master", "refs/heads/main"],
      exclude: []
    }
  },
  rules: [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "update"},
    {"type": "required_signatures"},
    {
      "type": "pull_request",
      "parameters": {
        "required_reviewers": ($required_reviewers),
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      }
    }
  ]
}')

# ─── 5. 幂等性检查 ───
echo "[INFO] 查找名为 '${RULESET_NAME}' 的现有 ruleset(幂等性检查)..."
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

# 按 name 查找现有 ruleset;若按 name 未找到,尝试按 target=branch + ref_name include 匹配
EXISTING_RULESET_ID=$(echo "$LIST_RESPONSE" \
  | jq -r --arg name "$RULESET_NAME" \
    '.[] | select(.name == $name) | .id' \
  | head -n 1)

if [ -z "$EXISTING_RULESET_ID" ]; then
  # fallback: target=branch 且 ref_name.include 包含 refs/heads/master
  EXISTING_RULESET_ID=$(echo "$LIST_RESPONSE" \
    | jq -r '
      .[] | select(.target == "branch")
      | select(.conditions.ref_name.include // [] | any(. == "refs/heads/master"))
      | .id' \
    | head -n 1)
  if [ -n "$EXISTING_RULESET_ID" ]; then
    echo "[INFO] 按 name 未找到,但按 target=branch + ref_name 匹配到 ruleset id=${EXISTING_RULESET_ID}"
  fi
fi

if [ -n "$EXISTING_RULESET_ID" ]; then
  echo "[INFO] 现有 ruleset 已存在(id=${EXISTING_RULESET_ID}),将 PUT 更新"
  if [ "$USE_GH_CLI" = "true" ]; then
    RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets/${EXISTING_RULESET_ID}" \
                -X PUT --input - <<< "$PAYLOAD")
  else
    RESPONSE=$(curl -sS -X PUT \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -d "${PAYLOAD}" \
      "https://api.github.com/repos/${OWNER}/${REPO}/rulesets/${EXISTING_RULESET_ID}")
  fi
else
  echo "[INFO] 现有 ruleset 不存在,将 POST 创建"
  if [ "$USE_GH_CLI" = "true" ]; then
    RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets" \
                -X POST --input - <<< "$PAYLOAD")
  else
    RESPONSE=$(curl -sS -X POST \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -d "${PAYLOAD}" \
      "https://api.github.com/repos/${OWNER}/${REPO}/rulesets")
  fi
fi

# 检查配置是否成功
if ! echo "$RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R67 P0-01] Ruleset 配置失败,GitHub API 响应:"
  echo "$RESPONSE"
  exit 1
fi

RULESET_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "✓ [R67 P0-01] Ruleset 配置成功(id=${RULESET_ID})"

# ─── 6. 配置后立即自检 ───
echo ""
echo "=== 验证配置(关键断言) ==="
RULESET_JSON="$RESPONSE"

echo "Assert: target == branch"
echo "$RULESET_JSON" | jq -e '.target == "branch"' > /dev/null \
  || { echo "ERROR: [R67 P0-01] target != branch"; exit 1; }

echo "Assert: enforcement == active"
echo "$RULESET_JSON" | jq -e '.enforcement == "active"' > /dev/null \
  || { echo "ERROR: [R67 P0-01] enforcement != active"; exit 1; }

echo "Assert: conditions.ref_name.include 包含 refs/heads/master 与 refs/heads/main"
echo "$RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/heads/master") != null' > /dev/null \
  || { echo "ERROR: [R67 P0-01] conditions.ref_name.include 不包含 refs/heads/master"; exit 1; }
echo "$RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/heads/main") != null' > /dev/null \
  || { echo "ERROR: [R67 P0-01] conditions.ref_name.include 不包含 refs/heads/main"; exit 1; }

echo "Assert: rules 包含 deletion (deletion=false, master/main 不可删除)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "deletion")' > /dev/null \
  || { echo "ERROR: [R67 P0-01] rules 缺少 deletion 类型"; exit 1; }

echo "Assert: rules 包含 non_fast_forward (non_fast_forward=false, 禁止 force push)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "non_fast_forward")' > /dev/null \
  || { echo "ERROR: [R67 P0-01] rules 缺少 non_fast_forward 类型"; exit 1; }

echo "Assert: rules 包含 update (update=false, 禁止直接 update)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "update")' > /dev/null \
  || { echo "ERROR: [R67 P0-01] rules 缺少 update 类型"; exit 1; }

echo "Assert: rules 包含 required_signatures (强制 GPG 签名验证)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_signatures")' > /dev/null \
  || { echo "ERROR: [R67 P0-01] rules 缺少 required_signatures 类型"; exit 1; }

echo "Assert: rules 包含 pull_request (PR-only 流程)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "pull_request")' > /dev/null \
  || { echo "ERROR: [R67 P0-01] rules 缺少 pull_request 类型"; exit 1; }

echo "Assert: pull_request.required_reviewers >= 2"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.required_reviewers] | add >= 2' > /dev/null \
  || { echo "ERROR: [R67 P0-01] pull_request.required_reviewers < 2"; exit 1; }

echo "Assert: pull_request.dismiss_stale_reviews_on_push == true"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.dismiss_stale_reviews_on_push] | add == true' > /dev/null \
  || { echo "ERROR: [R67 P0-01] pull_request.dismiss_stale_reviews_on_push != true"; exit 1; }

echo "Assert: pull_request.required_review_thread_resolution == true"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.required_review_thread_resolution] | add == true' > /dev/null \
  || { echo "ERROR: [R67 P0-01] pull_request.required_review_thread_resolution != true"; exit 1; }

echo "Assert: bypass_actors 为空(禁止任何角色 bypass,包括 admin)"
echo "$RULESET_JSON" | jq -e '(.bypass_actors // []) | length == 0' > /dev/null \
  || { echo "ERROR: [R67 P0-01] bypass_actors 非空(应禁止任何角色 bypass)"; exit 1; }

echo ""
echo "✓ [R67 P0-01] 所有断言通过,branch ruleset 已正确配置"
echo ""
echo "最终配置(关键字段):"
echo "$RULESET_JSON" | jq '{id, name, target, source_type, enforcement, conditions, rules, bypass_actors}'
