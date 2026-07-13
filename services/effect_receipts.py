"""R44 G0-2: 外部副作用 receipt 持久化,保证 effectively-once 语义。

每个外部副作用（Telegram 发送/R2 上传/CRDB UPSERT/SQLite 写入）执行前检查 receipt,
已执行则跳过;执行后写入 receipt。崩溃重试时通过 receipt 判断是否已完成。

receipt 结构: (action_id, effect_type, target, status, external_id, created_at)
- action_id: 关联 command_executions.action_id
- effect_type: 'telegram_send' / 'r2_upload' / 'r2_download' / 'crdb_upsert' / 'crdb_delete' / 'sqlite_write'
- target: 操作目标（如 telegram chat_id / r2 key / crdb table:pk）
- status: 'pending' / 'completed' / 'failed'
- external_id: 外部系统返回的 ID（如 telegram message_id / r2 version_id）
- created_at: ISO8601 时间戳
"""
import datetime
from typing import Optional

from loguru import logger


class EffectReceiptManager:
    """管理外部副作用 receipt 的记录和查询。

    使用 cache_store 的 SQLite 数据库持久化 receipt。
    表 DDL 由 database/cache_store.py 创建（R44 任务5 添加）:
        CREATE TABLE IF NOT EXISTS effect_receipts (
            action_id     TEXT NOT NULL,
            effect_type   TEXT NOT NULL,
            target        TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            external_id   TEXT,
            created_at    TEXT NOT NULL,
            completed_at  TEXT,
            PRIMARY KEY (action_id, effect_type, target)
        );
    """

    def __init__(self, cache_store):
        self._store = cache_store

    async def check_receipt(self, action_id: str, effect_type: str, target: str) -> Optional[dict]:
        """检查是否已有完成的 receipt。如果 status=completed,返回 receipt dict;否则返回 None。"""
        if not self._store._db:
            return None
        try:
            cursor = await self._store._db.execute(
                "SELECT status, external_id, completed_at FROM effect_receipts "
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
                }
            return None
        except Exception as e:
            logger.warning(f"[effect_receipts] check_receipt 失败: {e}")
            return None

    async def record_pending(self, action_id: str, effect_type: str, target: str) -> None:
        """记录开始执行 receipt（status=pending）。"""
        if not self._store._db:
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            await self._store._db.execute(
                "INSERT OR IGNORE INTO effect_receipts "
                "(action_id, effect_type, target, status, external_id, created_at, completed_at) "
                "VALUES (?, ?, ?, 'pending', NULL, ?, NULL)",
                (action_id, effect_type, target, now),
            )
            await self._store._db.commit()
        except Exception as e:
            logger.warning(f"[effect_receipts] record_pending 失败: {e}")

    async def record_completed(self, action_id: str, effect_type: str, target: str, external_id: str = "") -> None:
        """记录完成 receipt（status=completed）。"""
        if not self._store._db:
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'completed', external_id = ?, completed_at = ? "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (external_id, now, action_id, effect_type, target),
            )
            await self._store._db.commit()
        except Exception as e:
            logger.warning(f"[effect_receipts] record_completed 失败: {e}")

    async def record_failed(self, action_id: str, effect_type: str, target: str) -> None:
        """记录失败 receipt（status=failed）。"""
        if not self._store._db:
            return
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'failed' "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (action_id, effect_type, target),
            )
            await self._store._db.commit()
        except Exception as e:
            logger.warning(f"[effect_receipts] record_failed 失败: {e}")


# 模块级单例
_receipt_manager: Optional[EffectReceiptManager] = None


def get_receipt_manager(cache_store=None) -> EffectReceiptManager:
    """获取或创建 EffectReceiptManager 单例。"""
    global _receipt_manager
    if _receipt_manager is None and cache_store is not None:
        _receipt_manager = EffectReceiptManager(cache_store)
    return _receipt_manager
