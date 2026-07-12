# R39 P1-9: CRDB RU 指标采集闭环

## 背景

R39 终审发现: `prometheus_exporter` 读取 `kv_store.crdb_ru_daily` 暴露 `crdb_ru_daily` 指标,
但**无人写入该值**,导致指标长期显示 0。Prometheus 告警规则
`crdb_ru_daily > 100000000` 永远不会触发,无法发现 RU 异常。

本文档定义 RU 采集闭环:

```
CRDB Cloud Metrics API
    ↓ (crdb_ru_collector, 每小时拉取)
kv_store.crdb_ru_daily (SQLite 本地存储,零 RU)
    ↓ (prometheus_exporter, 每次 scrape 读取)
Prometheus crdb_ru_daily 指标
    ↓ (告警规则)
运维告警 (单日 >100M RU)
```

---

## 1. 数据流

| 阶段 | 组件 | 频率 | RU 消耗 |
| ---- | ---- | ---- | ------- |
| 采集 | `services/crdb_ru_collector.py` | 1 小时 | 0(走 CRDB Cloud API,不计费) |
| 存储 | `kv_store.crdb_ru_daily` (SQLite) | 每次采集 | 0(本地 SQLite) |
| 暴露 | `services/prometheus_exporter.py` | 每次 scrape (15s) | 0(读 SQLite) |
| 告警 | Prometheus alert rule | 每 5m eval | 0(读 Prometheus) |

**关键**: 采集 → 存储 → 暴露全程**不消耗 CRDB RU**,因为:
- CRDB Cloud Metrics API 是云平台 API,不走 SQL(不计 RU)
- kv_store 是 SQLite 本地表(不走 CRDB)

---

## 2. 占位实现说明

R39 P1-9 提供 `services/crdb_ru_collector.py` **占位骨架**:
- 模块结构、配置加载、采集循环、信号处理、kv_store 写入 API 已完整实现
- `fetch_ru_from_crdb_cloud()` 函数体为占位(返回 None),需运维按 §3 实现

未配置 `CRDB_CLOUD_API_KEY` / `CRDB_CLOUD_CLUSTER_ID` 时:
- collector 进入"占位模式"
- 不更新 `kv_store.crdb_ru_daily`(保持原值或初始 0)
- 日志输出 `[CRDB-RU] R39 P1-9: 未配置 ... 跳过采集`

---

## 3. 真正实现指南(运维侧)

### 3.1 获取 CRDB Cloud API Key

1. 访问 CockroachDB Cloud Console → Settings → API Keys
2. 创建 Service Account API Key (建议 Read-only)
3. 复制 Key(仅显示一次)

### 3.2 获取 Cluster ID

```bash
# 方式 1:从 Cloud Console URL 获取
# https://cockroachlabs.cloud/cluster/<CLUSTER_ID>/overview

# 方式 2:通过 API 列出 cluster
curl -H "Authorization: Bearer $CRDB_CLOUD_API_KEY" \
  https://cockroachlabs.cloud/api/v1/clusters
```

### 3.3 实现 `fetch_ru_from_crdb_cloud()`

替换 `services/crdb_ru_collector.py` 中的占位代码:

```python
async def fetch_ru_from_crdb_cloud() -> float | None:
    """从 CRDB Cloud Metrics API 拉取过去 24h RU 消耗。"""
    if not CRDB_CLOUD_API_KEY or not CRDB_CLOUD_CLUSTER_ID:
        logger.warning("未配置 CRDB_CLOUD_API_KEY / CRDB_CLOUD_CLUSTER_ID")
        return None

    import httpx
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)

    url = (
        f"{CRDB_CLOUD_API_BASE}/clusters/{CRDB_CLOUD_CLUSTER_ID}"
        f"/metrics/summary"
    )
    params = {
        "start_millis": int(start.timestamp() * 1000),
        "end_millis": int(now.timestamp() * 1000),
        "metric_source": "CLOUD",
        "metric_name": "request_units",
    }
    headers = {
        "Authorization": f"Bearer {CRDB_CLOUD_API_KEY}",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # 响应结构以 CRDB Cloud API 官方文档为准:
            # data["metrics"]["request_units"]["value"]["sum"]
            ru_sum = float(
                data.get("metrics", {})
                .get("request_units", {})
                .get("value", {})
                .get("sum", 0)
            )
            logger.info(f"[CRDB-RU] 过去 24h RU 消耗: {ru_sum:.0f}")
            return ru_sum
    except Exception as e:
        logger.error(f"[CRDB-RU] CRDB Cloud API 调用失败: {e}")
        return None
```

> **注意**: 上述代码为参考实现,实际 API 端点与响应结构以 CockroachDB Cloud
> 官方文档为准:
> https://www.cockroachlabs.com/docs/cockroachcloud/metrics-summary.html

### 3.4 环境变量配置

写入 `/etc/tgjiema/.env.shared` (或独立 `.env.secrets.crdb_ru_collector`):

```env
CRDB_CLOUD_API_KEY=<your-api-key>
CRDB_CLOUD_CLUSTER_ID=<your-cluster-id>
# 可选: 调整采集间隔(默认 3600s = 1 小时)
CRDB_RU_COLLECT_INTERVAL_SECONDS=3600
```

> **安全**: API Key 属于 secrets,必须存入 `.env.secrets.crdb_ru_collector`
> (不被 git 跟踪),不要写入 `.env.shared`。

---

## 4. 部署

### 4.1 独立 systemd unit

`/etc/systemd/system/tgjiema-crdb-ru-collector.service`:

```ini
[Unit]
Description=TGJiema CRDB RU Collector
After=network.target

[Service]
Type=simple
User=tgjiema
WorkingDirectory=/opt/tgjiema
ExecStart=/usr/bin/python3 -m services.crdb_ru_collector
EnvironmentFile=/etc/tgjiema/.env.shared
EnvironmentFile=/etc/tgjiema/.env.secrets.crdb_ru_collector
Restart=on-failure
RestartSec=30
TimeoutStopSec=40
KillSignal=SIGTERM
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tgjiema-crdb-ru-collector
sudo systemctl status tgjiema-crdb-ru-collector
sudo journalctl -u tgjiema-crdb-ru-collector -f
```

### 4.2 Docker Compose

在 `docker-compose.yml` 添加:

```yaml
  crdb_ru_collector:
    build: .
    command: python -m services.crdb_ru_collector
    env_file:
      - .env.shared
      - .env.secrets.crdb_ru_collector
    restart: unless-stopped
```

### 4.3 services.yaml 注册

在 `config/services.yaml` 的 services 列表添加:

```yaml
- name: crdb_ru_collector
  is_oneshot: false
  role: metrics
  description: "CRDB RU 指标采集器(每小时从 CRDB Cloud API 拉取)"
```

---

## 5. Prometheus 告警规则

`config/prometheus-rules.yml` (已有,见 `docs/observability.md`):

```yaml
- alert: crdb_ru_high
  expr: crdb_ru_daily > 100000000   # 单日 >100M RU
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "CRDB 单日 RU 消耗 {{ $value }}"
    description: "过去 24h RU 消耗超过 100M 阈值,请检查是否有异常查询"
```

R39 P1-9 补充告警:

```yaml
- alert: crdb_ru_collector_stale
  expr: time() - prometheus_exporter_data_age_seconds > 7200
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "CRDB RU 采集器 stale"
    description: "kv_store.crdb_ru_daily 超过 2 小时未更新,可能 collector 未运行"
```

---

## 6. 验证

```bash
# 1. 检查 collector 是否运行
sudo systemctl status tgjiema-crdb-ru-collector

# 2. 查看采集日志
sudo journalctl -u tgjiema-crdb-ru-collector -n 50

# 3. 检查 kv_store 是否有值
sqlite3 /opt/tgjiema/cache_store.db \
  "SELECT * FROM kv_store WHERE key='crdb_ru_daily';"

# 4. 通过 Prometheus exporter 验证
curl http://localhost:9100/metrics | grep crdb_ru_daily
# 应输出: crdb_ru_daily 1234567

# 5. 通过 Prometheus 查询
# up{job="prometheus_exporter"} == 1
# crdb_ru_daily > 0
```

---

## 7. 相关文件

- `services/crdb_ru_collector.py` — RU 采集器(占位实现)
- `services/prometheus_exporter.py` — 暴露 `crdb_ru_daily` 指标(读取 kv_store)
- `database/cache_store.py` — `set_kv()` / `get_kv()` API
- `docs/observability.md` — 监控指标总览
- `config/grafana-dashboard.json` — Grafana 仪表盘(crdb_ru panel)
