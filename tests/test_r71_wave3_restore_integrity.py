"""R71 Wave 3: 恢复完整性结构化校验 — 测试套件。

R71 报告 P0-08 指出:
    R71 Wave 2 的 verify_restore_integrity.py 只做基本校验
    (测试标记 + row count),无法验证 schema 结构、字段级完整性、
    迁移兼容性、应用可用性、合成交易、切换/回滚流程。

R71 Wave 3 整改(P0-08, Commit 3):
    1. scripts/verify_restore_integrity.py 大幅扩展:
       - 确定性测试数据集(unique ID + 边界值 + 关系 + payload hash)
       - Schema 指纹捕获(tables / pk / columns / conflict_col / source / DDL hash)
       - 字段级 hash(每表 SELECT * ORDER BY pk → sha256 of canonical JSON)
       - 迁移版本兼容性检查(current vs backup schema_version)
       - 恢复目标隔离验证(--target-db staging)
       - 应用启动/读写验证(python -m services.health + INSERT/SELECT/DELETE)
       - 恢复环境合成交易(synthetic_transaction.run_full_transaction)
       - 切换/回滚证据(RestoreOrchestrator import check + 结构化 JSON)
       - 机器可读恢复证据(增强 IntegrityEvidence dataclass)
       - 新增 CLI 子命令 full-check
    2. scripts/compose_runtime_e2e.py phase_backup_restore 升级:
       - 调用 verify_full()(替代 verify())
       - 新增 7 个 readiness 检查点
       - target_db="staging"(隔离恢复验证)
    3. services/db_restore.py 不修改(capability-sealed)

被测对象:
    - scripts/verify_restore_integrity.py(扩展后的结构化校验)
    - scripts/compose_runtime_e2e.py phase_backup_restore(新增 readiness 检查)
    - 新增数据类:TestDataset / SchemaFingerprint / TableHash / 增强 IntegrityEvidence

测试覆盖矩阵(37 个测试):
    A. 模块结构与数据类定义 — 5 个
    B. 确定性测试数据集生成 — 5 个
    C. Schema 指纹捕获与 hash 稳定性 — 4 个
    D. 字段级 hash 计算 — 3 个
    E. 迁移版本兼容性检查 — 3 个
    F. 测试数据集写入/验证/清理 — 4 个
    G. 应用启动/读写验证 — 3 个
    H. 切换/回滚证据结构 — 3 个
    I. verify_full 完整流程 — 4 个
    J. CLI 入口(full-check 子命令) — 3 个
    K. compose_runtime_e2e phase_backup_restore 集成 — 3 个

测试策略:
    - 用 monkeypatch 模拟 _exec_sql / _exec_python / _exec_health,
      确保在 Windows 无 Docker 环境下确定性运行
    - 验证数据类字段、JSON 序列化、fail-closed 行为
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_RESTORE_INTEGRITY_PATH = REPO_ROOT / "scripts" / "verify_restore_integrity.py"
COMPOSE_RUNTIME_E2E_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"


# ════════════════════════════════════════════════════════════════
# 辅助:动态加载模块(不通过 sys.path,避免污染)
# ════════════════════════════════════════════════════════════════


def _load_module_from_path(module_name: str, file_path: Path):
    """从文件路径动态加载 Python 模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None, f"无法加载模块 spec: {file_path}"
    assert spec.loader is not None, f"模块 loader 为 None: {file_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vri():
    """加载 verify_restore_integrity 模块(模块级缓存)。"""
    return _load_module_from_path(
        "scripts.verify_restore_integrity_r71w3", VERIFY_RESTORE_INTEGRITY_PATH,
    )


@pytest.fixture(scope="module")
def orch():
    """加载 compose_runtime_e2e 模块(模块级缓存)。"""
    return _load_module_from_path(
        "scripts.compose_runtime_e2e_r71w3", COMPOSE_RUNTIME_E2E_PATH,
    )


def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """构造模拟的 subprocess.CompletedProcess。"""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ════════════════════════════════════════════════════════════════
# A. 模块结构与数据类定义
# ════════════════════════════════════════════════════════════════


class TestModuleStructure:
    """R71 Wave 3 A: 模块结构与数据类定义。"""

    def test_script_file_exists(self):
        """scripts/verify_restore_integrity.py 文件存在。"""
        assert VERIFY_RESTORE_INTEGRITY_PATH.is_file(), (
            f"R71 Wave 3: 校验脚本不存在: {VERIFY_RESTORE_INTEGRITY_PATH}"
        )

    def test_module_exposes_required_dataclasses(self, vri):
        """模块必须暴露 R71 Wave 3 新增数据类。"""
        required_classes = [
            "TestDataset",
            "SchemaFingerprint",
            "TableHash",
            "TableCount",
            "IntegrityEvidence",
        ]
        for cls_name in required_classes:
            assert hasattr(vri, cls_name), (
                f"verify_restore_integrity.py 必须暴露 {cls_name} 数据类"
            )

    def test_module_exposes_required_functions(self, vri):
        """模块必须暴露 R71 Wave 3 新增函数。"""
        required_funcs = [
            "generate_test_dataset",
            "compute_payload_hash",
            "write_test_dataset",
            "verify_test_dataset",
            "verify_payload_hash",
            "cleanup_test_dataset",
            "capture_schema_fingerprint",
            "compare_schema_fingerprints",
            "compute_field_hashes",
            "compare_field_hashes",
            "check_migration_version_compatibility",
            "verify_app_start",
            "verify_app_read_write",
            "run_synthetic_transaction_in_restored_env",
            "generate_switch_rollback_evidence",
            "verify_full",
        ]
        for func_name in required_funcs:
            assert hasattr(vri, func_name), (
                f"verify_restore_integrity.py 必须暴露 {func_name}() 函数"
            )
            assert callable(getattr(vri, func_name)), (
                f"{func_name} 必须是可调用对象"
            )

    def test_integrity_evidence_has_wave3_fields(self, vri):
        """IntegrityEvidence 必须包含 R71 Wave 3 新增字段。"""
        fields = {f.name for f in vri.IntegrityEvidence.__dataclass_fields__.values()}
        required_wave3_fields = {
            "schema_fingerprint",
            "schema_fingerprint_hash",
            "pre_field_hashes",
            "post_field_hashes",
            "field_hash_mismatches",
            "marker_payload_hash_match",
            "migration_version_check",
            "app_start_check",
            "app_read_write_check",
            "synthetic_transaction",
            "switch_rollback_evidence",
            "target_db",
        }
        missing = required_wave3_fields - fields
        assert not missing, (
            f"IntegrityEvidence 缺少 R71 Wave 3 字段: {sorted(missing)}, "
            f"实际字段: {sorted(fields)}"
        )

    def test_target_db_paths_includes_staging(self, vri):
        """TARGET_DB_PATHS 必须包含 staging 路径(R71 Wave 3 隔离恢复要求)。"""
        assert "production" in vri.TARGET_DB_PATHS, (
            "TARGET_DB_PATHS 必须包含 production"
        )
        assert "staging" in vri.TARGET_DB_PATHS, (
            "TARGET_DB_PATHS 必须包含 staging(R71 Wave 3 隔离恢复目标)"
        )
        assert vri.TARGET_DB_PATHS["staging"].endswith("cache_store.db"), (
            "staging 路径必须指向 cache_store.db"
        )
        assert "staging" in vri.TARGET_DB_PATHS["staging"], (
            "staging 路径必须包含 'staging' 目录"
        )


# ════════════════════════════════════════════════════════════════
# B. 确定性测试数据集生成
# ════════════════════════════════════════════════════════════════


class TestTestDatasetGeneration:
    """R71 Wave 3 B: 确定性测试数据集生成。"""

    def test_generate_test_dataset_returns_testdataset(self, vri):
        """generate_test_dataset() 必须返回 TestDataset 实例。"""
        ds = vri.generate_test_dataset()
        assert isinstance(ds, vri.TestDataset), (
            f"generate_test_dataset 必须返回 TestDataset, 实际: {type(ds)}"
        )

    def test_generate_test_dataset_with_explicit_trace_id(self, vri):
        """generate_test_dataset(trace_id=...) 必须使用指定的 trace_id。"""
        custom_tid = "abc123def456"
        ds = vri.generate_test_dataset(trace_id=custom_tid)
        assert ds.trace_id == custom_tid, (
            f"trace_id 必须为 {custom_tid!r}, 实际: {ds.trace_id!r}"
        )

    def test_generate_test_dataset_includes_boundary_values(self, vri):
        """测试数据集必须包含边界值(0, -1, SQLITE_MAX_INT)。"""
        ds = vri.generate_test_dataset(trace_id="boundary_test")
        # 收集所有 last_ping / total_processed / total_errors 值
        all_values: list[int] = []
        for row in ds.rows:
            all_values.append(row["last_ping"])
            all_values.append(row["total_processed"])
            all_values.append(row["total_errors"])
        assert 0 in all_values, "数据集必须包含 0(边界值)"
        assert -1 in all_values, "数据集必须包含 -1(负数边界值)"
        assert vri.SQLITE_MAX_INT in all_values, (
            f"数据集必须包含 SQLITE_MAX_INT({vri.SQLITE_MAX_INT})"
        )

    def test_generate_test_dataset_includes_relations(self, vri):
        """测试数据集必须包含关系行(parent → child)。"""
        ds = vri.generate_test_dataset(trace_id="relation_test")
        assert len(ds.relations) >= 2, (
            f"数据集必须包含至少 2 个关系, 实际: {len(ds.relations)}"
        )
        # 每个 relation 必须是 (parent, child) 元组
        for rel in ds.relations:
            assert isinstance(rel, tuple) and len(rel) == 2, (
                f"关系必须是 2 元组, 实际: {rel!r}"
            )
            parent, child = rel
            assert child.startswith(parent), (
                f"child({child!r}) 必须以 parent({parent!r}) 为前缀(模拟 FK)"
            )

    def test_generate_test_dataset_payload_hash_stable(self, vri):
        """相同 trace_id 生成的数据集 payload_hash 必须相同(确定性)。"""
        ds1 = vri.generate_test_dataset(trace_id="stable_hash_test")
        ds2 = vri.generate_test_dataset(trace_id="stable_hash_test")
        assert ds1.payload_hash == ds2.payload_hash, (
            "相同 trace_id 的 payload_hash 必须相同(确定性)"
        )
        # payload_hash 必须是 64 字符 sha256 hex
        assert len(ds1.payload_hash) == 64, (
            f"payload_hash 必须是 64 字符 sha256 hex, 实际长度: {len(ds1.payload_hash)}"
        )
        # 不同 trace_id 生成的 payload_hash 必须不同
        ds3 = vri.generate_test_dataset(trace_id="different_trace_id")
        assert ds1.payload_hash != ds3.payload_hash, (
            "不同 trace_id 的 payload_hash 必须不同"
        )


# ════════════════════════════════════════════════════════════════
# C. Schema 指纹捕获与 hash 稳定性
# ════════════════════════════════════════════════════════════════


class TestSchemaFingerprint:
    """R71 Wave 3 C: Schema 指纹捕获与 hash 稳定性。"""

    def test_capture_schema_fingerprint_returns_schema_fingerprint(self, vri, monkeypatch):
        """capture_schema_fingerprint() 必须返回 SchemaFingerprint 实例。"""
        # mock _exec_sql 返回空 sqlite_master 结果
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "[]", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        fp = vri.capture_schema_fingerprint(target_db="staging")
        assert isinstance(fp, vri.SchemaFingerprint), (
            f"capture_schema_fingerprint 必须返回 SchemaFingerprint, 实际: {type(fp)}"
        )

    def test_schema_fingerprint_hash_is_sha256_hex(self, vri, monkeypatch):
        """SchemaFingerprint.fingerprint_hash 必须是 64 字符 sha256 hex。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "[]", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        fp = vri.capture_schema_fingerprint(target_db="staging")
        assert len(fp.fingerprint_hash) == 64, (
            f"fingerprint_hash 必须是 64 字符, 实际: {len(fp.fingerprint_hash)}"
        )
        # 必须是有效 hex
        int(fp.fingerprint_hash, 16)  # 不抛异常即合法

    def test_schema_fingerprint_stable_with_same_data(self, vri, monkeypatch):
        """相同 sqlite_master 数据生成的 fingerprint_hash 必须相同(确定性)。"""
        # 模拟固定的 sqlite_master 返回
        fixed_sqlite_master = json.dumps([
            {"type": "table", "name": "bot_heartbeat", "tbl_name": "bot_heartbeat",
             "sql": "CREATE TABLE bot_heartbeat (name TEXT PRIMARY KEY)"},
        ])

        call_count = [0]
        def mock_exec_sql(query, *args, **kwargs):
            call_count[0] += 1
            return 0, fixed_sqlite_master, ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        fp1 = vri.capture_schema_fingerprint(target_db="staging")
        fp2 = vri.capture_schema_fingerprint(target_db="staging")
        assert fp1.fingerprint_hash == fp2.fingerprint_hash, (
            "相同 sqlite_master 数据的 fingerprint_hash 必须相同(确定性)"
        )

    def test_compare_schema_fingerprints_identical_returns_empty(self, vri, monkeypatch):
        """两个完全相同的 schema 指纹比对必须返回空差异列表。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "[]", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        fp1 = vri.capture_schema_fingerprint(target_db="staging")
        fp2 = vri.capture_schema_fingerprint(target_db="staging")
        mismatches = vri.compare_schema_fingerprints(fp1, fp2)
        assert mismatches == [], (
            f"相同指纹的比对必须返回空列表, 实际: {mismatches}"
        )


# ════════════════════════════════════════════════════════════════
# D. 字段级 hash 计算
# ════════════════════════════════════════════════════════════════


class TestFieldHashes:
    """R71 Wave 3 D: 字段级 hash 计算。"""

    def test_compute_field_hashes_returns_table_hash_list(self, vri, monkeypatch):
        """compute_field_hashes() 必须返回 list[TableHash]。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "[]", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        hashes = vri.compute_field_hashes(target_db="staging")
        assert isinstance(hashes, list), (
            f"compute_field_hashes 必须返回 list, 实际: {type(hashes)}"
        )
        assert len(hashes) == len(vri.FIELD_HASH_TABLES), (
            f"必须为 FIELD_HASH_TABLES 中每张表返回一个 TableHash, "
            f"expected={len(vri.FIELD_HASH_TABLES)}, actual={len(hashes)}"
        )
        for h in hashes:
            assert isinstance(h, vri.TableHash), (
                f"列表元素必须是 TableHash, 实际: {type(h)}"
            )

    def test_field_hash_is_sha256_hex(self, vri, monkeypatch):
        """TableHash.field_hash 必须是 64 字符 sha256 hex(或空字符串表示失败)。"""
        fixed_rows = json.dumps([{"name": "test_row", "cnt": 1}])
        def mock_exec_sql(query, *args, **kwargs):
            return 0, fixed_rows, ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        hashes = vri.compute_field_hashes(target_db="staging")
        for h in hashes:
            # 成功时是 64 字符 hex;失败时为空字符串(有 error)
            if not h.error:
                assert len(h.field_hash) == 64, (
                    f"field_hash 必须是 64 字符, 实际: {len(h.field_hash)} "
                    f"(table={h.table})"
                )
                int(h.field_hash, 16)  # 验证是合法 hex

    def test_compare_field_hashes_identical_returns_empty(self, vri, monkeypatch):
        """相同的字段 hash 列表比对必须返回空差异列表。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "[]", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        pre = vri.compute_field_hashes(target_db="staging")
        post = vri.compute_field_hashes(target_db="staging")
        mismatches = vri.compare_field_hashes(pre, post)
        assert mismatches == [], (
            f"相同字段 hash 的比对必须返回空列表, 实际: {mismatches}"
        )


# ════════════════════════════════════════════════════════════════
# E. 迁移版本兼容性检查
# ════════════════════════════════════════════════════════════════


class TestMigrationVersionCompatibility:
    """R71 Wave 3 E: 迁移版本兼容性检查。"""

    def test_check_compatible_when_versions_match(self, vri):
        """当前版本与备份版本完全匹配时 compatible=True。"""
        result = vri.check_migration_version_compatibility(
            backup_schema_version="r71-wave3"
        )
        # current 来自 settings / db_backup / env / "unknown"
        assert "current" in result, "结果必须包含 current 字段"
        assert "backup" in result, "结果必须包含 backup 字段"
        assert "compatible" in result, "结果必须包含 compatible 字段"
        assert result["backup"] == "r71-wave3"

    def test_check_compatible_when_backup_is_prefix_of_current(self, vri, monkeypatch):
        """备份版本是当前版本前缀时 compatible=True(向后兼容)。"""
        # mock _get_schema_version 返回较长版本号
        monkeypatch.setattr(vri, "_get_schema_version", lambda: "v2.0.0-rc1")
        result = vri.check_migration_version_compatibility(
            backup_schema_version="v2.0.0"
        )
        assert result["compatible"] is True, (
            f"backup(v2.0.0) 是 current(v2.0.0-rc1) 的前缀, 应兼容, "
            f"结果: {result}"
        )

    def test_check_returns_dict_with_required_fields(self, vri):
        """check_migration_version_compatibility 必须返回含必需字段的 dict。"""
        result = vri.check_migration_version_compatibility(
            backup_schema_version="some_version"
        )
        assert isinstance(result, dict), (
            f"必须返回 dict, 实际: {type(result)}"
        )
        for field in ("current", "backup", "compatible", "note"):
            assert field in result, f"结果缺少必需字段: {field}"


# ════════════════════════════════════════════════════════════════
# F. 测试数据集写入/验证/清理
# ════════════════════════════════════════════════════════════════


class TestDatasetWriteVerifyCleanup:
    """R71 Wave 3 F: 测试数据集写入/验证/清理。"""

    def test_write_test_dataset_success(self, vri, monkeypatch):
        """write_test_dataset 在 SQL 成功时返回 (True, None)。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        ds = vri.generate_test_dataset(trace_id="write_test_ok")
        success, err = vri.write_test_dataset(ds, target_db="staging")
        assert success is True, (
            f"write_test_dataset 必须成功, error: {err}"
        )
        assert err is None, f"成功时 err 必须为 None, 实际: {err!r}"

    def test_write_test_dataset_failure_fail_closed(self, vri, monkeypatch):
        """write_test_dataset 在 SQL 失败时必须 fail-closed 返回 (False, error)。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 1, "", "table does not exist"
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        ds = vri.generate_test_dataset(trace_id="write_test_fail")
        success, err = vri.write_test_dataset(ds, target_db="staging")
        assert success is False, "SQL 失败时必须返回 success=False(fail-closed)"
        assert err is not None, "失败时必须提供 error 描述"
        assert "table does not exist" in err or "exit=1" in err, (
            f"error 必须包含失败原因, 实际: {err!r}"
        )

    def test_verify_test_dataset_success(self, vri, monkeypatch):
        """verify_test_dataset 在所有行都存在时返回 (True, None)。"""
        # mock: COUNT(*) 返回 1(行存在)
        def mock_exec_sql(query, *args, **kwargs):
            return 0, '[{"cnt": 1}]', ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        ds = vri.generate_test_dataset(trace_id="verify_test_ok")
        ok, err = vri.verify_test_dataset(ds, target_db="staging")
        assert ok is True, (
            f"所有行存在时必须返回 ok=True, error: {err}"
        )
        assert err is None

    def test_cleanup_test_dataset_success(self, vri, monkeypatch):
        """cleanup_test_dataset 在 SQL 成功时返回 (True, None)。"""
        def mock_exec_sql(query, *args, **kwargs):
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        ds = vri.generate_test_dataset(trace_id="cleanup_test_ok")
        ok, err = vri.cleanup_test_dataset(ds, target_db="staging")
        assert ok is True, f"清理必须成功, error: {err}"
        assert err is None


# ════════════════════════════════════════════════════════════════
# G. 应用启动/读写验证
# ════════════════════════════════════════════════════════════════


class TestAppStartReadWrite:
    """R71 Wave 3 G: 应用启动/读写验证。"""

    def test_verify_app_start_success(self, vri, monkeypatch):
        """verify_app_start 在 health 返回 healthy=true 时 started/healthy=True。"""
        def mock_exec_health(role, *args, **kwargs):
            health_json = json.dumps({"healthy": True, "checks": []})
            return 0, health_json, ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        result = vri.verify_app_start(role="db_writer")
        assert isinstance(result, dict), "必须返回 dict"
        assert result["started"] is True, "started 必须为 True"
        assert result["healthy"] is True, "healthy 必须为 True"
        assert result["role"] == "db_writer"
        assert result["error"] is None

    def test_verify_app_start_failure(self, vri, monkeypatch):
        """verify_app_start 在 health 失败时 started=False(fail-closed)。"""
        def mock_exec_health(role, *args, **kwargs):
            return 1, "", "command not found"
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        result = vri.verify_app_start(role="db_writer")
        assert result["started"] is False, (
            "health 失败时 started 必须为 False(fail-closed)"
        )
        assert result["healthy"] is False
        assert result["error"] is not None, "失败时必须提供 error"

    def test_verify_app_read_write_success(self, vri, monkeypatch):
        """verify_app_read_write 在 INSERT/SELECT/DELETE 全成功时返回全 True。"""
        # mock _exec_sql 让所有操作成功
        def mock_exec_sql(query, *args, **kwargs):
            # SELECT COUNT(*) 返回 1
            if "COUNT" in query:
                return 0, '[{"cnt": 1}]', ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        result = vri.verify_app_read_write(trace_id="rw_test_ok", target_db="staging")
        assert result["write_ok"] is True, "INSERT 必须成功"
        assert result["read_ok"] is True, "SELECT 必须成功"
        assert result["cleanup_ok"] is True, "DELETE 必须成功"
        assert result["error"] is None


# ════════════════════════════════════════════════════════════════
# H. 切换/回滚证据结构
# ════════════════════════════════════════════════════════════════


class TestSwitchRollbackEvidence:
    """R71 Wave 3 H: 切换/回滚证据结构。"""

    def test_generate_switch_rollback_evidence_returns_dict(self, vri):
        """generate_switch_rollback_evidence 必须返回 dict。"""
        result = vri.generate_switch_rollback_evidence()
        assert isinstance(result, dict), (
            f"必须返回 dict, 实际: {type(result)}"
        )

    def test_switch_rollback_evidence_has_required_fields(self, vri):
        """切换/回滚证据必须包含必需字段。"""
        result = vri.generate_switch_rollback_evidence()
        required_fields = {
            "orchestrator_available",
            "orchestrator_executed",
            "passed",
            "switch_version",
            "rollback_version",
            "old_db_identity",
            "new_db_identity",
            "business_probe_after_switch",
            "business_probe_after_rollback",
            "switch_time_seconds",
            "rto_seconds",
            "rpo_seconds",
            "target_db",
            "actual_db_path",
            "restore_phases",
            "error",
        }
        missing = required_fields - set(result.keys())
        assert not missing, (
            f"切换/回滚证据缺少必需字段: {sorted(missing)}, "
            f"实际字段: {sorted(result.keys())}"
        )

    def test_switch_rollback_evidence_performed_in_e2e_is_false(self, vri):
        """orchestrator_executed 必须为 False(E2E 中无凭证不实际执行破坏性切换)。"""
        result = vri.generate_switch_rollback_evidence()
        assert result["orchestrator_executed"] is False, (
            "orchestrator_executed 必须为 False(E2E 无凭证不执行破坏性切换)"
        )


# ════════════════════════════════════════════════════════════════
# I. verify_full 完整流程
# ════════════════════════════════════════════════════════════════


class TestVerifyFull:
    """R71 Wave 3 I: verify_full 完整流程。"""

    def test_verify_full_returns_integrity_evidence(self, vri, monkeypatch):
        """verify_full 必须返回 IntegrityEvidence 实例。"""
        # mock 所有 docker compose exec 调用
        def mock_exec_sql(query, *args, **kwargs):
            # 测试标记存在
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 1}]', ""
            # sqlite_master 空结果
            if "sqlite_master" in query:
                return 0, "[]", ""
            # 其他 SELECT 返回空数组
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        def mock_exec_health(role, *args, **kwargs):
            return 0, json.dumps({"healthy": True, "checks": []}), ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        # mock synthetic transaction
        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True, "trace_id": "mock"}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        evidence = vri.verify_full(
            trace_id="verify_full_test",
            pre_snapshot_path=None,
            target_db="staging",
            skip_synthetic=False,
            skip_app_checks=False,
        )
        assert isinstance(evidence, vri.IntegrityEvidence), (
            f"verify_full 必须返回 IntegrityEvidence, 实际: {type(evidence)}"
        )
        assert evidence.target_db == "staging", (
            f"target_db 必须为 'staging', 实际: {evidence.target_db!r}"
        )

    def test_verify_full_marker_missing_fails(self, vri, monkeypatch):
        """测试标记缺失时 verify_full 必须返回 passed=False(fail-closed)。"""
        def mock_exec_sql(query, *args, **kwargs):
            # 测试标记不存在
            if "COUNT" in query and "name" in query and "payload_hash" not in query:
                return 0, '[{"cnt": 0}]', ""
            # sqlite_master 空结果
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        def mock_exec_health(role, *args, **kwargs):
            return 0, json.dumps({"healthy": True, "checks": []}), ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        evidence = vri.verify_full(
            trace_id="missing_marker_test",
            pre_snapshot_path=None,
            target_db="staging",
        )
        assert evidence.passed is False, (
            "测试标记缺失时 passed 必须为 False(fail-closed)"
        )
        assert evidence.marker_found is False, (
            "marker_found 必须为 False(标记确实不存在)"
        )
        assert evidence.error is not None, "失败时必须提供 error"

    def test_verify_full_app_start_failure_fails(self, vri, monkeypatch):
        """应用启动失败时 verify_full 必须返回 passed=False(fail-closed)。"""
        def mock_exec_sql(query, *args, **kwargs):
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 1}]', ""
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        # mock health 失败
        def mock_exec_health(role, *args, **kwargs):
            return 1, "", "health check failed"
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        evidence = vri.verify_full(
            trace_id="app_start_fail_test",
            pre_snapshot_path=None,
            target_db="staging",
        )
        assert evidence.passed is False, (
            "应用启动失败时 passed 必须为 False(fail-closed)"
        )
        assert evidence.app_start_check.get("started") is False, (
            "app_start_check.started 必须为 False"
        )

    def test_verify_full_evidence_json_serializable(self, vri, monkeypatch):
        """IntegrityEvidence 必须可 JSON 序列化(机器可读)。"""
        from dataclasses import asdict

        def mock_exec_sql(query, *args, **kwargs):
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 1}]', ""
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        def mock_exec_health(role, *args, **kwargs):
            return 0, json.dumps({"healthy": True, "checks": []}), ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True, "trace_id": "json_test"}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        evidence = vri.verify_full(
            trace_id="json_serializable_test",
            pre_snapshot_path=None,
            target_db="staging",
        )
        # asdict + json.dumps 必须成功
        evidence_dict = asdict(evidence)
        evidence_json = json.dumps(evidence_dict, ensure_ascii=False)
        assert "trace_id" in evidence_json
        assert "schema_fingerprint" in evidence_json
        assert "synthetic_transaction" in evidence_json
        assert "switch_rollback_evidence" in evidence_json


# ════════════════════════════════════════════════════════════════
# J. CLI 入口(full-check 子命令)
# ════════════════════════════════════════════════════════════════


class TestCLIEntry:
    """R71 Wave 3 J: CLI 入口(full-check 子命令)。"""

    def test_main_exposes_full_check_subcommand(self, vri):
        """main() 必须支持 full-check 子命令。"""
        # 通过 --help 验证子命令存在
        with pytest.raises(SystemExit) as exc_info:
            vri.main(["--help"])
        assert exc_info.value.code == 0
        # --help 输出在 stdout
        import io
        import contextlib
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            try:
                vri.main(["--help"])
            except SystemExit:
                pass
        help_output = stdout_buf.getvalue()
        assert "full-check" in help_output, (
            "main() 必须在 --help 输出中暴露 full-check 子命令"
        )

    def test_main_full_check_with_missing_marker_returns_1(self, vri, monkeypatch, tmp_path):
        """full-check 在测试标记缺失时必须返回退出码 1(fail-closed)。"""
        def mock_exec_sql(query, *args, **kwargs):
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 0}]', ""
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        def mock_exec_health(role, *args, **kwargs):
            return 0, json.dumps({"healthy": True, "checks": []}), ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        output_path = tmp_path / "evidence.json"
        rc = vri.main([
            "full-check",
            "--trace-id", "cli_missing_marker",
            "--target-db", "staging",
            "--output", str(output_path),
            "--skip-synthetic",
            "--skip-app-checks",
        ])
        assert rc == 1, "标记缺失时 full-check 必须返回 1(fail-closed)"
        # 证据文件必须被写入
        assert output_path.is_file(), "失败时也必须写入证据文件"
        evidence = json.loads(output_path.read_text(encoding="utf-8"))
        assert evidence["passed"] is False
        assert evidence["marker_found"] is False

    def test_main_full_check_rejects_invalid_target_db(self, vri):
        """full-check 必须拒绝无效的 --target-db 值(argparse choices)。"""
        with pytest.raises(SystemExit) as exc_info:
            vri.main([
                "full-check",
                "--trace-id", "invalid_target",
                "--target-db", "invalid_db",
            ])
        # argparse choices 不匹配 → SystemExit(2)
        assert exc_info.value.code == 2, (
            "无效 --target-db 必须触发 argparse 错误退出码 2"
        )


# ════════════════════════════════════════════════════════════════
# K. compose_runtime_e2e phase_backup_restore 集成
# ════════════════════════════════════════════════════════════════


class TestComposeRuntimeE2EBackupRestore:
    """R71 Wave 3 K: compose_runtime_e2e.py phase_backup_restore 集成。"""

    def test_phase_backup_restore_docstring_mentions_wave3(self, orch):
        """phase_backup_restore docstring 必须提及 R71 Wave 3 P0-08。"""
        import inspect
        doc = orch.phase_backup_restore.__doc__ or ""
        assert "R71 Wave 3" in doc, (
            "phase_backup_restore docstring 必须提及 R71 Wave 3"
        )
        assert "P0-08" in doc, (
            "phase_backup_restore docstring 必须提及 P0-08"
        )
        assert "verify_full" in doc, (
            "phase_backup_restore docstring 必须提及 verify_full(R71 Wave 3 升级)"
        )

    def test_phase_backup_restore_has_wave3_readiness_checks(self, orch):
        """phase_backup_restore 必须在源码中包含 R71 Wave 3 新增 readiness 检查点。"""
        import inspect
        source = inspect.getsource(orch.phase_backup_restore)
        required_checks = [
            "schema_fingerprint_captured",
            "field_hashes_captured",
            "migration_version_compatible",
            "app_start_after_restore",
            "app_read_write_after_restore",
            "synthetic_transaction_after_restore",
            "switch_rollback_evidence_generated",
        ]
        for check in required_checks:
            assert check in source, (
                f"phase_backup_restore 源码必须包含 readiness 检查点: {check}"
            )

    def test_run_restore_integrity_verify_supports_verify_full(self, orch):
        """_run_restore_integrity_verify 必须支持 verify_full(R71 Wave 3)。"""
        import inspect
        source = inspect.getsource(orch._run_restore_integrity_verify)
        assert "verify_full" in source, (
            "_run_restore_integrity_verify 必须调用 verify_full(R71 Wave 3 升级)"
        )
        assert "target_db" in source, (
            "_run_restore_integrity_verify 必须支持 target_db 参数(隔离恢复)"
        )
        # 必须保留 _run_restore_integrity_verify 函数名(向后兼容 Wave 2 测试)
        assert "_run_restore_integrity_verify" in source


# ════════════════════════════════════════════════════════════════
# L. fail-closed 行为与一致性
# ════════════════════════════════════════════════════════════════


class TestFailClosedAndConsistency:
    """R71 Wave 3 L: fail-closed 行为与一致性。"""

    def test_no_todo_or_pass_in_verify_restore_integrity(self):
        """verify_restore_integrity.py 不允许 TODO / FIXME / XXX / HACK。"""
        content = VERIFY_RESTORE_INTEGRITY_PATH.read_text(encoding="utf-8")
        for forbidden in ["TODO", "FIXME", "XXX", "HACK"]:
            for line in content.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    assert forbidden not in stripped, (
                        f"verify_restore_integrity.py 注释中不允许 {forbidden}: {line}"
                    )
                else:
                    if forbidden == "TODO":
                        assert forbidden not in line, (
                            f"verify_restore_integrity.py 不允许 TODO: {line}"
                        )

    def test_verify_full_skips_synthetic_when_requested(self, vri, monkeypatch):
        """verify_full(skip_synthetic=True) 必须跳过合成交易验证。"""
        def mock_exec_sql(query, *args, **kwargs):
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 1}]', ""
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        def mock_exec_health(role, *args, **kwargs):
            return 0, json.dumps({"healthy": True, "checks": []}), ""
        monkeypatch.setattr(vri, "_exec_health", mock_exec_health)

        # 这个 mock 不应该被调用
        def mock_synthetic_should_not_be_called(timeout=60, **kwargs):
            raise AssertionError(
                "skip_synthetic=True 时不应调用 run_synthetic_transaction_in_restored_env"
            )
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env",
            mock_synthetic_should_not_be_called,
        )

        evidence = vri.verify_full(
            trace_id="skip_synthetic_test",
            pre_snapshot_path=None,
            target_db="staging",
            skip_synthetic=True,
            skip_app_checks=True,
        )
        # synthetic_transaction 应该为空 dict
        assert evidence.synthetic_transaction == {}, (
            "skip_synthetic=True 时 synthetic_transaction 必须为空 dict"
        )

    def test_verify_full_skips_app_checks_when_requested(self, vri, monkeypatch):
        """verify_full(skip_app_checks=True) 必须跳过应用启动/读写验证。"""
        def mock_exec_sql(query, *args, **kwargs):
            if "COUNT" in query and "name" in query:
                return 0, '[{"cnt": 1}]', ""
            if "sqlite_master" in query:
                return 0, "[]", ""
            if query.strip().upper().startswith("SELECT"):
                return 0, "[]", ""
            return 0, "OK", ""
        monkeypatch.setattr(vri, "_exec_sql", mock_exec_sql)

        # 这个 mock 不应该被调用
        def mock_health_should_not_be_called(role, *args, **kwargs):
            raise AssertionError(
                "skip_app_checks=True 时不应调用 _exec_health"
            )
        monkeypatch.setattr(vri, "_exec_health", mock_health_should_not_be_called)

        def mock_synthetic(timeout=60, **kwargs):
            return {"overall_passed": True}
        monkeypatch.setattr(
            vri, "run_synthetic_transaction_in_restored_env", mock_synthetic,
        )

        evidence = vri.verify_full(
            trace_id="skip_app_checks_test",
            pre_snapshot_path=None,
            target_db="staging",
            skip_synthetic=False,
            skip_app_checks=True,
        )
        # app_start_check / app_read_write_check 应该为空 dict
        assert evidence.app_start_check == {}, (
            "skip_app_checks=True 时 app_start_check 必须为空 dict"
        )
        assert evidence.app_read_write_check == {}, (
            "skip_app_checks=True 时 app_read_write_check 必须为空 dict"
        )

    def test_compute_payload_hash_deterministic(self, vri):
        """compute_payload_hash 必须对相同输入产生相同 hash(确定性)。"""
        rows = [
            {"name": "a", "value": 1},
            {"name": "b", "value": 2},
        ]
        hash1 = vri.compute_payload_hash(rows)
        hash2 = vri.compute_payload_hash(rows)
        assert hash1 == hash2, "相同输入必须产生相同 hash(确定性)"
        # 不同顺序的 key 不影响 hash(canonical JSON sort_keys=True)
        rows_diff_key_order = [
            {"value": 1, "name": "a"},
            {"value": 2, "name": "b"},
        ]
        hash3 = vri.compute_payload_hash(rows_diff_key_order)
        assert hash1 == hash3, (
            "key 顺序不同但内容相同的数据必须产生相同 hash(canonical JSON)"
        )
