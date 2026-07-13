#!/usr/bin/env python3
"""R42 P1-11: CommandBus 静态门禁 — 禁止高风险 API 直接调用。

使用 Python ast 模块解析所有 .py 文件,检测违规直接调用以下高风险 API:
- backup_engine.restore(...)            — 必须通过 CommandBus
- content_reports.takedown_file(...)     — 必须通过 CommandBus
- content_reports.takedown_content(...)  — 必须通过 CommandBus(实际函数名)
- content_reports.ban_user(...)          — 必须通过 CommandBus
- rbac.assign_role(...)                  — 必须通过 CommandBus
- rbac.revoke_role(...)                  — 必须通过 CommandBus
- maintenance_mode.disable(...)          — 必须通过 CommandBus
                                            (允许 disable_with_authorization)
- users.purge_user(...)                   — 必须通过 CommandBus
- credentials.rotate(...)                 — 必须通过 CommandBus

允许的调用方(白名单):
- services/approval_executor.py  (执行 CommandBus 调度的高风险操作)
- services/command_bus.py         (CommandBus 本身)
- services/approval_workflow.py   (审批后入队 command_outbox)
- services/backup_engine.py       (高风险 API 的定义文件)
- services/content_reports.py     (高风险 API 的定义文件)
- services/rbac.py                (高风险 API 的定义文件)
- services/maintenance_mode.py    (高风险 API 的定义文件)
- services/disaster_recovery.py   (灾备恢复执行器,合法调用)
- services/db_restore.py          (数据库恢复执行器,合法调用)
- services/db_backup.py           (数据库备份执行器,合法调用)
- tests/                          (测试代码)
- scripts/                        (运维脚本)

禁止的调用方(必须检测):
- bots/                  (Bot handler 不能直接调用高风险 API)
- admin/                 (Admin Web 不能直接调用)
- services/r40_scheduler.py (定时任务不能直接调用)

CI 调用方式(在 .github/workflows/release-gates.yml 中添加):
    - name: CommandBus 静态门禁
      run: python scripts/check_commandbus_gate.py

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

# 高风险 API 调用模式: (模块名, 方法名)
# 注: maintenance_mode.disable 精确匹配 "disable",不匹配 "disable_with_authorization"
HIGH_RISK_PATTERNS: list[tuple[str, str]] = [
    ("backup_engine", "restore"),
    ("content_reports", "takedown_file"),
    ("content_reports", "takedown_content"),
    ("content_reports", "ban_user"),
    ("rbac", "assign_role"),
    ("rbac", "revoke_role"),
    ("maintenance_mode", "disable"),
    ("users", "purge_user"),
    ("credentials", "rotate"),
]

# 允许的调用方路径前缀(相对 REPO_ROOT,使用 POSIX 路径)
# 命中任一前缀的文件将被跳过(允许直接调用高风险 API)
ALLOWED_PREFIXES: list[str] = [
    # CommandBus 调度链(任务明确允许)
    "services/approval_executor.py",
    "services/command_bus.py",
    "services/approval_workflow.py",
    # 高风险 API 的定义文件自身(允许定义 + 内部实现调用)
    "services/backup_engine.py",
    "services/content_reports.py",
    "services/rbac.py",
    "services/maintenance_mode.py",
    # 灾备 / 数据库恢复执行器(合法调用高风险 API)
    "services/disaster_recovery.py",
    "services/db_restore.py",
    "services/db_backup.py",
    # 测试与运维脚本
    "tests/",
    "scripts/",
]

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "cf-workers",  # 前端 worker,非 Python 调用方
    "data",        # 运行时数据
    ".pytest_cache",
]


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
    """检查文件路径是否在白名单前缀中。"""
    rel = _rel_posix(path)
    for prefix in ALLOWED_PREFIXES:
        if rel == prefix or rel.startswith(prefix):
            return True
    return False


def _find_high_risk_calls(tree: ast.AST) -> list[tuple[int, int, str, str]]:
    """在 AST 中查找高风险调用。

    匹配模式: <module_name>.<method_name>(...)
    其中 module_name 是 ast.Name 节点的 id,method_name 是 ast.Attribute 的 attr。

    Returns:
        [(lineno, col_offset, module_name, method_name), ...]
    """
    violations: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 仅检测直接属性调用: <name>.<method>(...)
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name):
            continue
        module_name = func.value.id
        method_name = func.attr
        for pattern_module, pattern_method in HIGH_RISK_PATTERNS:
            if module_name == pattern_module and method_name == pattern_method:
                violations.append(
                    (node.lineno, node.col_offset, module_name, method_name)
                )
                break
    return violations


def _iter_python_files() -> Iterable[Path]:
    """遍历 REPO_ROOT 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for py_file in REPO_ROOT.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


def check() -> tuple[int, list[dict]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, module, method}, ...]
    """
    violations: list[dict] = []
    scanned_count = 0

    for py_file in _iter_python_files():
        # 白名单中的文件跳过
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

        # 查找高风险调用
        calls = _find_high_risk_calls(tree)
        for lineno, col, module, method in calls:
            violations.append({
                "file": _rel_posix(py_file),
                "line": lineno,
                "col": col,
                "module": module,
                "method": method,
            })

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处高风险 API 直接调用 "
            f"(必须通过 CommandBus,扫描 {scanned_count} 个 .py 文件):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"{v['module']}.{v['method']}(...)"
            )
        print()
        print("允许直接调用高风险 API 的路径(白名单):")
        for p in ALLOWED_PREFIXES:
            print(f"  - {p}")
        return 1, violations

    print(
        f"[OK] CommandBus 静态门禁检查通过 "
        f"(扫描 {scanned_count} 个 .py 文件,无违规直接调用)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    exit_code, _ = check()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
