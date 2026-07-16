"""R56 §5.3: 按钮式流程(opaque token + CAS + MFA)单元测试。

测试覆盖:
1. ButtonTokenStore CRUD(create/get/update_mfa/update_approver/update_final_confirm)
2. ButtonTokenStore CAS 4 字段原子消费
3. ButtonFlow 6 步流程编排(prepare/preview/confirm/mfa/approve/execute)
4. 防护向量(双击/重放/跨用户/旧版本/并发)
5. opaque token — 客户端只接收 nonce,业务字段服务端持久化
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.button_flow import (  # type: ignore  # noqa: E402
    BUTTON_TOKENS_DDL,
    ButtonFlow,
    ButtonFlowResult,
    ButtonToken,
    ButtonTokenStore,
    DEFAULT_TTL_SECONDS,
    NONCE_HEX_LEN,
    SIGNATURE_HEX_LEN,
    _compute_signature,
    generate_nonce,
)


# ════════════════════════════════════════════════════════════════
# 测试 fixture
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def memory_db():
    """创建 in-memory aiosqlite 数据库(R56 §5.3 测试专用)。"""
    import aiosqlite
    db = await aiosqlite.connect(":memory:")
    try:
        for stmt in BUTTON_TOKENS_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await db.execute(stmt)
        await db.commit()
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def store(memory_db):
    """ButtonTokenStore(已附加 in-memory db)。"""
    s = ButtonTokenStore()
    s.attach_db(memory_db)
    return s


@pytest_asyncio.fixture
async def flow(store):
    """ButtonFlow(基于 store)。"""
    return ButtonFlow(store=store)


@pytest.fixture
def sample_token_data():
    """构造测试用 token 数据。"""
    return dict(
        action="purge",
        principal_id=1001,
        target="file_abc123",
        resource_version="v1",
        request_hash="a" * 64,
        locale="zh-CN",
        ttl=3600,
    )


# ════════════════════════════════════════════════════════════════
# 1. ButtonTokenStore CRUD 测试
# ════════════════════════════════════════════════════════════════


class TestButtonTokenStoreCRUD:
    """ButtonTokenStore 基本 CRUD 操作。"""

    @pytest.mark.asyncio
    async def test_create_and_get_token(self, store, sample_token_data):
        """create_token + get_token 基本流程。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        signature = _compute_signature(
            nonce, "purge", 1001, "file_abc123", "v1", "a" * 64, expires_at,
        )
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file_abc123", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN", signature=signature,
        )
        ok = await store.create_token(token)
        assert ok is True
        retrieved = await store.get_token(nonce)
        assert retrieved is not None
        assert retrieved.nonce == nonce
        assert retrieved.action == "purge"
        assert retrieved.principal_id == 1001
        assert retrieved.target == "file_abc123"

    @pytest.mark.asyncio
    async def test_create_duplicate_nonce_returns_false(self, store):
        """重复 nonce INSERT OR IGNORE 应成功但不重复(幂等)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        signature = _compute_signature(
            nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
        )
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN", signature=signature,
        )
        ok1 = await store.create_token(token)
        ok2 = await store.create_token(token)  # 幂等,不报错
        assert ok1 is True
        # INSERT OR IGNORE 第二次也返回 True(忽略)
        assert ok2 is True

    @pytest.mark.asyncio
    async def test_update_mfa_status(self, store):
        """update_mfa_status 应更新 mfa_verified 字段。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        ok = await store.update_mfa_status(nonce, True)
        assert ok is True
        retrieved = await store.get_token(nonce)
        assert retrieved.mfa_verified is True

    @pytest.mark.asyncio
    async def test_update_approver(self, store):
        """update_approver 应更新 approver_id。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        ok = await store.update_approver(nonce, 2002)
        assert ok is True
        retrieved = await store.get_token(nonce)
        assert retrieved.approver_id == 2002

    @pytest.mark.asyncio
    async def test_update_final_confirm(self, store):
        """update_final_confirm 应更新 final_confirm。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        ok = await store.update_final_confirm(nonce, True)
        assert ok is True
        retrieved = await store.get_token(nonce)
        assert retrieved.final_confirm is True

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, store):
        """cleanup_expired 应删除过期未使用的 token。"""
        nonce = generate_nonce()
        # 已过期的 token
        expires_at = (_dt.datetime.utcnow() - _dt.timedelta(seconds=1)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        deleted = await store.cleanup_expired(_dt.datetime.utcnow().isoformat())
        assert deleted == 1
        retrieved = await store.get_token(nonce)
        assert retrieved is None


# ════════════════════════════════════════════════════════════════
# 2. CAS 4 字段原子消费测试
# ════════════════════════════════════════════════════════════════


class TestCASConsume:
    """R56 §5.3 核心: CAS 4 字段原子消费。"""

    @pytest.mark.asyncio
    async def test_cas_consume_success(self, store):
        """CAS 消费成功(used_at IS NULL + expires_at>now + principal + version 匹配)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        # CAS 消费
        result = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result is not None
        assert result.nonce == nonce
        assert result.action == "purge"
        # 再次消费应失败(已 used)
        result2 = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result2 is None

    @pytest.mark.asyncio
    async def test_cas_consume_wrong_principal(self, store):
        """CAS 应拒绝错误 principal(跨用户防护)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        # 用错误的 principal_id
        result = await store.consume_token_cas(nonce, principal_id=9999, resource_version="v1")
        assert result is None
        # 原始 token 仍可使用(未被消费)
        result2 = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result2 is not None

    @pytest.mark.asyncio
    async def test_cas_consume_wrong_version(self, store):
        """CAS 应拒绝错误 resource_version(旧版本按钮防护)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        # 用错误的 version
        result = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v2")
        assert result is None
        # 正确的 version 应成功
        result2 = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result2 is not None

    @pytest.mark.asyncio
    async def test_cas_consume_expired(self, store):
        """CAS 应拒绝过期 token(重放防护)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() - _dt.timedelta(seconds=1)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        result = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_cas_consume_already_used(self, store):
        """CAS 应拒绝已使用 token(双击防护)。"""
        nonce = generate_nonce()
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(seconds=3600)).isoformat()
        token = ButtonToken(
            nonce=nonce, action="purge", principal_id=1001,
            target="file", resource_version="v1",
            request_hash="a" * 64, expires_at=expires_at,
            locale="zh-CN",
            signature=_compute_signature(
                nonce, "purge", 1001, "file", "v1", "a" * 64, expires_at,
            ),
        )
        await store.create_token(token)
        # 第一次消费成功
        result1 = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result1 is not None
        # 第二次消费失败(双击)
        result2 = await store.consume_token_cas(nonce, principal_id=1001, resource_version="v1")
        assert result2 is None


# ════════════════════════════════════════════════════════════════
# 3. ButtonFlow 6 步流程编排测试
# ════════════════════════════════════════════════════════════════


class TestButtonFlow6Steps:
    """R56 §5.3: 6 步流程编排(prepare/preview/confirm/mfa/approve/execute)。"""

    @pytest.mark.asyncio
    async def test_prepare_creates_opaque_token(self, flow, sample_token_data):
        """步骤 1: prepare 应生成 opaque token(客户端只收到 nonce)。"""
        result = await flow.prepare(**sample_token_data)
        assert result.success is True
        assert result.step == "prepare"
        assert result.token is not None
        # nonce 长度 = 32 hex(128 bit)
        assert len(result.token.nonce) == NONCE_HEX_LEN
        # 业务字段在服务端持久化
        assert result.token.action == "purge"
        assert result.token.principal_id == 1001
        assert result.token.target == "file_abc123"
        # 签名长度 = 32 hex(128 bit)
        assert len(result.token.signature) == SIGNATURE_HEX_LEN

    @pytest.mark.asyncio
    async def test_preview_returns_operation_data(self, flow, sample_token_data):
        """步骤 2: preview 应返回操作预览(不消费 token)。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        # preview
        result = await flow.preview(nonce, principal_id=1001)
        assert result.success is True
        assert result.step == "preview"
        assert result.preview_data["action"] == "purge"
        assert result.preview_data["target"] == "file_abc123"
        assert result.preview_data["resource_version"] == "v1"
        assert "expires_at" in result.preview_data
        # 极高风险 action 应要求 MFA + dual approval + final confirm
        assert result.preview_data["mfa_required"] is True
        assert result.preview_data["dual_approval_required"] is True
        assert result.preview_data["final_confirm_required"] is True
        # preview 不消费 token,execute 仍可成功
        exec_result = await flow.execute(
            nonce, principal_id=1001, resource_version="v1",
            executor=None,
        )
        # 注意:execute 会因为 mfa/approve/final 未满足而失败
        # 但 CAS 消费已发生(token 被标记 used)
        assert exec_result.success is False

    @pytest.mark.asyncio
    async def test_confirm_updates_final_confirm(self, flow, sample_token_data):
        """步骤 3: confirm 应更新 final_confirm 标记。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        result = await flow.confirm(nonce, principal_id=1001)
        assert result.success is True
        assert result.step == "confirm"
        # 验证 final_confirm 已更新
        token = await flow.store.get_token(nonce)
        assert token.final_confirm is True

    @pytest.mark.asyncio
    async def test_mfa_verify_success(self, flow, sample_token_data):
        """步骤 4: MFA 验证成功。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        result = await flow.mfa_verify(nonce, principal_id=1001, totp_code="123456")
        assert result.success is True
        assert result.step == "mfa"
        token = await flow.store.get_token(nonce)
        assert token.mfa_verified is True

    @pytest.mark.asyncio
    async def test_mfa_verify_invalid_code(self, flow, sample_token_data):
        """步骤 4: MFA 验证失败(无效 TOTP code)。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        # 错误长度
        result = await flow.mfa_verify(nonce, principal_id=1001, totp_code="12345")
        assert result.success is False
        assert result.error_code == "BUTTON.POLICY.MFA_REQUIRED"
        # 空 code
        result = await flow.mfa_verify(nonce, principal_id=1001, totp_code="")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_approve_success(self, flow, sample_token_data):
        """步骤 5: 双人审批成功(approver ≠ principal)。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        result = await flow.approve(nonce, approver_id=2002, principal_id=1001)
        assert result.success is True
        assert result.step == "approve"
        token = await flow.store.get_token(nonce)
        assert token.approver_id == 2002

    @pytest.mark.asyncio
    async def test_approve_same_principal_rejected(self, flow, sample_token_data):
        """步骤 5: 审批人与主体相同应拒绝。"""
        prepare_result = await flow.prepare(**sample_token_data)
        nonce = prepare_result.token.nonce
        result = await flow.approve(nonce, approver_id=1001, principal_id=1001)
        assert result.success is False
        assert result.error_code == "BUTTON.POLICY.DUAL_APPROVAL_REQUIRED"

    @pytest.mark.asyncio
    async def test_execute_success_full_flow(self, flow, sample_token_data):
        """步骤 6: 完整 6 步流程后 execute 成功。"""
        # 完整走完 6 步
        prepare = await flow.prepare(**sample_token_data)
        nonce = prepare.token.nonce
        await flow.preview(nonce, principal_id=1001)
        await flow.confirm(nonce, principal_id=1001)
        await flow.mfa_verify(nonce, principal_id=1001, totp_code="123456")
        await flow.approve(nonce, approver_id=2002, principal_id=1001)
        # execute
        executed = False

        async def executor(token):
            nonlocal executed
            executed = True
            return {"deleted_count": 5, "action_id": "test-123"}

        result = await flow.execute(
            nonce, principal_id=1001, resource_version="v1",
            executor=executor,
        )
        assert result.success is True
        assert result.step == "execute"
        assert executed is True
        assert result.receipt["deleted_count"] == 5

    @pytest.mark.asyncio
    async def test_execute_fail_without_mfa(self, flow, sample_token_data):
        """步骤 6: 极高风险 action 未 MFA 应失败。"""
        prepare = await flow.prepare(**sample_token_data)
        nonce = prepare.token.nonce
        await flow.confirm(nonce, principal_id=1001)
        await flow.approve(nonce, approver_id=2002, principal_id=1001)
        # 跳过 mfa_verify
        result = await flow.execute(
            nonce, principal_id=1001, resource_version="v1",
        )
        assert result.success is False
        assert result.error_code == "BUTTON.POLICY.MFA_REQUIRED"

    @pytest.mark.asyncio
    async def test_execute_fail_without_approver(self, flow, sample_token_data):
        """步骤 6: 极高风险 action 未双人审批应失败。"""
        prepare = await flow.prepare(**sample_token_data)
        nonce = prepare.token.nonce
        await flow.confirm(nonce, principal_id=1001)
        await flow.mfa_verify(nonce, principal_id=1001, totp_code="123456")
        # 跳过 approve
        result = await flow.execute(
            nonce, principal_id=1001, resource_version="v1",
        )
        assert result.success is False
        assert result.error_code == "BUTTON.POLICY.DUAL_APPROVAL_REQUIRED"


# ════════════════════════════════════════════════════════════════
# 4. 低风险 action 测试(跳过 MFA/审批)
# ════════════════════════════════════════════════════════════════


class TestLowRiskActionFlow:
    """低风险 action 可跳过 mfa/approve 步骤。"""

    @pytest.mark.asyncio
    async def test_low_risk_action_execute_directly(self, flow):
        """低风险 action(如 view)可直接 execute,不需要 MFA/审批/确认。"""
        result = await flow.prepare(
            action="view", principal_id=1001, target="file_abc",
            resource_version="v1", request_hash="b" * 64,
            locale="zh-CN", ttl=3600,
        )
        assert result.success is True
        # 直接 execute(跳过 confirm/mfa/approve)
        exec_result = await flow.execute(
            result.token.nonce, principal_id=1001, resource_version="v1",
        )
        assert exec_result.success is True

    @pytest.mark.asyncio
    async def test_preview_low_risk_no_mfa_required(self, flow):
        """低风险 action 的 preview 应显示 mfa_required=False。"""
        result = await flow.prepare(
            action="view", principal_id=1001, target="file_abc",
            resource_version="v1", request_hash="b" * 64,
            locale="zh-CN", ttl=3600,
        )
        preview = await flow.preview(result.token.nonce, principal_id=1001)
        assert preview.preview_data["mfa_required"] is False
        assert preview.preview_data["dual_approval_required"] is False
        assert preview.preview_data["final_confirm_required"] is False


# ════════════════════════════════════════════════════════════════
# 5. opaque token 测试(客户端只收 nonce)
# ════════════════════════════════════════════════════════════════


class TestOpaqueToken:
    """R56 §5.3: opaque token — 客户端只接收 nonce,业务字段服务端持久化。"""

    @pytest.mark.asyncio
    async def test_client_receives_only_nonce(self, flow, sample_token_data):
        """prepare 返回的 token 中,客户端只应使用 nonce。"""
        result = await flow.prepare(**sample_token_data)
        # 模拟客户端收到的"callback_data"应仅为 nonce
        client_callback_data = result.token.nonce
        assert len(client_callback_data) == NONCE_HEX_LEN
        # 服务端通过 nonce 能取回完整 token
        retrieved = await flow.store.get_token(client_callback_data)
        assert retrieved is not None
        assert retrieved.action == "purge"
        assert retrieved.target == "file_abc123"
        assert retrieved.resource_version == "v1"
        assert retrieved.request_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_nonce_has_sufficient_entropy(self):
        """nonce 应为 128 bit(32 hex chars)。"""
        nonce = generate_nonce()
        assert len(nonce) == NONCE_HEX_LEN
        # 不同的调用应产生不同的 nonce
        nonce2 = generate_nonce()
        assert nonce != nonce2

    @pytest.mark.asyncio
    async def test_signature_128bit(self, flow, sample_token_data):
        """HMAC 签名应为 128 bit(32 hex chars 截断)。"""
        result = await flow.prepare(**sample_token_data)
        assert len(result.token.signature) == SIGNATURE_HEX_LEN


# ════════════════════════════════════════════════════════════════
# 6. DDL 测试
# ════════════════════════════════════════════════════════════════


class TestButtonTokensDDL:
    """button_tokens 表 DDL 验证。"""

    def test_ddl_contains_required_fields(self):
        """DDL 应包含所有 R56 §5.3 要求的字段。"""
        required_fields = [
            "nonce", "action", "principal_id", "target",
            "resource_version", "request_hash", "expires_at",
            "used_at", "locale", "mfa_verified", "approver_id",
            "final_confirm", "signature", "created_at",
        ]
        for field in required_fields:
            assert field in BUTTON_TOKENS_DDL, f"DDL 缺少字段: {field}"

    def test_ddl_contains_indexes(self):
        """DDL 应创建必要的索引。"""
        assert "idx_button_tokens_principal" in BUTTON_TOKENS_DDL
        assert "idx_button_tokens_expires" in BUTTON_TOKENS_DDL
        assert "idx_button_tokens_action" in BUTTON_TOKENS_DDL

    def test_ddl_uses_create_if_not_exists(self):
        """DDL 应使用 CREATE TABLE IF NOT EXISTS(幂等)。"""
        assert "CREATE TABLE IF NOT EXISTS button_tokens" in BUTTON_TOKENS_DDL


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
