"""动态限速器 — 根据队列长度自动调节请求间隔
高峰期自动拉长等待时间，防止 Telegram API 被打爆。
"""
import asyncio
from config import settings


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
        self.base_delay = base_delay if base_delay is not None else settings.RATE_LIMIT_BASE_DELAY
        self.max_delay = max_delay if max_delay is not None else settings.RATE_LIMIT_MAX_DELAY
        self.threshold_low = threshold_low if threshold_low is not None else settings.RATE_LIMIT_THRESHOLD_LOW
        self.threshold_high = threshold_high if threshold_high is not None else settings.RATE_LIMIT_THRESHOLD_HIGH
        assert self.threshold_high > self.threshold_low, "threshold_high must be greater than threshold_low"
        self.recovery_factor = recovery_factor

        self._current_delay = self.base_delay
        self._last_queue_length = 0
        self._lock = asyncio.Lock()

    async def acquire(self, get_queue_length):
        """获取许可，自动等待。

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

        # 等待（在锁外执行，不影响并发）
        if self._current_delay > 0:
            await asyncio.sleep(self._current_delay)

        return True

    def get_stats(self) -> dict:
        """获取限速器状态。"""
        return {
            "current_delay": round(self._current_delay, 3),
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "last_queue_length": self._last_queue_length,
        }


# 全局单例
dynamic_rate_limiter = DynamicRateLimiter()
