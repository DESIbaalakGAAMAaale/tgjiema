"""R41 P0-5: CommandBus 幂等缓存持久化 — SQLite CAS 测试。

被测目标:
- ``services.command_bus._try_insert_or_get_cached`` — CAS INSERT + UNIQUE 冲突查询
- ``services.command_bus.claim_execution`` — CAS 认领(pending → executing)
- ``services.command_bus.renew_lease`` — 续租
- ``services.command_bus.release_execution`` — 释放(回退到 pending)
- ``services.command_bus.cleanup_stale_leases`` — 清理过期租约
- ``services.command_bus.CommandBus.execute`` — 端到端幂等
- ``services.command_bus._compute_action_id`` — SHA256 确定性 action_id
- ``services.command_bus._compute_request_hash`` — SHA256(payload) 防篡改

测试场景:
1. 重启模拟:执行 action → 清空内存 → 再次执行同一 action → 返回缓存,不重复执行
2. 多 worker 模拟:两个并发 execute(action_id 相同)→ 只有一个成功
3. Lease 过期:claim_execution 后 lease_until 过期 → cleanup → 可重新认领
4. request_hash 校验:相同 action_id 不同 payload → 拒绝执行
5. 状态机:pending → executing → executed/failed

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- Mock RBAC 返回 True(跳过权限检查)
- 使用 asyncio.gather 模拟并发
"""
import asyncio
import inspect
import json
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


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 _cs_module._store 为测试实例,
    使 get_cache_store() 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r41_p0_5_test_")
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
async def clean_executions(real_store):
    """每个用例前清空 command_executions 表,确保隔离。"""
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.commit()
    yield real_store
    # 用例后也清空(防止残留影响下一用例)
    await real_store._db.execute("DELETE FROM command_executions")
    await real_store._db.commit()


@pytest.fixture
def mock_rbac_permission():
    """Mock rbac.check_permission 返回 True。"""
    with patch("services.command_bus.CommandBus._get_rbac",
               return_value=MagicMock(check_permission=AsyncMock(return_value=True))):
        yield


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _make_command(action: str = "test_action", params: dict = None, requires_approval: bool = False):
    """构造测试用 Command 对象。"""
    from services.command_bus import Command, PERM_USERS_UNBAN
    call_count = {"n": 0}

    async def _handler(p):
        call_count["n"] += 1
        return {"ok": True, "params": p}

    cmd = Command(
        action=action,
        required_permission=PERM_USERS_UNBAN,
        handler=_handler,
        params=params or {"user_id": 12345},
        requires_approval=requires_approval,
    )
    return cmd, call_count


# ════════════════════════════════════════════════════════════════
# 1. 重启模拟测试
# ════════════════════════════════════════════════════════════════

class TestRestartSimulation:
    """R41 P0-5: 重启后 action_id 仍可见,不重复执行。"""

    @pytest.mark.asyncio
    async def test_restart_returns_cached_result(self, clean_executions, mock_rbac_permission):
        """执行 action → 模拟重启(新 CommandBus 实例)→ 再次执行 → 返回缓存,handler 不重复。"""
        from services.command_bus import CommandBus, AdminPrincipal

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        fixed_action_id = "restart_test_action_001"

        # 第一次执行
        cmd1, call_count1 = _make_command(params={"user_id": 999})
        bus1 = CommandBus()
        result1 = await bus1.execute(cmd1, principal, action_id=fixed_action_id)
        assert result1.success is True
        assert result1.data == {"ok": True, "params": {"user_id": 999}}
        assert call_count1["n"] == 1

        # 模拟重启:创建新的 CommandBus 实例(进程内状态丢失)
        # SQLite 持久化的 command_executions 表仍在
        cmd2, call_count2 = _make_command(params={"user_id": 999})
        assert call_count2 is not call_count1  # 不同的 handler 计数器
        bus2 = CommandBus()
        result2 = await bus2.execute(cmd2, principal, action_id=fixed_action_id)

        # 验证:返回缓存结果,handler 未执行
        assert result2.success is True
        assert result2.action_id == fixed_action_id
        assert call_count2["n"] == 0, "重启后再次执行同一 action_id,handler 不应被调用"

    @pytest.mark.asyncio
    async def test_deterministic_action_id_auto_idempotency(self, clean_executions, mock_rbac_permission):
        """同一 principal + 同一命令(相同参数)→ 自动幂等(action_id 由 SHA256 确定)。"""
        from services.command_bus import CommandBus, AdminPrincipal, _compute_action_id

        principal = AdminPrincipal(id=42, name="admin", source="web")

        # 不传 action_id,由 _compute_action_id 自动计算
        cmd1, call_count1 = _make_command(params={"user_id": 777})
        bus = CommandBus()
        result1 = await bus.execute(cmd1, principal)
        assert result1.success is True
        assert call_count1["n"] == 1

        # 第二次执行(不传 action_id)→ SHA256 相同 → 幂等命中
        cmd2, call_count2 = _make_command(params={"user_id": 777})
        result2 = await bus.execute(cmd2, principal)

        assert result2.success is True
        assert result2.action_id == result1.action_id  # 相同的确定性 action_id
        assert call_count2["n"] == 0, "相同参数自动幂等,handler 不应被调用"


# ════════════════════════════════════════════════════════════════
# 2. 多 worker 并发模拟
# ════════════════════════════════════════════════════════════════

class TestMultiWorkerConcurrency:
    """R41 P0-5: 两个并发 execute(action_id 相同)→ 只有一个成功执行。"""

    @pytest.mark.asyncio
    async def test_concurrent_execute_only_one_succeeds(self, clean_executions, mock_rbac_permission):
        """并发场景:两个 execute() 同时调用,只有一个执行 handler。"""
        from services.command_bus import CommandBus, AdminPrincipal

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        fixed_action_id = "concurrent_test_001"

        call_count = {"n": 0}

        async def _slow_handler(params):
            call_count["n"] += 1
            await asyncio.sleep(0.05)  # 模拟耗时操作
            return {"ok": True}

        from services.command_bus import Command, PERM_USERS_UNBAN
        command = Command(
            action="test_concurrent",
            required_permission=PERM_USERS_UNBAN,
            handler=_slow_handler,
            params={"user_id": 555},
            requires_approval=False,
        )

        bus = CommandBus()
        results = await asyncio.gather(
            bus.execute(command, principal, action_id=fixed_action_id),
            bus.execute(command, principal, action_id=fixed_action_id),
        )

        # 验证:只有一个成功执行 handler
        successes = [r for r in results if r.success and r.data == {"ok": True}]
        failures = [r for r in results if not r.success or r.data != {"ok": True}]
        assert len(successes) == 1, f"应只有一个成功执行,实际 {len(successes)}"
        assert len(failures) == 1, f"应有一个返回已存在,实际 {len(failures)}"
        assert call_count["n"] == 1, "handler 应只被调用一次"

        # 失败的那个应包含"已存在"
        failed_result = failures[0]
        assert "已存在" in failed_result.error or "抢占" in failed_result.error, \
            f"失败结果应包含'已存在'或'抢占',实际: {failed_result.error}"


# ════════════════════════════════════════════════════════════════
# 3. Lease 过期测试
# ════════════════════════════════════════════════════════════════

class TestLeaseExpiry:
    """R41 P0-5: 过期 lease 被 cleanup 后可重新认领。"""

    @pytest.mark.asyncio
    async def test_expired_lease_cleaned_and_reclaimed(self, clean_executions):
        """claim_execution 后 lease 过期 → cleanup → status 回到 pending → 可重新认领。"""
        from services.command_bus import (
            claim_execution, release_execution, cleanup_stale_leases,
            CMD_STATUS_PENDING, CMD_STATUS_EXECUTING,
        )
        from services.command_bus import _try_insert_or_get_cached, _compute_request_hash

        action_id = "lease_test_001"
        # 1. INSERT pending
        request_hash = _compute_request_hash({"x": 1})
        cached = await _try_insert_or_get_cached(action_id, "test_cmd", 1, request_hash)
        assert cached is None  # 新记录

        # 2. claim with 1-second lease
        claimed = await claim_execution(action_id, "worker_A", lease_seconds=1)
        assert claimed is True

        # 3. 等待 lease 过期
        await asyncio.sleep(1.5)

        # 4. cleanup stale leases
        cleaned = await cleanup_stale_leases()
        assert cleaned >= 1, "应清理至少 1 个过期租约"

        # 5. 验证状态已回退到 pending
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] == CMD_STATUS_PENDING, f"过期 lease 应回退到 pending,实际: {rows[0][0]}"

        # 6. 可重新认领
        reclaimed = await claim_execution(action_id, "worker_B", lease_seconds=60)
        assert reclaimed is True, "过期 lease 清理后应可重新认领"

    @pytest.mark.asyncio
    async def test_active_lease_not_cleaned(self, clean_executions):
        """未过期的 lease 不被 cleanup。"""
        from services.command_bus import (
            claim_execution, cleanup_stale_leases,
            CMD_STATUS_EXECUTING,
        )
        from services.command_bus import _try_insert_or_get_cached, _compute_request_hash

        action_id = "lease_active_001"
        request_hash = _compute_request_hash({"y": 2})
        await _try_insert_or_get_cached(action_id, "test_cmd", 1, request_hash)

        # claim with 60-second lease(不会过期)
        claimed = await claim_execution(action_id, "worker_C", lease_seconds=60)
        assert claimed is True

        # cleanup 应不清理(lease 未过期)
        cleaned = await cleanup_stale_leases()
        assert cleaned == 0, "未过期的 lease 不应被清理"

        # 验证状态仍为 executing
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert rows[0][0] == CMD_STATUS_EXECUTING


# ════════════════════════════════════════════════════════════════
# 4. request_hash 防篡改测试
# ════════════════════════════════════════════════════════════════

class TestRequestHashValidation:
    """R41 P0-5: 相同 action_id 不同 payload → 拒绝执行(防篡改)。"""

    @pytest.mark.asyncio
    async def test_different_payload_rejected(self, clean_executions, mock_rbac_permission):
        """相同 action_id 但不同 payload → 返回防篡改错误。"""
        from services.command_bus import CommandBus, AdminPrincipal, Command, PERM_USERS_UNBAN

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        fixed_action_id = "tamper_test_001"

        # 第一次执行(payload A)
        call_count_a = {"n": 0}

        async def _handler_a(params):
            call_count_a["n"] += 1
            return {"ok": True}

        cmd_a = Command(
            action="tamper_test",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler_a,
            params={"user_id": 100, "reason": "original"},
            requires_approval=False,
        )
        bus = CommandBus()
        result_a = await bus.execute(cmd_a, principal, action_id=fixed_action_id)
        assert result_a.success is True
        assert call_count_a["n"] == 1

        # 第二次执行(相同 action_id 但 payload B — 篡改)
        call_count_b = {"n": 0}

        async def _handler_b(params):
            call_count_b["n"] += 1
            return {"ok": True}

        cmd_b = Command(
            action="tamper_test",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler_b,
            params={"user_id": 100, "reason": "TAMPERED"},  # 不同 payload
            requires_approval=False,
        )
        result_b = await bus.execute(cmd_b, principal, action_id=fixed_action_id)

        # 验证:被拒绝(防篡改)
        assert result_b.success is False
        assert "防篡改" in result_b.error or "不一致" in result_b.error, \
            f"应返回防篡改错误,实际: {result_b.error}"
        assert call_count_b["n"] == 0, "篡改的请求 handler 不应被执行"

    @pytest.mark.asyncio
    async def test_same_payload_returns_cached(self, clean_executions, mock_rbac_permission):
        """相同 action_id 且相同 payload → 返回缓存结果(正常幂等)。"""
        from services.command_bus import CommandBus, AdminPrincipal, Command, PERM_USERS_UNBAN

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        fixed_action_id = "idem_test_002"
        same_params = {"user_id": 200, "reason": "same"}

        call_count = {"n": 0}

        async def _handler(params):
            call_count["n"] += 1
            return {"ok": True, "data": "result"}

        cmd = Command(
            action="idem_test",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params=same_params,
            requires_approval=False,
        )
        bus = CommandBus()
        result1 = await bus.execute(cmd, principal, action_id=fixed_action_id)
        result2 = await bus.execute(cmd, principal, action_id=fixed_action_id)

        assert result1.success is True
        assert result2.success is True
        assert result2.data == result1.data  # 返回缓存结果
        assert call_count["n"] == 1, "handler 只被调用一次"


# ════════════════════════════════════════════════════════════════
# 5. 状态机测试
# ════════════════════════════════════════════════════════════════

class TestStateMachine:
    """R41 P0-5: 状态机 pending → executing → executed/failed。"""

    @pytest.mark.asyncio
    async def test_full_success_flow(self, clean_executions, mock_rbac_permission):
        """完整成功流程:pending → executing → executed。"""
        from services.command_bus import CommandBus, AdminPrincipal, Command, PERM_USERS_UNBAN
        from services.command_bus import CMD_STATUS_EXECUTED

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        action_id = "state_success_001"

        async def _handler(params):
            return {"ok": True}

        cmd = Command(
            action="state_success",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 300},
            requires_approval=False,
        )
        bus = CommandBus()
        result = await bus.execute(cmd, principal, action_id=action_id)

        assert result.success is True

        # 验证 DB 中的最终状态
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status, result FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert len(rows) == 1
        status, result_json = rows[0]
        assert status == CMD_STATUS_EXECUTED, f"最终状态应为 executed,实际: {status}"
        # result JSON 应包含 success=True
        stored = json.loads(result_json)
        assert stored["success"] is True

    @pytest.mark.asyncio
    async def test_full_failure_flow(self, clean_executions, mock_rbac_permission):
        """完整失败流程:pending → executing → failed。"""
        from services.command_bus import CommandBus, AdminPrincipal, Command, PERM_USERS_UNBAN
        from services.command_bus import CMD_STATUS_FAILED

        principal = AdminPrincipal(id=1, name="admin", source="bot")
        action_id = "state_fail_001"

        async def _failing_handler(params):
            raise RuntimeError("handler 业务异常")

        cmd = Command(
            action="state_fail",
            required_permission=PERM_USERS_UNBAN,
            handler=_failing_handler,
            params={"user_id": 400},
            requires_approval=False,
        )
        bus = CommandBus()
        result = await bus.execute(cmd, principal, action_id=action_id)

        assert result.success is False
        assert "执行失败" in result.error

        # 验证 DB 中的最终状态
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status, result FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert len(rows) == 1
        status, result_json = rows[0]
        assert status == CMD_STATUS_FAILED, f"失败后状态应为 failed,实际: {status}"
        stored = json.loads(result_json)
        assert stored["success"] is False
        assert "业务异常" in stored["error"]

    @pytest.mark.asyncio
    async def test_manual_claim_release_cycle(self, clean_executions):
        """手动 claim → release → 重新 claim 的完整周期。"""
        from services.command_bus import (
            claim_execution, release_execution, cleanup_stale_leases,
            CMD_STATUS_PENDING, CMD_STATUS_EXECUTING,
        )
        from services.command_bus import _try_insert_or_get_cached, _compute_request_hash

        action_id = "manual_cycle_001"
        request_hash = _compute_request_hash({"z": 3})

        # INSERT pending
        cached = await _try_insert_or_get_cached(action_id, "manual_cmd", 1, request_hash)
        assert cached is None

        # claim → executing
        assert await claim_execution(action_id, "worker_X", lease_seconds=60) is True
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status, owner FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert rows[0][0] == CMD_STATUS_EXECUTING
        assert rows[0][1] == "worker_X"

        # release → pending
        assert await release_execution(action_id) is True
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status, owner FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert rows[0][0] == CMD_STATUS_PENDING
        assert rows[0][1] is None

        # 重新 claim → executing(不同 worker)
        assert await claim_execution(action_id, "worker_Y", lease_seconds=60) is True
        rows = await clean_executions._db.execute_fetchall(
            "SELECT status, owner FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        assert rows[0][0] == CMD_STATUS_EXECUTING
        assert rows[0][1] == "worker_Y"

    @pytest.mark.asyncio
    async def test_renew_lease_extends_expiry(self, clean_executions):
        """renew_lease 延长 lease_until。"""
        from services.command_bus import claim_execution, renew_lease
        from services.command_bus import _try_insert_or_get_cached, _compute_request_hash

        action_id = "renew_test_001"
        request_hash = _compute_request_hash({"r": 1})
        await _try_insert_or_get_cached(action_id, "renew_cmd", 1, request_hash)

        # claim with 1s lease
        assert await claim_execution(action_id, "worker_R", lease_seconds=1) is True

        # 获取当前 lease_until
        rows = await clean_executions._db.execute_fetchall(
            "SELECT lease_until FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        original_lease = rows[0][0]

        # renew with 60s lease
        await asyncio.sleep(0.1)  # 确保时间有变化
        assert await renew_lease(action_id, lease_seconds=60) is True

        rows = await clean_executions._db.execute_fetchall(
            "SELECT lease_until FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        renewed_lease = rows[0][0]
        assert renewed_lease > original_lease, "续租后 lease_until 应延后"


# ════════════════════════════════════════════════════════════════
# 6. 辅助函数单元测试
# ════════════════════════════════════════════════════════════════

class TestHelperFunctions:
    """R41 P0-5: 辅助函数单元测试。"""

    def test_compute_action_id_deterministic(self):
        """相同输入 → 相同 action_id(SHA256 确定性)。"""
        from services.command_bus import _compute_action_id
        aid1 = _compute_action_id("takedown", {"user": 1}, 100)
        aid2 = _compute_action_id("takedown", {"user": 1}, 100)
        assert aid1 == aid2
        assert len(aid1) == 64  # SHA256 hex 长度

    def test_compute_action_id_different_payload(self):
        """不同 payload → 不同 action_id。"""
        from services.command_bus import _compute_action_id
        aid1 = _compute_action_id("takedown", {"user": 1}, 100)
        aid2 = _compute_action_id("takedown", {"user": 2}, 100)
        assert aid1 != aid2

    def test_compute_action_id_different_principal(self):
        """不同 principal → 不同 action_id。"""
        from services.command_bus import _compute_action_id
        aid1 = _compute_action_id("takedown", {"user": 1}, 100)
        aid2 = _compute_action_id("takedown", {"user": 1}, 200)
        assert aid1 != aid2

    def test_compute_request_hash_deterministic(self):
        """相同 payload → 相同 request_hash。"""
        from services.command_bus import _compute_request_hash
        h1 = _compute_request_hash({"a": 1, "b": 2})
        h2 = _compute_request_hash({"b": 2, "a": 1})  # key 顺序不同
        assert h1 == h2  # sort_keys=True 确保顺序无关

    def test_compute_request_hash_different_payload(self):
        """不同 payload → 不同 request_hash。"""
        from services.command_bus import _compute_request_hash
        h1 = _compute_request_hash({"a": 1})
        h2 = _compute_request_hash({"a": 2})
        assert h1 != h2

    def test_get_worker_owner_format(self):
        """worker owner 格式为 hostname:pid。"""
        from services.command_bus import _get_worker_owner
        import os
        import socket
        owner = _get_worker_owner()
        assert ":" in owner
        assert str(os.getpid()) in owner
        assert socket.gethostname() in owner
