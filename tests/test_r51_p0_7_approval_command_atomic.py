"""R51 P0-7: Approval 与 Command Outbox 原子性测试。

被测目标:
- ``services.approval_workflow.create_approval()`` — R51 P0-7 后,CommandBus 类型审批
  在创建时回填确定性 action_id(``f"approval_{approval_id}_{action}"``)
- ``services.approval_workflow.approve()`` — R51 P0-7 后,PENDING→APPROVED + audit_log
  + dirty_outbox + command_outbox 必须在同一事务原子完成
- ``services.approval_workflow._enqueue_command_outbox()`` — 幂等补偿 worker 入口,
  UNIQUE(approval_id, action_id) 冲突时视为已写入返回 True
- ``database/cache_store.CacheStore`` — command_outbox 新增 UNIQUE(approval_id, action_id) 索引

测试场景:
1. approve 成功 → approval=APPROVED + audit + dirty_outbox + command_outbox 全部写入
2. command_outbox 写入失败(UNIQUE 冲突) → 整个 transaction 回滚,approval 仍 PENDING
3. action_id 缺失(payload 含 command_action 但无 action_id) → raise AppError
4. 重复 approve 同一 approval → 幂等(CAS 阻止重复,UNIQUE 约束兜底)
5. 并发 approve → CAS 防止重复(只有一个成功)
6. create_approval 回填确定性 action_id(CommandBus 审批缺 action_id 时)
7. _enqueue_command_outbox 幂等补偿(UNIQUE 冲突返回 True)

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 ``CacheStore.init()`` 创建表
- Mock rbac.check_permission 让 approve 通过权限检查
- 每个用例前重置 ApprovalExecutor / CommandBus / EffectReceipts 单例
"""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

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


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p0_7_test_")
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


@pytest_asyncio.fixture
async def mock_rbac_permission(real_store):
    """Mock rbac.check_permission 返回 True(让 approve 通过权限检查)。

    approval_workflow 在模块顶层 ``from services.rbac import check_permission``,
    所以需要 patch ``services.approval_workflow.check_permission``。
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.approval_workflow.check_permission",
            AsyncMock(return_value=True),
        )
        yield


@pytest.fixture(autouse=True)
def _reset_approval_executor_singleton():
    """每个用例前重置 ApprovalExecutor 单例,避免跨用例污染。"""
    from services import approval_executor
    approval_executor.reset_approval_executor()
    yield
    approval_executor.reset_approval_executor()


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """R46 P0-1: 每个用例前重置 EffectReceiptManager 单例。"""
    from services import effect_receipts
    original = effect_receipts._receipt_manager
    effect_receipts._receipt_manager = None
    yield
    effect_receipts._receipt_manager = original


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def command_action_to_approval_action(command_action: str) -> str:
    """将 CommandBus command_action 映射到 approval_workflow action。"""
    mapping = {
        "takedown_report": "takedown",
        "ban_user": "ban",
        "restore_backup": "restore",
        "assign_role": "rbac_assign",
        "enable_maintenance": "maintenance_enable",
        "disable_maintenance": "maintenance_disable",
        "purge_data": "delete_data",
    }
    return mapping.get(command_action, command_action)


async def _create_commandbus_approval(
    store,
    command_action: str = "takedown_report",
    params: Optional[dict] = None,
    action_id: Optional[str] = "test_action_001",
    created_by: int = 100,
) -> int:
    """通过 approval_workflow.create_approval 创建一个 CommandBus 类型的审批。

    payload 中包含 command_action 字段,标记此审批由 CommandBus 创建。
    action_id=None 时不写入 payload(用于测试 action_id 缺失场景)。
    """
    from services import approval_workflow
    if params is None:
        params = {"target_type": "file_code", "target_id": "TC001", "reason": "test"}
    payload = {
        "command_action": command_action,
        "params": params,
        "principal_id": created_by,
        "principal_name": "creator",
        "principal_source": "bot",
    }
    if action_id is not None:
        payload["action_id"] = action_id
    approval_id = await approval_workflow.create_approval(
        action=command_action_to_approval_action(command_action),
        payload=payload,
        created_by=created_by,
    )
    assert approval_id > 0, f"创建审批失败 action={command_action}"
    return approval_id


async def _list_command_outbox_entries(store):
    """列出所有 command_outbox 条目(按 id 升序)。"""
    cursor = await store._db.execute(
        "SELECT id, action_id, approval_id, command_type, payload, status, "
        "retry_count, max_retries, next_retry_at, last_error, "
        "created_at, updated_at "
        "FROM command_outbox ORDER BY id ASC",
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "action_id": r[1], "approval_id": r[2],
            "command_type": r[3], "payload": r[4], "status": r[5],
            "retry_count": r[6], "max_retries": r[7], "next_retry_at": r[8],
            "last_error": r[9], "created_at": r[10], "updated_at": r[11],
        }
        for r in rows
    ]


async def _count_audit_logs(store, approval_id: int, action: str) -> int:
    """统计指定 approval_id + action 的 audit_log 记录数。"""
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE target_id = ? AND action = ?",
        (str(approval_id), action),
    )
    rows = await cursor.fetchall()
    return int(rows[0][0]) if rows else 0


async def _count_dirty_outbox(store, table_name: str, pk: str) -> int:
    """统计指定 table_name + pk 的 dirty_outbox 记录数。"""
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM dirty_outbox WHERE table_name = ? AND pk = ?",
        (table_name, pk),
    )
    rows = await cursor.fetchall()
    return int(rows[0][0]) if rows else 0


async def _insert_command_outbox_entry(
    store,
    action_id: str,
    approval_id: int,
    command_type: str,
    payload: dict,
    status: str = "pending",
) -> int:
    """直接向 command_outbox 表插入一条记录(测试辅助,模拟已存在的条目)。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    cursor = await store._db.execute(
        "INSERT INTO command_outbox "
        "(action_id, approval_id, command_type, payload, status, "
        " retry_count, max_retries, next_retry_at, last_error, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, 3, NULL, NULL, ?, ?)",
        (action_id, approval_id, command_type, payload_str, status, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


async def _insert_approval_direct(
    store,
    action: str,
    payload: dict,
    created_by: int = 100,
) -> int:
    """直接向 approvals 表插入一条记录(绕过 create_approval,用于构造 action_id 缺失场景)。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    cursor = await store._db.execute(
        "INSERT INTO approvals (action, payload, status, approver_id, approver_note, "
        "created_by, created_at, resolved_at) "
        "VALUES (?, ?, 'pending', NULL, '', ?, ?, NULL)",
        (action, payload_str, created_by, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


# ════════════════════════════════════════════════════════════════
# 1. approve 成功 → approval=APPROVED + audit + dirty_outbox + command_outbox 全部写入
# ════════════════════════════════════════════════════════════════

class TestApproveAtomicSuccess:
    """R51 P0-7: approve() 成功时所有副作用原子写入。"""

    @pytest.mark.asyncio
    async def test_approve_writes_all_side_effects_atomically(
        self, real_store, mock_rbac_permission,
    ):
        """approve 成功 → approval=APPROVED + audit + dirty_outbox + command_outbox 全部写入。

        验证:
        - approval.status == 'approved'
        - audit_log 有 1 条 approve 记录
        - dirty_outbox 有 approvals 表的记录
        - dirty_outbox 有 audit_log 表的记录
        - command_outbox 有 1 条 pending 记录,action_id 与 payload 中一致
        """
        from services import approval_workflow

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "ATOMIC_001", "reason": "test"},
            action_id="atomic_success_001",
            created_by=100,
        )

        # 执行 approve
        ok = await approval_workflow.approve(approval_id, approver_id=200, note="批准")
        assert ok is True, "approve 应成功"

        # 1. 验证 approval 状态
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"
        assert approval["approver_id"] == 200

        # 2. 验证 audit_log
        audit_count = await _count_audit_logs(real_store, approval_id, "approve")
        assert audit_count == 1, f"应有 1 条 approve audit_log,实际 {audit_count}"

        # 3. 验证 dirty_outbox(approvals 表)
        dirty_approvals = await _count_dirty_outbox(real_store, "approvals", str(approval_id))
        assert dirty_approvals >= 1, \
            f"dirty_outbox 应有 approvals 记录,实际 {dirty_approvals}"

        # 4. 验证 dirty_outbox(audit_log 表 — create_approval + approve 各写一条)
        dirty_audit = await _count_dirty_outbox(real_store, "audit_log", "last")
        assert dirty_audit >= 1, \
            f"dirty_outbox 应有 audit_log 记录,实际 {dirty_audit}"

        # 5. 验证 command_outbox
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, f"应有 1 条 command_outbox 记录,实际 {len(entries)}"
        entry = entries[0]
        assert entry["status"] == "pending"
        assert entry["action_id"] == "atomic_success_001"
        assert entry["approval_id"] == approval_id
        assert entry["command_type"] == "takedown_report"
        assert entry["retry_count"] == 0
        assert entry["next_retry_at"] is None


# ════════════════════════════════════════════════════════════════
# 2. command_outbox 写入失败 → 整个 transaction 回滚,approval 仍 PENDING
# ════════════════════════════════════════════════════════════════

class TestApproveAtomicRollback:
    """R51 P0-7: command_outbox 写入失败时事务回滚。"""

    @pytest.mark.asyncio
    async def test_command_outbox_failure_rolls_back_approval(
        self, real_store, mock_rbac_permission,
    ):
        """command_outbox 写入失败(UNIQUE 冲突) → 整个 transaction 回滚,approval 仍 PENDING。

        步骤:
        - 创建 CommandBus 类型审批(action_id="rollback_001")
        - 预先向 command_outbox 插入同 (approval_id, action_id) 的记录(触发 UNIQUE 冲突)
        - 调用 approve()
        - 验证:approve 返回 False(事务回滚)
        - 验证:approval 状态仍为 pending(CAS UPDATE 被回滚)
        - 验证:audit_log 没有 approve 记录(被回滚)
        - 验证:command_outbox 仍只有 1 条(预插入的那条,没有新增)
        """
        from services import approval_workflow

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "ROLLBACK_001", "reason": "test"},
            action_id="rollback_001",
            created_by=100,
        )

        # 预先插入同 (approval_id, action_id) 的 command_outbox 记录
        await _insert_command_outbox_entry(
            real_store,
            action_id="rollback_001",
            approval_id=approval_id,
            command_type="takedown_report",
            payload={"pre": "inserted"},
            status="pending",
        )

        # 调用 approve — 应因 UNIQUE 冲突导致事务回滚
        ok = await approval_workflow.approve(approval_id, approver_id=200, note="尝试批准")

        # 验证:approve 返回 False(事务回滚)
        assert ok is False, "command_outbox UNIQUE 冲突应导致 approve 返回 False"

        # 验证:approval 状态仍为 pending(UPDATE 被回滚)
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "pending", \
            f"事务回滚后 approval 应仍为 pending,实际 {approval['status']}"

        # 验证:audit_log 没有 approve 记录(被回滚)
        audit_count = await _count_audit_logs(real_store, approval_id, "approve")
        assert audit_count == 0, \
            f"事务回滚后不应有 approve audit_log,实际 {audit_count}"

        # 验证:command_outbox 仍只有 1 条(预插入的那条)
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, \
            f"应有 1 条 command_outbox(预插入),实际 {len(entries)}"
        assert entries[0]["action_id"] == "rollback_001"
        # payload 应为预插入的 {"pre": "inserted"},不是 approve 写入的
        payload_data = json.loads(entries[0]["payload"])
        assert payload_data.get("pre") == "inserted"


# ════════════════════════════════════════════════════════════════
# 3. action_id 缺失 → raise AppError
# ════════════════════════════════════════════════════════════════

class TestApproveActionIdRequired:
    """R51 P0-7: payload 含 command_action 但 action_id 缺失时 raise AppError。"""

    @pytest.mark.asyncio
    async def test_approve_raises_apperror_when_action_id_missing(
        self, real_store, mock_rbac_permission,
    ):
        """action_id 缺失(payload 含 command_action 但无 action_id) → raise AppError。

        步骤:
        - 直接向 approvals 表插入一条记录(绕过 create_approval 的回填逻辑),
          payload 含 command_action 但无 action_id
        - 调用 approve()
        - 验证:raise AppError(ErrorCodes.APPROVAL_STATE_INVALID)
        - 验证:approval 状态仍为 pending(未执行任何写入)
        """
        from services import approval_workflow
        from services.error_codes import AppError, ErrorCodes

        # 直接插入 payload 含 command_action 但无 action_id 的审批
        # (绕过 create_approval 的回填逻辑,模拟旧版本/异常数据)
        approval_id = await _insert_approval_direct(
            real_store,
            action="takedown",
            payload={
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "NO_ACTION_ID"},
                "principal_id": 100,
                # 注意:没有 action_id 字段
            },
            created_by=100,
        )
        assert approval_id > 0

        # 调用 approve — 应 raise AppError
        with pytest.raises(AppError) as exc_info:
            await approval_workflow.approve(approval_id, approver_id=200, note="尝试批准")

        # 验证错误码
        assert exc_info.value.code == ErrorCodes.APPROVAL_STATE_INVALID, \
            f"应 raise APPROVAL_STATE_INVALID,实际 {exc_info.value.code}"

        # 验证:approval 状态仍为 pending(未执行任何写入)
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "pending"

        # 验证:command_outbox 为空
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 0, "action_id 缺失时不应写入 command_outbox"

    @pytest.mark.asyncio
    async def test_approve_raises_apperror_when_action_id_empty(
        self, real_store, mock_rbac_permission,
    ):
        """action_id 为空字符串(payload 含 command_action 且 action_id='') → raise AppError。"""
        from services import approval_workflow
        from services.error_codes import AppError, ErrorCodes

        approval_id = await _insert_approval_direct(
            real_store,
            action="takedown",
            payload={
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "EMPTY_ACTION_ID"},
                "principal_id": 100,
                "action_id": "",  # 空字符串
            },
            created_by=100,
        )

        with pytest.raises(AppError) as exc_info:
            await approval_workflow.approve(approval_id, approver_id=200, note="尝试批准")

        assert exc_info.value.code == ErrorCodes.APPROVAL_STATE_INVALID


# ════════════════════════════════════════════════════════════════
# 4. 重复 approve 同一 approval → 幂等(UNIQUE 约束阻止重复)
# ════════════════════════════════════════════════════════════════

class TestApproveIdempotent:
    """R51 P0-7: 重复 approve 同一 approval 时幂等(CAS + UNIQUE 双重保护)。"""

    @pytest.mark.asyncio
    async def test_duplicate_approve_is_idempotent(
        self, real_store, mock_rbac_permission,
    ):
        """重复 approve 同一 approval → 第二次 CAS 失败,command_outbox 不重复。

        步骤:
        - 创建 CommandBus 类型审批
        - 第一次 approve → 成功,1 条 command_outbox
        - 第二次 approve → CAS 失败(status 已 approved),返回 False
        - 验证:command_outbox 仍只有 1 条(不重复)
        """
        from services import approval_workflow

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "IDEMP_001", "reason": "test"},
            action_id="idempotent_001",
            created_by=100,
        )

        # 第一次 approve — 成功
        ok1 = await approval_workflow.approve(approval_id, approver_id=200, note="第一次")
        assert ok1 is True, "第一次 approve 应成功"

        # 第二次 approve — CAS 失败(status 已 approved)
        ok2 = await approval_workflow.approve(approval_id, approver_id=201, note="第二次")
        assert ok2 is False, "第二次 approve 应因 CAS 失败返回 False"

        # 验证:approval 状态仍为 approved(第一次的结果)
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"

        # 验证:command_outbox 只有 1 条(CAS 阻止第二次 approve 写入)
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, \
            f"CAS 应阻止重复写入,command_outbox 应只有 1 条,实际 {len(entries)}"
        assert entries[0]["action_id"] == "idempotent_001"

    @pytest.mark.asyncio
    async def test_unique_constraint_prevents_duplicate_outbox(
        self, real_store, mock_rbac_permission,
    ):
        """UNIQUE(approval_id, action_id) 直接测试 — 补偿 worker 重复写入时幂等。

        步骤:
        - 创建 CommandBus 类型审批 + approve 成功(已有 1 条 command_outbox)
        - 调用 _enqueue_command_outbox(补偿 worker 入口)写入相同 (approval_id, action_id)
        - 验证:返回 True(UNIQUE 冲突视为幂等成功)
        - 验证:command_outbox 仍只有 1 条
        """
        from services import approval_workflow

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "UNIQUE_001", "reason": "test"},
            action_id="unique_compensate_001",
            created_by=100,
        )

        # approve 成功 → 写入 1 条 command_outbox
        ok = await approval_workflow.approve(approval_id, approver_id=200, note="批准")
        assert ok is True

        entries_after_approve = await _list_command_outbox_entries(real_store)
        assert len(entries_after_approve) == 1

        # 调用补偿 worker 入口 — UNIQUE 冲突应幂等返回 True
        compensation_ok = await approval_workflow._enqueue_command_outbox(
            approval_id=approval_id,
            action_id="unique_compensate_001",
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "action_id": "unique_compensate_001"},
        )
        assert compensation_ok is True, \
            "UNIQUE 冲突时补偿 worker 应幂等返回 True"

        # 验证:command_outbox 仍只有 1 条
        entries_after_compensation = await _list_command_outbox_entries(real_store)
        assert len(entries_after_compensation) == 1, \
            f"补偿 worker 不应新增记录,实际 {len(entries_after_compensation)}"


# ════════════════════════════════════════════════════════════════
# 5. 并发 approve → CAS 防止重复
# ════════════════════════════════════════════════════════════════

class TestApproveConcurrentCAS:
    """R51 P0-7: 并发 approve 时 CAS 防止重复。"""

    @pytest.mark.asyncio
    async def test_concurrent_approve_only_one_succeeds(
        self, real_store, mock_rbac_permission,
    ):
        """并发场景:两个 approve() 同时调用,只有一个成功。

        验证:
        - 两个调用者都通过 get_approval() 检查(status=pending)
        - CAS UPDATE WHERE status='pending' 只有一个 rowcount=1
        - 另一个 rowcount=0,返回 False
        - command_outbox 只有 1 条
        - approval 状态为 approved
        """
        from services import approval_workflow

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "CONCURRENT_001", "reason": "test"},
            action_id="concurrent_001",
            created_by=100,
        )

        # 并发调用两个 approve()
        results = await asyncio.gather(
            approval_workflow.approve(approval_id, approver_id=200, note="审批人A"),
            approval_workflow.approve(approval_id, approver_id=201, note="审批人B"),
        )

        # 验证:只有一个成功
        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)
        assert success_count == 1, f"应只有一个 approve 成功,实际 {success_count}"
        assert failure_count == 1, f"应有一个 approve 失败,实际 {failure_count}"

        # 验证:approval 状态为 approved
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"

        # 验证:command_outbox 只有 1 条(CAS 阻止重复)
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, \
            f"并发场景下 command_outbox 应只有 1 条,实际 {len(entries)}"
        assert entries[0]["action_id"] == "concurrent_001"


# ════════════════════════════════════════════════════════════════
# 6. create_approval 回填确定性 action_id
# ════════════════════════════════════════════════════════════════

class TestCreateApprovalBackfillActionId:
    """R51 P0-7: create_approval 为 CommandBus 审批回填确定性 action_id。"""

    @pytest.mark.asyncio
    async def test_create_approval_backfills_deterministic_action_id(
        self, real_store,
    ):
        """CommandBus 审批缺 action_id 时,create_approval 回填确定性 ID。

        验证:
        - payload 含 command_action 但无 action_id
        - create_approval 后,payload.action_id == f"approval_{approval_id}_{action}"
        - 后续 approve() 可正常执行(不 raise AppError)
        """
        from services import approval_workflow

        payload = {
            "command_action": "takedown_report",
            "params": {"target_type": "file_code", "target_id": "BACKFILL_001"},
            "principal_id": 100,
            # 注意:没有 action_id
        }
        approval_id = await approval_workflow.create_approval(
            action="takedown",
            payload=payload,
            created_by=100,
        )
        assert approval_id > 0

        # 读取审批,验证 payload 中已回填 action_id
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        approval_payload = approval["payload"]
        assert isinstance(approval_payload, dict)
        expected_action_id = f"approval_{approval_id}_takedown"
        assert approval_payload.get("action_id") == expected_action_id, \
            f"应回填确定性 action_id={expected_action_id},实际 {approval_payload.get('action_id')}"

    @pytest.mark.asyncio
    async def test_create_approval_preserves_existing_action_id(
        self, real_store,
    ):
        """CommandBus 审批已有 action_id 时,create_approval 不覆盖。"""
        from services import approval_workflow

        payload = {
            "command_action": "takedown_report",
            "params": {"target_type": "file_code", "target_id": "PRESERVE_001"},
            "principal_id": 100,
            "action_id": "caller_provided_001",  # 调用方提供的 action_id
        }
        approval_id = await approval_workflow.create_approval(
            action="takedown",
            payload=payload,
            created_by=100,
        )
        assert approval_id > 0

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["payload"].get("action_id") == "caller_provided_001", \
            "已有 action_id 不应被覆盖"

    @pytest.mark.asyncio
    async def test_create_approval_no_backfill_for_non_commandbus(
        self, real_store,
    ):
        """非 CommandBus 审批(无 command_action)不回填 action_id。"""
        from services import approval_workflow

        payload = {"key": "file_prefix", "value": "test"}
        approval_id = await approval_workflow.create_approval(
            action="config_change",
            payload=payload,
            created_by=100,
        )
        assert approval_id > 0

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert "action_id" not in approval["payload"], \
            "非 CommandBus 审批不应回填 action_id"


# ════════════════════════════════════════════════════════════════
# 7. _enqueue_command_outbox 幂等补偿 worker
# ════════════════════════════════════════════════════════════════

class TestEnqueueCommandOutboxCompensation:
    """R51 P0-7: _enqueue_command_outbox 作为幂等补偿 worker 入口。"""

    @pytest.mark.asyncio
    async def test_compensation_worker_rejects_empty_action_id(
        self, real_store,
    ):
        """补偿 worker 入口拒绝空 action_id(返回 False,不再用含当前时间的临时 ID)。"""
        from services import approval_workflow

        ok = await approval_workflow._enqueue_command_outbox(
            approval_id=999,
            action_id="",  # 空 action_id
            command_type="takedown_report",
            payload={"command_action": "takedown_report"},
        )
        assert ok is False, "空 action_id 应被拒绝返回 False"

    @pytest.mark.asyncio
    async def test_compensation_worker_writes_new_entry(
        self, real_store,
    ):
        """补偿 worker 写入新条目(无冲突时正常写入)。"""
        from services import approval_workflow

        ok = await approval_workflow._enqueue_command_outbox(
            approval_id=888,
            action_id="compensate_new_001",
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "action_id": "compensate_new_001"},
        )
        assert ok is True, "新条目应写入成功"

        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["action_id"] == "compensate_new_001"
        assert entries[0]["approval_id"] == 888

    @pytest.mark.asyncio
    async def test_compensation_worker_idempotent_on_conflict(
        self, real_store,
    ):
        """补偿 worker 遇 UNIQUE 冲突时幂等返回 True(不报错)。"""
        from services import approval_workflow

        # 第一次写入 — 成功
        ok1 = await approval_workflow._enqueue_command_outbox(
            approval_id=777,
            action_id="compensate_conflict_001",
            command_type="takedown_report",
            payload={"command_action": "takedown_report"},
        )
        assert ok1 is True

        # 第二次写入同 (approval_id, action_id) — UNIQUE 冲突,幂等返回 True
        ok2 = await approval_workflow._enqueue_command_outbox(
            approval_id=777,
            action_id="compensate_conflict_001",
            command_type="takedown_report",
            payload={"command_action": "takedown_report"},
        )
        assert ok2 is True, "UNIQUE 冲突应幂等返回 True"

        # 验证:command_outbox 仍只有 1 条
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, \
            f"幂等冲突不应新增记录,实际 {len(entries)}"


# ════════════════════════════════════════════════════════════════
# 8. UNIQUE 索引存在性验证
# ════════════════════════════════════════════════════════════════

class TestUniqueIndexExists:
    """R51 P0-7: command_outbox UNIQUE(approval_id, action_id) 索引存在性验证。"""

    @pytest.mark.asyncio
    async def test_unique_index_exists(self, real_store):
        """验证 idx_command_outbox_approval_action 索引已创建。"""
        cursor = await real_store._db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_command_outbox_approval_action'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1, \
            f"idx_command_outbox_approval_action 索引应存在,实际找到 {len(rows)}"
        idx_name, idx_sql = rows[0]
        assert idx_name == "idx_command_outbox_approval_action"
        # 验证 SQL 包含 UNIQUE 关键字
        assert "UNIQUE" in idx_sql.upper(), \
            f"索引应为 UNIQUE,SQL: {idx_sql}"

    @pytest.mark.asyncio
    async def test_unique_constraint_enforced(self, real_store):
        """直接测试 UNIQUE 约束生效 — 重复插入应报错。"""
        import datetime as _dt
        now = _dt.datetime.now().isoformat()

        # 第一次插入 — 成功
        await real_store._db.execute(
            "INSERT INTO command_outbox "
            "(action_id, approval_id, command_type, payload, status, "
            " retry_count, max_retries, next_retry_at, last_error, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, 3, NULL, NULL, ?, ?)",
            ("unique_test_001", 555, "takedown_report", "{}", now, now),
        )
        await real_store._db.commit()

        # 第二次插入相同 (approval_id, action_id) — 应报 UNIQUE 冲突
        with pytest.raises(Exception) as exc_info:
            await real_store._db.execute(
                "INSERT INTO command_outbox "
                "(action_id, approval_id, command_type, payload, status, "
                " retry_count, max_retries, next_retry_at, last_error, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, 3, NULL, NULL, ?, ?)",
                ("unique_test_001", 555, "takedown_report", "{}", now, now),
            )
            await real_store._db.commit()

        # 验证异常信息包含 UNIQUE/constraint
        err_msg = str(exc_info.value).lower()
        assert "unique" in err_msg or "constraint" in err_msg, \
            f"应为 UNIQUE 约束冲突,实际异常: {exc_info.value}"
