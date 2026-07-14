#!/usr/bin/env bash
# R47 P0-7: 全新机恢复脚本(production fail-closed)
# 在新机器上从 CRDB 托管备份 + 加密 R2 bundle + SQLite/Relay session 恢复完整服务
#
# R47 P0-7 整改(对照 R47 终审报告):
#   1. production 强制 R2_BUCKET/BACKUP_ID/APPROVAL_ACTION_ID/KEK 非空,缺一即 fail-closed
#   2. CacheStore 未初始化必须 exit 1(原 WARN 后 return 修复)
#   3. unreconciled_copies 超阈值必须 exit 1(原 warning 修复)
#   4. Manifest 抽样验证真实执行(原仅框架)— 抽样 file↔code↔msg_id,验证 Telegram 消息存在
#   5. Telegram smoke test 真实执行(原仅框架)— sendMessage + 读取 + deleteMessage
#   6. recovery_report.json 必须签名(SSH 优先,GPG 兜底),未签名即失败
#   7. 删除所有 `|| true` 兜底(line 193 残留必须删除),任何命令失败必须真实退出
#
# 使用方法:
#   ./scripts/full_machine_recovery.sh --commit-sha <SHA> --backup-id <ID> \
#       --approval-action-id <UUID> --mode production \
#       --sample-size 10 --test-chat-id <CHAT_ID> --unreconciled-threshold 0
set -euo pipefail

# ─── 用法说明 ─────────────────────────────────────────────
usage() {
    cat <<'USAGE'
R47 P0-7 全新机恢复脚本(production fail-closed)

用法:
  full_machine_recovery.sh --commit-sha <SHA> [选项]

必填参数:
  --commit-sha SHA              checkout 的固定 git commit SHA

可选参数:
  --backup-id ID                R2 backup ID(production 必填)
  --approval-action-id UUID     审批 action ID(production 必填)
  --mode MODE                   production/staging(默认)/development
  --sample-size N               Manifest 抽样数量(默认 10,production 推荐 ≥10)
  --test-chat-id CHAT_ID        Telegram smoke test chat ID(production 必填)
  --unreconciled-threshold N    unreconciled_copies 阈值(默认 0,超过即失败)
  -h, --help                    显示本帮助

production mode 严格 fail-closed:
  - R2_BUCKET/BACKUP_ID/APPROVAL_ACTION_ID/KEK 必须非空
  - --test-chat-id 必填
  - CacheStore 必须初始化
  - unreconciled_copies <= 阈值
  - Manifest 抽样全部通过
  - Telegram smoke test 全部通过
  - recovery_report.json 必须签名

报告输出:
  recovery_report_YYYYMMDD_HHMMSS.json
  recovery_report_YYYYMMDD_HHMMSS.json.sig (SSH 签名,或 GPG .asc)
USAGE
}

# ─── 参数解析 ─────────────────────────────────────────────
COMMIT_SHA=""
BACKUP_ID=""
APPROVAL_ACTION_ID=""
MODE="staging"
SAMPLE_SIZE=10
TEST_CHAT_ID=""
UNRECONCILED_THRESHOLD=0

# 早期 --help 检查(在 trap 设置之前,避免触发 report 生成)
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage; exit 0
fi

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
        --sample-size)
            SAMPLE_SIZE="$2"; shift 2 ;;
        --test-chat-id)
            TEST_CHAT_ID="$2"; shift 2 ;;
        --unreconciled-threshold)
            UNRECONCILED_THRESHOLD="$2"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "ERROR: 未知参数 $1"; usage; exit 1 ;;
    esac
done

# ─── 基础参数校验(早期失败,不触发 report) ─────────────
if [[ -z "$COMMIT_SHA" ]]; then
    echo "ERROR: 必须指定 --commit-sha <SHA>"
    exit 1
fi
case "$MODE" in
    production|staging|development) ;;
    *) echo "ERROR: --mode 必须为 production/staging/development"; exit 1 ;;
esac
if ! [[ "$SAMPLE_SIZE" =~ ^[0-9]+$ ]] || [[ "$SAMPLE_SIZE" -lt 1 ]]; then
    echo "ERROR: --sample-size 必须为正整数"
    exit 1
fi
if ! [[ "$UNRECONCILED_THRESHOLD" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --unreconciled-threshold 必须为非负整数"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

START_TIME=$(date +%s)
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
REPORT_TS=$(date +%Y%m%d_%H%M%S)
RECOVERY_REPORT_JSON="$REPO_DIR/recovery_report_${REPORT_TS}.json"
RECOVERY_REPORT_SIG="$RECOVERY_REPORT_JSON.sig"

# 临时文件:checks 状态 + 抽样结果
CHECKS_FILE="$(mktemp)"
SAMPLE_RESULTS_FILE="$(mktemp)"
UNRECONCILED_COUNT=0

# ─── 工具函数 ─────────────────────────────────────────────
# 设置某个步骤的 check 状态(PASS/FAIL/SKIP/PENDING)
set_check() {
    local name=$1 status=$2
    # 移除同名旧记录(避免重复)
    # 用 awk 替代 grep -v:awk 不会因"没找到匹配"返回非零(grep 会返回 1),
    # 避免 set -e 误退出,也无需 `|| true` 兜底
    if [[ -f "$CHECKS_FILE" ]]; then
        local tmp
        tmp="$(mktemp)"
        awk -v pat="^${name} " '$0 !~ pat' "$CHECKS_FILE" > "$tmp" 2>/dev/null
        mv "$tmp" "$CHECKS_FILE"
    fi
    echo "${name} ${status}" >> "$CHECKS_FILE"
}

# 失败处理:更新 checks + 退出(trap 会生成 report)
fail_step() {
    local step=$1 msg=$2
    echo "ERROR: $msg"
    set_check "$step" "FAIL"
    exit 1
}

# 写 recovery report JSON + 签名(由 trap EXIT 调用)
write_recovery_report() {
    local status=$1
    local end_ts duration completed_at
    end_ts=$(date +%s)
    duration=$((end_ts - START_TIME))
    completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # 调用 Python 组装 JSON(通过环境变量传参,避免 shell 转义问题)
    if ! RECOVERY_REPORT_JSON="$RECOVERY_REPORT_JSON" \
         CHECKS_FILE="$CHECKS_FILE" \
         SAMPLE_RESULTS_FILE="$SAMPLE_RESULTS_FILE" \
         COMMIT_SHA="$COMMIT_SHA" \
         BACKUP_ID="${BACKUP_ID:-}" \
         APPROVAL_ACTION_ID="${APPROVAL_ACTION_ID:-}" \
         MODE="$MODE" \
         STARTED_AT="$STARTED_AT" \
         COMPLETED_AT="$completed_at" \
         DURATION="$duration" \
         STATUS="$status" \
         UNRECONCILED_COUNT="$UNRECONCILED_COUNT" \
         python3 <<'PYEOF'
import json, os
# 读取 checks 状态
checks = {}
try:
    with open(os.environ['CHECKS_FILE']) as f:
        for line in f:
            parts = line.strip().split(' ', 1)
            if len(parts) == 2:
                checks[parts[0]] = parts[1]
except Exception:
    pass
# 读取抽样结果
sample = []
try:
    with open(os.environ['SAMPLE_RESULTS_FILE']) as f:
        sample = json.load(f)
except Exception:
    sample = []
data = {
    'commit_sha': os.environ['COMMIT_SHA'],
    'backup_id': os.environ['BACKUP_ID'],
    'approval_action_id': os.environ['APPROVAL_ACTION_ID'],
    'mode': os.environ['MODE'],
    'started_at': os.environ['STARTED_AT'],
    'completed_at': os.environ['COMPLETED_AT'],
    'duration_seconds': int(os.environ['DURATION']),
    'status': os.environ['STATUS'],
    'checks': checks,
    'unreconciled_count': int(os.environ['UNRECONCILED_COUNT']),
    'sample_results': sample,
    'signed': False,  # 签名后不重写 JSON,.sig 文件存在性即签名证据
}
with open(os.environ['RECOVERY_REPORT_JSON'], 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
PYEOF
    then
        echo "ERROR: 写 recovery report JSON 失败"
        rm -f "$CHECKS_FILE" "$SAMPLE_RESULTS_FILE" 2>/dev/null
        exit 1
    fi

    if [[ ! -f "$RECOVERY_REPORT_JSON" ]]; then
        echo "ERROR: recovery report JSON 文件未生成"
        rm -f "$CHECKS_FILE" "$SAMPLE_RESULTS_FILE" 2>/dev/null
        exit 1
    fi

    # 签名(优先 SSH: ssh-keygen -Y sign,失败用 GPG --detach-sign)
    # SSH key 路径:优先 SSH_KEY_PATH 环境变量,其次 ~/.ssh/id_ed25519
    local sign_ok=0 sign_method=""
    local ssh_key=""
    if [[ -n "${SSH_KEY_PATH:-}" && -f "$SSH_KEY_PATH" ]]; then
        ssh_key="$SSH_KEY_PATH"
    elif [[ -f "$HOME/.ssh/id_ed25519" ]]; then
        ssh_key="$HOME/.ssh/id_ed25519"
    fi
    if [[ -n "$ssh_key" ]]; then
        # ssh-keygen -Y sign 输出 <file>.sig(覆盖默认)
        if ssh-keygen -Y sign -f "$ssh_key" -n recovery-report "$RECOVERY_REPORT_JSON" 2>/dev/null; then
            sign_ok=1
            sign_method="ssh"
        fi
    fi
    if [[ $sign_ok -eq 0 ]] && command -v gpg >/dev/null 2>&1; then
        if gpg --detach-sign --armor --output "$RECOVERY_REPORT_SIG" "$RECOVERY_REPORT_JSON" 2>/dev/null; then
            sign_ok=1
            sign_method="gpg"
        fi
    fi

    echo ""
    echo "=== Recovery Report ==="
    echo "报告: $RECOVERY_REPORT_JSON"
    if [[ $sign_ok -eq 1 ]]; then
        echo "签名: $RECOVERY_REPORT_SIG (method=$sign_method)"
        echo "状态: $status"
        echo "耗时: ${duration}s"
    else
        echo "签名: ✗ 失败(SSH key 和 GPG 均不可用或签名失败)"
        echo "状态: $status"
        echo "耗时: ${duration}s"
        # R47 P0-7: report 必须签名,未签名即失败(fail-closed)
        # 清理临时文件后退出
        rm -f "$CHECKS_FILE" "$SAMPLE_RESULTS_FILE" 2>/dev/null
        echo "ERROR: recovery report 签名失败,拒绝完成"
        exit 1
    fi
}

# trap EXIT:无论脚本以何种方式退出,都生成签名 report
on_exit() {
    local exit_code=$?
    # trap 内禁用 set -e,确保清理操作(rm)失败不影响最终退出码
    set +e
    local status="SUCCESS"
    if [[ $exit_code -ne 0 ]]; then
        status="FAILED"
    fi
    write_recovery_report "$status"
    rm -f "$CHECKS_FILE" "$SAMPLE_RESULTS_FILE" 2>/dev/null
}
trap on_exit EXIT

# ─── 启动横幅 ─────────────────────────────────────────────
echo "=== R47 P0-7: 全新机恢复开始 ==="
echo "时间: $(date)"
echo "模式: $MODE"
echo "Commit SHA: $COMMIT_SHA"
echo "Backup ID: ${BACKUP_ID:-N/A}"
echo "Approval Action ID: ${APPROVAL_ACTION_ID:-N/A}"
echo "Sample Size: $SAMPLE_SIZE"
echo "Test Chat ID: ${TEST_CHAT_ID:-N/A}"
echo "Unreconciled Threshold: $UNRECONCILED_THRESHOLD"
echo "目标 RTO: ≤ 30 分钟"
echo ""

# ─── Step 1: checkout 固定 SHA ─────────────────────────────
echo "[1/13] Checkout 固定 SHA ($COMMIT_SHA)..."
git fetch origin
if ! git checkout "$COMMIT_SHA"; then
    fail_step "checkout" "git checkout $COMMIT_SHA 失败"
fi
echo "✓ 代码已 checkout 到 $(git rev-parse --short HEAD)"
set_check "checkout" "PASS"

# ─── Step 2: 加载环境变量 ──────────────────────────────────
echo ""
echo "[2/13] 加载环境变量..."
if [[ ! -f .env.shared ]]; then
    fail_step "env_load" ".env.shared 不存在,请从安全渠道传输"
fi
# set -a 让 .env.shared 中所有变量自动 export,确保子进程可见
set -a
# shellcheck disable=SC1091
source .env.shared
set +a
echo "✓ .env.shared 已加载"

# ─── production 模式环境变量强制检查(R47 P0-7 整改 #1) ────
# production 必须强制 R2_BUCKET/BACKUP_ID/APPROVAL_ACTION_ID/KEK 非空
# 缺一即 fail-closed(不再进入 skip 分支)
if [[ "$MODE" == "production" ]]; then
    for var in R2_BUCKET BACKUP_ID APPROVAL_ACTION_ID KEK; do
        if [[ -z "${!var:-}" ]]; then
            fail_step "env_load" "production mode requires $var to be set"
        fi
    done
    # production 还必须指定 --test-chat-id(用于 smoke test)
    if [[ -z "$TEST_CHAT_ID" ]]; then
        fail_step "env_load" "production mode requires --test-chat-id"
    fi
    # UPLOAD_BOT_TOKEN 用于 smoke test + Manifest 抽样的 Telegram 验证
    if [[ -z "${UPLOAD_BOT_TOKEN:-}" ]]; then
        fail_step "env_load" "production mode requires UPLOAD_BOT_TOKEN (in .env.shared)"
    fi
fi
set_check "env_load" "PASS"

# ─── Step 3: 启动 Redis ───────────────────────────────────
echo ""
echo "[3/13] 启动 Redis..."
if ! docker-compose up -d redis; then
    fail_step "redis" "docker-compose up redis 失败"
fi
sleep 5
# 验证 Redis 可用 — 失败立即退出
if ! docker-compose exec -T redis redis-cli ping | grep -q PONG; then
    fail_step "redis" "Redis 启动失败/不可用"
fi
echo "✓ Redis 已启动"
set_check "redis" "PASS"

# ─── Step 4: 运行 migration ───────────────────────────────
echo ""
echo "[4/13] 运行 database migration..."
# migration 是 oneshot 服务
if ! systemctl start tgjiema-migration 2>/dev/null; then
    if ! docker-compose run --rm migration; then
        fail_step "migration" "Migration 失败(systemctl 和 docker-compose 均失败)"
    fi
fi
echo "✓ Migration 完成"
set_check "migration" "PASS"

# ─── Step 5: 从 CRDB 同步数据 ──────────────────────────────
echo ""
echo "[5/13] 从 CRDB 同步数据到本地 SQLite..."
if [[ -z "${CRDB_DATABASE_URL:-}" ]]; then
    echo "  (CRDB_DATABASE_URL 未配置,跳过 CRDB 同步,仅用本地数据)"
    set_check "crdb_sync" "SKIP"
else
    if ! systemctl start tgjiema-crdb_sync; then
        fail_step "crdb_sync" "systemctl start tgjiema-crdb_sync 失败"
    fi
    # 等待 crdb_sync 完成首次同步(最多 5 分钟,超时退出)
    echo "  等待 crdb_sync 完成首次同步(最多 5 分钟)..."
    SYNC_OK=0
    for i in $(seq 1 30); do
        if journalctl -u tgjiema-crdb_sync --since "5 minutes ago" 2>/dev/null | grep -q "首次同步完成\|initial sync complete"; then
            SYNC_OK=1
            break
        fi
        sleep 10
    done
    if [[ $SYNC_OK -eq 0 ]]; then
        fail_step "crdb_sync" "CRDB 同步超时(5 分钟内未完成)"
    fi
    echo "✓ CRDB 同步完成"
    set_check "crdb_sync" "PASS"
fi

# ─── Step 6: 从 R2 恢复加密备份 ───────────────────────────
echo ""
echo "[6/13] 从 R2 恢复加密备份..."
# R47 P0-7 整改:production 必须做 R2 恢复(已强制校验 R2_BUCKET/BACKUP_ID/KEK),
# 不再进入 skip 分支;staging/development 可选(配置齐全则执行)
if [[ "$MODE" == "production" ]]; then
    echo "  从 R2 bucket $R2_BUCKET 拉取备份 $BACKUP_ID..."
    if ! python -c "
import asyncio
import sys
sys.path.insert(0, '.')
from services.backup_engine import BackupEngine
async def main():
    engine = BackupEngine()
    # 验证 approval_action_id + KEK + 双 checksum
    result = await engine.restore(
        backup_id='$BACKUP_ID',
        approval_action_id='$APPROVAL_ACTION_ID',
        target='$MODE',
    )
    if not result.get('success'):
        print(f'ERROR: restore 失败: {result.get(\"error\", \"unknown\")}', file=sys.stderr)
        sys.exit(1)
    print(f'✓ restore 完成: {result}')
asyncio.run(main())
"; then
        fail_step "r2_restore" "R2 restore 失败"
    fi
    echo "✓ R2 恢复完成"
    set_check "r2_restore" "PASS"
elif [[ -n "${R2_BUCKET:-}" && -n "$BACKUP_ID" ]]; then
    # staging/development 但配置齐全,执行 R2 恢复
    echo "  (staging/development 模式但 R2_BUCKET 已配置,执行 R2 恢复)"
    if ! python -c "
import asyncio
import sys
sys.path.insert(0, '.')
from services.backup_engine import BackupEngine
async def main():
    engine = BackupEngine()
    result = await engine.restore(
        backup_id='$BACKUP_ID',
        approval_action_id='${APPROVAL_ACTION_ID:-}',
        target='$MODE',
    )
    if not result.get('success'):
        print(f'ERROR: restore 失败: {result.get(\"error\", \"unknown\")}', file=sys.stderr)
        sys.exit(1)
asyncio.run(main())
"; then
        fail_step "r2_restore" "R2 restore 失败"
    fi
    echo "✓ R2 恢复完成"
    set_check "r2_restore" "PASS"
else
    echo "  (staging/development 模式,R2_BUCKET/BACKUP_ID 未配置,跳过 R2 恢复)"
    set_check "r2_restore" "SKIP"
fi

# ─── Step 7: 启动所有业务服务(按依赖排序) ─────────────────
echo ""
echo "[7/13] 启动所有业务服务(按依赖排序)..."
# R46 P0-7: 按依赖排序启动,每个服务 health 不通过退出
# R47 P0-7 整改 #7: 删除 line 193 残留的 `|| true`,systemctl 和 docker-compose 均失败则直接 exit 1
SERVICES=(db_writer up idx dsp mon admin_bot admin)
for svc in "${SERVICES[@]}"; do
    echo "  启动 tgjiema-${svc}..."
    if ! systemctl start "tgjiema-${svc}" 2>/dev/null; then
        if ! docker-compose up -d "$svc" 2>/dev/null; then
            fail_step "services_start" "tgjiema-${svc} 启动失败(systemctl 和 docker-compose 均失败)"
        fi
    fi
    sleep 3
    # 验证服务状态(冗余检查)
    if ! systemctl is-active --quiet "tgjiema-${svc}" 2>/dev/null; then
        if ! docker-compose ps "$svc" 2>/dev/null | grep -q "Up\|running"; then
            fail_step "services_start" "tgjiema-${svc} 启动后状态检查失败"
        fi
    fi
    echo "  ✓ tgjiema-${svc} 已启动"
done
set_check "services_start" "PASS"

# ─── Step 8: 验证服务可用性 ───────────────────────────────
echo ""
echo "[8/13] 验证服务可用性..."
sleep 10  # 等待服务完全启动
VERIFY_OK=1

# 验证 admin Web
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/login 2>/dev/null | grep -q "200\|302"; then
    echo "  ✓ Admin Web 可访问"
else
    echo "  ✗ Admin Web 不可访问"
    VERIFY_OK=0
fi

# 验证 Telegram bot(检查 systemd active)
for svc in up idx dsp; do
    if systemctl is-active --quiet "tgjiema-${svc}" 2>/dev/null; then
        echo "  ✓ tgjiema-${svc} 运行中"
    else
        echo "  ✗ tgjiema-${svc} 未运行"
        VERIFY_OK=0
    fi
done

if [[ $VERIFY_OK -eq 0 ]]; then
    fail_step "service_verify" "服务验证失败,请检查日志"
fi
set_check "service_verify" "PASS"

# ─── Step 9: CacheStore 初始化检查(R47 P0-7 整改 #2) ────
echo ""
echo "[9/13] 验证 CacheStore 已初始化..."
# R47 P0-7: CacheStore 未初始化必须 exit 1(原 WARN 后 return 修复)
# 在脚本进程内调用 init_db() 初始化 CacheStore,然后检查 _db 非空
if ! python -c "
import asyncio, sys
sys.path.insert(0, '.')
async def main():
    try:
        from database import init_db
        await init_db()
    except Exception as e:
        print(f'ERROR: init_db 失败: {e}', file=sys.stderr)
        sys.exit(1)
    from database.cache_store import get_cache_store
    store = get_cache_store()
    if store is None:
        print('ERROR: get_cache_store() returned None', file=sys.stderr)
        sys.exit(1)
    if getattr(store, '_db', None) is None:
        print('ERROR: CacheStore._db is None (未初始化)', file=sys.stderr)
        sys.exit(1)
asyncio.run(main())
"; then
    fail_step "cache_store_init" "CacheStore 未初始化(_db 为 None)"
fi
echo "✓ CacheStore 已初始化"
set_check "cache_store_init" "PASS"

# ─── Step 10: 跨表不变量验证 ──────────────────────────────
echo ""
echo "[10/13] 跨表不变量验证..."
# R46 P0-7: 验证 file↔code 一致性 — 失败必须 exit 1(不再仅 warning)
if ! python -c "
import asyncio, sys
sys.path.insert(0, '.')
async def main():
    from database import init_db
    await init_db()
    from database.cache_store import get_cache_store
    store = get_cache_store()
    if not store or not store._db:
        print('ERROR: cache_store 未初始化', file=sys.stderr)
        sys.exit(1)
    # 验证 file_records ↔ codes 一致性
    cursor = await store._db.execute(
        'SELECT COUNT(*) FROM file_records_local WHERE file_code NOT IN (SELECT code FROM codes_local)'
    )
    orphan_files = (await cursor.fetchone())[0]
    if orphan_files > 0:
        print(f'ERROR: {orphan_files} 个 file_records 无对应 codes', file=sys.stderr)
        sys.exit(1)
    print('✓ 跨表不变量验证通过')
asyncio.run(main())
"; then
    fail_step "invariants" "跨表不变量验证失败(file_records ↔ codes 不一致)"
fi
set_check "invariants" "PASS"

# ─── Step 11: unreconciled_copies 阈值检查(R47 P0-7 整改 #3) ─
echo ""
echo "[11/13] unreconciled_copies 阈值检查(阈值=$UNRECONCILED_THRESHOLD)..."
# R47 P0-7: 调用 list_unreconciled_copies() 获取数量,超阈值直接 exit 1(不再仅 warning)
UNRECONCILED_COUNT=$(python -c "
import asyncio, sys
sys.path.insert(0, '.')
async def main():
    from database import init_db
    await init_db()
    from database.cache_store import get_cache_store
    store = get_cache_store()
    if not store:
        print('ERROR: cache_store 未初始化', file=sys.stderr)
        sys.exit(1)
    # 调用 list_unreconciled_copies() 获取数量(limit 给一个大值以覆盖实际)
    copies = await store.list_unreconciled_copies(limit=100000)
    print(len(copies))
asyncio.run(main())
") || fail_step "unreconciled_check" "list_unreconciled_copies() 调用失败"
echo "  当前 unreconciled_copies: $UNRECONCILED_COUNT"
if [[ "$UNRECONCILED_COUNT" -gt "$UNRECONCILED_THRESHOLD" ]]; then
    fail_step "unreconciled_check" "unreconciled_copies $UNRECONCILED_COUNT > 阈值 $UNRECONCILED_THRESHOLD"
fi
set_check "unreconciled_check" "PASS"

# ─── Step 12: Manifest 抽样验证(R47 P0-7 整改 #4) ────────
echo ""
echo "[12/13] Manifest 抽样验证(抽样 $SAMPLE_SIZE 条)..."
# R47 P0-7: 真实执行抽样验证(原仅框架)
#   1. 抽样 N 条 file↔code↔msg_id 记录
#   2. 验证 file_records_local 中存在
#   3. 验证 codes_local 中存在对应 file_code
#   4. 验证 Telegram storage channel 中实际消息存在(用 PTB copy_message 验证,然后删除 copy)
# 任一失败立即 exit 1
# 抽样数量由 --sample-size 控制(默认 10)
if ! SAMPLE_SIZE_VAL="$SAMPLE_SIZE" \
     TEST_CHAT_ID_VAL="$TEST_CHAT_ID" \
     UPLOAD_BOT_TOKEN_VAL="${UPLOAD_BOT_TOKEN:-}" \
     OUTPUT_FILE_VAL="$SAMPLE_RESULTS_FILE" \
     MODE_VAL="$MODE" \
     python3 <<'PYEOF'
import asyncio, json, os, sys
sys.path.insert(0, '.')

async def main():
    from database import init_db
    await init_db()
    from database.cache_store import get_cache_store
    store = get_cache_store()
    if not store or not store._db:
        print("ERROR: cache_store 未初始化", file=sys.stderr)
        sys.exit(1)

    sample_size = int(os.environ.get('SAMPLE_SIZE_VAL', '10'))
    test_chat_id = os.environ.get('TEST_CHAT_ID_VAL', '').strip()
    bot_token = os.environ.get('UPLOAD_BOT_TOKEN_VAL', '').strip()
    output_file = os.environ.get('OUTPUT_FILE_VAL')
    mode = os.environ.get('MODE_VAL', 'staging')

    # 抽样 file_records_local(随机抽取 N 条有 storage channel/msg_id 的记录)
    cursor = await store._db.execute(
        "SELECT file_code, primary_channel_id, primary_channel_msg_id "
        "FROM file_records_local "
        "WHERE primary_channel_id > 0 AND primary_channel_msg_id > 0 "
        "AND (deleted_at IS NULL OR deleted_at = '') "
        "ORDER BY RANDOM() LIMIT ?",
        (sample_size,)
    )
    rows = await cursor.fetchall()

    if not rows:
        print("  WARN: file_records_local 中无可用抽样记录(库可能为空)")
    else:
        print(f"  抽样到 {len(rows)} 条记录")

    # 决定是否做 Telegram 验证
    do_telegram_verify = bool(bot_token and test_chat_id)
    if not do_telegram_verify:
        if mode == "production":
            print("ERROR: production mode 需要 UPLOAD_BOT_TOKEN 和 TEST_CHAT_ID 做 Telegram 验证", file=sys.stderr)
            sys.exit(1)
        else:
            print("  (UPLOAD_BOT_TOKEN/TEST_CHAT_ID 未配置,跳过 Telegram 验证,仅做 DB 一致性检查)")

    # 初始化 PTB Bot(用于 Telegram 消息存在性验证)
    bot = None
    if do_telegram_verify:
        try:
            from telegram import Bot
            bot = Bot(bot_token)
        except Exception as e:
            print(f"ERROR: PTB Bot 初始化失败: {e}", file=sys.stderr)
            sys.exit(1)

    results = []
    fail_count = 0
    for r in rows:
        file_code, channel_id, msg_id = r[0], r[1], r[2]
        item = {
            "file_code": file_code,
            "channel_id": channel_id,
            "msg_id": msg_id,
            "exists_in_db": True,
            "exists_in_codes_local": False,
            "exists_in_telegram": None,
            "pass": False,
        }
        # 1. 验证 codes_local 中存在 file_code
        try:
            cc = await store._db.execute(
                "SELECT 1 FROM codes_local WHERE code = ? LIMIT 1",
                (file_code,)
            )
            cr = await cc.fetchone()
            item["exists_in_codes_local"] = (cr is not None)
        except Exception as e:
            item["codes_local_error"] = str(e)
        # 2. 验证 Telegram storage channel 中实际消息存在
        #    用 copy_message 复制到 test chat(不修改原消息),验证后立即删除 copy
        if bot is not None:
            try:
                copied = await bot.copy_message(
                    chat_id=int(test_chat_id),
                    from_chat_id=channel_id,
                    message_id=msg_id,
                )
                item["exists_in_telegram"] = True
                # 立即删除 copy(避免污染 test chat)
                try:
                    await bot.delete_message(chat_id=int(test_chat_id), message_id=copied.message_id)
                except Exception as del_err:
                    print(f"  WARN: 删除 copy 失败(code={file_code}, msg_id={copied.message_id}): {del_err}")
            except Exception as e:
                item["exists_in_telegram"] = False
                item["telegram_error"] = str(e)
        # 判定 pass
        ok = item["exists_in_db"] and item["exists_in_codes_local"]
        if bot is not None:
            ok = ok and item["exists_in_telegram"]
        item["pass"] = ok
        if not ok:
            fail_count += 1
        results.append(item)

    # 写入结果文件(供 recovery report 引用)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"  抽样验证结果: {len(results)} 条,{fail_count} 条失败")
    if fail_count > 0:
        print(f"ERROR: Manifest 抽样验证失败({fail_count}/{len(results)} 条不通过)", file=sys.stderr)
        sys.exit(1)
    print("✓ Manifest 抽样验证通过")

asyncio.run(main())
PYEOF
then
    fail_step "manifest_sample" "Manifest 抽样验证失败"
fi
set_check "manifest_sample" "PASS"

# ─── Step 13: Telegram smoke test(R47 P0-7 整改 #5) ──────
echo ""
echo "[13/13] Telegram smoke test..."
# R47 P0-7: 真实执行 smoke test(原仅框架)
#   1. sendMessage 发送测试消息到 test chat(--test-chat-id)
#   2. 验证返回 message_id
#   3. copyMessage 验证消息可读(从 test chat 复制到自己)
#   4. deleteMessage 删除原消息
#   5. deleteMessage 删除 copy(清理)
# 任一步骤失败立即 exit 1
if [[ -z "${UPLOAD_BOT_TOKEN:-}" || -z "$TEST_CHAT_ID" ]]; then
    if [[ "$MODE" == "production" ]]; then
        # production 已经在 Step 2 强制校验,不应到这里
        fail_step "telegram_smoke" "production mode: UPLOAD_BOT_TOKEN/TEST_CHAT_ID 缺失(应已在 Step 2 校验)"
    fi
    echo "  (UPLOAD_BOT_TOKEN/TEST_CHAT_ID 未配置,跳过 Telegram smoke test)"
    set_check "telegram_smoke" "SKIP"
else
    TG_API="https://api.telegram.org/bot${UPLOAD_BOT_TOKEN}"
    SMOKE_TEXT="recovery_smoke_test_$(date +%s)_$$"

    echo "  [1/4] sendMessage 到 chat_id=$TEST_CHAT_ID..."
    # -f 让 HTTP 4xx/5xx 返回非零,-sS 显示错误,-m 30 超时 30 秒
    SEND_RESP=$(curl -fsS -m 30 "$TG_API/sendMessage" \
        -d "chat_id=$TEST_CHAT_ID" \
        -d "text=$SMOKE_TEXT") || fail_step "telegram_smoke" "sendMessage 失败(curl 错误或 HTTP 非 2xx)"

    MSG_ID=$(echo "$SEND_RESP" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    if r.get('ok') and r.get('result', {}).get('message_id'):
        print(r['result']['message_id'])
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
") || fail_step "telegram_smoke" "sendMessage 响应解析失败/未返回 message_id"
    echo "  ✓ 消息已发送(message_id=$MSG_ID)"

    echo "  [2/4] 验证消息可读(copyMessage)..."
    COPY_RESP=$(curl -fsS -m 30 "$TG_API/copyMessage" \
        -d "chat_id=$TEST_CHAT_ID" \
        -d "from_chat_id=$TEST_CHAT_ID" \
        -d "message_id=$MSG_ID") || fail_step "telegram_smoke" "copyMessage 失败(消息不可读)"

    COPY_ID=$(echo "$COPY_RESP" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    if r.get('ok') and r.get('result', {}).get('message_id'):
        print(r['result']['message_id'])
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
") || fail_step "telegram_smoke" "copyMessage 响应解析失败"
    echo "  ✓ 消息可读(copy_id=$COPY_ID)"

    echo "  [3/4] 删除原消息(deleteMessage)..."
    curl -fsS -m 30 "$TG_API/deleteMessage" \
        -d "chat_id=$TEST_CHAT_ID" \
        -d "message_id=$MSG_ID" > /dev/null || fail_step "telegram_smoke" "deleteMessage(原消息)失败"
    echo "  ✓ 原消息已删除"

    echo "  [4/4] 删除 copy(清理)..."
    curl -fsS -m 30 "$TG_API/deleteMessage" \
        -d "chat_id=$TEST_CHAT_ID" \
        -d "message_id=$COPY_ID" > /dev/null || fail_step "telegram_smoke" "deleteMessage(copy)失败"
    echo "  ✓ copy 已删除"

    echo "✓ Telegram smoke test 全部通过"
    set_check "telegram_smoke" "PASS"
fi

# ─── 最终统计 ─────────────────────────────────────────────
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS_REM=$((ELAPSED % 60))

echo ""
echo "=== 恢复完成 ==="
echo "总耗时: ${MINUTES}m${SECONDS_REM}s"
if [[ $ELAPSED -le 1800 ]]; then
    echo "✓ RTO 达标(≤ 30 分钟)"
else
    echo "✗ RTO 超标(> 30 分钟)"
    # RTO 超标视为失败,trap 会写 report
    fail_step "rto" "RTO 超标(${MINUTES}m${SECONDS_REM}s > 30m)"
fi

echo ""
echo "下一步:"
echo "  1. 验证 Telegram bot 响应: 向 @${BOT_USERNAME:-your_bot} 发送测试消息"
echo "  2. 验证文件上传/解码: 上传测试文件"
echo "  3. 运行 72h 空载 RU 报告: python scripts/export_ru_report.py --hours 72"
echo "  4. 第二人复核 recovery_report_*.json + .sig 签名(ssh-keygen -Y verify 或 gpg --verify)"
