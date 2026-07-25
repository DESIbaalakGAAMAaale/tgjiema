#!/usr/bin/env bash
# R71 P1-01/02/03 (Wave 6): 验证 GitHub Repository Ruleset 配置
# (Solo Founder 模式 — 0 reviewers / strict_merge=true / 29 contexts / no bypass)
#
# 本脚本在 CI 中运行,验证仓库实际配置的 branch ruleset 与
# .github/branch_ruleset.expected.json 期望配置一致(solo-founder 语义)。
# 任意属性不匹配即 fail-closed 退出 1。
#
# 整改要求(R71 P1-01/02/03 — Solo Founder):
#   - target == branch
#   - enforcement == active
#   - conditions.ref_name.include 包含 refs/heads/master 与 refs/heads/main
#   - rules 包含 deletion (deletion=false)
#   - rules 包含 non_fast_forward (non_fast_forward=false, 禁止 force push)
#   - rules 包含 update (update=false, 禁止直接 update)
#   - rules 包含 required_signatures (强制 GPG 签名验证)
#   - rules 包含 pull_request (R71 P1-01 solo-founder 语义):
#       * required_reviewers == 0 (solo founder, 无审批死锁)
#       * require_code_owner_review == false (CODEOWNERS 保留但不阻断)
#       * dismiss_stale_reviews_on_push == true
#       * required_review_thread_resolution == true
#   - rules 包含 required_status_checks (R71 P1-02/03):
#       * strict_merge == true (current-SHA, 不允许 stale parent commit)
#       * required_checks 包含 29 个 context (仅 PR/master 事件可产生的 release-gates.yml job 名)
#       * 必含 R71 Wave 4/7 新增: validate-oci-rootfs / bind-runtime-config
#       * R72 P1-06: 移除 8 个 tag-only/environment-only 的 check(compose-runtime-e2e /
#         sign-image / publish-attestation / attestation-semantics-verify / verify-only-3x /
#         migration-binding-gate / verify-rc-identity / production-promotion-gate)
#   - bypass_actors 为空(禁止任何角色 bypass,包括 admin;紧急情况通过 record_break_glass.py)
#
# 使用方法:
#   OWNER=maxiuquan REPO=tgjiema ./scripts/verify_branch_ruleset.sh
#   ./scripts/verify_branch_ruleset.sh maxiuquan tgjiema
#
# 退出码:
#   0  所有断言通过
#   1  任意断言失败或 API 错误
set -euo pipefail

RULESET_NAME="${RULESET_NAME:-R71 Solo Founder Branch Ruleset}"

# R71 P1-02 / R72 P1-06: 必需 status checks 列表(31 项,仅 PR/master 事件可产生的 check)
# 与 .github/branch_ruleset.expected.json / scripts/configure_branch_ruleset.sh 保持一致
# R72 P1-06: 移除 8 个 tag-only/environment-only 的 check(compose-runtime-e2e /
# sign-image / publish-attestation / attestation-semantics-verify / verify-only-3x /
# migration-binding-gate / verify-rc-identity / production-promotion-gate)
# R72 RC60: 'test' 拆分为 'test (3.10)'/'test (3.11)'/'test (3.12)' (29→31 项)
EXPECTED_REQUIRED_CHECKS=(
  "lint"
  "static-gates"
  "test (3.10)"
  "test (3.11)"
  "test (3.12)"
  "docker-build"
  "docker-digest-verify"
  "compose-config"
  "redis-acl-matrix"
  "schema-diff"
  "restore-legacy-seal-gate"
  "i18n-strict-export-boundary-gate"
  "migration-manifest-gate"
  "button-flow-real-ux-gate"
  "backup-restore-drill"
  "sbom"
  "pip-audit"
  "trivy"
  "sign-artifacts"
  "verify-branch-protection"
  "verify-branch-ruleset"
  "verify-git-source-governance"
  "rc-continuity"
  "tag-ruleset-verify"
  "crdb-ru-72h-attribution-gate"
  "production-evidence"
  "oci-allowlist-verify"
  "validate-oci-rootfs"
  "runtime-smoke-compose"
  "bind-runtime-config"
  "release-summary"
)

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [OWNER] [REPO]

R71 P1-01/02/03 (Wave 6): 验证 master/main branch Repository Ruleset 配置
(Solo Founder 模式 — 0 reviewers / strict_merge=true / 29 contexts / no bypass)。

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
  echo "ERROR: [R71 Wave 6] 无法确定 OWNER / REPO"
  exit 1
fi

# ─── 2. 鉴权 ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: [R71 Wave 6] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
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
  echo "ERROR: [R71 Wave 6] 列出 rulesets 失败,GitHub API 响应:"
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
  # R68 P0-02: 删除 PR 宽松模式 — 治理配置必须在合并前完成。
  # 无论 PR 还是 master push,ruleset 不存在均 fail-closed。
  # 仓库 admin 必须先运行 configure_branch_ruleset.sh,再开整改 PR。
  echo "FAIL: [R68 P0-02] 未找到 branch ruleset(期望 name='${RULESET_NAME}' 或 target=branch + refs/heads/master)"
  echo ""
  echo "当前仓库 rulesets 列表:"
  echo "$LIST_RESPONSE" | jq '[.[] | {id, name, target, enforcement}]'
  echo ""
  echo "修复:管理员必须运行(需 admin PAT with administration:write scope):"
  echo "  OWNER=${OWNER} REPO=${REPO} ./scripts/configure_branch_ruleset.sh"
  echo ""
  echo "此 PR 将保持失败,直到 ruleset 配置完成。这是 R68 P0-02 要求的 fail-closed 行为。"
  exit 1
fi

echo "[INFO] 找到 branch ruleset id=${RULESET_ID},获取详细配置..."

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
  echo "ERROR: [R71 Wave 6] 获取 ruleset 详情失败,GitHub API 响应:"
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

# assert_contains_ctx: 同 assert_contains,但接受额外的 jq --arg 参数
# 用法: assert_contains_ctx "name" "$json" "jq_expr" "arg_name" "arg_value"
assert_contains_ctx() {
  local name="$1" json="$2" jq_expr="$3" arg_name="$4" arg_value="$5"
  if echo "$json" | jq -e --arg "$arg_name" "$arg_value" "$jq_expr" > /dev/null 2>&1; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name"
    FAIL=1
  fi
}

echo ""
echo "=== R71 P1-01/02/03: Solo Founder Branch Ruleset 断言检查 ==="

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

# R71 P1-01: Solo Founder — required_approving_review_count == 0 (无审批死锁)
# R71 fix: API field is required_approving_review_count (not required_reviewers)
assert_contains "R71 P1-01: pull_request.required_approving_review_count == 0 (solo founder, 无审批死锁)" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.required_approving_review_count] | add == 0'

# R71 P1-01: Solo Founder — require_code_owner_review == false (CODEOWNERS 保留但不阻断)
assert_contains "R71 P1-01: pull_request.require_code_owner_review == false (CODEOWNERS 保留但不阻断)" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.require_code_owner_review] | add == false'

assert_contains "pull_request.dismiss_stale_reviews_on_push == true" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.dismiss_stale_reviews_on_push] | add == true'

assert_contains "pull_request.required_review_thread_resolution == true" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "pull_request") | .parameters.required_review_thread_resolution] | add == true'

# R71 P1-02 / R72 P1-06: required_status_checks 必须存在,且包含全部 29 个 context
assert_contains "rules 含 required_status_checks (R71 P1-02)" \
  "$RULESET_JSON" \
  '[.rules[].type] | any(. == "required_status_checks")'

# R71 P1-03: strict_required_status_checks_policy == true (current-SHA, 不允许 stale parent commit)
# R71 fix: API field is strict_required_status_checks_policy (not strict_merge)
#   go-github RequiredStatusChecksRuleParameters:
#     StrictRequiredStatusChecksPolicy bool `json:"strict_required_status_checks_policy"`
assert_contains "R71 P1-03: required_status_checks.strict_required_status_checks_policy == true (current-SHA)" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "required_status_checks") | .parameters.strict_required_status_checks_policy] | add == true'

# R71 P1-02: 每个 expected context 都必须在 required_status_checks 中
for ctx in "${EXPECTED_REQUIRED_CHECKS[@]}"; do
  assert_contains_ctx "R71 P1-02: required_status_checks 含 '${ctx}'" \
    "$RULESET_JSON" \
    '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | any(. == ($c))' \
    "c" "$ctx"
done

# R71 P1-02: 特别验证 R71 Wave 4/7 新增的 context(R72 P1-06 移除 Wave 2/5)
assert_contains "R71 Wave 4: required_status_checks 含 'validate-oci-rootfs'" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | any(. == "validate-oci-rootfs")'

assert_contains "R71 Wave 7: required_status_checks 含 'bind-runtime-config'" \
  "$RULESET_JSON" \
  '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | any(. == "bind-runtime-config")'

# bypass_actors 必须为空(禁止任何角色 bypass,包括 admin;紧急情况通过 record_break_glass.py)
assert_contains "R71 P1-01: bypass_actors 为空 (禁止 admin bypass; 紧急情况用 record_break_glass.py)" \
  "$RULESET_JSON" \
  '(.bypass_actors // []) | length == 0'

echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: [R71 P1-01/02/03] Solo Founder Branch Ruleset 断言失败"
  echo ""
  echo "实际配置:"
  echo "$RULESET_JSON" | jq '{id, name, target, enforcement, conditions, rules, bypass_actors}'
  exit 1
fi

echo "PASS: [R71 P1-01/02/03] Solo Founder Branch Ruleset 所有断言通过"
exit 0
