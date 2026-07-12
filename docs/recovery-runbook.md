# tgjiema 故障恢复 Runbook(单一事实源)

> 最后更新: 2026-07-12 | 适用部署:VPS + systemd + Redis + CRDB Cloud + R2

本文档提供 tgjiema 各组件故障的诊断与恢复步骤。所有命令默认以 root 执行,需替换 `<DEPLOY_DIR>` 为 `/opt/tgjiema`(或实际部署路径)。

## 1. Redis 故障

### 1.1 症状
- db_writer 日志:`[RedisQueue] 连接失败,降级到 SQLite 直写(60s 后重试)`
- mon_bot 告警:`writer_pending > 1000` 或 `redis_health=down`
- 各 Bot 写入变慢(直写 SQLite 有锁竞争)

### 1.2 影响
- 写操作降级到 SQLite 直写(`write_router.should_use_redis()` 返回 False)
- 已入 Stream 但未 ACK 的消息保留在 pending,Redis 恢复后 XAUTOCLAIM 回收
- 不影响读路径(SQLite 缓存全可用)

### 1.3 恢复步骤

```bash
# 1. 检查 Redis 进程状态
systemctl status redis-server

# 2. 检查 Redis 日志(找 OOM / 持久化失败 / 网络问题)
journalctl -u redis-server -n 100 --no-pager

# 3. 验证 Redis 可达
redis-cli ping

# 4. 检查内存使用(若 maxmemory 已满,需扩容或清理)
redis-cli info memory | grep used_memory_human

# 5. 检查 Stream 长度(防止无限增长)
redis-cli XLEN tgjiema:writer:stream
redis-cli XLEN tgjiema:writer:dead

# 6. 检查 pending 消息数(崩溃遗留)
redis-cli XPENDING tgjiema:writer:stream tgjiema-writer-group

# 7. 若 Redis 进程挂掉,重启
systemctl restart redis-server
sleep 2
redis-cli ping

# 8. 重启 db_writer(触发 Consumer Group 重建 + pending 回收)
systemctl restart tgjiema-db_writer

# 9. 若 AOF 损坏导致 Redis 无法启动
redis-check-aof --fix /var/lib/redis/appendonly.aof
systemctl restart redis-server
```

### 1.4 验证命令

```bash
# db_writer 恢复消费
journalctl -u tgjiema-db_writer -n 50 --no-pager | grep "处理\|初始化完成"

# pending 数归零(正常 < 10)
redis-cli XPENDING tgjiema:writer:stream tgjiema-writer-group

# 各 Bot 恢复 Redis 写入(日志应无 "降级到 SQLite" 警告)
journalctl -u tgjiema-up -u tgjiema-idx -u tgjiema-dsp -n 30 --no-pager | grep -i "redis\|降级"
```

## 2. SQLite 故障

### 2.1 锁冲突(database is locked)

**症状**:`sqlite3.OperationalError: database is locked`,PRAGMA busy_timeout=15s 已等待超时。

**影响**:写入失败,用户操作报错。

**恢复步骤**:

```bash
# 1. 检查当前持锁进程(需安装 sqlite3)
sqlite3 <DEPLOY_DIR>/data/cache_store.db "PRAGMA journal_mode;"
fuser <DEPLOY_DIR>/data/cache_store.db 2>/dev/null

# 2. 检查 db_writer 是否卡死(独占连接长时间持有锁)
systemctl status tgjiema-db_writer
journalctl -u tgjiema-db_writer -n 50 --no-pager

# 3. 若 db_writer 卡死,重启(其他 Bot 会降级直写,短暂锁冲突可接受)
systemctl restart tgjiema-db_writer
sleep 5

# 4. 检查 WAL 文件大小(过大说明 checkpoint 失败)
ls -lh <DEPLOY_DIR>/data/cache_store.db-wal
# 若 >100MB,手动 checkpoint
sqlite3 <DEPLOY_DIR>/data/cache_store.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 2.2 文件损坏(file is not a database)

**症状**:`sqlite3.DatabaseError: file is not a database`,CacheStore.init() 自动检测并删除重建。

**恢复步骤**:

```bash
# 1. 确认损坏(备份后删除)
cp <DEPLOY_DIR>/data/cache_store.db /tmp/cache_store.db.bak.$(date +%s)
rm <DEPLOY_DIR>/data/cache_store.db <DEPLOY_DIR>/data/cache_store.db-wal <DEPLOY_DIR>/data/cache_store.db-shm 2>/dev/null

# 2. 重启所有服务(启动时从 CRDB 全量重载)
systemctl restart tgjiema.target

# 3. 验证热表已重建
journalctl -u tgjiema-idx -n 50 --no-pager | grep "预填充"
```

### 2.3 WAL 恢复

**症状**:VPS 异常断电,WAL 未 checkpoint,重启后数据不一致。

**恢复步骤**:SQLite WAL 模式自动恢复,启动时自动 replay WAL。若失败:

```bash
# 1. 停止所有服务
systemctl stop tgjiema.target

# 2. 强制 checkpoint
sqlite3 <DEPLOY_DIR>/data/cache_store.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 3. 若仍失败,删除 WAL(接受丢失未 checkpoint 的写入,CRDB 审计源仍在)
rm <DEPLOY_DIR>/data/cache_store.db-wal <DEPLOY_DIR>/data/cache_store.db-shm

# 4. 重启
systemctl start tgjiema.target
```

## 3. CRDB 故障

### 3.1 连接失败

**症状**:asyncpg 报 `connection refused` / `timeout`;`[DB] DDL版本SQLite检查跳过`。

**影响**:
- SQLite 继续服务(0 RU 路径)
- 新写入无法审计到 CRDB(异步任务失败,日志 DEBUG 级)
- db_backup 失败(Restart=on-failure 60s)
- admin_bot 配置修改失败(backup_config 表不可达)

**恢复步骤**:

```bash
# 1. 验证 CRDB 可达(从 .env 读取 URL)
DB_URL=$(grep ^COCKROACHDB_URL= <DEPLOY_DIR>/.env | cut -d= -f2- | sed 's/ #.*//')
cockroach sql --url "$DB_URL" -e "SELECT 1;"

# 2. 检查 DNS pinning(部署时已写入 /etc/hosts)
grep -E "cockroach|crdb" /etc/hosts

# 3. 检查 VPS 出口 IPv4(某些 VPS 仅 IPv4,CRDB Cloud 需 IPv4)
getent ahostsv4 $(echo $DB_URL | sed 's|.*@||;s|:.*||')

# 4. 若 DNS 解析失败,手动 pin CRDB Cloud IP
# (从 CockroachDB Cloud 控制台获取 ELB IPv4)
echo "<CRDB_IP> <CRDB_HOST>" >> /etc/hosts

# 5. 重启依赖 CRDB 的服务(SQLite 已服务,无需停机)
systemctl restart tgjiema-db_backup
systemctl restart tgjiema-admin_bot
```

### 3.2 RU 超限

**症状**:CRDB Cloud 控制台 RU 消耗告警;asyncpg 报 `rate limit exceeded`。

**恢复步骤**:

```bash
# 1. 检查 RU 消耗(CRDB Cloud 控制台)
# 2. 临时降低同步频率(编辑 .env)
# QUOTA_SYNC_INTERVAL=600   # 5min → 10min
# CRDB_CLEANUP_BATCH_SIZE=1000  # 5000 → 1000

# 3. 禁用 TTL 清理任务(若已废弃,见 admin/migrations/disable_crdb_ttl.sql)
cockroach sql --url "$DB_URL" -f <DEPLOY_DIR>/admin/migrations/disable_crdb_ttl.sql

# 4. 重启 mon_bot(应用新配置)
systemctl restart tgjiema-mon
```

### 3.3 同步延迟

**症状**:`[SyncBack] 批量同步 N 条 job 状态到 CRDB` 频繁;CRDB 数据滞后 SQLite >5 分钟。

**恢复步骤**:

```bash
# 1. 检查 dirty 记录数
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT COUNT(*) FROM cells_local WHERE crdb_synced=0;"
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT COUNT(*) FROM local_job_queue WHERE synced_at=0;"

# 2. 手动触发同步(在 mon_bot 中调用)
# 通过 admin_bot 发送 /sync_now 命令(若已实现),或重启 mon_bot
systemctl restart tgjiema-mon
```

## 4. R2 故障

### 4.1 备份失败

**症状**:db_backup 日志 `R2 PUT failed`;`backup_config.last_backup_at` 不更新。

**恢复步骤**:

```bash
# 1. 检查 R2 凭证(admin_bot /set_r2 或 .env)
DB_URL=$(grep ^COCKROACHDB_URL= <DEPLOY_DIR>/.env | cut -d= -f2-)
cockroach sql --url "$DB_URL" -e "SELECT config_key, length(config_value) FROM backup_config WHERE config_key LIKE 'r2_%';"

# 2. 验证 R2 可达(用 rclone 或 curl)
R2_ENDPOINT=$(grep ^R2_ENDPOINT= <DEPLOY_DIR>/.env | cut -d= -f2-)
curl -I "https://$R2_ENDPOINT/"

# 3. 检查 R2 桶权限(Cloudflare 控制台)
# 4. 重启 db_backup
systemctl restart tgjiema-db_backup
journalctl -u tgjiema-db_backup -n 50 --no-pager
```

### 4.2 恢复失败

**症状**:`python services/db_restore.py --latest` 报错。

**恢复步骤**:

```bash
# 1. 列出可用备份
python <DEPLOY_DIR>/services/db_restore.py --list

# 2. 预览恢复内容(不执行)
python <DEPLOY_DIR>/services/db_restore.py --latest --dry-run

# 3. 恢复特定表
python <DEPLOY_DIR>/services/db_restore.py --table file_records

# 4. 恢复全量(危险!会 UPSERT 覆盖)
python <DEPLOY_DIR>/services/db_restore.py --latest

# 5. 恢复后重启服务(重建 SQLite 缓存)
systemctl restart tgjiema.target
```

## 5. Telegram 频道故障

### 5.1 Active 频道不可用

**症状**:mon_bot 心跳检测 `fail_streak >= 3`;up_bot copy_message 报 `chat not found` / `CHANNEL_PRIVATE`。

**影响**:用户上传失败;dsp_bot 投递到该频道的任务失败。

**恢复步骤**(mon_bot 自动处理,人工介入步骤):

```bash
# 1. 检查 mon_bot 日志(应已触发降级)
journalctl -u tgjiema-mon -n 100 --no-pager | grep -i "degrade\|promote\|lost"

# 2. 检查 cells 状态(应看到 active→lost, shadow1→active)
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT slot_id, status, degrade_count FROM cells_local ORDER BY slot_id;"

# 3. 检查备用池是否有可用频道
cockroach sql --url "$DB_URL" -e "SELECT channel_id, account_name, is_used FROM spare_pool WHERE is_used=0;"

# 4. 若备用池空,通过 admin_bot 添加备用频道
# 在 Telegram 中向 admin_bot 发送:/add_spare <channel_id> <account_name>

# 5. 手动触发轮转(若 mon_bot 未自动处理)
# 通过 admin_bot 发送:/rotate now
```

### 5.2 Shadow 频道补位

**症状**:同组 Active 失败后,Shadow1 提升为 Active。

**恢复步骤**(自动执行,验证步骤):

```bash
# 1. 验证 Shadow1 已提升
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT slot_id, status FROM cells_local WHERE slot_id LIKE '%-1' OR slot_id LIKE '%-2';"

# 2. 验证轮转审计日志
cockroach sql --url "$DB_URL" -e "SELECT * FROM rotate_log ORDER BY timestamp DESC LIMIT 5;"

# 3. 验证 Shadow2 已 cascade 为 Shadow1
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT slot_id, status FROM cells_local WHERE status='shadow1';"
```

### 5.3 账号 ban

**症状**:Telegram API 报 `bot was kicked` / `user is deactivated` / `PEER_ID_INVALID`;该账号所有频道不可用。

**恢复步骤**:

```bash
# 1. 确认 ban 的账号(通过 mon_bot 告警或 admin_bot /status)
journalctl -u tgjiema-mon -n 200 --no-pager | grep -i "ban\|kicked\|deactivated"

# 2. 从备用池拉取新频道(admin_bot 自动或手动)
# 3. 重新 seed_topology(若拓扑大改)
cd <DEPLOY_DIR>
python admin/seed_topology.py --yes

# 4. 重启所有 Bot(刷新拓扑)
systemctl restart tgjiema.target
```

## 6. 进程崩溃恢复

### 6.1 db_writer 崩溃

**恢复机制**:
- systemd `Restart=on-failure` + `RestartSec=10s`
- Redis Stream pending 消息不丢失(XAUTOCLAIM 回收 idle >30s 的消息)
- writer_inbox 表幂等(已处理的消息 XACK 跳过)

**验证步骤**:

```bash
# 1. 检查 db_writer 状态
systemctl status tgjiema-db_writer

# 2. 检查 pending 是否被回收
redis-cli XPENDING tgjiema:writer:stream tgjiema-writer-group

# 3. 查看恢复日志
journalctl -u tgjiema-db_writer -n 50 --no-pager | grep -i "reclaim\|回收\|初始化"

# 4. 若 db_writer 反复崩溃(60s 内 >5 次),systemd 进入 failed
systemctl reset-failed tgjiema-db_writer
systemctl start tgjiema-db_writer
```

### 6.2 up_bot 崩溃

**恢复机制**:
- systemd `Restart=always` + `RestartSec=10s`
- `upload_sessions` 表中未完成会话 lease 过期后置 EXPIRED
- `pending_uploads` 表 CRDB 持久化,重启后可重放

**验证步骤**:

```bash
# 1. 检查 up_bot 状态
systemctl status tgjiema-up

# 2. 检查未完成的上传会话
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT upload_id, status, lease_until FROM upload_sessions WHERE status NOT IN ('READY','ABORTED','EXPIRED');"

# 3. 清理过期会话(若 up_bot 启动后未自动清理)
sqlite3 <DEPLOY_DIR>/data/cache_store.db "UPDATE upload_sessions SET status='EXPIRED' WHERE lease_until < strftime('%s','now') AND status NOT IN ('READY','ABORTED','EXPIRED');"
```

### 6.3 dsp_bot 崩溃

**恢复机制**:
- systemd `Restart=always`
- `local_job_queue` 中 `dispatched` 状态任务超时(5min)后回退为 `pending`(`reclaim_stale_dispatched`)
- `delivery_receipts` 表持久化已投递 msg_id,避免重复投递

**验证步骤**:

```bash
# 1. 检查 dsp_bot 状态
systemctl status tgjiema-dsp

# 2. 检查 dispatched 任务数(应 < 5min 内的数量)
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT COUNT(*) FROM local_job_queue WHERE status='dispatched';"

# 3. 检查 pending 任务积压
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT COUNT(*) FROM local_job_queue WHERE status='pending';"

# 4. 检查死信任务
sqlite3 <DEPLOY_DIR>/data/cache_store.db "SELECT COUNT(*) FROM local_job_queue WHERE status='dead';"
```

## 7. 全新机恢复演练

### 7.1 前置条件

- 新 VPS 已安装 Ubuntu 22.04+ / Debian 12+
- 拥有 CRDB Cloud 访问凭证(COCKROACHDB_URL)
- 拥有 R2 桶访问凭证(R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)
- 拥有 5 个 Bot Token + 5 个账号的频道 ID(写入 .env)
- 拥有 RELAY_ENCRYPTION_KEY(必须与原部署一致,否则中继账号无法解密)

### 7.2 步骤

```bash
# 步骤 1:克隆代码到 /opt/tgjiema
git clone <repo_url> /opt/tgjiema
cd /opt/tgjiema

# 步骤 2:复制 .env(从安全备份恢复)
cp /path/to/.env.backup /opt/tgjiema/.env
chmod 600 /opt/tgjiema/.env

# 步骤 3:运行部署脚本(自动安装依赖 + 创建 systemd + 配置 Redis + secrets 隔离)
bash deploy_vps_per_bot.sh

# 步骤 4:验证 CRDB 连通(部署脚本已自动 DNS pinning)
source /opt/tgjiema/.env
cockroach sql --url "$COCKROACHDB_URL" -e "SELECT count(*) FROM cells;"

# 步骤 5:验证 R2 备份可达
source /opt/tgjiema/.env
curl -I "https://${R2_ENDPOINT}/${R2_BUCKET_NAME}/"

# 步骤 6:从 R2 恢复最新备份(若需恢复历史数据)
cd /opt/tgjiema
source venv/bin/activate
python services/db_restore.py --latest --dry-run  # 预览
python services/db_restore.py --latest           # 执行恢复

# 步骤 7:重建拓扑(从 .env 账号配置)
python admin/seed_topology.py --yes

# 步骤 8:启动所有服务(部署脚本已自动启动,此处手动验证)
systemctl start tgjiema.target

# 步骤 9:验证服务状态
systemctl status tgjiema-*

# 步骤 10:验证业务功能
# - 向 up_bot 发送测试文件
# - 向 idx_bot 发送测试解码请求
# - 验证 dsp_bot 投递
# - 检查 admin_web:http://127.0.0.1:8080/health
```

### 7.3 验证清单

- [ ] 8 个 systemd 服务全部 active(`systemctl is-active tgjiema-*`)
- [ ] Redis 可达(`redis-cli ping` 返回 PONG)
- [ ] CRDB 可达(`cockroach sql -e "SELECT 1"` 成功)
- [ ] R2 可达(备份任务成功执行一次)
- [ ] bot_heartbeat 表有记录(`sqlite3 data/cache_store.db "SELECT name, last_ping FROM bot_heartbeat;"`)
- [ ] cells_local 表有拓扑(`sqlite3 data/cache_store.db "SELECT count(*) FROM cells_local;"`)
- [ ] file_records_local 表已从 CRDB 加载(`sqlite3 data/cache_store.db "SELECT count(*) FROM file_records_local;"`)
- [ ] db_writer 消费正常(`journalctl -u tgjiema-db_writer -n 20 | grep "初始化完成"`)
- [ ] 上传测试:向 up_bot 发送文件,验证 Active 频道收到
- [ ] 解码测试:向 idx_bot 发送文件码,验证 dsp_bot 投递
- [ ] 备份测试:`systemctl start tgjiema-db_backup` 后检查 R2 桶有新文件

## 8. 监控告警阈值

| 指标 | 告警阈值 | 严重 | 检查命令 |
|------|---------|------|---------|
| db_writer pending | > 1000 | WARN | `redis-cli XPENDING tgjiema:writer:stream tgjiema-writer-group` |
| db_writer 死信队列 | > 0 | CRIT | `redis-cli XLEN tgjiema:writer:dead` |
| local_job_queue pending | > 100 | WARN | `sqlite3 data/cache_store.db "SELECT count(*) FROM local_job_queue WHERE status='pending';"` |
| local_job_queue dead | > 10 | WARN | `sqlite3 data/cache_store.db "SELECT count(*) FROM local_job_queue WHERE status='dead';"` |
| relay_spool FAILED | > 0 | WARN | `sqlite3 data/relay_pool.db "SELECT count(*) FROM relay_spool WHERE status='FAILED';"` |
| cells_local active=0 | 0 | CRIT | `sqlite3 data/cache_store.db "SELECT count(*) FROM cells_local WHERE status='active';"` |
| cells_local lost | > 0 | WARN | `sqlite3 data/cache_store.db "SELECT count(*) FROM cells_local WHERE status='lost';"` |
| relay pool size | < 2 | WARN | `sqlite3 data/relay_pool.db "SELECT count(*) FROM relay_accounts WHERE is_active=1;"` |
| Bot heartbeat 延迟 | > 60s | WARN | `sqlite3 data/cache_store.db "SELECT name, last_ping FROM bot_heartbeat WHERE last_ping < strftime('%s','now')-60;"` |
| CRDB 连接 | 失败 | CRIT | `cockroach sql -e "SELECT 1"` |
| R2 备份延迟 | > 12h | WARN | `cockroach sql -e "SELECT config_value FROM backup_config WHERE config_key='last_backup_at';"` |
