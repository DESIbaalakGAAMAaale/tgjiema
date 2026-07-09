"""Mon 调度器:心跳检测、自动降级、环形推进、智能补齐"""

import os
import re
import datetime as _dt

import yaml
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
        self._replicate_count = 0

    async def shutdown(self):
        """Manifest 驱动后无需 Telethon 客户端,shutdown 无操作。
        保留方法签名兼容 mon_bot 调用。
        """
        pass

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
                # 失败次数已回落到阈值以下,说明频道已恢复健康,清除疑似标记
                # 不清除会导致下次 fail_streak 再次升到阈值时跳过"首轮标记"步骤,直接降级
                cell_suspicious.pop(slot_id, None)
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
            from_status = active_slot.get("status", "?")  # 在 _degrade_group 修改前捕获
            await self._degrade_group(active_slot, promote_slot, cascade_slot, fail_streak, all_cells)
            cell_suspicious.pop(slot_id, None)  # 降级后清除疑似标记
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

        Manifest 驱动(免 Telethon 读历史):
        - Up Bot 写入 Active 时已登记 manifest
        - Mon Bot 查 manifest 获取"Active 有但 Shadow/R100 没有"的文件
        - 用 bot.copy_messages 从 Active 复制到 Shadow/R100
        - 更新 manifest + message_backups(兼容 dsp_bot 故障切换)
        返回复制的消息总数。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        self._replicate_count += 1
        groups = self._group_slots(all_cells)
        total_copied = 0
        from database.cache_store import get_cache_store
        store = get_cache_store()

        # 找到 R100 槽位（全量归档用）
        r100_slot = next((c for c in all_cells if c.get("is_r100", 0) == 1 and c.get("status") == "r100"), None)
        if not r100_slot:
            logger.warning(f"[Mon][R100] 未找到 R100 槽位，跳过归档。cells 总数={len(all_cells)}")

        for _group_key, group in groups.items():
            active_slot = self._find_active_slot(group)
            if not active_slot:
                continue

            a_slot, s1_slot, s2_slot = group
            shadows = [s for s in (s1_slot, s2_slot) if s and s.get("status") in ("shadow1", "shadow2")]
            slot_id = active_slot["slot_id"]
            group_id = int(_group_key)
            active_channel = active_slot["channel_id"]

            # 1. R100 全量归档(manifest 驱动)
            if r100_slot:
                r100_channel_id = r100_slot["channel_id"]
                copied = await self._copy_missing_via_manifest(
                    bot_instance, store, group_id,
                    src_channel_id=active_channel,
                    dst_channel_id=r100_channel_id,
                    main_channel_id=active_channel,  # R100 的 main_msg_id 用 active 的
                )
                total_copied += copied
                if copied > 0:
                    logger.info(f"[Mon][R100] slot={slot_id} 归档 {copied} 条")

            # 2. 复制到同组的 shadow1/shadow2(manifest 驱动)
            if not shadows:
                logger.warning(
                    f"[Mon] slot={slot_id} 无可用 shadow,跳过复制 "
                    f"(可能两个 shadow 均 lost,请检查拓扑健康)"
                )
                continue

            for shadow in shadows:
                copied = await self._copy_missing_via_manifest(
                    bot_instance, store, group_id,
                    src_channel_id=active_channel,
                    dst_channel_id=shadow["channel_id"],
                    main_channel_id=active_channel,  # message_backups 的 main_msg_id 用 active 的
                )
                total_copied += copied
                if copied > 0:
                    logger.info(
                        f"[Mon][复制] slot={slot_id} {active_channel}→{shadow['channel_id']} "
                        f"复制 {copied} 条"
                    )

        return total_copied

    async def _copy_missing_via_manifest(
        self, bot_instance, store, group_id: int,
        src_channel_id: int, dst_channel_id: int, main_channel_id: int,
    ) -> int:
        """Manifest 驱动的副本同步:查 manifest 获取 dst 缺失的文件,从 src 复制过去。

        Args:
            src_channel_id: 源频道(复制来源,通常是 active)
            dst_channel_id: 目标频道(复制目标,shadow 或 R100)
            main_channel_id: message_backups 的 main_msg_id 对应的频道(通常是 active)
                             补位场景下可能不同于 src_channel_id
        Returns:
            成功复制的消息数。
        """
        # 1. 查 manifest:src 有但 dst 没有的文件
        try:
            missing = await store.get_missing_from_src(group_id, src_channel_id, dst_channel_id)
        except Exception as e:
            logger.warning(f"[Mon][manifest] 查询缺失文件失败 group={group_id} src={src_channel_id} dst={dst_channel_id}: {e}")
            return 0
        if not missing:
            return 0

        # 2. 按源 message_id 排序(保持顺序),批量 copy_messages
        # 注意:copy_messages 要求 message_ids 是同一频道的,这里源都是 src_channel_id
        missing.sort(key=lambda x: x["src_message_id"])

        # 媒体组感知分批:同一 media_group_id 的消息必须在同一个 copy_messages 调用中,
        # 否则 Telegram 会把一个相册拆成多个独立相册。
        # 策略:遍历排序后的列表,批次满 30 条时,若下一条与当前批次属同一媒体组则延展批次。
        batch_size = 30  # 每批最多 30 条,避免 FloodWait
        batches: list[list[dict]] = []
        cur_batch: list[dict] = []
        cur_mgids: set[str] = set()
        for item in missing:
            mgid = item.get("media_group_id") or ""
            if not cur_batch:
                cur_batch.append(item)
                if mgid:
                    cur_mgids.add(mgid)
                continue
            # 批次已满 且 下一条不属于当前批次任何媒体组 → 开新批次
            if len(cur_batch) >= batch_size and mgid not in cur_mgids:
                batches.append(cur_batch)
                cur_batch = [item]
                cur_mgids = {mgid} if mgid else set()
            else:
                cur_batch.append(item)
                if mgid:
                    cur_mgids.add(mgid)
        if cur_batch:
            batches.append(cur_batch)

        total_copied = 0
        for batch in batches:
            src_msg_ids = [item["src_message_id"] for item in batch]
            try:
                # 用 bot.copy_messages 批量复制(不带转发尾巴)
                copied_msgs = await bot_instance.copy_messages(
                    chat_id=dst_channel_id,
                    from_chat_id=src_channel_id,
                    message_ids=src_msg_ids,
                )
                # copy_messages 返回 List[MessageId],顺序与输入一致
                manifest_records = []
                backup_mappings = []
                for item, sent in zip(batch, copied_msgs):
                    sent_msg_id = sent.message_id
                    manifest_records.append({
                        "group_id": group_id,
                        "file_unique_id": item["file_unique_id"],
                        "channel_id": dst_channel_id,
                        "message_id": sent_msg_id,
                        "media_type": item.get("media_type", ""),
                        "media_group_id": item.get("media_group_id", ""),
                    })
                    # message_backups: main_msg_id 用 main_channel 的 msg_id
                    # 常规场景 main_channel_id == src_channel_id,直接用 src_message_id
                    # 补位场景 main_channel_id 可能不同,需查 manifest 获取 main_channel 的 msg_id
                    if main_channel_id == src_channel_id:
                        main_msg_id = item["src_message_id"]
                    else:
                        main_msg_id = await store.get_manifest_msg_id(
                            group_id, main_channel_id, item["file_unique_id"]
                        )
                    if main_msg_id:
                        backup_mappings.append((main_msg_id, sent_msg_id))

                # 3. 更新 manifest + message_backups
                try:
                    await store.upsert_manifest_batch(manifest_records)
                except Exception as e:
                    logger.warning(f"[Mon][manifest] 批量登记失败 dst={dst_channel_id}: {e}")

                if backup_mappings:
                    try:
                        await self._write_backup_mappings(main_channel_id, dst_channel_id, backup_mappings)
                    except Exception as e:
                        logger.warning(f"[Mon][manifest] message_backups 写入失败 dst={dst_channel_id}: {e}")

                total_copied += len(copied_msgs)
            except Exception as e:
                logger.warning(
                    f"[Mon][manifest] copy_messages 失败 src={src_channel_id} dst={dst_channel_id} "
                    f"batch_size={len(batch)}: {e}"
                )
                # 失败的批次下周期重试(幂等:manifest 未登记,get_missing_from_src 会再次返回)
                break

        return total_copied

    @staticmethod
    async def _write_backup_mappings(main_channel_id: int, backup_channel_id: int, mappings: list[tuple[int, int]]) -> bool:
        """将影子复制产生的 msg_id 映射写入 message_backups 表。
        故障切换时 dsp_bot 可据此查找影子频道中的新 msg_id。

        Returns:
            True 表示全部写入成功;False 表示失败,调用方不应推进游标。
        """
        try:
            from database.session import save_message_backup
            for main_msg_id, backed_msg_id in mappings:
                await save_message_backup(main_msg_id, backup_channel_id, backed_msg_id)
            return True
        except Exception as e:
            logger.warning(f"[Mon] 写入 message_backups 映射失败: {e}")
            return False

    async def auto_fill_new_channels(self, bot_instance, all_cells: list[dict]) -> int:
        """智能替补:检测新频道(manifest 中无记录),从 Active 槽位补齐所有存量文件。

        Manifest 驱动(免 Telethon):
        - 检测条件:该频道在 manifest 中无记录(新频道)
        - 从当前 Active 复制缺失文件到新频道
        - message_backups 的 main_msg_id 优先用组内 lost 频道(原 active)的 msg_id
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
            group_id = int(_group_key)
            a_slot, s1_slot, s2_slot = group

            # 找组内 lost 状态的槽位(原 active,补位时 main_msg_id 用它的 msg_id)
            lost_slot = next(
                (s for s in group if s and s.get("status") == "lost" and not s.get("is_r100")),
                None,
            )
            # main_channel_id:优先用 lost(原 active),否则用当前 active
            main_channel_id = lost_slot["channel_id"] if lost_slot else active_channel

            for shadow_slot in [s1_slot, s2_slot]:
                if not shadow_slot:
                    continue
                if shadow_slot["slot_id"] == active_slot["slot_id"]:
                    continue
                if shadow_slot.get("status") not in ("shadow1", "shadow2"):
                    continue

                shadow_channel = shadow_slot["channel_id"]
                # 检测新频道:manifest 中无记录
                try:
                    has_manifest = await store.has_manifest_for_channel(group_id, shadow_channel)
                except Exception:
                    has_manifest = True  # 查询失败不触发补齐,避免误判
                if has_manifest:
                    continue  # 已有记录,非新频道

                # 从 active 复制缺失文件到新 shadow
                filled = await self._copy_missing_via_manifest(
                    bot_instance, store, group_id,
                    src_channel_id=active_channel,
                    dst_channel_id=shadow_channel,
                    main_channel_id=main_channel_id,
                )
                if filled > 0:
                    total_filled += filled
                    # 标记 last_synced_msg_id 为非 0,表示已补齐(兼容旧逻辑)
                    await store.update_cell_fields_local(
                        shadow_slot["slot_id"], {"last_synced_msg_id": 1}
                    )
                    shadow_slot["last_synced_msg_id"] = 1
                    logger.info(
                        f"[Mon][填充] {shadow_slot['slot_id']} "
                        f"新频道补齐 {filled} 条文件 (main_channel={main_channel_id})"
                    )

        return total_filled

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