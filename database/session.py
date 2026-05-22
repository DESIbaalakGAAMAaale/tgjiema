import json
from datetime import datetime
from typing import Any, Optional

import httpx

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
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
        uploader_id INTEGER,
        primary_channel_id INTEGER,
        primary_channel_msg_id INTEGER,
        file_types TEXT,
        backup_channel_msg_ids TEXT,
        status TEXT DEFAULT 'active',
        request_count INTEGER DEFAULT 0,
        create_time TEXT,
        expire_time TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS decode_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_code TEXT,
        requester_id INTEGER,
        request_time TEXT,
        status TEXT DEFAULT 'queued',
        source_channel_id INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_users_first_name ON users(first_name)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_status ON file_records(status)",
    "CREATE INDEX IF NOT EXISTS idx_file_records_uploader ON file_records(uploader_id)",
    """CREATE INDEX IF NOT EXISTS idx_decode_logs_file_code ON decode_logs(file_code)""",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_requester ON decode_logs(requester_id)",
    "CREATE INDEX IF NOT EXISTS idx_decode_logs_request_time ON decode_logs(request_time)",
    """CREATE TABLE IF NOT EXISTS pending_uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uploader_id INTEGER,
        primary_channel_id INTEGER,
        primary_channel_msg_id INTEGER,
        file_types TEXT,
        batch_msg_ids TEXT,
        created_at TEXT,
        processed INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pending_uploads_unprocessed ON pending_uploads(processed)",
]


class UpdateResult:
    def __init__(self, matched_count: int = 0):
        self.matched_count = matched_count


class D1Client:
    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
        self._base_url: str = ""
        self._headers: dict = {}

    def configure(self, account_id: str, database_id: str, api_token: str):
        self._base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def connect(self):
        self._http = httpx.AsyncClient(timeout=30)
        for sql in DDL_STATEMENTS:
            await self.execute(sql)

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def execute(self, sql: str, params: list = None) -> list[dict]:
        body = {"sql": sql}
        if params:
            body["params"] = [str(p) if not isinstance(p, (int, float, type(None))) else p for p in params]

        resp = await self._http.post(
            self._base_url, headers=self._headers, json=body
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            raise RuntimeError(f"D1 query failed: {errors}")
        result = data.get("result", [])
        if result:
            return result[0].get("results", [])
        return []


_client: D1Client = D1Client()


async def init_db():
    from config import settings as _settings

    _client.configure(
        account_id=_settings.D1_ACCOUNT_ID,
        database_id=_settings.D1_DATABASE_ID,
        api_token=_settings.D1_API_TOKEN,
    )
    await _client.connect()


async def close_db():
    await _client.close()


def _row_to_dict(columns: list[str], row: list) -> dict:
    result = {}
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else None
        if col in (
            "user_id", "primary_channel_id", "primary_channel_msg_id",
            "requester_id", "source_channel_id", "request_count", "id",
            "daily_decode_quota", "quota_used_today", "uploader_id",
            "external_decode_quota", "external_used_today",
        ):
            result[col] = int(val) if val is not None else 0
        elif col in ("can_upload", "is_banned"):
            result[col] = bool(val)
        elif col in ("file_types", "backup_channel_msg_ids"):
            if val is None or val == "":
                result[col] = {}
            else:
                result[col] = json.loads(val)
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
        else:
            result[col] = val
    return result


def _safe_str(val: Any) -> str:
    if val is None:
        return None
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (list, dict)):
        return json.dumps(val, default=str)
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


class D1Collection:
    def __init__(self, table: str):
        self.table = table

    async def _query(self, sql: str, params: list = None) -> list[dict]:
        raw = await _client.execute(sql, params)
        if not raw:
            return []
        columns = list(raw[0].keys())
        return [_row_to_dict(columns, [row.get(c) for c in columns]) for row in raw]

    async def _execute(self, sql: str, params: list = None) -> int:
        await _client.execute(sql, params)
        return 1

    async def find_one(self, query: dict) -> Optional[dict]:
        where_parts = []
        params = []
        for k, v in query.items():
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ?")
            params.append(v)
        sql = f"SELECT * FROM {self.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        sql += " LIMIT 1"
        rows = await self._query(sql, params)
        return rows[0] if rows else None

    async def insert_one(self, doc: dict) -> dict:
        keys = []
        placeholders = []
        params = []
        for k, v in doc.items():
            keys.append(k)
            placeholders.append("?")
            params.append(_safe_str(v))
        sql = f"INSERT INTO {self.table} ({', '.join(keys)}) VALUES ({', '.join(placeholders)})"
        await self._execute(sql, params)
        return doc

    async def update_one(self, query: dict, update: dict) -> UpdateResult:
        where_parts = []
        where_params = []
        for k, v in query.items():
            if isinstance(v, dict):
                continue
            where_parts.append(f"{k} = ?")
            where_params.append(v)

        set_parts = []
        set_params = []
        if "$set" in update:
            for k, v in update["$set"].items():
                set_parts.append(f"{k} = ?")
                set_params.append(_safe_str(v))
        if "$inc" in update:
            for k, v in update["$inc"].items():
                set_parts.append(f"{k} = {k} + ?")
                set_params.append(int(v))
        if "$push" in update:
            for k, v in update["$push"].items():
                val_json = json.dumps(v, default=str)
                set_parts.append(
                    f"{k} = CASE WHEN {k} IS NULL OR {k} = '' "
                    f"THEN json_array(?) "
                    f"ELSE (SELECT json_group_array(value) FROM json_each({k}) UNION ALL SELECT ? END "
                )
                set_params.append(val_json)
                set_params.append(val_json)

        if not set_parts:
            return UpdateResult(0)
        sql = f"UPDATE {self.table} SET {', '.join(set_parts)}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        all_params = set_params + where_params
        await self._execute(sql, all_params)
        return UpdateResult(1)

    async def count_documents(self, query: dict) -> int:
        where_parts = []
        params = []
        for k, v in query.items():
            if k == "$or":
                continue
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ?")
                    params.append(_safe_str(v["$gte"]))
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ?")
                    params.append(f"%{_safe_str(v['$regex'])}%")
                continue
            where_parts.append(f"{k} = ?")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                for sk, sv in sub_q.items():
                    if isinstance(sv, dict) and "$regex" in sv:
                        or_parts.append(f"{sk} LIKE ?")
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
        where_parts = []
        params = []
        for k, v in query.items():
            if k == "$or":
                continue
            if isinstance(v, dict):
                if "$gte" in v:
                    where_parts.append(f"{k} >= ?")
                    params.append(_safe_str(v["$gte"]))
                elif "$regex" in v:
                    where_parts.append(f"{k} LIKE ?")
                    params.append(f"%{_safe_str(v['$regex'])}%")
                continue
            where_parts.append(f"{k} = ?")
            params.append(_safe_str(v))

        if "$or" in query:
            or_parts = []
            for sub_q in query["$or"]:
                or_clause = []
                for sk, sv in sub_q.items():
                    if isinstance(sv, dict) and "$regex" in sv:
                        or_clause.append(f"{sk} LIKE ?")
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
        else:
            sql += " ORDER BY rowid DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if skip:
            sql += f" OFFSET {int(skip)}"
        return await self._query(sql, params)


_users_col = D1Collection("users")
_file_records_col = D1Collection("file_records")
_decode_logs_col = D1Collection("decode_logs")
_pending_uploads_col = D1Collection("pending_uploads")


def get_users_col() -> D1Collection:
    return _users_col


def get_file_records_col() -> D1Collection:
    return _file_records_col


def get_decode_logs_col() -> D1Collection:
    return _decode_logs_col


def get_pending_uploads_col() -> D1Collection:
    return _pending_uploads_col