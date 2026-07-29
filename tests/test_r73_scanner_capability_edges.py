"""R73 §5.8 (P1-02): scanner capability-edge policy — 测试套件。

R73 §5.8 整改要求:
    旧版 scanner(``scripts/check_restore_no_legacy_writer.py``)依赖
    ``PRECISE_WHITELIST`` 的 source_digest(函数源码 SHA-256)做授权。
    问题:
    1. source_digest 绑定字面源码,任何注释/格式调整都导致 digest 失配
    2. 团队必须频繁运行 regenerate_scanner_whitelist_digests.py
    3. digest 不捕捉"能力边"(caller→callee 关系),只绑定源码文本
    4. 跨 Python 版本 ast_dump 不稳定(R67 P1-07 hotfix 已用 source_digest 兜底)

R73 §5.8 新方案(capability-edge policy):
    1. 用不可变 manifest(``CAPABILITY_EDGE_POLICY``)定义允许的 caller→callee 边
    2. AST 遍历(``_find_unauthorized_capability_edges``)验证所有 legacy writer
       调用都有匹配的边
    3. 检测动态分派(``_detect_dynamic_dispatch``)防止绕过:
       getattr/globals()/__import__/importlib.import_module/eval/exec
    4. 不依赖源码文本,函数重构(重命名变量/调整格式)不影响授权
    5. fail-closed:未知边 → 违规(强制团队更新 policy)

测试覆盖矩阵:
    A. Policy manifest 加载与结构验证
       - load_capability_edge_policy() 返回 dict
       - version 字段为 CAPABILITY_EDGE_POLICY_VERSION
       - edges 列表非空,每条边含必需字段
       - dynamic_dispatch_patterns 包含 6 个模式
    B. _find_module_name 路径转换
       - services/db_restore.py → services.db_restore
       - bots/admin_bot/handlers.py → bots.admin_bot.handlers
    C. _is_capability_edge_allowed 授权判定
       - 已授权边 → True
       - 未知 caller_module → False
       - 未知 caller_function → False
       - 已知 caller 但 callee 不在 allowed_callees → False
       - 模块级调用(caller_function=None)→ False
    D. _find_unauthorized_capability_edges AST 遍历
       - 授权调用 → 空列表
       - 未授权调用 → 包含违规条目
       - 违规条目结构正确(line/col/func/enclosing/caller_module/callee/violation_type)
    E. _detect_dynamic_dispatch 动态分派检测
       - getattr(...) → DYNAMIC_DISPATCH 违规
       - globals()[...]() → DYNAMIC_DISPATCH 违规
       - __import__(...) → DYNAMIC_DISPATCH 违规
       - importlib.import_module(...) → DYNAMIC_DISPATCH 违规
       - eval(...) → DYNAMIC_DISPATCH 违规
       - exec(...) → DYNAMIC_DISPATCH 违规
       - 无动态分派的代码 → 空列表
    F. 与 PRECISE_WHITELIST 加法式兼容
       - CAPABILITY_EDGE_POLICY 与 PRECISE_WHITELIST 共存
       - 新方案不破坏旧 source_digest 流程
    G. 常量与导出
       - CAPABILITY_EDGE_POLICY_VERSION 常量
       - DYNAMIC_DISPATCH_VIOLATION / UNAUTHORIZED_EDGE_VIOLATION 常量
"""
from __future__ import annotations

import ast
import importlib.util
import sys
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


def _import_gate_mod():
    """导入 scanner 模块(每次重新导入以避免状态污染)。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_restore_no_legacy_writer as gate_mod
        return gate_mod
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def gate():
    """提供 scanner 模块实例(模块级缓存)。"""
    return _import_gate_mod()


@pytest.fixture(scope="module")
def policy(gate):
    """提供 capability-edge policy dict(模块级缓存)。"""
    return gate.load_capability_edge_policy()


def _parse(source: str) -> ast.Module:
    """解析 Python 源码为 AST(语法错误时抛 SyntaxError)。"""
    return ast.parse(source)


def _build_parent_map(tree: ast.AST, gate) -> dict:
    """构建 AST parent map(复用 scanner 内部函数)。"""
    return gate._build_parent_map(tree)


# ════════════════════════════════════════════════════════════════
# A. Policy manifest 加载与结构验证
# ════════════════════════════════════════════════════════════════


class TestCapabilityEdgePolicyLoading:
    """R73 §5.8: load_capability_edge_policy() 加载与结构验证。"""

    def test_load_capability_edge_policy_exists(self, gate):
        """load_capability_edge_policy 函数应存在且可调用。"""
        assert hasattr(gate, "load_capability_edge_policy")
        assert callable(gate.load_capability_edge_policy)

    def test_load_capability_edge_policy_returns_dict(self, policy):
        """policy 应为 dict。"""
        assert isinstance(policy, dict)

    def test_policy_has_version(self, policy, gate):
        """policy 应含 version 字段,等于 CAPABILITY_EDGE_POLICY_VERSION。"""
        assert "version" in policy
        assert policy["version"] == gate.CAPABILITY_EDGE_POLICY_VERSION
        assert isinstance(policy["version"], int)

    def test_policy_version_is_one(self, policy):
        """初始版本应为 1。"""
        assert policy["version"] == 1, (
            "R73 §5.8 初始 capability-edge policy 版本应为 1"
        )

    def test_policy_has_description(self, policy):
        """policy 应含 description 字段(人类可读说明)。"""
        assert "description" in policy
        assert isinstance(policy["description"], str)
        assert "R73 §5.8" in policy["description"], (
            "description 应引用 R73 §5.8 来源"
        )

    def test_policy_has_edges_list(self, policy):
        """policy 应含 edges 列表(允许的 caller→callee 边)。"""
        assert "edges" in policy
        assert isinstance(policy["edges"], list)
        assert len(policy["edges"]) > 0, "edges 列表不应为空"

    def test_policy_edges_have_required_fields(self, policy):
        """每条边应含 caller_module/caller_function/allowed_callees/reason 字段。"""
        required_keys = {
            "caller_module",
            "caller_function",
            "allowed_callees",
            "reason",
        }
        for edge in policy["edges"]:
            assert isinstance(edge, dict), f"边不是 dict: {type(edge)}"
            missing = required_keys - set(edge.keys())
            assert not missing, f"边缺少必需字段: {missing}; 边内容: {edge}"
            # 字段类型检查
            assert isinstance(edge["caller_module"], str)
            assert isinstance(edge["caller_function"], str)
            assert isinstance(edge["allowed_callees"], tuple)
            assert isinstance(edge["reason"], str)
            # allowed_callees 不能为空(每条边至少授权一个 callee)
            assert len(edge["allowed_callees"]) > 0, (
                f"边的 allowed_callees 不应为空: {edge}"
            )

    def test_policy_edges_cover_known_callers(self, policy):
        """policy 应覆盖已知的 legacy writer 调用方(7 条边)。"""
        # 从 PRECISE_WHITELIST 推断已知调用方
        # (file, function) → 期望的 caller_module
        expected_edges = {
            ("services.db_restore", "run_restore"),
            ("services.db_restore", "main"),
            ("services.backup_dr_validate", "validate_and_restore_backup_strict"),
            ("services.backup_dr_validate", "_restore_preverified_payload"),
            ("services.restore_writer", "_restore_from_backup_data"),
            ("services.db_backup", "restore_from_backup"),
            ("services.command_bus", "_handler"),
        }
        actual_edges = {
            (e["caller_module"], e["caller_function"])
            for e in policy["edges"]
        }
        missing = expected_edges - actual_edges
        assert not missing, (
            f"capability-edge policy 缺少已知调用方边: {missing}"
        )

    def test_policy_has_dynamic_dispatch_patterns(self, policy):
        """policy 应含 dynamic_dispatch_patterns 字段(tuple[str])。"""
        assert "dynamic_dispatch_patterns" in policy
        assert isinstance(policy["dynamic_dispatch_patterns"], tuple)
        assert len(policy["dynamic_dispatch_patterns"]) > 0

    def test_policy_dynamic_dispatch_patterns_complete(self, policy):
        """dynamic_dispatch_patterns 应包含 6 个模式。"""
        expected_patterns = {
            "getattr",
            "globals",
            "__import__",
            "importlib.import_module",
            "eval",
            "exec",
        }
        actual_patterns = set(policy["dynamic_dispatch_patterns"])
        missing = expected_patterns - actual_patterns
        assert not missing, (
            f"dynamic_dispatch_patterns 缺少模式: {missing}"
        )

    def test_capability_edge_policy_constant_matches_loaded(self, gate, policy):
        """CAPABILITY_EDGE_POLICY 常量应与 load_capability_edge_policy() 返回一致。"""
        assert gate.CAPABILITY_EDGE_POLICY is policy, (
            "load_capability_edge_policy() 应直接返回 CAPABILITY_EDGE_POLICY 常量"
        )


# ════════════════════════════════════════════════════════════════
# B. _find_module_name 路径转换
# ════════════════════════════════════════════════════════════════


class TestFindModuleName:
    """R73 §5.8: _find_module_name — POSIX 路径 → Python 模块名转换。"""

    def test_find_module_name_exists(self, gate):
        """_find_module_name 函数应存在且可调用。"""
        assert hasattr(gate, "_find_module_name")
        assert callable(gate._find_module_name)

    def test_services_db_restore(self, gate):
        """services/db_restore.py → services.db_restore"""
        assert gate._find_module_name("services/db_restore.py") == "services.db_restore"

    def test_services_restore_writer(self, gate):
        """services/restore_writer.py → services.restore_writer"""
        assert gate._find_module_name("services/restore_writer.py") == "services.restore_writer"

    def test_services_backup_dr_validate(self, gate):
        """services/backup_dr_validate.py → services.backup_dr_validate"""
        assert gate._find_module_name("services/backup_dr_validate.py") == "services.backup_dr_validate"

    def test_services_db_backup(self, gate):
        """services/db_backup.py → services.db_backup"""
        assert gate._find_module_name("services/db_backup.py") == "services.db_backup"

    def test_services_command_bus(self, gate):
        """services/command_bus.py → services.command_bus"""
        assert gate._find_module_name("services/command_bus.py") == "services.command_bus"

    def test_nested_bots_path(self, gate):
        """bots/admin_bot/handlers.py → bots.admin_bot.handlers"""
        assert gate._find_module_name("bots/admin_bot/handlers.py") == "bots.admin_bot.handlers"

    def test_strips_py_suffix(self, gate):
        """应正确去除 .py 后缀。"""
        assert gate._find_module_name("foo.py") == "foo"
        assert gate._find_module_name("foo/bar.py") == "foo.bar"

    def test_no_py_suffix_unchanged(self, gate):
        """无 .py 后缀的路径应保持原样(仅替换分隔符)。"""
        assert gate._find_module_name("foo/bar") == "foo.bar"


# ════════════════════════════════════════════════════════════════
# C. _is_capability_edge_allowed 授权判定
# ════════════════════════════════════════════════════════════════


class TestIsCapabilityEdgeAllowed:
    """R73 §5.8: _is_capability_edge_allowed — 授权判定逻辑。"""

    def test_is_capability_edge_allowed_exists(self, gate):
        """_is_capability_edge_allowed 函数应存在且可调用。"""
        assert hasattr(gate, "_is_capability_edge_allowed")
        assert callable(gate._is_capability_edge_allowed)

    def test_module_level_call_not_allowed(self, gate, policy):
        """模块级调用(caller_function=None)永远不允许。"""
        result = gate._is_capability_edge_allowed(
            policy,
            caller_module="services.db_restore",
            caller_function=None,
            callee="run_restore",
        )
        assert result is False, (
            "模块级调用 legacy writer 永远不允许(fail-closed)"
        )

    def test_unknown_caller_module_not_allowed(self, gate, policy):
        """未知 caller_module 应 fail-closed 返回 False。"""
        result = gate._is_capability_edge_allowed(
            policy,
            caller_module="bots.admin_bot.handlers",
            caller_function="some_function",
            callee="run_restore",
        )
        assert result is False, (
            "未知 caller_module 应 fail-closed 不允许调用 legacy writer"
        )

    def test_unknown_caller_function_not_allowed(self, gate, policy):
        """未知 caller_function 应 fail-closed 返回 False。"""
        result = gate._is_capability_edge_allowed(
            policy,
            caller_module="services.db_restore",
            caller_function="unknown_function",
            callee="run_restore",
        )
        assert result is False

    def test_callee_not_in_allowed_callees(self, gate, policy):
        """callee 不在 allowed_callees 中应返回 False。"""
        # services.db_restore.run_restore 仅允许 validate_and_restore_backup_strict
        # 调用 _restore_from_backup_data 应失败
        result = gate._is_capability_edge_allowed(
            policy,
            caller_module="services.db_restore",
            caller_function="run_restore",
            callee="_restore_from_backup_data",
        )
        assert result is False, (
            "run_restore 仅允许调用 validate_and_restore_backup_strict,"
            "调用 _restore_from_backup_data 应被拒绝"
        )

    @pytest.mark.parametrize(
        "caller_module,caller_function,callee",
        [
            # db_restore.run_restore → validate_and_restore_backup_strict
            ("services.db_restore", "run_restore", "validate_and_restore_backup_strict"),
            # db_restore.main → run_restore
            ("services.db_restore", "main", "run_restore"),
            # backup_dr_validate.validate_and_restore_backup_strict → _restore_from_backup_data
            (
                "services.backup_dr_validate",
                "validate_and_restore_backup_strict",
                "_restore_from_backup_data",
            ),
            # backup_dr_validate._restore_preverified_payload → _restore_from_backup_data
            (
                "services.backup_dr_validate",
                "_restore_preverified_payload",
                "_restore_from_backup_data",
            ),
            # restore_writer._restore_from_backup_data → _restore_crdb_tables
            (
                "services.restore_writer",
                "_restore_from_backup_data",
                "_restore_crdb_tables",
            ),
            # restore_writer._restore_from_backup_data → _restore_sqlite_tables_to_db
            (
                "services.restore_writer",
                "_restore_from_backup_data",
                "_restore_sqlite_tables_to_db",
            ),
            # db_backup.restore_from_backup → validate_and_restore_backup_strict
            ("services.db_backup", "restore_from_backup", "validate_and_restore_backup_strict"),
            # command_bus._handler → restore_from_backup
            ("services.command_bus", "_handler", "restore_from_backup"),
        ],
    )
    def test_authorized_edges_allowed(self, gate, policy, caller_module, caller_function, callee):
        """policy 中已授权的边应返回 True。"""
        result = gate._is_capability_edge_allowed(
            policy,
            caller_module=caller_module,
            caller_function=caller_function,
            callee=callee,
        )
        assert result is True, (
            f"已授权边应允许: {caller_module}.{caller_function}() → {callee}()"
        )

    def test_empty_policy_returns_false(self, gate):
        """空 policy(无 edges)应 fail-closed 返回 False。"""
        empty_policy = {"version": 1, "edges": [], "dynamic_dispatch_patterns": ()}
        result = gate._is_capability_edge_allowed(
            empty_policy,
            caller_module="services.db_restore",
            caller_function="run_restore",
            callee="validate_and_restore_backup_strict",
        )
        assert result is False, "空 policy 应 fail-closed"

    def test_malformed_edge_skipped(self, gate):
        """结构不完整的边(缺字段)应被跳过,不影响其他边判定。"""
        malformed_policy = {
            "version": 1,
            "edges": [
                # 缺 allowed_callees 字段
                {"caller_module": "services.db_restore", "caller_function": "run_restore"},
                # 正确边
                {
                    "caller_module": "services.db_restore",
                    "caller_function": "run_restore",
                    "allowed_callees": ("validate_and_restore_backup_strict",),
                    "reason": "test",
                },
            ],
            "dynamic_dispatch_patterns": (),
        }
        result = gate._is_capability_edge_allowed(
            malformed_policy,
            caller_module="services.db_restore",
            caller_function="run_restore",
            callee="validate_and_restore_backup_strict",
        )
        assert result is True, "结构不完整的边应被跳过,但正确边仍应匹配"


# ════════════════════════════════════════════════════════════════
# D. _find_unauthorized_capability_edges AST 遍历
# ════════════════════════════════════════════════════════════════


class TestFindUnauthorizedCapabilityEdges:
    """R73 §5.8: _find_unauthorized_capability_edges — AST 未授权边检测。"""

    def test_find_unauthorized_capability_edges_exists(self, gate):
        """_find_unauthorized_capability_edges 函数应存在且可调用。"""
        assert hasattr(gate, "_find_unauthorized_capability_edges")
        assert callable(gate._find_unauthorized_capability_edges)

    def test_authorized_call_no_violations(self, gate, policy):
        """授权调用应返回空违规列表。"""
        # services/db_restore.py 中 run_restore() 调用 validate_and_restore_backup_strict
        # 是 policy 中已授权的边
        source = """
def run_restore(backup_id):
    return validate_and_restore_backup_strict(backup_id)
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"validate_and_restore_backup_strict"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.db_restore",
            policy=policy,
        )
        assert violations == [], (
            "已授权边(run_restore → validate_and_restore_backup_strict)"
            "不应产生违规"
        )

    def test_unauthorized_call_detected(self, gate, policy):
        """未授权调用应被检测。"""
        # bots.admin_bot.handlers 中调用 run_restore — 不在 policy 中
        source = """
def handle_restore_command(update, context):
    return run_restore(backup_id="abc")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="bots.admin_bot.handlers",
            policy=policy,
        )
        assert len(violations) == 1, (
            f"应检测到 1 个未授权调用,实际: {len(violations)}"
        )
        v = violations[0]
        assert v["func"] == "run_restore"
        assert v["enclosing"] == "handle_restore_command"
        assert v["caller_module"] == "bots.admin_bot.handlers"
        assert v["callee"] == "run_restore"
        assert v["violation_type"] == gate.UNAUTHORIZED_EDGE_VIOLATION

    def test_module_level_call_detected(self, gate, policy):
        """模块级调用 legacy writer 应被检测(caller_function=None)。"""
        source = """
run_restore(backup_id="abc")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.unknown_module",
            policy=policy,
        )
        assert len(violations) == 1, (
            "模块级调用 legacy writer 应被检测为未授权"
        )
        assert violations[0]["enclosing"] is None

    def test_callee_not_in_allowed_callees_detected(self, gate, policy):
        """caller 在 policy 中但 callee 不在 allowed_callees 应被检测。"""
        # services.db_restore.run_restore 仅允许 validate_and_restore_backup_strict
        # 调用 _restore_from_backup_data 应被检测
        source = """
def run_restore(backup_id):
    return _restore_from_backup_data(backup_id)
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"_restore_from_backup_data"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.db_restore",
            policy=policy,
        )
        assert len(violations) == 1, (
            "caller 已知但 callee 不在 allowed_callees 应被检测"
        )

    def test_violation_structure(self, gate, policy):
        """违规条目结构应包含所有必需字段。"""
        source = """
def bad_function():
    return run_restore(backup_id="abc")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.unknown",
            policy=policy,
        )
        assert len(violations) == 1
        v = violations[0]
        required_keys = {
            "line", "col", "func", "enclosing",
            "caller_module", "callee", "violation_type",
        }
        assert required_keys.issubset(set(v.keys())), (
            f"违规条目缺少字段: {required_keys - set(v.keys())}"
        )
        assert isinstance(v["line"], int)
        assert isinstance(v["col"], int)
        assert v["violation_type"] == gate.UNAUTHORIZED_EDGE_VIOLATION

    def test_multiple_violations(self, gate, policy):
        """多个未授权调用应全部被检测。"""
        source = """
def func_a():
    return run_restore(backup_id="a")

def func_b():
    return _restore_from_backup_data(data=b"abc")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore", "_restore_from_backup_data"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.unknown",
            policy=policy,
        )
        assert len(violations) == 2, (
            f"应检测到 2 个未授权调用,实际: {len(violations)}"
        )
        enclosing_set = {v["enclosing"] for v in violations}
        assert enclosing_set == {"func_a", "func_b"}

    def test_attribute_call_detected(self, gate, policy):
        """属性调用(db_restore.run_restore(...))也应被检测。"""
        source = """
def bad_handler():
    return db_restore.run_restore(backup_id="abc")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="bots.unknown",
            policy=policy,
        )
        assert len(violations) == 1, (
            "属性调用(db_restore.run_restore(...))应被检测"
        )
        assert violations[0]["func"] == "run_restore"

    def test_no_legacy_call_no_violations(self, gate, policy):
        """无 legacy writer 调用的代码应返回空违规列表。"""
        source = """
def normal_function():
    print("hello")
    return some_other_function(42)
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = {"run_restore", "_restore_from_backup_data"}
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.normal",
            policy=policy,
        )
        assert violations == []


# ════════════════════════════════════════════════════════════════
# E. _detect_dynamic_dispatch 动态分派检测
# ════════════════════════════════════════════════════════════════


class TestDetectDynamicDispatch:
    """R73 §5.8: _detect_dynamic_dispatch — 动态分派模式检测。"""

    def test_detect_dynamic_dispatch_exists(self, gate):
        """_detect_dynamic_dispatch 函数应存在且可调用。"""
        assert hasattr(gate, "_detect_dynamic_dispatch")
        assert callable(gate._detect_dynamic_dispatch)

    def test_getattr_detected(self, gate, policy):
        """getattr(...) 调用应被检测为 DYNAMIC_DISPATCH 违规。"""
        source = """
def dynamic_call():
    func = getattr(module, "run_restore")
    return func()
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        assert len(violations) >= 1, (
            "getattr(...) 应被检测为动态分派"
        )
        getattr_violations = [v for v in violations if v["pattern"] == "getattr"]
        assert len(getattr_violations) == 1
        v = getattr_violations[0]
        assert v["violation_type"] == gate.DYNAMIC_DISPATCH_VIOLATION
        assert v["caller_module"] == "services.suspicious"
        assert v["enclosing"] == "dynamic_call"

    def test_globals_detected(self, gate, policy):
        """globals()(...) 调用应被检测。"""
        source = """
def dynamic_call():
    return globals()["_restore_from_backup_data"]()
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        globals_violations = [v for v in violations if v["pattern"] == "globals"]
        assert len(globals_violations) == 1, (
            "globals()(...) 应被检测为动态分派"
        )
        assert globals_violations[0]["violation_type"] == gate.DYNAMIC_DISPATCH_VIOLATION

    def test_dunder_import_detected(self, gate, policy):
        """__import__(...) 调用应被检测。"""
        source = """
def dynamic_call():
    mod = __import__("services.db_restore")
    return mod.run_restore()
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        import_violations = [
            v for v in violations if v["pattern"] == "__import__"
        ]
        assert len(import_violations) == 1, (
            "__import__(...) 应被检测为动态分派"
        )

    def test_importlib_import_module_detected(self, gate, policy):
        """importlib.import_module(...) 调用应被检测。"""
        source = """
import importlib

def dynamic_call():
    mod = importlib.import_module("services.db_restore")
    return mod.run_restore()
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        importlib_violations = [
            v for v in violations if v["pattern"] == "importlib.import_module"
        ]
        assert len(importlib_violations) == 1, (
            "importlib.import_module(...) 应被检测为动态分派"
        )

    def test_eval_detected(self, gate, policy):
        """eval(...) 调用应被检测。"""
        source = """
def dynamic_call():
    return eval("run_restore(backup_id='abc')")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        eval_violations = [v for v in violations if v["pattern"] == "eval"]
        assert len(eval_violations) == 1, (
            "eval(...) 应被检测为动态分派"
        )

    def test_exec_detected(self, gate, policy):
        """exec(...) 调用应被检测。"""
        source = """
def dynamic_call():
    exec("run_restore(backup_id='abc')")
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        exec_violations = [v for v in violations if v["pattern"] == "exec"]
        assert len(exec_violations) == 1, (
            "exec(...) 应被检测为动态分派"
        )

    def test_no_dynamic_dispatch_no_violations(self, gate, policy):
        """无动态分派的代码应返回空违规列表。"""
        source = """
def normal_function():
    print("hello")
    x = len([1, 2, 3])
    return x
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.normal",
            policy=policy,
        )
        assert violations == [], (
            "无动态分派模式的代码不应产生违规"
        )

    def test_violation_structure(self, gate, policy):
        """动态分派违规条目结构应包含所有必需字段。"""
        source = """
def suspicious():
    return getattr(obj, "method")()
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        assert len(violations) == 1
        v = violations[0]
        required_keys = {
            "line", "col", "pattern", "enclosing",
            "caller_module", "violation_type",
        }
        assert required_keys.issubset(set(v.keys())), (
            f"动态分派违规条目缺少字段: {required_keys - set(v.keys())}"
        )
        assert isinstance(v["line"], int)
        assert isinstance(v["col"], int)
        assert v["violation_type"] == gate.DYNAMIC_DISPATCH_VIOLATION

    def test_multiple_dynamic_dispatches(self, gate, policy):
        """多个动态分派模式应全部被检测。"""
        source = """
def multi_dispatch():
    a = getattr(obj, "method")
    b = eval("code")
    c = exec("code")
    return a, b, c
"""
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        violations = gate._detect_dynamic_dispatch(
            tree, parent_map,
            caller_module="services.suspicious",
            policy=policy,
        )
        patterns = {v["pattern"] for v in violations}
        assert {"getattr", "eval", "exec"}.issubset(patterns), (
            f"应检测到 getattr/eval/exec 三个模式,实际: {patterns}"
        )


# ════════════════════════════════════════════════════════════════
# F. 与 PRECISE_WHITELIST 加法式兼容
# ════════════════════════════════════════════════════════════════


class TestAdditiveCompatibility:
    """R73 §5.8: 新 capability-edge policy 与旧 PRECISE_WHITELIST 共存(加法式)。"""

    def test_precise_whitelist_still_exists(self, gate):
        """PRECISE_WHITELIST 应仍存在(向后兼容)。"""
        assert hasattr(gate, "PRECISE_WHITELIST")
        assert isinstance(gate.PRECISE_WHITELIST, tuple)
        assert len(gate.PRECISE_WHITELIST) > 0

    def test_precise_whitelist_entries_have_source_digest(self, gate):
        """PRECISE_WHITELIST 条目仍保留 source_digest 字段(旧方案兜底)。"""
        for entry in gate.PRECISE_WHITELIST:
            assert "source_digest" in entry, (
                f"旧白名单条目应保留 source_digest 字段: {entry}"
            )
            assert "ast_signature" in entry
            assert "allowed_callees" in entry

    def test_both_mechanisms_coexist(self, gate):
        """两套机制(旧 source_digest + 新 capability-edge)应共存。"""
        # 旧机制
        assert hasattr(gate, "PRECISE_WHITELIST")
        assert hasattr(gate, "_is_call_allowed")
        # 新机制
        assert hasattr(gate, "CAPABILITY_EDGE_POLICY")
        assert hasattr(gate, "load_capability_edge_policy")
        assert hasattr(gate, "_is_capability_edge_allowed")
        assert hasattr(gate, "_find_unauthorized_capability_edges")
        assert hasattr(gate, "_detect_dynamic_dispatch")

    def test_legacy_check_function_still_works(self, gate):
        """旧 check() 函数应仍可调用(不破坏现有 CI 流程)。"""
        assert hasattr(gate, "check")
        assert callable(gate.check)
        # 注意:此处不调用 check() 以避免长扫描时间;
        # 仅验证函数存在且签名兼容

    def test_policy_edges_align_with_precise_whitelist(self, gate, policy):
        """新 policy 的边集合应与旧 PRECISE_WHITELIST 的调用关系对齐。

        每个 PRECISE_WHITELIST 条目应在 CAPABILITY_EDGE_POLICY 中有对应边。
        """
        for entry in gate.PRECISE_WHITELIST:
            file_rel = entry["file"]
            function = entry["function"]
            caller_module = gate._find_module_name(file_rel)
            # 在 policy 中查找对应边
            matching_edges = [
                e for e in policy["edges"]
                if e["caller_module"] == caller_module
                and e["caller_function"] == function
            ]
            assert len(matching_edges) >= 1, (
                f"PRECISE_WHITELIST 条目 ({file_rel}::{function}()) "
                f"在 CAPABILITY_EDGE_POLICY 中无对应边"
            )
            # 验证 allowed_callees 一致(顺序无关)
            policy_callees = set(matching_edges[0]["allowed_callees"])
            whitelist_callees = set(entry["allowed_callees"])
            assert policy_callees == whitelist_callees, (
                f"({file_rel}::{function}()) "
                f"policy callees {policy_callees} != "
                f"whitelist callees {whitelist_callees}"
            )


# ════════════════════════════════════════════════════════════════
# G. 常量与导出
# ════════════════════════════════════════════════════════════════


class TestConstantsAndExports:
    """R73 §5.8: 常量定义与导出验证。"""

    def test_capability_edge_policy_version_constant(self, gate):
        """CAPABILITY_EDGE_POLICY_VERSION 常量应存在。"""
        assert hasattr(gate, "CAPABILITY_EDGE_POLICY_VERSION")
        assert isinstance(gate.CAPABILITY_EDGE_POLICY_VERSION, int)
        assert gate.CAPABILITY_EDGE_POLICY_VERSION == 1

    def test_dynamic_dispatch_violation_constant(self, gate):
        """DYNAMIC_DISPATCH_VIOLATION 常量应存在。"""
        assert hasattr(gate, "DYNAMIC_DISPATCH_VIOLATION")
        assert isinstance(gate.DYNAMIC_DISPATCH_VIOLATION, str)
        assert gate.DYNAMIC_DISPATCH_VIOLATION == "DYNAMIC_DISPATCH"

    def test_unauthorized_edge_violation_constant(self, gate):
        """UNAUTHORIZED_EDGE_VIOLATION 常量应存在。"""
        assert hasattr(gate, "UNAUTHORIZED_EDGE_VIOLATION")
        assert isinstance(gate.UNAUTHORIZED_EDGE_VIOLATION, str)
        assert gate.UNAUTHORIZED_EDGE_VIOLATION == "UNAUTHORIZED_EDGE"

    def test_violation_constants_distinct(self, gate):
        """两种违规类型常量应不同(便于告警路由)。"""
        assert gate.DYNAMIC_DISPATCH_VIOLATION != gate.UNAUTHORIZED_EDGE_VIOLATION

    def test_capability_edge_policy_constant(self, gate):
        """CAPABILITY_EDGE_POLICY 常量应存在且为 dict。"""
        assert hasattr(gate, "CAPABILITY_EDGE_POLICY")
        assert isinstance(gate.CAPABILITY_EDGE_POLICY, dict)


# ════════════════════════════════════════════════════════════════
# H. 端到端集成 — 验证 R73 §5.8 在真实文件上工作
# ════════════════════════════════════════════════════════════════


class TestEndToEndIntegration:
    """R73 §5.8: 端到端集成 — 在真实生产文件上验证 capability-edge。"""

    def test_real_db_restore_no_unauthorized_edges(self, gate, policy):
        """services/db_restore.py 中所有 legacy writer 调用应被 policy 授权。"""
        db_restore_path = REPO_ROOT / "services" / "db_restore.py"
        if not db_restore_path.exists():
            pytest.skip("services/db_restore.py 不存在")
        source = db_restore_path.read_text(encoding="utf-8")
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = set(gate.LEGACY_WRITER_FUNDS_DEFAULT) | set(
            gate.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.db_restore",
            policy=policy,
        )
        # db_restore.py 中的调用应全部在 policy 中已授权
        # (run_restore → validate_and_restore_backup_strict, main → run_restore)
        assert violations == [], (
            "services/db_restore.py 中存在未授权的 capability 边: "
            f"{violations}"
        )

    def test_real_backup_dr_validate_no_unauthorized_edges(self, gate, policy):
        """services/backup_dr_validate.py 中所有 legacy writer 调用应被授权。"""
        path = REPO_ROOT / "services" / "backup_dr_validate.py"
        if not path.exists():
            pytest.skip("services/backup_dr_validate.py 不存在")
        source = path.read_text(encoding="utf-8")
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = set(gate.LEGACY_WRITER_FUNDS_DEFAULT) | set(
            gate.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.backup_dr_validate",
            policy=policy,
        )
        assert violations == [], (
            "services/backup_dr_validate.py 中存在未授权的 capability 边: "
            f"{violations}"
        )

    def test_real_restore_writer_no_unauthorized_edges(self, gate, policy):
        """services/restore_writer.py 中所有 legacy writer 调用应被授权。"""
        path = REPO_ROOT / "services" / "restore_writer.py"
        if not path.exists():
            pytest.skip("services/restore_writer.py 不存在")
        source = path.read_text(encoding="utf-8")
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = set(gate.LEGACY_WRITER_FUNDS_DEFAULT)
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.restore_writer",
            policy=policy,
        )
        assert violations == [], (
            "services/restore_writer.py 中存在未授权的 capability 边: "
            f"{violations}"
        )

    def test_real_db_backup_no_unauthorized_edges(self, gate, policy):
        """services/db_backup.py 中所有 legacy writer 调用应被授权。"""
        path = REPO_ROOT / "services" / "db_backup.py"
        if not path.exists():
            pytest.skip("services/db_backup.py 不存在")
        source = path.read_text(encoding="utf-8")
        tree = _parse(source)
        parent_map = _build_parent_map(tree, gate)
        legacy_funds = set(gate.LEGACY_WRITER_FUNDS_DEFAULT) | set(
            gate.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )
        violations = gate._find_unauthorized_capability_edges(
            tree, legacy_funds, parent_map,
            caller_module="services.db_backup",
            policy=policy,
        )
        # db_backup.restore_from_backup → validate_and_restore_backup_strict
        # 已在 policy 中授权
        # 注意:如果 db_backup.py 中调用了 _restore_from_backup_data 之外的
        # 其他 legacy writer 且未授权,会在此处报错
        # 但根据 PRECISE_WHITELIST,restore_from_backup 仅调用 strict service
        assert violations == [], (
            "services/db_backup.py 中存在未授权的 capability 边: "
            f"{violations}"
        )

    def test_real_files_no_dynamic_dispatch_bypass_in_restore_path(self, gate, policy):
        """生产 restore 路径文件中不应有"针对 legacy writer 的动态分派绕过"。

        R73 §5.8 设计意图(_detect_dynamic_dispatch 函数注释):
            本函数仅检测模式存在性(getattr/globals/__import__/...),
            不判定是否实际调用 legacy writer — 这些模式在生产代码中
            可能有合法用途(如 ``getattr(settings, "BACKUP_SIGNING_KEY", "")``)。

        本测试识别"真正的绕过尝试":动态分派的字符串参数是否匹配
        legacy writer 函数名(意味着 ``getattr(obj, "run_restore")()``
        可能绕过静态 capability-edge 验证)。

        合法用途(应通过):
            - getattr(settings, "BACKUP_SIGNING_KEY", "")   # 配置访问
            - getattr(settings, "BACKUP_SCHEMA_VERSION", "R63")
            - importlib.import_module("services.db_restore")  # 标准导入
              (注:本测试不阻止 import_module,因为 import 本身不调用 legacy writer)

        真正的绕过(应失败):
            - getattr(obj, "run_restore")()                  # 直接绕过
            - globals()["_restore_from_backup_data"]()
            - eval("run_restore(...)")
        """
        restore_files = [
            REPO_ROOT / "services" / "db_restore.py",
            REPO_ROOT / "services" / "backup_dr_validate.py",
            REPO_ROOT / "services" / "restore_writer.py",
            REPO_ROOT / "services" / "db_backup.py",
        ]
        legacy_funds = set(gate.LEGACY_WRITER_FUNDS_DEFAULT) | set(
            gate.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )

        def _extract_subscript_str(subscript_node) -> str | None:
            """从 Subscript 节点提取字符串字面量(Python 3.8/3.9+ 兼容)。"""
            slice_val = subscript_node.slice
            # Python 3.9+: subscript.slice 直接是表达式
            if isinstance(slice_val, ast.Constant) and isinstance(slice_val.value, str):
                return slice_val.value
            # Python 3.8: subscript.slice 是 ast.Index
            if (
                hasattr(ast, "Index")
                and isinstance(slice_val, ast.Index)
                and isinstance(slice_val.value, ast.Constant)
                and isinstance(slice_val.value.value, str)
            ):
                return slice_val.value.value
            return None

        for path in restore_files:
            if not path.exists():
                continue
            source = path.read_text(encoding="utf-8")
            tree = _parse(source)
            parent_map = _build_parent_map(tree, gate)
            # 直接遍历 AST:查找动态分派模式 + 字符串字面量匹配 legacy writer
            # 这是真正的"绕过尝试"信号(而非合法的配置访问)
            bypass_attempts: list[dict] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = gate._get_call_func_name(node)
                if not func_name:
                    continue
                # 检测 getattr(obj, "legacy_writer_name") 模式
                if func_name == "getattr" and len(node.args) >= 2:
                    second_arg = node.args[1]
                    if (
                        isinstance(second_arg, ast.Constant)
                        and isinstance(second_arg.value, str)
                        and second_arg.value in legacy_funds
                    ):
                        bypass_attempts.append({
                            "line": node.lineno,
                            "pattern": "getattr",
                            "target": second_arg.value,
                            "reason": "getattr 字符串参数匹配 legacy writer 名(绕过尝试)",
                        })
                # 检测 globals()["legacy_writer_name"] 模式
                elif func_name == "globals":
                    # globals()[...](): globals() Call 的 parent 是 Subscript
                    parent = parent_map.get(id(node))
                    if isinstance(parent, ast.Subscript):
                        val = _extract_subscript_str(parent)
                        if val is not None and val in legacy_funds:
                            bypass_attempts.append({
                                "line": node.lineno,
                                "pattern": "globals",
                                "target": val,
                                "reason": "globals()[] 字符串参数匹配 legacy writer 名(绕过尝试)",
                            })
                # 检测 eval/exec(包含 legacy writer 字符串)模式
                elif func_name in ("eval", "exec") and node.args:
                    first_arg = node.args[0]
                    if (
                        isinstance(first_arg, ast.Constant)
                        and isinstance(first_arg.value, str)
                        and any(lw in first_arg.value for lw in legacy_funds)
                    ):
                        bypass_attempts.append({
                            "line": node.lineno,
                            "pattern": func_name,
                            "target": first_arg.value[:50],
                            "reason": f"{func_name}() 字符串参数含 legacy writer 名(绕过尝试)",
                        })

            assert bypass_attempts == [], (
                f"{path} 中检测到针对 legacy writer 的动态分派绕过尝试: "
                f"{bypass_attempts}"
            )
