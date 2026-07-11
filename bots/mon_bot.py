"""Mon 监控机器人 v2
职责:
  1. 频道健康监控 + 心跳检测
  2. 封禁检测 → 通知管理员 Bot → 自动从备用池补充
  3. 活跃频道轮转(按文件数/时间切换)
  4. 文件同步写入 Shadow 频道
  5. 自动降级调度
与 admin_bot 完全分离,admin 负责管理配置,mon 负责运行时监控和文件冗余。
"""

import asyncio
import datetime as _dt
import re
import time

from loguru import logger
from telegram import Bot
from telegram.error import TelegramError, RetryAfter

from config import settings
from database import (
    init_db, close_db, get_cells_col,
    log_rotate,
    get_spare_for_account, get_any_spare,
    consume_spare, get_rotation_config,
)
from database.cache_store import get_cache_store
from services.mon import MonScheduler
from utils.monitor import metrics

TOKEN = settings.MON_BOT_TOKEN
if not TOKEN:
    raise RuntimeError("MON_BOT_TOKEN 未配置,监控机器人必须有独立的 Bot Token")
RECOVERY_INTERVAL = getattr(settings, "MON_CHECK_INTERVAL", 60)

# ─── 封禁关键词(Telegram API 错误消息匹配) ───
BAN_KEYWORDS = [
    "chat not found", "channel not found",
    "CHAT_NOT_FOUND", "CHANNEL_PRIVATE",
    "bot was kicked", "kicked",
    "CHAT_FORBIDDEN", "chat is forbidden",
    "user is deactivated",
    "PEER_ID_INVALID",
]


def _is_ban_error(exc: Exception) -> bool:
    """判断异常是否为封禁/频道丢失错误。"""
    msg = str(exc).lower()
    return any(kw.lower() in msg for kw in BAN_KEYWORDS)


class MonBot:
    """Mon 监控机器人 v2(后台轮询 + 轮转 + 备用池补充)。"""

    def __init__(self, stop_event: asyncio.Event | None = None):
        self.bot = Bot(token=TOKEN)
        self.scheduler = MonScheduler()
        self._running = False
        self._cycle_count = 0
        # 通知管理员的 admin_bot 实例(延迟初始化)
        self._admin_bot = None
        self._admin_chat_id = settings.ADMIN_TELEGRAM_ID
        self._notify_cooldowns: dict[str, float] = {}
        # A1: 告警状态追踪(每种告警独立冷却 + 恢复通知)
        self._alert_states: dict[str, str] = {}  # alert_key -> "active"|"resolved"
        # P1-15:接入 run_all 的全局停止事件,确保 run_all 触发停止时 mon_bot 优雅退出,
        # 与其它 4 个 bot 行为一致。None 表示未注册(独立运行时仍可靠 self._running 退出)。
        self._stop_event: asyncio.Event | None = stop_event
        # 轮转状态
        self._rotation = {
            "active_window_size": 3,
            "files_per_slot": 500,
            "time_per_slot": 3600,
        }
        self._rotation_reload_countdown = 0
        # ─── 轮转通知频率控制: 每 3600 秒最多通知一次 ───
        self._last_rotation_notify_ts: float = 0
        # ─── 心跳本地缓存:写入 SQLite,零 CRDB RU ───
        # slot_id → True(上次心跳成功)/ False(上次心跳失败)
        self._cell_healthy: dict[str, bool] = {}
        # slot_id → 连续失败次数(用于降级判断)
        self._cell_fail_streak: dict[str, int] = {}
        # slot_id → 是否已标记为疑似(二次确认去抖,首轮标记次轮确认)
        self._cell_suspicious: dict[str, bool] = {}
        # ─── cells 全量缓存:一次查询,多轮复用 ───
        self._cells_cache: list[dict] | None = None
        self._cells_cache_ts: float = 0
        # C3: cells 变更监听任务(5s 轮询,替代 10 周期强制清空)
        self._cells_change_task: asyncio.Task | None = None
        self._last_cells_version: int = 0
        # ─── FloodWait 跳过机制:避免被限速频道恶性循环 ───
        # slot_id → FloodWait 解除时间戳(time.time())
        # 处于 FloodWait 期的频道跳过心跳,直到解除时间到达
        self._cell_floodwait_until: dict[str, float] = {}
        # ─── shadow2 冷备心跳降频:每 N 轮检测一次,减少不必要的 API 调用 ───
        # shadow2 是冷备频道,不需要跟 active/shadow1 同频率检测
        self._SHADOW2_HEARTBEAT_EVERY_N_CYCLES = 5  # 5 轮 = 5 分钟(60s 间隔)

    async def _get_cells(self) -> list[dict]:
        """获取全量 cells，本地 SQLite 优先（零 CRDB RU），CRDB 仅首次兜底。

        热路径数据（status/next_active_chat_id/file_count/rotation_started_at/last_synced_msg_id）
        全部从本地 SQLite 读取。CRDB 仅在首次启动本地无数据时读取一次做 bootstrap。
        """
        now = time.time()
        if self._cycle_count % 10 == 0:
            self._cells_cache = None
        if self._cells_cache and (now - self._cells_cache_ts) < 60:
            return self._cells_cache

        store = get_cache_store()
        cells = await store.get_all_cells_local()
        if cells:
            self._cells_cache = cells
            self._cells_cache_ts = now
            return self._cells_cache

        logger.info("[Mon] 本地 cells 为空，从 CRDB 加载并写入本地（bootstrap）")
        col = get_cells_col()
        crdb_cells = await col.find({}, projection=[
            "slot_id", "channel_id", "status", "next_active_chat_id",
            "account_name", "is_r100", "last_heartbeat",
            "file_count", "rotation_started_at", "last_synced_msg_id",
            "degrade_count",
        ])
        if not crdb_cells:
            return []
        await store.bulk_upsert_cells_local(crdb_cells)
        self._cells_cache = await store.get_all_cells_local()
        self._cells_cache_ts = now
        logger.info(f"[Mon] Bootstrap 完成: {len(self._cells_cache)} 条 cells 写入本地")
        return self._cells_cache

    def _invalidate_cells_cache(self):
        """下次循环重新从本地 SQLite 加载 cells。"""
        self._cells_cache = None
        self._cells_cache_ts = 0

    def _update_cell_in_cache(self, slot_id: str, updates: dict):
        """在内存缓存中更新某个 cell 的字段（本地写入后立即生效，无需等下次加载）。"""
        if not self._cells_cache:
            return
        for c in self._cells_cache:
            if c["slot_id"] == slot_id:
                for k, v in updates.items():
                    c[k] = v
                break

    async def _watch_cells_change_loop(self):
        """C3: 每 5 秒检查 cells_change_notify,发现外部变更(admin_bot 增减槽位)
        立即失效内存缓存,将感知延迟从 ~10 分钟降到 ~5 秒。
        """
        from database.cache_store import get_cache_store
        store = get_cache_store()
        while self._running:
            try:
                changed, new_version = await store.has_cells_change(self._last_cells_version)
                if changed:
                    self._invalidate_cells_cache()
                    self._last_cells_version = new_version
                    logger.info(f"[Mon] cells 外部变更检测(version={new_version}),已失效缓存")
            except Exception as e:
                logger.debug(f"[Mon] cells 变更检测异常: {e}")
            await asyncio.sleep(5)

    async def _start_cells_change_watcher(self):
        """启动 cells 变更监听任务(幂等)。"""
        if self._cells_change_task is None or self._cells_change_task.done():
            self._cells_change_task = asyncio.create_task(self._watch_cells_change_loop())

    async def _stop_cells_change_watcher(self):
        """停止 cells 变更监听任务。"""
        if self._cells_change_task and not self._cells_change_task.done():
            self._cells_change_task.cancel()
            try:
                await self._cells_change_task
            except asyncio.CancelledError:
                pass
        self._cells_change_task = None

    async def _reload_rotation_config(self):
        """从本地 KV 读取轮转参数(30 个周期一次, 零 CRDB RU)。
        首次无本地值时从 CRDB 读取一次并写入本地 KV，后续全部走本地。
        """
        self._rotation_reload_countdown -= 1
        if self._rotation_reload_countdown > 0:
            return

        self._rotation_reload_countdown = 30
        store = get_cache_store()
        try:
            changed = False
            key_map = {
                "rotation_active_window_size": ("active_window_size", int, 3),
                "rotation_files_per_slot": ("files_per_slot", int, 500),
                "rotation_time_per_slot": ("time_per_slot", int, 3600),
            }
            for cfg_key, (local_key, cast_fn, default) in key_map.items():
                local_val = await store.get_kv(f"rotconf:{cfg_key}")
                if local_val is not None:
                    try:
                        val = cast_fn(local_val)
                    except (ValueError, TypeError):
                        val = default
                else:
                    crdb_val = await get_rotation_config(cfg_key)
                    if crdb_val and crdb_val.isdigit():
                        val = int(crdb_val)
                        await store.set_kv(f"rotconf:{cfg_key}", str(val))
                    else:
                        env_key = local_key.upper()
                        val = getattr(settings, f"ROTATION_{env_key}", default)
                        await store.set_kv(f"rotconf:{cfg_key}", str(val))
                old = self._rotation.get(local_key, 0)
                if old != val:
                    changed = True
                    logger.info(f"[Mon][Config] {local_key}: {old} → {val}")
                self._rotation[local_key] = val

            if changed:
                logger.info(f"[Mon][Config] 轮转参数已更新: {self._rotation}")
        except Exception as e:
            logger.warning(f"[Mon][Config] 读取轮转配置失败: {e}")

    async def _notify_admin(self, msg: str) -> bool:
        """通知管理员(通过 admin_bot 或直接向管理员聊天发消息)。
        按事件类型冷却：同一事件类型 10 分钟内不重复发送。
        返回 True 表示消息已发出，False 表示被冷却或失败。
        """
        if not self._admin_chat_id or self._admin_chat_id == 0:
            return False

        # 按事件类型冷却：取消息首行作为事件标识
        event_key = msg.split("\n")[0] if msg else msg
        if not hasattr(self, "_notify_cooldowns"):
            self._notify_cooldowns: dict[str, float] = {}
        now = time.time()
        last = self._notify_cooldowns.get(event_key, 0)
        if now - last < 600:
            return False

        try:
            if not self._admin_bot:
                admin_token = settings.ADMIN_BOT_TOKEN
                if admin_token:
                    try:
                        self._admin_bot = Bot(token=admin_token)
                        await self._admin_bot.initialize()
                    except Exception as init_err:
                        # 初始化失败时重置 _admin_bot,以便下次重试,避免永久锁死
                        self._admin_bot = None
                        logger.warning(f"[Mon][Notify] admin_bot 初始化失败: {init_err}")
                        return False
                else:
                    self._admin_bot = self.bot
            await self._admin_bot.send_message(
                chat_id=self._admin_chat_id,
                text=msg,
                disable_web_page_preview=True,
            )
            self._notify_cooldowns[event_key] = now  # 成功，设置冷却（10分钟内不再发送同类通知）
            return True
        except Exception as e:
            logger.warning(f"[Mon][Notify] 发送通知失败: {e}")
            return False  # 失败不设冷却，下次循环可重试

    async def _check_alerts(self):
        """A1: 端到端监控告警检查。

        从 counter_snapshot 聚合各进程计数器,计算全局指标,
        按 WARNING/CRITICAL 分级告警,支持恢复通知。
        relay 存活统计由 idx_bot 上报到 counter_snapshot(跨进程)。
        """
        try:
            store = get_cache_store()
            # 1. 从 counter_snapshot 读取跨进程聚合计数器
            aggregated = await store.load_counter_snapshot()
            send_success = aggregated.get("dsp.send_success", 0)
            send_fail = aggregated.get("dsp.send_fail", 0)
            total_deliveries = send_success + send_fail
            delivery_rate = (send_success / total_deliveries * 100) if total_deliveries > 0 else 100.0

            # 2. 队列积压深度(共享 SQLite, 0 RU)
            queue_backlog = await store.count_pending_jobs()

            # 3. 账号存活率(从 counter_snapshot 读取 idx_bot 上报的数据)
            alive = aggregated.get("relay.alive", 0)
            total = aggregated.get("relay.total", 0)
            survival_rate = (alive / total * 100) if total > 0 else 0.0

            # 4. Bot 离线检测
            stale_bots = metrics.get_stale_bots()

            # ── 告警规则 ──
            alerts_to_check = []

            # 投递成功率(仅在有投递数据时检查,避免冷启动误报)
            if total_deliveries >= 10:
                if delivery_rate < 80:
                    alerts_to_check.append(("delivery_rate", "CRITICAL",
                        f"投递成功率严重偏低: {delivery_rate:.1f}% ({send_success}/{total_deliveries})"))
                elif delivery_rate < 95:
                    alerts_to_check.append(("delivery_rate", "WARNING",
                        f"投递成功率偏低: {delivery_rate:.1f}% ({send_success}/{total_deliveries})"))

            # 队列积压
            if queue_backlog > 200:
                alerts_to_check.append(("queue_backlog", "CRITICAL",
                    f"队列积压严重: {queue_backlog} 个待处理任务"))
            elif queue_backlog > 50:
                alerts_to_check.append(("queue_backlog", "WARNING",
                    f"队列积压偏高: {queue_backlog} 个待处理任务"))

            # 账号存活率
            if total > 0:
                if survival_rate < 50:
                    alerts_to_check.append(("account_survival", "CRITICAL",
                        f"中继账号存活率严重偏低: {alive}/{total} ({survival_rate:.0f}%)"))
                elif survival_rate < 80:
                    alerts_to_check.append(("account_survival", "WARNING",
                        f"中继账号存活率偏低: {alive}/{total} ({survival_rate:.0f}%)"))

            # A2: 绝对数量告警(即使存活率高,但绝对数量低于安全水位也告警)
            safe_threshold = getattr(settings, "RELAY_SAFE_POOL_SIZE", 2)
            if total > 0 and alive < safe_threshold:
                alerts_to_check.append(("account_pool_low", "CRITICAL",
                    f"中继账号池即将耗尽: 仅剩 {alive}/{total} 个可用账号 (安全水位: {safe_threshold})"))

            # Bot 离线
            if stale_bots:
                alerts_to_check.append(("bot_stale", "CRITICAL",
                    f"以下 Bot 离线超过 5 分钟: {', '.join(stale_bots)}"))

            # ── 发送告警 + 恢复通知 ──
            # 状态追踪只用 severity(不含变化的数值),避免每轮都因数值变化触发重复通知
            active_keys = {a[0] for a in alerts_to_check}
            severity_emoji = {"WARNING": "⚠️", "CRITICAL": "🚨"}

            # 发送新告警(或严重级别变化时)
            for alert_key, severity, message in alerts_to_check:
                prev_severity = self._alert_states.get(alert_key)
                if prev_severity != severity:
                    emoji = severity_emoji.get(severity, "⚠️")
                    # 通知首行用 alert_key 作为稳定事件标识(利于 _notify_admin 冷却)
                    sent = await self._notify_admin(f"{emoji} [{severity}] {alert_key}\n{message}")
                    # 仅在通知成功发送时更新状态,避免冷却抑制导致永久丢失告警
                    if sent:
                        self._alert_states[alert_key] = severity

            # 发送恢复通知(之前有告警但现在已恢复)
            for alert_key in list(self._alert_states.keys()):
                if alert_key not in active_keys:
                    prev = self._alert_states.pop(alert_key, "")
                    if prev:
                        sent = await self._notify_admin(f"✅ [恢复] {alert_key} 已恢复正常")
                        # 通知发送失败时恢复状态,下轮会重试
                        if not sent:
                            self._alert_states[alert_key] = prev

        except Exception as e:
            logger.warning(f"[Mon][Alerts] 告警检查异常: {e}")

    async def _handle_channel_ban(self, cell: dict, error: str):
        """处理频道封禁/丢失:通知管理员 + 尝试从备用池补充。
        异常事件: CRDB 写入审计(log_rotate), 本地同步更新缓存。
        """
        slot_id = cell.get("slot_id", "?")
        channel_id = cell.get("channel_id", 0)
        account_name = cell.get("account_name", "未知")
        status = cell.get("status", "?")
        store = get_cache_store()

        notify_msg = (
            f"🚨 频道封禁告警\n\n"
            f"槽位: {slot_id}\n"
            f"频道 ID: {channel_id}\n"
            f"所属账号: {account_name}\n"
            f"状态: {status}\n"
            f"错误: {error}\n\n"
        )

        spare = None
        spare_source = ""

        if account_name and account_name not in ("?", "R100"):
            spare = await get_spare_for_account(account_name)
            spare_source = f"同账号备用池 ({account_name})"

        if not spare:
            spare = await get_any_spare()
            spare_source = "通用备用池"

        if spare:
            spare_ch = spare["channel_id"]
            try:
                await consume_spare(spare_ch)
            except Exception as e:
                logger.error(f"[Mon] consume_spare 失败 (channel={spare_ch}): {e}")
                # consume_spare 失败意味着该 spare 在 DB 中仍 is_used=0,
                # 继续替换会导致同一 spare 被双重分配。必须中止替换。
                notify_msg += f"⚠️ 备用池标记失败，已跳过替换: {e}"
                await self._notify_admin(notify_msg)
                return
            new_status = status if status in ("active", "shadow1", "shadow2", "r100") else "active"
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            await store.update_cell_fields_local(slot_id, {
                "channel_id": spare_ch,
                "status": new_status,
                "account_name": account_name,
                "file_count": 0,
                "last_synced_msg_id": 0,  # 归零触发 auto_fill_new_channels 补齐存量
                "rotation_started_at": now_iso,
            }, mark_dirty=True)
            self._update_cell_in_cache(slot_id, {
                "channel_id": spare_ch, "status": new_status,
                "file_count": 0, "last_synced_msg_id": 0,
                "rotation_started_at": now_iso,
            })
            # P2: 修正环形链表前驱指针:找到 next_active_chat_id 指向旧 channel_id 的槽位,
            # 更新为新的 spare_ch,避免 delivery_resolver 沿旧指针投到已封禁频道
            try:
                all_cells = await store.get_all_cells_local()
                for prev_cell in all_cells:
                    if prev_cell.get("next_active_chat_id") == channel_id:
                        prev_slot_id = prev_cell["slot_id"]
                        await store.update_cell_fields_local(prev_slot_id, {
                            "next_active_chat_id": spare_ch,
                        }, mark_dirty=True)
                        self._update_cell_in_cache(prev_slot_id, {
                            "next_active_chat_id": spare_ch,
                        })
                        logger.info(f"[Mon][Ban] 环指针修正: {prev_slot_id}.next_active_chat_id {channel_id} → {spare_ch}")
                        break
            except Exception as ring_err:
                logger.warning(f"[Mon][Ban] 环指针修正失败(非致命,下次轮转会重建): {ring_err}")
            await log_rotate(
                from_slot_id=slot_id, to_slot_id=slot_id,
                from_status=status, to_status=new_status,
                reason=f"封禁替换: old={channel_id} → new={spare_ch} ({spare_source})",
                triggered_by="mon",
            )
            notify_msg += (
                f"✅ 已自动从{spare_source}补充\n"
                f"新频道 ID: {spare_ch}\n"
                f"操作: 直接替换,无需降级"
            )
            logger.info(f"[Mon][Ban] {slot_id} 封禁 → 备用池 {spare_ch} 替换")
            # Manifest 驱动:新频道无 manifest 记录,auto_fill_new_channels 会自动从 Active 补齐
        else:
            notify_msg += (
                "⚠️ 备用池无可用频道!\n"
                "请通过管理员 Bot 添加备用频道:\n"
                "/spare_add <频道ID> [账号名]\n"
            )
            if status in ("active", "shadow1", "shadow2"):
                await store.update_cell_fields_local(slot_id, {"status": "lost", "next_active_chat_id": None}, mark_dirty=True)
                self._update_cell_in_cache(slot_id, {"status": "lost", "next_active_chat_id": None})
                await log_rotate(
                    from_slot_id=slot_id, to_slot_id="NONE",
                    from_status=status, to_status="lost",
                    reason=f"频道封禁且无备用: {error}",
                    triggered_by="mon",
                )

        await self._notify_admin(notify_msg)
        self._invalidate_cells_cache()

    async def _recover_lost(self, all_cells: list[dict]) -> int:
        """定期检查 lost 频道是否恢复可用，若可用则重新激活为 shadow2。

        lost 频道可能因临时网络波动被降级，或封禁后频道被解封。
        恢复的频道设为 shadow2（最低优先级），由后续轮转自然提升。
        返回恢复数量。
        """
        lost_cells = [c for c in all_cells if c.get("status") == "lost"]
        if not lost_cells:
            return 0

        store = get_cache_store()
        recovered = 0
        for cell in lost_cells:
            slot_id = cell["slot_id"]
            channel_id = cell["channel_id"]
            try:
                await self.bot.get_chat(channel_id)
                # 频道可访问，恢复为 shadow2
                await store.update_cell_fields_local(slot_id, {
                    "status": "shadow2",
                    "next_active_chat_id": None,
                    "degrade_count": 0,
                    "file_count": 0,
                    "demoted_to_channel_id": None,
                }, mark_dirty=True)
                self._update_cell_in_cache(slot_id, {
                    "status": "shadow2", "next_active_chat_id": None,
                    "degrade_count": 0, "file_count": 0,
                    "demoted_to_channel_id": None,
                })
                self._cell_healthy[slot_id] = True
                self._cell_fail_streak[slot_id] = 0
                self._cell_suspicious.pop(slot_id, None)  # 恢复后清除疑似标记,与心跳成功一致
                recovered += 1
                logger.info(f"[Mon] lost 频道恢复: {slot_id} (channel={channel_id}) → shadow2")
            except Exception as e:
                logger.warning(f"[Mon] lost 频道 {slot_id} 仍不可用: {e}")

        if recovered > 0:
            # N21-2: 移除冗余 _bump_cells_version()，update_cell_fields_local 已内部 bump
            await self._notify_admin(
                f"🔄 lost 频道恢复\n\n"
                f"共恢复 {recovered} 个频道为 shadow2\n"
                f"已重新加入环形拓扑，后续轮转将自然提升"
            )
        return recovered

    async def _check_rotation(self, all_cells: list[dict]):
        """检查活跃频道窗口是否该轮转。
        条件:任一活跃频道满足 file_count >= files_per_slot 或 rotation time >= time_per_slot
        轮转:将环形链表中最早的 window_size 个 active 槽位休眠(shadow1),
              唤醒对应组的下一个 shadow 槽位,修复环形链表指针。
        """
        active_cells = [c for c in all_cells if c.get("status") == "active"]
        if not active_cells:
            return False

        window_size = self._rotation.get("active_window_size", 3)
        files_per_slot = self._rotation.get("files_per_slot", 500)
        time_per_slot = self._rotation.get("time_per_slot", 3600)

        now = _dt.datetime.now(_dt.timezone.utc)
        should_rotate = False
        trigger_cell = None

        for cell in active_cells:
            fc = cell.get("file_count") or 0
            if fc >= files_per_slot:
                logger.info(f"[Mon][Rotation] {cell['slot_id']} file_count={fc} >= {files_per_slot},触发轮转")
                should_rotate = True
                trigger_cell = cell
                break
            rts = cell.get("rotation_started_at")
            if rts:
                try:
                    started = _dt.datetime.fromisoformat(rts)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= time_per_slot:
                        logger.info(f"[Mon][Rotation] {cell['slot_id']} 已运行 {elapsed:.0f}s >= {time_per_slot}s,触发轮转")
                        should_rotate = True
                        trigger_cell = cell
                        break
                except (ValueError, TypeError):
                    pass

        if not should_rotate:
            return False

        groups = self.scheduler._group_slots(all_cells)
        if not groups:
            logger.warning("[Mon][Rotation] 无有效分组,跳过轮转")
            return False

        channel_to_cell = {c["channel_id"]: c for c in active_cells}

        def _ring_order(start_cell: dict) -> list[dict]:
            ordered = []
            visited = set()
            current = start_cell
            while current and current["channel_id"] not in visited:
                visited.add(current["channel_id"])
                if current.get("status") == "active":
                    ordered.append(current)
                nxt_id = current.get("next_active_chat_id")
                current = channel_to_cell.get(nxt_id) if nxt_id else None
            for c in active_cells:
                if c["channel_id"] not in visited:
                    ordered.append(c)
            return ordered

        if trigger_cell:
            ring_ordered = _ring_order(trigger_cell)
        else:
            ring_ordered = _ring_order(active_cells[0])

        to_demote = ring_ordered[:window_size]
        if len(to_demote) < window_size:
            logger.warning(f"[Mon][Rotation] 活跃槽位不足 {window_size} 个,跳过轮转")
            return False

        now_iso = now.isoformat()
        demoted_slots = []
        promoted_slots = []
        cascade_slots = []
        ring_repairs = []
        batch_updates = []

        next_reverse: dict[int, list[dict]] = {}
        for c in active_cells:
            nxt = c.get("next_active_chat_id")
            if nxt:
                next_reverse.setdefault(nxt, []).append(c)

        demoted_slot_ids: set[str] = set()
        for cell in to_demote:
            sid = cell["slot_id"]
            m = re.match(r'[as](\d+)', sid)
            if not m:
                continue
            gnum = m.group(1)
            group = groups.get(gnum)
            if not group:
                continue
            demoted_slots.append(cell)
            demoted_slot_ids.add(sid)
            promote, cascade = self.scheduler._get_next_promotable(group)
            if promote:
                promoted_slots.append((promote, cell))
                if cascade and cascade.get("status") != "shadow1":
                    cascade_slots.append(cascade)
                preds = next_reverse.get(cell["channel_id"], [])
                for pred in preds:
                    if pred["slot_id"] != cell["slot_id"] and pred["slot_id"] not in demoted_slot_ids:
                        ring_repairs.append((pred["slot_id"], promote["channel_id"]))
            else:
                logger.warning(f"[Mon][Rotation] {sid} 无可用 shadow 提升,仅休眠不替换")

        if not promoted_slots:
            logger.warning("[Mon][Rotation] 无可提升的 shadow 槽位,跳过轮转")
            return False

        demoted_ch_to_promoted_ch: dict[int, int] = {}
        # PRE-01: 同时建立 slot_id → promoted_channel_id 映射，用于持久化到 cells_local.demoted_to_channel_id
        demoted_slot_to_promoted_ch: dict[str, int] = {}
        for promote_slot, from_cell in promoted_slots:
            demoted_ch_to_promoted_ch[from_cell["channel_id"]] = promote_slot["channel_id"]
            demoted_slot_to_promoted_ch[from_cell["slot_id"]] = promote_slot["channel_id"]

        for cell in demoted_slots:
            # PRE-01: 持久化降级映射，delivery_resolver 可立即跳转到接替频道
            promoted_ch = demoted_slot_to_promoted_ch.get(cell["slot_id"])
            if cell.get("status") != "shadow1":
                update_fields = {
                    "status": "shadow1", "file_count": 0, "next_active_chat_id": None,
                }
                if promoted_ch is not None:
                    update_fields["demoted_to_channel_id"] = promoted_ch
                batch_updates.append((cell["slot_id"], update_fields, True))
                self._update_cell_in_cache(cell["slot_id"], update_fields)
            elif promoted_ch is not None:
                # 已是 shadow1 但需更新降级映射（确保接替关系最新）
                update_fields = {
                    "file_count": 0, "next_active_chat_id": None,
                    "demoted_to_channel_id": promoted_ch,
                }
                batch_updates.append((cell["slot_id"], update_fields, True))
                self._update_cell_in_cache(cell["slot_id"], update_fields)

        for promote_slot, from_cell in promoted_slots:
            # 环指针继承:优先从同组 a 槽位(持有正确的环 next 指针)继承,
            # 而非从 from_cell(被降级的 active,其 next 通常为 NULL)继承。
            # a 槽位的 next 指向环中下一个 active 频道,这是正确的环顺序。
            sid = from_cell["slot_id"]
            m = re.match(r'[as](\d+)', sid)
            inherited_next = from_cell.get("next_active_chat_id")
            if m:
                gnum = m.group(1)
                group = groups.get(gnum)
                if group and group[0]:  # group[0] 是 a 槽位
                    a_next = group[0].get("next_active_chat_id")
                    if a_next:
                        inherited_next = a_next
            # 如果继承的 next 指向被降级的频道,替换为提升后的频道
            nxt = inherited_next
            if nxt and nxt in demoted_ch_to_promoted_ch:
                nxt = demoted_ch_to_promoted_ch[nxt]
            # PRE-01: 提升为 active 时清除 demoted_to_channel_id（它现在就是接替频道本身）
            batch_updates.append((promote_slot["slot_id"], {
                "status": "active", "next_active_chat_id": nxt,
                "file_count": 0, "rotation_started_at": now_iso,
                "demoted_to_channel_id": None,
            }, True))
            self._update_cell_in_cache(promote_slot["slot_id"], {
                "status": "active", "next_active_chat_id": nxt,
                "file_count": 0, "rotation_started_at": now_iso,
                "demoted_to_channel_id": None,
            })

        for cascade_slot in cascade_slots:
            updates = {
                "status": "shadow1", "file_count": 0, "degrade_count": 0,
                "next_active_chat_id": None, "demoted_to_channel_id": None,
            }
            batch_updates.append((cascade_slot["slot_id"], updates, True))
            self._update_cell_in_cache(cascade_slot["slot_id"], updates)

        for prev_slot_id, new_next in ring_repairs:
            if new_next and new_next in demoted_ch_to_promoted_ch:
                new_next = demoted_ch_to_promoted_ch[new_next]
            batch_updates.append((prev_slot_id, {"next_active_chat_id": new_next}, True))
            self._update_cell_in_cache(prev_slot_id, {"next_active_chat_id": new_next})

        store = get_cache_store()
        await store.batch_update_cells_local(batch_updates)

        # ── 轮转后重建环指针:确保所有 active 槽位形成闭环 ──
        # 轮转可能导致 next 指针指向非 active 频道(如 a 槽位是 shadow1),
        # 这里重新计算所有 active 槽位的环顺序,确保闭环正确。
        # 顺序规则:按组号排序,末尾指向首部形成环。
        updated_cells = await store.get_all_cells_local()
        active_after = [c for c in updated_cells if c.get("status") == "active"]
        if len(active_after) >= 2:
            # 按组号排序
            def _group_num(c):
                m = re.match(r'[as](\d+)', c.get("slot_id", ""))
                return int(m.group(1)) if m else 999
            active_after.sort(key=_group_num)

            ring_updates = []
            for i, c in enumerate(active_after):
                next_i = (i + 1) % len(active_after)
                next_ch = active_after[next_i]["channel_id"]
                cur_next = c.get("next_active_chat_id")
                if cur_next != next_ch:
                    ring_updates.append((c["slot_id"], next_ch))
                    self._update_cell_in_cache(c["slot_id"], {"next_active_chat_id": next_ch})

            if ring_updates:
                await store.batch_update_cells_local([
                    (sid, {"next_active_chat_id": ch}, True) for sid, ch in ring_updates
                ])
                logger.info(
                    f"[Mon][Rotation] 环指针重建: 修复 {len(ring_updates)} 个 active 槽位的 next 指针"
                )

        # ── 清理非 active 槽位的脏 next 指针 ──
        # shadow1/shadow2/lost 槽位不应持有指向非 active 频道的 next 指针,
        # 否则 validate_topology 会误报。仅保留 shadow2 指向同组 a 槽位的指针(设计如此)。
        active_channels_set = {c["channel_id"] for c in active_after} if len(active_after) >= 2 else set()
        clean_updates = []
        for c in updated_cells:
            if c.get("status") not in ("active", "r100"):
                nxt = c.get("next_active_chat_id")
                if nxt and nxt not in active_channels_set and c.get("status") != "shadow2":
                    clean_updates.append((c["slot_id"], None))
                    self._update_cell_in_cache(c["slot_id"], {"next_active_chat_id": None})
        if clean_updates:
            await store.batch_update_cells_local([
                (sid, {"next_active_chat_id": None}, True) for sid, _ in clean_updates
            ])
            logger.info(
                f"[Mon][Rotation] 清理 {len(clean_updates)} 个非 active 槽位的脏 next 指针"
            )

        for cell in demoted_slots:
            logger.info(f"[Mon][Rotation] 休眠 {cell['slot_id']}")
        for promote_slot, from_cell in promoted_slots:
            logger.info(f"[Mon][Rotation] 唤醒 {promote_slot['slot_id']}→active (替换 {from_cell['slot_id']})")

        logger.info(f"[Mon][Rotation] 轮转完成: 休眠{len(demoted_slots)}个, 唤醒{len(promoted_slots)}个 (零CRDB)")
        return True

    async def start(self):
        """启动 Mon 主循环。"""
        await init_db()
        from database.cache_store import report_bot_heartbeat
        await report_bot_heartbeat("mon_bot")
        await self.bot.initialize()
        self._running = True
        # C3: 启动 cells 变更监听(5s 轮询,加速外部变更感知)
        await self._start_cells_change_watcher()
        self._rotation_reload_countdown = 0
        # 从本地 SQLite 恢复心跳状态,避免重启后 fail_streak 从零开始
        store = get_cache_store()
        hb_data = await store.get_all_heartbeats()
        for slot_id, data in hb_data.items():
            # P2: 根据上次心跳结果恢复健康状态,而非一律置 True
            # 避免重启后丢失上次心跳的真实状态导致降级失效
            last_ok = data.get("last_ok", True)
            self._cell_healthy[slot_id] = last_ok
            self._cell_fail_streak[slot_id] = data.get("fail_streak", 0)
        if hb_data:
            logger.info(f"[Mon] 从 SQLite 恢复 {len(hb_data)} 条心跳记录")
        logger.info("[Mon] 监控机器人 v2 已启动")

        while self._running and not (self._stop_event and self._stop_event.is_set()):
            try:
                # 0. 心跳上报（跨进程共享）
                await report_bot_heartbeat("mon_bot")

                # 1. 重新加载轮转配置(每 30 周期一次)
                await self._reload_rotation_config()

                # ── 一次查询 cells 表(走缓存),所有方法复用 ──
                all_cells = await self._get_cells()

                # 1. 对所有 active + shadow 槽位发心跳(同时检测封禁)
                ok_count, ban_count = await self._heartbeat_with_ban_detection(all_cells)
                if ok_count > 0:
                    logger.debug(f"[Mon] 心跳: {ok_count} 正常, {ban_count} 封禁")
                if ban_count > 0:
                    self._invalidate_cells_cache()  # 封禁替换写了 cells,下次循环重载
                    all_cells = await self._get_cells()  # 重新加载以反映变更

                # 2. 核心写入:将 Active 槽位新文件同步到 Shadow 频道
                copied = await self.scheduler.replicate_all_active_to_shadows(self.bot, all_cells)
                if copied > 0:
                    logger.debug(f"[Mon] 文件同步: 复制了 {copied} 条消息到 Shadow 频道")

                # 3. 智能替补:新频道自动补齐存量文件
                filled = await self.scheduler.auto_fill_new_channels(self.bot, all_cells)
                if filled > 0:
                    logger.debug(f"[Mon] 智能替补: 补齐 {filled} 条消息到新频道")

                # 4. 降级检查(使用内存中的连续失败次数,零 CRDB RU)
                alerts, self._cell_suspicious = await self.scheduler.run_degrade_check(
                    all_cells, self._cell_fail_streak, self._cell_suspicious
                )
                if alerts:
                    for msg in alerts:
                        logger.warning(msg)
                        await metrics.increment("mon.degrade")
                        # 降级告警通知管理员
                        if "[DEGRADE]" in msg:
                            await self._notify_admin(f"⚠️ {msg}")
                    self._invalidate_cells_cache()  # 降级改了 cells status,失效缓存
                    all_cells = await self._get_cells()  # 重新加载以反映变更

                # 5. 活跃频道轮转检查
                rotated = await self._check_rotation(all_cells)
                if rotated:
                    self._invalidate_cells_cache()  # 轮转改了 cells status,失效缓存
                    all_cells = await self._get_cells()  # 重新加载以反映变更
                    # 轮转通知频率控制: 每 3600 秒最多通知一次
                    now = time.time()
                    if now - self._last_rotation_notify_ts > 3600:
                        if await self._notify_admin(
                            f"🔄 频道轮转完成\n\n"
                            f"当前窗口已推进，新窗口已激活\n"
                            f"参数: {self._rotation['active_window_size']}活态 / "
                            f"{self._rotation['files_per_slot']}文件/{self._rotation['time_per_slot']}秒"
                        ):
                            self._last_rotation_notify_ts = now

                # 6. 定期拓扑校验(每 10 轮一次)
                self._cycle_count += 1
                if self._cycle_count % 10 == 0:
                    issues = await self.scheduler.validate_topology(all_cells)
                    if issues:
                        for issue in issues:
                            logger.warning(issue)
                    else:
                        logger.debug("[Mon] 拓扑校验: 健康")
                    # 清理字典中已不存在的 slot_id,防止槽位删除/重命名后内存泄漏
                    valid_slots = {c["slot_id"] for c in all_cells}
                    # _notify_cooldowns 的 key 是消息首行(非 slot_id),按时间清理(超过 1200s 的条目)
                    now_ts = time.time()
                    stale_notify = [k for k, ts in self._notify_cooldowns.items() if now_ts - ts > 1200]
                    for k in stale_notify:
                        del self._notify_cooldowns[k]
                    # 其余三个字典按 slot_id 清理
                    stale_healthy = [k for k in self._cell_healthy if k not in valid_slots]
                    for k in stale_healthy:
                        del self._cell_healthy[k]
                    stale_streak = [k for k in self._cell_fail_streak if k not in valid_slots]
                    for k in stale_streak:
                        del self._cell_fail_streak[k]
                    stale_suspicious = [k for k in self._cell_suspicious if k not in valid_slots]
                    for k in stale_suspicious:
                        del self._cell_suspicious[k]
                    # 清理已解除的 FloodWait 记录 + 已删除槽位的记录
                    stale_fw = [k for k in self._cell_floodwait_until if k not in valid_slots]
                    for k in stale_fw:
                        del self._cell_floodwait_until[k]
                    # 顺便清理已过期的 FloodWait 记录(释放内存)
                    expired_fw = [k for k, v in self._cell_floodwait_until.items() if v <= now_ts]
                    for k in expired_fw:
                        del self._cell_floodwait_until[k]
                    if stale_notify or stale_healthy or stale_streak or stale_suspicious:
                        logger.debug(
                            f"[Mon] 清理过期字典条目: notify={len(stale_notify)} "
                            f"healthy={len(stale_healthy)} streak={len(stale_streak)} "
                            f"suspicious={len(stale_suspicious)}"
                        )

                # 6.5 脏数据同步到 CRDB(每 10 轮一次,~5分钟)
                if self._cycle_count % 10 == 0:
                    try:
                        from database.session import sync_dirty_cells_to_crdb
                        await sync_dirty_cells_to_crdb()
                    except Exception as e:
                        logger.warning(f"[Mon] 脏数据同步异常: {e}")

                # A1: 端到端监控告警(每 10 轮一次, ~10 分钟)
                if self._cycle_count % 10 == 0:
                    await self._check_alerts()

                # 7. 报告当前拓扑状态(从缓存读取,不查 DB)
                await self._report_status(all_cells)

                # 8. 定期恢复 lost 频道（每 10 轮一次，~10 分钟）
                if self._cycle_count % 10 == 0:
                    recovered = await self._recover_lost(all_cells)
                    if recovered > 0:
                        self._invalidate_cells_cache()
                        all_cells = await self._get_cells()

            except Exception as e:
                logger.error(f"[Mon] 调度异常: {e}")

            # P1-15:以 stop_event.wait 替代固定 sleep,run_all 触发停止时立即唤醒退出,
            # 避免最多等待一个 RECOVERY_INTERVAL(60s)才响应。超时即正常进入下一轮。
            if self._stop_event is not None:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=RECOVERY_INTERVAL)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(RECOVERY_INTERVAL)

    async def _heartbeat_with_ban_detection(self, all_cells: list[dict]) -> tuple[int, int]:
        """心跳检测 + 封禁识别。返回 (ok_count, ban_count)。
        优化:心跳只写入本地 SQLite(零 CRDB RU)，降级判断使用内存 fail_streak。
        仅在实际发生状态变更(降级/轮转/封禁替换)时才写入 CRDB。
        last_heartbeat 不再写入 CRDB，减少 RU 消耗。

        优化:
        - FloodWait 跳过:处于限速期的频道跳过心跳,避免恶性循环
        - shadow2 降频:冷备频道每 N 轮检测一次,减少不必要的 API 调用
        - 批量 commit:所有心跳写入完成后统一 commit,减少 SQLite 锁冲突
        """
        store = get_cache_store()
        now = time.time()
        # 从传入的 all_cells 中筛选 active/shadow
        cells = [c for c in all_cells if c.get("status") in ("active", "shadow1", "shadow2")]
        ok_count = 0
        ban_count = 0
        has_writes = False  # 标记是否有写入操作,用于决定是否 commit
        for cell in cells:
            slot_id = cell["slot_id"]
            status = cell.get("status", "")

            # FloodWait 跳过:处于限速期的频道跳过心跳,避免恶性循环
            fw_until = self._cell_floodwait_until.get(slot_id, 0)
            if fw_until > now:
                logger.debug(f"[Mon] 心跳跳过 slot={slot_id} (FloodWait 剩余 {int(fw_until - now)}s)")
                continue

            # shadow2 冷备降频:每 N 轮检测一次
            if status == "shadow2" and self._cycle_count % self._SHADOW2_HEARTBEAT_EVERY_N_CYCLES != 0:
                continue

            try:
                # 使用 get_chat 做更彻底的检测
                await self.bot.get_chat(cell["channel_id"])
                # 心跳成功 → 只写本地 SQLite,不碰 CRDB → 零 RU
                await store.write_heartbeat(slot_id, ok=True, _batch=True)
                has_writes = True
                self._cell_healthy[slot_id] = True
                self._cell_fail_streak[slot_id] = 0
                self._cell_suspicious.pop(slot_id, None)  # 恢复后清除疑似标记
                ok_count += 1
            except RetryAfter as e:
                # FloodWait:记录解除时间,本轮跳过等待,后续轮次跳过心跳直到解除
                wait_sec = e.retry_after + 2  # 多加 2s 余量
                self._cell_floodwait_until[slot_id] = now + wait_sec
                logger.warning(
                    f"[Mon] 心跳触发 FloodWait slot={slot_id}, 跳过 {wait_sec}s "
                    f"(本轮不等待,后续轮次自动跳过)"
                )
                await store.write_heartbeat(slot_id, ok=False, _batch=True)
                has_writes = True
                self._cell_fail_streak[slot_id] = self._cell_fail_streak.get(slot_id, 0) + 1
            except TelegramError as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 普通错误(如 flood),记录失败但不降级
                    await store.write_heartbeat(slot_id, ok=False, _batch=True)
                    has_writes = True
                    self._cell_fail_streak[slot_id] = self._cell_fail_streak.get(slot_id, 0) + 1
            except Exception as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 非 TelegramError 异常（如网络超时），记录失败
                    await store.write_heartbeat(slot_id, ok=False, _batch=True)
                    has_writes = True
                    self._cell_fail_streak[slot_id] = self._cell_fail_streak.get(slot_id, 0) + 1
                    logger.warning(f"[Mon] 心跳异常 slot={slot_id}: {e}")

        # 批量 commit:所有心跳写入完成后统一提交,减少 SQLite 锁冲突
        if has_writes:
            try:
                await store.commit()
            except Exception as e:
                logger.warning(f"[Mon] 心跳批量 commit 失败: {e}")
        return ok_count, ban_count

    async def stop(self):
        """停止 Mon。"""
        self._running = False
        # C3: 停止 cells 变更监听任务
        await self._stop_cells_change_watcher()
        if self._admin_bot:
            try:
                await self._admin_bot.shutdown()
            except Exception as e:
                logger.warning(f"[Mon] admin_bot shutdown 异常: {e}")
        try:
            await self.bot.shutdown()
        except Exception as e:
            logger.warning(f"[Mon] bot shutdown 异常: {e}")
        # 关闭 scheduler 自建的 Telethon 客户端(复用 relay_pool 的不关闭)
        try:
            await self.scheduler.shutdown()
        except Exception as e:
            logger.warning(f"[Mon] scheduler shutdown 异常: {e}")
        # close_db 可能因初始化失败/重复关闭而抛异常,需保护避免掩盖其他错误
        try:
            await close_db()
        except Exception as e:
            logger.warning(f"[Mon] close_db 异常: {e}")
        logger.info("[Mon] 监控机器人已停止")

    async def _report_status(self, all_cells: list[dict]):
        """输出当前拓扑健康状态到日志(从缓存读取,不查 DB)。"""
        active_count = len([c for c in all_cells if c.get("status") == "active"])
        total = len(all_cells)
        lost = len([c for c in all_cells if c.get("status") == "lost"])
        r100 = len([c for c in all_cells if c.get("status") == "r100"])
        rotation_config = self._rotation
        logger.info(
            f"[Mon] 拓扑: {active_count}/{total} 活跃, {lost} 失联, {r100} R100 | "
            f"轮转: {rotation_config['active_window_size']}活态, "
            f"{rotation_config['files_per_slot']}文件/{rotation_config['time_per_slot']}秒"
        )


async def run_mon():
    """启动 Mon 监控机器人。"""
    # P1-15:创建并注册全局停止事件,让 run_all 的 SIGTERM/SIGINT handler 能 set 它,
    # 触发 mon_bot 优雅退出,与其它 4 个 bot 行为一致。
    from run_all import _set_stop_event
    stop_event = asyncio.Event()
    _set_stop_event(stop_event)
    mon = MonBot(stop_event=stop_event)
    try:
        await mon.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("[Mon] 收到中断信号,正在停止...")
    except Exception as e:
        logger.error(f"[Mon] 异常退出: {e}")
    finally:
        # start() 正常返回(停止事件触发)或异常退出,都需执行优雅关闭
        await mon.stop()


if __name__ == "__main__":
    asyncio.run(run_mon())