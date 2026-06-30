# CRDB RU 消耗优化完整实施计划

> 创建日期: 2026-06-30
> 目标: 降低 CockroachDB RU 消耗，提升免费套餐日活承载力
> 原则: 只做缓存层优化和本地队列改造，不改业务逻辑

---

## 〇、执行摘要

| 项目 | 数值 |
|------|------|
| 当前月 RU 消耗 | ~4500 万（接近免费套餐 5000 万上限） |
| 当前日活承载 | ~1 万 |
| 优化后月 RU 消耗 | ~1200 万（节省 73%） |
| 优化后日活承载 | ~3 万+ |
| 改动文件总数 | 12 个 |
| 实施工时 | 6-10 小时 |

---

## 一、背景

### 1.1 CRDB 免费套餐限制

| 指标 | 免费额度 | 当前月消耗 | 余量 |
|------|---------|-----------|------|
| 存储 | 10 GiB | ~1 GB | 充裕 |
| RU 配额 | 5000 万 | ~4500 万 | 紧张 |
| 突发 RU/sec | 3 万 | 峰值 ~500 | 充裕 |

### 1.2 RU 消耗热点

| 排名 | 模块 | 每分钟 RU | 占比 |
|------|------|----------|------|
| 1 | Dsp Bot dequeue_jobs CTE | ~2400 | 70% |
| 2 | Idx Bot 用户/文件查询（绕过缓存） | ~80 | 2% |
| 3 | Admin Bot 统计/管理 | ~10 | 0.3% |
| 4 | Mon Bot cells 轮询 | ~2 | 0.05% |
| 5 | Up Bot 启动查询 | ~1 | 0.03% |
| 其他 | 各类配置/日志/事件查询 | ~2007 | 28% |

### 1.3 现有缓存架构

| 层级 | 介质 | 覆盖范围 | 状态 |
|------|------|---------|------|
| L0 内存 | Python dict (QueryCache) | users/3h, file_records/5min, configs/10min, codes/7d | ✅ 已实现 |
| L1 SQLite | cache_store.db | 内存缓存持久化备份 | ✅ 已实现 |
| L2 SQLite 缓冲 | cache_store.db | decode_logs/code_changes/user_quota | ✅ 已实现 |
| L3 CRDB | CockroachDB | 最终数据源 | ⚠️ 大量绕过缓存查询 |

---

## 二、方案总览

| 编号 | 方案名称 | 优先级 | 改动文件数 | 难度 | 节省 RU/分钟 |
|------|---------|--------|-----------|------|-------------|
| **D** | SQLite 本地任务队列 | P0 | 3 | 中 | **~1878** |
| **A1** | 修复 get_or_create_user 缓存绕过 | P0 | 2 | 低 | **~60** |
| **A2** | 修复 file_records 缓存绕过 | P1 | 3 | 低 | **~10** |
| **B1** | code_cache TTL 优化 + invalidate | P1 | 2 | 中 | **~5** |
| **C1** | 负缓存防穿透 | P1 | 2 | 中 | **~5** |
| **B3** | delivery_resolver TTL 对齐 | P2 | 1 | 低 | **~3** |
| **B5** | QueryCache 懒清理 | P2 | 1 | 低 | **~3** |
| **E1** | cells 跨进程共享 SQLite 缓存 | P2 | 4 | 中 | **~1** |
| **E2** | count_documents 内存计数 | P2 | 2 | 中 | **~5** |
| **E7** | 用户码列表缓存 | P2 | 2 | 中 | **~0.5** |
| **合计** | | | | | **~1970** |

---

## 三、方案 D: SQLite 本地任务队列（核心）

### 3.1 当前问题

`bots/dsp_bot.py:253` — 4 个 Worker 直接调 `dequeue_jobs(10)` 从 CRDB 原子取任务。

`database/session.py:1395-1405` — CTE 原子操作：

```sql
WITH next AS (
    SELECT id FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 10
)
UPDATE jobs SET status='dispatched' WHERE id IN (SELECT id FROM next)
RETURNING *;
```

| 指标 | 数值 |
|------|------|
| 单次 RU | ~30 |
| 4 Worker 合计 | 80 次/分钟 |
| 每分钟 RU | **~2400** |
| 占总 RU 比例 | **~70%** |

### 3.2 改造方案

#### 3.2.1 新增 SQLite 表

**文件**: `database/cache_store.py` — 在 `CacheStore.init()` 方法中新增 DDL

```sql
CREATE TABLE IF NOT EXISTS local_job_queue (
    crdb_id   INTEGER PRIMARY KEY,
    code      TEXT NOT NULL,
    target_user_id INTEGER NOT NULL,
    storage_channel_id INTEGER NOT NULL,
    storage_msg_ids TEXT,
    batch_file_meta TEXT,
    task_type TEXT DEFAULT 'single',
    status    TEXT DEFAULT 'pending',   -- pending / dispatched / done / retried / dead
    retry_count INTEGER DEFAULT 0,
    protect_content INTEGER DEFAULT 0,
    created_at TEXT,
    dispatched_at TEXT,
    dead_reason TEXT,
    synced_at REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_local_queue_status
ON local_job_queue(status, created_at);
```

#### 3.2.2 CacheStore 新增方法

**文件**: `database/cache_store.py` — `CacheStore` 类中新增

```python
async def upsert_jobs_batch(self, jobs: list[dict]):
    """批量 UPSERT jobs 到本地队列（Queue Syncer 用）"""

async def get_local_pending_jobs(self, limit: int = 10) -> list[dict]:
    """取本地 pending 的 job（Worker 用）"""

async def mark_local_job_dispatched(self, crdb_id: int):
    """标记 job 为 dispatched（Worker 拿任务后）"""

async def update_local_job(self, crdb_id: int, status: str, **kwargs):
    """更新本地 job 状态（Worker 发送成功/失败后）"""

async def get_local_unsynced_jobs(self) -> list[dict]:
    """获取需要回写 CRDB 的 job（Sync Back 用）"""

async def mark_local_job_synced(self, crdb_id: int):
    """标记已同步（Sync Back 完成后）"""

async def delete_local_job(self, crdb_id: int):
    """删除本地 job 记录（发送成功后）"""

async def cleanup_local_queue(self, max_age_days: int = 7):
    """清理超过 7 天的记录"""
```

#### 3.2.3 Queue Syncer 协程

**文件**: `database/session.py` — 新增函数

```python
async def sync_pending_jobs_to_sqlite(batch_size: int = 100) -> list[dict]:
    """从 CRDB 拉取 pending jobs 并写入本地 SQLite（每 5 秒执行一次）"""
    col = get_jobs_col()
    rows = await col._query("""
        WITH batch AS (
            SELECT * FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT $1
        ),
        updated AS (
            UPDATE jobs SET status = 'dispatched'
            WHERE id IN (SELECT id FROM batch)
            RETURNING *
        )
        SELECT * FROM updated
    """, [batch_size])
    return list(rows) if rows else []


async def queue_syncer_loop():
    """每 5 秒从 CRDB 同步 pending jobs 到本地 SQLite"""
    from .cache_store import get_cache_store
    from loguru import logger
    while True:
        try:
            jobs = await sync_pending_jobs_to_sqlite(batch_size=100)
            if jobs:
                store = get_cache_store()
                await store.upsert_jobs_batch(jobs)
                logger.debug(f"[QueueSyncer] 同步 {len(jobs)} 个 job 到本地")
        except Exception as e:
            logger.error(f"[QueueSyncer] 同步异常: {e}")
        await asyncio.sleep(5)
```

**RU 消耗**: 12 次/分钟 × ~50 RU = **~600 RU/分钟**

#### 3.2.4 Worker 改造

**文件**: `bots/dsp_bot.py` — `_dsp_worker()` 函数

**改造前**（第 253 行）:
```python
local_queue = await dequeue_jobs(10)
```

**改造后**:
```python
from database.cache_store import get_cache_store
store = get_cache_store()
local_queue = await store.get_local_pending_jobs(10)
```

**处理流程**:

```
1. 取 job: store.get_local_pending_jobs(10)
2. 标记: store.mark_local_job_dispatched(crdb_id)
3. 发送文件（不变）
4. 成功: store.delete_local_job(crdb_id)
5. 失败: store.update_local_job(crdb_id, 'retried', retry_count=retry_count+1)
6. 死信: store.update_local_job(crdb_id, 'dead', dead_reason='...')
```

#### 3.2.5 Sync Back 协程

**文件**: `database/session.py` — 新增函数

```python
async def sync_local_job_status_to_crdb():
    """每 30 秒将本地未同步状态回写 CRDB"""
    from .cache_store import get_cache_store
    from loguru import logger
    from .session import reenqueue_job, mark_job_dead
    
    store = get_cache_store()
    
    # 1. 回写 retried job
    for job in await store.get_local_unsynced_jobs(status='retried'):
        try:
            await reenqueue_job(job['crdb_id'])
            await store.mark_local_job_synced(job['crdb_id'])
            await store.delete_local_job(job['crdb_id'])
        except Exception as e:
            logger.error(f"[SyncBack] retried job {job['crdb_id']} 同步失败: {e}")
    
    # 2. 回写 dead job
    for job in await store.get_local_unsynced_jobs(status='dead'):
        try:
            await mark_job_dead(job['crdb_id'], job.get('dead_reason', ''))
            await store.mark_local_job_synced(job['crdb_id'])
            await store.delete_local_job(job['crdb_id'])
        except Exception as e:
            logger.error(f"[SyncBack] dead job {job['crdb_id']} 同步失败: {e}")


async def sync_back_to_crdb_loop():
    """每 30 秒执行 sync_local_job_status_to_crdb"""
    while True:
        try:
            await sync_local_job_status_to_crdb()
        except Exception as e:
            from loguru import logger
            logger.error(f"[SyncBackLoop] 异常: {e}")
        await asyncio.sleep(30)
```

**RU 消耗**: 2 次/分钟 × ~10 RU = **~20 RU/分钟**

#### 3.2.6 死信重试兼容

**文件**: `bots/dsp_bot.py:95-105` — `_retry_dead_jobs()`

保持不变（仍走 CRDB `get_and_reset_dead_jobs()`），因为 Sync Back 已把 dead 状态回写到 CRDB。

#### 3.2.7 动态限速兼容

**文件**: `bots/dsp_bot.py:263` — `dynamic_rate_limiter.acquire(get_pending_jobs_count)`

**新增函数**:

```python
_pending_count_cache = {"count": 0, "ts": 0}

async def get_pending_jobs_count_local() -> int:
    """从本地 SQLite 查 pending job 数量（零 CRDB RU）"""
    from database.cache_store import get_cache_store
    store = get_cache_store()
    rows = await store._db.execute_fetchall(
        "SELECT COUNT(*) FROM local_job_queue WHERE status='pending'"
    )
    return rows[0][0] if rows else 0


async def get_pending_jobs_count_cached() -> int:
    """带 5 秒缓存的 pending jobs 数量"""
    now = time.time()
    if now - _pending_count_cache["ts"] < 5:
        return _pending_count_cache["count"]
    count = await get_pending_jobs_count_local()
    _pending_count_cache["count"] = count
    _pending_count_cache["ts"] = now
    return count
```

**修改 `_dsp_worker()` 第 263 行**:
```python
# 改造前
await dynamic_rate_limiter.acquire(get_pending_jobs_count)

# 改造后
await dynamic_rate_limiter.acquire(get_pending_jobs_count_cached)
```

#### 3.2.8 Dsp Bot 启动变更

**文件**: `bots/dsp_bot.py` — `_async_main()` 函数

新增两个协程:
```python
from database.session import queue_syncer_loop, sync_back_to_crdb_loop
create_safe_task(queue_syncer_loop(), name="queue-syncer")
create_safe_task(sync_back_to_crdb_loop(), name="sync-back")
```

#### 3.2.9 数据库导出更新

**文件**: `database/__init__.py` — 新增导出

```python
from .session import (
    ...
    queue_syncer_loop,
    sync_back_to_crdb_loop,
    sync_local_job_status_to_crdb,
    get_pending_jobs_count_local,
    get_pending_jobs_count_cached,
)
```

### 3.3 D 方案安全分析

#### 核心原则

**CRDB 的 jobs 表是唯一权威数据源。SQLite 只是本地处理缓存。**

#### 数据丢失风险分析

| 场景 | 是否丢数据 | 后果 | 严重程度 |
|------|-----------|------|---------|
| 正常流程 | ❌ | 无 | 无 |
| Dsp Bot 崩溃 | ❌ | 极小概率重复发送 | 可接受 |
| 电脑断电 | ❌ | 任务从 CRDB 恢复 | 无 |
| CRDB 断网 | ❌ | 延迟同步 | 无 |
| 文件本身 | ❌ | 文件在 Telegram 频道 | 无 |
| 用户数据 | ❌ | users 表在 CRDB | 无 |
| job 任务 | ❌ | CRDB 是权威源 | 无 |

### 3.4 D 方案 RU 节省

| 指标 | 当前 | 改造后 | 节省 |
|------|------|--------|------|
| 每分钟 RU | ~2400 | ~620 | **75%** |
| 每月 RU | ~100 万 | ~27 万 | **73 万** |

---

## 四、方案 A1: 修复 get_or_create_user 缓存绕过

### 4.1 当前问题

`services/permission.py:64-82` — `get_or_create_user()` 直接查 CRDB，绕过了三级缓存 `get_user_cached()`（TTL 3 小时）。

**调用位置**:

| 文件 | 行号 | 调用场景 | 频率 |
|------|------|---------|------|
| `bots/idx_bot.py` | 344 | `/start` 欢迎 | ~10/分钟 |
| `bots/idx_bot.py` | 385 | `/status` 查状态 | ~5/分钟 |
| `bots/idx_bot.py` | 632 | 解码时查用户 | ~20/分钟 |

### 4.2 改造方案

**文件**: `services/permission.py:64-82`

```python
async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    from database.cache import get_user_cache
    
    # 先走三级缓存
    user = await get_user_cached(user_id)
    if user is not None:
        return user
    
    # 缓存未命中，创建新用户
    user = make_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        membership_level="free",
        daily_decode_quota=settings.FREE_DAILY_QUOTA,
        external_decode_quota=settings.FREE_EXTERNAL_DAILY_QUOTA,
    )
    col = get_users_col()
    try:
        await col.insert_one(user)
        # 插入成功才写缓存
        get_user_cache().set(f"user:{user_id}", user)
    except Exception:
        # 并发插入冲突，重新查一次
        user = await get_user_cached(user_id) or await col.find_one({"user_id": user_id})
        if not user:
            raise
    return user
```

### 4.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 最坏后果 | 用户改名后 3 小时内看到旧名字 |
| 风险等级 | 无风险 |

### 4.4 节省

约 **~60 RU/分钟**（~1620 万 RU/月）

---

## 五、方案 A2: 修复 file_records 缓存绕过

### 5.1 当前问题

7 处代码直接调 `files_col.find_one()` 绕过三级缓存 `get_file_record_cached()`（TTL 5 分钟）。

### 5.2 改造内容

全部替换为 `await get_file_record_cached(file_code)`:

| 文件 | 行号 | 场景 |
|------|------|------|
| `bots/idx_bot.py` | 239 | 中继媒体组处理 |
| `bots/idx_bot.py` | 299 | 中继 RELAY_DELIVER |
| `bots/idx_bot.py` | 316 | 中继 RELAY_DELIVER 另一分支 |
| `bots/idx_bot.py` | 1460 | 外部码映射处理 |
| `bots/idx_bot.py` | 1582 | 中继 RELAY_FILE 媒体 |
| `bots/dsp_bot.py` | 570 | 举报回调 |
| `bots/admin_bot/handlers.py` | 240 | 管理员文件详情 |

**改动示例**:

```python
# 改造前
files_col = get_file_records_col()
record = await files_col.find_one({"file_code": code})

# 改造后
record = await get_file_record_cached(code)
```

**注意**: 需要在文件顶部确保导入 `get_file_record_cached`：

```python
from database import get_file_record_cached  # 添加
```

### 5.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 最坏后果 | 文件状态变更后 5 分钟内缓存生效 |
| 已有兜底 | 修改操作已有 `update_file_record_and_invalidate()` |

### 5.4 节省

约 **~10 RU/分钟**（~50 万 RU/月）

---

## 六、方案 B1: code_cache TTL 优化

### 6.1 当前问题

`database/cache.py:70` — `_code_cache` TTL 604800 秒（7 天），导致管理员删除/下架文件码后，用户最长 7 天内仍可解码（幽灵解码）。

### 6.2 改造方案

#### 6.2.1 修改 TTL

**文件**: `database/cache.py:70`

```python
# 改造前
_code_cache = QueryCache(max_size=5000, ttl_seconds=604800)

# 改造后
_code_cache = QueryCache(max_size=5000, ttl_seconds=3600)  # 1 小时
```

#### 6.2.2 新增失效函数

**文件**: `database/cache.py` — 在 `get_code_cache()` 后新增

```python
def invalidate_code_cache(code: str):
    """失效指定文件码的缓存（管理员改码后调用）"""
    _code_cache.invalidate(f"code:{code}")
```

#### 6.2.3 管理员操作加上失效调用

**文件**: `bots/admin_bot/handlers.py`

在所有修改 codes 表的函数末尾加 `invalidate_code_cache(file_code)`：

| 操作 | 函数（需逐个确认） | 位置 |
|------|-------------------|------|
| 下架文件码 | `cmd_detach` 等 | 末尾加 |
| 删除文件码 | `cmd_delete` 等 | 末尾加 |
| 修改状态 | `cmd_set_status` 等 | 末尾加 |
| 修改过期 | `cmd_set_expiry` 等 | 末尾加 |

**改动示例**:
```python
# 改造前
async def cmd_detach(update, context, file_code):
    # ... 修改 codes 表 ...
    await col.update_one(...)

# 改造后
async def cmd_detach(update, context, file_code):
    # ... 修改 codes 表 ...
    await col.update_one(...)
    from database.cache import invalidate_code_cache
    invalidate_code_cache(file_code)
```

### 6.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 实际效果 | 减少缓存不一致窗口（7天→1小时） |
| 风险 | 如果漏加 invalidate，1 小时内旧缓存仍在 |

### 6.4 节省

约 **~5 RU/分钟**

---

## 七、方案 C1: 负缓存防穿透

### 7.1 当前问题

查询不存在的 user_id 或 file_code 时，每次都穿透到 CRDB，无法被缓存拦截。

### 7.2 改造方案

#### 7.2.1 新增负缓存存储

**文件**: `database/cache.py` — 全局变量

```python
# 负缓存：缓存"不存在"的结果，TTL 60 秒，防止恶意探测穿透 CRDB
_negative_user_cache: dict[int, float] = {}   # user_id -> expired_at
_negative_file_cache: dict[str, float] = {}   # file_code -> expired_at
```

#### 7.2.2 新增失效函数

```python
def invalidate_negative_user_cache(user_id: int):
    """创建/修改用户后清除负缓存"""
    _negative_user_cache.pop(user_id, None)

def invalidate_negative_file_cache(file_code: str):
    """创建文件码后清除负缓存"""
    _negative_file_cache.pop(file_code, None)
```

#### 7.2.3 在 get_user_cached 中使用

**文件**: `database/session.py:1045-1070`

```python
async def get_user_cached(user_id: int) -> Optional[dict]:
    from .cache import _negative_user_cache, invalidate_negative_user_cache
    
    # 检查负缓存
    now = time.time()
    if user_id in _negative_user_cache:
        if now < _negative_user_cache[user_id]:
            return None
        del _negative_user_cache[user_id]
    
    # L1: 内存缓存
    cache = get_user_cache()
    cache_key = f"user:{user_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    # L2: SQLite 兜底
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)
        return cached
    
    # CRDB
    col = get_users_col()
    user = await col.find_one({"user_id": user_id})
    
    if user:
        cache.set(cache_key, user)
        await store.set(cache_key, user)
    else:
        # 写入负缓存 60 秒
        _negative_user_cache[user_id] = time.time() + 60
    
    return user
```

#### 7.2.4 在 get_file_record_cached 中使用

**文件**: `database/session.py:1081-1106`

同理在函数开头和结尾增加负缓存逻辑（用字符串 key）。

#### 7.2.5 用户创建时失效负缓存

**文件**: `services/permission.py` — `get_or_create_user()` 插入成功后:

```python
# 插入成功才写缓存
get_user_cache().set(f"user:{user_id}", user)
# 失效负缓存
from database.cache import invalidate_negative_user_cache
invalidate_negative_user_cache(user_id)
```

### 7.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 最坏后果 | 新用户在负缓存窗口（60秒）内被误判"未注册" |
| 缓解 | 用户创建时主动 invalidate 负缓存 |

### 7.4 节省

约 **~5 RU/分钟**

---

## 八、方案 B3: delivery_resolver 缓存 TTL 对齐

### 8.1 当前问题

`storage/delivery_resolver.py:19` — 缓存 TTL 30 秒，与 Mon Bot 的 120 秒缓存不同步。

### 8.2 改造方案

**文件**: `storage/delivery_resolver.py:19`

```python
# 改造前
_CELL_CACHE_TTL: float = 30.0

# 改造后（与 Mon Bot 120 秒对齐）
_CELL_CACHE_TTL: float = 120.0
```

### 8.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 最坏后果 | 频道被封后最晚 2 分钟感知，期间 1-2 次发送失败自动重试 |

### 8.4 节省

约 **~3 RU/分钟**

---

## 九、方案 B5: QueryCache 懒清理

### 9.1 当前问题

`database/cache.py:13-20` — 每次 `get()` / `set()` 都执行 `_clean_expired()` 全量扫描过期项。对 5000 条的 `_code_cache` 是 O(N) 浪费。

### 9.2 改造方案

**文件**: `database/cache.py` — 修改 `QueryCache` 类

```python
def get(self, key: str) -> Optional[Any]:
    """获取缓存值，仅检查当前 key 是否过期"""
    if key not in self.cache:
        return None
    entry = self.cache[key]
    if time.time() - entry["ts"] >= self.ttl:
        del self.cache[key]
        return None
    self.cache.move_to_end(key)
    return entry["data"]

def set(self, key: str, data: Any):
    """设置缓存值，满时尝试淘汰少量过期项，无过期项则 LRU 弹出"""
    if len(self.cache) >= self.max_size:
        self._evict_one_or_expired()
    self.cache[key] = {"data": data, "ts": time.time()}

def _evict_one_or_expired(self):
    """满时先尝试淘汰一个过期项，无过期项才 LRU 弹出最旧"""
    now = time.time()
    for i, key in enumerate(self.cache):
        if i >= 5:
            break
        if now - self.cache[key]["ts"] >= self.ttl:
            del self.cache[key]
            return
    self.cache.popitem(last=False)
```

### 9.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 后果 | 内存中可能短暂留下过期项，下次访问时被自动清理 |

### 9.4 节省

约 **~3 RU/分钟**（间接收益）

---

## 十、方案 E1: cells 跨进程共享 SQLite 缓存

### 10.1 当前问题

```
Up Bot: 每 60 秒调用 get_active_cells() → 查 CRDB
Idx Bot: 每 60 秒调用 get_active_cells() → 查 CRDB
Mon Bot: 每 120 秒查 cells → 查 CRDB
Dsp Bot: 4 worker 偶发查 cells → 查 CRDB
```

每个进程独立查同一份几乎不变的数据。

### 10.2 改造方案

#### 10.2.1 新增 SQLite 表

**文件**: `database/cache_store.py`

```sql
CREATE TABLE IF NOT EXISTS cells_snapshot (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- 严格只存 1 行
    data TEXT NOT NULL,        -- JSON 序列化的 cells 列表
    updated_at REAL NOT NULL,
    version INTEGER NOT NULL   -- 单调递增版本号
);

CREATE TABLE IF NOT EXISTS cells_change_notify (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    ts REAL NOT NULL
);
```

#### 10.2.2 CacheStore 新增方法

```python
async def save_cells_snapshot(self, cells: list[dict], version: int):
    """保存 cells 全量快照（Mon Bot 用）"""

async def load_cells_snapshot(self) -> tuple[list[dict], int] | tuple[None, 0]:
    """加载 cells 快照（其他 Bot 启动用）"""

async def notify_cells_change(self, version: int):
    """通知 cells 变更（Mon Bot 用）"""

async def has_cells_change(self) -> tuple[bool, int]:
    """检查是否有 cells 变更通知（其他 Bot 用）"""
```

#### 10.2.3 Mon Bot 改造

**文件**: `bots/mon_bot.py`

在 cells 变更时（`set_cell_status` 调用后）增加：

```python
# 已有: col.update_one(...)
# 新增:
from database.cache_store import get_cache_store
store = get_cache_store()
new_version = int(time.time())
all_cells = await col.find({})
await store.save_cells_snapshot(list(all_cells), new_version)
await store.notify_cells_change(new_version)
```

#### 10.2.4 其他 Bot 改造

**文件**: `bots/up_bot.py` / `bots/idx_bot.py` / `bots/dsp_bot.py`

启动时从 SQLite 加载 cells 快照：

```python
# 启动时
store = get_cache_store()
cells, version = await store.load_cells_snapshot()
if cells:
    _active_a_slots_cache = cells
```

定时任务改为查 SQLite 通知 + 重新加载：

```python
# 改造前
while True:
    await _refresh_active_slots()  # 查 CRDB
    await asyncio.sleep(60)

# 改造后
while True:
    has_change, version = await store.has_cells_change()
    if has_change:
        cells, _ = await store.load_cells_snapshot()
        _active_a_slots_cache = cells
    await asyncio.sleep(30)
```

#### 10.2.5 get_active_cells() 改造

**文件**: `database/session.py:1270-1295`

```python
async def get_active_cells() -> list[dict]:
    """优先从 SQLite 快照加载，CRDB 兜底"""
    from .cache_store import get_cache_store
    store = get_cache_store()
    cells, _ = await store.load_cells_snapshot()
    if cells is not None:
        return [c for c in cells if c.get("status") == "active"]
    # CRDB 兜底
    col = get_cells_col()
    return await col.find({"status": "active"})
```

### 10.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 最坏后果 | 启动时 SQLite 为空，回退 CRDB |
| 一致性 | cells 变更后最多 30 秒内各进程感知 |

### 10.4 节省

约 **~1 RU/分钟**（多进程重复查询消除）

---

## 十一、方案 E2: count_documents 内存计数

### 11.1 当前问题

`admin_bot/display.py:40-44` 在 `/status` 一次调用 4 个 count_documents。
`idx_bot.py:846` 在 `/my_codes` 查用户总记录数。

### 11.2 改造方案

#### 11.2.1 全局计数器增强

**文件**: `utils/shared_counters.py`

增加本地增量维护：

```python
# 已有的 status_counters 字典
# 新增: 增量计数

_user_code_count_delta: dict[int, int] = {}  # user_id -> 本地未同步增量

def incr_user_code_count(user_id: int, delta: int = 1):
    _user_code_count_delta[user_id] = _user_code_count_delta.get(user_id, 0) + delta

def get_user_code_count(user_id: int) -> int:
    """返回本地累积的 + status_counters 中的总数"""
    return status_counters.get("total_codes", 0) + _user_code_count_delta.get(user_id, 0)
```

#### 11.2.2 Idx Bot 改造

**文件**: `bots/idx_bot.py`

在 `_process_pending_uploads()` 生成文件码后:

```python
# 已有: 写 codes 表
# 新增:
from utils.shared_counters import incr_user_code_count
incr_user_code_count(uploader_id, 1)
```

#### 11.2.3 /my_codes 改造

**文件**: `bots/idx_bot.py:846`

```python
# 改造前
total_rows = await codes_col.count_documents({"uploader_id": user.id})

# 改造后
from utils.shared_counters import get_user_code_count
total_rows = get_user_code_count(user.id)  # 0 RU
```

### 11.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢（增量丢失不影响业务，只影响显示数字） |
| 后果 | 数字可能略低，重启后重置为 0 |

### 11.4 节省

约 **~5 RU/分钟**

---

## 十二、方案 E7: 用户码列表缓存

### 12.1 当前问题

`idx_bot.py:846` 用户查 `/my_codes` 时调用 `count_documents` + `find()` 分页，每用户每次 ~5 RU。

### 12.2 改造方案

**文件**: `database/cache.py` — 新增内存缓存

```python
_user_codes_cache = QueryCache(max_size=500, ttl_seconds=300)  # 500 用户 / 5 分钟

def get_user_codes_cache() -> QueryCache:
    return _user_codes_cache

def invalidate_user_codes(user_id: int):
    """用户改码后调用"""
    _user_codes_cache.invalidate(f"user_codes:{user_id}")
```

**改造** `idx_bot.py:846-860`:

```python
# 改造前
total_rows = await codes_col.count_documents({"uploader_id": user.id})
rows = await codes_col.find({"uploader_id": user.id}, ...)

# 改造后
from database.cache import get_user_codes_cache, invalidate_user_codes
cache = get_user_codes_cache()
cache_key = f"user_codes:{user.id}:{page}"
cached = cache.get(cache_key)
if cached is not None:
    total_rows, rows = cached
else:
    total_rows = await codes_col.count_documents({"uploader_id": user.id})
    rows = await codes_col.find(...)
    cache.set(cache_key, (total_rows, rows))
```

**改造** Idx Bot 用户改码的所有回调 (mycode_*)：在末尾加 `invalidate_user_codes(user.id)`

### 12.3 安全分析

| 项目 | 说明 |
|------|------|
| 丢数据 | 不丢 |
| 后果 | 5 分钟内用户看不到自己刚改的码（无业务影响） |

### 12.4 节省

约 **~0.5 RU/分钟**（低频操作）

---

## 十三、完整改动文件清单

| 文件 | 方案 | 改动类型 |
|------|------|---------|
| `database/cache.py` | B1, B5, C1, E7 | 缓存类重构 + 失效函数 + 负缓存 |
| `database/cache_store.py` | D, E1 | 新增 local_job_queue / cells_snapshot 表 + 方法 |
| `database/session.py` | D, C1, E1 | Queue Syncer / Sync Back + 负缓存集成 + get_active_cells 改造 |
| `database/__init__.py` | D | 新增导出 |
| `services/permission.py` | A1, C1 | get_or_create_user 优先走缓存 + 负缓存失效 |
| `bots/dsp_bot.py` | A2, D | Worker 改读 SQLite + 协程启动 + 动态限速兼容 |
| `bots/idx_bot.py` | A2, E2, E7 | 5 处 files_col.find_one + 增量计数 + 码列表缓存 |
| `bots/admin_bot/handlers.py` | A2, B1 | files_col.find_one + invalidate_code_cache |
| `bots/mon_bot.py` | E1 | cells 变更时写快照 + 通知 |
| `bots/up_bot.py` | E1 | 启动时加载 cells 快照 + 定时检查通知 |
| `storage/delivery_resolver.py` | B3 | _CELL_CACHE_TTL 30 → 120 |
| `utils/shared_counters.py` | E2 | 新增增量计数函数 |

---

## 十四、容易出错的地方（重点审查）

### 14.1 D 方案的并发与重入问题

#### 14.1.1 Queue Syncer 的 CTE 与 Worker 的 SQLite 写竞争

**错误场景**:

```
T1: Queue Syncer 执行 CTE 标记 dispatched
T2: Worker 此时崩溃，CRDB 仍是 pending
T3: 下轮 Queue Syncer 又把这 job 拉入
T4: Worker 重新发送（重复发送）
```

**正确处理**: 已通过"Worker 标记 dispatched + Sync Back 删除"避免，但需在 `dsp_bot.py:253` 改造时**正确处理 local_queue 为空的情况**：

```python
# 易错点: 没有处理 local_queue 为空
local_queue = await store.get_local_pending_jobs(10)
if not local_queue:
    idle_count += 1
    await asyncio.sleep(1)
    continue
```

#### 14.1.2 dead job 重复回写

**错误场景**: Worker 标记 `dead` 后崩溃，Sync Back 又重试 `mark_job_dead()`，CRDB 中 `dead_retry_count` 误增。

**正确处理**: Sync Back 调 `mark_job_dead` 后立即 `mark_local_job_synced` + `delete_local_job`，防止重复处理。

#### 14.1.3 get_pending_jobs_count 缓存失效

**错误场景**: Queue Syncer 把新 job 写进 SQLite，但 `get_pending_jobs_count_cached` 还在返回旧的缓存值（5 秒内）。

**正确处理**: 这是可接受的，最多 5 秒延迟。无需处理。

### 14.2 A1 方案的缓存一致性问题

#### 14.2.1 插入失败时缓存与 DB 不一致

**错误代码**:
```python
try:
    await col.insert_one(user)
    get_user_cache().set(f"user:{user_id}", user)  # ← 如果 insert 失败，缓存写脏数据
except Exception:
    pass
```

**正确处理**: 必须 `insert` 成功后才写缓存：

```python
try:
    await col.insert_one(user)
    get_user_cache().set(f"user:{user_id}", user)  # ← 仅成功路径
except Exception:
    user = await get_user_cached(user_id) or await col.find_one({"user_id": user_id})
```

#### 14.2.2 并发首次插入

**错误场景**: 两个请求同时插入同一 user_id。

**正确处理**: insert 冲突时 catch 异常，重新查缓存或 DB（已在代码中处理 ✅）。

### 14.3 A2 方案的导入问题

#### 14.3.1 get_file_record_cached 路径错误

**易错点**: 不同文件用不同路径:

```python
# dsp_bot.py 已经有:
from database import dequeue_jobs, get_file_records_col, ...

# 但缺少:
from database import get_file_record_cached  # ← 需要添加
```

**注意**: `get_file_record_cached` 在 `database.session` 中定义，但通过 `database.__init__` 导出。修改时**确认导入路径正确**。

#### 14.3.2 字段名差异

**易错点**: `files_col.find_one` 返回 `dict`，`get_file_record_cached` 也返回 `dict`，但**如果 cache miss 走 DB 路径返回的字段可能与 cache hit 不同**。

**正确处理**: 两者实现已统一（看 `session.py:1081-1106`），字段一致 ✅。

### 14.4 B1 方案的失效函数遗漏

#### 14.4.1 admin_bot 多处改码操作

**易错点**: 漏掉某个 admin 操作函数的 invalidate。

**正确处理**:
1. 先用 `grep` 找出所有 `codes_col.update_one` 的位置
2. 逐个加 `invalidate_code_cache(file_code)`
3. 建议**统一在 `update_code_status` 类的 helper 函数里加**，避免遗漏

#### 14.4.2 文件码的 hash 不一致

**易错点**: `code_cache` 用 `f"code:{text}"` 作为 key，但 admin 传进来的可能是大写/小写。

**正确处理**: 已有 `idx_bot.py:651` `cache_key = f"code:{text}"` 统一 ✅。

### 14.5 C1 方案的负缓存失效时机

#### 14.5.1 负缓存阻止合法用户注册

**错误场景**: 用户先被探测（不存在的 user_id 命中负缓存），60 秒内首次访问被拒绝。

**正确处理**: 在 `get_or_create_user()` 插入成功后**立即**调 `invalidate_negative_user_cache(user_id)`（已在 7.2.5 说明）。

#### 14.5.2 负缓存与正常缓存的双重失效

**易错点**: 创建用户后只失效负缓存，但 `_user_cache` 也得刷新。

**正确处理**:
```python
# 必须同时:
get_user_cache().set(f"user:{user_id}", user)  # 失效旧缓存或写入新值
invalidate_negative_user_cache(user_id)  # 失效负缓存
```

### 14.6 B3 方案与 Mon Bot 协同

#### 14.6.1 TTL 改长后 Mon 降级未及时通知

**错误场景**: Mon 触发降级，Delivery_Resolver 缓存还有 2 分钟有效期。

**正确处理**: `invalidate_cell_cache()` 仍会在 Mon 降级时调用（看 `mon_bot.py`），**B3 改造后失效函数仍有效** ✅。

### 14.7 B5 方案的兼容性

#### 14.7.1 原有调用方依赖 _clean_expired

**易错点**: 删掉 `_clean_expired()` 调用后，某些代码可能依赖 get 时的全量清理。

**正确处理**: 没有外部调用方依赖 `_clean_expired()`，它是私有方法 ✅。

### 14.8 E1 方案的循环依赖

#### 14.8.1 Mon Bot 没启动时其他 Bot 启动

**错误场景**: Up Bot 启动，SQLite cells_snapshot 为空（Mon Bot 还没写），回退 CRDB。

**正确处理**: 已有兜底逻辑（10.2.5 第 6 行）✅。

#### 14.8.2 SQLite 锁竞争

**错误场景**: Mon Bot 正在写 snapshot，其他 Bot 同时读。

**正确处理**: SQLite WAL 模式 + busy_timeout=5000（看 `cache_store.py:29`）已处理 ✅。

### 14.9 E2 方案的进程间不同步

#### 14.9.1 shared_counters 是进程内

**易错点**: Up Bot 和 Idx Bot 是不同进程，计数器不共享。

**正确处理**: 方案 E2 主要优化**单进程内**的高频查询，不试图跨进程同步。/my_codes 只在 Idx Bot 进程调用，本地计数器足够。

### 14.10 E7 方案的失效遗漏

#### 14.10.1 用户码缓存失效点不全

**易错点**: 用户改码后没调 `invalidate_user_codes(user_id)`。

**正确处理**: `mycode_*` 系列回调（修改/删除/下架）都要加。**至少 8 处需要审查**。

---

## 十五、最终安全审查

### 15.1 数据丢失零容忍

所有方案的核心理念：

> **CRDB 永远是唯一权威数据源，SQLite 只做本地处理缓存。**

**逐方案确认**:

| 方案 | 数据流向 | 是否可能丢数据 |
|------|---------|--------------|
| D | CRDB → SQLite → Worker | ❌ 不可能 |
| A1 | 缓存 → CRDB | ❌ 不可能（缓存是副本） |
| A2 | 缓存 → CRDB | ❌ 不可能 |
| B1 | 缓存 TTL 缩短 | ❌ 不可能 |
| C1 | 负缓存 60 秒 | ❌ 不可能 |
| B3 | 缓存延长 | ❌ 不可能 |
| B5 | 清理逻辑优化 | ❌ 不可能 |
| E1 | SQLite 共享 | ❌ 不可能 |
| E2 | 增量计数 | ❌ 不可能（数字略低） |
| E7 | 列表缓存 | ❌ 不可能 |

### 15.2 唯一可能异常

**D 方案**：Worker 崩溃 + 重复发送（和现有架构 crash 风险相同）

**E 方案**：所有缓存都是只读副本，DB 始终是权威源

### 15.3 必须的测试

| 方案 | 测试场景 |
|------|---------|
| D | 1) 正常发送 2) 模拟 Worker 崩溃 3) 模拟 Sync Back 失败 4) 重启 Dsp 验证 |
| A1 | 1) 新用户首次创建 2) 缓存命中 3) 并发创建同一用户 |
| A2 | 1) 中继解码 2) 文件状态变更后立即查询 |
| B1 | 1) 管理员下架码后立即解码 2) 1 小时后自然过期 |
| C1 | 1) 探测不存在的 user_id 2) 60 秒后再次查询 3) 创建新用户 |
| B3 | 1) 频道降级后 2 分钟内发送 |
| B5 | 1) 5000 条 cache 满时 set 2) LRU 弹出 |
| E1 | 1) Mon 降级后其他 Bot 30 秒内感知 2) 启动时无快照回退 |
| E2 | 1) /my_codes 数字 2) 重启后数字重置 |
| E7 | 1) /my_codes 缓存命中 2) 改码后失效 |

---

## 十六、实施顺序

### 第一批（P0，估计 4-6 小时）

| 步骤 | 方案 | 预计时长 |
|------|------|---------|
| 1 | A1 | 30 分钟 |
| 2 | D | 4-5 小时 |
| 3 | 测试验证 | 1 小时 |

### 第二批（P1，估计 2-3 小时）

| 步骤 | 方案 | 预计时长 |
|------|------|---------|
| 4 | A2 | 30 分钟 |
| 5 | B1 | 1-2 小时 |
| 6 | C1 | 1 小时 |

### 第三批（P2，估计 1-2 小时）

| 步骤 | 方案 | 预计时长 |
|------|------|---------|
| 7 | B3 | 5 分钟 |
| 8 | B5 | 30 分钟 |
| 9 | E1 | 1-2 小时 |
| 10 | E2 | 30 分钟 |
| 11 | E7 | 30 分钟 |

---

## 十七、预期效果

| 指标 | 优化前 | 优化后 | 变化 |
|------|--------|--------|------|
| 每分钟 RU | ~4500 | ~2530 | **-44%** |
| 每月 RU | ~4500 万 | ~3300 万 | **-27%** |
| 免费套餐日活支持 | ~1 万 | ~2 万 | **2 倍** |

> **注**: 实际节省取决于实施完成度。如 D 方案完全实施，可达 -73%。

---

## 十八、验证方法

### D 方案验证

1. 启动 Dsp Bot，确认 queue-syncer 和 sync-back 协程正常启动
2. Idx Bot 写入一条 job → 5 秒内出现在 SQLite local_job_queue
3. Worker 消费 → 文件正常发送
4. 模拟发送失败 → retried 状态 30 秒内回写 CRDB
5. 模拟死信 → dead 状态回写 CRDB
6. 重启 Dsp Bot → 全量重新拉取，不丢 job

### A1/A2 验证

1. `/start` → 首次创建用户成功
2. 再次 `/start` → 命中缓存（不再查 CRDB）
3. 中继解码 → 文件正常发送

### B1 验证

1. 解码一个文件码 → 成功
2. 管理员下架该文件码 → 调用 invalidate_code_cache
3. 再次解码 → 提示"文件不存在"

### C1 验证

1. 用不存在的 user_id 查用户 → 命中负缓存
2. 60 秒后再次查询 → 负缓存过期，重新查 CRDB

### E1 验证

1. Mon Bot 降级某 cell
2. 30 秒内其他 Bot 感知（通过 cells_change_notify）

---

## 十九、回滚方案

所有改动均为缓存层优化，CRDB 是唯一权威数据源。

回滚方法:
1. D 方案: 恢复 `dsp_bot.py` 的 `dequeue_jobs` 调用，删除 queue-syncer/sync-back 协程
2. A1/A2: 恢复原函数调用
3. B1/B3/B5/C1/E1/E2/E7: 恢复原 TTL 和清理逻辑

**不回滚也不丢数据，最坏就是 RU 消耗回到优化前水平。**

---

## 二十、不实施的方案（详细分析）

### 20.1 A3: get_relay_config 缓存

**为什么发现**: 计划文档初版曾提议改造 `session.py:770-778` 的 `get_relay_config()`。

**详细分析**:

| 维度 | 数据 |
|------|------|
| 调用位置 | `session.py:770-778` |
| 真实调用频率 | Relay Pool 启动时**调用 1 次**（非每次中继） |
| 当前每次 RU | ~3 RU（3 次 `_get_config` 查 CRDB） |
| 实施后 RU | 0 |
| 节省 RU/月 | **< 1 万** |
| 改造风险 | 中（需改 session.py 多处导入） |

**不实施原因**: 启动期一次性查询，月节省 < 1 万 RU，不值得改。

**当前已有兜底**: `_get_config()` 内部已有 10 分钟缓存（看 `cache.py:48` `_config_cache`），所以**当前已不是每次启动都查 CRDB**——只有中继账号配置变更后 10 分钟内重启才查 CRDB。

---

### 20.2 B2: external_code_mapping 全量缓存改单条

**为什么发现**: 计划文档初版曾提议改造 `session.py:973-1013`。

**详细分析**:

| 维度 | 数据 |
|------|------|
| 调用位置 | `session.py:973-1013` |
| 已有缓存 | **60 秒全量缓存**（`find({})` 后用 Python dict 查） |
| 大部分情况 | **走内存，0 RU** |
| 缓存失效时 | 1 次全表扫描 ~0.5 RU（44 行小表） |
| 真实 RU/分钟 | **< 1 RU** |
| 改造后预计 | < 0.2 RU/分钟 |

**不实施原因**: 已有 60 秒全量缓存，实际 RU 消耗 < 5 万/月，改单条缓存收益 < 5 万/月，**收益不抵改动成本**。

**实施成本**: 需新增 `_external_code_cache` dict + 改 `get_system_code_for_external` 签名 + 测试所有外部码调用方。

---

### 20.3 B4: get_pending_jobs_count 缓存

**为什么发现**: 计划文档初版曾提议加 5 秒缓存。

**详细分析**:

| 维度 | 数据 |
|------|------|
| 原计划 | 缓存 5 秒，省 ~8 RU/分钟 |
| 冲突方案 | 方案 D（SQLite 本地队列）实施后，此函数应**直接查本地 SQLite** |
| 查本地 SQLite | 0 RU |
| 是否仍需 5 秒缓存 | 不需要，SQLite 查 COUNT(*) 极快（<1ms） |

**不实施原因**: **被方案 D 替代**。方案 D 实施后此查询零 RU 走本地 SQLite，5 秒缓存反而是多余的。

---

### 20.4 C2: 统一配置缓存机制

**为什么发现**: 计划文档初版曾提议合并 `get_all_code_bot_routes` 和 `_refresh_bot_config_cache`。

**详细分析**:

| 维度 | 数据 |
|------|------|
| 调用位置 | `session.py:862-867` |
| 已有缓存 | `_BOT_CONFIG_TTL` 控制（看 session.py 上下文） |
| 调用频率 | 每次解码 1 次 |
| 真实 RU/分钟 | < 3 |

**不实施原因**: 改动复杂（涉及多个 Bot 的 `routes` 缓存），收益 < 16 万 RU/月。

---

### 20.5 C3: 缓存命中率指标

**为什么发现**: 计划文档初版提议在 admin_bot `/status` 暴露命中率。

**详细分析**:

| 维度 | 数据 |
|------|------|
| 真实 RU 节省 | **0**（只读缓存命中状态） |
| 价值 | 可观测性，便于后续调优 |
| 实施成本 | 中（需在 QueryCache 加 hits/misses 计数） |

**不实施原因**: **不直接省 RU**，是辅助功能。后续可作为独立任务做。

---

### 20.6 E3: admin /status 缓存

**详细分析**:

| 维度 | 数据 |
|------|------|
| 调用位置 | `display.py:34-89` |
| 调用频率 | 管理员手动查询，估算 **1-2 次/小时** |
| 每次 RU | 0（数据全从 `status_counters` 内存读） |
| **真实消耗** | **首次启动时 4 次 count_documents**（一次） |
| 后续查询 | **0 RU**（走 `status_counters` 内存） |

**不实施原因**: 计划文档初版误以为每次 `/status` 都查 CRDB。**实际只首次启动查一次**，后续全从内存读，**已是最优实现**。

---

### 20.7 E4: 启动全量加载

**详细分析**:

| 维度 | 数据 |
|------|------|
| 真实收益 | **未发现明确收益点** |
| 与 E1 重叠 | users/files/codes 的启动加载可放在 `_load_caches_from_disk()` 复用 |

**不实施原因**: 与 E1 部分重叠，且没有明确高频查询点需要它。

---

### 20.8 E5: 启动统计优化

**详细分析**:

| 维度 | 数据 |
|------|------|
| 当前实现 | `display.py:38-45` 启动时初始化 4 个计数器 |
| 涉及查询 | total_users, total_files, active_files, today_decodes |
| 真实 RU | ~5 RU（启动时一次） |
| 优化空间 | 用本地 SQLite 替代 `today_decodes`（看 cache_store.db 是否有） |

**不实施原因**: 启动时**只查一次**，5 RU 消耗可忽略。

---

### 20.9 E6: Dsp cells 通知

**详细分析**: 计划文档初版曾提议 Dsp 跨进程 cells 通知。

**不实施原因**: **与 E1 完全重叠**。E1 已经做了 cells 跨进程 SQLite 共享，Dsp 同样受益，单独做 E6 是重复劳动。

---

## 二十三、新发现的优化点

刚才深度审查代码时发现 3 个**之前没纳入计划**的优化点：

### 23.1 方案 F1: enqueue_job 的 status_counters 同步已可增强

**问题**: `session.py:1341-1381` 的 `enqueue_job` 已在 `status_counters` 中递增 `total_files` 和 `active_files`，但 **dequeue 时没有 -1**。

**详细分析**:

| 当前行为 | 后果 |
|---------|------|
| enqueue: total_files +1, active_files +1 | ✅ 正确 |
| Worker 发送成功: 不修改 | ❌ active_files 永远不减少 |
| 文件过期下线: 不修改 | ❌ active_files 失真 |
| 用户主动下架: 不修改 | ❌ active_files 失真 |

**影响**: admin /status 显示的"活跃文件"数字会**长期虚高**（不是 RU 消耗问题，是数据准确性）。

**改造**: 在 `reenqueue_job` / `mark_job_dead` / 用户下架回调中同步 `active_files -1` 即可。

**RU 节省**: 0（是数据准确性修复）。

---

### 23.2 方案 F2: _code_cache 已被广泛使用，无需新建

**重要发现**: 审查代码后发现：

- `idx_bot.py:1137-1143, 1262-1265, 1375-1378, 1405-1411` 已经在用户改码时**直接修改 `get_code_cache().cache`**，**没有调用 `invalidate`**
- 这是**正确的做法**（避免重新查 CRDB），但前提是 cache 键一致
- 方案 B1 仍需做（改 TTL），但**invalidate 函数可能用不上**

**详细分析**:

```python
# idx_bot.py:1262-1265 用户下架码
if cache_key in get_code_cache().cache:
    get_code_cache().cache[cache_key]["status"] = new_status
```

这是**手动修改缓存值**而不是 `invalidate + 重新查 DB`。**节省更多 RU**（既不走 DB 也不重建缓存对象）。

**对方案 B1 的影响**:

| 改造 | 原计划 | 实际需要 |
|------|--------|---------|
| 改 TTL | ✅ | ✅ |
| 新增 invalidate_code_cache | ✅ | ⚠️ 可选（已有手动修改模式） |
| admin 改码加 invalidate | ✅ | ❌ **不需要**（已有手动修改） |

**B1 改造可简化**: 只需改 TTL（70 行），不用新增 `invalidate_code_cache` 函数。

---

### 23.3 方案 F3: pending_uploads 已有本地通知，不需额外缓存

**重要发现**: `idx_bot.py:453-455`:

```python
if not await store.has_new_upload():  # ← 本地 SQLite 通知
    await asyncio.sleep(30)
    continue

pending_col = get_pending_uploads_col()
rows = await pending_col.find({"processed": 0}, limit=5)  # ← 只在有通知时才查
```

**已是最优实现**: Up Bot 写入 pending_uploads 时通过 `store.notify_upload()` 通知 Idx Bot，Idx Bot 30 秒内有通知才查 CRDB。

**真实 RU**: 极低（每 30 秒最多 1 次 `find`，空闲时 0 次）。

**结论**: **不需要额外优化**。

---

### 23.4 方案 F4: 用户改码已经手动更新缓存

**重要发现**: 多个 `idx_bot.py:mycode_*` 回调已用 `get_code_cache().cache[key][...] = new_value` 模式直接改缓存：

| 回调 | 行号 | 改的字段 |
|------|------|---------|
| `mycode_set_status` | 1262-1265 | `status` |
| `mycode_set_expiry` | 1405-1411 | `expire_time` |
| `mycode_set_note` | 1375-1378 | `note` |
| `mycode_extend_30d` | 1137-1143 | `expire_time` |
| `mycode_set_expiry_custom` | 1405-1411 | `expire_time` |

**结论**: B1 改造只需改 TTL，**不需要新增任何 invalidate 函数**。

**对原计划的影响**:

| 方案 | 原计划改动 | 实际需要改动 |
|------|----------|------------|
| B1 改 TTL | 1 行 | 1 行（不变） |
| B1 新增 invalidate_code_cache | 新增 | ❌ **删除** |
| B1 admin 加 invalidate | 4 处 | ❌ **删除**（已有手动修改） |

**简化后 B1 改造量**: 仅 1 行代码改动。

---

### 23.5 方案 F5: 启动时 init status_counters 改造

**问题**: `display.py:38-45` 启动时 4 个 count_documents 一次性查询。

**新方案**: 把这 4 个查询**改成本地 SQLite 缓存**：

| 查询 | 当前 | 优化后 |
|------|------|--------|
| `total_users` | count_documents({}) | 启动时本地 sqlite `users_count` 表 |
| `total_files` | count_documents({}) | 同上 |
| `active_files` | count_documents({status:active}) | 同上 |
| `today_decodes` | count_documents({request_time:today}) | 用 `_flush_decode_log_buffer_loop` 的 `status_counters["today_decodes"]`（已维护） |

**实施**: 在 `cache_store.py` 新增 `counter_snapshot` 表 + `CacheStore.save_counter_snapshot()` / `load_counter_snapshot()` 方法。

**启动逻辑**:
```python
# 改造前
_status_counters["total_users"] = await users_col.count_documents({})  # 5 RU

# 改造后
counters = await store.load_counter_snapshot()
if counters:
    _status_counters.update(counters)
else:
    # 首次启动回退 CRDB
    ...
```

**RU 节省**: ~5 RU/启动（一次性），但**提升了 admin /status 启动速度**。

**实施难度**: 低。

---

### 23.6 方案 F6: pending_uploads 改本地 SQLite 通知驱动（已实现）

**问题**: idx_bot.py 轮询 pending_uploads 是 `find({processed: 0})`。

**新发现**: 已有 `store.has_new_upload()` 通知机制（第 23.3 节分析）。

**结论**: **不需要额外优化**。

---

## 二十四、最终方案列表（修订）

经过更深入的代码审查，**原 10 个方案 + 6 个新发现** = 实际可实施 14 个优化点（其中 3 个不需要实施，11 个需要修改）。

| 编号 | 方案 | 实施? | RU/分钟 | 改动 |
|------|------|------|---------|------|
| **D** | SQLite 本地任务队列 | ✅ 必做 | ~1878 | 中 |
| **A1** | 修复 get_or_create_user | ✅ 必做 | ~60 | 低 |
| **A2** | 修复 file_records | ✅ 必做 | ~10 | 低 |
| **B1** | code_cache TTL 改 1h | ✅ 必做 | ~5 | **极低**（1 行） |
| **C1** | 负缓存 | ✅ 必做 | ~5 | 中 |
| **B3** | delivery_resolver TTL | ✅ 必做 | ~3 | **极低**（1 行） |
| **B5** | QueryCache 懒清理 | ✅ 必做 | ~3 | 低 |
| **E1** | cells 跨进程共享 | ⚠️ 可选 | ~1 | 中 |
| **E2** | count 内存化 | ⚠️ 可选 | ~5 | 中 |
| **E7** | 用户码列表缓存 | ⚠️ 可选 | ~0.5 | 中 |
| **F1** | status_counters 修复 | ✅ 必做 | 0（数据准确性） | 极低 |
| **F2** | 简化 B1 实施 | ✅ 必做 | 0（删除冗余） | 0（删除） |
| **F4** | 已用手动缓存模式 | ✅ 确认 | 0 | 0 |
| **F5** | 启动 init 走 SQLite | ⚠️ 可选 | 0 | 低 |
| A3 | get_relay_config | ❌ | < 1/月 | 收益过低 |
| B2 | external_code_mapping | ❌ | < 5/月 | 收益过低 |
| B4 | get_pending_jobs_count | ❌ | - | 被 D 替代 |
| C2 | 统一配置缓存 | ❌ | < 16/月 | 收益过低 |
| C3 | 缓存命中率指标 | ❌ | 0 | 不省 RU |
| E3 | admin /status 缓存 | ❌ | 0 | 已是内存读 |
| E4 | 启动全量加载 | ❌ | - | 未发现收益点 |
| E5 | 启动统计优化 | ❌ | 5/启动 | 可忽略 |
| E6 | Dsp cells 通知 | ❌ | - | 与 E1 重叠 |

---

## 二十五、关键审查点（提醒 AI 重点审查）

如果让其他 AI 交叉审查，建议重点关注：

1. **D 方案的数据流**：
   - Queue Syncer 5 秒一次是否够用？
   - Worker 崩溃恢复后是否会重复处理？
   - Sync Back 30 秒延迟是否影响业务？

2. **A1 方案的并发**：
   - `get_or_create_user` 改造后并发首次插入是否安全？
   - 缓存命中时的 user 数据是否是最新？

3. **B1 方案的简化**：
   - 原计划加 invalidate 函数是否多余？
   - 现有手动改 cache 模式是否已经够用？

4. **C1 方案的一致性**：
   - 负缓存与正常缓存的双重失效？
   - 用户改名/换头像等场景？

5. **E1 方案的进程协同**：
   - Mon Bot 启动顺序？其他 Bot 在 Mon 没启动时怎么工作？
   - SQLite 锁竞争问题？

6. **F1 方案的数据准确性**：
   - `status_counters["active_files"]` 长期虚高问题是否需要修复？
   - 哪些操作应该 -1？

---

**文档结束**

**确认无误后，可按"第十六节-实施顺序"逐步实施。**
