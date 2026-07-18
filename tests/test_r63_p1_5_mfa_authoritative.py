"""R63 P1-05: MFA 吊销权威层统一验证入口 测试。

被测目标(audit P1-05):
- ``admin/mfa.py::verify_mfa_receipt_authoritative()`` — R63 P1-05 新增的唯一权威
  async MFA receipt 验证入口,内部完成签名 + age + kid + SQLite revocation +
  principal/purpose/action_hash 匹配 + 一次性消费。生产代码 MUST 使用本函数,
  禁止调用低层 sync ``verify_mfa_receipt()``(已 @deprecated)。
- ``admin/mfa.py::verify_mfa_receipt()`` — R63 P1-05 标记为 @deprecated,
  调用时触发 DeprecationWarning。保留仅供测试/非生产路径。

修复的问题(P1-05):
  旧 sync verify_mfa_receipt() 只查进程内 L1 吊销缓存,注释要求跨进程调用方
  在验证前另行 await is_mfa_receipt_revoked()。安全正确性不应由每个调用点
  自行组合。新 async verify_mfa_receipt_authoritative() 内部完成权威 SQLite
  吊销查询,调用方无需"记得"查询权威层。

测试场景:
1. 权威验证 happy path:合法 token → 返回 payload
2. 签名/字段校验失败(sub/purpose/action_hash 不匹配)→ raise AUTH_MFA_RECEIPT_INVALID
3. age 超限(陈旧 receipt)→ raise AUTH_MFA_RECEIPT_EXPIRED(reason=mfa_receipt_age_exceeded)
4. 跨进程吊销检测(P1-05 核心):revoke_mfa_receipt 写入 SQLite + L1 重置后,
   verify_mfa_receipt_authoritative 仍能通过 SQLite 权威查询发现吊销
5. 一次性消费(consume=True):第二次调用同 jti → raise already_consumed
6. consume=False:不消费,UoW 可在统一事务中自行 CAS 消费
7. DeprecationWarning:sync verify_mfa_receipt 触发,async 权威入口抑制
8. data_lifecycle._verify_break_glass_two_person_approval 集成:使用权威入口

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 CacheStore.init() 创建表
- 通过 monkeypatch 设置 MFA_RECEIPT_SIGNING_KEY 环境变量
- 跨进程吊销通过 reset_mfa_state_for_testing() 模拟进程重启(L1 清空,SQLite 保留)
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path
from unittest.mock import MagicMock

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
    tmpdir = tempfile.mkdtemp(prefix="r63_p1_5_test_")
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


@pytest.fixture(autouse=True)
def reset_mfa_state():
    """每个用例前重置 MFA 模块级 L1 缓存状态(含 R62 P1-07 吊销 ledger)。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


@pytest.fixture
def mfa_signing_key(monkeypatch):
    """设置 MFA receipt 签名密钥环境变量(仅测试用)。

    issue_mfa_receipt / verify_mfa_receipt 通过 _get_mfa_receipt_keyring()
    读取 MFA_RECEIPT_SIGNING_KEY 环境变量,缺失时 fail-closed。
    """
    monkeypatch.setenv(
        "MFA_RECEIPT_SIGNING_KEY",
        "r63_p1_05_test_signing_key_32bytes_min",
    )
    # 清除 previous 密钥,确保密钥环只有 current
    monkeypatch.delenv("MFA_RECEIPT_SIGNING_KEY_PREVIOUS", raising=False)
    yield


# ════════════════════════════════════════════════════════════════
# 辅助常量
# ════════════════════════════════════════════════════════════════

_PRINCIPAL_ID = 1001
_ACTION_HASH_64HEX = "a" * 64  # 64 位 hex(SHA-256 格式)
_PURPOSE = "break_glass_approval"


# ════════════════════════════════════════════════════════════════
# 1. 权威验证 happy path
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAuthoritativeHappyPath:
    """R63 P1-05: verify_mfa_receipt_authoritative 合法 token happy path。"""

    @pytest.mark.asyncio
    async def test_valid_token_returns_payload(self, real_store, mfa_signing_key):
        """合法 token + consume=True → 返回 payload 且 jti 被消费。"""
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
            consume_mfa_receipt,
        )

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )

        payload = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=True,
        )
        # 返回完整 payload
        assert payload["sub"] == _PRINCIPAL_ID
        assert payload["purpose"] == _PURPOSE
        assert payload["action_hash"] == _ACTION_HASH_64HEX
        assert payload["jti"], "payload 应含 jti"
        # jti 已被消费(第二次 consume 返回 False)
        consumed_again = await consume_mfa_receipt(payload["jti"])
        assert consumed_again is False, "consume=True 应已消费 jti"

    @pytest.mark.asyncio
    async def test_valid_token_consume_false_does_not_consume(
        self, real_store, mfa_signing_key,
    ):
        """合法 token + consume=False → 返回 payload 但 jti 未被消费。"""
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
            consume_mfa_receipt,
        )

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )

        payload = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,  # 不消费
        )
        jti = payload["jti"]
        # jti 未被消费(后续 consume_mfa_receipt 应返回 True)
        consumed_after = await consume_mfa_receipt(jti)
        assert consumed_after is True, (
            "consume=False 不应消费 jti,后续 consume_mfa_receipt 应返回 True"
        )


# ════════════════════════════════════════════════════════════════
# 2. 签名/字段校验失败
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAuthoritativeFieldMismatch:
    """R63 P1-05: 字段不匹配 → raise AUTH_MFA_RECEIPT_INVALID。"""

    @pytest.mark.asyncio
    async def test_wrong_principal_raises(self, real_store, mfa_signing_key):
        """sub 不匹配 → raise AUTH_MFA_RECEIPT_INVALID(reason=sub_mismatch)。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt_authoritative
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID + 1,  # 不匹配
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "sub_mismatch"

    @pytest.mark.asyncio
    async def test_wrong_purpose_raises(self, real_store, mfa_signing_key):
        """purpose 不匹配 → raise AUTH_MFA_RECEIPT_INVALID(reason=purpose_mismatch)。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt_authoritative
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose="different_purpose",  # 不匹配
                expected_action_hash=_ACTION_HASH_64HEX,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "purpose_mismatch"

    @pytest.mark.asyncio
    async def test_wrong_action_hash_raises(self, real_store, mfa_signing_key):
        """action_hash 不匹配 → raise AUTH_MFA_RECEIPT_INVALID(reason=action_hash_mismatch)。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt_authoritative
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash="b" * 64,  # 不匹配
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "action_hash_mismatch"

    @pytest.mark.asyncio
    async def test_malformed_token_raises(self, real_store, mfa_signing_key):
        """格式非法 token → raise AUTH_MFA_RECEIPT_INVALID(reason=malformed_token)。"""
        from admin.mfa import verify_mfa_receipt_authoritative
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token="not_a_valid_token",
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "malformed_token"


# ════════════════════════════════════════════════════════════════
# 3. age 校验(陈旧 receipt)
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAuthoritativeAgeCheck:
    """R63 P1-05: age 超限 → raise AUTH_MFA_RECEIPT_EXPIRED。"""

    @pytest.mark.asyncio
    async def test_stale_receipt_raises_age_exceeded(
        self, real_store, mfa_signing_key,
    ):
        """iat 在 sync skew(60s)内但 age > max_age_seconds →
        raise AUTH_MFA_RECEIPT_EXPIRED(reason=mfa_receipt_age_exceeded)。

        设计说明:sync verify_mfa_receipt 强制 iat 落在 [now-60, now+60]
        (_MFA_RECEIPT_IAT_SKEW_SECONDS=60),所以无法用"10 分钟前的 iat"
        触发 age 校验(那种 token 在 sync 阶段就被 reason=iat_in_future 拒绝)。
        本测试用 iat=now-30(通过 sync)+ max_age_seconds=10(age=30 > 10)
        精确触发 verify_mfa_receipt_age 失败路径,验证 age 校验逻辑。
        """
        from admin.mfa import (
            verify_mfa_receipt_authoritative,
            _b64url_encode,
            _sign_receipt_payload,
            _get_signing_key_and_kid,
            _MFA_RECEIPT_TOKEN_PREFIX,
        )
        import json as _json
        import uuid as _uuid
        from services.error_codes import AppError, ErrorCodes

        key, kid = _get_signing_key_and_kid()
        now = int(time.time())
        # iat 在 sync skew(60s)内,通过 sync verify_mfa_receipt;
        # age = 30s > max_age_seconds=10,触发 verify_mfa_receipt_age 失败
        recent_iat = now - 30
        payload = {
            "jti": _uuid.uuid4().hex,
            "sub": _PRINCIPAL_ID,
            "purpose": _PURPOSE,
            "action_hash": _ACTION_HASH_64HEX,
            "amr": ["totp"],
            "iat": recent_iat,
            # exp - iat = 300(满足 lifetime 上限),exp > now(未过期)
            "exp": now + 270,
            "kid": kid,
        }
        payload_json = _json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
        signature = _sign_receipt_payload(payload_b64, key)
        token = f"{_MFA_RECEIPT_TOKEN_PREFIX}.{payload_b64}.{signature}"

        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
                max_age_seconds=10,  # age=30 > 10 → 触发 age_exceeded
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED
        assert exc_info.value.params.get("reason") == "mfa_receipt_age_exceeded"

    @pytest.mark.asyncio
    async def test_custom_max_age_allows_stale_receipt(
        self, real_store, mfa_signing_key,
    ):
        """iat 在 sync skew(60s)内且 max_age 放宽到 60s → 通过(可配置上限)。

        设计说明:与 test_stale_receipt_raises_age_exceeded 对称,
        iat=now-30 通过 sync,max_age_seconds=60(age=30 <= 60)通过 age 校验。
        """
        from admin.mfa import (
            verify_mfa_receipt_authoritative,
            _b64url_encode,
            _sign_receipt_payload,
            _get_signing_key_and_kid,
            _MFA_RECEIPT_TOKEN_PREFIX,
        )
        import json as _json
        import uuid as _uuid

        key, kid = _get_signing_key_and_kid()
        now = int(time.time())
        # iat 在 sync skew(60s)内,通过 sync verify_mfa_receipt;
        # age = 30s <= max_age_seconds=60,通过 verify_mfa_receipt_age
        recent_iat = now - 30  # 30 秒前(在 skew 内)
        payload = {
            "jti": _uuid.uuid4().hex,
            "sub": _PRINCIPAL_ID,
            "purpose": _PURPOSE,
            "action_hash": _ACTION_HASH_64HEX,
            "amr": ["totp"],
            "iat": recent_iat,
            "exp": now + 270,
            "kid": kid,
        }
        payload_json = _json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
        signature = _sign_receipt_payload(payload_b64, key)
        token = f"{_MFA_RECEIPT_TOKEN_PREFIX}.{payload_b64}.{signature}"

        # max_age=60 允许 30 秒前的 receipt(age=30 <= 60)
        result = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            max_age_seconds=60,
            consume=False,
        )
        assert result["jti"] == payload["jti"]


# ════════════════════════════════════════════════════════════════
# 4. 跨进程吊销检测(P1-05 核心修复)
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAuthoritativeCrossProcessRevocation:
    """R63 P1-05: 跨进程吊销检测(SQLite 权威层)。

    P1-05 核心修复场景:
      - 旧 sync verify_mfa_receipt 只查 L1 缓存,无法发现其他进程刚写入
        SQLite 的吊销;调用方需"记得"另行 await is_mfa_receipt_revoked()。
      - 新 async verify_mfa_receipt_authoritative 内部完成 SQLite 权威查询,
        调用方无需"记得"查询权威层。
    """

    @pytest.mark.asyncio
    async def test_revoked_receipt_raises_via_sqlite(
        self, real_store, mfa_signing_key,
    ):
        """revoke_mfa_receipt 后,verify_mfa_receipt_authoritative raise revoked。"""
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
            get_mfa_manager,
        )
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 先验证一次拿到 jti
        payload = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        jti = payload["jti"]

        # 显式吊销(写入 SQLite + L1)
        manager = get_mfa_manager()
        await manager.revoke_mfa_receipt(jti, reason="test_revoke")

        # 再次权威验证应 raise revoked
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
                consume=False,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "revoked"

    @pytest.mark.asyncio
    async def test_cross_process_revocation_survives_l1_reset(
        self, real_store, mfa_signing_key,
    ):
        """跨进程吊销:L1 重置(模拟进程重启)后,权威 SQLite 查询仍能发现吊销。

        P1-05 核心测试:
          - 进程 A revoke_mfa_receipt(jti) → 写入 SQLite + L1
          - 进程 B(模拟)L1 重置 → 仅 SQLite 保留吊销记录
          - 进程 B verify_mfa_receipt_authoritative → 通过 SQLite 权威查询
            发现吊销,raise revoked
          - 旧 sync verify_mfa_receipt 在 L1 重置后会漏判(只查 L1 缓存),
            这正是 P1-05 要修复的问题。
        """
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
            verify_mfa_receipt,
            get_mfa_manager,
        )
        from admin import mfa as _mfa_mod
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 拿 jti
        payload = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        jti = payload["jti"]

        # 进程 A 吊销
        manager = get_mfa_manager()
        await manager.revoke_mfa_receipt(jti, reason="cross_process_test")

        # 模拟进程重启:L1 缓存清空(SQLite 保留吊销记录)
        _mfa_mod.reset_mfa_state_for_testing()

        # 旧 sync verify_mfa_receipt 在 L1 重置后会漏判(只查 L1,不查 SQLite)
        # 这正是 P1-05 要修复的安全漏洞:用 warnings.catch_warnings 抑制
        # deprecation warning,验证旧函数确实漏判(返回 payload 而非 raise)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_payload = verify_mfa_receipt(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
            )
        assert legacy_payload["jti"] == jti, (
            "旧 sync verify_mfa_receipt 在 L1 重置后漏判跨进程吊销"
            "(这正是 P1-05 要修复的安全漏洞)"
        )

        # 新 async verify_mfa_receipt_authoritative 通过 SQLite 权威查询
        # 发现吊销,raise revoked(P1-05 修复后正确行为)
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
                consume=False,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        assert exc_info.value.params.get("reason") == "revoked", (
            "verify_mfa_receipt_authoritative 必须通过 SQLite 权威查询发现跨进程吊销"
        )


# ════════════════════════════════════════════════════════════════
# 5. 一次性消费(consume=True)
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptAuthoritativeOneTimeConsumption:
    """R63 P1-05: 一次性消费(consume=True),重放被拒绝。"""

    @pytest.mark.asyncio
    async def test_second_call_raises_already_consumed(
        self, real_store, mfa_signing_key,
    ):
        """同 jti 第二次调用 verify_mfa_receipt_authoritative(consume=True)
        → raise AUTH_MFA_RECEIPT_EXPIRED(reason=already_consumed)。
        """
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
        )
        from services.error_codes import AppError, ErrorCodes

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 第一次:消费成功
        payload1 = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=True,
        )
        assert payload1["jti"]

        # 第二次:同 jti 已消费 → raise already_consumed
        with pytest.raises(AppError) as exc_info:
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
                consume=True,
            )
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED
        assert exc_info.value.params.get("reason") == "already_consumed"

    @pytest.mark.asyncio
    async def test_consume_false_allows_multiple_verify(
        self, real_store, mfa_signing_key,
    ):
        """consume=False 时可多次验证(供 data_lifecycle UoW 在统一事务中消费)。

        R60/R61 P0-01 约束:data_lifecycle._verify_break_glass_two_person_approval
        使用 consume=False,实际 CAS 消费延迟到 execute_high_risk_command_uow
        统一事务中(审批消费 + 状态机 CAS + 业务副作用原子提交/回滚)。
        """
        from admin.mfa import (
            issue_mfa_receipt,
            verify_mfa_receipt_authoritative,
        )

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        # consume=False 可多次验证(不消费)
        payload1 = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        payload2 = await verify_mfa_receipt_authoritative(
            token=token,
            expected_principal_id=_PRINCIPAL_ID,
            expected_purpose=_PURPOSE,
            expected_action_hash=_ACTION_HASH_64HEX,
            consume=False,
        )
        # 两次返回相同 jti(未消费)
        assert payload1["jti"] == payload2["jti"]


# ════════════════════════════════════════════════════════════════
# 6. DeprecationWarning
# ════════════════════════════════════════════════════════════════

class TestVerifyMfaReceiptDeprecationWarning:
    """R63 P1-05: sync verify_mfa_receipt 触发 DeprecationWarning;
    async 权威入口抑制内部调用产生的 warning。"""

    def test_sync_verify_emits_deprecation_warning(self, mfa_signing_key):
        """sync verify_mfa_receipt 调用时触发 DeprecationWarning。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            verify_mfa_receipt(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
            )
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) >= 1, (
            "sync verify_mfa_receipt 应触发 DeprecationWarning(R63 P1-05)"
        )
        # 验证 warning 消息包含关键信息
        msg = str(deprecation_warnings[0].message)
        assert "deprecated" in msg.lower()
        assert "verify_mfa_receipt_authoritative" in msg

    @pytest.mark.asyncio
    async def test_authoritative_suppresses_deprecation_warning(
        self, real_store, mfa_signing_key,
    ):
        """async verify_mfa_receipt_authoritative 内部调用 verify_mfa_receipt 时
        抑制 DeprecationWarning(避免每次权威验证都打印噪声)。"""
        from admin.mfa import issue_mfa_receipt, verify_mfa_receipt_authoritative

        token = issue_mfa_receipt(
            principal_id=_PRINCIPAL_ID,
            purpose=_PURPOSE,
            action_hash=_ACTION_HASH_64HEX,
            amr=["totp"],
            ttl_seconds=300,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await verify_mfa_receipt_authoritative(
                token=token,
                expected_principal_id=_PRINCIPAL_ID,
                expected_purpose=_PURPOSE,
                expected_action_hash=_ACTION_HASH_64HEX,
                consume=False,
            )
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) == 0, (
            "verify_mfa_receipt_authoritative 应抑制内部 verify_mfa_receipt "
            "调用产生的 DeprecationWarning(避免噪声)"
        )


# ════════════════════════════════════════════════════════════════
# 7. 静态检查:函数签名 + 模块导出
# ════════════════════════════════════════════════════════════════

class TestStaticChecks:
    """R63 P1-05: 静态检查新函数定义存在性。"""

    def test_verify_mfa_receipt_authoritative_exists(self):
        """admin.mfa 应定义 verify_mfa_receipt_authoritative async 函数。"""
        from admin import mfa as _mfa_mod
        assert hasattr(_mfa_mod, "verify_mfa_receipt_authoritative"), \
            "admin.mfa 应定义 verify_mfa_receipt_authoritative(R63 P1-05)"
        assert inspect.iscoroutinefunction(_mfa_mod.verify_mfa_receipt_authoritative), \
            "verify_mfa_receipt_authoritative 必须为 async 函数(R63 P1-05)"

    def test_verify_mfa_receipt_authoritative_signature(self):
        """verify_mfa_receipt_authoritative 签名应包含 consume 关键字参数。"""
        from admin.mfa import verify_mfa_receipt_authoritative
        sig = inspect.signature(verify_mfa_receipt_authoritative)
        params = sig.parameters
        # 必填参数
        assert "token" in params
        assert "expected_principal_id" in params
        assert "expected_purpose" in params
        assert "expected_action_hash" in params
        # 可选关键字参数
        assert "consume" in params, (
            "verify_mfa_receipt_authoritative 应支持 consume 关键字参数"
            "(R60/R61 P0-01:data_lifecycle UoW 需 consume=False)"
        )
        assert params["consume"].default is True, (
            "consume 默认值应为 True(验证即消费,生产默认行为)"
        )
        assert "max_age_seconds" in params
        assert params["max_age_seconds"].default == 300

    def test_data_lifecycle_uses_authoritative_verifier(self):
        """data_lifecycle.py 应使用 verify_mfa_receipt_authoritative。"""
        repo_root = Path(__file__).resolve().parent.parent
        data_lifecycle_file = repo_root / "services" / "data_lifecycle.py"
        source = data_lifecycle_file.read_text(encoding="utf-8")
        assert "verify_mfa_receipt_authoritative" in source, (
            "data_lifecycle.py 应引用 verify_mfa_receipt_authoritative(R63 P1-05)"
        )
        # consume=False 用于保留 R60/R61 P0-01 统一事务原子性
        assert "consume=False" in source, (
            "data_lifecycle.py 应使用 consume=False 保留 UoW 统一事务原子性"
        )

    def test_sync_verify_mfa_receipt_has_deprecation_warning(self):
        """verify_mfa_receipt 源码应包含 DeprecationWarning。"""
        repo_root = Path(__file__).resolve().parent.parent
        mfa_file = repo_root / "admin" / "mfa.py"
        source = mfa_file.read_text(encoding="utf-8")
        assert "DeprecationWarning" in source, (
            "admin/mfa.py 应在 verify_mfa_receipt 中触发 DeprecationWarning(R63 P1-05)"
        )
        assert "warnings.warn" in source, (
            "admin/mfa.py 应使用 warnings.warn 触发 deprecation"
        )


# ════════════════════════════════════════════════════════════════
# 8. data_lifecycle 集成测试:_verify_break_glass_two_person_approval
# ════════════════════════════════════════════════════════════════

class TestDataLifecycleBreakGlassIntegration:
    """R63 P1-05: data_lifecycle._verify_break_glass_two_person_approval 集成测试。

    验证 _verify_break_glass_two_person_approval 内部使用
    verify_mfa_receipt_authoritative(consume=False),且跨进程吊销会被检测到。
    """

    @pytest.mark.asyncio
    async def test_break_glass_verification_revoked_receipt_blocked(
        self, real_store, mfa_signing_key,
    ):
        """break-glass 验证:某 approver 的 receipt 已被吊销 →
        _verify_break_glass_two_person_approval raise。"""
        from admin.mfa import issue_mfa_receipt, get_mfa_manager
        from services.data_lifecycle import _verify_break_glass_two_person_approval
        from services.error_codes import AppError, ErrorCodes
        from database.cache_store import get_cache_store
        import datetime as _dt

        store = get_cache_store()
        # 两个不同 approver
        approver1 = 2001
        approver2 = 2002
        principal_id = 1001  # 发起人(不能自审批)
        request_hash = "c" * 64  # 64 hex
        # 两个 approver 的 mfa_receipt token
        token1 = issue_mfa_receipt(
            principal_id=approver1,
            purpose="break_glass_approval",
            action_hash=request_hash,
            amr=["totp"],
            ttl_seconds=300,
        )
        token2 = issue_mfa_receipt(
            principal_id=approver2,
            purpose="break_glass_approval",
            action_hash=request_hash,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 拿 jti1(用于吊销)
        from admin.mfa import verify_mfa_receipt_authoritative
        payload1 = await verify_mfa_receipt_authoritative(
            token=token1,
            expected_principal_id=approver1,
            expected_purpose="break_glass_approval",
            expected_action_hash=request_hash,
            consume=False,
        )
        jti1 = payload1["jti"]
        # 吊销 approver1 的 receipt(模拟跨进程吊销)
        manager = get_mfa_manager()
        await manager.revoke_mfa_receipt(jti1, reason="test_break_glass_revoke")

        # 创建 command_approvals 表(由 _ensure_command_approvals_table 通过
        # 版本化 migration 创建,CacheStore.init 不创建该表)
        from services.data_lifecycle import _ensure_command_approvals_table
        ok = await _ensure_command_approvals_table()
        assert ok, "command_approvals 表创建失败(migration 应用失败)"

        # 插入两条 break_glass 审批记录
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        expires_at = (_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(minutes=5)).isoformat()
        for approver_id, token in [(approver1, token1), (approver2, token2)]:
            await store._db.execute(
                "INSERT INTO command_approvals "
                "(action_id, approver_id, approval_type, decision, request_hash, "
                "mfa_receipt, permission, approved_at, expires_at) "
                "VALUES (?, ?, 'break_glass', 'approved', ?, ?, ?, ?, ?)",
                ("test_action_revoked", approver_id, request_hash, token,
                 "purge", now_iso, expires_at),
            )
        await store._db.commit()

        # 验证:_verify_break_glass_two_person_approval 应检测到 approver1 的
        # receipt 已被吊销,raise AppError
        with pytest.raises(AppError) as exc_info:
            await _verify_break_glass_two_person_approval(
                action_id="test_action_revoked",
                expected_principal_id=principal_id,
            )
        # verify_mfa_receipt_authoritative raise AUTH_MFA_RECEIPT_INVALID(reason=revoked),
        # _verify_break_glass_two_person_approval 的 `except AppError: raise` 直接传播
        # 原始 MFA 错误码(不包装为 DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
        # 保留精确诊断信息供上层处理)
        assert exc_info.value.code == ErrorCodes.AUTH_MFA_RECEIPT_INVALID
        reason = exc_info.value.params.get("reason", "")
        assert reason == "revoked", (
            f"应检测到吊销的 receipt,reason={reason}"
        )

    @pytest.mark.asyncio
    async def test_break_glass_verification_happy_path(
        self, real_store, mfa_signing_key,
    ):
        """break-glass 验证 happy path:两个 approver 合法 receipt →
        返回 ApprovalGrant(jti 未被消费,供 UoW 统一事务消费)。"""
        from admin.mfa import issue_mfa_receipt, consume_mfa_receipt
        from services.data_lifecycle import _verify_break_glass_two_person_approval
        from database.cache_store import get_cache_store
        import datetime as _dt

        store = get_cache_store()
        approver1 = 2001
        approver2 = 2002
        principal_id = 1001
        request_hash = "d" * 64
        token1 = issue_mfa_receipt(
            principal_id=approver1,
            purpose="break_glass_approval",
            action_hash=request_hash,
            amr=["totp"],
            ttl_seconds=300,
        )
        token2 = issue_mfa_receipt(
            principal_id=approver2,
            purpose="break_glass_approval",
            action_hash=request_hash,
            amr=["totp"],
            ttl_seconds=300,
        )
        # 创建 command_approvals 表(由 _ensure_command_approvals_table 通过
        # 版本化 migration 创建,CacheStore.init 不创建该表)
        from services.data_lifecycle import _ensure_command_approvals_table
        ok = await _ensure_command_approvals_table()
        assert ok, "command_approvals 表创建失败(migration 应用失败)"

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        expires_at = (_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(minutes=5)).isoformat()
        for approver_id, token in [(approver1, token1), (approver2, token2)]:
            await store._db.execute(
                "INSERT INTO command_approvals "
                "(action_id, approver_id, approval_type, decision, request_hash, "
                "mfa_receipt, permission, approved_at, expires_at) "
                "VALUES (?, ?, 'break_glass', 'approved', ?, ?, ?, ?, ?)",
                ("test_action_happy", approver_id, request_hash, token,
                 "purge", now_iso, expires_at),
            )
        await store._db.commit()

        # 验证:应返回 ApprovalGrant
        grant = await _verify_break_glass_two_person_approval(
            action_id="test_action_happy",
            expected_principal_id=principal_id,
        )
        assert grant.action_id == "test_action_happy"
        assert grant.request_hash == request_hash
        assert len(grant.jti_list) == 2, "应收集两个 approver 的 jti"
        # jti 未被消费(consume=False):后续 consume_mfa_receipt 应返回 True
        for jti in grant.jti_list:
            consumed = await consume_mfa_receipt(jti)
            assert consumed is True, (
                "consume=False 时 jti 不应被消费,UoW 可在统一事务中消费"
            )
