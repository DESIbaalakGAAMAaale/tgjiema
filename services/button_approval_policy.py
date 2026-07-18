"""R55 §18: 统一按钮 Approval Policy — 所有高风险按钮的统一审批策略入口。

职责:
    - 定义统一 ``ButtonApprovalContext`` 数据结构,绑定 principal/resource/version/hash/expiry/nonce
    - 定义 ``BUTTON_APPROVAL_POLICY`` 全局策略表,按 action 分级
    - 提供 ``enforce_button_approval_policy()`` 统一校验所有绑定
    - 提供 ``make_button_error_response()`` 生成 ErrorEnvelope + i18n 错误响应

六种攻击向量防护:
    1. 双击         → nonce 原子消费(callback_nonces 表 UPDATE WHERE consumed_at IS NULL)
    2. 跨用户       → principal 绑定(callback user_id 与 current principal 必须匹配)
    3. 旧版本       → resource_version 绑定(资源版本不匹配 → 签名不匹配 → 拒绝)
    4. 篡改         → HMAC-SHA256 签名(常量时间比较,128 bit)
    5. 重放         → nonce + expire_ts(过期或已消费的 nonce 拒绝)
    6. 并发点击     → CAS UPDATE + rowcount 检测(仅一个请求能成功消费 nonce)

设计要点:
    - fail-closed:任何必填字段缺失或校验失败,立即 raise AppError(不降级执行)
    - 统一输出:错误响应通过 ``error_codes.make_error_response()`` 生成 6 键格式
    - i18n:错误消息通过 ``locales/zh-CN.json`` 与 ``locales/en-US.json`` 渲染
    - 与现有模块协作:
        * ``button_security.py`` 提供 HMAC 签名 + nonce 持久化 + 原子消费
        * ``approval_workflow.py`` 提供审批状态机 + CommandBus 路由
        * ``error_codes.py`` 提供统一错误码 + ErrorEnvelope
        * ``callback_allowlist.py`` 提供运行时 action 白名单门禁
"""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from services.button_security import (
    HIGH_RISK_ACTIONS,
    verify_button_token,
)
from services.error_codes import (
    AppError,
    ErrorCodes,
    make_error_response,
)
from services.i18n import translate as _i18n_t


# ════════════════════════════════════════════════════════════════
# 1. 按钮 Approval Policy 分级
# ════════════════════════════════════════════════════════════════

# Policy level — 按钮风险分级
POLICY_LEVEL_LOW = "low"           # 低风险(查看/取消/刷新等只读操作)
POLICY_LEVEL_HIGH = "high"         # 高风险(需要 nonce + 签名 + 审批)
POLICY_LEVEL_CRITICAL = "critical"  # 极高风险(需要 MFA + 双人审批 + 最终确认)


# 极高风险 action 集合(必须 MFA + 双人审批 + 最终确认)
# 这些 action 影响多用户权益或系统关键状态,除常规审批外还要求:
#   1. MFA 已验证(session.mfa_verified = True)
#   2. 双人审批(approver_id 与 principal_id 不同)
#   3. 最终确认(final_confirm = True,防止误点)
CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL: frozenset[str] = frozenset({
    "break_glass",        # 紧急访问(绕过常规权限,影响系统安全姿态)
    "purge", "purge_file",  # 物理清除文件(不可恢复)
    "delete", "delete_file",  # 删除文件(影响用户数据)
    "rotate_keys",        # 密钥轮转(影响所有加密数据)
    "reset_quota",        # 配额重置(影响用户权益)
    "admin_grant",        # 管理员授权(影响 RBAC)
    "factory_reset",      # 工厂重置(影响整个系统)
    "crdb_delete",        # CRDB 删除(影响跨机同步数据)
})


# 需要最终确认的 action(防误点)
# 这些 action 在执行前必须经过 final_confirm=True 标记,
# 防止用户在 confirm 弹窗中误点"确认"后立即执行
ACTIONS_REQUIRING_FINAL_CONFIRM: frozenset[str] = frozenset({
    "ban", "unban",
    "takedown", "release_takedown",
    "purge", "purge_file", "restore", "restore_file",
    "delete", "delete_file",
    "admin_grant", "admin_revoke",
    "rotate_keys",
    "reset_quota",
    "break_glass",
    "force_logout",
    "approve_appeal", "reject_appeal",
    "update_config", "reload_config",
    "factory_reset",
    "crdb_delete",
})


# Policy 决策表:action → (policy_level, requires_mfa, requires_dual_approval, requires_final_confirm)
def _build_button_approval_policy() -> dict[str, tuple[str, bool, bool, bool]]:
    """构建按钮 Approval Policy 决策表。

    Returns:
        {action: (policy_level, requires_mfa, requires_dual_approval, requires_final_confirm)}
    """
    policy: dict[str, tuple[str, bool, bool, bool]] = {}
    # 高风险 action:均需 nonce + 签名 + 审批,部分需要 MFA / 双人 / 最终确认
    for action in HIGH_RISK_ACTIONS:
        requires_dual = action in CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL
        requires_final = action in ACTIONS_REQUIRING_FINAL_CONFIRM
        # 极高风险 action 强制 MFA
        requires_mfa = requires_dual or action in {
            "break_glass", "rotate_keys", "reset_quota", "admin_grant",
        }
        level = POLICY_LEVEL_CRITICAL if requires_dual else POLICY_LEVEL_HIGH
        policy[action] = (level, requires_mfa, requires_dual, requires_final)
    # R55 §18: CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL 中的 action 可能不在
    # HIGH_RISK_ACTIONS 中(如 crdb_delete / factory_reset),也必须加入 Policy 表
    for action in CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL:
        if action in policy:
            continue
        requires_final = action in ACTIONS_REQUIRING_FINAL_CONFIRM
        policy[action] = (POLICY_LEVEL_CRITICAL, True, True, requires_final)
    return policy


# 全局 Policy 决策表(模块加载时构建)
BUTTON_APPROVAL_POLICY: dict[str, tuple[str, bool, bool, bool]] = _build_button_approval_policy()


def get_action_policy(action: str) -> tuple[str, bool, bool, bool]:
    """查询 action 的 Approval Policy。

    Args:
        action: 动作标识

    Returns:
        (policy_level, requires_mfa, requires_dual_approval, requires_final_confirm)
        低风险 action 返回 (POLICY_LEVEL_LOW, False, False, False)
    """
    return BUTTON_APPROVAL_POLICY.get(
        action, (POLICY_LEVEL_LOW, False, False, False)
    )


# ════════════════════════════════════════════════════════════════
# 2. 统一按钮 Approval Context 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ButtonApprovalContext:
    """R55 §18: 统一按钮 Approval Context — 所有高风险按钮必须构建的审批上下文。

    绑定字段(六种攻击向量防护):
        - principal_id:    操作主体 ID(防跨用户攻击)
        - resource:        资源标识(如 file_code, user_id, approval_id)
        - resource_version: 资源版本(防旧版本按钮操作已更新资源)
        - request_hash:    审批请求 Hash(防审批与记录错位)
        - expiry_ts:       过期时间戳(防长期有效 callback)
        - nonce:           随机 nonce(防重放/双击/并发点击)
        - signature:       HMAC 签名(防篡改)

    可选字段:
        - mfa_verified:    MFA 是否已验证(极高风险 action 强制)
        - approver_id:      审批人 ID(双人审批时必须 ≠ principal_id)
        - final_confirm:    最终确认标记(防误点)
        - mfa_receipt:      R62 P1-07 MFA receipt payload dict(由 verify_mfa_receipt
                            返回,含 iat 字段);高风险动作会校验其签发年龄,
                            防止陈旧 receipt 绕过二次认证。None 时仅校验布尔
                            mfa_verified(向后兼容)。
    """
    action: str
    principal_id: int
    resource: str
    resource_version: str
    request_hash: str
    expiry_ts: int
    nonce: str
    signature: str
    mfa_verified: bool = False
    approver_id: int = 0
    final_confirm: bool = False
    mfa_receipt: Optional[dict] = None

    def validate_required_fields(self) -> None:
        """校验所有必填字段非空(fail-closed)。

        Raises:
            AppError: 任一必填字段缺失时(BUTTON_POLICY_BINDING_MISSING)
        """
        missing: list[str] = []
        if not self.action:
            missing.append("action")
        if not self.principal_id or int(self.principal_id) <= 0:
            missing.append("principal_id")
        if not self.resource:
            missing.append("resource")
        if not self.resource_version:
            missing.append("resource_version")
        if not self.request_hash:
            missing.append("request_hash")
        if not self.expiry_ts or int(self.expiry_ts) <= 0:
            missing.append("expiry_ts")
        if not self.nonce:
            missing.append("nonce")
        if not self.signature:
            missing.append("signature")
        if missing:
            raise AppError(
                ErrorCodes.BUTTON_POLICY_BINDING_MISSING,
                params={
                    "action": self.action,
                    "missing_field": ",".join(missing),
                    "reason": "required_binding_missing",
                },
            )


# ════════════════════════════════════════════════════════════════
# 3. 统一 Approval Policy 校验入口
# ════════════════════════════════════════════════════════════════


async def enforce_button_approval_policy(
    ctx: ButtonApprovalContext,
    current_principal_id: int,
    callback_data: str,
    *,
    resource_version_expected: str = "",
    request_hash_expected: str = "",
    store=None,
) -> tuple[bool, str, str]:
    """R55 §18: 统一按钮 Approval Policy 校验入口。

    所有高风险按钮必须经过本函数校验,确保:
        1. 所有必填绑定字段非空(fail-closed)
        2. callback 签名 + nonce + expiry 通过 ``verify_button_token`` 校验
        3. principal 匹配(callback user_id 与 current_principal_id 一致)
        4. resource_version 匹配(防旧版本按钮)
        5. request_hash 匹配(防审批与记录错位)
        6. MFA 已验证(极高风险 action 强制)
        7. 双人审批(approver_id 与 principal_id 不同)
        8. 最终确认(final_confirm=True)

    本函数为统一入口,调用 ``button_security.verify_button_token`` 完成底层
    签名 + nonce + expiry 校验,本函数补充 Policy 级别的 MFA / 双人审批 /
    最终确认校验。

    Args:
        ctx: ButtonApprovalContext 审批上下文(必须已填充所有绑定字段)
        current_principal_id: 当前操作主体 ID(从 session 获取)
        callback_data: 原始 callback_data 字符串(用于 verify_button_token)
        resource_version_expected: 期望的 resource_version(从资源当前状态获取)
        request_hash_expected: 期望的 request_hash(从审批记录获取)
        store: 可选 CacheStore 实例(测试注入)

    Returns:
        (valid, action, data): valid=True 时 action/data 可用;
        valid=False 时 action/data 为空字符串

    Raises:
        AppError: 当 MFA / 双人审批 / 最终确认校验失败时(非 AppError 的
            底层签名/nonce/expiry/principal 校验失败返回 (False, "", ""))
    """
    # ── 步骤 1: 校验必填绑定字段(fail-closed)──────────────────
    ctx.validate_required_fields()

    # ── 步骤 2: 查询 action 的 Approval Policy ──────────────────
    policy_level, requires_mfa, requires_dual, requires_final = get_action_policy(ctx.action)

    # 低风险 action 不需要 Approval Policy(直接放行,但仍需 verify_button_token)
    # 低风险 action 通常不调用本函数,这里保持兼容
    if policy_level == POLICY_LEVEL_LOW:
        return await verify_button_token(
            callback_data, current_principal_id, store=store,
        )

    # ── 步骤 3: 底层签名 + nonce + expiry + principal 校验 ──────
    # verify_button_token 内部完成:
    #   - 解析 callback_data 各字段
    #   - 验证 user_id 与 current_principal_id 匹配(跨用户防护)
    #   - 验证未过期(expiry_ts > now,重放防护)
    #   - 高风险 action 必须 6 段格式(含 nonce)
    #   - 签名长度 ≥ 32 hex chars(128 bit)
    #   - 签名匹配(常量时间比较,篡改防护)
    #   - 原子消费 nonce(双击/并发点击防护)
    valid, action, data = await verify_button_token(
        callback_data, current_principal_id, store=store,
    )
    if not valid:
        # 底层校验失败已记录日志,这里直接返回
        # 失败原因可能是:过期、签名不匹配、nonce 已消费、principal 不匹配
        # 这些都是安全攻击向量,统一返回 False 不暴露具体原因
        logger.warning(
            f"[ButtonPolicy] R55 §18: 底层 verify_button_token 失败 "
            f"action={ctx.action} principal={current_principal_id}"
        )
        return False, "", ""

    # ── 步骤 4: resource_version 匹配校验(防旧版本按钮)─────────
    if resource_version_expected:
        if not ctx.resource_version or ctx.resource_version != resource_version_expected:
            logger.warning(
                f"[ButtonPolicy] R55 §18: resource_version 不匹配 "
                f"action={ctx.action} expected={resource_version_expected} "
                f"got={ctx.resource_version}"
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH,
                params={
                    "action": ctx.action,
                    "reason": "resource_version_mismatch",
                },
            )

    # ── 步骤 5: request_hash 匹配校验(防审批与记录错位)─────────
    if request_hash_expected:
        if not ctx.request_hash or ctx.request_hash != request_hash_expected:
            logger.warning(
                f"[ButtonPolicy] R55 §18: request_hash 不匹配 "
                f"action={ctx.action} principal={current_principal_id}"
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_HASH_MISMATCH,
                params={
                    "action": ctx.action,
                    "reason": "request_hash_mismatch",
                },
            )

    # ── 步骤 6: MFA 强制门禁(极高风险 action)──────────────────
    if requires_mfa and not ctx.mfa_verified:
        logger.warning(
            f"[ButtonPolicy] R55 §18: MFA 未验证 "
            f"action={ctx.action} principal={current_principal_id}"
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_MFA_REQUIRED,
            params={
                "action": ctx.action,
                "reason": "mfa_verification_required",
            },
        )

    # R62 P1-07: 高风险动作验证 MFA age,不只验证布尔 mfa_verified
    # 高风险动作(requires_mfa=True)且调用方提供了 mfa_receipt(verify_mfa_receipt
    # 返回的 payload dict)时,额外校验 receipt 签发时间距今不超过 max_age_seconds。
    # 防止攻击者使用陈旧但仍未过期的 receipt(如刚签发后立刻泄露)绕过二次认证。
    # mfa_receipt 为 None 时不校验年龄(向后兼容,仅依赖布尔 mfa_verified)。
    if requires_mfa and ctx.mfa_receipt:
        # 延迟 import 避免循环依赖
        from admin.mfa import get_mfa_manager
        mfa_manager = get_mfa_manager()
        # 默认 5 分钟内签发的 receipt 才视为"近期完成 MFA"
        if not mfa_manager.verify_mfa_receipt_age(ctx.mfa_receipt, max_age_seconds=300):
            logger.warning(
                f"[ButtonPolicy] R62 P1-07: MFA receipt 已过期(age 超限) "
                f"action={ctx.action} principal={current_principal_id}"
            )
            raise AppError(
                ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED,
                params={
                    "user_id": current_principal_id,
                    "reason": "mfa_receipt_age_exceeded",
                    "action": ctx.action,
                },
            )

    # ── 步骤 7: 双人审批(极高风险 action)──────────────────────
    if requires_dual:
        if not ctx.approver_id or int(ctx.approver_id) <= 0:
            logger.warning(
                f"[ButtonPolicy] R55 §18: 双人审批缺失 approver "
                f"action={ctx.action} principal={current_principal_id}"
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                params={
                    "action": ctx.action,
                    "reason": "approver_id_required",
                },
            )
        if int(ctx.approver_id) == int(ctx.principal_id):
            logger.warning(
                f"[ButtonPolicy] R55 §18: 双人审批 approver 与 principal 相同 "
                f"action={ctx.action} principal={current_principal_id}"
            )
            raise AppError(
                ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
                params={
                    "action": ctx.action,
                    "reason": "approver_must_differ_from_principal",
                },
            )

    # ── 步骤 8: 最终确认(防误点)──────────────────────────────
    if requires_final and not ctx.final_confirm:
        logger.warning(
            f"[ButtonPolicy] R55 §18: 最终确认缺失 "
            f"action={ctx.action} principal={current_principal_id}"
        )
        raise AppError(
            ErrorCodes.BUTTON_POLICY_FINAL_CONFIRM_REQUIRED,
            params={
                "action": ctx.action,
                "reason": "final_confirm_required",
            },
        )

    # ── 所有校验通过 ──────────────────────────────────────────
    logger.info(
        f"[ButtonPolicy] R55 §18: 审批通过 "
        f"action={ctx.action} principal={current_principal_id} "
        f"level={policy_level} mfa={ctx.mfa_verified} "
        f"dual={requires_dual} final={ctx.final_confirm}"
    )
    return True, action, data


# ════════════════════════════════════════════════════════════════
# 4. 统一错误响应 helper(ErrorEnvelope + i18n)
# ════════════════════════════════════════════════════════════════


def make_button_error_response(
    error_code: str,
    trace_id: str,
    *,
    action: str = "",
    reason: str = "",
    missing_field: str = "",
    retryable: bool = False,
    severity: str = "critical",
) -> dict:
    """R55 §18: 生成按钮错误响应(ErrorEnvelope + i18n)。

    统一输出格式:
        {code, message_key, trace_id, retryable, severity, safe_params}

    安全过滤:
        - 不暴露 nonce / signature / hash / resource_version 等敏感字段
        - 仅暴露 action / reason / missing_field(供前端渲染用户消息)

    Args:
        error_code: ErrorCodes 常量(如 BUTTON_POLICY_BINDING_MISSING)
        trace_id: UUID trace_id(贯穿全链路)
        action: 动作标识(可安全暴露)
        reason: 失败原因(可安全暴露)
        missing_field: 缺失字段名(仅 BINDING_MISSING 时使用)
        retryable: 是否可重试
        severity: 严重级别

    Returns:
        统一错误响应 dict
    """
    # 安全参数:仅 action / reason / missing_field
    # 不暴露 nonce / signature / hash / resource_version / principal_id
    safe_params: dict = {"action": action, "reason": reason}
    if missing_field:
        safe_params["missing_field"] = missing_field

    # 根据 error_code 确定 message_key
    message_key_map = {
        ErrorCodes.BUTTON_POLICY_BINDING_MISSING: "errors.button.policy.binding_missing",
        ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED: "errors.button.policy.nonce_consumed",
        ErrorCodes.BUTTON_POLICY_SIGNATURE_INVALID: "errors.button.policy.signature_invalid",
        ErrorCodes.BUTTON_POLICY_EXPIRED: "errors.button.policy.expired",
        ErrorCodes.BUTTON_POLICY_PRINCIPAL_MISMATCH: "errors.button.policy.principal_mismatch",
        ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH: "errors.button.policy.version_mismatch",
        ErrorCodes.BUTTON_POLICY_HASH_MISMATCH: "errors.button.policy.hash_mismatch",
        ErrorCodes.BUTTON_POLICY_MFA_REQUIRED: "errors.button.policy.mfa_required",
        ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED: "errors.button.policy.dual_approval_required",
        ErrorCodes.BUTTON_POLICY_FINAL_CONFIRM_REQUIRED: "errors.button.policy.final_confirm_required",
    }
    message_key = message_key_map.get(error_code, "errors.button.policy.unknown")

    return make_error_response(
        code=error_code,
        message_key=message_key,
        trace_id=trace_id,
        retryable=retryable,
        severity=severity,
        safe_params=safe_params,
    )


# ════════════════════════════════════════════════════════════════
# 5. 模块导出
# ════════════════════════════════════════════════════════════════


__all__ = [
    # Policy 级别
    "POLICY_LEVEL_LOW",
    "POLICY_LEVEL_HIGH",
    "POLICY_LEVEL_CRITICAL",
    # 集合
    "CRITICAL_ACTIONS_REQUIRING_DUAL_APPROVAL",
    "ACTIONS_REQUIRING_FINAL_CONFIRM",
    "BUTTON_APPROVAL_POLICY",
    # 函数
    "get_action_policy",
    "enforce_button_approval_policy",
    "make_button_error_response",
    # 数据类
    "ButtonApprovalContext",
]
