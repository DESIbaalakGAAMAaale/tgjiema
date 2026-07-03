# tgjiema CockroachDB RU 消耗排查与优化方案

<aside>
🚀

面向 tgjiema **正式上线**的 RU 优化落地方案。核心：以「推送代替轮询、批量代替 N+1、热读走缓存」三个架构改造为主，配合参数治理与监控，把后台底噪 RU 压到最低。下面每一项都附可直接参考的改造代码。

</aside>

## 一、结论先行

- 「空闲还掉 RU」的本质：多个 bot 进程的定时任务（约每 5 分钟）持续轮询 CRDB，还叠加逐条 UPDATE 与可能的缓存穿透。
- 生产环境不能靠「停进程」降 RU，只能靠架构：**推送 > 批量 > 缓存 > 合理间隔 > 连接池**。
- 目标：30min 后台底噪从 ~5,520 RU 降到 ~200–500 RU（降 90%+），把 RU 留给真正服务用户的解码请求。

---

## 二、四项架构改造（附代码）

### 1. 连接池：全局单例，5 个 bot 共用

```python
# database/session.py
import asyncpg
from config.settings import settings

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.COCKROACHDB_URL,
            min_size=1,              # 空闲时只保 1 条，减少保活开销
            max_size=10,             # 按真实并发调
            max_inactive_connection_lifetime=30,  # 空闲 30s 回收
            command_timeout=15,
            server_settings={"statement_timeout": "15000"},
        )
    return _pool
```

要点：绝不要每个 bot / 每次查询各开连接；`min_size` 小、空闲连接快回收；关掉框架的高频 `SELECT 1` 心跳。

### 2. request_count 批量刷写（逐条 → 一次往返）

```python
# database/cache.py — 用 unnest 一条 SQL 完成 N 个增量更新
async def flush_request_counts(counts: dict[str, int]):
    if not counts:
        return
    codes = list(counts.keys())
    deltas = [counts[c] for c in codes]
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE file_records AS f
        SET request_count = f.request_count + d.delta
        FROM unnest($1::text[], $2::bigint[]) AS d(code, delta)
        WHERE f.code = d.code
        """,
        codes, deltas,
    )
```

无论 5 分钟内积了 10 个还是 500 个码，都只是 **1 次 round-trip**。

### 3. code_changes 批量更新（多字段）

```python
# bots/idx_bot.py — 多字段一次性回写
async def sync_code_changes(changes: list[dict]):
    if not changes:
        return
    codes    = [c["code"]         for c in changes]
    notes    = [c.get("note")    for c in changes]
    expiries = [c.get("expiry")  for c in changes]
    statuses = [c.get("status")  for c in changes]
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE codes AS c
        SET note = u.note, expiry = u.expiry, status = u.status
        FROM unnest($1::text[], $2::text[], $3::timestamptz[], $4::text[])
             AS u(code, note, expiry, status)
        WHERE c.code = u.code
        """,
        codes, notes, expiries, statuses,
    )
```

### 4. job 分发：CHANGEFEED 推送代替轮询

```sql
-- 用 core changefeed 监听 pending job，由单个消费进程接收
EXPERIMENTAL CHANGEFEED FOR jobs WHERE status = 'pending' WITH updated;
```

消费端收到变更后写本地 SQLite 队列，**彻底去掉** `sync_jobs_from_crdb_to_sqlite` 的周期性空转轮询。这是生产环境下持续 RU 下降最大的一项。

### 5. 热读全走缓存

`file_records` / `codes` / `cells` 的高频读走 L1（内存）+ L2（SQLite），CRDB 只做持久化与跨进程同步。上线前先用日志确认 `get_file_record` / `get_code_entry` / `get_cell_by_key` 命中率 > 95%，避免缓存穿透把读直接打到 CRDB（RU 放大的首要嫌疑）。

---

## 三、正确性修复（生产不能带 bug）

### upsert bug

```python
# ❌ 错误：update_one 不支持 upsert 参数 → TypeError
await col.update_one({"cell_key": k}, {"$set": v}, upsert=True)

# ✅ 修复：批量 INSERT ... ON CONFLICT
await pool.execute(
    """
    INSERT INTO cells (cell_key, status, degrade_count, next_active_chat_id)
    SELECT * FROM unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[])
    ON CONFLICT (cell_key) DO UPDATE
    SET status = EXCLUDED.status,
        degrade_count = EXCLUDED.degrade_count,
        next_active_chat_id = EXCLUDED.next_active_chat_id
    """,
    keys, statuses, degrade_counts, next_ids,
)
```

### ban/degrade 去抖

现有 `FAIL_STREAK_DEGRADE_THRESHOLD = 3` 已是一层防护；再加一道「疑似→确认」二次校验，避免 Telegram 临时报错触发 `log_rotate` INSERT → 查 `spare_pool` → `consume_spare` UPDATE 的级联写风暴（既降 RU 又防误降级）。

---

## 四、生产参数配置

| 参数 | 生产建议值 | 说明 |
| --- | --- | --- |
| `QUOTA_SYNC_INTERVAL` | 300～600 | 配额实时性与 RU 权衡 |
| `request_count` 刷写 | 300～600s + 批量 | 批量后频率影响很小 |
| `code_changes` 同步 | 300s + 批量 | 下架/备注需较快生效 |
| dsp job 分发 | CHANGEFEED / 事件驱动 | 取代 120s 轮询 |
| `CRDB_CLEANUP_CRON_HOURS` | 6～12 | 控制单次 DELETE 量 |
| `DB_BACKUP_ENABLED` | true | 生产必须开备份（配 R2） |
| `DB_BACKUP_INTERVAL_MINUTES` | 360 | 每 6h 或按 RPO 需求 |
| `MON_CHECK_INTERVAL` | 60（保持） | 走内存/SQLite，零 RU |

---

## 五、CRDB 侧 / 成本 / 监控

- **auto stats**：保留（对查询计划重要），仅对极小、低变更表适当降频，不建议关闭。
- **TTL job**：确认 `admin/migrations/disable_crdb_ttl.sql` 已执行，用应用层批量 DELETE 替代原生 TTL。
- **索引**：`code`、`status`、`cell_key` 等高频查询列建索引，避免全表扫描。
- **成本**：上线后采样估算月 RU vs 免费额度，在 Console 设 spend limit + 用量告警。
- **监控**：Statements/SQL Activity 看 TOP SQL；埋点缓存命中率、各 loop 单次条数、降级频率；告警 RU 阈值与降级风暴。

---

## 六、上线前检查清单

- [ ]  5 个 bot 共用全局连接池（min=1, max=10, 空闲回收）
- [ ]  `request_count` / `code_changes` / `dirty_cells` 全部改 unnest 批量写
- [ ]  job 分发改 CHANGEFEED，删除 `sync_jobs_from_crdb_to_sqlite` 轮询
- [ ]  修 `sync_dirty_cells_to_crdb` 的 upsert bug（改 ON CONFLICT）
- [ ]  ban/degrade 加二次确认去抖
- [ ]  热读命中率验证 > 95%，无缓存穿透
- [ ]  轮询间隔设为生产值（见第四节）
- [ ]  `DB_BACKUP_ENABLED=true` 并配 R2 凭证
- [ ]  确认 CRDB TTL 已禁用、高频查询有索引
- [ ]  Console 设 spend limit + 告警，估算月 RU
- [ ]  部署监控：TOP SQL、缓存命中率、降级风暴告警

---

## 七、预期效果

| 指标 | 当前 | 批量+间隔后 | CHANGEFEED+缓存后 |
| --- | --- | --- | --- |
| 30min 后台 RU | ~5,520 | ~1,000–1,500 | ~200–500 |
| RU/min 均值 | ~184 | ~33–50 | ~7–17 |

> 以上为后台底噪；生产真实 RU = 底噪 + 用户解码请求。目标是把底噪压到最低。
>