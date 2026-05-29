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
_file_record_cache = QueryCache(max_size=2000, ttl_seconds=300)
_config_cache = QueryCache(max_size=100, ttl_seconds=60)


def get_user_cache() -> QueryCache:
    return _user_cache


def get_file_record_cache() -> QueryCache:
    return _file_record_cache


def get_config_cache() -> QueryCache:
    return _config_cache