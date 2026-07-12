#!/bin/bash
# ============================================================
#  TG文件解码器 — VPS 部署（独立 systemd 版）
#  每个 Bot 独立 systemd 服务，可单独启停/重启
#  架构：环形冗余 v2（up / idx / dsp / mon / admin_bot / admin / db_backup / db_writer）
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

if [[ $EUID -ne 0 ]]; then
    error "请使用 root 用户或 sudo 执行此脚本"
fi

DEPLOY_DIR="/opt/tgjiema"
PYTHON="python3.12"
SVC_PREFIX="tgjiema"

echo ""
echo "============================================"
echo "  TG文件解码器 — 独立 systemd 部署向导"
echo "  每个 Bot 独立服务，共 8 个 systemd 单元"
echo "============================================"
echo ""

# ──────────────────────────────────────────────
# 第一步：系统依赖
# ──────────────────────────────────────────────
info "第一步：安装系统依赖..."

# 检测发行版：Ubuntu 需添加 deadsnakes PPA 才能装 python3.12；
# Debian 等则检测系统自带 Python 版本，≥3.10 直接复用，否则报错。
if grep -q "Ubuntu" /etc/os-release 2>/dev/null; then
    info "检测到 Ubuntu，安装 software-properties-common 并添加 deadsnakes PPA..."
    apt-get update
    apt-get install -y --no-install-recommends software-properties-common ca-certificates
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
else
    info "检测到非 Ubuntu（Debian 等），检查系统自带 Python 版本..."
    apt-get update
    apt-get install -y --no-install-recommends python3 ca-certificates
    SYS_PY_VER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
    SYS_PY_MAJOR=$(echo "$SYS_PY_VER" | cut -d. -f1)
    SYS_PY_MINOR=$(echo "$SYS_PY_VER" | cut -d. -f2)
    if [[ "$SYS_PY_MAJOR" -ge 3 && "$SYS_PY_MINOR" -ge 10 ]]; then
        info "系统自带 Python ${SYS_PY_VER} ≥ 3.10，使用系统自带版本替代 python3.12"
        PYTHON="python3"
    else
        error "系统自带 Python ${SYS_PY_VER} 低于 3.10，请手动安装 Python 3.10+ 后重试"
    fi
fi

apt-get install -y --no-install-recommends \
    ${PYTHON} ${PYTHON}-venv ${PYTHON}-dev \
    libpq-dev gcc g++ make curl git sqlite3 procps net-tools

# C1: Redis Stream 事件驱动(dsp_bot 替代轮询)
if ! command -v redis-cli &> /dev/null; then
    echo "安装 Redis..."
    apt-get install -y redis-server
    systemctl enable redis-server
    systemctl start redis-server
fi

success "系统依赖安装完成"

# ──────────────────────────────────────────────
# 第二步：部署目录
# ──────────────────────────────────────────────
info "第二步：创建部署目录..."

mkdir -p "$DEPLOY_DIR"/{data,logs,config}

if [[ ! -f "$DEPLOY_DIR/run_all.py" ]]; then
    error "未找到 run_all.py，请确保项目代码已复制到 $DEPLOY_DIR"
fi

success "部署目录创建完成：$DEPLOY_DIR"

# P-2: 创建专用非特权用户（替代 root 运行服务）
info "创建专用用户 tgjiema..."
if ! id -u tgjiema &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -d "$DEPLOY_DIR" tgjiema
    success "用户 tgjiema 已创建（系统用户，无登录 shell）"
else
    info "用户 tgjiema 已存在，跳过"
fi
chown -R tgjiema:tgjiema "$DEPLOY_DIR"
success "部署目录权限已设置"

# ──────────────────────────────────────────────
# 第三步：虚拟环境
# ──────────────────────────────────────────────
info "第三步：创建 Python 虚拟环境..."

cd "$DEPLOY_DIR"

if [[ ! -d "venv" ]]; then
    ${PYTHON} -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt
pip install --no-cache-dir "uvloop>=0.19.0,<0.21.0" "orjson>=3.9.0,<4.0.0"

# P-2b: pip install 后重新 chown，确保 venv 文件属主正确
chown -R tgjiema:tgjiema "$DEPLOY_DIR"

success "Python 依赖安装完成"

# ──────────────────────────────────────────────
# 第四步：检查 .env 和拓扑
# ──────────────────────────────────────────────
info "第四步：检查配置..."

ENV_FILE="$DEPLOY_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$DEPLOY_DIR/.env.example" "$ENV_FILE"
    warn "请编辑: $ENV_FILE"
    ${EDITOR:-vi} "$ENV_FILE"
fi
# P-1: 收紧权限: .env 仅所有者可读，data 目录仅所有者可读写
chmod 600 "$ENV_FILE"
chmod 700 "$DEPLOY_DIR/data"

# 自动生成 RELAY_ENCRYPTION_KEY（若 .env 中为空）—— 必须用 venv 的 python，
# 因为系统 Python 可能未安装 cryptography。此处 venv 已激活。
if grep -q "^RELAY_ENCRYPTION_KEY=$" "$ENV_FILE" 2>/dev/null; then
    KEY=$("${DEPLOY_DIR}/venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null)
    if [[ -n "$KEY" ]]; then
        sed -i "s|^RELAY_ENCRYPTION_KEY=.*|RELAY_ENCRYPTION_KEY=${KEY}|" "$ENV_FILE"
        success "已自动生成 RELAY_ENCRYPTION_KEY"
    else
        warn "RELAY_ENCRYPTION_KEY 为空且自动生成失败，请手动执行：venv/bin/python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    fi
fi

# C1: Redis Stream 事件驱动 — 若 .env 未配置 REDIS_URL 则写入本地默认值
if ! grep -q "^REDIS_URL=" "$ENV_FILE" 2>/dev/null; then
    echo "REDIS_URL=redis://127.0.0.1:6379/0" >> "$ENV_FILE"
    success "已写入 REDIS_URL=redis://127.0.0.1:6379/0 (dsp_bot 事件驱动)"
fi

if [[ ! -f "$DEPLOY_DIR/config/topology.yaml" ]] && [[ -f "$DEPLOY_DIR/config/groups.yaml" ]]; then
    source venv/bin/activate
    cd "$DEPLOY_DIR"
    python config/generate_topology.py
    success "topology.yaml 生成完成"
fi

success "配置文件检查完成"

# ──────────────────────────────────────────────
# 步骤 4.5：CRDB TTL 迁移（幂等，自动执行不阻断部署）
# ──────────────────────────────────────────────
info "执行 CRDB TTL 迁移（关闭已废弃的行级 TTL job）..."

_TTL_SQL="$DEPLOY_DIR/admin/migrations/disable_crdb_ttl.sql"
if [[ ! -f "$_TTL_SQL" ]]; then
    warn "未找到 TTL 迁移脚本: $_TTL_SQL,跳过"
else
    # 从 .env 读取 COCKROACHDB_URL(兼容行内注释)
    _DB_URL=""
    if [[ -f "$ENV_FILE" ]]; then
        _DB_URL=$(grep -E '^COCKROACHDB_URL=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | xargs 2>/dev/null || true)
    fi
    if [[ -z "$_DB_URL" ]]; then
        warn "未配置 COCKROACHDB_URL,TTL 迁移已跳过(可在配置后手动执行 cockroach sql --url \"\$COCKROACHDB_URL\" -f $_TTL_SQL)"
    elif ! command -v cockroach &> /dev/null; then
        warn "未找到 cockroach 命令行客户端,TTL 迁移已跳过——请手动执行:"
        warn "    cockroach sql --url \"\$COCKROACHDB_URL\" -f $_TTL_SQL"
    else
        if cockroach sql --url "$_DB_URL" -f "$_TTL_SQL" >> "$DEPLOY_DIR/logs/deploy_ttl_migration.log" 2>&1; then
            success "CRDB TTL 迁移执行成功(详见 $DEPLOY_DIR/logs/deploy_ttl_migration.log)"
        else
            warn "CRDB TTL 迁移执行失败(不影响部署),详见 $DEPLOY_DIR/logs/deploy_ttl_migration.log"
        fi
    fi
fi

# ──────────────────────────────────────────────
# 第五步：创建 systemd 服务（8 个独立单元）
# ──────────────────────────────────────────────
info "第五步：创建 systemd 服务单元..."

# 服务定义：(名称, 描述, 启动类目)
# 特殊条目可用 5 字段格式: 名称:描述:TimeoutStopSec:Restart:RestartSec
SERVICES=(
    "up:上传接收Bot:接收用户文件，转发到存储频道"
    "idx:解码索引Bot:生成文件码，解码外部码，写派工表"
    "dsp:派送分发Bot:从jobs表轮询任务，通过媒体组发送给用户"
    "mon:监控管理Bot:频道健康监控，自动降级，环形指针推进"
    "admin_bot:管理员Bot:管理配置、用户、重置等操作"
    "admin:Web管理后台:Web管理面板，fastapi+uvicorn"
    "db_backup:数据库备份:定期备份数据库到R2"
    "db_writer:数据库写入:40:always:10"
)

# 生成 systemd 模板函数
generate_service() {
    local name="$1"
    local desc="$2"
    local detail="$3"
    local svc="${SVC_PREFIX}-${name}"
    local restart_type="always"
    local restart_sec="10"
    local stop_timeout="40"
    # 5 字段格式(detail="TimeoutStopSec:Restart:RestartSec"),用于 db_writer 等需要显式指定重启策略的服务
    if [[ "$detail" =~ ^[0-9]+:(always|on-failure|no):[0-9]+$ ]]; then
        stop_timeout=$(echo "$detail" | cut -d: -f1)
        restart_type=$(echo "$detail" | cut -d: -f2)
        restart_sec=$(echo "$detail" | cut -d: -f3)
    fi
    # db_backup 备份任务失败不应无限重启，等待更久再重试
    if [[ "$name" == "db_backup" ]]; then
        restart_type="on-failure"
        restart_sec="60"
        stop_timeout="15"
    fi

    # C1: Bot 服务依赖 redis.service(db_backup 不依赖 Redis)
    local after_deps="network.target"
    if [[ "$name" != "db_backup" ]]; then
        after_deps="network.target redis.service"
    fi

    cat > "/etc/systemd/system/${svc}.service" << EOF
[Unit]
Description=TG文件解码器 — ${desc}
After=${after_deps}
Wants=network.target
PartOf=${SVC_PREFIX}.target
# systemd 内置防抖:60秒内最多重启5次,超限后冷却30秒
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
User=tgjiema
WorkingDirectory=${DEPLOY_DIR}
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=-${DEPLOY_DIR}/.env
ExecStart=${DEPLOY_DIR}/venv/bin/python ${DEPLOY_DIR}/run_all.py --standalone ${name}
Restart=${restart_type}
RestartSec=${restart_sec}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${svc}

# 优雅关闭:用 SIGTERM(run_all.py 同时处理 SIGTERM 和 SIGINT)
# 给 40 秒时间让 polling 优雅关闭,避免幽灵连接导致 409 Conflict
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=${stop_timeout}

# 资源限制
LimitNOFILE=65536
LimitNPROC=4096

# 安全加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    echo "  [OK] ${svc}.service 已创建"
}

for entry in "${SERVICES[@]}"; do
    IFS=":" read -r name desc detail <<< "$entry"
    generate_service "$name" "$desc" "$detail"
done

# 额外：创建一键启停所有服务的 target
info "创建聚合 target..."

cat > "/etc/systemd/system/${SVC_PREFIX}.target" << EOF
[Unit]
Description=TG文件解码器 — 全部服务
Wants=${SVC_PREFIX}-up.service ${SVC_PREFIX}-idx.service ${SVC_PREFIX}-dsp.service ${SVC_PREFIX}-mon.service ${SVC_PREFIX}-admin_bot.service ${SVC_PREFIX}-admin.service ${SVC_PREFIX}-db_backup.service ${SVC_PREFIX}-db_writer.service
After=network.target

[Install]
WantedBy=multi-user.target
EOF

success "聚合 target ${SVC_PREFIX}.target 已创建"

# 重载 systemd
systemctl daemon-reload

# 启用所有服务
for entry in "${SERVICES[@]}"; do
    IFS=":" read -r name desc detail <<< "$entry"
    systemctl enable "${SVC_PREFIX}-${name}"
done

success "所有服务已启用（开机自启）"

# ──────────────────────────────────────────────
# 第六步：DNS pinning（避免 IPv6-only 解析导致 R2/CockroachDB 不可达）
# ──────────────────────────────────────────────
info "第六步：配置 /etc/hosts 静态解析（IPv4 pinning）..."

# 某些 VPS（如 LXC 容器）的 DNS 对 Cloudflare/CockroachDB 只返回 AAAA，
# 而 VPS 本身没有 IPv6 公网，导致 httpx/asyncpg 报 "name resolution" 失败。
# 这里把关键外部服务固定到 Cloudflare anycast IPv4，绕过 IPv6-only 解析。
ensure_host_pin() {
    local ip="$1"
    local host="$2"
    # 幂等：已存在则跳过
    if grep -qE "^\s*${ip}\s+${host}\s*$" /etc/hosts 2>/dev/null; then
        return 0
    fi
    # 删除该 host 的旧 pin（可能 IP 已更新）
    sed -i "/[[:space:]]${host//./\\.}[[:space:]]*$/d" /etc/hosts 2>/dev/null || true
    echo "${ip} ${host}" >> /etc/hosts
}

# 1. CockroachDB Cloud（从 .env 读取主机名）
CRDB_URL=$(grep -E "^COCKROACHDB_URL=" "$DEPLOY_DIR/.env" 2>/dev/null | head -1 | sed 's/.*@\([^:]*\).*/\1/')
if [[ -n "$CRDB_URL" ]]; then
    # 用 getent 取当前解析到的任一 IPv4（CRDB Cloud 的 ELB 有多个）
    CRDB_IP=$(getent ahostsv4 "$CRDB_URL" 2>/dev/null | head -1 | awk '{print $1}')
    if [[ -n "$CRDB_IP" ]]; then
        ensure_host_pin "$CRDB_IP" "$CRDB_URL"
        success "已 pin CockroachDB: $CRDB_IP → $CRDB_URL"
    else
        warn "无法解析 CockroachDB 主机 $CRDB_URL 的 IPv4，跳过（请检查 DNS）"
    fi
fi

# 2. Cloudflare R2（从 .env 读取 endpoint，回退用 account_id 拼接）
R2_ENDPOINT=$(grep -E "^R2_ENDPOINT=" "$DEPLOY_DIR/.env" 2>/dev/null | head -1 | sed 's/.*=//' | sed 's|^https\\?://||' | sed 's|/.*||' | tr -d '`' )
R2_ACCOUNT=$(grep -E "^R2_ACCOUNT_ID=" "$DEPLOY_DIR/.env" 2>/dev/null | head -1 | sed 's/.*=//')
R2_HOST="${R2_ENDPOINT:-${R2_ACCOUNT}.r2.cloudflarestorage.com}"
R2_HOST="${R2_HOST#\`}"  # 去除可能的反引号
R2_HOST="${R2_HOST%\`}"
if [[ -n "$R2_HOST" ]]; then
    # R2 用 Cloudflare anycast IPv4（104.16.0.0/12 段），不依赖 DNS 返回
    ensure_host_pin "104.16.0.1" "$R2_HOST"
    success "已 pin Cloudflare R2: 104.16.0.1 → $R2_HOST"
fi

# 3. Telegram API（可选，通常 DNS 正常，但保险起见也 pin 上）
# TG_IP=$(getent ahostsv4 api.telegram.org 2>/dev/null | head -1 | awk '{print $1}')
# [[ -n "$TG_IP" ]] && ensure_host_pin "$TG_IP" "api.telegram.org"

success "DNS pinning 完成"

# ──────────────────────────────────────────────
# 第七步：启动所有服务
# ──────────────────────────────────────────────
info "第七步：启动所有服务..."

# 先启动数据库备份（无依赖）
systemctl start "${SVC_PREFIX}-db_backup" || warn "启动 ${SVC_PREFIX}-db_backup 失败，请查看日志"
sleep 1

# 启动 db_writer（Redis 就绪后，其他 bot 依赖 Writer 落盘，必须先于 bot 启动）
systemctl start "${SVC_PREFIX}-db_writer" || warn "启动 ${SVC_PREFIX}-db_writer 失败，请查看日志"
sleep 2

# 启动核心 Bot（up 和 idx 先启动，让拓扑初始化完成）
systemctl start "${SVC_PREFIX}-up" || warn "启动 ${SVC_PREFIX}-up 失败，请查看日志"
systemctl start "${SVC_PREFIX}-idx" || warn "启动 ${SVC_PREFIX}-idx 失败，请查看日志"
sleep 3

# 启动其余服务
systemctl start "${SVC_PREFIX}-dsp" || warn "启动 ${SVC_PREFIX}-dsp 失败，请查看日志"
systemctl start "${SVC_PREFIX}-mon" || warn "启动 ${SVC_PREFIX}-mon 失败，请查看日志"
systemctl start "${SVC_PREFIX}-admin_bot" || warn "启动 ${SVC_PREFIX}-admin_bot 失败，请查看日志"
systemctl start "${SVC_PREFIX}-admin" || warn "启动 ${SVC_PREFIX}-admin 失败，请查看日志"

sleep 3

# ──────────────────────────────────────────────
# 第八步：拓扑刷新（清理 git 占位 ID，从 .env 重新生成）
# ──────────────────────────────────────────────
info "第八步：拓扑刷新..."

source venv/bin/activate
cd "$DEPLOY_DIR"

# 检查 topology.yaml 是否含有占位频道 ID（-1000000000xxx），有则从 .env 重新生成
if grep -q -- "-1000000000" config/topology.yaml 2>/dev/null; then
    warn "检测到 topology.yaml 含有占位频道 ID，从 .env 重新生成..."
    # P3: 拓扑刷新以 root 身份执行，无论 Python 是否中途失败都必须 chown，
    # 否则 data/ 下文件属主会残留为 root，导致 tgjiema 服务无法读写。
    # 因此临时关闭 set -e，确保 chown 一定被执行。
    set +e
    "${DEPLOY_DIR}/venv/bin/python" -c "
import asyncio
from database import init_db, close_db, get_cells_col
from admin.seed_topology import seed

async def refresh():
    await init_db()
    col = get_cells_col()
    count = await col.count_documents({})
    if count > 0:
        await col.delete_many({})
        print(f'cells 表 {count} 个旧槽位已清空')
    await close_db()
asyncio.run(refresh())
"
    refresh_rc1=$?
    "${DEPLOY_DIR}/venv/bin/python" admin/seed_topology.py --yes
    refresh_rc2=$?
    set -e
    # P3: 无条件 chown，防止 root 属主文件残留
    chown -R tgjiema:tgjiema "$DEPLOY_DIR"
    if [[ $refresh_rc1 -ne 0 || $refresh_rc2 -ne 0 ]]; then
        warn "拓扑刷新过程中发生错误(rc1=$refresh_rc1, rc2=$refresh_rc2)，但属主已修复为 tgjiema"
        info "请手动检查 topology.yaml 和 admin/seed_topology.py 输出"
    else
        success "拓扑已从 .env 刷新，频道 ID 已更新"
        info "请手动执行: systemctl restart tgjiema.target"
    fi
else
    success "topology.yaml 已包含真实频道 ID，跳过"
fi

# ──────────────────────────────────────────────
# 第九步：状态检查
# ──────────────────────────────────────────────
info "第九步：状态检查..."

echo ""
echo "--------------------------------------------------------------------------------"
printf "%-20s %-10s %s\n" "服务名" "状态" "PID"
echo "--------------------------------------------------------------------------------"

ALL_OK=true
for entry in "${SERVICES[@]}"; do
    IFS=":" read -r name desc detail <<< "$entry"
    svc="${SVC_PREFIX}-${name}"
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "failed")
    pid=$(systemctl show -p MainPID "$svc" 2>/dev/null | cut -d= -f2)
    if [[ "$status" == "active" ]]; then
        printf "${GREEN}%-20s %-10s %s${NC}\n" "$svc" "$status" "$pid"
    else
        printf "${RED}%-20s %-10s %s${NC}\n" "$svc" "$status" "$pid"
        ALL_OK=false
    fi
done

echo "--------------------------------------------------------------------------------"
echo ""

if [[ "$ALL_OK" == "true" ]]; then
    success "全部 8 个服务运行正常！"
else
    warn "部分服务未正常启动，请查看日志："
    echo "  journalctl -u ${SVC_PREFIX}-<name> -n 30 --no-pager"
fi

# ──────────────────────────────────────────────
# 完成
# ──────────────────────────────────────────────
echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "  项目目录:    $DEPLOY_DIR"
echo "  日志目录:    $DEPLOY_DIR/logs/"
echo "  配置文件:    $DEPLOY_DIR/.env"
echo ""
echo "  ── 常用命令 ──"
echo ""
echo "  查看所有状态:"
echo "    systemctl status tgjiema-*"
echo ""
echo "  一键启停全部:"
echo "    systemctl start tgjiema.target      # 启动全部"
echo "    systemctl stop tgjiema.target       # 停止全部"
echo "    systemctl restart tgjiema.target    # 重启全部"
echo ""
echo "  单独操作某个 Bot:"
echo "    systemctl restart tgjiema-up        # 重启上传Bot"
echo "    systemctl stop tgjiema-dsp          # 停止分发Bot"
echo "    systemctl start tgjiema-idx         # 启动解码Bot"
echo ""
echo "  实时日志:"
echo "    journalctl -u tgjiema-up -f         # 上传Bot日志"
echo "    journalctl -u tgjiema-dsp -f        # 分发Bot日志"
echo "    journalctl -u tgjiema -f            # 全部日志"
echo "    journalctl -u tgjiema-* -f          # 全部日志(通配符)"
echo ""
echo "  错误日志:"
echo "    journalctl -u tgjiema-* -p err -n 50 --no-pager"
echo ""

# ──────────────────────────────────────────────
# 第十步：TLS 反代（Caddy）— 可选，仅当 caddy 已安装时自动配置
# ──────────────────────────────────────────────
info "第十步：TLS 反代（Caddy）配置检查..."

if command -v caddy &> /dev/null; then
    info "检测到 Caddy，正在生成 TLS 反代配置模板..."
    CADDYFILE="/etc/caddy/Caddyfile.tgjiema"
    cat > "$CADDYFILE" << 'CADDY_EOF'
# ============================================================
#  TGJiema 管理后台 — Caddy TLS 反代配置
#  生成方式: deploy_vps_per_bot.sh 自动检测
#  使用说明:
#    1. 将 your-domain.com 替换为你的真实域名（DNS 需已指向本机）
#    2. 包含此配置到 Caddyfile 或复制到 /etc/caddy/Caddyfile
#    3. systemctl reload caddy
#    4. 在 .env 中设置 CSRF_COOKIE_SECURE=1
# ============================================================

# --- 方法一：独立站点配置（推荐）---
# 执行: sudo cp /etc/caddy/Caddyfile.tgjiema /etc/caddy/Caddyfile
#       然后修改下方域名后: sudo systemctl reload caddy

your-domain.com {
    reverse_proxy localhost:8080

    # 可选：IP 白名单（仅允许特定 IP 访问管理后台）
    # @allowed remote_ip 1.2.3.4 5.6.7.8
    # handle @allowed {
    #     reverse_proxy localhost:8080
    # }
    # handle {
    #     respond "Access Denied" 403
    # }

    # 可选：Basic Auth 作为额外安全层
    # basicauth {
    #     admin $2a$14$...
    # }

    # 安全头
    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
}

# --- 方法二：作为片段引入已有 Caddyfile ---
# 在已有的 Caddyfile 站点块中添加: reverse_proxy localhost:8080
CADDY_EOF

    chmod 644 "$CADDYFILE"
    success "Caddy 反代模板已生成: $CADDYFILE"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  TLS 反代配置说明:${NC}"
    echo -e "${YELLOW}  1. 编辑 $CADDYFILE${NC}"
    echo -e "${YELLOW}     将 your-domain.com 替换为你的真实域名${NC}"
    echo -e "${YELLOW}  2. 复制到 Caddy 配置目录:${NC}"
    echo -e "${YELLOW}     sudo cp $CADDYFILE /etc/caddy/Caddyfile${NC}"
    echo -e "${YELLOW}  3. 重载 Caddy（自动申请 Let's Encrypt 证书）:${NC}"
    echo -e "${YELLOW}     sudo systemctl reload caddy${NC}"
    echo -e "${YELLOW}  4. 在 .env 中启用 Secure Cookie:${NC}"
    echo -e "${YELLOW}     echo 'CSRF_COOKIE_SECURE=1' >> $DEPLOY_DIR/.env${NC}"
    echo -e "${YELLOW}  5. 重启管理服务:${NC}"
    echo -e "${YELLOW}     sudo systemctl restart ${SVC_PREFIX}-admin${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
else
    info "未检测到 Caddy（跳过 TLS 反代配置）"
    info "如需启用 HTTPS，请安装 Caddy 后重新运行此脚本，或手动执行："
    info "  bash deploy_tls_caddy.sh"
    echo ""
    echo -e "${YELLOW}  ⚠ 警告：管理后台通过 HTTP 明文传输凭据，仅限可信内网使用！${NC}"
    echo -e "${YELLOW}  生产环境强烈建议安装 Caddy 启用 HTTPS:${NC}"
    echo -e "${YELLOW}    sudo apt install -y caddy${NC}"
    echo -e "${YELLOW}    bash deploy_tls_caddy.sh${NC}"
    echo ""
fi