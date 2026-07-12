import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger


_STALE_THRESHOLD = 300  # 5 分钟无 ping 视为离线

# 模块级通用计数器（L3：SystemMetrics.increment 的实际存储）
_counters: dict[str, int] = {}


@dataclass
class BotHealth:
    name: str
    is_running: bool = False
    last_ping: float = 0.0
    total_processed: int = 0
    total_errors: int = 0


@dataclass
class SystemMetrics:
    bots: dict[str, BotHealth] = field(default_factory=dict)
    upload_count: int = 0
    decode_count: int = 0
    send_success_count: int = 0
    send_fail_count: int = 0
    backup_count: int = 0
    backup_fail_count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get_bot(self, name: str) -> BotHealth:
        if name not in self.bots:
            self.bots[name] = BotHealth(name=name)
        return self.bots[name]

    async def ping_bot(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.is_running = True
            bot.last_ping = time.time()

    async def record_error(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.total_errors += 1

    async def record_processed(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.total_processed += 1

    async def increment(self, key: str, amount: int = 1):
        """通用计数器递增（如 mon.degrade）。

        L3: 实际自增模块级 _counters 并记录 debug 日志，
        替代原来的空 stub 实现。
        """
        async with self._lock:
            val = _counters.get(key, 0) + amount
            _counters[key] = val
            logger.debug("metric %s +%d -> %d", key, amount, val)

    async def record_send_success(self):
        """记录投递成功(统一写入 _counters,to_dict 也从 _counters 读取,保持一致)"""
        async with self._lock:
            val = _counters.get("dsp.send_success", 0) + 1
            _counters["dsp.send_success"] = val
            # P3: dataclass 字段作为缓存,与 _counters 保持同步
            self.send_success_count = val

    async def record_send_fail(self):
        """记录投递失败(统一写入 _counters,to_dict 也从 _counters 读取,保持一致)"""
        async with self._lock:
            val = _counters.get("dsp.send_fail", 0) + 1
            _counters["dsp.send_fail"] = val
            # P3: dataclass 字段作为缓存,与 _counters 保持同步
            self.send_fail_count = val

    @staticmethod
    def get_counter(key: str, default: int = 0) -> int:
        """读取指定计数器的当前值"""
        return _counters.get(key, default)

    async def set_counter(self, key: str, value: int):
        """设置计数器为指定值(用于绝对值指标,如账号存活数、队列积压)"""
        async with self._lock:
            _counters[key] = value

    @staticmethod
    def snapshot() -> dict:
        """返回当前所有通用计数器的快照（副本）。"""
        return dict(_counters)

    def get_stale_bots(self) -> list[str]:
        """返回所有超时的 bot 名称列表。"""
        now = time.time()
        return [
            name for name, bot in self.bots.items()
            if bot.last_ping > 0 and (now - bot.last_ping) > _STALE_THRESHOLD
        ]

    def to_dict(self) -> dict:
        """导出为字典格式,用于 admin 面板展示或 prometheus 导出。"""
        now = time.time()
        # P3: send_success/send_fail 优先从 _counters 读取,与 snapshot() 保持一致
        return {
            "bots": {
                name: {
                    "is_running": bot.is_running,
                    "last_ping_age": round(now - bot.last_ping, 1) if bot.last_ping > 0 else None,
                    "total_processed": bot.total_processed,
                    "total_errors": bot.total_errors,
                    "is_stale": bot.last_ping > 0 and (now - bot.last_ping) > _STALE_THRESHOLD,
                }
                for name, bot in self.bots.items()
            },
            "upload_count": self.upload_count,
            "decode_count": self.decode_count,
            # P3: 从 _counters 读取,确保与 snapshot() 数据源一致
            "send_success_count": _counters.get("dsp.send_success", self.send_success_count),
            "send_fail_count": _counters.get("dsp.send_fail", self.send_fail_count),
            "backup_count": self.backup_count,
            "backup_fail_count": self.backup_fail_count,
        }


metrics = SystemMetrics()


async def start_counter_reporter(role: str, interval: float = 60.0):
    """启动计数器定期上报任务(每 interval 秒将 _counters 写入 SQLite counter_snapshot)。

    各 bot 进程在启动时调用此函数,实现跨进程计数器聚合。
    mon_bot 通过 load_counter_snapshot() 读取全局聚合数据。
    """
    from database.cache_store import get_cache_store
    while True:
        try:
            counters = SystemMetrics.snapshot()
            if counters:
                await get_cache_store().save_counter_snapshot(counters, role)
        except Exception as e:
            logger.debug(f"[metrics] counter report failed ({role}): {e}")
        await asyncio.sleep(interval)