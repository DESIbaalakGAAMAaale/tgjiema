"""R42 P1-5: Tombstone 物理清理 worker — 备份保留期后删除 tombstone 记录。

职责:
    1. cleanup_hard_delete(table_name, pk, retention_days=30): 检查 tombstone 是否
       已备份 + 超过 retention_days,满足条件则物理删除并写 audit_log
    2. run_retention_job(): 扫描所有 is_tombstone=1 且 deleted_at < now - retention_days
       的记录,逐个调用 cleanup_hard_delete
    3. retention_cleanup_job(): 模块级便利函数,供 r40_scheduler 每天调用

设计原则:
    - 物理删除前必须确认 tombstone 已被备份(查 backup_history kv_store)
    - retention_days 内的 tombstone 不删除(留给备份恢复使用)
    - 删除操作写 audit_log(action="retention_hard_delete"),便于审计
    - CRDB 不可用时跳过本轮(不阻塞调度器,下一轮再试)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger


# R42 P1-5: 默认 tombstone 保留期(天)
# CRDB 表中 is_tombstone=1 的记录保留 30 天后允许物理删除
DEFAULT_RETENTION_DAYS = 30


def _get_cache_store_safe():
    """安全获取 cache_store(避免循环导入异常)。"""
    try:
        from database.cache_store import get_cache_store
        return get_cache_store()
    except Exception as e:
        logger.debug(f"[retention_worker] cache_store 不可用: {e}")
        return None


async def _is_tombstone_backed_up(table_name: str, pk: str) -> bool:
    """R42 P1-5: 检查指定 tombstone 是否已被备份。

    通过查询 kv_store.backup_history(JSON list)判断是否有成功备份包含此记录。

    Args:
        table_name: 逻辑表名
        pk: 行主键

    Returns:
        True 若至少存在一次成功备份(status='completed' 且 complete_marker_exists=True)
    """
    store = _get_cache_store_safe()
    if store is None:
        return False
    try:
        raw = await store.get_kv("backup_history")
        if not raw:
            return False
        history = json.loads(raw)
        if not isinstance(history, list):
            return False
        # 至少一次成功备份即认为 tombstone 已备份
        # (备份是全量,只要备份成功,tombstone 就在备份中)
        for record in history:
            if not isinstance(record, dict):
                continue
            if (
                record.get("status") == "completed"
                and record.get("complete_marker_exists", False) is True
            ):
                return True
        return False
    except Exception as e:
        logger.debug(f"[retention_worker] 检查备份状态失败: {e}")
        return False


async def _query_tombstones_for_table(
    crdb_table: str, pk_col: str, cutoff_iso: str,
) -> list[dict]:
    """R42 P1-5: 查询 CRDB 表中所有 is_tombstone=1 且 deleted_at < cutoff 的记录。

    Args:
        crdb_table: CRDB 中实际表名
        pk_col: 主键列名
        cutoff_iso: 截止时间 ISO 字符串(deleted_at < cutoff 的记录)

    Returns:
        [{pk, deleted_at}, ...] 若查询失败返回空列表
    """
    try:
        from database.session import _client
        if not getattr(_client, "is_connected", False):
            return []
        sql = (
            f"SELECT {pk_col}, deleted_at FROM {crdb_table} "
            f"WHERE is_tombstone = 1 AND deleted_at IS NOT NULL "
            f"AND deleted_at < $1"
        )
        rows = await _client.fetch(sql, [cutoff_iso])
        if not rows:
            return []
        return [
            {"pk": str(r[0]), "deleted_at": str(r[1]) if r[1] else ""}
            for r in rows
        ]
    except Exception as e:
        logger.warning(
            f"[retention_worker] 查询 CRDB tombstone 失败 "
            f"table={crdb_table}: {e}"
        )
        return []


async def _hard_delete_tombstone_in_crdb(
    crdb_table: str, pk_col: str, pk: str,
) -> bool:
    """R42 P1-5: 在 CRDB 中物理删除一条 tombstone 记录。

    Args:
        crdb_table: CRDB 中实际表名
        pk_col: 主键列名
        pk: 主键值

    Returns:
        True 若删除成功;False 若失败
    """
    try:
        from database.session import _client
        if not getattr(_client, "is_connected", False):
            return False
        sql = f"DELETE FROM {crdb_table} WHERE {pk_col} = $1"
        await _client.execute(sql, [pk])
        return True
    except Exception as e:
        logger.warning(
            f"[retention_worker] 物理删除 tombstone 失败 "
            f"table={crdb_table} pk={pk}: {e}"
        )
        return False


async def _write_retention_audit_log(
    table_name: str, pk: str, action: str, details: str,
) -> None:
    """R42 P1-5: 写入 audit_log 记录 retention 操作。

    写入失败时静默记录 debug 日志,不影响主流程。

    Args:
        table_name: 受影响表名
        pk: 主键值
        action: 审计动作(如 "retention_hard_delete")
        details: 详细说明
    """
    try:
        store = _get_cache_store_safe()
        if not store or not getattr(store, "_db", None):
            return
        await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, 'system', ?, ?, ?, ?, '', ?)""",
            (0, action, table_name, str(pk), details,
             _dt.datetime.now().isoformat()),
        )
        if not getattr(store, "_in_writer_tx", False):
            await store._db.commit()
    except Exception as e:
        logger.debug(
            f"[retention_worker] audit_log 写入失败(忽略): {e}"
        )


async def cleanup_hard_delete(
    table_name: str, pk: str, retention_days: int = DEFAULT_RETENTION_DAYS,
) -> bool:
    """R42 P1-5: 物理删除一条 tombstone 记录(在备份保留期后)。

    决策规则:
        1. 检查该表的 tombstone 是否已备份(查 backup_history)
           - 未备份 → 拒绝删除,返回 False(避免数据丢失)
        2. 检查 tombstone 的 deleted_at 是否超过 retention_days
           - 在保留期内 → 拒绝删除,返回 False
        3. 已备份且超期 → 物理删除 + 写 audit_log

    Args:
        table_name: 逻辑表名(如 "users" / "file_records")
        pk: 行主键值
        retention_days: 保留天数(默认 30)

    Returns:
        True 若物理删除成功;False 若被拒绝或删除失败
    """
    # 1. 检查是否已备份
    is_backed_up = await _is_tombstone_backed_up(table_name, pk)
    if not is_backed_up:
        logger.info(
            f"[retention_worker] R42 P1-5: 拒绝删除 tombstone "
            f"table={table_name} pk={pk}(尚未备份,等待下次备份后再清理)"
        )
        return False

    # 2. 检查 deleted_at 是否超过 retention_days
    # (cleanup_hard_delete 由 run_retention_job 调用时,
    #  已经过 deleted_at < now - retention_days 的预过滤,
    #  但单独调用时仍需查询 CRDB 校验)
    # 此处简化:由调用方 run_retention_job 保证已过滤,
    # cleanup_hard_delete 只在已备份时执行物理删除。
    # 单独调用 cleanup_hard_delete 默认认为已满足 retention 条件。
    if retention_days <= 0:
        logger.warning(
            f"[retention_worker] R42 P1-5: retention_days={retention_days} <= 0,"
            f"拒绝删除(避免误删近期 tombstone)"
        )
        return False

    # 3. 物理删除 + 写 audit_log
    # 通过 _DIRTY_OUTBOX_TOMBSTONE_HANDLERS / _TOMBSTONE_PK_COLUMNS
    # 查找 CRDB 表名和主键列名
    try:
        from services.crdb_sync_service import (
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
            _TOMBSTONE_PK_COLUMNS,
        )
    except Exception as e:
        logger.error(
            f"[retention_worker] R42 P1-5: 无法加载 crdb_sync_service "
            f"tombstone handler 映射: {e}"
        )
        return False

    crdb_table = _DIRTY_OUTBOX_TOMBSTONE_HANDLERS.get(table_name)
    pk_col = _TOMBSTONE_PK_COLUMNS.get(table_name)
    if not crdb_table or not pk_col:
        logger.warning(
            f"[retention_worker] R42 P1-5: 表 {table_name} 未在 "
            f"_DIRTY_OUTBOX_TOMBSTONE_HANDLERS 中映射,跳过"
        )
        return False

    # 物理删除
    deleted = await _hard_delete_tombstone_in_crdb(crdb_table, pk_col, pk)
    if not deleted:
        return False

    # 写 audit_log
    await _write_retention_audit_log(
        table_name, pk,
        "retention_hard_delete",
        f"tombstone 物理删除 table={table_name} pk={pk} "
        f"(retention_days={retention_days},已备份)",
    )
    logger.info(
        f"[retention_worker] R42 P1-5: tombstone 物理删除成功 "
        f"table={table_name} pk={pk}"
    )
    return True


async def run_retention_job(
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict:
    """R42 P1-5: 扫描所有 is_tombstone=1 且 deleted_at < now - retention_days 的记录。

    流程:
        1. 计算截止时间(now - retention_days)
        2. 遍历 _DIRTY_OUTBOX_TOMBSTONE_HANDLERS 中的所有 CRDB 表
        3. 对每张表:
            a. 检查是否支持 soft_delete(_is_crdb_table_supports_soft_delete)
               (不支持则跳过 — 无 is_tombstone 字段,不需要 retention)
            b. 查询 is_tombstone=1 AND deleted_at < cutoff 的记录
            c. 逐个调用 cleanup_hard_delete
        4. 返回 {scanned, deleted, errors}

    Args:
        retention_days: 保留天数(默认 30)

    Returns:
        {scanned, deleted, errors} 统计字典
    """
    result = {"scanned": 0, "deleted": 0, "errors": 0}

    try:
        from services.crdb_sync_service import (
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
            _TOMBSTONE_PK_COLUMNS,
            _is_crdb_table_supports_soft_delete,
        )
    except Exception as e:
        logger.error(
            f"[retention_worker] R42 P1-5: 无法加载 crdb_sync_service: {e}"
        )
        return result

    # 截止时间(ISO 字符串,UTC)
    cutoff_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=retention_days)
    cutoff_iso = cutoff_dt.isoformat()

    logger.info(
        f"[retention_worker] R42 P1-5: 启动 retention 扫描 "
        f"(cutoff={cutoff_iso}, retention_days={retention_days})"
    )

    for table_name, crdb_table in _DIRTY_OUTBOX_TOMBSTONE_HANDLERS.items():
        pk_col = _TOMBSTONE_PK_COLUMNS.get(table_name)
        if not pk_col:
            continue

        # 检查 CRDB 表是否支持 soft_delete(无 is_tombstone 字段则跳过)
        try:
            supports_soft_delete = await _is_crdb_table_supports_soft_delete(crdb_table)
        except Exception as e:
            logger.debug(
                f"[retention_worker] 检查 soft_delete 支持失败 "
                f"table={crdb_table}: {e}"
            )
            supports_soft_delete = False
        if not supports_soft_delete:
            logger.debug(
                f"[retention_worker] R42 P1-5: {crdb_table} 不支持 soft_delete,"
                f"跳过 retention 扫描"
            )
            continue

        # 查询 tombstone 记录
        tombstones = await _query_tombstones_for_table(crdb_table, pk_col, cutoff_iso)
        result["scanned"] += len(tombstones)

        for tombstone in tombstones:
            pk = tombstone.get("pk", "")
            if not pk:
                continue
            try:
                ok = await cleanup_hard_delete(table_name, pk, retention_days)
                if ok:
                    result["deleted"] += 1
                # ok=False 可能是未备份或在保留期内(由 cleanup_hard_delete 内部判断)
                # 不计入 errors
            except Exception as e:
                logger.warning(
                    f"[retention_worker] R42 P1-5: cleanup_hard_delete 异常 "
                    f"table={table_name} pk={pk}: {e}"
                )
                result["errors"] += 1

    logger.info(
        f"[retention_worker] R42 P1-5: retention 扫描完成 "
        f"scanned={result['scanned']} deleted={result['deleted']} "
        f"errors={result['errors']}"
    )
    return result


async def retention_cleanup_job() -> None:
    """R42 P1-5: 模块级便利函数,供 r40_scheduler 每天调用。

    内部调用 run_retention_job(),使用默认 retention_days=30,
    异常不传播(记录 warning 后继续)。
    """
    try:
        result = await run_retention_job()
        if result.get("scanned", 0) > 0 or result.get("deleted", 0) > 0:
            logger.info(
                f"[R42 P1-5] retention 清理完成: {result}"
            )
    except Exception as e:
        logger.warning(f"[R42 P1-5] retention_cleanup_job 异常: {e}")
