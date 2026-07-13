"""R45: decode_logs 7 天短期保留 + 每日分批清理。

评估报告 5.1: decode_logs 仅用于近期排障、风控与审查,不作为长期审计库。
- 保留窗口: 7 天(可配置 DECODE_LOGS_RETENTION_DAYS)
- 每日执行一次分批清理,每批限制 500 行,避免一次性大删除造成 RU 峰值
- 保留 idx_decode_logs_request_time 索引,服务于按时间范围的过期清理
"""
import asyncio
import datetime
from typing import Any

from loguru import logger


async def cleanup_expired_decode_logs(
    store,
    retention_days: int = 7,
    batch_size: int = 500,
    max_batches: int = 20,
) -> dict[str, Any]:
    """清理超过保留期的 decode_logs 记录。
    
    Args:
        store: cache_store 实例(用于 SQLite 本地清理)
        retention_days: 保留天数(默认 7 天)
        batch_size: 每批删除行数(默认 500)
        max_batches: 单次执行最大批数(避免长时间锁表,默认 20)
    
    Returns:
        {
            "deleted_count": int,    # 实际删除总数
            "batches_run": int,       # 执行批数
            "retention_days": int,    # 使用的保留天数
            "cutoff_time": str,       # 截止时间 ISO 字符串
        }
    """
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=retention_days)).isoformat()
    total_deleted = 0
    batches_run = 0
    
    for batch_idx in range(max_batches):
        batches_run += 1
        try:
            # 本地 SQLite 清理(零 RU)
            cursor = await store._db.execute(
                "DELETE FROM decode_logs WHERE request_time < ? AND id IN ("
                "  SELECT id FROM decode_logs WHERE request_time < ? LIMIT ?"
                ")",
                (cutoff, cutoff, batch_size),
            )
            await store._db.commit()
            deleted = cursor.rowcount if cursor.rowcount > 0 else 0
            total_deleted += deleted
            
            if deleted < batch_size:
                # 没有更多记录需要清理
                break
        except Exception as e:
            logger.error(f"[decode_logs_cleanup] 批次 {batch_idx} 清理失败: {e}")
            break
        await asyncio.sleep(0.1)  # 批次间短暂休眠,避免锁竞争
    
    logger.info(
        f"[decode_logs_cleanup] 清理完成: 删除 {total_deleted} 条, "
        f"保留 {retention_days} 天, 截止 {cutoff}"
    )
    return {
        "deleted_count": total_deleted,
        "batches_run": batches_run,
        "retention_days": retention_days,
        "cutoff_time": cutoff,
    }


async def run_daily_cleanup_loop(store, retention_days: int = 7):
    """每日清理循环(24 小时一次)。"""
    while True:
        try:
            await cleanup_expired_decode_logs(store, retention_days=retention_days)
        except Exception as e:
            logger.error(f"[decode_logs_cleanup] 每日清理异常: {e}")
        await asyncio.sleep(86400)  # 24 小时
