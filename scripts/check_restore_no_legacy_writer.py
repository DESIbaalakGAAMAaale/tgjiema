#!/usr/bin/env python3
"""R65 P0-07 / P1-07: capability-seal 静态门禁 — 禁止生产代码直接调用旧 restore writer。

使用 Python ast 模块解析全仓 .py 文件,检测违规调用以下"旧 restore writer"函数:

默认模式(legacy writer 私有/CLI 入口):
    - _restore_from_backup_data     (services/db_restore.py 私有写入器)
    - _restore_crdb_tables          (services/db_restore.py 私有 CRDB 子写入器)
    - _restore_sqlite_tables_to_db  (services/db_restore.py 私有 SQLite 子写入器)
    - run_restore                   (services/db_restore.py CLI 入口,已被 capability-seal)

--strict 模式(额外检测 strict service / backup wrapper 公共入口):
    - validate_and_restore_backup_strict  (services/backup_dr_validate.py 公共入口)
    - restore_from_backup                (services/db_backup.py 公共 wrapper)

白名单(允许直接调用的位置):
    - services/db_restore.py            (writer 模块自身,定义这些函数)
    - services/backup_dr_validate.py    (strict service,合法调用 _restore_from_backup_data)
    - services/restore_backends.py      (RestoreBackend 实现)
    - services/restore_orchestrator.py  (状态机编排)
    - services/error_codes.py           (仅引用错误码字符串,非调用)
    - tests/                            (测试逃生舱,配合 ALLOW_LEGACY_RESTORE=1)
    - scripts/                          (gate 脚本本身,自我引用)

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
    tests/ + scripts/ + 已 whitelisted 的 services/ 模块使用。

CI 调用方式:
    # ci.yml(static-gates job)— 默认模式,捕获直接调用私有 writer
    python scripts/check_restore_no_legacy_writer.py

    # release-gates.yml(--strict 模式,捕获所有调用包括 strict service)
    python scripts/check_restore_no_legacy_writer.py --strict

退出码:
    - 0: 无违规
    - 1: 检测到违规
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

# 白名单文件(POSIX 相对路径)— 这些文件允许直接调用 legacy writer
# - db_restore.py: writer 模块自身(定义这些函数,内部互相调用)
# - backup_dr_validate.py: strict service,合法调用 _restore_from_backup_data
# - restore_backends.py: RestoreBackend 实现
# - restore_orchestrator.py: 状态机编排(若直接调用 legacy writer)
# - error_codes.py: 仅引用错误码字符串(RESTORE_LEGACY_WRITER_SEALED)
WHITELIST_FILES: set[str] = {
    "services/db_restore.py",
    "services/backup_dr_validate.py",
    "services/restore_backends.py",
    "services/restore_orchestrator.py",
    "services/error_codes.py",
}

# 白名单目录(POSIX 相对路径前缀)— 这些目录下所有文件允许直接调用 legacy writer
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
    """检查文件是否在白名单中(允许直接调用 legacy writer)。"""
    rel = _rel_posix(path)
    # 白名单文件(精确匹配)
    if rel in WHITELIST_FILES:
        return True
    # 白名单目录(前缀匹配)
    for prefix in WHITELIST_DIR_PREFIXES:
        if rel.startswith(prefix):
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


def _find_violations(
    tree: ast.AST,
    legacy_funds: set[str],
) -> list[tuple[int, int, str]]:
    """在 AST 中查找所有违规调用。

    Returns:
        [(lineno, col_offset, func_name), ...]
    """
    violations: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _get_call_func_name(node)
            if func_name and func_name in legacy_funds:
                violations.append((node.lineno, node.col_offset, func_name))
    return violations


def check(strict: bool = False) -> tuple[int, list[dict]]:
    """主校验流程。

    Args:
        strict: 是否启用 --strict 模式(扫描更广的 legacy writer 集合)

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, func}, ...]
    """
    # 构建当前模式的 legacy writer 函数集合
    legacy_funds = set(LEGACY_WRITER_FUNDS_DEFAULT)
    if strict:
        legacy_funds |= LEGACY_WRITER_FUNDS_STRICT_EXTRA

    violations: list[dict] = []
    scanned_count = 0
    whitelisted_skipped = 0

    for py_file in _iter_python_files():
        rel = _rel_posix(py_file)
        # 白名单文件跳过(允许直接调用 legacy writer)
        if _is_whitelisted(py_file):
            whitelisted_skipped += 1
            continue
        scanned_count += 1

        # 解析 AST
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # AST 解析失败时跳过该文件(不误报)
            continue

        # 查找违规
        file_violations = _find_violations(tree, legacy_funds)
        for lineno, col, func_name in file_violations:
            violations.append({
                "file": rel,
                "line": lineno,
                "col": col,
                "func": func_name,
            })

    if violations:
        mode_label = "--strict" if strict else "default"
        print(
            f"[FAIL] 检测到 {len(violations)} 处违规调用旧 restore writer "
            f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
            f"白名单跳过 {whitelisted_skipped} 个文件):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"调用 {v['func']!r}"
            )
        print()
        print("R65 P0-07 / P1-07: 旧直接 restore writer 已被 capability-seal,")
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
        print("白名单(允许直接调用 legacy writer):")
        for f in sorted(WHITELIST_FILES):
            print(f"  - {f}")
        for d in sorted(WHITELIST_DIR_PREFIXES):
            print(f"  - {d}*")
        return 1, violations

    mode_label = "--strict" if strict else "default"
    print(
        f"[OK] R65 P0-07 / P1-07 capability-seal 门禁检查通过 "
        f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
        f"白名单跳过 {whitelisted_skipped} 个文件,无违规调用)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R65 P0-07 / P1-07: capability-seal 静态门禁 — "
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
