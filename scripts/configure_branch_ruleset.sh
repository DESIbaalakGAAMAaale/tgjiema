#!/usr/bin/env bash
# R67 P0-01 / R70 Wave 10: 配置 GitHub Repository Ruleset 实现 master/main 分支不可变性
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
#   或 PUT /repos/{owner}/{repo}/rulesets/{id})配置两个针对
#   refs/heads/master 与 refs/heads/main 的 Repository Ruleset:
#
#   1. R67 P0-01 Branch Immutability Ruleset(签名 + 不可变历史):
#     - deletion             false — 禁止删除 master/main
#     - non_fast_forward     false — 禁止 force push(历史不可变)
#     - update               false — 禁止直接 update(必须走 PR)
#     - required_signatures  true  — 所有推送 commit 必须经过 GPG 签名验证
#     - pull_request         2 名 reviewer + stale dismissal + conversation resolution
#
#   2. R70 Wave 10 governance-master-protect(P0-01 治理止血):
#     - pull_request         1 名 approving review + require_code_owner_review=true
#                            + dismiss_stale_reviews_on_push=true
#                            (CODEOWNERS 强制 owner 评审)
#     - required_status_checks  必含 lint / static-gates / test /
#                            verify-branch-ruleset / verify-branch-protection
#     - non_fast_forward     false — 禁止 force push(no force push)
#
#   两个 ruleset 的 bypass_actors 均为空:禁止任何角色(包括 admin/app)bypass。
#   R67 P0-01 与 R70 Wave 10 均明确要求"禁止 admin bypass"。
#
# 使用方法:
#   OWNER=maxiuquan REPO=tgjiema ./scripts/configure_branch_ruleset.sh
#   ./scripts/configure_branch_ruleset.sh maxiuquan tgjiema
#   ./scripts/configure_branch_ruleset.sh --dry-run            # 仅打印 payload,不调用 gh api
#   ./scripts/configure_branch_ruleset.sh --dry-run maxiuquan tgjiema
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量(admin scope)
#   - 或 gh CLI 已登录(gh auth login with admin scope)
#
# 幂等性: 若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。
#         两个 ruleset(R67 + R70)分别独立做幂等性检查。
# 退出码: 0 成功 / 1 API 失败或参数错误。
set -euo pipefail

# ─── R67 P0-01 Ruleset 配置(签名 + 不可变历史) ───
RULESET_NAME="${RULESET_NAME:-R67 P0-01 Branch Immutability Ruleset}"
RULESET_DESCRIPTION="${RULESET_DESCRIPTION:-R67 P0-01: master/main branch 不可变 ruleset — 强制 GPG 签名 commits、禁止 force push/delete、禁止 admin bypass。所有变更必须通过 PR(2 名独立 reviewer + conversation resolution)。}"

# ─── R70 Wave 10 governance-master-protect Ruleset 配置(治理止血) ───
R70_RULESET_NAME="${R70_RULESET_NAME:-r70-governance-master-protect}"
R70_RULESET_DESCRIPTION="${R70_RULESET_DESCRIPTION:-R70 Wave 10 P0-01 治理止血: master 分支 governance ruleset — pull_request(1 approving review + code_owner_review + stale dismissal)+ required_status_checks(lint/static-gates/test/verify-branch-ruleset/verify-branch-protection)+ non_fast_forward(no force push)。bypass_actors 空,admin/app 不可绕过。}"

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [--dry-run] [OWNER] [REPO]

R67 P0-01 / R70 Wave 10: 为 refs/heads/master 与 refs/heads/main 配置两个
GitHub Repository Ruleset:
  1. R67 P0-01 Branch Immutability Ruleset(签名 + 不可变历史)
  2. R70 Wave 10 governance-master-protect(治理止血)

R67 P0-01 必需的规则:
  - deletion             false — 禁止删除 master/main
  - non_fast_forward     false — 禁止 force push(历史不可变)
  - update               false — 禁止直接 update(必须走 PR)
  - required_signatures  true  — 所有推送 commit 必须经过 GPG 签名验证
  - pull_request         2 名 reviewer + stale dismissal + conversation resolution
  - bypass_actors        []    — 禁止任何角色(包括 admin)bypass

R70 Wave 10 governance-master-protect 必需的规则:
  - pull_request         1 approving review + require_code_owner_review=true
                         + dismiss_stale_reviews_on_push=true
  - required_status_checks  必含 lint / static-gates / test /
                         verify-branch-ruleset / verify-branch-protection
  - non_fast_forward     false — 禁止 force push(no force push)
  - bypass_actors        []    — 禁止任何角色(包括 admin/app)bypass

参数(可选,可通过环境变量或位置参数指定):
  OWNER  仓库 owner(默认从 gh repo view / git remote 推断)
  REPO   仓库名(默认从 gh repo view / git remote 推断)

标志:
  --dry-run   仅打印两个 ruleset 的 payload,不调用 gh api(用于审计/测试)
  --help, -h  显示帮助信息

环境变量:
  OWNER                  仓库 owner
  REPO                   仓库名
  RULESET_NAME           R67 ruleset 名称
  RULESET_DESCRIPTION    R67 ruleset 描述
  R70_RULESET_NAME       R70 governance ruleset 名称
  R70_RULESET_DESCRIPTION  R70 governance ruleset 描述
  REQUIRED_REVIEWERS     R67 PR 必需 reviewer 数量(默认: 2)
  REQUIRED_STATUS_CHECKS R70 必需 status check(JSON 数组,默认 5 项)
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT(admin scope,若未设置则使用 gh CLI)

鉴权(二选一):
  - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
  - 或 gh CLI 已登录(gh auth login with admin scope)

幂等性:
  若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。
  两个 ruleset(R67 + R70)分别独立做幂等性检查。

退出码:
  0  成功(或 --dry-run 打印 payload 完成)
  1  API 失败或参数错误

示例:
  OWNER=maxiuquan REPO=tgjiema $0
  $0 maxiuquan tgjiema
  $0 --dry-run
  $0 --dry-run maxiuquan tgjiema
EOF
}

# ─── 0. 解析 flags(--dry-run / --help) ───
DRY_RUN=false
POSITIONAL_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      print_help
      exit 0
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --*)
      echo "ERROR: [R70 Wave 10] 未知 flag: $1"
      echo "  用法: $0 [--dry-run] [OWNER] [REPO]"
      echo "  运行 '$0 --help' 查看完整帮助"
      exit 1
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done
# 恢复位置参数
set -- "${POSITIONAL_ARGS[@]+"${POSITIONAL_ARGS[@]}"}" 2>/dev/null || set --

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
  echo "ERROR: [R70 Wave 10] 无法确定 OWNER / REPO"
  echo "  用法 1: OWNER=owner REPO=repo $0"
  echo "  用法 2: $0 owner repo"
  echo "  用法 3: $0 --dry-run owner repo"
  exit 1
fi

echo "[INFO] OWNER=${OWNER}  REPO=${REPO}  DRY_RUN=${DRY_RUN}"

# ─── 2. 鉴权(--dry-run 模式跳过) ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if [ "$DRY_RUN" = "false" ]; then
    if ! gh auth status >/dev/null 2>&1; then
      echo "ERROR: [R70 Wave 10] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
      echo "  提示: 使用 --dry-run 可跳过鉴权与 API 调用"
      exit 1
    fi
  else
    # dry-run 模式: 不需要鉴权,但仍检查 jq 可用
    if ! command -v jq >/dev/null 2>&1; then
      echo "ERROR: [R70 Wave 10] 需要 jq(即使 --dry-run 模式也需构造 payload)"
      exit 1
    fi
  fi
fi

# ─── 3. 必需 reviewer 数量(R67 P0-01: 默认 2) ───
REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"
if ! [[ "$REQUIRED_REVIEWERS" =~ ^[0-9]+$ ]] || [ "$REQUIRED_REVIEWERS" -lt 1 ]; then
  echo "ERROR: [R67 P0-01] REQUIRED_REVIEWERS 必须为正整数(实际: $REQUIRED_REVIEWERS)"
  exit 1
fi

# ─── 4. R70 必需 status checks(默认 5 项) ───
# R70 Wave 10 要求:lint / static-gates / test / verify-branch-ruleset / verify-branch-protection
REQUIRED_STATUS_CHECKS="${REQUIRED_STATUS_CHECKS:-}"
if [ -z "$REQUIRED_STATUS_CHECKS" ]; then
  REQUIRED_STATUS_CHECKS='["lint","static-gates","test","verify-branch-ruleset","verify-branch-protection"]'
fi
# 校验是合法 JSON 数组
if ! echo "$REQUIRED_STATUS_CHECKS" | jq -e 'type == "array" and length >= 1' > /dev/null 2>&1; then
  echo "ERROR: [R70 Wave 10] REQUIRED_STATUS_CHECKS 必须为非空 JSON 数组(实际: $REQUIRED_STATUS_CHECKS)"
  exit 1
fi
# 校验元素均为字符串
if ! echo "$REQUIRED_STATUS_CHECKS" | jq -e 'all(.[]; type == "string")' > /dev/null 2>&1; then
  echo "ERROR: [R70 Wave 10] REQUIRED_STATUS_CHECKS 数组元素必须全部为字符串"
  exit 1
fi

# ─── 5. 构造 R67 P0-01 ruleset payload ───
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

# ─── 6. 构造 R70 Wave 10 governance-master-protect ruleset payload ───
# R70 Wave 10:
#   - pull_request: 1 approving review + require_code_owner_review=true + dismiss_stale=true
#   - required_status_checks: lint / static-gates / test / verify-branch-ruleset / verify-branch-protection
#   - non_fast_forward: no force push
#   - bypass_actors: 空(admin/app 不可绕过)
R70_PAYLOAD=$(jq -n \
  --arg name "$R70_RULESET_NAME" \
  --arg description "$R70_RULESET_DESCRIPTION" \
  --argjson required_checks "$REQUIRED_STATUS_CHECKS" '{
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
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "required_reviewers": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "required_checks": ($required_checks | map({context: .})),
        "strict_merge": false
      }
    }
  ]
}')

# ─── 7. --dry-run 模式:打印 payload 后退出 ───
if [ "$DRY_RUN" = "true" ]; then
  echo ""
  echo "=========================================================="
  echo "  DRY RUN — 不调用 gh api,仅打印 payload 供审计"
  echo "=========================================================="
  echo ""
  echo "=== [1/2] R67 P0-01 Branch Immutability Ruleset payload ==="
  echo "$PAYLOAD" | jq '.'
  echo ""
  echo "=== [2/2] R70 Wave 10 governance-master-protect ruleset payload ==="
  echo "$R70_PAYLOAD" | jq '.'
  echo ""
  echo "=========================================================="
  echo "  DRY RUN 完成 — 未调用任何 gh api"
  echo "  实际应用: 去掉 --dry-run 重新运行本脚本"
  echo "=========================================================="
  exit 0
fi

# ════════════════════════════════════════════════════════════════
# 8. R67 P0-01: 幂等配置(查找现有 ruleset,PUT 更新或 POST 创建)
# ════════════════════════════════════════════════════════════════
echo ""
echo "=== [1/2] 配置 R67 P0-01 Branch Immutability Ruleset ==="
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

if ! echo "$RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R67 P0-01] Ruleset 配置失败,GitHub API 响应:"
  echo "$RESPONSE"
  exit 1
fi

RULESET_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "✓ [R67 P0-01] Ruleset 配置成功(id=${RULESET_ID})"

# ─── 9. R67 P0-01 配置后自检 ───
echo ""
echo "=== 验证 R67 P0-01 配置(关键断言) ==="
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
echo "✓ [R67 P0-01] 所有断言通过"
echo ""
echo "最终配置(关键字段):"
echo "$RULESET_JSON" | jq '{id, name, target, source_type, enforcement, conditions, rules, bypass_actors}'

# ════════════════════════════════════════════════════════════════
# 10. R70 Wave 10 governance-master-protect: 幂等配置
# ════════════════════════════════════════════════════════════════
echo ""
echo "=== [2/2] 配置 R70 Wave 10 governance-master-protect ruleset ==="
echo "[INFO] 查找名为 '${R70_RULESET_NAME}' 的现有 ruleset(幂等性检查)..."

# 重新列出 rulesets(刚创建/更新 R67 后,列表可能已变)
if [ "$USE_GH_CLI" = "true" ]; then
  R70_LIST_RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets")
else
  R70_LIST_RESPONSE=$(curl -sS \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${OWNER}/${REPO}/rulesets")
fi

if ! echo "$R70_LIST_RESPONSE" | jq -e 'type == "array"' > /dev/null 2>&1; then
  echo "ERROR: [R70 Wave 10] 列出 rulesets 失败,GitHub API 响应:"
  echo "$R70_LIST_RESPONSE"
  exit 1
fi

R70_EXISTING_RULESET_ID=$(echo "$R70_LIST_RESPONSE" \
  | jq -r --arg name "$R70_RULESET_NAME" \
    '.[] | select(.name == $name) | .id' \
  | head -n 1)

if [ -n "$R70_EXISTING_RULESET_ID" ]; then
  echo "[INFO] 现有 R70 ruleset 已存在(id=${R70_EXISTING_RULESET_ID}),将 PUT 更新"
  if [ "$USE_GH_CLI" = "true" ]; then
    R70_RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets/${R70_EXISTING_RULESET_ID}" \
                -X PUT --input - <<< "$R70_PAYLOAD")
  else
    R70_RESPONSE=$(curl -sS -X PUT \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -d "${R70_PAYLOAD}" \
      "https://api.github.com/repos/${OWNER}/${REPO}/rulesets/${R70_EXISTING_RULESET_ID}")
  fi
else
  echo "[INFO] 现有 R70 ruleset 不存在,将 POST 创建"
  if [ "$USE_GH_CLI" = "true" ]; then
    R70_RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets" \
                -X POST --input - <<< "$R70_PAYLOAD")
  else
    R70_RESPONSE=$(curl -sS -X POST \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -d "${R70_PAYLOAD}" \
      "https://api.github.com/repos/${OWNER}/${REPO}/rulesets")
  fi
fi

if ! echo "$R70_RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R70 Wave 10] Ruleset 配置失败,GitHub API 响应:"
  echo "$R70_RESPONSE"
  exit 1
fi

R70_RULESET_ID=$(echo "$R70_RESPONSE" | jq -r '.id')
echo "✓ [R70 Wave 10] Ruleset 配置成功(id=${R70_RULESET_ID})"

# ─── 11. R70 Wave 10 配置后自检 ───
echo ""
echo "=== 验证 R70 Wave 10 配置(关键断言) ==="
R70_RULESET_JSON="$R70_RESPONSE"

echo "Assert: name == r70-governance-master-protect"
echo "$R70_RULESET_JSON" | jq -e --arg n "$R70_RULESET_NAME" '.name == $n' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] name != r70-governance-master-protect"; exit 1; }

echo "Assert: target == branch"
echo "$R70_RULESET_JSON" | jq -e '.target == "branch"' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] target != branch"; exit 1; }

echo "Assert: enforcement == active"
echo "$R70_RULESET_JSON" | jq -e '.enforcement == "active"' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] enforcement != active"; exit 1; }

echo "Assert: conditions.ref_name.include 包含 refs/heads/master"
echo "$R70_RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/heads/master") != null' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] conditions.ref_name.include 不包含 refs/heads/master"; exit 1; }

echo "Assert: rules 包含 non_fast_forward (no force push)"
echo "$R70_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "non_fast_forward")' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] rules 缺少 non_fast_forward 类型"; exit 1; }

echo "Assert: rules 包含 pull_request (PR 流程 + code owner review)"
echo "$R70_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "pull_request")' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] rules 缺少 pull_request 类型"; exit 1; }

echo "Assert: pull_request.require_code_owner_review == true"
echo "$R70_RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.require_code_owner_review] | add == true' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] pull_request.require_code_owner_review != true"; exit 1; }

echo "Assert: pull_request.dismiss_stale_reviews_on_push == true"
echo "$R70_RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.dismiss_stale_reviews_on_push] | add == true' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] pull_request.dismiss_stale_reviews_on_push != true"; exit 1; }

echo "Assert: pull_request.required_reviewers >= 1"
echo "$R70_RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.required_reviewers] | add >= 1' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] pull_request.required_reviewers < 1"; exit 1; }

echo "Assert: rules 包含 required_status_checks"
echo "$R70_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_status_checks")' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] rules 缺少 required_status_checks 类型"; exit 1; }

echo "Assert: required_status_checks 包含全部 5 个必需 check"
R70_REQUIRED_CHECKS=$(echo "$R70_RULESET_JSON" \
  | jq -r '[.rules[] | select(.type == "required_status_checks") | .parameters.required_checks[].context] | sort | join(",")')
EXPECTED_CHECKS=$(echo "$REQUIRED_STATUS_CHECKS" | jq -r 'sort | join(",")')
if [ "$R70_REQUIRED_CHECKS" != "$EXPECTED_CHECKS" ]; then
  echo "ERROR: [R70 Wave 10] required_status_checks.contexts 与预期不一致"
  echo "  实际:   $R70_REQUIRED_CHECKS"
  echo "  预期:   $EXPECTED_CHECKS"
  exit 1
fi

echo "Assert: bypass_actors 为空(禁止任何角色 bypass,包括 admin/app)"
echo "$R70_RULESET_JSON" | jq -e '(.bypass_actors // []) | length == 0' > /dev/null \
  || { echo "ERROR: [R70 Wave 10] bypass_actors 非空(应禁止任何角色 bypass)"; exit 1; }

echo ""
echo "✓ [R70 Wave 10] 所有断言通过"
echo ""
echo "=== R70 Wave 10 governance-master-protect 最终配置(关键字段) ==="
echo "$R70_RULESET_JSON" | jq '{id, name, target, source_type, enforcement, conditions, rules, bypass_actors}'

echo ""
echo "=========================================================="
echo "  ✓ R67 P0-01 + R70 Wave 10 两个 ruleset 均已配置成功"
echo "=========================================================="
