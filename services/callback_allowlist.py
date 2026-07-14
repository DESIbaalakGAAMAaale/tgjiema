"""R50 P1-2: Callback 动态 action 运行时 allowlist。

R50 终审整改:静态扫描无法检测 action 为变量(非字符串字面量)的调用点。
本模块提供运行时 allowlist,高风险 action 必须在 ALLOWED_HIGH_RISK_ACTIONS 集合内,
其他 action 默认拒绝(防扩散)。

设计:
  - ALLOWED_HIGH_RISK_ACTIONS: 运行时允许的高风险 action 集合(从 button_security.HIGH_RISK_ACTIONS 继承)
  - validate_action_allowed(action): action 必须在 allowlist 内,否则 raise CallbackActionNotAllowedError
  - get_action_risk_level(action): 返回 'high'/'low'/'unknown'
  - register_runtime_action(action, risk_level): 动态注册新 action(运维扩展,需审计日志)
  - AUDIT_LOG_PATH: 动态注册时写入审计日志(可选,默认 stderr)

使用场景:
  - Bot callback handler 在 verify_button_token 后,执行 action 前调用 validate_action_allowed
  - 动态注册新 action 时调用 register_runtime_action(需审计)
"""
from __future__ import annotations

import datetime
import sys
from typing import Optional

from loguru import logger

# R50 P1-1: 统一错误码协议化(替代裸字符串 ValueError)
from services.error_codes import AppError, ErrorCodes

# 从 button_security 导入基础 HIGH_RISK_ACTIONS
from services.button_security import HIGH_RISK_ACTIONS


class CallbackActionNotAllowedError(Exception):
    """R50 P1-2: action 不在 allowlist 内时抛出。"""


# 运行时 allowlist(初始 = HIGH_RISK_ACTIONS,可动态扩展)
_ALLOWED_HIGH_RISK_ACTIONS: set[str] = set(HIGH_RISK_ACTIONS)

# 低风险 action 集合(无需 nonce,允许旧 sync API)
# 这些 action 仅查看/取消/语言选择,无副作用
_ALLOWED_LOW_RISK_ACTIONS: frozenset[str] = frozenset({
    "view", "cancel", "close", "dismiss", "language",
    "lang", "select_lang", "page", "next", "prev",
    "refresh", "info", "help", "back", "menu",
    "noop", "ack", "confirm_view",
})


def validate_action_allowed(action: str) -> None:
    """R50 P1-2: 验证 action 在运行时 allowlist 内。

    高风险 action 必须在 _ALLOWED_HIGH_RISK_ACTIONS 内;
    低风险 action 必须在 _ALLOWED_LOW_RISK_ACTIONS 内;
    其他 action(包括 unknown)默认拒绝(fail-closed)。

    Raises:
        CallbackActionNotAllowedError: action 不在 allowlist 内
    """
    if not action:
        raise AppError(
            ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED,
            params={"action": action or ""},
        )
    if action in _ALLOWED_HIGH_RISK_ACTIONS:
        return
    if action in _ALLOWED_LOW_RISK_ACTIONS:
        return
    raise AppError(
        ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED,
        params={
            "action": action,
            "high_risk_count": len(_ALLOWED_HIGH_RISK_ACTIONS),
            "low_risk_count": len(_ALLOWED_LOW_RISK_ACTIONS),
        },
    )


def get_action_risk_level(action: str) -> str:
    """R50 P1-2: 返回 action 风险等级。

    Returns:
        'high': 高风险(需 nonce + 原子消费)
        'low': 低风险(允许旧 sync API)
        'unknown': 未知(默认拒绝)
    """
    if action in _ALLOWED_HIGH_RISK_ACTIONS:
        return "high"
    if action in _ALLOWED_LOW_RISK_ACTIONS:
        return "low"
    return "unknown"


def register_runtime_action(
    action: str,
    risk_level: str = "high",
    *,
    operator: str = "system",
    reason: str = "",
) -> None:
    """R50 P1-2: 动态注册新 action 到运行时 allowlist。

    用于运维扩展(新增 action),需记录审计日志。

    Args:
        action: action 名称
        risk_level: 'high' 或 'low'
        operator: 操作者(管理员 principal_id 或 'system')
        reason: 注册原因(审计用)

    Raises:
        ValueError: risk_level 非 'high'/'low',或 action 为空
    """
    global _ALLOWED_LOW_RISK_ACTIONS
    if not action:
        raise AppError(
            ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED,
            params={"action": action or ""},
        )
    if risk_level not in ("high", "low"):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"risk_level": risk_level},
        )
    if risk_level == "high":
        if action in _ALLOWED_HIGH_RISK_ACTIONS:
            logger.info(f"[callback_allowlist] action '{action}' 已在高风险 allowlist 内(幂等)")
            return
        _ALLOWED_HIGH_RISK_ACTIONS.add(action)
    else:
        if action in _ALLOWED_LOW_RISK_ACTIONS:
            logger.info(f"[callback_allowlist] action '{action}' 已在低风险 allowlist 内(幂等)")
            return
        # 不修改 frozenset,改为维护一个 mutable 低风险集合
        _ALLOWED_LOW_RISK_ACTIONS = _ALLOWED_LOW_RISK_ACTIONS | {action}
    # 审计日志
    audit_msg = (
        f"[R50 P1-2 AUDIT] register_runtime_action "
        f"action={action} risk_level={risk_level} "
        f"operator={operator} reason={reason or '(none)'} "
        f"timestamp={datetime.datetime.utcnow().isoformat()}"
    )
    logger.info(audit_msg)
    print(audit_msg, file=sys.stderr, flush=True)


def reset_runtime_allowlist() -> None:
    """R50 P1-2: 重置运行时 allowlist 到初始状态(测试用)。

    清除所有动态注册的 action,恢复到 HIGH_RISK_ACTIONS + 默认低风险集合。
    """
    global _ALLOWED_HIGH_RISK_ACTIONS, _ALLOWED_LOW_RISK_ACTIONS
    _ALLOWED_HIGH_RISK_ACTIONS = set(HIGH_RISK_ACTIONS)
    _ALLOWED_LOW_RISK_ACTIONS = frozenset({
        "view", "cancel", "close", "dismiss", "language",
        "lang", "select_lang", "page", "next", "prev",
        "refresh", "info", "help", "back", "menu",
        "noop", "ack", "confirm_view",
    })


def get_allowlist_snapshot() -> dict:
    """R50 P1-2: 返回当前 allowlist 快照(用于审计/诊断)。"""
    return {
        "high_risk": sorted(_ALLOWED_HIGH_RISK_ACTIONS),
        "low_risk": sorted(_ALLOWED_LOW_RISK_ACTIONS),
        "total": len(_ALLOWED_HIGH_RISK_ACTIONS) + len(_ALLOWED_LOW_RISK_ACTIONS),
    }
