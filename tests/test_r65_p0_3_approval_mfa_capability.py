"""R65 P0-03: 不可伪造的恢复审批/MFA capability + UoW CAS 消费 测试。

审计背景(R65 终审报告 P0-03):
    旧 ``execute_blue_green_switch`` 仅比较 ``approval_id == operation.approval_id``
    与 ``mfa_receipt_id == operation.mfa_receipt_id`` — 这两个值都是调用方传入的
    不透明字符串,可被任意伪造。攻击者只需在 request_approval 阶段塞入任意 ID,
    再在 execute_blue_green_switch 阶段传入相同 ID 即可绕过审批/MFA。

整改方案(R65 P0-03):
    1. ``ApprovalAuthority.verify_and_consume`` + ``MFAAuthority.verify_and_consume``
       返回不可伪造的 capability(由权威层校验 + CAS 消费后才构造)
    2. ``execute_blue_green_switch`` 在同一 ``UnitOfWork`` 中 CAS 消费:
       approval(consumed_at)、MFA(jti INSERT OR IGNORE)、operation phase
       (await_approval → blue_green_switch)、nonce(reserved → consumed)
    3. 任一失败 → UoW 回滚 → approval/MFA 未消费(防重放,可安全重试)
    4. ``authority=None`` 时保留旧 ID 比较路径(向后兼容 R64 测试)

测试覆盖矩阵:
    A. ApprovalAuthority.verify_and_consume
       - happy path(返回 ApprovalCapability + CAS 消费 consumed_at)
       - fail-closed: empty id / not found / decision != approved / revoked /
         already consumed / expired / action_hash mismatch / approver == requester
    B. MFAAuthority.verify_and_consume
       - happy path(返回 MFACapability + CAS 消费 jti)
       - fail-closed: revoked / already consumed / wrong principal / wrong purpose /
         wrong action_hash
    C. execute_blue_green_switch with authorities
       - happy path(capabilities consumed + phase → COMPLETED + switch_version)
       - wrong approval_id → RESTORE_APPROVAL_REQUIRED + UoW 回滚(approval 未消费)
       - wrong mfa → RESTORE_MFA_REQUIRED + UoW 回滚(approval 未消费)
    D. 向后兼容(authorities=None)旧 ID 比较路径仍工作
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 测试环境兼容(conftest 在收集阶段已注入 config/telegram mock)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 测试辅助
# ════════════════════════════════════════════════════════════════


async def _make_store_with_tables(db_path: str | None = None):
    """构造真实 CacheStore + restore_operations 三表 + command_approvals 表。

    与 R64 测试一致,通过手动执行 migration 007 SQL 创建 restore_operations 三表,
    额外创建 command_approvals 表(严格 schema,与 migration 003 final 一致)。
    """
    from database.cache_store import CacheStore

    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r65_p0_3_test_")
        db_path = str(Path(_tmp_dir) / "test_restore.db")
    store = CacheStore(db_path=db_path)
    await store.init()
    # 执行 migration 007(restore_operations 三表)— 与 R64 测试一致的语句分割
    migration_path = (
        REPO_ROOT / "database" / "migrations" / "007_restore_operations_ledger.sql"
    )
    sql_text = migration_path.read_text(encoding="utf-8")
    buffer = ""
    for line in sql_text.splitlines():
        if not line.strip() or line.strip().startswith("--"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt and not stmt.startswith("--"):
                await store._db.execute(stmt)
            buffer = ""
    if buffer.strip() and not buffer.strip().startswith("--"):
        await store._db.execute(buffer.strip())
    # 创建 command_approvals 表(严格 schema,与 migration 003 final rename 后一致)
    await store._db.execute(
        """
        CREATE TABLE IF NOT EXISTS command_approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id       TEXT NOT NULL,
            approver_id     BIGINT NOT NULL,
            approval_type   TEXT NOT NULL
                CHECK (approval_type IN (
                    'break_glass','quarantine_delete','collection',
                    'maintenance','rbac')),
            decision        TEXT NOT NULL
                CHECK (decision IN ('approved','rejected','cancelled'))
                DEFAULT 'approved',
            request_hash    TEXT NOT NULL
                CHECK (length(request_hash) = 64
                       AND request_hash NOT GLOB '*[^0-9a-f]*'),
            mfa_receipt     TEXT NOT NULL
                CHECK (length(trim(mfa_receipt)) > 0),
            permission      TEXT NOT NULL
                CHECK (length(trim(permission)) > 0),
            approved_at     TEXT NOT NULL,
            expires_at      TEXT NOT NULL
                CHECK (length(trim(expires_at)) > 0),
            consumed_at     TEXT,
            revoked_at      TEXT,
            metadata_json   TEXT,
            UNIQUE(action_id, approver_id, approval_type)
        )
        """
    )
    await store._db.commit()
    return store, db_path


def _make_orchestrator(
    store,
    *,
    staging_root: str | None = None,
    fault_hooks=None,
    rollback_ttl_seconds: int = 86400,
    approval_authority=None,
    mfa_authority=None,
):
    """构造 RestoreOrchestrator(真实 authorities)或 SkeletonFake(authorities=None)。

    R66 P0-06: 生产类 RestoreOrchestrator 已删除所有 Optional 降级骨架,
    backends / approval_authority / mfa_authority 均为必需参数。
    - 提供 approval_authority / mfa_authority 时:构造真实 RestoreOrchestrator
      (注入 mock backends)
    - authorities=None 时:使用 tests-only fake 保留旧 ID 比较路径
    """
    if staging_root is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r65_p0_3_staging_")
        staging_root = _tmp_dir
    if approval_authority is None and mfa_authority is None:
        # R66 P0-06: authorities=None 时使用 tests-only fake(旧 ID 比较路径)
        from tests._restore_skeleton_fake import RestoreOrchestratorSkeletonFake
        return RestoreOrchestratorSkeletonFake(
            store,
            staging_root=staging_root,
            fault_hooks=fault_hooks or {},
            rollback_ttl_seconds=rollback_ttl_seconds,
        )
    # 真实 authorities:构造生产类 RestoreOrchestrator(注入 mock backends)
    from services.restore_orchestrator import RestoreOrchestrator
    from services.restore_backends import BackendRegistry
    return RestoreOrchestrator(
        store,
        staging_root=staging_root,
        fault_hooks=fault_hooks or {},
        rollback_ttl_seconds=rollback_ttl_seconds,
        backends=BackendRegistry(),
        approval_authority=approval_authority,
        mfa_authority=mfa_authority,
    )


async def _insert_command_approval(
    store,
    *,
    approver_id: int,
    request_hash: str,
    mfa_receipt: str,
    action_id: str | None = None,
    approval_type: str = "break_glass",
    decision: str = "approved",
    permission: str = "break_glass",
    approved_at: str | None = None,
    expires_at: str | None = None,
    consumed_at: str | None = None,
    revoked_at: str | None = None,
) -> int:
    """向 command_approvals 插入一条审批记录,返回自增 id。"""
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if approved_at is None:
        approved_at = now_iso
    if expires_at is None:
        # 默认 1 小时后过期
        future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
        expires_at = future.isoformat()
    if action_id is None:
        action_id = f"restore_action_{uuid.uuid4().hex[:16]}"
    cursor = await store._db.execute(
        """
        INSERT INTO command_approvals
        (action_id, approver_id, approval_type, decision, request_hash,
         mfa_receipt, permission, approved_at, expires_at,
         consumed_at, revoked_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            action_id, approver_id, approval_type, decision, request_hash,
            mfa_receipt, permission, approved_at, expires_at,
            consumed_at, revoked_at,
        ),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


def _set_mfa_signing_key(monkeypatch):
    """设置 MFA receipt 签名密钥(仅测试用)。"""
    monkeypatch.setenv(
        "MFA_RECEIPT_SIGNING_KEY",
        "r65_p0_03_test_signing_key_32bytes_min",
    )
    monkeypatch.delenv("MFA_RECEIPT_SIGNING_KEY_PREVIOUS", raising=False)


# 测试用常量
_REQUESTER_ID = 1001  # operation.created_by(数字字符串)
_APPROVER_ID = 2002   # 审批人(必须 != requester)
_ACTION_HASH_64HEX = "a" * 64  # 64 位 hex(SHA-256,与 manifest_digest 一致)
_PURPOSE_RESTORE = "restore"


# ════════════════════════════════════════════════════════════════
# A. ApprovalAuthority.verify_and_consume
# ════════════════════════════════════════════════════════════════


class TestApprovalAuthorityVerifyAndConsume:
    """R65 P0-03: ApprovalAuthority — 校验审批状态 + UoW 内 CAS 消费。"""

    @pytest.mark.asyncio
    async def test_happy_path_returns_capability_and_consumes(self, monkeypatch):
        """合法审批 → 返回 ApprovalCapability + consumed_at 写入 DB。"""
        from services.restore_capabilities import (
            ApprovalAuthority, ApprovalCapability,
        )
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            cap = await authority.verify_and_consume(
                str(approval_id),
                expected_action_hash=_ACTION_HASH_64HEX,
                expected_requester=str(_REQUESTER_ID),
                uow=uow,
            )

        # 返回不可伪造的 capability
        assert isinstance(cap, ApprovalCapability)
        assert cap.approval_id == str(approval_id)
        assert cap.approver_id == _APPROVER_ID
        assert cap.action_hash == _ACTION_HASH_64HEX
        assert cap.consumed_at  # ISO8601 非空

        # 验证 DB 中 consumed_at 已写入
        cursor = await store._db.execute(
            "SELECT consumed_at FROM command_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is not None, \
            "UoW 提交后 consumed_at 应非 NULL"

    @pytest.mark.asyncio
    async def test_empty_approval_id_raises(self, monkeypatch):
        """approval_id 为空 → RESTORE_APPROVAL_REQUIRED(approval_id_empty)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    "",
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_approval_not_found_raises(self, monkeypatch):
        """approval_id 不存在 → RESTORE_APPROVAL_REQUIRED(approval_not_found)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    "99999",
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_decision_not_approved_raises(self, monkeypatch):
        """decision != 'approved' → RESTORE_APPROVAL_REQUIRED(decision_not_approved)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
            decision="rejected",
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_revoked_raises(self, monkeypatch):
        """revoked_at 非 NULL → RESTORE_APPROVAL_REQUIRED(approval_revoked)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
            revoked_at=now_iso,
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_already_consumed_raises(self, monkeypatch):
        """consumed_at 非 NULL → RESTORE_APPROVAL_REQUIRED(approval_already_consumed)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
            consumed_at=now_iso,
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_expired_raises(self, monkeypatch):
        """expires_at <= now → RESTORE_APPROVAL_REQUIRED(approval_expired)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        past_iso = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
        ).isoformat()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
            expires_at=past_iso,
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_action_hash_mismatch_raises(self, monkeypatch):
        """request_hash != expected_action_hash → RESTORE_APPROVAL_REQUIRED(action_hash_mismatch)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        stored_hash = "b" * 64  # 与 expected 不同
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=stored_hash,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,  # "a"*64
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_approver_equals_requester_raises(self, monkeypatch):
        """approver_id == requester → RESTORE_APPROVAL_REQUIRED(approver_equals_requester)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        # approver_id == _REQUESTER_ID(自审批,违反双人审批)
        approval_id = await _insert_command_approval(
            store,
            approver_id=_REQUESTER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        authority = ApprovalAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_exception_rolls_back_consumed_at(self, monkeypatch):
        """UoW 异常退出 → consumed_at 回滚为 NULL(approval 未消费,replay-safe)。"""
        from services.restore_capabilities import ApprovalAuthority
        from services.error_codes import AppError
        from database.unit_of_work import UnitOfWork

        store, _ = await _make_store_with_tables()
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        authority = ApprovalAuthority(store=store)

        # verify_and_consume 成功后,在 UoW 内抛异常 → 回滚
        with pytest.raises(RuntimeError):
            async with UnitOfWork(store=store) as uow:
                await authority.verify_and_consume(
                    str(approval_id),
                    expected_action_hash=_ACTION_HASH_64HEX,
                    expected_requester=str(_REQUESTER_ID),
                    uow=uow,
                )
                raise RuntimeError("simulated_uow_failure")

        # consumed_at 应为 NULL(UoW 回滚)
        cursor = await store._db.execute(
            "SELECT consumed_at FROM command_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is None, \
            "UoW 异常回滚后 consumed_at 必须为 NULL(approval 未消费)"


# ════════════════════════════════════════════════════════════════
# B. MFAAuthority.verify_and_consume
# ════════════════════════════════════════════════════════════════


class TestMFAAuthorityVerifyAndConsume:
    """R65 P0-03: MFAAuthority — 校验 MFA receipt + UoW 内 CAS 消费。"""

    @pytest.mark.asyncio
    async def test_happy_path_returns_capability_and_consumes(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """合法 MFA receipt → 返回 MFACapability + jti 在 mfa_receipts 表。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import MFAAuthority, MFACapability
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        authority = MFAAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            cap = await authority.verify_and_consume(
                token,
                expected_principal_id=_REQUESTER_ID,
                expected_purpose=_PURPOSE_RESTORE,
                expected_action_hash=_ACTION_HASH_64HEX,
                uow=uow,
            )

        assert isinstance(cap, MFACapability)
        assert cap.principal_id == _REQUESTER_ID
        assert cap.purpose == _PURPOSE_RESTORE
        assert cap.action_hash == _ACTION_HASH_64HEX
        assert cap.amr == ("totp",)
        assert cap.consumed_at

        # 验证 jti 在 mfa_receipts 表中(CAS 消费成功)
        cursor = await store._db.execute(
            "SELECT jti FROM mfa_receipts WHERE jti = ?",
            (cap.jti,),
        )
        row = await cursor.fetchone()
        assert row is not None, "UoW 提交后 jti 应在 mfa_receipts 表中"

    @pytest.mark.asyncio
    async def test_revoked_raises(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """jti 已吊销 → AppError(revoked)。"""
        from admin.mfa import issue_mfa_receipt, get_mfa_manager
        from services.restore_capabilities import MFAAuthority
        from services.error_codes import AppError
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 先 verify 一次(consume=False)拿到 jti
        from admin.mfa import verify_mfa_receipt_authoritative
        payload = await verify_mfa_receipt_authoritative(
            token,
            expected_principal_id=_REQUESTER_ID,
            expected_purpose=_PURPOSE_RESTORE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        jti = payload["jti"]
        # 吊销 jti
        manager = get_mfa_manager()
        await manager.revoke_mfa_receipt(jti, reason="test_revoke")

        authority = MFAAuthority(store=store)
        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError):
                await authority.verify_and_consume(
                    token,
                    expected_principal_id=_REQUESTER_ID,
                    expected_purpose=_PURPOSE_RESTORE,
                    expected_action_hash=_ACTION_HASH_64HEX,
                    uow=uow,
                )

    @pytest.mark.asyncio
    async def test_already_consumed_raises(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """jti 已消费(INSERT OR IGNORE rowcount=0)→ RESTORE_MFA_REQUIRED。"""
        from admin.mfa import issue_mfa_receipt, consume_mfa_receipt
        from services.restore_capabilities import MFAAuthority
        from services.error_codes import AppError, ErrorCodes
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 先 verify(consume=False)拿到 jti,然后预消费 jti
        from admin.mfa import verify_mfa_receipt_authoritative
        payload = await verify_mfa_receipt_authoritative(
            token,
            expected_principal_id=_REQUESTER_ID,
            expected_purpose=_PURPOSE_RESTORE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        pre_consumed = await consume_mfa_receipt(payload["jti"])
        assert pre_consumed is True, "预消费 jti 应成功"

        authority = MFAAuthority(store=store)
        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError) as exc_info:
                await authority.verify_and_consume(
                    token,
                    expected_principal_id=_REQUESTER_ID,
                    expected_purpose=_PURPOSE_RESTORE,
                    expected_action_hash=_ACTION_HASH_64HEX,
                    uow=uow,
                )
        assert exc_info.value.code == ErrorCodes.RESTORE_MFA_REQUIRED

    @pytest.mark.asyncio
    async def test_wrong_principal_raises(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """sub != expected_principal_id → AppError。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import MFAAuthority
        from services.error_codes import AppError
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        authority = MFAAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError):
                await authority.verify_and_consume(
                    token,
                    expected_principal_id=9999,  # 错误 principal
                    expected_purpose=_PURPOSE_RESTORE,
                    expected_action_hash=_ACTION_HASH_64HEX,
                    uow=uow,
                )

    @pytest.mark.asyncio
    async def test_wrong_purpose_raises(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """purpose != expected_purpose → AppError。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import MFAAuthority
        from services.error_codes import AppError
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        authority = MFAAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError):
                await authority.verify_and_consume(
                    token,
                    expected_principal_id=_REQUESTER_ID,
                    expected_purpose="wrong_purpose",
                    expected_action_hash=_ACTION_HASH_64HEX,
                    uow=uow,
                )

    @pytest.mark.asyncio
    async def test_wrong_action_hash_raises(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """action_hash != expected_action_hash → AppError。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import MFAAuthority
        from services.error_codes import AppError
        from database.unit_of_work import UnitOfWork

        store = real_store_fixture
        token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        authority = MFAAuthority(store=store)

        async with UnitOfWork(store=store) as uow:
            with pytest.raises(AppError):
                await authority.verify_and_consume(
                    token,
                    expected_principal_id=_REQUESTER_ID,
                    expected_purpose=_PURPOSE_RESTORE,
                    expected_action_hash="c" * 64,  # 错误 hash
                    uow=uow,
                )


# ════════════════════════════════════════════════════════════════
# C. execute_blue_green_switch with authorities(UoW CAS 路径)
# ════════════════════════════════════════════════════════════════


class TestExecuteBlueGreenSwitchWithAuthorities:
    """R65 P0-03: execute_blue_green_switch 在提供 authorities 时走 UoW+CAS 路径。"""

    @pytest.mark.asyncio
    async def test_happy_path_consumes_all_and_completes(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """提供 authorities → 完整 happy path:approval+MFA+nonce 消费 + COMPLETED。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import ApprovalAuthority, MFAAuthority

        store = real_store_fixture
        # 准备 approval 行(action_hash == manifest_digest)
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        # 准备 MFA receipt token
        mfa_token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        approval_authority = ApprovalAuthority(store=store)
        mfa_authority = MFAAuthority(store=store)
        orch = _make_orchestrator(
            store,
            approval_authority=approval_authority,
            mfa_authority=mfa_authority,
        )

        operation_id = await orch.start_operation(
            backup_id="backup_r65_001",
            manifest_digest=_ACTION_HASH_64HEX,
            requested_by=str(_REQUESTER_ID),
            payload_digest="d" * 64,
            nonce="nonce_r65_happy",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, str(approval_id), mfa_token)

        switch_version = await orch.execute_blue_green_switch(
            operation_id, str(approval_id), mfa_token
        )
        uuid.UUID(switch_version)  # UUID 格式

        # operation 进入 COMPLETED 终态
        op = await orch.get_operation(operation_id)
        assert op.phase.value == "completed"

        # approval 已消费
        cursor = await store._db.execute(
            "SELECT consumed_at FROM command_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is not None, "approval 应已消费"

        # nonce 已消费
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE operation_id = ?",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        assert any(r[0] == "consumed" for r in rows), \
            f"nonce 应已消费,实际: {rows}"

    @pytest.mark.asyncio
    async def test_wrong_approval_raises_and_rolls_back(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """错误 approval_id → RESTORE_APPROVAL_REQUIRED + UoW 回滚(approval 未消费)。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import ApprovalAuthority, MFAAuthority
        from services.error_codes import AppError, ErrorCodes

        store = real_store_fixture
        # 准备一个合法 approval(但调用时传错误的 approval_id)
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        mfa_token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        approval_authority = ApprovalAuthority(store=store)
        mfa_authority = MFAAuthority(store=store)
        orch = _make_orchestrator(
            store,
            approval_authority=approval_authority,
            mfa_authority=mfa_authority,
        )
        operation_id = await orch.start_operation(
            backup_id="backup_r65_wrong_approval",
            manifest_digest=_ACTION_HASH_64HEX,
            requested_by=str(_REQUESTER_ID),
            payload_digest="d" * 64,
            nonce="nonce_r65_wrong_approval",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, str(approval_id), mfa_token)

        # 传错误的 approval_id(不存在)
        with pytest.raises(AppError) as exc_info:
            await orch.execute_blue_green_switch(
                operation_id, "99999", mfa_token
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED

        # UoW 回滚:approval 未消费
        cursor = await store._db.execute(
            "SELECT consumed_at FROM command_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is None, \
            "UoW 回滚后 approval consumed_at 必须为 NULL(未消费,replay-safe)"

        # operation phase 仍为 await_approval(未切换)
        op = await orch.get_operation(operation_id)
        assert op.phase.value == "await_approval", \
            "UoW 回滚后 phase 应仍为 await_approval"

    @pytest.mark.asyncio
    async def test_wrong_mfa_raises_and_rolls_back(
        self, monkeypatch, real_store_fixture, mfa_signing_key_env,
    ):
        """错误 MFA → AppError + UoW 回滚(approval 未消费,尽管 approval CAS 已执行)。"""
        from admin.mfa import issue_mfa_receipt
        from services.restore_capabilities import ApprovalAuthority, MFAAuthority
        from services.error_codes import AppError

        store = real_store_fixture
        approval_id = await _insert_command_approval(
            store,
            approver_id=_APPROVER_ID,
            request_hash=_ACTION_HASH_64HEX,
            mfa_receipt="mfa_placeholder_nonempty",
        )
        # 错误的 MFA token(用错误的 action_hash 签发)
        wrong_mfa_token = issue_mfa_receipt(
            principal_id=_REQUESTER_ID,
            purpose=_PURPOSE_RESTORE,
            action_hash="c" * 64,  # 与 manifest_digest 不匹配
            amr=["totp"],
            ttl_seconds=300,
        )
        approval_authority = ApprovalAuthority(store=store)
        mfa_authority = MFAAuthority(store=store)
        orch = _make_orchestrator(
            store,
            approval_authority=approval_authority,
            mfa_authority=mfa_authority,
        )
        operation_id = await orch.start_operation(
            backup_id="backup_r65_wrong_mfa",
            manifest_digest=_ACTION_HASH_64HEX,
            requested_by=str(_REQUESTER_ID),
            payload_digest="d" * 64,
            nonce="nonce_r65_wrong_mfa",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, str(approval_id), wrong_mfa_token)

        with pytest.raises(AppError):
            await orch.execute_blue_green_switch(
                operation_id, str(approval_id), wrong_mfa_token
            )

        # UoW 回滚:approval 未消费(approval CAS 已执行但被回滚)
        cursor = await store._db.execute(
            "SELECT consumed_at FROM command_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is None, \
            "UoW 回滚后 approval consumed_at 必须为 NULL(尽管 approval CAS 已执行)"

        # operation phase 仍为 await_approval
        op = await orch.get_operation(operation_id)
        assert op.phase.value == "await_approval"

        # nonce 仍为 reserved(未消费)
        cursor = await store._db.execute(
            "SELECT status FROM restore_capability_nonces WHERE operation_id = ?",
            (operation_id,),
        )
        rows = await cursor.fetchall()
        assert all(r[0] == "reserved" for r in rows), \
            f"UoW 回滚后 nonce 应仍为 reserved,实际: {rows}"


# ════════════════════════════════════════════════════════════════
# D. 向后兼容(authorities=None 旧 ID 比较路径)
# ════════════════════════════════════════════════════════════════


class TestBackwardCompatNoAuthorities:
    """R65 P0-03: authorities=None 时保留旧 ID 比较路径(R64 测试向后兼容)。"""

    @pytest.mark.asyncio
    async def test_no_authorities_uses_id_comparison_path(self, monkeypatch):
        """authorities=None → 旧 ID 比较路径仍工作(完整 happy path)。"""
        store, _ = await _make_store_with_tables()
        orch = _make_orchestrator(store)  # authorities=None

        operation_id = await orch.start_operation(
            backup_id="backup_compat_001",
            manifest_digest="a" * 64,
            requested_by="tester",  # 非数字字符串(旧路径不要求 numeric)
            payload_digest="d" * 64,
            nonce="nonce_compat_001",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        # 旧路径:不透明字符串 ID
        approval_id = "approval_compat_001"
        mfa_receipt_id = "mfa_compat_001"
        await orch.request_approval(operation_id, approval_id, mfa_receipt_id)
        switch_version = await orch.execute_blue_green_switch(
            operation_id, approval_id, mfa_receipt_id
        )
        uuid.UUID(switch_version)
        op = await orch.get_operation(operation_id)
        assert op.phase.value == "completed"

    @pytest.mark.asyncio
    async def test_no_authorities_approval_mismatch_raises(self, monkeypatch):
        """authorities=None + approval_id 不匹配 → RESTORE_APPROVAL_REQUIRED。"""
        from services.error_codes import AppError, ErrorCodes

        store, _ = await _make_store_with_tables()
        orch = _make_orchestrator(store)
        operation_id = await orch.start_operation(
            backup_id="backup_compat_002",
            manifest_digest="a" * 64,
            requested_by="tester",
            payload_digest="d" * 64,
            nonce="nonce_compat_002",
        )
        await orch.provision_staging(operation_id)
        for ds in ("crdb", "sqlite", "relay_sqlite"):
            await orch.restore_to_staging(operation_id, ds)
        await orch.validate_staging(operation_id)
        await orch.request_approval(operation_id, "approval_001", "mfa_001")
        with pytest.raises(AppError) as exc_info:
            await orch.execute_blue_green_switch(
                operation_id, "approval_WRONG", "mfa_001"
            )
        assert exc_info.value.code == ErrorCodes.RESTORE_APPROVAL_REQUIRED


# ════════════════════════════════════════════════════════════════
# Fixtures: 真实 SQLite store + MFA 签名密钥
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store_fixture(monkeypatch):
    """创建真实 CacheStore 临时数据库 + restore_operations 三表 + command_approvals。

    设置 ``_cs_module._store`` 使 ``get_cache_store()`` 返回测试 store
    (MFA 校验内部通过 ``_get_store()`` 获取同一 store)。
    """
    from database import cache_store as _cs_module

    tmpdir = tempfile.mkdtemp(prefix="r65_p0_3_real_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        store, _ = await _make_store_with_tables(db_path=str(db_path))
        _cs_module._store = store
        yield store
        await store.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_mfa_state_each_test():
    """每个用例前重置 MFA 模块级 L1 缓存(含吊销 ledger)。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


@pytest.fixture
def mfa_signing_key_env(monkeypatch):
    """设置 MFA receipt 签名密钥环境变量(仅测试用)。"""
    _set_mfa_signing_key(monkeypatch)
    yield
