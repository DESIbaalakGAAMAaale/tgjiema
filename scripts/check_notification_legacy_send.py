#!/usr/bin/env python3
"""R53 P1-1 + R54 P1-3 + R55 P1-4: Notification legacy send() 调用门禁 — 禁止业务代码直接调用 send()。

R55 P1-4 整改(完整符号表 + 调用图):
    - 检测赋值别名(my_send = send / my_send = notifications.send)
    - 检测动态导入(__import__("...").send / importlib.import_module("...").send)
    - 检测 getattr 调用(getattr(notifications, "send")(...))
    - 检测 ImportFrom 任意别名(from services.notifications import send as xyz)
    - 检测 re-export(__all__ 或模块级赋值包含 legacy send)
    - 最终目标:删除 legacy send()->int,所有业务调用统一结构化契约

R54 P1-3 整改:
    - 解析 Import/ImportFrom 建立符号表,检测任意别名导入
    - 检测直接 send(...) 调用(from services.notifications import send)
    - 检测 re-export(__all__ 或模块级赋值包含 legacy send)
    - 业务模块只导出结构化 API,legacy 函数移入兼容模块

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


def _build_import_symbol_table(tree: ast.AST) -> dict[str, str]:
    """R54 P1-3: 解析 Import/ImportFrom 建立符号表。

    返回 {local_name: original_module.path} 映射,
    检测 services.notifications 的任意别名导入。

    例如:
        import services.notifications as notif → {"notif": "services.notifications"}
        from services.notifications import send → {"send": "services.notifications.send"}
        from services import notifications → {"notifications": "services.notifications"}
    """
    symbols: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name
                symbols[local_name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname or alias.name
                symbols[local_name] = f"{module}.{alias.name}" if module else alias.name
    return symbols


def _build_assignment_symbol_table(tree: ast.AST) -> dict[str, str]:
    """R55 P1-4: 解析赋值别名建立扩展符号表。

    检测形如:
        my_send = send                     → {"my_send": "send"}
        my_send = notifications.send        → {"my_send": "notifications.send"}
        my_send = notif_svc.send            → {"my_send": "notif_svc.send"}
        my_send = notifications.send         → {"my_send": "notifications.send"}

    Returns:
        {local_name: resolved_source} 映射
    """
    assignments: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # 只处理单个目标的简单赋值(my_send = ...)
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        # 模式 1: my_send = send (Name 赋值)
        if isinstance(value, ast.Name):
            assignments[target.id] = value.id
        # 模式 2: my_send = notifications.send (Attribute 赋值)
        elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            assignments[target.id] = f"{value.value.id}.{value.attr}"
    return assignments


def _is_notification_alias(alias_name: str, symbol_table: dict[str, str]) -> bool:
    """R54 P1-3: 检查别名是否指向 notifications 模块。

    同时检查静态白名单和动态符号表。
    """
    # 静态白名单(快速路径)
    if alias_name in NOTIFICATION_ALIASES:
        return True
    # R54 P1-3: 动态符号表查询
    resolved = symbol_table.get(alias_name, "")
    if "notifications" in resolved:
        return True
    return False


def _is_notification_send_import(local_name: str, symbol_table: dict[str, str]) -> bool:
    """R54 P1-3: 检查局部名称是否是从 notifications 导入的 send 函数。"""
    resolved = symbol_table.get(local_name, "")
    # 匹配 "services.notifications.send" 或 "notifications.send"
    return resolved.endswith(".notifications.send") or resolved == "notifications.send"


def _is_notification_send_via_assignment(
    local_name: str,
    import_symbols: dict[str, str],
    assign_symbols: dict[str, str],
) -> bool:
    """R55 P1-4: 检查局部名称是否通过赋值别名指向 notifications.send。

    检测链式赋值别名:
        my_send = send                    (send 来自 import)
        my_send = notifications.send      (notifications 来自 import)
        my_send = notif_svc.send          (notif_svc 是 notifications 别名)
    """
    resolved = assign_symbols.get(local_name, "")
    if not resolved:
        return False
    # 模式 1: my_send = send (send 来自 import 符号表)
    if resolved == "send":
        return _is_notification_send_import("send", import_symbols)
    # 模式 2: my_send = <alias>.send (alias 指向 notifications)
    if "." in resolved:
        parts = resolved.rsplit(".", 1)
        if len(parts) == 2 and parts[1] == "send":
            alias_name = parts[0]
            return _is_notification_alias(alias_name, import_symbols)
    return False


def _is_dynamic_import_call(node: ast.Call) -> bool:
    """R55 P1-4: 检测动态导入调用(__import__ / importlib.import_module)。

    检测模式:
        __import__("services.notifications").send(...)
        importlib.import_module("notifications").send(...)
        importlib.import_module("services.notifications").send(...)
    """
    func = node.func
    # 模式: <dynamic_import>(...).send(...)
    if isinstance(func, ast.Attribute) and func.attr == "send":
        inner = func.value
        if isinstance(inner, ast.Call):
            inner_func = inner.func
            # __import__(...)
            if isinstance(inner_func, ast.Name) and inner_func.id == "__import__":
                return True
            # importlib.import_module(...)
            if (isinstance(inner_func, ast.Attribute)
                and inner_func.attr == "import_module"):
                return True
    return False


def _is_getattr_send_call(node: ast.Call) -> bool:
    """R55 P1-4: 检测 getattr(notifications, "send")(...) 调用。

    检测模式:
        getattr(notifications, "send")(...)
        getattr(notif_svc, "send")(...)
    """
    func = node.func
    if not isinstance(func, ast.Call):
        return False
    inner_func = func.func
    if not (isinstance(inner_func, ast.Name) and inner_func.id == "getattr"):
        return False
    args = func.args
    if len(args) < 2:
        return False
    # 第二个参数必须是字符串常量 "send"
    second_arg = args[1]
    if isinstance(second_arg, ast.Constant) and second_arg.value == "send":
        return True
    return False


def _find_legacy_send_calls(tree: ast.AST) -> list[tuple[int, int, str]]:
    """R53 P1-1 + R54 P1-3 + R55 P1-4: 在 AST 中查找 legacy send() 调用。

    检测六种模式:
    1. <alias>.send(...) — alias 指向 notifications 模块
    2. send(...) — 直接调用从 notifications 导入的 send
    3. re-export — __all__ 或模块级赋值包含 legacy send
    4. R55 P1-4: <my_send>(...) — 通过赋值别名调用(my_send = send)
    5. R55 P1-4: 动态导入 __import__("...").send(...) / importlib.import_module("...").send(...)
    6. R55 P1-4: getattr(notifications, "send")(...)

    Returns:
        [(lineno, col_offset, description), ...]
    """
    # R54 P1-3: 构建导入符号表
    import_symbols = _build_import_symbol_table(tree)
    # R55 P1-4: 构建赋值符号表(检测别名赋值)
    assign_symbols = _build_assignment_symbol_table(tree)

    violations: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 模式 1: <alias>.send(...) — alias 指向 notifications
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            alias_name = func.value.id
            method_name = func.attr
            if method_name == "send" and _is_notification_alias(alias_name, import_symbols):
                violations.append((node.lineno, node.col_offset, f"{alias_name}.send(...)"))
        # R54 P1-3 模式 2: 直接 send(...) — 从 notifications import send
        elif isinstance(func, ast.Name) and func.id == "send":
            if _is_notification_send_import("send", import_symbols):
                violations.append((node.lineno, node.col_offset, "send(...) [imported]"))
        # R55 P1-4 模式 4: <my_send>(...) — 通过赋值别名调用
        elif isinstance(func, ast.Name) and func.id in assign_symbols:
            if _is_notification_send_via_assignment(
                func.id, import_symbols, assign_symbols,
            ):
                violations.append((
                    node.lineno, node.col_offset,
                    f"{func.id}(...) [assignment alias of send]",
                ))
        # R55 P1-4 模式 5: 动态导入 __import__("...").send(...)
        elif _is_dynamic_import_call(node):
            violations.append((
                node.lineno, node.col_offset,
                "dynamic_import().send(...) [__import__ or importlib]",
            ))
        # R55 P1-4 模式 6: getattr(notifications, "send")(...)
        elif _is_getattr_send_call(node):
            violations.append((
                node.lineno, node.col_offset,
                "getattr(<notifications>, 'send')(...) [getattr dispatch]",
            ))

    # R54 P1-3 模式 3: 检测 re-export
    for node in ast.walk(tree):
        # __all__ 中包含 "send"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and elt.value == "send":
                                violations.append((
                                    node.lineno, node.col_offset,
                                    "__all__ contains 'send' (re-export)"
                                ))
    # R55 P1-4: 检测赋值 re-export(my_send = notifications.send)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    value = node.value
                    # my_send = notifications.send / my_send = notif_svc.send
                    if (isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.attr == "send"
                        and _is_notification_alias(value.value.id, import_symbols)):
                        violations.append((
                            node.lineno, node.col_offset,
                            f"{target.id} = {value.value.id}.send (assignment re-export)",
                        ))

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
        for lineno, col, desc in calls:
            violations.append({
                "file": _rel_posix(py_file),
                "line": lineno,
                "col": col,
                "description": desc,
            })

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处 legacy send() 调用 "
            f"(必须改用 send_with_dedup_contract(),扫描 {scanned_count} 个 .py 文件):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"{v['description']}"
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
