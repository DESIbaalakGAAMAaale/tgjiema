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
import time
import aiosqlite
from pathlib import Path
from typing import Optional
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "data" / "cache_store.db"


class CacheStore:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH), timeout=10)
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
        except Exception:
            pass
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
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cells_local_status ON cells_local(status)"
        )
        # ─── KV 键值存储：用于缓存 DDL 版本等配置 ───
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS kv_store (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        await self._db.commit()
        # ─── 注入 db 连接给 Buffer ───
        _decode_log_buffer.set_db(self._db)
        _code_change_buffer.set_db(self._db)
        logger.info(f"[CacheStore] 初始化完成: {DB_PATH}")

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
        except Exception:
            pass

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
        except Exception:
            pass  # 清理失败不影响主流程

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

    async def mark_local_job_dispatched(self, crdb_id: int):
        """标记本地 job 为 dispatched"""
        if not self._db:
            return
        await self._db.execute(
            "UPDATE local_job_queue SET status='dispatched', dispatched_at=? WHERE crdb_id=?",
            (time.time(), crdb_id),
        )
        await self._db.commit()

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

    # ─── F5: 启动统计快照 ───

    async def save_counter_snapshot(self, counters: dict[str, int]):
        """保存启动统计快照(各 Bot 周期性写入)"""
        if not self._db:
            return
        now = time.time()
        for k, v in counters.items():
            await self._db.execute(
                "INSERT OR REPLACE INTO counter_snapshot (key, value, ts) VALUES (?, ?, ?)",
                (k, v, now),
            )
        await self._db.commit()

    async def load_counter_snapshot(self) -> dict[str, int]:
        """加载启动统计快照"""
        if not self._db:
            return {}
        try:
            rows = await self._db.execute_fetchall(
                "SELECT key, value FROM counter_snapshot"
            )
            return {r[0]: r[1] for r in rows}
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
        """初始化/全量同步:批量写入 cells 到本地表(crdb_synced=1,视为已同步)"""
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
                    (slot_id, channel_id, status, next_active_chat_id, account_name,
                     is_r100, last_heartbeat, last_synced_msg_id, degrade_count,
                     file_count, rotation_started_at, updated_at, crdb_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
                return

    async def get_all_cells_local(self) -> list[dict]:
        """从本地表读取全部 cells，返回 dict 列表（零 CRDB RU）"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, account_name,
                      is_r100, last_heartbeat, last_synced_msg_id, degrade_count,
                      file_count, rotation_started_at
               FROM cells_local ORDER BY slot_id"""
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "account_name",
                "is_r100", "last_heartbeat", "last_synced_msg_id", "degrade_count",
                "file_count", "rotation_started_at"]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            d["is_r100"] = int(d["is_r100"] or 0)
            d["degrade_count"] = int(d["degrade_count"] or 0)
            d["file_count"] = int(d["file_count"] or 0)
            d["last_synced_msg_id"] = int(d["last_synced_msg_id"] or 0)
            if d["next_active_chat_id"]:
                d["next_active_chat_id"] = int(d["next_active_chat_id"])
            result.append(d)
        return result

    async def get_active_cells_local(self) -> list[dict]:
        """从本地表读取 status=active 的 cells（零 CRDB RU）"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, account_name,
                      is_r100, last_heartbeat, last_synced_msg_id, degrade_count,
                      file_count, rotation_started_at
               FROM cells_local WHERE status = 'active'"""
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "account_name",
                "is_r100", "last_heartbeat", "last_synced_msg_id", "degrade_count",
                "file_count", "rotation_started_at"]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            d["is_r100"] = int(d["is_r100"] or 0)
            d["degrade_count"] = int(d["degrade_count"] or 0)
            d["file_count"] = int(d["file_count"] or 0)
            d["last_synced_msg_id"] = int(d["last_synced_msg_id"] or 0)
            if d["next_active_chat_id"]:
                d["next_active_chat_id"] = int(d["next_active_chat_id"])
            result.append(d)
        return result

    async def get_dirty_cells_local(self, limit: int = 50) -> list[dict]:
        """获取需要同步到 CRDB 的脏 cells（异常事件）"""
        if not self._db:
            return []
        rows = await self._db.execute_fetchall(
            """SELECT slot_id, channel_id, status, next_active_chat_id, account_name,
                      is_r100, degrade_count, file_count, rotation_started_at
               FROM cells_local WHERE crdb_synced = 0 LIMIT ?""",
            (limit,),
        )
        cols = ["slot_id", "channel_id", "status", "next_active_chat_id", "account_name",
                "is_r100", "degrade_count", "file_count", "rotation_started_at"]
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
        version = int(now * 1000)
        await self._db.execute(
            "INSERT OR REPLACE INTO cells_snapshot (id, data, version, updated_at) VALUES (1, ?, ?, ?)",
            (val, version, now),
        )
        await self._bump_cells_version(now)

    async def _bump_cells_version(self, ts: float):
        """写入变更通知 + 递增版本号"""
        if not self._db:
            return
        version = int(ts * 1000)
        await self._db.execute(
            "INSERT INTO cells_change_notify (version, ts) VALUES (?, ?)",
            (version, ts),
        )
        await self._db.commit()

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


async def get_unsynced_quotas(min_synced_at: float = 0) -> list[dict]:
    """模块级便利函数：获取待同步到 CRDB 的配额。"""
    return await _store.get_unsynced_quotas(min_synced_at)


async def mark_quota_synced(user_id: int):
    """模块级便利函数：标记配额已同步。"""
    await _store.mark_quota_synced(user_id)


async def invalidate_user_quota_cache(user_id: int):
    """模块级便利函数：使 SQLite 配额缓存失效。"""
    await _store.invalidate_user_quota(user_id)
