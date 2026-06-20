import asyncio
import time
from config import settings


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self.period_seconds]
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True


class UserRateLimiter:
    def __init__(self, max_calls_per_minute: int = None):
        self.max_calls = max_calls_per_minute or settings.RATE_LIMIT_PER_USER_PER_MINUTE
        self._users: dict[int, list[float]] = defaultdict(list)
        self._acquire_count = 0
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._users[user_id] = [
                t for t in self._users[user_id] if now - t < 60
            ]
            if len(self._users[user_id]) >= self.max_calls:
                return False
            self._users[user_id].append(now)
            return True


global_rate_limiter = RateLimiter(
    max_calls=settings.RATE_LIMIT_GLOBAL_PER_SECOND, period_seconds=1.0
)
user_rate_limiter = UserRateLimiter()