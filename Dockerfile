# TG文件解码器 — Docker 镜像
# 环形冗余 v2 架构

# ── 第一阶段：编译依赖 ─────────────────────────────────
FROM python:3.12-slim AS builder

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
FROM python:3.12-slim

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
