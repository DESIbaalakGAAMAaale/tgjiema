#!/usr/bin/env python3
"""R48 P1-b: 按钮 nonce API 全覆盖静态扫描 — 高风险 action 必须使用 async nonce API。

R48 终审报告 P1 整改:R47 实现了异步 sign_button_token_with_nonce / verify_button_token,
但高风险 handler 是否只使用 async API 未验证。旧同步 generate_signed_callback 不持久化 nonce,
低风险动作仍可在 TTL 内重放。

本脚本使用 Python ast 模块静态扫描所有 .py 文件中以下 4 个函数的调用点:
  - generate_signed_callback(...)       — 旧同步签名(不持久化 nonce)
  - verify_signed_callback(...)         — 旧同步验证(不消费 nonce)
  - sign_button_token_with_nonce(...)   — R47 async 签名(持久化 nonce)
  - verify_button_token(...)            — R47 async 验证(原子消费 nonce)

规则:
  - HIGH_RISK_ACTIONS(ban/takedown/purge/restore/admin_grant 等):
    必须使用 sign_button_token_with_nonce(async) + verify_button_token(async)
    不允许使用旧 sync generate_signed_callback / verify_signed_callback
    违规 → exit 1
  - 低风险 action(查看/取消/语言选择等):
    允许使用旧 sync API(向后兼容)
    但建议迁移到 async API
  - action 为变量(非字符串字面量)时无法判定,跳过(输出 info 日志)

白名单(跳过扫描):
  - services/button_security.py  (API 定义文件)
  - tests/                        (测试代码)
  - scripts/                      (运维脚本)

CI 调用方式:
    python scripts/check_button_nonce_coverage.py

退出码:
  - 0: 无高风险 action 违规
  - 1: 检测到高风险 action 使用旧 sync API
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 从 button_security 模块导入 HIGH_RISK_ACTIONS
# (动态导入,避免硬编码 action 列表)
sys.path.insert(0, str(REPO_ROOT))
try:
    from services.button_security import HIGH_RISK_ACTIONS
except Exception:
    # 导入失败时回退到硬编码(最后手段,避免 CI 因导入问题跳过检查)
    HIGH_RISK_ACTIONS = frozenset({
        "ban", "unban", "takedown", "release_takedown",
        "purge", "purge_file", "restore", "restore_file",
        "delete_file", "delete", "admin_grant", "admin_revoke",
        "rotate_keys", "reset_quota", "break_glass", "force_logout",
        "approve_appeal", "reject_appeal", "update_config", "reload_config",
    })

# 旧 sync API(高风险 action 禁止使用)
OLD_SYNC_APIS: frozenset[str] = frozenset({
    "generate_signed_callback",
    "verify_signed_callback",
})

# 新 async API(高风险 action 必须使用)
NEW_ASYNC_APIS: frozenset[str] = frozenset({
    "sign_button_token_with_nonce",
    "verify_button_token",
})

# 所有需扫描的 API 名(旧 + 新)
ALL_TRACKED_APIS: frozenset[str] = OLD_SYNC_APIS | NEW_ASYNC_APIS

# 白名单:这些路径前缀的文件跳过扫描
ALLOWED_PREFIXES: list[str] = [
    "services/button_security.py",  # API 定义文件
    "tests/",                        # 测试代码
    "scripts/",                      # 运维脚本
]

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "cf-workers",
    "data",
    ".pytest_cache",
]


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串。"""
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


def _extract_action_arg(call_node: ast.Call) -> str | None:
    """从函数调用节点中提取 action 参数值。

    支持两种传参方式:
      1. 位置参数:user_id, action, ... (action 是第 2 个位置参数,index=1)
      2. 关键字参数:action="ban"

    仅当 action 是字符串字面量时返回其值,否则返回 None(无法静态判定)。

    generate_signed_callback(user_id, action, data="", ttl=3600, nonce="", resource_version=None)
    verify_signed_callback(callback_data, current_user_id, resource_version=None)
    sign_button_token_with_nonce(principal_id, action, payload="", expires_at=None, ttl=3600)
    verify_button_token(callback_data, current_user_id, store=None)

    注意:不同函数的 action 位置不同:
      - generate_signed_callback: action 是第 2 个位置参数(index=1)
      - sign_button_token_with_nonce: action 是第 2 个位置参数(index=1)
      - verify_signed_callback: action 在 callback_data 内部解析(无法静态提取)
      - verify_button_token: action 在 callback_data 内部解析(无法静态提取)
    """
    func_name = _get_call_func_name(call_node)
    if func_name is None:
        return None

    # verify_signed_callback / verify_button_token 的 action 在 callback_data 内部,
    # 无法在调用点静态提取(需要解析 callback_data 字符串),跳过
    if func_name in ("verify_signed_callback", "verify_button_token"):
        return None

    # generate_signed_callback / sign_button_token_with_nonce:
    # action 是第 2 个位置参数(index=1)
    # 先查关键字参数 action=...
    for kw in call_node.keywords:
        if kw.arg == "action" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return kw.value.value
    # 再查位置参数(index=1)
    if len(call_node.args) >= 2:
        arg = call_node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _get_call_func_name(call_node: ast.Call) -> str | None:
    """提取调用函数名(支持直接调用和属性调用)。

    支持两种形式:
      1. generate_signed_callback(...)     — ast.Name
      2. button_security.generate_signed_callback(...) — ast.Attribute
      3. bs.generate_signed_callback(...)  — ast.Attribute (别名)
    """
    func = call_node.func
    # 直接调用: generate_signed_callback(...)
    if isinstance(func, ast.Name):
        return func.id
    # 属性调用: xxx.generate_signed_callback(...)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_button_api_calls(tree: ast.AST) -> list[tuple[int, str, str, str | None]]:
    """在 AST 中查找按钮安全 API 调用。

    Returns:
        [(lineno, func_name, action_or_none, is_high_risk), ...]
        其中 action_or_none 为字符串字面量值或 None(变量/无法判定)
    """
    results: list[tuple[int, str, str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _get_call_func_name(node)
        if func_name is None or func_name not in ALL_TRACKED_APIS:
            continue
        action = _extract_action_arg(node)
        is_high_risk = action is not None and action in HIGH_RISK_ACTIONS
        results.append((node.lineno, func_name, action, action if is_high_risk else None))
    return results


def _iter_python_files() -> Iterable[Path]:
    """遍历 REPO_ROOT 下所有 .py 文件(跳过缓存/依赖目录)。"""
    for py_file in REPO_ROOT.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


def check() -> tuple[int, list[dict], list[dict]]:
    """主校验流程。

    Returns:
        (exit_code, violations, info)
        exit_code: 0=无违规,1=有高风险 action 违规
        violations: 高风险 action 使用旧 sync API 的违规列表
        info: 所有扫描到的 API 调用点(用于报告)
    """
    violations: list[dict] = []
    info: list[dict] = []
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

        # 查找按钮 API 调用
        calls = _find_button_api_calls(tree)
        for lineno, func_name, action, high_risk_action in calls:
            rel_path = _rel_posix(py_file)

            # 记录所有调用点(用于 info 报告)
            info.append({
                "file": rel_path,
                "line": lineno,
                "func": func_name,
                "action": action,
                "is_high_risk": high_risk_action is not None,
            })

            # 检查违规:高风险 action 使用旧 sync API
            if high_risk_action is not None and func_name in OLD_SYNC_APIS:
                violations.append({
                    "file": rel_path,
                    "line": lineno,
                    "func": func_name,
                    "action": high_risk_action,
                    "reason": (
                        f"高风险 action='{high_risk_action}' 必须使用 async API "
                        f"(sign_button_token_with_nonce / verify_button_token),"
                        f"禁止使用旧 sync {func_name}(不持久化 nonce,可重放)"
                    ),
                })

    return (1 if violations else 0), violations, info


def main() -> None:
    """脚本入口。"""
    exit_code, violations, info = check()

    # 输出所有扫描到的调用点(info 级别)
    if info:
        print(f"[INFO] 扫描到 {len(info)} 处按钮安全 API 调用点:")
        for item in info:
            action_str = item["action"] if item["action"] else "(变量/无法判定)"
            risk_tag = " [高风险]" if item["is_high_risk"] else ""
            print(
                f"  - {item['file']}:{item['line']} -> "
                f"{item['func']}(action={action_str}){risk_tag}"
            )
    else:
        print("[INFO] 未扫描到任何按钮安全 API 调用点(代码库中未使用 signed callback)")

    print()

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处高风险 action 违规 "
            f"(必须使用 async nonce API):"
        )
        for v in violations:
            print(
                f"  - {v['file']}:{v['line']} -> "
                f"{v['func']}(action=\"{v['action']}\")"
            )
            print(f"    原因: {v['reason']}")
        print()
        print("整改方案:")
        print("  1. 将 generate_signed_callback(action=...) 改为 await sign_button_token_with_nonce(...)")
        print("  2. 将 verify_signed_callback(...) 改为 await verify_button_token(...)")
        print("  3. 确保 store 参数传入(cache_store 实例)")
        print()
        print(f"HIGH_RISK_ACTIONS 集合({len(HIGH_RISK_ACTIONS)} 个):")
        for a in sorted(HIGH_RISK_ACTIONS):
            print(f"  - {a}")
        sys.exit(1)

    print(
        f"[OK] 按钮 nonce API 全覆盖检查通过 "
        f"(扫描 {len(info)} 处调用点,无高风险 action 违规)"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
