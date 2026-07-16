"""R42 P1-12: 维护模式失败保持锁定 + 人工批准恢复测试。

被测目标:
- ``services.maintenance_mode.execute_maintenance_workflow`` 失败分支设置 recover_status='pending'
- ``services.maintenance_mode.disable`` 增加 approval_action_id 参数(recover_status='pending' 时校验)
- ``services.maintenance_mode.enable`` 重置 recover_status='completed'(新维护周期开始)
- ``services.maintenance_mode.recover_maintenance(principal_id, reason, approval_action_id, request_hash)``
- ``services.rbac.PERMISSION_MAINTENANCE_RECOVER`` 权限常量 + 角色映射

R51 P1-6 适配:
    disable / recover_maintenance 在 recover_status='pending' 时新增 request_hash 强制绑定,
    所有调用必须同时提供 request_hash + approval_action_id + principal(ended_by)。
    本测试已更新所有调用点以适配此要求。

测试场景(15 个用例):
 1. workflow 失败时 maintenance_kept_enabled=True
 2. workflow 失败时 recover_status='pending'(强制审批恢复)
 3. enable() 重置 recover_status='completed'(新维护周期开始)
 4. disable() 在 recover_status='pending' 时拒绝关闭(无 approval_action_id)
 5. disable() 在 recover_status='pending' 且 approval_action_id 有效时允许关闭
 6. disable() 在 recover_status='pending' 但 approval 未 executed 时拒绝
 7. disable() 在 recover_status='pending' 但 approval_action_id 不存在时拒绝
 8. recover_maintenance 无 approval_action_id 抛 PermissionError
 9. recover_maintenance approval 未 executed 抛 PermissionError
10. recover_maintenance approval_action_id 不存在抛 PermissionError
11. recover_maintenance 无 maintenance:recover 权限抛 PermissionError
12. recover_maintenance 通过后 maintenance 关闭
13. recover_maintenance 成功后写 audit_log(action=recover_maintenance)
14. recover_maintenance 成功后 recover_status='completed'
15. RBAC super_admin / ops 默认角色拥有 maintenance:recover 权限

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- Mock drain_queues / trigger_backup / check_readiness 模拟 workflow 各步骤
- Mock services.rbac.check_permission 控制权限放行/拒绝
- 通过直接 INSERT command_executions 模拟 CommandBus 审批结果
- R51 P1-6: 所有 disable / recover_maintenance 调用传入 request_hash="test_hash_001"
"""
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# R51 P1-6 适配: 全测试统一的 request_hash 值(与 _insert_command_execution 默认值一致)
# R55 P0-2: request_hash 强制 64 位 hex(满足 claim_execution_approved 校验)
_TEST_REQUEST_HASH = "a" * 64

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
    tmpdir = tempfile.mkdtemp(prefix="r42_p1_12_test_")
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
async def reset_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_command_execution(
    store,
    action_id: str,
    status: str = "approved",
    request_hash: str = "a" * 64,  # R55 P0-2: 64 位 hex
    result_json: str = '{"success": true}',
):
    """直接插入一条 command_executions 记录(模拟 CommandBus 审批结果)。

    R52 P0-5: 状态机统一为 pending → approved → executing → executed/failed,
    审批通过后执行前的状态为 'approved'(旧版 'executed' 语义冲突已废弃)。
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        " request_hash, result, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (action_id, "recover_maintenance", 100, status, "test_worker",
         request_hash, result_json, now, now),
    )
    await store._db.commit()


async def _set_recover_status(store, status: str):
    """直接 UPDATE maintenance_state.recover_status(用于场景构造)。"""
    await store._db.execute(
        "UPDATE maintenance_state SET recover_status = ? WHERE id = ?",
        (status, 1),
    )
    await store._db.commit()


async def _get_recover_status(store) -> str:
    """查询 maintenance_state.recover_status。"""
    rows = await store._db.execute_fetchall(
        "SELECT recover_status FROM maintenance_state WHERE id = ?", (1,)
    )
    if rows and rows[0]:
        return rows[0][0] or "completed"
    return "completed"


# ════════════════════════════════════════════════════════════════
# P1-12 测试用例
# ════════════════════════════════════════════════════════════════

class TestWorkflowFailureSetsRecoverPending:
    """R42 P1-12: workflow 失败时设置 recover_status='pending'。"""

    @pytest.mark.asyncio
    async def test_drain_failure_keeps_enabled_and_sets_pending(
        self, real_store, reset_cache
    ):
        """workflow 失败(drain 失败)时保持 enabled 且 recover_status='pending'。"""
        from services import maintenance_mode

        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 5,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 drain 失败 → recover_pending",
                started_by=100,
                auto_disable=True,
            )

        # workflow 失败 + 保持 enabled
        assert result["success"] is False, "drain 失败时 workflow 应失败"
        assert result["maintenance_kept_enabled"] is True, \
            "drain 失败时应保持 maintenance enabled"

        # recover_status 应为 pending(强制审批恢复)
        recover_status = await _get_recover_status(real_store)
        assert recover_status == "pending", \
            f"workflow 失败后 recover_status 应为 'pending',实际: {recover_status}"

        # maintenance_state 仍为 enabled
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "drain 失败后 maintenance_state 应仍为 enabled"

    @pytest.mark.asyncio
    async def test_backup_failure_sets_recover_pending(self, real_store, reset_cache):
        """workflow 失败(backup 失败)时 recover_status='pending'。"""
        from services import maintenance_mode

        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": True,
                "remaining_outbox": 0,
                "remaining_jobs": 0,
                "timeout": False,
            }),
        ), patch(
            "services.disaster_recovery.trigger_backup",
            new=AsyncMock(side_effect=Exception("R2 备份失败")),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 backup 失败 → recover_pending",
                started_by=100,
                auto_disable=True,
            )

        assert result["success"] is False
        assert result["maintenance_kept_enabled"] is True

        recover_status = await _get_recover_status(real_store)
        assert recover_status == "pending", \
            f"backup 失败后 recover_status 应为 'pending',实际: {recover_status}"


class TestEnableResetsRecoverStatus:
    """R42 P1-12: enable() 重置 recover_status='completed'。"""

    @pytest.mark.asyncio
    async def test_enable_resets_recover_status_to_completed(
        self, real_store, reset_cache
    ):
        """enable() 开启新维护周期时重置 recover_status='completed'。"""
        from services import maintenance_mode

        # 先构造 recover_status='pending' 的场景
        ok = await maintenance_mode.enable("第一次开启", started_by=100)
        assert ok is True
        await _set_recover_status(real_store, "pending")
        # 验证构造成功
        assert await _get_recover_status(real_store) == "pending"

        # 再次 enable(新维护周期)→ recover_status 应被重置
        ok = await maintenance_mode.enable("第二次开启(重置)", started_by=100)
        assert ok is True

        recover_status = await _get_recover_status(real_store)
        assert recover_status == "completed", \
            f"enable 后 recover_status 应被重置为 'completed',实际: {recover_status}"


class TestDisableRejectsWhenRecoverPending:
    """R42 P1-12: disable() 在 recover_status='pending' 时拒绝关闭。"""

    @pytest.mark.asyncio
    async def test_disable_rejected_when_pending_without_approval(
        self, real_store, reset_cache
    ):
        """recover_status='pending' + 无 approval_action_id → 抛 MaintenancePreconditionError。"""
        from services import maintenance_mode

        # 开启维护模式 + 设置 recover_status='pending'
        await maintenance_mode.enable("测试 recover 拒绝", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 清理 enable 产生的 dirty_outbox,让常规前置检查能通过
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 调用 disable(默认 force=False, 无 approval_action_id)应抛异常
        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(ended_by=100)

        assert "recover_maintenance" in str(exc_info.value) or \
               "审批" in str(exc_info.value), \
            f"异常消息应提示需要 recover_maintenance 审批,实际: {exc_info.value}"

        # 验证:maintenance_state 仍为 enabled(未关闭)
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "recover_status=pending 且无审批时 disable 应拒绝"
        # recover_status 仍为 pending(未被 disable 改动)
        assert await _get_recover_status(real_store) == "pending"

    @pytest.mark.asyncio
    async def test_disable_allowed_when_pending_with_executed_approval(
        self, real_store, reset_cache
    ):
        """recover_status='pending' + 有效 approval_action_id(executed) → 允许关闭。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 recover 通过", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 清理 dirty_outbox,让前置检查通过
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 插入一条 command_executions 记录(status='approved')
        # R52 P0-5: 审批通过待执行的状态为 'approved'(非 'executed')
        approval_action_id = "recover_action_001"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        # 调用 disable(带 approval_action_id + request_hash)应成功
        # R51 P1-6: recover_status=pending 时必须同时提供 request_hash + approval_action_id + principal
        result = await maintenance_mode.disable(
            ended_by=100,
            approval_action_id=approval_action_id,
            request_hash=_TEST_REQUEST_HASH,
        )
        assert result is True, \
            "recover_status=pending + 有效 approval_action_id + request_hash 时 disable 应成功"

        # 验证:maintenance_state 已关闭
        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, "有效审批通过后 maintenance 应关闭"

    @pytest.mark.asyncio
    async def test_disable_rejected_when_approval_not_executed(
        self, real_store, reset_cache
    ):
        """recover_status='pending' + approval_action_id 状态非 executed → 拒绝。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 approval 未 executed", started_by=100)
        await _set_recover_status(real_store, "pending")
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 插入 status='executing' 的 command_executions(未 executed)
        approval_action_id = "recover_action_002"
        await _insert_command_execution(
            real_store, approval_action_id, status="executing",
        )

        # R51 P1-6: 必须传 request_hash,否则会在 approval 状态检查之前就抛 request_hash 异常
        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(
                ended_by=100,
                approval_action_id=approval_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        assert "not approved" in str(exc_info.value).lower() or \
               "executed" in str(exc_info.value).lower() or \
               "状态非" in str(exc_info.value), \
            f"异常消息应提示状态非 approved,实际: {exc_info.value}"

        # 未关闭
        assert await maintenance_mode.is_enabled() is True

    @pytest.mark.asyncio
    async def test_disable_rejected_when_approval_action_id_not_exists(
        self, real_store, reset_cache
    ):
        """recover_status='pending' + approval_action_id 不存在 → 拒绝。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 approval 不存在", started_by=100)
        await _set_recover_status(real_store, "pending")
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 不插入任何 command_executions 记录,直接使用不存在的 action_id
        nonexistent_action_id = "nonexistent_recover_action"

        # R51 P1-6: 必须传 request_hash,否则会在 approval 不存在检查之前就抛 request_hash 异常
        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(
                ended_by=100,
                approval_action_id=nonexistent_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        assert "不存在" in str(exc_info.value) or \
               "not exist" in str(exc_info.value).lower(), \
            f"异常消息应提示 approval_action_id 不存在,实际: {exc_info.value}"

        # 未关闭
        assert await maintenance_mode.is_enabled() is True


class TestRecoverMaintenanceValidation:
    """R42 P1-12: recover_maintenance 参数校验。"""

    @pytest.mark.asyncio
    async def test_recover_maintenance_without_approval_action_id_raises(
        self, real_store, reset_cache
    ):
        """recover_maintenance 未提供 approval_action_id → 抛 PermissionError。"""
        from services import maintenance_mode

        # 先开启维护模式(无需设置 recover_status,因为校验在 approval_action_id 检查之后)
        await maintenance_mode.enable("测试无 approval_action_id", started_by=100)

        with pytest.raises(PermissionError) as exc_info:
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="尝试无 approval 恢复",
                approval_action_id=None,
            )

        assert "approval_action_id" in str(exc_info.value), \
            f"异常应提到 approval_action_id,实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_recover_maintenance_approval_not_executed_raises(
        self, real_store, reset_cache
    ):
        """recover_maintenance approval_action_id 状态非 executed → 抛 PermissionError。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 approval 非 executed", started_by=100)

        # 插入 status='executing' 的记录
        approval_action_id = "recover_action_003"
        await _insert_command_execution(
            real_store, approval_action_id, status="executing",
        )

        # R51 P1-6: 必须传 request_hash,否则会在 approval 状态检查之前就抛 request_hash 异常
        with pytest.raises(PermissionError) as exc_info:
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="尝试未完成审批恢复",
                approval_action_id=approval_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        assert "not approved" in str(exc_info.value).lower() or \
               "executed" in str(exc_info.value).lower() or \
               "状态非" in str(exc_info.value), \
            f"异常应提示状态非 approved,实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_recover_maintenance_approval_not_exists_raises(
        self, real_store, reset_cache
    ):
        """recover_maintenance approval_action_id 不存在 → 抛 PermissionError。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 approval 不存在", started_by=100)

        # R51 P1-6: 必须传 request_hash,否则会在 approval 不存在检查之前就抛 request_hash 异常
        with pytest.raises(PermissionError) as exc_info:
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="尝试不存在的 approval 恢复",
                approval_action_id="nonexistent_action_id_xxx",
                request_hash=_TEST_REQUEST_HASH,
            )

        assert "不存在" in str(exc_info.value) or \
               "not exist" in str(exc_info.value).lower(), \
            f"异常应提示 approval 不存在,实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_recover_maintenance_without_permission_raises(
        self, real_store, reset_cache
    ):
        """recover_maintenance principal 无 maintenance:recover 权限 → 抛 PermissionError。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试无权限 recover", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 插入 status='approved' 的 approval(满足前置条件)
        # R52 P0-5: 审批通过待执行的状态为 'approved'(非 'executed')
        approval_action_id = "recover_action_004"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        # Mock check_permission 返回 False(无权限)
        with patch(
            "services.rbac.check_permission",
            new=AsyncMock(return_value=False),
        ):
            # R51 P1-6: 必须传 request_hash,否则会在 RBAC 权限校验之前就抛 request_hash 异常
            with pytest.raises(PermissionError) as exc_info:
                await maintenance_mode.recover_maintenance(
                    principal_id=100,
                    reason="无权限恢复",
                    approval_action_id=approval_action_id,
                    request_hash=_TEST_REQUEST_HASH,
                )

        assert "maintenance:recover" in str(exc_info.value) or \
               "权限" in str(exc_info.value), \
            f"异常应提到 maintenance:recover 权限,实际: {exc_info.value}"

        # 验证:maintenance_state 仍为 enabled(未关闭)
        assert await maintenance_mode.is_enabled() is True

        # 验证:写入了未授权尝试的 audit_log
        audit_rows = await real_store._db.execute_fetchall(
            "SELECT action FROM audit_log WHERE action = ?",
            ("recover_maintenance_unauthorized",),
        )
        assert audit_rows and len(audit_rows) > 0, \
            "无权限恢复时应写 audit_log(action=recover_maintenance_unauthorized)"


class TestRecoverMaintenanceSuccess:
    """R42 P1-12: recover_maintenance 成功路径。"""

    @pytest.mark.asyncio
    async def test_recover_maintenance_closes_maintenance(
        self, real_store, reset_cache
    ):
        """recover_maintenance 通过验证后调用 disable 关闭维护模式。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 recover 成功", started_by=100)
        await _set_recover_status(real_store, "pending")
        # 清理 dirty_outbox
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        approval_action_id = "recover_action_success_001"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        # Mock check_permission 返回 True
        with patch(
            "services.rbac.check_permission",
            new=AsyncMock(return_value=True),
        ):
            # R51 P1-6: 必须传 request_hash 绑定审批动作 + principal + 请求来源
            ok = await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="审批通过后恢复",
                approval_action_id=approval_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        assert ok is True, "通过验证后 recover_maintenance 应返回 True"

        # maintenance 应已关闭
        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, "recover_maintenance 成功后 maintenance 应关闭"

    @pytest.mark.asyncio
    async def test_recover_maintenance_writes_audit_log(
        self, real_store, reset_cache
    ):
        """recover_maintenance 成功后写 audit_log(action=recover_maintenance)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 audit_log", started_by=100)
        await _set_recover_status(real_store, "pending")
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        approval_action_id = "recover_action_audit_001"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        with patch(
            "services.rbac.check_permission",
            new=AsyncMock(return_value=True),
        ):
            # R51 P1-6: 必须传 request_hash 绑定审批动作 + principal + 请求来源
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="审批通过写审计",
                approval_action_id=approval_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        # 验证:audit_log 中有 action='recover_maintenance' 记录
        audit_rows = await real_store._db.execute_fetchall(
            "SELECT action, details FROM audit_log WHERE action = ?",
            ("recover_maintenance",),
        )
        assert audit_rows and len(audit_rows) > 0, \
            "recover_maintenance 成功后应写 audit_log(action=recover_maintenance)"

        # details 中应包含 approval_action_id
        details = audit_rows[0][1] or ""
        assert approval_action_id in details, \
            f"audit_log.details 应包含 approval_action_id,实际: {details}"

    @pytest.mark.asyncio
    async def test_recover_maintenance_resets_recover_status(
        self, real_store, reset_cache
    ):
        """recover_maintenance 成功后 recover_status='completed'(允许后续正常 disable)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 reset recover_status", started_by=100)
        await _set_recover_status(real_store, "pending")
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        approval_action_id = "recover_action_reset_001"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        with patch(
            "services.rbac.check_permission",
            new=AsyncMock(return_value=True),
        ):
            # R51 P1-6: 必须传 request_hash 绑定审批动作 + principal + 请求来源
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="恢复后重置状态",
                approval_action_id=approval_action_id,
                request_hash=_TEST_REQUEST_HASH,
            )

        # recover_status 应被重置为 completed
        recover_status = await _get_recover_status(real_store)
        assert recover_status == "completed", \
            f"recover_maintenance 成功后 recover_status 应为 'completed'," \
            f"实际: {recover_status}"

        # 验证:再次 enable + disable(无 approval_action_id)应正常工作
        # (即 recover_status 已重置,不再强制要求审批)
        await maintenance_mode.enable("新维护周期", started_by=100)
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()
        # 现在 disable 应不需要 approval_action_id
        ok = await maintenance_mode.disable(ended_by=100)
        assert ok is True, \
            "recover_status 重置为 completed 后,disable 应不需要 approval_action_id"


class TestRbacPermissionConstant:
    """R42 P1-12: RBAC 权限常量与默认角色映射。"""

    def test_permission_maintenance_recover_constant_exists(self):
        """rbac.PERMISSION_MAINTENANCE_RECOVER 常量存在且值正确。"""
        from services import rbac
        assert hasattr(rbac, "PERMISSION_MAINTENANCE_RECOVER"), \
            "rbac 应定义 PERMISSION_MAINTENANCE_RECOVER 常量"
        assert rbac.PERMISSION_MAINTENANCE_RECOVER == "maintenance:recover", \
            f"PERMISSION_MAINTENANCE_RECOVER 应为 'maintenance:recover'," \
            f"实际: {rbac.PERMISSION_MAINTENANCE_RECOVER!r}"

    def test_super_admin_and_ops_have_maintenance_recover_permission(self):
        """super_admin 与 ops 角色默认权限列表包含 maintenance:recover。"""
        from services import rbac
        # super_admin 显式包含(便于审计)
        super_admin_perms = rbac._DEFAULT_ROLE_PERMISSIONS[rbac.ROLE_SUPER_ADMIN]
        assert rbac.PERMISSION_MAINTENANCE_RECOVER in super_admin_perms, \
            "super_admin 应显式包含 maintenance:recover 权限(便于审计)"

        # ops 角色应包含
        ops_perms = rbac._DEFAULT_ROLE_PERMISSIONS[rbac.ROLE_OPS]
        assert rbac.PERMISSION_MAINTENANCE_RECOVER in ops_perms, \
            "ops 角色应包含 maintenance:recover 权限"

        # support / operator 角色不应包含(最小权限原则)
        support_perms = rbac._DEFAULT_ROLE_PERMISSIONS.get(rbac.ROLE_SUPPORT, [])
        assert rbac.PERMISSION_MAINTENANCE_RECOVER not in support_perms, \
            "support 角色不应包含 maintenance:recover 权限(最小权限原则)"

        operator_perms = rbac._DEFAULT_ROLE_PERMISSIONS.get(rbac.ROLE_OPERATOR, [])
        assert rbac.PERMISSION_MAINTENANCE_RECOVER not in operator_perms, \
            "operator 角色不应包含 maintenance:recover 权限(最小权限原则)"
