#!/usr/bin/env bash
# R44 G0-1: 配置 GitHub branch protection for master 分支
# 使用方法: ./scripts/configure_branch_protection.sh [github_token] [repo_owner/repo_name]
# 需要 gh CLI 已登录,或传入 GITHUB_TOKEN 环境变量
set -euo pipefail

REPO="${2:-maxiuquan/tgjiema}"
TOKEN="${1:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"

if [ -z "$TOKEN" ]; then
  echo "ERROR: 需要提供 GitHub Token 作为第一个参数或 GH_TOKEN/GITHUB_TOKEN 环境变量"
  exit 1
fi

# 必需的 status check contexts
CONTEXTS=(
  "test (3.10)"
  "test (3.11)"
  "test (3.12)"
  "lint"
  "security"
  "deploy-check / verify-deploy"
  "release-gates"
  "E2E Tests / playwright-e2e"
)

# 构造 JSON payload
CONTEXTS_JSON=$(printf '%s\n' "${CONTEXTS[@]}" | jq -R . | jq -s .)

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

echo "Configuring branch protection for ${REPO}/master..."
RESPONSE=$(curl -s -X PUT \
  -H "Authorization: token ${TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -d "${PAYLOAD}" \
  "https://api.github.com/repos/${REPO}/branches/master/protection")

if echo "$RESPONSE" | jq -e '.url' > /dev/null 2>&1; then
  echo "✓ Branch protection configured successfully"
  echo "Required status checks:"
  echo "$RESPONSE" | jq '.required_status_checks.contexts'
else
  echo "ERROR: Failed to configure branch protection"
  echo "$RESPONSE"
  exit 1
fi
