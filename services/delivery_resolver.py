"""Replica-aware Delivery Resolver — M2 控制面收敛

按 file_unique_id 查询 Manifest 的实际存活副本,结合 cells 状态与频道健康评分
选择真实存在该文件的频道。只有 Manifest 不可用时才降级到 message_backups。

核心改进(相对 storage/delivery_resolver.py 的拓扑级解析):
1. 不再把"拓扑可用"误等同于"该文件存在" —— 以 Manifest 副本为准
2. 按 cells 状态(active > shadow1 > shadow2)分级
3. 频道健康评分(5 分钟失败窗口)驱动同级别内排序
4. 跨组 fallback 仅在 Manifest 确认有副本时才考虑
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    # 仅类型检查时导入,避免运行时重依赖(测试环境可能用 mock 占位 cache_store)
    from database.cache_store import CacheStore


# cells status → 排序权重(active 最高,shadow2 最低,其余兜底)
_STATUS_PRIORITY: dict[str, int] = {
    "active": 0,
    "shadow1": 1,
    "shadow2": 2,
}
_DEFAULT_PRIORITY = 3  # lost / unknown / direct 等未识别状态


class ReplicaAwareResolver:
    """Replica-aware delivery resolver.

    按 file_unique_id 查询 Manifest 的实际存活副本,
    结合 cells 状态和频道健康评分选择最佳频道。
    """

    def __init__(self, store: "CacheStore"):
        self._store = store
        # channel_id -> [失败时间戳列表]
        self._channel_failures: dict[int, list[float]] = {}
        self._failure_window = 300  # 5 分钟窗口
        # 最近一次解析的 group_id,供 verify_replica_exists 复用(减少重复查询)
        self._context_group_id: int | None = None
        # 最近一次查询到的副本缓存,供 verify_replica_exists 零查询校验
        self._last_replicas: list[dict] = []

    # ─── 核心解析 ───────────────────────────────────────────────

    async def resolve_channel_for_file(
        self,
        file_unique_id: str,
        group_id: int,
        preferred_channels: list[int] | None = None,
        exclude_channels: set[int] | None = None,
        fail_closed: bool = True,
    ) -> tuple[int, int] | None:
        """为指定文件选择最佳频道和消息 ID。

        返回 (channel_id, message_id) 或 None(无可用副本)。

        算法:
        1. 查询 manifest 获取该 file_unique_id 的所有副本(精确索引)
        2. 过滤掉 exclude_channels
        3. 按 cells 状态过滤(active > shadow1 > shadow2)
        4. 按 channel_health 评分排序(失败率低优先)
        5. 如果 preferred_channels 中有匹配,优先返回
        6. 返回最佳频道 + 消息 ID

        Args:
            fail_closed: True(默认, 商用安全) 时查询失败返回 None;
                         False(兼容) 时查询失败降级到乐观放行(已弃用, 不推荐)
        """
        replicas = await self.get_available_replicas(
            file_unique_id, group_id, fail_closed=fail_closed
        )
        if not replicas:
            logger.info(
                f"[replica-resolver] 无可用副本 (fuid={file_unique_id}, group={group_id})"
            )
            return None

        exclude = exclude_channels or set()
        candidates = [r for r in replicas if r["channel_id"] not in exclude]
        if not candidates:
            logger.info(
                f"[replica-resolver] 所有副本被 exclude (fuid={file_unique_id})"
            )
            return None

        # 按 cells 状态 + 健康评分排序
        ranked = await self._rank_by_status_and_health(candidates)

        # preferred_channels 优先:在排序列表中找到第一个属于 preferred 的频道
        if preferred_channels:
            pref_set = set(preferred_channels)
            for r in ranked:
                if r["channel_id"] in pref_set:
                    logger.debug(
                        f"[replica-resolver] 命中 preferred "
                        f"(ch={r['channel_id']}, fuid={file_unique_id})"
                    )
                    return (r["channel_id"], r["message_id"])

        # 返回最佳频道
        best = ranked[0]
        logger.debug(
            f"[replica-resolver] 选定 "
            f"(ch={best['channel_id']}, msg={best['message_id']}, fuid={file_unique_id})"
        )
        return (best["channel_id"], best["message_id"])

    async def resolve_channels_for_media_group(
        self,
        media_group_id: str,
        group_id: int,
        preferred_channels: list[int] | None = None,
        exclude_channels: set[int] | None = None,
        fail_closed: bool = True,
    ) -> list[tuple[int, int]] | None:
        """为媒体组选择频道(所有文件必须在同一频道)。

        返回 [(channel_id, message_id), ...] 或 None。
        算法: 找到拥有完整媒体组的频道,优先返回。

        Args:
            fail_closed: True(默认, 商用安全) 时查询失败返回 None;
                         False(兼容) 时查询失败降级到乐观放行(不推荐)
    """
        if not media_group_id:
            return None

        try:
            # 媒体组涉及多个 file_unique_id,必须拉整组 Manifest 过滤
            records = await self._store.get_manifest_by_group(group_id)
        except Exception as e:
            logger.warning(
                f"[replica-resolver] manifest 查询异常 "
                f"(group={group_id}, mgid={media_group_id}, fail_closed={fail_closed}): {e}"
            )
            # fail_closed=True 时返回 None(拒绝投递);False 时返回 None 仍表示无副本
            # (此处与 fail_closed 语义一致:查询失败即视为无可用副本)
            return None

        # 过滤出该 media_group_id 的所有记录
        mg_records = [r for r in records if r.get("media_group_id") == media_group_id]
        if not mg_records:
            logger.info(
                f"[replica-resolver] manifest 无此媒体组 "
                f"(group={group_id}, mgid={media_group_id})"
            )
            return None

        # 媒体组应包含的全部 file_unique_id 集合
        all_fuids = {r["file_unique_id"] for r in mg_records}

        # 按 channel_id 分组
        by_channel: dict[int, list[dict]] = {}
        for r in mg_records:
            by_channel.setdefault(r["channel_id"], []).append(r)

        # 筛选拥有完整媒体组的频道(未被 exclude)
        exclude = exclude_channels or set()
        complete: list[tuple[int, list[dict]]] = [
            (ch, recs)
            for ch, recs in by_channel.items()
            if ch not in exclude and {r["file_unique_id"] for r in recs} == all_fuids
        ]
        if not complete:
            logger.info(
                f"[replica-resolver] 无频道拥有完整媒体组 (mgid={media_group_id})"
            )
            return None

        # 构造 candidate 列表用于排序(message_id 取该频道最小值仅作占位)
        candidates = [
            {
                "channel_id": ch,
                "message_id": min(r["message_id"] for r in recs),
                "media_type": recs[0].get("media_type", ""),
            }
            for ch, recs in complete
        ]
        ranked = await self._rank_by_status_and_health(candidates)

        # 更新上下文(供 verify_replica_exists 复用)
        self._context_group_id = group_id
        self._last_replicas = [
            {
                "channel_id": r["channel_id"],
                "message_id": r["message_id"],
                "media_type": r.get("media_type", ""),
                "media_group_id": r.get("media_group_id", ""),
            }
            for r in mg_records
        ]

        # preferred_channels 优先
        if preferred_channels:
            pref_set = set(preferred_channels)
            for c in ranked:
                if c["channel_id"] in pref_set:
                    return [
                        (c["channel_id"], r["message_id"])
                        for r in by_channel[c["channel_id"]]
                    ]

        # 返回最佳频道的全部消息
        best = ranked[0]
        return [
            (best["channel_id"], r["message_id"])
            for r in by_channel[best["channel_id"]]
        ]

    # ─── 副本查询 ───────────────────────────────────────────────

    async def get_available_replicas(
        self, file_unique_id: str, group_id: int, fail_closed: bool = True,
    ) -> list[dict]:
        """查询文件的所有存活副本(从 manifest, 精确索引)。

        返回 [{"channel_id": ..., "message_id": ..., "media_type": ...}, ...]

        P2-4: 改用 get_manifest_by_file_unique_id 精确索引查询,
        不再每次拉整组 Manifest。

        降级策略:
        - Manifest 查询异常时:
          - fail_closed=True (默认, 商用): 返回空列表(拒绝投递)
          - fail_closed=False (兼容, 不推荐): 返回空列表(由调用方 fallback)
        - 查询成功但无副本: 返回空列表(无论 fail_closed)
        message_backups 按 main_msg_id 索引,无法仅凭 file_unique_id 查询,
        故此处不触发;由调用方在持有 main_msg_id 时另行查询。
        """
        if not file_unique_id:
            return []

        try:
            # P2-4: 精确索引查询,不再拉整组 Manifest
            records = await self._store.get_manifest_by_file_unique_id(
                file_unique_id, group_id
            )
        except Exception as e:
            logger.warning(
                f"[replica-resolver] manifest 查询异常 "
                f"(group={group_id}, fuid={file_unique_id}, fail_closed={fail_closed}): {e}"
            )
            # fail_closed=True: 返回空列表(拒绝投递)
            # fail_closed=False: 同样返回空列表(行为一致, 但调用方可能 fallback 到拓扑解析)
            return []

        # 精确查询返回的即为该 file_unique_id 的副本,无需客户端过滤
        replicas = [
            {
                "channel_id": r["channel_id"],
                "message_id": r["message_id"],
                "media_type": r.get("media_type", ""),
                "media_group_id": r.get("media_group_id", ""),
            }
            for r in records
        ]

        # 记录上下文(供 verify_replica_exists 零查询复用)
        self._context_group_id = group_id
        self._last_replicas = replicas

        if not replicas:
            logger.info(
                f"[replica-resolver] manifest 无此文件副本 "
                f"(group={group_id}, fuid={file_unique_id});"
                f"message_backups 降级需 main_msg_id 上下文,当前不可用"
            )
        return replicas

    async def verify_replica_exists(
        self, channel_id: int, message_id: int, fail_closed: bool = True,
    ) -> bool:
        """验证副本是否真实存在(Manifest 中有记录)。

        注意:此方法只检查 Manifest 记录,不实际调用 Telegram API。
        实际的 Telegram copyMessage 失败由调用方处理。

        Args:
            fail_closed: True(默认, 商用安全) 时查询失败/无上下文返回 False(拒绝);
                         False(兼容, 不推荐) 时查询失败乐观放行(由调用方 Telegram API 兜底)
        """
        # 1. 优先复用最近一次查询的副本缓存(零查询)
        for r in self._last_replicas:
            if r["channel_id"] == channel_id and r["message_id"] == message_id:
                return True

        # 2. 回退到 manifest 查询(需要 group 上下文)
        group_id = self._context_group_id
        if group_id is None:
            if fail_closed:
                logger.warning(
                    f"[replica-resolver] verify 无 group 上下文(fail-closed),拒绝 "
                    f"(ch={channel_id}, msg={message_id})"
                )
                return False
            logger.debug(
                f"[replica-resolver] verify 无 group 上下文(fail-open),乐观放行 "
                f"(ch={channel_id}, msg={message_id})"
            )
            # 无上下文时乐观放行,由调用方 Telegram API 兜底校验
            return True

        try:
            # verify 需按 (channel_id, message_id) 过滤,无法用 file_unique_id 精确索引
            # 故仍用 get_manifest_by_group(整组查询);失败时按 fail_closed 决定
            records = await self._store.get_manifest_by_group(group_id)
        except Exception as e:
            logger.warning(
                f"[replica-resolver] verify manifest 查询异常 "
                f"(group={group_id}, fail_closed={fail_closed}): {e}"
            )
            if fail_closed:
                return False  # 查询失败时拒绝,由调用方处理
            return True  # 兼容:查询失败时乐观放行,由调用方 Telegram API 兜底

        for r in records:
            if r["channel_id"] == channel_id and r["message_id"] == message_id:
                return True
        return False

    # ─── 频道健康评分 ────────────────────────────────────────────

    def record_channel_failure(self, channel_id: int):
        """记录频道失败(用于健康评分)。

        追加当前时间戳到失败列表,同时清理窗口外旧记录以防无限增长。
        """
        now = time.time()
        failures = self._channel_failures.get(channel_id, [])
        # 清理窗口外旧记录(惰性清理,防止列表无限增长)
        failures = [t for t in failures if now - t < self._failure_window]
        failures.append(now)
        self._channel_failures[channel_id] = failures

    def record_channel_success(self, channel_id: int):
        """记录频道成功(清除失败记录)。"""
        self._channel_failures.pop(channel_id, None)

    def get_channel_health_score(self, channel_id: int) -> float:
        """返回频道健康评分(0.0-1.0,越高越好)。

        算法:
        - 1.0 = 无失败
        - 失败次数越多分数越低(每次扣 0.2)
        - 5 分钟窗口外的失败不计入
        """
        now = time.time()
        failures = self._channel_failures.get(channel_id, [])
        recent = [t for t in failures if now - t < self._failure_window]

        # 惰性清理:窗口外记录已失效则回写/删除
        if len(recent) != len(failures):
            if recent:
                self._channel_failures[channel_id] = recent
            else:
                self._channel_failures.pop(channel_id, None)

        score = max(0.0, 1.0 - len(recent) * 0.2)
        return score

    # ─── 内部工具 ───────────────────────────────────────────────

    async def _rank_by_status_and_health(
        self, candidates: list[dict]
    ) -> list[dict]:
        """按 cells 状态(权重)+ 频道健康评分排序。

        - primary 频道(status='active')优先级最高
        - shadow1 次之,shadow2 最低
        - 同级别内按健康评分降序(失败率低优先)
        """
        cell_map = await self._load_cell_map()

        def sort_key(c: dict) -> tuple[int, float]:
            cell = cell_map.get(c["channel_id"])
            status = cell.get("status", "") if cell else ""
            priority = _STATUS_PRIORITY.get(status, _DEFAULT_PRIORITY)
            health = self.get_channel_health_score(c["channel_id"])
            # priority 升序(active=0 在前);health 降序(取负使高分在前)
            return (priority, -health)

        return sorted(candidates, key=sort_key)

    async def _load_cell_map(self) -> dict[int, dict]:
        """加载 cells_local 全量数据,构建 channel_id -> cell 映射。

        优先使用本地逐行表(get_all_cells_local),降级到 JSON 快照。
        与 storage/delivery_resolver.py 的 _walk_ring_for_channel 保持一致。
        """
        try:
            cells = await self._store.get_all_cells_local()
        except Exception as e:
            logger.warning(f"[replica-resolver] cells 查询异常: {e}")
            cells = []

        if not cells:
            # 降级到快照(Mon Bot 写入的全量 JSON)
            try:
                snap_cells, _ = await self._store.load_cells_snapshot()
                cells = snap_cells or []
            except Exception as e:
                logger.warning(f"[replica-resolver] cells 快照降级失败: {e}")
                cells = []

        return {c["channel_id"]: c for c in cells if "channel_id" in c}
