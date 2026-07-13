#!/usr/bin/env bash
# R41 P1-11: 依赖供应链完整性校验脚本
#
# 校验当前已安装的 Python 依赖是否与 requirements.lock / requirements.txt 一致:
#   1. 解析 requirements.txt 中的 "package==version" 条目
#   2. 通过 `pip show <package>` 获取当前安装版本
#   3. 比对版本,任何不一致立即报告并以非零退出码退出
#   4. 若存在 requirements.lock 且 hash 非 0 占位,尝试 pip install --require-hashes 校验
#
# 使用方法:
#   bash scripts/verify_deps.sh                  # 基础校验
#   bash scripts/verify_deps.sh --strict         # 严格模式(任何警告都视为错误)
#   bash scripts/verify_deps.sh --no-lock        # 跳过 hash 校验
#
# 退出码:
#   0=所有依赖一致
#   1=校验失败(版本不一致或缺失)
#   2=环境错误(如 pip 不可用)
set -uo pipefail

STRICT=0
NO_LOCK=0
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ_FILE="${PROJECT_ROOT}/requirements.txt"
LOCK_FILE="${PROJECT_ROOT}/requirements.lock"

# ─── 参数解析 ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict)
            STRICT=1
            shift
            ;;
        --no-lock)
            NO_LOCK=1
            shift
            ;;
        --help|-h)
            echo "Usage: bash scripts/verify_deps.sh [--strict] [--no-lock]"
            echo "  --strict    任何警告均视为错误(退出码 1)"
            echo "  --no-lock   跳过 requirements.lock hash 校验"
            exit 0
            ;;
        *)
            echo "[verify_deps] 未知参数: $1" >&2
            exit 2
            ;;
    esac
done

# ─── 环境检查 ────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "[verify_deps] 未找到 python 可执行文件" >&2
    exit 2
fi
PYTHON_BIN="$(command -v python3 || command -v python)"
if ! "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
    echo "[verify_deps] pip 不可用" >&2
    exit 2
fi

if [[ ! -f "${REQ_FILE}" ]]; then
    echo "[verify_deps] 未找到 requirements.txt: ${REQ_FILE}" >&2
    exit 2
fi

echo "[verify_deps] Python: ${PYTHON_BIN}"
echo "[verify_deps] requirements.txt: ${REQ_FILE}"
echo "[verify_deps] 开始校验已安装依赖版本..."

# ─── 解析 requirements.txt ────────────────────────────────
# 提取 "package==version" 行,跳过注释、空行、环境标记
parse_requirements() {
    local file="$1"
    grep -E '^[A-Za-z0-9_]' "${file}" 2>/dev/null \
        | grep -E '==[^=]' \
        | awk -F';' '{print $1}' \
        | grep -oE '^[A-Za-z0-9_][A-Za-z0-9_.\-]*==[A-Za-z0-9_.\-+*]+'
}

# ─── 比对版本 ────────────────────────────────────────────────
MISMATCH_COUNT=0
MISSING_COUNT=0
CHECKED_COUNT=0

while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    # 拆分 name==version
    pkg_name="${line%%==*}"
    expected_version="${line##*==}"
    [[ -z "${pkg_name}" || -z "${expected_version}" ]] && continue
    CHECKED_COUNT=$((CHECKED_COUNT + 1))

    # 查询已安装版本
    installed_version="$(
        "${PYTHON_BIN}" -m pip show "${pkg_name}" 2>/dev/null \
            | grep -i '^Version:' \
            | awk '{print $2}'
    )"
    if [[ -z "${installed_version}" ]]; then
        echo "[verify_deps] MISSING: ${pkg_name} 未安装(期望 ${expected_version})" >&2
        MISSING_COUNT=$((MISSING_COUNT + 1))
        continue
    fi
    if [[ "${installed_version}" != "${expected_version}" ]]; then
        echo "[verify_deps] MISMATCH: ${pkg_name} 期望 ${expected_version} 实际 ${installed_version}" >&2
        MISMATCH_COUNT=$((MISMATCH_COUNT + 1))
    fi
done < <(parse_requirements "${REQ_FILE}")

echo "[verify_deps] 版本校验完成: 共 ${CHECKED_COUNT} 个包, ${MISMATCH_COUNT} 个版本不一致, ${MISSING_COUNT} 个未安装"

# ─── hash 校验(requirements.lock) ────────────────────────
HASH_CHECK_FAILED=0
if [[ "${NO_LOCK}" -eq 0 && -f "${LOCK_FILE}" ]]; then
    echo "[verify_deps] 检测到 requirements.lock,尝试 hash 校验..."
    # 检测是否仍为占位 hash(全 0)
    if grep -qE 'sha256:0{64}' "${LOCK_FILE}"; then
        echo "[verify_deps] WARNING: requirements.lock 仍包含占位 hash(全 0),跳过 hash 校验"
        if [[ "${STRICT}" -eq 1 ]]; then
            echo "[verify_deps] --strict 模式下占位 hash 视为错误" >&2
            HASH_CHECK_FAILED=1
        fi
    else
        # 真实 hash,尝试 pip install --require-hashes --dry-run
        if "${PYTHON_BIN}" -m pip install --dry-run --require-hashes -r "${LOCK_FILE}" >/dev/null 2>&1; then
            echo "[verify_deps] requirements.lock hash 校验通过"
        else
            echo "[verify_deps] FAILED: requirements.lock hash 校验失败(可能存在篡改或 hash 不匹配)" >&2
            HASH_CHECK_FAILED=1
        fi
    fi
else
    if [[ "${NO_LOCK}" -eq 0 ]]; then
        echo "[verify_deps] 未检测到 requirements.lock,跳过 hash 校验"
    fi
fi

# ─── 退出码判定 ────────────────────────────────────────────────
EXIT_CODE=0
if [[ ${MISMATCH_COUNT} -gt 0 || ${MISSING_COUNT} -gt 0 ]]; then
    EXIT_CODE=1
elif [[ ${HASH_CHECK_FAILED} -gt 0 ]]; then
    EXIT_CODE=1
fi

if [[ ${EXIT_CODE} -eq 0 ]]; then
    echo "[verify_deps] ✅ 所有依赖校验通过"
else
    echo "[verify_deps] ❌ 依赖校验失败" >&2
fi
exit ${EXIT_CODE}
