#!/usr/bin/env bash
# R48 P0-2: 配置 GitHub branch protection for master 分支
#
# R48 整改说明:
#   旧版本硬编码 required_status_checks.contexts 为
#     ("CI" "Deploy Check" "Release Gates" "E2E"),
#   但 GitHub 实际 check run 名称通常是 "{workflow_name} / {job_name}"
#   (如 "CI / test (3.11)"、"Release Gates / docker-build"),
#   导致 BP context 与实际 check-runs 错配,PR 永久阻塞。
#
#   新版本通过 scripts/detect_branch_protection_contexts.sh 自动读取
#   最新 master commit 的 check-runs,提取实际 context 名称,再配置 BP。
#   用户也可通过 CONTEXTS_JSON 环境变量手动覆盖。
#
# 使用方法:
#   # 1) 自动检测 context(推荐)
#   ./scripts/configure_branch_protection.sh
#   # 2) 通过环境变量指定 owner/repo
#   OWNER=maxiuquan REPO=tgjiema ./scripts/configure_branch_protection.sh
#   # 3) 通过位置参数指定
#   ./scripts/configure_branch_protection.sh maxiuquan tgjiema
#   # 4) 手动指定 contexts(JSON 数组,覆盖自动检测)
#   CONTEXTS_JSON='["CI / test (3.11)","Deploy Check / verify-deploy", \
#                   "Release Gates / verify-branch-protection", \
#                   "E2E Tests / playwright-e2e"]' \
#     ./scripts/configure_branch_protection.sh
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
#   - 或 gh CLI 已登录(gh auth login)
#
# 配置完成后会立即复用 verify-branch-protection job 的断言逻辑做自检,
# 任何属性不满足 R47/R48 P0-2 要求都会让本脚本以非零退出。
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
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
    exit 1
  fi
fi

# ─── 3. 确定 status check contexts(R48 P0-2:不再硬编码) ───
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT_SCRIPT="${SCRIPT_DIR}/detect_branch_protection_contexts.sh"

# 3.1 用户可通过 CONTEXTS_JSON 环境变量手动覆盖
if [ -n "${CONTEXTS_JSON:-}" ]; then
  echo "[INFO] 使用用户通过 CONTEXTS_JSON 提供的 contexts"
  # 校验是合法 JSON 数组
  if ! echo "$CONTEXTS_JSON" | jq -e 'type == "array"' > /dev/null 2>&1; then
    echo "ERROR: CONTEXTS_JSON 不是合法的 JSON 数组"
    echo "  示例: CONTEXTS_JSON='[\"CI / test (3.11)\",\"CI / lint\"]' $0"
    exit 1
  fi
  # 校验数组元素均为字符串
  if ! echo "$CONTEXTS_JSON" | jq -e 'all(.[]; type == "string")' > /dev/null 2>&1; then
    echo "ERROR: CONTEXTS_JSON 数组元素必须全部为字符串"
    exit 1
  fi
else
  # 3.2 调用 detect 脚本自动检测
  echo "[INFO] 自动检测实际 check-runs 名称..."
  if [ ! -f "$DETECT_SCRIPT" ]; then
    echo "ERROR: detect_branch_protection_contexts.sh 不存在: $DETECT_SCRIPT"
    echo "  请确认脚本已正确部署"
    exit 1
  fi

  # 把鉴权信息传给 detect 脚本(显式传空字符串避免被 set -u 影响)
  if ! DETECT_OUTPUT=$(OWNER="$OWNER" REPO="$REPO" \
        GH_TOKEN="${GH_TOKEN:-}" GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
        bash "$DETECT_SCRIPT" 2>/dev/null); then
    echo "ERROR: 自动检测 contexts 失败"
    echo "  请先在 master 分支触发至少一次 CI / Deploy Check / Release Gates / E2E workflow,"
    echo "  然后再运行本脚本;或手动指定:"
    echo "  CONTEXTS_JSON='[\"CI / test (3.11)\",\"Deploy Check / verify-deploy\",...]' $0"
    exit 1
  fi
  CONTEXTS_JSON="$DETECT_OUTPUT"
fi

# 3.3 输出待配置的 contexts 供用户确认
CONTEXTS_COUNT=$(echo "$CONTEXTS_JSON" | jq 'length')
if [ "$CONTEXTS_COUNT" -lt 1 ]; then
  echo "ERROR: contexts 列表为空,无法配置 branch protection"
  exit 1
fi

echo ""
echo "=== 即将配置的 status check contexts (${CONTEXTS_COUNT} 个) ==="
echo "$CONTEXTS_JSON" | jq -r '.[]' | sed 's/^/  - /'
echo ""

# 3.4 R48 P0-2 软警告:四个核心 workflow 至少各有一个 context 覆盖
# (警告不阻断,允许用户自定义子集)
MISSING_COVERAGE=()
for prefix in "CI /" "Deploy Check /" "Release Gates /" "E2E Tests /"; do
  if ! echo "$CONTEXTS_JSON" | jq -e --arg p "$prefix" \
        'any(.[]; startswith($p))' > /dev/null 2>&1; then
    MISSING_COVERAGE+=("$prefix")
  fi
done
if [ "${#MISSING_COVERAGE[@]}" -gt 0 ]; then
  echo "WARN: 以下 workflow 前缀未被 contexts 覆盖:"
  for p in "${MISSING_COVERAGE[@]}"; do
    echo "  - $p"
  done
  echo "  这可能导致对应 workflow 的失败无法阻断合并。"
  echo "  若为有意为之,可忽略本警告。"
  echo ""
fi

# 3.5 R64 P0-01 / P1-11 软警告:Release Gates 14 个 job + CI / repo-hygiene 覆盖
# Release Gates workflow 实际 job 名必须与 release-gates.yml 完全一致(14 个 job):
#   docker-build / docker-digest-verify / compose-config / redis-acl-matrix / schema-diff /
#   backup-restore-drill / sbom / pip-audit / trivy / sign-image / verify-branch-protection /
#   rc-continuity / publish-attestation / release-summary
# CI workflow 必须包含 repo-hygiene(R64 P1-11 required context)。
# 注意:sign-image / publish-attestation / release-summary 仅在 push 到 master/main 时运行,
#      PR 场景或未触发过的 master 上可能缺失,属正常情况(soft WARN)。
EXPECTED_RG_JOBS=(
  "docker-build"
  "docker-digest-verify"
  "compose-config"
  "redis-acl-matrix"
  "schema-diff"
  "backup-restore-drill"
  "sbom"
  "pip-audit"
  "trivy"
  "sign-image"
  "verify-branch-protection"
  "rc-continuity"
  "publish-attestation"
  "release-summary"
)
MISSING_RG_JOBS=()
for rg_job in "${EXPECTED_RG_JOBS[@]}"; do
  expected_ctx="Release Gates / ${rg_job}"
  if ! echo "$CONTEXTS_JSON" | jq -e --arg c "$expected_ctx" \
        'any(.[]; . == $c)' > /dev/null 2>&1; then
    MISSING_RG_JOBS+=("$expected_ctx")
  fi
done
if [ "${#MISSING_RG_JOBS[@]}" -gt 0 ]; then
  echo "WARN: 以下 Release Gates job context 未在待配置 contexts 中:"
  for c in "${MISSING_RG_JOBS[@]}"; do
    echo "  - $c"
  done
  echo "  这可能导致对应 job 的失败无法阻断合并(R64 P0-01/P1-11 要求 14 个 job 全覆盖)。"
  echo "  若为有意为之(如 PR 场景配置子集),可忽略本警告。"
  echo ""
fi
if ! echo "$CONTEXTS_JSON" | jq -e 'any(.[]; . == "CI / repo-hygiene")' > /dev/null 2>&1; then
  echo "WARN: CI / repo-hygiene 未在待配置 contexts 中(R64 P1-11 required context)"
  echo ""
fi

# ─── 4. 构造 PUT /branches/master/protection 的 payload ───
# 严格包含 R47 P0-2 要求的所有字段:
#   - required_status_checks.strict = true
#   - enforce_admins = true
#   - required_pull_request_reviews.required_approving_review_count >= 1
#   - allow_force_pushes = false
#   - allow_deletions = false
PAYLOAD=$(jq -n --argjson contexts "$CONTEXTS_JSON" '{
  required_status_checks: {
    strict: true,
    contexts: $contexts
  },
  enforce_admins: true,
  required_pull_request_reviews: {
    required_approving_review_count: 1,
    dismiss_stale_reviews: true,
    require_code_owner_reviews: false
  },
  restrictions: null,
  allow_force_pushes: false,
  allow_deletions: false,
  required_linear_history: false,
  block_creations: false
}')

# ─── 5. 调用 GitHub API 配置 branch protection ───
echo "Configuring branch protection for ${OWNER}/${REPO}/master..."
if [ "$USE_GH_CLI" = "true" ]; then
  RESPONSE=$(gh api "repos/${OWNER}/${REPO}/branches/master/protection" \
              -X PUT --input - <<< "$PAYLOAD")
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

# R48 P0-2: 验证 BP 中实际配置的 contexts 与传入的 contexts 完全一致(集合相等)
# 旧版只断言"必需 context 存在",但 GitHub 可能保留历史 context,导致脏配置。
# 新版断言集合完全相等,确保没有多余/缺失的 context。
echo "Assert: required_status_checks.contexts 集合与预期一致"
DIFF=$(jq -n --argjson bp "$BP_JSON" \
          --argjson expected "$CONTEXTS_JSON" \
        '($bp.required_status_checks.contexts | sort) as $actual |
         ($expected | sort) as $exp |
         {only_in_bp: ($actual - $exp), only_in_expected: ($exp - $actual)}')

ONLY_IN_BP=$(echo "$DIFF" | jq '.only_in_bp | length')
ONLY_IN_EXP=$(echo "$DIFF" | jq '.only_in_expected | length')

if [ "$ONLY_IN_BP" -gt 0 ] || [ "$ONLY_IN_EXP" -gt 0 ]; then
  echo "ERROR: required_status_checks.contexts 与预期不一致"
  echo "$DIFF" | jq '.'
  exit 1
fi

echo ""
echo "✓ 所有 R47/R48 P0-2 断言通过"
echo ""
echo "最终配置(关键字段):"
echo "$BP_JSON" | jq '.required_status_checks, .enforce_admins, .required_pull_request_reviews, .allow_force_pushes, .allow_deletions'
