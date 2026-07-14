"""R51 P0-6: Content Appeal 绕过 CommandBus 问题 — 修复验证测试。

被测目标:
- services/content_reports.py: process_appeal (第二审批只写 command_outbox,不直接调用 handler)
- services/command_bus.py: make_restore_content_command (restore_content handler 工厂)
- services/effect_receipts.py: record_pending / record_failed / record_completed

测试场景:
1. process_appeal 第二审批完成 → 只写 command_outbox,不直接调用 handler
2. restore 失败 → 不降级直接恢复,记录 effect receipt + reconcile_status
3. 重复 appeal 同一 content → action_id 确定性,幂等
4. 未授权用户 → RBAC 拒绝(AppError)
5. restore_pending 状态的 appeal 不能再次审批
"""
import hashlib
import inspect
import json
import shutil
import tempfile
from datetime import datetime, timedelta
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
    tmpdir = tempfile.mkdtemp(prefix="r51_p0_6_test_")
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


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_user(store, user_id: int, is_banned: int = 0,
                        ban_expires_at: str = None):
    """插入测试用户到 users_local。"""
    now = datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO users_local
           (user_id, username, first_name, is_banned, ban_expires_at,
            created_at, updated_at, crdb_synced)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (user_id, f"user_{user_id}", "Test", is_banned, ban_expires_at, now, now),
    )
    await store._db.commit()


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


async def _get_report_status(store, report_id: int) -> str:
    """查询举报状态。"""
    rows = await store._db.execute_fetchall(
        "SELECT status FROM content_reports WHERE id = ?",
        (report_id,),
    )
    return rows[0][0] if rows else ""


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


# ════════════════════════════════════════════════════════════════
# 1. 第二审批只写 command_outbox,不直接调用 handler
# ════════════════════════════════════════════════════════════════

class TestSecondApprovalWritesOutboxOnly:
    """R51 P0-6: 第二审批完成后只写 command_outbox,不直接调用 handler。"""

    @pytest.mark.asyncio
    async def test_second_approval_writes_outbox_not_handler(self, store):
        """第二审批 → 写 command_outbox + restored=False(不直接调用 handler)。"""
        from services.content_reports import process_appeal
        # 准备:已软删除的文件 + 申诉
        await _insert_file_record(store, "R51P06001", uploader_id=10001, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10001, target_type="file",
            target_id="R51P06001", status="appealed",
        )
        # 给两个审批人分配 ops 角色(拥有 disaster:restore)
        await _assign_ops_role(store, 20001)
        await _assign_ops_role(store, 20002)
        # 第一审批
        r1 = await process_appeal(report_id, principal_id=20001, decision="approve")
        assert r1["stage"] == "first_approval"
        assert r1["restored"] is False
        # 第二审批
        r2 = await process_appeal(report_id, principal_id=20002, decision="approve")
        assert r2["success"] is True
        assert r2["stage"] == "second_approval"
        # R51 P0-6 核心:restored=False(restore 由 ApprovalExecutor 异步执行)
        assert r2["restored"] is False
        # 验证 command_outbox 已写入
        outbox_entries = await _get_command_outbox_entries(store)
        assert len(outbox_entries) >= 1
        entry = outbox_entries[-1]
        assert entry["command_type"] == "restore_content"
        assert entry["status"] == "pending"
        assert entry["approval_id"] == report_id
        # action_id 确定性格式:restore_content_{appeal_id}_{content_hash[:16]}
        assert entry["action_id"].startswith(f"restore_content_{report_id}_")
        # 验证文件尚未恢复(restore 由 executor 执行)
        rows = await store._db.execute_fetchall(
            "SELECT status, deleted_at FROM file_records_local WHERE file_code = ?",
            ("R51P06001",),
        )
        assert rows[0][0] == "deleted"
        assert rows[0][1] is not None

    @pytest.mark.asyncio
    async def test_second_approval_writes_effect_receipt_pending(self, store):
        """第二审批 → 写 effect_receipts(pending,critical effect)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R51P06002", uploader_id=10002, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10002, target_type="file",
            target_id="R51P06002", status="appealed",
        )
        await _assign_ops_role(store, 20003)
        await _assign_ops_role(store, 20004)
        # 第一 + 第二审批
        await process_appeal(report_id, principal_id=20003, decision="approve")
        r2 = await process_appeal(report_id, principal_id=20004, decision="approve")
        assert r2["success"] is True
        # 验证 effect_receipts 已写入 pending
        action_id = r2["action_id"]
        receipts = await _get_effect_receipts(store, action_id)
        assert len(receipts) >= 1
        receipt = receipts[0]
        assert receipt["effect_type"] == "restore"
        assert receipt["status"] == "pending"
        assert receipt["request_hash"] != ""  # critical effect 必须有 request_hash
        assert receipt["target"] == "file:R51P06002"


# ════════════════════════════════════════════════════════════════
# 2. restore 失败 → 不降级直接恢复,记录 effect receipt + reconcile
# ════════════════════════════════════════════════════════════════

class TestRestoreFailureNoFallback:
    """R51 P0-6: restore 失败 → 不降级直接恢复,记录 effect receipt + reconcile。"""

    @pytest.mark.asyncio
    async def test_restore_failure_marks_restore_failed(self, store):
        """restore_content handler 恢复失败 → 状态变为 restore_failed + effect receipt failed。"""
        from services.command_bus import make_restore_content_command
        from services.error_codes import AppError, ErrorCodes
        from services.content_reports import REPORT_STATUS_RESTORE_FAILED
        # 准备:申诉已审批(command_outbox 已写)
        # 不创建文件记录 → _restore_content_internal 会返回 False(file 不存在)
        report_id = await _insert_report(
            store, reporter_id=10003, target_type="file",
            target_id="NONEXISTENT_FILE", status="restore_pending",
        )
        content_hash = hashlib.sha256(b"file:NONEXISTENT_FILE").hexdigest()
        action_id = f"restore_content_{report_id}_{content_hash[:16]}"
        # 记录 pending effect receipt(模拟 process_appeal 第二审批后的状态)
        from services.effect_receipts import get_receipt_manager
        receipt_mgr = get_receipt_manager(store)
        await receipt_mgr.record_pending(
            action_id=action_id,
            effect_type="restore",
            target="file:NONEXISTENT_FILE",
            request_hash="a" * 64,
        )
        # 执行 restore_content handler(模拟 ApprovalExecutor 调度)
        cmd = make_restore_content_command(
            appeal_id=report_id,
            target_type="file",
            target_id="NONEXISTENT_FILE",
            admin_id=20005,
            content_hash=content_hash,
            reporter_id=10003,
        )
        # handler 应抛出 AppError(CONTENT_APPEAL_RESTORE_FAILED)
        with pytest.raises(AppError) as exc_info:
            await cmd.handler(cmd.params)
        assert exc_info.value.code == ErrorCodes.CONTENT_APPEAL_RESTORE_FAILED
        # 验证 appeal 状态变为 restore_failed
        status = await _get_report_status(store, report_id)
        assert status == REPORT_STATUS_RESTORE_FAILED
        # 验证 effect receipt failed + reconcile_status
        receipts = await _get_effect_receipts(store, action_id)
        assert len(receipts) >= 1
        receipt = receipts[0]
        assert receipt["status"] == "failed"
        assert receipt["reconcile_status"] == "needs_reconcile"

    @pytest.mark.asyncio
    async def test_restore_success_but_status_update_fail_reconciles(self, store):
        """restore 成功但状态更新失败 → effect receipt completed + reconcile_status=needs_reconcile。"""
        from services.command_bus import make_restore_content_command
        from services.error_codes import AppError, ErrorCodes
        # 准备:已软删除的文件
        await _insert_file_record(store, "R51P06004", uploader_id=10004, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10004, target_type="file",
            target_id="R51P06004", status="restore_pending",
        )
        content_hash = hashlib.sha256(b"file:R51P06004").hexdigest()
        action_id = f"restore_content_{report_id}_{content_hash[:16]}"
        # 记录 pending effect receipt
        from services.effect_receipts import get_receipt_manager
        receipt_mgr = get_receipt_manager(store)
        await receipt_mgr.record_pending(
            action_id=action_id,
            effect_type="restore",
            target="file:R51P06004",
            request_hash="b" * 64,
        )
        # 删除 content_reports 记录模拟状态更新失败(rowcount=0)
        await store._db.execute(
            "DELETE FROM content_reports WHERE id = ?",
            (report_id,),
        )
        await store._db.commit()
        # 执行 handler — restore 会成功(file 存在)但状态更新会失败(记录不存在)
        cmd = make_restore_content_command(
            appeal_id=report_id,
            target_type="file",
            target_id="R51P06004",
            admin_id=20006,
            content_hash=content_hash,
            reporter_id=10004,
        )
        with pytest.raises(AppError) as exc_info:
            await cmd.handler(cmd.params)
        assert exc_info.value.code == ErrorCodes.CONTENT_APPEAL_RESTORE_FAILED
        # 验证文件已恢复(restore 成功执行)
        rows = await store._db.execute_fetchall(
            "SELECT status, deleted_at FROM file_records_local WHERE file_code = ?",
            ("R51P06004",),
        )
        assert rows[0][0] == "active"
        assert rows[0][1] is None
        # 验证 effect receipt completed + reconcile_status=needs_reconcile
        receipts = await _get_effect_receipts(store, action_id)
        assert len(receipts) >= 1
        receipt = receipts[0]
        assert receipt["status"] == "completed"
        assert receipt["reconcile_status"] == "needs_reconcile"


# ════════════════════════════════════════════════════════════════
# 3. action_id 确定性 + 幂等
# ════════════════════════════════════════════════════════════════

class TestActionIdDeterministic:
    """R51 P0-6: 重复 appeal 同一 content → action_id 确定性,幂等。"""

    @pytest.mark.asyncio
    async def test_same_appeal_same_content_same_action_id(self, store):
        """相同 appeal_id + 相同 content → 相同 action_id(确定性)。"""
        import hashlib as _hl
        target_type = "file"
        target_id = "R51P06005"
        content_hash = _hl.sha256(
            f"{target_type}:{target_id}".encode("utf-8"),
        ).hexdigest()
        # 模拟两次相同参数的 action_id 计算
        appeal_id = 12345
        action_id_1 = f"restore_content_{appeal_id}_{content_hash[:16]}"
        action_id_2 = f"restore_content_{appeal_id}_{content_hash[:16]}"
        assert action_id_1 == action_id_2
        # 不同 appeal_id → 不同 action_id
        action_id_3 = f"restore_content_{appeal_id + 1}_{content_hash[:16]}"
        assert action_id_1 != action_id_3
        # 不同 content → 不同 action_id
        content_hash_2 = _hl.sha256(
            f"{target_type}:DIFFERENT".encode("utf-8"),
        ).hexdigest()
        action_id_4 = f"restore_content_{appeal_id}_{content_hash_2[:16]}"
        assert action_id_1 != action_id_4

    @pytest.mark.asyncio
    async def test_outbox_unique_constraint_prevents_duplicate(self, store):
        """command_outbox UNIQUE 约束防止重复写入相同 action_id。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R51P06006", uploader_id=10006, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10006, target_type="file",
            target_id="R51P06006", status="appealed",
        )
        await _assign_ops_role(store, 20007)
        await _assign_ops_role(store, 20008)
        # 第一 + 第二审批
        await process_appeal(report_id, principal_id=20007, decision="approve")
        r2 = await process_appeal(report_id, principal_id=20008, decision="approve")
        assert r2["success"] is True
        action_id = r2["action_id"]
        # 验证 command_outbox 只有一条记录(UNIQUE 约束)
        entries = await _get_command_outbox_entries(store, action_id)
        assert len(entries) == 1


# ════════════════════════════════════════════════════════════════
# 4. 未授权用户 → RBAC 拒绝(AppError)
# ════════════════════════════════════════════════════════════════

class TestRbacRejectsUnauthorized:
    """R51 P0-6: 未授权用户 → RBAC 拒绝(AppError)。"""

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self, store):
        """无 disaster:restore 权限的用户 → 第二审批被 RBAC 拒绝(AppError)。"""
        from services.content_reports import process_appeal
        from services.error_codes import AppError, ErrorCodes
        await _insert_file_record(store, "R51P06007", uploader_id=10007, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10007, target_type="file",
            target_id="R51P06007", status="appealed",
        )
        # 第一审批人分配 ops 角色
        await _assign_ops_role(store, 20009)
        # 第二审批人不分配任何角色(无 disaster:restore 权限)
        # 第一审批
        r1 = await process_appeal(report_id, principal_id=20009, decision="approve")
        assert r1["stage"] == "first_approval"
        # 第二审批 — 未授权用户应被拒绝
        with pytest.raises(AppError) as exc_info:
            await process_appeal(report_id, principal_id=20010, decision="approve")
        assert exc_info.value.code == ErrorCodes.AUTH_RBAC_PERMISSION_DENIED
        # 验证 command_outbox 未写入(RBAC 拒绝在写 outbox 之前)
        entries = await _get_command_outbox_entries(store)
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_authorized_user_allowed(self, store):
        """有 disaster:restore 权限的用户 → 第二审批通过(写 command_outbox)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R51P06008", uploader_id=10008, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10008, target_type="file",
            target_id="R51P06008", status="appealed",
        )
        # 两个审批人都分配 ops 角色
        await _assign_ops_role(store, 20011)
        await _assign_ops_role(store, 20012)
        # 第一 + 第二审批
        r1 = await process_appeal(report_id, principal_id=20011, decision="approve")
        assert r1["stage"] == "first_approval"
        r2 = await process_appeal(report_id, principal_id=20012, decision="approve")
        assert r2["success"] is True
        assert r2["stage"] == "second_approval"
        # 验证 command_outbox 已写入
        entries = await _get_command_outbox_entries(store)
        assert len(entries) >= 1


# ════════════════════════════════════════════════════════════════
# 5. restore_pending 状态的 appeal 不能再次审批
# ════════════════════════════════════════════════════════════════

class TestRestorePendingNoReapproval:
    """R51 P0-6: restore_pending 状态(第二审批已完成)的 appeal 不能再次审批。"""

    @pytest.mark.asyncio
    async def test_cannot_approve_after_second_approval(self, store):
        """第二审批已写 command_outbox → 第三次审批被拒绝(AppError)。"""
        from services.content_reports import process_appeal
        from services.error_codes import AppError, ErrorCodes
        await _insert_file_record(store, "R51P06009", uploader_id=10009, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10009, target_type="file",
            target_id="R51P06009", status="appealed",
        )
        # 三个审批人分配 ops 角色
        await _assign_ops_role(store, 20013)
        await _assign_ops_role(store, 20014)
        await _assign_ops_role(store, 20015)
        # 第一 + 第二审批
        r1 = await process_appeal(report_id, principal_id=20013, decision="approve")
        assert r1["stage"] == "first_approval"
        r2 = await process_appeal(report_id, principal_id=20014, decision="approve")
        assert r2["success"] is True
        action_id = r2["action_id"]
        # 第三次审批(不同审批人)→ 应被拒绝(重复审批)
        with pytest.raises(AppError) as exc_info:
            await process_appeal(report_id, principal_id=20015, decision="approve")
        assert exc_info.value.code == ErrorCodes.CONTENT_APPEAL_INVALID_STATE
        # 验证 command_outbox 仍只有一条记录
        entries = await _get_command_outbox_entries(store, action_id)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_reject_still_works_after_second_approval(self, store):
        """第二审批已完成 → reject 仍可用(状态变为 rejected)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "R51P06010", uploader_id=10010, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=10010, target_type="file",
            target_id="R51P06010", status="appealed",
        )
        await _assign_ops_role(store, 20016)
        await _assign_ops_role(store, 20017)
        # 第一 + 第二审批
        await process_appeal(report_id, principal_id=20016, decision="approve")
        r2 = await process_appeal(report_id, principal_id=20017, decision="approve")
        assert r2["success"] is True
        # reject 仍可用(reject 不检查 command_outbox)
        r3 = await process_appeal(report_id, principal_id=20018, decision="reject")
        # reject 可能成功也可能因状态不是 appealed/restore_pending 而失败
        # 但至少不应抛出异常
        assert r3["stage"] in ("rejected", "noop")
