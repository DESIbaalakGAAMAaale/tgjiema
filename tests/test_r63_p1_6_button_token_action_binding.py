"""R63 P1-06: verify_button_token_by_handle 强制 action/audience/resource_version 绑定。

终审整改需求:
    handler 获取 ``(valid, token_action, payload)`` 后,主要分支仅重点校验
    payload/sub-action,但未形成统一的 ``token_action == expected_action`` 强制器。
    每个 report/restore/delete handler 必须拒绝跨 action handle,
    不能只依赖签名和 user id。

修复:
    ``verify_button_token_by_handle(handle, actor, expected_action,
    expected_audience, expected_resource_version)``;库内部一次性完成全部绑定
    与 nonce 消费。

测试覆盖:
1. action 不匹配 → 抛出 AppError(BUTTON_POLICY_HASH_MISMATCH, reason=action_mismatch)
2. audience 不匹配 → 抛出 AppError(BUTTON_POLICY_AUDIENCE_MISMATCH, reason=audience_mismatch)
3. resource_version 不匹配 → 抛出 AppError(BUTTON_POLICY_VERSION_MISMATCH, reason=resource_version_mismatch)
4. 全部匹配 → 返回 (True, action, payload)
5. nonce 原子消费(同一 handle_id 第二次 verify 应失败)
6. resource_version=None(默认)→ 不检查 resource_version(向后兼容)
7. 各 handler(report/restore/delete)传递正确的 expected_action
8. sign_button_token_with_handle 接受 audience / resource_version 参数
9. cache_store.button_token_lookup_with_bindings 返回完整元数据
10. AppError 错误码 + params 正确性

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
- monkeypatch 替换 database.cache_store.DB_PATH 指向临时路径
- monkeypatch 替换 get_cache_store 返回测试 store
- 固定 ADMIN_BOT_TOKEN 避免 MagicMock 干扰 HMAC
"""
from __future__ import annotations

import ast
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# Fixture
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略:
    1. 临时目录下的 test_r63_p1_6.db
    2. 直接替换 database.cache_store.DB_PATH
    3. 替换 database.cache_store.get_cache_store 返回测试 store
    4. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r63_p1_6_test_")
    db_path = Path(tmpdir) / "test_r63_p1_6.db"
    original_path = _cs_module.DB_PATH
    original_get_store = _cs_module.get_cache_store
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module.get_cache_store = lambda: s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module.get_cache_store = original_get_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r63_p1_6_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r63_p1_6_test_sender_bot_token")
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(config.settings, "ADMIN_TELEGRAM_ID", "999999")


# ════════════════════════════════════════════════════════════════
# 1. action 不匹配 → AppError(BUTTON_POLICY_HASH_MISMATCH)
# ════════════════════════════════════════════════════════════════


class TestActionMismatchEnforced:
    """R63 P1-06: token_action != expected_action → fail-closed AppError。"""

    @pytest.mark.asyncio
    async def test_action_mismatch_raises_apperror(self, store):
        """签名 token action="report",但 handler 期望 expected_action="restore" → 抛出。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        # 期望 restore,但 token 是 report → 应抛 AppError(action_mismatch)
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="restore",  # 不匹配 token 的 action="report"
                expected_audience="admin_callback",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH, (
            f"action 不匹配应抛 BUTTON_POLICY_HASH_MISMATCH,"
            f"实际: {exc_info.value.code}"
        )
        # 验证 params 包含 reason / expected / actual
        params = exc_info.value.params
        assert params.get("reason") == "action_mismatch", (
            f"reason 应为 action_mismatch,实际: {params.get('reason')}"
        )
        assert params.get("expected") == "restore", (
            f"expected 应为 restore,实际: {params.get('expected')}"
        )
        assert params.get("actual") == "report", (
            f"actual 应为 report,实际: {params.get('actual')}"
        )

    @pytest.mark.asyncio
    async def test_action_mismatch_replay_protection(self, store):
        """action 不匹配时,nonce 仍被消费(防重放)。

        R63 P1-06: action 检查发生在 verify_button_token(签名 + nonce 消费)之后,
        所以即使 action 不匹配抛出 AppError,nonce 也已经被原子消费。
        重新调用 verify 应该失败(nonce 已消费)。
        """
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError

        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )
        # 第一次:action 不匹配,抛 AppError(nonce 已被消费)
        with pytest.raises(AppError):
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="restore",
                expected_audience="admin_callback",
                store=store,
            )
        # 第二次:即使 expected_action 正确,nonce 已被消费 → 返回 (False, "", "")
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is False, (
            "action 不匹配后,nonce 已消费,第二次 verify 应失败(防重放)"
        )


# ════════════════════════════════════════════════════════════════
# 2. audience 不匹配 → AppError(BUTTON_POLICY_AUDIENCE_MISMATCH)
# ════════════════════════════════════════════════════════════════


class TestAudienceMismatchEnforced:
    """R63 P1-06: token_audience != expected_audience → fail-closed AppError。"""

    @pytest.mark.asyncio
    async def test_audience_mismatch_raises_apperror(self, store):
        """token audience="admin_callback",但 handler 期望 "other_callback" → 抛出。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
            audience="admin_callback",  # 默认 audience
        )
        # 期望 other_callback,但 token 是 admin_callback → 应抛 AppError
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="report",
                expected_audience="other_callback",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH, (
            f"audience 不匹配应抛 BUTTON_POLICY_AUDIENCE_MISMATCH,"
            f"实际: {exc_info.value.code}"
        )
        params = exc_info.value.params
        assert params.get("reason") == "audience_mismatch"
        assert params.get("expected") == "other_callback"
        assert params.get("actual") == "admin_callback"

    @pytest.mark.asyncio
    async def test_custom_audience_match_accepted(self, store):
        """自定义 audience 匹配时应通过(用于多 handler 场景)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
            audience="custom_handler_audience",
        )
        valid, action, payload = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="custom_handler_audience",
            store=store,
        )
        assert valid is True
        assert action == "report"
        assert payload == "ban|12345"


# ════════════════════════════════════════════════════════════════
# 3. resource_version 不匹配 → AppError(BUTTON_POLICY_VERSION_MISMATCH)
# ════════════════════════════════════════════════════════════════


class TestResourceVersionMismatchEnforced:
    """R63 P1-06: resource_version 不匹配 → fail-closed AppError。"""

    @pytest.mark.asyncio
    async def test_resource_version_mismatch_raises_apperror(self, store):
        """token resource_version="v1",但 handler 期望 "v2" → 抛出。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            resource_version="file_code_abc:v1",
        )
        # 期望 v2,但 token 是 v1 → 应抛 AppError
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="delete_file",
                expected_audience="admin_callback",
                expected_resource_version="file_code_abc:v2",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH, (
            f"resource_version 不匹配应抛 BUTTON_POLICY_VERSION_MISMATCH,"
            f"实际: {exc_info.value.code}"
        )
        params = exc_info.value.params
        assert params.get("reason") == "resource_version_mismatch"
        assert params.get("expected") == "file_code_abc:v2"
        assert params.get("actual") == "file_code_abc:v1"

    @pytest.mark.asyncio
    async def test_resource_version_match_accepted(self, store):
        """resource_version 匹配时应通过。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            resource_version="file_code_abc:v3",
        )
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="delete_file",
            expected_audience="admin_callback",
            expected_resource_version="file_code_abc:v3",
            store=store,
        )
        assert valid is True

    @pytest.mark.asyncio
    async def test_resource_version_none_skips_check(self, store):
        """expected_resource_version=None(默认)→ 不检查 resource_version。

        向后兼容:旧 handler 不传 expected_resource_version,
        即使 token 携带 resource_version,也跳过检查。
        """
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            resource_version="file_code_abc:v1",
        )
        # 不传 expected_resource_version(默认 None)→ 应通过(向后兼容)
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="delete_file",
            expected_audience="admin_callback",
            # expected_resource_version 默认 None
            store=store,
        )
        assert valid is True, (
            "expected_resource_version=None 应跳过 resource_version 检查(向后兼容)"
        )

    @pytest.mark.asyncio
    async def test_token_without_resource_version_but_expected_set(self, store):
        """token 未携带 resource_version,但 handler 期望非 None → 应抛 AppError。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        # 不传 resource_version(默认 "")
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            # resource_version 默认 "" → 存为 None
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="delete_file",
                expected_audience="admin_callback",
                expected_resource_version="file_code_abc:v1",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH


# ════════════════════════════════════════════════════════════════
# 4. 全部匹配 → (True, action, payload)
# ════════════════════════════════════════════════════════════════


class TestAllBindingsMatchAccepted:
    """R63 P1-06: 所有绑定匹配 → 返回 (True, action, payload)。"""

    @pytest.mark.asyncio
    async def test_all_bindings_match_returns_valid(self, store):
        """action + audience + resource_version 全部匹配 → 通过。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            audience="admin_callback",
            resource_version="file_code_abc:v1",
        )
        valid, action, payload = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="delete_file",
            expected_audience="admin_callback",
            expected_resource_version="file_code_abc:v1",
            store=store,
        )
        assert valid is True
        assert action == "delete_file"
        assert payload == "file_code_abc"

    @pytest.mark.asyncio
    async def test_default_audience_admin_callback(self, store):
        """sign_button_token_with_handle 默认 audience='admin_callback'。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        # 不传 audience 参数(应默认 'admin_callback')
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
        )
        # verify 时传 expected_audience='admin_callback' 应通过
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is True, (
            "默认 audience 应为 'admin_callback',与 handler expected_audience 一致"
        )


# ════════════════════════════════════════════════════════════════
# 5. nonce 原子消费(防重放)
# ════════════════════════════════════════════════════════════════


class TestNonceAtomicConsume:
    """R63 P1-06: nonce 原子消费(同一 handle_id 第二次 verify 应失败)。"""

    @pytest.mark.asyncio
    async def test_replay_after_success_rejected(self, store):
        """第一次 verify 成功 → 第二次 verify 应失败(nonce 已消费)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
        )
        # 第一次成功
        valid1, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid1 is True
        # 第二次失败(nonce 已消费)
        valid2, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid2 is False, (
            "重放应被拒绝(nonce 原子消费,防重放攻击)"
        )

    @pytest.mark.asyncio
    async def test_replay_after_action_mismatch_rejected(self, store):
        """action 不匹配抛 AppError 后,nonce 已被消费(防绕过)。

        R63 P1-06 关键安全保证:action 检查发生在 nonce 消费之后,
        攻击者无法通过故意触发 action_mismatch 来"重置" nonce。
        """
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError

        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
        )
        # 第一次:故意传错 expected_action 触发 AppError
        with pytest.raises(AppError):
            await verify_button_token_by_handle(
                handle_id, current_user_id=1001,
                expected_action="restore",  # 错误 action
                expected_audience="admin_callback",
                store=store,
            )
        # 第二次:传正确 expected_action 也应失败(nonce 已被消费)
        valid, _, _ = await verify_button_token_by_handle(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is False, (
            "action 不匹配触发 AppError 后,nonce 已被消费,重放应被拒绝"
        )


# ════════════════════════════════════════════════════════════════
# 6. 各 handler(report/restore/delete)传递正确的 expected_action
# ════════════════════════════════════════════════════════════════


class TestHandlerExpectedActionBindings:
    """R63 P1-06: 每个 handler(report/restore/delete)传递正确的 expected_action。"""

    def _parse_callback_module(self):
        """解析 bots/admin_bot/callback.py 的 AST。"""
        cb_path = REPO_ROOT / "bots" / "admin_bot" / "callback.py"
        source = cb_path.read_text(encoding="utf-8")
        return ast.parse(source)

    def _get_handler_node(self, tree, name: str):
        """从 AST 中提取指定函数节点。"""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
        return None

    def _get_call_keywords(self, func_node, target_func: str) -> list[dict]:
        """提取函数体内对 target_func 的所有调用的 keyword 参数。

        Returns:
            list of dict: 每个 dict 是一次调用的 keyword 参数 {name: value_str}
        """
        calls = []
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name != target_func:
                continue
            kwargs = {}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                # 仅提取字面量值(字符串/None),复杂表达式跳过
                if isinstance(kw.value, ast.Constant):
                    kwargs[kw.arg] = kw.value.value
                else:
                    kwargs[kw.arg] = "<expr>"
            calls.append(kwargs)
        return calls

    def test_report_handler_passes_expected_action_report(self):
        """_handle_report_action 调用 verify_button_token_by_handle 时
        必须传 expected_action='report', expected_audience='admin_callback'。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_report_action")
        assert handler is not None, "_handle_report_action 函数应在 callback.py 中"
        calls = self._get_call_keywords(handler, "verify_button_token_by_handle")
        assert calls, (
            "_handle_report_action 应调用 verify_button_token_by_handle"
        )
        # 至少一次调用满足 report + admin_callback
        matched = any(
            c.get("expected_action") == "report"
            and c.get("expected_audience") == "admin_callback"
            for c in calls
        )
        assert matched, (
            "_handle_report_action 应传 expected_action='report', "
            f"expected_audience='admin_callback',实际调用: {calls}"
        )

    def test_restore_handler_passes_expected_action_restore(self):
        """_handle_restore_action 调用 verify_button_token_by_handle 时
        必须传 expected_action='restore', expected_audience='admin_callback'。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_restore_action")
        assert handler is not None, "_handle_restore_action 函数应在 callback.py 中"
        calls = self._get_call_keywords(handler, "verify_button_token_by_handle")
        assert calls, (
            "_handle_restore_action 应调用 verify_button_token_by_handle"
        )
        matched = any(
            c.get("expected_action") == "restore"
            and c.get("expected_audience") == "admin_callback"
            for c in calls
        )
        assert matched, (
            "_handle_restore_action 应传 expected_action='restore', "
            f"expected_audience='admin_callback',实际调用: {calls}"
        )

    def test_delete_file_handler_passes_expected_action_delete_file(self):
        """_handle_delete_file_action 调用 verify_button_token_by_handle 时
        必须传 expected_action='delete_file', expected_audience='admin_callback'。"""
        tree = self._parse_callback_module()
        handler = self._get_handler_node(tree, "_handle_delete_file_action")
        assert handler is not None, "_handle_delete_file_action 函数应在 callback.py 中"
        calls = self._get_call_keywords(handler, "verify_button_token_by_handle")
        assert calls, (
            "_handle_delete_file_action 应调用 verify_button_token_by_handle"
        )
        matched = any(
            c.get("expected_action") == "delete_file"
            and c.get("expected_audience") == "admin_callback"
            for c in calls
        )
        assert matched, (
            "_handle_delete_file_action 应传 expected_action='delete_file', "
            f"expected_audience='admin_callback',实际调用: {calls}"
        )

    def test_all_three_handlers_use_distinct_action_strings(self):
        """三个 handler 的 expected_action 必须互不相同(防跨 action 滥用)。"""
        tree = self._parse_callback_module()
        actions = {}
        for handler_name, expected_action in [
            ("_handle_report_action", "report"),
            ("_handle_restore_action", "restore"),
            ("_handle_delete_file_action", "delete_file"),
        ]:
            handler = self._get_handler_node(tree, handler_name)
            assert handler is not None, f"{handler_name} 函数应在 callback.py 中"
            calls = self._get_call_keywords(handler, "verify_button_token_by_handle")
            assert calls, f"{handler_name} 应调用 verify_button_token_by_handle"
            matched = any(c.get("expected_action") == expected_action for c in calls)
            assert matched, (
                f"{handler_name} 应传 expected_action='{expected_action}',"
                f"实际调用: {calls}"
            )
            actions[handler_name] = expected_action
        # 三个 action 必须互不相同
        unique = set(actions.values())
        assert len(unique) == 3, (
            f"三个 handler 的 expected_action 必须互不相同(防跨 action 滥用),"
            f"实际: {actions}"
        )


# ════════════════════════════════════════════════════════════════
# 7. sign_button_token_with_handle 接受 audience / resource_version
# ════════════════════════════════════════════════════════════════


class TestSignButtonTokenWithHandleNewParams:
    """R63 P1-06: sign_button_token_with_handle 接受 audience / resource_version 参数。"""

    def test_sign_function_signature_has_audience_param(self):
        """sign_button_token_with_handle 签名应包含 audience 参数(默认 'admin_callback')。"""
        import inspect
        from services.button_security import sign_button_token_with_handle
        sig = inspect.signature(sign_button_token_with_handle)
        assert "audience" in sig.parameters, (
            "sign_button_token_with_handle 应有 audience 参数"
        )
        assert sig.parameters["audience"].default == "admin_callback", (
            f"audience 默认值应为 'admin_callback',"
            f"实际: {sig.parameters['audience'].default}"
        )

    def test_sign_function_signature_has_resource_version_param(self):
        """sign_button_token_with_handle 签名应包含 resource_version 参数(默认 '')。"""
        import inspect
        from services.button_security import sign_button_token_with_handle
        sig = inspect.signature(sign_button_token_with_handle)
        assert "resource_version" in sig.parameters, (
            "sign_button_token_with_handle 应有 resource_version 参数"
        )
        assert sig.parameters["resource_version"].default == "", (
            f"resource_version 默认值应为 ''(空串),"
            f"实际: {sig.parameters['resource_version'].default}"
        )

    def test_verify_function_signature_has_required_params(self):
        """verify_button_token_by_handle 签名应包含 expected_action / expected_audience /
        expected_resource_version(None 默认)。"""
        import inspect
        from services.button_security import verify_button_token_by_handle
        sig = inspect.signature(verify_button_token_by_handle)
        assert "expected_action" in sig.parameters, (
            "verify_button_token_by_handle 应有 expected_action 参数"
        )
        assert "expected_audience" in sig.parameters, (
            "verify_button_token_by_handle 应有 expected_audience 参数"
        )
        assert "expected_resource_version" in sig.parameters, (
            "verify_button_token_by_handle 应有 expected_resource_version 参数"
        )
        assert sig.parameters["expected_resource_version"].default is None, (
            "expected_resource_version 默认应为 None(可选检查)"
        )


# ════════════════════════════════════════════════════════════════
# 8. cache_store.button_token_lookup_with_bindings 元数据查询
# ════════════════════════════════════════════════════════════════


class TestButtonTokenLookupWithBindings:
    """R63 P1-06: cache_store.button_token_lookup_with_bindings 返回完整元数据。"""

    @pytest.mark.asyncio
    async def test_lookup_with_bindings_returns_full_metadata(self, store):
        """存储 token + audience + resource_version 后,lookup_with_bindings 应返回完整字典。"""
        from services.button_security import sign_button_token_with_handle
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
            audience="admin_callback",
            resource_version="file_code_abc:v1",
        )
        bindings = await store.button_token_lookup_with_bindings(handle_id)
        assert bindings is not None, "lookup_with_bindings 不应返回 None"
        assert bindings["token"], "token 字段不应为空"
        assert bindings["principal_id"] == 1001
        assert bindings["action"] == "delete_file"
        assert bindings["audience"] == "admin_callback"
        assert bindings["resource_version"] == "file_code_abc:v1"

    @pytest.mark.asyncio
    async def test_lookup_with_bindings_unknown_handle_returns_none(self, store):
        """未知 handle_id → 返回 None。"""
        bindings = await store.button_token_lookup_with_bindings("nonexistent_xyz")
        assert bindings is None, "未知 handle_id 应返回 None"

    @pytest.mark.asyncio
    async def test_lookup_with_bindings_no_audience_returns_none_value(self, store):
        """未传 audience 的 token(默认 'admin_callback'),lookup 应返回该 audience。"""
        from services.button_security import sign_button_token_with_handle
        handle_id = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345",
            # audience 默认 'admin_callback'
        )
        bindings = await store.button_token_lookup_with_bindings(handle_id)
        assert bindings is not None
        assert bindings["audience"] == "admin_callback", (
            "默认 audience 应为 'admin_callback'"
        )
        # resource_version 默认 "" → 存为 NULL
        assert bindings["resource_version"] is None, (
            "未传 resource_version 时,存储应为 None"
        )


# ════════════════════════════════════════════════════════════════
# 9. 错误码 + ErrorDefinition 注册 + locale
# ════════════════════════════════════════════════════════════════


class TestErrorCodeRegistration:
    """R63 P1-06: BUTTON_POLICY_AUDIENCE_MISMATCH 错误码 + ErrorDefinition + locale。"""

    def test_error_code_constant_exists(self):
        """ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH 常量存在。"""
        from services.error_codes import ErrorCodes
        assert ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH == "BUTTON.POLICY.AUDIENCE_MISMATCH"

    def test_error_definition_registered(self):
        """ErrorRegistry 中应已注册 BUTTON_POLICY_AUDIENCE_MISMATCH。"""
        from services.error_codes import ErrorRegistry, ErrorCodes
        defn = ErrorRegistry.get(ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH)
        assert defn is not None, (
            "BUTTON_POLICY_AUDIENCE_MISMATCH 应在 ErrorRegistry 中注册"
        )
        assert defn.code == "BUTTON.POLICY.AUDIENCE_MISMATCH"
        assert defn.http_status == 403, (
            f"audience_mismatch 应为 403,实际: {defn.http_status}"
        )
        assert defn.severity == "critical"
        # safe_params 应包含 expected / actual(用于诊断)
        assert "expected" in defn.safe_params, (
            f"safe_params 应包含 'expected',实际: {defn.safe_params}"
        )
        assert "actual" in defn.safe_params, (
            f"safe_params 应包含 'actual',实际: {defn.safe_params}"
        )
        assert "reason" in defn.safe_params

    def test_message_key_in_zh_cn_locale(self):
        """zh-CN.json 应包含 button.policy.audience_mismatch 键。"""
        import json
        locale_path = REPO_ROOT / "locales" / "zh-CN.json"
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        errors_dict = data.get("errors", {})
        assert "button.policy.audience_mismatch" in errors_dict, (
            "zh-CN.json errors 段应包含 button.policy.audience_mismatch 键"
        )

    def test_message_key_in_en_us_locale(self):
        """en-US.json 应包含 button.policy.audience_mismatch 键。"""
        import json
        locale_path = REPO_ROOT / "locales" / "en-US.json"
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        errors_dict = data.get("errors", {})
        assert "button.policy.audience_mismatch" in errors_dict, (
            "en-US.json errors 段应包含 button.policy.audience_mismatch 键"
        )

    def test_apperror_with_audience_mismatch_renders_message(self):
        """AppError(BUTTON_POLICY_AUDIENCE_MISMATCH) 应能渲染 i18n 消息。"""
        from services.error_codes import AppError, ErrorCodes
        err = AppError(
            ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH,
            params={
                "action": "report",
                "reason": "audience_mismatch",
                "expected": "admin_callback",
                "actual": "other_callback",
            },
        )
        # i18n 消息应包含 audience 关键词(zh/en 都包含)
        msg = err.message.lower()
        assert "audience" in msg, (
            f"AppError 消息应包含 'audience' 关键词,实际: {err.message}"
        )


# ════════════════════════════════════════════════════════════════
# 10. 跨 handler 滥用端到端场景(report handle 被 restore handler 调用)
# ════════════════════════════════════════════════════════════════


class TestCrossHandlerAbuseEndToEnd:
    """R63 P1-06: 端到端跨 handler 滥用防护。

    场景:攻击者获取 report 按钮的 handle_id,尝试用 restore handler 验证。
    旧版:仅校验签名 + user_id,可能通过(若 user_id 匹配)。
    新版:expected_action='restore' 与 token_action='report' 不匹配 → 拒绝。
    """

    @pytest.mark.asyncio
    async def test_report_handle_rejected_by_restore_handler(self, store):
        """report handle 被 restore handler 调用 → 拒绝(action_mismatch)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        # 用户点击 report 按钮,获得 handle_id(action='report')
        report_handle = await sign_button_token_with_handle(
            principal_id=1001, action="report",
            payload="ban|12345|67890|dsp",
        )

        # 攻击者尝试用 restore handler 验证该 handle
        # restore handler 传 expected_action='restore',与 token_action='report' 不匹配
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                report_handle, current_user_id=1001,
                expected_action="restore",
                expected_audience="admin_callback",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH
        assert exc_info.value.params.get("reason") == "action_mismatch"

    @pytest.mark.asyncio
    async def test_restore_handle_rejected_by_delete_handler(self, store):
        """restore handle 被 delete_file handler 调用 → 拒绝(action_mismatch)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        restore_handle = await sign_button_token_with_handle(
            principal_id=1001, action="restore",
            payload="1|1|table:users",
        )
        # delete_file handler 传 expected_action='delete_file',不匹配 'restore'
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                restore_handle, current_user_id=1001,
                expected_action="delete_file",
                expected_audience="admin_callback",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH
        assert exc_info.value.params.get("reason") == "action_mismatch"

    @pytest.mark.asyncio
    async def test_delete_handle_rejected_by_report_handler(self, store):
        """delete_file handle 被 report handler 调用 → 拒绝(action_mismatch)。"""
        from services.button_security import (
            sign_button_token_with_handle,
            verify_button_token_by_handle,
        )
        from services.error_codes import AppError, ErrorCodes

        delete_handle = await sign_button_token_with_handle(
            principal_id=1001, action="delete_file",
            payload="file_code_abc",
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle(
                delete_handle, current_user_id=1001,
                expected_action="report",
                expected_audience="admin_callback",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH
        assert exc_info.value.params.get("reason") == "action_mismatch"


# ════════════════════════════════════════════════════════════════
# 11. button_tokens 表 schema 含 audience / resource_version 列
# ════════════════════════════════════════════════════════════════


class TestButtonTokensTableSchema:
    """R63 P1-06: button_tokens 表 schema 应含 audience / resource_version 列。"""

    @pytest.mark.asyncio
    async def test_button_tokens_table_has_audience_column(self, store):
        """button_tokens 表应有 audience 列。"""
        cursor = await store._db.execute("PRAGMA table_info(button_tokens)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}  # row[1] = column name
        assert "audience" in columns, (
            f"button_tokens 表应有 audience 列,实际列: {columns}"
        )
        assert "resource_version" in columns, (
            f"button_tokens 表应有 resource_version 列,实际列: {columns}"
        )

    @pytest.mark.asyncio
    async def test_button_token_store_accepts_audience_param(self, store):
        """button_token_store 接受 audience / resource_version 参数。"""
        import inspect
        sig = inspect.signature(store.button_token_store)
        assert "audience" in sig.parameters, (
            "button_token_store 应有 audience 参数"
        )
        assert "resource_version" in sig.parameters, (
            "button_token_store 应有 resource_version 参数"
        )

    @pytest.mark.asyncio
    async def test_button_token_store_persists_audience(self, store):
        """button_token_store 持久化 audience 到 button_tokens 表。"""
        await store.button_token_store(
            handle_id="test_handle_1",
            token="fake_token_1",
            principal_id=1001,
            action="report",
            audience="admin_callback",
            resource_version="file:v1",
        )
        bindings = await store.button_token_lookup_with_bindings("test_handle_1")
        assert bindings is not None
        assert bindings["audience"] == "admin_callback"
        assert bindings["resource_version"] == "file:v1"

    @pytest.mark.asyncio
    async def test_button_token_store_default_audience_none(self, store):
        """button_token_store 默认 audience=None(向后兼容旧调用方)。"""
        await store.button_token_store(
            handle_id="test_handle_2",
            token="fake_token_2",
            principal_id=1001,
            action="report",
            # 不传 audience / resource_version(默认 None)
        )
        bindings = await store.button_token_lookup_with_bindings("test_handle_2")
        assert bindings is not None
        assert bindings["audience"] is None
        assert bindings["resource_version"] is None
