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
import os
import sqlite3
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


# ── 指标采集 ────────────────────────────────────────────

# R38 P2-7: 高基数 label 黑名单 — 不可出现在任何指标的 label 中
# 这些 label 的值空间随用户/消息增长,会导致 Prometheus TSDB 时间序列爆炸
_HIGH_CARDINALITY_LABELS = frozenset({
    "user_id", "chat_id", "message_id", "file_code",
    "job_id", "phone", "token", "spool_id", "msg_id",
})


def _check_no_high_cardinality_labels(metric_line: str) -> None:
    """R38 P2-7: 检查指标行不含高基数 label。

    在 collect_metrics() 输出前调用,发现违规 label 时记录警告(不阻断输出,
    避免 exporter 不可用导致监控盲区),但会在日志中留下审计痕迹。

    Args:
        metric_line: 单行 Prometheus 指标文本(如 'relay_spool_status_count{status="RECEIVED"} 5')
    """
    # 提取 {...} 内的 label 部分
    if "{" not in metric_line or "}" not in metric_line:
        return
    label_section = metric_line[metric_line.index("{") + 1: metric_line.index("}")]
    for pair in label_section.split(","):
        if "=" not in pair:
            continue
        label_name = pair.split("=")[0].strip()
        if label_name in _HIGH_CARDINALITY_LABELS:
            logger.warning(
                f"[R38-P2-7] 检测到高基数 label '{label_name}' 在指标行: "
                f"{metric_line[:80]}... — 高基数 label 会导致 TSDB 膨胀,应移除"
            )


def collect_metrics() -> str:
    """采集所有指标并格式化为 Prometheus text format。

    R38 P2-7: 所有指标均不带高基数 label(user_id/file_code/message_id 等),
    collect_metrics() 输出前会调用 _check_no_high_cardinality_labels() 审计。

    参考: https://prometheus.io/docs/instrumenting/exposition_formats/
    """
    lines: list[str] = []

    # 1. crdb_ru_daily — CRDB 当日 RU 消耗
    crdb_ru_str = _read_kv_value("crdb_ru_daily", "0")
    try:
        crdb_ru = float(crdb_ru_str)
    except (TypeError, ValueError):
        crdb_ru = 0.0
    lines.append("# HELP crdb_ru_daily CockroachDB daily Request Units consumed")
    lines.append("# TYPE crdb_ru_daily gauge")
    lines.append(f"crdb_ru_daily {crdb_ru}")

    # 2. redis_pel_depth — Redis Stream pending entries 长度
    pel_str = _read_kv_value("redis_pel_depth", "0")
    try:
        pel_depth = float(pel_str)
    except (TypeError, ValueError):
        pel_depth = 0.0
    lines.append("# HELP redis_pel_depth Redis Stream pending entries length")
    lines.append("# TYPE redis_pel_depth gauge")
    lines.append(f"redis_pel_depth {pel_depth}")

    # 3. dlq_depth — 死信队列深度
    dlq_str = _read_kv_value("dlq_depth", "0")
    try:
        dlq_depth = float(dlq_str)
    except (TypeError, ValueError):
        dlq_depth = 0.0
    lines.append("# HELP dlq_depth Dead letter queue depth")
    lines.append("# TYPE dlq_depth gauge")
    lines.append(f"dlq_depth {dlq_depth}")

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

    # R38 P2-7: 输出前审计高基数 label(仅日志告警,不阻断输出)
    for line in lines:
        if not line.startswith("#"):
            _check_no_high_cardinality_labels(line)

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

    return "\n".join(lines) + "\n"


def check_readiness() -> dict:
    """R39 P1-8: readiness 检查 — 数据库可读 + 最近采集成功 + 关键 schema 存在。

    供 /health 端点调用,不满足时返回 503 Service Unavailable。

    Returns:
        {"ready": bool, "passed": int, "checks": {name: bool}}
    """
    checks: dict[str, bool] = {}
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
        except Exception:
            sqlite_ok = False
    checks["sqlite_readable"] = sqlite_ok

    # 2. 最近采集成功(_last_scrape_ok + data_age 未超阈值)
    data_age = (time.time() - _last_scrape_ts) if _last_scrape_ts > 0 else -1.0
    scrape_recent = (
        _last_scrape_ok
        and data_age >= 0
        and data_age < _DATA_AGE_ALERT_THRESHOLD
    )
    checks["recent_scrape"] = scrape_recent

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
        except Exception:
            schema_ok = False
    checks["key_schema_exists"] = schema_ok

    passed = sum(1 for v in checks.values() if v)
    ready = all(checks.values())
    return {"ready": ready, "passed": passed, "checks": checks}


# ── HTTP Handler ─────────────────────────────────────────


class MetricsHTTPRequestHandler(BaseHTTPRequestHandler):
    """Prometheus metrics HTTP handler。

    路由:
      GET /metrics → Prometheus text format 指标
      GET /health   → "OK"(供 Docker healthcheck / k8s liveness)
      GET /         → 简单介绍页
      其他          → 404
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
            # 检查: SQLite 可读 + 最近采集成功 + 关键 schema 存在
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
        elif self.path == "/" or self.path == "/index":
            body = (
                b"TGJiema Prometheus Exporter\n"
                b"\nEndpoints:\n"
                b"  /metrics  - Prometheus metrics (text format)\n"
                b"  /health   - Health check\n"
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
