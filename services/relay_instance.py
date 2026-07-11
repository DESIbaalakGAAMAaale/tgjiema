"""单个中继账号实例 — 独立 Telethon 客户端
- 每个账号独立 TelegramClient + session 文件
- 支持并发处理解码任务
- session 文件持久化，VPS 重启后自动恢复
"""
import asyncio
import re
import time
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage
from utils.task_utils import create_safe_task
from telethon.errors import SessionPasswordNeededError, FloodWaitError

from config import settings

_SETTLE_WAIT = 1.5
_INITIAL_SETTLE_WAIT = 3

# B1: 指数退避参数
_BOT_BACKOFF_BASE = 5        # 初始退避秒数
_BOT_BACKOFF_MAX = 300        # 最大退避秒数(5分钟)
_BOT_BACKOFF_THRESHOLD = 5   # 连续限速 N 次触发熔断
_BOT_CIRCUIT_BREAK_SEC = 600  # 熔断持续时间(10分钟)

_BAN_KEYWORDS = [
    "deactivated", "banned", "auth_key", "unauthorized",
    "session_revoked", "auth_key_duplicated",
    "user_deactivated", "phone_number_banned",
    "phone is banned", "user is deactivated",
]


def _is_ban_error(exc: Exception) -> bool:
    """判断异常是否为账号封禁/受限/失效错误。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in _BAN_KEYWORDS)


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
        # idx_bot 确认等待：code -> Event
        self._ready_events: dict[str, asyncio.Event] = {}
        # bot_override 内存缓存（避免每次外发都全表扫描 SQLite）
        self._bot_override_cache: list[dict] = []
        self._bot_override_cache_ts: float = 0
        # P1-8:跟踪内部 create_task 产生的后台任务,shutdown() 时统一 cancel 并 await 完成,
        # 避免孤儿任务/资源泄漏(参考批次一 cache.py 的 _pending_tasks 模式)。
        self._background_tasks: set[asyncio.Task] = set()
        # P1-8:start() 守卫,避免重复创建 cleanup 循环(重复调用 start 时)。
        self._cleanup_loop_started = False
        self._shutdown_done = False
        # A2: FloodWait / 封禁自愈
        self._floodwait_until: float = 0.0  # FloodWait 到期时间戳, 0 表示无限制
        self._ban_detected: bool = False  # 是否检测到封禁/受限
        # P3: noforwards 受保护聊天回退 — 下载到内存缓冲,稍后批量作为媒体组发送(保持格式)
        self._download_buffers: dict[str, list] = {}
        # P3-2: 消息引用缓冲 — 所有文件先缓冲,最后统一批量作为媒体组发送(避免逐个发送破坏相册格式)
        self._pending_msgs: dict[str, list] = {}
        # B1: 指数退避 — bot 级别连续限速计数,等待时间指数递增
        # bot_username_lower -> {"count": N, "next_backoff": seconds, "last_at": ts}
        self._bot_backoff: dict[str, dict] = {}

    @property
    def is_ready(self) -> bool:
        if not self._ready.is_set():
            return False
        if self._ban_detected:
            return False
        if time.time() < self._floodwait_until:
            return False
        return True

    def record_flood_wait(self, seconds: int):
        """记录 FloodWait,暂时标记账号不可用。"""
        self._floodwait_until = time.time() + seconds + 5
        self._spawn(self._report_status("floodwait", f"限制{seconds}秒"))
        logger.warning(
            f"[RelayInstance:{self.phone}] FloodWait {seconds}s, "
            f"暂时不可用"
        )

    def record_ban(self, reason: str):
        """记录账号封禁/受限。"""
        if not self._ban_detected:
            logger.error(f"[RelayInstance:{self.phone}] 账号受限/封禁: {reason}")
        self._ban_detected = True
        self._spawn(self._report_status("banned", reason[:200]))

    def clear_expired_floodwait(self):
        """清除已到期的 FloodWait 限制。"""
        if self._floodwait_until > 0 and time.time() >= self._floodwait_until:
            self._floodwait_until = 0.0
            logger.info(f"[RelayInstance:{self.phone}] FloodWait 已到期,恢复可用")
            # 写回 DB 状态,避免 admin_bot 显示过时的 floodwait
            self._spawn(self._report_status("online", "FloodWait 已到期"))

    async def check_health(self) -> bool:
        """A2: 主动健康检查,验证账号是否仍可用。

        用于:
        - 恢复误判封禁的账号
        - 恢复 FloodWait 到期的账号
        - 检测客户端断线并尝试重连
        """
        if not self._client:
            return False
        try:
            if not self._client.is_connected():
                await self._client.connect()
                if not await self._client.is_user_authorized():
                    return False
            me = await self._client.get_me()
            if me:
                if self._ban_detected or self._floodwait_until > 0:
                    logger.info(f"[RelayInstance:{self.phone}] 健康检查通过,恢复可用")
                self._ban_detected = False
                self._floodwait_until = 0.0
                await self._report_status("online", f"{me.first_name}(@{me.username})")
                return True
        except FloodWaitError as e:
            self._floodwait_until = time.time() + e.seconds + 5
            logger.warning(
                f"[RelayInstance:{self.phone}] 健康检查触发 FloodWait: {e.seconds}s"
            )
            return False
        except Exception as e:
            if _is_ban_error(e):
                if not self._ban_detected:
                    logger.error(f"[RelayInstance:{self.phone}] 健康检查检测到封禁: {e}")
                self._ban_detected = True
                try:
                    await self._report_status("banned", str(e)[:200])
                except Exception:
                    pass
            return False
        return False

    @property
    def relay_user_id(self) -> int | None:
        return self._relay_user_id

    def set_pending_cleanup(self, callback):
        self._pending_cleanup = callback

    async def _report_status(self, status: str, info: str = ""):
        """将账号状态写入 relay_pool.db 的 relay_accounts.status 字段。

        idx_bot 进程写入，admin_bot 进程读取。
        """
        try:
            from database.relay_db import get_relay_db
            db = await get_relay_db()
            await db.update_account_status(self.phone, status, info)
        except Exception:
            pass

    # ── B1: 指数退避 + 熔断 ──

    def _record_rate_limit(self, bot_username: str, suggested_wait: int = 0) -> float:
        """记录一次限速事件,返回本次应等待的秒数(指数递增)。
        suggested_wait: 解码器/Telegram 建议的等待秒数,0 表示未知。
        """
        key = bot_username.lower()
        state = self._bot_backoff.get(key, {"count": 0, "next_backoff": _BOT_BACKOFF_BASE, "last_at": 0.0})
        state["count"] += 1
        state["last_at"] = time.time()
        # 取 max(指数退避, 建议等待),避免低于解码器要求
        backoff = state["next_backoff"]
        actual_wait = max(backoff, suggested_wait) if suggested_wait > 0 else backoff
        # 计算下次退避: 指数递增 *2,上限 _BOT_BACKOFF_MAX
        state["next_backoff"] = min(state["next_backoff"] * 2, _BOT_BACKOFF_MAX)
        self._bot_backoff[key] = state
        logger.warning(
            f"[RelayInstance:{self.phone}] @{bot_username} 限速第{state['count']}次, "
            f"退避 {actual_wait:.0f}s (下次 {state['next_backoff']:.0f}s)"
        )
        return actual_wait

    def _reset_rate_limit(self, bot_username: str):
        """成功完成一次解码后重置该 bot 的退避计数。"""
        key = bot_username.lower()
        if key in self._bot_backoff:
            old = self._bot_backoff.pop(key)
            logger.info(f"[RelayInstance:{self.phone}] @{bot_username} 退避重置 (之前限速{old.get('count', 0)}次)")

    def _is_circuit_broken(self, bot_username: str) -> tuple[bool, float]:
        """检查 bot 是否被熔断(连续限速超阈值)。
        返回 (是否熔断, 剩余熔断秒数)。
        """
        key = bot_username.lower()
        state = self._bot_backoff.get(key)
        if not state or state.get("count", 0) < _BOT_BACKOFF_THRESHOLD:
            return False, 0
        elapsed = time.time() - state.get("last_at", 0)
        remaining = _BOT_CIRCUIT_BREAK_SEC - elapsed
        if remaining <= 0:
            # 熔断已过期,重置
            self._bot_backoff.pop(key, None)
            logger.info(f"[RelayInstance:{self.phone}] @{bot_username} 熔断已过期,恢复可用")
            return False, 0
        return True, remaining

    def _spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """创建内部后台任务并纳入 _background_tasks 跟踪(P1-8)。

        任务完成后自动从集合中移除,防止集合无限增长。
        """
        task = create_safe_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _wait_for_admin_code(self) -> str | None:
        from database.session import get_config, set_config

        await set_config(f"relay_auth_pending:{self.phone}", "1")
        await self._report_status("pending_auth", "等待管理员提交验证码")
        logger.info(f"[RelayInstance:{self.phone}] 验证码已发送到 Telegram，等待管理员通过管理机器人提交...")

        for i in range(100):
            await asyncio.sleep(3)
            code = await get_config(f"relay_auth_code:{self.phone}")
            if code and code.strip():
                await set_config(f"relay_auth_pending:{self.phone}", "0")
                await set_config(f"relay_auth_code:{self.phone}", "")
                logger.info(f"[RelayInstance:{self.phone}] 已收到管理员提交的验证码")
                return code.strip()

        await set_config(f"relay_auth_pending:{self.phone}", "0")
        logger.error(f"[RelayInstance:{self.phone}] 等待验证码超时（5分钟）")
        return None

    async def _wait_for_admin_password(self) -> str | None:
        """等待管理员提交二步验证密码(类似验证码流程,5分钟超时)。"""
        from database.session import get_config, set_config

        await set_config(f"relay_password_pending:{self.phone}", "1")
        await self._report_status("pending_password", "等待管理员提交二步验证密码")
        logger.info(f"[RelayInstance:{self.phone}] 该账号开启了二步验证,等待管理员通过 /relay_password 提交密码...")

        for i in range(100):
            await asyncio.sleep(3)
            password = await get_config(f"relay_auth_password:{self.phone}")
            if password and password.strip():
                await set_config(f"relay_password_pending:{self.phone}", "0")
                await set_config(f"relay_auth_password:{self.phone}", "")
                logger.info(f"[RelayInstance:{self.phone}] 已收到管理员提交的二步验证密码")
                return password.strip()

        await set_config(f"relay_password_pending:{self.phone}", "0")
        logger.error(f"[RelayInstance:{self.phone}] 等待二步验证密码超时（5分钟）")
        return None

    async def start(self):
        await self._report_status("connecting")

        # 诊断日志:记录实际使用的凭证(脱敏)
        logger.info(f"[RelayInstance:{self.phone}] 启动登录, api_id={self.api_id}(type={type(self.api_id).__name__}), api_hash={self.api_hash[:10]}...(len={len(self.api_hash)})")

        self._client = TelegramClient(
            self._session_path,
            self.api_id,
            self.api_hash,
        )

        try:
            await self._client.connect()
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] connect 失败(api_id/api_hash 可能无效): {e}")
            logger.error(f"[RelayInstance:{self.phone}] 提示: 如果确认 api_id/api_hash 正确,可能是 DB 存储值与 .env 不匹配")
            await self._report_status("offline", f"连接失败:{str(e)[:100]}")
            return

        if not await self._client.is_user_authorized():
            try:
                await self._client.send_code_request(self.phone)
            except Exception as e:
                logger.error(f"[RelayInstance:{self.phone}] send_code_request 失败: {e}")
                logger.error(f"[RelayInstance:{self.phone}] 请检查 .env 中的 RELAY_API_ID 和 RELAY_API_HASH 是否正确")
                logger.error(f"[RelayInstance:{self.phone}] 如已更新 .env,需从 DB 删除该账号后重新添加(旧 api_hash 可能已加密存储)")
                await self._report_status("offline", f"发送验证码失败:{str(e)[:100]}")
                await self._client.disconnect()
                return
            logger.info(f"[RelayInstance:{self.phone}] 验证码已发送到 Telegram 账号")
            logger.info(f"[RelayInstance:{self.phone}] 等待管理员通过 /relay_code 提交验证码...")
            code = await self._wait_for_admin_code()

            if not code:
                logger.error(f"[RelayInstance:{self.phone}] 无法获取验证码，登录失败")
                await self._report_status("offline", "等待验证码超时(5分钟)")
                await self._client.disconnect()
                return

            try:
                await self._client.sign_in(self.phone, code)
            except SessionPasswordNeededError:
                logger.info(f"[RelayInstance:{self.phone}] 该账号开启了二步验证,等待密码")
                password = await self._wait_for_admin_password()
                if not password:
                    logger.error(f"[RelayInstance:{self.phone}] 无法获取二步验证密码，登录失败")
                    await self._report_status("offline", "等待二步验证密码超时(5分钟)")
                    await self._client.disconnect()
                    return
                try:
                    await self._client.sign_in(password=password)
                except Exception as e:
                    logger.error(f"[RelayInstance:{self.phone}] 二步验证密码错误或登录失败: {e}")
                    await self._report_status("offline", f"二步验证失败:{str(e)[:100]}")
                    await self._client.disconnect()
                    return
            except Exception as e:
                logger.error(f"[RelayInstance:{self.phone}] 登录失败: {e}")
                await self._report_status("offline", f"登录失败:{str(e)[:100]}")
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

        self._register_handlers()
        self._ready.set()
        await self._report_status("online", f"{me.first_name}(@{me.username})")
        logger.info(f"[RelayInstance:{self.phone}] 中继已就绪")

        # 启动周期清理冷却记录的后台任务(P1-8:start 守卫 + 任务跟踪)
        if not self._cleanup_loop_started:
            self._cleanup_loop_started = True
            self._spawn(self._cleanup_cooldowns_loop(), name="relay-cleanup-cooldowns")

    async def _cleanup_cooldowns_loop(self):
        """每 10 分钟清理一次过期的 bot_cooldown 记录"""
        try:
            from database.relay_db import get_relay_db
            while True:
                await asyncio.sleep(600)
                try:
                    relay_db = await get_relay_db()
                    if relay_db:
                        await relay_db.cleanup_cooldowns()
                except Exception as e:
                    logger.debug(f"[RelayInstance:{self.phone}] cleanup_cooldowns 失败: {e}")
        except asyncio.CancelledError:
            pass

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
                logger.info(f"[RelayInstance:{self.phone}] 该账号开启了二步验证,等待密码")
                password = await self._wait_for_admin_password()
                if not password:
                    raise RuntimeError("二步验证密码获取超时")
                try:
                    await self._client.sign_in(password=password)
                except Exception as e:
                    raise RuntimeError(f"二步验证密码错误或登录失败: {e}")

        me = await self._client.get_me()
        self._relay_user_id = me.id
        self._decoder_bot_entity = await self._client.get_entity(settings.DECODER_BOT_USERNAME)
        self._register_handlers()
        self._ready.set()
        await self._report_status("online", f"{me.first_name}(@{me.username})")
        logger.info(f"[RelayInstance:{self.phone}] 动态添加并登录成功: {me.username}")

    async def send_external_code(self, bot_username: str, code: str, user_id: int) -> bool:
        """发送外部码解码请求"""
        if not self._client:
            return False
        # P1-9: 目标解码器 bot 白名单校验,避免被诱导向任意 bot 发消息
        try:
            from config import settings
            allowed = getattr(settings, "ALLOWED_DECODER_BOTS", "")
            if allowed:
                allowed_set = {b.strip().lower().lstrip("@") for b in allowed.split(",") if b.strip()}
                if bot_username.lower().lstrip("@") not in allowed_set:
                    logger.warning(
                        f"[RelayInstance:{self.phone}] 拒绝向非白名单 bot 发送: @{bot_username} (user={user_id})"
                    )
                    return False
        except Exception as e:
            logger.debug(f"[RelayInstance] 白名单校验异常(放行): {e}")
        # B1: 检查 bot 是否被熔断(连续限速超阈值)
        broken, remaining = self._is_circuit_broken(bot_username)
        if broken:
            logger.warning(
                f"[RelayInstance:{self.phone}] @{bot_username} 已熔断(连续限速),"
                f"拒绝请求,剩余 {remaining:.0f}s (user={user_id}, code={code})"
            )
            return False
        # 检查冷却期（在锁外执行，避免阻塞整个实例）
        from database.relay_db import get_relay_db
        relay_db = await get_relay_db()
        if relay_db:
            cooldown = await relay_db.get_bot_cooldown(bot_username)
            if cooldown > 0:
                logger.info(
                    f"[RelayInstance:{self.phone}] @{bot_username} 在冷却期，"
                    f"等待 {cooldown:.0f}s"
                )
                await asyncio.sleep(cooldown)
        # 在加锁前先检查 mapped_codes 本地缓存：已映射的码无需重新发送，
        # 返回 False 让调用方从存储直接投递，避免 is_busy 泄漏和静默失败
        try:
            if relay_db and await relay_db.is_code_mapped(code):
                logger.info(f"[RelayInstance:{self.phone}] 码已映射（本地缓存），跳过发送: code={code}")
                return False
        except Exception:
            pass
        async with self._lock:
            if self.is_busy:
                logger.warning(f"[RelayInstance:{self.phone}] 账号正忙，拒绝新请求")
                return False
            self.is_busy = True
        result = await self._do_send_external_code(bot_username, code, user_id, relay_db)
        if not result:
            self.is_busy = False
        return result

    async def _do_send_external_code(self, bot_username: str, code: str, user_id: int, relay_db=None) -> bool:
        try:

            # 检查 bot_overrides 覆盖规则（按最长前缀匹配，带内存缓存避免每次全表扫描）
            try:
                import time
                now = time.time()
                if now - self._bot_override_cache_ts > 60:
                    if relay_db is None:
                        from database.relay_db import get_relay_db
                        relay_db = await get_relay_db()
                    if relay_db:
                        self._bot_override_cache = await relay_db.list_bot_overrides()
                        self._bot_override_cache_ts = now
                for ov in self._bot_override_cache:
                    if ov.get("is_active") and code.startswith(ov["prefix"]):
                        logger.info(f"[RelayInstance:{self.phone}] 前缀覆盖: {code} → @{ov['bot_username']}")
                        bot_username = ov["bot_username"]
                        break
            except Exception:
                pass

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
            now = asyncio.get_running_loop().time()
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
                "_page_count": 0,
            }
            self._restart_settle(
                self._bot_exchange[bot_username.lower()], bot_username.lower(),
                settle_wait=_INITIAL_SETTLE_WAIT,
            )
            logger.info(f"[RelayInstance:{self.phone}] 已向 @{bot_username} 发送外部码 (user={user_id}, code={code})")
            return True
        except FloodWaitError as e:
            self.record_flood_wait(e.seconds)
            logger.warning(
                f"[RelayInstance:{self.phone}] 向 @{bot_username} 发送触发 FloodWait: {e.seconds}s"
            )
            return False
        except Exception as e:
            if _is_ban_error(e):
                self.record_ban(str(e))
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
        """缓冲单个文件消息引用,不立即发送。
        所有文件积累后由 _flush_download_buffer 统一批量作为媒体组发送(保持相册格式)。
        """
        if not getattr(msg, "media", None):
            await self._decrement_cache_counter(code)
            return
        # R30-6: 排除 webpage 预览，避免无意义的 send_file 失败尝试
        if isinstance(msg.media, MessageMediaWebPage):
            await self._decrement_cache_counter(code)
            return
        orig_caption = (getattr(msg, "message", "") or "").strip()
        # P3-2: 仅缓冲消息引用,不立即发送,避免逐个发送破坏媒体组格式
        # _flush_download_buffer 会先尝试批量引用发送,失败再下载到临时文件批量发送
        buf = self._pending_msgs.setdefault(code, [])
        buf.append({"msg": msg, "orig_caption": orig_caption, "user_id": user_id})
        logger.debug(f"[RelayInstance:{self.phone}] 已缓冲消息引用 (code={code}, 共{len(buf)}个)")
        await self._decrement_cache_counter(code)

    async def _buffer_download(self, msg, code: str, orig_caption: str):
        """P3: noforwards 回退 — 下载媒体到临时文件缓冲,稍后批量作为媒体组发送(保持格式)
        用临时文件而非内存 bytes,避免大文件(2GB+)导致内存耗尽。
        """
        import os
        import tempfile
        try:
            # 提取原始文件名扩展名,以便 Telethon 正确推断文件类型
            ext = ""
            if hasattr(msg, "document") and msg.document:
                file_name = getattr(msg.document, "file_name", None) or ""
                if "." in file_name:
                    ext = "." + file_name.rsplit(".", 1)[-1].lower()
            elif hasattr(msg, "photo") and msg.photo:
                ext = ".jpg"
            # 创建临时文件(不自动删除,发送后手动清理)
            fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix=f"relay_{code[:8]}_")
            os.close(fd)
            downloaded = await self._client.download_media(msg, file=tmp_path)
            if downloaded is None:
                logger.warning(f"[RelayInstance:{self.phone}] download_media 返回 None (code={code})")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return
            buf = self._download_buffers.setdefault(code, [])
            buf.append({
                "path": tmp_path,
                "orig_caption": orig_caption,
            })
            logger.info(f"[RelayInstance:{self.phone}] 下载并缓冲到临时文件 (code={code}, 已缓冲{len(buf)}个, path={tmp_path})")
        except Exception as e2:
            if _is_ban_error(e2):
                self.record_ban(str(e2))
            logger.error(f"[RelayInstance:{self.phone}] 下载缓冲失败 (code={code}): {e2}")

    async def _flush_download_buffer(self, user_id: int, code: str):
        """统一批量发送所有缓冲文件给 Up Bot(保持媒体组格式)。
        1. 优先用 _pending_msgs 中的 InputMedia 引用批量发送(快,无需下载)
        2. 引用失败(noforwards 受保护聊天)则下载到临时文件,再批量发送
        3. _download_buffers 中已有的临时文件(之前 _flush_media_group_buffer 回退产生的)一并合并
        发送后清理临时文件。
        """
        pending = self._pending_msgs.pop(code, [])
        downloads = self._download_buffers.pop(code, [])
        if not pending and not downloads:
            return
        if not self._up_bot_entity:
            logger.warning(f"[RelayInstance:{self.phone}] Up Bot 不可用,丢弃缓冲 (code={code}, pending={len(pending)}, downloads={len(downloads)})")
            self._cleanup_tmp_files(downloads)
            return
        _code_hex = code.encode('utf-8').hex()
        # 找到第一个非空 orig_caption(pending 和 downloads 都检查)
        orig_caption = ""
        for item in pending:
            if item.get("orig_caption"):
                orig_caption = item["orig_caption"]
                break
        if not orig_caption:
            for item in downloads:
                if item.get("orig_caption"):
                    orig_caption = item["orig_caption"]
                    break
        first_caption = f"EXTERNAL_RELAY:{user_id}:H:{_code_hex}\n{orig_caption}" if orig_caption else f"EXTERNAL_RELAY:{user_id}:H:{_code_hex}"

        # 1. 优先用引用批量发送(pending)
        if pending:
            media_list = [item["msg"].media for item in pending
                         if item["msg"].media and not isinstance(item["msg"].media, MessageMediaWebPage)]
            if media_list:
                sent = await self._send_batch_as_album(media_list, first_caption, code, "引用")
                if sent:
                    # 引用发送成功,清理 downloads(如果有)并返回
                    self._cleanup_tmp_files(downloads)
                    return
                # 引用发送失败(可能是 protected chat),下载所有 pending 到临时文件
                logger.warning(f"[RelayInstance:{self.phone}] 引用批量发送失败,下载到临时文件 (code={code})")
                for item in pending:
                    msg = item["msg"]
                    if msg.media and not isinstance(msg.media, MessageMediaWebPage):
                        await self._buffer_download(msg, code, item["orig_caption"])
                downloads = self._download_buffers.pop(code, []) + downloads

        # 2. 用临时文件批量发送(downloads)
        if downloads:
            path_list = [item["path"] for item in downloads]
            await self._send_batch_as_album(path_list, first_caption, code, "临时文件")
            self._cleanup_tmp_files(downloads)

    async def _send_batch_as_album(self, media_list: list, first_caption: str, code: str, mode: str) -> bool:
        """将 media_list 分批作为媒体组相册发送给 Up Bot。
        media_list 元素可以是 InputMedia 引用或文件路径字符串。
        返回 True 表示全部发送成功,False 表示失败(调用方回退)。
        """
        BATCH = 10  # Telegram 媒体组上限
        all_success = True
        for i in range(0, len(media_list), BATCH):
            batch = media_list[i:i + BATCH]
            try:
                if len(batch) == 1:
                    await self._client.send_file(
                        self._up_bot_entity, batch[0],
                        caption=first_caption, force_document=False,
                    )
                else:
                    # Telethon send_file 传入列表时自动以相册(sendMultiMedia)发送
                    captions = [first_caption] + [None] * (len(batch) - 1)
                    await self._client.send_file(
                        self._up_bot_entity, batch,
                        caption=captions, force_document=False,
                    )
                logger.info(f"[RelayInstance:{self.phone}] {mode}批量发送 ({len(batch)}个, code={code})")
            except FloodWaitError as e:
                self.record_flood_wait(e.seconds)
                logger.warning(f"[RelayInstance:{self.phone}] {mode}发送 FloodWait (code={code}): {e.seconds}s")
                all_success = False
            except Exception as e:
                if _is_ban_error(e):
                    self.record_ban(str(e))
                if len(batch) > 1:
                    # 批量失败回退到逐个发送(保持 caption 在第一条)
                    logger.warning(f"[RelayInstance:{self.phone}] {mode}批量发送失败,回退逐个 (code={code}): {e}")
                    for j, m in enumerate(batch):
                        cap = first_caption if j == 0 else None
                        try:
                            await self._client.send_file(
                                self._up_bot_entity, m,
                                caption=cap, force_document=False,
                            )
                        except Exception as e2:
                            if _is_ban_error(e2):
                                self.record_ban(str(e2))
                            logger.error(f"[RelayInstance:{self.phone}] 逐个发送也失败 (code={code}): {e2}")
                            all_success = False
                else:
                    # 单个文件失败,返回 False 让调用方回退(引用失败→下载)
                    logger.warning(f"[RelayInstance:{self.phone}] {mode}单文件发送失败 (code={code}): {e}")
                    all_success = False
        return all_success

    @staticmethod
    def _cleanup_tmp_files(buf: list):
        """清理临时文件缓冲中的磁盘文件"""
        import os
        for item in buf:
            path = item.get("path")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _detect_media_type(msg) -> str:
        # P1-16:引用 file_utils.MEDIA_TYPE 规范词表,确保 voice 与 file_utils 一致(返回 "voice" 而非 "audio")
        from utils.file_utils import MEDIA_TYPE
        if hasattr(msg, "photo") and msg.photo:
            return MEDIA_TYPE["PHOTO"]
        if hasattr(msg, "video") and msg.video:
            return MEDIA_TYPE["VIDEO"]
        if hasattr(msg, "audio") and msg.audio:
            return MEDIA_TYPE["AUDIO"]
        if hasattr(msg, "voice") and msg.voice:
            return MEDIA_TYPE["VOICE"]
        if hasattr(msg, "animation") and msg.animation:
            return MEDIA_TYPE["ANIMATION"]
        if hasattr(msg, "gif") and msg.gif:
            return MEDIA_TYPE["ANIMATION"]
        if hasattr(msg, "sticker") and msg.sticker:
            return MEDIA_TYPE["STICKER"]
        if hasattr(msg, "document") and msg.document:
            mime = getattr(msg.document, "mime_type", "") or ""
            if "video" in mime:
                return MEDIA_TYPE["VIDEO"]
            if "audio" in mime:
                return MEDIA_TYPE["AUDIO"]
            return MEDIA_TYPE["DOCUMENT"]
        return MEDIA_TYPE["DOCUMENT"]

    @staticmethod
    def _extract_number(text: str) -> int | None:
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            return int(digits)
        return None

    _RATE_LIMIT_PATTERNS = [
        (re.compile(r"(?:请|等待?|需\s*要?)\s*(\d+)\s*秒"), 1),
        (re.compile(r"(\d+)\s*秒\s*(?:后|再|之?后)"), 1),
        (re.compile(r"wait\s+(\d+)\s*sec(?:ond)?s?", re.IGNORECASE), 1),
        (re.compile(r"try\s+again\s+(?:in|after)\s+(\d+)\s*sec(?:ond)?s?", re.IGNORECASE), 1),
        (re.compile(r"(\d+)\s*sec(?:ond)?s?\s*(?:later|after)", re.IGNORECASE), 1),
        (re.compile(r"(?:频率|操作)\s*(?:过快|频繁|过于频繁)"), None),
        (re.compile(r"too\s+(?:fast|frequent|many\s+requests)", re.IGNORECASE), None),
        (re.compile(r"(?:请稍[候后]|稍[候后]再试|请勿频繁)"), None),
        (re.compile(r"flood\s*wait", re.IGNORECASE), None),
    ]

    _DEFAULT_RATE_LIMIT_WAIT = 5

    def _check_rate_limit(self, exchange: dict) -> float:
        import re
        text_responses = exchange.get("text_responses", [])
        if not text_responses:
            return 0
        recent = text_responses[-5:]
        for entry in recent:
            text = entry.get("text", "")
            if not text:
                continue
            for pattern, group_idx in self._RATE_LIMIT_PATTERNS:
                m = pattern.search(text)
                if m:
                    if group_idx is not None:
                        try:
                            seconds = int(m.group(group_idx))
                        except (IndexError, ValueError):
                            seconds = self._DEFAULT_RATE_LIMIT_WAIT
                    else:
                        seconds = self._DEFAULT_RATE_LIMIT_WAIT
                    wait_time = min(max(seconds, 1), 60)
                    exchange["_min_click_interval"] = max(exchange.get("_min_click_interval", 0), wait_time)
                    return wait_time
        return 0

    def _register_handlers(self):
        if self._handlers_registered:
            return
        self._handlers_registered = True
        @self._client.on(events.NewMessage(incoming=True))
        async def on_new_message(event):
            now_ts = asyncio.get_running_loop().time()
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

            # 检测 idx_bot 确认消息：解码器 Bot 转发"外部文件已就绪"
            decoder_un = settings.DECODER_BOT_USERNAME.lower().lstrip("@")
            if bot_username == decoder_un and self._ready_events:
                text = getattr(event.message, "message", None) or ""
                for code, ev in list(self._ready_events.items()):
                    # 精确匹配：code 作为独立词出现（前后为空格/冒号/行首尾）
                    if re.search(rf"(?:^|\s|:|：){re.escape(code)}(?:\s|$|，|。)", text) and ("已就绪" in text or "ready" in text.lower()):
                        if not ev.is_set():
                            ev.set()
                            logger.info(f"[RelayInstance:{self.phone}] idx_bot 已确认外部文件就绪: code={code}")
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
                    buf["_expires"] = now_ts + 2
                    return
                self._media_buffers[media_group_id] = {
                    "events": [event],
                    "bot_username": bot_username,
                    "_expires": now_ts + 2,
                }
                self._spawn(
                    self._flush_media_group_buffer(media_group_id, bot_username)
                )
                return

            msg = event.message
            if msg.media and not isinstance(msg.media, MessageMediaWebPage):
                exchange.setdefault("events", []).append(event)
            else:
                text = getattr(msg, "message", None) or ""
                if text:
                    exchange.setdefault("text_responses", []).append({"msg_id": msg.id, "text": text})
                # 带键盘的文本消息也需对决策函数可见（翻页按钮、错误文本检测）
                if msg.reply_markup:
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
        exchange["_settle_task"] = self._spawn(
            self._message_loop(bot_username, settle_wait)
        )

    async def _flush_media_group_buffer(self, media_group_id: str, bot_username: str):
        await asyncio.sleep(1.5)
        now_ts = asyncio.get_running_loop().time()
        while True:
            buf = self._media_buffers.get(media_group_id)
            if not buf:
                return
            if buf["_expires"] > now_ts:
                await asyncio.sleep(0.3)
                now_ts = asyncio.get_running_loop().time()
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
        # P1: 筛选有效媒体,保持媒体组格式批量发送
        media_list = []
        valid_events = []
        # 收集第三方 Bot 原始 caption（通常只有组内一条消息带文本）
        orig_caption = ""
        for ev in events_list:
            msg = ev.message
            if not getattr(msg, "media", None):
                continue
            if isinstance(msg.media, MessageMediaWebPage):
                continue
            media_list.append(msg.media)
            valid_events.append(ev)
            msg_text = (getattr(msg, "message", "") or "").strip()
            if msg_text and not orig_caption:
                orig_caption = msg_text
        if not media_list:
            return
        # 第一条消息承载路由前缀 + 原始 caption，其余消息无 caption（保持相册单 caption 展示）
        # code 用 hex 编码，避免 emoji 在 Telethon→Bot API 传输中变成 NULL 字符
        _code_hex = code.encode('utf-8').hex()
        first_caption = f"EXTERNAL_RELAY:{user_id}:H:{_code_hex}\n{orig_caption}" if orig_caption else f"EXTERNAL_RELAY:{user_id}:H:{_code_hex}"
        # 递增 pending 计数器
        for _ in valid_events:
            if code:
                self._pending_cache_counts[code] = self._pending_cache_counts.get(code, 0) + 1
        try:
            if self._up_bot_entity:
                if len(media_list) == 1:
                    await self._client.send_file(
                        self._up_bot_entity, media_list[0], caption=first_caption
                    )
                else:
                    # Telethon send_file 传入列表时自动以相册(sendMultiMedia)发送，无需 grouping 参数
                    # 仅第一条消息带 caption，其余不带（保持相册单 caption 展示）
                    captions = [first_caption] + [None] * (len(media_list) - 1)
                    await self._client.send_file(
                        self._up_bot_entity, media_list, caption=captions
                    )
                    logger.info(f"[RelayInstance:{self.phone}] 媒体组批量发送 ({len(media_list)}个文件, code={code}, orig_caption={'有' if orig_caption else '无'})")
            else:
                logger.warning(f"[RelayInstance:{self.phone}] Up Bot 不可用，跳过媒体组 (code={code})")
        except FloodWaitError as e:
            self.record_flood_wait(e.seconds)
            logger.warning(f"[RelayInstance:{self.phone}] 媒体组发送 FloodWait (code={code}): {e.seconds}s")
            for _ in valid_events:
                await self._decrement_cache_counter(code)
        except Exception as e:
            if _is_ban_error(e):
                self.record_ban(str(e))
                for _ in valid_events:
                    await self._decrement_cache_counter(code)
            else:
                # P3: 批量失败回退到逐个 send_file + _buffer_download(保持媒体组格式)
                logger.warning(f"[RelayInstance:{self.phone}] 媒体组批量发送失败,回退逐个发送 (code={code}): {e}")
                for ev in valid_events:
                    await self._download_and_cache_one(ev.message, user_id, code)
        else:
            # 成功时递减计数器(失败路径由 _download_and_cache_one 的 finally 处理)
            for _ in valid_events:
                await self._decrement_cache_counter(code)
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
        _MAX_PAGES = 30  # 安全上限：防止翻页死循环
        try:
            while True:
                if bot_username not in self._bot_exchange:
                    break
                exchange = self._bot_exchange[bot_username]
                exchange["_expires"] = asyncio.get_running_loop().time() + 120
                # 翻页次数安全上限
                if exchange.get("_page_count", 0) >= _MAX_PAGES:
                    logger.warning(f"[RelayInstance:{self.phone}] 翻页达到安全上限 {_MAX_PAGES} 次，终止 (bot=@{bot_username})")
                    await self._process_all_collected(bot_username)
                    break
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
                        except Exception as notify_err:
                            logger.warning(
                                f"[Relay] RELAY_ERROR 通知失败 user={user_id} code={code} "
                                f"reason={reason}: {notify_err}"
                            )
                    await self._cleanup_exchange(bot_username)
                    break
                elif action == "wait":
                    # B1: 指数退避 — 用 _record_rate_limit 递增等待时间
                    suggested = decision.get("wait_seconds", 5)
                    wait_sec = self._record_rate_limit(bot_username, int(suggested))
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        exchange["_last_click_time"] = asyncio.get_running_loop().time() + wait_sec
                    # 记录冷却到本地 SQLite(跨请求级别)
                    from database.relay_db import get_relay_db
                    relay_db = await get_relay_db()
                    await relay_db.set_bot_cooldown(bot_username, int(wait_sec))
                    # B1: 检查是否触发熔断
                    broken, break_remaining = self._is_circuit_broken(bot_username)
                    if broken:
                        logger.warning(
                            f"[RelayInstance:{self.phone}] @{bot_username} 触发熔断,"
                            f"终止解码 (剩余{break_remaining:.0f}s)"
                        )
                        # 通知 decoder_bot 解码失败
                        exchange_data = self._bot_exchange.get(bot_username)
                        if exchange_data:
                            user_id = exchange_data.get("user_id", 0)
                            code = exchange_data.get("code", "")
                            try:
                                await self._client.send_message(
                                    self._decoder_bot_entity,
                                    f"RELAY_ERROR:{user_id}:{code}:目标Bot连续限速熔断{break_remaining:.0f}s",
                                )
                            except Exception:
                                pass
                        await self._cleanup_exchange(bot_username)
                        break
                    await asyncio.sleep(wait_sec)
                    continue
                elif action == "click_button":
                    exchange = self._bot_exchange.get(bot_username)
                    if exchange:
                        min_interval = exchange.get("_min_click_interval", 0)
                        last_click = exchange.get("_last_click_time", 0)
                        now = asyncio.get_running_loop().time()
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
                        exchange["_last_click_time"] = asyncio.get_running_loop().time()
                        exchange["_page_count"] = exchange.get("_page_count", 0) + 1
                        logger.info(f"[RelayInstance:{self.phone}] 翻页: 第{exchange['_page_count']}次 (bot=@{bot_username})")
                    await asyncio.sleep(2)
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
            # N-15-6: 统一复位 is_busy，防止 CancelledError 等边缘路径导致永久 busy
            self.is_busy = False
            if bot_username in self._bot_exchange:
                self._bot_exchange[bot_username]["_ai_running"] = False

    def _make_decision(self, exchange: dict) -> dict:
        _NEXT_KW = ("next", "下一页", "下一頁", "下一组",
                     "→", "▶", "➡", ">>", "»")
        # "继续发送"类按钮:可重复点击,每次加载更多内容(不受 _clicked_buttons 跳过限制)
        # 靠 stale_clicks >= 3 机制防止死循环(连续3次无新文件则终止)
        _CONTINUE_KW = ("继续发送", "继续", "更多", "加载更多", "继续接收",
                        "continue", "more", "load more", "show more", "send more")
        _PREV_KW = ("prev", "上一页", "上一頁", "上一组", "上一組", "←", "◀", "⬅", "<<", "«", "back", "返回")
        _ERROR_KW = ("未找到", "已过期", "已失效",
                     "不存在", "not found", "expired", "invalid")
        _FINISH_KW = {"finish", "done", "完成", "结束"}

        msg_events = exchange.get("events", [])
        if not msg_events:
            return {"action": "finish", "target_button_row": None, "target_button_col": None,
                    "target_button_text": None, "reason": "无消息", "wait_seconds": None}

        for ev in msg_events:
            text = (getattr(ev.message, "message", None) or "").lower()
            for ek in _ERROR_KW:
                if ek in text:
                    return {"action": "error", "target_button_row": None, "target_button_col": None,
                            "target_button_text": None, "reason": f"检测到错误: {ek}",
                            "wait_seconds": None}

        wait_sec = self._check_rate_limit(exchange)
        if wait_sec > 0:
            return {"action": "wait", "target_button_row": None, "target_button_col": None,
                    "target_button_text": None,
                    "reason": "检测到限速", "wait_seconds": wait_sec}

        clicked = exchange.get("_clicked_buttons") or set()
        for ev in msg_events:
            msg = ev.message
            if not (msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows):
                continue
            rows = msg.reply_markup.rows
            # 0. 优先查找"继续发送"类按钮(可重复点击,每次加载更多内容,不跳过已点击位置)
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    btn_text = (getattr(btn, "text", None) or "").lower().strip()
                    if any(kw in btn_text for kw in _CONTINUE_KW):
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": f"检测到继续发送按钮: {btn_text}",
                                "wait_seconds": None}
            # 1. 查找"下一页"按钮(可重复点击,每次加载新页面,不跳过已点击位置)
            # 防死循环靠 stale_clicks >= 3 机制(连续3次翻页无新文件则终止)
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    btn_text = (getattr(btn, "text", None) or "").lower().strip()
                    if any(kw in btn_text for kw in _NEXT_KW):
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": f"检测到翻页按钮: {btn_text}",
                                "wait_seconds": None}
            # 2. 数字翻页（需要单行至少3个数字按钮）
            for row_idx, row in enumerate(rows):
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                numbers = []
                for col_idx, t in enumerate(btn_texts):
                    n = self._extract_number(t)
                    if n is not None:
                        numbers.append((col_idx, t, n))
                if len(numbers) >= 3:
                    all_nums = sorted([n for _, _, n in numbers])
                    last = exchange.get("_last_clicked_number")
                    if last is None:
                        target = 2 if 2 in all_nums else all_nums[1] if len(all_nums) > 1 else all_nums[0]
                        exchange["_last_button_range"] = tuple(all_nums)
                    else:
                        target = last + 1
                        if target > all_nums[-1]:
                            current_range = tuple(all_nums)
                            prev_range = exchange.get("_last_button_range")
                            if current_range != prev_range:
                                exchange["_last_button_range"] = current_range
                                target = 2 if 2 in all_nums else all_nums[1] if len(all_nums) > 1 else all_nums[0]
                            else:
                                break
                    for col_idx, t, n in numbers:
                        if n == target:
                            exchange["_last_clicked_number"] = target
                            return {"action": "click_button", "target_button_row": row_idx,
                                    "target_button_col": col_idx, "target_button_text": t,
                                    "reason": f"数字翻页第{target}页",
                                    "wait_seconds": None}
                    break
            # 3. 纯图标按钮（最后一行全空文本）
            for row_idx in range(len(rows) - 1, -1, -1):
                row = rows[row_idx]
                if not row.buttons:
                    continue
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                if all(not t for t in btn_texts):
                    last_btn = row.buttons[-1]
                    if getattr(last_btn, "data", None):
                        if (row_idx, len(row.buttons) - 1) in clicked:
                            continue
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": len(row.buttons) - 1, "target_button_text": "",
                                "reason": "纯图标", "wait_seconds": None}
            # 4. 兜底：点击未点击过的按钮（排除"上一页"和"完成"）
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    if getattr(btn, "data", None):
                        btn_text = (getattr(btn, "text", None) or "").strip().lower()
                        if any(kw in btn_text for kw in _FINISH_KW):
                            continue
                        # 排除"上一页"类按钮，防止翻回前一页造成死循环
                        if any(kw in btn_text for kw in _PREV_KW):
                            continue
                        if (row_idx, col_idx) in clicked:
                            continue
                        return {"action": "click_button", "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": f"尝试点击: {btn_text}",
                                "wait_seconds": None}
        return {"action": "finish", "target_button_row": None, "target_button_col": None,
                "target_button_text": None, "reason": "无翻页按钮",
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
        try:
            if hasattr(target_btn, "data") and target_btn.data:
                await keyboard_msg.click(data=target_btn.data)
                return True
            if hasattr(target_btn, "url") and target_btn.url:
                url = str(target_btn.url)
                if "t.me/" in url or "telegram.me/" in url:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(url)
                    target_user = parsed.path.strip("/").lower()
                    # 仅允许向已知可信 Bot 自动发送 /start（解码器 Bot、上传 Bot、当前交互 Bot）
                    allowed = {settings.DECODER_BOT_USERNAME.lower().lstrip("@"),
                               settings.UPLOAD_BOT_USERNAME.lower().lstrip("@"),
                               bot_username.lower().lstrip("@")}
                    if target_user not in allowed:
                        logger.warning(f"[RelayInstance:{self.phone}] 拒绝向不可信实体自动 /start: {target_user}")
                        return False
                    params = parse_qs(parsed.query)
                    start_param = params.get("start", [None])[0]
                    if start_param:
                        entity = await self._client.get_entity(parsed.path.strip("/"))
                        await self._client.send_message(entity, f"/start {start_param}")
                        return True
                return False
            return False
        except FloodWaitError as e:
            self.record_flood_wait(e.seconds)
            exchange = self._bot_exchange.get(bot_username)
            if exchange:
                exchange["_min_click_interval"] = max(
                    exchange.get("_min_click_interval", 0), e.seconds
                )
            # B1: FloodWait 也记录限速事件(指数退避)
            self._record_rate_limit(bot_username, e.seconds)
            await asyncio.sleep(e.seconds)
            try:
                if hasattr(target_btn, "data") and target_btn.data:
                    await keyboard_msg.click(data=target_btn.data)
                    return True
            except Exception as click_err:
                logger.debug(f"[Relay] FloodWait 后按钮点击失败: {click_err}")
            return False
        except Exception:
            return False

    async def _process_all_collected(self, bot_username: str):
        # 关键:必须先等待所有文件缓存完成,再 pop exchange
        # 否则 _wait_all_cached 内部 get(bot_username) 会返回 None,等待逻辑失效,导致文件丢失
        try:
            await self._wait_all_cached(bot_username, timeout=60)
        except Exception as e:
            logger.warning(f"[RelayInstance:{self.phone}] _wait_all_cached 异常: {e}")
        exchange = self._bot_exchange.pop(bot_username, None)
        self.is_busy = False
        if not exchange:
            return
        user_id = exchange.get("user_id")
        code = exchange.get("code")
        # P3: flush 下载缓冲(noforwards 回退的文件)作为媒体组发送给 Up Bot
        # 必须在 _cleanup_code_dicts 之前执行(后者会清理 _download_buffers)
        try:
            await self._flush_download_buffer(user_id, code)
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] flush 下载缓冲异常 (code={code}): {e}")
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
                except Exception as notify_err:
                    logger.warning(
                        f"[Relay] RELAY_ERROR 通知失败(目标机器人无文件) "
                        f"user={user_id} code={code}: {notify_err}"
                    )
            if self._pending_cleanup:
                self._pending_cleanup(bot_username)
            return
        # 所有文件已发送到 Up Bot，发送 EXTERNAL_DONE 信号触发批量写入
        try:
            if self._up_bot_entity:
                # P4: 不再等待 idx_bot 确认(原 120s 超时是最大延迟瓶颈)
                # up_bot 收到 EXTERNAL_DONE 后会 flush 并通知 idx_bot 处理
                # idx_bot 处理完直接调度 dsp 发送给用户(P2),无需中继等待
                await self._client.send_message(
                    self._up_bot_entity,
                    f"EXTERNAL_DONE:{user_id}:{code}",
                )
                logger.info(f"[RelayInstance:{self.phone}] 已通知 Up Bot 完成外部文件收集: code={code}")
                # 直接标记为已映射(下次该外部码可直接走本地缓存)
                await self._mark_code_mapped(code)
                # B1: 成功完成,重置该 bot 的退避计数
                self._reset_rate_limit(bot_username)
        except Exception as e:
            logger.error(f"[RelayInstance:{self.phone}] 通知 Up Bot 失败 (code={code}): {e}")
            await self._unmark_code(code)
        if self._pending_cleanup:
            self._pending_cleanup(bot_username)

    async def _cleanup_exchange(self, bot_username: str):
        exchange = self._bot_exchange.pop(bot_username, None)
        self.is_busy = False
        if exchange:
            code = exchange.get("code", "")
            await self._unmark_code(code)
            self._cleanup_code_dicts(code)
            if exchange.get("_settle_task") and not exchange["_settle_task"].done():
                exchange["_settle_task"].cancel()
        if self._pending_cleanup:
            self._pending_cleanup(bot_username)

    def _cleanup_code_dicts(self, code: str):
        """清理与指定 code 关联的所有缓存字典条目，防止内存泄漏和磁盘残留。"""
        if code:
            self._pending_cache_counts.pop(code, None)
            self._pending_cache_events.pop(code, None)
            self._cache_locks.pop(code, None)
            self._event_locks.pop(code, None)
            self._pending_msgs.pop(code, None)
            # 清理磁盘上的临时文件(异常路径,_flush_download_buffer 未来得及执行)
            buf = self._download_buffers.pop(code, None)
            if buf:
                self._cleanup_tmp_files(buf)

    async def _mark_code_mapped(self, code: str):
        """标记码已成功映射到本地缓存，避免重复查询 CRDB。"""
        if not code:
            return
        try:
            from database.relay_db import get_relay_db
            relay_db = await get_relay_db()
            if relay_db:
                await relay_db.mark_code_mapped(code)
        except Exception as e:
            logger.debug(f"[RelayInstance:{self.phone}] mark_code_mapped 失败: {e}")

    async def _unmark_code(self, code: str):
        """清除码的本地缓存标记（失败/超时时调用）。"""
        if not code:
            return
        try:
            from database.relay_db import get_relay_db
            relay_db = await get_relay_db()
            if relay_db:
                await relay_db.unmark_code(code)
        except Exception as e:
            logger.debug(f"[RelayInstance:{self.phone}] unmark_code 失败: {e}")

    async def deliver_cached(self, user_id: int, code: str) -> bool:
        if not self._client:
            return False
        try:
            from database import get_file_records_col, get_file_record_cached
            col = get_file_records_col()
            record = await get_file_record_cached(code)
            if not record:
                return False
            file_ids_str = record.get("file_ids", "") or ""
            if not isinstance(file_ids_str, str):
                file_ids_str = str(file_ids_str)
            file_ids = [f.strip() for f in file_ids_str.split(",") if f.strip()]
            if file_ids:
                if self._decoder_bot_entity:
                    # 跟踪每个文件发送结果,任一失败都通知 decoder_bot 重试
                    sent_ids: list[str] = []
                    failed_ids: list[str] = []
                    for fid in file_ids:
                        try:
                            await self._client.send_file(self._decoder_bot_entity, fid)
                            sent_ids.append(fid)
                        except Exception as e:
                            logger.warning(
                                f"[RelayInstance:{self.phone}] send_file 失败 (code={code}, fid={fid}): {e}"
                            )
                            failed_ids.append(fid)
                    if not sent_ids:
                        # 全部失败,不发送 RELAY_DELIVER,避免 decoder_bot 误以为文件已到
                        logger.error(
                            f"[RelayInstance:{self.phone}] deliver_cached 全部文件发送失败 (code={code}),"
                            f"不发送 RELAY_DELIVER,触发上层重试"
                        )
                        return False
                    await self._client.send_message(
                        self._decoder_bot_entity,
                        f"RELAY_DELIVER:{user_id}:{code}",
                    )
                    if failed_ids:
                        # 部分失败:记录日志,decoder_bot 可据此提示用户部分文件缺失
                        logger.warning(
                            f"[RelayInstance:{self.phone}] deliver_cached 部分文件发送失败 "
                            f"(code={code}, failed={len(failed_ids)}/{len(file_ids)})"
                        )
                return True
            # expired
            await col.delete_one({"file_code": code})
            # N-M13: 同时删除 SQLite 本地缓存和内存缓存，防止 RENEW 循环
            from database.cache_store import get_cache_store
            store = get_cache_store()
            await store.delete_file_record_local(code)
            from database.cache import get_file_record_cache
            get_file_record_cache().invalidate(f"file:{code}")
            if self._decoder_bot_entity:
                await self._client.send_message(
                    self._decoder_bot_entity,
                    f"RELAY_RENEW:{user_id}:{code}",
                )
            return False
        except FloodWaitError as e:
            self.record_flood_wait(e.seconds)
            logger.warning(
                f"[RelayInstance:{self.phone}] 缓存交付触发 FloodWait (code={code}): {e.seconds}s"
            )
            return False
        except Exception as e:
            if _is_ban_error(e):
                self.record_ban(str(e))
            logger.error(f"[RelayInstance:{self.phone}] 缓存交付失败 (code={code}): {e}")
            return False

    async def shutdown(self):
        # P1-8:取消所有内部后台任务,等待其完成,避免孤儿任务/资源泄漏。
        if self._shutdown_done:
            return
        self._shutdown_done = True
        pending = [t for t in self._background_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception as e:
                logger.debug(f"[RelayInstance:{self.phone}] 等待后台任务结束异常: {e}")
        self._background_tasks.clear()
        # 清理残留的临时文件和缓冲(防止服务重启时磁盘泄漏)
        self._pending_msgs.clear()
        for code, buf in list(self._download_buffers.items()):
            self._cleanup_tmp_files(buf)
        self._download_buffers.clear()
        if self._client:
            await self._client.disconnect()
