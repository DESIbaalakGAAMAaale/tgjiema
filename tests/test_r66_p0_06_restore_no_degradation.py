"""R66 P0-06: RestoreOrchestrator 生产类删除 Optional 降级骨架 — fail-closed 测试。

审计背景(R66 终审报告 P0-06):
    R65 P0-02/P0-03 整改时,RestoreOrchestrator 仍保留生产可达的降级骨架:
      - ``backends: Optional[BackendRegistry] = None`` → 走骨架路径(touch 空文件 /
        占位符 switch),状态机可产生"已恢复、已验证"审计记录但未真实恢复
      - ``approval_authority: Any = None`` / ``mfa_authority: Any = None``
        → 走旧不透明字符串 ID 比较路径(approval_id/mfa_receipt_id 可任意伪造)
      - 内存 ``_operations`` 字典作为 phase 决策权威源,与持久化层可能不一致
    "生产应提供"(production should provide)仅是注释,不是可执行安全边界。

整改方案(R66 P0-06):
    1. 删除生产类的 Optional 依赖:backends / approval_authority / mfa_authority /
       store 任一为 None → 构造时立即 raise AppError(fail-closed)
    2. 兼容骨架行为(touch 空文件 / 占位符 switch / 旧 ID 比较)迁移到 tests-only
       fake 类(``RestoreOrchestratorSkeletonFake``),不在生产模块中保留
    3. ``get_operation`` 每次从权威 store 重载,内存缓存仅作版本化快照(_meta),
       不得作为 phase 决策权威源
    4. ``check_startup_readiness`` 静态校验三个 backend / approval authority /
       mfa authority / nonce ledger / active pointer / fencing store 全部可用

测试覆盖矩阵:
    A. 构造时必需依赖缺失 → fail-closed
       - backends=None
       - approval_authority=None
       - mfa_authority=None
       - store=None
    B. check_startup_readiness
       - 全部依赖就绪 → 通过(无 raise)
       - 任一依赖缺失 → raise AppError
    C. 生产类 AST 扫描:无 ``if X is None: <fallback>`` 降级分支
    D. get_operation 从权威 store 重载(缓存仅作快照)
"""
from __future__ import annotations

import ast
import inspect
import sqlite3
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
    """构造真实 CacheStore + restore_operations 三张表 + command_approvals 表。"""
    from database.cache_store import CacheStore

    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r66_p0_06_test_")
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
    # 创建 command_approvals 表(与 r65_p0_3 测试一致)
    await store._db.execute(
        """
        CREATE TABLE IF NOT EXISTS command_approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id       TEXT NOT NULL,
            approver_id     BIGINT NOT NULL,
            approval_type   TEXT NOT NULL,
            decision        TEXT NOT NULL DEFAULT 'approved',
            request_hash    TEXT NOT NULL,
            mfa_receipt     TEXT NOT NULL,
            permission      TEXT NOT NULL,
            approved_at     TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            consumed_at     TEXT,
            revoked_at      TEXT,
            metadata_json   TEXT,
            UNIQUE(action_id, approver_id, approval_type)
        )
        """
    )
    await store._db.commit()
    return store, db_path


def _make_full_registry(staging_root: Path):
    """构造完整 BackendRegistry(crdb / sqlite / relay_sqlite 三者均注册)。

    crdb 使用 CRDBRestoreBackend + mock crdb_client(测试不实际调用 CRDB 操作,
    仅验证注册表完整性);sqlite / relay_sqlite 使用真实 SQLiteRestoreBackend。
    """
    from services.restore_backends import (
        BackendRegistry,
        CRDBRestoreBackend,
        SQLiteRestoreBackend,
    )

    registry = BackendRegistry()
    # crdb: CRDBRestoreBackend + mock client(测试不实际执行 CRDB 操作)
    registry.register(
        "crdb",
        CRDBRestoreBackend(
            crdb_client=MagicMock(),
            active_schema="public",
        ),
    )
    registry.register(
        "sqlite",
        SQLiteRestoreBackend(
            datasource_name="sqlite",
            active_db_path=staging_root / "active_cache.db",
        ),
    )
    registry.register(
        "relay_sqlite",
        SQLiteRestoreBackend(
            datasource_name="relay_sqlite",
            active_db_path=staging_root / "active_relay.db",
        ),
    )
    return registry


def _make_real_authorities(store):
    """构造真实 ApprovalAuthority + MFAAuthority(基于 store)。"""
    from services.restore_capabilities import ApprovalAuthority, MFAAuthority

    return (
        ApprovalAuthority(store=store),
        MFAAuthority(store=store),
    )


# ════════════════════════════════════════════════════════════════
# A. 构造时必需依赖缺失 → fail-closed
# ════════════════════════════════════════════════════════════════


class TestConstructorRaisesOnMissingDependency:
    """R66 P0-06: 生产类构造时必需依赖缺失 → 立即 raise AppError(fail-closed)。"""

    def test_constructor_raises_on_missing_backend(self):
        """backends=None → RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        store = MagicMock()
        approval_authority = MagicMock()
        mfa_authority = MagicMock()
        with pytest.raises(AppError) as exc_info:
            RestoreOrchestrator(
                store,
                backends=None,
                approval_authority=approval_authority,
                mfa_authority=mfa_authority,
            )
        assert exc_info.value.code == (
            ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
        )

    def test_constructor_raises_on_missing_approval_authority(self):
        """approval_authority=None → RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        store = MagicMock()
        backends = MagicMock()
        mfa_authority = MagicMock()
        with pytest.raises(AppError) as exc_info:
            RestoreOrchestrator(
                store,
                backends=backends,
                approval_authority=None,
                mfa_authority=mfa_authority,
            )
        assert exc_info.value.code == (
            ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
        )

    def test_constructor_raises_on_missing_mfa_authority(self):
        """mfa_authority=None → RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        store = MagicMock()
        backends = MagicMock()
        approval_authority = MagicMock()
        with pytest.raises(AppError) as exc_info:
            RestoreOrchestrator(
                store,
                backends=backends,
                approval_authority=approval_authority,
                mfa_authority=None,
            )
        assert exc_info.value.code == (
            ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
        )

    def test_constructor_raises_on_missing_store(self):
        """store=None → RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        backends = MagicMock()
        approval_authority = MagicMock()
        mfa_authority = MagicMock()
        with pytest.raises(AppError) as exc_info:
            RestoreOrchestrator(
                None,
                backends=backends,
                approval_authority=approval_authority,
                mfa_authority=mfa_authority,
            )
        assert exc_info.value.code == (
            ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
        )

    def test_constructor_signature_no_optional_defaults(self):
        """R66 P0-06: 构造函数签名 — backends/approval_authority/mfa_authority
        不应有 None 默认值(即必需参数)。"""
        from services.restore_orchestrator import RestoreOrchestrator

        sig = inspect.signature(RestoreOrchestrator.__init__)
        for param_name in ("backends", "approval_authority", "mfa_authority"):
            param = sig.parameters[param_name]
            # 必需参数:无默认值(param.default is inspect.Parameter.empty)
            assert param.default is inspect.Parameter.empty, (
                f"参数 {param_name} 必须为必需参数(无默认值),"
                f"当前 default={param.default!r}"
            )


# ════════════════════════════════════════════════════════════════
# B. check_startup_readiness
# ════════════════════════════════════════════════════════════════


class TestCheckStartupReadiness:
    """R66 P0-06: 启动就绪检查 — 验证所有必需依赖可用。"""

    @pytest.mark.asyncio
    async def test_check_startup_readiness_all_dependencies_present(self):
        """所有依赖就绪 → 通过(无 raise)。"""
        from services.restore_orchestrator import RestoreOrchestrator

        store, _ = await _make_store_with_restore_tables()
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_ready_"))
        try:
            backends = _make_full_registry(staging_root)
            approval_authority, mfa_authority = _make_real_authorities(store)

            # 全部依赖就绪 — 不应 raise
            await RestoreOrchestrator.check_startup_readiness(
                store=store,
                backends=backends,
                approval_authority=approval_authority,
                mfa_authority=mfa_authority,
            )
        finally:
            await store.close()
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_check_startup_readiness_missing_backend(self):
        """backends 缺少 crdb → raise RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.restore_backends import BackendRegistry, SQLiteRestoreBackend
        from services.error_codes import AppError, ErrorCodes

        store, _ = await _make_store_with_restore_tables()
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_no_crdb_"))
        try:
            # 只注册 sqlite + relay_sqlite,缺少 crdb
            backends = BackendRegistry()
            backends.register(
                "sqlite",
                SQLiteRestoreBackend(
                    datasource_name="sqlite",
                    active_db_path=staging_root / "active_cache.db",
                ),
            )
            backends.register(
                "relay_sqlite",
                SQLiteRestoreBackend(
                    datasource_name="relay_sqlite",
                    active_db_path=staging_root / "active_relay.db",
                ),
            )
            approval_authority, mfa_authority = _make_real_authorities(store)

            with pytest.raises(AppError) as exc_info:
                await RestoreOrchestrator.check_startup_readiness(
                    store=store,
                    backends=backends,
                    approval_authority=approval_authority,
                    mfa_authority=mfa_authority,
                )
            assert exc_info.value.code == (
                ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
            )
        finally:
            await store.close()
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_check_startup_readiness_missing_authority(self):
        """approval_authority=None → raise。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        store, _ = await _make_store_with_restore_tables()
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_no_auth_"))
        try:
            backends = _make_full_registry(staging_root)
            _, mfa_authority = _make_real_authorities(store)

            with pytest.raises(AppError) as exc_info:
                await RestoreOrchestrator.check_startup_readiness(
                    store=store,
                    backends=backends,
                    approval_authority=None,
                    mfa_authority=mfa_authority,
                )
            assert exc_info.value.code == (
                ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
            )
        finally:
            await store.close()
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_check_startup_readiness_missing_nonce_ledger(self):
        """store 缺少 nonce ledger 方法 → raise。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        # 构造一个 mock store,有 _db 但无 reserve_capability_nonce 方法
        store = MagicMock()
        store._db = MagicMock()
        # 删除 nonce ledger 方法
        for method in (
            "reserve_capability_nonce",
            "consume_capability_nonce",
            "fail_capability_nonce",
        ):
            if hasattr(store, method):
                delattr(store, method)
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_no_nonce_"))
        try:
            backends = _make_full_registry(staging_root)
            approval_authority, mfa_authority = _make_real_authorities(store)

            with pytest.raises(AppError) as exc_info:
                await RestoreOrchestrator.check_startup_readiness(
                    store=store,
                    backends=backends,
                    approval_authority=approval_authority,
                    mfa_authority=mfa_authority,
                )
            assert exc_info.value.code == (
                ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING
            )
        finally:
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# C. AST 扫描:生产类无 Optional 降级分支
# ════════════════════════════════════════════════════════════════


class TestNoOptionalBranchesInProductionClass:
    """R66 P0-06: 生产类 RestoreOrchestrator 不得包含任何
    ``if X is None: <fallback>`` 形式的降级分支(生产可达骨架)。"""

    def test_no_optional_branches_in_production_class(self):
        """AST 扫描 restore_orchestrator.py:不得出现
        ``if self._backends is None`` / ``if self._backends is not None``
        / ``if self._approval_authority is None`` /
        ``if self._approval_authority is not None`` /
        ``if self._mfa_authority is None`` /
        ``if self._mfa_authority is not None`` 形式的分支判断。
        """
        source_path = REPO_ROOT / "services" / "restore_orchestrator.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        # 收集 RestoreOrchestrator 类(跳过测试 fake / 内嵌测试类)
        target_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RestoreOrchestrator":
                target_class = node
                break
        assert target_class is not None, "未找到 RestoreOrchestrator 类"

        # 禁止的属性访问模式(以 self._ 开头)
        forbidden_attrs = {
            "_backends",
            "_approval_authority",
            "_mfa_authority",
        }

        violations: list[str] = []

        def _check_is_none_test(test_node: ast.expr) -> bool:
            """检测 ``self._X is None`` / ``self._X is not None`` 模式。"""
            if not isinstance(test_node, ast.Compare):
                return False
            if len(test_node.ops) != 1:
                return False
            op = test_node.ops[0]
            if not isinstance(op, (ast.Is, ast.IsNot)):
                return False
            # 左边应是 self._X
            left = test_node.left
            if not isinstance(left, ast.Attribute):
                return False
            if not isinstance(left.value, ast.Name):
                return False
            if left.value.id != "self":
                return False
            return left.attr in forbidden_attrs

        # 遍历类内所有 If 节点,检查 test 是否为禁止模式
        for node in ast.walk(target_class):
            if isinstance(node, ast.If):
                # 直接 if self._X is None
                if _check_is_none_test(node.test):
                    violations.append(
                        f"line {node.lineno}: 发现 "
                        f"``if self.{ast.dump(node.test)}:`` 降级分支"
                    )
                # if A is not None and B is not None: 组合形式
                if isinstance(node.test, ast.BoolOp):
                    for val in node.test.values:
                        if _check_is_none_test(val):
                            violations.append(
                                f"line {node.lineno}: 发现组合条件中包含 "
                                f"``self.{ast.dump(val)}`` 降级分支"
                            )

        assert not violations, (
            "R66 P0-06: RestoreOrchestrator 生产类仍存在 Optional 降级分支:\n  - "
            + "\n  - ".join(violations)
        )

    def test_no_optional_type_hint_on_required_deps(self):
        """构造函数签名:backends/approval_authority/mfa_authority 不得为 Optional。"""
        from services.restore_orchestrator import RestoreOrchestrator

        sig = inspect.signature(RestoreOrchestrator.__init__)
        for param_name in ("backends", "approval_authority", "mfa_authority"):
            param = sig.parameters[param_name]
            ann = str(param.annotation)
            # 不得出现 Optional[...] 或 | None
            assert "Optional" not in ann, (
                f"参数 {param_name} 注解 {ann!r} 含 Optional — "
                f"R66 P0-06 已要求生产类删除 Optional 依赖"
            )
            # 允许 Any / BackendRegistry 等,但不允许 | None
            assert "| None" not in ann, (
                f"参数 {param_name} 注解 {ann!r} 含 | None — "
                f"R66 P0-06 已要求生产类删除 Optional 依赖"
            )

    def test_no_backend_registry_skeleton_in_production_module(self):
        """生产模块不得保留 RestoreOrchestratorSkeletonFake 或类似 fake 类。"""
        source_path = REPO_ROOT / "services" / "restore_orchestrator.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        forbidden_class_names = {
            "RestoreOrchestratorSkeletonFake",
            "SkeletonRestoreOrchestrator",
            "RestoreOrchestratorFake",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert node.name not in forbidden_class_names, (
                    f"生产模块不得包含 fake 类 {node.name!r} — "
                    f"R66 P0-06 要求 fake 类仅存在于 tests/"
                )


# ════════════════════════════════════════════════════════════════
# D. get_operation 从权威 store 重载(缓存仅作快照)
# ════════════════════════════════════════════════════════════════


class TestGetOperationReloadsFromStore:
    """R66 P0-06: get_operation 每次从权威 store 重载,缓存仅作版本化快照。"""

    @pytest.mark.asyncio
    async def test_get_operation_reloads_from_store(self):
        """get_operation 返回持久化层最新状态,而非内存缓存。"""
        from services.restore_orchestrator import (
            RestoreOrchestrator,
            RestorePhase,
        )

        store, _ = await _make_store_with_restore_tables()
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_reload_"))
        try:
            backends = _make_full_registry(staging_root)
            approval_authority, mfa_authority = _make_real_authorities(store)

            orch = RestoreOrchestrator(
                store,
                staging_root=staging_root,
                backends=backends,
                approval_authority=approval_authority,
                mfa_authority=mfa_authority,
            )
            operation_id = await orch.start_operation(
                backup_id="b", manifest_digest="a" * 64,
                requested_by="tester", payload_digest="d" * 64,
                nonce="n_" + uuid.uuid4().hex[:16],
            )
            # 直接通过持久化层修改 phase(模拟另一个 orchestrator 实例的写入)
            await store._db.execute(
                "UPDATE restore_operations SET phase = ? WHERE operation_id = ?",
                (RestorePhase.STAGING_PROVISION.value, operation_id),
            )
            await store._db.commit()

            # get_operation 应从持久化层 reload,反映最新 phase
            op = await orch.get_operation(operation_id)
            assert op.phase == RestorePhase.STAGING_PROVISION, (
                "get_operation 应从持久化层 reload — 缓存不得作为 phase 决策权威源"
            )
        finally:
            await store.close()
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_get_operation_not_found_raises(self):
        """get_operation 查询不存在的 operation_id → raise AppError。"""
        from services.restore_orchestrator import RestoreOrchestrator
        from services.error_codes import AppError, ErrorCodes

        store, _ = await _make_store_with_restore_tables()
        staging_root = Path(tempfile.mkdtemp(prefix="r66_p0_06_notfound_"))
        try:
            backends = _make_full_registry(staging_root)
            approval_authority, mfa_authority = _make_real_authorities(store)

            orch = RestoreOrchestrator(
                store,
                staging_root=staging_root,
                backends=backends,
                approval_authority=approval_authority,
                mfa_authority=mfa_authority,
            )
            with pytest.raises(AppError) as exc_info:
                await orch.get_operation("nonexistent_op_id")
            # 应为 phase transition invalid(operation_not_found)
            assert exc_info.value.code == ErrorCodes.RESTORE_PHASE_TRANSITION_INVALID
        finally:
            await store.close()
            import shutil
            shutil.rmtree(staging_root, ignore_errors=True)
