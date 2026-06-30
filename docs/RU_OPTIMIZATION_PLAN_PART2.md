# CRDB RU 优化第二部分：可选方案详细实施计划

> 配套文档: [RU_OPTIMIZATION_PLAN.md](file:///f:/xiangmu/tgjiema/docs/RU_OPTIMIZATION_PLAN.md)
> 包含 4 个可选方案: E1 / E2 / E7 / F5
> 目标: 在第一部分 7 个必做方案基础上，进一步降低 RU 消耗

---

## 〇、本部分摘要

| 方案 | 主题 | RU 节省 | 优先级 | 难度 | 改动文件数 |
|------|------|---------|--------|------|----------|
| **E2** | count_documents 内存计数 | ~5/分钟 | P0 | 中 | 2 |
| **F5** | 启动 init 走 SQLite | ~5/启动 | P0 | 低 | 1 |
| **E1** | cells 跨进程共享 | ~1/分钟 | P1 | 中 | 4 |
| **E7** | 用户码列表缓存 | ~0.5/分钟 | P2 | 中 | 2 |

**总新增节省**: ~6.5 RU/分钟 + 5 RU/启动

---

## 一、方案 E2: count_documents 内存计数

### 1.1 当前问题

`bots/admin_bot/display.py:38-44` 启动 admin_bot 时调用 4 个 count_documents：
- `users_col.count_documents({})` → 5 RU
- `files_col.count_documents({})` → 5 RU
- `files_col.count_documents({status: "active"})` → 5 RU
- `logs_col.count_documents({request_time: today})` → 5 RU

`bots/idx_bot.py:846` 每次 `/my_codes` 命令：
- `codes_col.count_documents({uploader_id: user.id})` → ~1 RU/次
- `codes_col.find({uploader_id: user.id})` → ~2 RU/次

### 1.2 真实 RU 消耗

| 调用点 | 频率 | 单次 RU | 每分钟 RU |
|--------|------|---------|----------|
| `display.py:40` total_users | 启动1次 | ~5 | ~0.01 |
| `display.py:41` total_files | 启动1次 | ~5 | ~0.01 |
| `display.py:42` active_files | 启动1次 | ~5 | ~0.01 |
| `display.py:44` today_decodes | 启动1次 | ~5 | ~0.01 |
| `idx_bot.py:846` 用户码计数 | 用户点击 | ~3 | ~5（DAU 1万 / 300秒） |
| `idx_bot.py:855` 用户码列表 | 用户点击 | ~2 | ~3 |
| `display.py:193/221` admin 日志分页 | 管理员点击 | ~3 | ~0.5 |
| **合计** | | | **~8.5/分钟** |

### 1.3 改造方案

#### 1.3.1 改造 utils/shared_counters.py

**文件**: `utils/shared_counters.py`

**当前**:
```python
status_counters: dict = {
    "total_users": 0,
    "total_files": 0,
    "active_files": 0,
    "today_decodes": 0,
}
status_counters_initialized: bool = False
```

**改造后**:
```python
status_counters: dict = {
    "total_users": 0,
    "total_files": 0,
    "active_files": 0,
    "today_decodes": 0,
}
status_counters_initialized: bool = False

# ─── 用户码本地计数器（E2 新增） ────────────
# 进程内增量计数,避免每次 /my_codes 查 CRDB
_user_code_count_delta: dict[int, int] = {}  # user_id -> 增量

def incr_user_code_count(user_id: int, delta: int = 1):
    """用户生成新码时 +1"""
    _user_code_count_delta[user_id] = _user_code_count_delta.get(user_id, 0) + delta

def decr_user_code_count(user_id: int, delta: int = 1):
    """用户删除/下架码时 -1"""
    _user_code_count_delta[user_id] = _user_code_count_delta.get(user_id, 0) - delta

def get_user_code_count(user_id: int) -> int:
    """获取用户的码总数(本地缓存, 0 RU)"""
    delta = _user_code_count_delta.get(user_id, 0)
    # 与 display 启动时从 CRDB 同步的基础值合并
    return max(0, status_counters.get(f"user_code_count:{user_id}", 0) + delta)
```

#### 1.3.2 改造 bots/idx_bot.py

**文件**: `bots/idx_bot.py:445` — `_process_pending_uploads()` 在生成码后增加:

**定位**: 在 `code` 写入 codes 表的代码块**之后**（约第 533 行 `get_code_cache().set(...)` 之前）:

```python
# 已有: 写入 codes_col
# ...
await codes_col.insert_one(ce)

# 新增: 递增用户码计数
try:
    from utils.shared_counters import incr_user_code_count
    incr_user_code_count(uploader_id, 1)
except Exception:
    pass
```

**注意**: 这个改动必须在 `_process_pending_uploads` 的循环里（针对每个生成的 code），而不是循环外。

**具体行号定位**: `idx_bot.py:512-540` 附近生成 file_code 后立即调用 `incr_user_code_count`。

#### 1.3.3 改造 bots/idx_bot.py:846

**文件**: `bots/idx_bot.py:840-870` — `cmd_my_codes` 处理

**改造前**:
```python
async def cmd_my_codes(update, context, page=1):
    # ...
    total_rows = await codes_col.count_documents({"uploader_id": user.id})  # ← 1 RU
    if total_rows == 0:
        await safe_reply_text(update.message, "您还没有上传过文件码。")
        return
    rows = await codes_col.find(  # ← 2 RU
        {"uploader_id": user.id},
        sort=("created_at", -1),
        skip=skip,
        limit=PER_PAGE,
    )
```

**改造后**:
```python
async def cmd_my_codes(update, context, page=1):
    # ...
    # E2: 用户码计数走本地缓存(0 RU)
    from utils.shared_counters import get_user_code_count
    total_rows = get_user_code_count(user.id)
    if total_rows == 0:
        await safe_reply_text(update.message, "您还没有上传过文件码。")
        return
    # 列表仍需查 CRDB（一次性查全表分页,这里用本地缓存不能替代)
    rows = await codes_col.find(
        {"uploader_id": user.id},
        sort=("created_at", -1),
        skip=skip,
        limit=PER_PAGE,
    )
```

**注意**: `find()` 仍需查 CRDB（因为要分页数据），但 `count_documents` 已被本地计数替代。

#### 1.3.4 用户下架/删除码时递减计数

**文件**: `bots/idx_bot.py:1262-1268` — `mycode_set_status` 等回调

**改造**: 在用户下架码时增加:
```python
# 用户下架码
if new_status == "offline":
    from utils.shared_counters import decr_user_code_count
    decr_user_code_count(user.id, 1)
```

**涉及函数**（需逐个加 `decr_user_code_count`）:

| 函数 | 行号 | 触发条件 |
|------|------|---------|
| `mycode_set_status` (offline) | 1262 | 用户下架 |
| `mycode_delete` (未来) | 待定 | 用户删除 |

#### 1.3.5 启动时同步基线

**文件**: `bots/admin_bot/display.py:38-45`

**新增**（在现有初始化代码之后）:
```python
# E2: 同步用户的码计数基线
# 注意: 仅在 admin_bot 启动时执行一次, 代价是 ~10 RU
# 替代方案: 用户首次 /my_codes 时按需同步
if not _shared_counters.status_counters_initialized:
    codes_col = get_codes_col()
    async for doc in codes_col.find({}, projection={"uploader_id": 1}):
        uid = doc.get("uploader_id")
        if uid:
            _status_counters[f"user_code_count:{uid}"] = _status_counters.get(f"user_code_count:{uid}", 0) + 1
```

**注意**: 这里的 ~10 RU 是**一次性**启动成本，可接受。

### 1.4 E2 错误审查

#### 错误 1: 计数与 CRDB 不同步

**场景**: 用户通过 admin_bot 删除/下架码，但 `decr_user_code_count` 没被调用。

**后果**: 数字偏多（无 RU 风险，只是数据准确性）。

**缓解**: 
- F1 方案（status_counters 修复）也修了类似问题
- 接受小的不精确（最多偏高）

#### 错误 2: 跨进程不同步

**场景**: Up Bot 生成码，Idx Bot 进程的 `_user_code_count_delta` 没增加。

**关键事实**:
- `incr_user_code_count` 写在 `_process_pending_uploads` 中（Idx Bot 进程）
- Up Bot 写 pending_uploads，**不写 codes**
- Idx Bot 把 pending_uploads 处理后才写 codes
- 所以 incr 调用在 **Idx Bot 进程**，符合预期 ✅

**确认**: idx_bot.py:445 起的 `_process_pending_uploads` 在 Idx Bot 进程内执行，`incr_user_code_count` 调用也在 Idx Bot 进程 ✅。

#### 错误 3: get_user_code_count 第一次返回 0

**场景**: 新用户首次 /my_codes，启动时基线没同步（admin_bot 没启动过）。

**后果**: 显示 0，但实际已有码。

**缓解**:
- 用户第一次 /my_codes 触发回退查询 CRDB
- 后续 incr 累积
- 数字会先显示 0，刷新后正常（可接受）

**改进**（可选）:
```python
def get_user_code_count(user_id: int) -> int:
    delta = _user_code_count_delta.get(user_id, 0)
    base = status_counters.get(f"user_code_count:{user_id}", -1)  # -1 表示未初始化
    if base == -1:
        return -1  # 调用方应触发按需同步
    return max(0, base + delta)
```

**改造后调用方**:
```python
total = get_user_code_count(user.id)
if total == -1:
    # 按需同步: 查一次 CRDB, 写入本地
    from database import get_codes_col
    codes_col = get_codes_col()
    actual = await codes_col.count_documents({"uploader_id": user.id})
    _status_counters[f"user_code_count:{user.id}"] = actual
    total = actual
```

#### 错误 4: status_counters 是 dict，key 冲突

**场景**: `status_counters[f"user_code_count:{user_id}"]` 与现有 key（如 `total_users`）冲突。

**事实**: key 格式不同（带冒号和数字），不会冲突 ✅。

### 1.5 E2 安全分析

| 数据 | 是否可能丢 | 后果 | 严重程度 |
|------|----------|------|---------|
| 码记录本身 | ❌ | 在 CRDB，本地只是计数 | 无 |
| 计数准确性 | ⚠️ | 可能略偏多（漏 -1） | 低（仅 /my_codes 显示） |
| 跨进程 | ❌ | Idx Bot 进程单独维护 | 无 |

### 1.6 E2 节省

| 指标 | 数值 |
|------|------|
| 每分钟 RU | ~5 |
| 每月 RU | ~150 万 |
| 实施难度 | 中 |
| 风险 | 低 |

---

## 二、方案 F5: 启动 init 走 SQLite 缓存

### 2.1 当前问题

`bots/admin_bot/display.py:38-45` admin_bot 启动时执行 4 个 count_documents，~20 RU 一次性消耗。

### 2.2 改造方案

#### 2.2.1 新增 SQLite 表

**文件**: `database/cache_store.py` — 在 `init()` 方法中新增 DDL（约第 86 行 `await self._db.commit()` 之前）:

```python
# ─── 启动统计快照：admin_bot 启动时从 SQLite 加载（E2/F5） ───
await self._db.execute(
    """CREATE TABLE IF NOT EXISTS counter_snapshot (
        key   TEXT PRIMARY KEY,
        value INTEGER NOT NULL,
        ts    REAL NOT NULL
    )"""
)
```

#### 2.2.2 CacheStore 新增方法

**文件**: `database/cache_store.py` — 在 `dump` 方法后新增

```python
async def save_counter_snapshot(self, counters: dict[str, int]):
    """保存启动统计快照(各 Bot 写入)"""
    if not self._db:
        return
    now = time.time()
    rows = [(k, v, now) for k, v in counters.items()]
    for key, val, ts in rows:
        await self._db.execute(
            "INSERT OR REPLACE INTO counter_snapshot (key, value, ts) VALUES (?, ?, ?)",
            (key, val, ts)
        )
    await self._db.commit()

async def load_counter_snapshot(self) -> dict[str, int]:
    """加载启动统计快照"""
    if not self._db:
        return {}
    try:
        async with self._db.execute("SELECT key, value FROM counter_snapshot") as cursor:
            rows = await cursor.fetchall()
            return {k: v for k, v in rows}
    except Exception:
        return {}
```

#### 2.2.3 改造 display.py:38-45

**文件**: `bots/admin_bot/display.py:34-46`

**改造前**:
```python
async def _get_status_text() -> str:
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()
    # 首次启动时,用 DB 查询初始化
    if not _shared_counters.status_counters_initialized:
        _status_counters["total_users"] = await users_col.count_documents({})  # 5 RU
        _status_counters["total_files"] = await files_col.count_documents({})  # 5 RU
        _status_counters["active_files"] = await files_col.count_documents({"status": "active"})  # 5 RU
        today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        _status_counters["today_decodes"] = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})  # 5 RU
        _shared_counters.status_counters_initialized = True
```

**改造后**:
```python
async def _get_status_text() -> str:
    # 首次启动时,优先从本地 SQLite 加载（F5 优化）
    if not _shared_counters.status_counters_initialized:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        cached = await store.load_counter_snapshot()
        if cached and "total_users" in cached:
            # 命中本地快照,零 CRDB RU
            for k, v in cached.items():
                _status_counters[k] = v
        else:
            # 本地无快照,回退 CRDB 查询
            users_col = get_users_col()
            files_col = get_file_records_col()
            logs_col = get_decode_logs_col()
            _status_counters["total_users"] = await users_col.count_documents({})
            _status_counters["total_files"] = await files_col.count_documents({})
            _status_counters["active_files"] = await files_col.count_documents({"status": "active"})
            today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            _status_counters["today_decodes"] = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})
            # 保存到本地 SQLite,下次启动直接用
            await store.save_counter_snapshot({
                "total_users": _status_counters["total_users"],
                "total_files": _status_counters["total_files"],
                "active_files": _status_counters["active_files"],
                "today_decodes": _status_counters["today_decodes"],
            })
        _shared_counters.status_counters_initialized = True
```

#### 2.2.4 维护快照的实时性

**关键问题**: 快照是**启动时**拍的照片，需要**实时更新**。

**当前 status_counters 的实时更新**（已实现）:

| 操作 | 位置 | 更新 |
|------|------|------|
| enqueue_job | `session.py:1371-1372` | total_files +1, active_files +1 |
| _flush_decode_log_buffer_loop | `cache.py:234` | today_decodes +N |

**需要新增**:

| 操作 | 位置 | 新增 |
|------|------|------|
| 文件下架 | `idx_bot.py:1262` mycode_set_status | active_files -1 |
| 文件删除 | 未来 | active_files -1 |
| 新用户注册 | `permission.py` get_or_create_user | total_users +1 |

**改造方法**: 在 `incr_user_code_count` 同一处调用位置加:

```python
# 在 session.py:enqueue_job 已有逻辑附近
try:
    from utils.shared_counters import status_counters
    status_counters["total_files"] = status_counters.get("total_files", 0) + 1
    status_counters["active_files"] = status_counters.get("active_files", 0) + 1
    # F5: 持久化到 SQLite(异步,不阻塞)
    create_safe_task(
        save_counters_to_sqlite(status_counters),
        name="save-counters"
    )
except Exception:
    pass
```

**新增 helper**:
```python
async def save_counters_to_sqlite(counters: dict):
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.save_counter_snapshot(counters)
```

#### 2.2.5 定期同步策略

**避免每次 incr 都写 SQLite**（性能差）。

**改造**: 增加**定期同步**任务（每 5 分钟一次）:

**文件**: 在 `admin_bot/_async_main()` 启动后:

```python
async def _periodic_save_counters():
    """每 5 分钟同步 status_counters 到 SQLite"""
    from utils.shared_counters import status_counters
    from database.cache_store import get_cache_store
    while True:
        try:
            await asyncio.sleep(300)  # 5 分钟
            store = get_cache_store()
            # 仅同步核心 4 个 + 用户码计数
            snapshot = {
                k: v for k, v in status_counters.items()
                if not k.startswith("user_code_count:")  # 太多,不同步
            }
            await store.save_counter_snapshot(snapshot)
        except Exception as e:
            logger.debug(f"[F5] 同步 counters 失败: {e}")

create_safe_task(_periodic_save_counters(), name="save-counters")
```

### 2.3 F5 错误审查

#### 错误 1: 快照数据陈旧

**场景**: admin_bot 重启时,本地 SQLite 快照是 5 分钟前的。

**后果**: 显示数字略旧（不影响业务）。

**缓解**:
- 接受小陈旧（5 分钟级别）
- 启动后 incr/decr 实时更新 `status_counters` 内存值

#### 错误 2: 用户码计数基线冲突（与 E2）

**场景**: F5 同步 `status_counters` 时,包含 `user_code_count:*` key，导致 SQLite 写入 N 条记录。

**缓解**:
- 第 2.2.5 节已排除 `user_code_count:*`
- 单独处理

#### 错误 3: 跨进程冲突

**场景**: Up Bot 的 status_counters 和 admin Bot 的 status_counters 是不同进程,数值不同。

**事实**: `status_counters` 是**进程级**全局变量,F5 持久化的是**当前进程的值**。

**注意**: 
- admin_bot 进程有自己独立的 `status_counters`
- 其他 Bot 进程也有自己的 `status_counters`
- F5 仅在 admin_bot 进程保存/加载快照
- 数字会**以 admin_bot 进程为准**(这是合理的,因为 admin_bot 显示)

#### 错误 4: 启动时多 Bot 同时写快照

**场景**: 多个 Bot 同时启动,都向 SQLite 写 snapshot。

**缓解**:
- SQLite WAL 模式 + busy_timeout=5000（已配置）
- 多次 INSERT OR REPLACE 不会损坏
- 最终一致性 OK

### 2.4 F5 安全分析

| 数据 | 是否可能丢 | 后果 | 严重程度 |
|------|----------|------|---------|
| status_counters 数值 | ❌ | 重启后从 SQLite 恢复 | 无 |
| 实时计数 | ⚠️ | 5 分钟内最准 | 低 |
| 跨进程 | ❌ | admin_bot 单进程维护 | 无 |

### 2.5 F5 节省

| 指标 | 数值 |
|------|------|
| 启动 RU | ~20 → 0（首次仍 ~20，第二次起 0） |
| 月 RU | 启动频率低, < 1 万 |
| 实施难度 | 低 |
| 风险 | 低 |

---

## 三、方案 E1: cells 跨进程共享 SQLite 缓存

### 3.1 当前问题

每个 Bot 进程独立查 CRDB cells 表:

| 进程 | 调用位置 | 频率 | RU/分钟 |
|------|---------|------|---------|
| Up Bot | `_refresh_active_slots()` | 60 秒 | ~0.5 |
| Idx Bot | `_refresh_active_slots()` | 60 秒 | ~0.5 |
| Mon Bot | `_get_cells()` (已有 120s 缓存) | 120 秒 | ~0.25 |
| Dsp Bot | delivery_resolver | 按需 | ~0.1 |
| Admin Bot | `display.py:64` (每次 /status) | 偶尔 | ~0.05 |
| **合计** | | | **~1.4/分钟** |

### 3.2 改造方案

#### 3.2.1 新增 SQLite 表

**文件**: `database/cache_store.py` — 在 `init()` 新增

```python
# ─── cells 跨进程快照（E1） ────────────
await self._db.execute(
    """CREATE TABLE IF NOT EXISTS cells_snapshot (
        id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        data       TEXT NOT NULL,        -- JSON 序列化的 cells 列表
        version    INTEGER NOT NULL,     -- 单调递增版本号
        updated_at REAL NOT NULL
    )"""
)
await self._db.execute(
    """CREATE TABLE IF NOT EXISTS cells_change_notify (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        version INTEGER NOT NULL,
        ts      REAL NOT NULL
    )"""
)
```

#### 3.2.2 CacheStore 新增方法

**文件**: `database/cache_store.py`

```python
async def save_cells_snapshot(self, cells: list[dict], version: int):
    """保存 cells 全量快照(仅 Mon Bot 写)"""
    if not self._db:
        return
    try:
        raw = json.dumps(cells, default=str)
        val = raw.decode() if isinstance(raw, bytes) else raw
    except Exception:
        return
    now = time.time()
    await self._db.execute(
        "INSERT OR REPLACE INTO cells_snapshot (id, data, version, updated_at) VALUES (1, ?, ?, ?)",
        (val, version, now)
    )
    await self._db.execute(
        "INSERT INTO cells_change_notify (version, ts) VALUES (?, ?)",
        (version, now)
    )
    await self._db.commit()

async def load_cells_snapshot(self) -> tuple[list[dict] | None, int]:
    """加载 cells 快照（其他 Bot 启动时调）"""
    if not self._db:
        return None, 0
    try:
        async with self._db.execute(
            "SELECT data, version FROM cells_snapshot WHERE id=1"
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None, 0
            data_raw, version = row[0], row[1]
            cells = json.loads(data_raw)
            return cells, version
    except Exception:
        return None, 0

async def has_cells_change(self, last_seen_version: int) -> tuple[bool, int]:
    """检查是否有 cells 变更通知（其他 Bot 定时调）"""
    if not self._db:
        return False, last_seen_version
    try:
        async with self._db.execute(
            "SELECT MAX(version) FROM cells_change_notify WHERE version > ?",
            (last_seen_version,)
        ) as cursor:
            row = await cursor.fetchone()
            new_version = row[0] if row and row[0] else last_seen_version
            return new_version > last_seen_version, new_version
    except Exception:
        return False, last_seen_version
```

#### 3.2.3 Mon Bot 改造

**文件**: `bots/mon_bot.py:80-99` — `_get_cells` 增加快照写入

**改造**（在 `set_cell_status` 调用后,以及 `log_rotate` 后）:

**关键点**: Mon Bot 的 cells 状态变更在多处发生:
- `mon_bot.py:232` `set_cell_status(slot_id, "lost")`
- `mon_bot.py:377-378` 封禁替换
- `mon_bot.py:399-400` 降级
- `mon_bot.py:405-406` 轮转

**方案 A（推荐）**: 在 Mon Bot 启动一个**单次快照任务**,每次 `_invalidate_cells_cache` 后重新写快照:

```python
# 在 mon_bot.py 增加 helper
async def _save_cells_to_sqlite():
    """将当前 cells 全量保存到本地 SQLite 供其他进程使用"""
    from database.cache_store import get_cache_store
    all_cells = await self._get_cells()  # 利用进程内 120s 缓存
    version = int(time.time())
    store = get_cache_store()
    await store.save_cells_snapshot(all_cells, version)
    logger.debug(f"[Mon] cells 快照已写入 SQLite (version={version}, count={len(all_cells)})")
```

**调用点**: 在 `mon_bot.py:232, 377, 399, 405` 等 cells 变更后立即调用:
```python
await set_cell_status(slot_id, "lost")
self._invalidate_cells_cache()  # 已有
create_safe_task(_save_cells_to_sqlite(), name="save-cells-snapshot")  # 新增
```

**启动时也调用**（让其他进程立刻有数据可用）:
```python
async def _async_main():
    monitor = HealthMonitor()
    # ... 启动其他协程 ...
    create_safe_task(_save_cells_to_sqlite(), name="save-cells-snapshot")
```

#### 3.2.4 其他 Bot 改造（Up/Idx/Dsp）

**文件**: `bots/up_bot.py` / `bots/idx_bot.py` / `bots/dsp_bot.py`

**模式**: 每个 Bot 启动时加载 cells 快照,定时检查变更。

**新增 helper** (建议放在 `storage/delivery_resolver.py` 或 `database/session.py`):

```python
# database/session.py 新增
_cells_local_version: int = 0
_cells_local_cache: list[dict] | None = None

async def get_active_cells_local() -> list[dict]:
    """本地 SQLite cells 快照优先,CRDB 兜底(E1)"""
    global _cells_local_cache, _cells_local_version
    from .cache_store import get_cache_store
    store = get_cache_store()
    
    # 首次加载
    if _cells_local_cache is None:
        cells, version = await store.load_cells_snapshot()
        if cells:
            _cells_local_cache = cells
            _cells_local_version = version
        else:
            # 无快照,查 CRDB
            col = get_cells_col()
            _cells_local_cache = await col.find({"status": "active"})
            _cells_local_version = 0
        return _cells_local_cache
    
    # 检查变更
    has_change, new_version = await store.has_cells_change(_cells_local_version)
    if has_change:
        cells, version = await store.load_cells_snapshot()
        if cells:
            _cells_local_cache = cells
            _cells_local_version = version
    
    return _cells_local_cache
```

**改造点**:

| 文件 | 当前调用 | 改为 |
|------|---------|------|
| `up_bot.py` | `get_active_cells()` (查 CRDB) | `get_active_cells_local()` |
| `idx_bot.py` | `get_active_cells()` | `get_active_cells_local()` |
| `delivery_resolver.py` | 走 `_cell_cache` 内存 | 保留 + 异步加载 |

**注意**: `get_active_cells()` 仍保留（CRDB 兜底用）,新增 `get_active_cells_local()` 走本地。

#### 3.2.5 兼容性: delivery_resolver 已有缓存

**事实**: `storage/delivery_resolver.py:81-100` 已有 `_cell_cache` per-channel 缓存。

**E1 与之关系**:
- `_cell_cache` 是 per-channel 缓存（命中具体 channel_id）
- E1 是全量 cells 列表缓存（命中 _get_cells 时的全表扫描）
- **两者互补不冲突**

**改造**:
- `_get_cells()` 优先走 E1 SQLite
- `_cell_cache` 仍按 channel_id 缓存

### 3.3 E1 错误审查

#### 错误 1: SQLite 锁竞争

**场景**: Mon Bot 正在写 snapshot,Up Bot 同时读。

**缓解**:
- SQLite WAL 模式（已配置）
- busy_timeout=5000（已配置）
- 读不阻塞写 ✅

#### 错误 2: 启动顺序

**场景**: Up Bot 启动,Mon Bot 还没启动,SQLite 快照为空。

**缓解**:
- `get_active_cells_local()` 已实现: 快照为空时回退 CRDB
- Mon Bot 启动后第一次 `_save_cells_to_sqlite` 写入快照
- 后续 Up Bot 周期检查能感知

#### 错误 3: 通知表无限增长

**场景**: `cells_change_notify` 表每次 cells 变更都插入一条,可能很大。

**缓解**:
- 已有 `dump_cache_to_disk_loop` 每 10 分钟调 `cleanup_notify_tables()`
- 新增 cells_change_notify 也走该清理逻辑
- `cleanup_notify_tables` 需增加对 cells_change_notify 的清理（看 `cache_store.py:148`）

**改造 cache_store.py**:
```python
async def cleanup_notify_tables(self):
    """每 10 分钟清理一次过期通知表"""
    cutoff = time.time() - 3600  # 保留 1 小时
    await self._db.execute("DELETE FROM pending_notify WHERE ts < ?", (cutoff,))
    await self._db.execute("DELETE FROM dsp_notify WHERE ts < ?", (cutoff,))
    await self._db.execute("DELETE FROM cells_change_notify WHERE ts < ?", (cutoff,))  # 新增
    await self._db.commit()
```

#### 错误 4: cells JSON 序列化大小

**场景**: cells 列表 ~50 个 cell,每个 ~500 字节,总 ~25 KB。JSON 序列化后 ~30 KB。

**影响**: SQLite 写一次 ~30 KB,极快（< 1ms）。✅

#### 错误 5: 跨进程版本号同步

**场景**: 两个 Mon Bot 实例（不可能,但假设）同时写 snapshot,version 冲突。

**现实**: 项目是单机部署,只有 1 个 Mon Bot 进程。✅

**如未来多机部署**:
- 改 version 为 `unix_timestamp * 1000 + monotonic_counter`
- 或用 CRDB 序列生成器

#### 错误 6: get_active_cells_local 状态过滤

**原 `get_active_cells()`**: 过滤 status='active' 的 cells。

**E1 实现**: 加载所有 cells,在内存中过滤。

**性能**: 50 cells 的 list 过滤极快（<1ms）。✅

### 3.4 E1 安全分析

| 数据 | 是否可能丢 | 后果 | 严重程度 |
|------|----------|------|---------|
| cells 状态 | ❌ | CRDB 是权威源 | 无 |
| 启动时一致性 | ⚠️ | Mon 没启动时回退 CRDB | 低 |
| 通知 | ❌ | 每 10 分钟清理 | 无 |

### 3.5 E1 节省

| 指标 | 数值 |
|------|------|
| 每分钟 RU | ~1.4 |
| 每月 RU | ~6 万 |
| 实施难度 | 中 |
| 风险 | 低 |

---

## 四、方案 E7: 用户码列表缓存

### 4.1 当前问题

`bots/idx_bot.py:846, 855` 每次 `/my_codes` 用户查:
- `count_documents({uploader_id: user.id})` → 1 RU
- `find({uploader_id: user.id})` → 2 RU

**频率**: DAU 1 万 / 300 秒 = ~33 次/分钟。

### 4.2 改造方案

#### 4.2.1 新增内存缓存

**文件**: `database/cache.py` — 在 `_code_cache` 附近新增

```python
# ─── 用户码列表缓存（E7） ────────────
_user_codes_cache = QueryCache(max_size=500, ttl_seconds=300)  # 500 用户/5 分钟

def get_user_codes_cache() -> QueryCache:
    return _user_codes_cache

def invalidate_user_codes(user_id: int):
    """用户改码后调用(下架/删除/修改)"""
    # 失效该用户所有分页缓存
    cache = get_user_codes_cache()
    keys_to_remove = [k for k in cache.cache if k.startswith(f"user_codes:{user_id}:")]
    for k in keys_to_remove:
        cache.invalidate(k)
```

#### 4.2.2 改造 bots/idx_bot.py:cmd_my_codes

**文件**: `bots/idx_bot.py:840-880`

**改造前**:
```python
async def cmd_my_codes(update, context, page=1):
    PER_PAGE = 10
    # ... (获取 user 对象)
    total_rows = await codes_col.count_documents({"uploader_id": user.id})  # 1 RU
    if total_rows == 0:
        await safe_reply_text(update.message, "您还没有上传过文件码。")
        return
    rows = await codes_col.find(  # 2 RU
        {"uploader_id": user.id},
        sort=("created_at", -1),
        skip=skip,
        limit=PER_PAGE,
    )
```

**改造后**:
```python
async def cmd_my_codes(update, context, page=1):
    PER_PAGE = 10
    from database.cache import get_user_codes_cache
    from utils.shared_counters import get_user_code_count  # E2 已实现
    
    user = await get_or_create_user(update.effective_user.id, ...)  # A1 已优化
    if user is None:
        return
    
    # 优先走 E2 本地计数
    total_rows = get_user_code_count(user.id)
    if total_rows == 0:
        # 首次访问,按需同步基线
        codes_col = get_codes_col()
        total_rows = await codes_col.count_documents({"uploader_id": user.id})
        _status_counters[f"user_code_count:{user.id}"] = total_rows
    
    if total_rows == 0:
        await safe_reply_text(update.message, "您还没有上传过文件码。")
        return
    
    # E7: 列表查询也走缓存
    cache = get_user_codes_cache()
    cache_key = f"user_codes:{user.id}:{page}"
    cached = cache.get(cache_key)
    if cached is not None:
        rows = cached
    else:
        codes_col = get_codes_col()
        rows = await codes_col.find(
            {"uploader_id": user.id},
            sort=("created_at", -1),
            skip=skip,
            limit=PER_PAGE,
        )
        rows = list(rows)  # 物化
        cache.set(cache_key, rows)
```

#### 4.2.3 失效点

**文件**: `bots/idx_bot.py:1137-1411` — 所有 `mycode_*` 回调

**改造**: 在用户改码、下架、删除操作后加 `invalidate_user_codes(user.id)`:

| 回调 | 行号 | 操作 |
|------|------|------|
| `mycode_set_status` | 1262-1268 | 下架/上架 → invalidate |
| `mycode_set_expiry` | 1405-1413 | 改过期 → invalidate |
| `mycode_set_note` | 1375-1379 | 改备注 → invalidate |
| `mycode_extend_30d` | 1137-1143 | 延30天 → invalidate |
| `mycode_set_expiry_custom` | 1405-1411 | 自定义过期 → invalidate |

**示例**:
```python
# idx_bot.py:1262 mycode_set_status
if cache_key in get_code_cache().cache:
    get_code_cache().cache[cache_key]["status"] = new_status
# 新增: 失效该用户的码列表缓存
from database.cache import invalidate_user_codes
invalidate_user_codes(user.id)
```

### 4.3 E7 错误审查

#### 错误 1: 缓存与 DB 不一致（修改后未失效）

**场景**: 用户通过 admin_bot 改码,但 Idx Bot 的 `_user_codes_cache` 未失效。

**事实**: admin_bot 改码的 `update_file_record_and_invalidate` 只失效 file_record 缓存,**不失效 codes 缓存**。

**后果**: 用户 5 分钟内看不到自己的改动。

**缓解**:
- TTL 5 分钟（5 分钟自动过期）
- 用户主动刷新也需等 5 分钟
- 接受（无业务影响）

**更彻底的修复**（可选）:
- 在 admin_bot/handlers.py 的 `cmd_set_status` 等也调用 `invalidate_user_codes(uploader_id)`
- 需要先查 codes 找到 uploader_id

#### 错误 2: 跨用户串扰

**场景**: 用户 A 的缓存键 `user_codes:A:1`,用户 B 改 A 的码(不可能,但假设)。

**事实**: 缓存键 `f"user_codes:{user.id}:{page}"` 已按 user_id 隔离,不会串扰 ✅。

#### 错误 3: 缓存对象序列化问题

**场景**: MongoDB 返回的 dict 含 `ObjectId` 等不能 pickle 的对象,缓存后读取失败。

**事实**: 当前项目用的是 `bson`/`motor`,返回的应该是 Python 原生类型。✅

**保险措施**:
```python
# 物化时 deep copy
rows = [dict(r) for r in rows]  # 去掉非原生类型
```

#### 错误 4: 分页 key 重复

**场景**: page=1 和 page=2 的 key 一样,导致数据覆盖。

**修复**: `f"user_codes:{user.id}:{page}"` 已含 page 编号,不会重复 ✅。

### 4.4 E7 安全分析

| 数据 | 是否可能丢 | 后果 | 严重程度 |
|------|----------|------|---------|
| 码列表 | ❌ | 缓存是副本 | 无 |
| 一致性 | ⚠️ | 5 分钟延迟 | 低 |
| 跨用户 | ❌ | 按 user_id 隔离 | 无 |

### 4.5 E7 节省

| 指标 | 数值 |
|------|------|
| 每分钟 RU | ~0.5 |
| 每月 RU | ~2 万 |
| 实施难度 | 中 |
| 风险 | 低 |

---

## 五、合并实施清单

| 方案 | 文件 | 改动类型 |
|------|------|---------|
| **E2** | `utils/shared_counters.py` | 新增 incr/decr/get_user_code_count |
| **E2** | `bots/idx_bot.py` | _process_pending_uploads 增量 + mycode 改码减量 + cmd_my_codes 改造 |
| **E2** | `bots/admin_bot/display.py` | 启动时同步基线（一次性 ~10 RU） |
| **F5** | `database/cache_store.py` | 新增 counter_snapshot 表 + save/load 方法 + cleanup |
| **F5** | `bots/admin_bot/display.py` | 启动逻辑改造 + 定期同步任务 |
| **E1** | `database/cache_store.py` | 新增 cells_snapshot / cells_change_notify 表 + save/load/has_change 方法 + cleanup 扩展 |
| **E1** | `database/session.py` | 新增 get_active_cells_local 函数 |
| **E1** | `bots/mon_bot.py` | 增加 _save_cells_to_sqlite helper + 多处调用 |
| **E1** | `bots/up_bot.py` / `bots/idx_bot.py` | get_active_cells → get_active_cells_local |
| **E7** | `database/cache.py` | 新增 _user_codes_cache + invalidate_user_codes |
| **E7** | `bots/idx_bot.py` | cmd_my_codes 改造 + 5 处 mycode_* 加 invalidate |

---

## 六、实施顺序（建议）

### 第一批（核心收益）

| 步骤 | 方案 | 预计时长 |
|------|------|---------|
| 1 | F5 | 30 分钟（最简单） |
| 2 | E2 | 1-2 小时 |

### 第二批（增强收益）

| 步骤 | 方案 | 预计时长 |
|------|------|---------|
| 3 | E7 | 1-2 小时 |
| 4 | E1 | 2-3 小时（跨进程协同） |

---

## 七、最终预期效果（两部分合计）

| 指标 | 优化前 | 第一部分 7 个 | 加 4 个可选 | 总变化 |
|------|--------|-------------|-----------|--------|
| 每分钟 RU | ~4500 | ~2530 | ~2523 | **-44%** |
| 启动 RU | ~20 | ~20 | ~5 | **-75%** |
| 每月 RU | ~4500 万 | ~3300 万 | ~3260 万 | **-27%** |

> **注**: 第一部分 D 方案完全实施可达 -73%。4 个可选方案额外增加 ~0.4% 收益,主要价值在**减少启动 RU** 和**数据准确性**。

---

## 八、关键审查点（提醒 AI 重点审查）

如果让其他 AI 交叉审查,建议重点关注:

1. **E2 跨进程一致性**:
   - Up Bot 写 pending_uploads, Idx Bot 写 codes,**incr 必须在 Idx Bot 进程**（_process_pending_uploads 中）。确认正确吗?
   - 用户通过 admin_bot 改码时, Idx Bot 进程没收到通知,**decr 会漏**。可接受吗?

2. **F5 跨进程冲突**:
   - `status_counters` 是进程级变量,F5 持久化的是 admin_bot 进程的值。如果 Up Bot 进程同时修改,会怎样?
   - 答: 各自维护,admin_bot 进程最后保存的值会被加载。✅

3. **E1 启动顺序**:
   - Mon Bot 没启动时,其他 Bot 启动能不能正常工作?
   - 答: `get_active_cells_local` 有 CRDB 兜底,Mon Bot 启动后下次同步生效。✅

4. **E7 admin_bot 改码**:
   - admin_bot 改码后,**没通知 Idx Bot 失效缓存**。用户 5 分钟内看不到改动。
   - 答: 可接受(无业务影响)。如需更强一致性,需在 admin_bot/handlers.py 也加 `invalidate_user_codes`。

5. **cells JSON 序列化大小**:
   - 50 cells × 500 字节 = 25 KB,SQLite 写一次没问题吧?
   - 答: WAL 模式 + 异步, 没问题。✅

---

**文档结束**

**配套第一部分必做方案: [RU_OPTIMIZATION_PLAN.md](file:///f:/xiangmu/tgjiema/docs/RU_OPTIMIZATION_PLAN.md)**
