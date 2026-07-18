"""R62 P1-07: MFA receipt 语义强化 + 审批自拒防护 测试。

被测目标(audit P1-07):
- ``services.approval_workflow.reject()`` — R62 P1-07: 与 approve() 对称强制
  requester != approver,防止自拒绕过审计
- ``services.approval_workflow.approve()`` — 既有自审批防护不应被破坏
- ``admin/mfa.py::MFAManager.verify_mfa_receipt_age()`` — R62 P1-07: 校验 MFA
  receipt 签发年龄(默认 5 分钟内),防止陈旧 receipt 绕过二次认证
- ``admin/mfa.py::MFAManager.revoke_mfa_receipt()`` — R62 P1-07: 显式吊销 receipt,
  写入 revocation ledger(L1 + SQLite),后续 verify_mfa_receipt fail-closed
- ``admin/mfa.py::verify_mfa_receipt()`` — R62 P1-07: 吊销 ledger 检查
- ``admin/mfa.py::consume_mfa_receipt()`` — 并发原子消费(同一 jti 仅一次成功)
- ``services.button_approval_policy.enforce_button_approval_policy()`` — R62 P1-07:
  高风险动作验证 MFA age,不只验证布尔 mfa_verified

测试场景:
1. reject() with requester == approver raises AppError(APPROVAL_SELF_REJECT_FORBIDDEN)
2. approve() with requester == approver 返回 False(既有自审批防护)
3. verify_mfa_receipt_age() with recent receipt returns True
4. verify_mfa_receipt_age() with old receipt returns False
5. verify_mfa_receipt_age() with missing iat returns False
6. revoke_mfa_receipt() causes subsequent verify_mfa_receipt() to fail
7. 并发测试: 两个线程调用 consume_mfa_receipt() for same jti,仅一个成功
8. 高风险动作 with stale MFA receipt raises AppError(AUTH_MFA_RECEIPT_EXPIRED)

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 CacheStore.init() 创建表
- Mock rbac.check_permission 让 approve/reject 通过权限检查
- 通过 monkeypatch 设置 MFA_RECEIPT_SIGNING_KEY 环境变量
- 并发测试使用 ThreadPoolExecutor + asyncio.run 隔离 event loop
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r62_p1_7_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def mock_rbac_permission(real_store):
    """Mock rbac.check_permission 返回 True(让 approve/reject 通过权限检查)。

    approval_workflow 在模块顶层 ``from services.rbac import check_permission``,
    所以需要 patch ``services.approval_workflow.check_permission``。
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.approval_workflow.check_permission",
            AsyncMock(return_value=True),
        )
        yield


@pytest.fixture(autouse=True)
def reset_mfa_state():
    """每个用例前重置 MFA 模块级 L1 缓存状态(含 R62 P1-07 吊销 ledger)。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


@pytest.fixture(autouse=True)
def _reset_approval_executor_singleton():
    """每个用例前重置 ApprovalExecutor 单例,避免跨用例污染。"""
    from services import approval_executor
    approval_executor.reset_approval_executor()
    yield
    approval_executor.reset_approval_executor()


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


@pytest.fixture
def mfa_signing_key(monkeypatch):
    """设置 MFA receipt 签名密钥环境变量(仅测试用)。

    issue_mfa_receipt / verify_mfa_receipt 通过 _get_mfa_receipt_keyring()
    读取 MFA_RECEIPT_SIGNING_KEY 环境变量,缺失时 fail-closed。
    """
    monkeypatch.setenv(
        "MFA_RECEIPT_SIGNING_KEY",
        "r62_p1_07_test_signing_key_32bytes_min",
    )
    # 清除 previous 密钥,确保密钥环只有 current
    monkeypatch.delenv("MFA_RECEIPT_SIGNING_KEY_PREVIOUS", raising=False)
    yield


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _create_simple_approval(
    store,
    action: str = "takedown",
    created_by: int = 100,
) -> int:
    """创建一个简单审批(无 command_action,用于 approve/reject 测试)。"""
    from services import approval_workflow
    payload = {"target_type": "file_code", "target_id": "R62_001", "reason": "test"}
    approval_id = await approval_workflow.create_approval(
        action=action,
        payload=payload,
        created_by=created_by,
    )
    assert approval_id > 0, f"创建审批失败 action={action}"
    return approval_id


# ════════════════════════════════════════════════════════════════
# 1. reject() 自拒防护(R62 P1-07)
# ════════════════════════════════════════════════════════════════

class TestRejectSelfApprovalPrevention:
    """R62 P1-07: reject() 必须强制 requester != approver。"""

    @pytest.mark.asyncio
    async def test_reject_with_requester_equals_approver_raises(
        self, real_store, mock_rbac_permission,
    ):
        """reject() approver == created_by → raise APPROVAL_SELF_REJECT_FORBIDDEN。

        R62 P1-07: 与 approve() 自审批防护对称,防止创建者通过自拒绕过审计。
        """
        from services import approval_workflow
        from services.error_codes import AppError, ErrorCodes

        approval_id = await _create_simple_approval(real_store, created_by=100)

        # 创建者自己驳回 → 必须 raise AppError
        with pytest.raises(AppError) as exc_info:
            await approval_workflow.reject(approval_id, approver_id=100, reason="自拒")
        assert exc_info.value.code == ErrorCodes.APPROVAL_SELF_REJECT_FORBIDDEN
        # 验证错误参数包含审批上下文
        params = exc_info.value.params
        assert params.get("approval_id") == approval_id
        assert params.get("approver_id") == 100
        assert params.get("created_by") == 100

        # 验证审批状态未变(仍是 pending)— raise 在 CAS UPDATE 之前
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "pending"

    @pytest.mark.asyncio
    async def test_reject_with_different_approver_succeeds(
        self, real_store, mock_rbac_permission,
    ):
        """reject() approver != created_by → 成功(正常路径)。"""
        from services import approval_workflow

        approval_id = await _create_simple_approval(real_store, created_by=100)

        ok = await approval_workflow.reject(approval_id, approver_id=200, reason="不同审批人驳回")
        assert ok is True

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "rejected"
        assert approval["approver_id"] == 200


# ════════════════════════════════════════════════════════════════
# 2. approve() 自审批防护(既有行为,验证未被破坏)
# ════════════════════════════════════════════════════════════════

class TestApproveSelfApprovalPrevention:
    """R62 P1-07: approve() 既有自审批防护不应被破坏(回归测试)。"""

    @pytest.mark.asyncio
    async def test_approve_with_requester_equals_approver_returns_false(
        self, real_store, mock_rbac_permission,
    ):
        """approve() approver == created_by → 返回 False(既有自审批防护)。

        注: approve() 既有实现返回 False(不 raise),reject() 新实现 raise AppError。
        两者都阻断自审批,但错误返回方式不同(保持各自既有语义)。
        """
        from services import approval_workflow

        approval_id = await _create_simple_approval(real_store, created_by=100)

        # 创建者自己批准 → 返回 False(既有行为)
        ok = await approval_workflow.approve(approval_id, approver_id=100, note="自批")
        assert ok is False

        # 审批状态未变
        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "pending"

    @pytest.mark.asyncio
    async def test_approve_with_different_approver_succeeds(
        self, real_store, mock_rbac_permission,
    ):
        """approve() approver != created_by → 成功(正常路径)。"""
        from services import approval_workflow

        approval_id = await _create_simple_approval(real_store, created_by=100)

        ok = await approval_workflow.approve(approval_id, approver_id=200, note="不同审批人批准")
        assert ok is True

        approval = await approval_workflow.get_approval(approval_id)
        assert approval is not None
        assert approval["status"] == "approved"
        assert approval["approver_id"] == 200


# ════════════════════════════════════════════════════════════════
# 3. verify_mfa_receipt_age() 年龄校验(R62 P1-07)
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAge:
    """R62 P1-07: MFAManager.verify_mfa_receipt_age() 年龄校验。"""

    def test_recent_receipt_returns_true(self, mfa_signing_key):
        """近期签发的 receipt(2 秒前)→ age 校验通过。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        now = int(time.time())
        receipt = {"iat": now - 2, "jti": "test_recent", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=300) is True

    def test_old_receipt_returns_false(self, mfa_signing_key):
        """陈旧 receipt(10 分钟前签发,超过 5 分钟上限)→ age 校验失败。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        now = int(time.time())
        # 600 秒前签发,超过默认 300 秒上限
        receipt = {"iat": now - 600, "jti": "test_old", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=300) is False

    def test_old_receipt_with_custom_max_age_returns_true(self, mfa_signing_key):
        """陈旧 receipt 但 max_age 放宽到 900 秒 → 通过(可配置上限)。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        now = int(time.time())
        receipt = {"iat": now - 600, "jti": "test_custom_age", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=900) is True

    def test_missing_iat_returns_false(self, mfa_signing_key):
        """receipt 缺少 iat 字段 → fail-closed 返回 False。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        receipt = {"jti": "test_no_iat", "sub": 100}  # 无 iat
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=300) is False

    def test_invalid_iat_type_returns_false(self, mfa_signing_key):
        """iat 类型非法(字符串/None/bool)→ fail-closed 返回 False。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        # 字符串
        assert manager.verify_mfa_receipt_age(
            {"iat": "1700000000"}, max_age_seconds=300,
        ) is False
        # None
        assert manager.verify_mfa_receipt_age(
            {"iat": None}, max_age_seconds=300,
        ) is False
        # bool(True — isinstance(True, int) 为 True,但 bool 子类被显式排除)
        assert manager.verify_mfa_receipt_age(
            {"iat": True}, max_age_seconds=300,
        ) is False

    def test_future_iat_returns_false(self, mfa_signing_key):
        """iat 在未来(时钟漂移或伪造)→ fail-closed 返回 False。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        now = int(time.time())
        receipt = {"iat": now + 100, "jti": "test_future", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=300) is False

    def test_non_dict_receipt_returns_false(self, mfa_signing_key):
        """receipt 非 dict → fail-closed 返回 False。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        assert manager.verify_mfa_receipt_age(None, max_age_seconds=300) is False
        assert manager.verify_mfa_receipt_age("not_a_dict", max_age_seconds=300) is False
        assert manager.verify_mfa_receipt_age([], max_age_seconds=300) is False

    def test_boundary_age_exact_max_returns_true(self, mfa_signing_key):
        """age 恰好等于 max_age_seconds(边界)→ 通过(<=)。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        now = int(time.time())
        # age 恰好为 300(<=300 通过)
        receipt = {"iat": now - 300, "jti": "test_boundary", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt, max_age_seconds=300) is True
        # age=301 失败
        receipt_over = {"iat": now - 301, "jti": "test_over", "sub": 100}
        assert manager.verify_mfa_receipt_age(receipt_over, max_age_seconds=300) is False


# ════════════════════════════════════════════════════════════════
# 4. revoke_mfa_receipt() 吊销 ledger(R62 P1-07)
# ════════════════════════════════════════════════════════════════

class TestRevokeMfaReceipt:
    """R62 P1-07: MFAManager.revoke_mfa_receipt() + verify_mfa_receipt() 吊销检查。"""

    @pytest.mark.asyncio
    async def test_revoke_causes_subsequent_verify_to_fail(
        self, real_store, mfa_signing_key,
    ):
        """revoke_mfa_receipt(jti) → 后续 verify_mfa_receipt(token) raise AppError(reason=revoked)。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt, get_mfa_manager
        from services.error_codes import AppError, ErrorCodes

        principal_id = 100
        action_hash = "a" * 64  # 64 位 hex(SHA-256 格式)
        token = issue_mfa_receipt(
            principal_id=principal_id,
            purpose="break_glass_approval",
            action_hash=action_hash,
            amr=["totp"],
            ttl_seconds=300,
        )

        # 1. 吊销前:verify 应成功
        payload = verify_mfa_receipt(
            token=token,
            expected_principal_id=principal_id,
            expected_purpose="break_glass_approval",
            expected_action_hash=action_hash,
        )
        jti = payload["jti"]
        assert jti, "verify_mfa_receipt 应返回含 jti 的 payload"

        # 2. 吊销 receipt
        manager = get_mfa_manager()
        ok = await manager.revoke_mfa_receipt(jti, reason="test_revoke")
        assert ok is True

        # 3. 吊销后:verify 应 fail-closed,reason=revoked
        with pytest.raises(AppError) as exc_info:
            verify_mfa_receipt(
                token=token,
                expected_principal_id=principal_id,
                expected_purpose="break_glass_approval",
                expected_action_hash=action_hash,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent(self, real_store, mfa_signing_key):
        """同一 jti 多次吊销 → 幂等(均返回 True)。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        jti = "a" * 32  # 32 位 hex 格式

        ok1 = await manager.revoke_mfa_receipt(jti, reason="first")
        assert ok1 is True
        ok2 = await manager.revoke_mfa_receipt(jti, reason="second")
        assert ok2 is True  # 幂等

    @pytest.mark.asyncio
    async def test_revoke_empty_jti_returns_false(self, real_store, mfa_signing_key):
        """空 jti → 返回 False(参数校验)。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        assert await manager.revoke_mfa_receipt("", reason="empty") is False
        assert await manager.revoke_mfa_receipt(None, reason="none") is False

    @pytest.mark.asyncio
    async def test_is_mfa_receipt_revoked_authoritative_check(
        self, real_store, mfa_signing_key,
    ):
        """is_mfa_receipt_revoked() 权威查询(L1 + SQLite)。"""
        from admin.mfa import get_mfa_manager

        manager = get_mfa_manager()
        jti = "b" * 32

        # 未吊销
        assert await manager.is_mfa_receipt_revoked(jti) is False

        # 吊销后查询
        await manager.revoke_mfa_receipt(jti, reason="test")
        assert await manager.is_mfa_receipt_revoked(jti) is True

    @pytest.mark.asyncio
    async def test_revoke_persists_across_l1_reset(
        self, real_store, mfa_signing_key,
    ):
        """吊销写入 SQLite,L1 重置后通过 is_mfa_receipt_revoked 仍可查到(权威层)。"""
        from admin.mfa import get_mfa_manager
        from admin import mfa as _mfa_mod

        manager = get_mfa_manager()
        jti = "c" * 32
        await manager.revoke_mfa_receipt(jti, reason="persist_test")

        # L1 重置(模拟进程重启)
        _mfa_mod.reset_mfa_state_for_testing()

        # 通过 async 权威查询应仍能查到(SQLite 持久化)
        assert await manager.is_mfa_receipt_revoked(jti) is True


# ════════════════════════════════════════════════════════════════
# 5. consume_mfa_receipt() 并发原子消费(R62 P1-07 并发测试)
# ════════════════════════════════════════════════════════════════

class TestConsumeMfaReceiptConcurrency:
    """R62 P1-07: 同一 jti 并发消费,仅一个成功(INSERT OR IGNORE + rowcount)。"""

    @pytest.mark.asyncio
    async def test_concurrent_consume_only_one_succeeds(self, real_store, mfa_signing_key):
        """两个并发 consume_mfa_receipt(same jti) → 仅一个返回 True。

        使用真实 SQLite + aiosqlite,通过 asyncio.gather 并发触发。
        jti PRIMARY KEY + INSERT OR IGNORE 保证原子性。
        """
        from admin.mfa import consume_mfa_receipt

        jti = "d" * 32  # 32 位 hex 格式

        # 并发调用两次 consume
        results = await asyncio.gather(
            consume_mfa_receipt(jti),
            consume_mfa_receipt(jti),
        )
        # 恰好一个 True,一个 False
        assert results.count(True) == 1, f"应恰好一个成功,实际 {results}"
        assert results.count(False) == 1, f"应恰好一个失败,实际 {results}"

    @pytest.mark.asyncio
    async def test_concurrent_consume_thread_pool(self, real_store, mfa_signing_key):
        """多线程并发 consume_mfa_receipt(same jti) → 仅一个成功。

        使用 ThreadPoolExecutor 模拟跨线程并发(每线程独立 event loop)。
        验证 SQLite rowcount 原子性在多线程下仍成立。
        """
        from admin.mfa import consume_mfa_receipt

        jti = "e" * 32

        def _consume_in_new_loop() -> bool:
            """在新 event loop 中调用 async consume_mfa_receipt。"""
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(consume_mfa_receipt(jti))
            finally:
                loop.close()

        # 4 个线程并发
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_consume_in_new_loop) for _ in range(4)]
            results = [f.result() for f in futures]

        # 恰好一个 True
        success_count = sum(1 for r in results if r is True)
        assert success_count == 1, (
            f"4 线程并发应恰好一个成功,实际 success_count={success_count}, results={results}"
        )

    @pytest.mark.asyncio
    async def test_consume_empty_jti_returns_false(self, real_store, mfa_signing_key):
        """空 jti → 返回 False(参数校验)。"""
        from admin.mfa import consume_mfa_receipt
        assert await consume_mfa_receipt("") is False


# ════════════════════════════════════════════════════════════════
# 6. 高风险动作 MFA age 校验(R62 P1-07,button_approval_policy)
# ════════════════════════════════════════════════════════════════

class TestHighRiskActionMfaAgeCheck:
    """R62 P1-07: 高风险动作验证 MFA age,enforce_button_approval_policy 集成。"""

    @pytest.mark.asyncio
    async def test_stale_mfa_receipt_raises_in_policy_check(self, mfa_signing_key):
        """高风险动作 + 陈旧 mfa_receipt → enforce_button_approval_policy raise AUTH_MFA_RECEIPT_EXPIRED。

        直接测试 enforce_button_approval_policy 中 R62 P1-07 新增的 age 校验逻辑:
        - requires_mfa=True(action=break_glass)
        - mfa_verified=True(通过布尔门禁)
        - mfa_receipt 提供但 iat 过旧(10 分钟前)
        → raise AUTH_MFA_RECEIPT_EXPIRED

        注:本用例不调用 verify_button_token(底层签名校验),通过直接
        monkeypatch verify_button_token 返回 (True, action, data) 跳过底层,
        专注验证 age 校验逻辑。
        """
        from services.button_approval_policy import (
            ButtonApprovalContext,
            enforce_button_approval_policy,
        )
        from services.error_codes import AppError, ErrorCodes

        now = int(time.time())
        # 陈旧 receipt:10 分钟前签发,超过 300 秒上限
        stale_receipt = {"iat": now - 600, "jti": "stale_jti", "sub": 1001}

        ctx = ButtonApprovalContext(
            action="break_glass",  # 极高风险,requires_mfa=True
            principal_id=1001,
            resource="emergency",
            resource_version="v1",
            request_hash="abc123def456",
            expiry_ts=now + 3600,
            nonce="test_nonce_123",
            signature="a" * 32,
            mfa_verified=True,  # 通过布尔门禁
            approver_id=2002,  # 双人审批通过
            final_confirm=True,
            mfa_receipt=stale_receipt,  # R62 P1-07: 陈旧 receipt
        )

        # Mock verify_button_token 跳过底层签名校验,直接返回成功
        async def _fake_verify_button_token(callback_data, current_principal_id, store=None):
            return True, "break_glass", "fake_data"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "services.button_approval_policy.verify_button_token",
                _fake_verify_button_token,
            )
            with pytest.raises(AppError) as exc_info:
                await enforce_button_approval_policy(
                    ctx,
                    current_principal_id=1001,
                    callback_data="1001:break_glass:emergency:9999999999:test_nonce:fake_sig",
                )
            assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED
            assert exc_info.value.params.get("reason") == "mfa_receipt_age_exceeded"

    @pytest.mark.asyncio
    async def test_fresh_mfa_receipt_passes_policy_check(self, mfa_signing_key):
        """高风险动作 + 近期 mfa_receipt → 通过 age 校验(继续后续步骤)。"""
        from services.button_approval_policy import (
            ButtonApprovalContext,
            enforce_button_approval_policy,
        )

        now = int(time.time())
        fresh_receipt = {"iat": now - 30, "jti": "fresh_jti", "sub": 1001}

        ctx = ButtonApprovalContext(
            action="break_glass",
            principal_id=1001,
            resource="emergency",
            resource_version="v1",
            request_hash="abc123def456",
            expiry_ts=now + 3600,
            nonce="test_nonce_123",
            signature="a" * 32,
            mfa_verified=True,
            approver_id=2002,
            final_confirm=True,
            mfa_receipt=fresh_receipt,  # 近期 receipt
        )

        async def _fake_verify_button_token(callback_data, current_principal_id, store=None):
            return True, "break_glass", "fake_data"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "services.button_approval_policy.verify_button_token",
                _fake_verify_button_token,
            )
            valid, action, data = await enforce_button_approval_policy(
                ctx,
                current_principal_id=1001,
                callback_data="1001:break_glass:emergency:9999999999:test_nonce:fake_sig",
            )
            # age 校验通过,后续双人审批也通过,最终返回 valid=True
            assert valid is True
            assert action == "break_glass"

    @pytest.mark.asyncio
    async def test_no_mfa_receipt_falls_back_to_boolean(self, mfa_signing_key):
        """高风险动作 + mfa_receipt=None → 仅依赖布尔 mfa_verified(向后兼容)。"""
        from services.button_approval_policy import (
            ButtonApprovalContext,
            enforce_button_approval_policy,
        )

        now = int(time.time())
        ctx = ButtonApprovalContext(
            action="break_glass",
            principal_id=1001,
            resource="emergency",
            resource_version="v1",
            request_hash="abc123def456",
            expiry_ts=now + 3600,
            nonce="test_nonce_123",
            signature="a" * 32,
            mfa_verified=True,
            approver_id=2002,
            final_confirm=True,
            mfa_receipt=None,  # 无 receipt,向后兼容
        )

        async def _fake_verify_button_token(callback_data, current_principal_id, store=None):
            return True, "break_glass", "fake_data"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "services.button_approval_policy.verify_button_token",
                _fake_verify_button_token,
            )
            valid, _, _ = await enforce_button_approval_policy(
                ctx,
                current_principal_id=1001,
                callback_data="1001:break_glass:emergency:9999999999:test_nonce:fake_sig",
            )
            assert valid is True


# ════════════════════════════════════════════════════════════════
# 7. 静态检查:新方法/字段定义存在性
# ════════════════════════════════════════════════════════════════

class TestStaticChecks:
    """R62 P1-07: 静态检查新方法/字段定义存在性。"""

    def test_mfa_manager_has_verify_mfa_receipt_age(self):
        """MFAManager 应定义 verify_mfa_receipt_age sync 方法。"""
        from admin.mfa import MFAManager
        assert hasattr(MFAManager, "verify_mfa_receipt_age"), \
            "MFAManager 应定义 verify_mfa_receipt_age 方法(R62 P1-07)"
        # 验证是普通方法(非 async)
        import inspect
        assert not inspect.iscoroutinefunction(MFAManager.verify_mfa_receipt_age), \
            "verify_mfa_receipt_age 应为 sync 方法"

    def test_mfa_manager_has_revoke_mfa_receipt(self):
        """MFAManager 应定义 revoke_mfa_receipt async 方法。"""
        from admin.mfa import MFAManager
        assert hasattr(MFAManager, "revoke_mfa_receipt"), \
            "MFAManager 应定义 revoke_mfa_receipt 方法(R62 P1-07)"
        import inspect
        assert inspect.iscoroutinefunction(MFAManager.revoke_mfa_receipt), \
            "revoke_mfa_receipt 应为 async 方法"

    def test_mfa_manager_has_is_mfa_receipt_revoked(self):
        """MFAManager 应定义 is_mfa_receipt_revoked async 方法。"""
        from admin.mfa import MFAManager
        assert hasattr(MFAManager, "is_mfa_receipt_revoked"), \
            "MFAManager 应定义 is_mfa_receipt_revoked 方法(R62 P1-07)"

    def test_button_approval_context_has_mfa_receipt_field(self):
        """ButtonApprovalContext 应包含 mfa_receipt 字段(R62 P1-07)。"""
        from services.button_approval_policy import ButtonApprovalContext
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(ButtonApprovalContext)}
        assert "mfa_receipt" in field_names, \
            "ButtonApprovalContext 应包含 mfa_receipt 字段(R62 P1-07)"

    def test_error_codes_has_new_codes(self):
        """ErrorCodes 应包含 R62 P1-07 新增错误码。"""
        from services.error_codes import ErrorCodes
        assert hasattr(ErrorCodes, "APPROVAL_SELF_REJECT_FORBIDDEN")
        assert hasattr(ErrorCodes, "AUTH_MFA_RECEIPT_EXPIRED")
        assert ErrorCodes.APPROVAL_SELF_REJECT_FORBIDDEN == "APPROVAL.SELF_REJECT.FORBIDDEN"
        assert ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED == "AUTH.MFA.RECEIPT_EXPIRED"

    def test_error_registry_has_new_codes_registered(self):
        """ErrorRegistry 应已注册 R62 P1-07 新增错误码。"""
        from services.error_codes import ErrorRegistry, ErrorCodes
        assert ErrorRegistry.is_registered(ErrorCodes.APPROVAL_SELF_REJECT_FORBIDDEN), \
            "APPROVAL_SELF_REJECT_FORBIDDEN 必须注册到 ErrorRegistry"
        assert ErrorRegistry.is_registered(ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED), \
            "AUTH_MFA_RECEIPT_EXPIRED 必须注册到 ErrorRegistry"

    def test_cache_store_has_revocations_table_ddl(self):
        """cache_store.py 应包含 mfa_receipt_revocations 建表 DDL。"""
        repo_root = Path(__file__).resolve().parent.parent
        cache_store_file = repo_root / "database" / "cache_store.py"
        source = cache_store_file.read_text(encoding="utf-8")
        assert "mfa_receipt_revocations" in source, \
            "cache_store.py 应包含 mfa_receipt_revocations 表 DDL(R62 P1-07)"

    def test_verify_mfa_receipt_checks_revocation(self):
        """verify_mfa_receipt 源码应包含吊销 ledger 检查逻辑。"""
        repo_root = Path(__file__).resolve().parent.parent
        mfa_file = repo_root / "admin" / "mfa.py"
        source = mfa_file.read_text(encoding="utf-8")
        assert "_mfa_receipt_revocations" in source, \
            "mfa.py 应引用 _mfa_receipt_revocations L1 缓存(R62 P1-07)"
        assert 'reason": "revoked"' in source or "reason': 'revoked'" in source or \
               '"reason": "revoked"' in source, \
            "verify_mfa_receipt 应在吊销时返回 reason=revoked"
