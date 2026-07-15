"""R53 P1-1: Notification Dedup 迁移测试。

被测目标:
    - ``services/notifications.py`` send() 内部委托 send_with_dedup_contract()
    - send_with_dedup_contract() 返回结构化 dedup 契约(sent/deduplicated/error)
    - error 状态使用 ErrorCodes + i18n(禁止 str(e) 作为用户可见 error_msg)
    - dispatch_notification() 正确处理 deduplicated 状态
    - AST 门禁 scripts/check_notification_legacy_send.py 通过

测试策略:
    - 使用真实 SQLite 临时数据库隔离生产数据
    - 通过 mock 验证 send() → send_with_dedup_contract() 委托关系
    - 通过唯一约束冲突验证 deduplicated 状态
    - 通过 CacheStore 不可用验证 error 状态 + ErrorCodes
    - 通过子进程调用 AST 门禁脚本验证无 legacy send() 调用
"""
from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r53_p1_1_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_notifications_schema():
    """每个用例前重置 notifications outbox schema 初始化标记。"""
    try:
        from services import notifications
        notifications._reset_outbox_schema_for_test()
    except Exception:
        pass
    yield
    try:
        from services import notifications
        notifications._reset_outbox_schema_for_test()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
# 测试 1: send() 内部委托 send_with_dedup_contract() 返回结构化 dict
# ════════════════════════════════════════════════════════════════

class TestSendDelegatesToDedupContract:
    """R53 P1-1: send() 内部委托 send_with_dedup_contract()。"""

    @pytest.mark.asyncio
    async def test_send_delegates_to_send_with_dedup_contract(self, real_store):
        """send() 内部调用 send_with_dedup_contract()(通过 mock 验证委托关系)。"""
        from services import notifications

        # mock send_with_dedup_contract,验证 send() 是否委托调用
        mock_result = {
            "status": "sent",
            "notif_id": 99999,
            "outbox_id": 88888,
        }
        with patch.object(
            notifications,
            "send_with_dedup_contract",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fn:
            result = await notifications.send(
                user_id=6001,
                notif_type="ready",
                payload={"file_code": "R53_DELEGATE"},
            )
            # 验证 send_with_dedup_contract 被调用(委托成立)
            mock_fn.assert_called_once()
            # 验证参数透传
            call_args = mock_fn.call_args
            assert call_args.args[0] == 6001  # user_id
            assert call_args.args[1] == "ready"  # notif_type
            # 验证 send() 从结构化 dict 中提取 notif_id 返回(int 向后兼容)
            assert result == 99999, \
                f"send() 应从委托结果提取 notif_id,实际: {result}"

    @pytest.mark.asyncio
    async def test_send_returns_int_from_dedup_contract_dict(self, real_store):
        """send() 从 send_with_dedup_contract() 的结构化 dict 中提取 notif_id(int)。"""
        from services import notifications

        # 真实调用(非 mock):验证 send() 返回 int(从 dict 提取)
        notif_id = await notifications.send(
            user_id=6002,
            notif_type="ready",
            payload={"file_code": "R53_REAL"},
        )
        assert isinstance(notif_id, int), \
            f"send() 应返回 int(从 dict 提取 notif_id),实际类型: {type(notif_id)}"
        assert notif_id > 0, f"send() 应返回 >0 的 notif_id,实际: {notif_id}"

    @pytest.mark.asyncio
    async def test_send_with_dedup_contract_returns_structured_dict(self, real_store):
        """send_with_dedup_contract() 返回结构化 dict(含 status/notif_id/outbox_id)。"""
        from services.notifications import send_with_dedup_contract

        result = await send_with_dedup_contract(
            user_id=6003,
            notif_type="ready",
            payload={"file_code": "R53_DICT"},
        )
        # 验证返回是 dict(结构化契约)
        assert isinstance(result, dict), \
            f"send_with_dedup_contract 应返回 dict,实际类型: {type(result)}"
        assert "status" in result, "结构化契约应包含 status 字段"
        assert "notif_id" in result, "结构化契约应包含 notif_id 字段"
        assert "outbox_id" in result, "结构化契约应包含 outbox_id 字段"
        assert result["status"] == "sent", \
            f"新通知应为 sent 状态,实际: {result['status']}"
        assert result["notif_id"] > 0
        assert result["outbox_id"] > 0


# ════════════════════════════════════════════════════════════════
# 测试 2: send_with_dedup_contract() 返回 deduplicated 状态
# ════════════════════════════════════════════════════════════════

class TestDedupContractDeduplicatedStatus:
    """R53 P1-1: send_with_dedup_contract() 去重命中返回 deduplicated 状态。"""

    @pytest.mark.asyncio
    async def test_dedup_returns_deduplicated_status(self, real_store):
        """同 dedup_key + 同 window 的重复插入 → 返回 deduplicated 状态。"""
        from services.notifications import send_with_dedup_contract

        # 第一次发送(带 dedup_key)
        result1 = await send_with_dedup_contract(
            user_id=6101,
            notif_type="ready",
            payload={"file_code": "R53_DEDUP_1", "_dedup_key": "r53_dedup:6101"},
        )
        assert result1["status"] == "sent"
        original_notif_id = result1["notif_id"]
        assert original_notif_id > 0

        # 第二次发送(同 user_id + dedup_key + window → 唯一约束冲突)
        result2 = await send_with_dedup_contract(
            user_id=6101,
            notif_type="ready",
            payload={"file_code": "R53_DEDUP_2", "_dedup_key": "r53_dedup:6101"},
        )
        assert result2["status"] == "deduplicated", \
            f"重复 dedup_key 应返回 deduplicated,实际: {result2['status']}"
        # 去重应返回现有权威记录的 notif_id
        assert result2["notif_id"] == original_notif_id, \
            f"去重应返回原 notif_id={original_notif_id},实际: {result2['notif_id']}"
        assert result2.get("dedup_key") == "r53_dedup:6101"

    @pytest.mark.asyncio
    async def test_dedup_different_users_not_deduplicated(self, real_store):
        """同 dedup_key 不同 user_id → 不去重(均 sent)。"""
        from services.notifications import send_with_dedup_contract

        r1 = await send_with_dedup_contract(
            user_id=6102,
            notif_type="ready",
            payload={"file_code": "R53_U1", "_dedup_key": "shared_r53"},
        )
        r2 = await send_with_dedup_contract(
            user_id=6103,
            notif_type="ready",
            payload={"file_code": "R53_U2", "_dedup_key": "shared_r53"},
        )
        assert r1["status"] == "sent"
        assert r2["status"] == "sent", \
            f"不同 user_id 不应去重,实际: {r2['status']}"
        assert r1["notif_id"] != r2["notif_id"]


# ════════════════════════════════════════════════════════════════
# 测试 3: error 状态使用 ErrorCodes 而非 str(e)
# ════════════════════════════════════════════════════════════════

class TestDedupContractErrorUsesErrorCodes:
    """R53 P1-1: error 状态使用 ErrorCodes + i18n(禁止 str(e))。"""

    @pytest.mark.asyncio
    async def test_error_uses_error_code_not_str_e(self, real_store):
        """CacheStore 不可用时,error_msg 为 i18n 消息(非 str(e))。"""
        from services.notifications import send_with_dedup_contract
        from services.error_codes import ErrorCodes
        from database.cache_store import get_cache_store

        # Mock store._db 为 None(模拟 CacheStore 不可用)
        store = get_cache_store()
        original_db = store._db
        store._db = None
        try:
            result = await send_with_dedup_contract(
                user_id=6201,
                notif_type="ready",
                payload={"file_code": "R53_ERR"},
            )
            assert result["status"] == "error"
            assert result["notif_id"] == 0
            # 验证使用 ErrorCodes(非裸字符串)
            assert result["error_code"] == ErrorCodes.DB_CACHE_UNAVAILABLE
            # 验证 error_msg 不是 str(e) 形式(应为 i18n 消息)
            error_msg = result["error_msg"]
            assert isinstance(error_msg, str)
            assert len(error_msg) > 0
            # error_msg 不应包含 "NoneType" 或异常对象字符串表示
            assert "NoneType" not in error_msg, \
                f"error_msg 不应包含 str(e) 形式,实际: {error_msg}"
        finally:
            store._db = original_db

    @pytest.mark.asyncio
    async def test_error_on_write_failure_uses_error_codes(self, real_store):
        """写入失败时 error_code 为 NOTIFICATION_OUTBOX_WRITE_FAILED(非 str(e))。"""
        from services.notifications import send_with_dedup_contract
        from services.error_codes import ErrorCodes

        # 先正常发送一条(确保 schema 已初始化)
        await send_with_dedup_contract(
            user_id=6202, notif_type="ready",
            payload={"file_code": "INIT"},
        )

        # Mock tx.execute 在 INSERT INTO notification_outbox 时抛异常
        original_execute = real_store._db.execute

        async def mock_execute(sql, params=None):
            if "INSERT INTO notification_outbox" in sql:
                raise RuntimeError("simulated R53 write failure for test")
            return await original_execute(sql, params)

        with patch.object(real_store._db, "execute", side_effect=mock_execute):
            result = await send_with_dedup_contract(
                user_id=6203,
                notif_type="ready",
                payload={"file_code": "R53_WRITE_FAIL"},
            )
        assert result["status"] == "error"
        assert result["notif_id"] == 0
        # 验证 error_code 是 ErrorCodes 常量(非 str(e))
        assert result["error_code"] == ErrorCodes.NOTIFICATION_OUTBOX_WRITE_FAILED
        # 验证 error_msg 不包含异常字符串(应为 i18n 消息)
        error_msg = result["error_msg"]
        assert "simulated R53 write failure" not in error_msg, \
            f"error_msg 不应包含 str(e),实际: {error_msg}"
        # error_msg 应为 i18n 渲染后的消息(非空)
        assert len(error_msg) > 0

    @pytest.mark.asyncio
    async def test_error_msg_is_i18n_message(self, real_store):
        """error_msg 来自 ErrorRegistry i18n 渲染(可包含占位符渲染后的值)。"""
        from services.notifications import send_with_dedup_contract
        from services.error_codes import ErrorCodes, ErrorRegistry
        from database.cache_store import get_cache_store

        # CacheStore 不可用 → error_msg 应为 i18n 渲染消息
        store = get_cache_store()
        original_db = store._db
        store._db = None
        try:
            result = await send_with_dedup_contract(
                user_id=6204,
                notif_type="ready",
                payload={"file_code": "R53_I18N"},
            )
            # 通过 ErrorRegistry 独立渲染同一 code,比对消息
            envelope = ErrorRegistry.create_envelope(
                ErrorCodes.DB_CACHE_UNAVAILABLE,
                params={"component": "notifications"},
            )
            assert result["error_msg"] == envelope.message, \
                f"error_msg 应与 ErrorRegistry i18n 渲染一致,实际: {result['error_msg']}"
        finally:
            store._db = original_db


# ════════════════════════════════════════════════════════════════
# 测试 4: AST 门禁扫描通过(无 legacy send 调用)
# ════════════════════════════════════════════════════════════════

class TestASTGateNoLegacySend:
    """R53 P1-1: AST 门禁 scripts/check_notification_legacy_send.py 通过。"""

    def test_ast_gate_passes_no_legacy_send_in_business_code(self):
        """AST 门禁扫描 bots/admin/services,无 legacy send() 调用。"""
        script_path = Path(__file__).resolve().parent.parent / "scripts" / \
            "check_notification_legacy_send.py"
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, \
            f"AST 门禁应通过(exit 0),实际 exit={result.returncode}\n" \
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        assert "[OK]" in result.stdout, \
            f"AST 门禁应输出 [OK],实际: {result.stdout}"

    def test_ast_gate_detects_violation_in_temp_file(self, tmp_path):
        """AST 门禁能正确检测违规调用(构造临时违规文件验证检测能力)。"""
        # 此测试验证 AST 门禁的检测逻辑(不修改生产代码)
        # 通过直接调用 _find_legacy_send_calls 验证检测能力
        import ast
        from scripts.check_notification_legacy_send import _find_legacy_send_calls

        # 构造违规代码
        violation_code = """
from services import notifications as notif_svc

async def bad_call():
    await notif_svc.send(123, "ready", {"file_code": "BAD"})
    await notifications.send(456, "ready", {"file_code": "BAD2"})
    # 以下不应被检测(send_with_dedup_contract)
    await notif_svc.send_with_dedup_contract(789, "ready", {"file_code": "OK"})
"""
        tree = ast.parse(violation_code)
        violations = _find_legacy_send_calls(tree)
        # 应检测到 2 处违规(notif_svc.send 和 notifications.send)
        assert len(violations) == 2, \
            f"应检测到 2 处 legacy send 调用,实际: {len(violations)}"
        # R54 P1-3: violations 第三元素为 description(如 "notif_svc.send(...)"),
        # 不再是纯 alias 名。检查 description 包含合法 alias 前缀。
        valid_descriptions = {"notif_svc.send(...)", "notifications.send(...)"}
        for _, _, desc in violations:
            assert desc in valid_descriptions, \
                f"violation description 应为合法格式,实际: {desc}"


# ════════════════════════════════════════════════════════════════
# 测试 5: 已迁移的调用方(dispatch_notification)正确处理 deduplicated 状态
# ════════════════════════════════════════════════════════════════

class TestDispatchNotificationHandlesDeduplicated:
    """R53 P1-1: dispatch_notification 正确处理 deduplicated 状态。"""

    @pytest.mark.asyncio
    async def test_dispatch_returns_notif_id_on_sent(self, real_store):
        """dispatch_notification 首次投递(sent)→ 返回 notif_id(>0)。"""
        from services import notifications

        notif_id = await notifications.dispatch_notification(
            user_id=6301,
            type="ready",
            content={"file_code": "R53_DISP_1"},
            dedup_key="r53_dispatch:6301",
        )
        assert notif_id > 0, \
            f"首次投递应返回 notif_id > 0,实际: {notif_id}"

    @pytest.mark.asyncio
    async def test_dispatch_returns_zero_on_deduplicated(self, real_store):
        """dispatch_notification 重复投递(deduplicated)→ 返回 0(去重跳过)。"""
        from services import notifications

        # 第一次投递
        id1 = await notifications.dispatch_notification(
            user_id=6302,
            type="ready",
            content={"file_code": "R53_DISP_2"},
            dedup_key="r53_dispatch:6302",
        )
        assert id1 > 0

        # 第二次投递(同 dedup_key + window → deduplicated)
        id2 = await notifications.dispatch_notification(
            user_id=6302,
            type="ready",
            content={"file_code": "R53_DISP_2"},
            dedup_key="r53_dispatch:6302",
        )
        assert id2 == 0, \
            f"去重跳过应返回 0,实际: {id2}"

    @pytest.mark.asyncio
    async def test_dispatch_returns_zero_on_error(self, real_store):
        """dispatch_notification 在 error 状态下返回 0(失败)。"""
        from services import notifications
        from database.cache_store import get_cache_store

        # Mock CacheStore 不可用 → send_with_dedup_contract 返回 error
        store = get_cache_store()
        original_db = store._db
        store._db = None
        try:
            notif_id = await notifications.dispatch_notification(
                user_id=6303,
                type="ready",
                content={"file_code": "R53_DISP_ERR"},
                dedup_key="r53_dispatch:6303",
            )
            assert notif_id == 0, \
                f"error 状态应返回 0,实际: {notif_id}"
        finally:
            store._db = original_db

    @pytest.mark.asyncio
    async def test_dispatch_no_dedup_key_always_sends(self, real_store):
        """dispatch_notification 无 dedup_key → 不去重,每次都 sent。"""
        from services import notifications

        id1 = await notifications.dispatch_notification(
            user_id=6304,
            type="ready",
            content={"file_code": "R53_NO_DEDUP_1"},
            dedup_key="",  # 无 dedup_key
        )
        id2 = await notifications.dispatch_notification(
            user_id=6304,
            type="ready",
            content={"file_code": "R53_NO_DEDUP_2"},
            dedup_key="",  # 无 dedup_key
        )
        assert id1 > 0
        assert id2 > 0
        assert id1 != id2, "无 dedup_key 时不应去重,两条应都成功"

    @pytest.mark.asyncio
    async def test_dispatch_delegates_to_send_with_dedup_contract(self, real_store):
        """dispatch_notification 内部委托 send_with_dedup_contract()(mock 验证)。"""
        from services import notifications

        mock_result = {
            "status": "sent",
            "notif_id": 77777,
            "outbox_id": 66666,
        }
        with patch.object(
            notifications,
            "send_with_dedup_contract",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fn:
            notif_id = await notifications.dispatch_notification(
                user_id=6305,
                type="ready",
                content={"file_code": "R53_MOCK"},
                dedup_key="r53_mock:6305",
            )
            # 验证委托调用
            mock_fn.assert_called_once()
            # 验证 dedup_key 被注入到 content
            call_args = mock_fn.call_args
            # send_with_dedup_contract(user_id, type, payload)
            payload = call_args.args[2]
            assert "_dedup_key" in payload, "dispatch_notification 应注入 _dedup_key"
            assert payload["_dedup_key"] == "r53_mock:6305"
            # 验证从 sent 状态提取 notif_id
            assert notif_id == 77777
