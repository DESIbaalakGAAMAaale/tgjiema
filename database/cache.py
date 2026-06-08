import time
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


_user_cache = QueryCache(max_size=5000, ttl_seconds=300)
_file_record_cache = QueryCache(max_size=5000, ttl_seconds=3600)
_config_cache = QueryCache(max_size=100, ttl_seconds=60)


def get_user_cache() -> QueryCache:
    return _user_cache


def get_file_record_cache() -> QueryCache:
    return _file_record_cache


def get_config_cache() -> QueryCache:
    return _config_cache


# ─── SQLite 持久化备份 ──────────────────────────────────────────

async def dump_cache_to_disk():
    """将本进程内存缓存 dump 到 SQLite（由后台任务定期调用）"""
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
                entries.append((f"{prefix}{k}", v["data"], now))
    if entries:
        await store.dump(entries)


async def dump_cache_to_disk_loop():
    """后台任务：每 60 秒定期 dump 内存缓存到 SQLite"""
    import asyncio as _asyncio
    from loguru import logger

    while True:
        await _asyncio.sleep(60)
        try:
            await dump_cache_to_disk()
        except Exception as e:
            logger.debug(f"[cache_store] 定期 dump 失败: {e}")


async def load_cache_from_disk():
    """启动时从 SQLite 恢复到本进程内存缓存"""
    from loguru import logger
    from .cache_store import get_cache_store

    store = get_cache_store()
    try:
        records = await store.load()
        loaded = 0
        for key, data in records:
            if key.startswith("user:") and len(_user_cache.cache) < _user_cache.max_size:
                _user_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
            elif key.startswith("file:") and len(_file_record_cache.cache) < _file_record_cache.max_size:
                _file_record_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
            elif key.startswith("config:") and len(_config_cache.cache) < _config_cache.max_size:
                _config_cache.cache[key] = {"data": data, "ts": time.time()}
                loaded += 1
        logger.info(f"[cache_store] 从 SQLite 恢复 {loaded} 条缓存到内存")
    except Exception as e:
        logger.warning(f"[cache_store] 加载失败（回退纯内存模式）: {e}")