import json
from datetime import datetime
from typing import Any, Optional

import asyncpg
from loguru import logger

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
    "CREATE INDEX IF NOT EXISTS idx_send_queue_unprocessed ON send_queue(processed)",
    """CREATE TABLE IF NOT EXISTS backup_config (
        config_key TEXT PRIMARY KEY,
        config_value TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS code_bot_mapping (
        code TEXT PRIMARY KEY,
        bot_username TEXT NOT NULL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS message_backups (
        main_msg_id BIGINT,
        backup_channel_id BIGINT,
        backed_msg_id BIGINT,
        backed_at TEXT,
        PRIMARY KEY (main_msg_id, backup_channel_id)
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
            min_size=2,
            max_size=10,
            statement_cache_size=0,
        )
        for sql in DDL_STATEMENTS:
            await self.execute(sql)
        for sql in MIGRATION_STATEMENTS:
            try:
                await self.execute(sql)
            except Exception as e:
                logger.warning(f"[DB] 迁移 SQL 执行失败 (可忽略): {e}")

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, params: list = None):
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *(params or []))


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
        return json.dumps(val, default=str)
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
                val_json = json.dumps(v, default=str)
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
    existing = await col.find_one({"code": code})
    if existing:
        await col.update_one(
            {"code": code},
            {"$set": {"bot_username": bot_username, "created_at": now}},
        )
    else:
        await col.insert_one({"code": code, "bot_username": bot_username, "created_at": now})


async def get_bot_for_code(code: str) -> str:
    col = get_code_bot_mapping_col()
    row = await col.find_one({"code": code})
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
    await _set_config(f"backup_channels_{group}", json.dumps(channels))


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
    return await _get_config(key)


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