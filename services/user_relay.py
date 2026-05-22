import asyncio
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import PeerChannel, PeerUser

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

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

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
        logger.info(f"[UserRelay] 已登录: {me.first_name} (@{me.username})")

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

    async def _relay_via_download(self, message, target_entity, target_name: str) -> bool:
        try:
            data = await self._client.download_media(message, file=bytes)
            if not data:
                logger.warning(f"[UserRelay] 下载媒体失败（无媒体可下载），目标={target_name}")
                return False
            caption = getattr(message, "message", None) or ""
            await self._client.send_file(target_entity, data, caption=caption)
            logger.info(f"[UserRelay] 已通过下载重传方式发送到 {target_name}")
            return True
        except Exception as e:
            logger.error(f"[UserRelay] 下载重传到 {target_name} 失败: {e}")
            return False

    async def _cache_file_record(self, code: str, message_id: int):
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
                await files_col.update_one(
                    {"file_code": code},
                    {"$set": {"batch_msg_ids": ",".join(batch_ids)}},
                )
                logger.info(f"[UserRelay] 外部码 {code} 追加 msg_id={message_id}，batch={batch_ids}")
            else:
                record = make_file_record(
                    file_code=code,
                    uploader_id=0,
                    primary_channel_id=_s.MAIN_STORAGE_CHANNEL_ID,
                    primary_channel_msg_id=message_id,
                    file_types={},
                )
                await files_col.insert_one(record)
                logger.info(f"[UserRelay] 外部码 {code} 已缓存到本地存储")
        except Exception as e:
            logger.error(f"[UserRelay] 缓存外部码失败 (code={code}, msg_id={message_id}): {e}")

    def _is_forward_restricted_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        return "protected" in msg or "forward" in msg

    def _register_handlers(self):
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            sender = await event.get_sender()
            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return

            bot_username = sender.username
            if not bot_username or bot_username not in self._pending:
                return

            pending = self._pending[bot_username]
            user_id = pending["user_id"]
            code = pending.get("code", "")

            logger.info(
                f"[UserRelay] 收到 @{bot_username} 的文件响应，转发给用户 {user_id}"
            )

            forwarded_to_decoder = False
            if self._decoder_bot_entity:
                try:
                    await self._client.forward_messages(
                        self._decoder_bot_entity, event.message
                    )
                    logger.info(f"[UserRelay] 已转发到解码机器人")
                    forwarded_to_decoder = True
                except Exception as e:
                    if self._is_forward_restricted_error(e):
                        logger.warning(f"[UserRelay] 转发受限，改用下载重传到解码机器人")
                        forwarded_to_decoder = await self._relay_via_download(
                            event.message, self._decoder_bot_entity, "解码机器人"
                        )
                    else:
                        logger.error(f"[UserRelay] 转发到解码机器人失败: {e}")

            forwarded_to_user = False
            user_entity = None
            try:
                user_entity = await self._client.get_entity(PeerUser(user_id))
                await self._client.forward_messages(user_entity, event.message)
                logger.info(f"[UserRelay] 已转发给原始用户 {user_id}")
                forwarded_to_user = True
            except Exception as e:
                if self._is_forward_restricted_error(e):
                    logger.warning(f"[UserRelay] 转发受限，改用下载重传到用户 {user_id}")
                    if user_entity is None:
                        try:
                            user_entity = await self._client.get_entity(PeerUser(user_id))
                        except Exception as e2:
                            logger.error(f"[UserRelay] 无法获取用户实体 {user_id}: {e2}")
                    if user_entity is not None:
                        forwarded_to_user = await self._relay_via_download(
                            event.message, user_entity, f"用户 {user_id}"
                        )
                else:
                    logger.error(f"[UserRelay] 转发给用户 {user_id} 失败: {e}")

            if code and (forwarded_to_user or forwarded_to_decoder):
                try:
                    data = await self._client.download_media(event.message, file=bytes)
                    if data and self._storage_channel_entity:
                        storage_msg = await self._client.send_file(
                            self._storage_channel_entity, data
                        )
                        await self._cache_file_record(code, storage_msg.id)
                        logger.info(f"[UserRelay] 外部码 {code} 的文件已缓存到存储频道")
                except Exception as e:
                    logger.error(f"[UserRelay] 缓存到存储频道失败: {e}")

            self._pending.pop(bot_username, None)

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

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            await self._report_status("offline")
            logger.info("[UserRelay] 已断开连接")


user_relay = UserRelay()