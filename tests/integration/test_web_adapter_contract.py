"""R76 O5: ``services/sink_adapters/web_adapter.py`` contract 入口测试。

整改依据(R76 终审报告 10.O-O5 / P0-03 / P0-04):
    - secretless 入口必须校验 contract token 和 payload schema;
    - 端点不能接受 SQL、stream key、handler 名或任意 method;
    - ``_e2e_file_content_b64`` 严格禁止(P0-03);
    - production 启动若路由被注册则直接失败;
    - GET 状态查询不推动状态机。

测试覆盖:
    正向:
        - POST 有效 payload → 200 + accepted + trace_id;
        - 注入 mock dispatcher 后,trace_id 状态最终为 delivered;
        - GET 已知 trace_id → 200 + 状态字段完整。
    负向:
        - 缺失/错误 X-Contract-Token → 401;
        - 顶层多余 key → 400;
        - 缺 trace_id / update / message / document / file_id → 400;
        - message_id / date 非整数 → 400;
        - file_size 负数 → 400;
        - 禁止 key(sql/stream_key/handler/_e2e_file_content_b64)出现在任意层级 → 400;
        - GET 未知 trace_id → 404;
        - 非 SECRETLESS_MODE 调用 create_contract_app → RuntimeError;
        - 空 contract_token 调用 create_contract_app → RuntimeError。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from services.error_codes import AppError


# ════════════════════════════════════════════════════════════════
# 测试辅助
# ════════════════════════════════════════════════════════════════
def _ensure_secretless_mode_true() -> None:
    """确保 ``config.settings.SECRETLESS_MODE`` 为 True(覆盖 conftest mock)。

    conftest.py 注入 MagicMock 作为 config.settings,但未显式设置 SECRETLESS_MODE;
    本测试需要 SECRETLESS_MODE=True 才能调用 ``create_contract_app()``。
    """
    import config
    if not hasattr(config.settings, "SECRETLESS_MODE") or isinstance(
        getattr(config.settings, "SECRETLESS_MODE", None), MagicMock
    ):
        config.settings.SECRETLESS_MODE = True
    else:
        config.settings.SECRETLESS_MODE = True


def _set_secretless_mode(value: bool) -> None:
    """显式设置 SECRETLESS_MODE(用于负向测试)。"""
    import config
    config.settings.SECRETLESS_MODE = value


def _make_valid_payload(trace_id: str = "trace-test-001") -> dict[str, Any]:
    """构造一个通过 schema 校验的有效 payload。"""
    return {
        "update": {
            "update_id": 100001,
            "message": {
                "message_id": 200001,
                "date": 1735689600,
                "from": {"id": 111, "is_bot": False, "first_name": "CI User"},
                "chat": {"id": 111, "type": "private"},
                "document": {
                    "file_id": "sha256:abcdef0123456789",
                    "file_unique_id": "sha256:abcdef0123456789",
                    "file_name": "fixture.bin",
                    "file_size": 12,
                },
            },
        },
        "trace_id": trace_id,
    }


def _make_mock_dispatcher(*, fail: bool = False) -> Any:
    """构造 mock dispatcher(模拟 ``bots.up_bot._dispatch_media``)。

    Args:
        fail: 若为 True,mock 会抛异常以测试失败路径
    """
    async def _mock_dispatcher(update, context):
        # 验证 context.bot / context.user_data 存在
        assert context.bot is not None, "context.bot 必须存在"
        assert isinstance(context.user_data, dict), "context.user_data 必须为 dict"
        if fail:
            raise RuntimeError("mock dispatcher failure for test")
        # 模拟业务处理(只记录调用)
        _mock_dispatcher.call_count += 1
        _mock_dispatcher.last_update = update
        _mock_dispatcher.last_context = context

    _mock_dispatcher.call_count = 0
    _mock_dispatcher.last_update = None
    _mock_dispatcher.last_context = None
    return _mock_dispatcher


def _make_mock_bot() -> Any:
    """构造 mock bot 实例(模拟 ContractProviderClient)。"""
    bot = MagicMock(name="mock_bot")
    bot.send_message = AsyncMock(return_value=None)
    return bot


def _create_test_app(
    *,
    contract_token: str = "ci-test-contract-token",
    dispatcher_fail: bool = False,
):
    """构造测试用 contract app(注入 mock dispatcher 和 bot)。"""
    _ensure_secretless_mode_true()
    from services.sink_adapters.web_adapter import (
        create_contract_app,
        get_contract_transaction_registry,
    )

    # 重置 registry(避免跨用例污染)
    registry = get_contract_transaction_registry()
    registry._states.clear()

    dispatcher = _make_mock_dispatcher(fail=dispatcher_fail)
    bot = _make_mock_bot()
    app = create_contract_app(
        contract_token=contract_token,
        public_dispatcher=dispatcher,
        bot=bot,
    )
    # R81 fix: mock get_or_create_user 避免 contract dispatch 写数据库
    # (测试环境无 CRDB 连接池,get_or_create_user 会 AttributeError)
    import services.permission as _perm_mod
    _perm_mod.get_or_create_user = AsyncMock(return_value=None)
    return app, dispatcher, bot


# ════════════════════════════════════════════════════════════════
# 正向测试
# ════════════════════════════════════════════════════════════════
class TestContractUpdatePositive:
    """POST /internal/contract/update 正向测试。"""

    def test_valid_payload_returns_accepted(self):
        """有效 payload 应返回 200 + accepted 状态 + trace_id。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id="trace-positive-001")
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["trace_id"] == "trace-positive-001"
        assert data["status"] == "accepted"
        assert "accepted_at" in data

    def test_dispatcher_is_called_async(self):
        """提交后 dispatcher 应被异步调用,state 最终为 delivered。"""
        app, dispatcher, _ = _create_test_app()
        client = TestClient(app)

        trace_id = "trace-positive-002"
        payload = _make_valid_payload(trace_id=trace_id)
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 200

        # 轮询状态直到 delivered
        deadline_iters = 0
        while deadline_iters < 50:
            status_resp = client.get(
                f"/internal/contract/transactions/{trace_id}",
                headers={"X-Contract-Token": "ci-test-contract-token"},
            )
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            if status_data["status"] in ("delivered", "failed"):
                break
            deadline_iters += 1

        assert dispatcher.call_count == 1, "dispatcher 应被调用一次"
        assert status_data["status"] == "delivered", (
            f"状态应为 delivered,实际为 {status_data['status']}, "
            f"error={status_data.get('error')}"
        )
        assert status_data["completed_at"] is not None

    def test_idempotent_resubmit_returns_current_state(self):
        """同 trace_id 重复提交应返回当前状态(幂等)。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        trace_id = "trace-positive-003"
        payload = _make_valid_payload(trace_id=trace_id)

        # 第一次提交
        resp1 = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "accepted"

        # 等待 dispatcher 完成
        for _ in range(50):
            status_resp = client.get(
                f"/internal/contract/transactions/{trace_id}",
                headers={"X-Contract-Token": "ci-test-contract-token"},
            )
            if status_resp.json()["status"] in ("delivered", "failed"):
                break

        # 第二次提交(幂等返回当前状态)
        resp2 = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["trace_id"] == trace_id
        # 状态应为 delivered(已处理)
        assert data2["status"] == "delivered"


class TestContractTransactionStatusPositive:
    """GET /internal/contract/transactions/{trace_id} 正向测试。"""

    def test_get_known_trace_id_returns_state(self):
        """已知 trace_id 应返回完整状态字段。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        trace_id = "trace-status-001"
        payload = _make_valid_payload(trace_id=trace_id)
        submit_resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert submit_resp.status_code == 200

        # 等待终态
        for _ in range(50):
            status_resp = client.get(
                f"/internal/contract/transactions/{trace_id}",
                headers={"X-Contract-Token": "ci-test-contract-token"},
            )
            if status_resp.json()["status"] in ("delivered", "failed"):
                break

        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["trace_id"] == trace_id
        assert "status" in data
        assert "accepted_at" in data
        assert "completed_at" in data
        assert "details" in data
        assert "error" in data

    def test_health_endpoint_returns_ok(self):
        """/health 端点应返回 ok 状态。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "contract-adapter"
        assert data["secretless_mode"] is True


# ════════════════════════════════════════════════════════════════
# 负向测试 - 认证
# ════════════════════════════════════════════════════════════════
class TestContractUpdateAuthNegative:
    """POST /internal/contract/update 认证负向测试。"""

    def test_missing_contract_token_returns_401(self):
        """缺失 X-Contract-Token 应返回 401。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        resp = client.post("/internal/contract/update", json=payload)
        assert resp.status_code == 401
        assert "missing" in resp.json()["detail"].lower()

    def test_wrong_contract_token_returns_401(self):
        """错误的 X-Contract-Token 应返回 401。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "wrong-token"},
        )
        assert resp.status_code == 401
        assert "invalid" in resp.json()["detail"].lower()


# ════════════════════════════════════════════════════════════════
# 负向测试 - payload schema
# ════════════════════════════════════════════════════════════════
class TestContractUpdatePayloadNegative:
    """POST /internal/contract/update payload schema 负向测试。"""

    def test_payload_not_object_returns_400(self):
        """payload 非 dict 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)
        resp = client.post(
            "/internal/contract/update",
            json=[1, 2, 3],  # list 而非 dict
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_extra_top_level_key_returns_400(self):
        """顶层多余 key 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        payload["extra_field"] = "should be rejected"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "unexpected" in resp.json()["detail"].lower()

    def test_missing_trace_id_returns_400(self):
        """缺 trace_id 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        del payload["trace_id"]
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "trace_id" in resp.json()["detail"]

    def test_empty_trace_id_returns_400(self):
        """空 trace_id 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id="")
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_missing_update_returns_400(self):
        """缺 update 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = {"trace_id": "trace-test"}
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_missing_message_returns_400(self):
        """缺 update.message 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        del payload["update"]["message"]
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "message" in resp.json()["detail"]

    def test_missing_required_message_field_returns_400(self):
        """缺 update.message.from / chat / document / message_id / date 任一应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        for field_to_remove in ("from", "chat", "document", "message_id", "date"):
            payload = _make_valid_payload(trace_id=f"trace-missing-{field_to_remove}")
            del payload["update"]["message"][field_to_remove]
            resp = client.post(
                "/internal/contract/update",
                json=payload,
                headers={"X-Contract-Token": "ci-test-contract-token"},
            )
            assert resp.status_code == 400, (
                f"缺 {field_to_remove} 应返回 400,实际 {resp.status_code}"
            )

    def test_missing_file_id_returns_400(self):
        """缺 update.message.document.file_id 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        del payload["update"]["message"]["document"]["file_id"]
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_non_int_message_id_returns_400(self):
        """message_id 非整数应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        payload["update"]["message"]["message_id"] = "not-int"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "message_id" in resp.json()["detail"]

    def test_non_int_date_returns_400(self):
        """date 非整数应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        payload["update"]["message"]["date"] = "2024-01-01"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_negative_file_size_returns_400(self):
        """file_size 负数应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        payload["update"]["message"]["document"]["file_size"] = -1
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_non_positive_from_id_returns_400(self):
        """from.id 非正整数应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload()
        payload["update"]["message"]["from"]["id"] = 0
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_invalid_json_returns_400(self):
        """无效 JSON 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/contract/update",
            content=b"not json",
            headers={
                "X-Contract-Token": "ci-test-contract-token",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════
# 负向测试 - 禁止 key(防注入 / P0-03)
# ════════════════════════════════════════════════════════════════
class TestContractUpdateForbiddenKeys:
    """POST /internal/contract/update 禁止 key 测试。

    整改依据(R76 终审报告 10.O-O5 / P0-03):
        - 端点不能接受 SQL、stream key、handler 名或任意 method;
        - ``_e2e_file_content_b64`` 严格禁止(P0-03:Update 内嵌文件内容)。
    """

    @pytest.mark.parametrize("forbidden_key", [
        "sql", "query", "statement", "raw_sql",
        "stream_key", "redis_key", "queue_key",
        "handler", "handler_name", "method", "command",
        "exec", "eval", "import", "subprocess", "shell",
        "_e2e_file_content_b64", "file_content_b64", "file_content",
        "bot_override", "force_dispatch", "skip_validation",
    ])
    def test_forbidden_key_at_top_level_rejected(self, forbidden_key):
        """禁止 key 出现在 update 顶层应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id=f"trace-forbidden-top-{forbidden_key}")
        payload["update"][forbidden_key] = "malicious-value"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400, (
            f"forbidden key '{forbidden_key}' at top level should be rejected, "
            f"got {resp.status_code}"
        )
        assert "forbidden" in resp.json()["detail"].lower()

    @pytest.mark.parametrize("forbidden_key", [
        "sql", "stream_key", "handler", "method",
        "_e2e_file_content_b64", "file_content_b64",
    ])
    def test_forbidden_key_in_message_rejected(self, forbidden_key):
        """禁止 key 出现在 update.message 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id=f"trace-forbidden-msg-{forbidden_key}")
        payload["update"]["message"][forbidden_key] = "malicious-value"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"].lower()

    @pytest.mark.parametrize("forbidden_key", [
        "sql", "stream_key", "handler", "_e2e_file_content_b64",
    ])
    def test_forbidden_key_in_document_rejected(self, forbidden_key):
        """禁止 key 出现在 update.message.document 应返回 400。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id=f"trace-forbidden-doc-{forbidden_key}")
        payload["update"]["message"]["document"][forbidden_key] = "malicious-value"
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400

    def test_e2e_file_content_b64_in_update_strictly_rejected(self):
        """P0-03: ``_e2e_file_content_b64`` 严格禁止。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        payload = _make_valid_payload(trace_id="trace-p003-violation")
        # 模拟 R73 旧实现:Update 内嵌文件内容 base64
        payload["update"]["_e2e_file_content_b64"] = "aGVsbG8gd29ybGQ="
        resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 400
        assert "_e2e_file_content_b64" in resp.json()["detail"]


# ════════════════════════════════════════════════════════════════
# 负向测试 - 状态查询
# ════════════════════════════════════════════════════════════════
class TestContractTransactionStatusNegative:
    """GET /internal/contract/transactions/{trace_id} 负向测试。"""

    def test_unknown_trace_id_returns_404(self):
        """未知 trace_id 应返回 404。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.get(
            "/internal/contract/transactions/unknown-trace-id",
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert resp.status_code == 404

    def test_missing_token_returns_401(self):
        """GET 缺失 X-Contract-Token 应返回 401。"""
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.get("/internal/contract/transactions/any-trace")
        assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════
# 负向测试 - 失败路径
# ════════════════════════════════════════════════════════════════
class TestContractDispatchFailure:
    """dispatcher 抛异常时交易状态应为 failed。"""

    def test_dispatcher_failure_marks_transaction_failed(self):
        """dispatcher 抛异常时状态应为 failed,error 不为空。"""
        app, _, _ = _create_test_app(dispatcher_fail=True)
        client = TestClient(app)

        trace_id = "trace-fail-001"
        payload = _make_valid_payload(trace_id=trace_id)
        submit_resp = client.post(
            "/internal/contract/update",
            json=payload,
            headers={"X-Contract-Token": "ci-test-contract-token"},
        )
        assert submit_resp.status_code == 200

        # 轮询直到终态
        for _ in range(50):
            status_resp = client.get(
                f"/internal/contract/transactions/{trace_id}",
                headers={"X-Contract-Token": "ci-test-contract-token"},
            )
            if status_resp.json()["status"] in ("delivered", "failed"):
                break

        data = status_resp.json()
        assert data["status"] == "failed", (
            f"dispatcher 失败时状态应为 failed,实际为 {data['status']}"
        )
        assert data["error"] is not None
        assert "RuntimeError" in data["error"]


# ════════════════════════════════════════════════════════════════
# 安全边界测试 - production 拒绝
# ════════════════════════════════════════════════════════════════
class TestCreateContractAppSecurityBoundary:
    """create_contract_app() 安全边界测试。

    整改依据(R76 终审报告 10.O-O5):
        production 启动时若路由被注册则直接失败。
    """

    def test_create_contract_app_in_production_raises(self):
        """非 SECRETLESS_MODE 调用 create_contract_app 应抛 AppError。"""
        _set_secretless_mode(False)
        try:
            from services.sink_adapters.web_adapter import create_contract_app
            with pytest.raises(AppError):
                create_contract_app(
                    contract_token="ci-test-token",
                    public_dispatcher=_make_mock_dispatcher(),
                    bot=_make_mock_bot(),
                )
        finally:
            _set_secretless_mode(True)

    def test_create_contract_app_with_empty_token_raises(self):
        """空 contract_token 调用 create_contract_app 应抛 AppError。"""
        _ensure_secretless_mode_true()
        from services.sink_adapters.web_adapter import create_contract_app
        with pytest.raises(AppError):
            create_contract_app(
                contract_token="",
                public_dispatcher=_make_mock_dispatcher(),
                bot=_make_mock_bot(),
            )


# ════════════════════════════════════════════════════════════════
# 负向验收:grep 检查禁止字符串
# ════════════════════════════════════════════════════════════════
class TestNegativeAcceptanceGrep:
    """R76 报告 10.B 负向验收:web_adapter.py 不允许出现禁止字符串。

    整改依据(R76 终审报告 10.B / 10.O-O4 / P0-03):
        - ``_dispatch_media`` 不应作为测试驱动入口(本模块只通过参数注入);
        - ``_e2e_file_content_b64`` 不应出现在生产代码;
        - ``bot=None`` 不应作为合法路径;
        - ``print('OK')`` 不应作为成功标准。
    """

    def test_web_adapter_no_e2e_file_content_b64_string(self):
        """web_adapter.py 不应包含 ``_e2e_file_content_b64`` 字符串。

        注:允许在 ``_FORBIDDEN_CONTRACT_KEYS`` 集合中以字符串字面量形式存在
        (用于禁止该 key);但不允许作为业务逻辑使用。
        """
        web_adapter_path = (
            Path(__file__).resolve().parent.parent.parent
            / "services" / "sink_adapters" / "web_adapter.py"
        )
        content = web_adapter_path.read_text(encoding="utf-8")
        # 统计出现次数:允许在 _FORBIDDEN_CONTRACT_KEYS 中出现一次
        count = content.count("_e2e_file_content_b64")
        assert count <= 2, (
            f"_e2e_file_content_b64 在 web_adapter.py 中出现 {count} 次,"
            f"应仅在 _FORBIDDEN_CONTRACT_KEYS 和注释中出现(最多 2 次)"
        )

    def test_web_adapter_no_print_ok_as_success(self):
        """web_adapter.py 不应使用 ``print('OK')`` 作为成功标准。"""
        web_adapter_path = (
            Path(__file__).resolve().parent.parent.parent
            / "services" / "sink_adapters" / "web_adapter.py"
        )
        content = web_adapter_path.read_text(encoding="utf-8")
        # 禁止 print("OK") 或 print('OK') 作为成功标准
        assert "print('OK')" not in content, (
            "web_adapter.py 不应使用 print('OK') 作为成功标准"
        )
        assert 'print("OK")' not in content, (
            "web_adapter.py 不应使用 print(\"OK\") 作为成功标准"
        )
