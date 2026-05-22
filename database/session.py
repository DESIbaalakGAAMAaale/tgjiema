import json
import re
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

import redis.asyncio as redis

PREFIX = "tgjiema"

USERS_KEY = f"{PREFIX}:users"
USERS_INDEX = f"{PREFIX}:users:by_id"
USER_NAME_INDEX = f"{PREFIX}:users:by_name"

FILE_RECORDS_KEY = f"{PREFIX}:file_records"
FILE_RECORDS_INDEX = f"{PREFIX}:file_records:by_code"
FILE_RECORDS_BY_UPLOADER = f"{PREFIX}:file_records:by_uploader"

DECODE_LOGS_KEY = f"{PREFIX}:decode_logs"
DECODE_LOGS_INDEX = f"{PREFIX}:decode_logs:by_file"
DECODE_LOGS_BY_REQUESTER = f"{PREFIX}:decode_logs:by_requester"
DECODE_LOGS_BY_TIME = f"{PREFIX}:decode_logs:by_time"


_pool: redis.ConnectionPool = None


async def init_db(redis_url: str = None, ssl_sni: str = None):
    from config import settings as _settings
    global _pool
    url = redis_url or _settings.REDIS_URL
    sni = ssl_sni or _settings.REDIS_SSL_SNI
    ssl_kwargs = {}
    if sni:
        ssl_kwargs["ssl_sni"] = sni
    _pool = redis.ConnectionPool.from_url(
        url,
        decode_responses=True,
        max_connections=20,
        **ssl_kwargs,
    )
    client = redis.Redis(connection_pool=_pool)
    await client.ping()
    await client.aclose()


async def close_db():
    global _pool
    if _pool:
        await _pool.disconnect()
        _pool = None


def _json_loads(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        return json.loads(val)
    return val


def _json_dumps(val: Any) -> str:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, (list, dict)):
        return json.dumps(val, default=str)
    return val


def _doc_to_redis(doc: dict) -> dict:
    result = {}
    for k, v in doc.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            result[k] = json.dumps(v, default=str)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, bool):
            result[k] = "1" if v else "0"
        else:
            result[k] = str(v)
    return result


def _doc_from_redis(key: str, data: dict) -> Optional[dict]:
    if not data:
        return None
    result = {}
    for k, v in data.items():
        if k in ("file_types", "backup_channel_msg_ids"):
            result[k] = _json_loads(v)
        elif k in (
            "user_id", "primary_channel_id", "primary_channel_msg_id",
            "requester_id", "source_channel_id", "request_count",
            "daily_decode_quota", "quota_used_today", "external_decode_quota",
            "external_used_today",
        ):
            result[k] = int(v) if v else 0
        elif k in ("can_upload", "is_banned"):
            result[k] = v == "1"
        elif k in ("created_at", "updated_at", "create_time", "request_time",
                    "expire_time", "quota_date", "external_quota_date"):
            if v:
                try:
                    result[k] = datetime.fromisoformat(v)
                except (ValueError, TypeError):
                    result[k] = v
            else:
                result[k] = v
        else:
            result[k] = _json_loads(v)
    result["_key"] = key
    return result


class AsyncRedisCollection:
    def __init__(self, key: str, indexes: list[str] = None):
        self.key = key
        self.indexes = indexes or []

    async def insert_one(self, doc: dict) -> dict:
        client = redis.Redis(connection_pool=_pool)
        try:
            redis_doc = _doc_to_redis(doc)
            pipe = client.pipeline()
            pipe.hset(self.key, mapping=redis_doc)
            for idx_key, idx_val in self._build_indexes(doc).items():
                pipe.sadd(idx_key, idx_val)
            await pipe.execute()
            return doc
        finally:
            await client.close()

    async def find_one(self, query: dict) -> Optional[dict]:
        if "user_id" in query:
            uid = int(query["user_id"])
            client = redis.Redis(connection_pool=_pool)
            try:
                data = await client.hgetall(f"{USERS_INDEX}:{uid}")
                if data:
                    return _doc_from_redis(f"{USERS_INDEX}:{uid}", data)
            finally:
                await client.close()
            return None

        if "file_code" in query:
            fc = query["file_code"]
            client = redis.Redis(connection_pool=_pool)
            try:
                data = await client.hgetall(f"{FILE_RECORDS_INDEX}:{fc}")
                if data:
                    return _doc_from_redis(f"{FILE_RECORDS_INDEX}:{fc}", data)
            finally:
                await client.close()
            return None

        return None

    async def update_one(
        self, query: dict, update: dict, return_document=None
    ) -> "UpdateResult":
        if "user_id" in query:
            uid = int(query["user_id"])
            key = f"{USERS_INDEX}:{uid}"
        elif "file_code" in query:
            key = f"{FILE_RECORDS_INDEX}:{query['file_code']}"
        else:
            return UpdateResult(0)

        client = redis.Redis(connection_pool=_pool)
        try:
            if "$set" in update:
                flat = _doc_to_redis(update["$set"])
                if flat:
                    await client.hset(key, mapping=flat)
            if "$inc" in update:
                pipe = client.pipeline()
                for k, v in update["$inc"].items():
                    pipe.hincrby(key, k, v)
                await pipe.execute()
            if "$push" in update:
                for k, v in update["$push"].items():
                    stored = await client.hget(key, k)
                    arr = _json_loads(stored) if stored else []
                    if isinstance(v, list):
                        arr.extend(v)
                    else:
                        arr.append(v)
                    await client.hset(key, k, json.dumps(arr, default=str))
            return UpdateResult(1)
        finally:
            await client.close()

    async def count_documents(self, query: dict) -> int:
        client = redis.Redis(connection_pool=_pool)
        try:
            if not query:
                if self.key == DECODE_LOGS_KEY:
                    return await client.zcard(DECODE_LOGS_BY_TIME)
                if self.key == FILE_RECORDS_KEY:
                    return await client.scard(self.key)
                if self.key == USERS_KEY:
                    return await client.scard(USERS_INDEX)

            if "status" in query and self.key == FILE_RECORDS_KEY:
                status = query["status"]
                keys = await client.smembers(self.key)
                count = 0
                for k in keys:
                    v = await client.hget(f"{FILE_RECORDS_INDEX}:{k}", "status")
                    if v == status:
                        count += 1
                return count

            if "request_time" in query and "$gte" in query["request_time"]:
                if self.key == DECODE_LOGS_KEY:
                    since_ts = query["request_time"]["$gte"]
                    if isinstance(since_ts, datetime):
                        since_ts = since_ts.timestamp()
                    return await client.zcount(
                        DECODE_LOGS_BY_TIME, since_ts, 9999999999
                    )

            if query and self.key == USERS_KEY:
                results = await self._scan_and_filter(client, query, limit=None)
                return len(results)

            if query and self.key == FILE_RECORDS_KEY:
                results = await self._scan_file_records(client, query, limit=None)
                return len(results)

            return 0
        finally:
            await client.close()

    async def find(
        self, query: dict = None, sort: tuple = None, skip: int = 0, limit: int = None
    ) -> list[dict]:
        client = redis.Redis(connection_pool=_pool)
        try:
            if self.key == DECODE_LOGS_KEY:
                results = await self._find_decode_logs(client, query, sort, skip, limit)
            elif self.key == FILE_RECORDS_KEY:
                results = await self._find_file_records(client, query, sort, skip, limit)
            elif self.key == USERS_KEY:
                results = await self._find_users(client, query, sort, skip, limit)
            else:
                results = []
            return results
        finally:
            await client.close()

    async def _scan_and_filter(
        self, client, query: dict, limit: Optional[int] = None
    ) -> list[dict]:
        results = []
        pattern = f"{FILE_RECORDS_INDEX}:*"
        idx = 0
        async for key in client.scan_iter(match=pattern):
            if limit and idx >= limit:
                break
            data = await client.hgetall(key)
            doc = _doc_from_redis(key, data)
            if doc and self._matches(doc, query):
                results.append(doc)
                idx += 1
        return results

    def _matches(self, doc: dict, query: dict) -> bool:
        if "$or" in query:
            return any(
                self._matches(doc, {k: v}) for v in query["$or"]
            )
        for k, v in query.items():
            if k == "$or" or k == "$regex":
                continue
            if k == "$gte":
                continue
            doc_val = doc.get(k)
            if isinstance(v, dict):
                if "$regex" in v:
                    flags = v.get("$options", "i")
                    pattern = v["$regex"]
                    if flags == "i":
                        if not re.search(pattern, str(doc_val or ""), re.I):
                            return False
                    else:
                        if not re.search(pattern, str(doc_val or "")):
                            return False
                elif "$gte" in v:
                    if doc_val is None:
                        return False
                    if isinstance(doc_val, datetime):
                        if doc_val < v["$gte"]:
                            return False
                    elif doc_val < v["$gte"]:
                        return False
            else:
                if str(doc_val) != str(v) and doc_val != v:
                    return False
        return True

    async def _find_decode_logs(
        self, client, query: dict, sort: tuple, skip: int, limit: int
    ) -> list[dict]:
        if query and "file_code" in query:
            fc = query["file_code"]
            keys = await client.smembers(f"{DECODE_LOGS_INDEX}:{fc}")
            results = []
            idx = 0
            sk = skip
            lm = limit or 100
            for k in sorted(keys):
                if sk > 0:
                    sk -= 1
                    continue
                if idx >= lm:
                    break
                data = await client.hgetall(k)
                doc = _doc_from_redis(k, data)
                if doc:
                    results.append(doc)
                    idx += 1
            return results

        if query and "requester_id" in query:
            rid = int(query["requester_id"])
            score_key = f"{DECODE_LOGS_BY_REQUESTER}:{rid}"
            ts_map_raw = await client.zrange(score_key, 0, -1, withscores=True)
            if ts_map_raw:
                ts_map = dict(ts_map_raw)
                sorted_keys = sorted(ts_map.keys(), key=lambda k: ts_map[k], reverse=True)
                start = skip
                end = skip + limit - 1 if limit else len(sorted_keys) - 1
                paged_keys = sorted_keys[start : end + 1] if limit else sorted_keys[start:]
                results = []
                for k in paged_keys:
                    data = await client.hgetall(k)
                    doc = _doc_from_redis(k, data)
                    if doc:
                        results.append(doc)
                return results
            return []

        rev = sort and sort[1] < 0
        start = skip
        end = (skip + limit) if limit else skip + 100
        keys = await client.zrevrange(
            DECODE_LOGS_BY_TIME, start, end - 1, withscores=True
        ) if not rev else await client.zrange(
            DECODE_LOGS_BY_TIME, start, end - 1, withscores=True
        )
        results = []
        for k, _ in keys:
            data = await client.hgetall(k)
            doc = _doc_from_redis(k, data)
            if doc and (not query or self._matches(doc, query)):
                results.append(doc)
        return results

    async def _find_file_records(
        self, client, query: dict, sort: tuple, skip: int, limit: int
    ) -> list[dict]:
        if query and "uploader_id" in query:
            uid = int(query["uploader_id"])
            keys = await client.smembers(f"{FILE_RECORDS_BY_UPLOADER}:{uid}")
            results = []
            idx = 0
            sk = skip
            lm = limit or 100
            for k in sorted(keys, reverse=True):
                if sk > 0:
                    sk -= 1
                    continue
                if idx >= lm:
                    break
                data = await client.hgetall(f"{FILE_RECORDS_INDEX}:{k}")
                doc = _doc_from_redis(f"{FILE_RECORDS_INDEX}:{k}", data)
                if doc and (not query or self._matches(doc, query)):
                    results.append(doc)
                    idx += 1
            return results

        results = await self._scan_and_filter(client, query, limit=limit or 100)

        sort_key = sort[0] if sort else "create_time"
        sort_reverse = sort[1] < 0 if sort else True
        if sort_key == "primary_channel_msg_id":
            results.sort(key=lambda x: x.get("primary_channel_msg_id") or 0, reverse=sort_reverse)
        elif sort_key == "create_time":
            results.sort(key=lambda x: x.get("create_time") or datetime.min, reverse=sort_reverse)
        elif sort_key == "created_at":
            results.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=sort_reverse)
        elif sort_key == "request_time":
            results.sort(key=lambda x: x.get("request_time") or datetime.min, reverse=sort_reverse)

        if skip:
            results = results[skip:]
        if limit:
            results = results[:limit]
        return results

    async def _find_users(
        self, client, query: dict, sort: tuple, skip: int, limit: int
    ) -> list[dict]:
        if query and "user_id" in query:
            uid = int(query["user_id"])
            data = await client.hgetall(f"{USERS_INDEX}:{uid}")
            doc = _doc_from_redis(f"{USERS_INDEX}:{uid}", data)
            return [doc] if doc else []

        if query and "$or" in query:
            results = []
            for sub_q in query["$or"]:
                sub_results = await self._find_users(client, sub_q, None, 0, None)
                seen = {r.get("user_id") for r in results}
                for r in sub_results:
                    if r.get("user_id") not in seen:
                        results.append(r)
                        seen.add(r.get("user_id"))
            if sort and sort[0] == "created_at":
                results.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=sort[1] < 0)
            if skip:
                results = results[skip:]
            if limit:
                results = results[:limit]
            return results

        results = []
        pattern = f"{USERS_INDEX}:*"
        idx = 0
        async for key in client.scan_iter(match=pattern):
            if limit and idx >= limit:
                break
            data = await client.hgetall(key)
            doc = _doc_from_redis(key, data)
            if doc and (not query or self._matches(doc, query)):
                results.append(doc)
                idx += 1

        if sort and sort[0] == "created_at":
            results.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=sort[1] < 0)
        if skip:
            results = results[skip:]
        if limit:
            results = results[:limit]
        return results

    async def _scan_file_records(
        self, client, query: dict, limit: Optional[int] = None
    ) -> list[dict]:
        return await self._scan_and_filter(client, query, limit=limit)

    def _build_indexes(self, doc: dict) -> dict:
        indexes = {}
        if "user_id" in doc:
            uid = doc["user_id"]
            indexes[f"{USERS_INDEX}:{uid}"] = doc.get("_key") or ""
        if "file_code" in doc:
            fc = doc["file_code"]
            indexes[f"{FILE_RECORDS_INDEX}:{fc}"] = doc.get("_key") or ""
            indexes[f"{FILE_RECORDS_BY_UPLOADER}:{doc.get('uploader_id', 0)}"] = fc
        if "requester_id" in doc:
            rid = doc["requester_id"]
            indexes[f"{DECODE_LOGS_BY_REQUESTER}:{rid}"] = doc.get("_key") or ""
        return indexes


_users_col = AsyncRedisCollection(USERS_KEY, [USERS_INDEX])
_file_records_col = AsyncRedisCollection(FILE_RECORDS_KEY)
_decode_logs_col = AsyncRedisCollection(DECODE_LOGS_KEY)


def get_users_col() -> AsyncRedisCollection:
    return _users_col


def get_file_records_col() -> AsyncRedisCollection:
    return _file_records_col


def get_decode_logs_col() -> AsyncRedisCollection:
    return _decode_logs_col


async def _insert_decode_log(client, doc: dict) -> dict:
    fc = doc.get("file_code", "")
    rid = doc.get("requester_id", 0)
    key = f"{DECODE_LOGS_KEY}:{fc}:{rid}:{datetime.utcnow().timestamp()}"
    pipe = client.pipeline()
    redis_doc = _doc_to_redis(doc)
    pipe.hset(key, mapping=redis_doc)
    if fc:
        pipe.sadd(f"{DECODE_LOGS_INDEX}:{fc}", key)
    ts = doc.get("request_time")
    if isinstance(ts, datetime):
        ts = ts.timestamp()
    elif ts is None:
        ts = datetime.utcnow().timestamp()
    pipe.zadd(DECODE_LOGS_BY_TIME, {key: ts})
    if rid:
        pipe.zadd(f"{DECODE_LOGS_BY_REQUESTER}:{rid}", {key: ts})
    await pipe.execute()
    return doc


async def _find_latest_file_record(client) -> Optional[dict]:
    keys = await client.smembers(FILE_RECORDS_KEY)
    if not keys:
        return None
    max_key = max(keys, key=lambda k: int(k.split(":")[-1]) if ":" in k else 0)
    data = await client.hgetall(max_key)
    return _doc_from_redis(max_key, data)


async def _add_backup_channel(
    client, file_code: str, channel_id: int, backup_bot: str
) -> None:
    key = f"{FILE_RECORDS_INDEX}:{file_code}"
    item = json.dumps({"channel_id": channel_id, "backup_bot": backup_bot})
    backup_list_key = f"{key}:backup_channel_msg_ids"
    await client.rpush(backup_list_key, item)
    stored = await client.lrange(backup_list_key, 0, -1)
    backup_arr = [json.loads(x) for x in stored]
    await client.hset(key, "backup_channel_msg_ids", json.dumps(backup_arr))


class UpdateResult:
    def __init__(self, matched_count: int = 1):
        self.matched_count = matched_count


def _patch_collection_insert(col: AsyncRedisCollection, key: str) -> None:
    orig_insert = col.insert_one

    async def wrapped_insert_one(doc: dict) -> dict:
        client = redis.Redis(connection_pool=_pool)
        try:
            redis_doc = _doc_to_redis(doc)
            await client.hset(key, doc.get("user_id") or doc.get("file_code") or "", json.dumps(redis_doc))
            idx_data = col._build_indexes(doc) if hasattr(col, "_build_indexes") else {}
            pipe = client.pipeline()
            for idx_key, idx_val in idx_data.items():
                pipe.sadd(idx_key, idx_val)
            await pipe.execute()

            if col == _decode_logs_col:
                await _insert_decode_log(client, doc)
            return doc
        finally:
            await client.close()

    col.insert_one = wrapped_insert_one


_patch_collection_insert(_users_col, USERS_KEY)
_patch_collection_insert(_file_records_col, FILE_RECORDS_KEY)
_patch_collection_insert(_decode_logs_col, DECODE_LOGS_KEY)
