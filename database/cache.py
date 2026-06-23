import time
import asyncio
from collections import OrderedDict
from typing import Optional, Any


class QueryCache:
    def __init__(self, max_size: int = 2000, ttl_seconds: int = 120):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds

    def _clean_expired(self):
        now = time.time()
        to_remove = []
        for key, entry in self.cache.items():
            if now - entry["ts"] >= self.ttl:
                to_remove.append(key)
        for key in to_remove:
            del self.cache[key]

    def get(self, key: str) -> Optional[Any]:
        self._clean_expired()
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry["ts"] < self.ttl:
                self.cache.move_to_end(key)
                return entry["data"]
            del self.cache[key]
        return None

    def set(self, key: str, data: Any):
        self._clean_expired()
        if len(self.cache) >= self.max_size:
            self.cache.popitem(last=False)
        self.cache[key] = {"data": data, "ts": time.time()}

    def invalidate(self, key: str):
        if key in self.cache:
            del self.cache[key]

    def clear(self):
        self.cache.clear()


_user_cache = QueryCache(max_size=1000, ttl_seconds=10800)           # 1000条/3小时
_file_record_cache = QueryCache(max_size=1000, ttl_seconds=300)  # 1000条/5分钟(缩短以快速响应状态变更)
_config_cache = QueryCache(max_size=100, ttl_seconds=600)            # 10分钟


def get_user_cache() -> QueryCache:
    return _user_cache


def get_file_record_cache() -> QueryCache:
    return _file_record_cache


def invalidate_file_record(file_code: str):
    """失效指定文件码的缓存,用于文件状态变更时立即生效。"""
    _file_record_cache.invalidate(f"file_record:{file_code}")


def get_config_cache() -> QueryCache:
    return _config_cache


# ─── codes 表缓存(取件码创建后不变,7天 TTL) ────────────

_code_cache = QueryCache(max_size=5000, ttl_seconds=604800)  # 5000条/7天


def get_code_cache() -> QueryCache:
    return _code_cache


# ─── request_count 本地累积:批量写 CRDB ──────────────────

_request_count_buffer: dict[str, int] = {}
_request_count_lock = asyncio.Lock()
_REQUEST_COUNT_FLUSH_INTERVAL = 300  # 每 300 秒 flush 一次(5分钟),减少 CRDB RU 消耗


async def incr_request_count(file_code: str):
    """本地累积 request_count,避免每次解码写一次 CRDB。"""
    async with _request_count_lock:
        _request_count_buffer[file_code] = _request_count_buffer.get(file_code, 0) + 1


async def _flush_request_count_loop():
    """后台任务:每 60 秒批量 flush request_count 到 CRDB。"""
    from loguru import logger
    from .session import update_file_record_and_invalidate as _update

    while True:
        await asyncio.sleep(_REQUEST_COUNT_FLUSH_INTERVAL)
        try:
            async with _request_count_lock:
                if not _request_count_buffer:
                    continue
                batch = dict(_request_count_buffer)
                _request_count_buffer.clear()
            for code, count in batch.items():
                await _update(code, {"$inc": {"request_count": count}})
            logger.debug(f"[request_count] flushed {len(batch)} codes, {sum(batch.values())} counts")
        except Exception as e:
            logger.error(f"[request_count] flush failed: {e}")


# ─── SQLite 持久化备份 ──────────────────────────────────────────

async def dump_cache_to_disk():
    """将本进程内存缓存 dump 到 SQLite(由后台任务定期调用)"""
    import asyncio as _asyncio
    from .cache_store import get_cache_store

    store = get_cache_store()
    now = time.time()
    entries = []
    for cache, prefix in [
        (_user_cache, "user:"),
        (_file_record_cache, "file:"),
        (_config_cache, "config:"),
    ]:
        for k, v in cache.cache.items():
            if time.time() - v["ts"] < cache.ttl:
                entries.append((k, v["data"], now))
    if entries:
        await store.dump(entries)


async def dump_cache_to_disk_loop():
    """后台任务:每 60 秒定期 dump 内存缓存到 SQLite,并清理过期通知"""
    import asyncio as _asyncio
    from loguru import logger
    from .cache_store import get_cache_store

    cleanup_counter = 0
    while True:
        await _asyncio.sleep(60)
        try:
            await dump_cache_to_disk()
            # 每 10 分钟清理一次通知表
            cleanup_counter += 1
            if cleanup_counter >= 10:
                cleanup_counter = 0
                store = get_cache_store()
                await store.cleanup_notify_tables()
                logger.debug("[cache_store] 已清理过期通知表记录")
        except Exception as e:
            logger.debug(f"[cache_store] 定期 dump 失败: {e}")


async def load_cache_from_disk():
    """启动时从 SQLite 恢复到本进程内存缓存(只恢复最热的 N 条到 L1)"""
    from loguru import logger
    from .cache_store import get_cache_store

    store = get_cache_store()
    try:
        records = await store.load()
        loaded = 0
        max_user = _user_cache.max_size
        max_file = _file_record_cache.max_size
        max_config = _config_cache.max_size
        for key, data in records:
            if key.startswith("user:"):
                if len(_user_cache.cache) >= max_user:
                    continue
                _user_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
            elif key.startswith("file:"):
                if len(_file_record_cache.cache) >= max_file:
                    continue
                _file_record_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
            elif key.startswith("config:"):
                if len(_config_cache.cache) >= max_config:
                    continue
                _config_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
        logger.info(f"[cache_store] 从 SQLite 恢复 {loaded} 条最近缓存到 L1")
        logger.info(f"    用户缓存: {len(_user_cache.cache)}条, 文件缓存: {len(_file_record_cache.cache)}条")
    except Exception as e:
        logger.warning(f"[cache_store] 加载失败(回退纯内存模式): {e}")


# ─── Decode Logs 缓冲:定时 flush ──────────────────
# 策略:30 分钟兜底 + Bot 关闭时强制 flush

_DECODE_LOG_FLUSH_INTERVAL = 30 * 60  # 30 分钟


async def _flush_decode_log_buffer_loop():
    """后台任务:混合策略 flush decode_logs 到 CRDB"""
    from loguru import logger
    from .session import get_decode_logs_col
    decode_logs_col = get_decode_logs_col()
    from .cache_store import get_decode_log_buffer

    while True:
        await asyncio.sleep(_DECODE_LOG_FLUSH_INTERVAL)
        try:
            buf = get_decode_log_buffer()
            rows = await buf._db.execute_fetchall(
                "SELECT id, file_code, requester_id, request_time, status, source_channel_id "
                "FROM decode_log_buffer ORDER BY id LIMIT 500"
            )
            if rows:
                # 分批写入,每批 200 条
                batch_size = 200
                for i in range(0, len(rows), batch_size):
                    batch = rows[i:i + batch_size]
                    await decode_logs_col.insert_many([
                        {
                            "file_code": r[1],
                            "requester_id": r[2],
                            "request_time": r[3],
                            "status": r[4],
                            "source_channel_id": r[5],
                        }
                        for r in batch
                    ])
                # 清空已 flush 的记录
                ids = [r[0] for r in rows]
                await buf._db.execute(
                    "DELETE FROM decode_log_buffer WHERE id IN ({})".format(",".join(str(i) for i in ids))
                )
                await buf._db.commit()
                logger.info(f"[DecodeLog] flushed {len(rows)} logs to CRDB")
                # 递增本地计数器
                try:
                    from utils.shared_counters import status_counters
                    status_counters["today_decodes"] = status_counters.get("today_decodes", 0) + len(rows)
                except Exception:
                    pass
            else:
                logger.debug("[DecodeLog] flush: no new logs")
        except Exception as e:
            logger.error(f"[DecodeLog] flush failed: {e}")