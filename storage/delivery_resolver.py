"""Delivery Resolver — Dsp 发送频道解析器
给定一个取件任务，按环形顺序解析最佳存储频道。
如果首选频道不可达，自动降级到 Shadow1→Shadow2→下一环。
"""
import asyncio
from database import (
    get_active_or_shadow_cell,
    get_next_active_cell,
    get_cells_col,
)
from utils.flood_waiter import safe_copy_message
from utils.per_channel_limiter import acquire_channel_limit


class DeliveryChannel:
    """解析结果：推荐的存储频道 + 降级链路。"""

    def __init__(self, channel_id: int, slot_id: str, status: str):
        self.channel_id = channel_id
        self.slot_id = slot_id
        self.status = status

    def __repr__(self):
        return f"DeliveryChannel({self.slot_id}/{self.status}/{self.channel_id})"


async def resolve_delivery_channel(primary_channel_id: int) -> DeliveryChannel:
    """给定主存储频道ID，返回当前可用的发送频道。

    解析顺序：
    1. 查询该频道对应的 cell（可能是 active/shadow1/shadow2/lost）
    2. 如果是 active 或 r100，直接返回
    3. 如果是 shadow 或 lost，沿环形找下一个 active 或 shadow1
    4. 最多尝试 3 层降级，防止无限环
    """
    cell = await get_active_or_shadow_cell(primary_channel_id)

    if cell is None:
        # 该频道不在 cells 表中，直接返回原频道
        return DeliveryChannel(primary_channel_id, "unknown", "direct")

    status = cell["status"]

    # active 或 r100：直接用
    if status in ("active", "r100"):
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], status)

    # shadow1：可用但非首选
    if status == "shadow1":
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], "shadow1")

    # shadow2 或 lost：需要沿环找下一个
    return await _walk_ring_for_channel(primary_channel_id, max_hops=5)


async def _walk_ring_for_channel(channel_id: int, max_hops: int = 5) -> DeliveryChannel:
    """环形遍历，找到第一个可用的频道。"""
    col = get_cells_col()
    visited = {channel_id}
    current_channel = channel_id

    for _ in range(max_hops):
        next_cell = await get_next_active_cell(current_channel)
        if next_cell is None:
            break

        nid = next_cell["channel_id"]
        if nid in visited:
            break  # 检测到环，跳出
        visited.add(nid)

        status = next_cell["status"]
        if status in ("active", "r100", "shadow1"):
            return DeliveryChannel(nid, next_cell["slot_id"], status)

        current_channel = nid

    # 兜底：找同组的 shadow1
    original_cell = await col.find_one({"channel_id": channel_id})
    if original_cell:
        slot_id = original_cell.get("slot_id", "")
        # 提取组号
        import re
        m = re.match(r'[as](\d+)', slot_id)
        if m:
            group_num = m.group(1)
            shadows = await col.find({
                "slot_id": {"$regex": f"s{group_num}a$"},
                "status": "shadow1",
            })
            if shadows:
                sc = shadows[0]
                return DeliveryChannel(sc["channel_id"], sc["slot_id"], "shadow1")

    # 最终兜底：原频道
    return DeliveryChannel(channel_id, "unknown", "fallback")


async def try_deliver(bot_instance, target_user_id: int, from_channel_id: int, message_id: int, protect_content: bool = False) -> bool:
    """尝试从指定频道发送一条消息给用户（带 Flood Wait 退避 + 频道限流）。成功返回 True。"""
    # 频道限流：检查是否超过 15 msg/min
    wait = acquire_channel_limit(from_channel_id)
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        await safe_copy_message(bot_instance, target_user_id, from_channel_id, message_id, protect_content=protect_content)
        return True
    except Exception:
        return False


async def deliver_with_fallback(
    bot_instance,
    target_user_id: int,
    primary_channel_id: int,
    message_ids: list[int],
    max_attempts: int = 3,
    protect_content: bool = False,
) -> int:
    """带降级的批量发送。逐个消息尝试，失败时换频道。

    返回成功发送的消息数。
    """
    resolved = await resolve_delivery_channel(primary_channel_id)
    current_channel = resolved.channel_id
    success_count = 0
    tried_channels = set()

    for msg_id in message_ids:
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