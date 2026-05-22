import asyncio
from typing import List

from telegram import Bot
from telegram.ext import Application
from loguru import logger

from config import settings
from database import get_file_records_col
from utils.monitor import metrics


class BackupBot:
    def __init__(self, bot_token: str, backup_channel_ids: List[int], name: str):
        self.token = bot_token
        self.backup_channel_ids = backup_channel_ids
        self.name = name
        self._last_processed_msg_id = 0

    async def run(self):
        logger.info(f"启动备份机器人 {self.name}，目标频道: {self.backup_channel_ids}")
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
                    logger.error(f"备份机器人 {self.name} 异常: {e}")
                    metrics.record_error(self.name)
                await asyncio.sleep(5)

        await asyncio.gather(backup_loop(), health_ping())

    async def _scan_and_backup(self, bot: Bot):
        col = get_file_records_col()
        cursor = col.find().sort("primary_channel_msg_id", -1).limit(1)
        latest = await cursor.to_list(length=1)
        latest = latest[0] if latest else None

        if latest is None:
            return

        msg_id = latest.get("primary_channel_msg_id", 0)
        if msg_id <= self._last_processed_msg_id:
            return

        source_channel = latest.get("primary_channel_id")
        file_code = latest.get("file_code")

        for target_channel in self.backup_channel_ids:
            try:
                await bot.forward_message(
                    chat_id=target_channel,
                    from_chat_id=source_channel,
                    message_id=msg_id,
                )
                logger.info(
                    f"备份机器人 {self.name}: 文件 {file_code} 已备份到频道 {target_channel}"
                )
                metrics.backup_count += 1
                metrics.record_processed(self.name)

                await col.update_one(
                    {"file_code": file_code},
                    {
                        "$push": {
                            "backup_channel_msg_ids": {
                                "channel_id": target_channel,
                                "backup_bot": self.name,
                            }
                        }
                    },
                )
            except Exception as e:
                logger.error(
                    f"备份机器人 {self.name} 备份到频道 {target_channel} 失败: {e}"
                )
                metrics.backup_fail_count += 1

        self._last_processed_msg_id = msg_id


def run_backup_1():
    token = settings.BOT_TOKENS.get("BACKUP_BOT_1", "")
    bot = BackupBot(token, settings.BACKUP_CHANNELS_GROUP_1, "backup_bot_1")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_2():
    token = settings.BOT_TOKENS.get("BACKUP_BOT_2", "")
    bot = BackupBot(token, settings.BACKUP_CHANNELS_GROUP_2, "backup_bot_2")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_3():
    token = settings.BOT_TOKENS.get("BACKUP_BOT_3", "")
    bot = BackupBot(token, settings.BACKUP_CHANNELS_GROUP_3, "backup_bot_3")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "2":
        run_backup_2()
    elif len(sys.argv) > 1 and sys.argv[1] == "3":
        run_backup_3()
    else:
        run_backup_1()