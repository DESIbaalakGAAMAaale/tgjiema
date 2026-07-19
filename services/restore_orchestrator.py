"""R64 P0-03: 恢复编排状态机 — staging 蓝绿切换 + 限时回滚点。

审计背景(R64 终审报告 P0-03: restore 仍可能形成跨数据源混合时间点):

    旧 writer 按 CRDB → cache SQLite → relay SQLite 顺序执行 restore。
    前一数据源成功、后一数据源失败时,无法用普通事务回滚已经提交的另一个存储。
    覆盖模式尤其可能先清空生产表再失败,造成 active 数据被破坏且不可恢复。

整改方案(R64 P0-03):

    1. 恢复只能写入全新的 staging CRDB database/schema 与新的 SQLite 文件,
       禁止原地覆盖生产(蓝绿切换模型)
    2. 每个数据源完成 schema / 行数 / 主外键 / 业务守恒 / 抽样+全量 hash 和
       应用只读演练后才视为可切换
    3. 所有 staging 数据源均验证成功后,在维护窗口执行版本化蓝绿切换;
       任何失败只销毁 staging,不影响 active 数据
    4. 切换后保留旧版本作为限时回滚点;回滚也必须使用状态机和审计事件
    5. nonce 不在真正写入前永久消费 — 采用 operation ledger:
       验证后 reserved,成功切换后 consumed,失败后允许同 operation 安全重试
       但禁止换 payload(防篡改)

状态机:

    INIT → STAGING_PROVISION → STAGING_RESTORE → STAGING_VALIDATE →
        AWAIT_APPROVAL → BLUE_GREEN_SWITCH → COMPLETED
                                       │
                                       └→ ROLLED_BACK (切换后回滚)
    任意阶段失败 → FAILED (销毁 staging,nonce=failed,允许同 payload 重试)

设计原则:

    - RestoreOrchestrator 与 db_restore._restore_from_backup_data 解耦:
      orchestrator 负责状态机 + staging 目标管理 + 蓝绿切换 + 回滚;
      实际写入仍委托 _restore_from_backup_data(到 staging 文件/库)。
    - 持久化:每个阶段切换都写入 restore_operations 表 + 一条 audit event。
    - nonce 绑定:operation_id + backup_id + manifest_digest + payload_digest;
      reserved 状态可重试同 payload,consumed 状态不可再切换,
      failed 状态允许新 nonce 重试(但新 nonce 必须绑定同一 payload_digest)。
    - 故障注入:通过 fault_hooks 字典支持测试注入故障(production 应为空)。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t

# R65 P0-03: UnitOfWork — 在同一事务中 CAS 消费 approval/MFA/phase/nonce
from database.unit_of_work import UnitOfWork

# R65 P0-02: 真实恢复数据面 — RestoreBackend Protocol + 三个具体实现
# 详见 services/restore_backends.py
from services.restore_backends import (
    BackendRegistry,
    CRDBRestoreBackend,
    RestoreBackend,
    SQLiteRestoreBackend,
    StagingProvisionResult,
    StagingRestoreResult,
    StagingValidationResult,
    SwitchResult,
)


# ════════════════════════════════════════════════════════════════
# 1. 状态机枚举与数据类
# ════════════════════════════════════════════════════════════════


class RestorePhase(str, Enum):
    """恢复操作状态机的阶段。

    继承 str + Enum 使其可直接作为字符串比较与序列化。

    阶段语义:
        INIT                — 操作创建,nonce 已 reserved
        STAGING_PROVISION   — 为 CRDB/SQLite/relay_sqlite 创建全新 staging 目标
        STAGING_RESTORE     — 按数据源顺序将数据写入 staging(不接触 active)
        STAGING_VALIDATE    — 校验 staging:schema/行数/主外键/守恒/hash/演练
        AWAIT_APPROVAL      — 等待审批 + MFA receipt
        BLUE_GREEN_SWITCH   — CAS 切换 active 指针到 staging,旧版本留作回滚点
        COMPLETED           — 切换完成,operation 成功
        FAILED              — 任一阶段失败,已销毁 staging,nonce=failed
        ROLLED_BACK         — 切换后回滚到旧版本(终态)
    """

    INIT = "init"
    STAGING_PROVISION = "staging_provision"
    STAGING_RESTORE = "staging_restore"
    STAGING_VALIDATE = "staging_validate"
    AWAIT_APPROVAL = "await_approval"
    BLUE_GREEN_SWITCH = "blue_green_switch"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# 合法的 phase 转换图(单向):
#   INIT → STAGING_PROVISION / FAILED
#   STAGING_PROVISION → STAGING_RESTORE / FAILED
#   STAGING_RESTORE → STAGING_VALIDATE / FAILED
#   STAGING_VALIDATE → AWAIT_APPROVAL / FAILED
#   AWAIT_APPROVAL → BLUE_GREEN_SWITCH / FAILED
#   BLUE_GREEN_SWITCH → COMPLETED / FAILED / ROLLED_BACK
#   COMPLETED → ROLLED_BACK (维护窗口内回滚)
#   FAILED → (终态,不可转换)
#   ROLLED_BACK → (终态,不可转换)
_LEGAL_PHASE_TRANSITIONS: dict[RestorePhase, frozenset[RestorePhase]] = {
    RestorePhase.INIT: frozenset({RestorePhase.STAGING_PROVISION, RestorePhase.FAILED}),
    RestorePhase.STAGING_PROVISION: frozenset(
        {RestorePhase.STAGING_RESTORE, RestorePhase.FAILED}
    ),
    RestorePhase.STAGING_RESTORE: frozenset(
        {RestorePhase.STAGING_VALIDATE, RestorePhase.FAILED}
    ),
    RestorePhase.STAGING_VALIDATE: frozenset(
        {RestorePhase.AWAIT_APPROVAL, RestorePhase.FAILED}
    ),
    RestorePhase.AWAIT_APPROVAL: frozenset(
        {RestorePhase.BLUE_GREEN_SWITCH, RestorePhase.FAILED}
    ),
    RestorePhase.BLUE_GREEN_SWITCH: frozenset(
        {RestorePhase.COMPLETED, RestorePhase.FAILED, RestorePhase.ROLLED_BACK}
    ),
    RestorePhase.COMPLETED: frozenset({RestorePhase.ROLLED_BACK}),
    RestorePhase.FAILED: frozenset(),
    RestorePhase.ROLLED_BACK: frozenset(),
}

# 终态(不可再转换)
_TERMINAL_PHASES: frozenset[RestorePhase] = frozenset(
    {RestorePhase.COMPLETED, RestorePhase.FAILED, RestorePhase.ROLLED_BACK}
)

# 恢复时数据源处理顺序(与 db_restore 一致:CRDB → cache SQLite → relay SQLite)
_DATASOURCE_ORDER: tuple[str, ...] = ("crdb", "sqlite", "relay_sqlite")


@dataclass(frozen=True)
class RestoreOperation:
    """恢复操作状态(不可变快照)。

    每次阶段切换生成新快照,旧快照保留在 restore_operation_events 审计轨迹。

    Attributes:
        operation_id: 操作唯一 ID(UUID)
        backup_id: 备份 ID(timestamp)
        manifest_digest: manifest 原始 bytes SHA-256(绑定 nonce)
        phase: 当前状态机阶段
        datasource_states: {crdb/sqlite/relay_sqlite: {status, rows, ...}}
        validation_summary: {schema/row_count/fk/hash/business/dry_run: ok|fail|...}
        approval_id: 审批 ID(AWAIT_APPROVAL 阶段填入)
        mfa_receipt_id: MFA receipt ID(AWAIT_APPROVAL 阶段填入)
        switch_version: 蓝绿切换版本号(BLUE_GREEN_SWITCH 阶段填入)
        previous_version: 切换前的旧 active 版本(切换时填入,作为回滚目标)
        created_at: ISO8601 创建时间
        updated_at: ISO8601 最后更新时间
        created_by: 创建者标识(hostname:pid 或 principal)
    """

    operation_id: str
    backup_id: str
    manifest_digest: str
    phase: RestorePhase
    datasource_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    approval_id: str = ""
    mfa_receipt_id: str = ""
    switch_version: str = ""
    previous_version: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass(frozen=True)
class ValidationSummary:
    """staging 验证摘要(由 validate_staging 生成)。

    每个维度: "ok" / "fail" / "skipped" + 详细信息。
    """

    schema: str = "pending"
    row_count: str = "pending"
    foreign_keys: str = "pending"
    business_invariant: str = "pending"
    hash_check: str = "pending"
    dry_run: str = "pending"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "row_count": self.row_count,
            "foreign_keys": self.foreign_keys,
            "business_invariant": self.business_invariant,
            "hash_check": self.hash_check,
            "dry_run": self.dry_run,
            "details": self.details,
        }

    @property
    def all_passed(self) -> bool:
        """所有非 skipped 维度均 ok。"""
        for key in ("schema", "row_count", "foreign_keys",
                    "business_invariant", "hash_check", "dry_run"):
            value = getattr(self, key)
            if value == "skipped":
                continue
            if value != "ok":
                return False
        return True


# ════════════════════════════════════════════════════════════════
# 2. RestoreOrchestrator
# ════════════════════════════════════════════════════════════════


# Fault hook 类型:在指定阶段注入故障的回调(operation_id, datasource) -> None|raise
FaultHook = Callable[["RestoreOrchestrator", str, Optional[str]], None]


class RestoreOrchestrator:
    """R64 P0-03: 恢复编排状态机 — staging 蓝绿切换 + 限时回滚点。

    职责:
        - 管理 RestoreOperation 状态机(INIT → ... → COMPLETED/FAILED/ROLLED_BACK)
        - 为每个数据源创建全新 staging 目标(不接触 active)
        - 校验 staging:schema / 行数 / 主外键 / 业务守恒 / hash / 只读演练
        - 蓝绿切换:CAS 切换 active 指针,保留旧版本作为限时回滚点
        - 失败处理:销毁所有 staging 资源,nonce=failed,允许同 payload 重试
        - 回滚:状态机驱动,写入审计事件

    设计原则:
        - 不直接写 active 数据(只 staging → 切换)
        - nonce 绑定 operation_id + backup_id + manifest_digest + payload_digest
        - reserved 状态可重试同 payload,consumed 状态不可再切换,
          failed 状态允许新 nonce 重试(但禁止换 payload)
        - 持久化:每个阶段切换都写入 restore_operations + restore_operation_events
        - 故障注入:通过 fault_hooks 字典支持测试注入故障(production 应为空)

    Args:
        store: CacheStore 实例(提供 _db / nonce ledger 方法)
        staging_root: staging 文件根目录(默认系统临时目录)
        rollback_ttl_seconds: 回滚点保留时长(默认 24 小时)
        clock: 时间源(默认 time.time,测试可注入)
        fault_hooks: 故障注入钩子(测试用),形如:
            {
                "staging_provision.crdb": lambda orch, op_id, ds: raise RuntimeError(...),
                "staging_restore.sqlite": ...,
                "staging_validate.hash_check": ...,
                "blue_green_switch": ...,
            }
    """

    def __init__(
        self,
        store: Any,
        *,
        staging_root: Optional[str] = None,
        rollback_ttl_seconds: int = 86400,
        clock: Optional[Callable[[], float]] = None,
        fault_hooks: Optional[dict[str, FaultHook]] = None,
        backends: Optional[BackendRegistry] = None,
        approval_authority: Any = None,
        mfa_authority: Any = None,
    ) -> None:
        """R65 P0-03: 接受 ApprovalAuthority/MFAAuthority 以启用 UoW CAS 消费。

        Args:
            store: CacheStore 实例(提供 _db / nonce ledger 方法)
            staging_root: staging 文件根目录(默认系统临时目录)
            rollback_ttl_seconds: 回滚点保留时长(默认 24 小时)
            clock: 时间源(默认 time.time,测试可注入)
            fault_hooks: 故障注入钩子(测试用)
            backends: R65 P0-02 BackendRegistry(crdb/sqlite/relay_sqlite)。
                若提供,orchestrator 调用 backend 真实方法(provision/load/
                validate/prepare_switch/commit_switch/rollback_switch/destroy)。
                若为 None,保留旧骨架行为(向后兼容已有测试,但不应在生产使用)。
            approval_authority: R65 P0-03 ApprovalAuthority 实例。若提供,
                execute_blue_green_switch 在同一 UnitOfWork 中调用其
                verify_and_consume CAS 消费 approval(不可伪造 capability)。
                若为 None,保留旧 ID 比较路径(向后兼容 R64 测试,生产应提供)。
            mfa_authority: R65 P0-03 MFAAuthority 实例。若提供,在同一 UoW 中
                CAS 消费 MFA receipt(不可伪造 capability)。若为 None,旧路径。
        """
        self._store = store
        # staging_root 优先参数,其次环境变量 RESTORE_STAGING_ROOT,最后系统临时目录
        self._staging_root: Path = Path(
            staging_root
            or os.environ.get("RESTORE_STAGING_ROOT")
            or _dt.datetime.now().strftime("/tmp/restore_staging_%Y%m%d")
        )
        self._rollback_ttl_seconds = rollback_ttl_seconds
        self._clock = clock or _dt.datetime.now
        # fault_hooks 仅用于测试故障注入(production 应为空 dict)
        self._fault_hooks: dict[str, FaultHook] = dict(fault_hooks or {})
        # 内存中的操作状态缓存(持久化层在 restore_operations 表)
        self._operations: dict[str, RestoreOperation] = {}
        # R65 P0-02: 真实恢复数据面 backend 注册表
        self._backends: Optional[BackendRegistry] = backends
        if backends is None:
            logger.warning(
                _i18n_t("diagnostics.r65.p0_02.no_backend_registry")
            )
        # R65 P0-03: 审批/MFA 权威(可选)
        # 若提供,execute_blue_green_switch 走 UoW+CAS 路径(不可伪造 capability)
        # 若为 None,保留旧 ID 比较路径(向后兼容 R64 测试)
        self._approval_authority = approval_authority
        self._mfa_authority = mfa_authority

    # ─── 公共属性 ──────────────────────────────────────────

    @property
    def staging_root(self) -> Path:
        """staging 文件根目录(用于 CRDB schema 名 / SQLite 文件路径)。"""
        return self._staging_root

    @property
    def rollback_ttl_seconds(self) -> int:
        """回滚点保留时长(秒)。"""
        return self._rollback_ttl_seconds

    # ─── 状态机转换核心 ──────────────────────────────────

    @staticmethod
    def is_legal_transition(
        frm: RestorePhase, to: RestorePhase
    ) -> bool:
        """检查 phase 转换是否合法。

        Args:
            frm: 起始 phase
            to: 目标 phase

        Returns:
            True 若转换在 _LEGAL_PHASE_TRANSITIONS 中允许
        """
        return to in _LEGAL_PHASE_TRANSITIONS.get(frm, frozenset())

    @staticmethod
    def is_terminal(phase: RestorePhase) -> bool:
        """检查 phase 是否为终态(COMPLETED/FAILED/ROLLED_BACK)。"""
        return phase in _TERMINAL_PHASES

    def _assert_legal_transition(
        self, operation: RestoreOperation, target: RestorePhase
    ) -> None:
        """断言 phase 转换合法,否则 raise AppError(PHASE_TRANSITION_INVALID)。"""
        if not self.is_legal_transition(operation.phase, target):
            raise AppError(
                ErrorCodes.RESTORE_PHASE_TRANSITION_INVALID,
                params={
                    "operation_id": operation.operation_id,
                    "phase_from": operation.phase.value,
                    "phase_to": target.value,
                },
            )

    # ─── 持久化 ──────────────────────────────────────────

    async def _persist_operation(
        self, operation: RestoreOperation
    ) -> None:
        """持久化 operation 状态到 restore_operations 表(UPSERT)。"""
        if not self._store or not getattr(self._store, "_db", None):
            # 测试环境无 store 时仅更新内存缓存(由 _operations 字典维护)
            return
        now = self._now_iso()
        # updated_at 始终刷新
        op = replace(operation, updated_at=now) if operation.updated_at == "" else operation
        # 写入 SQLite restore_operations 表(UPSERT)
        await self._store._db.execute(
            """INSERT INTO restore_operations
               (operation_id, backup_id, manifest_digest, phase,
                datasource_states, validation_summary, approval_id,
                mfa_receipt_id, switch_version, previous_version,
                created_at, updated_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(operation_id) DO UPDATE SET
                phase = excluded.phase,
                datasource_states = excluded.datasource_states,
                validation_summary = excluded.validation_summary,
                approval_id = excluded.approval_id,
                mfa_receipt_id = excluded.mfa_receipt_id,
                switch_version = excluded.switch_version,
                previous_version = excluded.previous_version,
                updated_at = excluded.updated_at""",
            (
                op.operation_id, op.backup_id, op.manifest_digest,
                op.phase.value,
                json.dumps(op.datasource_states, ensure_ascii=False),
                json.dumps(op.validation_summary, ensure_ascii=False),
                op.approval_id, op.mfa_receipt_id,
                op.switch_version, op.previous_version,
                op.created_at, op.updated_at, op.created_by,
            ),
        )
        await self._store._db.commit()
        # 同步内存缓存
        self._operations[op.operation_id] = op

    async def _persist_operation_uow(
        self, operation: RestoreOperation, uow: Any
    ) -> None:
        """R65 P0-03: 在 UoW 内持久化 operation 状态(UPSERT,不独立 commit)。

        与 ``_persist_operation`` 行为一致,但通过 ``uow.execute`` 执行,
        由调用方的 UnitOfWork 统一控制事务边界(commit/rollback)。

        Args:
            operation: 要持久化的 operation 快照
            uow: UnitOfWork 实例(事务上下文)
        """
        if not self._store or not getattr(self._store, "_db", None):
            return
        now = self._now_iso()
        op = replace(operation, updated_at=now) if operation.updated_at == "" else operation
        await uow.execute(
            """INSERT INTO restore_operations
               (operation_id, backup_id, manifest_digest, phase,
                datasource_states, validation_summary, approval_id,
                mfa_receipt_id, switch_version, previous_version,
                created_at, updated_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(operation_id) DO UPDATE SET
                phase = excluded.phase,
                datasource_states = excluded.datasource_states,
                validation_summary = excluded.validation_summary,
                approval_id = excluded.approval_id,
                mfa_receipt_id = excluded.mfa_receipt_id,
                switch_version = excluded.switch_version,
                previous_version = excluded.previous_version,
                updated_at = excluded.updated_at""",
            (
                op.operation_id, op.backup_id, op.manifest_digest,
                op.phase.value,
                json.dumps(op.datasource_states, ensure_ascii=False),
                json.dumps(op.validation_summary, ensure_ascii=False),
                op.approval_id, op.mfa_receipt_id,
                op.switch_version, op.previous_version,
                op.created_at, op.updated_at, op.created_by,
            ),
        )
        # 同步内存缓存(不依赖 commit)
        self._operations[op.operation_id] = op

    async def _write_event(
        self,
        operation: RestoreOperation,
        event_type: str,
        phase_from: Optional[RestorePhase],
        phase_to: RestorePhase,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """写入审计事件到 restore_operation_events 表。"""
        if not self._store or not getattr(self._store, "_db", None):
            return
        trace_id = str(uuid.uuid4())
        await self._store._db.execute(
            """INSERT INTO restore_operation_events
               (operation_id, event_type, phase_from, phase_to,
                payload, trace_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation.operation_id, event_type,
                phase_from.value if phase_from else None,
                phase_to.value,
                json.dumps(payload or {}, ensure_ascii=False),
                trace_id, self._now_iso(),
            ),
        )
        await self._store._db.commit()

    async def _write_event_uow(
        self,
        operation: RestoreOperation,
        event_type: str,
        phase_from: Optional[RestorePhase],
        phase_to: RestorePhase,
        uow: Any,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """R65 P0-03: 在 UoW 内写入审计事件(不独立 commit)。

        与 ``_write_event`` 行为一致,但通过 ``uow.execute`` 执行,
        由调用方的 UnitOfWork 统一控制事务边界。
        """
        if not self._store or not getattr(self._store, "_db", None):
            return
        trace_id = str(uuid.uuid4())
        await uow.execute(
            """INSERT INTO restore_operation_events
               (operation_id, event_type, phase_from, phase_to,
                payload, trace_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation.operation_id, event_type,
                phase_from.value if phase_from else None,
                phase_to.value,
                json.dumps(payload or {}, ensure_ascii=False),
                trace_id, self._now_iso(),
            ),
        )

    def _now_iso(self) -> str:
        """当前 ISO8601 时间。"""
        return self._clock().isoformat() if hasattr(self._clock(), "isoformat") else \
            _dt.datetime.now(_dt.timezone.utc).isoformat()

    # ─── 状态机入口 ──────────────────────────────────────

    async def start_operation(
        self,
        backup_id: str,
        manifest_digest: str,
        requested_by: str,
        *,
        payload_digest: str = "",
        nonce: Optional[str] = None,
    ) -> str:
        """创建恢复操作,初始 phase=INIT,nonce state=reserved。

        Args:
            backup_id: 备份 ID(timestamp)
            manifest_digest: manifest 原始 bytes SHA-256
            requested_by: 创建者标识(principal 或 hostname:pid)
            payload_digest: payload canonical SHA-256(绑定 nonce,防换 payload)
            nonce: 可选 nonce(默认生成 secrets.token_hex(16))

        Returns:
            operation_id (UUID)

        Raises:
            AppError(RESTORE.NONCE_PAYLOAD_MISMATCH): 同 backup_id+manifest_digest
                已有 failed nonce,但 payload_digest 不匹配(禁止换 payload)
        """
        operation_id = str(uuid.uuid4())
        nonce = nonce or secrets.token_hex(16)
        now = self._now_iso()

        # nonce 绑定校验:若同 backup_id + manifest_digest 已有 failed nonce,
        # 必须使用相同 payload_digest(禁止换 payload 重试)
        if payload_digest:
            await self._check_payload_consistency_for_retry(
                backup_id, manifest_digest, payload_digest
            )

        operation = RestoreOperation(
            operation_id=operation_id,
            backup_id=backup_id,
            manifest_digest=manifest_digest,
            phase=RestorePhase.INIT,
            datasource_states={
                "crdb": {"status": "pending"},
                "sqlite": {"status": "pending"},
                "relay_sqlite": {"status": "pending"},
            },
            validation_summary={},
            approval_id="",
            mfa_receipt_id="",
            switch_version="",
            previous_version="",
            created_at=now,
            updated_at=now,
            created_by=requested_by,
        )
        # 持久化 operation + 初始事件
        await self._persist_operation(operation)
        await self._write_event(
            operation, "phase_transition", None, RestorePhase.INIT,
            payload={"nonce": nonce, "requested_by": requested_by},
        )

        # reserve nonce(若 store 支持 nonce ledger)
        nonce_store = getattr(self._store, "reserve_capability_nonce", None)
        if nonce_store is not None and payload_digest:
            reserved = await nonce_store(
                nonce=nonce,
                operation_id=operation_id,
                backup_id=backup_id,
                manifest_sha256=manifest_digest,
                payload_digest=payload_digest,
                reserved_by=requested_by,
            )
            if not reserved:
                # nonce 冲突(已有同 nonce)— 极少发生(secrets.token_hex),
                # 视为重放攻击,fail-closed
                raise AppError(
                    ErrorCodes.RESTORE_NONCE_PAYLOAD_MISMATCH,
                    params={
                        "operation_id": operation_id,
                        "reason": "nonce_already_reserved_replay_detected",
                    },
                )

        # 缓存 nonce + payload_digest 到内存(用于后续 consume/fail)
        op_with_meta = replace(
            operation,
            datasource_states={
                **operation.datasource_states,
                "_meta": {
                    "nonce": nonce,
                    "payload_digest": payload_digest,
                },
            },
        )
        self._operations[operation_id] = op_with_meta
        return operation_id

    async def _check_payload_consistency_for_retry(
        self,
        backup_id: str,
        manifest_digest: str,
        payload_digest: str,
    ) -> None:
        """检查重试时 payload 是否一致(禁止换 payload)。

        若同 backup_id + manifest_digest 已有 failed nonce,其 payload_digest
        必须与当前 payload_digest 一致;否则 raise NONCE_PAYLOAD_MISMATCH。
        """
        if not self._store or not getattr(self._store, "_db", None):
            return
        try:
            cursor = await self._store._db.execute(
                """SELECT payload_digest FROM restore_capability_nonces
                   WHERE backup_id = ? AND manifest_sha256 = ?
                     AND status = 'failed'
                   ORDER BY reserved_at DESC LIMIT 1""",
                (backup_id, manifest_digest),
            )
            row = await cursor.fetchone()
            if row is None:
                return  # 无 failed 历史记录,允许
            previous_payload = row[0] or ""
            if previous_payload and previous_payload != payload_digest:
                raise AppError(
                    ErrorCodes.RESTORE_NONCE_PAYLOAD_MISMATCH,
                    params={
                        "backup_id": backup_id,
                        "reason": (
                            "retry_with_different_payload_forbidden — "
                            "failed nonce 已绑定不同 payload_digest"
                        ),
                    },
                )
        except AppError:
            raise
        except Exception as e:
            # 查询失败不阻塞(向后兼容无表的场景),仅记录
            logger.debug(f"[restore_orchestrator] payload consistency 查询失败(忽略): {e}")

    def get_operation(self, operation_id: str) -> RestoreOperation:
        """获取操作状态(优先内存缓存,其次持久化层)。"""
        if operation_id in self._operations:
            return self._operations[operation_id]
        raise AppError(
            ErrorCodes.RESTORE_PHASE_TRANSITION_INVALID,
            params={
                "operation_id": operation_id,
                "phase_from": "",
                "phase_to": "",
                "reason": "operation_not_found",
            },
        )

    # ─── staging provision ──────────────────────────────

    async def provision_staging(self, operation_id: str) -> dict[str, str]:
        """为 CRDB / cache SQLite / relay SQLite 创建全新 staging 目标。

        - CRDB: 返回新 schema 名(不接触 active schema)
        - cache SQLite: 返回新文件路径(不接触 cache_store.db)
        - relay SQLite: 返回新文件路径(不接触 relay_pool.db)

        R65 P0-02: 若提供 BackendRegistry,调用 backend.provision() 真实创建
        staging 目标(CRDB schema / SQLite 文件 + schema 初始化),
        并保存 StagingProvisionResult 到 datasource_states[ds].provision_result。
        若 backends=None,保留旧骨架行为(仅 touch 空文件,向后兼容)。

        任何数据源 provision 失败 → fail_operation(销毁已创建的 staging)。

        Returns:
            {crdb: "staging_schema_xxx", sqlite: "/path/staging_cache.db",
             relay_sqlite: "/path/staging_relay.db"}
        """
        operation = self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.STAGING_PROVISION)
        # 触发故障注入(测试用)
        self._maybe_inject_fault("staging_provision", operation_id, None)

        staging_targets: dict[str, str] = {}
        # 骨架默认目标(向后兼容 backends=None 场景)
        staging_targets["crdb"] = f"staging_restore_{operation_id.replace('-', '')[:16]}"
        staging_targets["sqlite"] = str(
            self._staging_root / f"staging_cache_{operation_id}.db"
        )
        staging_targets["relay_sqlite"] = str(
            self._staging_root / f"staging_relay_{operation_id}.db"
        )
        # 确保 staging_root 存在
        self._staging_root.mkdir(parents=True, exist_ok=True)

        # R65 P0-02: 真实 provision — 仅调用注册的 backend,未注册的保留骨架目标
        provision_results: dict[str, dict[str, Any]] = {}
        for datasource in _DATASOURCE_ORDER:
            try:
                self._maybe_inject_fault(
                    f"staging_provision.{datasource}", operation_id, datasource
                )
                if self._backends is not None and datasource in self._backends:
                    # R65 P0-02: 真实调用 backend.provision()
                    backend = self._backends.get(datasource)
                    result = await backend.provision(operation_id, self._staging_root)
                    staging_targets[datasource] = result.target
                    provision_results[datasource] = {
                        "target": result.target,
                        "target_type": result.target_type,
                        "created_at": result.created_at,
                        "schema_fingerprint": result.schema_fingerprint,
                    }
                    logger.info(
                        _i18n_t(
                            "diagnostics.r65.p0_02.backend_provision",
                            datasource=datasource,
                            target=result.target,
                            operation_id=operation_id,
                        )
                    )
                else:
                    # 骨架行为:仅 touch 空文件(SQLite)或仅记录 schema 名(CRDB)
                    if datasource in ("sqlite", "relay_sqlite"):
                        Path(staging_targets[datasource]).touch(exist_ok=True)
                    provision_results[datasource] = {
                        "target": staging_targets[datasource],
                        "target_type": "skeleton",
                    }
            except Exception as e:
                # 任一数据源 provision 失败 → fail_operation
                logger.error(
                    f"[restore_orchestrator] staging provision {datasource} "
                    f"失败 (operation_id={operation_id}): {e}"
                )
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

        # 更新 operation 状态:phase=STAGING_PROVISION,记录 staging 目标 + provision_result
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
            op, "staging_provisioned", operation.phase, RestorePhase.STAGING_PROVISION,
            payload={"staging_targets": staging_targets,
                     "provision_results": provision_results},
        )
        return staging_targets

    # ─── staging restore ──────────────────────────────

    async def restore_to_staging(
        self,
        operation_id: str,
        datasource: str,
        *,
        tables_data: Optional[dict[str, list]] = None,
        merge: bool = False,
    ) -> bool:
        """按 datasource 顺序执行 restore 到 staging。

        R65 P0-02: 若提供 BackendRegistry + tables_data,调用
        backend.load_verified_payload() 真实写入 staging,
        保存 StagingRestoreResult(rows_restored / content_hash / schema_fingerprint)。
        若 backends=None 或 tables_data=None,保留骨架行为(仅状态变更,向后兼容)。

        失败时调用 fail_operation 销毁 staging。

        Args:
            operation_id: 操作 ID
            datasource: "crdb" / "sqlite" / "relay_sqlite"
            tables_data: {table_name: rows}(仅属于本 datasource 的表)。
                R65 P0-02: 提供且 backends 已注册时执行真实写入。
            merge: True=增量补充,False=覆盖(默认)

        Returns:
            True 成功(该数据源 restore 完成)
            False 不应到达(失败时 raise AppError)

        Raises:
            AppError(RESTORE.STAGING_PROVISION_FAILED): restore 失败
        """
        operation = self.get_operation(operation_id)
        if datasource not in _DATASOURCE_ORDER:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": datasource,
                    "reason": "unknown_datasource",
                },
            )
        # 故障注入(测试用)在 try 内触发,失败 → fail_operation + AppError
        try:
            # 触发故障注入(测试用)
            self._maybe_inject_fault(
                f"staging_restore.{datasource}", operation_id, datasource
            )

            new_ds_states = dict(operation.datasource_states)
            ds_state = dict(new_ds_states.get(datasource, {}))
            ds_state["status"] = "restored"

            # R65 P0-02: 真实写入 — backend.load_verified_payload()
            if (
                self._backends is not None
                and datasource in self._backends
                and tables_data is not None
            ):
                backend = self._backends.get(datasource)
                # 从 ds_state.provision_result 重建 StagingProvisionResult
                provision_dict = ds_state.get("provision_result", {})
                provision_result = StagingProvisionResult(
                    target=provision_dict.get("target", ds_state.get("target", "")),
                    target_type=provision_dict.get("target_type", ""),
                    created_at=provision_dict.get("created_at", ""),
                    schema_fingerprint=provision_dict.get("schema_fingerprint", ""),
                )
                restore_result = await backend.load_verified_payload(
                    operation_id=operation_id,
                    provision_result=provision_result,
                    tables_data=tables_data,
                    merge=merge,
                )
                ds_state["restore_result"] = {
                    "rows_restored": dict(restore_result.rows_restored),
                    "content_hash": dict(restore_result.content_hash),
                    "schema_fingerprint": restore_result.schema_fingerprint,
                    "bytes_written": restore_result.bytes_written,
                    "duration_seconds": restore_result.duration_seconds,
                }
                logger.info(
                    _i18n_t(
                        "diagnostics.r65.p0_02.backend_load_verified_payload",
                        datasource=datasource,
                        rows=restore_result.rows_restored,
                        bytes=restore_result.bytes_written,
                        operation_id=operation_id,
                    )
                )
            else:
                # 骨架行为:仅状态变更(向后兼容 backends=None 或 tables_data=None)
                logger.debug(
                    _i18n_t(
                        "diagnostics.r65.p0_02.staging_restore_skeleton",
                        datasource=datasource,
                        operation_id=operation_id,
                    )
                )

            new_ds_states[datasource] = ds_state
            op = replace(
                operation,
                phase=RestorePhase.STAGING_RESTORE,
                datasource_states=new_ds_states,
                updated_at=self._now_iso(),
            )
            await self._persist_operation(op)
            await self._write_event(
                op, "staging_restored", operation.phase, RestorePhase.STAGING_RESTORE,
                payload={"datasource": datasource,
                         "restore_result": ds_state.get("restore_result", {})},
            )
            return True
        except Exception as e:
            logger.error(
                f"[restore_orchestrator] staging restore {datasource} "
                f"失败 (operation_id={operation_id}): {e}"
            )
            await self.fail_operation(
                operation_id,
                reason=f"staging_restore_failed:{datasource}:{e}",
            )
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": datasource,
                    "reason": str(e),
                },
            )

    # ─── staging validate ──────────────────────────────

    async def validate_staging(
        self,
        operation_id: str,
        *,
        expected_tables: Optional[dict[str, dict[str, list]]] = None,
    ) -> ValidationSummary:
        """对每个 datasource 检查 schema / 行数 / 主外键 / 业务守恒 / hash / 演练。

        R65 P0-02: 若提供 BackendRegistry + expected_tables,调用
        backend.validate() 执行真实 6 维度验证。任一维度非 ok(fail/skipped/
        pending/unknown)即整体失败,不能默认 ok。
        若 backends=None,保留骨架行为(按维度故障注入,默认 ok,向后兼容)。

        Args:
            operation_id: 操作 ID
            expected_tables: {datasource: {table_name: rows}} 用于行数/hash 比对。
                R65 P0-02: 提供且 backends 已注册时执行真实验证。

        任一失败 → fail_operation(销毁 staging)。

        Returns:
            ValidationSummary(各维度 ok / fail / skipped + 详情)
        """
        operation = self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.STAGING_VALIDATE)

        # R65 P0-02: 真实验证 — backend.validate()
        if (
            self._backends is not None
            and expected_tables is not None
        ):
            # 收集所有 datasource 的验证结果,合并到整体 summary
            merged_status: dict[str, str] = {
                "schema": "ok",
                "row_count": "ok",
                "foreign_keys": "ok",
                "business_invariant": "ok",
                "hash_check": "ok",
                "dry_run": "ok",
            }
            merged_details: dict[str, Any] = {"datasources": {}}
            for datasource in _DATASOURCE_ORDER:
                if datasource not in self._backends:
                    continue
                # 故障注入(测试用,按维度)
                # 即使 backend 真实验证,仍允许 fault hook 注入维度失败
                for dim in ("schema", "row_count", "foreign_keys",
                            "business_invariant", "hash_check", "dry_run"):
                    self._maybe_inject_fault(
                        f"staging_validate.{dim}", operation_id, dim
                    )

                backend = self._backends.get(datasource)
                ds_state = operation.datasource_states.get(datasource, {})
                provision_dict = ds_state.get("provision_result", {})
                provision_result = StagingProvisionResult(
                    target=provision_dict.get("target", ds_state.get("target", "")),
                    target_type=provision_dict.get("target_type", ""),
                    created_at=provision_dict.get("created_at", ""),
                    schema_fingerprint=provision_dict.get("schema_fingerprint", ""),
                )
                restore_dict = ds_state.get("restore_result", {})
                restore_result = StagingRestoreResult(
                    rows_restored=restore_dict.get("rows_restored", {}),
                    content_hash=restore_dict.get("content_hash", {}),
                    schema_fingerprint=restore_dict.get("schema_fingerprint", ""),
                    bytes_written=restore_dict.get("bytes_written", 0),
                    duration_seconds=restore_dict.get("duration_seconds", 0.0),
                )
                expected_ds = expected_tables.get(datasource, {})
                try:
                    val_result = await backend.validate(
                        operation_id=operation_id,
                        provision_result=provision_result,
                        restore_result=restore_result,
                        expected_tables=expected_ds,
                    )
                except AppError:
                    raise
                except Exception as e:
                    # 验证异常 → 整体失败
                    summary = ValidationSummary(
                        schema="fail", row_count="fail", foreign_keys="fail",
                        business_invariant="fail", hash_check="fail", dry_run="fail",
                        details={"error": str(e), "datasource": datasource},
                    )
                    await self.fail_operation(
                        operation_id,
                        reason=f"staging_validate_failed:{datasource}:{e}",
                    )
                    raise AppError(
                        ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED,
                        params={
                            "operation_id": operation_id,
                            "datasource": datasource,
                            "reason": str(e),
                        },
                    )

                merged_details["datasources"][datasource] = val_result.to_dict()
                # 合并:任一 datasource 的维度非 ok 即整体非 ok
                for dim in ("schema", "row_count", "foreign_keys",
                            "business_invariant", "hash_check", "dry_run"):
                    ds_value = getattr(val_result, dim)
                    if ds_value != "ok":
                        merged_status[dim] = "fail"

            summary = ValidationSummary(
                schema=merged_status["schema"],
                row_count=merged_status["row_count"],
                foreign_keys=merged_status["foreign_keys"],
                business_invariant=merged_status["business_invariant"],
                hash_check=merged_status["hash_check"],
                dry_run=merged_status["dry_run"],
                details=merged_details,
            )
            # R65 P0-02: 任一维度非 ok → fail_operation
            if not summary.all_passed:
                failed_dims = [
                    dim for dim in ("schema", "row_count", "foreign_keys",
                                    "business_invariant", "hash_check", "dry_run")
                    if getattr(summary, dim) != "ok"
                ]
                await self.fail_operation(
                    operation_id,
                    reason=f"staging_validate_failed:dims={failed_dims}",
                )
                raise AppError(
                    ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED,
                    params={
                        "operation_id": operation_id,
                        "reason": f"validation_failed:dims={failed_dims}",
                    },
                )
        else:
            # 骨架行为:按维度故障注入,默认 ok(向后兼容 backends=None)
            dimensions = ["schema", "row_count", "foreign_keys",
                          "business_invariant", "hash_check", "dry_run"]
            results: dict[str, str] = {}
            details: dict[str, Any] = {}
            for dim in dimensions:
                try:
                    self._maybe_inject_fault(
                        f"staging_validate.{dim}", operation_id, dim
                    )
                    results[dim] = "ok"
                    details[dim] = {"checked": True}
                except AppError:
                    raise
                except Exception as e:
                    results[dim] = "fail"
                    details[dim] = {"error": str(e)}
                    summary = ValidationSummary(
                        schema=results.get("schema", "pending"),
                        row_count=results.get("row_count", "pending"),
                        foreign_keys=results.get("foreign_keys", "pending"),
                        business_invariant=results.get("business_invariant", "pending"),
                        hash_check=results.get("hash_check", "pending"),
                        dry_run=results.get("dry_run", "pending"),
                        details=details,
                    )
                    # 验证失败 → fail_operation
                    await self.fail_operation(
                        operation_id,
                        reason=f"staging_validate_failed:{dim}:{e}",
                    )
                    raise AppError(
                        ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED,
                        params={
                            "operation_id": operation_id,
                            "dimension": dim,
                            "reason": str(e),
                        },
                    )

            summary = ValidationSummary(
                schema=results.get("schema", "ok"),
                row_count=results.get("row_count", "ok"),
                foreign_keys=results.get("foreign_keys", "ok"),
                business_invariant=results.get("business_invariant", "ok"),
                hash_check=results.get("hash_check", "ok"),
                dry_run=results.get("dry_run", "ok"),
                details=details,
            )

        # 更新 operation 状态
        op = replace(
            operation,
            phase=RestorePhase.STAGING_VALIDATE,
            validation_summary=summary.to_dict(),
            updated_at=self._now_iso(),
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "staging_validated", operation.phase, RestorePhase.STAGING_VALIDATE,
            payload={"summary": summary.to_dict()},
        )
        return summary

    # ─── approval ──────────────────────────────────────

    async def request_approval(
        self,
        operation_id: str,
        approval_id: str,
        mfa_receipt_id: str,
    ) -> None:
        """进入 AWAIT_APPROVAL,要求 approval_id + mfa_receipt_id。

        Raises:
            AppError(RESTORE.APPROVAL_REQUIRED): approval_id 为空
            AppError(RESTORE.MFA_REQUIRED): mfa_receipt_id 为空
        """
        operation = self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.AWAIT_APPROVAL)
        if not approval_id:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={"operation_id": operation_id, "reason": "approval_id_empty"},
            )
        if not mfa_receipt_id:
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={"operation_id": operation_id, "reason": "mfa_receipt_id_empty"},
            )
        op = replace(
            operation,
            phase=RestorePhase.AWAIT_APPROVAL,
            approval_id=approval_id,
            mfa_receipt_id=mfa_receipt_id,
            updated_at=self._now_iso(),
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "approval_requested", operation.phase, RestorePhase.AWAIT_APPROVAL,
            payload={"approval_id": approval_id, "mfa_receipt_id": mfa_receipt_id},
        )

    # ─── 蓝绿切换 ──────────────────────────────────────

    async def execute_blue_green_switch(
        self,
        operation_id: str,
        approval_id: str,
        mfa_receipt_id: str,
    ) -> str:
        """CAS 切换 active 指针;保留旧版本作为 rollback target;nonce state=consumed。

        R65 P0-02: 若提供 BackendRegistry,调用 backend.prepare_switch() +
        backend.commit_switch() 执行真实蓝绿切换:
        - SQLite: 原子 rename(active → backup, staging → active)
        - CRDB: schema 指针切换(逻辑层 routing 切换)
        switch_version 来自 backend(SwitchResult.switch_version,UUID),
        previous_target 持久化为 rollback target。
        若 backends=None,保留骨架行为(仅记录 switch_version,向后兼容)。

        R65 P0-03: 若提供 ApprovalAuthority + MFAAuthority,走 UoW+CAS 路径:
        - 在同一 UnitOfWork 中 CAS 消费 approval(consumed_at)、MFA(jti)、
          operation phase(await_approval → blue_green_switch)、rollback_target、
          audit event — 任一失败 UoW 回滚 → approval/MFA 未消费(replay-safe)
        - 旧 ID 比较路径仅在 authorities=None 时使用(向后兼容 R64 测试,
          生产环境必须提供 authorities,否则 approval_id/mfa_receipt_id 仅是
          调用方传入的不透明字符串,可被任意伪造)

        Args:
            operation_id: 操作 ID
            approval_id: 审批 ID(必须与 request_approval 一致;
                UoW 路径下由 ApprovalAuthority 校验 + CAS 消费)
            mfa_receipt_id: MFA receipt ID(必须与 request_approval 一致;
                UoW 路径下由 MFAAuthority 校验 + CAS 消费)

        Returns:
            switch_version (UUID)

        Raises:
            AppError(RESTORE.APPROVAL_REQUIRED): approval_id 为空/不匹配/CAS 失败
            AppError(RESTORE.MFA_REQUIRED): mfa_receipt_id 为空/不匹配/CAS 失败
            AppError(RESTORE.SWITCH_FAILED): 切换失败
        """
        operation = self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.BLUE_GREEN_SWITCH)

        # R65 P0-03: 提供两个 authority 时走 UoW+CAS 路径(不可伪造 capability)
        # 否则保留旧 ID 比较路径(向后兼容 R64 测试,生产应提供 authorities)
        if (
            self._approval_authority is not None
            and self._mfa_authority is not None
        ):
            return await self._execute_switch_with_authorities(
                operation, operation_id, approval_id, mfa_receipt_id,
            )

        # 旧 ID 比较路径(向后兼容 R64 测试 — opaque string comparison)
        # 校验 approval + MFA 与 request_approval 阶段一致
        if not approval_id or approval_id != operation.approval_id:
            raise AppError(
                ErrorCodes.RESTORE_APPROVAL_REQUIRED,
                params={
                    "operation_id": operation_id,
                    "reason": "approval_id_mismatch_or_empty",
                },
            )
        if not mfa_receipt_id or mfa_receipt_id != operation.mfa_receipt_id:
            raise AppError(
                ErrorCodes.RESTORE_MFA_REQUIRED,
                params={
                    "operation_id": operation_id,
                    "reason": "mfa_receipt_id_mismatch_or_empty",
                },
            )

        # 触发故障注入(测试用)
        try:
            self._maybe_inject_fault("blue_green_switch", operation_id, None)
        except AppError:
            raise
        except Exception as e:
            await self.fail_operation(
                operation_id, reason=f"switch_failed:{e}"
            )
            raise AppError(
                ErrorCodes.RESTORE_SWITCH_FAILED,
                params={"operation_id": operation_id, "reason": str(e)},
            )

        # R65 P0-02: 真实蓝绿切换 — backend.prepare_switch() + backend.commit_switch()
        active_pointer: dict[str, Any] = {}
        switch_results: dict[str, dict[str, Any]] = {}
        switch_version = str(uuid.uuid4())  # 默认值(backends=None 时使用)
        previous_version = f"v_prev_{operation.backup_id}"

        if self._backends is not None:
            for datasource in _DATASOURCE_ORDER:
                if datasource not in self._backends:
                    continue
                backend = self._backends.get(datasource)
                ds_state = operation.datasource_states.get(datasource, {})
                provision_dict = ds_state.get("provision_result", {})
                provision_result = StagingProvisionResult(
                    target=provision_dict.get("target", ds_state.get("target", "")),
                    target_type=provision_dict.get("target_type", ""),
                    created_at=provision_dict.get("created_at", ""),
                    schema_fingerprint=provision_dict.get("schema_fingerprint", ""),
                )
                try:
                    prepare_result = await backend.prepare_switch(
                        operation_id=operation_id,
                        provision_result=provision_result,
                    )
                    switch_result = await backend.commit_switch(
                        operation_id=operation_id,
                        provision_result=provision_result,
                        prepare_result=prepare_result,
                    )
                except AppError:
                    # 切换失败 — 已成功的 datasource 应当回滚
                    # 简化处理:已 commit 的 datasource 调用 rollback_switch
                    for ds_done, sr_done in switch_results.items():
                        try:
                            await self._backends.get(ds_done).rollback_switch(
                                operation_id=operation_id,
                                switch_result=SwitchResult(**sr_done),
                            )
                        except Exception as rollback_err:
                            logger.critical(
                                _i18n_t(
                                    "diagnostics.r65.p0_02.rollback_after_switch_failed",
                                    datasource=ds_done,
                                    error=rollback_err,
                                )
                            )
                    await self.fail_operation(
                        operation_id, reason=f"switch_failed:{datasource}"
                    )
                    raise
                except Exception as e:
                    await self.fail_operation(
                        operation_id, reason=f"switch_failed:{datasource}:{e}"
                    )
                    raise AppError(
                        ErrorCodes.RESTORE_SWITCH_FAILED,
                        params={
                            "operation_id": operation_id,
                            "datasource": datasource,
                            "reason": str(e),
                        },
                    )
                switch_results[datasource] = {
                    "switch_version": switch_result.switch_version,
                    "previous_target": switch_result.previous_target,
                    "new_target": switch_result.new_target,
                    "switched_at": switch_result.switched_at,
                }
                active_pointer[datasource] = {
                    "previous_target": switch_result.previous_target,
                    "new_target": switch_result.new_target,
                    "switched_at": switch_result.switched_at,
                }
                # 使用第一个 datasource 的 switch_version 作为整体版本
                if datasource == _DATASOURCE_ORDER[0]:
                    switch_version = switch_result.switch_version
                    previous_version = switch_result.previous_target or previous_version
                logger.info(
                    _i18n_t(
                        "diagnostics.r65.p0_02.backend_commit_switch",
                        datasource=datasource,
                        previous=switch_result.previous_target,
                        new=switch_result.new_target,
                        operation_id=operation_id,
                    )
                )
        else:
            # 骨架行为:仅记录占位符(向后兼容 backends=None)
            active_pointer = {
                "crdb": {"database": "active_crdb"},
                "sqlite": {"path": "active_cache_store.db"},
                "relay_sqlite": {"path": "active_relay_pool.db"},
            }

        # 切换后保留旧版本作为限时回滚点
        await self._persist_rollback_target(
            operation_id=operation_id,
            switch_version=switch_version,
            active_pointer=active_pointer,
        )

        # 更新 operation 状态(包含 switch_results 用于回滚)
        new_ds_states = dict(operation.datasource_states)
        for ds, sr in switch_results.items():
            ds_state = dict(new_ds_states.get(ds, {}))
            ds_state["switch_result"] = sr
            new_ds_states[ds] = ds_state
        op = replace(
            operation,
            phase=RestorePhase.BLUE_GREEN_SWITCH,
            switch_version=switch_version,
            previous_version=previous_version,
            datasource_states=new_ds_states,
            updated_at=self._now_iso(),
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "switched", operation.phase, RestorePhase.BLUE_GREEN_SWITCH,
            payload={"switch_version": switch_version,
                     "previous_version": previous_version,
                     "switch_results": switch_results},
        )

        # consume nonce(reserved → consumed)
        await self._consume_nonce(operation_id, op)

        # 切换成功后进入 COMPLETED 终态
        await self._transition_to_completed(operation_id, op)
        return switch_version

    async def _execute_switch_with_authorities(
        self,
        operation: RestoreOperation,
        operation_id: str,
        approval_id: str,
        mfa_receipt_id: str,
    ) -> str:
        """R65 P0-03: UoW+CAS 路径 — approval/MFA capability + 原子 CAS 消费。

        在同一 ``UnitOfWork`` 中:
          1. ``ApprovalAuthority.verify_and_consume`` CAS 消费 approval
             (UPDATE command_approvals SET consumed_at=? WHERE id=? AND
              consumed_at IS NULL AND revoked_at IS NULL — rowcount==1)
          2. ``MFAAuthority.verify_and_consume`` CAS 消费 MFA receipt
             (INSERT OR IGNORE INTO mfa_receipts — rowcount==1)
          3. 故障注入(测试用,失败 → UoW 回滚)
          4. backend.commit_switch 或骨架路径(失败 → UoW 回滚)
          5. INSERT rollback_target(via uow)
          6. UPSERT operation phase → BLUE_GREEN_SWITCH(via uow)
          7. INSERT audit event "switched"(via uow)
          — UoW 提交后:approval/MFA 已消费、phase 已切换、rollback_target 已记录

        UoW 退出后(独立 commit):
          8. ``_consume_nonce``(reserved → consumed,store 独立 CAS+commit)
          9. ``_transition_to_completed``(phase → COMPLETED,独立 commit)

        任一步骤 1-7 失败 → UoW ``__aexit__`` 回滚 → approval/MFA 未消费
        (replay-safe,可安全重试)、phase 仍为 await_approval、nonce 仍 reserved。

        Args:
            operation: 当前 RestoreOperation 快照(phase=AWAIT_APPROVAL)
            operation_id: 操作 ID
            approval_id: 审批 ID(由 ApprovalAuthority 校验 + CAS 消费)
            mfa_receipt_id: MFA receipt token(由 MFAAuthority 校验 + CAS 消费)

        Returns:
            switch_version (UUID)

        Raises:
            AppError(RESTORE_APPROVAL_REQUIRED): approval 校验/CAS 失败
            AppError(RESTORE_MFA_REQUIRED): MFA 校验/CAS 失败
            AppError(RESTORE_SWITCH_FAILED): 故障注入/backend 切换失败
        """
        # UoW 包裹 approval CAS + MFA CAS + phase CAS + rollback_target + event
        # 任一失败 → __aexit__ 回滚 → approval/MFA 未消费(replay-safe)
        async with UnitOfWork(store=self._store) as uow:
            # 1. CAS 消费 approval — 返回不可伪造 ApprovalCapability
            #    校验:非空/int/行存在/decision=approved/未吊销/未消费/未过期/
            #          request_hash == manifest_digest/approver != requester
            approval_cap = await self._approval_authority.verify_and_consume(
                approval_id,
                expected_action_hash=operation.manifest_digest,
                expected_requester=operation.created_by,
                uow=uow,
            )
            # 2. CAS 消费 MFA receipt — 返回不可伪造 MFACapability
            #    校验:签名/sub/purpose/action_hash/未吊销/未过期/age/未消费
            mfa_cap = await self._mfa_authority.verify_and_consume(
                mfa_receipt_id,
                expected_principal_id=int(operation.created_by),
                expected_purpose="restore",
                expected_action_hash=operation.manifest_digest,
                uow=uow,
            )

            # 3. 故障注入(测试用) — 在 UoW 内,失败 → 回滚
            try:
                self._maybe_inject_fault("blue_green_switch", operation_id, None)
            except AppError:
                raise
            except Exception as e:
                raise AppError(
                    ErrorCodes.RESTORE_SWITCH_FAILED,
                    params={"operation_id": operation_id, "reason": str(e)},
                )

            # 4. R65 P0-02: 真实蓝绿切换 — backend.prepare_switch + commit_switch
            #    在 UoW 内执行,失败 → UoW 回滚(approval/MFA CAS 也回滚)
            active_pointer: dict[str, Any] = {}
            switch_results: dict[str, dict[str, Any]] = {}
            switch_version = str(uuid.uuid4())  # 默认值(backends=None 时使用)
            previous_version = f"v_prev_{operation.backup_id}"

            if self._backends is not None:
                for datasource in _DATASOURCE_ORDER:
                    if datasource not in self._backends:
                        continue
                    backend = self._backends.get(datasource)
                    ds_state = operation.datasource_states.get(datasource, {})
                    provision_dict = ds_state.get("provision_result", {})
                    provision_result = StagingProvisionResult(
                        target=provision_dict.get(
                            "target", ds_state.get("target", "")),
                        target_type=provision_dict.get("target_type", ""),
                        created_at=provision_dict.get("created_at", ""),
                        schema_fingerprint=provision_dict.get(
                            "schema_fingerprint", ""),
                    )
                    try:
                        prepare_result = await backend.prepare_switch(
                            operation_id=operation_id,
                            provision_result=provision_result,
                        )
                        switch_result = await backend.commit_switch(
                            operation_id=operation_id,
                            provision_result=provision_result,
                            prepare_result=prepare_result,
                        )
                    except AppError:
                        # 切换失败 — 已 commit 的 datasource 调用 rollback_switch
                        # (backend 操作,非 DB 写入,可在 UoW 内安全调用)
                        for ds_done, sr_done in switch_results.items():
                            try:
                                await self._backends.get(ds_done).rollback_switch(
                                    operation_id=operation_id,
                                    switch_result=SwitchResult(**sr_done),
                                )
                            except Exception as rollback_err:
                                logger.critical(
                                    _i18n_t(
                                        "diagnostics.r65.p0_02.rollback_after_switch_failed",
                                        datasource=ds_done,
                                        error=rollback_err,
                                    )
                                )
                        # UoW 回滚 approval/MFA CAS;不在此调用 fail_operation
                        # (fail_operation 独立 commit 会破坏 UoW 事务边界)
                        raise
                    except Exception as e:
                        raise AppError(
                            ErrorCodes.RESTORE_SWITCH_FAILED,
                            params={
                                "operation_id": operation_id,
                                "datasource": datasource,
                                "reason": str(e),
                            },
                        )
                    switch_results[datasource] = {
                        "switch_version": switch_result.switch_version,
                        "previous_target": switch_result.previous_target,
                        "new_target": switch_result.new_target,
                        "switched_at": switch_result.switched_at,
                    }
                    active_pointer[datasource] = {
                        "previous_target": switch_result.previous_target,
                        "new_target": switch_result.new_target,
                        "switched_at": switch_result.switched_at,
                    }
                    if datasource == _DATASOURCE_ORDER[0]:
                        switch_version = switch_result.switch_version
                        previous_version = (
                            switch_result.previous_target or previous_version)
                    logger.info(
                        _i18n_t(
                            "diagnostics.r65.p0_02.backend_commit_switch_uow",
                            datasource=datasource,
                            previous=switch_result.previous_target,
                            new=switch_result.new_target,
                            operation_id=operation_id,
                        )
                    )
            else:
                # 骨架行为:仅记录占位符(向后兼容 backends=None)
                active_pointer = {
                    "crdb": {"database": "active_crdb"},
                    "sqlite": {"path": "active_cache_store.db"},
                    "relay_sqlite": {"path": "active_relay_pool.db"},
                }

            # 5. INSERT rollback_target(via uow — 与 CAS 原子提交)
            await self._persist_rollback_target_uow(
                operation_id=operation_id,
                switch_version=switch_version,
                active_pointer=active_pointer,
                uow=uow,
            )

            # 6. UPSERT operation phase → BLUE_GREEN_SWITCH(via uow)
            new_ds_states = dict(operation.datasource_states)
            for ds, sr in switch_results.items():
                ds_state = dict(new_ds_states.get(ds, {}))
                ds_state["switch_result"] = sr
                new_ds_states[ds] = ds_state
            op = replace(
                operation,
                phase=RestorePhase.BLUE_GREEN_SWITCH,
                switch_version=switch_version,
                previous_version=previous_version,
                datasource_states=new_ds_states,
                updated_at=self._now_iso(),
            )
            await self._persist_operation_uow(op, uow)

            # 7. INSERT audit event "switched"(via uow)
            await self._write_event_uow(
                op, "switched", operation.phase, RestorePhase.BLUE_GREEN_SWITCH,
                uow=uow,
                payload={
                    "switch_version": switch_version,
                    "previous_version": previous_version,
                    "switch_results": switch_results,
                    "approval_cap": {
                        "approval_id": approval_cap.approval_id,
                        "approver_id": approval_cap.approver_id,
                    },
                    "mfa_cap": {
                        "jti": mfa_cap.jti,
                        "principal_id": mfa_cap.principal_id,
                    },
                },
            )
            # UoW 退出 — 提交(approval/MFA CAS + phase + rollback_target + event 原子)

        # UoW 已提交 — 以下操作独立 commit(nonce CAS + phase → COMPLETED)
        # nonce consume 失败仅记录 critical(切换已成功,不可回滚)
        await self._consume_nonce(operation_id, op)
        # phase → COMPLETED 终态
        await self._transition_to_completed(operation_id, op)
        return switch_version

    async def _consume_nonce(
        self, operation_id: str, operation: RestoreOperation
    ) -> None:
        """切换成功后 consume nonce(reserved → consumed)。"""
        meta = operation.datasource_states.get("_meta", {})
        nonce = meta.get("nonce", "")
        payload_digest = meta.get("payload_digest", "")
        if not nonce or not payload_digest:
            return  # 无 nonce(可能 store 不支持 nonce ledger)
        nonce_store = getattr(self._store, "consume_capability_nonce", None)
        if nonce_store is None:
            return
        try:
            consumed = await nonce_store(
                nonce=nonce,
                backup_id=operation.backup_id,
                manifest_sha256=operation.manifest_digest,
                payload_digest=payload_digest,
                consumed_by=operation.created_by,
            )
            if not consumed:
                # nonce 不在 reserved 状态(已 consumed / failed / 不存在)
                # 视为重放攻击 — 但切换已成功,仅记录 critical 告警
                logger.critical(
                    f"[restore_orchestrator] nonce consume 失败 "
                    f"(operation_id={operation_id}, nonce={nonce[:8]}...) — "
                    f"可能重放或状态错乱,但切换已成功"
                )
        except Exception as e:
            logger.warning(
                f"[restore_orchestrator] nonce consume 异常 "
                f"(operation_id={operation_id}): {e}"
            )

    async def _transition_to_completed(
        self, operation_id: str, operation: RestoreOperation
    ) -> None:
        """切换成功后转换到 COMPLETED 终态。"""
        op = replace(
            operation, phase=RestorePhase.COMPLETED, updated_at=self._now_iso()
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "phase_transition", operation.phase, RestorePhase.COMPLETED,
        )

    async def _persist_rollback_target(
        self,
        operation_id: str,
        switch_version: str,
        active_pointer: dict[str, Any],
    ) -> None:
        """持久化回滚目标(限时回滚点)。"""
        if not self._store or not getattr(self._store, "_db", None):
            return
        now = self._now_iso()
        # expires_at = now + rollback_ttl_seconds
        try:
            now_dt = _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now_dt = _dt.datetime.now(_dt.timezone.utc)
        expires_dt = now_dt + _dt.timedelta(seconds=self._rollback_ttl_seconds)
        await self._store._db.execute(
            """INSERT OR REPLACE INTO restore_rollback_targets
               (switch_version, operation_id, active_pointer,
                created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                switch_version, operation_id,
                json.dumps(active_pointer, ensure_ascii=False),
                now, expires_dt.isoformat(),
            ),
        )
        await self._store._db.commit()

    async def _persist_rollback_target_uow(
        self,
        operation_id: str,
        switch_version: str,
        active_pointer: dict[str, Any],
        uow: Any,
    ) -> None:
        """R65 P0-03: 在 UoW 内持久化回滚目标(不独立 commit)。

        与 ``_persist_rollback_target`` 行为一致,但通过 ``uow.execute`` 执行,
        由调用方的 UnitOfWork 统一控制事务边界。
        """
        if not self._store or not getattr(self._store, "_db", None):
            return
        now = self._now_iso()
        try:
            now_dt = _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now_dt = _dt.datetime.now(_dt.timezone.utc)
        expires_dt = now_dt + _dt.timedelta(seconds=self._rollback_ttl_seconds)
        await uow.execute(
            """INSERT OR REPLACE INTO restore_rollback_targets
               (switch_version, operation_id, active_pointer,
                created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                switch_version, operation_id,
                json.dumps(active_pointer, ensure_ascii=False),
                now, expires_dt.isoformat(),
            ),
        )

    # ─── 回滚 ──────────────────────────────────────────

    async def rollback_operation(
        self, operation_id: str, reason: str
    ) -> str:
        """状态机驱动回滚到旧版本,写入审计事件。

        仅允许 COMPLETED → ROLLED_BACK(BLUE_GREEN_SWITCH 后回滚)。
        切换前的 staging 失败用 fail_operation(销毁 staging)。

        R65 P0-02: 若提供 BackendRegistry,调用 backend.rollback_switch()
        对每个 datasource 执行真实回滚(SQLite 反向 rename / CRDB schema 反向切换)。

        Args:
            operation_id: 操作 ID(必须处于 COMPLETED 状态)
            reason: 回滚原因(写入审计事件)

        Returns:
            rollback_version (与 switch_version 相同,即回滚到的旧版本指针)
        """
        operation = self.get_operation(operation_id)
        self._assert_legal_transition(operation, RestorePhase.ROLLED_BACK)
        switch_version = operation.switch_version
        if not switch_version:
            # 未切换过的 operation 不应进入回滚(状态机已禁止)
            raise AppError(
                ErrorCodes.RESTORE_ROLLBACK_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": "no_switch_version_to_rollback",
                },
            )
        # 触发故障注入(测试用)
        try:
            self._maybe_inject_fault("rollback", operation_id, None)
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_ROLLBACK_FAILED,
                params={"operation_id": operation_id, "reason": str(e)},
            )

        # 加载旧版本 active 指针(rollback target)
        rollback_pointer = await self._load_rollback_target(switch_version)

        # R65 P0-02: 真实回滚 — backend.rollback_switch()
        rollback_results: dict[str, dict[str, Any]] = {}
        if self._backends is not None:
            for datasource in _DATASOURCE_ORDER:
                if datasource not in self._backends:
                    continue
                ds_state = operation.datasource_states.get(datasource, {})
                switch_dict = ds_state.get("switch_result", {})
                if not switch_dict:
                    # 该 datasource 未切换(可能未注册或骨架行为)
                    continue
                try:
                    switch_result = SwitchResult(
                        switch_version=switch_dict.get("switch_version", ""),
                        previous_target=switch_dict.get("previous_target", ""),
                        new_target=switch_dict.get("new_target", ""),
                        switched_at=switch_dict.get("switched_at", ""),
                    )
                    backend = self._backends.get(datasource)
                    rb_result = await backend.rollback_switch(
                        operation_id=operation_id,
                        switch_result=switch_result,
                    )
                    rollback_results[datasource] = {
                        "switch_version": rb_result.switch_version,
                        "previous_target": rb_result.previous_target,
                        "new_target": rb_result.new_target,
                        "switched_at": rb_result.switched_at,
                    }
                    logger.info(
                        _i18n_t(
                            "diagnostics.r65.p0_02.backend_rollback_switch",
                            datasource=datasource,
                            previous=rb_result.previous_target,
                            new=rb_result.new_target,
                            operation_id=operation_id,
                        )
                    )
                except AppError:
                    raise
                except Exception as e:
                    logger.error(
                        _i18n_t(
                            "diagnostics.r65.p0_02.rollback_failed",
                            datasource=datasource,
                            error=e,
                        )
                    )
                    raise AppError(
                        ErrorCodes.RESTORE_ROLLBACK_FAILED,
                        params={
                            "operation_id": operation_id,
                            "datasource": datasource,
                            "reason": str(e),
                        },
                    )

        op = replace(
            operation, phase=RestorePhase.ROLLED_BACK, updated_at=self._now_iso()
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "rolled_back", operation.phase, RestorePhase.ROLLED_BACK,
            payload={
                "switch_version": switch_version,
                "rollback_pointer": rollback_pointer,
                "rollback_results": rollback_results,
                "reason": reason,
            },
        )
        return switch_version

    async def _load_rollback_target(
        self, switch_version: str
    ) -> dict[str, Any]:
        """加载回滚目标指针。"""
        if not self._store or not getattr(self._store, "_db", None):
            return {}
        try:
            cursor = await self._store._db.execute(
                """SELECT active_pointer FROM restore_rollback_targets
                   WHERE switch_version = ?""",
                (switch_version,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {}
            return json.loads(row[0] or "{}")
        except Exception as e:
            logger.warning(
                f"[restore_orchestrator] load rollback target 失败: {e}"
            )
            return {}

    # ─── 失败处理 ──────────────────────────────────────

    async def fail_operation(
        self, operation_id: str, reason: str
    ) -> None:
        """销毁所有 staging 资源,nonce state=failed(允许同 operation 重试但禁止换 payload)。

        幂等:若 operation 已处于 FAILED/ROLLED_BACK 终态,直接返回(不重复销毁)。
        """
        operation = self.get_operation(operation_id)
        if operation.phase in (RestorePhase.FAILED, RestorePhase.ROLLED_BACK):
            return  # 幂等:已终态
        # 销毁 staging 资源(SQLite 文件删除,CRDB schema drop 由实际 client 完成)
        await self._destroy_staging(operation)
        # nonce state=failed(允许同 operation 重试但禁止换 payload)
        await self._fail_nonce(operation_id, operation, reason)
        # 更新 operation 状态
        op = replace(
            operation, phase=RestorePhase.FAILED, updated_at=self._now_iso()
        )
        await self._persist_operation(op)
        await self._write_event(
            op, "failed", operation.phase, RestorePhase.FAILED,
            payload={"reason": reason},
        )

    async def _destroy_staging(self, operation: RestoreOperation) -> None:
        """销毁所有 staging 资源。

        R65 P0-02: 若提供 BackendRegistry,调用 backend.destroy() 真实销毁
        (SQLite unlink / CRDB DROP SCHEMA CASCADE)。
        未注册的 datasource 退回骨架行为(直接 unlink SQLite 文件)。
        """
        for ds in _DATASOURCE_ORDER:
            ds_state = operation.datasource_states.get(ds, {})
            target = ds_state.get("target", "")
            if not target:
                continue
            # R65 P0-02: 优先调用 backend.destroy()
            if self._backends is not None and ds in self._backends:
                provision_dict = ds_state.get("provision_result", {})
                provision_result = StagingProvisionResult(
                    target=provision_dict.get("target", target),
                    target_type=provision_dict.get("target_type", ""),
                    created_at=provision_dict.get("created_at", ""),
                    schema_fingerprint=provision_dict.get("schema_fingerprint", ""),
                )
                try:
                    backend = self._backends.get(ds)
                    await backend.destroy(
                        operation_id=operation.operation_id,
                        provision_result=provision_result,
                    )
                    logger.info(
                        _i18n_t(
                            "diagnostics.r65.p0_02.backend_destroy",
                            datasource=ds,
                            target=target,
                        )
                    )
                    continue
                except Exception as e:
                    logger.warning(
                        _i18n_t(
                            "diagnostics.r65.p0_02.backend_destroy_failed_fallback",
                            datasource=ds,
                            error=e,
                        )
                    )
                    # 回退到骨架 unlink(若为 SQLite)
            # 骨架行为:直接 unlink(SQLite 文件)
            if ds in ("sqlite", "relay_sqlite") and Path(target).exists():
                try:
                    Path(target).unlink()
                    logger.info(
                        f"[restore_orchestrator] 销毁 staging {ds}: {target}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[restore_orchestrator] 销毁 staging {ds} 失败 "
                        f"(忽略,可能已被删): {e}"
                    )

    async def _fail_nonce(
        self,
        operation_id: str,
        operation: RestoreOperation,
        reason: str,
    ) -> None:
        """nonce state=failed(允许同 operation 重试但禁止换 payload)。"""
        meta = operation.datasource_states.get("_meta", {})
        nonce = meta.get("nonce", "")
        payload_digest = meta.get("payload_digest", "")
        if not nonce or not payload_digest:
            return
        fail_store = getattr(self._store, "fail_capability_nonce", None)
        if fail_store is None:
            return
        try:
            # fail_capability_nonce 签名仅接受 nonce + failure_reason
            # (backup_id / manifest_sha256 / payload_digest 已在 reserve 时绑定,
            #  fail 仅 CAS reserved→failed,无需重复绑定)
            await fail_store(
                nonce=nonce,
                failure_reason=reason,
            )
        except Exception as e:
            logger.warning(
                f"[restore_orchestrator] nonce fail 异常 "
                f"(operation_id={operation_id}): {e}"
            )

    # ─── 故障注入(测试用) ──────────────────────────────

    def _maybe_inject_fault(
        self,
        hook_key: str,
        operation_id: str,
        datasource: Optional[str],
    ) -> None:
        """触发故障注入钩子(production 应无任何 hook)。

        hook_key 形如:
            staging_provision.crdb
            staging_restore.sqlite
            staging_validate.hash_check
            blue_green_switch
            rollback

        Args:
            hook_key: 钩子键
            operation_id: 操作 ID
            datasource: 数据源(可为 None)
        """
        hook = self._fault_hooks.get(hook_key)
        if hook is None:
            return
        # 调用钩子(钩子内可 raise 以模拟故障)
        hook(self, operation_id, datasource)

    # ─── 查询辅助 ──────────────────────────────────────

    async def list_operation_events(
        self, operation_id: str
    ) -> list[dict[str, Any]]:
        """列出 operation 的所有审计事件(按时间顺序)。"""
        if not self._store or not getattr(self._store, "_db", None):
            return []
        cursor = await self._store._db.execute(
            """SELECT event_id, operation_id, event_type, phase_from, phase_to,
                      payload, trace_id, created_at
               FROM restore_operation_events
               WHERE operation_id = ?
               ORDER BY event_id ASC""",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "event_id": r[0], "operation_id": r[1], "event_type": r[2],
                "phase_from": r[3], "phase_to": r[4],
                "payload": json.loads(r[5] or "{}"),
                "trace_id": r[6], "created_at": r[7],
            }
            for r in rows
        ]

    async def list_rollback_targets(
        self, operation_id: str
    ) -> list[dict[str, Any]]:
        """列出 operation 的所有回滚目标(限时回滚点)。"""
        if not self._store or not getattr(self._store, "_db", None):
            return []
        cursor = await self._store._db.execute(
            """SELECT switch_version, operation_id, active_pointer,
                      created_at, expires_at
               FROM restore_rollback_targets
               WHERE operation_id = ?
               ORDER BY created_at ASC""",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "switch_version": r[0], "operation_id": r[1],
                "active_pointer": json.loads(r[2] or "{}"),
                "created_at": r[3], "expires_at": r[4],
            }
            for r in rows
        ]

    async def get_persisted_operation(
        self, operation_id: str
    ) -> Optional[dict[str, Any]]:
        """从持久化层读取 operation(供测试验证持久化)。"""
        if not self._store or not getattr(self._store, "_db", None):
            return None
        cursor = await self._store._db.execute(
            """SELECT operation_id, backup_id, manifest_digest, phase,
                      datasource_states, validation_summary, approval_id,
                      mfa_receipt_id, switch_version, previous_version,
                      created_at, updated_at, created_by
               FROM restore_operations
               WHERE operation_id = ?""",
            (operation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "operation_id": row[0], "backup_id": row[1],
            "manifest_digest": row[2], "phase": row[3],
            "datasource_states": json.loads(row[4] or "{}"),
            "validation_summary": json.loads(row[5] or "{}"),
            "approval_id": row[6], "mfa_receipt_id": row[7],
            "switch_version": row[8], "previous_version": row[9],
            "created_at": row[10], "updated_at": row[11],
            "created_by": row[12],
        }
