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
    """失效指定文件码的缓存,用于文件状态变更时立即生效。
    注意：缓存键前缀必须与 dump_cache_to_disk/load_cache_from_disk 使用的 "file:" 一致，
    否则失效操作不会命中缓存条目（审查问题 PRE-03）。
    """
    _file_record_cache.invalidate(f"file:{file_code}")
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
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(store.delete(cache_key))
    except RuntimeError:
        # 无运行中的事件循环（如测试环境），跳过异步删除
        pass


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
_REQUEST_COUNT_FLUSH_INTERVAL = 900  # 每 900 秒 flush 一次(15分钟),大幅减少 CRDB RU 消耗


async def incr_request_count(file_code: str):
    """本地累积 request_count,避免每次解码写一次 CRDB。"""
    async with _request_count_lock:
        _request_count_buffer[file_code] = _request_count_buffer.get(file_code, 0) + 1


async def _flush_request_count_loop():
    """后台任务:批量 flush request_count 到 CRDB。
    
    使用单条 SQL 批量 UPDATE，替代 N+1 循环，大幅减少 RU 消耗。
    """
    from loguru import logger
    from .session import get_file_records_col
    from .session import bulk_update_request_counts
    from database.cache import get_file_record_cache

    while True:
        await asyncio.sleep(_REQUEST_COUNT_FLUSH_INTERVAL)
        try:
            async with _request_count_lock:
                if not _request_count_buffer:
                    continue
                batch = dict(_request_count_buffer)
                _request_count_buffer.clear()
            
            # 单条 SQL 批量 UPDATE (CASE WHEN 技巧)
            affected = await bulk_update_request_counts(batch)
            cache = get_file_record_cache()
            for code in batch:
                cache.invalidate(f"file:{code}")
            
            logger.debug(f"[request_count] flushed {len(batch)} codes, {sum(batch.values())} counts, affected rows={affected}")
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
# 策略:60 分钟兜底 + Bot 关闭时强制 flush

_DECODE_LOG_FLUSH_INTERVAL = 60 * 60  # 60 分钟，减少 CRDB 写入频率


async def _flush_decode_log_buffer_loop():
    """后台任务:混合策略 flush decode_logs 到 CRDB + 清理 7 天前数据

    清理策略（替代废弃的 CRDB TTL job，0 RU 起）：
    - decode_log_buffer（SQLite）：按 buffered_at 删 7 天前记录（0 RU）
    - decode_logs（CRDB）：按 request_time 删 7 天前记录（每 30 分钟一次，分批）
    - jobs（CRDB）：按 created_at 删 7 天前 done/failed 状态记录
    """
    from loguru import logger
    from datetime import datetime, timezone, timedelta
    from .session import get_decode_logs_col, get_jobs_col
    from .cache_store import get_decode_log_buffer
    from config.settings import settings

    decode_logs_col = get_decode_logs_col()
    jobs_col = get_jobs_col()

    cleanup_cron_hours = int(getattr(settings, "CRDB_CLEANUP_CRON_HOURS", 6))
    cleanup_days = int(getattr(settings, "DATA_RETENTION_DAYS", 7))
    cleanup_batch_size = int(getattr(settings, "CRDB_CLEANUP_BATCH_SIZE", 5000))
    last_cleanup_at = 0.0

    while True:
        await asyncio.sleep(_DECODE_LOG_FLUSH_INTERVAL)
        try:
            buf = get_decode_log_buffer()
            rows = await buf._db.execute_fetchall(
                "SELECT id, file_code, requester_id, request_time, status, source_channel_id "
                "FROM decode_log_buffer ORDER BY id LIMIT 200"
            )
            if rows:
                # 分批写入,每批 100 条(减少单次 CRDB 事务大小)
                batch_size = 100
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

            # ── 0. 本地 SQLite 缓冲清理（0 RU）──────────────────
            try:
                await buf.cleanup_old(days=cleanup_days)
            except Exception as e:
                logger.warning(f"[DecodeLog] 本地缓冲清理异常: {e}")

            # ── 1. CRDB 端周期清理（每 cleanup_cron_hours 跑一次，代替废弃的 TTL job）────
            import time as _t
            now = _t.time()
            if now - last_cleanup_at < cleanup_cron_hours * 3600:
                continue  # 还没到清理周期
            last_cleanup_at = now

            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=cleanup_days)).isoformat()
            # CRDB decode_logs:按 request_time 删 7 天前记录（分批 5000 条避免大事务）
            try:
                deleted_total = 0
                while True:
                    deleted = await decode_logs_col.delete_many({
                        "request_time": {"$lt": cutoff_iso}
                    }, limit=cleanup_batch_size)
                    deleted_total += deleted
                    if deleted < cleanup_batch_size:
                        break
                    await asyncio.sleep(1)
                if deleted_total > 0:
                    logger.info(f"[Cleanup] decode_logs 删除 {deleted_total} 条 {cleanup_days} 天前记录")
            except Exception as e:
                logger.warning(f"[Cleanup] decode_logs 清理失败: {e}")

            # CRDB jobs:按 created_at 删 7 天前已完成/失败记录（分批，保留 dead_retry 队列）
            try:
                deleted_total = 0
                while True:
                    deleted = await jobs_col.delete_many({
                        "created_at": {"$lt": cutoff_iso},
                        "status": {"$in": ["done", "failed"]},
                    }, limit=cleanup_batch_size)
                    deleted_total += deleted
                    if deleted < cleanup_batch_size:
                        break
                    await asyncio.sleep(1)
                if deleted_total > 0:
                    logger.info(f"[Cleanup] jobs 删除 {deleted_total} 条 {cleanup_days} 天前已完成记录")
            except Exception as e:
                logger.warning(f"[Cleanup] jobs 清理失败: {e}")
        except Exception as e:
            logger.error(f"[DecodeLog] flush failed: {e}")


# ─── 热表增量同步：每 120 秒从 CRDB 拉取新记录到 SQLite ──────────

async def _sync_local_tables_loop():
    """每 120 秒从 CRDB 增量同步 4 张热表到本地 SQLite
    
    确保 SQLite 本地缓存与 CRDB 保持同步（最长 120 秒延迟）。
    增量查询：只拉取 updated_at > 上次同步时间的记录，极高性价比。
    """
    from loguru import logger
    from .cache_store import get_cache_store
    from .session import (
        get_file_records_col, get_codes_col, get_users_col,
        get_external_code_mapping_col,
    )
    import datetime as _dt

    while True:
        await asyncio.sleep(120)
        store = get_cache_store()
        if not store._db:
            continue

        last_sync = await store.get_kv("_last_sync_local_tables")
        last_sync_iso = last_sync or "1970-01-01T00:00:00+00:00"
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        total = 0

        try:
            # 1. file_records 增量
            fr_col = get_file_records_col()
            new_fr = await fr_col.find({"updated_at": {"$gt": last_sync_iso}}, limit=200)
            for r in new_fr:
                try:
                    await store.upsert_file_record_local(r, mark_dirty=False)
                    total += 1
                except Exception:
                    pass
            if new_fr:
                logger.debug(f"[Sync] file_records 增量: {len(new_fr)} 条")

            # 2. codes 增量
            codes_col = get_codes_col()
            new_codes = await codes_col.find({"updated_at": {"$gt": last_sync_iso}}, limit=200)
            for r in new_codes:
                try:
                    await store.upsert_code_local(r, mark_dirty=False)
                    total += 1
                except Exception:
                    pass
            if new_codes:
                logger.debug(f"[Sync] codes 增量: {len(new_codes)} 条")

            # 3. users 增量
            users_col = get_users_col()
            new_users = await users_col.find({"updated_at": {"$gt": last_sync_iso}}, limit=100)
            for r in new_users:
                try:
                    await store.upsert_user_local(r, mark_dirty=False)
                    total += 1
                except Exception:
                    pass
            if new_users:
                logger.debug(f"[Sync] users 增量: {len(new_users)} 条")

            # 4. external_code_mapping 增量
            ec_col = get_external_code_mapping_col()
            new_ec = await ec_col.find({"updated_at": {"$gt": last_sync_iso}}, limit=50)
            for r in new_ec:
                try:
                    await store._db.execute(
                        "INSERT OR REPLACE INTO external_code_mapping_local "
                        "(external_code, system_code, bot_username, created_at, updated_at, crdb_synced) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (r["external_code"], r.get("system_code", ""),
                         r.get("bot_username"), r.get("created_at"), r.get("updated_at")),
                    )
                    await store._db.commit()
                    total += 1
                except Exception:
                    pass

            await store.set_kv("_last_sync_local_tables", now_iso)
            if total > 0:
                logger.debug(f"[Sync] 本地表同步 {total} 条记录")

        except Exception as e:
            logger.warning(f"[Sync] 本地表同步失败: {e}")