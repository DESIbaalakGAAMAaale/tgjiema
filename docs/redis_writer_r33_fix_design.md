# Redis Writer R33 审查修复设计文档

## 1. 背景与问题

R33 审查发现 P0 数据丢失窗口：当前使用 Redis List BRPOP/LPOP 模式，消息在弹出后、SQLite 提交前
如果进程崩溃（SIGKILL/OOM/掉电），消息永久丢失，死信逻辑无法执行。

## 2. 修复方案：Redis Streams Consumer Group

### 2.1 核心变更

| 原方案 (List) | 新方案 (Streams) |
|---|---|
| LPUSH 写入 | XADD 写入 Stream |
| BRPOP/LPOP 弹出（立即删除） | XREADGROUP 读取（不删除，进入 pending） |
| 无 ACK 机制 | XACK 确认（SQLite 提交后才 ACK） |
| 崩溃丢消息 | 崩溃后 XAUTOCLAIM 回收 pending 消息 |
| 无幂等键 | 每条消息带 message_id (UUID) |
| 无去重 | SQLite writer_inbox 表实现幂等 |

### 2.2 可靠消费流程

```
Producer (bot进程)          Redis Stream           Consumer (db_writer)
    │                            │                        │
    │── XADD (带message_id) ──→ │                        │
    │                            │                        │
    │                            │ ←── XREADGROUP ──────│ (消息进入pending,不删除)
    │                            │                       │
    │                            │              ┌────────┤
    │                            │              │ 检查inbox│
    │                            │              │ 已存在? │
    │                            │              └───┬────┘
    │                            │              是 ←─┴─→ 否
    │                            │              ↓        ↓
    │                            │           XACK跳过  执行SQLite写
    │                            │                      ↓
    │                            │              写入inbox(message_id)
    │                            │                      ↓
    │                            │           DEL读缓存 + XACK
    │                            │                      │
    │                            │ ←── XACK ────────────│
    │                            │ (消息从pending删除)    │
```

### 2.3 崩溃恢复

进程崩溃后重启：
1. `XAUTOCLAIM` 回收 pending >30s 的消息
2. 对每条回收消息检查 `writer_inbox`
3. 已处理：XACK 跳过
4. 未处理：重新执行 SQLite 写 + 写 inbox + XACK

### 2.4 非幂等操作处理

`increment_user_quota_used`、`refund_quota` 等非幂等操作从 Redis Queue 移至 `_DIRECT_WRITE_METHODS`，
直写 SQLite，避免重放导致二次扣减/退款。

## 3. 死信队列闭环 (P1-1)

### 3.1 死信消息结构

```json
{
  "original": {...},
  "reason": "error message",
  "message_id": "uuid",
  "attempts": 1,
  "max_attempts": 3,
  "failed_at": 1234567890,
  "next_retry_at": 1234567950
}
```

### 3.2 重试策略

- `attempts < max_attempts`: 延迟重试（XADD 回主队列，延迟 60s）
- `attempts >= max_attempts`: 永久死信，需人工排查
- TypeError: 直接永久死信（签名错误，重试无意义）

## 4. Redis 持久化 (P1-3)

部署脚本配置：
- `appendonly yes` (AOF 开启)
- `appendfsync everysec` (每秒 fsync，平衡性能与持久性)
- `noeviction yes` (内存满时拒绝写入，不驱逐)

## 5. 服务级 Secrets 隔离 (P1-5)

按服务拆分 EnvironmentFile：
- `db_writer` 只需: REDIS_URL, WRITER_*, DB_PATH
- `up_bot` 只需: UPLOAD_BOT_TOKEN, DECODER_BOT_USERNAME, ...
- 其他服务同理

## 6. 新增配置项

```python
WRITER_STREAM_KEY: str = "tgjiema:writer:stream"      # Redis Stream key
WRITER_CONSUMER_GROUP: str = "tgjiema-writer-group"    # Consumer Group 名
WRITER_CONSUMER_NAME: str = "db_writer"                 # Consumer 名
WRITER_RECLAIM_IDLE_MS: int = 30000                     # pending 回收阈值(ms)
WRITER_DEAD_MAX_ATTEMPTS: int = 3                       # 死信最大重试次数
WRITER_DEAD_RETRY_DELAY: int = 60                       # 死信重试延迟(秒)
WRITER_DEAD_STREAM_KEY: str = "tgjiema:writer:dead"     # 死信 Stream key
```

## 7. 新增 SQLite 表

```sql
CREATE TABLE IF NOT EXISTS writer_inbox (
    message_id   TEXT PRIMARY KEY,
    method_name  TEXT NOT NULL,
    stream_id    TEXT,
    created_at   REAL NOT NULL,
    processed_at REAL NOT NULL
);
```

## 8. 文件变更清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| database/redis_queue.py | 重写 | List→Streams, 新增ack/reclaim/ensure_group |
| database/db_writer.py | 重写 | XREADGROUP消费, inbox幂等, XACK确认 |
| database/write_router.py | 修改 | 生成message_id |
| database/cache_store.py | 修改 | 新增writer_inbox DDL+方法, 非幂等操作移直写 |
| config/settings.py | 修改 | 新增Stream/Consumer配置 |
| bots/mon_bot.py | 修改 | 新增DLQ/pending/age监控 |
| deploy_vps_per_bot.sh | 修改 | Redis AOF配置, 服务级env文件 |
| .env.example | 修改 | 更新文档 |
| tests/ | 新增 | 真实集成测试+故障注入测试 |
