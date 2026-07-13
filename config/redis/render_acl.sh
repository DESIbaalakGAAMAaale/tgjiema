#!/bin/sh
# R40 P0-3: Redis ACL 渲染脚本(容器入口初始化)
#
# 作用:
#   从环境变量读取 REDIS_HEALTH_PASSWORD / REDIS_WRITER_PASSWORD / REDIS_READER_PASSWORD,
#   缺失时用 openssl rand -hex 24 随机生成并打印警告(不写入 .env,容器场景使用 Docker secrets)。
#   将 users.acl.template 中的 <REDIS_*_PASSWORD> 占位符替换为真实密码,输出到 /data/users.acl。
#
# 调用方:
#   1. docker-compose 的 redis-acl-init 一次性服务(挂载 /data 卷,生成 /data/users.acl)
#   2. redis 主容器挂载同一 redis_data 卷,以 --aclfile /data/users.acl 启动
#   3. deploy_vps_per_bot.sh 在 systemd 部署模式下也调用本脚本(无 Docker 时)
#
# 校验:
#   - 输出文件不得含 '<' 字符(占位符未替换)
#   - 输出文件不得含 'changeme'(默认密码未替换)
#   - 任一校验失败 exit 1
#
# 注意:
#   - 容器以 root 启动,完成后改 redis:redis 属主
#   - 模板文件路径默认 /app/config/redis/users.acl.template(容器内)
#     或与本脚本同目录的 users.acl.template(裸机部署)

set -eu

# 模板与输出路径(支持环境变量覆盖,容器内由 docker-compose 挂载决定)
TEMPLATE_PATH="${ACL_TEMPLATE_PATH:-/app/config/redis/users.acl.template}"
OUTPUT_PATH="${ACL_OUTPUT_PATH:-/data/users.acl}"

# 兼容裸机部署:若 /app/config/redis/users.acl.template 不存在,
# 回退到与本脚本同目录的 users.acl.template
if [ ! -f "$TEMPLATE_PATH" ]; then
    SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
    TEMPLATE_PATH="$SCRIPT_DIR/users.acl.template"
fi

if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "[render_acl] ERROR: ACL 模板不存在: $TEMPLATE_PATH" >&2
    exit 1
fi

# ── 1. 读取或生成密码 ──
# 容器场景:密码由 .env -> docker-compose environment 注入(或 Docker secrets)
# 缺失时生成随机密码,但仅打印到 stderr(不回写 .env,因为只读挂载)

gen_password() {
    # 优先使用 openssl,fallback 到 /dev/urandom
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
    else
        head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

HEALTH_PWD="${REDIS_HEALTH_PASSWORD:-}"
WRITER_PWD="${REDIS_WRITER_PASSWORD:-}"
READER_PWD="${REDIS_READER_PASSWORD:-}"

if [ -z "$HEALTH_PWD" ]; then
    HEALTH_PWD=$(gen_password)
    echo "[render_acl] WARN: REDIS_HEALTH_PASSWORD 未设置,已生成随机密码(health 用户仅 PING 权限)" >&2
    echo "[render_acl] INFO: 容器场景请通过 docker-compose environment 或 Docker secrets 注入" >&2
fi
if [ -z "$WRITER_PWD" ]; then
    WRITER_PWD=$(gen_password)
    echo "[render_acl] WARN: REDIS_WRITER_PASSWORD 未设置,已生成随机密码(tgjiema_writer 用户)" >&2
    echo "[render_acl] INFO: 容器场景请通过 docker-compose environment 或 Docker secrets 注入" >&2
fi
if [ -z "$READER_PWD" ]; then
    READER_PWD=$(gen_password)
    echo "[render_acl] WARN: REDIS_READER_PASSWORD 未设置,已生成随机密码(tgjiema_reader 用户)" >&2
    echo "[render_acl] INFO: 容器场景请通过 docker-compose environment 或 Docker secrets 注入" >&2
fi

# ── 2. 校验密码不含 sed 特殊字符(|) ──
# 使用 | 作为 sed 分隔符,密码含 | 会导致解析错误
for pwd in "$HEALTH_PWD" "$WRITER_PWD" "$READER_PWD"; do
    if echo "$pwd" | grep -q '|'; then
        echo "[render_acl] ERROR: 密码含 sed 分隔符 '|',拒绝渲染" >&2
        exit 1
    fi
done

# ── 3. 确保 OUTPUT_PATH 目录存在 ──
OUTPUT_DIR=$(dirname "$OUTPUT_PATH")
mkdir -p "$OUTPUT_DIR" 2>/dev/null || true

# ── 4. sed 替换占位符,输出到目标文件 ──
# 占位符格式: <REDIS_HEALTH_PASSWORD> / <REDIS_WRITER_PASSWORD> / <REDIS_READER_PASSWORD>
# 使用 | 作为分隔符避免密码中可能的 / 冲突
sed \
    -e "s|<REDIS_HEALTH_PASSWORD>|${HEALTH_PWD}|g" \
    -e "s|<REDIS_WRITER_PASSWORD>|${WRITER_PWD}|g" \
    -e "s|<REDIS_READER_PASSWORD>|${READER_PWD}|g" \
    "$TEMPLATE_PATH" > "$OUTPUT_PATH"

# ── 5. 校验输出文件 ──
# 5.1 不得含 '<'(占位符未替换)
if grep -q '<' "$OUTPUT_PATH"; then
    echo "[render_acl] ERROR: 输出文件仍含 '<' 字符,占位符未完全替换: $OUTPUT_PATH" >&2
    grep -n '<' "$OUTPUT_PATH" | head -5 >&2
    exit 1
fi

# 5.2 不得含 'changeme'(默认密码未替换)
if grep -qi 'changeme' "$OUTPUT_PATH"; then
    echo "[render_acl] ERROR: 输出文件仍含 'changeme' 字符,默认密码未替换: $OUTPUT_PATH" >&2
    grep -in 'changeme' "$OUTPUT_PATH" | head -5 >&2
    exit 1
fi

# ── 6. 权限与属主 ──
chmod 600 "$OUTPUT_PATH" 2>/dev/null || true
# 容器内 redis 用户 UID 通常为 999,裸机部署为 redis:redis
chown redis:redis "$OUTPUT_PATH" 2>/dev/null || \
    chown 999:999 "$OUTPUT_PATH" 2>/dev/null || true

echo "[render_acl] OK: ACL 文件已生成: $OUTPUT_PATH"
echo "[render_acl] INFO: 3 个用户(health/writer/reader)密码已注入,占位符已全部替换"
echo "[render_acl] INFO: redis-server 启动时通过 --aclfile $OUTPUT_PATH 加载"
