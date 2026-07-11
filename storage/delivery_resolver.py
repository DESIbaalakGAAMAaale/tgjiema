"""Delivery Resolver — Dsp 发送频道解析器
给定一个取件任务,按环形顺序解析最佳存储频道。
如果首选频道不可达,自动降级到 Shadow1→Shadow2→下一环。
"""
import asyncio
import re
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
    2. 如果是 active,直接返回（r100 是只写归档频道，不参与派发）
    3. 如果是 shadow1,直接返回自己 —— 轮转降级后文件仍在原位(轮转不删文件),
       用原始 msg_id 直接 copy 即可,无需跳到新 active、不依赖镜像映射。
       这避免了镜像未完成时 PRE-01 跳转后映射缺失导致投递失败的问题。
    4. 如果是 shadow2 或 lost(频道可能不可访问),沿环形找同组 shadow1/其他 active
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

    # active:直接用（R-2: r100 是只写归档频道，不参与派发）
    if status == "active":
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], status)

    # shadow1:轮转降级后文件仍在原位,直接用原始 msg_id 投递。
    # 不跳到新 active(PRE-01),因为镜像可能未完成导致映射缺失。
    # 旧频道 Telegram 频道本身未变,文件仍在,bot 仍有读权限。
    if status == "shadow1":
        return DeliveryChannel(cell["channel_id"], cell["slot_id"], "shadow1")

    # shadow2 或 lost:文件可能不可访问(被封/故障),走环形找替代频道
    # 优先同组 shadow1(有镜像),其次跨组 active(兜底)
    return await _walk_ring_for_channel(primary_channel_id, max_hops=5)


async def _walk_ring_for_channel(channel_id: int, max_hops: int = 5) -> DeliveryChannel:
    """原频道降级(shadow2/lost)时解析可用频道。使用 cells 全量数据在内存中遍历。

    注意：始终从 SQLite cells_local 读取全量数据，不使用 _cell_cache 部分缓存。
    环形遍历需要完整的拓扑信息，部分缓存会导致 cell_map 缺失中间节点，遍历提前中断。

    解析顺序(关键):
    1. 优先同组 shadow1 —— 文件由 mon_bot 同组镜像(active→shadow1/shadow2),
       同组 shadow1 一定有文件副本且 message_backups 有映射。
    2. 同组 shadow1 不可用 → 沿环形链表找其他可用频道(跨组,可能无文件,仅作兜底)。
    3. 都失败 → 返回原频道(fallback)。

    注意:不能先走环形遍历!环形 next_active_chat_id 指向的是其他组的 active 频道,
    文件没有跨组复制,先走环形会返回一个没有文件的频道,导致映射缺失、投递失败。
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
    original_cell = cell_map.get(channel_id)

    # 1. 优先同组频道(文件镜像于同组 shadow1/shadow2,映射存在)
    # slot_id 格式: active=a{N}, shadow1=s{N}a, shadow2=s{N}b
    # 轮转后 slot_id 不变只改 status,所以按组号查找,不按 slot_id 字面匹配
    if original_cell:
        slot_id = original_cell.get("slot_id", "")
        m = re.match(r'[as](\d+)', slot_id)
        if m:
            group_num = m.group(1)
            # 同组所有 cell: slot_id 中数字部分 == group_num
            same_group = [
                c for c in all_cells
                if re.match(rf'[as]{group_num}[ab]?$', c.get("slot_id", ""))
            ]
            # 优先同组 active(可能是轮转提升的,有完整镜像)
            for c in same_group:
                if c.get("status") == "active" and c["channel_id"] != channel_id:
                    return DeliveryChannel(c["channel_id"], c["slot_id"], "active")
            # 其次同组 shadow1(有镜像)
            for c in same_group:
                if c.get("status") == "shadow1":
                    return DeliveryChannel(c["channel_id"], c["slot_id"], "shadow1")
            # 最后同组 shadow2(也有镜像,优先级最低)
            for c in same_group:
                if c.get("status") == "shadow2":
                    return DeliveryChannel(c["channel_id"], c["slot_id"], "shadow2")

    # 2. 同组都不可用 → 沿环形链表找其他可用频道(跨组兜底,可能无文件)
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
        if status in ("active", "shadow1"):
            return DeliveryChannel(nid, next_cell["slot_id"], status)

        current_channel = nid

    # 3. 最终兜底:原频道
    return DeliveryChannel(channel_id, "unknown", "fallback")


async def try_deliver(bot_instance, target_user_id: int, from_channel_id: int, message_id: int, protect_content: bool = False, bot_id: int = 1, original_channel_id: int | None = None) -> int | None:
    """尝试从指定频道发送一条消息给用户(带 Flood Wait 退避 + 频道限流)。成功返回复制后的 message_id，失败返回 None。

    如果 from_channel_id 是影子频道，会先查 message_backups 映射表获取正确的 backed_msg_id。
    映射缺失时返回 None（禁止回退用主频道 msg_id 盲发，避免投错文件）。
    返回 int 在 truthiness 判断中等价于 True，None 等价于 False，兼容现有 if 调用方式。
    """
    # 查询影子频道 msg_id 映射（如果存在）
    resolved_msg_id = await resolve_backup_msg_id(message_id, from_channel_id, original_channel_id)
    if resolved_msg_id is None:
        return None

    # 频道限流:检查是否超过 15 msg/min,循环等待直到拿到配额
    while True:
        wait = await acquire_channel_limit(from_channel_id)
        if wait <= 0:
            break
        await asyncio.sleep(wait)

    try:
        sent = await safe_copy_message(bot_instance, target_user_id, from_channel_id, resolved_msg_id, protect_content=protect_content, bot_id=bot_id)
        return sent.message_id if sent else None
    except BadRequest as e:
        # C3: 消息不存在于该频道(常见于 failover/rotation 后 target 频道无历史文件)
        logger.warning(f"[delivery] 消息不存在 (channel={from_channel_id}, msg={resolved_msg_id}): {e}")
        return None
    except Exception as e:
        logger.warning(f"[delivery] try_deliver 失败 (channel={from_channel_id}, msg={resolved_msg_id}): {e}")
        return None


async def resolve_backup_msg_id(main_msg_id: int, channel_id: int, original_channel_id: int | None = None) -> int | None:
    """查询 message_backups 表，获取影子频道中对应的 msg_id。

    当 channel_id 与原始存储频道不同（即从影子频道发送）且映射缺失时，
    返回 None 禁止回退用主频道 msg_id 盲发（可能投错文件）。
    当 channel_id 就是原始频道时，返回 main_msg_id 本身。
    original_channel_id 默认为 channel_id，避免漏传时静默用主频道 msg_id 盲发影子频道。
    """
    if original_channel_id is None:
        original_channel_id = channel_id
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
        logger.debug(f"[delivery] 查询 message_backups 映射失败 (main_msg_id={main_msg_id}, channel={channel_id})")
        pass

    # 从影子频道发送但无映射 → 返回 None，禁止盲发
    if channel_id != original_channel_id:
        logger.warning(
            f"[delivery] 影子频道映射缺失 (main_msg_id={main_msg_id}, "
            f"channel={channel_id}, original={original_channel_id})，跳过该频道"
        )
        return None
    return main_msg_id


async def resolve_backup_msg_ids(main_msg_ids: list[int], channel_id: int, original_channel_id: int | None = None) -> list[int] | None:
    """批量查询影子频道 msg_id 映射。返回解析后的 msg_id 列表,任一缺失则返回 None。

    用于 copy_messages 批量复制场景:同组消息要么全部有映射(一起复制保持相册形态),
    要么放弃批量回退逐条(部分相册无意义)。
    """
    if not main_msg_ids:
        return None
    # 原频道:msg_id 不变(original_channel_id 默认为 channel_id,避免漏传)
    if original_channel_id is None:
        original_channel_id = channel_id
    if channel_id == original_channel_id:
        return list(main_msg_ids)
    try:
        from database.session import get_message_backups_col
        col = get_message_backups_col()
        results = await col.find({
            "main_msg_id": {"$in": main_msg_ids},
            "backup_channel_id": channel_id,
        })
        id_map = {r["main_msg_id"]: r.get("backed_msg_id") for r in results}
    except Exception:
        logger.debug(f"[delivery] 批量查询 message_backups 映射失败 (channel={channel_id})")
        return None
    resolved = []
    for mid in main_msg_ids:
        backed = id_map.get(mid)
        if not backed:
            logger.warning(
                f"[delivery] 影子频道批量映射缺失 (main_msg_id={mid}, "
                f"channel={channel_id}, original={original_channel_id})，放弃批量"
            )
            return None
        resolved.append(backed)
    logger.debug(f"[delivery] 批量映射成功: {main_msg_ids} → {resolved} (channel={channel_id})")
    return resolved


async def try_deliver_batch(bot_instance, target_user_id: int, from_channel_id: int, message_ids: list[int], protect_content: bool = False, bot_id: int = 1, original_channel_id: int | None = None) -> list[int] | None:
    """批量复制多条消息(保持媒体组相册形态)。成功返回复制后的 message_id 列表，失败返回 None。

    用 Telegram Bot API copyMessages 一次性复制,同媒体组的消息在目标聊天以相册展示。
    影子频道场景需先解析批量 msg_id 映射,任一缺失则返回 None(回退逐条)。
    """
    from utils.flood_waiter import safe_copy_messages
    resolved_ids = await resolve_backup_msg_ids(message_ids, from_channel_id, original_channel_id)
    if resolved_ids is None:
        return None

    # 频道限流:批量算一次调用,但仍按频道配额等待
    while True:
        wait = await acquire_channel_limit(from_channel_id)
        if wait <= 0:
            break
        await asyncio.sleep(wait)

    try:
        copied = await safe_copy_messages(bot_instance, target_user_id, from_channel_id, resolved_ids, protect_content=protect_content, bot_id=bot_id)
        if copied:
            return [m.message_id for m in copied]
        return None
    except BadRequest as e:
        logger.warning(f"[delivery] 批量消息不存在 (channel={from_channel_id}, msgs={resolved_ids}): {e}")
        return None
    except Exception as e:
        logger.warning(f"[delivery] try_deliver_batch 失败 (channel={from_channel_id}, msgs={resolved_ids}): {e}")
        return None


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
    # 初始解析一次主频道,后续每条消息从主频道开始尝试,避免上一条消息的降级状态污染下一条
    initial_resolved = await resolve_delivery_channel(primary_channel_id)
    initial_channel = initial_resolved.channel_id
    success_count = 0

    for msg_id in message_ids:
        # 每条消息独立维护尝试过的频道集合和当前频道,避免跨消息状态污染
        current_channel = initial_channel
        tried_channels = {current_channel}
        for attempt in range(max_attempts):
            ok = await try_deliver(bot_instance, target_user_id, current_channel, msg_id, protect_content=protect_content, original_channel_id=primary_channel_id)
            if ok:
                success_count += 1
                break

            # 失败:尝试下一个频道
            if attempt < max_attempts - 1:
                # 同一条消息重试前短暂等待
                await asyncio.sleep(0.15)
                next_resolved = await resolve_delivery_channel(current_channel)
                if next_resolved.channel_id in tried_channels:
                    # 所有可用频道都试过了,放弃这条消息
                    break
                current_channel = next_resolved.channel_id
                tried_channels.add(current_channel)

    return success_count