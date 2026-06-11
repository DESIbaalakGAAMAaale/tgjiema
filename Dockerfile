# TG文件解码器 — Docker 镜像
# 环形冗余 v2 架构

FROM python:3.12-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 项目代码
COPY . .

# 数据目录（SQLite relay_pool.db）
RUN mkdir -p /app/data /app/logs

# 默认启动所有服务
CMD ["python", "run_all.py"]