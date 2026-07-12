# 可观测性 / 告警规则(R37 P2-7)

本文档说明 TG文件解码器 项目的 Prometheus exporter、Grafana 仪表盘和告警规则。

**核心原则**: 运行时监控必须是真实代码 / 配置,而不是纸面文档。

---

## 1. 架构总览

```
   ┌──────────────────────┐    scrape     ┌───────────────┐
   │ services/prometheus_ │ ◄──────────  │  Prometheus    │
   │ exporter.py          │   /metrics    │  server        │
   │ (HTTP :9100)         │              └───────┬───────┘
   └──────────┬───────────┘                      │
              │ read                              │ query
              ▼                                   ▼
   ┌──────────────────────┐              ┌───────────────┐
   │ SQLite cache_store   │              │  Grafana      │
   │  relay_pool          │              │  dashboard    │
   └──────────────────────┘              └───────────────┘
```

### 1.1 组件清单

| 组件 | 文件 | 部署 |
| ---- | --- | --- |
| Prometheus exporter | `services/prometheus_exporter.py` | 独立进程,systemd unit `tgjiema-prometheus` |
| Grafana 仪表盘 | `config/grafana-dashboard.json` | 导入 Grafana |
| 告警规则 | 本文档 + Alertmanager config | 运维侧部署 |
| 备份脚本 | (运维) | crontab 或 systemd timer |

### 1.2 启动 exporter

```bash
# 直接启动
python -m services.prometheus_exporter

# 或通过 systemd
systemctl start tgjiema-prometheus

# Docker(可选,作为单独容器)
docker run -d --name tgjiema-prometheus \
  -p 9100:9100 \
  -v /opt/tgjiema/data:/app/data:ro \
  ghcr.io/<org>/tgjiema:latest \
  python -m services.prometheus_exporter
```

环境变量:

| 变量 | 默认 | 含义 |
| ---- | --- | --- |
| `PROMETHEUS_EXPORTER_HOST` | `0.0.0.0` | 监听地址 |
| `PROMETHEUS_EXPORTER_PORT` | `9100` | 监听端口 |
| `CACHE_STORE_DB` | `<data>/cache_store.db` | cache_store DB 路径 |
| `RELAY_DB_PATH` | `<data>/relay_pool.db` | relay DB 路径 |
| `RELAY_SPOOL_DIR` | `<data>/relay_spool_files` | spool 临时文件目录 |
| `RELAY_SPOOL_MAX_BYTES` | `5368709120`(5GB) | spool 配额上限 |

---

## 2. 暴露指标清单

### 2.1 核心业务指标

| 指标 | 类型 | 含义 | 数据来源 |
| ---- | ---- | ---- | ------- |
| `crdb_ru_daily` | gauge | CockroachDB 当日 RU 消耗 | `kv_store.crdb_ru_daily` |
| `redis_pel_depth` | gauge | Redis Stream pending entries 长度 | `kv_store.redis_pel_depth` |
| `dlq_depth` | gauge | 死信队列深度 | `kv_store.dlq_depth` |
| `dirty_outbox_rows` | gauge | upload_outbox 未完成行数 | `SELECT COUNT(*) FROM upload_outbox WHERE status NOT IN ('DONE','FAILED')` |
| `backup_age_seconds` | gauge | 最近一次成功备份距今秒数 | `kv_store.last_backup_at`(无备份时为 -1) |

### 2.2 Relay spool 指标

| 指标 | 类型 | 含义 |
| ---- | ---- | ---- |
| `relay_spool_disk_usage_bytes` | gauge | spool 目录当前字节数 |
| `relay_spool_disk_max_bytes` | gauge | spool 配额上限 |
| `relay_spool_usage_ratio` | gauge | 使用率(0.0-1.0+) |
| `relay_spool_high_water` | gauge(0/1) | 是否达高水位(80%) |
| `relay_spool_status_count{status=...}` | gauge | 各状态 spool 数量 |

### 2.3 示例输出

```
# HELP crdb_ru_daily CockroachDB daily Request Units consumed
# TYPE crdb_ru_daily gauge
crdb_ru_daily 1234567

# HELP redis_pel_depth Redis Stream pending entries length
# TYPE redis_pel_depth gauge
redis_pel_depth 0

# HELP dlq_depth Dead letter queue depth
# TYPE dlq_depth gauge
dlq_depth 0

# HELP dirty_outbox_rows Unsynced upload_outbox rows
# TYPE dirty_outbox_rows gauge
dirty_outbox_rows 3

# HELP backup_age_seconds Seconds since last successful backup (-1 if never)
# TYPE backup_age_seconds gauge
backup_age_seconds 3600

# HELP relay_spool_status_count Relay spool count by status
# TYPE relay_spool_status_count gauge
relay_spool_status_count{status="RECEIVED"} 2
relay_spool_status_count{status="INDEXED"} 0
relay_spool_status_count{status="ACKED"} 124
```

---

## 3. Prometheus 抓取配置

`/etc/prometheus/prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'tgjiema'
    scrape_interval: 15s
    scrape_timeout: 10s
    metrics_path: /metrics
    static_configs:
      - targets:
          - 'tgjiema-vps:9100'   # 生产 VPS IP / 域名
        labels:
          service: 'tgjiema'
          env: 'prod'
```

---

## 4. Grafana 仪表盘

### 4.1 导入仪表盘

```bash
# 方式一: Grafana UI
1. Grafana → Dashboards → Import
2. Upload `config/grafana-dashboard.json`
3. 选择 Prometheus 数据源
4. Save

# 方式二: API
curl -X POST -H "Content-Type: application/json" \
  -u admin:<password> \
  http://grafana:3000/api/dashboards/db \
  -d @config/grafana-dashboard.json
```

### 4.2 仪表盘面板

- **CRDB RU 当日消耗** — stat gauge,阈值 50M/100M
- **Redis Stream PEL 深度** — stat gauge,阈值 100/1000
- **死信队列深度** — stat,>0 红色
- **未完成 outbox 行数** — stat,阈值 50/200
- **备份距今秒数** — stat,阈值 86400/172800
- **Relay spool 使用率** — stat,阈值 60%/85%
- **Relay spool 状态分布** — timeseries,按 status 分组

刷新间隔: 30s,默认时间窗口: 最近 6 小时。

---

## 5. 告警规则

### 5.1 Alertmanager rules.yaml

```yaml
groups:
  - name: tgjiema
    interval: 30s
    rules:

      # ── CRDB RU 异常 ──
      - alert: crdb_ru_high
        expr: crdb_ru_daily > 100000000   # 单日 >100M RU
        for: 5m
        labels:
          severity: warning
          service: tgjiema
        annotations:
          summary: "CRDB 当日 RU 消耗过高 ({{ $value }})"
          description: "已超过 100M RU/天阈值,可能有大查询或循环写入"

      # ── Redis Stream 堆积 ──
      - alert: redis_pel_depth_high
        expr: redis_pel_depth > 1000
        for: 5m
        labels:
          severity: warning
          service: tgjiema
        annotations:
          summary: "Redis Stream PEL 深度过高 ({{ $value }})"
          description: "db_writer 跟不上,检查消费者组状态"

      # ── 死信队列非空 ──
      - alert: dlq_not_empty
        expr: dlq_depth > 0
        for: 1m
        labels:
          severity: critical
          service: tgjiema
        annotations:
          summary: "死信队列非空 (depth={{ $value }})"
          description: "需人工介入处理失败消息"

      # ── Outbox 堆积 ──
      - alert: outbox_backlog
        expr: dirty_outbox_rows > 200
        for: 5m
        labels:
          severity: warning
          service: tgjiema
        annotations:
          summary: "upload_outbox 积压 ({{ $value }} 行未完成)"
          description: "db_writer 卡住或下游服务不可达"

      # ── 备份过期 ──
      - alert: backup_stale
        expr: backup_age_seconds > 86400   # 24h 未备份
        for: 5m
        labels:
          severity: critical
          service: tgjiema
        annotations:
          summary: "数据库备份过期 ({{ $value | humanizeDuration }})"
          description: "检查 db_backup 服务状态,可能 R2 不可达或 KEK 损坏"

      - alert: backup_never
        expr: backup_age_seconds < 0
        for: 5m
        labels:
          severity: critical
          service: tgjiema
        annotations:
          summary: "从未成功备份"
          description: "首次部署后立即触发,必须人工初始化一次备份"

      # ── Relay spool 高水位 ──
      - alert: relay_spool_high_water
        expr: relay_spool_high_water == 1
        for: 5m
        labels:
          severity: warning
          service: tgjiema
        annotations:
          summary: "Relay spool 磁盘已达高水位"
          description: "拒绝新 spool,触发清理 + 扩容"

      # ── Relay spool 大量 RECEIVED ──
      - alert: relay_spool_backlog
        expr: relay_spool_status_count{status="RECEIVED"} > 50
        for: 5m
        labels:
          severity: warning
          service: tgjiema
        annotations:
          summary: "Relay spool RECEIVED 任务积压"
          description: "中继账号可能被 ban,或 Up Bot 处理慢"

      # ── Exporter 自身存活 ──
      - alert: prometheus_exporter_down
        expr: up{job="tgjiema"} == 0
        for: 2m
        labels:
          severity: critical
          service: tgjiema
        annotations:
          summary: "Prometheus exporter 不可达"
          description: "9100 端口无响应,检查 tgjiema-prometheus 服务"
```

### 5.2 告警接收器

Alertmanager `alertmanager.yml`:

```yaml
route:
  receiver: 'tgjiema-team'
  group_by: ['service', 'alertname']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: 'tgjiema-team'
    webhook_configs:
      - url: 'https://hooks.slack.com/services/...'
        send_resolved: true
    # 可选: 同时发送邮件
    email_configs:
      - to: 'ops@tgjiema.com'
        from: 'alertmanager@tgjiema.com'
        smarthost: 'smtp.tgjiema.com:587'
        auth_username: 'alertmanager@tgjiema.com'
        auth_password: '<SECRET>'
```

---

## 6. 告警演练

新部署或重大版本上线前,执行告警演练:

### 6.1 触发死信队列告警

```bash
# 1. 注入一条死信(向 redis dead stream 写一条假消息)
redis-cli -a <writer_pwd> --user tgjiema_writer \
  XADD tgjiema:writer:dead '*' test 'fault-injection'

# 2. 等 1 分钟,Prometheus scrape 后
# 3. 验证 dlq_not_empty 告警触发
curl -s http://alertmanager:9093/api/v2/alerts | jq '.[] | select(.labels.alertname=="dlq_not_empty")'

# 4. 清理: XDEL 该消息
redis-cli -a <writer_pwd> --user tgjiema_writer \
  XDEL tgjiema:writer:dead <msg_id>
```

### 6.2 触发 relay spool 高水位

```bash
# 1. 临时调小 RELAY_SPOOL_MAX_BYTES
echo 'RELAY_SPOOL_MAX_BYTES=1024' >> /opt/tgjiema/.env.shared
systemctl restart tgjiema-prometheus

# 2. 等 30s,验证告警
curl -s http://localhost:9100/metrics | grep relay_spool_high_water
# 应输出: relay_spool_high_water 1

# 3. 恢复
sed -i '/^RELAY_SPOOL_MAX_BYTES=1024$/d' /opt/tgjiema/.env.shared
systemctl restart tgjiema-prometheus
```

### 6.3 触发备份过期告警

```bash
# 1. 临时把 last_backup_at 改为很久以前
sqlite3 /opt/tgjiema/data/cache_store.db \
  "UPDATE kv_store SET value='0' WHERE key='last_backup_at'"

# 2. 等 scrape 后验证告警 backup_age_seconds < 0
curl -s http://localhost:9100/metrics | grep backup_age_seconds

# 3. 触发一次真实备份恢复
systemctl restart tgjiema-db_backup
```

---

## 7. 运维清单

部署后必检:

- [ ] `curl http://localhost:9100/metrics` 返回 200 + 文本
- [ ] `curl http://localhost:9100/health` 返回 OK
- [ ] Prometheus targets 页面显示 tgjiema job up=1
- [ ] Grafana 仪表盘所有面板有数据(无 "No data")
- [ ] 至少执行一次告警演练(§6)

---

## 8. 引用

- `services/prometheus_exporter.py` — exporter 实现
- `config/grafana-dashboard.json` — 仪表盘配置
- `docs/relay-spool-management.md` — relay spool 监控详解
- `docs/redis-security.md` — Redis 监控(redis_pel_depth)
