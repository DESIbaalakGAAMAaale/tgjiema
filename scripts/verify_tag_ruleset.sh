#!/usr/bin/env bash
# R66 P1-11: Tag Ruleset 严格验证脚本。
#
# 本脚本验证 GitHub Repository Ruleset 已正确配置为标签不可变
# (禁止删除 / 禁止移动 / 禁止更新 / 创建限制 / 强制 GPG 签名)。
#
# R66 P1-11 审计要求:
#   为 tags 配置不可变、禁止删除/移动的 ruleset;
#   生产 environment 需要独立审批、最小权限和 digest-pinned deploy。
#   禁止用 master 的 required checks 推导 tag 已满足相同条件。
#
# 用法: 在 GitHub Actions workflow 或本地通过
#   bash scripts/verify_tag_ruleset.sh
# 调用。依赖环境变量:
#   GH_TOKEN / GITHUB_TOKEN  — 有 administration:read 权限的 PAT(可选)
#   GITHUB_REPOSITORY        — 仓库全名(owner/repo),GitHub Actions 自动注入
#   OWNER / REPO             — 本地运行时可通过环境变量指定
#
# 退出码:
#   0 — 所有断言通过
#   1 — 任一断言失败(已通过 fail_diag 输出完整诊断)
set -euo pipefail

RULESET_NAME="${RULESET_NAME:-R66 P1-11 Tag Immutability Ruleset}"

# 工具函数:任意失败时输出完整诊断后退出
# 用法: fail_diag "错误消息"
fail_diag() {
  local msg="$1"
  echo "::error::${msg}"
  echo ""
  echo "================ 诊断信息 ================"
  echo ""
  echo "----- 当前 Repository Rulesets 列表 -----"
  if [ -n "${RULESETS_JSON:-}" ]; then
    echo "$RULESETS_JSON" | jq '.' 2>/dev/null || echo "$RULESETS_JSON"
  else
    echo "(未获取到 rulesets 列表)"
  fi
  echo ""
  echo "----- 匹配的 Tag Ruleset(若找到)-----"
  if [ -n "${TAG_RULESET_JSON:-}" ]; then
    echo "$TAG_RULESET_JSON" | jq '.' 2>/dev/null || echo "$TAG_RULESET_JSON"
  else
    echo "(未找到匹配的 tag ruleset)"
  fi
  echo ""
  echo "================ 修复建议 ================"
  echo "1. 配置 tag ruleset(创建 / 更新不可变规则):"
  echo "     bash scripts/configure_tag_ruleset.sh"
  echo "2. 或手动通过 GitHub REST API 创建:"
  echo "     POST /repos/{owner}/{repo}/rulesets"
  echo "     target=tags, conditions.ref_name.include=['refs/tags/*']"
  echo "     rules 包含 creation/deletion/non_fast_forward/update/required_signatures"
  echo "3. R66 P1-11 要求:"
  echo "   - 禁止用 master required checks 推导 tag 已满足相同条件"
  echo "   - 生产 environment 需独立审批、最小权限和 digest-pinned deploy"
  exit 1
}

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [OWNER] [REPO]

R66 P1-11: 验证 GitHub Repository Ruleset 已正确配置为标签不可变。

断言:
  1. Ruleset 存在,target == "tags",conditions.ref_name.include 包含 refs/tags/*
  2. rules 包含 deletion 类型       (deletion=false, 禁止删除 tag)
  3. rules 包含 non_fast_forward 类型 (non_fast_forward=false, 禁止移动 tag)
  4. rules 包含 update 类型         (update=false, 禁止更新 tag, 若字段存在)
  5. rules 包含 required_signatures 类型 (强制 GPG 签名验证, 若字段存在)
  6. rules 包含 creation 类型       (创建限制)

参数(可选,可通过环境变量或位置参数指定):
  OWNER  仓库 owner
  REPO   仓库名

环境变量:
  OWNER / REPO          本地运行时指定仓库
  GITHUB_REPOSITORY     GitHub Actions 自动注入(owner/repo 格式)
  RULESET_NAME          期望的 ruleset 名称(默认: "${RULESET_NAME}")
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT(若未设置则使用 gh CLI 登录凭证)

退出码:
  0  所有断言通过
  1  任一断言失败
EOF
}

# ─── 0. 解析 flags(--strict / --dry-run / --help) ───
# R71 RC5 fix: 之前 --strict 被当作 OWNER 位置参数,导致 API 调用
#   repos/--strict/tgjiema/rulesets → 404。
# 现在先提取 flags,再解析位置参数。
STRICT_MODE=false
DRY_RUN=false
POSITIONAL_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --strict)
      STRICT_MODE=true
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    --help|-h)
      print_help
      exit 0
      ;;
    *)
      POSITIONAL_ARGS+=("$arg")
      ;;
  esac
done
# 重置位置参数为非 flag 参数,后续 ${1:-} / ${2:-} 只引用位置参数
set -- "${POSITIONAL_ARGS[@]}"

if [ "$STRICT_MODE" = "true" ]; then
  echo "[R66 P1-11] Strict mode enabled — all assertions must pass"
fi
if [ "$DRY_RUN" = "true" ]; then
  echo "[R66 P1-11] Dry-run mode — failures will still exit 1 (use || in caller for non-fatal)"
fi

# ─── 1. 解析 OWNER / REPO(GITHUB_REPOSITORY 优先,其次环境变量,最后位置参数 / gh repo view / git remote) ───
# GITHUB_REPOSITORY 格式为 "owner/repo"(GitHub Actions 自动注入)
# R71 RC5 fix: 移除 ${1:-} 依赖 — flags 已在上方提取,$1 现在只引用位置参数。
if [ -n "${GITHUB_REPOSITORY:-}" ] && [ -z "${OWNER:-}" ]; then
  OWNER="${GITHUB_REPOSITORY%%/*}"
  REPO="${GITHUB_REPOSITORY#*/}"
fi
OWNER="${OWNER:-${1:-}}"
REPO="${REPO:-${2:-}}"

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  # 优先用 gh repo view --json owner,name 推断
  if command -v gh >/dev/null 2>&1; then
    REPO_INFO=$(gh repo view --json owner,name 2>/dev/null || true)
    if [ -n "$REPO_INFO" ]; then
      OWNER="${OWNER:-$(echo "$REPO_INFO" | jq -r '.owner.login // empty')}"
      REPO="${REPO:-$(echo "$REPO_INFO" | jq -r '.name // empty')}"
    fi
  fi
fi

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  # 从 git remote origin 推断
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
  echo "ERROR: [R66 P1-11] 无法确定 OWNER / REPO"
  echo "  用法 1: GITHUB_REPOSITORY=owner/repo $0"
  echo "  用法 2: OWNER=owner REPO=repo $0"
  echo "  用法 3: $0 owner repo"
  exit 1
fi

# ─── 2. 鉴权(token 优先,否则用 gh CLI 已登录的凭证) ───
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
USE_GH_CLI=true
if [ -n "$TOKEN" ]; then
  USE_GH_CLI=false
else
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: [R66 P1-11] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
    exit 1
  fi
fi

# R71 RC3 fix: 诊断输出 — token 长度和前缀(不暴露完整 token)
# 用于排查 BP_PAT_TOKEN secret 是否正确传递到 CI
if [ -n "$TOKEN" ]; then
  TOKEN_LEN=${#TOKEN}
  TOKEN_PREFIX=$(echo "$TOKEN" | cut -c1-4)
  echo "[diag] TOKEN length=${TOKEN_LEN}, prefix=${TOKEN_PREFIX}***"
  # 验证 token 有效性:调用 /user 端点
  USER_LOGIN=$(curl -sS -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/user" 2>/dev/null | jq -r '.login // "FAILED"' 2>/dev/null || echo "CURL_ERROR")
  echo "[diag] /user login=${USER_LOGIN}"
else
  echo "[diag] TOKEN is empty, using gh CLI keyring credentials"
fi

# ─── 3. 拉取所有 repository rulesets ───
# GET /repos/{owner}/{repo}/rulesets 返回当前仓库的所有 ruleset 列表
if [ "$USE_GH_CLI" = "true" ]; then
  if ! RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets" 2>&1); then
    echo "::error::[R66 P1-11] 无法获取 repository rulesets 列表(可能 token 权限不足)"
    echo "::error::gh api 输出: $RESPONSE"
    echo ""
    echo "Tag Ruleset 未正确配置,请运行:"
    echo "  bash scripts/configure_tag_ruleset.sh"
    exit 1
  fi
else
  RESPONSE=$(curl -sS \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${OWNER}/${REPO}/rulesets")
  # R71 RC3 fix: 如果 curl 返回非数组(如 404),尝试用 gh CLI 作为 fallback
  if ! echo "$RESPONSE" | jq -e 'type == "array"' > /dev/null 2>&1; then
    echo "[diag] curl 返回非数组,尝试用 gh CLI fallback..."
    if command -v gh >/dev/null 2>&1; then
      GH_RESPONSE=$(gh api "repos/${OWNER}/${REPO}/rulesets" 2>&1 || echo "")
      if echo "$GH_RESPONSE" | jq -e 'type == "array"' > /dev/null 2>&1; then
        echo "[diag] gh CLI fallback 成功"
        RESPONSE="$GH_RESPONSE"
      else
        echo "[diag] gh CLI fallback 也失败: $GH_RESPONSE"
      fi
    fi
  fi
fi

# 校验响应是合法 JSON 数组(非数组表示 API 调用失败或权限不足)
if ! echo "$RESPONSE" | jq -e 'type == "array"' > /dev/null 2>&1; then
  echo "::error::[R66 P1-11] rulesets 响应不是数组(可能 token 权限不足或 API 调用失败)"
  echo "::error::响应: $RESPONSE"
  echo ""
  echo "Tag Ruleset 未正确配置,请运行:"
  echo "  bash scripts/configure_tag_ruleset.sh"
  exit 1
fi

RULESETS_JSON="$RESPONSE"

RULESETS_COUNT=$(echo "$RULESETS_JSON" | jq 'length')
echo "=== 当前 Repository Rulesets 列表(共 ${RULESETS_COUNT} 个)==="
echo "$RULESETS_JSON" | jq '.[] | {id, name, target, source_type, enforcement}' 2>/dev/null || true
echo "================================================="

# ─── 4. 查找 target=tags 且 conditions.ref_name.include 包含 refs/tags/* 的 ruleset ───
# 优先按 RULESET_NAME 查找;若未找到,则按 target=tags + ref_name 匹配任意 tag ruleset
TAG_RULESET_JSON=$(echo "$RULESETS_JSON" \
  | jq -c --arg name "$RULESET_NAME" \
    '.[] | select(.name == $name)' \
  | head -n 1)

if [ -z "$TAG_RULESET_JSON" ]; then
  # 未按名称找到,回退到按 target=tags + ref_name 匹配
  TAG_RULESET_JSON=$(echo "$RULESETS_JSON" \
    | jq -c \
      '.[] | select(.target == "tag" and ((.conditions.ref_name.include // []) | index("refs/tags/*") != null))' \
    | head -n 1)
fi

if [ -z "$TAG_RULESET_JSON" ]; then
  fail_diag "未找到 target=tags 且 conditions.ref_name.include 包含 refs/tags/* 的 ruleset (R66 P1-11)"
fi

RULESET_ID=$(echo "$TAG_RULESET_JSON" | jq -r '.id')
RULESET_NAME_ACTUAL=$(echo "$TAG_RULESET_JSON" | jq -r '.name')
echo ""
echo "=== 找到匹配的 Tag Ruleset (id=${RULESET_ID}, name='${RULESET_NAME_ACTUAL}') ==="
echo "$TAG_RULESET_JSON" | jq '.'
echo "========================================================================="

# ─── 5. R66 P1-11: 严格断言所有必需属性 ───

# 5.1 target == tag (GitHub API 返回单数 "tag",非 "tags")
echo "Assert: target == tag"
echo "$TAG_RULESET_JSON" | jq -e '.target == "tag"' > /dev/null \
  || fail_diag "target != tag (R66 P1-11: ruleset 必须针对 tags)"

# 5.2 enforcement == active
echo "Assert: enforcement == active"
echo "$TAG_RULESET_JSON" | jq -e '.enforcement == "active"' > /dev/null \
  || fail_diag "enforcement != active (R66 P1-11: ruleset 必须启用 active enforcement)"

# 5.3 conditions.ref_name.include 包含 refs/tags/*
echo "Assert: conditions.ref_name.include 包含 refs/tags/*"
echo "$TAG_RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/tags/*") != null' > /dev/null \
  || fail_diag "conditions.ref_name.include 不包含 refs/tags/* (R66 P1-11: 必须覆盖所有 tag)"

# 5.4 R66 P1-11 核心:deletion=false(tags 不可删除)
# ruleset API 中,deletion 规则的存在即表示"禁止删除"(deletion=false)
echo "Assert: rules 包含 deletion (deletion=false, tags 不可删除)"
echo "$TAG_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "deletion")' > /dev/null \
  || fail_diag "rules 缺少 deletion 类型 (R66 P1-11: deletion=false 未启用,tags 可被删除)"

# 5.5 R66 P1-11 核心:non_fast_forward=false(tags 不可移动)
echo "Assert: rules 包含 non_fast_forward (non_fast_forward=false, tags 不可移动)"
echo "$TAG_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "non_fast_forward")' > /dev/null \
  || fail_diag "rules 缺少 non_fast_forward 类型 (R66 P1-11: non_fast_forward=false 未启用,tags 可被移动)"

# 5.6 R66 P1-11 核心:update=false(tags 不可更新,若字段存在)
echo "Assert: rules 包含 update (update=false, tags 不可更新)"
echo "$TAG_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "update")' > /dev/null \
  || fail_diag "rules 缺少 update 类型 (R66 P1-11: update=false 未启用,tags 可被更新)"

# 5.7 R66 P1-11 核心:required_signatures 启用(强制 GPG 签名验证,若字段存在)
echo "Assert: rules 包含 required_signatures (强制 GPG 签名验证)"
echo "$TAG_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_signatures")' > /dev/null \
  || fail_diag "rules 缺少 required_signatures 类型 (R66 P1-11: 强制 GPG 签名验证未启用)"

# 5.8 R66 P1-11:creation 规则存在(创建限制)
echo "Assert: rules 包含 creation (创建限制)"
echo "$TAG_RULESET_JSON" | jq -e '[.rules[].type] | any(. == "creation")' > /dev/null \
  || fail_diag "rules 缺少 creation 类型 (R66 P1-11: 创建限制未启用,任意角色可创建 tag)"

# 5.9 软警告:bypass_actors 不为空(限制创建权限)
BYPASS_ACTORS_COUNT=$(echo "$TAG_RULESET_JSON" | jq -r '.bypass_actors // [] | length')
if [ "$BYPASS_ACTORS_COUNT" -eq 0 ]; then
  echo "WARN: bypass_actors 为空 — 任何角色均可 bypass creation 规则"
  echo "  R66 P1-11 建议:bypass_actors 仅包含 admin / release manager 角色"
else
  echo "✓ bypass_actors 包含 ${BYPASS_ACTORS_COUNT} 个 actor(创建权限已限制)"
fi

echo ""
echo "✓ [R66 P1-11] Tag Ruleset 验证通过,所有断言均满足"
echo "  - target=tags, enforcement=active, conditions.ref_name.include 包含 refs/tags/*"
echo "  - deletion=false (tags 不可删除)"
echo "  - non_fast_forward=false (tags 不可移动 / 不可变)"
echo "  - update=false (tags 不可更新)"
echo "  - required_signatures 启用 (强制 GPG 签名验证)"
echo "  - creation 规则存在 (创建限制)"
