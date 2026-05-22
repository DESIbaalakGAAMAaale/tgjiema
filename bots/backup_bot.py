import asyncio
import json
import io

from loguru import logger
from telegram import Bot
from telegram.ext import Application

from config import settings
from database import get_file_records_col
from storage import get_r2
from utils.monitor import metrics


class BackupBot:
    def __init__(self, bot_token: str, name: str):
        self.token = bot_token
        self.name = name
        self._last_processed_msg_id = 0

    async def run(self):
        logger.info(f"启动备份机器人 {self.name}，目标: R2 -> {settings.R2_BUCKET_NAME}")
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
        latest_list = await col.find(sort=("primary_channel_msg_id", -1), limit=1)
        latest = latest_list[0] if latest_list else None

        if latest is None:
            return

        msg_id = latest.get("primary_channel_msg_id", 0)
        if msg_id <= self._last_processed_msg_id:
            return

        source_channel = latest.get("primary_channel_id")
        file_code = latest.get("file_code")

        r2_key = f"{self.name}/{file_code}_{msg_id}"
        try:
            tg_file = await bot.get_file(
                file_id=None,
                chat_id=source_channel,
                message_id=msg_id,
            )
            if tg_file is None:
                file_msg = await bot.forward_message(
                    chat_id=settings.MAIN_STORAGE_CHANNEL_ID,
                    from_chat_id=source_channel,
                    message_id=msg_id,
                )
                tg_file_info = file_msg.document or file_msg.video or file_msg.photo or file_msg.audio
                if not tg_file_info:
                    logger.error(f"备份机器人 {self.name}: 无法获取文件信息 {file_code}")
                    return
                main_channel_msg = file_msg
            else:
                main_channel_msg = None

            tg_file_obj = main_channel_msg.document or main_channel_msg.video or main_channel_msg.photo or main_channel_msg.audio if main_channel_msg else None
            if tg_file_obj:
                downloaded = await tg_file_obj.download_to_memory()
                if hasattr(downloaded, 'read'):
                    data = downloaded.read()
                elif isinstance(downloaded, bytes):
                    data = downloaded
                else:
                    data = None
            else:
                downloaded = await bot.forward_message(
                    chat_id=settings.MAIN_STORAGE_CHANNEL_ID,
                    from_chat_id=source_channel,
                    message_id=msg_id,
                )
                doc = downloaded.document or downloaded.video or downloaded.photo or downloaded.audio
                if doc:
                    file_bytes = await doc.download_to_memory()
                    if hasattr(file_bytes, 'read'):
                        data = file_bytes.read()
                    elif isinstance(file_bytes, bytes):
                        data = file_bytes
                    else:
                        data = None
                else:
                    data = None

            if data is None:
                logger.error(f"备份机器人 {self.name}: 无法下载文件 {file_code}")
                metrics.backup_fail_count += 1
                return

        except Exception as e:
            logger.error(f"备份机器人 {self.name}: 下载文件 {file_code} 失败: {e}")
            metrics.backup_fail_count += 1
            return

        r2 = get_r2()
        try:
            await r2.upload(r2_key, data)
            logger.info(f"备份机器人 {self.name}: 文件 {file_code} 已上传到 R2 -> {r2_key}")
            metrics.backup_count += 1
            metrics.record_processed(self.name)

            backup_entry = {
                "channel_id": settings.MAIN_STORAGE_CHANNEL_ID,
                "backup_bot": self.name,
                "r2_key": r2_key,
            }
            backup_json = json.dumps(backup_entry)
            existing = latest.get("r2_backup_keys") or []
            if isinstance(existing, str):
                existing = json.loads(existing) if existing else []
            existing.append(backup_entry)

            await col.update_one(
                {"file_code": file_code},
                {"$set": {"r2_backup_keys": existing}},
            )
        except Exception as e:
            logger.error(f"备份机器人 {self.name}: R2 上传 {file_code} 失败: {e}")
            metrics.backup_fail_count += 1

        self._last_processed_msg_id = msg_id


def _init_sync():
    import asyncio as _asyncio
    async def _do():
        from database import init_db
        from storage import init_r2
        await init_db()
        await init_r2()
    _asyncio.get_event_loop().run_until_complete(_do())


def run_backup_1():
    _init_sync()
    token = settings.BOT_TOKENS.get("BACKUP_BOT_1", "")
    bot = BackupBot(token, "backup_bot_1")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_2():
    _init_sync()
    token = settings.BOT_TOKENS.get("BACKUP_BOT_2", "")
    bot = BackupBot(token, "backup_bot_2")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())


def run_backup_3():
    _init_sync()
    token = settings.BOT_TOKENS.get("BACKUP_BOT_3", "")
    bot = BackupBot(token, "backup_bot_3")
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.run())