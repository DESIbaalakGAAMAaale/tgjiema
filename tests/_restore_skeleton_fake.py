"""R66 P0-06: tests-only fake — 保留 R65 之前的骨架行为用于向后兼容测试。

审计背景(R66 终审报告 P0-06):
    R65 P0-02/P0-03 整改时,``RestoreOrchestrator`` 仍保留生产可达的降级骨架:
      - ``backends: Optional[BackendRegistry] = None`` → 走骨架路径(touch 空文件 /
        占位符 switch)
      - ``approval_authority: Any = None`` / ``mfa_authority: Any = None``
        → 走旧不透明字符串 ID 比较路径

    R66 P0-06 整改已删除生产类的所有 Optional 降级骨架,生产类不得存在任何
    ``if X is None: <fallback>`` 降级分支。本模块复刻旧的骨架行为(touch 空文件 /
    占位符 switch / 旧 ID 比较),仅供 R64/R65 旧测试向后兼容使用。

⚠️ 本模块仅存在于 tests/,生产代码(services/、bots/、admin/)不得引用。
"""
from __future__ import annotations

import datetime as _dt
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

from services.error_codes import AppError, ErrorCodes
from services.restore_backends import BackendRegistry
from services.restore_orchestrator import (
    RestoreOperation,
    RestoreOrchestrator,
    RestorePhase,
    _DATASOURCE_ORDER,
)


class RestoreOrchestratorSkeletonFake(RestoreOrchestrator):
    """R66 P0-06: tests-only fake — 保留 R65 之前的骨架行为。

    本类继承 ``RestoreOrchestrator`` 但绕过构造时的必需依赖校验,
    允许 ``backends=None`` / ``approval_authority=None`` / ``mfa_authority=None``,
    并复刻旧的骨架行为:

      - ``provision_staging``: touch 空 SQLite 文件 + ``target_type="skeleton"``
      - ``execute_blue_green_switch``: 旧 ID 比较(opaque string comparison)+
        占位符 switch(无真实 backend 切换)

    其他方法(``restore_to_staging`` / ``validate_staging`` / ``rollback_operation``
    / ``fail_operation`` / ``_destroy_staging``)使用生产类实现 — 当
    ``self._backends`` 为空 ``BackendRegistry`` 时,生产代码会自然走"无 backend
    注册"分支(仅状态变更 / 默认 ok / unlink),与旧骨架行为一致。

    ⚠️ 本类仅存在于 tests/,生产代码不得引用。
    """

    def __init__(
        self,
        store: Any,
        *,
        staging_root: Optional[str] = None,
        rollback_ttl_seconds: int = 86400,
        clock: Optional[Callable[[], float]] = None,
        fault_hooks: Optional[dict[str, Any]] = None,
        backends: Optional[BackendRegistry] = None,
        approval_authority: Any = None,
        mfa_authority: Any = None,
    ) -> None:
        # R66 P0-06: 跳过生产类必需依赖校验(允许 None)
        # 不调用 super().__init__() — 直接初始化属性
        self._store = store
        self._staging_root: Path = Path(
            staging_root
            or os.environ.get("RESTORE_STAGING_ROOT")
            or _dt.datetime.now().strftime("/tmp/restore_staging_%Y%m%d")
        )
        self._rollback_ttl_seconds = rollback_ttl_seconds
        self._clock = clock or _dt.datetime.now
        # fault_hooks 仅用于测试故障注入
        self._fault_hooks: dict[str, Any] = dict(fault_hooks or {})
        # 内存缓存(_meta 等非持久化字段)
        self._operations: dict[str, RestoreOperation] = {}
        # 允许 backends=None;若提供则使用,否则用空 registry
        # (生产代码检查 `ds in self._backends` 返回 False,自然走骨架行为)
        self._backends: BackendRegistry = (
            backends if backends is not None else BackendRegistry()
        )
        # 允许 approval_authority / mfa_authority 为 None(旧 ID 比较路径)
        self._approval_authority = approval_authority
        self._mfa_authority = mfa_authority

    # ─── 骨架行为:provision_staging ──────────────────────────

    async def provision_staging(self, operation_id: str) -> dict[str, str]:
        """骨架行为:touch 空 SQLite 文件 + ``target_type='skeleton'``。

        复刻 R65 P0-02 之前的骨架 provision 行为:
          - crdb: 仅占位符 schema 名(target_type='skeleton')
          - sqlite / relay_sqlite: touch 空文件(target_type='skeleton')
        """
        operation = await self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.STAGING_PROVISION)
        # 触发故障注入(测试用)
        self._maybe_inject_fault("staging_provision", operation_id, None)

        staging_targets: dict[str, str] = {}
        staging_targets["crdb"] = (
            f"staging_restore_{operation_id.replace('-', '')[:16]}"
        )
        staging_targets["sqlite"] = str(
            self._staging_root / f"staging_cache_{operation_id}.db"
        )
        staging_targets["relay_sqlite"] = str(
            self._staging_root / f"staging_relay_{operation_id}.db"
        )
        # 确保 staging_root 存在
        self._staging_root.mkdir(parents=True, exist_ok=True)

        provision_results: dict[str, dict[str, Any]] = {}
        for datasource in _DATASOURCE_ORDER:
            try:
                self._maybe_inject_fault(
                    f"staging_provision.{datasource}", operation_id, datasource
                )
                # 骨架行为:touch 空 SQLite 文件(crdb 仅占位符名)
                if datasource in ("sqlite", "relay_sqlite"):
                    Path(staging_targets[datasource]).touch()
                provision_results[datasource] = {
                    "target": staging_targets[datasource],
                    "target_type": "skeleton",
                    "created_at": self._now_iso(),
                    "schema_fingerprint": "",
                }
            except Exception as e:
                # 任一数据源 provision 失败 → fail_operation
                await self.fail_operation(
                    operation_id,
                    reason=f"staging_provision_failed:{datasource}:{e}",
                )
                raise AppError(
                    ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                    params={
                        "operation_id": operation_id,
                        "datasource": datasource,
                        "reason": str(e),
                    },
                )

        # 更新 operation 状态:phase=STAGING_PROVISION
        new_ds_states = {
            **{k: v for k, v in operation.datasource_states.items()
               if not k.startswith("_")},
            "crdb": {
                "status": "provisioned",
                "target": staging_targets["crdb"],
                "provision_result": provision_results["crdb"],
            },
            "sqlite": {
                "status": "provisioned",
                "target": staging_targets["sqlite"],
                "provision_result": provision_results["sqlite"],
            },
            "relay_sqlite": {
                "status": "provisioned",
                "target": staging_targets["relay_sqlite"],
                "provision_result": provision_results["relay_sqlite"],
            },
            "_meta": operation.datasource_states.get("_meta", {}),
        }
        op = replace(
            operation,
            phase=RestorePhase.STAGING_PROVISION,
            datasource_states=new_ds_states,
            updated_at=self._now_iso(),
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "staging_provisioned",
            operation.phase, RestorePhase.STAGING_PROVISION,
        )
        self._operations[operation_id] = op
        return staging_targets

    # ─── 骨架行为:execute_blue_green_switch ──────────────────

    async def execute_blue_green_switch(
        self,
        operation_id: str,
        approval_id: str,
        mfa_receipt_id: str,
    ) -> str:
        """骨架行为:旧 ID 比较(opaque string comparison)+ 占位符 switch。

        复刻 R65 P0-03 之前的旧 ID 比较路径:
          - approval_id / mfa_receipt_id 作为不透明字符串与 request_approval 时记录的值比较
          - 不调用 ApprovalAuthority / MFAAuthority(可伪造 ID)
          - 不调用 backend.prepare_switch / commit_switch(占位符 active_pointer)
          - 仍持久化 rollback_target / operation phase / audit event / consume nonce
        """
        operation = await self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.BLUE_GREEN_SWITCH)

        # 旧路径:不透明字符串 ID 比较(可伪造 — 仅用于向后兼容测试)
        if not approval_id or approval_id != operation.approval_id:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "operation_id": operation_id,
                    "reason": "approval_id_mismatch",
                },
            )
        if not mfa_receipt_id or mfa_receipt_id != operation.mfa_receipt_id:
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={
                    "operation_id": operation_id,
                    "reason": "mfa_receipt_id_mismatch",
                },
            )

        # 故障注入(测试用)
        try:
            self._maybe_inject_fault("blue_green_switch", operation_id, None)
        except AppError:
            # 故障注入触发 — 调用 fail_operation 进入 FAILED 终态
            await self.fail_operation(
                operation_id, reason="blue_green_switch_fault_injected"
            )
            raise
        except Exception as e:
            await self.fail_operation(
                operation_id, reason=f"blue_green_switch_failed:{e}"
            )
            raise AppError(
                ErrorCodes.RESTORE_SWITCH_FAILED,
                params={"operation_id": operation_id, "reason": str(e)},
            )

        # 骨架行为:占位符 switch(无真实 backend 切换)
        switch_version = str(uuid.uuid4())
        previous_version = f"v_prev_{operation.backup_id}"
        active_pointer: dict[str, Any] = {
            "crdb": {"database": "active_crdb"},
            "sqlite": {"path": "active_cache_store.db"},
            "relay_sqlite": {"path": "active_relay_pool.db"},
        }

        # 持久化 rollback target(限时回滚点)
        await self._persist_rollback_target(
            operation_id=operation_id,
            switch_version=switch_version,
            active_pointer=active_pointer,
        )

        # 更新 operation 状态
        op = replace(
            operation,
            phase=RestorePhase.BLUE_GREEN_SWITCH,
            switch_version=switch_version,
            previous_version=previous_version,
            updated_at=self._now_iso(),
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "switched",
            operation.phase, RestorePhase.BLUE_GREEN_SWITCH,
            payload={
                "switch_version": switch_version,
                "previous_version": previous_version,
                "active_pointer": active_pointer,
            },
        )

        # UoW 退出后(独立 commit):consume nonce + transition to COMPLETED
        await self._consume_nonce(operation_id, op)
        await self._transition_to_completed(operation_id, op)
        return switch_version


__all__ = ["RestoreOrchestratorSkeletonFake"]
