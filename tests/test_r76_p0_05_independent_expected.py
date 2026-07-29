"""R76 P0-05: restore capability 所有 expected 值必须来自独立来源(RestoreOperationContext)。

R76 终审报告 P0-05 整改验收测试:
    所有 expected 值(operation_id / source_sha / nonce / run_id / run_attempt /
    audience / target_identity / target_uri / allowed_action / manifest_digest /
    payload_digest)必须:
    1. 来自 RestoreOperationContext(独立来源)
    2. 缺失时 fail-closed(抛 AppError),不得静默回退为 0 / "local" / 1
    3. 在消费入口独立比较(不得仅与硬编码常量比较)

测试用例:
    1. capability.operation_id != context.operation_id → fail-closed
    2. capability.allowed_action != context.allowed_action → fail-closed
    3. run_id 缺失(env 未设置)→ fail-closed(不回退为 0)
    4. run_attempt 缺失 → fail-closed(不回退为 1)
    5. source_sha 缺失 → fail-closed(不回退为 "local")

整改文件:
    - services/restore_capability_file.py (verify_capability 新增 expected_operation_id /
      expected_allowed_action 参数及校验;verify_and_consume_capability 传入新参数)
    - services/backup_dr_validate.py (两处 env 读取改为 fail-closed)
    - services/restore_orchestrator.py (start_operation 删除 env 回退)
    - services/db_restore.py (run_restore 改用 verify_and_consume_capability)
    - services/restore_writer.py (_restore_from_backup_data 使用
      operation_context.target_identity / target_uri)
    - services/restore_operation_context.py (删除 allowed_action / nonce 默认值)
"""
from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 测试辅助 ──────────────────────────────────────────────────


def _ensure_backup_dr_validate_importable():
    """确保 services.backup_dr_validate 可导入(仅依赖 loguru / i18n)。"""
    if "services.backup_dr_validate" in sys.modules:
        return sys.modules["services.backup_dr_validate"]
    import importlib
    return importlib.import_module("services.backup_dr_validate")


def _build_operation_context(
    *,
    payload_digest: str = "d" * 64,
    backup_id: str = "backup_test_001",
    schema_fingerprint: str = "R76-P0-05-test-fingerprint",
    nonce: str = "test_nonce_0123456789abcdef0123456789abcdef",
    operation_id: str = "op_test_001",
    allowed_action: str = "restore_to_blank_target",
):
    """R76 P0-05: 构造合法的 RestoreOperationContext(独立 expected 值来源)。"""
    from services.restore_operation_context import RestoreOperationContext
    ctx = RestoreOperationContext(
        operation_id=operation_id,
        backup_id=backup_id,
        source_sha="test_source_sha",
        run_id=12345,
        run_attempt=2,
        audience="test_audience",
        target_identity=schema_fingerprint,
        target_uri="sqlite:///tmp/test_restore_r76.db",
        manifest_digest="a" * 64,
        payload_digest=payload_digest,
        allowed_action=allowed_action,
        nonce=nonce,
    )
    ctx.validate()  # fail-closed
    return ctx


def _build_signed_capability(
    *,
    signing_key: bytes = b"r76_p0_05_test_signing_key_32bytes!",
    operation_id: str = "op_test_001",
    backup_id: str = "backup_test_001",
    source_sha: str = "test_source_sha",
    run_id: int = 12345,
    run_attempt: int = 2,
    audience: str = "test_audience",
    target_database_identity: str = "R76-P0-05-test-fingerprint",
    target_path: str = "db_backup/payload_test.enc",
    target_uri: str = "sqlite:///tmp/test_restore_r76.db",
    nonce: str = "test_nonce_0123456789abcdef0123456789abcdef",
    allowed_action: str = "restore_to_blank_target",
):
    """R76 P0-05: 构造合法的 HMAC 签名 capability dict。"""
    from services.restore_capability_file import issue_capability
    return issue_capability(
        backup_id=backup_id,
        source_sha=source_sha,
        target_database_identity=target_database_identity,
        target_path=target_path,
        run_id=run_id,
        run_attempt=run_attempt,
        audience=audience,
        target_uri=target_uri,
        signing_key=signing_key,
        operation_id=operation_id,
        nonce=nonce,
    )


# ═══════════════════════════════════════════════════════════════
# 用例 1 & 2: capability.operation_id / allowed_action 与 context 不一致 → fail-closed
# ═══════════════════════════════════════════════════════════════


class TestOperationIdAndAllowedActionMismatch:
    """R76 P0-05: operation_id 与 allowed_action 必须在消费入口独立比较。"""

    def setup_method(self):
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_operation_id_mismatch_raises(self):
        """用例 1: capability.operation_id != context.operation_id → fail-closed。

        R76 P0-05: expected_operation_id 不再仅与硬编码常量比较,
        必须在消费入口与独立来源(context)比对。
        """
        from services.restore_capability_file import verify_capability
        from services.error_codes import AppError, ErrorCodes

        signing_key = b"r76_p0_05_test_signing_key_32bytes!"
        # capability 的 operation_id 为 "op_capability_001"
        capability = _build_signed_capability(
            signing_key=signing_key,
            operation_id="op_capability_001",
        )
        # context 的 operation_id 为 "op_context_001"(不一致)
        with pytest.raises(AppError) as exc_info:
            verify_capability(
                capability,
                signing_key=signing_key,
                expected_operation_id="op_context_001",
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # 错误 reason 应明确指向 operation_id 不匹配
        params = exc_info.value.params if hasattr(exc_info.value, "params") else {}
        assert params.get("reason") == "capability_operation_id_mismatch"

    @pytest.mark.asyncio
    async def test_allowed_action_mismatch_raises(self):
        """用例 2: capability.allowed_action != context.allowed_action → fail-closed。

        R76 P0-05: expected_allowed_action 不再仅与硬编码常量
        ALLOWED_ACTION_RESTORE("restore_to_blank_target") 比较,
        必须在消费入口与独立来源(context)比对。
        """
        from services.restore_capability_file import verify_capability, issue_capability
        from services.error_codes import AppError, ErrorCodes

        signing_key = b"r76_p0_05_test_signing_key_32bytes!"
        # 构造一个 allowed_action="restore_to_blank_target" 的 capability
        capability = _build_signed_capability(
            signing_key=signing_key,
            allowed_action="restore_to_blank_target",
        )
        # context 的 allowed_action 为 "restore_to_active_target"(不一致)
        # 注意:capability 内部 allowed_action 必须为 "restore_to_blank_target"
        # 才能通过 issue_capability 校验,但 verify_capability 在与 context 比对前
        # 会先校验 capability["allowed_action"] == ALLOWED_ACTION_RESTORE
        # 因此本测试验证:当 context.allowed_action 与 capability.allowed_action
        # 不一致时,verify_capability 应抛 capability_allowed_action_mismatch
        with pytest.raises(AppError) as exc_info:
            verify_capability(
                capability,
                signing_key=signing_key,
                expected_allowed_action="restore_to_active_target",
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        params = exc_info.value.params if hasattr(exc_info.value, "params") else {}
        assert params.get("reason") == "capability_allowed_action_mismatch"

    @pytest.mark.asyncio
    async def test_operation_id_match_passes(self):
        """附加: capability.operation_id == context.operation_id → 通过校验。"""
        from services.restore_capability_file import verify_capability

        signing_key = b"r76_p0_05_test_signing_key_32bytes!"
        capability = _build_signed_capability(
            signing_key=signing_key,
            operation_id="op_match_001",
        )
        # 一致 — 不应抛异常
        verify_capability(
            capability,
            signing_key=signing_key,
            expected_operation_id="op_match_001",
            expected_allowed_action="restore_to_blank_target",
        )

    @pytest.mark.asyncio
    async def test_verify_and_consume_capability_uses_context_operation_id(self, monkeypatch):
        """附加: verify_and_consume_capability 传入 context.operation_id 与
        context.allowed_action 给 verify_capability(独立来源比对)。"""
        from services.restore_capability_file import (
            verify_and_consume_capability,
            issue_capability,
        )
        from services.error_codes import AppError, ErrorCodes

        signing_key = b"r76_p0_05_test_signing_key_32bytes!"
        capability = _build_signed_capability(
            signing_key=signing_key,
            operation_id="op_capability_xyz",
        )
        # context.operation_id 与 capability.operation_id 不一致 → fail-closed
        context = _build_operation_context(operation_id="op_context_xyz")
        # mock nonce_store.consume 返回 True(隔离 nonce 消费逻辑)
        mock_nonce_store = MagicMock()
        mock_nonce_store.consume = AsyncMock(return_value=True)

        with pytest.raises(AppError) as exc_info:
            await verify_and_consume_capability(
                capability,
                signing_key=signing_key,
                operation_context=context,
                nonce_store=mock_nonce_store,
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # 在 verify_capability 阶段就失败,不应调用 nonce_store.consume
        assert not mock_nonce_store.consume.called


# ═══════════════════════════════════════════════════════════════
# 用例 3, 4, 5: env 缺失 → fail-closed(不回退为 0 / 1 / "local")
# ═══════════════════════════════════════════════════════════════


class TestEnvMissingFailClosed:
    """R76 P0-05: env 缺失时必须 fail-closed,不得静默回退为 0 / 1 / "local"。

    生产环境(无 ALLOW_LEGACY_RESTORE=1)env 缺失 → raise AppError
    测试环境(ALLOW_LEGACY_RESTORE=1)env 缺失 → 允许回退到默认值
    """

    def setup_method(self):
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_run_id_missing_raises_in_production(self, monkeypatch):
        """用例 3: run_id 缺失(env 未设置)→ fail-closed(不回退为 0)。

        生产环境:无 ALLOW_LEGACY_RESTORE=1,无 GITHUB_RUN_ID → raise
        """
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        # 模拟生产环境:无 ALLOW_LEGACY_RESTORE,有 GITHUB_SHA / GITHUB_RUN_ATTEMPT,无 GITHUB_RUN_ID
        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)
        monkeypatch.setenv("GITHUB_SHA", "abc123def456")
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")  # 有 attempt,但无 run_id
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-key-32-bytes-for-r76-test!")

        # mock get_cache_store 返回 None(测试隔离,触发 nonce_store fail-closed)
        # 但实际应在 env 校验阶段就抛异常(在到达 nonce_store 之前)
        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: None)

        # mock r2_storage.download 返回完整三段式备份(不应被调用,因 env 校验先行失败)
        from unittest.mock import MagicMock as _MM
        mock_r2 = _MM()
        mock_r2.download = AsyncMock(return_value=None)

        with pytest.raises(AppError) as exc_info:
            await mod.validate_and_restore_backup_strict(
                data={"tables": {}},
                tables=None,
                merge=False,
                timestamp="20260718_120000",
                backup_type="full",
                r2_storage=mock_r2,
                signing_key=b"fake_signing_key_for_test_only",
                decryptor=_MM(decrypt=lambda ct, aad=None: b'{"tables": {}}'),
                expected_manifest_key="db_backup/manifest.json",
                expected_backup_id="20260718_120000",
                current_schema_version="test_v1",
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        params = exc_info.value.params if hasattr(exc_info.value, "params") else {}
        assert "run_id required" in params.get("reason", ""), (
            f"应抛 run_id required,实际 reason: {params.get('reason')}"
        )

    @pytest.mark.asyncio
    async def test_run_attempt_missing_raises_in_production(self, monkeypatch):
        """用例 4: run_attempt 缺失 → fail-closed(不回退为 1)。

        生产环境:无 ALLOW_LEGACY_RESTORE=1,有 GITHUB_RUN_ID,无 GITHUB_RUN_ATTEMPT → raise
        """
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        # 模拟生产环境:无 ALLOW_LEGACY_RESTORE,有 GITHUB_SHA / GITHUB_RUN_ID,无 GITHUB_RUN_ATTEMPT
        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)
        monkeypatch.setenv("GITHUB_SHA", "abc123def456")
        monkeypatch.setenv("GITHUB_RUN_ID", "12345")
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-key-32-bytes-for-r76-test!")

        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: None)

        from unittest.mock import MagicMock as _MM
        mock_r2 = _MM()
        mock_r2.download = AsyncMock(return_value=None)

        with pytest.raises(AppError) as exc_info:
            await mod.validate_and_restore_backup_strict(
                data={"tables": {}},
                tables=None,
                merge=False,
                timestamp="20260718_120000",
                backup_type="full",
                r2_storage=mock_r2,
                signing_key=b"fake_signing_key_for_test_only",
                decryptor=_MM(decrypt=lambda ct, aad=None: b'{"tables": {}}'),
                expected_manifest_key="db_backup/manifest.json",
                expected_backup_id="20260718_120000",
                current_schema_version="test_v1",
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        params = exc_info.value.params if hasattr(exc_info.value, "params") else {}
        assert "run_attempt required" in params.get("reason", ""), (
            f"应抛 run_attempt required,实际 reason: {params.get('reason')}"
        )

    @pytest.mark.asyncio
    async def test_source_sha_missing_raises_in_production(self, monkeypatch):
        """用例 5: source_sha 缺失 → fail-closed(不回退为 "local")。

        生产环境:无 ALLOW_LEGACY_RESTORE=1,无 GITHUB_SHA → raise
        """
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        # 模拟生产环境:无 ALLOW_LEGACY_RESTORE,无 GITHUB_SHA
        monkeypatch.delenv("ALLOW_LEGACY_RESTORE", raising=False)
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        monkeypatch.setenv("GITHUB_RUN_ID", "12345")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-key-32-bytes-for-r76-test!")

        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: None)

        from unittest.mock import MagicMock as _MM
        mock_r2 = _MM()
        mock_r2.download = AsyncMock(return_value=None)

        with pytest.raises(AppError) as exc_info:
            await mod.validate_and_restore_backup_strict(
                data={"tables": {}},
                tables=None,
                merge=False,
                timestamp="20260718_120000",
                backup_type="full",
                r2_storage=mock_r2,
                signing_key=b"fake_signing_key_for_test_only",
                decryptor=_MM(decrypt=lambda ct, aad=None: b'{"tables": {}}'),
                expected_manifest_key="db_backup/manifest.json",
                expected_backup_id="20260718_120000",
                current_schema_version="test_v1",
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        params = exc_info.value.params if hasattr(exc_info.value, "params") else {}
        assert "source_sha required" in params.get("reason", ""), (
            f"应抛 source_sha required,实际 reason: {params.get('reason')}"
        )

    @pytest.mark.asyncio
    async def test_legacy_mode_allows_env_fallback(self, monkeypatch):
        """附加: 测试环境(ALLOW_LEGACY_RESTORE=1)env 缺失允许回退到默认值。

        验证测试环境兼容性:conftest.py 的 allow_legacy_restore_writer fixture
        设置 ALLOW_LEGACY_RESTORE=1,即使 env 缺失也不应抛 VALIDATION_FAILED。
        """
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError

        # 测试环境:ALLOW_LEGACY_RESTORE=1,无 GITHUB_SHA / GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-key-32-bytes-for-r76-test!")

        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: None)

        from unittest.mock import MagicMock as _MM
        mock_r2 = _MM()
        mock_r2.download = AsyncMock(return_value=None)

        # 在 legacy 模式下,env 缺失不应抛 VALIDATION_FAILED
        # (后续会因 R2 download 返回 None 在 strict 三段式验证中失败,
        # 但失败原因是 BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,而非 VALIDATION_FAILED)
        with pytest.raises(AppError) as exc_info:
            await mod.validate_and_restore_backup_strict(
                data={"tables": {}},
                tables=None,
                merge=False,
                timestamp="20260718_120000",
                backup_type="full",
                r2_storage=mock_r2,
                signing_key=b"fake_signing_key_for_test_only",
                decryptor=_MM(decrypt=lambda ct, aad=None: b'{"tables": {}}'),
                expected_manifest_key="db_backup/manifest.json",
                expected_backup_id="20260718_120000",
                current_schema_version="test_v1",
            )
        # 不应是 VALIDATION_FAILED(应通过 env 校验,在后续 strict 验证中失败)
        from services.error_codes import ErrorCodes
        assert exc_info.value.code != ErrorCodes.VALIDATION_FAILED, (
            "legacy 模式下 env 缺失不应抛 VALIDATION_FAILED"
        )


# ═══════════════════════════════════════════════════════════════
# 用例 6: RestoreOperationContext 不允许 allowed_action / nonce 默认值
# ═══════════════════════════════════════════════════════════════


class TestRestoreOperationContextNoDefaults:
    """R76 P0-05: RestoreOperationContext 删除 allowed_action / nonce 默认值。

    构造时缺失这两个字段应抛 TypeError(fail-closed)。
    """

    def test_missing_allowed_action_raises_typeerror(self):
        """构造 RestoreOperationContext 时不传 allowed_action → TypeError。"""
        from services.restore_operation_context import RestoreOperationContext

        with pytest.raises(TypeError):
            RestoreOperationContext(
                operation_id="op_test",
                backup_id="backup_test",
                source_sha="sha_test",
                run_id=1,
                run_attempt=1,
                audience="audience_test",
                target_identity="identity_test",
                target_uri="sqlite:///tmp/test.db",
                manifest_digest="a" * 64,
                payload_digest="b" * 64,
                nonce="test_nonce",
                # 缺失 allowed_action
            )

    def test_missing_nonce_raises_typeerror(self):
        """构造 RestoreOperationContext 时不传 nonce → TypeError。"""
        from services.restore_operation_context import RestoreOperationContext

        with pytest.raises(TypeError):
            RestoreOperationContext(
                operation_id="op_test",
                backup_id="backup_test",
                source_sha="sha_test",
                run_id=1,
                run_attempt=1,
                audience="audience_test",
                target_identity="identity_test",
                target_uri="sqlite:///tmp/test.db",
                manifest_digest="a" * 64,
                payload_digest="b" * 64,
                allowed_action="restore_to_blank_target",
                # 缺失 nonce
            )

    def test_explicit_allowed_action_and_nonce_required(self):
        """附加: 显式传入 allowed_action 与 nonce 才能成功构造。"""
        from services.restore_operation_context import RestoreOperationContext

        ctx = RestoreOperationContext(
            operation_id="op_test",
            backup_id="backup_test",
            source_sha="sha_test",
            run_id=1,
            run_attempt=1,
            audience="audience_test",
            target_identity="identity_test",
            target_uri="sqlite:///tmp/test.db",
            manifest_digest="a" * 64,
            payload_digest="b" * 64,
            allowed_action="restore_to_blank_target",
            nonce="test_nonce_value",
        )
        ctx.validate()  # fail-closed
        assert ctx.allowed_action == "restore_to_blank_target"
        assert ctx.nonce == "test_nonce_value"


# ═══════════════════════════════════════════════════════════════
# 用例 7: from_dict 严格要求 allowed_action / nonce 字段
# ═══════════════════════════════════════════════════════════════


class TestFromDictStrictFields:
    """R76 P0-05: from_dict 不再为 allowed_action 提供默认值。"""

    def test_from_dict_missing_allowed_action_raises(self):
        """from_dict 缺失 allowed_action → ValueError(不再回退到默认值)。"""
        from services.restore_operation_context import RestoreOperationContext

        data = {
            "operation_id": "op_test",
            "backup_id": "backup_test",
            "source_sha": "sha_test",
            "run_id": 1,
            "run_attempt": 1,
            "audience": "audience_test",
            "target_identity": "identity_test",
            "target_uri": "sqlite:///tmp/test.db",
            "manifest_digest": "a" * 64,
            "payload_digest": "b" * 64,
            "nonce": "test_nonce_value",
            # 缺失 allowed_action
        }
        with pytest.raises((ValueError, KeyError)):
            RestoreOperationContext.from_dict(data)

    def test_from_dict_missing_nonce_raises(self):
        """from_dict 缺失 nonce → ValueError。"""
        from services.restore_operation_context import RestoreOperationContext

        data = {
            "operation_id": "op_test",
            "backup_id": "backup_test",
            "source_sha": "sha_test",
            "run_id": 1,
            "run_attempt": 1,
            "audience": "audience_test",
            "target_identity": "identity_test",
            "target_uri": "sqlite:///tmp/test.db",
            "manifest_digest": "a" * 64,
            "payload_digest": "b" * 64,
            "allowed_action": "restore_to_blank_target",
            # 缺失 nonce
        }
        with pytest.raises((ValueError, KeyError)):
            RestoreOperationContext.from_dict(data)

    def test_from_dict_with_all_fields_succeeds(self):
        """附加: from_dict 传入所有字段 → 成功构造 + validate 通过。"""
        from services.restore_operation_context import RestoreOperationContext

        data = {
            "operation_id": "op_test",
            "backup_id": "backup_test",
            "source_sha": "sha_test",
            "run_id": 1,
            "run_attempt": 1,
            "audience": "audience_test",
            "target_identity": "identity_test",
            "target_uri": "sqlite:///tmp/test.db",
            "manifest_digest": "a" * 64,
            "payload_digest": "b" * 64,
            "allowed_action": "restore_to_blank_target",
            "nonce": "test_nonce_value",
        }
        ctx = RestoreOperationContext.from_dict(data)
        assert ctx.allowed_action == "restore_to_blank_target"
        assert ctx.nonce == "test_nonce_value"
