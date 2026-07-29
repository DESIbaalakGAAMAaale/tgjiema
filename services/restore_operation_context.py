"""R76 P0-05 / O8: 恢复操作独立上下文 — expected 值的权威来源。

R76 终审报告 P0-05 要求:restore writer 的所有 expected 值(operation_id /
source_sha / nonce / run_id / run_attempt / audience / target_identity / target_uri)
必须来自独立来源,**不得由 capability 自身回填**(否则为自比较,无安全意义)。

本模块定义 ``RestoreOperationContext`` — 由 ``RestoreOrchestrator.start_operation()``
持久化的权威状态对象,writer 调用 ``verify_and_consume_capability()`` 时必须传入
同一 context 实例,作为独立 expected 值来源。

安全模型:
    1. ``RestoreOrchestrator.start_operation()`` 创建 operation 时,持久化完整
       ``RestoreOperationContext`` 到 ``restore_operations`` 表(含 source_sha /
       run_id / run_attempt / audience / target_identity / target_uri 等独立字段)。
    2. ``issue_capability()`` 签发 capability 时,从持久化 context 读取 expected 值
       (不从环境变量或调用方参数读取,防止伪造)。
    3. writer 调用 ``verify_and_consume_capability()`` 时,传入同一 context 实例,
       作为独立 expected 值来源(不从 capability 回填)。
    4. 任一字段缺失立即抛 ``BACKUP_RESTORE_TRUST_CHAIN_REQUIRED`` (fail-closed)。

不变性:
    - ``RestoreOperationContext`` 为 frozen dataclass,构造后字段不可变,
      防止调用方篡改 expected 值。
    - 所有字段为必填(无 Optional / 默认值),缺任一字段构造失败。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.error_codes import AppError, ErrorCodes


@dataclass(frozen=True)
class RestoreOperationContext:
    """恢复操作独立上下文 — writer expected 值的权威来源。

    R76 P0-05 / O8: 所有 expected 值由 orchestrator 权威状态加载,
    不得由 capability 自身回填。本 dataclass 为 frozen,构造后字段不可变。

    Attributes:
        operation_id: 恢复操作唯一 ID(UUID,由 orchestrator 生成)
        backup_id: 备份 ID(timestamp,绑定 capability)
        source_sha: 当前 master SHA(独立来源,防跨 commit 重放)
        run_id: GitHub Actions run ID(独立来源,防跨 run 重放)
        run_attempt: GitHub Actions run attempt(独立来源,防跨 attempt 重放)
        audience: 目标受众标识(如 "restore-writer",绑定 capability)
        target_identity: 恢复目标数据库 identity hash(独立来源,非 capability 自报)
        target_uri: 恢复目标 URI(独立来源,非 capability 自报)
        manifest_digest: manifest 原始 bytes SHA-256(绑定 nonce,防换 manifest)
        payload_digest: payload canonical JSON SHA-256(绑定 nonce,防换 payload)
        allowed_action: 允许的操作(必填,通常为 "restore_to_blank_target";R76 P0-05 强制非默认值)
        nonce: capability nonce(由 orchestrator 生成,独立于 capability 签发;R76 P0-05 强制必填)
    """

    operation_id: str
    backup_id: str
    source_sha: str
    run_id: int
    run_attempt: int
    audience: str
    target_identity: str
    target_uri: str
    manifest_digest: str
    payload_digest: str
    # R76 P0-05 整改:删除 allowed_action 与 nonce 的默认值 — 强制构造时显式传入
    # (无默认值,缺失即 TypeError fail-closed)
    allowed_action: str
    nonce: str

    def validate(self) -> None:
        """R76 P0-05: 校验所有必填字段非空(fail-closed)。

        缺任一字段立即抛 ``ValueError``,防止 capability 自比较绕过。
        本方法由 ``verify_and_consume_capability()`` 在 writer 入口调用。

        Raises:
            AppError: 任一必填字段为空或非法 (VALIDATION_FAILED)
        """
        # R76 P0-05: 所有 expected 值必须来自独立来源,缺任一即 fail-closed
        if not self.operation_id:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.operation_id 必须非空"
                                  "(独立 expected 值,不得由 capability 回填)"},
            )
        if not self.backup_id:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.backup_id 必须非空"},
            )
        if not self.source_sha:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.source_sha 必须非空"
                                  "(master SHA,防跨 commit 重放)"},
            )
        if not isinstance(self.run_id, int) or self.run_id < 0:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.run_id 必须为非负整数"
                                  "(GitHub Actions run ID;0=本地非 CI 环境,防跨 run 重放)"},
            )
        if not isinstance(self.run_attempt, int) or self.run_attempt < 0:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.run_attempt 必须为非负整数"
                                  "(GitHub Actions run attempt;0=本地非 CI 环境,防跨 attempt 重放)"},
            )
        if not self.audience:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.audience 必须非空"},
            )
        if not self.target_identity:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.target_identity 必须非空"
                                  "(恢复目标数据库 identity,独立来源)"},
            )
        if not self.target_uri:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.target_uri 必须非空"
                                  "(恢复目标 URI,独立来源)"},
            )
        if not self.manifest_digest or len(self.manifest_digest) != 64:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.manifest_digest 必须为 64 hex 字符"},
            )
        if not self.payload_digest or len(self.payload_digest) != 64:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.payload_digest 必须为 64 hex 字符"},
            )
        if not self.allowed_action:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.allowed_action 必须非空"},
            )
        if not self.nonce:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "R76 P0-05: RestoreOperationContext.nonce 必须非空"
                                  "(由 orchestrator 生成,独立于 capability 签发)"},
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict(用于持久化到 restore_operations 表)。"""
        return {
            "operation_id": self.operation_id,
            "backup_id": self.backup_id,
            "source_sha": self.source_sha,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "audience": self.audience,
            "target_identity": self.target_identity,
            "target_uri": self.target_uri,
            "manifest_digest": self.manifest_digest,
            "payload_digest": self.payload_digest,
            "allowed_action": self.allowed_action,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RestoreOperationContext":
        """从 dict 反序列化(从 restore_operations 表读取)。

        Args:
            data: 持久化的 context dict(由 to_dict 生成)

        Returns:
            RestoreOperationContext 实例

        Raises:
            ValueError: 任一必填字段缺失或类型错误
        """
        try:
            ctx = cls(
                operation_id=str(data["operation_id"]),
                backup_id=str(data["backup_id"]),
                source_sha=str(data["source_sha"]),
                run_id=int(data["run_id"]),
                run_attempt=int(data["run_attempt"]),
                audience=str(data["audience"]),
                target_identity=str(data["target_identity"]),
                target_uri=str(data["target_uri"]),
                manifest_digest=str(data["manifest_digest"]),
                payload_digest=str(data["payload_digest"]),
                # R76 P0-05: allowed_action 必填(不再提供默认值)
                allowed_action=str(data["allowed_action"]),
                nonce=str(data["nonce"]),
            )
        except KeyError as e:
            raise ValueError(
                f"R76 P0-05: RestoreOperationContext.from_dict 缺失必填字段: {e}"
            ) from e
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"R76 P0-05: RestoreOperationContext.from_dict 类型转换失败: {e}"
            ) from e
        ctx.validate()
        return ctx
