#!/usr/bin/env python3
"""R62 P0-01: 恢复信任链静态门禁 — 禁止 skip_strict_validation / *_override 绕过参数。

使用 Python ast 模块解析 services/ 下所有 .py 文件,检测违规:
1. 函数定义 (FunctionDef / AsyncFunctionDef) 形参中出现以下禁止参数:
   - skip_strict_validation
   - validation_note
   - backup_id_override
   - manifest_sha256_override
   - payload_key_override
   - ciphertext_sha256_override
   - plaintext_sha256_override
   - encryption_key_id_override
   (R62 P0-01 已移除这些绕过参数,必须通过 validate_and_restore_backup_strict
    走严格三段式验证路径,不可再以任何"兼容模式"跳过验证)

2. 函数调用 (Call) 关键字参数中出现上述禁止参数
   (调用方也不可传递这些参数)

背景:
    R61 P0-03 引入 skip_strict_validation + 6 个 *_override 参数,允许调用方
    在声称"已通过等效验证"时跳过 manifest/ciphertext/plaintext/key 完整性校验,
    等同于关闭信任链 — 任何能调用该函数的代码路径均可绕过验证。R62 P0-01
    彻底移除这些参数,强制所有恢复路径走严格三段式验证(payload.enc +
    manifest.json + COMPLETE marker)。

CI 调用方式(在 .github/workflows/release-gates.yml 中添加):
    - name: 恢复信任链无绕过门禁
      run: python scripts/check_restore_no_skip.py

退出码:
- 0: 无违规
- 1: 检测到违规参数
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 仅扫描 services/ 目录(R62 P0-01 信任链相关代码均在 services/ 下)
SCAN_DIR = REPO_ROOT / "services"

# 禁止的参数名(R62 P0-01 已移除 — 任何重新引入均视为违规)
FORBIDDEN_PARAMS: set[str] = {
    "skip_strict_validation",
    "validation_note",
    "backup_id_override",
    "manifest_sha256_override",
    "payload_key_override",
    "ciphertext_sha256_override",
    "plaintext_sha256_override",
    "encryption_key_id_override",
}

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
]


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。"""
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


def _iter_python_files() -> Iterable[Path]:
    """遍历 SCAN_DIR 下所有 .py 文件(跳过缓存目录)。"""
    if not SCAN_DIR.exists():
        return
    for py_file in SCAN_DIR.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


def _check_function_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, int, str]]:
    """检查函数定义的形参是否包含禁止参数。

    检查范围:
        - args.args          (位置参数)
        - args.kwonlyargs    (仅关键字参数)
        - args.posonlyargs   (仅位置参数,Python 3.8+)
        - args.vararg        (*args 名)
        - args.kwarg         (**kwargs 名)

    Returns:
        [(lineno, col_offset, param_name), ...]
    """
    violations: list[tuple[int, int, str]] = []

    # 位置参数 + 仅关键字参数 + 仅位置参数
    all_args = list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
    for arg in all_args:
        if isinstance(arg, ast.arg) and arg.arg in FORBIDDEN_PARAMS:
            violations.append((arg.lineno, arg.col_offset, arg.arg))

    # *args(vararg)
    if node.args.vararg and node.args.vararg.arg in FORBIDDEN_PARAMS:
        violations.append((
            node.args.vararg.lineno,
            node.args.vararg.col_offset,
            node.args.vararg.arg,
        ))

    # **kwargs(kwarg)
    if node.args.kwarg and node.args.kwarg.arg in FORBIDDEN_PARAMS:
        violations.append((
            node.args.kwarg.lineno,
            node.args.kwarg.col_offset,
            node.args.kwarg.arg,
        ))

    return violations


def _check_call_keywords(node: ast.Call) -> list[tuple[int, int, str]]:
    """检查函数调用是否在关键字参数中传递禁止参数。

    Returns:
        [(lineno, col_offset, param_name), ...]
    """
    violations: list[tuple[int, int, str]] = []
    for kw in node.keywords:
        if kw.arg is None:
            # **kwargs 展开,跳过(无法静态判断展开内容)
            continue
        if kw.arg in FORBIDDEN_PARAMS:
            violations.append((kw.lineno, kw.col_offset, kw.arg))
    return violations


def _find_violations(tree: ast.AST) -> list[tuple[int, int, str, str]]:
    """在 AST 中查找所有违规。

    Returns:
        [(lineno, col_offset, violation_type, param_name), ...]
        violation_type: "param" (函数形参) 或 "kwarg" (调用关键字参数)
    """
    violations: list[tuple[int, int, str, str]] = []

    for node in ast.walk(tree):
        # 1. 函数定义中的禁止形参
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for lineno, col, param in _check_function_def(node):
                violations.append((lineno, col, "param", param))

        # 2. 函数调用中的禁止关键字参数
        elif isinstance(node, ast.Call):
            for lineno, col, param in _check_call_keywords(node):
                violations.append((lineno, col, "kwarg", param))

    return violations


def check() -> tuple[int, list[dict]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, type, param}, ...]
    """
    violations: list[dict] = []
    scanned_count = 0

    for py_file in _iter_python_files():
        scanned_count += 1

        # 解析 AST
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # AST 解析失败时跳过该文件(不误报)
            continue

        # 查找违规
        file_violations = _find_violations(tree)
        for lineno, col, vtype, param in file_violations:
            violations.append({
                "file": _rel_posix(py_file),
                "line": lineno,
                "col": col,
                "type": vtype,
                "param": param,
            })

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处禁止的绕过参数 "
            f"(扫描 {scanned_count} 个 services/*.py 文件):"
        )
        for v in violations:
            type_label = "形参" if v["type"] == "param" else "调用关键字参数"
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"{type_label} {v['param']!r}"
            )
        print()
        print("R62 P0-01: 以下参数已移除,禁止重新引入:")
        for p in sorted(FORBIDDEN_PARAMS):
            print(f"  - {p}")
        print()
        print("替代方案:所有恢复路径必须通过 validate_and_restore_backup_strict()")
        print("走严格三段式验证(payload.enc + manifest.json + COMPLETE marker)。")
        print("旧格式备份请使用离线导入/迁移工具转换为三段式格式后再恢复。")
        return 1, violations

    print(
        f"[OK] 恢复信任链无绕过门禁检查通过 "
        f"(扫描 {scanned_count} 个 services/*.py 文件,无禁止参数)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    exit_code, _ = check()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
