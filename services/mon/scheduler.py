"""Mon 调度器:心跳检测、自动降级、环形推进、智能补齐"""

import os
import re
import datetime as _dt
from pathlib import Path

import yaml
from telethon import TelegramClient
from loguru import logger
from database import (
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


# ─── 降级阈值：连续 fail_streak 达到此值触发降级 ───
# 每个周期默认 60s,阈值 3 = 180s 无响应后触发
FAIL_STREAK_DEGRADE_THRESHOLD = 3


def _get_msg_id(msg) -> int:
    """获取消息 ID，兼容 Telethon (.id) 和 PTB (.message_id)。"""
    return msg.id if hasattr(msg, 'id') else msg.message_id


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
        self._telethon_client: TelegramClient | None = None
        self._cursor_cache: dict[str, int] = {}
        self._flushed_cursors: dict[str, int] = {}  # 已 flush 到 CRDB 的游标值
        self._replicate_count = 0
        self._cursor_flush_interval = 30  # 每 N 次同步 flush 一次(约30分钟)

    async def _ensure_telethon_client(self):
        """获取或创建 Telethon 客户端，用于读取频道历史消息。
        优先复用 relay_pool 已有实例，否则查询 relay_accounts 表创建新客户端。
        失败时返回 None，调用方回退到旧 get_updates 行为。
        """
        if self._telethon_client and self._telethon_client.is_connected():
            return self._telethon_client
        
        from config.settings import settings
        api_id = settings.RELAY_API_ID
        api_hash = settings.RELAY_API_HASH
        if not api_id or not api_hash:
            logger.warning("[Mon] RELAY_API_ID/RELAY_API_HASH 未配置，无法使用 Telethon 历史复制")
            return None
        
        # 尝试复用 relay_pool 已有实例
        try:
            from services.relay_pool import relay_pool
            if relay_pool.instances:
                for instance in relay_pool.instances:
                    if instance._client and instance._client.is_connected():
                        self._telethon_client = instance._client
                        logger.info(f"[Mon] 复用 relay_pool 已有 Telethon 客户端 ({instance.phone})")
                        return self._telethon_client
        except Exception:
            pass
        
        # 创建新客户端：从 relay_accounts 表获取第一个账号
        try:
            from database.session import get_collection
            col = get_collection("relay_accounts")
            accounts = await col.find({}, limit=1)
            if accounts:
                phone = accounts[0]["phone"]
                session_path = str(Path(__file__).parent.parent.parent / "data" / f"relay_session_{phone}")
                if os.path.exists(session_path + ".session"):
                    client = TelegramClient(session_path, api_id, api_hash)
                    await client.connect()
                    if await client.is_user_authorized():
                        self._telethon_client = client
                        logger.info(f"[Mon] 创建 Telethon 客户端成功 ({phone})")
                        return client
                    await client.disconnect()
                else:
                    logger.warning(f"[Mon] session 文件不存在: {session_path}.session，跳过 Telethon 初始化")
        except Exception as e:
            logger.warning(f"[Mon] 创建 Telethon 客户端失败: {e}")
        
        return None

    async def run_degrade_check(
        self, all_cells: list[dict],
        cell_fail_streak: dict[str, int] = None,
        cell_suspicious: dict[str, bool] = None,
    ) -> list[str] | tuple[list[str], dict[str, bool]]:
        """执行一轮降级检查,返回日志描述列表。

        降级判断基于内存中的连续失败次数(fail_streak),零 CRDB RU。
        仅在实际触发降级时才写入 CRDB(sync_dirty_cells_to_crdb + log_rotate)。

        二次确认去抖: fail_streak >= 3 时先标记为「疑似」,下一轮仍失败才确认降级,
        避免 Telegram 临时报错(网络抖动)触发级联写风暴。

        Args:
            all_cells: 由调用方统一查询传入,避免重复 DB 查询。
            cell_fail_streak: {slot_id: 连续失败次数},由 MonBot 心跳循环维护。
            cell_suspicious: {slot_id: 是否已标记为疑似},由 MonBot 维护。
        """
        alerts = []
        if cell_fail_streak is None:
            cell_fail_streak = {}
        if cell_suspicious is None:
            cell_suspicious = {}

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

            # 二次确认去抖: 首轮标记疑似,次轮确认降级
            if not cell_suspicious.get(slot_id):
                cell_suspicious[slot_id] = True
                alerts.append(
                    f"[SUSPICIOUS] {slot_id} 连续失败{fail_streak}次,标记疑似,"
                    f"等待下一轮确认(防误降级)"
                )
                continue

            # 已确认: 执行降级
            promote_slot, cascade_slot = self._get_next_promotable(group)
            await self._degrade_group(active_slot, promote_slot, cascade_slot, fail_streak, all_cells)
            cell_suspicious.pop(slot_id, None)  # 降级后清除疑似标记
            from_status = active_slot.get("status", "?")
            alerts.append(
                f"[DEGRADE] {slot_id}({from_status}→lost) "
                f"→ {promote_slot['slot_id'] if promote_slot else 'none'}(shadow→active) "
                f"→ {cascade_slot['slot_id'] if cascade_slot else 'none'}(shadow2→shadow1) "
                f"连续失败{fail_streak}次(已二次确认)"
            )

        return alerts, cell_suspicious

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
        """核心功能:将每个 Active 槽的新消息复制到同组的 Shadow 槽位 + R100 全量归档。

        这是 Mon 的「写入」职责——替代原 backup_bot 的文件备份功能。
        R100 作为最终全量归档，接收所有 Active 写入的消息（不重复写入）。
        返回复制的消息总数。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        self._replicate_count += 1
        groups = self._group_slots(all_cells)
        total_copied = 0

        # 找到 R100 槽位（全量归档用）
        r100_slot = next((c for c in all_cells if c.get("is_r100", 0) == 1 and c.get("status") == "r100"), None)

        for _group_key, group in groups.items():
            active_slot = self._find_active_slot(group)
            if not active_slot:
                continue

            a_slot, s1_slot, s2_slot = group
            # 1. 复制到同组的 shadow1/shadow2
            shadows = [s for s in (s1_slot, s2_slot) if s and s.get("status") in ("shadow1", "shadow2")]

            slot_id = active_slot["slot_id"]
            last_cursor = self._cursor_cache.get(slot_id) or active_slot.get("last_synced_msg_id") or 0
            new_messages = await self._fetch_new_messages(
                bot_instance, active_slot["channel_id"], last_cursor
            )
            if not new_messages:
                continue

            # N-16-2: 仅将游标推进到「所有影子都成功复制」的最大 msg_id
            # 避免某条消息复制失败后被永久跳过
            shadow_max_ids = []  # 每个影子成功复制的最大 msg_id
            for shadow in shadows:
                copied, mappings = await self._copy_messages(
                    bot_instance, active_slot["channel_id"],
                    shadow["channel_id"], new_messages,
                )
                total_copied += copied
                if mappings:
                    await self._write_backup_mappings(
                        active_slot["channel_id"], shadow["channel_id"], mappings
                    )
                    shadow_max_ids.append(max(orig_id for orig_id, _ in mappings))

            # 2. 额外复制到 R100 全量归档（如果配置了 R100）
            if r100_slot and new_messages:
                copied_r100, mappings_r100 = await self._copy_messages(
                    bot_instance, active_slot["channel_id"],
                    r100_slot["channel_id"], new_messages,
                )
                total_copied += copied_r100
                if mappings_r100:
                    await self._write_backup_mappings(
                        active_slot["channel_id"], r100_slot["channel_id"], mappings_r100
                    )

            if shadow_max_ids:
                # 取所有影子中最小的成功最大 id，确保失败的消息不会被跳过
                self._cursor_cache[slot_id] = min(shadow_max_ids)

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

    @staticmethod
    async def _write_backup_mappings(main_channel_id: int, backup_channel_id: int, mappings: list[tuple[int, int]]):
        """将影子复制产生的 msg_id 映射写入 message_backups 表。
        故障切换时 dsp_bot 可据此查找影子频道中的新 msg_id。
        """
        try:
            from database.session import save_message_backup
            for main_msg_id, backed_msg_id in mappings:
                await save_message_backup(main_msg_id, backup_channel_id, backed_msg_id)
        except Exception as e:
            logger.warning(f"[Mon] 写入 message_backups 映射失败: {e}")

    async def _fetch_new_messages(self, bot_instance, channel_id: int, last_cursor: int) -> list:
        """获取频道中最后游标之后的新消息(媒体文件)。
        
        优先使用 Telethon iter_messages 可靠获取历史消息，
        失败时回退到 get_updates。
        """
        msgs = []
        try:
            client = await self._ensure_telethon_client()
            if client:
                async for msg in client.iter_messages(channel_id, min_id=last_cursor, reverse=True):
                    if msg.media:
                        msgs.append(msg)
                return msgs
        except Exception as e:
            logger.warning(f"[Mon][复制] Telethon 获取频道 {channel_id} 消息失败: {e}")
        
        # 回退到 get_updates
        logger.warning(f"[Mon][复制] Telethon 不可用，回退到 get_updates 获取频道 {channel_id} 消息（建议排查 Telethon 连接）")
        try:
            updates = await bot_instance.get_updates(
                offset=-100,
                allowed_updates=["channel_post"],
                timeout=5,
            )
            for update in updates:
                if update.channel_post and update.channel_post.chat_id == channel_id:
                    msg = update.channel_post
                    if msg.message_id > last_cursor:
                        if msg.photo or msg.video or msg.document or msg.audio or msg.animation:
                            msgs.append(msg)
        except Exception as e:
            logger.warning(f"[Mon][复制] 获取频道 {channel_id} 消息失败: {e}")
        return msgs

    @staticmethod
    async def _copy_messages(bot_instance, from_channel: int, to_channel: int, messages: list) -> tuple[int, list[tuple[int, int]]]:
        """批量复制消息到目标频道。返回 (成功复制数, [(原msg_id, 新msg_id)])。"""
        if not messages:
            return 0, []
        copied = 0
        mappings = []
        for msg in messages:
            try:
                result = await bot_instance.copy_message(
                    chat_id=to_channel,
                    from_chat_id=from_channel,
                    message_id=_get_msg_id(msg),
                )
                copied += 1
                mappings.append((_get_msg_id(msg), result.message_id))
            except Exception as e:
                logger.warning(f"[Mon][复制] copy_message 失败 msg_id={_get_msg_id(msg)}: {e}")
        return copied, mappings

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

                filled, mappings = await self._copy_messages(
                    bot_instance, active_channel,
                    shadow_slot["channel_id"], all_media,
                )
                if filled > 0:
                    await self._write_backup_mappings(
                        active_channel, shadow_slot["channel_id"], mappings
                    )
                    latest_id = max(_get_msg_id(msg) for msg in all_media)
                    await store.update_cell_fields_local(shadow_slot["slot_id"], {"last_synced_msg_id": latest_id})
                    shadow_slot["last_synced_msg_id"] = latest_id
                    total_filled += filled
                    logger.info(
                        f"[Mon][填充] {shadow_slot['slot_id']} "
                        f"新频道补齐 {filled} 条消息"
                    )

        return total_filled

    async def _is_channel_nearly_empty(self, bot_instance, channel_id: int, threshold: int = 3) -> bool:
        """判断频道是否几乎为空(媒体消息 ≤ threshold)。"""
        try:
            client = await self._ensure_telethon_client()
            if client:
                count = 0
                async for msg in client.iter_messages(channel_id, limit=threshold):
                    if msg.media:
                        count += 1
                return count < threshold
        except Exception as e:
            logger.warning(f"[Mon][检查] 频道 {channel_id} 检查失败: {e}")
        return False

    async def _fetch_all_media(self, bot_instance, channel_id: int, limit: int = 200) -> list:
        """拉取频道中所有媒体消息(用于智能补齐)。

        N-16-3: 分页拉取全部历史，而非仅最新 200 条，确保大频道补齐完整。
        """
        msgs = []
        try:
            client = await self._ensure_telethon_client()
            if client:
                batch_size = 200
                max_id = 0  # 0 表示从最新开始
                while True:
                    batch = []
                    async for msg in client.iter_messages(channel_id, limit=batch_size, max_id=max_id):
                        if msg.media:
                            batch.append(msg)
                    if not batch:
                        break
                    msgs.extend(batch)
                    # 用本批最小 id 作为下一批的 max_id，继续往前翻页
                    max_id = min(_get_msg_id(msg) for msg in batch) - 1
                    if len(batch) < batch_size:
                        break
        except Exception as e:
            logger.warning(f"[Mon][填充] 拉取频道 {channel_id} 媒体消息失败: {e}")
        return msgs

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