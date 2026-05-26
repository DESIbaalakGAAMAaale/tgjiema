import asyncio
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import PeerChannel
from telethon.utils import pack_bot_file_id

from config import settings
from services.ai_agent import ai_agent


_SETTLE_WAIT = 5
_INITIAL_SETTLE_WAIT = 20
_MAX_AI_LOOP = 10


class UserRelay:
    def __init__(self):
        self._client: TelegramClient | None = None
        self._decoder_bot_entity = None
        self._storage_channel_entity = None
        self._bot_exchange: dict[str, dict] = {}
        self._media_buffers: dict[str, dict] = {}
        self._session_path = str(Path(__file__).parent.parent / "relay_session")
        self._ready = asyncio.Event()
        self._relay_api_id: int = 0
        self._relay_api_hash: str = ""
        self._relay_phone: str = ""
        self._pending_cleanup = None
        self._relay_user_id = None
        self._cache_locks: dict[str, asyncio.Lock] = {}

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

        ai_agent.configure()
        self._register_handlers()
        self._ready.set()
        await self._report_status("online")
        logger.info(f"[UserRelay] 中继已就绪 (AI决策: {'启用' if ai_agent.enabled else '未启用'})")

    async def _cache_file_record(self, code: str, message_id: int, file_id: str = ""):
        lock = self._cache_locks.setdefault(code, asyncio.Lock())
        async with lock:
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

    async def _download_and_cache_one(self, msg, user_id: int, code: str):
        if not getattr(msg, "media", None):
            return
        try:
            storage_msg = await self._client.send_file(
                self._storage_channel_entity, msg.media
            )
            cache_fid = self._extract_file_id(storage_msg)
            await self._cache_file_record(code, storage_msg.id, file_id=cache_fid)
            logger.info(
                f"[UserRelay] 已缓存到存储频道 (code={code}, msg_id={storage_msg.id})"
            )
        except Exception as e:
            logger.error(f"[UserRelay] 缓存到存储频道失败 (code={code}): {e}")

    def _register_handlers(self):
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            now_ts = asyncio.get_event_loop().time()
            expired = [k for k, v in list(self._bot_exchange.items()) if v.get("_expires", 0) < now_ts]
            for k in expired:
                old = self._bot_exchange.pop(k, None)
                if old and old.get("_settle_task") and not old["_settle_task"].done():
                    old["_settle_task"].cancel()

            sender = await event.get_sender()
            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return

            bot_username = sender.username
            if not bot_username:
                return

            exchange = self._bot_exchange.get(bot_username)
            if not exchange:
                return

            exchange["_expires"] = now_ts + 120

            if event.message.reply_markup:
                exchange["_keyboard_msg"] = event.message

            media_group_id = getattr(event.message, "media_group_id", None)
            if media_group_id:
                buf = self._media_buffers.get(media_group_id)
                if buf:
                    buf["events"].append(event)
                    buf["_expires"] = now_ts + 5
                    logger.info(
                        f"[UserRelay] 媒体组 {media_group_id} 收集第 {len(buf['events'])} 条"
                    )
                    return

                self._media_buffers[media_group_id] = {
                    "events": [event],
                    "bot_username": bot_username,
                    "_expires": now_ts + 5,
                }
                logger.info(
                    f"[UserRelay] 媒体组 {media_group_id} 开始收集"
                )
                asyncio.create_task(
                    self._flush_media_group_buffer(media_group_id, bot_username)
                )
                return

            exchange.setdefault("events", []).append(event)
            await self._download_and_cache_one(event.message, exchange.get("user_id"), exchange.get("code"))
            self._restart_settle(exchange, bot_username)

    def _restart_settle(self, exchange: dict, bot_username: str, settle_wait: float = _SETTLE_WAIT):
        exchange["_msg_version"] = exchange.get("_msg_version", 0) + 1
        old = exchange.get("_settle_task")
        if old and not old.done():
            if old.get_name() == "settle_sleeping":
                old.cancel()
            else:
                return
        exchange["_settle_task"] = asyncio.create_task(
            self._ai_message_loop(bot_username, settle_wait)
        )

    async def _flush_media_group_buffer(self, media_group_id: str, bot_username: str):
        await asyncio.sleep(3)

        now_ts = asyncio.get_event_loop().time()
        while True:
            buf = self._media_buffers.get(media_group_id)
            if not buf:
                return
            if buf["_expires"] > now_ts:
                await asyncio.sleep(0.5)
                now_ts = asyncio.get_event_loop().time()
                continue
            break

        buf = self._media_buffers.pop(media_group_id, None)
        if not buf:
            return

        exchange = self._bot_exchange.get(bot_username)
        if not exchange:
            logger.warning(f"[UserRelay] 媒体组 {media_group_id} 无对应 exchange")
            return

        events_list = buf["events"]
        user_id = exchange.get("user_id")
        code = exchange.get("code")
        logger.info(
            f"[UserRelay] 媒体组 {media_group_id} 共 {len(events_list)} 条，开始缓存到存储频道"
        )

        for ev in events_list:
            await self._download_and_cache_one(ev.message, user_id, code)

        exchange.setdefault("events", []).extend(events_list)
        self._restart_settle(exchange, bot_username)

    async def _ai_message_loop(self, bot_username: str, settle_wait: float = _SETTLE_WAIT):
        task = asyncio.current_task()
        if task:
            task.set_name("settle_sleeping")
        await asyncio.sleep(settle_wait)
        if task:
            task.set_name("")

        exchange = self._bot_exchange.get(bot_username)
        if not exchange:
            return

        if exchange.get("_ai_running"):
            return

        exchange["_ai_running"] = True

        loop_count = 0
        try:
            while loop_count < _MAX_AI_LOOP:
                loop_count += 1

                if bot_username not in self._bot_exchange:
                    logger.warning(f"[UserRelay] AI循环: exchange 已被清理 (bot={bot_username})")
                    break

                exchange = self._bot_exchange[bot_username]
                exchange["_expires"] = asyncio.get_event_loop().time() + 120
                version_before = exchange.get("_msg_version", 0)

                decision = await ai_agent.decide({
                    "bot_username": bot_username,
                    "events": exchange.get("events", []),
                })

                if bot_username not in self._bot_exchange:
                    break
                exchange = self._bot_exchange[bot_username]
                version_after = exchange.get("_msg_version", 0)

                if version_after != version_before:
                    logger.info(
                        f"[UserRelay] 新消息到达 (v{version_before}→v{version_after})，"
                        f"重新评估"
                    )
                    continue

                action = decision.get("action", "finish")
                reason = decision.get("reason", "")
                logger.info(
                    f"[UserRelay] AI决策 #{loop_count}: action={action}, reason={reason}"
                )

                if action == "finish":
                    await self._process_all_collected(bot_username)
                    break
                elif action == "error":
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        user_id = exchange.get("user_id", 0)
                        code = exchange.get("code", "")
                        try:
                            await self._client.send_message(
                                self._decoder_bot_entity,
                                f"RELAY_ERROR:{user_id}:{code}:{reason}",
                            )
                        except Exception as e:
                            logger.error(f"[UserRelay] 发送错误通知失败: {e}")
                    await self._cleanup_exchange(bot_username)
                    break
                elif action == "wait":
                    if not exchange.get("events"):
                        logger.info(
                            "[UserRelay] AI要求等待但无任何消息到达，直接结束 "
                            f"(bot={bot_username})"
                        )
                        await self._process_all_collected(bot_username)
                        break
                    wait_s = decision.get("wait_seconds", 5)
                    if not isinstance(wait_s, (int, float)) or wait_s <= 0:
                        wait_s = 5
                    logger.info(f"[UserRelay] AI要求等待 {wait_s}s")
                    await asyncio.sleep(wait_s)
                    continue
                elif action == "click_button":
                    row = decision.get("target_button_row")
                    col = decision.get("target_button_col")
                    if row is None or col is None:
                        logger.warning("[UserRelay] AI要求点击按钮但未指定 row/col")
                        continue

                    clicked = await self._click_button(bot_username, row, col)
                    if not clicked:
                        await self._process_all_collected(bot_username)
                        break

                    await asyncio.sleep(4)
                    continue
                else:
                    logger.warning(f"[UserRelay] AI返回未知 action: {action}")
                    await self._process_all_collected(bot_username)
                    break

        except asyncio.CancelledError:
            logger.debug(f"[UserRelay] AI循环被取消 (bot={bot_username})")
        except Exception as e:
            logger.error(f"[UserRelay] AI循环异常 (bot={bot_username}): {e}")
            await self._process_all_collected(bot_username)
        finally:
            if bot_username in self._bot_exchange:
                self._bot_exchange[bot_username]["_ai_running"] = False

    async def _click_button(self, bot_username: str, row: int, col: int) -> bool:
        exchange = self._bot_exchange.get(bot_username)
        if not exchange:
            return False

        keyboard_msg = exchange.get("_keyboard_msg")
        if not keyboard_msg or not keyboard_msg.reply_markup:
            logger.warning(f"[UserRelay] 无可用键盘消息 (bot={bot_username})")
            return False

        reply_markup = keyboard_msg.reply_markup
        if not hasattr(reply_markup, "rows"):
            return False

        try:
            target_row = reply_markup.rows[row]
            target_btn = target_row.buttons[col]
        except (IndexError, AttributeError):
            logger.warning(f"[UserRelay] 按钮位置无效: row={row}, col={col}")
            return False

        exchange.pop("_keyboard_msg", None)
        try:
            await keyboard_msg.click(data=target_btn.data)
            btn_text = getattr(target_btn, "text", "") or "(无文字/图标按钮)"
            logger.info(f"[UserRelay] 已点击按钮 [{row},{col}] {btn_text}")
            return True
        except Exception as e:
            logger.error(f"[UserRelay] 点击按钮失败 [{row},{col}]: {e}")
            return False

    async def _process_all_collected(self, bot_username: str):
        exchange = self._bot_exchange.pop(bot_username, None)
        if not exchange:
            return

        user_id = exchange.get("user_id")
        code = exchange.get("code")
        all_events = exchange.get("events", [])

        logger.info(
            f"[UserRelay] 处理全部已收集文件: user={user_id}, code={code}, "
            f"events={len(all_events)}"
        )

        if not all_events:
            logger.warning(
                f"[UserRelay] 目标机器人 @{bot_username} 未返回任何文件 "
                f"(user={user_id}, code={code})"
            )
            if self._decoder_bot_entity:
                try:
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_ERROR:{user_id}:{code}:目标机器人未返回任何文件",
                    )
                except Exception as e:
                    logger.error(f"[UserRelay] 通知解码机器人失败: {e}")
            if self._pending_cleanup:
                self._pending_cleanup(bot_username)
            return

        from database import get_file_records_col
        try:
            files_col = get_file_records_col()
            record = None
            for retry in range(6):
                if retry > 0:
                    await asyncio.sleep(2)
                record = await files_col.find_one({"file_code": code})
                if record:
                    break
                if retry < 5:
                    logger.info(
                        f"[UserRelay] DB 暂无记录 (code={code})，等待后台缓存 "
                        f"(第{retry+1}次重试)"
                    )
            if not record:
                logger.warning(f"[UserRelay] DB 始终无记录 (code={code})")
                if self._pending_cleanup:
                    self._pending_cleanup(bot_username)
                return

            batch_ids_str = record.get("batch_msg_ids") or ""
            if not isinstance(batch_ids_str, str):
                batch_ids_str = str(batch_ids_str)
            msg_ids = []
            primary_mid = record.get("primary_channel_msg_id")
            if primary_mid:
                msg_ids.append(str(primary_mid))
            for mid in batch_ids_str.split(","):
                m = mid.strip()
                if m and m not in msg_ids:
                    msg_ids.append(m)

            if not msg_ids:
                logger.warning(f"[UserRelay] 无存储 msg_id (code={code})")
                if self._pending_cleanup:
                    self._pending_cleanup(bot_username)
                return

            storage_ids_str = ",".join(msg_ids)

            if self._decoder_bot_entity:
                await self._client.send_message(
                    self._decoder_bot_entity,
                    f"RELAY_BATCH:{user_id}:{code}\nSTORAGE_IDS:{storage_ids_str}",
                )
                logger.info(
                    f"[UserRelay] 已通知解码机器人: user={user_id}, code={code}, "
                    f"{len(msg_ids)} 个缓存文件"
                )

        except Exception as e:
            logger.error(f"[UserRelay] 处理收集文件失败 (code={code}): {e}")

        if self._pending_cleanup:
            self._pending_cleanup(bot_username)

    async def _cleanup_exchange(self, bot_username: str):
        exchange = self._bot_exchange.pop(bot_username, None)
        if exchange and exchange.get("_settle_task") and not exchange["_settle_task"].done():
            exchange["_settle_task"].cancel()
        if self._pending_cleanup:
            self._pending_cleanup(bot_username)

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        if not self._client:
            return False

        try:
            entity = await self._client.get_entity(bot_username)
            await self._client.send_message(entity, code)
            now = asyncio.get_event_loop().time()
            self._bot_exchange[bot_username] = {
                "user_id": user_id,
                "code": code,
                "events": [],
                "_expires": now + 120,
                "_settle_task": None,
                "_ai_running": False,
                "_keyboard_msg": None,
            }
            self._restart_settle(
                self._bot_exchange[bot_username], bot_username,
                settle_wait=_INITIAL_SETTLE_WAIT,
            )
            logger.info(f"[UserRelay] 已向 @{bot_username} 发送外部码，AI驱动等待响应 (user={user_id}, code={code})")
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