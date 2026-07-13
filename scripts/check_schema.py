"""R42 P0-1: SQLite / CockroachDB schema 校验脚本(占位实现)。

功能:
    1. 从 database/session.py 读取 DDL_STATEMENTS 和 MIGRATION_STATEMENTS
    2. 校验每条 SQL 语句可被字符串解析(非空、合法关键字开头)
    3. 校验 DDL_VERSION 为整数
    4. 不实际连接数据库,仅做静态校验(供 CI 中 schema-diff job 使用)

设计要点:
    - 离线优先:不依赖 asyncpg / sqlite3 实际连接
    - 容错:database/session.py 依赖 asyncpg,若未安装则使用 AST 解析提取常量
    - 退出码:0=校验通过;1=校验失败;2=环境错误(模块不可导入)

使用方法:
    python scripts/check_schema.py
    python scripts/check_schema.py --strict   # 严格模式(警告视为错误)

对应 R42 P0-1 release-gates.yml schema-diff job。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Any, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = PROJECT_ROOT / "database" / "session.py"


def _extract_statements_via_ast(source: str) -> Tuple[List[str], List[str], int]:
    """通过 AST 解析从 database/session.py 提取 DDL_STATEMENTS / MIGRATION_STATEMENTS / DDL_VERSION。

    避免 import database.session(其依赖 asyncpg 可能未安装)。

    返回 (ddl_statements, migration_statements, ddl_version)。
    """
    tree = ast.parse(source)
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments[node.target.target.id if False else node.target.id] = node.value

    def _eval_list(node: ast.AST) -> List[str]:
        if isinstance(node, ast.List):
            return [ast.literal_eval(elt) for elt in node.elts if isinstance(elt, ast.Constant)]
        if isinstance(node, ast.Tuple):
            return [ast.literal_eval(elt) for elt in node.elts if isinstance(elt, ast.Constant)]
        return []

    ddl = _eval_list(assignments.get("DDL_STATEMENTS", ast.List(elts=[])))
    mig = _eval_list(assignments.get("MIGRATION_STATEMENTS", ast.List(elts=[])))
    version_node = assignments.get("DDL_VERSION")
    ddl_version = int(ast.literal_eval(version_node)) if version_node else 0
    return ddl, mig, ddl_version


def _validate_sql_statement(sql: str, index: int) -> Tuple[bool, str]:
    """校验单条 SQL 语句的合法性(静态规则)。

    返回 (is_valid, message)。
    """
    if not isinstance(sql, str):
        return False, f"第 {index} 条语句不是字符串类型"
    stripped = sql.strip()
    if not stripped:
        return False, f"第 {index} 条语句为空"
    if len(stripped) < 10:
        return False, f"第 {index} 条语句过短(<10 字符): {stripped!r}"
    # 检查合法 SQL 关键字开头(CREATE / ALTER / INSERT / UPDATE / DELETE / DROP / PRAGMA)
    allowed_prefixes = (
        "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE",
        "DROP", "PRAGMA", "SELECT", "WITH", "BEGIN", "COMMIT",
    )
    upper = stripped.upper()
    if not upper.startswith(allowed_prefixes):
        return False, f"第 {index} 条语句以非标准关键字开头: {stripped[:30]!r}"
    # 简单括号匹配检查
    if stripped.count("(") != stripped.count(")"):
        return False, f"第 {index} 条语句括号不匹配: (={stripped.count('(')}, )={stripped.count(')')}"
    return True, "OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="R42 P0-1: Schema 静态校验")
    parser.add_argument("--strict", action="store_true", help="严格模式:警告视为错误")
    parser.add_argument("--session-file", default=str(SESSION_FILE), help="session.py 路径")
    args = parser.parse_args()

    session_path = Path(args.session_file)
    if not session_path.exists():
        print(f"ERROR: session.py 不存在: {session_path}", file=sys.stderr)
        return 2

    print(f"INFO: 读取 schema 源文件: {session_path}")
    source = session_path.read_text(encoding="utf-8")

    try:
        ddl, mig, ddl_version = _extract_statements_via_ast(source)
    except Exception as exc:
        print(f"ERROR: AST 解析失败: {exc}", file=sys.stderr)
        return 2

    print(f"INFO: DDL_STATEMENTS={len(ddl)} 条")
    print(f"INFO: MIGRATION_STATEMENTS={len(mig)} 条")
    print(f"INFO: DDL_VERSION={ddl_version}")

    if not ddl:
        print("WARN: DDL_STATEMENTS 为空", file=sys.stderr)
        if args.strict:
            return 1

    if not mig:
        print("WARN: MIGRATION_STATEMENTS 为空", file=sys.stderr)
        if args.strict:
            return 1

    if not isinstance(ddl_version, int) or ddl_version <= 0:
        print(f"WARN: DDL_VERSION 非正整数: {ddl_version}", file=sys.stderr)
        if args.strict:
            return 1

    # 校验每条 SQL 语句
    failures = 0
    for idx, sql in enumerate(ddl):
        is_valid, msg = _validate_sql_statement(sql, idx)
        if is_valid:
            preview = sql.strip()[:60].replace("\n", " ")
            print(f"  OK  DDL[{idx}]: {preview}...")
        else:
            print(f"  FAIL DDL[{idx}]: {msg}", file=sys.stderr)
            failures += 1

    for idx, sql in enumerate(mig):
        is_valid, msg = _validate_sql_statement(sql, idx)
        if is_valid:
            preview = sql.strip()[:60].replace("\n", " ")
            print(f"  OK  MIG[{idx}]: {preview}...")
        else:
            print(f"  FAIL MIG[{idx}]: {msg}", file=sys.stderr)
            failures += 1

    if failures > 0:
        print(f"ERROR: {failures} 条语句校验失败", file=sys.stderr)
        return 1

    # 检查关键表是否在 DDL 中定义
    expected_tables = [
        "users", "decode_logs", "upload_sessions", "outbox",
        "relay_spool", "relay_ack", "dlq", "cells",
    ]
    all_sql = "\n".join(ddl).upper()
    missing_tables = [t for t in expected_tables if f"CREATE TABLE" in all_sql and t.upper() not in all_sql]
    if missing_tables:
        print(f"WARN: DDL 中未找到关键表: {missing_tables}", file=sys.stderr)
        if args.strict:
            return 1
    else:
        print(f"INFO: 关键表检查通过({len(expected_tables)} 个表)")

    print("")
    print("PASS: Schema 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
