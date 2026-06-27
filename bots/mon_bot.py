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
import time

from loguru import logger
from telegram import Bot
from telegram.error import TelegramError

from config import settings
from database import (
    init_db, close_db, get_cells_col, get_active_cells,
    set_cell_status, update_cell_heartbeat, log_rotate,
    add_spare_channel, get_spare_for_account, get_any_spare,
    consume_spare, get_rotation_config, set_rotation_config,
    _client,
)
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
        # 轮转状态
        self._rotation = {
            "active_window_size": 3,
            "files_per_slot": 500,
            "time_per_slot": 3600,
        }
        self._rotation_reload_countdown = 0
        # ─── 心跳本地缓存:减少 CRDB 写入 ───
        # slot_id → True(上次心跳成功)/ False(上次心跳失败)
        self._cell_healthy: dict[str, bool] = {}
        # ─── cells 全量缓存:一次查询,多轮复用 ───
        self._cells_cache: list[dict] | None = None
        self._cells_cache_ts: float = 0

    async def _get_cells(self) -> list[dict]:
        """获取全量 cells,带进程内缓存。

        缓存策略:
        - 正常周期:复用缓存(0 CRDB 查询)
        - 每 5 周期:强制重载一次(兜底其他进程写入)
        - 写入 cells 后(轮转/降级/封禁):主动失效
        """
        now = time.time()
        if self._cycle_count % 5 == 0:
            self._cells_cache = None
        if self._cells_cache and (now - self._cells_cache_ts) < 120:
            return self._cells_cache
        col = get_cells_col()
        self._cells_cache = await col.find({})
        self._cells_cache_ts = now
        return self._cells_cache

    def _invalidate_cells_cache(self):
        """写入 cells 后调用,下次循环自动重载。"""
        self._cells_cache = None

    async def _reload_rotation_config(self):
        """从 DB rotation_config 表重新读取轮转参数(30 个周期一次)。"""
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
        """通知管理员(通过 admin_bot 或直接向管理员聊天发消息)。"""
        if not self._admin_chat_id:
            logger.warning(f"[Mon][Notify] 未配置 ADMIN_TELEGRAM_ID,无法通知: {msg}")
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
        """处理频道封禁/丢失:通知管理员 + 尝试从备用池补充。"""
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
                f"操作: 直接替换,无需降级"
            )
            logger.info(f"[Mon][Ban] {slot_id} 封禁 → 备用池 {spare_ch} 替换")
        else:
            notify_msg += (
                "⚠️ 备用池无可用频道!\n"
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

    async def _check_rotation(self, all_cells: list[dict]):
        """检查活跃频道窗口是否该轮转。
        条件:当前窗口内任一活跃频道满足 file_count >= files_per_slot
              或 rotation time >= time_per_slot
        切换:将当前窗口的 status 由 active 改为 rotation(临时)标记,
              推进窗口指针到下一组。
        """
        active_cells = [c for c in all_cells if c.get("status") == "active"]
        if not active_cells:
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
                logger.info(f"[Mon][Rotation] {cell['slot_id']} file_count={fc} >= {files_per_slot},触发轮转")
                should_rotate = True
                break

            # 检查时间
            rts = cell.get("rotation_started_at")
            if rts:
                try:
                    started = _dt.datetime.fromisoformat(rts)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= time_per_slot:
                        logger.info(f"[Mon][Rotation] {cell['slot_id']} 已运行 {elapsed:.0f}s >= {time_per_slot}s,触发轮转")
                        should_rotate = True
                        break
                except (ValueError, TypeError):
                    pass

        if not should_rotate:
            return False

        # ── 执行轮转:将当前窗口全部标记为 shadow1(休眠),推进窗口 ──
        # 使用 CRDB 事务保证原子性:所有 update 要么全部成功,要么全部回滚
        # 避免中途失败导致部分槽位状态不一致

        # 先计算分组和窗口信息(在事务外,避免长事务)
        groups = self.scheduler._group_slots(all_cells)
        all_group_keys = sorted(groups.keys(), key=lambda x: int(x))

        if not all_group_keys:
            logger.warning("[Mon][Rotation] 无有效分组,跳过轮转")
            return

        # 找到当前 active 窗口的起始位置
        current_idx = 0
        for i, gkey in enumerate(all_group_keys):
            a_slot = groups[gkey][0]
            if a_slot and a_slot.get("status") == "active":
                current_idx = i
                break

        # 计算下一窗口的组键列表
        next_window_keys = []
        for i in range(window_size):
            wi = (current_idx + window_size + i) % len(all_group_keys)
            next_window_keys.append(all_group_keys[wi])

        now_iso = now.isoformat()
        async with _client.transaction() as conn:
            # 休眠当前窗口(active → shadow1)
            for i in range(window_size):
                wi = (current_idx + i) % len(all_group_keys)
                gkey = str(all_group_keys[wi])
                if gkey in groups and groups[gkey][0]:
                    a_slot = groups[gkey][0]
                    await conn.execute(
                        "UPDATE cells SET status = $1, file_count = 0, rotation_started_at = $2 WHERE slot_id = $3",
                        "shadow1", now_iso, a_slot["slot_id"],
                    )
                    logger.info(f"[Mon][Rotation] 休眠 {a_slot['slot_id']}")

            # 唤醒下一窗口(shadow1 → active)
            for gkey in next_window_keys:
                if gkey in groups and groups[gkey][1]:
                    s1_slot = groups[gkey][1]
                    target_active = groups[gkey][0]
                    nxt = target_active.get("next_active_chat_id") if target_active else None
                    await conn.execute(
                        "UPDATE cells SET status = $1, next_active_chat_id = $2, file_count = 0, rotation_started_at = $3, last_heartbeat = $4 WHERE slot_id = $5",
                        "active", str(nxt) if nxt else None, now_iso, now_iso, s1_slot["slot_id"],
                    )
                    # 同组的原 active → shadow1(如果还没变的话)
                    if target_active and target_active["status"] == "active":
                        await conn.execute(
                            "UPDATE cells SET status = $1 WHERE slot_id = $2",
                            "shadow1", target_active["slot_id"],
                        )
                    logger.info(f"[Mon][Rotation] 唤醒 {s1_slot['slot_id']} → active")

        await self._notify_admin(
            f"🔄 频道轮转通知\n\n"
            f"原窗口: 组 {all_group_keys[current_idx]}-{all_group_keys[(current_idx + window_size - 1) % len(all_group_keys)]}\n"
            f"新窗口: 组 {next_window_keys[0]}-{next_window_keys[-1]}\n"
            f"触发条件: 文件数/时间达到阈值\n"
            f"当前参数: {files_per_slot}文件 / {time_per_slot}秒 / {window_size}活态"
        )
        return True

    async def start(self):
        """启动 Mon 主循环。"""
        await init_db()
        await self.bot.initialize()
        self._running = True
        self._rotation_reload_countdown = 0
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

                # 4. 降级检查
                alerts = await self.scheduler.run_degrade_check(all_cells)
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
        优化:每 3 个周期(约 180s)写一次 CRDB,减少 RU 消耗。
        degrade_check 的 heartbeat_timeout 默认 240s,180s 写入间隔留有 60s 安全余量。
        """
        # 从传入的 all_cells 中筛选 active/shadow
        cells = [c for c in all_cells if c.get("status") in ("active", "shadow1", "shadow2")]
        ok_count = 0
        ban_count = 0
        for cell in cells:
            slot_id = cell["slot_id"]
            try:
                # 使用 get_chat 做更彻底的检测
                await self.bot.get_chat(cell["channel_id"])
                # 每 3 个周期写一次心跳到 CRDB,保持 last_heartbeat 新鲜
                # 避免因心跳过旧被 degrade_check 误判为挂掉
                if self._cycle_count % 3 == 0:
                    await update_cell_heartbeat(slot_id)
                self._cell_healthy[slot_id] = True
                ok_count += 1
            except TelegramError as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
                    ban_count += 1
                    await self._handle_channel_ban(cell, str(e))
                else:
                    # 普通错误(如 flood),只记不降级
                    pass
            except Exception as e:
                if _is_ban_error(e):
                    self._cell_healthy[slot_id] = False
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