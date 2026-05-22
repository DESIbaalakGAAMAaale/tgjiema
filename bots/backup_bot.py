import asyncio
from loguru import logger
from telegram import Bot
from config import settings
from database import get_file_records_col
from utils.monitor import metrics


class BackupBot:
    def __init__(self, bot_token: str, name: str, backup_channels: list[int]):
        self.token = bot_token
        self.name = name
        self.backup_channels = backup_channels
        self._processed_ids: set[str] = set()

    async def run(self):
        from database import init_db
        await init_db()

        logger.info(f"启动备份机器人 {self.name}，目标频道: {self.backup_channels}")
        bot = Bot(token=self.token)
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

    async def _scan_and_backup(self, bot: Bot):
        col = get_file_records_col()
        records = await col.find(
            {"status": "active"},
            sort=("primary_channel_msg_id", -1),
            limit=50,
        )
        if not records:
            return

        for record in records:
            file_code = record.get("file_code")
            if not file_code:
                continue

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

        if len(self._processed_ids) > 2000:
            self._processed_ids.clear()
        self._processed_ids.add(self.name)


def run_backup_1():
    token = settings.BACKUP_BOT_1_TOKEN
    if not token:
        return
    bot = BackupBot(token, "backup_bot_1", settings.BACKUP_CHANNELS_GROUP_1)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_2():
    token = settings.BACKUP_BOT_2_TOKEN
    if not token:
        return
    bot = BackupBot(token, "backup_bot_2", settings.BACKUP_CHANNELS_GROUP_2)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_3():
    token = settings.BACKUP_BOT_3_TOKEN
    if not token:
        return
    bot = BackupBot(token, "backup_bot_3", settings.BACKUP_CHANNELS_GROUP_3)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())