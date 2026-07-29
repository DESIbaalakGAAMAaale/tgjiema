"""R73 §5.15: Runtime E2E 阶段 DAG 测试套件。

R73 §5.15 整改要求:
    compose-runtime-e2e 必须按显式 DAG 顺序执行阶段,
    后续阶段不能在上游失败后继续产生成功证据。

被测对象:
    - scripts/compose_runtime_e2e.py
    - 16 阶段 DAG 定义(PHASE_DEPENDENCIES)
    - ALLOWED_AFTER_FAILURE 集合(cleanup 和诊断采集)
    - PhaseResult 数据类 DAG 字段(depends_on/started_at/completed_at/blocking_reason)
    - main() 函数 DAG 失败传播规则

测试覆盖矩阵:
    A. PHASES 列表顺序与数量(16 阶段 DAG 拓扑序)— 3 个
    B. PHASE_DEPENDENCIES 完整性与无环性 — 4 个
    C. PHASE_FUNCS 覆盖所有阶段 — 2 个
    D. ALLOWED_AFTER_FAILURE 正确性 — 3 个
    E. PhaseResult DAG 字段完整性 — 4 个
    F. _pass_result / _fail_result / _skipped_result 填充 DAG 字段 — 3 个
    G. main() DAG 失败传播规则(模拟) — 6 个

测试策略:
    - 不实际调用 docker(用 unittest.mock 模拟 phase 函数)
    - 验证 DAG 逻辑正确性(失败传播/skipped/cleanup 不覆盖 failure)
    - 严格遵守 R73 §5.15 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"


# ════════════════════════════════════════════════════════════════
# 辅助:加载编排器模块(不通过 sys.path,直接 spec_from_file_location)
# ════════════════════════════════════════════════════════════════


def _load_orchestrator_module():
    """直接从文件路径加载编排器模块,避免 sys.path 污染。"""
    if "scripts.compose_runtime_e2e" in sys.modules:
        return sys.modules["scripts.compose_runtime_e2e"]
    spec = importlib.util.spec_from_file_location(
        "scripts.compose_runtime_e2e", SCRIPT_PATH
    )
    assert spec is not None, f"无法加载模块 spec: {SCRIPT_PATH}"
    assert spec.loader is not None, f"模块 loader 为 None: {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts.compose_runtime_e2e"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def orch():
    """加载编排器模块(模块级缓存)。"""
    return _load_orchestrator_module()


# ════════════════════════════════════════════════════════════════
# A. PHASES 列表顺序与数量
# ════════════════════════════════════════════════════════════════


class TestPhasesOrder:
    """验证 PHASES 列表按 R73 §5.15 DAG 拓扑序排列。"""

    EXPECTED_PHASES = [
        "preflight",
        "start_infrastructure",
        "start_application_roles",
        "real_product_transaction_before_backup",
        "full_backup_to_r2",
        "blank_isolated_restore",
        "restore_integrity_and_target_identity",
        "actual_switch",
        "real_product_transaction_after_switch",
        "fault_injection",
        "actual_rollback",
        "real_product_transaction_after_rollback",
        "sigterm_with_inflight_message",
        "restart_and_pending_recovery",
        "final_identity_and_cleanup",
        "evidence_signing",
    ]

    def test_phases_count_is_16(self, orch):
        """PHASES 列表必须正好包含 16 个阶段。"""
        assert len(orch.PHASES) == 16, (
            f"PHASES 必须有 16 个阶段, 实际: {len(orch.PHASES)}"
        )

    def test_phases_names_in_dag_order(self, orch):
        """PHASES 名称必须按 R73 §5.15 DAG 拓扑序匹配。"""
        actual_names = [name for name, _ in orch.PHASES]
        assert actual_names == self.EXPECTED_PHASES, (
            f"PHASES 名称不匹配: expected={self.EXPECTED_PHASES}, "
            f"actual={actual_names}"
        )

    def test_phases_have_non_empty_descriptions(self, orch):
        """每个阶段必须有非空描述。"""
        for name, desc in orch.PHASES:
            assert desc, f"阶段 {name} 描述为空"
            assert isinstance(desc, str), f"阶段 {name} 描述不是字符串"


# ════════════════════════════════════════════════════════════════
# B. PHASE_DEPENDENCIES 完整性与无环性
# ════════════════════════════════════════════════════════════════


class TestPhaseDependencies:
    """验证 PHASE_DEPENDENCIES 定义完整且无环。"""

    def test_dependencies_cover_all_phases(self, orch):
        """PHASE_DEPENDENCIES 必须覆盖所有 16 个阶段。"""
        phase_names = {name for name, _ in orch.PHASES}
        dep_keys = set(orch.PHASE_DEPENDENCIES.keys())
        assert dep_keys == phase_names, (
            f"PHASE_DEPENDENCIES 键不匹配 PHASES: "
            f"missing={phase_names - dep_keys}, "
            f"extra={dep_keys - phase_names}"
        )

    def test_dependencies_reference_valid_phases(self, orch):
        """所有依赖必须引用 PHASES 中存在的阶段。"""
        phase_names = {name for name, _ in orch.PHASES}
        for phase, deps in orch.PHASE_DEPENDENCIES.items():
            for dep in deps:
                assert dep in phase_names, (
                    f"阶段 {phase} 依赖不存在的阶段: {dep}"
                )

    def test_dependencies_form_linear_chain(self, orch):
        """R73 §5.15: 依赖必须形成线性链(每个阶段依赖前一个)。

        线性 DAG 确保严格顺序执行,任一阶段失败传播到所有下游。
        """
        phases = [name for name, _ in orch.PHASES]
        for i, phase in enumerate(phases):
            deps = orch.PHASE_DEPENDENCIES[phase]
            if i == 0:
                # 第一个阶段无依赖
                assert deps == [], (
                    f"第一阶段 {phase} 不应有依赖, 实际: {deps}"
                )
            else:
                # 后续阶段必须依赖前一个阶段
                prev = phases[i - 1]
                assert prev in deps, (
                    f"阶段 {phase} 必须依赖前一个阶段 {prev}, "
                    f"实际依赖: {deps}"
                )

    def test_dependencies_acyclic(self, orch):
        """DAG 必须无环(通过拓扑排序验证)。"""
        # Kahn's algorithm
        in_degree = {p: 0 for p in orch.PHASE_DEPENDENCIES}
        for deps in orch.PHASE_DEPENDENCIES.values():
            for dep in deps:
                in_degree[dep] = in_degree.get(dep, 0)  # 已初始化
        # 计算入度
        in_degree = {p: 0 for p in orch.PHASE_DEPENDENCIES}
        adj: dict[str, list[str]] = {p: [] for p in orch.PHASE_DEPENDENCIES}
        for phase, deps in orch.PHASE_DEPENDENCIES.items():
            for dep in deps:
                adj[dep].append(phase)
                in_degree[phase] += 1

        queue = [p for p, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        assert visited == len(orch.PHASE_DEPENDENCIES), (
            f"PHASE_DEPENDENCIES 存在环: 只能拓扑排序 {visited}/"
            f"{len(orch.PHASE_DEPENDENCIES)} 个节点"
        )


# ════════════════════════════════════════════════════════════════
# C. PHASE_FUNCS 覆盖所有阶段
# ════════════════════════════════════════════════════════════════


class TestPhaseFuncs:
    """验证 PHASE_FUNCS 字典覆盖所有阶段且可调用。"""

    def test_phase_funcs_cover_all_phases(self, orch):
        """PHASE_FUNCS 必须覆盖所有 16 个阶段。"""
        for phase_name, _ in orch.PHASES:
            assert phase_name in orch.PHASE_FUNCS, (
                f"PHASE_FUNCS 缺少阶段: {phase_name}"
            )

    def test_phase_funcs_are_callable(self, orch):
        """每个 PHASE_FUNCS 值必须是可调用对象。"""
        for phase_name, _ in orch.PHASES:
            assert callable(orch.PHASE_FUNCS[phase_name]), (
                f"PHASE_FUNCS[{phase_name}] 不是可调用对象"
            )


# ════════════════════════════════════════════════════════════════
# D. ALLOWED_AFTER_FAILURE 正确性
# ════════════════════════════════════════════════════════════════


class TestAllowedAfterFailure:
    """验证 ALLOWED_AFTER_FAILURE 集合(cleanup 和诊断采集阶段)。"""

    def test_allowed_after_failure_is_set(self, orch):
        """ALLOWED_AFTER_FAILURE 必须是 set 类型。"""
        assert isinstance(orch.ALLOWED_AFTER_FAILURE, set), (
            f"ALLOWED_AFTER_FAILURE 必须是 set, "
            f"实际: {type(orch.ALLOWED_AFTER_FAILURE)}"
        )

    def test_allowed_after_failure_contains_cleanup_and_evidence(self, orch):
        """ALLOWED_AFTER_FAILURE 必须包含 cleanup 和 evidence_signing 阶段。"""
        assert "final_identity_and_cleanup" in orch.ALLOWED_AFTER_FAILURE, (
            "ALLOWED_AFTER_FAILURE 必须包含 final_identity_and_cleanup (cleanup)"
        )
        assert "evidence_signing" in orch.ALLOWED_AFTER_FAILURE, (
            "ALLOWED_AFTER_FAILURE 必须包含 evidence_signing (诊断采集)"
        )

    def test_allowed_after_failure_only_contains_valid_phases(self, orch):
        """ALLOWED_AFTER_FAILURE 中的阶段必须都在 PHASES 中。"""
        phase_names = {name for name, _ in orch.PHASES}
        for p in orch.ALLOWED_AFTER_FAILURE:
            assert p in phase_names, (
                f"ALLOWED_AFTER_FAILURE 包含不存在的阶段: {p}"
            )


# ════════════════════════════════════════════════════════════════
# E. PhaseResult DAG 字段完整性
# ════════════════════════════════════════════════════════════════


class TestPhaseResultDagFields:
    """验证 PhaseResult 数据类包含 R73 §5.15 DAG 字段。"""

    REQUIRED_DAG_FIELDS = [
        "depends_on",
        "started_at",
        "completed_at",
        "blocking_reason",
    ]

    def test_phase_result_has_dag_fields(self, orch):
        """PhaseResult 必须包含所有 DAG 字段。"""
        from dataclasses import fields
        field_names = {f.name for f in fields(orch.PhaseResult)}
        for dag_field in self.REQUIRED_DAG_FIELDS:
            assert dag_field in field_names, (
                f"PhaseResult 缺少 DAG 字段: {dag_field}"
            )

    def test_phase_result_depends_on_default_factory(self, orch):
        """depends_on 默认值必须是空 list(可变默认值用 default_factory)。"""
        from dataclasses import fields
        depends_on_field = next(
            f for f in fields(orch.PhaseResult) if f.name == "depends_on"
        )
        assert depends_on_field.default_factory is not None, (
            "depends_on 必须使用 default_factory(可变默认值)"
        )

    def test_phase_result_blocking_reason_nullable(self, orch):
        """blocking_reason 默认值必须为 None(仅 skipped/fail 时填充)。"""
        from dataclasses import fields
        blocking_field = next(
            f for f in fields(orch.PhaseResult) if f.name == "blocking_reason"
        )
        assert blocking_field.default is None, (
            f"blocking_reason 默认值必须为 None, 实际: {blocking_field.default}"
        )

    def test_phase_result_status_values(self, orch):
        """PhaseResult.status 必须支持 pass/fail/skipped 三种状态。"""
        # 通过构造不同状态验证
        for status in ("pass", "fail", "skipped"):
            result = orch.PhaseResult(
                phase="test",
                description="test",
                status=status,
                timestamp="2026-01-01T00:00:00+00:00",
                duration_seconds=0.0,
            )
            assert result.status == status


# ════════════════════════════════════════════════════════════════
# F. _pass_result / _fail_result / _skipped_result 填充 DAG 字段
# ════════════════════════════════════════════════════════════════


class TestResultHelpersDagFields:
    """验证结果构造辅助函数正确填充 DAG 字段。"""

    def test_pass_result_populates_dag_fields(self, orch):
        """_pass_result 必须填充 depends_on/started_at/completed_at。"""
        import time
        started = time.time()
        started_at = "2026-01-01T00:00:00+00:00"
        result = orch._pass_result(
            phase="preflight",
            description="test",
            started=started,
            started_at=started_at,
        )
        assert result.status == "pass"
        assert result.depends_on == orch.PHASE_DEPENDENCIES.get("preflight", [])
        assert result.started_at == started_at
        assert result.completed_at != ""
        assert result.blocking_reason is None

    def test_fail_result_populates_dag_fields(self, orch):
        """_fail_result 必须填充 depends_on/started_at/completed_at/blocking_reason。"""
        import time
        started = time.time()
        started_at = "2026-01-01T00:00:00+00:00"
        error_msg = "test failure"
        result = orch._fail_result(
            phase="preflight",
            description="test",
            started=started,
            started_at=started_at,
            error=error_msg,
        )
        assert result.status == "fail"
        assert result.depends_on == orch.PHASE_DEPENDENCIES.get("preflight", [])
        assert result.started_at == started_at
        assert result.completed_at != ""
        assert result.blocking_reason == error_msg
        assert result.error == error_msg

    def test_skipped_result_populates_dag_fields(self, orch):
        """_skipped_result 必须填充 depends_on/blocking_reason。"""
        reason = "上游失败"
        result = orch._skipped_result(
            phase="evidence_signing",
            description="test",
            blocking_reason=reason,
        )
        assert result.status == "skipped"
        assert result.depends_on == orch.PHASE_DEPENDENCIES.get("evidence_signing", [])
        assert result.blocking_reason == reason
        assert result.error == reason
        assert result.duration_seconds == 0.0


# ════════════════════════════════════════════════════════════════
# G. main() DAG 失败传播规则(模拟)
# ════════════════════════════════════════════════════════════════


class TestDagFailurePropagation:
    """验证 main() 函数 DAG 失败传播规则。

    R73 §5.15 要求:
        - 任一阶段失败总状态立即标记 failure
        - 仅 ALLOWED_AFTER_FAILURE 阶段可在上游失败后执行
        - 其余阶段必须 skipped(blocking_reason 记录上游失败阶段)
        - cleanup 成功不得覆盖原始 failure
        - 不允许 continue-on-error 影响门禁结论
    """

    def _make_pass_result(self, orch, phase_name):
        """构造 pass 结果。"""
        return orch.PhaseResult(
            phase=phase_name,
            description=next((d for n, d in orch.PHASES if n == phase_name), ""),
            status="pass",
            timestamp="2026-01-01T00:00:00+00:00",
            duration_seconds=0.1,
            depends_on=orch.PHASE_DEPENDENCIES.get(phase_name, []),
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:00+00:00",
        )

    def _make_fail_result(self, orch, phase_name, error="simulated failure"):
        """构造 fail 结果。"""
        return orch.PhaseResult(
            phase=phase_name,
            description=next((d for n, d in orch.PHASES if n == phase_name), ""),
            status="fail",
            timestamp="2026-01-01T00:00:00+00:00",
            duration_seconds=0.1,
            depends_on=orch.PHASE_DEPENDENCIES.get(phase_name, []),
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:00+00:00",
            error=error,
            blocking_reason=error,
        )

    def _patch_all_phases(self, orch, phase_results: dict[str, orch.PhaseResult]):
        """patch PHASE_FUNCS 使每个阶段返回指定结果。

        Args:
            phase_results: {phase_name: PhaseResult}
                          未指定的阶段返回 pass
        """
        patched_funcs = {}
        for phase_name, _ in orch.PHASES:
            if phase_name in phase_results:
                result = phase_results[phase_name]
                patched_funcs[phase_name] = lambda timeout, _r=result: _r
            else:
                result = self._make_pass_result(orch, phase_name)
                patched_funcs[phase_name] = lambda timeout, _r=result: _r
        return patched_funcs

    def test_all_pass_returns_0(self, orch):
        """全部通过时返回 0。"""
        patched = self._patch_all_phases(orch, {})
        with patch.dict(orch.PHASE_FUNCS, patched, clear=True):
            exit_code = orch.main([])
        assert exit_code == 0, (
            f"全部通过应返回 0, 实际: {exit_code}"
        )

    def test_preflight_fail_skips_all_downstream(self, orch):
        """preflight 失败时,所有下游非 ALLOWED 阶段必须 skipped。"""
        fail_result = self._make_fail_result(orch, "preflight")
        patched = self._patch_all_phases(orch, {"preflight": fail_result})
        # 对于 ALLOWED_AFTER_FAILURE 阶段,即使上游失败也应执行(返回 pass)
        # 但由于 preflight 失败,start_infrastructure 等中间阶段应被 skipped

        with patch.dict(orch.PHASE_FUNCS, patched, clear=True):
            # 捕获 main 函数的输出(通过捕获 print)
            from io import StringIO
            captured_stderr = StringIO()
            captured_stdout = StringIO()
            with patch("sys.stderr", captured_stderr), \
                 patch("sys.stdout", captured_stdout):
                exit_code = orch.main([])

        assert exit_code == 1, (
            f"preflight 失败应返回 1, 实际: {exit_code}"
        )

        # 验证 stderr 输出包含失败信息
        stderr_output = captured_stderr.getvalue()
        assert "FAIL" in stderr_output or "失败" in stderr_output, (
            f"stderr 应包含失败信息: {stderr_output}"
        )

    def test_middle_phase_fail_propagates_to_downstream(self, orch):
        """中间阶段失败时,所有下游非 ALLOWED 阶段必须 skipped。

        full_backup_to_r2 失败 → blank_isolated_restore / restore_integrity /
        actual_switch / ... / restart_and_pending_recovery 必须 skipped。
        final_identity_and_cleanup 和 evidence_signing 仍执行(ALLOWED)。
        """
        fail_result = self._make_fail_result(orch, "full_backup_to_r2")
        patched = self._patch_all_phases(orch, {"full_backup_to_r2": fail_result})

        # 记录哪些阶段被实际调用
        called_phases: list[str] = []
        original_patched = {}
        for phase_name, func in patched.items():
            def make_wrapper(name, f):
                def wrapper(timeout):
                    called_phases.append(name)
                    return f(timeout)
                return wrapper
            original_patched[phase_name] = make_wrapper(phase_name, func)

        with patch.dict(orch.PHASE_FUNCS, original_patched, clear=True):
            from io import StringIO
            captured_stderr = StringIO()
            with patch("sys.stderr", captured_stderr):
                exit_code = orch.main([])

        assert exit_code == 1, (
            f"full_backup_to_r2 失败应返回 1, 实际: {exit_code}"
        )

        # full_backup_to_r2 失败后,其下游非 ALLOWED 阶段不应被调用
        # 而应被 skipped(不调用 phase_func)
        downstream_non_allowed = [
            "blank_isolated_restore",
            "restore_integrity_and_target_identity",
            "actual_switch",
            "real_product_transaction_after_switch",
            "fault_injection",
            "actual_rollback",
            "real_product_transaction_after_rollback",
            "sigterm_with_inflight_message",
            "restart_and_pending_recovery",
        ]
        for phase in downstream_non_allowed:
            assert phase not in called_phases, (
                f"阶段 {phase} 应被 skipped(上游 full_backup_to_r2 失败),"
                f"但被实际调用了"
            )

        # ALLOWED_AFTER_FAILURE 阶段应被调用(即使上游失败)
        assert "final_identity_and_cleanup" in called_phases, (
            "final_identity_and_cleanup (ALLOWED_AFTER_FAILURE) "
            "应在上游失败后仍执行"
        )
        assert "evidence_signing" in called_phases, (
            "evidence_signing (ALLOWED_AFTER_FAILURE) "
            "应在上游失败后仍执行"
        )

    def test_cleanup_pass_does_not_override_failure(self, orch):
        """cleanup 成功不得覆盖原始 failure(overall_passed 不可逆)。"""
        fail_result = self._make_fail_result(orch, "full_backup_to_r2")
        # final_identity_and_cleanup 返回 pass(cleanup 成功)
        cleanup_pass = self._make_pass_result(orch, "final_identity_and_cleanup")
        evidence_pass = self._make_pass_result(orch, "evidence_signing")
        patched = self._patch_all_phases(orch, {
            "full_backup_to_r2": fail_result,
            "final_identity_and_cleanup": cleanup_pass,
            "evidence_signing": evidence_pass,
        })

        with patch.dict(orch.PHASE_FUNCS, patched, clear=True):
            from io import StringIO
            captured_stderr = StringIO()
            with patch("sys.stderr", captured_stderr):
                exit_code = orch.main([])

        # 即使 cleanup 成功,exit_code 仍为 1(原始 failure 不可逆)
        assert exit_code == 1, (
            "cleanup 成功不得覆盖原始 failure, 应返回 1"
        )

    def test_skipped_phases_have_blocking_reason(self, orch):
        """skipped 阶段必须有 blocking_reason 记录上游失败阶段。"""
        fail_result = self._make_fail_result(orch, "preflight")
        patched = self._patch_all_phases(orch, {"preflight": fail_result})

        # 使用 wraps 包装原始 _skipped_result,记录调用参数
        with patch.dict(orch.PHASE_FUNCS, patched, clear=True), \
             patch.object(orch, "_skipped_result", wraps=orch._skipped_result) as mock_skipped:
            from io import StringIO
            captured_stderr = StringIO()
            with patch("sys.stderr", captured_stderr):
                orch.main([])

        # 从 mock 调用记录中提取 skipped 阶段信息
        # mock_skipped.call_args_list 每个元素是 call(phase=..., description=..., blocking_reason=...)
        skipped_calls = mock_skipped.call_args_list
        assert len(skipped_calls) > 0, (
            "preflight 失败后应至少有一个下游阶段被 skipped"
        )

        # 提取被 skipped 的阶段名和 blocking_reason
        skipped_info: list[tuple[str, str]] = []
        for call_obj in skipped_calls:
            # call_obj 是 unittest.mock.call 对象
            # 关键字参数: phase=..., description=..., blocking_reason=...
            kwargs = call_obj.kwargs
            phase = kwargs.get("phase", "")
            reason = kwargs.get("blocking_reason", "")
            # 过滤掉 --keep-on-success 的跳过(本测试未设置该标志,但防御性检查)
            if "keep-on-success" not in reason:
                skipped_info.append((phase, reason))

        # 至少有一些阶段被 skipped(除了 ALLOWED_AFTER_FAILURE)
        # preflight 之后到 final_identity_and_cleanup 之前的阶段都应被 skipped
        expected_skipped = [
            "start_infrastructure",
            "start_application_roles",
            "real_product_transaction_before_backup",
            "full_backup_to_r2",
            "blank_isolated_restore",
            "restore_integrity_and_target_identity",
            "actual_switch",
            "real_product_transaction_after_switch",
            "fault_injection",
            "actual_rollback",
            "real_product_transaction_after_rollback",
            "sigterm_with_inflight_message",
            "restart_and_pending_recovery",
        ]
        skipped_phases = {phase for phase, _ in skipped_info}
        for phase in expected_skipped:
            assert phase in skipped_phases, (
                f"阶段 {phase} 应被 skipped, "
                f"实际 skipped: {skipped_phases}"
            )

        # 每个 skipped 结果必须有 blocking_reason 提到上游失败
        for phase, reason in skipped_info:
            if phase in expected_skipped:
                assert reason, (
                    f"skipped 阶段 {phase} 缺少 blocking_reason"
                )
                assert "上游" in reason or "skipped" in reason, (
                    f"skipped 阶段 {phase} 的 blocking_reason 应提到上游失败: "
                    f"{reason}"
                )

    def test_no_continue_on_error_affects_gate(self, orch):
        """不允许 continue-on-error 影响门禁结论。

        验证:即使后续阶段全部 pass,只要有一个阶段 fail,exit_code 必须为 1。
        """
        # 仅 preflight 失败,其余全部 pass(但下游会被 skipped)
        fail_result = self._make_fail_result(orch, "preflight")
        patched = self._patch_all_phases(orch, {"preflight": fail_result})

        with patch.dict(orch.PHASE_FUNCS, patched, clear=True):
            from io import StringIO
            captured_stderr = StringIO()
            with patch("sys.stderr", captured_stderr):
                exit_code = orch.main([])

        assert exit_code == 1, (
            "即使后续阶段 pass,只要存在 fail,exit_code 必须为 1"
        )

        # 验证 stderr 输出包含 DAG 失败统计
        stderr_output = captured_stderr.getvalue()
        assert "DAG 失败" in stderr_output or "阶段失败" in stderr_output, (
            f"stderr 应包含 DAG 失败统计: {stderr_output}"
        )


# ════════════════════════════════════════════════════════════════
# H. evidence 输出包含 DAG 元数据
# ════════════════════════════════════════════════════════════════


class TestEvidenceDagMetadata:
    """验证 _build_evidence 输出包含 R73 §5.15 DAG 元数据。"""

    def test_evidence_contains_dag_fields(self, orch):
        """evidence 输出必须包含 dag_enforced / phase_dependencies / allowed_after_failure。"""
        results = [
            orch._pass_result(
                phase="preflight",
                description="test",
                started=0.0,
                started_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        evidence = orch._build_evidence(
            results=results,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            overall_passed=True,
        )
        assert evidence["dag_enforced"] is True, (
            "evidence 必须包含 dag_enforced=True"
        )
        assert "phase_dependencies" in evidence, (
            "evidence 必须包含 phase_dependencies"
        )
        assert "allowed_after_failure" in evidence, (
            "evidence 必须包含 allowed_after_failure"
        )
        assert evidence["schema_version"] == "r73-sec5.15", (
            f"schema_version 必须为 r73-sec5.15, "
            f"实际: {evidence['schema_version']}"
        )

    def test_evidence_phase_summary_includes_dag_fields(self, orch):
        """evidence phase_summary 必须包含 depends_on 和 blocking_reason。"""
        results = [
            orch._pass_result(
                phase="preflight",
                description="test",
                started=0.0,
                started_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        evidence = orch._build_evidence(
            results=results,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            overall_passed=True,
        )
        summary = evidence["phase_summary"][0]
        assert "depends_on" in summary, (
            "phase_summary 必须包含 depends_on"
        )
        assert "blocking_reason" in summary, (
            "phase_summary 必须包含 blocking_reason"
        )


# ════════════════════════════════════════════════════════════════
# I. 阶段 16 诊断 envelope fail-closed 聚合
# ════════════════════════════════════════════════════════════════


class TestDiagnosticEnvelopeConclusion:
    """验证阶段级诊断 envelope 不会在上游失败后声称可晋级。"""

    @staticmethod
    def _result(orch, phase_name: str, status: str):
        return orch.PhaseResult(
            phase=phase_name,
            description="test",
            status=status,
            timestamp="2026-01-01T00:00:00+00:00",
            duration_seconds=0.1,
        )

    def _required_results(self, orch, status_overrides=None):
        status_overrides = status_overrides or {}
        return [
            self._result(
                orch,
                phase_name,
                status_overrides.get(phase_name, "pass"),
            )
            for phase_name, _ in orch.PHASES
            if phase_name != "evidence_signing"
        ]

    def test_all_required_pass_yields_success(self, orch):
        conclusion, eligible, details = (
            orch._aggregate_required_phase_conclusion(
                self._required_results(orch)
            )
        )
        assert conclusion == "success"
        assert eligible is True
        assert details["all_required_phases_passed"] is True
        assert details["missing_required_phases"] == []
        assert details["non_pass_required_phases"] == {}

    @pytest.mark.parametrize("status", ["fail", "skipped"])
    def test_fail_or_skipped_yields_failure(self, orch, status):
        conclusion, eligible, details = (
            orch._aggregate_required_phase_conclusion(
                self._required_results(
                    orch,
                    {"start_infrastructure": status},
                )
            )
        )
        assert conclusion == "failure"
        assert eligible is False
        assert details["all_required_phases_passed"] is False
        assert details["non_pass_required_phases"] == {
            "start_infrastructure": status,
        }

    def test_missing_or_duplicate_required_phase_yields_failure(self, orch):
        results = self._required_results(orch)
        results = [r for r in results if r.phase != "actual_switch"]
        results.append(self._result(orch, "preflight", "pass"))

        conclusion, eligible, details = (
            orch._aggregate_required_phase_conclusion(results)
        )
        assert conclusion == "failure"
        assert eligible is False
        assert details["missing_required_phases"] == ["actual_switch"]
        assert details["duplicate_required_phases"] == {
            "preflight": ["pass", "pass"],
        }

    def test_phase_evidence_signing_uses_failed_dag_conclusion(
        self, orch, monkeypatch
    ):
        captured: dict[str, object] = {}

        class EnvelopeModule:
            @staticmethod
            def build_evidence_envelope(**kwargs):
                captured.update(kwargs)
                return {
                    "gate_level": kwargs["gate_level"],
                    "overall_conclusion": kwargs["overall_conclusion"],
                    "promotion_eligible": kwargs["promotion_eligible"],
                }

            @staticmethod
            def validate_envelope(envelope):
                return True, []

        class Spec:
            loader = MagicMock()

        Spec.loader.exec_module = lambda module: None
        monkeypatch.setattr(orch, "_docker_available", lambda: True)
        monkeypatch.setattr(orch, "_get_source_sha", lambda: "a" * 40)
        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda *args, **kwargs: Spec(),
        )
        monkeypatch.setattr(
            importlib.util,
            "module_from_spec",
            lambda spec: EnvelopeModule,
        )
        monkeypatch.setenv(
            "TGJIEMA_IMAGE",
            "ghcr.io/maxiuquan/tgjiema@sha256:" + "b" * 64,
        )
        monkeypatch.setenv("R73_RUNTIME_CONFIG_DIGEST", "sha256:" + "c" * 64)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_REF", "refs/tags/rc-v1.0.86")
        orch._DAG_RESULTS_CONTEXT.clear()
        orch._DAG_RESULTS_CONTEXT.extend(
            self._required_results(
                orch,
                {"start_infrastructure": "fail"},
            )
        )

        result = orch.phase_evidence_signing(timeout=10)

        assert result.status == "pass"
        assert captured["overall_conclusion"] == "failure"
        assert captured["promotion_eligible"] is False
        assert result.evidence["envelope"]["promotion_eligible"] is False

    def test_phase_evidence_signing_only_promotable_when_all_pass(
        self, orch, monkeypatch
    ):
        captured: dict[str, object] = {}

        class EnvelopeModule:
            @staticmethod
            def build_evidence_envelope(**kwargs):
                captured.update(kwargs)
                return {
                    "gate_level": kwargs["gate_level"],
                    "overall_conclusion": kwargs["overall_conclusion"],
                    "promotion_eligible": kwargs["promotion_eligible"],
                }

            @staticmethod
            def validate_envelope(envelope):
                return True, []

        class Spec:
            loader = MagicMock()

        Spec.loader.exec_module = lambda module: None
        monkeypatch.setattr(orch, "_docker_available", lambda: True)
        monkeypatch.setattr(orch, "_get_source_sha", lambda: "a" * 40)
        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda *args, **kwargs: Spec(),
        )
        monkeypatch.setattr(
            importlib.util,
            "module_from_spec",
            lambda spec: EnvelopeModule,
        )
        monkeypatch.setenv(
            "TGJIEMA_IMAGE",
            "ghcr.io/maxiuquan/tgjiema@sha256:" + "b" * 64,
        )
        monkeypatch.setenv("R73_RUNTIME_CONFIG_DIGEST", "sha256:" + "c" * 64)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_REF", "refs/tags/rc-v1.0.86")
        orch._DAG_RESULTS_CONTEXT.clear()
        orch._DAG_RESULTS_CONTEXT.extend(self._required_results(orch))

        result = orch.phase_evidence_signing(timeout=10)

        assert result.status == "pass"
        assert captured["overall_conclusion"] == "success"
        assert captured["promotion_eligible"] is True
