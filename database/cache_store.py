"""内存缓存的 SQLite 持久化备份层
- 启动时从 SQLite 恢复到内存 -> VPS 重启后不击穿 CRDB
- 运行时后台定期 dump -> 下次重启有最新的热数据
- WAL 模式支持多进程并发写入
"""
try:
    import orjson as json
except ImportError:
    import json
import time
import aiosqlite
from pathlib import Path
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "data" / "cache_store.db"


class CacheStore:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
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
                rows.append((k, json.dumps(v, default=str).decode(), ts))
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
            val = json.dumps(data, default=str).decode()
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
        await self._db.execute(
            "INSERT INTO pending_notify (ts) VALUES (?)", (time.time(),)
        )
        await self._db.commit()

    async def has_new_upload(self) -> bool:
        """Idx Bot 检查是否有未处理的上传通知。有则返回 True 并原子清空。
        
        使用 DELETE ... RETURNING 实现原子出队，避免 SELECT + DELETE 竞态。
        """
        if not self._db:
            return True  # 连接未就绪，回退到直接查 CRDB
        row = await self._db.execute_fetchall(
            "DELETE FROM pending_notify WHERE id = (SELECT id FROM pending_notify LIMIT 1) RETURNING id"
        )
        return bool(row)

    # ─── Dsp Bot 通知：Idx Bot 写入 → Dsp Bot 感知 ───

    async def notify_dsp_new_job(self):
        """Idx Bot 写 jobs 表后调用，通知 Dsp Bot 有新任务。"""
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO dsp_notify (ts) VALUES (?)", (time.time(),)
        )
        await self._db.commit()

    async def has_new_dsp_job(self) -> bool:
        """Dsp Bot 检查是否有未处理的新 jobs。有则返回 True 并原子清空。
        
        使用 DELETE ... RETURNING 实现原子出队，避免 SELECT + DELETE 竞态。
        """
        if not self._db:
            return True  # 连接未就绪，回退到直接查 CRDB
        row = await self._db.execute_fetchall(
            "DELETE FROM dsp_notify WHERE id = (SELECT id FROM dsp_notify LIMIT 1) RETURNING id"
        )
        return bool(row)

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None


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