"""动态限速器 — 根据队列长度自动调节请求间隔
高峰期自动拉长等待时间，防止 Telegram API 被打爆。
"""
import asyncio
import time


class DynamicRateLimiter:
    """动态限速器。

    核心逻辑:
    - 低负载: 基础延迟 (默认 0.2s)
    - 中负载 (10-30 jobs): 线性增加延迟
    - 高负载 (>30 jobs): 最大延迟 (默认 3s)
    - 负载下降: 快速恢复
    """

    def __init__(
        self,
        base_delay: float = None,
        max_delay: float = None,
        threshold_low: int = None,
        threshold_high: int = None,
        recovery_factor: float = 0.5,
    ):
        """
        Args:
            base_delay: 空闲时的基础延迟（秒）
            max_delay: 高峰期最大延迟（秒）
            threshold_low: 低负载阈值（jobs 数量 < 此值用 base_delay）
            threshold_high: 高负载阈值（jobs 数量 > 此值用 max_delay）
            recovery_factor: 负载下降时的恢复速度（0-1，越小恢复越慢）
        """
        # L4: 延迟读取 settings，消除 import 副作用（配置未就绪即崩溃）
        if base_delay is not None:
            self.base_delay = base_delay
        else:
            from config import settings
            self.base_delay = settings.RATE_LIMIT_BASE_DELAY
        if max_delay is not None:
            self.max_delay = max_delay
        else:
            from config import settings
            self.max_delay = settings.RATE_LIMIT_MAX_DELAY
        if threshold_low is not None:
            self.threshold_low = threshold_low
        else:
            from config import settings
            self.threshold_low = settings.RATE_LIMIT_THRESHOLD_LOW
        if threshold_high is not None:
            self.threshold_high = threshold_high
        else:
            from config import settings
            self.threshold_high = settings.RATE_LIMIT_THRESHOLD_HIGH
        # L5: 使用显式校验替代 assert（-O 模式下 assert 失效）
        if not self.threshold_high > self.threshold_low:
            raise ValueError(
                f"threshold_high ({self.threshold_high}) must be greater than "
                f"threshold_low ({self.threshold_low})"
            )
        self.recovery_factor = recovery_factor

        self._current_delay = self.base_delay
        self._last_queue_length = 0
        self._lock = asyncio.Lock()
        self._last_release_time: float = 0.0  # 上次释放许可的 monotonic 时间

    async def acquire(self, get_queue_length):
        """获取许可，自动等待（串行化）。

        通过在锁内 sleep 确保请求被串行化释放，避免并发请求同时通过。
        多个协程并发调用时会排队等待，每个协程至少间隔 _current_delay 秒。

        Args:
            get_queue_length: 异步函数，返回当前队列长度

        Returns:
            True 表示允许通过
        """
        async with self._lock:
            queue_length = await get_queue_length() if callable(get_queue_length) else 0

            # 根据队列长度计算目标延迟
            if queue_length <= self.threshold_low:
                target_delay = self.base_delay
            elif queue_length >= self.threshold_high:
                target_delay = self.max_delay
            else:
                # 线性插值
                ratio = (queue_length - self.threshold_low) / (self.threshold_high - self.threshold_low)
                target_delay = self.base_delay + ratio * (self.max_delay - self.base_delay)

            # 平滑过渡：避免延迟突变
            if target_delay < self._current_delay:
                # 负载下降：快速恢复
                self._current_delay = max(target_delay, self._current_delay * self.recovery_factor)
            else:
                # 负载上升：逐步增加
                self._current_delay = min(
                    target_delay,
                    self._current_delay + (target_delay - self._current_delay) * 0.3 + 0.05,
                )

            self._last_queue_length = queue_length

            # 计算需要等待的时间：距上次释放至少间隔 _current_delay 秒
            now = time.monotonic()
            elapsed = now - self._last_release_time if self._last_release_time > 0 else self._current_delay
            wait = max(0, self._current_delay - elapsed)

            # 在锁内 sleep，串行化所有并发请求
            if wait > 0:
                await asyncio.sleep(wait)

            self._last_release_time = time.monotonic()

        return True

    def get_stats(self) -> dict:
        """获取限速器状态。"""
        return {
            "current_delay": round(self._current_delay, 3),
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "last_queue_length": self._last_queue_length,
        }


# L4: 延迟创建全局单例，避免模块 import 时读取 settings
_dynamic_rate_limiter: "DynamicRateLimiter | None" = None


def _get_dynamic_rate_limiter() -> DynamicRateLimiter:
    global _dynamic_rate_limiter
    if _dynamic_rate_limiter is None:
        _dynamic_rate_limiter = DynamicRateLimiter()
    return _dynamic_rate_limiter


def __getattr__(name: str):
    if name == "dynamic_rate_limiter":
        return _get_dynamic_rate_limiter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
