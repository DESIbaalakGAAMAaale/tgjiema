"""R47 P1-c: ErrorCodes 协议化测试。

测试覆盖:
1. ErrorDefinition 注册 — 所有 ErrorCodes 常量都已在 ErrorRegistry 注册
2. ErrorEnvelope 生成 — create_envelope 返回完整字段且 message 已 i18n 渲染
3. trace_id 贯穿 — 自动生成 UUID,可通过参数注入,envelope.trace_id 与传入一致
4. 未注册 code fallback — 未知 code 自动 fallback 到 ERROR_INTERNAL
5. safe_params 过滤 — 未列入 safe_params 白名单的字段不进入 envelope.params
6. AppError 异常 — 继承 Exception,携带 trace_id / code / envelope
7. AppError.write_audit_log — 写入 audit_log 表(details 含 trace_id)
8. i18n 多语言 — zh-CN / en-US 都能正确渲染
9. ErrorCodes 常量向后兼容 — 原有常量值不变
10. CI 脚本可执行 — check_error_codes.py / verify_i18n_keys.py 能正常退出

测试策略:
- 纯单元测试(不需要数据库,除 write_audit_log 用例外)
- ErrorRegistry 用例间隔离(reset fixture)
- 真实 SQLite 临时数据库(用于 write_audit_log 验证)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# 测试环境兼容(mock telegram 库)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# Fixture: ErrorRegistry 重置(用例间隔离)
# ════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def reset_error_registry():
    """每个用例前重置 ErrorRegistry(避免用例间污染)。

    注意:reset 后下一次 get/create_envelope 调用会重新触发 _register_defaults,
    所以用例无需手动重新注册默认定义。
    """
    from services.error_codes import ErrorRegistry
    ErrorRegistry.reset()
    yield
    ErrorRegistry.reset()


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(用于 write_audit_log 验证)
# ════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def cache_store_with_audit_log():
    """创建带 audit_log 表的临时 SQLite cache_store。"""
    from database import cache_store as cs_module

    # 检查 CacheStore 是否可用
    import inspect
    if not inspect.isclass(cs_module.CacheStore):
        pytest.skip("database.cache_store.CacheStore 不可用")

    tmpdir = tempfile.mkdtemp(prefix="r47_p1_c_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = cs_module.DB_PATH
    original_store = getattr(cs_module, "_store", None)
    cs_module.DB_PATH = db_path
    try:
        s = cs_module.CacheStore()
        await s.init()
        cs_module._store = s
        yield s
        await s.close()
    finally:
        cs_module.DB_PATH = original_path
        if original_store is not None:
            cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 1. ErrorDefinition 注册测试
# ════════════════════════════════════════════════════════════════
class TestErrorDefinitionRegistration:
    """验证所有 ErrorCodes 常量都已在 ErrorRegistry 注册。"""

    def test_all_error_codes_registered(self):
        """所有 ErrorCodes 类常量都必须在 ErrorRegistry 中注册。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # 收集所有 ErrorCodes 类常量(大写字母+下划线)
        code_attrs = [
            attr for attr in dir(ErrorCodes)
            if not attr.startswith("_") and attr.isupper()
        ]
        assert len(code_attrs) >= 18, (
            f"ErrorCodes 应至少有 18 个常量,实际 {len(code_attrs)}"
        )

        unregistered = []
        for attr in code_attrs:
            code = getattr(ErrorCodes, attr)
            if not ErrorRegistry.is_registered(code):
                unregistered.append(f"{attr}={code}")
        assert not unregistered, (
            f"以下 ErrorCodes 未在 ErrorRegistry 注册: {unregistered}"
        )

    def test_required_error_codes_present(self):
        """R47 P1-c 要求的 6 个原始 ErrorCodes 必须存在且已注册。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        required_codes = [
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            ErrorCodes.INDEX_FINALIZE_OUTBOX_FAILED,
            ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
            ErrorCodes.AUTH_MFA_REPLAYED,
            ErrorCodes.BACKUP_RESTORE_APPROVAL_INVALID,
            ErrorCodes.EFFECT_RECEIPT_MANAGER_UNAVAILABLE,
        ]
        for code in required_codes:
            assert ErrorRegistry.is_registered(code), (
                f"必需的 ErrorCode 未注册: {code}"
            )

    def test_error_internal_registered_as_fallback(self):
        """ERROR_INTERNAL 必须注册(作为未注册 code 的 fallback)。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        assert ErrorRegistry.is_registered(ErrorCodes.ERROR_INTERNAL), (
            "ERROR_INTERNAL 必须注册(作为 fallback)"
        )

    def test_definition_fields_complete(self):
        """每个 ErrorDefinition 应包含完整字段(code/message_key/http_status/retryable/severity/safe_params)。"""
        from services.error_codes import ErrorRegistry

        all_codes = ErrorRegistry.all_codes()
        for code in all_codes:
            definition = ErrorRegistry.get(code)
            assert definition.code == code
            assert isinstance(definition.message_key, str) and definition.message_key
            assert isinstance(definition.http_status, int) and 100 <= definition.http_status <= 599
            assert isinstance(definition.retryable, bool)
            assert definition.severity in ("info", "warning", "error", "critical"), (
                f"非法 severity {definition.severity} for code {code}"
            )
            assert isinstance(definition.safe_params, list)


# ════════════════════════════════════════════════════════════════
# 2. ErrorEnvelope 生成测试
# ════════════════════════════════════════════════════════════════
class TestErrorEnvelopeCreation:
    """验证 ErrorEnvelope 生成。"""

    def test_create_envelope_basic_fields(self):
        """create_envelope 返回的 envelope 包含所有必需字段。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "abc123"},
        )

        # 必需字段
        assert envelope.code == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        assert isinstance(envelope.message, str) and envelope.message
        assert isinstance(envelope.message_key, str) and envelope.message_key
        assert isinstance(envelope.trace_id, str) and envelope.trace_id
        assert isinstance(envelope.retryable, bool)
        assert isinstance(envelope.severity, str)
        assert isinstance(envelope.params, dict)
        assert isinstance(envelope.timestamp, str) and envelope.timestamp

    def test_create_envelope_message_rendered_with_params(self):
        """message 应使用 params 中的值渲染占位符。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILEXYZ"},
        )
        # message 中应包含 file_code 值
        assert "FILEXYZ" in envelope.message, (
            f"message 应包含 file_code 渲染值,实际: {envelope.message}"
        )

    def test_create_envelope_to_dict_serializable(self):
        """to_dict 返回的 dict 可 JSON 序列化。"""
        import json
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
            params={"wait_seconds": 30, "user_id": 123},
        )
        d = envelope.to_dict()
        # 必须可 JSON 序列化
        serialized = json.dumps(d, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized["code"] == ErrorCodes.DELIVERY_SEND_FLOOD_WAIT
        assert deserialized["trace_id"] == envelope.trace_id

    def test_create_envelope_zh_and_en_locales(self):
        """zh-CN 和 en-US 都能正确渲染 message。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        zh_envelope = ErrorRegistry.create_envelope(
            ErrorCodes.FILE_NOT_FOUND,
            params={"file_code": "TEST123"},
            locale="zh-CN",
        )
        en_envelope = ErrorRegistry.create_envelope(
            ErrorCodes.FILE_NOT_FOUND,
            params={"file_code": "TEST123"},
            locale="en-US",
        )
        # 两个 locale 的 message 应不同(中文 vs 英文)
        assert zh_envelope.message != en_envelope.message, (
            f"zh-CN 和 en-US 的 message 不应相同: "
            f"zh={zh_envelope.message}, en={en_envelope.message}"
        )
        # 都应包含 file_code 渲染值
        assert "TEST123" in zh_envelope.message
        assert "TEST123" in en_envelope.message


# ════════════════════════════════════════════════════════════════
# 3. trace_id 贯穿测试
# ════════════════════════════════════════════════════════════════
class TestTraceIdPropagation:
    """验证 trace_id 自动生成 + 可注入 + 贯穿 envelope。"""

    def test_trace_id_auto_generated(self):
        """不传 trace_id 时自动生成 UUID。"""
        from services.error_codes import ErrorRegistry, ErrorCodes

        envelope = ErrorRegistry.create_envelope(ErrorCodes.ERROR_INTERNAL)
        # 应是合法 UUID
        parsed = uuid.UUID(envelope.trace_id)
        assert str(parsed) == envelope.trace_id

    def test_trace_id_injected(self):
        """传入 trace_id 时,envelope.trace_id 与传入值一致。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        custom_trace_id = str(uuid.uuid4())
        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.ERROR_INTERNAL,
            trace_id=custom_trace_id,
        )
        assert envelope.trace_id == custom_trace_id

    def test_trace_id_unique_per_call(self):
        """每次调用生成不同的 trace_id。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        e1 = ErrorRegistry.create_envelope(ErrorCodes.ERROR_INTERNAL)
        e2 = ErrorRegistry.create_envelope(ErrorCodes.ERROR_INTERNAL)
        assert e1.trace_id != e2.trace_id

    def test_trace_id_in_message_template(self):
        """ERROR_INTERNAL 的 message 模板应包含 {trace_id} 占位符并被渲染。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.ERROR_INTERNAL,
            trace_id="test-trace-id-12345",
        )
        # message 应包含 trace_id 值
        assert "test-trace-id-12345" in envelope.message, (
            f"message 应包含 trace_id 渲染值,实际: {envelope.message}"
        )
        # 但 params 不应包含 trace_id(仅用于 message 渲染)
        assert "trace_id" not in envelope.params


# ════════════════════════════════════════════════════════════════
# 4. 未注册 code fallback 测试
# ════════════════════════════════════════════════════════════════
class TestUnregisteredCodeFallback:
    """验证未注册 code 时 fallback 到 ERROR_INTERNAL。"""

    def test_unknown_code_falls_back_to_error_internal(self):
        """未注册的 code 应 fallback 到 ERROR_INTERNAL。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        unknown_code = "UNKNOWN.CODE.NOT_REGISTERED"
        envelope = ErrorRegistry.create_envelope(unknown_code)
        assert envelope.code == ErrorCodes.ERROR_INTERNAL, (
            f"未注册 code 应 fallback 到 ERROR_INTERNAL,实际: {envelope.code}"
        )

    def test_get_unknown_code_returns_error_internal_definition(self):
        """ErrorRegistry.get 对未注册 code 返回 ERROR_INTERNAL 定义。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        definition = ErrorRegistry.get("NOT_REGISTERED_CODE")
        assert definition.code == ErrorCodes.ERROR_INTERNAL

    def test_is_registered_does_not_trigger_fallback(self):
        """is_registered 对未注册 code 返回 False(不触发 fallback)。"""
        from services.error_codes import ErrorRegistry

        assert ErrorRegistry.is_registered("NOT_REGISTERED_CODE") is False
        # 已注册 code 返回 True
        from services.error_codes import ErrorCodes
        assert ErrorRegistry.is_registered(ErrorCodes.ERROR_INTERNAL) is True


# ════════════════════════════════════════════════════════════════
# 5. safe_params 过滤测试
# ════════════════════════════════════════════════════════════════
class TestSafeParamsFiltering:
    """验证 safe_params 白名单过滤敏感字段。"""

    def test_safe_params_keeps_whitelisted_fields(self):
        """列入 safe_params 的字段应保留在 envelope.params 中。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # UPLOAD_COPY_TELEGRAM_TIMEOUT 的 safe_params = ["file_code", "channel_id"]
        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={
                "file_code": "FILE123",      # 在白名单
                "channel_id": -100123,        # 在白名单
            },
        )
        assert envelope.params == {"file_code": "FILE123", "channel_id": -100123}

    def test_safe_params_filters_non_whitelisted_fields(self):
        """未列入 safe_params 的字段不应出现在 envelope.params 中。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={
                "file_code": "FILE123",          # 在白名单
                "secret_token": "sensitive",     # 不在白名单(应被过滤)
                "password": "should_be_filtered", # 不在白名单(应被过滤)
            },
        )
        assert "secret_token" not in envelope.params
        assert "password" not in envelope.params
        assert envelope.params == {"file_code": "FILE123"}

    def test_safe_params_empty_when_no_whitelist(self):
        """safe_params 为空时,envelope.params 应为空 dict。"""
        from services.error_codes import ErrorDefinition, ErrorCodes, ErrorRegistry

        # 临时注册一个 safe_params 为空的定义
        custom_code = "TEST.NO.SAFE_PARAMS"
        ErrorRegistry.register(ErrorDefinition(
            code=custom_code,
            message_key="errors.error.internal.unexpected",
            http_status=500,
            retryable=False,
            severity="info",
            safe_params=[],
        ))
        envelope = ErrorRegistry.create_envelope(
            custom_code,
            params={"secret": "should_be_filtered"},
        )
        assert envelope.params == {}

    def test_safe_params_empty_when_no_params_passed(self):
        """不传 params 时,envelope.params 应为空 dict。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
        )
        assert envelope.params == {}


# ════════════════════════════════════════════════════════════════
# 6. AppError 异常测试
# ════════════════════════════════════════════════════════════════
class TestAppError:
    """验证 AppError 异常类行为。"""

    def test_app_error_inherits_exception(self):
        """AppError 应继承 Exception。"""
        from services.error_codes import AppError, ErrorCodes

        err = AppError(ErrorCodes.ERROR_INTERNAL)
        assert isinstance(err, Exception)

    def test_app_error_carries_trace_id(self):
        """AppError 应携带 trace_id(自动生成)。"""
        from services.error_codes import AppError, ErrorCodes

        err = AppError(ErrorCodes.ERROR_INTERNAL)
        assert hasattr(err, "trace_id")
        assert err.trace_id
        # 应是合法 UUID
        uuid.UUID(err.trace_id)

    def test_app_error_trace_id_injected(self):
        """AppError 应支持注入 trace_id。"""
        from services.error_codes import AppError, ErrorCodes

        custom_id = "custom-trace-id-abc"
        err = AppError(ErrorCodes.ERROR_INTERNAL, trace_id=custom_id)
        assert err.trace_id == custom_id

    def test_app_error_carries_code_and_envelope(self):
        """AppError 应携带 code 和 envelope。"""
        from services.error_codes import AppError, ErrorCodes, ErrorEnvelope

        err = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE123"},
        )
        assert err.code == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        assert isinstance(err.envelope, ErrorEnvelope)
        assert err.envelope.code == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        # trace_id 一致
        assert err.trace_id == err.envelope.trace_id

    def test_app_error_to_dict(self):
        """AppError.to_dict 应返回 R55 §17 精简响应格式。

        R55 §17: to_dict 返回 ``{code, message_key, trace_id, retryable,
        severity, safe_params}``,不含 message/timestamp(原始异常不暴露)。
        如需完整 envelope(含 i18n message),应直接访问 ``self.envelope``。
        """
        from services.error_codes import AppError, ErrorCodes

        err = AppError(ErrorCodes.ERROR_INTERNAL)
        d = err.to_dict()
        # R55 §17 精简响应格式的必需字段
        assert d["code"] == ErrorCodes.ERROR_INTERNAL
        assert d["message_key"] == err.envelope.message_key
        assert d["trace_id"] == err.envelope.trace_id
        assert d["retryable"] == err.envelope.retryable
        assert d["severity"] == err.envelope.severity
        # safe_params 应存在且为 dict(经过 is_safe_param 过滤)
        assert isinstance(d["safe_params"], dict)
        # 精简格式不应包含 message/params/timestamp(避免泄露内部细节)
        assert "message" not in d
        assert "params" not in d
        assert "timestamp" not in d

    def test_app_error_can_be_raised_and_caught(self):
        """AppError 可被 raise 并被 except 捕获。"""
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            raise AppError(
                ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
                params={"file_code": "FILE123"},
            )
        # 异常消息应是 i18n message
        assert "FILE123" in str(exc_info.value)

    def test_app_error_cause_chain(self):
        """AppError 应支持 cause 参数(异常链)。"""
        from services.error_codes import AppError, ErrorCodes

        original = ValueError("original cause")
        err = AppError(ErrorCodes.ERROR_INTERNAL, cause=original)
        # cause 信息不直接挂在 __cause__ 上(避免 Python 异常链混淆)
        # 但应可通过 _cause 访问
        assert err._cause is original

    def test_app_error_params_filtered(self):
        """AppError 的 params 应按 safe_params 过滤。"""
        from services.error_codes import AppError, ErrorCodes

        err = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={
                "file_code": "FILE123",
                "secret_token": "should_be_filtered",
            },
        )
        assert err.params == {"file_code": "FILE123"}
        assert "secret_token" not in err.params


# ════════════════════════════════════════════════════════════════
# 7. AppError.write_audit_log 测试
# ════════════════════════════════════════════════════════════════
class TestAppErrorWriteAuditLog:
    """验证 AppError.write_audit_log 写入 audit_log 表。"""

    @pytest.mark.asyncio
    async def test_write_audit_log_success(self, cache_store_with_audit_log):
        """成功写入 audit_log 表。"""
        from services.error_codes import AppError, ErrorCodes

        err = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE_AUDIT"},
        )
        ok = await err.write_audit_log()
        assert ok is True

        # 验证 audit_log 表中有对应记录
        store = cache_store_with_audit_log
        cursor = await store._db.execute(
            "SELECT action, target_id, details FROM audit_log "
            "WHERE target_id = ?",
            (err.trace_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1, f"应写入 1 条 audit_log,实际 {len(rows)}"

        action, target_id, details_json = rows[0]
        assert action == f"app_error:{ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT}"
        assert target_id == err.trace_id

        # details 应包含 trace_id / code / params / severity
        import json
        details = json.loads(details_json)
        assert details["trace_id"] == err.trace_id
        assert details["code"] == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        assert details["params"] == {"file_code": "FILE_AUDIT"}
        assert "severity" in details
        assert "retryable" in details

    @pytest.mark.asyncio
    async def test_write_audit_log_returns_false_when_db_unavailable(self):
        """DB 不可用时 write_audit_log 返回 False(不抛异常)。"""
        from services.error_codes import AppError, ErrorCodes
        from database import cache_store as cs_module

        # 临时清空 _store 模拟 DB 不可用
        original_store = getattr(cs_module, "_store", None)
        cs_module._store = None
        try:
            err = AppError(ErrorCodes.ERROR_INTERNAL)
            ok = await err.write_audit_log()
            assert ok is False
        finally:
            cs_module._store = original_store


# ════════════════════════════════════════════════════════════════
# 8. ErrorCodes 常量向后兼容测试
# ════════════════════════════════════════════════════════════════
class TestErrorCodesBackwardCompatibility:
    """验证原有 ErrorCodes 常量值未变(向后兼容)。"""

    def test_original_six_error_codes_unchanged(self):
        """R47 P1-c 要求的 6 个原始 ErrorCodes 值未变。"""
        from services.error_codes import ErrorCodes

        assert ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT == "UPLOAD.COPY.TELEGRAM_TIMEOUT"
        assert ErrorCodes.INDEX_FINALIZE_OUTBOX_FAILED == "INDEX.FINALIZE.OUTBOX_FAILED"
        assert ErrorCodes.DELIVERY_SEND_FLOOD_WAIT == "DELIVERY.SEND.FLOOD_WAIT"
        assert ErrorCodes.AUTH_MFA_REPLAYED == "AUTH.MFA.REPLAYED"
        assert ErrorCodes.BACKUP_RESTORE_APPROVAL_INVALID == "BACKUP.RESTORE.APPROVAL_INVALID"
        assert ErrorCodes.EFFECT_RECEIPT_MANAGER_UNAVAILABLE == "EFFECT.RECEIPT.MANAGER_UNAVAILABLE"

    def test_all_error_codes_three_segment_format(self):
        """所有 ErrorCodes 应为三段式 DOMAIN.OPERATION.REASON 格式。"""
        from services.error_codes import ErrorCodes

        code_attrs = [
            attr for attr in dir(ErrorCodes)
            if not attr.startswith("_") and attr.isupper()
        ]
        for attr in code_attrs:
            code = getattr(ErrorCodes, attr)
            parts = code.split(".")
            assert len(parts) == 3, (
                f"ErrorCode {attr}={code} 应为三段式格式(实际 {len(parts)} 段)"
            )
            for part in parts:
                assert part, f"ErrorCode {attr}={code} 含空段"


# ════════════════════════════════════════════════════════════════
# 9. ErrorRegistry 辅助方法测试
# ════════════════════════════════════════════════════════════════
class TestErrorRegistryHelpers:
    """验证 ErrorRegistry 的辅助方法。"""

    def test_all_codes_sorted(self):
        """all_codes 返回排序后的列表。"""
        from services.error_codes import ErrorRegistry

        codes = ErrorRegistry.all_codes()
        assert codes == sorted(codes)

    def test_all_message_keys_sorted_deduplicated(self):
        """all_message_keys 返回去重 + 排序后的列表。"""
        from services.error_codes import ErrorRegistry

        keys = ErrorRegistry.all_message_keys()
        assert keys == sorted(keys)
        assert len(keys) == len(set(keys)), "message_keys 应去重"

    def test_register_custom_definition(self):
        """可注册自定义 ErrorDefinition(覆盖已有或新增)。"""
        from services.error_codes import ErrorDefinition, ErrorCodes, ErrorRegistry

        # 注册自定义定义
        custom_code = "CUSTOM.TEST.CASE"
        ErrorRegistry.register(ErrorDefinition(
            code=custom_code,
            message_key="errors.error.internal.unexpected",
            http_status=418,
            retryable=False,
            severity="info",
            safe_params=["custom_field"],
        ))
        assert ErrorRegistry.is_registered(custom_code)
        definition = ErrorRegistry.get(custom_code)
        assert definition.http_status == 418

    def test_reset_clears_registry(self):
        """reset 清空注册表(后续调用会重新触发 _register_defaults)。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # reset 后 _initialized=False,但下一次 get 会重新注册
        ErrorRegistry.reset()
        assert ErrorRegistry._initialized is False
        # 触发重新初始化
        definition = ErrorRegistry.get(ErrorCodes.ERROR_INTERNAL)
        assert definition.code == ErrorCodes.ERROR_INTERNAL
        assert ErrorRegistry._initialized is True


# ════════════════════════════════════════════════════════════════
# 10. CI 脚本可执行性测试
# ════════════════════════════════════════════════════════════════
class TestCIScriptsExecutable:
    """验证 CI 脚本能正常执行(不抛异常,exit code 合理)。"""

    def test_check_error_codes_script_default_mode(self):
        """check_error_codes.py 默认宽松模式应 exit 0(仅警告,不阻断)。"""
        import subprocess

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "check_error_codes.py"
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # 默认宽松模式应 exit 0
        assert result.returncode == 0, (
            f"check_error_codes.py 默认模式应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_verify_i18n_keys_script_passes(self):
        """verify_i18n_keys.py 应 exit 0(所有 message_key 都在 locale 文件中)。"""
        import subprocess

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "verify_i18n_keys.py"
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"verify_i18n_keys.py 应 exit 0,实际 {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ════════════════════════════════════════════════════════════════
# 11. 完整链路集成测试
# ════════════════════════════════════════════════════════════════
class TestEndToEndIntegration:
    """端到端集成测试 — 模拟实际错误返回链路。"""

    def test_full_flow_raise_app_error_and_serialize(self):
        """完整流程:raise AppError → except → to_dict → JSON 序列化。

        R55 §17: to_dict 返回精简格式(无 message/params/timestamp),
        message 渲染通过 err.envelope.message 访问,params 过滤后放入 safe_params。
        """
        import json
        from services.error_codes import AppError, ErrorCodes

        # 模拟业务代码抛出 AppError
        try:
            raise AppError(
                ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
                params={"wait_seconds": 60, "user_id": 999, "secret": "leak"},
                trace_id="trace-e2e-001",
            )
        except AppError as err:
            # 模拟 HTTP/Bot 响应序列化
            response_dict = err.to_dict()
            serialized = json.dumps(response_dict, ensure_ascii=False)
            deserialized = json.loads(serialized)

            # R55 §17 精简格式字段验证
            assert deserialized["code"] == ErrorCodes.DELIVERY_SEND_FLOOD_WAIT
            assert deserialized["trace_id"] == "trace-e2e-001"
            assert deserialized["retryable"] is True
            assert deserialized["severity"] == "warning"  # DELIVERY_SEND_FLOOD_WAIT severity
            assert "message_key" in deserialized
            assert isinstance(deserialized["safe_params"], dict)
            # R55 §17: 精简格式不应包含 message/params/timestamp
            assert "message" not in deserialized
            assert "params" not in deserialized
            assert "timestamp" not in deserialized
            # secret 应被 is_safe_param 过滤(key 匹配 _SENSITIVE_KEY_PATTERNS)
            assert "secret" not in deserialized["safe_params"]
            assert "leak" not in serialized
            # wait_seconds 应保留在 safe_params 中(非敏感字段)
            assert deserialized["safe_params"].get("wait_seconds") == 60
            # envelope.message 仍可访问完整 i18n 渲染
            assert "60" in err.envelope.message

    @pytest.mark.asyncio
    async def test_full_flow_with_audit_log(self, cache_store_with_audit_log):
        """完整流程:raise AppError → except → write_audit_log → 查询验证。"""
        import json
        from services.error_codes import AppError, ErrorCodes

        # 模拟业务代码抛出 AppError
        try:
            raise AppError(
                ErrorCodes.AUTH_MFA_REPLAYED,
                params={"user_id": 12345},
                trace_id="trace-audit-002",
            )
        except AppError as err:
            # 写入 audit_log
            ok = await err.write_audit_log()
            assert ok is True

        # 查询 audit_log 表
        store = cache_store_with_audit_log
        cursor = await store._db.execute(
            "SELECT action, target_id, details FROM audit_log "
            "WHERE target_id = ?",
            ("trace-audit-002",),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

        action, target_id, details_json = rows[0]
        assert action == f"app_error:{ErrorCodes.AUTH_MFA_REPLAYED}"
        assert target_id == "trace-audit-002"

        details = json.loads(details_json)
        assert details["trace_id"] == "trace-audit-002"
        assert details["code"] == ErrorCodes.AUTH_MFA_REPLAYED
        assert details["severity"] == "critical"
        assert details["params"] == {"user_id": 12345}
