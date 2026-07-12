"""R39 P1-9: CRDB RU 指标采集器(占位实现)。

职责:
    周期性从 CockroachDB Cloud Metrics API / Datadog / PromQL 拉取
    过去 24 小时的 RU 消耗总量,写入本地 kv_store.crdb_ru_daily,
    供 prometheus_exporter 暴露为 crdb_ru_daily 指标。

背景(R39 P1-9):
    原 prometheus_exporter 读取 kv_store.crdb_ru_daily,但无人写入该值,
    导致指标长期显示 0。本模块填补"采集闭环"缺失环节。

实现状态:
    R39 P1-9 仅提供占位骨架 + 文档说明。
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
import os
import sys
import time
from datetime import datetime, timezone

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

# R39 P1-9: CRDB Cloud API 配置(运维通过环境变量注入)
# 真正实现时需提供 CRDB_CLOUD_API_KEY,否则 collector 降级为"占位模式"
CRDB_CLOUD_API_KEY = os.environ.get("CRDB_CLOUD_API_KEY", "")
CRDB_CLOUD_CLUSTER_ID = os.environ.get("CRDB_CLOUD_CLUSTER_ID", "")
CRDB_CLOUD_API_BASE = os.environ.get(
    "CRDB_CLOUD_API_BASE_URL",
    "https://cockroachlabs.cloud/api/v1",
)


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

    # R39 P1-9: 占位 — 真正实现见 docs/crdb-ru-metrics.md §3
    # TODO(运维): 实现 CRDB Cloud API 调用
    logger.info(
        f"[CRDB-RU] R39 P1-9: 占位 — 真正的 CRDB Cloud API 调用未实现,"
        f"cluster_id={CRDB_CLOUD_CLUSTER_ID}, base={CRDB_CLOUD_API_BASE}"
    )
    return None


async def write_ru_to_kv_store(ru_value: float) -> bool:
    """R39 P1-9: 将当日 RU 消耗写入 kv_store.crdb_ru_daily。

    写入成功后,prometheus_exporter 下次 scrape 会暴露更新后的 crdb_ru_daily 指标。
    kv_store 写入零 CRDB RU(SQLite 本地存储)。

    Returns:
        True: 写入成功
        False: 写入失败(下次重试)
    """
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        await store.set_kv(KV_KEY_CRDB_RU_DAILY, str(ru_value))
        logger.info(
            f"[CRDB-RU] R39 P1-9: kv_store.crdb_ru_daily 已更新 → {ru_value:.0f} RU"
        )
        return True
    except Exception as e:
        logger.error(f"[CRDB-RU] R39 P1-9: 写入 kv_store 失败: {e}")
        return False


async def _collect_once() -> None:
    """R39 P1-9: 单次采集 RU 指标并写入 kv_store。"""
    ru_value = await fetch_ru_from_crdb_cloud()
    if ru_value is None:
        # 占位模式或采集失败,不更新 kv_store(保持原值)
        return
    if not isinstance(ru_value, (int, float)) or ru_value < 0:
        logger.warning(
            f"[CRDB-RU] R39 P1-9: RU 值非法({ru_value}),跳过写入"
        )
        return
    await write_ru_to_kv_store(float(ru_value))


async def _collect_loop() -> None:
    """R39 P1-9: 主采集循环(每小时一次)。"""
    logger.info(
        f"[CRDB-RU] R39 P1-9: collector 启动,"
        f"间隔 {COLLECT_INTERVAL_SECONDS}s,"
        f"API Key 配置: {'已配置' if CRDB_CLOUD_API_KEY else '未配置(占位模式)'}"
    )
    # 启动时立即采集一次
    await _collect_once()
    while True:
        try:
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)
            await _collect_once()
        except asyncio.CancelledError:
            logger.info("[CRDB-RU] R39 P1-9: collector 收到取消信号,退出")
            raise
        except Exception as e:
            logger.error(f"[CRDB-RU] R39 P1-9: 采集循环异常: {e}")
            await asyncio.sleep(60)  # 异常后等待 1 分钟再重试


def _handle_signal(signum, frame) -> None:
    """R39 P1-9: 信号处理(SIGTERM/SIGINT 优雅退出)。"""
    logger.info(f"[CRDB-RU] R39 P1-9: 收到信号 {signum},准备退出")
    sys.exit(0)


def main() -> None:
    """R39 P1-9: collector 入口。"""
    import signal
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(_collect_loop())
    except KeyboardInterrupt:
        logger.info("[CRDB-RU] R39 P1-9: KeyboardInterrupt,退出")


if __name__ == "__main__":
    main()
