"""R62 P0-01 / P0-02: 恢复信任链整改 — 不可绕过的恢复能力令牌。

测试覆盖:
1. P0-01: 移除 validate_and_restore_backup_strict 的 skip_strict_validation /
   validation_note / 6 个 *_override 危险参数,以及 skip-mode 代码路径。
2. P0-02: _restore_from_backup_data 强制 capability 边界 — 首条语句调用
   capability.assert_valid(...),数据从 VerifiedBackupPayload.tables 读取。

测试用例(对照 spec):
    1.  合法 capability 通过 assert_valid(无异常)
    2.  过期 capability 抛 AppError
    3.  重放 capability(nonce 被消费两次)抛 AppError
    4.  payload_digest 不匹配抛 AppError
    5.  必填字段缺失抛 AppError(构造时校验)
    6.  无 sentinel 直接构造抛 RuntimeError
    7.  篡改 manifest/payload/ciphertext/plaintext/key-id 均在写入前失败
        (validate_and_restore_backup_strict 严格三段式验证拒绝,不调用写入器)
    8.  run_restore() 旧格式备份直接失败(不再通过 skip 绕过)
    9.  _restore_from_backup_data 从 verified_payload.tables 读取
        (不再从 data.get("tables", {}) 读取)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 测试辅助 ──────────────────────────────────────────────────


def _ensure_backup_dr_validate_importable():
    """确保 services.backup_dr_validate 可导入(它仅依赖 loguru / i18n)。"""
    if "services.backup_dr_validate" in sys.modules:
        return sys.modules["services.backup_dr_validate"]
    import importlib
    return importlib.import_module("services.backup_dr_validate")


def _ensure_restore_module_importable():
    """确保 services.db_restore 可导入(mock database.session / storage.r2 依赖)。"""
    if "services.db_restore" in sys.modules:
        return sys.modules["services.db_restore"]

    # 确保 database 包存在
    if "database" not in sys.modules:
        db_pkg = types.ModuleType("database")
        db_pkg.__path__ = []
        sys.modules["database"] = db_pkg

    if "database.session" not in sys.modules:
        mock_session = types.ModuleType("database.session")
        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.fetch = AsyncMock(return_value=[])
        mock_client.execute = AsyncMock()
        mock_session._client = mock_client
        mock_session.get_config = AsyncMock(return_value=None)
        mock_session._validate_identifier = lambda x: x.replace('"', '').replace(';', '')
        mock_session.init_db = AsyncMock()
        mock_session.close_db = AsyncMock()
        sys.modules["database.session"] = mock_session
        setattr(sys.modules["database"], "session", mock_session)

    if "database.cache_store" not in sys.modules:
        mock_cs = types.ModuleType("database.cache_store")
        mock_cs.DB_PATH = Path("/tmp/fake_cache_store_r62.db")
        sys.modules["database.cache_store"] = mock_cs
        setattr(sys.modules["database"], "cache_store", mock_cs)

    if "database.relay_db" not in sys.modules:
        mock_rdb = types.ModuleType("database.relay_db")
        mock_rdb.DB_PATH = Path("/tmp/fake_relay_pool_r62.db")
        sys.modules["database.relay_db"] = mock_rdb
        setattr(sys.modules["database"], "relay_db", mock_rdb)

    if "storage" not in sys.modules:
        storage_pkg = types.ModuleType("storage")
        storage_pkg.__path__ = []
        sys.modules["storage"] = storage_pkg
    if "storage.r2" not in sys.modules:
        mock_r2 = types.ModuleType("storage.r2")
        mock_r2_obj = MagicMock()
        mock_r2_obj._access_key = ""
        mock_r2._r2 = mock_r2_obj
        mock_r2.configure_r2_dynamic = AsyncMock()
        # R76 O7: configure_r2_dynamic 已重构为 configure_storage_from_settings
        mock_r2.configure_storage_from_settings = AsyncMock()
        sys.modules["storage.r2"] = mock_r2
        setattr(sys.modules.get("storage", types.ModuleType("storage")), "r2", mock_r2)

    import importlib
    try:
        importlib.import_module("services.db_backup")
    except Exception:
        sys.modules.pop("services.db_backup", None)
        importlib.import_module("services.db_backup")

    return importlib.import_module("services.db_restore")


def _build_valid_capability(
    backup_dr_validate_module,
    *,
    payload_digest: str = "d" * 64,
    backup_id: str = "backup_test_001",
    schema_fingerprint: str = "R62-P0-01-test-fingerprint",
    issuer: str = "test_issuer",
    ttl_seconds: int = 600,
):
    """构造一个合法的 _RestoreCapability(通过模块私有 sentinel)。"""
    return backup_dr_validate_module._RestoreCapability(
        backup_dr_validate_module._RESTORE_SENTINEL,
        backup_id=backup_id,
        manifest_sha256="a" * 64,
        payload_key="db_backup/payload_test.enc",
        ciphertext_sha256="b" * 64,
        plaintext_sha256="c" * 64,
        encryption_key_id="test_key_id",
        issuer=issuer,
        schema_fingerprint=schema_fingerprint,
        payload_digest=payload_digest,
        ttl_seconds=ttl_seconds,
    )


async def _fresh_store():
    """R63 P1-01: 构造一个真实 CacheStore(临时 DB 文件),用于 nonce 持久化测试。

    替代原 ``mod._CONSUMED_NONCES.clear()`` 的进程内清理 — 现在每次测试用全新
    DB 文件,天然隔离,且能验证跨"重启"(新建 CacheStore 实例)的持久化。
    """
    import tempfile
    from database.cache_store import CacheStore
    _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_test_")
    _db_path = str(Path(_tmp_dir) / "test_cache.db")
    _store = CacheStore(db_path=_db_path)
    await _store.init()
    return _store


def _build_operation_context(
    *,
    payload_digest: str,
    backup_id: str = "backup_test_001",
    schema_fingerprint: str = "R62-P0-01-test-fingerprint",
    nonce: str = "test_nonce_0123456789abcdef0123456789abcdef",
):
    """R76 P0-05: 构造合法的 RestoreOperationContext(独立 expected 值来源)。

    writer 用 operation_context.payload_digest 校验 actual bytes digest,
    替代旧 R63 的 verified_payload.payload_digest(同对象内字段,可被同时篡改)。
    """
    from services.restore_operation_context import RestoreOperationContext
    ctx = RestoreOperationContext(
        operation_id="op_test_001",
        backup_id=backup_id,
        source_sha="test_source_sha",
        run_id=0,
        run_attempt=1,
        audience="test_audience",
        target_identity=schema_fingerprint,
        target_uri="sqlite:///tmp/test_restore.db",
        manifest_digest="a" * 64,
        payload_digest=payload_digest,
        allowed_action="restore_to_blank_target",
        nonce=nonce,
    )
    ctx.validate()  # fail-closed
    return ctx


# ═══════════════════════════════════════════════════════════════
# 1-6: _RestoreCapability 单元测试
# ═══════════════════════════════════════════════════════════════


class TestRestoreCapabilityConstruction:
    """R62 P0-01: _RestoreCapability 增强后的构造与边界校验。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_valid_capability_passes_assert_valid(self):
        """用例 1: 合法 capability 通过 assert_valid(无异常抛出)。"""
        mod = _ensure_backup_dr_validate_importable()
        cap = _build_valid_capability(mod, payload_digest="d" * 64)
        store = await _fresh_store()
        # 不应抛出任何异常
        await cap.assert_valid(
            payload_digest="d" * 64,
            clock=time.time(),
            expected_scope="R62-P0-01-test-fingerprint",
            store=store,
        )

    @pytest.mark.asyncio
    async def test_expired_capability_raises(self):
        """用例 2: 过期 capability 抛 AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        store = await _fresh_store()
        # ttl_seconds=0 + 略微偏移时钟 → 立即过期
        cap = _build_valid_capability(mod, ttl_seconds=0)
        # 等待过期(时钟前进 1 秒)
        with pytest.raises(AppError) as exc_info:
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=time.time() + 1,  # 时钟前进 → 已过期
                expected_scope="R62-P0-01-test-fingerprint",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_replayed_capability_raises(self):
        """用例 3: 同一 capability 的 nonce 被消费两次后抛 AppError(防重放)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        store = await _fresh_store()
        cap = _build_valid_capability(mod, payload_digest="d" * 64)
        now = time.time()
        # 第一次调用 — 成功(nonce 原子消费到 DB)
        await cap.assert_valid(
            payload_digest="d" * 64,
            clock=now,
            expected_scope="R62-P0-01-test-fingerprint",
            store=store,
        )
        # 第二次调用同一 capability — 应抛 AppError(防重放)
        with pytest.raises(AppError) as exc_info:
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=now,
                expected_scope="R62-P0-01-test-fingerprint",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_mismatched_payload_digest_raises(self):
        """用例 4: payload_digest 与 capability 内嵌 digest 不匹配抛 AppError。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        store = await _fresh_store()
        # capability 内嵌 digest="d"*64
        cap = _build_valid_capability(mod, payload_digest="d" * 64)
        with pytest.raises(AppError) as exc_info:
            # 调用方传入不同的 digest(说明 payload 被替换/篡改)
            await cap.assert_valid(
                payload_digest="e" * 64,  # 不匹配
                clock=time.time(),
                expected_scope="R62-P0-01-test-fingerprint",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_scope_mismatch_raises(self):
        """附加: expected_scope 与 capability.schema_fingerprint 不匹配抛 AppError。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        store = await _fresh_store()
        cap = _build_valid_capability(
            mod, payload_digest="d" * 64,
            schema_fingerprint="correct_scope_v1",
        )
        with pytest.raises(AppError) as exc_info:
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=time.time(),
                expected_scope="wrong_scope_v2",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    def test_missing_required_fields_raises_at_construction(self):
        """用例 5: 必填字段缺失(空字符串)在构造时抛 AppError。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        # backup_id 为空 → 构造时应拒绝
        with pytest.raises((AppError, ValueError, RuntimeError)) as exc_info_empty_bid:
            mod._RestoreCapability(
                mod._RESTORE_SENTINEL,
                backup_id="",  # 空 — 必填
                manifest_sha256="a" * 64,
                payload_key="db_backup/payload_test.enc",
                ciphertext_sha256="b" * 64,
                plaintext_sha256="c" * 64,
                encryption_key_id="test_key_id",
                issuer="test_issuer",
                schema_fingerprint="R62-P0-01-test-fingerprint",
                payload_digest="d" * 64,
            )
        # 应为 AppError 或等价的 fail-closed 异常
        assert exc_info_empty_bid.value is not None

        # manifest_sha256 格式非法(非 64 hex) → 构造时应拒绝
        with pytest.raises((AppError, ValueError, RuntimeError)):
            mod._RestoreCapability(
                mod._RESTORE_SENTINEL,
                backup_id="backup_test_001",
                manifest_sha256="not_a_sha",  # 非法格式
                payload_key="db_backup/payload_test.enc",
                ciphertext_sha256="b" * 64,
                plaintext_sha256="c" * 64,
                encryption_key_id="test_key_id",
                issuer="test_issuer",
                schema_fingerprint="R62-P0-01-test-fingerprint",
                payload_digest="d" * 64,
            )

    def test_construction_without_sentinel_raises_runtime_error(self):
        """用例 6: 无 _RESTORE_SENTINEL 直接构造抛异常(RuntimeError 或 AppError)。

        R62 P1-04: data-integrity 域零容忍,capability 构造校验已协议化为 AppError。
        """
        mod = _ensure_backup_dr_validate_importable()

        # 用任意非 sentinel 对象构造 — 应抛异常(RuntimeError 或 AppError)
        with pytest.raises((RuntimeError, Exception)):
            mod._RestoreCapability(
                object(),  # 非 _RESTORE_SENTINEL
                backup_id="backup_test_001",
                manifest_sha256="a" * 64,
                payload_key="db_backup/payload_test.enc",
                ciphertext_sha256="b" * 64,
                plaintext_sha256="c" * 64,
                encryption_key_id="test_key_id",
                issuer="test_issuer",
                schema_fingerprint="R62-P0-01-test-fingerprint",
                payload_digest="d" * 64,
            )

        # 用 None 构造 — 也应抛异常
        with pytest.raises((RuntimeError, Exception)):
            mod._RestoreCapability(
                None,
                backup_id="backup_test_001",
                manifest_sha256="a" * 64,
                payload_key="db_backup/payload_test.enc",
                ciphertext_sha256="b" * 64,
                plaintext_sha256="c" * 64,
                encryption_key_id="test_key_id",
                issuer="test_issuer",
                schema_fingerprint="R62-P0-01-test-fingerprint",
                payload_digest="d" * 64,
            )

    def test_capability_is_immutable(self):
        """附加: capability 字段为只读 property,赋值抛 AttributeError。"""
        mod = _ensure_backup_dr_validate_importable()
        cap = _build_valid_capability(mod, payload_digest="d" * 64)

        # 通过 __slots__ + property getter,字段赋值应抛 AttributeError
        with pytest.raises(AttributeError):
            cap.backup_id = "tampered"
        with pytest.raises(AttributeError):
            cap.payload_digest = "e" * 64
        with pytest.raises(AttributeError):
            cap.nonce = "fake_nonce"


# ═══════════════════════════════════════════════════════════════
# VerifiedBackupPayload + _compute_payload_digest
# ═══════════════════════════════════════════════════════════════


class TestVerifiedBackupPayload:
    """R62 P0-02: VerifiedBackupPayload frozen dataclass + _compute_payload_digest。"""

    def test_payload_digest_is_canonical_sha256(self):
        """_compute_payload_digest 使用 canonical JSON(sort_keys + 紧凑分隔符)计算 sha256。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"b": 1, "a": 2, "tables": {"users": []}}
        digest = mod._compute_payload_digest(data)
        # 重新计算,验证一致性(canonical JSON: sort_keys=True, separators=(",",":"))
        expected = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        assert digest == expected
        assert len(digest) == 64

    def test_payload_digest_independent_of_key_order(self):
        """不同 key 顺序但内容相同的 dict 应产生相同 digest(canonical JSON)。"""
        mod = _ensure_backup_dr_validate_importable()
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert mod._compute_payload_digest(d1) == mod._compute_payload_digest(d2)

    def test_verified_backup_payload_is_frozen(self):
        """VerifiedBackupPayload 为 frozen dataclass,字段不可修改。"""
        mod = _ensure_backup_dr_validate_importable()
        payload = mod.VerifiedBackupPayload(
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R62-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(
                mod._enrich_payload_data(
                    {"tables": {"users": [{"user_id": 1}]}},
                    backup_id="backup_001", created_at="2024-01-01T00:00:00Z",
                )
            ),
        )
        # frozen=True → 赋值应抛 FrozenInstanceError(AttributeError 子类)
        with pytest.raises(Exception):  # FrozenInstanceError is AttributeError subclass
            payload.backup_id = "tampered"

    def test_verified_backup_payload_payload_digest_computed(self):
        """VerifiedBackupPayload.payload_digest 自动从 canonical_payload_bytes 计算。"""
        mod = _ensure_backup_dr_validate_importable()
        payload_data = mod._enrich_payload_data(
            {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"},
            backup_id="b001", created_at="2024-01-01T00:00:00Z",
        )
        canonical_bytes = mod._canonical_json_bytes(payload_data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R62-fingerprint",
            canonical_payload_bytes=canonical_bytes,
        )
        import hashlib as _hashlib
        expected = _hashlib.sha256(canonical_bytes).hexdigest()
        assert payload.payload_digest == expected


# ═══════════════════════════════════════════════════════════════
# P0-01: validate_and_restore_backup_strict 移除 skip/override 参数
# ═══════════════════════════════════════════════════════════════


class TestStrictRestoreNoSkipParams:
    """R62 P0-01: validate_and_restore_backup_strict 移除 skip_strict_validation /
    validation_note / 6 个 *_override 参数(签名层面禁止)。"""

    def test_signature_has_no_skip_params(self):
        """validate_and_restore_backup_strict 函数签名不含 skip_strict_validation /
        validation_note / 6 个 *_override 参数。"""
        mod = _ensure_backup_dr_validate_importable()
        import inspect
        sig = inspect.signature(mod.validate_and_restore_backup_strict)
        param_names = set(sig.parameters.keys())
        # 禁止的危险参数(必须已移除)
        forbidden = {
            "skip_strict_validation",
            "validation_note",
            "backup_id_override",
            "manifest_sha256_override",
            "payload_key_override",
            "ciphertext_sha256_override",
            "plaintext_sha256_override",
            "encryption_key_id_override",
        }
        leftover = forbidden & param_names
        assert not leftover, (
            f"validate_and_restore_backup_strict 仍含危险参数: {leftover}"
        )

    def test_skip_strict_validation_true_not_accepted(self):
        """调用方传 skip_strict_validation=True 应直接 TypeError(参数已不存在)。"""
        mod = _ensure_backup_dr_validate_importable()
        # TypeError: unexpected keyword argument 'skip_strict_validation'
        with pytest.raises(TypeError):
            # 不实际执行,仅触发签名校验(传 data=None 提前在函数内失败也行)
            import asyncio
            coro = mod.validate_and_restore_backup_strict(
                data={"tables": {}},
                skip_strict_validation=True,
            )
            # 关闭未等待的 coroutine 避免警告
            coro.close()


# ═══════════════════════════════════════════════════════════════
# 用例 7: 篡改 manifest/payload/ciphertext/plaintext/key-id 均在写入前失败
# ═══════════════════════════════════════════════════════════════


def _build_three_stage_backup(
    backup_dr_validate_module,
    *,
    backup_id: str = "20260718_120000",
    backup_type: str = "full",
    signing_key: bytes = b"r62_test_signing_key",
    schema_version: str = "R62-P0-01-test-fingerprint",
    plaintext: bytes = b'{"tables": {"users": [{"user_id": 1}]}}',
    tamper_manifest: bool = False,
    tamper_ciphertext: bool = False,
    tamper_plaintext: bool = False,
    tamper_key_id: bool = False,
):
    """构造一个完整的三段式备份(payload.enc / manifest.json / COMPLETE marker)。

    所有对象以 dict 形式返回,供 mock r2_storage.download 按 key 查找。

    篡改语义(模拟攻击者在签名后篡改对象):
        - tamper_manifest:  COMPLETE marker 中的 manifest_sha256 来自原始 manifest,
                            但 R2 上的 manifest.json 被替换为篡改后的版本(sha 不匹配)
        - tamper_ciphertext: manifest 中的 ciphertext_sha256 来自原始密文,
                            但 R2 上的 payload.enc 被替换为篡改后的版本(sha 不匹配)
        - tamper_plaintext:  manifest 中的 plaintext_sha256 来自原始明文,
                            但解密器返回篡改后的明文(sha 不匹配,模拟解密失败/被替换)
        - tamper_key_id:     manifest 中的 encryption.key_id 被改为"tampered_key_id",
                            AAD 中包含 tampered_key_id(而非原始 key_id),
                            解密器 mock 校验 AAD 中的 key_id,不匹配则抛异常(模拟 AEAD 验证失败)
    """
    mod = backup_dr_validate_module

    # 1. 构造原始密文(用"假装加密":plaintext 加前缀)
    original_ciphertext = b"CIPHERTEXT_" + plaintext
    # 2. 计算原始摘要(从原始数据,用于 manifest / COMPLETE marker 签名)
    original_ciphertext_sha = hashlib.sha256(original_ciphertext).hexdigest()
    plaintext_sha = hashlib.sha256(plaintext).hexdigest()

    # 3. 构造 manifest(含原始摘要 — 模拟签名时的状态)
    encryption_info = {
        "encrypted": True,
        "algorithm": "AES-256-GCM",
        "wrapped_dek": "fake_wrapped_dek",
        "nonce": "fake_nonce_b64",
        "key_id": "test_key_id_v1",  # 原始 key_id(用于 AAD 绑定)
    }

    manifest = {
        "version": 1,
        "commit_sha": "a" * 40,
        "schema_version": schema_version,
        "plaintext_sha256": plaintext_sha,
        "ciphertext_sha256": original_ciphertext_sha,  # 原始密文的 sha
        "backup_id": backup_id,
        # R65 P1-06: created_at 用于 _enrich_payload_data 补齐 canonical payload 必填字段
        "created_at": "2026-07-18T12:00:00Z",
        "content_size_bytes": len(plaintext),
        "backup_started_at": "2026-07-18T12:00:00",
        "backup_finished_at": "2026-07-18T12:00:01",
        "table_stats": {"users": {"row_count": 1}},
        "backup_type": backup_type,
        "encryption": encryption_info,
    }

    original_manifest_bytes = json.dumps(
        manifest, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
    # manifest_sha 来自原始 manifest(进入 COMPLETE marker 签名)
    manifest_sha = hashlib.sha256(original_manifest_bytes).hexdigest()

    # 4. 应用篡改(模拟 R2 上的对象被替换为篡改版本)
    actual_ciphertext = original_ciphertext
    if tamper_ciphertext:
        # 篡改密文:追加字节 → sha 不再匹配 manifest.ciphertext_sha256
        actual_ciphertext = original_ciphertext + b"TAMPERED"

    actual_manifest_bytes = original_manifest_bytes
    if tamper_manifest:
        # 篡改 manifest:加多余字段 → sha 不再匹配 COMPLETE marker.manifest_sha256
        tampered_manifest = json.loads(original_manifest_bytes)
        tampered_manifest["_tampered"] = True
        actual_manifest_bytes = json.dumps(
            tampered_manifest, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")

    # tamper_key_id: 修改 manifest.encryption.key_id(影响 AAD,使 AEAD 解密失败)
    if tamper_key_id:
        tampered_manifest = json.loads(actual_manifest_bytes)
        tampered_manifest["encryption"]["key_id"] = "tampered_key_id"
        actual_manifest_bytes = json.dumps(
            tampered_manifest, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
        # 注意: 此篡改不影响 manifest_sha(因为 manifest_sha 是从原始 manifest 计算的,
        # 进入 COMPLETE marker 签名)。所以 manifest_sha 检查不会捕获此篡改,
        # 但 AAD 中的 key_id 与原始 key_id 不匹配 → AEAD 解密失败。

    # 5. 构造 COMPLETE marker(用真实 build_complete_marker + 原始摘要确保签名正确)
    payload_key = mod.get_payload_key(backup_id, backup_type)
    complete_marker = mod.build_complete_marker(
        backup_id=backup_id,
        manifest_key=mod.get_manifest_key(backup_id, backup_type),
        manifest_sha256=manifest_sha,  # 原始 manifest 的 sha
        payload_key=payload_key,
        payload_sha256=original_ciphertext_sha,  # 原始密文的 sha
        signing_key=signing_key,
        schema_version=schema_version,
    )

    # 6. 解密器 mock:
    #    - tamper_plaintext: 返回篡改后的明文(sha 不匹配)
    #    - tamper_key_id: 校验 AAD 中的 key_id 是否为原始 "test_key_id_v1",
    #                     不匹配则抛 Exception(模拟 AEAD 解密失败)
    #    - 否则返回原始 plaintext(校验通过)
    decryptor = MagicMock()

    def _decrypt(ciphertext, aad=None):
        # tamper_key_id: AAD 中的 key_id 应为 "tampered_key_id"(从 manifest 读取),
        # 与原始 "test_key_id_v1" 不匹配 → AEAD 解密失败
        if tamper_key_id:
            aad_str = aad.decode("utf-8") if isinstance(aad, bytes) else str(aad)
            # AAD 格式: backup_id|schema_version|payload_key|key_id|plaintext_sha256
            parts = aad_str.split("|")
            if len(parts) >= 4 and parts[3] != "test_key_id_v1":
                raise ValueError(
                    f"AEAD verification failed: key_id mismatch "
                    f"(expected test_key_id_v1, got {parts[3]})"
                )
        # tamper_plaintext: 返回篡改后的明文
        if tamper_plaintext:
            return plaintext + b"TAMPERED"
        return plaintext

    decryptor.decrypt = _decrypt

    # 7. mock r2_storage.download 按 key 返回对应内容
    storage = MagicMock()
    async def _download(key):
        if key == mod.get_complete_key(backup_id, backup_type):
            return complete_marker
        if key == mod.get_manifest_key(backup_id, backup_type):
            return actual_manifest_bytes  # 可能被篡改
        if key == payload_key:
            return actual_ciphertext  # 可能被篡改
        return None
    storage.download = _download

    return {
        "backup_id": backup_id,
        "backup_type": backup_type,
        "signing_key": signing_key,
        "schema_version": schema_version,
        "expected_manifest_key": mod.get_manifest_key(backup_id, backup_type),
        "expected_backup_id": backup_id,
        "current_schema_version": schema_version,
        "r2_storage": storage,
        "decryptor": decryptor,
        "data": json.loads(plaintext.decode("utf-8")),
    }


class TestStrictValidationRejectsTampering:
    """用例 7: 篡改 manifest/payload/ciphertext/plaintext/key-id 均在写入前失败。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tamper_param",
        [
            "tamper_manifest",
            "tamper_ciphertext",
            "tamper_plaintext",
            "tamper_key_id",
        ],
    )
    async def test_tampered_trust_chain_fails_before_restore(self, tamper_param, monkeypatch):
        """篡改 manifest/ciphertext/plaintext/key-id 任一均使 validate_and_restore_backup_strict
        抛 AppError,且不调用 _restore_from_backup_data。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError

        tamper_kwargs = {p: False for p in (
            "tamper_manifest", "tamper_ciphertext", "tamper_plaintext", "tamper_key_id"
        )}
        tamper_kwargs[tamper_param] = True

        bundle = _build_three_stage_backup(mod, **tamper_kwargs)

        # Mock _restore_from_backup_data — 应 NOT 被调用
        # R69 Wave 2: _restore_from_backup_data 已从 services.db_restore 迁移到
        # services.restore_writer,backup_dr_validate.py 通过函数内 import 调用,
        # 因此需要同时 mock 两个模块路径(sys.modules 替换)。
        mock_writer = AsyncMock(return_value={"restored": {}, "skipped": [], "errors": []})
        mock_db_restore = types.ModuleType("services.db_restore")
        mock_db_restore._restore_from_backup_data = mock_writer
        monkeypatch.setitem(sys.modules, "services.db_restore", mock_db_restore)
        mock_restore_writer = types.ModuleType("services.restore_writer")
        mock_restore_writer._restore_from_backup_data = mock_writer
        monkeypatch.setitem(sys.modules, "services.restore_writer", mock_restore_writer)

        with pytest.raises(AppError):
            await mod.validate_and_restore_backup_strict(
                data=bundle["data"],
                tables=None,
                merge=False,
                timestamp=bundle["backup_id"],
                backup_type=bundle["backup_type"],
                r2_storage=bundle["r2_storage"],
                signing_key=bundle["signing_key"],
                decryptor=bundle["decryptor"],
                expected_manifest_key=bundle["expected_manifest_key"],
                expected_backup_id=bundle["expected_backup_id"],
                current_schema_version=bundle["current_schema_version"],
            )

        # 写入器不应被调用(信任链失败前已拒绝)
        assert not mock_writer.called, (
            f"{tamper_param}=True 时,_restore_from_backup_data 不应被调用"
        )

    @pytest.mark.asyncio
    async def test_valid_three_stage_backup_passes_and_calls_writer(self, monkeypatch):
        """附加: 合法三段式备份通过验证,调用 _restore_from_backup_data 并传入 VerifiedBackupPayload。"""
        # R76 P0-06: validate_and_restore_backup_strict 内部签发 capability 需要
        # RESTORE_CAPABILITY_SIGNING_KEY 环境变量(本地测试用固定 key)
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-restore-capability-signing-key-32b")
        mod = _ensure_backup_dr_validate_importable()
        bundle = _build_three_stage_backup(mod)

        # R76 P0-06: mock RestoreNonceStore.reserve 返回 True,避免依赖真实 cache_store
        # (测试环境下 cache_store 单例可能未初始化 SQLite/CRDB,导致 reserve 返回 False)
        # 同时 mock RestoreOperationContext.validate 通过(由 issue_capability 已生成合法字段)
        mock_nonce_store_instance = MagicMock()
        mock_nonce_store_instance.reserve = AsyncMock(return_value=True)
        mock_nonce_store_instance.consume = AsyncMock(return_value=True)
        mock_nonce_store_instance.fail = AsyncMock(return_value=True)
        mock_nonce_store_cls = MagicMock(return_value=mock_nonce_store_instance)
        monkeypatch.setattr(
            "services.restore_nonce_store.RestoreNonceStore", mock_nonce_store_cls,
        )

        # Mock 写入器,验证传入的是 VerifiedBackupPayload
        captured = {}

        async def _fake_writer(verified_payload, *, capability, operation_context,
                               nonce_store, tables=None, merge=False):
            captured["verified_payload"] = verified_payload
            captured["capability"] = capability
            captured["operation_context"] = operation_context
            captured["nonce_store"] = nonce_store
            captured["tables"] = tables
            captured["merge"] = merge
            return {"restored": {}, "skipped": [], "errors": []}

        mock_db_restore = types.ModuleType("services.db_restore")
        mock_db_restore._restore_from_backup_data = _fake_writer
        monkeypatch.setitem(sys.modules, "services.db_restore", mock_db_restore)
        # R69 Wave 2: backup_dr_validate.py 从 services.restore_writer import
        # _restore_from_backup_data(函数内 import),必须同时 mock 此路径
        mock_restore_writer = types.ModuleType("services.restore_writer")
        mock_restore_writer._restore_from_backup_data = _fake_writer
        monkeypatch.setitem(sys.modules, "services.restore_writer", mock_restore_writer)

        await mod.validate_and_restore_backup_strict(
            data=bundle["data"],
            tables=None,
            merge=False,
            timestamp=bundle["backup_id"],
            backup_type=bundle["backup_type"],
            r2_storage=bundle["r2_storage"],
            signing_key=bundle["signing_key"],
            decryptor=bundle["decryptor"],
            expected_manifest_key=bundle["expected_manifest_key"],
            expected_backup_id=bundle["expected_backup_id"],
            current_schema_version=bundle["current_schema_version"],
        )

        # 验证传入的是 VerifiedBackupPayload 实例(而非 raw dict)
        assert isinstance(captured["verified_payload"], mod.VerifiedBackupPayload)
        # R76 P0-05: operation_context 提供独立 expected 值,与 verified_payload 共享
        # 信任链字段(manifest_sha256 / backup_id)一致(均来自已验证的 manifest)
        assert captured["operation_context"].manifest_digest == captured["verified_payload"].manifest_sha256
        assert captured["operation_context"].backup_id == captured["verified_payload"].backup_id
        # R76 P0-05: operation_context.payload_digest 必须为 64 hex(非空,fail-closed)
        assert len(captured["operation_context"].payload_digest) == 64
        # R76 P0-06: capability 是 dict(HMAC 签名),nonce 与 operation_context.nonce 一致
        assert captured["capability"]["nonce"] == captured["operation_context"].nonce
        # nonce_store 被注入(非 None)
        assert captured["nonce_store"] is not None


# ═══════════════════════════════════════════════════════════════
# 用例 8: run_restore() 旧格式备份直接失败
# ═══════════════════════════════════════════════════════════════


class TestRunRestoreFailsOnLegacyFormat:
    """用例 8: run_restore() 检测到旧格式备份时直接失败,
    不再通过 skip_strict_validation 绕过验证。

    R63 P0-06: run_restore 改为接受 backup_id 参数,从 COMPLETE marker 发现备份。
    旧格式备份(无 COMPLETE marker)在 strict service 内自然 fail-closed。
    本测试更新为:传入 backup_id,Mock R2 返回 None(COMPLETE marker 不存在),
    验证 run_restore 以 AppError 失败且日志指向离线导入/迁移工具。
    """

    @pytest.mark.asyncio
    async def test_run_restore_old_format_fails(self, monkeypatch, caplog):
        """run_restore(backup_id=...) 在无 COMPLETE marker(旧格式备份特征)时
        必须以 AppError 失败,错误消息明确指向离线导入/迁移工具。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes
        # 项目用 loguru,caplog 不捕获 → 用 loguru 日志拦截器收集 ERROR 级别
        from loguru import logger as _loguru_logger
        captured_logs: list[str] = []
        _sink_id = _loguru_logger.add(
            lambda msg: captured_logs.append(msg.record["message"]),
            level="ERROR",
            format="{message}",
        )

        # R63 P0-06: 不再 mock get_latest_backup(已从 run_restore 移除)
        # 旧格式备份特征:R2 上无 COMPLETE_{backup_id}_full.COMPLETE marker
        # Mock R2 配置 + download 返回 None(模拟旧格式备份无 COMPLETE marker)
        mock_r2 = MagicMock()
        mock_r2._access_key = "fake"
        mock_r2.configure = MagicMock(return_value=None)
        mock_r2.connect = AsyncMock(return_value=None)
        mock_r2.close = AsyncMock(return_value=None)
        mock_r2.download = AsyncMock(return_value=None)  # COMPLETE marker 不存在
        monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

        # Mock settings(已在 conftest 中注入)
        from config import settings as _settings
        monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")
        monkeypatch.setattr(_settings, "R2_ACCESS_KEY_ID", "fake_ak")
        monkeypatch.setattr(_settings, "R2_SECRET_ACCESS_KEY", "fake_sk")
        monkeypatch.setattr(_settings, "R2_BUCKET_NAME", "fake_bucket")
        monkeypatch.setattr(_settings, "R2_ENDPOINT", "")
        # R63 P0-06: 需配置 BACKUP_SIGNING_KEY 与 BACKUP_KEK 才能进入 strict service
        monkeypatch.setattr(_settings, "BACKUP_SIGNING_KEY", b"fake_signing_key")
        monkeypatch.setattr(_settings, "BACKUP_KEK", "fake_kek_for_test")

        # Mock decryptor(避免因 BACKUP_KEK 格式问题失败)
        from unittest.mock import MagicMock as _MM
        mock_decryptor = _MM()
        mock_decryptor.decrypt = lambda ct, aad=None: b'{"tables": {}}'
        monkeypatch.setattr(db_restore, "_build_cli_decryptor", lambda: mock_decryptor)

        # R63 P0-06: 调用 run_restore 必须传入 backup_id
        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(
                backup_id="20260718_120000",
                table=None,
                dry_run=True,
            )

        # 错误码:信任链失败(无法在不通过严格验证的情况下恢复)
        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # 错误消息应指向离线导入/迁移工具(在 logger.error 中明确告知用户)
        # AppError 自身只暴露 safe_params(backup_id),迁移提示通过日志告知用户
        log_text = "\n".join(captured_logs)
        _loguru_logger.remove(_sink_id)
        assert (
            "迁移" in log_text or "migration" in log_text.lower() or "离线" in log_text
        ), f"日志应指向离线导入/迁移工具,实际日志: {log_text}"

    @pytest.mark.asyncio
    async def test_run_restore_without_backup_id_fails(self, monkeypatch):
        """R63 P0-06: run_restore 不传 backup_id 必须以 AppError 失败。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        # Mock R2(不应被调用)
        mock_r2 = MagicMock()
        mock_r2.configure = MagicMock(return_value=None)
        mock_r2.connect = AsyncMock(return_value=None)
        monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

        from config import settings as _settings
        monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id=None, dry_run=True)

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_db_backup_restore_from_backup_old_format_fails(self, monkeypatch):
        """附加: db_backup.restore_from_backup 对旧格式 key 也必须失败。"""
        _ensure_restore_module_importable()
        from services import db_backup
        from services.error_codes import AppError

        legacy_backup = {
            "backup_time": "2026-07-18T12:00:00Z",
            "tables": {"users": [{"user_id": 1}]},
        }
        mock_content = json.dumps(legacy_backup).encode("utf-8")

        mock_r2 = MagicMock()
        mock_r2._access_key = "fake"
        mock_r2.download = AsyncMock(return_value=mock_content)
        monkeypatch.setattr(db_backup, "r2_storage", mock_r2)
        # R76 O7: configure_r2_dynamic 已重构为 configure_storage_from_settings
        monkeypatch.setattr(db_backup, "configure_storage_from_settings", AsyncMock())
        # R67 P0-06 / R65 P0-07: 旧格式 key 拒绝逻辑在 capability-seal 之后,
        # 需设置 ALLOW_LEGACY_RESTORE=1 才能到达旧格式 key 检测分支
        monkeypatch.setenv("ALLOW_LEGACY_RESTORE", "1")
        # 确保非生产环境(_production_guard 不阻断)
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("DEPLOY_ENV", raising=False)

        # 旧格式 key: db_backup/db_backup_*.json
        with pytest.raises(AppError):
            await db_backup.restore_from_backup(
                "db_backup/db_backup_20260718_120000_full.json",
                merge=False,
            )


# ═══════════════════════════════════════════════════════════════
# 用例 9: _restore_from_backup_data 从 verified_payload.tables 读取
# ═══════════════════════════════════════════════════════════════


class TestRestoreFromBackupDataReadsVerifiedPayload:
    """用例 9: _restore_from_backup_data 从 verified_payload.tables 读取,
    不再从 data.get("tables", {}) 读取。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_reads_from_verified_payload_tables(self, monkeypatch):
        """_restore_from_backup_data 从 verified_payload.tables 读取,
        即使 data 中 tables 字段不同也不受影响。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        mod = _ensure_backup_dr_validate_importable()
        from unittest.mock import patch

        # R63 P1-01: _restore_from_backup_data 内部调用 assert_valid(无 store= 参数),
        # assert_valid 会通过 get_cache_store() 获取单例。注入真实 CacheStore(临时 DB)
        # 以完成 nonce 原子消费,否则未初始化的单例会令 consume 返回 False → AppError。
        store = await _fresh_store()
        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: store)

        # verified_payload.tables 包含真实数据
        # R64 P1-01: 单一 canonical bytes 来源 — tables/payload 均从 canonical_payload_bytes 解码
        # R65 P1-06: 用 _enrich_payload_data 补齐 canonical payload 必填字段
        verified_payload = mod.VerifiedBackupPayload(
            backup_id="backup_test_001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R62-P0-01-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(
                mod._enrich_payload_data(
                    {"tables": {"users": [{"user_id": 1, "username": "test"}]}},
                    backup_id="backup_test_001", created_at="2024-01-01T00:00:00Z",
                )
            ),
        )
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R62-P0-01-test-fingerprint",
        )

        # R76 P0-05/P0-06: writer 需要 RESTORE_CAPABILITY_SIGNING_KEY env + operation_context + nonce_store
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-restore-capability-signing-key-32b")
        monkeypatch.setattr(
            "services.restore_capability_file.verify_and_consume_capability",
            AsyncMock(return_value=True),
        )
        # R76 P0-05: 用真实 RestoreOperationContext(独立 payload_digest 来源)
        operation_context = _build_operation_context(
            payload_digest=cap.payload_digest,
            backup_id="backup_test_001",
            schema_fingerprint="R62-P0-01-test-fingerprint",
        )

        captured = {}

        async def _fake_crdb(tables_data, merge, result):
            captured["crdb_tables"] = tables_data
        async def _fake_sqlite(tables_data, merge, result, is_relay=False):
            captured["sqlite_tables"] = tables_data

        # R69 Wave 2: _restore_from_backup_data 已从 services.db_restore 迁移到
        # services.restore_writer,函数体内查找的是 services.restore_writer.__dict__,
        # 因此 patch 路径必须指向 services.restore_writer(同时 patch db_restore
        # 保持向后兼容)。
        with patch("services.db_restore._restore_crdb_tables", _fake_crdb), \
             patch("services.db_restore._restore_sqlite_tables_to_db", _fake_sqlite), \
             patch("services.restore_writer._restore_crdb_tables", _fake_crdb), \
             patch("services.restore_writer._restore_sqlite_tables_to_db", _fake_sqlite):
            result = await _restore_from_backup_data(
                verified_payload,
                capability=cap,
                operation_context=operation_context,
                nonce_store=MagicMock(),
                tables=None,
                merge=False,
            )

        # 验证:users 表(来自 verified_payload.tables)被传给 CRDB 恢复
        # R63 P0-02: tables 已深冻结(tuple of MappingProxyType),用 _to_serializable 还原后比较
        assert "users" in captured["crdb_tables"]
        from services.backup_dr_validate import _to_serializable
        users_data = _to_serializable(captured["crdb_tables"]["users"])
        assert users_data == [{"user_id": 1, "username": "test"}]

    @pytest.mark.asyncio
    async def test_capability_assert_valid_is_first_statement(self, monkeypatch):
        """附加: _restore_from_backup_data 首条语句调用 capability.assert_valid,
        无效 capability 时立即抛异常(不读 tables)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError

        verified_payload = mod.VerifiedBackupPayload(
            backup_id="backup_test_001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R62-P0-01-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(
                mod._enrich_payload_data(
                    {"tables": {"users": [{"user_id": 1}]}},
                    backup_id="backup_test_001", created_at="2024-01-01T00:00:00Z",
                )
            ),
        )
        # 构造一个 capability,但其 payload_digest 与 verified_payload 不匹配
        cap = _build_valid_capability(
            mod,
            payload_digest="0" * 64,  # 与 verified_payload.payload_digest 不匹配
            schema_fingerprint="R62-P0-01-test-fingerprint",
        )

        # R76 P0-05/P0-06: writer 需要 RESTORE_CAPABILITY_SIGNING_KEY env + operation_context + nonce_store
        monkeypatch.setenv("RESTORE_CAPABILITY_SIGNING_KEY", "test-restore-capability-signing-key-32b")
        monkeypatch.setattr(
            "services.restore_capability_file.verify_and_consume_capability",
            AsyncMock(return_value=True),
        )
        # R76 P0-05: 用真实 RestoreOperationContext(独立 payload_digest 来源)
        # operation_context.payload_digest 与 cap.payload_digest 一致("0"*64),
        # 与 verified_payload.payload_digest 不匹配 → writer digest 校验 fail-closed
        operation_context = _build_operation_context(
            payload_digest=cap.payload_digest,
            backup_id="backup_test_001",
            schema_fingerprint="R62-P0-01-test-fingerprint",
        )

        # 调用应在首条语句(assert_valid)抛 AppError,不读 tables
        from unittest.mock import patch
        read_called = MagicMock()

        async def _should_not_be_called(*args, **kwargs):
            read_called()
            return {"restored": {}, "skipped": [], "errors": []}

        with patch("services.db_restore._restore_crdb_tables", _should_not_be_called), \
             patch("services.db_restore._restore_sqlite_tables_to_db", _should_not_be_called):
            with pytest.raises(AppError):
                await _restore_from_backup_data(
                    verified_payload,
                    capability=cap,
                    operation_context=operation_context,
                    nonce_store=MagicMock(),
                    tables=None,
                    merge=False,
                )

        # 验证:写入器未读取任何表(assert_valid 在前)
        assert not read_called.called
