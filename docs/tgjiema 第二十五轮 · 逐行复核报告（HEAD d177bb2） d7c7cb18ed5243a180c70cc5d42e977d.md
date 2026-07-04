# tgjiema 第二十五轮 · 逐行复核报告（HEAD d177bb2）

<callout icon="🧯">**审查对象**：`maxiuquan/tgjiema` @ `d177bb2`（parent `73b5632`）

**时间**：2026-07-03 19:18 (Asia/Shanghai)

**结论先行**：**暂不可上线** —— N24-1 / N24-4 本轮确认真修，N24-2 已缓解；但 🔴 BLOCKER-1（R2 `base_url` 双花括号）**连续 5+ 轮仍未修**，且本轮逐行读完 `db_restore.py` 后新发现 2 个恢复链路数据正确性问题。</callout>

## 一、本轮变更与验证（commit `d177bb2`）

提交信息：“N24-1: R100追赶解耦continue + N24-4: 影子频道映射缺失禁止回退盲发 + N24-2: 缩短游标落盘间隔至10轮”。diff = **+51/−37**，改动 3 个文件：`services/mon/scheduler.py`、`storage/delivery_resolver.py`、`bots/dsp_bot.py`。

| 上轮问题 | 本轮状态 | 验证依据 |
| --- | --- | --- |
| 🟠 N24-1（R100 静默追赶缺口） | ✅ **真修** | `replicate_all_active_to_shadows` 已重排：R100 块移到 shadow 的 `if not new_messages: continue` **之前**，且使用独立变量 `r100_new_messages`（由 `r100_last_cursor` 取数，独立 fetch）——无 NameError，静默期 R100 也能追赶 |
| 🟠 N24-4（影子 msg_id 回退盲发） | ✅ **真修** | `resolve_backup_msg_id(main_msg_id, channel_id, original_channel_id)` 新增第三参；当 `channel_id != original_channel_id` 且映射缺失时返回 `None` → `try_deliver` 返回 False 换下一频道。dsp_bot **全部 5 个调用点**均已传 `original_channel_id` |
| 🟡 N24-2（重启重复窗口） | ⚠️ **部分缓解** | `_cursor_flush_interval` 30→10（约 10 分钟）；但目的端仍未按 `(main_msg_id, backup_channel_id)` 去重，崩溃窗口内（≤8 个周期）仍有重复归档可能 |

## 二、🔴 阻断级问题（仍未修复）

<callout icon="🔴">**BLOCKER-1 · `storage/r2.py` `base_url` 双花括号 —— 连续 5+ 轮未修（唯一硬阻断）**

本轮直接读取 `d177bb2` 的 `storage/r2.py`，第 35 行仍为：

```python
@property
def base_url(self) -> str:
    return f"{{https://{self._endpoint}}}/{self._bucket}"
```

 /  在 f-string 中是转义字面量，实际生成 `{https://<endpoint>}/<bucket>` → httpx `InvalidURL`。`upload/download/delete/list_objects` 全部依赖 `base_url` → **所有 R2 读写失效**。

**本轮影响升级**：现已确认 `services/db_backup.py`（`r2_storage.upload`）**与** `services/db_restore.py`（`r2_storage.list_objects` + `download`）**两条灾备链路都直接调用 `base_url`** → 备份写不进、恢复读不出，**整个 R2 容灾网全部不可用**。

**修复（一行）**：`return f"{{https://{self._endpoint}}}/{self._bucket}"`</callout>

## 三、🟠 本轮新发现（逐行读完 `db_restore.py` / `db_backup.py`）

<callout icon="🟠">**N25-1 · `db_restore.py` 对 `message_backups` 用单列主键 `main_msg_id` 恢复 → 备份映射丢失/报错**

`db_restore.py` 中 `TABLE_PK["message_backups"] = "main_msg_id"`，恢复时生成 `INSERT ... ON CONFLICT (main_msg_id) DO UPDATE`。

但 `message_backups` 的语义主键是 **复合键 `(main_msg_id, backup_channel_id)`** —— 同一个 `main_msg_id` 会备份到 shadow1 / shadow2 / R100 **多个频道**，对应多行。后果二选一：

- 若 DB 中 `main_msg_id` 非唯一索引 → `ON CONFLICT (main_msg_id)` **无对应唯一约束，asyncpg 直接报错** → 整表 `message_backups` 恢复失败（逐行被 catch 后跳过）；
- 苹当作唯一键处理 → 同一 `main_msg_id` 的多条备份映射被折叠成一条 → **丢失其余频道的 msg_id 映射**。

两种情况都使恢复后 `delivery_resolver.resolve_backup_msg_id` 查不到影子/R100 映射 → 叠加 N24-4 修复后会直接跳过该频道 → 灾后取件失败。

**修复建议**：`message_backups` 恢复改用复合键 `ON CONFLICT (main_msg_id, backup_channel_id)`（需 DB 存在对应复合唯一约束）。</callout>

<callout icon="🟠">**N25-2 · `db_restore._safe_val` 将 bool 转成 int 1/0 → 恢复 BOOL 列时 asyncpg 可能报错**

```python
def _safe_val(val):
    if isinstance(val, bool):
        return 1 if val else 0   # ← bool → int
```

备份 JSON 中 `can_upload / is_banned / protect_content / is_r100 / is_used / is_active / dead / enabled` 等字段为 `true/false`，`json.loads` 后为 Python `bool`，经 `_safe_val` 变成 `1/0` int。asyncpg 对类型严格：若对应列在 CRDB 为 `BOOL` 类型，传 int 会抛 `invalid input for type boolean` → **该行恢复失败（被 catch 静默跳过）**。

需对照 `database/session.py` 的 `CREATE TABLE` 确认这些列的实际类型（本轮未重读 [session.py](http://session.py)，见诚实声明）；若为 BOOL 则为真实缺陷。

**修复建议**：bool 保留为 bool（或根据目标列类型转换），不要一律转 int。</callout>

## 四、🟡 中危 / ℹ️ 提示

<callout icon="🟡">**N25-3 · `db_backup.py` 对 `message_backups` 有 5000 行上限 + 无去重**

`MAX_ROWS_PER_TABLE=5000` 对 `message_backups` 同样生效。繁忙系统下映射行数远超 5000（每条归档消息×备份频道数） → 备份**不完整** → 恢复时无法重建完整映射。建议对 `message_backups` 取消行数上限或改用分页全量备份。</callout>

<aside>
ℹ️

**N25-4（轻微性能观察）**：R100 重排后，每个 Active 槽每个周期都会**无条件**多跑一次 `_fetch_new_messages`（R100 一次 + shadow 一次），即使无新消息。即 15 组 × 2 次 Telethon `iter_messages`/周期。功能正确，但在大拓扑下会增加 Telethon 调用量，建议观察限速。

</aside>

<aside>
ℹ️

**提示（沿用）**：同账号匹配替换依赖 spare/cells 的 `account_name` 已填充；`add_spare_channel` 建议强制要求账号，避免退化为跨账号取备用。

</aside>

## 五、本轮逐行通读情况（诚实声明）

**本轮新完整逐行读完：**

| 文件 | 本轮状态 |
| --- | --- |
| `storage/r2.py` | ✅ 完整（确认 base_url 仍 bug） |
| `services/mon/scheduler.py`（replicate/flush/fetch 区） | ✅ 重排后全部确认 |
| `storage/delivery_resolver.py`（diff） | ✅ 全部改动确认 |
| `bots/dsp_bot.py` | ✅ **本轮读完 769/769 行**（上轮仅至 632） |
| `services/db_restore.py` | ✅ **本轮首次完整逐行（269 行）** |
| `services/db_backup.py` | ✅ 上轮完整逐行 |

## 六、尚未在本轮逐行重读的大文件（诚实声明）

下列文件在 `d177bb2` 相对 `73b5632` **零改动**（commit 文件清单已证实），行为与前轮一致，本轮**未再逐行重读**：

| 文件 | 大小 | 备注 |
| --- | --- | --- |
| `database/session.py` | 91,459 B | **N25-2 需对照此文件确认 BOOL 列类型**，下轮应优先读 |
| `bots/idx_bot.py` | 75,346 B | 前轮 |
| `database/cache_store.py` | 75,531 B | 仅定点核实 get/set |
| `bots/admin_bot/handlers.py` | 43,831 B | 前轮 |
| `services/relay_instance.py` | 37,955 B | 前轮 |
| `bots/up_bot.py` | 35,841 B | 前轮（尾部未读完） |

## 七、上线前检查清单

- [ ]  🔴 修复 `storage/r2.py` `base_url` 双花括号（阻断所有 R2 读写 + 备份/恢复）
- [ ]  🟠 N25-1：`message_backups` 恢复改用复合键 `(main_msg_id, backup_channel_id)`
- [ ]  🟠 N25-2：核对 [session.py](http://session.py) BOOL 列；`_safe_val` 不要把 bool 一律转 int
- [ ]  🟡 N25-3：`message_backups` 备份取消 5000 行上限或分页全量
- [ ]  修复 R2 后实测：备份写入 → `db_restore --dry-run` → 实际恢复全链路（重点验 message_backups / bool 列）
- [ ]  R100 端到端冒烟：静默期追赶 + 重启不重复

## 八、结论

**暂不可上线。** N24-1 / N24-4 本轮确认真修，N24-2 已部分缓解；但 🔴 BLOCKER-1（R2 `base_url`）连续 5+ 轮仍未修，且同时阻断备份与恢复两条链路；本轮逐行读完恢复链路后又新增 🟠 N25-1（message_backups 单键恢复）与 🟠 N25-2（bool→int 恢复）两个灾备数据正确性问题。建议优先修 BLOCKER-1，再修 N25-1/N25-2，并跑一次完整的 R2 备份→恢复冒烟。