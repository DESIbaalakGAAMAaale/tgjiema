import asyncio
import json
import re

from loguru import logger
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from config import settings
from database import (
    get_file_records_col, get_backup_channels, get_backup_bot_tokens,
    save_message_backup, get_message_backups,
)
from utils.monitor import metrics
from utils.storage_channel import get_active_storage_channel_id


class BackupBot:
    def __init__(self, name: str, group: int):
        self.name = name
        self.group = group
        self.backup_channels: list[int] = []
        self._cursor = 0
        self._token: str | None = None

    async def _load_config(self):
        token = None
        if self.group == 1:
            token = settings.BACKUP_BOT_1_TOKEN
        elif self.group == 2:
            token = settings.BACKUP_BOT_2_TOKEN
        elif self.group == 3:
            token = settings.BACKUP_BOT_3_TOKEN

        try:
            tokens = await get_backup_bot_tokens()
            db_token = tokens.get(str(self.group))
            if db_token:
                token = db_token
        except Exception as e:
            logger.warning(f"[{self.name}] 读取 DB Token 失败: {e}")

        if not token:
            return False

        channels = await get_backup_channels(self.group)
        if not channels:
            if self.group == 1:
                channels = list(settings.BACKUP_CHANNELS_GROUP_1)
            elif self.group == 2:
                channels = list(settings.BACKUP_CHANNELS_GROUP_2)
            elif self.group == 3:
                channels = list(settings.BACKUP_CHANNELS_GROUP_3)
        self.backup_channels = channels
        self._token = token
        return True

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

    async def _handle_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        channel_post = update.channel_post
        if not channel_post:
            return

        chat_id = channel_post.chat_id
        message_id = channel_post.message_id

        active_channel = await get_active_storage_channel_id()
        if chat_id != active_channel:
            return

        logger.info(f"[{self.name}] 主频道新消息: msg_id={message_id}")

        backed_any = False
        for target_channel in self.backup_channels:
            try:
                backed = await channel_post.copy(chat_id=target_channel)
                await save_message_backup(message_id, target_channel, backed.message_id)
                logger.info(
                    f"[{self.name}] 实时备份 msg_id={message_id} → 频道 {target_channel} (backed_id={backed.message_id})"
                )
                metrics.backup_count += 1
                backed_any = True
            except Exception as e:
                err_msg = str(e)
                flood_match = re.search(r'Flood control exceeded\. Retry in (\d+) seconds', err_msg, re.IGNORECASE)
                if flood_match:
                    wait = int(flood_match.group(1)) + 2
                    logger.warning(f"[{self.name}] Flood，等待 {wait} 秒")
                    await asyncio.sleep(wait)
                    try:
                        backed = await channel_post.copy(chat_id=target_channel)
                        await save_message_backup(message_id, target_channel, backed.message_id)
                        logger.info(
                            f"[{self.name}] 重试备份 msg_id={message_id} → 频道 {target_channel} (backed_id={backed.message_id})"
                        )
                        metrics.backup_count += 1
                        backed_any = True
                    except Exception as e2:
                        logger.error(f"[{self.name}] 重试备份 {message_id} → 频道 {target_channel} 仍失败: {e2}")
                        metrics.backup_fail_count += 1
                else:
                    logger.error(f"[{self.name}] 实时备份 msg_id={message_id} → 频道 {target_channel} 失败: {err_msg}")
                    metrics.backup_fail_count += 1

        if backed_any:
            await self._try_update_file_record_backups(message_id)

    async def _try_update_file_record_backups(self, message_id: int):
        col = get_file_records_col()
        msg_str = str(message_id)

        records = await col.find({"batch_msg_ids": {"$regex": msg_str}})
        if not records:
            primary = await col.find_one({"primary_channel_msg_id": message_id})
            if primary:
                records = [primary]

        for record in records:
            await self._update_record_backup_info(col, record, message_id)

    async def _update_record_backup_info(self, col, record: dict, message_id: int):
        backups = await get_message_backups(message_id)
        if not backups:
            return

        file_code = record.get("file_code")
        if not file_code:
            return

        existing_backups = record.get("backup_channel_msg_ids") or []
        if isinstance(existing_backups, str):
            try:
                existing_backups = json.loads(existing_backups)
            except (json.JSONDecodeError, TypeError):
                existing_backups = []

        updated = False
        for b in backups:
            target_channel = b.get("backup_channel_id")
            backed_msg_id = b.get("backed_msg_id")
            if not target_channel or not backed_msg_id:
                continue

            entry = None
            for e in existing_backups:
                if isinstance(e, dict) and e.get("channel_id") == target_channel:
                    entry = e
                    break

            if entry:
                backed_ids = entry.get("backed_msg_ids", [])
                if isinstance(backed_ids, list) and backed_msg_id not in backed_ids:
                    backed_ids.append(backed_msg_id)
                    entry["backed_msg_ids"] = backed_ids
                    entry["backup_bot"] = self.name
                    updated = True
            else:
                existing_backups.append({
                    "channel_id": target_channel,
                    "backup_bot": self.name,
                    "backed_msg_ids": [backed_msg_id],
                })
                updated = True

        if updated:
            try:
                await col.update_one(
                    {"file_code": file_code},
                    {"$set": {"backup_channel_msg_ids": existing_backups}},
                )
                logger.info(f"[{self.name}] 已更新文件记录 {file_code} 的备份信息")
            except Exception as e:
                logger.error(f"[{self.name}] 更新文件记录 {file_code} 备份信息失败: {e}")

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

    async def _scan_and_backup(self, bot):
        col = get_file_records_col()

        records = await col.find(
            {"status": "active", "primary_channel_msg_id": {"$gte": self._cursor + 1}},
            sort=("primary_channel_msg_id", 1),
        )
        if not records:
            return

        for record in records:
            try:
                await self._backup_record(bot, col, record)
            except Exception as e:
                logger.error(f"[{self.name}] 备份记录异常 (code={record.get('file_code')}): {e}")
                continue
            mid = record.get("primary_channel_msg_id", 0)
            if mid > self._cursor:
                self._cursor = mid

        await self._save_cursor()

    async def _backup_record(self, bot, col, record: dict) -> bool:
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

            for mid in missing:
                try:
                    await bot.copy_message(
                        chat_id=target_channel,
                        from_chat_id=source_channel,
                        message_id=mid,
                    )
                    await save_message_backup(mid, target_channel, mid)
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

    async def _init(self):
        from database import init_db
        await init_db()

    async def run_app(self, app: Application):
        bot = app.bot

        await self._load_cursor()
        logger.info(
            f"[{self.name}] 启动实时监控，游标={self._cursor}，"
            f"目标备份频道: {self.backup_channels}"
        )

        async def scan_loop():
            while True:
                try:
                    await self._scan_and_backup(bot)
                except Exception as e:
                    logger.error(f"[{self.name}] 扫描备份异常: {e}")
                    metrics.record_error(self.name)
                await asyncio.sleep(5)

        async def health_ping():
            while True:
                metrics.ping_bot(self.name)
                await asyncio.sleep(30)

        loop = asyncio.get_running_loop()
        loop.create_task(scan_loop())
        loop.create_task(health_ping())


def _create_backup_app(name: str, group: int):
    import asyncio as _asyncio

    bot = BackupBot(name, group)

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(bot._init())

    if not loop.run_until_complete(bot._load_config()):
        logger.warning(f"[{name}] 未配置 Token，跳过启动")
        return

    logger.info(f"启动 {name} (group={group})...")
    app = Application.builder().token(bot._token).build()

    async def _handle_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await bot._handle_channel_post(update, context)

    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, _handle_post)
    )

    loop.run_until_complete(bot.run_app(app))
    app.run_polling()


def run_backup_1():
    _create_backup_app("backup_bot_1", 1)


def run_backup_2():
    _create_backup_app("backup_bot_2", 2)


def run_backup_3():
    _create_backup_app("backup_bot_3", 3)