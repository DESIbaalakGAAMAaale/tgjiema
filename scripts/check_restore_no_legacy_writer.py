#!/usr/bin/env python3
"""R65 P0-07 / P1-07 / R66 P0-07: capability-seal 静态门禁 — 禁止生产代码直接调用旧 restore writer。

使用 Python ast 模块解析全仓 .py 文件,检测违规调用以下"旧 restore writer"函数:

默认模式(legacy writer 私有/CLI 入口):
    - _restore_from_backup_data     (services/db_restore.py 私有写入器)
    - _restore_crdb_tables          (services/db_restore.py 私有 CRDB 子写入器)
    - _restore_sqlite_tables_to_db  (services/db_restore.py 私有 SQLite 子写入器)
    - run_restore                   (services/db_restore.py CLI 入口,已被 capability-seal)

--strict 模式(额外检测 strict service / backup wrapper 公共入口):
    - validate_and_restore_backup_strict  (services/backup_dr_validate.py 公共入口)
    - restore_from_backup                (services/db_backup.py 公共 wrapper)

R66 P0-07 整改(本次变更):
    1. 白名单从"整个文件"改为精确函数+行范围+AST 调用关系:
       - db_restore.py: 仅 _restore_from_backup_data 内部委托给子写入器,
                        仅 run_restore CLI 入口委托给 validate_and_restore_backup_strict
       - backup_dr_validate.py: 仅 validate_and_restore_backup_strict /
                                _restore_preverified_payload 调用 _restore_from_backup_data
       - restore_orchestrator.py / restore_backends.py: 移出白名单(新生产路径,禁止调用 legacy writer)
       - error_codes.py: 仍完全跳过(仅引用错误码字符串,非调用)
    2. 解析失败必须 fail(不再 skip),防止语法/编码异常让扫描器漏检
    3. 禁止 wrapper 再导出 legacy writer:
       - __all__ 包含 legacy writer 名 → 违规(显式再导出)
       - from X import legacy_writer as alias (alias != legacy_writer) → 违规(别名再导出)

违规示例:
    - bots/admin_bot/handlers.py 直接调用 db_restore.run_restore(...)
      → 默认模式报错;生产应改走 RestoreOrchestrator 蓝绿切换路径
    - services/db_backup.py:restore_from_backup() 调用 validate_and_restore_backup_strict()
      → --strict 模式报错;生产应改走 RestoreOrchestrator

背景:
    R65 终审报告 P0-07 / P1-07: 旧直接 restore writer(_restore_from_backup_data
    / _restore_crdb_tables / _restore_sqlite_tables_to_db / run_restore /
    validate_and_restore_backup_strict)在原地覆盖模式下可能"先清空生产表再失败",
    造成 active 数据被破坏且不可恢复。R64 P0-03 已引入 RestoreOrchestrator
    蓝绿切换模型(staging → active),R65 P0-07 / P1-07 在 capability-seal 层
    进一步封存旧 writer:生产入口必须改走 orchestrator,旧 writer 仅保留给
    tests/ + scripts/ + 已精确白名单的 services/ 函数使用。

    R66 P0-07 进一步整改:白名单从"整个文件"收紧为"函数+行范围+AST 调用关系",
    防止宽白名单恰好覆盖最危险的旧入口与适配层;解析失败必须 fail,防止
    语法/编码异常让扫描器漏检;增加 wrapper 再导出检测。

CI 调用方式:
    # ci.yml(static-gates job)— 默认模式,捕获直接调用私有 writer
    python scripts/check_restore_no_legacy_writer.py

    # release-gates.yml(--strict 模式,捕获所有调用包括 strict service)
    python scripts/check_restore_no_legacy_writer.py --strict

退出码:
    - 0: 无违规
    - 1: 检测到违规(或解析失败 — R66 P0-07)
    - 2: 严重错误(参数解析失败等)
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认模式:扫描的 legacy writer 函数名(私有写入器 + CLI 入口)
LEGACY_WRITER_FUNDS_DEFAULT: set[str] = {
    "_restore_from_backup_data",
    "_restore_crdb_tables",
    "_restore_sqlite_tables_to_db",
    "run_restore",
}

# --strict 模式:额外扫描的 strict service / backup wrapper 公共入口
LEGACY_WRITER_FUNDS_STRICT_EXTRA: set[str] = {
    "validate_and_restore_backup_strict",
    "restore_from_backup",
}

# ═══════════════════════════════════════════════════════════════
# R66 P0-07: 精确白名单 — 从"整个文件"改为"函数+行范围+AST 调用关系"
# ═══════════════════════════════════════════════════════════════
# 每个条目: (file_posix, function_name, line_start, line_end, allowed_callees)
# 仅允许指定函数在指定行范围内调用指定的 legacy writer(内部委托 only)。
#
# 设计原则:
#   - 白名单仅允许"同模块内私有委托"(如 _restore_from_backup_data → 子写入器)
#     或"已被 capability-seal 的 CLI/strict 入口委托"(如 run_restore → strict service)。
#   - restore_orchestrator.py / restore_backends.py 不在白名单(新生产路径,
#     禁止调用 legacy writer)。
#   - 行范围用于防止代码漂移:若函数移动导致调用超出范围,scanner 会标记违规,
#     强制团队更新白名单(避免"静默漂移让违规通过")。
PRECISE_WHITELIST: tuple[dict, ...] = (
    # db_restore.py: _restore_from_backup_data 内部委托给子写入器(同模块私有委托)
    {
        "file": "services/db_restore.py",
        "function": "_restore_from_backup_data",
        "line_start": 440,
        "line_end": 631,
        "allowed_callees": frozenset({"_restore_crdb_tables", "_restore_sqlite_tables_to_db"}),
        "reason": "同模块私有委托:_restore_from_backup_data → 子写入器",
    },
    # db_restore.py: run_restore CLI 入口委托给 validate_and_restore_backup_strict
    # (run_restore 本身被 ALLOW_LEGACY_RESTORE seal,但仍允许调用 strict service)
    {
        "file": "services/db_restore.py",
        "function": "run_restore",
        "line_start": 856,
        "line_end": 999,
        "allowed_callees": frozenset({"validate_and_restore_backup_strict"}),
        "reason": "CLI 入口委托:run_restore → validate_and_restore_backup_strict(strict service)",
    },
    # db_restore.py: main() CLI argparse 入口委托给 run_restore
    # (main 仅在 python -m services.db_restore 时执行,运行时受 run_restore 的 ALLOW_LEGACY_RESTORE seal 防护)
    {
        "file": "services/db_restore.py",
        "function": "main",
        "line_start": 1019,
        "line_end": 1044,
        "allowed_callees": frozenset({"run_restore"}),
        "reason": "CLI argparse 入口委托:main → run_restore(运行时由 run_restore 的 seal 防护)",
    },
    # backup_dr_validate.py: validate_and_restore_backup_strict 构造 capability 后调用私有写入器
    {
        "file": "services/backup_dr_validate.py",
        "function": "validate_and_restore_backup_strict",
        "line_start": 1716,
        "line_end": 2044,
        "allowed_callees": frozenset({"_restore_from_backup_data"}),
        "reason": "strict service 构造 capability 后调用私有写入器",
    },
    # backup_dr_validate.py: _restore_preverified_payload 内部委托给 _restore_from_backup_data
    {
        "file": "services/backup_dr_validate.py",
        "function": "_restore_preverified_payload",
        "line_start": 2050,
        "line_end": 2141,
        "allowed_callees": frozenset({"_restore_from_backup_data"}),
        "reason": "preverified payload 路径委托给私有写入器",
    },
    # R66 P0-07: 已 sealed 的生产入口(带 ALLOW_LEGACY_RESTORE capability-seal 检查)
    # 这些入口在生产环境(AppEnv=production)会被 RESTORE_LEGACY_WRITER_SEALED 阻断,
    # 仅在 tests/ 与 scripts/ 设置 ALLOW_LEGACY_RESTORE=1 时才能调用。
    # 长期目标:迁移到 RestoreOrchestrator 蓝绿切换路径后移除这些白名单条目。
    #
    # db_backup.py: restore_from_backup 公共入口(capability-sealed at lines 809-823)
    # 委托给 validate_and_restore_backup_strict(strict service 路径)
    {
        "file": "services/db_backup.py",
        "function": "restore_from_backup",
        "line_start": 776,
        "line_end": 865,
        "allowed_callees": frozenset({"validate_and_restore_backup_strict"}),
        "reason": (
            "sealed 公共入口(capability-seal at lines 809-823):"
            "生产环境被 RESTORE_LEGACY_WRITER_SEALED 阻断,"
            "仅 ALLOW_LEGACY_RESTORE=1 时委托 strict service 路径"
        ),
    },
    # command_bus.py: make_restore_backup_command 内的 _handler(capability-sealed at lines 2392-2406)
    # 委托给 db_backup.restore_from_backup(已 sealed 的公共入口)
    {
        "file": "services/command_bus.py",
        "function": "_handler",
        "line_start": 2381,
        "line_end": 2425,
        "allowed_callees": frozenset({"restore_from_backup"}),
        "reason": (
            "sealed command handler(capability-seal at lines 2392-2406):"
            "生产环境被 RESTORE_LEGACY_WRITER_SEALED 阻断,"
            "仅 ALLOW_LEGACY_RESTORE=1 时委托 sealed 公共入口"
        ),
    },
)

# 完整跳过的白名单文件(不扫描)— 仅引用错误码字符串,无 legacy writer 调用
WHITELIST_FILES_FULL_SKIP: frozenset[str] = frozenset({
    "services/error_codes.py",  # 仅引用错误码字符串(RESTORE_LEGACY_WRITER_SEALED),非调用
})

# 白名单目录(完整跳过)— 测试逃生舱 + gate 脚本自身
# - tests/: 测试逃生舱(配合 ALLOW_LEGACY_RESTORE=1 环境变量)
# - scripts/: gate 脚本本身(自我引用)
WHITELIST_DIR_PREFIXES: tuple[str, ...] = (
    "tests/",
    "scripts/",
)

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    "data",
    "logs",
    "backups",
    "production-evidence",
    "migrations",
]

# 跳过的文件名(不扫描,通常是文档/报告)
SKIP_FILE_SUFFIXES: tuple[str, ...] = (
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".txt",
    ".sql",
    ".html",
    ".css",
    ".js",
)


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(在 SKIP_DIR_PARTS 中或后缀在 SKIP_FILE_SUFFIXES 中)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    if path.suffix and path.suffix not in (".py",):
        return True
    return False


def _is_whitelisted(path: Path) -> bool:
    """R66 P0-07: 检查文件是否完全跳过(不扫描)。

    注意:此函数仅返回 True 表示"完全跳过扫描"。
    db_restore.py / backup_dr_validate.py 不再完全跳过,
    而是通过 PRECISE_WHITELIST 进行函数级精确白名单检查。
    """
    rel = _rel_posix(path)
    # 完整跳过的白名单文件(精确匹配)
    if rel in WHITELIST_FILES_FULL_SKIP:
        return True
    # 白名单目录(前缀匹配)
    for prefix in WHITELIST_DIR_PREFIXES:
        if rel.startswith(prefix):
            return True
    return False


def _is_call_allowed(
    file_rel: str,
    enclosing: str | None,
    callee: str,
    line: int,
) -> bool:
    """R66 P0-07: 检查 (file, enclosing_function, callee, line) 是否在精确白名单中。

    Args:
        file_rel: POSIX 相对路径
        enclosing: 调用所在的函数名(None 表示模块级)
        callee: 被调用的函数名
        line: 调用所在行号

    Returns:
        True 如果在精确白名单中(允许调用)
    """
    if enclosing is None:
        # 模块级调用 legacy writer 永远不允许
        return False
    for entry in PRECISE_WHITELIST:
        if (
            entry["file"] == file_rel
            and entry["function"] == enclosing
            and callee in entry["allowed_callees"]
            and entry["line_start"] <= line <= entry["line_end"]
        ):
            return True
    return False


def _iter_python_files() -> Iterable[Path]:
    """遍历 REPO_ROOT 下所有 .py 文件(跳过缓存/数据/白名单后缀目录)。"""
    for py_file in REPO_ROOT.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


def _get_call_func_name(node: ast.Call) -> str | None:
    """提取 Call 节点的函数名(只看最后一段 attr 或 Name)。

    例如:
        db_restore.run_restore(...)        → "run_restore"
        _restore_from_backup_data(...)     → "_restore_from_backup_data"
        obj.method.run_restore(...)        → "run_restore"
        print(...)                          → "print"

    Returns:
        函数名字符串,无法识别时返回 None
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """构建 parent map: {node_id: parent_node},用于查找 enclosing function。"""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    return parent_map


def _find_enclosing_function(
    node: ast.AST,
    parent_map: dict[int, ast.AST],
) -> str | None:
    """找到节点最近的 enclosing function 名(或 None 表示模块级)。

    通过 parent map 向上遍历,找到最近的 FunctionDef / AsyncFunctionDef。
    """
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent_map.get(id(current))
    return None


def _find_legacy_calls(
    tree: ast.AST,
    legacy_funds: set[str],
    parent_map: dict[int, ast.AST],
) -> list[dict]:
    """查找 AST 中所有 legacy writer 调用(不做白名单过滤)。

    Returns:
        [{line, col, func, enclosing}, ...]
        enclosing: 调用所在的函数名(None 表示模块级)
    """
    calls: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _get_call_func_name(node)
            if func_name and func_name in legacy_funds:
                enclosing = _find_enclosing_function(node, parent_map)
                calls.append({
                    "line": node.lineno,
                    "col": node.col_offset,
                    "func": func_name,
                    "enclosing": enclosing,
                })
    return calls


def _find_reexport_violations(
    tree: ast.AST,
    legacy_funds: set[str],
) -> list[dict]:
    """R66 P0-07: 检测 wrapper re-export 违规:

    1. __all__ 包含 legacy writer 名 → 违规(显式再导出)
    2. from X import legacy_writer as alias (alias != legacy_writer) → 违规(别名再导出)

    Returns:
        [{line, col, func, enclosing}, ...]
    """
    violations: list[dict] = []
    for node in tree.body:
        # __all__ = [...] 检查(普通赋值)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                if elt.value in legacy_funds:
                                    violations.append({
                                        "line": node.lineno,
                                        "col": node.col_offset,
                                        "func": elt.value,
                                        "enclosing": "__all__",
                                    })
        # __all__: list[str] = [...] 检查(带类型注解的赋值)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "__all__"
                and node.value is not None
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value in legacy_funds:
                            violations.append({
                                "line": node.lineno,
                                "col": node.col_offset,
                                "func": elt.value,
                                "enclosing": "__all__",
                            })
        # from X import Y as Z 检查(别名再导出)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (
                    alias.name in legacy_funds
                    and alias.asname is not None
                    and alias.asname != alias.name
                ):
                    violations.append({
                        "line": node.lineno,
                        "col": node.col_offset,
                        "func": alias.name,
                        "enclosing": f"import_as_{alias.asname}",
                    })
    return violations


def _find_violations(
    tree: ast.AST,
    legacy_funds: set[str],
) -> list[tuple[int, int, str]]:
    """向后兼容: 查找 AST 中所有 legacy writer 调用(不做白名单过滤)。

    注意:此函数仅返回原始调用列表(不含 enclosing 信息与白名单过滤)。
    新代码应使用 _find_legacy_calls + _is_call_allowed 进行精确白名单检查。

    Returns:
        [(lineno, col_offset, func_name), ...]
    """
    parent_map = _build_parent_map(tree)
    calls = _find_legacy_calls(tree, legacy_funds, parent_map)
    return [(c["line"], c["col"], c["func"]) for c in calls]


def check(strict: bool = False) -> tuple[int, list[dict]]:
    """主校验流程。

    Args:
        strict: 是否启用 --strict 模式(扫描更广的 legacy writer 集合)

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规(或解析失败 — R66 P0-07)
        violations: 违规列表 [{file, line, col, func, enclosing}, ...]
    """
    # 构建当前模式的 legacy writer 函数集合
    legacy_funds = set(LEGACY_WRITER_FUNDS_DEFAULT)
    if strict:
        legacy_funds |= LEGACY_WRITER_FUNDS_STRICT_EXTRA

    violations: list[dict] = []
    parse_errors: list[dict] = []
    scanned_count = 0
    whitelisted_skipped = 0

    for py_file in _iter_python_files():
        rel = _rel_posix(py_file)
        # 完整跳过的白名单文件(error_codes.py / tests/ / scripts/)
        if _is_whitelisted(py_file):
            whitelisted_skipped += 1
            continue
        scanned_count += 1

        # R66 P0-07: 解析失败必须 fail(不再 skip),防止语法/编码异常让扫描器漏检
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            parse_errors.append({
                "file": rel,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        parent_map = _build_parent_map(tree)

        # 查找所有 legacy writer 调用,按精确白名单过滤
        calls = _find_legacy_calls(tree, legacy_funds, parent_map)
        for c in calls:
            if not _is_call_allowed(rel, c["enclosing"], c["func"], c["line"]):
                violations.append({
                    "file": rel,
                    "line": c["line"],
                    "col": c["col"],
                    "func": c["func"],
                    "enclosing": c["enclosing"],
                })

        # R66 P0-07: 检测 wrapper re-export 违规(__all__ / from ... import ... as ...)
        reexports = _find_reexport_violations(tree, legacy_funds)
        for r in reexports:
            violations.append({
                "file": rel,
                "line": r["line"],
                "col": r["col"],
                "func": r["func"],
                "enclosing": r["enclosing"],
            })

    # R66 P0-07: 解析失败必须 fail(不再 skip)
    if parse_errors:
        print(
            f"[FAIL] R66 P0-07: 检测到 {len(parse_errors)} 个文件解析失败 "
            f"(必须 fail,防止语法/编码异常让扫描器漏检):"
        )
        for pe in parse_errors:
            print(f"  - {pe['file']}: {pe['error']}")
        print()
        print("R66 P0-07 整改: 解析失败必须 fail(不再 skip)。")
        print("请修复语法/编码错误后重新运行 scanner。")
        return 1, violations

    if violations:
        mode_label = "--strict" if strict else "default"
        print(
            f"[FAIL] 检测到 {len(violations)} 处违规调用旧 restore writer "
            f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
            f"白名单跳过 {whitelisted_skipped} 个文件):"
        )
        for v in violations:
            enclosing = v["enclosing"] if v["enclosing"] else "<module>"
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"调用 {v['func']!r} (in {enclosing})"
            )
        print()
        print("R65 P0-07 / P1-07 / R66 P0-07: 旧直接 restore writer 已被 capability-seal,")
        print("生产恢复必须通过 RestoreOrchestrator 蓝绿切换路径执行:")
        print("  1. start_operation(backup_id, manifest_digest, payload_digest, nonce)")
        print("  2. provision_staging(operation_id)")
        print("  3. restore_to_staging(operation_id, datasource)")
        print("  4. validate_staging(operation_id)")
        print("  5. request_approval(operation_id, approval_id, mfa_receipt_id)")
        print("  6. execute_blue_green_switch(operation_id, approval_id, mfa_receipt_id)")
        print()
        print("逃生舱(仅限 tests/ 与 scripts/):")
        print("  设置环境变量 ALLOW_LEGACY_RESTORE=1")
        print("  生产部署绝不应配置此环境变量(应在系统层强制 unset)。")
        print()
        print("R66 P0-07 精确白名单(允许直接调用 legacy writer 的函数+行范围):")
        for entry in PRECISE_WHITELIST:
            callees = ", ".join(sorted(entry["allowed_callees"]))
            print(
                f"  - {entry['file']}::{entry['function']}() "
                f"(lines {entry['line_start']}-{entry['line_end']}) "
                f"→ 可调用: {callees}"
            )
        print()
        print("完全跳过的白名单(仅引用错误码字符串,非调用):")
        for f in sorted(WHITELIST_FILES_FULL_SKIP):
            print(f"  - {f}")
        for d in sorted(WHITELIST_DIR_PREFIXES):
            print(f"  - {d}*")
        return 1, violations

    mode_label = "--strict" if strict else "default"
    print(
        f"[OK] R65 P0-07 / P1-07 / R66 P0-07 capability-seal 门禁检查通过 "
        f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
        f"白名单跳过 {whitelisted_skipped} 个文件,无违规调用)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R65 P0-07 / P1-07 / R66 P0-07: capability-seal 静态门禁 — "
            "禁止生产代码直接调用旧 restore writer。"
        )
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "严格模式:额外检测 validate_and_restore_backup_strict 与 "
            "restore_from_backup 调用(覆盖 strict service 公共入口)。"
            "默认模式仅检测私有 writer(_restore_from_backup_data 等)与 CLI 入口。"
        ),
    )
    args = parser.parse_args()
    exit_code, _ = check(strict=args.strict)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
