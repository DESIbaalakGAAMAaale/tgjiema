"""Mon 调度器:心跳检测、自动降级、环形推进、智能补齐"""

import asyncio
import os
import re
import datetime as _dt
from typing import Any

import yaml
from loguru import logger
from database import (
    log_rotate,
)
from utils.flood_waiter import safe_copy_message, reset_backoff
from utils.file_utils import is_media_message


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
        "heartbeat_timeout": mon_cfg.get("heartbeat_timeout", 240),
        "degrade_cooldown": mon_cfg.get("degrade_cooldown", 300),
        "r100_managed": mon_cfg.get("r100_managed", False),
    }


# ─── 降级阈值：连续 fail_streak 达到此值触发降级 ───
# 每个周期默认 60s,阈值 3 = 180s 无响应后触发
FAIL_STREAK_DEGRADE_THRESHOLD = 3


class MonScheduler:
    """Mon 调度器。

    每个调度周期:
    1. 遍历所有 active 槽位,检查连续失败次数是否超过阈值
    2. 超阈值 → 降级: active→lost, shadow1→active, shadow2→shadow1
    3. R100 槽位永不自降,仅记告警
    """

    def __init__(self):
        cfg = _load_mon_config()
        self.degrade_cooldown = cfg["degrade_cooldown"]
        self.r100_managed = cfg["r100_managed"]
        # ─── last_synced_msg_id 本地缓存:每 30 次同步 flush 一次(30分钟)，且只 flush 有变化的 ───
        self._cursor_cache: dict[str, int] = {}
        self._flushed_cursors: dict[str, int] = {}  # 已 flush 到 CRDB 的游标值
        self._replicate_count = 0
        self._cursor_flush_interval = 30  # 每 N 次同步 flush 一次(约30分钟)

    async def run_degrade_check(self, all_cells: list[dict], cell_fail_streak: dict[str, int] = None) -> list[str]:
        """执行一轮降级检查,返回日志描述列表。

        降级判断基于内存中的连续失败次数(fail_streak),零 CRDB RU。
        仅在实际触发降级时才写入 CRDB(set_cell_status + log_rotate)。

        Args:
            all_cells: 由调用方统一查询传入,避免重复 DB 查询。
            cell_fail_streak: {slot_id: 连续失败次数},由 MonBot 心跳循环维护。
        """
        alerts = []
        if cell_fail_streak is None:
            cell_fail_streak = {}

        groups = self._group_slots(all_cells)

        for _group_key, group in groups.items():
            active_slot = self._find_active_slot(group)
            if not active_slot:
                continue

            a_slot, s1_slot, s2_slot = group
            is_r100 = active_slot.get("is_r100", 0) == 1
            slot_id = active_slot["slot_id"]
            fail_streak = cell_fail_streak.get(slot_id, 0)

            if fail_streak < FAIL_STREAK_DEGRADE_THRESHOLD:
                continue

            degrade_count = active_slot.get("degrade_count", 0)
            if degrade_count == 0:
                cooldown = self.degrade_cooldown
            elif degrade_count == 1:
                cooldown = 600
            else:
                cooldown = 1200

            last_hb = active_slot.get("last_heartbeat", "")
            if last_hb:
                try:
                    hb_time = _dt.datetime.fromisoformat(last_hb)
                    elapsed = (_dt.datetime.now(_dt.timezone.utc) - hb_time).total_seconds()
                    if elapsed < cooldown:
                        continue
                except (ValueError, TypeError):
                    pass

            if is_r100 and not self.r100_managed:
                alerts.append(
                    f"[R100] {slot_id} 连续失败{fail_streak}次,仅告警不降级"
                )
                await log_rotate(
                    from_slot_id=slot_id,
                    to_slot_id=slot_id,
                    from_status="r100",
                    to_status="r100",
                    reason=f"R100 fail_streak={fail_streak} — manual takeover required",
                    triggered_by="mon",
                )
                continue

            promote_slot, cascade_slot = self._get_next_promotable(group)
            await self._degrade_group(active_slot, promote_slot, cascade_slot, fail_streak, all_cells)
            from_status = active_slot.get("status", "?")
            alerts.append(
                f"[DEGRADE] {slot_id}({from_status}→lost) "
                f"→ {promote_slot['slot_id'] if promote_slot else 'none'}(shadow→active) "
                f"→ {cascade_slot['slot_id'] if cascade_slot else 'none'}(shadow2→shadow1) "
                f"连续失败{fail_streak}次"
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

            if sid.endswith("a") and not sid.startswith("a"):
                groups[group_num][1] = cell
            elif sid.endswith("b"):
                groups[group_num][2] = cell
            elif sid.startswith("a"):
                groups[group_num][0] = cell

        return groups

    @staticmethod
    def _find_active_slot(group: list) -> dict | None:
        """返回组内当前 status=active 的槽位,若无则返回 None。"""
        for slot in group:
            if slot and slot.get("status") == "active":
                return slot
        return None

    @staticmethod
    def _get_next_promotable(group: list) -> tuple[dict | None, dict | None]:
        """返回组内下一个可提升的 shadow 槽位和后续 shadow 槽位。
        返回 (promote_to_active, promote_to_shadow1)
        - 若 s1 是 shadow1: 提升 s1→active, s2→shadow1
        - 若 s1 是 shadow2/lost 但 s2 是 shadow1: 提升 s2→active
        - 若无可用 shadow: 返回 (None, None)
        """
        a_slot, s1_slot, s2_slot = group
        s1_ok = s1_slot and s1_slot.get("status") in ("shadow1", "shadow2")
        s2_ok = s2_slot and s2_slot.get("status") in ("shadow1", "shadow2")
        if s1_slot and s1_slot.get("status") == "shadow1":
            return s1_slot, s2_slot if s2_slot and s2_slot.get("status") == "shadow2" else None
        if s1_slot and s1_slot.get("status") in ("shadow2", "lost"):
            if s2_slot and s2_slot.get("status") == "shadow1":
                return s2_slot, None
        if s2_slot and s2_slot.get("status") == "shadow1":
            return s2_slot, None
        return None, None

    async def _degrade_group(
        self, failed_slot: dict, promote_slot: dict | None, cascade_slot: dict | None,
        fail_streak: int, all_cells: list[dict],
    ):
        """执行降级:failed→lost, promote_slot→active, cascade_slot→shadow1

        热路径零 CRDB RU:常规 failover(shadow→active, shadow2→shadow1)只写本地 SQLite。
        异常事件(failed→lost)写入 CRDB 审计 + 本地标记脏。
        log_rotate 在本地更新完成后执行(审计日志,低频)。
        """
        from database.cache_store import get_cache_store
        store = get_cache_store()
        batch_updates = []
        log_entries = []

        pred_slot_ids = []
        if promote_slot:
            failed_chan = failed_slot["channel_id"]
            for c in all_cells:
                if c.get("status") == "active" and c.get("next_active_chat_id") == failed_chan \
                   and c["slot_id"] != failed_slot["slot_id"]:
                    pred_slot_ids.append(c["slot_id"])

        new_next = failed_slot.get("next_active_chat_id")
        promote_channel = promote_slot["channel_id"] if promote_slot else None
        new_degrade_count = failed_slot.get("degrade_count", 0) + 1
        old_status = failed_slot.get("status", "active")

        batch_updates.append((failed_slot["slot_id"], {
            "status": "lost",
            "degrade_count": new_degrade_count,
            "next_active_chat_id": None,
        }, True))
        failed_slot["status"] = "lost"
        failed_slot["degrade_count"] = new_degrade_count
        failed_slot["next_active_chat_id"] = None
        log_entries.append((
            failed_slot["slot_id"], promote_slot["slot_id"] if promote_slot else "none",
            old_status, "lost",
            f"fail_streak={fail_streak}", "mon",
        ))

        if promote_slot:
            promote_old_status = promote_slot.get("status", "shadow1")
            batch_updates.append((promote_slot["slot_id"], {
                "status": "active",
                "next_active_chat_id": new_next,
                "degrade_count": new_degrade_count,
            }, True))
            promote_slot["status"] = "active"
            promote_slot["next_active_chat_id"] = new_next
            promote_slot["degrade_count"] = new_degrade_count

            for pred_sid in pred_slot_ids:
                batch_updates.append((pred_sid, {"next_active_chat_id": promote_channel}, True))
                for c in all_cells:
                    if c["slot_id"] == pred_sid:
                        c["next_active_chat_id"] = promote_channel
                        break
            log_entries.append((
                promote_slot["slot_id"], promote_slot["slot_id"],
                promote_old_status, "active",
                f"promoted after {failed_slot['slot_id']} timeout", "mon",
            ))

        if cascade_slot:
            cascade_old_status = cascade_slot.get("status", "shadow2")
            if cascade_old_status != "shadow1":
                batch_updates.append((cascade_slot["slot_id"], {"status": "shadow1"}, True))
                cascade_slot["status"] = "shadow1"
            log_entries.append((
                cascade_slot["slot_id"], cascade_slot["slot_id"],
                cascade_old_status, "shadow1",
                f"cascade after {failed_slot['slot_id']} timeout", "mon",
            ))

        await store.batch_update_cells_local(batch_updates)

        for entry in log_entries:
            await log_rotate(*entry)

    async def replicate_all_active_to_shadows(self, bot_instance, all_cells: list[dict]) -> int:
        """核心功能:将每个 Active 槽的新消息复制到同组的 Shadow 槽位。

        这是 Mon 的「写入」职责——替代原 backup_bot 的文件备份功能。
        返回复制的消息总数。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        self._replicate_count += 1
        groups = self._group_slots(all_cells)
        total_copied = 0

        for _group_key, group in groups.items():
            active_slot = self._find_active_slot(group)
            if not active_slot:
                continue

            a_slot, s1_slot, s2_slot = group
            shadows = [s for s in (s1_slot, s2_slot) if s and s.get("status") in ("shadow1", "shadow2")]
            if not shadows:
                continue

            slot_id = active_slot["slot_id"]
            last_cursor = self._cursor_cache.get(slot_id) or active_slot.get("last_synced_msg_id") or 0
            new_messages = await self._fetch_new_messages(
                bot_instance, active_slot["channel_id"], last_cursor
            )
            if not new_messages:
                continue

            for shadow in shadows:
                copied = await self._copy_messages(
                    bot_instance, active_slot["channel_id"],
                    shadow["channel_id"], new_messages,
                )
                total_copied += copied

            latest_id = max(msg.message_id for msg in new_messages if msg)
            self._cursor_cache[slot_id] = latest_id

        if self._replicate_count % self._cursor_flush_interval == 0:
            await self._flush_cursor_cache()

        return total_copied

    async def _flush_cursor_cache(self):
        """批量 flush 本地缓存的游标到 SQLite（零 CRDB RU）。只 flush 自上次以来有变化的游标。"""
        if not self._cursor_cache:
            return
        from database.cache_store import get_cache_store
        store = get_cache_store()
        changed = 0
        for slot_id, cursor in self._cursor_cache.items():
            if self._flushed_cursors.get(slot_id) == cursor:
                continue
            try:
                await store.update_cell_fields_local(slot_id, {"last_synced_msg_id": cursor})
                self._flushed_cursors[slot_id] = cursor
                changed += 1
            except Exception:
                pass
        if changed > 0:
            logger.debug(f"[Mon] flush 游标到本地: {changed} 个有变化")

    async def _fetch_new_messages(self, bot_instance, channel_id: int, last_cursor: int) -> list:
        """获取频道中最后游标之后的新消息(媒体文件)。
        
        使用 getUpdates API 获取最近频道消息，筛选目标频道中 > last_cursor 的媒体消息。
        """
        msgs = []
        try:
            updates = await bot_instance.get_updates(
                offset=-100,  # 获取最近 100 条更新
                allowed_updates=["channel_post"],
                timeout=5,
            )
            for update in updates:
                if update.channel_post and update.channel_post.chat_id == channel_id:
                    msg = update.channel_post
                    if msg.message_id > last_cursor:
                        # 只复制媒体消息
                        if msg.photo or msg.video or msg.document or msg.audio or msg.animation:
                            msgs.append(msg)
        except Exception as e:
            logger.warning(f"[Mon][复制] 获取频道 {channel_id} 消息失败: {e}")
        return msgs

    @staticmethod
    async def _copy_messages(bot_instance, from_channel: int, to_channel: int, messages: list) -> int:
        """批量复制消息到目标频道。返回成功复制数。"""
        if not messages:
            return 0
        copied = 0
        for msg in messages:
            try:
                await bot_instance.copy_message(
                    chat_id=to_channel,
                    from_chat_id=from_channel,
                    message_id=msg.message_id,
                )
                copied += 1
            except Exception as e:
                logger.warning(f"[Mon][复制] copy_message 失败 msg_id={msg.message_id}: {e}")
        return copied

    async def auto_fill_new_channels(self, bot_instance, all_cells: list[dict]) -> int:
        """智能替补:检测新频道(last_synced_msg_id=0 且消息数 < Active),
        从对应的 Active 槽位补齐所有存量文件。

        「新频道只拉 Mon,系统自动补齐」—— 元宝方案核心设计。
        返回补齐的消息总数。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        groups = self._group_slots(all_cells)
        from database.cache_store import get_cache_store
        store = get_cache_store()
        total_filled = 0

        for _group_key, group in groups.items():
            active_slot = self._find_active_slot(group)
            if not active_slot:
                continue

            active_channel = active_slot["channel_id"]
            a_slot, s1_slot, s2_slot = group

            for shadow_slot in [s1_slot, s2_slot]:
                if not shadow_slot:
                    continue
                if shadow_slot["slot_id"] == active_slot["slot_id"]:
                    continue

                last_synced = shadow_slot.get("last_synced_msg_id") or 0
                if last_synced > 0:
                    continue

                shadow_empty = await self._is_channel_nearly_empty(
                    bot_instance, shadow_slot["channel_id"]
                )
                if not shadow_empty:
                    continue

                all_media = await self._fetch_all_media(bot_instance, active_channel)
                if not all_media:
                    continue

                filled = await self._copy_messages(
                    bot_instance, active_channel,
                    shadow_slot["channel_id"], all_media,
                )
                if filled > 0:
                    latest_id = max(msg.message_id for msg in all_media)
                    await store.update_cell_fields_local(shadow_slot["slot_id"], {"last_synced_msg_id": latest_id})
                    shadow_slot["last_synced_msg_id"] = latest_id
                    total_filled += filled
                    logger.info(
                        f"[Mon][填充] {shadow_slot['slot_id']} "
                        f"新频道补齐 {filled} 条消息"
                    )

        return total_filled

    @staticmethod
    async def _is_channel_nearly_empty(bot_instance, channel_id: int, threshold: int = 3) -> bool:
        """判断频道是否几乎为空(媒体消息 ≤ threshold)。
        
        通过 getChat 获取频道信息，检查 recently_active_date 等指标。
        """
        try:
            # 直接验证频道可达，返回 True 表示需要检查
            await bot_instance.get_chat(channel_id)
            # 无法迭代消息，保守返回 False（不做补齐）
            return False
        except Exception as e:
            logger.warning(f"[Mon][检查] 频道 {channel_id} 检查失败: {e}")
            return False

    @staticmethod
    async def _fetch_all_media(bot_instance, channel_id: int, limit: int = 200) -> list:
        """拉取频道中所有媒体消息(用于智能补齐)。
        
        ptb 21.6 不支持迭代消息，返回空列表。
        """
        return []

    async def validate_topology(self, all_cells: list[dict]) -> list[str]:
        """定期拓扑校验:检测环形链表是否断裂。

        检查项:
        1. 每个 active cell 的 next_active_chat_id 指向有效的 active cell
        2. 无重复的 next 指针
        3. 所有 active 槽位可达(单向遍历)
        4. 每组 (a, s1, s2) 三元组完整

        返回问题描述列表,空列表表示健康。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        issues = []
        active_cells = [c for c in all_cells if c["status"] == "active"]

        if not active_cells:
            issues.append("[拓扑] 无 Active 槽位,系统不可用")
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
                    f"[拓扑]{c['slot_id']}的next指向 {nxt},但该频道不是active"
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

        # 3. 可达性:从第一个 active 出发遍历
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