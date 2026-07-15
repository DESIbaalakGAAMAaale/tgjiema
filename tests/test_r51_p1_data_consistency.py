"""R51 P1 数据一致性整改测试(5 项 P1 issue × 3+ 测试 = 15+ 测试)。

被测目标:
- P1-1: ``services.data_lifecycle.delete_user_data`` 状态机 + step receipts + backup marker
- P1-2: ``services.entitlements.set_user_plan`` 事务化 + ``get_quota`` fail-closed
- P1-3: ``services.collections.update_collection`` 强制 expected_version + bypass_cas
- P1-4: ``services.task_center.record_task`` 拒绝未知类型/状态 + list 函数 fail-closed
- P1-5: ``services.repair_console.execute_repair`` 风险等级 + 审批三要素校验

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 ``CacheStore.init()`` 创建表
- 直接 INSERT 测试数据到 users_local / file_records_local / collections / tasks /
  command_executions 等表
- 通过 ``unittest.mock.patch.object`` 模拟 DB 异常 / backup engine / repair 函数
- 中文注释 + 中文日志,英文 raise 消息(遵循项目规范)
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ── P1-2 辅助: 构造真实 Plan 对象(避免 MagicMock 干扰数值比较) ──
def _make_real_plans():
    """构造包含真实 int 值的 _PLANS 字典(避免 conftest MagicMock settings 导致
    ``settings.BASIC_DAILY_QUOTA`` 返回 MagicMock,使 ``max(0, daily_limit - used)``
    抛 TypeError)。"""
    from services.entitlements import Plan
    return {
        "free": Plan(
            name="free", daily_quota=3, external_daily_quota=1,
            max_file_size=50 * 1024 * 1024, max_concurrent=1,
            retention_days=7, priority_queue="normal", max_collection_items=10,
        ),
        "basic": Plan(
            name="basic", daily_quota=20, external_daily_quota=5,
            max_file_size=500 * 1024 * 1024, max_concurrent=2,
            retention_days=30, priority_queue="normal", max_collection_items=50,
        ),
        "premium": Plan(
            name="premium", daily_quota=100, external_daily_quota=20,
            max_file_size=2 * 1024 * 1024 * 1024, max_concurrent=5,
            retention_days=90, priority_queue="high", max_collection_items=200,
        ),
    }


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p1_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 测试数据辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_user(store, user_id: int, level: str = "free"):
    """插入测试用户到 users_local 表。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO users_local
           (user_id, username, first_name, membership_level,
            daily_decode_quota, quota_used_today, quota_date, can_upload,
            is_banned, created_at, updated_at, crdb_synced)
           VALUES (?, ?, ?, ?, ?, 0, ?, 1, 0, ?, ?, 1)""",
        (user_id, f"user_{user_id}", f"User{user_id}", level, 3, now, now, now),
    )
    await store._db.commit()


async def _insert_file_record(store, file_code: str, uploader_id: int):
    """插入测试 file_records_local 记录。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO file_records_local
           (file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
            file_types, status, request_count, create_time, updated_at, crdb_synced)
           VALUES (?, ?, 100, 1, 'photo', 'active', 0, ?, ?, 1)""",
        (file_code, uploader_id, now, now),
    )
    await store._db.commit()


async def _insert_code(store, code: str, uploader_id: int):
    """插入测试 codes_local 记录。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO codes_local
           (code, file_record_code, uploader_id, status, created_at, crdb_synced)
           VALUES (?, ?, ?, 'active', ?, 1)""",
        (code, f"frec_{code}", uploader_id, now),
    )
    await store._db.commit()


async def _insert_collection(store, owner_id: int, name: str = "test_coll") -> int:
    """插入测试 collections 记录,返回自增 id。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    cursor = await store._db.execute(
        """INSERT INTO collections (name, code, owner_id, description,
                                     version, item_count, status,
                                     created_at, updated_at)
           VALUES (?, ?, ?, '', 1, 0, 'active', ?, ?)""",
        (name, f"code_{name}_{owner_id}", owner_id, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


async def _insert_notification(store, user_id: int):
    """插入测试 notifications 记录。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT INTO notifications (user_id, type, payload, is_read, created_at)
           VALUES (?, 'system', '{}', 0, ?)""",
        (user_id, now),
    )
    await store._db.commit()


async def _insert_task(store, task_type: str, user_id: int, status: str = "pending") -> int:
    """插入测试 tasks 记录,返回自增 id。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    cursor = await store._db.execute(
        """INSERT INTO tasks (task_type, user_id, status, progress, eta_seconds,
                              payload, result, error, trace_id,
                              created_at, updated_at)
           VALUES (?, ?, ?, 0, 0, '{}', '', '', '', ?, ?)""",
        (task_type, user_id, status, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


async def _insert_command_execution(
    store, action_id: str, principal_id: int, status: str = "approved",
    request_hash: str = "",
):
    """插入测试 command_executions 记录(用于审批验证)。

    R52 P0-5: 状态机统一为 pending → approved → executing → executed/failed,
    审批通过后执行前的状态为 'approved'(旧版 'executed' 语义冲突已废弃)。
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO command_executions
           (action_id, command_type, principal_id, status, owner,
            lease_until, request_hash, result, created_at, updated_at)
           VALUES (?, 'repair', ?, ?, NULL, NULL, ?, '', ?, ?)""",
        (action_id, principal_id, status, request_hash, now, now),
    )
    await store._db.commit()


async def _insert_quota_reservation(store, user_id: int, amount: int = 1):
    """插入测试 quota_reservations 记录(用于配额消耗统计)。

    R53 P1-4: created_at 使用 UTC aware timestamp(ISO 带 +00:00),
    与生产代码 reserve() 的写入格式一致,匹配 BILLING_TIMEZONE UTC 边界查询。
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    await store._db.execute(
        """INSERT INTO quota_reservations
           (id, user_id, amount, reason, status, actual_amount,
            created_at, settled_at, expired_at)
           VALUES (?, ?, ?, 'decode', 'settled', ?, ?, ?, NULL)""",
        (f"res_{user_id}_{amount}", user_id, amount, amount, now, now),
    )
    await store._db.commit()


async def _query_deletion_request(store, request_id: str) -> dict | None:
    """查询 deletion_requests 表记录。"""
    cursor = await store._db.execute(
        "SELECT request_id, user_id, admin_id, status, current_step, "
        "step_receipts, started_at, completed_at, failed_at, "
        "failure_reason, failed_step, created_at "
        "FROM deletion_requests WHERE request_id = ?",
        (request_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    try:
        receipts = json.loads(row[5]) if row[5] else {}
    except Exception:
        receipts = {}
    return {
        "request_id": row[0], "user_id": row[1], "admin_id": row[2],
        "status": row[3], "current_step": row[4], "step_receipts": receipts,
        "started_at": row[6], "completed_at": row[7], "failed_at": row[8],
        "failure_reason": row[9], "failed_step": row[10], "created_at": row[11],
    }


# ════════════════════════════════════════════════════════════════
# P1-1: Data Lifecycle 状态机 + step receipts + backup marker
# ════════════════════════════════════════════════════════════════

class TestP1_1_DataLifecycleStateMachine:
    """P1-1: 删除用户数据状态机测试。"""

    @pytest.mark.asyncio
    async def test_p1_1_delete_user_data_success_state_machine(self, real_store):
        """测试: 删除用户数据成功 → deletion_requests 状态变为 completed,所有 step 都有 receipt。"""
        from services import data_lifecycle

        # 准备测试数据
        user_id = 10001
        await _insert_user(real_store, user_id, level="basic")
        await _insert_file_record(real_store, "FC_P1_1_SUCCESS", user_id)
        await _insert_code(real_store, "CODE_P1_1_SUCCESS", user_id)
        await _insert_collection(real_store, user_id, "coll_success")
        await _insert_notification(real_store, user_id)
        await _insert_task(real_store, "upload", user_id, status="running")

        # 执行删除
        result = await data_lifecycle.delete_user_data(user_id, admin_id=999)
        assert result is True, "删除应返回 True"

        # 验证 deletion_requests 表存在 completed 记录
        cursor = await real_store._db.execute(
            "SELECT request_id, status, step_receipts FROM deletion_requests "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, "应创建 deletion_requests 记录"
        request_id, status, receipts_raw = row[0], row[1], row[2]
        assert status == "completed", f"状态应为 completed,实际: {status}"

        receipts = json.loads(receipts_raw) if receipts_raw else {}
        # 验证所有 6 个 step 都有 receipt 且 status=success
        expected_steps = data_lifecycle.DELETE_STEPS
        for step_name in expected_steps:
            assert step_name in receipts, f"缺少 step receipt: {step_name}"
            assert receipts[step_name]["status"] == "success", (
                f"step {step_name} 应为 success,实际: {receipts[step_name]['status']}"
            )

        # 验证 users_local 已标记删除
        cursor = await real_store._db.execute(
            "SELECT is_banned, deleted_at FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        urow = await cursor.fetchone()
        assert urow is not None, "用户记录应仍存在(软删除)"
        assert urow[0] == 1, "is_banned 应为 1"
        assert urow[1] is not None, "deleted_at 应已设置"

        # 验证 notifications 已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        nrow = await cursor.fetchone()
        assert nrow[0] == 0, "notifications 应已物理删除"

        # 验证 tasks 已标记 cancelled
        cursor = await real_store._db.execute(
            "SELECT status FROM tasks WHERE user_id = ?",
            (user_id,),
        )
        trow = await cursor.fetchone()
        assert trow[0] == "cancelled", "task 应标记为 cancelled"

    @pytest.mark.asyncio
    async def test_p1_1_delete_user_data_failure_marks_failed(self, real_store):
        """测试: step 执行失败 → deletion_requests 标记 failed,raise AppError,事务回滚。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 10002
        await _insert_user(real_store, user_id)
        await _insert_file_record(real_store, "FC_P1_1_FAIL", user_id)

        # 模拟 step_users_local 阶段失败(通过 patch _execute_step_in_tx 让最后一个 step 失败)
        original_execute_step = data_lifecycle._execute_step_in_tx

        async def _mock_execute_step(tx, store, step_name, uid, now):
            if step_name == "step_users_local":
                # 模拟 users_local 更新失败
                raise RuntimeError("simulated_users_local_failure")
            return await original_execute_step(tx, store, step_name, uid, now)

        with patch.object(data_lifecycle, "_execute_step_in_tx", _mock_execute_step):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.delete_user_data(user_id, admin_id=999)

        # 验证错误码
        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_DELETE_STEP_FAILED
        assert "step_users_local" in str(exc_info.value)

        # 验证 deletion_requests 状态为 failed
        cursor = await real_store._db.execute(
            "SELECT status, failed_step, failure_reason FROM deletion_requests "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, "应创建 deletion_requests 记录"
        assert row[0] == "failed", f"状态应为 failed,实际: {row[0]}"
        assert row[1] == "step_users_local", f"failed_step 应为 step_users_local,实际: {row[1]}"
        assert "simulated_users_local_failure" in row[2], (
            f"failure_reason 应包含模拟错误,实际: {row[2]}"
        )

        # 验证事务回滚: users_local 应未被标记删除(因为 step_users_local 失败)
        cursor = await real_store._db.execute(
            "SELECT is_banned, deleted_at FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        urow = await cursor.fetchone()
        assert urow is not None, "用户记录应仍存在"
        # 事务回滚后,is_banned 应保持原值(0)
        assert urow[0] == 0, "事务回滚后 is_banned 应为 0(未删除)"

    @pytest.mark.asyncio
    async def test_p1_1_cleanup_expired_rejects_without_backup_marker(self, real_store):
        """测试: cleanup_expired_data 无 backup marker 时 raise AppError(BACKUP_MARKER_MISSING)。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        # Mock BackupEngine.get_last_successful_backup 返回 None(无备份)
        async def _mock_get_last_successful_backup(self):
            return None

        with patch(
            "services.backup_engine.BackupEngine.get_last_successful_backup",
            _mock_get_last_successful_backup,
        ):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.cleanup_expired_data(batch_size=10)

        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING

    @pytest.mark.asyncio
    async def test_p1_1_cleanup_expired_allows_with_backup_marker(self, real_store, monkeypatch):
        """测试: cleanup_expired_data 有 backup marker 时正常执行(skip_backup_check 兜底)。

        R53 P1-3: skip_backup_check=True 必须有 break-glass 审批,
        测试通过 BREAK_GLASS_APPROVED 环境变量授权。
        """
        from services import data_lifecycle

        # 设置保留期
        await data_lifecycle._ensure_retention_table()
        await data_lifecycle.set_retention(user_id=10003, days=7)

        # 插入已软删的 file_records_local
        import datetime as _dt
        await _insert_file_record(real_store, "FC_P1_1_CLEANUP", 10003)
        # 手动设置 deleted_at 为 8 天前(超过保留期)
        old_dt = (_dt.datetime.now() - _dt.timedelta(days=8)).isoformat()
        await real_store._db.execute(
            "UPDATE file_records_local SET deleted_at = ? WHERE file_code = ?",
            (old_dt, "FC_P1_1_CLEANUP"),
        )
        await real_store._db.commit()

        # R53 P1-3: skip_backup_check=True 需要 break-glass 审批
        # 测试场景: 设置 BREAK_GLASS_APPROVED 环境变量
        monkeypatch.setenv("BREAK_GLASS_APPROVED", "1")
        cleaned = await data_lifecycle.cleanup_expired_data(
            batch_size=10, skip_backup_check=True,
        )
        assert cleaned >= 1, "应清理至少 1 条 file_records_local"

        # 验证记录已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            ("FC_P1_1_CLEANUP",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "file_records_local 应已物理删除"


# ════════════════════════════════════════════════════════════════
# P1-2: Entitlements 事务化 + fail-closed
# ════════════════════════════════════════════════════════════════

class TestP1_2_EntitlementsTransactional:
    """P1-2: 套餐设置事务化 + 配额查询 fail-closed 测试。"""

    @pytest.mark.asyncio
    async def test_p1_2_set_user_plan_transactional_success(self, real_store, monkeypatch):
        """测试: _set_user_plan_internal 成功 → users_local + user_quota + audit_log 同事务写入。"""
        from services import entitlements

        # 替换 _PLANS 为真实 int 值(避免 conftest MagicMock settings 干扰)
        monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())

        user_id = 20001
        admin_id = 888
        await _insert_user(real_store, user_id, level="free")

        result = await entitlements._set_user_plan_internal(user_id, "premium", admin_id=admin_id)
        assert result is True, "_set_user_plan_internal 应返回 True"

        # 验证 users_local.membership_level 已更新
        cursor = await real_store._db.execute(
            "SELECT membership_level FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == "premium", f"membership_level 应为 premium,实际: {row[0]}"

        # 验证 user_quota 已写入(level + daily_quota)
        cursor = await real_store._db.execute(
            "SELECT level, daily_quota FROM user_quota WHERE user_id = ?",
            (user_id,),
        )
        qrow = await cursor.fetchone()
        assert qrow is not None, "user_quota 记录应存在"
        assert qrow[0] == "premium", f"level 应为 premium,实际: {qrow[0]}"
        assert qrow[1] == entitlements._PLANS["premium"].daily_quota

        # 验证 audit_log 已写入
        cursor = await real_store._db.execute(
            "SELECT action, target_id FROM audit_log "
            "WHERE actor_id = ? AND action = 'set_plan' ORDER BY id DESC LIMIT 1",
            (admin_id,),
        )
        arow = await cursor.fetchone()
        assert arow is not None, "audit_log 应有 set_plan 记录"
        assert arow[1] == str(user_id)

        # 验证 dirty_outbox 已写入(users_local + user_quota + audit_log)
        cursor = await real_store._db.execute(
            "SELECT table_name, COUNT(*) FROM dirty_outbox "
            "WHERE pk = ? OR table_name IN ('users_local', 'user_quota', 'audit_log') "
            "GROUP BY table_name",
            (str(user_id),),
        )
        rows = await cursor.fetchall()
        table_names = {r[0] for r in rows}
        assert "users_local" in table_names, "dirty_outbox 应包含 users_local"
        assert "user_quota" in table_names, "dirty_outbox 应包含 user_quota"

    @pytest.mark.asyncio
    async def test_p1_2_set_user_plan_transactional_rollback(self, real_store, monkeypatch):
        """测试: _set_user_plan_internal 中途失败 → 整个事务回滚,users_local 未更新。"""
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes

        # 替换 _PLANS 为真实 int 值(避免 conftest MagicMock settings 干扰)
        monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())

        user_id = 20002
        admin_id = 888
        await _insert_user(real_store, user_id, level="free")

        # Mock add_dirty_outbox 在第二次调用时失败(模拟事务中途失败)
        original_add_dirty = real_store.add_dirty_outbox
        call_count = {"n": 0}

        async def _failing_add_dirty(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise RuntimeError("simulated_dirty_outbox_failure")
            return await original_add_dirty(*args, **kwargs)

        with patch.object(real_store, "add_dirty_outbox", _failing_add_dirty):
            with pytest.raises(AppError) as exc_info:
                await entitlements._set_user_plan_internal(user_id, "premium", admin_id=admin_id)

        assert exc_info.value.code == ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED

        # 验证事务回滚: users_local.membership_level 应保持 'free'
        cursor = await real_store._db.execute(
            "SELECT membership_level FROM users_local WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == "free", (
            f"事务回滚后 membership_level 应为 free,实际: {row[0]}"
        )

        # 验证 user_quota 未写入(事务回滚)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM user_quota WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "事务回滚后 user_quota 不应有记录"

    @pytest.mark.asyncio
    async def test_p1_2_get_quota_fail_closed_on_db_error(self, real_store, monkeypatch):
        """测试: get_quota 查询失败时 raise AppError(QUOTA_QUERY_FAILED)(fail-closed)。"""
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes
        from config import settings

        # 替换 _PLANS 为真实 int 值(避免 conftest MagicMock settings 干扰)
        monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())
        # R53 P1-4: 设置真实 BILLING_TIMEZONE(避免 ZoneInfo 解析 MagicMock 失败)
        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")

        user_id = 20003
        await _insert_user(real_store, user_id)

        # Mock execute_fetchall 仅对 quota_reservations 查询抛异常
        # (get_user_local / get_user_quota 等其他查询正常执行,
        #  确保 AppError 由 quota_reservations 查询失败触发,而非 get_plan)
        original_fetchall = real_store._db.execute_fetchall

        async def _failing_fetchall(*args, **kwargs):
            sql = args[0] if args else kwargs.get("sql", "")
            if "quota_reservations" in str(sql):
                raise RuntimeError("simulated_db_connection_lost")
            return await original_fetchall(*args, **kwargs)

        with patch.object(real_store._db, "execute_fetchall", _failing_fetchall):
            with pytest.raises(AppError) as exc_info:
                await entitlements.get_quota(user_id)

        assert exc_info.value.code == ErrorCodes.ENTITLEMENT_QUOTA_QUERY_FAILED
        # reason 不在 safe_params 白名单中,不会出现在 i18n 消息里,
        # 改为检查消息含 fail-closed 语义 + user_id
        assert "fail-closed" in str(exc_info.value)
        assert "20003" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_p1_2_get_quota_normal_returns_quota(self, real_store, monkeypatch):
        """测试: get_quota 正常情况返回 Quota 对象(回归测试)。"""
        from services import entitlements
        from config import settings

        # 替换 _PLANS 为真实 int 值(避免 conftest MagicMock settings 干扰)
        monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())
        # R53 P1-4: 设置真实 BILLING_TIMEZONE(避免 ZoneInfo 解析 MagicMock 失败)
        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")

        user_id = 20004
        await _insert_user(real_store, user_id, level="basic")
        # 插入今日 quota_reservations 记录(消耗 2)
        await _insert_quota_reservation(real_store, user_id, amount=2)

        quota = await entitlements.get_quota(user_id)
        assert quota is not None
        assert quota.used_today == 2, f"used_today 应为 2,实际: {quota.used_today}"
        # basic 套餐的 daily_quota 来自 settings
        expected_limit = entitlements._PLANS["basic"].daily_quota
        assert quota.daily_limit == expected_limit
        assert quota.remaining == max(0, expected_limit - 2)


# ════════════════════════════════════════════════════════════════
# P1-3: Collections CAS 强制 expected_version + bypass_cas
# ════════════════════════════════════════════════════════════════

class TestP1_3_CollectionsCasEnforcement:
    """P1-3: collections.update_collection 强制 CAS 测试。"""

    @pytest.mark.asyncio
    async def test_p1_3_update_requires_expected_version(self, real_store):
        """测试: 不传 expected_version 且不传 bypass_cas → raise AppError(VERSION_REQUIRED)。"""
        from services import collections
        from services.error_codes import AppError, ErrorCodes

        user_id = 30001
        coll_id = await _insert_collection(real_store, user_id, "coll_require_ev")

        with pytest.raises(AppError) as exc_info:
            await collections.update_collection(
                coll_id, name="new_name",
                # 不传 expected_version,也不传 bypass_cas
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED

    @pytest.mark.asyncio
    async def test_p1_3_update_cas_conflict_raises(self, real_store):
        """测试: expected_version 不匹配 → raise AppError(CAS_CONFLICT)。"""
        from services import collections
        from services.error_codes import AppError, ErrorCodes

        user_id = 30002
        coll_id = await _insert_collection(real_store, user_id, "coll_conflict")

        # 传入错误的 expected_version(实际 version=1,传 99)
        with pytest.raises(AppError) as exc_info:
            await collections.update_collection(
                coll_id, name="new_name", expected_version=99,
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_CAS_CONFLICT

    @pytest.mark.asyncio
    async def test_p1_3_update_bypass_cas_allows_admin(self, real_store):
        """测试: 私有方法 + 真实审批记录 允许运维/迁移绕过 CAS(显式 opt-in)。

        R53 P0-4: bypass 路径必须通过 _update_collection_without_cas 私有方法,
        严格校验 command_executions 表中的 status / principal_id / request_hash。
        """
        from services import collections

        user_id = 30003
        coll_id = await _insert_collection(real_store, user_id, "coll_bypass")

        # R53 P0-4: 插入真实审批记录(status=approved)
        await _insert_command_execution(
            real_store, "approval_bypass_p1_3", principal_id=user_id,
            status="approved", request_hash="hash_p1_3_bypass",
        )

        # R53 P0-4: 调用私有方法 + 真实审批 → 成功更新
        result = await collections._update_collection_without_cas(
            coll_id, name="migrated_name",
            principal_id=user_id,
            request_hash="hash_p1_3_bypass",
            approval_action_id="approval_bypass_p1_3",
            caller="test_migration",
        )
        assert result["success"] is True, "私有方法 + 真实审批应更新成功"
        assert result["new_version"] >= 2, "version 应递增"

        # 验证 name 已更新
        cursor = await real_store._db.execute(
            "SELECT name, version FROM collections WHERE id = ?",
            (coll_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == "migrated_name", f"name 应为 migrated_name,实际: {row[0]}"
        assert row[1] == result["new_version"]

    @pytest.mark.asyncio
    async def test_p1_3_update_cas_success_increments_version(self, real_store):
        """测试: expected_version 匹配 → CAS 更新成功,version 递增(回归测试)。"""
        from services import collections

        user_id = 30004
        coll_id = await _insert_collection(real_store, user_id, "coll_cas_ok")

        # 初始 version=1,传入 expected_version=1 应成功
        result = await collections.update_collection(
            coll_id, name="updated_name", expected_version=1,
        )
        assert result["success"] is True, "CAS 匹配应更新成功"
        assert result["conflict"] is False
        assert result["new_version"] == 2, f"version 应从 1 递增到 2,实际: {result['new_version']}"

    @pytest.mark.asyncio
    async def test_p1_3_bypass_cas_with_expected_version_rejected(self, real_store):
        """R53 P0-4: 公共 API 不再接受 bypass_cas 参数(传参应抛 TypeError)。"""
        from services import collections

        user_id = 30005
        coll_id = await _insert_collection(real_store, user_id, "coll_both")

        # R53 P0-4: 公共 API 已移除 bypass_cas 参数,传参应抛 TypeError
        with pytest.raises(TypeError):
            await collections.update_collection(
                coll_id, name="new_name",
                expected_version=1, bypass_cas=True,
            )


# ════════════════════════════════════════════════════════════════
# P1-4: Task Center 错误处理
# ════════════════════════════════════════════════════════════════

class TestP1_4_TaskCenterErrorHandling:
    """P1-4: task_center 拒绝未知类型/状态 + DB 异常 raise AppError 测试。"""

    @pytest.mark.asyncio
    async def test_p1_4_record_task_rejects_unknown_type(self, real_store):
        """测试: record_task 未知 task_type → raise AppError(UNKNOWN_TYPE)。"""
        from services import task_center
        from services.error_codes import AppError, ErrorCodes

        user_id = 40001
        await _insert_user(real_store, user_id)

        with pytest.raises(AppError) as exc_info:
            await task_center.record_task(
                user_id=user_id,
                task_type="invalid_type",  # 不在 TASK_TYPES 中
                status="pending",
                metadata={"file_code": "FC_P1_4_UNKNOWN_TYPE"},
            )
        assert exc_info.value.code == ErrorCodes.TASK_CENTER_UNKNOWN_TYPE
        # 验证错误信息包含原始 task_type 和允许的类型列表
        assert "invalid_type" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_p1_4_record_task_rejects_unknown_status(self, real_store):
        """测试: record_task 未知 status → raise AppError(UNKNOWN_STATUS)。"""
        from services import task_center
        from services.error_codes import AppError, ErrorCodes

        user_id = 40002
        await _insert_user(real_store, user_id)

        with pytest.raises(AppError) as exc_info:
            await task_center.record_task(
                user_id=user_id,
                task_type="upload",
                status="invalid_status",  # 不在 STATUS_* 枚举中
                metadata={"file_code": "FC_P1_4_UNKNOWN_STATUS"},
            )
        assert exc_info.value.code == ErrorCodes.TASK_CENTER_UNKNOWN_STATUS
        assert "invalid_status" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_p1_4_record_task_valid_input_succeeds(self, real_store):
        """测试: record_task 合法输入 → 正常创建任务(回归测试)。"""
        from services import task_center

        user_id = 40003
        await _insert_user(real_store, user_id)

        task_id = await task_center.record_task(
            user_id=user_id,
            task_type="upload",
            status="completed",
            metadata={"file_code": "FC_P1_4_OK", "file_size": 1024},
        )
        assert task_id > 0, f"task_id 应 > 0,实际: {task_id}"

        # 验证任务已写入并标记为 completed
        cursor = await real_store._db.execute(
            "SELECT task_type, status, progress FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "upload"
        assert row[1] == "completed"
        assert row[2] == 100, "completed 状态 progress 应为 100"

    @pytest.mark.asyncio
    async def test_p1_4_list_user_tasks_raises_on_db_error(self, real_store):
        """测试: list_user_tasks DB 异常 → raise AppError(LIST_DB_ERROR)(不再返回空 list)。"""
        from services import task_center
        from services.error_codes import AppError, ErrorCodes

        user_id = 40004

        # Mock store._db.execute 抛异常
        async def _failing_execute(*args, **kwargs):
            raise RuntimeError("simulated_db_corruption")

        with patch.object(real_store._db, "execute", _failing_execute):
            with pytest.raises(AppError) as exc_info:
                await task_center.list_user_tasks(user_id)

        assert exc_info.value.code == ErrorCodes.TASK_CENTER_LIST_DB_ERROR

    @pytest.mark.asyncio
    async def test_p1_4_list_all_tasks_raises_on_db_error(self, real_store):
        """测试: list_all_tasks DB 异常 → raise AppError(LIST_DB_ERROR)。"""
        from services import task_center
        from services.error_codes import AppError, ErrorCodes

        # Mock execute_fetchall 抛异常
        async def _failing_fetchall(*args, **kwargs):
            raise RuntimeError("simulated_db_io_error")

        with patch.object(real_store._db, "execute_fetchall", _failing_fetchall):
            with pytest.raises(AppError) as exc_info:
                await task_center.list_all_tasks()

        assert exc_info.value.code == ErrorCodes.TASK_CENTER_LIST_DB_ERROR


# ════════════════════════════════════════════════════════════════
# P1-5: Repair Console 审批强制 + hash/principal 校验
# ════════════════════════════════════════════════════════════════

class TestP1_5_RepairConsoleApprovalEnforcement:
    """P1-5: repair_console 高风险动作强制审批 + 审批三要素校验测试。"""

    @pytest.mark.asyncio
    async def test_p1_5_high_risk_requires_approval(self, real_store):
        """测试: 高风险动作(skip_outbox)未传 approval_action_id → raise AppError(APPROVAL_REQUIRED)。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        principal_id = 50001
        # skip_outbox 是 HIGH 风险,不传 approval_action_id 应被拒绝
        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="skip_outbox",
                params={"ids": [1, 2, 3], "reason": "test"},
                principal_id=principal_id,
                # 不传 approval_action_id
            )
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_REQUIRED
        assert "skip_outbox" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_p1_5_low_risk_allows_without_approval(self, real_store):
        """测试: 低风险动作(retry_outbox)不传 approval_action_id 应允许执行。"""
        from services import repair_console

        principal_id = 50002

        # Mock retry_outbox 函数(避免依赖真实 dirty_outbox 数据)
        async def _mock_retry_outbox(ids):
            return len(ids)

        with patch.object(repair_console, "retry_outbox", _mock_retry_outbox):
            result = await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": [10, 20, 30]},
                principal_id=principal_id,
                # 低风险动作不传 approval_action_id
            )
        assert result["success"] is True, "低风险动作应允许执行"
        assert result["affected_count"] == 3
        assert result["approval_verified"] is False  # 未审批

    @pytest.mark.asyncio
    async def test_p1_5_approval_hash_mismatch_rejected(self, real_store):
        """测试: 审批的 request_hash 与期望不匹配 → raise AppError(HASH_MISMATCH)。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        principal_id = 50003
        # 插入一条 command_executions 记录,request_hash 故意写错
        # R52 P0-5: 审批状态为 'approved'(非 'executed')
        await _insert_command_execution(
            real_store,
            action_id="approval_hash_mismatch",
            principal_id=principal_id,
            status="approved",
            request_hash="wrong_hash_value",
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="skip_outbox",
                params={"ids": [1], "reason": "test"},
                principal_id=principal_id,
                approval_action_id="approval_hash_mismatch",
            )
        # 审批验证失败 → 抛 APPROVAL_HASH_MISMATCH
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH

    @pytest.mark.asyncio
    async def test_p1_5_approval_principal_mismatch_rejected(self, real_store):
        """测试: 审批的 principal_id 与操作人不匹配 → raise AppError(验证失败)。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        # 审批人 50004,操作人 59999(不一致)
        approver_id = 50004
        operator_id = 59999

        # 计算期望的 request_hash
        expected_hash = repair_console.compute_repair_request_hash(
            "skip_outbox", {"ids": [1], "reason": "test"},
        )
        # 插入审批记录,principal_id=approver_id(与操作人不同)
        # R52 P0-5: 审批状态为 'approved'(非 'executed')
        await _insert_command_execution(
            real_store,
            action_id="approval_principal_mismatch",
            principal_id=approver_id,
            status="approved",
            request_hash=expected_hash,
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="skip_outbox",
                params={"ids": [1], "reason": "test"},
                principal_id=operator_id,  # 操作人与审批人不一致
                approval_action_id="approval_principal_mismatch",
            )
        # principal 不匹配 → _verify_approval 返回 False → 抛 HASH_MISMATCH
        # (代码中统一抛 HASH_MISMATCH,内部日志会区分具体原因)
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH

    @pytest.mark.asyncio
    async def test_p1_5_approval_valid_executes_high_risk(self, real_store):
        """测试: 高风险动作 + 完整审批(status+principal+hash 三要素一致) → 执行成功。"""
        from services import repair_console

        principal_id = 50005

        # 计算期望的 request_hash
        params = {"ids": [1, 2], "reason": "cleanup"}
        expected_hash = repair_console.compute_repair_request_hash("skip_outbox", params)

        # 插入审批记录,三要素全部一致
        # R52 P0-5: 审批状态为 'approved'(非 'executed')
        await _insert_command_execution(
            real_store,
            action_id="approval_valid_high_risk",
            principal_id=principal_id,
            status="approved",
            request_hash=expected_hash,
        )

        # Mock skip_outbox 函数
        async def _mock_skip_outbox(ids, reason=""):
            return len(ids)

        with patch.object(repair_console, "skip_outbox", _mock_skip_outbox):
            result = await repair_console.execute_repair(
                action="skip_outbox",
                params=params,
                principal_id=principal_id,
                approval_action_id="approval_valid_high_risk",
            )
        assert result["success"] is True, "审批通过应执行成功"
        assert result["affected_count"] == 2
        assert result["approval_verified"] is True

    @pytest.mark.asyncio
    async def test_p1_5_status_not_executed_rejected(self, real_store):
        """测试: 审批状态非 executed(如 pending) → 验证失败。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        principal_id = 50006
        params = {"ids": [1]}
        expected_hash = repair_console.compute_repair_request_hash("skip_outbox", params)

        # 插入审批记录,status='pending'(未审批通过)
        await _insert_command_execution(
            real_store,
            action_id="approval_pending_status",
            principal_id=principal_id,
            status="pending",  # 未审批通过
            request_hash=expected_hash,
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="skip_outbox",
                params=params,
                principal_id=principal_id,
                approval_action_id="approval_pending_status",
            )
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH


# ════════════════════════════════════════════════════════════════
# 集成验证: AppError + ErrorCodes 协议合规性
# ════════════════════════════════════════════════════════════════

class TestR51P1ErrorProtocolCompliance:
    """R51 P1 整改协议合规性验证: 所有 P1 整改都使用 AppError + ErrorCodes 协议。"""

    def test_error_codes_registered_in_registry(self):
        """验证所有 R51 P1 新增的 ErrorCodes 都已在 ErrorRegistry 注册。"""
        from services import error_codes

        # P1-1
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.DATA_LIFECYCLE_DELETE_STEP_FAILED)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING)

        # P1-2
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.ENTITLEMENT_QUOTA_QUERY_FAILED)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED)

        # P1-3
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.COLLECTION_CAS_CONFLICT)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.COLLECTION_CAS_BYPASS_NOT_ALLOWED)

        # P1-4
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.TASK_CENTER_UNKNOWN_TYPE)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.TASK_CENTER_UNKNOWN_STATUS)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.TASK_CENTER_LIST_DB_ERROR)

        # P1-5
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_REQUIRED)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH)
        assert error_codes.ErrorRegistry.is_registered(error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_PRINCIPAL_MISMATCH)

    def test_locale_translations_exist(self):
        """验证所有新增 ErrorCode 的 message_key 在 zh-CN / en-US locale 文件中存在。"""
        from services import error_codes

        # 加载 locale 文件
        import os
        locale_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "locales",
        )

        for locale_file in ("zh-CN.json", "en-US.json"):
            locale_path = os.path.join(locale_dir, locale_file)
            with open(locale_path, "r", encoding="utf-8") as f:
                locale_data = json.load(f)
            errors_section = locale_data.get("errors", {})

            # 检查 R51 P1 所有 ErrorCode 的 message_key
            r51_p1_codes = [
                error_codes.ErrorCodes.DATA_LIFECYCLE_DELETE_STEP_FAILED,
                error_codes.ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
                error_codes.ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING,
                error_codes.ErrorCodes.ENTITLEMENT_QUOTA_QUERY_FAILED,
                error_codes.ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
                error_codes.ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
                error_codes.ErrorCodes.COLLECTION_CAS_CONFLICT,
                error_codes.ErrorCodes.COLLECTION_CAS_BYPASS_NOT_ALLOWED,
                error_codes.ErrorCodes.TASK_CENTER_UNKNOWN_TYPE,
                error_codes.ErrorCodes.TASK_CENTER_UNKNOWN_STATUS,
                error_codes.ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
                error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_REQUIRED,
                error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH,
                error_codes.ErrorCodes.REPAIR_CONSOLE_APPROVAL_PRINCIPAL_MISMATCH,
            ]
            for code in r51_p1_codes:
                definition = error_codes.ErrorRegistry.get(code)
                assert definition is not None, f"ErrorCode {code} 未在 registry 注册"
                msg_key = definition.message_key
                # message_key 形如 "errors.data.lifecycle.delete_step_failed",
                # 但 locale 文件 errors section 的键名不含 "errors." 前缀,
                # 需 strip 前缀后再查
                locale_key = msg_key[len("errors."):] if msg_key.startswith("errors.") else msg_key
                assert locale_key in errors_section, (
                    f"locale={locale_file} 缺少 message_key={locale_key} "
                    f"(ErrorCode={code}, raw message_key={msg_key})"
                )
