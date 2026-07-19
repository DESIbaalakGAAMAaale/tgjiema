"""Mon 调度器:心跳检测、自动降级、环形推进、智能补齐"""
from __future__ import annotations

import os
import re
import time
import asyncio
import datetime as _dt

import yaml
from loguru import logger
from database import (
    log_rotate,
)
from services.i18n import translate as _i18n_t


# ─── R36 B0-3: replication_tasks 安全包装(异常不传播到主流程) ───
# 设计参考 up_bot.py 的 _create_replication_task_safe 系列:
# 所有 replication_tasks 写入失败只记录 warning,不影响复制主流程的兼容降级。

async def _create_replication_task_safe(
    store, group_id: int, file_unique_id: str, src_channel_id: int,
    dst_channel_id: int, src_msg_id: int, media_group_id: str = "",
) -> int:
    """安全创建 replication_task(PLANNED),返回 task_id(0 表示失败)。

    异常安全: 失败只记录 warning,不传播到主流程。
    """
    if not file_unique_id or not group_id or not store:
        return 0
    try:
        return await store.create_replication_task(
            group_id, file_unique_id, src_channel_id, dst_channel_id,
            src_msg_id, media_group_id=media_group_id,
        )
    except Exception as e:
        logger.warning(
            f"[Mon][repl] 创建 replication_task 失败(不影响主流程, "
            f"fuid={file_unique_id}): {e}"
        )
        return None


async def _mark_replication_copying_safe(store, task_id: int) -> bool:
    """安全 CAS claim: PLANNED → COPYING。返回 claim 是否成功。"""
    if not task_id or not store:
        return False
    try:
        return await store.mark_replication_copying(task_id)
    except Exception as e:
        logger.warning(
            f"[Mon][repl] 标记 COPYING 失败(不影响主流程, task_id={task_id}): {e}"
        )
        return False


async def _mark_replication_copied_safe(store, task_id: int, dst_msg_id: int) -> bool:
    """安全标记 COPIED_UNVERIFIED(写入 dst_msg_id)。"""
    if not task_id or not store:
        return False
    try:
        return await store.mark_replication_copied(task_id, dst_msg_id)
    except Exception as e:
        logger.warning(
            f"[Mon][repl] 标记 COPIED_UNVERIFIED 失败(不影响主流程, "
            f"task_id={task_id}): {e}"
        )
        return False


async def _mark_replication_failed_safe(store, task_id: int, reason: str) -> None:
    """安全标记 FAILED/PLANNED(重试)。"""
    if not task_id or not store:
        return
    try:
        await store.mark_replication_failed(task_id, reason)
    except Exception as e:
        logger.warning(
            f"[Mon][repl] 标记 FAILED 失败(不影响主流程, task_id={task_id}): {e}"
        )


async def _commit_replication_transaction_safe(
    store, task_id: int,
    manifest_records: list[dict] | None = None,
    backup_mappings: list[tuple[int, int]] | None = None,
    backup_channel_id: int | None = None,
) -> bool:
    """安全原子提交: Manifest + message_backups + COMMITTED 同事务。"""
    if not task_id or not store:
        return False
    try:
        return await store.commit_replication_transaction(
            task_id, manifest_records, backup_mappings, backup_channel_id,
        )
    except Exception as e:
        logger.warning(
            f"[Mon][repl] 原子提交失败(不影响主流程, task_id={task_id}): {e}"
        )
        return False


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
                    _i18n_t('services.mon.scheduler.s4', slot_id=slot_id, fail_streak=fail_streak)
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
                    _i18n_t('services.mon.scheduler.s5', slot_id=slot_id, fail_streak=fail_streak)
                )
                continue

            # 已确认: 执行降级
            promote_slot, cascade_slot = self._get_next_promotable(group)
            from_status = active_slot.get("status", "?")  # 在 _degrade_group 修改前捕获
            await self._degrade_group(active_slot, promote_slot, cascade_slot, fail_streak, all_cells)
            cell_suspicious.pop(slot_id, None)  # 降级后清除疑似标记
            alerts.append(
                _i18n_t('services.mon.scheduler.s1', slot_id=slot_id, from_status=from_status, promote_slot_slot_id_if_promote_slot_else_none=promote_slot['slot_id'] if promote_slot else 'none', cascade_slot_slot_id_if_cascade_slot_else_none=cascade_slot['slot_id'] if cascade_slot else 'none', fail_streak=fail_streak)
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

        R36 B0-3: replication_task-first 控制面。
        每个调度周期开始时先做:
        1. 重置超时的 COPYING 任务 → PLANNED(worker 崩溃恢复)
        2. 对账 COPIED_UNVERIFIED 任务(优先对账,不重新 copy)

        然后进入 task-first 复制主循环:
        - Up Bot 写入 Active 时已登记 manifest
        - Mon Bot 查 manifest 获取"Active 有但 Shadow/R100 没有"的文件
        - 为每个缺失文件创建/查询 replication_task(PLANNED)
        - CAS claim → COPYING → Telegram copy → COPIED_UNVERIFIED → 原子提交 COMMITTED
        返回复制的消息总数。

        Args:
            all_cells: 由调用方统一查询传入。
        """
        self._replicate_count += 1
        groups = self._group_slots(all_cells)
        total_copied = 0
        from database.cache_store import get_cache_store
        store = get_cache_store()

        # R36 B0-3: 周期开始时先做恢复对账
        # 1. 重置超时的 COPYING 任务(lease 过期,worker 可能已崩溃)
        try:
            reset_count = await store.reset_stale_copying_tasks(
                lease_timeout_seconds=600,
            )
            if reset_count > 0:
                logger.info(
                    f"[Mon][reconcile] 重置 {reset_count} 个超时 COPYING 任务回 PLANNED"
                )
        except Exception as e:
            logger.warning(f"[Mon][reconcile] 重置超时 COPYING 任务异常: {e}")

        # 2. 对账 COPIED_UNVERIFIED 任务(优先对账,不重新 copy)
        try:
            reconciled = await self._reconcile_copied_unverified(store)
            if reconciled > 0:
                logger.info(
                    f"[Mon][reconcile] 本轮对账推进 {reconciled} 个 COPIED_UNVERIFIED → COMMITTED"
                )
        except Exception as e:
            logger.warning(f"[Mon][reconcile] 对账 COPIED_UNVERIFIED 异常: {e}")

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

            # 1. R100 全量归档(task-first 驱动)
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

            # 2. 复制到同组的 shadow1/shadow2(task-first 驱动)
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
        """R36 B0-3: replication_task-first 副本同步。

        流程(每条缺失文件):
        1. INSERT OR IGNORE replication_task(PLANNED, 唯一业务键)
        2. 查询任务状态:
           - COMMITTED: 已完成,跳过
           - COPYING / COPIED_UNVERIFIED: 在途,跳过(由 _reconcile_copied_unverified 处理)
           - PLANNED: 进入 claim 流程
        3. CAS claim → COPYING(mark_replication_copying)
        4. Telegram copy_messages
        5. 成功: mark_replication_copied → COPIED_UNVERIFIED(写入 dst_msg_id)
        6. 原子提交: commit_replication_transaction(Manifest + message_backups + COMMITTED 同事务)
        7. 失败: mark_replication_failed(task_id, reason)

        媒体组感知:同一 media_group_id 的所有成员必须全部 PLANNED 才能 claim,
        否则整组跳过(避免拆散相册)。COPIED_UNVERIFIED 的成员由对账恢复推进。

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
            return None
        if not missing:
            return 0

        # 2. 按源 message_id 排序(保持顺序)
        missing.sort(key=lambda x: x["src_message_id"])

        # 3. R36 B0-3: 为每个缺失文件 INSERT OR IGNORE replication_task(PLANNED)
        #    并查询当前状态,过滤出 PLANNED 的文件进入 claim 流程
        #    COMMITTED/COPYING/COPIED_UNVERIFIED 的任务跳过(避免重复 copy)
        plan_items: list[dict] = []  # 仅 PLANNED 状态的文件进入复制流程
        task_id_map: dict[str, int] = {}  # file_unique_id → task_id
        skipped_inflight = 0
        skipped_committed = 0
        for item in missing:
            fuid = item["file_unique_id"]
            mgid = item.get("media_group_id") or ""
            # INSERT OR IGNORE 幂等创建任务
            task_id = await _create_replication_task_safe(
                store, group_id, fuid, src_channel_id, dst_channel_id,
                item["src_message_id"], media_group_id=mgid,
            )
            if task_id == 0:
                # R37 P1-1: task 创建失败必须 fail-closed(不可直接 copy)
                # 原实现降级到无 task 复制(fail-open),绕过 task-first 可恢复性保证,
                # 一旦复制成功但 manifest 登记失败将无法对账回退。
                # 现在:跳过该条目并告警,由 reconcile worker 在 task 表恢复后重试。
                logger.error(
                    f"[Mon][repl] R37 P1-1: task 创建失败,fuid={fuid},"
                    f"跳过复制(fail-closed,等待对账 worker 恢复 task 后重试)"
                )
                continue
            task_id_map[fuid] = task_id
            # 查询任务当前状态决定是否进入 claim 流程
            try:
                task = await store.get_replication_task_by_unique_key(
                    group_id, fuid, src_channel_id, dst_channel_id,
                )
            except Exception as e:
                logger.warning(
                    f"[Mon][repl] 查询 task 状态失败 task_id={task_id},按 PLANNED 处理: {e}"
                )
                task = None
            status = (task or {}).get("status", "PLANNED")
            if status == "COMMITTED":
                skipped_committed += 1
                continue
            if status in ("COPYING", "COPIED_UNVERIFIED"):
                # 在途任务交给 _reconcile_copied_unverified 推进
                skipped_inflight += 1
                continue
            # PLANNED 或 FAILED-已回退-PLANNED → 进入 claim 流程
            plan_items.append(item)

        if skipped_committed > 0 or skipped_inflight > 0:
            logger.info(
                f"[Mon][repl] group={group_id} src={src_channel_id} dst={dst_channel_id} "
                f"missing={len(missing)} planned={len(plan_items)} "
                f"skipped_committed={skipped_committed} skipped_inflight={skipped_inflight}"
            )
        if not plan_items:
            # 全部已完成或在途,无需 copy
            return 0

        # 4. 媒体组感知分批:同一 media_group_id 必须在同一个 copy_messages 调用
        #    否则 Telegram 会把一个相册拆成多个独立相册。
        #    策略:遍历排序后的列表,批次满 30 条时,若下一条与当前批次属同一媒体组则延展批次。
        batch_size = 30
        batches: list[list[dict]] = []
        cur_batch: list[dict] = []
        cur_mgids: set[str] = set()
        for item in plan_items:
            mgid = item.get("media_group_id") or ""
            if not cur_batch:
                cur_batch.append(item)
                if mgid:
                    cur_mgids.add(mgid)
                continue
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
            # 4a. R36 B0-3: 媒体组完整性检查
            #     若 batch 含媒体组成员,必须所有成员的 task 都是 PLANNED 才能 claim
            #     (前面已过滤掉非 PLANNED 的,这里再做一次保险)
            batch_mgids = {item.get("media_group_id") or "" for item in batch}
            has_media_group = any(mgid for mgid in batch_mgids)

            # 4b. CAS claim 每个 task: PLANNED → COPYING
            #     任一 claim 失败(被其他 worker 抢占)→ 整批跳过(避免媒体组拆散)
            claimed_task_ids: list[tuple[int, dict]] = []
            claim_failed = False
            for item in batch:
                fuid = item["file_unique_id"]
                task_id = task_id_map.get(fuid, 0)
                if not task_id:
                    claim_failed = True
                    break
                ok = await _mark_replication_copying_safe(store, task_id)
                if not ok:
                    # 被其他 worker 抢占或状态已变 → 跳过整批(媒体组不能拆散)
                    claim_failed = True
                    break
                claimed_task_ids.append((task_id, item))

            if claim_failed:
                # 已 claim 的 task 回退为 FAILED(让下轮重试)
                # 但不 mark_replication_failed(因为只是 claim 冲突,不是真失败)
                # 改为:已 claim 的保留 COPYING,下轮 _reconcile 通过 lease 超时回退
                # 这里只跳过本批次,不破坏已 claim 的 task
                if has_media_group:
                    logger.info(
                        f"[Mon][repl] 媒体组 claim 部分失败,整批跳过 "
                        f"src={src_channel_id} dst={dst_channel_id} "
                        f"mgids={batch_mgids}"
                    )
                    continue
                # 非媒体组:仅跳过失败的项,保留已 claim 的继续 copy
                if not claimed_task_ids:
                    continue

            # 4c. 取已成功 claim 的子集作为本次 copy 的输入
            #     (claim 失败时,claimed_task_ids 可能是 batch 的子集)
            effective_batch = [item for (_tid, item) in claimed_task_ids]
            effective_task_ids = [tid for (tid, _item) in claimed_task_ids]
            if not effective_batch:
                continue
            src_msg_ids = [item["src_message_id"] for item in effective_batch]

            # 5. Telegram copy_messages(FloodWait 重试)
            copied_msgs = None
            for attempt in range(3):
                try:
                    copied_msgs = await bot_instance.copy_messages(
                        chat_id=dst_channel_id,
                        from_chat_id=src_channel_id,
                        message_ids=src_msg_ids,
                    )
                    break
                except Exception as e:
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after is None and "Too Many Requests" in str(e):
                        try:
                            retry_after = float(str(e).split("retry in")[-1].split("seconds")[0].strip())
                        except Exception:
                            retry_after = 30
                    if retry_after and attempt < 2:
                        logger.warning(
                            f"[Mon][manifest] FloodWait {retry_after}s,等待后重试(attempt={attempt+1}) "
                            f"src={src_channel_id} dst={dst_channel_id}"
                        )
                        await asyncio.sleep(retry_after + 1)
                        continue
                    logger.warning(
                        f"[Mon][manifest] copy_messages 失败 src={src_channel_id} dst={dst_channel_id} "
                        f"batch_size={len(effective_batch)}: {e}"
                    )
                    break

            if copied_msgs is None:
                # copy 失败:把所有已 claim 的 task 标记 FAILED(可重试)
                for tid in effective_task_ids:
                    await _mark_replication_failed_safe(
                        store, tid, f"copy_messages_failed: src={src_channel_id} dst={dst_channel_id}"
                    )
                # 失败的批次终止后续批次(避免 FloodWait 连锁)
                break

            # 6. R36 B0-3: 逐个 task 推进状态机
            #    copy_messages 返回 List[MessageId],顺序与输入一致
            try:
                for (task_id, item), sent in zip(claimed_task_ids, copied_msgs):
                    sent_msg_id = sent.message_id
                    # 6a. 标记 COPIED_UNVERIFIED(写入 dst_msg_id)
                    #     失败不阻断后续,留给 _reconcile_copied_unverified 推进
                    ok = await _mark_replication_copied_safe(store, task_id, sent_msg_id)
                    if not ok:
                        logger.warning(
                            f"[Mon][repl] 标记 COPIED_UNVERIFIED 失败 task_id={task_id},"
                            f"由对账恢复处理"
                        )
                        total_copied += 1
                        continue

                    # 6b. R36 B0-3: 原子提交(Manifest + message_backups + COMMITTED 同事务)
                    manifest_record = {
                        "group_id": group_id,
                        "file_unique_id": item["file_unique_id"],
                        "channel_id": dst_channel_id,
                        "message_id": sent_msg_id,
                        "media_type": item.get("media_type", ""),
                        "media_group_id": item.get("media_group_id", ""),
                    }
                    # message_backups 的 main_msg_id:
                    #   常规场景 main_channel_id == src_channel_id,直接用 src_message_id
                    #   补位场景 main_channel_id 可能不同,需查 manifest 获取 main_channel 的 msg_id
                    if main_channel_id == src_channel_id:
                        main_msg_id = item["src_message_id"]
                    else:
                        try:
                            main_msg_id = await store.get_manifest_msg_id(
                                group_id, main_channel_id, item["file_unique_id"]
                            )
                        except Exception as e:
                            logger.warning(
                                f"[Mon][repl] 查询 main_msg_id 失败 fuid={item['file_unique_id']}: {e}"
                            )
                            main_msg_id = None
                    backup_mappings = (
                        [(main_msg_id, sent_msg_id)] if main_msg_id else None
                    )

                    committed = await _commit_replication_transaction_safe(
                        store, task_id,
                        manifest_records=[manifest_record],
                        backup_mappings=backup_mappings,
                        backup_channel_id=dst_channel_id,
                    )
                    if not committed:
                        # 原子提交失败:task 仍停留在 COPIED_UNVERIFIED
                        # 由 _reconcile_copied_unverified 在下轮重试提交
                        logger.warning(
                            f"[Mon][repl] 原子提交失败 task_id={task_id},"
                            f"由对账恢复重试"
                        )
                    total_copied += 1
            except Exception as e:
                logger.warning(
                    f"[Mon][manifest] 复制后处理异常 dst={dst_channel_id}: {e}"
                )
                # 已 copy 但状态机推进失败,task 留在 COPIED_UNVERIFIED 等待对账
                break

        return total_copied

    async def _reconcile_copied_unverified(self, store, max_tasks: int = 100) -> int:
        """R36 B0-3: COPIED_UNVERIFIED 对账恢复。

        扫描所有 COPIED_UNVERIFIED 状态的 replication_task,优先对账而不重新 copy:

        1. 检查 manifest 是否已写入(可能 worker 在 commit_replication_transaction
           成功写入 manifest 但状态机推进前崩溃)
           - 已写入 → mark_replication_committed → COMMITTED
        2. 未写入 → 用已存的 dst_msg_id 重新写 manifest + message_backups
           + COMMITTED(不重新 copy)
        3. 任务超时(updated_at 早于 now - reconcile_timeout)且仍 COPIED_UNVERIFIED
           → mark_replication_failed 让下轮 worker 重新创建 task

        Args:
            store: CacheStore 实例
            max_tasks: 单次最多处理任务数

        Returns:
            完成对账的任务数(COMMITTED 推进数)。
        """
        if not store:
            return 0
        try:
            tasks = await store.get_copied_unverified_tasks(limit=max_tasks)
        except Exception as e:
            logger.warning(f"[Mon][reconcile] 查询 COPIED_UNVERIFIED 任务失败: {e}")
            return None
        if not tasks:
            return 0

        reconciled = 0
        now = time.time()
        reconcile_timeout = 3600  # 1 小时仍未对账完成则放弃,转 FAILED

        for task in tasks:
            task_id = task["task_id"]
            group_id = task["group_id"]
            fuid = task["file_unique_id"]
            dst_channel_id = task["dst_channel_id"]
            src_channel_id = task["src_channel_id"]
            src_msg_id = task["src_msg_id"]
            dst_msg_id = task.get("dst_msg_id")
            media_group_id = task.get("media_group_id") or ""
            media_type = ""  # task 表不存 media_type,从 manifest 查
            updated_at = task.get("updated_at") or now

            # 超时检测:任务停留在 COPIED_UNVERIFIED 过久 → 标记 FAILED 重试
            if now - updated_at > reconcile_timeout:
                # R37 P1-2: 重试前做二次探测,避免产生重复副本/孤儿消息
                # 若 dst_msg_id 仍存在,说明 copy 实际已成功(只是状态机未推进),
                # 应再尝试一次 manifest + COMMITTED 写入,不重新 copy。
                # 只有 dst_msg_id 也丢失时,才允许标记 FAILED 让下轮重新 copy
                # (此时确实无法判断 copy 是否成功,但这是少数边界场景)。
                if dst_msg_id:
                    manifest_record = {
                        "group_id": group_id,
                        "file_unique_id": fuid,
                        "channel_id": dst_channel_id,
                        "message_id": dst_msg_id,
                        "media_type": media_type,
                        "media_group_id": media_group_id,
                    }
                    backup_mappings = (
                        [(src_msg_id, dst_msg_id)] if src_msg_id else None
                    )
                    committed = await _commit_replication_transaction_safe(
                        store, task_id,
                        manifest_records=[manifest_record],
                        backup_mappings=backup_mappings,
                        backup_channel_id=dst_channel_id,
                    )
                    if committed:
                        reconciled += 1
                        logger.info(
                            f"[Mon][reconcile] R37 P1-2: task_id={task_id} "
                            f"超时但 dst_msg_id 存在,二次写入 manifest 成功,"
                            f"推进 COMMITTED(避免重复 copy)"
                        )
                        continue
                    logger.warning(
                        f"[Mon][reconcile] R37 P1-2: task_id={task_id} "
                        f"超时且二次写入 manifest 失败,标记 FAILED 重试"
                    )
                else:
                    logger.warning(
                        f"[Mon][reconcile] task_id={task_id} COPIED_UNVERIFIED 超时 "
                        f"且 dst_msg_id 缺失(无法二次探测,允许重 copy)"
                    )
                await _mark_replication_failed_safe(
                    store, task_id, "reconcile_timeout_copied_unverified"
                )
                continue

            if not dst_msg_id:
                # 缺少 dst_msg_id 说明 mark_replication_copied 未完成,
                # 但任务在 COPIED_UNVERIFIED 状态说明 copy 已成功(只是 dst_msg_id 未落库)
                # 这种情况无法对账,直接标记 FAILED
                logger.warning(
                    f"[Mon][reconcile] task_id={task_id} 缺少 dst_msg_id,标记 FAILED"
                )
                await _mark_replication_failed_safe(
                    store, task_id, "reconcile_missing_dst_msg_id"
                )
                continue

            # 1. 检查 manifest 是否已写入
            try:
                manifest_msg_id = await store.get_manifest_msg_id(
                    group_id, dst_channel_id, fuid,
                )
            except Exception as e:
                logger.warning(
                    f"[Mon][reconcile] 查询 manifest 失败 task_id={task_id}: {e}"
                )
                continue

            if manifest_msg_id:
                # 1a. manifest 已写入 → 推进 COMMITTED
                try:
                    ok = await store.mark_replication_committed(task_id)
                    if ok:
                        reconciled += 1
                        logger.info(
                            f"[Mon][reconcile] task_id={task_id} manifest 已存在,"
                            f"推进 COMMITTED"
                        )
                except Exception as e:
                    logger.warning(
                        f"[Mon][reconcile] mark_replication_committed 失败 "
                        f"task_id={task_id}: {e}"
                    )
                continue

            # 2. manifest 未写入 → 用已存的 dst_msg_id 重新写
            #    (不重新 copy,使用 task 表中保留的 dst_msg_id)
            manifest_record = {
                "group_id": group_id,
                "file_unique_id": fuid,
                "channel_id": dst_channel_id,
                "message_id": dst_msg_id,
                "media_type": media_type,
                "media_group_id": media_group_id,
            }
            # message_backups 的 main_msg_id:优先用 src_msg_id(常规场景)
            # 补位场景无法在此判断 main_channel,退化为不写 backup_mapping
            backup_mappings = [(src_msg_id, dst_msg_id)] if src_msg_id else None

            try:
                ok = await store.commit_replication_transaction(
                    task_id,
                    manifest_records=[manifest_record],
                    backup_mappings=backup_mappings,
                    backup_channel_id=dst_channel_id,
                )
                if ok:
                    reconciled += 1
                    logger.info(
                        f"[Mon][reconcile] task_id={task_id} 重新写入 manifest 成功,"
                        f"推进 COMMITTED"
                    )
                else:
                    logger.warning(
                        f"[Mon][reconcile] task_id={task_id} 原子提交失败,"
                        f"下轮重试"
                    )
            except Exception as e:
                logger.warning(
                    f"[Mon][reconcile] task_id={task_id} 对账提交异常: {e}"
                )

        if reconciled > 0:
            logger.info(
                f"[Mon][reconcile] 本轮对账完成 {reconciled}/{len(tasks)} 个 task"
            )
        return reconciled

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
            issues.append(_i18n_t('services.mon.scheduler.s2'))
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
                    _i18n_t('services.mon.scheduler.s6', c_slot_id=c['slot_id'], nxt=nxt)
                )

        # 2. 重复指针
        for nxt, sources in next_reverse.items():
            if len(sources) > 1:
                slot_names = [
                    c["slot_id"] for c in active_cells
                    if c["channel_id"] in sources
                ]
                issues.append(
                    _i18n_t('services.mon.scheduler.s7', nxt=nxt, slot_names=slot_names)
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
                _i18n_t('services.mon.scheduler.s3', slot_names=slot_names)
            )

        # 4. 三元组完整性
        groups = self._group_slots(all_cells)
        for group_key, (a_slot, s1_slot, s2_slot) in groups.items():
            missing = []
            if not a_slot:
                missing.append(_i18n_t('services.mon.scheduler.s8'))
            if not s1_slot:
                missing.append("Shadow1")
            if not s2_slot:
                missing.append("Shadow2")
            if missing:
                issues.append(
                    _i18n_t('services.mon.scheduler.s9', group_key=group_key, join_missing=', '.join(missing))
                )

        return issues