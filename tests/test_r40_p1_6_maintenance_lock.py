"""R40 P1-6: 维护模式工作流失败保持 enabled 测试。

被测目标:
- ``services.maintenance_mode.execute_maintenance_workflow()``
- ``services.maintenance_mode.disable(force=False)`` — 前置检查
- ``services.maintenance_mode.check_disable_preconditions()``
- ``services.maintenance_mode.rollback_maintenance()``

测试场景:
1. drain_queues 失败时保持 maintenance enabled(不调用 disable)
2. trigger_backup 失败时保持 maintenance enabled
3. 全部成功 + auto_disable=True 时调用 disable,关闭维护模式
4. disable() 前置检查不满足时抛 MaintenancePreconditionError
5. rollback_maintenance(force=True) 跳过前置检查强制关闭

修复说明:
- 原 workflow 任何步骤失败后仍调用 disable(),导致维护模式被关闭,
  系统在未就绪状态下接受新请求(数据不一致风险)。
- 修复后:失败时保持 enabled,记录失败原因到 audit_log,
  提供 rollback_maintenance() 人工恢复 API。
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_6_test_")
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
async def reset_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


# ════════════════════════════════════════════════════════════════
# P1-6 测试用例
# ════════════════════════════════════════════════════════════════

class TestMaintenanceWorkflowFailureKeepsEnabled:
    """R40 P1-6: workflow 失败时保持 maintenance enabled。"""

    @pytest.mark.asyncio
    async def test_drain_failure_keeps_enabled(self, real_store, reset_cache):
        """drain_queues 失败(超时未排空)时保持 maintenance enabled。"""
        from services import maintenance_mode

        # Mock drain_queues 返回 drained=False(超时)
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
                reason="测试 drain 失败",
                started_by=100,
                auto_disable=True,  # 即使 auto_disable=True,失败也不应 disable
            )

        # 验证:workflow 失败
        assert result["success"] is False, "drain 失败时 workflow 应失败"
        # 验证:保持 enabled
        assert result["maintenance_kept_enabled"] is True, \
            "drain 失败时应保持 maintenance enabled"
        # 验证:failure_reason 包含 drain_queues
        assert "drain" in result["failure_reason"].lower(), \
            f"failure_reason 应包含 drain,实际: {result['failure_reason']}"

        # 验证:maintenance_state 仍为 enabled
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "drain 失败后 maintenance_state 应仍为 enabled"

    @pytest.mark.asyncio
    async def test_backup_failure_keeps_enabled(self, real_store, reset_cache):
        """trigger_backup 失败时保持 maintenance enabled。"""
        from services import maintenance_mode

        # Mock drain_queues 成功
        # Mock trigger_backup 抛异常
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
                reason="测试 backup 失败",
                started_by=100,
                auto_disable=True,
            )

        assert result["success"] is False, "backup 失败时 workflow 应失败"
        assert result["maintenance_kept_enabled"] is True, \
            "backup 失败时应保持 maintenance enabled"
        assert "backup" in result["failure_reason"].lower() or \
               "trigger_backup" in result["failure_reason"].lower(), \
            f"failure_reason 应包含 backup,实际: {result['failure_reason']}"

        # 验证:maintenance_state 仍为 enabled
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "backup 失败后 maintenance_state 应仍为 enabled"

    @pytest.mark.asyncio
    async def test_verify_failure_keeps_enabled(self, real_store, reset_cache):
        """verify(就绪检查)失败时保持 maintenance enabled。"""
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
            new=AsyncMock(return_value="backup_test_001"),
        ), patch.object(
            maintenance_mode,
            "check_readiness",
            new=AsyncMock(return_value={
                "ready": False,
                "pending_uploads": 0,
                "pending_jobs": 3,  # 有未完成任务
                "unprocessed_outbox": 0,
                "active_replication": 0,
            }),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 verify 失败",
                started_by=100,
                auto_disable=True,
            )

        assert result["success"] is False, "verify 失败时 workflow 应失败"
        assert result["maintenance_kept_enabled"] is True, \
            "verify 失败时应保持 maintenance enabled"

        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "verify 失败后 maintenance_state 应仍为 enabled"


class TestMaintenanceWorkflowSuccessDisable:
    """R40 P1-6: workflow 全部成功 + auto_disable=True 时关闭维护模式。"""

    @pytest.mark.asyncio
    async def test_successful_workflow_with_auto_disable(self, real_store, reset_cache):
        """全部步骤成功 + auto_disable=True 时调用 disable 关闭维护模式。"""
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
            new=AsyncMock(return_value="backup_success_001"),
        ), patch.object(
            maintenance_mode,
            "check_readiness",
            new=AsyncMock(return_value={
                "ready": True,
                "pending_uploads": 0,
                "pending_jobs": 0,
                "unprocessed_outbox": 0,
                "active_replication": 0,
            }),
        ), patch.object(
            maintenance_mode,
            "check_disable_preconditions",
            new=AsyncMock(return_value={
                "ok": True,
                "reason": "",
                "dirty_outbox_remaining": 0,
                "jobs_remaining": 0,
                "backup_count": 1,
            }),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试成功流程",
                started_by=100,
                auto_disable=True,
            )

        assert result["success"] is True, "全部成功时 workflow 应成功"
        assert result["maintenance_kept_enabled"] is False, \
            "auto_disable=True 且成功时应关闭 maintenance(不保持 enabled)"

        # 验证:maintenance_state 已关闭
        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, "成功流程后 maintenance_state 应为 disabled"

    @pytest.mark.asyncio
    async def test_successful_workflow_without_auto_disable_keeps_enabled(
        self, real_store, reset_cache
    ):
        """全部步骤成功 + auto_disable=False(默认)时保持 enabled,等待人工确认。"""
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
            new=AsyncMock(return_value="backup_success_002"),
        ), patch.object(
            maintenance_mode,
            "check_readiness",
            new=AsyncMock(return_value={
                "ready": True,
                "pending_uploads": 0,
                "pending_jobs": 0,
                "unprocessed_outbox": 0,
                "active_replication": 0,
            }),
        ):
            # auto_disable 默认为 False
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试默认 auto_disable=False",
                started_by=100,
            )

        assert result["success"] is True, "全部成功时 workflow 应成功"
        # 默认 auto_disable=False,即使成功也保持 enabled
        assert result["maintenance_kept_enabled"] is True, \
            "auto_disable=False 时应保持 enabled 等待人工确认"

        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "默认 auto_disable=False 时应保持 enabled"


class TestMaintenanceDisablePreconditions:
    """R40 P1-6: disable() 前置检查测试。"""

    @pytest.mark.asyncio
    async def test_disable_rejected_when_dirty_outbox_not_empty(self, real_store, reset_cache):
        """dirty_outbox 未排空时 disable 抛 MaintenancePreconditionError。"""
        from services import maintenance_mode

        # 先开启维护模式
        ok = await maintenance_mode.enable("测试前置检查", started_by=100)
        assert ok is True

        # 插入未处理的 dirty_outbox 记录
        await real_store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, processed, created_at) "
            "VALUES (?, ?, 0, ?)",
            ("test_table", "test_pk_1", "2026-01-01T00:00:00"),
        )
        await real_store._db.commit()

        # 调用 disable(默认 force=False)应抛异常
        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(ended_by=100)

        assert "dirty_outbox" in str(exc_info.value), \
            f"异常消息应包含 dirty_outbox,实际: {exc_info.value}"

        # 验证:maintenance_state 仍为 enabled
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "前置检查失败后应保持 enabled"

    @pytest.mark.asyncio
    async def test_disable_rejected_when_jobs_pending(self, real_store, reset_cache):
        """local_job_queue 有 pending 任务时 disable 抛 MaintenancePreconditionError。"""
        from services import maintenance_mode

        ok = await maintenance_mode.enable("测试 jobs 前置检查", started_by=100)
        assert ok is True

        # 清理 enable 产生的 dirty_outbox(隔离 jobs_pending 场景)
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 插入 pending job(必填字段:code / target_user_id / storage_channel_id)
        await real_store._db.execute(
            "INSERT INTO local_job_queue (code, target_user_id, storage_channel_id, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            ("TESTCODE", 999, -100, "2026-01-01T00:00:00"),
        )
        await real_store._db.commit()

        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(ended_by=100)

        assert "job" in str(exc_info.value).lower() or "queue" in str(exc_info.value).lower(), \
            f"异常消息应包含 job/queue,实际: {exc_info.value}"

        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "前置检查失败后应保持 enabled"

    @pytest.mark.asyncio
    async def test_disable_succeeds_when_preconditions_met(self, real_store, reset_cache):
        """前置条件满足时(队列空 + 无 pending job)disable 成功。"""
        from services import maintenance_mode

        ok = await maintenance_mode.enable("测试正常 disable", started_by=100)
        assert ok is True

        # 清理 enable 产生的 dirty_outbox(模拟 drain_queues 已排空)
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 无 dirty_outbox,无 pending job → 前置检查通过
        result = await maintenance_mode.disable(ended_by=100)
        assert result is True, "前置条件满足时 disable 应成功"

        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, "disable 成功后应为 disabled"


class TestMaintenanceRollback:
    """R40 P1-6: rollback_maintenance() 强制关闭测试。"""

    @pytest.mark.asyncio
    async def test_rollback_forces_disable_skipping_preconditions(self, real_store, reset_cache):
        """rollback_maintenance(force=True) 跳过前置检查强制关闭。"""
        from services import maintenance_mode

        ok = await maintenance_mode.enable("测试 rollback", started_by=100)
        assert ok is True

        # 插入未处理的 dirty_outbox(正常 disable 会被拒绝)
        await real_store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, processed, created_at) "
            "VALUES (?, ?, 0, ?)",
            ("test_table", "rollback_pk", "2026-01-01T00:00:00"),
        )
        await real_store._db.commit()

        # 验证正常 disable 会被拒绝
        with pytest.raises(maintenance_mode.MaintenancePreconditionError):
            await maintenance_mode.disable(ended_by=100)

        # rollback_maintenance 应跳过前置检查强制关闭
        result = await maintenance_mode.rollback_maintenance(
            reason="运维确认系统状态后强制恢复", ended_by=100,
        )
        assert result is True, "rollback_maintenance 应强制关闭成功"

        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, "rollback 后 maintenance_state 应为 disabled"

    @pytest.mark.asyncio
    async def test_check_disable_preconditions_returns_dict(self, real_store, reset_cache):
        """check_disable_preconditions 返回结构化字典。"""
        from services import maintenance_mode

        result = await maintenance_mode.check_disable_preconditions()

        assert isinstance(result, dict)
        assert "ok" in result
        assert "reason" in result
        assert "dirty_outbox_remaining" in result
        assert "jobs_remaining" in result
        assert "backup_count" in result

    @pytest.mark.asyncio
    async def test_workflow_failure_records_audit_log(self, real_store, reset_cache):
        """workflow 失败时写入 audit_log(maintenance_workflow_failed)。"""
        from services import maintenance_mode

        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 1,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ):
            await maintenance_mode.execute_maintenance_workflow(
                reason="测试 audit_log 记录",
                started_by=200,
                auto_disable=True,
            )

        # 验证 audit_log 中有 maintenance_workflow_failed 记录
        rows = await real_store._db.execute_fetchall(
            "SELECT action FROM audit_log WHERE action = 'maintenance_workflow_failed'"
        )
        assert rows and len(rows) > 0, \
            "workflow 失败应写入 maintenance_workflow_failed audit_log 记录"
