import asyncio
import json
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import PeerChannel
from telethon.utils import pack_bot_file_id

from config import settings


_SETTLE_WAIT = 5
_INITIAL_SETTLE_WAIT = 20


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
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._pending_cache_counts: dict[str, int] = {}
        self._pending_cache_events: dict[str, asyncio.Event] = {}

    @property
    def is_ready(self) -> bool:
        from services.relay_pool import relay_pool
        if relay_pool.instances:
            return any(i.is_ready for i in relay_pool.instances)
        return self._ready.is_set()

    @property
    def relay_user_id(self) -> int | None:
        from services.relay_pool import relay_pool
        for i in relay_pool.instances:
            if i.is_ready:
                return i.relay_user_id
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
        api_id = 0
        api_hash = ""
        phone = ""

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
            logger.info("[UserRelay] 等待管理员通过 /relay_code 提交验证码...")
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
            self._storage_channel_id = settings.MAIN_STORAGE_CHANNEL_ID
        except Exception:
            self._storage_channel_entity = None
            self._storage_channel_id = 0

        self._register_handlers()
        self._ready.set()
        await self._report_status("online")
        logger.info("[UserRelay] 中继已就绪")

    async def _cache_file_record(self, code: str, message_id: int, file_id: str = "", media_type: str = "document"):
        lock = self._cache_locks.setdefault(code, asyncio.Lock())
        async with lock:
            try:
                from database import get_file_records_col, make_file_record

                files_col = get_file_records_col()
                existing = await files_col.find_one({"file_code": code})
                if existing:
                    batch = existing.get("batch_msg_ids", "") or ""
                    if not isinstance(batch, str):
                        batch = str(batch)
                    batch_ids = [mid for mid in batch.split(",") if mid.strip()]
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

                    meta_raw = existing.get("batch_file_meta") or ""
                    try:
                        meta_list = (
                            json.loads(meta_raw)
                            if isinstance(meta_raw, str) and meta_raw
                            else (meta_raw if isinstance(meta_raw, list) else [])
                        )
                    except (json.JSONDecodeError, TypeError):
                        meta_list = []
                    if not isinstance(meta_list, list):
                        meta_list = []

                    found = False
                    mid_str = str(message_id)
                    for entry in meta_list:
                        if isinstance(entry, dict) and str(entry.get("msg_id", "")) == mid_str:
                            entry["file_id"] = file_id or entry.get("file_id", "")
                            entry["type"] = media_type
                            found = True
                            break
                    if not found:
                        meta_list.append({
                            "msg_id": mid_str,
                            "file_id": file_id,
                            "type": media_type,
                        })
                    update["$set"]["batch_file_meta"] = json.dumps(meta_list)

                    healed = await self._self_heal_file_ids(code, meta_list)
                    if healed > 0:
                        update["$set"]["batch_file_meta"] = json.dumps(meta_list)
                        fids_list = [e.get("file_id", "") for e in meta_list
                                     if isinstance(e, dict) and e.get("file_id")]
                        update["$set"]["file_ids"] = ",".join(fids_list)

                    await files_col.update_one({"file_code": code}, update)
                    logger.info(f"[UserRelay] 外部码 {code} 追加 msg_id={message_id}，batch={batch_ids}")
                else:
                    record = make_file_record(
                        file_code=code,
                        uploader_id=0,
                        primary_channel_id=self._storage_channel_id,
                        primary_channel_msg_id=message_id,
                        file_types={},
                    )
                    if file_id:
                        record["file_ids"] = file_id
                        record["batch_file_meta"] = json.dumps([{
                            "msg_id": str(message_id),
                            "file_id": file_id,
                            "type": media_type,
                        }])
                    await files_col.insert_one(record)
                    logger.info(f"[UserRelay] 外部码 {code} 已缓存到本地存储")
            except Exception as e:
                logger.error(f"[UserRelay] 缓存外部码失败 (code={code}, msg_id={message_id}): {e}")

    def _extract_file_id(self, msg) -> str:
        if not msg or not msg.media:
            return ""
        try:
            media = msg.media
            from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

            if isinstance(media, MessageMediaPhoto):
                inner = media.photo
                return pack_bot_file_id(inner) or ""
            elif isinstance(media, MessageMediaDocument):
                inner = media.document
                return pack_bot_file_id(inner) or ""
            return pack_bot_file_id(media) or ""
        except Exception:
            logger.debug(f"[UserRelay] _extract_file_id 失败 (msg_id={getattr(msg, 'id', '?')})")
            return ""

    async def _self_heal_file_ids(self, code: str, meta_list: list) -> int:
        fixed_count = 0
        for entry in meta_list:
            if not isinstance(entry, dict):
                continue
            if entry.get("file_id"):
                continue
            msg_id = entry.get("msg_id")
            if not msg_id:
                continue
            if not self._client or not self._storage_channel_entity:
                break
            try:
                old_msg = await self._client.get_messages(
                    self._storage_channel_entity, ids=int(msg_id)
                )
                if old_msg:
                    fid = self._extract_file_id(old_msg)
                    if fid:
                        entry["file_id"] = fid
                        fixed_count += 1
            except Exception:
                pass
        if fixed_count > 0:
            logger.info(f"[UserRelay] 自修复: 码 {code} 共修复 {fixed_count} 个空 file_id")
        return fixed_count

    async def _decrement_cache_counter(self, code: str):
        current = self._pending_cache_counts.get(code, 0)
        self._pending_cache_counts[code] = max(0, current - 1)
        if self._pending_cache_counts[code] == 0:
            ev = self._pending_cache_events.get(code)
            if ev:
                ev.set()

    async def _download_and_cache_one(self, msg, user_id: int, code: str):
        if not getattr(msg, "media", None):
            await self._decrement_cache_counter(code)
            return
        try:
            storage_msg = await self._client.send_file(
                self._storage_channel_entity, msg.media
            )
            cache_fid = self._extract_file_id(storage_msg)
            media_type = self._detect_media_type(msg)
            await self._cache_file_record(
                code, storage_msg.id, file_id=cache_fid, media_type=media_type,
            )
            logger.info(
                f"[UserRelay] 已缓存到存储频道 (code={code}, msg_id={storage_msg.id})"
            )
        except Exception as e:
            logger.error(f"[UserRelay] 缓存到存储频道失败 (code={code}): {e}")
        finally:
            await self._decrement_cache_counter(code)

    @staticmethod
    def _detect_media_type(msg) -> str:
        if hasattr(msg, "photo") and msg.photo:
            return "photo"
        if hasattr(msg, "video") and msg.video:
            return "video"
        if hasattr(msg, "audio") and msg.audio:
            return "audio"
        if hasattr(msg, "voice") and msg.voice:
            return "audio"
        if hasattr(msg, "animation") and msg.animation:
            return "animation"
        if hasattr(msg, "gif") and msg.gif:
            return "animation"
        if hasattr(msg, "sticker") and msg.sticker:
            return "sticker"
        if hasattr(msg, "document") and msg.document:
            mime = getattr(msg.document, "mime_type", "") or ""
            if "video" in mime:
                return "video"
            if "audio" in mime:
                return "audio"
            return "document"
        return "document"

    @staticmethod
    def _extract_number(text: str) -> int | None:
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            return int(digits)
        return None

    @staticmethod
    def _extract_wait_seconds(msg) -> int:
        import re
        text = (getattr(msg, "message", None) or "").lower()
        patterns = [
            r"(\d+)\s*秒",
            r"(\d+)\s*seconds?",
            r"(\d+)\s*secs?",
            r"wait\s*(\d+)",
            r"等\s*(\d+)",
            r"稍等\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                sec = int(m.group(1))
                return max(sec, 1)
        return 5

    def _register_handlers(self):
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            now_ts = asyncio.get_event_loop().time()
            expired = [k for k, v in list(self._bot_exchange.items()) if v.get("_expires", 0) < now_ts]
            for k in expired:
                old = self._bot_exchange.pop(k, None)
                if old:
                    self._cleanup_code_dicts(old.get("code", ""))
                    if old.get("_settle_task") and not old["_settle_task"].done():
                        old["_settle_task"].cancel()
            # 清理过期的 media_buffers
            stale_buffers = [
                mgid for mgid, buf in list(self._media_buffers.items())
                if buf.get("_expires", 0) < now_ts
            ]
            for mgid in stale_buffers:
                self._media_buffers.pop(mgid, None)

            sender = await event.get_sender()

            if self._bot_exchange:
                sender_info = ""
                try:
                    sid = getattr(sender, "id", "?")
                    sun = getattr(sender, "username", None) or ""
                    sbot = getattr(sender, "bot", False)
                    sender_info = f"id={sid}, username=@{sun}, is_bot={sbot}"
                except Exception:
                    sender_info = "unknown"
                logger.info(
                    f"[UserRelay] 收到消息: sender({sender_info}), "
                    f"has_media={bool(getattr(event.message, 'media', None))}, "
                    f"active_exchanges={list(self._bot_exchange.keys())}"
                )

            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return

            bot_username = (sender.username or "").lower()
            if not bot_username:
                return

            exchange = self._bot_exchange.get(bot_username)
            if not exchange:
                logger.info(
                    f"[UserRelay] 来自 @{bot_username} 的消息无对应 exchange，忽略"
                )
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
            code = exchange.get("code")
            if code:
                self._pending_cache_counts[code] = self._pending_cache_counts.get(code, 0) + 1
                await self._download_and_cache_one(event.message, exchange.get("user_id"), code)
            self._restart_settle(exchange, bot_username)

        @self._client.on(events.MessageEdited(incoming=True))
        async def on_message_edited(event):
            sender = await event.get_sender()
            if not sender or not hasattr(sender, "bot") or not sender.bot:
                return
            bot_username = (sender.username or "").lower()
            if not bot_username:
                return
            exchange = self._bot_exchange.get(bot_username)
            if not exchange:
                return
            if not (event.message.reply_markup and hasattr(event.message.reply_markup, "rows") and event.message.reply_markup.rows):
                return
            exchange["_keyboard_msg"] = event.message
            exchange["_msg_version"] = exchange.get("_msg_version", 0) + 1
            logger.info(f"[UserRelay] 捕获键盘编辑 (bot=@{bot_username})")

    def _restart_settle(self, exchange: dict, bot_username: str, settle_wait: float = _SETTLE_WAIT):
        exchange["_msg_version"] = exchange.get("_msg_version", 0) + 1
        old = exchange.get("_settle_task")
        if old and not old.done():
            if old.get_name() == "settle_sleeping":
                old.cancel()
            else:
                return
        exchange["_settle_task"] = asyncio.create_task(
            self._message_loop(bot_username, settle_wait)
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
            self._pending_cache_counts[code] = self._pending_cache_counts.get(code, 0) + 1

        for ev in events_list:
            await self._download_and_cache_one(ev.message, user_id, code)

        exchange.setdefault("events", []).extend(events_list)
        self._restart_settle(exchange, bot_username)

    async def _wait_all_cached(self, bot_username: str, timeout: float = 60):
        exchange = self._bot_exchange.get(bot_username)
        if not exchange:
            return
        code = exchange.get("code", "")
        pending = self._pending_cache_counts.get(code, 0)
        if pending <= 0:
            return
        # 使用 lock 保护 clear/wait 原子性
        ev_lock = self._event_locks.setdefault(code, asyncio.Lock())
        async with ev_lock:
            ev = self._pending_cache_events.get(code)
            if ev is None:
                ev = self._pending_cache_events[code] = asyncio.Event()
            ev.clear()
            # 再次检查计数，避免在 clear 后 decrement 还没到来
            if self._pending_cache_counts.get(code, 0) <= 0:
                return
            logger.info(f"[UserRelay] 等待 {pending} 个缓存操作完成 (code={code})")
            try:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
                logger.info(f"[UserRelay] 缓存操作全部完成 (code={code})")
            except asyncio.TimeoutError:
                logger.warning(f"[UserRelay] 等待缓存操作超时 (code={code}), 剩余 {self._pending_cache_counts.get(code, '?')}")

    async def _message_loop(self, bot_username: str, settle_wait: float = _SETTLE_WAIT):
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

        stale_clicks = 0
        last_msg_ids = set()  # 追踪消息 ID 去重，防止翻页无新内容时死循环
        try:
            while True:

                if bot_username not in self._bot_exchange:
                    logger.warning(f"[UserRelay] 翻页循环: exchange 已被清理 (bot={bot_username})")
                    break

                exchange = self._bot_exchange[bot_username]
                exchange["_expires"] = asyncio.get_event_loop().time() + 120
                version_before = exchange.get("_msg_version", 0)

                decision = self._make_decision(exchange)

                if bot_username not in self._bot_exchange:
                    break
                exchange = self._bot_exchange[bot_username]
                version_after = exchange.get("_msg_version", 0)

                if version_after != version_before:
                    logger.info(
                        f"[UserRelay] 新消息到达 (v{version_before}→v{version_after})，重新评估"
                    )
                    continue

                action = decision.get("action", "finish")
                reason = decision.get("reason", "")
                logger.info(
                    f"[UserRelay] 翻页决策: action={action}, reason={reason}"
                )

                if action == "finish":
                    await self._process_all_collected(bot_username)
                    break
                elif action == "error":
                    exchange_data = self._bot_exchange.get(bot_username)
                    if exchange_data:
                        user_id = exchange_data.get("user_id", 0)
                        code = exchange_data.get("code", "")
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
                    wait_sec = decision.get("wait_seconds", 5)
                    logger.info(
                        f"[UserRelay] 遵守翻页速度限制，等待 {wait_sec} 秒 "
                        f"(bot={bot_username})"
                    )
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        exchange["_last_click_time"] = (
                            asyncio.get_event_loop().time() + wait_sec
                        )
                    await asyncio.sleep(wait_sec)
                    continue
                elif action == "click_button":
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        min_interval = exchange.get("_min_click_interval", 0)
                        last_click = exchange.get("_last_click_time", 0)
                        now = asyncio.get_event_loop().time()
                        remaining = (last_click + min_interval) - now
                        if remaining > 0:
                            logger.info(
                                f"[UserRelay] 翻页速度限制，等待剩余 {remaining:.1f} 秒 "
                                f"(bot={bot_username}, min_interval={min_interval}s)"
                            )
                            await asyncio.sleep(remaining)
                    exchange = self._bot_exchange.get(bot_username)
                    row = decision.get("target_button_row")
                    col = decision.get("target_button_col")
                    btn_text = decision.get("target_button_text", "")
                    if row is None or col is None:
                        logger.warning("[UserRelay] 决策要求点击按钮但未指定 row/col")
                        await self._process_all_collected(bot_username)
                        break

                    events_before = len(exchange.get("events", []))

                    logger.info(
                        f'[UserRelay] 点击翻页按钮 [{row},{col}] "{btn_text}" (events={events_before})'
                    )
                    clicked = await self._click_button(bot_username, row, col)
                    if not clicked:
                        logger.info("[UserRelay] 翻页按钮点击失败（可能已是最后一页）")
                        await self._process_all_collected(bot_username)
                        break

                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        exchange.setdefault("_clicked_buttons", set()).add((row, col))
                        exchange["_last_click_time"] = asyncio.get_event_loop().time()
                    await asyncio.sleep(4)
                    await self._wait_all_cached(bot_username)

                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        events_after = len(exchange.get("events", []))
                        # 用消息 ID 去重判断是否有新内容，而非仅靠数量
                        current_ids = set()
                        for ev in exchange.get("events", []):
                            mid = getattr(ev.message, "id", None)
                            if mid:
                                current_ids.add(mid)
                        new_ids = current_ids - last_msg_ids
                        if new_ids:
                            stale_clicks = 0
                            last_msg_ids = current_ids
                        else:
                            stale_clicks += 1
                            logger.info(
                                f"[UserRelay] 翻页无新消息 ID (stale_clicks={stale_clicks}/3), "
                                f"events_before={events_before}, events_after={events_after}"
                            )
                            if stale_clicks >= 3:
                                logger.info("[UserRelay] 连续翻页无新消息，结束收集")
                                await self._process_all_collected(bot_username)
                                break
                    continue
                else:
                    logger.warning(f"[UserRelay] 未知 action: {action}")
                    await self._process_all_collected(bot_username)
                    break

        except asyncio.CancelledError:
            logger.debug(f"[UserRelay] 翻页循环被取消 (bot={bot_username})")
        except Exception as e:
            logger.error(f"[UserRelay] 翻页循环异常 (bot={bot_username}): {e}")
            await self._process_all_collected(bot_username)
        finally:
            if bot_username in self._bot_exchange:
                self._bot_exchange[bot_username]["_ai_running"] = False

    def _make_decision(self, exchange: dict) -> dict:
        _NEXT_KW = (
            "next", "\u4e0b\u4e00\u9875", "\u4e0b\u4e00\u9801",
            "\u4e0b\u4e00\u7ec4",
            "\u2192", "\u25b6", "\u27a1", ">>", "\u00bb",
        )
        _ERROR_KW = (
            "\u672a\u627e\u5230", "\u5df2\u8fc7\u671f", "\u5df2\u5931\u6548",
            "\u4e0d\u5b58\u5728", "not found", "expired", "invalid",
        )
        _RATE_LIMIT_KW = (
            "\u8bf7\u7a0d\u540e", "\u8bf7\u7b49\u5f85", "\u7a0d\u540e\u518d\u8bd5",
            "\u901f\u5ea6\u592a\u5feb", "\u7ffb\u9875\u592a\u5feb", "\u64cd\u4f5c\u592a\u5feb",
            "\u9891\u7e41", "\u8bf7\u6162\u4e00\u70b9", "\u6162\u4e00\u70b9",
            "too fast", "wait", "slow down", "rate limit",
            "\u8bf7\u52ff\u8fc7\u5feb",
        )
        _FINISH_KW = {"finish", "done", "\u5b8c\u6210", "\u7ed3\u675f"}

        msg_events = exchange.get("events", [])
        if not msg_events:
            return {
                "action": "finish", "target_button_row": None,
                "target_button_col": None, "target_button_text": None,
                "reason": "\u65e0\u6d88\u606f", "wait_seconds": None,
            }

        for ev in msg_events:
            msg = ev.message
            text = (getattr(msg, "message", None) or "").lower()
            for ek in _ERROR_KW:
                if ek in text:
                    return {
                        "action": "error", "target_button_row": None,
                        "target_button_col": None, "target_button_text": None,
                        "reason": f"\u68c0\u6d4b\u5230\u9519\u8bef\u5173\u952e\u8bcd: {ek}",
                        "wait_seconds": None,
                    }

        for ev in msg_events:
            msg = ev.message
            text = (getattr(msg, "message", None) or "").lower()
            for rk in _RATE_LIMIT_KW:
                if rk in text:
                    wait_sec = self._extract_wait_seconds(msg)
                    exchange["_min_click_interval"] = max(
                        exchange.get("_min_click_interval", 0), wait_sec
                    )
                    return {
                        "action": "wait",
                        "target_button_row": None,
                        "target_button_col": None,
                        "target_button_text": None,
                        "reason": f"\u68c0\u6d4b\u5230\u7ffb\u9875\u901f\u5ea6\u9650\u5236: {rk}, \u7b49\u5f85{wait_sec}\u79d2",
                        "wait_seconds": wait_sec,
                    }

        for ev in msg_events:
            msg = ev.message
            if not (msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows):
                continue

            rows = msg.reply_markup.rows

            # Phase 1: text-based next detection
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    btn_text = (getattr(btn, "text", None) or "").lower().strip()
                    if any(kw in btn_text for kw in _NEXT_KW):
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": col_idx,
                            "target_button_text": getattr(btn, "text", None) or "",
                            "reason": f"\u68c0\u6d4b\u5230\u7ffb\u9875\u6309\u94ae: {btn_text}",
                            "wait_seconds": None,
                        }

            # Phase 2: number pagination
            for row_idx, row in enumerate(rows):
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                numbers = []
                for col_idx, t in enumerate(btn_texts):
                    n = self._extract_number(t)
                    if n is not None:
                        numbers.append((col_idx, t, n))
                if len(numbers) >= 3:
                    all_digits = sorted([n for _, _, n in numbers])
                    last_clicked = exchange.get("_last_clicked_number")

                    if last_clicked is None:
                        target_num = 2 if 2 in all_digits else all_digits[1]
                    else:
                        next_num = last_clicked + 1
                        if next_num > all_digits[-1]:
                            break
                        target_num = next_num

                    target_str = str(target_num)
                    for col_idx, t, n in numbers:
                        if n == target_num:
                            exchange["_last_clicked_number"] = target_num
                            return {
                                "action": "click_button",
                                "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": t,
                                "reason": f"\u6570\u5b57\u7ffb\u9875\uff0c\u70b9\u51fb\u7b2c{target_num}\u9875 (\u6309\u94ae\u6587\u672c: {t})",
                                "wait_seconds": None,
                            }
                    break

            # Phase 3: icon-only — click rightmost button with callback_data
            for row_idx in range(len(rows) - 1, -1, -1):
                row = rows[row_idx]
                if not row.buttons:
                    continue
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                all_empty = all(not t for t in btn_texts)
                if all_empty:
                    last_btn = row.buttons[-1]
                    if getattr(last_btn, "data", None):
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": len(row.buttons) - 1,
                            "target_button_text": "",
                            "reason": "\u7eaf\u56fe\u6807\uff0c\u70b9\u51fb\u6700\u53f3\u4fa7\u6309\u94ae",
                            "wait_seconds": None,
                        }

            # Phase 4: any remaining callback button as potential next
            clicked = exchange.get("_clicked_buttons") or set()
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    if getattr(btn, "data", None):
                        btn_text = (getattr(btn, "text", None) or "").strip().lower()
                        if any(kw in btn_text for kw in _FINISH_KW):
                            continue
                        if (row_idx, col_idx) in clicked:
                            continue
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": col_idx,
                            "target_button_text": getattr(btn, "text", None) or "",
                            "reason": f"\u5c1d\u8bd5\u70b9\u51fb\u5269\u4f59\u6309\u94ae: {btn_text}",
                            "wait_seconds": None,
                        }

        return {
            "action": "finish", "target_button_row": None,
            "target_button_col": None, "target_button_text": None,
            "reason": "\u65e0\u7ffb\u9875\u6309\u94ae\uff0c\u7ed3\u675f\u6536\u96c6",
            "wait_seconds": None,
        }

    async def _click_button(self, bot_username: str, row: int, col: int) -> bool:
        exchange = self._bot_exchange.get(bot_username)
        if not exchange:
            return False

        keyboard_msg = exchange.get("_keyboard_msg")
        if not keyboard_msg or not keyboard_msg.reply_markup:
            for ev in reversed(exchange.get("events", [])):
                msg = ev.message
                if msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows:
                    keyboard_msg = msg
                    exchange["_keyboard_msg"] = msg
                    # 重新校验 row/col 在新键盘中是否有效
                    if exchange.get("_row", 0) >= len(msg.reply_markup.rows):
                        exchange["_row"] = 0
                    if exchange.get("_col", 0) >= len(msg.reply_markup.rows[exchange.get("_row", 0)].buttons):
                        exchange["_col"] = 0
                    break
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
            if hasattr(target_btn, "data") and target_btn.data:
                await keyboard_msg.click(data=target_btn.data)
                btn_text = getattr(target_btn, "text", "") or "(无文字/图标按钮)"
                logger.info(f"[UserRelay] 已点击按钮 [{row},{col}] {btn_text}")
                return True

            if hasattr(target_btn, "url") and target_btn.url:
                url = str(target_btn.url)
                btn_text = getattr(target_btn, "text", "") or "(URL按钮)"
                logger.info(f"[UserRelay] 检测到 URL 按钮 [{row},{col}] {btn_text}, url={url}")
                if "t.me/" in url or "telegram.me/" in url:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    start_param = params.get("start", [None])[0]
                    if start_param:
                        entity = await self._client.get_entity(
                            parsed.path.strip("/")
                        )
                        await self._client.send_message(entity, f"/start {start_param}")
                        logger.info(f"[UserRelay] 已通过 deep link 翻页: /start {start_param}")
                        return True
                logger.warning(f"[UserRelay] URL 按钮无法点击 (非 t.me 链接): {url}")
                return False

            btn_text = getattr(target_btn, "text", "") or "(未知类型)"
            logger.warning(
                f"[UserRelay] 按钮 [{row},{col}] 类型不支持: {type(target_btn).__name__}"
            )
            return False
        except FloodWaitError as e:
            wait_seconds = e.seconds
            logger.warning(
                f"[UserRelay] 触发 FloodWait，需要等待 {wait_seconds} 秒 "
                f"(bot={bot_username})"
            )
            exchange["_min_click_interval"] = max(
                exchange.get("_min_click_interval", 0), wait_seconds
            )
            await asyncio.sleep(wait_seconds)
            try:
                if hasattr(target_btn, "data") and target_btn.data:
                    await keyboard_msg.click(data=target_btn.data)
                    btn_text = getattr(target_btn, "text", "") or "(无文字/图标按钮)"
                    logger.info(
                        f"[UserRelay] FloodWait 后重试成功 [{row},{col}] {btn_text}"
                    )
                    return True
                return False
            except Exception as retry_e:
                logger.error(
                    f"[UserRelay] FloodWait 后重试点击失败 [{row},{col}]: {retry_e}"
                )
                return False
        except Exception as e:
            logger.error(f"[UserRelay] 点击按钮失败 [{row},{col}]: {e}")
            return False

    async def _process_all_collected(self, bot_username: str):
        exchange = self._bot_exchange.pop(bot_username, None)
        if not exchange:
            return

        user_id = exchange.get("user_id")
        code = exchange.get("code")
        # 清理对应的缓存字典，防止无限增长
        self._cleanup_code_dicts(code)
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
            await self._wait_all_cached(bot_username, timeout=60)
            lock = self._cache_locks.get(code)
            if lock:
                async with lock:
                    pass
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
            bfm = record.get("batch_file_meta", "")
            if isinstance(bfm, list):
                bfm_len = len(bfm)
            elif isinstance(bfm, str) and bfm:
                try:
                    bfm_len = len(json.loads(bfm))
                except (json.JSONDecodeError, TypeError):
                    bfm_len = 0
            else:
                bfm_len = 0
            logger.info(
                f"[UserRelay] DB记录 (code={code}): primary_mid={record.get('primary_channel_msg_id')}, "
                f"batch_ids_str={batch_ids_str}, "
                f"batch_file_meta_len={bfm_len}"
            )

            bfm_parsed = None
            if isinstance(bfm, list):
                bfm_parsed = bfm
            elif isinstance(bfm, str) and bfm:
                try:
                    bfm_parsed = json.loads(bfm)
                except (json.JSONDecodeError, TypeError):
                    bfm_parsed = []

            if bfm_parsed and isinstance(bfm_parsed, list):
                healed = await self._self_heal_file_ids(code, bfm_parsed)
                if healed > 0:
                    fids_update = {}
                    fids_list = [e.get("file_id", "") for e in bfm_parsed
                                 if isinstance(e, dict) and e.get("file_id")]
                    fids_update["file_ids"] = ",".join(fids_list)
                    fids_update["batch_file_meta"] = json.dumps(bfm_parsed)
                    await files_col.update_one(
                        {"file_code": code}, {"$set": fids_update}
                    )
                    logger.info(
                        f"[UserRelay] 自修复已保存: code={code}, "
                        f"修复了 {healed} 个 file_id"
                    )

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
        if exchange:
            code = exchange.get("code", "")
            self._cleanup_code_dicts(code)
            if exchange.get("_settle_task") and not exchange["_settle_task"].done():
                exchange["_settle_task"].cancel()
        if self._pending_cleanup:
            self._pending_cleanup(bot_username)

    def _cleanup_code_dicts(self, code: str):
        """清理与指定 code 关联的所有缓存字典条目，防止内存泄漏。"""
        if code:
            self._pending_cache_counts.pop(code, None)
            self._pending_cache_events.pop(code, None)
            self._cache_locks.pop(code, None)
            self._event_locks.pop(code, None)

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        if not self._client:
            return False

        try:
            entity = await self._client.get_entity(bot_username)
            logger.info(
                f"[UserRelay] 目标实体: {type(entity).__name__}, "
                f"id={getattr(entity, 'id', '?')}, "
                f"username=@{getattr(entity, 'username', '?')}"
            )
            await self._client.send_message(entity, code)
            now = asyncio.get_event_loop().time()
            self._bot_exchange[bot_username.lower()] = {
                "user_id": user_id,
                "code": code,
                "events": [],
                "_expires": now + 120,
                "_settle_task": None,
                "_ai_running": False,
                "_keyboard_msg": None,
                "_last_clicked_number": None,
                "_clicked_buttons": set(),
                "_min_click_interval": 0,
                "_last_click_time": 0,
            }
            self._restart_settle(
                self._bot_exchange[bot_username.lower()], bot_username.lower(),
                settle_wait=_INITIAL_SETTLE_WAIT,
            )
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


# ─── 向后兼容 ──────────────────────────────────────────────────────
# decoder_bot.py 仍使用此单例，自动委托给 relay_pool 中的第一个就绪实例

user_relay = UserRelay()