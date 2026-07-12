# R39 P2-7: 数据库容量、水位与 Vacuum/WAL Checkpoint 策略

## 背景

R39 终审指出: "建立数据库容量、水位和 vacuum/WAL checkpoint 策略。"

项目使用两类数据库:
- **SQLite** (cache_store.db / relay_pool.db): 本地权威存储,写入频繁
- **CockroachDB** (CRDB): 跨机镜像/审计

SQLite 长期运行会累积:
- DELETE 留下的空闲页(VACUUM 回收)
- WAL 文件膨胀(checkpoint 控制)
- 历史数据无 TTL(容量无限增长)
- 索引碎片(REINDEX 优化)

CRDB 则需:
- Zone config 配置 TTL
- Range split/merge 监控
- 节点存储水位监控

---

## 1. SQLite 容量与水位

### 1.1 数据库文件监控

```python
# services/db_health.py (建议实现)
import os
import sqlite3
from pathlib import Path

def get_sqlite_stats(db_path: str) -> dict:
    """获取 SQLite 数据库文件统计。"""
    path = Path(db_path)
    if not path.exists():
        return {"exists": False}
    size_bytes = path.stat().st_size
    # 获取页面计数和空闲页面
    conn = sqlite3.connect(db_path)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    return {
        "exists": True,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 2),
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "fragmentation_pct": round(freelist_count / max(page_count, 1) * 100, 2),
    }
```

### 1.2 水位阈值

| 指标 | 警告水位 | 严重水位 | 处理动作 |
| ---- | -------- | -------- | -------- |
| cache_store.db 文件大小 | > 500 MB | > 1 GB | 触发 VACUUM + 历史数据归档 |
| relay_pool.db 文件大小 | > 200 MB | > 500 MB | 触发 VACUUM + relay 清理 |
| 空闲页占比 (freelist/page_count) | > 20% | > 40% | 触发 VACUUM |
| WAL 文件大小 | > 100 MB | > 500 MB | 触发 checkpoint |
| 单表行数 (decode_logs) | > 100k | > 500k | 归档到 R2 + TRUNCATE |

### 1.3 Prometheus 指标

在 `prometheus_exporter.py` 中添加:

```python
# R39 P2-7: SQLite 容量指标
sqlite_db_size_bytes{db="cache_store"} 524288000
sqlite_db_size_bytes{db="relay_pool"} 209715200
sqlite_freelist_pages{db="cache_store"} 12000
sqlite_total_pages{db="cache_store"} 128000
sqlite_wal_size_bytes{db="cache_store"} 52428800

# 告警规则
# - sqlite_db_size_bytes > 1GB → warning
# - sqlite_freelist_pages / sqlite_total_pages > 0.4 → warning(需 VACUUM)
# - sqlite_wal_size_bytes > 500MB → warning(需 checkpoint)
```

---

## 2. VACUUM 策略

### 2.1 VACUUM vs VACUUM INTO

| 命令 | 锁级别 | 用途 | 频率 |
| ---- | ------ | ---- | ---- |
| `VACUUM` | 排他锁(阻塞写入) | 重建数据库文件,回收空闲页 | 每周一次(低峰期) |
| `PRAGMA incremental_vacuum` | 增量,可中断 | 配合 `auto_vacuum=INCREMENTAL` | 每日一次 |
| `VACUUM INTO 'backup.db'` | 无锁(只读副本) | 创建压缩副本 | 备份时使用 |

### 2.2 自动 VACUUM 配置

```sql
-- 启用增量 auto_vacuum(避免全量 VACUUM 阻塞写入)
PRAGMA auto_vacuum = INCREMENTAL;  -- 必须在数据库创建时设置

-- 设置空闲页阈值,达到后自动 incremental_vacuum
PRAGMA wal_autocheckpoint = 1000;  -- 每 1000 页自动 checkpoint
```

### 2.3 定时 VACUUM 任务

```python
# services/db_maintenance.py (建议实现)
import asyncio
import sqlite3
from loguru import logger

async def vacuum_cache_store(db_path: str) -> None:
    """R39 P2-7: 定时 VACUUM cache_store.db(低峰期执行)。

    VACUUM 会阻塞写入,建议在凌晨 4 点执行(用户活动最低)。
    """
    logger.info(f"[DB-Maintenance] R39 P2-7: 开始 VACUUM {db_path}")
    try:
        # 在独立连接中执行(避免干扰运行时连接)
        conn = sqlite3.connect(db_path)
        conn.execute("VACUUM")
        conn.close()
        logger.info(f"[DB-Maintenance] R39 P2-7: VACUUM 完成 {db_path}")
    except Exception as e:
        logger.error(f"[DB-Maintenance] R39 P2-7: VACUUM 失败: {e}")


async def db_maintenance_loop() -> None:
    """R39 P2-7: 每日凌晨 4 点执行 VACUUM。"""
    while True:
        try:
            # 等待到凌晨 4 点
            now = datetime.now()
            next_run = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run + timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            # 执行 VACUUM
            await vacuum_cache_store("cache_store.db")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[DB-Maintenance] R39 P2-7: 维护循环异常: {e}")
            await asyncio.sleep(3600)
```

---

## 3. WAL Checkpoint 策略

### 3.1 WAL 模式配置

```sql
-- 启用 WAL 模式(默认已启用)
PRAGMA journal_mode = WAL;

-- 自动 checkpoint 配置
PRAGMA wal_autocheckpoint = 1000;  -- 每 1000 页自动 checkpoint(默认)

-- 手动 checkpoint(在低峰期执行)
PRAGMA wal_checkpoint(TRUNCATE);  -- TRUNCATE 模式截断 WAL 文件到 0
```

### 3.2 WAL 文件监控

```bash
# 检查 WAL 文件大小
ls -lh cache_store.db-wal
ls -lh relay_pool.db-wal

# 若 WAL 文件 > 500MB,手动触发 checkpoint
sqlite3 cache_store.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 3.3 Prometheus 指标

```python
# R39 P2-7: WAL 文件大小
import os
def get_wal_size(db_path: str) -> int:
    wal_path = db_path + "-wal"
    return os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

# 指标:
# sqlite_wal_size_bytes{db="cache_store"} 52428800
# 告警: sqlite_wal_size_bytes > 500MB → warning
```

---

## 4. 历史数据 TTL 与归档

### 4.1 数据保留策略

| 表 | 保留期 | 归档目标 | 清理方式 |
| ---- | ------ | -------- | -------- |
| `decode_logs` | 30 天 | R2 (gzip JSON) | 每日归档 + DELETE |
| `jobs` (已完成) | 7 天 | R2 (gzip JSON) | 每日归档 + DELETE |
| `pending_uploads` | 1 天 | 不归档(瞬时状态) | 每小时 DELETE |
| `rotate_log` | 90 天 | R2 (gzip JSON) | 每月归档 + DELETE |
| `delivery_receipts` | 90 天 | R2 (gzip JSON) | 每月归档 + DELETE |
| `dirty_outbox` (已同步) | 7 天 | 不归档 | 每日 DELETE WHERE crdb_synced=1 |
| `kv_store` | 永久 | 不归档 | 不清理(配置数据) |
| `file_records_local` | 永久 | 不归档 | 仅软删除(deleted_at) |

### 4.2 归档脚本

```python
# services/db_archive.py (建议实现)
async def archive_old_data(table: str, days: int) -> int:
    """R39 P2-7: 归档 N 天前的数据到 R2 并删除。

    Returns:
        归档并删除的行数
    """
    # 1. 查询 N 天前的数据
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = await store.query_old_rows(table, cutoff, limit=10000)

    # 2. 压缩 + 上传到 R2
    if rows:
        payload = gzip.compress(json.dumps(rows).encode())
        key = f"archive/{table}/{datetime.utcnow():%Y%m%d_%H%M%S}.json.gz"
        await r2_storage.upload(key, payload, "application/gzip")

    # 3. 删除已归档的行
    deleted = await store.delete_old_rows(table, cutoff, limit=10000)

    logger.info(f"[DB-Archive] R39 P2-7: {table} 归档 {deleted} 行")
    return deleted
```

### 4.3 定时归档任务

```python
async def archive_loop() -> None:
    """R39 P2-7: 每日凌晨 3 点执行归档(在 VACUUM 之前)。"""
    while True:
        await asyncio.sleep(86400)  # 24 小时
        await archive_old_data("decode_logs", days=30)
        await archive_old_data("jobs", days=7, where="status='completed'")
        await archive_old_data("pending_uploads", days=1)
```

---

## 5. CRDB 容量与水位

### 5.1 Zone Config TTL

```sql
-- R39 P2-7: 为 CRDB 表配置 TTL(自动删除过期行)
ALTER TABLE decode_logs CONFIGURE ZONE USING
    ttl_expire_after = '30 days';

ALTER TABLE jobs CONFIGURE ZONE USING
    ttl_expire_after = '7 days',
    ttl_expiration_expression = "CASE WHEN status = 'completed' THEN clock_timestamp() END";

-- 注意: TTL 会在每行超过 expire 时间后由 CRDB 自动删除(无需手动 DELETE)
```

### 5.2 存储水位监控

```sql
-- 查询 CRDB 节点存储使用情况
SELECT
    node_id,
    round(disk_used_bytes / 1024 / 1024 / 1024, 2) AS used_gb,
    round(disk_total_bytes / 1024 / 1024 / 1024, 2) AS total_gb,
    round(disk_used_bytes / disk_total_bytes * 100, 2) AS used_pct
FROM crdb_internal.node_metrics
ORDER BY used_pct DESC;
```

水位阈值:
- `used_pct > 70%` → warning(扩容)
- `used_pct > 85%` → critical(紧急扩容 + 清理)

### 5.3 Range 数量监控

```sql
-- 查询表 range 数量(过多影响性能)
SELECT table_name, range_count
FROM crdb_internal.table_ranges
ORDER BY range_count DESC
LIMIT 10;
```

---

## 6. 索引维护

### 6.1 SQLite 索引碎片

```sql
-- 检查索引碎片化(SQLITE_STAT1 表)
ANALYZE;  -- 更新统计信息
SELECT * FROM sqlite_stat1 WHERE tbl = 'jobs';

-- 重建索引(低峰期)
REINDEX;

-- 重建单个索引
REINDEX idx_delivery_receipts_token;
```

### 6.2 CRDB 索引重建

```sql
-- CRDB 不需要 REINDEX(自动维护)
-- 但可检查索引使用率
SELECT
    index_name,
    total_reads,
    total_writes
FROM crdb_internal.index_usage_statistics
ORDER BY total_reads DESC;
```

---

## 7. 部署

### 7.1 systemd 集成

创建独立的数据库维护服务:

```ini
# /etc/systemd/system/tgjiema-db-maintenance.service
[Unit]
Description=TGJiema DB Maintenance (VACUUM + Archive)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m services.db_maintenance
Restart=on-failure
RestartSec=60
TimeoutStopSec=40
KillSignal=SIGTERM
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

### 7.2 Cron 备用方案

```cron
# /etc/cron.d/tgjiema-db-maintenance
# R39 P2-7: 每日凌晨 4 点 VACUUM
0 4 * * * tgjiema sqlite3 /opt/tgjiema/cache_store.db "VACUUM;"
0 5 * * * tgjiema sqlite3 /opt/tgjiema/relay_pool.db "VACUUM;"
```

---

## 8. 验证

```bash
# 1. 检查 SQLite 文件大小
ls -lh cache_store.db relay_pool.db

# 2. 检查空闲页占比
sqlite3 cache_store.db "SELECT freelist_count, page_count, round(freelist_count * 100.0 / page_count, 2) FROM pragma_freelist_count, pragma_page_count;"

# 3. 检查 WAL 文件
ls -lh cache_store.db-wal

# 4. 手动触发 VACUUM
sqlite3 cache_store.db "VACUUM;"

# 5. 手动触发 checkpoint
sqlite3 cache_store.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 6. Prometheus 指标
curl http://localhost:9100/metrics | grep sqlite_
```

---

## 9. 相关文件

- `database/cache_store.py` — SQLite 权威存储
- `database/relay_db.py` — relay SQLite
- `services/prometheus_exporter.py` — 容量指标暴露
- `services/db_backup.py` — R2 备份(可复用归档逻辑)
- `docs/observability.md` — 监控指标总览
