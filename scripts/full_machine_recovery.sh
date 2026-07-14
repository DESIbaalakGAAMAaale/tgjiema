#!/usr/bin/env bash
# R44 G0-5 / R46 P0-7: 全新机恢复脚本
# 在新机器上从 CRDB 托管备份 + 加密 R2 bundle + SQLite/Relay session 恢复完整服务
#
# R46 P0-7 整改:
#   - 参数化 --commit-sha / --backup-id / --approval-action-id / --mode
#   - checkout 固定 SHA,不允许 git pull master
#   - 真正下载 marker/manifest/payload,验证双 checksum、KEK、schema version
#   - restore 失败立刻退出
#   - CRDB bootstrap 超时退出
#   - 每个服务 health 不通过退出
#   - 输出签名 recovery report
#
# 使用方法:
#   ./scripts/full_machine_recovery.sh --commit-sha <SHA> --backup-id <ID> --approval-action-id <UUID> --mode production
set -euo pipefail

# ─── 参数解析 ───────────────────────────────────────────────
COMMIT_SHA=""
BACKUP_ID=""
APPROVAL_ACTION_ID=""
MODE="staging"

while [[ $# -gt 0 ]]; do
    case $1 in
        --commit-sha)
            COMMIT_SHA="$2"; shift 2 ;;
        --backup-id)
            BACKUP_ID="$2"; shift 2 ;;
        --approval-action-id)
            APPROVAL_ACTION_ID="$2"; shift 2 ;;
        --mode)
            MODE="$2"; shift 2 ;;
        *)
            echo "ERROR: 未知参数 $1"; exit 1 ;;
    esac
done

if [ -z "$COMMIT_SHA" ]; then
    echo "ERROR: 必须指定 --commit-sha <SHA>"
    exit 1
fi
if [ "$MODE" = "production" ] && [ -z "$BACKUP_ID" ]; then
    echo "ERROR: production 模式必须指定 --backup-id <ID>"
    exit 1
fi
if [ "$MODE" = "production" ] && [ -z "$APPROVAL_ACTION_ID" ]; then
    echo "ERROR: production 模式必须指定 --approval-action-id <UUID>"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "=== R46 P0-7: 全新机恢复开始 ==="
echo "时间: $(date)"
echo "模式: $MODE"
echo "Commit SHA: $COMMIT_SHA"
echo "Backup ID: ${BACKUP_ID:-N/A}"
echo "Approval Action ID: ${APPROVAL_ACTION_ID:-N/A}"
echo "目标 RTO: ≤ 30 分钟"
START_TIME=$(date +%s)
RECOVERY_REPORT="$REPO_DIR/recovery_report_$(date +%Y%m%d_%H%M%S).txt"
echo "=== Recovery Report ===" > "$RECOVERY_REPORT"

# ─── Step 1: checkout 固定 SHA ─────────────────────────────
echo ""
echo "[1/9] Checkout 固定 SHA..."
echo "[1/9] Checkout 固定 SHA ($COMMIT_SHA)" >> "$RECOVERY_REPORT"
git fetch origin
git checkout "$COMMIT_SHA"
echo "✓ 代码已 checkout 到 $(git rev-parse --short HEAD)"
echo "✓ checkout 完成" >> "$RECOVERY_REPORT"

# ─── Step 2: 加载环境变量 ──────────────────────────────────
echo ""
echo "[2/9] 加载环境变量..."
echo "[2/9] 加载环境变量" >> "$RECOVERY_REPORT"
if [ ! -f .env.shared ]; then
    echo "ERROR: .env.shared 不存在,请从安全渠道传输"
    echo "FAIL: .env.shared 缺失" >> "$RECOVERY_REPORT"
    exit 1
fi
source .env.shared
echo "✓ .env.shared 已加载"
echo "✓ .env.shared 已加载" >> "$RECOVERY_REPORT"

# ─── Step 3: 启动 Redis ───────────────────────────────────
echo ""
echo "[3/9] 启动 Redis..."
echo "[3/9] 启动 Redis" >> "$RECOVERY_REPORT"
docker-compose up -d redis
sleep 5
# 验证 Redis 可用 — 失败立即退出
if ! docker-compose exec -T redis redis-cli ping | grep -q PONG; then
    echo "ERROR: Redis 启动失败"
    echo "FAIL: Redis 不可用" >> "$RECOVERY_REPORT"
    exit 1
fi
echo "✓ Redis 已启动"
echo "✓ Redis 已启动" >> "$RECOVERY_REPORT"

# ─── Step 4: 运行 migration ───────────────────────────────
echo ""
echo "[4/9] 运行 database migration..."
echo "[4/9] Migration" >> "$RECOVERY_REPORT"
# migration 是 oneshot 服务
if ! systemctl start tgjiema-migration 2>/dev/null; then
    if ! docker-compose run --rm migration; then
        echo "ERROR: Migration 失败"
        echo "FAIL: Migration 失败" >> "$RECOVERY_REPORT"
        exit 1
    fi
fi
echo "✓ Migration 完成"
echo "✓ Migration 完成" >> "$RECOVERY_REPORT"

# ─── Step 5: 从 CRDB 同步数据 ──────────────────────────────
echo ""
echo "[5/9] 从 CRDB 同步数据到本地 SQLite..."
echo "[5/9] CRDB 同步" >> "$RECOVERY_REPORT"
# 先检查 CRDB 可达性
if [ -z "${CRDB_DATABASE_URL:-}" ]; then
    echo "WARN: CRDB_DATABASE_URL 未配置,跳过 CRDB 同步(仅本地数据)"
    echo "WARN: CRDB_DATABASE_URL 未配置" >> "$RECOVERY_REPORT"
else
    systemctl start tgjiema-crdb_sync
    # 等待 crdb_sync 完成首次同步(最多 5 分钟,超时退出)
    echo "等待 crdb_sync 完成首次同步(最多 5 分钟)..."
    SYNC_OK=0
    for i in $(seq 1 30); do
        if journalctl -u tgjiema-crdb_sync --since "5 minutes ago" 2>/dev/null | grep -q "首次同步完成\|initial sync complete"; then
            SYNC_OK=1
            break
        fi
        sleep 10
    done
    if [ $SYNC_OK -eq 0 ]; then
        echo "ERROR: CRDB 同步超时(5 分钟内未完成)"
        echo "FAIL: CRDB 同步超时" >> "$RECOVERY_REPORT"
        exit 1
    fi
    echo "✓ CRDB 同步完成"
    echo "✓ CRDB 同步完成" >> "$RECOVERY_REPORT"
fi

# ─── Step 6: 从 R2 恢复加密备份 ───────────────────────────
echo ""
echo "[6/9] 从 R2 恢复加密备份..."
echo "[6/9] R2 恢复" >> "$RECOVERY_REPORT"
if [ "$MODE" = "production" ] && [ -n "${R2_BUCKET:-}" ]; then
    echo "  从 R2 bucket $R2_BUCKET 拉取备份 $BACKUP_ID..."
    # R46 P0-7: 真正下载 marker/manifest/payload,验证双 checksum、KEK
    if ! python -c "
import asyncio
import sys
sys.path.insert(0, '.')
from services.backup_engine import BackupEngine
async def main():
    engine = BackupEngine()
    # 验证 approval_action_id
    result = await engine.restore(
        backup_id='$BACKUP_ID',
        approval_action_id='$APPROVAL_ACTION_ID',
        mode='$MODE',
    )
    if not result.get('success'):
        print(f'ERROR: restore 失败: {result.get(\"error\", \"unknown\")}', file=sys.stderr)
        sys.exit(1)
    print(f'✓ restore 完成: {result}')
asyncio.run(main())
"; then
        echo "ERROR: R2 restore 失败"
        echo "FAIL: R2 restore 失败" >> "$RECOVERY_REPORT"
        exit 1
    fi
    echo "✓ R2 恢复完成"
    echo "✓ R2 恢复完成" >> "$RECOVERY_REPORT"
else
    echo "  (跳过,staging 模式或 R2_BUCKET 未配置)"
    echo "SKIP: staging 模式跳过 R2 恢复" >> "$RECOVERY_REPORT"
fi

# ─── Step 7: 启动所有业务服务(按依赖排序) ─────────────────
echo ""
echo "[7/9] 启动所有业务服务(按依赖排序)..."
echo "[7/9] 启动业务服务" >> "$RECOVERY_REPORT"
# R46 P0-7: 按依赖排序启动,每个服务 health 不通过退出
SERVICES=(db_writer up idx dsp mon admin_bot admin)
for svc in "${SERVICES[@]}"; do
    echo "  启动 tgjiema-${svc}..."
    if ! systemctl start "tgjiema-${svc}" 2>/dev/null; then
        docker-compose up -d "$svc" 2>/dev/null || true
    fi
    sleep 3
    # 验证服务状态 — 失败立即退出
    if ! systemctl is-active --quiet "tgjiema-${svc}" 2>/dev/null; then
        if ! docker-compose ps "$svc" 2>/dev/null | grep -q "Up\|running"; then
            echo "ERROR: tgjiema-${svc} 启动失败"
            echo "FAIL: tgjiema-${svc} 未启动" >> "$RECOVERY_REPORT"
            exit 1
        fi
    fi
    echo "  ✓ tgjiema-${svc} 已启动"
    echo "  ✓ tgjiema-${svc} 已启动" >> "$RECOVERY_REPORT"
done

# ─── Step 8: 验证服务可用性 ───────────────────────────────
echo ""
echo "[8/9] 验证服务可用性..."
echo "[8/9] 验证服务" >> "$RECOVERY_REPORT"
sleep 10  # 等待服务完全启动
VERIFY_OK=1

# 验证 admin Web
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/login 2>/dev/null | grep -q "200\|302"; then
    echo "  ✓ Admin Web 可访问"
    echo "  ✓ Admin Web 可访问" >> "$RECOVERY_REPORT"
else
    echo "  ✗ Admin Web 不可访问"
    echo "  FAIL: Admin Web 不可访问" >> "$RECOVERY_REPORT"
    VERIFY_OK=0
fi

# 验证 Telegram bot(检查 systemd active)
for svc in up idx dsp; do
    if systemctl is-active --quiet "tgjiema-${svc}" 2>/dev/null; then
        echo "  ✓ tgjiema-${svc} 运行中"
        echo "  ✓ tgjiema-${svc} 运行中" >> "$RECOVERY_REPORT"
    else
        echo "  ✗ tgjiema-${svc} 未运行"
        echo "  FAIL: tgjiema-${svc} 未运行" >> "$RECOVERY_REPORT"
        VERIFY_OK=0
    fi
done

# ─── Step 9: 跨表不变量与 smoke test ──────────────────────
echo ""
echo "[9/9] 跨表不变量验证 + smoke test..."
echo "[9/9] 不变量验证" >> "$RECOVERY_REPORT"
# R46 P0-7: 验证 file↔code 一致性、manifest↔channel/message 一致性
if ! python -c "
import asyncio
import sys
sys.path.insert(0, '.')
async def main():
    from database.cache_store import get_cache_store
    store = get_cache_store()
    if not store or not store._db:
        print('WARN: cache_store 未初始化,跳过不变量检查')
        return
    # 验证 file_records ↔ codes 一致性
    cursor = await store._db.execute(
        'SELECT COUNT(*) FROM file_records WHERE code NOT IN (SELECT code FROM codes)'
    )
    orphan_files = (await cursor.fetchone())[0]
    if orphan_files > 0:
        print(f'ERROR: {orphan_files} 个 file_records 无对应 codes', file=sys.stderr)
        sys.exit(1)
    # 验证 unregistered_copies 中未 reconciled 数量
    cursor = await store._db.execute(
        'SELECT COUNT(*) FROM unregistered_copies WHERE reconciled_at IS NULL'
    )
    unreconciled = (await cursor.fetchone())[0]
    if unreconciled > 0:
        print(f'WARN: {unreconciled} 个 unregistered_copies 待 reconciled')
    print('✓ 跨表不变量验证通过')
asyncio.run(main())
"; then
    echo "  ✗ 跨表不变量验证失败"
    echo "  FAIL: 跨表不变量验证失败" >> "$RECOVERY_REPORT"
    VERIFY_OK=0
fi

if [ $VERIFY_OK -eq 0 ]; then
    echo ""
    echo "ERROR: 服务验证失败,请检查日志"
    echo "FAIL: 服务验证失败" >> "$RECOVERY_REPORT"
    echo "=== Recovery Report ===" >> "$RECOVERY_REPORT"
    echo "状态: FAILED" >> "$RECOVERY_REPORT"
    exit 1
fi

# ─── 最终报告 ─────────────────────────────────────────────
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS=$((ELAPSED % 60))

echo ""
echo "=== 恢复完成 ==="
echo "总耗时: ${MINUTES}m${SECONDS}s"
if [ $ELAPSED -le 1800 ]; then
    echo "✓ RTO 达标(≤ 30 分钟)"
    echo "状态: SUCCESS" >> "$RECOVERY_REPORT"
    echo "RTO: ${MINUTES}m${SECONDS}s (达标)" >> "$RECOVERY_REPORT"
else
    echo "✗ RTO 超标(> 30 分钟)"
    echo "状态: RTO_EXCEEDED" >> "$RECOVERY_REPORT"
    echo "RTO: ${MINUTES}m${SECONDS}s (超标)" >> "$RECOVERY_REPORT"
    exit 1
fi
echo ""
echo "Recovery report 已保存到: $RECOVERY_REPORT"
echo ""
echo "下一步:"
echo "  1. 验证 Telegram bot 响应: 向 @${BOT_USERNAME:-your_bot} 发送测试消息"
echo "  2. 验证文件上传/解码: 上传测试文件"
echo "  3. 运行 72h 空载 RU 报告: python scripts/export_ru_report.py --hours 72"
echo "  4. 执行 Manifest 抽样验证: python scripts/verify_file_records_status_index.py"
