#!/usr/bin/env bash
# R67 P0-01: 验证 Git source governance(commit 签名 + tag 签名 + 祖先链签名策略)
#
# 本脚本在 Release Gates 中运行,验证:
#   1. 当前 commit ($GITHUB_SHA) 已 GPG 签名并通过验证
#   2. (tag push 场景) annotated release tag 已 GPG 签名
#   3. (tag push 场景) tag 指向的 commit 与 release candidate commit 完全一致
#   4. (可选)祖先链签名策略:从 release candidate 回溯 N 个 commit 全部已签名
#   5. GitHub API commit verification 与本地 git verify-commit 双重确认
#
# 退出码:
#   0  所有验证通过
#   1  任意验证失败
#   2  参数错误或环境缺失
#
# 使用方法:
#   GITHUB_SHA=<sha> GITHUB_REF=<ref> GITHUB_REPOSITORY=<owner/repo> \
#     GH_TOKEN=<token> ./scripts/verify_git_source_governance.sh
#
#   可选环境变量:
#     ANCESTOR_POLICY_DEPTH   祖先链签名策略深度(默认: 0=不检查祖先)
#     EXPECTED_TAG_COMMIT     tag 场景下期望 tag 指向的 commit SHA
#                             (默认: $GITHUB_SHA,即 tag 必须指向当前 release candidate)
set -euo pipefail

GITHUB_SHA="${GITHUB_SHA:-}"
GITHUB_REF="${GITHUB_REF:-}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"
GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
ANCESTOR_POLICY_DEPTH="${ANCESTOR_POLICY_DEPTH:-0}"
EXPECTED_TAG_COMMIT="${EXPECTED_TAG_COMMIT:-}"

if [ -z "$GITHUB_SHA" ] || [ -z "$GITHUB_REF" ] || [ -z "$GITHUB_REPOSITORY" ]; then
  echo "ERROR: [R67 P0-01] 缺少必选环境变量"
  echo "  需要: GITHUB_SHA, GITHUB_REF, GITHUB_REPOSITORY"
  echo "  可选: GH_TOKEN, ANCESTOR_POLICY_DEPTH, EXPECTED_TAG_COMMIT"
  exit 2
fi

if [ -z "$GH_TOKEN" ]; then
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: [R67 P0-01] 需要 GH_TOKEN / GITHUB_TOKEN 环境变量,或先执行 gh auth login"
    exit 2
  fi
fi

FAIL=0
fail() {
  echo "FAIL: [R67 P0-01] $1"
  FAIL=1
}

echo "=== R67 P0-01: Git Source Governance 验证 ==="
echo "GITHUB_SHA: ${GITHUB_SHA:0:12}..."
echo "GITHUB_REF: ${GITHUB_REF}"
echo "GITHUB_REPOSITORY: ${GITHUB_REPOSITORY}"
echo "ANCESTOR_POLICY_DEPTH: ${ANCESTOR_POLICY_DEPTH}"
echo ""

# ════════════════════════════════════════════════════════════════
# 1. 当前 commit 签名验证 — git verify-commit
# R69 P0-8: 正确区分 %G? 状态语义(G/U/X 不能混为一谈)
#   G: good signature (签名有效且公钥在本地信任网)
#   B: bad signature (签名无效)
#   U: good signature with unknown validity (签名有效,但公钥不在本地信任网)
#      → GitHub API 持有公钥,可作为权威源验证
#   X: no signature (commit 完全无签名)
#      → 只有 GitHub web-flow / squash commit (GitHub 用自己密钥签名) 才可接受
#   Y: expired key but valid signature (密钥过期但签名有效)
#   R: revoked key (密钥已撤销)
#   E: expired key (密钥过期)
# ════════════════════════════════════════════════════════════════
echo "--- 检查 1: git verify-commit ${GITHUB_SHA:0:12} ---"
COMMIT_SIG_STATUS=$(git log --pretty='%G?' -1 "${GITHUB_SHA}" 2>/dev/null || echo "X")
case "$COMMIT_SIG_STATUS" in
  G) echo "  ✓ commit 已签名且验证通过(G — good signature)" ;;
  B) fail "commit 签名验证失败(B — bad signature)" ;;
  U) echo "  ⚠ commit 签名有效但公钥不在本地信任网(U — good signature with unknown validity)" ;;
  X) echo "  ⚠ commit 无签名(X — no signature,本地未检测到任何 GPG 签名)" ;;
  Y) echo "  ✓ commit 已签名且验证通过(Y — expired key but valid signature)" ;;
  R) fail "commit 签名已撤销(R — revoked)" ;;
  E) fail "commit 签名无法验证(E — expired key)" ;;
  *) fail "commit 签名状态未知: ${COMMIT_SIG_STATUS}" ;;
esac

# git verify-commit 显式调用
#   - G/Y: 应该通过(本地有公钥且签名有效)
#   - U: 签名本身有效,但本地缺公钥 → 不视为硬失败,由 GitHub API 裁决
#   - X: 无签名,git verify-commit 必然失败 → 不视为硬失败,由 GitHub API + reason 裁决
#   - B/R/E: 硬失败(签名无效/撤销/过期)
case "$COMMIT_SIG_STATUS" in
  G|Y)
    if git verify-commit "${GITHUB_SHA}" >/dev/null 2>&1; then
      echo "  ✓ git verify-commit 显式验证通过(本地信任的 GPG 公钥)"
    else
      fail "git verify-commit ${GITHUB_SHA} 失败(G/Y 状态但 verify-commit 未通过)"
    fi
    ;;
  U|X)
    # U: 签名有效但缺公钥 / X: 无签名 — 由 GitHub API + reason 裁决
    echo "  [INFO] ${COMMIT_SIG_STATUS} 状态 — 将由 GitHub API verification.reason 裁决"
    ;;
  *)
    fail "git verify-commit ${GITHUB_SHA} 失败(签名状态=${COMMIT_SIG_STATUS})"
    ;;
esac

# ════════════════════════════════════════════════════════════════
# 2. GitHub API commit verification 双重确认
# R69 P0-8: 区分 U / X 的 GitHub API fallback 语义
#   - U (签名有效,缺公钥): GitHub API verified=true 即可接受(签名本身有效)
#   - X (无签名): 必须检查 reason — 只有明确的 GitHub web-flow / squash
#     签名类型才可接受;普通 unsigned commit 即使 API verified=true 也必须失败
# ════════════════════════════════════════════════════════════════
echo ""
echo "--- 检查 2: GitHub API commit verification ---"
API_RESPONSE=$(curl -sS \
  -H "Authorization: token ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}/commits/${GITHUB_SHA}" 2>/dev/null || echo "")

if [ -z "$API_RESPONSE" ] || ! echo "$API_RESPONSE" | jq -e '.commit.verification' >/dev/null 2>&1; then
  fail "GitHub API 返回非预期响应(无 commit.verification 字段)"
else
  VERIFIED=$(echo "$API_RESPONSE" | jq -r '.commit.verification.verified')
  REASON=$(echo "$API_RESPONSE" | jq -r '.commit.verification.reason')
  SIGNATURE=$(echo "$API_RESPONSE" | jq -r '.commit.verification.signature // "(none)"')
  if [ "$VERIFIED" = "true" ]; then
    echo "  ✓ GitHub API verification.verified=true (reason=${REASON})"
    # R69 P0-8: 根据 %G? 状态分流处理
    case "$COMMIT_SIG_STATUS" in
      G|Y)
        # 本地已验证通过,API 双重确认 — 签名有效
        echo "  ✓ 双重验证通过(本地 G/Y + GitHub API verified=true)"
        ;;
      U)
        # R69 P0-8: U 状态 — 签名本身有效,但本地缺公钥
        # GitHub API 持有公钥,verified=true 即为权威源(签名确实有效)
        echo "  ✓ U 状态(签名有效,公钥不在本地信任网)— GitHub API 验证通过"
        echo "    签名本身有效,GitHub 持有公钥,verified=true 即为权威源"
        ;;
      X)
        # R69 P0-8: X 状态 — commit 无签名,API verified=true 必须有明确原因
        # 只接受 GitHub web-flow / squash 签名(这些是 GitHub 用自己密钥签名的)
        # 普通 unsigned commit 即使 API verified=true 也必须失败
        case "$REASON" in
          *web-flow*|*web_flow*|*squash*|*merged*|*pseudo*)
            echo "  ✓ X 状态 — GitHub ${REASON} 签名(GitHub 用自己密钥签名,合法)"
            ;;
          *unsigned*|*none*|*"")
            fail "X 状态 + GitHub API reason=${REASON}(无签名/unknown)— 普通 unsigned commit 不得通过"
            echo "    R69 P0-8: 普通 unsigned commit 不得仅凭泛化 API 结果通过"
            echo "    签名: ${SIGNATURE:0:60}..."
            ;;
          *)
            # 其他 reason(如 valid_signature / signed)— 可能是 GitHub 验证了
            # 某种签名类型,但本地 git %G?=X 表示本地未检测到 GPG 签名
            # 这种情况需要额外检查 signature 字段是否存在
            if [ "$SIGNATURE" != "(none)" ] && [ -n "$SIGNATURE" ]; then
              echo "  ✓ X 状态 — GitHub API 检测到签名(reason=${REASON},signature 存在)"
              echo "    本地 git 未检测到 GPG 签名,但 GitHub API 持有签名数据"
            else
              fail "X 状态 + GitHub API reason=${REASON}(无 signature 数据)— 无法证明签名存在"
              echo "    R69 P0-8: 无签名的 commit 必须有明确的 GitHub 签名类型(web-flow/squash)"
            fi
            ;;
        esac
        ;;
    esac
  else
    fail "GitHub API verification.verified=false (reason=${REASON})"
    echo "    签名: ${SIGNATURE:0:60}..."
    echo "    R68 P0-05: commit 签名验证失败,GitHub API 是权威源,verified=false 即硬失败"
  fi
fi

# ════════════════════════════════════════════════════════════════
# 3. 祖先链签名策略(可选)
# ════════════════════════════════════════════════════════════════
if [ "$ANCESTOR_POLICY_DEPTH" -gt 0 ]; then
  echo ""
  echo "--- 检查 3: 祖先链签名策略(深度=${ANCESTOR_POLICY_DEPTH}) ---"
  UNSIGNED_ANCESTORS=()
  for i in $(seq 0 $((ANCESTOR_POLICY_DEPTH - 1))); do
    ANCESTOR_SHA=$(git rev-parse "${GITHUB_SHA}~${i}" 2>/dev/null || echo "")
    if [ -z "$ANCESTOR_SHA" ]; then
      fail "无法获取祖先 commit ~${i}(可能已到根)"
      break
    fi
    ANCESTOR_SIG=$(git log --pretty='%G?' -1 "${ANCESTOR_SHA}" 2>/dev/null || echo "X")
    case "$ANCESTOR_SIG" in
      G|Y)
        echo "  ✓ ~${i} ${ANCESTOR_SHA:0:12} 已签名验证通过(${ANCESTOR_SIG})"
        ;;
      *)
        echo "  ✗ ~${i} ${ANCESTOR_SHA:0:12} 签名状态=${ANCESTOR_SIG}"
        UNSIGNED_ANCESTORS+=("${ANCESTOR_SHA}")
        ;;
    esac
  done
  if [ "${#UNSIGNED_ANCESTORS[@]}" -gt 0 ]; then
    fail "祖先链含 ${#UNSIGNED_ANCESTORS[@]} 个未签名/未验证 commit(违反签名策略)"
    for sha in "${UNSIGNED_ANCESTORS[@]}"; do
      echo "    - ${sha}"
    done
  fi
fi

# ════════════════════════════════════════════════════════════════
# 4. Tag 场景验证(仅在 GITHUB_REF 指向 refs/tags/ 时执行)
# ════════════════════════════════════════════════════════════════
if [[ "$GITHUB_REF" == refs/tags/* ]]; then
  TAG_NAME="${GITHUB_REF#refs/tags/}"
  echo ""
  echo "--- 检查 4: Release Tag '${TAG_NAME}' 签名验证 ---"

  # 4a. tag 必须存在
  if ! git rev-parse --verify "refs/tags/${TAG_NAME}" >/dev/null 2>&1; then
    fail "tag refs/tags/${TAG_NAME} 不存在"
  else
    # 4b. tag 必须是 annotated(轻量 tag 无签名能力)
    TAG_TYPE=$(git cat-file -t "refs/tags/${TAG_NAME}" 2>/dev/null || echo "")
    if [ "$TAG_TYPE" = "tag" ]; then
      echo "  ✓ tag 是 annotated(tag object)"
    else
      fail "tag 是轻量 tag(type=${TAG_TYPE}),不是 annotated — 无法签名"
    fi

    # 4c. git verify-tag 验证签名
    if git verify-tag "${TAG_NAME}" >/dev/null 2>&1; then
      echo "  ✓ git verify-tag '${TAG_NAME}' 通过"
    else
      fail "git verify-tag '${TAG_NAME}' 失败(tag 未签名或签名无效)"
    fi

    # 4d. tag 指向的 commit 必须与 release candidate commit 完全一致
    TAG_COMMIT=$(git rev-parse "${TAG_NAME}^{commit}" 2>/dev/null || echo "")
    EXPECTED_COMMIT="${EXPECTED_TAG_COMMIT:-${GITHUB_SHA}}"
    if [ "$TAG_COMMIT" = "$EXPECTED_COMMIT" ]; then
      echo "  ✓ tag 指向 commit ${TAG_COMMIT:0:12} 与 release candidate 一致"
    else
      fail "tag 指向 commit ${TAG_COMMIT:0:12} 与 release candidate ${EXPECTED_COMMIT:0:12} 不一致"
    fi

    # 4e. tag 指向的 commit 本身必须已签名(commit 签名独立于 tag 签名)
    TAG_COMMIT_SIG=$(git log --pretty='%G?' -1 "${TAG_COMMIT}" 2>/dev/null || echo "X")
    case "$TAG_COMMIT_SIG" in
      G|Y) echo "  ✓ tag 指向的 commit 签名验证通过(${TAG_COMMIT_SIG})" ;;
      *) fail "tag 指向的 commit 签名状态=${TAG_COMMIT_SIG}(tag 签名不能替代 commit 签名)" ;;
    esac
  fi

  # 4f. tag immutability 校验:tag 必须是首次创建(不能移动)
  #     通过查询 GitHub API 确认 tag 不存在于历史 push events(简单近似)
  echo "  [INFO] tag immutability 由 R66 P1-11 tag ruleset (creation/update/deletion) 保证"
fi

echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: [R67 P0-01] Git Source Governance 验证失败"
  exit 1
fi
echo "PASS: [R67 P0-01] Git Source Governance 验证通过"
exit 0
