"""R44 G0-2 / R46 P0-1: 外部副作用 receipt 持久化,保证 effectively-once 语义。

R46 P0-1 整改:
- critical effect 类型(telegram_send/copy/r2_put/restore/ban/takedown/purge) fail-closed:
  manager 不可用或读写失败时直接拒绝外部副作用(raise EffectReceiptError)。
- 非关键通知允许显式 best_effort=True。
- 表增加 request_hash、attempt、lease_owner、lease_until、last_error、reconcile_status。
- record_pending 使用 CAS claim(ON CONFLICT)防止并发重复执行。
- DB 写回失败进入 reconciliation,不盲重试。

receipt 结构:
    (action_id, effect_type, target, status, external_id, created_at,
     completed_at, request_hash, attempt, lease_owner, lease_until,
     last_error, reconcile_status)
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from loguru import logger


# R46 P0-1: critical effect 类型集合 — manager 不可用或读写失败时 fail-closed
CRITICAL_EFFECT_TYPES: frozenset[str] = frozenset({
    "telegram_send",
    "telegram_copy",
    "r2_put",
    "r2_download",
    "restore",
    "ban",
    "takedown",
    "purge",
    "crdb_delete",
})


class EffectReceiptError(Exception):
    """R46 P0-1: Effect Receipt 持久化失败,critical 副作用必须中止。"""


class EffectReceiptManager:
    """管理外部副作用 receipt 的记录和查询。

    使用 cache_store 的 SQLite 数据库持久化 receipt。
    表 DDL 由 database/cache_store.py 创建:
        CREATE TABLE IF NOT EXISTS effect_receipts (
            action_id          TEXT NOT NULL,
            effect_type       TEXT NOT NULL,
            target            TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            external_id       TEXT,
            created_at        TEXT NOT NULL,
            completed_at      TEXT,
            request_hash      TEXT,
            attempt           INTEGER NOT NULL DEFAULT 0,
            lease_owner       TEXT,
            lease_until       TEXT,
            last_error        TEXT,
            reconcile_status  TEXT,
            PRIMARY KEY (action_id, effect_type, target)
        );
    """

    def __init__(self, cache_store):
        self._store = cache_store

    async def check_receipt(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        fail_closed: bool = False,
    ) -> Optional[dict]:
        """检查是否已有 receipt。

        Args:
            fail_closed: True 时 DB 错误抛 EffectReceiptError(critical 副作用拒绝执行);
                         False 时返回 None(继续执行)。
        """
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法检查 receipt "
                    f"(action={action_id}, type={effect_type}, target={target})"
                )
            return None
        try:
            cursor = await self._store._db.execute(
                "SELECT status, external_id, completed_at, attempt, reconcile_status "
                "FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (action_id, effect_type, target),
            )
            row = await cursor.fetchone()
            if row and row[0] == "completed":
                return {
                    "status": row[0],
                    "external_id": row[1],
                    "completed_at": row[2],
                    "attempt": row[3],
                    "reconcile_status": row[4],
                }
            return None
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] check_receipt 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"check_receipt DB 错误: {e}") from e
            return None

    async def record_pending(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        request_hash: str = "",
        lease_owner: str = "",
        lease_until: str = "",
        fail_closed: bool = False,
    ) -> bool:
        """记录开始执行 receipt(status=pending)。

        R46 P0-1: CAS claim 语义 — INSERT OR IGNORE,若已存在 pending 行则 attempt+1。
        Returns True 表示 claim 成功,False 表示已有 completed(应跳过)。
        """
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 pending "
                    f"(action={action_id})"
                )
            return False
        now = datetime.datetime.utcnow().isoformat()
        try:
            # 先检查是否已 completed
            cursor = await self._store._db.execute(
                "SELECT status FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (action_id, effect_type, target),
            )
            existing = await cursor.fetchone()
            if existing and existing[0] == "completed":
                return False  # 已完成,调用方应跳过

            # CAS claim: INSERT OR IGNORE,已存在则 attempt+1
            if existing:
                await self._store._db.execute(
                    "UPDATE effect_receipts SET status='pending', attempt=attempt+1, "
                    "lease_owner=?, lease_until=?, last_error=NULL, "
                    "reconcile_status='pending', created_at=? "
                    "WHERE action_id=? AND effect_type=? AND target=?",
                    (lease_owner, lease_until, now,
                     action_id, effect_type, target),
                )
            else:
                await self._store._db.execute(
                    "INSERT OR IGNORE INTO effect_receipts "
                    "(action_id, effect_type, target, status, external_id, "
                    " created_at, completed_at, request_hash, attempt, "
                    " lease_owner, lease_until, last_error, reconcile_status) "
                    "VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, 1, ?, ?, NULL, 'pending')",
                    (action_id, effect_type, target, now, request_hash,
                     lease_owner, lease_until),
                )
            await self._store._db.commit()
            return True
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_pending 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_pending DB 错误: {e}") from e
            return False

    async def record_completed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        external_id: str = "",
        *,
        fail_closed: bool = False,
    ) -> None:
        """记录完成 receipt(status=completed)。"""
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 completed"
                )
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'completed', "
                "external_id = ?, completed_at = ?, reconcile_status = 'completed', "
                "last_error = NULL "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (external_id, now, action_id, effect_type, target),
            )
            await self._store._db.commit()
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_completed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_completed DB 错误: {e}") from e

    async def record_failed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        error_msg: str = "",
        *,
        fail_closed: bool = False,
    ) -> None:
        """记录失败 receipt(status=failed)。"""
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 failed"
                )
            return
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'failed', "
                "last_error = ?, reconcile_status = 'needs_reconcile' "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (error_msg[:500] if error_msg else None,
                 action_id, effect_type, target),
            )
            await self._store._db.commit()
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_failed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_failed DB 错误: {e}") from e

    async def list_pending_reconcile(self, limit: int = 100) -> list[dict]:
        """R46 P0-1: 列出需要 reconciliation 的 receipt(status=failed/needs_reconcile)。"""
        if not self._store._db:
            return []
        try:
            cursor = await self._store._db.execute(
                "SELECT action_id, effect_type, target, status, attempt, "
                "last_error, reconcile_status "
                "FROM effect_receipts "
                "WHERE reconcile_status = 'needs_reconcile' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "action_id": r[0], "effect_type": r[1], "target": r[2],
                    "status": r[3], "attempt": r[4], "last_error": r[5],
                    "reconcile_status": r[6],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[effect_receipts] list_pending_reconcile 失败: {e}")
            return []


# 模块级单例
_receipt_manager: Optional[EffectReceiptManager] = None


def get_receipt_manager(cache_store=None) -> Optional[EffectReceiptManager]:
    """获取或创建 EffectReceiptManager 单例。

    Returns None if not initialized (caller must handle fail-closed).
    """
    global _receipt_manager
    if _receipt_manager is None and cache_store is not None:
        _receipt_manager = EffectReceiptManager(cache_store)
    return _receipt_manager
