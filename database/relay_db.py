"""中继账号池本地 SQLite 数据库
- 中继账号配置、认证、使用统计全部存储在本地，不占用 CockroachDB Cloud 配额
- VPS 重启后从本地恢复，无需重新登录
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
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


async def _delete_relay_from_crdb(phone: str):
    """异步从 CRDB 删除中继账号(不阻塞主流程)。"""
    try:
        from .session import _client, delete_relay_from_crdb as _do_delete
        if _client._pool is None:
            return  # CRDB 未连接，跳过
        await _do_delete(phone)
    except Exception as e:
        logger.debug(f"[RelayDB] CRDB 删除失败（不影响本地）: {e}")

DDL = """
CREATE TABLE IF NOT EXISTS relay_accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id       BIGINT NOT NULL,
    api_hash     TEXT NOT NULL,
    phone        TEXT NOT NULL UNIQUE,
    is_active    INTEGER DEFAULT 1,
    status       TEXT DEFAULT 'unknown',
    status_info  TEXT,
    auth_code    TEXT,
    relay_user_id BIGINT,
    created_at   TEXT DEFAULT (datetime('now')),
    last_login_at TEXT,
    status_updated_at TEXT
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

-- 外部码映射缓存：本地缓存已映射的码，避免重复查询 CRDB
CREATE TABLE IF NOT EXISTS mapped_codes (
    code        TEXT PRIMARY KEY,
    file_code   TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Bot 覆盖规则：按文件码前缀将解码请求路由到指定 Bot
CREATE TABLE IF NOT EXISTS bot_overrides (
    prefix      TEXT PRIMARY KEY,
    bot_username TEXT NOT NULL,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- M1 业务闭环：中继任务池（持久化中继代发任务，支持崩溃恢复）
-- H6: ACK 语义增强 — 细粒度状态机:
--   RECEIVED → BUFFERED → FORWARDING → FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED → ACKED(已清理)
--   (兼容旧流程: RECEIVED → BUFFERED → FORWARDING → ACKED)
--   任意状态 → FAILED
CREATE TABLE IF NOT EXISTS relay_spool (
    spool_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    relay_account_id  INTEGER NOT NULL,        -- 关联 relay_accounts.id
    code              TEXT NOT NULL,           -- 外部码或内部码
    user_id           BIGINT NOT NULL,
    external_code     TEXT,
    source_msg_ids    TEXT,                    -- JSON: 原消息 ID 列表
    buffered_files    TEXT,                    -- JSON: 临时文件路径列表
    checksum          TEXT,                    -- 文件校验和
    upload_id         TEXT,                    -- H6: Up Bot 返回的上传 ID,用于关联持久化确认
    status            TEXT NOT NULL DEFAULT 'RECEIVED',
    prev_status       TEXT,
    attempts          INTEGER DEFAULT 0,
    ttl_expires_at    REAL,                    -- TTL 过期时间戳
    last_error        TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    acked_at          REAL
);

CREATE INDEX IF NOT EXISTS idx_relay_spool_status ON relay_spool(status, ttl_expires_at);
CREATE INDEX IF NOT EXISTS idx_relay_spool_account ON relay_spool(relay_account_id, status);
CREATE INDEX IF NOT EXISTS idx_relay_spool_code ON relay_spool(code);
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
        await self._db.execute("PRAGMA busy_timeout=15000")  # 多进程并发写,15 秒超时
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
        await self._db.executescript(DDL)
        await self._db.commit()

        # 幂等迁移:为旧表补充新增列(CREATE TABLE IF NOT EXISTS 不会为已存在的表添加列)
        # duplicate column 错误是预期的,表示列已存在,可忽略
        for col_def in (
            "status TEXT DEFAULT 'unknown'",
            "status_info TEXT",
            "status_updated_at TEXT",
            "relay_user_id BIGINT",
        ):
            try:
                await self._db.execute(f"ALTER TABLE relay_accounts ADD COLUMN {col_def}")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    logger.debug(f"[RelayDB] ALTER TABLE 已存在列(幂等,可忽略): {col_def.split()[0]}")
                else:
                    logger.warning(f"[RelayDB] ALTER TABLE 失败(非预期): {e}")
        # mapped_codes 表补充 file_code 列
        try:
            await self._db.execute("ALTER TABLE mapped_codes ADD COLUMN file_code TEXT DEFAULT ''")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                logger.debug("[RelayDB] mapped_codes.file_code 列已存在(幂等,可忽略)")
            else:
                logger.warning(f"[RelayDB] ALTER TABLE mapped_codes 失败(非预期): {e}")
        # H6: relay_spool 表补充 upload_id 列(幂等)
        try:
            await self._db.execute("ALTER TABLE relay_spool ADD COLUMN upload_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                logger.debug("[RelayDB] relay_spool.upload_id 列已存在(幂等,可忽略)")
            else:
                logger.warning(f"[RelayDB] ALTER TABLE relay_spool 失败(非预期): {e}")
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
        try:
            cursor = await self._db.execute(
                "INSERT INTO relay_accounts (api_id, api_hash, phone) VALUES (?, ?, ?)",
                (api_id, encrypt(api_hash), phone),
            )
            await self._db.commit()
        except sqlite3.IntegrityError as e:
            # H-3/R-1: 捕获 UNIQUE 约束冲突(重复手机号),提供友好错误信息
            if "UNIQUE" in str(e) and "phone" in str(e):
                raise RuntimeError(f"手机号 {phone} 已存在(UNIQUE 冲突),请勿重复添加") from e
            raise RuntimeError(f"数据库约束冲突: {e}") from e
        except sqlite3.Error as e:
            # R-1: 捕获其他 sqlite3 异常(如 database is locked),统一转 RuntimeError
            raise RuntimeError(f"数据库写入失败: {e}") from e

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

    async def update_account_status(self, phone: str, status: str, info: str = ""):
        await self._db.execute(
            "UPDATE relay_accounts SET status=?, status_info=?, status_updated_at=datetime('now') WHERE phone=?",
            (status, info, phone),
        )
        await self._db.commit()

    async def update_relay_user_id(self, phone: str, user_id: int):
        """登录成功后记录该中继账号的 Telegram user_id,用于移除时清理白名单"""
        await self._db.execute(
            "UPDATE relay_accounts SET relay_user_id=? WHERE phone=?",
            (user_id, phone),
        )
        await self._db.commit()

    async def get_relay_user_id(self, phone: str) -> int | None:
        """查询中继账号的 Telegram user_id,用于移除时从白名单清除"""
        cursor = await self._db.execute(
            "SELECT relay_user_id FROM relay_accounts WHERE phone=?",
            (phone,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_active_accounts(self) -> list[dict]:
        rows = await self._db.execute_fetchall(
            "SELECT id, api_id, api_hash, phone, is_active, status, status_info, status_updated_at, created_at, last_login_at "
            "FROM relay_accounts WHERE is_active=1 ORDER BY id"
        )
        # P2: 单条解密失败不中断整批,跳过损坏记录并记录日志
        result = []
        for r in rows:
            try:
                api_hash = decrypt(r[2])
            except RuntimeError as e:
                logger.error(f"[RelayDB] 跳过损坏账号 id={r[0]} phone={r[3]}: {e}")
                continue
            result.append({
                "id": r[0],
                "api_id": r[1],
                "api_hash": api_hash,
                "phone": r[3],
                "is_active": bool(r[4]),
                "status": r[5] or 'unknown',
                "status_info": r[6] or '',
                "status_updated_at": r[7],
                "created_at": r[8],
                "last_login_at": r[9],
            })
        return result

    async def get_all_accounts(self) -> list[dict]:
        rows = await self._db.execute_fetchall(
            "SELECT id, api_id, api_hash, phone, is_active, created_at, last_login_at "
            "FROM relay_accounts ORDER BY id"
        )
        # P2: 单条解密失败不中断整批
        result = []
        for r in rows:
            try:
                api_hash = decrypt(r[2])
            except RuntimeError as e:
                logger.error(f"[RelayDB] 跳过损坏账号 id={r[0]} phone={r[3]}: {e}")
                continue
            result.append({
                "id": r[0],
                "api_id": r[1],
                "api_hash": api_hash,
                "phone": r[3],
                "is_active": bool(r[4]),
                "created_at": r[5],
                "last_login_at": r[6],
            })
        return result

    async def remove_account(self, phone: str) -> bool:
        cur = await self._db.execute("DELETE FROM relay_accounts WHERE phone=?", (phone,))
        await self._db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            # 同步删除 CRDB 中的记录,避免重启后从 CRDB 拉回已删除的账号
            task = asyncio.create_task(_delete_relay_from_crdb(phone))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        return deleted

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

    # ── 外部码映射缓存 ──

    async def is_code_mapped(self, code: str) -> bool:
        async with self._db.execute("SELECT 1 FROM mapped_codes WHERE code = ?", (code,)) as cursor:
            return await cursor.fetchone() is not None

    async def mark_code_mapped(self, code: str) -> None:
        await self._db.execute("INSERT OR IGNORE INTO mapped_codes (code) VALUES (?)", (code,))
        await self._db.commit()

    async def unmark_code(self, code: str) -> None:
        await self._db.execute("DELETE FROM mapped_codes WHERE code = ?", (code,))
        await self._db.commit()

    async def get_mapped_file_code(self, code: str) -> str:
        """获取 mapped_codes 中记录的 file_code（idx_bot 处理 pending 后写入）"""
        async with self._db.execute("SELECT file_code FROM mapped_codes WHERE code = ?", (code,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else ""

    async def get_mapped_code_info(self, code: str) -> tuple[str, str]:
        """返回 (file_code, created_at)，用于判断脏标记是否过期"""
        async with self._db.execute("SELECT file_code, created_at FROM mapped_codes WHERE code = ?", (code,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0] or "", row[1] or ""
            return "", ""

    async def update_mapped_file_code(self, code: str, file_code: str) -> None:
        """idx_bot 处理 pending 后更新 file_code，使后续请求可直接从存储频道发送"""
        await self._db.execute(
            "UPDATE mapped_codes SET file_code = ? WHERE code = ?",
            (file_code, code),
        )
        await self._db.commit()

    # ── Bot 覆盖规则 ──

    async def add_bot_override(self, prefix: str, bot_username: str) -> bool:
        await self._db.execute(
            "INSERT OR REPLACE INTO bot_overrides (prefix, bot_username, is_active) VALUES (?, ?, 1)",
            (prefix, bot_username),
        )
        await self._db.commit()
        return True

    async def remove_bot_override(self, prefix: str) -> bool:
        cursor = await self._db.execute("DELETE FROM bot_overrides WHERE prefix = ?", (prefix,))
        deleted = cursor.rowcount
        await self._db.commit()
        return deleted > 0

    async def toggle_bot_override(self, prefix: str) -> bool:
        cursor = await self._db.execute(
            "UPDATE bot_overrides SET is_active = CASE WHEN is_active THEN 0 ELSE 1 END WHERE prefix = ?",
            (prefix,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def list_bot_overrides(self) -> list[dict]:
        rows = await self._db.execute_fetchall(
            "SELECT prefix, bot_username, is_active FROM bot_overrides ORDER BY length(prefix) DESC"
        )
        return [{"prefix": r[0], "bot_username": r[1], "is_active": bool(r[2])} for r in rows]

    async def get_bot_override(self, code: str) -> str | None:
        """按最长前缀匹配返回覆盖的 Bot 用户名，无匹配返回 None"""
        overrides = await self.list_bot_overrides()
        for ov in overrides:
            if ov["is_active"] and code.startswith(ov["prefix"]):
                return ov["bot_username"]
        return None

    # ── relay_spool（M1 业务闭环：中继任务池）──

    async def create_relay_spool(self, relay_account_id: int, code: str, user_id: int,
                                 external_code: str = "", source_msg_ids: list = None,
                                 buffered_files: list = None, checksum: str = "",
                                 ttl_seconds: int = 300) -> int:
        """创建中继任务池记录，status='RECEIVED'，返回 spool_id。

        source_msg_ids / buffered_files 使用 JSON 序列化存储。
        ttl_seconds <= 0 表示不设置 TTL（ttl_expires_at 为 None）。
        """
        now = time.time()
        ttl_expires_at = now + ttl_seconds if ttl_seconds > 0 else None
        source_msg_ids_json = json.dumps(source_msg_ids or [])
        buffered_files_json = json.dumps(buffered_files or [])
        for attempt in range(3):
            try:
                cursor = await self._db.execute(
                    "INSERT INTO relay_spool (relay_account_id, code, user_id, external_code, "
                    "source_msg_ids, buffered_files, checksum, status, ttl_expires_at, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?, ?, ?)",
                    (relay_account_id, code, user_id, external_code,
                     source_msg_ids_json, buffered_files_json, checksum,
                     ttl_expires_at, now, now),
                )
                await self._db.commit()
                return cursor.lastrowid
            except sqlite3.Error as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                raise RuntimeError(f"[RelayDB] 创建 relay_spool 失败: {e}") from e
        return 0  # 不可达，避免类型告警

    async def get_relay_spool(self, spool_id: int) -> dict | None:
        """按主键查询 relay_spool 记录，反序列化 JSON 字段后返回 dict，无记录返回 None。"""
        async with self._db.execute(
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool WHERE spool_id = ?",
            (spool_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "spool_id": row[0],
            "relay_account_id": row[1],
            "code": row[2],
            "user_id": row[3],
            "external_code": row[4] or "",
            "source_msg_ids": json.loads(row[5]) if row[5] else [],
            "buffered_files": json.loads(row[6]) if row[6] else [],
            "checksum": row[7] or "",
            "upload_id": row[8] or "",
            "status": row[9],
            "prev_status": row[10],
            "attempts": row[11],
            "ttl_expires_at": row[12],
            "last_error": row[13],
            "created_at": row[14],
            "updated_at": row[15],
            "acked_at": row[16],
        }

    async def get_active_spool_by_code(self, code: str) -> list[dict]:
        """查询某 code 的活跃中继任务（status NOT IN 终态），用于幂等去重。

        H6: 终态为 ACKED(已清理)/FAILED。INDEXED(已索引,待清理)仍算活跃,
        因为临时文件尚未删除,恢复 worker 需要能查到它以执行清理。
        """
        async with self._db.execute(
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool WHERE code = ? AND status NOT IN ('ACKED', 'FAILED') "
            "ORDER BY created_at",
            (code,),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "spool_id": row[0],
                "relay_account_id": row[1],
                "code": row[2],
                "user_id": row[3],
                "external_code": row[4] or "",
                "source_msg_ids": json.loads(row[5]) if row[5] else [],
                "buffered_files": json.loads(row[6]) if row[6] else [],
                "checksum": row[7] or "",
                "upload_id": row[8] or "",
                "status": row[9],
                "prev_status": row[10],
                "attempts": row[11],
                "ttl_expires_at": row[12],
                "last_error": row[13],
                "created_at": row[14],
                "updated_at": row[15],
                "acked_at": row[16],
            })
        return result

    async def get_pending_spool_by_account(self, relay_account_id: int,
                                           limit: int = 10) -> list[dict]:
        """拉取某中继账号待处理（RECEIVED 且未过期）的任务，用于崩溃恢复。

        ttl_expires_at IS NULL 视为永不过期；ttl_expires_at > now 视为未过期。
        """
        now = time.time()
        async with self._db.execute(
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool WHERE relay_account_id = ? AND status = 'RECEIVED' "
            "AND (ttl_expires_at IS NULL OR ttl_expires_at > ?) "
            "ORDER BY created_at LIMIT ?",
            (relay_account_id, now, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "spool_id": row[0],
                "relay_account_id": row[1],
                "code": row[2],
                "user_id": row[3],
                "external_code": row[4] or "",
                "source_msg_ids": json.loads(row[5]) if row[5] else [],
                "buffered_files": json.loads(row[6]) if row[6] else [],
                "checksum": row[7] or "",
                "upload_id": row[8] or "",
                "status": row[9],
                "prev_status": row[10],
                "attempts": row[11],
                "ttl_expires_at": row[12],
                "last_error": row[13],
                "created_at": row[14],
                "updated_at": row[15],
                "acked_at": row[16],
            })
        return result

    async def transition_spool_status(self, spool_id: int, new_status: str,
                                      reason: str = "", **update_fields) -> bool:
        """原子状态迁移：WHERE spool_id=? AND status != new_status。

        update_fields 中的键会作为额外 UPDATE 字段（如 buffered_files/checksum/acked_at）。
        注意：buffered_files / source_msg_ids 等 list 类型字段由调用方自行 json.dumps 后传入。
        返回 True 表示迁移成功（rowcount > 0），False 表示状态未变化或记录不存在。
        """
        now = time.time()
        # 构造动态 SET 子句（白名单字段，避免 SQL 注入）
        allowed_fields = {
            "external_code", "source_msg_ids", "buffered_files", "checksum",
            "upload_id", "ttl_expires_at", "last_error", "acked_at", "attempts",
        }
        set_parts = [
            "status = ?",
            "prev_status = (SELECT status FROM relay_spool WHERE spool_id = ?)",
            "updated_at = ?",
        ]
        params: list = [new_status, spool_id, now]
        if reason:
            set_parts.append("last_error = ?")
            params.append(reason)
        for field_name, field_value in update_fields.items():
            if field_name not in allowed_fields:
                logger.warning(f"[RelayDB] transition_spool_status 跳过非法字段: {field_name}")
                continue
            set_parts.append(f"{field_name} = ?")
            params.append(field_value)
        params.append(spool_id)
        params.append(new_status)
        sql = (
            "UPDATE relay_spool SET " + ", ".join(set_parts) +
            " WHERE spool_id = ? AND status != ?"
        )
        for attempt in range(3):
            try:
                cursor = await self._db.execute(sql, params)
                await self._db.commit()
                return cursor.rowcount > 0
            except sqlite3.Error as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                raise RuntimeError(f"[RelayDB] 迁移 relay_spool 状态失败: {e}") from e
        return False  # 不可达，避免类型告警

    async def ack_relay_spool(self, spool_id: int):
        """标记任务为 ACKED（Up/Idx 已确认处理完成），记录 acked_at。"""
        now = time.time()
        await self.transition_spool_status(spool_id, "ACKED", acked_at=now)

    async def fail_relay_spool(self, spool_id: int, reason: str, max_attempts: int = 3):
        """累计失败次数；超过 max_attempts 则置为 FAILED，否则保留当前状态允许重试。

        返回更新后的 attempts；若置为 FAILED 会同时写 last_error。
        """
        now = time.time()
        for attempt in range(3):
            try:
                # 先读取当前 attempts（避免竞态下重复 +1）
                async with self._db.execute(
                    "SELECT attempts FROM relay_spool WHERE spool_id = ?",
                    (spool_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    return
                current_attempts = row[0] or 0
                new_attempts = current_attempts + 1
                if new_attempts >= max_attempts:
                    await self._db.execute(
                        "UPDATE relay_spool SET attempts = ?, last_error = ?, "
                        "status = 'FAILED', updated_at = ? WHERE spool_id = ?",
                        (new_attempts, reason, now, spool_id),
                    )
                else:
                    await self._db.execute(
                        "UPDATE relay_spool SET attempts = ?, last_error = ?, "
                        "updated_at = ? WHERE spool_id = ?",
                        (new_attempts, reason, now, spool_id),
                    )
                await self._db.commit()
                return
            except sqlite3.Error as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                raise RuntimeError(f"[RelayDB] fail_relay_spool 失败: {e}") from e

    async def cleanup_expired_spool(self, ttl_seconds: int = 300) -> int:
        """清理 RECEIVED/BUFFERED 状态下 TTL 已过期的任务，置为 FAILED。

        判定条件：status IN ('RECEIVED','BUFFERED') AND ttl_expires_at < now - ttl_seconds
        返回清理数量。
        """
        now = time.time()
        threshold = now - ttl_seconds
        async with self._db.execute(
            "SELECT spool_id FROM relay_spool "
            "WHERE status IN ('RECEIVED', 'BUFFERED') AND ttl_expires_at IS NOT NULL "
            "AND ttl_expires_at < ?",
            (threshold,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return 0
        cleaned = 0
        for (spool_id,) in rows:
            for attempt in range(3):
                try:
                    await self._db.execute(
                        "UPDATE relay_spool SET status = 'FAILED', "
                        "last_error = 'TTL expired', updated_at = ? WHERE spool_id = ?",
                        (now, spool_id),
                    )
                    await self._db.commit()
                    cleaned += 1
                    break
                except sqlite3.Error as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        await asyncio.sleep(0.3)
                        continue
                    logger.error(f"[RelayDB] 清理过期 relay_spool {spool_id} 失败: {e}")
                    break
        if cleaned > 0:
            logger.info(f"[RelayDB] 清理 {cleaned} 条过期 relay_spool 任务")
        return cleaned

    async def get_spool_stats(self) -> dict:
        """返回各状态的任务计数。

        H6: 新增 FORWARDED_TO_UP / UP_DURABLE_ACK / INDEXED 细粒度状态。
        完整状态: RECEIVED / BUFFERED / FORWARDING / FORWARDED_TO_UP / UP_DURABLE_ACK /
                  INDEXED / ACKED / FAILED
        """
        result = {
            "RECEIVED": 0, "BUFFERED": 0, "FORWARDING": 0,
            "FORWARDED_TO_UP": 0, "UP_DURABLE_ACK": 0, "INDEXED": 0,
            "ACKED": 0, "FAILED": 0,
        }
        async with self._db.execute(
            "SELECT status, COUNT(*) FROM relay_spool GROUP BY status",
        ) as cursor:
            rows = await cursor.fetchall()
        for status, count in rows:
            if status in result:
                result[status] = count
            else:
                # 未知状态（理论上不应出现），记录日志但不抛错
                logger.warning(f"[RelayDB] relay_spool 出现未知状态: {status} (count={count})")
        return result

    # ── H6: Relay ACK 语义增强 — 细粒度状态方法 ──

    async def update_spool_status(self, spool_id: int, new_status: str,
                                  **extra) -> bool:
        """H6: 通用状态更新(非原子,供 Up Bot / Idx Bot 直接写状态)。

        与 transition_spool_status 的区别:
        - transition_spool_status: 原子 WHERE status != new_status,适合状态机推进
        - update_spool_status: 无条件更新,适合外部 Bot 强制写状态(如 Up 写 upload_id + UP_DURABLE_ACK)

        支持的 extra 字段(白名单): upload_id, acked_at, last_error, buffered_files,
        source_msg_ids, external_code, checksum, ttl_expires_at, attempts。
        返回 True 表示更新成功。
        """
        now = time.time()
        allowed_fields = {
            "upload_id", "acked_at", "last_error", "buffered_files",
            "source_msg_ids", "external_code", "checksum", "ttl_expires_at", "attempts",
        }
        set_parts = [
            "status = ?",
            "prev_status = (SELECT status FROM relay_spool WHERE spool_id = ?)",
            "updated_at = ?",
        ]
        params: list = [new_status, spool_id, now]
        for field_name, field_value in extra.items():
            if field_name not in allowed_fields:
                logger.warning(f"[RelayDB] update_spool_status 跳过非法字段: {field_name}")
                continue
            set_parts.append(f"{field_name} = ?")
            params.append(field_value)
        params.append(spool_id)
        sql = (
            "UPDATE relay_spool SET " + ", ".join(set_parts) +
            " WHERE spool_id = ?"
        )
        for attempt in range(3):
            try:
                cursor = await self._db.execute(sql, params)
                await self._db.commit()
                return cursor.rowcount > 0
            except sqlite3.Error as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                raise RuntimeError(f"[RelayDB] update_spool_status 失败: {e}") from e
        return False

    async def get_spool_by_upload_id(self, upload_id: str) -> dict | None:
        """H6: 按 Up Bot 返回的 upload_id 查询 spool 记录。

        Up Bot 处理完成后将 upload_id 写入 relay_spool,Idx Bot 可通过 upload_id
        关联查找对应的 spool 进行后续处理。
        """
        if not upload_id:
            return None
        async with self._db.execute(
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool WHERE upload_id = ? ORDER BY created_at LIMIT 1",
            (upload_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "spool_id": row[0],
            "relay_account_id": row[1],
            "code": row[2],
            "user_id": row[3],
            "external_code": row[4] or "",
            "source_msg_ids": json.loads(row[5]) if row[5] else [],
            "buffered_files": json.loads(row[6]) if row[6] else [],
            "checksum": row[7] or "",
            "upload_id": row[8] or "",
            "status": row[9],
            "prev_status": row[10],
            "attempts": row[11],
            "ttl_expires_at": row[12],
            "last_error": row[13],
            "created_at": row[14],
            "updated_at": row[15],
            "acked_at": row[16],
        }

    async def get_unacked_spools(self, timeout_seconds: int = 120,
                                 account_id: int | None = None) -> list[dict]:
        """H6: 查找超时未确认的 spool(FORWARDED_TO_UP / UP_DURABLE_ACK 状态超时)。

        这些 spool 已发送给 Up Bot 但长时间未收到持久化确认(UP_DURABLE_ACK)
        或索引确认(INDEXED),需要恢复 worker 介入:
        - FORWARDED_TO_UP 超时 → 重试发送或告警
        - UP_DURABLE_ACK 超时 → 检查 upload_session 状态

        参数:
        - timeout_seconds: 超时阈值(秒),updated_at 距今超过该值视为超时
        - account_id: 可选,限定某中继账号;None 表示所有账号
        """
        threshold = time.time() - timeout_seconds
        sql = (
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool "
            "WHERE status IN ('FORWARDED_TO_UP', 'UP_DURABLE_ACK') "
            "AND updated_at <= ?"
        )
        params: list = [threshold]
        if account_id is not None:
            sql += " AND relay_account_id = ?"
            params.append(account_id)
        sql += " ORDER BY updated_at"
        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "spool_id": row[0],
                "relay_account_id": row[1],
                "code": row[2],
                "user_id": row[3],
                "external_code": row[4] or "",
                "source_msg_ids": json.loads(row[5]) if row[5] else [],
                "buffered_files": json.loads(row[6]) if row[6] else [],
                "checksum": row[7] or "",
                "upload_id": row[8] or "",
                "status": row[9],
                "prev_status": row[10],
                "attempts": row[11],
                "ttl_expires_at": row[12],
                "last_error": row[13],
                "created_at": row[14],
                "updated_at": row[15],
                "acked_at": row[16],
            })
        return result

    async def get_indexed_spools_for_cleanup(self, account_id: int) -> list[dict]:
        """H6: 查找已 INDEXED 但尚未清理临时文件的 spool(acked_at IS NULL)。

        Idx Bot 处理完成后将状态置为 INDEXED,relay_instance 扫描到此状态后
        可安全删除 buffered_files 中的临时文件,然后设置 acked_at 标记清理完成。
        """
        async with self._db.execute(
            "SELECT spool_id, relay_account_id, code, user_id, external_code, "
            "source_msg_ids, buffered_files, checksum, upload_id, status, prev_status, "
            "attempts, ttl_expires_at, last_error, created_at, updated_at, acked_at "
            "FROM relay_spool "
            "WHERE relay_account_id = ? AND status = 'INDEXED' AND acked_at IS NULL "
            "ORDER BY updated_at",
            (account_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "spool_id": row[0],
                "relay_account_id": row[1],
                "code": row[2],
                "user_id": row[3],
                "external_code": row[4] or "",
                "source_msg_ids": json.loads(row[5]) if row[5] else [],
                "buffered_files": json.loads(row[6]) if row[6] else [],
                "checksum": row[7] or "",
                "upload_id": row[8] or "",
                "status": row[9],
                "prev_status": row[10],
                "attempts": row[11],
                "ttl_expires_at": row[12],
                "last_error": row[13],
                "created_at": row[14],
                "updated_at": row[15],
                "acked_at": row[16],
            })
        return result


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
