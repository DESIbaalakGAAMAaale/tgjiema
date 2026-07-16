#!/usr/bin/env python3
"""R56 P0-6: CI AST 静态扫描 — 密码安全规则门禁。

扫描 admin/、services/、bots/、database/ 下的 Python 文件,
使用 AST 检测以下密码安全违规:

规则 1: 禁止 password 参数出现在 f-string 中 (如 f"...{password}...")
规则 2: 禁止 password 被传递到 print() 或 logger.*() 调用
规则 3: 禁止 UPDATE admin_principals SQL 使用字符串拼接 (必须用参数化查询)
规则 4: 禁止 password_hash 出现在 f-string、print()、logger.*() 中
规则 5: 禁止接受 password 参数的函数通过字符串拼接构造 SQL

白名单(跳过扫描):
    - admin/passwords.py  (密码哈希处理模块,合法操作)
    - __pycache__/         (缓存目录)
    - test_*.py            (测试文件)

CI 调用方式:
    python scripts/check_password_safety.py --strict

退出码:
    0 — 通过(无违规,或非 strict 模式仅警告)
    1 — 发现违规(--strict 模式)
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 待扫描的目录(相对 REPO_ROOT)
SCAN_DIRS: list[str] = ["admin", "services", "bots", "database"]

# 跳过的目录名(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
]

# 白名单文件(允许处理密码的模块)
ALLOWED_FILES: list[str] = [
    "admin/passwords.py",  # 密码哈希处理模块,合法操作
]

# 敏感变量名(不可出现在日志/输出中)
SENSITIVE_NAMES: frozenset[str] = frozenset({"password", "password_hash"})

# SQL 关键字(用于检测字符串拼接构造 SQL)
SQL_KEYWORDS: tuple[str, ...] = (
    "select", "insert", "update", "delete",
    "alter", "create", "drop", "where", "values", "set",
)

# 触发 Rule 3 的 SQL 片段(小写匹配)
ADMIN_PRINCIPALS_SQL = "update admin_principals"


# ════════════════════════════════════════════════════════════
# 路径工具函数
# ════════════════════════════════════════════════════════════
def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。

    若文件不在 REPO_ROOT 内,返回绝对路径的 POSIX 形式。
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(包含缓存/依赖目录名)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _is_allowed(path: Path) -> bool:
    """检查文件是否在白名单中(允许处理密码)。"""
    rel = _rel_posix(path)
    for allowed in ALLOWED_FILES:
        if rel == allowed:
            return True
    return False


def _is_test_file(path: Path) -> bool:
    """检查是否是测试文件(test_*.py 或 *_test.py)。"""
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _iter_python_files() -> Iterable[Path]:
    """遍历 SCAN_DIRS 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for scan_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / scan_dir
        if not scan_path.exists():
            continue
        for py_file in scan_path.rglob("*.py"):
            if _is_skipped_path(py_file):
                continue
            yield py_file


# ════════════════════════════════════════════════════════════
# AST 检测辅助函数
# ════════════════════════════════════════════════════════════
def _get_sensitive_name(node: ast.AST) -> str | None:
    """从 AST 节点提取敏感变量名(支持 Name 和 Attribute)。

    - ast.Name(id="password")       → "password"
    - ast.Attribute(attr="password") → "password"  (如 self.password)
    - ast.Name(id="password_hash")   → "password_hash"
    """
    if isinstance(node, ast.Name) and node.id in SENSITIVE_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in SENSITIVE_NAMES:
        return node.attr
    return None


def _is_print_call(call_node: ast.Call) -> bool:
    """检查是否是 print() 调用。"""
    func = call_node.func
    return isinstance(func, ast.Name) and func.id == "print"


def _is_logger_call(call_node: ast.Call) -> bool:
    """检查是否是 logger.*() 或 self.logger.*() 调用。

    支持两种形式:
      1. logger.info(...)       — ast.Attribute(value=ast.Name(id="logger"))
      2. self.logger.info(...)  — ast.Attribute(value=ast.Attribute(
                                   value=ast.Name(id="self"), attr="logger"))
    """
    func = call_node.func
    if not isinstance(func, ast.Attribute):
        return False
    # logger.info(...) / logger.error(...) 等
    if isinstance(func.value, ast.Name) and func.value.id == "logger":
        return True
    # self.logger.info(...) 等
    if (isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
            and func.value.attr == "logger"):
        return True
    return False


def _is_print_or_logger_call(call_node: ast.Call) -> bool:
    """检查是否是 print() 或 logger.*() 调用。"""
    return _is_print_call(call_node) or _is_logger_call(call_node)


def _call_args_have_sensitive_name(call_node: ast.Call) -> list[str]:
    """检查调用参数中是否包含敏感变量名(直接 Name/Attribute 引用)。

    检查位置参数和关键字参数的值。
    返回匹配到的敏感变量名列表。
    """
    found: list[str] = []
    # 位置参数
    for arg in call_node.args:
        name = _get_sensitive_name(arg)
        if name:
            found.append(name)
    # 关键字参数(检查 value,不检查 arg 名)
    for kw in call_node.keywords:
        name = _get_sensitive_name(kw.value)
        if name:
            found.append(name)
    return found


def _fstring_has_sensitive_name(joined_str: ast.JoinedStr) -> list[str]:
    """检查 f-string 中是否引用了敏感变量名。

    遍历 FormattedValue 节点,提取 {expression} 中的变量名。
    支持 {password}、{self.password}、{password!r} 等形式。
    """
    found: list[str] = []
    for value in joined_str.values:
        if isinstance(value, ast.FormattedValue):
            name = _get_sensitive_name(value.value)
            if name:
                found.append(name)
    return found


def _binop_has_admin_principals_sql(binop_node: ast.BinOp) -> bool:
    """检查 BinOp(Add) 是否涉及 UPDATE admin_principals SQL 字符串拼接。

    检测模式:
      "UPDATE admin_principals SET ..." + variable
      variable + "UPDATE admin_principals SET ..."
    """
    for operand in (binop_node.left, binop_node.right):
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
            if ADMIN_PRINCIPALS_SQL in operand.value.lower():
                return True
    return False


def _binop_is_sql_concat(binop_node: ast.BinOp) -> bool:
    """检查 BinOp(Add) 是否是 SQL 字符串拼接(任一操作数为含 SQL 关键字的字符串)。

    用于 Rule 5:检测接受 password 参数的函数中是否有 SQL 拼接。
    """
    for operand in (binop_node.left, binop_node.right):
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
            lower = operand.value.lower()
            if any(kw in lower for kw in SQL_KEYWORDS):
                return True
    return False


def _function_has_password_param(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """检查函数是否有 password 参数(包括位置参数、关键字参数、*args、**kwargs)。"""
    args = func_node.args
    all_arg_names: set[str] = set()
    # 位置参数(posonlyargs + args)
    for a in getattr(args, "posonlyargs", []):
        all_arg_names.add(a.arg)
    for a in args.args:
        all_arg_names.add(a.arg)
    # 关键字参数
    for a in args.kwonlyargs:
        all_arg_names.add(a.arg)
    # *args
    if args.vararg:
        all_arg_names.add(args.vararg.arg)
    # **kwargs
    if args.kwarg:
        all_arg_names.add(args.kwarg.arg)
    return "password" in all_arg_names


def _walk_skip_nested(node: ast.AST) -> Iterable[ast.AST]:
    """递归遍历节点,跳过嵌套的 FunctionDef/AsyncFunctionDef/Lambda。

    用于 Rule 5:只检查当前函数体内的 SQL 拼接,
    不检查嵌套函数体内的(嵌套函数有自己的作用域)。
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield from _walk_skip_nested(child)


def _walk_func_body_no_nested(
    func_node: ast.AST,
) -> Iterable[ast.AST]:
    """遍历函数体中的所有节点,但不进入嵌套函数定义。"""
    for child in ast.iter_child_nodes(func_node):
        yield from _walk_skip_nested(child)


# ════════════════════════════════════════════════════════════
# 主扫描逻辑
# ════════════════════════════════════════════════════════════
def scan_file(path: Path) -> list[tuple[int, str]]:
    """扫描单个 Python 文件,返回 [(line_no, detail), ...] 违规列表。

    使用 AST 解析,检测 5 类密码安全违规。
    """
    findings: list[tuple[int, str]] = []

    # 读取文件内容(容忍编码错误)
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings

    # 解析 AST
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return findings

    # 收集所有函数定义(用于 Rule 5)
    func_defs = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    # ── Rule 1+4: 检查 f-string 中的敏感变量名 ──
    # 禁止 password / password_hash 出现在 f-string 中
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            names = _fstring_has_sensitive_name(node)
            for name in names:
                findings.append((
                    node.lineno,
                    f"P0-6 规则1/4: 敏感变量 '{name}' 出现在 f-string 中 "
                    f"(禁止 password/password_hash 进入 f-string)",
                ))

    # ── Rule 2+4: 检查 print()/logger.*() 调用中的敏感变量名 ──
    # 禁止 password / password_hash 被直接传递到 print() 或 logger.*()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_print_or_logger_call(node):
            names = _call_args_have_sensitive_name(node)
            for name in names:
                findings.append((
                    node.lineno,
                    f"P0-6 规则2/4: 敏感变量 '{name}' 被传递到 print()/logger.*() 调用 "
                    f"(禁止日志/输出中包含密码)",
                ))

    # ── Rule 3: 检查 UPDATE admin_principals SQL 字符串拼接 ──
    # 禁止通过 + 拼接构造 UPDATE admin_principals SQL
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if _binop_has_admin_principals_sql(node):
                findings.append((
                    node.lineno,
                    "P0-6 规则3: UPDATE admin_principals SQL 使用字符串拼接 "
                    "(必须使用参数化查询,禁止 + 拼接 SQL)",
                ))

    # ── Rule 5: 检查接受 password 参数的函数是否使用字符串拼接构造 SQL ──
    # 如果函数有 password 参数,则函数体内不允许任何 SQL 字符串拼接
    for func in func_defs:
        if not _function_has_password_param(func):
            continue
        for node in _walk_func_body_no_nested(func):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                if _binop_is_sql_concat(node):
                    findings.append((
                        node.lineno,
                        f"P0-6 规则5: 函数 '{func.name}' 接受 password 参数 "
                        f"但使用字符串拼接构造 SQL (必须使用参数化查询)",
                    ))
                    break  # 每个函数只报告一次

    return findings


def collect_findings() -> list[tuple[str, int, str]]:
    """收集所有文件的违规,返回 [(file, line_no, detail), ...]。"""
    all_findings: list[tuple[str, int, str]] = []
    for py_file in _iter_python_files():
        # 跳过白名单文件和测试文件
        if _is_allowed(py_file) or _is_test_file(py_file):
            continue
        file_findings = scan_file(py_file)
        rel = _rel_posix(py_file)
        for line_no, detail in file_findings:
            all_findings.append((rel, line_no, detail))
    return all_findings


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════
def main() -> int:
    """脚本入口。返回退出码(0=通过,1=有违规)。"""
    parser = argparse.ArgumentParser(
        description="R56 P0-6: 密码安全 AST 静态扫描门禁",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式:发现任何违规即 exit 1(用于 CI 门禁)",
    )
    args = parser.parse_args()

    findings = collect_findings()

    if not findings:
        print("[OK] R56 P0-6 密码安全检查通过: 未发现违规")
        return 0

    # 打印所有违规(file:line:detail 格式)
    print(f"[FAIL] R56 P0-6 密码安全检查: 发现 {len(findings)} 处违规:")
    for file, line, detail in findings:
        print(f"  {file}:{line}: {detail}")

    print()
    print("修复建议:")
    print("  规则1/2/4: 禁止 password/password_hash 出现在 f-string、print()、logger.*() 中")
    print("  规则3/5:   禁止字符串拼接构造 SQL,必须使用参数化查询 (cursor.execute(sql, params))")
    print("  白名单:    admin/passwords.py (密码哈希模块,允许处理密码)")

    if args.strict:
        return 1

    # 非 strict 模式:仅警告,exit 0
    print()
    print("[WARN] 非 --strict 模式: 仅警告,不阻断 CI。CI 中应使用 --strict。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
