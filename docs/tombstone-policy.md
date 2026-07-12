# Tombstone 软删除策略(R38 P1-3)

本文档说明 TG文件解码器 项目的统一软删除(tombstone)策略,确保
CRDB 跨节点同步能够正确感知删除事件,避免"本地删除 → CRDB 残留"
或"CRDB 删除 → 本地拉回"的不一致问题。

---

## 1. 背景与问题

原实现中,生产代码大量使用 `DELETE FROM ...` 直接物理删除行:

- `database/cache_store.py` `delete_file_record_local()` 直接 DELETE file_records_local
- `database/cache_store.py` `delete_cell_local()` 直接 DELETE cells_local
- `database/relay_db.py` `remove_account()` 直接 DELETE relay_accounts

这种做法在 CRDB 同步场景下存在以下问题:

1. **行丢失 → 无法 tombstone 同步**:DELETE 后行立即从 SQLite 消失,
   crdb_sync 通过 `crdb_synced=0` 检测 dirty 的机制无法识别"已删除"行,
   CRDB 中对应记录残留,跨节点不一致。
2. **重启拉回**:本地删除后,bootstrap_runner 从 CRDB 全量加载时
   会把 CRDB 中残留的行重新拉回 SQLite,造成"删除撤销"假象。
3. **审计缺失**:删除操作无时间戳、无操作者,事故排查困难。

---

## 2. 软删除策略

### 2.1 业务表统一增加 `deleted_at` 列

R38 P1-3 为以下业务表新增 `deleted_at TEXT` 列(SQLite 本地 + CRDB):

| 表名 | 主键 | 原 status 列 | 新增 deleted_at |
| ---- | ---- | ----------- | -------------- |
| `file_records_local` | file_code | TEXT DEFAULT 'active' | ✅ |
| `codes_local` | code | TEXT DEFAULT 'active' | ✅ |
| `users_local` | user_id | (is_banned) | ✅ |
| `external_code_mapping_local` | external_code | (无) | ✅ |
| `cells_local` | slot_id | TEXT NOT NULL DEFAULT 'shadow1' | ✅ |
| `relay_accounts` | phone | TEXT DEFAULT 'unknown' | ✅ |

迁移方式:`CREATE TABLE IF NOT EXISTS` 新建表含 `deleted_at` 列;
对已存在的表,通过 `ALTER TABLE ... ADD COLUMN deleted_at TEXT`(try/except 幂等)。

### 2.2 DELETE → UPDATE 转换规则

| 场景 | 处理方式 |
| ---- | ------- |
| 业务数据删除(file_record / cell / relay_account) | `UPDATE ... SET deleted_at=?, status='deleted', crdb_synced=0` |
| 缓存重建(bootstrap_codes / bootstrap_users 等 `DELETE FROM table`) | 保留 DELETE(全表重建,无需 tombstone) |
| retention 清理(WHERE created_at < ? / ts < ?) | 保留 DELETE(过期清理,非业务删除) |
| writer_inbox / pending_notify / dsp_notify 清理 | 保留 DELETE(队列清理,非业务删除) |
| ttl_cache / counter_snapshot / cache_backup 清理 | 保留 DELETE(内部缓存) |
| factory_reset(管理员显式确认清空全部数据) | 保留 DELETE(灾难恢复操作,需显式二次确认) |
| db_restore(从备份恢复时清空表) | 保留 DELETE(恢复操作前置清理) |

### 2.3 crdb_sync 如何识别 tombstone

通过两种机制(并存,互不冲突):

1. **`crdb_synced=0` 标志**:UPDATE 软删除时同步设置 `crdb_synced=0`,
   crdb_sync 检测到 dirty → 拉取行 → 看到 `status='deleted'` + `deleted_at!=null`
   → 向 CRDB 发送 `UPDATE ... SET deleted_at, status='deleted'`(或按业务需要 DELETE)。
2. **R38 P1-2 dirty_outbox**:业务代码调用 `add_dirty_outbox(table, pk, operation='tombstone')`,
   crdb_sync 从 dirty_outbox 批量拉取 tombstone 事件,按 operation 类型决定 UPSERT 或 tombstone。

---

## 3. 已转换的 DELETE 语句清单

### 3.1 已转换为 soft-delete 的语句

| 文件 | 原语句 | 新语句 |
| ---- | ------ | ------ |
| `database/cache_store.py` `delete_file_record_local()` | `DELETE FROM file_records_local WHERE file_code=?` | `UPDATE ... SET deleted_at=?, status='deleted', crdb_synced=0` |
| `database/cache_store.py` `delete_cell_local()` | `DELETE FROM cells_local WHERE slot_id=?` | `UPDATE ... SET deleted_at=?, status='deleted', crdb_synced=0` |
| `database/relay_db.py` `remove_account()` | `DELETE FROM relay_accounts WHERE phone=?` | `UPDATE ... SET deleted_at=?, is_active=0, status='deleted'` |

### 3.2 保留 DELETE 的语句(已审计)

| 文件 | 语句 | 保留理由 |
| ---- | ---- | ------- |
| `database/cache_store.py` `bootstrap_*()` | `DELETE FROM codes_local / users_local / file_records_local / external_code_mapping_local`(全表) | bootstrap 全量重建,需要清空旧数据 |
| `database/cache_store.py` retention 清理 | `DELETE FROM ... WHERE created_at < ?` | 过期数据 retention,非业务删除 |
| `database/cache_store.py` 队列清理 | `DELETE FROM writer_inbox / pending_notify / dsp_notify` | 队列消息清理 |
| `database/cache_store.py` `delete_pending_file_code()` | `DELETE FROM pending_file_codes WHERE id=?` | 内部暂存表,无需 tombstone |
| `database/cache_store.py` `delete_upload_session()` | `DELETE FROM upload_sessions WHERE upload_id=? AND status IN ('READY','ABORTED','EXPIRED')` | 会话终态清理,非业务数据 |
| `database/cache_store.py` `clear_ttl_cache_prefix()` | `DELETE FROM ttl_cache WHERE key LIKE ?` | 内部 TTL 缓存 |
| `database/relay_db.py` `clear_usage()` | `DELETE FROM relay_usage`(全表) | 统计清零,非业务数据 |
| `database/relay_db.py` `unmark_code()` | `DELETE FROM mapped_codes WHERE code=?` | 本地缓存,非业务数据 |
| `database/relay_db.py` `remove_bot_override()` | `DELETE FROM bot_overrides WHERE prefix=?` | 配置项删除,非业务数据 |
| `database/relay_db.py` `clear_bot_cooldowns()` | `DELETE FROM bot_cooldown` | 内部冷却状态 |
| `bots/admin_bot/handlers.py` factory_reset | `DELETE FROM {table}`(显式二次确认) | 灾难恢复,需 `/factory_reset confirm I_UNDERSTAND` |
| `services/db_restore.py` | `DELETE FROM "{table}"`(恢复前清空) | 备份恢复操作前置清理 |
| `database/cache.py` `decode_log_buffer` 清理 | `DELETE FROM decode_log_buffer WHERE id IN (...)` | 日志缓冲,非业务数据 |
| `database/session.py` `delete_one()` / `delete_many()` | 通用 CRUD `DELETE FROM` | 框架方法,具体行为由调用方决定(已审计调用点) |

---

## 4. 审计与维护

### 4.1 新增 DELETE 语句的检查清单

提交代码前,若新增 `DELETE FROM` 语句,需回答以下问题:

- [ ] 目标表是否为业务数据表(file_records / codes / users / cells / relay_accounts)?
- [ ] 是否为缓存重建(全表 DELETE)?若是,保留 DELETE,在 docstring 标注。
- [ ] 是否为 retention 清理(WHERE 时间条件)?若是,保留 DELETE。
- [ ] 是否为业务删除(单行/批量按业务主键)?若是,**必须**改为 `UPDATE ... SET deleted_at=?, status='deleted'`。

### 4.2 crdb_sync tombstone 同步路径

```
业务代码
  ↓ UPDATE ... SET deleted_at=?, status='deleted', crdb_synced=0
SQLite 本地表(行保留,标记软删除)
  ↓ crdb_sync 检测 crdb_synced=0 或 dirty_outbox.operation='tombstone'
CRDB UPSERT(写入 deleted_at, status='deleted')或 DELETE(按业务需要)
  ↓ 标记 crdb_synced=1 / mark_dirty_processed()
完成同步
```

### 4.3 查询过滤

业务读查询**必须**过滤 `deleted_at IS NULL` 或 `status != 'deleted'`,
避免读到已软删除的行。已有查询大部分使用 `status='active'` 过滤,
新增查询需注意此约束。

---

## 5. 相关文档

- `docs/delivery-idempotency.md` — 投递幂等(delivery_token)
- `docs/data-ownership.md` — 数据所有权
- `docs/recovery-runbook.md` — 恢复手册
- `database/cache_store.py` — dirty_outbox 表与 add/get/mark 方法(R38 P1-2)
- `services/crdb_sync_service.py` — crdb_sync 懒加载 + dirty 检测(R38 P1-1)
