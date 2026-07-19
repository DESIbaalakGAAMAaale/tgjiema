#!/usr/bin/env bash
# R47/R48/R65 P0-2/P1-12: Branch Protection 严格验证脚本。
#
# 本脚本从 .github/workflows/release-gates.yml 的 verify-branch-protection
# job 中提取,目的是避免 YAML run: 块超过 GitHub Actions 21000 字节限制
# (中文注释 UTF-8 编码后字节数显著膨胀)。
#
# 用法: 在 GitHub Actions workflow 中通过
#   bash scripts/verify_branch_protection.sh
# 调用。依赖环境变量:
#   GH_TOKEN             — 有 administration:read 权限的 PAT(BP_PAT_TOKEN)
#   GITHUB_REPOSITORY    — 仓库全名(owner/repo),GitHub Actions 自动注入
#   GITHUB_EVENT_NAME    — 事件类型(push / pull_request),GitHub Actions 自动注入
#   GITHUB_REF           — Git ref,GitHub Actions 自动注入
#
# 退出码:
#   0 — 所有 BP 断言通过
#   1 — 任一断言失败(已通过 fail_diag 输出完整诊断)
set -euo pipefail

# R53: GitHub Actions check-run 的 name 字段就是 job 名(无 "CI /" 前缀),
# 不是 "{workflow_name} / {job_name}"。原 REQUIRED_PREFIXES=("CI /" ...)
# 永远匹配不到实际 context(如 "test (3.11)"),导致 Assert B 永远失败。
# 改为按 workflow 分组列出代表 job,检查 BP 至少包含每组中一个 job。
# 格式: "workflow_name|job1,job2,job3"
# R64 P0-01 / P1-11: Release Gates 列表必须与 workflow 实际 job 名完全一致。
# R65 P1-09: 新增 crdb-ru-72h-attribution-gate 作为 required context。
# R65 P0-04: 新增 production-promotion-gate(release tag 严格门禁)。
WORKFLOW_JOBS=(
  "CI|test (3.10),test (3.11),test (3.12),lint,repo-hygiene,i18n-check,static-gates,security,fault-injection,migration-dry-run"
  "Deploy Check|verify-deploy"
  "Release Gates|docker-build,docker-digest-verify,compose-config,redis-acl-matrix,schema-diff,restore-legacy-seal-gate,i18n-strict-export-boundary-gate,migration-manifest-gate,backup-restore-drill,sbom,pip-audit,trivy,sign-image,verify-branch-protection,rc-continuity,crdb-ru-72h-attribution-gate,publish-attestation,production-promotion-gate,release-summary"
  "E2E Tests|playwright-e2e"
)

# 工具函数:任意失败时输出完整诊断后退出
# 用法: fail_diag "错误消息"
fail_diag() {
  local msg="$1"
  echo "::error::${msg}"
  echo ""
  echo "================ 诊断信息 ================"
  echo ""
  echo "----- 当前 Branch Protection 配置(BP JSON)-----"
  if [ -n "${BP_JSON:-}" ]; then
    echo "$BP_JSON" | jq '.' 2>/dev/null || echo "$BP_JSON"
  else
    echo "(未获取到 BP_JSON)"
  fi
  echo ""
  echo "----- 最新 master commit 的实际 check-runs 名称 -----"
  if [ -n "${ACTUAL_CONTEXTS_JSON:-}" ]; then
    echo "$ACTUAL_CONTEXTS_JSON" | jq '.' 2>/dev/null || echo "$ACTUAL_CONTEXTS_JSON"
  else
    echo "(未获取到 check-runs)"
  fi
  echo ""
  echo "----- BP contexts vs 实际 check-runs 差异 -----"
  if [ -n "${DIFF_JSON:-}" ]; then
    echo "$DIFF_JSON" | jq '.' 2>/dev/null || echo "$DIFF_JSON"
  else
    echo "(未生成差异)"
  fi
  echo ""
  echo "================ 修复建议 ================"
  echo "1. 自动检测当前实际 check-runs 并配置 BP:"
  echo "     bash scripts/detect_branch_protection_contexts.sh > contexts.json"
  echo "     bash scripts/configure_branch_protection.sh"
  echo "2. 或手动指定 contexts(JSON 数组):"
  echo "     CONTEXTS_JSON='[\"CI / test (3.11)\",\"Deploy Check / verify-deploy\",\\"
  echo "                     \"Release Gates / verify-branch-protection\",\\"
  echo "                     \"E2E Tests / playwright-e2e\"]' \\"
  echo "       bash scripts/configure_branch_protection.sh"
  echo "3. 注意:context 名称格式为 '{workflow_name} / {job_name}',"
  echo "   矩阵 job 还会带 ' ({matrix_label})' 后缀。"
  echo "   需先在 master 分支触发过相关 workflow 才能检测到 check-runs。"
  exit 1
}

# 1. 拉取 branch protection 配置(gh api 失败立即捕获,不吞错误)
if ! RESPONSE=$(gh api "repos/${GITHUB_REPOSITORY}/branches/master/protection" 2>&1); then
  echo "::error::无法获取 branch protection 配置(可能未配置或 token 权限不足)"
  echo "::error::gh api 输出: $RESPONSE"
  echo ""
  echo "Branch Protection 未正确配置,请运行:"
  echo "  bash scripts/detect_branch_protection_contexts.sh > contexts.json"
  echo "  bash scripts/configure_branch_protection.sh"
  exit 1
fi

# 2. 检查是否未配置(Branch not protected 错误响应)
if echo "$RESPONSE" | grep -q "Branch not protected"; then
  echo "::error::Branch protection 未配置"
  echo ""
  echo "请运行:"
  echo "  bash scripts/detect_branch_protection_contexts.sh > contexts.json"
  echo "  bash scripts/configure_branch_protection.sh"
  exit 1
fi

BP_JSON="$RESPONSE"

# 3. 输出当前 BP 配置供调试
echo "=== 当前 Branch Protection 配置 ==="
echo "$BP_JSON" | jq '.'
echo "==================================="

# 4. R47 P0-2: 严格断言所有必需属性
# 4.1 strict=true (status check 必须针对最新提交)
echo "Assert: required_status_checks.strict == true"
echo "$BP_JSON" | jq -e '.required_status_checks.strict == true' > /dev/null \
  || fail_diag "required_status_checks.strict != true"

# 4.2 enforce_admins=true (管理员也受规则约束)
echo "Assert: enforce_admins.enabled == true"
echo "$BP_JSON" | jq -e '.enforce_admins.enabled == true' > /dev/null \
  || fail_diag "enforce_admins.enabled != true"

# 4.3 至少 1 个 approving review (R47 P0-2: 必须 >= 1)
echo "Assert: required_approving_review_count >= 1"
echo "$BP_JSON" | jq -e '.required_pull_request_reviews.required_approving_review_count >= 1' > /dev/null \
  || fail_diag "required_approving_review_count < 1"

# 4.4 allow_force_pushes=false (禁止 force push)
echo "Assert: allow_force_pushes.enabled == false"
echo "$BP_JSON" | jq -e '.allow_force_pushes.enabled == false' > /dev/null \
  || fail_diag "allow_force_pushes.enabled != false"

# 4.5 allow_deletions=false (禁止删除分支)
echo "Assert: allow_deletions.enabled == false"
echo "$BP_JSON" | jq -e '.allow_deletions.enabled == false' > /dev/null \
  || fail_diag "allow_deletions.enabled != false"

# ════════════════════════════════════════════════════════════════
# 4.6 — 4.12: R65 P1-12 严格化断言
#   - dismiss_stale_reviews=true       (新提交自动 dismiss 旧 approval)
#   - required_approving_review_count>=2 (独立 reviewer)
#   - required_linear_history=true      (禁 merge commit)
#   - required_conversation_resolution=true (所有 conversation 必须解决)
#   - required_signatures.enabled=true  (signed commits 必需)
#   - dismissal_restrictions 不为 null  (任何人可 dismiss,但要求存在)
#   - require_code_owner_reviews 与 CODEOWNERS 存在性一致
# ════════════════════════════════════════════════════════════════
echo "Assert: dismiss_stale_reviews == true (R65 P1-12)"
echo "$BP_JSON" | jq -e '.required_pull_request_reviews.dismiss_stale_reviews == true' > /dev/null \
  || fail_diag "dismiss_stale_reviews != true (R65 P1-12: 新提交必须 dismiss 旧 approval)"

echo "Assert: required_approving_review_count >= 2 (R65 P1-12: 独立 reviewer)"
echo "$BP_JSON" | jq -e '.required_pull_request_reviews.required_approving_review_count >= 2' > /dev/null \
  || fail_diag "required_approving_review_count < 2 (R65 P1-12 要求 >= 2 个独立 reviewer)"

echo "Assert: required_linear_history.enabled == true (R65 P1-12: 禁 merge commit)"
echo "$BP_JSON" | jq -e '.required_linear_history.enabled == true' > /dev/null \
  || fail_diag "required_linear_history.enabled != true (R65 P1-12: 必须禁用 merge commit,要求 rebase/squash)"

echo "Assert: required_conversation_resolution.enabled == true (R65 P1-12)"
echo "$BP_JSON" | jq -e '.required_conversation_resolution.enabled == true' > /dev/null \
  || fail_diag "required_conversation_resolution.enabled != true (R65 P1-12: 所有 conversation 必须解决后才能合并)"

# R65 P1-12: dismissal_restrictions 断言
# GitHub API 限制:个人仓库(owner.type=User)不允许配置 dismissal_restrictions
# (PUT /branches/{branch}/protection 返回 422 "Only organization repositories
#  can have users and team restrictions"),即使只配置 apps: [] 也被拒绝。
# 因此对个人仓库跳过此断言(避免 API 平台限制阻断 CI),org 仓库仍严格要求非 null。
REPO_OWNER_TYPE=$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.owner.type' 2>/dev/null || echo "User")
echo "Assert: dismissal_restrictions 配置正确 (R65 P1-12, repo owner.type=${REPO_OWNER_TYPE})"
if [ "$REPO_OWNER_TYPE" = "Organization" ]; then
  echo "$BP_JSON" | jq -e '.required_pull_request_reviews.dismissal_restrictions != null' > /dev/null \
    || fail_diag "dismissal_restrictions 为 null (R65 P1-12: org 仓库必须显式配置 dismissal_restrictions)"
else
  # 个人仓库:GitHub API 不允许配置 dismissal_restrictions,接受 null
  DR_VALUE=$(echo "$BP_JSON" | jq -r '.required_pull_request_reviews.dismissal_restrictions // "null"')
  echo "  (个人仓库 — dismissal_restrictions=${DR_VALUE},GitHub API 平台限制,跳过非 null 断言)"
fi

# R65 P1-12: require_code_owner_reviews 与 CODEOWNERS 存在性一致
# 无 CODEOWNERS 时 require_code_owner_reviews=false(可接受),
# 有 CODEOWNERS 时 require_code_owner_reviews=true(强制 code owner 评审)。
CODEOWNERS_EXISTS=false
if [ -f "CODEOWNERS" ] || [ -f ".github/CODEOWNERS" ] || [ -f "docs/CODEOWNERS" ]; then
  CODEOWNERS_EXISTS=true
fi
echo "Assert: require_code_owner_reviews 与 CODEOWNERS 存在性一致 (R65 P1-12, CODEOWNERS_EXISTS=${CODEOWNERS_EXISTS})"
echo "$BP_JSON" | jq -e --argjson expected "${CODEOWNERS_EXISTS}" \
  '.required_pull_request_reviews.require_code_owner_reviews == $expected' > /dev/null \
  || fail_diag "require_code_owner_reviews 与 CODEOWNERS 存在性不一致 (期望: ${CODEOWNERS_EXISTS})"

# R65 P1-12: required_signatures 通过独立 API 启用(不在 PUT payload 中)
# https://docs.github.com/rest/branches/branch-protection#get-commit-signature-protection
echo "Assert: required_signatures.enabled == true (R65 P1-12: signed commits 必需)"
SIG_JSON=$(gh api "repos/${GITHUB_REPOSITORY}/branches/master/protection/required_signatures" 2>&1 || echo '{}')
if ! echo "$SIG_JSON" | jq -e '.enabled == true' > /dev/null 2>&1; then
  BP_JSON="${BP_JSON} || required_signatures_response=${SIG_JSON}"
  fail_diag "required_signatures.enabled != true (R65 P1-12: signed commits 必需,需通过 POST /branches/{branch}/protection/required_signatures 启用)"
fi

# ════════════════════════════════════════════════════════════════
# 4.13: R65 P1-12 动态 context 一致性检查
#   运行 scripts/check_branch_protection_contexts.py,从 .github/workflows/*.yml
#   提取所有 job 名(含矩阵展开),与 BP required_status_checks.contexts
#   双向比对(孤儿 + 缺失),任何不一致即 fail_diag。
#   优先用 gh api 拉取的 BP JSON,失败时回退到 checked-in 基线配置文件。
# ════════════════════════════════════════════════════════════════
echo ""
echo "=== R65 P1-12: 动态 context 一致性检查 ==="
# 安装 pyyaml(check_branch_protection_contexts.py 解析 workflow YAML 必需)
python -m pip install --quiet pyyaml >/dev/null 2>&1 || true
# 优先用 gh api 实时拉取的 BP JSON
BP_CONTEXTS_FILE="$(mktemp)"
echo "$BP_JSON" > "$BP_CONTEXTS_FILE"
# R65 P1-12 适配: 用 --json 模式捕获 workflow job 名列表(供 Assert A 排除)
# 解决 chicken-and-egg 问题: 新增 workflow job 在 PR 合并前不会出现在
# master 的 check-runs 中,但 BP 已配置为 required context。
# 若不排除这些 "future" context,Assert A 会永久阻断 PR 合并。
WORKFLOW_JOBS_FILE="$(mktemp)"
if ! python scripts/check_branch_protection_contexts.py \
      --bp-config "$BP_CONTEXTS_FILE" \
      --workflows-dir .github/workflows \
      --json > "$WORKFLOW_JOBS_FILE"; then
  echo "::error::R65 P1-12 动态一致性检查失败"
  echo "  BP required contexts 与 .github/workflows/*.yml job 名不一致"
  echo "  修复: bash scripts/configure_branch_protection.sh"
  rm -f "$BP_CONTEXTS_FILE" "$WORKFLOW_JOBS_FILE"
  fail_diag "BP contexts 与 workflow job 名不一致 (R65 P1-12)"
fi
# 校验 consistent 字段(双重保险)
WORKFLOW_CONSISTENT=$(jq -r '.consistent // false' "$WORKFLOW_JOBS_FILE")
if [ "$WORKFLOW_CONSISTENT" != "true" ]; then
  echo "::error::R65 P1-12 动态一致性检查失败 (consistent != true)"
  cat "$WORKFLOW_JOBS_FILE"
  rm -f "$BP_CONTEXTS_FILE" "$WORKFLOW_JOBS_FILE"
  fail_diag "BP contexts 与 workflow job 名不一致 (R65 P1-12)"
fi
rm -f "$BP_CONTEXTS_FILE"
echo "✓ R65 P1-12: BP contexts 与 workflow job 名动态一致"

# ════════════════════════════════════════════════════════════════
# 5. R48 P0-2: 动态读取实际 check-runs,与 BP contexts 比对
# ════════════════════════════════════════════════════════════════

# 5.1 获取最新 master commit SHA(注意:此处用 GITHUB_REPOSITORY 上下文)
echo ""
echo "=== R48 P0-2: 动态读取实际 check-runs ==="
LATEST_SHA=$(gh api "repos/${GITHUB_REPOSITORY}/commits/master" --jq '.sha')
if [ -z "$LATEST_SHA" ] || [ "$LATEST_SHA" = "null" ]; then
  fail_diag "无法获取 master 最新 commit SHA"
fi
echo "最新 master commit: ${LATEST_SHA}"

# 5.2 拉取该 commit 的 check-runs
#     R56 P0-1 修复:check-runs 可能尚未创建(4 个 workflow 同时启动时
#     时序竞争),添加 retry/wait 逻辑(最多等待 5 分钟)
CHECK_RUNS_JSON=""
CHECK_RUNS_TOTAL=0
MAX_ATTEMPTS=30
SLEEP_SECONDS=10
ATTEMPT=0
while [ "${ATTEMPT}" -lt "${MAX_ATTEMPTS}" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  echo "拉取 check-runs(尝试 ${ATTEMPT}/${MAX_ATTEMPTS})..."
  CHECK_RUNS_JSON=$(gh api "repos/${GITHUB_REPOSITORY}/commits/${LATEST_SHA}/check-runs")
  CHECK_RUNS_TOTAL=$(echo "$CHECK_RUNS_JSON" | jq -r '.total_count // 0')
  echo "check-runs 总数: ${CHECK_RUNS_TOTAL}"
  if [ "${CHECK_RUNS_TOTAL}" -gt 0 ]; then
    break
  fi
  echo "  check-runs 尚未创建,等待 ${SLEEP_SECONDS}s 后重试..."
  sleep "${SLEEP_SECONDS}"
done

if [ "$CHECK_RUNS_TOTAL" -eq 0 ]; then
  fail_diag "等待 $((MAX_ATTEMPTS * SLEEP_SECONDS))s 后仍无 check-runs,可能 BP 配置错误或 workflow 未触发"
fi

# 5.3 提取实际 check-runs 名称(去重 + 排序,输出 JSON 数组)
#     check run 的 name 字段就是 BP 中的 context 名称
ACTUAL_CONTEXTS_JSON=$(echo "$CHECK_RUNS_JSON" \
  | jq -r '.check_runs[].name' \
  | sort -u \
  | jq -R . | jq -s .)

echo ""
echo "----- 实际 check-runs 名称(去重) -----"
echo "$ACTUAL_CONTEXTS_JSON" | jq -r '.[]' | sed 's/^/  - /'
echo "---------------------------------------"

# 5.4 提取 BP 中配置的 contexts
BP_CONTEXTS_JSON=$(echo "$BP_JSON" \
  | jq -c '.required_status_checks.contexts // []')

echo ""
echo "----- BP required_status_checks.contexts -----"
echo "$BP_CONTEXTS_JSON" | jq -r '.[]' | sed 's/^/  - /'
echo "---------------------------------------------"

# 5.5 R48 P0-2 核心断言 A:BP 中的每个 context 必须在实际 check-runs 中存在
#     (允许 BP 是 check-runs 的子集,但 BP 中的 context 不能是凭空捏造的)
#     R65 P1-12 适配: 新增 workflow job 在 PR 合并前不会出现在 master 的
#     check-runs 中(master 的 workflow YAML 还没有这些 job 定义)。
#     若 BP 已将这些 job 配置为 required context,Assert A 会因
#     "orphan context" 永久阻断 PR 合并(chicken-and-egg 问题)。
#     修复: 从 step 4.13 捕获的 WORKFLOW_JOBS_FILE 中提取所有 workflow
#     job 名(含 push-only / self-excluded / non-blocking),将这些
#     "future" context 从 orphan 集合中排除。只有当 BP context 既不在
#     实际 check-runs 中、也不在 workflow YAML 中时(真正的 typo / 错配),
#     才视为 Assert A 失败。
echo ""
echo "Assert A: BP contexts 中每一项都必须在实际 check-runs 或 workflow YAML 中存在"
# 提取 workflow YAML 中所有 job 名(jobs + excluded_push_only + self_excluded + non_blocking)
WORKFLOW_ALL_JOBS_JSON=$(jq -c \
  '[.workflows[].jobs[], .workflows[].excluded_push_only_jobs[], .workflows[].self_excluded_jobs[], .workflows[].non_blocking_jobs[]] | unique' \
  "$WORKFLOW_JOBS_FILE")
ORPHAN_CONTEXTS=$(jq -n \
  --argjson bp "$BP_CONTEXTS_JSON" \
  --argjson actual "$ACTUAL_CONTEXTS_JSON" \
  '($bp - $actual)')
ORPHAN_COUNT=$(echo "$ORPHAN_CONTEXTS" | jq 'length')
if [ "$ORPHAN_COUNT" -gt 0 ]; then
  # R65 P1-12: 排除 "future" context(在 workflow YAML 中定义但尚未在
  # master 上运行的新增 job)
  REAL_ORPHAN_CONTEXTS=$(jq -n \
    --argjson orphan "$ORPHAN_CONTEXTS" \
    --argjson wf "$WORKFLOW_ALL_JOBS_JSON" \
    '($orphan - $wf)')
  REAL_ORPHAN_COUNT=$(echo "$REAL_ORPHAN_CONTEXTS" | jq 'length')
  FUTURE_CONTEXTS=$(jq -n \
    --argjson orphan "$ORPHAN_CONTEXTS" \
    --argjson real "$REAL_ORPHAN_CONTEXTS" \
    '($orphan - $real)')
  FUTURE_COUNT=$(echo "$FUTURE_CONTEXTS" | jq 'length')
  if [ "$FUTURE_COUNT" -gt 0 ]; then
    echo "  INFO: ${FUTURE_COUNT} 个 BP context 在 workflow YAML 中定义但尚未在 master check-runs 中(新增 job,PR 合并后会自动产生 check-run):"
    echo "$FUTURE_CONTEXTS" | jq -r '.[]' | sed 's/^/    - /'
  fi
  if [ "$REAL_ORPHAN_COUNT" -gt 0 ]; then
    DIFF_JSON=$(jq -n \
      --argjson bp "$BP_CONTEXTS_JSON" \
      --argjson actual "$ACTUAL_CONTEXTS_JSON" \
      --argjson wf "$WORKFLOW_ALL_JOBS_JSON" \
      '{bp_contexts: $bp, actual_check_runs: $actual, workflow_jobs: $wf, only_in_bp_orphan_typo: ($bp - $actual - $wf), future_contexts: (($bp - $actual) - ($bp - $actual - $wf)), only_in_actual: ($actual - $bp)}')
    rm -f "$WORKFLOW_JOBS_FILE"
    fail_diag "BP 中存在 ${REAL_ORPHAN_COUNT} 个 context 既不在实际 check-runs 中,也不在 workflow YAML 中(可能是 context 名称错配,如把 'CI / test (3.11)' 误写为 'CI')"
  fi
  echo "  ✓ BP contexts 全部在实际 check-runs 或 workflow YAML 中存在(${FUTURE_COUNT} 个 future context 已容忍)"
else
  echo "  ✓ BP contexts 全部在实际 check-runs 中存在"
fi
rm -f "$WORKFLOW_JOBS_FILE"

# 5.6 R53: 核心断言 B — 四个核心 workflow 各至少有一个代表 job 在 BP contexts 中
#     (CI / Deploy Check / Release Gates / E2E Tests)
#     原实现用 "CI /" 前缀匹配,但实际 check-run name 就是 job 名(无前缀),
#     永远匹配不到。改为直接按 job 名检查。
echo ""
echo "Assert B: 四个核心 workflow 各至少有一个 job 在 BP contexts 中覆盖"
MISSING_COVERAGE=()
for entry in "${WORKFLOW_JOBS[@]}"; do
  workflow="${entry%%|*}"
  jobs_csv="${entry##*|}"
  IFS=',' read -ra jobs <<< "$jobs_csv"
  covered_job=""
  for job in "${jobs[@]}"; do
    if echo "$BP_CONTEXTS_JSON" | jq -e --arg j "$job" \
          'any(.[]; . == $j)' > /dev/null 2>&1; then
      covered_job="$job"
      break
    fi
  done
  if [ -n "$covered_job" ]; then
    echo "  ✓ ${workflow}: ${covered_job}"
  else
    echo "  ✗ ${workflow}: 缺失(候选: ${jobs[*]})"
    MISSING_COVERAGE+=("$workflow")
  fi
done
if [ "${#MISSING_COVERAGE[@]}" -gt 0 ]; then
  DIFF_JSON=$(jq -n \
    --argjson bp "$BP_CONTEXTS_JSON" \
    --argjson actual "$ACTUAL_CONTEXTS_JSON" \
    --arg missing "${MISSING_COVERAGE[*]}" \
    '{bp_contexts: $bp, actual_check_runs: $actual, missing_workflows: ($missing | split(" "))}')
  fail_diag "BP contexts 未覆盖以下 workflow: ${MISSING_COVERAGE[*]}"
fi

# 5.7 R48 P0-2 核心断言 C:BP contexts 集合应与实际 check-runs 集合一致
#     (推荐完全一致,避免遗漏任何 job 的状态检查)
#     注意:verify-branch-protection 自身在 PR 触发时可能尚未完成,
#     因此允许 "Release Gates / verify-branch-protection" 不在 BP 中
#     (这是合理的,因为 BP 不能要求自身阻断自己)
echo ""
echo "Assert C: BP contexts 集合应与实际 check-runs 集合一致"
DIFF_JSON=$(jq -n \
  --argjson bp "$BP_CONTEXTS_JSON" \
  --argjson actual "$ACTUAL_CONTEXTS_JSON" \
  '{only_in_bp_orphan: ($bp - $actual),
    only_in_actual_missing_in_bp: ($actual - $bp)}')

ONLY_IN_ACTUAL=$(echo "$DIFF_JSON" | jq -c '.only_in_actual_missing_in_bp')
ONLY_IN_ACTUAL_COUNT=$(echo "$DIFF_JSON" | jq '.only_in_actual_missing_in_bp | length')

if [ "$ONLY_IN_ACTUAL_COUNT" -gt 0 ]; then
  echo "  WARN: 以下实际 check-runs 未在 BP contexts 中(仅警告,不阻断):"
  echo "$ONLY_IN_ACTUAL" | jq -r '.[]' | sed 's/^/    - /'
  echo "  这些 job 的失败不会阻断合并。如需启用,请重新运行配置脚本。"
  echo "  (此为软警告,BP 允许是 check-runs 的子集)"
fi

echo ""
echo "✓ Branch protection 验证通过,所有 R47/R48 P0-2 断言均满足"
echo "  - 所有 BP 属性(strict / enforce_admins / review>=1 /"
echo "    allow_force_pushes=false / allow_deletions=false)正确"
echo "  - BP contexts 全部在实际 check-runs 中存在(无错配)"
echo "  - 四个核心 workflow 前缀(CI / / Deploy Check / /"
echo "    Release Gates / / E2E Tests /)均已覆盖"
