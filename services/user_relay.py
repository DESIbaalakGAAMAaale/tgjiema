import asyncio
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
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

    async def _wait_for_admin_code(self) -> str | None:
        from database.session import get_config, set_config

        await set_config("relay_auth_pending", "1")
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

    async def start(self):
        if not settings.RELAY_API_ID or not settings.RELAY_API_HASH or not settings.RELAY_PHONE:
            logger.warning("[UserRelay] 未配置 RELAY_API_ID/HASH/PHONE，跳过中继")
            return

        self._client = TelegramClient(
            self._session_path,
            settings.RELAY_API_ID,
            settings.RELAY_API_HASH,
        )

        await self._client.connect()

        if not await self._client.is_user_authorized():
            await self._client.send_code_request(settings.RELAY_PHONE)
            logger.info("[UserRelay] 验证码已发送到 Telegram 账号")

            code = settings.RELAY_CODE.strip() if settings.RELAY_CODE else None
            if not code:
                logger.info("[UserRelay] 环境变量 RELAY_CODE 未设置，等待管理员通过管理机器人提交...")
                code = await self._wait_for_admin_code()

            if not code:
                logger.error("[UserRelay] 无法获取验证码，登录失败")
                await self._client.disconnect()
                return

            try:
                await self._client.sign_in(settings.RELAY_PHONE, code)
            except SessionPasswordNeededError:
                logger.error(
                    "[UserRelay] 该账号开启了二步验证，暂不支持。请关闭二步验证或使用无二步验证的账号。"
                )
                await self._client.disconnect()
                return
            except Exception as e:
                logger.error(f"[UserRelay] 登录失败: {e}")
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