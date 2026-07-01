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
from telegram.error import TelegramError

from config import settings
from database import (
    init_db, close_db, get_cells_col,
    log_rotate,
    add_spare_channel, get_spare_for_account, get_any_spare,
    consume_spare, get_rotation_config, set_rotation_config,
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

    def __init__(self):
        self.bot = Bot(token=TOKEN)
        self.scheduler = MonScheduler()
        self._running = False
        self._cycle_count = 0
        # 通知管理员的 admin_bot 实例(延迟初始化)
        self._admin_bot = None
        self._admin_chat_id = settings.ADMIN_TELEGRAM_ID
        self._notify_cooldowns: dict[str, float] = {}
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
        # ─── cells 全量缓存:一次查询,多轮复用 ───
        self._cells_cache: list[dict] | None = None
        self._cells_cache_ts: float = 0

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
                local_val = await store.get_kv(f"rot_{cfg_key}")
                if local_val is not None:
                    try:
                        val = cast_fn(local_val)
                    except (ValueError, TypeError):
                        val = default
                else:
                    crdb_val = await get_rotation_config(cfg_key)
                    if crdb_val and crdb_val.isdigit():
                        val = int(crdb_val)
                        await store.set_kv(f"rot_{cfg_key}", str(val))
                    else:
                        env_key = local_key.upper()
                        val = getattr(settings, f"ROTATION_{env_key}", default)
                        await store.set_kv(f"rot_{cfg_key}", str(val))
                old = self._rotation.get(local_key, 0)
                if old != val:
                    changed = True
                    logger.info(f"[Mon][Config] {local_key}: {old} → {val}")
                self._rotation[local_key] = val

            if changed:
                logger.info(f"[Mon][Config] 轮转参数已更新: {self._rotation}")
        except Exception as e:
            logger.warning(f"[Mon][Config] 读取轮转配置失败: {e}")

    async def _notify_admin(self, msg: str):
        """通知管理员(通过 admin_bot 或直接向管理员聊天发消息)。
        按事件类型冷却：同一事件类型 10 分钟内不重复发送。
        """
        if not self._admin_chat_id or self._admin_chat_id == 0:
            return

        # 按事件类型冷却：取消息首行作为事件标识
        event_key = msg.split("\n")[0] if msg else msg
        if not hasattr(self, "_notify_cooldowns"):
            self._notify_cooldowns: dict[str, float] = {}
        now = time.time()
        last = self._notify_cooldowns.get(event_key, 0)
        if now - last < 600:
            return

        try:
            if not self._admin_bot:
                admin_token = settings.ADMIN_BOT_TOKEN
                if admin_token:
                    self._admin_bot = Bot(token=admin_token)
                    await self._admin_bot.initialize()
                else:
                    self._admin_bot = self.bot
            await self._admin_bot.send_message(
                chat_id=self._admin_chat_id,
                text=msg,
                disable_web_page_preview=True,
            )
            self._notify_cooldowns[event_key] = 0  # 成功，清除冷却
        except Exception as e:
            self._notify_cooldowns[event_key] = now
            logger.warning(f"[Mon][Notify] 发送通知失败: {e}")

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
            await consume_spare(spare_ch)
            new_status = status if status in ("active", "shadow1", "shadow2", "r100") else "active"
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            col = get_cells_col()
            await col.update_one(
                {"slot_id": slot_id},
                {"$set": {
                    "channel_id": spare_ch,
                    "status": new_status,
                    "account_name": account_name,
                    "file_count": 0,
                    "rotation_started_at": now_iso,
                    "updated_at": now_iso,
                }},
            )
            await store.update_cell_fields_local(slot_id, {
                "channel_id": spare_ch,
                "status": new_status,
                "account_name": account_name,
                "file_count": 0,
                "rotation_started_at": now_iso,
            }, mark_dirty=True)
            self._update_cell_in_cache(slot_id, {
                "channel_id": spare_ch, "status": new_status,
                "file_count": 0, "rotation_started_at": now_iso,
            })
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
        else:
            notify_msg += (
                "⚠️ 备用池无可用频道!\n"
                "请通过管理员 Bot 添加备用频道:\n"
                "/spare_add <频道ID> [账号名]\n"
            )
            if status in ("active", "shadow1", "shadow2"):
                col = get_cells_col()
                now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
                await col.update_one(
                    {"slot_id": slot_id},
                    {"$set": {"status": "lost", "updated_at": now_iso}},
                )
                await store.update_cell_fields_local(slot_id, {"status": "lost"}, mark_dirty=True)
                self._update_cell_in_cache(slot_id, {"status": "lost"})
                await log_rotate(
                    from_slot_id=slot_id, to_slot_id="NONE",
                    from_status=status, to_status="lost",
                    reason=f"频道封禁且无备用: {error}",
                    triggered_by="mon",
                )

        await self._notify_admin(notify_msg)
        self._invalidate_cells_cache()

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
            promote, cascade = self.scheduler._get_next_promotable(group)
            if promote:
                promoted_slots.append((promote, cell))
                if cascade and cascade.get("status") != "shadow1":
                    cascade_slots.append(cascade)
                preds = next_reverse.get(cell["channel_id"], [])
                for pred in preds:
                    if pred["slot_id"] != cell["slot_id"]:
                        ring_repairs.append((pred["slot_id"], promote["channel_id"]))
            else:
                logger.warning(f"[Mon][Rotation] {sid} 无可用 shadow 提升,仅休眠不替换")

        if not promoted_slots:
            logger.warning("[Mon][Rotation] 无可提升的 shadow 槽位,跳过轮转")
            return False

        for cell in demoted_slots:
            if cell.get("status") != "shadow1":
                batch_updates.append((cell["slot_id"], {
                    "status": "shadow1", "file_count": 0,
                }, False))
                self._update_cell_in_cache(cell["slot_id"], {
                    "status": "shadow1", "file_count": 0,
                })

        for promote_slot, from_cell in promoted_slots:
            nxt = from_cell.get("next_active_chat_id")
            batch_updates.append((promote_slot["slot_id"], {
                "status": "active", "next_active_chat_id": nxt,
                "file_count": 0, "rotation_started_at": now_iso,
            }, False))
            self._update_cell_in_cache(promote_slot["slot_id"], {
                "status": "active", "next_active_chat_id": nxt,
                "file_count": 0, "rotation_started_at": now_iso,
            })

        for cascade_slot in cascade_slots:
            batch_updates.append((cascade_slot["slot_id"], {"status": "shadow1"}, False))
            self._update_cell_in_cache(cascade_slot["slot_id"], {"status": "shadow1"})

        for prev_slot_id, new_next in ring_repairs:
            batch_updates.append((prev_slot_id, {"next_active_chat_id": new_next}, False))
            self._update_cell_in_cache(prev_slot_id, {"next_active_chat_id": new_next})

        store = get_cache_store()
        await store.batch_update_cells_local(batch_updates)

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
        self._rotation_reload_countdown = 0
        # 从本地 SQLite 恢复心跳状态,避免重启后 fail_streak 从零开始
        store = get_cache_store()
        hb_data = await store.get_all_heartbeats()
        for slot_id, data in hb_data.items():
            self._cell_healthy[slot_id] = True  # 历史记录存在说明上次是健康的
            self._cell_fail_streak[slot_id] = data.get("fail_streak", 0)
        if hb_data:
            logger.info(f"[Mon] 从 SQLite 恢复 {len(hb_data)} 条心跳记录")
        logger.info("[Mon] 监控机器人 v2 已启动")

        while self._running:
            try:
                # 0. 重新加载轮转配置(每 30 周期一次)
                await self._reload_rotation_config()

                # ── 一次查询 cells 表(走缓存),所有方法复用 ──
                all_cells = await self._get_cells()

                # 1. 对所有 active + shadow 槽位发心跳(同时检测封禁)
                ok_count, ban_count = await self._heartbeat_with_ban_detection(all_cells)
                if ok_count > 0:
                    logger.info(f"[Mon] 心跳: {ok_count} 正常, {ban_count} 封禁")
                if ban_count > 0:
                    self._invalidate_cells_cache()  # 封禁替换写了 cells,下次循环重载
                    all_cells = await self._get_cells()  # 重新加载以反映变更

                # 2. 核心写入:将 Active 槽位新文件同步到 Shadow 频道
                copied = await self.scheduler.replicate_all_active_to_shadows(self.bot, all_cells)
                if copied > 0:
                    logger.info(f"[Mon] 文件同步: 复制了 {copied} 条消息到 Shadow 频道")

                # 3. 智能替补:新频道自动补齐存量文件
                filled = await self.scheduler.auto_fill_new_channels(self.bot, all_cells)
                if filled > 0:
                    logger.info(f"[Mon] 智能替补: 补齐 {filled} 条消息到新频道")

                # 4. 降级检查(使用内存中的连续失败次数,零 CRDB RU)
                alerts = await self.scheduler.run_degrade_check(all_cells, self._cell_fail_streak)
                if alerts:
                    for msg in alerts:
                        logger.warning(msg)
                        metrics.increment("mon.degrade")
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
                        self._last_rotation_notify_ts = now
                        await self._notify_admin(
                            f"🔄 频道轮转完成\n\n"
                            f"当前窗口已推进，新窗口已激活\n"
                            f"参数: {self._rotation['active_window_size']}活态 / "
                            f"{self._rotation['files_per_slot']}文件/{self._rotation['time_per_slot']}秒"
                        )

                # 6. 定期拓扑校验(每 10 轮一次)
                self._cycle_count += 1
                if self._cycle_count % 10 == 0:
                    issues = await self.scheduler.validate_topology(all_cells)
                    if issues:
                        for issue in issues:
                            logger.warning(issue)
                    else:
                        logger.info("[Mon] 拓扑校验: 健康")

                # 7. 报告当前拓扑状态(从缓存读取,不查 DB)
                await self._report_status(all_cells)

            except Exception as e:
                logger.error(f"[Mon] 调度异常: {e}")

            await asyncio.sleep(RECOVERY_INTERVAL)

    async def _heartbeat_with_ban_detection(self, all_cells: list[dict]) -> tuple[int, int]:
        """心跳检测 + 封禁识别。返回 (ok_count, ban_count)。
        优化:心跳只写入本地 SQLite(零 CRDB RU)，降级判断使用内存 fail_streak。
        仅在实际发生状态变更(降级/轮转/封禁替换)时才写入 CRDB。
        last_heartbeat 不再写入 CRDB，减少 RU 消耗。
        """
        store = get_cache_store()
        # 从传入的 all_cells 中筛选 active/shadow
        cells = [c for c in all_cells if c.get("status") in ("active", "shadow1", "shadow2")]
        ok_count = 0
        ban_count = 0
        for cell in cells:
            slot_id = cell["slot_id"]
            try:
                # 使用 get_chat 做更彻底的检测
                await self.bot.get_chat(cell["channel_id"])
                # 心跳成功 → 只写本地 SQLite,不碰 CRDB → 零 RU
                await store.write_heartbeat(slot_id, ok=True)
                self._cell_healthy[slot_id] = True
                self._cell_fail_streak[slot_id] = 0
                ok_count += 1
            except TelegramError as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 普通错误(如 flood),记录失败但不降级
                    await store.write_heartbeat(slot_id, ok=False)
                    self._cell_fail_streak[slot_id] = self._cell_fail_streak.get(slot_id, 0) + 1
            except Exception as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 非 TelegramError 异常（如网络超时），记录失败
                    await store.write_heartbeat(slot_id, ok=False)
                    self._cell_fail_streak[slot_id] = self._cell_fail_streak.get(slot_id, 0) + 1
                    logger.warning(f"[Mon] 心跳异常 slot={slot_id}: {e}")
        return ok_count, ban_count

    async def stop(self):
        """停止 Mon。"""
        self._running = False
        if self._admin_bot:
            try:
                await self._admin_bot.shutdown()
            except Exception:
                pass
        await self.bot.shutdown()
        await close_db()
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
    mon = MonBot()
    try:
        await mon.start()
    except KeyboardInterrupt:
        await mon.stop()
    except Exception as e:
        logger.error(f"[Mon] 异常退出: {e}")
        await mon.stop()


if __name__ == "__main__":
    asyncio.run(run_mon())