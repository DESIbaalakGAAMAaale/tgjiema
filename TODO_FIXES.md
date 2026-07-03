# 待修复问题清单

> 生成日期：2026-07-01  
> 已排除：Bot Token 明文存储、中继账号 api_hash 无加密存储、文件内容无扫描/过滤

---

## 一、高危漏洞（4 项）

### 1. `upsert=True` 传递给不支持的 `update_one` —— 必将报错

| 字段 | 内容 |
|---|---|
| 文件 | `database/session.py` |
| 行号 | ~1947-1949 |
| 函数 | `sync_dirty_cells_to_crdb()` |

**问题：** `update_one()` 方法不支持 `upsert=True` 参数，调用时会直接抛 `TypeError`。

**影响：** 脏 Cell 同步到 CRDB 功能完全不可用，可能导致 Cell 状态变更丢失。

**修复方向：** 改用原生 SQL 的 `INSERT ... ON CONFLICT ... DO UPDATE`，或为 `update_one` 增加 upsert 支持。


### 2. `_process_pending_uploads()` 竞态条件

| 字段 | 内容 |
|---|---|
| 文件 | `bots/idx_bot.py` |
| 行号 | ~581-614 |

**问题：** 多个 worker 可能同时处理同一条 pending 记录，因为查询 pending 和标记"处理中"之间存在时间窗口，没有原子锁。

**影响：** 同一文件可能被重复解码/索引，产生重复 code 条目。

**修复方向：** 使用 `SELECT ... FOR UPDATE` 或 CRDB 事务 + 状态字段（如 `status = 'processing'`）实现乐观锁。


### 3. `_is_channel_nearly_empty()` / `_fetch_all_media()` 死代码

| 字段 | 内容 |
|---|---|
| 文件 | `services/mon/scheduler.py` |

**问题：**
- `_is_channel_nearly_empty()` 总是返回 `False`
- `_fetch_all_media()` 总是返回 `[]`

**影响：** auto-fill（自动填充新频道）功能形同虚设，频道切换后新频道无历史文件可检索。

**修复方向：** 补全这两个方法的实际实现，使其能真正获取频道消息历史并判断是否为空。


### 4. `_json_dumps` 中的 `UnboundLocalError`

| 字段 | 内容 |
|---|---|
| 文件 | `bots/up_bot.py` |
| 行号 | ~15-18 |

**问题：** 海象运算符 `isinstance(result := json.dumps(...), bytes)` 在条件为 `False`（即结果是 `str`）时，`result` 变量未定义，但函数末尾无条件 `return result`。

**影响：** 当 `json.dumps` 返回 `str` 类型（绝大多数情况）时，必然触发 `UnboundLocalError`，上传流程中断。

**修复方向：** 移除海象运算符，改为显式赋值：

```python
result = json.dumps(data, ensure_ascii=False)
if isinstance(result, bytes):
    result = result.decode('utf-8')
return result
```

---

## 二、中危缺陷（4 项）

### 5. 不一致的返回值类型

| 字段 | 内容 |
|---|---|
| 文件 | `bots/idx_bot.py` |
| 行号 | ~700 |

**问题：** 正常情况下返回 `False` 表示失败/跳过，但错误分支中却 `return True`，返回值语义不一致。

**影响：** 调用方基于返回值做判断时逻辑错乱，可能导致错误被静默吞掉或成功被误判为失败。

**修复方向：** 统一返回值语义，建议错误时也返回 `False` 并记录日志。


### 6. `safe_reply_text` lambda 闭包捕获过期引用

| 字段 | 内容 |
|---|---|
| 文件 | `utils/flood_waiter.py` |

**问题：** `safe_reply_text` 中的 lambda 闭包捕获了 `message` 对象的引用。当 flood wait 时间较长导致重试时，`message` 可能已过期或被垃圾回收。

**影响：** 重试发消息时可能操作已失效的对象，导致异常。

**修复方向：** lambda 中捕获 `chat_id` + `text` 等不可变值，而非整个 `message` 对象。


### 7. 页面大小参数命名错误

| 字段 | 内容 |
|---|---|
| 文件 | `admin/` 相关模板或 API |

**问题：** 分页参数名使用了非标准命名（如 `PAGE_SIZE` 实际控制的是每页条数但命名暗示全局常量），容易在配置调整时产生误解。

**影响：** 维护性差，后续开发者可能错误修改。

**修复方向：** 重命名为语义明确的名称，如 `DEFAULT_PAGE_SIZE`、`ITEMS_PER_PAGE` 等。


### 8. 错误的导入路径

| 字段 | 内容 |
|---|---|
| 文件 | `database/cache.py` |
| 行号 | ~312 |

**问题：** `from utils.config import settings` 路径不准确，项目实际配置可能在 `config.settings`。

**影响：** 如果 `utils/config.py` 不存在或内容不一致，导入失败导致缓存模块不可用。

**修复方向：** 确认正确路径后修正为 `from config.settings import settings`。


---

## 三、低危问题（3 项）

### 9. `_login_failures` 内存泄漏

| 字段 | 内容 |
|---|---|
| 文件 | `admin/__init__.py` |

**问题：** 登录失败计数器字典 `_login_failures` 只增不减，从未清理过期 IP 记录。

**影响：** 遭受持续攻击时字典无限膨胀，内存缓慢增长。

**修复方向：** 添加定时清理任务（如每 10 分钟清理超过 1 小时的记录），或使用 `TTLCache`。


### 10. `_pending_media_groups` / `_external_buffers` 内存泄漏

| 字段 | 内容 |
|---|---|
| 文件 | `bots/up_bot.py` |

**问题：** 这两个字典用于暂存媒体组和外部缓冲数据，但没有过期清理机制。如果 Telegram 未发送完整媒体组，对应条目永远不会被移除。

**影响：** 长期运行后内存持续增长。

**修复方向：** 添加基于时间的过期淘汰（如超过 5 分钟未完成的条目自动清理）。


### 11. `RELAY_ENCRYPTION_KEY` 定义但未使用

| 字段 | 内容 |
|---|---|
| 文件 | `config/settings.py` |

**问题：** 配置中定义了 `RELAY_ENCRYPTION_KEY`，但整个代码库没有任何地方引用它，中继通信实际未加密。

**影响：** 要么是遗漏的加密功能（安全隐患），要么是废弃配置（代码清洁度问题）。

**修复方向：** 如确实需要加密中继通信，补齐加密逻辑；否则删除该配置项避免误导。


---

## 四、架构隐患（2 项）

### 12. 多进程缓存一致性问题

| 字段 | 内容 |
|---|---|
| 涉及 | `bots/mon_bot.py` + 各 bot 进程的本地缓存 |

**问题：** Mon bot 更新 Cell 状态后写入 CRDB，但其他 bot 进程的本地内存缓存（L1）最多 60 秒后才刷新。在此期间各进程看到的 Cell 拓扑不一致。

**影响：** 频道已切换但部分 bot 仍向旧频道发消息，导致消息丢失。

**修复方向：**
- 方案 A：使用 CRDB 的 `NOTIFY/LISTEN` 实现缓存失效广播
- 方案 B：缩短缓存 TTL 到 5-10 秒
- 方案 C：依赖 CRDB 写入时间戳，读取时检查 staleness


### 13. `dequeue_jobs` 原子性边界不足

| 字段 | 内容 |
|---|---|
| 文件 | `database/session.py` |

**问题：** 虽然使用了 CTE + `UPDATE ... RETURNING` 保证单次出队原子性，但 5 秒超时后的重试逻辑与原始操作的隔离边界不够清晰，极端情况下可能导致同一任务被两个 worker 认领。

**影响：** 低概率重复派发同一文件。

**修复方向：** 为 job 表增加 `worker_id` 字段，出队时写入唯一 worker 标识，配合 `WHERE worker_id IS NULL` 实现 CAS 语义。


---

## 修复优先级建议

| 优先级 | 编号 | 问题 | 理由 |
|---|---|---|---|
| **P0 立即** | #4 | `_json_dumps` UnboundLocalError | 上传功能必崩，直接影响主流程 |
| **P1 当天** | #1 | `upsert=True` 不支持 | Cell 同步完全不可用 |
| **P1 当天** | #8 | 错误导入路径 | 可能导致缓存模块加载失败 |
| **P2 本周** | #2 | pending_uploads 竞态 | 可能产生脏数据 |
| **P2 本周** | #5 | 不一致返回值 | 错误处理逻辑混乱 |
| **P2 本周** | #6 | lambda 闭包过期引用 | 重试场景可能异常 |
| **P3 本迭代** | #3 | 死代码 auto-fill | 功能缺失 |
| **P3 本迭代** | #7 | 分页参数命名 | 代码规范 |
| **P3 本迭代** | #9 | login_failures 泄漏 | 长期稳定性 |
| **P3 本迭代** | #10 | 媒体组缓冲泄漏 | 长期稳定性 |
| **P4 下迭代** | #11 | 未使用的加密 key | 安全/清洁度 |
| **P4 下迭代** | #12 | 多进程缓存一致性 | 需架构讨论 |
| **P4 下迭代** | #13 | dequeue 原子性 | 需架构讨论 |
