"""R66 P0-07: scanner 严格化整改测试 — 精确白名单 + 解析失败 fail + wrapper 再导出检测。

审计背景(R66 终审报告 P0-07):
    R65 的 scanner ``scripts/check_restore_no_legacy_writer.py`` 存在两个问题:
    1. 宽白名单:整个文件级白名单恰好覆盖最危险的旧入口与适配层
       (db_restore.py / backup_dr_validate.py / restore_backends.py /
        restore_orchestrator.py)
    2. 解析失败放行:AST 解析失败时 skip 文件,而非 fail,可能让语法/编码
       异常让扫描器漏检违规。

R66 P0-07 整改:
    1. 白名单从"整个文件"改为精确函数+行范围+AST 调用关系:
       - db_restore.py: 仅 _restore_from_backup_data / run_restore / main
         三个函数在指定行范围内可调用指定 callee
       - backup_dr_validate.py: 仅 validate_and_restore_backup_strict /
         _restore_preverified_payload 可调用 _restore_from_backup_data
       - restore_orchestrator.py / restore_backends.py: 移出白名单
       - error_codes.py: 仍完全跳过(仅引用错误码字符串)
    2. 解析失败必须 fail(不再 skip)
    3. 禁止 wrapper 再导出 legacy writer(__all__ / from ... import ... as ...)
    4. 增加运行时门禁:生产环境(ENVIRONMENT=production 或 APP_ENV=production)
       配置 ALLOW_LEGACY_RESTORE=1/true/yes 时,Settings 加载失败 → 启动失败

测试覆盖矩阵:
    A. 解析失败 fail
       - SyntaxError 文件 → scanner exit 1(不再 skip)
    B. 精确白名单(函数+行范围)
       - PRECISE_WHITELIST 条目结构:file / function / line_start / line_end / allowed_callees
       - restore_orchestrator.py 不在白名单
       - restore_backends.py 不在白名单
       - db_restore.py / backup_dr_validate.py 在精确白名单(但不再 whole-file skip)
    C. wrapper 再导出检测
       - __all__ 包含 legacy writer → 违规
       - from X import legacy_writer as alias → 违规
    D. 运行时门禁(Settings validator)
       - ENVIRONMENT=production + ALLOW_LEGACY_RESTORE=1 → ValueError
       - APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → ValueError
       - ENVIRONMENT=development + ALLOW_LEGACY_RESTORE=1 → 通过(测试逃生舱)
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
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


def _run_gate(strict: bool = False) -> tuple[int, str]:
    """运行 AST gate 脚本,返回 (exit_code, stdout+stderr)。"""
    cmd = [sys.executable, "scripts/check_restore_no_legacy_writer.py"]
    if strict:
        cmd.append("--strict")
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


def _load_real_settings_class():
    """直接从 config/settings.py 文件加载真实 Settings 类。

    conftest 在收集阶段将 ``config`` 替换为 MagicMock 模块(仅含 ``settings`` 属性,
    不是 package),因此 ``import config.settings`` 会失败。本函数使用
    ``importlib.util.spec_from_file_location`` 绕过 ``sys.modules['config']``,
    从文件路径直接加载真实 ``Settings`` 类,以测试 R66 P0-07 的 model_validator。

    每次调用都重新加载,确保 validator 在新环境变量下重新执行。
    """
    settings_path = REPO_ROOT / "config" / "settings.py"
    spec = importlib.util.spec_from_file_location(
        "_r66_p0_07_real_settings", settings_path
    )
    assert spec is not None and spec.loader is not None, (
        f"无法加载 config/settings.py: {settings_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Settings


# ════════════════════════════════════════════════════════════════
# A. 解析失败必须 fail(R66 P0-07)
# ════════════════════════════════════════════════════════════════


class TestParseErrorFails:
    """R66 P0-07: scanner 解析失败必须 fail(不再 skip)。"""

    def test_parse_error_fails(self, tmp_path, monkeypatch):
        """SyntaxError 文件 → scanner exit 1(不再 skip)。

        通过 monkey-patch _iter_python_files 注入一个含 SyntaxError 的临时文件,
        验证 scanner 返回 exit_code=1 而非 0(不再 skip 解析失败的文件)。
        """
        gate_mod = _import_gate_mod()

        # 创建一个含 SyntaxError 的临时 .py 文件
        bad_file = tmp_path / "bad_syntax.py"
        bad_file.write_text(
            "def broken(:\n"  # SyntaxError: 括号未闭合
            "    pass\n",
            encoding="utf-8",
        )

        # monkey-patch _iter_python_files 仅返回这个坏文件
        monkeypatch.setattr(
            gate_mod, "_iter_python_files", lambda: iter([bad_file])
        )
        # monkey-patch _rel_posix 让坏文件路径可被识别
        monkeypatch.setattr(
            gate_mod, "_rel_posix",
            lambda p: "tmp/bad_syntax.py" if p == bad_file else str(p),
        )

        exit_code, violations = gate_mod.check(strict=False)
        assert exit_code == 1, (
            "R66 P0-07: 解析失败必须 fail(exit_code=1),不应 skip 后返回 0"
        )
        # 违规列表可能为空(解析错误不算违规),但 exit_code 必须为 1
        # (parse_errors 单独检查,在 check() 内部直接返回 1)

    def test_parse_error_message_contains_file_and_error(self, tmp_path, monkeypatch):
        """解析失败时输出包含文件名与错误类型(便于定位)。"""
        gate_mod = _import_gate_mod()

        bad_file = tmp_path / "broken.py"
        bad_file.write_text(
            "import os\n"
            "def f(:\n"  # SyntaxError
            "    pass\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            gate_mod, "_iter_python_files", lambda: iter([bad_file])
        )
        monkeypatch.setattr(
            gate_mod, "_rel_posix",
            lambda p: "tmp/broken.py" if p == bad_file else str(p),
        )

        # 捕获 stdout
        import io
        from contextlib import redirect_stdout
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code, _ = gate_mod.check(strict=False)

        output = captured.getvalue()
        assert exit_code == 1
        assert "tmp/broken.py" in output, "输出应包含解析失败的文件路径"
        assert "SyntaxError" in output, "输出应包含错误类型(SyntaxError)"
        assert "R66 P0-07" in output, "输出应标注 R66 P0-07 整改说明"

    def test_parse_error_does_not_skip_file_silently(self, tmp_path, monkeypatch):
        """解析失败不再静默 skip — 必须在输出中明确报告。

        旧实现:except (SyntaxError, ...): continue  # 静默 skip
        新实现:记录 parse_errors,check() 末尾若 parse_errors 非空则 exit 1
        """
        gate_mod = _import_gate_mod()

        bad_file = tmp_path / "silent.py"
        bad_file.write_text("def f(:\n    pass\n", encoding="utf-8")

        monkeypatch.setattr(
            gate_mod, "_iter_python_files", lambda: iter([bad_file])
        )
        monkeypatch.setattr(
            gate_mod, "_rel_posix",
            lambda p: "tmp/silent.py" if p == bad_file else str(p),
        )

        import io
        from contextlib import redirect_stdout
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code, _ = gate_mod.check(strict=False)

        output = captured.getvalue()
        # 不应出现 "[OK]" — 解析失败必须 fail
        assert "[OK]" not in output, (
            "解析失败时不应输出 [OK](必须 fail,不再静默 skip)"
        )
        assert "[FAIL]" in output, "解析失败时应输出 [FAIL]"


# ════════════════════════════════════════════════════════════════
# B. 精确白名单(函数+行范围)— R66 P0-07
# ════════════════════════════════════════════════════════════════


class TestPreciseWhitelist:
    """R66 P0-07 / R67 P1-07: 白名单从"整个文件"改为函数 qualified name +
    AST signature + source digest(R67 P1-07 禁止行范围授权)。"""

    def test_whitelist_is_precise_not_whole_file(self):
        """白名单条目必须指定 function + ast_signature + source_digest + allowed_callees。

        R67 P1-07 整改:删除 line_start/line_end(行范围授权),
        改为 ast_signature + source_digest 双重绑定。

        每个 PRECISE_WHITELIST 条目必须包含:
            - file: 文件路径
            - function: 函数名(非空字符串)
            - ast_signature: 64 字符 SHA-256 hex
            - source_digest: 64 字符 SHA-256 hex
            - allowed_callees: 允许调用的 callee 集合(非空 frozenset)
        """
        gate_mod = _import_gate_mod()

        assert len(gate_mod.PRECISE_WHITELIST) > 0, "精确白名单不应为空"

        for entry in gate_mod.PRECISE_WHITELIST:
            # 必须包含所有必需字段
            assert "file" in entry, f"白名单条目缺少 file 字段: {entry}"
            assert "function" in entry, f"白名单条目缺少 function 字段: {entry}"
            assert "ast_signature" in entry, f"白名单条目缺少 ast_signature 字段: {entry}"
            assert "source_digest" in entry, f"白名单条目缺少 source_digest 字段: {entry}"
            assert "allowed_callees" in entry, f"白名单条目缺少 allowed_callees 字段: {entry}"

            # function 必须是非空字符串(不是 None 或 "")
            assert isinstance(entry["function"], str) and entry["function"], (
                f"function 必须是非空字符串: {entry}"
            )

            # R67 P1-07: ast_signature / source_digest 必须是 64 字符 hex SHA-256
            assert isinstance(entry["ast_signature"], str) and len(entry["ast_signature"]) == 64, (
                f"ast_signature 必须是 64 字符 SHA-256 hex: {entry}"
            )
            assert all(c in "0123456789abcdef" for c in entry["ast_signature"]), (
                f"ast_signature 必须是 hex 字符: {entry}"
            )
            assert isinstance(entry["source_digest"], str) and len(entry["source_digest"]) == 64, (
                f"source_digest 必须是 64 字符 SHA-256 hex: {entry}"
            )
            assert all(c in "0123456789abcdef" for c in entry["source_digest"]), (
                f"source_digest 必须是 hex 字符: {entry}"
            )

            # R67 P1-07: 不得包含 line_start / line_end(禁止行范围授权)
            assert "line_start" not in entry, (
                f"R67 P1-07: 白名单条目不得包含 line_start(禁止行范围授权): {entry}"
            )
            assert "line_end" not in entry, (
                f"R67 P1-07: 白名单条目不得包含 line_end(禁止行范围授权): {entry}"
            )

            # allowed_callees 必须是非空 frozenset
            assert isinstance(entry["allowed_callees"], frozenset) and entry["allowed_callees"], (
                f"allowed_callees 必须是非空 frozenset: {entry}"
            )

    def test_restore_orchestrator_not_in_whitelist(self):
        """restore_orchestrator.py 不在白名单(新生产路径,禁止调用 legacy writer)。

        R66 P0-07: restore_orchestrator.py 是新生产路径(RestoreOrchestrator 蓝绿切换),
        不应直接调用 legacy writer,故不在精确白名单中。
        """
        gate_mod = _import_gate_mod()

        # 不在完全跳过的白名单文件中
        ro_path = REPO_ROOT / "services" / "restore_orchestrator.py"
        assert not gate_mod._is_whitelisted(ro_path), (
            "services/restore_orchestrator.py 不应在完全跳过白名单中"
        )

        # 不在精确白名单中
        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/restore_orchestrator.py" not in precise_files, (
            "services/restore_orchestrator.py 不应在精确白名单中(R66 P0-07: 新生产路径)"
        )

    def test_restore_backends_not_in_whitelist(self):
        """restore_backends.py 不在白名单(新 backend 适配层,禁止调用 legacy writer)。

        R66 P0-07: restore_backends.py 是新 backend 适配层,
        不应直接调用 legacy writer,故不在精确白名单中。
        """
        gate_mod = _import_gate_mod()

        # 不在完全跳过的白名单文件中
        rb_path = REPO_ROOT / "services" / "restore_backends.py"
        assert not gate_mod._is_whitelisted(rb_path), (
            "services/restore_backends.py 不应在完全跳过白名单中"
        )

        # 不在精确白名单中
        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/restore_backends.py" not in precise_files, (
            "services/restore_backends.py 不应在精确白名单中(R66 P0-07: 新 backend 适配层)"
        )

    def test_db_restore_in_precise_whitelist(self):
        """db_restore.py 在精确白名单(但不再 whole-file skip)。

        R66 P0-07: db_restore.py 不再完全跳过,改为精确函数级白名单。

        R70 Wave 7 整改(restore writer 唯一化):
            db_restore.py 中的 _restore_from_backup_data / TABLE_PK / _safe_val
            等 writer 实现已移除(全部 re-export 自 services/restore_writer.py)。
            因此 db_restore.py 精确白名单中只保留 CLI 入口函数:
              - run_restore(委托 validate_and_restore_backup_strict)
              - main(委托 run_restore)
            _restore_from_backup_data 的白名单授权已迁移至
            services/restore_writer.py(单一事实源)。
        """
        gate_mod = _import_gate_mod()

        # 不在完全跳过的白名单中
        db_restore_path = REPO_ROOT / "services" / "db_restore.py"
        assert not gate_mod._is_whitelisted(db_restore_path), (
            "services/db_restore.py 不应完全跳过(R66 P0-07: 改为精确函数级白名单)"
        )

        # 在精确白名单中
        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/db_restore.py" in precise_files

        # R70 Wave 7: db_restore.py 只保留 CLI 入口函数(run_restore / main)
        db_restore_entries = [
            entry for entry in gate_mod.PRECISE_WHITELIST
            if entry["file"] == "services/db_restore.py"
        ]
        functions = {entry["function"] for entry in db_restore_entries}
        assert "run_restore" in functions, (
            "db_restore.py 的 run_restore 应在精确白名单(CLI 入口委托 strict service)"
        )
        assert "main" in functions, (
            "db_restore.py 的 main 应在精确白名单(CLI argparse 入口)"
        )
        # R70 Wave 7: _restore_from_backup_data 已从 db_restore.py 移除,
        # 不再需要在此文件的白名单中(授权迁移至 services/restore_writer.py)
        assert "_restore_from_backup_data" not in functions, (
            "R70 Wave 7: db_restore.py 不再定义 _restore_from_backup_data,"
            "白名单授权已迁移至 services/restore_writer.py"
        )

    def test_restore_writer_in_precise_whitelist(self):
        """R70 Wave 7: restore_writer.py 在精确白名单(唯一 writer 实现)。

        db_restore.py 中的 _restore_from_backup_data / _restore_crdb_tables /
        _restore_sqlite_tables_to_db 等 writer 实现已迁移至
        services/restore_writer.py(单一事实源)。restore_writer.py 必须在
        精确白名单中,授权 _restore_from_backup_data 调用其子写入器。
        """
        gate_mod = _import_gate_mod()

        restore_writer_path = REPO_ROOT / "services" / "restore_writer.py"
        assert not gate_mod._is_whitelisted(restore_writer_path), (
            "services/restore_writer.py 不应完全跳过(R70 Wave 7: 改为精确函数级白名单)"
        )

        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/restore_writer.py" in precise_files, (
            "R70 Wave 7: services/restore_writer.py 应在精确白名单(唯一 writer 实现)"
        )

        rw_entries = [
            entry for entry in gate_mod.PRECISE_WHITELIST
            if entry["file"] == "services/restore_writer.py"
        ]
        functions = {entry["function"] for entry in rw_entries}
        assert "_restore_from_backup_data" in functions, (
            "R70 Wave 7: restore_writer.py 的 _restore_from_backup_data 应在精确白名单"
        )

    def test_backup_dr_validate_in_precise_whitelist(self):
        """backup_dr_validate.py 在精确白名单(但不再 whole-file skip)。"""
        gate_mod = _import_gate_mod()

        bdv_path = REPO_ROOT / "services" / "backup_dr_validate.py"
        assert not gate_mod._is_whitelisted(bdv_path), (
            "services/backup_dr_validate.py 不应完全跳过(R66 P0-07: 改为精确函数级白名单)"
        )

        precise_files = {entry["file"] for entry in gate_mod.PRECISE_WHITELIST}
        assert "services/backup_dr_validate.py" in precise_files

        bdv_entries = [
            entry for entry in gate_mod.PRECISE_WHITELIST
            if entry["file"] == "services/backup_dr_validate.py"
        ]
        functions = {entry["function"] for entry in bdv_entries}
        assert "validate_and_restore_backup_strict" in functions
        assert "_restore_preverified_payload" in functions

    def test_is_call_allowed_precise_check(self):
        """R67 P1-07: _is_call_allowed 基于 (file, function, callee) +
        AST signature + source digest 精确匹配(禁止行范围授权)。

        新方案:
            1. (file, function, callee) 基本匹配 — 不提供 enclosing_func_node/source
               时仅做基本匹配(向后兼容旧测试)
            2. 提供 enclosing_func_node + source 时,必须 signature + digest 双匹配
               - signature 不匹配 → 拒绝(函数结构已修改)
               - source digest 不匹配 → 拒绝(函数源码已修改)
            3. 模块级调用 / 非白名单文件 / 不在 allowed_callees 中的 callee → 永远拒绝
        """
        gate_mod = _import_gate_mod()

        # —— 基本匹配(向后兼容:不提供 node/source)— 验证 (file, function, callee) 三元组 ——

        # R70 Wave 7: _restore_from_backup_data 的授权已迁移至 services/restore_writer.py
        # restore_writer.py: _restore_from_backup_data 调用 _restore_crdb_tables → 允许
        assert gate_mod._is_call_allowed(
            "services/restore_writer.py", "_restore_from_backup_data",
            "_restore_crdb_tables", 584,
        ), "_restore_from_backup_data 调用 _restore_crdb_tables 应允许(基本匹配)"

        # db_restore.py: _restore_from_backup_data 已不再定义(改为 re-export),
        # 不再授权 — 即使函数名匹配,也不允许在 db_restore.py 中调用
        assert not gate_mod._is_call_allowed(
            "services/db_restore.py", "_restore_from_backup_data",
            "_restore_crdb_tables", 584,
        ), "R70 Wave 7: db_restore.py 不再授权 _restore_from_backup_data(已 re-export)"

        # db_restore.py: run_restore 调用 validate_and_restore_backup_strict → 允许
        assert gate_mod._is_call_allowed(
            "services/db_restore.py", "run_restore",
            "validate_and_restore_backup_strict", 391,
        ), "run_restore 调用 validate_and_restore_backup_strict 应允许(基本匹配)"

        # restore_writer.py: _restore_from_backup_data 调用 run_restore → 不允许(不在 allowed_callees)
        assert not gate_mod._is_call_allowed(
            "services/restore_writer.py", "_restore_from_backup_data",
            "run_restore", 500,
        ), "_restore_from_backup_data 不允许调用 run_restore(不在 allowed_callees)"

        # restore_writer.py: 其他函数调用 _restore_crdb_tables → 不允许(函数名不匹配)
        assert not gate_mod._is_call_allowed(
            "services/restore_writer.py", "some_other_function",
            "_restore_crdb_tables", 500,
        ), "非白名单函数不允许调用 _restore_crdb_tables"

        # 模块级调用(None enclosing)→ 不允许
        assert not gate_mod._is_call_allowed(
            "services/restore_writer.py", None,
            "_restore_crdb_tables", 500,
        ), "模块级调用 legacy writer 不允许"

        # 其他文件 → 不允许(即使函数名匹配)
        assert not gate_mod._is_call_allowed(
            "services/other.py", "_restore_from_backup_data",
            "_restore_crdb_tables", 500,
        ), "非白名单文件不允许(即使函数名匹配)"

    def test_is_call_allowed_signature_digest_verification(self):
        """R67 P1-07: 提供 enclosing_func_node + source 时必须 signature+digest 双匹配。

        构造一个合成的 FunctionDef 节点(结构完全不同于白名单中的真实函数),
        验证即使 (file, function, callee) 三元组匹配,signature 不匹配也会拒绝。
        """
        gate_mod = _import_gate_mod()

        # 合成一个与白名单中 _restore_from_backup_data 完全不同的函数体
        # (即使函数名相同,AST 结构不同 → signature 不匹配 → 拒绝)
        synthetic_source = (
            "def _restore_from_backup_data(backup_id, tables=None):\n"
            "    # 这是合成的、与真实函数完全不同的实现\n"
            "    return 'synthetic_impl'\n"
        )
        tree = ast.parse(synthetic_source, filename="<synthetic>")
        synthetic_func_node = tree.body[0]
        assert isinstance(synthetic_func_node, ast.FunctionDef), (
            "合成节点应为 FunctionDef"
        )

        # R70 Wave 7: _restore_from_backup_data 现位于 services/restore_writer.py
        # 即使 (file, function, callee) 三元组匹配,signature 不匹配 → 拒绝
        # (合成的函数结构与真实 _restore_from_backup_data 完全不同)
        allowed = gate_mod._is_call_allowed(
            "services/restore_writer.py", "_restore_from_backup_data",
            "_restore_crdb_tables", 500,
            enclosing_func_node=synthetic_func_node,
            source=synthetic_source,
        )
        assert not allowed, (
            "R67 P1-07: signature 不匹配应拒绝(防止函数修改后静默通过白名单)"
        )

    def test_is_call_allowed_real_function_signature_matches(self):
        """R67 P1-07: 真实白名单函数的 signature + digest 必须匹配(scanner 主流程验证)。

        R70 Wave 7: _restore_from_backup_data 已从 services/db_restore.py 迁移至
        services/restore_writer.py(单一事实源),本测试相应改为从
        services/restore_writer.py 解析函数。
        """
        gate_mod = _import_gate_mod()

        # R70 Wave 7: 从 services/restore_writer.py 解析 _restore_from_backup_data
        restore_writer_path = REPO_ROOT / "services" / "restore_writer.py"
        assert restore_writer_path.exists(), "services/restore_writer.py 应存在"
        source = restore_writer_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(restore_writer_path))

        # 找到 _restore_from_backup_data 函数节点(可能是 FunctionDef 或 AsyncFunctionDef)
        target_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_restore_from_backup_data"
            ):
                target_func = node
                break
        assert target_func is not None, (
            "services/restore_writer.py 中应存在 _restore_from_backup_data 函数"
        )

        # 真实函数的 signature + digest 必须与白名单条目匹配
        allowed = gate_mod._is_call_allowed(
            "services/restore_writer.py", "_restore_from_backup_data",
            "_restore_crdb_tables", target_func.lineno,
            enclosing_func_node=target_func,
            source=source,
        )
        assert allowed, (
            "R67 P1-07: 真实 _restore_from_backup_data 函数的 signature+digest "
            "必须与 PRECISE_WHITELIST 中的条目匹配(若失配,请运行 "
            "scripts/regenerate_scanner_whitelist_digests.py 重新生成)"
        )

    def test_default_mode_still_passes(self):
        """默认模式:无违规(精确白名单正确覆盖所有合法调用)。"""
        exit_code, output = _run_gate(strict=False)
        assert exit_code == 0, (
            f"默认模式应通过(精确白名单覆盖所有合法调用),实际输出:\n{output}"
        )
        assert "[OK]" in output

    def test_strict_mode_sealed_callers_whitelisted(self):
        """--strict 模式:已知 sealed 调用方已被精确白名单覆盖(db_backup / command_bus)。

        R66 P0-07 整改后:db_backup.py:restore_from_backup 与
        command_bus.py:_handler 是已 capability-sealed 的调用方
        (运行时由 ALLOW_LEGACY_RESTORE env var + RESTORE_LEGACY_WRITER_SEALED
        错误码防护)。这些 sealed 调用方已加入 PRECISE_WHITELIST
        (精确函数+AST signature+source digest),因此 --strict 模式应通过(无违规)。

        R67 P1-07 整改:删除 line_start/line_end(行范围授权),
        改为 ast_signature + source_digest 双重绑定。

        本测试验证:
          1. sealed 调用方在 PRECISE_WHITELIST 中(精确白名单已覆盖)
          2. sealed 调用方条目含 ast_signature/source_digest(64 字符 SHA-256 hex)
          3. 真实函数的 signature/digest 与白名单条目匹配(scanner 主流程验证)
          4. --strict 模式 exit_code=0(无违规)
        """
        gate_mod = _import_gate_mod()

        # 验证 sealed 调用方在 PRECISE_WHITELIST 中(精确白名单已覆盖)
        precise_files_functions = {
            (entry["file"], entry["function"])
            for entry in gate_mod.PRECISE_WHITELIST
        }
        assert ("services/db_backup.py", "restore_from_backup") in precise_files_functions, (
            "services/db_backup.py:restore_from_backup 应在 PRECISE_WHITELIST 中"
            "(已 capability-sealed,精确白名单覆盖)"
        )
        assert ("services/command_bus.py", "_handler") in precise_files_functions, (
            "services/command_bus.py:_handler 应在 PRECISE_WHITELIST 中"
            "(已 capability-sealed,精确白名单覆盖)"
        )

        # R67 P1-07: 验证 sealed 调用方条目含 ast_signature/source_digest
        # (64 字符 SHA-256 hex),且不再含 line_start/line_end
        db_backup_entry = next(
            entry for entry in gate_mod.PRECISE_WHITELIST
            if entry["file"] == "services/db_backup.py"
            and entry["function"] == "restore_from_backup"
        )
        assert db_backup_entry["allowed_callees"] == frozenset({"validate_and_restore_backup_strict"}), (
            f"restore_from_backup 仅允许调用 validate_and_restore_backup_strict, "
            f"实际: {db_backup_entry['allowed_callees']}"
        )
        # R67 P1-07: 必须含 ast_signature/source_digest(64 字符 SHA-256 hex)
        assert "ast_signature" in db_backup_entry, (
            "R67 P1-07: 白名单条目必须含 ast_signature 字段"
        )
        assert "source_digest" in db_backup_entry, (
            "R67 P1-07: 白名单条目必须含 source_digest 字段"
        )
        assert len(db_backup_entry["ast_signature"]) == 64, (
            "ast_signature 必须是 64 字符 SHA-256 hex"
        )
        assert len(db_backup_entry["source_digest"]) == 64, (
            "source_digest 必须是 64 字符 SHA-256 hex"
        )
        # R67 P1-07: 禁止 line_start/line_end(行范围授权已废弃)
        assert "line_start" not in db_backup_entry, (
            "R67 P1-07: 禁止 line_start 字段(行范围授权已废弃)"
        )
        assert "line_end" not in db_backup_entry, (
            "R67 P1-07: 禁止 line_end 字段(行范围授权已废弃)"
        )

        command_bus_entry = next(
            entry for entry in gate_mod.PRECISE_WHITELIST
            if entry["file"] == "services/command_bus.py"
            and entry["function"] == "_handler"
        )
        assert command_bus_entry["allowed_callees"] == frozenset({"restore_from_backup"}), (
            f"_handler 仅允许调用 restore_from_backup, "
            f"实际: {command_bus_entry['allowed_callees']}"
        )
        # R67 P1-07: 必须含 ast_signature/source_digest
        assert "ast_signature" in command_bus_entry
        assert "source_digest" in command_bus_entry
        assert len(command_bus_entry["ast_signature"]) == 64
        assert len(command_bus_entry["source_digest"]) == 64
        assert "line_start" not in command_bus_entry
        assert "line_end" not in command_bus_entry

        # R67 P1-07: 真实函数的 signature/digest 必须与白名单条目匹配
        # (scanner 主流程的行为,保证白名单不过期)
        db_backup_path = REPO_ROOT / "services" / "db_backup.py"
        db_backup_source = db_backup_path.read_text(encoding="utf-8")
        db_backup_tree = ast.parse(db_backup_source, filename=str(db_backup_path))
        db_backup_func = None
        for node in ast.walk(db_backup_tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "restore_from_backup"
            ):
                db_backup_func = node
                break
        assert db_backup_func is not None, (
            "services/db_backup.py 中应存在 restore_from_backup 函数"
        )
        actual_sig = gate_mod.compute_ast_signature(db_backup_func)
        actual_src = gate_mod.compute_source_digest(db_backup_func, db_backup_source)
        # R67 P1-07 hotfix: source_digest 是授权依据(跨版本稳定),
        # ast_signature 仅作诊断(非阻塞) — ast.dump() 在 Python 3.10/3.11/3.12/
        # 3.13/3.14 上对同一 AST 输出不同字段集合(如 type_params/TypeAlias),
        # 故跨版本比较 ast_signature 不可靠,但 source_digest 始终稳定。
        assert actual_src == db_backup_entry["source_digest"], (
            "R67 P1-07: 真实 restore_from_backup 的 source_digest 与白名单条目不匹配 "
            "(请运行 scripts/regenerate_scanner_whitelist_digests.py 重新生成)"
        )
        # ast_signature 仍需为合法的 64 字符 SHA-256 hex(诊断字段结构校验)
        assert len(actual_sig) == 64, (
            "ast_signature 必须是 64 字符 SHA-256 hex(诊断字段结构校验)"
        )

        # --strict 模式应通过(sealed 调用方已被白名单覆盖)
        exit_code, output = _run_gate(strict=True)
        assert exit_code == 0, (
            f"--strict 模式应通过(sealed 调用方已被精确白名单覆盖),"
            f"实际输出:\n{output}"
        )
        assert "[OK]" in output
        # 不应出现 [FAIL](sealed 调用方已白名单覆盖,无违规)
        assert "[FAIL]" not in output


# ════════════════════════════════════════════════════════════════
# C. wrapper 再导出检测(R66 P0-07)
# ════════════════════════════════════════════════════════════════


class TestWrapperReexportDetected:
    """R66 P0-07: 禁止 wrapper 再导出 legacy writer(__all__ / from ... import ... as ...)。"""

    def test_wrapper_reexport_detected_all(self):
        """__all__ 包含 legacy writer 名 → 检测为违规。"""
        gate_mod = _import_gate_mod()

        # 合成源码:__all__ 包含 _restore_from_backup_data
        source = """
__all__ = ["_restore_from_backup_data", "other_func"]
"""
        tree = ast.parse(source, filename="<test>")
        violations = gate_mod._find_reexport_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 1, (
            f"应检测到 1 处 __all__ 再导出违规,实际: {violations}"
        )
        assert violations[0]["func"] == "_restore_from_backup_data"
        assert violations[0]["enclosing"] == "__all__"

    def test_wrapper_reexport_detected_strict_mode(self):
        """--strict 模式下 __all__ 包含 validate_and_restore_backup_strict → 检测为违规。"""
        gate_mod = _import_gate_mod()

        source = """
__all__ = ["validate_and_restore_backup_strict", "restore_from_backup"]
"""
        tree = ast.parse(source, filename="<test>")
        strict_funds = (
            gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
            | gate_mod.LEGACY_WRITER_FUNDS_STRICT_EXTRA
        )
        violations = gate_mod._find_reexport_violations(tree, strict_funds)
        assert len(violations) == 2, (
            f"--strict 模式应检测到 2 处 __all__ 再导出违规,实际: {violations}"
        )
        funcs = {v["func"] for v in violations}
        assert "validate_and_restore_backup_strict" in funcs
        assert "restore_from_backup" in funcs

    def test_wrapper_reexport_detected_import_as(self):
        """from X import legacy_writer as alias → 检测为违规(别名再导出)。"""
        gate_mod = _import_gate_mod()

        source = """
from services.db_restore import _restore_from_backup_data as legacy_writer
"""
        tree = ast.parse(source, filename="<test>")
        violations = gate_mod._find_reexport_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 1, (
            f"应检测到 1 处 import as 别名再导出违规,实际: {violations}"
        )
        assert violations[0]["func"] == "_restore_from_backup_data"
        assert "import_as_legacy_writer" in violations[0]["enclosing"]

    def test_import_without_alias_not_flagged(self):
        """from X import legacy_writer(无 as 别名)→ 不算再导出(正常使用)。

        注意:backup_dr_validate.py 中
        ``from services.db_restore import _restore_from_backup_data``
        是正常使用(在函数内延迟导入后调用),不算再导出。
        """
        gate_mod = _import_gate_mod()

        source = """
from services.db_restore import _restore_from_backup_data
from services.db_restore import run_restore
"""
        tree = ast.parse(source, filename="<test>")
        violations = gate_mod._find_reexport_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 0, (
            f"无 as 别名的 import 不应被检测为再导出违规,实际: {violations}"
        )

    def test_all_without_legacy_writer_not_flagged(self):
        """__all__ 不含 legacy writer → 不算违规(正常导出其他符号)。"""
        gate_mod = _import_gate_mod()

        source = """
__all__ = ["Settings", "settings", "ErrorCodes"]
"""
        tree = ast.parse(source, filename="<test>")
        violations = gate_mod._find_reexport_violations(
            tree, gate_mod.LEGACY_WRITER_FUNDS_DEFAULT
        )
        assert len(violations) == 0

    def test_reexport_violation_in_check_flow(self, tmp_path, monkeypatch):
        """合成文件含 __all__ 再导出 → check() 检测为违规。"""
        gate_mod = _import_gate_mod()

        # 合成文件:__all__ 包含 legacy writer
        bad_file = tmp_path / "reexport.py"
        bad_file.write_text(
            "from services.db_restore import _restore_from_backup_data\n"
            "__all__ = ['_restore_from_backup_data']\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            gate_mod, "_iter_python_files", lambda: iter([bad_file])
        )
        monkeypatch.setattr(
            gate_mod, "_rel_posix",
            lambda p: "tmp/reexport.py" if p == bad_file else str(p),
        )

        exit_code, violations = gate_mod.check(strict=False)
        assert exit_code == 1, (
            "含 __all__ 再导出的文件应被检测为违规(exit_code=1)"
        )
        # 应有至少 1 处违规(__all__ 再导出)
        reexport_violations = [
            v for v in violations if v.get("enclosing") == "__all__"
        ]
        assert len(reexport_violations) >= 1, (
            f"应检测到 __all__ 再导出违规,实际 violations: {violations}"
        )


# ════════════════════════════════════════════════════════════════
# D. 运行时门禁:ALLOW_LEGACY_RESTORE 在生产配置中出现即启动失败
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def allow_legacy_restore_writer():
    """覆盖 conftest 的 autouse fixture — 本测试类需要自行控制 ALLOW_LEGACY_RESTORE。

    conftest.py 中的 ``allow_legacy_restore_writer`` autouse fixture 会强制设置
    ``ALLOW_LEGACY_RESTORE=1``(测试逃生舱),与本测试类的目标冲突。
    此处定义同名 fixture 覆盖之(在 pytest 中,测试类内同名 fixture 优先于
    conftest 的 autouse fixture),让本测试类的每个用例自行设置环境变量。
    """
    yield


class TestAllowLegacyRestoreInProductionFails:
    """R66 P0-07: 生产环境配置 ALLOW_LEGACY_RESTORE → Settings 启动失败。

    注意:本测试类通过 ``_load_real_settings_class()`` 直接从
    ``config/settings.py`` 文件路径加载真实 Settings 类(绕过 conftest
    注入的 MagicMock config 模块),以测试 R66 P0-07 的 model_validator。

    ``config/settings.py`` 在模块末尾有 ``settings = Settings()`` 单例构造,
    会在模块加载时立即触发 model_validator。因此:
      - "fails" 用例:``_load_real_settings_class()`` 本身抛异常
        (pydantic ValidationError 是 ValueError 子类,R70 Wave 3 escape_hatch_guard
        抛 AppError — 两者都会导致 Settings 加载失败,均为 fail-closed)
      - "passes" 用例:``_load_real_settings_class()`` 成功加载

    所有用例均设置 ``SERVICE_ROLE=prometheus_exporter`` 以绕过
    ``validate_required_fields`` 的其他必填字段校验(如 Bot Token / CRDB URL),
    让 R66 P0-07 / R70 Wave 3 的 validator 成为唯一可能失败的校验器。

    R70 Wave 3 整改(escape hatch 硬守卫):
        R70 Wave 3 在 Settings.after_validator 中调用
        ``services.escape_hatch_guard.assert_no_test_escape_hatches``,这是第一道
        防线,在 R66 P0-07 validator 之前执行。当 APP_ENV=production 且
        ALLOW_LEGACY_RESTORE=1/true/yes 时,两者都会触发 — 但 Wave 3 先触发并
        抛 AppError(而非 ValueError)。本测试类相应更新为接受两种异常类型
        (ValueError 或 AppError),并接受 R66 P0-07 或 R70 Wave 3 的错误标识
        (两者均为 fail-closed,生产拒绝启动)。
    """

    def test_allow_legacy_restore_in_production_fails(self, monkeypatch):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=1 → 启动失败(fail-closed)。

        生产环境(APP_ENV=production,R69 P0-1 单一权威源)配置
        ALLOW_LEGACY_RESTORE=1/true/yes 时,Settings 加载应失败(raise),
        阻止进程启动。

        R70 Wave 3: escape_hatch_guard 先于 R66 P0-07 validator 触发,
        抛 AppError(RESTORE_LEGACY_WRITER_SEALED / production_escape_hatch_hard_guard)。
        本测试接受 ValueError 或 AppError(两者均为 fail-closed)。
        """
        # 模拟生产环境 — R69 P0-1: APP_ENV 是唯一权威源,ENVIRONMENT 由其派生
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # 设置 SERVICE_ROLE=prometheus_exporter(无 secrets 依赖,避免其他 validator 失败)
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        # 模块加载时 settings = Settings() 会触发 validator
        # R70 Wave 3: escape_hatch_guard 抛 AppError(非 ValueError 子类)
        # R66 P0-07: validator 抛 ValueError(pydantic ValidationError 子类)
        # 两者均导致 fail-closed,本测试接受任一异常
        with pytest.raises((ValueError, Exception)) as exc_info:
            _load_real_settings_class()

        error_msg = str(exc_info.value)
        # 接受 R66 P0-07 或 R70 Wave 3 的错误标识(两者均 fail-closed)
        assert (
            "R66 P0-07" in error_msg
            or "R70 Wave 3" in error_msg
            or "production_escape_hatch" in error_msg
            or "RESTORE_LEGACY_WRITER_SEALED" in error_msg
        ), f"错误消息应包含 R66 P0-07 或 R70 Wave 3 标识: {error_msg}"
        assert "ALLOW_LEGACY_RESTORE" in error_msg

    def test_allow_legacy_restore_true_in_production_fails(self, monkeypatch):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=true → 启动失败(fail-closed)。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "true")
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        with pytest.raises((ValueError, Exception)):
            _load_real_settings_class()

    def test_allow_legacy_restore_with_app_env_production_fails(self, monkeypatch):
        """APP_ENV=production + ENVIRONMENT=development + ALLOW_LEGACY_RESTORE=1 → fail-closed。

        R70 Wave 1: APP_ENV 与 ENVIRONMENT 冲突时(before-validator)直接拒绝启动,
        不再等到 after-validator 的 R66 P0-07 / R70 Wave 3 检查。这也是
        fail-closed(防止生产环境静默降级)。
        """
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        with pytest.raises((ValueError, Exception)) as exc_info:
            _load_real_settings_class()

        # 接受 R70 Wave 1(冲突)、R66 P0-07、R70 Wave 3 任一错误标识
        error_msg = str(exc_info.value)
        assert (
            "R70" in error_msg
            or "R66 P0-07" in error_msg
            or "ALLOW_LEGACY_RESTORE" in error_msg
        ), f"错误消息应包含 R70/R66 P0-07/ALLOW_LEGACY_RESTORE 标识: {error_msg}"

    def test_allow_legacy_restore_yes_in_production_fails(self, monkeypatch):
        """APP_ENV=production + ALLOW_LEGACY_RESTORE=yes → 启动失败(fail-closed)。"""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "yes")
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        with pytest.raises((ValueError, Exception)):
            _load_real_settings_class()

    def test_allow_legacy_restore_in_development_passes(self, monkeypatch):
        """ENVIRONMENT=development + ALLOW_LEGACY_RESTORE=1 → 通过(测试逃生舱)。

        非生产环境(development / staging 之外的测试环境)允许配置
        ALLOW_LEGACY_RESTORE=1 作为测试逃生舱。
        """
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        # 不应 raise — 开发环境允许 ALLOW_LEGACY_RESTORE
        try:
            _load_real_settings_class()
        except ValueError as e:
            # 仅当错误与 ALLOW_LEGACY_RESTORE 相关时才视为失败
            err_msg = str(e)
            if "ALLOW_LEGACY_RESTORE" in err_msg and "R66 P0-07" in err_msg:
                pytest.fail(
                    f"开发环境不应因 ALLOW_LEGACY_RESTORE=1 失败: {e}"
                )
            # 其他 ValueError(如缺失必填字段)不算 R66 P0-07 失败
        except Exception as e:
            # R70 Wave 3 escape_hatch_guard 在 development 环境不应触发
            err_msg = str(e)
            if "production_escape_hatch" in err_msg:
                pytest.fail(
                    f"开发环境不应触发 R70 Wave 3 escape_hatch_guard: {e}"
                )

    def test_no_allow_legacy_restore_in_production_passes(self, monkeypatch):
        """ENVIRONMENT=production + 无 ALLOW_LEGACY_RESTORE → 通过(不触发 validator)。

        生产环境不配置 ALLOW_LEGACY_RESTORE 时,R66 P0-07 / R70 Wave 3 validator
        不应阻止启动(其他必填字段校验可能失败,但不应是这两个 validator 触发的)。
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)
        monkeypatch.delenv("APP_ENV", raising=False)
        # 设置 SERVICE_ROLE=prometheus_exporter(无 secrets 依赖,避免其他 validator 失败)
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        try:
            _load_real_settings_class()
        except ValueError as e:
            # 不应是 R66 P0-07 / R70 Wave 3 validator 触发的
            assert "R66 P0-07" not in str(e), (
                f"未配置 ALLOW_LEGACY_RESTORE 时不应触发 R66 P0-07 validator: {e}"
            )
        except Exception as e:
            # R70 Wave 3 escape_hatch_guard 不应触发(无 ALLOW_LEGACY_RESTORE)
            assert "production_escape_hatch" not in str(e), (
                f"未配置 ALLOW_LEGACY_RESTORE 时不应触发 R70 Wave 3: {e}"
            )

    def test_allow_legacy_restore_empty_in_production_passes(self, monkeypatch):
        """ENVIRONMENT=production + ALLOW_LEGACY_RESTORE=''(空值)→ 通过。

        空值等价于未配置,不应触发 validator。
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "")
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        try:
            _load_real_settings_class()
        except ValueError as e:
            assert "R66 P0-07" not in str(e), (
                f"ALLOW_LEGACY_RESTORE='' 时不应触发 R66 P0-07 validator: {e}"
            )
        except Exception as e:
            assert "production_escape_hatch" not in str(e), (
                f"ALLOW_LEGACY_RESTORE='' 时不应触发 R70 Wave 3: {e}"
            )

    def test_allow_legacy_restore_invalid_in_production_passes(self, monkeypatch):
        """ENVIRONMENT=production + ALLOW_LEGACY_RESTORE=invalid → 通过。

        非 1/true/yes 的值视为未启用,不应触发 validator。
        (与 db_restore.run_restore 的 seal 语义一致:invalid 视为生产模式)
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "invalid")
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("SERVICE_ROLE", "prometheus_exporter")

        try:
            _load_real_settings_class()
        except ValueError as e:
            assert "R66 P0-07" not in str(e), (
                f"ALLOW_LEGACY_RESTORE=invalid 时不应触发 R66 P0-07 validator: {e}"
            )
        except Exception as e:
            assert "production_escape_hatch" not in str(e), (
                f"ALLOW_LEGACY_RESTORE=invalid 时不应触发 R70 Wave 3: {e}"
            )


# ════════════════════════════════════════════════════════════════
# E. 完整 scanner 执行验证(R66 P0-07)
# ════════════════════════════════════════════════════════════════


class TestScannerExecution:
    """R66 P0-07: 完整 scanner 执行验证(子进程方式)。"""

    def test_default_mode_passes(self):
        """默认模式:scanner 通过(无违规)。

        验证精确白名单正确覆盖所有合法调用,无新违规。
        """
        exit_code, output = _run_gate(strict=False)
        assert exit_code == 0, (
            f"默认模式应通过(精确白名单覆盖所有合法调用),实际输出:\n{output}"
        )
        assert "[OK]" in output
        assert "R66 P0-07" in output

    def test_strict_mode_passes_with_sealed_callers_whitelisted(self):
        """--strict 模式:sealed 调用方已被精确白名单覆盖,scanner 通过。

        R66 P0-07 整改后:db_backup.py:restore_from_backup 与
        command_bus.py:_handler 是已 capability-sealed 的调用方,
        已加入 PRECISE_WHITELIST。--strict 模式应通过(无违规),
        不报告 [FAIL]。
        """
        exit_code, output = _run_gate(strict=True)
        assert exit_code == 0, (
            f"--strict 模式应通过(sealed 调用方已被精确白名单覆盖),"
            f"实际输出:\n{output}"
        )
        assert "[OK]" in output
        assert "R66 P0-07" in output
        # 不应出现 [FAIL](sealed 调用方已白名单覆盖,无违规)
        assert "[FAIL]" not in output

    def test_scanner_output_contains_r66_p0_07_marker(self):
        """scanner 输出包含 R66 P0-07 整改标记(便于审计)。"""
        # strict 模式应通过,但仍包含 R66 P0-07 整改说明
        exit_code, output = _run_gate(strict=True)
        assert exit_code == 0
        # 输出应包含 R66 P0-07 整改标记
        assert "R66 P0-07" in output or "R65 P0-07" in output
        # 输出应包含扫描统计信息
        assert "扫描" in output
        assert "白名单跳过" in output
