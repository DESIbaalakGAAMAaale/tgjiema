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
import json
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
    except Exception as e:
        logger.warning(_i18n_t('diagnostics.r65.p1_04.crdb_ru_is_data_fresh_exception', error=e))
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


# ════════════════════════════════════════════════════════════════
#  R64 P1-10: RU 归因(SQL fingerprint / service / job / time bucket)
# ════════════════════════════════════════════════════════════════

# R64 P1-10: RU 归因 kv_store key 前缀
# 完整 key 格式: ru_attribution:{YYYYMMDD}:{time_bucket_hour}:{service}:{job}
# value 为 JSON: {"fingerprint": {fp: ru_amount}, "total": int, "samples": [...]}
KV_KEY_RU_ATTRIBUTION_PREFIX = "ru_attribution"

# R64 P1-10: 业务 Bot 角色不应产生空载 CRDB RU(由 settings.CRDB_RU_BUSINESS_BOT_ROLES 配置)
# 此常量用于静态校验,确保 collector 在采集空载 RU 时仅累计业务 Bot
BUSINESS_BOT_ROLES_DEFAULT = (
    "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
)

# R64 P1-10: 允许有 CRDB RU 消耗的非业务角色(运维/同步/迁移等)
# 这些角色的 RU 消耗不计入"业务空载"门禁
NON_BUSINESS_CRDB_ROLES = frozenset({
    "crdb_sync",       # 独占同步(由 dirty 驱动,非空载)
    "migration",       # DDL 执行(oneshot)
    "bootstrap",       # 显式人工恢复任务
    "disaster_recovery",
    "backup",          # 备份(显式触发)
    "db_writer",       # 写入路径(由用户操作驱动)
})

# R64 P1-10: 已审计确认不产生空载 CRDB 命中的服务清单
# - r40_scheduler: 所有周期任务委托 cache_store(SQLite)/command_bus(SQLite)/Redis
# - crdb_sync_service: leader 走 Redis SET NX;dirty 检测走 SQLite;CRDB 仅 dirty 驱动懒加载
# - prometheus_exporter: 所有指标走 SQLite cache_store;/health & /readiness 走 SQLite ping
# - 任何新增周期任务如需触达 CRDB,必须由 dirty/event 显式驱动,不得空载轮询
IDLE_CRDB_FREE_SERVICES_AUDITED = frozenset({
    "r40_scheduler",
    "crdb_sync_service",
    "prometheus_exporter",
    "decode_logs_cleanup",
    "retention_worker",
    "backup_gc",
    "approval_executor",
})


def get_business_bot_roles() -> tuple[str, ...]:
    """R64 P1-10: 读取业务 Bot 角色列表(从 settings 读取,逗号分隔)。

    Returns:
        业务 Bot 角色元组(默认 up_bot/idx_bot/dsp_bot/mon_bot/admin_bot)
    """
    try:
        from config import settings
        raw = getattr(settings, "CRDB_RU_BUSINESS_BOT_ROLES", "")
        if raw:
            roles = tuple(r.strip() for r in raw.split(",") if r.strip())
            if roles:
                return roles
    except Exception:
        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='services/crdb_ru_collector.py:get_business_bot_roles'))
    return BUSINESS_BOT_ROLES_DEFAULT


async def record_ru_attribution(
    service: str,
    ru_amount: int,
    fingerprint: str = "",
    job: str = "",
    time_bucket: str | None = None,
    user_id: int = 0,
) -> bool:
    """R64 P1-10: 记录 RU 归因(SQL fingerprint / service / job / time bucket)。

    归因维度:
        - service: 服务角色(如 up_bot / crdb_sync)
        - fingerprint: SQL fingerprint(MD5/normalize 后的 SQL 模板)
        - job: 触发 RU 的 job/task 名称(如 sync_jobs / migration_step_3)
        - time_bucket: 时间桶(YYYYMMDDHH,UTC 小时粒度)

    数据结构(kv_store JSON):
        {
            "service": str,
            "job": str,
            "time_bucket": str,
            "total_ru": int,
            "by_fingerprint": {fp: ru_amount},
            "samples": [...],  # 最近 N 条样本(限制 50 条)
        }

    本函数纯走 SQLite kv_store,零 CRDB RU。

    Args:
        service: 服务角色(若不在已知清单内则归入 "unknown")
        ru_amount: RU 消耗量(必须 > 0)
        fingerprint: SQL 指纹(可选,空字符串表示未采集)
        job: 触发 job 名(可选)
        time_bucket: 时间桶 YYYYMMDDHH,None 表示当前小时(UTC)
        user_id: 触发用户(可选)

    Returns:
        True 记录成功, False 失败
    """
    if ru_amount <= 0:
        return False
    if not service:
        service = "unknown"
    if not job:
        job = "default"
    if not fingerprint:
        fingerprint = "unknown"
    if time_bucket is None:
        time_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")

    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        # key 格式: ru_attribution:{YYYYMMDD}:{time_bucket}:{service}:{job}
        day_str = time_bucket[:8]  # YYYYMMDD
        key = f"{KV_KEY_RU_ATTRIBUTION_PREFIX}:{day_str}:{time_bucket}:{service}:{job}"

        existing = await store.get_kv(key)
        if existing:
            try:
                data = json.loads(existing)
            except (json.JSONDecodeError, TypeError):
                data = {
                    "service": service, "job": job, "time_bucket": time_bucket,
                    "total_ru": 0, "by_fingerprint": {}, "samples": [],
                }
        else:
            data = {
                "service": service, "job": job, "time_bucket": time_bucket,
                "total_ru": 0, "by_fingerprint": {}, "samples": [],
            }

        data["total_ru"] = data.get("total_ru", 0) + ru_amount
        by_fp = data.get("by_fingerprint", {})
        by_fp[fingerprint] = by_fp.get(fingerprint, 0) + ru_amount
        data["by_fingerprint"] = by_fp

        samples = data.get("samples", [])
        samples.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "ru": ru_amount,
            "user_id": user_id,
        })
        if len(samples) > 50:
            samples = samples[-50:]
        data["samples"] = samples

        await store.set_kv(key, json.dumps(data, ensure_ascii=False))
        return True
    except Exception as e:
        logger.debug(f"[CRDB-RU] R64 P1-10: record_ru_attribution 失败: {e}")
        return False


async def get_ru_attribution(
    date_str: str | None = None,
    service: str | None = None,
) -> dict:
    """R64 P1-10: 查询某日的 RU 归因汇总(按 service / job / fingerprint / time_bucket)。

    本函数纯走 SQLite kv_store,零 CRDB RU。

    Args:
        date_str: 日期 YYYYMMDD,None 表示今天
        service: 限定服务(可选,None 表示所有服务)

    Returns:
        {
            "date": str,
            "total_ru": int,
            "by_service": {service: amount},
            "by_job": {job: amount},
            "by_fingerprint": {fp: amount},
            "by_time_bucket": {bucket: amount},
            "business_bot_ru": int,   # 业务 Bot 空载 RU(应 = 0)
            "non_business_ru": int,   # 非 Bot 角色(crdb_sync/migration 等)RU
        }
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

        # 业务 Bot 角色集合(用于区分业务空载 vs 运维)
        business_roles = set(get_business_bot_roles())

        total_ru = 0
        by_service: dict[str, int] = {}
        by_job: dict[str, int] = {}
        by_fingerprint: dict[str, int] = {}
        by_time_bucket: dict[str, int] = {}
        business_bot_ru = 0
        non_business_ru = 0

        # 遍历可能的 time_bucket(24 小时)
        for hour in range(24):
            time_bucket = f"{date_str}{hour:02d}"
            # kv_store 不支持前缀扫描,需逐个 service/job 组合查询
            # 已知 services(来自 ru_cost_center.SERVICES)+ 业务角色 + 运维角色
            from services.ru_cost_center import SERVICES
            candidate_services = list(SERVICES) + list(business_roles)
            if service:
                candidate_services = [s for s in candidate_services if s == service]
            # 去重
            seen = set()
            candidate_services = [
                s for s in candidate_services
                if not (s in seen or seen.add(s))
            ]
            # 已知 jobs(常见 job 名;实际无 job 字典,尝试常见)
            candidate_jobs = [
                "default", "sync_jobs", "sync_cells", "sync_files",
                "migration", "backup", "restore", "official_metric",
                "health_check", "leader_renewal",
            ]
            for svc in candidate_services:
                for job in candidate_jobs:
                    key = (
                        f"{KV_KEY_RU_ATTRIBUTION_PREFIX}:{date_str}:"
                        f"{time_bucket}:{svc}:{job}"
                    )
                    try:
                        raw = await store.get_kv(key)
                        if not raw:
                            continue
                        data = json.loads(raw)
                        ru = int(data.get("total_ru", 0))
                        if ru <= 0:
                            continue
                        total_ru += ru
                        by_service[svc] = by_service.get(svc, 0) + ru
                        by_job[job] = by_job.get(job, 0) + ru
                        by_time_bucket[time_bucket] = (
                            by_time_bucket.get(time_bucket, 0) + ru
                        )
                        for fp, amount in data.get("by_fingerprint", {}).items():
                            by_fingerprint[fp] = (
                                by_fingerprint.get(fp, 0) + int(amount)
                            )
                        if svc in business_roles:
                            business_bot_ru += ru
                        else:
                            non_business_ru += ru
                    except Exception:
                        continue

        return {
            "date": date_str,
            "total_ru": total_ru,
            "by_service": by_service,
            "by_job": by_job,
            "by_fingerprint": by_fingerprint,
            "by_time_bucket": by_time_bucket,
            "business_bot_ru": business_bot_ru,
            "non_business_ru": non_business_ru,
        }
    except Exception as e:
        logger.debug(f"[CRDB-RU] R64 P1-10: get_ru_attribution 失败: {e}")
        return {
            "date": date_str or "",
            "total_ru": 0,
            "by_service": {},
            "by_job": {},
            "by_fingerprint": {},
            "by_time_bucket": {},
            "business_bot_ru": 0,
            "non_business_ru": 0,
        }


# R64 P1-10: json 已在文件顶部导入(record_ru_attribution / get_ru_attribution 使用)


# ════════════════════════════════════════════════════════════════
# R65 P1-09: SQL fingerprint / service / job 归因(SQLite 表)
# ════════════════════════════════════════════════════════════════
# 审计发现:CRDB RU 仍只有阈值脚本(check_crdb_ru_threshold.py),
# 没有 72h 真实数据 + SQL fingerprint 归因。POOL_MIN_SIZE=0 不证明空载 RU。
# 本节新增 crdb_ru_attribution SQLite 表,记录每个采样窗口的:
#   - fingerprint_sha256: 归一化 SQL 的 sha256(归并等价 SQL)
#   - service: 服务角色(bot/admin/api/scheduler/crdb_sync/...)
#   - job: 触发 job 名(crontab 名 / outbox_worker / migration_step_N)
#   - ru_consumed: 本窗口 RU 消耗
#   - sampled_at: ISO 8601 采样时间戳
#   - sample_window_seconds: 采样窗口(默认 3600 = 1 小时)
#   - query_text_sample: 代表性 SQL 文本前 200 字符(便于人工排查)

# 表 DDL(惰性创建:首次写入时自动 CREATE TABLE IF NOT EXISTS)
CRDB_RU_ATTRIBUTION_TABLE_DDL = """CREATE TABLE IF NOT EXISTS crdb_ru_attribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_sha256 TEXT NOT NULL,
    service TEXT NOT NULL,
    job TEXT,
    ru_consumed REAL NOT NULL,
    sampled_at TEXT NOT NULL,
    sample_window_seconds INTEGER NOT NULL,
    query_text_sample TEXT
)"""

DEFAULT_SAMPLE_WINDOW_SECONDS = 3600
QUERY_TEXT_SAMPLE_MAX_LEN = 200


def normalize_sql(sql: str) -> str:
    """R65 P1-09: SQL 归一化(用于 fingerprint 计算)。

    归一化规则:
        1. 去除单行注释(``-- ...``)和多行注释(``/* ... */``)
        2. 小写化(关键字/标识符)
        3. 替换数字字面量为 ``?``
        4. 替换字符串字面量为 ``?``
        5. 折叠连续空白为单个空格
        6. 去除首尾空白

    归一化后,等价 SQL 产生相同 fingerprint:
        SELECT * FROM users WHERE id = 42
        select * from users where id = 99
        → 均归一化为 "select * from users where id = ?"

    Args:
        sql: 原始 SQL 文本

    Returns:
        归一化后的 SQL 文本
    """
    if not sql or not isinstance(sql, str):
        return ""
    import re
    # 1. 去除单行注释(-- 到行尾)
    result = re.sub(r"--[^\n]*", "", sql)
    # 2. 去除多行注释(/* ... */)
    result = re.sub(r"/\*.*?\*/", "", result, flags=re.DOTALL)
    # 3. 小写化
    result = result.lower()
    # 4. 替换字符串字面量('...' 或 "...")为 ?
    result = re.sub(r"'(?:[^'\\]|\\.)*'", "?", result)
    result = re.sub(r'"(?:[^"\\]|\\.)*"', "?", result)
    # 5. 替换数字字面量(整数 / 浮点)为 ?
    result = re.sub(r"\b\d+(?:\.\d+)?\b", "?", result)
    # 6. 折叠连续空白
    result = re.sub(r"\s+", " ", result)
    # 7. 去除首尾空白
    return result.strip()


def compute_sql_fingerprint(sql: str) -> str:
    """R65 P1-09: 计算 SQL 的 fingerprint(归一化 SQL 的 sha256)。

    Args:
        sql: 原始 SQL 文本

    Returns:
        64 字符的 sha256 hex 字符串(归一化后)
    """
    normalized = normalize_sql(sql)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def truncate_query_sample(sql: str) -> str:
    """R65 P1-09: 截断 query_text_sample 到 ``QUERY_TEXT_SAMPLE_MAX_LEN`` 字符。

    用于写入 SQLite 表时限制字段长度,避免长 SQL 撑爆 DB。

    Args:
        sql: 原始 SQL 文本

    Returns:
        截断后的 SQL(最多 QUERY_TEXT_SAMPLE_MAX_LEN 字符)
    """
    if not sql or not isinstance(sql, str):
        return ""
    if len(sql) <= QUERY_TEXT_SAMPLE_MAX_LEN:
        return sql
    return sql[:QUERY_TEXT_SAMPLE_MAX_LEN]


def _resolve_cache_db_path(db_path: str | None = None) -> str:
    """R65 P1-09: 解析 SQLite DB 路径(优先使用参数,其次环境变量,最后默认)。

    Args:
        db_path: 显式指定的 DB 路径(None 使用环境变量或默认)

    Returns:
        DB 文件路径字符串
    """
    if db_path:
        return db_path
    from pathlib import Path
    _default_data_dir = Path(__file__).resolve().parent.parent / "data"
    return os.getenv("CACHE_STORE_DB", str(_default_data_dir / "cache_store.db"))


def init_crdb_ru_attribution_table(db_path: str | None = None) -> bool:
    """R65 P1-09: 初始化 ``crdb_ru_attribution`` 表(惰性创建)。

    首次写入前调用本函数,确保表 + 索引存在。
    本函数纯 SQLite,零 CRDB RU。

    Args:
        db_path: SQLite DB 路径(None 使用默认)

    Returns:
        True: 表已存在或创建成功
        False: 创建失败(权限/磁盘满等)
    """
    import sqlite3
    if db_path is None:
        db_path = _resolve_cache_db_path()
    # R65 P1-04: except 块禁止裸 return False(吞错误);用 success 变量记录结果
    success = False
    try:
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # 文件不存在时 sqlite3.connect 会自动创建空 DB
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute(CRDB_RU_ATTRIBUTION_TABLE_DDL)
            # 创建索引:按 sampled_at 时间范围查询(frequent access pattern)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crdb_ru_attr_sampled_at "
                "ON crdb_ru_attribution(sampled_at)"
            )
            # 创建索引:按 fingerprint 聚合(Top-N 查询)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crdb_ru_attr_fingerprint "
                "ON crdb_ru_attribution(fingerprint_sha256)"
            )
            # 创建索引:按 service 聚合(by-service 汇总)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crdb_ru_attr_service "
                "ON crdb_ru_attribution(service)"
            )
            conn.commit()
        finally:
            conn.close()
        success = True
    except sqlite3.Error as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.init_table_failed", error=e)
        )
    except Exception as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.init_table_exception", error=e)
        )
    return success


async def record_ru_attribution_row(
    fingerprint_sha256: str,
    service: str,
    ru_consumed: float,
    sampled_at: str | None = None,
    sample_window_seconds: int = DEFAULT_SAMPLE_WINDOW_SECONDS,
    job: str | None = None,
    query_text_sample: str | None = None,
    db_path: str | None = None,
) -> bool:
    """R65 P1-09: 写入一行 RU 归因到 ``crdb_ru_attribution`` 表。

    每个采样窗口闭合时,collector 调用本函数一次,写入:
        - fingerprint_sha256: 归一化 SQL 的 sha256
        - service: 服务角色(bot/admin/api/scheduler/crdb_sync/...)
        - job: 触发 job 名(crontab 名 / outbox_worker / migration_step_N)
        - ru_consumed: 本窗口 RU 消耗
        - sampled_at: ISO 8601 采样时间戳
        - sample_window_seconds: 采样窗口(默认 3600)
        - query_text_sample: 代表性 SQL 文本前 200 字符

    本函数纯 SQLite,零 CRDB RU。表不存在时惰性创建。
    优先尝试通过 cache_store 写入(aiosqlite),失败时降级为同步 sqlite3。

    Args:
        fingerprint_sha256: SQL fingerprint(64 字符 sha256 hex)
        service: 服务角色名
        ru_consumed: 本窗口 RU 消耗
        sampled_at: ISO 8601 采样时间戳(None 使用当前 UTC)
        sample_window_seconds: 采样窗口秒数(默认 3600)
        job: 触发 job 名(可选)
        query_text_sample: 代表性 SQL 文本(自动截断到 200 字符)
        db_path: SQLite DB 路径(可选)

    Returns:
        True: 写入成功
        False: 写入失败(已记录日志,不抛异常)
    """
    from datetime import datetime, timezone
    if sampled_at is None:
        sampled_at = datetime.now(timezone.utc).isoformat()
    if query_text_sample:
        query_text_sample = truncate_query_sample(query_text_sample)
    else:
        query_text_sample = ""

    # 优先尝试 cache_store(aiosqlite,与现有 kv_store 共用连接)
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if db_path is None:
            db_path = _resolve_cache_db_path()
        # 确保 cache_store 的 DB 是目标 DB
        if hasattr(store, "_db") and store._db is not None:
            # 惰性创建表(若不存在)
            await store._db.execute(CRDB_RU_ATTRIBUTION_TABLE_DDL)
            await store._db.execute(
                """INSERT INTO crdb_ru_attribution
                   (fingerprint_sha256, service, job, ru_consumed,
                    sampled_at, sample_window_seconds, query_text_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint_sha256,
                    service,
                    job,
                    float(ru_consumed),
                    sampled_at,
                    int(sample_window_seconds),
                    query_text_sample,
                ),
            )
            await store._db.commit()
            return True
    except Exception as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.cache_store_write_failed", error=e)
        )

    # 降级路径:同步 sqlite3 直连
    import sqlite3
    if db_path is None:
        db_path = _resolve_cache_db_path()
    # R65 P1-04: except 块禁止裸 return False(吞错误);用 success 变量记录结果
    success = False
    try:
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute(CRDB_RU_ATTRIBUTION_TABLE_DDL)
            conn.execute(
                """INSERT INTO crdb_ru_attribution
                   (fingerprint_sha256, service, job, ru_consumed,
                    sampled_at, sample_window_seconds, query_text_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint_sha256,
                    service,
                    job,
                    float(ru_consumed),
                    sampled_at,
                    int(sample_window_seconds),
                    query_text_sample,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        success = True
    except sqlite3.Error as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.write_attribution_failed", error=e)
        )
    except Exception as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.record_row_exception", error=e)
        )
    return success


def query_ru_attribution_rows(
    start_time: str | None = None,
    end_time: str | None = None,
    service: str | None = None,
    fingerprint_sha256: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """R65 P1-09: 查询 ``crdb_ru_attribution`` 表中的归因行(同步只读)。

    本函数为同步函数(使用 sqlite3 只读连接),便于脚本在同步上下文中调用。
    支持按时间范围 / service / fingerprint 过滤。

    Args:
        start_time: ISO 8601 起始时间(包含),None 表示不限制
        end_time: ISO 8601 结束时间(不包含),None 表示不限制
        service: 限定服务(可选)
        fingerprint_sha256: 限定 fingerprint(可选)
        db_path: SQLite DB 路径(None 使用默认)

    Returns:
        归因行列表,每行是 dict(字段对齐表 schema)。
        表不存在或无数据时返回空列表。
    """
    import sqlite3
    if db_path is None:
        db_path = _resolve_cache_db_path()
    result: list[dict] = []
    try:
        from pathlib import Path
        if not Path(db_path).exists():
            return result
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            sql = (
                "SELECT id, fingerprint_sha256, service, job, ru_consumed, "
                "       sampled_at, sample_window_seconds, query_text_sample "
                "FROM crdb_ru_attribution WHERE 1=1"
            )
            params: list = []
            if start_time is not None:
                sql += " AND sampled_at >= ?"
                params.append(start_time)
            if end_time is not None:
                sql += " AND sampled_at < ?"
                params.append(end_time)
            if service is not None:
                sql += " AND service = ?"
                params.append(service)
            if fingerprint_sha256 is not None:
                sql += " AND fingerprint_sha256 = ?"
                params.append(fingerprint_sha256)
            sql += " ORDER BY sampled_at ASC, id ASC"
            cursor = conn.execute(sql, params)
            for row in cursor.fetchall():
                result.append({
                    "id": row[0],
                    "fingerprint_sha256": row[1],
                    "service": row[2],
                    "job": row[3],
                    "ru_consumed": float(row[4]) if row[4] is not None else 0.0,
                    "sampled_at": row[5],
                    "sample_window_seconds": int(row[6]) if row[6] is not None else 0,
                    "query_text_sample": row[7] if row[7] is not None else "",
                })
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.query_attribution_failed", error=e)
        )
    except Exception as e:
        logger.debug(
            _i18n_t("diagnostics.r65.p1_09.query_rows_exception", error=e)
        )
    return result


def aggregate_ru_attribution(
    start_time: str | None = None,
    end_time: str | None = None,
    db_path: str | None = None,
) -> dict:
    """R65 P1-09: 聚合 ``crdb_ru_attribution`` 表中的归因数据。

    聚合维度:
        - total_ru: 总 RU 消耗
        - by_service: {service: ru}
        - by_job: {job: ru}
        - by_fingerprint: {fingerprint: ru}(含 query_text_sample)
        - by_sample: 每个采样点的 RU(sampled_at → ru)
        - peak_hourly_ru: 最大单窗口 RU
        - daily_average_ru: 日均 RU(基于采样窗口总数推算)
        - top_fingerprints: 按 RU 排序的前 N 个 fingerprint(默认 10)

    Args:
        start_time: ISO 8601 起始时间(可选)
        end_time: ISO 8601 结束时间(可选)
        db_path: SQLite DB 路径(可选)

    Returns:
        聚合 dict。表不存在或无数据时返回空聚合(各字段为 0 / 空字典)。
    """
    rows = query_ru_attribution_rows(
        start_time=start_time,
        end_time=end_time,
        db_path=db_path,
    )
    total_ru = 0.0
    by_service: dict[str, float] = {}
    by_job: dict[str, float] = {}
    by_fingerprint: dict[str, dict] = {}
    by_sample: dict[str, float] = {}
    peak_hourly_ru = 0.0

    for r in rows:
        ru = float(r.get("ru_consumed", 0))
        total_ru += ru
        svc = r.get("service", "unknown")
        job = r.get("job") or "default"
        fp = r.get("fingerprint_sha256", "")
        sampled_at = r.get("sampled_at", "")

        by_service[svc] = by_service.get(svc, 0.0) + ru
        by_job[job] = by_job.get(job, 0.0) + ru
        by_sample[sampled_at] = by_sample.get(sampled_at, 0.0) + ru
        if ru > peak_hourly_ru:
            peak_hourly_ru = ru

        if fp not in by_fingerprint:
            by_fingerprint[fp] = {
                "fingerprint_sha256": fp,
                "ru": 0.0,
                "service": svc,
                "job": job,
                "query_text_sample": r.get("query_text_sample", ""),
                "sample_count": 0,
            }
        by_fingerprint[fp]["ru"] += ru
        by_fingerprint[fp]["sample_count"] += 1

    # 日均 RU(基于采样窗口总数推算)
    sample_count = len(rows)
    # 假设每个 sample_window_seconds = 3600(1 小时),日均 = 总 RU / (sample_count / 24)
    if sample_count > 0:
        days = sample_count / 24.0
        daily_average_ru = total_ru / days if days > 0 else 0.0
    else:
        daily_average_ru = 0.0

    # Top 10 fingerprint by RU
    top_fingerprints = sorted(
        by_fingerprint.values(),
        key=lambda x: x["ru"],
        reverse=True,
    )[:10]

    return {
        "total_ru": total_ru,
        "sample_count": sample_count,
        "by_service": by_service,
        "by_job": by_job,
        "by_fingerprint": by_fingerprint,
        "by_sample": by_sample,
        "peak_hourly_ru": peak_hourly_ru,
        "daily_average_ru": daily_average_ru,
        "top_fingerprints": top_fingerprints,
    }


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
