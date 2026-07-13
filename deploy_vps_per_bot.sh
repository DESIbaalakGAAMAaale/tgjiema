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
# R33 P1-3: Redis 持久化配置(AOF + noeviction),防止崩溃丢消息 + 内存写满逐出
if ! command -v redis-cli &> /dev/null; then
    echo "安装 Redis..."
    apt-get install -y redis-server
fi

# R33 P1-3: 配置 Redis 持久化(幂等,重复运行不破坏已有配置)
configure_redis_persistence() {
    local redis_conf="/etc/redis/redis.conf"
    if [[ ! -f "$redis_conf" ]]; then
        # Debian 12+ 路径可能在 /etc/redis/redis.conf
        warn "未找到 $redis_conf,跳过 Redis 持久化配置"
        return 0
    fi
    info "配置 Redis 持久化(AOF everysec + noeviction)..."

    # 用 sed 原地修改(幂等: 先删除可能存在的旧配置行再追加新值)
    # appendonly yes: 启用 AOF(更耐丢数据,R33 P1-3 要求)
    sed -i 's/^\s*appendonly\s.*/appendonly yes/' "$redis_conf"
    if ! grep -qE '^\s*appendonly\s+yes' "$redis_conf"; then
        echo "appendonly yes" >> "$redis_conf"
    fi
    # appendfsync everysec: 每秒 fsync(性能与安全的平衡,最多丢 1 秒数据)
    sed -i 's/^\s*appendfsync\s.*/appendfsync everysec/' "$redis_conf"
    if ! grep -qE '^\s*appendfsync\s+everysec' "$redis_conf"; then
        echo "appendfsync everysec" >> "$redis_conf"
    fi
    # maxmemory-policy noeviction: 内存满时不逐出写入(返回错误),防止写消息丢失
    # R33 P1-3: 避免驱逐策略导致 Stream 消息被删除
    sed -i 's/^\s*maxmemory-policy\s.*/maxmemory-policy noeviction/' "$redis_conf"
    if ! grep -qE '^\s*maxmemory-policy\s+noeviction' "$redis_conf"; then
        echo "maxmemory-policy noeviction" >> "$redis_conf"
    fi
    # 可选: 设置最大内存(默认 0=不限制,VPS 上建议设置)
    # sed -i 's/^\s*maxmemory\s.*/maxmemory 512mb/' "$redis_conf"

    # 重启 Redis 应用配置
    systemctl enable redis-server
    systemctl restart redis-server
    sleep 1
    if systemctl is-active --quiet redis-server; then
        success "Redis 持久化已配置(appendonly=yes, appendfsync=everysec, maxmemory-policy=noeviction)"
    else
        warn "Redis 重启失败,请手动检查: systemctl status redis-server"
    fi
}
configure_redis_persistence

# ──────────────────────────────────────────────
# R39 P0-1 + P0-2: Redis ACL 初始化(占位符 sed 替换 + healthcheck 用户)
# 参考文档: docs/redis-security.md, docs/redis-acl-setup.md
# ──────────────────────────────────────────────
init_redis_acl() {
    if ! command -v redis-cli &> /dev/null; then
        warn "未找到 redis-cli,跳过 ACL 初始化"
        return 0
    fi
    info "R39 P0-2: 初始化 Redis ACL(占位符 sed 替换 + 正向白名单 + tgjiema:* 命名空间)..."

    local env_file="$DEPLOY_DIR/.env"
    local writer_pwd reader_pwd health_pwd
    # R39 P0-2: 优先读取新变量名 REDIS_WRITER_PASSWORD / REDIS_READER_PASSWORD / REDIS_HEALTH_PASSWORD
    writer_pwd=$(grep -E '^REDIS_WRITER_PASSWORD=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')
    reader_pwd=$(grep -E '^REDIS_READER_PASSWORD=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')
    health_pwd=$(grep -E '^REDIS_HEALTH_PASSWORD=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')
    # 兼容: 若旧变量 REDIS_WRITER_PWD / REDIS_READER_PWD 存在,迁移到新变量名
    if [[ -z "$writer_pwd" ]]; then
        writer_pwd=$(grep -E '^REDIS_WRITER_PWD=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    if [[ -z "$reader_pwd" ]]; then
        reader_pwd=$(grep -E '^REDIS_READER_PWD=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    if [[ -z "$writer_pwd" ]]; then
        writer_pwd=$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | xxd -p)
        echo "REDIS_WRITER_PASSWORD=${writer_pwd}" >> "$env_file"
        success "已生成 REDIS_WRITER_PASSWORD 并写入 .env"
    fi
    if [[ -z "$reader_pwd" ]]; then
        reader_pwd=$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | xxd -p)
        echo "REDIS_READER_PASSWORD=${reader_pwd}" >> "$env_file"
        success "已生成 REDIS_READER_PASSWORD 并写入 .env"
    fi
    if [[ -z "$health_pwd" ]]; then
        # R39 P0-1: healthcheck 用户密码(仅 PING 权限,密码也需随机但权限极低)
        health_pwd=$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | xxd -p)
        echo "REDIS_HEALTH_PASSWORD=${health_pwd}" >> "$env_file"
        success "已生成 REDIS_HEALTH_PASSWORD 并写入 .env(healthcheck 用户专用)"
    fi

    # R40 P0-3: 优先使用 users.acl.template(render_acl.sh 规范模板),
    # 缺失时回退到 users.acl(向后兼容)。两文件内容相同,均为占位符模板。
    # 占位符格式: <REDIS_WRITER_PASSWORD> / <REDIS_READER_PASSWORD> / <REDIS_HEALTH_PASSWORD>
    local acl_template="$DEPLOY_DIR/config/redis/users.acl.template"
    if [[ ! -f "$acl_template" ]]; then
        # 回退兼容: 旧版 deploy 脚本读取 config/redis/users.acl
        acl_template="$DEPLOY_DIR/config/redis/users.acl"
    fi
    local acl_target="/etc/redis/users.acl"
    if [[ ! -f "$acl_template" ]]; then
        warn "未找到 ACL 模板 $acl_template,回退到旧 redis-cli ACL SETUSER 模式"
        # 兼容回退: 直接调用 ACL SETUSER(向后兼容)
        redis-cli ACL SETUSER tgjiema_writer on >"${writer_pwd}" ~tgjiema:* \
            -@all +XADD +XREADGROUP +XACK +XLEN +XPENDING +XCLAIM +XTRIM +XINFO +XDEL +PING +EXPIRE +TTL +SET +GET +DEL 2>/dev/null \
            && success "Redis ACL 用户 tgjiema_writer 已创建(回退模式, 写权限)" \
            || warn "Redis ACL SETUSER tgjiema_writer 失败"
        redis-cli ACL SETUSER tgjiema_reader on >"${reader_pwd}" ~tgjiema:* \
            -@all +XREADGROUP +XINFO +XLEN +XPENDING +PING +GET +TTL 2>/dev/null \
            && success "Redis ACL 用户 tgjiema_reader 已创建(回退模式, 只读)" \
            || warn "Redis ACL SETUSER tgjiema_reader 失败"
        redis-cli ACL SETUSER health on >"${health_pwd}" ~* &* \
            -@all +PING 2>/dev/null \
            && success "Redis ACL 用户 health 已创建(回退模式, 仅 PING)" \
            || warn "Redis ACL SETUSER health 失败"
        redis-cli ACL SETUSER default off 2>/dev/null \
            && success "Redis default 用户已禁用" \
            || warn "Redis 禁用 default 失败"
        redis-cli ACL SAVE 2>/dev/null && success "Redis ACL 已持久化(回退模式)"
    else
        # R39 P0-2: 主路径 — sed 替换占位符,部署 ACL 文件
        # 用 | 作为 sed 分隔符避免密码中可能的 / 冲突
        # 占位符在 ACL 文件中是 <REDIS_WRITER_PASSWORD> 等形式
        sed \
            -e "s|<REDIS_WRITER_PASSWORD>|${writer_pwd}|g" \
            -e "s|<REDIS_READER_PASSWORD>|${reader_pwd}|g" \
            -e "s|<REDIS_HEALTH_PASSWORD>|${health_pwd}|g" \
            "$acl_template" > "$acl_target"
        chmod 600 "$acl_target"
        chown redis:redis "$acl_target" 2>/dev/null || true
        success "ACL 文件已从模板生成并写入 $acl_target (R39 P0-2: 占位符 sed 替换)"

        # 配置 redis.conf 加载 ACL 文件(幂等)
        local redis_conf="/etc/redis/redis.conf"
        if [[ -f "$redis_conf" ]]; then
            # 移除可能存在的旧 aclfile 配置行
            sed -i '/^\s*aclfile\s/d' "$redis_conf"
            echo "aclfile $acl_target" >> "$redis_conf"
            systemctl restart redis-server 2>/dev/null || systemctl restart redis 2>/dev/null || true
            sleep 1
            success "Redis ACL 已加载(从 $acl_target)"
        fi

        # 验证 health 用户能 PING(若 redis 可用)
        if redis-cli --user health -a "${health_pwd}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
            success "R39 P0-1: health 用户 PING 验证通过(healthcheck 可用)"
        else
            warn "R39 P0-1: health 用户 PING 验证失败,请检查 ACL 文件 + 密码"
        fi
    fi

    info "  R39 P0-2: 业务 Bot 应使用 redis://tgjiema_writer:<pwd>@127.0.0.1:6379/0 连接"
    info "  db_writer 用 writer,其它 Bot 用 reader(或 writer 视需要)"
    info "  REDIS_URL 在 .env.shared 中应嵌入凭证(部署后由 deploy 脚本辅助更新)"
}
init_redis_acl

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

# ──────────────────────────────────────────────
# R33 P1-5: 服务级 secrets 隔离(最小权限)
# 原: 所有 8 个 systemd 服务加载同一份 .env,每个服务都能看到所有 5 个 Bot Token + R2 凭证 + ADMIN_PASSWORD
# 新: 拆分为 .env.shared(共享配置) + .env.secrets.<service>(仅该服务需要的 secrets)
# 兼容: 若 .env.secrets.<service> 不存在,回退到加载完整 .env(向后兼容旧部署)
# ──────────────────────────────────────────────
info "R33: 配置服务级 secrets 隔离(最小权限)..."

split_env_per_service() {
    local env_file="$DEPLOY_DIR/.env"
    if [[ ! -f "$env_file" ]]; then
        return 0
    fi

    # 定义每个服务需要的 secret 变量(仅 secrets,不含共享配置)
    # 格式: "服务名:变量1,变量2,..."
    local -A SERVICE_SECRETS=(
        # R37 P0-1: migration 需要直连 CRDB 执行 DDL,必须注入 COCKROACHDB_URL
        [migration]="COCKROACHDB_URL"
        [up]="UPLOAD_BOT_TOKEN,RELAY_API_ID,RELAY_API_HASH,RELAY_ENCRYPTION_KEY,RELAY_ACCOUNT_IDS,COLLECTOR_ACCOUNT_IDS"
        [idx]="DECODER_BOT_TOKEN,RELAY_API_ID,RELAY_API_HASH,RELAY_ENCRYPTION_KEY,RELAY_ACCOUNT_IDS,ALLOWED_DECODER_BOTS"
        [dsp]="SENDER_BOT_TOKEN"
        [mon]="MON_BOT_TOKEN,ADMIN_BOT_TOKEN,ADMIN_TELEGRAM_ID,DB_WRITER_SERVICE_NAME"
        [admin_bot]="ADMIN_BOT_TOKEN,ADMIN_TELEGRAM_ID"
        [admin]="ADMIN_USERNAME,ADMIN_PASSWORD,ADMIN_WEB_PORT,ADMIN_WEB_HOST,CSRF_COOKIE_SECURE"
        [db_backup]="R2_ACCOUNT_ID,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET_NAME,R2_ENDPOINT,DB_BACKUP_ENABLED,DB_BACKUP_INTERVAL_MINUTES,COCKROACHDB_URL,BACKUP_KEK"
        [db_writer]=""  # 无 secrets,只用 REDIS_URL(来自 .env.shared)
        # R36 §6.3: crdb_sync 需要连接 CRDB
        [crdb_sync]="COCKROACHDB_URL"
        # R38 P1-9: prometheus_exporter 无 secrets
        [prometheus_exporter]=""
    )

    # 共享变量(所有服务都需要,写入 .env.shared)
    # 注意: 非敏感配置放这里(REDIS_URL, 配额, 限流参数等)
    local shared_vars_pattern="^(REDIS_URL|WRITER_|CRDB_POOL_|CACHE_|RATE_LIMIT_|ROTATION_|DATA_RETENTION|CRDB_CLEANUP|CHANNEL_FAILURE|RESTART_|TOPOLOGY_|FREE_|BASIC_|PREMIUM_|FILE_CODE_PREFIX|DEFAULT_|PENDING_|SEND_|PAGE_|MEDIA_GROUP|EXTERNAL_|CACHE_STORE_|MAX_RESTART|MON_CHECK_INTERVAL|QUOTA_SYNC_|RELAY_WEIGHT|RELAY_NORM|RELAY_SAFE_POOL|ACCOUNT_|R100_CHANNEL|UPLOAD_BOT_USERNAME|DECODER_BOT_USERNAME|SENDER_BOT_USERNAME|FORCE_JOIN_|LOG_LEVEL|DB_BACKUP_INTERVAL_MINUTES)="

    # R34 P1-2: 生成 .env.shared(共享配置) — > 重定向即使 grep 无匹配也会创建空文件,
    # 但需校验非空(REDIS_URL, WRITER_* 等共享配置缺失会导致服务启动失败)
    grep -E "$shared_vars_pattern" "$env_file" 2>/dev/null > "$DEPLOY_DIR/.env.shared" || true
    if [[ ! -s "$DEPLOY_DIR/.env.shared" ]]; then
        warn "R34: .env.shared 为空,无法从 .env 提取共享配置(REDIS_URL, WRITER_* 等)"
        warn "  请检查 .env 是否包含 REDIS_URL=... 等共享变量,否则服务可能无法启动"
    fi
    chmod 600 "$DEPLOY_DIR/.env.shared"

    # 为每个服务生成 .env.secrets.<service>
    # R34 P1-2: 不再静默回退到完整 .env;对需要 secrets 的服务做非空校验
    local missing_secrets=()
    for svc in "${!SERVICE_SECRETS[@]}"; do
        local secrets_file="$DEPLOY_DIR/.env.secrets.${svc}"
        local var_list="${SERVICE_SECRETS[$svc]}"
        : > "$secrets_file"  # 清空/创建文件(即使无 secrets 也创建空文件,chmod 600)
        if [[ -n "$var_list" ]]; then
            IFS=',' read -ra vars <<< "$var_list"
            for var in "${vars[@]}"; do
                # 从主 .env 中提取该变量(兼容行内注释)
                grep -E "^${var}=" "$env_file" 2>/dev/null >> "$secrets_file" || true
            done
            # R34 P1-2: 验证 secrets 文件非空(对需要 secrets 的服务)
            if [[ ! -s "$secrets_file" ]]; then
                warn "R34: .env.secrets.${svc} 为空,但该服务需要 secrets: ${var_list}"
                warn "  请在 .env 中配置上述变量,否则 ${svc} 服务可能无法启动"
                missing_secrets+=("$svc")
            fi
        fi
        chmod 600 "$secrets_file"
    done

    # 保留原 .env 文件作为备份和参考,systemd 不再直接加载完整 .env
    # 权限收紧: 仅所有者可读
    chmod 600 "$env_file"

    if [[ ${#missing_secrets[@]} -gt 0 ]]; then
        warn "R34: 以下服务的 secrets 文件为空(可能影响启动): ${missing_secrets[*]}"
        warn "  请编辑 .env 补充缺失的变量后重新运行部署脚本"
    fi
    success "secrets 隔离完成: .env.shared + .env.secrets.<service> (8 个服务)"
    info "  R34 P1-2: systemd 单元不再加载完整 .env,实现真隔离"
    info "  原 .env 已保留作为备份和参考,权限 600"
}

split_env_per_service

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
    "db_writer:数据库写入:40:on-failure:10"
    # R36 §6.3: 单一 crdb_sync 服务(独占 CRDB 同步事实源)
    "crdb_sync:CRDB同步服务:40:on-failure:10"
    # R38 P1-9: prometheus exporter(metrics 暴露)
    "prometheus_exporter:Prometheus exporter:15:always:10"
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
    # P1修复: db_writer 是 Redis 硬依赖,使用 Requires= 强制 Redis 启动
    # (无 Redis 时 db_writer init 抛 RuntimeError,Restart=on-failure 会紧密重启循环)
    local requires_dep=""
    if [[ "$name" == "db_writer" ]]; then
        requires_dep="Requires=redis.service"
    fi

    cat > "/etc/systemd/system/${svc}.service" << EOF
[Unit]
Description=TG文件解码器 — ${desc}
After=${after_deps}
Wants=network.target
${requires_dep}
PartOf=${SVC_PREFIX}.target
# systemd 防抖:60秒内最多重启5次,超限后进入 failed 状态
# (需手动 systemctl reset-failed <service> 后才能重启)
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
User=tgjiema
WorkingDirectory=${DEPLOY_DIR}
Environment="PYTHONUNBUFFERED=1"
# R33 P1-5: 服务级 secrets 隔离(最小权限)
# R34 P1-2: 真隔离 — 移除 EnvironmentFile=-.env 回退,防止各服务读取全部
#           Token/CRDB/R2/管理员凭据(原隔离被第三行 .env 抵消)
# 1. .env.shared: 共享配置(REDIS_URL, 配额, 限流参数等,所有服务可读)
# 2. .env.secrets.${name}: 仅该服务需要的 secrets(Bot Token, R2 凭证等)
# 若对应文件不存在,systemd 因 `-` 前缀不报错(由 split_env_per_service 自动生成)
EnvironmentFile=-${DEPLOY_DIR}/.env.shared
EnvironmentFile=-${DEPLOY_DIR}/.env.secrets.${name}
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

# R37 P2-2: 最小权限沙箱(NoNewPrivileges + ProtectSystem + CapabilityBoundingSet 等)
# 参考文档: docs/least-privilege.md
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${DEPLOY_DIR}/data ${DEPLOY_DIR}/logs
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectProc=invisible
PrivateDevices=true

[Install]
WantedBy=multi-user.target
EOF

    echo "  [OK] ${svc}.service 已创建"
}

for entry in "${SERVICES[@]}"; do
    IFS=":" read -r name desc detail <<< "$entry"
    generate_service "$name" "$desc" "$detail"
done

# R36 §6.4.3: migration/bootstrap 改为 systemd oneshot;业务 Bot 禁止执行 DDL
# 所有 Bot 服务 After=+Requires= tgjiema-migration.service,确保 DDL 先完成
info "创建 migration oneshot 服务(DDL 一次性执行,业务 Bot 不再触发 DDL)..."

cat > "/etc/systemd/system/${SVC_PREFIX}-migration.service" << EOF
[Unit]
Description=TG文件解码器 — DDL 迁移(oneshot)
After=network.target
Before=${SVC_PREFIX}-up.service ${SVC_PREFIX}-idx.service ${SVC_PREFIX}-dsp.service ${SVC_PREFIX}-mon.service ${SVC_PREFIX}-admin_bot.service ${SVC_PREFIX}-admin.service ${SVC_PREFIX}-db_backup.service ${SVC_PREFIX}-db_writer.service ${SVC_PREFIX}-crdb_sync.service ${SVC_PREFIX}-prometheus_exporter.service
PartOf=${SVC_PREFIX}.target

[Service]
Type=oneshot
User=tgjiema
WorkingDirectory=${DEPLOY_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="SERVICE_ROLE=migration"
EnvironmentFile=-${DEPLOY_DIR}/.env.shared
EnvironmentFile=-${DEPLOY_DIR}/.env.secrets.migration
# R37 P1-8: 调用 migration_runner 执行 DDL(唯一允许的 DDL/TTL/版本写入入口)
# 业务 Bot 调用 init_db() 仅做 runtime 连接,不执行 DDL/bootstrap
ExecStart=${DEPLOY_DIR}/venv/bin/python -m services.migration_runner
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SVC_PREFIX}-migration

# 安全加固
NoNewPrivileges=true
PrivateTmp=true

# R37 P2-2: migration oneshot 也加最小权限沙箱
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${DEPLOY_DIR}/data ${DEPLOY_DIR}/logs
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectProc=invisible
PrivateDevices=true

[Install]
WantedBy=multi-user.target
EOF

echo "  [OK] ${SVC_PREFIX}-migration.service 已创建(oneshot)"
systemctl enable "${SVC_PREFIX}-migration.service" || warn "启用 migration.service 失败(可忽略,稍后手动启动)"

# 业务 Bot 服务增加对 migration 的依赖(已生成的 service 文件追加 After/Requires)
for entry in "${SERVICES[@]}"; do
    IFS=":" read -r name desc detail <<< "$entry"
    svc_file="/etc/systemd/system/${SVC_PREFIX}-${name}.service"
    if [[ -f "$svc_file" ]]; then
        # 在 After= 行追加 migration.service(若未包含)
        if ! grep -q "migration.service" "$svc_file"; then
            sed -i "s|^After=network.target redis.service|After=network.target redis.service ${SVC_PREFIX}-migration.service|" "$svc_file"
            sed -i "s|^Wants=network.target|Wants=network.target\nRequires=${SVC_PREFIX}-migration.service|" "$svc_file"
        fi
    fi
done

# 额外：创建一键启停所有服务的 target
info "创建聚合 target..."

cat > "/etc/systemd/system/${SVC_PREFIX}.target" << EOF
[Unit]
Description=TG文件解码器 — 全部服务
Wants=${SVC_PREFIX}-migration.service ${SVC_PREFIX}-up.service ${SVC_PREFIX}-idx.service ${SVC_PREFIX}-dsp.service ${SVC_PREFIX}-mon.service ${SVC_PREFIX}-admin_bot.service ${SVC_PREFIX}-admin.service ${SVC_PREFIX}-db_backup.service ${SVC_PREFIX}-db_writer.service ${SVC_PREFIX}-crdb_sync.service ${SVC_PREFIX}-prometheus_exporter.service
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

# R36 §6.3: 启动 crdb_sync(单一 CRDB 同步事实源,与其他 Bot 并行运行)
systemctl start "${SVC_PREFIX}-crdb_sync" || warn "启动 ${SVC_PREFIX}-crdb_sync 失败，请查看日志"
sleep 1

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