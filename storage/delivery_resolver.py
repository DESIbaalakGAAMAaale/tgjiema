"""Delivery Resolver — Dsp 发送频道解析器
给定一个取件任务,按环形顺序解析最佳存储频道。
如果首选频道不可达,自动降级到 Shadow1→Shadow2→下一环。
"""
import asyncio
import time
from loguru import logger
from telegram.error import BadRequest
from database import (
    get_cell_by_channel_local,
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
    3. PRE-01: 如果 cell 有 demoted_to_channel_id（被轮转降级且接替频道已就绪），
       立即跳转到接替频道（接替频道已镜像了原频道内容），无需走环形遍历
    4. 如果是 shadow 或 lost,沿环形找下一个 active 或 shadow1
    5. 最多尝试 3 层降级,防止无限环
    """
    # 先查本地缓存(per-entry TTL),避免每次调用都查询 CRDB
    now = time.monotonic()
    cached = _cell_cache.get(primary_channel_id)
    if cached is not None and now - cached[1] < _CELL_CACHE_TTL:
        cell = cached[0]
    else:
        cell = None  # 缓存过期或未命中,强制重新查询

    if cell is None:
        cell = await get_cell_by_channel_local(primary_channel_id)
        if cell is not None:
            _cell_cache[primary_channel_id] = (cell, time.monotonic())

    if cell is None:
        return DeliveryChannel(primary_channel_id, "unknown", "direct")

    status = cell["status"]

    # active 或 r100:直接用
    if status in ("active", "r100"):
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], status)

    # PRE-01: 降级映射优先 —— 若 mon_bot 已记录 demoted_to_channel_id，
    # 直接跳转到接替频道（该频道是提升后的 active，已镜像原频道内容）。
    # 这避免了环形遍历的开销，且确保 Dsp 在 mon_bot 轮转后第一时间命中新频道。
    demoted_to = cell.get("demoted_to_channel_id")
    if demoted_to:
        # 递归解析接替频道（通常一步到位为 active，但防御性递归以防多次降级）
        # 加 visited 集合防止循环
        visited = {primary_channel_id}
        current_to = demoted_to
        for _ in range(5):
            if current_to in visited:
                break
            visited.add(current_to)
            promoted_cell = await get_cell_by_channel_local(current_to)
            if promoted_cell is None:
                break
            p_status = promoted_cell.get("status", "")
            if p_status in ("active", "r100"):
                return DeliveryChannel(promoted_cell["channel_id"], promoted_cell["slot_id"], p_status)
            # 接替频道也已被降级？沿其 demoted_to_channel_id 继续
            next_to = promoted_cell.get("demoted_to_channel_id")
            if not next_to:
                break
            current_to = next_to
        # 降级映射未能命中 active，继续走环形遍历兜底

    # shadow1:可用但非首选
    if status == "shadow1":
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], "shadow1")

    # shadow2 或 lost:需要沿环找下一个
    return await _walk_ring_for_channel(primary_channel_id, max_hops=5)


async def _walk_ring_for_channel(channel_id: int, max_hops: int = 5) -> DeliveryChannel:
    """环形遍历,找到第一个可用的频道。使用 cells 全量数据在内存中遍历环形链表。

    注意：始终从 SQLite cells_local 读取全量数据，不使用 _cell_cache 部分缓存。
    环形遍历需要完整的拓扑信息，部分缓存会导致 cell_map 缺失中间节点，遍历提前中断。
    """
    from database.cache_store import get_cache_store
    store = get_cache_store()
    cells = await store.get_all_cells_local()
    if cells:
        all_cells = cells
    else:
        snap_cells, _ = await store.load_cells_snapshot()
        all_cells = snap_cells or []

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
    """尝试从指定频道发送一条消息给用户(带 Flood Wait 退避 + 频道限流)。成功返回 True。
    
    如果 from_channel_id 是影子频道，会先查 message_backups 映射表获取正确的 backed_msg_id。
    """
    # 查询影子频道 msg_id 映射（如果存在）
    resolved_msg_id = await resolve_backup_msg_id(message_id, from_channel_id)
    
    # 频道限流:检查是否超过 15 msg/min,循环等待直到拿到配额
    while True:
        wait = await acquire_channel_limit(from_channel_id)
        if wait <= 0:
            break
        await asyncio.sleep(wait)

    try:
        await safe_copy_message(bot_instance, target_user_id, from_channel_id, resolved_msg_id, protect_content=protect_content, bot_id=bot_id)
        return True
    except BadRequest as e:
        # C3: 消息不存在于该频道(常见于 failover/rotation 后 target 频道无历史文件)
        logger.warning(f"[delivery] 消息不存在 (channel={from_channel_id}, msg={resolved_msg_id}): {e}")
        return False
    except Exception as e:
        logger.warning(f"[delivery] try_deliver 失败 (channel={from_channel_id}, msg={resolved_msg_id}): {e}")
        return False


async def resolve_backup_msg_id(main_msg_id: int, channel_id: int) -> int:
    """查询 message_backups 表，获取影子频道中对应的 msg_id。
    如果无映射（该消息是直接存储而非复制），返回原始 main_msg_id。
    """
    try:
        from database.session import get_message_backups_col
        col = get_message_backups_col()
        result = await col.find_one({
            "main_msg_id": main_msg_id,
            "backup_channel_id": channel_id,
        })
        if result:
            backed_id = result.get("backed_msg_id")
            if backed_id:
                logger.debug(f"[delivery] msg_id 映射: {main_msg_id} → {backed_id} (channel={channel_id})")
                return backed_id
    except Exception:
        pass
    return main_msg_id


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