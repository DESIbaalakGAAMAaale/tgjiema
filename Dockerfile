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
# R70 Wave 2: 新增 docker/entrypoint.py 作为正式容器入口
#   - SERVICE_ROLE 显式映射到唯一生产命令
#   - Compose/systemd 只注入 SERVICE_ROLE,不复制启动命令
COPY run_all.py ./
COPY services/ ./services/
COPY bots/ ./bots/
COPY admin/ ./admin/
COPY config/ ./config/
COPY database/ ./database/
COPY locales/ ./locales/
COPY utils/ ./utils/
COPY storage/ ./storage/
COPY docker/ ./docker/
COPY requirements.txt ./

# R69 P0-5 (Wave 3): 物理删除 blocklist 文件作为第二道防线
#   - 第一道防线:.dockerignore(构建时排除)
#   - 第二道防线:此处 RUN rm(物理删除,即使 .dockerignore 失效也确保不进入镜像)
#   - 第三道防线:CI verify_oci_allowlist.py(运行时验证镜像 filesystem)
#   - 不得依靠 .dockerignore 单点排除敏感文件(R69 Wave 3 要求)
#   - services/db_restore.py: legacy restore CLI,生产 runtime 写入器在 services/restore_writer.py
#   - tests/、scripts/、docs/、.github/、.vscode/、IDE 配置:开发文件,生产镜像不需要
#   - .env、.env.secrets:生产通过 systemd EnvironmentFile 注入
#   - *.db、*.log:运行时数据,不应在镜像中预置
RUN rm -f /app/services/db_restore.py && \
    rm -rf /app/tests /app/scripts /app/docs /app/.github /app/.vscode /app/.idea && \
    rm -f /app/.env /app/.env.local /app/.env.production /app/.env.staging /app/.env.secrets.* && \
    rm -f /app/*.db /app/*.db-shm /app/*.db-wal /app/*.db-journal && \
    rm -f /app/*.log /app/*.tmp /app/*.bak /app/*.orig && \
    rm -rf /app/build /app/dist /app/*.egg-info /app/.pytest_cache /app/.mypy_cache /app/.ruff_cache && \
    find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# R64 P0-02: 生产镜像默认启用 migration manifest 验证 + APP_ENV=production
#   - MIGRATION_MANIFEST_VERIFY=1: 强制 cosign verify-blob + HEAD/Tree 绑定 + 集合一致性
#   - APP_ENV=production: _is_manifest_verify_enabled() 检测到 staging/production
#     且未启用验签时直接 raise AppError 拒绝启动(fail-closed)
#   - R69 P0-1: APP_ENV 是单一权威源(Dockerfile/Compose/run_all.py/Settings/_production_guard 统一)
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

# R69 P0-2: 显式 STOPSIGNAL SIGTERM
#   - systemd 默认发 SIGTERM,Docker 默认 STOPSIGNAL 也是 SIGTERM,
#     但显式声明是契约要求(scripts/check_compose_static_rules.py 与
#     docs/deployment.md 均断言此设置;真实 SIGTERM 行为由
#     scripts/runtime_smoke_compose.py 在镜像中执行验证)
#   - run_all.py 与各 standalone runner 都已注册 SIGTERM handler
#     (见 _register_sigterm_handler),收到信号后 set _stop_event
#     让事件循环走 finally 块优雅关闭 polling / HTTP / DB / 队列
STOPSIGNAL SIGTERM

# R70 Wave 2: 正式 ENTRYPOINT — 基于 SERVICE_ROLE 的角色映射启动器
#   - 替代旧的 CMD ["python", "run_all.py"](在 APP_ENV=production 下 exit 1
#     进入 restart loop)
#   - ENTRYPOINT 通过 SERVICE_ROLE 环境变量映射到唯一生产命令:
#       SERVICE_ROLE=up → python run_all.py --standalone up
#       SERVICE_ROLE=migration → python -m services.migration_runner
#       SERVICE_ROLE=prometheus_exporter → python -m services.prometheus_exporter
#   - production/staging 下未指定 SERVICE_ROLE → fail-closed exit 1
#   - development/test 下未指定 SERVICE_ROLE → 回退 run_all.py 多进程模式
#   - Compose/systemd 只注入 SERVICE_ROLE,不复制启动命令
#   - exec 模式:entrypoint.py 通过 os.execvp 替换进程映像,确保 PID 1
#     直接是业务进程,SIGTERM 直达
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]

# R70 Wave 2: 默认 CMD 为空(entrypoint.py 根据 SERVICE_ROLE 决定)
#   - 若 Compose/systemd 未注入 SERVICE_ROLE 且 APP_ENV=production → exit 1
#   - 这是 fail-closed 设计:不允许容器空跑被误判为正常
CMD []
