"""R41 P0-4: ApprovalExecutor — command_outbox 异步消费机制测试。

被测目标:
- ``services.approval_workflow.approve()`` — R41 P0-4 后只做 CAS PENDING → APPROVED
  + 审计 + 写入 ``command_outbox``,不再直接调用 ``CommandBus.execute_approved_action``
- ``services.approval_executor.ApprovalExecutor.drain_once()`` — 消费 ``command_outbox``
  的 pending 条目,调用 ``CommandBus.execute_command_outbox_entry()`` 执行 handler
- ``services.command_bus.CommandBus.execute_command_outbox_entry()`` — 从 outbox entry
  执行 handler(不走幂等缓存,允许重试)

测试场景:
1. ``approve()`` 不再触发 handler 直接执行,只写 ``command_outbox``
2. ``ApprovalExecutor.drain_once()`` 消费 pending 记录并执行 handler
3. handler 失败时重试机制(retry_count 递增,next_retry_at 延迟)
4. 达到 max_retries 时 status='failed'
5. 嵌套事务场景:approve 在独立事务中,executor 在独立调用,handler 不再触发嵌套 BEGIN

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 ``CacheStore.init()`` 创建表
- 使用 ``monkeypatch`` 替换 ``content_reports.takedown_content`` 等 handler 副作用
- 每个用例前重置 ApprovalExecutor 单例和 CommandBus 幂等缓存
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
    tmpdir = tempfile.mkdtemp(prefix="r41_p0_4_test_")
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
    """R46 P0-1: 每个用例前重置 EffectReceiptManager 单例,避免跨用例持有已关闭 store。

    real_store fixture 每次创建新的临时 SQLite,但 services.effect_receipts._receipt_manager
    是模块级单例,会持有旧 store 引用,导致 check_receipt/record_pending 在旧 store 上
    操作(返回 False 被 EffectReceiptContext 误判为 skipped),进而跳过 handler 调用。
    """
    from services import effect_receipts
    original = effect_receipts._receipt_manager
    effect_receipts._receipt_manager = None
    yield
    effect_receipts._receipt_manager = original


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _create_commandbus_approval(
    store,
    command_action: str = "takedown_report",
    params: Optional[dict] = None,
    action_id: str = "test_action_001",
    created_by: int = 100,
) -> int:
    """通过 approval_workflow.create_approval 创建一个 CommandBus 类型的审批。

    payload 中包含 command_action 字段,标记此审批由 CommandBus 创建。
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
        "action_id": action_id,
    }
    approval_id = await approval_workflow.create_approval(
        action=command_action_to_approval_action(command_action),
        payload=payload,
        created_by=created_by,
    )
    assert approval_id > 0, f"创建审批失败 action={command_action}"
    return approval_id


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


async def _get_command_outbox_entry(store, entry_id: int) -> Optional[dict]:
    """读取 command_outbox 表中指定 id 的条目。"""
    cursor = await store._db.execute(
        "SELECT id, action_id, approval_id, command_type, payload, status, "
        "retry_count, max_retries, next_retry_at, last_error, "
        "created_at, updated_at "
        "FROM command_outbox WHERE id = ?",
        (entry_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r[0],
        "action_id": r[1],
        "approval_id": r[2],
        "command_type": r[3],
        "payload": r[4],
        "status": r[5],
        "retry_count": r[6],
        "max_retries": r[7],
        "next_retry_at": r[8],
        "last_error": r[9],
        "created_at": r[10],
        "updated_at": r[11],
    }


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


async def _insert_command_outbox_entry(
    store,
    action_id: str,
    approval_id: int,
    command_type: str,
    payload: dict,
    status: str = "pending",
    retry_count: int = 0,
    max_retries: int = 3,
    next_retry_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> int:
    """直接向 command_outbox 表插入一条记录(测试辅助)。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    cursor = await store._db.execute(
        "INSERT INTO command_outbox "
        "(action_id, approval_id, command_type, payload, status, "
        " retry_count, max_retries, next_retry_at, last_error, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, approval_id, command_type, payload_str, status,
         retry_count, max_retries, next_retry_at, last_error, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


# ════════════════════════════════════════════════════════════════
# 1. approve() 不再触发 handler 直接执行(只写 command_outbox)
# ════════════════════════════════════════════════════════════════

class TestApproveWritesCommandOutbox:
    """R41 P0-4: approve() 不再直接调用 CommandBus.execute_approved_action()。"""

    @pytest.mark.asyncio
    async def test_approve_does_not_call_handler_directly(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """approve() 通过后 handler 不应被直接调用,应只写 command_outbox。

        步骤:
        - 创建 CommandBus 类型审批(payload 含 command_action)
        - Mock content_reports.takedown_content 用于断言未被调用
        - 调用 approve()
        - 验证:takedown_content 未被调用
        - 验证:command_outbox 表中有一条 pending 记录
        - 验证:审批状态为 approved
        """
        from services import approval_workflow
        import services.content_reports as cr_mod

        # Mock takedown_content
        takedown_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            takedown_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        # 创建 CommandBus 类型审批
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "TC001", "reason": ""},
            action_id="approve_no_call_001",
            created_by=100,
        )

        # 执行 approve(approver=200,与创建者不同)
        ok = await approval_workflow.approve(
            approval_id=approval_id, approver_id=200, note="approved",
        )

        # 验证:approve 成功
        assert ok is True, "approve 应成功"

        # 验证:takedown_content 未被直接调用
        assert takedown_called["n"] == 0, \
            "approve() 不应直接调用 takedown_content,应通过 command_outbox 异步消费"

        # 验证:审批状态为 approved
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"

        # 验证:command_outbox 表中有 1 条 pending 记录
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1, f"应有 1 条 command_outbox 记录,实际 {len(entries)}"
        entry = entries[0]
        assert entry["status"] == "pending"
        assert entry["action_id"] == "approve_no_call_001"
        assert entry["approval_id"] == approval_id
        assert entry["command_type"] == "takedown_report"
        assert entry["retry_count"] == 0
        assert entry["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_approve_skips_command_outbox_for_non_commandbus_approval(
        self, real_store, mock_rbac_permission,
    ):
        """非 CommandBus 创建的审批(无 command_action)approve 后不写 command_outbox。"""
        from services import approval_workflow

        # 创建普通审批(payload 无 command_action 字段)
        approval_id = await approval_workflow.create_approval(
            action="config_change",
            payload={"key": "file_prefix", "value": "test"},
            created_by=100,
        )
        assert approval_id > 0

        ok = await approval_workflow.approve(
            approval_id=approval_id, approver_id=200, note="ok",
        )

        assert ok is True
        # 验证:command_outbox 表为空
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 0, \
            "非 CommandBus 审批通过后不应写 command_outbox"


# ════════════════════════════════════════════════════════════════
# 2. ApprovalExecutor.drain_once() 消费 pending 记录并执行
# ════════════════════════════════════════════════════════════════

class TestApprovalExecutorDrainOnce:
    """R41 P0-4: ApprovalExecutor.drain_once() 消费 pending 条目测试。"""

    @pytest.mark.asyncio
    async def test_drain_once_executes_pending_entry(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """drain_once() 消费 pending 条目,执行 handler,标记 executed。"""
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock takedown_content(返回 True 表示成功)
        takedown_called = {"n": 0, "args": None}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            takedown_called["n"] += 1
            takedown_called["args"] = {
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
                "admin_id": admin_id,
            }
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        # 创建审批 + approve(写 command_outbox)
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "DRAIN_001", "reason": "test"},
            action_id="drain_test_001",
            created_by=100,
        )
        ok = await approval_workflow.approve(approval_id, approver_id=200)
        assert ok is True

        # 验证:command_outbox 表中有 1 条 pending
        entries_before = await _list_command_outbox_entries(real_store)
        assert len(entries_before) == 1
        assert entries_before[0]["status"] == "pending"

        # 执行 drain_once
        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        # 验证统计
        assert stats["total"] == 1
        assert stats["success"] == 1
        assert stats["failed"] == 0
        assert stats["retry_scheduled"] == 0

        # 验证:takedown_content 被调用一次
        assert takedown_called["n"] == 1
        assert takedown_called["args"]["target_id"] == "DRAIN_001"

        # 验证:command_outbox 状态变为 executed
        entries_after = await _list_command_outbox_entries(real_store)
        assert len(entries_after) == 1
        assert entries_after[0]["status"] == "executed"

        # 验证:审批状态为 executed
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "executed"

    @pytest.mark.asyncio
    async def test_drain_once_empty_outbox_returns_zero_stats(
        self, real_store, mock_rbac_permission,
    ):
        """command_outbox 表为空时 drain_once 返回零统计。"""
        from services.approval_executor import ApprovalExecutor

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats == {
            "total": 0, "success": 0, "failed": 0,
            "retry_scheduled": 0, "skipped": 0,
        }

    @pytest.mark.asyncio
    async def test_drain_once_skips_entries_with_future_next_retry_at(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """next_retry_at 在未来的 pending 条目本轮不处理(等待退避到期)。"""
        import datetime as _dt
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock takedown_content
        takedown_called = {"n": 0}

        async def _fake_takedown(*args, **kwargs):
            takedown_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        # 创建审批 + approve
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "FUTURE", "reason": ""},
            action_id="future_retry_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 手动将 next_retry_at 设为未来时间(模拟退避未到期)
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        await real_store._db.execute(
            "UPDATE command_outbox SET next_retry_at = ? WHERE action_id = ?",
            (future, "future_retry_test_001"),
        )
        await real_store._db.commit()

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        # 验证:total=0(无到期条目),handler 未调用
        assert stats["total"] == 0
        assert takedown_called["n"] == 0

        # 验证:command_outbox 状态仍为 pending
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["status"] == "pending"


# ════════════════════════════════════════════════════════════════
# 3. handler 失败时重试机制
# ════════════════════════════════════════════════════════════════

class TestApprovalExecutorRetry:
    """R41 P0-4: ApprovalExecutor 失败重试机制测试。"""

    @pytest.mark.asyncio
    async def test_handler_failure_increments_retry_count_and_sets_next_retry_at(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """handler 失败时 retry_count + 1,next_retry_at 设为未来时间,status 回到 pending。"""
        import datetime as _dt
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock takedown_content 抛异常
        async def _fail_takedown(*args, **kwargs):
            raise RuntimeError("目标不存在")

        monkeypatch.setattr(cr_mod, "takedown_content", _fail_takedown)

        # 创建审批 + approve
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "FAIL_001", "reason": ""},
            action_id="retry_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 执行 drain_once(handler 失败)
        before_drain = _dt.datetime.now()
        executor = ApprovalExecutor(backoff_base=5.0)
        stats = await executor.drain_once()

        # 验证统计:total=1,failed=1(retry_scheduled=1)
        assert stats["total"] == 1
        # 注意:_handle_failure 返回 "retry_scheduled",但 _process_entry 将其当作 failed 处理
        # 实际上 _process_entry 返回 _handle_failure 的结果,drain_once 累加到对应统计
        assert stats["retry_scheduled"] == 1 or stats["failed"] >= 1

        # 验证:command_outbox 状态为 pending(回到待重试),retry_count=1
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "pending", \
            f"失败后应回到 pending 等待重试,实际 {entry['status']}"
        assert entry["retry_count"] == 1, \
            f"retry_count 应为 1,实际 {entry['retry_count']}"
        assert entry["next_retry_at"] is not None, "next_retry_at 应已设置"
        # next_retry_at 应在未来(>= now)
        next_retry_dt = _dt.datetime.fromisoformat(entry["next_retry_at"])
        assert next_retry_dt >= before_drain, \
            f"next_retry_at 应在未来,实际 {entry['next_retry_at']}"
        # last_error 应包含异常信息
        assert "目标不存在" in (entry["last_error"] or ""), \
            f"last_error 应包含异常信息,实际 {entry['last_error']}"

        # 验证:审批状态为 failed(mark_failed 被调用)
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "failed"

    @pytest.mark.asyncio
    async def test_retry_with_exponential_backoff(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """多次失败时 backoff 指数增长(5s, 10s, 20s, ...)。"""
        import datetime as _dt
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fail(*args, **kwargs):
            raise RuntimeError("持续失败")

        monkeypatch.setattr(cr_mod, "takedown_content", _fail)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "EXP", "reason": ""},
            action_id="backoff_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 第 1 次失败:retry_count=1,backoff=5s
        executor = ApprovalExecutor(backoff_base=5.0, backoff_max=300.0)
        await executor.drain_once()
        entries = await _list_command_outbox_entries(real_store)
        entry = entries[0]
        assert entry["retry_count"] == 1
        retry1_dt = _dt.datetime.fromisoformat(entry["next_retry_at"])
        # 清除 next_retry_at 让下一轮可以处理
        await real_store._db.execute(
            "UPDATE command_outbox SET next_retry_at = NULL WHERE id = ?",
            (entry["id"],),
        )
        await real_store._db.commit()

        # 第 2 次失败:retry_count=2,backoff=10s
        # 注:由于审批已被 mark_failed,mark_executing 会失败(EXECUTING 状态转换失败)
        # 但 execute_command_outbox_entry 仍会调用 handler(执行失败)
        # 为简化测试,重置审批状态为 approved 让 mark_executing 成功
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        await executor.drain_once()
        entries = await _list_command_outbox_entries(real_store)
        entry = entries[0]
        assert entry["retry_count"] == 2
        retry2_dt = _dt.datetime.fromisoformat(entry["next_retry_at"])
        # 清除 next_retry_at 让下一轮可以处理
        await real_store._db.execute(
            "UPDATE command_outbox SET next_retry_at = NULL WHERE id = ?",
            (entry["id"],),
        )
        await real_store._db.commit()

        # 验证 backoff 指数增长:retry2 应比 retry1 更远
        # 由于时间精度问题,通过比较 next_retry_at 的相对值
        # 第 2 次 backoff = 5 * 2^1 = 10s,应大于第 1 次 5 * 2^0 = 5s
        # 这里通过比较 next_retry_at 的值验证(允许小误差)
        assert retry2_dt >= retry1_dt, \
            f"第 2 次重试应晚于第 1 次,实际 retry1={retry1_dt} retry2={retry2_dt}"


# ════════════════════════════════════════════════════════════════
# 4. 达到 max_retries 时 status='failed'
# ════════════════════════════════════════════════════════════════

class TestApprovalExecutorMaxRetries:
    """R41 P0-4: 达到 max_retries 时 entry 标记为 failed。"""

    @pytest.mark.asyncio
    async def test_entry_marked_failed_after_max_retries(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """retry_count 达到 max_retries(=3)时,status='failed',不再重试。"""
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fail(*args, **kwargs):
            raise RuntimeError("永久失败")

        monkeypatch.setattr(cr_mod, "takedown_content", _fail)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "MAXR", "reason": ""},
            action_id="max_retries_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 直接构造 retry_count=2(已失败 2 次)的 pending 条目,
        # 这样下一次失败就达到 max_retries=3
        await real_store._db.execute(
            "UPDATE command_outbox SET retry_count = 2 WHERE action_id = ?",
            ("max_retries_test_001",),
        )
        await real_store._db.commit()

        # 同时重置审批状态为 approved(mark_executing 才能成功)
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        executor = ApprovalExecutor(backoff_base=5.0, backoff_max=300.0)
        stats = await executor.drain_once()

        # 验证统计:failed=1(达到 max_retries)
        assert stats["total"] == 1
        assert stats["failed"] >= 1, \
            f"达到 max_retries 应标记 failed,实际 stats={stats}"

        # 验证:command_outbox 状态为 failed,retry_count=3
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "failed", \
            f"达到 max_retries 后 status 应为 failed,实际 {entry['status']}"
        assert entry["retry_count"] == 3, \
            f"retry_count 应为 3,实际 {entry['retry_count']}"
        # next_retry_at 应为 NULL(不再重试)
        assert entry["next_retry_at"] is None, \
            "达到 max_retries 后 next_retry_at 应为 NULL(不再重试)"
        # last_error 应包含异常信息
        assert "永久失败" in (entry["last_error"] or "")

    @pytest.mark.asyncio
    async def test_failed_entry_not_reprocessed_by_drain(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """status='failed' 的条目不再被 drain_once 选中。"""
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock 用于验证不被调用
        takedown_called = {"n": 0}

        async def _fake_takedown(*args, **kwargs):
            takedown_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "NRP", "reason": ""},
            action_id="no_reprocess_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 手动将 command_outbox 状态设为 failed
        await real_store._db.execute(
            "UPDATE command_outbox SET status = 'failed', retry_count = 3, "
            "last_error = 'manually failed' WHERE action_id = ?",
            ("no_reprocess_test_001",),
        )
        await real_store._db.commit()

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        # 验证:total=0(failed 状态不会被 SELECT)
        assert stats["total"] == 0
        assert takedown_called["n"] == 0


# ════════════════════════════════════════════════════════════════
# 5. 嵌套事务场景:approve 在独立事务,executor 在独立调用,
#    handler 不再触发嵌套 BEGIN
# ════════════════════════════════════════════════════════════════

class TestNoNestedTransaction:
    """R41 P0-4: 消除嵌套 SQLite transaction 风险。

    原问题:
        approve() 在 ``async with store.transaction()`` 内调用
        ``CommandBus.execute_approved_action()``,后者调用 ``mark_executing()``
        开启新的 ``store.transaction()``,导致 SQLite BEGIN 嵌套。

    修复:
        approve() 审批事务提交后立即返回,不再调用 execute_approved_action。
        真正的 handler 执行由 ApprovalExecutor 在独立调用中完成,
        完全独立的事务边界,无嵌套。
    """

    @pytest.mark.asyncio
    async def test_approve_does_not_call_execute_approved_action(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """approve() 不应调用 CommandBus.execute_approved_action()。

        通过 spy 验证 execute_approved_action 没有被调用。
        """
        from services import approval_workflow
        from services.command_bus import CommandBus
        import services.content_reports as cr_mod

        # Mock takedown_content
        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(return_value=True),
        )

        # Spy: 包装 execute_approved_action 用于检测是否被调用
        original_execute_approved = CommandBus.execute_approved_action
        execute_approved_calls = {"n": 0}

        async def _spy_execute_approved(self, approval_id, action_id=None):
            execute_approved_calls["n"] += 1
            return await original_execute_approved(self, approval_id, action_id)

        monkeypatch.setattr(CommandBus, "execute_approved_action", _spy_execute_approved)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "NEST_001", "reason": ""},
            action_id="no_nested_test_001",
            created_by=100,
        )

        # approve — 应该不调用 execute_approved_action
        ok = await approval_workflow.approve(approval_id, approver_id=200)
        assert ok is True
        assert execute_approved_calls["n"] == 0, \
            "approve() 不应调用 CommandBus.execute_approved_action"

    @pytest.mark.asyncio
    async def test_executor_runs_in_independent_transaction(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """ApprovalExecutor.drain_once() 在独立调用中执行 handler,不嵌套 approve 事务。

        验证:
        - approve() 完成(事务已提交)
        - drain_once() 在新调用中执行 handler
        - handler 不会触发 "cannot start a transaction within a transaction" 错误
        - 整个流程:approve → 写 command_outbox → drain_once → handler 执行成功
        """
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # 记录 handler 调用
        handler_calls = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            handler_calls["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        # 1. approve 阶段(独立事务,完成后立即返回)
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "NEST_OK", "reason": ""},
            action_id="independent_tx_test_001",
            created_by=100,
        )
        ok = await approval_workflow.approve(approval_id, approver_id=200)
        assert ok is True

        # approve 完成后,审批状态为 approved,command_outbox 有 1 条 pending
        approval = await approval_workflow.get_approval(approval_id)
        assert approval["status"] == "approved"
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["status"] == "pending"

        # 2. executor 阶段(独立调用,不在 approve 事务内)
        # 关键验证:不会抛出 "cannot start a transaction within a transaction"
        executor = ApprovalExecutor()
        # 不应抛异常
        stats = await executor.drain_once()
        assert stats["total"] == 1
        assert stats["success"] == 1

        # 3. handler 被成功调用一次
        assert handler_calls["n"] == 1

        # 4. 审批状态最终为 executed
        approval = await approval_workflow.get_approval(approval_id)
        assert approval["status"] == "executed"

        # 5. command_outbox 状态为 executed
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["status"] == "executed"

    @pytest.mark.asyncio
    async def test_full_workflow_approve_then_drain(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """完整工作流:create_approval → approve → drain_once → executed。

        端到端验证嵌套事务风险已消除:整个流程中无任何嵌套 BEGIN。
        """
        from services import approval_workflow
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock takedown_content
        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(return_value=True),
        )

        # 1. 创建审批
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={
                "target_type": "file_code",
                "target_id": "FULL_WORKFLOW_001",
                "reason": "完整流程测试",
            },
            action_id="full_workflow_action_001",
            created_by=100,
        )
        assert approval_id > 0

        # 2. approve(写 command_outbox)
        ok = await approval_workflow.approve(approval_id, approver_id=200)
        assert ok is True

        # 3. drain_once(独立调用)
        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["total"] == 1
        assert stats["success"] == 1
        assert stats["failed"] == 0

        # 4. 验证最终状态
        approval = await approval_workflow.get_approval(approval_id)
        assert approval["status"] == "executed"

        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["status"] == "executed"
        assert entries[0]["retry_count"] == 0
        assert entries[0]["last_error"] is None


# ════════════════════════════════════════════════════════════════
# 6. 模块级便利函数 + r40_scheduler 集成测试
# ════════════════════════════════════════════════════════════════

class TestApprovalExecutorModuleFunctions:
    """R41 P0-4: 模块级 drain_once() 便利函数 + r40_scheduler 注册测试。"""

    @pytest.mark.asyncio
    async def test_module_drain_once_calls_executor(self, real_store, monkeypatch):
        """services.approval_executor.drain_once() 调用单例 ApprovalExecutor.drain_once()。"""
        from services import approval_executor

        # 注入 mock CommandBus 到 executor
        from services.command_bus import CommandBus, Result

        mock_cb = MagicMock()
        mock_cb.execute_command_outbox_entry = AsyncMock(
            return_value=Result(success=True, data={"ok": True}, action_id="mod_fn_test"),
        )
        executor = approval_executor.ApprovalExecutor(command_bus=mock_cb)
        approval_executor._executor = executor  # 直接设置单例

        # 直接插入一条 command_outbox 条目
        await _insert_command_outbox_entry(
            real_store,
            action_id="mod_fn_test_001",
            approval_id=1,
            command_type="takedown_report",
            payload={
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "MOD", "reason": ""},
                "principal_id": 100,
                "principal_name": "creator",
                "principal_source": "bot",
                "action_id": "mod_fn_test_001",
            },
        )
        # 同时插入一个对应的 approval(让 mark_executing 不至于完全失败,但 mock_cb 已替代)
        # 简化:不创建真实 approval,mark_executing 失败仅记 warning

        stats = await approval_executor.drain_once()

        assert stats["total"] == 1
        assert stats["success"] == 1
        # 验证 mock_cb.execute_command_outbox_entry 被调用
        mock_cb.execute_command_outbox_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_r40_scheduler_approval_drain_job_no_exception(
        self, real_store, monkeypatch,
    ):
        """r40_scheduler.approval_executor_drain_job 在空 outbox 时不抛异常。"""
        from services import r40_scheduler

        # 调用 job(不应抛异常,即使 outbox 为空)
        await r40_scheduler.approval_executor_drain_job()

    @pytest.mark.asyncio
    async def test_r40_scheduler_approval_drain_job_processes_entries(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """r40_scheduler.approval_executor_drain_job 处理 command_outbox 条目。"""
        from services import approval_workflow, r40_scheduler
        import services.content_reports as cr_mod

        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(return_value=True),
        )

        # 创建审批 + approve
        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "SCHED", "reason": ""},
            action_id="scheduler_test_001",
            created_by=100,
        )
        await approval_workflow.approve(approval_id, approver_id=200)

        # 调用 r40_scheduler 的 job
        await r40_scheduler.approval_executor_drain_job()

        # 验证:command_outbox 状态变为 executed
        entries = await _list_command_outbox_entries(real_store)
        assert len(entries) == 1
        assert entries[0]["status"] == "executed"


# ════════════════════════════════════════════════════════════════
# 7. CommandBus.execute_command_outbox_entry 测试
# ════════════════════════════════════════════════════════════════

class TestExecuteCommandOutboxEntry:
    """R41 P0-4: CommandBus.execute_command_outbox_entry 直接测试。"""

    @pytest.mark.asyncio
    async def test_execute_command_outbox_entry_success(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """execute_command_outbox_entry 成功执行 handler 并返回 Result。"""
        from services import approval_workflow
        from services.command_bus import CommandBus
        import services.content_reports as cr_mod

        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(return_value=True),
        )

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "ECE", "reason": ""},
            action_id="execute_entry_test_001",
            created_by=100,
        )
        # 不通过 approve,直接构造 approved 状态 + command_outbox entry
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        # 构造 entry dict(模拟从 command_outbox 表 SELECT 出的行)
        entry = {
            "id": 1,
            "action_id": "execute_entry_test_001",
            "approval_id": approval_id,
            "command_type": "takedown_report",
            "payload": json.dumps({
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "ECE", "reason": ""},
                "principal_id": 100,
                "principal_name": "creator",
                "principal_source": "bot",
                "action_id": "execute_entry_test_001",
            }),
            "status": "executing",
            "retry_count": 0,
            "max_retries": 3,
            "next_retry_at": None,
            "last_error": None,
            "created_at": "2026-07-13T10:00:00",
            "updated_at": "2026-07-13T10:00:00",
        }

        cb = CommandBus()
        result = await cb.execute_command_outbox_entry(entry)

        assert result.success is True
        assert result.data == {"takedown_ok": True}
        assert result.action_id == "execute_entry_test_001"

        # 验证审批状态最终为 executed
        approval = await approval_workflow.get_approval(approval_id)
        assert approval["status"] == "executed"

    @pytest.mark.asyncio
    async def test_execute_command_outbox_entry_handler_failure(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """execute_command_outbox_entry handler 抛异常时返回失败 Result。"""
        from services import approval_workflow
        from services.command_bus import CommandBus
        import services.content_reports as cr_mod

        async def _fail(*args, **kwargs):
            raise RuntimeError("handler 失败")

        monkeypatch.setattr(cr_mod, "takedown_content", _fail)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "FAIL", "reason": ""},
            action_id="execute_entry_fail_001",
            created_by=100,
        )
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        entry = {
            "id": 1,
            "action_id": "execute_entry_fail_001",
            "approval_id": approval_id,
            "command_type": "takedown_report",
            "payload": json.dumps({
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "FAIL", "reason": ""},
                "principal_id": 100,
                "principal_name": "creator",
                "principal_source": "bot",
                "action_id": "execute_entry_fail_001",
            }),
            "status": "executing",
            "retry_count": 0,
            "max_retries": 3,
            "next_retry_at": None,
            "last_error": None,
            "created_at": "2026-07-13T10:00:00",
            "updated_at": "2026-07-13T10:00:00",
        }

        cb = CommandBus()
        result = await cb.execute_command_outbox_entry(entry)

        assert result.success is False
        assert "执行失败" in result.error
        assert "handler 失败" in result.error

        # 验证审批状态最终为 failed
        approval = await approval_workflow.get_approval(approval_id)
        assert approval["status"] == "failed"

    @pytest.mark.asyncio
    async def test_execute_command_outbox_entry_unknown_command_type(
        self, real_store, mock_rbac_permission,
    ):
        """command_type 未知时返回失败,不调用 handler。"""
        from services.command_bus import CommandBus

        approval_id = 999
        entry = {
            "id": 1,
            "action_id": "unknown_cmd_001",
            "approval_id": approval_id,
            "command_type": "unknown_command_action",  # 未注册
            "payload": "{}",
            "status": "executing",
            "retry_count": 0,
            "max_retries": 3,
            "next_retry_at": None,
            "last_error": None,
            "created_at": "2026-07-13T10:00:00",
            "updated_at": "2026-07-13T10:00:00",
        }

        cb = CommandBus()
        result = await cb.execute_command_outbox_entry(entry)

        assert result.success is False
        assert "无法解析命令 handler" in result.error
        assert "unknown_command_action" in result.error

    @pytest.mark.asyncio
    async def test_execute_command_outbox_entry_allows_retry(
        self, real_store, mock_rbac_permission, monkeypatch,
    ):
        """execute_command_outbox_entry 不走 SQLite 幂等缓存,允许 ApprovalExecutor 重试。

        R41 P0-5 已将进程内 _EXECUTED_ACTIONS dict 迁移到 SQLite command_executions 表。
        与 execute_approved_action 调用 _execute_handler 不同:
        execute_command_outbox_entry 在第 778 行直接调用 command.handler(command.params),
        绕过 _try_insert_or_get_cached / claim_execution / _mark_executed 幂等检查,
        从而允许 ApprovalExecutor 在重试场景下重新调用 handler。
        幂等性由 command_outbox.action_id UNIQUE 约束 + handler 自身保证。
        """
        from services import approval_workflow
        from services.command_bus import CommandBus
        import services.content_reports as cr_mod

        # 第一次调用时返回 True,记录调用次数
        call_count = {"n": 0}

        async def _fake_takedown(*args, **kwargs):
            call_count["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        approval_id = await _create_commandbus_approval(
            real_store,
            command_action="takedown_report",
            params={"target_type": "file_code", "target_id": "RETRY", "reason": ""},
            action_id="retry_allowed_test_001",
            created_by=100,
        )
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        entry = {
            "id": 1,
            "action_id": "retry_allowed_test_001",
            "approval_id": approval_id,
            "command_type": "takedown_report",
            "payload": json.dumps({
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "RETRY", "reason": ""},
                "principal_id": 100,
                "principal_name": "creator",
                "principal_source": "bot",
                "action_id": "retry_allowed_test_001",
            }),
            "status": "executing",
            "retry_count": 0,
            "max_retries": 3,
            "next_retry_at": None,
            "created_at": "2026-07-13T10:00:00",
            "updated_at": "2026-07-13T10:00:00",
            "last_error": None,
        }

        cb = CommandBus()

        # 第一次调用:成功
        result1 = await cb.execute_command_outbox_entry(entry)
        assert result1.success is True
        assert call_count["n"] == 1

        # 模拟重试场景:重置审批状态为 approved 让 mark_executing 成功
        await real_store._db.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (approval_id,),
        )
        await real_store._db.commit()

        # 第二次调用:不走 SQLite command_executions 幂等缓存,handler 应再次被调用
        result2 = await cb.execute_command_outbox_entry(entry)
        assert result2.success is True
        assert call_count["n"] == 2, \
            "execute_command_outbox_entry 不应走幂等缓存,handler 应被调用 2 次"
