"""R39 P1-9 / R41 RU 门禁: CRDB RU 指标采集器。

职责:
    周期性从 CockroachDB Cloud Metrics API / Datadog / PromQL 拉取
    过去 24 小时的 RU 消耗总量,写入本地 kv_store.crdb_ru_daily,
    供 prometheus_exporter 暴露为 crdb_ru_daily 指标。

R41 RU 门禁新增:
    - 采集业务 Bot 空载 RU(kv_store.crdb_idle_ru_daily)
      业务 Bot 不应触发 CRDB RU,本指标用于门禁告警
    - 静态扫描门禁: COCKROACHDB_URL 仅 crdb_sync/migration/disaster_recovery 可读
      其他业务服务读取 COCKROACHDB_URL 视为违规(由测试 test_r41_ru_gate 验证)

背景(R39 P1-9):
    原 prometheus_exporter 读取 kv_store.crdb_ru_daily,但无人写入该值,
    导致指标长期显示 0。本模块填补"采集闭环"缺失环节。

实现状态:
    R39 P1-9 提供占位骨架 + 文档说明。
    R41 改进:新增业务 Bot 空载 RU 采集路径 + 静态门禁辅助。
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


async def write_idle_ru_to_kv_store(ru_value: float) -> bool:
    """R41 RU 门禁: 将业务 Bot 空载 RU 写入 kv_store.crdb_idle_ru_daily。

    写入成功后,prometheus_exporter 暴露 tgjiema_crdb_idle_ru_daily 指标。
    kv_store 写入零 CRDB RU(SQLite 本地存储)。

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
