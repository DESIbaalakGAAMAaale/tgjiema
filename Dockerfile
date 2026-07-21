# TG文件解码器 — Docker 镜像
# 环形冗余 v2 架构

# R40 P2-2 / R41 P0-1: 基础镜像使用真实 digest pinning(不可变),保证供应链可复现。
# digest 来源: Docker Hub API https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim
#   查询时间: 2026-07-16
#   tag: 3.12-slim
#   manifest digest (multi-arch list): sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
# 校验命令:
#   docker pull python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# 更新流程见 docs/docker-image-pinning.md。
# CI 会校验 Dockerfile 中 PYTHON_IMAGE 必须包含 @sha256: 前缀 + 64 位 hex
#   (见 .github/workflows/ci.yml release-gates job 与 tests/test_r38_p0.py)
ARG PYTHON_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc libpq-dev libc6-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# P1-6: 安装到可读路径的虚拟环境 /app/venv(而非 /root/.local),
# 避免运行时切到 USER app 后无权限访问 root 家目录下的包
RUN python -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# ── 第二阶段：运行时镜像 ───────────────────────────────
# R38 P0-1: 与 builder 阶段使用同一 PYTHON_IMAGE,保证可重现构建
FROM ${PYTHON_IMAGE}

WORKDIR /app

# 只安装运行时依赖（不含编译工具）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev procps && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的虚拟环境(位于可读路径 /app/venv)
COPY --from=builder /app/venv /app/venv
ENV PATH=/app/venv/bin:$PATH

# 项目代码 — R68 P0-07: 显式 allowlist COPY(不再使用全量复制)
# 物理排除 legacy restore CLI(services/db_restore.py) — 未被 run_all.py 引用,
# 仅用于 admin CLI 恢复,生产环境通过 _production_guard 硬守卫禁止。
# .dockerignore 同时排除 services/db_restore.py 作为纵深防御。
COPY run_all.py ./
COPY services/ ./services/
COPY bots/ ./bots/
COPY admin/ ./admin/
COPY config/ ./config/
COPY database/ ./database/
COPY locales/ ./locales/
COPY utils/ ./utils/
COPY storage/ ./storage/
COPY requirements.txt ./

# R64 P0-02: 生产镜像默认启用 migration manifest 验证 + APP_ENV=production
#   - MIGRATION_MANIFEST_VERIFY=1: 强制 cosign verify-blob + HEAD/Tree 绑定 + 集合一致性
#   - APP_ENV=production: _is_manifest_verify_enabled() 检测到 staging/production
#     且未启用验签时直接 raise AppError 拒绝启动(fail-closed)
#   - 部署环境必须通过 RELEASE_SOURCE_COMMIT / RELEASE_SOURCE_TREE 环境变量
#     注入签名 attestation 中的 source commit/tree(非 git 部署 fail-closed)
ENV MIGRATION_MANIFEST_VERIFY=1
ENV APP_ENV=production

# 创建非root用户
RUN useradd -m app

# 数据目录（SQLite relay_pool.db）
RUN mkdir -p /app/data /app/logs && \
    chown -R app:app /app

# 切换到非root用户(可访问 /app/venv 与 /app/data、/app/logs)
USER app

# 默认启动所有服务(PATH 已含 venv,python 解析为 /app/venv/bin/python)
CMD ["python", "run_all.py"]
