"""R53 P0-2: CommandBus claim_execution_approved fail-closed 测试。

背景:
    原 ``claim_execution_approved`` 在 ``store._db`` 不可用时记录"降级执行"并返回 True,
    导致 Repair/Maintenance/Restore/Entitlements 在 DB 故障时可能在没有审批状态下
    执行高风险动作,严重违反 fail-closed 原则。

R53 P0-2 整改:
    - DB 未初始化 / 访问异常 / commit 失败时,**必须**抛 ``AppError``
      (``COMMAND_EXECUTION_STORE_UNAVAILABLE``),禁止降级执行。
    - 调用方必须传播异常或返回失败,**不得**捕获后继续执行。
    - CAS rowcount=0 仍返回 False(非异常路径,表示已被抢占或状态非 approved)。

测试覆盖:
    1. DB=None 时 claim_execution_approved 抛 AppError
    2. DB 连接异常(execute_fetchall 抛 Exception)时同上
    3. DB commit 失败(commit 抛 Exception)时同上
    4. CAS rowcount=0 时返回 False(非异常)
    5. 副作用函数(MagicMock)从未被调用(call_count == 0)

测试策略:
    - 使用 ``unittest.mock.patch`` mock ``database.cache_store.get_cache_store``
      返回的 store,以模拟 DB 不可用 / 访问异常 / commit 失败等场景。
    - 不依赖真实 SQLite,纯单元测试(快速隔离)。
    - 中文注释,英文 raise 消息。
"""
from __future__ import annotations

import inspect
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module  # noqa: E402

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )


# ════════════════════════════════════════════════════════════════
# 辅助函数:构造 mock store / mock db
# ════════════════════════════════════════════════════════════════

def _make_store_with_no_db() -> MagicMock:
    """构造 _db=None 的 mock store(模拟 DB 未初始化)。"""
    store = MagicMock()
    store._db = None
    return store


def _make_store_with_db_error(
    *,
    fetchall_raises: BaseException | None = None,
    execute_raises: BaseException | None = None,
    commit_raises: BaseException | None = None,
    rowcount: int = 1,
) -> MagicMock:
    """构造 _db 可用但访问/commit 异常的 mock store。

    Args:
        fetchall_raises: 若非 None,execute_fetchall 抛此异常
        execute_raises: 若非 None,execute 抛此异常
        commit_raises: 若非 None,commit 抛此异常
        rowcount: execute 返回 cursor 的 rowcount(默认 1)
    """
    store = MagicMock()
    db = MagicMock()
    store._db = db

    # execute_fetchall 默认返回空列表(不影响主路径)
    if fetchall_raises is not None:
        db.execute_fetchall = AsyncMock(side_effect=fetchall_raises)
    else:
        db.execute_fetchall = AsyncMock(return_value=[])

    # execute 返回 cursor(含 rowcount)
    cursor = MagicMock()
    cursor.rowcount = rowcount
    if execute_raises is not None:
        db.execute = AsyncMock(side_effect=execute_raises)
    else:
        db.execute = AsyncMock(return_value=cursor)

    # commit 默认正常,可选抛异常
    if commit_raises is not None:
        db.commit = AsyncMock(side_effect=commit_raises)
    else:
        db.commit = AsyncMock(return_value=None)

    return store


# ════════════════════════════════════════════════════════════════
# 1. claim_execution_approved fail-closed 单元测试
# ════════════════════════════════════════════════════════════════

class TestClaimExecutionApprovedFailClosed:
    """R53 P0-2: DB 不可用时 claim_execution_approved 必须 fail-closed。"""

    @pytest.mark.asyncio
    async def test_raises_when_db_is_none(self):
        """DB=None 时必须抛 AppError(COMMAND_EXECUTION_STORE_UNAVAILABLE)。

        旧逻辑:记录"降级执行"并返回 True(fail-open,严重安全问题)。
        新逻辑:抛 AppError(fail-closed),禁止执行高风险动作。
        """
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError, ErrorCodes

        mock_store = _make_store_with_no_db()
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            with pytest.raises(AppError) as exc_info:
                await claim_execution_approved(
                    action_id="r53_p0_2_db_none",
                    owner="test_worker",
                )

        # 校验错误码为 COMMAND_EXECUTION_STORE_UNAVAILABLE
        assert exc_info.value.code == ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE, (
            f"DB=None 时应抛 COMMAND_EXECUTION_STORE_UNAVAILABLE,"
            f"实际: {exc_info.value.code}"
        )
        # 校验 safe_params 包含 action_id / reason
        assert exc_info.value.params.get("action_id") == "r53_p0_2_db_none"
        assert exc_info.value.params.get("reason") == "db_not_initialized"

    @pytest.mark.asyncio
    async def test_raises_when_db_connection_error(self):
        """DB 连接异常(execute_fetchall 抛 sqlite3.Error)时必须抛 AppError。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError, ErrorCodes

        # 模拟 DB 连接异常(execute_fetchall 抛 Exception)
        mock_store = _make_store_with_db_error(
            fetchall_raises=ConnectionError("simulated db connection lost"),
        )
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            with pytest.raises(AppError) as exc_info:
                await claim_execution_approved(
                    action_id="r53_p0_2_conn_err",
                    owner="test_worker",
                    request_hash="hash_001",
                )

        # 校验错误码为 COMMAND_EXECUTION_STORE_UNAVAILABLE
        assert exc_info.value.code == ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE, (
            f"DB 连接异常时应抛 COMMAND_EXECUTION_STORE_UNAVAILABLE,"
            f"实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_raises_when_execute_update_error(self):
        """DB UPDATE 异常(execute 抛 Exception)时必须抛 AppError。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError, ErrorCodes

        # 模拟 DB UPDATE 异常(execute 抛 Exception,跳过 request_hash 校验路径)
        mock_store = _make_store_with_db_error(
            execute_raises=RuntimeError("simulated UPDATE failure"),
        )
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            with pytest.raises(AppError) as exc_info:
                await claim_execution_approved(
                    action_id="r53_p0_2_update_err",
                    owner="test_worker",
                )

        assert exc_info.value.code == ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE, (
            f"DB UPDATE 异常时应抛 COMMAND_EXECUTION_STORE_UNAVAILABLE,"
            f"实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_raises_when_commit_fails(self):
        """DB commit 失败时必须抛 AppError(fail-closed,禁止降级执行)。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError, ErrorCodes

        # 模拟 commit 抛异常(UPDATE 成功但 commit 失败,事务未持久化)
        mock_store = _make_store_with_db_error(
            commit_raises=RuntimeError("simulated commit failure"),
            rowcount=1,
        )
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            with pytest.raises(AppError) as exc_info:
                await claim_execution_approved(
                    action_id="r53_p0_2_commit_err",
                    owner="test_worker",
                )

        assert exc_info.value.code == ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE, (
            f"DB commit 失败时应抛 COMMAND_EXECUTION_STORE_UNAVAILABLE,"
            f"实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_rowcount_zero(self):
        """CAS rowcount=0 时返回 False(非异常路径,表示已被抢占或状态非 approved)。

        此场景 DB 可用且访问正常,只是 CAS 未命中,属于正常业务路径,不应抛异常。
        """
        from services.command_bus import claim_execution_approved

        # 模拟 CAS 未命中:execute 成功但 rowcount=0
        mock_store = _make_store_with_db_error(rowcount=0)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await claim_execution_approved(
                action_id="r53_p0_2_rowcount_zero",
                owner="test_worker",
            )

        assert result is False, (
            "rowcount=0 时应返回 False(已被抢占或状态非 approved),不应抛异常"
        )

    @pytest.mark.asyncio
    async def test_returns_true_when_claim_success(self):
        """CAS rowcount=1 时返回 True(认领成功,正常路径)。

        对照测试:确认 fail-closed 整改未破坏正常路径。
        """
        from services.command_bus import claim_execution_approved

        # 模拟 CAS 成功:execute 成功且 rowcount=1
        mock_store = _make_store_with_db_error(rowcount=1)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await claim_execution_approved(
                action_id="r53_p0_2_claim_ok",
                owner="test_worker",
            )

        assert result is True, "rowcount=1 时应返回 True(认领成功)"

    @pytest.mark.asyncio
    async def test_returns_false_when_hash_mismatch(self):
        """request_hash 不匹配时返回 False(防篡改,非异常路径)。"""
        from services.command_bus import claim_execution_approved

        # 模拟 request_hash 不匹配:execute_fetchall 返回存储的 hash 与请求 hash 不一致
        mock_store = MagicMock()
        db = MagicMock()
        mock_store._db = db
        # execute_fetchall 返回存储的 hash
        db.execute_fetchall = AsyncMock(return_value=[("stored_hash_abc",)])
        db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        db.commit = AsyncMock(return_value=None)

        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            result = await claim_execution_approved(
                action_id="r53_p0_2_hash_mismatch",
                owner="test_worker",
                request_hash="tampered_hash_xyz",
            )

        assert result is False, "request_hash 不匹配时应返回 False(防篡改)"
        # 校验:hash 不匹配时未执行 CAS UPDATE(execute 未被调用)
        assert db.execute.call_count == 0, (
            "hash 不匹配时不应执行 CAS UPDATE"
        )


# ════════════════════════════════════════════════════════════════
# 2. 副作用函数从未被调用测试(调用方 fail-closed 验证)
# ════════════════════════════════════════════════════════════════

class TestNoSideEffectsWhenDbUnavailable:
    """R53 P0-2: DB 不可用时调用方不得继续执行副作用函数。

    模拟一个高风险调用流程:
        claimed = await claim_execution_approved(...)
        if not claimed:
            return  # 不执行副作用
        # 副作用函数(如 backup restore / maintenance disable / repair action)
        await side_effect_fn()

    验证:DB 不可用时 claim_execution_approved 抛 AppError,
    副作用函数从未被调用(call_count == 0)。
    """

    @pytest.mark.asyncio
    async def test_side_effect_not_called_when_db_none(self):
        """DB=None 时副作用函数从未被调用。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError

        side_effect_fn = MagicMock()
        side_effect_fn_async = AsyncMock()
        side_effect_fn_async.mock = side_effect_fn  # 用于 call_count 验证

        mock_store = _make_store_with_no_db()
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            # 模拟调用方典型模式:claim → 副作用
            try:
                claimed = await claim_execution_approved(
                    action_id="r53_p0_2_side_effect_none",
                    owner="test_worker",
                )
                # 不应执行到这里(DB=None 应抛异常)
                if claimed:
                    await side_effect_fn_async()
            except AppError:
                # 调用方应传播异常(此处模拟传播)
                pass

        # 校验:副作用函数从未被调用
        assert side_effect_fn_async.call_count == 0, (
            "DB=None 时副作用函数不应被调用(fail-closed)"
        )
        assert side_effect_fn.call_count == 0, (
            "DB=None 时副作用函数不应被调用(fail-closed)"
        )

    @pytest.mark.asyncio
    async def test_side_effect_not_called_when_db_connection_error(self):
        """DB 连接异常时副作用函数从未被调用。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError

        side_effect_fn = AsyncMock()

        mock_store = _make_store_with_db_error(
            fetchall_raises=ConnectionError("db connection lost"),
        )
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            try:
                claimed = await claim_execution_approved(
                    action_id="r53_p0_2_side_effect_conn_err",
                    owner="test_worker",
                    request_hash="hash_002",
                )
                if claimed:
                    await side_effect_fn()
            except AppError:
                pass

        assert side_effect_fn.call_count == 0, (
            "DB 连接异常时副作用函数不应被调用(fail-closed)"
        )

    @pytest.mark.asyncio
    async def test_side_effect_not_called_when_commit_fails(self):
        """DB commit 失败时副作用函数从未被调用。"""
        from services.command_bus import claim_execution_approved
        from services.error_codes import AppError

        side_effect_fn = AsyncMock()

        mock_store = _make_store_with_db_error(
            commit_raises=RuntimeError("commit failed"),
            rowcount=1,
        )
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            try:
                claimed = await claim_execution_approved(
                    action_id="r53_p0_2_side_effect_commit_err",
                    owner="test_worker",
                )
                if claimed:
                    await side_effect_fn()
            except AppError:
                pass

        assert side_effect_fn.call_count == 0, (
            "DB commit 失败时副作用函数不应被调用(fail-closed)"
        )

    @pytest.mark.asyncio
    async def test_side_effect_not_called_when_rowcount_zero(self):
        """CAS rowcount=0(返回 False)时副作用函数从未被调用。"""
        from services.command_bus import claim_execution_approved

        side_effect_fn = AsyncMock()

        mock_store = _make_store_with_db_error(rowcount=0)
        with patch("database.cache_store.get_cache_store", return_value=mock_store):
            claimed = await claim_execution_approved(
                action_id="r53_p0_2_side_effect_rowcount_zero",
                owner="test_worker",
            )
            # 调用方典型模式:claimed=False 时不执行副作用
            if claimed:
                await side_effect_fn()

        assert side_effect_fn.call_count == 0, (
            "rowcount=0(返回 False)时副作用函数不应被调用"
        )


# ════════════════════════════════════════════════════════════════
# 3. ErrorCode / i18n 注册校验
# ════════════════════════════════════════════════════════════════

class TestErrorCodeRegistration:
    """R53 P0-2: ErrorCode 常量 + ErrorDefinition 注册 + i18n key 校验。"""

    def test_error_code_constant_exists(self):
        """ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE 常量存在。"""
        from services.error_codes import ErrorCodes

        assert hasattr(ErrorCodes, "COMMAND_EXECUTION_STORE_UNAVAILABLE"), (
            "ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE 必须存在"
        )
        assert ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE == (
            "COMMAND.EXECUTE.STORE_UNAVAILABLE"
        ), "错误码值必须为 COMMAND.EXECUTE.STORE_UNAVAILABLE"

    def test_error_definition_registered(self):
        """ErrorDefinition 已注册(503 / retryable / critical)。"""
        from services.error_codes import ErrorRegistry, ErrorCodes

        assert ErrorRegistry.is_registered(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE
        ), "COMMAND_EXECUTION_STORE_UNAVAILABLE 必须在 ErrorRegistry 中注册"

        definition = ErrorRegistry.get(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
        )
        assert definition.http_status == 503, "HTTP 状态码必须为 503"
        assert definition.retryable is True, "必须为可重试(retryable=True)"
        assert definition.severity == "critical", "严重级别必须为 critical"
        assert definition.message_key == "errors.command.status.store_unavailable", (
            "message_key 必须为 errors.command.status.store_unavailable"
        )

    def test_i18n_key_exists_in_zh_cn(self):
        """zh-CN.json 包含 command.status.store_unavailable key。"""
        import json
        from pathlib import Path

        locale_path = Path(__file__).resolve().parent.parent / "locales" / "zh-CN.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        errors = data.get("errors", {})
        assert "command.status.store_unavailable" in errors, (
            "zh-CN.json errors 中必须包含 command.status.store_unavailable key"
        )

    def test_i18n_key_exists_in_en_us(self):
        """en-US.json 包含 command.status.store_unavailable key。"""
        import json
        from pathlib import Path

        locale_path = Path(__file__).resolve().parent.parent / "locales" / "en-US.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        errors = data.get("errors", {})
        assert "command.status.store_unavailable" in errors, (
            "en-US.json errors 中必须包含 command.status.store_unavailable key"
        )

    def test_app_error_envelope_renders_message(self):
        """AppError 实例化后 envelope.message 可正常渲染(非 message_key 本身)。"""
        from services.error_codes import AppError, ErrorCodes

        err = AppError(
            ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
            params={"action_id": "test_001", "reason": "db_not_initialized"},
        )
        # message 应包含中文/英文文案,而非 message_key 字符串本身
        assert err.message_key == "errors.command.status.store_unavailable"
        assert err.message != "errors.command.status.store_unavailable", (
            "i18n 渲染失败:message 仍是 message_key 本身"
        )
        assert err.retryable is True
        assert err.severity == "critical"
