"""R65 P0-02: RestoreBackend 真实恢复数据面测试。

R65 终审报告 P0-02 整改背景:
    R64 的 RestoreOrchestrator 只是状态机骨架:
      - provision_staging() 仅 touch 空文件
      - restore_to_staging() 仅改状态字段
      - validate_staging() 默认 ok
      - execute_blue_green_switch() 仅做字符串相等比较
    状态机可以产生"已恢复、已验证"的审计记录但没有真实恢复和验证。

R65 P0-02 整改要求:
    1. RestoreBackend Protocol 提供 7 个真实方法
    2. 三个具体实现(CRDB / cache SQLite / relay SQLite)
    3. orchestrator 调用 backend 真实方法,不再伪造状态
    4. backend 验证返回真实 row counts + content hash + schema fingerprint
    5. 蓝绿切换:SQLite 原子 rename / CRDB schema routing

测试覆盖矩阵:
    A. SQLiteRestoreBackend 单元测试
        - provision: 创建新 staging 文件
        - load_verified_payload: 真实写入数据 + 返回 row counts + content hash
        - validate: 6 维度验证(全 ok + 行数不匹配 fail)
        - commit_switch: 原子 rename(staging → active, active → backup)
        - rollback_switch: 反向 rename
        - destroy: 删除 staging 文件
    B. BackendRegistry
        - register/get/all_backends
        - fail-closed: 未注册的 datasource 抛异常
        - datasource_name 不匹配拒绝
    C. RestoreOrchestrator + BackendRegistry 集成
        - provision_staging 真实创建 staging 文件
        - restore_to_staging 真实写入 + 返回 row counts + content hash
        - validate_staging 真实 6 维度验证
        - execute_blue_green_switch 真实切换 + 持久化 switch_result
        - rollback_operation 真实回滚
        - fail_operation 真实销毁 staging
    D. StagingValidationResult.all_passed 严格性
        - 任一维度非 ok(skipped/pending/unknown/fail)即 False
    E. 向后兼容:backends=None 时 orchestrator 仍走骨架路径
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 测试环境兼容
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# 模块级 skip:cache_store 必须真实可用
from database import cache_store as _cs_module  # noqa: E402

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _make_test_schema_initializer(tables: list[str]):
    """构造 SQLite schema initializer 回调,创建指定表(简单 schema 用于测试)。"""

    async def _initializer(conn) -> None:
        # 简单测试 schema:每张表 id INTEGER PK + content TEXT
        for table in tables:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                f"(id INTEGER PRIMARY KEY, content TEXT)"
            )

    return _initializer


def _make_simple_records(n: int) -> list[dict]:
    """生成 n 条简单测试记录。"""
    return [{"id": i, "content": f"row_{i}"} for i in range(n)]


async def _make_store_with_restore_tables(db_path: str | None = None):
    """构造 CacheStore + restore_operations 三张表(复用 r64 测试辅助)。"""
    from database.cache_store import CacheStore
    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r65_p0_2_test_")
        db_path = str(Path(_tmp_dir) / "test_restore.db")
    store = CacheStore(db_path=db_path)
    await store.init()
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


def _make_orchestrator(store, *, staging_root=None, backends=None, fault_hooks=None):
    """构造 RestoreOrchestrator(注入 store + 可选 backends)。"""
    from services.restore_orchestrator import RestoreOrchestrator
    if staging_root is None:
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_staging_")
    return RestoreOrchestrator(
        store,
        staging_root=staging_root,
        fault_hooks=fault_hooks or {},
        backends=backends,
    )


# ════════════════════════════════════════════════════════════════
# A. SQLiteRestoreBackend 单元测试
# ════════════════════════════════════════════════════════════════


class TestSQLiteRestoreBackend:
    """R65 P0-02: SQLiteRestoreBackend 真实恢复后端测试。"""

    @pytest.mark.asyncio
    async def test_provision_creates_staging_file(self):
        """provision() 创建新 staging SQLite 文件。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_prov_"))
        active_path = staging_root / "active.db"
        # 创建空 active 文件(模拟首次部署)
        active_path.touch()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["test_table"]),
        )
        operation_id = str(uuid.uuid4())
        result = await backend.provision(operation_id, staging_root)

        assert result.target_type == "sqlite_file"
        assert Path(result.target).exists()
        assert result.target != str(active_path)  # 不能接触 active
        # staging 文件应有初始化的 schema
        import aiosqlite
        async with aiosqlite.connect(result.target) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='test_table'"
            )
            row = await cursor.fetchone()
            assert row is not None, "staging 应有 test_table"
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_load_verified_payload_writes_data_and_returns_counts(self):
        """load_verified_payload() 真实写入数据并返回 row counts + content hash。"""
        from services.restore_backends import SQLiteRestoreBackend, StagingProvisionResult

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_load_"))
        active_path = staging_root / "active.db"
        active_path.touch()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["test_table"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)

        records = _make_simple_records(10)
        restore_result = await backend.load_verified_payload(
            operation_id=operation_id,
            provision_result=provision_result,
            tables_data={"test_table": records},
        )

        assert restore_result.rows_restored["test_table"] == 10
        assert restore_result.content_hash["test_table"]  # 非空 hash
        assert len(restore_result.content_hash["test_table"]) == 64  # SHA-256 hex
        assert restore_result.bytes_written > 0
        assert restore_result.duration_seconds >= 0
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_validate_all_dimensions_ok(self):
        """validate() 在数据完整时所有 6 维度均为 ok。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_val_ok_"))
        active_path = staging_root / "active.db"
        active_path.touch()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["test_table"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)
        records = _make_simple_records(10)
        restore_result = await backend.load_verified_payload(
            operation_id=operation_id,
            provision_result=provision_result,
            tables_data={"test_table": records},
        )
        validation = await backend.validate(
            operation_id=operation_id,
            provision_result=provision_result,
            restore_result=restore_result,
            expected_tables={"test_table": records},
        )

        # R65 P0-02: 所有 6 维度必须显式 ok
        assert validation.schema == "ok"
        assert validation.row_count == "ok"
        assert validation.foreign_keys == "ok"
        assert validation.business_invariant == "ok"
        assert validation.hash_check == "ok"
        assert validation.dry_run == "ok"
        assert validation.all_passed is True
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_validate_row_count_mismatch_fails(self):
        """validate() 行数不匹配时 row_count=fail → all_passed=False。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_val_fail_"))
        active_path = staging_root / "active.db"
        active_path.touch()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["test_table"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)
        records = _make_simple_records(10)
        restore_result = await backend.load_verified_payload(
            operation_id=operation_id,
            provision_result=provision_result,
            tables_data={"test_table": records},
        )
        # 期望 20 行但实际只有 10 行
        validation = await backend.validate(
            operation_id=operation_id,
            provision_result=provision_result,
            restore_result=restore_result,
            expected_tables={"test_table": _make_simple_records(20)},
        )

        assert validation.row_count == "fail"
        assert validation.all_passed is False
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_commit_switch_atomic_rename(self):
        """commit_switch() 原子 rename:staging → active,active → backup。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_switch_"))
        active_path = staging_root / "active.db"

        # 准备旧 active(含旧数据)
        import aiosqlite
        async with aiosqlite.connect(str(active_path)) as conn:
            await conn.execute("CREATE TABLE old_table (id INTEGER PRIMARY KEY)")
            await conn.execute("INSERT INTO old_table VALUES (1)")
            await conn.commit()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["new_table"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)
        records = _make_simple_records(5)
        await backend.load_verified_payload(
            operation_id=operation_id,
            provision_result=provision_result,
            tables_data={"new_table": records},
        )

        prepare_result = await backend.prepare_switch(operation_id, provision_result)
        switch_result = await backend.commit_switch(operation_id, provision_result, prepare_result)

        # 验证:active 现在是 staging 内容(new_table)
        assert switch_result.previous_target  # 旧 active 备份路径
        assert switch_result.new_target == str(active_path)
        # active 现在应有 new_table 而非 old_table
        async with aiosqlite.connect(str(active_path)) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='new_table'"
            )
            assert await cursor.fetchone() is not None
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='old_table'"
            )
            assert await cursor.fetchone() is None
        # 旧 active 备份存在
        assert Path(switch_result.previous_target).exists()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_rollback_switch_restores_previous_active(self):
        """rollback_switch() 反向 rename:backup → active。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_rb_"))
        active_path = staging_root / "active.db"

        import aiosqlite
        async with aiosqlite.connect(str(active_path)) as conn:
            await conn.execute("CREATE TABLE old_table (id INTEGER PRIMARY KEY)")
            await conn.execute("INSERT INTO old_table VALUES (1)")
            await conn.commit()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["new_table"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)
        await backend.load_verified_payload(
            operation_id=operation_id,
            provision_result=provision_result,
            tables_data={"new_table": _make_simple_records(3)},
        )
        prepare = await backend.prepare_switch(operation_id, provision_result)
        switch_result = await backend.commit_switch(operation_id, provision_result, prepare)

        # 回滚
        rollback_result = await backend.rollback_switch(operation_id, switch_result)
        # active 应恢复为旧内容(old_table)
        async with aiosqlite.connect(str(active_path)) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='old_table'"
            )
            assert await cursor.fetchone() is not None
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='new_table'"
            )
            assert await cursor.fetchone() is None
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_destroy_deletes_staging_file(self):
        """destroy() 删除 staging 文件(幂等)。"""
        from services.restore_backends import SQLiteRestoreBackend

        staging_root = Path(tempfile.mkdtemp(prefix="r65_p0_2_destroy_"))
        active_path = staging_root / "active.db"
        active_path.touch()

        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=active_path,
            schema_initializer=_make_test_schema_initializer(["t"]),
        )
        operation_id = str(uuid.uuid4())
        provision_result = await backend.provision(operation_id, staging_root)
        assert Path(provision_result.target).exists()

        await backend.destroy(operation_id, provision_result)
        assert not Path(provision_result.target).exists()
        # 幂等:再次 destroy 不抛异常
        await backend.destroy(operation_id, provision_result)
        shutil.rmtree(staging_root, ignore_errors=True)

    def test_invalid_datasource_name_rejected(self):
        """SQLiteRestoreBackend 拒绝非 sqlite/relay_sqlite 的 datasource_name。"""
        from services.restore_backends import SQLiteRestoreBackend
        with pytest.raises(ValueError, match="不支持 datasource"):
            SQLiteRestoreBackend(datasource_name="crdb", active_db_path="/tmp/x.db")


# ════════════════════════════════════════════════════════════════
# B. BackendRegistry 测试
# ════════════════════════════════════════════════════════════════


class TestBackendRegistry:
    """R65 P0-02: BackendRegistry fail-closed 注册表。"""

    def test_register_and_get(self):
        """register + get 正常工作。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        registry = BackendRegistry()
        backend = SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path="/tmp/test_registry.db",
        )
        registry.register("sqlite", backend)
        assert "sqlite" in registry
        assert len(registry) == 1
        assert registry.get("sqlite") is backend

    def test_get_unregistered_fails_closed(self):
        """R65 P0-02: get 未注册的 datasource 抛异常(fail-closed)。"""
        from services.restore_backends import BackendRegistry
        from services.error_codes import AppError
        registry = BackendRegistry()
        with pytest.raises(AppError):
            registry.get("crdb")

    def test_register_invalid_datasource_rejected(self):
        """register 拒绝非法 datasource 名。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        registry = BackendRegistry()
        backend = SQLiteRestoreBackend(
            datasource_name="sqlite", active_db_path="/tmp/test.db"
        )
        with pytest.raises(ValueError, match="不支持 datasource"):
            registry.register("invalid", backend)

    def test_register_datasource_name_mismatch_rejected(self):
        """register 拒绝 backend.datasource_name 与注册 datasource 不匹配。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        registry = BackendRegistry()
        backend = SQLiteRestoreBackend(
            datasource_name="sqlite", active_db_path="/tmp/test.db"
        )
        with pytest.raises(ValueError, match="不匹配"):
            registry.register("relay_sqlite", backend)


# ════════════════════════════════════════════════════════════════
# C. StagingValidationResult 严格性
# ════════════════════════════════════════════════════════════════


class TestStagingValidationResultStrictness:
    """R65 P0-02: StagingValidationResult.all_passed 严格性 — 任何非 ok 即失败。"""

    def test_all_ok_returns_true(self):
        from services.restore_backends import StagingValidationResult
        r = StagingValidationResult(
            schema="ok", row_count="ok", foreign_keys="ok",
            business_invariant="ok", hash_check="ok", dry_run="ok",
        )
        assert r.all_passed is True

    @pytest.mark.parametrize("dim", [
        "schema", "row_count", "foreign_keys",
        "business_invariant", "hash_check", "dry_run",
    ])
    def test_any_fail_returns_false(self, dim):
        from services.restore_backends import StagingValidationResult
        kwargs = {dim: "fail"}
        r = StagingValidationResult(**kwargs)
        assert r.all_passed is False

    @pytest.mark.parametrize("value", ["skipped", "pending", "unknown", ""])
    def test_non_ok_values_return_false(self, value):
        """R65 P0-02: skipped/pending/unknown/空 均视为失败(不能默认 ok)。"""
        from services.restore_backends import StagingValidationResult
        r = StagingValidationResult(schema=value)
        assert r.all_passed is False


# ════════════════════════════════════════════════════════════════
# D. RestoreOrchestrator + BackendRegistry 集成
# ════════════════════════════════════════════════════════════════


class TestOrchestratorBackendIntegration:
    """R65 P0-02: orchestrator 与 backend 真实集成测试。"""

    @pytest.mark.asyncio
    async def test_provision_staging_calls_backend_provision(self):
        """provision_staging 在提供 backends 时调用 backend.provision()。"""
        from services.restore_backends import (
            BackendRegistry, SQLiteRestoreBackend,
        )

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_prov_")

        active_cache = Path(staging_root) / "active_cache.db"
        active_relay = Path(staging_root) / "active_relay.db"

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["t1"]),
            ),
        )
        registry.register(
            "relay_sqlite",
            SQLiteRestoreBackend(
                datasource_name="relay_sqlite",
                active_db_path=active_relay,
                schema_initializer=_make_test_schema_initializer(["t2"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        targets = await orch.provision_staging(operation_id)

        # 验证:sqlite/relay_sqlite 真实创建了 staging 文件
        assert Path(targets["sqlite"]).exists()
        assert Path(targets["relay_sqlite"]).exists()
        # crdb 未注册,使用骨架默认名
        assert targets["crdb"].startswith("staging_restore_")

        # 验证:provision_result 保存在 datasource_states
        op = orch.get_operation(operation_id)
        assert op.datasource_states["sqlite"]["provision_result"]["target_type"] == "sqlite_file"
        assert op.datasource_states["relay_sqlite"]["provision_result"]["target_type"] == "sqlite_file"
        # crdb 走骨架路径
        assert op.datasource_states["crdb"]["provision_result"]["target_type"] == "skeleton"

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_restore_to_staging_calls_backend_load(self):
        """restore_to_staging 在提供 backends + tables_data 时真实写入。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_load_")

        active_cache = Path(staging_root) / "active_cache.db"

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["t1"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)

        records = _make_simple_records(5)
        await orch.restore_to_staging(
            operation_id, "sqlite", tables_data={"t1": records},
        )

        op = orch.get_operation(operation_id)
        rr = op.datasource_states["sqlite"]["restore_result"]
        assert rr["rows_restored"]["t1"] == 5
        assert len(rr["content_hash"]["t1"]) == 64
        assert rr["bytes_written"] > 0

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_validate_staging_calls_backend_validate(self):
        """validate_staging 在提供 backends + expected_tables 时真实验证。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_val_")

        active_cache = Path(staging_root) / "active_cache.db"

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["t1"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        records = _make_simple_records(5)
        await orch.restore_to_staging(
            operation_id, "sqlite", tables_data={"t1": records},
        )
        # crdb/relay_sqlite 未注册,仅 sqlite 真实验证
        summary = await orch.validate_staging(
            operation_id,
            expected_tables={"sqlite": {"t1": records}},
        )

        assert summary.schema == "ok"
        assert summary.row_count == "ok"
        assert summary.hash_check == "ok"
        assert summary.dry_run == "ok"
        assert summary.all_passed is True

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_validate_staging_failure_fail_closed(self):
        """validate_staging 行数不匹配时 fail_operation + raise。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        from services.error_codes import AppError

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_valfail_")

        active_cache = Path(staging_root) / "active_cache.db"

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["t1"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        records = _make_simple_records(5)
        await orch.restore_to_staging(
            operation_id, "sqlite", tables_data={"t1": records},
        )

        # 期望 100 行但实际 5 行 → row_count fail
        from services.restore_orchestrator import RestorePhase
        with pytest.raises(AppError):
            await orch.validate_staging(
                operation_id,
                expected_tables={"sqlite": {"t1": _make_simple_records(100)}},
            )

        # operation 应已 fail → staging 已销毁
        op = orch.get_operation(operation_id)
        assert op.phase == RestorePhase.FAILED

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_execute_switch_calls_backend_commit(self):
        """execute_blue_green_switch 在提供 backends 时调用 backend.commit_switch()。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        from services.restore_orchestrator import RestorePhase

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_sw_")

        active_cache = Path(staging_root) / "active_cache.db"
        # 准备旧 active(有旧数据)
        import aiosqlite
        async with aiosqlite.connect(str(active_cache)) as conn:
            await conn.execute("CREATE TABLE old_t (id INTEGER PRIMARY KEY)")
            await conn.execute("INSERT INTO old_t VALUES (1)")
            await conn.commit()

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["new_t"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        records = _make_simple_records(3)
        await orch.restore_to_staging(
            operation_id, "sqlite", tables_data={"new_t": records},
        )
        await orch.validate_staging(
            operation_id, expected_tables={"sqlite": {"new_t": records}},
        )
        approval_id = "approval_" + uuid.uuid4().hex[:16]
        mfa_receipt_id = "mfa_" + uuid.uuid4().hex[:16]
        await orch.request_approval(operation_id, approval_id, mfa_receipt_id)
        switch_version = await orch.execute_blue_green_switch(
            operation_id, approval_id, mfa_receipt_id,
        )

        # 切换后 active 应有 new_t(无 old_t)
        async with aiosqlite.connect(str(active_cache)) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='new_t'"
            )
            assert await cursor.fetchone() is not None
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='old_t'"
            )
            assert await cursor.fetchone() is None

        # switch_result 持久化在 datasource_states
        op = orch.get_operation(operation_id)
        assert op.phase == RestorePhase.COMPLETED
        assert op.switch_version == switch_version
        sr = op.datasource_states["sqlite"]["switch_result"]
        assert sr["new_target"] == str(active_cache)
        assert sr["previous_target"]  # 旧 active 备份

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_rollback_operation_calls_backend_rollback(self):
        """rollback_operation 在提供 backends 时调用 backend.rollback_switch()。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        from services.restore_orchestrator import RestorePhase

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_rb_")

        active_cache = Path(staging_root) / "active_cache.db"
        import aiosqlite
        async with aiosqlite.connect(str(active_cache)) as conn:
            await conn.execute("CREATE TABLE old_t (id INTEGER PRIMARY KEY)")
            await conn.execute("INSERT INTO old_t VALUES (1)")
            await conn.commit()

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["new_t"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        records = _make_simple_records(3)
        await orch.restore_to_staging(
            operation_id, "sqlite", tables_data={"new_t": records},
        )
        await orch.validate_staging(
            operation_id, expected_tables={"sqlite": {"new_t": records}},
        )
        approval_id = "approval_" + uuid.uuid4().hex[:16]
        mfa_receipt_id = "mfa_" + uuid.uuid4().hex[:16]
        await orch.request_approval(operation_id, approval_id, mfa_receipt_id)
        await orch.execute_blue_green_switch(operation_id, approval_id, mfa_receipt_id)

        # 回滚
        await orch.rollback_operation(operation_id, reason="test_rollback")
        op = orch.get_operation(operation_id)
        assert op.phase == RestorePhase.ROLLED_BACK
        # active 恢复为旧内容(old_t 回来了)
        async with aiosqlite.connect(str(active_cache)) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='old_t'"
            )
            assert await cursor.fetchone() is not None

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_fail_operation_calls_backend_destroy(self):
        """fail_operation 在提供 backends 时调用 backend.destroy()。"""
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend

        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_orch_fail_")

        active_cache = Path(staging_root) / "active_cache.db"

        registry = BackendRegistry()
        registry.register(
            "sqlite",
            SQLiteRestoreBackend(
                datasource_name="sqlite",
                active_db_path=active_cache,
                schema_initializer=_make_test_schema_initializer(["t1"]),
            ),
        )

        orch = _make_orchestrator(store, staging_root=staging_root, backends=registry)
        operation_id = await orch.start_operation(
            backup_id="backup_test",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        targets = await orch.provision_staging(operation_id)
        # 注入故障触发 fail
        from services.restore_orchestrator import RestorePhase
        orch._fault_hooks["staging_restore.sqlite"] = lambda o, oid, ds: (_ for _ in ()).throw(
            RuntimeError("injected_fault")
        )

        staging_path = Path(targets["sqlite"])
        assert staging_path.exists()
        with pytest.raises(Exception):
            await orch.restore_to_staging(operation_id, "sqlite")

        # fail_operation 应已销毁 staging
        assert not staging_path.exists()
        op = orch.get_operation(operation_id)
        assert op.phase == RestorePhase.FAILED

        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# E. 向后兼容:backends=None 时仍走骨架路径
# ════════════════════════════════════════════════════════════════


class TestBackwardCompatibilitySkeleton:
    """R65 P0-02: backends=None 时 orchestrator 仍走骨架路径(向后兼容)。"""

    @pytest.mark.asyncio
    async def test_skeleton_provision_creates_empty_files(self):
        """backends=None 时 provision_staging 仅 touch 空文件(骨架行为)。"""
        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_skeleton_")
        orch = _make_orchestrator(store, staging_root=staging_root, backends=None)
        operation_id = await orch.start_operation(
            backup_id="b", manifest_digest="a" * 64,
            requested_by="t", payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        targets = await orch.provision_staging(operation_id)
        # 骨架行为:sqlite/relay_sqlite 文件存在但为空
        assert Path(targets["sqlite"]).exists()
        assert Path(targets["sqlite"]).stat().st_size == 0
        assert Path(targets["relay_sqlite"]).exists()
        assert Path(targets["relay_sqlite"]).stat().st_size == 0
        # provision_result.target_type == "skeleton"
        op = orch.get_operation(operation_id)
        assert op.datasource_states["sqlite"]["provision_result"]["target_type"] == "skeleton"
        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_skeleton_restore_to_staging_no_data(self):
        """backends=None 时 restore_to_staging 仅状态变更(无真实写入)。"""
        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_sk_restore_")
        orch = _make_orchestrator(store, staging_root=staging_root, backends=None)
        operation_id = await orch.start_operation(
            backup_id="b", manifest_digest="a" * 64,
            requested_by="t", payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        # 不传 tables_data → 骨架行为
        result = await orch.restore_to_staging(operation_id, "sqlite")
        assert result is True
        op = orch.get_operation(operation_id)
        assert op.datasource_states["sqlite"]["status"] == "restored"
        # 无 restore_result(骨架未写入)
        assert "restore_result" not in op.datasource_states["sqlite"]
        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_skeleton_validate_staging_defaults_ok(self):
        """backends=None + 无 expected_tables 时 validate_staging 默认 ok。"""
        store, _ = await _make_store_with_restore_tables()
        staging_root = tempfile.mkdtemp(prefix="r65_p0_2_sk_val_")
        orch = _make_orchestrator(store, staging_root=staging_root, backends=None)
        operation_id = await orch.start_operation(
            backup_id="b", manifest_digest="a" * 64,
            requested_by="t", payload_digest="d" * 64,
            nonce="n_" + uuid.uuid4().hex[:16],
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        summary = await orch.validate_staging(operation_id)
        assert summary.all_passed is True
        await store.close()
        shutil.rmtree(staging_root, ignore_errors=True)
