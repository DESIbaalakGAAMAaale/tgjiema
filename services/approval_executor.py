"""R41 P0-4: ApprovalExecutor — 异步消费 command_outbox 的高风险命令执行器。

职责:
- 周期性扫描 ``command_outbox`` 表中 ``status='pending'`` 的条目
- CAS claim 获取独占执行权(``pending → executing``)
- 调用 ``CommandBus.execute_command_outbox_entry()`` 执行 handler
- 成功 → ``mark_executed``;失败 → 增加 ``retry_count``,达到 ``max_retries`` 时 ``mark_failed``
- 失败时通过指数退避(``next_retry_at``)避免无脑重试

设计要点:
- 与 ``services.outbox_worker.OutboxWorker`` 不同,本执行器不依赖 Redis 租约
  (单进程串行执行,简单可靠;CAS UPDATE 即可保证幂等)
- 每个条目的处理完全在独立 ``store.transaction()`` 中完成,
  不与审批 ``approve()`` 事务嵌套(避免 SQLite BEGIN 嵌套报错)
- 由 ``services.r40_scheduler`` 每 30 秒触发一次 ``drain_once()``
- 失败重试时 ``next_retry_at`` 设为未来时间,本轮跳过、下一轮到期再处理

调用关系:
    r40_scheduler.run_approval_executor_loop()
        → approval_executor_drain_job()
            → ApprovalExecutor.drain_once()
                → CommandBus.execute_command_outbox_entry(entry)
                    → handler(params)  # 真正副作用
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from loguru import logger

from database.cache_store import get_cache_store


# ─── 默认参数 ─────────────────────────────────────────────
DEFAULT_BACKOFF_BASE: float = 5.0    # 指数退避基准(秒):5 / 10 / 20 / 40 ...
DEFAULT_BACKOFF_MAX: float = 300.0    # 指数退避上限(5 分钟)
DEFAULT_BATCH_SIZE: int = 10          # 单批最大条目数
DEFAULT_MAX_RETRIES: int = 3         # 默认最大重试次数(与表 schema 默认值一致)


class ApprovalExecutor:
    """R41 P0-4: ``command_outbox`` 表的唯一副作用驱动器。

    通过 ``r40_scheduler`` 周期调用 ``drain_once()`` 消费 pending 条目,
    每个条目在独立事务中处理,与审批事务解耦。
    """

    def __init__(
        self,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        batch_size: int = DEFAULT_BATCH_SIZE,
        command_bus: Optional[Any] = None,
    ):
        """初始化 ApprovalExecutor。

        Args:
            backoff_base: 指数退避基准秒数(失败 1 次 → 5s,2 次 → 10s ...)
            backoff_max: 指数退避上限(超过则截断)
            batch_size: 单轮扫描最大条目数
            command_bus: 可选 CommandBus 实例(测试时注入 mock;None 时 lazy import)
        """
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._batch_size = batch_size
        self._command_bus = command_bus

    # ─── 依赖懒加载 ───────────────────────────────────────

    def _get_command_bus(self):
        if self._command_bus is not None:
            return self._command_bus
        from services.command_bus import CommandBus
        return CommandBus()

    # ─── 主入口 ───────────────────────────────────────────

    async def drain_once(self) -> dict:
        """处理一批 pending command_outbox 条目,返回执行统计。

        流程:
            1. SELECT pending 条目(``status='pending'`` 且 ``next_retry_at`` 已到期或为 NULL)
            2. 对每个条目:
                a. CAS ``pending → executing``(UPDATE WHERE status='pending')
                b. 调用 ``CommandBus.execute_command_outbox_entry(entry)``
                c. 成功:``UPDATE status='executed'``
                d. 失败:增加 retry_count;达到 max_retries 则 ``status='failed'``,
                   否则 ``status='pending'`` + ``next_retry_at=now + backoff``

        Returns:
            统计字典: ``{"total", "success", "failed", "retry_scheduled", "skipped"}``
        """
        store = get_cache_store()
        if not store or not store._db:
            return {"total": 0, "success": 0, "failed": 0,
                    "retry_scheduled": 0, "skipped": 0}

        # 1. 拉取 pending 条目(next_retry_at IS NULL 或 <= now)
        now = datetime.datetime.now().isoformat()
        try:
            cursor = await store._db.execute(
                """SELECT id, action_id, approval_id, command_type, payload,
                          status, retry_count, max_retries, next_retry_at, last_error,
                          created_at, updated_at
                   FROM command_outbox
                   WHERE status = 'pending'
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY id ASC LIMIT ?""",
                (now, self._batch_size),
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"[ApprovalExecutor] 拉取 pending 条目失败: {e}")
            return {"total": 0, "success": 0, "failed": 0,
                    "retry_scheduled": 0, "skipped": 0}

        stats = {
            "total": len(rows),
            "success": 0,
            "failed": 0,
            "retry_scheduled": 0,
            "skipped": 0,
        }

        for row in rows:
            entry = self._row_to_entry(row)
            try:
                outcome = await self._process_entry(entry)
                if outcome == "success":
                    stats["success"] += 1
                elif outcome == "failed":
                    stats["failed"] += 1
                elif outcome == "retry_scheduled":
                    stats["retry_scheduled"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                # 顶层兜底:防止单个条目异常导致整批失败
                logger.error(
                    f"[ApprovalExecutor] 处理 entry id={entry['id']} "
                    f"action_id={entry['action_id']} 顶层异常: {e}"
                )
                await self._handle_failure(entry, f"顶层异常: {e}")
                stats["failed"] += 1

        if stats["total"] > 0:
            logger.info(f"[ApprovalExecutor] drain_once 完成: {stats}")
        return stats

    # ─── 单条目处理 ───────────────────────────────────────

    async def _process_entry(self, entry: dict) -> str:
        """处理单个 entry。

        Args:
            entry: command_outbox 行字典

        Returns:
            "success" / "failed" / "retry_scheduled" / "skipped"
        """
        store = get_cache_store()
        if not store or not store._db:
            return "skipped"

        # 2a. CAS pending → executing(独立事务,不与审批事务嵌套)
        now = datetime.datetime.now().isoformat()
        try:
            async with store.transaction() as tx:
                cursor = await tx.execute(
                    "UPDATE command_outbox SET status = 'executing', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (now, entry["id"]),
                )
                affected = cursor.rowcount if cursor else 0
            if affected == 0:
                # CAS 失败:已被其他 worker 抢占或状态已变(可能在并发场景下发生)
                logger.debug(
                    f"[ApprovalExecutor] entry id={entry['id']} CAS 未命中"
                    f"(已被处理或状态非 pending)"
                )
                return "skipped"
        except Exception as e:
            logger.warning(
                f"[ApprovalExecutor] entry id={entry['id']} CAS 失败: {e}"
            )
            return "skipped"

        # 2b. 调用 CommandBus 执行 handler(完全独立的事务边界)
        cb = self._get_command_bus()
        try:
            result = await cb.execute_command_outbox_entry(entry)
        except Exception as e:
            logger.error(
                f"[ApprovalExecutor] CommandBus 执行异常 entry id={entry['id']}: {e}"
            )
            return await self._handle_failure(entry, f"CommandBus 异常: {e}")

        # 2c. 更新 outbox 状态
        if result.success:
            await self._mark_executed(entry["id"])
            return "success"
        else:
            return await self._handle_failure(entry, result.error)

    async def _mark_executed(self, entry_id: int) -> None:
        """标记 entry 为 executed。"""
        store = get_cache_store()
        if not store or not store._db:
            return
        now = datetime.datetime.now().isoformat()
        try:
            async with store.transaction() as tx:
                await tx.execute(
                    "UPDATE command_outbox SET status = 'executed', updated_at = ? "
                    "WHERE id = ?",
                    (now, entry_id),
                )
        except Exception as e:
            logger.warning(
                f"[ApprovalExecutor] mark_executed 失败 entry_id={entry_id}: {e}"
            )

    async def _handle_failure(self, entry: dict, error: str) -> str:
        """处理失败:retry_count + 1,达到 max_retries 则 ``status='failed'``。

        否则 ``status='pending'`` + ``next_retry_at=now + backoff`` 安排重试。

        Args:
            entry: 失败的 command_outbox 行字典(retry_count 为失败前值)
            error: 错误信息(写入 last_error,截断至 500 字符)

        Returns:
            "failed"(达到最大重试)或 "retry_scheduled"(安排重试)
        """
        store = get_cache_store()
        if not store or not store._db:
            return "failed"

        new_retry_count = int(entry.get("retry_count", 0)) + 1
        max_retries = int(entry.get("max_retries", DEFAULT_MAX_RETRIES))
        now = datetime.datetime.now().isoformat()
        truncated_error = (error or "unknown")[:500]

        if new_retry_count >= max_retries:
            # 达到最大重试次数,标记 failed
            try:
                async with store.transaction() as tx:
                    await tx.execute(
                        "UPDATE command_outbox "
                        "SET status = 'failed', retry_count = ?, last_error = ?, "
                        "    updated_at = ? "
                        "WHERE id = ?",
                        (new_retry_count, truncated_error, now, entry["id"]),
                    )
                logger.warning(
                    f"[ApprovalExecutor] entry id={entry['id']} "
                    f"达到 max_retries={max_retries} 标记 failed "
                    f"(retry_count={new_retry_count})"
                )
            except Exception as e:
                logger.warning(
                    f"[ApprovalExecutor] mark_failed 失败 entry_id={entry['id']}: {e}"
                )
            return "failed"

        # 安排重试(指数退避:backoff_base * 2^(retry_count-1),上限 backoff_max)
        backoff_seconds = min(
            self._backoff_base * (2 ** (new_retry_count - 1)),
            self._backoff_max,
        )
        next_retry_dt = datetime.datetime.now() + datetime.timedelta(seconds=backoff_seconds)
        next_retry_at = next_retry_dt.isoformat()
        try:
            async with store.transaction() as tx:
                await tx.execute(
                    "UPDATE command_outbox "
                    "SET status = 'pending', retry_count = ?, next_retry_at = ?, "
                    "    last_error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_retry_count, next_retry_at, truncated_error, now, entry["id"]),
                )
            logger.info(
                f"[ApprovalExecutor] entry id={entry['id']} 安排重试 "
                f"retry_count={new_retry_count}/{max_retries} "
                f"next_retry_at={next_retry_at} backoff={backoff_seconds}s"
            )
        except Exception as e:
            logger.warning(
                f"[ApprovalExecutor] 安排重试失败 entry_id={entry['id']}: {e}"
            )
        return "retry_scheduled"

    # ─── 辅助 ─────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row) -> dict:
        """将数据库行转换为 entry 字典(字段顺序与 SELECT 一致)。"""
        return {
            "id": row[0],
            "action_id": row[1],
            "approval_id": row[2],
            "command_type": row[3],
            "payload": row[4],
            "status": row[5],
            "retry_count": row[6],
            "max_retries": row[7],
            "next_retry_at": row[8],
            "last_error": row[9],
            "created_at": row[10],
            "updated_at": row[11],
        }


# ─── 模块级单例 + 便利函数 ────────────────────────────────

_executor: Optional[ApprovalExecutor] = None


def get_approval_executor() -> ApprovalExecutor:
    """获取单例 ApprovalExecutor(测试可调用 reset_approval_executor 重置)。"""
    global _executor
    if _executor is None:
        _executor = ApprovalExecutor()
    return _executor


def reset_approval_executor() -> None:
    """重置单例(测试用例间隔离)。"""
    global _executor
    _executor = None


async def drain_once() -> dict:
    """模块级便利函数:处理一批 command_outbox 条目。

    由 ``r40_scheduler.approval_executor_drain_job`` 调用。
    """
    return await get_approval_executor().drain_once()
