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
        await self._db.commit()
        # 注入 db 连接给 DecodeLogBuffer
        _decode_log_buffer.set_db(self._db)
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
            except (TypeError, ValueError):
                continue
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
        except (TypeError, ValueError):
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
        """Idx Bot 检查是否有未处理的上传通知。有则返回 True 并原子清空。
        
        使用 DELETE ... RETURNING 实现原子出队，避免 SELECT + DELETE 竞态。
        """
        if not self._db:
            return True  # 连接未就绪，回退到直接查 CRDB
        row = await self._db.execute_fetchall(
            "DELETE FROM pending_notify WHERE id = (SELECT id FROM pending_notify LIMIT 1) RETURNING id"
        )
        await self._db.commit()
        return bool(row)

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
        """Dsp Bot 检查是否有未处理的新 jobs。有则返回 True 并原子清空。
        
        使用 DELETE ... RETURNING 实现原子出队，避免 SELECT + DELETE 竞态。
        """
        if not self._db:
            return True  # 连接未就绪，回退到直接查 CRDB
        row = await self._db.execute_fetchall(
            "DELETE FROM dsp_notify WHERE id = (SELECT id FROM dsp_notify LIMIT 1) RETURNING id"
        )
        await self._db.commit()
        return bool(row)

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
            await self._db.commit()
        except Exception:
            pass  # 清理失败不影响主流程

    # ─── 心跳本地存储：Mon Bot 写入 SQLite，零 CRDB RU ───

    async def write_heartbeat(self, slot_id: str, ok: bool):
        """写入本地心跳记录。ok=True 时重置 fail_streak，ok=False 时递增。"""
        if not self._db:
            return
        now = time.time()
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
]


class DecodeLogBuffer:
    """Decode Logs 本地缓冲，定时批量 flush 到 CRDB
    
    注意：缓冲表 DDL 由 CacheStore.init() 统一创建，此处无需重复。
    """

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


_store = CacheStore()
_decode_log_buffer = DecodeLogBuffer()


def get_cache_store() -> CacheStore:
    return _store


def get_decode_log_buffer() -> DecodeLogBuffer:
    return _decode_log_buffer


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