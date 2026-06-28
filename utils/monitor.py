import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict


_STALE_THRESHOLD = 300  # 5 分钟无 ping 视为离线


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
    _lock = asyncio.Lock()

    def get_bot(self, name: str) -> BotHealth:
        if name not in self.bots:
            self.bots[name] = BotHealth(name=name)
        return self.bots[name]

    async def ping_bot(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.is_running = True
            bot.last_ping = time.monotonic()

    async def record_error(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.total_errors += 1

    async def record_processed(self, name: str):
        async with self._lock:
            bot = self.get_bot(name)
            bot.total_processed += 1

    async def increment(self, key: str):
        """通用计数器递增（如 mon.degrade）。"""
        async with self._lock:
            if key == "mon.degrade":
                pass  # 降级计数仅用于日志，无额外聚合

    def get_stale_bots(self) -> list[str]:
        """返回所有超时的 bot 名称列表。"""
        now = time.monotonic()
        return [
            name for name, bot in self.bots.items()
            if bot.last_ping > 0 and (now - bot.last_ping) > _STALE_THRESHOLD
        ]

    def to_dict(self) -> dict:
        """导出为字典格式,用于 admin 面板展示或 prometheus 导出。"""
        now = time.monotonic()
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
            "send_success_count": self.send_success_count,
            "send_fail_count": self.send_fail_count,
            "backup_count": self.backup_count,
            "backup_fail_count": self.backup_fail_count,
        }


metrics = SystemMetrics()