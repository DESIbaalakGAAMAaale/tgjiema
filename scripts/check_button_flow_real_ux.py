#!/usr/bin/env python3
"""R65 P1-11: 按钮式流程真实 UX 落地门禁 — ButtonUXSpec 必须在真实 handler 上落地。

审计需求 P1-11:
    按钮式流程要从 policy 表落到真实 UX。ButtonUXSpec 需要在真实
    Telegram/Web handler 上验证:目标、影响、不可逆性、审批状态、MFA 状态、
    resource version、确认、取消、过期恢复、重试和返回按钮均真实渲染;
    不能只检查 sidecar metadata。

本门禁消费 ``scripts/button_handler_inventory.json``(由
``generate_button_handler_inventory.py`` 产出)与 ``services.button_flow_lifecycle``,
对每个 ButtonUXSpec 执行五条规则:

  Rule E(真实 handler 存在): 每个 HIGH_RISK_POLICY action 若在
          ACTION_TYPE_TO_POLICY_ACTIONS 映射中覆盖,必须在 inventory 中存在
          至少一个高风险 handler(fastapi_post / callback_sub_dispatcher /
          callback_query_handler)。policy-only action(purge_data /
          assign_role / rotate_keys 等)无 UI 按钮,跳过 Rule E/F/I。

  Rule F(handler 渲染实际按钮): 高风险 handler 文件必须 AST 扫描到
          InlineKeyboardButton(...) 调用并带 callback_data= 参数(Telegram),
          或 RedirectResponse / HTML form(Web FastAPI)。
          仅依赖 sidecar metadata 不算落地。

  Rule G(5 生命周期路径): 每个 HIGH_RISK_POLICY action × {zh-CN, en-US}
          必须通过 ``verify_all_lifecycle_paths`` 验证全部 5 条路径
          (confirm / cancel / expire / retry / return)。

  Rule H(双语翻译存在): 每个 spec 引用的 i18n key(目标 / 影响 / 不可逆性 /
          审批状态 / 取消 / 确认 / per-action key)必须在 zh-CN.json 与
          en-US.json 中都有非空值。

  Rule I(UserMessage + locale 绑定): 高风险 handler 文件必须 AST 扫描到
          ``UserMessage.from_*`` 调用(typed sink adapter,禁止裸 str)
          且使用 ``_i18n_t(...)`` / ``i18n_manager.translate(...)`` 绑定 locale。

退出码:
  - 0: 无违规
  - 1: 检测到任一规则违规

CI 调用方式:
    python scripts/check_button_flow_real_ux.py --strict

约束:
  - ``--strict`` 为默认(任何违规 exit 1)
  - ``--no-strict`` 允许 ratchet 模式(本门禁目前未启用 baseline,
    保留参数为未来扩展)
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# inventory 文件(由 generate_button_handler_inventory.py 产出)
INVENTORY_FILE = Path(__file__).parent / "button_handler_inventory.json"

# locales 目录(双语 key 校验数据源)
LOCALES_DIR = REPO_ROOT / "locales"

# ════════════════════════════════════════════════════════════════
# 1. 早期注入 fake config(与 tests/conftest.py 的 _install_fake_config 同模式)
# ════════════════════════════════════════════════════════════════
# services.button_security 通过 `from config import settings` 懒加载,
# 真实 config.settings.Settings 强制要求 UPLOAD_BOT_TOKEN/DECODER_BOT_TOKEN/
# COCKROACHDB_URL 等环境变量,在 CI 中无这些环境变量时会抛 ValidationError。
# 本门禁只读取 ButtonFlow/ButtonUXPolicy 的策略表,不依赖真实连接配置,
# 因此注入 MagicMock 绕过环境变量校验(与 conftest.py 一致)。


def _install_fake_config() -> None:
    """注入模拟 config 模块(覆盖可能存在的真实 config)。"""
    if "config" in sys.modules:
        # 已有 config(可能为真实模块或先前注入的 mock)→ 检查 settings 是否可用
        try:
            _ = sys.modules["config"].settings  # type: ignore[attr-defined]
            return
        except AttributeError:
            pass
    settings = MagicMock(name="button_flow_real_ux_settings")
    settings.REDIS_URL = ""
    fake_config = types.ModuleType("config")
    fake_config.settings = settings  # type: ignore[attr-defined]
    sys.modules["config"] = fake_config  # type: ignore[assignment]


_install_fake_config()

# 注入 fake config 后才可安全导入 services.button_flow_lifecycle
sys.path.insert(0, str(REPO_ROOT))
from services.button_ux_policy import (  # noqa: E402
    ButtonUXPolicy,
    KEY_APPROVAL_STATUS,
    KEY_CANCEL,
    KEY_CONFIRM,
    KEY_IMPACT_SCOPE,
    KEY_IRREVERSIBILITY_NOTICE,
    KEY_RECOVERY_CANCEL,
    KEY_RECOVERY_REAPPLY_APPROVAL,
    KEY_RECOVERY_RELOAD,
    KEY_RECOVERY_REPLAY_MFA,
    KEY_RECOVERY_RESUBMIT,
    KEY_RECOVERY_RETRY,
    KEY_TARGET_DISPLAY,
)
from services.button_flow_lifecycle import (  # noqa: E402
    LIFECYCLE_PATHS,
    verify_all_lifecycle_paths,
)
from services.high_risk_policy import HIGH_RISK_POLICY  # noqa: E402


# ════════════════════════════════════════════════════════════════
# 2. 常量:action_type → HIGH_RISK_POLICY action 映射(与 check_button_handler_gate.py 一致)
# ════════════════════════════════════════════════════════════════
# 用于反向查找:HIGH_RISK_POLICY action → inventory 中的 action_type
# (用于 Rule E:每个有 UI 按钮的 HIGH_RISK_POLICY action 必须在 inventory 中有 handler)
ACTION_TYPE_TO_POLICY_ACTIONS: dict[str, tuple[str, ...]] = {
    "ban": ("ban_user", "unban_user"),
    "delete": ("delete_file",),
    "takedown": ("takedown_report",),
    "maintenance": ("enable_maintenance", "disable_maintenance"),
    # report_action 包含 ban/detach/block 子动作
    "report_action": ("ban_user", "detach_file", "block_user_for_file"),
    "restore": ("restore_backup", "restore_content"),
}

# 反向映射:HIGH_RISK_POLICY action → inventory action_type
# (一个 action 可能对应多个 action_type,如 ban_user → ban + report_action)
_POLICY_ACTION_TO_TYPES: dict[str, list[str]] = {}
for _action_type, _actions in ACTION_TYPE_TO_POLICY_ACTIONS.items():
    for _action in _actions:
        _POLICY_ACTION_TO_TYPES.setdefault(_action, []).append(_action_type)

# 高风险 handler 的 entry_type 集合(Rule E/F/I 检查范围)
HIGH_RISK_ENTRY_TYPES: frozenset[str] = frozenset({
    "fastapi_post",
    "callback_query_handler",
    "callback_sub_dispatcher",
})

# 受支持 locale(Rule H 双语校验范围)
SUPPORTED_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")

# Rule F:Telegram 渲染按钮的 AST 函数名(任一匹配即通过)
TELEGRAM_BUTTON_RENDER_FUNCS: frozenset[str] = frozenset({
    "InlineKeyboardButton",
})

# Rule F:Web 渲染按钮/表单的 AST 函数名(任一匹配即通过)
WEB_BUTTON_RENDER_FUNCS: frozenset[str] = frozenset({
    "RedirectResponse",
    "HTMLResponse",
})

# Rule I:UserMessage 工厂方法(任一匹配即通过)
USER_MESSAGE_FACTORIES: frozenset[str] = frozenset({
    "UserMessage.from_key",
    "UserMessage.from_error",
    "UserMessage.from_raw_text",
})

# Rule I:locale 绑定调用(任一匹配即通过)
LOCALE_BINDING_FUNCS: frozenset[str] = frozenset({
    "_i18n_t",
    "i18n_manager.translate",
    "translate",
})


# ════════════════════════════════════════════════════════════════
# 3. FakeI18nManager — 读取真实 locale 文件解析嵌套 key
# ════════════════════════════════════════════════════════════════
# 复用 tests/test_r64_p1_8_button_ux_completeness.py 的 FakeI18nManager 模式,
# 读取真实 locales/{locale}.json 而非 mock 翻译(避免 locale 文件缺失被掩盖)。


class FakeI18nManager:
    """轻量 i18n 管理器,读取真实 locales/{locale}.json 解析 dotted key。"""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        for locale_file in LOCALES_DIR.glob("*.json"):
            locale = locale_file.stem
            self._data[locale] = json.loads(locale_file.read_text(encoding="utf-8"))

    def translate(self, key: str, locale: Optional[str] = None, **kwargs: Any) -> str:
        """按 dotted key 路径查找翻译值(与 services.i18n.translate 一致)。"""
        loc = locale or "zh-CN"
        tree = self._data.get(loc, {})
        parts = key.split(".")
        node: Any = tree
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return key  # key 不存在,返回 key 本身(与真实 I18nManager fallback 一致)
        return str(node) if not isinstance(node, dict) else key

    def has_key(self, key: str, locale: str) -> bool:
        """检查 key 在指定 locale 中是否存在(且不为 dict)。"""
        return self.translate(key, locale=locale) != key


# ════════════════════════════════════════════════════════════════
# 4. 文件加载
# ════════════════════════════════════════════════════════════════


def _load_inventory(inventory_path: Path | None = None) -> dict:
    """加载 inventory JSON。

    Returns:
        {"handlers": [...], "handler_count": N, "high_risk_count": M, ...}
    Raises:
        FileNotFoundError: inventory 文件不存在
        ValueError: inventory 格式错误
    """
    path = inventory_path or INVENTORY_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"inventory 文件不存在: {path}\n"
            f"请先运行: python scripts/generate_button_handler_inventory.py"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if "handlers" not in data or not isinstance(data["handlers"], list):
        raise ValueError(f"inventory 格式错误: 缺少 handlers 列表 ({path})")
    return data


# ════════════════════════════════════════════════════════════════
# 5. Rule E: 真实 handler 存在
# ════════════════════════════════════════════════════════════════


def _find_handlers_for_action(action: str, inventory: dict) -> list[dict]:
    """查找 inventory 中覆盖指定 HIGH_RISK_POLICY action 的高风险 handler 列表。

    Args:
        action: HIGH_RISK_POLICY 中的 action 名(如 "delete_file")
        inventory: inventory 字典

    Returns:
        高风险 handler 列表(每个 handler 含 file/handler/entry_type/...)
    """
    action_types = _POLICY_ACTION_TO_TYPES.get(action, [])
    if not action_types:
        return []  # policy-only action,无对应 UI handler

    handlers: list[dict] = []
    for h in inventory.get("handlers", []):
        if not h.get("is_high_risk"):
            continue
        if h.get("is_dispatcher"):
            continue
        if h.get("entry_type") not in HIGH_RISK_ENTRY_TYPES:
            continue
        if h.get("action_type") in action_types:
            handlers.append(h)
    return handlers


def _check_rule_e(action: str, inventory: dict) -> list[dict]:
    """Rule E: HIGH_RISK_POLICY action 在 ACTION_TYPE_TO_POLICY_ACTIONS 覆盖范围内时,
    必须在 inventory 中存在至少一个高风险 handler。

    policy-only action(无 UI 按钮,如 purge_data/assign_role/rotate_keys)
    跳过本规则(返回空违规列表)。

    Returns:
        违规列表(每项含 rule/violation_type/action/reason)
    """
    violations: list[dict] = []
    action_types = _POLICY_ACTION_TO_TYPES.get(action, [])
    if not action_types:
        # policy-only action,不在 ACTION_TYPE_TO_POLICY_ACTIONS 覆盖范围内
        # (如 purge_data/assign_role/rotate_keys)→ 无 UI 按钮,跳过 Rule E
        return violations

    handlers = _find_handlers_for_action(action, inventory)
    if not handlers:
        violations.append({
            "rule": "E",
            "violation_type": "RULE_E_NO_REAL_HANDLER",
            "action": action,
            "action_types": action_types,
            "reason": (
                f"HIGH_RISK_POLICY action '{action}' 映射到 action_type {action_types},"
                f"但 inventory 中无对应高风险 handler。"
                f"P1-11 要求每个 ButtonUXSpec 在真实 Telegram/Web handler 上落地,"
                f"不能只停留在 sidecar metadata。"
                f"请补全对应 handler(fastapi_post / callback_sub_dispatcher / "
                f"callback_query_handler),或在 ACTION_TYPE_TO_POLICY_ACTIONS 中"
                f"取消该 action 的映射(标记为 policy-only)。"
            ),
        })
    return violations


# ════════════════════════════════════════════════════════════════
# 6. Rule F: handler 渲染实际按钮(AST 扫描)
# ════════════════════════════════════════════════════════════════


def _ast_find_call_names(node: ast.AST, target_names: frozenset[str]) -> list[str]:
    """AST 扫描:返回 node 中调用函数名(支持 Name 与 Attribute 形式)
    与 target_names 匹配的所有调用名。

    支持形式:
      - InlineKeyboardButton(...)      → ast.Name
      - telegram.InlineKeyboardButton(...) → ast.Attribute
    """
    matched: list[str] = []

    def _get_call_name(call: ast.Call) -> Optional[str]:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            # 返回 "module.attr" 或 "attr"(便于匹配 UserMessage.from_key)
            if isinstance(func.value, ast.Name):
                return f"{func.value.id}.{func.attr}"
            return func.attr
        return None

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _get_call_name(child)
            if name and (name in target_names):
                matched.append(name)
    return matched


def _ast_call_has_kwarg(node: ast.AST, func_names: frozenset[str], kwarg_name: str) -> bool:
    """AST 扫描:检查 node 中是否存在对 func_names 中任一函数的调用,
    且该调用包含名为 kwarg_name 的关键字参数。

    用于检查 InlineKeyboardButton(... callback_data=...) 是否带 callback_data。
    """
    def _get_call_name(call: ast.Call) -> Optional[str]:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _get_call_name(child)
            if name and name in func_names:
                for kw in child.keywords:
                    if kw.arg == kwarg_name:
                        return True
    return False


def _check_rule_f(action: str, inventory: dict) -> list[dict]:
    """Rule F: 高风险 handler 文件必须 AST 扫描到实际按钮渲染。

    - Telegram handler(callback_sub_dispatcher / callback_query_handler):
      文件中必须存在 ``InlineKeyboardButton(...)`` 调用并带 ``callback_data=`` 参数
    - Web handler(fastapi_post):
      文件中必须存在 ``RedirectResponse(...)`` / ``HTMLResponse(...)`` 调用
      (Web FastAPI 通过 form POST + CSRF + RedirectResponse 实现"按钮",
      无 inline keyboard)

    policy-only action(无 UI handler)跳过本规则。

    Returns:
        违规列表
    """
    violations: list[dict] = []
    handlers = _find_handlers_for_action(action, inventory)
    if not handlers:
        return violations  # policy-only action,跳过

    # 按 file 去重(同文件多个 handler 共享一次 AST 扫描)
    files_seen: set[str] = set()
    for h in handlers:
        rel_path = h.get("file", "")
        if not rel_path or rel_path in files_seen:
            continue
        files_seen.add(rel_path)
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            violations.append({
                "rule": "F",
                "violation_type": "RULE_F_HANDLER_FILE_MISSING",
                "action": action,
                "file": rel_path,
                "reason": f"高风险 handler 文件不存在: {rel_path}",
            })
            continue
        try:
            source = abs_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(abs_path))
        except (SyntaxError, UnicodeDecodeError) as e:
            violations.append({
                "rule": "F",
                "violation_type": "RULE_F_AST_PARSE_FAILED",
                "action": action,
                "file": rel_path,
                "reason": f"AST 解析失败: {e}",
            })
            continue

        entry_type = h.get("entry_type", "")
        if entry_type in ("callback_sub_dispatcher", "callback_query_handler"):
            # Telegram handler: 要求 InlineKeyboardButton 调用 + callback_data 关键字
            #
            # 实际架构:按钮生命周期跨两个文件 —
            #   1. handlers.py(command 入口)渲染初始按钮 InlineKeyboardButton(... callback_data=...)
            #   2. callback.py(子分发器,inventory 捕获此文件)处理点击 → 验签 → CommandBus
            # 子分发器本身可能只使用预构建的 BACK_BTN 常量(menus.py 定义),
            # 不直接构造 InlineKeyboardButton。因此 Rule F 必须扫描该文件所在目录
            # 的所有 sibling .py 文件,任一 sibling 含 InlineKeyboardButton(... callback_data=...)
            # 即视为按钮真实渲染(避免误判)。
            has_button = bool(
                _ast_find_call_names(tree, TELEGRAM_BUTTON_RENDER_FUNCS)
            )
            has_callback_data = _ast_call_has_kwarg(
                tree, TELEGRAM_BUTTON_RENDER_FUNCS, "callback_data",
            )
            # 若本文件未通过,扫描同目录 sibling .py(handlers.py / menus.py 等)
            if not has_button or not has_callback_data:
                for sibling in sorted(abs_path.parent.glob("*.py")):
                    if sibling == abs_path:
                        continue  # 跳过自身(已扫描)
                    try:
                        sib_source = sibling.read_text(encoding="utf-8")
                        sib_tree = ast.parse(sib_source, filename=str(sibling))
                    except (SyntaxError, UnicodeDecodeError):
                        continue
                    if not has_button and _ast_find_call_names(
                        sib_tree, TELEGRAM_BUTTON_RENDER_FUNCS,
                    ):
                        has_button = True
                    if not has_callback_data and _ast_call_has_kwarg(
                        sib_tree, TELEGRAM_BUTTON_RENDER_FUNCS, "callback_data",
                    ):
                        has_callback_data = True
                    if has_button and has_callback_data:
                        break
            if not has_button or not has_callback_data:
                violations.append({
                    "rule": "F",
                    "violation_type": "RULE_F_NO_REAL_BUTTON_RENDERED",
                    "action": action,
                    "file": rel_path,
                    "handler": h.get("handler", ""),
                    "entry_type": entry_type,
                    "reason": (
                        f"Telegram handler 模块(含 sibling .py)未渲染实际按钮"
                        f"(AST 未扫描到 InlineKeyboardButton(... callback_data=...) 调用)。"
                        f"P1-11 要求按钮 UX 必须真实渲染,不能只检查 sidecar metadata。"
                        f"has_button={has_button}, has_callback_data={has_callback_data}。"
                    ),
                })
        elif entry_type == "fastapi_post":
            # Web handler:要求 RedirectResponse / HTMLResponse 调用
            has_response = bool(_ast_find_call_names(tree, WEB_BUTTON_RENDER_FUNCS))
            if not has_response:
                violations.append({
                    "rule": "F",
                    "violation_type": "RULE_F_NO_REAL_BUTTON_RENDERED",
                    "action": action,
                    "file": rel_path,
                    "handler": h.get("handler", ""),
                    "entry_type": entry_type,
                    "reason": (
                        f"Web FastAPI handler 文件未渲染实际响应(AST 未扫描到 "
                        f"RedirectResponse / HTMLResponse 调用)。"
                        f"Web 通过 form POST + CSRF + RedirectResponse 实现'按钮',"
                        f"必须存在至少一个响应调用。"
                    ),
                })
    return violations


# ════════════════════════════════════════════════════════════════
# 7. Rule I: handler 使用 UserMessage + locale 绑定
# ════════════════════════════════════════════════════════════════


def _check_rule_i(action: str, inventory: dict) -> list[dict]:
    """Rule I: 高风险 handler 文件必须 AST 扫描到 UserMessage + locale 绑定。

    - Telegram handler(callback_sub_dispatcher / callback_query_handler):
      必须存在 ``UserMessage.from_key`` / ``from_error`` / ``from_raw_text`` 调用
      (typed sink adapter,禁止裸 str)
      + ``_i18n_t(...)`` / ``i18n_manager.translate(...)`` / ``translate(...)``
      调用(locale 绑定,禁止使用全局默认 locale)
    - Web handler(fastapi_post):
      必须存在 locale 绑定调用(``_i18n_t(...)`` / ``i18n_manager.translate(...)``)。
      Web 通过 HTML 模板 + flash message + RedirectResponse 实现用户面消息,
      UserMessage(typed sink adapter for chat)在 Web 不直接适用 —
      Web 用 i18n key 在模板侧渲染,因此只校验 locale 绑定。

    policy-only action(无 UI handler)跳过本规则。

    Returns:
        违规列表
    """
    violations: list[dict] = []
    handlers = _find_handlers_for_action(action, inventory)
    if not handlers:
        return violations

    # 按 file 去重
    files_seen: set[str] = set()
    for h in handlers:
        rel_path = h.get("file", "")
        if not rel_path or rel_path in files_seen:
            continue
        files_seen.add(rel_path)
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            # Rule F 已检测文件缺失,此处不重复报错
            continue
        try:
            source = abs_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(abs_path))
        except (SyntaxError, UnicodeDecodeError):
            continue  # Rule F 已报错

        entry_type = h.get("entry_type", "")
        # locale 绑定调用检测(所有 handler 类型都要求)
        locale_calls = _ast_find_call_names(tree, LOCALE_BINDING_FUNCS)
        has_locale_binding = len(locale_calls) > 0

        # UserMessage.from_* 调用检测(仅 Telegram handler 要求)
        # Web FastAPI 通过 HTML 模板渲染,UserMessage 不直接适用
        requires_user_message = entry_type in (
            "callback_sub_dispatcher", "callback_query_handler",
        )
        if requires_user_message:
            um_calls = _ast_find_call_names(tree, USER_MESSAGE_FACTORIES)
            has_user_message = len(um_calls) > 0
        else:
            um_calls = []
            has_user_message = True  # Web handler 不强制 UserMessage

        if not has_user_message or not has_locale_binding:
            violations.append({
                "rule": "I",
                "violation_type": "RULE_I_NO_USER_MESSAGE_OR_LOCALE",
                "action": action,
                "file": rel_path,
                "handler": h.get("handler", ""),
                "entry_type": entry_type,
                "has_user_message": has_user_message,
                "has_locale_binding": has_locale_binding,
                "user_message_calls": um_calls,
                "locale_binding_calls": locale_calls,
                "requires_user_message": requires_user_message,
                "reason": (
                    f"高风险 handler 文件未满足 UserMessage + locale 绑定要求。"
                    f"P1-11 要求 handler 使用 typed sink adapter (UserMessage.from_*) "
                    f"并显式绑定 locale(_i18n_t / i18n_manager.translate),"
                    f"禁止裸 str 与全局默认 locale。"
                    f"entry_type={entry_type}, "
                    f"requires_user_message={requires_user_message}, "
                    f"has_user_message={has_user_message}, "
                    f"has_locale_binding={has_locale_binding}。"
                ),
            })
    return violations


# ════════════════════════════════════════════════════════════════
# 8. Rule G: 5 生命周期路径(asyncio)
# ════════════════════════════════════════════════════════════════


async def _check_rule_g_h_async(
    action: str,
    locale: str,
    i18n: FakeI18nManager,
    policy: ButtonUXPolicy,
) -> tuple[list[dict], list[dict]]:
    """Rule G + Rule H 联合检查(异步,因为 lifecycle 模拟器是 async)。

    Returns:
        (g_violations, h_violations)
    """
    g_violations: list[dict] = []
    h_violations: list[dict] = []

    # 构造 ButtonUXSpec
    try:
        spec = policy.destructive_confirmation(action, "test_target", locale)
    except Exception as e:
        g_violations.append({
            "rule": "G",
            "violation_type": "RULE_G_SPEC_CONSTRUCTION_FAILED",
            "action": action,
            "locale": locale,
            "reason": f"ButtonUXSpec 构造失败: {e}",
        })
        return g_violations, h_violations

    # Rule G: 5 生命周期路径
    try:
        verification = await verify_all_lifecycle_paths(
            spec, i18n_manager=i18n,
        )
    except Exception as e:
        g_violations.append({
            "rule": "G",
            "violation_type": "RULE_G_LIFECYCLE_SIMULATION_FAILED",
            "action": action,
            "locale": locale,
            "reason": f"verify_all_lifecycle_paths 抛异常: {e}",
        })
        return g_violations, h_violations

    if not verification.all_passed:
        g_violations.append({
            "rule": "G",
            "violation_type": "RULE_G_LIFECYCLE_PATH_FAILED",
            "action": action,
            "locale": locale,
            "failed_paths": list(verification.failed_paths),
            "reason": (
                f"5 生命周期路径验证失败,失败路径: {verification.failed_paths}。"
                f"P1-11 要求每个 ButtonUXSpec 实现 confirm/cancel/expire/retry/return "
                f"全部 5 条路径(无死路)。"
            ),
        })

    # Rule H: 双语翻译存在
    # 检查 spec 实际引用的所有 i18n key 在两个 locale 中都有非空值。
    # 注意:per-action key 仅在 action 命中 _ACTION_IMPACT_SCOPE_KEY 等映射时使用,
    # 否则 ButtonUXPolicy 会回退到通用 key(button.ux.destructive.impact_scope 等)。
    # 因此直接读取 spec 字段中实际声明的 key,而非假设 per-action key 一定存在。
    spec_keys: list[str] = [
        # 目标展示 key(spec.target_display 格式为 "{KEY_TARGET_DISPLAY}|{target}")
        KEY_TARGET_DISPLAY,
        # 通用 destructive key(始终检查)
        KEY_CANCEL,
        KEY_CONFIRM,
        # 错误恢复 key(必填,所有恢复按钮都需要)
        KEY_RECOVERY_RETRY,
        KEY_RECOVERY_CANCEL,
        KEY_RECOVERY_RESUBMIT,
        KEY_RECOVERY_RELOAD,
        KEY_RECOVERY_REAPPLY_APPROVAL,
        KEY_RECOVERY_REPLAY_MFA,
        # mfa badge key(spec.requires_mfa_badge=True 时使用)
        "button.ux.destructive.mfa_required_badge",
    ]
    # 从 spec 字段读取实际使用的 per-action key(由 ButtonUXPolicy 决定,
    # 命中映射时为 per-action key,否则回退到通用 key)
    spec_keys.append(spec.impact_scope)
    spec_keys.append(spec.irreversibility_notice)
    spec_keys.append(spec.approval_status_display)

    # 检查所有 key 在当前 locale 中都有值
    # (按 locale 检查当前 spec — 若 zh-CN 缺失,在 locale=zh-CN 的检查中会捕获;
    #  en-US 同理。两次循环覆盖两个 locale)
    for key in spec_keys:
        if not i18n.has_key(key, locale):
            h_violations.append({
                "rule": "H",
                "violation_type": "RULE_H_LOCALE_KEY_MISSING",
                "action": action,
                "locale": locale,
                "key": key,
                "reason": (
                    f"i18n key '{key}' 在 locale='{locale}' 中缺失(或值为空)。"
                    f"P1-11 要求所有用户面字符串必须有双语翻译(zh-CN + en-US),"
                    f"禁止仅单语或硬编码文案。"
                ),
            })

    return g_violations, h_violations


# ════════════════════════════════════════════════════════════════
# 9. 主检查流程
# ════════════════════════════════════════════════════════════════


def check(
    inventory: dict | None = None,
    strict: bool = True,
) -> tuple[int, list[dict]]:
    """主校验流程。

    Args:
        inventory: inventory 字典(默认从 INVENTORY_FILE 加载)
        strict: True=严格模式(任何违规 exit 1,本门禁默认 strict)

    Returns:
        (exit_code, all_violations)
        exit_code: 0=无违规,1=有违规
        all_violations: 所有违规列表(按 rule/action/file 分组)
    """
    if inventory is None:
        inventory = _load_inventory()

    all_violations: list[dict] = []

    # 初始化 i18n / policy
    i18n = FakeI18nManager()
    policy = ButtonUXPolicy()

    # Rule E / F / I:对每个 HIGH_RISK_POLICY action 检查真实 handler 落地情况
    for action in HIGH_RISK_POLICY:
        all_violations.extend(_check_rule_e(action, inventory))
        all_violations.extend(_check_rule_f(action, inventory))
        all_violations.extend(_check_rule_i(action, inventory))

    # Rule G / H:对每个 action × locale 异步检查 5 生命周期路径 + 双语翻译
    async def _run_all_g_h() -> list[dict]:
        violations: list[dict] = []
        for action in HIGH_RISK_POLICY:
            for locale in SUPPORTED_LOCALES:
                g_v, h_v = await _check_rule_g_h_async(action, locale, i18n, policy)
                violations.extend(g_v)
                violations.extend(h_v)
        return violations

    g_h_violations = asyncio.run(_run_all_g_h())
    all_violations.extend(g_h_violations)

    exit_code = 1 if all_violations else 0
    # strict 模式默认启用(本门禁无 baseline 机制,strict 仅作显式标记)
    return exit_code, all_violations


# ════════════════════════════════════════════════════════════════
# 10. 报告输出
# ════════════════════════════════════════════════════════════════


def _print_violations(violations: list[dict]) -> None:
    """打印违规列表(按 rule 分组)。"""
    if not violations:
        return
    by_rule: dict[str, list[dict]] = {}
    for v in violations:
        by_rule.setdefault(v["rule"], []).append(v)
    for rule in sorted(by_rule.keys()):
        items = by_rule[rule]
        print(f"\n[Rule {rule}] {len(items)} 处违规:")
        for v in items:
            action = v.get("action", "?")
            locale = v.get("locale", "")
            file = v.get("file", "")
            handler = v.get("handler", "")
            loc_str = f" locale={locale}" if locale else ""
            file_str = f" file={file}" if file else ""
            handler_str = f" handler={handler}" if handler else ""
            print(
                f"  action={action}{loc_str}{file_str}{handler_str}"
                f" [{v.get('violation_type', '?')}]"
            )
            print(f"    原因: {v.get('reason', '')}")
            if "failed_paths" in v:
                print(f"    失败路径: {v['failed_paths']}")
            if "key" in v:
                print(f"    key: {v['key']}")


def _print_summary(inventory: dict, violations: list[dict]) -> None:
    """打印统计信息(总 action 数 / 覆盖数 / policy-only 数 / 违规数)。"""
    handlers = inventory.get("handlers", [])
    high_risk_handlers = [h for h in handlers if h.get("is_high_risk")]
    print(f"[INFO] HIGH_RISK_POLICY action 总数: {len(HIGH_RISK_POLICY)}")
    print(f"[INFO] inventory 高风险 handler 数: {len(high_risk_handlers)}")

    # 统计有 UI handler 的 action 与 policy-only action
    actions_with_ui: list[str] = []
    policy_only_actions: list[str] = []
    for action in HIGH_RISK_POLICY:
        if _POLICY_ACTION_TO_TYPES.get(action):
            actions_with_ui.append(action)
        else:
            policy_only_actions.append(action)
    print(f"[INFO] 有 UI handler 的 action({len(actions_with_ui)}): {actions_with_ui}")
    print(f"[INFO] policy-only action({len(policy_only_actions)}): {policy_only_actions}")

    # 按 rule 统计违规
    by_rule: dict[str, int] = {}
    for v in violations:
        by_rule[v["rule"]] = by_rule.get(v["rule"], 0) + 1
    print(f"\n[INFO] 违规统计(按规则):")
    print(f"  Rule E (真实 handler 存在):     {by_rule.get('E', 0)}")
    print(f"  Rule F (渲染实际按钮):          {by_rule.get('F', 0)}")
    print(f"  Rule G (5 生命周期路径):        {by_rule.get('G', 0)}")
    print(f"  Rule H (双语翻译存在):          {by_rule.get('H', 0)}")
    print(f"  Rule I (UserMessage + locale):  {by_rule.get('I', 0)}")


# ════════════════════════════════════════════════════════════════
# 11. CLI 入口
# ════════════════════════════════════════════════════════════════


def main() -> int:
    """脚本入口。返回退出码。"""
    parser = argparse.ArgumentParser(
        description=(
            "R65 P1-11: 按钮式流程真实 UX 落地门禁 "
            "(ButtonUXSpec 必须在真实 handler 上落地)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="严格模式(默认):任何违规 exit 1。本门禁默认即 strict。",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="非严格模式(预留,当前等价于 --strict)。",
    )
    parser.add_argument(
        "--inventory",
        default=str(INVENTORY_FILE),
        help=f"inventory JSON 路径(默认: {INVENTORY_FILE})",
    )
    args = parser.parse_args()

    # 加载 inventory
    try:
        inventory = _load_inventory(Path(args.inventory))
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}")
        return 1

    exit_code, violations = check(
        inventory=inventory,
        strict=args.strict,
    )

    # 输出统计
    _print_summary(inventory, violations)

    if violations:
        _print_violations(violations)
        print()
        print("整改方案:")
        print("  Rule E: 为 HIGH_RISK_POLICY action 补全真实 handler")
        print("          (fastapi_post / callback_sub_dispatcher / callback_query_handler),")
        print("          或在 ACTION_TYPE_TO_POLICY_ACTIONS 中取消该 action 的映射")
        print("          (标记为 policy-only,无 UI 按钮)。")
        print("  Rule F: 高风险 handler 文件必须渲染实际按钮:")
        print("          Telegram: InlineKeyboardButton(... callback_data=...)")
        print("          Web: RedirectResponse / HTMLResponse")
        print("          不能只依赖 sidecar metadata(button_handler_metadata.json)。")
        print("  Rule G: 5 生命周期路径(confirm/cancel/expire/retry/return)必须全部通过,")
        print("          通过 services.button_flow_lifecycle.verify_all_lifecycle_paths 验证。")
        print("  Rule H: 所有用户面字符串必须有双语翻译(zh-CN + en-US),")
        print("          在 locales/zh-CN.json 与 locales/en-US.json 中补全 key。")
        print("  Rule I: handler 必须使用 UserMessage.from_key/from_error/from_raw_text")
        print("          (typed sink adapter) + _i18n_t / i18n_manager.translate (locale 绑定),")
        print("          禁止裸 str 与全局默认 locale。")
        print()
        print(f"基线模式: 本门禁默认 strict(任何违规 exit 1),无 baseline ratchet。")
        return 1

    print()
    print(
        "[OK] R65 P1-11 门禁通过: 所有 ButtonUXSpec 在真实 handler 上落地,"
        "5 生命周期路径全部实现,双语翻译齐全,UserMessage + locale 绑定正确。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
