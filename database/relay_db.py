"""中继账号池本地 SQLite 数据库
- 中继账号配置、认证、使用统计全部存储在本地，不占用 CockroachDB Cloud 配额
- VPS 重启后从本地恢复，无需重新登录
"""
import os
import aiosqlite
from datetime import date
from pathlib import Path
from loguru import logger

# ── 加密层（Fernet 对称加密）──────────────────────────────────────────
_fernet = None
_FERNET_ERROR = None

try:
    from cryptography.fernet import Fernet
except ImportError as e:
    _FERNET_ERROR = str(e)
    logger.warning(f"[RelayDB] cryptography 库未安装，API_HASH 将以明文存储: {_FERNET_ERROR}")


def _get_fernet() -> Fernet | None:
    """延迟初始化 Fernet 实例，从环境变量读取密钥或自动生成"""
    global _fernet, _FERNET_ERROR
    if _fernet is not None:
        return _fernet
    if _FERNET_ERROR is not None:
        return None

    key = os.getenv("RELAY_ENCRYPTION_KEY", "")
    if not key:
        key = Fernet.generate_key().decode()
        os.environ["RELAY_ENCRYPTION_KEY"] = key
        logger.warning(
            "[RelayDB] ⚠️  RELAY_ENCRYPTION_KEY 未设置，已自动生成密钥。"
            "请立即将以下密钥添加到 .env 文件以确保重启后能解密已有数据：\n"
            f"RELAY_ENCRYPTION_KEY={key}"
        )
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except Exception as e:
        _FERNET_ERROR = str(e)
        logger.error(f"[RelayDB] Fernet 初始化失败，API_HASH 将以明文存储: {e}")
        return None


def encrypt(plain_text: str) -> str:
    """加密明文，返回密文字符串；如果加密不可用则返回明文 + 标记前缀"""
    f = _get_fernet()
    if f is None:
        return plain_text
    return f.encrypt(plain_text.encode()).decode()


def decrypt(cipher_text: str) -> str:
    """解密密文，返回明文字符串；如果解密失败或不可用则返回原文"""
    f = _get_fernet()
    if f is None:
        return cipher_text
    try:
        return f.decrypt(cipher_text.encode()).decode()
    except Exception as e:
        logger.error(
            f"[RelayDB] 解密失败，API_HASH 数据可能已损坏或密钥不匹配，"
            f"返回原值（可能无法正确登录）: {e}"
        )
        return cipher_text


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
        self._request_count = 0

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(DB_PATH))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
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
            (api_id, encrypt(api_hash), phone),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_account(self, phone: str, api_id: int, api_hash: str):
        await self._db.execute(
            "UPDATE relay_accounts SET api_id=?, api_hash=? WHERE phone=?",
            (api_id, encrypt(api_hash), phone),
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
                "api_hash": decrypt(r[2]),
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
                "api_hash": decrypt(r[2]),
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
        """原子操作记录请求：使用 INSERT ... ON CONFLICT DO UPDATE 避免竞态条件"""
        self._request_count += 1
        await self._db.execute(
            "INSERT INTO relay_usage "
            "(relay_id, today_requests, total_requests, total_wait_ms, "
            "last_request_at, last_reset_at) "
            "VALUES (?, 1, 1, ?, datetime('now'), date('now')) "
            "ON CONFLICT(relay_id) DO UPDATE SET "
            "  today_requests = CASE WHEN last_reset_at != date('now') "
            "    THEN 1 ELSE today_requests + 1 END, "
            "  total_requests = total_requests + 1, "
            "  total_wait_ms = total_wait_ms + ?, "
            "  avg_wait_ms = (total_wait_ms + ?) * 1.0 / (total_requests + 1), "
            "  last_request_at = datetime('now'), "
            "  last_reset_at = CASE WHEN last_reset_at != date('now') "
            "    THEN date('now') ELSE last_reset_at END",
            (relay_id, duration_ms, duration_ms, duration_ms),
        )
        await self._db.commit()
        if self._request_count % 100 == 0:
            await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

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
