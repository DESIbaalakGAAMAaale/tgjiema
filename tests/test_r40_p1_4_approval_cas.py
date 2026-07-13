"""R40 P1-4: 审批工作流 TOCTOU 修复 — CAS UPDATE 测试。

被测目标:
- ``services.approval_workflow.approve()`` — CAS UPDATE + rowcount 检查
- ``services.approval_workflow.reject()`` — CAS UPDATE + rowcount 检查

测试场景:
1. 并发场景:两个 approve() 同时调用,只有一个成功(CAS UPDATE rowcount=1)
2. 状态不存在时返回失败(approval_id 不在表中)
3. 已审批的记录再次 approve 返回失败(status 非 pending)

修复说明:
- 原 approve()/reject() 先调用 get_approval() 检查 status='pending',
  再执行 UPDATE。两个并发调用者都可能通过 get_approval() 检查,
  导致重复审批(TOCTOU 竞争)。
- 修复后使用 CAS UPDATE: WHERE id=? AND status='pending',
  通过 cursor.rowcount 检测并发竞争(rowcount=0 表示已被处理)。
"""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


# ── Fixture: 真实 SQLite 临时数据库 ──────────────────────────────

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 _cs_module._store 为测试实例,
    使 get_cache_store() 返回正确的测试 store(非模块级导入时的单例)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_4_test_")
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
    """Mock rbac.check_permission 返回 True(让 approve/reject 通过权限检查)。

    approval_workflow 在模块顶层 from services.rbac import check_permission,
    所以需要 patch services.approval_workflow.check_permission。
    """
    with patch("services.approval_workflow.check_permission",
               new=AsyncMock(return_value=True)):
        yield


# ════════════════════════════════════════════════════════════════
# P1-4 测试用例
# ════════════════════════════════════════════════════════════════

class TestApprovalCASApprove:
    """R40 P1-4: approve() CAS UPDATE 测试。"""

    @pytest.mark.asyncio
    async def test_concurrent_approve_only_one_succeeds(self, real_store, mock_rbac_permission):
        """并发场景:两个 approve() 同时调用,只有一个成功。

        验证 CAS UPDATE 通过 rowcount 检测并发竞争:
        - 两个调用者都通过 get_approval() 检查(status=pending)
        - 两个调用者都通过 check_permission 检查
        - CAS UPDATE WHERE status='pending' 只有一个 rowcount=1
        - 另一个 rowcount=0,返回 False
        """
        from services import approval_workflow

        # 创建审批记录(created_by=100, approver=200,非自审批)
        approval_id = await approval_workflow.create_approval(
            action="takedown",
            payload={"target_user_id": 999, "reason": "测试并发"},
            created_by=100,
        )
        assert approval_id > 0, "创建审批失败"

        # 并发调用两个 approve()(使用 asyncio.gather 同时启动)
        results = await asyncio.gather(
            approval_workflow.approve(approval_id, approver_id=200, note="审批人A"),
            approval_workflow.approve(approval_id, approver_id=201, note="审批人B"),
        )

        # 验证:只有一个成功
        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)
        assert success_count == 1, f"应只有一个 approve 成功,实际 {success_count}"
        assert failure_count == 1, f"应有一个 approve 失败,实际 {failure_count}"

        # 验证最终状态为 approved
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_returns_false(self, real_store, mock_rbac_permission):
        """审批不存在的 approval_id 时返回 False。"""
        from services import approval_workflow

        result = await approval_workflow.approve(
            approval_id=99999,  # 不存在的 ID
            approver_id=200,
            note="不存在的审批",
        )
        assert result is False, "审批不存在的 ID 应返回 False"

    @pytest.mark.asyncio
    async def test_reapprove_already_approved_returns_false(self, real_store, mock_rbac_permission):
        """已审批的记录再次 approve 返回 False(CAS UPDATE rowcount=0)。"""
        from services import approval_workflow

        # 创建审批记录
        approval_id = await approval_workflow.create_approval(
            action="ban",
            payload={"target_user_id": 888, "reason": "违规"},
            created_by=100,
        )
        assert approval_id > 0

        # 第一次 approve 成功
        result1 = await approval_workflow.approve(approval_id, approver_id=200, note="第一次")
        assert result1 is True, "第一次 approve 应成功"

        # 第二次 approve 失败(status 已变为 'approved',CAS UPDATE rowcount=0)
        result2 = await approval_workflow.approve(approval_id, approver_id=201, note="第二次")
        assert result2 is False, "已审批的记录再次 approve 应返回 False"

        # 验证状态仍为 approved
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_after_reject_returns_false(self, real_store, mock_rbac_permission):
        """已驳回的记录再 approve 返回 False(CAS UPDATE rowcount=0)。"""
        from services import approval_workflow

        approval_id = await approval_workflow.create_approval(
            action="restore",
            payload={"file_code": "TEST001"},
            created_by=100,
        )
        assert approval_id > 0

        # 先驳回
        reject_result = await approval_workflow.reject(approval_id, approver_id=200, reason="不批准")
        assert reject_result is True, "reject 应成功"

        # 再 approve — CAS 失败(status='rejected' != 'pending')
        approve_result = await approval_workflow.approve(approval_id, approver_id=201, note="尝试批准")
        assert approve_result is False, "已驳回的记录再 approve 应返回 False"

        # 验证状态仍为 rejected
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "rejected"


class TestApprovalCASReject:
    """R40 P1-4: reject() CAS UPDATE 测试。"""

    @pytest.mark.asyncio
    async def test_concurrent_reject_only_one_succeeds(self, real_store, mock_rbac_permission):
        """并发场景:两个 reject() 同时调用,只有一个成功。"""
        from services import approval_workflow

        approval_id = await approval_workflow.create_approval(
            action="delete_data",
            payload={"target": "test_data"},
            created_by=100,
        )
        assert approval_id > 0

        # 并发调用两个 reject()
        results = await asyncio.gather(
            approval_workflow.reject(approval_id, approver_id=200, reason="驳回人A"),
            approval_workflow.reject(approval_id, approver_id=201, reason="驳回人B"),
        )

        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)
        assert success_count == 1, f"应只有一个 reject 成功,实际 {success_count}"
        assert failure_count == 1, f"应有一个 reject 失败,实际 {failure_count}"

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_nonexistent_returns_false(self, real_store, mock_rbac_permission):
        """驳回不存在的 approval_id 时返回 False。"""
        from services import approval_workflow

        result = await approval_workflow.reject(
            approval_id=99999,
            approver_id=200,
            reason="不存在的审批",
        )
        assert result is False, "驳回不存在的 ID 应返回 False"

    @pytest.mark.asyncio
    async def test_reject_after_approve_returns_false(self, real_store, mock_rbac_permission):
        """已批准的记录再 reject 返回 False(CAS UPDATE rowcount=0)。"""
        from services import approval_workflow

        approval_id = await approval_workflow.create_approval(
            action="config_change",
            payload={"key": "test", "value": "1"},
            created_by=100,
        )
        assert approval_id > 0

        # 先批准
        approve_result = await approval_workflow.approve(approval_id, approver_id=200, note="批准")
        assert approve_result is True

        # 再驳回 — CAS 失败
        reject_result = await approval_workflow.reject(approval_id, approver_id=201, reason="尝试驳回")
        assert reject_result is False, "已批准的记录再 reject 应返回 False"

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"


class TestApprovalCASMixed:
    """R40 P1-4: approve/reject 混合并发测试。"""

    @pytest.mark.asyncio
    async def test_concurrent_approve_and_reject_only_one_succeeds(self, real_store, mock_rbac_permission):
        """并发场景:approve() 和 reject() 同时调用,只有一个成功。"""
        from services import approval_workflow

        approval_id = await approval_workflow.create_approval(
            action="factory_reset",
            payload={"confirm": True},
            created_by=100,
        )
        assert approval_id > 0

        # 并发:一个 approve,一个 reject
        results = await asyncio.gather(
            approval_workflow.approve(approval_id, approver_id=200, note="批准"),
            approval_workflow.reject(approval_id, approver_id=201, reason="驳回"),
        )

        # 只有一个成功
        success_count = sum(1 for r in results if r is True)
        assert success_count == 1, f"approve/reject 并发应只有一个成功,实际 {success_count}"

        # 验证最终状态(approved 或 rejected,取决于调度)
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] in ("approved", "rejected")
