import time
from dataclasses import dataclass, field
from collections import defaultdict


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

    def get_bot(self, name: str) -> BotHealth:
        if name not in self.bots:
            self.bots[name] = BotHealth(name=name)
        return self.bots[name]

    def ping_bot(self, name: str):
        bot = self.get_bot(name)
        bot.is_running = True
        bot.last_ping = time.time()

    def record_error(self, name: str):
        bot = self.get_bot(name)
        bot.total_errors += 1

    def record_processed(self, name: str):
        bot = self.get_bot(name)
        bot.total_processed += 1


metrics = SystemMetrics()