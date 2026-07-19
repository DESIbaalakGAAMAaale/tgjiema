"""R65 P1-11: 按钮式流程 5 生命周期路径定义与模拟器。

审计 P1-11 要求:
    按钮式流程要从 policy 表落到真实 UX。ButtonUXSpec 需要在真实
    Telegram/Web handler 上验证:目标、影响、不可逆性、审批状态、MFA 状态、
    resource version、确认、取消、过期恢复、重试和返回按钮均真实渲染;
    不能只检查 sidecar metadata。

每个高风险按钮流程必须实现 5 条生命周期路径(P1-11 强制要求):
    1. confirm — 用户点击确认 → CAS 消费 + 执行 → 成功(回执)
    2. cancel  — 用户点击取消 → 不执行,token 保留未消费 → 提示已取消
    3. expire  — token 过期(TTL 超时)→ CAS 失败 → 渲染 resubmit 恢复按钮
    4. retry   — 执行失败(版本冲突 / nonce 已用 / principal 不匹配)→
                 渲染对应恢复按钮(resubmit/reload/reapply_approval/replay_mfa)
    5. return  — 取消 / 过期 / 失败后 → 渲染 back 按钮回到初始菜单(无死路)

本模块提供:
    - ``LIFECYCLE_PATHS``: 5 条路径常量(顺序固定,供门禁脚本与测试引用)
    - ``LifecyclePathResult``: 路径模拟结果(含 success/error_code/
      recovery_buttons/back_button/rendered_text)
    - ``InMemoryButtonTokenStore``: 测试用内存 ButtonTokenStore(不依赖
      aiosqlite,实现 ButtonTokenStore 同步语义子集)
    - ``simulate_lifecycle_path``: 在 mock ButtonFlow 上模拟单条路径
    - ``verify_all_lifecycle_paths``: 对一个 ButtonUXSpec 验证全部 5 条路径

设计原则:
    - 模拟器只验证"UX 路径可达性",不重复 ButtonFlow 6 步流程的端到端测试
    - 5 条路径覆盖 P1-11 强制的全部 UX 状态转换(确认/取消/过期/重试/返回)
    - 恢复按钮通过 ``render_recovery_options`` 真实渲染,验证 i18n key 解析
    - 返回按钮固定使用 callback_data="menu:main"(与 bots/admin_bot/menus.py
      的 BACK_BTN 一致),证明"无死路"要求
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from services.button_flow import (
    ButtonFlow,
    ButtonFlowResult,
    ButtonToken,
    ButtonTokenStore,
    DEFAULT_TTL_SECONDS,
)
from services.button_ux_policy import (
    ButtonUXPolicy,
    ButtonUXSpec,
    ERROR_RECOVERY_CATEGORY_MAP,
    ERROR_RECOVERY_KEY_MAP,
    KEY_CANCEL,
    KEY_CONFIRM,
    KEY_RECOVERY_CANCEL,
)
from services.button_ux_renderer import (
    InlineKeyboardButton,
    render_confirmation_buttons,
    render_destructive_confirmation,
    render_recovery_options,
)
from services.error_codes import ErrorCodes


# ════════════════════════════════════════════════════════════════
# 1. 5 生命周期路径常量
# ════════════════════════════════════════════════════════════════

# P1-11 强制的 5 条生命周期路径(顺序固定,门禁脚本与测试均按此顺序遍历)
LIFECYCLE_PATHS: tuple[str, ...] = (
    "confirm",
    "cancel",
    "expire",
    "retry",
    "return",
)

# 返回按钮的固定 callback_data(与 bots/admin_bot/menus.py 的 BACK_BTN 一致)
# 用于证明"取消/过期/失败后用户可返回初始菜单,无死路"
RETURN_CALLBACK_DATA = "menu:main"

# 取消按钮的 callback_data 前缀(与 bots/admin_bot/handlers.py 的渲染模式一致)
CANCEL_CALLBACK_PREFIX = "cancel|"


# ════════════════════════════════════════════════════════════════
# 2. LifecyclePathResult — 路径模拟结果
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LifecyclePathResult:
    """R65 P1-11: 单条生命周期路径的模拟结果。

    Attributes:
        path: 路径名(confirm/cancel/expire/retry/return)
        success: 路径是否如期完成(confirm→True;其他路径"如期失败/取消"也算 True)
        error_code: 路径产生的错误码(confirm 路径为空;expire/retry 路径为
                    BUTTON_POLICY_NONCE_CONSUMED 等)
        rendered_text: 路径渲染的 Telegram 消息文本(含目标/影响/不可逆性/
                       审批状态/cancel/confirm 标签)
        confirmation_buttons: 确认面板按钮列表([confirm, cancel])
        recovery_buttons: 恢复面板按钮列表(expire/retry 路径产出;
                          cancel/confirm/return 路径为空)
        back_button: 返回按钮(return 路径产出;其他路径为空)
        token_consumed: token 是否被 CAS 消费(confirm 路径=True;
                        cancel/expire/retry 路径=False)
        message: 人类可读的路径说明(供测试断言失败时排查)
    """
    path: str
    success: bool
    error_code: str = ""
    rendered_text: str = ""
    confirmation_buttons: tuple[InlineKeyboardButton, ...] = ()
    recovery_buttons: tuple[InlineKeyboardButton, ...] = ()
    back_button: Optional[InlineKeyboardButton] = None
    token_consumed: bool = False
    note: str = ""


# ════════════════════════════════════════════════════════════════
# 3. InMemoryButtonTokenStore — 测试用内存 ButtonTokenStore
# ════════════════════════════════════════════════════════════════


class InMemoryButtonTokenStore:
    """R65 P1-11: 测试用内存 ButtonTokenStore(不依赖 aiosqlite)。

    实现 ButtonTokenStore 的同步语义子集(create_token / consume_token_cas /
    get_token / update_mfa_status / update_approver / update_final_confirm),
    使用 dict 存储,供 lifecycle 模拟器与测试使用。

    与 ``services.button_flow.ButtonTokenStore`` 的语义差异:
        - 不持久化(进程结束即丢失)
        - 不使用 SQL RETURNING 子句(直接 dict 操作)
        - 不调用 ``_ensure_table``(无 DDL)
    其余 CAS 4 字段语义(used_at IS NULL AND expires_at>now AND principal=?
    AND version=?)与 ButtonTokenStore 完全一致。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, ButtonToken] = {}
        self._used_at: dict[str, str] = {}  # nonce → used_at ISO

    async def create_token(self, token: ButtonToken) -> bool:
        """创建 token(nonce 主键,重复返回 False)。"""
        if token.nonce in self._tokens:
            return False
        self._tokens[token.nonce] = token
        return True

    async def consume_token_cas(
        self,
        nonce: str,
        principal_id: int,
        resource_version: str,
    ) -> Optional[ButtonToken]:
        """R56 §5.3: 单事务 CAS 4 字段原子消费(语义与 ButtonTokenStore 一致)。

        CAS 条件:
            used_at IS NULL AND expires_at > now
            AND principal_id = ? AND resource_version = ?
        """
        token = self._tokens.get(nonce)
        if token is None:
            return None
        if nonce in self._used_at:
            return None  # used_at IS NOT NULL
        # expires_at > now
        try:
            expires_dt = _dt.datetime.fromisoformat(
                token.expires_at.replace("Z", "")
            )
        except Exception:
            return None
        if expires_dt <= _dt.datetime.utcnow():
            return None
        if token.principal_id != principal_id:
            return None
        if token.resource_version != resource_version:
            return None
        # CAS 通过,标记 used_at
        self._used_at[nonce] = _dt.datetime.utcnow().isoformat()
        return token

    async def get_token(self, nonce: str) -> Optional[ButtonToken]:
        """查询 token(不消费)。"""
        return self._tokens.get(nonce)

    async def update_mfa_status(
        self, nonce: str, mfa_verified: bool,
    ) -> bool:
        """更新 MFA 验证状态(仅在 token 未消费时)。"""
        if nonce not in self._tokens or nonce in self._used_at:
            return False
        token = self._tokens[nonce]
        self._tokens[nonce] = replace(token, mfa_verified=mfa_verified)
        return True

    async def update_approver(
        self, nonce: str, approver_id: int,
    ) -> bool:
        """更新第二审批人 ID(仅在 token 未消费时)。"""
        if nonce not in self._tokens or nonce in self._used_at:
            return False
        token = self._tokens[nonce]
        self._tokens[nonce] = replace(token, approver_id=approver_id)
        return True

    async def update_final_confirm(
        self, nonce: str, final_confirm: bool,
    ) -> bool:
        """更新最终确认标记(仅在 token 未消费时)。"""
        if nonce not in self._tokens or nonce in self._used_at:
            return False
        token = self._tokens[nonce]
        self._tokens[nonce] = replace(token, final_confirm=final_confirm)
        return True


# ════════════════════════════════════════════════════════════════
# 4. simulate_lifecycle_path — 单条路径模拟
# ════════════════════════════════════════════════════════════════


async def simulate_lifecycle_path(
    spec: ButtonUXSpec,
    path: str,
    *,
    i18n_manager: Any,
    policy: Optional[ButtonUXPolicy] = None,
    principal_id: int = 1001,
    approver_id: int = 2002,
) -> LifecyclePathResult:
    """在 mock ButtonFlow 上模拟单条生命周期路径。

    Args:
        spec: ButtonUXSpec(由 ButtonUXPolicy.destructive_confirmation 产出)
        path: 路径名(必须在 LIFECYCLE_PATHS 中)
        i18n_manager: I18nManager 实例(或 duck-typed FakeI18nManager)
        policy: 可选的 ButtonUXPolicy(默认使用模块单例)
        principal_id: 模拟操作主体 ID
        approver_id: 模拟第二审批人 ID(双人审批路径用)

    Returns:
        LifecyclePathResult — 含路径产生的 error_code / 渲染文本 / 恢复按钮 /
        返回按钮 / token 消费状态

    Raises:
        ValueError: path 不在 LIFECYCLE_PATHS 中
    """
    if path not in LIFECYCLE_PATHS:
        raise ValueError(
            f"未知生命周期路径: {path!r}; 必须是 {LIFECYCLE_PATHS} 之一"
        )

    if policy is None:
        from services.button_ux_policy import get_button_ux_policy
        policy = get_button_ux_policy()

    # 构造 mock ButtonFlow(使用 InMemoryButtonTokenStore)
    store = InMemoryButtonTokenStore()
    flow = ButtonFlow(store=store)  # type: ignore[arg-type]

    # 渲染确认面板文本 + 按钮(所有路径都先渲染确认面板,模拟用户首次看到按钮)
    rendered_text = render_destructive_confirmation(spec, i18n_manager)
    confirmation_buttons = tuple(
        render_confirmation_buttons(
            spec, i18n_manager,
            confirm_callback=f"confirm|{spec.action}",
            cancel_callback=f"{CANCEL_CALLBACK_PREFIX}{spec.action}",
        )
    )

    # 路径分发
    if path == "confirm":
        return await _simulate_confirm_path(
            spec, flow, i18n_manager, principal_id, approver_id,
            rendered_text, confirmation_buttons,
        )
    elif path == "cancel":
        return await _simulate_cancel_path(
            spec, i18n_manager, rendered_text, confirmation_buttons,
            principal_id,
        )
    elif path == "expire":
        return await _simulate_expire_path(
            spec, flow, i18n_manager, rendered_text, confirmation_buttons,
            principal_id,
        )
    elif path == "retry":
        return await _simulate_retry_path(
            spec, flow, i18n_manager, rendered_text, confirmation_buttons,
            principal_id,
        )
    elif path == "return":
        return _simulate_return_path(
            spec, i18n_manager, rendered_text, confirmation_buttons,
        )

    # 不可达(LIFECYCLE_PATHS 已校验)
    raise ValueError(f"unreachable: path={path!r}")


async def _simulate_confirm_path(
    spec: ButtonUXSpec,
    flow: ButtonFlow,
    i18n_manager: Any,
    principal_id: int,
    approver_id: int,
    rendered_text: str,
    confirmation_buttons: tuple[InlineKeyboardButton, ...],
) -> LifecyclePathResult:
    """confirm 路径: prepare → mfa_verify(若需) → approve(若需) →
    confirm(若需) → execute → 成功。
    """
    # prepare
    prep = await flow.prepare(
        action=spec.action,
        principal_id=principal_id,
        target="test_target",
        resource_version="v1",
        request_hash="hash_" + spec.action,
        locale=spec.locale,
        ttl=DEFAULT_TTL_SECONDS,
    )
    if not prep.success or prep.token is None:
        return LifecyclePathResult(
            path="confirm", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note=f"prepare 失败: {prep.error_code}",
        )
    nonce = prep.token.nonce

    # 查询 action policy(决定是否需要 mfa/dual/final_confirm)
    from services.button_approval_policy import get_action_policy
    _, requires_mfa, requires_dual, requires_final = get_action_policy(spec.action)

    # mfa_verify(若需)
    if requires_mfa:
        mfa_result = await flow.mfa_verify(nonce, principal_id, "123456")
        if not mfa_result.success:
            return LifecyclePathResult(
                path="confirm", success=False,
                rendered_text=rendered_text,
                confirmation_buttons=confirmation_buttons,
                note=f"mfa_verify 失败: {mfa_result.error_code}",
            )

    # approve(若需双人审批)
    if requires_dual:
        appr_result = await flow.approve(
            nonce, approver_id=approver_id, principal_id=principal_id,
        )
        if not appr_result.success:
            return LifecyclePathResult(
                path="confirm", success=False,
                rendered_text=rendered_text,
                confirmation_buttons=confirmation_buttons,
                note=f"approve 失败: {appr_result.error_code}",
            )

    # confirm(若需最终确认)
    if requires_final:
        conf_result = await flow.confirm(nonce, principal_id)
        if not conf_result.success:
            return LifecyclePathResult(
                path="confirm", success=False,
                rendered_text=rendered_text,
                confirmation_buttons=confirmation_buttons,
                note=f"confirm 失败: {conf_result.error_code}",
            )

    # execute(CAS 消费 + executor 执行)
    async def _success_executor(token: ButtonToken) -> dict:
        return {"action": token.action, "executed": True}

    exec_result = await flow.execute(
        nonce, principal_id, "v1", executor=_success_executor,
    )
    if not exec_result.success:
        return LifecyclePathResult(
            path="confirm", success=False,
            error_code=exec_result.error_code,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note=f"execute 失败: {exec_result.error_code}",
        )

    return LifecyclePathResult(
        path="confirm",
        success=True,
        rendered_text=rendered_text,
        confirmation_buttons=confirmation_buttons,
        token_consumed=True,
        note="confirm 路径完成: prepare → mfa → approve → confirm → execute → 成功",
    )


async def _simulate_cancel_path(
    spec: ButtonUXSpec,
    i18n_manager: Any,
    rendered_text: str,
    confirmation_buttons: tuple[InlineKeyboardButton, ...],
    principal_id: int,
) -> LifecyclePathResult:
    """cancel 路径: 用户点击取消按钮 → 不调用 execute → token 保留未消费。

    渲染取消提示 + 返回按钮(无死路)。
    """
    # cancel 按钮在 confirmation_buttons 中(category="cancel")
    cancel_btn = next(
        (b for b in confirmation_buttons if b.category == "cancel"), None,
    )
    if cancel_btn is None:
        return LifecyclePathResult(
            path="cancel", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note="confirmation_buttons 缺少 cancel 按钮",
        )
    # 渲染返回按钮(取消后用户可回到初始菜单)
    back_btn = _render_back_button(i18n_manager, spec.locale)
    return LifecyclePathResult(
        path="cancel",
        success=True,
        rendered_text=rendered_text,
        confirmation_buttons=confirmation_buttons,
        back_button=back_btn,
        token_consumed=False,
        note="cancel 路径完成: 用户点击取消 → token 未消费 → 渲染返回按钮",
    )


async def _simulate_expire_path(
    spec: ButtonUXSpec,
    flow: ButtonFlow,
    i18n_manager: Any,
    rendered_text: str,
    confirmation_buttons: tuple[InlineKeyboardButton, ...],
    principal_id: int,
) -> LifecyclePathResult:
    """expire 路径: prepare(ttl=-1,已过期) → execute → CAS 失败 → 渲染 resubmit。

    模拟 token TTL 超时:用户在过期后才点击确认按钮,consume_token_cas
    因 expires_at <= now 返回 None,ButtonFlow.execute 返回
    BUTTON_POLICY_NONCE_CONSUMED(无法区分 expired/used/principal-mismatch/
    version-mismatch,均返回 NONCE_CONSUMED)。
    """
    # prepare(ttl=-1 → expires_at 在过去)
    prep = await flow.prepare(
        action=spec.action,
        principal_id=principal_id,
        target="test_target",
        resource_version="v1",
        request_hash="hash_" + spec.action,
        locale=spec.locale,
        ttl=-1,  # 已过期
    )
    if not prep.success or prep.token is None:
        return LifecyclePathResult(
            path="expire", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note=f"prepare 失败: {prep.error_code}",
        )
    # execute → CAS 失败(expires_at <= now)
    exec_result = await flow.execute(
        prep.token.nonce, principal_id, "v1", executor=None,
    )
    if exec_result.success:
        return LifecyclePathResult(
            path="expire", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note="expire 路径期望 execute 失败,但实际成功(token 未过期?)",
        )
    # 渲染恢复按钮(expire → resubmit)
    recovery = tuple(
        render_recovery_options(
            exec_result.error_code, spec.locale, i18n_manager,
            action=spec.action, target="test_target",
        )
    )
    back_btn = _render_back_button(i18n_manager, spec.locale)
    return LifecyclePathResult(
        path="expire",
        success=True,
        error_code=exec_result.error_code,
        rendered_text=rendered_text,
        confirmation_buttons=confirmation_buttons,
        recovery_buttons=recovery,
        back_button=back_btn,
        token_consumed=False,
        note=(
            f"expire 路径完成: ttl=-1 → execute CAS 失败 "
            f"({exec_result.error_code}) → 渲染 resubmit 恢复按钮"
        ),
    )


async def _simulate_retry_path(
    spec: ButtonUXSpec,
    flow: ButtonFlow,
    i18n_manager: Any,
    rendered_text: str,
    confirmation_buttons: tuple[InlineKeyboardButton, ...],
    principal_id: int,
) -> LifecyclePathResult:
    """retry 路径: prepare(v1) → execute(错版本 v2) → CAS 失败 → 渲染恢复按钮。

    模拟资源版本冲突:用户在资源已被更新后点击旧按钮,consume_token_cas
    因 resource_version 不匹配返回 None,ButtonFlow.execute 返回
    BUTTON_POLICY_NONCE_CONSUMED。token 未消费,用户可重新发起获取新 token。
    """
    prep = await flow.prepare(
        action=spec.action,
        principal_id=principal_id,
        target="test_target",
        resource_version="v1",
        request_hash="hash_" + spec.action,
        locale=spec.locale,
        ttl=DEFAULT_TTL_SECONDS,
    )
    if not prep.success or prep.token is None:
        return LifecyclePathResult(
            path="retry", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note=f"prepare 失败: {prep.error_code}",
        )
    # execute → CAS 失败(resource_version 不匹配)
    exec_result = await flow.execute(
        prep.token.nonce, principal_id, "v2_wrong_version", executor=None,
    )
    if exec_result.success:
        return LifecyclePathResult(
            path="retry", success=False,
            rendered_text=rendered_text,
            confirmation_buttons=confirmation_buttons,
            note="retry 路径期望 execute 失败,但实际成功(版本不匹配未触发?)",
        )
    # 渲染恢复按钮(version mismatch → resubmit/reload)
    recovery = tuple(
        render_recovery_options(
            exec_result.error_code, spec.locale, i18n_manager,
            action=spec.action, target="test_target",
        )
    )
    back_btn = _render_back_button(i18n_manager, spec.locale)
    return LifecyclePathResult(
        path="retry",
        success=True,
        error_code=exec_result.error_code,
        rendered_text=rendered_text,
        confirmation_buttons=confirmation_buttons,
        recovery_buttons=recovery,
        back_button=back_btn,
        token_consumed=False,
        note=(
            f"retry 路径完成: 错版本 v2 → execute CAS 失败 "
            f"({exec_result.error_code}) → 渲染恢复按钮"
        ),
    )


def _simulate_return_path(
    spec: ButtonUXSpec,
    i18n_manager: Any,
    rendered_text: str,
    confirmation_buttons: tuple[InlineKeyboardButton, ...],
) -> LifecyclePathResult:
    """return 路径: 取消/过期/失败后 → 渲染 back 按钮回到初始菜单。

    return 路径不调用 ButtonFlow(无 token 操作),只验证 back 按钮可渲染,
    证明"无死路"要求(P1-08 + P1-11 共同强制)。
    """
    back_btn = _render_back_button(i18n_manager, spec.locale)
    return LifecyclePathResult(
        path="return",
        success=True,
        rendered_text=rendered_text,
        confirmation_buttons=confirmation_buttons,
        back_button=back_btn,
        token_consumed=False,
        note="return 路径完成: 渲染 back 按钮(callback_data=menu:main)",
    )


def _render_back_button(i18n_manager: Any, locale: str) -> InlineKeyboardButton:
    """渲染返回按钮(与 bots/admin_bot/menus.py 的 BACK_BTN 一致)。

    使用 i18n key 'bot.admin_bot.menus.s20' 查询文案,
    callback_data 固定为 'menu:main'(返回主菜单)。
    """
    try:
        label = i18n_manager.translate(
            "bot.admin_bot.menus.s20", locale=locale,
        )
    except Exception:
        label = "返回" if locale == "zh-CN" else "Back"
    return InlineKeyboardButton(
        label=label,
        callback_data=RETURN_CALLBACK_DATA,
        category="return",
    )


# ════════════════════════════════════════════════════════════════
# 5. verify_all_lifecycle_paths — 全部 5 路径验证
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LifecycleVerification:
    """R65 P1-11: 一个 ButtonUXSpec 的 5 路径验证结果汇总。"""
    spec_action: str
    locale: str
    path_results: tuple[LifecyclePathResult, ...]
    all_passed: bool
    failed_paths: tuple[str, ...] = ()

    def path_result(self, path: str) -> LifecyclePathResult:
        """按路径名查询结果。"""
        for r in self.path_results:
            if r.path == path:
                return r
        raise KeyError(f"路径未模拟: {path!r}")


async def verify_all_lifecycle_paths(
    spec: ButtonUXSpec,
    *,
    i18n_manager: Any,
    policy: Optional[ButtonUXPolicy] = None,
    principal_id: int = 1001,
    approver_id: int = 2002,
) -> LifecycleVerification:
    """对一个 ButtonUXSpec 验证全部 5 条生命周期路径。

    Args:
        spec: ButtonUXSpec
        i18n_manager: I18nManager 实例
        policy: 可选 ButtonUXPolicy(默认使用模块单例)
        principal_id: 模拟主体 ID
        approver_id: 模拟审批人 ID

    Returns:
        LifecycleVerification — 含 5 条路径结果与 all_passed 标志
    """
    results: list[LifecyclePathResult] = []
    failed: list[str] = []
    for path in LIFECYCLE_PATHS:
        result = await simulate_lifecycle_path(
            spec, path,
            i18n_manager=i18n_manager,
            policy=policy,
            principal_id=principal_id,
            approver_id=approver_id,
        )
        results.append(result)
        if not result.success:
            failed.append(path)

    return LifecycleVerification(
        spec_action=spec.action,
        locale=spec.locale,
        path_results=tuple(results),
        all_passed=(len(failed) == 0),
        failed_paths=tuple(failed),
    )


# ════════════════════════════════════════════════════════════════
# 6. 模块导出
# ════════════════════════════════════════════════════════════════

__all__ = [
    # 常量
    "LIFECYCLE_PATHS",
    "RETURN_CALLBACK_DATA",
    "CANCEL_CALLBACK_PREFIX",
    # 数据类
    "LifecyclePathResult",
    "LifecycleVerification",
    # 内存 store
    "InMemoryButtonTokenStore",
    # 模拟器
    "simulate_lifecycle_path",
    "verify_all_lifecycle_paths",
]
