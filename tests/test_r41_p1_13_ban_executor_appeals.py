"""R41 P1-13: 临时封禁自动执行器 + 申诉处理测试。

被测目标:
- services/content_reports.py: cleanup_expired_bans (自动解封 + 通知 + 审计)
- services/content_reports.py: process_appeal (2 人审批状态机)
- services/content_reports.py: _restore_content_internal (恢复内容)
- services/content_reports.py: appeal_report (用户申诉)

测试场景:
1. cleanup_expired_bans 解封过期用户 + 写 audit_log(auto_unban) + 发通知(ban_expired)
2. cleanup_expired_bans 不解封未过期用户
3. process_appeal reject → status=rejected + 通知申诉者(appeal_rejected)
4. process_appeal first approve → status=restore_pending + audit_log(appeal_first_approval)
5. process_appeal second approve (different admin) → restore + status=resolved + 通知举报者
6. process_appeal 同一审批人不能审批两次
7. process_appeal 举报者不能审批自己的申诉
8. process_appeal 非法 decision 拒绝
9. _restore_content_internal 恢复文件(撤销软删除)
10. _restore_content_internal 恢复用户(解封)

测试策略:
- 使用真实 SQLite 临时文件数据库,通过 _cs_module._store 共享给所有模块
- 通过直接 INSERT 插入测试数据(用户 / 文件记录 / 举报)
- 用 SQL 直接修改 ban_expires_at 为过去时间模拟封禁到期
"""
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
    task_center / collections)的 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r41_p1_13_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        # R51 P0-6: 初始化 RBAC 默认角色(第二审批需要 disaster:restore 权限)
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


async def _get_notifications(store, user_id: int, ntype: str = None) -> list[dict]:
    """查询用户通知(可按类型过滤)。"""
    if ntype:
        rows = await store._db.execute_fetchall(
            "SELECT id, user_id, type, payload FROM notifications "
            "WHERE user_id = ? AND type = ? ORDER BY id ASC",
            (user_id, ntype),
        )
    else:
        rows = await store._db.execute_fetchall(
            "SELECT id, user_id, type, payload FROM notifications "
            "WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        )
    return [
        {
            "id": r[0], "user_id": int(r[1]), "type": r[2],
            "payload": json.loads(r[3]) if r[3] else {},
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


# ════════════════════════════════════════════════════════════════
# 1. cleanup_expired_bans 测试
# ════════════════════════════════════════════════════════════════

class TestCleanupExpiredBans:
    """cleanup_expired_bans 自动解封执行器。"""

    @pytest.mark.asyncio
    async def test_unban_expired_user(self, store):
        """过期封禁用户被自动解封。"""
        from services.content_reports import cleanup_expired_bans
        # 插入过期封禁用户(ban_expires_at 在过去)
        past_time = (datetime.now() - timedelta(hours=1)).isoformat()
        await _insert_user(store, 4001, is_banned=1, ban_expires_at=past_time)
        # 执行清理
        count = await cleanup_expired_bans()
        assert count == 1
        # 验证用户已解封
        rows = await store._db.execute_fetchall(
            "SELECT is_banned, ban_expires_at FROM users_local WHERE user_id = ?",
            (4001,),
        )
        assert int(rows[0][0]) == 0
        assert rows[0][1] is None

    @pytest.mark.asyncio
    async def test_not_unban_active_ban(self, store):
        """未过期封禁用户不被解封。"""
        from services.content_reports import cleanup_expired_bans
        future_time = (datetime.now() + timedelta(days=1)).isoformat()
        await _insert_user(store, 4002, is_banned=1, ban_expires_at=future_time)
        count = await cleanup_expired_bans()
        assert count == 0
        rows = await store._db.execute_fetchall(
            "SELECT is_banned FROM users_local WHERE user_id = ?",
            (4002,),
        )
        assert int(rows[0][0]) == 1

    @pytest.mark.asyncio
    async def test_unban_writes_audit_log(self, store):
        """解封后写 audit_log(action='auto_unban')。"""
        from services.content_reports import cleanup_expired_bans
        past_time = (datetime.now() - timedelta(hours=2)).isoformat()
        await _insert_user(store, 4003, is_banned=1, ban_expires_at=past_time)
        await cleanup_expired_bans()
        logs = await _get_audit_logs(store, "auto_unban")
        assert len(logs) >= 1
        log = logs[0]
        assert log["target_type"] == "user"
        assert log["target_id"] == "4003"
        assert log["details"].get("executor") == "cleanup_expired_bans"

    @pytest.mark.asyncio
    async def test_unban_sends_notification(self, store):
        """解封后发通知(type='ban_expired')给用户。"""
        from services.content_reports import cleanup_expired_bans
        past_time = (datetime.now() - timedelta(hours=3)).isoformat()
        await _insert_user(store, 4004, is_banned=1, ban_expires_at=past_time)
        await cleanup_expired_bans()
        notifs = await _get_notifications(store, 4004, "ban_expired")
        assert len(notifs) >= 1
        notif = notifs[0]
        assert notif["payload"].get("user_id") == 4004
        assert "unbanned_at" in notif["payload"]

    @pytest.mark.asyncio
    async def test_unban_multiple_users(self, store):
        """批量解封多个过期用户。"""
        from services.content_reports import cleanup_expired_bans
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        await _insert_user(store, 4010, is_banned=1, ban_expires_at=past)
        await _insert_user(store, 4011, is_banned=1, ban_expires_at=past)
        await _insert_user(store, 4012, is_banned=1, ban_expires_at=past)
        count = await cleanup_expired_bans()
        assert count == 3

    @pytest.mark.asyncio
    async def test_no_expired_bans_returns_zero(self, store):
        """无过期封禁时返回 0。"""
        from services.content_reports import cleanup_expired_bans
        count = await cleanup_expired_bans()
        assert count == 0

    @pytest.mark.asyncio
    async def test_permanent_ban_not_unbanned(self, store):
        """永久封禁(ban_expires_at=NULL)不被解封。"""
        from services.content_reports import cleanup_expired_bans
        await _insert_user(store, 4020, is_banned=1, ban_expires_at=None)
        count = await cleanup_expired_bans()
        assert count == 0
        rows = await store._db.execute_fetchall(
            "SELECT is_banned FROM users_local WHERE user_id = ?",
            (4020,),
        )
        assert int(rows[0][0]) == 1


# ════════════════════════════════════════════════════════════════
# 2. _restore_content_internal 测试
# ════════════════════════════════════════════════════════════════

class TestRestoreContentInternal:
    """_restore_content_internal 恢复内容(撤销软删除 / 解封用户)。"""

    @pytest.mark.asyncio
    async def test_restore_file(self, store):
        """恢复文件:撤销软删除(deleted_at=NULL, status='active')。"""
        from services.content_reports import _restore_content_internal
        # 插入已软删除的文件
        await _insert_file_record(store, "RESTORE001", uploader_id=5001, deleted=True)
        ok = await _restore_content_internal("file", "RESTORE001", admin_id=999)
        assert ok is True
        # 验证文件已恢复
        rows = await store._db.execute_fetchall(
            "SELECT status, deleted_at FROM file_records_local WHERE file_code = ?",
            ("RESTORE001",),
        )
        assert rows[0][0] == "active"
        assert rows[0][1] is None

    @pytest.mark.asyncio
    async def test_restore_nonexistent_file(self, store):
        """恢复不存在的文件 → 返回 False。"""
        from services.content_reports import _restore_content_internal
        ok = await _restore_content_internal("file", "NONEXISTENT", admin_id=999)
        assert ok is False

    @pytest.mark.asyncio
    async def test_restore_user_unban(self, store):
        """恢复用户:清除 is_banned + ban_expires_at。"""
        from services.content_reports import _restore_content_internal
        await _insert_user(store, 5002, is_banned=1,
                          ban_expires_at=(datetime.now() + timedelta(days=1)).isoformat())
        ok = await _restore_content_internal("user", "5002", admin_id=999)
        assert ok is True
        rows = await store._db.execute_fetchall(
            "SELECT is_banned, ban_expires_at FROM users_local WHERE user_id = ?",
            (5002,),
        )
        assert int(rows[0][0]) == 0
        assert rows[0][1] is None

    @pytest.mark.asyncio
    async def test_restore_writes_audit_log(self, store):
        """恢复操作写 audit_log(action='restore')。"""
        from services.content_reports import _restore_content_internal
        await _insert_file_record(store, "RESTORE002", uploader_id=5003, deleted=True)
        await _restore_content_internal("file", "RESTORE002", admin_id=888)
        logs = await _get_audit_logs(store, "restore")
        assert len(logs) >= 1
        assert logs[0]["actor_id"] == 888
        assert logs[0]["target_type"] == "file"


# ════════════════════════════════════════════════════════════════
# 3. process_appeal — 拒绝申诉测试
# ════════════════════════════════════════════════════════════════

class TestProcessAppealReject:
    """process_appeal decision='reject' → 维持下架 + 通知申诉者。"""

    @pytest.mark.asyncio
    async def test_reject_appeal(self, store):
        """拒绝申诉 → status=rejected + 通知申诉者(appeal_rejected)。"""
        from services.content_reports import process_appeal
        # 创建已申诉的举报(reporter=6001, target=file)
        await _insert_file_record(store, "APPEAL001", uploader_id=6001, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6001, target_type="file",
            target_id="APPEAL001", status="appealed",
        )
        # 管理员拒绝
        result = await process_appeal(report_id, principal_id=7001, decision="reject")
        assert result["success"] is True
        assert result["stage"] == "rejected"
        assert result["restored"] is False
        # 验证状态
        assert await _get_report_status(store, report_id) == "rejected"
        # 验证通知(appeal_rejected 发给 reporter)
        notifs = await _get_notifications(store, 6001, "appeal_rejected")
        assert len(notifs) >= 1

    @pytest.mark.asyncio
    async def test_reject_writes_audit_log(self, store):
        """拒绝申诉写 audit_log(action='appeal_rejected')。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL002", uploader_id=6002, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6002, target_type="file",
            target_id="APPEAL002", status="appealed",
        )
        await process_appeal(report_id, principal_id=7002, decision="reject", note="违规内容")
        logs = await _get_audit_logs(store, "appeal_rejected")
        assert len(logs) >= 1
        assert logs[-1]["actor_id"] == 7002


# ════════════════════════════════════════════════════════════════
# 4. process_appeal — 2 人审批测试
# ════════════════════════════════════════════════════════════════

class TestProcessAppealTwoPersonApproval:
    """process_appeal 2 人审批状态机:appealed → restore_pending → resolved。"""

    @pytest.mark.asyncio
    async def test_first_approval(self, store):
        """第一审批人 approve → status=restore_pending + audit_log(appeal_first_approval)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL003", uploader_id=6003, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6003, target_type="file",
            target_id="APPEAL003", status="appealed",
        )
        result = await process_appeal(report_id, principal_id=8001, decision="approve")
        assert result["success"] is True
        assert result["stage"] == "first_approval"
        assert result["restored"] is False
        # 验证状态
        assert await _get_report_status(store, report_id) == "restore_pending"
        # 验证审计日志
        logs = await _get_audit_logs(store, "appeal_first_approval")
        assert len(logs) >= 1
        assert logs[-1]["actor_id"] == 8001

    @pytest.mark.asyncio
    async def test_second_approval_restores_content(self, store):
        """第二审批人 approve(不同人)→ 写 command_outbox + restored=False(R51 P0-6: 由 ApprovalExecutor 异步执行)。"""
        from services.content_reports import process_appeal
        from services import rbac as _rbac_mod
        await _insert_file_record(store, "APPEAL004", uploader_id=6004, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6004, target_type="file",
            target_id="APPEAL004", status="appealed",
        )
        # R51 P0-6: 第二审批需要 disaster:restore 权限,给审批人分配 ops 角色
        await _rbac_mod.assign_role(8002, "ops", assigned_by=0)
        await _rbac_mod.assign_role(8003, "ops", assigned_by=0)
        # 第一审批人
        r1 = await process_appeal(report_id, principal_id=8002, decision="approve")
        assert r1["stage"] == "first_approval"
        # 第二审批人(不同人)
        r2 = await process_appeal(report_id, principal_id=8003, decision="approve")
        assert r2["success"] is True
        assert r2["stage"] == "second_approval"
        # R51 P0-6: restored=False(restore 由 ApprovalExecutor 异步执行)
        assert r2["restored"] is False
        # 验证状态保持 restore_pending(不在此处变为 resolved)
        assert await _get_report_status(store, report_id) == "restore_pending"
        # 验证文件尚未恢复(restore 由 executor 执行)
        rows = await store._db.execute_fetchall(
            "SELECT status, deleted_at FROM file_records_local WHERE file_code = ?",
            ("APPEAL004",),
        )
        assert rows[0][0] == "deleted"
        assert rows[0][1] is not None
        # 验证审计日志(第二审批)
        logs = await _get_audit_logs(store, "appeal_second_approval")
        assert len(logs) >= 1
        assert logs[-1]["actor_id"] == 8003
        # 验证 command_outbox 已写入(由 ApprovalExecutor 消费)
        outbox_rows = await store._db.execute_fetchall(
            "SELECT command_type, status FROM command_outbox "
            "WHERE approval_id = ? AND command_type = 'restore_content'",
            (report_id,),
        )
        assert len(outbox_rows) >= 1
        assert outbox_rows[0][0] == "restore_content"
        assert outbox_rows[0][1] == "pending"

    @pytest.mark.asyncio
    async def test_same_approver_twice_rejected(self, store):
        """同一审批人不能审批两次(需要 2 个不同管理员)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL005", uploader_id=6005, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6005, target_type="file",
            target_id="APPEAL005", status="appealed",
        )
        # 第一审批人
        r1 = await process_appeal(report_id, principal_id=8004, decision="approve")
        assert r1["stage"] == "first_approval"
        # 同一审批人再次 approve → 拒绝
        r2 = await process_appeal(report_id, principal_id=8004, decision="approve")
        assert r2["success"] is False
        assert "同一审批人" in r2["error"] or "2 个不同" in r2["error"]

    @pytest.mark.asyncio
    async def test_reporter_cannot_approve_own_appeal(self, store):
        """举报者不能审批自己的申诉。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL006", uploader_id=6006, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6006, target_type="file",
            target_id="APPEAL006", status="appealed",
        )
        # 举报者自己审批 → 拒绝
        result = await process_appeal(report_id, principal_id=6006, decision="approve")
        assert result["success"] is False
        assert "举报者" in result["error"] or "自己" in result["error"]


# ════════════════════════════════════════════════════════════════
# 5. process_appeal — 边界条件测试
# ════════════════════════════════════════════════════════════════

class TestProcessAppealEdgeCases:
    """process_appeal 边界条件与错误处理。"""

    @pytest.mark.asyncio
    async def test_invalid_decision(self, store):
        """非法 decision → 拒绝。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL007", uploader_id=6007, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6007, target_type="file",
            target_id="APPEAL007", status="appealed",
        )
        result = await process_appeal(report_id, principal_id=9001, decision="invalid")
        assert result["success"] is False
        assert "非法" in result["error"]
        assert result["stage"] == "noop"

    @pytest.mark.asyncio
    async def test_nonexistent_report(self, store):
        """举报不存在 → 失败。"""
        from services.content_reports import process_appeal
        result = await process_appeal(99999, principal_id=9002, decision="approve")
        assert result["success"] is False
        assert "不存在" in result["error"]

    @pytest.mark.asyncio
    async def test_wrong_status_noop(self, store):
        """举报状态不允许处理(pending 状态不能处理申诉)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL008", uploader_id=6008, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6008, target_type="file",
            target_id="APPEAL008", status="pending",
        )
        result = await process_appeal(report_id, principal_id=9003, decision="approve")
        assert result["success"] is False
        assert "状态不允许" in result["error"]

    @pytest.mark.asyncio
    async def test_reject_from_restore_pending(self, store):
        """restore_pending 状态也可被 reject(任一审批人可拒绝)。"""
        from services.content_reports import process_appeal
        await _insert_file_record(store, "APPEAL009", uploader_id=6009, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6009, target_type="file",
            target_id="APPEAL009", status="appealed",
        )
        # 第一审批人 approve → restore_pending
        r1 = await process_appeal(report_id, principal_id=9101, decision="approve")
        assert r1["stage"] == "first_approval"
        # 第二审批人 reject
        r2 = await process_appeal(report_id, principal_id=9102, decision="reject")
        assert r2["success"] is True
        assert r2["stage"] == "rejected"
        # 验证状态
        assert await _get_report_status(store, report_id) == "rejected"


# ════════════════════════════════════════════════════════════════
# 6. appeal_report 测试(用户提交申诉)
# ════════════════════════════════════════════════════════════════

class TestAppealReport:
    """appeal_report 用户提交申诉。"""

    @pytest.mark.asyncio
    async def test_appeal_takedown_report(self, store):
        """对 takedown 状态的举报提交申诉 → status=appealed。"""
        from services.content_reports import appeal_report
        await _insert_file_record(store, "APPEAL010", uploader_id=6010, deleted=True)
        report_id = await _insert_report(
            store, reporter_id=6010, target_type="file",
            target_id="APPEAL010", status="takedown",
        )
        ok = await appeal_report(report_id, user_id=6010, appeal_text="内容无违规")
        assert ok is True
        assert await _get_report_status(store, report_id) == "appealed"

    @pytest.mark.asyncio
    async def test_appeal_non_takedown_fails(self, store):
        """对非 takedown 状态的举报申诉 → 失败。"""
        from services.content_reports import appeal_report
        report_id = await _insert_report(
            store, reporter_id=6011, target_type="file",
            target_id="APPEAL011", status="pending",
        )
        ok = await appeal_report(report_id, user_id=6011, appeal_text="test")
        assert ok is False

    @pytest.mark.asyncio
    async def test_appeal_nonexistent_report(self, store):
        """对不存在的举报申诉 → 失败。"""
        from services.content_reports import appeal_report
        ok = await appeal_report(99999, user_id=6012, appeal_text="test")
        assert ok is False
