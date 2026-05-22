import random
from typing import Optional

from config import settings


class ChannelSelector:
    def __init__(self):
        self._hot_backup_indices: set[int] = set()
        self._cold_backup_indices: set[int] = set()
        backup_count = len(settings.ALL_BACKUP_CHANNELS)
        if backup_count > 0:
            hot_count = max(1, backup_count // 2)
            all_indices = list(range(backup_count))
            self._hot_backup_indices = set(random.sample(all_indices, hot_count))
            self._cold_backup_indices = set(all_indices) - self._hot_backup_indices

    def select_channel(
        self, preferred_channel_id: Optional[int] = None
    ) -> Optional[int]:
        if preferred_channel_id and preferred_channel_id in settings.ALL_STORAGE_CHANNELS:
            return preferred_channel_id
        use_hot = random.random() < 0.8
        if use_hot and self._hot_backup_indices:
            idx = random.choice(list(self._hot_backup_indices))
            return settings.ALL_BACKUP_CHANNELS[idx]
        elif self._cold_backup_indices:
            idx = random.choice(list(self._cold_backup_indices))
            return settings.ALL_BACKUP_CHANNELS[idx]
        return settings.MAIN_STORAGE_CHANNEL_ID

    def rotate_pools(self):
        backup_count = len(settings.ALL_BACKUP_CHANNELS)
        if backup_count == 0:
            return
        hot_count = max(1, backup_count // 2)
        all_indices = list(range(backup_count))
        self._hot_backup_indices = set(random.sample(all_indices, hot_count))
        self._cold_backup_indices = set(all_indices) - self._hot_backup_indices


channel_selector = ChannelSelector()