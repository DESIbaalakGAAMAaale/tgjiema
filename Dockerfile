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
RUN pip install --no-cache-dir --user -r requirements.txt

# ── 第二阶段：运行时镜像 ───────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# 只安装运行时依赖（不含编译工具）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的 Python 包
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 项目代码
COPY . .

# 数据目录（SQLite relay_pool.db）
RUN mkdir -p /app/data /app/logs

# 默认启动所有服务
CMD ["python", "run_all.py"]