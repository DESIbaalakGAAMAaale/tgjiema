import asyncio
from loguru import logger
from telegram import Bot
from telegram.ext import Application
from config import settings
from database import get_file_records_col
from utils.monitor import metrics


class BackupBot:
    def __init__(self, bot_token: str, name: str, backup_channels: list[int]):
        self.token = bot_token
        self.name = name
        self.backup_channels = backup_channels
        self._last_processed_msg_id = 0

    async def run(self):
        logger.info(f"启动备份机器人 {self.name}，目标频道: {self.backup_channels}")
        app = Application.builder().token(self.token).build()
        bot = app.bot
        metrics.ping_bot(self.name)

        async def health_ping():
            while True:
                metrics.ping_bot(self.name)
                await asyncio.sleep(30)

        async def backup_loop():
            while True:
                try:
                    await self._scan_and_backup(bot)
                except Exception as e:
                    logger.error(f"[{self.name}] 备份异常: {e}")
                    metrics.record_error(self.name)
                await asyncio.sleep(5)

        await asyncio.gather(backup_loop(), health_ping())

    async def _scan_and_backup(self, bot: Bot):
        col = get_file_records_col()
        latest_list = await col.find(sort=("primary_channel_msg_id", -1), limit=1)
        latest = latest_list[0] if latest_list else None
        if latest is None:
            return

        msg_id = latest.get("primary_channel_msg_id", 0)
        if msg_id <= self._last_processed_msg_id:
            return

        source_channel = latest.get("primary_channel_id")
        file_code = latest.get("file_code")

        existing_backups = latest.get("backup_channel_msg_ids") or []
        if isinstance(existing_backups, str):
            existing_backups = []

        for target_channel in self.backup_channels:
            already_done = any(
                b.get("channel_id") == target_channel for b in existing_backups
            )
            if already_done:
                continue
            try:
                await bot.forward_message(
                    chat_id=target_channel,
                    from_chat_id=source_channel,
                    message_id=msg_id,
                )
                entry = {"channel_id": target_channel, "backup_bot": self.name}
                existing_backups.append(entry)
                await col.update_one(
                    {"file_code": file_code},
                    {"$set": {"backup_channel_msg_ids": existing_backups}},
                )
                logger.info(f"[{self.name}] 文件 {file_code} 已转发到频道 {target_channel}")
                metrics.backup_count += 1
                metrics.record_processed(self.name)
            except Exception as e:
                logger.error(f"[{self.name}] 转发 {file_code} 到频道 {target_channel} 失败: {e}")
                metrics.backup_fail_count += 1

        self._last_processed_msg_id = msg_id


def _init_sync():
    async def _do():
        from database import init_db
        await init_db()
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(_do())


def run_backup_1():
    _init_sync()
    token = settings.BACKUP_BOT_1_TOKEN
    bot = BackupBot(token, "backup_bot_1", settings.BACKUP_CHANNELS_GROUP_1)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_2():
    _init_sync()
    token = settings.BACKUP_BOT_2_TOKEN
    bot = BackupBot(token, "backup_bot_2", settings.BACKUP_CHANNELS_GROUP_2)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_3():
    _init_sync()
    token = settings.BACKUP_BOT_3_TOKEN
    bot = BackupBot(token, "backup_bot_3", settings.BACKUP_CHANNELS_GROUP_3)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())