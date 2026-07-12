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


def collect_metrics() -> str:
    """采集所有指标并格式化为 Prometheus text format。

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

    return "\n".join(lines) + "\n"


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
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
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
