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
from pathlib import Path
from typing import Optional
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "data" / "cache_store.db"

# 单调递增版本号计数器，替代时间戳避免同一毫秒内多次变更获得相同版本号
_cells_version_counter = int(time.time() * 1000)

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


class CacheStore:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None

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
        await self._db.execute("PRAGMA busy_timeout=5000")
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
                crdb_synced          INTEGER DEFAULT 1
            )"""
        )
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

    async def reactivate_waiting_start_jobs(self, user_id: int):
        """用户 /start dsp 后，将其 waiting_start jobs 改回 pending"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET status = 'pending' WHERE target_user_id = ? AND status = 'waiting_start'",
            (user_id,),
        )
        await self._db.commit()
        # 写入 dsp_notify 触发 worker 重新拉取
        await self._db.execute("INSERT INTO dsp_notify (ts) VALUES (?)", (time.time(),))
        await self._db.commit()

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
            await self._db.commit()
        except Exception as e:
            logger.debug(f"[CacheStore] 通知表清理失败: {e}")

    # ─── 心跳本地存储：Mon Bot 写入 SQLite，零 CRDB RU ───

    async def write_heartbeat(self, slot_id: str, ok: bool):
        """写入本地心跳记录。ok=True 时重置 fail_streak，ok=False 时递增。
        同时更新 cells_local.last_heartbeat（ISO 格式）供降级 cooldown 判断使用。
        """
        if not self._db:
            return
        now = time.time()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
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

    async def reclaim_stale_dispatched(self, timeout_seconds: int = 300) -> int:
        """回收超时的 dispatched 状态 job,重置为 pending。
        防止进程崩溃或 task 异常导致 job 永久停留在 dispatched 状态。
        返回回收的 job 数量。
        """
        if not self._db:
            return 0
        cutoff = (datetime.datetime.now(datetime.timezone.utc) -
                  datetime.timedelta(seconds=timeout_seconds)).isoformat()
        cursor = await self._db.execute(
            "UPDATE local_job_queue SET status='pending', dispatched_at=NULL, synced_at=0 "
            "WHERE status='dispatched' AND dispatched_at < ?",
            (cutoff,),
        )
        count = cursor.rowcount
        if count > 0:
            await self._db.commit()
            await self.notify_dsp_new_job()
            logger.info(f"[CacheStore] 回收 {count} 个超时 dispatched jobs")
        return count

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
                    await self._db.commit()
                except Exception:
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
        await self._db.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._db.commit()

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
                r.get("create_time"), r.get("updated_at"), 1,  # crdb_synced=1
            ))
        await self._db.executemany(
            """INSERT OR REPLACE INTO file_records_local
            (file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
             file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
             file_ids, status, request_count, protect_content, file_ttl_days, note,
             expire_time, blocked_users, create_time, updated_at, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                      expire_time, blocked_users, create_time, updated_at
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
            "create_time": r[16], "updated_at": r[17],
        })

    async def upsert_file_record_local(self, record: dict, mark_dirty: bool = True):
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
             expire_time, blocked_users, create_time, updated_at, crdb_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.get("file_code"), record.get("uploader_id"),
             record.get("primary_channel_id"), record.get("primary_channel_msg_id"),
             _serialize(record.get("file_types")), _serialize(record.get("backup_channel_msg_ids")),
             record.get("batch_msg_ids"), _serialize(record.get("batch_file_meta")),
             record.get("file_ids"), record.get("status", "active"),
             record.get("request_count", 0), int(record.get("protect_content", 0) or 0),
             record.get("file_ttl_days", 0), record.get("note", ""),
             record.get("expire_time"), _serialize(record.get("blocked_users", "[]")),
             record.get("create_time"), record.get("updated_at"), synced),
        )
        await self._db.commit()

    async def get_dirty_file_records(self, limit: int = 100) -> list[dict]:
        """获取需要同步到 CRDB 的脏 file_records"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
                      file_types, backup_channel_msg_ids, batch_msg_ids, batch_file_meta,
                      file_ids, status, request_count, protect_content, file_ttl_days, note,
                      expire_time, blocked_users, create_time, updated_at
               FROM file_records_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        return [_deserialize_sqlite_row({
            "file_code": r[0], "uploader_id": r[1], "primary_channel_id": r[2],
            "primary_channel_msg_id": r[3], "file_types": r[4], "backup_channel_msg_ids": r[5],
            "batch_msg_ids": r[6], "batch_file_meta": r[7], "file_ids": r[8], "status": r[9],
            "request_count": r[10], "protect_content": r[11], "file_ttl_days": r[12],
            "note": r[13], "expire_time": r[14], "blocked_users": r[15],
            "create_time": r[16], "updated_at": r[17],
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

    async def upsert_code_local(self, record: dict, mark_dirty: bool = True):
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

    async def upsert_user_local(self, user: dict, mark_dirty: bool = True):
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
        """写入本地缓冲表（零 RU）"""
        if not self._db:
            return
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

    async def close(self):
        pass


class CodeChangeBuffer:
    """文件码变更缓冲，定时批量 flush 到 CRDB"""

    def __init__(self):
        self._db = None

    def set_db(self, db):
        self._db = db

    async def insert(self, code: str, change_type: str, new_value: str, uploader_id: int):
        """写入变更缓冲（零 CRDB RU）"""
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO code_changes (code, change_type, new_value, uploader_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, change_type, new_value, uploader_id, time.time()),
        )
        await self._db.commit()

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


_store = CacheStore()
_decode_log_buffer = DecodeLogBuffer()
_code_change_buffer = CodeChangeBuffer()


def get_cache_store() -> CacheStore:
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
