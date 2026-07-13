#!/usr/bin/env bash
# R44 G0-5: 全新机恢复脚本
# 在新机器上从 CRDB 托管备份 + 加密 R2 bundle + SQLite/Relay session 恢复完整服务
#
# 前置条件:
# - 新机器已安装 Docker、docker-compose
# - .env.secrets.* 文件已安全传输到新机器
# - CRDB Cloud 备份可用
# - R2 bundle 已上传到 R2
#
# 使用方法: ./scripts/full_machine_recovery.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "=== R44 G0-5: 全新机恢复开始 ==="
echo "时间: $(date)"
echo "目标 RTO: ≤ 30 分钟"
START_TIME=$(date +%s)

# Step 1: 拉取代码
echo ""
echo "[1/8] 拉取代码..."
git pull origin master
echo "✓ 代码已更新到 $(git rev-parse --short HEAD)"

# Step 2: 加载环境变量
echo ""
echo "[2/8] 加载环境变量..."
if [ ! -f .env.shared ]; then
    echo "ERROR: .env.shared 不存在,请从安全渠道传输"
    exit 1
fi
source .env.shared
echo "✓ .env.shared 已加载"

# Step 3: 启动 Redis(恢复前置依赖)
echo ""
echo "[3/8] 启动 Redis..."
docker-compose up -d redis
sleep 5
# 验证 Redis 可用
docker-compose exec -T redis redis-cli ping
echo "✓ Redis 已启动"

# Step 4: 运行 migration(创建表结构)
echo ""
echo "[4/8] 运行 database migration..."
# migration 是 oneshot 服务
systemctl start tgjiema-migration || docker-compose run --rm migration
echo "✓ Migration 完成"

# Step 5: 从 CRDB 拉取数据
echo ""
echo "[5/8] 从 CRDB 同步数据到本地 SQLite..."
systemctl start tgjiema-crdb_sync
# 等待 crdb_sync 完成首次同步
echo "等待 crdb_sync 完成首次同步(最多 5 分钟)..."
for i in $(seq 1 30); do
    if journalctl -u tgjiema-crdb_sync --since "5 minutes ago" | grep -q "首次同步完成\|initial sync complete"; then
        echo "✓ CRDB 同步完成"
        break
    fi
    sleep 10
done

# Step 6: 从 R2 恢复加密备份(可选,用于恢复历史文件)
echo ""
echo "[6/8] 从 R2 恢复加密备份(可选)..."
if [ -n "${R2_BUCKET:-}" ]; then
    echo "  从 R2 bucket $R2_BUCKET 拉取最新备份..."
    # 使用 backup_engine.restore 恢复
    python -c "
import asyncio
from services.backup_engine import BackupEngine
async def main():
    engine = BackupEngine()
    # 注意: production 恢复需要 approval_action_id
    # 在灾备场景下,应先通过 admin 后台发起 restore 审批
    print('请在 admin 后台发起 restore 审批,获取 approval_action_id 后执行:')
    print('  python -c \"from services.backup_engine import BackupEngine; ...\"')
asyncio.run(main())
" || echo "  (跳过,需要手动通过 admin 后台恢复)"
else
    echo "  (跳过,R2_BUCKET 未配置)"
fi

# Step 7: 启动所有业务服务
echo ""
echo "[7/8] 启动所有业务服务..."
SERVICES=(db_writer up idx dsp mon admin_bot admin)
for svc in "${SERVICES[@]}"; do
    systemctl start "tgjiema-${svc}" 2>/dev/null || docker-compose up -d "$svc"
    sleep 2
    if systemctl is-active --quiet "tgjiema-${svc}" 2>/dev/null; then
        echo "  ✓ tgjiema-${svc} 已启动"
    else
        echo "  ✗ tgjiema-${svc} 启动失败,请检查日志: journalctl -u tgjiema-${svc} -n 50"
    fi
done

# Step 8: 验证服务可用性
echo ""
echo "[8/8] 验证服务可用性..."
sleep 10  # 等待服务完全启动

# 验证 admin Web
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/login | grep -q "200\|302"; then
    echo "  ✓ Admin Web 可访问"
else
    echo "  ✗ Admin Web 不可访问"
fi

# 验证 Telegram bot(检查 systemd active)
for svc in up idx dsp; do
    if systemctl is-active --quiet "tgjiema-${svc}"; then
        echo "  ✓ tgjiema-${svc} 运行中"
    else
        echo "  ✗ tgjiema-${svc} 未运行"
    fi
done

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS=$((ELAPSED % 60))

echo ""
echo "=== 恢复完成 ==="
echo "总耗时: ${MINUTES}m${SECONDS}s"
if [ $ELAPSED -le 1800 ]; then
    echo "✓ RTO 达标(≤ 30 分钟)"
else
    echo "✗ RTO 超标(> 30 分钟),请优化恢复流程"
fi
echo ""
echo "下一步:"
echo "  1. 验证 Telegram bot 响应: 向 @${BOT_USERNAME:-your_bot} 发送测试消息"
echo "  2. 验证文件上传/解码: 上传测试文件"
echo "  3. 运行 72h 空载 RU 报告: python scripts/export_ru_report.py --hours 72"
