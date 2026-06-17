import asyncio
import time
from collections import defaultdict
from config import settings


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._calls: list[float] = []

    def acquire(self) -> bool:
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self.period_seconds]
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True


class TokenBucketRateLimiter:
    """Token Bucket 限流器 — 允许短期突发，不丢请求。

    原理：每秒往池子里灌 rate 个 token，池子最大容量 burst。
    使用时从池子里拿 token，池子空了就等下一批。
    """

    def __init__(self, rate: float = 25.0, burst: int = 35):
        """
        rate: 每秒生成的 token 数（建议 25-30，留余量给 copy_message 等操作）
        burst: 最大缓冲 token 数（允许短期突发）
        """
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 5.0) -> bool:
        """获取一个 token，超时则返回 False。"""
        start = time.monotonic()
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                # 估算等待时间
                wait = (1.0 - self._tokens) / self.rate

            if time.monotonic() - start >= timeout:
                return False

            await asyncio.sleep(wait)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now


class UserRateLimiter:
    def __init__(self, max_calls_per_minute: int = None):
        self.max_calls = max_calls_per_minute or settings.RATE_LIMIT_PER_USER_PER_MINUTE
        self._users: dict[int, list[float]] = defaultdict(list)
        self._acquire_count = 0

    def acquire(self, user_id: int) -> bool:
        now = time.monotonic()
        # 每 1000 次调用清理一次已过期的用户条目，防止内存泄漏
        self._acquire_count += 1
        if self._acquire_count % 1000 == 0:
            stale = [uid for uid, calls in self._users.items()
                     if not calls or now - calls[-1] >= 3600]
            for uid in stale:
                del self._users[uid]
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