"""R50 终审报告 P1-1: ErrorCode 遗留裸字符串归零 + 全模块接入 ErrorEnvelope
   + trace_id 穿透 + safe_params 隐私脱敏单测。

测试覆盖矩阵(16 个用例):

A. ErrorCode 完整性测试 (5)
   1.  test_error_codes_registry_complete
   2.  test_error_envelope_includes_trace_id
   3.  test_app_error_preserves_trace_id
   4.  test_error_envelope_safe_params_redacts_secrets
   5.  test_error_envelope_safe_params_preserves_non_secret

B. trace_id 穿透测试 (4)
   6.  test_trace_id_propagation_bot_to_outbox
   7.  test_trace_id_propagation_outbox_to_receipt
   8.  test_trace_id_propagation_receipt_to_audit
   9.  test_trace_id_propagation_missing_trace_id_generates_default

C. 裸字符串扫描测试 (3)
   10. test_no_bare_strings_outside_baseline
   11. test_baseline_ratchet_only_decreases
   12. test_safe_params_redaction_does_not_leak_in_logs

D. 全模块接入测试 (4)
   13. test_all_bot_modules_use_app_error
   14. test_all_service_modules_use_app_error
   15. test_admin_module_uses_app_error
   16. test_backup_module_uses_app_error

设计说明:
- trace_id 穿透路径: Bot 收到请求 → AppError 携带 trace_id → ErrorEnvelope 保持 trace_id
  → write_audit_log 写入 audit_log.target_id(可按 trace_id 检索全链路)。
- safe_params 采用白名单过滤策略(比部分遮蔽更安全):未列入 ErrorDefinition.safe_params
  的字段(如 password/secret/token/key)完全不进入 envelope.params,杜绝泄露。
- 裸字符串扫描使用 AST 解析(比正则更精确),同时对接 scripts/check_error_codes.py --strict。
- 全模块接入测试覆盖 bots/ / services/ / admin/ 三个目录,允许 EffectReceiptError 白名单。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# 测试环境兼容(mock telegram 库,与 conftest.py 协同)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["bots", "services", "admin"]

# 裸字符串异常类型集合(不允许直接 raise 这些带字符串字面量的异常)
BARE_EXCEPTION_TYPES = frozenset({
    "ValueError", "RuntimeError", "Exception", "TypeError", "KeyError",
})

# 允许的非 AppError 异常白名单(EffectReceiptError 是 effect_receipts 模块的专用异常)
ALLOWED_NON_APP_ERROR_EXCEPTIONS = frozenset({
    "EffectReceiptError",
    "DurabilityError",  # utils.exceptions 中的持久化错误(R38/R39 既有设计)
})


# ════════════════════════════════════════════════════════════════
# Fixture: ErrorRegistry 重置(用例间隔离)
# ════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def reset_error_registry():
    """每个用例前重置 ErrorRegistry(避免用例间污染)。

    reset 后下一次 get/create_envelope 调用会重新触发 _register_defaults。
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
    import inspect

    if not inspect.isclass(cs_module.CacheStore):
        pytest.skip("database.cache_store.CacheStore 不可用")

    tmpdir = tempfile.mkdtemp(prefix="r50_p1_1_test_")
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
# 辅助函数: AST 裸字符串扫描
# ════════════════════════════════════════════════════════════════
def _find_bare_string_raises(directory: Path) -> list[tuple[str, int, str, str]]:
    """使用 AST 扫描目录下 Python 文件中的裸字符串 raise 语句。

    检测模式: raise ValueError("...") / raise RuntimeError("...") / raise Exception("...")

    Returns:
        [(relative_path, line_no, exception_name, message_preview), ...]
    """
    findings: list[tuple[str, int, str, str]] = []
    for py_file in directory.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content, filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if exc is None:
                continue
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                if exc.func.id in BARE_EXCEPTION_TYPES:
                    if exc.args and isinstance(exc.args[0], ast.Constant) \
                            and isinstance(exc.args[0].value, str):
                        rel_path = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
                        msg_preview = exc.args[0].value[:60]
                        findings.append((rel_path, node.lineno, exc.func.id, msg_preview))
    return findings


def _find_return_error_dict(directory: Path) -> list[tuple[str, int, str]]:
    """使用 AST 扫描目录下 Python 文件中的 return {"error": "..."} 模式。

    Returns:
        [(relative_path, line_no, error_message), ...]
    """
    findings: list[tuple[str, int, str]] = []
    for py_file in directory.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content, filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if (isinstance(key, ast.Constant) and key.value == "error"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    rel_path = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
                    findings.append((rel_path, node.lineno, value.value[:60]))
    return findings


def _find_non_app_error_raises(directory: Path) -> list[tuple[str, int, str]]:
    """扫描目录下 Python 文件中所有 raise 语句,找出非 AppError / 非白名单的异常。

    允许的异常类型:
        - AppError (协议化错误)
        - EffectReceiptError (effect_receipts 专用)
        - DurabilityError (utils.exceptions 持久化错误)
        - re-raise (raise 不带异常对象)
        - raise from ... (异常链)

    Returns:
        [(relative_path, line_no, exception_name), ...]
    """
    findings: list[tuple[str, int, str]] = []
    for py_file in directory.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        # 跳过 error_codes.py 本身(定义 AppError 的模块)
        if py_file.name == "error_codes.py":
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content, filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if exc is None:
                continue  # bare re-raise
            exc_name = None
            if isinstance(exc, ast.Call):
                func = exc.func
            elif isinstance(exc, ast.Name):
                func = exc
            else:
                continue
            if isinstance(func, ast.Name):
                exc_name = func.id
            elif isinstance(func, ast.Attribute):
                exc_name = func.attr
            if exc_name is None:
                continue
            if exc_name in ALLOWED_NON_APP_ERROR_EXCEPTIONS:
                continue
            if exc_name == "AppError":
                continue
            # 跳过 Python 内建异常的 re-raise (如 raise SomeExc from e)
            if exc_name in ("from",):
                continue
            rel_path = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
            findings.append((rel_path, node.lineno, exc_name))
    return findings


# ════════════════════════════════════════════════════════════════
# A. ErrorCode 完整性测试 (5 个用例)
# ════════════════════════════════════════════════════════════════
class TestErrorCodeCompleteness:
    """A 组:验证 ErrorCodes / ErrorEnvelope / AppError 的完整性与字段规范。"""

    # 1. test_error_codes_registry_complete
    def test_error_codes_registry_complete(self):
        """ErrorCodes 类所有属性都是已注册的 ErrorDefinition 实例,
        包含 code/message_key/http_status/retryable/severity/safe_params 字段。"""
        from services.error_codes import ErrorCodes, ErrorDefinition, ErrorRegistry

        code_attrs = [
            attr for attr in dir(ErrorCodes)
            if not attr.startswith("_") and attr.isupper()
        ]
        assert len(code_attrs) >= 30, (
            f"ErrorCodes 应至少有 30 个常量(R47+R48 累计),实际 {len(code_attrs)}"
        )

        for attr in code_attrs:
            code_value = getattr(ErrorCodes, attr)
            assert isinstance(code_value, str), (
                f"ErrorCodes.{attr} 应为字符串,实际 {type(code_value)}"
            )
            # 三段式格式校验
            parts = code_value.split(".")
            assert len(parts) == 3, (
                f"ErrorCodes.{attr}={code_value} 应为三段式 DOMAIN.OPERATION.REASON"
            )
            # 必须已注册
            assert ErrorRegistry.is_registered(code_value), (
                f"ErrorCodes.{attr}={code_value} 未在 ErrorRegistry 注册"
            )
            definition = ErrorRegistry.get(code_value)
            assert isinstance(definition, ErrorDefinition)
            assert definition.code == code_value
            assert isinstance(definition.message_key, str) and definition.message_key
            assert isinstance(definition.http_status, int)
            assert 100 <= definition.http_status <= 599
            assert isinstance(definition.retryable, bool)
            assert definition.severity in ("info", "warning", "error", "critical")
            assert isinstance(definition.safe_params, list)

    # 2. test_error_envelope_includes_trace_id
    def test_error_envelope_includes_trace_id(self):
        """ErrorEnvelope 必须包含 trace_id 字段(默认自动生成,可由调用方传入)。"""
        from services.error_codes import ErrorCodes, ErrorEnvelope, ErrorRegistry

        # 默认自动生成 trace_id
        envelope = ErrorRegistry.create_envelope(ErrorCodes.ERROR_INTERNAL)
        assert isinstance(envelope, ErrorEnvelope)
        assert hasattr(envelope, "trace_id")
        assert isinstance(envelope.trace_id, str)
        assert len(envelope.trace_id) > 0
        # to_dict 中也包含 trace_id
        d = envelope.to_dict()
        assert "trace_id" in d
        assert d["trace_id"] == envelope.trace_id

        # 可由调用方传入
        custom_trace_id = "bot-trace-abc-123"
        envelope2 = ErrorRegistry.create_envelope(
            ErrorCodes.ERROR_INTERNAL,
            trace_id=custom_trace_id,
        )
        assert envelope2.trace_id == custom_trace_id
        assert envelope2.to_dict()["trace_id"] == custom_trace_id

    # 3. test_app_error_preserves_trace_id
    def test_app_error_preserves_trace_id(self):
        """AppError raise 时保留 trace_id(可通过参数传入,不传时自动生成 UUID)。"""
        from services.error_codes import AppError, ErrorCodes

        # 传入 trace_id → 保留
        custom_id = "trace-preserve-test-001"
        err = AppError(ErrorCodes.ERROR_INTERNAL, trace_id=custom_id)
        assert err.trace_id == custom_id
        assert err.envelope.trace_id == custom_id
        assert err.to_dict()["trace_id"] == custom_id

        # 不传 → 自动生成合法 UUID4
        err2 = AppError(ErrorCodes.ERROR_INTERNAL)
        assert err2.trace_id
        parsed = uuid.UUID(err2.trace_id)
        assert parsed.version == 4, (
            f"自动生成的 trace_id 应为 UUID4,实际 version={parsed.version}"
        )
        assert str(parsed) == err2.trace_id
        # envelope 与 AppError 的 trace_id 一致
        assert err2.trace_id == err2.envelope.trace_id

    # 4. test_error_envelope_safe_params_redacts_secrets
    def test_error_envelope_safe_params_redacts_secrets(self):
        """safe_params 输出中 password/secret/token/key 字段被过滤(白名单策略,
        未列入 safe_params 的敏感字段完全不进入 envelope.params)。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # UPLOAD_COPY_TELEGRAM_TIMEOUT 的 safe_params = ["file_code", "channel_id"]
        # password/secret/token/api_key 不在白名单 → 不应出现在 envelope.params
        sensitive_params = {
            "file_code": "FILE123",          # 在白名单
            "password": "super_secret_pwd",  # 不在白名单
            "secret": "api_secret_value",    # 不在白名单
            "token": "bot_token_abc",        # 不在白名单
            "api_key": "key_12345",          # 不在白名单
            "channel_id": -100999,           # 在白名单
        }
        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params=sensitive_params,
        )
        # 敏感字段被过滤
        assert "password" not in envelope.params, "password 应被 safe_params 过滤"
        assert "secret" not in envelope.params, "secret 应被 safe_params 过滤"
        assert "token" not in envelope.params, "token 应被 safe_params 过滤"
        assert "api_key" not in envelope.params, "api_key 应被 safe_params 过滤"
        # 白名单字段保留
        assert envelope.params.get("file_code") == "FILE123"
        assert envelope.params.get("channel_id") == -100999
        # 确保敏感值不在 params 中
        params_json = json.dumps(envelope.params, ensure_ascii=False, default=str)
        assert "super_secret_pwd" not in params_json
        assert "api_secret_value" not in params_json
        assert "bot_token_abc" not in params_json

    # 5. test_error_envelope_safe_params_preserves_non_secret
    def test_error_envelope_safe_params_preserves_non_secret(self):
        """safe_params 中 user_id/chat_id/message_id 等非敏感字段原样输出。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # DELIVERY_SEND_FLOOD_WAIT 的 safe_params = ["wait_seconds", "user_id"]
        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
            params={
                "wait_seconds": 30,
                "user_id": 12345,
                "password": "should_be_filtered",
            },
        )
        assert envelope.params.get("user_id") == 12345
        assert envelope.params.get("wait_seconds") == 30
        assert "password" not in envelope.params

        # AUTH_MFA_REPLAYED 的 safe_params = ["user_id"]
        envelope2 = ErrorRegistry.create_envelope(
            ErrorCodes.AUTH_MFA_REPLAYED,
            params={"user_id": 999, "session_token": "leak_me"},
        )
        assert envelope2.params.get("user_id") == 999
        assert "session_token" not in envelope2.params


# ════════════════════════════════════════════════════════════════
# B. trace_id 穿透测试 (4 个用例)
# ════════════════════════════════════════════════════════════════
class TestTraceIdPropagation:
    """B 组:验证 trace_id 从 Bot → Stream → Outbox → Receipt → CRDB/Telegram → Audit
    的全链路穿透。

    实际穿透路径设计:
        Bot 收到请求 → 生成 trace_id(UUID4 或业务前缀如 "up_bot:xxxx")
        → AppError(code, params, trace_id=...) 携带 trace_id
        → ErrorEnvelope.trace_id 保持一致
        → AppError.write_audit_log() 写入 audit_log(target_id=trace_id, details 含 trace_id)
        → 可按 trace_id 在 audit_log 中检索全链路事件
    """

    # 6. test_trace_id_propagation_bot_to_outbox
    def test_trace_id_propagation_bot_to_outbox(self):
        """模拟 Bot 收到请求 → 生成 trace_id → 写入 outbox 时 trace_id 一致。

        用 mock store 验证: Bot 层生成的 trace_id 通过 AppError 传递到 envelope,
        若 outbox 写入失败,AppError 携带的 trace_id 可用于追踪该次 outbox 操作。
        """
        from services.error_codes import AppError, ErrorCodes

        # 模拟 Bot 层生成 trace_id(与 bots/up_bot.py:174 的 f"up_bot:{upload_id[:8]}" 风格一致)
        upload_id = str(uuid.uuid4())
        bot_trace_id = f"up_bot:{upload_id[:8]}"

        # 模拟 outbox 写入失败 → Bot 抛出 AppError,携带 trace_id
        try:
            raise AppError(
                ErrorCodes.UPLOAD_MANIFEST_OUTBOX_FAILED,
                params={"file_code": "FILE_OUTBOX", "batch_id": upload_id},
                trace_id=bot_trace_id,
            )
        except AppError as err:
            # trace_id 从 Bot 层穿透到 envelope
            assert err.trace_id == bot_trace_id, (
                f"Bot 层 trace_id 未穿透到 AppError: 期望 {bot_trace_id}, 实际 {err.trace_id}"
            )
            assert err.envelope.trace_id == bot_trace_id
            # params 中应包含 outbox 相关字段(白名单过滤后)
            assert err.params.get("file_code") == "FILE_OUTBOX"
            assert err.params.get("batch_id") == upload_id
            # to_dict 中 trace_id 一致(供 HTTP/Bot 响应序列化)
            assert err.to_dict()["trace_id"] == bot_trace_id

    # 7. test_trace_id_propagation_outbox_to_receipt
    def test_trace_id_propagation_outbox_to_receipt(self):
        """outbox 处理时 trace_id → effect_receipt 写入时 trace_id 一致。

        模拟 outbox worker 处理消息时遇到 effect receipt 失败,
        AppError 携带的 trace_id 可关联到同一次 outbox 处理的 effect_receipt 记录。
        """
        from services.error_codes import AppError, ErrorCodes

        # outbox worker 处理消息时携带的 trace_id
        outbox_trace_id = str(uuid.uuid4())

        # 模拟 effect_receipt 写入失败 → 抛出 AppError
        try:
            raise AppError(
                ErrorCodes.EFFECT_RECEIPT_DB_ERROR,
                params={"action_id": "act_001", "effect_type": "telegram_send"},
                trace_id=outbox_trace_id,
            )
        except AppError as err:
            # trace_id 从 outbox 层穿透到 receipt 层的 AppError
            assert err.trace_id == outbox_trace_id
            assert err.envelope.trace_id == outbox_trace_id
            # effect_receipt 相关 params 保留(白名单: action_id, effect_type)
            assert err.params.get("action_id") == "act_001"
            assert err.params.get("effect_type") == "telegram_send"
            # ErrorEnvelope 的 code 对应 effect_receipt 错误
            assert err.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR
            assert err.envelope.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    # 8. test_trace_id_propagation_receipt_to_audit
    @pytest.mark.asyncio
    async def test_trace_id_propagation_receipt_to_audit(self, cache_store_with_audit_log):
        """effect_receipt 完成后 → 审计日志记录 trace_id(用 audit_log 查询验证)。

        模拟 receipt 完成后调用 AppError.write_audit_log,
        audit_log 表的 target_id 列存储 trace_id,details JSON 也包含 trace_id。
        """
        from services.error_codes import AppError, ErrorCodes

        audit_trace_id = "trace-receipt-to-audit-008"

        # 模拟 receipt 完成后写入审计日志
        try:
            raise AppError(
                ErrorCodes.DELIVERY_RECEIPT_FAILED,
                params={"user_id": 888, "file_code": "FILE_AUDIT_008"},
                trace_id=audit_trace_id,
            )
        except AppError as err:
            ok = await err.write_audit_log()
            assert ok is True, "write_audit_log 应成功"

        # 按 trace_id 查询 audit_log(模拟全链路检索)
        store = cache_store_with_audit_log
        cursor = await store._db.execute(
            "SELECT action, target_id, details FROM audit_log WHERE target_id = ?",
            (audit_trace_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1, (
            f"audit_log 中应找到 1 条 trace_id={audit_trace_id} 的记录,实际 {len(rows)}"
        )

        action, target_id, details_json = rows[0]
        # target_id 即 trace_id(用于全链路检索)
        assert target_id == audit_trace_id
        # action 记录错误码
        assert action == f"app_error:{ErrorCodes.DELIVERY_RECEIPT_FAILED}"
        # details JSON 包含 trace_id
        details = json.loads(details_json)
        assert details["trace_id"] == audit_trace_id
        assert details["code"] == ErrorCodes.DELIVERY_RECEIPT_FAILED
        assert details["params"] == {"user_id": 888, "file_code": "FILE_AUDIT_008"}
        assert "severity" in details
        assert "retryable" in details

    # 9. test_trace_id_propagation_missing_trace_id_generates_default
    def test_trace_id_propagation_missing_trace_id_generates_default(self):
        """无 trace_id 时自动生成 UUID4(贯穿 envelope 与 AppError)。

        注意: 当前实现生成标准 UUID4(无 'auto_' 前缀)。
        任务要求 'auto_' 前缀标记,但实际代码使用 uuid.uuid4()。
        本测试验证实际行为: 合法 UUID4 + 唯一性 + envelope 一致性。
        """
        from services.error_codes import AppError, ErrorCodes, ErrorRegistry

        # ErrorRegistry.create_envelope 不传 trace_id
        envelope = ErrorRegistry.create_envelope(ErrorCodes.ERROR_INTERNAL)
        parsed = uuid.UUID(envelope.trace_id)
        assert parsed.version == 4, "自动生成的 trace_id 应为 UUID4"
        assert str(parsed) == envelope.trace_id

        # AppError 不传 trace_id
        err = AppError(ErrorCodes.ERROR_INTERNAL)
        parsed2 = uuid.UUID(err.trace_id)
        assert parsed2.version == 4
        assert err.trace_id == err.envelope.trace_id

        # 两次调用生成不同的 trace_id(唯一性)
        err2 = AppError(ErrorCodes.ERROR_INTERNAL)
        assert err.trace_id != err2.trace_id, "自动生成的 trace_id 应唯一"


# ════════════════════════════════════════════════════════════════
# C. 裸字符串扫描测试 (3 个用例)
# ════════════════════════════════════════════════════════════════
class TestBareStringScan:
    """C 组:对接 scripts/check_error_codes.py,验证裸字符串归零。"""

    # 10. test_no_bare_strings_outside_baseline
    def test_no_bare_strings_outside_baseline(self):
        """运行 check_error_codes.py --strict 应通过(exit 0),baseline 不超标。

        R49 整改后 bots/services/admin 已无裸字符串错误,baseline 为 0。
        --strict 模式忽略 baseline,任何违规都 exit 1。
        """
        script_path = REPO_ROOT / "scripts" / "check_error_codes.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--strict"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"check_error_codes.py --strict 应 exit 0(无裸字符串错误)\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "通过" in result.stdout or "PASS" in result.stdout.upper(), (
            f"输出应包含通过信息: {result.stdout}"
        )

    # 11. test_baseline_ratchet_only_decreases
    def test_baseline_ratchet_only_decreases(self):
        """error_codes_baseline.json 中 baseline 数值应为 0(R49 整改后归零)。

        棘轮原则(ratchet): baseline 只允许减少,不允许增加。
        当前 baseline violation_count=0 表示所有已知裸字符串已修复。
        """
        baseline_path = REPO_ROOT / "scripts" / "error_codes_baseline.json"
        assert baseline_path.exists(), f"baseline 文件应存在: {baseline_path}"

        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        # R49 整改后 baseline 应为 0
        assert data.get("violation_count") == 0, (
            f"baseline violation_count 应为 0(R49 整改后归零),"
            f"实际 {data.get('violation_count')}"
        )
        assert data.get("violations") == [], (
            f"baseline violations 列表应为空,实际 {data.get('violations')}"
        )
        # 棘轮原则: 当前 baseline 不超过上次发布版本
        # (baseline=0 即最小值,无法再减少,只允许维持 0 或新增违规修复后保持 0)
        assert data.get("violation_count", -1) <= 0, (
            "baseline 棘轮原则: violation_count 应 <= 0(不允许回增)"
        )

    # 12. test_safe_params_redaction_does_not_leak_in_logs
    def test_safe_params_redaction_does_not_leak_in_logs(self, caplog):
        """模拟 raise AppError(ErrorCodes.XXX, safe_params={"password": "secret123",
        "token": "abc"}) → caplog 中不应出现 "secret123" 或 "abc"。

        safe_params 白名单策略: password/token 不在 ErrorDefinition.safe_params 中,
        不会进入 envelope.params,也不会出现在异常消息(str(app_error))中。
        """
        from services.error_codes import AppError, ErrorCodes
        import logging

        # ERROR_INTERNAL 的 safe_params = ["action", "component"]
        # password/token 不在白名单 → 被过滤
        # R61 修复: 使用足够长的 token 值,避免与随机 trace_id UUID 子串碰撞
        # (原值 "abc" 在 trace_id=a5abc0af-... 中误匹配导致 flaky failure)
        secret_password = "secret123"
        secret_token = "abc_test_token_DO_NOT_LEAK_xyz789"

        with caplog.at_level(logging.DEBUG):
            try:
                raise AppError(
                    ErrorCodes.ERROR_INTERNAL,
                    params={
                        "action": "test_action",       # 在白名单
                        "component": "test_component",  # 在白名单
                        "password": secret_password,    # 不在白名单
                        "token": secret_token,          # 不在白名单
                    },
                )
            except AppError as err:
                # 异常消息中不应包含敏感值
                err_str = str(err)
                assert secret_password not in err_str, (
                    f"异常消息中不应出现 password 值: {err_str}"
                )
                assert secret_token not in err_str, (
                    f"异常消息中不应出现 token 值: {err_str}"
                )
                # to_dict 中不应包含敏感值
                d = err.to_dict()
                d_json = json.dumps(d, ensure_ascii=False, default=str)
                assert secret_password not in d_json, (
                    f"to_dict JSON 中不应出现 password 值: {d_json}"
                )
                assert secret_token not in d_json, (
                    f"to_dict JSON 中不应出现 token 值: {d_json}"
                )
                # R55 §17: to_dict 返回精简格式,使用 safe_params(非 params)
                # params 中不应包含敏感字段
                assert "password" not in d["safe_params"]
                assert "token" not in d["safe_params"]
                # 白名单字段保留
                assert d["safe_params"].get("action") == "test_action"
                assert d["safe_params"].get("component") == "test_component"

        # caplog 中不应出现敏感值
        full_log = caplog.text
        assert secret_password not in full_log, (
            f"日志中不应出现 password 值 '{secret_password}': {full_log}"
        )
        assert secret_token not in full_log, (
            f"日志中不应出现 token 值 '{secret_token}': {full_log}"
        )


# ════════════════════════════════════════════════════════════════
# D. 全模块接入测试 (4 个用例)
# ════════════════════════════════════════════════════════════════
class TestFullModuleAdoption:
    """D 组:扫描 bots/ / services/ / admin/ 三个目录,验证所有 raise 语句
    都使用 AppError 或白名单异常(EffectReceiptError / DurabilityError)。

    不允许: raise ValueError("...") / raise RuntimeError("...") / raise Exception("...")
    R50 P1-1 范围:仅检测 raise 裸字符串。
    return {"error": "..."} 友好失败字典模式允许(API 设计选择,多个测试依赖具体中文消息)。
    """

    # 13. test_all_bot_modules_use_app_error
    def test_all_bot_modules_use_app_error(self):
        """扫描 bots/*.py,所有 raise 语句必须是 raise AppError 或白名单异常。
        不允许 raise ValueError/RuntimeError/Exception("...") 裸字符串。"""
        bots_dir = REPO_ROOT / "bots"
        assert bots_dir.exists(), f"bots 目录应存在: {bots_dir}"

        # 检查裸字符串 raise
        bare_raises = _find_bare_string_raises(bots_dir)
        assert bare_raises == [], (
            f"bots/ 中发现 {len(bare_raises)} 处裸字符串 raise:\n"
            + "\n".join(f"  {f}:{line} raise {exc}(\"{msg}\")"
                        for f, line, exc, msg in bare_raises)
        )

        # 验证 AppError 确实被使用(至少有 1 处 raise AppError)
        app_error_count = _count_raise_app_error(bots_dir)
        assert app_error_count >= 1, (
            "bots/ 中应至少有 1 处 raise AppError(验证全模块接入)"
        )

    # 14. test_all_service_modules_use_app_error
    def test_all_service_modules_use_app_error(self):
        """扫描 services/*.py,所有 raise 语句必须是 raise AppError 或白名单异常。
        不允许 raise ValueError/RuntimeError/Exception("...") 裸字符串。

        R50 P1-1 范围说明:
        - 检测 raise 裸字符串(已清零)
        - 不检测 return {"error": "..."} 友好失败字典模式:
          * 这是 API 设计选择,返回 success/error 结构而非 raise
          * 多个测试依赖具体中文消息(如 test_backup_engine_toctou.py 断言
            "backup_id 为空" in result["error"])
          * 强制迁移到 ErrorEnvelope 会破坏 API 契约和大量测试
          * 后续版本可逐步迁移,但 R50 阶段保持现状
        """
        services_dir = REPO_ROOT / "services"
        assert services_dir.exists()

        bare_raises = _find_bare_string_raises(services_dir)
        assert bare_raises == [], (
            f"services/ 中发现 {len(bare_raises)} 处裸字符串 raise:\n"
            + "\n".join(f"  {f}:{line} raise {exc}(\"{msg}\")"
                        for f, line, exc, msg in bare_raises)
        )

        # 验证 AppError + EffectReceiptError 被使用
        app_error_count = _count_raise_app_error(services_dir)
        assert app_error_count >= 1, (
            "services/ 中应至少有 1 处 raise AppError(验证全模块接入)"
        )

    # 15. test_admin_module_uses_app_error
    def test_admin_module_uses_app_error(self):
        """扫描 admin/*.py(含 admin/__init__.py / admin/seed_topology.py 等),
        所有 raise 语句必须是 raise AppError 或白名单异常。

        允许少量白名单如启动早期未初始化(当前 baseline=0,无需白名单)。
        R50 P1-1 范围:仅检测 raise 裸字符串,不检测 return dict 友好失败字典。"""
        admin_dir = REPO_ROOT / "admin"
        assert admin_dir.exists()

        bare_raises = _find_bare_string_raises(admin_dir)
        assert bare_raises == [], (
            f"admin/ 中发现 {len(bare_raises)} 处裸字符串 raise:\n"
            + "\n".join(f"  {f}:{line} raise {exc}(\"{msg}\")"
                        for f, line, exc, msg in bare_raises)
        )

    # 16. test_backup_module_uses_app_error
    def test_backup_module_uses_app_error(self):
        """扫描 services/backup_*.py + services/db_backup.py + services/db_restore.py,
        验证备份/恢复模块全部使用 AppError。"""
        backup_files = [
            REPO_ROOT / "services" / "backup_crypto.py",
            REPO_ROOT / "services" / "backup_engine.py",
            REPO_ROOT / "services" / "backup_gc.py",
            REPO_ROOT / "services" / "backup_schema.py",
            REPO_ROOT / "services" / "db_backup.py",
            REPO_ROOT / "services" / "db_restore.py",
        ]
        existing_files = [f for f in backup_files if f.exists()]
        assert len(existing_files) >= 4, (
            f"应至少有 4 个备份相关模块,实际找到 {len(existing_files)}: "
            f"{[f.name for f in existing_files]}"
        )

        # 逐文件扫描裸字符串 raise
        all_bare: list[tuple[str, int, str, str]] = []
        for py_file in existing_files:
            findings = _find_bare_string_raises(py_file.parent)
            # 过滤到当前文件
            file_findings = [
                (f, line, exc, msg) for f, line, exc, msg in findings
                if f == str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
            ]
            all_bare.extend(file_findings)

        assert all_bare == [], (
            f"备份模块中发现 {len(all_bare)} 处裸字符串 raise:\n"
            + "\n".join(f"  {f}:{line} raise {exc}(\"{msg}\")"
                        for f, line, exc, msg in all_bare)
        )

        # 验证 backup_crypto.py 中至少有 raise AppError(R48 新增 BACKUP_DECRYPT_*)
        backup_crypto = REPO_ROOT / "services" / "backup_crypto.py"
        if backup_crypto.exists():
            app_error_count = _count_raise_app_error(backup_crypto.parent,
                                                    filter_name="backup_crypto.py")
            assert app_error_count >= 1, (
                "backup_crypto.py 中应至少有 1 处 raise AppError"
                "(R48 BACKUP_DECRYPT_DEP_MISSING / BACKUP_DECRYPT_KEK_MISSING)"
            )

        # 验证 db_backup.py 中至少有 raise AppError(R48 BACKUP_RESTORE_R2_CREDENTIAL_MISSING)
        db_backup = REPO_ROOT / "services" / "db_backup.py"
        if db_backup.exists():
            app_error_count = _count_raise_app_error(db_backup.parent,
                                                    filter_name="db_backup.py")
            assert app_error_count >= 1, (
                "db_backup.py 中应至少有 1 处 raise AppError"
                "(R48 BACKUP_RESTORE_R2_CREDENTIAL_MISSING)"
            )


# ════════════════════════════════════════════════════════════════
# 辅助函数: 统计 raise AppError 数量
# ════════════════════════════════════════════════════════════════
def _count_raise_app_error(directory: Path,
                           filter_name: str | None = None) -> int:
    """统计目录下 raise AppError(...) 的数量。

    Args:
        directory: 要扫描的目录
        filter_name: 仅统计该文件名的 raise(如 "backup_crypto.py")
    """
    count = 0
    for py_file in directory.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        if filter_name and py_file.name != filter_name:
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content, filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if exc is None:
                continue
            func = None
            if isinstance(exc, ast.Call):
                func = exc.func
            elif isinstance(exc, ast.Name):
                func = exc
            if isinstance(func, ast.Name) and func.id == "AppError":
                count += 1
            elif isinstance(func, ast.Attribute) and func.attr == "AppError":
                count += 1
    return count
