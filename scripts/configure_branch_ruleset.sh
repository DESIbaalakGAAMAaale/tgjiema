#!/usr/bin/env bash
# R71 P1-01/02/03 (Wave 6): 配置 GitHub Repository Ruleset — Solo Founder 模式
# (单一 ruleset,无审批死锁,current-SHA strict merge,全 release gates 必需)
#
# R71 P1-01/02/03 整改说明(审计报告要求):
#   旧版 R67 P0-01 + R70 Wave 10 配置了两个 ruleset:
#     1. R67 P0-01 Branch Immutability Ruleset — required_approving_review_count: 2
#     2. r70-governance-master-protect — required_approving_review_count: 1 + code_owner_review: true
#   对于 solo founder (@maxiuquan 是唯一开发者),这造成审批死锁:
#   唯一维护者无法合并自己的 PR(无人能批准)。
#
#   R71 Wave 6 整改:用单一 "R71 Solo Founder Branch Ruleset" 替换两个旧 ruleset:
#     - required_approving_review_count: 0(solo founder,无审批死锁)
#     - require_code_owner_review: false(CODEOWNERS 保留但不阻断)
#     - dismiss_stale_reviews_on_push: true(新 push 时作废旧 approval)
#     - required_review_thread_resolution: true(conversation 必须解决)
#     - required_status_checks.strict_required_status_checks_policy: true(current-SHA,不允许 stale parent commit)
#     - required_status_checks.required_status_checks: 覆盖所有真实 release-gates.yml job 名
#     - bypass_actors: [](无 admin/app bypass;紧急情况通过 scripts/record_break_glass.py 审计日志)
#     - deletion / non_fast_forward / required_signatures: 启用(不可变历史 + 签名)
#     - 不配置 update restriction: GitHub 会同时阻止 PR merge，PR-only 由 pull_request rule 保证
#
#   本脚本通过 GitHub REST API(POST /repos/{owner}/{repo}/rulesets
#   或 PUT /repos/{owner}/{repo}/rulesets/{id})配置单一 Repository Ruleset,
#   针对 refs/heads/master 与 refs/heads/main。
#
#   R71 fix: API field is strict_required_status_checks_policy (not strict_merge),
#            required_approving_review_count (not required_reviewers),
#            required_status_checks (not required_checks).
#            GitHub REST API 文档不完整;go-github 源码(google/go-github/github/rules.go)确认:
#            - RequiredStatusChecksRuleParameters.StrictRequiredStatusChecksPolicy
#              JSON "strict_required_status_checks_policy" (非指针 bool, 无 omitempty → REQUIRED)
#            - PullRequestRuleParameters.RequiredApprovingReviewCount
#              JSON "required_approving_review_count"
#            - required_status_checks 数组字段 JSON key 为 "required_status_checks" (非 "required_checks")
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
# 退出码: 0 成功 / 1 API 失败或参数错误。
set -euo pipefail

# ─── R71 Solo Founder Ruleset 配置 ───
RULESET_NAME="${RULESET_NAME:-R71 Solo Founder Branch Ruleset}"
RULESET_DESCRIPTION="${RULESET_DESCRIPTION:-R71 P1-01/02/03: Solo-founder governance ruleset for @maxiuquan — 0 required reviewers (no approval deadlock), strict current-SHA merge, all release gates required, no admin bypass. Break-glass via scripts/record_break_glass.py audit log.}"

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [--dry-run] [OWNER] [REPO]

R71 P1-01/02/03 (Wave 6): 为 refs/heads/master 与 refs/heads/main 配置单一
GitHub Repository Ruleset("R71 Solo Founder Branch Ruleset")。

R71 Solo Founder 必需的规则(solo-founder 模式,无审批死锁):
  - deletion             false — 禁止删除 master/main
  - non_fast_forward     false — 禁止 force push(历史不可变)
  - update               absent — 不启用 branch update restriction；该规则会阻止 PR merge
  - required_signatures  true  — 所有推送 commit 必须经过 GPG 签名验证
  - pull_request         0 名 reviewer(solo founder,无审批死锁)
                         + dismiss_stale_reviews_on_push=true
                         + require_code_owner_review=false(CODEOWNERS 保留但不阻断)
                         + required_review_thread_resolution=true
  - required_status_checks  strict_required_status_checks_policy=true(current-SHA,不允许 stale parent commit)
                         + 28 个 required_status_checks(仅 PR/master 事件可产生的 check,
                           R72 P1-06 已移除 tag-only/environment-only 的 check 如
                           compose-runtime-e2e / sign-image / publish-attestation /
                           attestation-semantics-verify / verify-only-3x /
                           migration-binding-gate / verify-rc-identity /
                           production-promotion-gate;
                           R74 P1-06: promote-rc-reusable → validate-promotion-workflow;
                           R76 P1-07: 新增 secretless-crdb-closed-loop-gate)
  - required_linear_history true  — 强制线性历史(无 merge commit,squash/rebase only)
  - bypass_actors        []    — 禁止任何角色(包括 admin)bypass;
                         紧急情况通过 scripts/record_break_glass.py 审计日志

参数(可选,可通过环境变量或位置参数指定):
  OWNER  仓库 owner(默认从 gh repo view / git remote 推断)
  REPO   仓库名(默认从 gh repo view / git remote 推断)

标志:
  --dry-run   仅打印 ruleset payload,不调用 gh api(用于审计/测试)
  --help, -h  显示帮助信息

环境变量:
  OWNER                  仓库 owner
  REPO                   仓库名
  RULESET_NAME           ruleset 名称(默认: "R71 Solo Founder Branch Ruleset")
  RULESET_DESCRIPTION    ruleset 描述
  REQUIRED_REVIEWERS     PR 必需 reviewer 数量(默认: 0,solo founder)
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT(admin scope,若未设置则使用 gh CLI)

鉴权(二选一):
  - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
  - 或 gh CLI 已登录(gh auth login with admin scope)

幂等性:
  若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。

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
      echo "ERROR: [R71 Wave 6] 未知 flag: $1"
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
  echo "ERROR: [R71 Wave 6] 无法确定 OWNER / REPO"
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
      echo "ERROR: [R71 Wave 6] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
      echo "  提示: 使用 --dry-run 可跳过鉴权与 API 调用"
      exit 1
    fi
  else
    # dry-run 模式: 不需要鉴权,但仍检查 jq 可用
    if ! command -v jq >/dev/null 2>&1; then
      echo "ERROR: [R71 Wave 6] 需要 jq(即使 --dry-run 模式也需构造 payload)"
      exit 1
    fi
  fi
fi

# ─── 3. 必需 reviewer 数量(R71 P1-01: 默认 0,solo founder) ───
REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-0}"
if ! [[ "$REQUIRED_REVIEWERS" =~ ^[0-9]+$ ]] || [ "$REQUIRED_REVIEWERS" -lt 0 ]; then
  echo "ERROR: [R71 P1-01] REQUIRED_REVIEWERS 必须为非负整数(实际: $REQUIRED_REVIEWERS)"
  exit 1
fi

# ─── 4. R71 Solo Founder 必需 status checks(28 项,仅 PR/master 事件可产生的 check) ───
# R71 P1-02: 必需 status checks 列表必须完整,覆盖所有 PR/master 事件实际可产生的
# release-gates.yml job 名(以及 ci / e2e / deploy-check 中的 job)。
# R72 P1-06: 移除以下 tag-only / environment-only 的 check(它们不在 PR/master 事件
#   产出 check,会造成合并死锁):
#   - compose-runtime-e2e / sign-image / publish-attestation /
#     attestation-semantics-verify / verify-only-3x / migration-binding-gate:
#     RC-only job(if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/rc-v'))
#   - verify-rc-identity / production-promotion-gate: environment-only job
#     (if: github.event_name == 'workflow_dispatch' || startsWith(github.ref, 'refs/tags/production-v'))
# 保留 R71 Wave 4 新增的 validate-oci-rootfs(RC61 移除 R71 Wave 7 的 bind-runtime-config —
# 其 if: 条件在 PR 事件下不满足,导致 strict_required_status_checks_policy=true 阻断 PR 合并)。
# R74 P1-06: promote-rc-reusable 替换为 validate-promotion-workflow
#   (promotion job 是 workflow_dispatch only,不能作为 PR required context)
# R76 P1-07: 新增 secretless-crdb-closed-loop-gate (CRDB 闭环证据门禁)
# R76 P1-08: 实际 ruleset (19723336) 必须收敛到本期望列表(27→28 项)
REQUIRED_STATUS_CHECKS="${REQUIRED_STATUS_CHECKS:-}"
if [ -z "$REQUIRED_STATUS_CHECKS" ]; then
  REQUIRED_STATUS_CHECKS='["lint","static-gates","test (3.10)","test (3.11)","test (3.12)","docker-build","docker-digest-verify","compose-config","redis-acl-matrix","schema-diff","restore-legacy-seal-gate","i18n-strict-export-boundary-gate","migration-manifest-gate","button-flow-real-ux-gate","backup-restore-drill","sbom","pip-audit","trivy","verify-branch-protection","verify-branch-ruleset","rc-continuity","crdb-ru-72h-attribution-gate","validate-promotion-workflow","oci-allowlist-verify","validate-oci-rootfs","runtime-smoke-compose","secretless-crdb-closed-loop-gate","release-summary"]'
fi
# 校验是合法 JSON 数组
if ! echo "$REQUIRED_STATUS_CHECKS" | jq -e 'type == "array" and length >= 1' > /dev/null 2>&1; then
  echo "ERROR: [R71 P1-02] REQUIRED_STATUS_CHECKS 必须为非空 JSON 数组(实际: $REQUIRED_STATUS_CHECKS)"
  exit 1
fi
# 校验元素均为字符串
if ! echo "$REQUIRED_STATUS_CHECKS" | jq -e 'all(.[]; type == "string")' > /dev/null 2>&1; then
  echo "ERROR: [R71 P1-02] REQUIRED_STATUS_CHECKS 数组元素必须全部为字符串"
  exit 1
fi
# R71 P1-02 / R72 P1-06 / RC61 / R76 P1-07/P1-08: 校验至少包含 28 个 context
# (仅 PR/master 事件可产生的 check,已移除 8 个 tag-only/environment-only 的 check:
#   compose-runtime-e2e / sign-image / publish-attestation /
#   attestation-semantics-verify / verify-only-3x / migration-binding-gate /
#   verify-rc-identity / production-promotion-gate;
# RC61: 再移除 4 个 PR-skipped 的 check: sign-artifacts / verify-git-source-governance /
#   tag-ruleset-verify / bind-runtime-config;
# R74 P1-06: promote-rc-reusable → validate-promotion-workflow;
# R76 P1-07: 新增 secretless-crdb-closed-loop-gate)
REQUIRED_CHECKS_COUNT=$(echo "$REQUIRED_STATUS_CHECKS" | jq 'length')
if [ "$REQUIRED_CHECKS_COUNT" -lt 28 ]; then
  echo "ERROR: [R71 P1-02] REQUIRED_STATUS_CHECKS 至少需要 28 个 context(仅 PR/master 事件可产生的 release-gates.yml job),实际: $REQUIRED_CHECKS_COUNT"
  exit 1
fi

# ─── 5. 构造 R71 Solo Founder ruleset payload ───
# R71 P1-01/02/03 + R72 P1-06:
#   - required_approving_review_count: 0(solo founder,无审批死锁)
#   - require_code_owner_review: false(CODEOWNERS 保留但不阻断)
#   - strict_required_status_checks_policy: true(current-SHA,不允许 stale parent commit)
#   - required_linear_history: true(强制线性历史,无 merge commit,squash/rebase only,
#     与 .github/branch_ruleset.expected.json 一致 — R72 P1-06 修复: 旧 payload 缺失此 rule)
#   - do_not_enforce_on_create: false(与 expected.json 一致,创建 PR 时仍强制 check)
#   - bypass_actors: [](无 admin/app bypass;紧急情况通过 record_break_glass.py 审计日志)
PAYLOAD=$(jq -n \
  --arg name "$RULESET_NAME" \
  --arg description "$RULESET_DESCRIPTION" \
  --argjson required_reviewers "$REQUIRED_REVIEWERS" \
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
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_signatures"},
    {"type": "required_linear_history"},
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": ($required_reviewers),
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "required_status_checks": ($required_checks | map({context: .})),
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false
      }
    }
  ]
}')

# ─── 6. --dry-run 模式:打印 payload 后退出 ───
if [ "$DRY_RUN" = "true" ]; then
  echo ""
  echo "=========================================================="
  echo "  DRY RUN — 不调用 gh api,仅打印 payload 供审计"
  echo "  R71 P1-01/02/03 Solo Founder Branch Ruleset"
  echo "=========================================================="
  echo ""
  echo "=== R71 Solo Founder Branch Ruleset payload ==="
  echo "$PAYLOAD" | jq '.'
  echo ""
  echo "=========================================================="
  echo "  DRY RUN 完成 — 未调用任何 gh api"
  echo "  实际应用: 去掉 --dry-run 重新运行本脚本"
  echo "=========================================================="
  exit 0
fi

# ════════════════════════════════════════════════════════════════
# 7. R71 Solo Founder: 幂等配置(查找现有 ruleset,PUT 更新或 POST 创建)
# ════════════════════════════════════════════════════════════════
echo ""
echo "=== 配置 R71 Solo Founder Branch Ruleset ==="
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
  echo "ERROR: [R71 Wave 6] 列出 rulesets 失败,GitHub API 响应:"
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
    echo "[WARN] 将复用此 ruleset id 并覆盖其配置为 R71 Solo Founder Ruleset"
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
  echo "ERROR: [R71 Wave 6] Ruleset 配置失败,GitHub API 响应:"
  echo "$RESPONSE"
  exit 1
fi

RULESET_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "✓ [R71 Wave 6] Ruleset 配置成功(id=${RULESET_ID})"

# ─── 8. R71 Solo Founder 配置后自检 ───
echo ""
echo "=== 验证 R71 Solo Founder 配置(关键断言) ==="
RULESET_JSON="$RESPONSE"

echo "Assert: target == branch"
echo "$RULESET_JSON" | jq -e '.target == "branch"' > /dev/null \
  || { echo "ERROR: [R71 P1-01] target != branch"; exit 1; }

echo "Assert: enforcement == active"
echo "$RULESET_JSON" | jq -e '.enforcement == "active"' > /dev/null \
  || { echo "ERROR: [R71 P1-01] enforcement != active"; exit 1; }

echo "Assert: conditions.ref_name.include 包含 refs/heads/master 与 refs/heads/main"
echo "$RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/heads/master") != null' > /dev/null \
  || { echo "ERROR: [R71 P1-01] conditions.ref_name.include 不包含 refs/heads/master"; exit 1; }
echo "$RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/heads/main") != null' > /dev/null \
  || { echo "ERROR: [R71 P1-01] conditions.ref_name.include 不包含 refs/heads/main"; exit 1; }

echo "Assert: rules 包含 deletion (deletion=false, master/main 不可删除)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "deletion")' > /dev/null \
  || { echo "ERROR: [R71 P1-01] rules 缺少 deletion 类型"; exit 1; }

echo "Assert: rules 包含 non_fast_forward (non_fast_forward=false, 禁止 force push)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "non_fast_forward")' > /dev/null \
  || { echo "ERROR: [R71 P1-01] rules 缺少 non_fast_forward 类型"; exit 1; }

echo "Assert: rules 不含 update restriction (允许合规 PR merge)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "update") | not' > /dev/null \
  || { echo "ERROR: [R83] branch update restriction 会阻止 PR merge"; exit 1; }

echo "Assert: rules 包含 required_signatures (强制 GPG 签名验证)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_signatures")' > /dev/null \
  || { echo "ERROR: [R71 P1-01] rules 缺少 required_signatures 类型"; exit 1; }

echo "Assert: rules 包含 required_linear_history (强制线性历史,无 merge commit)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_linear_history")' > /dev/null \
  || { echo "ERROR: [R72 P1-06] rules 缺少 required_linear_history 类型(与 expected.json 不一致)"; exit 1; }

echo "Assert: rules 包含 pull_request (PR-only 流程)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "pull_request")' > /dev/null \
  || { echo "ERROR: [R71 P1-01] rules 缺少 pull_request 类型"; exit 1; }

echo "Assert: pull_request.required_approving_review_count == 0 (solo founder, 无审批死锁)"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.required_approving_review_count] | add == 0' > /dev/null \
  || { echo "ERROR: [R71 P1-01] pull_request.required_approving_review_count != 0 (solo founder 要求 0)"; exit 1; }

echo "Assert: pull_request.require_code_owner_review == false (CODEOWNERS 保留但不阻断)"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.require_code_owner_review] | add == false' > /dev/null \
  || { echo "ERROR: [R71 P1-01] pull_request.require_code_owner_review != false"; exit 1; }

echo "Assert: pull_request.dismiss_stale_reviews_on_push == true"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.dismiss_stale_reviews_on_push] | add == true' > /dev/null \
  || { echo "ERROR: [R71 P1-01] pull_request.dismiss_stale_reviews_on_push != true"; exit 1; }

echo "Assert: pull_request.required_review_thread_resolution == true"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "pull_request") | .parameters.required_review_thread_resolution] | add == true' > /dev/null \
  || { echo "ERROR: [R71 P1-01] pull_request.required_review_thread_resolution != true"; exit 1; }

echo "Assert: rules 包含 required_status_checks"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_status_checks")' > /dev/null \
  || { echo "ERROR: [R71 P1-02] rules 缺少 required_status_checks 类型"; exit 1; }

echo "Assert: required_status_checks.strict_required_status_checks_policy == true (current-SHA, 不允许 stale parent commit)"
echo "$RULESET_JSON" | jq -e '[.rules[] | select(.type == "required_status_checks") | .parameters.strict_required_status_checks_policy] | add == true' > /dev/null \
  || { echo "ERROR: [R71 P1-03] required_status_checks.strict_required_status_checks_policy != true"; exit 1; }

echo "Assert: required_status_checks 包含全部 28 个必需 check (R71 P1-02 / R72 P1-06 / R76 P1-07/P1-08)"
ACTUAL_CHECKS=$(echo "$RULESET_JSON" \
  | jq -r '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | sort | join(",")')
EXPECTED_CHECKS=$(echo "$REQUIRED_STATUS_CHECKS" | jq -r 'sort | join(",")')
if [ "$ACTUAL_CHECKS" != "$EXPECTED_CHECKS" ]; then
  echo "ERROR: [R71 P1-02] required_status_checks.contexts 与预期不一致"
  echo "  实际:   $ACTUAL_CHECKS"
  echo "  预期:   $EXPECTED_CHECKS"
  exit 1
fi

echo "Assert: required_status_checks 不包含 tag-only / environment-only / PR-skipped 的 check (R72 P1-06 + RC61)"
# R72 P1-06: 以下 check 不在 PR/master 事件产出,会造成合并死锁,必须移除
# RC61: 追加 4 个 PR-skipped 的 check(sign-artifacts / verify-git-source-governance /
# tag-ruleset-verify / bind-runtime-config — 其 if: 条件在 PR 事件下不满足)
for ctx in "compose-runtime-e2e" "sign-image" "publish-attestation" "attestation-semantics-verify" "verify-only-3x" "migration-binding-gate" "verify-rc-identity" "production-promotion-gate" "sign-artifacts" "verify-git-source-governance" "tag-ruleset-verify" "bind-runtime-config"; do
  echo "$RULESET_JSON" | jq -e --arg c "$ctx" \
    '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | any(. == $c)' > /dev/null \
    && { echo "ERROR: [R72 P1-06/RC61] required_status_checks 不应包含 tag-only/environment-only/PR-skipped check: $ctx"; exit 1; }
done

echo "Assert: required_status_checks 包含 R71 Wave 4 新增 check (PR/master 事件可产生)"
# RC61: 移除 R71 Wave 7 的 bind-runtime-config(其 if: 条件在 PR 事件下不满足)
for ctx in "validate-oci-rootfs"; do
  echo "$RULESET_JSON" | jq -e --arg c "$ctx" \
    '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | any(. == $c)' > /dev/null \
    || { echo "ERROR: [R71 P1-02] required_status_checks 缺少 R71 新增 check: $ctx"; exit 1; }
done

echo "Assert: bypass_actors 为空(禁止任何角色 bypass,包括 admin;紧急情况通过 record_break_glass.py 审计日志)"
echo "$RULESET_JSON" | jq -e '(.bypass_actors // []) | length == 0' > /dev/null \
  || { echo "ERROR: [R71 P1-01] bypass_actors 非空(应禁止任何角色 bypass)"; exit 1; }

echo ""
echo "✓ [R71 P1-01/02/03 + R72 P1-06] 所有断言通过"

# ════════════════════════════════════════════════════════════════
# 9. R73 §5.13: Ruleset reconciliation — export actual ruleset,
#    compute digest, compare with .github/branch_ruleset.expected.json
# ════════════════════════════════════════════════════════════════
echo ""
echo "=== R73 §5.13: Ruleset reconciliation (expected vs actual) ==="

EXPECTED_FILE=".github/branch_ruleset.expected.json"
if [ ! -f "$EXPECTED_FILE" ]; then
  echo "ERROR: [R73 §5.13] Expected ruleset baseline not found: $EXPECTED_FILE"
  exit 1
fi

# Re-export actual ruleset JSON via gh api(独立于 PUT/POST 响应,确保读到最新状态)
if [ "$USE_GH_CLI" = "true" ]; then
  ACTUAL_RULESET_JSON=$(gh api "repos/${OWNER}/${REPO}/rulesets/${RULESET_ID}")
else
  ACTUAL_RULESET_JSON=$(curl -sS \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${OWNER}/${REPO}/rulesets/${RULESET_ID}")
fi

if ! echo "$ACTUAL_RULESET_JSON" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R73 §5.13] 重新导出 ruleset JSON 失败(id=${RULESET_ID})"
  echo "$ACTUAL_RULESET_JSON"
  exit 1
fi

# 提取语义字段(剥离 metadata:_comment / _schema / _r71_solo_founder_requirements /
# id / node_id / created_at / updated_at / _links / current_user_can_* 等)
# 仅对比:name, description, target, source_type, enforcement, conditions, rules, bypass_actors
ACTUAL_SEMANTIC=$(echo "$ACTUAL_RULESET_JSON" \
  | jq -S -c '{name, description, target, source_type, enforcement, conditions, rules, bypass_actors}')
EXPECTED_SEMANTIC=$(jq -S -c '{name, description, target, source_type, enforcement, conditions, rules, bypass_actors}' "$EXPECTED_FILE")

# 计算 SHA-256 digest(跨平台兼容:sha256sum 优先,回退到 shasum -a 256)
_compute_sha256() {
  local input="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$input" | sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$input" | shasum -a 256 | cut -d' ' -f1
  else
    echo "ERROR: [R73 §5.13] sha256sum / shasum 均不可用" >&2
    return 1
  fi
}

ACTUAL_DIGEST=$(_compute_sha256 "$ACTUAL_SEMANTIC") || exit 1
EXPECTED_DIGEST=$(_compute_sha256 "$EXPECTED_SEMANTIC") || exit 1

MATCHED=false
if [ "$EXPECTED_DIGEST" = "$ACTUAL_DIGEST" ]; then
  MATCHED=true
fi

# 写入 reconciliation 报告(.github/ruleset-reconciliation.json)
TMP_ACTUAL=$(mktemp)
printf '%s' "$ACTUAL_RULESET_JSON" > "$TMP_ACTUAL"
jq -n \
  --arg ruleset_id "$RULESET_ID" \
  --arg expected_digest "sha256:$EXPECTED_DIGEST" \
  --arg actual_digest "sha256:$ACTUAL_DIGEST" \
  --argjson matched "$MATCHED" \
  --slurpfile actual "$TMP_ACTUAL" \
  '{
    reconciliation_schema: "r73-5-13-ruleset-reconciliation",
    ruleset_id: ($ruleset_id | tonumber),
    expected_digest: $expected_digest,
    actual_digest: $actual_digest,
    matched: $matched,
    reconciled_at: (now | todate),
    actual_ruleset_json: $actual[0]
  }' > .github/ruleset-reconciliation.json
rm -f "$TMP_ACTUAL"

echo "[INFO] Reconciliation 报告: .github/ruleset-reconciliation.json"
jq '{ruleset_id, expected_digest, actual_digest, matched}' .github/ruleset-reconciliation.json

if [ "$MATCHED" != "true" ]; then
  echo "ERROR: [R73 §5.13] Ruleset digest 不匹配 — 实际 ruleset 与 expected baseline 不一致"
  echo "  expected_digest: sha256:$EXPECTED_DIGEST"
  echo "  actual_digest:   sha256:$ACTUAL_DIGEST"
  echo "  expected semantic JSON: $EXPECTED_SEMANTIC"
  echo "  actual semantic JSON:   $ACTUAL_SEMANTIC"
  exit 1
fi
echo "✓ [R73 §5.13] Ruleset digest 与 expected baseline 匹配"

echo ""
echo "最终配置(关键字段):"
echo "$RULESET_JSON" | jq '{id, name, target, source_type, enforcement, conditions, rules, bypass_actors}'

echo ""
echo "=========================================================="
echo "  ✓ R71 Solo Founder Branch Ruleset 已配置成功"
echo "  P1-01: required_approving_review_count=0 (无审批死锁)"
echo "  P1-02: 28 个 required_status_checks (仅 PR/master 事件可产生的 check)"
echo "  P1-03: strict_required_status_checks_policy=true (current-SHA)"
echo "  R72 P1-06: required_linear_history=true (强制线性历史)"
echo "  R72 P1-06: 已移除 8 个 tag-only/environment-only check (避免合并死锁)"
echo "  R74 P1-06: promote-rc-reusable → validate-promotion-workflow"
echo "  R76 P1-07: 新增 secretless-crdb-closed-loop-gate (CRDB 闭环证据门禁)"
echo "  R76 P1-08: 实际 ruleset 收敛到本期望列表"
echo "  bypass_actors=[] (无 admin bypass; 紧急情况用 record_break_glass.py)"
echo "=========================================================="
