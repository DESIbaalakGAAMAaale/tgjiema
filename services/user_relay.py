import asyncio
import tempfile
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import PeerChannel, PeerUser
from telethon.utils import pack_bot_file_id

from config import settings


class UserRelay:
    def __init__(self):
        self._client: TelegramClient | None = None
        self._decoder_bot_entity = None
        self._storage_channel_entity = None
        self._pending: dict[str, dict] = {}
        self._session_path = str(Path(__file__).parent.parent / "relay_session")
        self._ready = asyncio.Event()
        self._relay_api_id: int = 0
        self._relay_api_hash: str = ""
        self._relay_phone: str = ""
        self._pending_cleanup = None
        self._relay_user_id = None
        self._media_group_pending: dict[str, dict] = {}

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def relay_user_id(self) -> int | None:
        return self._relay_user_id

    def set_pending_cleanup(self, callback):
        self._pending_cleanup = callback

    async def _report_status(self, status: str):
        try:
            from database.session import set_config
            await set_config("relay_status", status)
        except Exception:
            pass

    async def _wait_for_admin_code(self) -> str | None:
        from database.session import get_config, set_config

        await set_config("relay_auth_pending", "1")
        await self._report_status("pending_auth")
        logger.info("[UserRelay] 验证码已发送到 Telegram，等待管理员通过管理机器人提交...")

        for i in range(100):
            await asyncio.sleep(3)
            code = await get_config("relay_auth_code")
            if code and code.strip():
                await set_config("relay_auth_pending", "0")
                await set_config("relay_auth_code", "")
                logger.info("[UserRelay] 已收到管理员提交的验证码")
                return code.strip()

        await set_config("relay_auth_pending", "0")
        logger.error("[UserRelay] 等待验证码超时（5分钟）")
        return None

    async def _load_config(self):
        api_id = settings.RELAY_API_ID or 0
        api_hash = settings.RELAY_API_HASH or ""
        phone = settings.RELAY_PHONE or ""

        if not api_id or not api_hash or not phone:
            try:
                from database.session import get_relay_config
                db_config = await get_relay_config()
                if db_config.get("api_id"):
                    api_id = db_config["api_id"]
                if db_config.get("api_hash"):
                    api_hash = db_config["api_hash"]
                if db_config.get("phone"):
                    phone = db_config["phone"]
                if api_id and api_hash and phone:
                    logger.info("[UserRelay] 使用数据库中配置的中继账号")
            except Exception as e:
                logger.warning(f"[UserRelay] 读取 DB 中继配置失败: {e}")

        self._relay_api_id = api_id
        self._relay_api_hash = api_hash
        self._relay_phone = phone

    async def start(self):
        await self._load_config()

        if not self._relay_api_id or not self._relay_api_hash or not self._relay_phone:
            logger.warning("[UserRelay] 未配置中继账号 (API_ID/HASH/PHONE)，跳过中继")
            await self._report_status("offline")
            return

        await self._report_status("connecting")

        self._client = TelegramClient(
            self._session_path,
            self._relay_api_id,
            self._relay_api_hash,
        )

        await self._client.connect()

        if not await self._client.is_user_authorized():
            await self._client.send_code_request(self._relay_phone)
            logger.info("[UserRelay] 验证码已发送到 Telegram 账号")

            code = settings.RELAY_CODE.strip() if settings.RELAY_CODE else None
            if not code:
                logger.info("[UserRelay] 环境变量 RELAY_CODE 未设置，等待管理员通过管理机器人提交...")
                code = await self._wait_for_admin_code()

            if not code:
                logger.error("[UserRelay] 无法获取验证码，登录失败")
                await self._report_status("offline")
                await self._client.disconnect()
                return

            try:
                await self._client.sign_in(self._relay_phone, code)
            except SessionPasswordNeededError:
                logger.error(
                    "[UserRelay] 该账号开启了二步验证，暂不支持。请关闭二步验证或使用无二步验证的账号。"
                )
                await self._report_status("offline")
                await self._client.disconnect()
                return
            except Exception as e:
                logger.error(f"[UserRelay] 登录失败: {e}")
                await self._report_status("offline")
                await self._client.disconnect()
                return

        me = await self._client.get_me()
        self._relay_user_id = me.id
        logger.info(f"[UserRelay] 已登录: {me.first_name} (@{me.username}), id={me.id}")

        try:
            self._decoder_bot_entity = await self._client.get_entity(
                settings.DECODER_BOT_USERNAME
            )
        except Exception as e:
            logger.warning(f"[UserRelay] 无法获取解码机器人 @{settings.DECODER_BOT_USERNAME}: {e}")

        try:
            self._storage_channel_entity = PeerChannel(settings.MAIN_STORAGE_CHANNEL_ID)
        except Exception:
            self._storage_channel_entity = None

        self._register_handlers()
        self._ready.set()
        await self._report_status("online")
        logger.info(f"[UserRelay] 中继已就绪")


    async def _cache_file_record(self, code: str, message_id: int, file_id: str = ""):
        try:
            from database import get_file_records_col, make_file_record
            from config import settings as _s

            files_col = get_file_records_col()
            existing = await files_col.find_one({"file_code": code})
            if existing:
                batch = existing.get("batch_msg_ids", "") or ""
                if isinstance(batch, str):
                    batch_ids = [mid for mid in batch.split(",") if mid.strip()]
                else:
                    batch_ids = [str(x) for x in batch] if isinstance(batch, list) else []
                if str(message_id) not in batch_ids:
                    batch_ids.append(str(message_id))

                fids = existing.get("file_ids", "") or ""
                if not isinstance(fids, str):
                    fids = str(fids)
                fid_list = [f for f in fids.split(",") if f.strip()]
                if file_id and file_id not in fid_list:
                    fid_list.append(file_id)

                update = {"$set": {"batch_msg_ids": ",".join(batch_ids)}}
                if file_id:
                    update["$set"]["file_ids"] = ",".join(fid_list)
                await files_col.update_one({"file_code": code}, update)
                logger.info(f"[UserRelay] 外部码 {code} 追加 msg_id={message_id}，batch={batch_ids}")
            else:
                record = make_file_record(
                    file_code=code,
                    uploader_id=0,
                    primary_channel_id=_s.MAIN_STORAGE_CHANNEL_ID,
                    primary_channel_msg_id=message_id,
                    file_types={},
                )
                if file_id:
                    record["file_ids"] = file_id
                await files_col.insert_one(record)
                logger.info(f"[UserRelay] 外部码 {code} 已缓存到本地存储")
        except Exception as e:
            logger.error(f"[UserRelay] 缓存外部码失败 (code={code}, msg_id={message_id}): {e}")

    def _extract_file_id(self, msg) -> str:
        if not msg or not msg.media:
            return ""
        try:
            return pack_bot_file_id(msg.media) or ""
        except Exception:
            return ""

    def _register_handlers(self):
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            sender = await event.get_sender()
            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return

            bot_username = sender.username

            media_group_id = getattr(event.message, "media_group_id", None)
            if media_group_id and media_group_id in self._media_group_pending:
                entry = self._media_group_pending[media_group_id]
                entry["events"].append(event)
                logger.info(
                    f"[UserRelay] 媒体组 {media_group_id} 暂存第 {len(entry['events'])} 条"
                )
                return

            if not bot_username or bot_username not in self._pending:
                return

            pending = self._pending.pop(bot_username, None)
            if not pending:
                return
            user_id = pending["user_id"]
            code = pending.get("code", "")

            logger.info(
                f"[UserRelay] 收到 @{bot_username} 响应 (user={user_id}, code={code})"
            )

            if self._pending_cleanup:
                self._pending_cleanup(bot_username)

            if media_group_id:
                self._media_group_pending[media_group_id] = {
                    "user_id": user_id,
                    "code": code,
                    "events": [event],
                }
                asyncio.create_task(
                    self._flush_media_group(media_group_id, user_id, code)
                )
                logger.info(f"[UserRelay] 媒体组 {media_group_id} 开始收集")
                return

            await self._relay_and_cache(event.message, user_id, code)

    async def _relay_and_cache(self, msg, user_id: int, code: str) -> bool:
        """Try forward to storage + user. Return True if user got it."""
        forwarded_to_user = False

        storage_msg_ids = await self._forward_to_storage([msg], code)

        if storage_msg_ids:
            try:
                await self._client.forward_messages(PeerUser(user_id), msg)
                forwarded_to_user = True
                logger.info(f"[UserRelay] 已直接转发给用户 {user_id}")
            except Exception as e:
                logger.info(f"[UserRelay] 无法直接转发给用户 {user_id}: {e}")

        if not forwarded_to_user:
            storage_msg_ids = storage_msg_ids or await self._forward_to_storage([msg], code)
            if storage_msg_ids and code and self._decoder_bot_entity:
                self._schedule_relay_deliver(user_id, code)

        return forwarded_to_user

    async def _forward_to_storage(self, messages: list, code: str) -> list[int] | None:
        if not code or not self._storage_channel_entity:
            return None
        try:
            fwd_msgs = await self._client.forward_messages(
                self._storage_channel_entity, messages
            )
            if not isinstance(fwd_msgs, list):
                fwd_msgs = [fwd_msgs]
            for i, m in enumerate(fwd_msgs):
                fid = self._extract_file_id(messages[i] if i < len(messages) else m)
                await self._cache_file_record(code, m.id, file_id=fid)
            logger.info(
                f"[UserRelay] 已转发 {len(fwd_msgs)} 条到存储频道并缓存 (含 file_id)"
            )
            return [m.id for m in fwd_msgs]
        except Exception as e:
            logger.warning(f"[UserRelay] 转发到存储频道失败: {e}")
            ids = []
            for msg in messages:
                mid = await self._download_and_cache(msg, code)
                if mid:
                    ids.append(mid)
                    fid = self._extract_file_id(msg)
                    if fid:
                        await self._cache_file_record(code, mid, file_id=fid)
            return ids if ids else None

    async def _download_and_cache(self, msg, code: str) -> int | None:
        is_video = getattr(msg, "video", None) is not None
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = await self._client.download_media(msg, file=tmpdir)
            if not file_path:
                text = getattr(msg, "message", None) or ""
                if text and self._decoder_bot_entity:
                    try:
                        await self._client.send_message(self._decoder_bot_entity, text)
                    except Exception as e:
                        logger.error(f"[UserRelay] 转发文本到解码机器人失败: {e}")
                return None

            send_kwargs = {"caption": getattr(msg, "message", None) or ""}
            if is_video:
                send_kwargs["video"] = True

            if self._decoder_bot_entity:
                try:
                    await self._client.send_file(
                        self._decoder_bot_entity, file_path, **send_kwargs
                    )
                except Exception as e:
                    logger.error(f"[UserRelay] 发送到解码机器人失败: {e}")

            if code and self._storage_channel_entity:
                try:
                    storage_msg = await self._client.send_file(
                        self._storage_channel_entity, file_path
                    )
                    await self._cache_file_record(code, storage_msg.id)
                    return storage_msg.id
                except Exception as e:
                    logger.error(f"[UserRelay] 缓存到存储频道失败: {e}")
        return None

    def _schedule_relay_deliver(self, user_id: int, code: str):
        async def _deliver():
            await asyncio.sleep(2)
            try:
                await self._client.send_message(
                    self._decoder_bot_entity,
                    f"RELAY_DELIVER:{user_id}:{code}",
                )
                logger.info(f"[UserRelay] 已通知解码机器人代发给用户 {user_id}")
            except Exception as e:
                logger.error(f"[UserRelay] 通知解码机器人失败: {e}")

        asyncio.create_task(_deliver())

    async def _flush_media_group(self, media_group_id, user_id: int, code: str):
        await asyncio.sleep(5)
        entry = self._media_group_pending.pop(media_group_id, None)
        if not entry:
            return

        events = entry["events"]
        messages = [e.message for e in events]
        logger.info(
            f"[UserRelay] 媒体组 {media_group_id} 共 {len(events)} 条，开始整体转发"
        )

        storage_ids = await self._forward_to_storage(messages, code)

        forwarded_to_user = False
        try:
            await self._client.forward_messages(PeerUser(user_id), messages)
            forwarded_to_user = True
            logger.info(f"[UserRelay] 媒体组已直接转发给用户 {user_id}")
        except Exception as e:
            logger.info(f"[UserRelay] 媒体组无法直接转发给用户 {user_id}: {e}")

        if not forwarded_to_user:
            if not storage_ids:
                storage_ids = await self._forward_to_storage(messages, code)
            if storage_ids and code and self._decoder_bot_entity:
                self._schedule_relay_deliver(user_id, code)

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        if not self._client:
            return False

        try:
            entity = await self._client.get_entity(bot_username)
            await self._client.send_message(entity, code)
            self._pending[bot_username] = {"user_id": user_id, "code": code}
            logger.info(f"[UserRelay] 已向 @{bot_username} 发送外部码，等待响应 (user={user_id}, code={code})")
            return True
        except Exception as e:
            logger.error(f"[UserRelay] 向 @{bot_username} 发送失败: {e}")
            return False

    async def deliver_cached(self, user_id: int, code: str) -> bool:
        if not self._client:
            logger.warning(f"[UserRelay] 无法交付缓存: 客户端未就绪")
            return False

        try:
            from database import get_file_records_col
            col = get_file_records_col()
            record = await col.find_one({"file_code": code})
            if not record:
                logger.warning(f"[UserRelay] 缓存交付: 码 {code} 无记录")
                return False

            file_ids_str = record.get("file_ids", "") or ""
            if not isinstance(file_ids_str, str):
                file_ids_str = str(file_ids_str)
            file_ids = [f.strip() for f in file_ids_str.split(",") if f.strip()]

            if file_ids:
                logger.info(
                    f"[UserRelay] 缓存交付: 码 {code}, {len(file_ids)} 条 file_id, 目标用户 {user_id}"
                )
                if self._decoder_bot_entity:
                    for fid in file_ids:
                        try:
                            await self._client.send_file(self._decoder_bot_entity, fid)
                        except Exception as e:
                            logger.error(f"[UserRelay] 发送 file_id 到解码机器人失败: {e}")
                if self._decoder_bot_entity:
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_DELIVER:{user_id}:{code}",
                    )
                    logger.info(f"[UserRelay] 已通知解码机器人代发给用户 {user_id}")
                return True

            logger.warning(f"[UserRelay] 缓存交付: 码 {code} 无 file_id，尝试从存储频道获取")
            ok = await self._deliver_via_storage_channel(user_id, code, record)
            if not ok:
                logger.warning(
                    f"[UserRelay] 缓存交付: 码 {code} 存储频道记录已过期，清除记录"
                )
                await col.delete_one({"file_code": code})
                if self._decoder_bot_entity:
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_RENEW:{user_id}:{code}",
                    )
                    logger.info(
                        f"[UserRelay] 已通知解码机器人: 码 {code} 需重新请求"
                    )
            return ok

        except Exception as e:
            logger.error(f"[UserRelay] 缓存交付失败 (code={code}, user={user_id}): {e}")
            return False

    async def _deliver_via_storage_channel(self, user_id: int, code: str, record: dict) -> bool:
        if not self._storage_channel_entity:
            return False
        msg_ids_raw = record.get("batch_msg_ids") or ""
        if not isinstance(msg_ids_raw, str):
            msg_ids_raw = str(msg_ids_raw)
        msg_ids = []
        if msg_ids_raw:
            msg_ids = [int(m) for m in msg_ids_raw.split(",") if m.strip().isdigit()]
        if not msg_ids:
            primary = record.get("primary_channel_msg_id")
            if primary:
                msg_ids = [primary]
        if not msg_ids:
            return False

        any_success = False
        for msg_id in msg_ids:
            msgs = await self._client.get_messages(
                self._storage_channel_entity, ids=msg_id
            )
            msg = msgs[0] if isinstance(msgs, list) else msgs
            if not msg:
                logger.warning(f"[UserRelay] 存储频道中未找到 msg_id={msg_id}")
                continue

            with tempfile.TemporaryDirectory() as tmpdir:
                file_path = await self._client.download_media(msg, file=tmpdir)
                if not file_path:
                    continue

                caption = getattr(msg, "message", None) or ""
                is_video = getattr(msg, "video", None) is not None
                send_kwargs = {"caption": caption}
                if is_video:
                    send_kwargs["video"] = True

                if self._decoder_bot_entity:
                    try:
                        await self._client.send_file(
                            self._decoder_bot_entity, file_path, **send_kwargs
                        )
                        any_success = True
                    except Exception as e:
                        logger.error(f"[UserRelay] 存储频道回退发送失败: {e}")

        if any_success and self._decoder_bot_entity:
            await self._client.send_message(
                self._decoder_bot_entity,
                f"RELAY_DELIVER:{user_id}:{code}",
            )

        return any_success

    async def backup_to_channel(self, source_channel_id: int, msg_ids: list[int], target_channel_id: int) -> bool:
        if not self._client:
            return False
        try:
            source = PeerChannel(source_channel_id)
            target = PeerChannel(target_channel_id)
            msgs = []
            for mid in msg_ids:
                result = await self._client.get_messages(source, ids=mid)
                m = result[0] if isinstance(result, list) else result
                if m:
                    msgs.append(m)
            if not msgs:
                logger.warning(f"[UserRelay] backup_to_channel: 源频道未找到任何有效消息")
                return False
            await self._client.forward_messages(target, msgs)
            logger.info(
                f"[UserRelay] backup_to_channel: 已转发 {len(msgs)} 条到 {target_channel_id}"
            )
            return True
        except Exception as e:
            logger.error(f"[UserRelay] backup_to_channel 失败: {e}")
            return False

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            await self._report_status("offline")
            logger.info("[UserRelay] 已断开连接")


user_relay = UserRelay()