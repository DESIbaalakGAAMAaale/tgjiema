# CockroachDB RU 异常消耗排查方案

> 截图时段：近 30 分钟 | 前台显示：+5.52k RU  
> 图表特征：Request Units 每 **~5 分钟**出现一次周期性尖峰，CPU 同步飙升，Reads 同步增长

---

## 一、RU 尖峰根因定位

截图中 RU 曲线的 **5 分钟周期性尖峰** 与代码中以下定时任务的间隔完全吻合：

| 定时任务 | 间隔 | 文件 | 预估单次 RU |
|---|---|---|---|
| `_flush_request_count_loop` | **300s (5 min)** | `database/cache.py:189-206` | 1-50 |
| `_code_changes_sync_loop` | **300s (5 min)** | `bots/idx_bot.py:1827-1870` | 1-30 |
| `sync_dirty_cells_to_crdb()` | **~300s (~5 min)** | `bots/mon_bot.py:532-537` | 1-20 |
| `_is_banned_or_degraded()` | **~300s (~5 min)** | `bots/mon_bot.py:484-498` | 1-10 |

**结论：5 分钟周期的多个同步任务叠加触发是 RU 尖峰的直接原因。**

---

## 二、各消耗点详细分析

### 🔴 高消耗点（需优先关注）

#### 1. request_count 刷写 — 逐条 `$inc` UPDATE

```
文件: database/cache.py:189-206
频率: 每 5 分钟
SQL: update_file_record_and_invalidate(code, {"$inc": {"request_count": count}}) × N 个不同 file_code
```

**问题：**
- 内存中累积了所有热门码的计数后，**逐条 UPDATE 到 CRDB 的 `file_records` 表**
- 如果 5 分钟内有 100 个不同的码被解码 → 100 次 UPDATE
- 每次 UPDATE = 1 次 SQL round-trip + 行锁

**优化方向：**
- 改用批量 `UPDATE ... SET request_count = request_count + $1 WHERE code IN (...)`
- 或将计数合并到 SQLite 层，仅在需要审计时才刷到 CRDB（降低频率到 30min/60min）

#### 2. code 变更同步 — N+1 UPDATE

```
文件: bots/idx_bot.py:1827-1870
频率: 每 5 分钟
SQL: codes_col.update_one({"code": code}, {"$set": {...}}) × N 条变更
```

**问题：**
- 用户操作（下架、备注修改、有效期设置）产生的 code 变更缓冲在内存
- 每 5 分钟 **逐条 UPDATE 到 `codes` 表**
- 典型负载下每次可能 10-50 条变更

**优化方向：**
- 改为 batch UPDATE: `UPDATE codes SET note=$1, expiry=$2, status=$3 WHERE code IN ($4, $5, ...)`
- 或使用 `executemany` / CTE 批量更新

#### 3. dirty cells 同步 — 含已知 bug

```
文件: bots/mon_bot.py:532-537 (调用 session.py ~1947-1949)
频率: 每 ~5 分钟 (每 10 轮 Mon 主循环)
SQL: update_one({"cell_key": k}, {"$set": v}, upsert=✗) × N 个脏 cell
```

**问题（双重）：**
- ⚠️ **已知 bug**: `upsert=True` 参数传给了不支持的 `update_one()`，会导致 TypeError（见 TODO_FIXES.md #1）
- 即使修复 bug 后，仍然是逐条 UPDATE 模式
- 正常运行时脏 cell 数通常较少（<10），但异常事件（大规模降级/轮转）时可能突增

#### 4. ban/degrade 检测 — SELECT + 可能的级联写入

```
文件: bots/mon_bot.py:484-498
频率: 每 ~5 分钟 (Mon 主循环内)
SQL: get_cell_by_key() SELECT cells 表 × N 个活跃 cell
```

**问题：**
- 对每个活跃 cell 执行一次 SELECT 检测封禁状态
- 如果检测到异常，会级联触发：
  - `log_rotate()` INSERT rotate_log 表
  - `get_spare_for_account()` SELECT spare_pool 表
  - `consume_spare()` UPDATE spare_pool 表
- **一次异常事件可能产生 5-15 次 SQL**

### 🟡 中等消耗点

#### 5. decode_logs flush — 60 分钟集中 burst

```
文件: database/cache.py:300-409
频率: 每 60 分钟
SQL: batch INSERT decode_log (100条/批) + 分批 DELETE 清理旧数据(5000条/批)
```

**影响：** 60 分钟一次大写入，RU 图上会看到一个大尖峰。但频率低，总体贡献不大。

#### 6. dsp_bot job 状态同步 — 120 秒

```
文件: bots/dsp_bot.py:702-709
频率: 每 120 秒
SQL: UPDATE jobs SET status=... WHERE id=$1 × N 条已完成 job
```

**影响：** 频率较高但每次数据量小（仅已完成 job），总体可控。

### 🟢 已优化（零 RU 或极低）

| 功能 | 文件 | 说明 |
|---|---|---|
| Cell 缓存读取 | `mon_bot.py`, `idx_bot.py` | 走 SQLite 本地缓存，不碰 CRDB ✅ |
| 频道列表刷新 | `idx_bot.py:1820-1824` | `get_active_cells_local()` 读 SQLite ✅ |
| 心跳检测 | `mon_bot.py` | 只写 SQLite ✅ |
| 轮转判断 | `mon_bot.py` | 只写 SQLite ✅ |

---

## 三、5.52k RU / 30min 是否合理？

粗略估算：

| 来源 | 单次 RU | 30min 内次数 | 小计 RU |
|---|---|---|---|
| request_count 刷写 | 5-50 | 6 次 | 30-300 |
| code 变更同步 | 5-30 | 6 次 | 30-180 |
| dirty cells 同步 | 5-20 | 6 次 | 30-120 |
| ban/degrade 检测 | 5-10 | 6 次 | 30-60 |
| dsp_job 状态同步 | 1-10 | 15 次 | 15-150 |
| decode_logs flush | 10-100 | 0.5 次 | 5-50 |
| **正常估算合计** | | | **140-860 RU** |

**5.52k RU 是正常估值的 6-40 倍！** 存在异常放大因素。

---

## 四、异常放大的可能原因

### 可能原因 A：缓存穿透（最可疑 🔴）

如果 L1/L2 缓存频繁失效或未命中，大量本应走缓存的读请求直接打到 CRDB：

```python
# database/cache.py 的 fallback 逻辑
async def get_file_record(code):
    # L1 memory miss?
    # L2 SQLite miss?
    # → 直接查 CRDB ← 这一步可能是 RU 放大的主因
    return await db.file_records_col.find_one(...)
```

**检查方法：**
- 在 cache.py 各 `get_*` 方法中加日志统计 hit/miss rate
- 特别关注 `get_file_record()`, `get_code_entry()`, `get_cell_by_key()` 三个高频路径

### 可能原因 B：N+1 循环被高频触发

如果有外部因素导致某个循环内的查询数量异常膨胀：

- `request_count` 缓冲中积累了远超预期的不同 code 数量
- `code_changes` 缓冲因用户操作风暴而暴增
- `dirty_cells` 在某次大规模降级事件中积累大量条目

**检查方法：**
- 日志打印每次 flush/sync 的实际操作条目数
- 观察是否有某次的条目数远超均值（如突然从 5 条跳到 200 条）

### 可能原因 C：ban/degrade 检测级联风暴

如果 Telegram API 返回临时错误（非真实封禁），Mon Bot 可能误判为降级并触发级联操作：

```
检测到 "降级" → log_rotate INSERT → 查 spare_pool → consume_spare UPDATE
              → 下一个 cell 也检测到... (重复 N 次)
```

**检查方法：**
- 查看 `rotate_log` 表最近 30 分钟的 INSERT 记录数
- 查看 spare_pool 表的操作记录

---

## 五、诊断与修复方案（分阶段）

### Phase 1：诊断（不动代码，先拿数据）

| 步骤 | 操作 | 目的 |
|---|---|---|
| 1.1 | 在 `cache.py` 所有 `get_*` 方法加 counter 日志 | 确认 L1/L2 hit rate |
| 1.2 | 在所有 sync loop 入口打日志记录操作条目数 | 确认是否某次 flush 数据量异常 |
| 1.3 | 查询 CRDB `SELECT COUNT(*) FROM rotate_log WHERE ts > now() - interval '30 minutes'` | 确认是否有降级风暴 |
| 1.4 | 开启 CRDB `SHOW STATEMENTS` 或查看 Console 的 Statements 页面 | 看 TOP SQL 及其执行次数 |

### Phase 2：快速止血（低风险改动）

| 编号 | 改动 | 预计节省 RU | 风险 |
|---|---|---|---|
| 2.1 | `request_count` 刷写频率 5min → 30min | -80% 该项 RU | 低（计数延迟可接受） |
| 2.2 | `code_changes` 同步改为 batch UPDATE | -60% 该项 RU | 低 |
| 2.3 | `dirty_cells` 同步改为 batch UPSERT | -50% 该项 RU | 低（同时修复 #1 bug） |
| 2.4 | ban/degrade 检测增加去抖（连续 2 次才确认） | -90% 误判级联 RU | 中 |

### Phase 3：深度优化（需测试）

| 编号 | 改动 | 预计效果 |
|---|---|---|
| 3.1 | 引入 CRDB 语句级缓存（`PREPARE` 复用） | 减少 parse 成本 |
| 3.2 | 将 `file_records` 和 `codes` 的热读路径完全迁移到 SQLite | 热读 RU 接近零 |
| 3.3 | 使用 CRDB 的 `CHANGEFEED` 替代轮询模式 | 从"主动拉"变"被动推"，消除空转查询 |
| 3.4 | request_count 改为仅存 SQLite，CRDB 仅每日归档一次 | 该项 RU 降低 95%+ |

---

## 六、预期目标

| 指标 | 当前 | Phase 2 后目标 | Phase 3 后目标 |
|---|---|---|---|
| 30min RU 总量 | **~5,520** | **~1,000-1,500** | **~200-500** |
| RU/min 均值 | ~184 | ~33-50 | ~7-17 |
| 尖峰高度 | ~8 RU/次 | ~3 RU/次 | ~1 RU/次 |

**Phase 2 预计可削减 70-80% RU 消耗。**
