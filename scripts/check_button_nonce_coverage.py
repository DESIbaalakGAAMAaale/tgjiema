#!/usr/bin/env python3
"""R49 P1-b: 按钮 nonce API 全覆盖静态扫描 — 高风险 action 必须使用 async nonce API。

R49 整改:R48 scanner 输出"未扫描到任何调用点"是错误的——代码库中有大量
CallbackQueryHandler 注册点,但旧 scanner 仅扫描 4 个 API 函数的直接调用,
遗漏了 CallbackQueryHandler 注册点。本版本新增 CallbackQueryHandler 检测。

扫描内容:
  1. 直接 API 调用(generate_signed_callback / verify_signed_callback /
     sign_button_token_with_nonce / verify_button_token)
  2. CallbackQueryHandler 注册点(通过 AST 找 CallbackQueryHandler(...) 调用)
  3. 对每个 CallbackQueryHandler 的 callback 函数,检测是否调用 verify_button_token
     或 verify_signed_callback

规则:
  - HIGH_RISK_ACTIONS(ban/takedown/purge/restore/admin_grant 等)+ 模式匹配
    (action 包含 delete/ban/purge/takedown/force_join/rotate/demote/promote/
     maintenance/recover/restore 等子串也视为高风险):
    必须使用 sign_button_token_with_nonce(async) + verify_button_token(async)
    不允许使用旧 sync generate_signed_callback / verify_signed_callback
    违规 → exit 1
  - 低风险 action(查看/取消/语言选择等):
    允许使用旧 sync API(向后兼容)
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

# R49 P1-b: 高风险 action 模式匹配(子串匹配,扩大检测范围)
# 除 HIGH_RISK_ACTIONS 精确匹配外,action/callback_data 包含以下子串也视为高风险
HIGH_RISK_ACTION_PATTERNS: frozenset[str] = frozenset({
    "delete", "ban", "purge", "takedown", "force_join",
    "rotate", "demote", "promote", "maintenance", "recover",
    "restore", "admin_grant", "admin_revoke", "reset_quota",
    "break_glass", "force_logout", "appeal",
    "update_config", "reload_config",
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
# R67 P1-08: scripts/ 不再整体跳过 — 通过 `is_skippable_script()` 细粒度判断。
# 仅 GATE_SCANNERS 可跳过;OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描。
ALLOWED_PREFIXES: list[str] = [
    "services/button_security.py",  # API 定义文件
    "tests/",                        # 测试代码
]

# R67 P1-08: scripts/ 下可跳过的文件清单(从 _script_categories 导入)
try:
    from scripts._script_categories import is_skippable_script as _is_skippable_script_p1_08
except ImportError:
    # _script_categories 不可用时 fail-closed:不跳过任何 scripts/ 文件
    def _is_skippable_script_p1_08(rel_path: str) -> bool:
        return False

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
    ".claude",  # Claude IDE 工作目录(worktrees/sessions),非项目代码
]


def _is_high_risk_action(action: str) -> bool:
    """R49 P1-b: 判断 action 是否为高风险(精确匹配 + 模式匹配)。

    精确匹配:action in HIGH_RISK_ACTIONS
    模式匹配:action 包含 HIGH_RISK_ACTION_PATTERNS 中任一子串(不区分大小写)
    """
    if action in HIGH_RISK_ACTIONS:
        return True
    action_lower = action.lower()
    for pattern in HIGH_RISK_ACTION_PATTERNS:
        if pattern in action_lower:
            return True
    return False


def _is_high_risk_pattern(pattern_str: str) -> bool:
    """R49 P1-b: 判断 CallbackQueryHandler pattern 字符串是否暗示高风险 action。

    检查 pattern 字符串是否包含 HIGH_RISK_ACTION_PATTERNS 中任一子串。
    """
    pattern_lower = pattern_str.lower()
    for pattern in HIGH_RISK_ACTION_PATTERNS:
        if pattern in pattern_lower:
            return True
    return False


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


def _extract_action_arg(call_node: ast.Call) -> str | None:
    """从函数调用节点中提取 action 参数值。

    支持两种传参方式:
      1. 位置参数:user_id, action, ... (action 是第 2 个位置参数,index=1)
      2. 关键字参数:action="ban"

    仅当 action 是字符串字面量时返回其值,否则返回 None(无法静态判定)。

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


def _find_button_api_calls(tree: ast.AST) -> list[tuple[int, str, str, str | None]]:
    """在 AST 中查找按钮安全 API 调用。

    Returns:
        [(lineno, func_name, action_or_none, high_risk_action_or_none), ...]
        其中 action_or_none 为字符串字面量值或 None(变量/无法判定)
        high_risk_action_or_none 为 action 字符串(若高风险)或 None
    """
    results: list[tuple[int, str, str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _get_call_func_name(node)
        if func_name is None or func_name not in ALL_TRACKED_APIS:
            continue
        action = _extract_action_arg(node)
        # R49 P1-b: 使用 _is_high_risk_action(精确匹配 + 模式匹配)
        is_high_risk = action is not None and _is_high_risk_action(action)
        results.append((node.lineno, func_name, action, action if is_high_risk else None))
    return results


def _check_func_uses_verify(func_node: ast.AST) -> str | None:
    """R49 P1-b: 检测函数体是否调用 verify_button_token 或 verify_signed_callback。

    Args:
        func_node: FunctionDef / AsyncFunctionDef 节点

    Returns:
        "async" if calls verify_button_token (R47 async,推荐)
        "sync"  if calls verify_signed_callback (legacy sync,不推荐)
        None    if neither found
    """
    uses_async = False
    uses_sync = False
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func_name = _get_call_func_name(node)
        if func_name == "verify_button_token":
            uses_async = True
        elif func_name == "verify_signed_callback":
            uses_sync = True
    if uses_async:
        return "async"
    if uses_sync:
        return "sync"
    return None


def _find_callback_handlers(tree: ast.AST) -> list[dict]:
    """R49 P1-b: 查找所有 CallbackQueryHandler 注册点。

    通过 AST 找 CallbackQueryHandler(callback, pattern=...) 调用,
    并检测 callback 函数是否调用 verify_button_token / verify_signed_callback。

    Returns:
        [{"line": ..., "callback": ..., "pattern": ..., "action": ...,
          "is_high_risk": ..., "uses_verify": ...}, ...]
    """
    handlers: list[dict] = []
    # 收集所有函数定义(用于查找 callback 函数体)
    func_defs: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_defs[node.name] = node

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _get_call_func_name(node)
        if func_name != "CallbackQueryHandler":
            continue
        # 提取 callback 函数名(第一个位置参数)
        callback_name: str | None = None
        if node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                callback_name = arg.id
            elif isinstance(arg, ast.Attribute):
                callback_name = arg.attr
        # 提取 pattern(关键字参数或第二个位置参数)
        pattern_str: str | None = None
        for kw in node.keywords:
            if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    pattern_str = kw.value.value
        if pattern_str is None and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                pattern_str = arg.value

        # 从 pattern 提取 action 名(用于高风险判定)
        action: str | None = pattern_str  # 用 pattern 字符串作为 action 标识

        # 判定是否高风险(pattern 包含高风险子串)
        is_high_risk = bool(pattern_str and _is_high_risk_pattern(pattern_str))

        # 检测 callback 函数是否调用 verify API
        uses_verify: str | None = None
        if callback_name and callback_name in func_defs:
            uses_verify = _check_func_uses_verify(func_defs[callback_name])

        handlers.append({
            "line": node.lineno,
            "callback": callback_name,
            "pattern": pattern_str,
            "action": action,
            "is_high_risk": is_high_risk,
            "uses_verify": uses_verify,
        })
    return handlers


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
        info: 所有扫描到的 API 调用点 + CallbackQueryHandler 注册点
              (每项含 "type" 字段: "api_call" 或 "callback_handler")
    """
    violations: list[dict] = []
    info: list[dict] = []

    for py_file in _iter_python_files():
        # 白名单中的文件跳过
        if _is_allowed(py_file):
            continue

        # 解析 AST
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # AST 解析失败时跳过该文件(不误报)
            continue

        rel_path = _rel_posix(py_file)

        # 1. 查找按钮安全 API 直接调用
        calls = _find_button_api_calls(tree)
        for lineno, func_name, action, high_risk_action in calls:
            # 记录所有调用点(用于 info 报告)
            info.append({
                "type": "api_call",
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
                    "violation_type": "HIGH_RISK_SYNC_API",
                    "reason": (
                        f"高风险 action='{high_risk_action}' 必须使用 async API "
                        f"(sign_button_token_with_nonce / verify_button_token),"
                        f"禁止使用旧 sync {func_name}(不持久化 nonce,可重放)"
                    ),
                })

        # 2. R49 P1-b: 查找 CallbackQueryHandler 注册点
        handlers = _find_callback_handlers(tree)
        for handler in handlers:
            info.append({
                "type": "callback_handler",
                "file": rel_path,
                "line": handler["line"],
                "callback": handler["callback"],
                "pattern": handler["pattern"],
                "action": handler["action"],
                "is_high_risk": handler["is_high_risk"],
                "uses_verify": handler["uses_verify"],
            })

    return (1 if violations else 0), violations, info


def main() -> None:
    """脚本入口。"""
    exit_code, violations, info = check()

    # 分类统计
    api_calls = [i for i in info if i.get("type") == "api_call"]
    callback_handlers = [i for i in info if i.get("type") == "callback_handler"]

    # 输出 API 调用点(info 级别)
    if api_calls:
        print(f"[INFO] 扫描到 {len(api_calls)} 处按钮安全 API 调用点:")
        for item in api_calls:
            action_str = item["action"] if item["action"] else "(变量/无法判定)"
            risk_tag = " [高风险]" if item["is_high_risk"] else ""
            print(
                f"  - {item['file']}:{item['line']} -> "
                f"{item['func']}(action={action_str}){risk_tag}"
            )
    else:
        print("[INFO] 未扫描到任何按钮安全 API 直接调用点")

    print()

    # R49 P1-b: 输出 CallbackQueryHandler 注册点
    if callback_handlers:
        print(f"[INFO] 扫描到 {len(callback_handlers)} 处 CallbackQueryHandler 注册点:")
        for item in callback_handlers:
            risk_tag = " [高风险]" if item["is_high_risk"] else ""
            verify_tag = ""
            if item["uses_verify"] == "async":
                verify_tag = " [uses:verify_button_token]"
            elif item["uses_verify"] == "sync":
                verify_tag = " [uses:verify_signed_callback]"
            elif item["uses_verify"] is None:
                verify_tag = " [uses:无verify]"
            callback_str = item["callback"] or "(未知)"
            pattern_str = item["pattern"] or "(无pattern)"
            print(
                f"  - {item['file']}:{item['line']} -> "
                f"CallbackQueryHandler({callback_str}, pattern={pattern_str})"
                f"{risk_tag}{verify_tag}"
            )
    else:
        print("[INFO] 未扫描到任何 CallbackQueryHandler 注册点")

    print()

    if violations:
        print(
            f"[FAIL] 检测到 {len(violations)} 处高风险 action 违规 "
            f"(必须使用 async nonce API):"
        )
        for v in violations:
            # R49 P1-b: 输出格式 {file}:{line} [{action}] {violation_type}: {description}
            print(
                f"  {v['file']}:{v['line']} [{v['action']}] "
                f"{v['violation_type']}: {v['reason']}"
            )
        print()
        print("整改方案:")
        print("  1. 将 generate_signed_callback(action=...) 改为 await sign_button_token_with_nonce(...)")
        print("  2. 将 verify_signed_callback(...) 改为 await verify_button_token(...)")
        print("  3. 确保 store 参数传入(cache_store 实例)")
        print()
        print(f"HIGH_RISK_ACTIONS 集合({len(HIGH_RISK_ACTIONS)} 个精确匹配) + "
              f"模式匹配({len(HIGH_RISK_ACTION_PATTERNS)} 个子串):")
        for a in sorted(HIGH_RISK_ACTIONS | HIGH_RISK_ACTION_PATTERNS):
            print(f"  - {a}")
        sys.exit(1)

    print(
        f"[OK] 按钮 nonce API 全覆盖检查通过 "
        f"(API 调用点 {len(api_calls)} 处, "
        f"CallbackQueryHandler 注册点 {len(callback_handlers)} 处, "
        f"无高风险 action 违规)"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
