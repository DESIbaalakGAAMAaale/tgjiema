# Delivery Idempotency / Effectively-Once(R37 P2-5)

本文档说明 TG文件解码器 投递系统如何在外部副作用(Telegram `copy_message`)
不可回滚的前提下,达到 **effectively-once** 语义,以及重复通知处理流程。

---

## 1. 问题背景

Telegram Bot API 的 `copy_message` / `copyMessages` 是**外部副作用**:

- 调用成功 → 消息已发送给用户,**无法回滚**
- 网络超时 → 不知道是否真正成功(可能已发,可能未发)
- Bot 重启 / 进程崩溃 → 可能重试已成功的投递

如果重试时再次 `copy_message`,用户会收到**重复通知**,体验恶劣。

理论上严格的 exactly-once 需要两阶段提交 / SAGAS,Telegram API 不支持,
只能做到 **effectively-once**:

> 通过稳定 token + 投递前检查 + 投递后记录,
> 让"重复投递"在事实上不发生(99.99% 概率),失败的 0.01% 通过撤回流程兜底。

---

## 2. delivery_token 设计

### 2.1 token 定义

```
delivery_token = SHA-256(file_code | target_user_id | job_id)
```

- `file_code`: 文件码(业务唯一标识)
- `target_user_id`: 目标用户 Telegram ID
- `job_id`: 派工表 job 主键(CRDB 自增 ID)

输出 64 字符 hex 字符串,作为 `delivery_receipts.delivery_token` 列的值。

### 2.2 为什么选 SHA-256 而非 UUID

| 选项 | 稳定性 | 跨进程一致 | 适用性 |
| ---- | ----- | -------- | ----- |
| UUID v4 | 随机 | ❌(每次不同) | ❌ 重试时 token 变,无法判重 |
| UUID v5(namespace + name) | 稳定 | ✅ | ✅ 但需固定 namespace |
| **SHA-256(file_code + user + job)** | ✅ 稳定 | ✅ | ✅ 推荐 |

SHA-256 让同一三元组永远生成相同 token,即使:
- 进程重启
- 跨机器(分布式部署)
- 时间变化

token 都不变,可作为稳定判重 key。

### 2.3 token 的存储

`delivery_receipts` 表新增 `delivery_token TEXT` 列,并建索引:

```sql
ALTER TABLE delivery_receipts ADD COLUMN delivery_token TEXT;
CREATE INDEX idx_delivery_receipts_token ON delivery_receipts(delivery_token);
```

代码位置: `database/cache_store.py` → CacheStore.__init__()

---

## 3. 投递流程

### 3.1 投递前检查(idempotency check)

```python
from storage.delivery_resolver import (
    compute_delivery_token, is_delivery_already_done,
)

# 投递前
token = compute_delivery_token(file_code, target_user_id, job_id)
if await is_delivery_already_done(store, file_code, target_user_id, job_id):
    logger.info(f"[Dsp] delivery_token={token[:8]}... 已投递过,跳过 job={job_id}")
    return True  # 视为成功(幂等)
```

### 3.2 投递成功后记录

```python
sent_msg_id = await try_deliver(bot, target_user_id, channel, msg_id, ...)

if sent_msg_id:
    # 写入带 delivery_token 的投递回执
    await store.upsert_delivery_receipt(
        job_id, source_msg_id, target_user_id,
        sent_msg_id=sent_msg_id, status="SENT",
        delivery_token=token,  # ← 关键: 写入稳定 token
    )
```

### 3.3 重试时的处理

```python
# retry 时再次进入投递函数
token = compute_delivery_token(...)  # 同样的三元组 → 同样的 token
if await is_delivery_already_done(store, ...):
    # 上次已成功,只是 ack 丢失 → 跳过,不重复发
    return True
# 否则正常投递
```

---

## 4. 重复通知处理流程(撤回)

如果 effectively-once 失效(token 检查未拦截,导致用户收到重复消息),
启动撤回流程:

### 4.1 自动检测重复

```python
# dsp_bot 在 _confirm_delivery_receipt_safe 之前,查询同一 token 的历史记录
existing = await store.get_delivery_receipts_by_job(job_id)
same_token_records = [r for r in existing if r["delivery_token"] == token]
if len(same_token_records) > 1:
    # 同一 token 出现多条 → 重复投递
    logger.error(f"[Dsp] 重复投递告警! token={token}, job={job_id}")
    # 触发告警 + 自动撤回(下条消息发出后立即 delete)
```

### 4.2 自动撤回

```python
# 撤回多余的 sent_msg_id(保留最早一条)
async def revoke_duplicate(bot, chat_id, sent_msg_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=sent_msg_id)
        logger.info(f"[Dsp] 已撤回重复消息 chat={chat_id} msg={sent_msg_id}")
    except Exception as e:
        logger.warning(f"[Dsp] 撤回失败(可能已超 48h): {e}")
```

### 4.3 用户感知补偿

- 撤回成功:用户只看到一条消息(无感知)
- 撤回失败(已超 48h Telegram 限制):**主动发道歉消息**:

```python
await bot.send_message(
    chat_id=target_user_id,
    text="⚠️ 检测到系统重复投递,已记录并排查,给您带来不便敬请谅解。"
)
# 同时触发运维告警
```

### 4.4 运维介入

- Prometheus 告警 `duplicate_delivery_count > 0`
- 日志查 `[Dsp] 重复投递告警` 定位 token
- 检查 db_writer 是否漏写 receipt / receipt 表是否被损坏

---

## 5. 边界场景

### 5.1 token 检查失败(网络异常)

```python
try:
    if await is_delivery_already_done(...):
        return True
except Exception:
    # 检查失败 → 保守策略: 继续投递(宁可重复,不可漏投)
    logger.warning("[delivery] 幂等检查失败,继续投递(可能重复)")
```

**取舍**: 漏投比重复投递更严重(用户拿不到文件 vs 用户收到两条),
所以检查失败时倾向于"投递",再靠重复通知撤回兜底。

### 5.2 媒体组投递

媒体组用 `try_deliver_batch` 一次发多条消息(相册形态):
- token 仍按 `(file_code, target_user_id, job_id)` 计算
- 一次 batch 调用 = 一个 token = 一条 receipt 记录(包含 media_group_id)
- batch 部分成功 → 整 batch 重试,token 仍相同,但已成功的部分会跳过

### 5.3 投递确认延迟(SENT → CONFIRMED)

- `SENT`: Telegram API 返回 sent_msg_id,但用户尚未"已读"
- `CONFIRMED`: 用户点击 inline button 或读取分页(通过 callback 确认)
- token 检查只看 `status IN ('SENT', 'CONFIRMED')`,两者都视为"已投递"

---

## 6. 验证测试

- `tests/test_r37_batch4_p2.py::TestP25DeliveryToken`
  - `test_compute_delivery_token_is_stable` — 同输入永远输出同 token
  - `test_compute_delivery_token_different_inputs` — 不同输入产生不同 token
  - `test_is_delivery_already_done_true_when_token_exists` — 有记录则返回 True
  - `test_is_delivery_already_done_false_when_no_record` — 无记录返回 False

---

## 7. 引用

- `storage/delivery_resolver.py` — `compute_delivery_token()` + `is_delivery_already_done()`
- `database/cache_store.py` — `delivery_receipts.delivery_token` 列 + `is_delivery_already_done()` 方法
- `bots/dsp_bot.py` — 投递流程调用方

---

## 8. R38 P2-5: Effectively-Once SLO 标注

### 8.1 SLO 定义

投递系统达到 **effectively-once** 语义,SLI/SLO 如下:

| 指标 | SLI 定义 | SLO 目标 | 测量窗口 |
| ---- | ------- | -------- | -------- |
| 重复投递率 | `duplicate_delivery_count / total_delivery_count` | < 0.01% (99.99% 不重复) | 30 天滚动 |
| 漏投率 | `missing_delivery_count / total_delivery_count` | < 0.001% (99.999% 不漏投) | 30 天滚动 |
| 投递延迟 P99 | `delivery_latency_p99` | < 5s | 5 分钟窗口 |
| token 检查成功率 | `token_check_success_count / token_check_total` | > 99.9% | 1 小时窗口 |

### 8.2 Effectively-Once 语义保证

**"Effectively-once"** 而非 "exactly-once" 的原因:

- Telegram Bot API 的 `copy_message` 是外部副作用,不可回滚
- 网络超时/进程崩溃可能导致"已发但未记录 receipt",重试时会重复发送
- 严格 exactly-once 需要两阶段提交(2PC),Telegram API 不支持

**保证机制**(四层防线):

1. **投递前检查** — `is_delivery_already_done(token)` 拦截 99%+ 的重复
2. **投递后记录** — `upsert_delivery_receipt(token)` 持久化已投递状态
3. **重复检测** — 同一 token 出现多条 receipt 时自动告警
4. **自动撤回** — 检测到重复后 `bot.delete_message()` 撤回多余消息(48h 内)

### 8.3 降级场景

| 场景 | 行为 | SLO 影响 |
| ---- | ---- | -------- |
| token 检查网络异常 | 保守投递(宁可重复不漏投) | 重复率可能短暂升高,撤回流程兜底 |
| receipt 写入失败 | 投递已成功但未记录,重试时会重复 | 重复率升高,撤回流程兜底 |
| 撤回失败(超 48h) | 用户收到重复消息 + 道歉消息 | 用户体验降级,触发运维告警 |
| db_writer 崩溃 | Redis Stream PEL 未 ACK,重放后 token 检查拦截 | 无 SLO 影响(effectively-once 生效) |

### 8.4 监控指标(Prometheus)

```prometheus
# 重复投递计数(应接近 0)
duplicate_delivery_count_total

# 投递总量(用于计算重复率)
delivery_total_total

# token 检查失败计数
delivery_token_check_fail_total

# 投递延迟分布(histogram)
delivery_latency_seconds_bucket{le="1"}
delivery_latency_seconds_bucket{le="5"}
delivery_latency_seconds_bucket{le="10"}
```

告警规则:
- `rate(duplicate_delivery_count_total[1h]) > 0` → 立即告警(重复投递发生)
- `rate(delivery_token_check_fail_total[5m]) / rate(delivery_total_total[5m]) > 0.01` → token 检查失败率告警

---

## 9. R39 P1-11: Receipt 失败暂停 Job

### 9.1 问题背景

原实现中,`_upsert_delivery_receipt_safe()` 与 `_confirm_delivery_receipt_safe()`
在写 receipt 失败时仅记录 warning 后继续投递流程。这意味着:

- **权威持久层 receipt 写失败** → 内存 `_sent_msg_tracker` 仍标记成功
- 进程崩溃后重启 → 内存丢失,但 Telegram 副作用已发出
- 重试时由于无 receipt 记录 → `is_delivery_already_done()` 返回 False
- → **重复 `copy_message` 调用**,用户收到重复消息

虽然 §4 撤回流程可兜底,但这违反"权威 receipt 写失败时不应继续 Telegram 副作用"原则,
使 effectively-once 的第二层防线(投递后记录)失效。

### 9.2 改进方案

R39 P1-11 调整 receipt 写入失败的语义为 **fail-closed**:

```python
async def _upsert_delivery_receipt_safe(...) -> bool:
    """R39 P1-11: 失败返回 False,调用方应暂停 job。"""
    if store is None:
        return False
    try:
        await store.upsert_delivery_receipt(...)
        return True
    except Exception as e:
        logger.error(f"[Dsp] R39 P1-11: upsert_delivery_receipt 失败(应暂停 job) ...")
        return False  # ← 关键: 不再"忽略后继续"


async def _confirm_delivery_receipt_safe(...) -> bool:
    """R39 P1-11: 失败返回 False,调用方应暂停 job。"""
    if store is None:
        return False
    try:
        await store.confirm_delivery_receipt(...)
        return True
    except Exception as e:
        logger.error(f"[Dsp] R39 P1-11: confirm_delivery_receipt 失败(应暂停 job) ...")
        return False


async def _pause_job_for_receipt_failure(store, job_id: int) -> None:
    """R39 P1-11: receipt 写失败时暂停该 job(标记 receipt_pending)。"""
    if store is None:
        return
    try:
        await store.update_local_job_status(job_id, "receipt_pending")
        logger.warning(f"[Dsp] R39 P1-11: job={job_id} 已暂停(receipt_pending),等待 receipt 恢复后重新入队")
    except Exception as e:
        logger.error(f"[Dsp] R39 P1-11: 暂停 job={job_id} 失败(可能重复投递): {e}")
```

### 9.3 调用点行为变化

| 调用点 | 原行为 | R39 P1-11 新行为 |
| ------ | ------ | ---------------- |
| 投递前写 PENDING receipt | 失败 → warning + 继续 | 失败 → `_pause_job_for_receipt_failure()` + return False |
| 投递成功后写 CONFIRMED receipt | 失败 → warning + 继续 | 失败 → `_pause_job_for_receipt_failure()` + 不再更新内存 tracker |

### 9.4 job 状态机扩展

新增 `receipt_pending` 状态:

```
pending → in_progress → (receipt 写失败) → receipt_pending
                                              ↓
                                  (运维修复 receipt 后重新入队)
                                              ↓
                                          pending
```

- Dsp 主循环跳过 `receipt_pending` 状态的 job
- 等待运维介入修复 receipt 表(例如修复 SQLite/CRDB 写入)
- 修复完成后将状态改回 `pending`,job 重新入队
- 重试时 `is_delivery_already_done()` 会检查 receipt 是否已存在

### 9.5 SLO 影响

| 场景 | R39 P1-11 后行为 | SLO 影响 |
| ---- | ---------------- | -------- |
| receipt 写入失败(SQLite 故障) | job 暂停,不发出 Telegram 副作用 | 漏投率短暂升高,但无重复投递 |
| receipt 写入失败后进程崩溃 | 重启后 job 仍为 `receipt_pending`,不会重复发 | 重复率保持 0 |
| 运维修复后重新入队 | 重试时 token 检查拦截已投递的 job | 无影响 |

**取舍**: 漏投率(可重试恢复)优于重复投递(无法回滚的 Telegram 副作用),
符合 effectively-once 的"宁可漏投不可重复"原则(见 §5.1)。

### 9.6 监控指标

```prometheus
# receipt 写入失败导致 job 暂停的计数
receipt_pause_job_total

# 当前处于 receipt_pending 状态的 job 数量
receipt_pending_jobs
```

告警规则:
- `rate(receipt_pause_job_total[1h]) > 0` → 立即告警(receipt 写入异常)
- `receipt_pending_jobs > 10` → 容量告警(积压过多需运维介入)

### 9.7 引用

- `bots/dsp_bot.py` — `_upsert_delivery_receipt_safe()` / `_confirm_delivery_receipt_safe()` / `_pause_job_for_receipt_failure()`
- `database/cache_store.py` — `update_local_job_status()` 方法
