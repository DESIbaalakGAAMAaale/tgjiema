import asyncio
import time
from collections import defaultdict


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
        # L4: 延迟读取 settings，消除 import 副作用（配置未就绪即崩溃）
        self._max_calls = max_calls_per_minute
        self._users: dict[int, list[float]] = defaultdict(list)
        self._acquire_count = 0
        self._lock = asyncio.Lock()
        self._last_cleanup = time.monotonic()

    @property
    def max_calls(self) -> int:
        """首次访问时懒读 settings.RATE_LIMIT_PER_USER_PER_MINUTE。"""
        if self._max_calls is None:
            from config import settings
            self._max_calls = settings.RATE_LIMIT_PER_USER_PER_MINUTE
        return self._max_calls

    async def acquire(self, user_id: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._users[user_id] = [
                t for t in self._users[user_id] if now - t < 60
            ]
            # 每 10 分钟清理一次过期用户条目，防止内存泄漏
            if now - self._last_cleanup > 600:
                stale = [uid for uid, ts_list in self._users.items() if not ts_list]
                for uid in stale:
                    del self._users[uid]
                self._last_cleanup = now
            if len(self._users[user_id]) >= self.max_calls:
                return False
            self._users[user_id].append(now)
            return True


# L4: 延迟创建全局单例，避免模块 import 时读取 settings
_global_rate_limiter: RateLimiter | None = None
_user_rate_limiter: UserRateLimiter | None = None


def _get_global_rate_limiter() -> RateLimiter:
    global _global_rate_limiter
    if _global_rate_limiter is None:
        from config import settings
        _global_rate_limiter = RateLimiter(
            max_calls=settings.RATE_LIMIT_GLOBAL_PER_SECOND, period_seconds=1.0
        )
    return _global_rate_limiter


def _get_user_rate_limiter() -> UserRateLimiter:
    global _user_rate_limiter
    if _user_rate_limiter is None:
        _user_rate_limiter = UserRateLimiter()
    return _user_rate_limiter


# 向后兼容：保持原有的模块级属性访问方式
# 通过 __getattr__ 在首次访问时懒初始化
def __getattr__(name: str):
    if name == "global_rate_limiter":
        return _get_global_rate_limiter()
    if name == "user_rate_limiter":
        return _get_user_rate_limiter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")