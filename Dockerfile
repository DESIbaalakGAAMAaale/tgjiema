# TG文件解码器 — Docker 镜像
# 环形冗余 v2 架构

# R38 P0-1: 占位 digest 已替换为 tag 引用,避免构建失败。
# 原 R37 P2-4 使用了非合法 64 位 hex 的占位 digest(此处不再展示具体值,
# 避免被自动化脚本误识别为残留),会导致 docker build 直接失败。
#
# 生产部署前应执行以下流程获取真实 digest 并替换 PYTHON_IMAGE 默认值:
#   1. docker pull python:3.12-slim
#   2. docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
#   3. 用 --build-arg PYTHON_IMAGE=python:3.12-slim@<真实 digest> 覆盖默认值,
#      或修改下方 ARG PYTHON_IMAGE 默认值,确保两个 FROM 行使用同一 digest。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
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

# 项目代码
COPY . .

# 创建非root用户
RUN useradd -m app

# 数据目录（SQLite relay_pool.db）
RUN mkdir -p /app/data /app/logs && \
    chown -R app:app /app

# 切换到非root用户(可访问 /app/venv 与 /app/data、/app/logs)
USER app

# 默认启动所有服务(PATH 已含 venv,python 解析为 /app/venv/bin/python)
CMD ["python", "run_all.py"]
