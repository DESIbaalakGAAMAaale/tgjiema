import asyncio
import json
import re
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
        self._cursor = 0
        self._inaccessible_sources: set[int] = set()

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
        self.backup_channels = channels
        return token

    async def _load_cursor(self):
        from database.session import get_config
        try:
            val = await get_config(f"backup_{self.name}_cursor")
            self._cursor = int(val) if val else 0
        except Exception:
            self._cursor = 0

    async def _save_cursor(self):
        from database.session import set_config
        try:
            await set_config(f"backup_{self.name}_cursor", str(self._cursor))
        except Exception as e:
            logger.warning(f"[{self.name}] 保存游标失败: {e}")

    def _collect_msg_ids(self, record: dict) -> list[int]:
        msg_ids = []
        primary = record.get("primary_channel_msg_id")
        if primary:
            msg_ids.append(primary)
        batch_str = record.get("batch_msg_ids", "") or ""
        if batch_str:
            if not isinstance(batch_str, str):
                batch_str = str(batch_str)
            for mid in batch_str.split(","):
                mid = mid.strip()
                if mid.isdigit() and int(mid) not in msg_ids:
                    msg_ids.append(int(mid))
        return msg_ids

    async def run(self):
        from database import init_db
        await init_db()

        token = await self._load_config()
        if not token:
            logger.warning(f"[{self.name}] 未配置 Token，跳过启动")
            return

        bot = Bot(token=token)
        await self._load_cursor()
        logger.info(f"[{self.name}] 启动，游标={self._cursor}，目标频道: {self.backup_channels}")

        from services.user_relay import user_relay
        has_relay = user_relay.is_ready

        async def backup_loop():
            while True:
                try:
                    await self._scan_and_backup(bot, has_relay)
                except Exception as e:
                    logger.error(f"[{self.name}] 备份异常: {e}")
                    metrics.record_error(self.name)
                await asyncio.sleep(5)

        async def health_ping():
            while True:
                metrics.ping_bot(self.name)
                await asyncio.sleep(30)

        await asyncio.gather(backup_loop(), health_ping())

    async def _scan_and_backup(self, bot: Bot, has_relay: bool):
        col = get_file_records_col()

        records = await col.find(
            {"status": "active", "primary_channel_msg_id": {"$gte": self._cursor + 1}},
            sort=("primary_channel_msg_id", 1),
        )
        if not records:
            return

        for record in records:
            try:
                await self._backup_record(bot, col, record, has_relay)
            except Exception as e:
                logger.error(f"[{self.name}] 备份记录异常 (code={record.get('file_code')}): {e}")
                continue
            mid = record.get("primary_channel_msg_id", 0)
            if mid > self._cursor:
                self._cursor = mid

        await self._save_cursor()

    async def _backup_record(self, bot: Bot, col, record: dict, has_relay: bool) -> bool:
        file_code = record.get("file_code")
        source_channel = record.get("primary_channel_id")
        if not file_code or not source_channel:
            return False

        msg_ids = self._collect_msg_ids(record)
        if not msg_ids:
            return False

        existing_backups = record.get("backup_channel_msg_ids") or []
        if isinstance(existing_backups, str):
            try:
                existing_backups = json.loads(existing_backups)
            except (json.JSONDecodeError, TypeError):
                existing_backups = []

        updated = False
        for target_channel in self.backup_channels:
            backup_entry = None
            for b in existing_backups:
                if isinstance(b, dict) and b.get("channel_id") == target_channel:
                    backup_entry = b
                    break

            backed_mids = set(backup_entry.get("backed_msg_ids", [])) if backup_entry else set()
            missing = [mid for mid in msg_ids if mid not in backed_mids]
            if not missing:
                continue

            if has_relay:
                from services.user_relay import user_relay
                ok = await user_relay.backup_to_channel(source_channel, missing, target_channel)
                if ok:
                    backed_mids.update(missing)
                    logger.info(
                        f"[{self.name}] 消息 {missing} (码 {file_code}) 已通过中继备份到频道 {target_channel}"
                    )
                    metrics.backup_count += 1
                    updated = True
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
                    err_msg = str(e)
                    flood_match = re.search(r'Flood control exceeded\. Retry in (\d+) seconds', err_msg, re.IGNORECASE)
                    if flood_match:
                        wait = int(flood_match.group(1)) + 2
                        logger.warning(f"[{self.name}] Flood，等待 {wait} 秒")
                        await asyncio.sleep(wait)
                        continue
                    if "Message to copy not found" in err_msg or "message to forward not found" in err_msg.lower():
                        logger.warning(
                            f"[{self.name}] 消息 {mid} 未找到 (码 {file_code})，跳过"
                        )
                    else:
                        logger.error(f"[{self.name}] 备份 {mid} 失败: {err_msg}")
                    metrics.backup_fail_count += 1

            if backed_mids:
                updated = True

            if backup_entry:
                backup_entry["backed_msg_ids"] = sorted(backed_mids)
                backup_entry.setdefault("backup_bot", self.name)
            else:
                existing_backups.append({
                    "channel_id": target_channel,
                    "backup_bot": self.name,
                    "backed_msg_ids": sorted(backed_mids),
                })

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
