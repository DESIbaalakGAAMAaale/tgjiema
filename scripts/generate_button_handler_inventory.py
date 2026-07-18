#!/usr/bin/env python3
"""R61 P1-09: 按钮 handler 清单生成器 — 全域状态机证明。

审计需求 P1-09: 按钮安全基础设施已存在(ButtonFlow / ButtonTokenStore /
CommandBus / sign_button_token_with_nonce),但需要 PROVE 所有高风险 handler
都走 ButtonFlow/CommandBus,而不只是关键路径。本生成器扫描 admin/services/bots
下的所有按钮式 handler 入口,产出确定性 inventory JSON,供门禁与审计消费。

扫描入口类型:
  1. ``CallbackQueryHandler(callback, pattern=...)`` 注册点(Telegram 按钮)
  2. ``@app.post(...)`` / ``@router.post(...)`` FastAPI 端点(Web 表单/按钮)
  3. ``ButtonFlow(...)`` 实例化 / ``get_button_flow()`` 调用 /
     ``register_button_handler(...)`` 注册点(新式按钮流程,排除定义文件自身)
  4. 子分发器(menu_callback 风格:注册的 callback 函数体内调用 ``_handle_*_action``
     等辅助函数,每个辅助函数按 data.startswith 分发不同 action)

  注:CallbackQueryHandler 注册点与 callback 函数定义可能跨文件
  (如 bots/admin_bot/run.py 注册 menu_callback,定义在 callback.py),
  因此生成器先构建跨模块函数索引再解析 callback 体。

对每个 handler 记录:
  - handler 名 / file / line / entry_type / route_or_pattern
  - action_type(启发式:ban/takedown/purge/restore/delete/maintenance/...)
  - routes_through_command_bus(函数体调用 bus.execute / CommandBus / make_*_command)
  - routes_through_button_flow(函数体调用 ButtonFlow / get_button_flow /
    consume_token_cas)
  - is_high_risk(动作类型 ∈ HIGH_RISK_ACTION_TYPES,或调用 make_*_command,
    或调用破坏性 API)
  - calls_destructive_api(直接调 update_user_and_invalidate /
    update_file_record_and_invalidate / purge_* / delete_user_data /
    store.delete / 物理 DELETE)
  - uses_signed_token_api(函数体调用 sign_button_token_with_nonce /
    verify_button_token / consume_token_cas / create_token)
  - bypass_reason(高风险但未走 CommandBus/ButtonFlow 时的原因,供门禁消费)

确定性保证:
  - 文件按相对路径排序遍历
  - handler 列表按 (file, line, handler_name, entry_type) 排序输出
  - generated_at 使用固定占位符(R61-P1-09),避免时间戳导致 CI diff 噪音

输出: ``scripts/button_handler_inventory.json``
退出码: 0(生成器总是成功;门禁由 ``check_button_handler_gate.py`` 负责)

CI 调用方式:
    python scripts/generate_button_handler_inventory.py
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 输出文件(与生成器同目录)
OUTPUT_FILE = Path(__file__).parent / "button_handler_inventory.json"

# 待扫描的目录(相对 REPO_ROOT)
SCAN_DIRS: list[str] = ["admin", "services", "bots"]

# 跳过的子目录/文件名片段
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "cf-workers",
    "data",
    ".pytest_cache",
    ".claude",
    "migrations",
    "static",
    "templates",
]

# ButtonFlow / ButtonSecurity 定义文件自身跳过(避免把类方法当成 handler 入口)
BUTTON_INFRA_FILES: frozenset[str] = frozenset({
    "services/button_flow.py",
    "services/button_security.py",
    "services/command_bus.py",
})

# CommandBus 命令工厂前缀(调用这些函数 = 通过 CommandBus 路由)
COMMAND_BUS_FACTORY_PREFIXES: frozenset[str] = frozenset({
    "make_takedown_command",
    "make_ban_user_command",
    "make_unban_user_command",
    "make_assign_role_command",
    "make_restore_backup_command",
    "make_enable_maintenance_command",
    "make_disable_maintenance_command",
    "make_purge_data_command",
    "make_delete_file_command",
    "make_factory_reset_command",
    "make_set_r2_command",
    "make_restore_content_command",
})

# CommandBus 路由标记(函数体出现这些调用 = 走 CommandBus)
COMMAND_BUS_ROUTING_NAMES: frozenset[str] = frozenset({
    "CommandBus",
    "bus.execute",
    "get_command_bus",
}) | COMMAND_BUS_FACTORY_PREFIXES

# ButtonFlow 路由标记(函数体出现这些调用 = 走 ButtonFlow)
BUTTON_FLOW_ROUTING_NAMES: frozenset[str] = frozenset({
    "ButtonFlow",
    "get_button_flow",
    "get_button_token_store",
    "consume_token_cas",
    "create_token",
    "ButtonTokenStore",
})

# 破坏性 API(高风险 handler 禁止直接调用,必须走 CommandBus)
# Rule A: 禁止直接调 data_lifecycle.purge_* / admin.purge / store.delete /
#         update_user_and_invalidate(写 is_banned) / update_file_record_and_invalidate /
#         delete_user_data / 物理 DELETE
DESTRUCTIVE_API_NAMES: frozenset[str] = frozenset({
    "update_user_and_invalidate",
    "update_file_record_and_invalidate",
    "delete_user_data",
    "cleanup_expired_data",
    "purge_data",
    "purge_channel",
    "factory_reset",
    "delete_file",
    "delete_pending_file_code",
})

# 高风险动作类型(基于 services/button_security.HIGH_RISK_ACTIONS +
# services/command_bus.HIGH_RISK_COMMAND_REGISTRY key)
# 注:break_glass 不在此列 — break_glass_login 是认证端点(仅创建 session),
# 实际 break_glass 破坏性操作在 services/data_lifecycle.py 内走
# execute_high_risk_command_uow,不属于按钮 handler 入口。
HIGH_RISK_ACTION_TYPES: frozenset[str] = frozenset({
    "ban", "unban",
    "takedown", "release_takedown",
    "purge", "purge_file", "purge_data", "purge_channel",
    "restore", "restore_file", "restore_backup", "restore_content",
    "delete", "delete_file", "delete_user_data",
    "admin_grant", "admin_revoke", "assign_role",
    "rotate_keys", "reset_quota",
    "force_logout",
    "approve_appeal", "reject_appeal",
    "update_config", "reload_config",
    "maintenance_enable", "maintenance_disable",
    "factory_reset",
    "report_action",
})

# 签名绑定 API(Rule C:高风险 callback handler 必须使用其一)
SIGNED_TOKEN_API_NAMES: frozenset[str] = frozenset({
    "sign_button_token_with_nonce",
    "verify_button_token",
    "consume_token_cas",
    "create_token",
})


# ════════════════════════════════════════════════════════════════
# 路径辅助
# ════════════════════════════════════════════════════════════════


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(命中 SKIP_DIR_PARTS)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    return False


def _is_infra_file(rel_path: str) -> bool:
    """检查文件是否为按钮基础设施定义文件(跳过 ButtonFlow handler 扫描)。"""
    return rel_path in BUTTON_INFRA_FILES


def _iter_scan_files() -> Iterable[Path]:
    """遍历 SCAN_DIRS 下所有 .py 文件(按路径排序,保证确定性)。"""
    collected: list[Path] = []
    for scan_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / scan_dir
        if not scan_path.exists():
            continue
        for py_file in scan_path.rglob("*.py"):
            if _is_skipped_path(py_file):
                continue
            collected.append(py_file)
    collected.sort(key=lambda p: _rel_posix(p))
    return collected


# ════════════════════════════════════════════════════════════════
# AST 辅助
# ════════════════════════════════════════════════════════════════


def _get_call_func_name(call_node: ast.Call) -> str | None:
    """提取调用函数名(支持直接调用 / 属性调用)。

    - ``generate_signed_callback(...)``           → ``generate_signed_callback``
    - ``button_security.generate_signed_callback(...)`` → ``generate_signed_callback``
    - ``bus.execute(...)``                        → ``execute``
    """
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _get_attribute_chain(node: ast.AST) -> str:
    """还原属性调用链(如 ``bus.execute`` → ``bus.execute``)。

    用于检测 ``bus.execute`` / ``store.delete`` 等复合调用名。
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _collect_func_defs(tree: ast.AST) -> dict[str, ast.AST]:
    """收集模块内所有顶层函数定义(name → node)。"""
    func_defs: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_defs[node.name] = node
    return func_defs


def _scan_body_for_names(func_node: ast.AST) -> set[str]:
    """扫描函数体内所有 Call 节点的函数名(直接名 + 属性链名)。"""
    names: set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        short = _get_call_func_name(node)
        if short:
            names.add(short)
        full = _get_attribute_chain(node.func)
        if full and "." in full:
            names.add(full)
    return names


def _classify_action_type(handler_name: str, route_or_pattern: str | None) -> str:
    """启发式分类 action_type(基于 handler 名 + route/pattern 子串)。"""
    text = f"{handler_name} {route_or_pattern or ''}".lower()

    # 优先级:破坏性 > 配置 > 读取/导航
    if "ban" in text and "unban" not in text:
        return "ban"
    if "unban" in text:
        return "unban"
    if "takedown" in text:
        return "takedown"
    if "purge" in text:
        return "purge"
    if "factory_reset" in text:
        return "factory_reset"
    if "restore" in text:
        return "restore"
    if "delete" in text or "delfile" in text:
        return "delete"
    if "assign_role" in text or "admin_grant" in text or "admin_revoke" in text:
        return "assign_role"
    if "rotate" in text:
        return "rotate_keys"
    if "maintenance" in text:
        return "maintenance"
    if "break_glass" in text or "break-glass" in text:
        return "break_glass"
    if "appeal" in text:
        return "appeal"
    if "login" in text or "logout" in text or "mfa" in text:
        return "auth"
    if "toggle_status" in text or "set_expiry" in text or "set_access_limit" in text:
        return "config"
    if "report:" in text:
        # report:ban/detach/block 属于举报处理(高风险)
        return "report_action"
    if "_handle_report" in text or "report_action" in text:
        # 子分发器 _handle_report_action 处理 report:ban/detach/block(高风险)
        return "report_action"
    if "report" in text:
        # report_req 只是创建举报记录(低风险)
        return "report_create"
    if "menu" in text or "pg|" in text or "page" in text or "list" in text or "stats" in text:
        return "navigate"
    if "status" in text or "health" in text or "logs" in text or "files" in text:
        return "read"
    if "upload" in text or "opt|" in text:
        return "upload"
    return "unknown"


def _is_high_risk_action_type(action_type: str) -> bool:
    """判断 action_type 是否为高风险(基于动作类型,不含 break_glass/auth)。"""
    return action_type in HIGH_RISK_ACTION_TYPES


def _detect_routes(
    func_node: ast.AST,
) -> tuple[bool, bool, bool, str | None]:
    """检测函数体路由方式 + 破坏性 API 调用。

    Returns:
        (routes_through_command_bus, routes_through_button_flow,
         calls_destructive_api, destructive_api_name)
    """
    names = _scan_body_for_names(func_node)
    routes_bus = bool(names & COMMAND_BUS_ROUTING_NAMES)
    routes_flow = bool(names & BUTTON_FLOW_ROUTING_NAMES)
    destructive_hits = names & DESTRUCTIVE_API_NAMES
    calls_destructive = bool(destructive_hits)
    destructive_name = sorted(destructive_hits)[0] if destructive_hits else None
    return routes_bus, routes_flow, calls_destructive, destructive_name


def _infer_action_type_from_calls(names: set[str]) -> str:
    """根据函数体调用的命令工厂推断 action_type。"""
    if "make_ban_user_command" in names:
        return "ban"
    if "make_unban_user_command" in names:
        return "unban"
    if "make_takedown_command" in names:
        return "takedown"
    if "make_restore_backup_command" in names:
        return "restore"
    if "make_delete_file_command" in names:
        return "delete"
    if "make_purge_data_command" in names:
        return "purge"
    if "make_enable_maintenance_command" in names:
        return "maintenance"
    if "make_disable_maintenance_command" in names:
        return "maintenance"
    if "make_assign_role_command" in names:
        return "assign_role"
    if "make_factory_reset_command" in names:
        return "factory_reset"
    if "update_user_and_invalidate" in names:
        return "ban"
    if "update_file_record_and_invalidate" in names:
        return "delete"
    return "unknown"


# ════════════════════════════════════════════════════════════════
# 入口检测器
# ════════════════════════════════════════════════════════════════


def _find_callback_query_handlers(
    tree: ast.AST, local_funcs: dict[str, ast.AST],
    global_funcs: dict[str, tuple[str, ast.AST]],
) -> list[dict]:
    """查找 ``CallbackQueryHandler(callback, pattern=...)`` 注册点。

    Args:
        tree: 当前文件的 AST
        local_funcs: 当前文件的函数定义 {name → node}
        global_funcs: 跨模块函数索引 {name → (rel_path, node)}

    Returns:
        [{handler, line, pattern, callback_node, callback_file}, ...]
    """
    handlers: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _get_call_func_name(node) != "CallbackQueryHandler":
            continue
        callback_name: str | None = None
        if node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                callback_name = arg.id
            elif isinstance(arg, ast.Attribute):
                callback_name = arg.attr
        pattern_str: str | None = None
        for kw in node.keywords:
            if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    pattern_str = kw.value.value
        if pattern_str is None and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                pattern_str = arg.value
        # 优先用本地函数定义,其次跨模块索引
        callback_node: ast.AST | None = None
        callback_file: str | None = None
        if callback_name:
            if callback_name in local_funcs:
                callback_node = local_funcs[callback_name]
                callback_file = None  # 与注册点同文件,后续填
            elif callback_name in global_funcs:
                callback_file, callback_node = global_funcs[callback_name]
        handlers.append({
            "handler": callback_name or "(anonymous)",
            "line": node.lineno,
            "pattern": pattern_str,
            "callback_node": callback_node,
            "callback_file": callback_file,
        })
    return handlers


def _find_fastapi_post_endpoints(tree: ast.AST) -> list[dict]:
    """查找 ``@app.post(...)`` / ``@router.post(...)`` 装饰的函数。"""
    endpoints: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            route: str | None = None
            is_post = False
            if isinstance(dec, ast.Call):
                dec_func = dec.func
                if isinstance(dec_func, ast.Attribute) and dec_func.attr == "post":
                    if isinstance(dec_func.value, ast.Name):
                        is_post = True
                if is_post and dec.args:
                    arg = dec.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        route = arg.value
            if is_post:
                endpoints.append({
                    "handler": node.name,
                    "line": node.lineno,
                    "route": route,
                    "func_node": node,
                })
                break
    return endpoints


def _find_button_flow_handlers(
    tree: ast.AST, rel_path: str,
) -> list[dict]:
    """查找 ``ButtonFlow(...)`` 实例化 / ``get_button_flow()`` /
    ``register_button_handler(...)`` 调用所在的函数。

    跳过按钮基础设施定义文件自身(services/button_flow.py 等),
    避免把类方法当成 handler 入口。
    """
    if _is_infra_file(rel_path):
        return []
    handlers: list[dict] = []
    func_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_nodes.append(node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_func_name(node)
        if name not in BUTTON_FLOW_ROUTING_NAMES and name != "register_button_handler":
            continue
        enclosing = _find_enclosing_func(node, func_nodes)
        if enclosing is None:
            continue
        handlers.append({
            "handler": enclosing.name,
            "line": node.lineno,
            "func_node": enclosing,
        })
    return handlers


def _find_enclosing_func(
    target: ast.AST, func_nodes: list[ast.AST],
) -> ast.AST | None:
    """找到包含 target 节点的最近函数定义节点。"""
    best: ast.AST | None = None
    best_line = -1
    for func in func_nodes:
        if not hasattr(func, "lineno") or not hasattr(target, "lineno"):
            continue
        if func.lineno <= target.lineno:
            for child in ast.walk(func):
                if child is target:
                    if func.lineno > best_line:
                        best = func
                        best_line = func.lineno
                    break
    return best


def _find_sub_dispatchers(
    callback_node: ast.AST, global_funcs: dict[str, tuple[str, ast.AST]],
) -> list[dict]:
    """查找 callback 函数体内调用的 ``_handle_*`` 子分发器。

    menu_callback 风格:注册的 callback 函数体内调用 ``_handle_report_action`` /
    ``_handle_restore_action`` / ``_handle_delete_file_action`` 等辅助函数,
    每个辅助函数按 ``data.startswith(...)`` 分发不同 action。

    Args:
        callback_node: 注册的 callback 函数节点(可能跨文件)
        global_funcs: 跨模块函数索引 {name → (rel_path, node)}

    Returns:
        [{handler, line, parent_handler, func_node, file}, ...]
    """
    sub_handlers: list[dict] = []
    if callback_node is None:
        return sub_handlers
    seen_names: set[str] = set()
    for node in ast.walk(callback_node):
        if not isinstance(node, ast.Call):
            continue
        name = _get_call_func_name(node)
        if name is None or not name.startswith("_handle_"):
            continue
        if name in seen_names:
            continue
        # 在跨模块索引中查找子分发器定义
        if name not in global_funcs:
            continue
        seen_names.add(name)
        sub_file, sub_node = global_funcs[name]
        sub_handlers.append({
            "handler": name,
            "line": getattr(sub_node, "lineno", node.lineno),
            "parent_handler": getattr(callback_node, "name", None),
            "func_node": sub_node,
            "file": sub_file,
        })
    return sub_handlers


def _extract_sub_pattern(func_node: ast.AST) -> str | None:
    """从子分发器函数体提取首个 ``data.startswith("...")`` 字面量。"""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        if _get_call_func_name(node) != "startswith":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                return node.args[0].value
    return None


# ════════════════════════════════════════════════════════════════
# 主扫描流程
# ════════════════════════════════════════════════════════════════


def _build_handler_entry(
    handler_name: str,
    rel_path: str,
    line: int,
    entry_type: str,
    route_or_pattern: str | None,
    func_node: ast.AST | None,
    parent_handler: str | None = None,
    is_dispatcher: bool = False,
) -> dict:
    """构造单个 handler 的 inventory 条目。

    Args:
        is_dispatcher: True 表示该 handler 是纯分发器(如 menu_callback),
            函数体仅按 data.startswith 分发到 _handle_* 子分发器,不直接执行
            高风险逻辑。分发器本身不计为高风险(实际高风险逻辑在子分发器中)。
    """
    action_type = _classify_action_type(handler_name, route_or_pattern)
    is_high_risk = _is_high_risk_action_type(action_type)

    routes_bus = False
    routes_flow = False
    calls_destructive = False
    destructive_name: str | None = None
    uses_signed_token = False
    names: set[str] = set()

    if func_node is not None:
        names = _scan_body_for_names(func_node)
        routes_bus, routes_flow, calls_destructive, destructive_name = _detect_routes(func_node)
        uses_signed_token = bool(names & SIGNED_TOKEN_API_NAMES)
        # 若函数体调用 make_*_command 但未直接出现 bus.execute,仍视为走 CommandBus
        if not routes_bus and bool(names & COMMAND_BUS_FACTORY_PREFIXES):
            routes_bus = True

    # 分发器(menu_callback 风格):本身不直接执行高风险逻辑,
    # 实际高风险在 _handle_* 子分发器中,因此不计为高风险
    if is_dispatcher:
        is_high_risk = False
        bypass_reason: str | None = None
    else:
        # 高风险判定增强:调用 make_*_command 或破坏性 API 一律视为高风险
        # (即使 action_type 本身不在 HIGH_RISK_ACTION_TYPES 中)
        if not is_high_risk and func_node is not None:
            if bool(names & COMMAND_BUS_FACTORY_PREFIXES) or calls_destructive:
                is_high_risk = True
                if action_type in (
                    "unknown", "navigate", "read", "config",
                    "auth", "break_glass", "report_create",
                ):
                    action_type = _infer_action_type_from_calls(names)

        bypass_reason = None
        if is_high_risk and not routes_bus and not routes_flow:
            if calls_destructive and destructive_name:
                bypass_reason = (
                    f"高风险 handler 直接调用破坏性 API '{destructive_name}',"
                    f"未走 CommandBus/ButtonFlow(Rule A 违规)"
                )
            else:
                bypass_reason = (
                    "高风险 handler 未走 CommandBus/ButtonFlow(Rule A 违规)"
                )

    return {
        "handler": handler_name,
        "file": rel_path,
        "line": line,
        "entry_type": entry_type,
        "route_or_pattern": route_or_pattern,
        "action_type": action_type,
        "is_high_risk": is_high_risk,
        "is_dispatcher": is_dispatcher,
        "routes_through_command_bus": routes_bus,
        "routes_through_button_flow": routes_flow,
        "calls_destructive_api": calls_destructive,
        "destructive_api": destructive_name,
        "uses_signed_token_api": uses_signed_token,
        "parent_handler": parent_handler,
        "bypass_reason": bypass_reason,
    }


def generate_inventory(root: Path | None = None) -> dict:
    """生成 inventory 字典(确定性排序)。

    Args:
        root: 项目根目录(默认 REPO_ROOT,测试时可指向临时目录)

    Returns:
        {"generated_at": ..., "scan_dirs": [...], "handler_count": N,
         "high_risk_count": M, "handlers": [...]}
    """
    global REPO_ROOT
    original_root = REPO_ROOT
    if root is not None:
        REPO_ROOT = root
    try:
        # 第一遍:构建跨模块函数索引(name → (rel_path, node))
        # 用于解析 CallbackQueryHandler 注册的 callback 函数(可能跨文件定义)
        global_funcs: dict[str, tuple[str, ast.AST]] = {}
        file_trees: list[tuple[str, ast.AST]] = []
        for py_file in _iter_scan_files():
            rel_path = _rel_posix(py_file)
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            file_trees.append((rel_path, tree))
            for name, node in _collect_func_defs(tree).items():
                # 同名函数取第一个定义(按文件排序),避免覆盖
                if name not in global_funcs:
                    global_funcs[name] = (rel_path, node)

        # 第二遍:检测 handler 入口
        handlers: list[dict] = []
        for rel_path, tree in file_trees:
            local_funcs = _collect_func_defs(tree)

            # 1. CallbackQueryHandler 注册点
            for ch in _find_callback_query_handlers(tree, local_funcs, global_funcs):
                # 先查子分发器,判断是否为 dispatcher(menu_callback 风格)
                sub_dispatchers: list[dict] = []
                if ch["callback_node"] is not None:
                    sub_dispatchers = _find_sub_dispatchers(
                        ch["callback_node"], global_funcs,
                    )
                is_dispatcher = bool(sub_dispatchers)
                # 注册点本身的条目(标记为 callback_query_handler 入口)
                entry = _build_handler_entry(
                    handler_name=ch["handler"],
                    rel_path=rel_path,
                    line=ch["line"],
                    entry_type="callback_query_handler",
                    route_or_pattern=ch["pattern"],
                    func_node=ch["callback_node"],
                    is_dispatcher=is_dispatcher,
                )
                handlers.append(entry)

                # 子分发器(menu_callback 风格):承载实际高风险分类
                for sub in sub_dispatchers:
                    sub_pattern = _extract_sub_pattern(sub["func_node"])
                    sub_entry = _build_handler_entry(
                        handler_name=sub["handler"],
                        rel_path=sub["file"],
                        line=sub["line"],
                        entry_type="callback_sub_dispatcher",
                        route_or_pattern=sub_pattern,
                        func_node=sub["func_node"],
                        parent_handler=sub["parent_handler"],
                    )
                    handlers.append(sub_entry)

            # 2. FastAPI @app.post / @router.post 端点
            for ep in _find_fastapi_post_endpoints(tree):
                entry = _build_handler_entry(
                    handler_name=ep["handler"],
                    rel_path=rel_path,
                    line=ep["line"],
                    entry_type="fastapi_post",
                    route_or_pattern=ep["route"],
                    func_node=ep["func_node"],
                )
                handlers.append(entry)

            # 3. ButtonFlow / get_button_flow / register_button_handler
            for bf in _find_button_flow_handlers(tree, rel_path):
                entry = _build_handler_entry(
                    handler_name=bf["handler"],
                    rel_path=rel_path,
                    line=bf["line"],
                    entry_type="button_flow",
                    route_or_pattern=None,
                    func_node=bf["func_node"],
                )
                handlers.append(entry)

        # 去重(同一 (file, line, handler, entry_type) 只保留一条)
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for h in handlers:
            key = (h["file"], h["line"], h["handler"], h["entry_type"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(h)

        # 确定性排序:file → line → handler → entry_type
        deduped.sort(key=lambda h: (h["file"], h["line"], h["handler"], h["entry_type"]))

        high_risk_count = sum(1 for h in deduped if h["is_high_risk"])
        return {
            "generated_at": "R61-P1-09-deterministic",
            "scan_dirs": list(SCAN_DIRS),
            "handler_count": len(deduped),
            "high_risk_count": high_risk_count,
            "handlers": deduped,
        }
    finally:
        REPO_ROOT = original_root


def main() -> int:
    """脚本入口。返回退出码(0=成功生成)。"""
    parser = argparse.ArgumentParser(
        description="R61 P1-09: 按钮 handler 清单生成器(全域状态机证明)",
    )
    parser.add_argument(
        "--output", "-o",
        default=str(OUTPUT_FILE),
        help=f"输出 JSON 文件路径(默认: {OUTPUT_FILE})",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="静默模式(不打印摘要)",
    )
    args = parser.parse_args()

    inventory = generate_inventory()
    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not args.quiet:
        print(f"[OK] 按钮 handler 清单已生成: {output_path}")
        print(f"  handler 总数: {inventory['handler_count']}")
        print(f"  高风险 handler: {inventory['high_risk_count']}")
        high_risk = [h for h in inventory["handlers"] if h["is_high_risk"]]
        if high_risk:
            print("  高风险 handler 列表:")
            for h in high_risk:
                bus_tag = "✓CommandBus" if h["routes_through_command_bus"] else ""
                flow_tag = "✓ButtonFlow" if h["routes_through_button_flow"] else ""
                bypass_tag = f" ❌BYPASS:{h['bypass_reason']}" if h["bypass_reason"] else ""
                parent = f" (parent={h['parent_handler']})" if h["parent_handler"] else ""
                print(
                    f"    - {h['file']}:{h['line']} {h['handler']} "
                    f"[{h['action_type']}] {bus_tag}{flow_tag}{bypass_tag}{parent}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
