#!/usr/bin/env bash
# R71 P1-06: 清理已合并的整改分支和陈旧 release 路径
#
# 功能:
#   1. 识别并删除已 squash-merge 到 master 的本地分支
#   2. 识别并删除已 squash-merge 到 master 的远程分支
#   3. 保护 master / main / release/* 分支不被删除
#   4. 输出审计日志
#
# 用法:
#   ./scripts/cleanup_merged_branches.sh           # 交互模式(提示确认)
#   ./scripts/cleanup_merged_branches.sh --force    # 强制模式(不提示)
#   ./scripts/cleanup_merged_branches.sh --dry-run   # 只显示要删除什么,不实际删除
#   ./scripts/cleanup_merged_branches.sh --help      # 帮助
#
# 退出码:
#   0: 成功(或 dry-run)
#   1: 删除失败
#   2: 参数错误

set -euo pipefail

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

PROTECTED_BRANCHES="master main"
PROTECTED_PREFIXES="release/ hotfix/"

MODE="interactive"
DELETED_LOCAL=0
DELETED_REMOTE=0
SKIPPED_PROTECTED=0

# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

print_usage() {
    cat <<'USAGE'
R71 P1-06: 清理已合并的整改分支

用法:
    scripts/cleanup_merged_branches.sh [选项]

选项:
    --force      不提示确认,直接删除
    --dry-run    只显示要删除什么,不实际删除
    --help, -h   显示帮助
USAGE
}

is_protected() {
    local branch="$1"
    # 检查保护分支名
    for protected in $PROTECTED_BRANCHES; do
        if [ "$branch" = "$protected" ]; then
            return 0
        fi
    done
    # 检查保护前缀
    for prefix in $PROTECTED_PREFIXES; do
        if [[ "$branch" == "$prefix"* ]]; then
            return 0
        fi
    done
    return 1
}

# 检查分支是否已 squash-merge 到 master
# squash-merge 不保留 merge ancestry,所以用 cherry 检测
is_squash_merged() {
    local branch="$1"
    local base="master"

    # 获取分支最新 commit
    local branch_sha
    branch_sha=$(git rev-parse "$branch" 2>/dev/null) || return 1

    # 用 cherry 检测:如果 master 已包含该 commit 的所有改动,cherry 返回空
    # (cherry 比较的是 patch-id,即使 commit SHA 不同也能检测 squash-merge)
    local cherry_result
    cherry_result=$(git cherry "$base" "$branch" 2>/dev/null | grep '^\-' || true)

    if [ -n "$cherry_result" ]; then
        # cherry 输出 '-' 前缀表示 commit 已在 base 中(equivalent)
        return 0
    else
        # cherry 输出 '+' 前缀表示 commit 不在 base 中
        # 或者无输出表示没有差异
        # 再次检查:如果分支是 master 的祖先,则已合并
        if git merge-base --is-ancestor "$branch_sha" "$base" 2>/dev/null; then
            return 0
        fi
        return 1
    fi
}

confirm_delete() {
    local branch="$1"
    local type="$2"  # "local" or "remote"
    if [ "$MODE" = "force" ] || [ "$MODE" = "dry-run" ]; then
        return 0
    fi
    read -rp "删除 $type 分支 '$branch'? [y/N] " answer
    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# ════════════════════════════════════════════════════════════════
# 参数解析
# ════════════════════════════════════════════════════════════════

while [ $# -gt 0 ]; do
    case "$1" in
        --force)    MODE="force" ;;
        --dry-run)  MODE="dry-run" ;;
        --help|-h)  print_usage; exit 0 ;;
        *)          echo "错误:未知参数 '$1'" >&2; print_usage >&2; exit 2 ;;
    esac
    shift
done

# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

echo "=== R71 P1-06: 清理已合并的整改分支 ==="
echo "模式: $MODE"
echo ""

# 确保 master 是最新的
echo "[1/4] 更新 master..."
git fetch origin master:master 2>/dev/null || git fetch origin master 2>/dev/null || true
echo ""

# [2/4] 清理本地分支
echo "[2/4] 检查本地分支..."
for branch in $(git branch --format='%(refname:short)' | grep -v -E '^(master|main)$'); do
    if is_protected "$branch"; then
        echo "  [SKIP] 保护分支: $branch"
        SKIPPED_PROTECTED=$((SKIPPED_PROTECTED + 1))
        continue
    fi

    if is_squash_merged "$branch"; then
        echo "  [DEL]  已合并: $branch"
        if confirm_delete "$branch" "local"; then
            if [ "$MODE" != "dry-run" ]; then
                git branch -D "$branch"
            fi
            DELETED_LOCAL=$((DELETED_LOCAL + 1))
        else
            echo "        跳过(用户拒绝)"
        fi
    else
        echo "  [KEEP] 未合并: $branch"
    fi
done
echo ""

# [3/4] 清理远程分支
echo "[3/4] 检查远程分支..."
for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/ | grep -v -E 'origin/(master|main|HEAD)$'); do
    branch="${ref#origin/}"

    if is_protected "$branch"; then
        echo "  [SKIP] 保护分支: $branch"
        SKIPPED_PROTECTED=$((SKIPPED_PROTECTED + 1))
        continue
    fi

    if is_squash_merged "$ref"; then
        echo "  [DEL]  已合并: $branch"
        if confirm_delete "$branch" "remote"; then
            if [ "$MODE" != "dry-run" ]; then
                git push origin --delete "$branch"
            fi
            DELETED_REMOTE=$((DELETED_REMOTE + 1))
        else
            echo "        跳过(用户拒绝)"
        fi
    else
        echo "  [KEEP] 未合并: $branch"
    fi
done
echo ""

# [4/4] 检查陈旧 release tags
echo "[4/4] 检查陈旧 release tags..."
STALE_TAGS=""
for tag in $(git tag --list 'v*' 2>/dev/null || true); do
    # R70 P0-10: 旧版 v*.*.* tag 已废弃,production 只接受 production-v* tag
    # 如果旧 v*.*.* tag 存在且不在 master 上,标记为陈旧
    if ! git merge-base --is-ancestor "$tag" master 2>/dev/null; then
        STALE_TAGS="$STALE_TAGS $tag"
    fi
done

if [ -n "$STALE_TAGS" ]; then
    echo "  [WARN] 发现陈旧 tags:$STALE_TAGS"
    echo "         R70 P0-10: 旧版 v*.*.* tag 已废弃"
    echo "         建议手动删除: git tag -d <tag> && git push origin --delete <tag>"
else
    echo "  [OK]   无陈旧 release tags"
fi
echo ""

# ════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════

echo "=== 清理汇总 ==="
echo "  删除本地分支: $DELETED_LOCAL"
echo "  删除远程分支: $DELETED_REMOTE"
echo "  跳过保护分支: $SKIPPED_PROTECTED"
if [ "$MODE" = "dry-run" ]; then
    echo "  (dry-run 模式:未实际删除)"
fi
echo ""

if [ "$MODE" != "dry-run" ]; then
    # 记录审计日志到 .git/info/branch-cleanup-audit.log
    AUDIT_LOG="$(git rev-parse --git-dir)/info/branch-cleanup-audit.log"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    {
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cleanup_merged_branches.sh"
        echo "  deleted_local=$DELETED_LOCAL"
        echo "  deleted_remote=$DELETED_REMOTE"
        echo "  skipped_protected=$SKIPPED_PROTECTED"
        echo "  mode=$MODE"
        echo "  actor=$(whoami 2>/dev/null || echo unknown)"
    } >> "$AUDIT_LOG"
fi

exit 0
