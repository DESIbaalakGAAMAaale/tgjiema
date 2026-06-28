try:
    import orjson as json
except ImportError:
    import json
import asyncio
import time as _time
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import asyncpg
from loguru import logger


def _json_dumps(obj, **kwargs):
    """json.dumps compatible wrapper."""
    result = json.dumps(obj, **kwargs)
    if isinstance(result, bytes):
        return result.decode()
    return result

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        membership_level TEXT DEFAULT 'free',
        daily_decode_quota INTEGER DEFAULT 3,
        quota_used_today INTEGER DEFAULT 0,
        quota_date TEXT,
        can_upload INTEGER DEFAULT 0,
        external_decode_quota INTEGER DEFAULT 0,
        external_used_today INTEGER DEFAULT 0,
        external_quota_date TEXT,
        is_banned INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS file_records (
        file_code TEXT PRIMARY KEY,
        uploader_id BIGINT,
        primary_channel_id BIGINT,
        primary_channel_msg_id BIGINT,
        file_types TEXT,
        backup_channel_msg_ids TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        file_ids TEXT,
        status TEXT DEFAULT 'active',
        request_count INTEGER DEFAULT 0,
        create_time TEXT,
        expire_time TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS decode_logs (
        id SERIAL PRIMARY KEY,
        file_code TEXT,
        requester_id BIGINT,
        request_time TEXT,
        status TEXT DEFAULT 'queued',
        source_channel_id BIGINT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_users_first_name ON users(first_name)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_status ON file_records(status)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_uploader ON file_records(uploader_id)",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_file_code ON decode_logs(file_code)",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_requester ON decode_logs(requester_id)",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_request_time ON decode_logs(request_time)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_msg_id ON file_records(primary_channel_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_send_queue_processed_created ON send_queue(processed, created_at)",  # 已废弃(v2 用 jobs 表)
    """CREATE TABLE IF NOT EXISTS pending_uploads (
        id SERIAL PRIMARY KEY,
        uploader_id BIGINT,
        primary_channel_id BIGINT,
        primary_channel_msg_id BIGINT,
        file_types TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        status_msg_id BIGINT,
        created_at TEXT,
        processed INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pending_uploads_unprocessed ON pending_uploads(processed)",
    # 已废弃(v2 环形冗余架构用 jobs 表替代 send_queue)
    """CREATE TABLE IF NOT EXISTS send_queue (
        id SERIAL PRIMARY KEY,
        target_user_id BIGINT,
        channel_id BIGINT,
        message_id BIGINT,
        file_code TEXT,
        task_type TEXT DEFAULT 'single',
        channel_msg_ids TEXT,
        batch_file_meta TEXT,
        created_at TEXT,
        processed INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS backup_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS message_backups (
        main_msg_id BIGINT,
        backup_channel_id BIGINT,
        backed_msg_id BIGINT,
        backed_at TEXT,
        PRIMARY KEY (main_msg_id, backup_channel_id)
    )""",
    # ─── 环形冗余架构新表 ──────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS cells (
        slot_id TEXT PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'shadow1',
        next_active_chat_id BIGINT,
        prev_slot_id TEXT,
        account_name TEXT DEFAULT '',
        is_r100 INTEGER DEFAULT 0,
        last_heartbeat TEXT,
        last_synced_msg_id BIGINT DEFAULT 0,
        degrade_count INTEGER DEFAULT 0,
        file_count INTEGER DEFAULT 0,
        rotation_started_at TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        file_record_code TEXT,
        uploader_id BIGINT,
        file_types TEXT,
        batch_msg_ids TEXT,
        batch_file_meta TEXT,
        primary_channel_id BIGINT,
        status TEXT DEFAULT 'active',
        created_at TEXT,
        expire_time TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id SERIAL PRIMARY KEY,
        code TEXT NOT NULL,
        target_user_id BIGINT NOT NULL,
        storage_channel_id BIGINT,
        storage_msg_ids TEXT,
        batch_file_meta TEXT,
        task_type TEXT DEFAULT 'single',
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        dispatched_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rotate_log (
        id SERIAL PRIMARY KEY,
        timestamp TEXT NOT NULL,
        from_slot_id TEXT,
        to_slot_id TEXT,
        from_status TEXT,
        to_status TEXT,
        reason TEXT,
        triggered_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cells_channel ON cells(channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_cells_status ON cells(status)",
    "CREATE INDEX IF NOT EXISTS idx_cells_next_active ON cells(next_active_chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rotate_log_timestamp ON rotate_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_codes_expire_time ON codes(expire_time)",
    "CREATE INDEX IF NOT EXISTS idx_codes_status ON codes(status)",
    "CREATE INDEX IF NOT EXISTS idx_codes_file_record_code ON codes(file_record_code)",
    # ─── 备用池 + 轮转配置 ──────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS spare_pool (
        channel_id BIGINT PRIMARY KEY,
        account_name TEXT,
        is_used INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_spare_pool_account ON spare_pool(account_name)",
    "CREATE INDEX IF NOT EXISTS idx_spare_pool_used ON spare_pool(is_used)",
    """CREATE TABLE IF NOT EXISTS rotation_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT,
        updated_at TEXT
    )""",
    # ─── 中继账号池（云端备份，与 SQLite relay_pool.db 双向同步）─────────────
    """CREATE TABLE IF NOT EXISTS relay_accounts (
        id           SERIAL PRIMARY KEY,
        api_id       BIGINT NOT NULL,
        api_hash     TEXT NOT NULL,
        phone        TEXT NOT NULL UNIQUE,
        is_active    INTEGER DEFAULT 1,
        created_at   TEXT DEFAULT (datetime('now')),
        last_login_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_relay_accounts_phone ON relay_accounts(phone)",
    # ─── 外部码映射表（采集器写入，idx_bot 查询）─────────────────
    """CREATE TABLE IF NOT EXISTS external_code_mapping (
        external_code TEXT PRIMARY KEY,
        system_code TEXT NOT NULL,
        bot_username TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_external_code_mapping_system ON external_code_mapping(system_code)",
    "CREATE INDEX IF NOT EXISTS idx_external_code_mapping_bot ON external_code_mapping(bot_username)",
    # ─── code_bot_mapping 表(代码前缀 → Bot 路由──────────────
    """CREATE TABLE IF NOT EXISTS code_bot_mapping (
        code_prefix TEXT PRIMARY KEY,
        bot_username TEXT NOT NULL,
        created_at TEXT DEFAULT ''
    )""",
]

MIGRATION_STATEMENTS = [
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS batch_msg_ids TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS file_records ADD COLUMN IF NOT EXISTS file_ids TEXT",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS pending_uploads ADD COLUMN IF NOT EXISTS status_msg_id BIGINT",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS task_type TEXT DEFAULT 'single'",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS channel_msg_ids TEXT",
    "ALTER TABLE IF EXISTS send_queue ADD COLUMN IF NOT EXISTS batch_file_meta TEXT",
    "ALTER TABLE IF EXISTS codes ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''",
    # ─── jobs 表补充───────────────────────────────────────────────
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE",
    # ─── CRDB 行级 TTL：零 RU 自动清理，替代 Python cleanup ───
    "ALTER TABLE decode_logs SET (ttl_expiration_expression = 'CAST(request_time AS TIMESTAMPTZ) + INTERVAL ''7 days''', ttl_job_cron = '@hourly')",
    "ALTER TABLE jobs SET (ttl_expiration_expression = 'CAST(created_at AS TIMESTAMPTZ) + INTERVAL ''7 days''', ttl_job_cron = '@hourly')",
    # ─── 死信队列(Dead Letter Queue)──────────────────────────────
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_reason TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry_count INTEGER DEFAULT 0",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry BOOLEAN DEFAULT FALSE",
    "ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS dead_retry_at TEXT DEFAULT ''",
]


class UpdateResult:
    def __init__(self, matched_count: int = 0):
        self.matched_count = matched_count


class CockroachDBClient:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._url: str = ""

    def configure(self, url: str):
        self._url = url

    async def connect(self):
        self._pool = await asyncpg.create_pool(
            self._url,
            min_size=1,
            max_size=5,
            statement_cache_size=256,
        )

        # ─── SQLite 缓存备份：初始化并恢复内存缓存───
        from .cache_store import get_cache_store
        from .cache import load_cache_from_disk

        store = get_cache_store()
        await store.init()
        await load_cache_from_disk()

        for sql in DDL_STATEMENTS:
            await self.execute(sql)
        for sql in MIGRATION_STATEMENTS:
            try:
                await self.execute(sql)
            except Exception as e:
                logger.warning(f"[DB] 迁移 SQL 执行失败（可忽略）：{e}")

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, params: list = None):
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *(params or []))

    @asynccontextmanager
    async def transaction(self):
        """获取一个带事务的连接,用于需要原子性的多步操作        
        用法:
            async with db.transaction() as conn:
                await conn.execute(...)
                await conn.execute(...)
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn


_client: CockroachDBClient = CockroachDBClient()


async def init_db():
    from config import settings as _settings
    _client.configure(_settings.COCKROACHDB_URL)
    await _client.connect()


async def close_db():
    await _client.close()


def _row_to_dict(record) -> dict:
    result = {}
    for col in record.keys():
        val = record[col]
        if col in (
            "user_id", "primary_channel_id", "primary_channel_msg_id",
            "requester_id", "source_channel_id", "request_count", "id",
            "daily_decode_quota", "quota_used_today", "uploader_id",
            "external_decode_quota", "external_used_today", "target_user_id",
            "channel_id", "message_id", "cnt", "status_msg_id",
        ):
            result[col] = int(val) if val is not None else 0
        elif col in ("can_upload", "is_banned"):
            result[col] = bool(val)
        elif col in ("file_types", "backup_channel_msg_ids", "batch_file_meta"):
            if val is None or val == "":
                result[col] = ""
            else:
                try:
                    result[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    result[col] = val
        elif col == "batch_msg_ids":
            result[col] = str(val) if val else ""
        elif col in (
            "created_at", "updated_at", "create_time", "request_time",
            "expire_time", "quota_date", "external_quota_date",
        ):
            if val:
                try:
                    result[col] = datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    result[col] = val
            else:
                result[col] = None
        elif col == "processed":
            result[col] = int(val) if val is not None else 0
        else:
            result[col] = val
    return result


def _safe_str(val: Any):
    if val is None:
        return None
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, int):
        return val
    if isinstance(val, (list, dict)):
        return _json_dumps(val, default=str)
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


class D1Collection:
    def __init__(self, table: str):
        self.table = table

    async def _query(self, sql: str, params: list = None) -> list[dict]:
        async with _client._pool.acquire() as conn:
            records = await conn.fetch(sql, *(params or []))
            return [_row_to_dict(r) for r in records]

    async def _execute(self, sql: str, params: list = None) -> int:
        async with _client._pool.acquire() as conn:
            await conn.execute(sql, *(params or []))
            return 1

    async def find_one(self, query: dict) -> Optional[dict]:
        params = []
        where_parts = []
        for k, v in query.items():
            if isinstance(v, dict):
                for op, val in v.items():
                    if op == "$gte":
                        where_parts.append(f"{k} >= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lte":
                        where_parts.append(f"{k} <= ${len(params) + 1}")
                        params.append(val)
                    elif op == "$gt":
                        where_parts.append(f"{k} > ${len(params) + 1}")
                        params.append(val)
                    elif op == "$lt":
                        where_parts.append(f"{k} < ${len(params) + 1}")
                        params.append(val)
                    elif op == "$ne":
                        where_parts.append(f"{k} != ${len(params) + 1}")
                        params.append(val)
                    elif op == "$in":
                        placeholders = [f"${len(params) + j + 1}" for j in range(len(val))]
                        params.extend(val)
                        where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(v)
        sql = f"SELECT * FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        sql += " LIMIT 1"
        rows = await self._query(sql, params)
        return rows[0] if rows else None

    async def insert_one(self, doc: dict) -> dict:
        keys = list(doc.keys())
        params = [_safe_str(v) for v in doc.values()]
        placeholders = [f"${i + 1}" for i in range(len(params))]
        sql = f"INSERT INTO {self.table} ({', '.join(keys)}) VALUES ({', '.join(placeholders)})"
        await self._execute(sql, params)
        return doc

    async def insert_many(self, docs: list[dict]) -> list[dict]:
        """批量插入文档"""
        if not docs:
            return []
        keys = list(docs[0].keys())
        placeholders_list = []
        all_params = []
        for doc in docs:
            row_params = [_safe_str(v) for v in doc.values()]
            all_params.extend(row_params)
            start = len(all_params) - len(row_params) + 1
            placeholders_list.append(
                "(" + ", ".join([f"${start + i}" for i in range(len(row_params))]) + ")"
            )
        sql = f"INSERT INTO {self.table} ({', '.join(keys)}) VALUES {', '.join(placeholders_list)}"
        await self._execute(sql, all_params)
        return docs

    async def update_one(self, query: dict, update: dict) -> UpdateResult:
        all_params = []

        set_parts = []
        if "$set" in update:
            for k, v in update["$set"].items():
                set_parts.append(f"{k} = ${len(all_params) + 1}")
                all_params.append(_safe_str(v))
        if "$inc" in update:
            for k, v in update["$inc"].items():
                set_parts.append(f"{k} = {k} + ${len(all_params) + 1}")
                all_params.append(int(v))
        if "$push" in update:
            for k, v in update["$push"].items():
                val_json = _json_dumps(v, default=str)
                set_parts.append(
                    f"{k} = CASE WHEN {k} IS NULL OR {k} = '' "
                    f"THEN jsonb_build_array(${len(all_params) + 1}::jsonb) "
                    f"ELSE {k}::jsonb || jsonb_build_array(${len(all_params) + 2}::jsonb) END"
                )
                all_params.append(val_json)
                all_params.append(val_json)

        if not set_parts:
            return UpdateResult(0)

        where_parts = []
        for k, v in query.items():
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ${len(all_params) + 1}")
            all_params.append(v)

        sql = f"UPDATE {self.table} SET {', '.join(set_parts)}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        await self._execute(sql, all_params)
        return UpdateResult(1)

    async def delete_one(self, query: dict) -> bool:
        params = []
        where_parts = []
        for k, v in query.items():
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(v)
        sql = f"DELETE FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        sql += " LIMIT 1"
        await self._execute(sql, params)
        return True

    async def count_documents(self, query: dict) -> int:
        params = []
        where_parts = []
        for k, v in query.items():
            if k == "$or":
                continue
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ${len(params) + 1}")
                    params.append(_safe_str(v["$gte"]))
                elif "$lte" in v:
                    where_parts.append(f"{k} <= ${len(params) + 1}")
                    params.append(_safe_str(v["$lte"]))
                elif "$gt" in v:
                    where_parts.append(f"{k} > ${len(params) + 1}")
                    params.append(_safe_str(v["$gt"]))
                elif "$lt" in v:
                    where_parts.append(f"{k} < ${len(params) + 1}")
                    params.append(_safe_str(v["$lt"]))
                elif "$ne" in v:
                    where_parts.append(f"{k} != ${len(params) + 1}")
                    params.append(_safe_str(v["$ne"]))
                elif "$in" in v:
                    placeholders = [f"${len(params) + j + 1}" for j in range(len(v["$in"]))]
                    params.extend([_safe_str(x) for x in v["$in"]])
                    where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ${len(params) + 1}")
                    params.append(f"%{_safe_str(v['$regex'])}%")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                for sk, sv in sub_q.items():
                    if isinstance(sv, dict) and "$regex" in sv:
                        or_parts.append(f"{sk} LIKE ${len(params) + 1}")
                        params.append(f"%{_safe_str(sv['$regex'])}%")
            if or_parts:
                where_parts.append("(" + " OR ".join(or_parts) + ")")

        sql = f"SELECT COUNT(*) as cnt FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        rows = await self._query(sql, params)
        return rows[0]["cnt"] if rows else 0

    async def find(
        self, query: dict = None, sort: tuple = None,
        skip: int = 0, limit: int = None,
    ) -> list[dict]:
        query = query or {}
        params = []
        where_parts = []
        for k, v in query.items():
            if k == "$or":
                continue
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ${len(params) + 1}")
                    params.append(_safe_str(v["$gte"]))
                elif "$lte" in v:
                    where_parts.append(f"{k} <= ${len(params) + 1}")
                    params.append(_safe_str(v["$lte"]))
                elif "$gt" in v:
                    where_parts.append(f"{k} > ${len(params) + 1}")
                    params.append(_safe_str(v["$gt"]))
                elif "$lt" in v:
                    where_parts.append(f"{k} < ${len(params) + 1}")
                    params.append(_safe_str(v["$lt"]))
                elif "$ne" in v:
                    where_parts.append(f"{k} != ${len(params) + 1}")
                    params.append(_safe_str(v["$ne"]))
                elif "$in" in v:
                    placeholders = [f"${len(params) + j + 1}" for j in range(len(v["$in"]))]
                    params.extend([_safe_str(x) for x in v["$in"]])
                    where_parts.append(f"{k} IN ({', '.join(placeholders)})")
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ${len(params) + 1}")
                    params.append(f"%{_safe_str(v['$regex'])}%")
                continue
            where_parts.append(f"{k} = ${len(params) + 1}")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                or_clause = []
                for sk, sv in sub_q.items():
                    if isinstance(sv, dict) and "$regex" in sv:
                        or_clause.append(f"{sk} LIKE ${len(params) + 1}")
                        params.append(f"%{_safe_str(sv['$regex'])}%")
                if or_clause:
                    or_parts.append("(" + " OR ".join(or_clause) + ")")
            if or_parts:
                where_parts.append("(" + " OR ".join(or_parts) + ")")

        sql = f"SELECT * FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if sort and len(sort) >= 2:
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', sort[0]):
                raise ValueError(f"Invalid sort column: {sort[0]}")
            direction = "DESC" if sort[1] < 0 else "ASC"
            sql += f" ORDER BY {sort[0]} {direction}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if skip:
            sql += f" OFFSET {int(skip)}"
        return await self._query(sql, params)


_users_col = D1Collection("users")
_file_records_col = D1Collection("file_records")
_decode_logs_col = D1Collection("decode_logs")
_pending_uploads_col = D1Collection("pending_uploads")
_send_queue_col = D1Collection("send_queue")
_backup_config_col = D1Collection("backup_config")
_code_bot_mapping_col = D1Collection("code_bot_mapping")
_message_backups_col = D1Collection("message_backups")
_cells_col = D1Collection("cells")
_codes_col = D1Collection("codes")
_jobs_col = D1Collection("jobs")
_rotate_log_col = D1Collection("rotate_log")
_spare_pool_col = D1Collection("spare_pool")
_rotation_config_col = D1Collection("rotation_config")
_external_code_mapping_col = D1Collection("external_code_mapping")


def get_users_col() -> D1Collection:
    return _users_col


def get_file_records_col() -> D1Collection:
    return _file_records_col


def get_decode_logs_col() -> D1Collection:
    return _decode_logs_col


def get_pending_uploads_col() -> D1Collection:
    return _pending_uploads_col


def get_send_queue_col() -> D1Collection:
    return _send_queue_col


def get_backup_config_col() -> D1Collection:
    return _backup_config_col


def get_code_bot_mapping_col() -> D1Collection:
    return _code_bot_mapping_col


async def save_code_bot_mapping(code: str, bot_username: str):
    col = get_code_bot_mapping_col()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    existing = await col.find_one({"code_prefix": code})
    if existing:
        await col.update_one(
            {"code_prefix": code},
            {"$set": {"bot_username": bot_username, "created_at": now}},
        )
    else:
        await col.insert_one({"code_prefix": code, "bot_username": bot_username, "created_at": now})


async def get_bot_for_code(code: str) -> str:
    col = get_code_bot_mapping_col()
    row = await col.find_one({"code_prefix": code})
    if row:
        return row.get("bot_username", "")
    return ""


async def _get_config(key: str) -> Optional[str]:
    row = await _backup_config_col.find_one({"config_key": key})
    return row.get("config_value") if row else None


async def _set_config(key: str, value: str):
    import datetime as _dt
    existing = await _backup_config_col.find_one({"config_key": key})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": key},
            {"$set": {"config_value": value, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}},
        )
    else:
        await _backup_config_col.insert_one({
            "config_key": key,
            "config_value": value,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


async def get_backup_channels(group: int) -> list[int]:
    val = await _get_config(f"backup_channels_{group}")
    if val is None:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


async def set_backup_channels(group: int, channels: list[int]):
    await _set_config(f"backup_channels_{group}", _json_dumps(channels))


async def get_all_backup_channels() -> list[int]:
    result = []
    for g in (1, 2, 3):
        result.extend(await get_backup_channels(g))
    return result


async def get_backup_bot_tokens() -> dict[str, str]:
    tokens = {}
    for i in (1, 2, 3):
        val = await _get_config(f"backup_bot_{i}_token")
        if val:
            tokens[str(i)] = val
    return tokens


async def set_backup_bot_token(bot_num: int, token: str):
    await _set_config(f"backup_bot_{bot_num}_token", token)


async def delete_backup_bot_token(bot_num: int):
    existing = await _backup_config_col.find_one({"config_key": f"backup_bot_{bot_num}_token"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"backup_bot_{bot_num}_token"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )


async def get_config(key: str) -> str | None:
    """获取配置值,走内存缓存(10分钟 TTL"""
    return await get_config_cached(key)


async def set_config(key: str, value: str):
    await _set_config(key, value)


async def delete_config(key: str):
    existing = await _backup_config_col.find_one({"config_key": key})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": key},
            {"$set": {"config_value": "", "updated_at": ""}},
        )


async def get_relay_config() -> dict:
    api_id = await _get_config("relay_api_id")
    api_hash = await _get_config("relay_api_hash")
    phone = await _get_config("relay_phone")
    return {
        "api_id": int(api_id) if api_id else 0,
        "api_hash": api_hash or "",
        "phone": phone or "",
    }


async def set_relay_config(api_id: int, api_hash: str, phone: str):
    if api_id:
        await _set_config("relay_api_id", str(api_id))
    if api_hash:
        await _set_config("relay_api_hash", api_hash)
    if phone:
        await _set_config("relay_phone", phone)


async def get_relay_status() -> str:
    return await _get_config("relay_status") or "offline"


# ─── 文件码前缀 → Bot 路由 ──────────────────────────────────────

# 内存缓存(10 分钟 TTL)
_code_bot_routes_cache: dict[str, str] = {}
_code_bot_routes_cache_ts: float = 0.0
_bot_decode_interval_cache: dict[str, int] = {}
_bot_decode_interval_cache_ts: float = 0.0
_BOT_CONFIG_TTL: int = 600  # 10 分钟


async def _refresh_bot_config_cache():
    """DB 刷新 code_bot_route bot_decode_interval 缓存"""
    global _code_bot_routes_cache, _code_bot_routes_cache_ts
    global _bot_decode_interval_cache, _bot_decode_interval_cache_ts
    from loguru import logger

    try:
        # 刷新 code_bot_route 缓存
        routes = {}
        all_routes = await _backup_config_col.find({"config_key": {"$regex": "^code_bot_route:"}})
        for row in all_routes:
            key = row.get("config_key", "")
            val = row.get("config_value", "")
            prefix = key.replace("code_bot_route:", "")
            if prefix and val:
                routes[prefix] = val
        _code_bot_routes_cache = routes
        _code_bot_routes_cache_ts = _time.time()

        # 刷新 bot_decode_interval 缓存
        intervals = {}
        all_intervals = await _backup_config_col.find({"config_key": {"$regex": "^bot_decode_interval:"}})
        for row in all_intervals:
            key = row.get("config_key", "")
            val = row.get("config_value", "")
            bot = key.replace("bot_decode_interval:", "")
            if bot and val:
                try:
                    intervals[bot] = int(val)
                except ValueError:
                    pass
        _bot_decode_interval_cache = intervals
        _bot_decode_interval_cache_ts = _time.time()

        logger.debug(f"[ConfigCache] 已刷新 {len(routes)} routes, {len(intervals)} intervals")
    except Exception as e:
        logger.warning(f"[ConfigCache] 刷新失败: {e}")


async def set_code_bot_route(prefix: str, bot_username: str):
    await _set_config(f"code_bot_route:{prefix}", bot_username)
    _code_bot_routes_cache[prefix] = bot_username


async def get_code_bot_route(prefix: str) -> str | None:
    return await _get_config(f"code_bot_route:{prefix}")


async def delete_code_bot_route(prefix: str):
    existing = await _backup_config_col.find_one({"config_key": f"code_bot_route:{prefix}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"code_bot_route:{prefix}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )
    _code_bot_routes_cache.pop(prefix, None)


async def get_all_code_bot_routes() -> dict[str, str]:
    """获取所code_bot_route,走内存缓存0 分钟 TTL)"""
    global _code_bot_routes_cache, _code_bot_routes_cache_ts
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    return _code_bot_routes_cache


async def resolve_bot_for_code(code: str, default_bot: str) -> str:
    """根据文件码前缀匹配配置的路由，无匹配则返回 default_bot。"""
    global _code_bot_routes_cache, _code_bot_routes_cache_ts
    if _time.time() - _code_bot_routes_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    best_prefix = ""
    best_bot = ""
    for prefix, bot_username in _code_bot_routes_cache.items():
        if code.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_bot = bot_username
    return best_bot or default_bot


# ─── Bot 解码间隔限流 ────────────────────────────────────────────


async def set_bot_decode_interval(bot_username: str, interval_seconds: int):
    await _set_config(f"bot_decode_interval:{bot_username}", str(interval_seconds))
    # 更新内存缓存
    _bot_decode_interval_cache[bot_username] = interval_seconds


async def get_bot_decode_interval(bot_username: str) -> int:
    """获取 bot 解码间隔,走内存缓存0 分钟 TTL)"""
    global _bot_decode_interval_cache, _bot_decode_interval_cache_ts
    if _time.time() - _bot_decode_interval_cache_ts > _BOT_CONFIG_TTL:
        await _refresh_bot_config_cache()
    return _bot_decode_interval_cache.get(bot_username, 0)


async def delete_bot_decode_interval(bot_username: str):
    existing = await _backup_config_col.find_one({"config_key": f"bot_decode_interval:{bot_username}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"bot_decode_interval:{bot_username}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )
    _bot_decode_interval_cache.pop(bot_username, None)


async def get_all_bot_decode_intervals() -> dict[str, int]:
    col = _backup_config_col
    rows = await col.find({})
    result = {}
    for row in rows:
        key = row.get("config_key", "")
        val = row.get("config_value", "")
        if key.startswith("bot_decode_interval:") and val and val.isdigit():
            bot = key[len("bot_decode_interval:"):]
            result[bot] = int(val)
    return result


def get_message_backups_col() -> D1Collection:
    return _message_backups_col


async def save_message_backup(main_msg_id: int, backup_channel_id: int, backed_msg_id: int):
    import datetime as _dt
    col = get_message_backups_col()
    existing = await col.find_one({
        "main_msg_id": main_msg_id,
        "backup_channel_id": backup_channel_id,
    })
    if existing:
        await col.update_one(
            {"main_msg_id": main_msg_id, "backup_channel_id": backup_channel_id},
            {"$set": {
                "backed_msg_id": backed_msg_id,
                "backed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }},
        )
    else:
        await col.insert_one({
            "main_msg_id": main_msg_id,
            "backup_channel_id": backup_channel_id,
            "backed_msg_id": backed_msg_id,
            "backed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


async def get_message_backups(main_msg_id: int) -> list[dict]:
    col = get_message_backups_col()
    return await col.find({"main_msg_id": main_msg_id})


async def get_all_message_backups() -> list[dict]:
    col = get_message_backups_col()
    return await col.find({})


# ─── 缓存查询──────────────────────────────────────────────────
from .cache import get_user_cache, get_file_record_cache, get_config_cache


# ─── 外部码映射查询────────────────────────────────────────────

def get_external_code_mapping_col() -> D1Collection:
    return _external_code_mapping_col


# ─── 外部码映射内存缓存(启动加载,60 秒刷新)─────────────────────
_external_code_mapping_cache: dict[str, str] = {}
_external_code_mapping_cache_ts = 0
_EXTERNAL_CODE_MAPPING_TTL = 60  # 60 秒(缩短以快速响应管理员修改)


async def _refresh_external_code_mapping_cache():
    """刷新外部码映射内存缓存"""
    global _external_code_mapping_cache, _external_code_mapping_cache_ts
    try:
        rows = await _external_code_mapping_col.find({})
        _external_code_mapping_cache = {
            row["external_code"]: row.get("system_code", "")
            for row in rows
            if row.get("system_code")
        }
        _external_code_mapping_cache_ts = _time.time()
    except Exception as e:
        logger.warning(f"[DB] refresh external_code_mapping cache failed: {e}")


async def get_system_code_for_external(external_code: str) -> str | None:
    """查询外部码对应的系统码,命中idx_bot 可直接走本地解码流程"""
    global _external_code_mapping_cache, _external_code_mapping_cache_ts
    # 检查缓存是否过期
    if _time.time() - _external_code_mapping_cache_ts > _EXTERNAL_CODE_MAPPING_TTL:
        await _refresh_external_code_mapping_cache()
    # 先查内存缓存
    system_code = _external_code_mapping_cache.get(external_code)
    if system_code:
        return system_code
    # 缓存未命中，回退到 DB 查询。
    try:
        row = await _external_code_mapping_col.find_one({"external_code": external_code})
        if row:
            sc = row.get("system_code")
            if sc:
                _external_code_mapping_cache[external_code] = sc
                return sc
    except Exception as e:
        logger.warning(f"[DB] get_system_code_for_external failed ({external_code}): {e}")
    return None


async def set_external_code_mapping(
    external_code: str,
    system_code: str,
    bot_username: str = "",
) -> bool:
    """写入外部码→系统码映射(由采集器调用)"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        existing = await _external_code_mapping_col.find_one({"external_code": external_code})
        if existing:
            await _external_code_mapping_col.update_one(
                {"external_code": external_code},
                {"$set": {"system_code": system_code, "bot_username": bot_username, "updated_at": now}},
            )
        else:
            await _external_code_mapping_col.insert_one({
                "external_code": external_code,
                "system_code": system_code,
                "bot_username": bot_username,
                "created_at": now,
                "updated_at": now,
            })
        return True
    except Exception as e:
        logger.error(f"[DB] set_external_code_mapping failed ({external_code}): {e}")
        return False


async def get_user_cached(user_id: int) -> Optional[dict]:
    cache = get_user_cache()
    cache_key = f"user:{user_id}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # L2: SQLite 兜底(避免穿 CRDB)
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)  # promote 到 L1
        return cached

    # CRDB
    col = get_users_col()
    user = await col.find_one({"user_id": user_id})

    if user:
        cache.set(cache_key, user)
        await store.set(cache_key, user)  # 写穿透到 L2

    return user


async def update_user_and_invalidate(user_id: int, update: dict):
    col = get_users_col()
    await col.update_one({"user_id": user_id}, update)

    cache = get_user_cache()
    cache.invalidate(f"user:{user_id}")


async def get_file_record_cached(file_code: str) -> Optional[dict]:
    cache = get_file_record_cache()
    cache_key = f"file:{file_code}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # L2: SQLite 兜底
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)
        return cached

    # CRDB
    col = get_file_records_col()
    record = await col.find_one({"file_code": file_code})

    if record:
        cache.set(cache_key, record)
        await store.set(cache_key, record)

    return record


async def update_file_record_and_invalidate(file_code: str, update: dict):
    col = get_file_records_col()
    await col.update_one({"file_code": file_code}, update)

    cache = get_file_record_cache()
    cache.invalidate(f"file:{file_code}")


async def get_config_cached(key: str) -> Optional[str]:
    cache = get_config_cache()
    cache_key = f"config:{key}"

    # L1: 内存缓存
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # L2: SQLite 兜底
    from .cache_store import get_cache_store
    store = get_cache_store()
    cached = await store.get(cache_key)
    if cached is not None:
        cache.set(cache_key, cached)
        return cached

    # CRDB
    val = await _get_config(key)
    cache.set(cache_key, val)
    await store.set(cache_key, val)
    return val


async def set_config_and_invalidate(key: str, value: str):
    await _set_config(key, value)
    cache = get_config_cache()
    cache.invalidate(f"config:{key}")


# ─── 环形冗余架构 新表操作 ──────────────────────────────────────


def get_cells_col() -> D1Collection:
    return _cells_col


def get_codes_col() -> D1Collection:
    return _codes_col


def get_jobs_col() -> D1Collection:
    return _jobs_col


def get_rotate_log_col() -> D1Collection:
    return _rotate_log_col


def get_spare_pool_col() -> D1Collection:
    return _spare_pool_col


def get_rotation_config_col() -> D1Collection:
    return _rotation_config_col


# ─── 备用池操作──────────────────────────────────────────────────

async def add_spare_channel(channel_id: int, account_name: str = None) -> bool:
    """添加备用频道到备用池"""
    import datetime as _dt
    col = get_spare_pool_col()
    existing = await col.find_one({"channel_id": channel_id})
    if existing:
        await col.update_one(
            {"channel_id": channel_id},
            {"$set": {"account_name": account_name, "is_used": 0}},
        )
    else:
        await col.insert_one({
            "channel_id": channel_id,
            "account_name": account_name,
            "is_used": 0,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
    return True


async def get_spare_for_account(account_name: str) -> dict | None:
    """获取指定账号的未使用备用频道(优先同账号)"""
    col = get_spare_pool_col()
    # 优先匹配同账号
    spare = await col.find_one({"account_name": account_name, "is_used": 0})
    if spare:
        return spare
    return None


async def get_any_spare() -> dict | None:
    """获取任意未使用的备用频道(无账号归属)"""
    col = get_spare_pool_col()
    # 优先无归属的备用池频道
    spare = await col.find_one({"account_name": None, "is_used": 0})
    if not spare:
        spare = await col.find_one({"is_used": 0})
    return spare


async def consume_spare(channel_id: int) -> bool:
    """标记备用频道为已使用"""
    col = get_spare_pool_col()
    await col.update_one({"channel_id": channel_id}, {"$set": {"is_used": 1}})
    return True


async def release_spare(channel_id: int) -> bool:
    """释放备用频道回池(标记为未使用)"""
    col = get_spare_pool_col()
    await col.update_one({"channel_id": channel_id}, {"$set": {"is_used": 0}})
    return True


async def remove_spare(channel_id: int) -> bool:
    """从备用池中删除频道"""
    col = get_spare_pool_col()
    await col.delete_one({"channel_id": channel_id})
    return True


async def list_spare_pool() -> list[dict]:
    """列出所有备用池频道"""
    col = get_spare_pool_col()
    return await col.find({}, sort=("account_name", 1))


# ─── 轮转配置操作 ──────────────────────────────────────────────

async def get_rotation_config(key: str) -> str | None:
    """读取轮转配置"""
    col = get_rotation_config_col()
    row = await col.find_one({"config_key": key})
    return row.get("config_value") if row else None


async def set_rotation_config(key: str, value: str):
    """写入轮转配置"""
    import datetime as _dt
    col = get_rotation_config_col()
    existing = await col.find_one({"config_key": key})
    if existing:
        await col.update_one(
            {"config_key": key},
            {"$set": {"config_value": value, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}},
        )
    else:
        await col.insert_one({
            "config_key": key,
            "config_value": value,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


async def get_active_cells() -> list[dict]:
    """获取所有状态为 active 的槽位,按环next_active_chat_id 排序"""
    col = get_cells_col()
    cells = await col.find({"status": "active"})
    # 尝试按环形顺序排序
    if len(cells) <= 1:
        return cells
    # next_active_chat_id 构成
    channel_map = {c["channel_id"]: c for c in cells}
    ordered = []
    visited = set()
    if cells:
        current = cells[0]
        while current["channel_id"] not in visited:
            visited.add(current["channel_id"])
            ordered.append(current)
            nxt = current.get("next_active_chat_id")
            if nxt and nxt in channel_map:
                current = channel_map[nxt]
            else:
                # 加入未访问的剩余 cell
                for c in cells:
                    if c["channel_id"] not in visited:
                        ordered.append(c)
                break
    return ordered


async def get_next_active_cell(current_channel_id: int) -> dict | None:
    """获取环形current 的下一active 槽位"""
    col = get_cells_col()
    current = await col.find_one({"channel_id": current_channel_id, "status": "active"})
    if not current:
        return None
    nxt_id = current.get("next_active_chat_id")
    if nxt_id:
        return await col.find_one({"channel_id": nxt_id, "status": "active"})
    # 回环：取第一个 active
    cells = await col.find({"status": "active"}, sort=("created_at", 1), limit=1)
    return cells[0] if cells else None


async def get_active_or_shadow_cell(channel_id: int) -> dict | None:
    """获取指定 channel cell 记录(任status)"""
    col = get_cells_col()
    return await col.find_one({"channel_id": channel_id})


async def set_cell_status(slot_id: str, new_status: str):
    """更新 cell 状态"""
    import datetime as _dt
    col = get_cells_col()
    await col.update_one(
        {"slot_id": slot_id},
        {"$set": {
            "status": new_status,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }},
    )


async def update_cell_heartbeat(slot_id: str):
    """更新 cell 心跳时间"""
    import datetime as _dt
    col = get_cells_col()
    await col.update_one(
        {"slot_id": slot_id},
        {"$set": {"last_heartbeat": _dt.datetime.now(_dt.timezone.utc).isoformat()}},
    )


async def enqueue_job(
    code: str,
    target_user_id: int,
    storage_channel_id: int,
    storage_msg_ids: list[int],
    batch_file_meta: str = "",
    task_type: str = "single",
    protect_content: bool = False,
):
    """jobs 表写入派工任务"""
    import datetime as _dt
    try:
        import orjson as _json
    except ImportError:
        import json as _json
    col = get_jobs_col()
    await col.insert_one({
        "code": code,
        "target_user_id": target_user_id,
        "storage_channel_id": storage_channel_id,
        "storage_msg_ids": _json_dumps(storage_msg_ids),
        "batch_file_meta": batch_file_meta,
        "task_type": task_type,
        "status": "pending",
        "protect_content": protect_content,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    })
    # 递增本地计数器(用于 admin /status)
    try:
        from utils.shared_counters import status_counters
        status_counters["total_files"] = status_counters.get("total_files", 0) + 1
        status_counters["active_files"] = status_counters.get("active_files", 0) + 1
    except Exception:
        pass
    # 通知 Dsp Bot（本地 SQLite 通知，零 RU）
    try:
        from .cache_store import get_cache_store
        store = get_cache_store()
        await store.notify_dsp_new_job()
    except Exception:
        pass  # 通知失败不影响 jobs 写入


async def dequeue_jobs(batch_size: int = 10) -> list:
    """从 jobs 表批量取出待派工任务(原子操作:CTE + UPDATE ... RETURNING *,一次 DB 往返)。
    
    注意: 查询带 asyncio.wait_for 超时保护(5s),防止数据库异常时永久阻塞。
    Args:
        batch_size: 一次取出的最大任务数,默认 10
    Returns:
        JobResult 列表,可能为空列表
    """
    col = get_jobs_col()
    try:
        rows = await asyncio.wait_for(col._query("""
            WITH next AS (
                SELECT id FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT $1
            )
            UPDATE jobs SET status = 'dispatched'
            WHERE id IN (SELECT id FROM next)
            RETURNING *
        """, [batch_size]), timeout=5.0)
    except asyncio.TimeoutError:
        logger.error("[DB] dequeue_jobs 查询超时(>5s),跳过本次")
        return []
    if not rows:
        return []
    try:
        import orjson as _json
    except ImportError:
        import json as _json
    results = []
    for row in rows:
        storage_msg_ids = []
        raw = row.get("storage_msg_ids", "")
        if raw:
            try:
                storage_msg_ids = _json.loads(raw) if isinstance(raw, str) else raw
            except (_json.JSONDecodeError, TypeError):
                storage_msg_ids = []
        results.append(JobResult(
            job_id=row["id"],
            code=row["code"],
            target_user_id=row["target_user_id"],
            storage_channel_id=row.get("storage_channel_id", 0),
            storage_msg_ids=storage_msg_ids,
            batch_file_meta=row.get("batch_file_meta", ""),
            task_type=row.get("task_type", "single"),
            protect_content=row.get("protect_content", False),
            retry_count=row.get("retry_count", 0),
        ))
    return results


async def dequeue_job():
    """从 jobs 表取出一个待派工任务(保持向后兼容,内部调用 dequeue_jobs(1))"""
    results = await dequeue_jobs(1)
    return results[0] if results else None


async def get_pending_jobs_count() -> int:
    """获取 pending 状态的 jobs 数量（用于动态限速）。"""
    col = get_jobs_col()
    rows = await col._query(
        "SELECT COUNT(*) as cnt FROM jobs WHERE status = 'pending'",
    )
    if rows:
        return int(rows[0].get("cnt", 0))
    return 0


async def reenqueue_job(job_id: int, max_retries: int = 3) -> bool:
    """将一条 dispatched 的 job 重新标记为 pending 并递增 retry_count。
    
    Args:
        job_id: 任务 ID
        max_retries: 数据库操作最大重试次数
    Returns:
        True 表示成功
    """
    col = get_jobs_col()
    for attempt in range(max_retries):
        try:
            result = await col._query("""
                UPDATE jobs SET status = 'pending', retry_count = COALESCE(retry_count, 0) + 1
                WHERE id = $1 AND status = 'dispatched'
                RETURNING id
            """, [job_id])
            return len(result) > 0
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"[DB] reenqueue_job 重试{max_retries}次后失败 job_id={job_id}: {e}")
                return False
            await asyncio.sleep(0.5 * (attempt + 1))


async def reenqueue_job_no_retry(job_id: int, max_retries: int = 3) -> bool:
    """将一条 dispatched 的 job 重新标记为 pending，不递增 retry_count。
    用于 semaphore 等待超时等尚未实际尝试发送的场景，避免白白消耗重试次数。
    
    Args:
        job_id: 任务 ID
        max_retries: 数据库操作最大重试次数
    Returns:
        True 表示成功
    """
    col = get_jobs_col()
    for attempt in range(max_retries):
        try:
            result = await col._query("""
                UPDATE jobs SET status = 'pending'
                WHERE id = $1 AND status = 'dispatched'
                RETURNING id
            """, [job_id])
            return len(result) > 0
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"[DB] reenqueue_job_no_retry 重试{max_retries}次后失败 job_id={job_id}: {e}")
                return False
            await asyncio.sleep(0.5 * (attempt + 1))


class JobResult:
    def __init__(
        self,
        job_id: int,
        code: str,
        target_user_id: int,
        storage_channel_id: int,
        storage_msg_ids: list[int],
        batch_file_meta: str,
        task_type: str = "single",
        protect_content: bool = False,
        retry_count: int = 0,
    ):
        self.job_id = job_id
        self.code = code
        self.target_user_id = target_user_id
        self.storage_channel_id = storage_channel_id
        self.storage_msg_ids = storage_msg_ids or []
        self.batch_file_meta = batch_file_meta
        self.task_type = task_type
        self.protect_content = protect_content
        self.retry_count = retry_count


async def mark_job_dead(job_id: int, reason: str, max_retries: int = 3):
    """将反复失败的 job 标记为死信(status='dead'),不再重试。
    
    Args:
        job_id: 任务 ID
        reason: 死信原因
        max_retries: 数据库操作最大重试次数
    """
    import datetime as _dt
    col = get_jobs_col()
    for attempt in range(max_retries):
        try:
            await col.update_one(
                {"id": job_id},
                {"$set": {
                    "status": "dead",
                    "dead_reason": reason,
                    "dispatched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                }},
            )
            return
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"[DB] mark_job_dead 重试{max_retries}次后失败 job_id={job_id}: {e}")
            else:
                await asyncio.sleep(0.5 * (attempt + 1))


async def get_and_reset_dead_jobs(max_count: int = 10) -> list:
    """获取死信队列中的 job 并重置为 pending,供 DSP 重试。
    
    限制: 每个 job 最多重试 2 次死信队列,超过后永久丢弃。
    """
    col = get_jobs_col()
    import datetime as _dt
    # 查找死信 job,且死信重试次数 < 2
    # 使用 find 替代不存在的 aggregate 方法
    dead_jobs = await col.find(
        {"status": "dead"},
        sort=("created_at", 1),
        limit=max_count,
    )

    if not dead_jobs:
        return []

    # 重置这些 job,并递增死信重试次数
    # 不重置 retry_count,避免每个死信周期都重试 3 次
    updated = []
    for job in dead_jobs:
        job_id = job.get("id")
        if not job_id:
            continue
        dead_retry_count = job.get("dead_retry_count", 0)
        if dead_retry_count >= 2:
            continue  # 超过重试上限,永久丢弃
        result = await col.update_one(
            {"id": job_id},
            {"$set": {
                "status": "pending",
                "dead_retry": True,
                "dead_retry_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
            "$inc": {"dead_retry_count": 1}},
        )
        if result.matched_count > 0:
            updated.append(job_id)

    return updated


async def set_code_expiry(code: str, expires_at: str):
    """设置取件码的过期时间"""
    col = get_codes_col()
    await col.update_one(
        {"code": code},
        {"$set": {"expire_time": expires_at}},
    )


async def log_rotate(
    from_slot_id: str,
    to_slot_id: str,
    from_status: str,
    to_status: str,
    reason: str,
    triggered_by: str = "mon",
):
    """写降级审计日志"""
    import datetime as _dt
    col = get_rotate_log_col()
    await col.insert_one({
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "from_slot_id": from_slot_id,
        "to_slot_id": to_slot_id,
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
        "triggered_by": triggered_by,
    })


# ─── 中继账号池 CRDB 备份 ──────────────────────────────────────

async def get_relay_accounts_from_crdb() -> list[dict]:
    """从 CRDB 拉取中继账号列表（用于 VPS 重启恢复）"""
    try:
        rows = await _query_raw(
            "SELECT id, api_id, api_hash, phone, is_active, created_at, last_login_at "
            "FROM relay_accounts WHERE is_active = 1 ORDER BY id"
        )
        return [
            {
                "id": r["id"],
                "api_id": r["api_id"],
                "api_hash": r["api_hash"],
                "phone": r["phone"],
                "is_active": bool(r["is_active"]),
                "created_at": r.get("created_at"),
                "last_login_at": r.get("last_login_at"),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[RelayDB/CRDB] 拉取中继账号失败: {e}")
        return []


async def sync_relay_to_crdb(api_id: int, api_hash: str, phone: str):
    """同步中继账号到 CRDB（幂等操作，phone 有 UNIQUE 约束）"""
    try:
        await _client.execute(
            """INSERT INTO relay_accounts (api_id, api_hash, phone, is_active)
               VALUES ($1, $2, $3, 1)
               ON CONFLICT(phone) DO UPDATE SET
                   api_id = EXCLUDED.api_id,
                   api_hash = EXCLUDED.api_hash,
                   is_active = 1
            """,
            [api_id, api_hash, phone],
        )
    except Exception as e:
        logger.warning(f"[RelayDB/CRDB] 同步中继账号失败 (phone={phone}): {e}")


async def _query_raw(sql: str, params: list = None) -> list[dict]:
    """执行原始 SQL 查询，返回 dict 列表"""
    if _client._pool is None:
        logger.error("[DB] _query_raw: 连接池未初始化")
        return []
    async with _client._pool.acquire() as conn:
        records = await conn.fetch(sql, *(params or []))
        return [_row_to_dict(r) for r in records]


# ─── 清理函数已移除──────────────────────────────────────────────
# CRDB 行级 TTL 已启用(decode_logs + jobs 7 天自动清理)# RU 自动清理,Python 兜底清理不再需要