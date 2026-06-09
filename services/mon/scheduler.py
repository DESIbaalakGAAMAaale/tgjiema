"""Mon 调度器：心跳检测、自动降级、环形推进、智能补齐"""

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
from utils.flood_waiter import safe_copy_message, reset_backoff


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
        """批量复制消息到目标频道（带 Flood Wait 自动退避）。返回成功复制数。"""
        copied = 0
        for msg in messages:
            try:
                await safe_copy_message(
                    bot_instance, to_channel, from_channel, msg.message_id,
                )
                copied += 1
            except Exception:
                pass
        if copied > 0:
            reset_backoff()
        return copied

    async def auto_fill_new_channels(self, bot_instance) -> int:
        """智能替补：检测新频道（last_synced_msg_id=0 且消息数 < Active），
        从对应的 Active 槽位补齐所有存量文件。

        「新频道只拉 Mon，系统自动补齐」—— 元宝方案核心设计。
        返回补齐的消息总数。
        """
        col = get_cells_col()
        all_cells = await col.find({})
        groups = self._group_slots(all_cells)
        total_filled = 0

        for _group_key, (a_slot, s1_slot, s2_slot) in groups.items():
            if not a_slot or a_slot["status"] != "active":
                continue

            active_channel = a_slot["channel_id"]

            for shadow_slot in [s1_slot, s2_slot]:
                if not shadow_slot:
                    continue

                last_synced = shadow_slot.get("last_synced_msg_id") or 0
                if last_synced > 0:
                    continue  # 已有同步记录，走增量

                # 检查该 shadow 频道是否为空（或几乎为空）
                shadow_empty = await self._is_channel_nearly_empty(
                    bot_instance, shadow_slot["channel_id"]
                )
                if not shadow_empty:
                    # 有内容但不是通过 Mon 同步的，跳过
                    continue

                # 从 Active 获取所有媒体消息
                all_media = await self._fetch_all_media(bot_instance, active_channel)
                if not all_media:
                    continue

                filled = await self._copy_messages(
                    bot_instance, active_channel,
                    shadow_slot["channel_id"], all_media,
                )
                if filled > 0:
                    # 更新游标
                    latest_id = max(msg.message_id for msg in all_media)
                    await col.update_one(
                        {"slot_id": shadow_slot["slot_id"]},
                        {"$set": {"last_synced_msg_id": latest_id}},
                    )
                    total_filled += filled
                    from loguru import logger
                    logger.info(
                        f"[Mon][填充] {shadow_slot['slot_id']} "
                        f"新频道补齐 {filled} 条消息"
                    )

        return total_filled

    @staticmethod
    async def _is_channel_nearly_empty(bot_instance, channel_id: int, threshold: int = 3) -> bool:
        """判断频道是否几乎为空（媒体消息 ≤ threshold）。"""
        try:
            count = 0
            async for msg in bot_instance.iter_messages(channel_id, limit=20):
                if MonScheduler._is_media_message(msg):
                    count += 1
            return count <= threshold
        except Exception:
            return False

    @staticmethod
    async def _fetch_all_media(bot_instance, channel_id: int, limit: int = 200) -> list:
        """拉取频道中所有媒体消息（用于智能补齐）。"""
        msgs = []
        try:
            async for msg in bot_instance.iter_messages(channel_id, limit=limit):
                if MonScheduler._is_media_message(msg):
                    msgs.append(msg)
        except Exception:
            pass
        return list(reversed(msgs))

    async def validate_topology(self) -> list[str]:
        """定期拓扑校验：检测环形链表是否断裂。

        检查项：
        1. 每个 active cell 的 next_active_chat_id 指向有效的 active cell
        2. 无重复的 next 指针
        3. 所有 active 槽位可达（单向遍历）
        4. 每组 (a, s1, s2) 三元组完整

        返回问题描述列表，空列表表示健康。
        """
        issues = []
        col = get_cells_col()
        all_cells = await col.find({})
        active_cells = [c for c in all_cells if c["status"] == "active"]

        if not active_cells:
            issues.append("[拓扑] 无 Active 槽位，系统不可用")
            return issues

        active_channels = {c["channel_id"] for c in active_cells}
        next_map = {}  # channel_id → next_active_chat_id
        next_reverse = {}  # next_active_chat_id → [channel_ids]
        for c in active_cells:
            nxt = c.get("next_active_chat_id")
            next_map[c["channel_id"]] = nxt
            if nxt:
                next_reverse.setdefault(nxt, []).append(c["channel_id"])

        # 1. 指针有效性
        for c in active_cells:
            nxt = c.get("next_active_chat_id")
            if nxt and nxt not in active_channels:
                issues.append(
                    f"[拓扑]{c['slot_id']}的next指向 {nxt}，但该频道不是active"
                )

        # 2. 重复指针
        for nxt, sources in next_reverse.items():
            if len(sources) > 1:
                slot_names = [
                    c["slot_id"] for c in active_cells
                    if c["channel_id"] in sources
                ]
                issues.append(
                    f"[拓扑] 多个槽位指向同一个next {nxt}: {slot_names}"
                )

        # 3. 可达性：从第一个 active 出发遍历
        visited = set()
        if active_cells:
            current = active_cells[0]
            for _ in range(len(active_cells) + 1):
                if current["channel_id"] in visited:
                    break
                visited.add(current["channel_id"])
                nxt = current.get("next_active_chat_id")
                if not nxt or nxt not in active_channels:
                    break
                current = next(
                    (c for c in active_cells if c["channel_id"] == nxt), None
                )
                if current is None:
                    break

        unreachable = active_channels - visited
        if unreachable:
            slot_names = [
                c["slot_id"] for c in active_cells
                if c["channel_id"] in unreachable
            ]
            issues.append(
                f"[拓扑] 不可达的 Active 槽位: {slot_names}"
            )

        # 4. 三元组完整性
        groups = self._group_slots(all_cells)
        for group_key, (a_slot, s1_slot, s2_slot) in groups.items():
            missing = []
            if not a_slot:
                missing.append("A槽")
            if not s1_slot:
                missing.append("Shadow1")
            if not s2_slot:
                missing.append("Shadow2")
            if missing:
                issues.append(
                    f"[拓扑] 组{group_key}缺失: {', '.join(missing)}"
                )

        return issues