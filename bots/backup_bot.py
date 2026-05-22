import asyncio
from loguru import logger
from telegram import Bot
from config import settings
from database import get_file_records_col, get_backup_channels, get_backup_bot_tokens
from utils.monitor import metrics


class BackupBot:
    def __init__(self, name: str, group: int):
        self.name = name
        self.group = group
        self.backup_channels: list[int] = []
        self._full_sync_done: bool = False
        self._sync_offset: int = 0
        self._last_synced_id: int = 0
        self._max_seen_id: int = 0
        self._quick_cursor: int = 0

    async def _load_config(self):
        from config import settings as _s

        token = None
        if self.group == 1:
            token = _s.BACKUP_BOT_1_TOKEN
        elif self.group == 2:
            token = _s.BACKUP_BOT_2_TOKEN
        elif self.group == 3:
            token = _s.BACKUP_BOT_3_TOKEN

        try:
            tokens = await get_backup_bot_tokens()
            db_token = tokens.get(str(self.group))
            if db_token:
                token = db_token
                logger.info(f"[{self.name}] 使用数据库中配置的 Token")
        except Exception as e:
            logger.warning(f"[{self.name}] 读取 DB Token 失败: {e}")

        if not token:
            return None

        channels = await get_backup_channels(self.group)
        if not channels:
            if self.group == 1:
                channels = list(_s.BACKUP_CHANNELS_GROUP_1)
            elif self.group == 2:
                channels = list(_s.BACKUP_CHANNELS_GROUP_2)
            elif self.group == 3:
                channels = list(_s.BACKUP_CHANNELS_GROUP_3)
            if channels:
                logger.info(f"[{self.name}] 使用 .env 中配置的频道列表")

        self.backup_channels = channels
        return token

    async def _refresh_channels(self):
        new_channels = await get_backup_channels(self.group)
        if not new_channels:
            return
        if set(new_channels) != set(self.backup_channels):
            added = set(new_channels) - set(self.backup_channels)
            removed = set(self.backup_channels) - set(new_channels)
            self.backup_channels = new_channels
            if added:
                logger.info(f"[{self.name}] 检测到新增备份频道: {added}，触发全量同步")
                self._full_sync_done = False
                self._sync_offset = 0
            if removed:
                logger.info(f"[{self.name}] 检测到移除备份频道: {removed}")

    async def run(self):
        from database import init_db
        await init_db()

        token = await self._load_config()
        if not token:
            logger.warning(f"[{self.name}] 未配置 Token，跳过启动")
            return

        logger.info(f"启动备份机器人 {self.name}，目标频道: {self.backup_channels}")
        bot = Bot(token=token)
        metrics.ping_bot(self.name)

        async def health_ping():
            while True:
                metrics.ping_bot(self.name)
                await asyncio.sleep(30)

        async def backup_loop():
            while True:
                try:
                    await self._refresh_channels()
                    await self._scan_and_backup(bot)
                except Exception as e:
                    logger.error(f"[{self.name}] 备份异常: {e}")
                    metrics.record_error(self.name)
                await asyncio.sleep(5)

        await asyncio.gather(backup_loop(), health_ping())

    def _collect_msg_ids(self, record: dict) -> list[int]:
        msg_ids = []
        primary = record.get("primary_channel_msg_id")
        if primary:
            msg_ids.append(primary)
        batch_str = record.get("batch_msg_ids", "") or ""
        if batch_str:
            for mid in batch_str.split(","):
                mid = mid.strip()
                if mid.isdigit():
                    msg_ids.append(int(mid))
        return list(dict.fromkeys(msg_ids))

    async def _backup_record(self, bot: Bot, col, record: dict) -> bool:
        file_code = record.get("file_code")
        if not file_code:
            return False

        source_channel = record.get("primary_channel_id")
        msg_ids = self._collect_msg_ids(record)

        existing_backups = record.get("backup_channel_msg_ids") or []
        if isinstance(existing_backups, str):
            existing_backups = []

        updated = False
        for target_channel in self.backup_channels:
            backup_entry = None
            for b in existing_backups:
                if b.get("channel_id") == target_channel:
                    backup_entry = b
                    break

            backed_mids = set(backup_entry.get("backed_msg_ids", [])) if backup_entry else set()
            missing = [mid for mid in msg_ids if mid not in backed_mids]
            if not missing:
                continue

            for mid in missing:
                try:
                    await bot.copy_message(
                        chat_id=target_channel,
                        from_chat_id=source_channel,
                        message_id=mid,
                    )
                    backed_mids.add(mid)
                    logger.info(
                        f"[{self.name}] 消息 {mid} (码 {file_code}) 已备份到频道 {target_channel}"
                    )
                    metrics.backup_count += 1
                except Exception as e:
                    logger.error(
                        f"[{self.name}] 备份消息 {mid} 到频道 {target_channel} 失败: {e}"
                    )
                    metrics.backup_fail_count += 1

            if backup_entry:
                backup_entry["backed_msg_ids"] = sorted(backed_mids)
                backup_entry["backup_bot"] = self.name
            else:
                existing_backups.append({
                    "channel_id": target_channel,
                    "backup_bot": self.name,
                    "backed_msg_ids": sorted(backed_mids),
                })
            updated = True

        if updated:
            try:
                await col.update_one(
                    {"file_code": file_code},
                    {"$set": {"backup_channel_msg_ids": existing_backups}},
                )
                metrics.record_processed(self.name)
            except Exception as e:
                logger.error(f"[{self.name}] 更新备份记录 {file_code} 失败: {e}")

        return updated

    async def _scan_and_backup(self, bot: Bot):
        col = get_file_records_col()

        if self._full_sync_done:
            await self._delta_sync(bot, col)
        else:
            await self._full_sync(bot, col)
            await self._quick_sync(bot, col)

    async def _full_sync(self, bot: Bot, col):
        BATCH = 100
        records = await col.find(
            {"status": "active"},
            sort=("primary_channel_msg_id", 1),
            skip=self._sync_offset,
            limit=BATCH,
        )

        if not records:
            self._full_sync_done = True
            self._last_synced_id = self._max_seen_id
            self._sync_offset = 0
            logger.info(f"[{self.name}] ✅ 全量备份完成，进入增量模式，游标={self._last_synced_id}")
            return

        for record in records:
            await self._backup_record(bot, col, record)
            mid = record.get("primary_channel_msg_id", 0)
            if mid > self._max_seen_id:
                self._max_seen_id = mid

        self._sync_offset += len(records)
        logger.info(
            f"[{self.name}] 🔄 全量备份中: {self._sync_offset} 条已处理"
        )

    async def _quick_sync(self, bot: Bot, col):
        if self._quick_cursor == 0:
            records = await col.find(
                {"status": "active"},
                sort=("primary_channel_msg_id", -1),
                limit=50,
            )
            if not records:
                return
            for record in records:
                await self._backup_record(bot, col, record)
                mid = record.get("primary_channel_msg_id", 0)
                if mid > self._quick_cursor:
                    self._quick_cursor = mid
        else:
            records = await col.find(
                {"status": "active", "primary_channel_msg_id": {"$gte": self._quick_cursor + 1}},
                sort=("primary_channel_msg_id", 1),
            )
            if not records:
                return
            for record in records:
                await self._backup_record(bot, col, record)
                mid = record.get("primary_channel_msg_id", 0)
                if mid > self._quick_cursor:
                    self._quick_cursor = mid

    async def _delta_sync(self, bot: Bot, col):
        records = await col.find(
            {"status": "active", "primary_channel_msg_id": {"$gte": self._last_synced_id + 1}},
            sort=("primary_channel_msg_id", 1),
        )
        if not records:
            return

        for record in records:
            await self._backup_record(bot, col, record)
            mid = record.get("primary_channel_msg_id", 0)
            if mid > self._last_synced_id:
                self._last_synced_id = mid


def run_backup_1():
    bot = BackupBot("backup_bot_1", 1)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_2():
    bot = BackupBot("backup_bot_2", 2)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_3():
    bot = BackupBot("backup_bot_3", 3)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())