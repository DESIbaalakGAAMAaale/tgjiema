"""按频道限流器 — 防止单个存储频道被频繁 copy_message 触发 429。
Telegram 对同一频道的操作限制约为 20 msg/min，留 5 个余量，
安全阈值设为 15 次/分钟。
"""

import time
import asyncio
from collections import defaultdict


class PerChannelRateLimiter:
    def __init__(self, max_per_minute: int = 15):
        self.max_per_minute = max_per_minute
        self._channels: dict[int, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def acquire(self, channel_id: int) -> float:
        """检查频道限流，返回需要等待的秒数（0 表示立即通过）。

        返回值：
            0.0 — 可以立即发送
            >0  — 需要等待这么多秒后再发送
        """
        now = time.monotonic()
        async with self._lock:
            # 清理过期条目（>60秒）
            self._channels[channel_id] = [
                t for t in self._channels[channel_id] if now - t < 60.0
            ]
            if len(self._channels[channel_id]) >= self.max_per_minute:
                # 计算最早的一条什么时候过期
                oldest = self._channels[channel_id][0]
                wait = 60.0 - (now - oldest)
                return max(wait, 0.0)
            # 记录本次操作
            self._channels[channel_id].append(now)
            return 0.0


_channel_limiter = PerChannelRateLimiter(max_per_minute=15)


async def acquire_channel_limit(channel_id: int) -> float:
    """按频道获取限流许可，返回需等待秒数。"""
    return await _channel_limiter.acquire(channel_id)