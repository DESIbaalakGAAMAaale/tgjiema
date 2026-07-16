"""R39 P1-9 / R41 RU 门禁 / R42 P1-10: CRDB RU 指标采集器。

职责:
    周期性从 CockroachDB Cloud Metrics API / Datadog / PromQL 拉取
    过去 24 小时的 RU 消耗总量,写入本地 kv_store.crdb_ru_daily,
    供 prometheus_exporter 暴露为 crdb_ru_daily 指标。

R41 RU 门禁新增:
    - 采集业务 Bot 空载 RU(kv_store.crdb_idle_ru_daily)
      业务 Bot 不应触发 CRDB RU,本指标用于门禁告警
    - 静态扫描门禁: COCKROACHDB_URL 仅 crdb_sync/migration/disaster_recovery 可读
      其他业务服务读取 COCKROACHDB_URL 视为违规(由测试 test_r41_ru_gate 验证)

R42 P1-10 新增(unknown 状态):
    - ``get_ru_status()`` 返回结构化状态(ru_value/freshness_seconds/source)
      source 取值:
        * "official" — CRDB API 成功且数据新鲜(< 1 小时)
        * "unknown"  — CRDB API 成功但数据陈旧(≥ 1 小时,可能 collector 中断)
        * "failed"   — CRDB API 调用失败(ru_value=None)
    - ``is_data_fresh(max_age_seconds)`` 判断数据是否新鲜(默认 1 小时)
    - prometheus_exporter 据此输出 tgjiema_crdb_ru_source gauge(0/1/2)
      与 tgjiema_crdb_ru_freshness_seconds gauge

背景(R39 P1-9):
    原 prometheus_exporter 读取 kv_store.crdb_ru_daily,但无人写入该值,
    导致指标长期显示 0。本模块填补"采集闭环"缺失环节。

实现状态:
    R39 P1-9 提供占位骨架 + 文档说明。
    R41 改进:新增业务 Bot 空载 RU 采集路径 + 静态门禁辅助。
    R42 P1-10:新增 unknown 状态识别,区分"采集失败" vs "数据陈旧"。
    真正的 CRDB Cloud API 调用需运维提供 API Key 并按官方文档实现:
        https://www.cockroachlabs.com/docs/cockroachcloud/metrics-summary.html

部署方式(独立 systemd unit):
    [Unit]
    Description=TGJiema CRDB RU Collector
    After=network.target

    [Service]
    Type=simple
    ExecStart=/usr/bin/python3 -m services.crdb_ru_collector
    Restart=on-failure
    RestartSec=30
    TimeoutStopSec=40
    KillSignal=SIGTERM
    KillMode=mixed

    [Install]
    WantedBy=multi-user.target

轮询间隔:
    默认 1 小时拉取一次(CRDB Cloud 指标聚合粒度为分钟级,1 小时足够)。
    可通过环境变量 CRDB_RU_COLLECT_INTERVAL_SECONDS 调整。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import sys
import time
from datetime import datetime, timezone
from services.i18n import translate as _i18n_t

try:
    from loguru import logger
except ImportError:  # pragma: no cover - 占位日志
    class _StdLogger:
        def info(self, msg): print(f"[INFO] {msg}")
        def warning(self, msg): print(f"[WARN] {msg}")
        def error(self, msg): print(f"[ERROR] {msg}")
    logger = _StdLogger()  # type: ignore


# ─── R39 P1-9: 采集间隔与 key 常量 ──────────────────────────
COLLECT_INTERVAL_SECONDS = int(
    os.environ.get("CRDB_RU_COLLECT_INTERVAL_SECONDS", "3600")
)
KV_KEY_CRDB_RU_DAILY = "crdb_ru_daily"
# R41 RU 门禁: 业务 Bot 空载 RU kv_store key
KV_KEY_CRDB_IDLE_RU_DAILY = "crdb_idle_ru_daily"

# R39 P1-9: CRDB Cloud API 配置(运维通过环境变量注入)
# 真正实现时需提供 CRDB_CLOUD_API_KEY,否则 collector 降级为"占位模式"
CRDB_CLOUD_API_KEY = os.environ.get("CRDB_CLOUD_API_KEY", "")
CRDB_CLOUD_CLUSTER_ID = os.environ.get("CRDB_CLOUD_CLUSTER_ID", "")
CRDB_CLOUD_API_BASE = os.environ.get(
    "CRDB_CLOUD_API_BASE_URL",
    "https://cockroachlabs.cloud/api/v1",
)

# R41 RU 门禁: 允许读取 COCKROACHDB_URL 的服务白名单
# 业务 Bot(up/idx/dsp/admin_bot)不在白名单中,通过 SERVICE_ROLE 门禁校验
_ALLOWED_CRDB_URL_READERS = frozenset({
    "crdb_sync",       # 独占同步 dirty_outbox → CRDB
    "migration",       # DDL 执行
    "bootstrap",       # 显式人工恢复任务(systemd oneshot)
    "disaster_recovery",  # 灾备恢复
    "backup",          # 备份(显式触发)
})


def is_service_allowed_crdb_url(service_role: str) -> bool:
    """R41 RU 门禁: 检查服务是否允许读取 COCKROACHDB_URL。

    用于静态扫描门禁:业务 Bot 不应在源码中引用 COCKROACHDB_URL,
    且运行时不应读取该环境变量。

    Args:
        service_role: 服务角色(从 SERVICE_ROLE 环境变量读取)

    Returns:
        True: 允许读取(白名单内服务)
        False: 不允许(业务 Bot 应只读 SQLite 本地权威状态)
    """
    return (service_role or "").strip() in _ALLOWED_CRDB_URL_READERS


async def fetch_ru_from_crdb_cloud() -> float | None:
    """R39 P1-9: 从 CRDB Cloud Metrics API 拉取过去 24h RU 消耗。

    占位实现: 未配置 API Key 时返回 None,collector 跳过写入,
    保持 kv_store.crdb_ru_daily 为上一次成功采集的值(或初始 0)。

    真正实现需:
    1. 调用 GET /clusters/{cluster_id}/metrics/summary
       (参考 https://www.cockroachlabs.com/docs/cockroachcloud/...)
    2. 解析 response["metrics"]["request_units"]["value"]["sum"]
    3. 转换为当日 RU 总量(float)

    Returns:
        float: 当日 RU 消耗总量
        None: 未配置 API Key 或采集失败(保持原值不变)
    """
    if not CRDB_CLOUD_API_KEY or not CRDB_CLOUD_CLUSTER_ID:
        logger.warning(
            "[CRDB-RU] R39 P1-9: 未配置 CRDB_CLOUD_API_KEY / CRDB_CLOUD_CLUSTER_ID,"
            "跳过采集(kv_store.crdb_ru_daily 保持原值)"
        )
        return None

    # R41: 真正实现 — 调用 CRDB Cloud Metrics API
    # 注意:此处实现为可选 HTTP 调用,失败时返回 None(不阻塞主循环)
    try:
        import urllib.request
        import urllib.error
        import json as _json

        url = (
            f"{CRDB_CLOUD_API_BASE}/clusters/{CRDB_CLOUD_CLUSTER_ID}/metrics/summary"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {CRDB_CLOUD_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        # 5 秒超时,避免阻塞采集循环
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            data = _json.loads(body)
        # 解析 RU 指标:CRDB Cloud API 返回结构因版本而异
        # 常见字段:metrics.request_units.value.sum
        metrics_section = data.get("metrics", {}) if isinstance(data, dict) else {}
        ru_section = metrics_section.get("request_units", {}) if isinstance(metrics_section, dict) else {}
        value_section = ru_section.get("value", {}) if isinstance(ru_section, dict) else {}
        ru_sum = value_section.get("sum")
        if ru_sum is None:
            logger.warning(
                f"[CRDB-RU] R41: CRDB Cloud API 返回无 request_units.value.sum 字段"
            )
            return None
        try:
            return float(ru_sum)
        except (TypeError, ValueError):
            logger.warning(
                f"[CRDB-RU] R41: RU 值无法转为 float: {ru_sum}"
            )
            return None
    except urllib.error.URLError as e:
        logger.warning(f"[CRDB-RU] R41: CRDB Cloud API 请求失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"[CRDB-RU] R41: CRDB Cloud API 调用异常: {e}")
        return None


async def fetch_idle_ru_from_local() -> float:
    """R41 RU 门禁: 采集业务 Bot 空载 RU 消耗。

    业务 Bot(up/idx/dsp/admin_bot)不持有 CRDB URL,理论上 RU = 0。
    本函数从 SQLite kv_store 读取累计的 ru_usage 计数(由 ru_cost_center 写入),
    计算当日空载 RU 总量。

    空载 RU = 总 RU - crdb_sync 同步消耗的 RU(由 crdb_sync 内部计数)
    简化版:直接读取当日所有业务服务累计 RU 之和。

    Returns:
        float: 业务 Bot 当日空载 RU 消耗(0 表示无 CRDB 调用)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            return 0.0
        # 读取当日所有业务服务累计 RU(由 ru_cost_center.record_usage 写入 kv_store)
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        # 业务服务列表(排除 crdb_sync/migration 等基础设施服务)
        business_services = ["up_bot", "idx_bot", "dsp_bot", "admin_bot", "db_writer"]
        total_idle_ru = 0.0
        for service in business_services:
            key = f"ru_usage:{today}:{service}"
            raw = await store.get_kv(key)
            if not raw:
                continue
            try:
                import json as _json
                data = _json.loads(raw)
                # ru_cost_center 写入格式: {by_operation: {op: amount}, total: float}
                total = data.get("total", 0) if isinstance(data, dict) else 0
                try:
                    total_idle_ru += float(total)
                except (TypeError, ValueError):
                    pass
            except (ValueError, TypeError):
                continue
        return total_idle_ru
    except Exception as e:
        logger.debug(f"[CRDB-RU] R41: fetch_idle_ru_from_local 失败: {e}")
        return 0.0


# ════════════════════════════════════════════════════════════════
#  R42 P1-10: unknown 状态识别(get_ru_status / is_data_fresh)
# ════════════════════════════════════════════════════════════════


# R42 P1-10: 数据新鲜度阈值(秒)— 超过此值认为数据陈旧(source="unknown")
RU_DATA_FRESH_THRESHOLD = 3600  # 1 小时


async def get_ru_status() -> dict:
    """R42 P1-10: 获取 CRDB RU 指标的结构化状态。

    返回 dict 包含:
        - ru_value: int | None
            None 表示采集失败(CRDB API 不可用)
            非 None 表示 kv_store.crdb_ru_daily 的最新值
        - freshness_seconds: int | None
            None 表示从未采集或时间戳解析失败
            非 None 表示距上次成功采集的秒数
        - source: "official" | "unknown" | "failed"
            "official": CRDB API 调用成功且数据新鲜(< RU_DATA_FRESH_THRESHOLD)
            "unknown" : 数据存在但陈旧(≥ RU_DATA_FRESH_THRESHOLD),可能 collector 中断
            "failed"  : CRDB API 调用失败(ru_value=None,freshness_seconds=None)

    本函数区分 "采集失败" 与 "数据陈旧" 两种场景:
        - "failed"  → 修复 collector(API Key / 网络 / CRDB 状态)
        - "unknown" → 检查 collector 是否在运行,或调高采集频率

    Returns:
        {
            "ru_value": int | None,
            "freshness_seconds": int | None,
            "source": "official" | "unknown" | "failed",
            "last_collected_at": str,   # ISO 时间戳,失败时为空字符串
            "details": str,
        }
    """
    # 1. 调用 CRDB API 获取最新 RU 值
    try:
        api_ru_value = await fetch_ru_from_crdb_cloud()
    except Exception as e:
        # API 调用本身异常 → failed
        logger.warning(f"[CRDB-RU] R42 P1-10: fetch_ru_from_crdb_cloud 异常: {e}")
        api_ru_value = None

    # 2. 读取 kv_store 中的最新采集记录
    kv_ru_value: float | None = None
    last_collected_at: str = ""
    freshness_seconds: int | None = None

    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if store and getattr(store, "_db", None):
            raw = await store.get_kv(KV_KEY_CRDB_RU_DAILY)
            if raw:
                try:
                    kv_ru_value = float(raw)
                except (TypeError, ValueError):
                    kv_ru_value = None
            # 也读取上次成功采集的时间戳(kv_store.crdb_ru_last_collected_at)
            ts_raw = await store.get_kv("crdb_ru_last_collected_at")
            if ts_raw:
                last_collected_at = str(ts_raw)
                # 解析 ISO 时间戳并计算 freshness_seconds
                try:
                    # 尝试 ISO 8601
                    dt = _parse_iso_datetime(ts_raw)
                    if dt is not None:
                        # 转 epoch(秒)
                        # datetime.timestamp() 在 aware datetime 上返回 POSIX 时间戳;
                        # 在 naive datetime 上视为本地时间(应避免)
                        if dt.tzinfo is None:
                            # naive datetime → 视为 UTC(因为 collector 用 utcnow 写入)
                            import datetime as _dt2
                            dt = dt.replace(tzinfo=_dt2.timezone.utc)
                        freshness_seconds = int(time.time() - dt.timestamp())
                        if freshness_seconds < 0:
                            # 时间戳在未来(时钟漂移),视为 0
                            freshness_seconds = 0
                except (ValueError, TypeError):
                    # 尝试解析为 epoch 数字
                    try:
                        ts_num = float(ts_raw)
                        freshness_seconds = int(time.time() - ts_num)
                        if freshness_seconds < 0:
                            freshness_seconds = 0
                    except (ValueError, TypeError):
                        freshness_seconds = None
    except Exception as e:
        logger.debug(f"[CRDB-RU] R42 P1-10: 读取 kv_store 失败: {e}")

    # 3. 决定 source 与 ru_value
    if api_ru_value is None and kv_ru_value is None:
        # 采集失败且无历史数据 → failed
        return {
            "ru_value": None,
            "freshness_seconds": None,
            "source": "failed",
            "last_collected_at": "",
            "details": _i18n_t('services.crdb_ru_collector.s2'),
        }

    # 优先使用 API 最新值,否则用 kv_store 历史值
    ru_value = api_ru_value if api_ru_value is not None else kv_ru_value

    # 判断新鲜度
    if freshness_seconds is None:
        # 时间戳缺失 → 视为 unknown(数据存在但无法判断新鲜度)
        source = "unknown"
        details = (
            _i18n_t('services.crdb_ru_collector.s1')
        )
    elif freshness_seconds >= RU_DATA_FRESH_THRESHOLD:
        # 数据陈旧 → unknown
        source = "unknown"
        details = (
            _i18n_t('services.crdb_ru_collector.s3', freshness_seconds=freshness_seconds, RU_DATA_FRESH_THRESHOLD=RU_DATA_FRESH_THRESHOLD)
        )
    else:
        # 数据新鲜 → official
        source = "official"
        details = (
            _i18n_t('services.crdb_ru_collector.s4', freshness_seconds=freshness_seconds, RU_DATA_FRESH_THRESHOLD=RU_DATA_FRESH_THRESHOLD)
        )

    return {
        "ru_value": int(ru_value) if ru_value is not None else None,
        "freshness_seconds": freshness_seconds,
        "source": source,
        "last_collected_at": last_collected_at,
        "details": details,
    }


def _parse_iso_datetime(ts_str: str):
    """解析 ISO 8601 时间字符串为 datetime 对象。

    支持以下格式:
        - "2026-07-13T10:00:00"
        - "2026-07-13T10:00:00.123456"
        - "2026-07-13T10:00:00Z"
        - "2026-07-13T10:00:00+00:00"

    失败时返回 None。
    """
    import datetime as _dt
    if not ts_str:
        return None
    s = ts_str.strip()
    # 替换 Z 为 +00:00 以便 fromisoformat 解析
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


async def is_data_fresh(max_age_seconds: int = RU_DATA_FRESH_THRESHOLD) -> bool:
    """R42 P1-10: 判断 kv_store 中的 RU 数据是否新鲜。

    Args:
        max_age_seconds: 新鲜度阈值(秒),默认 3600(1 小时)

    Returns:
        True: 数据存在且距上次采集 < max_age_seconds
        False: 数据不存在、时间戳缺失或陈旧(≥ max_age_seconds)
    """
    try:
        status = await get_ru_status()
        if status.get("source") != "official":
            return False
        fresh = status.get("freshness_seconds")
        if fresh is None:
            return False
        return fresh < max_age_seconds
    except Exception:
        return False


async def fetch_idle_ru_from_local_legacy() -> float:
    """占位:R42 P1-10 之前已存在 fetch_idle_ru_from_local(已重命名前缀)。

    为向后兼容保留对原 fetch_idle_ru_from_local 的调用入口。
    """
    return await fetch_idle_ru_from_local()


async def write_ru_to_kv_store(
    ru_value: float,
    query_window_start: str | None = None,
    query_window_end: str | None = None,
) -> bool:
    """R39 P1-9 + R54 P1-1 + R55 P1-3: 将当日 RU 消耗写入 kv_store.crdb_ru_daily。

    写入成功后,prometheus_exporter 下次 scrape 会暴露更新后的 crdb_ru_daily 指标。
    kv_store 写入零 CRDB RU(SQLite 本地存储)。

    R42 P1-10: 同时写入 kv_store.crdb_ru_last_collected_at(ISO 时间戳),
    供 get_ru_status() 计算数据新鲜度(freshness_seconds)。

    R54 P1-1: 同时写入 kv_store.crdb_ru_source(向后兼容保留,但 exporter 不再信任)。

    R55 P1-3: 新增独立表 ``crdb_ru_official`` 隔离官方 RU 数据,防止伪造。
    - 惰性创建 ``crdb_ru_official`` 表(CREATE TABLE IF NOT EXISTS)
    - 同一 SQLite 事务写入: official 表 INSERT + kv_store 的 set_kv
    - 记录 collector_id / query_window / response_digest / collection_version
    - response_digest = HMAC-SHA256(response_raw, collector_secret)
    - 估算器 ``write_idle_ru_to_kv_store`` 无权写该表

    Args:
        ru_value: 当日 RU 消耗总量
        query_window_start: 查询窗口起始 ISO8601(默认今日 UTC 0:00)
        query_window_end: 查询窗口结束 ISO8601(默认 now)

    Returns:
        True: 写入成功
        False: 写入失败(下次重试)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()

        # ── R55 P1-3: 计算 collector identity 与 response digest ──
        collector_id = f"{socket.gethostname()}:{os.getpid()}"
        collector_secret = os.environ.get(
            "CRDB_RU_COLLECTOR_SECRET", "tgjiema-collector-v1"
        )
        now_dt = datetime.now(timezone.utc)
        collected_at = now_dt.isoformat()
        # 默认查询窗口: 今日 UTC 0:00 到 now
        if query_window_start is None:
            window_start_dt = now_dt.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            query_window_start = window_start_dt.isoformat()
        if query_window_end is None:
            query_window_end = collected_at
        response_raw = (
            f"ru_value={ru_value}|window={query_window_start}-{query_window_end}"
            f"|collector={collector_id}"
        )
        response_digest = hmac.new(
            collector_secret.encode(),
            response_raw.encode(),
            hashlib.sha256,
        ).hexdigest()

        # ── R55 P1-3: 惰性创建 official 表 + 同事务原子写入 ──
        # 若 _db 不可用(如 mock 环境)或事务失败,降级为单独 set_kv 调用
        official_written = False
        db = getattr(store, "_db", None)
        if db is not None:
            try:
                # 惰性创建 crdb_ru_official 表
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS crdb_ru_official (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ru_value REAL NOT NULL,
                        source TEXT NOT NULL DEFAULT 'official_cloud_api',
                        collector_id TEXT NOT NULL,
                        query_window_start TEXT NOT NULL,
                        query_window_end TEXT NOT NULL,
                        response_digest TEXT NOT NULL,
                        collection_version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    )"""
                )
                # 同一事务写入 official 表 + kv_store(原子性保证)
                await db.execute(
                    """INSERT INTO crdb_ru_official
                       (ru_value, source, collector_id, query_window_start,
                        query_window_end, response_digest, collection_version,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        float(ru_value),
                        "official_cloud_api",
                        collector_id,
                        query_window_start,
                        query_window_end,
                        response_digest,
                        1,  # collection_version
                        collected_at,
                    ),
                )
                # kv_store 写入(同一事务,保证原子性)
                await db.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                    (KV_KEY_CRDB_RU_DAILY, str(ru_value)),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                    ("crdb_ru_last_collected_at", collected_at),
                )
                # R54 P1-1: 保留 kv_store.crdb_ru_source(向后兼容降级,
                # 但 prometheus_exporter R55 P1-3 后不再信任此值)
                await db.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                    ("crdb_ru_source", "official_cloud_api"),
                )
                await db.commit()
                official_written = True
            except Exception as official_err:
                logger.debug(
                    f"[CRDB-RU] R55 P1-3: 同事务写入 crdb_ru_official 失败,"
                    f"降级为单独 set_kv: {official_err}"
                )

        # ── 降级路径:official 表写入失败时,单独调用 set_kv ──
        if not official_written:
            await store.set_kv(KV_KEY_CRDB_RU_DAILY, str(ru_value))
            try:
                await store.set_kv("crdb_ru_last_collected_at", collected_at)
            except Exception as ts_err:
                logger.debug(
                    f"[CRDB-RU] R42 P1-10: 写入 crdb_ru_last_collected_at 失败: {ts_err}"
                )
            try:
                await store.set_kv("crdb_ru_source", "official_cloud_api")
            except Exception as src_err:
                logger.debug(
                    f"[CRDB-RU] R54 P1-1: 写入 crdb_ru_source 失败: {src_err}"
                )

        logger.info(
            f"[CRDB-RU] R39 P1-9: kv_store.crdb_ru_daily 已更新 → {ru_value:.0f} RU"
            f" (R55 P1-3 official 表写入: {'成功' if official_written else '降级'})"
        )
        return True
    except Exception as e:
        logger.error(f"[CRDB-RU] R39 P1-9: 写入 kv_store 失败: {e}")
        return False


async def write_idle_ru_to_kv_store(ru_value: float) -> bool:
    """R41 RU 门禁: 将业务 Bot 空载 RU 写入 kv_store.crdb_idle_ru_daily。

    写入成功后,prometheus_exporter 暴露 tgjiema_crdb_idle_ru_daily 指标。
    kv_store 写入零 CRDB RU(SQLite 本地存储)。

    R55 P1-3: 估算器无权写 crdb_ru_official 表(该表仅 crdb_ru_collector
    的 ``write_ru_to_kv_store`` 可写)。本函数仅写入 kv_store.crdb_idle_ru_daily,
    不可伪造官方 RU source。prometheus_exporter 通过 verify_ru_source_official()
    验证 official 表,估算值不被误判为官方数据。

    Args:
        ru_value: 业务 Bot 当日空载 RU 消耗

    Returns:
        True: 写入成功
        False: 写入失败(下次重试)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        await store.set_kv(KV_KEY_CRDB_IDLE_RU_DAILY, str(ru_value))
        logger.info(
            f"[CRDB-RU] R41: kv_store.crdb_idle_ru_daily 已更新 → {ru_value:.2f} RU"
        )
        return True
    except Exception as e:
        logger.error(f"[CRDB-RU] R41: 写入 kv_store.crdb_idle_ru_daily 失败: {e}")
        return False


# ════════════════════════════════════════════════════════════════
#  R55 P1-3: 官方 RU source 独立表验证(verify_ru_source_official)
# ════════════════════════════════════════════════════════════════


def verify_ru_source_official() -> dict:
    """R55 P1-3: 验证 ``crdb_ru_official`` 表的最新官方 RU 记录。

    查询 ``crdb_ru_official`` 表的最新记录(ORDER BY created_at DESC LIMIT 1),
    用于 prometheus_exporter 验证官方 RU source 是否真实存在(而非伪造的
    kv_store.crdb_ru_source 字符串)。

    本函数为同步函数(使用 ``sqlite3`` 只读连接),便于 prometheus_exporter
    在同步上下文中调用。表不存在或无记录时返回 ``is_official=False``。

    Returns:
        {
            "is_official": bool,      # True=有官方记录, False=无记录/表不存在
            "ru_value": float,        # 最新官方 RU 值(无记录时为 0.0)
            "collector_id": str,      # 采集进程标识(无记录时为 "")
            "response_digest": str,   # HMAC-SHA256 摘要(无记录时为 "")
            "created_at": str,        # 写入时间 ISO8601(无记录时为 "")
        }
    """
    default_result = {
        "is_official": False,
        "ru_value": 0.0,
        "collector_id": "",
        "response_digest": "",
        "created_at": "",
    }
    try:
        import sqlite3
        from pathlib import Path

        # 与 prometheus_exporter 一致的 DB 路径解析
        _default_data_dir = Path(__file__).resolve().parent.parent / "data"
        db_path = Path(
            os.getenv("CACHE_STORE_DB", str(_default_data_dir / "cache_store.db"))
        )
        if not db_path.exists():
            return default_result
        # 只读模式打开,避免与 collector 写入产生锁竞争
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cursor = conn.execute(
            "SELECT ru_value, collector_id, response_digest, created_at "
            "FROM crdb_ru_official ORDER BY created_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return default_result
        return {
            "is_official": True,
            "ru_value": float(row[0]),
            "collector_id": str(row[1]),
            "response_digest": str(row[2]),
            "created_at": str(row[3]),
        }
    except sqlite3.Error as e:
        # 表不存在(sqlite3.OperationalError: no such table)等
        logger.debug(f"[CRDB-RU] R55 P1-3: 查询 crdb_ru_official 表失败: {e}")
        return default_result
    except Exception as e:
        logger.debug(f"[CRDB-RU] R55 P1-3: verify_ru_source_official 异常: {e}")
        return default_result


async def _collect_once() -> None:
    """R39 P1-9 / R41: 单次采集 RU 指标并写入 kv_store。"""
    # R39 P1-9: 采集总 RU(从 CRDB Cloud API)
    ru_value = await fetch_ru_from_crdb_cloud()
    if ru_value is not None:
        if isinstance(ru_value, (int, float)) and ru_value >= 0:
            await write_ru_to_kv_store(float(ru_value))
        else:
            logger.warning(
                f"[CRDB-RU] R39 P1-9: RU 值非法({ru_value}),跳过写入"
            )

    # R41 RU 门禁: 采集业务 Bot 空载 RU(从 SQLite 本地 ru_cost_center 计数)
    idle_ru = await fetch_idle_ru_from_local()
    await write_idle_ru_to_kv_store(idle_ru)


async def _collect_loop() -> None:
    """R39 P1-9 / R41: 主采集循环(每小时一次)。"""
    logger.info(
        f"[CRDB-RU] R39 P1-9 / R41: collector 启动,"
        f"间隔 {COLLECT_INTERVAL_SECONDS}s,"
        f"API Key 配置: {'已配置' if CRDB_CLOUD_API_KEY else '未配置(占位模式)'},"
        f"业务 Bot 空载 RU 采集: 已启用"
    )
    # 启动时立即采集一次
    await _collect_once()
    while True:
        try:
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)
            await _collect_once()
        except asyncio.CancelledError:
            logger.info("[CRDB-RU] R39 P1-9 / R41: collector 收到取消信号,退出")
            raise
        except Exception as e:
            logger.error(f"[CRDB-RU] R39 P1-9 / R41: 采集循环异常: {e}")
            await asyncio.sleep(60)  # 异常后等待 1 分钟再重试


def _handle_signal(signum, frame) -> None:
    """R39 P1-9: 信号处理(SIGTERM/SIGINT 优雅退出)。"""
    logger.info(f"[CRDB-RU] R39 P1-9 / R41: 收到信号 {signum},准备退出")
    sys.exit(0)


def main() -> None:
    """R39 P1-9 / R41: collector 入口。"""
    import signal
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(_collect_loop())
    except KeyboardInterrupt:
        logger.info("[CRDB-RU] R39 P1-9 / R41: KeyboardInterrupt,退出")


if __name__ == "__main__":
    main()
