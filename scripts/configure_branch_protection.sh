#!/usr/bin/env bash
# R47 P0-1/P0-2: 配置 GitHub branch protection for master 分支
#
# 使用方法:
#   # 1) 通过环境变量指定 owner/repo
#   OWNER=maxiuquan REPO=tgjiema ./scripts/configure_branch_protection.sh
#   # 2) 通过位置参数指定
#   ./scripts/configure_branch_protection.sh maxiuquan tgjiema
#   # 3) 自动从 git remote origin 推断(无需传参)
#   ./scripts/configure_branch_protection.sh
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
#   - 或 gh CLI 已登录(gh auth login)
#
# 配置完成后会立即复用 verify-branch-protection job 的断言逻辑做自检,
# 任何属性不满足 R47 P0-2 要求都会让本脚本以非零退出。
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
  echo "ERROR: 无法确定 OWNER / REPO"
  echo "  用法 1: OWNER=owner REPO=repo $0"
  echo "  用法 2: $0 owner repo"
  echo "  或确保 git remote origin 指向 GitHub 仓库"
  exit 1
fi

# ─── 2. 鉴权(token 优先,否则用 gh CLI 已登录的凭证) ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
    exit 1
  fi
  USE_GH_CLI=true
fi

# ─── 3. R47 P0-2 必需的 status check contexts (4 个核心 context) ───
REQUIRED_CONTEXTS=("CI" "Deploy Check" "Release Gates" "E2E")

# 构造 contexts JSON 数组
CONTEXTS_JSON=$(printf '%s\n' "${REQUIRED_CONTEXTS[@]}" | jq -R . | jq -s .)

# ─── 4. 构造 PUT /branches/master/protection 的 payload ───
# 严格包含 R47 P0-2 要求的所有字段:
#   - required_status_checks.strict = true
#   - enforce_admins = true
#   - required_pull_request_reviews.required_approving_review_count >= 1
#   - allow_force_pushes = false
#   - allow_deletions = false
PAYLOAD=$(cat <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ${CONTEXTS_JSON}
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "block_creations": false
}
EOF
)

# ─── 5. 调用 GitHub API 配置 branch protection ───
echo "Configuring branch protection for ${OWNER}/${REPO}/master..."
if [ "$USE_GH_CLI" = "true" ]; then
  RESPONSE=$(gh api "repos/${OWNER}/${REPO}/branches/master/protection" -X PUT --input - <<< "$PAYLOAD")
else
  RESPONSE=$(curl -sS -X PUT \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -d "${PAYLOAD}" \
    "https://api.github.com/repos/${OWNER}/${REPO}/branches/master/protection")
fi

# 检查配置是否成功(成功响应包含 url 字段)
if ! echo "$RESPONSE" | jq -e '.url' > /dev/null 2>&1; then
  echo "ERROR: Branch protection 配置失败,GitHub API 响应:"
  echo "$RESPONSE"
  exit 1
fi
echo "✓ Branch protection 配置成功"

# ─── 6. 配置后立即运行验证(复用 verify-branch-protection job 的断言逻辑) ───
# R47 P0-2 要求:配置后必须自检,任何属性不满足则失败
echo ""
echo "=== 验证配置(复用 verify-branch-protection 断言) ==="
BP_JSON="$RESPONSE"

echo "Assert: required_status_checks.strict == true"
echo "$BP_JSON" | jq -e '.required_status_checks.strict == true' > /dev/null \
  || { echo "ERROR: required_status_checks.strict != true"; exit 1; }

echo "Assert: enforce_admins.enabled == true"
echo "$BP_JSON" | jq -e '.enforce_admins.enabled == true' > /dev/null \
  || { echo "ERROR: enforce_admins.enabled != true"; exit 1; }

echo "Assert: required_approving_review_count >= 1"
echo "$BP_JSON" | jq -e '.required_pull_request_reviews.required_approving_review_count >= 1' > /dev/null \
  || { echo "ERROR: required_approving_review_count < 1"; exit 1; }

echo "Assert: allow_force_pushes.enabled == false"
echo "$BP_JSON" | jq -e '.allow_force_pushes.enabled == false' > /dev/null \
  || { echo "ERROR: allow_force_pushes.enabled != false"; exit 1; }

echo "Assert: allow_deletions.enabled == false"
echo "$BP_JSON" | jq -e '.allow_deletions.enabled == false' > /dev/null \
  || { echo "ERROR: allow_deletions.enabled != false"; exit 1; }

# 逐个断言必需 context 存在
for ctx in "${REQUIRED_CONTEXTS[@]}"; do
  echo "Assert: required context '$ctx' present"
  echo "$BP_JSON" | jq -e --arg ctx "$ctx" \
    '.required_status_checks.contexts | index($ctx) != null' > /dev/null \
    || { echo "ERROR: required context '$ctx' missing"; exit 1; }
done

echo ""
echo "✓ 所有 R47 P0-2 断言通过"
echo ""
echo "最终配置:"
echo "$BP_JSON" | jq '.required_status_checks, .enforce_admins, .required_pull_request_reviews, .allow_force_pushes, .allow_deletions'
