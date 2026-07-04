"""中继账号池本地 SQLite 数据库
- 中继账号配置、认证、使用统计全部存储在本地，不占用 CockroachDB Cloud 配额
- VPS 重启后从本地恢复，无需重新登录
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import aiosqlite
from datetime import date, datetime, timezone
from pathlib import Path
from loguru import logger

# 后台异步同步任务的引用集合，防止 GC 回收 fire-and-forget 任务
_background_tasks: set = set()

# ── 加密层（Fernet 对称加密）──────────────────────────────────────────
# PRE-10: 取消所有静默回退路径。cryptography 缺失、密钥未配置、密钥格式非法、
# 解密失败均直接抛出 RuntimeError，避免 API_HASH 明文落盘或静默登录失败。
_fernet = None
_FERNET_ERROR = None

try:
    from cryptography.fernet import Fernet
except ImportError as e:
    _FERNET_ERROR = (
        f"cryptography 库未安装: {e}。"
        "请运行: pip install cryptography"
    )
    logger.error(f"[RelayDB] {_FERNET_ERROR}")


def _get_fernet() -> Fernet:
    """延迟初始化 Fernet 实例，从环境变量读取密钥。

    PRE-10: 任何失败路径都抛出 RuntimeError，绝不返回 None 以避免静默回退明文存储。
    """
    global _fernet, _FERNET_ERROR
    if _fernet is not None:
        return _fernet
    if _FERNET_ERROR is not None:
        raise RuntimeError(f"[RelayDB] 加密不可用: {_FERNET_ERROR}")

    key = os.getenv("RELAY_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "[RelayDB] RELAY_ENCRYPTION_KEY 未设置！\n"
            "请运行以下命令生成密钥并添加到 .env 文件：\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "然后将输出的密钥添加到 .env：\n"
            "  RELAY_ENCRYPTION_KEY=<生成的密钥>\n"
            "注意：密钥一旦设定不可更改，否则已加密的 API_HASH 将无法解密。"
        )
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except Exception as e:
        _FERNET_ERROR = (
            f"Fernet 初始化失败（密钥格式非法或损坏）: {e}。"
            "请重新生成 RELAY_ENCRYPTION_KEY 并更新 .env。"
            "注意：更换密钥后已加密的 API_HASH 将无法解密，需重新录入中继账号。"
        )
        logger.error(f"[RelayDB] {_FERNET_ERROR}")
        raise RuntimeError(f"[RelayDB] {_FERNET_ERROR}")


def encrypt(plain_text: str) -> str:
    """加密明文，返回密文字符串。

    PRE-10: 加密失败直接抛出 RuntimeError，绝不返回明文（避免 API_HASH 明文落盘）。
    """
    f = _get_fernet()
    return f.encrypt(plain_text.encode()).decode()


def decrypt(cipher_text: str) -> str:
    """解密密文，返回明文字符串。

    PRE-10: 解密失败直接抛出 RuntimeError，绝不静默返回原值。
    原行为（返回 cipher_text）会让登录静默失败且日志含糊，改为显式抛错让运维立即感知。
    若库中存在历史明文数据（PRE-10 之前写入），此函数会抛错，需用正确密钥重新加密或清理。
    """
    f = _get_fernet()
    try:
        return f.decrypt(cipher_text.encode()).decode()
    except Exception as e:
        raise RuntimeError(
            f"[RelayDB] 解密失败: {e}。"
            "API_HASH 数据可能已损坏或密钥不匹配。"
            "请检查 RELAY_ENCRYPTION_KEY 是否与加密时使用的密钥一致，"
            "或清理 relay_accounts 表后重新录入中继账号。"
        )


DB_PATH = Path(__file__).parent.parent / "data" / "relay_pool.db"


async def _sync_relay_to_crdb(api_id: int, api_hash: str, phone: str):
    """异步同步中继账号到 CRDB（不阻塞主流程）。
    S-1: api_hash 在落云前加密，与本地 SQLite 保持一致的安全级别。
    """
    try:
        from .session import _client, sync_relay_to_crdb as _do_sync
        if _client._pool is None:
            return  # CRDB 未连接，跳过
        await _do_sync(api_id, encrypt(api_hash), phone)
    except Exception as e:
        logger.debug(f"[RelayDB] CRDB 同步失败（不影响本地）: {e}")

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

CREATE TABLE IF NOT EXISTS bot_cooldown (
    bot_username     TEXT PRIMARY KEY,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    last_decode_at   TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);
"""


class RelayDB:
    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self._request_count = 0

    async def init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = await aiosqlite.connect(str(DB_PATH), timeout=10)
        except (sqlite3.DatabaseError, aiosqlite.Error) as e:
            if "file is not a database" in str(e).lower() and DB_PATH.exists():
                logger.warning(f"[RelayDB] SQLite 文件已损坏，删除重建: {DB_PATH}")
                DB_PATH.unlink()
                self._db = await aiosqlite.connect(str(DB_PATH), timeout=10)
            else:
                raise
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
        await self._db.executescript(DDL)
        await self._db.commit()
        
        # 启动恢复：如果 SQLite 为空，从 CRDB 拉取
        cursor = await self._db.execute("SELECT COUNT(*) FROM relay_accounts")
        row = await cursor.fetchone()
        if row and row[0] == 0:
            try:
                from .session import get_relay_accounts_from_crdb
                accounts = await get_relay_accounts_from_crdb()
                for acc in accounts:
                    # S-1: CRDB 中 api_hash 已加密，需先解密再交给 add_account（add_account 会再次加密存本地）
                    crdb_hash = acc.get('api_hash', '')
                    if crdb_hash:
                        try:
                            api_hash = decrypt(crdb_hash)
                        except RuntimeError:
                            # 兼容旧数据：CRDB 中可能是明文（迁移前写入的），直接用
                            api_hash = crdb_hash
                    else:
                        api_hash = ''
                    await self.add_account(acc['api_id'], api_hash, acc['phone'])
                logger.info(f"[RelayDB] 从 CRDB 恢复 {len(accounts)} 个中继账号")
            except Exception as e:
                logger.warning(f"[RelayDB] CRDB 恢复失败（回退空池模式）: {e}")
        
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
        
        # 双向同步到 CRDB（异步，不阻塞）
        task = asyncio.create_task(_sync_relay_to_crdb(api_id, api_hash, phone))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        
        return cursor.lastrowid

    async def update_account(self, phone: str, api_id: int, api_hash: str):
        await self._db.execute(
            "UPDATE relay_accounts SET api_id=?, api_hash=? WHERE phone=?",
            (api_id, encrypt(api_hash), phone),
        )
        await self._db.commit()
        
        # 双向同步到 CRDB（异步，不阻塞）
        task = asyncio.create_task(_sync_relay_to_crdb(api_id, api_hash, phone))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

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
        cursor = await self._db.execute(
            "SELECT today_requests, total_requests, total_wait_ms, avg_wait_ms, "
            "last_request_at, last_reset_at FROM relay_usage WHERE relay_id=?",
            (relay_id,),
        )
        row = await cursor.fetchone()
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

    # ─── bot_cooldown ──

    async def get_bot_cooldown(self, bot_username: str) -> float:
        """检查机器人是否在冷却期，返回剩余冷却秒数（0 表示不在冷却期）。"""
        cursor = await self._db.execute(
            "SELECT cooldown_seconds, last_decode_at FROM bot_cooldown WHERE bot_username = ?",
            (bot_username.lower(),),
        )
        row = await cursor.fetchone()
        if not row or not row[1]:
            return 0
        try:
            last_at = datetime.fromisoformat(row[1])
        except (ValueError, TypeError):
            return 0
        elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
        return max(0, row[0] - elapsed)

    async def set_bot_cooldown(self, bot_username: str, cooldown_seconds: int):
        """记录机器人的冷却时间（从解码器返回的限速文本中提取）。"""
        now = datetime.now(timezone.utc).isoformat()
        logger.info(f"[RelayDB] 设置 @{bot_username} 冷却 {cooldown_seconds}s")
        await self._db.execute(
            """INSERT INTO bot_cooldown (bot_username, cooldown_seconds, last_decode_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(bot_username) DO UPDATE SET
               cooldown_seconds=excluded.cooldown_seconds,
               last_decode_at=excluded.last_decode_at,
               updated_at=excluded.updated_at""",
            (bot_username.lower(), cooldown_seconds, now, now),
        )
        await self._db.commit()

    async def cleanup_cooldowns(self):
        """清理已过期的冷却记录，防止无用堆积。"""
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """DELETE FROM bot_cooldown
               WHERE last_decode_at IS NOT NULL
               AND datetime(last_decode_at, '+' || cooldown_seconds || ' seconds') < ?""",
            (now,),
        )
        deleted = cursor.rowcount
        await self._db.commit()
        if deleted > 0:
            logger.debug(f"[RelayDB] 清理 {deleted} 条过期 bot_cooldown 记录")


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
