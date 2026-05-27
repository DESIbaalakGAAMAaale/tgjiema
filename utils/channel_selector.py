import random
from typing import Optional

from config import settings


class ChannelSelector:
    def __init__(self):
        pass

    async def select_channel(
        self, preferred_channel_id: Optional[int] = None
    ) -> Optional[int]:
        from utils.storage_channel import get_active_storage_channel_id, invalidate_cache
        main_channel = await get_active_storage_channel_id()
        all_channels = [main_channel] + list(settings.ALL_BACKUP_CHANNELS)

        if preferred_channel_id and preferred_channel_id in all_channels:
            return preferred_channel_id

        all_backups = list(settings.ALL_BACKUP_CHANNELS)
        if not all_backups:
            return main_channel
        backup_count = len(all_backups)
        hot_count = max(1, backup_count // 2)
        all_indices = list(range(backup_count))
        hot_indices = set(random.sample(all_indices, hot_count))
        cold_indices = set(all_indices) - hot_indices
        use_hot = random.random() < 0.8
        if use_hot and hot_indices:
            idx = random.choice(list(hot_indices))
            return all_backups[idx]
        elif cold_indices:
            idx = random.choice(list(cold_indices))
            return all_backups[idx]
        return all_backups[0]


channel_selector = ChannelSelector()