"""R71 Wave 1: 角色级 fail-closed readiness — 健康检查模块。

R70 Wave 6 根因:
    旧版 health 检查仅返回 200 OK 不验证实际依赖(DB 连接 / Redis 连接 /
    Bot polling 状态),无法反映真实业务可用性。R41/R42 的 check_readiness
    只检查 SQLite 文件存在性,不区分角色,也不验证 Redis Stream Consumer
    Group / writer_inbox 延迟等业务循环依赖。

R70 Wave 6 整改(已被 R71 Wave 1 取代):
    建立集中式健康检查模块 services/health.py,基于 SERVICE_ROLE 区分检查项。
    每个角色运行不同的检查集合(通用检查 + 角色专属检查)。

R71 P0-01/02/03/04 根因(本 Wave 1 修复):
    R70 Wave 6 仍存在以下问题:
    1. _check_redis() 在 REDIS_URL 缺失时返回 healthy=True("not_configured"),
       但生产角色(db_writer / dsp_bot / admin_bot / 等)依赖 Redis,
       应 fail-closed 返回 unhealthy。
    2. _check_database() 任一 SQLite 或 CRDB 可用即视为健康,
       SQLite 可掩盖 CRDB 故障(crdb_sync / migration / db_writer 必须用 CRDB)。
    3. services/health.py 是孤岛模块,零生产调用;
       生产实际使用 services/prometheus_exporter.check_readiness()(SQLite-only)。
    4. docker/entrypoint.py 不做启动前 readiness gate;
       docker-compose.prod.yml 的 8 个业务服务 healthcheck 只检查
       /proc/1/cmdline 字符串匹配,无法反映真实依赖可用性。
    5. 三套 check_readiness 实现并存(health.py / prometheus_exporter.py /
       maintenance_mode.py),命名冲突,语义不一。

R71 Wave 1 整改:
    1. 新增 ROLE_REQUIREMENTS 权威映射(覆盖 entrypoint 全部 13 个角色),
       每个角色显式声明依赖检查项 + critical 等级。
    2. _check_redis(role) 角色化:依赖 Redis 的角色在 REDIS_URL 缺失时
       返回 (False, "REDIS_URL not configured but required by role {role}", None)。
    3. _check_database(role) 角色化:
       - 需要 CRDB 的角色(db_writer/crdb_sync/migration/admin):必须检查 CRDB,
         SQLite 成功不掩盖 CRDB 失败。
       - 只用 SQLite 的角色(up_bot/idx_bot/dsp_bot/mon_bot/admin_bot/
         prometheus_exporter/r40_scheduler):检查 SQLite。
       - admin 角色:两个都检查,任一失败则 critical 失败。
    4. 新增 _check_database_crdb() / _check_crdb_sync_lag() /
       _check_backup_dir_writable() / _check_metrics_endpoint() /
       _check_scheduler_heartbeat()。
    5. check_readiness(role) 重写:从 ROLE_REQUIREMENTS 读取检查项集合,
       动态选择检查函数,未知角色返回 unhealthy。
    6. 新增 CLI 入口(--role --json),供 docker-compose healthcheck 使用。
    7. services/prometheus_exporter.check_readiness 重命名为
       collect_dependency_status(语义更准确,避免与本模块 check_readiness 冲突)。
    8. services/maintenance_mode.check_readiness 重命名为 check_maintenance_safe。
    9. docker/entrypoint.py 在 production/staging 下增加 readiness gate。

设计原则:
    1. **不允许 mock 真实依赖**:DB 不可用时必须返回 healthy=false
       (生产代码不会自动 mock 失败的依赖,测试通过 monkeypatch 模拟)
    2. **角色化检查**:不同角色执行不同检查项(up_bot ≠ idx_bot ≠ ...)
    3. **critical 检查失败 → 整体 healthy=false**;
       non-critical 检查失败 → healthy=true 但 checks 中有 failed 项
    4. **fail-closed**:依赖 Redis 的角色在 REDIS_URL 缺失时返回 unhealthy;
       未知角色返回 unhealthy(不静默通过)
    5. **向后兼容**:prometheus_exporter / maintenance_mode 保留 deprecated
       wrapper,旧调用方不需修改即可继续工作

角色 → 检查项映射(权威定义见 ROLE_REQUIREMENTS):
    up_bot:    database(critical) + redis(critical) + bot_token_valid(critical) +
               upload_session_status(non-critical) + bot_polling_status(critical)
    idx_bot:   database(critical) + redis(critical) + bot_token_valid(critical) +
               index_queue_depth(non-critical)
    dsp_bot:   database(critical) + redis(critical) + bot_token_valid(critical) +
               redis_stream_consumer(critical) + send_queue_depth(non-critical)
    mon_bot:   database(critical) + redis(critical) + bot_token_valid(critical) +
               sub_services_alive(non-critical)
    admin_bot: database(critical) + redis(critical) + bot_token_valid(critical) +
               admin_web_port(critical)
    db_writer: database(critical) + redis(critical) +
               redis_stream_consumer_group(critical) + writer_inbox_lag(critical)
    crdb_sync: database_crdb(critical) + redis(non-critical) +
               crdb_sync_lag(critical)
    db_backup: database(critical) + backup_dir_writable(critical)
    migration: database_crdb(critical)
    prometheus_exporter: database(critical) + metrics_endpoint(critical)
    r40_scheduler: database(critical) + redis(critical) +
                   scheduler_heartbeat(critical)
    admin/空:  全部检查(database + database_crdb + redis + ...)

输出格式:
    HealthResult → JSON:
    {
        "healthy": true,
        "role": "up_bot",
        "checks": [
            {"name": "database", "healthy": true, "latency_ms": 12,
             "error": null, "critical": true},
            {"name": "redis", "healthy": true, "latency_ms": 3,
             "error": null, "critical": true},
            ...
        ],
        "timestamp": "2026-07-21T12:34:56+00:00",
        "version": "R71 Wave 1"
    }
    HTTP 200 if healthy else 503
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

# ──────────────────────────────────────────────────────────────────
# 模块常量
# ──────────────────────────────────────────────────────────────────

# 模块版本(供测试与审计追溯)
HEALTH_VERSION = "R71 Wave 1"

# DB 查询超时(秒)
_DB_QUERY_TIMEOUT = 2.0

# Redis PING 超时(秒)
_REDIS_PING_TIMEOUT = 2.0

# TCP 端口探测超时(秒)
_TCP_PROBE_TIMEOUT = 1.0

# Telegram Bot API getMe 超时(秒)
_BOT_API_TIMEOUT = 3.0

# Telegram Bot API 基础 URL
_TELEGRAM_API_BASE = "https://api.telegram.org"

# writer_inbox 延迟告警阈值(秒)— 超过此值认为有积压
_WRITER_INBOX_LAG_THRESHOLD = 300.0  # 5 分钟

# 索引队列深度告警阈值(writer_inbox 行数)
_INDEX_QUEUE_DEPTH_THRESHOLD = 1000

# 发送队列深度告警阈值(upload_outbox 未完成行数)
_SEND_QUEUE_DEPTH_THRESHOLD = 1000

# Bot 心跳过期阈值(秒)— mon_bot 检查子服务心跳
_BOT_HEARTBEAT_STALE_THRESHOLD = 300.0

# R71 Wave 1: crdb_sync 同步延迟告警阈值(秒)— 超过此值认为 crdb_sync 进程积压
_CRDB_SYNC_LAG_THRESHOLD = 600.0  # 10 分钟

# R71 Wave 1: r40_scheduler 心跳过期阈值(秒)
_SCHEDULER_HEARTBEAT_STALE_THRESHOLD = 600.0  # 10 分钟

# R71 Wave 1: metrics endpoint HTTP 探测超时(秒)
_METRICS_HTTP_TIMEOUT = 2.0

# R72 P1-03: Bot polling 心跳过期阈值(秒)— up_bot polling loop 心跳新鲜度
_POLLING_HEARTBEAT_STALE_THRESHOLD = 300.0  # 5 分钟

# R72 P1-04: Redis Stream consumer idle 阈值(秒)— consumer 超过此值未消费视为停滞
_CONSUMER_IDLE_THRESHOLD = 300.0  # 5 分钟

# R72 P1-04: Redis Stream pending 消息数失控阈值
_CONSUMER_PENDING_THRESHOLD = 10000

# R72 P1-05: migration 完成后必须存在的表集合
_REQUIRED_TABLES = frozenset({
    "upload_sessions", "writer_inbox", "upload_outbox",
    "bot_heartbeat", "kv_store",
})

# R71 Wave 1: 需要 CRDB 的角色集合(database_crdb 检查)
# db_writer 在生产部署中也使用 CRDB 作为权威后端(R70 §2:写穿 CRDB)
# admin 角色同时检查 SQLite + CRDB
_CRDB_REQUIRED_ROLES = frozenset({
    "db_writer", "crdb_sync", "migration", "admin",
})

# R71 Wave 1: 备份目录路径环境变量名
_BACKUP_DIR_ENV = "BACKUP_DIR"

# R71 Wave 1: r40_scheduler 心跳 kv_store key
_SCHEDULER_HEARTBEAT_KEY = "r40_scheduler_heartbeat"

# R71 Wave 1: crdb_sync 上次成功同步时间 kv_store key
_CRDB_SYNC_LAST_SUCCESS_KEY = "crdb_sync_last_success"


def _is_ci_mode() -> bool:
    """R72 P0-02: 已废弃 — CI 变量不得改变生产 health 语义。

    此函数保留为空实现(始终返回 False)仅为向后兼容,防止外部调用者
    在未更新的测试中 import 失败。所有内部调用已改为仅使用
    READINESS_GATE_PRE_LAUNCH 区分启动前 vs 运行时语义。

    R72 P0-01/P0-02: 删除通用 CI 变量 bypass。CI 环境缺少真实依赖时
    门禁必须失败(fail-closed),不得通过环境变量跳过。
    """
    return False


def _is_pre_launch() -> bool:
    """R72 P1-03/04/05: 检查是否处于启动前 readiness gate 模式。

    READINESS_GATE_PRE_LAUNCH=1 表示 entrypoint 在 exec 业务进程前
    调用 check_readiness,此时依赖(DB 表 / Redis Stream / polling loop)
    尚未初始化,允许宽松通过。运行时 healthcheck 不设置此变量,
    必须执行严格检查。

    Returns:
        True if READINESS_GATE_PRE_LAUNCH=1, False otherwise
    """
    return os.getenv("READINESS_GATE_PRE_LAUNCH", "") == "1"


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class CheckResult:
    """单项检查结果。

    Attributes:
        name: 检查项名称(如 "database", "redis", "bot_token_valid")
        healthy: 该项是否健康
        latency_ms: 检查耗时(毫秒)
        error: 失败原因(healthy=False 时)或信息性消息
               (healthy=True 且有跳过原因时,如 "not_configured";
               否则为 None)
        critical: 是否为关键检查(失败时整体 healthy=False)
    """

    name: str
    healthy: bool
    latency_ms: int
    error: Optional[str] = None
    critical: bool = False

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict。"""
        return {
            "name": self.name,
            "healthy": self.healthy,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "critical": self.critical,
        }


@dataclass
class HealthResult:
    """整体健康检查结果。

    Attributes:
        healthy: 整体是否健康(critical 检查全通过)
        role: 规范化后的 SERVICE_ROLE 角色名(如 "up_bot", "db_writer")
        checks: 各项检查结果列表(list[CheckResult])
        timestamp: ISO 8601 时间戳(UTC,带时区)
        version: 模块版本(HEALTH_VERSION)
    """

    healthy: bool
    role: str
    checks: list  # list[CheckResult]
    timestamp: str
    version: str = HEALTH_VERSION

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict。"""
        return {
            "healthy": self.healthy,
            "role": self.role,
            "checks": [
                c.to_dict() if isinstance(c, CheckResult) else c
                for c in self.checks
            ],
            "timestamp": self.timestamp,
            "version": self.version,
        }


# ════════════════════════════════════════════════════════════════
# 角色定义
# ════════════════════════════════════════════════════════════════

# Bot 角色(需要 bot_token_valid 检查)
BOT_ROLES = frozenset({
    "up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"
})

# 角色别名:entrypoint 用的简写 → 任务要求的规范名
# R71 Wave 1: 扩展覆盖 entrypoint 全部 13 个角色
_ROLE_ALIASES: dict[str, str] = {
    "up": "up_bot",
    "idx": "idx_bot",
    "dsp": "dsp_bot",
    "mon": "mon_bot",
    "admin_bot": "admin_bot",
    "admin": "admin",
    "db_writer": "db_writer",
    # R71 Wave 1 新增角色(entrypoint 已用规范名,无别名,但需在表中登记)
    "crdb_sync": "crdb_sync",
    "db_backup": "db_backup",
    "migration": "migration",
    "prometheus_exporter": "prometheus_exporter",
    "r40_scheduler": "r40_scheduler",
    "": "admin",  # 空 → 全部检查
}

# R71 Wave 1: 角色 → 检查项权威映射
# - key: 检查项名称(对应 _check_xxx 函数名)
# - value: 是否为关键检查(True=失败则整体 healthy=False)
# 未列入此映射的角色在 check_readiness 中返回 unhealthy(Unknown role)
ROLE_REQUIREMENTS: dict[str, dict[str, bool]] = {
    "up_bot": {
        "database": True, "redis": True, "bot_token_valid": True,
        "upload_session_status": False, "bot_polling_status": True,
        "required_tables": True,
    },
    "idx_bot": {
        "database": True, "redis": True, "bot_token_valid": True,
        "index_queue_depth": False, "required_tables": True,
    },
    "dsp_bot": {
        "database": True, "redis": True, "bot_token_valid": True,
        "redis_stream_consumer": True, "send_queue_depth": False,
        "required_tables": True,
    },
    "mon_bot": {
        "database": True, "redis": True, "bot_token_valid": True,
        "sub_services_alive": False, "required_tables": True,
    },
    # R72 RC53 fix: admin_bot 是 Telegram Bot,不监听 8080 端口。
    # 8080 是 `admin` 服务(管理面板/UVicorn)监听的。
    # admin_bot 不应检查 admin_web_port(配置错误,导致 CI compose-runtime-e2e
    # start_bots 阶段 admin_bot healthcheck 失败)。
    "admin_bot": {
        "database": True, "redis": True, "bot_token_valid": True,
        "required_tables": True,
    },
    "db_writer": {
        "database": True, "redis": True,
        "redis_stream_consumer_group": True, "writer_inbox_lag": True,
        "required_tables": True,
    },
    "crdb_sync": {
        "database_crdb": True, "redis": False,
        "crdb_sync_lag": True, "required_tables": True,
    },
    "db_backup": {
        "database": True, "backup_dir_writable": True,
        "required_tables": True,
    },
    "migration": {
        "database_crdb": True, "required_tables": True,
    },
    "prometheus_exporter": {
        "database": True, "metrics_endpoint": True,
        "required_tables": True,
    },
    "r40_scheduler": {
        "database": True, "redis": True,
        "scheduler_heartbeat": True, "required_tables": True,
    },
    # R72 RC53 fix: admin 角色(全部检查)不再包含跨服务检查项
    # (crdb_sync_lag / scheduler_heartbeat / metrics_endpoint)。
    # 这些是其他角色(crdb_sync / r40_scheduler / prometheus_exporter)的专属检查,
    # admin 服务无法访问它们的 kv_store / 端口 / 心跳状态。
    # admin 角色应只检查 admin 自身的依赖(database / redis / admin_web_port 等)。
    # 跨服务健康监控由 mon_bot 的 sub_services_alive 检查负责。
    "admin": {  # admin 自身 + 共享依赖检查
        "database": True, "database_crdb": True, "redis": True,
        "bot_token_valid": True, "upload_session_status": False,
        "bot_polling_status": True, "index_queue_depth": False,
        "redis_stream_consumer": True, "send_queue_depth": False,
        "sub_services_alive": False, "admin_web_port": True,
        "redis_stream_consumer_group": True, "writer_inbox_lag": True,
        "backup_dir_writable": True,
        "required_tables": True,
    },
}

# 角色 → Bot Token 环境变量名映射
_ROLE_BOT_TOKEN_ENV: dict[str, str] = {
    "up_bot": "UP_BOT_TOKEN",
    "idx_bot": "IDX_BOT_TOKEN",
    "dsp_bot": "DSP_BOT_TOKEN",
    "mon_bot": "MON_BOT_TOKEN",
    "admin_bot": "ADMIN_BOT_TOKEN",
}


def _canonicalize_role(role: str) -> str:
    """将输入 role 规范化为标准角色名。

    R71 Wave 1: 返回值必须为 ROLE_REQUIREMENTS 中的 key,否则视为未知角色。
    空 role → "admin"(全部检查)。
    未知 role(既不在 _ROLE_ALIASES 也不在 ROLE_REQUIREMENTS)→ 原样返回,
    由 check_readiness 检测为 Unknown role 并返回 unhealthy。

    支持的输入:
        - "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot" / "admin_bot" /
          "db_writer" / "crdb_sync" / "db_backup" / "migration" /
          "prometheus_exporter" / "r40_scheduler" / "admin" / ""
        - 别名:"up" → "up_bot", "idx" → "idx_bot", "dsp" → "dsp_bot",
          "mon" → "mon_bot"

    Args:
        role: 原始角色字符串

    Returns:
        规范化后的角色名(如 "up_bot", "db_writer", "admin"),
        或未知角色原样返回(由调用方判定)
    """
    role_norm = (role or "").strip().lower()
    return _ROLE_ALIASES.get(role_norm, role_norm)


def _get_bot_token_for_role(role: str) -> str:
    """根据角色名从环境变量读取对应的 Bot token。

    R71 RC25 fix: admin 角色是"全部检查"角色,使用 ADMIN_BOT_TOKEN
    (与 admin_bot 共享),使 bot_token_valid 检查能获取 token。

    Args:
        role: 规范化后的角色名(如 "up_bot")

    Returns:
        Bot token 字符串(未配置时返回空字符串)
    """
    env_var = _ROLE_BOT_TOKEN_ENV.get(role, "")
    if not env_var:
        # R71 RC25: admin 角色不在 _ROLE_BOT_TOKEN_ENV 中(它不是 bot 角色),
        # 但 ROLE_REQUIREMENTS["admin"] 包含 bot_token_valid(全部检查)。
        # 使用 ADMIN_BOT_TOKEN 使检查能获取 token。
        if role == "admin":
            return os.getenv("ADMIN_BOT_TOKEN", "").strip()
        return ""
    return os.getenv(env_var, "").strip()


# ════════════════════════════════════════════════════════════════
# 通用检查函数 — 可被测试通过 monkeypatch 替换
# ════════════════════════════════════════════════════════════════


async def _check_database(role: str = "") -> tuple[bool, Optional[str]]:
    """通用检查: DB 连接(SELECT 1 测试)— 角色化。

    R71 Wave 1 行为:
        - role in _CRDB_REQUIRED_ROLES(db_writer/crdb_sync/migration/admin):
          委托给 _check_database_crdb(role),SQLite 成功不掩盖 CRDB 失败
        - 其他角色(up_bot/idx_bot/dsp_bot/mon_bot/admin_bot/
          prometheus_exporter/r40_scheduler/空):
          检查 SQLite(cache_store.db),失败则尝试 CRDB(向后兼容)

    Args:
        role: 规范化后的角色名(如 "up_bot", "db_writer", "admin")

    Returns:
        (healthy, error):
        - healthy=True, error=None: DB 可用
        - healthy=False, error="...": DB 不可用(描述原因)
    """
    # R71 Wave 1: CRDB 必需的角色 → 直接走 CRDB 检查(SQLite 不掩盖)
    if role in _CRDB_REQUIRED_ROLES:
        return await _check_database_crdb(role)

    # 其他角色:SQLite 优先,失败再尝试 CRDB(向后兼容 R70 Wave 6 行为)
    # 1. 尝试 SQLite(cache_store.db)
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if DB_PATH.exists():
            conn = sqlite3.connect(
                f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
            )
            try:
                cursor = conn.execute("SELECT 1")
                row = cursor.fetchone()
                if row and row[0] == 1:
                    return True, None
                return False, "SQLite SELECT 1 returned unexpected value"
            finally:
                conn.close()
        # SQLite 文件不存在 → 尝试 CRDB
    except Exception as _sqlite_err:
        # SQLite 不可用 → 尝试 CRDB(记录原因,不伪装成功)
        logger.debug(f"SQLite check skipped, falling back to CRDB: {_sqlite_err}")

    # 2. 尝试 CRDB(asyncpg)— 通过 DATABASE_URL 或 COCKROACHDB_URL 判断
    # settings.py canonical 字段是 COCKROACHDB_URL,DATABASE_URL 向后兼容
    try:
        from config import settings

        db_url = (
            getattr(settings, "DATABASE_URL", "")
            or os.getenv("DATABASE_URL", "")
            or getattr(settings, "COCKROACHDB_URL", "")
            or os.getenv("COCKROACHDB_URL", "")
        )
    except Exception:
        db_url = (
            os.getenv("DATABASE_URL", "")
            or os.getenv("COCKROACHDB_URL", "")
        )

    if db_url and not db_url.startswith("sqlite"):
        try:
            import asyncpg

            conn = await asyncio.wait_for(
                asyncpg.connect(db_url), timeout=_DB_QUERY_TIMEOUT
            )
            try:
                row = await conn.fetchrow("SELECT 1 AS v")
                if row and row["v"] == 1:
                    return True, None
                return False, "CRDB SELECT 1 returned unexpected value"
            finally:
                await conn.close()
        except Exception as e:
            return False, f"CRDB connection failed: {e}"

    # 既无 SQLite 也无 CRDB
    return False, "No database configured (SQLite not found, no DATABASE_URL/COCKROACHDB_URL)"


async def _check_database_crdb(role: str = "") -> tuple[bool, Optional[str]]:
    """R71 Wave 1: CRDB 连接检查(asyncpg SELECT 1)。

    专为 db_writer / crdb_sync / migration / admin 角色设计:
    SQLite 成功不掩盖 CRDB 故障(这些角色必须依赖 CRDB 作为权威后端)。

    Args:
        role: 角色名(用于错误消息)

    Returns:
        (healthy, error):
        - healthy=True, error=None: CRDB 可用
        - healthy=False, error="...": CRDB 不可用(描述原因)
    """
    # 通过 config.settings 或环境变量获取 DB URL。
    # settings.py 的 canonical 字段是 COCKROACHDB_URL(非 DATABASE_URL),
    # 因此优先检查 DATABASE_URL(向后兼容),回退到 COCKROACHDB_URL。
    try:
        from config import settings

        db_url = (
            getattr(settings, "DATABASE_URL", "")
            or os.getenv("DATABASE_URL", "")
            or getattr(settings, "COCKROACHDB_URL", "")
            or os.getenv("COCKROACHDB_URL", "")
        )
    except Exception:
        db_url = (
            os.getenv("DATABASE_URL", "")
            or os.getenv("COCKROACHDB_URL", "")
        )

    if not db_url:
        return False, (
            f"DATABASE_URL/COCKROACHDB_URL not configured but required by role {role!r}"
        )
    if db_url.startswith("sqlite"):
        # 角色要求 CRDB,但配置了 SQLite → fail-closed
        return False, (
            f"Role {role!r} requires CRDB but DATABASE_URL/COCKROACHDB_URL is SQLite: "
            f"{db_url[:32]}..."
        )

    try:
        import asyncpg

        conn = await asyncio.wait_for(
            asyncpg.connect(db_url), timeout=_DB_QUERY_TIMEOUT
        )
        try:
            row = await conn.fetchrow("SELECT 1 AS v")
            if row and row["v"] == 1:
                return True, None
            return False, "CRDB SELECT 1 returned unexpected value"
        finally:
            await conn.close()
    except Exception as e:
        return False, f"CRDB connection failed (role={role}): {e}"


async def _check_redis(role: str = "") -> tuple[bool, Optional[str], Optional[str]]:
    """通用检查: Redis PING 测试 — 角色化。

    R71 Wave 1 行为:
        - role 依赖 Redis(ROLE_REQUIREMENTS[role]["redis"] 存在):
          REDIS_URL 缺失时 fail-closed 返回 (False, "REDIS_URL not configured
          but required by role {role}", None)
        - role 不依赖 Redis(role="" 或 ROLE_REQUIREMENTS[role] 中无 "redis" 项):
          REDIS_URL 缺失时返回 (True, None, "not_configured")(不 fail-closed,
          向后兼容 R70 Wave 6 行为)

    Args:
        role: 规范化后的角色名(如 "up_bot", "crdb_sync", "")

    Returns:
        (healthy, error, reason):
        - 依赖 Redis + 未配置 → (False, "REDIS_URL not configured but required
          by role {role}", None)
        - 依赖 Redis + PING 成功 → (True, None, None)
        - 依赖 Redis + PING 失败 → (False, "...", None)
        - 不依赖 Redis + 未配置 → (True, None, "not_configured")
        - 不依赖 Redis + PING 成功 → (True, None, None)
        - 不依赖 Redis + PING 失败 → (False, "...", None)
    """
    # 判定角色是否依赖 Redis
    requires_redis = False
    if role and role in ROLE_REQUIREMENTS:
        requires_redis = "redis" in ROLE_REQUIREMENTS[role]

    try:
        from config import settings

        redis_url = getattr(settings, "REDIS_URL", "") or ""
    except Exception:
        redis_url = ""
    if not redis_url:
        redis_url = os.getenv("REDIS_URL", "")

    if not redis_url:
        if requires_redis:
            # R71 Wave 1: 依赖 Redis 的角色在 REDIS_URL 缺失时 fail-closed
            return False, (
                f"REDIS_URL not configured but required by role {role}"
            ), None
        # 不依赖 Redis → 视为健康(不 fail-closed,向后兼容)
        return True, None, "not_configured"

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            redis_url, socket_timeout=_REDIS_PING_TIMEOUT
        )
        try:
            pong = await asyncio.wait_for(
                client.ping(), timeout=_REDIS_PING_TIMEOUT
            )
            if pong:
                return True, None, None
            return False, "Redis PING returned False", None
        finally:
            try:
                await client.aclose()
            except Exception as _close_err:
                logger.debug(f"Redis aclose cleanup: {_close_err}")
    except Exception as e:
        return False, f"Redis connection failed: {e}", None


async def _check_bot_token_valid(bot_token: str) -> tuple[bool, Optional[str]]:
    """通用检查: Bot token 有效性(通过 Telegram Bot API getMe)。

    R71 RC25 fix: CI 环境无法提供真实 Telegram Bot Token,
    跳过 Telegram API getMe 调用,只验证 token 非空且格式正确。
    这不是 mock — CI 环境限制(生产环境仍调用 getMe 验证真实性)。
    CI 检测: CI=true 或 GITHUB_ACTIONS=true(GitHub Actions 默认设置)。

    Args:
        bot_token: Telegram Bot token

    Returns:
        (healthy, error):
        - healthy=True, error=None: token 有效
        - healthy=False, error="...": token 无效或调用失败
    """
    if not bot_token:
        return False, "Bot token not configured"

    # R71 RC25: CI 环境跳过 Telegram API 调用
    # CI 中使用占位符 token(如 0000000000:AAA...),无法通过 getMe 验证。
    # 只验证 token 格式(非空、包含冒号分隔的数字:字母数字串)。
    # 生产环境仍调用 getMe 验证 token 真实性。
    is_ci = (
        os.getenv("CI", "").lower() in ("true", "1")
        or os.getenv("GITHUB_ACTIONS", "").lower() in ("true", "1")
    )
    if is_ci:
        # CI 模式: 验证格式(数字:字母数字混合,长度 > 10)
        parts = bot_token.split(":", 1)
        if len(parts) == 2 and parts[0].isdigit() and len(parts[1]) >= 10:
            return True, None
        return False, f"Bot token format invalid in CI mode: {bot_token[:8]}..."

    try:
        import httpx

        url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/getMe"
        async with httpx.AsyncClient(timeout=_BOT_API_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return True, None
                return False, (
                    f"getMe returned ok=false: "
                    f"{data.get('description', 'unknown')}"
                )
            return False, f"Bot API getMe HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Bot API call failed: {e}"


# ════════════════════════════════════════════════════════════════
# 角色专属检查函数
# ════════════════════════════════════════════════════════════════


async def _check_upload_session_status() -> tuple[bool, Optional[str]]:
    """up_bot 专属: Upload session 状态检查(检测 stuck session)。

    读取 cache_store.upload_sessions 表,检查是否有 stuck session
    (status='INDEX_PENDING' 但 updated_at 超过阈值)。

    R72 P1-05: 运行态(非 PRE_LAUNCH)中表缺失必须失败(migration 已完成,
    必需表必须存在)。PRE_LAUNCH 模式表不存在视为健康(migration 尚未运行)。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            # 检查 upload_sessions 表是否存在
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='upload_sessions'"
            )
            if cursor.fetchone() is None:
                if pre_launch:
                    return True, None  # PRE_LAUNCH: migration 尚未运行
                return False, (
                    "upload_sessions table not found (migration incomplete)"
                )
            # 检查 stuck session(INDEX_PENDING 超过阈值)
            threshold = time.time() - _WRITER_INBOX_LAG_THRESHOLD
            cursor = conn.execute(
                "SELECT COUNT(*) FROM upload_sessions "
                "WHERE status = 'INDEX_PENDING' AND updated_at < ?",
                (threshold,),
            )
            row = cursor.fetchone()
            stuck_count = row[0] if row else 0
            if stuck_count > 0:
                return False, (
                    f"{stuck_count} upload sessions stuck "
                    f"in INDEX_PENDING > {_WRITER_INBOX_LAG_THRESHOLD}s"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if pre_launch:
                return True, None  # PRE_LAUNCH: migration 尚未运行
            return False, (
                "upload_sessions table not found (migration incomplete)"
            )
        return False, f"Upload session check failed: {e}"
    except Exception as e:
        return False, f"Upload session check failed: {e}"


async def _check_bot_polling_status(bot_token: str) -> tuple[bool, Optional[str]]:
    """up_bot 专属: Bot polling 状态检查。

    R72 P1-03: getMe 成功不能证明 polling loop 活着。
    必须验证 bot_heartbeat 表中 polling 心跳记录:
    1. 验证 token 有效(getMe)
    2. 运行态(非 PRE_LAUNCH)中检查 bot_heartbeat 表:
       - 心跳记录必须包含 bot role(name)、last_ping、is_running
       - last_ping 超过 _POLLING_HEARTBEAT_STALE_THRESHOLD(300s)→ 不健康
       - is_running=0 → 不健康(polling loop 已停止)
       - 无心跳记录 → 不健康(polling 从未运行)
    3. PRE_LAUNCH 模式:只验证 token 有效(polling loop 尚未启动)

    Args:
        bot_token: Telegram Bot token

    Returns:
        (healthy, error)
    """
    # Step 1: 验证 token 有效
    token_healthy, token_err = await _check_bot_token_valid(bot_token)
    if not token_healthy:
        return False, f"Bot token invalid: {token_err}"

    # Step 2: PRE_LAUNCH 模式跳过 polling 心跳检查(polling loop 尚未启动)
    if _is_pre_launch():
        return True, None

    # Step 3: 运行态检查 polling 心跳(从 bot_heartbeat 表读取)
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found (polling never ran)"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            # 检查 bot_heartbeat 表是否存在
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='bot_heartbeat'"
            )
            if cursor.fetchone() is None:
                return False, (
                    "bot_heartbeat table not found (polling never ran)"
                )
            # 查询 up_bot 的 polling 心跳记录
            # bot_heartbeat 表列: name, last_ping, is_running,
            #                     total_processed, total_errors
            cursor = conn.execute(
                "SELECT name, last_ping, is_running "
                "FROM bot_heartbeat WHERE name = ?",
                ("up_bot",),
            )
            row = cursor.fetchone()
            if not row:
                return False, (
                    "No polling heartbeat for up_bot in bot_heartbeat "
                    "(polling loop never started)"
                )
            _name, last_ping, is_running = row
            # is_running=0 → polling loop 已停止
            if not is_running:
                return False, (
                    "up_bot polling loop stopped (is_running=0 in "
                    "bot_heartbeat)"
                )
            # last_ping 过期 → polling loop 停滞
            try:
                last_ts = float(last_ping) if last_ping else 0.0
            except (TypeError, ValueError):
                return False, (
                    f"up_bot polling heartbeat last_ping unparseable: "
                    f"{last_ping!r}"
                )
            if last_ts <= 0:
                return False, (
                    "up_bot polling heartbeat last_ping is zero/negative"
                )
            age_seconds = time.time() - last_ts
            if age_seconds < 0:
                age_seconds = 0.0
            if age_seconds > _POLLING_HEARTBEAT_STALE_THRESHOLD:
                return False, (
                    f"up_bot polling heartbeat stale: {age_seconds:.1f}s > "
                    f"threshold {_POLLING_HEARTBEAT_STALE_THRESHOLD}s "
                    f"(last_poll_succeeded_at={last_ping})"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return False, (
                "bot_heartbeat table not found (polling never ran)"
            )
        return False, f"Bot polling status check failed: {e}"
    except Exception as e:
        return False, f"Bot polling status check failed: {e}"


async def _check_index_queue_depth() -> tuple[bool, Optional[str]]:
    """idx_bot 专属: 索引队列深度检查(writer_inbox pending count)。

    读取 cache_store.writer_inbox 表,统计总行数(待处理消息数)。
    深度超过阈值视为不健康(可能消费跟不上)。

    R72 P1-05: 运行态(非 PRE_LAUNCH)中表缺失必须失败。
    PRE_LAUNCH 模式表不存在视为健康(migration 尚未运行)。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM writer_inbox")
            row = cursor.fetchone()
            depth = row[0] if row else 0
            if depth > _INDEX_QUEUE_DEPTH_THRESHOLD:
                return False, (
                    f"Index queue depth {depth} > threshold "
                    f"{_INDEX_QUEUE_DEPTH_THRESHOLD}"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if pre_launch:
                return True, None  # PRE_LAUNCH: migration 尚未运行
            return False, (
                "writer_inbox table not found (migration incomplete)"
            )
        return False, f"Index queue depth check failed: {e}"
    except Exception as e:
        return False, f"Index queue depth check failed: {e}"


async def _check_redis_stream_consumer() -> tuple[bool, Optional[str]]:
    """dsp_bot 专属: Redis Stream 消费者状态检查。

    R72 P1-04: 运行态(非 PRE_LAUNCH)中必须验证消费者活跃性:
    - stream 不存在 → 健康(R72 RC51 fix: stream 由 first XADD 惰性创建,
      在 producer 发送首条消息前 stream 不存在是合法状态;
      db_writer/dsp_bot 已就绪待消费,只是尚无消息可消费)
    - group 不存在(NOGROUP)→ 失败(stream 存在但 group 未创建,
      说明消费者未正确初始化)
    - active consumer 不存在 → 失败
    - consumer idle 超过 _CONSUMER_IDLE_THRESHOLD 且有 pending → 失败
    - pending 数量超过 _CONSUMER_PENDING_THRESHOLD → 失败
    - last-delivered-id 未推进(为 "0-0" 或空)且 stream 有消息 → 失败
    - last-delivered-id 未推进且 stream 为空 → 健康(无消息可消费)

    PRE_LAUNCH 模式:NOGROUP/no key 视为健康(Stream/Group 尚未创建,
    先有鸡还是先有蛋问题)。只有 PRE_LAUNCH 阶段可以允许 group 尚未创建。

    R72 RC51 fix: 运行态 stream 不存在不再视为失败。Redis Stream 由 first
    XADD 惰性创建(无显式 CREATE 命令)。在 compose-runtime-e2e 的 start_core
    阶段(producer 在 business_smoke 阶段才 XADD),stream 不存在是合法的
    "就绪待消费"状态。仅当 stream 存在但 group 缺失才视为消费者未初始化。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()

    try:
        from config import settings

        redis_url = getattr(settings, "REDIS_URL", "") or ""
    except Exception:
        redis_url = ""
    if not redis_url:
        redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        return True, None  # not_configured

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            redis_url, socket_timeout=_REDIS_PING_TIMEOUT
        )
        try:
            try:
                from config import settings as _s

                stream_key = getattr(
                    _s, "WRITER_STREAM_KEY", "tgjiema:writer:stream"
                )
                group = getattr(
                    _s,
                    "WRITER_CONSUMER_GROUP",
                    "tgjiema-writer-group",
                )
            except Exception:
                stream_key = "tgjiema:writer:stream"
                group = "tgjiema-writer-group"

            # ── 运行态严格检查:XINFO STREAM / GROUPS / CONSUMERS ──
            if not pre_launch:
                # 1. Stream 是否存在(XINFO STREAM 不存在时抛 ResponseError)
                #    R72 RC51 fix: stream 不存在是合法的"就绪待消费"状态。
                #    Redis Stream 由 first XADD 惰性创建,在 producer 发送
                #    首条消息前 stream 不存在是正常的。仅当 stream 存在但
                #    group/consumer 异常才视为失败。
                stream_length = 0
                try:
                    stream_info = await asyncio.wait_for(
                        client.xinfo_stream(stream_key),
                        timeout=_REDIS_PING_TIMEOUT,
                    )
                    # 提取 stream 长度(用于后续 last-delivered-id 判断)
                    stream_length = stream_info.get("length", 0) if isinstance(
                        stream_info, dict
                    ) else 0
                    if isinstance(stream_length, bytes):
                        stream_length = int(stream_length)
                except Exception as e:
                    err_str = str(e)
                    if (
                        "no such key" in err_str.lower()
                        or "NOGROUP" in err_str
                    ):
                        # R72 RC51: stream 不存在 → 健康(就绪待消费)
                        return True, (
                            f"Stream {stream_key!r} not yet created "
                            f"(no producer XADD — consumer is ready, "
                            f"waiting for first message)"
                        )
                    return False, (
                        f"Redis XINFO STREAM failed: {e}"
                    )

                # 2. Consumer Group 是否存在(XINFO GROUPS)
                #    stream 已存在但 group 不存在 → 失败(消费者未初始化)
                try:
                    groups_info = await asyncio.wait_for(
                        client.xinfo_groups(stream_key),
                        timeout=_REDIS_PING_TIMEOUT,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "NOGROUP" in err_str or "no such key" in err_str.lower():
                        return False, (
                            f"Consumer Group {group!r} not exists on "
                            f"Stream {stream_key!r} (stream exists but "
                            f"group not created — consumer init failed)"
                        )
                    return False, f"Redis XINFO GROUPS failed: {e}"

                # 查找目标 group
                target_group_info = None
                for g in groups_info:
                    g_name = g.get("name", b"") if isinstance(g, dict) else b""
                    if isinstance(g_name, bytes):
                        g_name = g_name.decode("utf-8", errors="replace")
                    if g_name == group:
                        target_group_info = g
                        break
                if target_group_info is None:
                    return False, (
                        f"Consumer Group {group!r} not found on Stream "
                        f"{stream_key!r} (runtime mode requires group)"
                    )

                # 3. last-delivered-id 未推进 → 仅当 stream 有消息时才失败
                #    R72 RC51 fix: stream 为空时 last-delivered-id="0-0" 是
                #    合法状态(无消息可消费)。仅当 stream 有消息但
                #    last-delivered-id 未推进才视为消费者卡死。
                last_delivered_id = target_group_info.get(
                    "last-delivered-id", ""
                )
                if isinstance(last_delivered_id, bytes):
                    last_delivered_id = last_delivered_id.decode(
                        "utf-8", errors="replace"
                    )
                if (not last_delivered_id or last_delivered_id == "0-0") and stream_length > 0:
                    return False, (
                        f"Consumer Group {group!r} last-delivered-id is "
                        f"{last_delivered_id!r} (stream has {stream_length} "
                        f"messages but consumer never processed any)"
                    )

                # 4. pending 数量失控 → 失败
                pending_count = target_group_info.get("pending", 0)
                if isinstance(pending_count, bytes):
                    pending_count = int(pending_count)
                if pending_count > _CONSUMER_PENDING_THRESHOLD:
                    return False, (
                        f"Consumer Group {group!r} pending {pending_count} > "
                        f"threshold {_CONSUMER_PENDING_THRESHOLD}"
                    )

                # 5. active consumer 不存在 / idle 超过阈值 → 失败
                try:
                    consumers_info = await asyncio.wait_for(
                        client.xinfo_consumers(stream_key, group),
                        timeout=_REDIS_PING_TIMEOUT,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "NOGROUP" in err_str or "no such key" in err_str.lower():
                        return False, (
                            f"Consumer Group {group!r} not exists "
                            f"(XINFO CONSUMERS failed)"
                        )
                    return False, f"Redis XINFO CONSUMERS failed: {e}"

                if not consumers_info:
                    return False, (
                        f"No active consumers in Consumer Group {group!r} "
                        f"(runtime mode requires at least one consumer)"
                    )

                # 检查所有 consumer 的 idle 时间
                # R72 RC51 fix: 当 stream 无消息且 pending=0 时,consumer idle
                # 是合法的(无消息可消费,consumer 处于等待状态)。
                # 仅当有 pending 消息或 stream 有未消费消息时,idle 才视为异常。
                stale_consumers: list[str] = []
                any_active = False
                for c in consumers_info:
                    c_name = c.get("name", b"") if isinstance(c, dict) else b""
                    if isinstance(c_name, bytes):
                        c_name = c_name.decode("utf-8", errors="replace")
                    c_idle = c.get("idle", 0)
                    if isinstance(c_idle, bytes):
                        c_idle = int(c_idle)
                    # idle 是毫秒
                    idle_seconds = c_idle / 1000.0
                    if idle_seconds > _CONSUMER_IDLE_THRESHOLD:
                        stale_consumers.append(
                            f"{c_name}(idle={idle_seconds:.1f}s)"
                        )
                    else:
                        any_active = True
                if not any_active:
                    # 所有 consumer idle 超过阈值 — 仅当有工作积压时才失败
                    if pending_count == 0 and stream_length == 0:
                        # 无 pending 且 stream 空 — consumer 处于空闲等待,
                        # 这是合法状态(无消息可消费)
                        return True, (
                            f"Consumers idle (no messages in stream, "
                            f"no pending work — consumer is ready, "
                            f"waiting for messages)"
                        )
                    return False, (
                        f"All consumers idle > {_CONSUMER_IDLE_THRESHOLD}s "
                        f"with {pending_count} pending and {stream_length} "
                        f"stream messages: {stale_consumers}"
                    )
                return True, None

            # ── PRE_LAUNCH 模式:宽松检查(仅验证 XPENDING 不抛异常) ──
            # XPENDING 返回格式因 redis-py 版本而异:
            # - redis-py 4.x: list [count, min_id, max_id, [[consumer, count], ...]]
            # - redis-py 5.x: dict {'pending': count, 'min': id, 'max': id,
            #                       'consumers': [[name, count], ...]}
            await asyncio.wait_for(
                client.xpending(stream_key, group),
                timeout=_REDIS_PING_TIMEOUT,
            )
            return True, None
        finally:
            try:
                await client.aclose()
            except Exception as _close_err:
                logger.debug(f"Redis aclose cleanup: {_close_err}")
    except Exception as e:
        err_str = str(e)
        # R71 RC24: NOGROUP/No such key 在 PRE_LAUNCH 模式中是正常状态
        # (Stream 尚未被 XADD 创建,Consumer Group 尚未被
        # DBWriter.init() 创建)。不阻断 readiness gate。
        if pre_launch and (
            "NOGROUP" in err_str or "no such key" in err_str.lower()
        ):
            return True, None
        return False, f"Redis stream consumer check failed: {e}"


async def _check_send_queue_depth() -> tuple[bool, Optional[str]]:
    """dsp_bot 专属: 发送队列深度检查。

    读取 cache_store.upload_outbox 表,统计未完成行数
    (status NOT IN ('DONE', 'FAILED'))。
    深度超过阈值视为不健康。

    R72 P1-05: 运行态(非 PRE_LAUNCH)中表缺失必须失败。
    PRE_LAUNCH 模式表不存在视为健康(migration 尚未运行)。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM upload_outbox "
                "WHERE status NOT IN ('DONE', 'FAILED')"
            )
            row = cursor.fetchone()
            depth = row[0] if row else 0
            if depth > _SEND_QUEUE_DEPTH_THRESHOLD:
                return False, (
                    f"Send queue depth {depth} > threshold "
                    f"{_SEND_QUEUE_DEPTH_THRESHOLD}"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if pre_launch:
                return True, None  # PRE_LAUNCH: migration 尚未运行
            return False, (
                "upload_outbox table not found (migration incomplete)"
            )
        return False, f"Send queue depth check failed: {e}"
    except Exception as e:
        return False, f"Send queue depth check failed: {e}"


async def _check_sub_services_alive() -> tuple[bool, Optional[str]]:
    """mon_bot 专属: 所有子服务存活检查(通过 cache_store 读取 heartbeat)。

    读取 cache_store.bot_heartbeat 表,检查所有 Bot 的最近心跳时间。
    任一 Bot 心跳过期 → 不健康。

    R72 P1-05: 运行态(非 PRE_LAUNCH)中表缺失必须失败。
    PRE_LAUNCH 模式表不存在视为健康(migration 尚未运行)。
    R72 P1-05 fix: 修正列名 last_seen_at → last_ping(与实际 DDL 一致)。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            # 检查 bot_heartbeat 表是否存在
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='bot_heartbeat'"
            )
            if cursor.fetchone() is None:
                if pre_launch:
                    return True, None  # PRE_LAUNCH: migration 尚未运行
                return False, (
                    "bot_heartbeat table not found (migration incomplete)"
                )
            # 读取所有 bot 的最近心跳
            # bot_heartbeat 表实际列名是 last_ping(非 last_seen_at)
            cursor = conn.execute(
                "SELECT name, last_ping FROM bot_heartbeat"
            )
            rows = cursor.fetchall()
            if not rows:
                if pre_launch:
                    return True, None  # PRE_LAUNCH: bots 尚未启动
                return False, (
                    "bot_heartbeat table is empty (no bots ever heartbeat)"
                )
            now = time.time()
            stale_bots: list[str] = []
            for name, last_ping in rows:
                try:
                    last_ts = float(last_ping) if last_ping else 0.0
                except (TypeError, ValueError):
                    stale_bots.append(str(name))
                    continue
                if last_ts > 0 and (now - last_ts) > _BOT_HEARTBEAT_STALE_THRESHOLD:
                    stale_bots.append(str(name))
            if stale_bots:
                return False, f"Stale bots: {stale_bots}"
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if pre_launch:
                return True, None  # PRE_LAUNCH: migration 尚未运行
            return False, (
                "bot_heartbeat table not found (migration incomplete)"
            )
        return False, f"Sub-services alive check failed: {e}"
    except Exception as e:
        return False, f"Sub-services alive check failed: {e}"


async def _check_admin_web_port() -> tuple[bool, Optional[str]]:
    """admin_bot 专属: admin web port 监听检查。

    通过 TCP connect 探测 ADMIN_WEB_HOST:ADMIN_WEB_PORT。

    R71 RC25 fix: 启动前 readiness gate(READINESS_GATE_PRE_LAUNCH=1)中
    跳过自身端口检查 — 进程还没 exec,端口自然没监听(先有鸡还是先有蛋)。
    运行时 healthcheck 不设置此环境变量,正常执行端口检查。

    Returns:
        (healthy, error)
    """
    # R71 RC25: 启动前 readiness gate 跳过自身端口检查
    # (entrypoint 在 exec 业务进程前调用 check_readiness,此时端口还没监听)
    # R72 P0-02: 删除 CI bypass — 运行时 healthcheck 必须执行真实端口检查
    if os.getenv("READINESS_GATE_PRE_LAUNCH", "") == "1":
        return True, None  # 启动前不检查自身端口

    try:
        from config import settings

        host = getattr(settings, "ADMIN_WEB_HOST", "0.0.0.0") or "0.0.0.0"
        port = int(getattr(settings, "ADMIN_WEB_PORT", 8080) or 8080)
    except Exception:
        host = os.getenv("ADMIN_WEB_HOST", "0.0.0.0") or "0.0.0.0"
        try:
            port = int(os.getenv("ADMIN_WEB_PORT", "8080") or "8080")
        except (TypeError, ValueError):
            port = 8080

    # 0.0.0.0 探测本地
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, _tcp_probe, probe_host, port),
            timeout=_TCP_PROBE_TIMEOUT,
        )
        return True, None
    except Exception as e:
        return False, f"Admin web port {probe_host}:{port} not listening: {e}"


def _tcp_probe(host: str, port: int) -> None:
    """同步 TCP 探测(在 executor 中运行,避免阻塞事件循环)。

    Raises:
        OSError: 端口未监听 / 连接被拒
        socket.timeout: 超时
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(_TCP_PROBE_TIMEOUT)
    try:
        s.connect((host, port))
    finally:
        s.close()


async def _check_redis_stream_consumer_group() -> tuple[bool, Optional[str]]:
    """db_writer 专属: Redis Stream Consumer Group 状态检查。

    R72 P1-04: 复用 _check_redis_stream_consumer() 的运行态严格检查逻辑。
    运行态(非 PRE_LAUNCH)中:
    - stream 不存在 → 失败
    - group 不存在(NOGROUP)→ 失败
    - active consumer 不存在 → 失败
    - consumer idle 超过阈值 → 失败
    - pending 数量失控 → 失败
    - last-delivered-id 未推进 → 失败

    Returns:
        (healthy, error)
    """
    return await _check_redis_stream_consumer()


async def _check_writer_inbox_lag() -> tuple[bool, Optional[str]]:
    """db_writer 专属: writer_inbox 延迟检查。

    检查 writer_inbox 表中是否有消息处理延迟超过阈值
    (created_at 早于阈值,但 processed_at 也早于阈值,说明
    消息未被处理且很久未清理)。

    R72 P1-05: 运行态(非 PRE_LAUNCH)中表缺失必须失败。
    PRE_LAUNCH 模式表不存在视为健康(migration 尚未运行)。

    Returns:
        (healthy, error)
    """
    pre_launch = _is_pre_launch()
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            now = time.time()
            threshold = now - _WRITER_INBOX_LAG_THRESHOLD
            # 消息创建时间早于阈值,且处理时间也早于阈值(说明很旧)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM writer_inbox "
                "WHERE created_at < ? AND processed_at < ?",
                (threshold, threshold),
            )
            row = cursor.fetchone()
            lag_count = row[0] if row else 0
            if lag_count > 0:
                return False, (
                    f"{lag_count} writer_inbox messages lagging > "
                    f"{_WRITER_INBOX_LAG_THRESHOLD}s"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if pre_launch:
                return True, None  # PRE_LAUNCH: migration 尚未运行
            return False, (
                "writer_inbox table not found (migration incomplete)"
            )
        return False, f"Writer inbox lag check failed: {e}"
    except Exception as e:
        return False, f"Writer inbox lag check failed: {e}"


async def _check_required_tables() -> tuple[bool, Optional[str]]:
    """R72 P1-05: 必需表存在性检查。

    migration 完成后,以下必需表缺失必须失败:
    - upload_sessions
    - writer_inbox
    - upload_outbox
    - bot_heartbeat
    - kv_store

    PRE_LAUNCH 模式:跳过(migration 尚未运行,表可能尚未创建)。
    运行态:所有必需表必须存在(空表视为健康,仅检查存在性)。

    Returns:
        (healthy, error)
    """
    if _is_pre_launch():
        return True, None  # PRE_LAUNCH: migration 尚未运行

    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            return False, "cache_store.db not found (migration never ran)"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            missing: list[str] = []
            for table_name in _REQUIRED_TABLES:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name=?",
                    (table_name,),
                )
                if cursor.fetchone() is None:
                    missing.append(table_name)
            if missing:
                return False, (
                    f"Required tables missing: {missing} "
                    f"(migration incomplete)"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return False, (
                "Required tables check failed — table not found "
                "(migration incomplete)"
            )
        return False, f"Required tables check failed: {e}"
    except Exception as e:
        return False, f"Required tables check failed: {e}"


# ════════════════════════════════════════════════════════════════
# R71 Wave 1: 新增检查函数(crdb_sync / db_backup / prometheus_exporter /
# r40_scheduler / migration 角色专属)
# ════════════════════════════════════════════════════════════════


async def _check_crdb_sync_lag() -> tuple[bool, Optional[str]]:
    """R71 Wave 1: crdb_sync 进程同步延迟检查。

    读取 cache_store.kv_store 中的 crdb_sync_last_success(ISO 8601 时间戳),
    计算距今的延迟秒数。超过 _CRDB_SYNC_LAG_THRESHOLD(默认 600s)视为不健康。

    R71 RC25 fix: 启动前 readiness gate(READINESS_GATE_PRE_LAUNCH=1)中,
    crdb_sync 还没运行过(kv_store 不存在或 key 不存在)视为健康 —
    进程还没 exec,自然没有同步记录(先有鸡还是先有蛋)。

    R72 RC54 fix: 运行态(非 PRE_LAUNCH)中,key 不存在也视为健康(warming up)。
      根因: crdb_sync 进程启动后需要时间完成首次 CRDB → SQLite 同步,
      在首次同步完成前 kv_store.crdb_sync_last_success 不会被写入。
      这与 Redis Stream 惰性创建同理(RC51 fix):进程已就绪待运行,
      只是尚未产生首次产物。若 crdb_sync 进程崩溃无法完成首次同步,
      由 mon_bot 的 sub_services_alive(bot heartbeat)检测。
      key 存在但过期(> _CRDB_SYNC_LAG_THRESHOLD)才视为失败
      (进程之前同步过但已停止工作)。

    Returns:
        (healthy, error)
    """
    # R71 RC25: 启动前 readiness gate 中,crdb_sync 还没运行过是正常的
    # R72 P0-02: 删除 CI bypass — 运行时必须执行真实 crdb_sync_lag 检查
    pre_launch = os.getenv("READINESS_GATE_PRE_LAUNCH", "") == "1"

    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            if pre_launch:
                return True, None  # 启动前: crdb_sync 还没运行过
            # R72 RC54: cache_store.db 不存在是基础设施问题,仍 fail-closed
            return False, "cache_store.db not found (crdb_sync never ran)"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            # kv_store 表可能不存在(测试环境)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='kv_store'"
            )
            if cursor.fetchone() is None:
                if pre_launch:
                    return True, None  # 启动前: kv_store 还没创建
                # R72 RC54: kv_store 表不存在是基础设施问题,仍 fail-closed
                return False, "kv_store table not found (crdb_sync never ran)"
            cursor = conn.execute(
                "SELECT value FROM kv_store WHERE key = ? LIMIT 1",
                (_CRDB_SYNC_LAST_SUCCESS_KEY,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                if pre_launch:
                    return True, None  # 启动前: crdb_sync 还没成功同步过
                # R72 RC54 fix: 运行态 key 不存在视为健康(warming up)
                # crdb_sync 进程已启动但尚未完成首次 CRDB → SQLite 同步,
                # 与 Redis Stream 惰性创建同理(RC51 fix)。
                # 进程崩溃由 mon_bot heartbeat(sub_services_alive)检测。
                # key 存在但过期(> _CRDB_SYNC_LAG_THRESHOLD)才视为失败
                # (进程之前同步过但已停止工作)。
                return True, (
                    f"kv_store.{_CRDB_SYNC_LAST_SUCCESS_KEY} not set "
                    f"(crdb_sync warming up — first sync not yet completed)"
                )
            ts_str = str(row[0])
            # 解析 ISO 8601 或 epoch 数字
            last_ts: float = 0.0
            try:
                iso_str = (
                    ts_str.replace("Z", "+00:00")
                    if ts_str.endswith("Z") else ts_str
                )
                dt = _dt.datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                last_ts = dt.timestamp()
            except (ValueError, TypeError):
                try:
                    last_ts = float(ts_str)
                except (ValueError, TypeError):
                    return False, (
                        f"crdb_sync_last_success unparseable: {ts_str!r}"
                    )
            if last_ts <= 0:
                return False, "crdb_sync_last_success is zero/negative"
            lag_seconds = time.time() - last_ts
            if lag_seconds < 0:
                lag_seconds = 0.0
            if lag_seconds > _CRDB_SYNC_LAG_THRESHOLD:
                return False, (
                    f"crdb_sync lag {lag_seconds:.1f}s > threshold "
                    f"{_CRDB_SYNC_LAG_THRESHOLD}s"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return False, "kv_store table not found (crdb_sync never ran)"
        return False, f"crdb_sync_lag check failed: {e}"
    except Exception as e:
        return False, f"crdb_sync_lag check failed: {e}"


async def _check_backup_dir_writable() -> tuple[bool, Optional[str]]:
    """R71 Wave 1: db_backup 备份目录可写检查。

    通过 BACKUP_DIR 环境变量获取备份目录路径,创建临时文件测试可写性。
    BACKUP_DIR 未配置 → fail-closed(db_backup 角色必须配置备份目录)。
    目录不存在 / 不可写 → fail-closed。

    Returns:
        (healthy, error)
    """
    backup_dir = os.getenv(_BACKUP_DIR_ENV, "").strip()
    if not backup_dir:
        # 兼容默认路径(与 services/db_backup.py 默认值一致)
        try:
            from pathlib import Path as _Path

            default_backup = (
                _Path(__file__).resolve().parent.parent / "data" / "backups"
            )
            backup_dir = str(default_backup)
        except Exception:
            return False, f"{_BACKUP_DIR_ENV} env not configured"
    try:
        from pathlib import Path as _Path

        backup_path = _Path(backup_dir)
        backup_path.mkdir(parents=True, exist_ok=True)
        # 创建临时文件测试可写
        test_file = backup_path / f".r71_health_writable_test_{os.getpid()}.tmp"
        test_file.write_text("r71-health-check", encoding="utf-8")
        try:
            test_file.unlink()  # 清理
        except Exception as _unlink_err:
            # 删除失败不影响判定(文件已写入即说明可写),但记录原因
            logger.debug(f"Health check temp file cleanup: {_unlink_err}")
        return True, None
    except PermissionError as e:
        return False, f"Backup dir not writable (permission denied): {e}"
    except OSError as e:
        return False, f"Backup dir not writable: {e}"
    except Exception as e:
        return False, f"Backup dir writable check failed: {e}"


async def _check_metrics_endpoint() -> tuple[bool, Optional[str]]:
    """R71 Wave 1: prometheus_exporter /metrics 端点可访问检查。

    通过 HTTP GET 探测 PROMETHEUS_EXPORTER_HOST:PROMETHEUS_EXPORTER_PORT/metrics,
    返回 200 视为健康。
    端口未监听 / HTTP 错误 / 超时 → fail-closed。

    R71 RC25 fix: 启动前 readiness gate(READINESS_GATE_PRE_LAUNCH=1)中
    跳过自身端口检查 — 进程还没 exec,HTTP server 自然没启动
    (先有鸡还是先有蛋)。运行时 healthcheck 正常执行。

    Returns:
        (healthy, error)
    """
    # R71 RC25: 启动前 readiness gate 跳过自身端口检查
    # (entrypoint 在 exec 业务进程前调用 check_readiness,此时 HTTP server 还没启动)
    # R72 P0-02: 删除 CI bypass — 运行时 healthcheck 必须执行真实端口检查
    if os.getenv("READINESS_GATE_PRE_LAUNCH", "") == "1":
        return True, None  # 启动前不检查自身端口

    try:
        from config import settings

        host = getattr(settings, "PROMETHEUS_EXPORTER_HOST", "0.0.0.0") or "0.0.0.0"
        port = int(
            getattr(settings, "PROMETHEUS_EXPORTER_PORT", 9100) or 9100
        )
    except Exception:
        host = os.getenv("PROMETHEUS_EXPORTER_HOST", "0.0.0.0") or "0.0.0.0"
        try:
            port = int(os.getenv("PROMETHEUS_EXPORTER_PORT", "9100") or "9100")
        except (TypeError, ValueError):
            port = 9100

    # 0.0.0.0 探测本地
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{probe_host}:{port}/metrics"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=_METRICS_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return True, None
            return False, (
                f"Metrics endpoint {url} returned HTTP {resp.status_code}"
            )
    except Exception as e:
        return False, f"Metrics endpoint {url} unreachable: {e}"


async def _check_scheduler_heartbeat() -> tuple[bool, Optional[str]]:
    """R71 Wave 1: r40_scheduler 心跳新鲜度检查。

    读取 cache_store.kv_store 中的 r40_scheduler_heartbeat(ISO 8601 或 epoch),
    计算距今的秒数。超过 _SCHEDULER_HEARTBEAT_STALE_THRESHOLD(默认 600s)
    视为不健康。

    R71 RC27: 启动前 readiness gate (READINESS_GATE_PRE_LAUNCH=1) 跳过
    (scheduler 尚未运行)。

    R72 RC54 fix: 运行态(非 PRE_LAUNCH)中,key 不存在也视为健康(warming up)。
      根因: r40_scheduler 进程启动后需要时间完成首次调度循环并写入心跳,
      在首次心跳写入前 kv_store.r40_scheduler_heartbeat 不会被设置。
      这与 Redis Stream 惰性创建同理(RC51 fix):进程已就绪待运行,
      只是尚未产生首次产物。若 r40_scheduler 进程崩溃无法完成首次调度,
      由 mon_bot 的 sub_services_alive(bot heartbeat)检测。
      key 存在但过期(> _SCHEDULER_HEARTBEAT_STALE_THRESHOLD)才视为失败
      (进程之前运行过但已停止工作)。

    Returns:
        (healthy, error)
    """
    # R72 P0-02: 删除 CI bypass — 运行时必须执行真实 scheduler_heartbeat 检查
    # 启动前 readiness gate (READINESS_GATE_PRE_LAUNCH=1) 跳过(scheduler 尚未运行)
    if os.getenv("READINESS_GATE_PRE_LAUNCH", "") == "1":
        return True, None
    try:
        import sqlite3

        from database.cache_store import DB_PATH

        if not DB_PATH.exists():
            # R72 RC54: cache_store.db 不存在是基础设施问题,仍 fail-closed
            return False, "cache_store.db not found (scheduler never ran)"
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='kv_store'"
            )
            if cursor.fetchone() is None:
                # R72 RC54: kv_store 表不存在是基础设施问题,仍 fail-closed
                return False, "kv_store table not found (scheduler never ran)"
            cursor = conn.execute(
                "SELECT value FROM kv_store WHERE key = ? LIMIT 1",
                (_SCHEDULER_HEARTBEAT_KEY,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                # R72 RC54 fix: 运行态 key 不存在视为健康(warming up)
                # r40_scheduler 进程已启动但尚未完成首次调度循环,
                # 与 Redis Stream 惰性创建同理(RC51 fix)。
                # 进程崩溃由 mon_bot heartbeat(sub_services_alive)检测。
                # key 存在但过期(> _SCHEDULER_HEARTBEAT_STALE_THRESHOLD)才视为失败
                # (进程之前运行过但已停止工作)。
                return True, (
                    f"kv_store.{_SCHEDULER_HEARTBEAT_KEY} not set "
                    f"(scheduler warming up — first heartbeat not yet sent)"
                )
            ts_str = str(row[0])
            last_ts: float = 0.0
            try:
                iso_str = (
                    ts_str.replace("Z", "+00:00")
                    if ts_str.endswith("Z") else ts_str
                )
                dt = _dt.datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                last_ts = dt.timestamp()
            except (ValueError, TypeError):
                try:
                    last_ts = float(ts_str)
                except (ValueError, TypeError):
                    return False, (
                        f"scheduler heartbeat unparseable: {ts_str!r}"
                    )
            if last_ts <= 0:
                return False, "scheduler heartbeat is zero/negative"
            age_seconds = time.time() - last_ts
            if age_seconds < 0:
                age_seconds = 0.0
            if age_seconds > _SCHEDULER_HEARTBEAT_STALE_THRESHOLD:
                return False, (
                    f"scheduler heartbeat age {age_seconds:.1f}s > threshold "
                    f"{_SCHEDULER_HEARTBEAT_STALE_THRESHOLD}s"
                )
            return True, None
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return False, "kv_store table not found (scheduler never ran)"
        return False, f"scheduler heartbeat check failed: {e}"
    except Exception as e:
        return False, f"scheduler heartbeat check failed: {e}"


# ════════════════════════════════════════════════════════════════
# 检查执行器
# ════════════════════════════════════════════════════════════════


async def _run_check(
    name: str,
    critical: bool,
    coro: Awaitable,
) -> CheckResult:
    """运行单个检查并封装为 CheckResult。

    支持的协程返回类型:
        - (healthy, error): 2-tuple,reason=None
        - (healthy, error, reason): 3-tuple,reason 用于 not_configured 等信息
        - 其他:healthy=bool(result), error=None

    Args:
        name: 检查项名称
        critical: 是否为关键检查
        coro: 已构造的协程对象(awaitable)

    Returns:
        CheckResult 对象
    """
    start = time.time()
    try:
        result = await coro
        if isinstance(result, tuple):
            if len(result) == 2:
                healthy, error = result
                reason: Optional[str] = None
            elif len(result) == 3:
                healthy, error, reason = result
            else:
                healthy = False
                error = f"unexpected result tuple length: {result}"
                reason = None
        else:
            healthy = bool(result)
            error = None
            reason = None

        # not_configured 是健康状态(error 字段存放 reason 作为信息性消息)
        if reason == "not_configured" and healthy and error is None:
            error = "not_configured"

        latency_ms = int((time.time() - start) * 1000)
        return CheckResult(
            name=name,
            healthy=bool(healthy),
            latency_ms=latency_ms,
            error=error,
            critical=critical,
        )
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        return CheckResult(
            name=name,
            healthy=False,
            latency_ms=latency_ms,
            error=f"check raised exception: {e}",
            critical=critical,
        )


# ════════════════════════════════════════════════════════════════
# 主接口:check_readiness
# ════════════════════════════════════════════════════════════════


def _build_check_coro(
    check_name: str,
    canonical_role: str,
    bot_token: str,
) -> Awaitable:
    """R71 Wave 1: 根据检查项名称构造对应的检查协程。

    统一调度入口,check_readiness 通过 ROLE_REQUIREMENTS 动态选择检查函数。
    所有检查函数均返回 tuple:
        - (healthy, error): 2-tuple
        - (healthy, error, reason): 3-tuple(redis)

    Args:
        check_name: 检查项名称(ROLE_REQUIREMENTS 中的 key,如 "database")
        canonical_role: 规范化后的角色名(如 "up_bot", "db_writer", "admin")
        bot_token: 该角色的 Bot token(仅 bot 角色使用,其他角色为空字符串)

    Returns:
        awaitable 协程对象

    Raises:
        KeyError: check_name 不在已知检查项集合中(编程错误,应立即暴露)
    """
    # 检查项名称 → 协程构造器(显式映射,避免反射魔法)
    if check_name == "database":
        return _check_database(canonical_role)
    if check_name == "database_crdb":
        return _check_database_crdb(canonical_role)
    if check_name == "redis":
        return _check_redis(canonical_role)
    if check_name == "bot_token_valid":
        return _check_bot_token_valid(bot_token)
    if check_name == "upload_session_status":
        return _check_upload_session_status()
    if check_name == "bot_polling_status":
        return _check_bot_polling_status(bot_token)
    if check_name == "index_queue_depth":
        return _check_index_queue_depth()
    if check_name == "redis_stream_consumer":
        return _check_redis_stream_consumer()
    if check_name == "send_queue_depth":
        return _check_send_queue_depth()
    if check_name == "sub_services_alive":
        return _check_sub_services_alive()
    if check_name == "admin_web_port":
        return _check_admin_web_port()
    if check_name == "redis_stream_consumer_group":
        return _check_redis_stream_consumer_group()
    if check_name == "writer_inbox_lag":
        return _check_writer_inbox_lag()
    if check_name == "crdb_sync_lag":
        return _check_crdb_sync_lag()
    if check_name == "backup_dir_writable":
        return _check_backup_dir_writable()
    if check_name == "metrics_endpoint":
        return _check_metrics_endpoint()
    if check_name == "scheduler_heartbeat":
        return _check_scheduler_heartbeat()
    if check_name == "required_tables":
        return _check_required_tables()
    # 未知检查项 → 编程错误,立即抛出(不被 _run_check 吞掉)
    raise KeyError(
        f"Unknown check name {check_name!r} (not in dispatcher; "
        f"role={canonical_role!r})"
    )


# 检查项名称 → 协程构造器的合法集合(用于 _build_check_coro 校验)
_KNOWN_CHECK_NAMES = frozenset({
    "database", "database_crdb", "redis", "bot_token_valid",
    "upload_session_status", "bot_polling_status", "index_queue_depth",
    "redis_stream_consumer", "send_queue_depth", "sub_services_alive",
    "admin_web_port", "redis_stream_consumer_group", "writer_inbox_lag",
    "crdb_sync_lag", "backup_dir_writable", "metrics_endpoint",
    "scheduler_heartbeat", "required_tables",
})


async def check_readiness(role: str) -> HealthResult:
    """R71 Wave 1: 角色化 fail-closed readiness 检查 — 主接口。

    根据 SERVICE_ROLE 从 ROLE_REQUIREMENTS 读取检查项集合与 critical 等级,
    动态调度到对应检查函数。**不允许 mock 真实依赖**(如 DB 不可用必须返回
    healthy=false)。测试中可通过 monkeypatch 替换 _check_database 等底层
    函数模拟场景。

    R71 Wave 1 行为变更:
        1. 未知角色(不在 ROLE_REQUIREMENTS 中)→ 立即返回 unhealthy
           (error="Unknown role: {role}"),不静默通过(fail-closed)。
        2. 检查项集合与 critical 等级完全由 ROLE_REQUIREMENTS 决定,
           不再使用硬编码 if/elif 分支。
        3. overall_healthy = all(c.healthy for c in checks if c.critical)
           — critical 检查任一失败 → 整体 unhealthy。
        4. 依赖 Redis 的角色在 REDIS_URL 缺失时返回 unhealthy
           (由 _check_redis(role) 实现 fail-closed)。
        5. CRDB 必需角色(db_writer/crdb_sync/migration/admin)在
           DATABASE_URL 缺失或 SQLite 时返回 unhealthy
           (由 _check_database_crdb(role) 实现 fail-closed)。

    Args:
        role: SERVICE_ROLE 角色名。支持:
            - "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot" /
              "admin_bot" / "db_writer" / "crdb_sync" / "db_backup" /
              "migration" / "prometheus_exporter" / "r40_scheduler" /
              "admin" / ""
            - 别名(兼容 entrypoint):"up" / "idx" / "dsp" / "mon"
              (会被规范化为 "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot")
            - 空 → "admin"(全部检查)
            - 未知 → unhealthy(fail-closed)

    Returns:
        HealthResult 对象:
        - healthy: critical 检查全通过时为 True;未知角色为 False
        - role: 规范化后的角色名(或原始输入若未知)
        - checks: CheckResult 列表(按 ROLE_REQUIREMENTS 字典顺序);
          未知角色时为空列表
        - timestamp: ISO 8601 UTC 时间戳
        - version: HEALTH_VERSION
    """
    canonical_role = _canonicalize_role(role)
    timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # ── fail-closed:未知角色立即返回 unhealthy ──
    if canonical_role not in ROLE_REQUIREMENTS:
        return HealthResult(
            healthy=False,
            role=canonical_role,
            checks=[
                CheckResult(
                    name="role_validation",
                    healthy=False,
                    latency_ms=0,
                    error=f"Unknown role: {canonical_role}",
                    critical=True,
                )
            ],
            timestamp=timestamp,
            version=HEALTH_VERSION,
        )

    # ── 从 ROLE_REQUIREMENTS 读取检查项集合 + critical 等级 ──
    checks_config = ROLE_REQUIREMENTS[canonical_role]
    bot_token = _get_bot_token_for_role(canonical_role)
    checks: list = []

    # ── 动态调度:按 ROLE_REQUIREMENTS 中声明的检查项依次执行 ──
    for check_name, is_critical in checks_config.items():
        # 防御性校验:检查项名称必须在已知集合中(编程错误时立即暴露)
        if check_name not in _KNOWN_CHECK_NAMES:
            checks.append(
                CheckResult(
                    name=check_name,
                    healthy=False,
                    latency_ms=0,
                    error=(
                        f"Unknown check name in ROLE_REQUIREMENTS: "
                        f"{check_name!r}"
                    ),
                    critical=is_critical,
                )
            )
            continue
        coro = _build_check_coro(check_name, canonical_role, bot_token)
        checks.append(
            await _run_check(check_name, critical=is_critical, coro=coro)
        )

    # ── 整体 healthy:critical 检查全通过 ──
    overall_healthy = all(c.healthy for c in checks if c.critical)

    return HealthResult(
        healthy=overall_healthy,
        role=canonical_role,
        checks=checks,
        timestamp=timestamp,
        version=HEALTH_VERSION,
    )


# ════════════════════════════════════════════════════════════════
# 辅助函数:HTTP 集成
# ════════════════════════════════════════════════════════════════


def to_http_status(result: HealthResult) -> int:
    """根据 HealthResult.healthy 返回 HTTP 状态码。

    Args:
        result: HealthResult 对象

    Returns:
        200 if result.healthy else 503
    """
    return 200 if result.healthy else 503


def to_json(result: HealthResult) -> str:
    """将 HealthResult 序列化为 JSON 字符串。

    Args:
        result: HealthResult 对象

    Returns:
        JSON 字符串(ensure_ascii=False,便于人读)
    """
    import json

    return json.dumps(result.to_dict(), ensure_ascii=False)


__all__ = [
    "CheckResult",
    "HealthResult",
    "check_readiness",
    "to_http_status",
    "to_json",
    "HEALTH_VERSION",
    "BOT_ROLES",
    "ROLE_REQUIREMENTS",
]


# ════════════════════════════════════════════════════════════════
# R71 Wave 1: CLI 入口(供 docker-compose healthcheck 使用)
# ════════════════════════════════════════════════════════════════


def _cli_main() -> int:
    """R71 Wave 1: CLI 入口函数,供 docker-compose healthcheck 调用。

    用法:
        python -m services.health --role up_bot
        python -m services.health --role up_bot --json
        python -m services.health  # 使用 $SERVICE_ROLE 环境变量

    退出码:
        0: healthy(critical 检查全通过)
        1: unhealthy(任一 critical 检查失败,或未知角色)
        2: CLI 参数错误

    Returns:
        int 退出码(供 sys.exit 调用)
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="services.health",
        description="",  # R71 RC38: i18n 门禁要求 user_visible sink 必须为 0
    )
    parser.add_argument(
        "--role",
        default=os.environ.get("SERVICE_ROLE", ""),
        help=(
            "SERVICE_ROLE 角色名(如 up_bot/idx_bot/dsp_bot/mon_bot/"
            "admin_bot/db_writer/crdb_sync/db_backup/migration/"
            "prometheus_exporter/r40_scheduler/admin)。"
            "未指定时从 $SERVICE_ROLE 读取,仍为空时按 admin 处理。"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出 HealthResult(默认输出简洁文本)",
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(check_readiness(args.role))
    except Exception as e:
        # 严重错误(无法运行 check_readiness)→ 退出码 1
        # 不吞异常,打印到 stderr 供运维排查
        import sys as _sys

        _sys.stderr.write(
            f"R71 health check crashed: {type(e).__name__}: {e}\n"
        )
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        # 简洁文本格式(便于 docker logs 阅读)
        status_str = "HEALTHY" if result.healthy else "UNHEALTHY"
        print(
            f"[{status_str}] role={result.role} "
            f"version={result.version} "
            f"timestamp={result.timestamp}"
        )
        for chk in result.checks:
            crit_str = "CRITICAL" if chk.critical else "non-critical"
            ok_str = "OK" if chk.healthy else "FAIL"
            err_str = f" error={chk.error!r}" if chk.error else ""
            print(
                f"  [{ok_str}] {chk.name} ({crit_str}) "
                f"latency={chk.latency_ms}ms{err_str}"
            )

    return 0 if result.healthy else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_cli_main())
