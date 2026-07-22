#!/usr/bin/env bash
# R66 P1-11: 配置 GitHub Repository Ruleset 实现标签不可变性
# (禁止删除 / 禁止移动 / 禁止更新 / 创建限制 / 强制 GPG 签名)
#
# R66 P1-11 整改说明(审计报告要求):
#   为 tags 配置不可变、禁止删除/移动的 ruleset;
#   生产 environment 需要独立审批、最小权限和 digest-pinned deploy。
#   禁止用 master 的 required checks 推导 tag 已满足相同条件。
#
#   本脚本通过 GitHub REST API(POST /repos/{owner}/{repo}/rulesets
#   或 PUT /repos/{owner}/{repo}/rulesets/{id})配置一个针对
#   refs/tags/* 的 Repository Ruleset,强制以下规则:
#     - creation          限制 tag 创建(仅 bypass_actors 可创建,如 admin / release manager)
#     - deletion          false — 禁止删除 tag(ruleset API 中规则存在即表示禁用)
#     - non_fast_forward  false — 禁止移动 / 更新 tag(不可变)
#     - update            false — 禁止更新 tag
#     - required_signatures  true — tag 所指向 commit 必须经过 GPG 签名验证
#
#   注意:required_signatures 规则验证的是 commit 签名。
#   若需强制 annotated tag 本身签名(git tag -s),建议在 CI 中
#   额外校验 tag 签名(pre-receive hook 或 GitHub Action)。
#
# 使用方法:
#   # 1) 通过环境变量指定 owner/repo
#   OWNER=maxiuquan REPO=tgjiema ./scripts/configure_tag_ruleset.sh
#   # 2) 通过位置参数指定
#   ./scripts/configure_tag_ruleset.sh maxiuquan tgjiema
#   # 3) 自动从 gh repo view / git remote 推断
#   ./scripts/configure_tag_ruleset.sh
#
# 鉴权(二选一):
#   - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
#   - 或 gh CLI 已登录(gh auth login)
#
# 幂等性: 若同名 ruleset 已存在(按 name 查找),则 PUT 更新;否则 POST 创建。
# 退出码: 0 成功 / 1 API 失败或参数错误。
set -euo pipefail

RULESET_NAME="${RULESET_NAME:-R66 P1-11 Tag Immutability Ruleset}"
RULESET_DESCRIPTION="${RULESET_DESCRIPTION:-R66 P1-11: 标签不可变 ruleset — 禁止删除/移动/更新,创建限制 + 强制 GPG 签名。生产环境需独立审批、最小权限和 digest-pinned deploy;禁止用 master required checks 推导 tag 已满足相同条件。}"

# ─── 帮助信息 ───
print_help() {
  cat <<EOF
用法: $0 [OWNER] [REPO]

R66 P1-11: 为 refs/tags/* 配置 GitHub Repository Ruleset,强制标签不可变性。

必需的规则:
  - creation             限制 tag 创建(仅 bypass_actors 可创建)
  - deletion             false — 禁止删除 tag
  - non_fast_forward     false — 禁止移动 / 更新 tag(不可变)
  - update               false — 禁止更新 tag
  - required_signatures  true  — tag 所指向 commit 必须经过 GPG 签名验证

参数(可选,可通过环境变量或位置参数指定):
  OWNER  仓库 owner(默认从 gh repo view / git remote 推断)
  REPO   仓库名(默认从 gh repo view / git remote 推断)

环境变量:
  OWNER                  仓库 owner
  REPO                   仓库名
  RULESET_NAME           ruleset 名称(默认: "${RULESET_NAME}")
  RULESET_DESCRIPTION    ruleset 描述
  BYPASS_ACTORS_JSON     bypass_actors JSON 数组(覆盖默认值)
                         示例: [{"actor_id":1,"actor_type":"RepositoryRole","bypass_mode":"always"}]
  GH_TOKEN / GITHUB_TOKEN  GitHub PAT(若未设置则使用 gh CLI 登录凭证)

鉴权(二选一):
  - 设置 GH_TOKEN / GITHUB_TOKEN 环境变量
  - 或 gh CLI 已登录(gh auth login)

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

# ─── 1. 解析 OWNER / REPO(环境变量优先,其次位置参数,最后 gh repo view / git remote 推断) ───
OWNER="${OWNER:-${1:-}}"
REPO="${REPO:-${2:-}}"

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  # 优先用 gh repo view --json owner,name 推断
  if command -v gh >/dev/null 2>&1; then
    REPO_INFO=$(gh repo view --json owner,name 2>/dev/null || true)
    if [ -n "$REPO_INFO" ]; then
      # gh repo view --json owner,name 返回 {"owner":{"login":"..."},"name":"..."}
      OWNER="${OWNER:-$(echo "$REPO_INFO" | jq -r '.owner.login // empty')}"
      REPO="${REPO:-$(echo "$REPO_INFO" | jq -r '.name // empty')}"
    fi
  fi
fi

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  # 从 git remote origin 推断
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
  echo "ERROR: [R66 P1-11] 无法确定 OWNER / REPO"
  echo "  用法 1: OWNER=owner REPO=repo $0"
  echo "  用法 2: $0 owner repo"
  echo "  或确保 git remote origin 指向 GitHub 仓库,或先 gh auth login"
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

# ─── 3. 构造 bypass_actors(限制 tag 创建权限) ───
# 默认仅允许仓库 owner(admin) bypass creation 规则。
# 用户可通过 BYPASS_ACTORS_JSON 环境变量覆盖。
# 参考: https://docs.github.com/rest/repos/rules#create-a-repository-ruleset
# bypass_mode:
#   - "always"         总是 bypass
#   - "pull_request"   通过 PR review 后 bypass
#
# R71 RC1 fix: RepositoryRole(actor_id=1) 在个人仓库中会报
# "Actor base role does not have write permissions"(HTTP 422)。
# 个人仓库的 owner 是 User 类型,不是 RepositoryRole。
# 因此默认改为动态获取仓库 owner 的 user ID,用 User 类型。
# 组织仓库仍可使用 BYPASS_ACTORS_JSON 环境变量传递 RepositoryRole:
#   BYPASS_ACTORS_JSON='[{"actor_id":1,"actor_type":"RepositoryRole","bypass_mode":"always"}]'
if [ -n "${BYPASS_ACTORS_JSON:-}" ]; then
  echo "[INFO] 使用用户通过 BYPASS_ACTORS_JSON 提供的 bypass_actors"
  # 校验是合法 JSON 数组
  if ! echo "$BYPASS_ACTORS_JSON" | jq -e 'type == "array"' > /dev/null 2>&1; then
    echo "ERROR: [R66 P1-11] BYPASS_ACTORS_JSON 不是合法的 JSON 数组"
    echo "  示例: BYPASS_ACTORS_JSON='[{\"actor_id\":1,\"actor_type\":\"RepositoryRole\",\"bypass_mode\":\"always\"}]' $0"
    exit 1
  fi
else
  # 默认 bypass_actors:动态获取仓库 owner user ID,用 User 类型 always bypass creation
  # R71 RC1 fix: 个人仓库 RepositoryRole(actor_id=1) 报 422,改用 User + owner ID
  if [ "$USE_GH_CLI" = "true" ]; then
    OWNER_USER_ID=$(gh api user --jq '.id' 2>/dev/null || echo "")
  else
    OWNER_USER_ID=$(curl -sS \
      -H "Authorization: token ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/user" 2>/dev/null | jq -r '.id // ""' 2>/dev/null || echo "")
  fi
  if [ -z "$OWNER_USER_ID" ]; then
    echo "ERROR: [R66 P1-11] 无法获取当前认证用户的 GitHub user ID"
    echo "  请通过 BYPASS_ACTORS_JSON 环境变量显式指定 bypass_actors"
    exit 1
  fi
  BYPASS_ACTORS_JSON=$(jq -n --argjson actor_id "$OWNER_USER_ID" \
    '[{"actor_id":$actor_id,"actor_type":"User","bypass_mode":"always"}]')
  echo "[INFO] 使用默认 bypass_actors(User id=${OWNER_USER_ID} always bypass creation)"
fi

# ─── 4. 构造 ruleset payload ───
# R66 P1-11 要求的规则:
#   - creation          限制 tag 创建(仅 bypass_actors 可创建)
#   - deletion          false — 禁止删除 tag
#   - non_fast_forward  false — 禁止移动 / 更新 tag(不可变)
#   - update            false — 禁止更新 tag
#   - required_signatures  true — tag 所指向 commit 必须经过 GPG 签名验证
#
# 注意:ruleset API 中,deletion / non_fast_forward / update / creation /
#       required_signatures 这些规则的存在即表示"启用 / 强制约束"(无 boolean 参数)。
#       它们的存在意味着:
#   - deletion 规则存在          → 禁止删除(deletion = false / 不允许)
#   - non_fast_forward 规则存在  → 禁止非快进移动(non_fast_forward = false / 不允许)
#   - update 规则存在            → 禁止更新(update = false / 不允许)
#   - creation 规则存在          → 限制创建(仅 bypass_actors 可创建)
#   - required_signatures 规则存在 → 强制 GPG 签名验证(required_signatures = true / 启用)
PAYLOAD=$(jq -n \
  --arg name "$RULESET_NAME" \
  --arg description "$RULESET_DESCRIPTION" \
  --argjson bypass_actors "$BYPASS_ACTORS_JSON" '{
  name: $name,
  description: $description,
  target: "tag",
  source_type: "Repository",
  enforcement: "active",
  bypass_actors: $bypass_actors,
  conditions: {
    ref_name: {
      include: ["refs/tags/*"],
      exclude: []
    }
  },
  rules: [
    {"type": "creation"},
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "update"},
    {"type": "required_signatures"}
  ]
}')

# ─── 5. 幂等性检查:查找同名 ruleset 是否已存在 ───
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
  echo "ERROR: [R66 P1-11] 列出 rulesets 失败,GitHub API 响应:"
  echo "$LIST_RESPONSE"
  exit 1
fi

# 按 name 查找现有 ruleset(返回 ruleset id 或空字符串)
# 幂等性核心:已存在则 PUT 更新,不存在则 POST 创建
EXISTING_RULESET_ID=$(echo "$LIST_RESPONSE" \
  | jq -r --arg name "$RULESET_NAME" \
    '.[] | select(.name == $name) | .id' \
  | head -n 1)

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

# 检查配置是否成功(成功响应包含 id 字段)
if ! echo "$RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
  echo "ERROR: [R66 P1-11] Ruleset 配置失败,GitHub API 响应:"
  echo "$RESPONSE"
  exit 1
fi

RULESET_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "✓ [R66 P1-11] Ruleset 配置成功(id=${RULESET_ID})"

# ─── 6. 配置后立即自检(复用 verify_tag_ruleset.sh 关键断言) ───
# R66 P1-11: 配置后必须自检,任何属性不满足要求都立即失败
echo ""
echo "=== 验证配置(复用 verify_tag_ruleset.sh 关键断言) ==="
RULESET_JSON="$RESPONSE"

echo "Assert: target == tags"
echo "$RULESET_JSON" | jq -e '.target == "tags"' > /dev/null \
  || { echo "ERROR: [R66 P1-11] target != tags"; exit 1; }

echo "Assert: enforcement == active"
echo "$RULESET_JSON" | jq -e '.enforcement == "active"' > /dev/null \
  || { echo "ERROR: [R66 P1-11] enforcement != active"; exit 1; }

echo "Assert: conditions.ref_name.include 包含 refs/tags/*"
echo "$RULESET_JSON" | jq -e '.conditions.ref_name.include | index("refs/tags/*") != null' > /dev/null \
  || { echo "ERROR: [R66 P1-11] conditions.ref_name.include 不包含 refs/tags/*"; exit 1; }

echo "Assert: rules 包含 deletion (deletion=false, tags 不可删除)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "deletion")' > /dev/null \
  || { echo "ERROR: [R66 P1-11] rules 缺少 deletion 类型(deletion=false 未启用)"; exit 1; }

echo "Assert: rules 包含 non_fast_forward (non_fast_forward=false, tags 不可移动)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "non_fast_forward")' > /dev/null \
  || { echo "ERROR: [R66 P1-11] rules 缺少 non_fast_forward 类型(non_fast_forward=false 未启用)"; exit 1; }

echo "Assert: rules 包含 update (update=false, tags 不可更新)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "update")' > /dev/null \
  || { echo "ERROR: [R66 P1-11] rules 缺少 update 类型(update=false 未启用)"; exit 1; }

echo "Assert: rules 包含 creation (创建限制)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "creation")' > /dev/null \
  || { echo "ERROR: [R66 P1-11] rules 缺少 creation 类型(创建限制未启用)"; exit 1; }

echo "Assert: rules 包含 required_signatures (强制 GPG 签名验证)"
echo "$RULESET_JSON" | jq -e '[.rules[].type] | any(. == "required_signatures")' > /dev/null \
  || { echo "ERROR: [R66 P1-11] rules 缺少 required_signatures 类型(强制 GPG 签名验证未启用)"; exit 1; }

echo ""
echo "✓ [R66 P1-11] 所有断言通过,tag ruleset 已正确配置"
echo ""
echo "最终配置(关键字段):"
echo "$RULESET_JSON" | jq '{id, name, target, source_type, enforcement, conditions, rules, bypass_actors}'
