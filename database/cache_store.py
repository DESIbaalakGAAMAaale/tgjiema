"""内存缓存的 SQLite 持久化备份层
- 启动时从 SQLite 恢复到内存 -> VPS 重启后不击穿 CRDB
- 运行时后台定期 dump -> 下次重启有最新的热数据
- WAL 模式支持多进程并发写入
"""
from __future__ import annotations

try:
    import orjson as json
except ImportError:
    import json
import asyncio
import datetime
import os
import sqlite3
import time
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
from loguru import logger

# R47 P0-3: 支持 DATABASE_URL 环境变量指定 SQLite 路径(E2E 测试每个 run 独立 DB)
# R48 P0-3: 修正解析 — sqlite:///tmp/x.db → Path("/tmp/x.db") (绝对路径)
# 之前用 len("sqlite:///") 会吃掉路径首个 /,导致 /tmp/... 被误解析为相对路径 tmp/...
# 改为 len("sqlite://") 保留路径开头的 /,与 CI 断言 str(DB_PATH) == '/tmp/...' 一致
# R48 P0-3: 添加 .resolve() — Windows 上 Path("/tmp/...") 是驱动器相对路径,
# .resolve() 将其解析为绝对路径(如 F:\tmp\...),避免 webServer 子进程驱动器不同导致路径错误
#
# R48 P0-3: 新增相对路径支持 — sqlite://tmp/e2e_local.db (两个斜杠)
# 相对路径基于项目根目录(cache_store.py 的两级父目录),确保 DB 文件在项目目录内,
# 避免 Windows 沙箱/权限限制阻止 webServer 子进程访问系统 /tmp 目录。
# CI(Linux) 仍用 sqlite:///tmp/e2e_local.db (绝对路径);本地 Windows 测试用相对路径。
#
# 格式:
#   sqlite:///path/to/db.db → Path("/path/to/db.db") (绝对路径, Linux CI)
#   sqlite://tmp/e2e.db     → <项目根>/tmp/e2e.db (相对路径, 本地 Windows)
#   sqlite:///F:/path.db    → Path("F:/path.db") (Windows 驱动器绝对路径)
# 未设置或非 sqlite 协议时回退到默认路径 data/cache_store.db
_db_url_env = os.environ.get("DATABASE_URL", "")
if _db_url_env.startswith("sqlite:///"):
    _path_part = _db_url_env[len("sqlite://"):]  # "/tmp/x.db" 或 "/F:/path"
    # Windows 驱动器路径: /F:/path → F:/path (strip leading / before drive letter)
    if len(_path_part) > 2 and _path_part[0] == '/' and _path_part[1].isalpha() and _path_part[2] == ':':
        DB_PATH = Path(_path_part[1:]).resolve()
    else:
        DB_PATH = Path(_path_part).resolve()
elif _db_url_env.startswith("sqlite://"):
    # 相对路径: sqlite://tmp/e2e.db → <项目根>/tmp/e2e.db
    _rel_path = _db_url_env[len("sqlite://"):]
    DB_PATH = (Path(__file__).parent.parent / _rel_path).resolve()
else:
    DB_PATH = Path(__file__).parent.parent / "data" / "cache_store.db"


# ─── R35 P1-2: WriterCommand Protocol(类型提示,不强制现有方法迁移) ───
# 推荐用法: Writer 命令接收 connection 并禁止自行 commit,
# 事务由单一 writer_transaction() 上下文管理器控制。
# 现有 81 处 _in_writer_tx 标志保留兼容,新代码应优先实现此 Protocol。


@runtime_checkable
class WriterCommand(Protocol):
    """Writer 命令接口规范(类型提示用,不强制实现)。

    实现此 Protocol 的命令应:
    1. 接收 connection 参数(由 writer_transaction() 传入)
    2. 禁止自行 commit/rollback(事务由 writer_transaction() 统一控制)
    3. 抛出异常时由 writer_transaction() 自动 ROLLBACK
    """

    async def execute(self, conn: Any, *args: Any, **kwargs: Any) -> Any:
        """在 Writer 事务中执行命令。

        Args:
            conn: aiosqlite.Connection(由 writer_transaction() 传入,
                  处于 BEGIN IMMEDIATE 事务中)
            *args, **kwargs: 命令参数

        Returns:
            命令执行结果

        Raises:
            任意异常(由 writer_transaction() 捕获并 ROLLBACK)
        """
        ...

# 单调递增版本号计数器，替代时间戳避免同一毫秒内多次变更获得相同版本号
_cells_version_counter = int(time.time() * 1000)
# C3: relay 账号池变更版本计数器(独立于 cells,语义隔离)
_relay_version_counter = int(time.time() * 1000)
# 文件记录变更版本计数器(admin_bot 写 → idx_bot/dsp_bot 读,失效内存缓存)
_file_record_version_counter = int(time.time() * 1000)

# ─── JSON 字段反序列化（SQLite 存储为 JSON 字符串，读取时需还原为 Python 对象）───
_JSON_FIELDS_DICT = {"file_types"}  # 期望 dict 的字段
_JSON_FIELDS_LIST = {"backup_channel_msg_ids", "batch_file_meta", "blocked_users"}  # 期望 list 的字段


def _deserialize_sqlite_row(row: dict) -> dict:
    """将 SQLite 行中的 JSON 字符串字段反序列化为 Python 对象。"""
    for key in _JSON_FIELDS_DICT:
        if key in row:
            val = row[key]
            if isinstance(val, str):
                try:
                    row[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    row[key] = {}
            elif val is None:
                row[key] = {}
    for key in _JSON_FIELDS_LIST:
        if key in row:
            val = row[key]
            if isinstance(val, str):
                try:
                    row[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    row[key] = []
            elif val is None:
                row[key] = []
    return row


def _next_cells_version() -> int:
    global _cells_version_counter
    _cells_version_counter += 1
    return _cells_version_counter


# ─── R42 P1-4: dirty_outbox version 单调来源 ───
# 问题: 同 (table, pk) 合并最高 version 时,若多调用方使用默认 version=0,
#   顺序可能依赖插入 ID 并吞掉真正新状态。
# 整改: add_dirty_outbox version=0 时自动从 updated_at 字段生成时间戳版本,
#   保证每个可同步实体使用 monotonic version。
# _TABLE_VERSION_FIELDS: 表名 → 用于生成 version 的时间戳字段名
# (与表的实际 schema 对齐,优先使用 updated_at,无则使用 create_time/created_at)
_TABLE_VERSION_FIELDS: dict[str, str] = {
    "users": "updated_at",
    "file_records": "create_time",
    "codes": "created_at",
    "cells": "updated_at",
    "jobs": "created_at",
    "decode_logs": "request_time",
    "relay_whitelist": "updated_at",
    "collector_whitelist": "updated_at",
    "spare_pool": "updated_at",
    "channels": "updated_at",
}


def _get_table_version_field(table_name: str) -> str:
    """R42 P1-4: 返回该表的 version 字段名。

    用于在 add_dirty_outbox version=0 时从 payload 中提取时间戳生成 version。
    默认返回 'updated_at'(若表无此字段,使用当前时间戳)。

    Args:
        table_name: 逻辑表名(与 dirty_outbox.table_name 对齐)

    Returns:
        该表用于生成 version 的时间戳字段名(如 'updated_at' / 'created_at' / 'create_time')
    """
    return _TABLE_VERSION_FIELDS.get(table_name, "updated_at")


def _generate_version_from_payload(table_name: str, payload) -> int:
    """R42 P1-4: 从 payload 中提取时间戳生成 monotonic version。

    优先级:
        1. payload 中的 version 字段(若有且 > 0)
        2. payload 中 _get_table_version_field() 返回的字段(如 updated_at)
           转为 Unix 时间戳(秒)
        3. 当前 Unix 时间戳(秒,最后兜底)

    Args:
        table_name: 逻辑表名(用于查找 version 字段映射)
        payload: JSON 字符串或 dict(行快照)

    Returns:
        int 类型的 version(Unix 时间戳,秒)
    """
    if not payload:
        return int(time.time())
    try:
        row = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(row, dict):
            # 优先用 payload 中的显式 version 字段
            if "version" in row and row["version"]:
                try:
                    v = int(row["version"])
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
            # 从时间戳字段生成 version
            field = _get_table_version_field(table_name)
            ts_val = row.get(field)
            if ts_val:
                try:
                    if isinstance(ts_val, str):
                        # ISO 格式: 2026-07-13T12:00:00(.ffffff)
                        # 兼容带 Z / 带时区 / 不带时区
                        normalized = ts_val.replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(normalized)
                        return int(dt.timestamp())
                    elif isinstance(ts_val, (int, float)):
                        return int(ts_val)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    # fallback 到当前时间戳(秒)
    return int(time.time())


def _next_relay_version() -> int:
    global _relay_version_counter
    _relay_version_counter += 1
    return _relay_version_counter


def _next_file_record_version() -> int:
    global _file_record_version_counter
    _file_record_version_counter += 1
    return _file_record_version_counter


# ─── M1 业务闭环: JSON 字段序列化辅助函数 ───
# orjson.dumps 返回 bytes,SQLite TEXT 列需解码为 str;
# 标准 json.dumps 返回 str,直接使用。json.loads 兼容两种格式。
def _m1_json_dumps(val) -> str | None:
    """将 Python 对象序列化为 JSON 字符串(orjson 返回 bytes 时解码为 str)。"""
    if val is None:
        return None
    raw = json.dumps(val, default=str)
    return raw.decode() if isinstance(raw, bytes) else raw


def _m1_json_loads(val):
    """将 JSON 字符串反序列化为 Python 对象,空值或解析失败返回 None。"""
    if val is None or val == "":
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


class CacheStore:
    def __init__(self, db_path: str | None = None):
        self._db: aiosqlite.Connection | None = None
        self._in_writer_tx: bool = False  # R34: Writer 事务模式标志
        # R40: 支持测试传入自定义 db_path(不修改全局 DB_PATH)
        self._custom_db_path: str | None = db_path

    async def init(self):
        # R40: 支持测试传入自定义 db_path
        db_path_str = self._custom_db_path if self._custom_db_path else str(DB_PATH)
        if not self._custom_db_path:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = await aiosqlite.connect(db_path_str, timeout=10)
        except (sqlite3.DatabaseError, aiosqlite.Error) as e:
            if "file is not a database" in str(e).lower() and not self._custom_db_path and DB_PATH.exists():
                logger.warning(f"[CacheStore] SQLite 文件已损坏，删除重建: {DB_PATH}")
                DB_PATH.unlink()
                self._db = await aiosqlite.connect(db_path_str, timeout=10)
            else:
                raise
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=15000")  # 多进程并发写,15 秒超时
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")  # WAL 自动 checkpoint
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS cache_backup (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL,
                ts     REAL NOT NULL
            )"""
        )
        # ─── 跨进程通知表：Up Bot 写入 → Idx Bot 感知 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending_notify (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     REAL NOT NULL
            )"""
        )
        # ─── Dsp Bot 通知表：Idx Bot 写入 → Dsp Bot 感知 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS dsp_notify (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     REAL NOT NULL
            )"""
        )
        # ─── Decode Logs 缓冲表 ───
        for sql in DDL_BUFFER_TABLES:
            await self._db.execute(sql)
        # ─── 心跳本地表：Mon Bot 写入，零 CRDB RU ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS heartbeat_local (
                slot_id    TEXT PRIMARY KEY,
                last_ok    REAL NOT NULL,
                fail_streak INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # ─── 跨进程 Bot 心跳表：各 Bot 独立进程写入，admin_bot 读取展示 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS bot_heartbeat (
                name      TEXT PRIMARY KEY,
                last_ping REAL NOT NULL,
                is_running INTEGER NOT NULL DEFAULT 1,
                total_processed INTEGER NOT NULL DEFAULT 0,
                total_errors INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # ─── 用户配额本地表：Idx Bot 读写零 RU，每 6h 同步 CRDB ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS user_quota (
                user_id            INTEGER PRIMARY KEY,
                level              TEXT NOT NULL DEFAULT 'free',
                daily_quota        INTEGER NOT NULL DEFAULT 3,
                used_today         INTEGER NOT NULL DEFAULT 0,
                quota_date         TEXT,
                ext_quota          INTEGER NOT NULL DEFAULT 0,
                ext_used_today     INTEGER NOT NULL DEFAULT 0,
                ext_quota_date     TEXT,
                synced_at          REAL NOT NULL DEFAULT 0
            )"""
        )
        # ─── D: 本地任务队列 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS local_job_queue (
                crdb_id       INTEGER PRIMARY KEY,
                code          TEXT NOT NULL,
                target_user_id INTEGER NOT NULL,
                storage_channel_id INTEGER NOT NULL,
                storage_msg_ids TEXT,
                batch_file_meta TEXT,
                task_type     TEXT DEFAULT 'single',
                status        TEXT DEFAULT 'pending',
                retry_count   INTEGER DEFAULT 0,
                protect_content BOOLEAN DEFAULT 0,
                created_at    TEXT,
                dispatched_at TEXT,
                dead_reason   TEXT,
                dead_retry_count INTEGER DEFAULT 0,
                synced_at     REAL DEFAULT 0,
                group_id      INTEGER DEFAULT 0,
                file_unique_id TEXT DEFAULT '',
                media_group_id TEXT DEFAULT ''
            )"""
        )
        try:
            await self._db.execute("ALTER TABLE local_job_queue ADD COLUMN dead_retry_count INTEGER DEFAULT 0")
        except Exception as e:
            logger.warning(f"[CacheStore] ALTER TABLE失败(非预期): {e}")
        # R36 B0-1: 幂等添加 ReplicaAwareResolver 所需的结构化字段
        # 旧表升级时新增 3 列;新表已包含则忽略重复列错误
        for _alter_sql in (
            "ALTER TABLE local_job_queue ADD COLUMN group_id INTEGER DEFAULT 0",
            "ALTER TABLE local_job_queue ADD COLUMN file_unique_id TEXT DEFAULT ''",
            "ALTER TABLE local_job_queue ADD COLUMN media_group_id TEXT DEFAULT ''",
        ):
            try:
                await self._db.execute(_alter_sql)
            except Exception as e:
                # SQLite 重复列错误是预期的(幂等升级),仅 debug 记录
                logger.debug(f"[CacheStore] ALTER TABLE 升级(可忽略): {e}")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_job_pending ON local_job_queue(status, created_at)"
        )
        # ─── F5: 启动统计快照 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS counter_snapshot (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL,
                ts    REAL NOT NULL
            )"""
        )
        # ─── E1: cells 跨进程快照 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS cells_snapshot (
                id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                data       TEXT NOT NULL,
                version    INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS cells_change_notify (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                ts      REAL NOT NULL
            )"""
        )
        # ─── C3: relay 账号池跨进程变更通知表(admin_bot 写 → idx_bot 读)───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS relay_change_notify (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                ts      REAL NOT NULL
            )"""
        )
        # ─── 文件记录变更跨进程通知表(admin_bot 写 → idx_bot/dsp_bot 读)───
        # admin_bot 处理举报(脱钩/封禁/限制)后写入,idx_bot/dsp_bot 检测到变更后失效内存缓存
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS file_record_change_notify (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                version      INTEGER NOT NULL,
                change_type  TEXT NOT NULL,
                record_key   TEXT NOT NULL,
                ts           REAL NOT NULL
            )"""
        )
        # ─── E2: cells 本地逐行存储(热路径零CRDB,仅异常事件同步CRDB) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS cells_local (
                slot_id TEXT PRIMARY KEY,
                channel_id BIGINT NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'shadow1',
                next_active_chat_id BIGINT,
                prev_slot_id TEXT,
                demoted_to_channel_id BIGINT,
                account_name TEXT DEFAULT '',
                is_r100 INTEGER DEFAULT 0,
                last_heartbeat TEXT,
                last_synced_msg_id BIGINT DEFAULT 0,
                degrade_count INTEGER DEFAULT 0,
                file_count INTEGER DEFAULT 0,
                rotation_started_at TEXT,
                updated_at REAL NOT NULL,
                deleted_at TEXT,
                crdb_synced INTEGER DEFAULT 1
            )"""
        )
        # R38 P1-3: cells_local 补 deleted_at 列(tombstone soft-delete)
        try:
            await self._db.execute(
                "ALTER TABLE cells_local ADD COLUMN deleted_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] cells_local ADD deleted_at (幂等,可忽略): {e}")
        # PRE-04 / PRE-01: 为已存在的数据库补字段（CRDB/SQLite 不支持 ADD COLUMN IF NOT EXISTS，用 try-except 兼容）
        for _col_ddl in [
            "ALTER TABLE cells_local ADD COLUMN prev_slot_id TEXT",
            "ALTER TABLE cells_local ADD COLUMN demoted_to_channel_id BIGINT",
        ]:
            try:
                await self._db.execute(_col_ddl)
            except Exception as e:
                logger.warning(f"[CacheStore] ALTER TABLE失败(非预期): {e}")
        # M2: cells CAS/fencing 字段 — 防止双控制面(Mon/Dsp)并发改写同一 cell
        # topology_version 每次成功 CAS 转换递增,作为 fencing token;lease_* 实现租约互斥;
        # transition_id 标识当前进行中的转换事务(UUID),便于跨进程追溯。
        for _col_name, _col_type in [
            ("topology_version", "INTEGER DEFAULT 0"),   # 拓扑版本号,每次 CAS 转换递增
            ("lease_owner", "TEXT DEFAULT ''"),           # 当前租约持有者(如 'mon_bot', 'dsp_bot')
            ("lease_until", "REAL DEFAULT 0"),            # 租约到期时间戳
            ("transition_id", "TEXT DEFAULT ''"),         # 当前转换事务 ID(UUID)
        ]:
            try:
                await self._db.execute(
                    f"ALTER TABLE cells_local ADD COLUMN {_col_name} {_col_type}"
                )
            except Exception as e:
                logger.debug(f"[CacheStore] cells_local ADD {_col_name} (幂等,可忽略): {e}")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cells_local_status ON cells_local(status)"
        )
        # ─── Manifest 表：Manifest 驱动的副本同步（免 Telethon 读历史）───
        # 记录每个文件(file_unique_id)在每个频道的 message_id,补位时按 manifest 从存活频道 copyMessages
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS manifest (
                group_id        INTEGER NOT NULL,
                file_unique_id  TEXT NOT NULL,
                channel_id      BIGINT NOT NULL,
                message_id      BIGINT NOT NULL,
                media_type      TEXT,
                media_group_id  TEXT,
                first_seen_at   TEXT,
                PRIMARY KEY (group_id, file_unique_id, channel_id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifest_group ON manifest(group_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifest_channel ON manifest(channel_id)"
        )
        # 为已存在的 manifest 表补 media_group_id 列(幂等,重复执行报错可忽略)
        try:
            await self._db.execute("ALTER TABLE manifest ADD COLUMN media_group_id TEXT")
        except Exception as e:
            logger.warning(f"[CacheStore] ALTER TABLE manifest失败(幂等,可忽略): {e}")
        # ─── KV 键值存储：用于缓存 DDL 版本等配置 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS kv_store (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        # ─── C2: 通用 TTL 缓存(跨进程共享,JSON 序列化) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS ttl_cache (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        # ─── 用户 Bot 启动状态跟踪（跨进程共享）───
        # 用户向 idx/dsp 发送 /start 后写入，up/idx/dsp 发送消息前检查
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS user_bot_started (
                user_id    BIGINT NOT NULL,
                bot_name   TEXT NOT NULL,
                started_at REAL NOT NULL,
                PRIMARY KEY (user_id, bot_name)
            )"""
        )
        # ─── 待发送文件码（用户未 /start idx 时暂存）───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending_file_codes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    BIGINT NOT NULL,
                file_code  TEXT NOT NULL,
                note       TEXT DEFAULT '',
                ext_code   TEXT DEFAULT '',
                created_at REAL NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_file_codes_user ON pending_file_codes(user_id)"
        )
        # ─── 热路径全表缓存：file_records / codes / users / external_code_mapping ───
        # 启动时从 CRDB 全量加载，后续所有读操作走 SQLite（0 CRDB RU）
        # 写操作双写：先写 SQLite(标记 dirty)，CRDB 异步/批量同步
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS file_records_local (
                file_code            TEXT PRIMARY KEY,
                uploader_id          BIGINT,
                primary_channel_id   BIGINT,
                primary_channel_msg_id BIGINT,
                file_types           TEXT,
                backup_channel_msg_ids TEXT,
                batch_msg_ids        TEXT,
                batch_file_meta      TEXT,
                file_ids             TEXT,
                status               TEXT DEFAULT 'active',
                request_count        INTEGER DEFAULT 0,
                protect_content      INTEGER DEFAULT 0,
                file_ttl_days        INTEGER DEFAULT 0,
                note                 TEXT DEFAULT '',
                expire_time          TEXT,
                blocked_users        TEXT DEFAULT '[]',
                create_time          TEXT,
                updated_at           TEXT,
                max_requests         INTEGER DEFAULT 0,
                is_collection        INTEGER DEFAULT 0,
                collection_codes     TEXT DEFAULT '[]',
                deleted_at           TEXT,
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
        # R38 P1-3: 为已存在的 file_records_local 补 deleted_at 列(tombstone soft-delete)
        try:
            await self._db.execute(
                "ALTER TABLE file_records_local ADD COLUMN deleted_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] file_records_local ADD deleted_at (幂等,可忽略): {e}")
        # 为已存在的 file_records_local 补字段（SQLite 不支持 ADD COLUMN IF NOT EXISTS，用 try-except 兼容）
        try:
            await self._db.execute("ALTER TABLE file_records_local ADD COLUMN max_requests INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug(f"[CacheStore] file_records_local ADD max_requests (幂等,可忽略): {e}")
        # 合集码字段：is_collection 标记是否为合集,collection_codes 存储 JSON 数组
        try:
            await self._db.execute("ALTER TABLE file_records_local ADD COLUMN is_collection INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug(f"[CacheStore] file_records_local ADD is_collection (幂等,可忽略): {e}")
        try:
            await self._db.execute("ALTER TABLE file_records_local ADD COLUMN collection_codes TEXT DEFAULT '[]'")
        except Exception as e:
            logger.debug(f"[CacheStore] file_records_local ADD collection_codes (幂等,可忽略): {e}")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS codes_local (
                code                 TEXT PRIMARY KEY,
                file_record_code     TEXT,
                uploader_id          BIGINT,
                file_types           TEXT,
                batch_msg_ids        TEXT,
                batch_file_meta      TEXT,
                primary_channel_id   BIGINT,
                status               TEXT DEFAULT 'active',
                created_at           TEXT,
                expire_time          TEXT,
                note                 TEXT DEFAULT '',
                deleted_at           TEXT,
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
        # R38 P1-3: codes_local 补 deleted_at 列(tombstone soft-delete)
        try:
            await self._db.execute(
                "ALTER TABLE codes_local ADD COLUMN deleted_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] codes_local ADD deleted_at (幂等,可忽略): {e}")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS users_local (
                user_id              BIGINT PRIMARY KEY,
                username             TEXT,
                first_name           TEXT,
                membership_level     TEXT DEFAULT 'free',
                daily_decode_quota   INTEGER DEFAULT 3,
                quota_used_today     INTEGER DEFAULT 0,
                quota_date           TEXT,
                can_upload           INTEGER DEFAULT 0,
                external_decode_quota INTEGER DEFAULT 0,
                external_used_today  INTEGER DEFAULT 0,
                external_quota_date  TEXT,
                is_banned            INTEGER DEFAULT 0,
                created_at           TEXT,
                updated_at           TEXT,
                deleted_at           TEXT,
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
        # R38 P1-3: users_local 补 deleted_at 列(tombstone soft-delete)
        try:
            await self._db.execute(
                "ALTER TABLE users_local ADD COLUMN deleted_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] users_local ADD deleted_at (幂等,可忽略): {e}")
        # R40 P1-11: users_local 补 ban_expires_at 列(临时封禁自动解封依据)
        # NULL=永久封禁;非空 ISO 时间=临时封禁到期时间。
        try:
            await self._db.execute(
                "ALTER TABLE users_local ADD COLUMN ban_expires_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] users_local ADD ban_expires_at (幂等,可忽略): {e}")
        # R40 P2-9: users_local 补 locale 列(用户语言偏好,默认 zh-CN)
        # 用于 i18n 翻译查找与 Bot 多语言回复
        try:
            await self._db.execute(
                "ALTER TABLE users_local ADD COLUMN locale TEXT DEFAULT 'zh-CN'"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] users_local ADD locale (幂等,可忽略): {e}")
        # R53 P0-5: users_local 补 version 列(CAS 乐观锁)
        # production 环境强制 expected_version,_set_user_plan_internal 通过
        # version 列实现 Compare-And-Swap 防止 lost update
        try:
            await self._db.execute(
                "ALTER TABLE users_local ADD COLUMN version INTEGER DEFAULT 0"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] users_local ADD version (幂等,可忽略): {e}")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS external_code_mapping_local (
                external_code        TEXT PRIMARY KEY,
                system_code          TEXT NOT NULL,
                bot_username         TEXT,
                created_at           TEXT,
                updated_at           TEXT,
                deleted_at           TEXT,
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
        # R38 P1-3: external_code_mapping_local 补 deleted_at 列(tombstone soft-delete)
        try:
            await self._db.execute(
                "ALTER TABLE external_code_mapping_local ADD COLUMN deleted_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] external_code_mapping_local ADD deleted_at (幂等,可忽略): {e}")
        # R33: Writer 幂等表 — 每条消息的 message_id 记录在此表,
        # db_writer 崩溃恢复后通过此表跳过已处理的消息,实现 exactly-once
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS writer_inbox (
                message_id   TEXT PRIMARY KEY,
                method_name  TEXT NOT NULL,
                stream_id    TEXT,
                created_at   REAL NOT NULL,
                processed_at REAL NOT NULL
            )"""
        )
        # ════════════════════════════════════════════════════════════════
        # M1 业务闭环: 5 张新表 — upload_sessions / upload_outbox /
        #   quota_ledger / delivery_receipts / replication_tasks
        # 与旧表共存,渐进式迁移。所有新表 IF NOT EXISTS 幂等建表。
        # ════════════════════════════════════════════════════════════════

        # ─── M1-1: upload_sessions 上传会话状态机 ───
        # 状态机: RECEIVED → COPIED_PRIMARY → MANIFESTED →
        #         OPTIONS_PENDING → INDEX_PENDING → READY / ABORTED / EXPIRED
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS upload_sessions (
                upload_id          TEXT PRIMARY KEY,
                user_id            BIGINT NOT NULL,
                source_msg_ids     TEXT,
                primary_channel_id BIGINT,
                primary_msg_ids    TEXT,
                media_group_id     TEXT,
                options_json       TEXT,
                trace_id           TEXT,
                status             TEXT NOT NULL DEFAULT 'RECEIVED',
                prev_status        TEXT,
                transitioned_at    REAL,
                transition_reason  TEXT,
                lease_owner        TEXT,
                lease_until        REAL,
                last_error         TEXT,
                created_at         REAL NOT NULL,
                updated_at         REAL NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_sessions_status ON upload_sessions(status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_sessions_user ON upload_sessions(user_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_sessions_lease ON upload_sessions(lease_until)"
        )

        # ─── M1-2: upload_outbox 事务发件箱(派工任务) ───
        # 状态机: PENDING → DISPATCHED → DONE / FAILED
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS upload_outbox (
                outbox_id          TEXT PRIMARY KEY,
                upload_id          TEXT,
                job_id             INTEGER,
                code               TEXT NOT NULL,
                target_user_id     BIGINT NOT NULL,
                storage_channel_id BIGINT NOT NULL,
                storage_msg_ids    TEXT,
                batch_file_meta    TEXT,
                task_type           TEXT DEFAULT 'single',
                protect_content    INTEGER DEFAULT 0,
                event_type         TEXT NOT NULL DEFAULT 'delivery_requested',
                status             TEXT NOT NULL DEFAULT 'PENDING',
                attempts           INTEGER DEFAULT 0,
                next_retry_at      REAL,
                created_at         REAL NOT NULL,
                processed_at       REAL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_outbox_status ON upload_outbox(status, next_retry_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_outbox_upload ON upload_outbox(upload_id)"
        )
        # R36 B0-2: 幂等添加 lease_owner / lease_until 字段(支持 OutboxWorker CAS claim)
        # 旧表升级时新增 2 列;新表已包含则忽略重复列错误
        for _alter_sql in (
            "ALTER TABLE upload_outbox ADD COLUMN lease_owner TEXT",
            "ALTER TABLE upload_outbox ADD COLUMN lease_until REAL",
        ):
            try:
                await self._db.execute(_alter_sql)
            except Exception as e:
                # SQLite 重复列错误是预期的(幂等升级),仅 debug 记录
                logger.debug(f"[CacheStore] ALTER TABLE upload_outbox 升级(可忽略): {e}")

        # ─── M1-3: quota_ledger 配额变更流水(追加式日志) ───
        # event_type: consume/refund/sync/reset/expire
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS quota_ledger (
                ledger_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      BIGINT NOT NULL,
                event_type   TEXT NOT NULL,
                is_external  INTEGER DEFAULT 0,
                quota_before INTEGER,
                quota_after  INTEGER,
                request_id   TEXT,
                reason       TEXT,
                created_at   REAL NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_ledger_user ON quota_ledger(user_id, created_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_ledger_request ON quota_ledger(request_id)"
        )

        # ─── M1-4: delivery_receipts 投递回执(替代 _sent_msg_tracker 内存态) ───
        # 状态: SENT → CONFIRMED / FAILED / PARTIAL
        # R37 P2-5: 新增 delivery_token 列(effectively-once 幂等)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS delivery_receipts (
                receipt_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id             INTEGER NOT NULL,
                source_msg_id      BIGINT NOT NULL,
                target_user_id     BIGINT NOT NULL,
                sent_msg_id        BIGINT,
                media_group_id     TEXT,
                group_receipt_id   TEXT,
                status             TEXT NOT NULL DEFAULT 'SENT',
                attempts           INTEGER DEFAULT 0,
                error_reason       TEXT,
                created_at         REAL NOT NULL,
                confirmed_at       REAL,
                delivery_token     TEXT,
                UNIQUE(job_id, source_msg_id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_job ON delivery_receipts(job_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_target ON delivery_receipts(target_user_id)"
        )
        # R37 P2-5: delivery_token 索引(快速查询 token 是否已存在)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_token ON delivery_receipts(delivery_token)"
        )
        # 幂等迁移: 为已存在的表补充 delivery_token 列(CREATE TABLE IF NOT EXISTS 不会添加新列)
        try:
            await self._db.execute(
                "ALTER TABLE delivery_receipts ADD COLUMN delivery_token TEXT"
            )
        except Exception as _e:
            # 列已存在时 SQLite 抛 OperationalError "duplicate column",属正常,可忽略
            if "duplicate column" not in str(_e).lower():
                logger.warning(f"[CacheStore] ALTER TABLE delivery_receipts 失败(非预期): {_e}")

        # ─── M1-5: replication_tasks 副本复制任务 ───
        # 状态机: PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED / FAILED
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS replication_tasks (
                task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id       INTEGER NOT NULL,
                file_unique_id TEXT NOT NULL,
                src_channel_id BIGINT NOT NULL,
                dst_channel_id BIGINT NOT NULL,
                src_msg_id     BIGINT NOT NULL,
                dst_msg_id     BIGINT,
                media_group_id TEXT,
                task_type      TEXT DEFAULT 'replica',
                priority       INTEGER DEFAULT 5,
                status         TEXT NOT NULL DEFAULT 'PLANNED',
                prev_status    TEXT,
                attempts       INTEGER DEFAULT 0,
                max_attempts   INTEGER DEFAULT 3,
                next_retry_at  REAL,
                last_error     TEXT,
                created_at     REAL NOT NULL,
                updated_at     REAL NOT NULL,
                committed_at   REAL,
                UNIQUE(group_id, file_unique_id, src_channel_id, dst_channel_id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_replication_tasks_status ON replication_tasks(status, priority)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_replication_tasks_group ON replication_tasks(group_id)"
        )

        # ─── R38 P1-2: 统一 dirty_outbox,所有 Bot 只写本地事务,
        # crdb_sync 批量 UPSERT/tombstone。替代分散的 *_local.crdb_synced=0 标记模式。 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS dirty_outbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name  TEXT NOT NULL,
                pk          TEXT NOT NULL,
                version     INTEGER DEFAULT 0,
                operation   TEXT DEFAULT 'upsert',
                payload     TEXT,
                created_at  TEXT,
                processed   INTEGER DEFAULT 0,
                local_only  INTEGER DEFAULT 0
            )"""
        )
        # R40 P0-5: 为已存在的 dirty_outbox 表添加 local_only 列(幂等)
        try:
            await self._db.execute(
                "ALTER TABLE dirty_outbox ADD COLUMN local_only INTEGER DEFAULT 0"
            )
        except Exception:
            pass  # 列已存在,忽略
        # R51 P0-9: 为 dirty_outbox 表添加 last_error + next_retry_at 列(指数退避)
        # DLQ 写入失败时记录错误信息并设置下次重试时间,支持指数退避重试
        try:
            await self._db.execute(
                "ALTER TABLE dirty_outbox ADD COLUMN last_error TEXT"
            )
        except Exception:
            pass  # 列已存在,忽略
        try:
            await self._db.execute(
                "ALTER TABLE dirty_outbox ADD COLUMN next_retry_at TEXT"
            )
        except Exception:
            pass  # 列已存在,忽略
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dirty_outbox_unprocessed ON dirty_outbox(processed)"
        )
        # R48 P0-5: dirty_outbox 增加 UNIQUE 约束 (table_name, pk, version)
        # 防止并发写入时 (table, pk, version) 重复,配合 allocate_version 原子递增使用。
        # 老库可能存在历史重复行,R48 要求 fail-fast(不允许只 warning 后继续):
        #   1. 先扫描重复行并归档到 migration_conflicts(保留权威记录)
        #   2. 创建 UNIQUE INDEX IF NOT EXISTS
        #   3. PRAGMA 验证 index 存在且为 UNIQUE,失败则 raise RuntimeError 拒绝启动
        # (R47 只 warning 不阻塞的旧逻辑已废弃,违反 fail-fast 要求)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS migration_conflicts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name    TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                record_id     INTEGER NOT NULL,
                record_data   TEXT NOT NULL,
                resolved_at   TEXT,
                created_at    TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_migration_conflicts_unresolved "
            "ON migration_conflicts(resolved_at) WHERE resolved_at IS NULL"
        )
        # 归档并清理 dirty_outbox 历史重复行(保留 created_at 最新/id 最大者为权威)
        # R49 P0-5: 完整 7 步迁移方案(preflight → 选定权威 → 归档 → 删除 →
        #          创建索引 → PRAGMA 验证 → fail-closed),替代 R48 的 3 步序列。
        # 旧方法 _archive_conflicts_to_migration_conflicts / _verify_unique_constraint_or_fail
        # 保留供 R48 测试使用,但 init() 现在统一调用 7 步迁移方法。
        await self._migrate_dirty_outbox_unique_constraint()

        # ─── R41 P0-6: dlq_records 死信队列权威存储表 ───
        # crdb_sync 处理失败的 dirty_outbox 记录路由到此表(替代/补充 jsonl 文件)。
        # 字段: status / retry_count / max_retries / next_retry_at / last_error /
        #       created_at / updated_at / message_id(去重键) / table_name / reason / original
        # 状态机: pending(可重试) → retrying(重试中) → done(成功) /
        #         permanently_failed(达到 max_retries,停止重试)
        # cleanup_dlq() 周期性将 retry_count >= max_retries 的记录标记为 permanently_failed。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS dlq_records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id     TEXT NOT NULL,
                table_name     TEXT NOT NULL,
                reason         TEXT,
                status         TEXT NOT NULL DEFAULT 'pending',
                retry_count    INTEGER NOT NULL DEFAULT 0,
                max_retries    INTEGER NOT NULL DEFAULT 5,
                next_retry_at  TEXT,
                last_error     TEXT,
                original       TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dlq_records_status ON dlq_records(status, next_retry_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dlq_records_table ON dlq_records(table_name)"
        )
        # 幂等迁移: 为已存在的 dlq_records 补充列(可忽略 duplicate column 错误)
        for _col_ddl_p0_6 in [
            "ALTER TABLE dlq_records ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE dlq_records ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 5",
            "ALTER TABLE dlq_records ADD COLUMN next_retry_at TEXT",
            "ALTER TABLE dlq_records ADD COLUMN last_error TEXT",
        ]:
            try:
                await self._db.execute(_col_ddl_p0_6)
            except Exception:
                pass  # 列已存在,忽略

        # ─── R40 P0-4: pending_uploads SQLite 本地权威表 ───
        # Up Bot 双写(CRDB + SQLite local),Idx Bot 仅从 SQLite 读取,
        # 消除 Idx Bot 对 CRDB 凭证的依赖(无 CRDB 凭证时主循环仍可工作)。
        # processed 状态机: 0=pending(待处理), 1=completed(已完成), 2=claimed(认领中,处理中)
        # claimed_at: 认领时间戳(0=未认领),用于 CAS 重领(超过 _CLAIM_TIMEOUT 视为崩溃可重领)
        # dead_reason + dead_count: 失败诊断信息(失败次数 + 原因)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending_uploads_local (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uploader_id BIGINT,
                primary_channel_id BIGINT,
                primary_channel_msg_id BIGINT,
                file_types  TEXT,
                batch_msg_ids TEXT,
                batch_file_meta TEXT,
                status_msg_id BIGINT DEFAULT 0,
                created_at  TEXT,
                processed   INTEGER DEFAULT 0,
                claimed_at  REAL DEFAULT 0,
                note        TEXT DEFAULT '',
                protect_content INTEGER DEFAULT 0,
                file_ttl_days INTEGER DEFAULT 0,
                upload_id   TEXT DEFAULT '',
                dead_reason TEXT DEFAULT '',
                dead_count  INTEGER DEFAULT 0,
                crdb_id     INTEGER DEFAULT 0,
                crdb_synced INTEGER DEFAULT 1
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_uploads_unprocessed ON pending_uploads_local(processed, claimed_at)"
        )
        # 为已存在的 pending_uploads_local 补字段(幂等,重复执行报错可忽略)
        for _col_ddl_p0_4 in [
            "ALTER TABLE pending_uploads_local ADD COLUMN dead_reason TEXT DEFAULT ''",
            "ALTER TABLE pending_uploads_local ADD COLUMN dead_count INTEGER DEFAULT 0",
            "ALTER TABLE pending_uploads_local ADD COLUMN upload_id TEXT DEFAULT ''",
            "ALTER TABLE pending_uploads_local ADD COLUMN crdb_id INTEGER DEFAULT 0",
            "ALTER TABLE pending_uploads_local ADD COLUMN crdb_synced INTEGER DEFAULT 1",
        ]:
            try:
                await self._db.execute(_col_ddl_p0_4)
            except Exception as _e_p0_4:
                logger.debug(f"[CacheStore] pending_uploads_local ALTER 升级(可忽略): {_e_p0_4}")

        # ─── R40: 统一任务中心(tasks) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type   TEXT NOT NULL,
                user_id     INTEGER NOT NULL,
                status      TEXT DEFAULT 'pending',
                progress    INTEGER DEFAULT 0,
                eta_seconds INTEGER DEFAULT 0,
                payload     TEXT,
                result      TEXT,
                error       TEXT,
                trace_id    TEXT DEFAULT '',
                created_at  TEXT,
                updated_at  TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_type_created ON tasks(task_type, created_at)"
        )

        # ─── R40: 文件集合(collections) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS collections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                code         TEXT UNIQUE,
                owner_id     INTEGER NOT NULL,
                description  TEXT DEFAULT '',
                version      INTEGER DEFAULT 1,
                item_count   INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'active',
                created_at   TEXT,
                updated_at   TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collections_owner ON collections(owner_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collections_code ON collections(code)"
        )

        # ─── R40: 集合项目(collection_items) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS collection_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL,
                file_code     TEXT NOT NULL,
                added_at      TEXT,
                FOREIGN KEY (collection_id) REFERENCES collections(id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_items_coll ON collection_items(collection_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_items_code ON collection_items(file_code)"
        )

        # ─── R40: 通知(notifications) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                type       TEXT NOT NULL,
                payload    TEXT,
                is_read    INTEGER DEFAULT 0,
                created_at TEXT,
                read_at    TEXT
            )"""
        )
        # R40 P2-4: 旧库补 read_at 列(用于 Prometheus 通知投递延迟指标)
        try:
            await self._db.execute(
                "ALTER TABLE notifications ADD COLUMN read_at TEXT"
            )
        except Exception as e:
            logger.debug(f"[CacheStore] notifications ADD read_at (幂等,可忽略): {e}")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read)"
        )

        # ─── R45/R51 P0-5: 通知投递 outbox(notification_outbox) ───
        # R45 第 16 节: 通知先写 outbox(pending),由各 Bot 异步投递
        # R51 P0-5: 新增 window_start 列 + 唯一约束,防并发重复插入
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS notification_outbox (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                notif_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                notif_type   TEXT NOT NULL,
                dedup_key    TEXT DEFAULT '',
                window_start TEXT,                 -- R51 P0-5: 去重窗口起始时间(整点对齐)
                payload      TEXT,
                status       TEXT DEFAULT 'pending',
                attempts     INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error   TEXT DEFAULT '',
                created_at   TEXT,
                delivered_at TEXT,
                updated_at   TEXT
            )"""
        )
        # R51 P0-5: 旧库补 window_start 列(幂等,已存在则忽略)
        try:
            await self._db.execute(
                "ALTER TABLE notification_outbox ADD COLUMN window_start TEXT"
            )
        except Exception as e:
            logger.debug(
                f"[CacheStore] notification_outbox ADD window_start (幂等,可忽略): {e}"
            )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notif_outbox_status "
            "ON notification_outbox(status, created_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notif_outbox_user "
            "ON notification_outbox(user_id, status)"
        )
        # R51 P0-5: (user_id, dedup_key, window_start) 唯一约束 — 防并发重复投递
        # 仅对 dedup_key 非空且非空字符串的记录生效(部分索引)
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedup "
            "ON notification_outbox(user_id, dedup_key, window_start) "
            "WHERE dedup_key IS NOT NULL AND dedup_key != ''"
        )
        # R45: 通知投递回执(notification_receipts)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS notification_receipts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                notif_id     INTEGER NOT NULL,
                outbox_id    INTEGER,
                user_id      INTEGER NOT NULL,
                channel      TEXT,
                status       TEXT NOT NULL,
                error        TEXT DEFAULT '',
                delivered_at TEXT,
                created_at   TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_receipts_notif "
            "ON notification_receipts(notif_id)"
        )

        # ─── R40: 内容举报(content_reports) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS content_reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id   INTEGER NOT NULL,
                target_type   TEXT NOT NULL,
                target_id     TEXT NOT NULL,
                reason        TEXT NOT NULL,
                description   TEXT DEFAULT '',
                status        TEXT DEFAULT 'pending',
                appeal_text   TEXT DEFAULT '',
                appealed_at   TEXT,
                resolved_by   INTEGER,
                resolved_at   TEXT,
                created_at    TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_reports_status ON content_reports(status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_reports_target ON content_reports(target_type, target_id)"
        )

        # ─── R40: 审计日志(audit_log) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id    INTEGER NOT NULL,
                actor_type  TEXT DEFAULT 'admin',
                action      TEXT NOT NULL,
                target_type TEXT,
                target_id   TEXT,
                details     TEXT,
                ip_addr     TEXT DEFAULT '',
                created_at  TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)"
        )

        # ─── R40: 配额预留(quota_reservations) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS quota_reservations (
                id              TEXT PRIMARY KEY,
                user_id         INTEGER NOT NULL,
                amount          INTEGER NOT NULL,
                reason          TEXT NOT NULL,
                status          TEXT DEFAULT 'reserved',
                actual_amount   INTEGER DEFAULT 0,
                created_at      TEXT,
                settled_at      TEXT,
                expired_at      TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_reservations_user ON quota_reservations(user_id, status)"
        )

        # ─── R40: RBAC 角色(rbac_roles) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS rbac_roles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                permissions TEXT DEFAULT '[]',
                created_at  TEXT
            )"""
        )

        # ─── R40: RBAC 用户角色(rbac_user_roles) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS rbac_user_roles (
                user_id     INTEGER PRIMARY KEY,
                role_id     INTEGER NOT NULL,
                assigned_at TEXT,
                assigned_by INTEGER,
                FOREIGN KEY (role_id) REFERENCES rbac_roles(id)
            )"""
        )

        # ─── R40: 审批流(approvals) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS approvals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                action        TEXT NOT NULL,
                payload       TEXT,
                status        TEXT DEFAULT 'pending',
                approver_id   INTEGER,
                approver_note TEXT DEFAULT '',
                created_by    INTEGER NOT NULL,
                created_at    TEXT,
                resolved_at   TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)"
        )

        # ─── R41 P0-4: command_outbox(审批通过后异步执行的高风险命令) ───
        # approve() 审批通过后,将高风险命令写入此表(独立事务),
        # ApprovalExecutor 异步消费并执行 handler,避免在 approve 事务内
        # 嵌套 BEGIN(mark_executing/mark_executed 各自开启事务)。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS command_outbox (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id     TEXT NOT NULL UNIQUE,
                approval_id   INTEGER NOT NULL,
                command_type  TEXT NOT NULL,
                payload       TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                retry_count   INTEGER NOT NULL DEFAULT 0,
                max_retries   INTEGER NOT NULL DEFAULT 3,
                next_retry_at TEXT,
                last_error    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_command_outbox_status ON command_outbox(status)"
        )
        # R51 P0-7: UNIQUE(approval_id, action_id) — 防止 approve() 重试或补偿 worker
        # 重复写入 command_outbox 条目,支持幂等补偿(UNIQUE 冲突即视为已写入,跳过即可)。
        # 注意: action_id 列本身已有 UNIQUE 约束,此复合唯一索引作为防御性冗余 +
        # 显式语义标识(approval_id, action_id) 二元组的唯一性。
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_command_outbox_approval_action "
            "ON command_outbox(approval_id, action_id) "
            "WHERE approval_id IS NOT NULL"
        )

        # ─── R40: 维护模式状态(maintenance_state) ───
        # R42 P1-12: 新增 recover_status 字段(workflow 失败时设为 'pending',
        # 强制 recover_maintenance 审批才能 disable)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS maintenance_state (
                id            INTEGER PRIMARY KEY CHECK(id = 1),
                enabled       INTEGER DEFAULT 0,
                reason        TEXT DEFAULT '',
                started_by    INTEGER,
                started_at    TEXT,
                ended_at      TEXT,
                recover_status TEXT DEFAULT 'completed'
            )"""
        )
        # R42 P1-12: 旧表升级 — 幂等添加 recover_status 列(已存在则忽略)
        try:
            await self._db.execute(
                "ALTER TABLE maintenance_state ADD COLUMN recover_status TEXT DEFAULT 'completed'"
            )
        except Exception as _alter_e:
            # SQLite 重复列错误是预期的(幂等升级),仅 debug 记录
            logger.debug(f"[CacheStore] maintenance_state ALTER TABLE 升级(可忽略): {_alter_e}")

        # ─── R40: 管理员访问日志(admin_access_log) ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS admin_access_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER NOT NULL,
                action      TEXT NOT NULL,
                target_type TEXT,
                target_id   TEXT,
                details     TEXT,
                ip_addr     TEXT DEFAULT '',
                created_at  TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_access_log_admin ON admin_access_log(admin_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_access_log_created ON admin_access_log(created_at)"
        )

        # ─── R42 P0-3: 管理员持久化身份表(admin_principals) ───
        # 替代 username hash 生成 ID 的方式,显式配置 ADMIN_PRINCIPAL_ID 并原子分配角色。
        # bootstrap_admin_principal() 在单事务中 UPSERT 记录 + 分配角色 + 写审计日志。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS admin_principals (
                id            INTEGER PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT DEFAULT '',
                roles         TEXT DEFAULT '[]',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT,
                updated_at    TEXT
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_principals_username ON admin_principals(username)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_principals_active ON admin_principals(is_active)"
        )

        # ─── R42 P0-3: 管理员角色多对多映射(admin_principal_roles) ───
        # 一个 principal 可拥有多个角色,一个角色可分配给多个 principal。
        # check_permission 优先从此表查询角色,fallback 到 rbac_user_roles。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS admin_principal_roles (
                principal_id INTEGER NOT NULL,
                role_name    TEXT NOT NULL,
                granted_by   INTEGER DEFAULT 0,
                granted_at   TEXT,
                PRIMARY KEY (principal_id, role_name),
                FOREIGN KEY (principal_id) REFERENCES admin_principals(id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_principal_roles_pid ON admin_principal_roles(principal_id)"
        )

        # ─── R41 P0-5: 命令执行持久化(幂等缓存 + 租约) ───
        # 替代原 CommandBus._EXECUTED_ACTIONS 进程内 dict,支持:
        # - 重启后 action_id 仍可见(持久化)
        # - 多 worker CAS 互斥(避免重复执行)
        # - 执行租约(executing + lease_until)防止僵死
        # - request_hash 防篡改(相同 action_id 不同 payload → 拒绝)
        # R53 P1-5: 新增 requires_approval 列(BOOLEAN,默认 0)+ CHECK 约束
        # - requires_approval=1 表示该命令必须经过审批路径(approved → executing)
        # - CHECK 约束 1: requires_approval 仅允许 0 / 1
        # - CHECK 约束 2: 高风险动作(requires_approval=1)status='executing' 时
        #   必须存在 approved_at(防止 pending → executing 直接转换,绕过审批)
        #   注:SQLite CHECK 不能跨行校验状态转换历史,approved_at 列由
        #   claim_execution_approved 在 CAS UPDATE 时同步写入,作为"已审批"标记。
        #   应用层 claim_execution 会在 UPDATE 前校验 HIGH_RISK_ACTIONS +
        #   requires_approval,SQL CHECK 仅作兜底保护。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS command_executions (
                action_id        TEXT PRIMARY KEY,
                command_type     TEXT NOT NULL,
                principal_id     INTEGER NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                owner            TEXT,
                lease_until      TEXT,
                request_hash     TEXT NOT NULL,
                result           TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approved_at      TEXT,
                CHECK (requires_approval IN (0, 1)),
                CHECK (requires_approval = 0 OR status != 'executing' OR approved_at IS NOT NULL)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmd_exec_status ON command_executions(status)"
        )
        # R53 P1-5: 幂等迁移 — 为旧表添加 requires_approval / approved_at 列(已存在则忽略)
        # SQLite 旧表无法 ALTER ADD CHECK,通过 PRAGMA table_info 检测并记录 warning,
        # 应用层 claim_execution 已在 UPDATE 前校验 HIGH_RISK_ACTIONS,SQL 约束仅对新表生效。
        for _col_ddl_cmd in (
            "ALTER TABLE command_executions ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE command_executions ADD COLUMN approved_at TEXT",
        ):
            try:
                await self._db.execute(_col_ddl_cmd)
            except Exception as _e_cmd:
                if "duplicate column name" in str(_e_cmd).lower():
                    logger.debug(
                        f"[CacheStore] command_executions ALTER 升级(可忽略): {_e_cmd}"
                    )
                else:
                    logger.warning(
                        f"[CacheStore] command_executions ALTER TABLE失败(非预期): {_e_cmd}"
                    )
        # R53 P1-5: 检测旧表 schema 是否缺少 CHECK 约束(仅记录 warning,应用层兜底)
        try:
            cursor_cmd_tbl = await self._db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='command_executions'"
            )
            _cmd_tbl_row = await cursor_cmd_tbl.fetchone()
            if _cmd_tbl_row and _cmd_tbl_row[0] and "CHECK" not in _cmd_tbl_row[0].upper():
                logger.warning(
                    "[CacheStore] command_executions 表缺少 CHECK 约束"
                    "(旧表 schema),应用层 claim_execution 校验已兜底,"
                    "建议重建表以启用 DDL 约束"
                )
        except Exception as _e_cmd_check:
            logger.debug(
                f"[CacheStore] command_executions schema 检测(可忽略): {_e_cmd_check}"
            )

        # ─── R44 G0-2 / R46 P0-1 / R48 P0-4 / R49 P0-4: effect_receipts 表 — 外部副作用 receipt 持久化 ───
        # R46 P0-1: 增加 request_hash/attempt/lease_owner/lease_until/last_error/reconcile_status
        # 用于幂等控制:action_id + effect_type + target 唯一标识一个外部副作用,
        # 执行前检查 receipt 是否已 completed,避免重复触发外部副作用。
        # R48 P0-4: 增加 CHECK 约束 — critical effect_type 行的 request_hash 必须非 NULL,
        # 防止同 action_id 不同参数共用 receipt(仅对新建表生效;
        # 旧表无法 ALTER ADD CHECK,由应用层 record_pending 校验兜底)。
        # R49 P0-4: request_hash 升级为 TEXT NOT NULL(全表非 NULL),
        # CHECK 约束改为 `request_hash != ''`(critical effect_type 不能为空字符串,
        # 非 critical 允许空串)。旧表无法加 NOT NULL/CHECK,用 PRAGMA table_info
        # 检测并记录 warning,应用层 record_pending 校验已兜底。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS effect_receipts (
                action_id          TEXT NOT NULL,
                effect_type        TEXT NOT NULL,
                target             TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'pending',
                external_id        TEXT,
                created_at         TEXT NOT NULL,
                completed_at       TEXT,
                request_hash       TEXT NOT NULL,
                attempt            INTEGER NOT NULL DEFAULT 0,
                lease_owner        TEXT,
                lease_until        TEXT,
                last_error         TEXT,
                reconcile_status   TEXT,
                PRIMARY KEY (action_id, effect_type, target),
                CHECK (request_hash != '' OR effect_type NOT IN
                       ('telegram_send','telegram_copy','r2_put','r2_download',
                        'restore','ban','takedown','purge','crdb_delete'))
            )"""
        )
        # R49 P0-4: 检测旧表 schema(无 NOT NULL / 无 CHECK 约束)。
        # SQLite 不能 ALTER TABLE 加 CHECK 约束,旧表只能记录 warning,
        # 应用层 record_pending 校验已能阻断 critical effect 空 request_hash。
        try:
            cursor = await self._db.execute(
                "PRAGMA table_info(effect_receipts)"
            )
            _er_cols = await cursor.fetchall()
            # (cid, name, type, notnull, dflt_value, pk)
            _rh_col = [c for c in _er_cols if c[1] == "request_hash"]
            if _rh_col and _rh_col[0][3] == 0:
                logger.warning(
                    "[CacheStore] effect_receipts.request_hash 缺少 NOT NULL 约束"
                    "(旧表 schema),应用层 record_pending 校验已兜底,"
                    "建议重建表以启用 DDL 约束"
                )
            cursor = await self._db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='effect_receipts'"
            )
            _er_tbl = await cursor.fetchone()
            if _er_tbl and _er_tbl[0] and "CHECK" not in _er_tbl[0].upper():
                logger.warning(
                    "[CacheStore] effect_receipts 表缺少 CHECK 约束"
                    "(旧表 schema),应用层 record_pending 校验已兜底,"
                    "建议重建表以启用 DDL 约束"
                )
        except Exception as _e:
            logger.debug(
                f"[CacheStore] effect_receipts schema 检测(可忽略): {_e}"
            )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_effect_receipts_action ON effect_receipts(action_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_effect_receipts_status ON effect_receipts(status)"
        )
        # R46 P0-1: 幂等升级旧表 — 添加新列(已存在则忽略)
        for _col_ddl in (
            "ALTER TABLE effect_receipts ADD COLUMN request_hash TEXT",
            "ALTER TABLE effect_receipts ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE effect_receipts ADD COLUMN lease_owner TEXT",
            "ALTER TABLE effect_receipts ADD COLUMN lease_until TEXT",
            "ALTER TABLE effect_receipts ADD COLUMN last_error TEXT",
            "ALTER TABLE effect_receipts ADD COLUMN reconcile_status TEXT",
        ):
            try:
                await self._db.execute(_col_ddl)
            except Exception as _e:
                if "duplicate column name" in str(_e).lower():
                    logger.debug(
                        f"[CacheStore] effect_receipts ALTER 升级(可忽略): {_e}"
                    )
                else:
                    logger.warning(
                        f"[CacheStore] effect_receipts ALTER TABLE失败(非预期): {_e}"
                    )
        await self._db.execute(
            # R50 P0-3: 扩展 partial index 覆盖 hash_mismatch_needs_reconcile 状态
            # 原 R46 仅覆盖 needs_reconcile,R50 P0-3 新增 hash_mismatch_needs_reconcile
            "CREATE INDEX IF NOT EXISTS idx_effect_receipts_reconcile "
            "ON effect_receipts(reconcile_status) "
            "WHERE reconcile_status IN ('needs_reconcile', 'hash_mismatch_needs_reconcile')"
        )

        # ─── R46 P1: mfa_used_totp 表 — TOTP 重放防护持久化(跨进程共享) ───
        # R46 P1: 存储已使用的 TOTP timestep(principal_id, timestep)防重放,
        # 替代进程内字典,确保多进程间共享重放检测状态。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS mfa_used_totp (
                principal_id  INTEGER NOT NULL,
                timestep      INTEGER NOT NULL,
                used_at       REAL NOT NULL,
                PRIMARY KEY (principal_id, timestep)
            )"""
        )

        # ─── R46 P1 / R47 P1-b: mfa_failures 表 — MFA 错误限流持久化(跨进程共享) ───
        # R47 P1-b: 旧 schema (principal_id, failed_at REAL, PRIMARY KEY(principal_id, failed_at))
        #   同秒/同毫秒多次失败会碰撞导致 INSERT OR IGNORE 丢失记录。
        # 新 schema: id INTEGER AUTOINCREMENT 主键 + failed_at_ms INTEGER 毫秒时间戳,
        #   彻底消除碰撞。老表保留为 mfa_failures_old 备份,数据迁移到新表。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS mfa_failures (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                principal_id   INTEGER NOT NULL,
                failed_at_ms   INTEGER NOT NULL,
                created_at     TEXT NOT NULL
            )"""
        )
        # 幂等迁移: 检测旧 schema mfa_failures 表并迁移到新表
        # 旧表特征: PRIMARY KEY (principal_id, failed_at) 且无 failed_at_ms 列
        try:
            cursor_old_check = await self._db.execute("PRAGMA table_info(mfa_failures)")
            cols_info = await cursor_old_check.fetchall()
            col_names = {row[1] for row in cols_info} if cols_info else set()
            # 新表应有 failed_at_ms 列,旧表应有 failed_at 列且无 failed_at_ms
            # 若检测到旧表(有 failed_at 无 failed_at_ms),执行迁移
            if "failed_at" in col_names and "failed_at_ms" not in col_names:
                # 1. 重命名旧表为 mfa_failures_old(若已存在则跳过)
                try:
                    await self._db.execute(
                        "ALTER TABLE mfa_failures RENAME TO mfa_failures_old"
                    )
                except Exception:
                    pass  # mfa_failures_old 已存在或重命名失败,忽略
                # 2. 创建新 schema 的 mfa_failures 表
                await self._db.execute(
                    """CREATE TABLE IF NOT EXISTS mfa_failures (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        principal_id   INTEGER NOT NULL,
                        failed_at_ms   INTEGER NOT NULL,
                        created_at     TEXT NOT NULL
                    )"""
                )
                # 3. 迁移数据(failed_at REAL 秒 → failed_at_ms 毫秒)
                try:
                    await self._db.execute(
                        "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
                        "SELECT principal_id, "
                        "CAST(failed_at * 1000 AS INTEGER), "
                        "datetime(failed_at, 'unixepoch') "
                        "FROM mfa_failures_old"
                    )
                    logger.info("[CacheStore] mfa_failures 旧表数据迁移到新表完成")
                except Exception as _e_migrate:
                    logger.warning(
                        f"[CacheStore] mfa_failures 数据迁移失败(可忽略): {_e_migrate}"
                    )
                # 4. 创建新索引(替换旧的 PRIMARY KEY 索引)
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mfa_failures_principal_time "
                    "ON mfa_failures(principal_id, failed_at_ms)"
                )
            else:
                # 新表已存在,确保索引存在
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mfa_failures_principal_time "
                    "ON mfa_failures(principal_id, failed_at_ms)"
                )
        except Exception as _e_mfa_migrate:
            logger.warning(
                f"[CacheStore] mfa_failures schema 迁移检测失败(可忽略): {_e_mfa_migrate}"
            )

        # ─── R46 P0-3: unregistered_copies 表 — 持久化 COPIED_UNREGISTERED 状态 ───
        # Telegram copy 成功但 outbox 写失败时,持久化目标 channel/message_id,
        # 进程重启后可扫描未 reconciled 行,优先补 Manifest 而非重新 copy。
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS unregistered_copies (
                upload_id       TEXT NOT NULL,
                file_unique_id  TEXT NOT NULL,
                media_group_id  TEXT,
                channel_id      INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                state           TEXT NOT NULL,
                reason          TEXT,
                created_at      TEXT NOT NULL,
                reconciled_at   TEXT,
                PRIMARY KEY (upload_id, file_unique_id, channel_id, message_id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_unreg_copies_state "
            "ON unregistered_copies(state) WHERE reconciled_at IS NULL"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_unreg_copies_upload ON unregistered_copies(upload_id)"
        )

        # ─── R46 P1: entity_versions 表 — 原子 version 分配 ───
        # 解决 dirty_outbox version 的 MAX+1 并发竞态
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS entity_versions (
                table_name  TEXT NOT NULL,
                pk          TEXT NOT NULL,
                version     INTEGER NOT NULL,
                PRIMARY KEY (table_name, pk)
            )"""
        )

        # ─── R47 P0-5: delivery_group_receipts 表 — 群发回执聚合 ───
        # 跟踪群发任务(group_id)的子任务确认状态,源消息 IDs → 目标用户 IDs
        # 状态机: pending(待确认) → partial(部分确认) → completed(全部确认) /
        #         failed(失败/超时)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS delivery_group_receipts (
                group_id         TEXT PRIMARY KEY,
                expected_count   INTEGER NOT NULL,
                confirmed_count  INTEGER NOT NULL DEFAULT 0,
                status           TEXT NOT NULL DEFAULT 'pending',
                source_ids       TEXT NOT NULL,
                target_ids       TEXT NOT NULL,
                action_id        TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_group_receipts_action_id "
            "ON delivery_group_receipts(action_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_group_receipts_status "
            "ON delivery_group_receipts(status)"
        )

        # ─── R47 P1-a: callback_nonces 表 — 回调 nonce 原子消费 ───
        # 防止回调 URL 被重放(如审批回调、支付回调),nonce 一次性消费
        # consumed_at IS NULL 表示未消费,UPDATE WHERE consumed_at IS NULL RETURNING 原子消费
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS callback_nonces (
                nonce         TEXT PRIMARY KEY,
                principal_id  INTEGER NOT NULL,
                action        TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                consumed_at   TEXT,
                created_at    TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_callback_nonces_principal "
            "ON callback_nonces(principal_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_callback_nonces_expires "
            "ON callback_nonces(expires_at)"
        )

        await self._db.commit()
        # ─── 注入 db 连接给 Buffer ───
        _decode_log_buffer.set_db(self._db)
        _code_change_buffer.set_db(self._db)
        logger.debug(f"[CacheStore] 初始化完成: {DB_PATH}")

    # ─── R38 P1-2: dirty_outbox 统一事务发件箱方法 ───
    # 所有 Bot 只写本地 SQLite 事务后,调用 add_dirty_outbox() 记录变更,
    # crdb_sync 通过 get_dirty_outbox_batch() 批量拉取并 UPSERT/tombstone 到 CRDB,
    # 处理完成后 mark_dirty_processed() 标记已处理。
    async def add_dirty_outbox(
        self, table_name: str, pk: str, operation: str = "upsert",
        payload: str | None = None, version: int = 0,
        connection: Any = None, tx: Any = None,
    ) -> int:
        """R38 P1-2 + R39 P0-4 + R40 P0-5 + R41 P0-6 + R47 P0-6: 写入 dirty_outbox 一条变更记录。

        R40 P0-5 变更:
          - 失败时抛出异常(不再仅 warning),让上层 UnitOfWork 捕获并回滚整个事务,
            避免业务表已提交但 dirty_outbox 写入丢失的"半提交"问题。
          - 新增 tx 别名参数(等价于 connection),便于与 UnitOfWork.transaction() 配合。

        R41 P0-6 变更:
          - 若 table_name 在 replication_policy 中声明为 LOCAL_ONLY,
            则写入时预设 processed=1 + local_only=1(预标记为已处理),
            crdb_sync dispatcher 不会重复拉取,避免无意义堆积。
            记录仍保留在 dirty_outbox 表中以便审计,但不参与 CRDB 同步。

        R47 P0-6 变更:
          - version=0 时改为调用 allocate_version(connection, table_name, pk)
            原子分配递增 version(替代旧的 MAX(version)+1 模式,消除并发竞态)。
          - 配合 dirty_outbox UNIQUE(table_name, pk, version) 约束,
            UNIQUE 冲突时自动重新分配 version 重试(最多 5 次),不回退时间戳。

        Args:
            table_name: 受影响表名(如 file_records_local / codes_local / tasks / approvals)
            pk: 行主键值(字符串)
            operation: 'upsert'(默认) 或 'tombstone'(软删除)
            payload: 可选 JSON 序列化后的载荷(行快照或变更字段)
            version: 单调递增版本号(用于 CRDB UPSERT 条件);0 表示自动分配
            connection: R39 P0-4 可选事务连接,传入时不自动 commit
                (由调用方在同一事务内控制 commit/rollback,确保与业务表更新原子性)
            tx: R40 P0-5 connection 的别名(与 connection 等价,优先使用 tx)

        Returns:
            新插入行 id;失败抛出异常(不再返回 0)

        Raises:
            RuntimeError: CacheStore 未初始化
            Exception: INSERT 失败时透传底层异常,由上层事务回滚
        """
        # R40 P0-5: tx 别名,优先使用 tx(向后兼容 connection 参数)
        if tx is not None:
            connection = tx

        # R41 P0-6: 检查表是否为 LOCAL_ONLY 策略
        # 若是,预设 processed=1 + local_only=1,避免 crdb_sync dispatcher 重复拉取
        # (记录仍写入 dirty_outbox 供审计/mark_dirty_local_only 等方法使用,但不参与同步)
        _is_local_only_table = False
        try:
            from services.replication_policy import is_local_only as _policy_is_local_only
            _is_local_only_table = _policy_is_local_only(table_name)
        except Exception:
            # replication_policy 模块不可用时降级,按普通表处理(不预标记)
            pass

        # R41 P0-6: 预设 processed + local_only 列值
        # local_only 表: processed=1, local_only=1(预标记为已处理)
        # 普通 / CRDB 表: processed=0, local_only=0(待 crdb_sync 处理)
        if _is_local_only_table:
            _processed_init = 1
            _local_only_init = 1
        else:
            _processed_init = 0
            _local_only_init = 0

        # R47 P0-6: version=0 时调用 allocate_version 原子分配递增 version
        # 替代旧的 MAX(version)+1 模式(存在并发竞态)和 payload 时间戳模式(可能碰撞)
        # allocate_version 通过 entity_versions 表 UPSERT + RETURNING 保证原子性
        if version == 0:
            try:
                version = await self.allocate_version(
                    table_name, pk, connection=connection
                )
            except Exception as _e_alloc:
                # allocate_version 失败时 fallback 到时间戳生成(保证不阻塞主流程)
                logger.warning(
                    f"[CacheStore] add_dirty_outbox allocate_version 失败,fallback 时间戳: {_e_alloc}"
                )
                version = _generate_version_from_payload(table_name, payload)

        # R39 P0-4 + R40 P0-5 + R47 P0-6: 事务发件箱模式 — 若调用方传入 connection/tx,
        # 则使用该连接(不自动 commit),确保 dirty_outbox 写入与业务表更新
        # 在同一事务内原子提交/回滚,避免半提交导致数据不一致。
        # R47 P0-6: 通过 _add_dirty_outbox_with_retry 处理 UNIQUE 冲突重试。
        # R40 P0-5: 失败时抛异常(而非仅 warning),让上层 UnitOfWork 回滚。
        if connection is not None:
            # 不调用 commit,由调用方控制事务;失败抛异常让上层回滚
            # R47 P0-6: UNIQUE 冲突时重试(最多 5 次)
            return await self._add_dirty_outbox_with_retry(
                connection, table_name, pk, version, operation, payload,
                _processed_init, _local_only_init,
            )
        # 兼容模式: 无 connection/tx 时自动 commit(向后兼容旧调用方)
        # R40 P0-5: 失败时抛异常,而非返回 0 让调用方误以为成功
        if not self._db:
            raise RuntimeError(
                "[CacheStore] add_dirty_outbox 失败: CacheStore 未初始化(_db is None)"
            )
        # R47 P0-6: 自动 commit 模式也使用重试逻辑
        try:
            _rid = await self._add_dirty_outbox_with_retry(
                self._db, table_name, pk, version, operation, payload,
                _processed_init, _local_only_init,
            )
            await self._db.commit()
            return _rid
        except Exception:
            # 自动 commit 模式下失败时回滚并抛出
            try:
                await self._db.rollback()
            except Exception:
                pass
            raise

    @asynccontextmanager
    async def transaction(self):
        """R40 P0-5: 事务上下文管理器 — 业务表 + audit_log + dirty_outbox 同事务。

        用法:
            async with store.transaction() as tx:
                await tx.execute("INSERT INTO ...")
                await store.add_dirty_outbox("table", "pk", connection=tx)
            # 退出时自动 COMMIT(或异常时 ROLLBACK)

        内部基于 aiosqlite.Connection 的 BEGIN/COMMIT/ROLLBACK。
        业务代码在事务上下文中不得再调用 store._db.commit()(由本管理器统一控制)。
        """
        if not self._db:
            raise RuntimeError("[CacheStore] transaction 失败: _db 未初始化")
        try:
            await self._db.execute("BEGIN")
        except Exception as begin_err:
            # 已处于事务中: 复用现有事务(不重新 BEGIN,也不主动 COMMIT)
            logger.debug(
                f"[CacheStore] transaction BEGIN 失败(复用现有事务): {begin_err}"
            )
        try:
            yield self._db
        except Exception:
            try:
                await self._db.rollback()
            except Exception as rollback_err:
                logger.warning(f"[CacheStore] transaction rollback 失败: {rollback_err}")
            raise
        else:
            await self._db.commit()

    async def get_dirty_outbox_batch(self, limit: int = 100) -> list[dict]:
        """R38 P1-2: 拉取一批未处理的 dirty_outbox 记录(processed=0)。

        Args:
            limit: 单批最大条数

        Returns:
            [{id, table_name, pk, version, operation, payload, created_at}, ...]
        """
        if not self._db:
            return []
        try:
            cursor = await self._db.execute(
                """SELECT id, table_name, pk, version, operation, payload, created_at
                   FROM dirty_outbox WHERE processed = 0
                   ORDER BY id ASC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0], "table_name": r[1], "pk": r[2],
                    "version": r[3], "operation": r[4],
                    "payload": r[5], "created_at": r[6],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[CacheStore] get_dirty_outbox_batch 失败: {e}")
            return []

    # ─── R40 P0-4: pending_uploads_local SQLite 权威方法 ───
    # Up Bot 调用 insert_pending_upload_local 双写(CRDB + SQLite local),
    # Idx Bot 调用 claim_pending_uploads CAS 认领 + complete/fail 推进状态。
    # 消除 Idx Bot 对 CRDB 凭证的依赖(无 CRDB 时主循环仍可工作)。

    async def insert_pending_upload_local(self, record: dict, mark_dirty: bool = False) -> int:
        """R40 P0-4 / R46 P0-4: 写入 pending_uploads_local(Up Bot 双写时调用)。

        R46 P0-4 整改:
          - pending_upload insert 和 dirty_outbox insert 使用同一个事务,
            任一步失败全部 rollback(不再 warning-and-continue)。
          - outbox 写失败时抛异常,让上层 UnitOfWork 回滚。

        Args:
            record: pending_upload 字段字典
            mark_dirty: True 则入 dirty_outbox 由 crdb_sync 同步

        Returns:
            新插入行的 id;失败返回 0
        """
        if not self._db:
            return 0
        import json as _json_pu
        from datetime import datetime as _dt_pu
        def _serialize_pu(val):
            if val is None:
                return None
            if isinstance(val, _dt_pu):
                return val.isoformat()
            if isinstance(val, (list, dict)):
                return _json_pu.dumps(val, default=str)
            return val
        # R46 P0-4: 统一事务 — BEGIN → insert pending → insert outbox → COMMIT
        # 任一步失败全部 rollback
        from contextlib import asynccontextmanager as _acm
        @_acm
        async def _tx_scope():
            if self._in_writer_tx:
                # 已在外层事务中,直接 yield
                yield self._db
                return
            await self._db.execute("BEGIN")
            try:
                yield self._db
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
        try:
            async with _tx_scope() as _tx:
                cursor = await _tx.execute(
                    """INSERT INTO pending_uploads_local
                       (uploader_id, primary_channel_id, primary_channel_msg_id,
                        file_types, batch_msg_ids, batch_file_meta, status_msg_id,
                        created_at, processed, claimed_at, note, protect_content,
                        file_ttl_days, upload_id, dead_reason, dead_count,
                        crdb_id, crdb_synced)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, '', 0, 0, ?)""",
                    (
                        record.get("uploader_id"),
                        record.get("primary_channel_id"),
                        record.get("primary_channel_msg_id"),
                        _serialize_pu(record.get("file_types")),
                        record.get("batch_msg_ids", ""),
                        _serialize_pu(record.get("batch_file_meta")),
                        int(record.get("status_msg_id", 0) or 0),
                        record.get("created_at") or _dt_pu.now().isoformat(),
                        record.get("note", ""),
                        int(bool(record.get("protect_content", False))),
                        int(record.get("file_ttl_days", 0) or 0),
                        record.get("upload_id", ""),
                        1 if not mark_dirty else 0,
                    ),
                )
                new_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
                if mark_dirty and new_id:
                    # R46 P0-4: dirty_outbox insert 与 pending_upload 在同一事务
                    # 失败时整个事务 rollback,不再 warning-and-continue
                    payload = _json_pu.dumps(record, default=str)
                    if isinstance(payload, bytes):
                        payload = payload.decode()
                    await self.add_dirty_outbox(
                        "pending_uploads", str(new_id), "upsert", payload,
                        tx=_tx,
                    )
                return new_id
        except Exception as e:
            logger.error(f"[CacheStore] insert_pending_upload_local 失败(事务回滚): {e}")
            if self._in_writer_tx:
                raise
            return 0

    async def claim_pending_uploads(self, cutoff_ts: float, limit: int = 10) -> list[dict]:
        """R40 P0-4: CAS 认领一批未处理的 pending_uploads 记录。

        在 BEGIN IMMEDIATE 事务中执行 SELECT + UPDATE,
        保证多 worker 并发时每条记录只被一个 worker 认领。
        不依赖 SQLite 3.35+ 的 RETURNING 语法,兼容所有 SQLite 版本。

        Args:
            cutoff_ts: 时间戳,claimed_at 早于此值(或为 0)的记录才能被认领。
                常用 time.time() - 300(5 分钟超时)。
            limit: 单批最大认领数,默认 10。

        Returns:
            认领成功的记录列表(已设置 processed=2, claimed_at=now)。
        """
        if not self._db:
            return []
        now = time.time()
        # pending_uploads_local 列名(用于 SELECT 结果转 dict)
        col_names = [
            "id", "uploader_id", "primary_channel_id", "primary_channel_msg_id",
            "file_types", "batch_msg_ids", "batch_file_meta", "status_msg_id",
            "created_at", "processed", "claimed_at", "note", "protect_content",
            "file_ttl_days", "upload_id", "dead_reason", "dead_count",
        ]
        for attempt in range(3):
            try:
                # 如果不在 writer_transaction 中,自己开启 BEGIN IMMEDIATE
                # 保证 SELECT + UPDATE 原子性(其他写操作会等待)
                own_tx = False
                if not self._in_writer_tx:
                    await self._db.execute("BEGIN IMMEDIATE")
                    own_tx = True
                try:
                    # SELECT 待认领的记录(获取完整字段)
                    cursor = await self._db.execute(
                        f"""SELECT {', '.join(col_names)}
                           FROM pending_uploads_local
                           WHERE processed = 0 AND (claimed_at < ? OR claimed_at = 0)
                           ORDER BY id ASC LIMIT ?""",
                        (cutoff_ts, limit),
                    )
                    rows = await cursor.fetchall()
                    if not rows:
                        if own_tx:
                            await self._db.execute("COMMIT")
                        return []
                    # UPDATE 认领状态(processed=2, claimed_at=now)
                    ids = [r[0] for r in rows]  # id 是第一列
                    placeholders = ",".join("?" * len(ids))
                    await self._db.execute(
                        f"UPDATE pending_uploads_local SET processed = 2, claimed_at = ? "
                        f"WHERE id IN ({placeholders})",
                        (now, *ids),
                    )
                    if own_tx:
                        await self._db.execute("COMMIT")
                    # 转换为 dict 列表,手动更新 processed 和 claimed_at
                    # (SELECT 返回的是旧值 processed=0,UPDATE 后才是 processed=2)
                    import json as _json_claim
                    result: list[dict] = []
                    for r in rows:
                        row_dict = dict(zip(col_names, r))
                        row_dict["processed"] = 2
                        row_dict["claimed_at"] = now
                        # 反序列化 JSON 字段(与 _deserialize_sqlite_row 行为一致)
                        if row_dict.get("file_types") and isinstance(row_dict["file_types"], str):
                            try:
                                row_dict["file_types"] = _json_claim.loads(row_dict["file_types"])
                            except Exception:
                                pass
                        if row_dict.get("batch_file_meta") and isinstance(row_dict["batch_file_meta"], str):
                            try:
                                row_dict["batch_file_meta"] = _json_claim.loads(row_dict["batch_file_meta"])
                            except Exception:
                                pass
                        result.append(row_dict)
                    return result
                except Exception:
                    if own_tx:
                        try:
                            await self._db.execute("ROLLBACK")
                        except Exception:
                            pass
                    raise
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] claim_pending_uploads 失败: {e}")
                return []

    async def complete_pending_upload(self, upload_id: int) -> bool:
        """R40 P0-4: 标记 pending_upload 为已完成(processed=1, claimed_at=0)。

        应在 file_records_local + codes_local + dirty_outbox 全部成功写入后调用,
        与上述写入在同事务(writer_transaction 上下文)中执行以确保原子性。

        Args:
            upload_id: pending_uploads_local.id

        Returns:
            True 表示标记成功(rowcount>0);False 表示未匹配到行(已被其他 worker 处理或不存在)。
        """
        if not self._db:
            return False
        try:
            cursor = await self._db.execute(
                "UPDATE pending_uploads_local SET processed = 1, claimed_at = 0 WHERE id = ?",
                (upload_id,),
            )
            if not self._in_writer_tx:
                await self._db.commit()
            return bool(cursor and cursor.rowcount > 0)
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.warning(f"[CacheStore] complete_pending_upload 失败 id={upload_id}: {e}")
            return False

    async def fail_pending_upload(self, upload_id: int, reason: str = "") -> bool:
        """R40 P0-4: 标记 pending_upload 处理失败,回滚到 processed=0 允许下轮重领。

        应在 file_records_local + codes_local 写入失败时调用(事务中),让整个事务 ROLLBACK
        会自动回滚 claimed_at=now 的更新;但若事务外调用,本方法独立回滚状态。

        记录 dead_reason + dead_count++ 便于诊断连续失败的记录。

        Args:
            upload_id: pending_uploads_local.id
            reason: 失败原因(可选,用于诊断)

        Returns:
            True 表示回滚成功;False 表示未匹配到行或失败。
        """
        if not self._db:
            return False
        try:
            cursor = await self._db.execute(
                "UPDATE pending_uploads_local SET processed = 0, claimed_at = 0, "
                "dead_reason = ?, dead_count = dead_count + 1 WHERE id = ?",
                (reason, upload_id),
            )
            if not self._in_writer_tx:
                await self._db.commit()
            return bool(cursor and cursor.rowcount > 0)
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.warning(f"[CacheStore] fail_pending_upload 失败 id={upload_id}: {e}")
            return False

    async def reset_stale_claims(self, claim_timeout_seconds: float = 300.0) -> int:
        """R40 P0-4: 重置超时的 claimed(processed=2)记录回 pending(processed=0)。

        场景: Idx Bot worker 崩溃后,claimed_at 已过期的记录需要被重新认领。
        由 _process_pending_uploads 调用(或独立维护任务定期清理)。

        Args:
            claim_timeout_seconds: 认领超过此秒数视为崩溃,默认 300(5 分钟)。

        Returns:
            被重置的记录数。
        """
        if not self._db:
            return 0
        now = time.time()
        cutoff = now - claim_timeout_seconds
        try:
            cursor = await self._db.execute(
                "UPDATE pending_uploads_local SET processed = 0, claimed_at = 0 "
                "WHERE processed = 2 AND claimed_at < ? AND claimed_at > 0",
                (cutoff,),
            )
            if not self._in_writer_tx:
                await self._db.commit()
            return cursor.rowcount if cursor else 0
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.warning(f"[CacheStore] reset_stale_claims 失败: {e}")
            return 0

    async def mark_dirty_processed(self, ids: list[int]) -> int:
        """R38 P1-2: 标记 dirty_outbox 记录为已处理(processed=1)。

        Args:
            ids: 已处理成功的 dirty_outbox.id 列表

        Returns:
            实际标记的行数
        """
        if not self._db or not ids:
            return 0
        try:
            placeholders = ",".join("?" * len(ids))
            cursor = await self._db.execute(
                f"UPDATE dirty_outbox SET processed = 1 WHERE id IN ({placeholders})",
                ids,
            )
            await self._db.commit()
            return cursor.rowcount if cursor else 0
        except Exception as e:
            logger.warning(f"[CacheStore] mark_dirty_processed 失败: {e}")
            return 0

    async def mark_dirty_local_only(self, ids: list[int]) -> int:
        """R40 P0-5: 标记 dirty_outbox 记录为 local_only + processed(跳过 CRDB 同步)。

        用于 local_only 表(tasks/collections/notifications/audit_log 等),
        这些表仅存在于 SQLite 本地,不需要同步到 CRDB。

        Args:
            ids: 已处理的 dirty_outbox.id 列表

        Returns:
            实际标记的行数
        """
        if not self._db or not ids:
            return 0
        try:
            placeholders = ",".join("?" * len(ids))
            cursor = await self._db.execute(
                f"UPDATE dirty_outbox SET processed = 1, local_only = 1 WHERE id IN ({placeholders})",
                ids,
            )
            await self._db.commit()
            return cursor.rowcount if cursor else 0
        except Exception as e:
            logger.warning(f"[CacheStore] mark_dirty_local_only 失败: {e}")
            return 0

    async def mark_dirty_retry(
        self, ids: list[int], error_msg: str,
        next_retry_at: str | None = None,
    ) -> int:
        """R51 P0-9: 标记 dirty_outbox 记录的错误信息和下次重试时间(指数退避)。

        DLQ 写入失败时,保持 dirty_outbox.processed=0(未处理),
        记录 last_error 和 next_retry_at,支持指数退避重试。
        下一轮 _sync_dirty_outbox 会重新拉取这些记录并重试。

        Args:
            ids: 需要重试的 dirty_outbox.id 列表
            error_msg: 失败原因(DLQ 写入失败的原因)
            next_retry_at: 下次重试时间(ISO 字符串),None 时自动计算 60s 后

        Returns:
            实际更新的行数
        """
        if not self._db or not ids:
            return 0
        import datetime as _dt_retry
        if next_retry_at is None:
            next_retry_at = (
                _dt_retry.datetime.now() + _dt_retry.timedelta(seconds=60)
            ).isoformat()
        try:
            placeholders = ",".join("?" * len(ids))
            cursor = await self._db.execute(
                f"UPDATE dirty_outbox SET last_error = ?, next_retry_at = ? "
                f"WHERE id IN ({placeholders})",
                [error_msg, next_retry_at] + list(ids),
            )
            await self._db.commit()
            return cursor.rowcount if cursor else 0
        except Exception as e:
            logger.warning(f"[CacheStore] mark_dirty_retry 失败: {e}")
            return 0

    async def count_unprocessed_dirty_outbox(self) -> int:
        """R38 P1-2: 统计未处理的 dirty_outbox 行数(供 crdb_sync 懒加载判断)。"""
        if not self._db:
            return 0
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM dirty_outbox WHERE processed = 0"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception as e:
            logger.debug(f"[CacheStore] count_unprocessed_dirty_outbox 异常: {e}")
            return 0

    # ─── R41 P0-6: DLQ 死信队列 SQLite 权威存储 ───
    # crdb_sync 处理失败的 dirty_outbox 记录路由到 dlq_records 表,
    # 字段: status / retry_count / max_retries / next_retry_at / last_error /
    #       created_at / updated_at / message_id(去重键) / table_name / reason / original
    # 状态机: pending(可重试) → retrying(重试中) → done(成功) /
    #         permanently_failed(达到 max_retries,停止重试)

    async def insert_dlq_record(
        self, message_id: str, table_name: str, reason: str,
        original: dict | None = None, max_retries: int = 5,
        next_retry_at: str | None = None,
    ) -> int:
        """R41 P0-6: 写入一条 DLQ 记录到 dlq_records 表。

        若 message_id 已存在(同一条 dirty_outbox 记录重复失败),
        则累加 retry_count 并更新 last_error / next_retry_at;
        否则插入新记录。

        Args:
            message_id: 去重键(如 "dirty_outbox:42"),同一记录重复失败时累加重试次数
            table_name: 受影响表名
            reason: 失败原因
            original: 原始 dirty_outbox 记录(可选,JSON 序列化后存储)
            max_retries: 最大重试次数(默认 5),达到后标记 permanently_failed
            next_retry_at: 下次重试时间(ISO 字符串),None 表示不重试

        Returns:
            新插入或更新的 dlq_records.id;失败返回 0
        """
        if not self._db:
            return 0
        import json as _json_dlq
        import datetime as _dt_dlq
        now_str = _dt_dlq.datetime.now().isoformat()
        original_json = _json_dlq.dumps(original, ensure_ascii=False, default=str) if original else None
        try:
            # 检查是否已存在相同 message_id 的记录(幂等去重)
            cursor = await self._db.execute(
                "SELECT id, retry_count, max_retries FROM dlq_records WHERE message_id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            if row:
                # 已存在: 累加 retry_count,更新 last_error / next_retry_at / updated_at
                existing_id = int(row[0])
                new_retry_count = int(row[1]) + 1
                existing_max_retries = int(row[2])
                # 若达到 max_retries,标记为 permanently_failed(停止重试)
                if new_retry_count >= existing_max_retries:
                    new_status = "permanently_failed"
                    new_next_retry = None
                else:
                    new_status = "pending"
                    new_next_retry = next_retry_at
                await self._db.execute(
                    """UPDATE dlq_records
                       SET retry_count = ?, last_error = ?, next_retry_at = ?,
                           status = ?, updated_at = ?
                       WHERE id = ?""",
                    (new_retry_count, reason, new_next_retry, new_status, now_str, existing_id),
                )
                if not self._in_writer_tx:
                    await self._db.commit()
                return existing_id
            # 不存在: 插入新记录
            cursor = await self._db.execute(
                """INSERT INTO dlq_records
                   (message_id, table_name, reason, status, retry_count,
                    max_retries, next_retry_at, last_error, original,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id, table_name, reason,
                    max_retries, next_retry_at, reason, original_json,
                    now_str, now_str,
                ),
            )
            new_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if not self._in_writer_tx:
                await self._db.commit()
            return new_id
        except Exception as e:
            logger.warning(f"[CacheStore] R41 P0-6: insert_dlq_record 失败: {e}")
            return 0

    async def cleanup_dlq(self) -> int:
        """R41 P0-6: 清理 DLQ — 将 retry_count >= max_retries 的记录标记为 permanently_failed。

        达到 max_retries 的记录不再重试,避免无限积压。
        此方法由 crdb_sync 周期性调用(每轮 _sync_dirty_outbox 末尾)。

        Returns:
            标记为 permanently_failed 的记录数
        """
        if not self._db:
            return 0
        try:
            cursor = await self._db.execute(
                """UPDATE dlq_records
                   SET status = 'permanently_failed',
                       next_retry_at = NULL,
                       updated_at = ?
                   WHERE status != 'permanently_failed'
                     AND retry_count >= max_retries""",
                (datetime.datetime.now().isoformat(),),  # type: ignore[attr-defined]
            )
            affected = cursor.rowcount if cursor else 0
            if affected > 0 and not self._in_writer_tx:
                await self._db.commit()
            if affected > 0:
                logger.info(
                    f"[CacheStore] R41 P0-6: cleanup_dlq 标记 {affected} 条 "
                    f"DLQ 记录为 permanently_failed(达到 max_retries)"
                )
            return affected
        except Exception as e:
            logger.warning(f"[CacheStore] R41 P0-6: cleanup_dlq 失败: {e}")
            return 0

    async def list_dlq_records(
        self, status: str | None = None, limit: int = 100,
    ) -> list[dict]:
        """R41 P0-6: 查询 DLQ 记录(供 repair_console / 监控使用)。

        Args:
            status: 可选状态过滤('pending' / 'retrying' / 'permanently_failed' / 'done')
            limit: 返回最大条数

        Returns:
            [{id, message_id, table_name, reason, status, retry_count, max_retries,
              next_retry_at, last_error, original, created_at, updated_at}, ...]
        """
        if not self._db:
            return []
        try:
            if status:
                cursor = await self._db.execute(
                    """SELECT id, message_id, table_name, reason, status,
                              retry_count, max_retries, next_retry_at, last_error,
                              original, created_at, updated_at
                       FROM dlq_records WHERE status = ?
                       ORDER BY id DESC LIMIT ?""",
                    (status, limit),
                )
            else:
                cursor = await self._db.execute(
                    """SELECT id, message_id, table_name, reason, status,
                              retry_count, max_retries, next_retry_at, last_error,
                              original, created_at, updated_at
                       FROM dlq_records
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0], "message_id": r[1], "table_name": r[2],
                    "reason": r[3], "status": r[4], "retry_count": r[5],
                    "max_retries": r[6], "next_retry_at": r[7],
                    "last_error": r[8], "original": r[9],
                    "created_at": r[10], "updated_at": r[11],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[CacheStore] R41 P0-6: list_dlq_records 失败: {e}")
            return []

    # ─── R39 P1-5: 统一软删除 API(tombstone 贯穿删除路径) ───

    # 支持软删除的本地表 → 对应 SQLite 表名 + 主键列名
    _SOFT_DELETE_TABLES: dict[str, tuple[str, str]] = {
        "file_records": ("file_records_local", "file_code"),
        "codes": ("codes_local", "code"),
        "users": ("users_local", "user_id"),
        "cells": ("cells_local", "slot_id"),
        "external_code_mapping": ("external_code_mapping_local", "external_code"),
        "collections": ("collections", "id"),
    }

    async def soft_delete(self, table: str, pk: str, deleted_at: str | None = None) -> bool:
        """R39 P1-5: 统一软删除 API — 设置 deleted_at + status='deleted' + 写 dirty_outbox tombstone。

        所有 UI / Bot / retention 删除路径必须走此 API,禁止物理 DELETE。
        物理删除仅在已备份、已同步、保留期届满后由独立 retention job 执行。

        Args:
            table: 逻辑表名(file_records / codes / users / cells / external_code_mapping)
            pk: 行主键值(字符串)
            deleted_at: 删除时间戳(ISO),None 时自动取当前时间

        Returns:
            True: 软删除成功(已更新 deleted_at + 写入 dirty_outbox tombstone)
            False: 软删除失败(表不支持 / 记录不存在 / 异常)
        """
        if not self._db:
            return False
        import datetime as _dt
        if deleted_at is None:
            deleted_at = _dt.datetime.now().isoformat()
        mapping = self._SOFT_DELETE_TABLES.get(table)
        if not mapping:
            logger.warning(f"[CacheStore] R39 P1-5: soft_delete 不支持表 {table}")
            return False
        sqlite_table, pk_col = mapping
        try:
            # 1. 设置 deleted_at + status='deleted' + crdb_synced=0(触发 crdb_sync tombstone)
            cursor = await self._db.execute(
                f"UPDATE {sqlite_table} SET deleted_at = ?, status = 'deleted', "
                f"crdb_synced = 0 WHERE {pk_col} = ?",
                (deleted_at, pk),
            )
            if cursor.rowcount == 0:
                logger.warning(
                    f"[CacheStore] R39 P1-5: soft_delete 未命中记录"
                    f"(table={table}, pk={pk})"
                )
                return False
            if not self._in_writer_tx:
                await self._db.commit()
            # 2. 写 dirty_outbox tombstone(供 crdb_sync 同步到 CRDB)
            # R47 P0-6: 使用 allocate_version 分配唯一 version,避免 UNIQUE 约束冲突
            _tombstone_version = await self.allocate_version(
                sqlite_table, pk, connection=self._db
            )
            await self._db.execute(
                """INSERT INTO dirty_outbox
                   (table_name, pk, version, operation, payload, created_at, processed, local_only)
                   VALUES (?, ?, ?, 'tombstone', ?, ?, 0, 0)""",
                (sqlite_table, pk, _tombstone_version,
                 f'{{"deleted_at":"{deleted_at}"}}',
                 _dt.datetime.now().isoformat()),
            )
            if not self._in_writer_tx:
                await self._db.commit()
            logger.info(
                f"[CacheStore] R39 P1-5: soft_delete 成功"
                f"(table={table}, pk={pk}, deleted_at={deleted_at})"
            )
            return True
        except Exception as e:
            logger.warning(f"[CacheStore] R39 P1-5: soft_delete 失败: {e}")
            return False

    async def dump(self, cache_entries: list[tuple[str, dict, float]]):
        """批量写入 [(key, data, timestamp), ...]"""
        if not self._db or not cache_entries:
            return
        rows = []
        for k, v, ts in cache_entries:
            try:
                raw = json.dumps(v, default=str)
                val = raw.decode() if isinstance(raw, bytes) else raw
                rows.append((k, val, ts))
            except (TypeError, ValueError) as e:
                logger.warning(f"[CacheStore] dump({k}) JSON 序列化失败: {e}")
        if rows:
            await self._db.executemany(
                "INSERT OR REPLACE INTO cache_backup (key, value, ts) VALUES (?, ?, ?)",
                rows,
            )
            await self._db.commit()

    async def load(self) -> list[tuple[str, dict]]:
        """返回所有缓存记录 [(key, data), ...]，按时间戳降序（最新的在前）"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT key, value FROM cache_backup ORDER BY ts DESC"
        )
        result = []
        for key, val_json in rows:
            try:
                result.append((key, json.loads(val_json)))
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    # ─── R33: Writer 幂等表方法 ───

    async def check_writer_inbox(self, message_id: str) -> bool:
        """检查消息是否已处理(幂等检查)。

        R33 P1修复: db_writer 处理消息前检查此表,已存在则 XACK 跳过。
        """
        if not self._db or not message_id:
            return False
        try:
            rows = await self._db.execute_fetchall(
                "SELECT 1 FROM writer_inbox WHERE message_id = ?",
                (message_id,)
            )
            return len(rows) > 0
        except Exception as e:
            logger.warning(f"[CacheStore] check_writer_inbox 异常: {e}")
            return False

    async def write_writer_inbox(self, message_id: str, method_name: str,
                                  stream_id: str = "") -> None:
        """记录已处理的消息(幂等键写入)。

        R33 P1修复: SQLite 写成功后调用,记录 message_id 用于去重。
        使用 INSERT OR IGNORE 避免重复插入。

        R34: 在 Writer 事务模式下不自行 commit(由 DBWriter 统一 COMMIT)。
        """
        if not self._db or not message_id:
            return
        now = time.time()
        await self._db.execute(
            "INSERT OR IGNORE INTO writer_inbox "
            "(message_id, method_name, stream_id, created_at, processed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, method_name, stream_id, now, now)
        )
        if not self._in_writer_tx:
            await self._db.commit()

    # ─── R34 P0-1: Writer 原子事务控制 ───

    async def begin_writer_tx(self):
        """开始 Writer 事务:monkey-patch commit 为 no-op,执行 BEGIN IMMEDIATE。

        R34 P0-1: 业务写与 writer_inbox 在同一事务中提交。
        方法内部调用 self._db.commit() 在事务模式下变为 no-op,
        由 DBWriter 的 commit_writer_tx() 统一提交。

        R35 P1-2 修复: 异常安全性 — BEGIN IMMEDIATE 失败时必须恢复 commit 方法
        并重置 _in_writer_tx 标志,否则后续调用会因 commit 被替换为 no-op 而
        静默丢数据。先设标志,再尝试 BEGIN,失败时在 except 中恢复。

        TODO: R39 P1-10 待迁移 — 当前 monkey-patch self._db.commit 为 no-op 的方式
        脆弱且影响同连接并发。迁移计划见 docs/writer-monkeypatch-removal.md:
          1. 改用显式 connection.execute("BEGIN IMMEDIATE") + 显式 COMMIT/ROLLBACK
          2. 所有业务方法不再调用 self._db.commit(),改由 Writer 统一控制事务边界
          3. 移除 _in_writer_tx 标志和 80+ 处 if self._in_writer_tx 分支
        """
        if not self._db:
            raise RuntimeError("CacheStore 未初始化")
        # R35 P1-2: 先设标志 + 替换 commit,再尝试 BEGIN IMMEDIATE
        # 这样 BEGIN 失败时可以在 except 中恢复 commit 方法
        self._in_writer_tx = True
        self._original_commit = self._db.commit
        async def _noop_commit():
            pass
        self._db.commit = _noop_commit
        try:
            await self._db.execute("BEGIN IMMEDIATE")
        except Exception:
            # R35 P1-2: BEGIN IMMEDIATE 失败(如数据库锁超时),
            # 必须恢复 commit 方法并重置标志,避免后续静默丢数据
            if hasattr(self, '_original_commit'):
                try:
                    self._db.commit = self._original_commit
                except Exception:
                    pass
            self._in_writer_tx = False
            raise

    async def commit_writer_tx(self):
        """提交 Writer 事务:执行 COMMIT,恢复 commit 方法。"""
        try:
            await self._db.execute("COMMIT")
        finally:
            if hasattr(self, '_original_commit'):
                self._db.commit = self._original_commit
            self._in_writer_tx = False

    async def rollback_writer_tx(self):
        """回滚 Writer 事务:执行 ROLLBACK,恢复 commit 方法。"""
        try:
            await self._db.execute("ROLLBACK")
        finally:
            if hasattr(self, '_original_commit'):
                self._db.commit = self._original_commit
            self._in_writer_tx = False

    @asynccontextmanager
    async def writer_transaction(self):
        """R35 P1-2: Writer 事务上下文管理器(推荐用法)。

        替代手动调用 begin_writer_tx/commit_writer_tx/rollback_writer_tx,
        确保异常时自动 ROLLBACK,避免遗漏。

        用法:
            async with store.writer_transaction():
                # 此范围内 _in_writer_tx=True,方法内部 commit 被吞
                await store.upsert_user_quota(...)
                await store.upsert_file_record_local(...)

        异常时会自动 ROLLBACK,正常退出时自动 COMMIT。
        """
        await self.begin_writer_tx()
        try:
            yield self._db
        except BaseException:
            # 任何异常(含 CancelledError)都 ROLLBACK
            await self.rollback_writer_tx()
            raise
        else:
            await self.commit_writer_tx()

    async def cleanup_writer_inbox(self, before_ts: float) -> int:
        """清理过期的 inbox 记录(保留最近 N 天)。

        Args:
            before_ts: 删除 created_at < before_ts 的记录

        Returns:
            删除的记录数
        """
        if not self._db:
            return 0
        cursor = await self._db.execute(
            "DELETE FROM writer_inbox WHERE created_at < ?",
            (before_ts,)
        )
        await self._db.commit()
        return cursor.rowcount if cursor else 0

    async def get(self, key: str) -> Optional[dict]:
        """单条读取：从 SQLite 缓存中读取指定 key 的数据"""
        if not self._db:
            return None
        row = await self._db.execute_fetchall(
            "SELECT value FROM cache_backup WHERE key = ?", (key,)
        )
        if row:
            try:
                return json.loads(row[0][0])
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    async def set(self, key: str, data: dict):
        """单条写入：直接写入 SQLite（写穿透时使用）"""
        if not self._db:
            return
        try:
            raw = json.dumps(data, default=str)
            val = raw.decode() if isinstance(raw, bytes) else raw
            await self._db.execute(
                "INSERT OR REPLACE INTO cache_backup (key, value, ts) VALUES (?, ?, ?)",
                (key, val, time.time()),
            )
            await self._db.commit()
        except (TypeError, ValueError) as e:
            logger.warning(f"[CacheStore] set({key}) JSON 序列化失败: {e}")

    async def delete(self, key: str):
        """删除指定 key 的缓存"""
        if not self._db:
            return
        try:
            await self._db.execute(
                "DELETE FROM cache_backup WHERE key = ?", (key,)
            )
            await self._db.commit()
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.debug(f"[CacheStore] 删除缓存失败: {e}")

    async def cleanup(self, max_age_days: int = 30):
        """清理超过 N 天的旧缓存"""
        if not self._db:
            return
        cutoff = time.time() - max_age_days * 86400
        await self._db.execute(
            "DELETE FROM cache_backup WHERE ts < ?", (cutoff,)
        )
        await self._db.commit()

    # ─── 跨进程通知：Up Bot 写入 → Idx Bot 感知 ───

    async def notify_new_upload(self):
        """Up Bot 写入 pending_uploads 后调用，通知 Idx Bot 有新任务。"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO pending_notify (ts) VALUES (?)", (time.time(),)
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] notify_new_upload 失败: {e}")
                break

    async def has_new_upload(self) -> bool:
        """Idx Bot 检查是否有未处理的上传通知。有则返回 True 并原子清空所有通知。"""
        if not self._db:
            return False
        try:
            cursor = await self._db.execute("DELETE FROM pending_notify")
            deleted = cursor.rowcount
            await self._db.commit()
            return deleted > 0
        except Exception:
            return False

    async def wait_for_new_upload(self, timeout: float = 30.0) -> bool:
        """等待上传通知，空闲时使用退避轮询降低 SQLite 空转。"""
        deadline = time.monotonic() + max(timeout, 0)
        delay = 0.2
        while True:
            if await self.has_new_upload():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 5.0)

    # ─── Dsp Bot 通知：Idx Bot 写入 → Dsp Bot 感知 ───

    async def notify_dsp_new_job(self):
        """Idx Bot 写 jobs 表后调用，通知 Dsp Bot 有新任务。"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO dsp_notify (ts) VALUES (?)", (time.time(),)
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] notify_dsp_new_job 失败: {e}")
                break

    async def has_new_dsp_job(self) -> bool:
        """Dsp Bot 检查是否有新的派发通知。有则返回 True 并原子清空所有通知。"""
        if not self._db:
            return False
        try:
            cursor = await self._db.execute("DELETE FROM dsp_notify")
            deleted = cursor.rowcount
            await self._db.commit()
            return deleted > 0
        except Exception:
            return False

    async def wait_for_dsp_job(self, timeout: float = 2.0) -> bool:
        """等待派发通知，避免空队列时高频自旋。"""
        deadline = time.monotonic() + max(timeout, 0)
        delay = 0.1
        while True:
            if await self.has_new_dsp_job():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)

    # ─── 用户 Bot 启动状态 ──────────────────────────────

    async def mark_user_started(self, user_id: int, bot_name: str):
        """标记用户已向指定 bot 发送 /start"""
        if not self._db:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO user_bot_started (user_id, bot_name, started_at) VALUES (?, ?, ?)",
            (user_id, bot_name, time.time()),
        )
        await self._db.commit()

    async def is_user_started(self, user_id: int, bot_name: str) -> bool:
        """检查用户是否已向指定 bot 发送 /start"""
        if not self._db:
            return True  # 数据库不可用时放行，避免阻塞流程
        try:
            row = await self._db.execute_fetchall(
                "SELECT 1 FROM user_bot_started WHERE user_id = ? AND bot_name = ?",
                (user_id, bot_name),
            )
            return bool(row)
        except Exception:
            return True  # 出错时放行

    # ─── 待发送文件码（用户未 /start idx 时暂存）──────────────

    async def add_pending_file_code(self, user_id: int, file_code: str, note: str = "", ext_code: str = ""):
        """暂存文件码，等待用户 /start 后补发"""
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO pending_file_codes (user_id, file_code, note, ext_code, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, file_code, note, ext_code, time.time()),
        )
        await self._db.commit()

    async def get_pending_file_codes(self, user_id: int) -> list[dict]:
        """取出用户的所有待发文件码（不删除，发送成功后调 delete_pending_file_code 删除）"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT id, file_code, note, ext_code FROM pending_file_codes WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        if not rows:
            return []
        return [
            {"id": r[0], "file_code": r[1], "note": r[2] or "", "ext_code": r[3] or ""}
            for r in rows
        ]

    async def delete_pending_file_code(self, row_id: int):
        """发送成功后删除单条暂存文件码"""
        if not self._db:
            return
        await self._db.execute(
            "DELETE FROM pending_file_codes WHERE id = ?",
            (row_id,),
        )
        await self._db.commit()

    # ─── Dsp job 等待用户启动 ──────────────────────────

    async def mark_job_waiting_start(self, job_id: int):
        """将 job 标记为 waiting_start 状态（不计入重试次数）"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET status = 'waiting_start' WHERE crdb_id = ?",
            (job_id,),
        )
        await self._db.commit()

    async def reactivate_waiting_start_jobs(self, user_id: int) -> list[int]:
        """用户 /start dsp 后，将其 waiting_start jobs 改回 pending。
        返回被恢复的 crdb_id 列表(供调用方 xadd 到 Redis Stream)。
        """
        if not self._db:
            return []
        # 先 SELECT 再 UPDATE,获取被恢复的 crdb_id 列表
        rows = await self._db.execute_fetchall(
            "SELECT crdb_id FROM local_job_queue "
            "WHERE target_user_id = ? AND status = 'waiting_start' AND crdb_id > 0",
            (user_id,),
        )
        reactivated_ids = [r[0] for r in rows if r and r[0]]
        if not reactivated_ids:
            return []
        await self._db.execute(
            "UPDATE local_job_queue SET status = 'pending' "
            "WHERE target_user_id = ? AND status = 'waiting_start'",
            (user_id,),
        )
        await self._db.commit()
        # 写入 dsp_notify 触发 worker 重新拉取
        await self._db.execute("INSERT INTO dsp_notify (ts) VALUES (?)", (time.time(),))
        await self._db.commit()
        return reactivated_ids

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def cleanup_notify_tables(self):
        """清理通知表中超过 1 小时的旧记录,防止无限增长。
        
        每 100 条通知插入后自动触发一次清理。
        """
        if not self._db:
            return
        try:
            await self._db.execute(
                "DELETE FROM pending_notify WHERE ts < ?",
                (time.time() - 3600,),
            )
            await self._db.execute(
                "DELETE FROM dsp_notify WHERE ts < ?",
                (time.time() - 3600,),
            )
            await self._db.execute(
                "DELETE FROM cells_change_notify WHERE ts < ?",
                (time.time() - 3600,),
            )
            await self._db.execute(
                "DELETE FROM relay_change_notify WHERE ts < ?",
                (time.time() - 3600,),
            )
            await self._db.commit()
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.debug(f"[CacheStore] 通知表清理失败: {e}")

    # ─── 心跳本地存储：Mon Bot 写入 SQLite，零 CRDB RU ───

    async def write_heartbeat(self, slot_id: str, ok: bool, _batch: bool = False):
        """写入本地心跳记录。ok=True 时重置 fail_streak，ok=False 时递增。
        同时更新 cells_local.last_heartbeat（ISO 格式）供降级 cooldown 判断使用。

        _batch=True 时不调用 commit(由调用方批量完成后统一 commit),
        减少 commit 次数避免多进程 SQLite 锁冲突。
        """
        if not self._db:
            return
        now = time.time()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for attempt in range(3):
            try:
                if ok:
                    await self._db.execute(
                        "INSERT INTO heartbeat_local (slot_id, last_ok, fail_streak) VALUES (?, ?, 0) "
                        "ON CONFLICT(slot_id) DO UPDATE SET last_ok = ?, fail_streak = 0",
                        (slot_id, now, now),
                    )
                else:
                    await self._db.execute(
                        "INSERT INTO heartbeat_local (slot_id, last_ok, fail_streak) VALUES (?, ?, 1) "
                        "ON CONFLICT(slot_id) DO UPDATE SET last_ok = ?, fail_streak = fail_streak + 1",
                        (slot_id, now, now),
                    )
                await self._db.execute(
                    "UPDATE cells_local SET last_heartbeat = ?, updated_at = ? WHERE slot_id = ?",
                    (now_iso, now, slot_id),
                )
                if not _batch:
                    await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                # 非锁冲突或重试耗尽,静默跳过(心跳下一轮会补上)
                if self._in_writer_tx:
                    raise
                return

    async def commit(self):
        """显式 commit(批量操作后调用)。"""
        if self._db:
            await self._db.commit()

    async def get_all_heartbeats(self) -> dict[str, dict]:
        """读取所有本地心跳记录，返回 {slot_id: {last_ok, fail_streak}}。
        用于 Mon Bot 启动时恢复到内存。
        """
        if not self._db:
            return {}
        rows = await self._db.execute_fetchall(
            "SELECT slot_id, last_ok, fail_streak FROM heartbeat_local"
        )
        return {row[0]: {"last_ok": row[1], "fail_streak": row[2]} for row in rows}

    # ─── 跨进程 Bot 心跳：各 Bot 写入，admin_bot 读取 ───

    async def write_bot_heartbeat(self, name: str, total_processed: int = 0, total_errors: int = 0):
        """写入 Bot 心跳。由各 Bot 独立进程定期调用。"""
        if not self._db:
            return
        now = time.time()
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR REPLACE INTO bot_heartbeat (name, last_ping, is_running, total_processed, total_errors) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (name, now, total_processed, total_errors),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return  # 静默失败

    async def get_all_bot_heartbeats(self) -> dict[str, dict]:
        """读取所有 Bot 心跳记录。
        返回 {name: {last_ping, is_running, total_processed, total_errors}}。
        """
        if not self._db:
            return {}
        rows = await self._db.execute_fetchall(
            "SELECT name, last_ping, is_running, total_processed, total_errors FROM bot_heartbeat"
        )
        return {
            row[0]: {
                "last_ping": row[1],
                "is_running": bool(row[2]),
                "total_processed": row[3],
                "total_errors": row[4],
            }
            for row in rows
        }

    # ─── 用户配额本地存储：Idx Bot 读写零 RU ───

    async def get_user_quota(self, user_id: int) -> dict | None:
        """从 SQLite 读取用户配额。未找到返回 None。

        R40 P1-12: 查询异常时 fail-closed 返回 0 配额 dict(拒绝操作,而非放行)。
        """
        if not self._db:
            return None
        try:
            rows = await self._db.execute_fetchall(
                "SELECT user_id, level, daily_quota, used_today, quota_date, "
                "ext_quota, ext_used_today, ext_quota_date, synced_at "
                "FROM user_quota WHERE user_id = ?",
                (user_id,),
            )
            if not rows:
                return None
            r = rows[0]
            return {
                "user_id": r[0],
                "level": r[1],
                "daily_quota": r[2],
                "used_today": r[3],
                "quota_date": r[4],
                "ext_quota": r[5],
                "ext_used_today": r[6],
                "ext_quota_date": r[7],
                "synced_at": r[8],
            }
        except Exception as e:
            # R40 P1-12: fail-closed — 查询异常时返回 0 配额 dict,拒绝操作
            logger.warning(
                f"[CacheStore] get_user_quota 查询异常 user_id={user_id}: {e},"
                f"按 fail-closed 返回 0 配额"
            )
            return {
                "user_id": user_id,
                "level": "free",
                "daily_quota": 0,
                "used_today": 0,
                "quota_date": "",
                "ext_quota": 0,
                "ext_used_today": 0,
                "ext_quota_date": "",
                "synced_at": 0,
            }

    async def upsert_user_quota(self, user_id: int, data: dict):
        """写入或更新用户配额到 SQLite。"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR REPLACE INTO user_quota "
                    "(user_id, level, daily_quota, used_today, quota_date, "
                    "ext_quota, ext_used_today, ext_quota_date, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        data.get("level", "free"),
                        data.get("daily_quota", 3),
                        data.get("used_today", 0),
                        data.get("quota_date"),
                        data.get("ext_quota", 0),
                        data.get("ext_used_today", 0),
                        data.get("ext_quota_date"),
                        data.get("synced_at", 0),
                    ),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return

    async def increment_user_quota_used(self, user_id: int, is_external: bool = False):
        """原子递增用户已用配额（不涉及 CRDB）。"""
        if not self._db:
            return
        col = "used_today" if not is_external else "ext_used_today"
        # 白名单校验，防止拼接注入
        if col not in ("used_today", "ext_used_today"):
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    f"UPDATE user_quota SET {col} = {col} + 1 WHERE user_id = ?",
                    (user_id,),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return

    async def try_consume_quota(self, user_id: int, is_external: bool = False) -> bool:
        """原子条件递增配额(乐观扣减),解决 TOCTOU 竞态。

        - Premium 用户(level='premium')跳过扣减,直接返回 True
        - 配额为 -1(不限) 跳过扣减,直接返回 True
        - 配额为 0(禁止) 返回 False
        - 已达上限返回 False
        - 成功扣减返回 True

        通过单条 UPDATE 的 WHERE 子句保证原子性,
        避免并发场景下多个请求同时通过 check 后超额。
        """
        if not self._db:
            return False
        col = "used_today" if not is_external else "ext_used_today"
        quota_col = "daily_quota" if not is_external else "ext_quota"
        # 白名单校验,防止拼接注入
        if col not in ("used_today", "ext_used_today") or quota_col not in ("daily_quota", "ext_quota"):
            return False
        for attempt in range(3):
            try:
                # premium 用户不扣减(used_today 保持不变);quota=-1 不限量不扣减;quota=0 禁止;否则 used < quota 才扣
                # 用 CASE 表达式让 premium 用户的 {col} 不递增,避免计数器只增不减(refund_quota 也不退 premium)
                # UPDATE 仍匹配行(rowcount=1),保证返回 True
                cursor = await self._db.execute(
                    f"UPDATE user_quota SET {col} = CASE WHEN level = 'premium' THEN {col} ELSE {col} + 1 END "
                    f"WHERE user_id = ? "
                    f"AND (level = 'premium' OR {quota_col} = -1 OR "
                    f"({quota_col} > 0 AND {col} < {quota_col}))",
                    (user_id,),
                )
                await self._db.commit()
                # rowcount: 1=成功扣减或premium跳过, 0=配额已满或用户不存在
                # 需进一步区分:premium 用户存在但跳过扣减的情况
                rc = cursor.rowcount if cursor else 0
                if rc > 0:
                    return True
                # rc=0:可能是 premium 用户但行不存在(已被删除),或配额已满,或用户不存在
                # 查询确认用户是否存在以及 level
                rows = await self._db.execute_fetchall(
                    f"SELECT level, {quota_col}, {col} FROM user_quota WHERE user_id = ?",
                    (user_id,),
                )
                if not rows:
                    return False  # 用户不存在
                level, q, used = rows[0]
                if level == "premium" or q == -1:
                    return True  # premium/不限量,跳过扣减视为成功
                return False  # 配额已满
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return False

    async def refund_quota(self, user_id: int, is_external: bool = False):
        """投递失败时回滚配额(递减),与 try_consume_quota 配对使用。

        - Premium 用户不操作(未扣减)
        - 配额为 -1 不操作(未扣减)
        - 正常用户递减 1,但不低于 0
        """
        if not self._db:
            return
        col = "used_today" if not is_external else "ext_used_today"
        quota_col = "daily_quota" if not is_external else "ext_quota"
        if col not in ("used_today", "ext_used_today") or quota_col not in ("daily_quota", "ext_quota"):
            return
        for attempt in range(3):
            try:
                # 只对非 premium、非 -1 配额的用户递减,且不低于 0
                await self._db.execute(
                    f"UPDATE user_quota SET {col} = MAX(0, {col} - 1) "
                    f"WHERE user_id = ? AND level != 'premium' AND {quota_col} != -1",
                    (user_id,),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return

    async def get_unsynced_quotas(self, min_synced_at: float = 0) -> list[dict]:
        """获取需要同步到 CRDB 的配额记录。"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT user_id, level, daily_quota, used_today, quota_date, "
            "ext_quota, ext_used_today, ext_quota_date "
            "FROM user_quota WHERE synced_at <= ?",
            (min_synced_at,),
        )
        return [
            {
                "user_id": r[0],
                "level": r[1],
                "daily_quota": r[2],
                "used_today": r[3],
                "quota_date": r[4],
                "ext_quota": r[5],
                "ext_used_today": r[6],
                "ext_quota_date": r[7],
            }
            for r in rows
        ]

    async def mark_quota_synced(self, user_id: int):
        """标记配额已同步到 CRDB。"""
        if not self._db:
            return
        now = time.time()
        await self._db.execute(
            "UPDATE user_quota SET synced_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        await self._db.commit()

    async def invalidate_user_quota(self, user_id: int):
        """使 SQLite 配额缓存失效，下次访问从 CRDB 重新加载。"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE user_quota SET synced_at = 0 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.commit()

    # ════════════════════════════════════════════════════════════════
    # M1 业务闭环: upload_sessions / upload_outbox / quota_ledger /
    #              delivery_receipts / replication_tasks
    # ════════════════════════════════════════════════════════════════

    # ─── M1-1: upload_sessions 上传会话状态机(7 个方法) ───

    async def create_upload_session(
        self, upload_id: str, user_id: int,
        source_msg_ids: list | None = None,
        options_json: dict | None = None,
        trace_id: str = "",
    ) -> bool:
        """创建上传会话,初始状态 RECEIVED。

        Args:
            upload_id: UUID 主键
            user_id: 发起用户 ID
            source_msg_ids: 用户原消息 ID 列表(JSON 序列化存储)
            options_json: 上传选项(protect_content/ttl/note 等,JSON 序列化)
            trace_id: 链路追踪 ID

        Returns:
            True 表示创建成功(或已存在,幂等)

        Raises:
            StoreUnavailable: R39 P0-5 当 _db 未初始化或 upload_id 为空时抛出,
                替代原先静默 return None,避免 strict 调用方误判为成功。
        """
        # R39 P0-5: 静默 return 改抛 StoreUnavailable,避免 strict 调用方绕过检查
        if not self._db or not upload_id:
            from utils.exceptions import StoreUnavailable
            raise StoreUnavailable(
                f"create_upload_session: store unavailable "
                f"(db_init={self._db is not None}, upload_id_empty={not upload_id})"
            )
        now = time.time()
        src_json = _m1_json_dumps(source_msg_ids) if source_msg_ids is not None else None
        opt_json = _m1_json_dumps(options_json) if options_json is not None else None
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO upload_sessions "
                    "(upload_id, user_id, source_msg_ids, options_json, trace_id, "
                    " status, prev_status, transitioned_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'RECEIVED', NULL, ?, ?, ?)",
                    (upload_id, user_id, src_json, opt_json, trace_id,
                     now, now, now),
                )
                await self._db.commit()
                return True
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                # R39 P0-5: 重试耗尽后抛 StoreUnavailable,不再静默 return
                from utils.exceptions import StoreUnavailable
                raise StoreUnavailable(
                    f"create_upload_session: failed after retries (upload_id={upload_id}): {e}"
                ) from e
        # 兜底返回 False(理论上不会到达,for 循环内必有 return 或 raise)
        return False

    async def get_upload_session(self, upload_id: str) -> dict | None:
        """按主键查询上传会话,未找到返回 None。"""
        if not self._db or not upload_id:
            return None
        try:
            rows = await self._db.execute_fetchall(
                "SELECT upload_id, user_id, source_msg_ids, primary_channel_id, "
                "primary_msg_ids, media_group_id, options_json, trace_id, status, "
                "prev_status, transitioned_at, transition_reason, lease_owner, "
                "lease_until, last_error, created_at, updated_at "
                "FROM upload_sessions WHERE upload_id = ?",
                (upload_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_upload_session 异常: {e}")
            return None
        if not rows:
            return None
        r = rows[0]
        return {
            "upload_id": r[0],
            "user_id": r[1],
            "source_msg_ids": _m1_json_loads(r[2]),
            "primary_channel_id": r[3],
            "primary_msg_ids": _m1_json_loads(r[4]),
            "media_group_id": r[5],
            "options_json": _m1_json_loads(r[6]),
            "trace_id": r[7],
            "status": r[8],
            "prev_status": r[9],
            "transitioned_at": r[10],
            "transition_reason": r[11],
            "lease_owner": r[12],
            "lease_until": r[13],
            "last_error": r[14],
            "created_at": r[15],
            "updated_at": r[16],
        }

    async def get_active_upload_sessions_by_user(self, user_id: int) -> list[dict]:
        """查询用户的活跃会话(status NOT IN READY/ABORTED/EXPIRED)。"""
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT upload_id, user_id, status, created_at, updated_at "
                "FROM upload_sessions WHERE user_id = ? "
                "AND status NOT IN ('READY', 'ABORTED', 'EXPIRED') "
                "ORDER BY created_at DESC",
                (user_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_active_upload_sessions_by_user 异常: {e}")
            return []
        cols = ["upload_id", "user_id", "status", "created_at", "updated_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def transition_upload_session(
        self, upload_id: str, new_status: str,
        reason: str = "", **update_fields,
    ) -> bool:
        """状态机迁移:原子条件 UPDATE WHERE upload_id=? AND status != new_status。

        更新 status / prev_status / transitioned_at / transition_reason / updated_at
        + update_fields 中的额外字段(白名单校验)。

        Returns: True 表示迁移成功(rowcount>0),False 表示会话不存在或已在目标状态。

        Raises:
            StoreUnavailable: R39 P0-5 当 _db 未初始化或 upload_id 为空时抛出,
                替代原先静默 return False,避免 strict 调用方误判为状态已推进。
        """
        # R39 P0-5: 静默 return False 改抛 StoreUnavailable,避免 strict 调用方绕过检查
        if not self._db or not upload_id:
            from utils.exceptions import StoreUnavailable
            raise StoreUnavailable(
                f"transition_upload_session: store unavailable "
                f"(db_init={self._db is not None}, upload_id_empty={not upload_id})"
            )
        # 白名单校验可更新的列,防止 SQL 注入
        _ALLOWED_UPDATE_COLS = {
            "primary_channel_id", "primary_msg_ids", "media_group_id",
            "options_json", "last_error", "source_msg_ids",
            "lease_owner", "lease_until",
        }
        now = time.time()
        set_parts = [
            "status = ?",
            "prev_status = (SELECT status FROM upload_sessions WHERE upload_id = ?)",
            "transitioned_at = ?",
            "transition_reason = ?",
            "updated_at = ?",
        ]
        params: list = [new_status, upload_id, now, reason, now]
        # 处理额外字段(白名单校验)
        for k, v in update_fields.items():
            if k not in _ALLOWED_UPDATE_COLS:
                logger.warning(f"[CacheStore] transition_upload_session 跳过非法列: {k}")
                continue
            # JSON 字段需要序列化
            if k in ("source_msg_ids", "primary_msg_ids", "options_json") and v is not None:
                v = _m1_json_dumps(v)
            set_parts.append(f"{k} = ?")
            params.append(v)
        params.append(upload_id)
        sql = (
            "UPDATE upload_sessions SET " + ", ".join(set_parts) +
            " WHERE upload_id = ? AND status != ?"
        )
        params.append(new_status)
        for attempt in range(3):
            try:
                cursor = await self._db.execute(sql, params)
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def lease_upload_session(
        self, upload_id: str, owner: str, lease_seconds: int,
    ) -> bool:
        """租约会话:UPDATE lease_owner/lease_until WHERE
        (lease_until < now OR lease_owner = owner)。

        Returns: True 表示租约成功,False 表示已被其他 owner 持有。
        """
        if not self._db or not upload_id:
            return False
        now = time.time()
        lease_until = now + lease_seconds
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_sessions SET lease_owner = ?, lease_until = ?, "
                    "updated_at = ? WHERE upload_id = ? "
                    "AND (lease_until IS NULL OR lease_until < ? OR lease_owner = ?)",
                    (owner, lease_until, now, upload_id, now, owner),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def cleanup_expired_upload_sessions(self, ttl_seconds: int) -> int:
        """清理租约过期且未完成的会话(status='EXPIRED')。

        Args:
            ttl_seconds: 超出 lease_until 多少秒才视为过期(now - ttl)
        Returns: 标记为 EXPIRED 的行数。
        """
        if not self._db:
            return 0
        now = time.time()
        cutoff = now - ttl_seconds
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_sessions SET status = 'EXPIRED', "
                    "prev_status = status, transitioned_at = ?, "
                    "transition_reason = 'lease_expired', updated_at = ? "
                    "WHERE status IN ('RECEIVED', 'COPIED_PRIMARY', 'MANIFESTED', "
                    "'OPTIONS_PENDING', 'INDEX_PENDING') "
                    "AND lease_until IS NOT NULL AND lease_until < ?",
                    (now, now, cutoff),
                )
                await self._db.commit()
                return cursor.rowcount if cursor else 0
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return 0

    async def delete_upload_session(self, upload_id: str) -> bool:
        """删除会话(仅 READY/ABORTED/EXPIRED 状态可删)。

        Returns: True 表示删除成功,False 表示状态不允许或会话不存在。
        """
        if not self._db or not upload_id:
            return False
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "DELETE FROM upload_sessions WHERE upload_id = ? "
                    "AND status IN ('READY', 'ABORTED', 'EXPIRED')",
                    (upload_id,),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    # ─── M1-2: upload_outbox 事务发件箱(6 个方法) ───

    async def create_outbox_entry(
        self, outbox_id: str, upload_id: str, code: str,
        target_user_id: int, storage_channel_id: int,
        storage_msg_ids: list | None = None,
        batch_file_meta: list | None = None,
        task_type: str = "single", protect_content: int = 0,
        event_type: str = "delivery_requested",
    ) -> bool:
        """创建发件箱条目,初始状态 PENDING。

        幂等:使用 INSERT OR IGNORE 避免重复插入(同 outbox_id 已存在则跳过)。

        Returns:
            True 表示创建成功(或已存在,幂等)

        Raises:
            StoreUnavailable: R39 P0-5 当 _db 未初始化或 outbox_id 为空时抛出,
                替代原先静默 return None,避免 strict 调用方误判为成功。
        """
        # R39 P0-5: 静默 return 改抛 StoreUnavailable,避免 strict 调用方绕过检查
        if not self._db or not outbox_id:
            from utils.exceptions import StoreUnavailable
            raise StoreUnavailable(
                f"create_outbox_entry: store unavailable "
                f"(db_init={self._db is not None}, outbox_id_empty={not outbox_id})"
            )
        now = time.time()
        sm_json = _m1_json_dumps(storage_msg_ids) if storage_msg_ids is not None else None
        bfm_json = _m1_json_dumps(batch_file_meta) if batch_file_meta is not None else None
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO upload_outbox "
                    "(outbox_id, upload_id, job_id, code, target_user_id, "
                    " storage_channel_id, storage_msg_ids, batch_file_meta, "
                    " task_type, protect_content, event_type, status, attempts, "
                    " next_retry_at, created_at, processed_at) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, NULL, ?, NULL)",
                    (outbox_id, upload_id, code, target_user_id, storage_channel_id,
                     sm_json, bfm_json, task_type, protect_content, event_type, now),
                )
                await self._db.commit()
                return True
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                # R39 P0-5: 重试耗尽后抛 StoreUnavailable,不再静默 return
                from utils.exceptions import StoreUnavailable
                raise StoreUnavailable(
                    f"create_outbox_entry: failed after retries (outbox_id={outbox_id}): {e}"
                ) from e
        # 兜底返回 False(理论上不会到达,for 循环内必有 return 或 raise)
        return False

    async def get_pending_outbox(self, limit: int = 10) -> list[dict]:
        """查询待处理发件箱条目(status='PENDING' 且 next_retry_at 已到或为 NULL)。"""
        if not self._db:
            return []
        now = time.time()
        try:
            rows = await self._db.execute_fetchall(
                "SELECT outbox_id, upload_id, job_id, code, target_user_id, "
                "storage_channel_id, storage_msg_ids, batch_file_meta, "
                "task_type, protect_content, event_type, status, attempts, "
                "next_retry_at, created_at, processed_at "
                "FROM upload_outbox WHERE status = 'PENDING' "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY created_at LIMIT ?",
                (now, limit),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_pending_outbox 异常: {e}")
            return []
        cols = ["outbox_id", "upload_id", "job_id", "code", "target_user_id",
                "storage_channel_id", "storage_msg_ids", "batch_file_meta",
                "task_type", "protect_content", "event_type", "status",
                "attempts", "next_retry_at", "created_at", "processed_at"]
        results = []
        for r in rows:
            d = dict(zip(cols, r))
            d["storage_msg_ids"] = _m1_json_loads(d.get("storage_msg_ids"))
            d["batch_file_meta"] = _m1_json_loads(d.get("batch_file_meta"))
            results.append(d)
        return results

    async def mark_outbox_dispatched(self, outbox_id: str, job_id: int) -> bool:
        """标记发条为已派工(status='DISPATCHED')。"""
        if not self._db or not outbox_id:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_outbox SET status = 'DISPATCHED', "
                    "job_id = ?, processed_at = ? WHERE outbox_id = ? "
                    "AND status = 'PENDING'",
                    (job_id, now, outbox_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_outbox_done(self, outbox_id: str) -> bool:
        """标记发条为已完成(status='DONE')。"""
        if not self._db or not outbox_id:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_outbox SET status = 'DONE', processed_at = ? "
                    "WHERE outbox_id = ? AND status IN ('PENDING', 'DISPATCHED')",
                    (now, outbox_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_outbox_failed(
        self, outbox_id: str, reason: str, next_retry_at: float,
        max_attempts: int = 0,
    ) -> bool:
        """标记发条为失败(attempts+1, next_retry_at=next_retry_at)。

        本方法 increments attempts 并设置 next_retry_at(重试等待)。
        当 max_attempts > 0 且 attempts+1 >= max_attempts 时,
        自动将 status 置为 'DEAD'(永久失败,不再重试)。
        否则将 status 重置为 'PENDING'(让 worker 在 next_retry_at 到达后重新扫描)。
        若需要直接终止,调用 mark_outbox_dead()。
        reason 仅记录到日志(upload_outbox 表无 last_error 列)。
        """
        if not self._db or not outbox_id:
            return False
        now = time.time()
        logger.info(f"[CacheStore] outbox {outbox_id} 失败: {reason}, next_retry_at={next_retry_at}")
        for attempt in range(3):
            try:
                if max_attempts > 0:
                    # R36 B0-2: 超过 max_attempts 时置为 DEAD(幂等:不再被 claim 扫描到)
                    # 否则重置为 PENDING(等 next_retry_at 到达后重新扫描)
                    cursor = await self._db.execute(
                        "UPDATE upload_outbox SET attempts = attempts + 1, "
                        "next_retry_at = ?, processed_at = ?, "
                        "status = CASE WHEN attempts + 1 >= ? THEN 'DEAD' ELSE 'PENDING' END, "
                        "lease_owner = NULL, lease_until = NULL "
                        "WHERE outbox_id = ?",
                        (next_retry_at, now, max_attempts, outbox_id),
                    )
                else:
                    # 不传 max_attempts 时重置为 PENDING(等 next_retry_at 到达后重试)
                    cursor = await self._db.execute(
                        "UPDATE upload_outbox SET attempts = attempts + 1, "
                        "next_retry_at = ?, processed_at = ?, status = 'PENDING', "
                        "lease_owner = NULL, lease_until = NULL "
                        "WHERE outbox_id = ?",
                        (next_retry_at, now, outbox_id),
                    )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def claim_outbox_entry(
        self, outbox_id: str, owner: str, lease_seconds: int = 60,
    ) -> bool:
        """R36 B0-2: CAS claim outbox 条目,获取独占执行权。

        UPDATE upload_outbox SET status='DISPATCHED', lease_owner=?, lease_until=?
        WHERE outbox_id=? AND status='PENDING'
        AND (lease_until IS NULL OR lease_until < ? OR lease_owner=?)

        Returns:
            True 表示 claim 成功(本 owner 获得独占执行权);
            False 表示已被其他 worker 抢占或状态已变更。
        """
        if not self._db or not outbox_id:
            return False
        now = time.time()
        lease_until = now + lease_seconds
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_outbox SET status = 'DISPATCHED', "
                    "lease_owner = ?, lease_until = ?, processed_at = ? "
                    "WHERE outbox_id = ? AND status = 'PENDING' "
                    "AND (lease_until IS NULL OR lease_until < ? OR lease_owner = ?)",
                    (owner, lease_until, now, outbox_id, now, owner),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_outbox_dead(self, outbox_id: str, reason: str = "") -> bool:
        """R36 B0-2: 标记发条为 DEAD(永久失败,不再重试)。

        用于 OutboxWorker 在 max_attempts 超出后的终态标记。
        """
        if not self._db or not outbox_id:
            return False
        now = time.time()
        if reason:
            logger.warning(f"[CacheStore] outbox {outbox_id} 置为 DEAD: {reason}")
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_outbox SET status = 'DEAD', "
                    "processed_at = ? WHERE outbox_id = ? "
                    "AND status IN ('PENDING', 'DISPATCHED')",
                    (now, outbox_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def reset_stale_outbox(self, current_owner: str = "") -> int:
        """R36 B0-2: 重置 DISPATCHED 但 lease 已过期的 outbox 条目为 PENDING。

        用于 OutboxWorker 崩溃恢复:某 worker 持有 lease 但未完成,
        lease 过期后其他 worker 可重新 claim 这些条目。

        Args:
            current_owner: 当前 worker 的 owner 名,避免重置自己正在处理的条目。

        Returns:
            被重置为 PENDING 的行数。
        """
        if not self._db:
            return 0
        now = time.time()
        for attempt in range(3):
            try:
                if current_owner:
                    cursor = await self._db.execute(
                        "UPDATE upload_outbox SET status = 'PENDING', "
                        "lease_owner = NULL, lease_until = NULL "
                        "WHERE status = 'DISPATCHED' "
                        "AND lease_until IS NOT NULL AND lease_until < ? "
                        "AND (lease_owner IS NULL OR lease_owner != ?)",
                        (now, current_owner),
                    )
                else:
                    cursor = await self._db.execute(
                        "UPDATE upload_outbox SET status = 'PENDING', "
                        "lease_owner = NULL, lease_until = NULL "
                        "WHERE status = 'DISPATCHED' "
                        "AND lease_until IS NOT NULL AND lease_until < ?",
                        (now,),
                    )
                await self._db.commit()
                return cursor.rowcount if cursor else 0
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return 0

    async def get_dispatched_outbox_by_owner(
        self, owner: str, limit: int = 10,
    ) -> list[dict]:
        """R36 B0-2: 查询某 owner 持有 lease 但仍处于 DISPATCHED 状态的条目。

        用于 OutboxWorker 重启后恢复未完成的 claim(租约未过期则跳过,
        租约已过期但状态仍为 DISPATCHED 的可重新接管)。
        """
        if not self._db or not owner:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT outbox_id, upload_id, job_id, code, target_user_id, "
                "storage_channel_id, storage_msg_ids, batch_file_meta, "
                "task_type, protect_content, event_type, status, attempts, "
                "next_retry_at, created_at, processed_at, lease_owner, lease_until "
                "FROM upload_outbox WHERE status = 'DISPATCHED' "
                "AND lease_owner = ? ORDER BY created_at LIMIT ?",
                (owner, limit),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_dispatched_outbox_by_owner 异常: {e}")
            return []
        cols = ["outbox_id", "upload_id", "job_id", "code", "target_user_id",
                "storage_channel_id", "storage_msg_ids", "batch_file_meta",
                "task_type", "protect_content", "event_type", "status",
                "attempts", "next_retry_at", "created_at", "processed_at",
                "lease_owner", "lease_until"]
        results = []
        for r in rows:
            d = dict(zip(cols, r))
            d["storage_msg_ids"] = _m1_json_loads(d.get("storage_msg_ids"))
            d["batch_file_meta"] = _m1_json_loads(d.get("batch_file_meta"))
            results.append(d)
        return results

    async def get_outbox_by_upload(self, upload_id: str) -> list[dict]:
        """查询某 upload_id 关联的所有发件箱条目。"""
        if not self._db or not upload_id:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT outbox_id, upload_id, job_id, code, target_user_id, "
                "storage_channel_id, storage_msg_ids, batch_file_meta, "
                "task_type, protect_content, event_type, status, attempts, "
                "next_retry_at, created_at, processed_at "
                "FROM upload_outbox WHERE upload_id = ? ORDER BY created_at",
                (upload_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_outbox_by_upload 异常: {e}")
            return []
        cols = ["outbox_id", "upload_id", "job_id", "code", "target_user_id",
                "storage_channel_id", "storage_msg_ids", "batch_file_meta",
                "task_type", "protect_content", "event_type", "status",
                "attempts", "next_retry_at", "created_at", "processed_at"]
        results = []
        for r in rows:
            d = dict(zip(cols, r))
            d["storage_msg_ids"] = _m1_json_loads(d.get("storage_msg_ids"))
            d["batch_file_meta"] = _m1_json_loads(d.get("batch_file_meta"))
            results.append(d)
        return results

    # ─── M1-3: quota_ledger 配额变更流水(3 个方法) ───

    async def append_quota_ledger(
        self, user_id: int, event_type: str, is_external: int = 0,
        quota_before: int | None = None, quota_after: int | None = None,
        request_id: str = "", reason: str = "", tx=None,
    ) -> None:
        """追加配额变更流水(INSERT,自增主键)。

        R40 P0-5: 支持 tx 参数(同事务写入),不自动 commit。

        Args:
            event_type: consume/refund/sync/reset/expire
            request_id: 业务幂等键(可选,用于去重检查)
            tx: 事务连接(aiosqlite.Connection),传入时不自动 commit
        """
        if not self._db:
            return
        now = time.time()
        # R40 P0-5: 有 tx 时直接写入,不自动 commit(由外层事务统一控制)
        if tx is not None:
            try:
                await tx.execute(
                    "INSERT INTO quota_ledger "
                    "(user_id, event_type, is_external, quota_before, quota_after, "
                    " request_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, event_type, is_external, quota_before, quota_after,
                     request_id or None, reason or None, now),
                )
            except Exception as e:
                logger.warning(f"[CacheStore] append_quota_ledger(tx) 失败: {e}")
                raise
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO quota_ledger "
                    "(user_id, event_type, is_external, quota_before, quota_after, "
                    " request_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, event_type, is_external, quota_before, quota_after,
                     request_id or None, reason or None, now),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return

    async def get_quota_ledger(self, user_id: int, limit: int = 100) -> list[dict]:
        """查询用户配额流水(按时间倒序,默认 100 条)。"""
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT ledger_id, user_id, event_type, is_external, "
                "quota_before, quota_after, request_id, reason, created_at "
                "FROM quota_ledger WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_quota_ledger 异常: {e}")
            return []
        cols = ["ledger_id", "user_id", "event_type", "is_external",
                "quota_before", "quota_after", "request_id", "reason", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def get_quota_ledger_by_request(self, request_id: str) -> list[dict]:
        """按 request_id 查询流水(幂等检查,返回所有匹配记录)。"""
        if not self._db or not request_id:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT ledger_id, user_id, event_type, is_external, "
                "quota_before, quota_after, request_id, reason, created_at "
                "FROM quota_ledger WHERE request_id = ? "
                "ORDER BY created_at",
                (request_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_quota_ledger_by_request 异常: {e}")
            return []
        cols = ["ledger_id", "user_id", "event_type", "is_external",
                "quota_before", "quota_after", "request_id", "reason", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    # ─── M1-4: delivery_receipts 投递回执(5 个方法) ───

    async def upsert_delivery_receipt(
        self, job_id: int, source_msg_id: int, target_user_id: int,
        sent_msg_id: int | None = None, media_group_id: str = "",
        group_receipt_id: str = "", status: str = "SENT",
        delivery_token: str = "",
    ) -> None:
        """写入或更新投递回执(基于 UNIQUE(job_id, source_msg_id))。

        INSERT OR REPLACE 会删除旧记录并插入新记录(自增 receipt_id 会变)。
        若需保留 receipt_id,应改用 UPDATE。本方法用于首次写入和重试场景。

        R37 P2-5: delivery_token 参数支持 effectively-once 幂等。
        """
        if not self._db:
            return
        now = time.time()
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR REPLACE INTO delivery_receipts "
                    "(job_id, source_msg_id, target_user_id, sent_msg_id, "
                    " media_group_id, group_receipt_id, status, attempts, "
                    " error_reason, created_at, confirmed_at, delivery_token) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, NULL, ?)",
                    (job_id, source_msg_id, target_user_id, sent_msg_id,
                     media_group_id or "", group_receipt_id or "", status, now,
                     delivery_token or ""),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return

    async def is_delivery_already_done(self, delivery_token: str) -> bool:
        """R37 P2-5: 检查 delivery_token 是否已有成功投递记录。

        通过 token 查询 delivery_receipts 表,若存在 status IN ('SENT', 'CONFIRMED')
        且 delivery_token 匹配的记录,则视为已投递(effectively-once 跳过)。

        Args:
            delivery_token: SHA-256(file_code | target_user_id | job_id) hex

        Returns:
            True = 已投递过(跳过), False = 未投递或 token 为空
        """
        if not self._db or not delivery_token:
            return False
        try:
            async with self._db.execute(
                "SELECT 1 FROM delivery_receipts "
                "WHERE delivery_token = ? AND status IN ('SENT', 'CONFIRMED') "
                "LIMIT 1",
                (delivery_token,),
            ) as cursor:
                row = await cursor.fetchone()
            return row is not None
        except Exception as e:
            logger.warning(f"[CacheStore] is_delivery_already_done 异常: {e}")
            return False

    async def get_delivery_receipts_by_job(self, job_id: int) -> list[dict]:
        """查询某 job 的所有投递回执。"""
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT receipt_id, job_id, source_msg_id, target_user_id, "
                "sent_msg_id, media_group_id, group_receipt_id, status, "
                "attempts, error_reason, created_at, confirmed_at, delivery_token "
                "FROM delivery_receipts WHERE job_id = ? "
                "ORDER BY source_msg_id",
                (job_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_delivery_receipts_by_job 异常: {e}")
            return []
        cols = ["receipt_id", "job_id", "source_msg_id", "target_user_id",
                "sent_msg_id", "media_group_id", "group_receipt_id", "status",
                "attempts", "error_reason", "created_at", "confirmed_at",
                "delivery_token"]
        return [dict(zip(cols, r)) for r in rows]

    async def confirm_delivery_receipt(
        self, job_id: int, source_msg_id: int, sent_msg_id: int,
    ) -> bool:
        """确认投递回执(status='CONFIRMED', confirmed_at=now)。"""
        if not self._db:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE delivery_receipts SET status = 'CONFIRMED', "
                    "confirmed_at = ?, sent_msg_id = COALESCE(?, sent_msg_id) "
                    "WHERE job_id = ? AND source_msg_id = ?",
                    (now, sent_msg_id, job_id, source_msg_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_delivery_failed(
        self, job_id: int, source_msg_id: int, reason: str,
    ) -> bool:
        """标记投递失败(status='FAILED', attempts+1, error_reason=reason)。"""
        if not self._db:
            return False
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE delivery_receipts SET status = 'FAILED', "
                    "attempts = attempts + 1, error_reason = ? "
                    "WHERE job_id = ? AND source_msg_id = ?",
                    (reason, job_id, source_msg_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def get_sent_msg_ids_for_job(self, job_id: int) -> list[int]:
        """查询 job 已成功发送的 sent_msg_id 列表(status IN SENT/CONFIRMED)。

        替代 _sent_msg_tracker 内存态的查询接口。
        """
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT sent_msg_id FROM delivery_receipts "
                "WHERE job_id = ? AND status IN ('SENT', 'CONFIRMED') "
                "AND sent_msg_id IS NOT NULL",
                (job_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_sent_msg_ids_for_job 异常: {e}")
            return []
        return [r[0] for r in rows if r[0] is not None]

    # ─── M1-5: replication_tasks 副本复制任务(6 个方法) ───

    async def create_replication_task(
        self, group_id: int, file_unique_id: str, src_channel_id: int,
        dst_channel_id: int, src_msg_id: int, media_group_id: str = "",
        task_type: str = "replica", priority: int = 5,
    ) -> int:
        """创建副本复制任务(INSERT OR IGNORE 幂等),返回 task_id(0 表示失败或已存在)。

        基于 UNIQUE(group_id, file_unique_id, src_channel_id, dst_channel_id) 去重。
        若任务已存在(INSERT OR IGNORE 跳过),返回已有记录的 task_id。
        """
        if not self._db or not file_unique_id:
            return 0
        now = time.time()
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO replication_tasks "
                    "(group_id, file_unique_id, src_channel_id, dst_channel_id, "
                    " src_msg_id, dst_msg_id, media_group_id, task_type, priority, "
                    " status, prev_status, attempts, max_attempts, next_retry_at, "
                    " last_error, created_at, updated_at, committed_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'PLANNED', NULL, 0, 3, "
                    " NULL, NULL, ?, ?, NULL)",
                    (group_id, file_unique_id, src_channel_id, dst_channel_id,
                     src_msg_id, media_group_id or "", task_type, priority, now, now),
                )
                await self._db.commit()
                # 获取 task_id(新插入的用 lastrowid,已存在的用 SELECT)
                cursor = await self._db.execute(
                    "SELECT task_id FROM replication_tasks "
                    "WHERE group_id=? AND file_unique_id=? AND src_channel_id=? AND dst_channel_id=?",
                    (group_id, file_unique_id, src_channel_id, dst_channel_id),
                )
                row = await cursor.fetchone()
                return row[0] if row else 0
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return 0

    async def get_pending_replication_tasks(
        self, limit: int = 10, priority_max: int = 10,
    ) -> list[dict]:
        """查询待处理的副本复制任务(status='PLANNED' 且 priority<=priority_max)。"""
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT task_id, group_id, file_unique_id, src_channel_id, "
                "dst_channel_id, src_msg_id, dst_msg_id, media_group_id, "
                "task_type, priority, status, prev_status, attempts, "
                "max_attempts, next_retry_at, last_error, created_at, "
                "updated_at, committed_at "
                "FROM replication_tasks WHERE status = 'PLANNED' "
                "AND priority <= ? ORDER BY priority, created_at LIMIT ?",
                (priority_max, limit),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_pending_replication_tasks 异常: {e}")
            return []
        cols = ["task_id", "group_id", "file_unique_id", "src_channel_id",
                "dst_channel_id", "src_msg_id", "dst_msg_id", "media_group_id",
                "task_type", "priority", "status", "prev_status", "attempts",
                "max_attempts", "next_retry_at", "last_error", "created_at",
                "updated_at", "committed_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def mark_replication_copying(self, task_id: int) -> bool:
        """标记任务为复制中(status='COPYING', prev_status=旧status)。"""
        if not self._db:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE replication_tasks SET prev_status = status, "
                    "status = 'COPYING', updated_at = ? WHERE task_id = ? "
                    "AND status = 'PLANNED'",
                    (now, task_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_replication_copied(self, task_id: int, dst_msg_id: int) -> bool:
        """标记任务为已复制未验证(status='COPIED_UNVERIFIED', dst_msg_id=?)。"""
        if not self._db:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE replication_tasks SET status = 'COPIED_UNVERIFIED', "
                    "dst_msg_id = ?, updated_at = ? WHERE task_id = ? "
                    "AND status = 'COPYING'",
                    (dst_msg_id, now, task_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_replication_committed(self, task_id: int) -> bool:
        """标记任务为已提交(status='COMMITTED', committed_at=now)。"""
        if not self._db:
            return False
        now = time.time()
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE replication_tasks SET status = 'COMMITTED', "
                    "committed_at = ?, updated_at = ? WHERE task_id = ? "
                    "AND status = 'COPIED_UNVERIFIED'",
                    (now, now, task_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    async def mark_replication_failed(
        self, task_id: int, reason: str, max_attempts: int = 3,
    ) -> bool:
        """标记任务失败:attempts+1,达到上限则 status='FAILED',否则 status='PLANNED' + next_retry_at。

        Returns: True 表示更新成功。
        """
        if not self._db:
            return False
        now = time.time()
        next_retry = now + 60  # 默认 60 秒后重试
        for attempt in range(3):
            try:
                # 使用 CASE 表达式根据 attempts 判断最终状态
                cursor = await self._db.execute(
                    "UPDATE replication_tasks SET "
                    "attempts = attempts + 1, "
                    "last_error = ?, "
                    "status = CASE WHEN attempts + 1 >= ? THEN 'FAILED' ELSE 'PLANNED' END, "
                    "next_retry_at = CASE WHEN attempts + 1 >= ? THEN NULL ELSE ? END, "
                    "updated_at = ? "
                    "WHERE task_id = ?",
                    (reason, max_attempts, max_attempts, next_retry, now, task_id),
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return False

    # ─── R36 B0-3: replication_tasks task-first 控制面扩展方法 ───
    # 设计目标:
    #   - worker 只扫描非终态(PLANNED/COPYING/COPIED_UNVERIFIED)
    #   - COPIED_UNVERIFIED 优先对账,不重新 copy
    #   - 提供"Manifest + message_backups + COMMITTED"原子提交边界
    #   - 提供 lease 超时检测,把卡在 COPYING 的任务回退到 PLANNED

    async def get_inflight_replication_tasks(
        self, states: list[str] | None = None, limit: int = 50,
    ) -> list[dict]:
        """查询非终态 replication_tasks(默认 PLANNED/COPYING/COPIED_UNVERIFIED)。

        Args:
            states: 自定义状态过滤,默认为三种非终态。
            limit: 最多返回条数。

        Returns:
            [{task_id, group_id, file_unique_id, src_channel_id, dst_channel_id,
              src_msg_id, dst_msg_id, media_group_id, task_type, priority,
              status, prev_status, attempts, max_attempts, next_retry_at,
              last_error, created_at, updated_at, committed_at}, ...]
        """
        if not self._db:
            return []
        if states is None:
            states = ["PLANNED", "COPYING", "COPIED_UNVERIFIED"]
        # 用 IN (?, ?, ?) 占位符
        placeholders = ", ".join("?" for _ in states)
        cols = ["task_id", "group_id", "file_unique_id", "src_channel_id",
                "dst_channel_id", "src_msg_id", "dst_msg_id", "media_group_id",
                "task_type", "priority", "status", "prev_status", "attempts",
                "max_attempts", "next_retry_at", "last_error", "created_at",
                "updated_at", "committed_at"]
        try:
            rows = await self._db.execute_fetchall(
                f"SELECT {', '.join(cols)} FROM replication_tasks "
                f"WHERE status IN ({placeholders}) "
                f"ORDER BY priority, created_at LIMIT ?",
                (*states, limit),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_inflight_replication_tasks 异常: {e}")
            return []
        return [dict(zip(cols, r)) for r in rows]

    async def get_copied_unverified_tasks(self, limit: int = 50) -> list[dict]:
        """查询所有 COPIED_UNVERIFIED 状态的任务(对账恢复专用)。

        这些任务已经完成 Telegram copy(写入了 dst_msg_id),但 Manifest
        /message_backups/COMMITTED 尚未原子提交。恢复时优先对账,不得
        重新 copy。
        """
        if not self._db:
            return []
        cols = ["task_id", "group_id", "file_unique_id", "src_channel_id",
                "dst_channel_id", "src_msg_id", "dst_msg_id", "media_group_id",
                "task_type", "priority", "status", "prev_status", "attempts",
                "max_attempts", "next_retry_at", "last_error", "created_at",
                "updated_at", "committed_at"]
        try:
            rows = await self._db.execute_fetchall(
                f"SELECT {', '.join(cols)} FROM replication_tasks "
                "WHERE status = 'COPIED_UNVERIFIED' "
                "ORDER BY updated_at LIMIT ?",
                (limit,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_copied_unverified_tasks 异常: {e}")
            return []
        return [dict(zip(cols, r)) for r in rows]

    async def reset_stale_copying_tasks(
        self, lease_timeout_seconds: float = 600,
    ) -> int:
        """重置超时的 COPYING 任务回到 PLANNED(恢复用)。

        场景: worker 崩溃后,COPYING 状态的任务 updated_at 已过期。
        本方法把 updated_at < now - lease_timeout_seconds 的 COPYING 任务
        回退到 PLANNED(prev_status 记录原状态为 COPYING,便于审计),
        让后续 worker 重新走 claim → copy 流程。

        Args:
            lease_timeout_seconds: 超过此秒数视为 lease 过期。默认 600s。

        Returns:
            被重置的任务数。
        """
        if not self._db:
            return 0
        now = time.time()
        cutoff = now - lease_timeout_seconds
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE replication_tasks SET prev_status = status, "
                    "status = 'PLANNED', updated_at = ? "
                    "WHERE status = 'COPYING' AND updated_at < ?",
                    (now, cutoff),
                )
                await self._db.commit()
                return cursor.rowcount if cursor else 0
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return 0

    async def commit_replication_transaction(
        self, task_id: int,
        manifest_records: list[dict] | None = None,
        backup_mappings: list[tuple[int, int]] | None = None,
        backup_channel_id: int | None = None,
    ) -> bool:
        """R36 B0-3: 原子提交 replication_task 的复制产物。

        在同一 SQLite 事务内完成:
        1. INSERT OR REPLACE manifest 记录
        2. INSERT OR REPLACE message_backups 映射(需 backup_channel_id)
        3. UPDATE replication_tasks SET status='COMMITTED', committed_at=now
           WHERE task_id=? AND status='COPIED_UNVERIFIED'

        任一步骤失败整体回滚,task 仍停留在 COPIED_UNVERIFIED 等待对账。

        Args:
            task_id: replication_task.task_id
            manifest_records: 与 upsert_manifest_batch 相同结构的 dict 列表
            backup_mappings: [(main_msg_id, backed_msg_id), ...]
            backup_channel_id: message_backups 的 backup_channel_id

        Returns:
            True 表示提交成功;False 表示 task 状态不符或失败。
        """
        if not self._db or not task_id:
            return False
        import datetime as _dt
        now = time.time()
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for attempt in range(3):
            try:
                # 显式开启事务,确保下面 3 个操作原子提交
                await self._db.execute("BEGIN")
                try:
                    # 1. 写 manifest
                    if manifest_records:
                        rows = [
                            (r["group_id"], r["file_unique_id"], r["channel_id"],
                             r["message_id"], r.get("media_type", ""),
                             r.get("media_group_id", ""), now_iso)
                            for r in manifest_records if r.get("file_unique_id")
                        ]
                        if rows:
                            await self._db.executemany(
                                "INSERT OR REPLACE INTO manifest "
                                "(group_id, file_unique_id, channel_id, message_id, "
                                "media_type, media_group_id, first_seen_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                rows,
                            )
                    # 2. 写 message_backups
                    if backup_mappings and backup_channel_id:
                        from database.session import save_message_backup
                        for main_msg_id, backed_msg_id in backup_mappings:
                            await save_message_backup(
                                main_msg_id, backup_channel_id, backed_msg_id,
                            )
                    # 3. 状态机推进 COPIED_UNVERIFIED → COMMITTED
                    #    关键: WHERE 子句含 status='COPIED_UNVERIFIED' 守卫,
                    #    若状态不符 rowcount=0,需主动 raise 触发 ROLLBACK,
                    #    否则前面的 manifest/backup 写入会被误提交。
                    cursor = await self._db.execute(
                        "UPDATE replication_tasks SET status = 'COMMITTED', "
                        "committed_at = ?, updated_at = ? "
                        "WHERE task_id = ? AND status = 'COPIED_UNVERIFIED'",
                        (now, now, task_id),
                    )
                    affected = cursor.rowcount if cursor else 0
                    if affected == 0:
                        # 状态不符:主动回滚,保证 manifest/backup 不被误提交
                        raise RuntimeError(
                            f"task_id={task_id} 状态不是 COPIED_UNVERIFIED,无法 COMMITTED"
                        )
                    await self._db.execute("COMMIT")
                    return True
                except Exception:
                    # 任一步失败:回滚,task 仍为 COPIED_UNVERIFIED
                    await self._db.execute("ROLLBACK")
                    raise
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(
                    f"[CacheStore] commit_replication_transaction 失败 "
                    f"task_id={task_id}: {e}"
                )
                return False

    async def get_replication_task_by_unique_key(
        self, group_id: int, file_unique_id: str,
        src_channel_id: int, dst_channel_id: int,
    ) -> dict | None:
        """按唯一业务键查询 replication_task(用于发现缺失时检查是否已有任务)。

        Returns:
            任务 dict(含 status 字段);若不存在返回 None。
        """
        if not self._db or not file_unique_id:
            return None
        cols = ["task_id", "group_id", "file_unique_id", "src_channel_id",
                "dst_channel_id", "src_msg_id", "dst_msg_id", "media_group_id",
                "task_type", "priority", "status", "prev_status", "attempts",
                "max_attempts", "next_retry_at", "last_error", "created_at",
                "updated_at", "committed_at"]
        try:
            rows = await self._db.execute_fetchall(
                f"SELECT {', '.join(cols)} FROM replication_tasks "
                "WHERE group_id=? AND file_unique_id=? "
                "AND src_channel_id=? AND dst_channel_id=? LIMIT 1",
                (group_id, file_unique_id, src_channel_id, dst_channel_id),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_replication_task_by_unique_key 异常: {e}")
            return None
        return dict(zip(cols, rows[0])) if rows else None

    # ─── D: 本地任务队列操作 ───

    # ─── H方案: 本地插入新 job(返回临时负数 ID) ───

    async def insert_local_job(self, job: dict) -> int:
        """H方案: 插入新 job 到本地队列，返回临时负数 ID。
        
        SQLite 是主路径，失败抛异常由调用方给用户重试反馈。
        """
        if not self._db:
            raise RuntimeError("SQLite 连接未就绪")
        local_id = -int(time.time() * 1000000)
        # 确保 ID 唯一(极端情况下同一微秒内多次调用)
        while True:
            existing = await self._db.execute_fetchall(
                "SELECT 1 FROM local_job_queue WHERE crdb_id = ?", (local_id,)
            )
            if not existing:
                break
            local_id -= 1
        await self._db.execute(
            """INSERT INTO local_job_queue
            (crdb_id, code, target_user_id, storage_channel_id,
             storage_msg_ids, batch_file_meta, task_type, status,
             retry_count, protect_content, created_at,
             group_id, file_unique_id, media_group_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                local_id, job["code"], job["target_user_id"],
                job.get("storage_channel_id", 0),
                job.get("storage_msg_ids", ""),
                job.get("batch_file_meta", ""),
                job.get("task_type", "single"),
                job.get("status", "pending"),
                job.get("retry_count", 0),
                job.get("protect_content", False),
                job.get("created_at", ""),
                job.get("group_id", 0),
                job.get("file_unique_id", ""),
                job.get("media_group_id", ""),
            ),
        )
        await self._db.commit()
        return local_id

    async def update_local_job_crdb_id(self, local_id: int, crdb_id: int):
        """H方案: 后台 CRDB 同步完成后，更新真实 CRDB ID"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET crdb_id = ? WHERE crdb_id = ?",
            (crdb_id, local_id),
        )
        await self._db.commit()

    async def get_local_job_by_code(self, code: str) -> dict | None:
        """H方案: 按 code 查找本地 job(启动同步去重用)"""
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT crdb_id FROM local_job_queue WHERE code = ? LIMIT 1",
            (code,),
        )
        return {"crdb_id": rows[0][0]} if rows else None

    async def upsert_local_job(self, job: dict):
        """将 CRDB job 数据同步到本地队列（仅插入不存在的 job，不覆盖本地状态）"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    """INSERT OR IGNORE INTO local_job_queue
                    (crdb_id, code, target_user_id, storage_channel_id,
                     storage_msg_ids, batch_file_meta, task_type, status,
                     retry_count, protect_content, created_at,
                     group_id, file_unique_id, media_group_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job["id"], job["code"], job["target_user_id"],
                        job.get("storage_channel_id", 0),
                        job.get("storage_msg_ids", ""),
                        job.get("batch_file_meta", ""),
                        job.get("task_type", "single"),
                        job.get("status", "pending"),
                        job.get("retry_count", 0),
                        job.get("protect_content", False),
                        job.get("created_at", ""),
                        job.get("group_id", 0),
                        job.get("file_unique_id", ""),
                        job.get("media_group_id", ""),
                    ),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return

    async def get_local_pending_jobs(self, limit: int = 10) -> list[dict]:
        """从本地队列获取 pending 状态的 jobs"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT crdb_id, code, target_user_id, storage_channel_id,
               storage_msg_ids, batch_file_meta, task_type, status,
               retry_count, protect_content, created_at,
               group_id, file_unique_id, media_group_id
               FROM local_job_queue WHERE status = 'pending'
               ORDER BY created_at LIMIT ?""",
            (limit,),
        )
        return [
            {
                "crdb_id": r[0], "code": r[1], "target_user_id": r[2],
                "storage_channel_id": r[3], "storage_msg_ids": r[4],
                "batch_file_meta": r[5], "task_type": r[6], "status": r[7],
                "retry_count": r[8], "protect_content": r[9], "created_at": r[10],
                "group_id": r[11] if len(r) > 11 else 0,
                "file_unique_id": r[12] if len(r) > 12 else "",
                "media_group_id": r[13] if len(r) > 13 else "",
            }
            for r in rows
        ]

    async def get_local_job_by_crdb_id(self, crdb_id: int) -> dict | None:
        """C1: 按 crdb_id 精确查 job(Redis Stream 消费者用)。"""
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT crdb_id, code, target_user_id, storage_channel_id, "
            "storage_msg_ids, batch_file_meta, task_type, status, "
            "retry_count, protect_content, created_at, "
            "group_id, file_unique_id, media_group_id "
            "FROM local_job_queue WHERE crdb_id = ?",
            (crdb_id,),
        )
        if not rows:
            return None
        cols = ["crdb_id", "code", "target_user_id", "storage_channel_id",
                "storage_msg_ids", "batch_file_meta", "task_type", "status",
                "retry_count", "protect_content", "created_at",
                "group_id", "file_unique_id", "media_group_id"]
        row = dict(zip(cols, rows[0]))
        # 类型转换(与 get_local_pending_jobs 保持一致)
        try:
            row["protect_content"] = bool(row.get("protect_content", False))
        except Exception:
            row["protect_content"] = False
        # 兼容旧表(可能没有新字段): 缺失则填默认值
        row.setdefault("group_id", 0)
        row.setdefault("file_unique_id", "")
        row.setdefault("media_group_id", "")
        return row

    async def get_local_job_with_replica_info(self, crdb_id: int) -> dict | None:
        """R36 B0-1: 按 crdb_id 查询 job 并确保返回结构化的副本信息字段。

        返回 dict 包含 group_id/file_unique_id/media_group_id,
        供 ReplicaAwareResolver 直接消费(无需 JSON 解析 batch_file_meta)。
        与 get_local_job_by_crdb_id 等价但语义更明确。
        """
        return await self.get_local_job_by_crdb_id(crdb_id)

    async def count_pending_jobs(self) -> int:
        """统计本地队列中 pending 状态的 job 数量(队列积压深度, 0 CRDB RU)"""
        if not self._db:
            return 0
        try:
            rows = await self._db.execute_fetchall(
                "SELECT COUNT(*) FROM local_job_queue WHERE status = 'pending'"
            )
            return rows[0][0] if rows else 0
        except Exception:
            return 0

    async def mark_local_job_dispatched(self, crdb_id: int) -> bool:
        """标记本地 job 为 dispatched (CAS 语义,防止多 worker 重复认领)。

        Returns:
            True 表示成功认领(从 pending → dispatched);False 表示已被其他 worker 认领。
        """
        if not self._db:
            return False
        cursor = await self._db.execute(
            "UPDATE local_job_queue SET status='dispatched', dispatched_at=? "
            "WHERE crdb_id=? AND status='pending'",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), crdb_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def update_local_job_status(self, crdb_id: int, status: str, retry_count: int = None, dead_reason: str = None):
        """更新本地 job 状态"""
        if not self._db:
            return
        if retry_count is not None:
            await self._db.execute(
                "UPDATE local_job_queue SET status=?, retry_count=?, synced_at=0 WHERE crdb_id=?",
                (status, retry_count, crdb_id),
            )
        elif dead_reason:
            await self._db.execute(
                "UPDATE local_job_queue SET status=?, dead_reason=?, synced_at=0 WHERE crdb_id=?",
                (status, dead_reason, crdb_id),
            )
        else:
            await self._db.execute(
                "UPDATE local_job_queue SET status=?, synced_at=0 WHERE crdb_id=?",
                (status, crdb_id),
            )
        await self._db.commit()

    async def retry_local_job(self, crdb_id: int, new_retry_count: int):
        """将失败 job 重置为 pending 状态，递增 retry_count，清空 dispatched_at，唤醒 worker。"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET status='pending', retry_count=?, dispatched_at=NULL, synced_at=0 WHERE crdb_id=?",
            (new_retry_count, crdb_id),
        )
        await self._db.commit()
        await self.notify_dsp_new_job()

    async def get_local_dead_jobs(self, limit: int = 10, max_dead_retry: int = 2) -> list[dict]:
        """获取可重试的本地 dead jobs（dead_retry_count < max_dead_retry）。"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT crdb_id, retry_count, dead_reason, COALESCE(dead_retry_count,0) as drc
               FROM local_job_queue WHERE status='dead' AND COALESCE(dead_retry_count,0) < ?
               ORDER BY created_at LIMIT ?""",
            (max_dead_retry, limit),
        )
        return [{"crdb_id": r[0], "retry_count": r[1], "dead_reason": r[2], "dead_retry_count": r[3]} for r in rows]

    async def retry_local_dead_job(self, crdb_id: int):
        """将 dead job 重置为 pending，重置 retry_count=0，递增 dead_retry_count。"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET status='pending', retry_count=0, dispatched_at=NULL, "
            "dead_retry_count=COALESCE(dead_retry_count,0)+1, synced_at=0 WHERE crdb_id=?",
            (crdb_id,),
        )
        await self._db.commit()
        await self.notify_dsp_new_job()

    async def reclaim_stale_dispatched(self, timeout_seconds: int = 300) -> list[int]:
        """回收超时的 dispatched 状态 job,重置为 pending。
        防止进程崩溃或 task 异常导致 job 永久停留在 dispatched 状态。
        返回回收的 crdb_id 列表(供调用方重新投递 Redis Stream)。
        """
        if not self._db:
            return []
        cutoff = (datetime.datetime.now(datetime.timezone.utc) -
                  datetime.timedelta(seconds=timeout_seconds)).isoformat()
        # 先 SELECT 再 UPDATE,获取被回收的 crdb_id 列表
        rows = await self._db.execute_fetchall(
            "SELECT crdb_id FROM local_job_queue "
            "WHERE status='dispatched' AND dispatched_at < ? AND crdb_id > 0",
            (cutoff,),
        )
        reclaimed_ids = [r[0] for r in rows if r and r[0]]
        if not reclaimed_ids:
            return []
        placeholders = ",".join("?" * len(reclaimed_ids))
        await self._db.execute(
            f"UPDATE local_job_queue SET status='pending', dispatched_at=NULL, synced_at=0 "
            f"WHERE status='dispatched' AND dispatched_at < ? AND crdb_id IN ({placeholders})",
            (cutoff, *reclaimed_ids),
        )
        await self._db.commit()
        await self.notify_dsp_new_job()
        logger.info(f"[CacheStore] 回收 {len(reclaimed_ids)} 个超时 dispatched jobs")
        return reclaimed_ids

    async def get_local_unsynced_jobs(self) -> list[dict]:
        """获取需要同步回 CRDB 的 job(状态变更未同步,仅 crdb_id>0 的)"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT crdb_id, status, retry_count, dead_reason
               FROM local_job_queue WHERE synced_at = 0 AND crdb_id > 0
               AND status IN ('retried','dead','done')
               LIMIT 50"""
        )
        return [
            {"crdb_id": r[0], "status": r[1], "retry_count": r[2], "dead_reason": r[3]}
            for r in rows
        ]

    async def mark_local_job_synced(self, crdb_id: int):
        """标记已同步回 CRDB"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET synced_at=? WHERE crdb_id=?",
            (time.time(), crdb_id),
        )
        await self._db.commit()

    async def count_local_pending(self) -> int:
        """本地 pending 任务数(0 RU)"""
        if not self._db:
            return 0
        row = await self._db.execute_fetchall(
            "SELECT COUNT(*) FROM local_job_queue WHERE status='pending'"
        )
        return row[0][0] if row else 0

    async def cleanup_local_jobs(self, max_age_days: int = 7):
        """清理超过 N 天的旧 job 记录"""
        if not self._db:
            return
        cutoff = time.time() - max_age_days * 86400
        await self._db.execute(
            "DELETE FROM local_job_queue WHERE created_at < ? AND status IN ('dispatched','done','dead')",
            (str(cutoff),),
        )
        await self._db.commit()

    # ─── F5: 启动统计快照（多进程隔离 + 聚合读取） ───

    async def save_counter_snapshot(self, counters: dict[str, int], role: str = None):
        """保存启动统计快照(各 Bot 周期性写入)

        多进程隔离：每个进程写入带进程标识前缀的 key（如 `up_bot:total_files`），
        避免不同进程互相覆盖。admin_bot 读取时通过 load_counter_snapshot 聚合求和。

        Args:
            counters: 计数器字典
            role: 进程标识（如 up_bot/idx_bot/dsp_bot/mon_bot/admin_bot）。
                  若未传入则从环境变量 BOT_ROLE 读取，再回退到 "default"。
        """
        if not self._db:
            return
        if role is None:
            role = os.environ.get("BOT_ROLE", "default")
        now = time.time()
        # 先清除本进程的旧快照（防止本次未更新的 key 残留旧值）
        await self._db.execute(
            "DELETE FROM counter_snapshot WHERE key LIKE ?",
            (f"{role}:%",),
        )
        for k, v in counters.items():
            await self._db.execute(
                "INSERT OR REPLACE INTO counter_snapshot (key, value, ts) VALUES (?, ?, ?)",
                (f"{role}:{k}", v, now),
            )
        await self._db.commit()

    async def load_counter_snapshot(self) -> dict[str, int]:
        """加载启动统计快照（聚合所有进程的计数）

        多进程聚合：各进程写入带前缀的 key（如 `up_bot:total_files`），
        本方法读取所有 key，按 `:` 后的部分聚合求和，得到全局总数。
        """
        if not self._db:
            return {}
        try:
            rows = await self._db.execute_fetchall(
                "SELECT key, value FROM counter_snapshot"
            )
            aggregated: dict[str, int] = {}
            for k, v in rows:
                # key 格式: <role>:<counter_name>
                if ":" in k:
                    counter_name = k.split(":", 1)[1]
                else:
                    # 兼容旧格式（无前缀）
                    counter_name = k
                try:
                    aggregated[counter_name] = aggregated.get(counter_name, 0) + int(v)
                except (ValueError, TypeError):
                    continue
            return aggregated
        except Exception:
            return {}

    # ─── E1: cells 跨进程共享 ───

    async def save_cells_snapshot(self, cells: list[dict], version: int):
        """保存 cells 全量快照(仅 Mon Bot 写)"""
        if not self._db:
            return
        try:
            raw = json.dumps(cells, default=str)
            val = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            if self._in_writer_tx:
                raise
            return
        now = time.time()
        await self._db.execute(
            "INSERT OR REPLACE INTO cells_snapshot (id, data, version, updated_at) VALUES (1, ?, ?, ?)",
            (val, version, now),
        )
        await self._db.execute(
            "INSERT INTO cells_change_notify (version, ts) VALUES (?, ?)",
            (version, now),
        )
        await self._db.commit()

    async def load_cells_snapshot(self) -> tuple[list[dict] | None, int]:
        """加载 cells 快照(其他 Bot 启动时调)"""
        if not self._db:
            return None, 0
        try:
            row = await self._db.execute_fetchall(
                "SELECT data, version FROM cells_snapshot WHERE id=1"
            )
            if not row:
                return None, 0
            cells = json.loads(row[0][0])
            return cells, row[0][1]
        except Exception:
            return None, 0

    async def has_cells_change(self, last_version: int) -> tuple[bool, int]:
        """检查是否有 cells 变更"""
        if not self._db:
            return False, last_version
        row = await self._db.execute_fetchall(
            "SELECT MAX(version) FROM cells_change_notify WHERE version > ?",
            (last_version,),
        )
        new_version = row[0][0] if row and row[0][0] else last_version
        return new_version > last_version, new_version

    # ─── E2: cells 本地逐行存储（热路径零 CRDB RU） ───

    async def bulk_upsert_cells_local(self, cells: list[dict]):
        """初始化/全量同步:批量写入 cells 到本地表(crdb_synced=1,视为已同步)

        PRE-04/PRE-01: 包含 prev_slot_id 和 demoted_to_channel_id 字段，
        前者用于反向遍历环形链表，后者用于降级时立即跳转到接替频道。
        """
        if not self._db or not cells:
            return
        now = time.time()
        rows = []
        for c in cells:
            rows.append((
                c["slot_id"],
                c.get("channel_id", 0),
                c.get("status", "shadow1"),
                c.get("next_active_chat_id"),
                c.get("prev_slot_id"),
                c.get("demoted_to_channel_id"),
                c.get("account_name", ""),
                c.get("is_r100", 0),
                c.get("last_heartbeat"),
                c.get("last_synced_msg_id", 0),
                c.get("degrade_count", 0),
                c.get("file_count", 0),
                c.get("rotation_started_at"),
                now,
                1,
            ))
        for attempt in range(3):
            try:
                await self._db.executemany(
                    """INSERT OR REPLACE INTO cells_local
                    (slot_id, channel_id, status, next_active_chat_id, prev_slot_id,
                     demoted_to_channel_id, account_name, is_r100, last_heartbeat,
                     last_synced_msg_id, degrade_count, file_count, rotation_started_at,
                     updated_at, crdb_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                await self._db.commit()
                await self._rebuild_cells_snapshot()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] bulk_upsert_cells_local 失败: {e}")
                return

    async def update_cell_fields_local(self, slot_id: str, fields: dict, mark_dirty: bool = False):
        """更新本地 cell 的若干字段（零 CRDB RU）。
        mark_dirty=True 时标记为需同步到 CRDB（异常事件用）。
        若更新涉及 status/next_active_chat_id（路由关键字段），自动重建 JSON 快照。
        """
        if not self._db or not fields:
            return
        now = time.time()
        routing_changed = bool(set(fields.keys()) & {"status", "next_active_chat_id"})
        set_parts = []
        params = []
        for k, v in fields.items():
            if k in ("slot_id",):
                continue
            set_parts.append(f"{k} = ?")
            params.append(v)
        set_parts.append("updated_at = ?")
        params.append(now)
        if mark_dirty:
            set_parts.append("crdb_synced = 0")
        params.append(slot_id)
        sql = f"UPDATE cells_local SET {', '.join(set_parts)} WHERE slot_id = ?"
        for attempt in range(3):
            try:
                await self._db.execute(sql, params)
                await self._db.commit()
                if routing_changed:
                    await self._rebuild_cells_snapshot()
                else:
                    await self._bump_cells_version(now)
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                if self._in_writer_tx:
                    raise
                return

    async def batch_update_cells_local(self, updates: list[tuple[str, dict, bool]]):
        """原子批量更新多个 cell（零 CRDB RU）。
        updates: [(slot_id, {fields}, mark_dirty), ...]
        所有更新在一个 SQLite 事务中完成，保证原子性。
        若更新涉及 status/next_active_chat_id（路由关键字段），自动重建 JSON 快照。
        """
        if not self._db or not updates:
            return
        now = time.time()
        routing_changed = any(
            bool(set(fields.keys()) & {"status", "next_active_chat_id"})
            for _, fields, _ in updates
        )
        for attempt in range(3):
            try:
                if not self._in_writer_tx:
                    await self._db.execute("BEGIN IMMEDIATE")
                try:
                    for slot_id, fields, mark_dirty in updates:
                        if not fields:
                            continue
                        set_parts = []
                        params = []
                        for k, v in fields.items():
                            if k in ("slot_id",):
                                continue
                            set_parts.append(f"{k} = ?")
                            params.append(v)
                        set_parts.append("updated_at = ?")
                        params.append(now)
                        if mark_dirty:
                            set_parts.append("crdb_synced = 0")
                        params.append(slot_id)
                        sql = f"UPDATE cells_local SET {', '.join(set_parts)} WHERE slot_id = ?"
                        await self._db.execute(sql, params)
                    if not self._in_writer_tx:
                        await self._db.commit()
                except Exception:
                    if not self._in_writer_tx:
                        await self._db.rollback()
                    raise
                if routing_changed:
                    await self._rebuild_cells_snapshot()
                else:
                    await self._bump_cells_version(now)
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] batch_update_cells_local 失败: {e}")
                return

    async def increment_cell_file_count_local(self, slot_id: str, delta: int = 1):
        """原子递增 file_count（Up Bot 上传文件后调用，零 CRDB RU）"""
        if not self._db:
            return
        now = time.time()
        for attempt in range(3):
            try:
                await self._db.execute(
                    "UPDATE cells_local SET file_count = file_count + ?, updated_at = ? WHERE slot_id = ?",
                    (delta, now, slot_id),
                )
                await self._db.commit()
                await self._bump_cells_version(now)
                await self._rebuild_cells_snapshot()  # 保持快照与逐行数据一致
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                if self._in_writer_tx:
                    raise
                return

    async def get_all_cells_local(self) -> list[dict]:
        """从本地表读取全部 cells，返回 dict 列表（零 CRDB RU）

        PRE-04/PRE-01: 包含 prev_slot_id 和 demoted_to_channel_id 字段。
        M2: 包含 topology_version/lease_owner/lease_until/transition_id 字段(CAS fencing)。
        """
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, prev_slot_id,
                      demoted_to_channel_id, account_name, is_r100, last_heartbeat,
                      last_synced_msg_id, degrade_count, file_count, rotation_started_at,
                      topology_version, lease_owner, lease_until, transition_id
               FROM cells_local ORDER BY slot_id"""
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "prev_slot_id",
                "demoted_to_channel_id", "account_name", "is_r100", "last_heartbeat",
                "last_synced_msg_id", "degrade_count", "file_count", "rotation_started_at",
                "topology_version", "lease_owner", "lease_until", "transition_id"]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            d["is_r100"] = int(d["is_r100"] or 0)
            d["degrade_count"] = int(d["degrade_count"] or 0)
            d["file_count"] = int(d["file_count"] or 0)
            d["last_synced_msg_id"] = int(d["last_synced_msg_id"] or 0)
            d["topology_version"] = int(d.get("topology_version") or 0)
            if d["next_active_chat_id"]:
                d["next_active_chat_id"] = int(d["next_active_chat_id"])
            if d.get("demoted_to_channel_id"):
                d["demoted_to_channel_id"] = int(d["demoted_to_channel_id"])
            result.append(d)
        return result

    async def get_active_cells_local(self) -> list[dict]:
        """从本地表读取 status=active 的 cells（零 CRDB RU）

        PRE-04/PRE-01: 包含 prev_slot_id 和 demoted_to_channel_id 字段（active 通常二者为空）。
        """
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, prev_slot_id,
                      demoted_to_channel_id, account_name, is_r100, last_heartbeat,
                      last_synced_msg_id, degrade_count, file_count, rotation_started_at
               FROM cells_local WHERE status = 'active'"""
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "prev_slot_id",
                "demoted_to_channel_id", "account_name", "is_r100", "last_heartbeat",
                "last_synced_msg_id", "degrade_count", "file_count", "rotation_started_at"]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            d["is_r100"] = int(d["is_r100"] or 0)
            d["degrade_count"] = int(d["degrade_count"] or 0)
            d["file_count"] = int(d["file_count"] or 0)
            d["last_synced_msg_id"] = int(d["last_synced_msg_id"] or 0)
            if d["next_active_chat_id"]:
                d["next_active_chat_id"] = int(d["next_active_chat_id"])
            if d.get("demoted_to_channel_id"):
                d["demoted_to_channel_id"] = int(d["demoted_to_channel_id"])
            result.append(d)
        return result

    async def get_dirty_cells_local(self, limit: int = 50) -> list[dict]:
        """获取需要同步到 CRDB 的脏 cells（异常事件）

        PRE-01: 包含 demoted_to_channel_id 字段，确保降级映射关系同步到 CRDB。
        """
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, prev_slot_id,
                      demoted_to_channel_id, account_name, is_r100, degrade_count,
                      file_count, rotation_started_at
               FROM cells_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "prev_slot_id",
                "demoted_to_channel_id", "account_name", "is_r100", "degrade_count",
                "file_count", "rotation_started_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def mark_cell_synced_local(self, slot_id: str):
        """标记 cell 已同步到 CRDB"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE cells_local SET crdb_synced = 1 WHERE slot_id = ?",
            (slot_id,),
        )
        await self._db.commit()

    # ─── M2: cells CAS/fencing — Mon 与 Dsp 自主降级使用 CAS + fencing token ───
    # 防止双控制面同时改写同一 cell,通过 topology_version 递增 + 租约互斥实现并发安全。

    async def cas_transition_cell(
        self,
        slot_id: str,
        expected_status: str,
        new_status: str,
        lease_owner: str,
        transition_id: str,
        lease_seconds: int = 60,
        **update_fields,
    ) -> bool:
        """CAS 原子转换 cell 状态(双控制面并发安全)。

        WHERE 子句包含 status=expected_status 实现 Compare-And-Swap:
        - 仅当当前 status == expected_status 时才更新,否则 rowcount=0(被其他控制面抢占)。
        - 成功更新时 topology_version 递增(作为 fencing token,防止旧版本回写)。
        - lease_owner/lease_until/transition_id 记录本次转换归属,便于追溯。
        - **update_fields 支持附加字段(如 channel_id, account_name)一并写入。
        返回 True 表示 CAS 成功,False 表示状态已被其他控制面改写(需重读后重试)。
        """
        if not self._db:
            return False
        now = time.time()
        lease_until = now + lease_seconds
        # 构造 SET 子句:状态转换 + fencing 字段 + 附加字段
        set_parts = [
            "status = ?",
            "topology_version = topology_version + 1",
            "lease_owner = ?",
            "lease_until = ?",
            "transition_id = ?",
            "updated_at = ?",
        ]
        params: list = [new_status, lease_owner, lease_until, transition_id, now]
        # 附加字段(排除 CAS/fencing 保留字段,避免重复赋值)
        _reserved = {"status", "topology_version", "lease_owner", "lease_until",
                     "transition_id", "updated_at", "slot_id"}
        for k, v in update_fields.items():
            if k in _reserved:
                continue
            set_parts.append(f"{k} = ?")
            params.append(v)
        # WHERE 子句:slot_id + expected_status(CAS 关键)
        params.extend([slot_id, expected_status])
        sql = (
            f"UPDATE cells_local SET {', '.join(set_parts)} "
            "WHERE slot_id = ? AND status = ?"
        )
        for attempt in range(3):
            try:
                cursor = await self._db.execute(sql, params)
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] cas_transition_cell 失败: {e}")
                return False
        return False

    async def acquire_cell_lease(
        self, slot_id: str, owner: str, lease_seconds: int = 60
    ) -> bool:
        """获取 cell 租约(防并发操作)。

        仅当租约已过期(lease_until < now)或当前持有者是自己时才能获取。
        返回 True 表示获取成功,False 表示租约被其他控制面持有。
        """
        if not self._db:
            return False
        now = time.time()
        lease_until = now + lease_seconds
        sql = (
            "UPDATE cells_local SET lease_owner = ?, lease_until = ? "
            "WHERE slot_id = ? AND (lease_until < ? OR lease_owner = ?)"
        )
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    sql, (owner, lease_until, slot_id, now, owner)
                )
                await self._db.commit()
                return bool(cursor and cursor.rowcount > 0)
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] acquire_cell_lease 失败: {e}")
                return False
        return False

    async def release_cell_lease(self, slot_id: str, owner: str):
        """释放 cell 租约(仅当当前持有者是自己时才释放,防止误释放他人租约)。"""
        if not self._db:
            return
        sql = (
            "UPDATE cells_local SET lease_owner = '', lease_until = 0 "
            "WHERE slot_id = ? AND lease_owner = ?"
        )
        for attempt in range(3):
            try:
                await self._db.execute(sql, (slot_id, owner))
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                logger.warning(f"[CacheStore] release_cell_lease 失败: {e}")
                return

    async def get_cell_lease_owner(self, slot_id: str) -> str | None:
        """查询当前租约持有者。若租约已过期或无人持有则返回 None。"""
        if not self._db:
            return None
        try:
            rows = await self._db.execute_fetchall(
                "SELECT lease_owner, lease_until FROM cells_local WHERE slot_id = ?",
                (slot_id,),
            )
            if not rows:
                return None
            lease_owner, lease_until = rows[0]
            if lease_owner and lease_until and lease_until > time.time():
                return lease_owner
            return None
        except Exception as e:
            logger.warning(f"[CacheStore] get_cell_lease_owner 失败: {e}")
            return None

    async def get_cells_by_version(self, min_version: int) -> list[dict]:
        """查询 topology_version > min_version 的 cells(增量同步用)。

        按 topology_version 升序返回,便于消费方按版本顺序应用增量。
        """
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT * FROM cells_local WHERE topology_version > ? "
                "ORDER BY topology_version",
                (min_version,),
            )
            if not rows:
                return []
            # aiosqlite 的 execute_fetchall 不返回 description,用 PRAGMA 拿列名
            col_rows = await self._db.execute_fetchall(
                "PRAGMA table_info(cells_local)"
            )
            col_names = [r[1] for r in col_rows]
            return [dict(zip(col_names, r)) for r in rows]
        except Exception as e:
            logger.warning(f"[CacheStore] get_cells_by_version 失败: {e}")
            return []

    async def get_max_topology_version(self) -> int:
        """查询当前最大 topology_version(无记录时返回 0)。"""
        if not self._db:
            return 0
        try:
            rows = await self._db.execute_fetchall(
                "SELECT MAX(topology_version) FROM cells_local"
            )
            if not rows or rows[0][0] is None:
                return 0
            return int(rows[0][0])
        except Exception as e:
            logger.warning(f"[CacheStore] get_max_topology_version 失败: {e}")
            return 0

    async def delete_cell_local(self, slot_id: str) -> bool:
        """C3: 删除单个 cell 并修复环形链表指针,然后重建快照。

        - 查找前驱 P(P.next_active_chat_id == X.channel_id)
        - 查找后继 S(S.prev_slot_id == X.slot_id)
        - 修复 P 和 S 的指针
        - 删除 X
        - 重建快照 + bump version(触发跨进程通知)
        返回 True 表示删除成功,False 表示 slot_id 不存在。
        """
        if not self._db:
            return False
        # 修复指针(单事务,SELECT 也在事务内避免 TOCTOU)
        if not self._in_writer_tx:
            await self._db.execute("BEGIN IMMEDIATE")
        try:
            # 先获取待删除 cell 的 channel_id
            rows = await self._db.execute_fetchall(
                "SELECT channel_id FROM cells_local WHERE slot_id = ?",
                (slot_id,),
            )
            if not rows:
                if not self._in_writer_tx:
                    await self._db.execute("ROLLBACK")
                return False
            target_channel_id = rows[0][0]
            # 查找前驱 P:next_active_chat_id == target_channel_id 的 cell
            prev_rows = await self._db.execute_fetchall(
                "SELECT slot_id, channel_id FROM cells_local WHERE next_active_chat_id = ?",
                (target_channel_id,),
            )
            # 查找后继 S:prev_slot_id == slot_id 的 cell
            next_rows = await self._db.execute_fetchall(
                "SELECT slot_id, channel_id FROM cells_local WHERE prev_slot_id = ?",
                (slot_id,),
            )
            prev_slot_id = prev_rows[0][0] if prev_rows else None
            prev_channel_id = prev_rows[0][1] if prev_rows else None
            next_slot_id = next_rows[0][0] if next_rows else None
            next_channel_id = next_rows[0][1] if next_rows else None
            # 排除自引用(X 的 next 指向自己或 prev 指向自己)
            if prev_slot_id == slot_id:
                prev_slot_id = None
            if next_slot_id == slot_id:
                next_slot_id = None
            # 更新前驱的 next 指向后继
            if prev_slot_id:
                new_next = next_channel_id if next_slot_id else None
                if new_next is not None:
                    await self._db.execute(
                        "UPDATE cells_local SET next_active_chat_id = ?, updated_at = ? WHERE slot_id = ?",
                        (new_next, time.time(), prev_slot_id),
                    )
                else:
                    await self._db.execute(
                        "UPDATE cells_local SET next_active_chat_id = NULL, updated_at = ? WHERE slot_id = ?",
                        (time.time(), prev_slot_id),
                    )
            # 更新后继的 prev 指向前驱
            if next_slot_id:
                new_prev = prev_slot_id if prev_slot_id else None
                await self._db.execute(
                    "UPDATE cells_local SET prev_slot_id = ?, updated_at = ? WHERE slot_id = ?",
                    (new_prev, time.time(), next_slot_id),
                )
            # R38 P1-3: 删除目标 cell 改为软删除(tombstone),保留行供 CRDB 同步
            # 原 DELETE FROM 会让 crdb_sync 检测不到删除事件,
            # 改为 UPDATE SET deleted_at, status='deleted' 让 crdb_sync 发送 tombstone
            await self._db.execute(
                "UPDATE cells_local SET deleted_at = ?, status = 'deleted', "
                "crdb_synced = 0 WHERE slot_id = ?",
                (datetime.datetime.now().isoformat(), slot_id),  # type: ignore[attr-defined]
            )
            if not self._in_writer_tx:
                await self._db.commit()
        except Exception as e:
            if not self._in_writer_tx:
                await self._db.execute("ROLLBACK")
            logger.error(f"[CacheStore] delete_cell_local 事务失败: {e}")
            raise
        # 重建快照 + bump version(触发跨进程通知)
        await self._rebuild_cells_snapshot()
        return True

    async def _rebuild_cells_snapshot(self):
        """从 cells_local 重建 JSON 快照，保持向后兼容（其他 Bot 读 snapshot）"""
        if not self._db:
            return
        cells = await self.get_all_cells_local()
        try:
            raw = json.dumps(cells, default=str)
            val = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            return
        now = time.time()
        version = _next_cells_version()
        await self._db.execute(
            "INSERT OR REPLACE INTO cells_snapshot (id, data, version, updated_at) VALUES (1, ?, ?, ?)",
            (val, version, now),
        )
        await self._bump_cells_version(now)

    async def _bump_cells_version(self, ts: float):
        """写入变更通知 + 递增版本号"""
        if not self._db:
            return
        version = _next_cells_version()
        await self._db.execute(
            "INSERT INTO cells_change_notify (version, ts) VALUES (?, ?)",
            (version, ts),
        )
        await self._db.commit()

    async def notify_relay_change(self):
        """C3: 写入 relay 账号池变更通知,触发 idx_bot 等消费者 reload。

        由 admin_bot 在 /relay_add、/relay_remove 成功后调用。
        """
        if not self._db:
            return
        version = _next_relay_version()
        now = time.time()
        await self._db.execute(
            "INSERT INTO relay_change_notify (version, ts) VALUES (?, ?)",
            (version, now),
        )
        await self._db.commit()

    async def has_relay_change(self, last_version: int) -> tuple[bool, int]:
        """C3: 检查是否有 relay 账号池变更。

        返回 (是否有变更, 最新版本号)。
        """
        if not self._db:
            return False, last_version
        row = await self._db.execute_fetchall(
            "SELECT MAX(version) FROM relay_change_notify WHERE version > ?",
            (last_version,),
        )
        new_version = row[0][0] if row and row[0][0] else last_version
        return new_version > last_version, new_version

    # ─── 记录变更跨进程通知(admin_bot 写 → idx_bot/dsp_bot 读)───

    async def notify_record_change(self, change_type: str, record_key: str):
        """写入记录变更通知,触发 idx_bot/dsp_bot 失效内存缓存。

        change_type: "file" 或 "user"
        record_key: file_code 或 user_id
        由 admin_bot 在处理举报(脱钩/封禁/限制)后调用。
        """
        if not self._db:
            return
        version = _next_file_record_version()
        now = time.time()
        await self._db.execute(
            "INSERT INTO file_record_change_notify (version, change_type, record_key, ts) VALUES (?, ?, ?, ?)",
            (version, change_type, str(record_key), now),
        )
        await self._db.commit()

    async def consume_record_changes(self, last_version: int) -> tuple[list[tuple[str, str]], int]:
        """查询 last_version 之后的所有记录变更,返回 ((change_type, record_key) 列表, 最新版本号)。

        idx_bot/dsp_bot 后台任务定期调用,检测到变更后逐个失效对应缓存。
        """
        if not self._db:
            return [], last_version
        rows = await self._db.execute_fetchall(
            "SELECT DISTINCT change_type, record_key FROM file_record_change_notify WHERE version > ?",
            (last_version,),
        )
        if not rows:
            return [], last_version
        changes = [(row[0], row[1]) for row in rows if row[0] and row[1]]
        ver_row = await self._db.execute_fetchall(
            "SELECT MAX(version) FROM file_record_change_notify"
        )
        new_version = ver_row[0][0] if ver_row and ver_row[0][0] else last_version
        return changes, new_version

    # ─── Manifest 驱动的副本同步（免 Telethon 读历史）───

    async def upsert_manifest(
        self, group_id: int, file_unique_id: str, channel_id: int,
        message_id: int, media_type: str = "", media_group_id: str = "",
    ):
        """登记/更新一条 manifest 记录(本地 SQLite,零 CRDB RU)。

        同一 (group_id, file_unique_id, channel_id) 幂等:已存在则更新 message_id。
        media_group_id 为分组键(空串表示独立文件),mon_bot 据此避免跨批次拆散相册。
        """
        if not self._db or not file_unique_id:
            return
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        await self._db.execute(
            "INSERT OR REPLACE INTO manifest "
            "(group_id, file_unique_id, channel_id, message_id, media_type, media_group_id, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_id, file_unique_id, channel_id, message_id, media_type, media_group_id or "", now_iso),
        )
        await self._db.commit()

    async def upsert_manifest_batch(self, records: list[dict]):
        """批量登记 manifest 记录。records: [{group_id, file_unique_id, channel_id, message_id, media_type, media_group_id}]"""
        if not self._db or not records:
            return
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        rows = [
            (r["group_id"], r["file_unique_id"], r["channel_id"],
             r["message_id"], r.get("media_type", ""), r.get("media_group_id", ""), now_iso)
            for r in records if r.get("file_unique_id")
        ]
        if not rows:
            return
        await self._db.executemany(
            "INSERT OR REPLACE INTO manifest "
            "(group_id, file_unique_id, channel_id, message_id, media_type, media_group_id, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()

    async def get_manifest_by_group(self, group_id: int) -> list[dict]:
        """返回该组所有 manifest 记录。"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT group_id, file_unique_id, channel_id, message_id, media_type, media_group_id, first_seen_at "
            "FROM manifest WHERE group_id = ?",
            (group_id,),
        )
        cols = ["group_id", "file_unique_id", "channel_id", "message_id", "media_type", "media_group_id", "first_seen_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def get_manifest_by_file_unique_id(
        self, file_unique_id: str, group_id: int | None = None,
    ) -> list[dict]:
        """精确索引查询:返回指定 file_unique_id 的所有副本记录。

        P2-4: ReplicaAwareResolver 不再每次拉整组 Manifest,
        改用 (group_id, file_unique_id) 复合索引精确查询。
        manifest 表 PRIMARY KEY (group_id, file_unique_id, channel_id) 左前缀覆盖此查询。

        Args:
            file_unique_id: 文件唯一标识(必填)
            group_id: 可选;传入时按 (group_id, file_unique_id) 精确查询,
                      不传时跨组扫描所有匹配 file_unique_id 的记录(慎用,影响多组)

        Returns:
            [{"group_id", "file_unique_id", "channel_id", "message_id",
               "media_type", "media_group_id", "first_seen_at"}, ...]
        """
        if not self._db or not file_unique_id:
            return []
        cols = ["group_id", "file_unique_id", "channel_id", "message_id",
                "media_type", "media_group_id", "first_seen_at"]
        if group_id is not None:
            rows = await self._db.execute_fetchall(
                "SELECT group_id, file_unique_id, channel_id, message_id, "
                "media_type, media_group_id, first_seen_at "
                "FROM manifest WHERE group_id = ? AND file_unique_id = ?",
                (group_id, file_unique_id),
            )
        else:
            rows = await self._db.execute_fetchall(
                "SELECT group_id, file_unique_id, channel_id, message_id, "
                "media_type, media_group_id, first_seen_at "
                "FROM manifest WHERE file_unique_id = ?",
                (file_unique_id,),
            )
        return [dict(zip(cols, r)) for r in rows]

    async def get_manifest_channels_for_group(self, group_id: int) -> list[int]:
        """返回该组在 manifest 中有记录的所有 channel_id(去重)。"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT DISTINCT channel_id FROM manifest WHERE group_id = ?",
            (group_id,),
        )
        return [r[0] for r in rows]

    async def get_missing_in_channel(
        self, group_id: int, channel_id: int,
    ) -> list[dict]:
        """返回该频道缺失的 file_unique_id 列表(含一个可用的源副本)。

        每条: {file_unique_id, media_type, src_channel_id, src_message_id}
        从任一其他存活频道取一个源副本。
        """
        if not self._db:
            return []
        # 先取该组所有 file_unique_id
        all_rows = await self._db.execute_fetchall(
            "SELECT DISTINCT file_unique_id FROM manifest WHERE group_id = ?",
            (group_id,),
        )
        if not all_rows:
            return []
        all_fuids = [r[0] for r in all_rows]
        # 再取该频道已有的 file_unique_id
        existing_rows = await self._db.execute_fetchall(
            "SELECT file_unique_id FROM manifest WHERE group_id = ? AND channel_id = ?",
            (group_id, channel_id),
        )
        existing = {r[0] for r in existing_rows}
        missing_fuids = [f for f in all_fuids if f not in existing]
        if not missing_fuids:
            return []
        # 为每个缺失的 fuid 取一个源副本(从其他频道)
        placeholders = ",".join("?" * len(missing_fuids))
        src_rows = await self._db.execute_fetchall(
            f"SELECT file_unique_id, channel_id, message_id, media_type "
            f"FROM manifest WHERE group_id = ? AND file_unique_id IN ({placeholders}) "
            f"AND channel_id != ? "
            f"GROUP BY file_unique_id",  # 每个文件取一条
            [group_id, *missing_fuids, channel_id],
        )
        cols = ["file_unique_id", "src_channel_id", "src_message_id", "media_type"]
        return [dict(zip(cols, r)) for r in src_rows]

    async def pick_peer_copy(
        self, group_id: int, file_unique_id: str, exclude_channel: int,
    ) -> dict | None:
        """从存活频道取该文件的一个源副本(排除指定频道)。

        返回 {channel_id, message_id, media_type} 或 None。
        """
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT channel_id, message_id, media_type FROM manifest "
            "WHERE group_id = ? AND file_unique_id = ? AND channel_id != ? LIMIT 1",
            (group_id, file_unique_id, exclude_channel),
        )
        if not rows:
            return None
        return {"channel_id": rows[0][0], "message_id": rows[0][1], "media_type": rows[0][2]}

    async def has_manifest_for_channel(self, group_id: int, channel_id: int) -> bool:
        """该频道是否已有 manifest 记录(用于判断是否需要补齐)。"""
        if not self._db:
            return False
        rows = await self._db.execute_fetchall(
            "SELECT 1 FROM manifest WHERE group_id = ? AND channel_id = ? LIMIT 1",
            (group_id, channel_id),
        )
        return bool(rows)

    async def get_missing_from_src(
        self, group_id: int, src_channel_id: int, dst_channel_id: int,
    ) -> list[dict]:
        """返回 dst_channel 缺失、但 src_channel 有的文件列表。

        每条: {file_unique_id, src_message_id, media_type, media_group_id}
        用于常规复制(active→shadow):指定源为 active 频道。
        media_group_id 为分组键,mon_bot 据此避免跨批次拆散相册。
        """
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT a.file_unique_id, a.message_id, a.media_type, a.media_group_id "
            "FROM manifest a "
            "WHERE a.group_id = ? AND a.channel_id = ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM manifest b "
            "  WHERE b.group_id = a.group_id AND b.channel_id = ? "
            "  AND b.file_unique_id = a.file_unique_id"
            ")",
            (group_id, src_channel_id, dst_channel_id),
        )
        cols = ["file_unique_id", "src_message_id", "media_type", "media_group_id"]
        return [dict(zip(cols, r)) for r in rows]

    async def get_manifest_msg_id(
        self, group_id: int, channel_id: int, file_unique_id: str,
    ) -> int | None:
        """查询某文件在某频道的 message_id,不存在返回 None。"""
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT message_id FROM manifest "
            "WHERE group_id = ? AND channel_id = ? AND file_unique_id = ? LIMIT 1",
            (group_id, channel_id, file_unique_id),
        )
        return rows[0][0] if rows else None

    async def get_existing_file_in_group(
        self, group_id: int, file_unique_id: str,
    ) -> dict | None:
        """秒传去重:查询该组内是否已存在此 file_unique_id 的文件。

        优先返回 active 频道的记录,其次返回任一存活频道记录。
        返回 {channel_id, message_id, media_type, media_group_id} 或 None。
        """
        if not self._db or not file_unique_id:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT channel_id, message_id, media_type, media_group_id "
            "FROM manifest WHERE group_id = ? AND file_unique_id = ?",
            (group_id, file_unique_id),
        )
        if not rows:
            return None
        cols = ["channel_id", "message_id", "media_type", "media_group_id"]
        records = [dict(zip(cols, r)) for r in rows]
        # 优先返回 active 频道的记录(dsp_bot 主要从 active 读取)
        active_rows = await self._db.execute_fetchall(
            "SELECT channel_id FROM cells_local WHERE status = 'active'"
        )
        active_channels = {r[0] for r in active_rows}
        for r in records:
            if r["channel_id"] in active_channels:
                return r
        # 无 active 频道记录,返回第一条(shadow 频道,dsp_bot 可通过 message_backups 找到)
        return records[0]

    # ─── KV 键值存储（0 CRDB RU）───

    async def get_kv(self, key: str) -> str | None:
        """读取键值缓存（SQLite，零 CRDB RU）"""
        if not self._db:
            return None
        try:
            row = await self._db.execute_fetchall(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            )
            return row[0][0] if row else None
        except Exception:
            return None

    async def set_kv(self, key: str, value: str):
        """写入键值缓存（SQLite，零 CRDB RU）"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                    (key, value),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                if self._in_writer_tx:
                    raise
                return  # 锁冲突静默跳过,KV 缓存下一轮会重新写入

    # ─── R42 P0-3: AdminPrincipal 持久化身份 + bootstrap ───

    async def get_admin_principal_record(self, principal_id: int) -> dict | None:
        """R42 P0-3: 根据 principal_id 读取 admin_principals 记录。

        Args:
            principal_id: 管理员主体 ID

        Returns:
            {id, username, password_hash, roles, is_active, created_at, updated_at};
            不存在或 DB 不可用时返回 None
        """
        if not self._db or not principal_id:
            return None
        try:
            rows = await self._db.execute_fetchall(
                "SELECT id, username, password_hash, roles, is_active, created_at, updated_at "
                "FROM admin_principals WHERE id = ?",
                (principal_id,),
            )
            if not rows:
                return None
            r = rows[0]
            import json as _json_ap
            try:
                roles = _json_ap.loads(r[3]) if r[3] else []
            except (ValueError, TypeError):
                roles = []
            return {
                "id": int(r[0]),
                "username": str(r[1] or ""),
                "password_hash": str(r[2] or ""),
                "roles": roles,
                "is_active": bool(r[4]),
                "created_at": str(r[5] or ""),
                "updated_at": str(r[6] or ""),
            }
        except Exception as e:
            logger.warning(f"[CacheStore] get_admin_principal_record 失败 id={principal_id}: {e}")
            return None

    async def get_admin_principal_by_username(self, username: str) -> dict | None:
        """R42 P0-3: 根据 username 读取 admin_principals 记录。"""
        if not self._db or not username:
            return None
        try:
            rows = await self._db.execute_fetchall(
                "SELECT id, username, password_hash, roles, is_active, created_at, updated_at "
                "FROM admin_principals WHERE username = ?",
                (username,),
            )
            if not rows:
                return None
            r = rows[0]
            import json as _json_ap
            try:
                roles = _json_ap.loads(r[3]) if r[3] else []
            except (ValueError, TypeError):
                roles = []
            return {
                "id": int(r[0]),
                "username": str(r[1] or ""),
                "password_hash": str(r[2] or ""),
                "roles": roles,
                "is_active": bool(r[4]),
                "created_at": str(r[5] or ""),
                "updated_at": str(r[6] or ""),
            }
        except Exception as e:
            logger.warning(f"[CacheStore] get_admin_principal_by_username 失败 user={username}: {e}")
            return None

    async def list_admin_principal_roles(self, principal_id: int) -> list[str]:
        """R42 P0-3: 读取 principal 的所有角色名(从 admin_principal_roles 表)。

        Args:
            principal_id: 管理员主体 ID

        Returns:
            角色名列表(如 ["super_admin"]);无记录或异常时返回空列表
        """
        if not self._db or not principal_id:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT role_name FROM admin_principal_roles WHERE principal_id = ?",
                (principal_id,),
            )
            return [str(r[0]) for r in rows] if rows else []
        except Exception as e:
            logger.warning(f"[CacheStore] list_admin_principal_roles 失败 id={principal_id}: {e}")
            return []

    async def bootstrap_admin_principal(
        self,
        principal_id: int,
        username: str,
        roles: list[str] | None = None,
        password_hash: str = "",
    ) -> bool:
        """R42 P0-3: 原子 bootstrap 管理员身份(单事务 UPSERT + 分配角色 + 写审计)。

        步骤(全部在单 transaction 中,失败回滚):
          1. UPSERT admin_principals 记录(id=principal_id, username=username)
          2. 清除该 principal 的旧角色映射(admin_principal_roles)
          3. 为该 principal 分配 roles 中的角色
          4. 写 audit_log(action="bootstrap_admin_principal", actor_id=0)

        幂等:重复调用不报错,角色映射会被覆盖为新值。

        Args:
            principal_id: 管理员主体 ID(必须 > 0)
            username: 管理员用户名
            roles: 角色列表(默认 ["super_admin"])
            password_hash: 密码哈希(可选,bootstrap 阶段通常为空)

        Returns:
            True 表示成功;False 表示失败(principal_id <= 0 或 DB 异常)
        """
        if not principal_id or principal_id <= 0:
            logger.warning("[CacheStore] bootstrap_admin_principal principal_id 必须 > 0")
            return False
        if not username:
            logger.warning("[CacheStore] bootstrap_admin_principal username 不能为空")
            return False
        if roles is None:
            roles = ["super_admin"]

        if not self._db:
            logger.warning("[CacheStore] bootstrap_admin_principal DB 未初始化")
            return False

        import json as _json_bp
        now = datetime.datetime.now().isoformat()
        roles_json = _json_bp.dumps(roles)

        try:
            async with self.transaction() as tx:
                # 1. UPSERT admin_principals 记录
                await tx.execute(
                    "INSERT OR REPLACE INTO admin_principals "
                    "(id, username, password_hash, roles, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (principal_id, username, password_hash, roles_json, now, now),
                )
                # 2. 清除旧角色映射(幂等:重复 bootstrap 覆盖角色)
                await tx.execute(
                    "DELETE FROM admin_principal_roles WHERE principal_id = ?",
                    (principal_id,),
                )
                # 3. 分配新角色
                for role_name in roles:
                    role_name = role_name.strip()
                    if not role_name:
                        continue
                    await tx.execute(
                        "INSERT OR IGNORE INTO admin_principal_roles "
                        "(principal_id, role_name, granted_by, granted_at) "
                        "VALUES (?, ?, ?, ?)",
                        (principal_id, role_name, 0, now),
                    )
                # 4. 写审计日志(action=bootstrap_admin_principal, actor_id=0)
                await tx.execute(
                    "INSERT INTO audit_log (actor_id, actor_type, action, target_type, "
                    "target_id, details, ip_addr, created_at) "
                    "VALUES (?, 'system', 'bootstrap_admin_principal', 'admin_principal', ?, ?, '', ?)",
                    (
                        0,
                        str(principal_id),
                        _json_bp.dumps({"username": username, "roles": roles}),
                        now,
                    ),
                )
                # dirty_outbox 标记(admin_principals 为本地表,但仍记录以供审计)
                try:
                    await self.add_dirty_outbox(
                        "admin_principals", str(principal_id),
                        operation="upsert", connection=tx,
                    )
                except Exception as de:
                    logger.debug(f"[CacheStore] bootstrap dirty_outbox 标记失败(可忽略): {de}")
            logger.info(
                f"[CacheStore] bootstrap_admin_principal 成功 id={principal_id} "
                f"user={username} roles={roles}"
            )
            return True
        except Exception as e:
            logger.error(
                f"[CacheStore] bootstrap_admin_principal 失败 id={principal_id} "
                f"user={username}: {e}"
            )
            return False

    async def cache_get(self, key: str, ttl: float):
        """C2: 读取 TTL 缓存。如果缓存不存在或已过期,返回 None。

        value 会被 JSON 反序列化后返回。
        """
        if not self._db:
            return None
        try:
            import time as _time
            import json
            rows = await self._db.execute_fetchall(
                "SELECT value, updated_at FROM ttl_cache WHERE key = ?", (key,)
            )
            if not rows:
                return None
            value_str, updated_at = rows[0]
            if _time.time() - updated_at > ttl:
                return None
            return json.loads(value_str)
        except Exception:
            return None

    async def cache_set(self, key: str, value):
        """C2: 写入 TTL 缓存。value 会被 JSON 序列化。"""
        if not self._db:
            return
        try:
            import time as _time
            import json
            value_str = json.dumps(value, default=str)
            now = _time.time()
            await self._db.execute(
                "INSERT OR REPLACE INTO ttl_cache (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value_str, now),
            )
            await self._db.commit()
        except Exception as e:
            if self._in_writer_tx:
                raise
            logger.debug(f"[cache_store] cache_set 失败: {e}")

    async def cache_delete_prefix(self, prefix: str):
        """C2: 删除所有以指定前缀开头的缓存条目(用于批量失效)。"""
        if not self._db:
            return
        try:
            await self._db.execute(
                "DELETE FROM ttl_cache WHERE key LIKE ?", (prefix + "%",)
            )
            await self._db.commit()
        except Exception as e:
            logger.debug(f"[cache_store] cache_delete_prefix 失败: {e}")

    # ─── 热路径全表缓存 CRUD：file_records / codes / users / external_code_mapping ───

    async def bootstrap_file_records(self, rows: list[dict]):
        """启动时从 CRDB 全量加载 file_records 到 SQLite（清空旧数据）"""
        if not self._db or not rows:
            return
        await self._db.execute("DELETE FROM file_records_local")
        # 序列化可能为 list/dict/datetime 的字段（CRDB _row_to_dict 会反序列化 JSONB / 转 datetime）
        import json as _json
        from datetime import datetime as _dt
        def _serialize(val):
            if val is None:
                return None
            if isinstance(val, _dt):
                return val.isoformat()
            if isinstance(val, (list, dict)):
                return _json.dumps(val, default=str)
            return val
        records = []
        for r in rows:
            records.append((
                r.get("file_code"), r.get("uploader_id"),
                r.get("primary_channel_id"), r.get("primary_channel_msg_id"),
                _serialize(r.get("file_types")), _serialize(r.get("backup_channel_msg_ids")),
                r.get("batch_msg_ids"), _serialize(r.get("batch_file_meta")),
                r.get("file_ids"), r.get("status", "active"),
                r.get("request_count", 0), int(r.get("protect_content", 0) or 0),
                r.get("file_ttl_days", 0), r.get("note", ""),
                r.get("expire_time"), _serialize(r.get("blocked_users", "[]")),
                r.get("create_time"), r.get("updated_at"),
                int(r.get("max_requests", 0) or 0),
                int(r.get("is_collection", 0) or 0), r.get("collection_codes", "[]") or "[]",
                1,  # crdb_synced=1
            ))
        await self._db.executemany(
            """INSERT OR REPLACE INTO file_records_local
            (file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
             file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
             file_ids, status, request_count, protect_content, file_ttl_days, note,
             expire_time, blocked_users, create_time, updated_at, max_requests,
             is_collection, collection_codes, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )
        await self._db.commit()

    async def get_file_record_local(self, file_code: str) -> dict | None:
        """从 SQLite 读取 file_record（0 CRDB RU），自动反序列化 JSON 字段。"""
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            """SELECT file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
                      file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
                      file_ids, status, request_count, protect_content, file_ttl_days, note,
                      expire_time, blocked_users, create_time, updated_at, max_requests,
                      is_collection, collection_codes
               FROM file_records_local WHERE file_code = ?""",
            (file_code,),
        )
        if not rows:
            return None
        r = rows[0]
        return _deserialize_sqlite_row({
            "file_code": r[0], "uploader_id": r[1], "primary_channel_id": r[2],
            "primary_channel_msg_id": r[3], "file_types": r[4],
            "backup_channel_msg_ids": r[5], "batch_msg_ids": r[6],
            "batch_file_meta": r[7], "file_ids": r[8], "status": r[9],
            "request_count": r[10], "protect_content": r[11], "file_ttl_days": r[12],
            "note": r[13], "expire_time": r[14], "blocked_users": r[15],
            "create_time": r[16], "updated_at": r[17], "max_requests": r[18],
            "is_collection": r[19], "collection_codes": r[20],
        })

    async def find_file_record_by_external_code(self, ext_code: str) -> dict | None:
        """通过 note 中的 external code 反查文件记录（用于历史遗留的 mapped_codes.file_code 为空的情况）。
        兼容 note 中 code 为原始 emoji 码或 hex 编码（H:前缀）两种情况。"""
        if not self._db:
            return None
        # 构造候选 key 列表：原始码 + hex 编码码（兼容旧数据）
        candidates = [ext_code]
        try:
            hex_code = "H:" + ext_code.encode('utf-8').hex()
            candidates.append(hex_code)
        except Exception:
            pass
        for cand in candidates:
            rows = await self._db.execute_fetchall(
                """SELECT file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
                          file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
                          file_ids, status, request_count, protect_content, file_ttl_days, note,
                          expire_time, blocked_users, create_time, updated_at, max_requests,
                          is_collection, collection_codes
                   FROM file_records_local WHERE note LIKE ? LIMIT 1""",
                (f'%"code":"{cand}"%',),
            )
            if rows:
                break
        if not rows:
            return None
        r = rows[0]
        return _deserialize_sqlite_row({
            "file_code": r[0], "uploader_id": r[1], "primary_channel_id": r[2],
            "primary_channel_msg_id": r[3], "file_types": r[4],
            "backup_channel_msg_ids": r[5], "batch_msg_ids": r[6],
            "batch_file_meta": r[7], "file_ids": r[8], "status": r[9],
            "request_count": r[10], "protect_content": r[11], "file_ttl_days": r[12],
            "note": r[13], "expire_time": r[14], "blocked_users": r[15],
            "create_time": r[16], "updated_at": r[17], "max_requests": r[18],
            "is_collection": r[19], "collection_codes": r[20],
        })

    async def upsert_file_record_local(self, record: dict, mark_dirty: bool = True, _batch: bool = False):
        """写入/更新 file_record 到 SQLite"""
        if not self._db:
            return
        synced = 0 if mark_dirty else 1
        # 序列化可能为 list/dict/datetime 的字段（CRDB _row_to_dict 会反序列化 JSONB / 转 datetime）
        import json as _json
        from datetime import datetime as _dt
        def _serialize(val):
            if val is None:
                return None
            if isinstance(val, _dt):
                return val.isoformat()
            if isinstance(val, (list, dict)):
                return _json.dumps(val, default=str)
            return val
        await self._db.execute(
            """INSERT OR REPLACE INTO file_records_local
            (file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
             file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
             file_ids, status, request_count, protect_content, file_ttl_days, note,
             expire_time, blocked_users, create_time, updated_at, max_requests,
             is_collection, collection_codes, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.get("file_code"), record.get("uploader_id"),
             record.get("primary_channel_id"), record.get("primary_channel_msg_id"),
             _serialize(record.get("file_types")), _serialize(record.get("backup_channel_msg_ids")),
             record.get("batch_msg_ids"), _serialize(record.get("batch_file_meta")),
             record.get("file_ids"), record.get("status", "active"),
             record.get("request_count", 0), int(record.get("protect_content", 0) or 0),
             record.get("file_ttl_days", 0), record.get("note", ""),
             record.get("expire_time"), _serialize(record.get("blocked_users", "[]")),
             record.get("create_time"), record.get("updated_at"),
             int(record.get("max_requests", 0) or 0),
             int(record.get("is_collection", 0) or 0), record.get("collection_codes", "[]") or "[]",
             synced),
        )
        # R40 P0-5: dirty_outbox 写入同事务(在 commit 之前,确保原子性)
        if mark_dirty:
            try:
                _fr_payload = _json.dumps(record, default=str)
                if isinstance(_fr_payload, bytes):
                    _fr_payload = _fr_payload.decode()
                await self.add_dirty_outbox(
                    "file_records", record.get("file_code", ""), "upsert", _fr_payload,
                )
            except Exception as _e:
                logger.warning(f"[CacheStore] upsert_file_record_local dirty_outbox 失败: {_e}")
        if not _batch:
            await self._db.commit()

    async def get_dirty_file_records(self, limit: int = 100) -> list[dict]:
        """获取需要同步到 CRDB 的脏 file_records"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
                      file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
                      file_ids, status, request_count, protect_content, file_ttl_days, note,
                      expire_time, blocked_users, create_time, updated_at, max_requests,
                      is_collection, collection_codes
               FROM file_records_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        return [_deserialize_sqlite_row({
            "file_code": r[0], "uploader_id": r[1], "primary_channel_id": r[2],
            "primary_channel_msg_id": r[3], "file_types": r[4], "backup_channel_msg_ids": r[5],
            "batch_msg_ids": r[6], "batch_file_meta": r[7], "file_ids": r[8], "status": r[9],
            "request_count": r[10], "protect_content": r[11], "file_ttl_days": r[12],
            "note": r[13], "expire_time": r[14], "blocked_users": r[15],
            "create_time": r[16], "updated_at": r[17], "max_requests": r[18],
            "is_collection": r[19], "collection_codes": r[20],
        }) for r in rows]

    async def mark_file_record_synced(self, file_code: str):
        if not self._db:
            return
        await self._db.execute(
            "UPDATE file_records_local SET crdb_synced = 1 WHERE file_code = ?",
            (file_code,),
        )
        await self._db.commit()

    async def delete_file_record_local(self, file_code: str):
        """R38 P1-3: 从 SQLite 本地缓存中软删除 file_record(tombstone)。

        原 N-M13 实现用 DELETE FROM,会丢失行用于 CRDB tombstone 同步的依据。
        新实现用 UPDATE ... SET deleted_at=?, status='deleted',
        crdb_sync 通过 dirty_outbox / crdb_synced=0 检测到 tombstone 后,
        向 CRDB 发送对应 DELETE 或 status='deleted' 更新,保证跨节点一致。
        """
        if not self._db:
            return
        await self._db.execute(
            "UPDATE file_records_local SET deleted_at = ?, status = 'deleted', "
            "crdb_synced = 0 WHERE file_code = ?",
            (datetime.datetime.now().isoformat(), file_code),  # type: ignore[attr-defined]
        )
        await self._db.commit()

    # ─── codes_local ───

    async def bootstrap_codes(self, rows: list[dict]):
        if not self._db or not rows:
            return
        await self._db.execute("DELETE FROM codes_local")
        records = []
        for r in rows:
            records.append((
                r.get("code"), r.get("file_record_code"), r.get("uploader_id"),
                r.get("file_types"), r.get("batch_msg_ids"), r.get("batch_file_meta"),
                r.get("primary_channel_id"), r.get("status", "active"),
                r.get("created_at"), r.get("expire_time"), r.get("note", ""),
                1,  # crdb_synced=1
            ))
        await self._db.executemany(
            """INSERT OR REPLACE INTO codes_local
            (code, file_record_code, uploader_id, file_types, batch_msg_ids,
             batch_file_meta, primary_channel_id, status, created_at, expire_time,
             note, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )
        await self._db.commit()

    async def get_code_local(self, code: str) -> dict | None:
        """从 SQLite 读取 code 记录（0 CRDB RU），自动反序列化 JSON 字段。"""
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            """SELECT code, file_record_code, uploader_id, file_types, batch_msg_ids,
                      batch_file_meta, primary_channel_id, status, created_at,
                      expire_time, note
               FROM codes_local WHERE code = ?""",
            (code,),
        )
        if not rows:
            return None
        r = rows[0]
        return _deserialize_sqlite_row({
            "code": r[0], "file_record_code": r[1], "uploader_id": r[2],
            "file_types": r[3], "batch_msg_ids": r[4], "batch_file_meta": r[5],
            "primary_channel_id": r[6], "status": r[7], "created_at": r[8],
            "expire_time": r[9], "note": r[10],
        })

    async def upsert_code_local(self, record: dict, mark_dirty: bool = True, _batch: bool = False):
        if not self._db:
            return
        synced = 0 if mark_dirty else 1
        import json as _json
        from datetime import datetime as _dt
        def _serialize(val):
            if val is None:
                return None
            if isinstance(val, _dt):
                return val.isoformat()
            if isinstance(val, (list, dict)):
                return _json.dumps(val, default=str)
            return val
        await self._db.execute(
            """INSERT OR REPLACE INTO codes_local
            (code, file_record_code, uploader_id, file_types, batch_msg_ids,
             batch_file_meta, primary_channel_id, status, created_at, expire_time,
             note, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.get("code"), record.get("file_record_code"), record.get("uploader_id"),
             _serialize(record.get("file_types")), record.get("batch_msg_ids"), _serialize(record.get("batch_file_meta")),
             record.get("primary_channel_id"), record.get("status", "active"),
             record.get("created_at"), record.get("expire_time"), record.get("note", ""),
             synced),
        )
        # R40 P0-5: dirty_outbox 写入同事务(在 commit 之前,确保原子性)
        if mark_dirty:
            try:
                _ce_payload = _json.dumps(record, default=str)
                if isinstance(_ce_payload, bytes):
                    _ce_payload = _ce_payload.decode()
                await self.add_dirty_outbox(
                    "codes", record.get("code", ""), "upsert", _ce_payload,
                )
            except Exception as _e:
                logger.warning(f"[CacheStore] upsert_code_local dirty_outbox 失败: {_e}")
        if not _batch:
            await self._db.commit()

    async def get_dirty_codes(self, limit: int = 100) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT code, file_record_code, uploader_id, file_types, batch_msg_ids,
                      batch_file_meta, primary_channel_id, status, created_at,
                      expire_time, note
               FROM codes_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        return [_deserialize_sqlite_row({
            "code": r[0], "file_record_code": r[1], "uploader_id": r[2],
            "file_types": r[3], "batch_msg_ids": r[4], "batch_file_meta": r[5],
            "primary_channel_id": r[6], "status": r[7], "created_at": r[8],
            "expire_time": r[9], "note": r[10],
        }) for r in rows]

    async def mark_code_synced(self, code: str):
        if not self._db:
            return
        await self._db.execute(
            "UPDATE codes_local SET crdb_synced = 1 WHERE code = ?", (code,),
        )
        await self._db.commit()

    # ─── users_local ───

    async def bootstrap_users(self, rows: list[dict]):
        if not self._db or not rows:
            return
        await self._db.execute("DELETE FROM users_local")
        records = []
        for r in rows:
            records.append((
                r.get("user_id"), r.get("username"), r.get("first_name"),
                r.get("membership_level", "free"), r.get("daily_decode_quota", 3),
                r.get("quota_used_today", 0), r.get("quota_date"),
                r.get("can_upload", 0), r.get("external_decode_quota", 0),
                r.get("external_used_today", 0), r.get("external_quota_date"),
                r.get("is_banned", 0), r.get("created_at"), r.get("updated_at"),
                1,  # crdb_synced=1
                r.get("ban_expires_at"),
            ))
        await self._db.executemany(
            """INSERT OR REPLACE INTO users_local
            (user_id, username, first_name, membership_level, daily_decode_quota,
             quota_used_today, quota_date, can_upload, external_decode_quota,
             external_used_today, external_quota_date, is_banned,
             created_at, updated_at, crdb_synced, ban_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )
        await self._db.commit()

    async def get_user_local(self, user_id: int) -> dict | None:
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            """SELECT user_id, username, first_name, membership_level,
                      daily_decode_quota, quota_used_today, quota_date, can_upload,
                      external_decode_quota, external_used_today, external_quota_date,
                      is_banned, created_at, updated_at, ban_expires_at
               FROM users_local WHERE user_id = ?""",
            (user_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "username": r[1], "first_name": r[2],
            "membership_level": r[3], "daily_decode_quota": r[4],
            "quota_used_today": r[5], "quota_date": r[6], "can_upload": r[7],
            "external_decode_quota": r[8], "external_used_today": r[9],
            "external_quota_date": r[10], "is_banned": r[11],
            "created_at": r[12], "updated_at": r[13],
            "ban_expires_at": r[14],
        }

    async def upsert_user_local(self, user: dict, mark_dirty: bool = True, _batch: bool = False):
        if not self._db:
            return
        synced = 0 if mark_dirty else 1
        await self._db.execute(
            """INSERT OR REPLACE INTO users_local
            (user_id, username, first_name, membership_level, daily_decode_quota,
             quota_used_today, quota_date, can_upload, external_decode_quota,
             external_used_today, external_quota_date, is_banned,
             created_at, updated_at, crdb_synced, ban_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user.get("user_id"), user.get("username"), user.get("first_name"),
             user.get("membership_level", "free"), user.get("daily_decode_quota", 3),
             user.get("quota_used_today", 0), user.get("quota_date"),
             user.get("can_upload", 0), user.get("external_decode_quota", 0),
             user.get("external_used_today", 0), user.get("external_quota_date"),
             user.get("is_banned", 0), user.get("created_at"), user.get("updated_at"),
             synced, user.get("ban_expires_at")),
        )
        if not _batch:
            await self._db.commit()

    async def get_dirty_users(self, limit: int = 100) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT user_id, username, first_name, membership_level,
                      daily_decode_quota, quota_used_today, quota_date, can_upload,
                      external_decode_quota, external_used_today, external_quota_date,
                      is_banned, created_at, updated_at, ban_expires_at
               FROM users_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        return [{
            "user_id": r[0], "username": r[1], "first_name": r[2],
            "membership_level": r[3], "daily_decode_quota": r[4],
            "quota_used_today": r[5], "quota_date": r[6], "can_upload": r[7],
            "external_decode_quota": r[8], "external_used_today": r[9],
            "external_quota_date": r[10], "is_banned": r[11],
            "created_at": r[12], "updated_at": r[13],
            "ban_expires_at": r[14],
        } for r in rows]

    async def mark_user_synced(self, user_id: int):
        if not self._db:
            return
        await self._db.execute(
            "UPDATE users_local SET crdb_synced = 1 WHERE user_id = ?", (user_id,),
        )
        await self._db.commit()

    # ─── R36 §6.4.5: Admin 走 SQLite read model,不再 CRDB 热 COUNT/regex ───

    async def count_users_local(self, search: str = "") -> int:
        """Admin /users 页 count — 走 SQLite,0 RU。

        Args:
            search: 搜索关键词(空字符串=全部)。支持 user_id(数字)或 username/first_name LIKE。
        """
        if not self._db:
            return 0
        if not search:
            row = await self._db.execute_fetchall("SELECT COUNT(*) FROM users_local")
            return int(row[0][0]) if row else 0
        if search.isdigit():
            uid = int(search)
            row = await self._db.execute_fetchall(
                "SELECT COUNT(*) FROM users_local WHERE user_id = ?", (uid,),
            )
            return int(row[0][0]) if row else 0
        like = f"%{search}%"
        row = await self._db.execute_fetchall(
            "SELECT COUNT(*) FROM users_local WHERE username LIKE ? OR first_name LIKE ?",
            (like, like),
        )
        return int(row[0][0]) if row else 0

    async def list_users_local_paginated(
        self, search: str = "", skip: int = 0, limit: int = 20,
        sort_field: str = "created_at", sort_dir: str = "desc",
    ) -> list[dict]:
        """Admin /users 页 list — 走 SQLite,0 RU,LIKE 搜索 + 分页 + 排序。

        Args:
            search: 搜索关键词(空=全部,数字=user_id,其他=username/first_name LIKE)
            skip/limit: 分页
            sort_field: 排序字段(created_at/updated_at/user_id)
            sort_dir: asc/desc
        """
        if not self._db:
            return []
        # 白名单排序字段,防 SQL 注入
        allowed_sort = {"created_at", "updated_at", "user_id", "username"}
        if sort_field not in allowed_sort:
            sort_field = "created_at"
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
        # R40: SQLite 不支持 NULLS FIRST/LAST,改用 ISNULL 函数(NULL 排在最后)
        null_sort = f"{sort_field} IS NULL," if sort_field in ("created_at", "updated_at") else ""

        where, params = "", []
        if search:
            if search.isdigit():
                where, params = "WHERE user_id = ?", [int(search)]
            else:
                like = f"%{search}%"
                where, params = "WHERE username LIKE ? OR first_name LIKE ?", [like, like]
        params.extend([limit, skip])
        sql = (
            f"SELECT user_id, username, first_name, membership_level, "
            f"daily_decode_quota, quota_used_today, quota_date, can_upload, "
            f"external_decode_quota, external_used_today, external_quota_date, "
            f"is_banned, created_at, updated_at "
            f"FROM users_local {where} "
            f"ORDER BY {null_sort} {sort_field} {direction} LIMIT ? OFFSET ?"
        )
        rows = await self._db.execute_fetchall(sql, params)
        return [{
            "user_id": r[0], "username": r[1], "first_name": r[2],
            "membership_level": r[3], "daily_decode_quota": r[4],
            "quota_used_today": r[5], "quota_date": r[6], "can_upload": r[7],
            "external_decode_quota": r[8], "external_used_today": r[9],
            "external_quota_date": r[10], "is_banned": r[11],
            "created_at": r[12], "updated_at": r[13],
        } for r in rows]

    async def count_file_records_local(self, search: str = "", status: str = "") -> int:
        """Admin /files 页 count — 走 SQLite,0 RU。

        Args:
            search: 搜索关键词(空=全部,数字=uploader_id,其他=file_code LIKE)
            status: 状态过滤(active/deleted 等,空=不过滤)
        """
        if not self._db:
            return 0
        where_parts, params = [], []
        if search:
            if search.isdigit():
                where_parts.append("uploader_id = ?")
                params.append(int(search))
            else:
                where_parts.append("file_code LIKE ?")
                params.append(f"%{search}%")
        if status:
            where_parts.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        row = await self._db.execute_fetchall(
            f"SELECT COUNT(*) FROM file_records_local {where}", params,
        )
        return int(row[0][0]) if row else 0

    async def list_file_records_local_paginated(
        self, search: str = "", skip: int = 0, limit: int = 20,
        sort_field: str = "create_time", sort_dir: str = "desc",
        status: str = "",
    ) -> list[dict]:
        """Admin /files 页 list — 走 SQLite,0 RU,LIKE 搜索 + 分页 + 排序。"""
        if not self._db:
            return []
        allowed_sort = {"create_time", "updated_at", "file_code", "request_count"}
        if sort_field not in allowed_sort:
            sort_field = "create_time"
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
        null_sort = f"{sort_field} IS NULL," if sort_field in ("create_time", "updated_at") else ""

        where_parts, params = [], []
        if search:
            if search.isdigit():
                where_parts.append("uploader_id = ?")
                params.append(int(search))
            else:
                where_parts.append("file_code LIKE ?")
                params.append(f"%{search}%")
        if status:
            where_parts.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params.extend([limit, skip])
        sql = (
            f"SELECT file_code, uploader_id, primary_channel_id, primary_channel_msg_id, "
            f"file_types, status, request_count, protect_content, file_ttl_days, note, "
            f"expire_time, create_time, updated_at, max_requests, is_collection "
            f"FROM file_records_local {where} "
            f"ORDER BY {null_sort} {sort_field} {direction} LIMIT ? OFFSET ?"
        )
        rows = await self._db.execute_fetchall(sql, params)
        return [_deserialize_sqlite_row({
            "file_code": r[0], "uploader_id": r[1], "primary_channel_id": r[2],
            "primary_channel_msg_id": r[3], "file_types": r[4], "status": r[5],
            "request_count": r[6], "protect_content": r[7], "file_ttl_days": r[8],
            "note": r[9], "expire_time": r[10], "create_time": r[11],
            "updated_at": r[12], "max_requests": r[13], "is_collection": r[14],
        }) for r in rows]

    # ─── external_code_mapping_local ───

    async def bootstrap_external_mappings(self, rows: list[dict]):
        if not self._db or not rows:
            return
        await self._db.execute("DELETE FROM external_code_mapping_local")
        records = [(r["external_code"], r["system_code"], r.get("bot_username"),
                    r.get("created_at"), r.get("updated_at"), 1) for r in rows]
        await self._db.executemany(
            """INSERT OR REPLACE INTO external_code_mapping_local
            (external_code, system_code, bot_username, created_at, updated_at, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?)""",
            records,
        )
        await self._db.commit()

    async def get_external_mapping_local(self, external_code: str) -> dict | None:
        if not self._db:
            return None
        rows = await self._db.execute_fetchall(
            "SELECT external_code, system_code, bot_username, created_at, updated_at "
            "FROM external_code_mapping_local WHERE external_code = ?",
            (external_code,),
        )
        if not rows:
            return None
        r = rows[0]
        return {"external_code": r[0], "system_code": r[1], "bot_username": r[2],
                "created_at": r[3], "updated_at": r[4]}

    async def get_all_external_mappings_local(self) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT external_code, system_code, bot_username, created_at, updated_at "
            "FROM external_code_mapping_local"
        )
        return [{"external_code": r[0], "system_code": r[1], "bot_username": r[2],
                 "created_at": r[3], "updated_at": r[4]} for r in rows]

    # ─── R46 P0-3: unregistered_copies 持久化操作 ──────────────

    async def insert_unregistered_copy(
        self, upload_id: str, file_unique_id: str, channel_id: int,
        message_id: int, media_group_id: str = "", reason: str = "",
    ) -> bool:
        """R46 P0-3: 持久化 COPIED_UNREGISTERED 记录。

        Telegram copy 成功但 outbox 写失败时调用,持久化目标 channel/message_id,
        进程重启后可扫描未 reconciled 行优先补 Manifest。
        """
        if not self._db:
            return False
        from datetime import datetime as _dt
        now = _dt.utcnow().isoformat()
        try:
            await self._db.execute(
                "INSERT OR IGNORE INTO unregistered_copies "
                "(upload_id, file_unique_id, media_group_id, channel_id, "
                " message_id, state, reason, created_at, reconciled_at) "
                "VALUES (?, ?, ?, ?, ?, 'COPIED_UNREGISTERED', ?, ?, NULL)",
                (upload_id, file_unique_id, media_group_id or None,
                 channel_id, message_id, reason, now),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[CacheStore] insert_unregistered_copy 失败: {e}")
            return False

    async def mark_unregistered_copy_reconciled(
        self, upload_id: str, file_unique_id: str, channel_id: int,
        message_id: int,
    ) -> bool:
        """R46 P0-3: Manifest outbox 成功后标记 reconciled。"""
        if not self._db:
            return False
        from datetime import datetime as _dt
        now = _dt.utcnow().isoformat()
        try:
            cursor = await self._db.execute(
                "UPDATE unregistered_copies SET reconciled_at = ? "
                "WHERE upload_id = ? AND file_unique_id = ? "
                "AND channel_id = ? AND message_id = ?",
                (now, upload_id, file_unique_id, channel_id, message_id),
            )
            await self._db.commit()
            return bool(cursor and cursor.rowcount > 0)
        except Exception as e:
            logger.error(f"[CacheStore] mark_unregistered_copy_reconciled 失败: {e}")
            return False

    async def list_unreconciled_copies(self, limit: int = 100) -> list[dict]:
        """R46 P0-3: 启动时扫描未 reconciled 行,优先补 Manifest。"""
        if not self._db:
            return []
        try:
            cursor = await self._db.execute(
                "SELECT upload_id, file_unique_id, media_group_id, "
                "channel_id, message_id, state, reason, created_at "
                "FROM unregistered_copies WHERE reconciled_at IS NULL "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {"upload_id": r[0], "file_unique_id": r[1],
                 "media_group_id": r[2], "channel_id": r[3],
                 "message_id": r[4], "state": r[5], "reason": r[6],
                 "created_at": r[7]}
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[CacheStore] list_unreconciled_copies 失败: {e}")
            return []

    # ─── R46 P1: entity_versions 原子 version 分配 ──────────────

    async def allocate_version(self, table_name: str, pk: str, connection: Any = None) -> int:
        """R46 P1 / R47 P0-6: 原子分配递增 version,解决 MAX+1 并发竞态。

        R47 P0-6 变更:
            - 新增 connection 参数,允许在调用方事务内执行(保证与 dirty_outbox INSERT 同事务原子)
            - 使用 INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING 单语句原子分配
              (SQLite 3.35+ 支持 RETURNING;若 RETURNING 不可用则 fallback 到两步查询)
            - 调用方未传 connection 时,使用 self._db 并自动 commit

        Args:
            table_name: 逻辑表名(如 users / file_records / codes_local)
            pk: 行主键值(字符串)
            connection: 可选事务连接,传入时不自动 commit(由调用方控制事务)

        Returns:
            分配的新 version(int,从 1 开始单调递增);失败时返回 1(不阻塞主流程)
        """
        # R47 P0-6: 优先使用调用方传入的事务连接
        _conn = connection if connection is not None else self._db
        if not _conn:
            return 1
        try:
            # 优先尝试 RETURNING 子句(SQLite 3.35+),单语句原子分配
            try:
                cursor = await _conn.execute(
                    "INSERT INTO entity_versions (table_name, pk, version) "
                    "VALUES (?, ?, 1) "
                    "ON CONFLICT(table_name, pk) DO UPDATE SET version = version + 1 "
                    "RETURNING version",
                    (table_name, pk),
                )
                row = await cursor.fetchone()
                if row is not None:
                    return int(row[0])
            except Exception as _e_returning:
                # RETURNING 不可用(SQLite < 3.35),fallback 到两步查询
                logger.debug(
                    f"[CacheStore] allocate_version RETURNING 不可用,fallback 两步: {_e_returning}"
                )
            # Fallback: INSERT/UPDATE 后再 SELECT(非原子,但兼容旧 SQLite)
            await _conn.execute(
                "INSERT INTO entity_versions (table_name, pk, version) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(table_name, pk) DO UPDATE SET version = version + 1",
                (table_name, pk),
            )
            cursor = await _conn.execute(
                "SELECT version FROM entity_versions WHERE table_name = ? AND pk = ?",
                (table_name, pk),
            )
            row = await cursor.fetchone()
            # 调用方未传 connection 时自动 commit;传 connection 时由调用方控制
            if connection is None and self._db:
                await self._db.commit()
            return int(row[0]) if row else 1
        except Exception as e:
            logger.error(f"[CacheStore] allocate_version 失败: {e}")
            return 1

    # ─── R48 P0-5: 迁移冲突处理框架(fail-fast) ──────────────

    async def _archive_conflicts_to_migration_conflicts(
        self, table_name: str, columns: list[str],
    ) -> int:
        """R48 P0-5: 通用迁移冲突归档 — 清理表中重复行并归档到 migration_conflicts。

        对指定表的 columns 组合扫描重复行,每组保留 created_at 最新(或 id 最大)的
        权威记录,其余记录序列化为 JSON 存入 migration_conflicts 后从原表删除。

        幂等:重复行清理后再次调用无副作用(无重复行可清理)。
        fail-fast:归档过程异常时 raise RuntimeError,不允许只 warning 后继续。

        Args:
            table_name: 目标表名(如 dirty_outbox)
            columns: 重复判定列(如 ["table_name", "pk", "version"])

        Returns:
            归档的冲突记录数(int)
        """
        if not self._db:
            return 0
        _cols_csv = ", ".join(columns)
        try:
            # 1. 扫描重复行组(列组合相同且 COUNT > 1)
            cursor = await self._db.execute(
                f"SELECT {_cols_csv}, COUNT(*) AS cnt "
                f"FROM {table_name} "
                f"GROUP BY {_cols_csv} "
                f"HAVING COUNT(*) > 1"
            )
            dup_groups = await cursor.fetchall()
            if not dup_groups:
                return 0
            import datetime as _dt
            import json as _json
            _now = _dt.datetime.now().isoformat()
            # 获取列名(用于 JSON 序列化),只查一次
            cur_cols = await self._db.execute(f"PRAGMA table_info({table_name})")
            col_names = [c[1] for c in await cur_cols.fetchall()]
            archived = 0
            for _row in dup_groups:
                # _row: (col1, col2, ..., cnt)
                _where = " AND ".join([f"{c} = ?" for c in columns])
                _params = list(_row[:len(columns)])
                # 拉取该组所有行,按 created_at DESC, id DESC 排序(权威记录排第一)
                cur_rows = await self._db.execute(
                    f"SELECT * FROM {table_name} WHERE {_where} "
                    f"ORDER BY created_at DESC, id DESC",
                    _params,
                )
                rows_all = await cur_rows.fetchall()
                if len(rows_all) < 2:
                    continue
                # 第一行为权威记录(最新 created_at 或最大 id),其余归档后删除
                for r in rows_all[1:]:
                    _rid = int(r[0])
                    _record_data = _json.dumps(
                        {col_names[i]: r[i] for i in range(len(col_names))},
                        ensure_ascii=False, default=str,
                    )
                    await self._db.execute(
                        "INSERT INTO migration_conflicts "
                        "(table_name, conflict_type, record_id, record_data, resolved_at, created_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?)",
                        (table_name, f"{table_name}_duplicate", _rid, _record_data, _now),
                    )
                    await self._db.execute(
                        f"DELETE FROM {table_name} WHERE id = ?",
                        (_rid,),
                    )
                    archived += 1
            await self._db.commit()
            if archived > 0:
                logger.warning(
                    f"[CacheStore] {table_name} 迁移冲突归档: "
                    f"清理 {archived} 条重复行到 migration_conflicts"
                )
            return archived
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"[CacheStore] _archive_conflicts_to_migration_conflicts 失败: {e}")
            raise RuntimeError(
                f"{table_name} 重复行归档失败,拒绝启动: {e}"
            ) from e

    async def _verify_unique_constraint_or_fail(
        self, table_name: str, index_name: str, columns: list[str],
    ) -> None:
        """R48 P0-5: 通用 UNIQUE 约束验证 — PRAGMA 验证索引存在且为 UNIQUE,失败 raise RuntimeError。

        CREATE UNIQUE INDEX IF NOT EXISTS 在同名索引已存在时会静默跳过(即使该索引
        非 UNIQUE),因此必须通过 PRAGMA index_list 二次验证 unique 标志位。

        Args:
            table_name: 目标表名
            index_name: 期望存在的 UNIQUE 索引名
            columns: 索引列(用于错误日志)

        Raises:
            RuntimeError: 索引不存在或非 UNIQUE(fail-fast,拒绝启动)
        """
        if not self._db:
            raise RuntimeError(
                f"无法验证 {table_name} UNIQUE 约束: 数据库连接未初始化"
            )
        try:
            cursor = await self._db.execute(
                f"PRAGMA index_list('{table_name}')"
            )
            indexes = await cursor.fetchall()
        except Exception as e:
            raise RuntimeError(
                f"{table_name} UNIQUE 约束验证失败(PRAGMA index_list 异常),拒绝启动: {e}"
            ) from e
        # PRAGMA index_list 返回: (seq, name, unique, origin, partial)
        _found = False
        _is_unique = False
        for _idx in indexes:
            if len(_idx) >= 3 and _idx[1] == index_name:
                _found = True
                _is_unique = bool(_idx[2])
                break
        if not _found:
            raise RuntimeError(
                f"dirty_outbox UNIQUE index 创建失败,拒绝启动: "
                f"索引 {index_name} 不存在于 {table_name} 表(PRAGMA 验证未发现)"
            )
        if not _is_unique:
            raise RuntimeError(
                f"dirty_outbox UNIQUE index 创建失败,拒绝启动: "
                f"索引 {index_name} 存在但非 UNIQUE(PRAGMA 验证 unique=0)"
            )

    # ─── R49 P0-5: dirty_outbox UNIQUE 约束完整 7 步迁移方案 ──────────────

    async def _migrate_dirty_outbox_unique_constraint(self) -> None:
        """R49 P0-5: dirty_outbox UNIQUE 约束完整 7 步迁移方案。

        完整 7 步流程(每步含详细日志,任一步失败 raise RuntimeError 拒绝启动):
          1. preflight 查询重复行(SELECT GROUP BY HAVING COUNT > 1)
          2. 选定权威行(每组按 created_at DESC, id DESC,第一行为权威)
          3. 归档冲突行到 migration_conflicts(conflict_type='dirty_outbox_duplicate')
          4. 删除冲突行(从 dirty_outbox 表)
          5. 创建 UNIQUE INDEX IF NOT EXISTS
          6. PRAGMA index_list + index_info 验证索引存在且 unique=1
          7. fail-closed(已通过上述 raise RuntimeError 实现)

        权威行选择说明:
          R49 任务描述原文为 "created_at ASC, id ASC",但 R48 已有的集成测试
          (test_init_archives_old_duplicates_and_succeeds) 断言保留 created_at 最新者。
          为满足 "R48 测试不破" 约束,本实现沿用 R48 的 DESC 语义
          (最新 created_at + 最大 id 为权威)。

        幂等性:
          - 步骤 1 无重复行时,步骤 2-4 跳过(无副作用)
          - 步骤 5 使用 CREATE UNIQUE INDEX IF NOT EXISTS(已存在则跳过)
          - 步骤 6 PRAGMA 只读验证(无副作用)
          - 重复运行 migration 应全部成功,无新副作用

        Raises:
            RuntimeError: 任一步失败(数据库连接未初始化、查询异常、
                          CREATE UNIQUE INDEX 异常、PRAGMA 验证索引缺失或非 UNIQUE)
        """
        _table = "dirty_outbox"
        _index_name = "idx_dirty_outbox_table_pk_version"
        _cols = ["table_name", "pk", "version"]
        _cols_csv = ", ".join(_cols)

        # ─── 步骤 0: 前置检查 ───
        if not self._db:
            logger.error("[R49 P0-5] 步骤 0 失败: 数据库连接未初始化")
            raise RuntimeError(
                "dirty_outbox UNIQUE 迁移失败: 数据库连接未初始化"
            )
        logger.info("[R49 P0-5] 开始 dirty_outbox UNIQUE 约束 7 步迁移")

        # ─── 步骤 1: preflight 查询重复行 ───
        logger.info("[R49 P0-5] 步骤 1/7: preflight 查询 dirty_outbox 重复行组")
        try:
            cursor = await self._db.execute(
                f"SELECT {_cols_csv}, COUNT(*) AS cnt "
                f"FROM {_table} "
                f"GROUP BY {_cols_csv} "
                f"HAVING COUNT(*) > 1"
            )
            dup_groups = await cursor.fetchall()
        except Exception as e:
            logger.error(f"[R49 P0-5] 步骤 1 失败: preflight 查询异常: {e}")
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 1 失败(preflight 查询): {e}"
            ) from e

        _total_conflicts = sum(max(0, int(g[-1]) - 1) for g in dup_groups)
        logger.info(
            f"[R49 P0-5] 步骤 1 完成: 发现 {len(dup_groups)} 个重复组, "
            f"预计归档 {_total_conflicts} 条冲突行"
        )

        # ─── 步骤 2-4: 选定权威行 + 归档冲突 + 删除冲突 ───
        if dup_groups:
            import json as _json
            _now = datetime.datetime.now().isoformat()
            # 获取列名(用于 JSON 序列化完整 payload)
            cur_cols = await self._db.execute(f"PRAGMA table_info({_table})")
            col_names = [c[1] for c in await cur_cols.fetchall()]
            _n_cols = len(col_names)

            archived = 0
            for _row in dup_groups:
                _where = " AND ".join([f"{c} = ?" for c in _cols])
                _params = list(_row[:len(_cols)])

                # 步骤 2: 拉取该组所有行,按 created_at DESC, id DESC 排序
                # (第一行为权威:最新 created_at 或最大 id;与 R48 语义一致)
                logger.debug(
                    f"[R49 P0-5] 步骤 2: 处理重复组 {_cols}={_params}"
                )
                cur_rows = await self._db.execute(
                    f"SELECT * FROM {_table} WHERE {_where} "
                    f"ORDER BY created_at DESC, id DESC",
                    _params,
                )
                rows_all = await cur_rows.fetchall()
                if len(rows_all) < 2:
                    continue

                # 第一行为权威,其余为冲突
                for r in rows_all[1:]:
                    _rid = int(r[0])
                    _record_data = _json.dumps(
                        {col_names[i]: r[i] for i in range(_n_cols)},
                        ensure_ascii=False, default=str,
                    )
                    # 步骤 3: 归档冲突行到 migration_conflicts
                    # (conflict_type='dirty_outbox_duplicate',含完整 payload JSON)
                    logger.debug(
                        f"[R49 P0-5] 步骤 3: 归档冲突行 id={_rid} 到 migration_conflicts"
                    )
                    await self._db.execute(
                        "INSERT INTO migration_conflicts "
                        "(table_name, conflict_type, record_id, record_data, resolved_at, created_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?)",
                        (_table, "dirty_outbox_duplicate", _rid, _record_data, _now),
                    )
                    # 步骤 4: 删除冲突行(只保留权威行)
                    logger.debug(
                        f"[R49 P0-5] 步骤 4: 删除 dirty_outbox 冲突行 id={_rid}"
                    )
                    await self._db.execute(
                        f"DELETE FROM {_table} WHERE id = ?",
                        (_rid,),
                    )
                    archived += 1

            await self._db.commit()
            logger.info(
                f"[R49 P0-5] 步骤 2-4 完成: 归档并删除 {archived} 条冲突行 "
                f"(权威行保留:最新 created_at 或最大 id)"
            )
        else:
            logger.info("[R49 P0-5] 步骤 2-4 跳过: 无重复行,无需归档")

        # ─── 步骤 5: 创建 UNIQUE INDEX ───
        logger.info(f"[R49 P0-5] 步骤 5/7: 创建 UNIQUE INDEX {_index_name}")
        try:
            await self._db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_index_name} "
                f"ON {_table}({_cols_csv})"
            )
            await self._db.commit()
        except Exception as e:
            # 清理重复后仍失败,说明存在其他约束冲突原因(如非 UNIQUE 同名索引),
            # 必须 fail-fast 拒绝启动
            logger.error(
                f"[R49 P0-5] 步骤 5 失败: CREATE UNIQUE INDEX 异常: {e}"
            )
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 5 失败"
                f"(CREATE UNIQUE INDEX 创建失败): {e}"
            ) from e
        logger.info(
            f"[R49 P0-5] 步骤 5 完成: UNIQUE INDEX 已创建或已存在(IF NOT EXISTS 幂等)"
        )

        # ─── 步骤 6: PRAGMA 验证 ───
        # CREATE UNIQUE INDEX IF NOT EXISTS 在同名索引已存在时会静默跳过
        # (即使该索引非 UNIQUE),因此必须通过 PRAGMA index_list 二次验证 unique 标志位。
        # PRAGMA index_list 返回: (seq, name, unique, origin, partial)
        #   - unique=1 表示索引是 UNIQUE 索引
        #   - origin: 'c'=CREATE INDEX 创建, 'u'=UNIQUE 约束, 'pk'=主键
        #   (CREATE UNIQUE INDEX 产生 origin='c' + unique=1;
        #    任务描述 "origin='u'(unique)" 实指 unique 标志位,此处验证 unique=1)
        logger.info(
            f"[R49 P0-5] 步骤 6/7: PRAGMA 验证 {_index_name} 存在且 unique=1"
        )
        try:
            cursor = await self._db.execute(
                f"PRAGMA index_list('{_table}')"
            )
            indexes = await cursor.fetchall()
        except Exception as e:
            logger.error(
                f"[R49 P0-5] 步骤 6 失败: PRAGMA index_list 异常: {e}"
            )
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 6 失败(PRAGMA index_list): {e}"
            ) from e

        _found = False
        _is_unique = False
        for _idx in indexes:
            # _idx: (seq, name, unique, origin, partial)
            if len(_idx) >= 3 and _idx[1] == _index_name:
                _found = True
                _is_unique = bool(_idx[2])
                _origin = _idx[3] if len(_idx) >= 4 else "?"
                logger.debug(
                    f"[R49 P0-5] 步骤 6: 找到索引 {_index_name} "
                    f"unique={_idx[2]} origin={_origin}"
                )
                break

        if not _found:
            logger.error(
                f"[R49 P0-5] 步骤 6 失败: 索引 {_index_name} 不存在于 {_table} 表"
            )
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 6 失败: "
                f"索引 {_index_name} 不存在于 {_table} 表(PRAGMA 验证未发现)"
            )
        if not _is_unique:
            logger.error(
                f"[R49 P0-5] 步骤 6 失败: 索引 {_index_name} 存在但非 UNIQUE"
            )
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 6 失败: "
                f"索引 {_index_name} 存在但非 UNIQUE(PRAGMA 验证 unique=0)"
            )

        # 额外验证 index_info:索引列应匹配 [table_name, pk, version]
        try:
            cur_info = await self._db.execute(
                f"PRAGMA index_info('{_index_name}')"
            )
            idx_cols = await cur_info.fetchall()
            _actual_cols = [c[2] for c in idx_cols]  # (seqno, cid, name)
            logger.debug(
                f"[R49 P0-5] 步骤 6: index_info 列 = {_actual_cols}"
            )
            if _actual_cols != _cols:
                logger.error(
                    f"[R49 P0-5] 步骤 6 失败: 索引列不匹配, "
                    f"期望 {_cols}, 实际 {_actual_cols}"
                )
                raise RuntimeError(
                    f"dirty_outbox UNIQUE 迁移步骤 6 失败: "
                    f"索引 {_index_name} 列不匹配"
                    f"(期望 {_cols}, 实际 {_actual_cols})"
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(
                f"[R49 P0-5] 步骤 6 失败: PRAGMA index_info 异常: {e}"
            )
            raise RuntimeError(
                f"dirty_outbox UNIQUE 迁移步骤 6 失败(PRAGMA index_info): {e}"
            ) from e

        logger.info(
            f"[R49 P0-5] 步骤 6 完成: 索引 {_index_name} 验证通过"
            f"(存在 + unique=1 + 列匹配 {_cols})"
        )

        # ─── 步骤 7: fail-closed ───
        # 已通过上述各步 raise RuntimeError 实现:
        # 任一步失败 → raise → init() 失败 → Web/Writer/DB 全部拒绝启动
        logger.info(
            "[R49 P0-5] 步骤 7/7: fail-closed 检查通过 — 所有步骤成功,migration 完成"
        )

    # ─── R47 P0-6: dirty_outbox 唯一冲突重试辅助 ──────────────

    async def _add_dirty_outbox_with_retry(
        self,
        conn: Any,
        table_name: str,
        pk: str,
        version: int,
        operation: str,
        payload: str | None,
        processed_init: int,
        local_only_init: int,
        max_retries: int = 5,
    ) -> int:
        """R47 P0-6: 在指定事务连接上 INSERT dirty_outbox,UNIQUE 冲突时重试。

        UNIQUE(table_name, pk, version) 冲突时:
            - 重新调用 allocate_version 获取新 version
            - 最多重试 max_retries 次
            - 不回退时间戳(version 由 allocate_version 单调递增保证)

        Args:
            conn: 事务连接(由调用方控制 commit/rollback)
            table_name / pk / operation / payload: dirty_outbox 字段
            version: 初始 version(若冲突会重新分配)
            processed_init / local_only_init: processed / local_only 初始值
            max_retries: UNIQUE 冲突最大重试次数

        Returns:
            新插入行 id;失败抛异常让上层事务回滚
        """
        _current_version = version
        _last_err: Exception | None = None
        for _attempt in range(max_retries + 1):
            try:
                cursor = await conn.execute(
                    """INSERT INTO dirty_outbox
                       (table_name, pk, version, operation, payload, created_at, processed, local_only)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        table_name, pk, _current_version, operation, payload,
                        datetime.datetime.now().isoformat(),  # type: ignore[attr-defined]
                        processed_init, local_only_init,
                    ),
                )
                return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            except Exception as _e_insert:
                _last_err = _e_insert
                # 检测 UNIQUE 冲突(SQLite ConstraintException 文本含 UNIQUE)
                _err_msg = str(_e_insert).lower()
                if "unique" in _err_msg and _attempt < max_retries:
                    # UNIQUE 冲突,重新分配 version 重试
                    _current_version = await self.allocate_version(
                        table_name, pk, connection=conn
                    )
                    logger.debug(
                        f"[CacheStore] dirty_outbox UNIQUE 冲突,重试 "
                        f"attempt={_attempt + 1} new_version={_current_version}"
                    )
                    continue
                # 非 UNIQUE 冲突或重试次数用尽,抛出异常让上层回滚
                raise
        # 理论上不会到达(循环内必 return 或 raise)
        raise _last_err if _last_err else RuntimeError(
            "[CacheStore] _add_dirty_outbox_with_retry: 重试次数用尽"
        )

    # ─── R47 P0-5: delivery_group_receipts 群发回执聚合方法 ──────────────

    async def delivery_group_receipt_create(
        self,
        group_id: str,
        expected_count: int,
        source_ids: list,
        target_ids: list,
        action_id: str,
    ) -> bool:
        """R47 P0-5: 创建群发回执记录(若 group_id 已存在则忽略,幂等)。

        Args:
            group_id: 群发任务唯一 ID
            expected_count: 预期子任务总数
            source_ids: 源消息 ID 列表(JSON 序列化存储)
            target_ids: 目标用户 ID 列表(JSON 序列化存储)
            action_id: 关联动作 ID(如审批 action_id)

        Returns:
            True=创建成功(或已存在);False=创建失败
        """
        if not self._db:
            return False
        import datetime as _dt
        _now = _dt.datetime.now().isoformat()
        _source_json = _m1_json_dumps(source_ids) or "[]"
        _target_json = _m1_json_dumps(target_ids) or "[]"
        try:
            await self._db.execute(
                """INSERT OR IGNORE INTO delivery_group_receipts
                   (group_id, expected_count, confirmed_count, status,
                    source_ids, target_ids, action_id, created_at, updated_at)
                   VALUES (?, ?, 0, 'pending', ?, ?, ?, ?, ?)""",
                (group_id, expected_count, _source_json, _target_json,
                 action_id, _now, _now),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[CacheStore] delivery_group_receipt_create 失败: {e}")
            return False

    async def delivery_group_receipt_confirm_child(
        self, group_id: str, child_msg_id: Any
    ) -> int | None:
        """R47 P0-5: 确认群发子任务完成,递增 confirmed_count 并返回新值。

        状态机迁移:
            - confirmed_count < expected_count → status='partial'
            - confirmed_count >= expected_count → status='completed'

        Args:
            group_id: 群发任务唯一 ID
            child_msg_id: 子任务消息 ID(保留参数,目前仅用于日志;

        Returns:
            新的 confirmed_count;group_id 不存在时返回 None
        """
        if not self._db:
            return None
        import datetime as _dt
        _now = _dt.datetime.now().isoformat()
        try:
            # 原子递增 confirmed_count 并读取新值 + expected_count
            cursor = await self._db.execute(
                """UPDATE delivery_group_receipts
                   SET confirmed_count = confirmed_count + 1,
                       updated_at = ?
                   WHERE group_id = ?""",
                (_now, group_id),
            )
            if cursor.rowcount == 0:
                return None
            # 查询新 confirmed_count + expected_count 决定状态迁移
            cursor = await self._db.execute(
                "SELECT confirmed_count, expected_count FROM delivery_group_receipts "
                "WHERE group_id = ?",
                (group_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            _confirmed, _expected = int(row[0]), int(row[1])
            _new_status = "completed" if _confirmed >= _expected else "partial"
            await self._db.execute(
                "UPDATE delivery_group_receipts SET status = ?, updated_at = ? "
                "WHERE group_id = ?",
                (_new_status, _now, group_id),
            )
            await self._db.commit()
            return _confirmed
        except Exception as e:
            logger.error(f"[CacheStore] delivery_group_receipt_confirm_child 失败: {e}")
            return None

    async def delivery_group_receipt_get(self, group_id: str) -> dict | None:
        """R47 P0-5: 查询群发回执详情。

        Returns:
            回执 dict(含 source_ids/target_ids 反序列化为 list);不存在返回 None
        """
        if not self._db:
            return None
        try:
            cursor = await self._db.execute(
                "SELECT group_id, expected_count, confirmed_count, status, "
                "source_ids, target_ids, action_id, created_at, updated_at "
                "FROM delivery_group_receipts WHERE group_id = ?",
                (group_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "group_id": row[0],
                "expected_count": int(row[1]),
                "confirmed_count": int(row[2]),
                "status": row[3],
                "source_ids": _m1_json_loads(row[4]) or [],
                "target_ids": _m1_json_loads(row[5]) or [],
                "action_id": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
        except Exception as e:
            logger.error(f"[CacheStore] delivery_group_receipt_get 失败: {e}")
            return None

    async def delivery_group_receipt_list_pending(self, limit: int = 100) -> list[dict]:
        """R47 P0-5: 列出未完成的群发回执(status=pending 或 partial)。

        供后台扫描器周期性检查超时/失败任务。

        Returns:
            回执 dict 列表(按 created_at 升序)
        """
        if not self._db:
            return []
        try:
            cursor = await self._db.execute(
                "SELECT group_id, expected_count, confirmed_count, status, "
                "source_ids, target_ids, action_id, created_at, updated_at "
                "FROM delivery_group_receipts "
                "WHERE status IN ('pending', 'partial') "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "group_id": r[0],
                    "expected_count": int(r[1]),
                    "confirmed_count": int(r[2]),
                    "status": r[3],
                    "source_ids": _m1_json_loads(r[4]) or [],
                    "target_ids": _m1_json_loads(r[5]) or [],
                    "action_id": r[6],
                    "created_at": r[7],
                    "updated_at": r[8],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[CacheStore] delivery_group_receipt_list_pending 失败: {e}")
            return []

    # ─── R47 P1-a: callback_nonces 回调 nonce 原子消费方法 ──────────────

    async def callback_nonce_create(
        self,
        nonce: str,
        principal_id: int,
        action: str,
        expires_at: str,
    ) -> bool:
        """R47 P1-a: 创建回调 nonce(若已存在则忽略,幂等)。

        Args:
            nonce: 唯一 nonce 字符串(由调用方生成,如 UUID4)
            principal_id: 关联主体 ID(管理员 principal_id)
            action: 关联动作(如 'approval_callback')
            expires_at: 过期时间(ISO 字符串)

        Returns:
            True=创建成功(或已存在);False=创建失败
        """
        if not self._db:
            return False
        import datetime as _dt
        _now = _dt.datetime.now().isoformat()
        try:
            await self._db.execute(
                """INSERT OR IGNORE INTO callback_nonces
                   (nonce, principal_id, action, expires_at, consumed_at, created_at)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                (nonce, principal_id, action, expires_at, _now),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[CacheStore] callback_nonce_create 失败: {e}")
            return False

    async def callback_nonce_consume(self, nonce: str) -> bool:
        """R47 P1-a: 原子消费 nonce(UPDATE WHERE consumed_at IS NULL RETURNING)。

        防止回调 URL 重放:同一 nonce 只能被消费一次。

        Args:
            nonce: 唯一 nonce 字符串

        Returns:
            True=消费成功(首次调用);False=已消费/不存在/已过期(调用方应拒绝回调)
        """
        if not self._db:
            return False
        import datetime as _dt
        _now = _dt.datetime.now().isoformat()
        try:
            # 优先尝试 RETURNING 子句(SQLite 3.35+)
            try:
                cursor = await self._db.execute(
                    "UPDATE callback_nonces SET consumed_at = ? "
                    "WHERE nonce = ? AND consumed_at IS NULL "
                    "RETURNING nonce",
                    (_now, nonce),
                )
                row = await cursor.fetchone()
                await self._db.commit()
                return row is not None
            except Exception:
                # RETURNING 不可用,fallback 到 rowcount 检查
                pass
            cursor = await self._db.execute(
                "UPDATE callback_nonces SET consumed_at = ? "
                "WHERE nonce = ? AND consumed_at IS NULL",
                (_now, nonce),
            )
            _affected = cursor.rowcount or 0
            await self._db.commit()
            return _affected > 0
        except Exception as e:
            logger.error(f"[CacheStore] callback_nonce_consume 失败: {e}")
            return False

    async def callback_nonce_exists(self, nonce: str) -> bool:
        """R47 P1-a: 检查 nonce 是否存在(无论是否已消费)。

        Args:
            nonce: 唯一 nonce 字符串

        Returns:
            True=存在;False=不存在
        """
        if not self._db:
            return False
        try:
            cursor = await self._db.execute(
                "SELECT 1 FROM callback_nonces WHERE nonce = ? LIMIT 1",
                (nonce,),
            )
            row = await cursor.fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"[CacheStore] callback_nonce_exists 失败: {e}")
            return False

    async def callback_nonce_cleanup(
        self,
        expired_before: str | None = None,
        consumed_before: str | None = None,
    ) -> dict:
        """R48 P1-b: 清理 callback_nonces 表中的过期/已消费记录。

        清理策略:
        1. 删除 expires_at < expired_before 的记录(已过期但未消费)
        2. 删除 consumed_at < consumed_before 的记录(已消费超过保留期)

        典型用法:
        - expired_before = now (删除所有过期未消费的 nonce)
        - consumed_before = now - 24h (删除 24h 前已消费的 nonce)

        Args:
            expired_before: ISO 时间字符串,删除 expires_at < 此值的记录。
                None 时不清理过期记录。
            consumed_before: ISO 时间字符串,删除 consumed_at < 此值(且非 NULL)的记录。
                None 时不清理已消费记录。

        Returns:
            {"deleted_expired": int, "deleted_consumed": int}
        """
        if not self._db:
            return {"deleted_expired": 0, "deleted_consumed": 0}
        deleted_expired = 0
        deleted_consumed = 0
        try:
            if expired_before:
                cursor = await self._db.execute(
                    "DELETE FROM callback_nonces "
                    "WHERE expires_at < ? AND consumed_at IS NULL",
                    (expired_before,),
                )
                deleted_expired = cursor.rowcount or 0
                await self._db.commit()
            if consumed_before:
                cursor = await self._db.execute(
                    "DELETE FROM callback_nonces "
                    "WHERE consumed_at IS NOT NULL AND consumed_at < ?",
                    (consumed_before,),
                )
                deleted_consumed = cursor.rowcount or 0
                await self._db.commit()
            if deleted_expired > 0 or deleted_consumed > 0:
                logger.info(
                    f"[CacheStore] callback_nonce_cleanup: "
                    f"expired={deleted_expired}, consumed={deleted_consumed}"
                )
        except Exception as e:
            logger.error(f"[CacheStore] callback_nonce_cleanup 失败: {e}")
        return {"deleted_expired": deleted_expired, "deleted_consumed": deleted_consumed}


# ─── Decode Logs 缓冲表 ──────────────────────────────────────

DDL_BUFFER_TABLES = [
    """CREATE TABLE IF NOT EXISTS decode_log_buffer (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_code TEXT,
        requester_id BIGINT,
        request_time TEXT,
        status TEXT DEFAULT 'queued',
        source_channel_id BIGINT,
        buffered_at REAL
    )""",
    # ─── 文件码变更缓冲表：用户管理操作先写 SQLite，后台批量 flush CRDB ───
    """CREATE TABLE IF NOT EXISTS code_changes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code       TEXT NOT NULL,
        change_type TEXT NOT NULL,  -- 'note' | 'expiry' | 'status'
        new_value  TEXT,
        uploader_id BIGINT NOT NULL,
        created_at REAL NOT NULL,
        synced     INTEGER DEFAULT 0
    )""",
]



class DecodeLogBuffer:
    """Decode Logs 本地缓冲，定时批量 flush 到 CRDB

    注意：缓冲表 DDL 由 CacheStore.init() 统一创建，此处无需重复。
    """

    async def cleanup_old(self, days: int = 7) -> int:
        """清理 N 天前的本地缓冲记录（0 RU，纯本地 SQLite）
        Returns: 删除的行数
        """
        from loguru import logger
        import time as _t
        cutoff = _t.time() - days * 86400
        try:
            cursor = await self._db.execute(
                "DELETE FROM decode_log_buffer WHERE buffered_at < ?", (cutoff,)
            )
            await self._db.commit()
            deleted = cursor.rowcount or 0
            if deleted > 0:
                logger.info(f"[DecodeLog] 本地缓冲清理 {deleted} 条 {days} 天前记录")
            return deleted
        except Exception as e:
            logger.warning(f"[DecodeLog] 本地缓冲清理失败: {e}")
            return 0

    def __init__(self):
        self._db = None

    def set_db(self, db):
        """由 CacheStore 注入数据库连接。"""
        self._db = db

    async def insert(self, entry: dict):
        """写入本地缓冲表（零 RU）。SQLite WAL 模式下并发写可能 'database is locked'，重试即可。"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO decode_log_buffer "
                    "(file_code, requester_id, request_time, status, source_channel_id, buffered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entry["file_code"],
                        entry["requester_id"],
                        entry.get("request_time"),
                        entry.get("status", "queued"),
                        entry.get("source_channel_id"),
                        time.time(),
                    ),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                logger.debug(f"[DecodeLogBuffer] insert 失败(非致命): {e}")
                return

    async def close(self):
        pass


class CodeChangeBuffer:
    """文件码变更缓冲，定时批量 flush 到 CRDB"""

    def __init__(self):
        self._db = None

    def set_db(self, db):
        self._db = db

    async def insert(self, code: str, change_type: str, new_value: str, uploader_id: int):
        """写入变更缓冲（零 CRDB RU）。SQLite WAL 模式下并发写可能 'database is locked'，重试即可。"""
        if not self._db:
            return
        for attempt in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO code_changes (code, change_type, new_value, uploader_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, change_type, new_value, uploader_id, time.time()),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                logger.debug(f"[CodeChangeBuffer] insert 失败(非致命): {e}")
                return

    async def get_unsynced(self, limit: int = 100) -> list[dict]:
        """获取未同步的变更记录"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            "SELECT id, code, change_type, new_value, uploader_id FROM code_changes "
            "WHERE synced = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return [
            {"id": r[0], "code": r[1], "change_type": r[2], "new_value": r[3], "uploader_id": r[4]}
            for r in rows
        ]

    async def mark_synced(self, change_ids: list[int]):
        """标记已同步"""
        if not self._db or not change_ids:
            return
        placeholders = ",".join("?" * len(change_ids))
        await self._db.execute(
            f"UPDATE code_changes SET synced = 1 WHERE id IN ({placeholders})",
            change_ids,
        )
        await self._db.commit()

    async def close(self):
        pass


def _json_safe(obj):
    """将对象转换为 JSON 可序列化形式(datetime → isoformat,递归处理 dict/list)。

    用于推入 Redis Queue 前的数据预处理,确保 redis_queue.push 内部的
    json.dumps 不报错。转换后的数据传给 db_writer 端 CacheStore 方法时,
    内部 _serialize 仍能正确处理(字符串原样返回,datetime 已转 isoformat)。
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    return obj


def _dumps_str(obj) -> str:
    """JSON 序列化为 str,兼容 orjson(bytes) 和标准 json(str)。"""
    raw = json.dumps(obj, default=str)
    return raw.decode() if isinstance(raw, bytes) else raw


def _loads_cached(cached: str):
    """安全反序列化 Redis 缓存(P1修复: 单条缓存损坏不抛异常,回退到 SQLite 重读)。

    Returns:
        反序列化后的对象;解析失败返回 None(调用方会回退到 SQLite 重读并回填)
    """
    try:
        return json.loads(cached)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f"[CacheStoreRouter] 缓存反序列化失败,回退 SQLite: {e}")
        return None


class CacheStoreRouter(CacheStore):
    """带 Redis 路由的 CacheStore(bot 进程使用)。

    写操作:推入 Redis Queue,由 db_writer 进程串行落盘 SQLite
    读操作:优先 Redis 缓存,未命中回退 SQLite + 回填
    CAS/事务写:直写 SQLite(不走 Redis,保证强一致),写后失效读缓存

    零调用方改动:所有改造在本子类内完成,19 个引用 cache_store 的文件无需修改。
    """

    # ── 重写高频写方法(路由到 Redis Queue)──

    async def write_heartbeat(self, slot_id: str, ok: bool, _batch: bool = False):
        """心跳写入 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="write_heartbeat",
            table="heartbeat_local",
            op_type="upsert",
            data={"slot_id": slot_id, "ok": ok, "_batch": False},  # P0修复: db_writer 端总是立即 commit
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).write_heartbeat(slot_id, ok, _batch=False),  # P1修复: 与 Redis 路径保持一致
        )

    async def write_bot_heartbeat(self, name: str, total_processed: int = 0, total_errors: int = 0):
        """Bot 心跳 — 路由到 Redis Queue,写后失效心跳缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="write_bot_heartbeat",
            table="bot_heartbeat",
            op_type="upsert",
            data={"name": name, "total_processed": total_processed, "total_errors": total_errors},
            redis_key="cache:all_bot_heartbeats",
            fallback=lambda: super(CacheStoreRouter, self).write_bot_heartbeat(name, total_processed, total_errors),
        )

    async def upsert_user_quota(self, user_id: int, data: dict):
        """用户配额写入 — 路由到 Redis Queue,写后失效配额缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="upsert_user_quota",
            table="user_quota",
            op_type="upsert",
            data={"user_id": user_id, "data": _json_safe(data)},
            redis_key=f"cache:user_quota:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).upsert_user_quota(user_id, data),
        )

    async def increment_user_quota_used(self, user_id: int, is_external: bool = False):
        """原子递增配额 — 路由到 Redis Queue,写后失效配额缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="increment_user_quota_used",
            table="user_quota",
            op_type="update",
            data={"user_id": user_id, "is_external": is_external},
            redis_key=f"cache:user_quota:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).increment_user_quota_used(user_id, is_external),
        )

    async def refund_quota(self, user_id: int, is_external: bool = False):
        """配额回滚 — 路由到 Redis Queue,写后失效配额缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="refund_quota",
            table="user_quota",
            op_type="update",
            data={"user_id": user_id, "is_external": is_external},
            redis_key=f"cache:user_quota:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).refund_quota(user_id, is_external),
        )

    async def upsert_file_record_local(self, record: dict, mark_dirty: bool = True, _batch: bool = False):
        """文件记录写入 — 路由到 Redis Queue,写后失效文件记录缓存"""
        from database import write_router
        safe_record = _json_safe(record)
        file_code = record.get("file_code", "") if isinstance(record, dict) else ""
        return await write_router.route_write(
            method_name="upsert_file_record_local",
            table="file_records_local",
            op_type="upsert",
            data={"record": safe_record, "mark_dirty": mark_dirty, "_batch": False},  # P0修复: db_writer 端总是立即 commit
            redis_key=f"cache:file_record:{file_code}" if file_code else "",
            fallback=lambda: super(CacheStoreRouter, self).upsert_file_record_local(record, mark_dirty, _batch=False),  # P1修复: 与 Redis 路径保持一致
        )

    async def upsert_code_local(self, record: dict, mark_dirty: bool = True, _batch: bool = False):
        """验证码写入 — 路由到 Redis Queue,写后失效验证码缓存"""
        from database import write_router
        safe_record = _json_safe(record)
        code = record.get("code", "") if isinstance(record, dict) else ""
        return await write_router.route_write(
            method_name="upsert_code_local",
            table="codes_local",
            op_type="upsert",
            data={"record": safe_record, "mark_dirty": mark_dirty, "_batch": False},  # P0修复: db_writer 端总是立即 commit
            redis_key=f"cache:code:{code}" if code else "",
            fallback=lambda: super(CacheStoreRouter, self).upsert_code_local(record, mark_dirty, _batch=False),  # P1修复: 与 Redis 路径保持一致
        )

    async def upsert_user_local(self, user: dict, mark_dirty: bool = True, _batch: bool = False):
        """用户记录写入 — 路由到 Redis Queue,写后失效用户缓存"""
        from database import write_router
        safe_user = _json_safe(user)
        user_id = user.get("user_id", 0) if isinstance(user, dict) else 0
        return await write_router.route_write(
            method_name="upsert_user_local",
            table="users_local",
            op_type="upsert",
            data={"user": safe_user, "mark_dirty": mark_dirty, "_batch": False},  # P0修复: db_writer 端总是立即 commit
            redis_key=f"cache:user:{user_id}" if user_id else "",
            fallback=lambda: super(CacheStoreRouter, self).upsert_user_local(user, mark_dirty, _batch=False),  # P1修复: 与 Redis 路径保持一致
        )

    async def update_cell_fields_local(self, slot_id: str, fields: dict, mark_dirty: bool = False):
        """更新 cell 字段 — 路由到 Redis Queue,写后失效 cells 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="update_cell_fields_local",
            table="cells_local",
            op_type="update",
            data={"slot_id": slot_id, "fields": _json_safe(fields), "mark_dirty": mark_dirty},
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).update_cell_fields_local(slot_id, fields, mark_dirty),
        )

    async def increment_cell_file_count_local(self, slot_id: str, delta: int = 1):
        """递增 cell 文件计数 — 路由到 Redis Queue,写后失效 cells 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="increment_cell_file_count_local",
            table="cells_local",
            op_type="update",
            data={"slot_id": slot_id, "delta": delta},
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).increment_cell_file_count_local(slot_id, delta),
        )

    async def set_kv(self, key: str, value: str):
        """KV 写入 — 路由到 Redis Queue,写后失效 KV 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="set_kv",
            table="kv_store",
            op_type="upsert",
            data={"key": key, "value": value},
            redis_key=f"cache:kv:{key}",
            fallback=lambda: super(CacheStoreRouter, self).set_kv(key, value),
        )

    async def cache_set(self, key: str, value):
        """TTL 缓存写入 — 路由到 Redis Queue
        P3修复: table 标签从 cache_backup 修正为 ttl_cache(实际写入的表名)。
        """
        from database import write_router
        return await write_router.route_write(
            method_name="cache_set",
            table="ttl_cache",
            op_type="upsert",
            data={"key": key, "value": _json_safe(value)},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).cache_set(key, value),
        )

    async def notify_new_upload(self):
        """上传通知 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="notify_new_upload",
            table="pending_notify",
            op_type="insert",
            data={},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).notify_new_upload(),
        )

    async def notify_dsp_new_job(self):
        """DSP 通知 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="notify_dsp_new_job",
            table="dsp_notify",
            op_type="insert",
            data={},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).notify_dsp_new_job(),
        )

    async def notify_relay_change(self):
        """relay 变更通知 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="notify_relay_change",
            table="relay_notify",
            op_type="insert",
            data={},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).notify_relay_change(),
        )

    async def notify_record_change(self, change_type: str, record_key: str):
        """记录变更通知 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="notify_record_change",
            table="record_notify",
            op_type="insert",
            data={"change_type": change_type, "record_key": record_key},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).notify_record_change(change_type, record_key),
        )

    async def save_counter_snapshot(self, counters: dict[str, int], role: str = None):
        """启动统计快照 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="save_counter_snapshot",
            table="counter_snapshot",
            op_type="upsert",
            data={"counters": counters, "role": role},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).save_counter_snapshot(counters, role),
        )

    async def mark_user_started(self, user_id: int, bot_name: str):
        """标记用户已启动 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_user_started",
            table="user_bot_started",
            op_type="upsert",
            data={"user_id": user_id, "bot_name": bot_name},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).mark_user_started(user_id, bot_name),
        )

    async def add_pending_file_code(self, user_id: int, file_code: str, note: str = "", ext_code: str = ""):
        """暂存文件码 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="add_pending_file_code",
            table="pending_file_codes",
            op_type="insert",
            data={"user_id": user_id, "file_code": file_code, "note": note, "ext_code": ext_code},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).add_pending_file_code(user_id, file_code, note, ext_code),
        )

    async def delete_pending_file_code(self, row_id: int):
        """删除暂存文件码 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="delete_pending_file_code",
            table="pending_file_codes",
            op_type="delete",
            data={"row_id": row_id},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).delete_pending_file_code(row_id),
        )

    async def update_local_job_status(self, crdb_id: int, status: str, retry_count: int = None, dead_reason: str = None):
        """更新 job 状态 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="update_local_job_status",
            table="local_job_queue",
            op_type="update",
            data={"crdb_id": crdb_id, "status": status, "retry_count": retry_count, "dead_reason": dead_reason},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).update_local_job_status(crdb_id, status, retry_count, dead_reason),
        )

    async def retry_local_job(self, crdb_id: int, new_retry_count: int):
        """重试 job — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="retry_local_job",
            table="local_job_queue",
            op_type="update",
            data={"crdb_id": crdb_id, "new_retry_count": new_retry_count},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).retry_local_job(crdb_id, new_retry_count),
        )

    async def retry_local_dead_job(self, crdb_id: int):
        """重试 dead job — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="retry_local_dead_job",
            table="local_job_queue",
            op_type="update",
            data={"crdb_id": crdb_id},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).retry_local_dead_job(crdb_id),
        )

    async def mark_local_job_synced(self, crdb_id: int):
        """标记 job 已同步 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_local_job_synced",
            table="local_job_queue",
            op_type="update",
            data={"crdb_id": crdb_id},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).mark_local_job_synced(crdb_id),
        )

    async def mark_quota_synced(self, user_id: int):
        """标记配额已同步 — 路由到 Redis Queue,写后失效配额缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_quota_synced",
            table="user_quota",
            op_type="update",
            data={"user_id": user_id},
            redis_key=f"cache:user_quota:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).mark_quota_synced(user_id),
        )

    async def invalidate_user_quota(self, user_id: int):
        """失效配额 — 路由到 Redis Queue,写后失效配额缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="invalidate_user_quota",
            table="user_quota",
            op_type="update",
            data={"user_id": user_id},
            redis_key=f"cache:user_quota:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).invalidate_user_quota(user_id),
        )

    async def mark_cell_synced_local(self, slot_id: str):
        """标记 cell 已同步 — 路由到 Redis Queue,写后失效 cells 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_cell_synced_local",
            table="cells_local",
            op_type="update",
            data={"slot_id": slot_id},
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).mark_cell_synced_local(slot_id),
        )

    async def cas_transition_cell(
        self,
        slot_id: str,
        expected_status: str,
        new_status: str,
        lease_owner: str,
        transition_id: str,
        lease_seconds: int = 60,
        **update_fields,
    ) -> bool:
        """CAS 原子转换 cell 状态 — 路由到 Redis Queue,写后失效 cells 缓存

        update_fields 合并入 data,经 **data 解包后被 **update_fields 捕获。
        """
        from database import write_router
        _data = {
            "slot_id": slot_id,
            "expected_status": expected_status,
            "new_status": new_status,
            "lease_owner": lease_owner,
            "transition_id": transition_id,
            "lease_seconds": lease_seconds,
        }
        _data.update(_json_safe(update_fields))
        return await write_router.route_write(
            method_name="cas_transition_cell",
            table="cells_local",
            op_type="update",
            data=_data,
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).cas_transition_cell(
                slot_id, expected_status, new_status, lease_owner, transition_id,
                lease_seconds, **update_fields,
            ),
        )

    async def acquire_cell_lease(
        self, slot_id: str, owner: str, lease_seconds: int = 60
    ) -> bool:
        """获取 cell 租约 — 路由到 Redis Queue,写后失效 cells 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="acquire_cell_lease",
            table="cells_local",
            op_type="update",
            data={"slot_id": slot_id, "owner": owner, "lease_seconds": lease_seconds},
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).acquire_cell_lease(
                slot_id, owner, lease_seconds
            ),
        )

    async def release_cell_lease(self, slot_id: str, owner: str):
        """释放 cell 租约 — 路由到 Redis Queue,写后失效 cells 缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="release_cell_lease",
            table="cells_local",
            op_type="update",
            data={"slot_id": slot_id, "owner": owner},
            redis_key="cache:all_cells",
            fallback=lambda: super(CacheStoreRouter, self).release_cell_lease(slot_id, owner),
        )

    async def mark_file_record_synced(self, file_code: str):
        """标记文件记录已同步 — 路由到 Redis Queue,写后失效文件记录缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_file_record_synced",
            table="file_records_local",
            op_type="update",
            data={"file_code": file_code},
            redis_key=f"cache:file_record:{file_code}",
            fallback=lambda: super(CacheStoreRouter, self).mark_file_record_synced(file_code),
        )

    async def mark_code_synced(self, code: str):
        """标记验证码已同步 — 路由到 Redis Queue,写后失效验证码缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_code_synced",
            table="codes_local",
            op_type="update",
            data={"code": code},
            redis_key=f"cache:code:{code}",
            fallback=lambda: super(CacheStoreRouter, self).mark_code_synced(code),
        )

    async def mark_user_synced(self, user_id: int):
        """标记用户已同步 — 路由到 Redis Queue,写后失效用户缓存"""
        from database import write_router
        return await write_router.route_write(
            method_name="mark_user_synced",
            table="users_local",
            op_type="update",
            data={"user_id": user_id},
            redis_key=f"cache:user:{user_id}",
            fallback=lambda: super(CacheStoreRouter, self).mark_user_synced(user_id),
        )

    async def upsert_manifest(
        self, group_id: int, file_unique_id: str, channel_id: int,
        message_id: int, media_type: str = "", media_group_id: str = "",
    ):
        """manifest 登记 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="upsert_manifest",
            table="manifest",
            op_type="upsert",
            data={
                "group_id": group_id,
                "file_unique_id": file_unique_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "media_type": media_type,
                "media_group_id": media_group_id,
            },
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).upsert_manifest(
                group_id, file_unique_id, channel_id, message_id, media_type, media_group_id
            ),
        )

    async def upsert_manifest_batch(self, records: list[dict]):
        """批量 manifest 登记 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="upsert_manifest_batch",
            table="manifest",
            op_type="upsert",
            data={"records": _json_safe(records)},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).upsert_manifest_batch(records),
        )

    async def save_cells_snapshot(self, cells: list[dict], version: int):
        """保存 cells 快照 — 路由到 Redis Queue"""
        from database import write_router
        return await write_router.route_write(
            method_name="save_cells_snapshot",
            table="cells_snapshot",
            op_type="upsert",
            data={"cells": _json_safe(cells), "version": version},
            redis_key="",
            fallback=lambda: super(CacheStoreRouter, self).save_cells_snapshot(cells, version),
        )

    # ── CAS 写方法(直写 SQLite + 失效读缓存,保证强一致)──

    async def try_consume_quota(self, user_id: int, is_external: bool = False) -> bool:
        """CAS 扣减配额 — 直写 SQLite(保证强一致),仅扣减成功时失效配额缓存
        (P1修复: 扣减失败时不删缓存,避免误删其他进程刚写入的有效缓存)
        """
        result = await super().try_consume_quota(user_id, is_external)
        if result:
            from database import redis_queue
            await redis_queue.cache_delete(f"cache:user_quota:{user_id}")
        return result

    async def bulk_upsert_cells_local(self, cells: list[dict]):
        """批量 upsert cells — 小批量路由 Redis Queue 串行化(避免 SQLite 锁冲突),
        大批量(>50)直写 SQLite + 失效缓存(避免大队列阻塞)。
        P1修复: admin_bot 单 cell 调用走 Redis Queue,消除锁冲突。
        """
        from database import write_router, redis_queue
        if len(cells) <= 50:
            # 小批量:走 Redis Queue 串行化,避免锁冲突
            return await write_router.route_write(
                method_name="bulk_upsert_cells_local",
                table="cells_local",
                op_type="upsert",
                data={"cells": _json_safe(cells)},
                redis_key="cache:all_cells",
                fallback=lambda: super(CacheStoreRouter, self).bulk_upsert_cells_local(cells),
            )
        # 大批量:直写 SQLite + 失效缓存(避免大队列阻塞)
        await super().bulk_upsert_cells_local(cells)
        await redis_queue.cache_delete("cache:all_cells")

    async def delete_file_record_local(self, file_code: str):
        """删除文件记录 — 路由到 Redis Queue,写后失效文件记录缓存
        (P0修复: 原先未重写,绕过 Redis 串行化且不失效 cache:file_record,
        导致 get_file_record_local 读到已删除的记录)
        """
        from database import write_router
        return await write_router.route_write(
            method_name="delete_file_record_local",
            table="file_records_local",
            op_type="delete",
            data={"file_code": file_code},
            redis_key=f"cache:file_record:{file_code}",
            fallback=lambda: super(CacheStoreRouter, self).delete_file_record_local(file_code),
        )

    async def batch_update_cells_local(self, updates: list[tuple[str, dict, bool]]):
        """原子批量更新 cells — 直写 SQLite(需事务原子性,不走 Redis),
        写后失效 cells 缓存(P0修复: 原先未重写,mon_bot 旋转后会读到旧缓存)
        """
        await super().batch_update_cells_local(updates)
        from database import redis_queue
        await redis_queue.cache_delete("cache:all_cells")

    async def delete_cell_local(self, slot_id: str) -> bool:
        """删除 cell — 直写 SQLite(需事务修复链表指针,不走 Redis),
        写后失效 cells 缓存(P0修复: 原先未重写,mon_bot 旋转后会读到旧缓存)
        """
        result = await super().delete_cell_local(slot_id)
        from database import redis_queue
        await redis_queue.cache_delete("cache:all_cells")
        return result

    # ── 重写读方法(加 Redis 缓存层)──

    async def get_user_quota(self, user_id: int) -> dict | None:
        """配额读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = f"cache:user_quota:{user_id}"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_user_quota(user_id)
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_QUOTA)
        return result

    async def get_file_record_local(self, file_code: str) -> dict | None:
        """文件记录读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = f"cache:file_record:{file_code}"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_file_record_local(file_code)
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_FILE_RECORD)
        return result

    async def get_code_local(self, code: str) -> dict | None:
        """验证码读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = f"cache:code:{code}"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_code_local(code)
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_CODE)
        return result

    async def get_user_local(self, user_id: int) -> dict | None:
        """用户记录读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = f"cache:user:{user_id}"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_user_local(user_id)
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_USER)
        return result

    async def get_all_cells_local(self) -> list[dict]:
        """全量 cells 读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = "cache:all_cells"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_all_cells_local()
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_CELLS)
        return result

    async def get_all_bot_heartbeats(self) -> dict[str, dict]:
        """全量 Bot 心跳读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = "cache:all_bot_heartbeats"
        cached = await redis_queue.cache_get(cache_key)
        if cached:
            parsed = _loads_cached(cached)  # P1修复: 安全反序列化
            if parsed is not None:
                return parsed
        result = await super().get_all_bot_heartbeats()
        if result:
            await redis_queue.cache_set(cache_key, _dumps_str(result), ttl=settings.WRITER_CACHE_TTL_BOT_HB)
        return result

    async def get_kv(self, key: str) -> str | None:
        """KV 读取 — 优先 Redis 缓存,未命中回退 SQLite + 回填"""
        from database import redis_queue
        from config import settings
        cache_key = f"cache:kv:{key}"
        cached = await redis_queue.cache_get(cache_key)
        if cached is not None:
            return cached
        result = await super().get_kv(key)
        if result is not None:
            await redis_queue.cache_set(cache_key, result, ttl=settings.WRITER_CACHE_TTL_KV)
        return result


# bot 进程返回 CacheStoreRouter(Redis 路由 + 缓存)
# db_writer 进程返回原始 CacheStore(直写 SQLite,不路由)
if os.environ.get("BOT_ROLE", "") == "db_writer":
    _store: CacheStore = CacheStore()
else:
    _store: CacheStore = CacheStoreRouter()
_decode_log_buffer = DecodeLogBuffer()
_code_change_buffer = CodeChangeBuffer()


def get_cache_store() -> CacheStore:
    """获取单例 CacheStore。

    bot 进程返回 CacheStoreRouter(Redis 路由 + 缓存)
    db_writer 进程返回原始 CacheStore(直写 SQLite)
    """
    return _store


def get_decode_log_buffer() -> DecodeLogBuffer:
    return _decode_log_buffer


def get_code_change_buffer() -> CodeChangeBuffer:
    return _code_change_buffer


async def report_bot_heartbeat(name: str, total_processed: int = 0, total_errors: int = 0):
    """模块级便利函数：各 Bot 启动/定时上报心跳。"""
    await _store.write_bot_heartbeat(name, total_processed, total_errors)


async def get_all_bot_heartbeats() -> dict[str, dict]:
    """模块级便利函数：admin_bot 读取所有 Bot 心跳。"""
    return await _store.get_all_bot_heartbeats()


async def get_user_quota(user_id: int) -> dict | None:
    """模块级便利函数：读取用户本地配额。"""
    return await _store.get_user_quota(user_id)


async def upsert_user_quota(user_id: int, data: dict):
    """模块级便利函数：写入用户本地配额。"""
    await _store.upsert_user_quota(user_id, data)


async def increment_user_quota_used(user_id: int, is_external: bool = False):
    """模块级便利函数：原子递增用户已用配额。"""
    await _store.increment_user_quota_used(user_id, is_external)


async def try_consume_quota(user_id: int, is_external: bool = False) -> bool:
    """模块级便利函数：原子条件递增配额(乐观扣减),解决 TOCTOU 竞态。"""
    return await _store.try_consume_quota(user_id, is_external)


async def refund_quota(user_id: int, is_external: bool = False):
    """模块级便利函数：投递失败时回滚配额。"""
    await _store.refund_quota(user_id, is_external)


async def get_unsynced_quotas(min_synced_at: float = 0) -> list[dict]:
    """模块级便利函数：获取待同步到 CRDB 的配额。"""
    return await _store.get_unsynced_quotas(min_synced_at)


async def mark_quota_synced(user_id: int):
    """模块级便利函数：标记配额已同步。"""
    await _store.mark_quota_synced(user_id)


async def invalidate_user_quota_cache(user_id: int):
    """模块级便利函数：使 SQLite 配额缓存失效。"""
    await _store.invalidate_user_quota(user_id)
