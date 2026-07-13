#!/usr/bin/env bash
# R42 P1-1: Docker 基础镜像 digest 真实可拉取校验脚本
#
# 功能:
#   1. 从 Dockerfile 第 17 行解析 ARG PYTHON_IMAGE 的默认值
#   2. 提取 sha256 digest 部分(64 位 hex)
#   3. 调用 `docker manifest inspect <image>@<digest>` 验证该 digest 在镜像仓库真实可拉取
#   4. 失败时以非零退出码退出,并打印诊断信息
#
# 使用方法:
#   bash scripts/verify_docker_digest.sh                 # 默认解析项目根 Dockerfile
#   bash scripts/verify_docker_digest.sh /path/to/Dockerfile  # 指定 Dockerfile 路径
#
# 退出码:
#   0 = digest 真实可拉取,校验通过
#   1 = 校验失败(digest 格式错误 / 占位值 / manifest inspect 失败)
#   2 = 环境错误(Dockerfile 不存在 / docker 不可用)
#
# 依赖:
#   - Docker CLI(用于 docker manifest inspect)
#   - GNU grep / sed / awk(标准 Unix 工具)
set -uo pipefail

# ─── 参数与路径解析 ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCKERFILE="${1:-${PROJECT_ROOT}/Dockerfile}"

# ─── 前置检查 ────────────────────────────────────────────────
if [[ ! -f "${DOCKERFILE}" ]]; then
    echo "ERROR: Dockerfile 不存在: ${DOCKERFILE}" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker 命令不可用,请先安装 Docker CLI" >&2
    exit 2
fi

# ─── 解析 ARG PYTHON_IMAGE 行 ────────────────────────────────
# 匹配形如: ARG PYTHON_IMAGE=python:3.12-slim@sha256:<64-hex>
PYTHON_IMAGE_LINE=$(grep -E '^ARG[[:space:]]+PYTHON_IMAGE=' "${DOCKERFILE}" || true)

if [[ -z "${PYTHON_IMAGE_LINE}" ]]; then
    echo "ERROR: Dockerfile 未定义 ARG PYTHON_IMAGE" >&2
    exit 1
fi

echo "INFO: 解析到 PYTHON_IMAGE 行: ${PYTHON_IMAGE_LINE}"

# 提取等号后的镜像引用(去除行尾注释)
IMAGE_REF=$(echo "${PYTHON_IMAGE_LINE}" | sed -E 's/^ARG[[:space:]]+PYTHON_IMAGE=([^[:space:]]+).*/\1/')

if [[ -z "${IMAGE_REF}" ]]; then
    echo "ERROR: PYTHON_IMAGE 值为空" >&2
    exit 1
fi

echo "INFO: 镜像引用: ${IMAGE_REF}"

# ─── 校验 digest 格式 ────────────────────────────────────────
# 期望格式: <repo>:<tag>@sha256:<64-hex>
if [[ "${IMAGE_REF}" != *@sha256:* ]]; then
    echo "ERROR: 镜像引用未包含 @sha256: digest 段: ${IMAGE_REF}" >&2
    echo "       R40 P2-2 要求基础镜像必须固定 digest 以保证供应链可复现" >&2
    exit 1
fi

# 提取 64 位 hex digest
DIGEST_HEX=$(echo "${IMAGE_REF}" | sed -E 's/.*@sha256:([a-f0-9]+).*/\1/')

if [[ ${#DIGEST_HEX} -ne 64 ]]; then
    echo "ERROR: digest 长度不为 64 位 hex,实际长度: ${#DIGEST_HEX}" >&2
    exit 1
fi

if ! [[ "${DIGEST_HEX}" =~ ^[a-f0-9]{64}$ ]]; then
    echo "ERROR: digest 不是合法的 64 位小写 hex: ${DIGEST_HEX}" >&2
    exit 1
fi

echo "INFO: digest 格式校验通过(64 位 sha256)"

# ─── 占位值检测 ─────────────────────────────────────────────
# 已知占位 digest 列表(来自 docs/docker-image-pinning.md 历史占位值)
PLACEHOLDER_DIGESTS=(
    "5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef"
    "b0d2c8b8e5b2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "placeholderplaceholderplaceholderplaceholderplaceholderplaceholder"
)

for placeholder in "${PLACEHOLDER_DIGESTS[@]}"; do
    if [[ "${DIGEST_HEX}" == "${placeholder}" ]]; then
        echo "ERROR: 检测到占位 digest: ${DIGEST_HEX}" >&2
        echo "       请替换为真实 digest,获取方法:" >&2
        echo "       docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim" >&2
        echo "       或访问 https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim" >&2
        exit 1
    fi
done

echo "INFO: digest 非占位值"

# ─── 调用 docker manifest inspect 验证可拉取 ────────────────
# 注意: docker manifest inspect 不需要本地拉取镜像,只查询仓库 manifest
# 这是验证 digest 真实存在的最轻量方法
echo "INFO: 调用 docker manifest inspect 验证 digest 可拉取..."

# 提取 repo:tag 部分(去掉 @sha256:digest 段)
REPO_TAG=$(echo "${IMAGE_REF}" | sed -E 's/@sha256:.*//')
FULL_REF="${REPO_TAG}@sha256:${DIGEST_HEX}"

echo "INFO: 完整引用: ${FULL_REF}"

# 使用 docker manifest inspect 验证
# --verbose 输出 mediaType 等元数据,便于诊断
MANIFEST_OUTPUT=$(docker manifest inspect "${FULL_REF}" 2>&1)
MANIFEST_EXIT=$?

if [[ ${MANIFEST_EXIT} -ne 0 ]]; then
    echo "ERROR: docker manifest inspect 失败,exit=${MANIFEST_EXIT}" >&2
    echo "       manifest 输出:" >&2
    echo "${MANIFEST_OUTPUT}" >&2
    echo ""
    echo "       可能原因:" >&2
    echo "       1. digest 不存在于镜像仓库(占位值或已删除)" >&2
    echo "       2. 网络访问受限(Docker Hub 不可达)" >&2
    echo "       3. Docker CLI 未启用 manifest 实验 feature(旧版本)" >&2
    echo "          启用方法: export DOCKER_CLI_EXPERIMENTAL=enabled" >&2
    exit 1
fi

echo "INFO: docker manifest inspect 成功,digest 真实可拉取"
echo "INFO: manifest 摘要:"
echo "${MANIFEST_OUTPUT}" | head -n 20

echo ""
echo "PASS: Docker 基础镜像 digest 校验通过"
echo "  - Dockerfile: ${DOCKERFILE}"
echo "  - 镜像引用: ${IMAGE_REF}"
echo "  - digest: sha256:${DIGEST_HEX}"
exit 0
