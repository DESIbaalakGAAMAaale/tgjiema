# Relay Spool 磁盘配额管理(R37 P2-6)

本文档说明 TG文件解码器 中继任务池(relay_spool)的磁盘配额、
高低水位、过期清理与人工修复策略。

**核心原则**: 未最终 INDEXED 的文件**禁止自动删除**,
避免业务数据丢失。

---

## 1. 背景

中继账号(relay account)收到外部 Bot 转发的文件后:

1. 文件下载到本地临时目录(buffered_files),状态 `RECEIVED`
2. 推进为 `BUFFERED`,relay_account 转发到 Up Bot,状态 `FORWARDING`
3. Up Bot 持久化后返回 upload_id,状态 `UP_DURABLE_ACK`
4. Idx Bot 完成索引,状态 `INDEXED`
5. relay_instance 检测到 INDEXED → 删除临时文件,置 `ACKED`(终态)

如果中转临时目录磁盘满:
- 新文件无法落盘 → 中继任务全失败
- 已落盘文件未被清理 → 磁盘膨胀

R37 P2-6 引入**高低水位 + 配额上限**机制:
- 80% 高水位:停止接收新 spool,触发告警
- 60% 低水位:清理足够后恢复接收
- 未 INDEXED 的临时文件**永不自动删除**,只标记 FAILED 等运维处理

---

## 2. 配置

环境变量(在 `.env.shared` 或 systemd EnvironmentFile 中配置):

| 变量 | 默认值 | 含义 |
| ---- | ----- | --- |
| `RELAY_SPOOL_MAX_BYTES` | `5368709120`(5GB) | spool 目录最大字节数,0=不限制 |
| `RELAY_SPOOL_DIR` | `<data>/relay_spool_files` | 临时文件目录 |

代码常量(`database/relay_db.py`):

```python
RELAY_SPOOL_HIGH_WATER_MARK = 0.80  # 80%,触达后拒绝新 spool
RELAY_SPOOL_LOW_WATER_MARK = 0.60  # 60%,回到此值以下恢复接收
```

---

## 3. 高水位处理流程

### 3.1 触达高水位(≥80%)

`RelayDB.should_accept_new_spool()` 返回 `False`,
`relay_instance.create_relay_spool` 调用前应检查:

```python
from database.relay_db import get_relay_db

db = await get_relay_db()
if not await db.should_accept_new_spool():
    # 拒绝接收,触发告警 + 等待运维介入
    raise RuntimeError("relay_spool disk high water exceeded")
```

relay_instance 在收到新中继请求时:

1. 调用 `should_accept_new_spool()` 检查
2. 若 False:
   - 不下载文件
   - 向用户回复"系统繁忙,请稍后重试"
   - 触发 Prometheus 告警 `relay_spool_high_water=1`
   - 等待 INDEXED 清理循环释放空间

### 3.2 回到低水位(≤60%)

后台清理循环(`relay_instance._spool_recovery_loop`)每 30s:

1. 查询 `status='INDEXED' AND acked_at IS NULL` 的 spool
2. 删除 buffered_files 临时文件
3. 置 `ACKED` 状态
4. 重新检查 `should_accept_new_spool()`,若 True 则恢复接收

清理只删除 INDEXED 状态的文件,**不删 RECEIVED / BUFFERED / FORWARDING 状态**,
即使 TTL 过期:
- TTL 过期只将状态置 `FAILED`,**保留临时文件**
- 让运维或恢复脚本人工介入

---

## 4. 过期清理策略

### 4.1 cleanup_expired_spool(已有,H6 实现)

```python
async def cleanup_expired_spool(self, ttl_seconds: int = 300) -> int:
    """清理 RECEIVED/BUFFERED 状态下 TTL 已过期的任务,置为 FAILED。

    注意: 仅修改 DB 状态,**不删除临时文件**(R37 P2-6 强化)
    """
```

`ttl_expires_at < now - ttl_seconds` 的 spool 置 `FAILED`,
但**临时文件保留**,因为:
- 中继账号可能因网络抖动误判 TTL
- 文件已被 Forward 到 Up Bot 但 ack 丢失
- 删除文件后无法回放恢复

### 4.2 cleanup_indexed_spools_only(新增,R37 P2-6)

```python
async def cleanup_indexed_spools_only(self) -> int:
    """仅清理已 INDEXED 的 spool 临时文件。

    严格策略: 未 INDEXED 的文件禁止自动删除。
    """
```

这是**唯一**自动删除临时文件的入口,只针对 `status='INDEXED'` 的记录。

---

## 5. 人工修复策略

### 5.1 高水位持续告警

监控告警 `relay_spool_usage_ratio > 0.85` 持续 5 分钟:

1. SSH 登录 VPS,检查磁盘:
   ```bash
   du -sh /opt/tgjiema/data/relay_spool_files/
   df -h /opt/tgjiema/data
   ```

2. 查看 spool 状态分布:
   ```bash
   sqlite3 /opt/tgjiema/data/relay_pool.db \
     "SELECT status, COUNT(*) FROM relay_spool GROUP BY status"
   ```

3. 诊断:
   - 大量 `RECEIVED/BUFFERED` → 中继账号被 ban,需更换
   - 大量 `FORWARDED_TO_UP` → Up Bot 处理慢,需扩容
   - 大量 `UP_DURABLE_ACK` → Idx Bot 索引慢,需检查
   - 大量 `INDEXED` 但 acked_at IS NULL → 清理循环未运行

### 5.2 手动清理 INDEXED

```bash
# 触发一次 INDEXED 清理(Python REPL)
cd /opt/tgjiema
venv/bin/python -c "
import asyncio
from database.relay_db import get_relay_db
async def clean():
    db = await get_relay_db()
    n = await db.cleanup_indexed_spools_only()
    print(f'cleaned {n} INDEXED spools')
asyncio.run(clean())
"
```

### 5.3 处理孤儿文件(orphan files)

DB 中无记录但磁盘上存在的临时文件:

```bash
# 列出 spool 目录所有文件
ls -la /opt/tgjiema/data/relay_spool_files/

# 与 DB 中的 buffered_files 比对
sqlite3 /opt/tgjiema/data/relay_pool.db \
  "SELECT buffered_files FROM relay_spool WHERE status != 'ACKED'" > /tmp/registered.txt

# 孤儿文件(>24h)可手动删除
find /opt/tgjiema/data/relay_spool_files/ -type f -mtime +1 -ls
```

### 5.4 紧急扩容

如果持续高水位且无法清理:

1. 临时调高 RELAY_SPOOL_MAX_BYTES(在 .env.shared 中)
2. 重启 relay 相关服务:`systemctl restart tgjiema-idx.target`
3. 长期方案:挂载更大磁盘到 `/opt/tgjiema/data`
4. 联系运维增加 VPS 磁盘

---

## 6. 监控指标

Prometheus exporter(`services/prometheus_exporter.py`)暴露:

| 指标 | 类型 | 含义 |
| ---- | ---- | --- |
| `relay_spool_disk_usage_bytes` | gauge | 当前 spool 目录字节数 |
| `relay_spool_disk_max_bytes` | gauge | 配额上限 |
| `relay_spool_usage_ratio` | gauge | 当前/上限(0.0-1.0+) |
| `relay_spool_high_water` | gauge(0/1) | 是否达高水位 |
| `relay_spool_status_count{status="RECEIVED"}` | gauge | 各状态 spool 数 |

Grafana 仪表盘 → "Relay Spool" panel,告警规则:

```yaml
- alert: relay_spool_high_water
  expr: relay_spool_usage_ratio > 0.85
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Relay spool 磁盘使用率 >85%"
```

详见 `docs/observability.md`。

---

## 7. 验证测试

`tests/test_r37_batch4_p2.py::TestP26RelaySpoolQuota`:

- `test_high_water_mark_constant` — `RELAY_SPOOL_HIGH_WATER_MARK == 0.80`
- `test_low_water_mark_constant` — `RELAY_SPOOL_LOW_WATER_MARK == 0.60`
- `test_should_accept_new_spool_below_high_water` — 低水位时返回 True
- `test_should_accept_new_spool_above_high_water` — 高水位时返回 False(通过环境变量模拟)
- `test_cleanup_indexed_spools_only_keeps_non_indexed` — 未 INDEXED 文件不被删

---

## 8. 引用

- `database/relay_db.py` — `RelayDB.should_accept_new_spool()` + `cleanup_indexed_spools_only()`
- `services/relay_instance.py` — `_spool_recovery_loop()` 后台清理
- `services/prometheus_exporter.py` — 暴露 spool 指标
- `docs/observability.md` — 整体监控体系
