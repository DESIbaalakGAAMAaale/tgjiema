"""写操作路由器(方案B v2: 决定走 Redis Stream 还是直写 SQLite)

R33修复: 非幂等操作(increment/refund)也移至直写,避免重放导致二次扣减/退款。

设计:
- WRITER_MODE=redis: 普通写操作入 Redis Stream,CAS/事务/非幂等写直写 SQLite
- WRITER_MODE=sqlite: 降级模式,所有写操作直写 SQLite(旧逻辑)
- Redis 不可用时自动降级到 SQLite 直写

不走 Redis 的方法(CAS/事务/非幂等,必须直写 SQLite):
- try_consume_quota: UPDATE rowcount 决定扣减成功与否
- mark_local_job_dispatched: UPDATE rowcount 决定认领成功与否
- batch_update_cells_local: BEGIN IMMEDIATE 多行原子提交
- delete_cell_local: BEGIN IMMEDIATE 链表指针修复
- R33: increment_user_quota_used: 非幂等(used = used + 1),重放会二次扣减
- R33: refund_quota: 非幂等(used = used - amount),重放会二次退款
"""
from typing import Callable, Awaitable, Any
from loguru import logger

from database import redis_queue


# 不走 Redis 的方法名集合(CAS/事务/非幂等/清理操作,必须直写 SQLite)
_DIRECT_WRITE_METHODS: frozenset[str] = frozenset({
    # CAS 操作(依赖 rowcount 判断成功与否)
    "try_consume_quota",
    "mark_local_job_dispatched",
    # 事务操作(需要 BEGIN IMMEDIATE 原子性)
    "batch_update_cells_local",
    "delete_cell_local",
    # 非幂等操作(R33: 重放会导致二次扣减/退款)
    "increment_user_quota_used",
    "refund_quota",
    # 查询类(需要立即返回结果)
    "reactivate_waiting_start_jobs",
    "reclaim_stale_dispatched",
    "insert_local_job",
    "has_new_upload",
    "has_new_dsp_job",
    # 清理类方法(低频但可能锁冲突)
    "cleanup_local_jobs",
    "delete",
    "cleanup",
    "cleanup_notify_tables",
})


def should_use_redis() -> bool:
    """判断是否启用 Redis Writer 模式。"""
    from config import settings
    if settings.WRITER_MODE != "redis":
        return False
    if not settings.REDIS_URL:
        return False
    return True


def is_direct_write(method_name: str) -> bool:
    """判断是否为 CAS/事务写(必须直写 SQLite)。"""
    return method_name in _DIRECT_WRITE_METHODS


async def route_write(
    method_name: str,
    table: str,
    op_type: str,
    data: dict,
    redis_key: str = "",
    fallback: Callable[[], Awaitable[Any]] = None,
) -> Any:
    """路由写操作。

    Args:
        method_name: cache_store 方法名(Writer 用于分派)
        table: 目标 SQLite 表名
        op_type: 操作类型(upsert/update/delete/insert)
        data: 方法参数字典
        redis_key: 关联的 Redis 缓存 key(Writer 写完后 DEL)
        fallback: 降级回调(降级到 SQLite 直写时调用)

    Returns:
        Redis 模式返回 True/False(推入成功与否)
        降级模式返回 fallback() 的结果
    """
    # CAS/事务写:直写 SQLite
    if is_direct_write(method_name):
        if fallback is not None:
            return await fallback()
        return None

    # 降级模式或 Redis 不可用:直写 SQLite
    if not should_use_redis():
        if fallback is not None:
            return await fallback()
        return None

    # Redis 模式:推入队列
    ok = await redis_queue.push(
        op_type=op_type,
        table=table,
        method_name=method_name,
        data=data,
        redis_key=redis_key,
    )
    if not ok:
        # Redis 推入失败,降级到 SQLite
        logger.debug(f"[WriteRouter] {method_name} Redis 推入失败,降级直写")
        if fallback is not None:
            return await fallback()
    return ok


async def invalidate_cache(redis_key: str) -> None:
    """写操作后失效对应读缓存(保证一致性)。"""
    if redis_key:
        await redis_queue.cache_delete(redis_key)
