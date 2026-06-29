#!/bin/bash
# ============================================================
#  TG文件解码器 — VPS 部署（独立 systemd 版）
#  每个 Bot 独立 systemd 服务，可单独启停/重启
#  架构：环形冗余 v2（up / idx / dsp / mon / admin_bot / admin / db_backup）
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
echo "  每个 Bot 独立服务，共 7 个 systemd 单元"
echo "============================================"
echo ""

# ──────────────────────────────────────────────
# 第一步：系统依赖
# ──────────────────────────────────────────────
info "第一步：安装系统依赖..."

apt-get update
apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3.12-dev \
    libpq-dev gcc g++ make curl git sqlite3 procps net-tools

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

# ──────────────────────────────────────────────
# 第三步：虚拟环境
# ──────────────────────────────────────────────
info "第三步：创建 Python 虚拟环境..."

cd "$DEPLOY_DIR"

if [[ ! -d "venv" ]]; then
    python3.12 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt
pip install --no-cache-dir "uvloop>=0.19.0,<0.21.0" "orjson>=3.9.0,<4.0.0"

success "Python 依赖安装完成"

# ──────────────────────────────────────────────
# 第四步：检查 .env 和拓扑
# ──────────────────────────────────────────────
info "第四步：检查配置..."

ENV_FILE="$DEPLOY_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$DEPLOY_DIR/.env.example" "$ENV_FILE"
    warn "请编辑: $ENV_FILE"
    nano "$ENV_FILE"
fi

if [[ ! -f "$DEPLOY_DIR/config/topology.yaml" ]] && [[ -f "$DEPLOY_DIR/config/groups.yaml" ]]; then
    source venv/bin/activate
    cd "$DEPLOY_DIR"
    python config/generate_topology.py
    success "topology.yaml 生成完成"
fi

success "配置文件检查完成"

# ──────────────────────────────────────────────
# 第五步：创建 systemd 服务（7 个独立单元）
# ──────────────────────────────────────────────
info "第五步：创建 systemd 服务单元..."

# 服务定义：(名称, 描述, 启动类目)
SERVICES=(
    "up:上传接收Bot:接收用户文件，转发到存储频道"
    "idx:解码索引Bot:生成文件码，解码外部码，写派工表"
    "dsp:派送分发Bot:从jobs表轮询任务，通过媒体组发送给用户"
    "mon:监控管理Bot:频道健康监控，自动降级，环形指针推进"
    "admin_bot:管理员Bot:管理配置、用户、重置等操作"
    "admin:Web管理后台:Web管理面板，fastapi+uvicorn"
    "db_backup:数据库备份:定期备份数据库到R2"
)

# 生成 systemd 模板函数
generate_service() {
    local name="$1"
    local desc="$2"
    local detail="$3"
    local svc="${SVC_PREFIX}-${name}"

    cat > "/etc/systemd/system/${svc}.service" << EOF
[Unit]
Description=TG文件解码器 — ${desc}
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${DEPLOY_DIR}
Environment="PYTHONUNBUFFERED=1"
ExecStart=${DEPLOY_DIR}/venv/bin/python ${DEPLOY_DIR}/run_all.py --standalone ${name}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${svc}

# systemd 内置防抖:60秒内最多重启5次,超限后冷却30秒
StartLimitBurst=5
StartLimitInterval=60

# 优雅关闭
KillSignal=SIGINT
TimeoutStopSec=15

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
Wants=${SVC_PREFIX}-up.service ${SVC_PREFIX}-idx.service ${SVC_PREFIX}-dsp.service ${SVC_PREFIX}-mon.service ${SVC_PREFIX}-admin_bot.service ${SVC_PREFIX}-file_bot.service ${SVC_PREFIX}-admin.service ${SVC_PREFIX}-db_backup.service
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
# 第六步：启动所有服务
# ──────────────────────────────────────────────
info "第六步：启动所有服务..."

# 先启动数据库备份（无依赖）
systemctl start "${SVC_PREFIX}-db_backup" 2>/dev/null || true
sleep 1

# 启动核心 Bot（up 和 idx 先启动，让拓扑初始化完成）
systemctl start "${SVC_PREFIX}-up" 2>/dev/null || true
systemctl start "${SVC_PREFIX}-idx" 2>/dev/null || true
sleep 3

# 启动其余服务
systemctl start "${SVC_PREFIX}-dsp" 2>/dev/null || true
systemctl start "${SVC_PREFIX}-mon" 2>/dev/null || true
systemctl start "${SVC_PREFIX}-admin_bot" 2>/dev/null || true
systemctl start "${SVC_PREFIX}-file_bot" 2>/dev/null || true
systemctl start "${SVC_PREFIX}-admin" 2>/dev/null || true

sleep 3

# ──────────────────────────────────────────────
# 第七步：状态检查
# ──────────────────────────────────────────────
info "第七步：状态检查..."

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

if $ALL_OK; then
    success "全部 7 个服务运行正常！"
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