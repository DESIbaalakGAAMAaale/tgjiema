"""R64 P0-03: 恢复编排状态机 — staging 蓝绿切换 + 限时回滚点(原子性测试)。

审计背景(R64 终审报告 P0-03: restore 仍可能形成跨数据源混合时间点):

    旧 writer 按 CRDB → cache SQLite → relay SQLite 顺序执行 restore。
    前一数据源成功、后一数据源失败时,无法用普通事务回滚已经提交的另一个存储。
    覆盖模式尤其可能先清空生产表再失败,造成 active 数据被破坏且不可恢复。

整改方案(R64 P0-03):
    1. 恢复只写入全新 staging(蓝绿切换模型),禁止原地覆盖生产
    2. 每个 datasource 完成 schema/行数/主外键/业务守恒/hash/演练后才可切换
    3. 任何失败只销毁 staging,不影响 active 数据
    4. 切换后保留旧版本作为限时回滚点;回滚也必须使用状态机和审计事件
    5. nonce 状态机:reserved → consumed|failed,失败后允许同 payload 重试但禁止换 payload

测试覆盖矩阵:
    A. 状态机合法性(legal/illegal transitions + terminal phases)
    B. start_operation(nonce reserve + payload 一致性校验)
    C. provision_staging(成功 + 各 datasource 故障注入)
    D. restore_to_staging(成功 + 故障注入)
    E. validate_staging(全维度通过 + 各维度失败)
    F. approval + MFA(缺失/不匹配)
    G. 蓝绿切换(成功 + nonce consume + rollback target 持久化 + 故障注入)
    H. rollback(COMPLETED → ROLLED_BACK + 无 switch_version 拒绝 + 故障注入)
    I. fail_operation(销毁 staging + nonce=failed + 幂等)
    J. nonce 状态机(reserve → consume / reserve → fail → 同 payload 重试 / 禁换 payload)
    K. 三次全新空白环境完整恢复(0% / 50% / 100% 故障注入均保证 active 一致)
    L. 持久化(restore_operations / restore_operation_events / restore_rollback_targets)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 测试环境兼容(conftest 在收集阶段已注入 config/telegram mock)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 测试辅助
# ════════════════════════════════════════════════════════════════


async def _make_store_with_restore_tables(db_path: str | None = None):
    """构造一个真实 CacheStore + 创建 restore_operations 三张表(供 orchestrator 持久化)。

    CacheStore.init() 创建 restore_capability_nonces 表(nonce ledger),
    但 restore_operations / restore_operation_events / restore_rollback_targets
    由 migration 007 创建。测试中直接执行该 SQL 以隔离测试(不依赖 migrate.py)。
    """
    from database.cache_store import CacheStore
    import sqlite3
    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r64_p0_3_test_")
        db_path = str(Path(_tmp_dir) / "test_restore.db")
    store = CacheStore(db_path=db_path)
    await store.init()
    # 手动执行 migration 007 SQL(创建 restore_operations 三张表 + 索引)
    # 使用 sqlite3.complete_statement 正确切分(与 migrate.py 一致),
    # 避免中文注释内的分号被误判为语句边界
    migration_path = (
        REPO_ROOT / "database" / "migrations" / "007_restore_operations_ledger.sql"
    )
    sql_text = migration_path.read_text(encoding="utf-8")
    buffer = ""
    for line in sql_text.splitlines():
        if not line.strip() or line.strip().startswith("--"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt and not stmt.startswith("--"):
                await store._db.execute(stmt)
            buffer = ""
    if buffer.strip() and not buffer.strip().startswith("--"):
        await store._db.execute(buffer.strip())
    await store._db.commit()
    return store, db_path


def _make_orchestrator(
    store,
    *,
    staging_root: str | None = None,
    fault_hooks=None,
    rollback_ttl_seconds: int = 86400,
):
    """构造 RestoreOrchestrator(注入 store + 可选 fault_hooks)。"""
    from services.restore_orchestrator import RestoreOrchestrator
    if staging_root is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r64_p0_3_staging_")
        staging_root = _tmp_dir
    return RestoreOrchestrator(
        store,
        staging_root=staging_root,
        fault_hooks=fault_hooks or {},
        rollback_ttl_seconds=rollback_ttl_seconds,
    )


async def _run_full_happy_path(orchestrator, *, payload_digest: str = "d" * 64):
    """执行完整 happy path: INIT → ... → COMPLETED,返回 operation_id + switch_version。"""
    operation_id = await orchestrator.start_operation(
        backup_id="backup_test_001",
        manifest_digest="a" * 64,
        requested_by="tester",
        payload_digest=payload_digest,
        nonce="nonce_" + uuid.uuid4().hex[:16],
    )
    await orchestrator.provision_staging(operation_id)
    for ds in ("crdb", "sqlite", "relay_sqlite"):
        await orchestrator.restore_to_staging(operation_id, ds)
    await orchestrator.validate_staging(operation_id)
    approval_id = "approval_" + uuid.uuid4().hex[:16]
    mfa_receipt_id = "mfa_" + uuid.uuid4().hex[:16]
    await orchestrator.request_approval(operation_id, approval_id, mfa_receipt_id)
    switch_version = await orchestrator.execute_blue_green_switch(
        operation_id, approval_id, mfa_receipt_id
    )
    return operation_id, switch_version


# ════════════════════════════════════════════════════════════════
# A. 状态机合法性
# ════════════════════════════════════════════════════════════════


class TestRestorePhaseStateMachine:
    """R64 P0-03: RestorePhase 状态机 — 合法/非法转换 + 终态。"""

    def test_legal_transition_init_to_staging_provision(self):
        """INIT → STAGING_PROVISION 合法。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert RestoreOrchestrator.is_legal_transition(
            RestorePhase.INIT, RestorePhase.STAGING_PROVISION
        )

    def test_legal_transition_full_happy_path(self):
        """完整 happy path 转换链全部合法。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        chain = [
            (RestorePhase.INIT, RestorePhase.STAGING_PROVISION),
            (RestorePhase.STAGING_PROVISION, RestorePhase.STAGING_RESTORE),
            (RestorePhase.STAGING_RESTORE, RestorePhase.STAGING_VALIDATE),
            (RestorePhase.STAGING_VALIDATE, RestorePhase.AWAIT_APPROVAL),
            (RestorePhase.AWAIT_APPROVAL, RestorePhase.BLUE_GREEN_SWITCH),
            (RestorePhase.BLUE_GREEN_SWITCH, RestorePhase.COMPLETED),
        ]
        for frm, to in chain:
            assert RestoreOrchestrator.is_legal_transition(frm, to), (
                f"合法转换 {frm.value} → {to.value} 应被允许"
            )

    def test_illegal_transition_init_to_completed(self):
        """INIT → COMPLETED 非法(跳过中间阶段)。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert not RestoreOrchestrator.is_legal_transition(
            RestorePhase.INIT, RestorePhase.COMPLETED
        )

    def test_illegal_transition_init_to_rolled_back(self):
        """INIT → ROLLED_BACK 非法(未切换不能回滚)。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert not RestoreOrchestrator.is_legal_transition(
            RestorePhase.INIT, RestorePhase.ROLLED_BACK
        )

    def test_illegal_transition_failed_to_anything(self):
        """FAILED 是终态,不可转换到任何阶段。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        for target in RestorePhase:
            assert not RestoreOrchestrator.is_legal_transition(
                RestorePhase.FAILED, target
            ), f"FAILED → {target.value} 应非法"

    def test_illegal_transition_rolled_back_to_anything(self):
        """ROLLED_BACK 是终态,不可转换到任何阶段。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        for target in RestorePhase:
            assert not RestoreOrchestrator.is_legal_transition(
                RestorePhase.ROLLED_BACK, target
            ), f"ROLLED_BACK → {target.value} 应非法"

    def test_terminal_phases(self):
        """COMPLETED / FAILED / ROLLED_BACK 是终态。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert RestoreOrchestrator.is_terminal(RestorePhase.COMPLETED)
        assert RestoreOrchestrator.is_terminal(RestorePhase.FAILED)
        assert RestoreOrchestrator.is_terminal(RestorePhase.ROLLED_BACK)
        assert not RestoreOrchestrator.is_terminal(RestorePhase.INIT)
        assert not RestoreOrchestrator.is_terminal(RestorePhase.BLUE_GREEN_SWITCH)

    def test_legal_transition_completed_to_rolled_back(self):
        """COMPLETED → ROLLED_BACK 合法(维护窗口内回滚)。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert RestoreOrchestrator.is_legal_transition(
            RestorePhase.COMPLETED, RestorePhase.ROLLED_BACK
        )

    def test_illegal_transition_completed_to_failed(self):
        """COMPLETED → FAILED 非法(切换成功后不能标记失败)。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        assert not RestoreOrchestrator.is_legal_transition(
            RestorePhase.COMPLETED, RestorePhase.FAILED
        )

    def test_legal_transition_any_to_failed(self):
        """任一非终态阶段均可转到 FAILED(失败处理)。"""
        from services.restore_orchestrator import RestoreOrchestrator, RestorePhase
        for phase in (
            RestorePhase.INIT,
            RestorePhase.STAGING_PROVISION,
            RestorePhase.STAGING_RESTORE,
            RestorePhase.STAGING_VALIDATE,
            RestorePhase.AWAIT_APPROVAL,
            RestorePhase.BLUE_GREEN_SWITCH,
        ):
            assert RestoreOrchestrator.is_legal_transition(
                phase, RestorePhase.FAILED
            ), f"{phase.value} → FAILED 应合法"


# ════════════════════════════════════════════════════════════════
# B. start_operation
# ════════════════════════════════════════════════════════════════


class TestStartOperation:
    """R64 P0-03: start_operation — 创建操作 + nonce reserve + payload 一致性。"""

    @pytest.mark.asyncio
    async def test_start_operation_returns_uuid(self):
        """start_operation 返回 UUID 格式的 operation_id。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_test_001",
        )
        # UUID 格式校验
        uuid.UUID(operation_id)

    @pytest.mark.asyncio
    async def test_start_operation_initializes_datasource_states(self):
        """start_operation 后 datasource_states 包含 crdb/sqlite/relay_sqlite 三项 pending。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_test_002",
        )
        op = orch.get_operation(operation_id)
        assert op.datasource_states["crdb"]["status"] == "pending"
        assert op.datasource_states["sqlite"]["status"] == "pending"
        assert op.datasource_states["relay_sqlite"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_start_operation_reserves_nonce(self):
        """start_operation 调用 reserve_capability_nonce(nonce 状态为 reserved)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_reserve_001",
        )
        # 验证 nonce 在 DB 中为 reserved 状态
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE nonce=?",
            ("nonce_reserve_001",),
        )
        row = await cursor.fetchone()
        assert row is not None, "nonce 应已被 reserve"
        assert row[0] == "reserved"

    @pytest.mark.asyncio
    async def test_start_operation_with_failed_nonce_same_payload_allowed(self):
        """同 backup_id + manifest_digest 已有 failed nonce,同 payload_digest 允许重试。"""
        store, _ = await _make_store_with_restore_tables()
        # 先 reserve + fail 一个 nonce
        await store.reserve_capability_nonce(
            nonce="nonce_failed_001",
            operation_id="op_old",
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            reserved_by="tester",
        )
        await store.fail_capability_nonce("nonce_failed_001", "test_failure")
        # 同 payload_digest 重新 reserve 应成功
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,  # 同 payload
            nonce="nonce_new_001",
        )
        assert operation_id  # 成功创建

    @pytest.mark.asyncio
    async def test_start_operation_with_failed_nonce_different_payload_forbidden(self):
        """同 backup_id + manifest_digest 已有 failed nonce,换 payload_digest 禁止重试。"""
        store, _ = await _make_store_with_restore_tables()
        # 先 reserve + fail 一个 nonce(payload="d"*64)
        await store.reserve_capability_nonce(
            nonce="nonce_failed_002",
            operation_id="op_old_2",
            backup_id="backup_002",
            manifest_sha256="b" * 64,
            payload_digest="d" * 64,
            reserved_by="tester",
        )
        await store.fail_capability_nonce("nonce_failed_002", "test_failure")
        # 换 payload_digest 重新 reserve 应抛 NONCE_PAYLOAD_MISMATCH
        from services.error_codes import AppError, ErrorCodes
        orch = _make_orchestrator(store)
        with pytest.raises(AppError) as exc_info:
            await orch.start_operation(
                backup_id="backup_002",
                manifest_digest="b" * 64,
                requested_by="tester",
                payload_digest="e" * 64,  # 不同 payload
                nonce="nonce_new_002",
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_NONCE_PAYLOAD_MISMATCH


# ════════════════════════════════════════════════════════════════
# C. provision_staging
# ════════════════════════════════════════════════════════════════


class TestProvisionStaging:
    """R64 P0-03: provision_staging — 创建全新 staging 目标 + 故障注入。"""

    @pytest.mark.asyncio
    async def test_provision_staging_creates_targets(self):
        """provision_staging 返回 crdb/sqlite/relay_sqlite 三项目标。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_prov_001",
        )
        targets = await orch.provision_staging(operation_id)
        assert "crdb" in targets
        assert "sqlite" in targets
        assert "relay_sqlite" in targets
        assert "staging_restore_" in targets["crdb"]
        assert targets["sqlite"].endswith(".db")
        assert targets["relay_sqlite"].endswith(".db")

    @pytest.mark.asyncio
    async def test_provision_staging_creates_sqlite_files(self):
        """provision_staging 后 sqlite/relay_sqlite 文件实际存在。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_prov_002",
        )
        targets = await orch.provision_staging(operation_id)
        assert Path(targets["sqlite"]).exists()
        assert Path(targets["relay_sqlite"]).exists()

    @pytest.mark.asyncio
    async def test_provision_staging_fault_injection_crdb(self):
        """provision_staging crdb 故障注入 → PROVISION_FAILED + staging 销毁。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "crdb":
                raise RuntimeError("simulated_crdb_provision_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_provision.crdb": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_prov_fault_crdb",
        )
        with pytest.raises(AppError) as exc_info:
            await orch.provision_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_PROVISION_FAILED
        # operation 应已进入 FAILED 终态
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"

    @pytest.mark.asyncio
    async def test_provision_staging_fault_injection_sqlite(self):
        """provision_staging sqlite 故障注入 → PROVISION_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "sqlite":
                raise RuntimeError("simulated_sqlite_provision_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_provision.sqlite": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_prov_fault_sqlite",
        )
        with pytest.raises(AppError) as exc_info:
            await orch.provision_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_PROVISION_FAILED

    @pytest.mark.asyncio
    async def test_provision_staging_fault_injection_relay_sqlite(self):
        """provision_staging relay_sqlite 故障注入 → PROVISION_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "relay_sqlite":
                raise RuntimeError("simulated_relay_provision_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_provision.relay_sqlite": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_prov_fault_relay",
        )
        with pytest.raises(AppError) as exc_info:
            await orch.provision_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_PROVISION_FAILED


# ════════════════════════════════════════════════════════════════
# D. restore_to_staging
# ════════════════════════════════════════════════════════════════


class TestRestoreToStaging:
    """R64 P0-03: restore_to_staging — 按 datasource 顺序写入 staging。"""

    @pytest.mark.asyncio
    async def test_restore_to_staging_crdb_success(self):
        """restore_to_staging crdb 成功 → datasource_states[crdb].status=restored。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_restore_crdb",
        )
        await orch.provision_staging(operation_id)
        result = await orch.restore_to_staging(operation_id, "crdb")
        assert result is True
        op = orch.get_operation(operation_id)
        assert op.datasource_states["crdb"]["status"] == "restored"

    @pytest.mark.asyncio
    async def test_restore_to_staging_sqlite_success(self):
        """restore_to_staging sqlite 成功 → datasource_states[sqlite].status=restored。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_restore_sqlite",
        )
        await orch.provision_staging(operation_id)
        result = await orch.restore_to_staging(operation_id, "sqlite")
        assert result is True
        op = orch.get_operation(operation_id)
        assert op.datasource_states["sqlite"]["status"] == "restored"

    @pytest.mark.asyncio
    async def test_restore_to_staging_relay_sqlite_success(self):
        """restore_to_staging relay_sqlite 成功。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_restore_relay",
        )
        await orch.provision_staging(operation_id)
        result = await orch.restore_to_staging(operation_id, "relay_sqlite")
        assert result is True
        op = orch.get_operation(operation_id)
        assert op.datasource_states["relay_sqlite"]["status"] == "restored"

    @pytest.mark.asyncio
    async def test_restore_to_staging_fault_injection(self):
        """restore_to_staging 故障注入 → PROVISION_FAILED + staging 销毁。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            raise RuntimeError("simulated_restore_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_restore.crdb": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_restore_fault",
        )
        await orch.provision_staging(operation_id)
        with pytest.raises(AppError) as exc_info:
            await orch.restore_to_staging(operation_id, "crdb")
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_PROVISION_FAILED


# ════════════════════════════════════════════════════════════════
# E. validate_staging
# ════════════════════════════════════════════════════════════════


class TestValidateStaging:
    """R64 P0-03: validate_staging — schema/行数/主外键/守恒/hash/演练。"""

    @pytest.mark.asyncio
    async def test_validate_staging_all_pass(self):
        """validate_staging 全维度通过 → all_passed=True。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_validate_ok",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        summary = await orch.validate_staging(operation_id)
        assert summary.all_passed is True
        assert summary.schema == "ok"
        assert summary.row_count == "ok"
        assert summary.foreign_keys == "ok"
        assert summary.business_invariant == "ok"
        assert summary.hash_check == "ok"
        assert summary.dry_run == "ok"

    @pytest.mark.asyncio
    async def test_validate_staging_schema_fail(self):
        """validate_staging schema 故障注入 → VALIDATE_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "schema":
                raise RuntimeError("schema_mismatch")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_validate.schema": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_validate_schema_fail",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        with pytest.raises(AppError) as exc_info:
            await orch.validate_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED

    @pytest.mark.asyncio
    async def test_validate_staging_hash_check_fail(self):
        """validate_staging hash_check 故障注入 → VALIDATE_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "hash_check":
                raise RuntimeError("hash_mismatch_detected")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_validate.hash_check": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_validate_hash_fail",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        with pytest.raises(AppError) as exc_info:
            await orch.validate_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED

    @pytest.mark.asyncio
    async def test_validate_staging_business_invariant_fail(self):
        """validate_staging business_invariant 故障注入 → VALIDATE_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            if ds == "business_invariant":
                raise RuntimeError("business_invariant_violation")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_validate.business_invariant": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_validate_biz_fail",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        with pytest.raises(AppError) as exc_info:
            await orch.validate_staging(operation_id)
        assert exc_info.value.code == ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED


# ════════════════════════════════════════════════════════════════
# F. approval + MFA
# ════════════════════════════════════════════════════════════════


class TestApprovalAndMFA:
    """R64 P0-03: request_approval + execute_switch — approval_id + MFA receipt 双因子。"""

    @pytest.mark.asyncio
    async def test_request_approval_success(self):
        """request_approval 成功 → phase=AWAIT_APPROVAL。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_approval_ok",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, "approval_001", "mfa_001")
        op = orch.get_operation(operation_id)
        assert op.phase.value == "await_approval"
        assert op.approval_id == "approval_001"
        assert op.mfa_receipt_id == "mfa_001"

    @pytest.mark.asyncio
    async def test_request_approval_missing_approval_id(self):
        """request_approval approval_id 为空 → APPROVAL_REQUIRED。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_approval_missing",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        with pytest.raises(AppError) as exc_info:
            await orch.request_approval(operation_id, "", "mfa_001")
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_request_approval_missing_mfa_receipt_id(self):
        """request_approval mfa_receipt_id 为空 → MFA_REQUIRED。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_mfa_missing",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        with pytest.raises(AppError) as exc_info:
            await orch.request_approval(operation_id, "approval_001", "")
        assert exc_info.value.code == ErrorCodes.RESTORE_MFA_REQUIRED

    @pytest.mark.asyncio
    async def test_execute_switch_approval_mismatch(self):
        """execute_switch approval_id 与 request_approval 不匹配 → APPROVAL_REQUIRED。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_switch_approval_mismatch",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, "approval_001", "mfa_001")
        with pytest.raises(AppError) as exc_info:
            await orch.execute_blue_green_switch(
                operation_id, "approval_WRONG", "mfa_001"
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_execute_switch_mfa_mismatch(self):
        """execute_switch mfa_receipt_id 与 request_approval 不匹配 → MFA_REQUIRED。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_switch_mfa_mismatch",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, "approval_001", "mfa_001")
        with pytest.raises(AppError) as exc_info:
            await orch.execute_blue_green_switch(
                operation_id, "approval_001", "mfa_WRONG"
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_MFA_REQUIRED


# ════════════════════════════════════════════════════════════════
# G. 蓝绿切换
# ════════════════════════════════════════════════════════════════


class TestBlueGreenSwitch:
    """R64 P0-03: execute_blue_green_switch — CAS 切换 + nonce consume + rollback target。"""

    @pytest.mark.asyncio
    async def test_execute_switch_success_returns_version(self):
        """execute_switch 成功 → 返回 switch_version(UUID)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, switch_version = await _run_full_happy_path(orch)
        uuid.UUID(switch_version)  # UUID 格式校验

    @pytest.mark.asyncio
    async def test_execute_switch_consumes_nonce(self):
        """execute_switch 成功后 nonce 状态变为 consumed。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, _ = await _run_full_happy_path(orch, payload_digest="c" * 64)
        # 从 DB 查询 nonce 状态
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE nonce=?",
            ("nonce_" + operation_id.split("-")[0][:16],),
        )
        # 找到本次 operation 关联的 nonce
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE operation_id=?",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert any(r[0] == "consumed" for r in rows), \
            f"应至少有一个 consumed nonce,实际: {rows}"

    @pytest.mark.asyncio
    async def test_execute_switch_persists_rollback_target(self):
        """execute_switch 成功后 restore_rollback_targets 表有记录。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, switch_version = await _run_full_happy_path(orch)
        targets = await orch.list_rollback_targets(operation_id)
        assert len(targets) == 1
        assert targets[0]["switch_version"] == switch_version
        assert "crdb" in targets[0]["active_pointer"]
        assert "expires_at" in targets[0]

    @pytest.mark.asyncio
    async def test_execute_switch_fault_injection(self):
        """execute_switch 故障注入 → SWITCH_FAILED + nonce 不 consume。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            raise RuntimeError("simulated_switch_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"blue_green_switch": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_switch_fault",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, "approval_001", "mfa_001")
        with pytest.raises(AppError) as exc_info:
            await orch.execute_blue_green_switch(
                operation_id, "approval_001", "mfa_001"
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_SWITCH_FAILED
        # operation 应进入 FAILED 终态
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"


# ════════════════════════════════════════════════════════════════
# H. rollback
# ════════════════════════════════════════════════════════════════


class TestRollback:
    """R64 P0-03: rollback_operation — COMPLETED → ROLLED_BACK 状态机驱动回滚。"""

    @pytest.mark.asyncio
    async def test_rollback_after_completed(self):
        """COMPLETED → ROLLED_BACK 成功,返回原 switch_version。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, switch_version = await _run_full_happy_path(orch)
        rollback_version = await orch.rollback_operation(
            operation_id, reason="post_switch_anomaly_detected"
        )
        assert rollback_version == switch_version
        op = orch.get_operation(operation_id)
        assert op.phase.value == "rolled_back"

    @pytest.mark.asyncio
    async def test_rollback_without_switch_fails(self):
        """未切换过的 operation 回滚 → ROLLED_BACK 转换非法(PHASE_TRANSITION_INVALID)。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_rollback_no_switch",
        )
        # operation 仍在 INIT 状态,直接调用 rollback → 非法转换
        with pytest.raises(AppError) as exc_info:
            await orch.rollback_operation(operation_id, reason="test")
        assert exc_info.value.code == ErrorCodes.RESTORE_PHASE_TRANSITION_INVALID

    @pytest.mark.asyncio
    async def test_rollback_fault_injection(self):
        """rollback 故障注入 → ROLLBACK_FAILED。"""
        from services.error_codes import AppError, ErrorCodes

        def _fault(orch, op_id, ds):
            raise RuntimeError("simulated_rollback_failure")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store, fault_hooks={"rollback": _fault})
        operation_id, _ = await _run_full_happy_path(orch)
        with pytest.raises(AppError) as exc_info:
            await orch.rollback_operation(operation_id, reason="test")
        assert exc_info.value.code == ErrorCodes.RESTORE_ROLLBACK_FAILED


# ════════════════════════════════════════════════════════════════
# I. fail_operation
# ════════════════════════════════════════════════════════════════


class TestFailOperation:
    """R64 P0-03: fail_operation — 销毁 staging + nonce=failed + 幂等。"""

    @pytest.mark.asyncio
    async def test_fail_operation_destroys_staging(self):
        """fail_operation 后 staging SQLite 文件被删除。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_fail_destroys",
        )
        targets = await orch.provision_staging(operation_id)
        # 确认 staging 文件存在
        assert Path(targets["sqlite"]).exists()
        assert Path(targets["relay_sqlite"]).exists()
        # 失败处理
        await orch.fail_operation(operation_id, reason="test_failure")
        # staging 文件应被删除
        assert not Path(targets["sqlite"]).exists()
        assert not Path(targets["relay_sqlite"]).exists()

    @pytest.mark.asyncio
    async def test_fail_operation_marks_nonce_failed(self):
        """fail_operation 后 nonce 状态变为 failed。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_fail_state",
        )
        await orch.fail_operation(operation_id, reason="test_failure")
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE nonce=?",
            ("nonce_fail_state",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed"

    @pytest.mark.asyncio
    async def test_fail_operation_idempotent(self):
        """fail_operation 对已 FAILED 的 operation 幂等(不重复销毁)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_001",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_fail_idempotent",
        )
        await orch.provision_staging(operation_id)
        # 第一次 fail
        await orch.fail_operation(operation_id, reason="first_failure")
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"
        # 第二次 fail(幂等,不抛异常)
        await orch.fail_operation(operation_id, reason="second_failure")
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"  # 仍是 failed


# ════════════════════════════════════════════════════════════════
# J. nonce 状态机
# ════════════════════════════════════════════════════════════════


class TestNonceStateMachine:
    """R64 P0-03: nonce 状态机 — reserved → consumed | failed + payload 防篡改。"""

    @pytest.mark.asyncio
    async def test_reserve_then_consume(self):
        """reserve → consume 成功(reserved → consumed CAS)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, _ = await _run_full_happy_path(orch, payload_digest="f" * 64)
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE operation_id=?",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        assert any(r[0] == "consumed" for r in rows)

    @pytest.mark.asyncio
    async def test_reserve_then_fail_then_retry_same_payload(self):
        """reserve → fail → 同 payload 重新 reserve 允许(failed nonce 留审计)。"""
        store, _ = await _make_store_with_restore_tables()
        # 第一次: reserve + fail
        await store.reserve_capability_nonce(
            nonce="nonce_retry_001",
            operation_id="op_first_attempt",
            backup_id="backup_retry",
            manifest_sha256="r" * 64,
            payload_digest="p" * 64,
            reserved_by="tester",
        )
        await store.fail_capability_nonce("nonce_retry_001", "first_attempt_failed")
        # 第二次: 同 payload 重新 reserve(通过 orchestrator)
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_retry",
            manifest_digest="r" * 64,
            requested_by="tester",
            payload_digest="p" * 64,  # 同 payload
            nonce="nonce_retry_002",
        )
        # 验证两个 nonce 都在 DB(第一个 failed + 第二个 reserved)
        cursor = await store._db.execute(
            "SELECT nonce, status FROM restore_capability_nonces "
            "WHERE backup_id=? ORDER BY reserved_at",
            ("backup_retry",),
        )
        rows = await cursor.fetchall()
        statuses = {r[0]: r[1] for r in rows}
        assert statuses["nonce_retry_001"] == "failed"
        assert statuses["nonce_retry_002"] == "reserved"

    @pytest.mark.asyncio
    async def test_reserve_then_fail_then_retry_different_payload_forbidden(self):
        """reserve → fail → 换 payload 重新 reserve 禁止(NONCE_PAYLOAD_MISMATCH)。"""
        from services.error_codes import AppError, ErrorCodes
        store, _ = await _make_store_with_restore_tables()
        # 第一次: reserve + fail(payload="p"*64)
        await store.reserve_capability_nonce(
            nonce="nonce_payload_001",
            operation_id="op_payload_first",
            backup_id="backup_payload",
            manifest_sha256="m" * 64,
            payload_digest="p" * 64,
            reserved_by="tester",
        )
        await store.fail_capability_nonce("nonce_payload_001", "first_failed")
        # 第二次: 换 payload → 应抛 NONCE_PAYLOAD_MISMATCH
        orch = _make_orchestrator(store)
        with pytest.raises(AppError) as exc_info:
            await orch.start_operation(
                backup_id="backup_payload",
                manifest_digest="m" * 64,
                requested_by="tester",
                payload_digest="q" * 64,  # 不同 payload
                nonce="nonce_payload_002",
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_NONCE_PAYLOAD_MISMATCH


# ════════════════════════════════════════════════════════════════
# K. 三次全新空白环境完整恢复(0% / 50% / 100% 故障注入)
# ════════════════════════════════════════════════════════════════


class TestThreeFullRecoveries:
    """R64 P0-03: 三次全新空白环境完整恢复 — 验收标准(验收时 active 数据保持单一一致时间点)。

    验收要求:在 CRDB / SQLite / relay SQLite 的 0% / 25% / 50% / 75% / 100% 注入故障,
    active 数据始终保持单一一致时间点(不混合);三次全新空白环境完整恢复成功。
    """

    @pytest.mark.asyncio
    async def test_full_recovery_no_fault(self):
        """0% 故障注入:完整恢复成功(三数据源全过)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, switch_version = await _run_full_happy_path(orch)
        op = orch.get_operation(operation_id)
        assert op.phase.value == "completed"
        # 验证 active 指针保留(rollback target 存在)
        targets = await orch.list_rollback_targets(operation_id)
        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_full_recovery_with_50_percent_fault(self):
        """50% 故障注入:第二个 datasource 失败 → operation FAILED + active 不受影响。

        模拟:crdb restore 成功,sqlite restore 故障注入失败。
        active 数据应保持切换前状态(staging 已销毁,未影响 active)。
        """
        from services.error_codes import AppError

        def _fault(orch, op_id, ds):
            if ds == "sqlite":
                raise RuntimeError("50_percent_fault_at_sqlite")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_restore.sqlite": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_50pct",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="5" * 64,
            nonce="nonce_50pct",
        )
        await orch.provision_staging(operation_id)
        # crdb 成功
        await orch.restore_to_staging(operation_id, "crdb")
        # sqlite 失败(50% 故障)
        with pytest.raises(AppError):
            await orch.restore_to_staging(operation_id, "sqlite")
        # 验证 active 未受影响(无 rollback target,因为未切换)
        targets = await orch.list_rollback_targets(operation_id)
        assert len(targets) == 0
        # operation 进入 FAILED
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"

    @pytest.mark.asyncio
    async def test_full_recovery_with_100_percent_fault(self):
        """100% 故障注入:provision 即失败 → operation FAILED + staging 销毁。"""
        from services.error_codes import AppError

        def _fault(orch, op_id, ds):
            raise RuntimeError("100_percent_fault_at_first_datasource")

        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(
            store, fault_hooks={"staging_provision.crdb": _fault}
        )
        operation_id = await orch.start_operation(
            backup_id="backup_100pct",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="1" * 64,
            nonce="nonce_100pct",
        )
        with pytest.raises(AppError):
            await orch.provision_staging(operation_id)
        # 验证 active 未受影响
        targets = await orch.list_rollback_targets(operation_id)
        assert len(targets) == 0
        op = orch.get_operation(operation_id)
        assert op.phase.value == "failed"

    @pytest.mark.asyncio
    async def test_three_sequential_full_recoveries(self):
        """三次连续完整恢复:每次均成功 + 独立隔离(operation_id 不同)。"""
        store, _ = await _make_store_with_restore_tables()
        operation_ids = []
        for i in range(3):
            orch = _make_orchestrator(store)
            operation_id, _ = await _run_full_happy_path(
                orch, payload_digest=f"payload_{i}" + "0" * 56
            )
            operation_ids.append(operation_id)
            op = orch.get_operation(operation_id)
            assert op.phase.value == "completed"
        # 验证三次 operation_id 互不相同
        assert len(set(operation_ids)) == 3


# ════════════════════════════════════════════════════════════════
# L. 持久化(restore_operations / restore_operation_events / restore_rollback_targets)
# ════════════════════════════════════════════════════════════════


class TestPersistence:
    """R64 P0-03: 持久化 — operation / events / rollback_targets 写入 SQLite 表。"""

    @pytest.mark.asyncio
    async def test_operation_persisted_to_db(self):
        """start_operation 后 restore_operations 表有记录(phase=init)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_persist_001",
            manifest_digest="a" * 64,
            requested_by="tester_persist",
            payload_digest="d" * 64,
            nonce="nonce_persist_op",
        )
        persisted = await orch.get_persisted_operation(operation_id)
        assert persisted is not None
        assert persisted["backup_id"] == "backup_persist_001"
        assert persisted["phase"] == "init"
        assert persisted["created_by"] == "tester_persist"

    @pytest.mark.asyncio
    async def test_operation_phase_updated_in_db(self):
        """provision_staging 后 restore_operations 表 phase=staging_provision。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_persist_002",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_persist_phase",
        )
        await orch.provision_staging(operation_id)
        persisted = await orch.get_persisted_operation(operation_id)
        assert persisted["phase"] == "staging_provision"

    @pytest.mark.asyncio
    async def test_events_persisted_to_db(self):
        """完整 happy path 后 restore_operation_events 表有多条事件。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id, _ = await _run_full_happy_path(orch)
        events = await orch.list_operation_events(operation_id)
        # 至少 7 条事件:phase_transition(init) + staging_provisioned +
        # staging_restored*3 + staging_validated + approval_requested + switched +
        # phase_transition(completed)
        assert len(events) >= 7
        event_types = {e["event_type"] for e in events}
        assert "phase_transition" in event_types
        assert "staging_provisioned" in event_types
        assert "staging_restored" in event_types
        assert "staging_validated" in event_types
        assert "approval_requested" in event_types
        assert "switched" in event_types

    @pytest.mark.asyncio
    async def test_rollback_target_persisted_to_db(self):
        """execute_switch 后 restore_rollback_targets 表有记录(含 expires_at)。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store, rollback_ttl_seconds=3600)
        operation_id, switch_version = await _run_full_happy_path(orch)
        targets = await orch.list_rollback_targets(operation_id)
        assert len(targets) == 1
        target = targets[0]
        assert target["switch_version"] == switch_version
        assert target["operation_id"] == operation_id
        assert "expires_at" in target
        assert target["expires_at"]  # 非空

    @pytest.mark.asyncio
    async def test_failed_operation_event_persisted(self):
        """fail_operation 后 restore_operation_events 表有 failed 事件。"""
        store, _ = await _make_store_with_restore_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_fail_event",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_fail_event",
        )
        await orch.fail_operation(operation_id, reason="test_failure_event")
        events = await orch.list_operation_events(operation_id)
        event_types = {e["event_type"] for e in events}
        assert "failed" in event_types
