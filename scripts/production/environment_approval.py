"""R67 Wave 4: 环境审批门禁(environment approval gate)。

R67 审计要求(Wave 4 — RC tag 正式演练):
    "tag workflow、environment approval、production evidence、digest-pinned
     deploy、rollback 全通过"

environment approval gate 确保 production 部署前已获得明确审批:
    1. 审批记录必须存在(approval_record 非空)
    2. 审批者与执行者必须不同(职责分离)
    3. 审批必须针对当前 candidate(approval.candidate_tag 匹配)
    4. 审批必须针对当前环境(approval.environment_id 匹配)
    5. 审批必须在有效期内(approval.approved_at 在 time_window 内)
    6. 审批未被撤销(approval.revoked != true)

使用方法:
    gate = EnvironmentApprovalGate(
        candidate_tag="rc-2026-07-21-v1",
        environment_id="production-vps-01",
    )
    approval_record = {
        "candidate_tag": "rc-2026-07-21-v1",
        "environment_id": "production-vps-01",
        "approved_by": "manager@example.com",
        "approved_at": "2026-07-21T10:00:00+00:00",
        "expires_at": "2026-07-21T22:00:00+00:00",
        "revoked": False,
    }
    gate.verify(approval_record, executed_by="ops@example.com")
"""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _now_utc() -> _dt.datetime:
    """当前 UTC 时间(带时区)。"""
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(value: str) -> _dt.datetime | None:
    """解析 ISO 8601 时间字符串(支持 Z 后缀)。"""
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class EnvironmentApprovalGate:
    """R67 Wave 4: 环境审批门禁。

    验证 production 部署前已获得明确、有效、针对当前 candidate + environment 的审批。
    """

    def __init__(
        self,
        *,
        candidate_tag: str,
        environment_id: str,
        clock: _dt.datetime | None = None,
    ) -> None:
        """初始化审批门禁。

        Args:
            candidate_tag: 当前候选 RC tag(如 "rc-2026-07-21-v1")
            environment_id: 当前部署环境 ID(如 "production-vps-01")
            clock: 可选的自定义时钟(用于测试),默认当前 UTC 时间
        """
        if not candidate_tag:
            raise ValueError("candidate_tag 不能为空")
        if not environment_id:
            raise ValueError("environment_id 不能为空")

        self.candidate_tag = candidate_tag
        self.environment_id = environment_id
        self.clock = clock

    def _now(self) -> _dt.datetime:
        return self.clock or _now_utc()

    def verify(
        self,
        approval_record: dict[str, Any],
        *,
        executed_by: str,
    ) -> dict[str, Any]:
        """验证审批记录是否满足 production 部署门禁。

        Args:
            approval_record: 审批记录 dict(见模块文档)
            executed_by: 执行部署的用户/服务账号(必须与 approved_by 不同)

        Returns:
            {
                "approved": bool,
                "reason": str,  # 失败原因(成功时为空)
                "candidate_tag": str,
                "environment_id": str,
                "approved_by": str,
                "approved_at": str,
            }

        Raises:
            ValueError: approval_record 不是 dict
        """
        if not isinstance(approval_record, dict):
            raise ValueError("approval_record 必须是 dict")

        reason = ""
        approved = False
        now = self._now()

        # 1. 审批记录必须存在(非空)
        if not approval_record:
            reason = "审批记录为空"
        # 2. 审批必须针对当前 candidate
        elif approval_record.get("candidate_tag") != self.candidate_tag:
            reason = (
                f"candidate_tag 不匹配: approval="
                f"{approval_record.get('candidate_tag')!r} vs expected="
                f"{self.candidate_tag!r}"
            )
        # 3. 审批必须针对当前环境
        elif approval_record.get("environment_id") != self.environment_id:
            reason = (
                f"environment_id 不匹配: approval="
                f"{approval_record.get('environment_id')!r} vs expected="
                f"{self.environment_id!r}"
            )
        # 4. 审批者与执行者必须不同(职责分离)
        elif not approval_record.get("approved_by"):
            reason = "审批记录缺少 approved_by"
        elif approval_record.get("approved_by") == executed_by:
            reason = (
                f"审批者与执行者相同({executed_by!r})— 违反职责分离原则"
            )
        # 5. 审批未被撤销
        elif approval_record.get("revoked") is True:
            reason = "审批已被撤销(revoked=true)"
        # 6. 审批必须在有效期内
        else:
            approved_at = _parse_iso(approval_record.get("approved_at", ""))
            expires_at = _parse_iso(approval_record.get("expires_at", ""))
            if approved_at is None:
                reason = f"approved_at 解析失败: {approval_record.get('approved_at')!r}"
            elif approved_at > now:
                reason = (
                    f"审批时间在未来(approved_at={approved_at.isoformat()} "
                    f"> now={now.isoformat()})"
                )
            elif expires_at is None:
                reason = f"expires_at 解析失败: {approval_record.get('expires_at')!r}"
            elif now > expires_at:
                reason = (
                    f"审批已过期(now={now.isoformat()} > "
                    f"expires_at={expires_at.isoformat()})"
                )
            else:
                approved = True

        return {
            "approved": approved,
            "reason": reason,
            "candidate_tag": self.candidate_tag,
            "environment_id": self.environment_id,
            "approved_by": approval_record.get("approved_by", ""),
            "approved_at": approval_record.get("approved_at", ""),
        }


def verify_environment_approval(
    approval_record: dict[str, Any],
    *,
    candidate_tag: str,
    environment_id: str,
    executed_by: str,
) -> dict[str, Any]:
    """便捷函数:验证环境审批(无需实例化 gate)。"""
    gate = EnvironmentApprovalGate(
        candidate_tag=candidate_tag,
        environment_id=environment_id,
    )
    return gate.verify(approval_record, executed_by=executed_by)
