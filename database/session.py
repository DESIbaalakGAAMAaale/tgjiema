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
    "CREATE INDEX IF NOT EXISTS idx_file_records_msg_id ON file_records(primary_channel_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_send_queue_processed_created ON send_queue(processed, created_at)",
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
    # ─── 环形冗余架构新表 ──────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS cells (
        slot_id TEXT PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'shadow1',
        next_active_chat_id BIGINT,
        prev_slot_id TEXT,
        is_r100 INTEGER DEFAULT 0,
        last_heartbeat TEXT,
        last_synced_msg_id BIGINT DEFAULT 0,
        degrade_count INTEGER DEFAULT 0,
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
    "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rotate_log_timestamp ON rotate_log(timestamp)",
    # ─── 外部码映射表（采集器写入，idx_bot 查询） ─────────────────
    """CREATE TABLE IF NOT EXISTS external_code_mapping (
        external_code TEXT PRIMARY KEY,
        system_code TEXT NOT NULL,
        bot_username TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_external_code_mapping_system ON external_code_mapping(system_code)",
    "CREATE INDEX IF NOT EXISTS idx_external_code_mapping_bot ON external_code_mapping(bot_username)",
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
            min_size=5,
            max_size=30,
            statement_cache_size=0,
        )

        # ─── SQLite 缓存备份：初始化并恢复内存缓存 ───
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
_cells_col = D1Collection("cells")
_codes_col = D1Collection("codes")
_jobs_col = D1Collection("jobs")
_rotate_log_col = D1Collection("rotate_log")
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


# ─── 文件码前缀 → Bot 路由 ──────────────────────────────────────


async def set_code_bot_route(prefix: str, bot_username: str):
    await _set_config(f"code_bot_route:{prefix}", bot_username)


async def get_code_bot_route(prefix: str) -> str | None:
    return await _get_config(f"code_bot_route:{prefix}")


async def delete_code_bot_route(prefix: str):
    existing = await _backup_config_col.find_one({"config_key": f"code_bot_route:{prefix}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"code_bot_route:{prefix}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )


async def get_all_code_bot_routes() -> dict[str, str]:
    col = _backup_config_col
    rows = await col.find({})
    result = {}
    for row in rows:
        key = row.get("config_key", "")
        val = row.get("config_value", "")
        if key.startswith("code_bot_route:") and val:
            prefix = key[len("code_bot_route:"):]
            result[prefix] = val
    return result


async def resolve_bot_for_code(code: str, default_bot: str) -> str:
    """根据文件码前缀匹配配置的路由，无匹配则返回 default_bot。"""
    col = _backup_config_col
    rows = await col.find({})
    best_prefix = ""
    best_bot = ""
    for row in rows:
        key = row.get("config_key", "")
        if not key.startswith("code_bot_route:"):
            continue
        prefix = key[len("code_bot_route:"):]
        if code.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_bot = row.get("config_value", "")
    return best_bot or default_bot


# ─── Bot 解码间隔限流 ────────────────────────────────────────────


async def set_bot_decode_interval(bot_username: str, interval_seconds: int):
    await _set_config(f"bot_decode_interval:{bot_username}", str(interval_seconds))


async def get_bot_decode_interval(bot_username: str) -> int:
    val = await _get_config(f"bot_decode_interval:{bot_username}")
    if val and val.isdigit():
        return int(val)
    return 0


async def delete_bot_decode_interval(bot_username: str):
    existing = await _backup_config_col.find_one({"config_key": f"bot_decode_interval:{bot_username}"})
    if existing:
        await _backup_config_col.update_one(
            {"config_key": f"bot_decode_interval:{bot_username}"},
            {"$set": {"config_value": "", "updated_at": ""}},
        )


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


# ─── 缓存查询层 ──────────────────────────────────────────────────
from .cache import get_user_cache, get_file_record_cache, get_config_cache


# ─── 外部码映射查询 ────────────────────────────────────────────

def get_external_code_mapping_col() -> D1Collection:
    return _external_code_mapping_col


async def get_system_code_for_external(external_code: str) -> str | None:
    """查询外部码对应的系统码，命中则 idx_bot 可直接走本地解码流程。"""
    try:
        row = await _external_code_mapping_col.find_one({"external_code": external_code})
        if row:
            return row.get("system_code")
    except Exception as e:
        logger.warning(f"[DB] get_system_code_for_external failed ({external_code}): {e}")
    return None


async def set_external_code_mapping(
    external_code: str,
    system_code: str,
    bot_username: str = "",
) -> bool:
    """写入外部码→系统码映射（由采集器调用）。"""
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

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    col = get_users_col()
    user = await col.find_one({"user_id": user_id})

    if user:
        cache.set(cache_key, user)

    return user


async def update_user_and_invalidate(user_id: int, update: dict):
    col = get_users_col()
    await col.update_one({"user_id": user_id}, update)

    cache = get_user_cache()
    cache.invalidate(f"user:{user_id}")


async def get_file_record_cached(file_code: str) -> Optional[dict]:
    cache = get_file_record_cache()
    cache_key = f"file:{file_code}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    col = get_file_records_col()
    record = await col.find_one({"file_code": file_code})

    if record:
        cache.set(cache_key, record)

    return record


async def update_file_record_and_invalidate(file_code: str, update: dict):
    col = get_file_records_col()
    await col.update_one({"file_code": file_code}, update)

    cache = get_file_record_cache()
    cache.invalidate(f"file:{file_code}")


async def get_config_cached(key: str) -> Optional[str]:
    cache = get_config_cache()
    cache_key = f"config:{key}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    val = await _get_config(key)
    cache.set(cache_key, val)
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


async def get_active_cells() -> list[dict]:
    """获取所有状态为 active 的槽位，按环形 next_active_chat_id 排序。"""
    col = get_cells_col()
    cells = await col.find({"status": "active"})
    # 尝试按环形顺序排列
    if len(cells) <= 1:
        return cells
    # 按 next_active_chat_id 构成环
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
    """获取环形中 current 的下一个 active 槽位。"""
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
    """获取指定 channel 的 cell 记录（任意 status）。"""
    col = get_cells_col()
    return await col.find_one({"channel_id": channel_id})


async def set_cell_status(slot_id: str, new_status: str):
    """更新 cell 状态。"""
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
    """更新 cell 心跳时间。"""
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
):
    """向 jobs 表写入派工任务。"""
    import datetime as _dt
    import json as _json
    col = get_jobs_col()
    await col.insert_one({
        "code": code,
        "target_user_id": target_user_id,
        "storage_channel_id": storage_channel_id,
        "storage_msg_ids": _json.dumps(storage_msg_ids),
        "batch_file_meta": batch_file_meta,
        "task_type": task_type,
        "status": "pending",
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    })


async def dequeue_job():
    """从 jobs 表取出一个待派工任务（原子操作：CTE + UPDATE ... RETURNING *，一次 DB 往返）。"""
    col = get_jobs_col()
    rows = await col._query("""
        WITH next AS (
            SELECT id FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT 1
        )
        UPDATE jobs SET status = 'dispatched'
        WHERE id = (SELECT id FROM next)
        RETURNING *
    """)
    if not rows:
        return None
    row = rows[0]
    import json as _json
    storage_msg_ids = []
    raw = row.get("storage_msg_ids", "")
    if raw:
        try:
            storage_msg_ids = _json.loads(raw) if isinstance(raw, str) else raw
        except (_json.JSONDecodeError, TypeError):
            storage_msg_ids = []
    return JobResult(
        job_id=row["id"],
        code=row["code"],
        target_user_id=row["target_user_id"],
        storage_channel_id=row.get("storage_channel_id", 0),
        storage_msg_ids=storage_msg_ids,
        batch_file_meta=row.get("batch_file_meta", ""),
        task_type=row.get("task_type", "single"),
    )


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
    ):
        self.job_id = job_id
        self.code = code
        self.target_user_id = target_user_id
        self.storage_channel_id = storage_channel_id
        self.storage_msg_ids = storage_msg_ids or []
        self.batch_file_meta = batch_file_meta
        self.task_type = task_type


async def log_rotate(
    from_slot_id: str,
    to_slot_id: str,
    from_status: str,
    to_status: str,
    reason: str,
    triggered_by: str = "mon",
):
    """写降级审计日志。"""
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


async def cleanup_old_records():
    """每天凌晨 3 点批量清理 30 天前的 decode_logs + jobs（批量 DELETE，低 RU 消耗）。"""
    import asyncio as _asyncio
    import time as _time
    import string as _string

    while True:
        await _asyncio.sleep(3600)
        now = _time.localtime()
        if now.tm_hour != 3:
            continue

        cutoff_iso = datetime.fromtimestamp(_time.time() - 86400 * 30).isoformat()
        tables = [
            (get_decode_logs_col(), "request_time"),
            (get_jobs_col(),        "created_at"),
        ]
        total_deleted = 0
        max_per_table = 100_000  # 单次清理上限保护

        for col, time_col in tables:
            deleted = 0
            while deleted < max_per_table:
                try:
                    rows = await col._query(
                        f"DELETE FROM {col.table} WHERE {time_col} < $1 LIMIT 5000 RETURNING 1",
                        [cutoff_iso],
                    )
                    if not rows:
                        break
                    deleted += len(rows)
                except Exception as e:
                    logger.warning(f"[cleanup] {col.table} 删除失败: {e}")
                    break
            if deleted > 0:
                total_deleted += deleted
                logger.info(f"[cleanup] {col.table} 已清理 {deleted} 条旧记录")

        if total_deleted > 0:
            logger.info(f"[cleanup] 本次共清理 {total_deleted} 条")
        await _asyncio.sleep(3600)  # 清理后等一小时再进入下一轮