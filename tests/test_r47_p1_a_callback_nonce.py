"""R47 P1-a 终审整改测试:callback nonce 原子消费 + production 禁 default_secret。

测试范围:
- services/button_security.py
  * sign_button_token_with_nonce:持久化 nonce 到 callback_nonces 表
  * verify_button_token:原子消费 nonce(UPDATE WHERE consumed_at IS NULL)
  * _check_production_secret:production 缺 BOT_TOKEN 启动失败
  * HIGH_RISK_ACTIONS:高风险 action 必须使用 6 段格式
  * SIGNATURE_LENGTH:签名长度 ≥ 32 hex chars(128 bit)

测试场景:
1. nonce 原子消费(并发只成功一次)— asyncio.gather 双发,仅一个 valid
2. 已消费 nonce 拒绝 — 第二次 verify 返回 False
3. 过期 nonce 拒绝 — ttl=-1,expire_ts < now
4. 高风险 action 用旧 5 段格式拒绝
5. production 缺 BOT_TOKEN 启动失败(RuntimeError)
6. 签名长度验证(≥ 32 hex chars = 128 bit)

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
- monkeypatch 替换 database.cache_store.DB_PATH 指向临时路径
- monkeypatch 替换 get_cache_store 返回测试 store(sign_button_token_with_nonce 内部调用)
- 固定 ADMIN_BOT_TOKEN 避免 MagicMock 干扰 HMAC
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

from services.error_codes import AppError, ErrorCodes

# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 MagicMock)──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ── Fixture ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略:
    1. 临时目录下的 test_r47_callback_nonce.db
    2. 直接替换 database.cache_store.DB_PATH(参考 test_m1_cache_store_tables.py 模式)
    3. 替换 database.cache_store.get_cache_store 返回测试 store
       (sign_button_token_with_nonce 内部调用 _cs.get_cache_store())
    4. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r47_p1_a_test_")
    db_path = Path(tmpdir) / "test_r47_callback_nonce.db"
    original_path = _cs_module.DB_PATH
    original_get_store = _cs_module.get_cache_store
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        # 让 button_security 内的 get_cache_store() 返回测试 store
        _cs_module.get_cache_store = lambda: s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module.get_cache_store = original_get_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。

    conftest 注入的 settings 是 MagicMock,ADMIN_BOT_TOKEN 属性也是 MagicMock,
    调用 .encode() 会返回 MagicMock 导致 hmac.new() 抛错。
    此处将其设为固定字符串,确保 _sign() 可正常工作。
    """
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r47_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r47_test_sender_bot_token")
    # 默认 development 环境(避免触发 production fail-closed)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")


# ════════════════════════════════════════════════════════════════
# 1. nonce 原子消费(并发只成功一次)
# ════════════════════════════════════════════════════════════════


class TestNonceAtomicConsume:
    """R47 P1-a: nonce 原子消费,防止回调被并发重放。"""

    @pytest.mark.asyncio
    async def test_concurrent_consume_only_one_succeeds(self, store):
        """并发双发 verify_button_token,仅第一个 valid=True。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        # 生成带 nonce 的 token(持久化到 callback_nonces 表)
        token = await sign_button_token_with_nonce(
            principal_id=1001,
            action="approve_appeal",
            payload="appeal_123",
            ttl=3600,
        )
        # 并发双发验证(同一 token 两次 verify)
        results = await asyncio.gather(
            verify_button_token(token, current_user_id=1001, store=store),
            verify_button_token(token, current_user_id=1001, store=store),
        )
        valid_results = [r[0] for r in results]
        # 恰好一个成功,一个失败
        assert sum(valid_results) == 1, (
            f"应恰好一个 valid=True,实际: {valid_results}"
        )
        # 成功的那个返回正确的 action/data
        for valid, action, data in results:
            if valid:
                assert action == "approve_appeal"
                assert data == "appeal_123"

    @pytest.mark.asyncio
    async def test_serial_consume_second_fails(self, store):
        """串行调用:第一次 verify 成功,第二次 verify 失败。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        token = await sign_button_token_with_nonce(
            principal_id=2002,
            action="ban",
            payload="user_456",
            ttl=3600,
        )
        # 第一次验证(消费 nonce)→ 成功
        valid1, action1, data1 = await verify_button_token(
            token, current_user_id=2002, store=store
        )
        assert valid1 is True
        assert action1 == "ban"
        assert data1 == "user_456"
        # 第二次验证(nonce 已消费)→ 失败
        valid2, _, _ = await verify_button_token(
            token, current_user_id=2002, store=store
        )
        assert valid2 is False, "已消费的 nonce 必须拒绝"

    @pytest.mark.asyncio
    async def test_triple_consume_only_one_succeeds(self, store):
        """三次并发,仅一个成功。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        token = await sign_button_token_with_nonce(
            principal_id=3003,
            action="takedown",
            payload="file_xyz",
            ttl=3600,
        )
        results = await asyncio.gather(
            verify_button_token(token, current_user_id=3003, store=store),
            verify_button_token(token, current_user_id=3003, store=store),
            verify_button_token(token, current_user_id=3003, store=store),
        )
        valid_results = [r[0] for r in results]
        assert sum(valid_results) == 1, (
            f"三次并发应恰好一个 valid=True,实际: {valid_results}"
        )


# ════════════════════════════════════════════════════════════════
# 2. 已消费 nonce 拒绝
# ════════════════════════════════════════════════════════════════


class TestConsumedNonceRejected:
    """R47 P1-a: 已消费的 nonce 必须拒绝(防重放)。"""

    @pytest.mark.asyncio
    async def test_already_consumed_nonce_rejected(self, store):
        """已消费的 nonce 再次验证返回 False。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        token = await sign_button_token_with_nonce(
            principal_id=4004,
            action="purge",
            payload="batch_001",
            ttl=3600,
        )
        # 第一次:成功
        valid1, _, _ = await verify_button_token(
            token, current_user_id=4004, store=store
        )
        assert valid1 is True
        # 第二次:已消费,拒绝
        valid2, action2, data2 = await verify_button_token(
            token, current_user_id=4004, store=store
        )
        assert valid2 is False
        assert action2 == ""
        assert data2 == ""

    @pytest.mark.asyncio
    async def test_nonce_not_in_db_rejected(self, store):
        """nonce 不在 callback_nonces 表中(generate_signed_callback 生成,未持久化)→ 拒绝。"""
        from services.button_security import generate_signed_callback, verify_button_token

        # generate_signed_callback 不持久化 nonce 到 DB
        token = generate_signed_callback(
            user_id=5005, action="confirm", data="X", ttl=3600
        )
        # verify_button_token 尝试消费 nonce,但 DB 中不存在 → 拒绝
        valid, _, _ = await verify_button_token(
            token, current_user_id=5005, store=store
        )
        assert valid is False, "nonce 不在 DB 中必须拒绝(verify_button_token 路径)"


# ════════════════════════════════════════════════════════════════
# 3. 过期 nonce 拒绝
# ════════════════════════════════════════════════════════════════


class TestExpiredNonceRejected:
    """R47 P1-a: 过期的 nonce/callback 必须拒绝。"""

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, store):
        """ttl=-1 生成的 token 立即过期,verify 返回 False。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        token = await sign_button_token_with_nonce(
            principal_id=6006,
            action="restore",
            payload="doc_789",
            ttl=-1,  # 立即过期
        )
        # 等待极短时间确保 now > expire_ts
        await asyncio.sleep(0.01)
        valid, action, data = await verify_button_token(
            token, current_user_id=6006, store=store
        )
        assert valid is False, "过期 token 必须拒绝"
        assert action == ""
        assert data == ""

    @pytest.mark.asyncio
    async def test_expired_token_does_not_consume_nonce(self, store):
        """过期 token 被拒绝时,不应消费 nonce(在消费前就拒绝)。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        # 用 ttl=-1 生成过期 token
        expired_token = await sign_button_token_with_nonce(
            principal_id=7007,
            action="purge_file",
            payload="file_abc",
            ttl=-1,
        )
        await asyncio.sleep(0.01)
        # 验证过期 token → 失败
        valid, _, _ = await verify_button_token(
            expired_token, current_user_id=7007, store=store
        )
        assert valid is False
        # 提取 nonce,验证未被消费(callback_nonce_exists 应为 True)
        # 注意:过期检查在 nonce 消费之前 return,所以 nonce 仍是未消费状态
        nonce = expired_token.split(":")[-2]
        exists = await store.callback_nonce_exists(nonce)
        assert exists is True, "过期 token 被拒绝时,nonce 不应被消费"
        # 此时 nonce 仍可被消费(因为是未消费状态)
        consumed = await store.callback_nonce_consume(nonce)
        assert consumed is True, "未消费的 nonce 应可消费"


# ════════════════════════════════════════════════════════════════
# 4. 高风险 action 用旧 5 段格式拒绝
# ════════════════════════════════════════════════════════════════


class TestHighRiskActionOldFormatRejected:
    """R47 P1-a: 高风险 action 必须使用 6 段格式(含 nonce),旧 5 段格式拒绝。"""

    def test_high_risk_action_old_format_rejected_sync(self):
        """高风险 action(ban)用 5 段格式 → verify_signed_callback 拒绝。"""
        from services.button_security import _sign, verify_signed_callback

        # 手工构造 5 段格式 token(无 nonce)
        expire_ts = int(time.time()) + 3600
        payload = f"12345:ban:target_user:{expire_ts}"
        signature = _sign(payload)
        token = f"{payload}:{signature}"
        # 验证 → 拒绝(高风险 action 必须 6 段格式)
        valid, action, data = verify_signed_callback(token, current_user_id=12345)
        assert valid is False
        assert action == ""
        assert data == ""

    @pytest.mark.asyncio
    async def test_high_risk_action_old_format_rejected_async(self, store):
        """高风险 action(takedown)用 5 段格式 → verify_button_token 拒绝。"""
        from services.button_security import _sign, verify_button_token

        expire_ts = int(time.time()) + 3600
        payload = f"99999:takedown:file_code:{expire_ts}"
        signature = _sign(payload)
        token = f"{payload}:{signature}"
        valid, action, data = await verify_button_token(
            token, current_user_id=99999, store=store
        )
        assert valid is False
        assert action == ""
        assert data == ""

    def test_low_risk_action_old_format_accepted_sync(self):
        """低风险 action(cancel)用 5 段格式 → verify_signed_callback 通过(向后兼容)。"""
        from services.button_security import _sign, verify_signed_callback

        expire_ts = int(time.time()) + 3600
        payload = f"88888:cancel:some_data:{expire_ts}"
        signature = _sign(payload)
        token = f"{payload}:{signature}"
        valid, action, data = verify_signed_callback(token, current_user_id=88888)
        assert valid is True
        assert action == "cancel"
        assert data == "some_data"

    @pytest.mark.asyncio
    async def test_high_risk_action_with_nonce_accepted(self, store):
        """高风险 action 用 sign_button_token_with_nonce 生成(6 段)→ verify 通过。"""
        from services.button_security import sign_button_token_with_nonce, verify_button_token

        token = await sign_button_token_with_nonce(
            principal_id=11111,
            action="admin_grant",
            payload="role_super_admin",
            ttl=3600,
        )
        valid, action, data = await verify_button_token(
            token, current_user_id=11111, store=store
        )
        assert valid is True
        assert action == "admin_grant"
        assert data == "role_super_admin"

    @pytest.mark.asyncio
    async def test_all_high_risk_actions_rejected_with_old_format(self, store):
        """遍历所有 HIGH_RISK_ACTIONS,验证 5 段格式均被拒绝。"""
        from services.button_security import HIGH_RISK_ACTIONS, _sign, verify_button_token

        expire_ts = int(time.time()) + 3600
        for action in HIGH_RISK_ACTIONS:
            payload = f"55555:{action}:data_field:{expire_ts}"
            signature = _sign(payload)
            token = f"{payload}:{signature}"
            valid, _, _ = await verify_button_token(
                token, current_user_id=55555, store=store
            )
            assert valid is False, (
                f"高风险 action={action} 用 5 段格式应被拒绝,实际 valid={valid}"
            )


# ════════════════════════════════════════════════════════════════
# 5. production 缺 BOT_TOKEN 启动失败
# ════════════════════════════════════════════════════════════════


class TestProductionFailClosed:
    """R47 P1-a: production 环境缺 BOT_TOKEN 必须 fail-closed。"""

    def test_production_missing_both_tokens_raises(self, monkeypatch):
        """production + ADMIN_BOT_TOKEN 和 SENDER_BOT_TOKEN 均空 → AppError(PRODUCTION_BOT_TOKEN_MISSING)。"""
        import config
        from services.button_security import _check_production_secret

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        with pytest.raises(AppError) as exc_info:
            _check_production_secret()
        assert exc_info.value.envelope.code == ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING

    def test_production_missing_admin_token_only_passes(self, monkeypatch):
        """production + 仅 SENDER_BOT_TOKEN 配置 → 通过(至少一个 token 即可)。"""
        import config
        from services.button_security import _check_production_secret

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "sender_token_prod")
        # 不应抛异常
        _check_production_secret()

    def test_production_missing_sender_token_only_passes(self, monkeypatch):
        """production + 仅 ADMIN_BOT_TOKEN 配置 → 通过。"""
        import config
        from services.button_security import _check_production_secret

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "admin_token_prod")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        _check_production_secret()

    def test_development_missing_tokens_passes(self, monkeypatch):
        """development + 两个 token 均空 → 通过(宽松,允许 default_secret)。"""
        import config
        from services.button_security import _check_production_secret

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        _check_production_secret()

    def test_module_init_does_not_raise_in_test_env(self):
        """模块初始化时(conftest 的 MagicMock settings)不应抛异常。

        conftest 注入的 settings 是 MagicMock,ENVIRONMENT 属性也是 MagicMock,
        str(MagicMock) != "production",故 _check_production_secret 不触发。
        """
        # 重新导入模块,验证不会因 _check_production_secret 抛异常
        import importlib
        import services.button_security
        importlib.reload(services.button_security)


# ════════════════════════════════════════════════════════════════
# 6. 签名长度验证
# ════════════════════════════════════════════════════════════════


class TestSignatureLength:
    """R47 P1-a: 签名长度 ≥ 32 hex chars(128 bit)。"""

    def test_signature_length_constant(self):
        """SIGNATURE_LENGTH 常量 = 32(128 bit)。"""
        from services.button_security import SIGNATURE_LENGTH
        assert SIGNATURE_LENGTH == 32

    def test_sign_returns_32_hex_chars(self):
        """_sign 返回 32 个十六进制字符(128 bit)。"""
        from services.button_security import _sign
        sig = _sign("test_payload_12345")
        assert len(sig) == 32, f"签名长度应为 32,实际: {len(sig)}"
        # 全部为十六进制字符
        int(sig, 16)  # 抛 ValueError 如果不是合法十六进制

    def test_generate_signed_callback_produces_32_char_sig(self):
        """generate_signed_callback 生成的签名长度 = 32。"""
        from services.button_security import generate_signed_callback
        token = generate_signed_callback(
            user_id=1, action="confirm", data="x", ttl=3600
        )
        parts = token.split(":")
        signature = parts[-1]
        assert len(signature) == 32, f"签名长度应为 32,实际: {len(signature)}"

    def test_sign_button_token_with_nonce_produces_32_char_sig(self):
        """sign_button_token_with_nonce 生成的签名长度 = 32(无需 store,直接测 _sign)。"""
        from services.button_security import _sign
        # 模拟 sign_button_token_with_nonce 内部的签名过程
        sig_payload = "1001:approve_appeal:appeal_123:9999999999:abc123"
        sig = _sign(sig_payload)
        assert len(sig) == 32

    def test_short_signature_rejected(self):
        """签名长度 < 32 → verify_signed_callback 拒绝。"""
        from services.button_security import _sign, verify_signed_callback
        # 构造一个签名长度不足的 token(模拟旧 16 字符签名)
        expire_ts = int(time.time()) + 3600
        payload = f"22222:cancel:data:{expire_ts}"
        # 只取前 16 字符(模拟旧的 64 bit 签名)
        full_sig = _sign(payload)
        short_sig = full_sig[:16]
        token = f"{payload}:{short_sig}"
        valid, _, _ = verify_signed_callback(token, current_user_id=22222)
        assert valid is False, "签名长度 < 32(128 bit)必须拒绝"

    @pytest.mark.asyncio
    async def test_short_signature_rejected_async(self, store):
        """签名长度 < 32 → verify_button_token(async)拒绝。"""
        from services.button_security import _sign, verify_button_token

        expire_ts = int(time.time()) + 3600
        # 低风险 action + 5 段格式(无 nonce,不会触发 nonce 消费)
        payload = f"33333:cancel:data:{expire_ts}"
        full_sig = _sign(payload)
        short_sig = full_sig[:16]  # 16 字符(64 bit)
        token = f"{payload}:{short_sig}"
        valid, _, _ = await verify_button_token(
            token, current_user_id=33333, store=store
        )
        assert valid is False

    def test_nonce_entropy_128_bit(self):
        """generate_signed_callback 生成的 nonce 熵 ≥ 128 bit(32 hex chars = 16 bytes)。"""
        from services.button_security import generate_signed_callback
        token = generate_signed_callback(
            user_id=1, action="confirm", data="x", ttl=3600
        )
        parts = token.split(":")
        nonce = parts[-2]  # 6 段格式:倒数第二段是 nonce
        # 32 hex chars = 16 bytes = 128 bit 熵
        assert len(nonce) >= 32, (
            f"nonce 长度应 ≥ 32 hex chars(128 bit),实际: {len(nonce)}"
        )
        int(nonce, 16)  # 验证为合法十六进制
