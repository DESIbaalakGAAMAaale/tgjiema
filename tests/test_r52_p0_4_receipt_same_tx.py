"""R52 P0-4: Content Appeal 事务内 Receipt 提交风险 — 修复验证测试。

被测目标:
- services/effect_receipts.py: record_pending / check_receipt 增加 tx 参数,
  传入 tx 时不自行 commit(由外层 transaction 统一管理)。
- services/content_reports.py: process_appeal 第二审批将 record_pending 移入
  store.transaction() 内,传递 tx,command_outbox + audit + dirty + receipt 同事务。
- services/effect_receipts_integration.py: with_effect_receipt / EffectReceiptContext
  传递 tx 给 check_receipt / record_pending。

测试场景:
1. record_pending 无 tx → 自行 commit(向后兼容)
2. record_pending 有 tx → 使用 tx,不 commit
3. record_pending 有 tx,tx 回滚 → receipt 也回滚
4. record_pending 有 tx,receipt 写失败 → 整个 tx 回滚
5. process_appeal 成功 → command_outbox + audit + dirty + receipt 全部写入
6. process_appeal receipt 写失败 → command_outbox + audit + dirty 全部回滚
7. process_appeal command_outbox 写失败 → receipt 不写入
8. 并发 process_appeal → CAS 防止重复
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio

from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(共享给所有 service 模块)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def store():
    """创建使用临时文件数据库的 CacheStore 实例。

    设置 _cs_module._store 让所有模块(content_reports / notifications /
    rbac / effect_receipts)的 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r52_p0_4_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        # 初始化 RBAC 默认角色
        from services import rbac as _rbac_mod
        await _rbac_mod.init_default_roles()
        # 重置 effect_receipts manager 单例(确保使用新 store)
        from services import effect_receipts as _er_mod
        _er_mod._receipt_manager = None
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        # 重置 effect_receipts manager 单例
        from services import effect_receipts as _er_mod
        _er_mod._receipt_manager = None
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """每个用例前重置 EffectReceiptManager 单例,避免跨用例污染。"""
    from services import effect_receipts as er_mod
    er_mod._receipt_manager = None
    yield
    er_mod._receipt_manager = None


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_file_record(store, file_code: str, uploader_id: int = 0,
                              deleted: bool = False):
    """插入测试文件记录到 file_records_local。"""
    now = datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO file_records_local
           (file_code, uploader_id, status, deleted_at, crdb_synced)
           VALUES (?, ?, ?, ?, 1)""",
        (file_code, uploader_id,
         "deleted" if deleted else "active",
         now if deleted else None),
    )
    await store._db.commit()


async def _insert_report(store, reporter_id: int, target_type: str,
                        target_id: str, status: str = "takedown") -> int:
    """直接插入测试举报,返回 report_id。"""
    now = datetime.now().isoformat()
    cursor = await store._db.execute(
        """INSERT INTO content_reports
           (reporter_id, target_type, target_id, reason, description,
            status, appeal_text, appealed_at, created_at)
           VALUES (?, ?, ?, 'spam', '', ?, 'test appeal', ?, ?)""",
        (reporter_id, target_type, target_id, status, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


async def _assign_ops_role(store, user_id: int):
    """给测试用户分配 ops 角色(拥有 disaster:restore 权限)。"""
    from services import rbac as _rbac_mod
    await _rbac_mod.assign_role(user_id, "ops", assigned_by=0)


async def _get_audit_logs(store, action: str) -> list[dict]:
    """查询指定 action 的审计日志。"""
    rows = await store._db.execute_fetchall(
        "SELECT id, actor_id, action, target_type, target_id, details "
        "FROM audit_log WHERE action = ? ORDER BY id ASC",
        (action,),
    )
    return [
        {
            "id": r[0], "actor_id": int(r[1] or 0), "action": r[2],
            "target_type": r[3], "target_id": r[4],
            "details": json.loads(r[5]) if r[5] else {},
        }
        for r in rows
    ]


async def _get_command_outbox_entries(store, action_id: str = None) -> list[dict]:
    """查询 command_outbox 条目。"""
    if action_id:
        rows = await store._db.execute_fetchall(
            "SELECT id, action_id, approval_id, command_type, payload, status "
            "FROM command_outbox WHERE action_id = ? ORDER BY id ASC",
            (action_id,),
        )
    else:
        rows = await store._db.execute_fetchall(
            "SELECT id, action_id, approval_id, command_type, payload, status "
            "FROM command_outbox ORDER BY id ASC",
        )
    return [
        {
            "id": r[0], "action_id": r[1], "approval_id": int(r[2] or 0),
            "command_type": r[3],
            "payload": json.loads(r[4]) if r[4] else {},
            "status": r[5],
        }
        for r in rows
    ]


async def _get_effect_receipts(store, action_id: str) -> list[dict]:
    """查询 effect_receipts 条目。"""
    rows = await store._db.execute_fetchall(
        "SELECT action_id, effect_type, target, status, request_hash, "
        "attempt, reconcile_status, last_error "
        "FROM effect_receipts WHERE action_id = ? ORDER BY created_at ASC",
        (action_id,),
    )
    return [
        {
            "action_id": r[0], "effect_type": r[1], "target": r[2],
            "status": r[3], "request_hash": r[4] or "",
            "attempt": int(r[5] or 0), "reconcile_status": r[6] or "",
            "last_error": r[7] or "",
        }
        for r in rows
    ]


async def _get_dirty_outbox(store, table_name: str = None,
                           pk: str = None) -> list[dict]:
    """查询 dirty_outbox 条目。"""
    if table_name and pk:
        rows = await store._db.execute_fetchall(
            "SELECT id, table_name, pk, version, operation "
            "FROM dirty_outbox WHERE table_name = ? AND pk = ? ORDER BY id ASC",
            (table_name, pk),
        )
    elif table_name:
        rows = await store._db.execute_fetchall(
            "SELECT id, table_name, pk, version, operation "
            "FROM dirty_outbox WHERE table_name = ? ORDER BY id ASC",
            (table_name,),
        )
    else:
        rows = await store._db.execute_fetchall(
            "SELECT id, table_name, pk, version, operation "
            "FROM dirty_outbox ORDER BY id ASC",
        )
    return [
        {
            "id": r[0], "table_name": r[1], "pk": r[2],
            "version": int(r[3] or 0), "operation": r[4],
        }
        for r in rows
    ]


class _TxWrapper:
    """包装 aiosqlite 连接,跟踪 commit 调用次数。

    用于验证 record_pending 在传入 tx 时不自行 commit。
    """

    def __init__(self, conn):
        self._conn = conn
        self.commit_count = 0

    async def execute(self, sql, parameters=None):
        if parameters is None:
            return await self._conn.execute(sql)
        return await self._conn.execute(sql, parameters)

    async def commit(self):
        self.commit_count += 1
        await self._conn.commit()

    async def rollback(self):
        await self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ════════════════════════════════════════════════════════════════
# 1-4: record_pending tx 参数行为测试
# ════════════════════════════════════════════════════════════════

class TestRecordPendingTx:
    """R52 P0-4: record_pending 的 tx 参数行为。"""

    @pytest.mark.asyncio
    async def test_record_pending_no_tx_self_commit(self, store):
        """场景1: record_pending 无 tx → 自行 commit(向后兼容)。

        无 tx 参数时,record_pending 使用全局 store._db 并自行 commit,
        receipt 在调用后立即可见(已持久化)。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(store)
        result = await mgr.record_pending(
            "act_r52_no_tx", "r2_put", "backups/test.enc",
            request_hash="a" * 64,
        )
        assert result is True
        # 无 tx 时应自行 commit,receipt 已持久化
        rows = await store._db.execute_fetchall(
            "SELECT status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            ("act_r52_no_tx", "r2_put", "backups/test.enc"),
        )
        assert len(rows) == 1
        assert rows[0][0] == "pending"

    @pytest.mark.asyncio
    async def test_record_pending_with_tx_does_not_self_commit(self, store):
        """场景2: record_pending 有 tx → 使用 tx 执行,不自行 commit。

        通过 _TxWrapper 跟踪 commit 调用次数,验证 record_pending
        在传入 tx 时不会调用 commit(commit_count 保持为 0)。
        receipt 写入由外层 transaction 统一 commit。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(store)
        wrapper = _TxWrapper(store._db)
        async with store.transaction() as tx:
            result = await mgr.record_pending(
                "act_r52_with_tx", "r2_put", "backups/test2.enc",
                request_hash="b" * 64,
                tx=wrapper,
            )
            assert result is True
            # record_pending 不应自行 commit
            assert wrapper.commit_count == 0, (
                "record_pending 有 tx 时不应自行 commit"
            )
        # 外层 transaction commit 后 receipt 已持久化
        rows = await store._db.execute_fetchall(
            "SELECT status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            ("act_r52_with_tx", "r2_put", "backups/test2.enc"),
        )
        assert len(rows) == 1
        assert rows[0][0] == "pending"

    @pytest.mark.asyncio
    async def test_record_pending_tx_rollback_receipt_gone(self, store):
        """场景3: record_pending 有 tx,tx 回滚 → receipt 也回滚。

        在 transaction 内调用 record_pending(tx=tx) 写入 receipt,
        随后触发异常使 transaction 回滚,receipt 应随之消失。
        """
        from services.effect_receipts import (
            EffectReceiptError, EffectReceiptManager,
        )
        mgr = EffectReceiptManager(store)
        with pytest.raises(EffectReceiptError):
            async with store.transaction() as tx:
                await mgr.record_pending(
                    "act_r52_rollback", "r2_put", "backups/test3.enc",
                    request_hash="c" * 64,
                    tx=tx,
                )
                # 模拟后续步骤失败 → 触发 transaction 回滚
                raise EffectReceiptError("simulated downstream failure")
        # transaction 回滚后 receipt 应不存在
        rows = await store._db.execute_fetchall(
            "SELECT status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            ("act_r52_rollback", "r2_put", "backups/test3.enc"),
        )
        assert len(rows) == 0, "tx 回滚后 receipt 应不存在"

    @pytest.mark.asyncio
    async def test_record_pending_failure_rolls_back_tx(self, store):
        """场景4: record_pending 有 tx,receipt 写失败 → 整个 tx 回滚。

        删除 effect_receipts 表模拟 receipt 写失败,
        验证 transaction 内的其他写入(audit_log)也一并回滚。
        """
        from services.effect_receipts import (
            EffectReceiptError, EffectReceiptManager,
        )
        mgr = EffectReceiptManager(store)
        # 删除 effect_receipts 表模拟 receipt 写失败
        await store._db.execute("DROP TABLE effect_receipts")
        await store._db.commit()
        now = datetime.now().isoformat()
        with pytest.raises(EffectReceiptError):
            async with store.transaction() as tx:
                # 写 audit_log(应被回滚)
                await tx.execute(
                    "INSERT INTO audit_log "
                    "(actor_id, actor_type, action, target_type, target_id, "
                    " details, ip_addr, created_at) "
                    "VALUES (?, 'admin', 'r52_test_action', 'report', '1', "
                    "'{}', '', ?)",
                    (999, now),
                )
                # record_pending 失败(表不存在)→ 触发回滚
                await mgr.record_pending(
                    "act_r52_fail", "restore", "file:x",
                    request_hash="d" * 64,
                    fail_closed=True,
                    tx=tx,
                )
        # 验证 audit_log 已回滚(record_pending 失败导致整个 tx 回滚)
        rows = await store._db.execute_fetchall(
            "SELECT id FROM audit_log WHERE action = 'r52_test_action'",
        )
        assert len(rows) == 0, (
            "record_pending 失败时 audit_log 应随事务回滚"
        )


# ════════════════════════════════════════════════════════════════
# 5-8: process_appeal 同事务集成测试
# ════════════════════════════════════════════════════════════════

class TestProcessAppealSameTx:
    """R52 P0-4: process_appeal 第二审批同事务写 receipt。"""

    @pytest.mark.asyncio
    async def test_process_appeal_success_all_written(self, store):
        """场景5: process_appeal 成功 → command_outbox + audit + dirty + receipt 全部写入。

        第二审批成功后,command_outbox、audit_log(aappeal_second_approval)、
        dirty_outbox、effect_receipts 应全部存在(同事务原子提交)。
        """
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R52P04001", uploader_id=40001, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=40001, target_type="file",
            target_id="R52P04001", status="appealed",
        )
        await _assign_ops_role(store, 40002)
        await _assign_ops_role(store, 40003)
        # 第一审批
        r1 = await process_appeal(report_id, principal_id=40002, decision="approve")
        assert r1["stage"] == "first_approval"
        # 第二审批
        r2 = await process_appeal(report_id, principal_id=40003, decision="approve")
        assert r2["success"] is True
        assert r2["stage"] == "second_approval"
        action_id = r2["action_id"]
        # 验证 command_outbox 已写入
        outbox = await _get_command_outbox_entries(store, action_id)
        assert len(outbox) == 1
        assert outbox[0]["command_type"] == "restore_content"
        assert outbox[0]["status"] == "pending"
        # 验证 audit_log(aappeal_second_approval)已写入
        audits = await _get_audit_logs(store, "appeal_second_approval")
        assert len(audits) == 1
        assert audits[0]["actor_id"] == 40003
        # 验证 dirty_outbox 已写入
        dirty = await _get_dirty_outbox(store, "content_reports", str(report_id))
        assert len(dirty) >= 1
        # 验证 effect_receipts 已写入(pending)
        receipts = await _get_effect_receipts(store, action_id)
        assert len(receipts) == 1
        assert receipts[0]["effect_type"] == "restore"
        assert receipts[0]["status"] == "pending"
        assert receipts[0]["request_hash"] != ""

    @pytest.mark.asyncio
    async def test_process_appeal_receipt_failure_rolls_back_all(self, store):
        """场景6: process_appeal receipt 写失败 → command_outbox + audit + dirty 全部回滚。

        删除 effect_receipts 表模拟 receipt 写失败,
        验证第二审批的 command_outbox、audit_log、dirty_outbox 全部回滚。
        """
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R52P04002", uploader_id=40004, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=40004, target_type="file",
            target_id="R52P04002", status="appealed",
        )
        await _assign_ops_role(store, 40005)
        await _assign_ops_role(store, 40006)
        # 第一审批
        await process_appeal(report_id, principal_id=40005, decision="approve")
        # 删除 effect_receipts 表模拟 receipt 写失败
        await store._db.execute("DROP TABLE effect_receipts")
        await store._db.commit()
        # 第二审批 → record_pending 失败 → 整个事务回滚
        r2 = await process_appeal(report_id, principal_id=40006, decision="approve")
        assert r2["success"] is False
        # 验证 command_outbox 未写入(已回滚)
        outbox = await _get_command_outbox_entries(store)
        assert len(outbox) == 0, "receipt 失败时 command_outbox 应回滚"
        # 验证 audit_log(aappeal_second_approval)未写入(已回滚)
        audits = await _get_audit_logs(store, "appeal_second_approval")
        assert len(audits) == 0, "receipt 失败时 audit_log 应回滚"
        # 验证 dirty_outbox 没有第二审批新增的条目
        # (第一审批会写一条 dirty_outbox,第二审批的应已回滚)
        dirty = await _get_dirty_outbox(store, "content_reports", str(report_id))
        # 第一审批写了一条,第二审批的应回滚 → 只有 1 条
        assert len(dirty) == 1, "receipt 失败时 dirty_outbox 应回滚(仅保留第一审批的)"

    @pytest.mark.asyncio
    async def test_process_appeal_outbox_failure_no_receipt(self, store):
        """场景7: process_appeal command_outbox 写失败 → receipt 不写入。

        删除 command_outbox 表模拟 command_outbox 写失败,
        验证 effect_receipts 未写入(record_pending 在 command_outbox 之后,
        command_outbox 失败导致事务回滚,record_pending 从未执行)。
        """
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R52P04003", uploader_id=40007, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=40007, target_type="file",
            target_id="R52P04003", status="appealed",
        )
        await _assign_ops_role(store, 40008)
        await _assign_ops_role(store, 40009)
        # 第一审批
        await process_appeal(report_id, principal_id=40008, decision="approve")
        # 删除 command_outbox 表模拟写失败
        await store._db.execute("DROP TABLE command_outbox")
        await store._db.commit()
        # 第二审批 → command_outbox INSERT 失败 → 事务回滚
        r2 = await process_appeal(report_id, principal_id=40009, decision="approve")
        assert r2["success"] is False
        # 验证 effect_receipts 未写入(record_pending 从未执行)
        rows = await store._db.execute_fetchall(
            "SELECT action_id FROM effect_receipts "
            "WHERE target = ?",
            ("file:R52P04003",),
        )
        assert len(rows) == 0, (
            "command_outbox 失败时 record_pending 不应执行,receipt 不应写入"
        )
        # 验证 audit_log(aappeal_second_approval)未写入(已回滚)
        audits = await _get_audit_logs(store, "appeal_second_approval")
        assert len(audits) == 0, "command_outbox 失败时 audit_log 应回滚"

    @pytest.mark.asyncio
    async def test_concurrent_process_appeal_cas_prevents_duplicate(self, store):
        """场景8: 并发/重复 process_appeal → CAS 防止重复。

        验证 command_outbox 的 UNIQUE 约束 + 幂等检查防止重复写入:
        - 第二审批成功后,再次审批同一 appeal 被幂等检查拒绝(AppError)。
        - 并发(asyncio.gather)执行时,CAS 确保最多一条 command_outbox 记录(无重复)。

        注:store.transaction() 基于单连接,真正并发事务会共享同一 BEGIN,
        因此并发部分使用错峰启动(延迟)确保第一个调用先提交事务,
        第二个调用命中幂等检查被拒绝,验证 CAS 防重复。
        """
        from services.content_reports import process_appeal
        from services.error_codes import AppError, ErrorCodes
        await _insert_file_record(store, "R52P04004", uploader_id=40010, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=40010, target_type="file",
            target_id="R52P04004", status="appealed",
        )
        await _assign_ops_role(store, 40011)
        await _assign_ops_role(store, 40012)
        await _assign_ops_role(store, 40013)
        # 第一审批
        await process_appeal(report_id, principal_id=40011, decision="approve")
        # 第二审批(成功)
        r2 = await process_appeal(report_id, principal_id=40012, decision="approve")
        assert r2["success"] is True
        action_id = r2["action_id"]
        # 重复第二审批(不同审批人)→ 幂等检查拒绝(command_outbox 已有相同 action_id)
        with pytest.raises(AppError) as exc_info:
            await process_appeal(report_id, principal_id=40013, decision="approve")
        assert exc_info.value.code == ErrorCodes.CONTENT_APPEAL_INVALID_STATE
        # 验证 command_outbox 只有一条(CAS 防重复)
        all_outbox = await _get_command_outbox_entries(store)
        assert len(all_outbox) == 1, (
            f"CAS 应防止重复 command_outbox,实际 {len(all_outbox)} 条"
        )
        # 验证 effect_receipts 只有一条(CAS claim)
        receipts = await _get_effect_receipts(store, action_id)
        assert len(receipts) == 1, (
            f"effect_receipts 应只有一条,实际 {len(receipts)} 条"
        )

        # ── 并发(asyncio.gather)CAS 防重复验证 ──────────────
        # 使用新 appeal,错峰启动确保第一个调用先提交事务,
        # 第二个调用命中幂等检查或 UNIQUE 约束被拒绝。
        await _insert_file_record(store, "R52P04005", uploader_id=40014, deleted=True)
        report_id2 = await _insert_report(
            store, reporter_id=40014, target_type="file",
            target_id="R52P04005", status="appealed",
        )
        await _assign_ops_role(store, 40015)
        await _assign_ops_role(store, 40016)
        await _assign_ops_role(store, 40017)
        # 第一审批
        await process_appeal(report_id2, principal_id=40015, decision="approve")

        async def _delayed_second_approval(principal_id: int):
            # 错峰 50ms,确保第一个调用的事务先提交
            await asyncio.sleep(0.05)
            return await process_appeal(
                report_id2, principal_id=principal_id, decision="approve",
            )

        results = await asyncio.gather(
            process_appeal(report_id2, principal_id=40016, decision="approve"),
            _delayed_second_approval(40017),
            return_exceptions=True,
        )
        # CAS 保证:最多一个成功(无重复)
        success_count = sum(
            1 for r in results
            if isinstance(r, dict) and r.get("success") is True
        )
        assert success_count == 1, (
            f"并发第二审批应恰好一个成功,实际 {success_count} 个成功"
        )
        # 验证 report_id2 的 command_outbox 只有一条(UNIQUE 约束防重复)
        outbox2 = await _get_command_outbox_entries(store)
        r2_outbox = [e for e in outbox2 if e["approval_id"] == report_id2]
        assert len(r2_outbox) == 1, (
            f"CAS 应防止重复 command_outbox,实际 {len(r2_outbox)} 条"
        )
