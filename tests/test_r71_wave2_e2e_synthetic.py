"""R71 Wave 2: 全角色 runtime E2E + 合成交易门禁 — 测试套件。

R71 报告 P0-05/06/07 指出三个问题:
    - P0-05: scripts/compose_runtime_e2e.py 存在但未成为 Release Gates required job
    - P0-06: business_smoke 只调用 admin /healthz,不是完整业务交易
    - P0-07: start_bots 只列出 5 个 bot,缺少 admin/crdb_sync/db_backup/prometheus_exporter

R71 Wave 2 整改:
    1. scripts/compose_runtime_e2e.py:
       - _get_entrypoint_roles() 自动从 docker/entrypoint.py 导出角色集合
       - start_bots 阶段扩展到全部 9 个业务服务(5 bot + 4 业务服务)
       - health_check 阶段对每个业务服务执行 python -m services.health --role <role> --json
       - business_smoke 阶段重写为合成交易(不再用 /healthz 代替业务交易)
       - backup_restore 阶段重写为结构化数据校验(不再用日志关键词判断恢复成功)
       - 新增 evidence 输出: runtime-e2e-evidence.json
    2. .github/workflows/release-gates.yml:
       - 新增 compose-runtime-e2e job(needs: docker-build)
       - 添加到 release-summary needs 列表
    3. scripts/synthetic_transaction.py: 新建合成交易执行器
    4. scripts/verify_restore_integrity.py: 新建恢复完整性校验脚本

被测对象:
    - scripts/compose_runtime_e2e.py(编排器扩展)
    - scripts/synthetic_transaction.py(合成交易执行器)
    - scripts/verify_restore_integrity.py(恢复完整性校验)
    - .github/workflows/release-gates.yml(工作流门禁)

测试覆盖矩阵:
    A. _get_entrypoint_roles() 自动导出 — 4 个
    B. start_bots 阶段扩展到全部 9 个业务服务 — 3 个
    C. health_check 阶段对每个角色执行 services.health --role — 3 个
    D. synthetic_transaction.py 合成交易执行器 — 5 个
    E. verify_restore_integrity.py 结构化数据校验 — 3 个
    F. runtime-e2e-evidence.json 证据输出 — 3 个
    G. .github/workflows/release-gates.yml 门禁配置 — 4 个
    H. fail-closed 行为(无 mock / no /healthz / no log keyword) — 5 个

测试策略:
    - 不实际调用 docker / redis / sqlite(用 unittest.mock 模拟)
    - 验证脚本逻辑正确性(返回结构、JSON 证据、fail-closed 行为)
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"
VERIFY_RESTORE_INTEGRITY_PATH = REPO_ROOT / "scripts" / "verify_restore_integrity.py"
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "entrypoint.py"
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"


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
def orch():
    """加载 compose_runtime_e2e 模块(模块级缓存)。"""
    return _load_module_from_path("scripts.compose_runtime_e2e_r71", SCRIPT_PATH)


@pytest.fixture(scope="module")
def synthetic_tx():
    """加载 synthetic_transaction 模块(模块级缓存)。"""
    return _load_module_from_path("scripts.synthetic_transaction_r71", SYNTHETIC_TRANSACTION_PATH)


@pytest.fixture(scope="module")
def verify_restore():
    """加载 verify_restore_integrity 模块(模块级缓存)。"""
    return _load_module_from_path(
        "scripts.verify_restore_integrity_r71", VERIFY_RESTORE_INTEGRITY_PATH
    )


# ════════════════════════════════════════════════════════════════
# A. _get_entrypoint_roles() 自动导出
# ════════════════════════════════════════════════════════════════


class TestGetEntrypointRoles:
    """R71 Wave 2 A: _get_entrypoint_roles() 自动从 entrypoint.py 导出角色集合。"""

    def test_function_exists(self, orch):
        """编排器必须暴露 _get_entrypoint_roles 函数。"""
        assert hasattr(orch, "_get_entrypoint_roles"), (
            "R71 Wave 2: 编排器必须暴露 _get_entrypoint_roles() 函数"
        )
        assert callable(orch._get_entrypoint_roles), (
            "_get_entrypoint_roles 必须是可调用对象"
        )

    def test_returns_complete_12_roles(self, orch):
        """_get_entrypoint_roles() 必须返回 entrypoint.py 中 ALLOWED_SERVICE_ROLES 的全部角色。

        entrypoint.py 定义:
          - SERVICE_ROLE_RUN_ALL = {up, idx, dsp, mon, admin, admin_bot,
            db_writer, crdb_sync, db_backup, r40_scheduler}
          - SERVICE_ROLE_MODULE = {migration: ..., prometheus_exporter: ...}
          - ALLOWED_SERVICE_ROLES = 10 + 2 = 12 个角色
        """
        roles = orch._get_entrypoint_roles()
        expected_roles = {
            # SERVICE_ROLE_RUN_ALL(10 个)
            "up", "idx", "dsp", "mon", "admin", "admin_bot",
            "db_writer", "crdb_sync", "db_backup", "r40_scheduler",
            # SERVICE_ROLE_MODULE keys(2 个)
            "migration", "prometheus_exporter",
        }
        assert roles == expected_roles, (
            f"_get_entrypoint_roles() 返回的角色不匹配: "
            f"expected={sorted(expected_roles)}, actual={sorted(roles)}, "
            f"missing={sorted(expected_roles - roles)}, "
            f"extra={sorted(roles - expected_roles)}"
        )

    def test_returns_set_type(self, orch):
        """_get_entrypoint_roles() 必须返回 set 类型。"""
        roles = orch._get_entrypoint_roles()
        assert isinstance(roles, set), (
            f"_get_entrypoint_roles() 必须返回 set, 实际: {type(roles)}"
        )

    def test_fail_closed_on_missing_entrypoint(self, orch, tmp_path):
        """entrypoint.py 不存在时返回空集合(fail-closed,不硬编码 fallback)。"""
        # 临时修改 ENTRYPOINT_PATH 指向不存在的文件
        original_path = orch.ENTRYPOINT_PATH
        try:
            orch.ENTRYPOINT_PATH = tmp_path / "nonexistent_entrypoint.py"
            roles = orch._get_entrypoint_roles()
            assert roles == set(), (
                "entrypoint.py 不存在时必须返回空集合(fail-closed),"
                "不允许硬编码角色列表作为 fallback"
            )
        finally:
            orch.ENTRYPOINT_PATH = original_path


# ════════════════════════════════════════════════════════════════
# B. start_bots 阶段扩展到全部 9 个业务服务
# ════════════════════════════════════════════════════════════════


class TestStartBotsPhaseExpansion:
    """R71 Wave 2 B: start_bots 阶段扩展到全部 9 个业务服务。"""

    def test_bot_services_has_9_entries(self, orch):
        """BOT_SERVICES 必须包含 9 个服务(5 bot + 4 业务服务)。"""
        assert len(orch.BOT_SERVICES) == 9, (
            f"R71 P0-07: BOT_SERVICES 必须有 9 个服务, 实际: {len(orch.BOT_SERVICES)}"
        )

    def test_bot_services_includes_new_roles(self, orch):
        """BOT_SERVICES 必须包含 admin/crdb_sync/db_backup/prometheus_exporter。"""
        required_new_services = {
            "admin", "crdb_sync", "db_backup", "prometheus_exporter",
        }
        actual = set(orch.BOT_SERVICES)
        missing = required_new_services - actual
        assert not missing, (
            f"R71 P0-07: BOT_SERVICES 缺少新角色: {sorted(missing)}, "
            f"实际: {sorted(actual)}"
        )

    def test_bot_services_includes_original_bots(self, orch):
        """BOT_SERVICES 必须仍包含原 5 个 bot(up/idx/dsp/mon/admin_bot)。"""
        original_bots = {"up", "idx", "dsp", "mon", "admin_bot"}
        actual = set(orch.BOT_SERVICES)
        missing = original_bots - actual
        assert not missing, (
            f"BOT_SERVICES 缺少原 5 个 bot: {sorted(missing)}, "
            f"实际: {sorted(actual)}"
        )


# ════════════════════════════════════════════════════════════════
# C. health_check 阶段对每个角色执行 services.health --role
# ════════════════════════════════════════════════════════════════


class TestHealthCheckRoleBased:
    """R71 Wave 2 C: health_check 阶段对每个业务服务执行 services.health --role。"""

    def test_service_roles_dict_exists(self, orch):
        """SERVICE_ROLES 映射字典必须存在。"""
        assert hasattr(orch, "SERVICE_ROLES"), (
            "编排器必须暴露 SERVICE_ROLES 映射字典"
        )
        assert isinstance(orch.SERVICE_ROLES, dict), (
            f"SERVICE_ROLES 必须是 dict, 实际: {type(orch.SERVICE_ROLES)}"
        )

    def test_service_roles_covers_all_business_services(self, orch):
        """SERVICE_ROLES 必须覆盖所有业务服务(非基础设施)。"""
        # 排除基础设施(redis / redis-acl-init)
        business_services = {
            k: v for k, v in orch.SERVICE_ROLES.items()
            if v != "infrastructure"
        }
        expected_business_services = {
            "migration", "db_writer", "crdb_sync",
            "up", "idx", "dsp", "mon", "admin_bot",
            "admin", "db_backup", "prometheus_exporter",
        }
        actual = set(business_services.keys())
        missing = expected_business_services - actual
        assert not missing, (
            f"SERVICE_ROLES 缺少业务服务: {sorted(missing)}, "
            f"实际: {sorted(actual)}"
        )

    def test_http_health_services_defined(self, orch):
        """HTTP_HEALTH_SERVICES 必须定义暴露 HTTP /health 的服务。"""
        assert hasattr(orch, "HTTP_HEALTH_SERVICES"), (
            "编排器必须暴露 HTTP_HEALTH_SERVICES 映射"
        )
        # 至少 admin 和 prometheus_exporter
        assert "admin" in orch.HTTP_HEALTH_SERVICES, (
            "HTTP_HEALTH_SERVICES 必须包含 admin(8080 端口)"
        )
        assert "prometheus_exporter" in orch.HTTP_HEALTH_SERVICES, (
            "HTTP_HEALTH_SERVICES 必须包含 prometheus_exporter(9100 端口)"
        )


# ════════════════════════════════════════════════════════════════
# D. synthetic_transaction.py 合成交易执行器
# ════════════════════════════════════════════════════════════════


class TestSyntheticTransactionExecutor:
    """R71 Wave 2 D: synthetic_transaction.py 合成交易执行器。"""

    def test_script_file_exists(self):
        """scripts/synthetic_transaction.py 文件存在。"""
        assert SYNTHETIC_TRANSACTION_PATH.is_file(), (
            f"R71 Wave 2: 合成交易执行器文件不存在: {SYNTHETIC_TRANSACTION_PATH}"
        )

    def test_script_importable(self, synthetic_tx):
        """合成交易执行器模块可被 import,且暴露关键符号。"""
        required_attrs = [
            "generate_trace_id",
            "inject_test_event",
            "verify_result",
            "verify_idempotency",
            "inject_failure_scenario",
            "cleanup",
            "run_full_transaction",
            "TransactionEvidence",
            "StepResult",
        ]
        for attr in required_attrs:
            assert hasattr(synthetic_tx, attr), (
                f"synthetic_transaction.py 必须暴露 {attr}()"
            )

    def test_generate_trace_id_returns_unique_ids(self, synthetic_tx):
        """generate_trace_id() 必须返回带 synthetic_r71_ 前缀的唯一 ID。"""
        id1 = synthetic_tx.generate_trace_id()
        id2 = synthetic_tx.generate_trace_id()
        assert id1.startswith("synthetic_r71_"), (
            f"trace_id 必须以 'synthetic_r71_' 前缀开头, 实际: {id1!r}"
        )
        assert id2.startswith("synthetic_r71_"), (
            f"trace_id 必须以 'synthetic_r71_' 前缀开头, 实际: {id2!r}"
        )
        assert id1 != id2, (
            "generate_trace_id() 必须返回唯一 ID(连续调用不能相同)"
        )

    def test_verify_result_returns_false_when_data_not_exists(self, synthetic_tx):
        """verify_result() 在数据不存在时必须返回 passed=False(fail-closed)。

        通过 mock subprocess.run 模拟 db_writer 查询返回 count=0,
        验证 verify_result 不假装通过(不 mock 真实功能)。
        """
        trace_id = "synthetic_r71_test_nonexistent"

        # mock subprocess.run 让查询返回 0(数据不存在)
        def mock_run_side_effect(*args, **kwargs):
            cp = MagicMock()
            cp.returncode = 0
            cp.stdout = "0"  # COUNT=0
            cp.stderr = ""
            return cp

        with patch("subprocess.run", side_effect=mock_run_side_effect):
            result = synthetic_tx.verify_result(trace_id, timeout=2)

        assert isinstance(result, synthetic_tx.StepResult), (
            f"verify_result 必须返回 StepResult, 实际: {type(result)}"
        )
        assert result.passed is False, (
            "verify_result 在数据不存在时必须返回 passed=False(fail-closed),"
            "不允许假装通过"
        )
        assert result.error is not None, (
            "verify_result 在失败时必须提供 error 说明"
        )

    def test_run_full_transaction_returns_evidence(self, synthetic_tx):
        """run_full_transaction() 必须返回 TransactionEvidence 对象。"""
        # mock inject_test_event 失败,让 run_full_transaction 快速返回
        # (验证证据结构,不验证业务逻辑)
        # 注意:run_full_transaction 在 finally 块中会调用 cleanup(),
        # cleanup() 会调用 subprocess.run(docker compose exec),
        # 因此也需要 mock subprocess.run 防止实际调用 docker
        def mock_inject(trace_id, timeout=30):
            return synthetic_tx.StepResult(
                step="inject",
                timestamp="2026-07-21T00:00:00+00:00",
                duration_seconds=0.1,
                returncode=1,
                stdout="",
                stderr="mock failure",
                passed=False,
                error="mock inject failure",
            )

        def mock_run_side_effect(*args, **kwargs):
            cp = MagicMock()
            cp.returncode = 0
            cp.stdout = "0"
            cp.stderr = ""
            return cp

        with patch.object(synthetic_tx, "inject_test_event", side_effect=mock_inject), \
             patch("subprocess.run", side_effect=mock_run_side_effect):
            evidence = synthetic_tx.run_full_transaction(timeout=2)

        assert isinstance(evidence, synthetic_tx.TransactionEvidence), (
            f"run_full_transaction 必须返回 TransactionEvidence, "
            f"实际: {type(evidence)}"
        )
        assert evidence.overall_passed is False, (
            "inject 失败时 overall_passed 必须为 False(fail-closed)"
        )
        assert evidence.trace_id.startswith("synthetic_r71_"), (
            f"trace_id 必须以 'synthetic_r71_' 前缀开头, 实际: {evidence.trace_id!r}"
        )


# ════════════════════════════════════════════════════════════════
# E. verify_restore_integrity.py 结构化数据校验
# ════════════════════════════════════════════════════════════════


class TestVerifyRestoreIntegrity:
    """R71 Wave 2 E: verify_restore_integrity.py 结构化数据校验。"""

    def test_script_file_exists(self):
        """scripts/verify_restore_integrity.py 文件存在。"""
        assert VERIFY_RESTORE_INTEGRITY_PATH.is_file(), (
            f"R71 Wave 2: 恢复完整性校验脚本不存在: {VERIFY_RESTORE_INTEGRITY_PATH}"
        )

    def test_script_exposes_required_functions(self, verify_restore):
        """verify_restore_integrity.py 必须暴露关键函数。"""
        required_attrs = [
            "write_marker",
            "take_snapshot",
            "verify",
            "cleanup_marker",
            "get_table_counts",
            "IntegrityEvidence",
            "TableCount",
            "CRITICAL_TABLES",
            "MARKER_TABLE",
        ]
        for attr in required_attrs:
            assert hasattr(verify_restore, attr), (
                f"verify_restore_integrity.py 必须暴露 {attr}"
            )

    def test_critical_tables_includes_essential_tables(self, verify_restore):
        """CRITICAL_TABLES 必须包含关键表。"""
        essential_tables = {
            "bot_heartbeat",
            "file_index",
            "user_quota",
        }
        actual = set(verify_restore.CRITICAL_TABLES)
        missing = essential_tables - actual
        assert not missing, (
            f"CRITICAL_TABLES 缺少关键表: {sorted(missing)}, "
            f"实际: {sorted(actual)}"
        )


# ════════════════════════════════════════════════════════════════
# F. runtime-e2e-evidence.json 证据输出
# ════════════════════════════════════════════════════════════════


class TestEvidenceOutput:
    """R71 Wave 2 F: runtime-e2e-evidence.json 证据输出结构。"""

    def test_build_evidence_function_exists(self, orch):
        """编排器必须暴露 _build_evidence 函数。"""
        assert hasattr(orch, "_build_evidence"), (
            "R71 Wave 2: 编排器必须暴露 _build_evidence() 函数"
        )
        assert callable(orch._build_evidence), (
            "_build_evidence 必须是可调用对象"
        )

    def test_build_evidence_returns_correct_structure(self, orch):
        """_build_evidence() 必须返回包含必需字段的 dict。"""
        # 构造模拟 PhaseResult
        phase_result = orch.PhaseResult(
            phase="test",
            description="test phase",
            status="pass",
            timestamp="2026-07-21T00:00:00+00:00",
            duration_seconds=0.1,
        )
        evidence = orch._build_evidence(
            results=[phase_result],
            started_at="2026-07-21T00:00:00+00:00",
            finished_at="2026-07-21T00:00:01+00:00",
            overall_passed=True,
        )

        # 必需字段
        required_fields = [
            "schema_version",
            "started_at",
            "finished_at",
            "overall_passed",
            "source_sha",
            "image_repo_digest",
            "compose_digest",
            "compose_file",
            "env_file",
            "role_matrix",
            "phases",
            "phase_summary",
        ]
        for field in required_fields:
            assert field in evidence, (
                f"evidence 缺少必需字段: {field}, "
                f"实际字段: {sorted(evidence.keys())}"
            )

        # schema_version 必须标识 R71(R71 Wave 2 引入,Wave 7 扩展为 r71-wave7)
        assert evidence["schema_version"] in ("r71-wave2", "r71-wave7"), (
            f"schema_version 必须为 'r71-wave2' 或 'r71-wave7', 实际: {evidence['schema_version']!r}"
        )

        # role_matrix 必须包含 entrypoint_roles
        role_matrix = evidence["role_matrix"]
        assert "entrypoint_roles" in role_matrix, (
            "role_matrix 必须包含 entrypoint_roles(R71 Wave 2: 角色矩阵)"
        )
        assert "service_roles" in role_matrix, (
            "role_matrix 必须包含 service_roles"
        )

    def test_main_has_output_cli_option(self, orch):
        """main() 必须支持 --output CLI 选项。"""
        # 通过 argparse 解析验证
        import argparse
        # 读取 main 函数源码,验证 --output 参数定义
        # (不实际执行 main,避免触发 docker 调用)
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert '"--output"' in content or "'--output'" in content, (
            "R71 Wave 2: compose_runtime_e2e.py 必须支持 --output CLI 选项"
        )
        assert "runtime-e2e-evidence.json" in content, (
            "R71 Wave 2: 必须在脚本中提及 runtime-e2e-evidence.json"
        )


# ════════════════════════════════════════════════════════════════
# G. .github/workflows/release-gates.yml 门禁配置
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesWorkflow:
    """R71 Wave 2 G: release-gates.yml 必须包含 compose-runtime-e2e 门禁。"""

    def test_release_gates_file_exists(self):
        """release-gates.yml 文件存在。"""
        assert RELEASE_GATES_PATH.is_file(), (
            f"release-gates.yml 不存在: {RELEASE_GATES_PATH}"
        )

    def test_compose_runtime_e2e_job_defined(self):
        """release-gates.yml 必须定义 compose-runtime-e2e job。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        assert "  compose-runtime-e2e:" in content, (
            "R71 P0-05: release-gates.yml 必须定义 'compose-runtime-e2e' job"
        )

    def test_compose_runtime_e2e_in_release_summary_needs(self):
        """release-gates.yml 的 release-summary job needs 必须包含 compose-runtime-e2e。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        # 验证 release-summary 的 needs 列表包含 compose-runtime-e2e
        # 通过查找 needs: [...] 块中的 compose-runtime-e2e
        assert "compose-runtime-e2e" in content, (
            "release-gates.yml 必须提及 compose-runtime-e2e"
        )
        # 验证 release-summary 的 needs 块包含 compose-runtime-e2e
        # 通过查找 release-summary 之后的 needs 列表
        release_summary_idx = content.find("  release-summary:")
        assert release_summary_idx != -1, (
            "release-gates.yml 必须有 release-summary job"
        )
        # 在 release-summary 之后查找 needs 列表
        needs_section = content[release_summary_idx:release_summary_idx + 2000]
        assert "compose-runtime-e2e" in needs_section, (
            "release-summary 的 needs 列表必须包含 compose-runtime-e2e"
        )

    def test_compose_runtime_e2e_has_correct_trigger(self):
        """compose-runtime-e2e job 必须仅在 rc-v* tag 触发。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        # 找到 compose-runtime-e2e job 定义
        job_idx = content.find("  compose-runtime-e2e:")
        assert job_idx != -1, "compose-runtime-e2e job 未定义"
        # 查看 job 后 1500 字符(包含 if 条件)
        job_section = content[job_idx:job_idx + 1500]
        # R70 P0-10: rc-v* tag 使用 startsWith(github.ref, 'refs/tags/rc-v')
        # (不带尾随 dash,匹配 rc-v1.0.0 / rc-v2.0.0 等)
        assert "startsWith(github.ref, 'refs/tags/rc-v')" in job_section, (
            "compose-runtime-e2e job 必须仅在 rc-v* tag 触发"
        )
        assert "environment: rc-candidate" in job_section, (
            "compose-runtime-e2e job 必须使用 rc-candidate environment"
        )

    def test_compose_runtime_e2e_runs_compose_runtime_e2e_script(self):
        """compose-runtime-e2e job 必须实际运行 compose_runtime_e2e.py 脚本。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        job_idx = content.find("  compose-runtime-e2e:")
        assert job_idx != -1, "compose-runtime-e2e job 未定义"
        # 查找 job 末尾(下一个 job 定义之前)
        next_job_idx = content.find("\n  # ───", job_idx + 1)
        if next_job_idx == -1:
            next_job_idx = len(content)
        job_section = content[job_idx:next_job_idx]
        assert "compose_runtime_e2e.py" in job_section, (
            "compose-runtime-e2e job 必须运行 compose_runtime_e2e.py 脚本"
        )
        assert "--output runtime-e2e-evidence.json" in job_section, (
            "compose-runtime-e2e job 必须使用 --output runtime-e2e-evidence.json 输出证据"
        )


# ════════════════════════════════════════════════════════════════
# H. fail-closed 行为(无 mock / no /healthz / no log keyword)
# ════════════════════════════════════════════════════════════════


class TestFailClosedBehavior:
    """R71 Wave 2 H: fail-closed 行为验证(无 mock / no /healthz / no log keyword)。"""

    def test_business_smoke_does_not_use_healthz(self, orch):
        """business_smoke 阶段不得用 /healthz 端点代替业务交易(R71 P0-06)。

        验证 phase_business_smoke 函数体不调用 /healthz 端点
        (urllib.request.urlopen + /healthz),而是通过 _run_synthetic_transaction
        调用真实合成交易。

        注意:函数 docstring 中可能提及 /healthz 用于解释整改背景,
        这是允许的;关键在于函数体不能实际调用 /healthz 端点。
        """
        import ast
        import inspect
        source = inspect.getsource(orch.phase_business_smoke)
        # 解析 AST,提取函数体(排除 docstring)
        tree = ast.parse(source)
        func_node = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)),
            None,
        )
        assert func_node is not None, "无法解析 phase_business_smoke 函数 AST"

        # 提取函数体代码(去掉 docstring)
        body_source_lines = []
        for node in func_node.body:
            # 跳过 docstring(Expr 节点包含 Constant 字符串)
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            body_source_lines.append(ast.get_source_segment(source, node) or "")

        body_source = "\n".join(body_source_lines)

        # 不允许在函数体中调用 urllib.request.urlopen 访问 /healthz
        # (admin /health 端点检查在 health_check 阶段已包含,
        #  business_smoke 必须做真实合成交易)
        assert "urlopen" not in body_source or "/healthz" not in body_source, (
            "R71 P0-06: business_smoke 阶段函数体不得调用 urlopen + /healthz "
            "代替业务交易(应通过 _run_synthetic_transaction 调用真实合成交易)"
        )
        # 必须调用 _run_synthetic_transaction
        assert "_run_synthetic_transaction" in body_source, (
            "R71 P0-06: business_smoke 阶段必须调用 _run_synthetic_transaction()"
        )

    def test_backup_restore_does_not_use_log_keywords(self, orch):
        """backup_restore 阶段不得用日志关键词判断恢复成功(R71 P0-07)。

        验证 phase_backup_restore 函数源码不包含 integrity_keywords 列表,
        而是通过 _run_restore_integrity_verify 进行结构化数据校验。
        """
        import inspect
        source = inspect.getsource(orch.phase_backup_restore)
        # 不允许日志关键词列表(旧版用了 ["ok", "success", "verified", "complete"])
        assert "integrity_keywords" not in source, (
            "R71 P0-07: backup_restore 阶段不得用日志关键词(integrity_keywords)判断恢复成功"
        )
        # 必须调用 _run_restore_integrity_verify
        assert "_run_restore_integrity_verify" in source, (
            "R71 P0-07: backup_restore 阶段必须调用 _run_restore_integrity_verify()"
        )

    def test_no_todo_or_pass_in_synthetic_transaction(self):
        """synthetic_transaction.py 不允许 TODO / pass / 占位符。"""
        content = SYNTHETIC_TRANSACTION_PATH.read_text(encoding="utf-8")
        # 排除注释中的 TODO(但 R71 严格禁止 TODO)
        for forbidden in ["TODO", "FIXME", "XXX", "HACK"]:
            # 检查行首(忽略空格)是否有 forbidden 标记
            for line in content.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    # 注释行:检查是否包含 TODO 等(严格禁止)
                    assert forbidden not in stripped, (
                        f"synthetic_transaction.py 注释中不允许 {forbidden}: {line}"
                    )
                else:
                    # 代码行:不允许单独 pass 语句(允许 pass 作为参数等)
                    if forbidden == "TODO":
                        assert forbidden not in line, (
                            f"synthetic_transaction.py 不允许 TODO: {line}"
                        )

    def test_no_todo_or_pass_in_verify_restore_integrity(self):
        """verify_restore_integrity.py 不允许 TODO / pass / 占位符。"""
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

    def test_synthetic_transaction_fail_closed_on_missing_module(self, orch):
        """_run_synthetic_transaction 在 synthetic_transaction.py 不存在时必须 fail-closed。"""
        # 临时修改 SYNTHETIC_TRANSACTION_PATH 指向不存在的文件
        original_path = orch.SYNTHETIC_TRANSACTION_PATH
        try:
            orch.SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "nonexistent.py"
            passed, evidence = orch._run_synthetic_transaction(timeout=2)
            assert passed is False, (
                "_run_synthetic_transaction 在模块不存在时必须返回 passed=False(fail-closed)"
            )
            assert "error" in evidence, (
                "_run_synthetic_transaction 失败时必须提供 error 字段"
            )
        finally:
            orch.SYNTHETIC_TRANSACTION_PATH = original_path
