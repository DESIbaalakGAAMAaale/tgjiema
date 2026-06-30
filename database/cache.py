import time
import asyncio
from collections import OrderedDict
from typing import Optional, Any


class QueryCache:
    def __init__(self, max_size: int = 2000, ttl_seconds: int = 120):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    def _evict_expired_or_lru(self):
        """淘汰策略:优先淘汰过期项,无过期项则 LRU 弹出最旧项。
        
        避免每次 get/set 全量扫描过期项,改为懒清理:只在淘汰时检查最多 10 个。
        """
        now = time.time()
        keys_to_check = list(self.cache.keys())[:10]
        for key in keys_to_check:
            if key in self.cache and now - self.cache[key]["ts"] >= self.ttl:
                del self.cache[key]
                return  # 找到一个过期项即可
        # 无过期项,正常 LRU 淘汰
        if self.cache:
            self.cache.popitem(last=False)

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry["ts"] < self.ttl:
                self.cache.move_to_end(key)
                self.hits += 1
                return entry["data"]
            del self.cache[key]
        self.misses += 1
        return None

    def set(self, key: str, data: Any):
        if len(self.cache) >= self.max_size:
            self._evict_expired_or_lru()
        self.cache[key] = {"data": data, "ts": time.time()}

    def invalidate(self, key: str):
        if key in self.cache:
            del self.cache[key]

    def clear(self):
        self.cache.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total * 100, 1) if total > 0 else 0,
            "size": len(self.cache),
            "max_size": self.max_size,
        }


_user_cache = QueryCache(max_size=1000, ttl_seconds=10800)           # 1000条/3小时
_file_record_cache = QueryCache(max_size=1000, ttl_seconds=300)  # 1000条/5分钟(缩短以快速响应状态变更)
_config_cache = QueryCache(max_size=100, ttl_seconds=600)            # 10分钟

# ─── C1: 负缓存(防穿透) ──────────────────────────────────
# 查询不存在的 user_id/file_code 时缓存空值 60 秒,避免恶意穿透到 CRDB
_NEGATIVE_CACHE_TTL = 60
_negative_user_cache: dict[int, float] = {}   # user_id -> expired_at
_negative_file_cache: dict[str, float] = {}   # file_code -> expired_at


def get_user_cache() -> QueryCache:
    return _user_cache


def get_file_record_cache() -> QueryCache:
    return _file_record_cache


def check_negative_user(user_id: int) -> bool:
    """检查是否命中负缓存,返回 True 表示该用户确认不存在"""
    if user_id in _negative_user_cache:
        if time.time() < _negative_user_cache[user_id]:
            return True
        del _negative_user_cache[user_id]
    return False


def set_negative_user(user_id: int):
    """写入负缓存:该用户确认不存在"""
    _negative_user_cache[user_id] = time.time() + _NEGATIVE_CACHE_TTL


def clear_negative_user(user_id: int):
    """清除负缓存:用户创建/更新时调用"""
    _negative_user_cache.pop(user_id, None)


def check_negative_file(file_code: str) -> bool:
    """检查是否命中负缓存,返回 True 表示该文件确认不存在"""
    if file_code in _negative_file_cache:
        if time.time() < _negative_file_cache[file_code]:
            return True
        del _negative_file_cache[file_code]
    return False


def set_negative_file(file_code: str):
    """写入负缓存:该文件确认不存在"""
    _negative_file_cache[file_code] = time.time() + _NEGATIVE_CACHE_TTL


def clear_negative_file(file_code: str):
    """清除负缓存:文件创建/更新时调用"""
    _negative_file_cache.pop(file_code, None)


def invalidate_file_record(file_code: str):
    """失效指定文件码的缓存,用于文件状态变更时立即生效。"""
    _file_record_cache.invalidate(f"file_record:{file_code}")
    clear_negative_file(file_code)  # C1: 同时清负缓存


def get_config_cache() -> QueryCache:
    return _config_cache


# ─── codes 表缓存(取件码创建后不变,7天 TTL) ────────────

_code_cache = QueryCache(max_size=5000, ttl_seconds=3600)  # 5000条/1小时(原7天,缩短避免幽灵解码)


def get_code_cache() -> QueryCache:
    return _code_cache


# ─── J 方案: code 缓存 SQLite 失效 ──────────────────────────

def invalidate_code_entry(code: str):
    """失效 code 缓存（内存 + SQLite），用于 status/expiry/note 变更时。"""
    cache = _code_cache
    cache_key = f"code:{code}"
    cache.invalidate(cache_key)
    # 同步删除 SQLite 持久化缓存
    from .cache_store import get_cache_store
    store = get_cache_store()
    asyncio.ensure_future(store.delete(cache_key))


# ─── E7: 用户码列表缓存 ──────────────────────────────────

_user_codes_cache = QueryCache(max_size=500, ttl_seconds=300)  # 500用户/5分钟


def get_user_codes_cache() -> QueryCache:
    return _user_codes_cache


def invalidate_user_codes(user_id: int):
    """用户改码后调用(下架/删除/修改),失效该用户所有分页缓存"""
    cache = get_user_codes_cache()
    keys_to_remove = [k for k in cache.cache if k.startswith(f"user_codes:{user_id}:")]
    for k in keys_to_remove:
        cache.invalidate(k)


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
            # 每 10 分钟清理一次通知表 + 保存 counter 快照
            cleanup_counter += 1
            if cleanup_counter >= 10:
                cleanup_counter = 0
                store = get_cache_store()
                await store.cleanup_notify_tables()
                # F5: 保存 status_counters 快照,下次启动从 SQLite 恢复
                try:
                    from utils.shared_counters import status_counters
                    core = {k: v for k, v in status_counters.items() if not k.startswith("user_code_count:")}
                    await store.save_counter_snapshot(core)
                except Exception:
                    pass
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
                placeholders = ",".join("?" for _ in ids)
                await buf._db.execute(
                    f"DELETE FROM decode_log_buffer WHERE id IN ({placeholders})", ids
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