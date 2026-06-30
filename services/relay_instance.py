"""单个中继账号实例 — 独立 Telethon 客户端
- 每个账号独立 TelegramClient + session 文件
- 支持并发处理解码任务
- session 文件持久化，VPS 重启后自动恢复
"""
import asyncio
import json
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import PeerChannel

from config import settings

_SETTLE_WAIT = 5
_INITIAL_SETTLE_WAIT = 20


class RelayInstance:
    """单个中继账号实例"""

    def __init__(self, account_id: int, api_id: int, api_hash: str, phone: str):
        self.account_id = account_id
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self._client: TelegramClient | None = None
        self._decoder_bot_entity = None
        self._up_bot_entity = None
        self._storage_channel_entity = None
        # bot_username -> exchange info
        self._bot_exchange: dict[str, dict] = {}
        self._media_buffers: dict[str, dict] = {}
        self._session_path = str(
            Path(__file__).parent.parent / "data" / f"relay_session_{phone}"
        )
        self._ready = asyncio.Event()
        self._pending_cleanup = None
        self._relay_user_id = None
        # 并发控制
        self.is_busy = False
        self._lock = asyncio.Lock()
        # 缓存锁
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._handlers_registered = False
        self._pending_cache_counts: dict[str, int] = {}
        self._pending_cache_events: dict[str, asyncio.Event] = {}

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
        logger.info(f"[RelayInstance:{self.phone}] 验证码已发送到 Telegram，等待管理员通过管理机器人提交...")

        for i in range(100):
            await asyncio.sleep(3)
            code = await get_config("relay_auth_code")
            if code and code.strip():
                await set_config("relay_auth_pending", "0")
                await set_config("relay_auth_code", "")
                logger.info(f"[RelayInstance:{self.phone}] 已收到管理员提交的验证码")
                return code.strip()

        await set_config("relay_auth_pending", "0")
        logger.error(f"[RelayInstance:{self.phone}] 等待验证码超时（5分钟）")
        return None

    async def start(self):
        await self._report_status("connecting")

        self._client = TelegramClient(
            self._session_path,
            self.api_id,
            self.api_hash,
        )

        await self._client.connect()

        if not await self._client.is_user_authorized():
            await self._client.send_code_request(self.phone)
            logger.info(f"[RelayInstance:{self.phone}] 验证码已发送到 Telegram 账号")
            logger.info(f"[RelayInstance:{self.phone}] 等待管理员通过 /relay_code 提交验证码...")
            code = await self._wait_for_admin_code()

            if not code:
                logger.error(f"[RelayInstance:{self.phone}] 无法获取验证码，登录失败")
                await self._report_status("offline")
                await self._client.disconnect()
                return

            try:
                await self._client.sign_in(self.phone, code)
            except SessionPasswordNeededError:
                logger.error(
                    f"[RelayInstance:{self.phone}] 该账号开启了二步验证，暂不支持"
                )
                await self._report_status("offline")
                await self._client.disconnect()
                return
            except Exception as e:
                logger.error(f"[RelayInstance:{self.phone}] 登录失败: {e}")
                await self._report_status("offline")
                await self._client.disconnect()
                return

        me = await self._client.get_me()
        self._relay_user_id = me.id
        logger.info(f"[RelayInstance:{self.phone}] 已登录: {me.first_name} (@{me.username}), id={me.id}")

        try:
            self._decoder_bot_entity = await self._client.get_entity(
                settings.DECODER_BOT_USERNAME
            )
        except Exception as e:
            logger.warning(f"[RelayInstance:{self.phone}] 无法获取解码机器人 @{settings.DECODER_BOT_USERNAME}: {e}")

        try:
            self._up_bot_entity = await self._client.get_entity(
                settings.UPLOAD_BOT_USERNAME
            )
        except Exception as e:
            logger.warning(f"[RelayInstance:{self.phone}] 无法获取上传机器人 @{settings.UPLOAD_BOT_USERNAME}: {e}")

        try:
            self._storage_channel_entity = PeerChannel(settings.MAIN_STORAGE_CHANNEL_ID)
        except Exception:
            self._storage_channel_entity = None

        self._register_handlers()
        self._ready.set()
        await self._report_status("online")
        logger.info(f"[RelayInstance:{self.phone}] 中继已就绪")

    async def login_with_credentials(self, api_id: int, api_hash: str, phone: str):
        """使用指定凭证登录（用于动态添加账号）"""
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone

        if not self._client or not self._client.is_connected():
            self._client = TelegramClient(self._session_path, api_id, api_hash)
            await self._client.connect()
        else:
            self._client.api_id = api_id
            self._client.api_hash = api_hash

        if not await self._client.is_user_authorized():
            await self._client.send_code_request(phone)
            code = await self._wait_for_admin_code()
            if not code:
                raise RuntimeError("验证码获取失败")
            try:
                await self._client.sign_in(self.phone, code)
            except SessionPasswordNeededError:
                raise RuntimeError("账号开启二步验证，暂不支持")

        me = await self._client.get_me()
        self._relay_user_id = me.id
        self._decoder_bot_entity = await self._client.get_entity(settings.DECODER_BOT_USERNAME)
        self._storage_channel_entity = PeerChannel(settings.MAIN_STORAGE_CHANNEL_ID)
        self._register_handlers()
        self._ready.set()
        logger.info(f"[RelayInstance:{self.phone}] 动态添加并登录成功: {me.username}")

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        """发送外部码解码请求"""
        if not self._client:
            return False
        async with self._lock:
            if self.is_busy:
                logger.warning(f"[RelayInstance:{self.phone}] 账号正忙，拒绝新请求")
                return False
            self.is_busy = True
        try:
            return await self._do_send_external_code(bot_username, code, user_id)
        finally:
            self.is_busy = False

    async def _do_send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        try:
            entity = await self._client.get_entity(bot_username)
            logger.info(
                f"[RelayInstance:{self.phone}] 目标实体: {type(entity).__name__}, "
                f"id={getattr(entity, 'id', '?')}, username=@{getattr(entity, 'username', '?')}"
            )
            # 检查 bot 是否已被占用（并发保护）
            key = bot_username.lower()
            if key in self._bot_exchange:
                logger.warning(
                    f"[RelayInstance:{self.phone}] bot @{bot_username} 正被占用，"
                    f"拒绝新请求 (user={user_id}, code={code})"
                )
                return False
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
            logger.info(f"[RelayInstance:{self.phone}] 已向 @{bot_username} 发送外部码 (user={user_id}, code={code})")
            return True
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 向 @{bot_username} 发送失败: {e}")
            return False

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
            if self._up_bot_entity:
                # 发送到 Up Bot，带 EXTERNAL_RELAY 标记统一上传到存储频道
                caption = f"EXTERNAL_RELAY:{user_id}:{code}"
                await self._client.send_file(
                    self._up_bot_entity, msg.media, caption=caption
                )
            else:
                logger.warning(f"[RelayInstance:{self.phone}] Up Bot 不可用，跳过文件 (code={code})")
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 发送到 Up Bot 失败 (code={code}): {e}")
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
            r"(\d+)\s*秒", r"(\d+)\s*seconds?", r"(\d+)\s*secs?",
            r"wait\s*(\d+)", r"等\s*(\d+)", r"稍等\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return max(int(m.group(1)), 1)
        return 5

    def _register_handlers(self):
        if self._handlers_registered:
            return
        self._handlers_registered = True
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
                    f"[RelayInstance:{self.phone}] 收到消息: sender({sender_info}), "
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
                    return
                self._media_buffers[media_group_id] = {
                    "events": [event],
                    "bot_username": bot_username,
                    "_expires": now_ts + 5,
                }
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
            return
        events_list = buf["events"]
        user_id = exchange.get("user_id")
        code = exchange.get("code")
        for ev in events_list:
            if code:
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
            try:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

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
        try:
            while True:
                if bot_username not in self._bot_exchange:
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
                    continue
                action = decision.get("action", "finish")
                reason = decision.get("reason", "")
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
                        except Exception:
                            pass
                    await self._cleanup_exchange(bot_username)
                    break
                elif action == "wait":
                    wait_sec = decision.get("wait_seconds", 5)
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        exchange["_last_click_time"] = asyncio.get_event_loop().time() + wait_sec
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
                            await asyncio.sleep(remaining)
                    exchange = self._bot_exchange.get(bot_username)
                    row = decision.get("target_button_row")
                    col = decision.get("target_button_col")
                    if row is None or col is None:
                        await self._process_all_collected(bot_username)
                        break
                    events_before = len(exchange.get("events", []))
                    clicked = await self._click_button(bot_username, row, col)
                    if not clicked:
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
                        if events_after > events_before:
                            stale_clicks = 0
                        else:
                            stale_clicks += 1
                            if stale_clicks >= 3:
                                await self._process_all_collected(bot_username)
                                break
                    continue
                else:
                    await self._process_all_collected(bot_username)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 翻页循环异常: {e}")
            await self._process_all_collected(bot_username)
        finally:
            if bot_username in self._bot_exchange:
                self._bot_exchange[bot_username]["_ai_running"] = False

    def _make_decision(self, exchange: dict) -> dict:
        _NEXT_KW = ("next", "\u4e0b\u4e00\u9875", "\u4e0b\u4e00\u9801", "\u4e0b\u4e00\u7ec4",
                     "\u2192", "\u25b6", "\u27a1", ">>", "\u00bb")
        _ERROR_KW = ("\u672a\u627e\u5230", "\u5df2\u8fc7\u671f", "\u5df2\u5931\u6548",
                     "\u4e0d\u5b58\u5728", "not found", "expired", "invalid")
        _RATE_LIMIT_KW = ("\u8bf7\u7a0d\u540e", "\u8bf7\u7b49\u5f85", "\u7a0d\u540e\u518d\u8bd5",
                         "\u901f\u5ea6\u592a\u5feb", "\u7ffb\u9875\u592a\u5feb", "\u64cd\u4f5c\u592a\u5feb",
                         "\u9891\u7e41", "\u8bf7\u6162\u4e00\u70b9", "\u6162\u4e00\u70b9",
                         "too fast", "wait", "slow down", "rate limit", "\u8bf7\u52ff\u8fc7\u5feb")
        _FINISH_KW = {"finish", "done", "\u5b8c\u6210", "\u7ed3\u675f"}

        msg_events = exchange.get("events", [])
        if not msg_events:
            return {"action": "finish", "target_button_row": None, "target_button_col": None,
                    "target_button_text": None, "reason": "\u65e0\u6d88\u606f", "wait_seconds": None}

        for ev in msg_events:
            text = (getattr(ev.message, "message", None) or "").lower()
            for ek in _ERROR_KW:
                if ek in text:
                    return {"action": "error", "target_button_row": None, "target_button_col": None,
                            "target_button_text": None, "reason": f"\u68c0\u6d4b\u5230\u9519\u8bef: {ek}",
                            "wait_seconds": None}

        for ev in msg_events:
            msg = ev.message
            text = (getattr(msg, "message", None) or "").lower()
            for rk in _RATE_LIMIT_KW:
                if rk in text:
                    wait_sec = self._extract_wait_seconds(msg)
                    exchange["_min_click_interval"] = max(exchange.get("_min_click_interval", 0), wait_sec)
                    return {"action": "wait", "target_button_row": None, "target_button_col": None,
                            "target_button_text": None,
                            "reason": f"\u68c0\u6d4b\u5230\u9650\u5236: {rk}",
                            "wait_seconds": wait_sec}

        for ev in msg_events:
            msg = ev.message
            if not (msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows):
                continue
            rows = msg.reply_markup.rows
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    btn_text = (getattr(btn, "text", None) or "").lower().strip()
                    if any(kw in btn_text for kw in _NEXT_KW):
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": f"\u68c0\u6d4b\u5230\u7ffb\u9875\u6309\u94ae: {btn_text}",
                                "wait_seconds": None}
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
                    for col_idx, t, n in numbers:
                        if n == target_num:
                            exchange["_last_clicked_number"] = target_num
                            return {"action": "click_button", "target_button_row": row_idx,
                                    "target_button_col": col_idx, "target_button_text": t,
                                    "reason": f"\u6570\u5b57\u7ffb\u9875\u7b2c{target_num}\u9875",
                                    "wait_seconds": None}
                    break
            for row_idx in range(len(rows) - 1, -1, -1):
                row = rows[row_idx]
                if not row.buttons:
                    continue
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                if all(not t for t in btn_texts):
                    last_btn = row.buttons[-1]
                    if getattr(last_btn, "data", None):
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": len(row.buttons) - 1, "target_button_text": "",
                                "reason": "\u7eaf\u56fe\u6807", "wait_seconds": None}
            clicked = exchange.get("_clicked_buttons") or set()
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    if getattr(btn, "data", None):
                        btn_text = (getattr(btn, "text", None) or "").strip().lower()
                        if any(kw in btn_text for kw in _FINISH_KW):
                            continue
                        if (row_idx, col_idx) in clicked:
                            continue
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": f"\u5c1d\u8bd5\u70b9\u51fb: {btn_text}",
                                "wait_seconds": None}
        return {"action": "finish", "target_button_row": None, "target_button_col": None,
                "target_button_text": None, "reason": "\u65e0\u7ffb\u9875\u6309\u94ae",
                "wait_seconds": None}

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
                    break
        if not keyboard_msg or not keyboard_msg.reply_markup:
            return False
        reply_markup = keyboard_msg.reply_markup
        if not hasattr(reply_markup, "rows"):
            return False
        try:
            target_row = reply_markup.rows[row]
            target_btn = target_row.buttons[col]
        except (IndexError, AttributeError):
            return False
        exchange.pop("_keyboard_msg", None)
        try:
            if hasattr(target_btn, "data") and target_btn.data:
                await keyboard_msg.click(data=target_btn.data)
                return True
            if hasattr(target_btn, "url") and target_btn.url:
                url = str(target_btn.url)
                if "t.me/" in url or "telegram.me/" in url:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    start_param = params.get("start", [None])[0]
                    if start_param:
                        entity = await self._client.get_entity(parsed.path.strip("/"))
                        await self._client.send_message(entity, f"/start {start_param}")
                        return True
                return False
            return False
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            try:
                if hasattr(target_btn, "data") and target_btn.data:
                    await keyboard_msg.click(data=target_btn.data)
                    return True
            except Exception:
                pass
            return False
        except Exception:
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
        if not all_events:
            if self._decoder_bot_entity:
                try:
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_ERROR:{user_id}:{code}:目标机器人未返回任何文件",
                    )
                except Exception:
                    pass
            if self._pending_cleanup:
                self._pending_cleanup(bot_username)
            return
        # 所有文件已发送到 Up Bot，发送 EXTERNAL_DONE 信号触发批量写入
        try:
            await self._wait_all_cached(bot_username, timeout=60)
            if self._up_bot_entity:
                await self._client.send_message(
                    self._up_bot_entity,
                    f"EXTERNAL_DONE:{user_id}:{code}",
                )
                logger.info(f"[RelayInstance:{self.phone}] 已通知 Up Bot 完成外部文件收集: code={code}")
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 通知 Up Bot 失败 (code={code}): {e}")
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

    async def deliver_cached(self, user_id: int, code: str) -> bool:
        if not self._client:
            return False
        try:
            from database import get_file_records_col
            col = get_file_records_col()
            record = await col.find_one({"file_code": code})
            if not record:
                return False
            file_ids_str = record.get("file_ids", "") or ""
            if not isinstance(file_ids_str, str):
                file_ids_str = str(file_ids_str)
            file_ids = [f.strip() for f in file_ids_str.split(",") if f.strip()]
            if file_ids:
                if self._decoder_bot_entity:
                    for fid in file_ids:
                        try:
                            await self._client.send_file(self._decoder_bot_entity, fid)
                        except Exception:
                            pass
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_DELIVER:{user_id}:{code}",
                    )
                return True
            # expired
            await col.delete_one({"file_code": code})
            if self._decoder_bot_entity:
                await self._client.send_message(
                    self._decoder_bot_entity,
                    f"RELAY_RENEW:{user_id}:{code}",
                )
            return False
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 缓存交付失败 (code={code}): {e}")
            return False

    async def shutdown(self):
        if self._client:
            await self._client.disconnect()
