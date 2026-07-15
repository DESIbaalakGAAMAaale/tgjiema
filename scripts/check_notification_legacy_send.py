#!/usr/bin/env python3
"""R53 P1-1: Notification legacy send() 调用门禁 — 禁止业务代码直接调用 send()。

使用 Python ast 模块解析 bots/ / admin/ / services/ 下的 .py 文件,
检测违规直接调用 ``notifications.send(...)``(legacy int 返回契约)。

R53 P1-1 背景:
    - 旧 ``send()`` 返回 int(notif_id 或 0),调用方无法区分"去重命中"
      与"真实写失败"(都返回 0)
    - 新 ``send_with_dedup_contract()`` 返回结构化 dict,明确区分
      sent / deduplicated / error 三种状态
    - R53 P1-1 已将 ``send()`` 改为委托 ``send_with_dedup_contract()``,
      但业务代码应直接使用 ``send_with_dedup_contract()`` 获取结构化契约
    - 本门禁强制新业务代码使用 ``send_with_dedup_contract()``,禁止
      新增 ``notifications.send(...)`` 调用(legacy 入口仅保留向后兼容)

允许的调用方(白名单):
- services/notifications.py  (定义文件本身,send() 内部委托)
- tests/                     (测试代码)
- scripts/                   (运维脚本)

检测模式:
    <module_alias>.send(...) 其中 module_alias 为以下之一:
    - notifications
    - notif_svc
    - _notif_svc
    - notif_service
    - _notifications

    注:``send_with_dedup_contract`` 不被误匹配(方法名精确匹配 "send")。

CI 调用方式:
    python scripts/check_notification_legacy_send.py

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

# notifications 模块的常见别名(从 import 语句中收集)
# 这些是已知的别名模式,AST 扫描时按此白名单匹配
NOTIFICATION_ALIASES: list[str] = [
    "notifications",
    "notif_svc",
    "_notif_svc",
    "notif_service",
    "_notifications",
]

# 允许直接调用 send() 的文件路径前缀(相对 REPO_ROOT,使用 POSIX 路径)
# 命中任一前缀的文件将被跳过
ALLOWED_PREFIXES: list[str] = [
    # notifications.py 自身(send 定义 + 内部委托)
    "services/notifications.py",
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

# 待扫描的业务代码目录(相对 REPO_ROOT)
SCAN_DIRS: list[str] = ["bots", "admin", "services"]


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


def _find_legacy_send_calls(tree: ast.AST) -> list[tuple[int, int, str]]:
    """在 AST 中查找 legacy send() 调用。

    匹配模式: <alias>.send(...)
    其中 alias 在 NOTIFICATION_ALIASES 中,且方法名精确为 "send"
    (不匹配 "send_with_dedup_contract")。

    Returns:
        [(lineno, col_offset, alias_name), ...]
    """
    violations: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 仅检测直接属性调用: <alias>.<method>(...)
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name):
            continue
        alias_name = func.value.id
        method_name = func.attr
        # 精确匹配 "send"(不匹配 "send_with_dedup_contract")
        if method_name != "send":
            continue
        if alias_name in NOTIFICATION_ALIASES:
            violations.append((node.lineno, node.col_offset, alias_name))
    return violations


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


def check() -> tuple[int, list[dict]]:
    """主校验流程。

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
        violations: 违规列表 [{file, line, col, alias}, ...]
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

        # 查找 legacy send 调用
        calls = _find_legacy_send_calls(tree)
        for lineno, col, alias in calls:
            violations.append({
                "file": _rel_posix(py_file),
                "line": lineno,
                "col": col,
                "alias": alias,
            })

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处 legacy send() 调用 "
            f"(必须改用 send_with_dedup_contract(),扫描 {scanned_count} 个 .py 文件):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"{v['alias']}.send(...)"
            )
        print()
        print("修复方式: 将 <alias>.send(...) 改为 <alias>.send_with_dedup_contract(...)")
        print("  send_with_dedup_contract 返回结构化 dict {status, notif_id, outbox_id},")
        print("  可明确区分 sent / deduplicated / error 三种状态。")
        print()
        print("允许直接调用 send() 的路径(白名单):")
        for p in ALLOWED_PREFIXES:
            print(f"  - {p}")
        return 1, violations

    print(
        f"[OK] Notification legacy send() 门禁检查通过 "
        f"(扫描 {scanned_count} 个 .py 文件,无违规调用)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    exit_code, _ = check()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
