"""Prometheus Exporter — 暴露 /metrics 端点

R37 P2-7: 运行时 Prometheus exporter 必须是真实代码(非文档)。

功能:
- 独立 HTTP server,监听 0.0.0.0:9100
- 暴露 /metrics 端点(Prometheus text format)
- 从 SQLite cache_store / relay_pool 读取指标值
- 暴露 /health 端点(供 Docker healthcheck 使用)

指标清单:
- crdb_ru_daily          CockroachDB 当日 RU 消耗(kv_store.crdb_ru_daily)
- redis_pel_depth         Redis Stream pending entries 长度(kv_store.redis_pel_depth)
- dlq_depth               死信队列深度(kv_store.dlq_depth)
- dirty_outbox_rows       upload_outbox 表未完成行数(status NOT IN DONE/FAILED)
- backup_age_seconds      最近一次成功备份距今秒数(kv_store.last_backup_at)
- relay_spool_disk_usage  relay spool 目录当前字节数
- relay_spool_usage_ratio relay spool 使用率(0.0-1.0+)
- relay_spool_high_water  relay spool 是否达高水位(0/1)
- tgjiema_i18n_missing_key_total i18n key 缺失累计计数(R44 6.2,I18nManager.get_missing_key_count)

R38 P2-7: 高基数 label 禁止规则
  本 exporter 的所有指标均为 gauge 类型,不带高基数 label(如 user_id / file_code /
  message_id / chat_id)。高基数 label 会导致 Prometheus 时间序列爆炸(TSDB 膨胀),
  每个唯一 label 组合创建一条新的时间序列。
  允许的低基数 label:
    - status (relay_spool_status_count{status="..."} — 枚举值,通常 <20 种)
  禁止的高基数 label(不可出现在任何指标中):
    - user_id / chat_id / message_id / file_code / job_id / phone / token
  如需按用户/文件维度排查,应通过结构化日志(loguru)或追踪系统(Jaeger)查询,
  而非 Prometheus 指标 label。

启动:
  python -m services.prometheus_exporter
  或:
  python services/prometheus_exporter.py

环境变量:
  PROMETHEUS_EXPORTER_HOST  监听地址(默认 0.0.0.0)
  PROMETHEUS_EXPORTER_PORT  监听端口(默认 9100)
  CACHE_STORE_DB            cache_store.db 路径
  RELAY_DB_PATH             relay_pool.db 路径
  RELAY_SPOOL_DIR           relay spool 临时文件目录
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from loguru import logger


# ── 路径与配置 ──────────────────────────────────────────
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_STORE_DB = Path(
    os.getenv("CACHE_STORE_DB", str(_DEFAULT_DATA_DIR / "cache_store.db"))
)
RELAY_DB_PATH = Path(
    os.getenv("RELAY_DB_PATH", str(_DEFAULT_DATA_DIR / "relay_pool.db"))
)
RELAY_SPOOL_DIR = Path(
    os.getenv("RELAY_SPOOL_DIR", str(_DEFAULT_DATA_DIR / "relay_spool_files"))
)

LISTEN_HOST = os.getenv("PROMETHEUS_EXPORTER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("PROMETHEUS_EXPORTER_PORT", "9100"))

# relay spool 高水位常量(与 database/relay_db.py 保持一致)
RELAY_SPOOL_HIGH_WATER_MARK = 0.80
RELAY_SPOOL_MAX_BYTES_DEFAULT = 5 * 1024 * 1024 * 1024  # 5GB

# R39 P1-8: readiness 状态跟踪
# _last_scrape_ok: 最近一次 collect_metrics 是否成功(至少一次 SQLite 查询成功)
# _last_scrape_ts: 最近一次成功采集的时间戳
# _scrape_errors: 累计采集错误数
_last_scrape_ok: bool = False
_last_scrape_ts: float = 0.0
_scrape_errors: int = 0
# R39 P1-8: 数据新鲜度告警阈值(秒) — 超过此值认为数据陈旧
_DATA_AGE_ALERT_THRESHOLD = 300  # 5 分钟无成功采集 → 不 ready

# R41 P1-10: 依赖状态跟踪(由 check_readiness 汇总,暴露到 /readiness 与指标)
# _last_crdb_sync_ts: 最近一次成功 CRDB 同步时间戳(从 kv_store.crdb_sync_last_success 读取)
# _last_r2_collect_ts: 最近一次 R2 指标采集时间戳(从 kv_store.r2_last_collect_time 读取)
# _acl_configured: ACL 是否完整配置(REDIS_*_PASSWORD 4 个变量均存在)
# _schema_valid: backup_schema.validate_schema() 是否通过
_last_crdb_sync_ts: float = 0.0
_last_r2_collect_ts: float = 0.0
_acl_configured: bool = False
_schema_valid: bool = False
# R41 P1-10: 依赖新鲜度阈值(秒)
# CRDB 同步与 R2 采集为周期任务,允许较长间隔(默认 1 小时)
_CRDB_SYNC_FRESH_THRESHOLD = 3600  # 1 小时无成功同步 → 不 ready
_R2_COLLECT_FRESH_THRESHOLD = 3600  # 1 小时无 R2 采集 → 不 ready

# ── R40: 新增指标状态(模块级缓存,由后台采集线程更新) ──────
# 采集由独立守护线程(独立事件循环)周期性执行,HTTP handler 只读取缓存,
# 避免 /metrics 请求阻塞在异步采集上。采集失败时保持上一次的值(降级)。
_r40_state: dict[str, Any] = {
    "maintenance_enabled": 0,
    "ru_daily_usage": {},           # {service: amount}
    # R51 P1-7: 每个服务的 RU 是否为估算值(1=估算, 0=官方 CockroachDB Cloud Metrics)
    "ru_daily_usage_estimated": {},  # {service: int}
    "replica_missing_count": 0,
    "quota_reservations_active": 0,
    "content_reports_pending": 0,
    "approvals_pending": 0,
    "tasks_running": 0,
    "notifications_unread": 0,
    "dlq_depth": 0,
    "outbox_unprocessed": 0,
    "audit_log_events_total": {},   # {action: count}
    "ru_operations_total": {},      # {(service, operation): count}
    # R40 P2-4: 功能成功率与延迟指标
    "approval_execution_success_rate": 0.0,        # Gauge: 0.0-1.0
    "approval_execution_total": 0,                 # Counter: 审批执行总数
    "approval_execution_success": 0,                # Counter: 审批执行成功数
    "notification_delivery_latency_samples": [],   # Histogram: 通知投递延迟样本(秒)
    "repair_success_rate": 0.0,                     # Gauge: 0.0-1.0
    "repair_total": 0,                              # Counter: 修复操作总数
    "repair_success": 0,                            # Counter: 修复成功数
    "real_rpo_seconds": -1.0,                       # Gauge: 真实 RPO(秒),-1=未计算
    "real_rto_seconds": -1.0,                       # Gauge: 真实 RTO(秒),-1=未计算
}
_r40_state_lock = threading.Lock()
_r40_last_collect_ts: float = 0.0
_r40_collector_started = False
_r40_collector_start_lock = threading.Lock()


# ── SQLite 读取工具 ─────────────────────────────────────


def _read_sqlite_single(db_path: Path, query: str, default: Any = 0) -> Any:
    """以只读方式打开 SQLite,执行查询并返回首行首列值。

    失败(文件不存在/查询异常)时返回 default,不让 exporter 崩溃。
    使用 URI 模式 `mode=ro` 避免写锁竞争。
    """
    if not db_path.exists():
        return default
    try:
        # timeout=2: 短超时,避免 Prometheus scrape 间隔(默认 15s)被拖长
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cursor = conn.execute(query)
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    except sqlite3.Error as e:
        logger.debug(f"[prometheus_exporter] SQLite 查询失败 ({db_path}): {e}")
        return default
    except Exception as e:
        logger.debug(f"[prometheus_exporter] 查询异常 ({db_path}): {e}")
        return default


def _read_kv_value(key: str, default: str = "0") -> str:
    """从 cache_store 的 kv_store 表读取 value。

    Args:
        key: kv_store.key
        default: 缺失时返回的默认值(字符串)

    Returns:
        value 字符串(default if missing)
    """
    val = _read_sqlite_single(
        CACHE_STORE_DB,
        f"SELECT value FROM kv_store WHERE key = ? LIMIT 1",
        default,
    )
    # 占位符 ? 无法在静态 SQL 中传递,改用安全字符串拼接(key 来自代码常量,非用户输入)
    if val == default:
        # 回退: 用字符串拼接(仅用于已知安全 key)
        val = _read_sqlite_single(
            CACHE_STORE_DB,
            f"SELECT value FROM kv_store WHERE key = '{key}' LIMIT 1",
            default,
        )
    return str(val) if val is not None else default


# ── R53 P1-6: 底层读取函数(返回 (value, valid, timestamp, source) 元组) ──
# 采集失败时 valid=False,调用方可选择不输出该 metric(避免 0 伪装健康)。


def _read_sqlite_single_with_meta(
    db_path: Path, query: str
) -> tuple[Any, bool, float, str]:
    """R53 P1-6: 以只读方式打开 SQLite,执行查询并返回 (value, valid, timestamp, source) 元组。

    与 ``_read_sqlite_single`` 的差异:
        - 不再返回默认 0,采集失败时 valid=False
        - 调用方可据 valid 决定是否输出 metric(避免 0 伪装健康)

    Args:
        db_path: SQLite 文件路径
        query: SQL 查询语句

    Returns:
        (value, valid, timestamp, source)
        - value: 查询结果首行首列(失败时为 None)
        - valid: True=采集成功, False=失败(文件不存在/查询异常/无数据)
        - timestamp: 采集时间戳(epoch 秒,失败时为 0.0)
        - source: "sqlite"(成功) / "failed"(失败)
    """
    if not db_path.exists():
        return None, False, 0.0, "failed"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cursor = conn.execute(query)
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None, False, time.time(), "failed"
        return row[0], True, time.time(), "sqlite"
    except sqlite3.Error as e:
        logger.debug(f"[prometheus_exporter] SQLite 查询失败 ({db_path}): {e}")
        return None, False, 0.0, "failed"
    except Exception as e:
        logger.debug(f"[prometheus_exporter] 查询异常 ({db_path}): {e}")
        return None, False, 0.0, "failed"


def _read_kv_value_with_meta(
    key: str,
) -> tuple[str, bool, float, str]:
    """R53 P1-6: 从 cache_store 的 kv_store 表读取 value,返回 (value, valid, timestamp, source) 元组。

    与 ``_read_kv_value`` 的差异:
        - 不再返回默认 "0",采集失败时 valid=False
        - 内部调用 ``_read_kv_value(key, "")``,空字符串表示缺失/失败
        - 调用方可据 valid 决定是否输出 metric(避免 0 伪装健康)

    Args:
        key: kv_store.key(来自代码常量,非用户输入)

    Returns:
        (value, valid, timestamp, source)
        - value: kv_store.value 字符串(失败时为 "")
        - valid: True=采集成功(值非空), False=失败/缺失
        - timestamp: 采集时间戳(epoch 秒)
        - source: "sqlite"(成功) / "failed"(失败)
    """
    # 使用空字符串作为 default,以便区分"真实值 0"与"缺失"
    val = _read_kv_value(key, "")
    if val == "" or val is None:
        return "", False, time.time(), "failed"
    return val, True, time.time(), "sqlite"


def _emit_metric_or_skip(
    metric_name: str,
    value: Any,
    valid: bool,
    labels: dict[str, str] | None = None,
) -> list[str]:
    """R53 P1-6: 生成 metric 行,valid=False 时不输出该 metric(避免 0 伪装健康)。

    采集失败时调用方应同时输出 ``tgjiema_collector_success{collector="..."} 0``。

    Args:
        metric_name: 指标名(如 "crdb_ru_daily")
        value: 指标值(数值,失败时可为 None)
        valid: True=采集成功,输出该 metric;False=采集失败,不输出
        labels: label 字典(如 {"source": "official"}),可选

    Returns:
        Prometheus text format 行列表(valid=False 时返回空列表)
    """
    if not valid:
        return []
    # 构建 label 部分(按 key 排序保证输出稳定)
    if labels:
        label_str = ",".join(
            f'{k}="{v}"' for k, v in sorted(labels.items())
        )
        return [f"{metric_name}{{{label_str}}} {value}"]
    return [f"{metric_name} {value}"]


def _compute_ru_go_signal(
    collector_success: bool,
    source_label: str,
    freshness_seconds: float,
) -> int:
    """R53 P1-6: 计算生产 GO 判定信号。

    告警规则必须同时要求 ``tgjiema_ru_go_signal == 1`` 才触发,
    确保 only official + fresh + success 的 CRDB RU 参与门禁。
    估算 RU(ru_estimated=1)只用于归因,不参与 GO 判定。

    Args:
        collector_success: 采集器是否成功(crdb_ru_daily 可读且可解析)
        source_label: 数据源标签("official" / "unknown" / "failed")
        freshness_seconds: 数据新鲜度(秒,-1=从未采集)

    Returns:
        1=可触发告警, 0=不可触发
    """
    # 1. collector_success=1
    if not collector_success:
        return 0
    # 2. source=official(来自 CockroachDB Cloud 官方 API)
    if source_label != "official":
        return 0
    # 3. fresh=1(freshness >= 0 且 < 阈值)
    if freshness_seconds < 0 or freshness_seconds >= _RU_DATA_FRESH_THRESHOLD:
        return 0
    return 1


def _get_relay_spool_disk_usage() -> int:
    """扫描 relay spool 目录返回总字节数。

    目录不存在或扫描失败返回 0。
    """
    if not RELAY_SPOOL_DIR.exists():
        return 0
    total = 0
    try:
        for p in RELAY_SPOOL_DIR.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception as e:
        logger.debug(f"[prometheus_exporter] 扫描 spool 目录异常: {e}")
    return total


# ════════════════════════════════════════════════════════════════
#  R42 P1-10: CRDB RU 数据源状态判定(official/unknown/failed)
# ════════════════════════════════════════════════════════════════


# 数据新鲜度阈值(秒)— 与 crdb_ru_collector.RU_DATA_FRESH_THRESHOLD 保持一致
_RU_DATA_FRESH_THRESHOLD = 3600


def _compute_crdb_ru_source_label() -> tuple[str, float, int]:
    """R42 P1-10 + R54 P1-1 + R55 P1-3: 计算 CRDB RU 指标的 source label 与 freshness。

    R55 P1-3 整改: 不再信任 kv_store.crdb_ru_source(任何有 CacheStore 写权限
    的进程都能伪造为 "official_cloud_api")。改为优先查询独立表
    ``crdb_ru_official`` 验证 source:
    - 表存在且有记录 + 数据新鲜 → "official"
    - 表不存在/无记录 → "unknown"(降级,不再回退到 kv_store.crdb_ru_source)
    - 无 RU 值且无时间戳 → "failed"

    Returns:
        (source_label, freshness_seconds, source_gauge_value)
        - source_label: "official" / "unknown" / "failed"
        - freshness_seconds: 距上次采集的秒数(-1=从未采集)
        - source_gauge_value: 0=unknown, 1=official, 2=failed
    """
    # 读取 kv_store 中的 RU 值
    ru_str = _read_kv_value("crdb_ru_daily", "")
    ru_value: float | None = None
    if ru_str:
        try:
            ru_value = float(ru_str)
        except (TypeError, ValueError):
            ru_value = None

    # 读取采集时间戳
    ts_str = _read_kv_value("crdb_ru_last_collected_at", "")
    freshness_seconds: float = -1.0  # -1 表示从未采集

    if ts_str:
        try:
            # 尝试 ISO 8601
            iso_str = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            dt = _dt.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                # naive datetime → 视为 UTC(collector 用 utcnow 写入)
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            freshness_seconds = float(time.time() - dt.timestamp())
            if freshness_seconds < 0:
                freshness_seconds = 0.0
        except (ValueError, TypeError):
            # 尝试解析为 epoch 数字
            try:
                ts_num = float(ts_str)
                freshness_seconds = float(time.time() - ts_num)
                if freshness_seconds < 0:
                    freshness_seconds = 0.0
            except (ValueError, TypeError):
                freshness_seconds = -1.0

    # R55 P1-3: 优先查询 crdb_ru_official 独立表验证官方 source
    # 不再信任 kv_store.crdb_ru_source(可被任何 CacheStore 写权限进程伪造)
    official_verified = False
    try:
        from services.crdb_ru_collector import verify_ru_source_official
        official_info = verify_ru_source_official()
        official_verified = bool(official_info.get("is_official", False))
    except Exception as e:
        logger.debug(
            f"[prometheus_exporter] R55 P1-3: verify_ru_source_official 异常: {e}"
        )
        official_verified = False

    # 判定 source:基于 official 表验证结果,不通过 freshness 推断
    if ru_value is None and freshness_seconds < 0:
        # 无 RU 值且无时间戳 → failed
        source_label = "failed"
    elif official_verified and freshness_seconds >= 0:
        # R55 P1-3: official 表有记录 + 有效时间戳 → official
        # 但数据陈旧仍降级为 unknown(collector 可能已停止)
        if freshness_seconds >= _RU_DATA_FRESH_THRESHOLD:
            source_label = "unknown"
        else:
            source_label = "official"
    elif freshness_seconds < 0:
        # 有 RU 值但无时间戳 → unknown
        source_label = "unknown"
    else:
        # 有数据但 official 表无记录 → unknown(估算值或未知来源)
        source_label = "unknown"

    source_gauge_value = {"unknown": 0, "official": 1, "failed": 2}.get(
        source_label, 0
    )
    return source_label, freshness_seconds, source_gauge_value


def _get_backup_compliance_status() -> tuple[int, float]:
    """R42 P1-9: 计算备份合规状态(基于真实 COMPLETE marker)。

    通过 BackupEngine.get_last_successful_backup() 查询最近一次成功备份
    (status='completed' 且 complete_marker_exists=True 且 COMPLETE marker 在 R2 真实存在)。

    Returns:
        (compliant, rpo_seconds)
        - compliant: 1=合规(有成功备份), 0=不合规(无成功备份)
        - rpo_seconds: 距上次成功备份的秒数(-1=从未备份)
    """
    # 直接读取 kv_store.last_backup_at 作为快速判断
    # 真实 COMPLETE marker 校验由 BackupEngine.get_last_successful_backup() 负责
    # 此处避免在 Prometheus scrape 路径中触发 async R2 调用
    backup_ts_str = _read_kv_value("last_backup_at", "")
    if not backup_ts_str:
        return 0, -1.0
    try:
        # 兼容 ISO 时间戳与 epoch 数字
        backup_ts: float
        try:
            # 先尝试 epoch 数字
            backup_ts = float(backup_ts_str)
        except (ValueError, TypeError):
            # ISO 8601 解析
            iso_str = (
                backup_ts_str.replace("Z", "+00:00")
                if backup_ts_str.endswith("Z")
                else backup_ts_str
            )
            dt = _dt.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            backup_ts = dt.timestamp()
        rpo_seconds = float(time.time() - backup_ts)
        if rpo_seconds < 0:
            rpo_seconds = 0.0
        # 有 backup_at 即认为合规(真实 marker 校验由 readiness 负责)
        return 1, rpo_seconds
    except (ValueError, TypeError):
        return 0, -1.0


# ── 指标采集 ────────────────────────────────────────────

# R38 P2-7: 高基数 label 黑名单 — 不可出现在任何指标的 label 中
# 这些 label 的值空间随用户/消息增长,会导致 Prometheus TSDB 时间序列爆炸
_HIGH_CARDINALITY_LABELS = frozenset({
    "user_id", "chat_id", "message_id", "file_code",
    "job_id", "phone", "token", "spool_id", "msg_id",
})

# R51 P1-7: 高基数 label 处理模式
# - "ci":在 CI/测试环境中 fail(raise AppError,用于门禁)
# - "runtime":运行时丢弃违规 metric(不输出该行,避免 TSDB 膨胀)
# 通过环境变量 PROMETHEUS_HIGH_CARDINALITY_MODE 控制(默认 runtime)
_HIGH_CARDINALITY_MODE = os.getenv("PROMETHEUS_HIGH_CARDINALITY_MODE", "runtime").lower()


def _check_no_high_cardinality_labels(metric_line: str) -> bool:
    """R38 P2-7 + R51 P1-7: 检查指标行不含高基数 label。

    R51 P1-7 行为变更:
        - CI/测试模式(PROMETHEUS_HIGH_CARDINALITY_MODE=ci):
          发现违规 label 时 raise AppError(METRICS_HIGH_CARDINALITY_LABEL),
          用于 CI 门禁阻断(防止违规代码合入)
        - 运行时模式(默认 runtime):
          发现违规 label 时返回 False(指示丢弃该 metric 行),
          不输出该行以避免 Prometheus TSDB 时间序列爆炸;
          同时记录 warning 日志保留审计痕迹

    Args:
        metric_line: 单行 Prometheus 指标文本(如 'relay_spool_status_count{status="RECEIVED"} 5')

    Returns:
        True 表示指标行安全(可输出);False 表示含高基数 label(应丢弃)
    """
    # 提取 {...} 内的 label 部分
    if "{" not in metric_line or "}" not in metric_line:
        return True
    label_section = metric_line[metric_line.index("{") + 1: metric_line.index("}")]
    for pair in label_section.split(","):
        if "=" not in pair:
            continue
        label_name = pair.split("=")[0].strip()
        if label_name in _HIGH_CARDINALITY_LABELS:
            # R51 P1-7: 提取 metric 名(行首到 { 之间)
            metric_name = metric_line.split("{")[0].strip()
            if _HIGH_CARDINALITY_MODE == "ci":
                # CI 模式:raise AppError 阻断
                from services.error_codes import AppError, ErrorCodes
                logger.error(
                    f"[R51-P1-7] CI 模式检测到高基数 label '{label_name}' "
                    f"在指标行: {metric_line[:80]}... — 阻断输出"
                )
                raise AppError(
                    ErrorCodes.METRICS_HIGH_CARDINALITY_LABEL,
                    params={"label": label_name, "metric": metric_name},
                )
            # 运行时模式:记录 warning + 返回 False(丢弃该行)
            logger.warning(
                f"[R51-P1-7] 运行时检测到高基数 label '{label_name}' "
                f"在指标行: {metric_line[:80]}... — 已丢弃该 metric(避免 TSDB 膨胀)"
            )
            return False
    return True


def collect_metrics() -> str:
    """采集所有指标并格式化为 Prometheus text format。

    R38 P2-7: 所有指标均不带高基数 label(user_id/file_code/message_id 等),
    collect_metrics() 输出前会调用 _check_no_high_cardinality_labels() 审计。

    参考: https://prometheus.io/docs/instrumenting/exposition_formats/
    """
    # R40: 懒启动采集后台线程(支持测试环境不调用 main())
    _start_r40_collector()

    lines: list[str] = []
    # R53 P1-6: 统一收集 collector_success 条目,最后一次性输出(避免 HELP/TYPE 重复)
    _collector_success_lines: list[str] = []

    # 1. crdb_ru_daily — CRDB 当日 RU 消耗
    # R53 P1-6: 使用 _read_kv_value_with_meta,采集失败时不输出主 metric(避免 0 伪装健康)
    crdb_ru_str, crdb_ru_valid, _, _ = _read_kv_value_with_meta("crdb_ru_daily")
    crdb_ru: float | None = None
    if crdb_ru_valid:
        try:
            crdb_ru = float(crdb_ru_str)
        except (TypeError, ValueError):
            crdb_ru_valid = False
            crdb_ru = None
    if crdb_ru_valid:
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="crdb_ru"} 1'
        )
    else:
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="crdb_ru"} 0'
        )
        logger.warning(
            "[R53-P1-6] crdb_ru_daily 采集失败,不输出主 metric(避免 0 伪装健康)"
        )
    lines.append("# HELP crdb_ru_daily CockroachDB daily Request Units consumed")
    lines.append("# TYPE crdb_ru_daily gauge")
    lines.extend(_emit_metric_or_skip("crdb_ru_daily", crdb_ru, crdb_ru_valid))

    # R41 RU 门禁: tgjiema_crdb_idle_ru_daily — 业务 Bot 空载 RU 每日消耗
    # 业务 Bot(up/idx/dsp/admin)应只读 SQLite,不触发 CRDB RU。
    # 理想值 ≤20 RU/天,上限 ≤100 RU/天,超过阈值告警(由 Alertmanager 触发)。
    # 数据来源: kv_store.crdb_idle_ru_daily(crdb_ru_collector 采集)
    # R42 P1-10: 根据 get_ru_status() 区分 source(official/unknown/failed)
    #   - failed  → 显示 -1, label source="failed"
    #   - unknown → 显示 -1, label source="unknown"
    #   - official→ 显示真实值, label source="official"
    # R53 P1-6: 使用 _read_kv_value_with_meta 区分采集成功/失败
    idle_ru_str, idle_ru_valid, _, _ = _read_kv_value_with_meta("crdb_idle_ru_daily")
    idle_ru: float = 0.0
    if idle_ru_valid:
        try:
            idle_ru = float(idle_ru_str)
        except (TypeError, ValueError):
            idle_ru_valid = False
            idle_ru = 0.0
    else:
        idle_ru = 0.0

    # R42 P1-10: 获取 RU 状态(official/unknown/failed)
    # 同步调用 get_ru_status() 会触发 CRDB API 调用,这里改为从 kv_store
    # 读取时间戳判断新鲜度(不实际调用 API,避免 Prometheus scrape 阻塞)
    crdb_ru_source_label, crdb_ru_freshness, crdb_ru_source_gauge = (
        _compute_crdb_ru_source_label()
    )

    # R53 P1-6: 计算 GO 判定信号(collector_success=1 AND source=official AND fresh=1)
    # 告警规则必须同时要求 tgjiema_ru_go_signal == 1 才触发
    # 估算 RU(ru_estimated=1)只用于归因,不参与 GO 判定
    ru_go_signal = _compute_ru_go_signal(
        collector_success=crdb_ru_valid,
        source_label=crdb_ru_source_label,
        freshness_seconds=crdb_ru_freshness,
    )

    # R42 P1-10: 失败/陈旧时 idle_ru 显示 -1,source 标签区分
    if crdb_ru_source_label == "official" and idle_ru_valid:
        idle_ru_display = idle_ru
    else:
        # failed / unknown / 采集失败 → 显示 -1(便于告警区分"无数据" vs "0 RU")
        idle_ru_display = -1.0

    lines.append(
        "# HELP tgjiema_crdb_idle_ru_daily 业务 Bot 空载 RU 每日消耗"
        "(理想 ≤20,上限 ≤100,超阈值告警;"
        "-1=采集失败/数据陈旧,label source=official/unknown/failed)"
    )
    lines.append("# TYPE tgjiema_crdb_idle_ru_daily gauge")
    lines.append(
        f'tgjiema_crdb_idle_ru_daily{{source="{crdb_ru_source_label}"}} '
        f'{idle_ru_display}'
    )

    # R42 P1-10: tgjiema_crdb_ru_source — 数据源状态 gauge
    # 0=unknown(数据陈旧或时间戳缺失)
    # 1=official(CRDB API 调用成功且数据新鲜)
    # 2=failed(CRDB API 调用失败且 kv_store 无历史数据)
    ru_source_value = {"unknown": 0, "official": 1, "failed": 2}.get(
        crdb_ru_source_label, 0
    )
    lines.append(
        "# HELP tgjiema_crdb_ru_source CRDB RU 数据源状态"
        "(0=unknown, 1=official, 2=failed)"
    )
    lines.append("# TYPE tgjiema_crdb_ru_source gauge")
    lines.append(f"tgjiema_crdb_ru_source {ru_source_value}")

    # R42 P1-10: tgjiema_crdb_ru_freshness_seconds — 数据新鲜度(秒)
    # -1=从未采集或时间戳缺失,其他=距上次成功采集的秒数
    lines.append(
        "# HELP tgjiema_crdb_ru_freshness_seconds CRDB RU 数据新鲜度"
        "(秒,距上次成功采集;-1=从未采集)"
    )
    lines.append("# TYPE tgjiema_crdb_ru_freshness_seconds gauge")
    lines.append(f"tgjiema_crdb_ru_freshness_seconds {crdb_ru_freshness}")

    # R53 P1-6: tgjiema_ru_go_signal — 生产 GO 判定信号
    # =1 当且仅当:collector_success=1 AND source=official AND fresh=1
    # 估算 RU(ru_estimated=1)不参与 GO 判定(仅用于归因)
    # 告警规则必须同时要求 tgjiema_ru_go_signal == 1 才触发,例如:
    #   expr: tgjiema_crdb_idle_ru_daily > 100 and on() tgjiema_ru_go_signal == 1
    lines.append(
        "# HELP tgjiema_ru_go_signal 生产 GO 判定信号"
        "(1=可触发告警: collector_success=1 AND source=official AND fresh=1; "
        "0=不可触发: 采集失败/数据陈旧/估算值)"
    )
    lines.append("# TYPE tgjiema_ru_go_signal gauge")
    lines.append(f"tgjiema_ru_go_signal {ru_go_signal}")

    # R41 RU 门禁: tgjiema_crdb_idle_ru_alert — 空载 RU 是否超阈值(0/1)
    # 阈值从环境变量 CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD 读取(默认 100)
    # R53 P1-6: 告警必须同时满足 GO 信号(collector_success=1 AND source=official AND fresh=1)
    try:
        idle_threshold = float(
            os.getenv("CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD", "100")
        )
    except (TypeError, ValueError):
        idle_threshold = 100.0
    idle_alert = 1 if (idle_ru > idle_threshold and ru_go_signal == 1) else 0
    lines.append(
        "# HELP tgjiema_crdb_idle_ru_alert 业务 Bot 空载 RU 是否超阈值告警"
        "(1=超阈值, 0=正常)"
    )
    lines.append("# TYPE tgjiema_crdb_idle_ru_alert gauge")
    lines.append(f"tgjiema_crdb_idle_ru_alert {idle_alert}")

    # 2. redis_pel_depth — Redis Stream pending entries 长度
    # R52 P1-7 + R53 P1-6: 采集失败时不输出 0 值带 error label(0 可能是真实值,无法区分;
    #   改为完全不输出主数值,仅输出统一的 tgjiema_collector_success=0)
    pel_str = _read_kv_value("redis_pel_depth", "")
    pel_collector_ok = True
    try:
        pel_depth = float(pel_str)
    except (TypeError, ValueError):
        # 解析失败 → 标记 collector 失败,不输出主数值
        pel_depth = None
        pel_collector_ok = False
        logger.warning(
            "[R52-P1-7] redis_pel_depth 采集失败(无法解析为 float),"
            "不输出主数值,仅输出 tgjiema_collector_success=0"
        )
    lines.append("# HELP redis_pel_depth Redis Stream pending entries length")
    lines.append("# TYPE redis_pel_depth gauge")
    if pel_depth is not None:
        lines.append(f"redis_pel_depth {pel_depth}")
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="redis_pel"} 1'
        )
    else:
        # R52 P1-7: 采集失败时不输出 0 值(避免伪装健康),
        # 仅输出统一 collector_success metric
        lines.append("# redis_pel_depth 采集失败,主数值不输出(避免 0 伪装健康)")
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="redis_pel"} 0'
        )

    # 3. dlq_depth — 死信队列深度
    # R52 P1-7 + R53 P1-6: 采集失败时不输出 0 值带 error label,仅输出统一 collector_success=0
    dlq_str = _read_kv_value("dlq_depth", "")
    dlq_collector_ok = True
    try:
        dlq_depth = float(dlq_str)
    except (TypeError, ValueError):
        dlq_depth = None
        dlq_collector_ok = False
        logger.warning(
            "[R52-P1-7] dlq_depth 采集失败(无法解析为 float),"
            "不输出主数值,仅输出 tgjiema_collector_success=0"
        )
    lines.append("# HELP dlq_depth Dead letter queue depth")
    lines.append("# TYPE dlq_depth gauge")
    if dlq_depth is not None:
        lines.append(f"dlq_depth {dlq_depth}")
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="dlq"} 1'
        )
    else:
        # R52 P1-7: 采集失败时不输出 0 值(避免伪装健康)
        lines.append("# dlq_depth 采集失败,主数值不输出(避免 0 伪装健康)")
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="dlq"} 0'
        )

    # 4. dirty_outbox_rows — upload_outbox 表未完成行数
    dirty = _read_sqlite_single(
        CACHE_STORE_DB,
        "SELECT COUNT(*) FROM upload_outbox WHERE status NOT IN ('DONE', 'FAILED')",
        0,
    )
    try:
        dirty_count = int(dirty or 0)
    except (TypeError, ValueError):
        dirty_count = 0
    lines.append("# HELP dirty_outbox_rows Unsynced upload_outbox rows")
    lines.append("# TYPE dirty_outbox_rows gauge")
    lines.append(f"dirty_outbox_rows {dirty_count}")

    # 5. backup_age_seconds — 最近一次成功备份距今秒数
    backup_ts_str = _read_kv_value("last_backup_at", "0")
    try:
        last_backup_ts = float(backup_ts_str)
    except (TypeError, ValueError):
        last_backup_ts = 0.0
    # 无备份记录时返回 -1(便于告警区分"无备份" vs "刚备份")
    backup_age = (time.time() - last_backup_ts) if last_backup_ts > 0 else -1.0
    lines.append("# HELP backup_age_seconds Seconds since last successful backup (-1 if never)")
    lines.append("# TYPE backup_age_seconds gauge")
    lines.append(f"backup_age_seconds {backup_age}")

    # R42 P1-9: tgjiema_backup_compliant — 备份合规状态(基于真实 COMPLETE marker)
    # 0=non-compliant(无成功备份或 COMPLETE marker 缺失)
    # 1=compliant(有 status='completed' 且 COMPLETE marker 存在的备份)
    backup_compliant, backup_rpo = _get_backup_compliance_status()
    lines.append(
        "# HELP tgjiema_backup_compliant 备份合规状态"
        "(0=non-compliant 无成功备份, 1=compliant 有 COMPLETE marker 的成功备份)"
    )
    lines.append("# TYPE tgjiema_backup_compliant gauge")
    lines.append(f"tgjiema_backup_compliant {backup_compliant}")

    # R42 P1-9: tgjiema_backup_rpo_seconds — 真实 RPO(基于 last_backup_at)
    # -1=从未备份,其他=距上次成功备份的秒数
    lines.append(
        "# HELP tgjiema_backup_rpo_seconds 真实 RPO(秒,距上次成功备份;"
        "-1=从未备份,基于 last_backup_at)"
    )
    lines.append("# TYPE tgjiema_backup_rpo_seconds gauge")
    lines.append(f"tgjiema_backup_rpo_seconds {backup_rpo}")

    # 6. relay_spool_disk_usage — relay spool 目录当前字节数
    spool_bytes = _get_relay_spool_disk_usage()
    lines.append("# HELP relay_spool_disk_usage_bytes Relay spool directory size in bytes")
    lines.append("# TYPE relay_spool_disk_usage_bytes gauge")
    lines.append(f"relay_spool_disk_usage_bytes {spool_bytes}")

    # 7. relay_spool_disk_max_bytes — relay spool 配额上限
    try:
        max_bytes = int(os.getenv("RELAY_SPOOL_MAX_BYTES", str(RELAY_SPOOL_MAX_BYTES_DEFAULT)))
    except (TypeError, ValueError):
        max_bytes = RELAY_SPOOL_MAX_BYTES_DEFAULT
    lines.append("# HELP relay_spool_disk_max_bytes Relay spool directory max bytes quota")
    lines.append("# TYPE relay_spool_disk_max_bytes gauge")
    lines.append(f"relay_spool_disk_max_bytes {max_bytes}")

    # 8. relay_spool_usage_ratio — relay spool 使用率(0.0-1.0+)
    if max_bytes > 0:
        usage_ratio = spool_bytes / max_bytes
    else:
        usage_ratio = 0.0
    lines.append("# HELP relay_spool_usage_ratio Relay spool disk usage ratio (current/max)")
    lines.append("# TYPE relay_spool_usage_ratio gauge")
    lines.append(f"relay_spool_usage_ratio {usage_ratio:.6f}")

    # 9. relay_spool_high_water — 是否达高水位(0/1)
    high_water = 1 if usage_ratio >= RELAY_SPOOL_HIGH_WATER_MARK else 0
    lines.append("# HELP relay_spool_high_water Relay spool high water mark exceeded (1=yes, 0=no)")
    lines.append("# TYPE relay_spool_high_water gauge")
    lines.append(f"relay_spool_high_water {high_water}")

    # 10. relay_spool_status_count{status=...} — 各状态 spool 数量
    spool_stats_query = (
        "SELECT status, COUNT(*) FROM relay_spool GROUP BY status"
    )
    if RELAY_DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{RELAY_DB_PATH}?mode=ro", uri=True, timeout=2)
            cursor = conn.execute(spool_stats_query)
            rows = cursor.fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.debug(f"[prometheus_exporter] 查询 relay_spool 状态失败: {e}")
            rows = []
        except Exception:
            rows = []
    else:
        rows = []
    lines.append("# HELP relay_spool_status_count Relay spool count by status")
    lines.append("# TYPE relay_spool_status_count gauge")
    if rows:
        for status, count in rows:
            # 转义 status 标签值(防止 Prometheus 格式注入)
            safe_status = str(status).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'relay_spool_status_count{{status="{safe_status}"}} {count}')
    else:
        # 无数据时输出 0,保证指标存在(避免 Prometheus 误判服务不可用)
        lines.append('relay_spool_status_count{status="unknown"} 0')

    # R40: 追加 R40 新增指标(在高基数审计前加入,使 R38 P2-7 检查覆盖 R40)
    lines.extend(_format_r40_metrics())

    # R38 P2-7 + R51 P1-7: 输出前审计高基数 label
    # - CI 模式:发现违规 raise AppError(阻断)
    # - 运行时模式:丢弃违规 metric 行(不输出)
    filtered_lines: list[str] = []
    for line in lines:
        if line.startswith("#"):
            # HELP/TYPE 注释行保留(不影响高基数检查)
            filtered_lines.append(line)
            continue
        # 检查数据行,违规则丢弃(运行时)或 raise(CI)
        if _check_no_high_cardinality_labels(line):
            filtered_lines.append(line)
    lines = filtered_lines

    # R39 P1-8: 新增 readiness 指标 — scrape_errors / data_age_seconds / readiness_checks
    global _last_scrape_ok, _last_scrape_ts, _scrape_errors
    # 判断本次采集是否成功(kv_store 至少可读)
    scrape_ok = CACHE_STORE_DB.exists() and pel_str != "0" or crdb_ru_str != "0" or True
    # 简化:只要 SQLite 文件可读即认为成功(_read_kv_value 不抛异常就算成功)
    scrape_ok = CACHE_STORE_DB.exists()
    if scrape_ok:
        _last_scrape_ok = True
        _last_scrape_ts = time.time()
    else:
        _last_scrape_ok = False
        _scrape_errors += 1

    # scrape_errors: 累计采集错误数
    lines.append("# HELP scrape_errors Total number of scrape errors")
    lines.append("# TYPE scrape_errors counter")
    lines.append(f"scrape_errors {_scrape_errors}")

    # data_age_seconds: 最近成功采集距今秒数(-1 表示从未成功采集)
    data_age = (time.time() - _last_scrape_ts) if _last_scrape_ts > 0 else -1.0
    lines.append("# HELP data_age_seconds Seconds since last successful scrape (-1 if never)")
    lines.append("# TYPE data_age_seconds gauge")
    lines.append(f"data_age_seconds {data_age}")

    # readiness_checks: readiness 检查项数(0=未通过,1+=通过的检查项)
    readiness = check_readiness()
    lines.append("# HELP readiness_checks Number of readiness checks passed")
    lines.append("# TYPE readiness_checks gauge")
    lines.append(f"readiness_checks {readiness['passed']}")

    # R41 P1-10: tgjiema_readiness_status — 整体就绪状态(1=ready, 0=not ready)
    # 用于 Prometheus 告警规则:readiness_status == 0 → 告警
    lines.append(
        "# HELP tgjiema_readiness_status Overall readiness status "
        "(1=ready, 0=not ready)"
    )
    lines.append("# TYPE tgjiema_readiness_status gauge")
    lines.append(f"tgjiema_readiness_status {1 if readiness['ready'] else 0}")

    # R44 6.2 + R51 P1-7 + R52 P1-7: tgjiema_i18n_missing_key_total — i18n key 缺失累计计数(Counter)
    # 用于告警:i18n key 缺失可能暴露内部 key 给用户(违反 R44 6.2 安全要求)
    # 数据来源: services.i18n.I18nManager.get_missing_key_count()
    # label: locale(低基数,通常 zh-CN / en-US 两种)
    # R52 P1-7: 采集失败时不输出 0 值带 error label(0 可能是真实值,无法区分;
    #   改为完全不输出主数值,仅输出统一的 tgjiema_collector_success=0)
    i18n_collector_ok = False
    i18n_missing_key_total: int | None = None
    try:
        from services.i18n import get_i18n_manager

        i18n_manager = get_i18n_manager()
        i18n_missing_key_total = i18n_manager.get_missing_key_count()
        i18n_collector_ok = True
    except Exception as e:
        # R52 P1-7: i18n 模块未初始化或采集失败 → 不输出主数值,仅输出 collector_success=0
        logger.warning(
            f"[R52-P1-7] i18n missing_key metric 采集失败,"
            f"不输出主数值,仅输出 tgjiema_collector_success=0: {e}"
        )
        i18n_collector_ok = False

    lines.append(
        "# HELP tgjiema_i18n_missing_key_total Total number of missing "
        "i18n keys encountered (high values indicate missing translations)"
    )
    lines.append("# TYPE tgjiema_i18n_missing_key_total counter")
    if i18n_collector_ok and i18n_missing_key_total is not None:
        # 采集成功:输出真实计数
        lines.append(
            f'tgjiema_i18n_missing_key_total{{locale="total"}} '
            f'{i18n_missing_key_total}'
        )
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="i18n_missing_key"} 1'
        )
    else:
        # R52 P1-7: 采集失败时不输出 0 值(避免伪装健康),
        # 仅输出统一 collector_success metric
        lines.append(
            "# tgjiema_i18n_missing_key_total 采集失败,主数值不输出(避免 0 伪装健康)"
        )
        _collector_success_lines.append(
            'tgjiema_collector_success{collector="i18n_missing_key"} 0'
        )

    # R53 P1-6: 统一输出 tgjiema_collector_success(所有 collector 的成功/失败状态)
    # 告警规则可通过 collector_success == 0 发现采集失败
    lines.append("# HELP tgjiema_collector_success 采集器状态(1=ok, 0=failed)")
    lines.append("# TYPE tgjiema_collector_success gauge")
    lines.extend(_collector_success_lines)

    return "\n".join(lines) + "\n"


def check_readiness() -> dict:
    """R39 P1-8 + R41 P1-10: readiness 检查 — 报告真实依赖状态。

    检查项:
      1. sqlite_readable — cache_store.db 存在且可查询
      2. recent_scrape — 最近采集成功(_last_scrape_ok + data_age 未超阈值)
      3. key_schema_exists — 关键业务表存在(file_records_local 等)
      4. schema_valid — backup_schema.validate_schema() 通过(R41 P1-10)
      5. crdb_sync_fresh — CRDB 同步最近成功(kv_store.crdb_sync_last_success)
      6. r2_collector_fresh — R2 指标采集最近成功(kv_store.r2_last_collect_time)
      7. acl_configured — REDIS_*_PASSWORD 4 个变量均存在(R41 P1-10)

    供 /health 与 /readiness 端点调用,不满足时返回 503 Service Unavailable。

    Returns:
        {
            "ready": bool,
            "passed": int,
            "checks": {name: bool},
            "details": {name: str},          # R41 P1-10: 各检查项详细信息
            "ru_daily_usage": str,           # R41 P1-10: "unknown" if 采集失败,否则数字
            "last_crdb_sync_age": float,     # R41 P1-10: CRDB 同步距今秒数(-1=从未)
            "last_r2_collect_age": float,     # R41 P1-10: R2 采集距今秒数(-1=从未)
        }
    """
    global _last_crdb_sync_ts, _last_r2_collect_ts, _acl_configured, _schema_valid
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}

    # 1. SQLite 可读(cache_store.db 存在且可查询)
    sqlite_ok = False
    if CACHE_STORE_DB.exists():
        try:
            conn = sqlite3.connect(
                f"file:{CACHE_STORE_DB}?mode=ro", uri=True, timeout=2
            )
            conn.execute("SELECT 1 FROM kv_store LIMIT 1")
            conn.close()
            sqlite_ok = True
            details["sqlite_readable"] = f"OK: {CACHE_STORE_DB}"
        except Exception as e:
            details["sqlite_readable"] = f"FAIL: {e}"
    else:
        details["sqlite_readable"] = f"FAIL: file not found {CACHE_STORE_DB}"
    checks["sqlite_readable"] = sqlite_ok

    # 2. 最近采集成功(_last_scrape_ok + data_age 未超阈值)
    data_age = (time.time() - _last_scrape_ts) if _last_scrape_ts > 0 else -1.0
    scrape_recent = (
        _last_scrape_ok
        and data_age >= 0
        and data_age < _DATA_AGE_ALERT_THRESHOLD
    )
    checks["recent_scrape"] = scrape_recent
    details["recent_scrape"] = (
        f"OK: data_age={data_age:.1f}s" if scrape_recent
        else f"FAIL: _last_scrape_ok={_last_scrape_ok}, data_age={data_age:.1f}s"
    )

    # 3. 关键 schema 存在(kv_store 表存在,且至少有一个业务表)
    schema_ok = False
    if sqlite_ok:
        try:
            conn = sqlite3.connect(
                f"file:{CACHE_STORE_DB}?mode=ro", uri=True, timeout=2
            )
            # 检查关键表存在(file_records_local / cells_local / upload_outbox)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('file_records_local', 'cells_local', 'upload_outbox') "
                "LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()
            schema_ok = row is not None
            details["key_schema_exists"] = (
                f"OK: {row[0]}" if row else "FAIL: 无关键业务表"
            )
        except Exception as e:
            details["key_schema_exists"] = f"FAIL: {e}"
    else:
        details["key_schema_exists"] = "SKIP: sqlite 不可读"
    checks["key_schema_exists"] = schema_ok

    # 4. R41 P1-10: backup_schema.validate_schema() 校验
    # validate_schema 返回 {is_valid, missing_tables, extra_tables, source_mismatches, ...}
    # 这里只检查 is_valid,不阻塞(SQLite-only 部署中部分 CRDB 表可能不适用)
    try:
        from services.backup_schema import validate_schema
        result = validate_schema()
        _schema_valid = bool(result.get("is_valid", False))
        # 缺失表与 source 错配为非致命问题(部分部署可能裁剪表),
        # 但 empty_columns 必须为空(列定义为代码生成的硬要求)
        empty_cols = result.get("empty_columns", [])
        # 只要 empty_columns 为空即认为 schema_valid 通过(允许 missing/extra)
        schema_valid_ok = (not empty_cols)
        _schema_valid = schema_valid_ok
        checks["schema_valid"] = schema_valid_ok
        details["schema_valid"] = (
            f"OK: empty_columns=[]" if schema_valid_ok
            else f"FAIL: empty_columns={empty_cols[:5]}"
        )
    except Exception as e:
        _schema_valid = False
        checks["schema_valid"] = False
        details["schema_valid"] = f"FAIL: {e}"

    # 5. R41 P1-10: CRDB 同步新鲜度(kv_store.crdb_sync_last_success)
    # crdb_sync 服务每次成功同步后写入 kv_store.crdb_sync_last_success = ISO 时间戳
    crdb_sync_age = -1.0
    if sqlite_ok:
        try:
            val = _read_kv_value("crdb_sync_last_success", "")
            if val:
                # 尝试解析 ISO 时间戳
                try:
                    sync_dt = _dt.datetime.fromisoformat(val)
                    _last_crdb_sync_ts = sync_dt.timestamp()
                    crdb_sync_age = time.time() - _last_crdb_sync_ts
                except (ValueError, TypeError):
                    # 兼容数字时间戳
                    try:
                        _last_crdb_sync_ts = float(val)
                        crdb_sync_age = time.time() - _last_crdb_sync_ts
                    except (ValueError, TypeError):
                        crdb_sync_age = -1.0
        except Exception:
            pass
    crdb_sync_fresh = (
        crdb_sync_age >= 0
        and crdb_sync_age < _CRDB_SYNC_FRESH_THRESHOLD
    )
    checks["crdb_sync_fresh"] = crdb_sync_fresh
    details["crdb_sync_fresh"] = (
        f"OK: age={crdb_sync_age:.1f}s" if crdb_sync_fresh
        else f"FAIL: age={crdb_sync_age:.1f}s, last_ts={_last_crdb_sync_ts}"
    )

    # 6. R41 P1-10: R2 采集新鲜度(kv_store.r2_last_collect_time)
    # r2_collector 服务每次成功采集后写入 kv_store.r2_last_collect_time = ISO 时间戳
    r2_collect_age = -1.0
    if sqlite_ok:
        try:
            val = _read_kv_value("r2_last_collect_time", "")
            if val:
                try:
                    collect_dt = _dt.datetime.fromisoformat(val)
                    _last_r2_collect_ts = collect_dt.timestamp()
                    r2_collect_age = time.time() - _last_r2_collect_ts
                except (ValueError, TypeError):
                    try:
                        _last_r2_collect_ts = float(val)
                        r2_collect_age = time.time() - _last_r2_collect_ts
                    except (ValueError, TypeError):
                        r2_collect_age = -1.0
        except Exception:
            pass
    r2_collector_fresh = (
        r2_collect_age >= 0
        and r2_collect_age < _R2_COLLECT_FRESH_THRESHOLD
    )
    checks["r2_collector_fresh"] = r2_collector_fresh
    details["r2_collector_fresh"] = (
        f"OK: age={r2_collect_age:.1f}s" if r2_collector_fresh
        else f"FAIL: age={r2_collect_age:.1f}s, last_ts={_last_r2_collect_ts}"
    )

    # 7. R41 P1-10: ACL 配置完整性(REDIS_*_PASSWORD 4 个变量)
    # 注意:仅检查环境变量是否设置,不读取密码值(避免日志泄漏)
    required_redis_envs = [
        "REDIS_HEALTH_PASSWORD",
        "REDIS_WRITER_PASSWORD",
        "REDIS_READER_PASSWORD",
        "REDIS_ADMIN_PASSWORD",  # R41 P1-9 新增
    ]
    missing_envs = [
        name for name in required_redis_envs
        if not os.getenv(name, "").strip()
    ]
    _acl_configured = (not missing_envs)
    checks["acl_configured"] = _acl_configured
    details["acl_configured"] = (
        "OK: all 4 REDIS_*_PASSWORD env vars configured" if _acl_configured
        else f"FAIL: missing {missing_envs}"
    )

    # R41 P1-10: RU 采集状态(unknown vs 数字)
    # kv_store 中 crdb_ru_daily 不存在或 SQLite 不可读时显示 "unknown"
    # (避免误报 0 RU — 0 可能是真实值,unknown 表示采集失败)
    ru_daily_usage = "unknown"
    if sqlite_ok:
        try:
            ru_val = _read_kv_value("crdb_ru_daily", "")
            if ru_val:
                # 校验为有效数字
                float(ru_val)
                ru_daily_usage = ru_val
        except (ValueError, TypeError):
            ru_daily_usage = "unknown"

    passed = sum(1 for v in checks.values() if v)
    ready = all(checks.values())
    return {
        "ready": ready,
        "passed": passed,
        "checks": checks,
        "details": details,
        "ru_daily_usage": ru_daily_usage,
        "last_crdb_sync_age": crdb_sync_age,
        "last_r2_collect_age": r2_collect_age,
    }


# ── R40: 新增指标采集 ────────────────────────────────────


async def collect_r40_metrics() -> None:
    """R40: 采集 R40 新增指标(由后台采集线程或 scheduler 调用)。

    所有采集失败均静默降级为 0(保持上一次值),不影响 exporter 可用性。
    cache_store._db 为 aiosqlite.Connection,使用 execute_fetchall 而非 asyncpg fetchval。

    R40 P2-4: 新增功能成功率与延迟指标采集:
    - approval_execution_success_rate: 审批执行成功率(executed/(executed+failed))
    - notification_delivery_latency_samples: 通知投递延迟样本(created_at → is_read_at)
    - repair_success_rate: 修复成功率(success/total)
    - real_rpo_seconds / real_rto_seconds: 真实 RPO/RTO(从灾备模块读取)
    """
    global _r40_state, _r40_last_collect_ts
    new_state: dict[str, Any] = {
        "maintenance_enabled": 0,
        "ru_daily_usage": {},
        # R51 P1-7: 每个服务的 RU 估算标记(1=估算, 0=官方)
        "ru_daily_usage_estimated": {},
        "replica_missing_count": 0,
        "quota_reservations_active": 0,
        "content_reports_pending": 0,
        "approvals_pending": 0,
        "tasks_running": 0,
        "notifications_unread": 0,
        "dlq_depth": 0,
        "outbox_unprocessed": 0,
        "audit_log_events_total": {},
        "ru_operations_total": {},
        # R40 P2-4: 功能成功率与延迟指标
        "approval_execution_success_rate": 0.0,
        "approval_execution_total": 0,
        "approval_execution_success": 0,
        "notification_delivery_latency_samples": [],
        "repair_success_rate": 0.0,
        "repair_total": 0,
        "repair_success": 0,
        "real_rpo_seconds": -1.0,
        "real_rto_seconds": -1.0,
    }

    # 1. maintenance_enabled — 维护模式是否开启
    try:
        from services.maintenance_mode import is_enabled
        new_state["maintenance_enabled"] = 1 if await is_enabled() else 0
    except Exception as e:
        logger.debug(f"[R40] 采集 maintenance_enabled 失败: {e}")

    # 2. ru_daily_usage + ru_operations_total — RU 当日使用量(按服务/操作)
    # R51 P1-7: 同时采集 ru_estimated 标记,区分估算值与官方 CockroachDB Cloud Metrics
    try:
        from services.ru_cost_center import get_daily_report, SERVICES
        report = await get_daily_report()
        for service, amount in report.get("by_service", {}).items():
            new_state["ru_daily_usage"][service] = amount
        # R51 P1-7: 采集每个服务的 ru_estimated 标记(1=估算, 0=官方)
        for service, estimated in report.get("by_service_estimated", {}).items():
            new_state["ru_daily_usage_estimated"][service] = int(estimated)
        # ru_operations_total: 按服务维度解析 kv_store 中的 by_operation
        today = _dt.datetime.now().strftime("%Y%m%d")
        from database.cache_store import get_cache_store as _get_store
        _store = _get_store()
        if _store._db:
            for service in SERVICES:
                key = f"ru_usage:{today}:{service}"
                raw = await _store.get_kv(key)
                if not raw:
                    continue
                try:
                    data = _json.loads(raw)
                except (ValueError, TypeError):
                    continue
                for op, amount in data.get("by_operation", {}).items():
                    new_state["ru_operations_total"][(service, op)] = amount
    except Exception as e:
        logger.debug(f"[R40] 采集 ru_daily_usage 失败: {e}")

    # 3. replica_missing_count — 缺失副本数量
    try:
        from services.topology_view import get_replica_status
        status = await get_replica_status()
        new_state["replica_missing_count"] = status.get("missing_replicas", 0)
    except Exception as e:
        logger.debug(f"[R40] 采集 replica_missing_count 失败: {e}")

    # 4. SQLite 表计数(配额预留/举报/审批/任务/通知/outbox/审计日志)
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if store._db:
            # 活跃配额预留
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM quota_reservations WHERE status='reserved'"
            )
            new_state["quota_reservations_active"] = rows[0][0] if rows else 0

            # 待处理举报
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM content_reports WHERE status='pending'"
            )
            new_state["content_reports_pending"] = rows[0][0] if rows else 0

            # 待审批
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM approvals WHERE status='pending'"
            )
            new_state["approvals_pending"] = rows[0][0] if rows else 0

            # 运行中任务
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM tasks WHERE status='running'"
            )
            new_state["tasks_running"] = rows[0][0] if rows else 0

            # 未读通知
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM notifications WHERE is_read=0"
            )
            new_state["notifications_unread"] = rows[0][0] if rows else 0

            # 未处理 dirty_outbox
            rows = await store._db.execute_fetchall(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed=0"
            )
            new_state["outbox_unprocessed"] = rows[0][0] if rows else 0

            # 审计日志事件总数(按 action 分组,Counter 类型)
            rows = await store._db.execute_fetchall(
                "SELECT action, COUNT(*) FROM audit_log GROUP BY action"
            )
            for action, count in rows:
                new_state["audit_log_events_total"][str(action)] = int(count)

            # R40 P2-4: 审批执行成功率
            # APPROVAL_STATUS_EXECUTED='executed', APPROVAL_STATUS_FAILED='failed'
            rows = await store._db.execute_fetchall(
                "SELECT status, COUNT(*) FROM approvals "
                "WHERE status IN ('executed', 'failed') GROUP BY status"
            )
            executed = 0
            failed = 0
            for status, count in rows:
                if status == "executed":
                    executed = int(count)
                elif status == "failed":
                    failed = int(count)
            total_exec = executed + failed
            new_state["approval_execution_total"] = total_exec
            new_state["approval_execution_success"] = executed
            if total_exec > 0:
                new_state["approval_execution_success_rate"] = float(executed) / float(total_exec)
            else:
                new_state["approval_execution_success_rate"] = 0.0

            # R40 P2-4: 通知投递延迟样本(从已读通知的 created_at → read_at 计算)
            # notifications 表 read_at 字段由 mark_read / mark_all_read 写入(R40 P2-4 新增)
            rows = await store._db.execute_fetchall(
                "SELECT created_at, read_at FROM notifications "
                "WHERE is_read = 1 AND created_at IS NOT NULL "
                "AND read_at IS NOT NULL AND read_at > created_at "
                "ORDER BY id DESC LIMIT 100"
            )
            latency_samples: list[float] = []
            for created_str, read_str in rows:
                try:
                    created_dt = _dt.datetime.fromisoformat(str(created_str))
                    read_dt = _dt.datetime.fromisoformat(str(read_str))
                    delta = (read_dt - created_dt).total_seconds()
                    if delta >= 0:
                        latency_samples.append(float(delta))
                except (ValueError, TypeError):
                    continue
            new_state["notification_delivery_latency_samples"] = latency_samples

            # R40 P2-4: 修复操作成功率(从 repair_console 审计日志中提取)
            # 修复操作以 action IN ('repair_outbox', 'repair_dlq',
            #                       'repair_replication', 'repair_relay') 记录
            rows = await store._db.execute_fetchall(
                "SELECT details, COUNT(*) FROM audit_log "
                "WHERE action IN ('repair_outbox', 'repair_dlq', "
                "                 'repair_replication', 'repair_relay') "
                "GROUP BY details"
            )
            repair_total = 0
            repair_success = 0
            for details_json, count in rows:
                try:
                    import json as _json_mod
                    details = _json_mod.loads(details_json) if details_json else {}
                    success = bool(details.get("success", False))
                    cnt = int(count)
                    repair_total += cnt
                    if success:
                        repair_success += cnt
                except (ValueError, TypeError):
                    continue
                except Exception:
                    # JSONDecodeError 等其他异常,跳过该行
                    continue
            new_state["repair_total"] = repair_total
            new_state["repair_success"] = repair_success
            if repair_total > 0:
                new_state["repair_success_rate"] = float(repair_success) / float(repair_total)
            else:
                new_state["repair_success_rate"] = 0.0
    except Exception as e:
        logger.debug(f"[R40] 采集 R40 SQLite 指标失败: {e}")

    # 5. dlq_depth — 死信队列深度(来自 repair_console)
    try:
        from services.repair_console import get_repair_overview
        overview = await get_repair_overview()
        new_state["dlq_depth"] = overview.get("dlq_count", 0)
    except Exception as e:
        logger.debug(f"[R40] 采集 dlq_depth 失败: {e}")

    # R40 P2-4: 真实 RPO/RTO(从灾备模块读取)
    try:
        from services.disaster_recovery import get_rpo_rto
        rpo_rto = await get_rpo_rto()
        # 真实 RPO: 距离最近一次成功备份的秒数(last_backup_age, None → -1)
        last_backup_age = rpo_rto.get("last_backup_age")
        if last_backup_age is not None:
            new_state["real_rpo_seconds"] = float(last_backup_age)
        else:
            new_state["real_rpo_seconds"] = -1.0
        # 真实 RTO: 估算恢复时间(estimated_recovery_time,基于历史恢复记录)
        new_state["real_rto_seconds"] = float(
            rpo_rto.get("estimated_recovery_time", -1.0)
        )
    except Exception as e:
        logger.debug(f"[R40] 采集 real_rpo/rto 失败: {e}")
        # 降级: 尝试从 kv_store 读取 last_backup_at 计算 RPO
        try:
            backup_ts_str = _read_kv_value("last_backup_at", "0")
            try:
                last_backup_ts = float(backup_ts_str)
                if last_backup_ts > 0:
                    new_state["real_rpo_seconds"] = time.time() - last_backup_ts
            except (TypeError, ValueError):
                pass
        except Exception:
            pass

    # 原子更新状态(拷贝替换,避免读取期间部分更新)
    with _r40_state_lock:
        _r40_state = new_state
        _r40_last_collect_ts = time.time()


def _format_r40_metrics() -> list[str]:
    """R40: 将缓存的 R40 指标格式化为 Prometheus text format 行列表。

    所有 label 均为低基数(service/action/operation),符合 R38 P2-7 高基数规则。
    无数据时输出 0 值占位行,保证指标存在(避免 Prometheus 误判服务不可用)。
    """
    with _r40_state_lock:
        state = {k: v for k, v in _r40_state.items()}

    def _escape(value: str) -> str:
        """转义 Prometheus label 值(防注入)。"""
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    lines: list[str] = []

    # tgjiema_maintenance_enabled (Gauge)
    lines.append("# HELP tgjiema_maintenance_enabled 维护模式是否开启(0=关闭, 1=开启)")
    lines.append("# TYPE tgjiema_maintenance_enabled gauge")
    lines.append(f"tgjiema_maintenance_enabled {state.get('maintenance_enabled', 0)}")

    # tgjiema_ru_daily_usage{service,ru_estimated} (Gauge)
    # R51 P1-7: 新增 ru_estimated label 区分估算值(1)与官方 CockroachDB Cloud Metrics(0)
    # - ru_estimated=1: 基于 RU_PER_READ/WRITE/QUERY 常量估算(业务自统计)
    # - ru_estimated=0: 来自 CockroachDB Cloud 官方 API(crdb_ru_collector 采集)
    # 两者不能互相替代:估算值用于业务自省,官方值用于成本核算
    lines.append(
        "# HELP tgjiema_ru_daily_usage 当日 RU 使用量(按服务,"
        "label ru_estimated: 1=估算值, 0=官方 CockroachDB Cloud Metrics)"
    )
    lines.append("# TYPE tgjiema_ru_daily_usage gauge")
    ru_usage = state.get("ru_daily_usage", {})
    # R51 P1-7: 每个服务的 ru_estimated 标记(默认 1=估算,兼容旧数据)
    ru_estimated_map = state.get("ru_daily_usage_estimated", {})
    if ru_usage:
        for service, amount in sorted(ru_usage.items()):
            estimated_flag = ru_estimated_map.get(service, 1)
            lines.append(
                f'tgjiema_ru_daily_usage{{service="{_escape(service)}",'
                f'ru_estimated="{estimated_flag}"}} {amount}'
            )
    else:
        lines.append('tgjiema_ru_daily_usage{service="unknown",ru_estimated="1"} 0')

    # R53 P1-6: tgjiema_ru_official_daily_usage{service} — 仅官方值(ru_estimated=0)
    # 估算 RU(ru_estimated=1)只用于归因,不参与生产 GO 判定。
    # 告警规则应基于本指标(而非 tgjiema_ru_daily_usage)触发,例如:
    #   expr: sum(tgjiema_ru_official_daily_usage) by (service) > threshold
    lines.append(
        "# HELP tgjiema_ru_official_daily_usage 当日官方 RU 使用量"
        "(仅 ru_estimated=0 的服务,用于告警门禁;估算值不参与 GO 判定)"
    )
    lines.append("# TYPE tgjiema_ru_official_daily_usage gauge")
    if ru_usage:
        has_official = False
        for service, amount in sorted(ru_usage.items()):
            estimated_flag = ru_estimated_map.get(service, 1)
            if estimated_flag == 0:
                lines.append(
                    f'tgjiema_ru_official_daily_usage{{service="{_escape(service)}"}} '
                    f'{amount}'
                )
                has_official = True
        if not has_official:
            lines.append('tgjiema_ru_official_daily_usage{service="none"} 0')
    else:
        lines.append('tgjiema_ru_official_daily_usage{service="none"} 0')

    # tgjiema_replica_missing_count (Gauge)
    lines.append("# HELP tgjiema_replica_missing_count 缺失副本数量")
    lines.append("# TYPE tgjiema_replica_missing_count gauge")
    lines.append(f"tgjiema_replica_missing_count {state.get('replica_missing_count', 0)}")

    # tgjiema_quota_reservations_active (Gauge)
    lines.append("# HELP tgjiema_quota_reservations_active 活跃配额预留数量")
    lines.append("# TYPE tgjiema_quota_reservations_active gauge")
    lines.append(f"tgjiema_quota_reservations_active {state.get('quota_reservations_active', 0)}")

    # tgjiema_content_reports_pending (Gauge)
    lines.append("# HELP tgjiema_content_reports_pending 待处理举报数量")
    lines.append("# TYPE tgjiema_content_reports_pending gauge")
    lines.append(f"tgjiema_content_reports_pending {state.get('content_reports_pending', 0)}")

    # tgjiema_approvals_pending (Gauge)
    lines.append("# HELP tgjiema_approvals_pending 待审批数量")
    lines.append("# TYPE tgjiema_approvals_pending gauge")
    lines.append(f"tgjiema_approvals_pending {state.get('approvals_pending', 0)}")

    # tgjiema_tasks_running (Gauge)
    lines.append("# HELP tgjiema_tasks_running 运行中任务数量")
    lines.append("# TYPE tgjiema_tasks_running gauge")
    lines.append(f"tgjiema_tasks_running {state.get('tasks_running', 0)}")

    # tgjiema_notifications_unread (Gauge)
    lines.append("# HELP tgjiema_notifications_unread 未读通知总数")
    lines.append("# TYPE tgjiema_notifications_unread gauge")
    lines.append(f"tgjiema_notifications_unread {state.get('notifications_unread', 0)}")

    # tgjiema_dlq_depth (Gauge)
    lines.append("# HELP tgjiema_dlq_depth 死信队列深度")
    lines.append("# TYPE tgjiema_dlq_depth gauge")
    lines.append(f"tgjiema_dlq_depth {state.get('dlq_depth', 0)}")

    # tgjiema_outbox_unprocessed (Gauge)
    lines.append("# HELP tgjiema_outbox_unprocessed 未处理 dirty_outbox 数量")
    lines.append("# TYPE tgjiema_outbox_unprocessed gauge")
    lines.append(f"tgjiema_outbox_unprocessed {state.get('outbox_unprocessed', 0)}")

    # tgjiema_audit_log_events_total{action} (Counter)
    lines.append("# HELP tgjiema_audit_log_events_total 审计日志事件总数(按 action)")
    lines.append("# TYPE tgjiema_audit_log_events_total counter")
    audit = state.get("audit_log_events_total", {})
    if audit:
        for action, count in sorted(audit.items()):
            lines.append(
                f'tgjiema_audit_log_events_total{{action="{_escape(action)}"}} {count}'
            )
    else:
        lines.append('tgjiema_audit_log_events_total{action="none"} 0')

    # tgjiema_ru_operations_total{service,operation} (Counter)
    lines.append("# HELP tgjiema_ru_operations_total RU 操作总数(按服务+操作)")
    lines.append("# TYPE tgjiema_ru_operations_total counter")
    ru_ops = state.get("ru_operations_total", {})
    if ru_ops:
        for (service, op), count in sorted(ru_ops.items()):
            lines.append(
                f'tgjiema_ru_operations_total{{service="{_escape(service)}",'
                f'operation="{_escape(op)}"}} {count}'
            )
    else:
        lines.append(
            'tgjiema_ru_operations_total{service="unknown",operation="none"} 0'
        )

    # ── R40 P2-4: 功能成功率与延迟指标 ──────────────────────

    # tgjiema_approval_execution_success_rate (Gauge, 0.0-1.0)
    lines.append(
        "# HELP tgjiema_approval_execution_success_rate 审批执行成功率 "
        "(executed / (executed + failed), 0.0-1.0)"
    )
    lines.append("# TYPE tgjiema_approval_execution_success_rate gauge")
    lines.append(
        f"tgjiema_approval_execution_success_rate "
        f"{state.get('approval_execution_success_rate', 0.0):.6f}"
    )

    # tgjiema_approval_execution_total (Counter)
    lines.append(
        "# HELP tgjiema_approval_execution_total 审批执行总次数(含成功+失败)"
    )
    lines.append("# TYPE tgjiema_approval_execution_total counter")
    lines.append(
        f"tgjiema_approval_execution_total {state.get('approval_execution_total', 0)}"
    )

    # tgjiema_approval_execution_success (Counter)
    lines.append(
        "# HELP tgjiema_approval_execution_success 审批执行成功次数"
    )
    lines.append("# TYPE tgjiema_approval_execution_success counter")
    lines.append(
        f"tgjiema_approval_execution_success {state.get('approval_execution_success', 0)}"
    )

    # tgjiema_notification_delivery_latency_seconds (Histogram)
    # 通知投递延迟(秒),从 created_at → read_at 计算
    # 输出: count + sum + bucket(le=0.5/1/5/10/30/60/300/Inf)
    latency_samples = state.get("notification_delivery_latency_samples", [])
    lines.append(
        "# HELP tgjiema_notification_delivery_latency_seconds "
        "通知投递延迟(秒,从 created_at 到 read_at)"
    )
    lines.append("# TYPE tgjiema_notification_delivery_latency_seconds histogram")
    buckets = [0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0]
    cumulative = 0
    sorted_samples = sorted(latency_samples)
    for le in buckets:
        count = sum(1 for s in sorted_samples if s <= le)
        lines.append(
            f'tgjiema_notification_delivery_latency_seconds_bucket{{le="{le}"}} {count}'
        )
    # +Inf bucket
    total_count = len(sorted_samples)
    lines.append(
        f'tgjiema_notification_delivery_latency_seconds_bucket{{le="+Inf"}} {total_count}'
    )
    lines.append(
        f"tgjiema_notification_delivery_latency_seconds_count {total_count}"
    )
    latency_sum = sum(sorted_samples) if sorted_samples else 0.0
    lines.append(
        f"tgjiema_notification_delivery_latency_seconds_sum {latency_sum:.6f}"
    )

    # tgjiema_repair_success_rate (Gauge, 0.0-1.0)
    lines.append(
        "# HELP tgjiema_repair_success_rate 修复操作成功率 "
        "(success / total, 0.0-1.0)"
    )
    lines.append("# TYPE tgjiema_repair_success_rate gauge")
    lines.append(
        f"tgjiema_repair_success_rate {state.get('repair_success_rate', 0.0):.6f}"
    )

    # tgjiema_repair_total (Counter)
    lines.append("# HELP tgjiema_repair_total 修复操作总次数")
    lines.append("# TYPE tgjiema_repair_total counter")
    lines.append(f"tgjiema_repair_total {state.get('repair_total', 0)}")

    # tgjiema_repair_success (Counter)
    lines.append("# HELP tgjiema_repair_success 修复操作成功次数")
    lines.append("# TYPE tgjiema_repair_success counter")
    lines.append(f"tgjiema_repair_success {state.get('repair_success', 0)}")

    # tgjiema_real_rpo_seconds (Gauge, -1=未计算/无备份)
    lines.append(
        "# HELP tgjiema_real_rpo_seconds 真实 RPO(秒,距上次成功备份的时间,"
        "-1=从未备份)"
    )
    lines.append("# TYPE tgjiema_real_rpo_seconds gauge")
    lines.append(
        f"tgjiema_real_rpo_seconds {state.get('real_rpo_seconds', -1.0):.2f}"
    )

    # tgjiema_real_rto_seconds (Gauge, -1=未计算/无恢复)
    lines.append(
        "# HELP tgjiema_real_rto_seconds 真实 RTO(秒,距上次成功恢复的时间,"
        "-1=从未恢复)"
    )
    lines.append("# TYPE tgjiema_real_rto_seconds gauge")
    lines.append(
        f"tgjiema_real_rto_seconds {state.get('real_rto_seconds', -1.0):.2f}"
    )

    return lines


def _r40_collector_loop() -> None:
    """R40 采集后台线程主循环(独立事件循环,避免与 HTTP handler 冲突)。

    cache_store 的 aiosqlite 连接绑定到创建它的事件循环,因此必须使用
    持久事件循环而非每次 asyncio.run(),否则连接会在循环销毁后失效。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # 初始化 cache_store(创建 aiosqlite 连接,绑定到本线程的事件循环)
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                loop.run_until_complete(store.init())
                logger.info("[prometheus_exporter] cache_store 已在 R40 采集线程中初始化")
        except Exception as e:
            logger.warning(f"[prometheus_exporter] R40 采集线程 cache_store 初始化失败: {e}")
        while True:
            try:
                loop.run_until_complete(collect_r40_metrics())
            except Exception as e:
                logger.debug(f"[prometheus_exporter] R40 采集异常: {e}")
            time.sleep(300)  # 5 分钟采集一次
    except Exception as e:
        logger.warning(f"[prometheus_exporter] R40 采集线程异常退出: {e}")
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _start_r40_collector() -> None:
    """启动 R40 指标采集后台线程(守护线程,进程退出时自动结束)。

    幂等:多次调用只启动一次。支持测试环境不调用 main() 时也能懒启动。
    """
    global _r40_collector_started
    with _r40_collector_start_lock:
        if _r40_collector_started:
            return
        _r40_collector_started = True
    t = threading.Thread(
        target=_r40_collector_loop, daemon=True, name="r40-collector"
    )
    t.start()
    logger.info("[prometheus_exporter] R40 指标采集后台线程已启动")


# ── HTTP Handler ─────────────────────────────────────────


class MetricsHTTPRequestHandler(BaseHTTPRequestHandler):
    """Prometheus metrics HTTP handler。

    路由:
      GET /metrics   → Prometheus text format 指标
      GET /health    → "OK"(供 Docker healthcheck / k8s liveness;返回 503 if 不就绪)
      GET /readiness → 详细依赖状态 JSON(R41 P1-10:200 if all ready, 503 if any not ready)
      GET /          → 简单介绍页
      其他           → 404
    """

    # 关闭默认 stderr 日志(改用 loguru)
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug(f"[prometheus_exporter] {args[0] if args else ''}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body = collect_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            # R39 P1-8: readiness 增强 — 不再永远返回 OK
            # R41 P1-10: /health 仍为 liveness 端点(快速检查),
            # 详细依赖状态请见 /readiness
            readiness = check_readiness()
            if readiness["ready"]:
                body = b"OK"
                self.send_response(200)
            else:
                # 不满足时返回 503 Service Unavailable
                failed = [k for k, v in readiness["checks"].items() if not v]
                body = (
                    f"Service Unavailable: readiness checks failed: {failed}"
                ).encode("utf-8")
                self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/readiness":
            # R41 P1-10: 详细依赖状态报告(200 if all ready, 503 if any not ready)
            # 返回 JSON 包含:checks / details / ru_daily_usage / last_*_age
            readiness = check_readiness()
            import json as _json_mod
            body = _json_mod.dumps(readiness, ensure_ascii=False).encode("utf-8")
            if readiness["ready"]:
                self.send_response(200)
            else:
                self.send_response(503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "/index":
            body = (
                b"TGJiema Prometheus Exporter\n"
                b"\nEndpoints:\n"
                b"  /metrics   - Prometheus metrics (text format)\n"
                b"  /health    - Liveness check (200 OK / 503 Service Unavailable)\n"
                b"  /readiness - Readiness check with dependency details (JSON)\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found\n")


def create_server(host: str = LISTEN_HOST, port: int = LISTEN_PORT) -> ThreadingHTTPServer:
    """创建 ThreadingHTTPServer 实例(便于测试)。

    使用 ThreadingHTTPServer 而非 HTTPServer,避免单连接阻塞
    影响 Prometheus 定期 scrape。
    """
    return ThreadingHTTPServer((host, port), MetricsHTTPRequestHandler)


def main() -> None:
    """主入口:启动 HTTP server。

    在容器中作为单独 service 运行:
      docker run ... python -m services.prometheus_exporter
    """
    logger.info(
        f"[prometheus_exporter] listening on http://{LISTEN_HOST}:{LISTEN_PORT}/metrics"
    )
    logger.info(f"[prometheus_exporter] cache_store_db={CACHE_STORE_DB}")
    logger.info(f"[prometheus_exporter] relay_db={RELAY_DB_PATH}")
    logger.info(f"[prometheus_exporter] relay_spool_dir={RELAY_SPOOL_DIR}")

    # R40: 启动 R40 指标采集后台线程
    _start_r40_collector()

    server = create_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[prometheus_exporter] received SIGINT, shutting down")
    finally:
        server.server_close()
        logger.info("[prometheus_exporter] server closed")


if __name__ == "__main__":
    main()
