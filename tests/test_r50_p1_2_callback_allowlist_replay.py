"""R50 P1-2 终审整改测试:Callback 动态 action 运行时 allowlist + 重放 E2E。

测试范围:
- services/callback_allowlist.py
  * validate_action_allowed:高风险/低风险 action allowlist 验证(fail-closed)
  * get_action_risk_level:返回 'high'/'low'/'unknown'
  * register_runtime_action:动态注册新 action(审计日志)
- services/button_security.py(已有 API,不修改)
  * sign_button_token_with_nonce + verify_button_token:async + nonce 原子消费
  * generate_signed_callback + verify_signed_callback:sync legacy 接口
  * HIGH_RISK_ACTIONS:高风险 action 集合

测试场景矩阵(18 用例):
A. Allowlist 验证(6)
   1. 高风险 action 通过
   2. 低风险 action 通过
   3. 未知 action raise CallbackActionNotAllowedError
   4. 空 action raise
   5. get_action_risk_level 三档返回正确
   6. register_runtime_action 后 validate 通过
B. 并发双击重放(3)
   7. asyncio.gather 2 次同 token → 仅 1 次成功
   8. asyncio.gather 10 次同 token → 仅 1 次成功
   9. 2 个不同 nonce 的 token 并发 → 2 次都成功
C. 过期 callback(2)
   10. ttl=1 秒,sleep 2 秒 → False
   11. ttl=10 秒,sleep 9 秒 → True(临界有效)
D. 跨用户篡改(2)
   12. user_id=100 签名,user_id=200 验证 → False
   13. 篡改 user_id 字段 → 签名不匹配 → False
E. 重放攻击(3)
   14. 同 token 第一次成功,第二次失败(nonce 已消费)
   15. 篡改 signature 字段 → False
   16. action="ban" 的 callback 改 action="view" → False
F. 高风险 action 必须使用 nonce(2)
   17. ban 用 5 段格式 → verify_button_token False
   18. view 用 5 段格式 → verify_signed_callback True

测试策略:
- 真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
- monkeypatch 替换 database.cache_store.DB_PATH 指向临时路径
- monkeypatch 替换 get_cache_store 返回测试 store
- asyncio.gather 模拟并发双击(真实协程竞争,非 mock)
- time.sleep 模拟过期场景(真实 wall-clock)
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

    策略(参考 test_r47_p1_a_callback_nonce.py):
    1. 临时目录下的 test_r50_p1_2_callback_allowlist.db
    2. 替换 database.cache_store.DB_PATH 指向临时路径
    3. 替换 database.cache_store.get_cache_store 返回测试 store
       (sign_button_token_with_nonce 内部调用 _cs.get_cache_store())
    4. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r50_p1_2_test_")
    db_path = Path(tmpdir) / "test_r50_p1_2_callback_allowlist.db"
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
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r50_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r50_test_sender_bot_token")
    # 默认 development 环境(避免触发 production fail-closed)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")


@pytest.fixture(autouse=True)
def reset_allowlist():
    """每个用例前后重置 callback_allowlist 的运行时状态(防跨用例污染)。"""
    from services import callback_allowlist
    callback_allowlist.reset_runtime_allowlist()
    yield
    callback_allowlist.reset_runtime_allowlist()


# ════════════════════════════════════════════════════════════════
# A. Allowlist 验证(6 测试)
# ════════════════════════════════════════════════════════════════


class TestAllowlistValidation:
    """R50 P1-2: 运行时 action allowlist 验证。"""

    def test_validate_action_allowed_high_risk_passes(self):
        """A1: HIGH_RISK_ACTIONS 中的 action 通过 validate_action_allowed。"""
        from services.callback_allowlist import validate_action_allowed
        from services.button_security import HIGH_RISK_ACTIONS

        # 抽样验证(避免遍历整个集合)
        for action in ("ban", "unban", "takedown", "purge", "delete",
                       "admin_grant", "admin_revoke", "rotate_keys",
                       "reset_quota", "break_glass", "force_logout",
                       "approve_appeal", "reject_appeal",
                       "update_config", "reload_config"):
            assert action in HIGH_RISK_ACTIONS, f"测试数据错误: {action} 不在 HIGH_RISK_ACTIONS"
            # 不抛异常即通过
            validate_action_allowed(action)

    def test_validate_action_allowed_low_risk_passes(self):
        """A2: 低风险 action(view/cancel/close 等)通过 validate_action_allowed。"""
        from services.callback_allowlist import validate_action_allowed

        for action in ("view", "cancel", "close", "dismiss", "language",
                       "lang", "select_lang", "page", "next", "prev",
                       "refresh", "info", "help", "back", "menu",
                       "noop", "ack", "confirm_view"):
            # 不抛异常即通过
            validate_action_allowed(action)

    def test_validate_action_allowed_unknown_raises(self):
        """A3: 未知 action("unknown_action") raise AppError(CALLBACK_ACTION_NOT_ALLOWED)。"""
        from services.callback_allowlist import validate_action_allowed
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            validate_action_allowed("unknown_action")
        assert exc_info.value.envelope.code == ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED
        # params 中保留 action 字段(便于审计/日志)
        assert exc_info.value.envelope.params.get("action") == "unknown_action"

    def test_validate_action_allowed_empty_raises(self):
        """A4: 空 action("") raise AppError(CALLBACK_ACTION_NOT_ALLOWED)。"""
        from services.callback_allowlist import validate_action_allowed
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            validate_action_allowed("")
        assert exc_info.value.envelope.code == ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED

    def test_get_action_risk_level_correct(self):
        """A5: get_action_risk_level 三档返回正确(ban→high, view→low, unknown→unknown)。"""
        from services.callback_allowlist import get_action_risk_level

        # 高风险
        assert get_action_risk_level("ban") == "high"
        assert get_action_risk_level("takedown") == "high"
        assert get_action_risk_level("admin_grant") == "high"
        assert get_action_risk_level("purge") == "high"
        # 低风险
        assert get_action_risk_level("view") == "low"
        assert get_action_risk_level("cancel") == "low"
        assert get_action_risk_level("close") == "low"
        assert get_action_risk_level("language") == "low"
        # 未知
        assert get_action_risk_level("unknown_action") == "unknown"
        assert get_action_risk_level("totally_new") == "unknown"
        assert get_action_risk_level("") == "unknown"

    def test_register_runtime_action_adds_to_allowlist(self):
        """A6: register_runtime_action("custom_ban", "high") 后 validate 通过。"""
        from services.callback_allowlist import (
            get_action_risk_level,
            get_allowlist_snapshot,
            register_runtime_action,
            validate_action_allowed,
        )

        # 注册前:custom_ban 不在 allowlist 内
        assert get_action_risk_level("custom_ban") == "unknown"
        with pytest.raises(Exception):
            validate_action_allowed("custom_ban")

        # 动态注册(高风险)
        register_runtime_action(
            "custom_ban",
            risk_level="high",
            operator="admin_001",
            reason="R50 P1-2 测试动态注册",
        )

        # 注册后:custom_ban 通过 validate,risk_level=high
        validate_action_allowed("custom_ban")  # 不抛异常即通过
        assert get_action_risk_level("custom_ban") == "high"

        # 验证快照包含新 action
        snapshot = get_allowlist_snapshot()
        assert "custom_ban" in snapshot["high_risk"]


# ════════════════════════════════════════════════════════════════
# B. 并发双击重放(3 测试)
# ════════════════════════════════════════════════════════════════


class TestConcurrentDoubleClickReplay:
    """R50 P1-2: 并发双击重放防护(nonce 原子消费)。"""

    @pytest.mark.asyncio
    async def test_concurrent_double_click_only_one_succeeds(self, store):
        """B7: 同一 callback_data 并发 verify_button_token 2 次,仅 1 次成功。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        # 生成带 nonce 的 token(持久化到 callback_nonces 表)
        token = await sign_button_token_with_nonce(
            principal_id=10001,
            action="ban",
            payload="user_abc",
            ttl=3600,
        )
        # 并发双击(同一 token 两次 verify)
        results = await asyncio.gather(
            verify_button_token(token, current_user_id=10001, store=store),
            verify_button_token(token, current_user_id=10001, store=store),
        )
        valid_results = [r[0] for r in results]
        # 恰好一个成功,一个失败
        assert sum(valid_results) == 1, (
            f"并发双击应恰好一个 valid=True,实际: {valid_results}"
        )
        # 成功的那个返回正确的 action/data
        for valid, action, data in results:
            if valid:
                assert action == "ban"
                assert data == "user_abc"

    @pytest.mark.asyncio
    async def test_concurrent_10_clicks_only_one_succeeds(self, store):
        """B8: 同一 callback_data 并发 10 次,仅 1 次成功。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10002,
            action="takedown",
            payload="file_xyz",
            ttl=3600,
        )
        # 并发 10 次(模拟用户狂点)
        coros = [
            verify_button_token(token, current_user_id=10002, store=store)
            for _ in range(10)
        ]
        results = await asyncio.gather(*coros)
        valid_results = [r[0] for r in results]
        assert sum(valid_results) == 1, (
            f"并发 10 次应恰好一个 valid=True,实际: {valid_results}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_double_click_different_nonces_both_succeed(self, store):
        """B9: 2 个不同 nonce 的 callback_data 并发调用,2 次都成功。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        # 两个不同的 token(不同 nonce,不同 principal_id 避免干扰)
        token1 = await sign_button_token_with_nonce(
            principal_id=10003,
            action="ban",
            payload="user_1",
            ttl=3600,
        )
        token2 = await sign_button_token_with_nonce(
            principal_id=10004,
            action="ban",
            payload="user_2",
            ttl=3600,
        )
        # 并发双击(两个不同 token)
        (r1, r2) = await asyncio.gather(
            verify_button_token(token1, current_user_id=10003, store=store),
            verify_button_token(token2, current_user_id=10004, store=store),
        )
        # 两个都应成功(不同 nonce,无竞争)
        assert r1[0] is True, "token1 应成功"
        assert r2[0] is True, "token2 应成功"
        assert r1[1] == "ban" and r1[2] == "user_1"
        assert r2[1] == "ban" and r2[2] == "user_2"


# ════════════════════════════════════════════════════════════════
# C. 过期 callback(2 测试)
# ════════════════════════════════════════════════════════════════


class TestExpiredCallback:
    """R50 P1-2: 过期 callback 必须拒绝。"""

    @pytest.mark.asyncio
    async def test_expired_callback_rejected(self, store):
        """C10: ttl=1 秒,sleep 2 秒后 verify 返回 False。

        使用真实 wall-clock sleep,确保 expire_ts < now 触发拒绝。
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10005,
            action="restore",
            payload="doc_1",
            ttl=1,  # 1 秒过期
        )
        # 等待 2 秒,确保 now > expire_ts
        time.sleep(2)
        valid, action, data = await verify_button_token(
            token, current_user_id=10005, store=store
        )
        assert valid is False, "过期 callback 必须拒绝"
        assert action == ""
        assert data == ""

    @pytest.mark.asyncio
    async def test_just_before_expiry_succeeds(self, store):
        """C11: ttl=10 秒,sleep 9 秒后 verify 仍成功(临界有效)。

        使用真实 wall-clock sleep,确保 expire_ts > now 仍通过。
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10006,
            action="purge",
            payload="batch_1",
            ttl=10,  # 10 秒过期
        )
        # 等待 9 秒,still before expiry
        time.sleep(9)
        valid, action, data = await verify_button_token(
            token, current_user_id=10006, store=store
        )
        assert valid is True, "临界有效(未过期)callback 必须通过"
        assert action == "purge"
        assert data == "batch_1"


# ════════════════════════════════════════════════════════════════
# D. 跨用户篡改(2 测试)
# ════════════════════════════════════════════════════════════════


class TestCrossUserTampering:
    """R50 P1-2: 跨用户篡改防护(user_id 绑定到签名)。"""

    @pytest.mark.asyncio
    async def test_cross_user_callback_rejected(self, store):
        """D12: user_id=100 签名的 callback,用 user_id=200 验证 → False。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=100,  # 签名时 user_id=100
            action="ban",
            payload="target_user",
            ttl=3600,
        )
        # 用 user_id=200 验证(跨用户)
        valid, action, data = await verify_button_token(
            token, current_user_id=200, store=store
        )
        assert valid is False, "跨用户 callback 必须拒绝"
        assert action == ""
        assert data == ""

    @pytest.mark.asyncio
    async def test_tampered_user_id_rejected(self, store):
        """D13: 修改 callback_data 中 user_id 字段 → 签名不匹配 → False。

        构造方式:
        1. 生成 user_id=100 的合法 token(6 段格式)
        2. 把第一段 user_id 改成 200,拼接其余字段
        3. 用 current_user_id=200 验证
           - user_id 解析后 = 200,匹配 current_user_id ✓
           - 但签名 payload 用的是 user_id=100,签名不匹配 → 拒绝
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        # 生成合法 token(user_id=100)
        token = await sign_button_token_with_nonce(
            principal_id=100,
            action="ban",
            payload="victim_user",
            ttl=3600,
        )
        # 解析 6 段
        parts = token.split(":")
        assert len(parts) >= 6, f"token 应为 6 段格式,实际: {len(parts)} 段"
        # 篡改 user_id:100 → 200(签名字段保留原值,所以签名不再匹配)
        parts[0] = "200"
        tampered_token = ":".join(parts)
        # 验证:user_id 解析 = 200,匹配 current_user_id=200,但签名不匹配
        valid, action, data = await verify_button_token(
            tampered_token, current_user_id=200, store=store
        )
        assert valid is False, "篡改 user_id 后签名不匹配,必须拒绝"
        assert action == ""
        assert data == ""


# ════════════════════════════════════════════════════════════════
# E. 重放攻击(3 测试)
# ════════════════════════════════════════════════════════════════


class TestReplayAttack:
    """R50 P1-2: 重放攻击防护(nonce 一次性 + 签名完整性)。"""

    @pytest.mark.asyncio
    async def test_replay_after_consume_rejected(self, store):
        """E14: 同一 callback 第一次 verify 成功,第二次 verify 失败(nonce 已消费)。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10007,
            action="admin_grant",
            payload="role_admin",
            ttl=3600,
        )
        # 第一次:成功(消费 nonce)
        valid1, action1, data1 = await verify_button_token(
            token, current_user_id=10007, store=store
        )
        assert valid1 is True
        assert action1 == "admin_grant"
        assert data1 == "role_admin"
        # 第二次:重放,nonce 已消费 → 失败
        valid2, action2, data2 = await verify_button_token(
            token, current_user_id=10007, store=store
        )
        assert valid2 is False, "重放(nonce 已消费)必须拒绝"
        assert action2 == ""
        assert data2 == ""

    @pytest.mark.asyncio
    async def test_replay_with_forged_signature_rejected(self, store):
        """E15: 篡改 signature 字段 → 签名不匹配 → False。"""
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10008,
            action="ban",
            payload="user_xyz",
            ttl=3600,
        )
        # 解析 6 段
        parts = token.split(":")
        assert len(parts) >= 6
        # 篡改 signature(最后一段):翻转几个字符
        original_sig = parts[-1]
        # 把第一个字符换成不同的十六进制字符(确保签名变化)
        first_char = original_sig[0]
        new_first_char = "0" if first_char != "0" else "1"
        forged_sig = new_first_char + original_sig[1:]
        assert forged_sig != original_sig, "篡改后签名必须与原签名不同"
        parts[-1] = forged_sig
        forged_token = ":".join(parts)
        # 验证:签名不匹配 → 拒绝
        valid, action, data = await verify_button_token(
            forged_token, current_user_id=10008, store=store
        )
        assert valid is False, "伪造签名必须拒绝"
        assert action == ""
        assert data == ""

    @pytest.mark.asyncio
    async def test_replay_cross_action_rejected(self, store):
        """E16: action="ban" 的 callback 改 action="view" → 签名不匹配 → False。

        构造方式:
        1. 生成 action="ban" 的合法 token(6 段)
        2. 把 action 段改成 "view"(签名字段保留原值)
        3. 验证:签名 payload 中的 action 是 "ban",token 中的 action 是 "view"
           → 重算签名不匹配 → 拒绝

        防护意义:防止把高风险 ban 按钮的 token "降级"伪装成低风险 view
        去绕过 action 校验,或反之。
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=10009,
            action="ban",  # 原始 action 是高风险 ban
            payload="target_user",
            ttl=3600,
        )
        # 解析 6 段
        parts = token.split(":")
        assert len(parts) >= 6
        # 篡改 action:ban → view
        assert parts[1] == "ban"
        parts[1] = "view"
        cross_action_token = ":".join(parts)
        # 验证:签名 payload 中的 action 是 "ban",token 中的 action 是 "view"
        # → 签名不匹配 → 拒绝
        valid, action, data = await verify_button_token(
            cross_action_token, current_user_id=10009, store=store
        )
        assert valid is False, "跨 action 篡改(callback 中 action 与签名 payload 中 action 不一致)必须拒绝"
        assert action == ""
        assert data == ""


# ════════════════════════════════════════════════════════════════
# F. 高风险 action 必须使用 nonce(2 测试)
# ════════════════════════════════════════════════════════════════


class TestHighRiskActionMustUseNonce:
    """R50 P1-2: 高风险 action 必须使用 6 段格式(含 nonce)。

    高风险 action(ban/takedown/purge 等)若用旧 5 段格式(无 nonce),
    则无法防重放,必须拒绝。低风险 action 允许 5 段格式向后兼容。
    """

    @pytest.mark.asyncio
    async def test_high_risk_action_without_nonce_rejected(self, store):
        """F17: ban action 用 5 段格式(无 nonce)→ verify_button_token 返回 False。

        构造方式:手工构造 5 段 token(无 nonce 字段),
        verify_button_token 检测到高风险 action + 无 nonce → 拒绝。
        """
        from services.button_security import _sign, verify_button_token

        # 手工构造 5 段格式 token(无 nonce)
        expire_ts = int(time.time()) + 3600
        payload = f"10010:ban:target_user:{expire_ts}"
        signature = _sign(payload)
        token = f"{payload}:{signature}"  # 5 段格式
        # 验证:高风险 action + 5 段格式 → 拒绝
        valid, action, data = await verify_button_token(
            token, current_user_id=10010, store=store
        )
        assert valid is False, "高风险 action 用 5 段格式(无 nonce)必须拒绝"
        assert action == ""
        assert data == ""

    def test_low_risk_action_allows_sync_api(self):
        """F18: view action 用 5 段格式 → verify_signed_callback 返回 True(向后兼容)。

        低风险 action(view/cancel/close 等)允许使用旧 5 段格式(无 nonce),
        verify_signed_callback(sync legacy 接口)通过。
        """
        from services.button_security import _sign, verify_signed_callback

        # 手工构造 5 段格式 token(无 nonce)
        expire_ts = int(time.time()) + 3600
        payload = f"10011:view:some_doc:{expire_ts}"
        signature = _sign(payload)
        token = f"{payload}:{signature}"  # 5 段格式
        # 验证:低风险 action + 5 段格式 → 通过(向后兼容)
        valid, action, data = verify_signed_callback(token, current_user_id=10011)
        assert valid is True, "低风险 action 用 5 段格式应通过(向后兼容)"
        assert action == "view"
        assert data == "some_doc"
