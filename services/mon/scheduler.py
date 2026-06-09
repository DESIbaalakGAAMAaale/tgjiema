"""Mon 调度器：心跳检测、自动降级、环形推进"""

import asyncio
import os
import re
import datetime as _dt

import yaml
from database import (
    get_cells_col,
    set_cell_status, update_cell_heartbeat,
    log_rotate,
)


def _load_mon_config():
    topo_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "config",
        "topology.yaml",
    )
    with open(topo_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    mon_cfg = config.get("mon", {})
    return {
        "heartbeat_interval": mon_cfg.get("heartbeat_interval", 30),
        "heartbeat_timeout": mon_cfg.get("heartbeat_timeout", 90),
        "degrade_cooldown": mon_cfg.get("degrade_cooldown", 300),
        "r100_managed": mon_cfg.get("r100_managed", False),
    }


class MonScheduler:
    """Mon 调度器。

    每个调度周期：
    1. 遍历所有 active 槽位，检查心跳是否超时
    2. 超时 → 降级: active→lost, shadow1→active, shadow2→shadow1
    3. R100 槽位永不自降，仅记告警
    """

    def __init__(self):
        cfg = _load_mon_config()
        self.heartbeat_timeout = cfg["heartbeat_timeout"]
        self.degrade_cooldown = cfg["degrade_cooldown"]
        self.r100_managed = cfg["r100_managed"]

    async def run_degrade_check(self) -> list[str]:
        """执行一轮降级检查，返回日志描述列表。"""
        col = get_cells_col()
        all_cells = await col.find({})
        alerts = []
        now = _dt.datetime.now(_dt.timezone.utc)

        groups = self._group_slots(all_cells)

        for _group_key, (a_slot, s1_slot, s2_slot) in groups.items():
            if not a_slot:
                continue

            is_r100 = a_slot.get("is_r100", 0) == 1
            last_hb = a_slot.get("last_heartbeat", "")
            if not last_hb:
                continue

            try:
                hb_time = _dt.datetime.fromisoformat(last_hb)
                elapsed = (now - hb_time).total_seconds()
            except (ValueError, TypeError):
                continue

            if elapsed <= self.heartbeat_timeout:
                continue

            if is_r100 and not self.r100_managed:
                alerts.append(
                    f"[R100] {a_slot['slot_id']} 心跳超时 {elapsed:.0f}s，仅告警不降级"
                )
                await log_rotate(
                    from_slot_id=a_slot["slot_id"],
                    to_slot_id=a_slot["slot_id"],
                    from_status="r100",
                    to_status="r100",
                    reason=f"R100 heartbeat timeout {elapsed:.0f}s — manual takeover required",
                    triggered_by="mon",
                )
                continue

            await self._degrade_group(a_slot, s1_slot, s2_slot, elapsed)
            alerts.append(
                f"[DEGRADE] {a_slot['slot_id']}(active→lost) "
                f"→ {s1_slot['slot_id']}(shadow1→active) "
                f"→ {s2_slot['slot_id']}(shadow2→shadow1) "
                f"超时{elapsed:.0f}s"
            )

        return alerts

    def _group_slots(self, all_cells: list[dict]) -> dict:
        """将槽位按组号聚合为 (a, s1, s2) 三元组。"""
        groups = {}
        for cell in all_cells:
            sid = cell["slot_id"]
            m = re.match(r'[as](\d+)[ab]?', sid)
            if not m:
                continue
            group_num = m.group(1)
            if group_num not in groups:
                groups[group_num] = [None, None, None]

            if sid.startswith("a"):
                groups[group_num][0] = cell
            elif sid.endswith("a"):
                groups[group_num][1] = cell
            elif sid.endswith("b"):
                groups[group_num][2] = cell

        return groups

    async def _degrade_group(
        self, a_slot: dict, s1_slot: dict, s2_slot: dict, elapsed: float
    ):
        """执行降级：active→lost, shadow1→active, shadow2→shadow1"""
        now = _dt.datetime.now(_dt.timezone.utc)
        col = get_cells_col()

        # active → lost
        await set_cell_status(a_slot["slot_id"], "lost")
        await log_rotate(
            a_slot["slot_id"], s1_slot["slot_id"] if s1_slot else "none",
            "active", "lost",
            f"heartbeat timeout {elapsed:.0f}s", "mon",
        )

        # shadow1 → active
        if s1_slot:
            new_next = a_slot.get("next_active_chat_id")
            await set_cell_status(s1_slot["slot_id"], "active")
            await col.update_one(
                {"slot_id": s1_slot["slot_id"]},
                {"$set": {
                    "next_active_chat_id": new_next,
                    "last_heartbeat": now.isoformat(),
                    "degrade_count": a_slot.get("degrade_count", 0) + 1,
                }},
            )
            prev_id = a_slot.get("prev_slot_id")
            if prev_id:
                await col.update_one(
                    {"slot_id": prev_id},
                    {"$set": {"next_active_chat_id": new_next}},
                )
            await log_rotate(
                s1_slot["slot_id"], s1_slot["slot_id"],
                "shadow1", "active",
                f"promoted after {a_slot['slot_id']} timeout", "mon",
            )

        # shadow2 → shadow1
        if s2_slot:
            await set_cell_status(s2_slot["slot_id"], "shadow1")
            await log_rotate(
                s2_slot["slot_id"], s2_slot["slot_id"],
                "shadow2", "shadow1",
                f"cascade after {a_slot['slot_id']} timeout", "mon",
            )

    async def heartbeat_all(self, bot_instance) -> int:
        """对 active/shadow 槽位发心跳（频道拉一条消息验证可达性）。
        返回成功计数。
        """
        col = get_cells_col()
        cells = await col.find({"status": {"$in": ["active", "shadow1", "shadow2"]}})
        count = 0
        for cell in cells:
            try:
                msgs = await bot_instance.get_messages(cell["channel_id"], limit=1)
                if msgs and len(msgs) > 0:
                    await update_cell_heartbeat(cell["slot_id"])
                    count += 1
            except Exception:
                pass
        return count

    async def replicate_all_active_to_shadows(self, bot_instance) -> int:
        """核心功能：将每个 Active A 槽的新消息复制到对应的 Shadow1/Shadow2。

        这是 Mon 的「写入」职责——替代原 backup_bot 的文件备份功能。
        返回复制的消息总数。
        """
        col = get_cells_col()
        all_cells = await col.find({})
        groups = self._group_slots(all_cells)
        total_copied = 0

        for _group_key, (a_slot, s1_slot, s2_slot) in groups.items():
            if not a_slot or a_slot["status"] != "active":
                continue

            last_cursor = a_slot.get("last_synced_msg_id") or 0
            new_messages = await self._fetch_new_messages(
                bot_instance, a_slot["channel_id"], last_cursor
            )
            if not new_messages:
                continue

            # 复制到 shadow1
            if s1_slot:
                copied_1 = await self._copy_messages(
                    bot_instance, a_slot["channel_id"],
                    s1_slot["channel_id"], new_messages,
                )
                total_copied += copied_1

            # 复制到 shadow2
            if s2_slot:
                copied_2 = await self._copy_messages(
                    bot_instance, a_slot["channel_id"],
                    s2_slot["channel_id"], new_messages,
                )
                total_copied += copied_2

            # 更新游标
            latest_id = max(msg.message_id for msg in new_messages if msg)
            await col.update_one(
                {"slot_id": a_slot["slot_id"]},
                {"$set": {"last_synced_msg_id": latest_id}},
            )

        return total_copied

    async def _fetch_new_messages(self, bot_instance, channel_id: int, last_cursor: int) -> list:
        """获取频道中最后游标之后的新消息（媒体文件）。"""
        msgs = []
        try:
            # 从最新消息开始往回拉，直到遇到 last_cursor
            async for msg in bot_instance.iter_messages(channel_id, limit=50):
                if msg.message_id <= last_cursor:
                    break
                if self._is_media_message(msg):
                    msgs.append(msg)
        except Exception:
            pass
        return list(reversed(msgs))  # 按时间正序

    @staticmethod
    def _is_media_message(msg) -> bool:
        return any([
            msg.photo, msg.video, msg.document,
            msg.audio, msg.voice, msg.animation,
        ])

    @staticmethod
    async def _copy_messages(bot_instance, from_channel: int, to_channel: int, messages: list) -> int:
        """批量复制消息到目标频道。返回成功复制数。"""
        copied = 0
        for msg in messages:
            try:
                await bot_instance.copy_message(
                    chat_id=to_channel,
                    from_chat_id=from_channel,
                    message_id=msg.message_id,
                )
                copied += 1
            except Exception:
                pass
        return copied