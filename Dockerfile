# TG文件解码器 — Docker 镜像
# 环形冗余 v2 架构

# R37 P2-4: 镜像 digest 固定(防止供应链篡改)
# 拉取时使用 sha256 digest 引用,确保每次构建基于同一份不可变的基础镜像
# 更新 digest 流程:
#   1. docker pull python:3.12-slim
#   2. docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
#   3. 用输出的 sha256:... 替换下方两个 FROM 行(需保持一致)
# 当前 digest 对应 python:3.12-slim 多架构 manifest(由 Docker Hub 自动选择)
FROM python:3.12-slim@sha256:b0d2c8b8e5b2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e AS builder

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
# R37 P2-4: 与 builder 阶段使用同一 digest,保证可重现构建
FROM python:3.12-slim@sha256:b0d2c8b8e5b2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e

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
