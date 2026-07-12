"""内存缓存的 SQLite 持久化备份层
- 启动时从 SQLite 恢复到内存 -> VPS 重启后不击穿 CRDB
- 运行时后台定期 dump -> 下次重启有最新的热数据
- WAL 模式支持多进程并发写入
"""
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
    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self._in_writer_tx: bool = False  # R34: Writer 事务模式标志

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = await aiosqlite.connect(str(DB_PATH), timeout=10)
        except (sqlite3.DatabaseError, aiosqlite.Error) as e:
            if "file is not a database" in str(e).lower() and DB_PATH.exists():
                logger.warning(f"[CacheStore] SQLite 文件已损坏，删除重建: {DB_PATH}")
                DB_PATH.unlink()
                self._db = await aiosqlite.connect(str(DB_PATH), timeout=10)
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
                synced_at     REAL DEFAULT 0
            )"""
        )
        try:
            await self._db.execute("ALTER TABLE local_job_queue ADD COLUMN dead_retry_count INTEGER DEFAULT 0")
        except Exception as e:
            logger.warning(f"[CacheStore] ALTER TABLE失败(非预期): {e}")
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
                crdb_synced INTEGER DEFAULT 1
            )"""
        )
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
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
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
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
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
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS external_code_mapping_local (
                external_code        TEXT PRIMARY KEY,
                system_code          TEXT NOT NULL,
                bot_username         TEXT,
                created_at           TEXT,
                updated_at           TEXT,
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
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
                UNIQUE(job_id, source_msg_id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_job ON delivery_receipts(job_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_target ON delivery_receipts(target_user_id)"
        )

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

        await self._db.commit()
        # ─── 注入 db 连接给 Buffer ───
        _decode_log_buffer.set_db(self._db)
        _code_change_buffer.set_db(self._db)
        logger.debug(f"[CacheStore] 初始化完成: {DB_PATH}")

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
        """从 SQLite 读取用户配额。未找到返回 None。"""
        if not self._db:
            return None
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
    ) -> None:
        """创建上传会话,初始状态 RECEIVED。

        Args:
            upload_id: UUID 主键
            user_id: 发起用户 ID
            source_msg_ids: 用户原消息 ID 列表(JSON 序列化存储)
            options_json: 上传选项(protect_content/ttl/note 等,JSON 序列化)
            trace_id: 链路追踪 ID
        """
        if not self._db or not upload_id:
            return
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
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return

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
        """
        if not self._db or not upload_id:
            return False
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
    ) -> None:
        """创建发件箱条目,初始状态 PENDING。

        幂等:使用 INSERT OR IGNORE 避免重复插入(同 outbox_id 已存在则跳过)。
        """
        if not self._db or not outbox_id:
            return
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
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                if self._in_writer_tx:
                    raise
                return

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
    ) -> bool:
        """标记发条为失败(attempts+1, next_retry_at=next_retry_at)。

        本方法不直接置为 'FAILED',而是 increments attempts 并设置 next_retry_at。
        调用方应根据 attempts >= UPLOAD_OUTBOX_MAX_ATTEMPTS 判断是否终止。
        若需要直接终止,可将 status 置为 'FAILED'(调用方自行管理)。
        reason 仅记录到日志(upload_outbox 表无 last_error 列)。
        """
        if not self._db or not outbox_id:
            return False
        now = time.time()
        logger.info(f"[CacheStore] outbox {outbox_id} 失败: {reason}, next_retry_at={next_retry_at}")
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "UPDATE upload_outbox SET attempts = attempts + 1, "
                    "next_retry_at = ?, processed_at = ? "
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
        request_id: str = "", reason: str = "",
    ) -> None:
        """追加配额变更流水(INSERT,自增主键)。

        Args:
            event_type: consume/refund/sync/reset/expire
            request_id: 业务幂等键(可选,用于去重检查)
        """
        if not self._db:
            return
        now = time.time()
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
    ) -> None:
        """写入或更新投递回执(基于 UNIQUE(job_id, source_msg_id))。

        INSERT OR REPLACE 会删除旧记录并插入新记录(自增 receipt_id 会变)。
        若需保留 receipt_id,应改用 UPDATE。本方法用于首次写入和重试场景。
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
                    " error_reason, created_at, confirmed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, NULL)",
                    (job_id, source_msg_id, target_user_id, sent_msg_id,
                     media_group_id or "", group_receipt_id or "", status, now),
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

    async def get_delivery_receipts_by_job(self, job_id: int) -> list[dict]:
        """查询某 job 的所有投递回执。"""
        if not self._db:
            return []
        try:
            rows = await self._db.execute_fetchall(
                "SELECT receipt_id, job_id, source_msg_id, target_user_id, "
                "sent_msg_id, media_group_id, group_receipt_id, status, "
                "attempts, error_reason, created_at, confirmed_at "
                "FROM delivery_receipts WHERE job_id = ? "
                "ORDER BY source_msg_id",
                (job_id,),
            )
        except Exception as e:
            logger.warning(f"[CacheStore] get_delivery_receipts_by_job 异常: {e}")
            return []
        cols = ["receipt_id", "job_id", "source_msg_id", "target_user_id",
                "sent_msg_id", "media_group_id", "group_receipt_id", "status",
                "attempts", "error_reason", "created_at", "confirmed_at"]
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
             retry_count, protect_content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                     retry_count, protect_content, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
               retry_count, protect_content, created_at
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
            "retry_count, protect_content, created_at "
            "FROM local_job_queue WHERE crdb_id = ?",
            (crdb_id,),
        )
        if not rows:
            return None
        cols = ["crdb_id", "code", "target_user_id", "storage_channel_id",
                "storage_msg_ids", "batch_file_meta", "task_type", "status",
                "retry_count", "protect_content", "created_at"]
        row = dict(zip(cols, rows[0]))
        # 类型转换(与 get_local_pending_jobs 保持一致)
        try:
            row["protect_content"] = bool(row.get("protect_content", False))
        except Exception:
            row["protect_content"] = False
        return row

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
        """
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, prev_slot_id,
                      demoted_to_channel_id, account_name, is_r100, last_heartbeat,
                      last_synced_msg_id, degrade_count, file_count, rotation_started_at
               FROM cells_local ORDER BY slot_id"""
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
            # 删除目标 cell
            await self._db.execute(
                "DELETE FROM cells_local WHERE slot_id = ?",
                (slot_id,),
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
        """从 SQLite 本地缓存中删除 file_record（N-M13: deliver_cached 过期清理）"""
        if not self._db:
            return
        await self._db.execute(
            "DELETE FROM file_records_local WHERE file_code = ?",
            (file_code,),
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
            ))
        await self._db.executemany(
            """INSERT OR REPLACE INTO users_local
            (user_id, username, first_name, membership_level, daily_decode_quota,
             quota_used_today, quota_date, can_upload, external_decode_quota,
             external_used_today, external_quota_date, is_banned,
             created_at, updated_at, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                      is_banned, created_at, updated_at
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
             created_at, updated_at, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user.get("user_id"), user.get("username"), user.get("first_name"),
             user.get("membership_level", "free"), user.get("daily_decode_quota", 3),
             user.get("quota_used_today", 0), user.get("quota_date"),
             user.get("can_upload", 0), user.get("external_decode_quota", 0),
             user.get("external_used_today", 0), user.get("external_quota_date"),
             user.get("is_banned", 0), user.get("created_at"), user.get("updated_at"),
             synced),
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
                      is_banned, created_at, updated_at
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
        } for r in rows]

    async def mark_user_synced(self, user_id: int):
        if not self._db:
            return
        await self._db.execute(
            "UPDATE users_local SET crdb_synced = 1 WHERE user_id = ?", (user_id,),
        )
        await self._db.commit()

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
