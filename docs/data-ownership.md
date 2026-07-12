# tgjiema 数据权威与所有权(单一事实源)

> 最后更新: 2026-07-12 | DDL_VERSION: 7

## 1. 数据权威定义

| 领域 | 权威源(Source of Truth) | 缓存/副本 | 禁止事项 |
|------|------------------------|----------|---------|
| 用户配额 | SQLite `user_quota`(idx_bot 写) | CRDB `users`(每 5min 批量同步) | 禁止从 CRDB 读配额做扣减决策 |
| 文件记录 | CRDB `file_records`(admin_bot/idx_bot 写) | SQLite `file_records_local`(全量缓存) | 禁止绕过 `update_file_record_and_invalidate` 直写 SQLite |
| 取件码 | CRDB `codes` | SQLite `codes_local`(全量缓存) | 禁止直读 CRDB(0 RU 路径优先) |
| 拓扑/Cells | SQLite `cells_local`(mon_bot 写) | CRDB `cells`(异常事件批量同步) | 禁止 dsp_bot 直写 cells_local 状态(用 CAS) |
| 中继账号 | SQLite `relay_pool.db` `relay_accounts` | CRDB `relay_accounts`(云端备份) | 禁止明文存 api_hash(Fernet 加密必填) |
| 投递回执 | SQLite `delivery_receipts`(dsp_bot 写) | — | 禁止内存 `_sent_msg_tracker` 作为权威 |
| 副本元数据 | SQLite `manifest`(up_bot 写) | — | 禁止 Telethon 读历史替代 manifest |
| 上传会话 | SQLite `upload_sessions`(up_bot 写) | — | 禁止跨进程非 CAS 修改状态 |
| 写入幂等 | SQLite `writer_inbox`(db_writer 写) | — | 禁止绕过 inbox 直接 XACK |
| 配置 | CRDB `backup_config`(admin_bot 写) | SQLite `kv_store` + 内存缓存 | 禁止白名单读 L1(get_config_fresh 绕过) |
| 心跳 | SQLite `heartbeat_local` + `bot_heartbeat` | — | 禁止写 CRDB(0 RU 路径) |
| 审计日志 | CRDB `decode_logs` / `rotate_log` / `jobs` | — | 100 年 TTL,人工清理 |

## 2. SQLite 表清单(cache_store.db)

DB 路径:`data/cache_store.db` | PRAGMA: WAL + synchronous=NORMAL + busy_timeout=15s + wal_autocheckpoint=1000

| 表名 | 用途 | Owner 进程 | 写路径 | 同步方向 | 保留期 |
|------|------|----------|--------|--------|-------|
| `cache_backup` | 内存缓存磁盘备份(key→json) | up/idx/dsp(mon 触发 dump) | `dump()` 批量 INSERT OR REPLACE | 仅本地 | 启动加载,无 TTL |
| `pending_notify` | Up→Idx 跨进程通知 | up(W) / idx(R) | `notify_new_upload()` | 仅本地 | 消费后清理 |
| `dsp_notify` | Idx→Dsp 跨进程通知 | idx(W) / dsp(R) | `notify_dsp_new_job()` | 仅本地 | 消费后清理 |
| `decode_log_buffer` | 解码日志缓冲批写 | idx(W) | buffer flush | → CRDB decode_logs | 7 天 |
| `heartbeat_local` | Mon 频道心跳(slot_id→last_ok) | mon(W) | `write_heartbeat()` | 仅本地 | 永久 |
| `bot_heartbeat` | 跨进程 Bot 心跳 | 各 Bot(W) / admin(R) | `write_bot_heartbeat()` | 仅本地 | 永久 |
| `user_quota` | 用户配额(权威) | idx(W) / mon(R) | `upsert_user_quota()` | → CRDB users 每 5min | 永久 |
| `local_job_queue` | 派工任务本地队列(权威) | idx(W) / dsp(W) / mon(R) | `insert_local_job()` / `mark_local_job_dispatched()` | ↔ CRDB jobs 双向 | 7 天 |
| `counter_snapshot` | 启动统计快照 | mon(W) | `save_counter_snapshot()` | 仅本地 | 永久 |
| `cells_snapshot` | cells 全量快照(单行 id=1) | mon(W) / all(R) | `save_cells_snapshot()` | ← CRDB cells 启动恢复 | 永久 |
| `cells_change_notify` | cells 变更版本通知 | mon(W) / all(R) | `batch_update_cells_local()` | 仅本地 | 消费后清理 |
| `relay_change_notify` | relay 池变更通知 | admin_bot(W) / idx(R) | `notify_relay_change()` | 仅本地 | 消费后清理 |
| `file_record_change_notify` | 文件记录变更通知(举报) | admin_bot(W) / idx,dsp(R) | `notify_record_change()` | 仅本地 | 消费后清理 |
| `cells_local` | cells 本地逐行存储(权威) | mon(W) / dsp(W-CAS) | `cas_transition_cell()` / `batch_update_cells_local()` | → CRDB cells 异常事件 | 永久 |
| `manifest` | 副本元数据(group,fuid,channel→msg_id) | up(W) / mon(R) | `upsert_manifest()` | → CRDB manifest 备份 | 永久 |
| `kv_store` | KV 配置缓存(DDL 版本等) | db_writer(W) / all(R) | `set_kv()` | ← CRDB rotation_config | 永久 |
| `ttl_cache` | 通用 TTL 缓存(跨进程) | 各 Bot(W) | `cache_set()` | 仅本地 | TTL |
| `user_bot_started` | 用户 /start 跨进程状态 | idx,dsp(W) / up,all(R) | `mark_user_started()` | 仅本地 | 永久 |
| `pending_file_codes` | 待发送文件码(用户未 /start idx) | idx(W) / up(R) | `add_pending_file_code()` | 仅本地 | 永久 |
| `file_records_local` | 文件记录全量缓存 | admin_bot,idx(W) / all(R) | `upsert_file_record_local()` | ← CRDB file_records 启动加载 | 永久 |
| `codes_local` | 取件码全量缓存 | idx(W) / all(R) | `upsert_code_local()` | ← CRDB codes 启动加载 | 永久 |
| `users_local` | 用户全量缓存 | idx(W) / all(R) | `upsert_user_local()` | ← CRDB users 启动加载 | 永久 |
| `external_code_mapping_local` | 外部码映射全量缓存 | idx(W) / all(R) | `bootstrap_external_mappings()` | ← CRDB external_code_mapping | 永久 |
| `writer_inbox` | Writer 幂等去重(message_id) | db_writer(W 独占) | `write_writer_inbox()` | 仅本地 | 168 小时(7 天) |
| `upload_sessions` | 上传会话状态机 | up(W) | `create_upload_session()` / `transition_upload_session()` | → CRDB upload_sessions 备份 | 30 天 |
| `upload_outbox` | 事务发件箱(派工) | up(W) / dsp(R) | `create_outbox_entry()` / `mark_outbox_done()` | → CRDB upload_outbox 备份 | 30 天 |
| `quota_ledger` | 配额变更流水(追加式) | idx(W) | `append_quota_ledger()` | → CRDB quota_ledger 备份 | 90 天 |
| `delivery_receipts` | 投递回执 | dsp(W) | `upsert_delivery_receipt()` / `confirm_delivery_receipt()` | → CRDB delivery_receipts 备份 | 30 天 |
| `replication_tasks` | 副本复制任务 | mon(W) | `create_replication_task()` / `mark_replication_*()` | → CRDB replication_tasks 备份 | 30 天 |

## 3. CRDB 表清单

DDL_VERSION=7,通过 `kv_store.ddl_version` 缓存避免每次启动查 CRDB。

| 表名 | 用途 | Owner | 写路径 | RPO |
|------|------|-------|--------|-----|
| `users` | 用户权威(配额审计) | idx(主) / admin_bot | `update_user_and_invalidate()` 双写 | ≤5min(SQLite→CRDB) |
| `file_records` | 文件记录权威 | idx / admin_bot | `update_file_record_and_invalidate()` 双写 | ≤实时(同步写) |
| `decode_logs` | 解码审计日志 | idx | buffer flush 批写 | ≤1min |
| `pending_uploads` | 上传待处理(历史遗留) | up | `insert_one()` | ≤实时 |
| `send_queue` | 已废弃(v2 用 jobs 替代) | — | 不再写 | — |
| `backup_config` | 配置权威(admin 热修改) | admin_bot | `_set_config()` | ≤实时 |
| `message_backups` | 主备频道消息映射 | mon | `save_message_backup()` | ≤实时 |
| `cells` | cells 审计备份 | mon(异常事件) | `sync_dirty_cells_to_crdb()` | ≤5min |
| `codes` | 取件码权威 | idx | 双写 | ≤实时 |
| `jobs` | 派工任务审计 | idx(异步) / dsp(状态回写) | `_sync_new_job_to_crdb()` fire-and-forget | ≤30s |
| `rotate_log` | 轮转审计日志 | mon | `log_rotate()` | ≤实时 |
| `spare_pool` | 备用频道池 | admin_bot / mon | `add_spare_channel()` | ≤实时 |
| `rotation_config` | 轮转配置 + DDL 版本 | 各 Bot | `set_rotation_config()` | ≤实时 |
| `external_code_mapping` | 外部码映射权威 | idx(采集器) | `set_external_code_mapping()` | ≤实时 |
| `relay_accounts` | 中继账号云端备份 | admin_bot / relay_db | `sync_relay_to_crdb()`(api_hash 加密) | ≤实时 |
| `code_bot_mapping` | 文件码前缀→Bot 路由 | admin_bot | `save_code_bot_mapping()` | ≤实时 |

注:M1 5 张新表(upload_sessions/upload_outbox/quota_ledger/delivery_receipts/replication_tasks)的 CRDB 副本由 db_backup 备份,主权威在 SQLite。

## 4. relay_pool.db 表清单

DB 路径:`data/relay_pool.db` | PRAGMA: WAL + synchronous=NORMAL + busy_timeout=15s

| 表名 | 用途 | Owner | 加密 |
|------|------|-------|------|
| `relay_accounts` | 中继账号池(api_id/api_hash/phone) | admin_bot(W) / idx_bot(R) | api_hash Fernet 加密 |
| `relay_usage` | 中继账号使用统计(今日/总请求数/avg_wait) | relay_pool(R/W) | ✗ |
| `relay_log` | 中继操作日志(action/code/duration/error) | relay_pool(W) | ✗ |
| `bot_cooldown` | Bot 解码冷却时间(限速提取) | relay_pool(R/W) | ✗ |
| `mapped_codes` | 外部码映射缓存(本地) | idx_bot(W) / relay_pool(R) | ✗ |
| `bot_overrides` | 文件码前缀→Bot 覆盖规则 | admin_bot(W) | ✗ |
| `relay_spool` | 中继任务池(M1 业务闭环) | up_bot(W) / relay_pool(R/W) | ✗ |

## 5. Redis 数据结构

| Key | 类型 | 用途 | TTL | 持久化 |
|-----|------|------|-----|--------|
| `tgjiema:writer:stream` | Stream | 写操作队列(主) | MAXLEN ~10000 | AOF everysec |
| `tgjiema:writer:dead` | Stream | 死信队列 | MAXLEN ~10000 | AOF everysec |
| `cache:user_quota:{uid}` | String | 用户配额读缓存 | 5s(WRITER_CACHE_TTL_QUOTA) | 可丢失(降级查 SQLite) |
| `cache:file_record:{code}` | String | 文件记录读缓存 | 30s(WRITER_CACHE_TTL_FILE_RECORD) | 可丢失 |
| `cache:code:{code}` | String | 取件码读缓存 | 30s(WRITER_CACHE_TTL_CODE) | 可丢失 |
| `cache:user:{uid}` | String | 用户记录读缓存 | 30s(WRITER_CACHE_TTL_USER) | 可丢失 |
| `cache:cells` | String | 全量 cells 读缓存 | 10s(WRITER_CACHE_TTL_CELLS) | 可丢失 |
| `cache:bot_hb:{name}` | String | Bot 心跳读缓存 | 5s(WRITER_CACHE_TTL_BOT_HB) | 可丢失 |
| `cache:kv:{key}` | String | KV 配置读缓存 | 60s(WRITER_CACHE_TTL_KV) | 可丢失 |
| Consumer Group `tgjiema-writer-group` | Stream Group | db_writer 消费组 | — | 随 Stream 持久化 |

Redis 配置要求(`deploy_vps_per_bot.sh` 强制设置):
- `appendonly yes` + `appendfsync everysec`(最多丢 1 秒)
- `maxmemory-policy noeviction`(满时不逐出,返回错误防消息丢失)

## 6. RPO/RTO 定义

| 数据类别 | RPO(数据丢失容忍) | RTO(恢复时间) | 恢复方式 |
|---------|------------------|---------------|---------|
| 用户配额(user_quota) | ≤5 分钟(SQLite→CRDB 同步间隔) | ≤1 分钟 | SQLite 仍可服务;CRDB 重启后从 SQLite 重放 |
| 文件记录(file_records) | 0(同步双写) | ≤5 分钟 | CRDB 不可用时 SQLite 继续;恢复后从 R2 备份恢复 |
| 派工任务(jobs / local_job_queue) | ≤30 秒(SQLite 主,CRDB 异步审计) | ≤1 分钟 | SQLite 是主路径;CRDB 仅审计 |
| 中继账号(relay_accounts) | 0(本地即时写 + CRDB 异步同步) | ≤5 分钟 | 从 CRDB 拉取恢复(需正确 RELAY_ENCRYPTION_KEY) |
| 上传会话(upload_sessions) | ≤1 分钟 | ≤5 分钟 | lease 过期后 EXPIRED;用户重试 |
| 投递回执(delivery_receipts) | ≤1 分钟 | ≤5 分钟 | 从 R2 备份恢复 |
| 副本元数据(manifest) | 0(SQLite 主) | ≤5 分钟 | 从 R2 备份恢复 |
| Writer 幂等(writer_inbox) | 0(同事务提交) | — | 168 小时后清理;崩溃恢复用 |
| 跨进程通知(pending_notify 等) | 可丢失(消费后清理) | ≤1 秒 | 重启后从 SQLite 重读 |
| 审计日志(decode_logs/rotate_log) | ≤1 分钟 | ≤30 分钟 | CRDB 100 年 TTL;无外部备份 |
| 备份快照(R2) | ≤6 小时(DB_BACKUP_INTERVAL_MINUTES) | ≤30 分钟 | `python services/db_restore.py --latest` |

## 7. 配置备份(backup_config 表)敏感字段加密

| config_key | 加密 | 说明 |
|-----------|------|------|
| `relay_api_hash` | ✓ Fernet | 中继账号 API Hash |
| `r2_secret_key` | ✓ Fernet | R2 Secret Key(R26-M1) |
| `backup_bot_{n}_token` | ✗ 明文 | 历史 Bot Token(P2-4 后已废弃) |
| `code_bot_route:*` | ✗ | 文件码前缀路由 |
| `relay_account_ids` | ✗ | 中继白名单(数字 ID) |
| `collector_account_ids` | ✗ | 采集器白名单(数字 ID) |
| `ddl_version` | ✗ | rotation_config 表(非 backup_config) |

注:`backup_config` 备份到 R2 时不脱敏(P0-3),因 R2 为运维可信环境,脱敏会导致恢复后凭证不可用。
