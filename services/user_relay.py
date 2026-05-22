import asyncio
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from config import settings


class UserRelay:
    def __init__(self):
        self._client: TelegramClient | None = None
        self._decoder_bot_entity = None
        self._storage_channel_entity = None
        self._pending: dict[str, int] = {}
        self._session_path = str(Path(__file__).parent.parent / "relay_session")
        self._ready = asyncio.Event()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def start(self):
        if not settings.RELAY_API_ID or not settings.RELAY_API_HASH or not settings.RELAY_PHONE:
            logger.warning("[UserRelay] 未配置 RELAY_API_ID/HASH/PHONE，跳过中继")
            return

        self._client = TelegramClient(
            self._session_path,
            settings.RELAY_API_ID,
            settings.RELAY_API_HASH,
        )

        await self._client.start(phone=settings.RELAY_PHONE)

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
        logger.info(f"[UserRelay] 中继已就绪")

    def _register_handlers(self):
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            sender = await event.get_sender()
            if not sender or not sender.bot:
                return

            bot_username = sender.username
            if not bot_username or bot_username not in self._pending:
                return

            user_id = self._pending[bot_username]
            logger.info(
                f"[UserRelay] 收到 @{bot_username} 的文件响应，转发给用户 {user_id}"
            )

            if self._decoder_bot_entity:
                try:
                    await self._client.forward_messages(
                        self._decoder_bot_entity, event.message
                    )
                    logger.info(f"[UserRelay] 已转发到解码机器人")
                except Exception as e:
                    logger.error(f"[UserRelay] 转发失败: {e}")
                    return

            try:
                await event.message.forward_to(user_id)
                logger.info(f"[UserRelay] 已转发给原始用户 {user_id}")
            except Exception as e:
                logger.error(f"[UserRelay] 转发给用户 {user_id} 失败: {e}")

            del self._pending[bot_username]

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        if not self._client:
            return False

        try:
            entity = await self._client.get_entity(bot_username)
            await self._client.send_message(entity, code)
            self._pending[bot_username] = user_id
            logger.info(f"[UserRelay] 已向 @{bot_username} 发送外部码，等待响应 (user={user_id})")
            return True
        except Exception as e:
            logger.error(f"[UserRelay] 向 @{bot_username} 发送失败: {e}")
            return False

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            logger.info("[UserRelay] 已断开连接")


user_relay = UserRelay()