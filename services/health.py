"""R70 Wave 6: 真实 readiness — 角色级健康检查模块。

R70 报告根因:
    旧版 health 检查仅返回 200 OK 不验证实际依赖(DB 连接 / Redis 连接 /
    Bot polling 状态),无法反映真实业务可用性。R41/R42 的 check_readiness
    只检查 SQLite 文件存在性,不区分角色,也不验证 Redis Stream Consumer
    Group / writer_inbox 延迟等业务循环依赖。

R70 Wave 6 整改:
    建立集中式健康检查模块 services/health.py,基于 SERVICE_ROLE 区分检查项。
    每个角色运行不同的检查集合(通用检查 + 角色专属检查)。

设计原则:
    1. **不允许 mock 真实依赖**:DB 不可用时必须返回 healthy=false
       (生产代码不会自动 mock 失败的依赖,测试通过 monkeypatch 模拟)
    2. **角色化检查**:不同角色执行不同检查项(up_bot ≠ idx_bot ≠ ...)
    3. **critical 检查失败 → 整体 healthy=false**;
       non-critical 检查失败 → healthy=true 但 checks 中有 failed 项
    4. **向后兼容**:不修改现有 health 端点
       (services/prometheus_exporter.check_readiness 保持不变)
    5. **可由 entrypoint 可选调用**:不强制接入,生产可逐步切换

角色 → 检查项映射:
    up_bot:    database(critical) + redis + bot_token_valid +
               upload_session_status + bot_polling_status
    idx_bot:   database(critical) + redis + bot_token_valid +
               index_queue_depth
    dsp_bot:   database(critical) + redis + bot_token_valid +
               redis_stream_consumer + send_queue_depth
    mon_bot:   database(critical) + redis + bot_token_valid +
               sub_services_alive
    admin_bot: database(critical) + redis + bot_token_valid +
               admin_web_port
    db_writer: database(critical) + redis +
               redis_stream_consumer_group + writer_inbox_lag
    admin/空:  database(critical) + redis + 全部角色专属检查

通用检查:
    - database: SELECT 1 测试 (critical=True)
    - redis: PING 测试 (未配置 REDIS_URL 时 healthy=True,
      error="not_configured",不 fail-closed)
    - bot_token_valid: 通过 Telegram Bot API getMe 验证 token 有效
      (仅 bot 角色:up_bot/idx_bot/dsp_bot/mon_bot/admin_bot)

输出格式:
    HealthResult → JSON:
    {
        "healthy": true,
        "role": "up_bot",
        "checks": [
            {"name": "database", "healthy": true, "latency_ms": 12,
             "error": null, "critical": true},
            {"name": "redis", "healthy": true, "latency_ms": 3,
             "error": "not_configured", "critical": false},
            ...
        ],
        "timestamp": "2026-07-21T12:34:56+00:00",
        "version": "R70 Wave 6"
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

# ──────────────────────────────────────────────────────────────────
# 模块常量
# ──────────────────────────────────────────────────────────────────

# 模块版本(供测试与审计追溯)
HEALTH_VERSION = "R70 Wave 6"

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
_ROLE_ALIASES: dict[str, str] = {
    "up": "up_bot",
    "idx": "idx_bot",
    "dsp": "dsp_bot",
    "mon": "mon_bot",
    "admin_bot": "admin_bot",
    "admin": "admin",
    "db_writer": "db_writer",
    "": "admin",  # 空 → 全部检查
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

    支持的输入:
        - "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot" / "admin_bot" /
          "db_writer" / "admin" / ""
        - 别名:"up" → "up_bot", "idx" → "idx_bot", "dsp" → "dsp_bot",
          "mon" → "mon_bot"

    Args:
        role: 原始角色字符串

    Returns:
        规范化后的角色名(如 "up_bot", "db_writer", "admin")
    """
    role_norm = (role or "").strip().lower()
    return _ROLE_ALIASES.get(role_norm, role_norm)


def _get_bot_token_for_role(role: str) -> str:
    """根据角色名从环境变量读取对应的 Bot token。

    Args:
        role: 规范化后的角色名(如 "up_bot")

    Returns:
        Bot token 字符串(未配置时返回空字符串)
    """
    env_var = _ROLE_BOT_TOKEN_ENV.get(role, "")
    if not env_var:
        return ""
    return os.getenv(env_var, "").strip()


# ════════════════════════════════════════════════════════════════
# 通用检查函数 — 可被测试通过 monkeypatch 替换
# ════════════════════════════════════════════════════════════════


async def _check_database() -> tuple[bool, Optional[str]]:
    """通用检查: DB 连接(SELECT 1 测试)。

    优先尝试 SQLite(cache_store.db),失败则尝试 CRDB(asyncpg)。
    任一数据库可用即视为健康。

    Returns:
        (healthy, error):
        - healthy=True, error=None: DB 可用
        - healthy=False, error="...": DB 不可用(描述原因)
    """
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
    except Exception:
        # SQLite 不可用 → 尝试 CRDB
        pass

    # 2. 尝试 CRDB(asyncpg)— 通过 DATABASE_URL 判断
    try:
        from config import settings

        db_url = getattr(settings, "DATABASE_URL", "") or os.getenv(
            "DATABASE_URL", ""
        )
    except Exception:
        db_url = os.getenv("DATABASE_URL", "")

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
    return False, "No database configured (SQLite not found, no DATABASE_URL)"


async def _check_redis() -> tuple[bool, Optional[str], Optional[str]]:
    """通用检查: Redis PING 测试。

    若未配置 REDIS_URL → 返回 (True, None, "not_configured")(不 fail-closed)。
    若配置但连接失败 → 返回 (False, error, None)。
    若 PING 成功 → 返回 (True, None, None)。

    Returns:
        (healthy, error, reason):
        - healthy=True, error=None, reason="not_configured":
          未配置 REDIS_URL(视为健康,不 fail-closed)
        - healthy=True, error=None, reason=None: PING 成功
        - healthy=False, error="...", reason=None: PING 失败
    """
    try:
        from config import settings

        redis_url = getattr(settings, "REDIS_URL", "") or ""
    except Exception:
        redis_url = ""
    if not redis_url:
        redis_url = os.getenv("REDIS_URL", "")

    if not redis_url:
        # 未配置 → 视为健康(不 fail-closed)
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
            except Exception:
                pass
    except Exception as e:
        return False, f"Redis connection failed: {e}", None


async def _check_bot_token_valid(bot_token: str) -> tuple[bool, Optional[str]]:
    """通用检查: Bot token 有效性(通过 Telegram Bot API getMe)。

    Args:
        bot_token: Telegram Bot token

    Returns:
        (healthy, error):
        - healthy=True, error=None: token 有效
        - healthy=False, error="...": token 无效或调用失败
    """
    if not bot_token:
        return False, "Bot token not configured"
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
    表不存在时视为健康(可能未启用 upload session 模块)。

    Returns:
        (healthy, error)
    """
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
                # upload_sessions 表不存在 → 视为健康(未启用)
                return True, None
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
            return True, None
        return False, f"Upload session check failed: {e}"
    except Exception as e:
        return False, f"Upload session check failed: {e}"


async def _check_bot_polling_status(bot_token: str) -> tuple[bool, Optional[str]]:
    """up_bot 专属: Bot polling 状态检查。

    通过 getMe 验证 Bot token 有效(不调用 getUpdates,避免消费消息副作用)。
    与 _check_bot_token_valid 共享实现,但语义不同:
    - bot_token_valid: 通用检查(token 是否有效)
    - bot_polling_status: up_bot 专属检查(Bot 是否能正常 polling)
    """
    return await _check_bot_token_valid(bot_token)


async def _check_index_queue_depth() -> tuple[bool, Optional[str]]:
    """idx_bot 专属: 索引队列深度检查(writer_inbox pending count)。

    读取 cache_store.writer_inbox 表,统计总行数(待处理消息数)。
    深度超过阈值视为不健康(可能消费跟不上)。
    表不存在时视为健康(无积压)。

    Returns:
        (healthy, error)
    """
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
            return True, None
        return False, f"Index queue depth check failed: {e}"
    except Exception as e:
        return False, f"Index queue depth check failed: {e}"


async def _check_redis_stream_consumer() -> tuple[bool, Optional[str]]:
    """dsp_bot 专属: Redis Stream 消费者状态检查。

    检查 Redis Stream Consumer Group 是否有 consumer 在消费。
    未配置 REDIS_URL 时视为健康(not_configured,不 fail-closed)。

    Returns:
        (healthy, error)
    """
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

            # XPENDING 返回 (count, min_id, max_id, consumers)
            pending_info = await asyncio.wait_for(
                client.xpending(stream_key, group),
                timeout=_REDIS_PING_TIMEOUT,
            )
            consumers = (
                pending_info[3]
                if pending_info and len(pending_info) > 3
                else []
            )
            if not consumers:
                return False, "No active consumer in group"
            return True, None
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
    except Exception as e:
        return False, f"Redis stream consumer check failed: {e}"


async def _check_send_queue_depth() -> tuple[bool, Optional[str]]:
    """dsp_bot 专属: 发送队列深度检查。

    读取 cache_store.upload_outbox 表,统计未完成行数
    (status NOT IN ('DONE', 'FAILED'))。
    深度超过阈值视为不健康。
    表不存在时视为健康。

    Returns:
        (healthy, error)
    """
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
            return True, None
        return False, f"Send queue depth check failed: {e}"
    except Exception as e:
        return False, f"Send queue depth check failed: {e}"


async def _check_sub_services_alive() -> tuple[bool, Optional[str]]:
    """mon_bot 专属: 所有子服务存活检查(通过 cache_store 读取 heartbeat)。

    读取 cache_store.bot_heartbeat 表,检查所有 Bot 的最近心跳时间。
    任一 Bot 心跳过期 → 不健康。
    表不存在时视为健康(未启用 heartbeat 模块)。

    Returns:
        (healthy, error)
    """
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
                return True, None  # 表不存在 → 视为健康
            # 读取所有 bot 的最近心跳
            cursor = conn.execute(
                "SELECT name, last_seen_at FROM bot_heartbeat"
            )
            rows = cursor.fetchall()
            if not rows:
                return True, None  # 无 bot 记录 → 视为健康
            now = time.time()
            stale_bots: list[str] = []
            for name, last_seen in rows:
                try:
                    last_ts = float(last_seen) if last_seen else 0.0
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
            return True, None
        return False, f"Sub-services alive check failed: {e}"
    except Exception as e:
        return False, f"Sub-services alive check failed: {e}"


async def _check_admin_web_port() -> tuple[bool, Optional[str]]:
    """admin_bot 专属: admin web port 监听检查。

    通过 TCP connect 探测 ADMIN_WEB_HOST:ADMIN_WEB_PORT。

    Returns:
        (healthy, error)
    """
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

    检查 Consumer Group 是否存在且有 consumer(复用 _check_redis_stream_consumer)。
    未配置 REDIS_URL 时视为健康。

    Returns:
        (healthy, error)
    """
    return await _check_redis_stream_consumer()


async def _check_writer_inbox_lag() -> tuple[bool, Optional[str]]:
    """db_writer 专属: writer_inbox 延迟检查。

    检查 writer_inbox 表中是否有消息处理延迟超过阈值
    (created_at 早于阈值,但 processed_at 也早于阈值,说明
    消息未被处理且很久未清理)。
    表不存在时视为健康。

    Returns:
        (healthy, error)
    """
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
            return True, None
        return False, f"Writer inbox lag check failed: {e}"
    except Exception as e:
        return False, f"Writer inbox lag check failed: {e}"


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


async def check_readiness(role: str) -> HealthResult:
    """R70 Wave 6: 角色化 readiness 检查 — 主接口。

    根据 SERVICE_ROLE 选择对应的检查项集合,执行真实依赖检查。
    **不允许 mock 真实依赖**(如 DB 不可用必须返回 healthy=false)。
    测试中可通过 monkeypatch 替换 _check_database 等底层函数模拟场景。

    Args:
        role: SERVICE_ROLE 角色名。支持:
            - "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot" /
              "admin_bot" / "db_writer" / "admin" / ""
            - 别名(兼容 entrypoint):"up" / "idx" / "dsp" / "mon"
              (会被规范化为 "up_bot" / "idx_bot" / "dsp_bot" / "mon_bot")
            - 空 → "admin"(全部检查)

    Returns:
        HealthResult 对象:
        - healthy: critical 检查全通过时为 True
        - role: 规范化后的角色名
        - checks: CheckResult 列表(按检查顺序)
        - timestamp: ISO 8601 UTC 时间戳
        - version: HEALTH_VERSION
    """
    canonical_role = _canonicalize_role(role)
    checks: list = []
    bot_token = _get_bot_token_for_role(canonical_role)

    # ── 通用检查:database (critical=True) ──
    checks.append(
        await _run_check("database", critical=True, coro=_check_database())
    )

    # ── 通用检查:redis (non-critical,可能 not_configured) ──
    checks.append(
        await _run_check("redis", critical=False, coro=_check_redis())
    )

    # ── 通用检查:bot_token_valid (仅 bot 角色, non-critical) ──
    if canonical_role in BOT_ROLES:
        checks.append(
            await _run_check(
                "bot_token_valid",
                critical=False,
                coro=_check_bot_token_valid(bot_token),
            )
        )

    # ── 角色专属检查 ──
    if canonical_role == "up_bot":
        checks.append(
            await _run_check(
                "upload_session_status",
                critical=False,
                coro=_check_upload_session_status(),
            )
        )
        checks.append(
            await _run_check(
                "bot_polling_status",
                critical=False,
                coro=_check_bot_polling_status(bot_token),
            )
        )
    elif canonical_role == "idx_bot":
        checks.append(
            await _run_check(
                "index_queue_depth",
                critical=False,
                coro=_check_index_queue_depth(),
            )
        )
    elif canonical_role == "dsp_bot":
        checks.append(
            await _run_check(
                "redis_stream_consumer",
                critical=False,
                coro=_check_redis_stream_consumer(),
            )
        )
        checks.append(
            await _run_check(
                "send_queue_depth",
                critical=False,
                coro=_check_send_queue_depth(),
            )
        )
    elif canonical_role == "mon_bot":
        checks.append(
            await _run_check(
                "sub_services_alive",
                critical=False,
                coro=_check_sub_services_alive(),
            )
        )
    elif canonical_role == "admin_bot":
        checks.append(
            await _run_check(
                "admin_web_port",
                critical=False,
                coro=_check_admin_web_port(),
            )
        )
    elif canonical_role == "db_writer":
        checks.append(
            await _run_check(
                "redis_stream_consumer_group",
                critical=False,
                coro=_check_redis_stream_consumer_group(),
            )
        )
        checks.append(
            await _run_check(
                "writer_inbox_lag",
                critical=False,
                coro=_check_writer_inbox_lag(),
            )
        )
    elif canonical_role == "admin":
        # 全部检查(包含所有角色专属检查)
        checks.append(
            await _run_check(
                "upload_session_status",
                critical=False,
                coro=_check_upload_session_status(),
            )
        )
        checks.append(
            await _run_check(
                "bot_polling_status",
                critical=False,
                coro=_check_bot_polling_status(bot_token),
            )
        )
        checks.append(
            await _run_check(
                "index_queue_depth",
                critical=False,
                coro=_check_index_queue_depth(),
            )
        )
        checks.append(
            await _run_check(
                "redis_stream_consumer",
                critical=False,
                coro=_check_redis_stream_consumer(),
            )
        )
        checks.append(
            await _run_check(
                "send_queue_depth",
                critical=False,
                coro=_check_send_queue_depth(),
            )
        )
        checks.append(
            await _run_check(
                "sub_services_alive",
                critical=False,
                coro=_check_sub_services_alive(),
            )
        )
        checks.append(
            await _run_check(
                "admin_web_port",
                critical=False,
                coro=_check_admin_web_port(),
            )
        )
        checks.append(
            await _run_check(
                "redis_stream_consumer_group",
                critical=False,
                coro=_check_redis_stream_consumer_group(),
            )
        )
        checks.append(
            await _run_check(
                "writer_inbox_lag",
                critical=False,
                coro=_check_writer_inbox_lag(),
            )
        )

    # 整体 healthy:critical 检查全通过
    overall_healthy = all(c.healthy for c in checks if c.critical)

    timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
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
]
