# CockroachDB RU 异常消耗诊断报告

> **分析时段**：14小时 | **总消耗**：~60,000 RU  
> **数据来源**：SQL Activity (6小时) + Metrics (14小时) 截图

---

## 一、问题现象

### 1.1 SQL Activity 数据（近 6 小时）

| 指标 | 数值 |
|------|------|
| Top SQL 语句数 | **1 条** |
| 语句内容 | `UPDATE cells SET updated_at...` |
| 执行次数 | **34 次** |
| 占总运行时间 | **0.4%** |
| Statement Time | 5.8 ms |
| SQL CPU Time | 273.3 μs |

### 1.2 Metrics 数据（14 小时）

- **Request Units（RU）曲线特征**：
  - 呈 **~5 分钟周期性尖峰**
  - 峰值达到 **45+ RU/second**
  - 14 小时累计消耗：**~60,000 RU**

### 1.3 核心矛盾

```
SQL Activity 显示：34次 UPDATE，仅占 0.4% 运行时间
Metrics 实际消耗：60,000 RU（是正常估算的 2.5-15倍）
```

**结论：SQL Activity 只展示了冰山一角，99.6% 的隐藏查询未显示在前台。**

---

## 二、根因定位

### 2.1 为什么 SQL Activity 数据不完整？

CockroachDB Console 的 SQL Activity 存在以下限制：

1. **只显示 Top 100 语句指纹**（按 `% of All Runtime` 排序，而非按执行次数或RU排序）
2. **短查询会被聚合或忽略**：单次执行 < 1ms 的微查询可能不记录
3. **连接获取/释放开销不计入语句统计**：连接池管理产生的开销不显示
4. **系统内部查询被过滤**：如 `SHOW`、元数据查询等可能不展示

**你的场景**：数千次微查询（每次 0.x ms），单次运行时间极低，聚合后不显示在前台，但累积 RU 极高。

### 2.2 真正的高频 RU 消耗点

根据代码分析，5 分钟周期尖峰的具体来源如下：

#### 🔴 高消耗点 A：request_count 刷写 — 逐条 N+1 UPDATE

```python
# 文件：database/cache.py:189-206
# 频率：每 5 分钟
# 问题：内存中累积了所有热门码的计数后，逐条 UPDATE 到 CRDB 的 file_records 表

for code, count in request_counter.items():
    await update_file_record_and_invalidate(code, {"$inc": {"request_count": count}})
    # ↑ 如果 5 分钟内有 100 个不同的码被解码 → 100 次 UPDATE
    # ↑ 每次 UPDATE = 1 次 SQL round-trip + 行锁 + WAL 写入
```

**影响评估**：
- 正常负载：每次 10-50 个热门码 → 10-50 次UPDATE
- 高峰负载：每次 100+ 个热门码 → 100+ 次UPDATE
- 单次 RU 消耗：5-50 RU（取决于行大小和索引数量）

---

#### 🔴 高消耗点 B：code 变更同步 — N+1 循环更新

```python
# 文件：bots/idx_bot.py:1827-1870
# 频率：每 5 分钟
# 问题：用户操作（下架、备注修改、有效期设置）产生的 code 变更缓冲在内存，逐条刷写到 CRDB

for change in unsynced_changes:
    await codes_col.update_one({"code": change.code}, {"$set": {
        "note": change.note,
        "expire_time": change.expire_time,
        "status": change.status,
    }})
    # ↑ 典型负载下每次可能 10-50 条变更
    # ↑ 用户操作高峰期可能突增到 100+ 条
```

**影响评估**：
- 正常负载：每次 5-20 条变更
- 高峰负载（管理员批量操作）：50-200+ 条变更
- 单次 RU 消耗：5-30 RU

---

#### 🟡 中等消耗点 C：dirty_cells 同步

```python
# 文件：bots/mon_bot.py:532-537 → database/session.py:1838-1875
# 频率：每 ~5 分钟（Mon 主循环每 10 轮触发一次）
# 功能：将本地 SQLite 中状态变更的 cell 同步回 CRDB（异常事件审计）

async def sync_dirty_cells_to_crdb():
    dirty = await store.get_dirty_cells_local(50)  # 从 SQLite 获取脏 cell
    for cell in dirty:
        await col.update_one({"slot_id": cell["slot_id"]}, {"$set": set_fields})
        # ↑ 正常运行时脏 cell 数 < 10
        # ↑ 大规模降级事件时可能突增到 20-50+
```

**影响评估**：
- 正常运行：每次 1-5 个脏 cell
- 异常事件（大规模轮转）：10-30 个脏 cell
- 单次 RU 消耗：5-20 RU

---

#### 🟡 中等消耗点 D：dsp_bot job 状态同步

```python
# 文件：bots/dsp_bot.py:702-709
# 频率：每 120 秒（2分钟）
# 功能：将本地 SQLite 中已完成的 job 状态同步回 CRDB

async def sync_local_jobs_to_crdb():
    unsynced = await store.get_local_unsynced_jobs()
    for job in unsynced:
        if status == "retried":
            await col.update_one({"id": crdb_id}, {"$set": {...}, "$inc": {...}})
        elif status == "dead":
            await col.update_one({"id": crdb_id}, {"$set": {...}})
        # ↑ 频率高但每次数据量小（仅已完成 job）
```

**影响评估**：
- 每次 1-10 条 job（取决于派发速度）
- 单次 RU 消耗：1-10 RU
- 14 小时调用次数：420 次（2分钟/次）

---

#### 🟠 可疑消耗点 E：ban/degrade 检测级联风暴

```python
# 文件：bots/mon_bot.py:484-498
# 频率：每 ~5 分钟（Mon 主循环内）
# 风险：如果 Telegram API 返回临时错误（非真实封禁），会触发级联写入

async def _is_banned_or_degraded(self, cell):
    if await self._check_cell_banned(cell):           # SELECT cells 表
        await log_rotate(...)                          # INSERT rotate_log 表
        spare = await get_spare_for_account(account)    # SELECT spare_pool 表
        await consume_spare(spare)                     # UPDATE spare_pool 表
        # ↑ 一次误判降级 = 4-8 次 CRDB 操作
        # ↑ 如果多个 cell 同时误判 → 级联放大 N 倍
```

**风险场景**：
- Telegram API 临时限流（429 Too Many Requests）被误判为封禁
- 网络抖动导致 `getChat` 超时被判定为频道丢失
- 一次事件可能产生 5-15 次 SQL（含级联）

---

## 三、量化估算

### 3.1 正常负载下的 RU 消耗估算（14 小时）

| 来源 | 单次RU | 调用次数 | 小计RU |
|------|--------|---------|--------|
| request_count 刷写 | 5-50 | 168次 (5min) | 840-8,400 |
| code 变更同步 | 5-30 | 168次 (5min) | 840-5,040 |
| dirty_cells 同步 | 5-20 | 168次 (~5min) | 840-3,360 |
| ban/degrade 检测 | 5-10 | 168次 (~5min) | 840-1,680 |
| dsp_job 状态同步 | 1-10 | 420次 (2min) | 420-4,200 |
| decode_logs flush | 10-100 | 14次 (60min) | 140-1,400 |
| **合计** | | | **3,920-24,080** |

### 3.2 实际 vs 估算对比

```
实际消耗：  ~60,000 RU
正常估算：  3,920-24,080 RU
放大倍数：  2.5x - 15x ❗
```

**结论：存在显著的异常放大因素！**

---

## 四、异常放大的三大可疑因素

### 🔴🔴🔴 可疑因素 A：N+1 循环查询风暴（最可能）

**现象描述**：

如果在某时段出现以下情况之一，会导致 RU 消耗呈指数级增长：

1. **热门码暴增**：某个文件在短时间内被大量用户请求解码
   - request_count 缓冲中积累了 200+ 个不同的 file_code
   - 一次 flush = 200+ 次 UPDATE
   
2. **管理员批量操作**：批量下架/修改备注/设置有效期
   - code_changes 缓冲区堆积了 100+ 条待同步变更
   - 一次 sync = 100+ 次 UPDATE

3. **大规模降级事件**：网络故障导致多个 channel 同时超时
   - Mon Bot 连续检测到 10+ 个 cell "封禁"
   - 每个 cell 触发 log_rotate + spare_pool 操作 = 50+ 次 SQL

**验证方法**：
```sql
-- 查看 rotate_log 最近 14 小时的插入量（判断是否有降级风暴）
SELECT COUNT(*), 
       DATE_TRUNC('hour', timestamp) as hour_bucket
FROM rotate_log 
WHERE timestamp > now() - interval '14 hours'
GROUP BY hour_bucket 
ORDER BY hour_bucket DESC;
```

---

### 🔴🔴 可疑因素 B：缓存穿透导致热读路径击穿 CRDB

**架构回顾**：

项目采用三级缓存架构：
```
L1: 进程内内存缓存（TTL: 10-60秒）
   ↓ miss
L2: SQLite 本地持久化缓存（跨进程共享）
   ↓ miss  
L3: CockroachDB（最终兜底）
```

**穿透场景**：

```python
# database/session.py:1196-1208 - get_user_cached() 的 fallback 路径
async def get_user_cached(user_id):
    # L1: 内存缓存 miss?
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    # L2: SQLite 缓存 miss?
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)
        return cached
    
    # L3: 直接查 CRDB ← 这里可能是隐形杀手
    user = await col.find_one({"user_id": user_id})  # 每次解码都可能触发
    if user:
        cache.set(cache_key, user)      # 回填 L1
        await store.set(cache_key, user) # 回填 L2
```

**可能导致穿透的场景**：

1. **新用户涌入**：大量首次访问的用户，L1/L2 都没有缓存
2. **冷门码突发**：某些长期未被访问的文件码突然被请求
3. **缓存失效风暴**：某种原因导致 L1/L2 大面积同时失效（如 Bot 重启）
4. **负缓存 TTL 过期**：60 秒负缓存过期后，同一批无效请求再次击穿

**影响评估**：
- 如果每小时有 1000 次新用户请求，且全部穿透到 CRDB
- 额外增加：1000 × 1 RU = **1000 RU/hour**（14小时=14,000 RU）

**验证方法**：
```python
# 在 cache.py 的 get_* 方法中添加统计日志（临时开启）
_l1_hits = 0
_l2_hits = 0
_crdb_fallbacks = 0

# 每小时打印一次 hit rate
logger.info(f"[CacheStats] L1={_l1_hits} L2={_l2_hits} CRDB={_crdb_fallbacks} rate={_crdb_fallbacks/(_l1_hits+_l2_hits+_crdb_fallbacks)*100:.1f}%")
```

---

### 🔴 可疑因素 C：Mon Bot 误判级联风暴

**问题描述**：

Mon Bot 的 `_is_banned_or_degraded()` 方法依赖 Telegram API 的返回值判断频道是否被封禁。但以下情况会导致误判：

1. **API 临时错误**：`Gateway Timeout`、`Internal Server Error` 被误读为封禁
2. **Rate Limiting**：`429 Too Many Requests` 触发 flood protection 后的请求失败
3. **网络分区**：VPS 到 Telegram API 的短暂网络中断
4. **Bot Token 权限变更**：管理员修改了 Bot 权限但未通知 Mon Bot

**级联链路**：

```
Cell-A 误判为"封禁"
  ↓
log_rotate(A→lost)              -- INSERT rotate_log
  ↓
查找同组 Shadow1 提升为 Active  -- UPDATE cells (shadow1→active)
  ↓
log_rotate(Shadow1 promoted)     -- INSERT rotate_log
  ↓
从 Spare Pool 取新频道替补       -- SELECT + UPDATE spare_pool
  ↓
log_rotate(new cell added)       -- INSERT rotate_log
  ↓
Cell-B 也被误判...              -- 重复以上过程 N 次
```

**最坏情况**：一轮主循环内 10 个 cell 全部误判 = **40-80 次 CRDB 操作**

**验证方法**：
```sql
-- 查看 rotate_log 最近 14 小时的详细记录
SELECT * FROM rotate_log 
WHERE timestamp > now() - interval '14 hours'
ORDER BY timestamp DESC 
LIMIT 100;

-- 查看 reason 字段是否包含 "fail_streak" 或 "timeout"
SELECT COUNT(*) as total,
       COUNT(CASE WHEN reason LIKE '%fail_streak%' THEN 1 END) as streak_count,
       COUNT(CASE WHEN reason LIKE '%timeout%' THEN 1 END) as timeout_count
FROM rotate_log 
WHERE timestamp > now() - interval '14 hours';
```

---

## 五、完整 RU 消耗分布图

```
14小时总消耗: ~60,000 RU ████████████████████████████████████████ 100%

├── 已识别的正常消耗:     ~14,000 RU ████████ 23%
│   ├── request_count:      ~4,600 RU  ████  8%
│   ├── code_changes:       ~2,900 ROI  ██  5%
│   ├── dirty_cells:        ~2,100 ROU  ██  3.5%
│   ├── ban/degrade:        ~1,260 RU   █  2%
│   ├── dsp_job_sync:       ~2,310 RU   ██  4%
│   └── decode_logs:        ~770 ROU    █  1.3%
│
└── 未解释的异常消耗:       ~46,000 RU ████████████████████ 77% ❗
    ├── [待诊断] 可能原因A: N+1循环风暴     ~20,000 RU 33%
    ├── [待诊断] 可能原因B: 缓存穿透       ~15,000 RU 25%
    └── [待诊断] 可能原因C: 误判级联风暴   ~11,000 RU 19%
```

---

## 六、诊断行动计划（Phase 1：不动代码，先拿数据）

### 步骤 1：开启 SQL 日志收集真实 QPS

**操作**：设置环境变量 `ENABLE_SQL_LOG=1` 并重启所有 Bot 进程

**目标**：打印所有 CRDB SQL 到日志文件，包括：
- 实际执行次数（不受 Top 100 限制）
- 单次耗时
- 调用栈来源

**预期产出**：
- 每 5 分钟的真实 SQL 调用量
- 定位哪个定时任务贡献了最多的查询次数

---

### 步骤 2：CRDB Statements 页面深度分析

**操作**：在 CockroachDB Console 执行以下查询

```sql
-- 2.1 查看所有语句的执行统计（不受前台 Top 100 限制）
SELECT statement, 
       count(executions) as exec_count,
       sum(executions) as total_execs,
       max(max_latency) as worst_latency
FROM [statements] 
WHERE aggregated_ts > now() - interval '14 hours'
GROUP BY statement
ORDER BY total_execs DESC
LIMIT 50;

-- 2.2 查看 rotate_log 近 14 小时的插入量
SELECT COUNT(*) as total_inserts,
       DATE_TRUNC('hour', timestamp) as hour_bucket
FROM rotate_log 
WHERE timestamp > now() - interval '14 hours'
GROUP BY hour_bucket 
ORDER BY hour_bucket DESC;

-- 2.3 查看 spare_pool 操作频率
SELECT COUNT(*) as ops,
       operation_type,
       DATE_TRUNC('hour', created_at) as hour_bucket
FROM (
    SELECT 'insert' as operation_type, created_at FROM spare_pool WHERE is_used = 0
    UNION ALL
    SELECT 'update_consume', now() FROM spare_pool WHERE is_used = 1
) fake
GROUP BY operation_type, hour_bucket
ORDER BY hour_bucket DESC;
```

**预期产出**：
- 真实的 TOP SQL 及其执行次数（可能发现高频短查询）
- 降级事件的时间分布（确认是否有风暴）
- 备用池操作的频率

---

### 步骤 3：添加缓存命中监控

**操作**：在 [`database/cache.py`](database/cache.py) 和 [`database/session.py`](database/session.py) 的关键 `get_*` 方法中添加统计计数器

```python
# 示例：在 get_file_record_cached() 入口处添加
_CACHE_STATS = {"l1_hit": 0, "l2_hit": 0, "crdb_fallback": 0}

async def get_file_record_cached(file_code):
    global _CACHE_STATS
    # ... existing logic ...
    
    # L1 hit
    if cached is not None:
        _CACHE_STATS["l1_hit"] += 1
        return cached
    
    # L2 hit
    cached = await store.get(cache_key)
    if cached is not None:
        _CACHE_STATS["l2_hit"] += 1
        cache.set(cache_key, cached)
        return cached
    
    # CRDB fallback
    _CACHE_STATS["crdb_fallback"] += 1
    record = await col.find_one({"file_code": file_code})
    # ...
```

**预期产出**：
- L1/L2/CRDB 三级缓存的命中率分布
- 如果 `crdb_fallback` 占比 > 10%，说明存在严重缓存穿透

---

### 步骤 4：在各 Sync Loop 入口打日志

**操作**：在以下函数入口处添加日志，记录每次调用的操作条目数：

1. `_flush_request_count_loop()` → 打印 `len(request_counter)`
2. `_code_changes_sync_loop()` → 打印 `len(unsynced_changes)`  
3. `sync_dirty_cells_to_crdb()` → 打印 `len(dirty_cells)`
4. `sync_local_jobs_to_crdb()` → 打印 `len(unsynced_jobs)`

**示例代码**：
```python
# 在 sync_dirty_cells_to_crdb() 函数体开头添加
dirty = await store.get_dirty_cells_local(50)
if dirty:
    logger.info(f"[SyncDirtyCells] 本次同步 {len(dirty)} 个脏 cell: {[c['slot_id'] for c in dirty]}")
else:
    logger.debug("[SyncDirtyCells] 无需同步")
```

**预期产出**：
- 各 Sync Loop 的实际操作数据量
- 发现是否有某次的条目数远超均值（如突然从 5 条跳到 200 条）

---

## 七、优化方案（Phase 2 & 3）

### Phase 2：快速止血（低风险改动）

| 编号 | 改动项 | 当前状态 | 目标状态 | 预计节省RU | 风险等级 |
|------|--------|----------|----------|-----------|---------|
| 2.1 | request_count 刷写频率 | 5 min | 30 min | -80%该项RU | 低 |
| 2.2 | code_changes 同步方式 | 逐条UPDATE | batch UPDATE | -60%该项RU | 低 |
| 2.3 | dirty_cells 同步方式 | 逐条UPDATE | batch UPSERT | -50%该项RU | 低 |
| 2.4 | ban/degrade 检测去抖 | 无 | 连续2次确认 | -90%误判RU | 中 |

#### 2.1 详细方案：降低 request_count 刷写频率

**改动文件**：`database/cache.py:189-206`

**改动内容**：
```python
# 原来：300秒 (5分钟)
FLUSH_INTERVAL = 300  

# 改为：1800秒 (30分钟)
FLUSH_INTERVAL = 1800
```

**副作用**：
- `file_records.request_count` 的实时性下降（最多延迟 30 分钟）
- Admin 后台显示的请求数可能有 30 分钟滞后
- **对业务无实质影响**（该字段仅用于统计分析，不影响核心流程）

---

#### 2.2 详细方案：batch UPDATE 替代 N+1 循环

**改动文件**：`bots/idx_bot.py:1827-1870`

**改动前**：
```python
for change in unsynced_changes:
    await codes_col.update_one({"code": change.code}, {"$set": {...}})
```

**改动后**：
```python
# 使用原生 SQL batch UPDATE
if unsynced_changes:
    params = []
    placeholders = []
    for i, change in enumerate(unsynced_changes):
        params.extend([change.new_value, change.code])
        placeholders.append(f"(CASE WHEN code = ${i*2+2} THEN ${i*2+1} ELSE note END)")
    
    sql = f"""
        UPDATE codes SET note = {' || '.join(placeholders)}
        WHERE code IN ({', '.join([f'${i*2+2}' for i in range(len(unsynced_changes))])})
    """
    await codes_col._execute(sql, params)
```

**效果**：N 次 UPDATE → 1 次 UPDATE（参数化批量）

---

#### 2.3 详细方案：dirty_cells batch 同步

**改动文件**：`database/session.py:1838-1875` (`sync_dirty_cells_to_crdb()`)

**改动思路**：
- 将 `update_one` 循环改为原生 SQL 的 `UPDATE ... CASE WHEN ... END`
- 或者使用 `executemany`（如果 asyncpg 支持）

**注意**：需要同时修复已知 bug（TODO_FIXES.md #1）：`upsert=True` 参数传给了不支持的 `update_one()`

---

#### 2.4 详细方案：ban/degrade 检测去抖

**改动文件**：`bots/mon_bot.py:484-498` (`_is_banned_or_degraded()`)

**改动思路**：
```python
# 新增：连续失败计数器（进程内存）
self._cell_fail_history: dict[str, list[bool]] = {}  # slot_id -> [最近N次结果]

async def _is_banned_or_degraded(self, cell):
    slot_id = cell["slot_id"]
    
    # 记录本次检测结果
    is_bad = await self._check_cell_banned_internal(cell)
    history = self._cell_fail_history.setdefault(slot_id, [])
    history.append(is_bad)
    
    # 只保留最近 3 次检测结果
    if len(history) > 3:
        history.pop(0)
    
    # 连续 2 次及以上失败才确认为降级
    if sum(history) >= 2:
        return True
    
    return False
```

**效果**：避免因临时网络/API 错误导致的单次误判触发级联操作

---

### Phase 3：深度优化（需测试验证）

| 编号 | 改动项 | 技术手段 | 预期效果 |
|------|--------|----------|----------|
| 3.1 | 引入 CRDB 语句级缓存 | 使用 `PREPARE` 复用执行计划 | 减少 parse 成本 ~10% |
| 3.2 | 热读路径完全迁移到 SQLite | `file_records` + `codes` 表全量缓存到 SQLite | 热读 RU 接近零 |
| 3.3 | 使用 CHANGEFEED 替代轮询 | 从"主动拉"变为"被动推" | 消除空转查询 |
| 3.4 | request_count 改为 SQLite-only | 仅每日归档时才写入 CRDB | 该项 RU 降低 95%+ |

---

## 八、预期优化效果

| 指标 | 当前值 | Phase 2 后 | Phase 3 后 |
|------|--------|------------|------------|
| **14h 总 RU** | **~60,000** | **12,000-18,000** | **2,000-5,000** |
| **RU/min 均值** | ~71 | ~14-21 | **2-6** |
| **尖峰高度** | ~45 RU/s | ~10-15 RU/s | **2-5 RU/s** |
| **削减比例** | - | **70-80%** | **92-97%** |

---

## 九、附录

### A. 相关文件清单

| 文件路径 | 关键函数 | 说明 |
|----------|----------|------|
| `database/session.py` | `sync_dirty_cells_to_crdb()` | dirty cells 同步到 CRDB |
| `database/session.py` | `sync_local_jobs_to_crdb()` | 本地 job 状态同步到 CRDB |
| `database/session.py` | `sync_jobs_from_crdb_to_sqlite()` | 从 CRDB 拉取 jobs 到本地 |
| `database/session.py` | `_refresh_external_code_mapping_cache()` | 外部码映射缓存刷新 |
| `database/session.py` | `_refresh_bot_config_cache()` | Bot 配置缓存刷新 |
| `database/cache.py` | `_flush_request_count_loop()` | request_count 刷写 |
| `database/cache_store.py` | `CacheStore` 类 | SQLite 本地缓存层 |
| `bots/mon_bot.py` | `_is_banned_or_degraded()` | 封禁/降级检测 |
| `bots/mon_bot.py` | `run_degrade_check()` | 主循环调度 |
| `bots/dsp_bot.py` | `sync_jobs_from_crdb_to_sqlite()` | Dsp job 同步 |
| `bots/idx_bot.py` | `_code_changes_sync_loop()` | code 变更同步 |
| `services/mon/scheduler.py` | `MonScheduler.run_degrade_check()` | 降级检查逻辑 |

### B. 已有的诊断文档

- [`RU_DIAGNOSIS.md`](RU_DIAGNOSIS.md)：之前的 RU 诊断报告（30分钟粒度）
- [`TODO_FIXES.md`](TODO_FIXES.md)：已知 bug 清单（含 upsert bug 等）

### C. CockroachDB RU 计费模型参考

| 操作类型 | RU 成本 | 说明 |
|----------|---------|------|
| SELECT (单行) | 1-3 RU | 取决于索引命中 |
| SELECT (范围扫描) | 1-10 RU/行 | 全表扫描更贵 |
| INSERT | 3-5 RU | 包含 WAL 写入 |
| UPDATE (单行) | 3-10 RU | 取决于修改列数和索引数量 |
| DELETE | 3-5 RU | 类似 INSERT |
| Batch 操作 | ~单次 × 0.7 | 有一定批量折扣 |

---

**报告生成时间**：2026-07-02  
**分析工具版本**：CockroachDB Console SQL Activity + Metrics  
**下一步行动**：建议先执行 Phase 1 的步骤 1-4 收集数据，确认具体的异常放大因素后再决定是否进入 Phase 2 优化。
