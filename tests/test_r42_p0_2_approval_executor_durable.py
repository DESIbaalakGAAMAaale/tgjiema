"""R42 P0-2: ApprovalExecutor 接入 command_executions 持久幂等测试。

被测目标:
- ``services.command_bus.claim_execution_for_outbox`` — 与 outbox 共享 action_id 的 CAS claim
- ``services.command_bus.mark_outbox_executed`` — 同事务更新三处状态
- ``services.command_bus.release_lease`` — 释放 lease(handler 失败后)
- ``services.approval_executor.ApprovalExecutor._process_entry`` — 接入持久幂等
- ``services.approval_workflow.mark_approval_executed`` — 标记审批已执行

测试场景:
1. claim outbox → claim command_executions → 执行 handler → 三处状态都为 executed
2. command_executions 已 executed → 幂等跳过(不重复执行 handler)
3. command_executions 被其他 worker 占用(lease 未过期)→ 跳过本轮
4. handler 失败 → release_lease + 安排重试
5. request_hash 不匹配 → 路由到 DLQ
6. approval_id 存在时同步标记 approval 为 executed
7. 顶层异常不导致 command_executions 卡在 executing
8. mark_outbox_executed 在同一事务内更新三处
9. release_lease 正确释放
10. claim_execution_for_outbox 各种状态返回
11. 多次重试后达到 max_retries 标记 failed
12. 空 action_id 处理
13. approval_id 为 None 时跳过 approval 更新
14. handler 返回 result 存入 command_executions.result
15. lease 过期后可被重新 claim

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- Mock content_reports.takedown_content 等 handler 副作用
- 每个用例前重置 ApprovalExecutor 单例和 CommandBus 幂等缓存
"""
import asyncio
import datetime
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
    tmpdir = tempfile.mkdtemp(prefix="r42_p0_2_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def mock_rbac_permission(real_store):
    """Mock rbac.check_permission 返回 True(让 approve 通过权限检查)。"""
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


@pytest_asyncio.fixture
async def clean_tables(real_store):
    """每个用例前清空 command_executions / command_outbox / approvals 表。"""
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.execute("DELETE FROM command_outbox")
    await real_store._db.execute("DELETE FROM approvals")
    await real_store._db.execute("DELETE FROM audit_log")
    await real_store._db.commit()
    yield real_store
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.execute("DELETE FROM command_outbox")
    await real_store._db.execute("DELETE FROM approvals")
    await real_store._db.commit()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _compute_request_hash(params: dict) -> str:
    """计算 params 的 SHA256(与 command_bus._compute_request_hash 一致)。"""
    from services.command_bus import _compute_request_hash
    return _compute_request_hash(params)


async def _insert_command_outbox_entry(
    store,
    action_id: str,
    approval_id: int = 0,
    command_type: str = "takedown_report",
    payload: Optional[dict] = None,
    status: str = "pending",
    retry_count: int = 0,
    max_retries: int = 3,
    next_retry_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> int:
    """直接向 command_outbox 表插入一条记录(测试辅助)。"""
    if payload is None:
        payload = {
            "command_action": command_type,
            "params": {"target_type": "file_code", "target_id": "TC001", "reason": "test"},
            "principal_id": 100,
            "principal_name": "creator",
            "principal_source": "bot",
            "action_id": action_id,
        }
    now = datetime.datetime.now().isoformat()
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


async def _insert_command_execution(
    store,
    action_id: str,
    status: str = "pending",
    owner: Optional[str] = None,
    lease_until: Optional[str] = None,
    request_hash: Optional[str] = None,
    result: Optional[str] = None,
    command_type: str = "outbox",
    principal_id: int = 0,
) -> None:
    """直接向 command_executions 表插入一条记录(测试辅助)。"""
    if request_hash is None:
        request_hash = _compute_request_hash({"target_type": "file_code", "target_id": "TC001", "reason": "test"})
    now = datetime.datetime.now().isoformat()
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        " request_hash, result, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, command_type, principal_id, status, owner, lease_until,
         request_hash, result, now, now),
    )
    await store._db.commit()


async def _get_command_outbox_entry(store, action_id: str) -> Optional[dict]:
    """按 action_id 读取 command_outbox 条目。"""
    rows = await store._db.execute_fetchall(
        "SELECT id, action_id, approval_id, command_type, payload, status, "
        "retry_count, max_retries, next_retry_at, last_error, "
        "created_at, updated_at "
        "FROM command_outbox WHERE action_id = ?",
        (action_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r[0], "action_id": r[1], "approval_id": r[2],
        "command_type": r[3], "payload": r[4], "status": r[5],
        "retry_count": r[6], "max_retries": r[7], "next_retry_at": r[8],
        "last_error": r[9], "created_at": r[10], "updated_at": r[11],
    }


async def _get_command_execution(store, action_id: str) -> Optional[dict]:
    """按 action_id 读取 command_executions 记录。"""
    rows = await store._db.execute_fetchall(
        "SELECT action_id, command_type, principal_id, status, owner, "
        "lease_until, request_hash, result, created_at, updated_at "
        "FROM command_executions WHERE action_id = ?",
        (action_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "action_id": r[0], "command_type": r[1], "principal_id": r[2],
        "status": r[3], "owner": r[4], "lease_until": r[5],
        "request_hash": r[6], "result": r[7],
        "created_at": r[8], "updated_at": r[9],
    }


async def _create_approval_record(
    store,
    action: str = "takedown",
    payload: Optional[dict] = None,
    status: str = "approved",
    created_by: int = 100,
) -> int:
    """直接插入 approvals 记录,返回 approval_id。"""
    if payload is None:
        payload = {"command_action": "takedown_report", "params": {}}
    now = datetime.datetime.now().isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    cursor = await store._db.execute(
        "INSERT INTO approvals (action, payload, status, approver_id, approver_note, "
        "created_by, created_at, resolved_at) VALUES (?, ?, ?, NULL, '', ?, ?, NULL)",
        (action, payload_str, status, created_by, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


# ════════════════════════════════════════════════════════════════
# 1. claim outbox → claim command_executions → handler → 三处 executed
# ════════════════════════════════════════════════════════════════

class TestApprovalExecutorDurableFullFlow:
    """R42 P0-2: ApprovalExecutor 持久幂等完整流程。"""

    @pytest.mark.asyncio
    async def test_claim_outbox_then_claim_executions_then_handler_success(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """完整流程:claim outbox → claim executions → handler → 三处 executed。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        # Mock handler 成功
        handler_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            handler_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_full_flow_001"
        params = {"target_type": "file_code", "target_id": "FULL_001", "reason": "test"}
        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="approved",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["success"] == 1, f"应成功 1 条,实际: {stats}"
        assert handler_called["n"] == 1, "handler 应被调用 1 次"

        # 三处状态都为 executed
        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "executed", \
            f"command_outbox.status 应为 executed,实际: {outbox['status']}"

        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row is not None, "command_executions 应有记录"
        assert exec_row["status"] == "executed", \
            f"command_executions.status 应为 executed,实际: {exec_row['status']}"

        # approval 也应为 executed
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,),
        )
        assert rows and rows[0][0] == "executed", \
            f"approval.status 应为 executed,实际: {rows[0][0] if rows else 'N/A'}"

    @pytest.mark.asyncio
    async def test_approval_id_marked_executed(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """approval_id 存在时同步标记 approval 为 executed。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_approval_marked_001"
        params = {"target_type": "file_code", "target_id": "APR_001", "reason": "x"}
        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="approved",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        await executor.drain_once()

        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,),
        )
        assert rows and rows[0][0] == "executed"

    @pytest.mark.asyncio
    async def test_handler_result_stored_in_command_executions(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """handler 返回 result 存入 command_executions.result。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_result_store_001"
        params = {"target_type": "file_code", "target_id": "RES_001", "reason": "test"}
        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="approved",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        await executor.drain_once()

        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row is not None
        assert exec_row["result"] is not None, "result 不应为 None"
        result_data = json.loads(exec_row["result"])
        assert result_data["success"] is True, \
            f"result.success 应为 True,实际: {result_data}"


# ════════════════════════════════════════════════════════════════
# 2. 幂等跳过(command_executions 已 executed)
# ════════════════════════════════════════════════════════════════

class TestIdempotentSkip:
    """R42 P0-2: command_executions 已 executed → 幂等跳过。"""

    @pytest.mark.asyncio
    async def test_already_executed_skips_handler_idempotent(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """command_executions 已 executed → 直接 mark_executed,不重复执行 handler。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        handler_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            handler_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_idem_skip_001"
        params = {"target_type": "file_code", "target_id": "IDEM_001", "reason": "x"}
        request_hash = _compute_request_hash(params)

        # 预置 command_executions 为 executed + 缓存 result
        cached_result = json.dumps({"success": True, "data": {"takedown_ok": True}, "error": ""})
        await _insert_command_execution(
            real_store, action_id=action_id, status="executed",
            request_hash=request_hash, result=cached_result,
        )

        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="approved",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["success"] == 1, f"幂等跳过应计为 success,实际: {stats}"
        assert handler_called["n"] == 0, "handler 不应被重复调用"

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "executed", "outbox 应被 mark_executed"


# ════════════════════════════════════════════════════════════════
# 3. claimed_by_other 跳过本轮
# ════════════════════════════════════════════════════════════════

class TestClaimedByOtherSkip:
    """R42 P0-2: command_executions 被其他 worker 占用 → 跳过本轮。"""

    @pytest.mark.asyncio
    async def test_claimed_by_other_skips_round(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """lease 未过期 → claimed_by_other → 跳过本轮,outbox 回退 pending。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        handler_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            handler_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_claimed_other_001"
        params = {"target_type": "file_code", "target_id": "CLM_001", "reason": "x"}
        request_hash = _compute_request_hash(params)

        # 预置 command_executions 为 executing,lease 未过期(future)
        future = (datetime.datetime.utcnow() + datetime.timedelta(seconds=300)).isoformat()
        await _insert_command_execution(
            real_store, action_id=action_id, status="executing",
            owner="other_host:1234", lease_until=future,
            request_hash=request_hash,
        )

        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["skipped"] == 1, f"应 skipped 1 条,实际: {stats}"
        assert handler_called["n"] == 0, "handler 不应被调用"

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "pending", \
            f"outbox 应回退到 pending,实际: {outbox['status']}"


# ════════════════════════════════════════════════════════════════
# 4. handler 失败 → release_lease + 安排重试
# ════════════════════════════════════════════════════════════════

class TestHandlerFailureReleasesLease:
    """R42 P0-2: handler 失败时释放 lease + 安排重试。"""

    @pytest.mark.asyncio
    async def test_handler_failure_releases_lease_and_schedules_retry(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """handler 失败 → release_lease + retry_scheduled。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            raise RuntimeError("模拟 handler 失败")

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_handler_fail_001"
        params = {"target_type": "file_code", "target_id": "FAIL_001", "reason": "x"}
        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="approved",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["retry_scheduled"] == 1, \
            f"应 retry_scheduled 1 条,实际: {stats}"

        # command_executions 应被 release_lease 回退到 pending
        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row is not None, "command_executions 应有记录"
        assert exec_row["status"] == "pending", \
            f"release_lease 后 status 应为 pending,实际: {exec_row['status']}"
        assert exec_row["owner"] is None, "owner 应被清空"
        assert exec_row["lease_until"] is None, "lease_until 应被清空"

        # command_outbox 应为 pending(等待重试)
        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "pending", \
            f"outbox 应为 pending(等待重试),实际: {outbox['status']}"
        assert outbox["retry_count"] == 1, "retry_count 应递增为 1"


# ════════════════════════════════════════════════════════════════
# 5. request_hash 不匹配 → 路由到 DLQ
# ════════════════════════════════════════════════════════════════

class TestRequestHashMismatchDLQ:
    """R42 P0-2: request_hash 不匹配 → 路由到 DLQ(标记 failed)。"""

    @pytest.mark.asyncio
    async def test_request_hash_mismatch_routes_to_dlq(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """request_hash 不匹配 → 标记 failed,不执行 handler。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        handler_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            handler_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_hash_mismatch_001"
        # 预置 command_executions 用 hash_A
        await _insert_command_execution(
            real_store, action_id=action_id, status="pending",
            request_hash="hash_A_original",
        )

        # outbox 中的 params 会计算出 hash_B(不同)
        params = {"target_type": "file_code", "target_id": "MISM_001", "reason": "different"}
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["failed"] == 1, f"应 failed 1 条(DLQ),实际: {stats}"
        assert handler_called["n"] == 0, "handler 不应被调用(防篡改拒绝)"

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "failed", \
            f"outbox 应为 failed(DLQ),实际: {outbox['status']}"
        assert "request_hash" in (outbox["last_error"] or ""), \
            f"last_error 应包含 request_hash,实际: {outbox['last_error']}"


# ════════════════════════════════════════════════════════════════
# 6. 顶层异常不导致 command_executions 卡在 executing
# ════════════════════════════════════════════════════════════════

class TestTopLevelExceptionReleasesLease:
    """R42 P0-2: 顶层异常时 release_lease 防止卡在 executing。"""

    @pytest.mark.asyncio
    async def test_top_level_exception_releases_lease(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """_process_entry 抛异常 → 顶层 release_lease + mark failed。"""
        from services.approval_executor import ApprovalExecutor
        from services import command_bus as cb_mod

        action_id = "r42_top_level_exc_001"
        params = {"target_type": "file_code", "target_id": "TOP_001", "reason": "x"}

        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        # Mock claim_execution_for_outbox 抛异常(模拟顶层异常)
        async def _raising_claim(*args, **kwargs):
            raise RuntimeError("模拟 claim 异常")

        monkeypatch.setattr(cb_mod, "claim_execution_for_outbox", _raising_claim)

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["failed"] == 1, f"应 failed 1 条,实际: {stats}"

        # 顶层异常后 command_executions 应不卡在 executing(无记录或非 executing)
        # 由于 claim 抛异常,INSERT 可能未发生;但如果有记录则不应为 executing
        exec_row = await _get_command_execution(real_store, action_id)
        if exec_row is not None:
            assert exec_row["status"] != "executing", \
                f"顶层异常后不应卡在 executing,实际: {exec_row['status']}"

        # outbox 应被 _handle_failure 处理(标记 failed 或 pending 重试)
        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] in ("pending", "failed"), \
            f"outbox 应为 pending/failed,实际: {outbox['status']}"


# ════════════════════════════════════════════════════════════════
# 7. mark_outbox_executed 在同一事务内更新三处
# ════════════════════════════════════════════════════════════════

class TestMarkOutboxExecutedAtomic:
    """R42 P0-2: mark_outbox_executed 原子更新三处状态。"""

    @pytest.mark.asyncio
    async def test_mark_outbox_executed_atomic_three_places(
        self, real_store, clean_tables,
    ):
        """mark_outbox_executed 在同一事务内更新三处。"""
        from services.command_bus import mark_outbox_executed

        action_id = "r42_atomic_001"
        params = {"target_type": "file_code", "target_id": "ATM_001", "reason": "x"}
        request_hash = _compute_request_hash(params)

        # 预置三处记录
        await _insert_command_execution(
            real_store, action_id=action_id, status="executing",
            request_hash=request_hash,
        )
        approval_id = await _create_approval_record(
            real_store, action="takedown",
            payload={"command_action": "takedown_report", "params": params},
            status="executing",
        )
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=approval_id,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "action_id": action_id},
            status="executing",
        )

        # 执行 mark_outbox_executed
        result_dict = {"success": True, "data": {"takedown_ok": True}, "error": ""}
        ok = await mark_outbox_executed(action_id, result_dict, approval_id=approval_id)
        assert ok is True, "mark_outbox_executed 应返回 True"

        # 验证三处都为 executed
        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row["status"] == "executed"
        assert exec_row["result"] is not None

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "executed"

        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,),
        )
        assert rows and rows[0][0] == "executed"


# ════════════════════════════════════════════════════════════════
# 8. release_lease 正确释放
# ════════════════════════════════════════════════════════════════

class TestReleaseLease:
    """R42 P0-2: release_lease 释放 lease。"""

    @pytest.mark.asyncio
    async def test_release_lease_resets_status(
        self, real_store, clean_tables,
    ):
        """release_lease 将 status 回退到 pending,清空 owner/lease_until。"""
        from services.command_bus import release_lease

        action_id = "r42_release_001"
        future = (datetime.datetime.utcnow() + datetime.timedelta(seconds=300)).isoformat()
        await _insert_command_execution(
            real_store, action_id=action_id, status="executing",
            owner="host:1234", lease_until=future,
        )

        ok = await release_lease(action_id)
        assert ok is True, "release_lease 应返回 True"

        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row["status"] == "pending", \
            f"status 应为 pending,实际: {exec_row['status']}"
        assert exec_row["owner"] is None, "owner 应被清空"
        assert exec_row["lease_until"] is None, "lease_until 应被清空"

    @pytest.mark.asyncio
    async def test_release_lease_nonexistent_action_id_returns_false(
        self, real_store, clean_tables,
    ):
        """release_lease 对不存在的 action_id 返回 False。"""
        from services.command_bus import release_lease
        ok = await release_lease("nonexistent_action_id_xxx")
        assert ok is False, "不存在的 action_id 应返回 False"


# ════════════════════════════════════════════════════════════════
# 9. claim_execution_for_outbox 各种状态返回
# ════════════════════════════════════════════════════════════════

class TestClaimExecutionForOutboxStatuses:
    """R42 P0-2: claim_execution_for_outbox 各种状态返回。"""

    @pytest.mark.asyncio
    async def test_claim_execution_for_outbox_various_statuses(
        self, real_store, clean_tables,
    ):
        """claim_execution_for_outbox 各种状态返回(claimed / already_executed / claimed_by_other / hash_mismatch)。"""
        from services.command_bus import claim_execution_for_outbox

        owner = "test_host:1234"

        # 1. 行不存在 → INSERT → claimed
        result = await claim_execution_for_outbox(
            "r42_status_new", _compute_request_hash({"a": 1}), owner,
        )
        assert result["status"] == "claimed", f"新 action_id 应 claimed,实际: {result}"

        # 2. 已 executed → already_executed
        await _insert_command_execution(
            real_store, action_id="r42_status_executed", status="executed",
            request_hash=_compute_request_hash({"b": 2}),
            result='{"success": true}',
        )
        result = await claim_execution_for_outbox(
            "r42_status_executed", _compute_request_hash({"b": 2}), owner,
        )
        assert result["status"] == "already_executed", \
            f"已 executed 应 already_executed,实际: {result}"

        # 3. executing + lease 未过期 → claimed_by_other
        future = (datetime.datetime.utcnow() + datetime.timedelta(seconds=300)).isoformat()
        await _insert_command_execution(
            real_store, action_id="r42_status_busy", status="executing",
            owner="other:5678", lease_until=future,
            request_hash=_compute_request_hash({"c": 3}),
        )
        result = await claim_execution_for_outbox(
            "r42_status_busy", _compute_request_hash({"c": 3}), owner,
        )
        assert result["status"] == "claimed_by_other", \
            f"executing+lease 未过期 应 claimed_by_other,实际: {result}"

        # 4. request_hash 不匹配 → hash_mismatch
        await _insert_command_execution(
            real_store, action_id="r42_status_mismatch", status="pending",
            request_hash="original_hash",
        )
        result = await claim_execution_for_outbox(
            "r42_status_mismatch", "different_hash", owner,
        )
        assert result["status"] == "hash_mismatch", \
            f"hash 不匹配 应 hash_mismatch,实际: {result}"

    @pytest.mark.asyncio
    async def test_lease_expired_can_be_reclaimed(
        self, real_store, clean_tables,
    ):
        """lease 过期后可被重新 claim(CAS UPDATE 命中)。"""
        from services.command_bus import claim_execution_for_outbox

        action_id = "r42_lease_expired_001"
        # 预置 executing 但 lease 已过期
        past = (datetime.datetime.utcnow() - datetime.timedelta(seconds=300)).isoformat()
        request_hash = _compute_request_hash({"x": 1})
        await _insert_command_execution(
            real_store, action_id=action_id, status="executing",
            owner="old_host:1234", lease_until=past,
            request_hash=request_hash,
        )

        result = await claim_execution_for_outbox(action_id, request_hash, "new_host:5678")
        assert result["status"] == "claimed", \
            f"lease 过期后应可被重新 claim,实际: {result}"

        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row["owner"] == "new_host:5678", "owner 应更新为新 worker"
        assert exec_row["status"] == "executing"


# ════════════════════════════════════════════════════════════════
# 10. 多次重试后达到 max_retries 标记 failed
# ════════════════════════════════════════════════════════════════

class TestMaxRetriesMarkFailed:
    """R42 P0-2: 达到 max_retries 标记 failed。"""

    @pytest.mark.asyncio
    async def test_max_retries_marks_failed(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """retry_count 达到 max_retries → status='failed'。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            raise RuntimeError("持续失败")

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_max_retry_001"
        params = {"target_type": "file_code", "target_id": "MAX_001", "reason": "x"}
        # retry_count=2, max_retries=3 → 一次失败后 retry_count=3 >= max_retries → failed
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
            retry_count=2, max_retries=3,
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["failed"] == 1, f"应 failed 1 条,实际: {stats}"

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "failed", \
            f"达到 max_retries 应 failed,实际: {outbox['status']}"
        assert outbox["retry_count"] == 3, "retry_count 应为 3"


# ════════════════════════════════════════════════════════════════
# 11. 空 action_id 处理
# ════════════════════════════════════════════════════════════════

class TestEmptyActionIdHandling:
    """R42 P0-2: 空 action_id 处理(不应崩溃)。"""

    @pytest.mark.asyncio
    async def test_empty_action_id_handling(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """空 action_id → 不崩溃,handler 仍可执行(降级模式)。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        params = {"target_type": "file_code", "target_id": "EMPTY_001", "reason": "x"}
        # action_id 为空字符串
        await _insert_command_outbox_entry(
            real_store, action_id="", approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": ""},
        )

        executor = ApprovalExecutor()
        # 不应抛异常
        stats = await executor.drain_once()

        # 空 action_id 仍可被处理(claim_execution_for_outbox 用空字符串作为 PK)
        assert stats["total"] == 1, f"应处理 1 条,实际: {stats}"


# ════════════════════════════════════════════════════════════════
# 12. approval_id 为 None 时跳过 approval 更新
# ════════════════════════════════════════════════════════════════

class TestApprovalIdNoneSkipsUpdate:
    """R42 P0-2: approval_id 为 None 时跳过 approval 更新。"""

    @pytest.mark.asyncio
    async def test_approval_id_none_skips_approval_update(
        self, real_store, clean_tables, mock_rbac_permission, monkeypatch,
    ):
        """approval_id=0(视为 None)→ 不更新 approvals 表。"""
        from services.approval_executor import ApprovalExecutor
        import services.content_reports as cr_mod

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        action_id = "r42_no_approval_001"
        params = {"target_type": "file_code", "target_id": "NOAPR_001", "reason": "x"}
        # approval_id=0
        await _insert_command_outbox_entry(
            real_store, action_id=action_id, approval_id=0,
            command_type="takedown_report",
            payload={"command_action": "takedown_report", "params": params,
                     "principal_id": 100, "principal_name": "creator",
                     "principal_source": "bot", "action_id": action_id},
        )

        executor = ApprovalExecutor()
        stats = await executor.drain_once()

        assert stats["success"] == 1, f"应成功 1 条,实际: {stats}"

        # command_executions 和 command_outbox 应为 executed
        exec_row = await _get_command_execution(real_store, action_id)
        assert exec_row["status"] == "executed"

        outbox = await _get_command_outbox_entry(real_store, action_id)
        assert outbox["status"] == "executed"

        # approvals 表应为空(approval_id=0 跳过更新)
        rows = await real_store._db.execute_fetchall(
            "SELECT COUNT(*) FROM approvals"
        )
        assert rows[0][0] == 0, "approvals 表应为空(approval_id=0 跳过更新)"


# ════════════════════════════════════════════════════════════════
# 13. mark_approval_executed(新函数)
# ════════════════════════════════════════════════════════════════

class TestMarkApprovalExecuted:
    """R42 P0-2: approval_workflow.mark_approval_executed 函数。"""

    @pytest.mark.asyncio
    async def test_mark_approval_executed_updates_status(
        self, real_store, clean_tables,
    ):
        """mark_approval_executed 将 approval 状态更新为 executed。"""
        from services.approval_workflow import mark_approval_executed

        approval_id = await _create_approval_record(
            real_store, action="takedown", status="approved",
        )
        ok = await mark_approval_executed(approval_id, action_id="r42_mark_apr_001")
        assert ok is True, "mark_approval_executed 应返回 True"

        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,),
        )
        assert rows and rows[0][0] == "executed"

    @pytest.mark.asyncio
    async def test_mark_approval_executed_already_executed_returns_false(
        self, real_store, clean_tables,
    ):
        """对已 executed 的 approval 调用 → 返回 False(幂等)。"""
        from services.approval_workflow import mark_approval_executed

        approval_id = await _create_approval_record(
            real_store, action="takedown", status="executed",
        )
        ok = await mark_approval_executed(approval_id, action_id="r42_mark_apr_002")
        assert ok is False, "已 executed 的 approval 应返回 False"
