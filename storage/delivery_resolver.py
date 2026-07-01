"""Delivery Resolver — Dsp 发送频道解析器
给定一个取件任务,按环形顺序解析最佳存储频道。
如果首选频道不可达,自动降级到 Shadow1→Shadow2→下一环。
"""
import asyncio
import time
from loguru import logger
from database import (
    get_active_or_shadow_cell,
    get_next_active_cell,
    get_cells_col,
)
from utils.flood_waiter import safe_copy_message
from utils.per_channel_limiter import acquire_channel_limit

# 模块级缓存:避免每次 resolve_delivery_channel() 都查询 CRDB
# 使用 per-entry 时间戳,避免新条目加入时延长整个缓存的生命周期
_cell_cache: dict[int, tuple[dict, float]] = {}  # channel_id -> (cell_dict, cached_at)
_CELL_CACHE_TTL: float = 120.0  # 单条缓存有效期 120 秒(与 Mon Bot 对齐,减少降级时全表扫描)


def invalidate_cell_cache(channel_id: int = None):
    """失效 delivery_resolver 缓存,用于 Mon Bot 改变频道状态时通知 Dsp。"""
    if channel_id is not None:
        _cell_cache.pop(channel_id, None)
    else:
        _cell_cache.clear()


class DeliveryChannel:
    """解析结果:推荐的存储频道 + 降级链路。"""

    def __init__(self, channel_id: int, slot_id: str, status: str):
        self.channel_id = channel_id
        self.slot_id = slot_id
        self.status = status

    def __repr__(self):
        return f"DeliveryChannel({self.slot_id}/{self.status}/{self.channel_id})"


async def resolve_delivery_channel(primary_channel_id: int) -> DeliveryChannel:
    """给定主存储频道ID,返回当前可用的发送频道。

    解析顺序:
    1. 查询该频道对应的 cell(可能是 active/shadow1/shadow2/lost)
    2. 如果是 active 或 r100,直接返回
    3. 如果是 shadow 或 lost,沿环形找下一个 active 或 shadow1
    4. 最多尝试 3 层降级,防止无限环
    """
    # 先查本地缓存(per-entry TTL),避免每次调用都查询 CRDB
    now = time.monotonic()
    cached = _cell_cache.get(primary_channel_id)
    if cached is not None and now - cached[1] < _CELL_CACHE_TTL:
        cell = cached[0]
    else:
        cell = None  # 缓存过期或未命中,强制重新查询

    if cell is None:
        cell = await get_active_or_shadow_cell(primary_channel_id)
        _cell_cache[primary_channel_id] = (cell, time.monotonic())

    if cell is None:
        # 该频道不在 cells 表中,直接返回原频道
        return DeliveryChannel(primary_channel_id, "unknown", "direct")

    status = cell["status"]

    # active 或 r100:直接用
    if status in ("active", "r100"):
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], status)

    # shadow1:可用但非首选
    if status == "shadow1":
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], "shadow1")

    # shadow2 或 lost:需要沿环找下一个
    return await _walk_ring_for_channel(primary_channel_id, max_hops=5)


async def _walk_ring_for_channel(channel_id: int, max_hops: int = 5) -> DeliveryChannel:
    """环形遍历,找到第一个可用的频道。使用 cells 全量数据在内存中遍历环形链表。"""
    # 尝试从 delivery_resolver 自身缓存获取(避免全表扫描)
    # 从 per-entry 缓存中提取所有 cell 字典
    all_cells = None
    if _cell_cache:
        now = time.monotonic()
        cached_cells = []
        for ch_id, (cell, ts) in list(_cell_cache.items()):
            if now - ts < _CELL_CACHE_TTL * 2:
                cached_cells.append(cell)
        if cached_cells:
            all_cells = cached_cells

    if all_cells is None:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        cells = await store.get_all_cells_local()
        if cells:
            all_cells = cells
        else:
            snap_cells, _ = await store.load_cells_snapshot()
            if snap_cells:
                all_cells = snap_cells
            else:
                col = get_cells_col()
                all_cells = list(await col.find({}, projection=[
                    "slot_id", "channel_id", "status", "next_active_chat_id",
                ]))

    if not all_cells:
        return DeliveryChannel(channel_id, "unknown", "fallback")

    # 构建 channel_id → cell 的映射
    cell_map = {c["channel_id"]: c for c in all_cells}

    visited = {channel_id}
    current_channel = channel_id

    for _ in range(max_hops):
        current_cell = cell_map.get(current_channel)
        if current_cell is None:
            break

        next_chat_id = current_cell.get("next_active_chat_id")
        if next_chat_id is None:
            break

        next_cell = cell_map.get(next_chat_id)
        if next_cell is None:
            break

        nid = next_cell["channel_id"]
        if nid in visited:
            break  # 检测到环,跳出
        visited.add(nid)

        status = next_cell["status"]
        if status in ("active", "r100", "shadow1"):
            return DeliveryChannel(nid, next_cell["slot_id"], status)

        current_channel = nid

    # 兜底:找同组的 shadow1,在 Python 内存中过滤
    original_cell = cell_map.get(channel_id)
    if original_cell:
        slot_id = original_cell.get("slot_id", "")
        import re
        m = re.match(r'[as](\d+)', slot_id)
        if m:
            group_num = m.group(1)
            shadows = [c for c in all_cells if c.get("slot_id", "").endswith(f"s{group_num}a") and c.get("status") == "shadow1"]
            if shadows:
                sc = shadows[0]
                return DeliveryChannel(sc["channel_id"], sc["slot_id"], "shadow1")

    # 最终兜底:原频道
    return DeliveryChannel(channel_id, "unknown", "fallback")


async def try_deliver(bot_instance, target_user_id: int, from_channel_id: int, message_id: int, protect_content: bool = False, bot_id: int = 1) -> bool:
    """尝试从指定频道发送一条消息给用户(带 Flood Wait 退避 + 频道限流)。成功返回 True。"""
    # 频道限流:检查是否超过 15 msg/min,循环等待直到拿到配额
    while True:
        wait = await acquire_channel_limit(from_channel_id)
        if wait <= 0:
            break
        await asyncio.sleep(wait)

    try:
        await safe_copy_message(bot_instance, target_user_id, from_channel_id, message_id, protect_content=protect_content, bot_id=bot_id)
        return True
    except Exception as e:
        logger.warning(f"[delivery] try_deliver 失败 (channel={from_channel_id}, msg={message_id}): {e}")
        return False


async def deliver_with_fallback(
    bot_instance,
    target_user_id: int,
    primary_channel_id: int,
    message_ids: list[int],
    max_attempts: int = 3,
    protect_content: bool = False,
) -> int:
    """带降级的批量发送。逐个消息尝试,失败时换频道。

    返回成功发送的消息数。
    """
    resolved = await resolve_delivery_channel(primary_channel_id)
    current_channel = resolved.channel_id
    success_count = 0

    for msg_id in message_ids:
        tried_channels = set()
        for attempt in range(max_attempts):
            if attempt > 0:
                # 尝试下一个频道
                next_resolved = await resolve_delivery_channel(current_channel)
                if next_resolved.channel_id in tried_channels:
                    break
                current_channel = next_resolved.channel_id
                tried_channels.add(current_channel)

            ok = await try_deliver(bot_instance, target_user_id, current_channel, msg_id, protect_content=protect_content)
            if ok:
                success_count += 1
                tried_channels.add(current_channel)
                break

            # 同一条消息重试前短暂等待
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.15)

    return success_count