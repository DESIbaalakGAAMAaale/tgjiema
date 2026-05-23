import asyncio
import tempfile
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import PeerChannel
from telethon.utils import pack_bot_file_id

from config import settings


class UserRelay:
    def __init__(self):
        self._client: TelegramClient | None = None
        self._decoder_bot_entity = None
        self._storage_channel_entity = None
        self._bot_exchange: dict[str, dict] = {}
        self._session_path = str(Path(__file__).parent.parent / "relay_session")
        self._ready = asyncio.Event()
        self._relay_api_id: int = 0
        self._relay_api_hash: str = ""
        self._relay_phone: str = ""
        self._pending_cleanup = None
        self._relay_user_id = None

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
            now_ts = asyncio.get_event_loop().time()
            expired = [k for k, v in list(self._bot_exchange.items()) if v.get("_expires", 0) < now_ts]
            for k in expired:
                self._bot_exchange.pop(k, None)

            sender = await event.get_sender()
            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return

            bot_username = sender.username
            if not bot_username:
                return

            exchange = self._bot_exchange.get(bot_username)
            if not exchange:
                return

            user_id = exchange["user_id"]
            code = exchange.get("code", "")
            exchange["_expires"] = now_ts + 60

            logger.info(
                f"[UserRelay] 收到 @{bot_username} 响应 (user={user_id}, code={code})"
            )

            await self._process_single(event.message, user_id, code)

            if self._pending_cleanup:
                self._pending_cleanup(bot_username)

    async def _process_single(self, msg, user_id: int, code: str):
        """Download file, send to decoder_bot via Telethon, cache to storage.
        decoder_bot receives RELAY_FILE and forwards to user via Bot API."""
        is_video = getattr(msg, "video", None) is not None

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = await self._client.download_media(msg, file=tmpdir)
            if not file_path:
                logger.warning(f"[UserRelay] 下载媒体失败 (user={user_id}, code={code})")
                return

            orig_caption = getattr(msg, "message", None) or ""
            relay_caption = f"RELAY_FILE:{user_id}:{code}\n\n{orig_caption}"
            send_kwargs = {"caption": relay_caption}
            if is_video:
                send_kwargs["video"] = True

            if self._decoder_bot_entity:
                try:
                    await self._client.send_file(
                        self._decoder_bot_entity, file_path, **send_kwargs
                    )
                    logger.info(
                        f"[UserRelay] 已通过 RELAY_FILE 发送给解码机器人 (user={user_id}, code={code})"
                    )
                except Exception as e:
                    logger.error(f"[UserRelay] 发送 RELAY_FILE 到解码机器人失败: {e}")

            if code and self._storage_channel_entity:
                try:
                    storage_msg = await self._client.send_file(
                        self._storage_channel_entity, file_path
                    )
                    fid = self._extract_file_id(msg)
                    await self._cache_file_record(code, storage_msg.id, file_id=fid)
                    logger.info(
                        f"[UserRelay] 已缓存到存储频道 (code={code}, msg_id={storage_msg.id})"
                    )
                except Exception as e:
                    logger.error(f"[UserRelay] 缓存到存储频道失败: {e}")

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        if not self._client:
            return False

        try:
            entity = await self._client.get_entity(bot_username)
            await self._client.send_message(entity, code)
            self._bot_exchange[bot_username] = {
                "user_id": user_id, "code": code,
                "_expires": asyncio.get_event_loop().time() + 120,
            }
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

            logger.warning(
                f"[UserRelay] 缓存交付: 码 {code} 无 file_id，记录已过期，清除并通知重新请求"
            )
            await col.delete_one({"file_code": code})
            if self._decoder_bot_entity:
                await self._client.send_message(
                    self._decoder_bot_entity,
                    f"RELAY_RENEW:{user_id}:{code}",
                )
                logger.info(f"[UserRelay] 已通知解码机器人: 码 {code} 需重新请求")
            return False

        except Exception as e:
            logger.error(f"[UserRelay] 缓存交付失败 (code={code}, user={user_id}): {e}")
            return False

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            await self._report_status("offline")
            logger.info("[UserRelay] 已断开连接")


user_relay = UserRelay()