"""频道选择器（环形冗余架构 v2）
从 cells 表获取 Active/Shadow 频道。
"""

import random
from typing import Optional


class ChannelSelector:
    async def select_channel(
        self, preferred_channel_id: Optional[int] = None
    ) -> Optional[int]:
        """选择一个可用的存储/Shadow 频道。"""
        from utils.storage_channel import get_active_storage_channel_id

        main_channel = await get_active_storage_channel_id()
        shadow_channels = await self._get_shadow_channels()

        all_channels = [main_channel] + shadow_channels

        if preferred_channel_id and preferred_channel_id in all_channels:
            return preferred_channel_id

        if not shadow_channels:
            return main_channel

        backup_count = len(shadow_channels)
        hot_count = max(1, backup_count // 2)
        all_indices = list(range(backup_count))
        hot_indices = set(random.sample(all_indices, hot_count))
        cold_indices = set(all_indices) - hot_indices
        use_hot = random.random() < 0.8
        if use_hot and hot_indices:
            idx = random.choice(list(hot_indices))
            return shadow_channels[idx]
        elif cold_indices:
            idx = random.choice(list(cold_indices))
            return shadow_channels[idx]
        return shadow_channels[0]

    @staticmethod
    async def _get_shadow_channels() -> list[int]:
        """从 cells 表获取所有 shadow 频道的 channel_id。"""
        try:
            from database import get_cells_col
            col = get_cells_col()
            rows = await col.find({"status": {"$in": ["shadow1", "shadow2"]}})
            return [r["channel_id"] for r in rows]
        except Exception:
            return []


channel_selector = ChannelSelector()