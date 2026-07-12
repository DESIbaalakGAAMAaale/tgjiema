# R39 P1-5 — Tombstone 软删除路径说明

## 背景

R39 终审发现,Admin 删除仅把 `status` 设为 `deleted`,没有写 `deleted_at`,也未证明 dirty_outbox 写入 tombstone。其他 cleanup/delete 路径仍可能物理删除。这会导致:
- 增量备份无法捕捉删除事件(仅靠 updated_at 无法感知删除)
- crdb_sync 无法同步删除到 CRDB(没有 tombstone 事件)
- 物理删除会丢失行用于 CRDB tombstone 同步的依据

## 整改方案

### 1. 统一软删除 API

`database/cache_store.py` 新增 `soft_delete(table, pk, deleted_at)`:
- 支持 file_records / codes / users / cells / external_code_mapping 五张表
- 执行 `UPDATE <table>_local SET deleted_at=?, status='deleted', crdb_synced=0 WHERE <pk>=?`
- 写入 `dirty_outbox` 一条 tombstone 记录(`operation='tombstone'`)
- 返回 bool(成功/未命中)

### 2. 所有删除路径必须走 soft_delete

| 路径 | 调用 | tombstone |
| --- | --- | --- |
| Admin Web `/files/{code}/delete` | `cache_store.soft_delete("file_records", code)` + CRDB `update_one($set: status, deleted_at)` | ✅ dirty_outbox tombstone |
| Bot 文件过期清理 | `delete_file_record_local`(已 R38 P1-3 实现 UPDATE+deleted_at) | ✅ status='deleted' + deleted_at |
| Bot cell 删除 | `delete_cell_local`(已 R38 P1-3 实现 UPDATE+deleted_at) | ✅ status='deleted' + deleted_at |
| retention 物理删除 | 仅在已备份 + 已同步 + 保留期届满后执行 | 物理 DELETE FROM |

### 3. Admin 删除改造

`admin/__init__.py` 的 `delete_file`:
- 原来: `files_col.update_one({"$set": {"status": "deleted"}})` — 缺 `deleted_at`
- 现在: 同时设置 `status='deleted'` + `deleted_at` + 调用 `cache_store.soft_delete()` 写本地 tombstone + dirty_outbox
- CRDB 更新和本地 SQLite tombstone 双写,保证跨节点一致

### 4. 物理删除规则

物理删除(`DELETE FROM`)仅在以下条件全部满足时由独立 retention job 执行:
1. 记录的 `deleted_at` 已设置(已软删除)
2. 距 `deleted_at` 超过保留期(如 30 天)
3. 已备份(最新备份包含此 tombstone)
4. 已同步到 CRDB(`crdb_synced=1`)
5. dirty_outbox tombstone 已处理(`processed=1`)

retention job 示例逻辑:
```python
# 物理删除已软删除且超过保留期的记录
cutoff = now - retention_days * 86400
await db.execute(
    "DELETE FROM file_records_local "
    "WHERE deleted_at IS NOT NULL AND deleted_at < ? AND crdb_synced = 1",
    (cutoff_iso,)
)
```

## 已实现的 delete 路径

### file_records
- `cache_store.delete_file_record_local()` — R38 P1-3 已实现 UPDATE+deleted_at
- `cache_store.soft_delete("file_records", code)` — R39 P1-5 新增,额外写 dirty_outbox tombstone
- Admin `/files/{code}/delete` — R39 P1-5 已改造,调用 soft_delete

### cells
- `cache_store.delete_cell_local()` — R38 P1-3 已实现 UPDATE+deleted_at
- `cache_store.soft_delete("cells", slot_id)` — R39 P1-5 新增

### codes / users / external_code_mapping
- 通过 `cache_store.soft_delete(table, pk)` 统一软删除

## 相关文件

- `database/cache_store.py` — `soft_delete()` / `delete_file_record_local()` / `delete_cell_local()` / `add_dirty_outbox()`
- `admin/__init__.py` — `delete_file` 路由(R39 P1-5 改造)
- `services/crdb_sync_service.py` — 消费 dirty_outbox tombstone 同步到 CRDB
- `services/db_backup.py` — 增量备份检查 `deleted_at > watermark` 捕捉删除事件
