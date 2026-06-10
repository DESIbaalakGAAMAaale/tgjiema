"""中继账号池本地 SQLite 数据库
- 中继账号配置、认证、使用统计全部存储在本地，不占用 CockroachDB Cloud 配额
- VPS 重启后从本地恢复，无需重新登录
"""
import aiosqlite
from pathlib import Path
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "data" / "relay_pool.db"

DDL = """
CREATE TABLE IF NOT EXISTS relay_accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id       BIGINT NOT NULL,
    api_hash     TEXT NOT NULL,
    phone        TEXT NOT NULL UNIQUE,
    is_active    INTEGER DEFAULT 1,
    auth_code    TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS relay_usage (
    relay_id      INTEGER PRIMARY KEY REFERENCES relay_accounts(id),
    today_requests INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    total_wait_ms  INTEGER DEFAULT 0,
    avg_wait_ms    REAL DEFAULT 0,
    last_request_at TEXT,
    last_reset_at  TEXT DEFAULT (date('today'))
);

CREATE TABLE IF NOT EXISTS relay_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    relay_id    INTEGER REFERENCES relay_accounts(id),
    action      TEXT NOT NULL,
    code        TEXT,
    bot_target  TEXT,
    duration_ms INTEGER,
    error       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
"""


class RelayDB:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(DDL)
        await self._db.commit()
        logger.info(f"[RelayDB] 初始化完成: {DB_PATH}")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    # ── relay_accounts ──

    async def add_account(self, api_id: int, api_hash: str, phone: str) -> int:
        cursor = await self._db.execute(
            "INSERT INTO relay_accounts (api_id, api_hash, phone) VALUES (?, ?, ?)",
            (api_id, api_hash, phone),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_account(self, phone: str, api_id: int, api_hash: str):
        await self._db.execute(
            "UPDATE relay_accounts SET api_id=?, api_hash=? WHERE phone=?",
            (api_id, api_hash, phone),
        )
        await self._db.commit()

    async def deactivate_account(self, phone: str):
        await self._db.execute(
            "UPDATE relay_accounts SET is_active=0 WHERE phone=?", (phone,)
        )
        await self._db.commit()

    async def activate_account(self, phone: str):
        await self._db.execute(
            "UPDATE relay_accounts SET is_active=1 WHERE phone=?", (phone,)
        )
        await self._db.commit()

    async def set_last_login(self, phone: str):
        await self._db.execute(
            "UPDATE relay_accounts SET last_login_at=datetime('now') WHERE phone=?",
            (phone,),
        )
        await self._db.commit()

    async def get_active_accounts(self) -> list[dict]:
        rows = await self._db.execute_fetchall(
            "SELECT id, api_id, api_hash, phone, is_active, created_at, last_login_at "
            "FROM relay_accounts WHERE is_active=1 ORDER BY id"
        )
        return [
            {
                "id": r[0],
                "api_id": r[1],
                "api_hash": r[2],
                "phone": r[3],
                "is_active": bool(r[4]),
                "created_at": r[5],
                "last_login_at": r[6],
            }
            for r in rows
        ]

    async def get_all_accounts(self) -> list[dict]:
        rows = await self._db.execute_fetchall(
            "SELECT id, api_id, api_hash, phone, is_active, created_at, last_login_at "
            "FROM relay_accounts ORDER BY id"
        )
        return [
            {
                "id": r[0],
                "api_id": r[1],
                "api_hash": r[2],
                "phone": r[3],
                "is_active": bool(r[4]),
                "created_at": r[5],
                "last_login_at": r[6],
            }
            for r in rows
        ]

    async def remove_account(self, phone: str) -> bool:
        cur = await self._db.execute("DELETE FROM relay_accounts WHERE phone=?", (phone,))
        await self._db.commit()
        return cur.rowcount > 0

    # ── relay_usage ──

    async def get_usage(self, relay_id: int) -> dict:
        row = await self._db.execute_fetchone(
            "SELECT today_requests, total_requests, total_wait_ms, avg_wait_ms, "
            "last_request_at, last_reset_at FROM relay_usage WHERE relay_id=?",
            (relay_id,),
        )
        if not row:
            return {
                "relay_id": relay_id,
                "today_requests": 0,
                "total_requests": 0,
                "total_wait_ms": 0,
                "avg_wait_ms": 0,
                "last_request_at": None,
                "last_reset_at": None,
            }
        return {
            "relay_id": relay_id,
            "today_requests": row[0],
            "total_requests": row[1],
            "total_wait_ms": row[2],
            "avg_wait_ms": row[3],
            "last_request_at": row[4],
            "last_reset_at": row[5],
        }

    async def record_request(self, relay_id: int, duration_ms: int):
        today = date_today()
        # 检查是否需要重置日统计
        row = await self._db.execute_fetchone(
            "SELECT last_reset_at, today_requests, total_requests, total_wait_ms "
            "FROM relay_usage WHERE relay_id=?", (relay_id,)
        )
        if not row:
            await self._db.execute(
                "INSERT INTO relay_usage (relay_id, today_requests, total_requests, total_wait_ms) "
                "VALUES (?, 1, 1, ?)",
                (relay_id, duration_ms),
            )
        else:
            last_reset = row[0]
            today_req = row[1]
            total_req = row[2]
            total_wait = row[3]
            if last_reset != today:
                today_req = 0
                total_req = 0
                total_wait = 0
            today_req += 1
            total_req += 1
            total_wait += duration_ms
            avg = total_wait / total_req if total_req > 0 else 0
            await self._db.execute(
                "UPDATE relay_usage SET today_requests=?, total_requests=?, "
                "total_wait_ms=?, avg_wait_ms=?, last_request_at=datetime('now') "
                "WHERE relay_id=?",
                (today_req, total_req, total_wait, avg, relay_id),
            )
        await self._db.commit()

    async def reset_usage(self):
        await self._db.execute("DELETE FROM relay_usage")
        await self._db.commit()

    # ── relay_log ──

    async def add_log(self, relay_id: int, action: str, code: str = None,
                      bot_target: str = None, duration_ms: int = None, error: str = None):
        await self._db.execute(
            "INSERT INTO relay_log (relay_id, action, code, bot_target, duration_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (relay_id, action, code, bot_target, duration_ms, error),
        )
        await self._db.commit()


def date_today() -> str:
    from datetime import date
    return date.today().isoformat()


_relay_db: RelayDB | None = None


async def get_relay_db() -> RelayDB:
    global _relay_db
    if _relay_db is None:
        _relay_db = RelayDB()
        await _relay_db.init()
    return _relay_db


def _get_relay_db_sync() -> RelayDB:
    """同步获取（用于 admin bot 回调中，此时 DB 已初始化）"""
    global _relay_db
    if _relay_db is None:
        raise RuntimeError("RelayDB not initialized. Call init_db() first.")
    return _relay_db
