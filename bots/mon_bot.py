"""Mon 监控机器人 v2
职责：
  1. 频道健康监控 + 心跳检测
  2. 封禁检测 → 通知管理员 Bot → 自动从备用池补充
  3. 活跃频道轮转（按文件数/时间切换）
  4. 文件同步写入 Shadow 频道
  5. 自动降级调度
与 admin_bot 完全分离，admin 负责管理配置，mon 负责运行时监控和文件冗余。
"""

import asyncio
import datetime as _dt
import logging
import re

from telegram import Bot
from telegram.error import TelegramError

from config import settings
from database import (
    init_db, close_db, get_cells_col, get_active_cells,
    set_cell_status, update_cell_heartbeat, log_rotate,
    add_spare_channel, get_spare_for_account, get_any_spare,
    consume_spare, get_rotation_config, set_rotation_config,
)
from services.mon import MonScheduler
from utils.monitor import metrics

logger = logging.getLogger("mon_bot")

TOKEN = settings.MON_BOT_TOKEN
if not TOKEN:
    raise RuntimeError("MON_BOT_TOKEN 未配置，监控机器人必须有独立的 Bot Token")
RECOVERY_INTERVAL = getattr(settings, "MON_CHECK_INTERVAL", 60)

# ─── 封禁关键词（Telegram API 错误消息匹配） ───
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
    """Mon 监控机器人 v2（后台轮询 + 轮转 + 备用池补充）。"""

    def __init__(self):
        self.bot = Bot(token=TOKEN)
        self.scheduler = MonScheduler()
        self._running = False
        self._cycle_count = 0
        # 通知管理员的 admin_bot 实例（延迟初始化）
        self._admin_bot = None
        self._admin_chat_id = settings.ADMIN_TELEGRAM_ID
        # 轮转状态
        self._rotation = {
            "active_window_size": 3,
            "files_per_slot": 500,
            "time_per_slot": 3600,
        }
        self._rotation_reload_countdown = 0

    async def _reload_rotation_config(self):
        """从 DB rotation_config 表重新读取轮转参数（30 个周期一次）。"""
        self._rotation_reload_countdown -= 1
        if self._rotation_reload_countdown > 0:
            return

        self._rotation_reload_countdown = 30
        try:
            vals = {}
            for key in ("active_window_size", "rotation_files_per_slot", "rotation_time_per_slot"):
                val = await get_rotation_config(key)
                if val and val.isdigit():
                    vals[key.replace("rotation_", "")] = int(val)
            # .env 兜底
            if "files_per_slot" not in vals:
                vals["files_per_slot"] = getattr(settings, "ROTATION_FILES_PER_SLOT", 500)
            if "time_per_slot" not in vals:
                vals["time_per_slot"] = getattr(settings, "ROTATION_TIME_PER_SLOT", 3600)
            if "active_window_size" not in vals:
                vals["active_window_size"] = getattr(settings, "ROTATION_ACTIVE_WINDOW_SIZE", 3)

            changed = False
            for k, v in vals.items():
                old = self._rotation.get(k, 0)
                if old != v:
                    changed = True
                    logger.info(f"[Mon][Config] {k}: {old} → {v}")
                self._rotation[k] = v

            if changed:
                logger.info(f"[Mon][Config] 轮转参数已更新: {self._rotation}")
        except Exception as e:
            logger.warning(f"[Mon][Config] 读取轮转配置失败: {e}")

    async def _notify_admin(self, msg: str):
        """通知管理员（通过 admin_bot 或直接向管理员聊天发消息）。"""
        if not self._admin_chat_id:
            logger.warning(f"[Mon][Notify] 未配置 ADMIN_TELEGRAM_ID，无法通知: {msg}")
            return
        try:
            if not self._admin_bot:
                admin_token = settings.ADMIN_BOT_TOKEN
                if admin_token:
                    self._admin_bot = Bot(token=admin_token)
                    await self._admin_bot.initialize()
                else:
                    self._admin_bot = self.bot  # 回退到 Mon Bot 自己的 token
            await self._admin_bot.send_message(
                chat_id=self._admin_chat_id,
                text=msg,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"[Mon][Notify] 发送通知失败: {e}")

    async def _handle_channel_ban(self, cell: dict, error: str):
        """处理频道封禁/丢失：通知管理员 + 尝试从备用池补充。"""
        slot_id = cell.get("slot_id", "?")
        channel_id = cell.get("channel_id", 0)
        account_name = cell.get("account_name", "未知")
        status = cell.get("status", "?")

        # ── 1. 通知管理员 ──
        notify_msg = (
            f"🚨 频道封禁告警\n\n"
            f"槽位: {slot_id}\n"
            f"频道 ID: {channel_id}\n"
            f"所属账号: {account_name}\n"
            f"状态: {status}\n"
            f"错误: {error}\n\n"
        )

        # ── 2. 尝试从备用池补充 ──
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
            # 将备用频道写入 cells 表替换封禁频道
            col = get_cells_col()
            now = _dt.datetime.now(_dt.timezone.utc)
            await col.update_one(
                {"slot_id": slot_id},
                {"$set": {
                    "channel_id": spare_ch,
                    "status": status,  # 保持原状态
                    "account_name": account_name,  # 继承原账号
                    "last_heartbeat": now.isoformat(),
                    "file_count": 0,
                    "rotation_started_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }},
            )
            await log_rotate(
                from_slot_id=slot_id, to_slot_id=slot_id,
                from_status=status, to_status=status,
                reason=f"封禁替换: old={channel_id} → new={spare_ch} ({spare_source})",
                triggered_by="mon",
            )
            notify_msg += (
                f"✅ 已自动从{spare_source}补充\n"
                f"新频道 ID: {spare_ch}\n"
                f"操作: 直接替换，无需降级"
            )
            logger.info(f"[Mon][Ban] {slot_id} 封禁 → 备用池 {spare_ch} 替换")
        else:
            notify_msg += (
                "⚠️ 备用池无可用频道！\n"
                "请通过管理员 Bot 添加备用频道:\n"
                "/spare_add <频道ID> [账号名]\n"
            )
            # 标记为 lost
            if status in ("active", "shadow1", "shadow2"):
                await set_cell_status(slot_id, "lost")
                await log_rotate(
                    from_slot_id=slot_id, to_slot_id="NONE",
                    from_status=status, to_status="lost",
                    reason=f"频道封禁且无备用: {error}",
                    triggered_by="mon",
                )

        await self._notify_admin(notify_msg)

    async def _check_rotation(self):
        """检查活跃频道窗口是否该轮转。
        条件：当前窗口内任一活跃频道满足 file_count >= files_per_slot
              或 rotation time >= time_per_slot
        切换：将当前窗口的 status 由 active 改为 rotation（临时）标记，
              推进窗口指针到下一组。
        """
        col = get_cells_col()
        all_cells = await col.find({"status": "active"})
        if not all_cells:
            return

        window_size = self._rotation.get("active_window_size", 3)
        files_per_slot = self._rotation.get("files_per_slot", 500)
        time_per_slot = self._rotation.get("time_per_slot", 3600)

        now = _dt.datetime.now(_dt.timezone.utc)
        should_rotate = False

        for cell in all_cells:
            # 检查文件数
            fc = cell.get("file_count") or 0
            if fc >= files_per_slot:
                logger.info(f"[Mon][Rotation] {cell['slot_id']} file_count={fc} >= {files_per_slot}，触发轮转")
                should_rotate = True
                break

            # 检查时间
            rts = cell.get("rotation_started_at")
            if rts:
                try:
                    started = _dt.datetime.fromisoformat(rts)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= time_per_slot:
                        logger.info(f"[Mon][Rotation] {cell['slot_id']} 已运行 {elapsed:.0f}s >= {time_per_slot}s，触发轮转")
                        should_rotate = True
                        break
                except (ValueError, TypeError):
                    pass

        if not should_rotate:
            return

        # ── 执行轮转：将当前窗口全部标记为 shadow1（休眠），推进窗口 ──
        # 当前窗口内的 active cells → 改为 "idle"（临时休眠）
        # 环中接下来 window_size 个 shadow1 → 提升为 active
        # 注意：active 滑块推进，原 active 降为新窗口的 shadow1

        # 先获取所有 slot（包括 active, shadow1, shadow2）
        all_cells_full = await col.find({})
        groups = self.scheduler._group_slots(all_cells_full)

        # 找到所有 active 组并排序
        active_groups = []
        for gkey, (a_slot, s1_slot, s2_slot) in groups.items():
            if a_slot and a_slot["status"] == "active":
                active_groups.append((int(gkey), a_slot, s1_slot, s2_slot))
        active_groups.sort(key=lambda x: x[0])

        if not active_groups:
            return

        # 当前窗口索引
        first_active_group = active_groups[0][0]
        all_group_keys = sorted(int(k) for k in groups.keys())
        current_idx = all_group_keys.index(first_active_group)

        # 找出下一组 active 窗口起始位置
        next_start_idx = (current_idx + window_size) % len(all_group_keys)
        next_window_keys = []
        for i in range(window_size):
            ni = (next_start_idx + i) % len(all_group_keys)
            next_window_keys.append(str(all_group_keys[ni]))

        # 休眠当前窗口（active → idle）
        for i in range(window_size):
            wi = (current_idx + i) % len(all_group_keys)
            gkey = str(all_group_keys[wi])
            if gkey in groups and groups[gkey][0]:
                a_slot = groups[gkey][0]
                await col.update_one(
                    {"slot_id": a_slot["slot_id"]},
                    {"$set": {
                        "status": "shadow1",
                        "file_count": 0,
                        "rotation_started_at": now.isoformat(),
                    }},
                )
                logger.info(f"[Mon][Rotation] 休眠 {a_slot['slot_id']}")

        # 唤醒下一窗口（shadow1 → active）
        for gkey in next_window_keys:
            if gkey in groups and groups[gkey][1]:
                s1_slot = groups[gkey][1]
                # 获取当前 active 的 next_active_chat_id
                target_active = groups[gkey][0]
                nxt = target_active.get("next_active_chat_id") if target_active else None
                await col.update_one(
                    {"slot_id": s1_slot["slot_id"]},
                    {"$set": {
                        "status": "active",
                        "next_active_chat_id": nxt,
                        "file_count": 0,
                        "rotation_started_at": now.isoformat(),
                        "last_heartbeat": now.isoformat(),
                    }},
                )
                # 同组的原 active → shadow1（如果还没变的话）
                if target_active and target_active["status"] == "active":
                    await col.update_one(
                        {"slot_id": target_active["slot_id"]},
                        {"$set": {"status": "shadow1"}},
                    )
                logger.info(f"[Mon][Rotation] 唤醒 {s1_slot['slot_id']} → active")

        await self._notify_admin(
            f"🔄 频道轮转通知\n\n"
            f"原窗口: 组 {all_group_keys[current_idx]}-{all_group_keys[(current_idx + window_size - 1) % len(all_group_keys)]}\n"
            f"新窗口: 组 {next_window_keys[0]}-{next_window_keys[-1]}\n"
            f"触发条件: 文件数/时间达到阈值\n"
            f"当前参数: {files_per_slot}文件 / {time_per_slot}秒 / {window_size}活态"
        )

    async def start(self):
        """启动 Mon 主循环。"""
        await init_db()
        await self.bot.initialize()
        self._running = True
        self._rotation_reload_countdown = 0
        logger.info("[Mon] 监控机器人 v2 已启动")

        while self._running:
            try:
                # 0. 重新加载轮转配置（每 30 周期一次）
                await self._reload_rotation_config()

                # 1. 对所有 active + shadow 槽位发心跳（同时检测封禁）
                ok_count, ban_count = await self._heartbeat_with_ban_detection()
                if ok_count > 0:
                    logger.info(f"[Mon] 心跳: {ok_count} 正常, {ban_count} 封禁")

                # 2. 核心写入：将 Active 槽位新文件同步到 Shadow 频道
                copied = await self.scheduler.replicate_all_active_to_shadows(self.bot)
                if copied > 0:
                    logger.info(f"[Mon] 文件同步: 复制了 {copied} 条消息到 Shadow 频道")

                # 3. 智能替补：新频道自动补齐存量文件
                filled = await self.scheduler.auto_fill_new_channels(self.bot)
                if filled > 0:
                    logger.info(f"[Mon] 智能替补: 补齐 {filled} 条消息到新频道")

                # 4. 降级检查
                alerts = await self.scheduler.run_degrade_check()
                if alerts:
                    for msg in alerts:
                        logger.warning(msg)
                        metrics.increment("mon.degrade")
                        # 降级告警通知管理员
                        if "[DEGRADE]" in msg:
                            await self._notify_admin(f"⚠️ {msg}")

                # 5. 活跃频道轮转检查
                await self._check_rotation()

                # 6. 定期拓扑校验（每 10 轮一次）
                self._cycle_count += 1
                if self._cycle_count % 10 == 0:
                    issues = await self.scheduler.validate_topology()
                    if issues:
                        for issue in issues:
                            logger.warning(issue)
                    else:
                        logger.info("[Mon] 拓扑校验: 健康")

                # 7. 报告当前拓扑状态
                await self._report_status()

            except Exception as e:
                logger.error(f"[Mon] 调度异常: {e}")

            await asyncio.sleep(RECOVERY_INTERVAL)

    async def _heartbeat_with_ban_detection(self) -> tuple[int, int]:
        """心跳检测 + 封禁识别。返回 (ok_count, ban_count)。"""
        col = get_cells_col()
        cells = await col.find({"status": {"$in": ["active", "shadow1", "shadow2"]}})
        ok_count = 0
        ban_count = 0
        for cell in cells:
            try:
                # 使用 get_chat 做更彻底的检测
                await self.bot.get_chat(cell["channel_id"])
                await update_cell_heartbeat(cell["slot_id"])
                ok_count += 1
            except TelegramError as e:
                if _is_ban_error(e):
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 普通错误（如 flood），只记不降级
                    pass
            except Exception as e:
                if _is_ban_error(e):
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
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

    async def _report_status(self):
        """输出当前拓扑健康状态到日志。"""
        cells = await get_active_cells()
        active_count = len(cells)
        all_cells = await get_cells_col().find({})
        total = len(all_cells)
        lost = len([c for c in all_cells if c["status"] == "lost"])
        r100 = len([c for c in all_cells if c["status"] == "r100"])
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