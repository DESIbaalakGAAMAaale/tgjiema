#!/usr/bin/env python3
"""R64 P1-05: MFA sync verifier 调用门禁 — 阻止生产目录直接调用旧 sync verifier。

整改背景(R64 终审报告 P1-05):
  admin/mfa.py 中存在两个 MFA receipt 验证函数:
    * verify_mfa_receipt(...)              — sync 旧版本(deprecated,
                                             仅做 L1 吊销缓存快速路径检查)
    * verify_mfa_receipt_authoritative(...) — async 权威版本(签名 + age +
                                             SQLite 权威吊销 ledger 查询 +
                                             可选一次性 CAS 消费)
  生产代码(bots/ services/ admin/)已全部迁移到 async 权威版本,但缺乏 CI
  门禁阻止未来回归。本脚本用 Python ast 模块扫描生产目录所有 .py 文件,
  静态阻断任何对 sync 旧 verifier 的直接 import 或 call。

检测规则:
  Rule 1 (import 违规): `from admin.mfa import verify_mfa_receipt`
    即 import 语句从 admin.mfa 导入 sync 版本(精确匹配 name
    `verify_mfa_receipt`,不匹配 `verify_mfa_receipt_authoritative`)。
  Rule 2 (call 违规):   任何 `verify_mfa_receipt(...)` 调用,包括
    ast.Name(id='verify_mfa_receipt') 形式与 ast.Attribute(attr=
    'verify_mfa_receipt') 形式(例如 manager.verify_mfa_receipt(...))。
    `verify_mfa_receipt_authoritative(...)` 调用合规,不被误报。

白名单(允许调用的文件):
  - admin/mfa.py    — 定义文件,内部可调用 sync primitive
  - tests/          — 测试可调用
  - scripts/        — 门禁脚本自身

CI 调用方式(在 .github/workflows/ci.yml 的 static-gates job 中添加):
    - name: Check MFA verifier gate (R64 P1-05)
      run: python scripts/check_mfa_verifier_gate.py

退出码:
  - 0: 无违规
  - 1: 检测到违规调用
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 仅扫描生产目录(bots/ services/ admin/)
SCAN_DIRS: list[str] = ["bots", "services", "admin"]

# 白名单(允许直接调用 sync verifier 的文件/目录,相对 REPO_ROOT,POSIX 路径)
# 命中任一前缀的文件将被跳过(允许直接调用)
# R67 P1-08: scripts/ 不再整体跳过 — 通过 `is_skippable_script()` 细粒度判断。
# 仅 GATE_SCANNERS 可跳过;OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描。
ALLOWED_PREFIXES: list[str] = [
    "admin/mfa.py",  # sync verifier 的定义文件本身
    "tests/",        # 测试代码
]

# R67 P1-08: scripts/ 下可跳过的文件清单(从 _script_categories 导入)
try:
    from scripts._script_categories import is_skippable_script as _is_skippable_script_p1_08
except ImportError:
    # _script_categories 不可用时 fail-closed:不跳过任何 scripts/ 文件
    def _is_skippable_script_p1_08(rel_path: str) -> bool:
        return False

# 跳过的目录名(出现在路径中即跳过)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
]

# sync 旧 verifier 函数名(精确匹配,与 authoritative 版本区分)
SYNC_VERIFIER_NAME = "verify_mfa_receipt"

# 视为 sync verifier 来源的 import 模块名(允许 admin.mfa 与相对 .mfa 两种形式)
SYNC_VERIFIER_SOURCE_MODULES: set[str] = {"admin.mfa", "mfa"}


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。

    若文件不在 REPO_ROOT 内,返回绝对路径的 POSIX 形式。
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(在 SKIP_DIR_PARTS 中)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _is_allowed(path: Path) -> bool:
    """检查文件路径是否在白名单前缀中。

    R67 P1-08: scripts/ 细粒度判断 — 仅 GATE_SCANNERS 可跳过;
    OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描。
    """
    rel = _rel_posix(path)
    for prefix in ALLOWED_PREFIXES:
        if rel == prefix or rel.startswith(prefix):
            return True
    # R67 P1-08: scripts/ 细粒度判断
    if rel.startswith("scripts/") and _is_skippable_script_p1_08(rel):
        return True
    return False


def _iter_python_files() -> Iterable[Path]:
    """遍历 SCAN_DIRS 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for dir_name in SCAN_DIRS:
        dir_path = REPO_ROOT / dir_name
        if not dir_path.is_dir():
            continue
        for py_file in dir_path.rglob("*.py"):
            if _is_skipped_path(py_file):
                continue
            yield py_file


def _find_import_violations(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Rule 1: 检测 `from admin.mfa import verify_mfa_receipt`。

    匹配 ast.ImportFrom 节点,module 为 admin.mfa(或同包内相对 .mfa),
    且 names 中存在精确等于 SYNC_VERIFIER_NAME 的别名。
    `verify_mfa_receipt_authoritative` 的 name 不同,自然不会被误报。

    Returns:
        [(lineno, col_offset, imported_name), ...]
    """
    violations: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module not in SYNC_VERIFIER_SOURCE_MODULES:
            continue
        for alias in node.names:
            # 精确匹配 sync verifier 名(不匹配 _authoritative 后缀)
            if alias.name == SYNC_VERIFIER_NAME:
                violations.append((node.lineno, node.col_offset, alias.name))
    return violations


def _find_call_violations(tree: ast.AST) -> list[tuple[int, int, str, str]]:
    """Rule 2: 检测 `verify_mfa_receipt(...)` 调用(直接或属性访问)。

    匹配 ast.Call 节点:
      - func 为 ast.Name 且 id == SYNC_VERIFIER_NAME(直接调用)
      - func 为 ast.Attribute 且 attr == SYNC_VERIFIER_NAME(方法调用,
        例如 mfa_manager.verify_mfa_receipt(...))

    `verify_mfa_receipt_authoritative(...)` 的 Name.id / Attribute.attr
    与 SYNC_VERIFIER_NAME 不同,自然不会被误报。

    Returns:
        [(lineno, col_offset, call_repr, kind), ...]
    """
    violations: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == SYNC_VERIFIER_NAME:
            violations.append((node.lineno, node.col_offset, func.id, "name"))
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == SYNC_VERIFIER_NAME
        ):
            violations.append(
                (node.lineno, node.col_offset, func.attr, "attribute")
            )
    return violations


def check() -> tuple[int, list[dict]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, rule, detail}, ...]
    """
    violations: list[dict] = []
    scanned_count = 0

    for py_file in _iter_python_files():
        # 白名单中的文件跳过(允许直接调用 sync verifier)
        if _is_allowed(py_file):
            continue
        scanned_count += 1

        # 解析 AST
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # AST 解析失败时跳过该文件(不误报)
            continue

        rel = _rel_posix(py_file)

        # Rule 1: import 违规
        for lineno, col, name in _find_import_violations(tree):
            violations.append({
                "file": rel,
                "line": lineno,
                "col": col,
                "rule": "Rule 1 (import 违规)",
                "detail": f"from admin.mfa import {name}",
            })

        # Rule 2: call 违规
        for lineno, col, name, kind in _find_call_violations(tree):
            if kind == "name":
                detail = f"{name}(...) 直接调用 sync verifier"
            else:
                detail = f"obj.{name}(...) 属性调用 sync verifier"
            violations.append({
                "file": rel,
                "line": lineno,
                "col": col,
                "rule": "Rule 2 (call 违规)",
                "detail": detail,
            })

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处 sync verify_mfa_receipt 违规 "
            f"(扫描 {scanned_count} 个 .py 文件):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} "
                f"[{v['rule']}] {v['detail']}"
            )
        print()
        print("允许调用 sync verifier 的路径(白名单):")
        for p in ALLOWED_PREFIXES:
            print(f"  - {p}")
        print()
        print(
            "整改建议:生产代码改用 async verify_mfa_receipt_authoritative()。"
        )
        return 1, violations

    print(
        "[OK] MFA verifier gate 通过:生产目录无 sync verify_mfa_receipt 调用"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    exit_code, _ = check()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
